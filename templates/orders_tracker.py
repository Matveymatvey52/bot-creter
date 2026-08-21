# TEMPLATE: orders_tracker
# USE FOR: учёт заказов клиентов, статус-флоу заказа (новый → в работе → отправлен → выполнен), несколько позиций в заказе, уведомление клиента при смене статуса, история заказов по покупателю
# CUSTOMIZE: sections marked with # CUSTOMIZE
from __future__ import annotations

import asyncio
import html
import json
import logging
import math
import os
import re
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
    CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove,
)

from db.database import add_bot_admin, get_bot_sheets_config, remove_bot_admin
from features.sheets import write_row

# ── CUSTOMIZE ────────────────────────────────────────────────────────────────
# Same status as every other template's CUSTOMIZE block: per-file source-text
# customization Claude edits when generating a specific bot, not per-bot
# runtime state (that's config.db_path/admins_file below).
BOT_DESCRIPTION = "Учёт заказов клиентов: статус-флоу, позиции внутри заказа, автоуведомление клиента о смене статуса."
WELCOME_TEXT = (
    "📦 <b>Учёт заказов</b>\n\n"
    "Заказы вводятся вручную (по телефону/из другого канала) — этот бот "
    "только отслеживает их жизненный цикл: новый → в работе → отправлен → "
    "выполнен.\n\nВыберите действие:"
)
CUSTOMER_WELCOME_TEXT = (
    "👋 Здравствуйте!\n\n"
    "Поделитесь своим номером телефона, и я буду присылать вам уведомления "
    "об изменении статуса ваших заказов."
)
STATUS_LABELS = {
    "new": "🆕 Новый",
    "in_progress": "⚙️ В работе",
    "shipped": "🚚 Отправлен",
    "done": "✅ Выполнен",
    "cancelled": "❌ Отменён",
}
# Text sent to the CUSTOMER (not the admin) when their order crosses into this
# status. No entry for "new" — that's the order being created, not a transition.
STATUS_NOTIFY_TEXT = {
    "in_progress": "⚙️ Ваш заказ №{order_id} взят в работу.",
    "shipped": "🚚 Ваш заказ №{order_id} отправлен!",
    "done": "✅ Ваш заказ №{order_id} выполнен. Спасибо за покупку!",
    "cancelled": "❌ Ваш заказ №{order_id} отменён.",
}
# ── END CUSTOMIZE ─────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
router = Router()

# ── mini-app config (see docs/MINIAPP_DESIGN.md, templates/tour_operator.py's
# own miniapp_config for the pilot) ─────────────────────────────────────────
# Declarative schema read by runtime/miniapp_api.py's generic CRUD handlers —
# NOT executable code (see that module's docstring). `table` and each field's
# `name` must match init_db()'s CREATE TABLE columns below exactly.
miniapp_config = {
    "resources": [
        {
            "name": "orders",
            "table": "orders",
            "order_by": "created_at DESC",
            "creatable": True,
            "title": "Заказы",
            "titleField": "notes",
            "fields": [
                {"name": "customer_id", "required": True, "label": "ID клиента", "kind": "number", "list": False, "detail": True, "create": True},
                {"name": "status", "label": "Статус", "kind": "status", "list": True, "detail": True, "create": False},
                {"name": "notes", "label": "Заметки", "kind": "text", "list": True, "detail": True, "create": True},
                {"name": "created_at", "label": "Создан", "kind": "date", "list": True, "detail": True, "create": False},
                {"name": "updated_at", "label": "Обновлён", "kind": "date", "list": False, "detail": True, "create": False},
            ],
        },
        {
            "name": "customers",
            "table": "customers",
            "order_by": "created_at DESC",
            "creatable": True,
            "title": "Клиенты",
            "titleField": "name",
            "fields": [
                {"name": "name", "required": True, "label": "Имя", "kind": "text", "list": True, "detail": True, "create": True},
                {"name": "phone", "required": True, "label": "Телефон", "kind": "text", "list": True, "detail": True, "create": True},
                {"name": "telegram_user_id", "label": "Telegram ID", "kind": "number", "list": False, "detail": True, "create": False},
                {"name": "created_at", "label": "Создан", "kind": "date", "list": False, "detail": True, "create": False},
            ],
        },
    ],
}

# Explicit forward-only flow, as specified in the design: no backward moves.
# "cancelled" is reachable from any non-terminal status (see docs/STAGE2_DESIGN.md
# "Фаза orders_tracker" — added as a side-branch, confirmed with the owner).
STATUS_TRANSITIONS = {
    "new": ["in_progress", "cancelled"],
    "in_progress": ["shipped", "cancelled"],
    "shipped": ["done", "cancelled"],
    "done": [],
    "cancelled": [],
}


# ── config ───────────────────────────────────────────────────────────────────
# Same pattern as every other template — see docs/STAGE2_DESIGN.md.

@dataclass
class OrdersTrackerConfig:
    bot_name: str
    db_path: str
    admins_file: Path
    welcome_image: Path
    display_name: str | None = None
    group_chat_id: str | None = None
    # None in standalone/subprocess mode (config_from_env — no row in `bots`
    # to be an id of); set from bots.id in webhook mode (config_from_bot_row).
    # Threaded into features/sheets.py calls exactly like FixtureConfig.bot_id
    # in tests/fixtures/payment_fixture_template.py — sheets integration is a
    # no-op wherever bot_id is None.
    bot_id: int | None = None
    owner_telegram_id: int | None = None


def _paths_for(name: str, data_dir: Path) -> OrdersTrackerConfig:
    return OrdersTrackerConfig(
        bot_name=name,
        db_path=str(data_dir / f"{name}_data.db"),
        admins_file=data_dir / f"admins_{name}.json",
        welcome_image=data_dir / "bot_images" / f"{name}.jpg",
    )


def config_from_env() -> OrdersTrackerConfig:
    """Standalone/subprocess mode."""
    name = Path(__file__).stem
    data_dir = Path(os.getenv("DATA_DIR", "./data"))
    data_dir.mkdir(exist_ok=True)
    return _paths_for(name, data_dir)


def config_from_bot_row(bot_row: dict, data_dir: Path) -> OrdersTrackerConfig:
    """Webhook runtime mode. Paths built from bot_row["bot_id"] (bots.id, the
    physically unique AUTOINCREMENT PK) — NOT bot_row["name"] — same reasoning
    as every other template's config_from_bot_row (see docs/STAGE2_DESIGN.md
    "Изоляция по bots.id")."""
    bot_id = bot_row["bot_id"]
    config = OrdersTrackerConfig(
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
    """Injects this bot's OrdersTrackerConfig into data["config"]."""

    def __init__(self, config: OrdersTrackerConfig) -> None:
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

def _is_admin(user_id: int, config: OrdersTrackerConfig) -> bool:
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


# ── sheets integration (feature module, see features/sheets.py) ────────────────
# Optional: only active when this bot_id has a row in db.database's
# bot_sheets_config (set by the connect FSM in handlers/manage_bots.py).
# Absent that, every function below is a no-op — orders_tracker works exactly
# as before for bots that never connected the sheets feature.

SHEETS_WORKSHEET = "Заказы"


async def _sheet_config_for(bot_id: int | None) -> dict | None:
    if bot_id is None:
        return None
    return await get_bot_sheets_config(bot_id)


def _sheet_safe(value: str) -> str:
    """Neutralizes spreadsheet-formula injection: customer_name/phone here
    come from free text an admin typed (order_item_name et al. have no
    formula filter — they're meant for product names, not spreadsheet cell
    content). Google Sheets, like Excel, evaluates a cell starting with
    =+-@ as a formula — e.g. an admin (any first /start becomes an admin,
    see cmd_start) entering a customer named '=HYPERLINK("evil","x")' would
    have it silently execute for whoever opens the connected spreadsheet.
    Prefixing with an apostrophe forces Sheets to treat the cell as plain
    text, same mitigation OWASP recommends for CSV injection."""
    if value and value[0] in ("=", "+", "-", "@"):
        return "'" + value
    return value


async def _write_status_to_sheet(
    bot_id: int | None, order_id: int, status: str, customer_name: str, phone: str
) -> None:
    """Appends a row for this status change to the connected spreadsheet, if
    any. Never raises: a Sheets/network failure must not roll back or block
    the status change itself, which has already been committed to the bot's
    own db by the time this is called — same "log, don't propagate" contract
    features/sheets.py's own write_row() already documents at its call site."""
    if bot_id is None:
        return
    try:
        if await get_bot_sheets_config(bot_id) is None:
            return
        await write_row(
            bot_id,
            SHEETS_WORKSHEET,
            # phone is NOT free text — _normalize_phone() always produces
            # "+7 (...) ...-..-.." server-side, so it's not escaped (doing so
            # would defeat the format, since it always starts with "+").
            [order_id, STATUS_LABELS.get(status, status), _sheet_safe(customer_name), phone,
             time.strftime("%Y-%m-%d %H:%M:%S")],
        )
    except Exception:
        logger.error(
            f"orders_tracker: failed to write sheets row for bot_id={bot_id} order={order_id}", exc_info=True
        )


# ── phone normalization ────────────────────────────────────────────────────────
# Same RU-phone formula as templates/booking_beauty.py's _normalize_phone() —
# reused here so a phone typed by the admin when creating a customer and the
# same phone number arriving via Telegram's Contact object (digits only, no
# punctuation) normalize to the IDENTICAL string and can be matched by a plain
# equality lookup.

def _normalize_phone(raw: str) -> str | None:
    digits = re.sub(r"[^\d]", "", raw)
    if len(digits) == 11 and digits[0] in ("7", "8"):
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    else:
        return None
    return f"+{digits[0]} ({digits[1:4]}) {digits[4:7]}-{digits[7:9]}-{digits[9:11]}"


# ── db ────────────────────────────────────────────────────────────────────────

async def init_db(db_path: str):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS customers (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                name              TEXT NOT NULL,
                phone             TEXT NOT NULL UNIQUE,
                telegram_user_id  INTEGER,
                created_at        TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id  INTEGER NOT NULL REFERENCES customers(id),
                status       TEXT NOT NULL DEFAULT 'new'
                             CHECK(status IN ('new','in_progress','shipped','done','cancelled')),
                notes        TEXT,
                created_at   TEXT DEFAULT (datetime('now','localtime')),
                updated_at   TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS order_items (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id  INTEGER NOT NULL REFERENCES orders(id),
                name      TEXT NOT NULL,
                qty       INTEGER NOT NULL DEFAULT 1,
                price     REAL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS order_status_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id    INTEGER NOT NULL,
                old_status  TEXT,
                new_status  TEXT NOT NULL,
                changed_by  INTEGER,
                notified    INTEGER DEFAULT 0,
                changed_at  TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)")
        await db.commit()


# ── FSM staleness guard ─────────────────────────────────────────────────────────
# Same mechanic as templates/moderator.py's FLOW_TIMEOUT_SECONDS/_flow_expired:
# without it, an admin who starts "➕ Новый заказ" and then goes quiet for a
# long time has their next unrelated plain-text message (a phone number
# mentioned to someone else, a stray digit) silently swallowed into a
# half-finished flow from a different day.
FLOW_TIMEOUT_SECONDS = 300

# Review-found: with no cap, the item-adding loop's confirmation message
# (_finalize_item below) could grow past Telegram's ~4096-char limit, making
# the send raise AFTER state was already reset to None — leaving the admin
# stuck with no buttons. Capping the loop keeps that message bounded.
MAX_ITEMS_PER_ORDER = 50


def _flow_expired(data: dict) -> bool:
    started_at = data.get("started_at")
    return started_at is None or (time.time() - started_at) > FLOW_TIMEOUT_SECONDS


# ── FSM states ───────────────────────────────────────────────────────────────

class OrderFlow(StatesGroup):
    find_phone = State()
    item_name = State()
    item_qty = State()
    item_price = State()

class CustomerFlow(StatesGroup):
    phone = State()   # standalone "➕ Новый клиент" only
    name = State()    # shared by standalone flow and order-flow's "customer not found"

class AdminMgmtFlow(StatesGroup):
    add_admin = State()
    remove_admin_pick = State()


def _valid_admin_id(text: str) -> bool:
    """Same guard as templates/moderator.py's _valid_admin_id() — rejects
    unbounded-length strings, non-ASCII look-alike digits, and 0/leading-zero
    phantom ids that would inflate the admin list without ever matching
    _is_admin()."""
    if not (bool(text) and text.isascii() and text.isdigit() and len(text) <= 15):
        return False
    return int(text) > 0 and str(int(text)) == text


def _valid_qty(text: str) -> int | None:
    try:
        qty = int(text.strip())
    except ValueError:
        return None
    if qty <= 0 or qty > 1_000_000:
        return None
    return qty


def _parse_price(text: str) -> float | None:
    try:
        price = float(text.strip().replace(",", "."))
    except ValueError:
        return None
    # Review-found: float("nan") doesn't raise ValueError, and NaN comparisons
    # are always False — without this check "nan"/"NaN" would pass the bounds
    # check below and silently poison every SUM(qty*price) total downstream.
    if not math.isfinite(price) or price < 0 or price > 1_000_000_000:
        return None
    return price


# ── keyboards ─────────────────────────────────────────────────────────────────

def kb_main_menu(sheet_connected: bool = False) -> InlineKeyboardMarkup:
    # NOTE for future call sites: the default False means a caller that
    # forgets to look up sheet_connected silently HIDES the button rather
    # than erroring — cheap to get wrong. Every FSM-timeout/"session
    # expired" fallback screen in this file intentionally takes that default
    # (scope-limited: not worth an extra db round trip on an error path);
    # cmd_start/cb_main_menu/cb_order_flow_cancel are the ones that look it
    # up via _sheet_config_for(config.bot_id) first.
    rows = [
        [InlineKeyboardButton(text="📦 Заказы", callback_data="ord_menu")],
        [InlineKeyboardButton(text="👤 Клиенты", callback_data="cust_menu")],
        [InlineKeyboardButton(text="👥 Админы", callback_data="adm_menu")],
    ]
    # Only rendered when a spreadsheet is actually connected for this bot_id —
    # sheet_connected is looked up (get_bot_sheets_config) by the caller before
    # building this keyboard, same "check, then render" shape as every other
    # feature-gated UI element in this factory (see moderator.py's panel).
    if sheet_connected:
        rows.append([InlineKeyboardButton(text="📊 Таблица заказов", callback_data="ord_sheet_link")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_back(callback_data: str = "main_menu") -> InlineKeyboardButton:
    return InlineKeyboardButton(text="◀️ Назад", callback_data=callback_data)

def kb_orders_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Новый заказ", callback_data="ord_new")],
        [InlineKeyboardButton(text="📋 Все заказы", callback_data="ord_list")],
        [kb_back()],
    ])

_STATUS_FILTERS = [
    ("new", "🆕 Новые"), ("in_progress", "⚙️ В работе"), ("shipped", "🚚 Отправленные"),
    ("done", "✅ Выполненные"), ("cancelled", "❌ Отменённые"), ("all", "📋 Все"),
]

def kb_status_filters() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=label, callback_data=f"ord_filter:{code}")]
            for code, label in _STATUS_FILTERS]
    rows.append([kb_back("ord_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

MAX_LIST_BUTTONS = 25

def kb_order_list(rows: list[tuple], status_filter: str) -> InlineKeyboardMarkup:
    btns = [
        [InlineKeyboardButton(
            text=f"№{oid} · {STATUS_LABELS.get(status, status)} · {_esc(customer_name, 40)}",
            callback_data=f"ord_view:{oid}",
        )]
        for oid, status, customer_name in rows[:MAX_LIST_BUTTONS]
    ]
    btns.append([kb_back("ord_list")])
    return InlineKeyboardMarkup(inline_keyboard=btns)

def kb_order_detail(order_id: int, status: str) -> InlineKeyboardMarkup:
    rows = []
    for target in STATUS_TRANSITIONS.get(status, []):
        icon = "❌ Отменить" if target == "cancelled" else f"▶️ {STATUS_LABELS.get(target, target)}"
        rows.append([InlineKeyboardButton(text=icon, callback_data=f"ord_status:{order_id}:{target}")])
    rows.append([kb_back("ord_list")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_item_more() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить позицию", callback_data="ord_item_more")],
        [InlineKeyboardButton(text="✅ Завершить заказ", callback_data="ord_item_done")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="ord_flow_cancel")],
    ])

def kb_price_skip() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭ Без цены", callback_data="ord_price_skip")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="ord_flow_cancel")],
    ])

def kb_flow_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="ord_flow_cancel")],
    ])

def kb_customers_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Новый клиент", callback_data="cust_new")],
        [InlineKeyboardButton(text="📋 Список клиентов", callback_data="cust_list")],
        [kb_back()],
    ])

def kb_customer_list(rows: list[tuple]) -> InlineKeyboardMarkup:
    btns = [
        [InlineKeyboardButton(text=f"{_esc(name, 40)} · {phone}", callback_data=f"cust_view:{cid}")]
        for cid, name, phone in rows[:MAX_LIST_BUTTONS]
    ]
    btns.append([kb_back("cust_menu")])
    return InlineKeyboardMarkup(inline_keyboard=btns)

def kb_customer_detail() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[kb_back("cust_list")]])

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

def kb_contact_request() -> ReplyKeyboardMarkup:
    # request_contact is a ReplyKeyboardMarkup-only capability — Telegram has
    # no inline-button equivalent for sharing a phone number, so this is the
    # one place in this template that departs from the inline-panel style.
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Поделиться номером", request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=True,
    )


# ── rendering helpers ────────────────────────────────────────────────────────

async def _order_detail_text(db_path: str, order_id: int, extra_note: str | None = None) -> str | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        order = await (await db.execute(
            "SELECT o.id, o.status, o.created_at, o.updated_at, c.name AS customer_name, c.phone "
            "FROM orders o JOIN customers c ON o.customer_id = c.id WHERE o.id=?",
            (order_id,),
        )).fetchone()
        if not order:
            return None
        items = await (await db.execute(
            "SELECT name, qty, price FROM order_items WHERE order_id=? ORDER BY id", (order_id,)
        )).fetchall()

    lines = [
        f"📦 <b>Заказ №{order['id']}</b> · {STATUS_LABELS.get(order['status'], order['status'])}\n",
        f"👤 {_esc(order['customer_name'])} · {_esc(order['phone'])}",
        f"🕐 Создан: {order['created_at']}\n",
        "<b>Позиции:</b>",
    ]
    total = 0.0
    for item in items:
        price_part = f" · {item['price']:.2f}" if item["price"] is not None else ""
        lines.append(f"• {_esc(item['name'])} × {item['qty']}{price_part}")
        if item["price"] is not None:
            total += item["price"] * item["qty"]
    if total > 0:
        lines.append(f"\n💰 Итого: {total:.2f}")
    if extra_note:
        lines.append(f"\n{extra_note}")
    return _join_bounded(lines)


# ── /start ────────────────────────────────────────────────────────────────────

@router.message(Command("start"), F.chat.type == "private")
async def cmd_start(message: Message, state: FSMContext, config: OrdersTrackerConfig):
    # Same reasoning as inventory.py's cmd_start: /start must reset any
    # dangling mid-flow FSM state before showing a menu.
    await state.clear()
    admins = _load_admins(config.admins_file)
    sender_id = message.from_user.id
    # Bug fixed here: this used to grant admin to whoever sent /start FIRST,
    # which lets any client who messages the bot before the owner does
    # permanently seize the admin panel. When bots.owner_telegram_id is known
    # (webhook/production mode), only that user may claim the empty-admins
    # bootstrap slot; a non-owner sending /start first now just gets the
    # regular client menu. In standalone/env mode (owner_telegram_id unknown)
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
        sheet_connected = await _sheet_config_for(config.bot_id) is not None
        if config.welcome_image.exists():
            await message.answer_photo(
                FSInputFile(str(config.welcome_image)), caption=WELCOME_TEXT,
                parse_mode="HTML", reply_markup=kb_main_menu(sheet_connected),
            )
        else:
            await message.answer(WELCOME_TEXT, parse_mode="HTML", reply_markup=kb_main_menu(sheet_connected))
        if first_time_admin:
            await message.answer(
                "👑 <b>Вы — администратор этого бота.</b>\n\n"
                "Управление другими администраторами — кнопка «👥 Админы» выше.",
                parse_mode="HTML",
            )
    else:
        await message.answer(CUSTOMER_WELCOME_TEXT, reply_markup=kb_contact_request())


@router.callback_query(F.data == "main_menu")
async def cb_main_menu(cb: CallbackQuery, state: FSMContext, config: OrdersTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    sheet_connected = await _sheet_config_for(config.bot_id) is not None
    await cb.message.edit_text(WELCOME_TEXT, parse_mode="HTML", reply_markup=kb_main_menu(sheet_connected))


# ── customer-side: link phone via shared Contact ──────────────────────────────

@router.message(F.contact, F.chat.type == "private")
async def on_contact(message: Message, config: OrdersTrackerConfig):
    contact = message.contact
    # Security-critical: a ReplyKeyboardMarkup request_contact button always
    # sends the PRESSER's own phone number, but a hand-crafted client could in
    # principle submit an arbitrary Contact object. Without this check, user A
    # could link user B's phone number to their OWN telegram_user_id and start
    # receiving user B's order-status notifications.
    if contact.user_id != message.from_user.id:
        await message.answer(
            "Пожалуйста, поделитесь именно СВОИМ номером через кнопку ниже.",
            reply_markup=kb_contact_request(),
        )
        return
    phone = _normalize_phone(contact.phone_number)
    if phone is None:
        await message.answer("Не удалось распознать номер.", reply_markup=kb_contact_request())
        return
    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute("SELECT id FROM customers WHERE phone=?", (phone,))).fetchone()
        if not row:
            await message.answer(
                "Не нашли ваш номер среди заказов. Если у вас есть заказ, "
                "свяжитесь с продавцом.", reply_markup=ReplyKeyboardRemove(),
            )
            return
        await db.execute("UPDATE customers SET telegram_user_id=? WHERE id=?", (message.from_user.id, row[0]))
        await db.commit()
    await message.answer(
        "✅ Готово! Буду присылать статус ваших заказов.", reply_markup=ReplyKeyboardRemove(),
    )


# ── ORDERS menu ────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "ord_menu")
async def cb_ord_menu(cb: CallbackQuery, state: FSMContext, config: OrdersTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    await cb.message.edit_text("📦 <b>Заказы</b>", parse_mode="HTML", reply_markup=kb_orders_menu())


@router.callback_query(F.data == "ord_list")
async def cb_ord_list(cb: CallbackQuery, config: OrdersTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await cb.message.edit_text("Выберите фильтр по статусу:", reply_markup=kb_status_filters())


@router.callback_query(F.data.startswith("ord_filter:"))
async def cb_ord_filter(cb: CallbackQuery, config: OrdersTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    status = cb.data.split(":", 1)[1]
    async with aiosqlite.connect(config.db_path) as db:
        if status == "all":
            rows = await (await db.execute(
                "SELECT o.id, o.status, c.name FROM orders o JOIN customers c ON o.customer_id=c.id "
                "ORDER BY o.id DESC LIMIT ?", (MAX_LIST_BUTTONS,)
            )).fetchall()
        else:
            rows = await (await db.execute(
                "SELECT o.id, o.status, c.name FROM orders o JOIN customers c ON o.customer_id=c.id "
                "WHERE o.status=? ORDER BY o.id DESC LIMIT ?", (status, MAX_LIST_BUTTONS)
            )).fetchall()
    if not rows:
        await cb.message.edit_text("Заказов не найдено.", reply_markup=kb_status_filters())
        return
    await cb.message.edit_text(
        f"📋 Заказы (последние {len(rows)}):", reply_markup=kb_order_list(rows, status)
    )


@router.callback_query(F.data.startswith("ord_view:"))
async def cb_ord_view(cb: CallbackQuery, config: OrdersTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        order_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute("SELECT status FROM orders WHERE id=?", (order_id,))).fetchone()
    if not row:
        await cb.message.edit_text("Заказ не найден.", reply_markup=kb_status_filters())
        return
    text = await _order_detail_text(config.db_path, order_id)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_order_detail(order_id, row[0]))


@router.callback_query(F.data.startswith("ord_status:"))
async def cb_ord_status(cb: CallbackQuery, bot: Bot, config: OrdersTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        _, order_id_s, new_status = cb.data.split(":", 2)
        order_id = int(order_id_s)
    except ValueError:
        return
    if new_status not in STATUS_LABELS:
        return

    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute("SELECT status FROM orders WHERE id=?", (order_id,))).fetchone()
        if not row:
            await cb.message.edit_text("Заказ не найден.", reply_markup=kb_status_filters())
            return
        old_status = row[0]
        if new_status not in STATUS_TRANSITIONS.get(old_status, []):
            # Stale button (order was already transitioned by another admin,
            # or a double-tap on an already-applied transition) — re-render
            # instead of silently no-op'ing, same principle as the compare-
            # and-swap UPDATE below.
            text = await _order_detail_text(config.db_path, order_id)
            await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_order_detail(order_id, old_status))
            return
        # Compare-and-swap: WHERE status=old_status makes a double-tap (two
        # callbacks racing on the same stale keyboard) a no-op on the second
        # write instead of double-logging/double-notifying — same principle
        # as inventory.py's double-confirm-tap fix.
        cur = await db.execute(
            "UPDATE orders SET status=?, updated_at=datetime('now','localtime') WHERE id=? AND status=?",
            (new_status, order_id, old_status),
        )
        if cur.rowcount == 0:
            await db.commit()
            text = await _order_detail_text(config.db_path, order_id)
            await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_order_detail(order_id, new_status))
            return
        await db.execute(
            "INSERT INTO order_status_log (order_id, old_status, new_status, changed_by) VALUES (?,?,?,?)",
            (order_id, old_status, new_status, cb.from_user.id),
        )
        customer = await (await db.execute(
            "SELECT c.telegram_user_id, c.name, c.phone FROM customers c JOIN orders o ON o.customer_id=c.id "
            "WHERE o.id=?",
            (order_id,),
        )).fetchone()
        await db.commit()

    await _write_status_to_sheet(
        config.bot_id, order_id, new_status,
        customer[1] if customer else "", customer[2] if customer else "",
    )

    note = None
    telegram_user_id = customer[0] if customer else None
    notify_text = STATUS_NOTIFY_TEXT.get(new_status)
    if telegram_user_id and notify_text:
        try:
            await bot.send_message(telegram_user_id, notify_text.format(order_id=order_id))
            note = "🔔 Клиент уведомлён."
        except TelegramAPIError as e:
            logger.warning(f"orders_tracker: failed to notify customer for order {order_id}: {e}")
            note = "⚠️ Не удалось уведомить клиента (возможно, заблокировал бота)."
        async with aiosqlite.connect(config.db_path) as db:
            await db.execute(
                "UPDATE order_status_log SET notified=? WHERE order_id=? AND new_status=? "
                "AND id=(SELECT MAX(id) FROM order_status_log WHERE order_id=? AND new_status=?)",
                (1 if note == "🔔 Клиент уведомлён." else 0, order_id, new_status, order_id, new_status),
            )
            await db.commit()
    elif notify_text:
        note = "🔕 Клиент не привязан к боту — уведомление не отправлено."

    text = await _order_detail_text(config.db_path, order_id, extra_note=note)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_order_detail(order_id, new_status))


# ── NEW ORDER flow: find/create customer → add items (loop) → finalize ────────

@router.callback_query(F.data == "ord_new")
async def cb_ord_new(cb: CallbackQuery, state: FSMContext, config: OrdersTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    await state.set_state(OrderFlow.find_phone)
    await state.update_data(started_at=time.time())
    await cb.message.edit_text(
        "📱 Введите номер телефона клиента (найдём существующего или создадим нового):",
        reply_markup=kb_flow_cancel(),
    )


@router.message(OrderFlow.find_phone, F.text, ~F.text.startswith("/"))
async def order_find_phone(msg: Message, state: FSMContext, config: OrdersTrackerConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    phone = _normalize_phone(msg.text.strip())
    if phone is None:
        await msg.answer(
            "❌ Неверный номер. Введите российский номер, например: "
            "<b>+7 999 123-45-67</b> или <b>89991234567</b>",
            parse_mode="HTML", reply_markup=kb_flow_cancel(),
        )
        return
    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute("SELECT id, name FROM customers WHERE phone=?", (phone,))).fetchone()
    if row:
        await state.update_data(customer_id=row[0], customer_name=row[1], items=[])
        await state.set_state(OrderFlow.item_name)
        await msg.answer(
            f"✅ Клиент найден: {_esc(row[1])}\n\n📝 Введите название первой позиции:",
            parse_mode="HTML", reply_markup=kb_flow_cancel(),
        )
    else:
        await state.update_data(new_customer_phone=phone, return_to="order")
        await state.set_state(CustomerFlow.name)
        await msg.answer(
            "Клиент не найден. Введите имя нового клиента:", reply_markup=kb_flow_cancel(),
        )


@router.message(OrderFlow.item_name, F.text, ~F.text.startswith("/"))
async def order_item_name(msg: Message, state: FSMContext):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    item_name = msg.text.strip()
    if not item_name:
        await msg.answer("Название не может быть пустым. Введите название позиции:", reply_markup=kb_flow_cancel())
        return
    await state.update_data(pending_item_name=item_name)
    await state.set_state(OrderFlow.item_qty)
    await msg.answer("🔢 Количество:", reply_markup=kb_flow_cancel())


@router.message(OrderFlow.item_qty, F.text, ~F.text.startswith("/"))
async def order_item_qty(msg: Message, state: FSMContext):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    qty = _valid_qty(msg.text)
    if qty is None:
        await msg.answer("Введите целое число от 1 до 1000000, например: 2", reply_markup=kb_flow_cancel())
        return
    await state.update_data(pending_item_qty=qty)
    await state.set_state(OrderFlow.item_price)
    await msg.answer("💰 Цена за штуку (или нажмите «Без цены»):", reply_markup=kb_price_skip())


async def _finalize_item(message_answer, state: FSMContext, price: float | None) -> None:
    data = await state.get_data()
    items = list(data.get("items", []))
    items.append({"name": data["pending_item_name"], "qty": data["pending_item_qty"], "price": price})
    await state.update_data(items=items)
    lines = ["📦 <b>Позиции заказа:</b>\n"] + [
        f"• {_esc(i['name'])} × {i['qty']}" + (f" · {i['price']:.2f}" if i["price"] is not None else "")
        for i in items
    ]
    await state.set_state(None)
    try:
        await message_answer(_join_bounded(lines), parse_mode="HTML", reply_markup=kb_item_more())
    except Exception as e:
        # Review-found: state was already reset to None just above (needed so
        # the NEXT tap of ord_item_more/ord_item_done is routed correctly) —
        # if the send itself then fails, don't leave the admin stuck with no
        # buttons at all. Same recovery shape as inventory.py's
        # _show_movement_confirmation.
        logger.error(f"orders_tracker: failed to show item-loop confirmation: {e}")
        await state.clear()
        await message_answer("⚠️ Не удалось показать список позиций. Начните заново.", reply_markup=kb_main_menu())


@router.message(OrderFlow.item_price, F.text, ~F.text.startswith("/"))
async def order_item_price(msg: Message, state: FSMContext):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    price = _parse_price(msg.text)
    if price is None:
        await msg.answer("Введите число, например: 199.90, или нажмите «Без цены»", reply_markup=kb_price_skip())
        return
    await _finalize_item(msg.answer, state, price)


@router.callback_query(OrderFlow.item_price, F.data == "ord_price_skip")
async def cb_order_price_skip(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    await _finalize_item(cb.message.answer, state, None)


@router.callback_query(F.data == "ord_item_more")
async def cb_order_item_more(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    if len(data.get("items", [])) >= MAX_ITEMS_PER_ORDER:
        await cb.message.edit_text(
            f"Достигнут лимит позиций в заказе ({MAX_ITEMS_PER_ORDER}). Нажмите «✅ Завершить заказ».",
            reply_markup=kb_item_more(),
        )
        return
    await state.set_state(OrderFlow.item_name)
    await cb.message.edit_text("📝 Название следующей позиции:", reply_markup=kb_flow_cancel())


@router.callback_query(F.data == "ord_item_done")
async def cb_order_item_done(cb: CallbackQuery, state: FSMContext, config: OrdersTrackerConfig):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    items = data.get("items", [])
    customer_id = data.get("customer_id")
    if not items or not customer_id:
        # Double-tap guard, same principle as inventory.py's cb_confirm:
        # clearing state immediately below with no awaited I/O in between
        # means a second concurrent tap sees empty state and hits this branch.
        await cb.message.edit_text("Заказ уже создан или сессия устарела.", reply_markup=kb_main_menu())
        return
    await state.clear()
    async with aiosqlite.connect(config.db_path) as db:
        cur = await db.execute("INSERT INTO orders (customer_id) VALUES (?)", (customer_id,))
        order_id = cur.lastrowid
        for item in items:
            await db.execute(
                "INSERT INTO order_items (order_id, name, qty, price) VALUES (?,?,?,?)",
                (order_id, item["name"], item["qty"], item["price"]),
            )
        await db.commit()
    text = await _order_detail_text(config.db_path, order_id)
    await cb.message.edit_text(
        f"✅ Заказ создан!\n\n{text}", parse_mode="HTML", reply_markup=kb_order_detail(order_id, "new")
    )


@router.callback_query(F.data == "ord_flow_cancel")
async def cb_order_flow_cancel(cb: CallbackQuery, state: FSMContext, config: OrdersTrackerConfig):
    await cb.answer()
    await state.clear()
    if not _is_admin(cb.from_user.id, config):
        return
    sheet_connected = await _sheet_config_for(config.bot_id) is not None
    await cb.message.edit_text("Отменено.", reply_markup=kb_main_menu(sheet_connected))


@router.callback_query(F.data == "ord_sheet_link")
async def cb_ord_sheet_link(cb: CallbackQuery, config: OrdersTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    sheet_config = await _sheet_config_for(config.bot_id)
    if sheet_config is None:
        # Button is only rendered when connected (see kb_main_menu), so this
        # is a disconnect-between-render-and-tap race, not the common path —
        # same "stale button, re-render" principle as cb_ord_status below.
        await cb.message.edit_text(WELCOME_TEXT, parse_mode="HTML", reply_markup=kb_main_menu(False))
        return
    url = f"https://docs.google.com/spreadsheets/d/{sheet_config['spreadsheet_id']}"
    await cb.message.answer(f"📊 <b>Таблица заказов:</b>\n{url}", parse_mode="HTML")


# ── CUSTOMERS menu ─────────────────────────────────────────────────────────────

@router.callback_query(F.data == "cust_menu")
async def cb_cust_menu(cb: CallbackQuery, state: FSMContext, config: OrdersTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    await cb.message.edit_text("👤 <b>Клиенты</b>", parse_mode="HTML", reply_markup=kb_customers_menu())


@router.callback_query(F.data == "cust_list")
async def cb_cust_list(cb: CallbackQuery, config: OrdersTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    async with aiosqlite.connect(config.db_path) as db:
        rows = await (await db.execute(
            "SELECT id, name, phone FROM customers ORDER BY id DESC LIMIT ?", (MAX_LIST_BUTTONS,)
        )).fetchall()
    if not rows:
        await cb.message.edit_text("Клиентов пока нет.", reply_markup=kb_customers_menu())
        return
    await cb.message.edit_text("📋 Клиенты:", reply_markup=kb_customer_list(rows))


@router.callback_query(F.data.startswith("cust_view:"))
async def cb_cust_view(cb: CallbackQuery, config: OrdersTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        customer_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        customer = await (await db.execute("SELECT * FROM customers WHERE id=?", (customer_id,))).fetchone()
        if not customer:
            await cb.message.edit_text("Клиент не найден.", reply_markup=kb_customers_menu())
            return
        orders = await (await db.execute(
            "SELECT id, status, created_at FROM orders WHERE customer_id=? ORDER BY id DESC", (customer_id,)
        )).fetchall()
    linked = "🔔 привязан к боту" if customer["telegram_user_id"] else "🔕 не привязан к боту"
    lines = [
        f"👤 <b>{_esc(customer['name'])}</b>\n",
        f"📱 {_esc(customer['phone'])} · {linked}\n",
        "<b>История заказов:</b>",
    ]
    if not orders:
        lines.append("— заказов пока нет —")
    for order in orders:
        lines.append(f"• №{order['id']} · {STATUS_LABELS.get(order['status'], order['status'])} · {order['created_at']}")
    await cb.message.edit_text(_join_bounded(lines), parse_mode="HTML", reply_markup=kb_customer_detail())


@router.callback_query(F.data == "cust_new")
async def cb_cust_new(cb: CallbackQuery, state: FSMContext, config: OrdersTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    await state.set_state(CustomerFlow.phone)
    await state.update_data(started_at=time.time(), return_to="standalone")
    await cb.message.edit_text("📱 Введите номер телефона нового клиента:", reply_markup=kb_flow_cancel())


@router.message(CustomerFlow.phone, F.text, ~F.text.startswith("/"))
async def customer_new_phone(msg: Message, state: FSMContext, config: OrdersTrackerConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    phone = _normalize_phone(msg.text.strip())
    if phone is None:
        await msg.answer(
            "❌ Неверный номер. Введите российский номер, например: +7 999 123-45-67",
            reply_markup=kb_flow_cancel(),
        )
        return
    async with aiosqlite.connect(config.db_path) as db:
        existing = await (await db.execute("SELECT name FROM customers WHERE phone=?", (phone,))).fetchone()
    if existing:
        await state.clear()
        await msg.answer(f"⚠️ Клиент с таким номером уже есть: {_esc(existing[0])}", parse_mode="HTML",
                          reply_markup=kb_customers_menu())
        return
    await state.update_data(new_customer_phone=phone)
    await state.set_state(CustomerFlow.name)
    await msg.answer("Введите имя клиента:", reply_markup=kb_flow_cancel())


@router.message(CustomerFlow.name, F.text, ~F.text.startswith("/"))
async def customer_new_name(msg: Message, state: FSMContext, config: OrdersTrackerConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    name = msg.text.strip()
    if not name:
        await msg.answer("Имя не может быть пустым. Введите имя клиента:", reply_markup=kb_flow_cancel())
        return
    phone = data.get("new_customer_phone")
    if not phone:
        await state.clear()
        await msg.answer("Сессия устарела, начните заново.", reply_markup=kb_main_menu())
        return
    async with aiosqlite.connect(config.db_path) as db:
        try:
            cur = await db.execute("INSERT INTO customers (name, phone) VALUES (?, ?)", (name, phone))
        except aiosqlite.IntegrityError:
            await state.clear()
            await msg.answer("⚠️ Клиент с таким номером уже есть.", reply_markup=kb_customers_menu())
            return
        customer_id = cur.lastrowid
        await db.commit()

    if data.get("return_to") == "order":
        await state.update_data(customer_id=customer_id, customer_name=name, items=[])
        await state.set_state(OrderFlow.item_name)
        await msg.answer(
            f"✅ Клиент создан: {_esc(name)}\n\n📝 Введите название первой позиции:",
            parse_mode="HTML", reply_markup=kb_flow_cancel(),
        )
    else:
        await state.clear()
        await msg.answer(f"✅ Клиент создан: {_esc(name)}", parse_mode="HTML", reply_markup=kb_customers_menu())


# ── ADMINS menu ────────────────────────────────────────────────────────────────

async def _admins_list_text(config: OrdersTrackerConfig) -> str:
    ids = sorted(_load_admins(config.admins_file))
    if not ids:
        return "👥 Пусто"
    return _join_bounded(["👥 <b>Администраторы бота:</b>\n"] + [f"• <code>{_esc(i)}</code>" for i in ids])


@router.callback_query(F.data == "adm_menu")
async def cb_adm_menu(cb: CallbackQuery, state: FSMContext, config: OrdersTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    text = await _admins_list_text(config)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_admins_menu())


@router.callback_query(F.data == "adm_add")
async def cb_adm_add(cb: CallbackQuery, state: FSMContext, config: OrdersTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.set_state(AdminMgmtFlow.add_admin)
    await state.update_data(started_at=time.time())
    await cb.message.edit_text("Введите Telegram ID нового администратора:", reply_markup=kb_flow_cancel())


@router.message(AdminMgmtFlow.add_admin, F.text, ~F.text.startswith("/"))
async def admin_add_id(msg: Message, state: FSMContext, config: OrdersTrackerConfig):
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
async def cb_adm_remove(cb: CallbackQuery, state: FSMContext, config: OrdersTrackerConfig):
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
async def cb_adm_remove_pick(cb: CallbackQuery, state: FSMContext, config: OrdersTrackerConfig):
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
