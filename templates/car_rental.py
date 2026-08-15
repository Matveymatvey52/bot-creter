# TEMPLATE: car_rental
# USE FOR: прокат конкретной единицы техники (авто, мото, велосипед, инструмент) на диапазон дат — бронирование с проверкой пересечения дат по конкретной единице, а не по временным слотам
# CUSTOMIZE: sections marked with # CUSTOMIZE
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import aiosqlite
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove,
)

# BASE_URL/PORT are process-wide, same treatment as templates/tour_operator.py
# (see that template's own comment) — every bot in the shared webhook process
# reads the same values, so these stay module-level env reads rather than
# CarRentalConfig fields.
PORT           = int(os.getenv("PORT", "8080"))
RAILWAY_DOMAIN = os.getenv("RAILWAY_PUBLIC_DOMAIN", "")
BASE_URL       = f"https://{RAILWAY_DOMAIN}" if RAILWAY_DOMAIN else f"http://localhost:{PORT}"

# ── CUSTOMIZE ────────────────────────────────────────────────────────────────
# Same status as every other template's CUSTOMIZE block: per-file source-text
# customization Claude edits when generating a specific bot, not per-bot
# runtime state (that's config.db_path/admins_file below).
BOT_DESCRIPTION = "Прокат техники (авто/мото/велосипед/инструмент): бронирование конкретной единицы на диапазон дат с проверкой пересечения дат, статус-флоу заявок, уведомление администратора о новой брони."
WELCOME_TEXT = (
    "🚗 <b>Прокат техники</b>\n\n"
    "Бронирование конкретной единицы техники на диапазон дат. Пересечение "
    "дат проверяется по каждой единице отдельно.\n\nВыберите действие:"
)
CUSTOMER_WELCOME_TEXT = (
    "👋 Здравствуйте!\n\n"
    "Забронируйте технику на нужные даты, или посмотрите свои действующие брони."
)
STATUS_LABELS = {
    "pending": "🕐 Ожидает подтверждения",
    "confirmed": "✅ Подтверждена",
    "cancelled": "❌ Отклонена",
    "completed": "✔️ Завершена",
}
# ── END CUSTOMIZE ─────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
router = Router()

# Explicit forward-only flow (same shape as vehicle_service.py's
# STATUS_TRANSITIONS): pending -> confirmed -> completed, with cancellation
# reachable as a side-branch from any non-terminal status.
STATUS_TRANSITIONS = {
    "pending": ["confirmed", "cancelled"],
    "confirmed": ["completed", "cancelled"],
    "cancelled": [],
    "completed": [],
}

# Only these statuses actually hold a unit for a date range — cancelled/
# completed bookings must not block a new overlapping booking.
BLOCKING_STATUSES = ("pending", "confirmed")


# ── mini-app config (see docs/MINIAPP_DESIGN.md, templates/tour_operator.py's
# own miniapp_config for the pilot) ─────────────────────────────────────────
# Declarative schema read by runtime/miniapp_api.py's generic CRUD handlers —
# NOT executable code (see that module's docstring). `table` and each field's
# `name` must match init_db()'s CREATE TABLE columns above exactly —
# miniapp_api.py builds SQL directly from these names (values are always
# bound as parameters, never interpolated; only the column/table identifiers
# come from this trusted, hand-authored dict, never from a request).
miniapp_config = {
    "resources": [
        {
            "name": "cars",
            "table": "rental_items",
            "order_by": "id DESC",
            "creatable": True,
            "title": "Автомобили",
            "titleField": "name",
            "fields": [
                {"name": "name", "required": True, "label": "Название", "kind": "text", "list": False, "detail": False, "create": True},
                {"name": "description", "label": "Описание", "kind": "text", "list": True, "detail": True, "create": True},
                {"name": "price_per_day", "label": "Цена/день", "kind": "number", "list": True, "detail": True, "create": True},
                {"name": "active", "label": "Активен", "kind": "status", "list": True, "detail": True, "create": True},
            ],
        },
        {
            "name": "bookings",
            "table": "rental_bookings",
            "order_by": "created_at DESC",
            "creatable": True,
            "title": "Бронирования",
            "titleField": "client_name",
            "fields": [
                {"name": "item_id", "required": True, "label": "ID авто", "kind": "number", "list": False, "detail": False, "create": True},
                {"name": "client_user_id", "required": True, "label": "ID клиента", "kind": "number", "list": False, "detail": False, "create": True},
                {"name": "client_name", "label": "Имя клиента", "kind": "text", "list": False, "detail": True, "create": True},
                {"name": "client_phone", "label": "Телефон", "kind": "text", "list": False, "detail": True, "create": True},
                {"name": "start_date", "required": True, "label": "Начало", "kind": "date", "list": True, "detail": True, "create": True},
                {"name": "end_date", "required": True, "label": "Окончание", "kind": "date", "list": True, "detail": True, "create": True},
                {"name": "status", "label": "Статус", "kind": "status", "list": True, "detail": True, "create": False},
                {"name": "created_at", "creatable": False, "label": "Создано", "kind": "date", "list": False, "detail": False, "create": False},
            ],
        },
    ],
}


# ── config ───────────────────────────────────────────────────────────────────
# Same pattern as every other template — see docs/STAGE2_DESIGN.md.

@dataclass
class CarRentalConfig:
    bot_name: str
    db_path: str
    admins_file: Path
    welcome_image: Path
    display_name: str | None = None
    group_chat_id: str | None = None
    # Only set in webhook mode (config_from_bot_row) — standalone mode has no
    # bots-table row to source it from. Used by _miniapp_url() to scope a
    # magic-link token to this bot; None means no mini-app link can be
    # minted (same as templates/tour_operator.py's CarRentalConfig.bot_id).
    bot_id: int | None = None


def _paths_for(name: str, data_dir: Path) -> CarRentalConfig:
    return CarRentalConfig(
        bot_name=name,
        db_path=str(data_dir / f"{name}_data.db"),
        admins_file=data_dir / f"admins_{name}.json",
        welcome_image=data_dir / "bot_images" / f"{name}.jpg",
    )


def config_from_env() -> CarRentalConfig:
    """Standalone/subprocess mode."""
    name = Path(__file__).stem
    data_dir = Path(os.getenv("DATA_DIR", "./data"))
    data_dir.mkdir(exist_ok=True)
    return _paths_for(name, data_dir)


def config_from_bot_row(bot_row: dict, data_dir: Path) -> CarRentalConfig:
    """Webhook runtime mode. Paths built from bot_row["bot_id"] (bots.id, the
    physically unique AUTOINCREMENT PK) — NOT bot_row["name"] — same reasoning
    as every other template's config_from_bot_row (see docs/STAGE2_DESIGN.md
    "Изоляция по bots.id")."""
    bot_id = bot_row["bot_id"]
    config = CarRentalConfig(
        bot_name=bot_row["name"],
        db_path=str(data_dir / f"bot_{bot_id}_data.db"),
        admins_file=data_dir / f"admins_{bot_id}.json",
        welcome_image=data_dir / "bot_images" / f"bot_{bot_id}.jpg",
    )
    config.display_name = bot_row.get("display_name")
    config.group_chat_id = bot_row.get("group_chat_id")
    config.bot_id = bot_id
    return config


class ConfigMiddleware(BaseMiddleware):
    """Injects this bot's CarRentalConfig into data["config"]."""

    def __init__(self, config: CarRentalConfig) -> None:
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

def _is_admin(user_id: int, config: CarRentalConfig) -> bool:
    return str(user_id) in _load_admins(config.admins_file)


def _esc(value, max_len: int = 500) -> str:
    """HTML-escapes AND length-bounds any user-supplied text before it goes into
    a parse_mode="HTML" message — same helper/rationale as templates/
    vehicle_service.py's _esc()."""
    text = str(value) if value is not None else ""
    if len(text) > max_len:
        text = text[:max_len] + "…"
    return html.escape(text)


def _join_bounded(lines: list[str], limit: int = 3500) -> str:
    """Joins lines with a length budget, dropping only WHOLE trailing lines —
    same rationale as templates/vehicle_service.py's _join_bounded()."""
    out: list[str] = []
    total = 0
    for line in lines:
        if total + len(line) + 1 > limit:
            out.append("…")
            break
        out.append(line)
        total += len(line) + 1
    return "\n".join(out)


# ── phone normalization ────────────────────────────────────────────────────────
# Same RU-phone formula as templates/vehicle_service.py's _normalize_phone(),
# reused verbatim per the design brief.

def _normalize_phone(raw: str) -> str | None:
    digits = re.sub(r"[^\d]", "", raw)
    if len(digits) == 11 and digits[0] in ("7", "8"):
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    else:
        return None
    return f"+{digits[0]} ({digits[1:4]}) {digits[4:7]}-{digits[7:9]}-{digits[9:11]}"


# ── date parsing/validation ────────────────────────────────────────────────────
# YYYY-MM-DD only, per the required schema. A max horizon bounds how far out a
# client can book, so a typo like "2099-01-01" doesn't create a booking that
# blocks the unit for decades.
MAX_BOOKING_HORIZON_DAYS = 3650  # ~10 years

def _parse_date(text: str) -> str | None:
    text = text.strip()
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None
    today = datetime.now().date()
    if parsed < today or parsed > today + timedelta(days=MAX_BOOKING_HORIZON_DAYS):
        return None
    return parsed.isoformat()


def _parse_price(text: str) -> int | None:
    try:
        price = int(text.strip())
    except ValueError:
        return None
    if price < 0 or price > 1_000_000_000:
        return None
    return price


def _valid_admin_id(text: str) -> bool:
    """Same guard as templates/vehicle_service.py's _valid_admin_id()."""
    if not (bool(text) and text.isascii() and text.isdigit() and len(text) <= 15):
        return False
    return int(text) > 0 and str(int(text)) == text


# ── db ────────────────────────────────────────────────────────────────────────

async def init_db(db_path: str):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS rental_items (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                name           TEXT NOT NULL,
                description    TEXT,
                price_per_day  INTEGER,
                active         INTEGER DEFAULT 1
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS rental_bookings (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id          INTEGER NOT NULL,
                client_user_id   INTEGER NOT NULL,
                client_name      TEXT,
                client_phone     TEXT,
                start_date       TEXT NOT NULL,
                end_date         TEXT NOT NULL,
                status           TEXT NOT NULL DEFAULT 'pending'
                                 CHECK(status IN ('pending','confirmed','cancelled','completed')),
                created_at       TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_bookings_item ON rental_bookings(item_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_bookings_client ON rental_bookings(client_user_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_bookings_status ON rental_bookings(status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_items_active ON rental_items(active)")
        await db.commit()


# ── booking engine: race-safe overlap check + insert ──────────────────────────
# BEGIN IMMEDIATE takes SQLite's write lock before the read-decide-write — same
# class of protection as templates/booking_fitness.py's _book_slot,
# templates/event_manager.py's link flow, templates/moderator.py's
# _apply_escalation and templates/shop_catalog.py's checkout: two concurrent
# clients tapping "confirm" for the SAME item with overlapping date ranges at
# the same moment cannot both read "no overlap" and both get inserted. The
# lock is released at commit(), before any Telegram API call the caller makes
# with the result.

async def _create_booking_if_free(
    db_path: str, item_id: int, client_user_id: int, client_name: str | None,
    client_phone: str | None, start_date: str, end_date: str,
) -> dict:
    """Returns {"ok": True, "booking_id": int} or
    {"ok": False, "error": "overlap"|"item_gone"}."""
    async with aiosqlite.connect(db_path, timeout=10) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        item = await (await db.execute(
            "SELECT id FROM rental_items WHERE id=? AND active=1", (item_id,)
        )).fetchone()
        if item is None:
            await db.commit()
            return {"ok": False, "error": "item_gone"}

        # Standard inclusive range overlap: NOT (new_end < existing_start OR
        # new_start > existing_end). Only pending/confirmed bookings block —
        # cancelled/completed don't hold the unit.
        placeholders = ",".join("?" * len(BLOCKING_STATUSES))
        conflict = await (await db.execute(
            f"SELECT id FROM rental_bookings WHERE item_id=? AND status IN ({placeholders}) "
            "AND NOT (?<start_date OR ?>end_date) LIMIT 1",
            (item_id, *BLOCKING_STATUSES, end_date, start_date),
        )).fetchone()
        if conflict is not None:
            await db.commit()
            return {"ok": False, "error": "overlap"}

        cur = await db.execute(
            "INSERT INTO rental_bookings "
            "(item_id, client_user_id, client_name, client_phone, start_date, end_date, status) "
            "VALUES (?,?,?,?,?,?,'pending')",
            (item_id, client_user_id, client_name, client_phone, start_date, end_date),
        )
        booking_id = cur.lastrowid
        await db.commit()
        return {"ok": True, "booking_id": booking_id}


# ── FSM staleness guard ─────────────────────────────────────────────────────────
# Same mechanic as templates/vehicle_service.py's FLOW_TIMEOUT_SECONDS/_flow_expired.
FLOW_TIMEOUT_SECONDS = 300

def _flow_expired(data: dict) -> bool:
    started_at = data.get("started_at")
    return started_at is None or (time.time() - started_at) > FLOW_TIMEOUT_SECONDS


# ── FSM states ───────────────────────────────────────────────────────────────

class BookingFlow(StatesGroup):
    pick_item = State()
    start_date = State()
    end_date = State()
    phone = State()

class ItemFlow(StatesGroup):
    name = State()
    price = State()

class AdminMgmtFlow(StatesGroup):
    add_admin = State()
    remove_admin_pick = State()


# ── keyboards ─────────────────────────────────────────────────────────────────

def kb_main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Брони", callback_data="bk_menu")],
        [InlineKeyboardButton(text="🚗 Техника", callback_data="it_menu")],
        [InlineKeyboardButton(text="👥 Админы", callback_data="adm_menu")],
        [InlineKeyboardButton(text="🌐 Веб-приложение", callback_data="miniapp_link")],
    ])

def kb_client_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚗 Забронировать", callback_data="cli_book")],
        [InlineKeyboardButton(text="📋 Мои брони", callback_data="cli_my")],
    ])

def kb_back(callback_data: str = "main_menu") -> InlineKeyboardButton:
    return InlineKeyboardButton(text="◀️ Назад", callback_data=callback_data)

def kb_flow_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="flow_cancel")],
    ])

MAX_LIST_BUTTONS = 25

# ── client booking flow ──
def kb_item_pick(rows: list[tuple]) -> InlineKeyboardMarkup:
    btns = [
        [InlineKeyboardButton(
            text=f"{_esc(name, 30)}" + (f" · {price}/день" if price is not None else ""),
            callback_data=f"cli_pick:{iid}",
        )]
        for iid, name, price in rows[:MAX_LIST_BUTTONS]
    ]
    btns.append([InlineKeyboardButton(text="❌ Отмена", callback_data="flow_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=btns)

def kb_contact_request() -> ReplyKeyboardMarkup:
    # request_contact is a ReplyKeyboardMarkup-only capability — same
    # exception to the inline-panel style as vehicle_service.py.
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Поделиться номером", request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=True,
    )

# ── admin bookings menu ──
def kb_bookings_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Все брони", callback_data="bk_list")],
        [kb_back()],
    ])

_STATUS_FILTERS = [
    ("pending", "🕐 Ожидают"), ("confirmed", "✅ Подтверждённые"),
    ("completed", "✔️ Завершённые"), ("cancelled", "❌ Отклонённые"), ("all", "📋 Все"),
]

def kb_status_filters() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=label, callback_data=f"bk_filter:{code}")]
            for code, label in _STATUS_FILTERS]
    rows.append([kb_back("bk_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_booking_list(rows: list[tuple]) -> InlineKeyboardMarkup:
    btns = [
        [InlineKeyboardButton(
            text=f"№{bid} · {STATUS_LABELS.get(status, status)} · {_esc(item_name, 20)} · {sd}—{ed}",
            callback_data=f"bk_view:{bid}",
        )]
        for bid, status, item_name, sd, ed in rows[:MAX_LIST_BUTTONS]
    ]
    btns.append([kb_back("bk_list")])
    return InlineKeyboardMarkup(inline_keyboard=btns)

def kb_booking_detail(booking_id: int, status: str) -> InlineKeyboardMarkup:
    rows = []
    for target in STATUS_TRANSITIONS.get(status, []):
        if target == "cancelled":
            icon = "❌ Отклонить"
        elif target == "confirmed":
            icon = "✅ Подтвердить"
        elif target == "completed":
            icon = "✔️ Завершить"
        else:
            icon = f"▶️ {STATUS_LABELS.get(target, target)}"
        rows.append([InlineKeyboardButton(text=icon, callback_data=f"bk_status:{booking_id}:{target}")])
    rows.append([kb_back("bk_list")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

# ── admin items (техника) menu ──
def kb_items_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить технику", callback_data="it_new")],
        [InlineKeyboardButton(text="📋 Список техники", callback_data="it_list")],
        [kb_back()],
    ])

def kb_item_list(rows: list[tuple]) -> InlineKeyboardMarkup:
    btns = [
        [InlineKeyboardButton(text=f"{_esc(name, 30)}" + (f" · {price}/день" if price is not None else ""),
                               callback_data=f"it_view:{iid}")]
        for iid, name, price in rows[:MAX_LIST_BUTTONS]
    ]
    btns.append([kb_back("it_menu")])
    return InlineKeyboardMarkup(inline_keyboard=btns)

def kb_item_detail(item_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Удалить (сделать неактивной)", callback_data=f"it_deactivate:{item_id}")],
        [kb_back("it_list")],
    ])

def kb_price_skip() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭ Без цены", callback_data="it_price_skip")],
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

async def _booking_detail_text(db_path: str, booking_id: int, extra_note: str | None = None) -> str | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            "SELECT b.*, i.name AS item_name, i.price_per_day "
            "FROM rental_bookings b JOIN rental_items i ON b.item_id = i.id WHERE b.id=?",
            (booking_id,),
        )).fetchone()
    if not row:
        return None
    lines = [
        f"📅 <b>Бронь №{row['id']}</b> · {STATUS_LABELS.get(row['status'], row['status'])}\n",
        f"🚗 {_esc(row['item_name'])}",
        f"🗓 {row['start_date']} — {row['end_date']}",
        f"👤 {_esc(row['client_name']) if row['client_name'] else '—'}"
        + (f" · {_esc(row['client_phone'])}" if row['client_phone'] else ""),
        f"🕐 Создана: {row['created_at']}",
    ]
    if extra_note:
        lines.append(f"\n{extra_note}")
    return _join_bounded(lines)


# ── /start ────────────────────────────────────────────────────────────────────

@router.message(Command("start"), F.chat.type == "private")
async def cmd_start(message: Message, state: FSMContext, config: CarRentalConfig):
    # Same reasoning as vehicle_service.py's cmd_start: /start must reset any
    # dangling mid-flow FSM state before showing a menu.
    await state.clear()
    admins = _load_admins(config.admins_file)
    first_time_admin = not admins
    if first_time_admin:
        _save_admins(config.admins_file, {str(message.from_user.id)})
        admins = {str(message.from_user.id)}

    if str(message.from_user.id) in admins:
        if config.welcome_image.exists():
            await message.answer_photo(
                FSInputFile(str(config.welcome_image)), caption=WELCOME_TEXT,
                parse_mode="HTML", reply_markup=kb_main_menu(),
            )
        else:
            await message.answer(WELCOME_TEXT, parse_mode="HTML", reply_markup=kb_main_menu())
        if first_time_admin:
            await message.answer(
                "👑 <b>Вы — администратор этого бота.</b>\n\n"
                "Управление другими администраторами — кнопка «👥 Админы» выше.",
                parse_mode="HTML",
            )
    else:
        await message.answer(CUSTOMER_WELCOME_TEXT, parse_mode="HTML", reply_markup=kb_client_menu())


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
async def cmd_app(m: Message, config: CarRentalConfig):
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
async def cb_miniapp_link(cb: CallbackQuery, config: CarRentalConfig):
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
async def cb_main_menu(cb: CallbackQuery, state: FSMContext, config: CarRentalConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    await cb.message.edit_text(WELCOME_TEXT, parse_mode="HTML", reply_markup=kb_main_menu())


@router.callback_query(F.data == "flow_cancel")
async def cb_flow_cancel(cb: CallbackQuery, state: FSMContext, config: CarRentalConfig):
    await cb.answer()
    await state.clear()
    if _is_admin(cb.from_user.id, config):
        await cb.message.edit_text("Отменено.", reply_markup=kb_main_menu())
    else:
        await cb.message.edit_text("Отменено.", reply_markup=kb_client_menu())


# ── CLIENT: booking flow ────────────────────────────────────────────────────

@router.callback_query(F.data == "cli_book")
async def cb_cli_book(cb: CallbackQuery, state: FSMContext, config: CarRentalConfig):
    await cb.answer()
    await state.clear()
    async with aiosqlite.connect(config.db_path) as db:
        rows = await (await db.execute(
            "SELECT id, name, price_per_day FROM rental_items WHERE active=1 ORDER BY id LIMIT ?",
            (MAX_LIST_BUTTONS,),
        )).fetchall()
    if not rows:
        await cb.message.edit_text("😔 Пока нет доступной техники для брони.", reply_markup=kb_client_menu())
        return
    await state.set_state(BookingFlow.pick_item)
    await state.update_data(started_at=time.time())
    await cb.message.edit_text("🚗 Выберите технику для брони:", reply_markup=kb_item_pick(rows))


@router.callback_query(BookingFlow.pick_item, F.data.startswith("cli_pick:"))
async def cb_cli_pick(cb: CallbackQuery, state: FSMContext, config: CarRentalConfig):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_client_menu())
        return
    try:
        item_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute(
            "SELECT id, name FROM rental_items WHERE id=? AND active=1", (item_id,)
        )).fetchone()
    if not row:
        await cb.message.edit_text("Эта техника больше недоступна.", reply_markup=kb_client_menu())
        return
    await state.update_data(item_id=item_id, item_name=row[1])
    await state.set_state(BookingFlow.start_date)
    await cb.message.edit_text(
        f"✅ Выбрано: {_esc(row[1])}\n\n🗓 Введите дату начала аренды в формате ГГГГ-ММ-ДД:",
        parse_mode="HTML", reply_markup=kb_flow_cancel(),
    )


@router.message(BookingFlow.start_date, F.text, ~F.text.startswith("/"))
async def booking_start_date(msg: Message, state: FSMContext):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_client_menu())
        return
    start = _parse_date(msg.text)
    if start is None:
        await msg.answer(
            "❌ Неверная дата. Введите дату начала в формате ГГГГ-ММ-ДД (не раньше сегодняшнего дня), "
            "например: 2026-09-01", reply_markup=kb_flow_cancel(),
        )
        return
    await state.update_data(start_date=start)
    await state.set_state(BookingFlow.end_date)
    await msg.answer("🗓 Введите дату окончания аренды в формате ГГГГ-ММ-ДД:", reply_markup=kb_flow_cancel())


@router.message(BookingFlow.end_date, F.text, ~F.text.startswith("/"))
async def booking_end_date(msg: Message, state: FSMContext):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_client_menu())
        return
    end = _parse_date(msg.text)
    if end is None:
        await msg.answer(
            "❌ Неверная дата. Введите дату окончания в формате ГГГГ-ММ-ДД, например: 2026-09-05",
            reply_markup=kb_flow_cancel(),
        )
        return
    start = data.get("start_date")
    if not start or end < start:
        await msg.answer(
            "❌ Дата окончания не может быть раньше даты начала. Введите дату окончания заново:",
            reply_markup=kb_flow_cancel(),
        )
        return
    await state.update_data(end_date=end)
    await state.set_state(BookingFlow.phone)
    await msg.answer(
        "📱 Поделитесь номером телефона для подтверждения брони:", reply_markup=kb_contact_request(),
    )


@router.message(BookingFlow.phone, F.contact, F.chat.type == "private")
async def booking_phone_contact(msg: Message, state: FSMContext, config: CarRentalConfig, bot: Bot):
    contact = msg.contact
    # Security-critical: a ReplyKeyboardMarkup request_contact button always
    # sends the PRESSER's own phone number, but a hand-crafted client could in
    # principle submit an arbitrary Contact object — same rationale as
    # templates/vehicle_service.py's on_contact.
    if contact.user_id != msg.from_user.id:
        await msg.answer(
            "Пожалуйста, поделитесь именно СВОИМ номером через кнопку ниже.",
            reply_markup=kb_contact_request(),
        )
        return
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_client_menu(), )
        return
    phone = _normalize_phone(contact.phone_number)
    if phone is None:
        await msg.answer("Не удалось распознать номер.", reply_markup=kb_contact_request())
        return
    await _finalize_booking(msg, state, config, bot, phone)


async def _finalize_booking(msg: Message, state: FSMContext, config: CarRentalConfig, bot: Bot, phone: str) -> None:
    data = await state.get_data()
    item_id = data.get("item_id")
    item_name = data.get("item_name")
    start = data.get("start_date")
    end = data.get("end_date")
    if not (item_id and start and end):
        await state.clear()
        await msg.answer("Сессия устарела, начните заново.", reply_markup=ReplyKeyboardRemove())
        await msg.answer("Главное меню:", reply_markup=kb_client_menu())
        return
    await state.clear()
    client_name = msg.from_user.full_name

    result = await _create_booking_if_free(
        config.db_path, item_id, msg.from_user.id, client_name, phone, start, end,
    )
    if not result["ok"]:
        if result["error"] == "overlap":
            text = (
                f"😔 Извините, {_esc(item_name)} уже забронирована на пересекающиеся даты "
                f"({start} — {end}). Попробуйте другие даты."
            )
        else:
            text = "😔 Эта техника больше недоступна."
        await msg.answer(text, parse_mode="HTML", reply_markup=ReplyKeyboardRemove())
        await msg.answer("Главное меню:", reply_markup=kb_client_menu())
        return

    booking_id = result["booking_id"]
    detail = await _booking_detail_text(config.db_path, booking_id)
    await msg.answer("✅ Заявка на бронь создана и ждёт подтверждения администратором!",
                      reply_markup=ReplyKeyboardRemove())
    await msg.answer(detail, parse_mode="HTML", reply_markup=kb_client_menu())

    for admin_id in _load_admins(config.admins_file):
        try:
            await bot.send_message(
                int(admin_id), f"🔔 <b>Новая заявка на бронь!</b>\n\n{detail}",
                parse_mode="HTML", reply_markup=kb_booking_detail(booking_id, "pending"),
            )
        except TelegramAPIError as e:
            logger.warning(f"car_rental: failed to notify admin {admin_id} of new booking: {e}")


# ── CLIENT: "Мои брони" ─────────────────────────────────────────────────────

@router.callback_query(F.data == "cli_my")
async def cb_cli_my(cb: CallbackQuery, config: CarRentalConfig):
    await cb.answer()
    async with aiosqlite.connect(config.db_path) as db:
        rows = await (await db.execute(
            "SELECT b.id, b.status, i.name, b.start_date, b.end_date "
            "FROM rental_bookings b JOIN rental_items i ON b.item_id = i.id "
            "WHERE b.client_user_id=? ORDER BY b.id DESC LIMIT ?",
            (cb.from_user.id, MAX_LIST_BUTTONS),
        )).fetchall()
    if not rows:
        await cb.message.edit_text("У вас пока нет броней.", reply_markup=kb_client_menu())
        return
    lines = ["📋 <b>Ваши брони:</b>\n"]
    for bid, status, name, sd, ed in rows:
        lines.append(f"№{bid} · {STATUS_LABELS.get(status, status)} · {_esc(name)} · {sd} — {ed}")
    await cb.message.edit_text(_join_bounded(lines), parse_mode="HTML", reply_markup=kb_client_menu())


# ── ADMIN: bookings menu ────────────────────────────────────────────────────

@router.callback_query(F.data == "bk_menu")
async def cb_bk_menu(cb: CallbackQuery, state: FSMContext, config: CarRentalConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    await cb.message.edit_text("📅 <b>Брони</b>", parse_mode="HTML", reply_markup=kb_bookings_menu())


@router.callback_query(F.data == "bk_list")
async def cb_bk_list(cb: CallbackQuery, config: CarRentalConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await cb.message.edit_text("Выберите фильтр по статусу:", reply_markup=kb_status_filters())


@router.callback_query(F.data.startswith("bk_filter:"))
async def cb_bk_filter(cb: CallbackQuery, config: CarRentalConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    status = cb.data.split(":", 1)[1]
    base_sql = (
        "SELECT b.id, b.status, i.name, b.start_date, b.end_date "
        "FROM rental_bookings b JOIN rental_items i ON b.item_id = i.id "
    )
    async with aiosqlite.connect(config.db_path) as db:
        if status == "all":
            rows = await (await db.execute(
                base_sql + "ORDER BY b.id DESC LIMIT ?", (MAX_LIST_BUTTONS,)
            )).fetchall()
        else:
            rows = await (await db.execute(
                base_sql + "WHERE b.status=? ORDER BY b.id DESC LIMIT ?", (status, MAX_LIST_BUTTONS)
            )).fetchall()
    if not rows:
        await cb.message.edit_text("Броней не найдено.", reply_markup=kb_status_filters())
        return
    await cb.message.edit_text(f"📋 Брони (последние {len(rows)}):", reply_markup=kb_booking_list(rows))


@router.callback_query(F.data.startswith("bk_view:"))
async def cb_bk_view(cb: CallbackQuery, config: CarRentalConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        booking_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute("SELECT status FROM rental_bookings WHERE id=?", (booking_id,))).fetchone()
    if not row:
        await cb.message.edit_text("Бронь не найдена.", reply_markup=kb_status_filters())
        return
    text = await _booking_detail_text(config.db_path, booking_id)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_booking_detail(booking_id, row[0]))


@router.callback_query(F.data.startswith("bk_status:"))
async def cb_bk_status(cb: CallbackQuery, bot: Bot, config: CarRentalConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        _, booking_id_s, new_status = cb.data.split(":", 2)
        booking_id = int(booking_id_s)
    except ValueError:
        return
    if new_status not in STATUS_LABELS:
        return

    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute(
            "SELECT status FROM rental_bookings WHERE id=?", (booking_id,)
        )).fetchone()
        if not row:
            await cb.message.edit_text("Бронь не найдена.", reply_markup=kb_status_filters())
            return
        old_status = row[0]
        if new_status not in STATUS_TRANSITIONS.get(old_status, []):
            # Stale button (already transitioned by another admin, or a
            # double-tap on an already-applied transition) — re-render instead
            # of silently no-op'ing, same principle as the compare-and-swap
            # UPDATE below.
            text = await _booking_detail_text(config.db_path, booking_id)
            await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_booking_detail(booking_id, old_status))
            return
        # Compare-and-swap: WHERE status=old_status makes a double-tap (two
        # callbacks racing on the same stale keyboard) a no-op on the second
        # write — same principle as templates/vehicle_service.py's cb_req_status.
        cur = await db.execute(
            "UPDATE rental_bookings SET status=? WHERE id=? AND status=?",
            (new_status, booking_id, old_status),
        )
        await db.commit()
        if cur.rowcount == 0:
            text = await _booking_detail_text(config.db_path, booking_id)
            await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_booking_detail(booking_id, new_status))
            return
        client_row = await (await db.execute(
            "SELECT client_user_id FROM rental_bookings WHERE id=?", (booking_id,)
        )).fetchone()

    text = await _booking_detail_text(config.db_path, booking_id)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_booking_detail(booking_id, new_status))

    client_user_id = client_row[0] if client_row else None
    if client_user_id:
        try:
            await bot.send_message(
                client_user_id,
                f"ℹ️ Статус вашей брони №{booking_id} изменён: {STATUS_LABELS.get(new_status, new_status)}",
            )
        except TelegramAPIError as e:
            logger.warning(f"car_rental: failed to notify client for booking {booking_id}: {e}")


# ── ADMIN: rental_items (техника) CRUD ──────────────────────────────────────

@router.callback_query(F.data == "it_menu")
async def cb_it_menu(cb: CallbackQuery, state: FSMContext, config: CarRentalConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    await cb.message.edit_text("🚗 <b>Техника</b>", parse_mode="HTML", reply_markup=kb_items_menu())


@router.callback_query(F.data == "it_list")
async def cb_it_list(cb: CallbackQuery, config: CarRentalConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    async with aiosqlite.connect(config.db_path) as db:
        rows = await (await db.execute(
            "SELECT id, name, price_per_day FROM rental_items WHERE active=1 ORDER BY id DESC LIMIT ?",
            (MAX_LIST_BUTTONS,),
        )).fetchall()
    if not rows:
        await cb.message.edit_text("Техники пока нет.", reply_markup=kb_items_menu())
        return
    await cb.message.edit_text("📋 Техника:", reply_markup=kb_item_list(rows))


@router.callback_query(F.data.startswith("it_view:"))
async def cb_it_view(cb: CallbackQuery, config: CarRentalConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        item_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute("SELECT * FROM rental_items WHERE id=?", (item_id,))).fetchone()
    if not row:
        await cb.message.edit_text("Техника не найдена.", reply_markup=kb_items_menu())
        return
    lines = [
        f"🚗 <b>{_esc(row['name'])}</b>\n",
        f"💰 Цена/день: {row['price_per_day']}" if row['price_per_day'] is not None else "💰 Цена не указана",
    ]
    if row["description"]:
        lines.append(f"📝 {_esc(row['description'])}")
    lines.append("🟢 Активна" if row["active"] else "🔴 Неактивна")
    await cb.message.edit_text(_join_bounded(lines), parse_mode="HTML", reply_markup=kb_item_detail(item_id))


@router.callback_query(F.data == "it_new")
async def cb_it_new(cb: CallbackQuery, state: FSMContext, config: CarRentalConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    await state.set_state(ItemFlow.name)
    await state.update_data(started_at=time.time())
    await cb.message.edit_text("🚗 Введите название новой техники:", reply_markup=kb_flow_cancel())


@router.message(ItemFlow.name, F.text, ~F.text.startswith("/"))
async def item_new_name(msg: Message, state: FSMContext):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    name = msg.text.strip()
    if not name:
        await msg.answer("Название не может быть пустым. Введите название техники:", reply_markup=kb_flow_cancel())
        return
    if len(name) > 200:
        await msg.answer("⚠️ Слишком длинное название. Введите название короче 200 символов:",
                          reply_markup=kb_flow_cancel())
        return
    await state.update_data(pending_name=name)
    await state.set_state(ItemFlow.price)
    await msg.answer("💰 Цена за день (или нажмите «Без цены»):", reply_markup=kb_price_skip())


async def _finalize_item(message_answer, state: FSMContext, config: CarRentalConfig, price: int | None) -> None:
    data = await state.get_data()
    name = data.get("pending_name")
    if not name:
        await state.clear()
        await message_answer("Сессия устарела, начните заново.", reply_markup=kb_main_menu())
        return
    await state.clear()
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute(
            "INSERT INTO rental_items (name, price_per_day) VALUES (?, ?)", (name, price),
        )
        await db.commit()
    await message_answer(f"✅ Техника добавлена: {_esc(name)}", parse_mode="HTML", reply_markup=kb_items_menu())


@router.message(ItemFlow.price, F.text, ~F.text.startswith("/"))
async def item_new_price(msg: Message, state: FSMContext, config: CarRentalConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    price = _parse_price(msg.text)
    if price is None:
        await msg.answer("Введите целое число от 0, например: 2000, или нажмите «Без цены»",
                          reply_markup=kb_price_skip())
        return
    await _finalize_item(msg.answer, state, config, price)


@router.callback_query(ItemFlow.price, F.data == "it_price_skip")
async def cb_item_price_skip(cb: CallbackQuery, state: FSMContext, config: CarRentalConfig):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    await _finalize_item(cb.message.answer, state, config, None)


@router.callback_query(F.data.startswith("it_deactivate:"))
async def cb_it_deactivate(cb: CallbackQuery, config: CarRentalConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        item_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute("UPDATE rental_items SET active=0 WHERE id=?", (item_id,))
        await db.commit()
    async with aiosqlite.connect(config.db_path) as db:
        rows = await (await db.execute(
            "SELECT id, name, price_per_day FROM rental_items WHERE active=1 ORDER BY id DESC LIMIT ?",
            (MAX_LIST_BUTTONS,),
        )).fetchall()
    if not rows:
        await cb.message.edit_text("✅ Техника удалена. Активной техники больше нет.", reply_markup=kb_items_menu())
        return
    await cb.message.edit_text("✅ Техника удалена.\n\n📋 Техника:", reply_markup=kb_item_list(rows))


# ── ADMINS menu ────────────────────────────────────────────────────────────────

async def _admins_list_text(config: CarRentalConfig) -> str:
    ids = sorted(_load_admins(config.admins_file))
    if not ids:
        return "👥 Пусто"
    return _join_bounded(["👥 <b>Администраторы бота:</b>\n"] + [f"• <code>{_esc(i)}</code>" for i in ids])


@router.callback_query(F.data == "adm_menu")
async def cb_adm_menu(cb: CallbackQuery, state: FSMContext, config: CarRentalConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    text = await _admins_list_text(config)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_admins_menu())


@router.callback_query(F.data == "adm_add")
async def cb_adm_add(cb: CallbackQuery, state: FSMContext, config: CarRentalConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.set_state(AdminMgmtFlow.add_admin)
    await state.update_data(started_at=time.time())
    await cb.message.edit_text("Введите Telegram ID нового администратора:", reply_markup=kb_flow_cancel())


@router.message(AdminMgmtFlow.add_admin, F.text, ~F.text.startswith("/"))
async def admin_add_id(msg: Message, state: FSMContext, config: CarRentalConfig):
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
    await msg.answer(f"✅ <code>{text}</code> добавлен.", parse_mode="HTML", reply_markup=kb_admins_menu())


@router.callback_query(F.data == "adm_remove")
async def cb_adm_remove(cb: CallbackQuery, state: FSMContext, config: CarRentalConfig):
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
async def cb_adm_remove_pick(cb: CallbackQuery, state: FSMContext, config: CarRentalConfig):
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
