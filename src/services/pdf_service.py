"""PDF document library: register folders, extract page text, search.

Phase 1 scope (text only)
-------------------------
A PDF is opened with PyMuPDF and each page's text is stored as one row in the
``pdf_pages`` Lance table. Search is **literal** (reusing the same Han/Latin
fragment split the image library uses), so Chinese queries like ``合同`` or
``资产负债表`` match page text directly. Page-image CLIP vectors and VLM
captions arrive in Phase 2 and are deliberately out of scope here.

The document library is a peer of the image and video libraries: its manifest
lives in ``document_library/library.json`` and its rows in ``pdf_documents`` /
``pdf_pages`` — nothing is written into ``image_assets``.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any, Mapping, Sequence

from src.app.logging_utils import get_logger
from src.media.formats import is_supported_pdf_path

logger = get_logger("pdf_service")

MANIFEST_VERSION = 1
LIBRARY_DIR_NAME = "document_library"
MANIFEST_NAME = "library.json"


# ---------------------------------------------------------------------------
# Manifest (folder registration)
# ---------------------------------------------------------------------------


def get_document_library_dir() -> str:
    from src.infra.paths import get_app_data_dir

    return os.path.join(get_app_data_dir(), LIBRARY_DIR_NAME)


def get_manifest_path() -> str:
    return os.path.join(get_document_library_dir(), MANIFEST_NAME)


def load_manifest() -> dict:
    path = get_manifest_path()
    if not os.path.isfile(path):
        return {"version": MANIFEST_VERSION, "roots": [], "updated_at": 0.0}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read the document library manifest: %s", exc)
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


def scan_pdf_files(root_path: str, *, recursive: bool = True) -> list[str]:
    """Return supported PDF paths under ``root_path`` (sorted, hidden skipped)."""
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
                if is_supported_pdf_path(name):
                    found.append(os.path.join(dirpath, name))
    else:
        for name in sorted(os.listdir(root)):
            full = os.path.join(root, name)
            if name.startswith(".") or not os.path.isfile(full):
                continue
            if is_supported_pdf_path(name):
                found.append(full)
    return found


def scan_manifest_roots(*, recursive: bool = True) -> list[str]:
    manifest = load_manifest()
    seen: set[str] = set()
    files: list[str] = []
    for entry in manifest.get("roots", []):
        if not isinstance(entry, dict):
            continue
        root = str(entry.get("root") or "")
        if not root:
            continue
        for path in scan_pdf_files(root, recursive=recursive):
            real = os.path.realpath(path)
            if real in seen:
                continue
            seen.add(real)
            files.append(path)
    return files


# ---------------------------------------------------------------------------
# Content identity & extraction
# ---------------------------------------------------------------------------


def file_content_id(path: str) -> str:
    """Hash file bytes (content-addressed), so the same PDF in two folders is one doc."""
    digest = hashlib.md5()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def _rel_path(path: str, root: str) -> str:
    try:
        return os.path.relpath(path, root)
    except ValueError:
        return path


def _page_texts(pdf_path: str) -> list[str]:
    """Extract per-page text via PyMuPDF. Returns one string per page."""
    try:
        import pymupdf  # PyMuPDF >= 1.24 prefers the `pymupdf` module name
    except ImportError:  # pragma: no cover - older installs
        import fitz as pymupdf  # type: ignore

    texts: list[str] = []
    try:
        with pymupdf.open(pdf_path) as doc:
            for page in doc:
                texts.append(str(page.get_text() or "").strip())
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Could not read PDF %s: %s", pdf_path, exc)
        return []
    return texts


# ---------------------------------------------------------------------------
# Indexing
# ---------------------------------------------------------------------------


def index_documents(
    paths: Sequence[str],
    *,
    config=None,
    progress=None,
    should_stop=None,
    root_for_path=None,
) -> dict:
    """Index the given PDFs: one document row + one page row per page (text only).

    Returns ``{scanned, added, updated, skipped, failed, pages}``.
    """
    from src.storage import pdf_lance_store as store

    paths = list(paths or [])
    total = len(paths)
    stats: dict[str, Any] = {
        "scanned": 0,
        "added": 0,
        "updated": 0,
        "skipped": 0,
        "failed": 0,
        "pages": 0,
    }

    doc_rows: list[dict] = []
    page_rows: list[dict] = []

    for index, path in enumerate(paths):
        if should_stop is not None and should_stop():
            break
        path = str(path or "")
        if not path or not os.path.isfile(path):
            stats["failed"] += 1
            continue
        stats["scanned"] += 1
        try:
            pdf_id = file_content_id(path)
            if not pdf_id:
                stats["failed"] += 1
                continue
            texts = _page_texts(path)
            has_text = any(text for text in texts)
            mtime = os.path.getmtime(path)
            size = os.path.getsize(path)
            root = ""
            if root_for_path is not None:
                root = str(root_for_path(path) or "")
            rel = _rel_path(path, root) if root else os.path.basename(path)

            doc_rows.append(
                {
                    "pdf_id": pdf_id,
                    "library_root": root,
                    "pdf_path": path,
                    "rel_path": rel,
                    "page_count": len(texts),
                    "has_text": has_text,
                    "mtime": mtime,
                    "bytes": size,
                }
            )
            for page_no, text in enumerate(texts, start=1):
                page_id = hashlib.md5(f"{pdf_id}:{page_no}".encode("utf-8")).hexdigest()
                page_rows.append(
                    {
                        "page_id": page_id,
                        "pdf_id": pdf_id,
                        "page_no": page_no,
                        "text": text,
                    }
                )
            stats["pages"] += len(texts)
            stats["added"] += 1
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Failed to index PDF %s: %s", path, exc)
            stats["failed"] += 1
        if progress is not None:
            progress(index + 1, total, path)

    if doc_rows:
        store.upsert_documents(doc_rows, config=config)
    if page_rows:
        store.upsert_pages(page_rows, config=config)

    return stats


# ---------------------------------------------------------------------------
# Search (literal, text only)
# ---------------------------------------------------------------------------


def _is_cjk_char(ch: str) -> bool:
    code = ord(ch)
    return (
        0x4E00 <= code <= 0x9FFF
        or 0x3400 <= code <= 0x4DBF
        or 0xF900 <= code <= 0xFAFF
    )


_CJK_STOP_CHARS = frozenset("的了和与或是在有就都而及")
_FRAGMENT_MIN = 2
_FRAGMENT_MAX = 4


def _query_fragments(query: str) -> tuple[list[str], list[str]]:
    """Split into Han fragments (2-4 char windows) plus Latin words."""
    text = str(query or "").casefold()
    fragments: list[str] = []
    seen: set[str] = set()

    def _push(value: str) -> None:
        if value and value not in seen:
            seen.add(value)
            fragments.append(value)

    run: list[str] = []

    def _flush() -> None:
        word = "".join(run)
        run.clear()
        if len(word) < _FRAGMENT_MIN:
            return
        for size in range(min(_FRAGMENT_MAX, len(word)), _FRAGMENT_MIN - 1, -1):
            for start in range(0, len(word) - size + 1):
                _push(word[start : start + size])

    for ch in text:
        if _is_cjk_char(ch) and ch not in _CJK_STOP_CHARS:
            run.append(ch)
        else:
            _flush()
    _flush()

    latin: list[str] = []
    word: list[str] = []
    for ch in text:
        if ch.isalnum() and not _is_cjk_char(ch):
            word.append(ch)
        else:
            if len(word) >= 2:
                latin.append("".join(word))
            word = []
    if len(word) >= 2:
        latin.append("".join(word))
    return fragments, latin


def search_by_text(
    query: str,
    *,
    config=None,
    top_k: int = 50,
) -> list[dict]:
    """Literal search over page text; each hit locates back to ``{pdf_path, page_no}``.

    Returns rows shaped ``{pdf_id, pdf_path, rel_path, page_no, text, score}`` with
    a ``snippet`` cut around the first matching fragment.
    """
    from src.storage import pdf_lance_store as store

    fragments, latin = _query_fragments(query)
    if not fragments and not latin:
        return []

    # Load document metadata once to enrich page hits with a path.
    docs: dict[str, dict] = {}
    for doc in store.iter_documents(
        config=config, columns=["pdf_id", "pdf_path", "rel_path", "library_root"]
    ):
        docs[str(doc.get("pdf_id") or "")] = doc

    scored: list[dict] = []
    for page in store.iter_pages(config=config, columns=["page_id", "pdf_id", "page_no", "text"]):
        text = str(page.get("text") or "")
        if not text:
            continue
        haystack = text.casefold()
        score = _literal_score(haystack, fragments, latin)
        if score <= 0:
            continue
        doc = docs.get(str(page.get("pdf_id") or ""), {})
        scored.append(
            {
                "pdf_id": str(page.get("pdf_id") or ""),
                "pdf_path": str(doc.get("pdf_path") or ""),
                "rel_path": str(doc.get("rel_path") or ""),
                "page_no": int(page.get("page_no") or 1),
                "text": text,
                "snippet": _snippet(text, fragments, latin),
                "score": score,
            }
        )
    scored.sort(key=lambda hit: hit["score"], reverse=True)
    return scored[: max(1, int(top_k))]


def _literal_score(haystack: str, fragments: Sequence[str], latin: Sequence[str]) -> float:
    lengths = sorted((len(frag) for frag in fragments if frag in haystack), reverse=True)
    words = sum(1 for word in latin if word in haystack)
    if not lengths and not words:
        return 0.0
    best = lengths[0] if lengths else 0
    extra = len(lengths) - 1
    return min(1.0, (best * 10.0 + extra * 3.0 + words * 5.0) / (_FRAGMENT_MAX * 10.0 + 6.0))


def _snippet(text: str, fragments: Sequence[str], latin: Sequence[str], *, width: int = 80) -> str:
    """A short excerpt centered on the first matching fragment."""
    haystack = text.casefold()
    target = next((frag for frag in fragments if frag in haystack), None)
    if target is None:
        target = next((word for word in latin if word in haystack), None)
    if target is None:
        return text[:width]
    idx = haystack.find(target)
    start = max(0, idx - width // 2)
    end = min(len(text), idx + len(target) + width // 2)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{text[start:end]}{suffix}"


def get_document_library_status(*, config=None) -> dict:
    from src.storage import pdf_lance_store as store

    manifest = load_manifest()
    stats = store.pdf_index_stats(config=config)
    return {
        "roots": manifest.get("roots", []),
        "manifest_path": get_manifest_path(),
        "documents": int(stats.get("documents") or 0),
        "pages": int(stats.get("pages") or 0),
        "lance_dir": stats.get("lance_dir") or "",
    }
