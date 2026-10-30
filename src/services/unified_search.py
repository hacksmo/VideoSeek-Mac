"""One query box, two indexes: video segments and photos.

Why this module exists
----------------------
The video library and the photo library answer the same sentence in different
ways. ``run_search`` looks for *moments* (a frame or clip inside a file) and
returns ``start_sec``/``end_sec``; ``image_index_service`` looks for *files* and
returns whole photos. Neither can call the other, and the UI pages live on
separate stacks, so a user who wants "this red dress" has to know in advance
whether the material is video or stills.

This module is the seam: it runs both searches for one query and returns one
dict with two lists, plus enough metadata for the caller to say *why* each side
is empty (index not built, description not written, no attribute words, ...).

Two deliberate choices
----------------------
* **Video goes through the Agent API's ``execute_agent_search``.** That function
  already resolves the query (text or image), picks the configured frame/chunk
  mode, honours the min-score policy and expands hits to clip windows. Copying
  that into a second code path would drift within a release.
* **The two sides fail independently.** A missing video index must not blank the
  photo results, and a photo index that was never built must not turn a working
  video search into an error. Errors are recorded per side and never raised.
"""

from __future__ import annotations

import os
import time

SCOPE_ALL = "all"
SCOPE_VIDEO = "video"
SCOPE_IMAGE = "image"
SCOPES = (SCOPE_ALL, SCOPE_VIDEO, SCOPE_IMAGE)

DEFAULT_VIDEO_TOP_K = 12
DEFAULT_IMAGE_TOP_K = 48

# What each image channel means, for the status line. The photo side has three
# recall channels (see image_index_service.search_by_query); the video side only
# has the vector engine, so it never reports one.
_IMAGE_CHANNELS = {
    "attribute": "attribute",
    "keyword": "literal",
    "vector": "vector",
    "image": "vector",
}


def normalize_scope(value: str | None) -> str:
    """``all`` unless the caller named a side; unknown values fall back to all."""
    text = str(value or "").strip().lower()
    return text if text in SCOPES else SCOPE_ALL


def _coerce_limit(value, default: int) -> int:
    try:
        resolved = int(value)
    except (TypeError, ValueError):
        return int(default)
    return max(1, min(500, resolved))


def video_rows(payload: dict, *, limit: int = DEFAULT_VIDEO_TOP_K) -> list[dict]:
    """Flatten Agent API hits into the three fields the UI actually shows.

    The payload is rich (clip windows, padding, rank, durations); keeping it whole
    would leak an API contract into the widget layer, so it is reduced here to
    path / time / score and a display-ready timecode.
    """
    rows: list[dict] = []
    for hit in list((payload or {}).get("hits") or []):
        if not isinstance(hit, dict):
            continue
        path = str(hit.get("video_path") or "").strip()
        if not path:
            continue
        start = float(hit.get("start_sec") or 0.0)
        end = float(hit.get("end_sec") or start)
        rows.append(
            {
                "video_path": path,
                "file_name": os.path.basename(path) or path,
                "start_sec": start,
                "end_sec": max(start, end),
                "score": float(hit.get("score") or 0.0),
                "start_timecode": str(hit.get("start_timecode") or ""),
                "end_timecode": str(hit.get("end_timecode") or ""),
                "matched_text": str(hit.get("matched_text") or ""),
                "match_kind": str(hit.get("match_kind") or ""),
            }
        )
        if len(rows) >= int(limit):
            break
    return rows


def search_videos(query: str, *, top_k: int = DEFAULT_VIDEO_TOP_K, config=None) -> list[dict]:
    """Video moments for ``query``. Raises on a broken index — the caller decides."""
    from src.web.agent_api.schemas import AgentSearchRequest
    from src.web.agent_api.search import execute_agent_search

    text = str(query or "").strip()
    if not text:
        return []
    limit = _coerce_limit(top_k, DEFAULT_VIDEO_TOP_K)
    payload = execute_agent_search(
        AgentSearchRequest(query=text, query_type="text", top_k=limit)
    )
    return video_rows(payload, limit=limit)


def image_channel(hits) -> str:
    """Which recall channel produced these photo hits (attribute / literal / vector)."""
    for hit in hits or []:
        match = str((hit or {}).get("match") or "").strip()
        if match in _IMAGE_CHANNELS:
            return _IMAGE_CHANNELS[match]
    return ""


def search_images(query: str, *, top_k: int = DEFAULT_IMAGE_TOP_K, config=None) -> list[dict]:
    """Photos for ``query``, using the same three channels as the photo page."""
    from src.services import image_index_service

    text = str(query or "").strip()
    if not text:
        return []
    limit = _coerce_limit(top_k, DEFAULT_IMAGE_TOP_K)
    return list(image_index_service.search_by_query(text, config=config, top_k=limit))


def _error_text(exc: Exception) -> str:
    from src.web.agent_api.errors import IndexNotReadyError

    if isinstance(exc, IndexNotReadyError):
        # The API wording ("Sync the library") is developer-facing; the page maps
        # this marker to a sentence about the 视频库 page instead.
        return "index_not_ready"
    return str(exc) or exc.__class__.__name__


def search(
    query: str,
    *,
    scope: str = SCOPE_ALL,
    video_top_k: int = DEFAULT_VIDEO_TOP_K,
    image_top_k: int = DEFAULT_IMAGE_TOP_K,
    config=None,
) -> dict:
    """Run both searches and return ``{videos, images, *_error, ...}``.

    Never raises for a side that failed: a half-empty result is still useful, and
    each side records its own reason so the page can say which one is missing.
    """
    started = time.perf_counter()
    text = str(query or "").strip()
    resolved_scope = normalize_scope(scope)
    result = {
        "query": text,
        "scope": resolved_scope,
        "videos": [],
        "images": [],
        "video_error": "",
        "image_error": "",
        "image_channel": "",
        "image_attributes": "",
        "elapsed_ms": 0,
    }
    if not text:
        return result

    if resolved_scope in (SCOPE_ALL, SCOPE_VIDEO):
        try:
            result["videos"] = search_videos(text, top_k=video_top_k, config=config)
        except Exception as exc:  # noqa: BLE001 - one side failing must not blank the other
            result["video_error"] = _error_text(exc)

    if resolved_scope in (SCOPE_ALL, SCOPE_IMAGE):
        try:
            hits = search_images(text, top_k=image_top_k, config=config)
            result["images"] = hits
            result["image_channel"] = image_channel(hits)
            result["image_attributes"] = _attribute_label(text)
        except Exception as exc:  # noqa: BLE001
            result["image_error"] = str(exc) or exc.__class__.__name__

    result["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
    return result


def _attribute_label(query: str) -> str:
    """Human summary of the attributes a query named, e.g. ``衣服颜色=黑色 · 体型=偏胖``."""
    try:
        from src.services import image_attributes as attrs

        return attrs.describe_constraints(attrs.constraints_for(query))
    except Exception as exc:  # noqa: BLE001 - a label must never fail a search
        from src.app.logging_utils import note_swallowed

        note_swallowed(exc, "src/services/unified_search.py:attribute_label")
        return ""
