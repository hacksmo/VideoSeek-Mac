"""LanceDB storage for the still-image library.

Design notes
------------
The image library is **additive**: it stores its rows in a dedicated table
(``image_assets``) inside the same Lance directory the video pipeline uses.
The existing ``frames`` / ``chunks`` / ``dialogue_segments`` schemas are not
touched, so a video index built before this module existed keeps working
without a rebuild and an image index can be dropped with no side effects.

One row == one image file. There is no timeline, so no ``start`` / ``end``
columns; the unified search layer treats ``image_id`` as the identity instead
of ``video_id``.
"""

from __future__ import annotations

import os
import re
import threading
from typing import Any, Iterable, Iterator, Mapping, Sequence

import numpy as np

from src.app.logging_utils import get_logger

logger = get_logger("image_lance_store")

IMAGE_TABLE_NAME = "image_assets"

# Columns other than the vector. Order matters for row dict construction but
# Lance is name-based, so keeping it declarative is enough.
_TEXT_COLUMNS = ("image_id", "library_root", "image_path", "rel_path")
_META_COLUMNS = ("mtime", "width", "height", "bytes")
_DESC_COLUMNS = ("caption", "people_json", "tags_json", "describer")

_IMAGE_ID_RE = re.compile(r"^[0-9a-f]{32}$")

_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _pa():
    import pyarrow as pa

    return pa


def _write_lock(lance_dir: str) -> threading.RLock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(lance_dir)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[lance_dir] = lock
        return lock


def get_image_lance_dir(config=None) -> str:
    """Lance directory shared with the video index (``<models>/lance``)."""
    from src.storage.config_store import get_local_model_asset_dirs

    base_dir = str(get_local_model_asset_dirs(config=config).get("base_dir") or "")
    if not base_dir:
        raise RuntimeError("Model asset directory is not configured; cannot open the image index.")
    return os.path.join(os.path.normpath(base_dir), "lance")


def image_table_schema(dimension: int):
    pa = _pa()
    return pa.schema(
        [
            pa.field("image_id", pa.string()),
            pa.field("library_root", pa.string()),
            pa.field("image_path", pa.string()),
            pa.field("rel_path", pa.string()),
            pa.field("mtime", pa.float64()),
            pa.field("width", pa.int32()),
            pa.field("height", pa.int32()),
            pa.field("bytes", pa.int64()),
            pa.field("caption", pa.string()),
            pa.field("people_json", pa.string()),
            pa.field("tags_json", pa.string()),
            pa.field("describer", pa.string()),
            pa.field("vector", pa.list_(pa.float32(), int(dimension))),
        ]
    )


def _connect(config=None):
    import lancedb

    lance_dir = get_image_lance_dir(config)
    os.makedirs(lance_dir, exist_ok=True)
    return lancedb.connect(lance_dir), lance_dir


def _table_names(db) -> list[str]:
    """Table names, tolerant of the lancedb 0.2x/0.3x API split.

    ``list_tables()`` changed shape more than once (plain list -> paginated
    response object), and ``table_names()`` is deprecated but still correct in
    the 0.34 line this repo pins. Prefer the stable one and silence the notice.
    """
    import warnings
    from collections.abc import Mapping as _MappingABC

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        if hasattr(db, "table_names"):
            try:
                return [str(name) for name in db.table_names()]
            except TypeError:  # very old versions returned a callable
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


def _read_vector_dim(table) -> int:
    """Return the declared vector width, or 0 when it cannot be determined."""
    import pyarrow as pa

    try:
        value_type = table.schema.field("vector").type
    except (KeyError, TypeError):
        return 0
    if pa.types.is_fixed_size_list(value_type):
        return int(value_type.list_size)
    if pa.types.is_list(value_type) or pa.types.is_large_list(value_type):
        # Variable-length vectors: read one row to learn the width.
        try:
            arrow = table.search().select(["vector"]).limit(1).to_arrow()
            if arrow.num_rows:
                return len(arrow.column("vector")[0].as_py())
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Could not read image vector width: %s", exc)
    return 0


def open_image_table(config=None, *, dimension: int = 0, create: bool = True):
    """Open (and optionally create) the image table.

    ``dimension`` is required to create the table. When the stored width differs
    from the active embedding model the table is recreated: image rows are
    derived data and can always be rebuilt, so a silent dimension mismatch is
    worse than a rebuild.
    """
    db, lance_dir = _connect(config)
    lock = _write_lock(lance_dir)
    with lock:
        exists = IMAGE_TABLE_NAME in _table_names(db)
        if exists:
            table = db.open_table(IMAGE_TABLE_NAME)
            stored_dim = _read_vector_dim(table)
            if dimension and stored_dim and int(stored_dim) != int(dimension):
                logger.warning(
                    "Image index vector width changed (%s -> %s); recreating %s. Re-index the images.",
                    stored_dim,
                    dimension,
                    IMAGE_TABLE_NAME,
                )
                db.drop_table(IMAGE_TABLE_NAME)
                exists = False
            else:
                return table, int(stored_dim or dimension)
        if not create:
            return None, 0
        if not dimension:
            raise RuntimeError("A vector dimension is required to create the image index table.")
        table = db.create_table(
            IMAGE_TABLE_NAME,
            schema=image_table_schema(int(dimension)),
            exist_ok=True,
        )
        return table, int(dimension)


def _safe_id(image_id: str) -> str:
    text = str(image_id or "").strip().lower()
    if not _IMAGE_ID_RE.match(text):
        raise ValueError(f"Invalid image id: {image_id!r}")
    return text


def upsert_image_rows(rows: Sequence[Mapping[str, Any]], *, config=None) -> int:
    """Insert or replace rows keyed by ``image_id``."""
    payload = [dict(row) for row in rows if row]
    if not payload:
        return 0
    dimension = len(payload[0].get("vector") or [])
    if not dimension:
        raise RuntimeError("Image rows must carry a non-empty vector.")
    table, _ = open_image_table(config, dimension=dimension)
    ids = [_safe_id(str(row["image_id"])) for row in payload]
    _, lance_dir = _connect(config)
    with _write_lock(lance_dir):
        try:
            (
                table.merge_insert("image_id")
                .when_matched_update_all()
                .when_not_matched_insert_all()
                .execute(payload)
            )
        except Exception as exc:
            # merge_insert depends on a backend capability that older Lance
            # files lack; delete-then-add is equivalent for derived rows.
            logger.debug("merge_insert failed (%s); falling back to delete+add", exc)
            clause = " OR ".join(f"image_id = '{value}'" for value in ids)
            table.delete(clause)
            table.add(payload)
    return len(payload)


def delete_images(image_ids: Iterable[str], *, config=None) -> int:
    ids = [_safe_id(value) for value in image_ids if value]
    if not ids:
        return 0
    table, _ = open_image_table(config, create=False)
    if table is None:
        return 0
    _, lance_dir = _connect(config)
    with _write_lock(lance_dir):
        clause = " OR ".join(f"image_id = '{value}'" for value in ids)
        table.delete(clause)
    return len(ids)


def get_image_row(image_id: str, *, config=None, columns: Sequence[str] | None = None) -> dict | None:
    rows = get_image_rows([image_id], config=config, columns=columns)
    return rows[0] if rows else None


def get_image_rows(
    image_ids: Sequence[str],
    *,
    config=None,
    columns: Sequence[str] | None = None,
) -> list[dict]:
    ids = [_safe_id(value) for value in image_ids if value]
    if not ids:
        return []
    table, _ = open_image_table(config, create=False)
    if table is None:
        return []
    clause = " OR ".join(f"image_id = '{value}'" for value in ids)
    builder = table.search().where(clause).limit(len(ids))
    if columns:
        builder = builder.select(list(columns))
    arrow = builder.to_arrow()
    return [dict(row) for row in arrow.to_pylist()]


def search_image_vectors(
    vector: Sequence[float] | np.ndarray,
    *,
    config=None,
    top_k: int = 24,
    where: str = "",
) -> list[dict]:
    """Cosine ANN over the image table. Returns rows with ``_distance``."""
    values = np.asarray(vector, dtype=np.float32).reshape(-1)
    if values.size == 0:
        return []
    table, _ = open_image_table(config, dimension=int(values.size), create=False)
    if table is None:
        return []
    builder = table.search(values.tolist()).metric("cosine")
    if where:
        builder = builder.where(where)
    try:
        arrow = builder.limit(max(1, int(top_k))).to_arrow()
    except Exception as exc:
        logger.warning("Image vector search failed: %s", exc)
        return []
    return arrow.to_pylist()


def iter_image_rows(
    *,
    config=None,
    columns: Sequence[str] | None = None,
    page_size: int = 4096,
) -> Iterator[dict]:
    """Stream every image row (used by keyword search and status reporting)."""
    table, _ = open_image_table(config, create=False)
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


def image_index_stats(*, config=None) -> dict:
    table, _ = open_image_table(config, create=False)
    if table is None:
        return {"rows": 0, "dimension": 0, "lance_dir": get_image_lance_dir(config), "described": 0}
    rows = int(table.count_rows() or 0)
    described = 0
    if rows:
        try:
            arrow = table.search().where("caption != ''").select(["image_id"]).limit(rows).to_arrow()
            described = int(arrow.num_rows)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Could not count described images: %s", exc)
    return {
        "rows": rows,
        "dimension": _read_vector_dim(table),
        "lance_dir": get_image_lance_dir(config),
        "described": described,
    }
