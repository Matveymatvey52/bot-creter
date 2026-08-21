# TEMPLATE: event_rsvp
# USE FOR: регистрация участников на одно мероприятие (концерт, вебинар, встреча выпускников, конференция) с ограничением по числу мест — гость указывает число мест (1-5), при нехватке мест уходит в лист ожидания и авто-подтверждается при отмене чужой брони, админ настраивает мероприятие (название/дата/вместимость) и видит список участников и лист ожидания
# CUSTOMIZE: sections marked with # CUSTOMIZE
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

import aiosqlite
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)

from db.database import add_bot_admin, remove_bot_admin

# BASE_URL/PORT are process-wide, same treatment as templates/tour_operator.py
# (see that template's own comment) — every bot in the shared webhook process
# reads the same values, so these stay module-level env reads rather than
# EventRsvpConfig fields.
PORT           = int(os.getenv("PORT", "8080"))
RAILWAY_DOMAIN = os.getenv("RAILWAY_PUBLIC_DOMAIN", "")
BASE_URL       = f"https://{RAILWAY_DOMAIN}" if RAILWAY_DOMAIN else f"http://localhost:{PORT}"

# ── CUSTOMIZE ────────────────────────────────────────────────────────────────
BOT_DESCRIPTION = "Регистрация участников на мероприятие с ограничением по числу мест: подтверждение или лист ожидания, авто-подтверждение из листа ожидания при отмене."
WELCOME_TEXT_ADMIN = (
    "🎫 <b>Регистрация на мероприятие</b>\n\n"
    "Гости регистрируются с числом мест, при нехватке — лист ожидания с "
    "авто-подтверждением при отмене чужой брони.\n\nВыберите действие:"
)
GUEST_WELCOME_TEXT = "👋 Здравствуйте! Здесь можно зарегистрироваться на мероприятие."
NO_EVENT_CONFIGURED_TEXT = "⏳ Мероприятие ещё не настроено организатором. Загляните позже."
# ── END CUSTOMIZE ─────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
router = Router()

STATUS_LABELS = {
    "confirmed": "✅ Подтверждено",
    "waitlist": "⏳ Лист ожидания",
    "cancelled": "❌ Отменено",
}


# ── mini-app config (see docs/MINIAPP_DESIGN.md, templates/tour_operator.py's
# own miniapp_config for the pilot) ─────────────────────────────────────────
# Declarative schema read by runtime/miniapp_api.py's generic CRUD handlers —
# NOT executable code (see that module's docstring). `table` and each field's
# `name` must match init_db()'s CREATE TABLE columns above exactly.
# event_details is a singleton row (id=1, see init_db()'s comment) — the
# "events" resource here still goes through the same generic list/detail/
# create handlers as any other resource; a bot's very first configure-event
# write is what creates that id=1 row (SQLite assigns rowid 1 to the first
# INTEGER PRIMARY KEY insert, satisfying the table's CHECK(id = 1)).
miniapp_config = {
    "resources": [
        {
            "name": "events",
            "table": "event_details",
            "order_by": "id DESC",
            "creatable": True,
            "title": "События",
            "titleField": "title",
            "fields": [
                {"name": "title", "label": "Название", "kind": "text", "list": False, "detail": False, "create": True},
                {"name": "description", "label": "Описание", "kind": "text", "list": True, "detail": True, "create": True},
                {"name": "event_date", "label": "Дата", "kind": "date", "list": True, "detail": True, "create": True},
                {"name": "capacity", "label": "Вместимость", "kind": "number", "list": False, "detail": True, "create": True},
            ],
        },
        {
            "name": "rsvps",
            "table": "rsvps",
            "order_by": "created_at DESC",
            "creatable": True,
            "title": "Заявки",
            "titleField": "client_name",
            "fields": [
                {"name": "client_user_id", "required": True, "label": "ID клиента", "kind": "number", "list": False, "detail": False, "create": True},
                {"name": "client_name", "label": "Имя клиента", "kind": "text", "list": False, "detail": True, "create": True},
                {"name": "client_phone", "label": "Телефон", "kind": "text", "list": False, "detail": True, "create": True},
                {"name": "guests_count", "label": "Гостей", "kind": "number", "list": True, "detail": True, "create": True},
                {"name": "status", "label": "Статус", "kind": "status", "list": True, "detail": True, "create": False},
                {"name": "created_at", "creatable": False, "label": "Создано", "kind": "date", "list": False, "detail": False, "create": False},
            ],
        },
    ],
}


# ── reminders (features/reminders.py, docs/REMINDERS_DESIGN.md) ────────────
# event_details.event_date is free-form user-typed text (see
# EventSetupFlow.event_date's handler above — no strptime validation on
# write, just a 100-char cap), so date_format below is a best-effort match:
# rows whose event_date doesn't parse under it are silently skipped by
# features/reminders.py (never treated as fatal), not "no reminder ever
# fires" for the whole bot. Bots whose admin types dates in this exact
# "ДД.ММ.ГГГГ ЧЧ:ММ" shape get working reminders; free-form text outside it
# does not, until event_date gets real input validation (a separate, larger
# change, out of scope here).
#
# recipient_query, not recipient_field: event_details is a singleton with no
# owner column at all (see its CHECK(id=1) comment above) — every confirmed
# RSVP's client is a recipient, found by joining off rsvps instead.
reminders_config = {
    "rules": [
        {
            "id": "event_rsvp_upcoming",
            "table": "event_details",
            "date_field": "event_date",
            "date_format": "%d.%m.%Y %H:%M",
            "recipient_query": "SELECT client_user_id AS chat_id FROM rsvps WHERE status = 'confirmed'",
            "offsets_hours": [24, 2],
            "message_template": "🔔 Напоминание: «{title}» начнётся {event_date:%d.%m %H:%M}",
        },
    ],
}


# ── config ───────────────────────────────────────────────────────────────────

@dataclass
class EventRsvpConfig:
    bot_name: str
    db_path: str
    admins_file: Path
    welcome_image: Path
    display_name: str | None = None
    group_chat_id: str | None = None
    # Only set in webhook mode (config_from_bot_row) — standalone mode has no
    # bots-table row to source it from. Used by _miniapp_url() to scope a
    # magic-link token to this bot; None means no mini-app link can be
    # minted (same as templates/tour_operator.py's TourOperatorConfig.bot_id).
    bot_id: int | None = None
    owner_telegram_id: int | None = None


def _paths_for(name: str, data_dir: Path) -> EventRsvpConfig:
    return EventRsvpConfig(
        bot_name=name,
        db_path=str(data_dir / f"{name}_data.db"),
        admins_file=data_dir / f"admins_{name}.json",
        welcome_image=data_dir / "bot_images" / f"{name}.jpg",
    )


def config_from_env() -> EventRsvpConfig:
    """Standalone/subprocess mode."""
    name = Path(__file__).stem
    data_dir = Path(os.getenv("DATA_DIR", "./data"))
    data_dir.mkdir(exist_ok=True)
    return _paths_for(name, data_dir)


def config_from_bot_row(bot_row: dict, data_dir: Path) -> EventRsvpConfig:
    """Webhook runtime mode. Paths built from bot_row["bot_id"] (bots.id, the
    physically unique AUTOINCREMENT PK) — NOT bot_row["name"] — same reasoning
    as every other template's config_from_bot_row (see docs/STAGE2_DESIGN.md
    "Изоляция по bots.id")."""
    bot_id = bot_row["bot_id"]
    config = EventRsvpConfig(
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
    """Injects this bot's EventRsvpConfig into data["config"]."""

    def __init__(self, config: EventRsvpConfig) -> None:
        self.config = config
        super().__init__()

    async def __call__(self, handler, event, data):
        data["config"] = self.config
        return await handler(event, data)


# ── admin helpers ─────────────────────────────────────────────────────────────

def _load_admins(admins_file: Path) -> set:
    try:
        return set(json.loads(admins_file.read_text()).get("ids", []))
    except Exception:
        return set()

def _save_admins(admins_file: Path, ids: set) -> None:
    admins_file.write_text(json.dumps({"ids": list(ids)}, ensure_ascii=False))

def _is_admin(user_id: int, config: EventRsvpConfig) -> bool:
    # The DB-known owner (bots.owner_telegram_id) is always an admin, even if
    # the local admins_file is empty/stale/hijacked — see cmd_start below for
    # why the file alone can't be trusted as the sole source of truth.
    if config.owner_telegram_id is not None and str(user_id) == str(config.owner_telegram_id):
        return True
    return str(user_id) in _load_admins(config.admins_file)


def _esc(value, max_len: int = 500) -> str:
    """HTML-escapes AND length-bounds any user-supplied text before it goes into
    a parse_mode="HTML" message — same helper/rationale as templates/
    inventory.py's _esc()."""
    text = str(value) if value is not None else ""
    if len(text) > max_len:
        text = text[:max_len] + "…"
    return html.escape(text)


def _join_bounded(lines: list[str], limit: int = 3500) -> str:
    """Joins lines with a length budget, dropping only WHOLE trailing lines —
    same rationale as templates/inventory.py's _join_bounded()."""
    out: list[str] = []
    total = 0
    for line in lines:
        if total + len(line) + 1 > limit:
            out.append("…")
            break
        out.append(line)
        total += len(line) + 1
    return "\n".join(out)


# ── db ────────────────────────────────────────────────────────────────────────
# event_details has exactly one row (id=1): one bot = one event. rsvps.status
# is the only mutable "seat ledger" field — capacity math always re-derives
# occupied seats by SUM(guests_count) over status='confirmed' rather than
# maintaining a separate counter, so it can never drift out of sync with the
# actual rows.

async def init_db(db_path: str):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS event_details (
                id           INTEGER PRIMARY KEY CHECK (id = 1),
                title        TEXT,
                description  TEXT,
                event_date   TEXT,
                capacity     INTEGER
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS rsvps (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                client_user_id   INTEGER NOT NULL,
                client_name      TEXT,
                client_phone     TEXT,
                guests_count     INTEGER NOT NULL DEFAULT 1,
                status           TEXT NOT NULL DEFAULT 'confirmed'
                                 CHECK(status IN ('confirmed','cancelled','waitlist')),
                created_at       TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_rsvps_status ON rsvps(status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_rsvps_client ON rsvps(client_user_id)")
        # "Офисы" (docs/OFFICES_DESIGN.md §8 вариант 1) — one row per matched
        # office_event, so an admin can see cross-bot overlaps without this
        # template needing a live Bot instance to push a Telegram message
        # (on_office_event() below only receives event+config, no Bot — see
        # runtime/registry.py's _office_event_hook). Kept minimal on purpose:
        # this is the FIRST real office_events consumer, proving the
        # publish→deliver→react path end-to-end on the existing order.created
        # contract, not a general notifications feature.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS office_notes (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                source_bot_id   INTEGER NOT NULL,
                event_type      TEXT NOT NULL,
                note            TEXT NOT NULL,
                created_at      TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.commit()


# "Офисы" (docs/OFFICES_DESIGN.md §8 вариант 1) — by-convention hook: exposing
# a module-level on_office_event(event, config) is what makes registry.py's
# build_entry() register this template as a subscriber for ANY bot linked to
# it via bot_office_links, regardless of event_type (the type check happens
# inside, since _EVENT_TYPES may grow beyond order.created later and this
# function only knows how to react to that one type today).
async def on_office_event(event, config: EventRsvpConfig) -> None:
    """Reacts to order.created from a linked source bot (e.g. tour_operator):
    if the paying customer (event.payload.customer_chat_id) already has an
    active RSVP for this bot's own event, records a note for the admin to see
    — same "tour guest is also an event attendee" cross-sell/logistics signal
    docs/OFFICES_DESIGN.md §8's Вариант 1 describes. Deliberately does NOT
    push a live Telegram message: on_office_event() is called with only
    (event, config), no Bot instance (see runtime/registry.py's
    _office_event_hook) — a DB note keeps this subscriber simple and
    consistent with every other office_events subscriber that will ever be
    added without needing its own Bot plumbing threaded through the hook.

    Never raises: features/office_events.py's publish_event() already
    isolates each subscriber in its own try/except, but this stays defensive
    on the same "must not have any dispatcher-side effect" principle CUSTOMIZE
    hooks across this codebase follow."""
    if event.event_type != "order.created":
        return
    customer_chat_id = getattr(event.payload, "customer_chat_id", None)
    if customer_chat_id is None:
        return
    try:
        async with aiosqlite.connect(config.db_path) as db:
            has_rsvp = await (await db.execute(
                "SELECT 1 FROM rsvps WHERE client_user_id=? AND status IN ('confirmed','waitlist') LIMIT 1",
                (customer_chat_id,),
            )).fetchone()
            if not has_rsvp:
                return
            await db.execute(
                """
                INSERT INTO office_notes (source_bot_id, event_type, note)
                VALUES (?, ?, ?)
                """,
                (
                    event.source_bot_id,
                    event.event_type,
                    f"Клиент {customer_chat_id} оформил заказ у связанного бота "
                    f"(bot_id={event.source_bot_id}) и уже зарегистрирован на это мероприятие.",
                ),
            )
            await db.commit()
    except Exception:
        logger.exception(
            f"on_office_event: failed to record office note for customer_chat_id={customer_chat_id} "
            f"source_bot_id={event.source_bot_id}"
        )


async def _get_event(db_path: str) -> aiosqlite.Row | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        return await (await db.execute("SELECT * FROM event_details WHERE id=1")).fetchone()


async def _confirmed_guest_count(db, exclude_rsvp_id: int | None = None) -> int:
    """SUM(guests_count) over confirmed RSVPs — the single source of truth for
    occupied seats. Must be called from WITHIN the same connection/transaction
    that will act on the result, so the read-then-write is atomic under
    SQLite's single-writer serialization (see race-safety note on
    _confirm_or_waitlist below)."""
    if exclude_rsvp_id is None:
        row = await (await db.execute(
            "SELECT COALESCE(SUM(guests_count), 0) FROM rsvps WHERE status='confirmed'"
        )).fetchone()
    else:
        row = await (await db.execute(
            "SELECT COALESCE(SUM(guests_count), 0) FROM rsvps WHERE status='confirmed' AND id != ?",
            (exclude_rsvp_id,),
        )).fetchone()
    return row[0]


# ── FSM staleness guard ─────────────────────────────────────────────────────────
FLOW_TIMEOUT_SECONDS = 300
MAX_GUESTS = 5


def _flow_expired(data: dict) -> bool:
    started_at = data.get("started_at")
    return started_at is None or (time.time() - started_at) > FLOW_TIMEOUT_SECONDS


# ── FSM states ───────────────────────────────────────────────────────────────

class RsvpFlow(StatesGroup):
    guests = State()
    name = State()
    phone = State()

class EventSetupFlow(StatesGroup):
    title = State()
    description = State()
    event_date = State()
    capacity = State()

class AdminMgmtFlow(StatesGroup):
    add_admin = State()
    remove_admin_pick = State()


def _valid_admin_id(text: str) -> bool:
    if not (bool(text) and text.isascii() and text.isdigit() and len(text) <= 15):
        return False
    return int(text) > 0 and str(int(text)) == text


def _valid_capacity(text: str) -> int | None:
    try:
        cap = int(text.strip())
    except ValueError:
        return None
    if cap <= 0 or cap > 1_000_000:
        return None
    return cap


# ── keyboards ─────────────────────────────────────────────────────────────────

def kb_main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚙️ Настроить мероприятие", callback_data="setup_menu")],
        [InlineKeyboardButton(text="📋 Список участников", callback_data="participants_list")],
        [InlineKeyboardButton(text="⏳ Лист ожидания", callback_data="waitlist_list")],
        [InlineKeyboardButton(text="👥 Админы", callback_data="adm_menu")],
        [InlineKeyboardButton(text="🌐 Веб-приложение", callback_data="miniapp_link")],
    ])

def kb_back(callback_data: str = "main_menu") -> InlineKeyboardButton:
    return InlineKeyboardButton(text="◀️ Назад", callback_data=callback_data)

def kb_flow_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="flow_cancel")],
    ])

MAX_LIST_ROWS = 40

# ── guest-side ──
def kb_guest_menu(has_rsvp: bool) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="📝 Зарегистрироваться", callback_data="rsvp_new")]]
    if has_rsvp:
        rows.append([InlineKeyboardButton(text="📋 Моя регистрация", callback_data="rsvp_mine")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_guests_count() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=str(n), callback_data=f"rsvp_guests:{n}") for n in range(1, MAX_GUESTS + 1)],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="flow_cancel")],
    ])

def kb_my_rsvp(rsvp_id: int, status: str) -> InlineKeyboardMarkup:
    rows = []
    if status in ("confirmed", "waitlist"):
        rows.append([InlineKeyboardButton(text="❌ Отменить", callback_data=f"rsvp_cancel:{rsvp_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

# ── admin: setup ──
def kb_setup_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Название", callback_data="setup_title")],
        [InlineKeyboardButton(text="📅 Дата", callback_data="setup_date")],
        [InlineKeyboardButton(text="🔢 Вместимость", callback_data="setup_capacity")],
        [kb_back()],
    ])

def kb_description_skip() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭ Без описания", callback_data="setup_desc_skip")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="flow_cancel")],
    ])

# ── admins menu ──
def kb_admins_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить админа", callback_data="adm_add")],
        [InlineKeyboardButton(text="➖ Убрать админа", callback_data="adm_remove")],
        [kb_back()],
    ])

MAX_ADMIN_REMOVE_BUTTONS = 30

def kb_remove_admins(ids: list[str]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=admin_id, callback_data=f"adm_rm:{i}")] for i, admin_id in enumerate(ids)]
    rows.append([kb_back("adm_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── rendering helpers ────────────────────────────────────────────────────────

async def _event_summary_text(db_path: str) -> str:
    event = await _get_event(db_path)
    async with aiosqlite.connect(db_path) as db:
        occupied = await _confirmed_guest_count(db)
    if not event or not event["title"]:
        return NO_EVENT_CONFIGURED_TEXT
    capacity = event["capacity"] or 0
    lines = [f"🎫 <b>{_esc(event['title'])}</b>\n"]
    if event["description"]:
        lines.append(f"{_esc(event['description'], 1000)}\n")
    if event["event_date"]:
        lines.append(f"📅 {_esc(event['event_date'])}")
    lines.append(f"🔢 Занято мест: {occupied}/{capacity}" if capacity else f"🔢 Занято мест: {occupied}")
    return _join_bounded(lines)


async def _my_rsvp_text(db_path: str, user_id: int) -> tuple[str, InlineKeyboardMarkup | None, int | None]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            "SELECT * FROM rsvps WHERE client_user_id=? AND status IN ('confirmed','waitlist') "
            "ORDER BY id DESC LIMIT 1",
            (user_id,),
        )).fetchone()
    if not row:
        return "У вас нет активной регистрации.", None, None
    lines = [
        f"📋 <b>Ваша регистрация</b> · {STATUS_LABELS.get(row['status'], row['status'])}\n",
        f"👤 {_esc(row['client_name'])}",
        f"👥 Гостей: {row['guests_count']}",
    ]
    return _join_bounded(lines), kb_my_rsvp(row["id"], row["status"]), row["id"]


async def _participants_list_text(db_path: str) -> str:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT client_name, guests_count FROM rsvps WHERE status='confirmed' ORDER BY id LIMIT ?",
            (MAX_LIST_ROWS,),
        )).fetchall()
        occupied = await _confirmed_guest_count(db)
    event = await _get_event(db_path)
    capacity = event["capacity"] if event else None
    header = f"📋 <b>Участники</b> · занято {occupied}" + (f"/{capacity}" if capacity else "") + "\n"
    if not rows:
        return header + "— пока никто не подтверждён —"
    lines = [header] + [f"• {_esc(r['client_name'])} — {r['guests_count']} чел." for r in rows]
    return _join_bounded(lines)


async def _waitlist_text(db_path: str) -> str:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT client_name, guests_count, created_at FROM rsvps WHERE status='waitlist' "
            "ORDER BY id LIMIT ?",
            (MAX_LIST_ROWS,),
        )).fetchall()
    if not rows:
        return "⏳ <b>Лист ожидания</b>\n\n— пусто —"
    lines = ["⏳ <b>Лист ожидания</b>\n"] + [
        f"• {_esc(r['client_name'])} — {r['guests_count']} чел. ({r['created_at']})" for r in rows
    ]
    return _join_bounded(lines)


# ── /start ────────────────────────────────────────────────────────────────────

@router.message(Command("start"), F.chat.type == "private")
async def cmd_start(message: Message, state: FSMContext, config: EventRsvpConfig):
    await state.clear()
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
        admins = {str(sender_id)}
        if config.bot_id is not None:
            try:
                await add_bot_admin(config.bot_id, str(sender_id))
            except Exception as e:
                logger.warning(f"cmd_start: add_bot_admin sync failed for bot {config.bot_id}: {e}")

    if str(message.from_user.id) in admins:
        text = WELCOME_TEXT_ADMIN
        if config.welcome_image.exists():
            await message.answer_photo(
                FSInputFile(str(config.welcome_image)), caption=text,
                parse_mode="HTML", reply_markup=kb_main_menu(),
            )
        else:
            await message.answer(text, parse_mode="HTML", reply_markup=kb_main_menu())
        if first_time_admin:
            await message.answer(
                "👑 <b>Вы — администратор этого бота.</b>\n\n"
                "Управление другими администраторами — кнопка «👥 Админы» выше.",
                parse_mode="HTML",
            )
        return

    event = await _get_event(config.db_path)
    if not event or not event["title"]:
        await message.answer(NO_EVENT_CONFIGURED_TEXT)
        return
    async with aiosqlite.connect(config.db_path) as db:
        has_rsvp_row = await (await db.execute(
            "SELECT 1 FROM rsvps WHERE client_user_id=? AND status IN ('confirmed','waitlist') LIMIT 1",
            (message.from_user.id,),
        )).fetchone()
    summary = await _event_summary_text(config.db_path)
    await message.answer(
        f"{GUEST_WELCOME_TEXT}\n\n{summary}", parse_mode="HTML",
        reply_markup=kb_guest_menu(bool(has_rsvp_row)),
    )


def _miniapp_url(bot_id: int, telegram_user_id: int) -> str | None:
    """Builds the mini-app link with a signed, expiring token (see
    runtime/miniapp_api.py's mint_magic_link_token) — same pattern as
    templates/tour_operator.py's _miniapp_url(). Returns None if
    MINIAPP_SECRET isn't configured (e.g. standalone/dev mode without the
    webhook runtime's mini-app layer) — callers fall back to a
    Telegram-only message in that case."""
    from runtime.miniapp_api import mint_magic_link_token

    try:
        token = mint_magic_link_token(bot_id, telegram_user_id)
    except RuntimeError:
        return None
    return f"{BASE_URL}/app/{bot_id}?token={token}"


@router.message(Command("app"))
async def cmd_app(m: Message, config: EventRsvpConfig):
    if not _is_admin(m.from_user.id, config):
        return
    url = _miniapp_url(config.bot_id, m.from_user.id) if config.bot_id is not None else None
    if not url:
        await m.answer(
            "🌐 Веб-приложение временно недоступно (не настроен MINIAPP_SECRET). "
            "Пользуйтесь кнопками ниже."
        )
        return
    await m.answer(
        f"🌐 <b>Веб-приложение</b>\n\n"
        f'<a href="{url}">Открыть →</a>\n\n<code>{url}</code>',
        parse_mode="HTML", disable_web_page_preview=True,
    )


@router.callback_query(F.data == "miniapp_link")
async def cb_miniapp_link(cb: CallbackQuery, config: EventRsvpConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    url = _miniapp_url(config.bot_id, cb.from_user.id) if config.bot_id is not None else None
    if not url:
        await cb.message.answer(
            "🌐 Веб-приложение временно недоступно (не настроен MINIAPP_SECRET). "
            "Пользуйтесь кнопками ниже."
        )
        return
    await cb.message.answer(
        f"🌐 <b>Веб-приложение</b>\n\n"
        f'<a href="{url}">Открыть →</a>\n\n<code>{url}</code>',
        parse_mode="HTML", disable_web_page_preview=True,
    )


@router.callback_query(F.data == "main_menu")
async def cb_main_menu(cb: CallbackQuery, state: FSMContext, config: EventRsvpConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    await cb.message.edit_text(WELCOME_TEXT_ADMIN, parse_mode="HTML", reply_markup=kb_main_menu())


@router.callback_query(F.data == "flow_cancel")
async def cb_flow_cancel(cb: CallbackQuery, state: FSMContext, config: EventRsvpConfig):
    await cb.answer()
    await state.clear()
    if _is_admin(cb.from_user.id, config):
        await cb.message.edit_text("Отменено.", reply_markup=kb_main_menu())
        return
    summary = await _event_summary_text(config.db_path)
    async with aiosqlite.connect(config.db_path) as db:
        has_rsvp_row = await (await db.execute(
            "SELECT 1 FROM rsvps WHERE client_user_id=? AND status IN ('confirmed','waitlist') LIMIT 1",
            (cb.from_user.id,),
        )).fetchone()
    await cb.message.edit_text(
        f"Отменено.\n\n{summary}", parse_mode="HTML", reply_markup=kb_guest_menu(bool(has_rsvp_row)),
    )


# ── GUEST: register ────────────────────────────────────────────────────────────

@router.callback_query(F.data == "rsvp_new")
async def cb_rsvp_new(cb: CallbackQuery, state: FSMContext, config: EventRsvpConfig):
    await cb.answer()
    event = await _get_event(config.db_path)
    if not event or not event["title"]:
        await cb.message.edit_text(NO_EVENT_CONFIGURED_TEXT)
        return
    await state.clear()
    await state.set_state(RsvpFlow.guests)
    await state.update_data(started_at=time.time())
    await cb.message.edit_text("👥 Сколько мест нужно (включая вас)?", reply_markup=kb_guests_count())


@router.callback_query(RsvpFlow.guests, F.data.startswith("rsvp_guests:"))
async def cb_rsvp_guests(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново с /start.")
        return
    try:
        guests = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    if guests < 1 or guests > MAX_GUESTS:
        return
    await state.update_data(pending_guests=guests)
    await state.set_state(RsvpFlow.name)
    await cb.message.edit_text("👤 Ваше имя:", reply_markup=kb_flow_cancel())


@router.message(RsvpFlow.name, F.text, ~F.text.startswith("/"))
async def rsvp_name(msg: Message, state: FSMContext):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново с /start.")
        return
    name = msg.text.strip()
    if not name:
        await msg.answer("Имя не может быть пустым. Введите ваше имя:", reply_markup=kb_flow_cancel())
        return
    if len(name) > 200:
        await msg.answer("⚠️ Слишком длинное имя. Введите имя короче 200 символов:", reply_markup=kb_flow_cancel())
        return
    await state.update_data(pending_name=name)
    await state.set_state(RsvpFlow.phone)
    await msg.answer("📱 Ваш телефон:", reply_markup=kb_flow_cancel())


@router.message(RsvpFlow.phone, F.text, ~F.text.startswith("/"))
async def rsvp_phone(msg: Message, state: FSMContext, config: EventRsvpConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново с /start.")
        return
    phone = msg.text.strip()
    if not phone:
        await msg.answer("Телефон не может быть пустым. Введите ваш телефон:", reply_markup=kb_flow_cancel())
        return
    if len(phone) > 32:
        await msg.answer("⚠️ Слишком длинный номер. Введите телефон короче 32 символов:", reply_markup=kb_flow_cancel())
        return

    guests = data.get("pending_guests")
    name = data.get("pending_name")
    if not guests or not name:
        await state.clear()
        await msg.answer("Сессия устарела, начните заново с /start.")
        return
    await state.clear()

    status = await _confirm_or_waitlist(config.db_path, msg.from_user.id, name, phone, guests)
    if status == "confirmed":
        await msg.answer(
            f"✅ Регистрация подтверждена! Мест: {guests}.", reply_markup=kb_guest_menu(True),
        )
    else:
        await msg.answer(
            f"⏳ Мест не хватает — вы в листе ожидания. Мест запрошено: {guests}. "
            "Мы сообщим, если место освободится.",
            reply_markup=kb_guest_menu(True),
        )


async def _confirm_or_waitlist(db_path: str, user_id: int, name: str, phone: str, guests: int) -> str:
    """Inserts one rsvp row, deciding confirmed vs waitlist by re-checking
    capacity from WITHIN the same connection as the insert.

    Race-safety: plain `async with aiosqlite.connect(...)` does NOT take
    SQLite's write lock until the first WRITE statement — by default
    (isolation_level=""), a bare SELECT runs autocommit/lock-free, so two
    concurrent connections can both read "1 seat free" before either has
    written anything, and both then insert as confirmed (verified overbooked
    under real asyncio concurrency before this fix). `BEGIN IMMEDIATE`
    forces SQLite to take the RESERVED write lock immediately, before the
    capacity SELECT below runs — so the second caller's BEGIN IMMEDIATE
    blocks until the first caller's COMMIT, and its subsequent SELECT is
    then guaranteed to see the first caller's INSERT. Covered by
    tests/test_event_rsvp.py's race-safety tests (asyncio.gather, not
    sequential calls)."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        event = await (await db.execute("SELECT capacity FROM event_details WHERE id=1")).fetchone()
        capacity = event[0] if event and event[0] else None
        occupied = await _confirmed_guest_count(db)
        status = "confirmed" if (capacity is None or occupied + guests <= capacity) else "waitlist"
        await db.execute(
            "INSERT INTO rsvps (client_user_id, client_name, client_phone, guests_count, status) "
            "VALUES (?,?,?,?,?)",
            (user_id, name, phone, guests, status),
        )
        await db.commit()
    return status


@router.callback_query(F.data == "rsvp_mine")
async def cb_rsvp_mine(cb: CallbackQuery, config: EventRsvpConfig):
    await cb.answer()
    text, kb, _ = await _my_rsvp_text(config.db_path, cb.from_user.id)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb or kb_guest_menu(True))


@router.callback_query(F.data.startswith("rsvp_cancel:"))
async def cb_rsvp_cancel(cb: CallbackQuery, bot: Bot, config: EventRsvpConfig):
    await cb.answer()
    try:
        rsvp_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return

    db_path = config.db_path
    promoted = None
    async with aiosqlite.connect(db_path) as db:
        row = await (await db.execute(
            "SELECT status, client_user_id, guests_count FROM rsvps WHERE id=?", (rsvp_id,)
        )).fetchone()
        if not row:
            await cb.message.edit_text("Регистрация не найдена.", reply_markup=kb_guest_menu(False))
            return
        old_status, owner_id, freed_guests = row
        # Ownership check: only the guest who made the RSVP can cancel it —
        # without this, guessing another guest's rsvp_id in the callback_data
        # would let anyone cancel a stranger's registration.
        if owner_id != cb.from_user.id:
            await cb.message.edit_text("Регистрация не найдена.", reply_markup=kb_guest_menu(False))
            return
        if old_status not in ("confirmed", "waitlist"):
            # Already cancelled (stale button / double-tap) — no-op, re-render.
            text, kb, _ = await _my_rsvp_text(db_path, cb.from_user.id)
            await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb or kb_guest_menu(True))
            return
        # Compare-and-swap: WHERE status=old_status makes a double-tap a no-op
        # on the second write instead of double-promoting a waitlist entry —
        # same principle as templates/orders_tracker.py's cb_ord_status.
        cur = await db.execute(
            "UPDATE rsvps SET status='cancelled' WHERE id=? AND status=?", (rsvp_id, old_status)
        )
        if cur.rowcount == 0:
            await db.commit()
            text, kb, _ = await _my_rsvp_text(db_path, cb.from_user.id)
            await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb or kb_guest_menu(True))
            return

        if old_status == "confirmed":
            promoted = await _promote_from_waitlist(db)
        await db.commit()

    await cb.message.edit_text("❌ Регистрация отменена.", reply_markup=kb_guest_menu(True))

    if promoted:
        promoted_user_id, promoted_guests = promoted
        try:
            await bot.send_message(
                promoted_user_id,
                f"✅ Место освободилось — ваша регистрация ({promoted_guests} чел.) подтверждена!",
            )
        except TelegramAPIError as e:
            logger.warning(f"event_rsvp: failed to notify promoted guest {promoted_user_id}: {e}")


async def _promote_from_waitlist(db: aiosqlite.Connection) -> tuple[int, int] | None:
    """Called from WITHIN the same connection/transaction as the cancellation
    that freed seats, so the capacity re-check below sees the cancellation
    already applied. Promotes the OLDEST waitlist entry that now fits in the
    freed capacity (FIFO), not necessarily one that exactly matches the seat
    count just freed — a party of 2 waiting can be promoted into 3 freed
    seats. Only ever promotes at most one entry per cancellation, matching
    the brief's "the next one from the waitlist" (singular)."""
    event = await (await db.execute("SELECT capacity FROM event_details WHERE id=1")).fetchone()
    capacity = event[0] if event and event[0] else None
    if capacity is None:
        return None
    occupied = await _confirmed_guest_count(db)
    candidate = await (await db.execute(
        "SELECT id, client_user_id, guests_count FROM rsvps WHERE status='waitlist' ORDER BY id LIMIT 1"
    )).fetchone()
    if not candidate:
        return None
    cand_id, cand_user_id, cand_guests = candidate
    if occupied + cand_guests > capacity:
        return None
    cur = await db.execute(
        "UPDATE rsvps SET status='confirmed' WHERE id=? AND status='waitlist'", (cand_id,)
    )
    if cur.rowcount == 0:
        return None
    return cand_user_id, cand_guests


# ── ADMIN: setup ─────────────────────────────────────────────────────────────

@router.callback_query(F.data == "setup_menu")
async def cb_setup_menu(cb: CallbackQuery, state: FSMContext, config: EventRsvpConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    text = await _event_summary_text(config.db_path)
    await cb.message.edit_text(f"⚙️ <b>Настройка мероприятия</b>\n\n{text}", parse_mode="HTML",
                                reply_markup=kb_setup_menu())


async def _ensure_event_row(db: aiosqlite.Connection) -> None:
    await db.execute("INSERT OR IGNORE INTO event_details (id) VALUES (1)")


@router.callback_query(F.data == "setup_title")
async def cb_setup_title(cb: CallbackQuery, state: FSMContext, config: EventRsvpConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    await state.set_state(EventSetupFlow.title)
    await state.update_data(started_at=time.time())
    await cb.message.edit_text("📝 Введите название мероприятия:", reply_markup=kb_flow_cancel())


@router.message(EventSetupFlow.title, F.text, ~F.text.startswith("/"))
async def setup_title(msg: Message, state: FSMContext, config: EventRsvpConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    title = msg.text.strip()
    if not title:
        await msg.answer("Название не может быть пустым. Введите название:", reply_markup=kb_flow_cancel())
        return
    if len(title) > 200:
        await msg.answer("⚠️ Слишком длинное название. Введите короче 200 символов:", reply_markup=kb_flow_cancel())
        return
    await state.update_data(pending_title=title)
    await state.set_state(EventSetupFlow.description)
    await msg.answer("📄 Описание (или нажмите «Без описания»):", reply_markup=kb_description_skip())


async def _finalize_title(message_answer, state: FSMContext, config: EventRsvpConfig, description: str | None) -> None:
    data = await state.get_data()
    title = data.get("pending_title")
    if not title:
        await state.clear()
        await message_answer("Сессия устарела, начните заново.", reply_markup=kb_main_menu())
        return
    await state.clear()
    async with aiosqlite.connect(config.db_path) as db:
        await _ensure_event_row(db)
        await db.execute("UPDATE event_details SET title=?, description=? WHERE id=1", (title, description))
        await db.commit()
    await message_answer(f"✅ Название обновлено: {_esc(title)}", parse_mode="HTML", reply_markup=kb_setup_menu())


@router.message(EventSetupFlow.description, F.text, ~F.text.startswith("/"))
async def setup_description(msg: Message, state: FSMContext, config: EventRsvpConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    description = msg.text.strip()
    if len(description) > 1000:
        await msg.answer("⚠️ Слишком длинное описание. Введите короче 1000 символов:", reply_markup=kb_description_skip())
        return
    await _finalize_title(msg.answer, state, config, description or None)


@router.callback_query(EventSetupFlow.description, F.data == "setup_desc_skip")
async def cb_setup_desc_skip(cb: CallbackQuery, state: FSMContext, config: EventRsvpConfig):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    await _finalize_title(cb.message.answer, state, config, None)


@router.callback_query(F.data == "setup_date")
async def cb_setup_date(cb: CallbackQuery, state: FSMContext, config: EventRsvpConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    await state.set_state(EventSetupFlow.event_date)
    await state.update_data(started_at=time.time())
    await cb.message.edit_text("📅 Введите дату мероприятия (в любом удобном формате):", reply_markup=kb_flow_cancel())


@router.message(EventSetupFlow.event_date, F.text, ~F.text.startswith("/"))
async def setup_date(msg: Message, state: FSMContext, config: EventRsvpConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    event_date = msg.text.strip()
    if not event_date:
        await msg.answer("Дата не может быть пустой. Введите дату:", reply_markup=kb_flow_cancel())
        return
    if len(event_date) > 100:
        await msg.answer("⚠️ Слишком длинная дата. Введите короче 100 символов:", reply_markup=kb_flow_cancel())
        return
    await state.clear()
    async with aiosqlite.connect(config.db_path) as db:
        await _ensure_event_row(db)
        await db.execute("UPDATE event_details SET event_date=? WHERE id=1", (event_date,))
        await db.commit()
    await msg.answer(f"✅ Дата обновлена: {_esc(event_date)}", parse_mode="HTML", reply_markup=kb_setup_menu())


@router.callback_query(F.data == "setup_capacity")
async def cb_setup_capacity(cb: CallbackQuery, state: FSMContext, config: EventRsvpConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    await state.set_state(EventSetupFlow.capacity)
    await state.update_data(started_at=time.time())
    await cb.message.edit_text("🔢 Введите вместимость (число мест):", reply_markup=kb_flow_cancel())


@router.message(EventSetupFlow.capacity, F.text, ~F.text.startswith("/"))
async def setup_capacity(msg: Message, state: FSMContext, config: EventRsvpConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    capacity = _valid_capacity(msg.text)
    if capacity is None:
        await msg.answer("Введите целое число от 1 до 1000000, например: 100", reply_markup=kb_flow_cancel())
        return
    await state.clear()
    async with aiosqlite.connect(config.db_path) as db:
        await _ensure_event_row(db)
        # Lowering capacity below the current confirmed headcount is allowed —
        # it just means no NEW seats open up until enough cancellations bring
        # occupied back under the new cap; existing confirmed guests are not
        # retroactively bumped to waitlist (that would be a surprising,
        # unrequested side effect of an admin editing a number).
        await db.execute("UPDATE event_details SET capacity=? WHERE id=1", (capacity,))
        await db.commit()
    await msg.answer(f"✅ Вместимость обновлена: {capacity}", reply_markup=kb_setup_menu())


# ── ADMIN: lists ─────────────────────────────────────────────────────────────

@router.callback_query(F.data == "participants_list")
async def cb_participants_list(cb: CallbackQuery, state: FSMContext, config: EventRsvpConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    text = await _participants_list_text(config.db_path)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[kb_back()]]))


@router.callback_query(F.data == "waitlist_list")
async def cb_waitlist_list(cb: CallbackQuery, state: FSMContext, config: EventRsvpConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    text = await _waitlist_text(config.db_path)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[kb_back()]]))


# ── ADMINS menu ────────────────────────────────────────────────────────────────

async def _admins_list_text(config: EventRsvpConfig) -> str:
    ids = sorted(_load_admins(config.admins_file))
    if not ids:
        return "👥 Пусто"
    return _join_bounded(["👥 <b>Администраторы бота:</b>\n"] + [f"• <code>{_esc(i)}</code>" for i in ids])


@router.callback_query(F.data == "adm_menu")
async def cb_adm_menu(cb: CallbackQuery, state: FSMContext, config: EventRsvpConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    text = await _admins_list_text(config)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_admins_menu())


@router.callback_query(F.data == "adm_add")
async def cb_adm_add(cb: CallbackQuery, state: FSMContext, config: EventRsvpConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.set_state(AdminMgmtFlow.add_admin)
    await state.update_data(started_at=time.time())
    await cb.message.edit_text("Введите Telegram ID нового администратора:", reply_markup=kb_flow_cancel())


@router.message(AdminMgmtFlow.add_admin, F.text, ~F.text.startswith("/"))
async def admin_add_id(msg: Message, state: FSMContext, config: EventRsvpConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    text = msg.text.strip()
    if not _valid_admin_id(text):
        await msg.answer("Некорректный ID. Введите числовой Telegram ID.", reply_markup=kb_flow_cancel())
        return
    await state.clear()
    ids = _load_admins(config.admins_file)
    ids.add(text)
    _save_admins(config.admins_file, ids)
    if config.bot_id is not None:
        try:
            await add_bot_admin(config.bot_id, text)
        except Exception as e:
            logger.warning(f"admin_add_id: add_bot_admin sync failed for bot {config.bot_id}: {e}")
    await msg.answer(f"✅ <code>{text}</code> добавлен.", parse_mode="HTML", reply_markup=kb_admins_menu())


@router.callback_query(F.data == "adm_remove")
async def cb_adm_remove(cb: CallbackQuery, state: FSMContext, config: EventRsvpConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    ids = sorted(_load_admins(config.admins_file))
    if len(ids) <= 1:
        await cb.message.edit_text("Нельзя удалить последнего администратора.", reply_markup=kb_admins_menu())
        return
    if len(ids) > MAX_ADMIN_REMOVE_BUTTONS:
        await cb.message.edit_text(
            "Слишком много админов для списка кнопок. Обратитесь к разработчику.", reply_markup=kb_admins_menu()
        )
        return
    await state.set_state(AdminMgmtFlow.remove_admin_pick)
    await state.update_data(started_at=time.time(), remove_admin_ids=ids)
    await cb.message.edit_text("Выберите администратора для удаления:", reply_markup=kb_remove_admins(ids))


@router.callback_query(AdminMgmtFlow.remove_admin_pick, F.data.startswith("adm_rm:"))
async def cb_adm_remove_pick(cb: CallbackQuery, state: FSMContext, config: EventRsvpConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    try:
        idx = int(cb.data.split(":", 1)[1])
        target = data["remove_admin_ids"][idx]
    except (ValueError, IndexError, KeyError):
        await state.clear()
        await cb.message.edit_text("Некорректный выбор.", reply_markup=kb_admins_menu())
        return
    ids = _load_admins(config.admins_file)
    if len(ids) <= 1:
        await state.clear()
        await cb.message.edit_text("Нельзя удалить последнего администратора.", reply_markup=kb_admins_menu())
        return
    ids.discard(target)
    _save_admins(config.admins_file, ids)
    if config.bot_id is not None:
        try:
            await remove_bot_admin(config.bot_id, target)
        except Exception as e:
            logger.warning(f"cb_adm_remove_pick: remove_bot_admin sync failed for bot {config.bot_id}: {e}")
    await state.clear()
    await cb.message.edit_text(f"✅ <code>{_esc(target)}</code> удалён.", parse_mode="HTML",
                                reply_markup=kb_admins_menu())


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
