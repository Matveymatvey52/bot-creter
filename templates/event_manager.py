# TEMPLATE: event_manager
# USE FOR: разовое мероприятие с гостевым списком — свадьба, день рождения, корпоратив, конференция: создание мероприятия, список гостей, самостоятельный RSVP гостя по персональной ссылке, чек-ин на входе, рассылка напоминания
# CUSTOMIZE: sections marked with # CUSTOMIZE
from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import aiosqlite
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove,
)

from db.database import add_bot_admin, remove_bot_admin

# ── CUSTOMIZE ────────────────────────────────────────────────────────────────
# Same status as every other template's CUSTOMIZE block: per-file source-text
# customization Claude edits when generating a specific bot, not per-bot
# runtime state (that's config.db_path/admins_file below). Events/guests
# themselves are runtime data, added/removed via this bot's own admin panel.
BOT_DESCRIPTION = "Учёт мероприятия: список гостей, RSVP по персональной ссылке, чек-ин на входе, рассылка напоминания."
WELCOME_TEXT = (
    "🎉 <b>Организатор мероприятия</b>\n\n"
    "Создавайте мероприятия, добавляйте гостей, следите за RSVP и отмечайте "
    "приход на входе.\n\n"
    "Выберите действие:"
)
GUEST_NEUTRAL_TEXT = (
    "🎉 Этот бот используется для учёта гостей мероприятия.\n\n"
    "Если у вас есть персональная ссылка-приглашение — откройте её."
)
NAME_MAX_LEN = 100
CONTACT_MAX_LEN = 200
LOCATION_MAX_LEN = 200
GUEST_LIMIT_MAX = 100_000
# ── END CUSTOMIZE ─────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
router = Router()

RSVP_LABELS = {"invited": "❔ Не ответил", "confirmed": "✅ Подтвердил", "declined": "❌ Отказался"}
RSVP_FILTERS = ["all", "invited", "confirmed", "declined"]
RSVP_FILTER_LABELS = {"all": "Все", "invited": "Не ответили", "confirmed": "Подтвердили", "declined": "Отказались"}

_DATETIME_RE = re.compile(r"^(\d{1,2})[./](\d{1,2})[./](\d{4})\s+(\d{1,2}):(\d{2})$")


def _esc(value, max_len: int = 500) -> str:
    """HTML-escapes AND length-bounds any user-supplied text before it goes
    into a parse_mode="HTML" message — same helper/rationale as every other
    template's _esc()."""
    text = str(value) if value is not None else ""
    if len(text) > max_len:
        text = text[:max_len] + "…"
    return html.escape(text)


def _join_bounded(lines: list[str], limit: int = 3500) -> str:
    """Joins lines with a length budget, dropping only WHOLE trailing lines —
    same approach as every other template's _join_bounded()."""
    out: list[str] = []
    total = 0
    for line in lines:
        if total + len(line) + 1 > limit:
            out.append("…")
            break
        out.append(line)
        total += len(line) + 1
    return "\n".join(out)


def _short(label: str, max_len: int = 40) -> str:
    return label if len(label) <= max_len else label[:max_len - 1] + "…"


def _valid_telegram_id(text: str) -> bool:
    """Same guard as moderator.py's _valid_admin_id / shop_catalog.py's
    _valid_telegram_id: bounds length before treating input as numeric,
    rejects non-ASCII look-alike digits, and rejects "0"/leading-zero/
    negative forms — no real Telegram user_id is ever 0, negative, or
    leading-zero."""
    if not (bool(text) and text.isascii() and text.isdigit() and len(text) <= 15):
        return False
    return int(text) > 0 and str(int(text)) == text


def _parse_event_dt(raw: str) -> str | None:
    """Accepts 'ДД.ММ.ГГГГ ЧЧ:ММ' (also with '/'), returns ISO
    'YYYY-MM-DD HH:MM' for storage/sorting, or None if unparsable — same
    convention as templates/campaign_tracker.py's _parse_date, extended with
    time-of-day."""
    m = _DATETIME_RE.match(raw.strip())
    if not m:
        return None
    day, month, year, hour, minute = (int(g) for g in m.groups())
    try:
        dt = datetime(year, month, day, hour, minute)
    except ValueError:
        return None
    return dt.strftime("%Y-%m-%d %H:%M")


def _fmt_event_dt(iso: str) -> str:
    return datetime.strptime(iso, "%Y-%m-%d %H:%M").strftime("%d.%m.%Y %H:%M")


# ── config ───────────────────────────────────────────────────────────────────
# Same shape/contract as every other template's Config dataclass — see
# docs/STAGE2_DESIGN.md "Config-контракт шаблона".

@dataclass
class EventManagerConfig:
    bot_name: str
    db_path: str
    admins_file: Path
    welcome_image: Path
    display_name: str | None = None
    group_chat_id: str | None = None
    bot_id: int | None = None
    owner_telegram_id: int | None = None


def _paths_for(name: str, data_dir: Path) -> EventManagerConfig:
    return EventManagerConfig(
        bot_name=name,
        db_path=str(data_dir / f"{name}_data.db"),
        admins_file=data_dir / f"admins_{name}.json",
        welcome_image=data_dir / "bot_images" / f"{name}.jpg",
    )


def config_from_env() -> EventManagerConfig:
    """Standalone/subprocess mode."""
    name = Path(__file__).stem
    data_dir = Path(os.getenv("DATA_DIR", "./data"))
    data_dir.mkdir(exist_ok=True)
    return _paths_for(name, data_dir)


def config_from_bot_row(bot_row: dict, data_dir: Path) -> EventManagerConfig:
    """Webhook runtime mode. Paths keyed by bot_row["bot_id"] (bots.id, the
    physically unique AUTOINCREMENT PK) — NOT bot_row["name"] — same
    reasoning as every other template's config_from_bot_row."""
    bot_id = bot_row["bot_id"]
    config = EventManagerConfig(
        bot_name=bot_row["name"],
        db_path=str(data_dir / f"bot_{bot_id}_data.db"),
        admins_file=data_dir / f"admins_{bot_id}.json",
        welcome_image=data_dir / "bot_images" / f"bot_{bot_id}.jpg",
    )
    config.display_name = bot_row.get("display_name")
    config.group_chat_id = bot_row.get("group_chat_id")
    config.bot_id = bot_id
    config.owner_telegram_id = bot_row.get("owner_telegram_id")
    return config


class ConfigMiddleware(BaseMiddleware):
    """Injects this bot's EventManagerConfig into data["config"]."""

    def __init__(self, config: EventManagerConfig) -> None:
        self.config = config
        super().__init__()

    async def __call__(self, handler, event, data):
        data["config"] = self.config
        return await handler(event, data)


# ── admin helpers ─────────────────────────────────────────────────────────────

def _load_admins(admins_file: Path) -> set:
    try:
        import json
        return set(json.loads(admins_file.read_text()).get("ids", []))
    except Exception:
        return set()

def _save_admins(admins_file: Path, ids: set) -> None:
    import json
    admins_file.write_text(json.dumps({"ids": list(ids)}, ensure_ascii=False))

def _is_bot_admin(user_id: int, config: EventManagerConfig) -> bool:
    # The DB-known owner (bots.owner_telegram_id) is always an admin, even if
    # the local admins_file is empty/stale/hijacked — see cmd_start below for
    # why the file alone can't be trusted as the sole source of truth.
    if config.owner_telegram_id is not None and str(user_id) == str(config.owner_telegram_id):
        return True
    return str(user_id) in _load_admins(config.admins_file)


# ── bot username cache (for building invite deep links) ───────────────────────
# getMe() is cheap and unrate-limited in practice, but there's no reason to
# call it on every single guest-add — cached per Bot.id for the process
# lifetime, same "cache once, keyed by identity" shape as
# runtime/registry.py's _template_module_cache.
_bot_username_cache: dict[int, str] = {}

async def _bot_username(bot: Bot) -> str:
    if bot.id not in _bot_username_cache:
        me = await bot.get_me()
        _bot_username_cache[bot.id] = me.username
    return _bot_username_cache[bot.id]


# ── db ────────────────────────────────────────────────────────────────────────

async def init_db(db_path: str):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                event_dt    TEXT NOT NULL,
                location    TEXT,
                guest_limit INTEGER,
                active      INTEGER NOT NULL DEFAULT 1,
                created_at  TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        # invite_token is a secrets.token_urlsafe(16) opaque string, NOT the
        # guest's own row id — a sequential id in the deep link
        # (t.me/bot?start=g_<id>) would let one guest guess another guest's
        # link and answer RSVP on their behalf. UNIQUE makes token lookup an
        # index hit and doubles as a collision guard (astronomically
        # unlikely at this token length, but INSERT would still fail loudly
        # instead of silently colliding).
        await db.execute("""
            CREATE TABLE IF NOT EXISTS guests (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id          INTEGER NOT NULL REFERENCES events(id),
                name              TEXT NOT NULL,
                contact           TEXT,
                invite_token      TEXT NOT NULL UNIQUE,
                telegram_user_id  INTEGER,
                rsvp_status       TEXT NOT NULL DEFAULT 'invited',
                checked_in        INTEGER NOT NULL DEFAULT 0,
                checked_in_at     TEXT,
                created_at        TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.commit()


miniapp_config = {
    "resources": [
        {
            "name": "events",
            "table": "events",
            "order_by": "event_dt",
            "creatable": True,
            "title": "Мероприятия",
            "titleField": "name",
            "fields": [
                {"name": "name", "required": True, "label": "Название", "kind": "text", "list": True, "detail": True, "create": True},
                {"name": "event_dt", "required": True, "label": "Дата и время", "kind": "date", "list": True, "detail": True, "create": True},
                {"name": "location", "label": "Место", "kind": "text", "list": False, "detail": True, "create": True},
                {"name": "guest_limit", "label": "Лимит гостей", "kind": "number", "list": False, "detail": True, "create": True},
                {"name": "active", "label": "Активно", "kind": "status", "list": True, "detail": True, "create": False},
            ],
        },
        {
            "name": "guests",
            "table": "guests",
            "order_by": "name",
            "creatable": False,
            "title": "Гости",
            "titleField": "name",
            "fields": [
                {"name": "event_id", "required": True, "label": "ID мероприятия", "kind": "number", "list": False, "detail": True, "create": False},
                {"name": "name", "label": "Имя", "kind": "text", "list": True, "detail": True, "create": False},
                {"name": "contact", "label": "Контакт", "kind": "text", "list": False, "detail": True, "create": False},
                {"name": "rsvp_status", "label": "RSVP", "kind": "status", "list": True, "detail": True, "create": False},
                {"name": "checked_in", "label": "Чек-ин", "kind": "status", "list": True, "detail": True, "create": False},
            ],
        },
    ],
}


async def _active_events(db_path: str) -> list[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT * FROM events WHERE active=1 ORDER BY event_dt"
        )).fetchall()
        return [dict(r) for r in rows]

async def _event_row(db_path: str, event_id: int) -> dict | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute("SELECT * FROM events WHERE id=?", (event_id,))).fetchone()
        return dict(row) if row else None

async def _create_event(db_path: str, name: str, event_dt: str, location: str | None, guest_limit: int | None) -> int:
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "INSERT INTO events (name, event_dt, location, guest_limit) VALUES (?,?,?,?)",
            (name, event_dt, location, guest_limit),
        )
        await db.commit()
        return cur.lastrowid

async def _archive_event(db_path: str, event_id: int) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute("UPDATE events SET active=0 WHERE id=?", (event_id,))
        await db.commit()

async def _event_stats(db_path: str, event_id: int) -> dict:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT rsvp_status, COUNT(*) AS n FROM guests WHERE event_id=? GROUP BY rsvp_status", (event_id,)
        )).fetchall()
        counts = {"invited": 0, "confirmed": 0, "declined": 0}
        for r in rows:
            counts[r["rsvp_status"]] = r["n"]
        total = sum(counts.values())
        checked_in = (await (await db.execute(
            "SELECT COUNT(*) FROM guests WHERE event_id=? AND checked_in=1", (event_id,)
        )).fetchone())[0]
        return {"counts": counts, "total": total, "checked_in": checked_in}


async def _guests_for_event(db_path: str, event_id: int, status_filter: str | None = None) -> list[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        if status_filter and status_filter != "all":
            rows = await (await db.execute(
                "SELECT * FROM guests WHERE event_id=? AND rsvp_status=? ORDER BY name", (event_id, status_filter)
            )).fetchall()
        else:
            rows = await (await db.execute(
                "SELECT * FROM guests WHERE event_id=? ORDER BY name", (event_id,)
            )).fetchall()
        return [dict(r) for r in rows]

async def _guest_row(db_path: str, guest_id: int) -> dict | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute("SELECT * FROM guests WHERE id=?", (guest_id,))).fetchone()
        return dict(row) if row else None

async def _create_guest(db_path: str, event_id: int, name: str, contact: str | None) -> dict:
    token = secrets.token_urlsafe(16)
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "INSERT INTO guests (event_id, name, contact, invite_token) VALUES (?,?,?,?)",
            (event_id, name, contact, token),
        )
        await db.commit()
        return {"id": cur.lastrowid, "invite_token": token}

async def _delete_guest(db_path: str, guest_id: int) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute("DELETE FROM guests WHERE id=?", (guest_id,))
        await db.commit()

async def _set_rsvp(db_path: str, guest_id: int, status: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute("UPDATE guests SET rsvp_status=? WHERE id=?", (status, guest_id))
        await db.commit()

async def _toggle_checkin(db_path: str, guest_id: int) -> None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute("SELECT checked_in FROM guests WHERE id=?", (guest_id,))).fetchone()
        if row is None:
            return
        if row["checked_in"]:
            await db.execute("UPDATE guests SET checked_in=0, checked_in_at=NULL WHERE id=?", (guest_id,))
        else:
            await db.execute(
                "UPDATE guests SET checked_in=1, checked_in_at=datetime('now','localtime') WHERE id=?", (guest_id,)
            )
        await db.commit()

async def _claim_guest(db_path: str, token: str, telegram_user_id: int) -> dict | None:
    """Atomically binds telegram_user_id to the guest matching invite_token.

    BEGIN IMMEDIATE + the WHERE ... AND telegram_user_id IS NULL guard makes
    this safe against two concurrent /start taps on the SAME still-unclaimed
    link (only one wins the UPDATE) — same rationale as shop_catalog.py's
    _create_order double-tap guard. If the token is already claimed by a
    DIFFERENT telegram_user_id, the row is returned UNCHANGED — the caller
    compares guest["telegram_user_id"] against the requester to tell "this is
    me reopening my own link" from "someone else's link, already used" and
    reacts accordingly; this function never reassigns an already-claimed
    token, which is exactly what stops a leaked link from being hijacked."""
    async with aiosqlite.connect(db_path, timeout=10) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        row = await (await db.execute("SELECT * FROM guests WHERE invite_token=?", (token,))).fetchone()
        if row is None:
            await db.commit()
            return None
        guest = dict(row)
        if guest["telegram_user_id"] is None:
            await db.execute(
                "UPDATE guests SET telegram_user_id=? WHERE id=? AND telegram_user_id IS NULL",
                (telegram_user_id, guest["id"]),
            )
            guest["telegram_user_id"] = telegram_user_id
        await db.commit()
        return guest


# ── FSM ───────────────────────────────────────────────────────────────────────

class EventFlow(StatesGroup):
    name = State(); date = State(); location = State(); limit = State()

class GuestFlow(StatesGroup):
    name = State(); contact = State()

class AdminFlow(StatesGroup):
    add_admin = State(); remove_admin_pick = State()

# Same rationale as every other template's FLOW_TIMEOUT_SECONDS: MemoryStorage
# keeps a state until explicitly cleared — without an expiry, an admin who
# opens a create-event/add-guest prompt and goes quiet has every LATER plain
# message silently captured by that stale flow step.
FLOW_TIMEOUT_SECONDS = 300

def _flow_expired(data: dict) -> bool:
    started_at = data.get("started_at")
    return started_at is None or (time.time() - started_at) > FLOW_TIMEOUT_SECONDS


# Guards the FINAL step of the create-event / add-guest FSM flows
# (_finish_create_event / _finish_add_guest) against a rapid double-tap on
# the last "skip"/submit button. Unlike shop_catalog.py's cart-based
# double-tap guard (there, the second call finds an already-emptied cart and
# fails naturally), there is no such natural "already consumed" resource
# here — both calls would otherwise read the same still-unfledged FSM state
# and each insert their own row before either clears it. Same
# check-and-add-before-any-await reasoning as _broadcasting_events below:
# race-free under asyncio's single-threaded cooperative scheduling. Keyed by
# (db_path, chat_id, flow) so different bots/chats/flows never block each
# other.
_flow_submitting: set[tuple[str, int, str]] = set()


# ── panel navigation helpers (same pattern as shop_catalog.py/moderator.py) ──

async def _replace_panel(
    bot: Bot, state: FSMContext, chat_id: int, text: str,
    reply_markup: InlineKeyboardMarkup | None = None, parse_mode: str | None = "HTML",
) -> None:
    data = await state.get_data()
    prev_id = data.get("panel_msg_id")
    if prev_id:
        try:
            await bot.delete_message(chat_id, prev_id)
        except Exception as e:
            logger.debug(f"_replace_panel: failed to delete old panel {prev_id} in chat {chat_id}: {e}")
    try:
        msg = await bot.send_message(chat_id, text, parse_mode=parse_mode, reply_markup=reply_markup)
    except Exception as e:
        logger.warning(f"_replace_panel: failed to send panel to chat {chat_id}: {e}")
        return
    await state.update_data(panel_msg_id=msg.message_id)


async def _clear_flow_keep_panel(state: FSMContext) -> None:
    data = await state.get_data()
    panel_msg_id = data.get("panel_msg_id")
    await state.clear()
    if panel_msg_id is not None:
        await state.update_data(panel_msg_id=panel_msg_id)


def _panel_chat_id(cb: CallbackQuery) -> int | None:
    return cb.message.chat.id if cb.message else None


# ── keyboards: main menu ────────────────────────────────────────────────────

def kb_main(is_admin: bool) -> ReplyKeyboardMarkup | ReplyKeyboardRemove:
    if not is_admin:
        return ReplyKeyboardRemove()
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📅 Мероприятия")],
        [KeyboardButton(text="👥 Админы бота")],
    ], resize_keyboard=True)

def kb_cancel(callback_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data=callback_data)]])

def kb_optional_step(skip_cb: str, cancel_cb: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏩ Пропустить", callback_data=skip_cb)],
        [InlineKeyboardButton(text="❌ Отмена", callback_data=cancel_cb)],
    ])


# ── keyboards: events ────────────────────────────────────────────────────────

def kb_events_list(events: list[dict]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=_short(f"{e['name']} · {_fmt_event_dt(e['event_dt'])}"), callback_data=f"evm_event:{e['id']}")]
        for e in events
    ]
    rows.append([InlineKeyboardButton(text="➕ Новое мероприятие", callback_data="evm_event_new")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_event_detail(event_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Список гостей", callback_data=f"evm_guests:{event_id}:all")],
        [InlineKeyboardButton(text="➕ Добавить гостя", callback_data=f"evm_guest_add:{event_id}")],
        [InlineKeyboardButton(text="✅ Чек-ин", callback_data=f"evm_checkin:{event_id}")],
        [InlineKeyboardButton(text="📣 Напомнить всем", callback_data=f"evm_remind_ask:{event_id}")],
        [InlineKeyboardButton(text="🗑 Архивировать", callback_data=f"evm_archive_ask:{event_id}")],
        [InlineKeyboardButton(text="◀️ К мероприятиям", callback_data="evm_events")],
    ])

def kb_archive_confirm(event_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да, архивировать", callback_data=f"evm_archive_yes:{event_id}"),
        InlineKeyboardButton(text="❌ Отмена", callback_data=f"evm_event:{event_id}"),
    ]])

def kb_remind_confirm(event_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Разослать", callback_data=f"evm_remind_yes:{event_id}"),
        InlineKeyboardButton(text="❌ Отмена", callback_data=f"evm_event:{event_id}"),
    ]])


# ── keyboards: guests ────────────────────────────────────────────────────────

def kb_guest_filter_and_list(event_id: int, status_filter: str, guests: list[dict]) -> InlineKeyboardMarkup:
    filter_row = [
        InlineKeyboardButton(
            text=("• " if f == status_filter else "") + RSVP_FILTER_LABELS[f],
            callback_data=f"evm_guests:{event_id}:{f}",
        ) for f in RSVP_FILTERS
    ]
    rows = [filter_row]
    for g in guests:
        icon = "✅" if g["checked_in"] else RSVP_LABELS[g["rsvp_status"]][0]
        rows.append([InlineKeyboardButton(text=_short(f"{icon} {g['name']}"), callback_data=f"evm_guest:{event_id}:{g['id']}")])
    if not guests:
        rows.append([InlineKeyboardButton(text="Гостей пока нет", callback_data="evm_noop")])
    rows.append([InlineKeyboardButton(text="◀️ К мероприятию", callback_data=f"evm_event:{event_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_guest_detail(event_id: int, guest: dict) -> InlineKeyboardMarkup:
    checkin_label = "↩️ Снять отметку" if guest["checked_in"] else "✅ Отметить пришедшим"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=checkin_label, callback_data=f"evm_guest_ci:{event_id}:{guest['id']}")],
        [InlineKeyboardButton(text="🗑 Удалить гостя", callback_data=f"evm_guest_rm_ask:{event_id}:{guest['id']}")],
        [InlineKeyboardButton(text="◀️ К списку гостей", callback_data=f"evm_guests:{event_id}:all")],
    ])

def kb_guest_remove_confirm(event_id: int, guest_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"evm_guest_rm_yes:{event_id}:{guest_id}"),
        InlineKeyboardButton(text="❌ Отмена", callback_data=f"evm_guest:{event_id}:{guest_id}"),
    ]])

def kb_checkin_list(event_id: int, guests: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for g in guests:
        box = "☑️" if g["checked_in"] else "⬜️"
        rows.append([InlineKeyboardButton(text=_short(f"{box} {g['name']}"), callback_data=f"evm_ci_toggle:{event_id}:{g['id']}")])
    if not guests:
        rows.append([InlineKeyboardButton(text="Гостей пока нет", callback_data="evm_noop")])
    rows.append([InlineKeyboardButton(text="◀️ К мероприятию", callback_data=f"evm_event:{event_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── keyboards: guest-facing RSVP ────────────────────────────────────────────

def kb_rsvp(guest_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Буду", callback_data=f"evm_rsvp:{guest_id}:yes"),
        InlineKeyboardButton(text="❌ Не смогу", callback_data=f"evm_rsvp:{guest_id}:no"),
    ]])


# ── keyboards: bot-admins panel (same pattern as shop_catalog.py/moderator.py) ─

def kb_admins_panel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить админа", callback_data="evm_adm_addadmin")],
        [InlineKeyboardButton(text="➖ Убрать админа", callback_data="evm_adm_removeadmin")],
    ])

MAX_ADMIN_REMOVE_BUTTONS = 30

def kb_remove_admins(ids: list[str]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=admin_id, callback_data=f"evm_adm_rma:{i}")] for i, admin_id in enumerate(ids)]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="evm_adm_admins")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── render helpers ───────────────────────────────────────────────────────────

async def _render_event_detail(bot: Bot, state: FSMContext, chat_id: int, config: EventManagerConfig, event_id: int) -> None:
    event = await _event_row(config.db_path, event_id)
    if event is None:
        events = await _active_events(config.db_path)
        await _replace_panel(bot, state, chat_id, "📅 <b>Мероприятия</b>", reply_markup=kb_events_list(events))
        return
    stats = await _event_stats(config.db_path, event_id)
    limit_line = ""
    if event["guest_limit"]:
        over = " ⚠️" if stats["total"] > event["guest_limit"] else ""
        limit_line = f"👥 Лимит: {stats['total']}/{event['guest_limit']}{over}\n"
    text = (
        f"🎉 <b>{_esc(event['name'])}</b>\n"
        f"🗓 {_fmt_event_dt(event['event_dt'])}\n"
        f"📍 {_esc(event['location']) if event['location'] else '—'}\n"
        f"{limit_line}\n"
        f"RSVP: {RSVP_LABELS['confirmed']} {stats['counts']['confirmed']} · "
        f"{RSVP_LABELS['declined']} {stats['counts']['declined']} · "
        f"{RSVP_LABELS['invited']} {stats['counts']['invited']}\n"
        f"✅ Пришли: {stats['checked_in']}/{stats['total']}"
    )
    await _replace_panel(bot, state, chat_id, text, reply_markup=kb_event_detail(event_id))


async def _render_guest_list(bot: Bot, state: FSMContext, chat_id: int, config: EventManagerConfig, event_id: int, status_filter: str) -> None:
    guests = await _guests_for_event(config.db_path, event_id, status_filter)
    text = f"👥 <b>Гости</b> · {RSVP_FILTER_LABELS[status_filter]}"
    await _replace_panel(bot, state, chat_id, text, reply_markup=kb_guest_filter_and_list(event_id, status_filter, guests))


async def _render_guest_detail(bot: Bot, state: FSMContext, chat_id: int, config: EventManagerConfig, event_id: int, guest_id: int) -> None:
    guest = await _guest_row(config.db_path, guest_id)
    if guest is None:
        await _render_guest_list(bot, state, chat_id, config, event_id, "all")
        return
    link_line = "🔗 Гость уже открыл приглашение." if guest["telegram_user_id"] else \
        f"🔗 Ссылка-приглашение (перешлите гостю):\n<code>{_esc(await _invite_link(bot, guest['invite_token']))}</code>"
    text = (
        f"👤 <b>{_esc(guest['name'])}</b>\n"
        f"📞 {_esc(guest['contact']) if guest['contact'] else '—'}\n"
        f"RSVP: {RSVP_LABELS[guest['rsvp_status']]}\n"
        f"Чек-ин: {'✅ Пришёл' if guest['checked_in'] else '⬜️ Не отмечен'}\n\n"
        f"{link_line}"
    )
    await _replace_panel(bot, state, chat_id, text, reply_markup=kb_guest_detail(event_id, guest))


async def _invite_link(bot: Bot, token: str) -> str:
    username = await _bot_username(bot)
    return f"https://t.me/{username}?start=g_{token}"


async def _render_checkin_list(bot: Bot, state: FSMContext, chat_id: int, config: EventManagerConfig, event_id: int) -> None:
    guests = await _guests_for_event(config.db_path, event_id)
    await _replace_panel(bot, state, chat_id, "✅ <b>Чек-ин</b> — нажмите на гостя, чтобы отметить приход",
                          reply_markup=kb_checkin_list(event_id, guests))


def _guest_facing_card(event: dict, guest: dict) -> str:
    return (
        f"🎉 <b>{_esc(event['name'])}</b>\n"
        f"🗓 {_fmt_event_dt(event['event_dt'])}\n"
        f"📍 {_esc(event['location']) if event['location'] else '—'}\n\n"
        f"Ваш ответ: {RSVP_LABELS[guest['rsvp_status']]}"
    )


# ── /start ────────────────────────────────────────────────────────────────────

@router.message(Command("start"), F.chat.type == "private")
async def cmd_start(message: Message, command: CommandObject, state: FSMContext, config: EventManagerConfig):
    await state.clear()
    payload = command.args

    if payload and payload.startswith("g_"):
        token = payload[2:]
        guest = await _claim_guest(config.db_path, token, message.from_user.id)
        if guest is None:
            await message.answer("⚠️ Ссылка-приглашение не найдена или недействительна.")
            return
        if guest["telegram_user_id"] != message.from_user.id:
            await message.answer("⚠️ Эта ссылка уже использована другим пользователем.")
            return
        event = await _event_row(config.db_path, guest["event_id"])
        if event is None:
            await message.answer("⚠️ Мероприятие не найдено.")
            return
        await message.answer(_guest_facing_card(event, guest), parse_mode="HTML", reply_markup=kb_rsvp(guest["id"]))
        return

    admins = _load_admins(config.admins_file)
    sender_id = message.from_user.id
    # Bug fixed here: this used to grant admin to whoever sent /start FIRST,
    # which lets any client who messages the bot before the owner does
    # permanently seize the admin panel. When bots.owner_telegram_id is known
    # (webhook/production mode), only that user may claim the empty-admins
    # bootstrap slot; a non-owner sending /start first now just gets the
    # regular guest menu. In standalone/env mode (owner_telegram_id unknown)
    # the old first-comer behavior is kept as the only option available.
    is_owner = config.owner_telegram_id is not None and sender_id == config.owner_telegram_id
    first_time_admin = not admins and (is_owner or config.owner_telegram_id is None)
    if first_time_admin:
        _save_admins(config.admins_file, {str(sender_id)})
        if config.bot_id is not None:
            try:
                await add_bot_admin(config.bot_id, str(sender_id))
            except Exception as e:
                logger.warning(f"cmd_start: add_bot_admin sync failed for bot {config.bot_id}: {e}")
    is_admin = _is_bot_admin(sender_id, config)
    if is_admin:
        # Same welcome_image convention as every other template's cmd_start
        # (e.g. shop_catalog.py, moderator.py): send it as a photo caption
        # when the bot has one configured, plain text otherwise.
        if config.welcome_image.exists():
            await message.answer_photo(FSInputFile(str(config.welcome_image)),
                                        caption=WELCOME_TEXT, parse_mode="HTML", reply_markup=kb_main(True))
        else:
            await message.answer(WELCOME_TEXT, parse_mode="HTML", reply_markup=kb_main(True))
        if first_time_admin:
            await message.answer(
                "👑 <b>Вы — администратор этого бота.</b>\n\n"
                "Кнопка «📅 Мероприятия» открывает создание мероприятий и управление гостями.",
                parse_mode="HTML",
            )
    else:
        if config.welcome_image.exists():
            await message.answer_photo(FSInputFile(str(config.welcome_image)),
                                        caption=GUEST_NEUTRAL_TEXT, reply_markup=kb_main(False))
        else:
            await message.answer(GUEST_NEUTRAL_TEXT, reply_markup=kb_main(False))


# ── RSVP (guest-facing) ──────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("evm_rsvp:"))
async def cb_rsvp(cb: CallbackQuery, config: EventManagerConfig):
    _, guest_id_raw, choice = cb.data.split(":", 2)
    guest_id = int(guest_id_raw)
    guest = await _guest_row(config.db_path, guest_id)
    if guest is None or guest["telegram_user_id"] != cb.from_user.id:
        # Blocks anyone other than the guest this specific message was sent
        # to — the deep-link token unpredictability keeps a stranger from
        # ever getting this callback in front of them in the first place,
        # this is the second, independent layer of defense.
        await cb.answer("Действие недоступно.", show_alert=True)
        return
    await cb.answer("Спасибо! Ответ сохранён.")
    status = "confirmed" if choice == "yes" else "declined"
    await _set_rsvp(config.db_path, guest_id, status)
    guest["rsvp_status"] = status
    event = await _event_row(config.db_path, guest["event_id"])
    if event is None:
        return
    await cb.message.edit_text(_guest_facing_card(event, guest), parse_mode="HTML", reply_markup=kb_rsvp(guest_id))


@router.callback_query(F.data == "evm_noop")
async def cb_noop(cb: CallbackQuery):
    await cb.answer()


# ── main menu: events ────────────────────────────────────────────────────────

@router.message(F.text == "📅 Мероприятия")
async def events_entry(msg: Message, bot: Bot, state: FSMContext, config: EventManagerConfig):
    if not _is_bot_admin(msg.from_user.id, config):
        return
    await state.clear()
    events = await _active_events(config.db_path)
    await _replace_panel(bot, state, msg.chat.id, "📅 <b>Мероприятия</b>", reply_markup=kb_events_list(events))

@router.callback_query(F.data == "evm_events")
async def cb_events(cb: CallbackQuery, bot: Bot, state: FSMContext, config: EventManagerConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    await _clear_flow_keep_panel(state)
    events = await _active_events(config.db_path)
    await _replace_panel(bot, state, chat_id, "📅 <b>Мероприятия</b>", reply_markup=kb_events_list(events))

@router.callback_query(F.data.startswith("evm_event:"))
async def cb_event_detail(cb: CallbackQuery, bot: Bot, state: FSMContext, config: EventManagerConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    await _clear_flow_keep_panel(state)
    event_id = int(cb.data.split(":", 1)[1])
    await _render_event_detail(bot, state, chat_id, config, event_id)


# ── create event (FSM) ──────────────────────────────────────────────────────

@router.callback_query(F.data == "evm_event_new")
async def cb_event_new(cb: CallbackQuery, bot: Bot, state: FSMContext, config: EventManagerConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    await state.update_data(started_at=time.time())
    await state.set_state(EventFlow.name)
    await _replace_panel(bot, state, chat_id, "Название мероприятия:", reply_markup=kb_cancel("evm_events"))

@router.message(EventFlow.name, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def ev_name(msg: Message, bot: Bot, state: FSMContext, config: EventManagerConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await _clear_flow_keep_panel(state)
        await msg.answer("⏳ Время ожидания истекло — начните заново."); return
    if not _is_bot_admin(msg.from_user.id, config):
        await _clear_flow_keep_panel(state); return
    name = msg.text.strip()[:NAME_MAX_LEN]
    if not name:
        await msg.answer("Название не может быть пустым."); return
    await state.update_data(ev_name=name)
    await state.set_state(EventFlow.date)
    await _replace_panel(bot, state, msg.chat.id, "Дата и время (ДД.ММ.ГГГГ ЧЧ:ММ), например 25.12.2026 19:00:", reply_markup=kb_cancel("evm_events"))

@router.message(EventFlow.date, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def ev_date(msg: Message, bot: Bot, state: FSMContext, config: EventManagerConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await _clear_flow_keep_panel(state)
        await msg.answer("⏳ Время ожидания истекло — начните заново."); return
    if not _is_bot_admin(msg.from_user.id, config):
        await _clear_flow_keep_panel(state); return
    event_dt = _parse_event_dt(msg.text)
    if event_dt is None:
        await msg.answer("Не удалось распознать дату. Формат: ДД.ММ.ГГГГ ЧЧ:ММ, например 25.12.2026 19:00"); return
    await state.update_data(ev_dt=event_dt)
    await state.set_state(EventFlow.location)
    await _replace_panel(bot, state, msg.chat.id, "Место проведения (или пропустите):",
                          reply_markup=kb_optional_step("evm_ev_loc_skip", "evm_events"))

@router.message(EventFlow.location, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def ev_location(msg: Message, bot: Bot, state: FSMContext, config: EventManagerConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await _clear_flow_keep_panel(state)
        await msg.answer("⏳ Время ожидания истекло — начните заново."); return
    if not _is_bot_admin(msg.from_user.id, config):
        await _clear_flow_keep_panel(state); return
    await _advance_to_limit(msg.bot, state, msg.chat.id, msg.text.strip()[:LOCATION_MAX_LEN])

@router.callback_query(EventFlow.location, F.data == "evm_ev_loc_skip")
async def cb_ev_location_skip(cb: CallbackQuery, bot: Bot, state: FSMContext, config: EventManagerConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    data = await state.get_data()
    if _flow_expired(data):
        await _clear_flow_keep_panel(state)
        events = await _active_events(config.db_path)
        await _replace_panel(bot, state, chat_id, "⏳ Время ожидания истекло.\n\n📅 <b>Мероприятия</b>", reply_markup=kb_events_list(events)); return
    await _advance_to_limit(bot, state, chat_id, None)

async def _advance_to_limit(bot: Bot, state: FSMContext, chat_id: int, location: str | None) -> None:
    await state.update_data(ev_location=location)
    await state.set_state(EventFlow.limit)
    await _replace_panel(bot, state, chat_id, "Лимит гостей, число (или пропустите — без лимита):",
                          reply_markup=kb_optional_step("evm_ev_limit_skip", "evm_events"))

@router.message(EventFlow.limit, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def ev_limit(msg: Message, bot: Bot, state: FSMContext, config: EventManagerConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await _clear_flow_keep_panel(state)
        await msg.answer("⏳ Время ожидания истекло — начните заново."); return
    if not _is_bot_admin(msg.from_user.id, config):
        await _clear_flow_keep_panel(state); return
    try:
        limit = int(msg.text.strip())
        if limit <= 0 or limit > GUEST_LIMIT_MAX:
            raise ValueError
    except ValueError:
        await msg.answer(f"Введите целое число от 1 до {GUEST_LIMIT_MAX}."); return
    await _finish_create_event(msg.bot, state, msg.chat.id, config, limit)

@router.callback_query(EventFlow.limit, F.data == "evm_ev_limit_skip")
async def cb_ev_limit_skip(cb: CallbackQuery, bot: Bot, state: FSMContext, config: EventManagerConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    data = await state.get_data()
    if _flow_expired(data):
        await _clear_flow_keep_panel(state)
        events = await _active_events(config.db_path)
        await _replace_panel(bot, state, chat_id, "⏳ Время ожидания истекло.\n\n📅 <b>Мероприятия</b>", reply_markup=kb_events_list(events)); return
    await _finish_create_event(bot, state, chat_id, config, None)

async def _finish_create_event(bot: Bot, state: FSMContext, chat_id: int, config: EventManagerConfig, guest_limit: int | None) -> None:
    key = (config.db_path, chat_id, "create_event")
    if key in _flow_submitting:
        return
    _flow_submitting.add(key)
    try:
        data = await state.get_data()
        name = data.get("ev_name", "").strip()
        event_dt = data.get("ev_dt")
        if not name or not event_dt:
            await _clear_flow_keep_panel(state)
            events = await _active_events(config.db_path)
            await _replace_panel(bot, state, chat_id, "📅 <b>Мероприятия</b>", reply_markup=kb_events_list(events))
            return
        event_id = await _create_event(config.db_path, name, event_dt, data.get("ev_location"), guest_limit)
        await _clear_flow_keep_panel(state)
        await _render_event_detail(bot, state, chat_id, config, event_id)
    finally:
        _flow_submitting.discard(key)


# ── archive event ────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("evm_archive_ask:"))
async def cb_archive_ask(cb: CallbackQuery, bot: Bot, state: FSMContext, config: EventManagerConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    event_id = int(cb.data.split(":", 1)[1])
    await _replace_panel(bot, state, chat_id, "Архивировать мероприятие? Оно исчезнет из списка (данные не удаляются).",
                          reply_markup=kb_archive_confirm(event_id))

@router.callback_query(F.data.startswith("evm_archive_yes:"))
async def cb_archive_yes(cb: CallbackQuery, bot: Bot, state: FSMContext, config: EventManagerConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    event_id = int(cb.data.split(":", 1)[1])
    await _archive_event(config.db_path, event_id)
    events = await _active_events(config.db_path)
    await _replace_panel(bot, state, chat_id, "✅ Мероприятие архивировано.\n\n📅 <b>Мероприятия</b>", reply_markup=kb_events_list(events))


# ── guests: list / detail ────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("evm_guests:"))
async def cb_guests_list(cb: CallbackQuery, bot: Bot, state: FSMContext, config: EventManagerConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    await _clear_flow_keep_panel(state)
    _, event_id_raw, status_filter = cb.data.split(":", 2)
    if status_filter not in RSVP_FILTERS:
        status_filter = "all"
    await _render_guest_list(bot, state, chat_id, config, int(event_id_raw), status_filter)

@router.callback_query(F.data.startswith("evm_guest:"))
async def cb_guest_detail(cb: CallbackQuery, bot: Bot, state: FSMContext, config: EventManagerConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    await _clear_flow_keep_panel(state)
    _, event_id_raw, guest_id_raw = cb.data.split(":", 2)
    await _render_guest_detail(bot, state, chat_id, config, int(event_id_raw), int(guest_id_raw))

@router.callback_query(F.data.startswith("evm_guest_ci:"))
async def cb_guest_ci_toggle(cb: CallbackQuery, bot: Bot, state: FSMContext, config: EventManagerConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    _, event_id_raw, guest_id_raw = cb.data.split(":", 2)
    await _toggle_checkin(config.db_path, int(guest_id_raw))
    await _render_guest_detail(bot, state, chat_id, config, int(event_id_raw), int(guest_id_raw))

@router.callback_query(F.data.startswith("evm_guest_rm_ask:"))
async def cb_guest_rm_ask(cb: CallbackQuery, bot: Bot, state: FSMContext, config: EventManagerConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    _, event_id_raw, guest_id_raw = cb.data.split(":", 2)
    await _replace_panel(bot, state, chat_id, "Удалить гостя? Это действие необратимо.",
                          reply_markup=kb_guest_remove_confirm(int(event_id_raw), int(guest_id_raw)))

@router.callback_query(F.data.startswith("evm_guest_rm_yes:"))
async def cb_guest_rm_yes(cb: CallbackQuery, bot: Bot, state: FSMContext, config: EventManagerConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    _, event_id_raw, guest_id_raw = cb.data.split(":", 2)
    await _delete_guest(config.db_path, int(guest_id_raw))
    await _render_guest_list(bot, state, chat_id, config, int(event_id_raw), "all")


# ── add guest (FSM) ─────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("evm_guest_add:"))
async def cb_guest_add(cb: CallbackQuery, bot: Bot, state: FSMContext, config: EventManagerConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    event_id = int(cb.data.split(":", 1)[1])
    if await _event_row(config.db_path, event_id) is None:
        await cb_events(cb, bot, state, config); return
    await state.update_data(guest_event_id=event_id, started_at=time.time())
    await state.set_state(GuestFlow.name)
    await _replace_panel(bot, state, chat_id, "Имя гостя:", reply_markup=kb_cancel(f"evm_event:{event_id}"))

@router.message(GuestFlow.name, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def gu_name(msg: Message, bot: Bot, state: FSMContext, config: EventManagerConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await _clear_flow_keep_panel(state)
        await msg.answer("⏳ Время ожидания истекло — начните заново."); return
    if not _is_bot_admin(msg.from_user.id, config):
        await _clear_flow_keep_panel(state); return
    name = msg.text.strip()[:NAME_MAX_LEN]
    if not name:
        await msg.answer("Имя не может быть пустым."); return
    await state.update_data(gu_name=name)
    await state.set_state(GuestFlow.contact)
    event_id = data.get("guest_event_id")
    await _replace_panel(bot, state, msg.chat.id, "Контакт гостя — телефон или @username (или пропустите):",
                          reply_markup=kb_optional_step("evm_gu_contact_skip", f"evm_event:{event_id}"))

@router.message(GuestFlow.contact, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def gu_contact(msg: Message, bot: Bot, state: FSMContext, config: EventManagerConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await _clear_flow_keep_panel(state)
        await msg.answer("⏳ Время ожидания истекло — начните заново."); return
    if not _is_bot_admin(msg.from_user.id, config):
        await _clear_flow_keep_panel(state); return
    await _finish_add_guest(msg.bot, state, msg.chat.id, config, msg.text.strip()[:CONTACT_MAX_LEN])

@router.callback_query(GuestFlow.contact, F.data == "evm_gu_contact_skip")
async def cb_gu_contact_skip(cb: CallbackQuery, bot: Bot, state: FSMContext, config: EventManagerConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    data = await state.get_data()
    if _flow_expired(data):
        await _clear_flow_keep_panel(state)
        event_id = data.get("guest_event_id")
        if event_id:
            await _render_event_detail(bot, state, chat_id, config, event_id)
        return
    await _finish_add_guest(bot, state, chat_id, config, None)

async def _finish_add_guest(bot: Bot, state: FSMContext, chat_id: int, config: EventManagerConfig, contact: str | None) -> None:
    key = (config.db_path, chat_id, "add_guest")
    if key in _flow_submitting:
        return
    _flow_submitting.add(key)
    try:
        data = await state.get_data()
        event_id = data.get("guest_event_id")
        name = data.get("gu_name", "").strip()
        if not event_id or not name:
            await _clear_flow_keep_panel(state)
            events = await _active_events(config.db_path)
            await _replace_panel(bot, state, chat_id, "📅 <b>Мероприятия</b>", reply_markup=kb_events_list(events))
            return
        guest = await _create_guest(config.db_path, event_id, name, contact)
        link = await _invite_link(bot, guest["invite_token"])
        await _clear_flow_keep_panel(state)
        await _replace_panel(
            bot, state, chat_id,
            f"✅ Гость добавлен.\n\n🔗 Ссылка-приглашение (перешлите гостю):\n<code>{_esc(link)}</code>",
            reply_markup=kb_guest_detail(event_id, {**guest, "checked_in": 0}),
        )
    finally:
        _flow_submitting.discard(key)


# ── check-in list ────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("evm_checkin:"))
async def cb_checkin_list(cb: CallbackQuery, bot: Bot, state: FSMContext, config: EventManagerConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    await _clear_flow_keep_panel(state)
    event_id = int(cb.data.split(":", 1)[1])
    await _render_checkin_list(bot, state, chat_id, config, event_id)

@router.callback_query(F.data.startswith("evm_ci_toggle:"))
async def cb_ci_toggle(cb: CallbackQuery, bot: Bot, state: FSMContext, config: EventManagerConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    _, event_id_raw, guest_id_raw = cb.data.split(":", 2)
    await _toggle_checkin(config.db_path, int(guest_id_raw))
    await _render_checkin_list(bot, state, chat_id, config, int(event_id_raw))


# ── remind broadcast ──────────────────────────────────────────────────────────

# Guards against a rapid double-tap on "✅ Разослать" firing two overlapping
# broadcasts: aiogram dispatches each callback_query as its own asyncio task,
# so editing the panel first (removing the confirm buttons) does NOT prevent
# a second, already-in-flight tap from also reaching cb_remind_yes — it only
# changes what that second tap's panel edit races against. The check-and-add
# below happens synchronously (no `await` between them), so under asyncio's
# single-threaded cooperative scheduling it is race-free: whichever task's
# synchronous prefix runs first claims the key before the other task's
# prefix can even start. Keyed by (db_path, event_id), NOT just event_id —
# event ids are per-bot AUTOINCREMENT, so two different bots can legitimately
# have the same event_id at the same time and must not block each other.
_broadcasting_events: set[tuple[str, int]] = set()

@router.callback_query(F.data.startswith("evm_remind_ask:"))
async def cb_remind_ask(cb: CallbackQuery, bot: Bot, state: FSMContext, config: EventManagerConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    event_id = int(cb.data.split(":", 1)[1])
    guests = await _guests_for_event(config.db_path, event_id)
    reachable = [g for g in guests if g["telegram_user_id"]]
    await _replace_panel(
        bot, state, chat_id,
        f"📣 Разослать напоминание {len(reachable)} гостям, открывшим приглашение "
        f"(из {len(guests)} всего)?",
        reply_markup=kb_remind_confirm(event_id),
    )

@router.callback_query(F.data.startswith("evm_remind_yes:"))
async def cb_remind_yes(cb: CallbackQuery, bot: Bot, state: FSMContext, config: EventManagerConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    event_id = int(cb.data.split(":", 1)[1])
    key = (config.db_path, event_id)
    if key in _broadcasting_events:
        await bot.send_message(chat_id, "⏳ Рассылка уже выполняется, дождитесь её завершения.")
        return
    _broadcasting_events.add(key)
    try:
        event = await _event_row(config.db_path, event_id)
        if event is None:
            await cb_events(cb, bot, state, config); return
        # Also collapses the confirm buttons immediately for UI feedback —
        # the actual double-tap guard is the _broadcasting_events check above.
        await _replace_panel(bot, state, chat_id, "⏳ Рассылка...")
        guests = await _guests_for_event(config.db_path, event_id)
        reachable = [g for g in guests if g["telegram_user_id"]]
        sent, failed = 0, 0
        for guest in reachable:
            try:
                await bot.send_message(
                    guest["telegram_user_id"],
                    "🔔 Напоминание!\n\n" + _guest_facing_card(event, guest),
                    parse_mode="HTML", reply_markup=kb_rsvp(guest["id"]),
                )
                sent += 1
            except Exception as e:
                failed += 1
                logger.warning(f"cb_remind_yes: reminder to guest {guest['id']} failed: {e}")
        await _render_event_detail(bot, state, chat_id, config, event_id)
        await bot.send_message(chat_id, f"📣 Разослано: {sent}. Не удалось: {failed}.")
    finally:
        _broadcasting_events.discard(key)


# ── bot-admins panel ─────────────────────────────────────────────────────────

async def _admins_list_text(config: EventManagerConfig) -> str:
    ids = sorted(_load_admins(config.admins_file))
    if not ids:
        return "👥 Пусто"
    return _join_bounded(["👥 <b>Администраторы бота:</b>\n"] + [f"• <code>{_esc(i)}</code>" for i in ids])

@router.message(F.text == "👥 Админы бота")
async def admins_entry(msg: Message, bot: Bot, state: FSMContext, config: EventManagerConfig):
    if not _is_bot_admin(msg.from_user.id, config):
        return
    await state.clear()
    await _replace_panel(bot, state, msg.chat.id, await _admins_list_text(config), reply_markup=kb_admins_panel())

@router.callback_query(F.data == "evm_adm_admins")
async def cb_adm_admins(cb: CallbackQuery, bot: Bot, state: FSMContext, config: EventManagerConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    await _clear_flow_keep_panel(state)
    await _replace_panel(bot, state, chat_id, await _admins_list_text(config), reply_markup=kb_admins_panel())

@router.callback_query(F.data == "evm_adm_addadmin")
async def cb_adm_addadmin(cb: CallbackQuery, bot: Bot, state: FSMContext, config: EventManagerConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    await _replace_panel(
        bot, state, chat_id, "Пришлите Telegram ID пользователя, которого нужно сделать администратором:",
        reply_markup=kb_cancel("evm_adm_admins"),
    )
    await state.update_data(started_at=time.time())
    await state.set_state(AdminFlow.add_admin)

@router.message(AdminFlow.add_admin, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def adm_add_admin(msg: Message, bot: Bot, state: FSMContext, config: EventManagerConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await _clear_flow_keep_panel(state)
        await msg.answer("⏳ Время ожидания истекло — начните заново кнопкой «👥 Админы бота»."); return
    if not _is_bot_admin(msg.from_user.id, config):
        await _clear_flow_keep_panel(state)
        await msg.answer("⛔ Нет доступа"); return
    text = msg.text.strip()
    if not _valid_telegram_id(text):
        await msg.answer("Пришлите числовой Telegram ID."); return
    ids = _load_admins(config.admins_file); ids.add(text); _save_admins(config.admins_file, ids)
    if config.bot_id is not None:
        try:
            await add_bot_admin(config.bot_id, text)
        except Exception as e:
            logger.warning(f"adm_add_admin: add_bot_admin sync failed for bot {config.bot_id}: {e}")
    logger.info(f"adm_add_admin: {text} added as bot admin by {msg.from_user.id}")
    await _clear_flow_keep_panel(state)
    await msg.answer(f"✅ <code>{_esc(text)}</code> добавлен.", parse_mode="HTML")
    await _replace_panel(bot, state, msg.chat.id, await _admins_list_text(config), reply_markup=kb_admins_panel())

@router.callback_query(F.data == "evm_adm_removeadmin")
async def cb_adm_removeadmin(cb: CallbackQuery, bot: Bot, state: FSMContext, config: EventManagerConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    ids = sorted(_load_admins(config.admins_file))
    if len(ids) <= 1:
        await _replace_panel(
            bot, state, chat_id, "⚠️ Нельзя удалить единственного администратора.\n\n" + await _admins_list_text(config),
            reply_markup=kb_admins_panel(),
        )
        return
    if len(ids) <= MAX_ADMIN_REMOVE_BUTTONS:
        await state.update_data(remove_admin_ids=ids)
        await _replace_panel(bot, state, chat_id, "Выберите администратора для удаления:", reply_markup=kb_remove_admins(ids))
    else:
        await state.update_data(started_at=time.time())
        await state.set_state(AdminFlow.remove_admin_pick)
        await _replace_panel(bot, state, chat_id, "Пришлите Telegram ID администратора для удаления:", reply_markup=kb_cancel("evm_adm_admins"))

@router.callback_query(F.data.startswith("evm_adm_rma:"))
async def cb_adm_rma(cb: CallbackQuery, bot: Bot, state: FSMContext, config: EventManagerConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    data = await state.get_data()
    ids = data.get("remove_admin_ids", [])
    try:
        idx = int(cb.data.split(":", 1)[1])
    except ValueError:
        idx = -1
    if idx < 0 or idx >= len(ids):
        await _replace_panel(bot, state, chat_id, await _admins_list_text(config), reply_markup=kb_admins_panel()); return
    target = ids[idx]
    current = _load_admins(config.admins_file)
    if target in current and len(current) <= 1:
        await _replace_panel(
            bot, state, chat_id, "⚠️ Нельзя удалить единственного администратора.\n\n" + await _admins_list_text(config),
            reply_markup=kb_admins_panel(),
        )
        return
    current.discard(target); _save_admins(config.admins_file, current)
    if config.bot_id is not None:
        try:
            await remove_bot_admin(config.bot_id, target)
        except Exception as e:
            logger.warning(f"cb_adm_rma: remove_bot_admin sync failed for bot {config.bot_id}: {e}")
    logger.info(f"cb_adm_rma: {target} removed as bot admin by {cb.from_user.id}")
    await _clear_flow_keep_panel(state)
    await _replace_panel(bot, state, chat_id, await _admins_list_text(config), reply_markup=kb_admins_panel())

@router.message(AdminFlow.remove_admin_pick, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def adm_remove_admin_text(msg: Message, bot: Bot, state: FSMContext, config: EventManagerConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await _clear_flow_keep_panel(state)
        await msg.answer("⏳ Время ожидания истекло — начните заново."); return
    if not _is_bot_admin(msg.from_user.id, config):
        await _clear_flow_keep_panel(state); return
    text = msg.text.strip()
    if not _valid_telegram_id(text):
        await msg.answer("Пришлите числовой Telegram ID."); return
    ids = _load_admins(config.admins_file)
    if text in ids and len(ids) <= 1:
        await msg.answer("⚠️ Нельзя удалить единственного администратора."); return
    ids.discard(text); _save_admins(config.admins_file, ids)
    if config.bot_id is not None:
        try:
            await remove_bot_admin(config.bot_id, text)
        except Exception as e:
            logger.warning(f"adm_remove_admin_text: remove_bot_admin sync failed for bot {config.bot_id}: {e}")
    logger.info(f"adm_remove_admin_text: {text} removed as bot admin by {msg.from_user.id}")
    await _clear_flow_keep_panel(state)
    await _replace_panel(bot, state, msg.chat.id, await _admins_list_text(config), reply_markup=kb_admins_panel())


# ── MAIN ──────────────────────────────────────────────────────────────────────

async def main():
    config = config_from_env()
    bot = Bot(token=os.getenv("BOT_TOKEN"))
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(ConfigMiddleware(config))
    dp.include_router(router)
    await bot.set_my_description(BOT_DESCRIPTION)
    await init_db(config.db_path)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
