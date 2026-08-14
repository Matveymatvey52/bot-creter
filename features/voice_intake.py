# FEATURE: voice_intake
# COMPATIBLE_WITH: tour_operator
"""Reusable "voice message -> structured record" feature module — extracted
from templates/tour_operator.py, which originally had this entire mechanism
(AssemblyAI transcription, Claude-based parsing into a typed record, a
confirmation card, and a save/on_saved dispatch) hardcoded for its own 6
record types (new_tour/location/program/hotel/guest/dds).

Unlike features/payments.py (one shared table shape for every host) or
features/sheets.py (no host-specific state at all), this module needs to
know a HOST-SPECIFIC SCHEMA: which record types exist, what fields each one
has, how to save each one into the host's own tables, and how to resolve a
"context id" (e.g. tour_operator's "currently active tour") that a parsed
record attaches to. That schema is supplied by the host template itself via
register_schema(bot_id, schema) — called once the host's per-bot config
(and therefore its db_path/tables) is known, mirroring how other bot_id-keyed
state works in this codebase (see features/sellable_items.py's per-bot_id
admin/session dicts). The schema is keyed by bot_id specifically because many
bots share this one process/module instance (same reasoning sellable_items.py
gives for its own bot_id-keyed state) — two bots on two different templates
(or the same template configured differently) must never see each other's
record types.

No init_db here: this module never owns a table of its own (same as
sheets.py) — the host's own `save` closures write into the host's own
db_path/tables, exactly like the original save_entry() did directly.

router is a module-level Router with F.voice + the vs_save/vs_edit/vs_cancel
callbacks, picked up by runtime/registry.py's `_load_and_include_features()`
via the by-convention `router` attribute, the same as every other feature
module. `data["bot_id"]` (injected by that same loader's
`_attach_bot_id_middleware`) is what on_voice uses to look up which bot's
schema applies; `data["config"].db_path` (injected by the HOST template's own
ConfigMiddleware, which rides the same Dispatcher) is what every DB read/write
below uses — this module has no config of its own, see VoiceIntakeConfig.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

import aiohttp
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import ANTHROPIC_API_KEY, ASSEMBLYAI_API_KEY

logger = logging.getLogger(__name__)

router = Router()


class VoiceIntakeConfig(Protocol):
    """Any host template Config dataclass with a db_path works — duck-typed,
    same convention as features/payments.py's PaymentsConfig and
    features/sellable_items.py's SellableItemsConfig. admins_file is optional
    (see _is_admin below) — a host with no admin-gating concept simply never
    sets it and every user is treated as allowed, same as a template with no
    admin file at all."""
    db_path: str
    admins_file: str | None


def _is_admin(user_id: int, admins_file: str | None) -> bool:
    """Reproduces templates/tour_operator.py's own _is_admin() read-path
    (plain JSON list of ids, via json.load — NOT features/sellable_items.py's
    different `{"ids": [...]}` shape, a different admins_file format
    entirely). The original on_voice handler gated on this before doing
    anything else; that gate moved here so it isn't lost in the extraction.

    Deliberately does NOT reproduce the original _is_admin's
    self-registration side effect (auto-writing [uid] as the sole admin the
    first time an empty admins list is seen) — that bootstrap belongs to
    /start, the user's first-ever interaction with the bot, not to a voice
    message that could arrive from anyone. An empty/missing/unreadable
    admins_file here fails CLOSED (no one is admin yet), leaving the actual
    bootstrap to happen the normal way via /start first."""
    if not admins_file:
        return True
    try:
        with open(admins_file) as f:
            admins = json.load(f)
    except Exception:
        return False
    return user_id in admins


# ── Schema registration API ──────────────────────────────────────────────────
@dataclass
class VoiceFieldSpec:
    name: str   # matches the JSON key Claude returns for this field
    label: str  # human label shown on the confirmation card, e.g. "Регион"


@dataclass
class VoiceRecordType:
    key: str                # e.g. "location", "new_tour"
    label: str              # card header, e.g. "ЛиП", "Новый тур"
    icon: str                # e.g. "📍"
    prompt_desc: str         # one prompt line describing this type's fields
    fields: list[VoiceFieldSpec]
    save: Callable[[str, Any, dict], Awaitable[Any]]
        # (db_path, context_id, parsed_data) -> new row id or None
    on_saved: Callable[[str, int, Any], Awaitable[None]] | None = None
        # (db_path, user_id, save_result) -> None, optional post-save hook


@dataclass
class VoiceSchema:
    record_types: list[VoiceRecordType]
    get_context_id: Callable[[str, int], Awaitable[Any]]  # (db_path, user_id) -> context id or None
    no_context_message: str
    language_code: str = "ru"


_schemas: dict[int, VoiceSchema] = {}


def register_schema(bot_id: int, schema: VoiceSchema) -> None:
    """Registers `schema` as the voice-intake schema for `bot_id`. Call this
    from the host template's own setup path (e.g. its init_db(config), which
    already runs once per bot registration/reload — see
    runtime/registry.py's _build_generic_middleware docstring), once that
    bot's own config/db_path is known. Idempotent: re-registering the same
    bot_id simply replaces its schema (safe on reload_one/reload_all)."""
    _schemas[bot_id] = schema


def _lookup_record_type(schema: VoiceSchema, key: str) -> VoiceRecordType | None:
    for rt in schema.record_types:
        if rt.key == key:
            return rt
    return None


# ── Voice -> text (AssemblyAI REST) ──────────────────────────────────────────
async def _transcribe_voice(bot, voice, language_code: str) -> str:
    tg_file = await bot.get_file(voice.file_id)
    # Uses THIS bot's own token (bot.token) — many bots share one process, so
    # a module-level token constant would be wrong for all but one of them.
    audio_url = f"https://api.telegram.org/file/bot{bot.token}/{tg_file.file_path}"
    async with aiohttp.ClientSession() as s:
        async with s.get(audio_url) as r:
            audio_bytes = await r.read()
        async with s.post(
            "https://api.assemblyai.com/v2/upload",
            headers={"authorization": ASSEMBLYAI_API_KEY, "content-type": "audio/ogg"},
            data=audio_bytes,
        ) as r:
            up = await r.json()
        upload_url = up.get("upload_url", "")
        if not upload_url:
            return ""
        async with s.post(
            "https://api.assemblyai.com/v2/transcript",
            headers={"authorization": ASSEMBLYAI_API_KEY, "content-type": "application/json"},
            json={"audio_url": upload_url, "language_code": language_code},
        ) as r:
            tx = await r.json()
        tx_id = tx.get("id", "")
        if not tx_id:
            return ""
        for _ in range(60):
            await asyncio.sleep(3)
            async with s.get(
                f"https://api.assemblyai.com/v2/transcript/{tx_id}",
                headers={"authorization": ASSEMBLYAI_API_KEY},
            ) as r:
                res = await r.json()
            if res.get("status") == "completed":
                return res.get("text", "")
            if res.get("status") == "error":
                return ""
    return ""


# ── Text -> structured data (Claude) ─────────────────────────────────────────
def _build_parse_prompt(schema: VoiceSchema, text: str) -> str:
    lines = [rt for rt in schema.record_types]
    type_lines = "\n".join(f"- {rt.key}: {rt.prompt_desc}" for rt in lines)
    return (
        "Ты помощник, разбирающий голосовые сообщения в структурированные записи. "
        "Разбери текст и верни JSON.\n\n"
        "Тип записи:\n"
        f"{type_lines}\n"
        "- unknown\n\n"
        'Ответ ТОЛЬКО JSON без пояснений:\n'
        '{"type":"...","data":{...},"confidence":0.0}\n\n'
        f"Текст: {text}"
    )


async def _parse_with_claude(schema: VoiceSchema, text: str) -> dict:
    prompt = _build_parse_prompt(schema, text)
    async with aiohttp.ClientSession() as s:
        async with s.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 512,
                "messages": [{"role": "user", "content": prompt}],
            },
        ) as r:
            res = await r.json()
    try:
        raw = res["content"][0]["text"]
        raw = re.sub(r"^```(?:json)?\n?", "", raw.strip())
        raw = re.sub(r"\n?```$", "", raw.strip())
        return json.loads(raw)
    except Exception:
        return {"type": "unknown", "data": {}, "confidence": 0}


# ── FSM state ─────────────────────────────────────────────────────────────────
class VoicePend(StatesGroup):
    confirm = State()


# ── Keyboard/card helpers ────────────────────────────────────────────────────
def _mk(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _btn(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


def _confirm_kb():
    return _mk([[
        _btn("✅ Сохранить", "vs_save"),
        _btn("✏️ Исправить", "vs_edit"),
        _btn("❌ Отменить", "vs_cancel"),
    ]])


def _format_parsed(schema: VoiceSchema, parsed: dict, text: str) -> str:
    t = parsed.get("type", "unknown")
    d = parsed.get("data", {})
    c = parsed.get("confidence", 0)
    rt = _lookup_record_type(schema, t)
    icon = rt.icon if rt else "❓"
    label = rt.label if rt else "Неизвестно"
    field_labels = {f.name: f.label for f in rt.fields} if rt else {}
    snippet = text[:200] + ("..." if len(text) > 200 else "")
    lines = [
        f"🎤 <i>{snippet}</i>",
        f"\n<b>{icon} {label}</b>  <i>{int(c * 100)}% уверенность</i>",
    ]
    for k, v in d.items():
        if v is not None and v != "" and v != 0:
            lines.append(f"  • {field_labels.get(k, k)}: <b>{v}</b>")
    return "\n".join(lines)


# ── Handlers ──────────────────────────────────────────────────────────────────
@router.message(F.voice)
async def on_voice(m: Message, state: FSMContext, config: VoiceIntakeConfig, bot_id: int | None = None):
    if bot_id is None:
        logger.warning("voice_intake.on_voice: no bot_id in handler data — skipped")
        return
    if not _is_admin(m.from_user.id, getattr(config, "admins_file", None)):
        return
    schema = _schemas.get(bot_id)
    if schema is None:
        logger.warning(f"voice_intake.on_voice: bot_id={bot_id} has no registered VoiceSchema — skipped")
        return
    context_id = await schema.get_context_id(config.db_path, m.from_user.id)
    if context_id is None:
        await m.answer(schema.no_context_message)
        return
    sm = await m.answer("🎤 Транскрибирую…")
    text = await _transcribe_voice(m.bot, m.voice, schema.language_code)
    if not text:
        await sm.edit_text("❌ Не удалось распознать речь. Попробуйте ещё раз.")
        return
    await sm.edit_text(f"🤖 Анализирую данные…\n\n<i>«{text[:200]}»</i>", parse_mode="HTML")
    parsed = await _parse_with_claude(schema, text)
    rt = _lookup_record_type(schema, parsed.get("type", "unknown"))
    if parsed.get("type") == "unknown" or rt is None:
        await sm.edit_text(
            f"❓ Не удалось определить тип записи.\n\n<i>«{text}»</i>",
            parse_mode="HTML",
        )
        return
    await state.set_state(VoicePend.confirm)
    await state.update_data(parsed=parsed, context_id=context_id)
    await sm.edit_text(
        _format_parsed(schema, parsed, text),
        parse_mode="HTML",
        reply_markup=_confirm_kb(),
    )


@router.callback_query(VoicePend.confirm, F.data == "vs_save")
async def vs_save(cb: CallbackQuery, state: FSMContext, config: VoiceIntakeConfig, bot_id: int | None = None):
    schema = _schemas.get(bot_id) if bot_id is not None else None
    d = await state.get_data()
    parsed = d.get("parsed", {})
    kind = parsed.get("type")
    data = parsed.get("data", {})
    context_id = d.get("context_id")
    rt = _lookup_record_type(schema, kind) if schema else None
    if rt is None:
        # Schema vanished or record type no longer registered between the
        # confirmation card being shown and "save" being tapped — bail out
        # cleanly rather than crash.
        await state.clear()
        await cb.message.edit_text("❌ Не удалось сохранить: тип записи больше не поддерживается.")
        await cb.answer()
        return
    result = await rt.save(config.db_path, context_id, data)
    await state.clear()
    if rt.on_saved is not None:
        await rt.on_saved(config.db_path, cb.from_user.id, result)
    await cb.message.edit_text(f"✅ {rt.icon} {rt.label} сохранена(о)!")
    await cb.answer("Сохранено!")


@router.callback_query(VoicePend.confirm, F.data.in_({"vs_edit", "vs_cancel"}))
async def vs_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("✏️ Отменено. Отправьте новое голосовое с уточнёнными данными.")
    await cb.answer()
