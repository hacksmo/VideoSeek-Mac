"""Image library: scan, index and search still images.

Scope
-----
Additive to the video pipeline. Images live in their own Lance table
(``image_assets``, see ``src/storage/image_lance_store.py``) and are keyed by a
content hash, so the same photo in two folders is indexed once.

Pipeline per image:

    load (JPG/PNG/WEBP/HEIC...) -> CLIP vector -> VLM description (optional)
        -> one row: vector + caption + people[] + tags[]

Search offers three entry points, mirroring the video search tabs:

    * text      -> CLIP text vector ANN (semantic)
    * keyword   -> substring over caption / people / tags (exact words)
    * image     -> CLIP image vector ANN (look-alike)

Nothing here mutates video state, so a broken image index cannot take the video
library down; deleting the ``image_assets`` table reverts the feature.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

import numpy as np

from src.app.logging_utils import get_logger
from src.media.formats import is_supported_image_path

logger = get_logger("image_index_service")

MANIFEST_VERSION = 1
LIBRARY_DIR_NAME = "image_library"
MANIFEST_NAME = "library.json"

# Image files are small; a full content hash is the cheapest way to get a stable
# id and free de-duplication. Chunked so a 200 MB TIFF does not sit in RAM.
_HASH_CHUNK = 1 << 20


@dataclass
class ImageIndexStats:
    scanned: int = 0
    added: int = 0
    updated: int = 0
    skipped: int = 0
    failed: int = 0
    described: int = 0
    describe_failed: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "scanned": self.scanned,
            "added": self.added,
            "updated": self.updated,
            "skipped": self.skipped,
            "failed": self.failed,
            "described": self.described,
            "describe_failed": self.describe_failed,
            "errors": self.errors[:20],
        }


# ---------------------------------------------------------------------------
# Paths / manifest
# ---------------------------------------------------------------------------


def get_image_library_dir() -> str:
    from src.infra.paths import get_app_data_dir

    return os.path.join(get_app_data_dir(), LIBRARY_DIR_NAME)


def get_manifest_path() -> str:
    return os.path.join(get_image_library_dir(), MANIFEST_NAME)


def load_manifest() -> dict:
    path = get_manifest_path()
    if not os.path.isfile(path):
        return {"version": MANIFEST_VERSION, "roots": [], "updated_at": 0.0}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read the image library manifest: %s", exc)
        return {"version": MANIFEST_VERSION, "roots": [], "updated_at": 0.0}
    if not isinstance(payload, dict):
        return {"version": MANIFEST_VERSION, "roots": [], "updated_at": 0.0}
    payload.setdefault("version", MANIFEST_VERSION)
    payload.setdefault("roots", [])
    payload.setdefault("updated_at", 0.0)
    return payload


def save_manifest(manifest: Mapping[str, Any]) -> None:
    path = get_manifest_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = dict(manifest)
    payload["version"] = MANIFEST_VERSION
    payload["updated_at"] = time.time()
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def add_library_root(root_path: str) -> dict:
    root = os.path.normpath(os.path.abspath(os.path.expanduser(str(root_path or "").strip())))
    if not os.path.isdir(root):
        raise ValueError(f"Not a folder: {root_path}")
    manifest = load_manifest()
    roots = [entry for entry in manifest.get("roots", []) if isinstance(entry, dict)]
    if not any(os.path.normpath(str(entry.get("root") or "")) == root for entry in roots):
        roots.append({"root": root, "added_at": time.time(), "last_scan": 0.0, "count": 0})
        manifest["roots"] = roots
        save_manifest(manifest)
    return manifest


def remove_library_root(root_path: str) -> dict:
    root = os.path.normpath(os.path.abspath(os.path.expanduser(str(root_path or "").strip())))
    manifest = load_manifest()
    manifest["roots"] = [
        entry
        for entry in manifest.get("roots", [])
        if isinstance(entry, dict) and os.path.normpath(str(entry.get("root") or "")) != root
    ]
    save_manifest(manifest)
    return manifest


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------


def scan_image_files(root_path: str, *, recursive: bool = True) -> list[str]:
    """Return supported image files under ``root_path`` (sorted, hidden skipped)."""
    root = os.path.normpath(os.path.abspath(str(root_path or "")))
    if not os.path.isdir(root):
        return []
    found: list[str] = []
    if recursive:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(
                name for name in dirnames if not name.startswith(".") and name.lower() != "__macosx"
            )
            for name in sorted(filenames):
                if name.startswith("."):
                    continue
                if is_supported_image_path(name):
                    found.append(os.path.join(dirpath, name))
    else:
        for name in sorted(os.listdir(root)):
            full = os.path.join(root, name)
            if name.startswith(".") or not os.path.isfile(full):
                continue
            if is_supported_image_path(name):
                found.append(full)
    return found


def scan_manifest_roots(*, recursive: bool = True) -> list[str]:
    """Union of every registered root, de-duplicated by real path."""
    manifest = load_manifest()
    seen: set[str] = set()
    files: list[str] = []
    for entry in manifest.get("roots", []):
        if not isinstance(entry, dict):
            continue
        root = str(entry.get("root") or "")
        if not root:
            continue
        for path in scan_image_files(root, recursive=recursive):
            real = os.path.realpath(path)
            if real in seen:
                continue
            seen.add(real)
            files.append(path)
    return files


def file_content_id(path: str) -> str:
    """Stable 32-hex id derived from file bytes (dedupes copies)."""
    digest = hashlib.sha1()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(_HASH_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()[:32]


def _rel_path(path: str, root: str) -> str:
    try:
        return os.path.relpath(path, root) if root else os.path.basename(path)
    except ValueError:
        return os.path.basename(path)


# ---------------------------------------------------------------------------
# Indexing
# ---------------------------------------------------------------------------


def _encode_image(image_bgr) -> list[float]:
    from src.core.clip_embedding import get_clip_embeddings_batch
    from src.core.vector_norm import normalize_vectors

    vectors = get_clip_embeddings_batch([image_bgr])
    if vectors is None or len(vectors) == 0:
        raise RuntimeError("CLIP encoder returned no vector")
    normalized = normalize_vectors(np.asarray(vectors, dtype=np.float32))
    return [float(value) for value in normalized[0]]


def _build_row(
    *,
    image_id: str,
    path: str,
    root: str,
    stat: os.stat_result,
    image_bgr,
    vector: Sequence[float],
    description: Mapping[str, Any] | None,
    describer: str,
) -> dict:
    people = list((description or {}).get("people") or [])
    tags = list((description or {}).get("tags") or [])
    return {
        "image_id": image_id,
        "library_root": root,
        "image_path": path,
        "rel_path": _rel_path(path, root),
        "mtime": float(stat.st_mtime),
        "width": int(image_bgr.shape[1]),
        "height": int(image_bgr.shape[0]),
        "bytes": int(stat.st_size),
        "caption": str((description or {}).get("caption") or ""),
        "people_json": json.dumps(people, ensure_ascii=False),
        "tags_json": json.dumps(tags, ensure_ascii=False),
        "describer": describer,
        "vector": [float(value) for value in vector],
    }


def index_images(
    paths: Sequence[str],
    *,
    config=None,
    describe: bool = True,
    force: bool = False,
    root: str = "",
    concurrency: int = 2,
    progress: Callable[[int, int, str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict:
    """Index (or re-index) the given image files. Returns ``ImageIndexStats``."""
    from src.core.image_io import load_image_bgr
    from src.storage import image_lance_store as store

    if config is None:
        from src.app.config import load_config

        config = load_config()

    stats = ImageIndexStats()
    total = len(paths)
    workers = max(1, min(4, int(concurrency or 2)))

    describe_enabled = bool(describe)
    describe_reason = ""
    if describe_enabled:
        from src.services.image_understanding import vlm_availability

        ready, reason = vlm_availability(config, probe=True)
        if not ready:
            describe_enabled = False
            describe_reason = reason
            logger.info("Image descriptions disabled: %s", reason)

    staged: list[tuple[dict, Any]] = []

    def _flush(pool: ThreadPoolExecutor) -> None:
        if not staged:
            return
        futures = []
        for row, image_bgr in staged:
            if not describe_enabled:
                continue
            futures.append(
                (
                    row,
                    pool.submit(
                        _describe_one,
                        image_bgr,
                        config=config,
                        should_stop=should_stop,
                    ),
                )
            )
        for row, future in futures:
            try:
                description = future.result()
            except Exception as exc:
                stats.describe_failed += 1
                row["describer"] = f"vlm-error: {type(exc).__name__}"
                stats.errors.append(f"describe {row['image_path']}: {exc}")
                logger.warning("Image description failed for %s: %s", row["image_path"], exc)
                continue
            row["caption"] = description.get("caption", "")
            row["people_json"] = json.dumps(description.get("people") or [], ensure_ascii=False)
            row["tags_json"] = json.dumps(description.get("tags") or [], ensure_ascii=False)
            row["describer"] = description.get("describer") or "vlm"
            stats.described += 1
        rows = [row for row, _ in staged]
        try:
            store.upsert_image_rows(rows, config=config)
        except Exception as exc:
            stats.failed += len(rows)
            stats.errors.append(f"upsert batch: {exc}")
            logger.error("Image upsert failed: %s", exc, exc_info=True)
        staged.clear()

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="vs-image-vlm") as pool:
        for position, path in enumerate(paths, start=1):
            if should_stop and should_stop():
                break
            stats.scanned += 1
            if progress is not None:
                try:
                    progress(position, total, path)
                except Exception:  # pragma: no cover - progress must never break indexing
                    logger.debug("Progress callback raised", exc_info=True)
            try:
                file_stat = os.stat(path)
                image_id = file_content_id(path)
                existing = store.get_image_row(
                    image_id,
                    config=config,
                    columns=["image_id", "mtime", "caption"],
                )
                if (
                    existing
                    and not force
                    and abs(float(existing.get("mtime") or 0) - float(file_stat.st_mtime)) < 1e-6
                    and (not describe_enabled or str(existing.get("caption") or "").strip())
                ):
                    stats.skipped += 1
                    continue
                image_bgr = load_image_bgr(path)
                if image_bgr is None:
                    stats.failed += 1
                    stats.errors.append(f"decode failed: {path}")
                    continue
                vector = _encode_image(image_bgr)
                row = _build_row(
                    image_id=image_id,
                    path=path,
                    root=root,
                    stat=file_stat,
                    image_bgr=image_bgr,
                    vector=vector,
                    description=None,
                    describer="clip-only",
                )
                stats.added += 0 if existing else 1
                stats.updated += 1 if existing else 0
                staged.append((row, image_bgr))
                if len(staged) >= workers:
                    _flush(pool)
            except Exception as exc:
                stats.failed += 1
                stats.errors.append(f"{path}: {exc}")
                logger.warning("Image index failed for %s: %s", path, exc)
        _flush(pool)

    result = stats.as_dict()
    result["describe_enabled"] = describe_enabled
    result["describe_reason"] = describe_reason
    return result


def _describe_one(image_bgr, *, config, should_stop) -> dict:
    from src.services.image_understanding import describe_image_bgr

    parsed = describe_image_bgr(
        image_bgr,
        config=config,
        should_stop_callback=should_stop,
    )
    from src.services.understanding_resource_service import get_remote_vlm_settings

    try:
        model = str(get_remote_vlm_settings(config).get("model") or "").strip()
    except Exception:  # pragma: no cover - defensive
        model = ""
    describer = f"vlm:{model}" if model else "vlm"
    return {**parsed, "describer": describer}


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def _row_to_hit(row: Mapping[str, Any], *, score: float, match: str) -> dict:
    try:
        people = json.loads(row.get("people_json") or "[]")
    except json.JSONDecodeError:
        people = []
    try:
        tags = json.loads(row.get("tags_json") or "[]")
    except json.JSONDecodeError:
        tags = []
    return {
        "image_id": str(row.get("image_id") or ""),
        "image_path": str(row.get("image_path") or ""),
        "rel_path": str(row.get("rel_path") or ""),
        "library_root": str(row.get("library_root") or ""),
        "caption": str(row.get("caption") or ""),
        "people": people if isinstance(people, list) else [],
        "tags": tags if isinstance(tags, list) else [],
        "people_count": sum(int(person.get("count") or 1) for person in people if isinstance(person, dict)),
        "score": round(float(score), 4),
        "match": match,
    }


def search_by_text(
    query: str,
    *,
    config=None,
    top_k: int = 24,
    min_score: float = 0.0,
) -> list[dict]:
    from src.core.clip_embedding import get_text_embedding
    from src.core.vector_norm import normalize_vectors
    from src.storage import image_lance_store as store

    text = str(query or "").strip()
    if not text:
        return []
    vector = normalize_vectors(np.asarray(get_text_embedding(text), dtype=np.float32))
    rows = store.search_image_vectors(vector.reshape(-1), config=config, top_k=top_k)
    hits: list[dict] = []
    for row in rows:
        distance = float(row.get("_distance") or 0.0)
        score = 1.0 - distance
        if score < float(min_score or 0):
            continue
        hits.append(_row_to_hit(row, score=score, match="vector"))
    return hits


def search_by_image(
    image_path: str,
    *,
    config=None,
    top_k: int = 24,
    min_score: float = 0.0,
) -> list[dict]:
    from src.core.image_io import load_image_bgr
    from src.storage import image_lance_store as store

    image = load_image_bgr(image_path)
    if image is None:
        raise RuntimeError(f"Could not decode the query image: {image_path}")
    vector = _encode_image(image)
    rows = store.search_image_vectors(vector, config=config, top_k=top_k)
    hits: list[dict] = []
    for row in rows:
        score = 1.0 - float(row.get("_distance") or 0.0)
        if score < float(min_score or 0):
            continue
        hits.append(_row_to_hit(row, score=score, match="image"))
    return hits


def _keyword_haystack(row: Mapping[str, Any]) -> str:
    parts = [
        str(row.get("caption") or ""),
        str(row.get("rel_path") or ""),
        str(row.get("people_json") or ""),
        str(row.get("tags_json") or ""),
    ]
    return " ".join(parts).casefold()


def search_by_keyword(
    keyword: str,
    *,
    config=None,
    top_k: int = 50,
    min_people: int = 0,
    max_people: int = 0,
) -> list[dict]:
    """Substring search over caption + people + tags, ranked by term coverage."""
    from src.storage import image_lance_store as store

    terms = [term for term in str(keyword or "").casefold().split() if term]
    if not terms:
        return []
    columns = ["image_id", "image_path", "rel_path", "library_root", "caption", "people_json", "tags_json"]
    scored: list[tuple[float, dict]] = []
    for row in store.iter_image_rows(config=config, columns=columns):
        haystack = _keyword_haystack(row)
        matched = sum(1 for term in terms if term in haystack)
        if not matched:
            continue
        hit = _row_to_hit(row, score=matched / len(terms), match="keyword")
        if min_people and hit["people_count"] < int(min_people):
            continue
        if max_people and hit["people_count"] > int(max_people):
            continue
        scored.append((hit["score"], hit))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [hit for _, hit in scored[: max(1, int(top_k))]]


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def get_image_library_status(*, config=None) -> dict:
    from src.storage import image_lance_store as store

    manifest = load_manifest()
    stats: dict[str, Any] = {"rows": 0, "dimension": 0, "described": 0, "lance_dir": ""}
    error = ""
    try:
        stats = store.image_index_stats(config=config)
    except Exception as exc:
        error = str(exc)
        logger.debug("Image index stats unavailable: %s", exc)
    from src.services.image_understanding import vlm_availability

    vlm_ready, vlm_reason = vlm_availability(config, probe=True)
    return {
        "roots": manifest.get("roots", []),
        "manifest_path": get_manifest_path(),
        "indexed": int(stats.get("rows") or 0),
        "described": int(stats.get("described") or 0),
        "dimension": int(stats.get("dimension") or 0),
        "lance_dir": stats.get("lance_dir") or "",
        "vlm_ready": vlm_ready,
        "vlm_reason": vlm_reason,
        "error": error,
    }
