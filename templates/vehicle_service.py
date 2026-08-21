# TEMPLATE: vehicle_service
# USE FOR: учёт СТО/автосервиса — клиенты и их автомобили (несколько машин на клиента), заявки на обслуживание со статус-флоу (принята → в работе → готова → выдана), список работ/запчастей с ценой, история обслуживания по каждому авто, уведомление клиента о готовности заявки
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

from db.database import add_bot_admin, remove_bot_admin

# ── CUSTOMIZE ────────────────────────────────────────────────────────────────
# Same status as every other template's CUSTOMIZE block: per-file source-text
# customization Claude edits when generating a specific bot, not per-bot
# runtime state (that's config.db_path/admins_file below).
BOT_DESCRIPTION = "Учёт СТО: клиенты и их автомобили, заявки на обслуживание со статус-флоу, история обслуживания по каждому авто, автоуведомление клиента о готовности."
WELCOME_TEXT = (
    "🔧 <b>Учёт автосервиса</b>\n\n"
    "Клиенты, их автомобили и заявки на обслуживание: принята → в работе → "
    "готова → выдана. История обслуживания хранится по каждому автомобилю "
    "отдельно.\n\nВыберите действие:"
)
CUSTOMER_WELCOME_TEXT = (
    "👋 Здравствуйте!\n\n"
    "Поделитесь своим номером телефона, и я сообщу вам, когда ваш автомобиль "
    "будет готов к выдаче."
)
STATUS_LABELS = {
    "accepted": "🆕 Принята",
    "in_progress": "⚙️ В работе",
    "ready": "✅ Готова",
    "issued": "📤 Выдана",
    "cancelled": "❌ Отменена",
}
# Text sent to the CLIENT (not the admin) when their vehicle's request crosses
# into this status. Only "ready" is required by the design — a client cares
# about being told their car can be picked up, not every intermediate step.
STATUS_NOTIFY_TEXT = {
    "ready": "✅ Ваш автомобиль {make_model} ({plate}) готов к выдаче! Заявка №{request_id}.",
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
            "name": "service_requests",
            "table": "service_requests",
            "order_by": "created_at DESC",
            "creatable": True,
            "title": "Заявки на сервис",
            "titleField": "notes",
            "fields": [
                {"name": "vehicle_id", "required": True, "label": "ID автомобиля", "kind": "number", "list": False, "detail": True, "create": True},
                {"name": "status", "label": "Статус", "kind": "status", "list": True, "detail": True, "create": False},
                {"name": "notes", "label": "Заметки", "kind": "text", "list": True, "detail": True, "create": True},
                {"name": "created_at", "label": "Создана", "kind": "date", "list": True, "detail": True, "create": False},
                {"name": "updated_at", "label": "Обновлена", "kind": "date", "list": False, "detail": True, "create": False},
            ],
        },
        {
            "name": "vehicles",
            "table": "vehicles",
            "order_by": "created_at DESC",
            "creatable": True,
            "title": "Автомобили",
            "titleField": "make_model",
            "fields": [
                {"name": "client_id", "required": True, "label": "ID клиента", "kind": "number", "list": False, "detail": True, "create": True},
                {"name": "plate_number", "required": True, "label": "Госномер", "kind": "text", "list": True, "detail": True, "create": True},
                {"name": "make_model", "required": True, "label": "Марка/модель", "kind": "text", "list": True, "detail": True, "create": True},
                {"name": "vin", "label": "VIN", "kind": "text", "list": False, "detail": True, "create": True},
                {"name": "created_at", "label": "Добавлен", "kind": "date", "list": False, "detail": True, "create": False},
            ],
        },
    ],
}

# Explicit forward-only flow, same shape as templates/orders_tracker.py's
# STATUS_TRANSITIONS: no backward moves. "cancelled" is reachable from any
# non-terminal status as a side-branch, mirroring the precedent already
# confirmed with the owner for orders_tracker.
STATUS_TRANSITIONS = {
    "accepted": ["in_progress", "cancelled"],
    "in_progress": ["ready", "cancelled"],
    "ready": ["issued", "cancelled"],
    "issued": [],
    "cancelled": [],
}


# ── config ───────────────────────────────────────────────────────────────────
# Same pattern as every other template — see docs/STAGE2_DESIGN.md.

@dataclass
class VehicleServiceConfig:
    bot_name: str
    db_path: str
    admins_file: Path
    welcome_image: Path
    display_name: str | None = None
    group_chat_id: str | None = None
    bot_id: int | None = None
    owner_telegram_id: int | None = None


def _paths_for(name: str, data_dir: Path) -> VehicleServiceConfig:
    return VehicleServiceConfig(
        bot_name=name,
        db_path=str(data_dir / f"{name}_data.db"),
        admins_file=data_dir / f"admins_{name}.json",
        welcome_image=data_dir / "bot_images" / f"{name}.jpg",
    )


def config_from_env() -> VehicleServiceConfig:
    """Standalone/subprocess mode."""
    name = Path(__file__).stem
    data_dir = Path(os.getenv("DATA_DIR", "./data"))
    data_dir.mkdir(exist_ok=True)
    return _paths_for(name, data_dir)


def config_from_bot_row(bot_row: dict, data_dir: Path) -> VehicleServiceConfig:
    """Webhook runtime mode. Paths built from bot_row["bot_id"] (bots.id, the
    physically unique AUTOINCREMENT PK) — NOT bot_row["name"] — same reasoning
    as every other template's config_from_bot_row (see docs/STAGE2_DESIGN.md
    "Изоляция по bots.id")."""
    bot_id = bot_row["bot_id"]
    config = VehicleServiceConfig(
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
    """Injects this bot's VehicleServiceConfig into data["config"]."""

    def __init__(self, config: VehicleServiceConfig) -> None:
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

def _is_admin(user_id: int, config: VehicleServiceConfig) -> bool:
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


# ── phone normalization ────────────────────────────────────────────────────────
# Same RU-phone formula as templates/orders_tracker.py's _normalize_phone(),
# reused verbatim per the design brief (don't reinvent the Contact-binding
# pattern) — a phone typed by the admin and the same phone arriving via
# Telegram's Contact object normalize to the IDENTICAL string.

def _normalize_phone(raw: str) -> str | None:
    digits = re.sub(r"[^\d]", "", raw)
    if len(digits) == 11 and digits[0] in ("7", "8"):
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    else:
        return None
    return f"+{digits[0]} ({digits[1:4]}) {digits[4:7]}-{digits[7:9]}-{digits[9:11]}"


# ── plate normalization ────────────────────────────────────────────────────────
# Plate numbers are this template's natural key for a vehicle (like phone is
# for a customer in orders_tracker) — normalized so "а123вс777", "А 123 ВС 777"
# and "A123BC777" (Latin look-alikes) all match the SAME stored row on lookup.
# RU plates only ever use the 12 Cyrillic letters that have a visually
# identical Latin counterpart (А/A, В/B, Е/E, К/K, М/M, Н/H, О/O, Р/P, С/C,
# Т/T, У/Y, Х/X) — review-found: without translating Latin input to the
# Cyrillic canonical form, an admin typing the SAME plate once on a RU
# keyboard layout and once on an EN layout would silently create two
# `vehicles` rows for one physical car, splitting its service history (this
# template's headline feature). Deliberately not a strict regional-format
# validator beyond that (RUS plate regexes vary by vehicle class) — just
# uppercase + whitespace-collapse + homoglyph-fold + a sane charset/length
# bound so junk input is rejected without over-specifying a format.
_PLATE_RE = re.compile(r"^[A-ZА-ЯЁ0-9]{2,15}$")
_PLATE_LATIN_TO_CYRILLIC = str.maketrans({
    "A": "А", "B": "В", "E": "Е", "K": "К", "M": "М", "H": "Н",
    "O": "О", "P": "Р", "C": "С", "T": "Т", "Y": "У", "X": "Х",
})

def _normalize_plate(raw: str) -> str | None:
    text = re.sub(r"\s+", "", raw.strip().upper()).translate(_PLATE_LATIN_TO_CYRILLIC)
    return text if _PLATE_RE.match(text) else None


# ── db ────────────────────────────────────────────────────────────────────────

async def init_db(db_path: str):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS clients (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                name              TEXT NOT NULL,
                phone             TEXT NOT NULL UNIQUE,
                telegram_user_id  INTEGER,
                created_at        TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS vehicles (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id     INTEGER NOT NULL REFERENCES clients(id),
                plate_number  TEXT NOT NULL UNIQUE,
                make_model    TEXT NOT NULL,
                vin           TEXT,
                created_at    TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS service_requests (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                vehicle_id   INTEGER NOT NULL REFERENCES vehicles(id),
                status       TEXT NOT NULL DEFAULT 'accepted'
                             CHECK(status IN ('accepted','in_progress','ready','issued','cancelled')),
                notes        TEXT,
                created_at   TEXT DEFAULT (datetime('now','localtime')),
                updated_at   TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS service_items (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id  INTEGER NOT NULL REFERENCES service_requests(id),
                name        TEXT NOT NULL,
                qty         INTEGER NOT NULL DEFAULT 1,
                price       REAL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS service_status_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id  INTEGER NOT NULL,
                old_status  TEXT,
                new_status  TEXT NOT NULL,
                changed_by  INTEGER,
                notified    INTEGER DEFAULT 0,
                changed_at  TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_vehicles_client ON vehicles(client_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_requests_vehicle ON service_requests(vehicle_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_requests_status ON service_requests(status)")
        await db.commit()


# ── FSM staleness guard ─────────────────────────────────────────────────────────
# Same mechanic as templates/orders_tracker.py's FLOW_TIMEOUT_SECONDS/_flow_expired.
FLOW_TIMEOUT_SECONDS = 300

# Same rationale as orders_tracker.py's MAX_ITEMS_PER_ORDER: keeps the
# item-loop confirmation message bounded so it can't grow past Telegram's
# message-length limit mid-flow.
MAX_ITEMS_PER_REQUEST = 50


def _flow_expired(data: dict) -> bool:
    started_at = data.get("started_at")
    return started_at is None or (time.time() - started_at) > FLOW_TIMEOUT_SECONDS


# ── FSM states ───────────────────────────────────────────────────────────────

class RequestFlow(StatesGroup):
    find_plate = State()
    item_name = State()
    item_qty = State()
    item_price = State()

class VehicleFlow(StatesGroup):
    plate = State()        # standalone "➕ Новый автомобиль" entry point only
    make_model = State()   # shared: standalone vehicle creation AND request-flow's "vehicle not found" chain
    vin = State()           # shared, follows make_model

class ClientFlow(StatesGroup):
    phone = State()   # standalone "➕ Новый клиент" entry, OR request/vehicle flow's owner lookup
    name = State()    # shared by every flow's "client not found" branch

class AdminMgmtFlow(StatesGroup):
    add_admin = State()
    remove_admin_pick = State()


def _valid_admin_id(text: str) -> bool:
    """Same guard as templates/orders_tracker.py's _valid_admin_id()."""
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

def kb_main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Заявки", callback_data="req_menu")],
        [InlineKeyboardButton(text="🚗 Автомобили", callback_data="veh_menu")],
        [InlineKeyboardButton(text="👤 Клиенты", callback_data="cli_menu")],
        [InlineKeyboardButton(text="👥 Админы", callback_data="adm_menu")],
    ])

def kb_back(callback_data: str = "main_menu") -> InlineKeyboardButton:
    return InlineKeyboardButton(text="◀️ Назад", callback_data=callback_data)

def kb_flow_cancel() -> InlineKeyboardMarkup:
    # Single shared "flow_cancel" callback for every creation chain (request/
    # vehicle/client) — same principle as orders_tracker.py's kb_flow_cancel().
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="flow_cancel")],
    ])

MAX_LIST_BUTTONS = 25

# ── requests menu ──
def kb_requests_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Новая заявка", callback_data="req_new")],
        [InlineKeyboardButton(text="📋 Все заявки", callback_data="req_list")],
        [kb_back()],
    ])

_STATUS_FILTERS = [
    ("accepted", "🆕 Принятые"), ("in_progress", "⚙️ В работе"), ("ready", "✅ Готовые"),
    ("issued", "📤 Выданные"), ("cancelled", "❌ Отменённые"), ("all", "📋 Все"),
]

def kb_status_filters() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=label, callback_data=f"req_filter:{code}")]
            for code, label in _STATUS_FILTERS]
    rows.append([kb_back("req_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_request_list(rows: list[tuple]) -> InlineKeyboardMarkup:
    btns = [
        [InlineKeyboardButton(
            text=f"№{rid} · {STATUS_LABELS.get(status, status)} · {_esc(plate, 15)} {_esc(make_model, 25)}",
            callback_data=f"req_view:{rid}",
        )]
        for rid, status, plate, make_model in rows[:MAX_LIST_BUTTONS]
    ]
    btns.append([kb_back("req_list")])
    return InlineKeyboardMarkup(inline_keyboard=btns)

def kb_request_detail(request_id: int, status: str) -> InlineKeyboardMarkup:
    rows = []
    for target in STATUS_TRANSITIONS.get(status, []):
        icon = "❌ Отменить" if target == "cancelled" else f"▶️ {STATUS_LABELS.get(target, target)}"
        rows.append([InlineKeyboardButton(text=icon, callback_data=f"req_status:{request_id}:{target}")])
    rows.append([kb_back("req_list")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_item_more() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить позицию", callback_data="req_item_more")],
        [InlineKeyboardButton(text="✅ Завершить заявку", callback_data="req_item_done")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="flow_cancel")],
    ])

def kb_price_skip() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭ Без цены", callback_data="req_price_skip")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="flow_cancel")],
    ])

# ── vehicles menu ──
def kb_vehicles_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Новый автомобиль", callback_data="veh_new")],
        [InlineKeyboardButton(text="📋 Список автомобилей", callback_data="veh_list")],
        [kb_back()],
    ])

def kb_vehicle_list(rows: list[tuple]) -> InlineKeyboardMarkup:
    btns = [
        [InlineKeyboardButton(text=f"{_esc(plate, 15)} · {_esc(make_model, 30)}", callback_data=f"veh_view:{vid}")]
        for vid, plate, make_model in rows[:MAX_LIST_BUTTONS]
    ]
    btns.append([kb_back("veh_menu")])
    return InlineKeyboardMarkup(inline_keyboard=btns)

def kb_vehicle_detail(vehicle_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🆕 Новая заявка для этого авто", callback_data=f"req_new_for:{vehicle_id}")],
        [kb_back("veh_list")],
    ])

def kb_vin_skip() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭ Без VIN", callback_data="veh_vin_skip")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="flow_cancel")],
    ])

# ── clients menu ──
def kb_clients_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Новый клиент", callback_data="cli_new")],
        [InlineKeyboardButton(text="📋 Список клиентов", callback_data="cli_list")],
        [kb_back()],
    ])

def kb_client_list(rows: list[tuple]) -> InlineKeyboardMarkup:
    btns = [
        [InlineKeyboardButton(text=f"{_esc(name, 30)} · {phone}", callback_data=f"cli_view:{cid}")]
        for cid, name, phone in rows[:MAX_LIST_BUTTONS]
    ]
    btns.append([kb_back("cli_menu")])
    return InlineKeyboardMarkup(inline_keyboard=btns)

def kb_client_detail(vehicles: list[tuple]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"{_esc(plate, 15)} · {_esc(make_model, 30)}", callback_data=f"veh_view:{vid}")]
        for vid, plate, make_model in vehicles[:MAX_LIST_BUTTONS]
    ]
    rows.append([kb_back("cli_list")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

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

def kb_contact_request() -> ReplyKeyboardMarkup:
    # request_contact is a ReplyKeyboardMarkup-only capability — same
    # exception to the inline-panel style as orders_tracker.py.
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Поделиться номером", request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=True,
    )


# ── rendering helpers ────────────────────────────────────────────────────────

async def _request_detail_text(db_path: str, request_id: int, extra_note: str | None = None) -> str | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        req = await (await db.execute(
            "SELECT r.id, r.status, r.created_at, r.updated_at, "
            "v.plate_number, v.make_model, v.vin, c.name AS client_name, c.phone "
            "FROM service_requests r "
            "JOIN vehicles v ON r.vehicle_id = v.id "
            "JOIN clients c ON v.client_id = c.id "
            "WHERE r.id=?",
            (request_id,),
        )).fetchone()
        if not req:
            return None
        items = await (await db.execute(
            "SELECT name, qty, price FROM service_items WHERE request_id=? ORDER BY id", (request_id,)
        )).fetchall()

    lines = [
        f"🔧 <b>Заявка №{req['id']}</b> · {STATUS_LABELS.get(req['status'], req['status'])}\n",
        f"🚗 {_esc(req['make_model'])} · {_esc(req['plate_number'])}"
        + (f" · VIN {_esc(req['vin'])}" if req["vin"] else ""),
        f"👤 {_esc(req['client_name'])} · {_esc(req['phone'])}",
        f"🕐 Создана: {req['created_at']}\n",
        "<b>Работы/запчасти:</b>",
    ]
    total = 0.0
    if not items:
        lines.append("— пока не добавлены —")
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


async def _vehicle_detail_text(db_path: str, vehicle_id: int) -> str | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        vehicle = await (await db.execute(
            "SELECT v.*, c.name AS client_name, c.phone, c.telegram_user_id "
            "FROM vehicles v JOIN clients c ON v.client_id = c.id WHERE v.id=?",
            (vehicle_id,),
        )).fetchone()
        if not vehicle:
            return None
        # Single query with SUM(qty*price) per request — same idea as
        # inventory.py's _current_stock aggregate, avoids an N+1 per-request
        # follow-up query when rendering the whole history.
        history = await (await db.execute(
            "SELECT r.id, r.status, r.created_at, COALESCE(SUM(si.qty * si.price), 0) AS total "
            "FROM service_requests r LEFT JOIN service_items si ON si.request_id = r.id "
            "WHERE r.vehicle_id=? GROUP BY r.id ORDER BY r.id DESC",
            (vehicle_id,),
        )).fetchall()

    linked = "🔔 привязан к боту" if vehicle["telegram_user_id"] else "🔕 не привязан к боту"
    lines = [
        f"🚗 <b>{_esc(vehicle['make_model'])}</b> · {_esc(vehicle['plate_number'])}\n",
    ]
    if vehicle["vin"]:
        lines.append(f"🔑 VIN: {_esc(vehicle['vin'])}\n")
    lines.append(f"👤 {_esc(vehicle['client_name'])} · {_esc(vehicle['phone'])} · {linked}\n")
    lines.append("<b>История обслуживания:</b>")
    if not history:
        lines.append("— заявок пока нет —")
    for row in history:
        total_part = f" · {row['total']:.2f}" if row["total"] else ""
        lines.append(
            f"• №{row['id']} · {STATUS_LABELS.get(row['status'], row['status'])} · {row['created_at']}{total_part}"
        )
    return _join_bounded(lines)


# ── /start ────────────────────────────────────────────────────────────────────

@router.message(Command("start"), F.chat.type == "private")
async def cmd_start(message: Message, state: FSMContext, config: VehicleServiceConfig):
    # Same reasoning as orders_tracker.py's cmd_start: /start must reset any
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
        await message.answer(CUSTOMER_WELCOME_TEXT, reply_markup=kb_contact_request())


@router.callback_query(F.data == "main_menu")
async def cb_main_menu(cb: CallbackQuery, state: FSMContext, config: VehicleServiceConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    await cb.message.edit_text(WELCOME_TEXT, parse_mode="HTML", reply_markup=kb_main_menu())


@router.callback_query(F.data == "flow_cancel")
async def cb_flow_cancel(cb: CallbackQuery, state: FSMContext, config: VehicleServiceConfig):
    await cb.answer()
    await state.clear()
    if not _is_admin(cb.from_user.id, config):
        return
    await cb.message.edit_text("Отменено.", reply_markup=kb_main_menu())


# ── customer-side: link phone via shared Contact ──────────────────────────────

@router.message(F.contact, F.chat.type == "private")
async def on_contact(message: Message, config: VehicleServiceConfig):
    contact = message.contact
    # Security-critical: a ReplyKeyboardMarkup request_contact button always
    # sends the PRESSER's own phone number, but a hand-crafted client could in
    # principle submit an arbitrary Contact object. Without this check, user A
    # could link user B's phone number to their OWN telegram_user_id and start
    # receiving user B's "готова к выдаче" notifications — same rationale as
    # templates/orders_tracker.py's on_contact.
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
        row = await (await db.execute("SELECT id FROM clients WHERE phone=?", (phone,))).fetchone()
        if not row:
            await message.answer(
                "Не нашли ваш номер среди клиентов автосервиса. Если у вас есть "
                "заявка, свяжитесь с сервисом.", reply_markup=ReplyKeyboardRemove(),
            )
            return
        await db.execute("UPDATE clients SET telegram_user_id=? WHERE id=?", (message.from_user.id, row[0]))
        await db.commit()
    await message.answer(
        "✅ Готово! Сообщу, когда ваш автомобиль будет готов к выдаче.", reply_markup=ReplyKeyboardRemove(),
    )


# ── REQUESTS menu ────────────────────────────────────────────────────────────

@router.callback_query(F.data == "req_menu")
async def cb_req_menu(cb: CallbackQuery, state: FSMContext, config: VehicleServiceConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    await cb.message.edit_text("📋 <b>Заявки</b>", parse_mode="HTML", reply_markup=kb_requests_menu())


@router.callback_query(F.data == "req_list")
async def cb_req_list(cb: CallbackQuery, config: VehicleServiceConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await cb.message.edit_text("Выберите фильтр по статусу:", reply_markup=kb_status_filters())


@router.callback_query(F.data.startswith("req_filter:"))
async def cb_req_filter(cb: CallbackQuery, config: VehicleServiceConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    status = cb.data.split(":", 1)[1]
    base_sql = (
        "SELECT r.id, r.status, v.plate_number, v.make_model "
        "FROM service_requests r JOIN vehicles v ON r.vehicle_id = v.id "
    )
    async with aiosqlite.connect(config.db_path) as db:
        if status == "all":
            rows = await (await db.execute(
                base_sql + "ORDER BY r.id DESC LIMIT ?", (MAX_LIST_BUTTONS,)
            )).fetchall()
        else:
            rows = await (await db.execute(
                base_sql + "WHERE r.status=? ORDER BY r.id DESC LIMIT ?", (status, MAX_LIST_BUTTONS)
            )).fetchall()
    if not rows:
        await cb.message.edit_text("Заявок не найдено.", reply_markup=kb_status_filters())
        return
    await cb.message.edit_text(f"📋 Заявки (последние {len(rows)}):", reply_markup=kb_request_list(rows))


@router.callback_query(F.data.startswith("req_view:"))
async def cb_req_view(cb: CallbackQuery, config: VehicleServiceConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        request_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute("SELECT status FROM service_requests WHERE id=?", (request_id,))).fetchone()
    if not row:
        await cb.message.edit_text("Заявка не найдена.", reply_markup=kb_status_filters())
        return
    text = await _request_detail_text(config.db_path, request_id)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_request_detail(request_id, row[0]))


@router.callback_query(F.data.startswith("req_status:"))
async def cb_req_status(cb: CallbackQuery, bot: Bot, config: VehicleServiceConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        _, request_id_s, new_status = cb.data.split(":", 2)
        request_id = int(request_id_s)
    except ValueError:
        return
    if new_status not in STATUS_LABELS:
        return

    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute(
            "SELECT status FROM service_requests WHERE id=?", (request_id,)
        )).fetchone()
        if not row:
            await cb.message.edit_text("Заявка не найдена.", reply_markup=kb_status_filters())
            return
        old_status = row[0]
        if new_status not in STATUS_TRANSITIONS.get(old_status, []):
            # Stale button (already transitioned by another admin, or a
            # double-tap on an already-applied transition) — re-render instead
            # of silently no-op'ing, same principle as the compare-and-swap
            # UPDATE below.
            text = await _request_detail_text(config.db_path, request_id)
            await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_request_detail(request_id, old_status))
            return
        # Compare-and-swap: WHERE status=old_status makes a double-tap (two
        # callbacks racing on the same stale keyboard) a no-op on the second
        # write instead of double-logging/double-notifying — same principle
        # as templates/orders_tracker.py's cb_ord_status.
        cur = await db.execute(
            "UPDATE service_requests SET status=?, updated_at=datetime('now','localtime') WHERE id=? AND status=?",
            (new_status, request_id, old_status),
        )
        if cur.rowcount == 0:
            await db.commit()
            text = await _request_detail_text(config.db_path, request_id)
            await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_request_detail(request_id, new_status))
            return
        await db.execute(
            "INSERT INTO service_status_log (request_id, old_status, new_status, changed_by) VALUES (?,?,?,?)",
            (request_id, old_status, new_status, cb.from_user.id),
        )
        contact_row = await (await db.execute(
            "SELECT c.telegram_user_id, v.make_model, v.plate_number "
            "FROM service_requests r JOIN vehicles v ON r.vehicle_id = v.id "
            "JOIN clients c ON v.client_id = c.id WHERE r.id=?",
            (request_id,),
        )).fetchone()
        await db.commit()

    note = None
    notify_text = STATUS_NOTIFY_TEXT.get(new_status)
    telegram_user_id = contact_row[0] if contact_row else None
    if telegram_user_id and notify_text:
        try:
            await bot.send_message(
                telegram_user_id,
                notify_text.format(request_id=request_id, make_model=contact_row[1], plate=contact_row[2]),
            )
            note = "🔔 Клиент уведомлён."
        except TelegramAPIError as e:
            logger.warning(f"vehicle_service: failed to notify client for request {request_id}: {e}")
            note = "⚠️ Не удалось уведомить клиента (возможно, заблокировал бота)."
        async with aiosqlite.connect(config.db_path) as db:
            await db.execute(
                "UPDATE service_status_log SET notified=? WHERE request_id=? AND new_status=? "
                "AND id=(SELECT MAX(id) FROM service_status_log WHERE request_id=? AND new_status=?)",
                (1 if note == "🔔 Клиент уведомлён." else 0, request_id, new_status, request_id, new_status),
            )
            await db.commit()
    elif notify_text:
        note = "🔕 Клиент не привязан к боту — уведомление не отправлено."

    text = await _request_detail_text(config.db_path, request_id, extra_note=note)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_request_detail(request_id, new_status))


# ── NEW REQUEST flow: find/create vehicle (+ client) → add items (loop) → finalize ──

@router.callback_query(F.data == "req_new")
async def cb_req_new(cb: CallbackQuery, state: FSMContext, config: VehicleServiceConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    await state.set_state(RequestFlow.find_plate)
    await state.update_data(started_at=time.time())
    await cb.message.edit_text(
        "🔢 Введите гос. номер автомобиля (найдём существующий или создадим новый):",
        reply_markup=kb_flow_cancel(),
    )


@router.callback_query(F.data.startswith("req_new_for:"))
async def cb_req_new_for(cb: CallbackQuery, state: FSMContext, config: VehicleServiceConfig):
    """Shortcut from a vehicle's own detail card — skips the plate lookup
    entirely since the vehicle is already known."""
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        vehicle_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute("SELECT id FROM vehicles WHERE id=?", (vehicle_id,))).fetchone()
    if not row:
        await cb.message.edit_text("Автомобиль не найден.", reply_markup=kb_vehicles_menu())
        return
    await state.clear()
    await state.set_state(RequestFlow.item_name)
    await state.update_data(started_at=time.time(), vehicle_id=vehicle_id, items=[])
    await cb.message.edit_text("📝 Введите название первой работы/запчасти:", reply_markup=kb_flow_cancel())


@router.message(RequestFlow.find_plate, F.text, ~F.text.startswith("/"))
async def request_find_plate(msg: Message, state: FSMContext, config: VehicleServiceConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    plate = _normalize_plate(msg.text)
    if plate is None:
        await msg.answer(
            "❌ Неверный номер. Введите гос. номер, например: <b>А123ВС777</b>",
            parse_mode="HTML", reply_markup=kb_flow_cancel(),
        )
        return
    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute(
            "SELECT v.id, v.make_model FROM vehicles v WHERE v.plate_number=?", (plate,)
        )).fetchone()
    if row:
        await state.update_data(vehicle_id=row[0], items=[])
        await state.set_state(RequestFlow.item_name)
        await msg.answer(
            f"✅ Автомобиль найден: {_esc(row[1])}\n\n📝 Введите название первой работы/запчасти:",
            parse_mode="HTML", reply_markup=kb_flow_cancel(),
        )
    else:
        await state.update_data(pending_plate=plate, return_to="request")
        await state.set_state(ClientFlow.phone)
        await msg.answer(
            "Автомобиль не найден. Введите телефон владельца:", reply_markup=kb_flow_cancel(),
        )


@router.message(RequestFlow.item_name, F.text, ~F.text.startswith("/"))
async def request_item_name(msg: Message, state: FSMContext):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    item_name = msg.text.strip()
    if not item_name:
        await msg.answer("Название не может быть пустым. Введите название работы/запчасти:", reply_markup=kb_flow_cancel())
        return
    await state.update_data(pending_item_name=item_name)
    await state.set_state(RequestFlow.item_qty)
    await msg.answer("🔢 Количество:", reply_markup=kb_flow_cancel())


@router.message(RequestFlow.item_qty, F.text, ~F.text.startswith("/"))
async def request_item_qty(msg: Message, state: FSMContext):
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
    await state.set_state(RequestFlow.item_price)
    await msg.answer("💰 Цена (или нажмите «Без цены»):", reply_markup=kb_price_skip())


async def _finalize_item(message_answer, state: FSMContext, price: float | None) -> None:
    data = await state.get_data()
    items = list(data.get("items", []))
    items.append({"name": data["pending_item_name"], "qty": data["pending_item_qty"], "price": price})
    await state.update_data(items=items)
    lines = ["🔧 <b>Работы/запчасти в заявке:</b>\n"] + [
        f"• {_esc(i['name'])} × {i['qty']}" + (f" · {i['price']:.2f}" if i["price"] is not None else "")
        for i in items
    ]
    await state.set_state(None)
    try:
        await message_answer(_join_bounded(lines), parse_mode="HTML", reply_markup=kb_item_more())
    except Exception as e:
        # Same recovery as orders_tracker.py's _finalize_item: state was
        # already reset to None just above (needed so the NEXT tap of
        # req_item_more/req_item_done routes correctly) — if the send itself
        # then fails, don't leave the admin stuck with no buttons at all.
        logger.error(f"vehicle_service: failed to show item-loop confirmation: {e}")
        await state.clear()
        await message_answer("⚠️ Не удалось показать список позиций. Начните заново.", reply_markup=kb_main_menu())


@router.message(RequestFlow.item_price, F.text, ~F.text.startswith("/"))
async def request_item_price(msg: Message, state: FSMContext):
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


@router.callback_query(RequestFlow.item_price, F.data == "req_price_skip")
async def cb_request_price_skip(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    await _finalize_item(cb.message.answer, state, None)


@router.callback_query(F.data == "req_item_more")
async def cb_request_item_more(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    if len(data.get("items", [])) >= MAX_ITEMS_PER_REQUEST:
        await cb.message.edit_text(
            f"Достигнут лимит позиций в заявке ({MAX_ITEMS_PER_REQUEST}). Нажмите «✅ Завершить заявку».",
            reply_markup=kb_item_more(),
        )
        return
    await state.set_state(RequestFlow.item_name)
    await cb.message.edit_text("📝 Название следующей работы/запчасти:", reply_markup=kb_flow_cancel())


@router.callback_query(F.data == "req_item_done")
async def cb_request_item_done(cb: CallbackQuery, state: FSMContext, config: VehicleServiceConfig):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    items = data.get("items", [])
    vehicle_id = data.get("vehicle_id")
    if not items or not vehicle_id:
        # Double-tap guard, same principle as orders_tracker.py's
        # cb_order_item_done: clearing state immediately below with no
        # awaited I/O in between means a second concurrent tap sees empty
        # state and hits this branch.
        await cb.message.edit_text("Заявка уже создана или сессия устарела.", reply_markup=kb_main_menu())
        return
    await state.clear()
    async with aiosqlite.connect(config.db_path) as db:
        cur = await db.execute("INSERT INTO service_requests (vehicle_id) VALUES (?)", (vehicle_id,))
        request_id = cur.lastrowid
        for item in items:
            await db.execute(
                "INSERT INTO service_items (request_id, name, qty, price) VALUES (?,?,?,?)",
                (request_id, item["name"], item["qty"], item["price"]),
            )
        await db.commit()
    text = await _request_detail_text(config.db_path, request_id)
    await cb.message.edit_text(
        f"✅ Заявка создана!\n\n{text}", parse_mode="HTML", reply_markup=kb_request_detail(request_id, "accepted")
    )


# ── VEHICLES menu ──────────────────────────────────────────────────────────────

@router.callback_query(F.data == "veh_menu")
async def cb_veh_menu(cb: CallbackQuery, state: FSMContext, config: VehicleServiceConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    await cb.message.edit_text("🚗 <b>Автомобили</b>", parse_mode="HTML", reply_markup=kb_vehicles_menu())


@router.callback_query(F.data == "veh_list")
async def cb_veh_list(cb: CallbackQuery, config: VehicleServiceConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    async with aiosqlite.connect(config.db_path) as db:
        rows = await (await db.execute(
            "SELECT id, plate_number, make_model FROM vehicles ORDER BY id DESC LIMIT ?", (MAX_LIST_BUTTONS,)
        )).fetchall()
    if not rows:
        await cb.message.edit_text("Автомобилей пока нет.", reply_markup=kb_vehicles_menu())
        return
    await cb.message.edit_text("📋 Автомобили:", reply_markup=kb_vehicle_list(rows))


@router.callback_query(F.data.startswith("veh_view:"))
async def cb_veh_view(cb: CallbackQuery, config: VehicleServiceConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        vehicle_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    text = await _vehicle_detail_text(config.db_path, vehicle_id)
    if text is None:
        await cb.message.edit_text("Автомобиль не найден.", reply_markup=kb_vehicles_menu())
        return
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_vehicle_detail(vehicle_id))


@router.callback_query(F.data == "veh_new")
async def cb_veh_new(cb: CallbackQuery, state: FSMContext, config: VehicleServiceConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    await state.set_state(VehicleFlow.plate)
    await state.update_data(started_at=time.time())
    await cb.message.edit_text("🔢 Введите гос. номер нового автомобиля:", reply_markup=kb_flow_cancel())


@router.message(VehicleFlow.plate, F.text, ~F.text.startswith("/"))
async def vehicle_new_plate(msg: Message, state: FSMContext, config: VehicleServiceConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    plate = _normalize_plate(msg.text)
    if plate is None:
        await msg.answer(
            "❌ Неверный номер. Введите гос. номер, например: А123ВС777", reply_markup=kb_flow_cancel(),
        )
        return
    async with aiosqlite.connect(config.db_path) as db:
        existing = await (await db.execute(
            "SELECT make_model FROM vehicles WHERE plate_number=?", (plate,)
        )).fetchone()
    if existing:
        await state.clear()
        await msg.answer(f"⚠️ Автомобиль с таким номером уже есть: {_esc(existing[0])}", parse_mode="HTML",
                          reply_markup=kb_vehicles_menu())
        return
    await state.update_data(pending_plate=plate, return_to="standalone_vehicle")
    await state.set_state(ClientFlow.phone)
    await msg.answer("Введите телефон владельца:", reply_markup=kb_flow_cancel())


@router.message(VehicleFlow.make_model, F.text, ~F.text.startswith("/"))
async def vehicle_make_model(msg: Message, state: FSMContext):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    make_model = msg.text.strip()
    if not make_model:
        await msg.answer("Марка/модель не может быть пустой. Введите марку/модель:", reply_markup=kb_flow_cancel())
        return
    if len(make_model) > 100:
        await msg.answer("⚠️ Слишком длинное название. Введите марку/модель короче 100 символов:",
                          reply_markup=kb_flow_cancel())
        return
    await state.update_data(pending_make_model=make_model)
    await state.set_state(VehicleFlow.vin)
    await msg.answer("🔑 VIN (или нажмите «Без VIN»):", reply_markup=kb_vin_skip())


async def _finalize_vehicle(message_answer, state: FSMContext, config: VehicleServiceConfig, vin: str | None) -> None:
    data = await state.get_data()
    client_id = data.get("client_id")
    plate = data.get("pending_plate")
    make_model = data.get("pending_make_model")
    if not client_id or not plate or not make_model:
        await state.clear()
        await message_answer("Сессия устарела, начните заново.", reply_markup=kb_main_menu())
        return
    async with aiosqlite.connect(config.db_path) as db:
        try:
            cur = await db.execute(
                "INSERT INTO vehicles (client_id, plate_number, make_model, vin) VALUES (?,?,?,?)",
                (client_id, plate, make_model, vin),
            )
        except aiosqlite.IntegrityError:
            await state.clear()
            await message_answer("⚠️ Автомобиль с таким номером уже есть.", reply_markup=kb_vehicles_menu())
            return
        vehicle_id = cur.lastrowid
        await db.commit()

    if data.get("return_to") == "request":
        await state.update_data(vehicle_id=vehicle_id, items=[])
        await state.set_state(RequestFlow.item_name)
        await message_answer(
            f"✅ Автомобиль создан: {_esc(make_model)} ({_esc(plate)})\n\n📝 Введите название первой работы/запчасти:",
            parse_mode="HTML", reply_markup=kb_flow_cancel(),
        )
    else:
        await state.clear()
        await message_answer(
            f"✅ Автомобиль создан: {_esc(make_model)} ({_esc(plate)})", parse_mode="HTML",
            reply_markup=kb_vehicles_menu(),
        )


@router.message(VehicleFlow.vin, F.text, ~F.text.startswith("/"))
async def vehicle_vin(msg: Message, state: FSMContext, config: VehicleServiceConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    vin = msg.text.strip()
    if len(vin) > 32:
        await msg.answer("⚠️ VIN должен быть короче 32 символов, или нажмите «Без VIN»:", reply_markup=kb_vin_skip())
        return
    await _finalize_vehicle(msg.answer, state, config, vin or None)


@router.callback_query(VehicleFlow.vin, F.data == "veh_vin_skip")
async def cb_vehicle_vin_skip(cb: CallbackQuery, state: FSMContext, config: VehicleServiceConfig):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    await _finalize_vehicle(cb.message.answer, state, config, None)


# ── CLIENTS menu ─────────────────────────────────────────────────────────────

@router.callback_query(F.data == "cli_menu")
async def cb_cli_menu(cb: CallbackQuery, state: FSMContext, config: VehicleServiceConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    await cb.message.edit_text("👤 <b>Клиенты</b>", parse_mode="HTML", reply_markup=kb_clients_menu())


@router.callback_query(F.data == "cli_list")
async def cb_cli_list(cb: CallbackQuery, config: VehicleServiceConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    async with aiosqlite.connect(config.db_path) as db:
        rows = await (await db.execute(
            "SELECT id, name, phone FROM clients ORDER BY id DESC LIMIT ?", (MAX_LIST_BUTTONS,)
        )).fetchall()
    if not rows:
        await cb.message.edit_text("Клиентов пока нет.", reply_markup=kb_clients_menu())
        return
    await cb.message.edit_text("📋 Клиенты:", reply_markup=kb_client_list(rows))


@router.callback_query(F.data.startswith("cli_view:"))
async def cb_cli_view(cb: CallbackQuery, config: VehicleServiceConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        client_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        client = await (await db.execute("SELECT * FROM clients WHERE id=?", (client_id,))).fetchone()
        if not client:
            await cb.message.edit_text("Клиент не найден.", reply_markup=kb_clients_menu())
            return
        vehicles = await (await db.execute(
            "SELECT id, plate_number, make_model FROM vehicles WHERE client_id=? ORDER BY id DESC", (client_id,)
        )).fetchall()
    linked = "🔔 привязан к боту" if client["telegram_user_id"] else "🔕 не привязан к боту"
    lines = [
        f"👤 <b>{_esc(client['name'])}</b>\n",
        f"📱 {_esc(client['phone'])} · {linked}\n",
        "<b>Автомобили:</b>" if vehicles else "— автомобилей пока нет —",
    ]
    await cb.message.edit_text(_join_bounded(lines), parse_mode="HTML", reply_markup=kb_client_detail(vehicles))


@router.callback_query(F.data == "cli_new")
async def cb_cli_new(cb: CallbackQuery, state: FSMContext, config: VehicleServiceConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    await state.set_state(ClientFlow.phone)
    await state.update_data(started_at=time.time(), return_to="standalone")
    await cb.message.edit_text("📱 Введите номер телефона нового клиента:", reply_markup=kb_flow_cancel())


@router.message(ClientFlow.phone, F.text, ~F.text.startswith("/"))
async def client_phone(msg: Message, state: FSMContext, config: VehicleServiceConfig):
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
    return_to = data.get("return_to")
    async with aiosqlite.connect(config.db_path) as db:
        existing = await (await db.execute("SELECT id, name FROM clients WHERE phone=?", (phone,))).fetchone()

    if return_to == "standalone":
        # Standalone "➕ Новый клиент" — an existing phone here is a hard stop,
        # same principle as orders_tracker.py's customer_new_phone: this
        # button's whole purpose is creating someone NEW.
        if existing:
            await state.clear()
            await msg.answer(f"⚠️ Клиент с таким номером уже есть: {_esc(existing[1])}", parse_mode="HTML",
                              reply_markup=kb_clients_menu())
            return
        await state.update_data(new_client_phone=phone)
        await state.set_state(ClientFlow.name)
        await msg.answer("Введите имя клиента:", reply_markup=kb_flow_cancel())
        return

    # return_to in {"request", "standalone_vehicle"}: looking up the OWNER of
    # a vehicle being created — an existing match here is the expected/common
    # path (reusing a returning customer for their next car), not an error.
    if existing:
        await state.update_data(client_id=existing[0], client_name=existing[1])
        await state.set_state(VehicleFlow.make_model)
        await msg.answer(
            f"✅ Клиент найден: {_esc(existing[1])}\n\n🚗 Введите марку/модель автомобиля:",
            parse_mode="HTML", reply_markup=kb_flow_cancel(),
        )
    else:
        await state.update_data(new_client_phone=phone)
        await state.set_state(ClientFlow.name)
        await msg.answer("Клиент не найден. Введите имя нового клиента:", reply_markup=kb_flow_cancel())


@router.message(ClientFlow.name, F.text, ~F.text.startswith("/"))
async def client_name(msg: Message, state: FSMContext, config: VehicleServiceConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    name = msg.text.strip()
    if not name:
        await msg.answer("Имя не может быть пустым. Введите имя клиента:", reply_markup=kb_flow_cancel())
        return
    phone = data.get("new_client_phone")
    if not phone:
        await state.clear()
        await msg.answer("Сессия устарела, начните заново.", reply_markup=kb_main_menu())
        return
    async with aiosqlite.connect(config.db_path) as db:
        try:
            cur = await db.execute("INSERT INTO clients (name, phone) VALUES (?, ?)", (name, phone))
        except aiosqlite.IntegrityError:
            await state.clear()
            await msg.answer("⚠️ Клиент с таким номером уже есть.", reply_markup=kb_clients_menu())
            return
        client_id = cur.lastrowid
        await db.commit()

    return_to = data.get("return_to")
    if return_to in ("request", "standalone_vehicle"):
        await state.update_data(client_id=client_id, client_name=name)
        await state.set_state(VehicleFlow.make_model)
        await msg.answer(
            f"✅ Клиент создан: {_esc(name)}\n\n🚗 Введите марку/модель автомобиля:",
            parse_mode="HTML", reply_markup=kb_flow_cancel(),
        )
    else:
        await state.clear()
        await msg.answer(f"✅ Клиент создан: {_esc(name)}", parse_mode="HTML", reply_markup=kb_clients_menu())


# ── ADMINS menu ────────────────────────────────────────────────────────────────

async def _admins_list_text(config: VehicleServiceConfig) -> str:
    ids = sorted(_load_admins(config.admins_file))
    if not ids:
        return "👥 Пусто"
    return _join_bounded(["👥 <b>Администраторы бота:</b>\n"] + [f"• <code>{_esc(i)}</code>" for i in ids])


@router.callback_query(F.data == "adm_menu")
async def cb_adm_menu(cb: CallbackQuery, state: FSMContext, config: VehicleServiceConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    text = await _admins_list_text(config)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_admins_menu())


@router.callback_query(F.data == "adm_add")
async def cb_adm_add(cb: CallbackQuery, state: FSMContext, config: VehicleServiceConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.set_state(AdminMgmtFlow.add_admin)
    await state.update_data(started_at=time.time())
    await cb.message.edit_text("Введите Telegram ID нового администратора:", reply_markup=kb_flow_cancel())


@router.message(AdminMgmtFlow.add_admin, F.text, ~F.text.startswith("/"))
async def admin_add_id(msg: Message, state: FSMContext, config: VehicleServiceConfig):
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
async def cb_adm_remove(cb: CallbackQuery, state: FSMContext, config: VehicleServiceConfig):
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
async def cb_adm_remove_pick(cb: CallbackQuery, state: FSMContext, config: VehicleServiceConfig):
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
