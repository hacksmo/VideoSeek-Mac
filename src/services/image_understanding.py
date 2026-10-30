"""Structured image description for the image library.

Why a separate module
---------------------
The video pipeline already has a caption component
(``src/core/understanding/components/remote_vl_caption.py``), but its prompt is
a free-form one-liner and its tags go through
``src/services/understanding_tags.py``, which deliberately drops
``男人 / 女人 / person / people`` as non-discriminative fillers. That is the
right call for shot tagging and exactly the wrong call for "describe the people
in this photo", so the image library asks the same VLM server for a **structured
JSON** payload instead and keeps ``people`` as its own field.

Model source: whatever is configured under ``understanding.remote_vlm``
(LM Studio / Ollama / any OpenAI-compatible cloud endpoint). No extra
dependency, no new model download.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Mapping, Sequence

import numpy as np

from src.app.logging_utils import get_logger

logger = get_logger("image_understanding")

# Hard limits so a chatty model cannot blow up a row.
MAX_CAPTION_CHARS = 200
MAX_PEOPLE = 12
MAX_TAGS = 12
MAX_TAG_CHARS = 24

_PERSON_TEXT_FIELDS = (
    "gender",
    "age",
    "clothing",
    "build",
    "style",
    "action",
    "position",
    "features",
)

_PROMPT_ZH = """你是图像标注员。只描述画面上**看得见**的内容，输出严格 JSON，不要解释、不要 Markdown 代码块。

格式：
{
  "caption": "一句话概括整张图（不超过 60 字）",
  "people": [
    {
      "count": 1,
      "gender": "女|男|不确定",
      "age": "儿童|少年|青年|中年|老年|不确定",
      "clothing": "主要服饰与颜色，例如 米色针织衫配白裤",
      "build": "偏胖|微胖|匀称|偏瘦|不确定",
      "style": "花哨|素净|正装|休闲|运动|不确定",
      "action": "正在做什么，例如 站在窗边看手机",
      "position": "画面左侧|画面中间|画面右侧|背景",
      "features": "其它可辨识特征：发型、眼镜、帽子、口罩、背包等"
    }
  ],
  "tags": ["最多 8 个关键词：场景、物品、风格、衣着颜色"]
}

规则：
- 画面里没有人时，people 必须是空数组。
- 多人同行可合并成一条并写 count；特征明显不同的人分开写。
- 不推测姓名、身份、职业、情绪、关系，不评价美丑，不评判身材好坏。
- build 只按可见轮廓中性归类；看不到全身或穿宽松外套时写 "不确定"。
- 看不清的字段写 "不确定"，不要编造。
- tags 用短词，不要整句。"""

_PROMPT_EN = """You are an image annotator. Describe only what is visible in the image and reply with strict JSON, no prose and no code fences.

Shape:
{
  "caption": "one sentence for the whole image, <= 40 words",
  "people": [
    {
      "count": 1,
      "gender": "female|male|uncertain",
      "age": "child|teen|young adult|middle-aged|senior|uncertain",
      "clothing": "main garments and colours, e.g. beige knit sweater with white trousers",
      "build": "heavy|plump|average|slim|uncertain",
      "style": "flashy|plain|formal|casual|sporty|uncertain",
      "action": "what they are doing, e.g. standing by the window looking at a phone",
      "position": "left|center|right|background",
      "features": "other visible cues: hair, glasses, hat, mask, backpack"
    }
  ],
  "tags": ["at most 8 short keywords: scene, objects, style, clothing colours"]
}

Rules:
- Empty people array when nobody is visible.
- Merge a group of similar people into one entry with count; split clearly different people.
- Never guess names, identity, occupation, mood or relationships. Do not rate looks or judge body shape.
- build is a neutral silhouette call; use "uncertain" for a head-and-shoulders crop or a loose coat.
- Use "uncertain" for anything you cannot see; do not invent.
- Tags are short words, not sentences."""


def build_image_prompt(language: str = "zh", *, extra: str = "") -> str:
    text = _PROMPT_EN if str(language or "").lower().startswith("en") else _PROMPT_ZH
    suffix = str(extra or "").strip()
    return f"{text}\n\n{suffix}" if suffix else text


def _strip_code_fences(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    fence = re.search(r"```(?:json)?\s*(.+?)```", raw, re.DOTALL | re.IGNORECASE)
    if fence:
        return fence.group(1).strip()
    return raw


def _extract_json_object(text: str) -> dict | None:
    raw = _strip_code_fences(text)
    if not raw:
        return None
    for candidate in (raw, raw[raw.find("{") : raw.rfind("}") + 1] if "{" in raw else ""):
        if not candidate:
            continue
        try:
            value = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(value, dict):
            return value
    return None


def _clean_text(value: Any, *, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def _coerce_count(value: Any) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return 1
    return max(1, min(99, number))


def _normalize_person(item: Mapping[str, Any]) -> dict | None:
    person = {"count": _coerce_count(item.get("count", 1))}
    filled = False
    for field in _PERSON_TEXT_FIELDS:
        text = _clean_text(item.get(field), limit=60)
        person[field] = text
        filled = filled or bool(text)
    face_visible = item.get("face_visible")
    if isinstance(face_visible, bool):
        person["face_visible"] = face_visible
    return person if filled else None


def parse_image_description(raw_text: str) -> dict:
    """Turn a VLM reply into ``{caption, people, tags, parse_ok}``.

    A model that ignores the JSON instruction still yields a usable caption:
    the whole reply becomes the caption and ``parse_ok`` is False.
    """
    payload = _extract_json_object(raw_text)
    fallback_caption = _clean_text(_strip_code_fences(raw_text), limit=MAX_CAPTION_CHARS)
    if payload is None:
        return {"caption": fallback_caption, "people": [], "tags": [], "parse_ok": False}

    caption = _clean_text(payload.get("caption") or payload.get("description"), limit=MAX_CAPTION_CHARS)
    if not caption:
        caption = fallback_caption

    people: list[dict] = []
    raw_people = payload.get("people")
    if isinstance(raw_people, list):
        for item in raw_people[:MAX_PEOPLE]:
            if isinstance(item, Mapping):
                person = _normalize_person(item)
                if person:
                    people.append(person)
            elif isinstance(item, str) and item.strip():
                # Tolerate ["a woman in red"] style replies.
                people.append(
                    {
                        "count": 1,
                        "gender": "",
                        "age": "",
                        "clothing": "",
                        "action": "",
                        "position": "",
                        "features": _clean_text(item, limit=60),
                    }
                )

    tags: list[str] = []
    raw_tags = payload.get("tags")
    if isinstance(raw_tags, list):
        for tag in raw_tags:
            text = _clean_text(tag, limit=MAX_TAG_CHARS)
            if text and text not in tags:
                tags.append(text)
            if len(tags) >= MAX_TAGS:
                break

    return {"caption": caption, "people": people, "tags": tags, "parse_ok": True}


def people_to_text(people: Sequence[Mapping[str, Any]]) -> str:
    """Flatten people entries into one searchable line."""
    parts: list[str] = []
    for person in people or []:
        bits = [
            str(person.get(field) or "").strip()
            for field in _PERSON_TEXT_FIELDS
        ]
        text = " ".join(bit for bit in bits if bit)
        if not text:
            continue
        count = int(person.get("count") or 1)
        parts.append(f"{count}人 {text}" if count > 1 else text)
    return " | ".join(parts)


def describe_image_bgr(
    image_bgr: np.ndarray,
    *,
    config=None,
    prompt: str | None = None,
    should_stop_callback: Callable[[], bool] | None = None,
) -> dict:
    """Call the configured VLM once and return a normalized description."""
    from src.core.understanding.components.remote_vl_caption import (
        _encode_image_jpeg_base64,
        limit_image_long_edge,
        resolve_vlm_upload_limits,
    )
    from src.services.cancellable_http import urlopen_json_with_stop
    from src.services.understanding_resource_service import (
        build_remote_vlm_auth_headers,
        get_remote_vlm_settings,
    )

    if image_bgr is None or getattr(image_bgr, "size", 0) == 0:
        raise RuntimeError("Cannot describe an empty image.")

    if config is None:
        from src.app.config import load_config

        config = load_config()

    settings = get_remote_vlm_settings(config)
    base_url = str(settings.get("base_url") or "").strip().rstrip("/")
    if not base_url:
        raise RuntimeError("remote VLM base_url is not configured")
    if not base_url.endswith("/v1"):
        base_url = f"{base_url}/v1"
    model = str(settings.get("model") or "").strip()
    if not model:
        raise RuntimeError("remote VLM model is not configured")

    if should_stop_callback and should_stop_callback():
        raise RuntimeError("Image indexing stopped by user")

    language = str(settings.get("caption_language") or "zh")
    prompt_text = prompt or build_image_prompt(language)
    max_long_edge, jpeg_quality = resolve_vlm_upload_limits(settings, {})
    upload_bgr = limit_image_long_edge(image_bgr, max_long_edge)
    image_b64 = _encode_image_jpeg_base64(upload_bgr, jpeg_quality=jpeg_quality)

    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt_text},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
                ],
            }
        ],
        # A group photo with build/style on every person runs past 576 tokens,
        # and a truncated reply parses as plain text -- i.e. the description is
        # silently lost. Cap generously; the model stops when the JSON closes.
        "max_tokens": max(384, min(1024, int(settings.get("max_tokens", 192) or 192) * 4)),
        "temperature": 0.1,
    }
    prefill = _json_prefill(settings)
    if prefill:
        payload["messages"].append({"role": "assistant", "content": prefill})

    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **build_remote_vlm_auth_headers(settings)},
        method="POST",
    )
    timeout_sec = float(settings.get("timeout_sec", 120) or 120)
    try:
        body = urlopen_json_with_stop(
            request,
            timeout_sec=timeout_sec,
            should_stop_callback=should_stop_callback,
            stop_message="Image indexing stopped by user",
        )
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"remote VLM HTTP {exc.code}: {detail[:300]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"remote VLM unreachable at {base_url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise RuntimeError(f"remote VLM timed out after {timeout_sec:.0f}s") from exc

    choices = body.get("choices") or []
    if not choices:
        raise RuntimeError("remote VLM returned no choices")
    message = choices[0].get("message") or {}
    text = str(message.get("content") or message.get("reasoning_content") or "").strip()
    if not text:
        raise RuntimeError("remote VLM returned empty content")
    if prefill and not text.lstrip().startswith("{"):
        # The seeded brace is echoed back by some servers and stripped by
        # others; put it back only when the reply starts mid-object.
        text = prefill + text
    return parse_image_description(text)


def _json_prefill(settings: Mapping[str, Any]) -> str:
    """Seed the assistant turn so a thinking model skips its reasoning block.

    Ollama's ``qwen3-vl`` ships the ``qwen3-vl-thinking`` renderer, which emits a
    think block before answering. ``think: false`` is ignored there (checked on
    0.40.13, native and OpenAI routes alike), so with a normal token budget the
    reply is *all* reasoning and ``content`` comes back empty. Handing the model
    an opening brace as its first assistant token makes it continue straight
    into the JSON: 15-25 s per image becomes ~3 s.
    """
    if str(settings.get("provider_preset") or "") != "ollama":
        return ""
    return "{"


def _endpoint_reachable(base_url: str, *, timeout_sec: float = 0.35) -> tuple[bool, str]:
    """Cheap TCP check so status can tell "configured" from "running"."""
    import socket
    from urllib.parse import urlparse

    try:
        parsed = urlparse(base_url if "//" in base_url else f"http://{base_url}")
    except ValueError:
        return False, ""
    host = parsed.hostname or ""
    if not host:
        return False, ""
    port = int(parsed.port or (443 if parsed.scheme == "https" else 80))
    try:
        with socket.create_connection((host, port), timeout=timeout_sec):
            return True, ""
    except OSError as exc:
        return False, f"{host}:{port} 不可达（{exc.__class__.__name__}）"


def vlm_availability(config=None, *, probe: bool = False) -> tuple[bool, str]:
    """``(ready, reason)`` for UI/CLI pre-flight checks.

    ``probe=True`` also checks that the endpoint accepts a TCP connection, which
    separates "settings look fine" from "the server is actually running".
    """
    from src.services.understanding_resource_service import get_remote_vlm_settings

    if config is None:
        from src.app.config import load_config

        config = load_config()
    try:
        settings = get_remote_vlm_settings(config)
    except Exception as exc:  # pragma: no cover - defensive
        return False, f"VLM settings unavailable: {exc}"
    if not str(settings.get("model") or "").strip():
        return False, "未配置视觉模型（设置 → 理解 → 模型服务 → VLM → 模型名）"
    base_url = str(settings.get("base_url") or "").strip()
    if not base_url:
        return False, "未配置视觉模型 base_url"
    if probe:
        reachable, detail = _endpoint_reachable(base_url)
        if not reachable:
            return False, f"已配置 {base_url}，但服务{detail}；请先启动 LM Studio / Ollama"
    return True, ""
