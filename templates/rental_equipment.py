# TEMPLATE: rental_equipment
# USE FOR: учёт проката оборудования/инвентаря — единицы инвентаря со статусом (доступна/выдана/в ремонте), клиенты, выдача единицы клиенту с датой возврата и залогом, возврат с фиксацией состояния, список просроченных выдач; явный отказ при попытке выдать уже выданную единицу
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
from datetime import date, datetime
from pathlib import Path

import aiosqlite
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

from db.database import add_bot_admin, remove_bot_admin

# ── CUSTOMIZE ────────────────────────────────────────────────────────────────
# Same status as every other template's CUSTOMIZE block: per-file source-text
# customization Claude edits when generating a specific bot, not per-bot
# runtime state (that's config.db_path/admins_file below).
BOT_DESCRIPTION = "Учёт проката оборудования: единицы инвентаря со статусом, выдача клиенту с датой возврата и залогом, возврат с фиксацией состояния, список просроченных выдач."
WELCOME_TEXT = (
    "📦 <b>Прокат оборудования</b>\n\n"
    "Единицы инвентаря, их статус (доступна / выдана / в ремонте), выдачи "
    "клиентам с датой возврата и залогом, возврат с фиксацией состояния.\n\n"
    "Выберите действие:"
)
# Domain has no client-facing bot experience at all — clients don't interact
# with the bot (no notifications, no Contact-binding), unlike orders_tracker.py
# /vehicle_service.py. A non-admin who opens the bot has nothing to do here.
NO_ACCESS_TEXT = "Это внутренний инструмент проката — у вас нет доступа."
ITEM_STATUS_LABELS = {
    "available": "🟢 Доступна",
    "rented": "🔴 Выдана",
    "repair": "🛠 В ремонте",
}
RENTAL_STATUS_LABELS = {
    "active": "🤝 Активна",
    "returned": "✅ Возвращена",
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
            "name": "items",
            "table": "items",
            "order_by": "created_at DESC",
            "creatable": True,
            "title": "Инвентарь",
            "titleField": "name",
            "fields": [
                {"name": "name", "required": True, "label": "Название", "kind": "text", "list": True, "detail": True, "create": True},
                {"name": "tag", "required": True, "label": "Метка", "kind": "text", "list": True, "detail": True, "create": True},
                {"name": "status", "label": "Статус", "kind": "status", "list": True, "detail": True, "create": False},
                {"name": "created_at", "label": "Создан", "kind": "date", "list": False, "detail": True, "create": False},
            ],
        },
        {
            "name": "rentals",
            "table": "rentals",
            "order_by": "issued_at DESC",
            "creatable": True,
            "title": "Аренды",
            "titleField": "status",
            "fields": [
                {"name": "item_id", "required": True, "label": "ID предмета", "kind": "number", "list": False, "detail": True, "create": True},
                {"name": "client_id", "required": True, "label": "ID клиента", "kind": "number", "list": False, "detail": True, "create": True},
                {"name": "issued_at", "label": "Выдано", "kind": "date", "list": True, "detail": True, "create": False},
                {"name": "due_date", "required": True, "label": "Вернуть до", "kind": "date", "list": True, "detail": True, "create": True},
                {"name": "deposit", "label": "Залог", "kind": "number", "list": False, "detail": True, "create": True},
                {"name": "status", "label": "Статус", "kind": "status", "list": True, "detail": True, "create": False},
                {"name": "returned_at", "label": "Возвращено", "kind": "date", "list": False, "detail": True, "create": False},
            ],
        },
    ],
}


# ── config ───────────────────────────────────────────────────────────────────
# Same pattern as every other template — see docs/STAGE2_DESIGN.md.

@dataclass
class RentalEquipmentConfig:
    bot_name: str
    db_path: str
    admins_file: Path
    welcome_image: Path
    display_name: str | None = None
    group_chat_id: str | None = None
    bot_id: int | None = None
    owner_telegram_id: int | None = None


def _paths_for(name: str, data_dir: Path) -> RentalEquipmentConfig:
    return RentalEquipmentConfig(
        bot_name=name,
        db_path=str(data_dir / f"{name}_data.db"),
        admins_file=data_dir / f"admins_{name}.json",
        welcome_image=data_dir / "bot_images" / f"{name}.jpg",
    )


def config_from_env() -> RentalEquipmentConfig:
    """Standalone/subprocess mode."""
    name = Path(__file__).stem
    data_dir = Path(os.getenv("DATA_DIR", "./data"))
    data_dir.mkdir(exist_ok=True)
    return _paths_for(name, data_dir)


def config_from_bot_row(bot_row: dict, data_dir: Path) -> RentalEquipmentConfig:
    """Webhook runtime mode. Paths built from bot_row["bot_id"] (bots.id, the
    physically unique AUTOINCREMENT PK) — NOT bot_row["name"] — same reasoning
    as every other template's config_from_bot_row (see docs/STAGE2_DESIGN.md
    "Изоляция по bots.id")."""
    bot_id = bot_row["bot_id"]
    config = RentalEquipmentConfig(
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
    """Injects this bot's RentalEquipmentConfig into data["config"]."""

    def __init__(self, config: RentalEquipmentConfig) -> None:
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

def _is_admin(user_id: int, config: RentalEquipmentConfig) -> bool:
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


def _valid_admin_id(text: str) -> bool:
    """Same guard as templates/orders_tracker.py's _valid_admin_id()."""
    if not (bool(text) and text.isascii() and text.isdigit() and len(text) <= 15):
        return False
    return int(text) > 0 and str(int(text)) == text


_TAG_RE = re.compile(r"^[A-ZА-ЯЁ0-9_\-/]{1,30}$")


def _normalize_tag(raw: str) -> str | None:
    """Inventory tag is this template's natural key for an item (same role as
    vehicle_service.py's plate_number) — uppercased/whitespace-stripped so
    "inv-001" and "INV-001" match the same stored row."""
    text = re.sub(r"\s+", "", raw.strip().upper())
    return text if _TAG_RE.match(text) else None


# Review-found (monkey-tester): date.fromisoformat("9999-12-31") is the max
# value the stdlib date type supports and was being accepted with no upper
# cap — a due date that far out is never intentional, just unbounded input.
MAX_DUE_DATE_YEARS_OUT = 10


def _parse_due_date(text: str) -> date | None:
    """Accepts ISO (YYYY-MM-DD) or RU (DD.MM.YYYY), same convention as
    templates/debtors.py's use of the stdlib date type. Rejects a due date in
    the past — issuing equipment with an already-overdue return date makes no
    sense and would immediately (and confusingly) show up in "Просроченные" —
    and rejects a due date implausibly far in the future (see
    MAX_DUE_DATE_YEARS_OUT above)."""
    text = text.strip()
    parsed: date | None = None
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.strptime(text, "%d.%m.%Y").date()
        except ValueError:
            return None
    today = date.today()
    if parsed < today or parsed.year > today.year + MAX_DUE_DATE_YEARS_OUT:
        return None
    return parsed


def _parse_deposit(text: str) -> float | None:
    """Same guard/rationale as templates/vehicle_service.py's _parse_price():
    float("nan") doesn't raise ValueError, and NaN comparisons are always
    False — without the math.isfinite() check "nan"/"NaN"/"-nan" would pass
    the bounds check below and silently poison the deposit field."""
    try:
        deposit = float(text.strip().replace(",", "."))
    except ValueError:
        return None
    if not math.isfinite(deposit) or deposit < 0 or deposit > 100_000_000:
        return None
    # Review-found (monkey-tester): float("-0") == -0.0, which passes
    # `deposit < 0` (False) untouched, then renders as the confusing
    # "💰 Залог: -0.00" — normalize the sign away since -0.0 == 0.0 anyway.
    if deposit == 0:
        deposit = 0.0
    return deposit


# ── db ────────────────────────────────────────────────────────────────────────

async def init_db(db_path: str):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS items (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL,
                tag        TEXT NOT NULL UNIQUE,
                status     TEXT NOT NULL DEFAULT 'available'
                           CHECK(status IN ('available','rented','repair')),
                created_at TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS clients (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL,
                contact    TEXT,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS rentals (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id           INTEGER NOT NULL REFERENCES items(id),
                client_id         INTEGER NOT NULL REFERENCES clients(id),
                issued_at         TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                due_date          TEXT NOT NULL,
                deposit           REAL,
                status            TEXT NOT NULL DEFAULT 'active'
                                  CHECK(status IN ('active','returned')),
                returned_at       TEXT,
                return_condition  TEXT,
                created_by        INTEGER
            )
        """)
        # Belt-and-suspenders DB-level guarantee for "нельзя выдать дважды",
        # independent of the items.status compare-and-swap below: a partial
        # unique index makes a second concurrently-active rental for the same
        # item_id physically impossible at the schema level, not just
        # application-logic level.
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_rental_per_item "
            "ON rentals(item_id) WHERE status='active'"
        )
        await db.execute("CREATE INDEX IF NOT EXISTS idx_rentals_status ON rentals(status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_rentals_due ON rentals(due_date)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_items_status ON items(status)")
        await db.commit()


async def _issue_item(db_path: str, item_id: int, client_id: int, due_date: str,
                       deposit: float | None, created_by: int) -> int | None:
    """Compare-and-swap issue: only succeeds if the item is currently
    'available'. Returns the new rental id, or None if the item was already
    rented/in repair (raced by another admin, or a stale button) — the caller
    MUST surface this as an explicit refusal, never silently overwrite the
    existing active rental. Same principle as orders_tracker.py's
    cb_ord_status compare-and-swap UPDATE."""
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "UPDATE items SET status='rented' WHERE id=? AND status='available'", (item_id,)
        )
        if cur.rowcount == 0:
            await db.commit()
            return None
        rental_cur = await db.execute(
            "INSERT INTO rentals (item_id, client_id, due_date, deposit, created_by) VALUES (?,?,?,?,?)",
            (item_id, client_id, due_date, deposit, created_by),
        )
        rental_id = rental_cur.lastrowid
        await db.commit()
        return rental_id


async def _return_item(db_path: str, rental_id: int, condition: str, damaged: bool) -> bool:
    """Compare-and-swap return: only succeeds if the rental is currently
    'active' — guards a double-tap (two admins tapping "Отметить возврат" on
    the same rental) the same way orders_tracker.py's status UPDATE does.
    Item status is derived from the outcome: 'repair' if damaged, else
    'available'. No separate CAS is needed on the items row here — while a
    rental is active, invariant guarantees the item's status is 'rented' and
    nothing else can touch it except this function."""
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "UPDATE rentals SET status='returned', returned_at=datetime('now','localtime'), "
            "return_condition=? WHERE id=? AND status='active'",
            (condition, rental_id),
        )
        if cur.rowcount == 0:
            await db.commit()
            return False
        row = await (await db.execute("SELECT item_id FROM rentals WHERE id=?", (rental_id,))).fetchone()
        new_item_status = "repair" if damaged else "available"
        await db.execute("UPDATE items SET status=? WHERE id=?", (new_item_status, row[0]))
        await db.commit()
        return True


# ── FSM staleness guard ─────────────────────────────────────────────────────────
# Same mechanic as templates/orders_tracker.py's FLOW_TIMEOUT_SECONDS/_flow_expired.
FLOW_TIMEOUT_SECONDS = 300

MAX_LIST_BUTTONS = 25
MAX_ADMIN_REMOVE_BUTTONS = 30


def _flow_expired(data: dict) -> bool:
    started_at = data.get("started_at")
    return started_at is None or (time.time() - started_at) > FLOW_TIMEOUT_SECONDS


# ── FSM states ───────────────────────────────────────────────────────────────

class ItemFlow(StatesGroup):
    name = State()
    tag = State()

class ClientFlow(StatesGroup):
    name = State()
    contact = State()   # shared: standalone "➕ Новый клиент" AND rental-flow's "client not found"

class RentalFlow(StatesGroup):
    due_date = State()
    deposit = State()

class ReturnFlow(StatesGroup):
    damage_note = State()

class AdminMgmtFlow(StatesGroup):
    add_admin = State()
    remove_admin_pick = State()


# ── keyboards ─────────────────────────────────────────────────────────────────

def kb_main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Инвентарь", callback_data="item_menu")],
        [InlineKeyboardButton(text="🤝 Выдачи", callback_data="rnt_menu")],
        [InlineKeyboardButton(text="👤 Клиенты", callback_data="cli_menu")],
        [InlineKeyboardButton(text="👥 Админы", callback_data="adm_menu")],
    ])

def kb_back(callback_data: str = "main_menu") -> InlineKeyboardButton:
    return InlineKeyboardButton(text="◀️ Назад", callback_data=callback_data)

def kb_flow_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="flow_cancel")],
    ])

# ── items menu ──
def kb_items_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Новая единица", callback_data="item_new")],
        [InlineKeyboardButton(text="📋 Список", callback_data="item_list")],
        [kb_back()],
    ])

_ITEM_STATUS_FILTERS = [
    ("available", "🟢 Доступные"), ("rented", "🔴 Выданные"), ("repair", "🛠 В ремонте"), ("all", "📋 Все"),
]

def kb_item_status_filters() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=label, callback_data=f"item_filter:{code}")]
            for code, label in _ITEM_STATUS_FILTERS]
    rows.append([kb_back("item_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_item_list(rows: list[tuple]) -> InlineKeyboardMarkup:
    btns = [
        [InlineKeyboardButton(
            text=f"{ITEM_STATUS_LABELS.get(status, status)} · {_esc(name, 25)} · {_esc(tag, 15)}",
            callback_data=f"item_view:{iid}",
        )]
        for iid, name, tag, status in rows[:MAX_LIST_BUTTONS]
    ]
    btns.append([kb_back("item_list")])
    return InlineKeyboardMarkup(inline_keyboard=btns)

def kb_item_detail(item_id: int, status: str) -> InlineKeyboardMarkup:
    rows = []
    if status == "available":
        rows.append([InlineKeyboardButton(text="🤝 Выдать", callback_data=f"rnt_new_for:{item_id}")])
        rows.append([InlineKeyboardButton(text="🛠 Отправить в ремонт", callback_data=f"item_set:{item_id}:repair")])
    elif status == "repair":
        rows.append([InlineKeyboardButton(text="✅ Готова, доступна", callback_data=f"item_set:{item_id}:available")])
    # status == "rented": no manual toggle — must go through the return flow.
    rows.append([kb_back("item_list")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

# ── rentals menu ──
def kb_rentals_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤝 Новая выдача", callback_data="rnt_new")],
        [InlineKeyboardButton(text="📋 Список", callback_data="rnt_list")],
        [InlineKeyboardButton(text="⏰ Просроченные", callback_data="rnt_overdue")],
        [kb_back()],
    ])

_RENTAL_FILTERS = [
    ("active", "🤝 Активные"), ("overdue", "⏰ Просроченные"), ("returned", "✅ Возвращённые"), ("all", "📋 Все"),
]

def kb_rental_filters() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=label, callback_data=f"rnt_filter:{code}")]
            for code, label in _RENTAL_FILTERS]
    rows.append([kb_back("rnt_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_rental_list(rows: list[tuple]) -> InlineKeyboardMarkup:
    btns = [
        [InlineKeyboardButton(
            text=f"№{rid} · {RENTAL_STATUS_LABELS.get(status, status)} · {_esc(item_name, 20)} · {_esc(client_name, 20)}",
            callback_data=f"rnt_view:{rid}",
        )]
        for rid, status, item_name, client_name in rows[:MAX_LIST_BUTTONS]
    ]
    btns.append([kb_back("rnt_list")])
    return InlineKeyboardMarkup(inline_keyboard=btns)

def kb_rental_detail(rental_id: int, status: str) -> InlineKeyboardMarkup:
    rows = []
    if status == "active":
        rows.append([InlineKeyboardButton(text="📥 Отметить возврат", callback_data=f"ret_start:{rental_id}")])
    rows.append([kb_back("rnt_list")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_item_picker(rows: list[tuple]) -> InlineKeyboardMarkup:
    """Only ever fed AVAILABLE items — see cb_rnt_new."""
    btns = [
        [InlineKeyboardButton(text=f"{_esc(name, 25)} · {_esc(tag, 15)}", callback_data=f"rnt_pick_item:{iid}")]
        for iid, name, tag in rows[:MAX_LIST_BUTTONS]
    ]
    btns.append([kb_back("rnt_menu")])
    return InlineKeyboardMarkup(inline_keyboard=btns)

def kb_client_picker(rows: list[tuple]) -> InlineKeyboardMarkup:
    btns = [
        [InlineKeyboardButton(text=_esc(name, 40), callback_data=f"rnt_pick_client:{cid}")]
        for cid, name in rows[:MAX_LIST_BUTTONS]
    ]
    btns.append([InlineKeyboardButton(text="➕ Новый клиент", callback_data="rnt_pick_client_new")])
    btns.append([kb_back("rnt_menu")])
    return InlineKeyboardMarkup(inline_keyboard=btns)

def kb_deposit_skip() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭ Без залога", callback_data="rnt_deposit_skip")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="flow_cancel")],
    ])

def kb_rental_confirm() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data="rnt_confirm")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="flow_cancel")],
    ])

def kb_return_condition() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ В порядке", callback_data="ret_ok")],
        [InlineKeyboardButton(text="⚠️ Повреждено", callback_data="ret_damaged")],
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
        [InlineKeyboardButton(text=_esc(name, 40), callback_data=f"cli_view:{cid}")]
        for cid, name in rows[:MAX_LIST_BUTTONS]
    ]
    btns.append([kb_back("cli_menu")])
    return InlineKeyboardMarkup(inline_keyboard=btns)

def kb_client_detail() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[kb_back("cli_list")]])

def kb_contact_skip() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭ Без контакта", callback_data="cli_contact_skip")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="flow_cancel")],
    ])

# ── admins menu ──
def kb_admins_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить админа", callback_data="adm_add")],
        [InlineKeyboardButton(text="➖ Убрать админа", callback_data="adm_remove")],
        [kb_back()],
    ])

def kb_remove_admins(ids: list[str]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=admin_id, callback_data=f"adm_rm:{i}")] for i, admin_id in enumerate(ids)]
    rows.append([kb_back("adm_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── rendering helpers ────────────────────────────────────────────────────────

async def _item_detail_text(db_path: str, item_id: int) -> tuple[str, str] | None:
    """Returns (text, status) or None if not found."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        item = await (await db.execute("SELECT * FROM items WHERE id=?", (item_id,))).fetchone()
        if not item:
            return None
        active_rental = await (await db.execute(
            "SELECT r.due_date, r.issued_at, c.name AS client_name FROM rentals r "
            "JOIN clients c ON r.client_id=c.id WHERE r.item_id=? AND r.status='active'", (item_id,)
        )).fetchone()

    lines = [
        f"📦 <b>{_esc(item['name'])}</b> · {_esc(item['tag'])}\n",
        f"Статус: {ITEM_STATUS_LABELS.get(item['status'], item['status'])}",
    ]
    if active_rental:
        lines.append(
            f"\n🤝 Выдана: {_esc(active_rental['client_name'])}\n"
            f"Выдано: {active_rental['issued_at']} · Вернуть до: {active_rental['due_date']}"
        )
    return _join_bounded(lines), item["status"]


async def _rental_detail_text(db_path: str, rental_id: int) -> tuple[str, str] | None:
    """Returns (text, status) or None if not found."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        r = await (await db.execute(
            "SELECT rt.*, i.name AS item_name, i.tag, c.name AS client_name, c.contact "
            "FROM rentals rt JOIN items i ON rt.item_id=i.id JOIN clients c ON rt.client_id=c.id "
            "WHERE rt.id=?", (rental_id,),
        )).fetchone()
        if not r:
            return None

    lines = [
        f"🤝 <b>Выдача №{r['id']}</b> · {RENTAL_STATUS_LABELS.get(r['status'], r['status'])}\n",
        f"📦 {_esc(r['item_name'])} · {_esc(r['tag'])}",
        f"👤 {_esc(r['client_name'])}" + (f" · {_esc(r['contact'])}" if r["contact"] else ""),
        f"🕐 Выдано: {r['issued_at']}",
        f"📅 Вернуть до: {r['due_date']}",
    ]
    if r["deposit"] is not None:
        lines.append(f"💰 Залог: {r['deposit']:.2f}")
    if r["status"] == "returned":
        lines.append(f"\n✅ Возвращено: {r['returned_at']}")
        if r["return_condition"]:
            lines.append(f"Состояние: {_esc(r['return_condition'])}")
    elif r["due_date"] < date.today().isoformat():
        lines.append("\n⏰ <b>ПРОСРОЧЕНО</b>")
    return _join_bounded(lines), r["status"]


# ── /start ────────────────────────────────────────────────────────────────────

@router.message(Command("start"), F.chat.type == "private")
async def cmd_start(message: Message, state: FSMContext, config: RentalEquipmentConfig):
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
        await message.answer(NO_ACCESS_TEXT)


@router.callback_query(F.data == "main_menu")
async def cb_main_menu(cb: CallbackQuery, state: FSMContext, config: RentalEquipmentConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    await cb.message.edit_text(WELCOME_TEXT, parse_mode="HTML", reply_markup=kb_main_menu())


@router.callback_query(F.data == "flow_cancel")
async def cb_flow_cancel(cb: CallbackQuery, state: FSMContext, config: RentalEquipmentConfig):
    await cb.answer()
    await state.clear()
    if not _is_admin(cb.from_user.id, config):
        return
    await cb.message.edit_text("Отменено.", reply_markup=kb_main_menu())


# ── ITEMS menu ─────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "item_menu")
async def cb_item_menu(cb: CallbackQuery, state: FSMContext, config: RentalEquipmentConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    await cb.message.edit_text("📦 <b>Инвентарь</b>", parse_mode="HTML", reply_markup=kb_items_menu())


@router.callback_query(F.data == "item_list")
async def cb_item_list(cb: CallbackQuery, config: RentalEquipmentConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await cb.message.edit_text("Выберите фильтр по статусу:", reply_markup=kb_item_status_filters())


@router.callback_query(F.data.startswith("item_filter:"))
async def cb_item_filter(cb: CallbackQuery, config: RentalEquipmentConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    status = cb.data.split(":", 1)[1]
    async with aiosqlite.connect(config.db_path) as db:
        if status == "all":
            rows = await (await db.execute(
                "SELECT id, name, tag, status FROM items ORDER BY id DESC LIMIT ?", (MAX_LIST_BUTTONS,)
            )).fetchall()
        else:
            rows = await (await db.execute(
                "SELECT id, name, tag, status FROM items WHERE status=? ORDER BY id DESC LIMIT ?",
                (status, MAX_LIST_BUTTONS),
            )).fetchall()
    if not rows:
        await cb.message.edit_text("Единиц не найдено.", reply_markup=kb_item_status_filters())
        return
    await cb.message.edit_text(f"📋 Инвентарь (последние {len(rows)}):", reply_markup=kb_item_list(rows))


@router.callback_query(F.data.startswith("item_view:"))
async def cb_item_view(cb: CallbackQuery, config: RentalEquipmentConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        item_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    result = await _item_detail_text(config.db_path, item_id)
    if result is None:
        await cb.message.edit_text("Единица не найдена.", reply_markup=kb_item_status_filters())
        return
    text, status = result
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_item_detail(item_id, status))


@router.callback_query(F.data.startswith("item_set:"))
async def cb_item_set(cb: CallbackQuery, config: RentalEquipmentConfig):
    """Manual available<->repair toggle. Guarded by a compare-and-swap WHERE
    status IN (...) so a double-tap or a stale button (item picked up for
    rental in the meantime) is a safe no-op with a re-render, not a silent
    overwrite of a 'rented' item's status — same principle as
    orders_tracker.py's cb_ord_status."""
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        _, item_id_s, new_status = cb.data.split(":", 2)
        item_id = int(item_id_s)
    except ValueError:
        return
    if new_status not in ("available", "repair"):
        return
    old_status = "repair" if new_status == "available" else "available"
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute(
            "UPDATE items SET status=? WHERE id=? AND status=?", (new_status, item_id, old_status)
        )
        await db.commit()
    result = await _item_detail_text(config.db_path, item_id)
    if result is None:
        await cb.message.edit_text("Единица не найдена.", reply_markup=kb_item_status_filters())
        return
    text, status = result
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_item_detail(item_id, status))


@router.callback_query(F.data == "item_new")
async def cb_item_new(cb: CallbackQuery, state: FSMContext, config: RentalEquipmentConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    await state.set_state(ItemFlow.name)
    await state.update_data(started_at=time.time())
    await cb.message.edit_text("📝 Введите название единицы инвентаря:", reply_markup=kb_flow_cancel())


@router.message(ItemFlow.name, F.text, ~F.text.startswith("/"))
async def item_new_name(msg: Message, state: FSMContext):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    name = msg.text.strip()
    if not name:
        await msg.answer("Название не может быть пустым. Введите название:", reply_markup=kb_flow_cancel())
        return
    if len(name) > 200:
        await msg.answer("⚠️ Слишком длинное название (макс. 200 символов):", reply_markup=kb_flow_cancel())
        return
    await state.update_data(pending_item_name=name)
    await state.set_state(ItemFlow.tag)
    await msg.answer("🏷 Инвентарный номер/бирка:", reply_markup=kb_flow_cancel())


@router.message(ItemFlow.tag, F.text, ~F.text.startswith("/"))
async def item_new_tag(msg: Message, state: FSMContext, config: RentalEquipmentConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    tag = _normalize_tag(msg.text)
    if tag is None:
        await msg.answer(
            "❌ Неверный формат. Используйте буквы/цифры/-_/, до 30 символов, например: INV-001",
            reply_markup=kb_flow_cancel(),
        )
        return
    name = data.get("pending_item_name")
    if not name:
        await state.clear()
        await msg.answer("Сессия устарела, начните заново.", reply_markup=kb_main_menu())
        return
    async with aiosqlite.connect(config.db_path) as db:
        try:
            await db.execute("INSERT INTO items (name, tag) VALUES (?, ?)", (name, tag))
        except aiosqlite.IntegrityError:
            await state.clear()
            await msg.answer("⚠️ Единица с такой биркой уже есть.", reply_markup=kb_items_menu())
            return
        await db.commit()
    await state.clear()
    await msg.answer(f"✅ Единица создана: {_esc(name)} ({_esc(tag)})", parse_mode="HTML",
                      reply_markup=kb_items_menu())


# ── RENTALS menu ──────────────────────────────────────────────────────────────

@router.callback_query(F.data == "rnt_menu")
async def cb_rnt_menu(cb: CallbackQuery, state: FSMContext, config: RentalEquipmentConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    await cb.message.edit_text("🤝 <b>Выдачи</b>", parse_mode="HTML", reply_markup=kb_rentals_menu())


@router.callback_query(F.data == "rnt_list")
async def cb_rnt_list(cb: CallbackQuery, config: RentalEquipmentConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await cb.message.edit_text("Выберите фильтр:", reply_markup=kb_rental_filters())


def _rental_list_base_sql() -> str:
    return (
        "SELECT r.id, r.status, i.name AS item_name, c.name AS client_name "
        "FROM rentals r JOIN items i ON r.item_id=i.id JOIN clients c ON r.client_id=c.id "
    )


@router.callback_query(F.data.startswith("rnt_filter:"))
async def cb_rnt_filter(cb: CallbackQuery, config: RentalEquipmentConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    status = cb.data.split(":", 1)[1]
    today = date.today().isoformat()
    base_sql = _rental_list_base_sql()
    async with aiosqlite.connect(config.db_path) as db:
        if status == "all":
            rows = await (await db.execute(
                base_sql + "ORDER BY r.id DESC LIMIT ?", (MAX_LIST_BUTTONS,)
            )).fetchall()
        elif status == "overdue":
            rows = await (await db.execute(
                base_sql + "WHERE r.status='active' AND r.due_date < ? ORDER BY r.due_date ASC LIMIT ?",
                (today, MAX_LIST_BUTTONS),
            )).fetchall()
        else:
            rows = await (await db.execute(
                base_sql + "WHERE r.status=? ORDER BY r.id DESC LIMIT ?", (status, MAX_LIST_BUTTONS)
            )).fetchall()
    if not rows:
        await cb.message.edit_text("Выдач не найдено.", reply_markup=kb_rental_filters())
        return
    await cb.message.edit_text(f"📋 Выдачи (последние {len(rows)}):", reply_markup=kb_rental_list(rows))


@router.callback_query(F.data == "rnt_overdue")
async def cb_rnt_overdue(cb: CallbackQuery, config: RentalEquipmentConfig):
    """Shortcut from the rentals menu straight into the overdue filter."""
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    today = date.today().isoformat()
    async with aiosqlite.connect(config.db_path) as db:
        rows = await (await db.execute(
            _rental_list_base_sql() + "WHERE r.status='active' AND r.due_date < ? ORDER BY r.due_date ASC LIMIT ?",
            (today, MAX_LIST_BUTTONS),
        )).fetchall()
    if not rows:
        await cb.message.edit_text("⏰ Просроченных выдач нет.", reply_markup=kb_rentals_menu())
        return
    await cb.message.edit_text(f"⏰ Просроченные выдачи ({len(rows)}):", reply_markup=kb_rental_list(rows))


@router.callback_query(F.data.startswith("rnt_view:"))
async def cb_rnt_view(cb: CallbackQuery, config: RentalEquipmentConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        rental_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    result = await _rental_detail_text(config.db_path, rental_id)
    if result is None:
        await cb.message.edit_text("Выдача не найдена.", reply_markup=kb_rental_filters())
        return
    text, status = result
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_rental_detail(rental_id, status))


# ── NEW RENTAL flow: pick item → pick/create client → due date → deposit → confirm ──

@router.callback_query(F.data == "rnt_new")
async def cb_rnt_new(cb: CallbackQuery, state: FSMContext, config: RentalEquipmentConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    async with aiosqlite.connect(config.db_path) as db:
        rows = await (await db.execute(
            "SELECT id, name, tag FROM items WHERE status='available' ORDER BY id DESC LIMIT ?", (MAX_LIST_BUTTONS,)
        )).fetchall()
    if not rows:
        await cb.message.edit_text("Нет доступных единиц для выдачи.", reply_markup=kb_rentals_menu())
        return
    await state.clear()
    await state.update_data(started_at=time.time())
    await cb.message.edit_text("📦 Выберите единицу инвентаря:", reply_markup=kb_item_picker(rows))


@router.callback_query(F.data.startswith("rnt_new_for:"))
async def cb_rnt_new_for(cb: CallbackQuery, state: FSMContext, config: RentalEquipmentConfig):
    """Shortcut from an item's own detail card — skips the item picker
    entirely since the item is already known."""
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        item_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute("SELECT id FROM items WHERE id=? AND status='available'", (item_id,))).fetchone()
    if not row:
        await cb.message.edit_text("Единица недоступна для выдачи.", reply_markup=kb_items_menu())
        return
    await state.clear()
    await state.update_data(started_at=time.time(), item_id=item_id)
    await _show_client_picker(cb, config)


async def _show_client_picker(cb: CallbackQuery, config: RentalEquipmentConfig) -> None:
    async with aiosqlite.connect(config.db_path) as db:
        rows = await (await db.execute(
            "SELECT id, name FROM clients ORDER BY id DESC LIMIT ?", (MAX_LIST_BUTTONS,)
        )).fetchall()
    await cb.message.edit_text("👤 Выберите клиента или создайте нового:", reply_markup=kb_client_picker(rows))


@router.callback_query(F.data.startswith("rnt_pick_item:"))
async def cb_rnt_pick_item(cb: CallbackQuery, state: FSMContext, config: RentalEquipmentConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    try:
        item_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute("SELECT id FROM items WHERE id=? AND status='available'", (item_id,))).fetchone()
    if not row:
        # Stale button: item was rented/sent to repair by someone else in the
        # meantime — explicit refusal + fresh picker, not a silent proceed.
        async with aiosqlite.connect(config.db_path) as db:
            rows = await (await db.execute(
                "SELECT id, name, tag FROM items WHERE status='available' ORDER BY id DESC LIMIT ?",
                (MAX_LIST_BUTTONS,),
            )).fetchall()
        if not rows:
            await state.clear()
            await cb.message.edit_text(
                "⚠️ Эта единица уже выдана или в ремонте. Больше нет доступных единиц.",
                reply_markup=kb_rentals_menu(),
            )
            return
        await cb.message.edit_text(
            "⚠️ Эта единица уже выдана или в ремонте. Выберите другую:", reply_markup=kb_item_picker(rows)
        )
        return
    await state.update_data(item_id=item_id)
    await _show_client_picker(cb, config)


@router.callback_query(F.data.startswith("rnt_pick_client:"))
async def cb_rnt_pick_client(cb: CallbackQuery, state: FSMContext, config: RentalEquipmentConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    data = await state.get_data()
    if _flow_expired(data) or not data.get("item_id"):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    try:
        client_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute("SELECT id FROM clients WHERE id=?", (client_id,))).fetchone()
    if not row:
        await cb.message.edit_text("Клиент не найден.", reply_markup=kb_rentals_menu())
        return
    await state.update_data(client_id=client_id)
    await state.set_state(RentalFlow.due_date)
    await cb.message.edit_text(
        "📅 Введите дату возврата (ДД.ММ.ГГГГ или ГГГГ-ММ-ДД):", reply_markup=kb_flow_cancel()
    )


@router.callback_query(F.data == "rnt_pick_client_new")
async def cb_rnt_pick_client_new(cb: CallbackQuery, state: FSMContext, config: RentalEquipmentConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    data = await state.get_data()
    if _flow_expired(data) or not data.get("item_id"):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    await state.update_data(return_to="rental")
    await state.set_state(ClientFlow.name)
    await cb.message.edit_text("Введите имя нового клиента:", reply_markup=kb_flow_cancel())


@router.message(RentalFlow.due_date, F.text, ~F.text.startswith("/"))
async def rental_due_date(msg: Message, state: FSMContext):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    due = _parse_due_date(msg.text)
    if due is None:
        await msg.answer(
            "❌ Неверная дата. Введите дату сегодня или позже, например: 25.12.2026",
            reply_markup=kb_flow_cancel(),
        )
        return
    await state.update_data(due_date=due.isoformat())
    await state.set_state(RentalFlow.deposit)
    await msg.answer("💰 Залог (или нажмите «Без залога»):", reply_markup=kb_deposit_skip())


async def _show_rental_confirm(message_answer, state: FSMContext, config: RentalEquipmentConfig) -> None:
    data = await state.get_data()
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        item = await (await db.execute("SELECT name, tag FROM items WHERE id=?", (data["item_id"],))).fetchone()
        client = await (await db.execute("SELECT name FROM clients WHERE id=?", (data["client_id"],))).fetchone()
    if not item or not client:
        await state.clear()
        await message_answer("Сессия устарела, начните заново.", reply_markup=kb_main_menu())
        return
    lines = [
        "🤝 <b>Подтвердите выдачу:</b>\n",
        f"📦 {_esc(item['name'])} · {_esc(item['tag'])}",
        f"👤 {_esc(client['name'])}",
        f"📅 Вернуть до: {data['due_date']}",
    ]
    deposit = data.get("deposit")
    if deposit is not None:
        lines.append(f"💰 Залог: {deposit:.2f}")
    await state.set_state(None)
    try:
        await message_answer(_join_bounded(lines), parse_mode="HTML", reply_markup=kb_rental_confirm())
    except Exception as e:
        # Same recovery shape as orders_tracker.py's _finalize_item: state was
        # already reset to None just above (needed so the NEXT tap of
        # rnt_confirm/flow_cancel is routed correctly) — if the send itself
        # then fails, don't leave the admin stuck with no buttons at all.
        logger.error(f"rental_equipment: failed to show rental confirm card: {e}")
        await state.clear()
        await message_answer("⚠️ Не удалось показать подтверждение. Начните заново.", reply_markup=kb_main_menu())


@router.message(RentalFlow.deposit, F.text, ~F.text.startswith("/"))
async def rental_deposit(msg: Message, state: FSMContext, config: RentalEquipmentConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    deposit = _parse_deposit(msg.text)
    if deposit is None:
        await msg.answer("Введите число ≥ 0, например: 2000, или нажмите «Без залога»", reply_markup=kb_deposit_skip())
        return
    await state.update_data(deposit=deposit)
    await _show_rental_confirm(msg.answer, state, config)


@router.callback_query(RentalFlow.deposit, F.data == "rnt_deposit_skip")
async def cb_rental_deposit_skip(cb: CallbackQuery, state: FSMContext, config: RentalEquipmentConfig):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    await state.update_data(deposit=None)
    await _show_rental_confirm(cb.message.answer, state, config)


@router.callback_query(F.data == "rnt_confirm")
async def cb_rnt_confirm(cb: CallbackQuery, state: FSMContext, config: RentalEquipmentConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    item_id = data.get("item_id")
    client_id = data.get("client_id")
    due_date = data.get("due_date")
    if not item_id or not client_id or not due_date:
        # Double-tap guard, same principle as orders_tracker.py's
        # cb_order_item_done: clearing state below with no awaited I/O in
        # between means a second concurrent tap sees empty state.
        await cb.message.edit_text("Выдача уже создана или сессия устарела.", reply_markup=kb_main_menu())
        return
    await state.clear()
    rental_id = await _issue_item(
        config.db_path, item_id, client_id, due_date, data.get("deposit"), cb.from_user.id
    )
    if rental_id is None:
        await cb.message.edit_text(
            "⚠️ Эта единица уже выдана или в ремонте — выдача отменена, повторите с другой единицей.",
            reply_markup=kb_rentals_menu(),
        )
        return
    result = await _rental_detail_text(config.db_path, rental_id)
    text, status = result
    await cb.message.edit_text(f"✅ Выдача создана!\n\n{text}", parse_mode="HTML",
                                reply_markup=kb_rental_detail(rental_id, status))


# ── RETURN flow ────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("ret_start:"))
async def cb_ret_start(cb: CallbackQuery, state: FSMContext, config: RentalEquipmentConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        rental_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute("SELECT id FROM rentals WHERE id=? AND status='active'", (rental_id,))).fetchone()
    if not row:
        await cb.message.edit_text("Выдача не найдена или уже закрыта.", reply_markup=kb_rentals_menu())
        return
    await state.clear()
    await state.update_data(started_at=time.time(), rental_id=rental_id)
    await cb.message.edit_text("Состояние при возврате:", reply_markup=kb_return_condition())


@router.callback_query(F.data == "ret_ok")
async def cb_ret_ok(cb: CallbackQuery, state: FSMContext, config: RentalEquipmentConfig):
    await cb.answer()
    await _finalize_return(cb, state, config, condition="В порядке", damaged=False)


@router.callback_query(F.data == "ret_damaged")
async def cb_ret_damaged(cb: CallbackQuery, state: FSMContext, config: RentalEquipmentConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    data = await state.get_data()
    if _flow_expired(data) or not data.get("rental_id"):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    await state.set_state(ReturnFlow.damage_note)
    await cb.message.edit_text("Опишите повреждение:", reply_markup=kb_flow_cancel())


@router.message(ReturnFlow.damage_note, F.text, ~F.text.startswith("/"))
async def return_damage_note(msg: Message, state: FSMContext, config: RentalEquipmentConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    note = msg.text.strip()
    if not note:
        await msg.answer("Описание не может быть пустым:", reply_markup=kb_flow_cancel())
        return
    await _finalize_return_message(msg, state, config, condition=f"Повреждено: {note}", damaged=True)


async def _finalize_return(cb: CallbackQuery, state: FSMContext, config: RentalEquipmentConfig,
                            condition: str, damaged: bool) -> None:
    if not _is_admin(cb.from_user.id, config):
        return
    data = await state.get_data()
    rental_id = data.get("rental_id")
    if _flow_expired(data) or not rental_id:
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    await state.clear()
    ok = await _return_item(config.db_path, rental_id, condition, damaged)
    if not ok:
        # Double-tap/race guard: rental was already closed by another admin
        # (or a repeated tap on the same stale card) — re-render, no error.
        await cb.message.edit_text("Эта выдача уже закрыта.", reply_markup=kb_rentals_menu())
        return
    result = await _rental_detail_text(config.db_path, rental_id)
    text, status = result
    await cb.message.edit_text(f"✅ Возврат зафиксирован!\n\n{text}", parse_mode="HTML",
                                reply_markup=kb_rental_detail(rental_id, status))


async def _finalize_return_message(msg: Message, state: FSMContext, config: RentalEquipmentConfig,
                                    condition: str, damaged: bool) -> None:
    # Review-found (monkey-tester): its callback sibling _finalize_return
    # (above) re-checks _is_admin — this text-input path was missing the same
    # check, so an admin revoked mid-flow (via "Убрать админа" on another
    # device) could still close out a rental after losing access.
    if not _is_admin(msg.from_user.id, config):
        return
    data = await state.get_data()
    rental_id = data.get("rental_id")
    if not rental_id:
        await state.clear()
        await msg.answer("Сессия устарела, начните заново.", reply_markup=kb_main_menu())
        return
    await state.clear()
    ok = await _return_item(config.db_path, rental_id, condition, damaged)
    if not ok:
        await msg.answer("Эта выдача уже закрыта.", reply_markup=kb_rentals_menu())
        return
    result = await _rental_detail_text(config.db_path, rental_id)
    text, status = result
    await msg.answer(f"✅ Возврат зафиксирован!\n\n{text}", parse_mode="HTML",
                      reply_markup=kb_rental_detail(rental_id, status))


# ── CLIENTS menu ─────────────────────────────────────────────────────────────

@router.callback_query(F.data == "cli_menu")
async def cb_cli_menu(cb: CallbackQuery, state: FSMContext, config: RentalEquipmentConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    await cb.message.edit_text("👤 <b>Клиенты</b>", parse_mode="HTML", reply_markup=kb_clients_menu())


@router.callback_query(F.data == "cli_list")
async def cb_cli_list(cb: CallbackQuery, config: RentalEquipmentConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    async with aiosqlite.connect(config.db_path) as db:
        rows = await (await db.execute(
            "SELECT id, name FROM clients ORDER BY id DESC LIMIT ?", (MAX_LIST_BUTTONS,)
        )).fetchall()
    if not rows:
        await cb.message.edit_text("Клиентов пока нет.", reply_markup=kb_clients_menu())
        return
    await cb.message.edit_text("📋 Клиенты:", reply_markup=kb_client_list(rows))


@router.callback_query(F.data.startswith("cli_view:"))
async def cb_cli_view(cb: CallbackQuery, config: RentalEquipmentConfig):
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
        rentals = await (await db.execute(
            "SELECT r.id, r.status, i.name AS item_name, r.due_date FROM rentals r "
            "JOIN items i ON r.item_id=i.id WHERE r.client_id=? ORDER BY r.id DESC", (client_id,)
        )).fetchall()
    lines = [
        f"👤 <b>{_esc(client['name'])}</b>\n",
    ]
    if client["contact"]:
        lines.append(f"📞 {_esc(client['contact'])}\n")
    lines.append("<b>История выдач:</b>")
    if not rentals:
        lines.append("— выдач пока нет —")
    for r in rentals:
        lines.append(
            f"• №{r['id']} · {RENTAL_STATUS_LABELS.get(r['status'], r['status'])} · "
            f"{_esc(r['item_name'])} · до {r['due_date']}"
        )
    await cb.message.edit_text(_join_bounded(lines), parse_mode="HTML", reply_markup=kb_client_detail())


@router.callback_query(F.data == "cli_new")
async def cb_cli_new(cb: CallbackQuery, state: FSMContext, config: RentalEquipmentConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    await state.set_state(ClientFlow.name)
    await state.update_data(started_at=time.time(), return_to="standalone")
    await cb.message.edit_text("Введите имя нового клиента:", reply_markup=kb_flow_cancel())


@router.message(ClientFlow.name, F.text, ~F.text.startswith("/"))
async def client_new_name(msg: Message, state: FSMContext):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    name = msg.text.strip()
    if not name:
        await msg.answer("Имя не может быть пустым. Введите имя клиента:", reply_markup=kb_flow_cancel())
        return
    if len(name) > 200:
        await msg.answer("⚠️ Слишком длинное имя (макс. 200 символов):", reply_markup=kb_flow_cancel())
        return
    await state.update_data(pending_client_name=name)
    await state.set_state(ClientFlow.contact)
    await msg.answer("📞 Контакт клиента (или нажмите «Без контакта»):", reply_markup=kb_contact_skip())


async def _finalize_client(message_answer, state: FSMContext, config: RentalEquipmentConfig,
                            contact: str | None) -> None:
    data = await state.get_data()
    name = data.get("pending_client_name")
    if not name:
        await state.clear()
        await message_answer("Сессия устарела, начните заново.", reply_markup=kb_main_menu())
        return
    async with aiosqlite.connect(config.db_path) as db:
        cur = await db.execute("INSERT INTO clients (name, contact) VALUES (?, ?)", (name, contact))
        client_id = cur.lastrowid
        await db.commit()

    if data.get("return_to") == "rental":
        await state.update_data(client_id=client_id)
        await state.set_state(RentalFlow.due_date)
        await message_answer(
            f"✅ Клиент создан: {_esc(name)}\n\n📅 Введите дату возврата (ДД.ММ.ГГГГ или ГГГГ-ММ-ДД):",
            parse_mode="HTML", reply_markup=kb_flow_cancel(),
        )
    else:
        await state.clear()
        await message_answer(f"✅ Клиент создан: {_esc(name)}", parse_mode="HTML", reply_markup=kb_clients_menu())


@router.message(ClientFlow.contact, F.text, ~F.text.startswith("/"))
async def client_new_contact(msg: Message, state: FSMContext, config: RentalEquipmentConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    contact = msg.text.strip()
    if len(contact) > 200:
        await msg.answer("⚠️ Слишком длинный контакт (макс. 200 символов), или нажмите «Без контакта»:",
                          reply_markup=kb_contact_skip())
        return
    await _finalize_client(msg.answer, state, config, contact or None)


@router.callback_query(ClientFlow.contact, F.data == "cli_contact_skip")
async def cb_client_contact_skip(cb: CallbackQuery, state: FSMContext, config: RentalEquipmentConfig):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    await _finalize_client(cb.message.answer, state, config, None)


# ── ADMINS menu ────────────────────────────────────────────────────────────────

async def _admins_list_text(config: RentalEquipmentConfig) -> str:
    ids = sorted(_load_admins(config.admins_file))
    if not ids:
        return "👥 Пусто"
    return _join_bounded(["👥 <b>Администраторы бота:</b>\n"] + [f"• <code>{_esc(i)}</code>" for i in ids])


@router.callback_query(F.data == "adm_menu")
async def cb_adm_menu(cb: CallbackQuery, state: FSMContext, config: RentalEquipmentConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    text = await _admins_list_text(config)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_admins_menu())


@router.callback_query(F.data == "adm_add")
async def cb_adm_add(cb: CallbackQuery, state: FSMContext, config: RentalEquipmentConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.set_state(AdminMgmtFlow.add_admin)
    await state.update_data(started_at=time.time())
    await cb.message.edit_text("Введите Telegram ID нового администратора:", reply_markup=kb_flow_cancel())


@router.message(AdminMgmtFlow.add_admin, F.text, ~F.text.startswith("/"))
async def admin_add_id(msg: Message, state: FSMContext, config: RentalEquipmentConfig):
    # Review-found (monkey-tester): its callback sibling cb_adm_remove_pick
    # re-checks _is_admin at write time — this handler didn't, which let an
    # admin revoked by someone else during the same 5-minute flow window
    # silently grant admin rights (including to themselves) after losing
    # access. This grants a PRIVILEGE, unlike the item/client-creation
    # handlers elsewhere in this file (which follow the sibling templates'
    # established convention of not re-checking mid-flow) — re-checking here
    # specifically closes a self-reinstatement path.
    if not _is_admin(msg.from_user.id, config):
        return
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
async def cb_adm_remove(cb: CallbackQuery, state: FSMContext, config: RentalEquipmentConfig):
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
async def cb_adm_remove_pick(cb: CallbackQuery, state: FSMContext, config: RentalEquipmentConfig):
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
