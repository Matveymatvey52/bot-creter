"""Gemini summarization service for channel_monitor (userbot product).

Separate from services/claude_service.py — a different provider, a different
key (GEMINI_API_KEY, not ANTHROPIC_API_KEY), and a much narrower job: turn one
channel post's text into a short summary. See
docs/USERBOT_CHANNEL_MONITOR_DESIGN.md §5.

Uses the official `google-genai` SDK (requirements.txt). google-genai==0.3.0
pins `pydantic<3.0.0,>=2.0.0`, which is compatible with aiogram 3.13's
`pydantic<2.9,>=2.4.1` pin also in requirements.txt (pip resolves both to the
same 2.8.x install) — confirmed no conflict when installed together.

Data-boundary contract (design doc §5, security-relevant): summarize_post()
receives ONLY the post's text. It must never be called with session_string,
phone, api_id/api_hash, or anything else that identifies the userbot client
account — callers (runtime/userbot_worker.py) are responsible for that
boundary; this module has no access to those secrets at all.
"""
from __future__ import annotations

import asyncio
import json
import logging

from config import GEMINI_API_KEY

logger = logging.getLogger(__name__)

_MODEL = "gemini-1.5-flash"
_TIMEOUT_SECONDS = 20
_MAX_INPUT_CHARS = 8000  # generous bound; keeps request size/cost predictable

_SUMMARY_PROMPT = (
    "Кратко суммируй следующий пост из Telegram-канала в 1-2 предложениях "
    "на русском языке, сохраняя ключевые факты. Не добавляй ничего от себя, "
    "кроме самой сути поста.\n\n"
)

_EXTRACT_PROMPT_HEADER = (
    "Извлеки из следующего поста Telegram-канала значения полей: {fields}.\n"
    "Верни СТРОГО валидный JSON-объект (никакого markdown, никаких ```-обёрток, "
    "никакого текста до или после) с ключами ровно этими названиями полей "
    "(на русском, как указано). Если поле не найдено в тексте — используй "
    "пустую строку \"\" как значение, не пропускай ключ.\n\nПост:\n"
)

_client = None


def _get_client():
    """Lazily builds and caches the genai Client — mirrors services/claude_service.py's
    module-level `client`, but lazy (not at import time) so importing this
    module doesn't hard-require GEMINI_API_KEY for code paths that never call
    summarize_post (matches the graceful-degradation contract below: a
    missing key degrades at call time, not at import time)."""
    global _client
    if _client is None:
        from google import genai  # imported lazily too, so a missing/broken
        # install of the optional SDK only breaks summarize_post(), not every
        # module that transitively imports this one.
        _client = genai.Client(api_key=GEMINI_API_KEY)
    return _client


async def summarize_post(text: str) -> str | None:
    """Summarizes one channel post's text via Gemini.

    Graceful degradation, per design doc §5: any failure (missing API key,
    network error, timeout, SDK error, unexpected response shape) returns
    None instead of raising — callers leave channel_posts.summary NULL and
    the feed falls back to raw text. This must never block or crash the
    ingestion pipeline that calls it.
    """
    if not text or not text.strip():
        return None
    if not GEMINI_API_KEY:
        logger.warning("summarize_post: GEMINI_API_KEY is not set — skipping summarization (degraded mode)")
        return None

    prompt = _SUMMARY_PROMPT + text[:_MAX_INPUT_CHARS]

    try:
        client = _get_client()
        response = await asyncio.wait_for(
            client.aio.models.generate_content(model=_MODEL, contents=prompt),
            timeout=_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning(f"summarize_post: Gemini API call timed out after {_TIMEOUT_SECONDS}s")
        return None
    except Exception as e:
        # Any SDK/network/auth error — degrade, don't raise. Never log post
        # text itself (kept out even though it isn't secret, to avoid noisy
        # logs regardless of content).
        logger.warning(f"summarize_post: Gemini API call failed: {type(e).__name__}: {e}")
        return None

    try:
        summary = (getattr(response, "text", None) or "").strip()
        return summary or None
    except (AttributeError, TypeError) as e:
        logger.warning(f"summarize_post: unexpected Gemini response shape: {type(e).__name__}: {e}")
        return None


async def extract_structured(text: str, fields: list[str]) -> dict | None:
    """Extracts a JSON object {field: value, ...} for `fields` out of one
    channel post's text — the channel_aggregator template's structured mode
    (docs/CHANNEL_AGGREGATOR_TEMPLATE_DESIGN.md §3.2), used instead of/
    alongside summarize_post() for channels with a configured extract_schema.

    Same graceful-degradation contract as summarize_post: any failure
    (missing key, network/timeout, malformed JSON in the response, unexpected
    shape) returns None and only logs a warning — must never crash
    runtime/userbot_worker.py's background extraction sweep. Never receives
    session_string/phone/api credentials, same data-boundary contract as
    summarize_post (module docstring)."""
    if not text or not text.strip():
        return None
    if not fields:
        return None
    if not GEMINI_API_KEY:
        logger.warning("extract_structured: GEMINI_API_KEY is not set — skipping extraction (degraded mode)")
        return None

    prompt = _EXTRACT_PROMPT_HEADER.format(fields=", ".join(fields)) + text[:_MAX_INPUT_CHARS]

    try:
        client = _get_client()
        response = await asyncio.wait_for(
            client.aio.models.generate_content(model=_MODEL, contents=prompt),
            timeout=_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning(f"extract_structured: Gemini API call timed out after {_TIMEOUT_SECONDS}s")
        return None
    except Exception as e:
        logger.warning(f"extract_structured: Gemini API call failed: {type(e).__name__}: {e}")
        return None

    try:
        raw = (getattr(response, "text", None) or "").strip()
    except (AttributeError, TypeError) as e:
        logger.warning(f"extract_structured: unexpected Gemini response shape: {type(e).__name__}: {e}")
        return None
    if not raw:
        return None
    # Models sometimes wrap JSON in ```json ... ``` despite the instruction not
    # to — strip a leading/trailing fence defensively before parsing, rather
    # than failing the whole extraction over formatting noise.
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning(f"extract_structured: response was not valid JSON: {e}")
        return None
    if not isinstance(parsed, dict):
        logger.warning("extract_structured: response JSON was not an object — discarding")
        return None
    return parsed
