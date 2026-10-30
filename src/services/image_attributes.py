"""Zero-shot attribute filters for the image library.

Why this exists
---------------
The bundled CLIP text tower is English-only, so a Chinese query embedded to
noise. Routing Han queries to a literal scan (``search_by_query``) fixed
``穿西装的男人``, but only for words that happen to sit in a filename. The
queries people actually type -- 浅色衣服 / 黑色衣服 / 肚子胖的人 / 穿得花哨 --
describe *pixels*, not filenames, so no amount of string matching finds them.

A VLM description would solve it, but that needs a running vision server. This
module gets most of the way with what is already on disk: the CLIP image vector
of every indexed photo, scored against a small panel of English prompts whose
mean embedding acts as a zero-shot classifier.

Measured on a real 1262-photo fashion library (top-12 visual inspection):
黑色 12/12, 浅色 10/12, 偏胖 9/12, 花哨 10/12. Height is the exception -- CLIP
has no notion of stature, so ``height`` is marked unreliable and the UI says so.

Nothing here writes to the index: scores are computed from stored vectors, so
adding an attribute does not require re-indexing.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import numpy as np

from src.app.logging_utils import get_logger

logger = get_logger("image_attributes")

# Softmax temperature over per-label cosine scores. Cosine similarities for
# this CLIP live around 0.20-0.35, i.e. a spread of ~0.15; 0.01 keeps the
# resulting probabilities readable (a clear winner lands near 0.9, a coin flip
# near 0.5) without saturating at 1.0 for every image.
_SOFTMAX_TEMPERATURE = 0.01

# Prompt panels. English is deliberate: the active model is OpenAI CLIP, whose
# text tower only speaks English. Swapping to Chinese CLIP means rewriting these
# (see docs/image-library.md).
GROUPS: dict[str, dict[str, tuple[str, ...]]] = {
    "color": {
        "黑色": ("a photo of a person wearing black clothes",),
        "白色": ("a photo of a person wearing white clothes",),
        "浅色": (
            "a photo of a person wearing light colored clothes such as beige, "
            "cream, light grey or pale pastel",
        ),
        "深色": (
            "a photo of a person wearing dark colored clothes such as navy, "
            "dark brown or charcoal",
        ),
        "彩色": (
            "a photo of a person wearing brightly colored clothes such as red, "
            "yellow, green or blue",
        ),
        "印花": ("a photo of a person wearing patterned, floral or printed clothing",),
    },
    "build": {
        "偏胖": ("a photo of a heavy set person", "a photo of a plump person with a round belly"),
        "匀称": ("a photo of a person with an average build",),
        "偏瘦": ("a photo of a slim thin person",),
    },
    "height": {
        "偏矮": ("a photo of a short person standing, full body",),
        "中等": ("a photo of a person of average height, full body",),
        "偏高": ("a photo of a tall person standing, full body",),
    },
    "style": {
        "花哨": (
            "a photo of a person wearing a flashy outfit with many colors, "
            "patterns and accessories",
        ),
        "素净": ("a photo of a person wearing a plain simple solid color outfit",),
        "正装": ("a photo of a person wearing a formal business suit",),
        "休闲": ("a photo of a person in casual streetwear",),
    },
    "person": {
        "有人": ("a photo of a person, portrait of somebody",),
        "无人": ("a photo of an object or scenery with no people",),
    },
}

GROUP_TITLES = {
    "color": "衣服颜色",
    "build": "体型",
    "height": "身高",
    "style": "穿搭风格",
    "person": "有没有人",
}

# Groups whose signal is too weak to present as fact. The UI appends a caveat
# rather than hiding them, because "全都不给" is worse than "给了但标注不准".
LOW_CONFIDENCE_GROUPS = frozenset({"height"})

# A photo that is mostly scenery/fabric still scores high on some colour prompt,
# so person-flavoured queries should also require the 有人 label.
_PERSON_GATED_GROUPS = frozenset({"color", "build", "height", "style"})

_NEG_PREFIX = frozenset("不没别非免")


@dataclass(frozen=True)
class Constraint:
    """One parsed attribute condition, e.g. ``color in {浅色, 白色}``."""

    group: str
    labels: tuple[str, ...]
    needle: str

    @property
    def title(self) -> str:
        return GROUP_TITLES.get(self.group, self.group)


# Query lexicon: (needle, group, accepted labels). Longest needle wins, so
# 浅色 beats 色 and 不高 beats 高. Keep in mind every label must exist in
# GROUPS[group]; anything else is ignored at scoring time.
_LEXICON: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("浅色系", "color", ("浅色", "白色")),
    ("浅色的", "color", ("浅色", "白色")),
    ("浅色", "color", ("浅色", "白色")),
    ("淡色", "color", ("浅色", "白色")),
    ("米白色", "color", ("浅色", "白色")),
    ("米色", "color", ("浅色",)),
    ("奶白", "color", ("浅色", "白色")),
    ("白色", "color", ("白色", "浅色")),
    ("深色系", "color", ("深色", "黑色")),
    ("深色", "color", ("深色", "黑色")),
    ("暗色", "color", ("深色", "黑色")),
    ("黑色", "color", ("黑色", "深色")),
    ("黑衣服", "color", ("黑色", "深色")),
    ("黑", "color", ("黑色", "深色")),
    ("白", "color", ("白色", "浅色")),
    ("彩色", "color", ("彩色",)),
    ("亮色", "color", ("彩色",)),
    ("鲜艳", "color", ("彩色",)),
    ("撞色", "color", ("彩色",)),
    ("印花", "color", ("印花",)),
    ("碎花", "color", ("印花",)),
    ("花纹", "color", ("印花",)),
    ("图案", "color", ("印花",)),
    ("格子", "color", ("印花",)),
    ("条纹", "color", ("印花",)),
    ("logo", "color", ("印花",)),
    # 体型
    ("啤酒肚", "build", ("偏胖",)),
    ("大肚", "build", ("偏胖",)),
    ("肚子", "build", ("偏胖",)),
    ("微胖", "build", ("偏胖",)),
    ("圆润", "build", ("偏胖",)),
    ("胖", "build", ("偏胖",)),
    ("苗条", "build", ("偏瘦",)),
    ("瘦", "build", ("偏瘦",)),
    # 身高（不可靠，仅作参考）
    ("个子不高", "height", ("偏矮",)),
    ("不高", "height", ("偏矮",)),
    ("小个子", "height", ("偏矮",)),
    ("矮", "height", ("偏矮",)),
    ("个子高", "height", ("偏高",)),
    ("高个", "height", ("偏高",)),
    ("高挑", "height", ("偏高",)),
    # 风格
    ("花里胡哨", "style", ("花哨",)),
    ("花哨", "style", ("花哨",)),
    ("浮夸", "style", ("花哨",)),
    ("张扬", "style", ("花哨",)),
    ("时髦", "style", ("花哨",)),
    ("素净", "style", ("素净",)),
    ("简约", "style", ("素净",)),
    ("简单", "style", ("素净",)),
    ("纯色", "style", ("素净",)),
    ("西装", "style", ("正装",)),
    ("正装", "style", ("正装",)),
    ("商务", "style", ("正装",)),
    ("休闲", "style", ("休闲",)),
    ("日常", "style", ("休闲",)),
    ("街头", "style", ("休闲",)),
    ("运动", "style", ("休闲",)),
)

# Longest-first so 浅色系 resolves before 浅色, and a needle never shadows a
# longer one that starts at the same offset.
_LEXICON_SORTED = tuple(sorted(_LEXICON, key=lambda item: len(item[0]), reverse=True))


def _find_needle(needle: str, text: str) -> int:
    """First occurrence of ``needle`` that is not negated (不花哨 / 没肚子)."""
    if not needle:
        return -1
    start = 0
    while True:
        index = text.find(needle, start)
        if index < 0:
            return -1
        previous = text[index - 1] if index else ""
        if previous in _NEG_PREFIX and not needle.startswith("不"):
            start = index + 1
            continue
        return index


def constraints_for(query: str) -> tuple[Constraint, ...]:
    """Map a Han query onto at most one constraint per group.

    ``穿的花哨的胖男人`` -> ``(style=花哨, build=偏胖)``. Unknown words are
    ignored, so a query with no attribute vocabulary yields ``()`` and the
    caller falls back to the literal scan.
    """
    text = str(query or "").strip().lower()
    if not text:
        return ()
    found: dict[str, Constraint] = {}
    for needle, group, labels in _LEXICON_SORTED:
        if group in found:
            continue
        if _find_needle(needle, text) < 0:
            continue
        found[group] = Constraint(group=group, labels=tuple(labels), needle=needle)
    return tuple(found[key] for key in GROUPS if key in found)


def describe_constraints(constraints: Sequence[Constraint]) -> str:
    """One-line summary for the UI, e.g. ``衣服颜色=浅色 · 体型=偏胖``."""
    parts = []
    for constraint in constraints or ():
        label = "/".join(constraint.labels[:1]) or constraint.needle
        parts.append(f"{constraint.title}={label}")
    return " · ".join(parts)


def _prompts() -> list[str]:
    seen: list[str] = []
    for labels in GROUPS.values():
        for prompts in labels.values():
            for prompt in prompts:
                if prompt not in seen:
                    seen.append(prompt)
    return seen


def _model_signature(config: Any) -> str:
    try:
        embedding = (config or {}).get("embedding") or {}
    except AttributeError:  # pragma: no cover - non-dict config
        return ""
    if not isinstance(embedding, Mapping):
        return ""
    bits = [str(embedding.get(key) or "") for key in ("provider", "model_profile", "model_path")]
    return "|".join(bits)


def _cache_path(config: Any) -> str:
    from src.infra.paths import get_app_data_dir

    digest = hashlib.sha1(
        json.dumps([_prompts(), _model_signature(config)], ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]
    folder = os.path.join(get_app_data_dir(), "cache", "attribute_prompts")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, f"{digest}.npz")


def _encode_groups(config: Any) -> dict[str, tuple[list[str], np.ndarray]]:
    """Encode the prompt panel, cached on disk so queries cost no model time."""
    from src.core.clip_embedding import get_text_embedding

    path = _cache_path(config)
    prompts = _prompts()
    cached: dict[str, np.ndarray] = {}
    if os.path.isfile(path):
        try:
            with np.load(path) as bundle:
                cached = {key: bundle[key] for key in bundle.files}
        except (OSError, ValueError) as exc:
            logger.warning("Attribute prompt cache unreadable, re-encoding: %s", exc)
            cached = {}

    vectors: dict[str, np.ndarray] = {}
    for index, prompt in enumerate(prompts):
        key = f"p{index}"
        if key in cached:
            vectors[prompt] = cached[key]
            continue
        value = np.asarray(get_text_embedding(prompt), dtype=np.float32).reshape(-1)
        norm = float(np.linalg.norm(value))
        if not np.isfinite(norm) or norm < 1e-6:
            logger.warning("Skipping prompt with a degenerate embedding: %.60s", prompt)
            continue
        vectors[prompt] = value / norm

    if len(cached) != len(vectors):
        try:
            np.savez_compressed(path, **{f"p{i}": vectors[p] for i, p in enumerate(prompts) if p in vectors})
        except OSError as exc:
            logger.debug("Attribute prompt cache not written: %s", exc)

    groups: dict[str, tuple[list[str], np.ndarray]] = {}
    for group, labels in GROUPS.items():
        names: list[str] = []
        rows: list[np.ndarray] = []
        for label, label_prompts in labels.items():
            stack = [vectors[p] for p in label_prompts if p in vectors]
            if not stack:
                continue
            mean = np.mean(np.stack(stack), axis=0)
            norm = float(np.linalg.norm(mean))
            if not np.isfinite(norm) or norm < 1e-6:
                continue
            names.append(label)
            rows.append(mean / norm)
        if names:
            groups[group] = (names, np.stack(rows))
    return groups


_GROUP_CACHE: dict[str, dict[str, tuple[list[str], np.ndarray]]] = {}


def group_embeddings(config: Any = None) -> dict[str, tuple[list[str], np.ndarray]]:
    """Unit-length label embeddings per group; memoised per model signature."""
    key = _model_signature(config)
    hit = _GROUP_CACHE.get(key)
    if hit is None:
        hit = _encode_groups(config)
        _GROUP_CACHE.clear()
        _GROUP_CACHE[key] = hit
    return hit


def probabilities(
    vectors: np.ndarray, *, config: Any = None
) -> dict[str, tuple[list[str], np.ndarray]]:
    """``group -> (labels, P)`` where ``P[label, row]`` is a softmax over labels.

    ``vectors`` must be L2-normalised rows (the index stores them that way).
    """
    matrix = np.asarray(vectors, dtype=np.float32)
    if matrix.ndim != 2 or matrix.size == 0:
        return {}
    out: dict[str, tuple[list[str], np.ndarray]] = {}
    for group, (labels, embedding) in group_embeddings(config).items():
        # The ONNX text encoder leaves Accelerate's FP flags raised, so numpy
        # reports divide/overflow on the first matmul afterwards even though both
        # operands are finite. The result is checked below instead of trusted.
        with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
            scores = embedding @ matrix.T
        scores = np.nan_to_num(scores, nan=-1.0, posinf=1.0, neginf=-1.0)
        shifted = (scores - scores.max(axis=0, keepdims=True)) / _SOFTMAX_TEMPERATURE
        exponent = np.exp(shifted)
        out[group] = (labels, exponent / exponent.sum(axis=0, keepdims=True))
    return out


def score_constraints(
    probs: Mapping[str, tuple[list[str], np.ndarray]],
    constraints: Sequence[Constraint],
) -> np.ndarray | None:
    """Multiply the per-group confidence of each constraint into one score.

    Returns ``None`` when no constraint resolves to a known label, so the caller
    can distinguish "everything scored low" from "nothing to score".
    """
    total: np.ndarray | None = None
    for constraint in constraints:
        entry = probs.get(constraint.group)
        if entry is None:
            continue
        labels, matrix = entry
        indexes = [labels.index(label) for label in constraint.labels if label in labels]
        if not indexes:
            continue
        best = matrix[indexes].max(axis=0)
        total = best if total is None else total * best
    return total


def person_gate(probs: Mapping[str, tuple[list[str], np.ndarray]], rows: int) -> np.ndarray:
    """Mask of images that actually contain a person (1/0), else all ones."""
    entry = probs.get("person")
    if entry is None:
        return np.ones(rows, dtype=np.float32)
    labels, matrix = entry
    if "有人" not in labels:
        return np.ones(rows, dtype=np.float32)
    return (matrix[labels.index("有人")] >= 0.5).astype(np.float32)


def needs_person_gate(constraints: Sequence[Constraint]) -> bool:
    return any(constraint.group in _PERSON_GATED_GROUPS for constraint in constraints)


def is_low_confidence(constraints: Sequence[Constraint]) -> bool:
    return any(constraint.group in LOW_CONFIDENCE_GROUPS for constraint in constraints)
