# TEMPLATE: loyalty_program
# USE FOR: программа лояльности для существующих клиентов — баллы начисляются за
#          сумму покупки (по настраиваемому курсу) и списываются вручную при обмене
#          на скидку/подарок. НЕ реферальная программа (см. referral_program — та
#          про привод НОВЫХ людей по персональным ссылкам, эта про удержание УЖЕ
#          существующих клиентов через баллы за покупки)
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
from pathlib import Path

import aiosqlite
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, Message, ReplyKeyboardMarkup,
)

# ── CUSTOMIZE ────────────────────────────────────────────────────────────────
# Same status as every other template's CUSTOMIZE block: per-file source-text
# customization Claude edits when generating a specific bot, not per-bot
# runtime state. Клиенты/операции — runtime-данные, добавляются через меню
# самого бота. Курс начисления НЕ здесь — это per-bot runtime-настройка (см.
# bot_settings ниже), т.к. разные точки продаж работают по-разному.
BOT_DESCRIPTION = "Программа лояльности: баллы за покупки, начисление по курсу, списание на скидку/подарок."
WELCOME_TEXT = (
    "⭐ <b>Программа лояльности</b>\n\n"
    "Начисляю баллы клиентам за сумму покупки и списываю их, когда клиент "
    "обменивает баллы на скидку или подарок.\n\n"
    "Выберите действие:"
)
NO_ACCESS_TEXT = "Это внутренний инструмент владельца бизнеса — у вас нет доступа."
CURRENCY_SYMBOL = "₽"
POINTS_UNIT_LABEL = "баллов"
DEFAULT_POINTS_RATE = 100.0  # ₽ за 1 балл — стартовое значение, дальше настраивается кнопкой "💱 Настройки"
NAME_MAX_LEN = 100
CONTACT_MAX_LEN = 200
NOTE_MAX_LEN = 300
AMOUNT_MAX = 100_000_000
POINTS_MAX = 100_000_000
RATE_MAX = 1_000_000
HISTORY_LIMIT = 30
CARD_PREVIEW_LIMIT = 5
# ── END CUSTOMIZE ─────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
router = Router()

SORT_OPTIONS = ("name", "balance")
SORT_LABELS = {"name": "По имени", "balance": "По баллам ↓"}
DEFAULT_SORT = "name"

# Same rationale as every other template's FLOW_TIMEOUT_SECONDS: MemoryStorage
# keeps a state until explicitly cleared — without an expiry, an admin who
# opens an начислить/списать/курс prompt and goes quiet has every LATER plain
# message silently captured by that stale flow step.
FLOW_TIMEOUT_SECONDS = 300

# Guards the final "write a point_entries row" step of both accrual and
# redemption against a double-tap inserting the same operation twice — same
# pattern as debtors.py's _busy_entry_saves. Keyed by (db_path, client_id),
# NOT client_id alone: this module is a SINGLE Python module instance shared
# by every bot running this template in the webhook multi-tenant runner
# (runtime/registry.py loads it once per process and only clones the Router,
# not the module globals) — client_id restarts from 1 independently per bot,
# so a bare client_id key would let bot A's client #1 and bot B's client #1
# collide in this same set. db_path is already unique per bot and already in
# scope at every call site, so including it costs nothing. Also covers
# accrual and redemption both mutating the SAME client's balance, so either
# one racing itself OR each other must be blocked.
_busy_entry_saves: set[tuple[str, int]] = set()

# Same rationale as _busy_entry_saves above, guarding _finish_new_client
# instead — a double-tap on "Без контакта" (or a race between that callback
# and the text-contact message) would otherwise INSERT the same new client
# twice. Keyed by (db_path, name) rather than an id, since no client id
# exists yet at the point this guard needs to apply.
_busy_client_creates: set[tuple[str, str]] = set()


def _flow_expired(data: dict) -> bool:
    started_at = data.get("started_at")
    return started_at is None or (time.time() - started_at) > FLOW_TIMEOUT_SECONDS


def _esc(value, max_len: int = 500) -> str:
    """HTML-escapes AND length-bounds any user-supplied text before it goes
    into a parse_mode="HTML" message — same helper/rationale as
    debtors.py's/accountant.py's _esc()."""
    text = str(value) if value is not None else ""
    if len(text) > max_len:
        text = text[:max_len] + "…"
    return html.escape(text)


def _short(label: str, max_len: int = 40) -> str:
    return label if len(label) <= max_len else label[:max_len - 1] + "…"


def _valid_telegram_id(text: str) -> bool:
    """Same guard as debtors.py's/shop_catalog.py's admin-id validator."""
    if not (bool(text) and text.isascii() and text.isdigit() and len(text) <= 15):
        return False
    return int(text) > 0 and str(int(text)) == text


_SQLITE_INT_MAX = 2**63 - 1
_SQLITE_INT_MIN = -(2**63)

def _parse_id(text: str) -> int | None:
    """callback_data is bot-generated, but Telegram's API doesn't stop a
    custom client from sending arbitrary callback_query data — int() on an
    unguarded split() would raise on a forged value. Same rationale as
    debtors.py's _parse_id. Also bounds the result to SQLite's signed
    64-bit INTEGER range: Python's int() happily parses a 30-digit string
    (no ValueError), but binding that value into an aiosqlite query later
    raises an unhandled OverflowError — reject it here instead, at the one
    choke point every id-bearing callback_data goes through."""
    try:
        value = int(text)
    except ValueError:
        return None
    if not (_SQLITE_INT_MIN <= value <= _SQLITE_INT_MAX):
        return None
    return value


def _parse_money(text: str) -> float | None:
    """Purchase amounts (unlike debtors.py's whole-unit convention) keep
    kopecks — a real purchase total is rarely a round number."""
    cleaned = re.sub(r"[^\d.,]", "", text).replace(",", ".")
    try:
        amount = float(cleaned)
    except ValueError:
        return None
    if amount <= 0 or amount > AMOUNT_MAX:
        return None
    return round(amount, 2)


def _parse_points_input(text: str) -> float | None:
    cleaned = re.sub(r"[^\d.,]", "", text).replace(",", ".")
    try:
        points = float(cleaned)
    except ValueError:
        return None
    if points <= 0 or points > POINTS_MAX:
        return None
    return round(points, 2)


def _parse_rate_input(text: str) -> float | None:
    cleaned = re.sub(r"[^\d.,]", "", text).replace(",", ".")
    try:
        rate = float(cleaned)
    except ValueError:
        return None
    if rate <= 0 or rate > RATE_MAX:
        return None
    return round(rate, 2)


def _compute_points(purchase_amount: float, rate: float) -> float:
    """Proportional accrual, not floored — a 150₽ purchase at a 100₽/балл
    rate earns 1.5 балла rather than losing the remainder. rate is always
    > 0 by construction (validated on write, defaulted on read)."""
    return round(purchase_amount / rate, 2)


def _fmt_num(value: float) -> str:
    """Formats a points/rate/money value with up to 2 decimals, trimming a
    trailing .00 or .50->.5 — used everywhere a balance, rate, or computed
    points value is shown to the admin."""
    rounded = round(value, 2)
    if rounded == 0:
        return "0"
    text = f"{rounded:.2f}".rstrip("0").rstrip(".")
    return text



# ── config ───────────────────────────────────────────────────────────────────
# Same shape/contract as every other template's Config dataclass — see
# docs/STAGE2_DESIGN.md "Config-контракт шаблона".

@dataclass
class LoyaltyConfig:
    bot_name: str
    db_path: str
    admins_file: Path
    welcome_image: Path
    display_name: str | None = None
    group_chat_id: str | None = None


def _paths_for(name: str, data_dir: Path) -> LoyaltyConfig:
    return LoyaltyConfig(
        bot_name=name,
        db_path=str(data_dir / f"{name}_data.db"),
        admins_file=data_dir / f"admins_{name}.json",
        welcome_image=data_dir / "bot_images" / f"{name}.jpg",
    )


def config_from_env() -> LoyaltyConfig:
    """Standalone/subprocess mode."""
    name = Path(__file__).stem
    data_dir = Path(os.getenv("DATA_DIR", "./data"))
    data_dir.mkdir(exist_ok=True)
    return _paths_for(name, data_dir)


def config_from_bot_row(bot_row: dict, data_dir: Path) -> LoyaltyConfig:
    """Webhook runtime mode. Paths keyed by bot_row["bot_id"] (bots.id, the
    physically unique AUTOINCREMENT PK) — NOT bot_row["name"] — same
    reasoning as every other template's config_from_bot_row."""
    bot_id = bot_row["bot_id"]
    config = LoyaltyConfig(
        bot_name=bot_row["name"],
        db_path=str(data_dir / f"bot_{bot_id}_data.db"),
        admins_file=data_dir / f"admins_{bot_id}.json",
        welcome_image=data_dir / "bot_images" / f"bot_{bot_id}.jpg",
    )
    config.display_name = bot_row.get("display_name")
    config.group_chat_id = bot_row.get("group_chat_id")
    return config


class ConfigMiddleware(BaseMiddleware):
    """Injects this bot's LoyaltyConfig into data["config"]."""

    def __init__(self, config: LoyaltyConfig) -> None:
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

def _is_bot_admin(user_id: int, config: LoyaltyConfig) -> bool:
    return str(user_id) in _load_admins(config.admins_file)


# ── db ────────────────────────────────────────────────────────────────────────

async def init_db(db_path: str):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS clients (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL,
                contact    TEXT,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        # points is SIGNED: positive = accrual (from a purchase), negative =
        # redemption (client exchanged points for a discount/gift). A
        # redemption is a NEW row with the opposite sign — never an edit of
        # an accrual row. Balance = SUM(points) over a client's entries, the
        # same computed-not-stored principle already used by debtors.py/
        # inventory.py, so it can never drift out of sync with its history.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS point_entries (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id       INTEGER NOT NULL REFERENCES clients(id),
                points          REAL NOT NULL,
                kind            TEXT NOT NULL CHECK (kind IN ('accrual','redemption')),
                purchase_amount REAL,
                note            TEXT,
                entry_date      TEXT DEFAULT (date('now','localtime')),
                created_at      TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        # Single-row settings table — the accrual rate is a PER-BOT business
        # choice (different points of sale run different rates), never
        # hardcoded, always read fresh via _get_rate() before every accrual.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bot_settings (
                id          INTEGER PRIMARY KEY CHECK (id = 1),
                points_rate REAL NOT NULL DEFAULT 100
            )
        """)
        await db.execute("INSERT OR IGNORE INTO bot_settings (id, points_rate) VALUES (1, ?)", (DEFAULT_POINTS_RATE,))
        await db.commit()


async def _insert_client(db_path: str, name: str, contact: str | None) -> int:
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute("INSERT INTO clients (name, contact) VALUES (?,?)", (name, contact))
        await db.commit()
        return cur.lastrowid

async def _client_row(db_path: str, client_id: int) -> dict | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute("SELECT * FROM clients WHERE id=?", (client_id,))).fetchone()
        return dict(row) if row else None

async def _delete_client(db_path: str, client_id: int) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute("DELETE FROM point_entries WHERE client_id=?", (client_id,))
        await db.execute("DELETE FROM clients WHERE id=?", (client_id,))
        await db.commit()

async def _clients_with_balance(db_path: str) -> list[dict]:
    """One row per client with a computed net `balance` and `last_entry_date`
    (NULL for a client with no operations yet)."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute("""
            SELECT c.id, c.name, c.contact,
                   COALESCE(SUM(e.points), 0) AS balance,
                   MAX(e.entry_date) AS last_entry_date
            FROM clients c
            LEFT JOIN point_entries e ON e.client_id = c.id
            GROUP BY c.id
            ORDER BY c.name
        """)).fetchall()
        return [dict(r) for r in rows]

async def _client_stats(db_path: str, client_id: int) -> dict:
    async with aiosqlite.connect(db_path) as db:
        row = await (await db.execute(
            "SELECT COALESCE(SUM(points),0), MAX(entry_date), COUNT(*) FROM point_entries WHERE client_id=?",
            (client_id,),
        )).fetchone()
        return {"balance": row[0], "last_entry_date": row[1], "count": row[2]}

async def _client_entries(db_path: str, client_id: int, limit: int = HISTORY_LIMIT) -> list[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT * FROM point_entries WHERE client_id=? ORDER BY entry_date DESC, id DESC LIMIT ?",
            (client_id, limit),
        )).fetchall()
        return [dict(r) for r in rows]

async def _insert_entry(db_path: str, client_id: int, points: float, kind: str,
                         purchase_amount: float | None, note: str | None) -> int:
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "INSERT INTO point_entries (client_id, points, kind, purchase_amount, note) VALUES (?,?,?,?,?)",
            (client_id, points, kind, purchase_amount, note),
        )
        await db.commit()
        return cur.lastrowid

async def _entry_row(db_path: str, entry_id: int) -> dict | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute("SELECT * FROM point_entries WHERE id=?", (entry_id,))).fetchone()
        return dict(row) if row else None

async def _delete_entry(db_path: str, entry_id: int) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute("DELETE FROM point_entries WHERE id=?", (entry_id,))
        await db.commit()


async def _redeem_points(db_path: str, client_id: int, points: float, note: str | None) -> dict:
    """Checks the client's CURRENT balance and inserts the redemption row in
    the same open connection, same guard shape as inventory.py's
    negative-stock check. NOTE: unlike a real BEGIN IMMEDIATE transaction,
    plain aiosqlite SELECTs don't take a write lock, so this function is NOT
    self-sufficiently race-safe against a second concurrent call for the
    same client_id — the actual "balance can't go negative" guarantee comes
    from the caller (_save_redemption_and_reply) always holding this
    client's _busy_entry_saves slot first. Any NEW caller of this function
    must bring the same guard with it, not assume it's built in here.
    Returns {"ok": True} or
    {"ok": False, "error": "insufficient_balance", "balance": <float>}."""
    async with aiosqlite.connect(db_path) as db:
        row = await (await db.execute(
            "SELECT COALESCE(SUM(points),0) FROM point_entries WHERE client_id=?", (client_id,)
        )).fetchone()
        balance = row[0]
        if points > balance:
            return {"ok": False, "error": "insufficient_balance", "balance": balance}
        await db.execute(
            "INSERT INTO point_entries (client_id, points, kind, purchase_amount, note) VALUES (?,?,?,?,?)",
            (client_id, -points, "redemption", None, note),
        )
        await db.commit()
        return {"ok": True}


def _sort_clients(clients: list[dict], sort: str) -> list[dict]:
    if sort == "balance":
        return sorted(clients, key=lambda c: (-c["balance"], c["name"]))
    return sorted(clients, key=lambda c: c["name"])  # name (default)


# ── settings ─────────────────────────────────────────────────────────────────

async def _get_rate(db_path: str) -> float:
    async with aiosqlite.connect(db_path) as db:
        await db.execute("INSERT OR IGNORE INTO bot_settings (id, points_rate) VALUES (1, ?)", (DEFAULT_POINTS_RATE,))
        await db.commit()
        row = await (await db.execute("SELECT points_rate FROM bot_settings WHERE id=1")).fetchone()
        return row[0]

async def _set_rate(db_path: str, rate: float) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute("INSERT OR IGNORE INTO bot_settings (id, points_rate) VALUES (1, ?)", (DEFAULT_POINTS_RATE,))
        await db.execute("UPDATE bot_settings SET points_rate=? WHERE id=1", (rate,))
        await db.commit()


# ── keyboards: main ─────────────────────────────────────────────────────────

def kb_main() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="👥 Клиенты")],
        [KeyboardButton(text="💱 Настройки")],
        [KeyboardButton(text="⚙️ Админы бота")],
    ], resize_keyboard=True)


# ── keyboards: clients list ───────────────────────────────────────────────────

def kb_clients_list(clients: list[dict], sort: str) -> InlineKeyboardMarkup:
    rows = []
    for c in clients:
        label = f"{_short(c['name'], 24)} — {_fmt_num(c['balance'])} {POINTS_UNIT_LABEL}"
        rows.append([InlineKeyboardButton(text=_short(label, 45), callback_data=f"loy_view:{c['id']}")])
    if not rows:
        rows.append([InlineKeyboardButton(text="Клиентов пока нет", callback_data="loy_noop")])

    sort_row = [
        InlineKeyboardButton(
            text=("✅ " if sort == s else "") + SORT_LABELS[s],
            callback_data=f"loy_list:{s}",
        ) for s in SORT_OPTIONS
    ]
    rows.append(sort_row)
    rows.append([InlineKeyboardButton(text="➕ Новый клиент", callback_data="loy_new")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_cancel(callback_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data=callback_data)]])

def kb_new_client_contact() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏩ Без контакта", callback_data="loy_new_skip_contact")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"loy_list:{DEFAULT_SORT}")],
    ])


# ── keyboards: client card ────────────────────────────────────────────────────

def kb_client_card(client_id: int, balance: float) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="➕ Начислить (покупка)", callback_data=f"loy_accrue:{client_id}")]]
    if balance > 0:
        rows.append([InlineKeyboardButton(text="➖ Списать баллы", callback_data=f"loy_redeem:{client_id}")])
    rows.append([
        InlineKeyboardButton(text="📋 История", callback_data=f"loy_hist:{client_id}"),
        InlineKeyboardButton(text="🗑 Удалить", callback_data=f"loy_del_ask:{client_id}"),
    ])
    rows.append([InlineKeyboardButton(text="◀️ К списку", callback_data=f"loy_list:{DEFAULT_SORT}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_step(cancel_cb: str, skip: bool = False, skip_cb: str = "loy_skip_note") -> InlineKeyboardMarkup:
    rows = []
    if skip:
        rows.append([InlineKeyboardButton(text="⏩ Пропустить", callback_data=skip_cb)])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data=cancel_cb)])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_delete_confirm(client_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"loy_del_confirm:{client_id}"),
        InlineKeyboardButton(text="❌ Отмена", callback_data=f"loy_view:{client_id}"),
    ]])

def kb_history(entries: list[dict], client_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text=_short(f"{e['entry_date']}  {'+' if e['points'] >= 0 else ''}{_fmt_num(e['points'])} {POINTS_UNIT_LABEL}", 40),
            callback_data=f"loy_entry_del:{e['id']}:{client_id}",
        )] for e in entries
    ]
    rows.append([InlineKeyboardButton(text="◀️ К клиенту", callback_data=f"loy_view:{client_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── keyboards: settings ───────────────────────────────────────────────────────

def kb_settings() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить курс", callback_data="loy_rate_edit")],
    ])


# ── keyboards: admins panel (same pattern as debtors.py/shop_catalog.py) ──────

def kb_admins_panel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить админа", callback_data="loy_adm_addadmin")],
        [InlineKeyboardButton(text="➖ Убрать админа", callback_data="loy_adm_removeadmin")],
    ])

MAX_ADMIN_REMOVE_BUTTONS = 30

def kb_remove_admins(ids: list[str]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=admin_id, callback_data=f"loy_adm_rma:{i}")] for i, admin_id in enumerate(ids)]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="loy_adm_admins")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── FSM ───────────────────────────────────────────────────────────────────────

class ClientNew(StatesGroup):
    name = State(); contact = State()

class AccrueFlow(StatesGroup):
    amount = State()

class RedeemFlow(StatesGroup):
    points = State(); note = State()

class RateFlow(StatesGroup):
    value = State()

class AdminFlow(StatesGroup):
    add_admin = State(); remove_admin_pick = State()


# ── /start ────────────────────────────────────────────────────────────────────

@router.message(Command("start"), F.chat.type == "private")
async def cmd_start(message: Message, state: FSMContext, config: LoyaltyConfig):
    await state.clear()
    admins = _load_admins(config.admins_file)
    first_time_admin = not admins
    if first_time_admin:
        admins = {str(message.from_user.id)}
        _save_admins(config.admins_file, admins)
    is_admin = str(message.from_user.id) in admins
    if not is_admin:
        await message.answer(NO_ACCESS_TEXT)
        return
    if config.welcome_image.exists():
        await message.answer_photo(FSInputFile(str(config.welcome_image)),
                                   caption=WELCOME_TEXT, parse_mode="HTML", reply_markup=kb_main())
    else:
        await message.answer(WELCOME_TEXT, parse_mode="HTML", reply_markup=kb_main())
    if first_time_admin:
        await message.answer(
            "👑 <b>Вы — администратор этого бота.</b>\n\n"
            "Кнопка «👥 Клиенты» открывает список и все действия.",
            parse_mode="HTML",
        )


# ── CLIENTS LIST ──────────────────────────────────────────────────────────────

async def _render_list(db_path: str, sort: str) -> tuple[str, InlineKeyboardMarkup]:
    all_clients = await _clients_with_balance(db_path)
    shown = _sort_clients(all_clients, sort)
    total_balance = sum(c["balance"] for c in all_clients)
    text = (
        f"👥 <b>Клиенты</b>\n\n"
        f"Всего клиентов: {len(all_clients)}\n"
        f"⭐ Суммарный баланс: {_fmt_num(total_balance)} {POINTS_UNIT_LABEL}\n"
    )
    return text, kb_clients_list(shown, sort)

@router.message(F.text == "👥 Клиенты", F.chat.type == "private")
async def clients_list(msg: Message, state: FSMContext, config: LoyaltyConfig):
    if not _is_bot_admin(msg.from_user.id, config):
        await msg.answer(NO_ACCESS_TEXT); return
    await state.clear()
    text, kb = await _render_list(config.db_path, DEFAULT_SORT)
    await msg.answer(text, parse_mode="HTML", reply_markup=kb)

@router.callback_query(F.data.startswith("loy_list:"))
async def cb_list(cb: CallbackQuery, state: FSMContext, config: LoyaltyConfig):
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    await state.clear()
    sort = cb.data.split(":", 1)[1]
    if sort not in SORT_OPTIONS:
        sort = DEFAULT_SORT
    text, kb = await _render_list(config.db_path, sort)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)

@router.callback_query(F.data == "loy_noop")
async def cb_noop(cb: CallbackQuery):
    await cb.answer()


# ── NEW CLIENT ────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "loy_new")
async def cb_new_client(cb: CallbackQuery, state: FSMContext, config: LoyaltyConfig):
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    await state.update_data(started_at=time.time())
    await state.set_state(ClientNew.name)
    await cb.message.edit_text(
        "✏️ Введите имя клиента:", reply_markup=kb_cancel(f"loy_list:{DEFAULT_SORT}")
    )

@router.message(ClientNew.name, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def new_client_name(msg: Message, state: FSMContext, config: LoyaltyConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("⏳ Время ожидания истекло — начните заново кнопкой «👥 Клиенты»."); return
    if not _is_bot_admin(msg.from_user.id, config):
        await state.clear(); return
    name = msg.text.strip()[:NAME_MAX_LEN]
    if not name:
        await msg.answer("Имя не может быть пустым."); return
    await state.update_data(name=name)
    await state.set_state(ClientNew.contact)
    await msg.answer("📞 Контакт (необязательно) — телефон/@username, или нажмите «Без контакта»:",
                     reply_markup=kb_new_client_contact())

@router.message(ClientNew.contact, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def new_client_contact(msg: Message, state: FSMContext, config: LoyaltyConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("⏳ Время ожидания истекло — начните заново кнопкой «👥 Клиенты»."); return
    if not _is_bot_admin(msg.from_user.id, config):
        await state.clear(); return
    contact = msg.text.strip()[:CONTACT_MAX_LEN]
    await _finish_new_client(msg, state, config, data["name"], contact or None)

@router.callback_query(ClientNew.contact, F.data == "loy_new_skip_contact")
async def new_client_skip_contact(cb: CallbackQuery, state: FSMContext, config: LoyaltyConfig):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("⏳ Время ожидания истекло — начните заново кнопкой «👥 Клиенты»."); return
    if not _is_bot_admin(cb.from_user.id, config):
        await state.clear(); return
    await _finish_new_client(cb.message, state, config, data["name"], None, edit=True)

async def _finish_new_client(msg: Message, state: FSMContext, config: LoyaltyConfig,
                              name: str, contact: str | None, edit: bool = False) -> None:
    busy_key = (config.db_path, name)
    if busy_key in _busy_client_creates:
        return  # double-submit guard — see _busy_client_creates docstring
    _busy_client_creates.add(busy_key)
    try:
        client_id = await _insert_client(config.db_path, name, contact)
        await state.clear()
    finally:
        _busy_client_creates.discard(busy_key)
    stats = await _client_stats(config.db_path, client_id)
    text, kb = await _render_card(config.db_path, client_id, stats)
    text = f"✅ Клиент добавлен.\n\n{text}"
    if edit:
        await msg.edit_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await msg.answer(text, parse_mode="HTML", reply_markup=kb)


# ── CLIENT CARD ───────────────────────────────────────────────────────────────

async def _render_card(db_path: str, client_id: int, stats: dict | None = None) -> tuple[str, InlineKeyboardMarkup] | tuple[None, None]:
    client = await _client_row(db_path, client_id)
    if client is None:
        return None, None
    if stats is None:
        stats = await _client_stats(db_path, client_id)
    entries = await _client_entries(db_path, client_id, CARD_PREVIEW_LIMIT)
    lines = [f"👤 <b>{_esc(client['name'])}</b>"]
    if client["contact"]:
        lines.append(f"📞 {_esc(client['contact'])}")
    lines.append(f"\n⭐ Баланс: <b>{_fmt_num(stats['balance'])} {POINTS_UNIT_LABEL}</b>")
    if entries:
        lines.append("\n<b>Последние операции:</b>")
        for e in entries:
            sign = "+" if e["points"] >= 0 else ""
            detail = f"покупка {_fmt_num(e['purchase_amount'])}{CURRENCY_SYMBOL}" if e["kind"] == "accrual" else (e["note"] or "без комментария")
            lines.append(f"  {e['entry_date']}  {sign}{_fmt_num(e['points'])} {POINTS_UNIT_LABEL} — {_esc(detail, 80)}")
    else:
        lines.append("\nОпераций пока нет.")
    return "\n".join(lines), kb_client_card(client_id, stats["balance"])

@router.callback_query(F.data.startswith("loy_view:"))
async def cb_view_client(cb: CallbackQuery, state: FSMContext, config: LoyaltyConfig):
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    await state.clear()
    client_id = _parse_id(cb.data.split(":", 1)[1])
    text, kb = (None, None) if client_id is None else await _render_card(config.db_path, client_id)
    if text is None:
        await cb.message.edit_text("Клиент не найден.", reply_markup=kb_cancel(f"loy_list:{DEFAULT_SORT}"))
        return
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


# ── ACCRUAL (начисление по сумме покупки) ─────────────────────────────────────

@router.callback_query(F.data.startswith("loy_accrue:"))
async def cb_accrue_start(cb: CallbackQuery, state: FSMContext, config: LoyaltyConfig):
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    client_id = _parse_id(cb.data.split(":", 1)[1])
    client = None if client_id is None else await _client_row(config.db_path, client_id)
    if client is None:
        await cb.message.edit_text("Клиент не найден."); return
    rate = await _get_rate(config.db_path)
    await state.update_data(started_at=time.time(), client_id=client_id, rate=rate)
    await state.set_state(AccrueFlow.amount)
    await cb.message.edit_text(
        f"💰 Сумма покупки ({CURRENCY_SYMBOL}) для <b>{_esc(client['name'])}</b>:\n"
        f"Курс: 1 балл = {_fmt_num(rate)}{CURRENCY_SYMBOL}",
        parse_mode="HTML", reply_markup=kb_cancel(f"loy_view:{client_id}"),
    )

@router.message(AccrueFlow.amount, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def accrue_amount(msg: Message, state: FSMContext, config: LoyaltyConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("⏳ Время ожидания истекло — начните заново из карточки клиента."); return
    if not _is_bot_admin(msg.from_user.id, config):
        await state.clear(); return
    amount = _parse_money(msg.text)
    if amount is None:
        await msg.answer("Введите сумму покупки, например: 1500"); return
    client_id = data.get("client_id")
    rate = data.get("rate") or await _get_rate(config.db_path)
    points = _compute_points(amount, rate)

    busy_key = (config.db_path, client_id) if client_id is not None else None
    if busy_key is None or busy_key in _busy_entry_saves:
        return  # double-submit guard — see _busy_entry_saves docstring
    _busy_entry_saves.add(busy_key)
    try:
        await _insert_entry(config.db_path, client_id, points, "accrual", amount, None)
        await state.clear()
    finally:
        _busy_entry_saves.discard(busy_key)

    text, kb = await _render_card(config.db_path, client_id)
    if text is None:
        text, kb = "Клиент не найден.", kb_cancel(f"loy_list:{DEFAULT_SORT}")
    else:
        text = f"✅ Начислено {_fmt_num(points)} {POINTS_UNIT_LABEL} (покупка {_fmt_num(amount)}{CURRENCY_SYMBOL}).\n\n{text}"
    await msg.answer(text, parse_mode="HTML", reply_markup=kb)


# ── REDEMPTION (списание вручную) ─────────────────────────────────────────────

@router.callback_query(F.data.startswith("loy_redeem:"))
async def cb_redeem_start(cb: CallbackQuery, state: FSMContext, config: LoyaltyConfig):
    if not _is_bot_admin(cb.from_user.id, config):
        await cb.answer(); return
    client_id = _parse_id(cb.data.split(":", 1)[1])
    client = None if client_id is None else await _client_row(config.db_path, client_id)
    if client is None:
        await cb.answer(); await cb.message.edit_text("Клиент не найден."); return
    stats = await _client_stats(config.db_path, client_id)
    balance = stats["balance"]
    if balance <= 0:
        await cb.answer("Баланс пуст — нечего списывать.", show_alert=True); return
    await cb.answer()
    await state.update_data(started_at=time.time(), client_id=client_id)
    await state.set_state(RedeemFlow.points)
    await cb.message.edit_text(
        f"➖ Сколько баллов списать у <b>{_esc(client['name'])}</b>?\n"
        f"Текущий баланс: {_fmt_num(balance)} {POINTS_UNIT_LABEL}",
        parse_mode="HTML", reply_markup=kb_cancel(f"loy_view:{client_id}"),
    )

@router.message(RedeemFlow.points, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def redeem_points(msg: Message, state: FSMContext, config: LoyaltyConfig):
    data = await state.get_data()
    client_id = data.get("client_id")
    if _flow_expired(data):
        await state.clear()
        await msg.answer("⏳ Время ожидания истекло — начните заново из карточки клиента."); return
    if not _is_bot_admin(msg.from_user.id, config):
        await state.clear(); return
    points = _parse_points_input(msg.text)
    if points is None:
        await msg.answer("Введите положительное число баллов, например: 500"); return
    # Fresh read, not the balance shown when this flow started — an admin
    # could take a while typing, and another operation on the same client
    # could have landed meanwhile. Final authority is still _redeem_points'
    # own atomic check at save time; this is just earlier, friendlier UX.
    stats = await _client_stats(config.db_path, client_id) if client_id is not None else {"balance": 0}
    if points > stats["balance"]:
        await msg.answer(f"⚠️ Нельзя списать больше, чем на балансе ({_fmt_num(stats['balance'])} {POINTS_UNIT_LABEL})."); return
    await state.update_data(points=points)
    await state.set_state(RedeemFlow.note)
    await msg.answer(
        "📝 На что обменяно (необязательно) — отправьте текст или нажмите «Пропустить»:",
        reply_markup=kb_step(f"loy_view:{client_id}", skip=True),
    )

@router.message(RedeemFlow.note, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def redeem_note(msg: Message, state: FSMContext, config: LoyaltyConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("⏳ Время ожидания истекло — начните заново из карточки клиента."); return
    if not _is_bot_admin(msg.from_user.id, config):
        await state.clear(); return
    note = msg.text.strip()[:NOTE_MAX_LEN] or None
    await _save_redemption_and_reply(msg, state, config, note)

@router.callback_query(RedeemFlow.note, F.data == "loy_skip_note")
async def redeem_skip_note(cb: CallbackQuery, state: FSMContext, config: LoyaltyConfig):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("⏳ Время ожидания истекло — начните заново из карточки клиента."); return
    if not _is_bot_admin(cb.from_user.id, config):
        await state.clear(); return
    await _save_redemption_and_reply(cb.message, state, config, None, edit=True)

async def _save_redemption_and_reply(msg: Message, state: FSMContext, config: LoyaltyConfig,
                                      note: str | None, edit: bool = False) -> None:
    data = await state.get_data()
    client_id = data.get("client_id")
    points = data.get("points")
    busy_key = (config.db_path, client_id) if client_id is not None else None
    if busy_key is None or points is None or busy_key in _busy_entry_saves:
        return  # double-submit guard — see _busy_entry_saves docstring
    _busy_entry_saves.add(busy_key)
    try:
        result = await _redeem_points(config.db_path, client_id, points, note)
        await state.clear()
    finally:
        _busy_entry_saves.discard(busy_key)

    if not result["ok"]:
        card_text, kb = await _render_card(config.db_path, client_id)
        header = (f"⚠️ Не удалось списать {_fmt_num(points)} {POINTS_UNIT_LABEL} — "
                  f"на балансе только {_fmt_num(result['balance'])} {POINTS_UNIT_LABEL}.")
        text = f"{header}\n\n{card_text}" if card_text else header
        kb = kb or kb_cancel(f"loy_list:{DEFAULT_SORT}")
    else:
        card_text, kb = await _render_card(config.db_path, client_id)
        if card_text is None:
            text, kb = "Клиент не найден.", kb_cancel(f"loy_list:{DEFAULT_SORT}")
        else:
            text = f"✅ Списано {_fmt_num(points)} {POINTS_UNIT_LABEL}.\n\n{card_text}"
    if edit:
        await msg.edit_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await msg.answer(text, parse_mode="HTML", reply_markup=kb)


# ── HISTORY / DELETE ENTRY ─────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("loy_hist:"))
async def cb_history(cb: CallbackQuery, config: LoyaltyConfig):
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    client_id = _parse_id(cb.data.split(":", 1)[1])
    client = None if client_id is None else await _client_row(config.db_path, client_id)
    if client is None:
        await cb.message.edit_text("Клиент не найден."); return
    entries = await _client_entries(config.db_path, client_id)
    if not entries:
        await cb.message.edit_text(
            f"📋 <b>{_esc(client['name'])}</b>\n\nОпераций пока нет.",
            parse_mode="HTML", reply_markup=kb_history([], client_id),
        )
        return
    await cb.message.edit_text(
        f"📋 <b>История: {_esc(client['name'])}</b>\n\nНажмите операцию, чтобы удалить её:",
        parse_mode="HTML", reply_markup=kb_history(entries, client_id),
    )

@router.callback_query(F.data.startswith("loy_entry_del:"))
async def cb_entry_del(cb: CallbackQuery, config: LoyaltyConfig):
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    parts = cb.data.split(":", 2)
    if len(parts) != 3:
        await cb.message.edit_text("Некорректные данные."); return
    entry_id, client_id = _parse_id(parts[1]), _parse_id(parts[2])
    if entry_id is None or client_id is None:
        await cb.message.edit_text("Некорректные данные."); return
    entry = await _entry_row(config.db_path, entry_id)
    if entry is not None:
        await _delete_entry(config.db_path, entry_id)
    client = await _client_row(config.db_path, client_id)
    if client is None:
        await cb.message.edit_text("Клиент не найден."); return
    entries = await _client_entries(config.db_path, client_id)
    await cb.message.edit_text(
        f"🗑 Операция удалена.\n\n📋 <b>История: {_esc(client['name'])}</b>",
        parse_mode="HTML", reply_markup=kb_history(entries, client_id),
    )


# ── DELETE CLIENT ───────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("loy_del_ask:"))
async def cb_del_ask(cb: CallbackQuery, config: LoyaltyConfig):
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    client_id = _parse_id(cb.data.split(":", 1)[1])
    client = None if client_id is None else await _client_row(config.db_path, client_id)
    if client is None:
        await cb.message.edit_text("Клиент не найден."); return
    await cb.message.edit_text(
        f"⚠️ Удалить клиента <b>{_esc(client['name'])}</b> и всю историю баллов? "
        f"Это нельзя отменить.",
        parse_mode="HTML", reply_markup=kb_delete_confirm(client_id),
    )

@router.callback_query(F.data.startswith("loy_del_confirm:"))
async def cb_del_confirm(cb: CallbackQuery, config: LoyaltyConfig):
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    client_id = _parse_id(cb.data.split(":", 1)[1])
    if client_id is None:
        await cb.message.edit_text("Некорректные данные."); return
    await _delete_client(config.db_path, client_id)
    text, kb = await _render_list(config.db_path, DEFAULT_SORT)
    await cb.message.edit_text(f"🗑 Клиент удалён.\n\n{text}", parse_mode="HTML", reply_markup=kb)


# ── SETTINGS: курс начисления ─────────────────────────────────────────────────

def _settings_text(rate: float) -> str:
    example_amount = 500.0
    example_points = _compute_points(example_amount, rate)
    return (
        f"💱 <b>Настройки начисления</b>\n\n"
        f"Текущий курс: 1 балл = {_fmt_num(rate)}{CURRENCY_SYMBOL}\n"
        f"Например, покупка на {_fmt_num(example_amount)}{CURRENCY_SYMBOL} даст "
        f"{_fmt_num(example_points)} {POINTS_UNIT_LABEL}."
    )

@router.message(F.text == "💱 Настройки", F.chat.type == "private")
async def settings_entry(msg: Message, state: FSMContext, config: LoyaltyConfig):
    if not _is_bot_admin(msg.from_user.id, config):
        await msg.answer(NO_ACCESS_TEXT); return
    await state.clear()
    rate = await _get_rate(config.db_path)
    await msg.answer(_settings_text(rate), parse_mode="HTML", reply_markup=kb_settings())

@router.callback_query(F.data == "loy_settings_back")
async def cb_settings_back(cb: CallbackQuery, state: FSMContext, config: LoyaltyConfig):
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    await state.clear()
    rate = await _get_rate(config.db_path)
    await cb.message.edit_text(_settings_text(rate), parse_mode="HTML", reply_markup=kb_settings())

@router.callback_query(F.data == "loy_rate_edit")
async def cb_rate_edit(cb: CallbackQuery, state: FSMContext, config: LoyaltyConfig):
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    await state.update_data(started_at=time.time())
    await state.set_state(RateFlow.value)
    await cb.message.edit_text(
        "✏️ Новый курс — сколько рублей за 1 балл?\nНапример: 100",
        reply_markup=kb_cancel("loy_settings_back"),
    )

@router.message(RateFlow.value, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def rate_value(msg: Message, state: FSMContext, config: LoyaltyConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("⏳ Время ожидания истекло — начните заново кнопкой «💱 Настройки»."); return
    if not _is_bot_admin(msg.from_user.id, config):
        await state.clear(); return
    rate = _parse_rate_input(msg.text)
    if rate is None:
        await msg.answer("Введите положительное число, например: 100"); return
    await _set_rate(config.db_path, rate)
    await state.clear()
    await msg.answer(f"✅ Курс обновлён.\n\n{_settings_text(rate)}", parse_mode="HTML", reply_markup=kb_settings())


# ── ADMIN: bot-admins panel (same pattern as debtors.py/shop_catalog.py) ──────

async def _admins_list_text(config: LoyaltyConfig) -> str:
    ids = sorted(_load_admins(config.admins_file))
    if not ids:
        return "👥 Пусто"
    return "👥 <b>Администраторы бота:</b>\n\n" + "\n".join(f"• <code>{_esc(i)}</code>" for i in ids)

@router.message(F.text == "⚙️ Админы бота", F.chat.type == "private")
async def admins_panel_entry(msg: Message, state: FSMContext, config: LoyaltyConfig):
    if not _is_bot_admin(msg.from_user.id, config):
        await msg.answer(NO_ACCESS_TEXT); return
    await state.clear()
    await msg.answer(await _admins_list_text(config), parse_mode="HTML", reply_markup=kb_admins_panel())

@router.callback_query(F.data == "loy_adm_admins")
async def cb_adm_admins(cb: CallbackQuery, state: FSMContext, config: LoyaltyConfig):
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    await state.clear()
    await cb.message.edit_text(await _admins_list_text(config), parse_mode="HTML", reply_markup=kb_admins_panel())

@router.callback_query(F.data == "loy_adm_addadmin")
async def cb_adm_addadmin(cb: CallbackQuery, state: FSMContext, config: LoyaltyConfig):
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    await state.update_data(started_at=time.time())
    await state.set_state(AdminFlow.add_admin)
    await cb.message.edit_text(
        "Пришлите Telegram ID пользователя, которого нужно сделать администратором:",
        reply_markup=kb_cancel("loy_adm_admins"),
    )

@router.message(AdminFlow.add_admin, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def adm_add_admin(msg: Message, state: FSMContext, config: LoyaltyConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("⏳ Время ожидания истекло — начните заново кнопкой «⚙️ Админы бота»."); return
    if not _is_bot_admin(msg.from_user.id, config):
        await state.clear(); return
    text = msg.text.strip()
    if not _valid_telegram_id(text):
        await msg.answer("Пришлите числовой Telegram ID."); return
    ids = _load_admins(config.admins_file); ids.add(text); _save_admins(config.admins_file, ids)
    logger.info(f"adm_add_admin: {text} added as bot admin by {msg.from_user.id}")
    await state.clear()
    await msg.answer(f"✅ <code>{_esc(text)}</code> добавлен.", parse_mode="HTML")
    await msg.answer(await _admins_list_text(config), parse_mode="HTML", reply_markup=kb_admins_panel())

@router.callback_query(F.data == "loy_adm_removeadmin")
async def cb_adm_removeadmin(cb: CallbackQuery, state: FSMContext, config: LoyaltyConfig):
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    ids = sorted(_load_admins(config.admins_file))
    if len(ids) <= 1:
        await cb.message.edit_text(
            "⚠️ Нельзя удалить единственного администратора.\n\n" + await _admins_list_text(config),
            parse_mode="HTML", reply_markup=kb_admins_panel(),
        )
        return
    if len(ids) <= MAX_ADMIN_REMOVE_BUTTONS:
        await state.update_data(remove_admin_ids=ids)
        await cb.message.edit_text("Выберите администратора для удаления:", reply_markup=kb_remove_admins(ids))
    else:
        await state.update_data(started_at=time.time())
        await state.set_state(AdminFlow.remove_admin_pick)
        await cb.message.edit_text("Пришлите Telegram ID администратора для удаления:", reply_markup=kb_cancel("loy_adm_admins"))

@router.callback_query(F.data.startswith("loy_adm_rma:"))
async def cb_adm_rma(cb: CallbackQuery, state: FSMContext, config: LoyaltyConfig):
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
        await cb.message.edit_text(await _admins_list_text(config), parse_mode="HTML", reply_markup=kb_admins_panel()); return
    target = ids[idx]
    current = _load_admins(config.admins_file)
    if target in current and len(current) <= 1:
        await cb.message.edit_text(
            "⚠️ Нельзя удалить единственного администратора.\n\n" + await _admins_list_text(config),
            parse_mode="HTML", reply_markup=kb_admins_panel(),
        )
        return
    current.discard(target); _save_admins(config.admins_file, current)
    logger.info(f"cb_adm_rma: {target} removed as bot admin by {cb.from_user.id}")
    await state.clear()
    await cb.message.edit_text(await _admins_list_text(config), parse_mode="HTML", reply_markup=kb_admins_panel())

@router.message(AdminFlow.remove_admin_pick, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def adm_remove_admin_text(msg: Message, state: FSMContext, config: LoyaltyConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("⏳ Время ожидания истекло — начните заново."); return
    if not _is_bot_admin(msg.from_user.id, config):
        await state.clear(); return
    text = msg.text.strip()
    if not _valid_telegram_id(text):
        await msg.answer("Пришлите числовой Telegram ID."); return
    ids = _load_admins(config.admins_file)
    if text in ids and len(ids) <= 1:
        await msg.answer("⚠️ Нельзя удалить единственного администратора."); return
    ids.discard(text); _save_admins(config.admins_file, ids)
    logger.info(f"adm_remove_admin_text: {text} removed as bot admin by {msg.from_user.id}")
    await state.clear()
    await msg.answer(await _admins_list_text(config), parse_mode="HTML", reply_markup=kb_admins_panel())


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
