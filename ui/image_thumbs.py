"""Thumbnail cache for the image library.

Why this exists
---------------
A photo grid must never decode full-resolution originals on the GUI thread: a
24 MP JPEG is ~36 MB once expanded and ~150 ms to decode, and a grid repaint
asks for dozens of cells at once. So every thumbnail is

    1. decoded once in a worker thread from the source file (HEIC included,
       through ``src.core.image_io.load_image_bgr``),
    2. written to ``~/.videoseek/cache/image_thumbs/<image_id>_<size>.jpg`` so
       it survives restarts, and
    3. kept in a small in-memory LRU of ``QImage`` for scroll-back.

``image_id`` is the file content hash, so a photo that changes on disk gets a
new id and therefore a new cache entry — no explicit invalidation needed.
"""

from __future__ import annotations

import os
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

from PySide6.QtCore import QObject, Signal

# Longest-edge sizes actually used by the UI.
THUMB_SIZE_GRID = 256
THUMB_SIZE_DETAIL = 1024

_CACHE_DIR_NAME = "image_thumbs"
_MAX_INFLIGHT = 8
_MEMORY_ENTRIES = 512


def thumb_cache_dir() -> str:
    from src.infra.paths import get_app_data_dir

    path = os.path.join(get_app_data_dir(), "cache", _CACHE_DIR_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def thumb_path(image_id: str, size: int) -> str:
    safe = "".join(ch for ch in str(image_id or "") if ch.isalnum())[:64] or "unknown"
    return os.path.join(thumb_cache_dir(), f"{safe}_{int(size)}.jpg")


def qimage_from_bgr(image_bgr):
    """BGR ndarray -> detached ``QImage`` (safe to hand across threads)."""
    import cv2
    import numpy as np
    from PySide6.QtGui import QImage

    rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
    rgb = np.ascontiguousarray(rgb)
    height, width, channels = rgb.shape
    return QImage(rgb.data, width, height, channels * width, QImage.Format.Format_RGB888).copy()


def _decode_for_thumb(image_path: str, target: int):
    """Decode at roughly ``target`` pixels instead of full resolution.

    ``cv2.imdecode`` can downscale while decoding (``IMREAD_REDUCED_*``), which
    is 4-8x faster and allocates far less than "decode 24 MP, then resize".
    HEIC/AVIF have no libjpeg DCT shortcut, so they go through the normal loader.
    """
    import cv2
    import numpy as np

    ext = os.path.splitext(image_path)[1].lower()
    if ext in {".heic", ".heif", ".heics", ".heifs", ".avif"}:
        from src.core.image_io import load_image_bgr

        return load_image_bgr(image_path)

    try:
        buffer = np.fromfile(image_path, dtype=np.uint8)
    except OSError:
        return None
    if buffer.size == 0:
        return None

    # Coarse first pass, then sharpen only if the small decode lost detail.
    if target <= 384:
        plan = (cv2.IMREAD_REDUCED_COLOR_4, cv2.IMREAD_REDUCED_COLOR_2, cv2.IMREAD_COLOR)
    else:
        plan = (cv2.IMREAD_REDUCED_COLOR_2, cv2.IMREAD_COLOR)

    best = None
    for flag in plan:
        image = cv2.imdecode(buffer, flag)
        if image is None:
            break
        best = image
        if max(image.shape[:2]) >= int(target):
            return image
    if best is not None:
        return best
    from src.core.image_io import load_image_bgr

    return load_image_bgr(image_path)


def build_thumb(image_path: str, *, image_id: str, size: int) -> str:
    """Return the cached thumbnail path, generating it when missing.

    Raises ``RuntimeError`` when the source cannot be decoded; callers fall back
    to a placeholder instead of blocking the grid.
    """
    target = thumb_path(image_id, size)
    if os.path.isfile(target) and os.path.getsize(target) > 0:
        return target
    src_stat = os.stat(image_path)
    if os.path.isfile(target) and os.path.getmtime(target) >= src_stat.st_mtime:
        return target

    image = _decode_for_thumb(image_path, int(size))
    if image is None:
        raise RuntimeError(f"decode failed: {image_path}")

    import cv2

    height, width = image.shape[:2]
    longest = max(height, width, 1)
    scale = min(1.0, float(size) / float(longest))
    if scale < 1.0:
        image = cv2.resize(
            image,
            (max(1, int(round(width * scale))), max(1, int(round(height * scale)))),
            interpolation=cv2.INTER_AREA,
        )

    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp = f"{target}.{os.getpid()}.tmp.jpg"
    ok, buffer = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), 86])
    if not ok:
        raise RuntimeError(f"encode failed: {image_path}")
    with open(tmp, "wb") as handle:
        handle.write(buffer.tobytes())
    os.replace(tmp, target)
    return target


class ImageThumbLoader(QObject):
    """Loads thumbnails off the GUI thread and delivers ``QImage`` by key."""

    loaded = Signal(str, object)  # (key, QImage | None)
    failed = Signal(str, str)  # (key, reason)

    def __init__(self, parent=None, *, workers: int = 4):
        super().__init__(parent)
        self._pool = ThreadPoolExecutor(
            max_workers=max(1, int(workers)), thread_name_prefix="vs-img-thumb"
        )
        self._lock = Lock()
        self._memory: OrderedDict[str, object] = OrderedDict()
        self._inflight: set[str] = set()
        self._closed = False

    # -- cache ----------------------------------------------------------
    def _remember(self, key: str, image) -> None:
        with self._lock:
            self._memory[key] = image
            self._memory.move_to_end(key)
            while len(self._memory) > _MEMORY_ENTRIES:
                self._memory.popitem(last=False)

    def peek(self, key: str):
        """Cached image for ``key`` (``None`` when absent). GUI thread only."""
        with self._lock:
            image = self._memory.get(key)
            if image is not None:
                self._memory.move_to_end(key)
            return image

    @staticmethod
    def make_key(image_id: str, size: int) -> str:
        return f"{image_id}:{int(size)}"

    # -- loading --------------------------------------------------------
    def request(self, image_id: str, image_path: str, size: int) -> None:
        """Ask for one thumbnail. Safe to call repeatedly; duplicates collapse."""
        key = self.make_key(image_id, size)
        cached = self.peek(key)
        if cached is not None:
            self.loaded.emit(key, cached)
            return
        if self._closed:
            return
        with self._lock:
            if key in self._inflight or len(self._inflight) >= _MAX_INFLIGHT:
                # Coalescing keeps scrolling cheap: the view asks again on repaint.
                return
            self._inflight.add(key)
        try:
            self._pool.submit(self._work, key, image_id, image_path, int(size))
        except RuntimeError:  # pool already shut down
            with self._lock:
                self._inflight.discard(key)

    def _work(self, key: str, image_id: str, image_path: str, size: int) -> None:
        try:
            cached = self.peek(key)
            if cached is not None:
                self.loaded.emit(key, cached)
                return
            path = build_thumb(image_path, image_id=image_id, size=size)
            from src.core.image_io import load_image_bgr

            image = load_image_bgr(path)
            if image is None:
                raise RuntimeError("thumbnail unreadable")
            qimage = qimage_from_bgr(image)
            self._remember(key, qimage)
            self.loaded.emit(key, qimage)
        except Exception as exc:
            self.failed.emit(key, str(exc))
            self.loaded.emit(key, None)
        finally:
            with self._lock:
                self._inflight.discard(key)

    def shutdown(self) -> None:
        self._closed = True
        try:
            self._pool.shutdown(wait=False, cancel_futures=True)
        except TypeError:  # Python < 3.9
            self._pool.shutdown(wait=False)
        with self._lock:
            self._memory.clear()
            self._inflight.clear()
