"""Supported library video container suffixes (ingestion whitelist)."""
from __future__ import annotations

import os

# Keep in sync with product docs / user-facing "supported formats" copy.
# Export destinations stay narrower (mp4/mkv/mov) elsewhere.
VIDEO_EXTS = (
    ".mp4",
    ".m4v",
    ".mkv",
    ".avi",
    ".mov",
    ".flv",
    ".wmv",
    ".webm",
    ".mpg",
    ".mpeg",
    ".ts",
    ".m2ts",
    ".mts",
)

# MPEG transport / program streams often omit container duration; probe harder.
_TRANSPORT_LIKE_EXTS = frozenset({".ts", ".m2ts", ".mts", ".mpg", ".mpeg"})

# MPEG program streams: VLC :start-time often lands tens of seconds early.
_MPEG_PROGRAM_STREAM_EXTS = frozenset({".mpg", ".mpeg"})


def is_supported_video_path(path: str | os.PathLike[str]) -> bool:
    name = os.fspath(path).lower()
    return name.endswith(VIDEO_EXTS)


def is_transport_like_video_path(path: str | os.PathLike[str]) -> bool:
    name = os.fspath(path).lower()
    return any(name.endswith(ext) for ext in _TRANSPORT_LIKE_EXTS)


def needs_seekable_preview_proxy(path: str | os.PathLike[str]) -> bool:
    """True when direct player seek on the source container is unreliable."""
    name = os.fspath(path).lower()
    return any(name.endswith(ext) for ext in _MPEG_PROGRAM_STREAM_EXTS)


# ---------------------------------------------------------------------------
# Still images (image library)
#
# Photos are indexed by ``image_id`` with no timeline, so they must NOT be added
# to ``VIDEO_EXTS``: several call sites treat that tuple as "ffprobe can read a
# duration from it". The image library keeps its own whitelist and its own
# storage table (``image_assets``), leaving the video pipeline untouched.
# ---------------------------------------------------------------------------
IMAGE_EXTS = (
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".bmp",
    ".tif",
    ".tiff",
    ".heic",
    ".heif",
    ".avif",
)


def is_supported_image_path(path: str | os.PathLike[str]) -> bool:
    name = os.fspath(path).lower()
    return name.endswith(IMAGE_EXTS)


def is_supported_media_path(path: str | os.PathLike[str]) -> bool:
    """True for a video container OR a still image the app can index."""
    return is_supported_video_path(path) or is_supported_image_path(path)


# ---------------------------------------------------------------------------
# Documents (PDF library)
#
# PDFs are a third, peer library type (alongside video and image). They are
# indexed into their own tables (``pdf_documents`` / ``pdf_pages``) and never
# mixed into ``image_assets``, so a broken PDF index cannot touch either the
# video or image pipeline.
# ---------------------------------------------------------------------------
PDF_EXTS = (".pdf",)


def is_supported_pdf_path(path: str | os.PathLike[str]) -> bool:
    name = os.fspath(path).lower()
    return name.endswith(PDF_EXTS)
