"""Image library page: register folders, index photos, search them.

Design notes
------------
* The grid is a ``QListView`` in icon mode with a custom delegate, not a pile of
  widgets. Only visible cells are painted, so 50k photos scroll fine; the model
  may hold every row.
* Thumbnails never decode on the GUI thread. ``ui.image_thumbs.ImageThumbLoader``
  produces disk-cached 256 px JPEGs, and the delegate falls back to a placeholder
  until the signal arrives.
* Indexing runs in a ``QThread`` wrapping ``image_index_service.index_images``
  which already accepts ``progress`` and ``should_stop`` callbacks. Search and
  status also run off-thread: a semantic query is a CLIP forward pass.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import weakref

from PySide6.QtCore import (
    QAbstractListModel,
    QModelIndex,
    QRect,
    QSize,
    Qt,
    QThread,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QDesktopServices,
    QPainter,
    QPainterPath,
    QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from ui.widgets.button_roles import set_button_role
from ui.widgets.scaffold import PageScaffold, VSCard, VSProgressStatusRow

HIT_ROLE = int(Qt.ItemDataRole.UserRole) + 1
THUMB_ROLE = HIT_ROLE + 1

CELL_WIDTH = 178
CELL_HEIGHT = 214

# "Browse all" reads whole rows without vectors; cap it so a six-figure library
# does not build a million-key dicts on every page open. Search is unbounded.
BROWSE_LIMIT = 20000

_STRINGS = {
    "zh": {
        "title": "图片库",
        "subtitle": "给照片和素材图建语义索引：文字描述找画面，也能按人物特征筛。索引与推理都在本机。",
        "library_card": "照片目录",
        "search_card": "检索",
        "add_root": "添加文件夹",
        "remove_root": "移除选中",
        "open_root": "打开目录",
        "rescan": "重新扫描",
        "prune": "清理失效",
        "prune_running": "正在检查失效记录…",
        "prune_done": "已清理 {removed} 条失效记录（文件已被移动或删除）",
        "prune_none": "没有失效记录",
        "start_index": "开始索引",
        "stop": "停止",
        "describe_toggle": "同时生成描述（需 VLM）",
        "force_toggle": "忽略已索引，强制重算",
        "roots_empty": "还没有登记目录。点「添加文件夹」，选一个照片目录（可多选子目录）。",
        "status_idle": "尚未索引",
        "status_summary": "已索引 {indexed} 张 · 已描述 {described} 张 · {dimension} 维",
        "vlm_ready": "VLM 就绪",
        "vlm_down": "VLM 未启动，描述将跳过",
        "scan_running": "正在扫描照片…",
        "scanned": "发现 {count} 张照片，其中已索引 {indexed} 张",
        "index_running": "{done}/{total} · {name} · 已用 {elapsed}",
        "index_running_eta": "{done}/{total} · {name} · 剩余约 {eta}",
        "index_done": "索引完成：新增 {added}、更新 {updated}、跳过 {skipped}、失败 {failed}、描述 {described}",
        "index_stopped": "已停止，已处理 {scanned} 张",
        "index_failed": "索引失败：{error}",
        "search_placeholder": "描述画面内容，或输入关键词",
        "mode_text": "语义检索",
        "mode_keyword": "关键词",
        "mode_image": "以图搜图",
        "browse_all": "浏览全部",
        "pick_image": "选一张图…",
        "min_people": "人数",
        "top_k": "结果数",
        "search": "搜索",
        "searching": "检索中…",
        "results_n": "{count} 张结果 · {elapsed}",
        "results_vector_only": "没有文字匹配，以下是向量近似结果，可能不相关",
        "results_empty": "没有命中。换个说法，或先用「浏览全部」确认索引是否有内容。",
        "browse_empty": "还没有索引任何照片。点「添加文件夹」选一个照片目录，再点「开始索引」。",
        "search_failed": "检索失败：{error}",
        "model_hint_en": "当前模型（{model}）按英文训练：中文查询会先匹配文件名与描述文字，命中不足再用向量补位。要按画面内容理解中文，建议在「设置 → 模型」切到 Chinese CLIP 或 SigLIP2。",
        "detail_empty": "选中一张图片查看详情",
        "detail_open": "打开图片",
        "detail_reveal": "在访达中显示",
        "detail_people": "人物",
        "detail_tags": "标签",
        "detail_caption": "描述",
        "detail_meta": "{width}×{height} · {size} · {mtime}",
        "detail_no_caption": "这张还没有描述（未运行 VLM）。",
        "confirm_big_title": "索引量大",
        "confirm_big_body": "本次将处理 {count} 张照片，可能要跑很久。继续吗？",
        "no_files": "没有发现可索引的照片。",
        "drop_hint": "也可以把照片文件夹拖进来登记，或把一张图片拖进来做以图搜图。",
    },
    "en": {
        "title": "Photo Library",
        "subtitle": "Semantic index for photos: describe a scene, or filter by the people in it. All local.",
        "library_card": "Folders",
        "search_card": "Search",
        "add_root": "Add folder",
        "remove_root": "Remove selected",
        "open_root": "Open folder",
        "rescan": "Rescan",
        "prune": "Clean stale",
        "prune_running": "Checking for stale rows…",
        "prune_done": "Removed {removed} rows whose files no longer exist",
        "prune_none": "No stale rows",
        "start_index": "Start indexing",
        "stop": "Stop",
        "describe_toggle": "Also describe (needs VLM)",
        "force_toggle": "Re-index everything",
        "roots_empty": "No folders yet. Click “Add folder” and pick a photo directory.",
        "status_idle": "Not indexed yet",
        "status_summary": "{indexed} indexed · {described} described · {dimension}-dim",
        "vlm_ready": "VLM ready",
        "vlm_down": "VLM offline — descriptions skipped",
        "scan_running": "Scanning photos…",
        "scanned": "{count} photos found, {indexed} already indexed",
        "index_running": "{done}/{total} · {name} · {elapsed} elapsed",
        "index_running_eta": "{done}/{total} · {name} · ~{eta} left",
        "index_done": "Done: {added} added, {updated} updated, {skipped} skipped, {failed} failed, {described} described",
        "index_stopped": "Stopped after {scanned} photos",
        "index_failed": "Indexing failed: {error}",
        "search_placeholder": "Describe the scene, or type keywords",
        "mode_text": "Semantic",
        "mode_keyword": "Keyword",
        "mode_image": "By image",
        "browse_all": "Browse all",
        "pick_image": "Pick an image…",
        "min_people": "People",
        "top_k": "Results",
        "search": "Search",
        "searching": "Searching…",
        "results_n": "{count} results · {elapsed}",
        "results_vector_only": "no text match — showing nearest vectors, likely unrelated",
        "results_empty": "No hits. Try another wording, or use “Browse all” to check the index.",
        "browse_empty": "Nothing indexed yet. Click “Add folder”, pick a photo directory, then “Start indexing”.",
        "search_failed": "Search failed: {error}",
        "model_hint_en": "The active model ({model}) is English-trained: Chinese queries match file names and descriptions first, then backfill with vectors. For real Chinese scene understanding, switch to Chinese CLIP or SigLIP2 in Settings → Models.",
        "detail_empty": "Select a photo to see details",
        "detail_open": "Open",
        "detail_reveal": "Reveal in Finder",
        "detail_people": "People",
        "detail_tags": "Tags",
        "detail_caption": "Description",
        "detail_meta": "{width}×{height} · {size} · {mtime}",
        "detail_no_caption": "No description yet (VLM has not run).",
        "confirm_big_title": "Large batch",
        "confirm_big_body": "About to index {count} photos. This can take a while. Continue?",
        "no_files": "No indexable photos found.",
        "drop_hint": "You can also drop a photo folder here, or drop one image to search by it.",
    },
}


def _human_bytes(value: int) -> str:
    size = float(value or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} GB"


def _human_duration(seconds: float) -> str:
    total = int(max(0, seconds))
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def _reveal_in_file_manager(path: str) -> None:
    if not path:
        return
    if sys.platform == "darwin":
        try:
            subprocess.Popen(["open", "-R", path])
            return
        except OSError:
            pass
    folder = path if os.path.isdir(path) else os.path.dirname(path)
    QDesktopServices.openUrl(QUrl.fromLocalFile(folder))


def _people_text(person: dict) -> str:
    fields = ("gender", "age", "clothing", "action", "position", "features")
    bits = [str(person.get(name) or "").strip() for name in fields]
    text = " · ".join(bit for bit in bits if bit)
    count = int(person.get("count") or 1)
    return f"{count}× {text}" if text else (f"{count} 人" if count else "")


# ---------------------------------------------------------------------------
# Workers
# ---------------------------------------------------------------------------


class _ScanWorker(QThread):
    scanned = Signal(list)
    failed = Signal(str)

    def __init__(self, root: str = "", parent=None):
        super().__init__(parent)
        self._root = str(root or "")

    def run(self):
        try:
            from src.services import image_index_service as service

            if self._root:
                files = service.scan_image_files(self._root)
            else:
                files = service.scan_manifest_roots()
            self.scanned.emit(list(files))
        except Exception as exc:  # pragma: no cover - defensive
            self.failed.emit(str(exc))


class _IndexWorker(QThread):
    progressed = Signal(int, int, str)
    indexed = Signal(dict)
    failed = Signal(str)

    def __init__(self, paths, *, describe: bool, force: bool, roots, parent=None):
        super().__init__(parent)
        self._paths = list(paths)
        self._describe = bool(describe)
        self._force = bool(force)
        self._roots = [str(root) for root in (roots or []) if str(root or "").strip()]
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def _root_for(self, path: str) -> str:
        # Longest registered folder wins, so nested libraries resolve correctly.
        best = ""
        for root in self._roots:
            if path == root or path.startswith(root.rstrip(os.sep) + os.sep):
                if len(root) > len(best):
                    best = root
        return best

    def run(self):
        try:
            from src.services import image_index_service as service

            def _progress(done: int, total: int, path: str) -> None:
                self.progressed.emit(int(done), int(total), str(path))

            result = service.index_images(
                self._paths,
                describe=self._describe,
                force=self._force,
                progress=_progress,
                should_stop=lambda: self._stop,
                root_for_path=self._root_for if self._roots else None,
            )
            self.indexed.emit(result)
        except Exception as exc:  # pragma: no cover - defensive
            self.failed.emit(str(exc))


class _SearchWorker(QThread):
    results = Signal(list)
    failed = Signal(str)

    def __init__(self, mode: str, query: str, *, top_k: int, min_people: int, parent=None):
        super().__init__(parent)
        self._mode = str(mode or "text")
        self._query = str(query or "")
        self._top_k = int(top_k)
        self._min_people = int(min_people)

    def run(self):
        try:
            from src.services import image_index_service as service

            if self._mode == "browse":
                hits = service.list_indexed_images(limit=BROWSE_LIMIT)
            elif self._mode == "keyword":
                hits = service.search_by_keyword(
                    self._query, top_k=self._top_k, min_people=self._min_people
                )
            elif self._mode == "image":
                hits = service.search_by_image(self._query, top_k=self._top_k)
            else:
                # search_by_query keeps English queries on the CLIP path and
                # routes Chinese ones through a literal scan first, because the
                # bundled text tower is English-only.
                hits = service.search_by_query(
                    self._query, top_k=self._top_k, min_people=self._min_people
                )
            self.results.emit(list(hits))
        except Exception as exc:
            self.failed.emit(str(exc))


class _PruneWorker(QThread):
    pruned = Signal(dict)
    failed = Signal(str)

    def run(self):
        try:
            from src.services import image_index_service as service

            self.pruned.emit(service.prune_missing_images())
        except Exception as exc:  # pragma: no cover - defensive
            self.failed.emit(str(exc))


class _StatusWorker(QThread):
    ready = Signal(dict)
    failed = Signal(str)

    def run(self):
        try:
            from src.services import image_index_service as service

            self.ready.emit(service.get_image_library_status())
        except Exception as exc:  # pragma: no cover - defensive
            self.failed.emit(str(exc))


# ---------------------------------------------------------------------------
# Model / delegate
# ---------------------------------------------------------------------------


class ImageGridModel(QAbstractListModel):
    """Holds hit dicts and the pixmaps already decoded for them."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._hits: list[dict] = []
        self._keys: list[str] = []
        self._thumbs: dict[str, QPixmap] = {}
        self._key_rows: dict[str, list[int]] = {}

    # -- Qt API ---------------------------------------------------------
    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._hits)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self._hits)):
            return None
        hit = self._hits[index.row()]
        if role == HIT_ROLE:
            return hit
        if role == THUMB_ROLE:
            return self._thumbs.get(self._keys[index.row()])
        if role == Qt.ItemDataRole.DisplayRole:
            return hit.get("caption") or hit.get("rel_path") or hit.get("image_path") or ""
        if role == Qt.ItemDataRole.ToolTipRole:
            return hit.get("caption") or hit.get("image_path") or ""
        return None

    # -- model API ------------------------------------------------------
    def set_hits(self, hits, keys) -> None:
        self.beginResetModel()
        self._hits = list(hits or [])
        self._keys = list(keys or [])
        self._key_rows = {}
        for row, key in enumerate(self._keys):
            self._key_rows.setdefault(key, []).append(row)
        self.endResetModel()

    def hit_at(self, row: int) -> dict:
        if 0 <= row < len(self._hits):
            return self._hits[row]
        return {}

    def hits(self) -> list[dict]:
        return self._hits

    def key_for_row(self, row: int) -> str:
        if 0 <= row < len(self._keys):
            return self._keys[row]
        return ""

    def set_thumb(self, key: str, pixmap: QPixmap) -> None:
        self._thumbs[key] = pixmap
        rows = self._key_rows.get(key) or []
        if not rows:
            return
        first, last = rows[0], rows[-1]
        self.dataChanged.emit(self.index(first), self.index(last), [THUMB_ROLE])


class ImageGridDelegate(QStyledItemDelegate):
    """Contact-sheet cell: cover-cropped thumbnail, caption, meta line.

    Colors come from the theme token map rather than ``option.palette``: the app
    paints with a QSS theme, and the raw palette stays light even in dark mode.
    """

    def __init__(self, page, parent=None):
        super().__init__(parent)
        # Weak: page -> grid -> delegate -> page would be a Qt/Python cycle that
        # crashes on collection (Qt deletes the child, then Python deletes again).
        self._page_ref = weakref.ref(page)
        self._colors: dict = {}
        self._colors_dark: bool | None = None

    def _page(self):
        return self._page_ref()

    def _theme(self) -> dict:
        from PySide6.QtWidgets import QApplication

        from ui.widgets.styles import theme_color_map

        app = QApplication.instance()
        dark = bool(app.property("videoseek_is_dark")) if app is not None else True
        if self._colors_dark is not dark or not self._colors:
            self._colors = theme_color_map(dark)
            self._colors_dark = dark
        return self._colors

    def sizeHint(self, option, index) -> QSize:  # noqa: N802 - Qt API
        return QSize(CELL_WIDTH, CELL_HEIGHT)

    def paint(self, painter: QPainter, option, index):  # noqa: N802 - Qt API
        hit = index.data(HIT_ROLE) or {}
        rect = option.rect.adjusted(3, 3, -3, -3)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        colors = self._theme()

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        radius = 10.0
        card = QPainterPath()
        card.addRoundedRect(rect.toRectF(), radius, radius)
        base = QColor(str(colors.get("PANEL") or "#2e2e2e"))
        if hovered:
            base = base.lighter(115) if base.lightness() < 128 else base.darker(104)
        painter.fillPath(card, base)

        caption_height = 42
        thumb_rect = QRect(
            rect.left(), rect.top(), rect.width(), max(24, rect.height() - caption_height)
        )
        thumb_clip = QPainterPath()
        thumb_clip.addRoundedRect(thumb_rect.toRectF(), radius, radius)
        # Square off the bottom of the thumbnail so it meets the caption cleanly.
        thumb_clip.addRect(
            QRect(
                thumb_rect.left(),
                thumb_rect.bottom() - int(radius),
                thumb_rect.width(),
                int(radius),
            )
        )
        painter.save()
        painter.setClipPath(thumb_clip, Qt.ClipOperation.IntersectClip)

        pixmap = index.data(THUMB_ROLE)
        if isinstance(pixmap, QPixmap) and not pixmap.isNull():
            _draw_cover(painter, thumb_rect, pixmap)
        else:
            painter.fillRect(thumb_rect, QColor(str(colors.get("FIELD") or "#242424")))
            painter.setPen(QColor(str(colors.get("MUTED") or "#a3a3a3")))
            font = painter.font()
            font.setPointSizeF(max(8.0, font.pointSizeF() - 1))
            painter.setFont(font)
            painter.drawText(thumb_rect, Qt.AlignmentFlag.AlignCenter, "…")
            page = self._page()
            if page is not None:
                page.request_thumb(hit)

        painter.restore()

        if selected:
            pen = painter.pen()
            pen.setColor(QColor(str(colors.get("ACCENT") or "#60a5fa")))
            pen.setWidth(2)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(card)

        text_color = QColor(str(colors.get("TEXT") or "#f5f5f5"))
        muted = QColor(str(colors.get("MUTED") or "#a3a3a3"))

        caption = str(hit.get("caption") or hit.get("rel_path") or "").strip()
        if not caption:
            caption = os.path.basename(str(hit.get("image_path") or ""))
        metrics = painter.fontMetrics()
        caption_rect = QRect(rect.left() + 9, thumb_rect.bottom() + 5, rect.width() - 18, 17)
        painter.setPen(text_color)
        painter.drawText(
            caption_rect,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            metrics.elidedText(caption, Qt.TextElideMode.ElideRight, caption_rect.width()),
        )

        meta_bits = []
        width = int(hit.get("width") or 0)
        height = int(hit.get("height") or 0)
        if width and height:
            meta_bits.append(f"{width}×{height}")
        people_count = int(hit.get("people_count") or 0)
        if people_count:
            meta_bits.append(f"{people_count} 人")
        score = float(hit.get("score") or 0.0)
        if str(hit.get("match") or "") != "browse" and score > 0:
            meta_bits.append(f"{score:.2f}")
        if meta_bits:
            font = painter.font()
            font.setPointSizeF(max(7.5, font.pointSizeF() - 1.5))
            painter.setFont(font)
            painter.setPen(muted)
            meta_rect = QRect(rect.left() + 9, caption_rect.bottom(), rect.width() - 18, 15)
            painter.drawText(
                meta_rect,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                metrics.elidedText(
                    " · ".join(meta_bits), Qt.TextElideMode.ElideRight, meta_rect.width()
                ),
            )

        painter.restore()


class _DetailPreview(QLabel):
    """Preview label that never lets its pixmap drive the layout.

    A plain ``QLabel`` with a pixmap reports that pixmap's size as its minimum
    size hint, which pushes the caption out of the panel. Here the source pixmap
    is kept aside and re-scaled to whatever the layout grants.
    """

    def __init__(self, placeholder: str = "", parent=None):
        super().__init__(placeholder, parent)
        self.setObjectName("ThumbPreview")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setWordWrap(True)
        self.setMinimumSize(1, 1)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self._source = QPixmap()
        self._placeholder = placeholder

    def set_placeholder(self, text: str) -> None:
        self._placeholder = str(text or "")
        if self._source.isNull():
            self.setText(self._placeholder)

    def set_source(self, pixmap: QPixmap) -> None:
        self._source = QPixmap(pixmap) if isinstance(pixmap, QPixmap) else QPixmap()
        if self._source.isNull():
            self.clear_source()
            return
        self.setText("")
        self._rescale()

    def clear_source(self) -> None:
        self._source = QPixmap()
        self.setPixmap(QPixmap())
        self.setText(self._placeholder)

    def resizeEvent(self, event):  # noqa: N802 - Qt API
        super().resizeEvent(event)
        self._rescale()

    def _rescale(self) -> None:
        if self._source.isNull():
            return
        target = self.size()
        if target.width() <= 2 or target.height() <= 2:
            return
        self.setPixmap(
            self._source.scaled(
                target,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )


def _draw_cover(painter: QPainter, target: QRect, pixmap: QPixmap) -> None:
    """Paint ``pixmap`` filling ``target`` (center crop, no distortion)."""
    if pixmap.isNull() or target.width() <= 0 or target.height() <= 0:
        return
    source_w, source_h = pixmap.width(), pixmap.height()
    if source_w <= 0 or source_h <= 0:
        return
    scale = max(target.width() / float(source_w), target.height() / float(source_h))
    crop_w = min(source_w, max(1, int(target.width() / scale)))
    crop_h = min(source_h, max(1, int(target.height() / scale)))
    source_x = max(0, (source_w - crop_w) // 2)
    source_y = max(0, (source_h - crop_h) // 2)
    painter.drawPixmap(
        target,
        pixmap,
        QRect(source_x, source_y, crop_w, crop_h),
    )


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------


class ImageLibraryPage(PageScaffold):
    """Photo library workspace: folders on top, search + grid + detail below."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ImageLibraryPage")
        self._language = "zh"
        self._t = _STRINGS["zh"]

        self._thumb_loader = None
        self._model = ImageGridModel(self)
        self._scan_worker = None
        self._prune_worker = None
        self._index_worker = None
        self._search_worker = None
        self._status_worker = None
        self._scan_files: list[str] = []
        self._last_query = ""
        self._index_after_scan = False
        self._index_started_at = 0.0
        self._current_hit: dict = {}
        self._note = ""
        self._detail_key = ""
        self._roots: list[str] = []

        self.setAcceptDrops(True)
        self.header.title.setText(self._t["title"])
        self.header.subtitle.setText(self._t["subtitle"])
        self.header.subtitle.show()

        self._build_library_card()
        self._build_search_card()
        self._decorate()

    # -- construction ---------------------------------------------------
    def _build_library_card(self) -> None:
        card = VSCard("panel", margins=(16, 14, 16, 14), spacing=10)
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        column = card.content_layout

        title = QLabel(self._t["library_card"])
        title.setObjectName("SectionTitle")
        column.addWidget(title)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.btn_add_root = QPushButton(self._t["add_root"])
        set_button_role(self.btn_add_root, "PrimaryButton")
        self.btn_remove_root = QPushButton(self._t["remove_root"])
        set_button_role(self.btn_remove_root, "GhostButton")
        self.btn_open_root = QPushButton(self._t["open_root"])
        set_button_role(self.btn_open_root, "GhostButton")
        for button in (self.btn_add_root, self.btn_remove_root, self.btn_open_root):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            actions.addWidget(button)
        actions.addStretch(1)
        column.addLayout(actions)

        self.roots_list = QListWidget()
        self.roots_list.setObjectName("ImageRootsList")
        self.roots_list.setFixedHeight(74)
        self.roots_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        column.addWidget(self.roots_list)

        self.roots_hint = QLabel(self._t["roots_empty"])
        self.roots_hint.setObjectName("Hint")
        self.roots_hint.setWordWrap(True)
        column.addWidget(self.roots_hint)

        index_row = QHBoxLayout()
        index_row.setSpacing(8)
        self.btn_rescan = QPushButton(self._t["rescan"])
        set_button_role(self.btn_rescan, "GhostButton")
        self.btn_prune = QPushButton(self._t["prune"])
        set_button_role(self.btn_prune, "GhostButton")
        self.btn_prune.setToolTip("文件被移动或删除后，索引里的旧记录会留下空白缩略图")
        self.btn_index = QPushButton(self._t["start_index"])
        set_button_role(self.btn_index, "AccentGhostButton")
        self.btn_stop = QPushButton(self._t["stop"])
        set_button_role(self.btn_stop, "DangerGhostButton")
        self.btn_stop.setEnabled(False)
        for button in (self.btn_rescan, self.btn_prune, self.btn_index, self.btn_stop):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            index_row.addWidget(button)
        self.chk_describe = QCheckBox(self._t["describe_toggle"])
        self.chk_describe.setToolTip("Settings → Understanding → model service → VLM")
        self.chk_force = QCheckBox(self._t["force_toggle"])
        index_row.addWidget(self.chk_describe)
        index_row.addWidget(self.chk_force)
        index_row.addStretch(1)
        column.addLayout(index_row)

        self.progress_row = VSProgressStatusRow(card)
        # The shared row styles its label as a short accent chip; indexing text
        # ("1234/50000 · name · ~12m left") needs the muted long-text style.
        self.progress_row.status_label.setObjectName("Hint")
        self.progress_row.status_label.style().unpolish(self.progress_row.status_label)
        self.progress_row.status_label.style().polish(self.progress_row.status_label)
        column.addWidget(self.progress_row)

        self.lbl_status = QLabel(self._t["status_idle"])
        self.lbl_status.setObjectName("Hint")
        self.lbl_status.setWordWrap(True)
        column.addWidget(self.lbl_status)

        self.content_layout.addWidget(card)

    def _build_search_card(self) -> None:
        card = VSCard("panel", margins=(16, 14, 16, 14), spacing=10)
        column = card.content_layout

        title = QLabel(self._t["search_card"])
        title.setObjectName("SectionTitle")
        column.addWidget(title)

        controls = QHBoxLayout()
        controls.setSpacing(8)
        self.mode_combo = QComboBox()
        for key in ("mode_text", "mode_keyword", "mode_image"):
            self.mode_combo.addItem(self._t[key], key)
        self.mode_combo.setMinimumWidth(118)
        self.search_input = QLineEdit()
        self.search_input.setObjectName("SearchInput")
        self.search_input.setPlaceholderText(self._t["search_placeholder"])
        self.search_input.setClearButtonEnabled(True)
        self.btn_search = QPushButton(self._t["search"])
        set_button_role(self.btn_search, "PrimaryButton")
        self.btn_browse = QPushButton(self._t["browse_all"])
        set_button_role(self.btn_browse, "GhostButton")
        for button in (self.btn_search, self.btn_browse):
            button.setCursor(Qt.CursorShape.PointingHandCursor)

        people_label = QLabel(self._t["min_people"])
        people_label.setObjectName("SectionLabel")
        self.spin_people = QSpinBox()
        self.spin_people.setRange(0, 99)
        self.spin_people.setSpecialValueText("—")
        self.spin_people.setFixedWidth(58)
        self.spin_people.setToolTip("最少人数（0 = 不限）")

        results_label = QLabel(self._t["top_k"])
        results_label.setObjectName("SectionLabel")
        self.spin_topk = QSpinBox()
        self.spin_topk.setRange(12, 2000)
        self.spin_topk.setValue(60)
        self.spin_topk.setSingleStep(12)
        self.spin_topk.setFixedWidth(72)

        controls.addWidget(self.mode_combo)
        controls.addWidget(self.search_input, 1)
        controls.addWidget(people_label)
        controls.addWidget(self.spin_people)
        controls.addWidget(results_label)
        controls.addWidget(self.spin_topk)
        controls.addWidget(self.btn_search)
        controls.addWidget(self.btn_browse)
        column.addLayout(controls)

        self.lbl_model_hint = QLabel("")
        self.lbl_model_hint.setObjectName("Hint")
        self.lbl_model_hint.setWordWrap(True)
        self.lbl_model_hint.hide()
        column.addWidget(self.lbl_model_hint)

        self.lbl_search_status = QLabel(self._t["drop_hint"])
        self.lbl_search_status.setObjectName("Hint")
        self.lbl_search_status.setWordWrap(True)
        column.addWidget(self.lbl_search_status)

        self.grid = QListView()
        self.grid.setObjectName("ImageGrid")
        self.grid.setViewMode(QListView.ViewMode.IconMode)
        self.grid.setResizeMode(QListView.ResizeMode.Adjust)
        self.grid.setMovement(QListView.Movement.Static)
        self.grid.setUniformItemSizes(True)
        self.grid.setSpacing(4)
        self.grid.setWordWrap(False)
        self.grid.setModel(self._model)
        self.grid.setItemDelegate(ImageGridDelegate(self, self.grid))
        self.grid.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.grid.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.grid.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.grid.setMouseTracking(True)
        self.grid.setMinimumHeight(320)
        self.grid.setStyleSheet(
            "QListView#ImageGrid{background:transparent;border:none;outline:none;}"
        )

        detail = self._build_detail_panel()

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setObjectName("ImageLibrarySplitter")
        splitter.addWidget(self.grid)
        splitter.addWidget(detail)
        splitter.setStretchFactor(0, 5)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([760, 320])
        splitter.setChildrenCollapsible(False)
        column.addWidget(splitter, 1)

        self.content_layout.addWidget(card, 1)

    def _build_detail_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("SubPanelCard")
        panel.setMinimumWidth(280)
        column = QVBoxLayout(panel)
        column.setContentsMargins(14, 14, 14, 14)
        column.setSpacing(8)

        self.detail_image = _DetailPreview(self._t["detail_empty"])
        self.detail_image.setMinimumHeight(180)
        column.addWidget(self.detail_image, 1)

        self.detail_caption = QLabel("")
        self.detail_caption.setWordWrap(True)
        self.detail_caption.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        column.addWidget(self.detail_caption)

        self.detail_people = QLabel("")
        self.detail_people.setObjectName("Hint")
        self.detail_people.setWordWrap(True)
        column.addWidget(self.detail_people)

        self.detail_tags = QLabel("")
        self.detail_tags.setObjectName("Hint")
        self.detail_tags.setWordWrap(True)
        column.addWidget(self.detail_tags)

        self.detail_meta = QLabel("")
        self.detail_meta.setObjectName("Hint")
        self.detail_meta.setWordWrap(True)
        column.addWidget(self.detail_meta)

        self.detail_path = QLabel("")
        self.detail_path.setObjectName("Hint")
        self.detail_path.setWordWrap(True)
        self.detail_path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        column.addWidget(self.detail_path)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.btn_open = QPushButton(self._t["detail_open"])
        set_button_role(self.btn_open, "PrimaryButton")
        self.btn_reveal = QPushButton(self._t["detail_reveal"])
        set_button_role(self.btn_reveal, "GhostButton")
        for button in (self.btn_open, self.btn_reveal):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setEnabled(False)
            row.addWidget(button)
        row.addStretch(1)
        column.addLayout(row)
        return panel

    def _decorate(self) -> None:
        self.btn_add_root.clicked.connect(self._on_add_root)
        self.btn_remove_root.clicked.connect(self._on_remove_root)
        self.btn_open_root.clicked.connect(self._on_open_root)
        self.btn_rescan.clicked.connect(self._on_rescan)
        self.btn_prune.clicked.connect(self._on_prune_clicked)
        self.btn_index.clicked.connect(self._on_index_clicked)
        self.btn_stop.clicked.connect(self._on_stop_clicked)
        self.btn_search.clicked.connect(self._on_search_clicked)
        self.btn_browse.clicked.connect(self._on_browse_clicked)
        self.search_input.returnPressed.connect(self._on_search_clicked)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        self.grid.selectionModel().currentChanged.connect(self._on_current_changed)
        self.grid.doubleClicked.connect(self._on_grid_activated)
        self.btn_open.clicked.connect(self._on_open_image)
        self.btn_reveal.clicked.connect(self._on_reveal_image)
        self.roots_list.currentRowChanged.connect(self._on_root_selected)
        self._on_mode_changed()

    # -- thumbnails -----------------------------------------------------
    def _loader(self):
        if self._thumb_loader is None:
            from ui.image_thumbs import ImageThumbLoader

            self._thumb_loader = ImageThumbLoader(self)
            self._thumb_loader.loaded.connect(self._on_thumb_loaded)
        return self._thumb_loader

    def request_thumb(self, hit: dict) -> None:
        image_id = str(hit.get("image_id") or "")
        path = str(hit.get("image_path") or "")
        if not image_id or not path:
            return
        from ui.image_thumbs import THUMB_SIZE_GRID

        self._loader().request(image_id, path, THUMB_SIZE_GRID)

    def _on_thumb_loaded(self, key: str, image) -> None:
        if image is None:
            return
        pixmap = QPixmap.fromImage(image)
        if pixmap.isNull():
            return
        self._model.set_thumb(key, pixmap)
        if self._detail_key and key == self._detail_key:
            self._paint_detail_image(pixmap)

    # -- language -------------------------------------------------------
    def apply_texts(self, language: str = "") -> None:
        lang = str(language or self._language or "zh")
        self._language = "en" if lang.startswith("en") else "zh"
        self._t = _STRINGS[self._language]
        t = self._t

        self.header.title.setText(t["title"])
        self.header.subtitle.setText(t["subtitle"])
        self.btn_add_root.setText(t["add_root"])
        self.btn_remove_root.setText(t["remove_root"])
        self.btn_open_root.setText(t["open_root"])
        self.btn_rescan.setText(t["rescan"])
        self.btn_prune.setText(t["prune"])
        self.btn_index.setText(t["start_index"])
        self.btn_stop.setText(t["stop"])
        self.chk_describe.setText(t["describe_toggle"])
        self.chk_force.setText(t["force_toggle"])
        self.btn_search.setText(t["search"])
        self.btn_browse.setText(t["browse_all"])
        self.btn_open.setText(t["detail_open"])
        self.btn_reveal.setText(t["detail_reveal"])
        self.search_input.setPlaceholderText(t["search_placeholder"])
        for index, key in enumerate(("mode_text", "mode_keyword", "mode_image")):
            self.mode_combo.setItemText(index, t[key])
        if not self._roots:
            self.roots_hint.setText(t["roots_empty"])
        self.detail_image.set_placeholder(t["detail_empty"])
        if not self._model.rowCount():
            self.detail_image.clear_source()
            self.lbl_search_status.setText(t["drop_hint"])
        self._render_status()
        self._refresh_model_hint()

    # -- folders --------------------------------------------------------
    def _load_roots(self) -> None:
        from src.services import image_index_service as service

        try:
            manifest = service.load_manifest()
        except Exception:
            manifest = {"roots": []}
        self._roots = [
            str(entry.get("root") or "")
            for entry in (manifest.get("roots") or [])
            if isinstance(entry, dict) and entry.get("root")
        ]
        self.roots_list.blockSignals(True)
        self.roots_list.clear()
        for root in self._roots:
            item = QListWidgetItem(root)
            item.setToolTip(root)
            self.roots_list.addItem(item)
        self.roots_list.blockSignals(False)
        self.roots_hint.setVisible(not self._roots)
        self.roots_list.setVisible(bool(self._roots))
        if self._roots:
            self.roots_list.setCurrentRow(0)

    def _on_add_root(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, self._t["add_root"], os.path.expanduser("~"))
        if folder:
            self._add_root(folder)

    def _add_root(self, folder: str) -> None:
        from src.services import image_index_service as service

        try:
            service.add_library_root(folder)
        except Exception as exc:
            QMessageBox.warning(self, self._t["title"], f"{folder}\n\n{exc}")
            return
        self._load_roots()
        self.refresh_status()
        self._start_scan(folder)

    def _on_remove_root(self) -> None:
        item = self.roots_list.currentItem()
        if item is None:
            return
        from src.services import image_index_service as service

        service.remove_library_root(item.text())
        self._load_roots()
        self.refresh_status()
        self.lbl_status.setText("已取消登记（索引行保留，可用 prune 清理）")

    def _on_open_root(self) -> None:
        item = self.roots_list.currentItem()
        if item is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(item.text()))

    def _on_root_selected(self, row: int) -> None:
        if 0 <= row < len(self._roots):
            self.btn_open_root.setEnabled(True)
            self.btn_remove_root.setEnabled(True)
        else:
            self.btn_open_root.setEnabled(False)
            self.btn_remove_root.setEnabled(False)

    # -- status ---------------------------------------------------------
    def _refresh_model_hint(self) -> None:
        """Warn when the active embedding model cannot match Chinese text.

        OpenCLIP is English-only, so a Chinese description mostly misses. That is
        a settings problem, not a bug in this page, so say so once instead of
        letting the user conclude the index is empty.
        """
        try:
            from src.services.model_profile_display import (
                format_active_model_display_name,
                resolve_provider_text_language,
            )
            from src.storage.config_store import get_active_embedding_spec

            spec = get_active_embedding_spec()
            provider = str(spec.get("provider") or "").strip().lower()
            text_language = resolve_provider_text_language(provider)
            model_name = format_active_model_display_name()
        except Exception:
            self.lbl_model_hint.hide()
            return
        if text_language == "en":
            self.lbl_model_hint.setText(self._t["model_hint_en"].format(model=model_name))
            self.lbl_model_hint.show()
        else:
            self.lbl_model_hint.hide()

    def refresh_status(self) -> None:
        self._refresh_model_hint()
        if self._status_worker is not None and self._status_worker.isRunning():
            return
        worker = _StatusWorker(self)
        worker.ready.connect(self._on_status_ready)
        worker.failed.connect(lambda message: self.lbl_status.setText(message))
        self._status_worker = worker
        worker.start()

    def _on_status_ready(self, status: dict) -> None:
        self._status = dict(status or {})
        self._render_status()

    def _render_status(self) -> None:
        status = getattr(self, "_status", None)
        if not status:
            self.lbl_status.setText(self._t["status_idle"])
            return
        if self._index_worker is not None and self._index_worker.isRunning():
            return
        parts = [
            self._t["status_summary"].format(
                indexed=int(status.get("indexed") or 0),
                described=int(status.get("described") or 0),
                dimension=int(status.get("dimension") or 0) or "—",
            ),
            self._t["vlm_ready"] if status.get("vlm_ready") else self._t["vlm_down"],
        ]
        text = " · ".join(part for part in parts if part)
        note = str(getattr(self, "_note", "") or "").strip()
        if note:
            text = f"{text}\n{note}"
        if status.get("error"):
            text = f"{text}\n{status['error']}"
        self.lbl_status.setText(text)
        self.chk_describe.setEnabled(bool(status.get("vlm_ready")))
        if not status.get("vlm_ready"):
            self.chk_describe.setChecked(False)
        self.chk_describe.setToolTip(str(status.get("vlm_reason") or ""))

    # -- scanning / indexing -------------------------------------------
    def _start_scan(self, root: str = "") -> None:
        if self._scan_worker is not None and self._scan_worker.isRunning():
            return
        self._note = ""
        self.progress_row.set_progress_visible(True)
        self.progress_row.set_progress_value(0)
        self.progress_row.set_status_text(self._t["scan_running"])
        worker = _ScanWorker(root, self)
        worker.scanned.connect(self._on_scanned)
        worker.failed.connect(self._on_scan_failed)
        self._scan_worker = worker
        worker.start()

    def _on_rescan(self) -> None:
        self._scan_files = []
        self._start_scan("")

    def _on_prune_clicked(self) -> None:
        if self._prune_worker is not None and self._prune_worker.isRunning():
            return
        self.btn_prune.setEnabled(False)
        self.lbl_status.setText(self._t["prune_running"])
        worker = _PruneWorker(self)
        worker.pruned.connect(self._on_pruned)
        worker.failed.connect(lambda message: self.lbl_status.setText(message))
        self._prune_worker = worker
        worker.start()

    def _on_pruned(self, result: dict) -> None:
        self.btn_prune.setEnabled(True)
        removed = int((result or {}).get("removed") or 0)
        self._note = (
            self._t["prune_done"].format(removed=removed) if removed else self._t["prune_none"]
        )
        self.refresh_status()
        self._run_search("browse", "")

    def _on_scanned(self, files: list) -> None:
        self._scan_files = list(files or [])
        indexed = int((getattr(self, "_status", {}) or {}).get("indexed") or 0)
        self.progress_row.set_progress_visible(False)
        if not self._scan_files:
            self.progress_row.set_status_text("")
            self.lbl_status.setText(self._t["no_files"])
            self._index_after_scan = False
            return
        self.lbl_status.setText(
            self._t["scanned"].format(count=len(self._scan_files), indexed=indexed)
        )
        if self._index_after_scan:
            self._index_after_scan = False
            self._start_index()

    def _on_scan_failed(self, message: str) -> None:
        self._index_after_scan = False
        self.progress_row.set_progress_visible(False)
        self.lbl_status.setText(message)

    def _on_index_clicked(self) -> None:
        if self._index_worker is not None and self._index_worker.isRunning():
            return
        if not self._scan_files:
            self._index_after_scan = True
            self._start_scan("")
            return
        self._start_index()

    def _start_index(self) -> None:
        self._note = ""
        paths = list(self._scan_files)
        if not paths:
            self.lbl_status.setText(self._t["no_files"])
            return
        if len(paths) > 1500:
            answer = QMessageBox.question(
                self,
                self._t["confirm_big_title"],
                self._t["confirm_big_body"].format(count=len(paths)),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        self._index_started_at = time.time()
        self.progress_row.set_progress_visible(True)
        self.progress_row.set_progress_value(0)
        self.btn_index.setEnabled(False)
        self.btn_rescan.setEnabled(False)
        self.btn_stop.setEnabled(True)

        worker = _IndexWorker(
            paths,
            describe=self.chk_describe.isChecked(),
            force=self.chk_force.isChecked(),
            roots=self._roots,
            parent=self,
        )
        worker.progressed.connect(self._on_index_progress)
        worker.indexed.connect(self._on_index_finished)
        worker.failed.connect(self._on_index_failed)
        self._index_worker = worker
        worker.start()

    def _on_index_progress(self, done: int, total: int, path: str) -> None:
        percent = int(round(100.0 * done / max(1, total)))
        self.progress_row.set_progress_value(percent)
        elapsed = max(0.0, time.time() - self._index_started_at)
        name = os.path.basename(path or "")
        if done > 3 and total > done:
            per_item = elapsed / max(1, done)
            eta = _human_duration(per_item * (total - done))
            text = self._t["index_running_eta"].format(
                done=done, total=total, name=name, eta=eta
            )
        else:
            text = self._t["index_running"].format(
                done=done, total=total, name=name, elapsed=_human_duration(elapsed)
            )
        self.progress_row.set_status_text(text)

    def _on_index_finished(self, result: dict) -> None:
        self.btn_index.setEnabled(True)
        self.btn_rescan.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.progress_row.set_progress_visible(False)
        self.progress_row.set_status_text("")
        payload = dict(result or {})
        if payload.get("describe_reason"):
            self.lbl_status.setText(
                self._t["index_done"].format(
                    added=payload.get("added", 0),
                    updated=payload.get("updated", 0),
                    skipped=payload.get("skipped", 0),
                    failed=payload.get("failed", 0),
                    described=payload.get("described", 0),
                )
                + f"\n{payload['describe_reason']}"
            )
        else:
            self.lbl_status.setText(
                self._t["index_done"].format(
                    added=payload.get("added", 0),
                    updated=payload.get("updated", 0),
                    skipped=payload.get("skipped", 0),
                    failed=payload.get("failed", 0),
                    described=payload.get("described", 0),
                )
            )
        self.refresh_status()
        self._run_search("browse", "")

    def _on_index_failed(self, message: str) -> None:
        self.btn_index.setEnabled(True)
        self.btn_rescan.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.progress_row.set_progress_visible(False)
        self.lbl_status.setText(self._t["index_failed"].format(error=message))

    def _on_stop_clicked(self) -> None:
        if self._index_worker is not None:
            self._index_worker.stop()
            self.lbl_status.setText(self._t["index_stopped"].format(scanned="…"))

    # -- search ---------------------------------------------------------
    def _on_mode_changed(self) -> None:
        mode = self.mode_combo.currentData() or "mode_text"
        is_image = mode == "mode_image"
        self.search_input.setEnabled(not is_image)
        if is_image:
            self.search_input.setPlaceholderText(self._t["pick_image"])
        else:
            self.search_input.setPlaceholderText(self._t["search_placeholder"])
        self.spin_people.setEnabled(mode in {"mode_keyword", "mode_text"})

    def _on_search_clicked(self) -> None:
        mode = self.mode_combo.currentData() or "mode_text"
        if mode == "mode_image":
            path, _ = QFileDialog.getOpenFileName(
                self,
                self._t["mode_image"],
                os.path.expanduser("~"),
                "Images (*.jpg *.jpeg *.png *.webp *.bmp *.tif *.tiff *.heic *.avif)",
            )
            if not path:
                return
            self.search_input.setText(os.path.basename(path))
            self._run_search("image", path)
            return
        query = self.search_input.text().strip()
        if not query:
            self._run_search("browse", "")
            return
        self._run_search("keyword" if mode == "mode_keyword" else "text", query)

    def _on_browse_clicked(self) -> None:
        self._run_search("browse", "")

    def _run_search(self, mode: str, query: str) -> None:
        if self._search_worker is not None and self._search_worker.isRunning():
            return
        self._last_query = str(query or "")
        self.lbl_search_status.setText(self._t["searching"])
        self.btn_search.setEnabled(False)
        worker = _SearchWorker(
            mode,
            query,
            top_k=int(self.spin_topk.value()),
            min_people=int(self.spin_people.value()),
            parent=self,
        )
        worker.results.connect(lambda hits, m=mode: self._on_results(hits, m))
        worker.failed.connect(self._on_search_failed)
        self._search_worker = worker
        worker.start()

    def _on_results(self, hits: list, mode: str) -> None:
        self.btn_search.setEnabled(True)
        min_people = int(self.spin_people.value())
        if min_people:
            hits = [hit for hit in hits if int(hit.get("people_count") or 0) >= min_people]
        keys = []
        from ui.image_thumbs import THUMB_SIZE_GRID, ImageThumbLoader

        for hit in hits:
            keys.append(
                ImageThumbLoader.make_key(str(hit.get("image_id") or ""), THUMB_SIZE_GRID)
            )
        self._model.set_hits(hits, keys)
        count = len(hits)
        if count:
            self.grid.setCurrentIndex(self._model.index(0))
            note = ""
            if mode == "text" and self._is_vector_only_guess(hits):
                note = " · " + self._t["results_vector_only"]
            self.lbl_search_status.setText(
                self._t["results_n"].format(count=count, elapsed="").rstrip(" ·") + note
            )
        else:
            empty_key = "browse_empty" if mode == "browse" else "results_empty"
            self.lbl_search_status.setText(self._t[empty_key])
            self._clear_detail()

    def _is_vector_only_guess(self, hits: list) -> bool:
        """True when a Chinese query matched nothing literally and fell back to vectors.

        Those results look random to a user who expects text matching, so the
        result count gets a caveat instead of pretending the search worked.
        """
        if not hits or any(str(hit.get("match")) != "vector" for hit in hits):
            return False
        from src.services.image_index_service import has_cjk

        return has_cjk(self._last_query)

    def _on_search_failed(self, message: str) -> None:
        self.btn_search.setEnabled(True)
        self.lbl_search_status.setText(self._t["search_failed"].format(error=message))

    # -- detail ---------------------------------------------------------
    def _on_current_changed(self, current: QModelIndex, _previous: QModelIndex) -> None:
        if not current.isValid():
            self._clear_detail()
            return
        hit = current.data(HIT_ROLE) or {}
        self._show_hit(hit)

    def _show_hit(self, hit: dict) -> None:
        self._current_hit = dict(hit or {})
        path = str(hit.get("image_path") or "")
        self.btn_open.setEnabled(bool(path))
        self.btn_reveal.setEnabled(bool(path))

        caption = str(hit.get("caption") or "").strip()
        self.detail_caption.setText(caption or self._t["detail_no_caption"])

        people = [person for person in (hit.get("people") or []) if isinstance(person, dict)]
        if people:
            lines = [f"{self._t['detail_people']}："]
            lines.extend(f"· {_people_text(person)}" for person in people if _people_text(person))
            self.detail_people.setText("\n".join(lines))
        else:
            self.detail_people.setText("")

        tags = [str(tag) for tag in (hit.get("tags") or []) if str(tag).strip()]
        self.detail_tags.setText(
            f"{self._t['detail_tags']}：{' · '.join(tags)}" if tags else ""
        )

        mtime = float(hit.get("mtime") or 0.0)
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime)) if mtime else "—"
        self.detail_meta.setText(
            self._t["detail_meta"].format(
                width=int(hit.get("width") or 0) or "—",
                height=int(hit.get("height") or 0) or "—",
                size=_human_bytes(int(hit.get("bytes") or 0)),
                mtime=when,
            )
        )
        self.detail_path.setText(hit.get("rel_path") or path)

        image_id = str(hit.get("image_id") or "")
        from ui.image_thumbs import THUMB_SIZE_DETAIL, ImageThumbLoader

        self._detail_key = ImageThumbLoader.make_key(image_id, THUMB_SIZE_DETAIL)
        cached = self._loader().peek(self._detail_key)
        self.detail_image.clear_source()
        if cached is not None:
            self._paint_detail_image(QPixmap.fromImage(cached))
        elif image_id and path:
            self._loader().request(image_id, path, THUMB_SIZE_DETAIL)

    def _paint_detail_image(self, pixmap: QPixmap) -> None:
        self.detail_image.set_source(pixmap)

    def _clear_detail(self) -> None:
        self._current_hit = {}
        self._detail_key = ""
        self.detail_image.clear_source()
        self.detail_caption.setText("")
        self.detail_people.setText("")
        self.detail_tags.setText("")
        self.detail_meta.setText("")
        self.detail_path.setText("")
        self.btn_open.setEnabled(False)
        self.btn_reveal.setEnabled(False)

    def _on_grid_activated(self, index: QModelIndex) -> None:
        hit = index.data(HIT_ROLE) or {}
        self._open_path(str(hit.get("image_path") or ""))

    def _on_open_image(self) -> None:
        self._open_path(str(self._current_hit.get("image_path") or ""))

    def _on_reveal_image(self) -> None:
        _reveal_in_file_manager(str(self._current_hit.get("image_path") or ""))

    @staticmethod
    def _open_path(path: str) -> None:
        if path and os.path.isfile(path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    # -- drag & drop ----------------------------------------------------
    def dragEnterEvent(self, event):  # noqa: N802 - Qt API
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):  # noqa: N802 - Qt API
        from src.media.formats import is_supported_image_path

        folders: list[str] = []
        images: list[str] = []
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if not path:
                continue
            if os.path.isdir(path):
                folders.append(path)
            elif is_supported_image_path(path):
                images.append(path)
        if images:
            self._run_search("image", images[0])
            self.lbl_search_status.setText(os.path.basename(images[0]))
        for folder in folders:
            self._add_root(folder)
        if folders or images:
            event.acceptProposedAction()

    # -- lifecycle ------------------------------------------------------
    def showEvent(self, event):  # noqa: N802 - Qt API
        super().showEvent(event)
        if getattr(self, "_loaded_once", False):
            return
        self._loaded_once = True
        self._load_roots()
        self.refresh_status()
        QTimer.singleShot(150, lambda: self._run_search("browse", ""))
    def shutdown(self) -> None:
        """Stop background work before the page (or the app) goes away."""
        deadline = time.time() + 4.0
        for name in (
            "_index_worker",
            "_scan_worker",
            "_prune_worker",
            "_search_worker",
            "_status_worker",
        ):
            worker = getattr(self, name, None)
            if worker is None:
                continue
            if worker.isRunning():
                stop = getattr(worker, "stop", None)
                if callable(stop):
                    stop()
                remaining = max(0, int((deadline - time.time()) * 1000))
                worker.wait(remaining)
            setattr(self, name, None)
        if self._thumb_loader is not None:
            try:
                self._thumb_loader.loaded.disconnect(self._on_thumb_loaded)
            except (RuntimeError, TypeError):
                pass
            self._thumb_loader.shutdown()
            self._thumb_loader = None
