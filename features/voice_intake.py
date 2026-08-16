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

Two ways to build a VoiceSchema:
1. Closure mode (original, used by templates/tour_operator.py): each
   VoiceRecordType carries a hand-written `save` coroutine (and optional
   `on_saved`) that the host template authors itself, and VoiceSchema carries
   a hand-written `get_context_id` coroutine. Full control, but requires
   Python code specific to the host's own tables — only template authors can
   do this today.
2. Data-driven mode (see register_data_driven_schema below): each
   VoiceRecordType instead carries a declarative `table` + `column_map`
   (parsed-field-name -> real DB column name), and VoiceSchema carries an
   optional `context_table`/`context_column` pair (or neither, meaning "no
   context gating — always accept"). A single generic `_generic_save`/
   `_generic_get_context_id` pair handles every data-driven record type the
   same way: a parameterized INSERT built from `column_map`, and a
   parameterized SELECT for context resolution. This is what lets a
   from-scratch generated bot (no hand-written Python for this feature at
   all) get voice intake purely from a JSON config produced by
   services/claude_service.py's _generate_voice_cashflow_config and
   validated against the bot's own real CREATE TABLE statements before it's
   ever registered here.

Both modes coexist on the same VoiceRecordType/VoiceSchema dataclasses (each
record type is independently closure-mode or data-driven — a schema is never
"mixed" in practice, but nothing here prevents it) and are dispatched by the
SAME on_voice/vs_save handlers below, which check which mode a given
VoiceRecordType/VoiceSchema uses at call time.

No init_db here for closure-mode host tables (this module never owns a
template's own tables) — data-driven mode also never creates tables: `table`
in a generated schema must always name a REAL, already-existing table
(validated at generation time), never one this module creates itself.

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
import aiosqlite
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
    """Reproduces templates/tour_operator.py's own _is_admin() read-path —
    the `{"ids": [...]}` shape with string ids (db.database.sync_bot_admins_json's
    format, same as features/sellable_items.py's admins_file convention).
    The original on_voice handler gated on this before doing anything else;
    that gate moved here so it isn't lost in the extraction.

    Deliberately does NOT reproduce _is_admin's self-registration side effect
    (auto-writing the caller as the sole admin the first time an empty admins
    list is seen) — that bootstrap belongs to /start, the user's first-ever
    interaction with the bot, not to a voice message that could arrive from
    anyone. An empty/missing/unreadable admins_file here fails CLOSED (no one
    is admin yet), leaving the actual bootstrap to happen the normal way via
    /start first."""
    if not admins_file:
        return True
    try:
        with open(admins_file) as f:
            data = json.load(f)
        admins = data.get("ids", []) if isinstance(data, dict) else data
    except Exception:
        return False
    return str(user_id) in admins


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
    save: Callable[[str, Any, dict], Awaitable[Any]] | None = None
        # (db_path, context_id, parsed_data) -> new row id or None
        # Closure mode only — exactly one of `save`/`table` must be set (see
        # module docstring's "Two ways to build a VoiceSchema").
    on_saved: Callable[[str, int, Any], Awaitable[None]] | None = None
        # (db_path, user_id, save_result) -> None, optional post-save hook
    table: str | None = None
        # Data-driven mode only — a real table name in the host bot's own
        # db_path, verified (by the caller that builds this dataclass, e.g.
        # register_data_driven_schema below) against that bot's actual
        # CREATE TABLE statements before this dataclass is ever constructed.
        # This module trusts `table`/`column_map` are already validated by
        # the time they reach here — it never re-derives or re-checks them
        # against the live schema itself (no schema-introspection dependency
        # in the hot voice-message path).
    column_map: dict[str, str] | None = None
        # Data-driven mode only — parsed-field-name (VoiceFieldSpec.name) ->
        # real column name on `table`. Every key here should have a matching
        # VoiceFieldSpec.name (fields with no entry are parsed but dropped,
        # never inserted) and every value must be a real column on `table` —
        # same validation contract as `table` above.
    context_column: str | None = None
        # Data-driven mode only — the column on `table` that stores this
        # record type's own context/parent id (tour_operator's `tour_id`
        # equivalent). None means "no context column on this record type"
        # (e.g. a top-level record like tour_operator's own `new_tour`,
        # which CREATES a context rather than belonging to one).


@dataclass
class VoiceSchema:
    record_types: list[VoiceRecordType]
    get_context_id: Callable[[str, int], Awaitable[Any]] | None = None
        # (db_path, user_id) -> context id or None. Closure mode only —
        # exactly one of `get_context_id`/(`context_table`+`context_column`)
        # must be set, unless the bot has no context concept at all (both
        # unset — see _resolve_context_id's "no context required" branch).
    no_context_message: str = ""
    language_code: str = "ru"
    context_table: str | None = None
        # Data-driven mode only — the table holding this bot's own
        # "currently active X per user" state (tour_operator's `user_prefs`
        # equivalent), e.g. "user_prefs". None (with context_column also
        # None) means this bot has no such concept — every data-driven
        # record type is accepted unconditionally, with context_id=None
        # passed to _generic_save (fine as long as no record type in this
        # schema declares its own `context_column`, since there'd be nothing
        # to gate against).
    context_column: str | None = None
        # Data-driven mode only — the column on context_table holding the
        # active context id, keyed by a user-id column (see
        # _CONTEXT_USER_ID_COLUMN_CANDIDATES in _generic_get_context_id for
        # how that column is located).


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


# ── Data-driven mode: generic save / context resolution ─────────────────────
#
# Column/table identifiers here are NEVER taken from parsed voice-message
# text or Claude's own extraction output — they only ever come from
# VoiceRecordType.table/column_map/context_column and VoiceSchema.
# context_table/context_column, which register_data_driven_schema builds
# exclusively from an already-validated config dict (validated against the
# bot's own real CREATE TABLE statements at generation time in
# services/claude_service.py, and re-checked defensively in
# _validate_data_driven_config below). Only VALUES are parameterized
# user/LLM-derived data — identifiers are always drawn from that trusted
# closure state, never string-formatted from request data, so there is no
# SQL-injection path through this code even though table/column names can't
# be parameterized by DB-API placeholders the way values can.

# Candidate column names user_prefs-style context tables use to key their
# "active X" row by Telegram user id — same convention tour_operator.py's own
# user_prefs table uses ("user_id" as TEXT, keyed by str(uid)). Tried in
# order; the first one present on context_table's real columns wins. This
# module never invents a name outside this fixed list.
_CONTEXT_USER_ID_COLUMN_CANDIDATES = ("user_id", "telegram_user_id", "owner_user_id")


async def _generic_save(db_path: str, context_id: Any, data: dict, rt: VoiceRecordType) -> int | None:
    """Data-driven counterpart to a host's own hand-written `save` closure —
    builds and runs one parameterized INSERT from rt.table/rt.column_map.
    Only columns present in rt.column_map are ever written (fields the
    schema didn't map are silently dropped, same as a closure that simply
    doesn't reference them); rt.context_column (if set) is added as an extra
    INSERT column carrying context_id, mirroring how tour_operator's own
    closures thread tour_id through positionally."""
    columns: list[str] = []
    values: list[Any] = []
    for field_name, column_name in (rt.column_map or {}).items():
        if field_name in data:
            columns.append(column_name)
            values.append(data[field_name])
    if rt.context_column and context_id is not None:
        columns.append(rt.context_column)
        values.append(context_id)
    if not columns:
        # Nothing to write (schema declared no fields with data, or the
        # parsed record was empty) — same "no-op, no crash" posture as a
        # closure that would otherwise INSERT an all-NULL row.
        return None
    placeholders = ", ".join("?" for _ in columns)
    column_list = ", ".join(columns)
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            f"INSERT INTO {rt.table} ({column_list}) VALUES ({placeholders})",
            values,
        )
        await db.commit()
        return cur.lastrowid


async def _generic_get_context_id(db_path: str, user_id: int, schema: VoiceSchema) -> Any | None:
    """Data-driven counterpart to a host's own hand-written `get_context_id`
    closure — looks up schema.context_column on schema.context_table for the
    row matching this user, trying each candidate user-id column name in
    turn. Returns None (meaning "show no_context_message") if no matching
    row/column is found, exactly like a closure returning None for "no
    active tour yet"."""
    if not schema.context_table or not schema.context_column:
        return None
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(f"PRAGMA table_info({schema.context_table})") as c:
            columns = {row["name"] async for row in c}
        user_id_column = next(
            (c for c in _CONTEXT_USER_ID_COLUMN_CANDIDATES if c in columns), None
        )
        if user_id_column is None:
            logger.warning(
                f"_generic_get_context_id: context_table={schema.context_table!r} has none of "
                f"{_CONTEXT_USER_ID_COLUMN_CANDIDATES} — cannot resolve context"
            )
            return None
        async with db.execute(
            f"SELECT {schema.context_column} FROM {schema.context_table} WHERE {user_id_column} = ?",
            (str(user_id),),
        ) as c:
            row = await c.fetchone()
            return row[0] if row else None


async def _resolve_context_id(db_path: str, user_id: int, schema: VoiceSchema) -> Any | None:
    """Single entry point on_voice() calls, dispatching to whichever mode
    this schema uses. `get_context_id` (closure mode) takes priority if both
    are somehow set (shouldn't happen — register_data_driven_schema never
    sets it). Neither set means "no context concept for this bot" — returns
    a sentinel-free None that callers must NOT treat as "context missing";
    see on_voice's own has_context_concept check below, which is what
    actually decides whether a None here blocks saving."""
    if schema.get_context_id is not None:
        return await schema.get_context_id(db_path, user_id)
    if schema.context_table and schema.context_column:
        return await _generic_get_context_id(db_path, user_id, schema)
    return None


def _schema_has_context_concept(schema: VoiceSchema) -> bool:
    """True if this schema expects SOME context to be resolved before saving
    (closure mode always does; data-driven mode only if context_table/
    context_column are both set) — used by on_voice to distinguish "no
    context id was found for this user" (blocks saving, shows
    no_context_message) from "this bot doesn't use context at all" (never
    blocks)."""
    return schema.get_context_id is not None or bool(schema.context_table and schema.context_column)


# ── register_data_driven_schema: builds a VoiceSchema from a validated dict ──
#
# Input shape (see services/claude_service.py's _generate_voice_cashflow_config
# for how it's produced and validated against real CREATE TABLE statements
# BEFORE it ever reaches here):
#   {
#     "context_table": str | None, "context_column": str | None,
#     "record_types": [
#       {"key": str, "label": str, "icon": str, "prompt_desc": str,
#        "table": str, "context_column": str | None,
#        "fields": [{"name": str, "label": str, "column": str}, ...]},
#       ...
#     ],
#   }
def register_data_driven_schema(bot_id: int, config: dict, *, language_code: str = "ru") -> None:
    """Builds a VoiceSchema entirely from `config` (data-driven mode) and
    registers it via register_schema — the from-scratch counterpart to a
    template hand-calling register_schema(bot_id, _build_voice_schema())
    itself. Called by runtime/registry.py's _load_and_include_features when
    a bot has the voice_intake feature enabled AND a generated
    bot_voice_cashflow_config row with a non-null voice_intake section.

    Trusts `config` is already validated (real table/column names,
    non-empty record_types) — see _validate_data_driven_config below, which
    every caller MUST run first; this function itself does no validation and
    will raise/build a broken schema on malformed input, same "trust the
    caller already checked" posture VoiceRecordType's own docstring states."""
    record_types = []
    for rt_cfg in config.get("record_types", []):
        fields = [
            VoiceFieldSpec(name=f["name"], label=f["label"]) for f in rt_cfg.get("fields", [])
        ]
        column_map = {f["name"]: f["column"] for f in rt_cfg.get("fields", [])}
        record_types.append(VoiceRecordType(
            key=rt_cfg["key"],
            label=rt_cfg["label"],
            icon=rt_cfg.get("icon", "🎙️"),
            prompt_desc=rt_cfg["prompt_desc"],
            fields=fields,
            table=rt_cfg["table"],
            column_map=column_map,
            context_column=rt_cfg.get("context_column"),
        ))
    schema = VoiceSchema(
        record_types=record_types,
        no_context_message="⚠️ Нет активной записи для привязки. Создайте новую запись сначала.",
        language_code=language_code,
        context_table=config.get("context_table"),
        context_column=config.get("context_column"),
    )
    register_schema(bot_id, schema)


def _validate_data_driven_config(config: dict, tables: dict[str, set[str]]) -> bool:
    """Defensive second check against the bot's own real tables (same
    `tables` shape services/claude_service.py's own
    _extract_create_table_names returns: table name -> set of column names)
    — mirrors _validate_miniapp_config_against_code's "never trust a single
    validation pass" posture, since this runs again at every registry
    load/reload, not just once at generation time (the underlying bot file
    can be regenerated/edited between those two points). Any mismatch fails
    the whole config, same all-or-nothing posture as miniapp_config."""
    record_types = config.get("record_types")
    if not isinstance(record_types, list) or not record_types:
        return False
    context_table = config.get("context_table")
    context_column = config.get("context_column")
    if context_table is not None:
        if context_table not in tables:
            return False
        if context_column is not None and context_column not in tables[context_table]:
            return False
    for rt_cfg in record_types:
        if not isinstance(rt_cfg, dict):
            return False
        for required_key in ("key", "label", "prompt_desc", "table", "fields"):
            if required_key not in rt_cfg:
                return False
        table = rt_cfg["table"]
        if table not in tables:
            return False
        columns = tables[table]
        fields = rt_cfg["fields"]
        if not isinstance(fields, list) or not fields:
            return False
        for f in fields:
            if not isinstance(f, dict) or "name" not in f or "label" not in f or "column" not in f:
                return False
            if f["column"] not in columns:
                return False
        rt_context_column = rt_cfg.get("context_column")
        if rt_context_column is not None and rt_context_column not in columns:
            return False
    return True


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
    context_id = await _resolve_context_id(config.db_path, m.from_user.id, schema)
    if context_id is None and _schema_has_context_concept(schema):
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
    if rt.save is not None:
        result = await rt.save(config.db_path, context_id, data)
    else:
        result = await _generic_save(config.db_path, context_id, data, rt)
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
