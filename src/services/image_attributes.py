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
        # 男装细分风格（老钱风等）
        "老钱风": (
            "a photo of a person wearing an old money aesthetic outfit with "
            "quiet luxury, high quality natural fabrics, neutral tones, a "
            "classic knit polo, tailored trousers and leather loafers",
        ),
        "工装风": (
            "a photo of a person wearing a rugged workwear outfit such as a "
            "denim or chore jacket, cargo pants and boots",
        ),
        "学院风": (
            "a photo of a person wearing a preppy ivy league outfit such as a "
            "blazer, knit sweater, oxford shirt and chinos",
        ),
        "运动风": (
            "a photo of a person wearing sporty athletic clothes such as a "
            "tracksuit, hoodie and sneakers",
        ),
        "商务休闲": (
            "a photo of a person wearing business casual clothes such as a "
            "button-up shirt with chinos and a blazer without a tie",
        ),
        "街头风": (
            "a photo of a person wearing streetwear such as a graphic hoodie, "
            "oversized tee, cargo pants and sneakers",
        ),
    },
    "clothing": {
        "衬衫": (
            "a photo of a person wearing a button-up dress shirt",
            "a photo of a person wearing a collared shirt",
        ),
        "T恤": (
            "a photo of a person wearing a short sleeve t-shirt",
            "a photo of a person wearing a crew neck tee",
        ),
        "Polo衫": (
            "a photo of a person wearing a collared polo shirt",
        ),
        "毛衣": (
            "a photo of a person wearing a knit sweater or jumper",
        ),
        "卫衣": (
            "a photo of a person wearing a hoodie or crewneck sweatshirt",
        ),
        "西装外套": (
            "a photo of a person wearing a tailored blazer or suit jacket",
        ),
        "夹克": (
            "a photo of a person wearing a casual jacket such as a bomber or "
            "denim jacket",
        ),
        "风衣": (
            "a photo of a person wearing a trench coat or windbreaker",
        ),
        "大衣": (
            "a photo of a person wearing a long wool overcoat",
        ),
        "羽绒服": (
            "a photo of a person wearing a puffy down jacket",
        ),
        "西裤": (
            "a photo of a person wearing tailored dress trousers",
        ),
        "牛仔裤": (
            "a photo of a person wearing denim jeans",
        ),
        "短裤": (
            "a photo of a person wearing shorts",
        ),
        "运动裤": (
            "a photo of a person wearing sweatpants or joggers",
        ),
        "马甲": (
            "a photo of a person wearing a vest or waistcoat",
        ),
        "针织开衫": (
            "a photo of a person wearing a knitted cardigan",
        ),
        "背心": (
            "a photo of a person wearing a sleeveless tank top or vest",
        ),
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
    "clothing": "服装单品",
    "person": "有没有人",
}

# Chinese panel for Chinese CLIP: its text tower was trained on Chinese pairs,
# so English prompts land off-distribution (a query like 衬衫 silently scores
# near noise). Label keys must mirror GROUPS exactly — the lexicon, the UI and
# score_constraints all speak in these labels; only the prompts differ.
GROUPS_ZH: dict[str, dict[str, tuple[str, ...]]] = {
    "color": {
        "黑色": ("一张穿着黑色衣服的人的照片",),
        "白色": ("一张穿着白色衣服的人的照片",),
        "浅色": (
            "一张穿着浅色衣服的人的照片，米色、奶白、浅灰或淡彩色",
        ),
        "深色": (
            "一张穿着深色衣服的人的照片，藏青、深棕或炭灰色",
        ),
        "彩色": (
            "一张穿着鲜艳彩色衣服的人的照片，红色、黄色、绿色或蓝色",
        ),
        "印花": ("一张穿着印花、碎花或图案衣服的人的照片",),
    },
    "build": {
        "偏胖": ("一张身材偏胖的人的照片", "一张有圆肚子、体型富态的人的照片"),
        "匀称": ("一张身材匀称普通的人的照片",),
        "偏瘦": ("一张身材偏瘦纤细的人的照片",),
    },
    "height": {
        "偏矮": ("一张个子偏矮的人的全身照",),
        "中等": ("一张身高普通的人的全身照",),
        "偏高": ("一张个子高挑的人的全身照",),
    },
    "style": {
        "花哨": (
            "一张穿着花哨衣服的人的照片，颜色鲜艳、图案复杂、配饰很多",
        ),
        "素净": ("一张穿着素净纯色简单衣服的人的照片",),
        "正装": ("一张穿着正装西服套装的人的照片",),
        "休闲": ("一张穿着休闲街头服饰的人的照片",),
        "老钱风": (
            "一张穿着老钱风衣服的人的照片，低调奢华、高级天然面料、中性色调、"
            "经典针织polo衫、剪裁西裤和乐福鞋",
        ),
        "工装风": (
            "一张穿着工装风衣服的人的照片，牛仔外套或工装夹克、工装裤和靴子",
        ),
        "学院风": (
            "一张穿着学院风衣服的人的照片，西装外套、针织毛衣、牛津衬衫和卡其裤",
        ),
        "运动风": (
            "一张穿着运动服的人的照片，运动套装、连帽卫衣和运动鞋",
        ),
        "商务休闲": (
            "一张穿着商务休闲装的人的照片，衬衫配卡其裤和西装外套，不打领带",
        ),
        "街头风": (
            "一张穿着街头潮牌服饰的人的照片，印花卫衣、宽松T恤、工装裤和球鞋",
        ),
    },
    "clothing": {
        "衬衫": (
            "一张穿着衬衫的人的照片",
            "一张穿着有领子的扣子衬衫的人的照片",
        ),
        "T恤": (
            "一张穿着短袖T恤的人的照片",
            "一张穿着圆领T恤的人的照片",
        ),
        "Polo衫": (
            "一张穿着polo衫的人的照片",
        ),
        "毛衣": (
            "一张穿着针织毛衣的人的照片",
        ),
        "卫衣": (
            "一张穿着连帽卫衣或圆领卫衣的人的照片",
        ),
        "西装外套": (
            "一张穿着西装外套的人的照片",
        ),
        "夹克": (
            "一张穿着夹克的人的照片，飞行员夹克或牛仔外套",
        ),
        "风衣": (
            "一张穿着风衣的人的照片",
        ),
        "大衣": (
            "一张穿着长款羊毛大衣的人的照片",
        ),
        "羽绒服": (
            "一张穿着羽绒服的人的照片",
        ),
        "西裤": (
            "一张穿着西裤的人的照片",
        ),
        "牛仔裤": (
            "一张穿着牛仔裤的人的照片",
        ),
        "短裤": (
            "一张穿着短裤的人的照片",
        ),
        "运动裤": (
            "一张穿着运动裤或束脚裤的人的照片",
        ),
        "马甲": (
            "一张穿着马甲背心的人的照片",
        ),
        "针织开衫": (
            "一张穿着针织开衫的人的照片",
        ),
        "背心": (
            "一张穿着无袖背心或吊带的人的照片",
        ),
    },
    "person": {
        "有人": ("一张人的照片，人物肖像",),
        "无人": ("一张没有人只有物体或风景的照片",),
    },
}


def groups_for(config: Any = None) -> dict[str, dict[str, tuple[str, ...]]]:
    """Prompt panel matching the active model's language.

    English panel for OpenAI CLIP (text tower is English-only); Chinese panel
    for Chinese CLIP. Same label keys either way, so lexicon/scoring code is
    model-agnostic.
    """
    return GROUPS_ZH if active_provider(config) == "chinese_clip_onnx" else GROUPS

# Groups whose signal is too weak to present as fact. The UI appends a caveat
# rather than hiding them, because "全都不给" is worse than "给了但标注不准".
LOW_CONFIDENCE_GROUPS = frozenset({"height"})

# A photo that is mostly scenery/fabric still scores high on some colour prompt,
# so person-flavoured queries should also require the 有人 label.
_PERSON_GATED_GROUPS = frozenset({"color", "build", "height", "style", "clothing"})

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
    ("街头风", "style", ("街头风",)),
    ("街头", "style", ("街头风",)),
    ("运动风", "style", ("运动风",)),
    ("运动", "style", ("运动风",)),
    ("老钱风", "style", ("老钱风",)),
    ("老钱", "style", ("老钱风",)),
    ("静奢风", "style", ("老钱风",)),
    ("静奢", "style", ("老钱风",)),
    ("低调奢华", "style", ("老钱风",)),
    ("工装风", "style", ("工装风",)),
    ("工装", "style", ("工装风",)),
    ("学院风", "style", ("学院风",)),
    ("学院", "style", ("学院风",)),
    ("商务休闲", "style", ("商务休闲",)),
    ("smart casual", "style", ("商务休闲",)),
    # 服装单品
    ("西装外套", "clothing", ("西装外套", "正装")),
    ("西裤", "clothing", ("西裤",)),
    ("衬衫", "clothing", ("衬衫",)),
    ("衬衣", "clothing", ("衬衫",)),
    ("polo衫", "clothing", ("Polo衫",)),
    ("polo", "clothing", ("Polo衫",)),
    ("t恤", "clothing", ("T恤",)),
    ("t裇", "clothing", ("T恤",)),
    ("短袖", "clothing", ("T恤",)),
    ("毛衣", "clothing", ("毛衣",)),
    ("针织衫", "clothing", ("毛衣",)),
    ("卫衣", "clothing", ("卫衣",)),
    ("连帽衫", "clothing", ("卫衣",)),
    ("夹克", "clothing", ("夹克",)),
    ("外套", "clothing", ("夹克",)),
    ("风衣", "clothing", ("风衣",)),
    ("大衣", "clothing", ("大衣",)),
    ("羽绒服", "clothing", ("羽绒服",)),
    ("棉服", "clothing", ("羽绒服",)),
    ("牛仔裤", "clothing", ("牛仔裤",)),
    ("牛仔", "clothing", ("牛仔裤",)),
    ("短裤", "clothing", ("短裤",)),
    ("运动裤", "clothing", ("运动裤",)),
    ("马甲", "clothing", ("马甲",)),
    ("针织开衫", "clothing", ("针织开衫",)),
    ("开衫", "clothing", ("针织开衫",)),
    ("背心", "clothing", ("背心",)),
    ("吊带", "clothing", ("背心",)),
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


def _prompts(config: Any = None) -> list[str]:
    seen: list[str] = []
    for labels in groups_for(config).values():
        for prompts in labels.values():
            for prompt in prompts:
                if prompt not in seen:
                    seen.append(prompt)
    return seen


def active_provider(config: Any = None) -> str:
    """Provider id of the model currently selected (clip_onnx / chinese_clip_onnx / ...)."""
    from src.storage.config_store import get_active_model_profile

    try:
        profile = get_active_model_profile(config=config)
    except Exception:  # noqa: BLE001 - never fail a search on config weirdness
        return "clip_onnx"
    return str(profile.get("provider", "") or "").strip() or "clip_onnx"


def _model_signature(config: Any) -> str:
    from src.storage.config_store import get_active_model_profile

    try:
        profile = get_active_model_profile(config=config)
    except Exception:  # noqa: BLE001
        return ""
    bits = [
        str(profile.get("provider") or ""),
        str(profile.get("id") or ""),
        str((profile.get("runtime") or {}).get("model_variant") or ""),
    ]
    return "|".join(bits)


def _cache_path(config: Any) -> str:
    from src.infra.paths import get_app_data_dir

    digest = hashlib.sha1(
        json.dumps([_prompts(config), _model_signature(config)], ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]
    folder = os.path.join(get_app_data_dir(), "cache", "attribute_prompts")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, f"{digest}.npz")


def _encode_groups(config: Any) -> dict[str, tuple[list[str], np.ndarray]]:
    """Encode the prompt panel, cached on disk so queries cost no model time."""
    from src.core.clip_embedding import get_text_embedding

    path = _cache_path(config)
    prompts = _prompts(config)
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
    for group, labels in groups_for(config).items():
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
