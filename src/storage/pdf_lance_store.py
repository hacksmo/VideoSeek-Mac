"""LanceDB storage for the PDF document library.

Design notes
------------
The document library is a **peer** of the image and video libraries, not a part
of them. It stores rows in two dedicated tables (``pdf_documents`` /
``pdf_pages``) inside the same Lance directory, keyed independently of
``image_assets`` and the video tables, so a broken PDF index cannot touch either
pipeline and can be dropped with no side effects.

Phase 1 indexes **text only** (one row per page, extracted with PyMuPDF). The
``vector`` column is reserved for Phase 2 (page-image CLIP vectors); until then
rows carry no vector and text search is literal/keyword only.
"""

from __future__ import annotations

import os
import re
import threading
from typing import Any, Iterable, Iterator, Mapping, Sequence

from src.app.logging_utils import get_logger

logger = get_logger("pdf_lance_store")

PDF_DOCUMENTS_TABLE = "pdf_documents"
PDF_PAGES_TABLE = "pdf_pages"

_PDF_ID_RE = re.compile(r"^[0-9a-f]{32}$")

_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _write_lock(lance_dir: str) -> threading.RLock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(lance_dir)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[lance_dir] = lock
        return lock


def get_pdf_lance_dir(config=None) -> str:
    """Lance directory shared with the video/image index (``<models>/lance``)."""
    from src.storage.config_store import get_local_model_asset_dirs

    base_dir = str(get_local_model_asset_dirs(config=config).get("base_dir") or "")
    if not base_dir:
        raise RuntimeError("Model asset directory is not configured; cannot open the PDF index.")
    return os.path.join(os.path.normpath(base_dir), "lance")


def _documents_schema():
    import pyarrow as pa

    return pa.schema(
        [
            pa.field("pdf_id", pa.string()),
            pa.field("library_root", pa.string()),
            pa.field("pdf_path", pa.string()),
            pa.field("rel_path", pa.string()),
            pa.field("page_count", pa.int32()),
            pa.field("has_text", pa.bool_()),
            pa.field("mtime", pa.float64()),
            pa.field("bytes", pa.int64()),
        ]
    )


def _pages_schema():
    import pyarrow as pa

    return pa.schema(
        [
            pa.field("page_id", pa.string()),
            pa.field("pdf_id", pa.string()),
            pa.field("page_no", pa.int32()),
            pa.field("text", pa.string()),
        ]
    )


def _connect(config=None):
    import lancedb

    lance_dir = get_pdf_lance_dir(config)
    os.makedirs(lance_dir, exist_ok=True)
    return lancedb.connect(lance_dir), lance_dir


def _table_names(db) -> list[str]:
    import warnings
    from collections.abc import Mapping as _MappingABC

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        if hasattr(db, "table_names"):
            try:
                return [str(name) for name in db.table_names()]
            except TypeError:
                return [str(name) for name in db.table_names()]
        result = db.list_tables()
    tables = getattr(result, "tables", None)
    if tables is None and isinstance(result, _MappingABC):
        tables = result.get("tables")
    if tables is None and isinstance(result, (list, tuple)):
        tables = [
            item[1]
            for item in result
            if isinstance(item, (list, tuple)) and item and item[0] == "tables"
        ]
        if not tables:
            tables = result
        elif isinstance(tables, list) and tables and isinstance(tables[0], list):
            tables = tables[0]
    return [str(name) for name in (tables or [])]


def _open_table(name: str, schema, config=None, *, create: bool = True):
    db, lance_dir = _connect(config)
    lock = _write_lock(lance_dir)
    with lock:
        exists = name in _table_names(db)
        if exists:
            return db.open_table(name)
        if not create:
            return None
        return db.create_table(name, schema=schema, exist_ok=True)


def open_documents_table(config=None, *, create: bool = True):
    return _open_table(PDF_DOCUMENTS_TABLE, _documents_schema(), config=config, create=create)


def open_pages_table(config=None, *, create: bool = True):
    return _open_table(PDF_PAGES_TABLE, _pages_schema(), config=config, create=create)


def _safe_id(value: str, field: str = "id") -> str:
    text = str(value or "").strip().lower()
    if not _PDF_ID_RE.match(text):
        raise ValueError(f"Invalid {field}: {value!r}")
    return text


def upsert_documents(rows: Sequence[Mapping[str, Any]], *, config=None) -> int:
    payload = [dict(row) for row in rows if row]
    if not payload:
        return 0
    table = open_documents_table(config=config)
    ids = [_safe_id(str(row["pdf_id"]), "pdf_id") for row in payload]
    _, lance_dir = _connect(config)
    with _write_lock(lance_dir):
        try:
            (
                table.merge_insert("pdf_id")
                .when_matched_update_all()
                .when_not_matched_insert_all()
                .execute(payload)
            )
        except Exception as exc:
            logger.debug("merge_insert failed (%s); falling back to delete+add", exc)
            clause = " OR ".join(f"pdf_id = '{value}'" for value in ids)
            table.delete(clause)
            table.add(payload)
    return len(payload)


def upsert_pages(rows: Sequence[Mapping[str, Any]], *, config=None) -> int:
    payload = [dict(row) for row in rows if row]
    if not payload:
        return 0
    table = open_pages_table(config=config)
    ids = [_safe_id(str(row["page_id"]), "page_id") for row in payload]
    _, lance_dir = _connect(config)
    with _write_lock(lance_dir):
        try:
            (
                table.merge_insert("page_id")
                .when_matched_update_all()
                .when_not_matched_insert_all()
                .execute(payload)
            )
        except Exception as exc:
            logger.debug("merge_insert failed (%s); falling back to delete+add", exc)
            clause = " OR ".join(f"page_id = '{value}'" for value in ids)
            table.delete(clause)
            table.add(payload)
    return len(payload)


def delete_documents(pdf_ids: Iterable[str], *, config=None) -> int:
    ids = [_safe_id(value, "pdf_id") for value in pdf_ids if value]
    if not ids:
        return 0
    table = open_documents_table(config=config, create=False)
    if table is None:
        return 0
    _, lance_dir = _connect(config)
    with _write_lock(lance_dir):
        clause = " OR ".join(f"pdf_id = '{value}'" for value in ids)
        table.delete(clause)
    # Cascade: drop the document's pages too.
    pages = open_pages_table(config=config, create=False)
    if pages is not None:
        with _write_lock(lance_dir):
            clause = " OR ".join(f"pdf_id = '{value}'" for value in ids)
            pages.delete(clause)
    return len(ids)


def iter_documents(
    *, config=None, columns: Sequence[str] | None = None, page_size: int = 4096
) -> Iterator[dict]:
    table = open_documents_table(config=config, create=False)
    if table is None:
        return
    total = int(table.count_rows() or 0)
    offset = 0
    while offset < total:
        builder = table.search().limit(page_size).offset(offset)
        if columns:
            builder = builder.select(list(columns))
        arrow = builder.to_arrow()
        rows = arrow.to_pylist()
        if not rows:
            break
        for row in rows:
            yield row
        offset += len(rows)


def iter_pages(
    *, config=None, columns: Sequence[str] | None = None, page_size: int = 4096
) -> Iterator[dict]:
    table = open_pages_table(config=config, create=False)
    if table is None:
        return
    total = int(table.count_rows() or 0)
    offset = 0
    while offset < total:
        builder = table.search().limit(page_size).offset(offset)
        if columns:
            builder = builder.select(list(columns))
        arrow = builder.to_arrow()
        rows = arrow.to_pylist()
        if not rows:
            break
        for row in rows:
            yield row
        offset += len(rows)


def pdf_index_stats(*, config=None) -> dict:
    docs = open_documents_table(config=config, create=False)
    pages = open_pages_table(config=config, create=False)
    return {
        "documents": int(docs.count_rows() or 0) if docs is not None else 0,
        "pages": int(pages.count_rows() or 0) if pages is not None else 0,
        "lance_dir": get_pdf_lance_dir(config),
    }
