# TEMPLATE: debtors
# USE FOR: личный учёт неформальных долгов между людьми (не бухгалтерия бизнеса) —
#          кто кому должен, долги друзьям/родственникам, "мне должны"/"я должен",
#          напоминания владельцу о старых непогашенных долгах
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
from datetime import date
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

from db.database import add_bot_admin, remove_bot_admin

# ── CUSTOMIZE ────────────────────────────────────────────────────────────────
# Same status as every other template's CUSTOMIZE block: per-file source-text
# customization Claude edits when generating a specific bot, not per-bot
# runtime state. Debtors/entries themselves are runtime data, added/removed
# via this bot's own menus, not by editing this file.
BOT_DESCRIPTION = "Личный учёт неформальных долгов: кто кому должен, остатки, напоминания."
WELCOME_TEXT = (
    "🤝 <b>Учёт долгов</b>\n\n"
    "Личный трекер долгов между вами и другими людьми — НЕ бухгалтерия бизнеса "
    "и НЕ платёжный инструмент. Бот никому ничего не отправляет сам — только "
    "готовит текст напоминания, который вы сами копируете и шлёте должнику.\n\n"
    "Выберите действие:"
)
NO_ACCESS_TEXT = "Это личный трекер долгов владельца — у вас нет доступа."
CURRENCY_SYMBOL = "₽"
NAME_MAX_LEN = 100
CONTACT_MAX_LEN = 200
DESCRIPTION_MAX_LEN = 300
AMOUNT_MAX = 100_000_000
HISTORY_LIMIT = 30
CARD_PREVIEW_LIMIT = 5
STALE_DAYS = 30  # entries older than this are flagged as "давно не платил"
# ── END CUSTOMIZE ─────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
router = Router()

SORT_OPTIONS = ("owed_to_me", "i_owe", "stale")
SORT_LABELS = {
    "owed_to_me": "Мне должны ↓",
    "i_owe": "Я должен ↓",
    "stale": "Давно не платил",
}
FILTER_OPTIONS = ("all", "owed_to_me", "i_owe", "settled")
FILTER_LABELS = {
    "all": "Все",
    "owed_to_me": "Мне должны",
    "i_owe": "Я должен",
    "settled": "Закрытые",
}
DEFAULT_SORT = "owed_to_me"
DEFAULT_FILTER = "all"

# Same rationale as every other template's FLOW_TIMEOUT_SECONDS: MemoryStorage
# keeps a state until explicitly cleared — without an expiry, an admin who
# opens an add-debt/add-debtor prompt and goes quiet has every LATER plain
# message silently captured by that stale flow step.
FLOW_TIMEOUT_SECONDS = 300

# Guards _save_entry_and_reply against a double-tap inserting the same debt
# entry twice — see the docstring there. Keyed by debtor_id, same pattern as
# accountant.py's _busy_excel_exports.
_busy_entry_saves: set[int] = set()


def _flow_expired(data: dict) -> bool:
    started_at = data.get("started_at")
    return started_at is None or (time.time() - started_at) > FLOW_TIMEOUT_SECONDS


def _esc(value, max_len: int = 500) -> str:
    """HTML-escapes AND length-bounds any user-supplied text before it goes
    into a parse_mode="HTML" message — same helper/rationale as
    accountant.py's/shop_catalog.py's _esc()."""
    text = str(value) if value is not None else ""
    if len(text) > max_len:
        text = text[:max_len] + "…"
    return html.escape(text)


def _short(label: str, max_len: int = 40) -> str:
    return label if len(label) <= max_len else label[:max_len - 1] + "…"


def _valid_telegram_id(text: str) -> bool:
    """Same guard as shop_catalog.py's/moderator.py's admin-id validator."""
    if not (bool(text) and text.isascii() and text.isdigit() and len(text) <= 15):
        return False
    return int(text) > 0 and str(int(text)) == text


def _parse_id(text: str) -> int | None:
    """callback_data is bot-generated, but Telegram's API doesn't stop a
    custom client from sending arbitrary callback_query data — int() on an
    unguarded split() would raise on a forged value. Same rationale as
    cb_adm_rma's existing try/except ValueError, applied consistently."""
    try:
        return int(text)
    except ValueError:
        return None


def _parse_amount(text: str) -> float | None:
    """Whole-currency-unit amounts only (no kopecks/cents) — same convention
    as shop_catalog.py's INTEGER price field. _fmt_amount below always
    formats with zero decimals (",.0f"), used everywhere a balance is shown
    to the admin; silently accepting a fractional amount here would let a
    real remainder (e.g. 0.4₽ still owed) round away to "0₽ (в расчёте)",
    indistinguishable from a fully settled debt — review-found, see
    balance-correctness tests."""
    cleaned = re.sub(r"[^\d.,]", "", text).replace(",", ".")
    try:
        amount = float(cleaned)
    except ValueError:
        return None
    if amount <= 0 or amount > AMOUNT_MAX or amount != int(amount):
        return None
    return amount


def _fmt_amount(amount: float) -> str:
    return f"{amount:,.0f}".replace(",", " ")


def _fmt_balance(balance: float) -> str:
    if balance > 0:
        return f"+{_fmt_amount(balance)}{CURRENCY_SYMBOL} (вам должны)"
    if balance < 0:
        return f"-{_fmt_amount(abs(balance))}{CURRENCY_SYMBOL} (вы должны)"
    return f"0{CURRENCY_SYMBOL} (в расчёте)"


def _days_ago(date_str: str | None) -> int | None:
    if not date_str:
        return None
    try:
        return (date.today() - date.fromisoformat(date_str)).days
    except ValueError:
        return None


# ── config ───────────────────────────────────────────────────────────────────
# Same shape/contract as every other template's Config dataclass — see
# docs/STAGE2_DESIGN.md "Config-контракт шаблона".

@dataclass
class DebtorsConfig:
    bot_name: str
    db_path: str
    admins_file: Path
    welcome_image: Path
    display_name: str | None = None
    group_chat_id: str | None = None
    bot_id: int | None = None
    owner_telegram_id: int | None = None


def _paths_for(name: str, data_dir: Path) -> DebtorsConfig:
    return DebtorsConfig(
        bot_name=name,
        db_path=str(data_dir / f"{name}_data.db"),
        admins_file=data_dir / f"admins_{name}.json",
        welcome_image=data_dir / "bot_images" / f"{name}.jpg",
    )


def config_from_env() -> DebtorsConfig:
    """Standalone/subprocess mode."""
    name = Path(__file__).stem
    data_dir = Path(os.getenv("DATA_DIR", "./data"))
    data_dir.mkdir(exist_ok=True)
    return _paths_for(name, data_dir)


def config_from_bot_row(bot_row: dict, data_dir: Path) -> DebtorsConfig:
    """Webhook runtime mode. Paths keyed by bot_row["bot_id"] (bots.id, the
    physically unique AUTOINCREMENT PK) — NOT bot_row["name"] — same
    reasoning as every other template's config_from_bot_row."""
    bot_id = bot_row["bot_id"]
    config = DebtorsConfig(
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
    """Injects this bot's DebtorsConfig into data["config"]."""

    def __init__(self, config: DebtorsConfig) -> None:
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

def _is_bot_admin(user_id: int, config: DebtorsConfig) -> bool:
    # The DB-known owner (bots.owner_telegram_id) is always an admin, even if
    # the local admins_file is empty/stale/hijacked — see cmd_start below for
    # why the file alone can't be trusted as the sole source of truth.
    if config.owner_telegram_id is not None and str(user_id) == str(config.owner_telegram_id):
        return True
    return str(user_id) in _load_admins(config.admins_file)


# ── db ────────────────────────────────────────────────────────────────────────

async def init_db(db_path: str):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS debtors (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL,
                contact    TEXT,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        # amount is SIGNED: positive increases what the debtor owes the owner
        # ("мне должны"), negative decreases it (repayment received, or the
        # owner borrowing from the debtor). A partial repayment is a NEW row
        # with the opposite sign — the original debt row is never edited.
        # Balance = SUM(amount) over a debtor's entries; this is the single
        # net number the owner sees, deliberately not two separate per-
        # direction tallies (see design discussion — one debtor, one relationship).
        await db.execute("""
            CREATE TABLE IF NOT EXISTS debt_entries (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                debtor_id   INTEGER NOT NULL REFERENCES debtors(id),
                amount      REAL NOT NULL,
                description TEXT,
                entry_date  TEXT DEFAULT (date('now','localtime')),
                created_at  TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.commit()


# ── mini-app config (see docs/MINIAPP_DESIGN.md, templates/car_rental.py's
# own miniapp_config for the authoring contract) ────────────────────────────
# debt_entries is creatable, but only with a plain unsigned "amount" field —
# the real sign (мне должны / я должен) is a business decision this template
# always makes via the two-button cb_add_start flow (dbt_add:...:p vs ...:m),
# never a raw column the generic create form should expose; a create-form
# amount here writes the value as-is (positive), matching the "мне должны"
# direction, the more common case.
miniapp_config = {
    "resources": [
        {
            "name": "debtors",
            "table": "debtors",
            "order_by": "name",
            "creatable": True,
            "title": "Должники",
            "titleField": "name",
            "fields": [
                {"name": "name", "required": True, "label": "Имя", "kind": "text", "list": True, "detail": True, "create": True},
                {"name": "contact", "label": "Контакт", "kind": "text", "list": False, "detail": True, "create": True},
                {"name": "created_at", "label": "Создан", "kind": "date", "list": False, "detail": True, "create": False},
            ],
        },
        {
            "name": "debt_entries",
            "table": "debt_entries",
            "order_by": "entry_date DESC, id DESC",
            "creatable": True,
            "title": "Записи",
            "fields": [
                {"name": "debtor_id", "required": True, "label": "ID должника", "kind": "number", "list": True, "detail": True, "create": True},
                {"name": "amount", "required": True, "label": "Сумма", "kind": "number", "list": True, "detail": True, "create": True},
                {"name": "description", "label": "Описание", "kind": "text", "list": False, "detail": True, "create": True},
                {"name": "entry_date", "label": "Дата", "kind": "date", "list": True, "detail": True, "create": False},
            ],
        },
    ],
}


async def _insert_debtor(db_path: str, name: str, contact: str | None) -> int:
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute("INSERT INTO debtors (name, contact) VALUES (?,?)", (name, contact))
        await db.commit()
        return cur.lastrowid

async def _debtor_row(db_path: str, debtor_id: int) -> dict | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute("SELECT * FROM debtors WHERE id=?", (debtor_id,))).fetchone()
        return dict(row) if row else None

async def _delete_debtor(db_path: str, debtor_id: int) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute("DELETE FROM debt_entries WHERE debtor_id=?", (debtor_id,))
        await db.execute("DELETE FROM debtors WHERE id=?", (debtor_id,))
        await db.commit()

async def _debtors_with_balance(db_path: str) -> list[dict]:
    """One row per debtor with a computed net `balance` and `last_entry_date`
    (NULL for a debtor with no entries yet)."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute("""
            SELECT d.id, d.name, d.contact,
                   COALESCE(SUM(e.amount), 0) AS balance,
                   MAX(e.entry_date) AS last_entry_date
            FROM debtors d
            LEFT JOIN debt_entries e ON e.debtor_id = d.id
            GROUP BY d.id
            ORDER BY d.name
        """)).fetchall()
        return [dict(r) for r in rows]

async def _debtor_stats(db_path: str, debtor_id: int) -> dict:
    async with aiosqlite.connect(db_path) as db:
        row = await (await db.execute(
            "SELECT COALESCE(SUM(amount),0), MAX(entry_date), COUNT(*) FROM debt_entries WHERE debtor_id=?",
            (debtor_id,),
        )).fetchone()
        return {"balance": row[0], "last_entry_date": row[1], "count": row[2]}

async def _debtor_entries(db_path: str, debtor_id: int, limit: int = HISTORY_LIMIT) -> list[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT * FROM debt_entries WHERE debtor_id=? ORDER BY entry_date DESC, id DESC LIMIT ?",
            (debtor_id, limit),
        )).fetchall()
        return [dict(r) for r in rows]

async def _insert_entry(db_path: str, debtor_id: int, amount: float, description: str | None) -> int:
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "INSERT INTO debt_entries (debtor_id, amount, description) VALUES (?,?,?)",
            (debtor_id, amount, description),
        )
        await db.commit()
        return cur.lastrowid

async def _entry_row(db_path: str, entry_id: int) -> dict | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute("SELECT * FROM debt_entries WHERE id=?", (entry_id,))).fetchone()
        return dict(row) if row else None

async def _delete_entry(db_path: str, entry_id: int) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute("DELETE FROM debt_entries WHERE id=?", (entry_id,))
        await db.commit()


def _sort_debtors(debtors: list[dict], sort: str) -> list[dict]:
    if sort == "i_owe":
        return sorted(debtors, key=lambda d: d["balance"])
    if sort == "stale":
        # No entries ever (last_entry_date is None) counts as maximally stale.
        return sorted(debtors, key=lambda d: (d["last_entry_date"] is not None, d["last_entry_date"] or ""))
    return sorted(debtors, key=lambda d: d["balance"], reverse=True)  # owed_to_me (default)

def _filter_debtors(debtors: list[dict], filt: str) -> list[dict]:
    if filt == "owed_to_me":
        return [d for d in debtors if d["balance"] > 0]
    if filt == "i_owe":
        return [d for d in debtors if d["balance"] < 0]
    if filt == "settled":
        return [d for d in debtors if d["balance"] == 0]
    return debtors


# ── keyboards: main ─────────────────────────────────────────────────────────

def kb_main() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="👥 Должники")],
        [KeyboardButton(text="⚙️ Админы бота")],
    ], resize_keyboard=True)


# ── keyboards: debtors list ───────────────────────────────────────────────────

def kb_debtors_list(debtors: list[dict], sort: str, filt: str) -> InlineKeyboardMarkup:
    rows = []
    for d in debtors:
        stale_mark = ""
        if d["balance"] > 0:
            days = _days_ago(d["last_entry_date"])
            if days is not None and days > STALE_DAYS:
                stale_mark = "⏰ "
        label = f"{stale_mark}{_short(d['name'], 24)} — {_fmt_balance(d['balance'])}"
        rows.append([InlineKeyboardButton(text=_short(label, 45), callback_data=f"dbt_view:{d['id']}")])
    if not rows:
        rows.append([InlineKeyboardButton(text="Никого нет по этому фильтру", callback_data="dbt_noop")])

    sort_row = [
        InlineKeyboardButton(
            text=("✅ " if sort == s else "") + SORT_LABELS[s],
            callback_data=f"dbt_list:{s}:{filt}",
        ) for s in SORT_OPTIONS
    ]
    filter_row = [
        InlineKeyboardButton(
            text=("✅ " if filt == f else "") + FILTER_LABELS[f],
            callback_data=f"dbt_list:{sort}:{f}",
        ) for f in FILTER_OPTIONS
    ]
    rows.append(sort_row)
    rows.append(filter_row)
    rows.append([InlineKeyboardButton(text="➕ Новый должник", callback_data="dbt_new")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_cancel(callback_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data=callback_data)]])

def kb_new_debtor_contact() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏩ Без контакта", callback_data="dbt_new_skip_contact")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="dbt_list:" + DEFAULT_SORT + ":" + DEFAULT_FILTER)],
    ])


# ── keyboards: debtor card ────────────────────────────────────────────────────

def kb_debtor_card(debtor_id: int, balance: float) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="➕ Мне должны", callback_data=f"dbt_add:{debtor_id}:p"),
            InlineKeyboardButton(text="➖ Я должен", callback_data=f"dbt_add:{debtor_id}:m"),
        ],
    ]
    if balance != 0:
        rows.append([InlineKeyboardButton(text="💵 Погашение", callback_data=f"dbt_repay:{debtor_id}")])
    if balance > 0:
        rows.append([InlineKeyboardButton(text="🔔 Напомнить о долге", callback_data=f"dbt_remind:{debtor_id}")])
    rows.append([
        InlineKeyboardButton(text="📋 История", callback_data=f"dbt_hist:{debtor_id}"),
        InlineKeyboardButton(text="🗑 Удалить", callback_data=f"dbt_del_ask:{debtor_id}"),
    ])
    rows.append([InlineKeyboardButton(text="◀️ К списку", callback_data=f"dbt_list:{DEFAULT_SORT}:{DEFAULT_FILTER}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_entry_step(cancel_cb: str, skip_desc: bool = False) -> InlineKeyboardMarkup:
    rows = []
    if skip_desc:
        rows.append([InlineKeyboardButton(text="⏩ Пропустить", callback_data="dbt_skip_desc")])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data=cancel_cb)])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_delete_confirm(debtor_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"dbt_del_confirm:{debtor_id}"),
        InlineKeyboardButton(text="❌ Отмена", callback_data=f"dbt_view:{debtor_id}"),
    ]])

def kb_history(entries: list[dict], debtor_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text=_short(f"{e['entry_date']}  {'+' if e['amount'] >= 0 else ''}{_fmt_amount(e['amount'])}{CURRENCY_SYMBOL}", 40),
            callback_data=f"dbt_entry_del:{e['id']}:{debtor_id}",
        )] for e in entries
    ]
    rows.append([InlineKeyboardButton(text="◀️ К должнику", callback_data=f"dbt_view:{debtor_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── keyboards: admins panel (same pattern as shop_catalog.py/moderator.py) ────

def kb_admins_panel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить админа", callback_data="dbt_adm_addadmin")],
        [InlineKeyboardButton(text="➖ Убрать админа", callback_data="dbt_adm_removeadmin")],
    ])

MAX_ADMIN_REMOVE_BUTTONS = 30

def kb_remove_admins(ids: list[str]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=admin_id, callback_data=f"dbt_adm_rma:{i}")] for i, admin_id in enumerate(ids)]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="dbt_adm_admins")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── FSM ───────────────────────────────────────────────────────────────────────

class DebtorNew(StatesGroup):
    name = State(); contact = State()

class EntryFlow(StatesGroup):
    amount = State(); description = State()

class AdminFlow(StatesGroup):
    add_admin = State(); remove_admin_pick = State()


# ── /start ────────────────────────────────────────────────────────────────────

@router.message(Command("start"), F.chat.type == "private")
async def cmd_start(message: Message, state: FSMContext, config: DebtorsConfig):
    await state.clear()
    admins = _load_admins(config.admins_file)
    sender_id = message.from_user.id
    # Bug fixed here: this used to grant admin to whoever sent /start FIRST,
    # which lets any client who messages the bot before the owner does
    # permanently seize the admin panel. When bots.owner_telegram_id is known
    # (webhook/production mode), only that user may claim the empty-admins
    # bootstrap slot. In standalone/env mode (owner_telegram_id unknown) the
    # old first-comer behavior is kept as the only option available.
    is_owner = config.owner_telegram_id is not None and sender_id == config.owner_telegram_id
    first_time_admin = not admins and (is_owner or config.owner_telegram_id is None)
    if first_time_admin:
        admins = {str(sender_id)}
        _save_admins(config.admins_file, admins)
        if config.bot_id is not None:
            try:
                await add_bot_admin(config.bot_id, str(sender_id))
            except Exception as e:
                logger.warning(f"cmd_start: add_bot_admin sync failed for bot {config.bot_id}: {e}")
    is_admin = _is_bot_admin(sender_id, config)
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
            "Кнопка «👥 Должники» открывает список и все действия.",
            parse_mode="HTML",
        )


# ── DEBTORS LIST ──────────────────────────────────────────────────────────────

async def _render_list(db_path: str, sort: str, filt: str) -> tuple[str, InlineKeyboardMarkup]:
    all_debtors = await _debtors_with_balance(db_path)
    shown = _sort_debtors(_filter_debtors(all_debtors, filt), sort)
    total_owed_to_me = sum(d["balance"] for d in all_debtors if d["balance"] > 0)
    total_i_owe = sum(-d["balance"] for d in all_debtors if d["balance"] < 0)
    text = (
        f"👥 <b>Должники</b>\n\n"
        f"📈 Всего мне должны: {_fmt_amount(total_owed_to_me)}{CURRENCY_SYMBOL}\n"
        f"📉 Всего я должен: {_fmt_amount(total_i_owe)}{CURRENCY_SYMBOL}\n"
    )
    return text, kb_debtors_list(shown, sort, filt)

@router.message(F.text == "👥 Должники", F.chat.type == "private")
async def debtors_list(msg: Message, state: FSMContext, config: DebtorsConfig):
    if not _is_bot_admin(msg.from_user.id, config):
        await msg.answer(NO_ACCESS_TEXT); return
    await state.clear()
    text, kb = await _render_list(config.db_path, DEFAULT_SORT, DEFAULT_FILTER)
    await msg.answer(text, parse_mode="HTML", reply_markup=kb)

@router.callback_query(F.data.startswith("dbt_list:"))
async def cb_list(cb: CallbackQuery, state: FSMContext, config: DebtorsConfig):
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    await state.clear()
    _, sort, filt = cb.data.split(":", 2)
    if sort not in SORT_OPTIONS or filt not in FILTER_OPTIONS:
        sort, filt = DEFAULT_SORT, DEFAULT_FILTER
    text, kb = await _render_list(config.db_path, sort, filt)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)

@router.callback_query(F.data == "dbt_noop")
async def cb_noop(cb: CallbackQuery):
    await cb.answer()


# ── NEW DEBTOR ────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "dbt_new")
async def cb_new_debtor(cb: CallbackQuery, state: FSMContext, config: DebtorsConfig):
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    await state.update_data(started_at=time.time())
    await state.set_state(DebtorNew.name)
    await cb.message.edit_text(
        "✏️ Введите имя должника:", reply_markup=kb_cancel(f"dbt_list:{DEFAULT_SORT}:{DEFAULT_FILTER}")
    )

@router.message(DebtorNew.name, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def new_debtor_name(msg: Message, state: FSMContext, config: DebtorsConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("⏳ Время ожидания истекло — начните заново кнопкой «👥 Должники»."); return
    if not _is_bot_admin(msg.from_user.id, config):
        await state.clear(); return
    name = msg.text.strip()[:NAME_MAX_LEN]
    if not name:
        await msg.answer("Имя не может быть пустым."); return
    await state.update_data(name=name)
    await state.set_state(DebtorNew.contact)
    await msg.answer("📞 Контакт (необязательно) — телефон/@username, или нажмите «Без контакта»:",
                     reply_markup=kb_new_debtor_contact())

@router.message(DebtorNew.contact, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def new_debtor_contact(msg: Message, state: FSMContext, config: DebtorsConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("⏳ Время ожидания истекло — начните заново кнопкой «👥 Должники»."); return
    if not _is_bot_admin(msg.from_user.id, config):
        await state.clear(); return
    contact = msg.text.strip()[:CONTACT_MAX_LEN]
    await _finish_new_debtor(msg, state, config, data["name"], contact or None)

@router.callback_query(DebtorNew.contact, F.data == "dbt_new_skip_contact")
async def new_debtor_skip_contact(cb: CallbackQuery, state: FSMContext, config: DebtorsConfig):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("⏳ Время ожидания истекло — начните заново кнопкой «👥 Должники»."); return
    if not _is_bot_admin(cb.from_user.id, config):
        await state.clear(); return
    await _finish_new_debtor(cb.message, state, config, data["name"], None, edit=True)

async def _finish_new_debtor(msg: Message, state: FSMContext, config: DebtorsConfig,
                              name: str, contact: str | None, edit: bool = False) -> None:
    debtor_id = await _insert_debtor(config.db_path, name, contact)
    await state.clear()
    stats = await _debtor_stats(config.db_path, debtor_id)
    text, kb = await _render_card(config.db_path, debtor_id, stats)
    text = f"✅ Должник добавлен.\n\n{text}"
    if edit:
        await msg.edit_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await msg.answer(text, parse_mode="HTML", reply_markup=kb)


# ── DEBTOR CARD ───────────────────────────────────────────────────────────────

async def _render_card(db_path: str, debtor_id: int, stats: dict | None = None) -> tuple[str, InlineKeyboardMarkup] | tuple[None, None]:
    debtor = await _debtor_row(db_path, debtor_id)
    if debtor is None:
        return None, None
    if stats is None:
        stats = await _debtor_stats(db_path, debtor_id)
    entries = await _debtor_entries(db_path, debtor_id, CARD_PREVIEW_LIMIT)
    lines = [f"👤 <b>{_esc(debtor['name'])}</b>"]
    if debtor["contact"]:
        lines.append(f"📞 {_esc(debtor['contact'])}")
    lines.append(f"\n💰 Остаток: <b>{_fmt_balance(stats['balance'])}</b>")
    if entries:
        lines.append("\n<b>Последние записи:</b>")
        for e in entries:
            sign = "+" if e["amount"] >= 0 else ""
            lines.append(f"  {e['entry_date']}  {sign}{_fmt_amount(e['amount'])}{CURRENCY_SYMBOL}"
                        + (f" — {_esc(e['description'], 80)}" if e["description"] else ""))
    else:
        lines.append("\nЗаписей пока нет.")
    return "\n".join(lines), kb_debtor_card(debtor_id, stats["balance"])

@router.callback_query(F.data.startswith("dbt_view:"))
async def cb_view_debtor(cb: CallbackQuery, state: FSMContext, config: DebtorsConfig):
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    await state.clear()
    debtor_id = _parse_id(cb.data.split(":", 1)[1])
    text, kb = (None, None) if debtor_id is None else await _render_card(config.db_path, debtor_id)
    if text is None:
        await cb.message.edit_text("Должник не найден.", reply_markup=kb_cancel(f"dbt_list:{DEFAULT_SORT}:{DEFAULT_FILTER}"))
        return
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


# ── ADD DEBT / REPAYMENT (shared amount+description flow, sign in FSM data) ──

@router.callback_query(F.data.startswith("dbt_add:"))
async def cb_add_start(cb: CallbackQuery, state: FSMContext, config: DebtorsConfig):
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    _, debtor_id_s, dir_code = cb.data.split(":", 2)
    debtor_id = _parse_id(debtor_id_s)
    debtor = None if debtor_id is None else await _debtor_row(config.db_path, debtor_id)
    if debtor is None:
        await cb.message.edit_text("Должник не найден."); return
    sign = 1 if dir_code == "p" else -1
    label = "мне должны" if sign > 0 else "я должен"
    await state.update_data(started_at=time.time(), debtor_id=debtor_id, sign=sign)
    await state.set_state(EntryFlow.amount)
    await cb.message.edit_text(
        f"💰 Сумма ({label}) для <b>{_esc(debtor['name'])}</b>:",
        parse_mode="HTML", reply_markup=kb_entry_step(f"dbt_view:{debtor_id}"),
    )

@router.callback_query(F.data.startswith("dbt_repay:"))
async def cb_repay_start(cb: CallbackQuery, state: FSMContext, config: DebtorsConfig):
    if not _is_bot_admin(cb.from_user.id, config):
        await cb.answer(); return
    debtor_id = _parse_id(cb.data.split(":", 1)[1])
    debtor = None if debtor_id is None else await _debtor_row(config.db_path, debtor_id)
    if debtor is None:
        await cb.answer(); await cb.message.edit_text("Должник не найден."); return
    stats = await _debtor_stats(config.db_path, debtor_id)
    balance = stats["balance"]
    if balance == 0:
        await cb.answer("Долг закрыт — нечего гасить.", show_alert=True); return
    await cb.answer()
    sign = -1 if balance > 0 else 1
    await state.update_data(started_at=time.time(), debtor_id=debtor_id, sign=sign)
    await state.set_state(EntryFlow.amount)
    await cb.message.edit_text(
        f"💵 Сумма погашения для <b>{_esc(debtor['name'])}</b> "
        f"(текущий остаток: {_fmt_balance(balance)}):",
        parse_mode="HTML", reply_markup=kb_entry_step(f"dbt_view:{debtor_id}"),
    )

@router.message(EntryFlow.amount, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def entry_amount(msg: Message, state: FSMContext, config: DebtorsConfig):
    data = await state.get_data()
    debtor_id = data.get("debtor_id")
    if _flow_expired(data):
        await state.clear()
        await msg.answer("⏳ Время ожидания истекло — начните заново из карточки должника."); return
    if not _is_bot_admin(msg.from_user.id, config):
        await state.clear(); return
    amount = _parse_amount(msg.text)
    if amount is None:
        await msg.answer("Введите положительное число, например: 1500"); return
    await state.update_data(amount=amount)
    await state.set_state(EntryFlow.description)
    await msg.answer(
        "📝 Описание (необязательно) — отправьте текст или нажмите «Пропустить»:",
        reply_markup=kb_entry_step(f"dbt_view:{debtor_id}", skip_desc=True),
    )

@router.message(EntryFlow.description, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def entry_description(msg: Message, state: FSMContext, config: DebtorsConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("⏳ Время ожидания истекло — начните заново из карточки должника."); return
    if not _is_bot_admin(msg.from_user.id, config):
        await state.clear(); return
    description = msg.text.strip()[:DESCRIPTION_MAX_LEN] or None
    await _save_entry_and_reply(msg, state, config, description)

@router.callback_query(EntryFlow.description, F.data == "dbt_skip_desc")
async def entry_skip_description(cb: CallbackQuery, state: FSMContext, config: DebtorsConfig):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("⏳ Время ожидания истекло — начните заново из карточки должника."); return
    if not _is_bot_admin(cb.from_user.id, config):
        await state.clear(); return
    await _save_entry_and_reply(cb.message, state, config, None, edit=True)

async def _save_entry_and_reply(msg: Message, state: FSMContext, config: DebtorsConfig,
                                 description: str | None, edit: bool = False) -> None:
    data = await state.get_data()
    debtor_id = data.get("debtor_id")
    if debtor_id is None or debtor_id in _busy_entry_saves:
        # Review-found: a fast double-tap on "Пропустить" (or a duplicate
        # description submit) can fire both entry_description and
        # entry_skip_description before either finishes — without this
        # guard both read the same not-yet-cleared FSM data and insert the
        # entry TWICE, silently doubling the amount. debtor_id is add()ed
        # to the module-level set below with no `await` before it, so the
        # check-then-add itself can't be interleaved by another coroutine
        # on this single-threaded event loop — same pattern as
        # accountant.py's _busy_excel_exports guard.
        return
    _busy_entry_saves.add(debtor_id)
    try:
        signed_amount = data["amount"] * data["sign"]
        await _insert_entry(config.db_path, debtor_id, signed_amount, description)
        await state.clear()
    finally:
        _busy_entry_saves.discard(debtor_id)
    text, kb = await _render_card(config.db_path, debtor_id)
    if text is None:
        text, kb = "Должник не найден.", kb_cancel(f"dbt_list:{DEFAULT_SORT}:{DEFAULT_FILTER}")
    else:
        text = f"✅ Запись сохранена.\n\n{text}"
    if edit:
        await msg.edit_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await msg.answer(text, parse_mode="HTML", reply_markup=kb)


# ── REMINDER ──────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("dbt_remind:"))
async def cb_remind(cb: CallbackQuery, config: DebtorsConfig):
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    debtor_id = _parse_id(cb.data.split(":", 1)[1])
    debtor = None if debtor_id is None else await _debtor_row(config.db_path, debtor_id)
    if debtor is None:
        await cb.message.answer("Должник не найден."); return
    stats = await _debtor_stats(config.db_path, debtor_id)
    if stats["balance"] <= 0:
        await cb.message.answer("По этому должнику нет долга — напоминать не о чем."); return
    days = _days_ago(stats["last_entry_date"])
    days_part = f" (последняя запись {days} дн. назад)" if days is not None else ""
    reminder_body = (
        f"Привет, {debtor['name']}! Ты должен мне {_fmt_amount(stats['balance'])}{CURRENCY_SYMBOL}"
        f"{days_part}. Буду благодарен, если вернёшь долг в ближайшее время 🙏"
    )
    await cb.message.answer(
        "📋 <b>Текст для отправки должнику</b>\n\n"
        "Нажмите на текст ниже, чтобы скопировать, и отправьте его должнику любым удобным "
        "способом — бот не отправляет сообщения должникам напрямую.\n\n"
        f"<code>{_esc(reminder_body, 1000)}</code>",
        parse_mode="HTML",
    )


# ── HISTORY / DELETE ENTRY ─────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("dbt_hist:"))
async def cb_history(cb: CallbackQuery, config: DebtorsConfig):
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    debtor_id = _parse_id(cb.data.split(":", 1)[1])
    debtor = None if debtor_id is None else await _debtor_row(config.db_path, debtor_id)
    if debtor is None:
        await cb.message.edit_text("Должник не найден."); return
    entries = await _debtor_entries(config.db_path, debtor_id)
    if not entries:
        await cb.message.edit_text(
            f"📋 <b>{_esc(debtor['name'])}</b>\n\nЗаписей пока нет.",
            parse_mode="HTML", reply_markup=kb_history([], debtor_id),
        )
        return
    await cb.message.edit_text(
        f"📋 <b>История: {_esc(debtor['name'])}</b>\n\nНажмите запись, чтобы удалить её:",
        parse_mode="HTML", reply_markup=kb_history(entries, debtor_id),
    )

@router.callback_query(F.data.startswith("dbt_entry_del:"))
async def cb_entry_del(cb: CallbackQuery, config: DebtorsConfig):
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    _, entry_id_s, debtor_id_s = cb.data.split(":", 2)
    entry_id, debtor_id = _parse_id(entry_id_s), _parse_id(debtor_id_s)
    if entry_id is None or debtor_id is None:
        await cb.message.edit_text("Некорректные данные."); return
    entry = await _entry_row(config.db_path, entry_id)
    if entry is not None:
        await _delete_entry(config.db_path, entry_id)
    debtor = await _debtor_row(config.db_path, debtor_id)
    if debtor is None:
        await cb.message.edit_text("Должник не найден."); return
    entries = await _debtor_entries(config.db_path, debtor_id)
    await cb.message.edit_text(
        f"🗑 Запись удалена.\n\n📋 <b>История: {_esc(debtor['name'])}</b>",
        parse_mode="HTML", reply_markup=kb_history(entries, debtor_id),
    )


# ── DELETE DEBTOR ───────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("dbt_del_ask:"))
async def cb_del_ask(cb: CallbackQuery, config: DebtorsConfig):
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    debtor_id = _parse_id(cb.data.split(":", 1)[1])
    debtor = None if debtor_id is None else await _debtor_row(config.db_path, debtor_id)
    if debtor is None:
        await cb.message.edit_text("Должник не найден."); return
    await cb.message.edit_text(
        f"⚠️ Удалить должника <b>{_esc(debtor['name'])}</b> и всю историю долгов? "
        f"Это нельзя отменить.",
        parse_mode="HTML", reply_markup=kb_delete_confirm(debtor_id),
    )

@router.callback_query(F.data.startswith("dbt_del_confirm:"))
async def cb_del_confirm(cb: CallbackQuery, config: DebtorsConfig):
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    debtor_id = _parse_id(cb.data.split(":", 1)[1])
    if debtor_id is None:
        await cb.message.edit_text("Некорректные данные."); return
    await _delete_debtor(config.db_path, debtor_id)
    text, kb = await _render_list(config.db_path, DEFAULT_SORT, DEFAULT_FILTER)
    await cb.message.edit_text(f"🗑 Должник удалён.\n\n{text}", parse_mode="HTML", reply_markup=kb)


# ── ADMIN: bot-admins panel (same pattern as shop_catalog.py/moderator.py) ────

async def _admins_list_text(config: DebtorsConfig) -> str:
    ids = sorted(_load_admins(config.admins_file))
    if not ids:
        return "👥 Пусто"
    return "👥 <b>Администраторы бота:</b>\n\n" + "\n".join(f"• <code>{_esc(i)}</code>" for i in ids)

@router.message(F.text == "⚙️ Админы бота", F.chat.type == "private")
async def admins_panel_entry(msg: Message, state: FSMContext, config: DebtorsConfig):
    if not _is_bot_admin(msg.from_user.id, config):
        await msg.answer(NO_ACCESS_TEXT); return
    await state.clear()
    await msg.answer(await _admins_list_text(config), parse_mode="HTML", reply_markup=kb_admins_panel())

@router.callback_query(F.data == "dbt_adm_admins")
async def cb_adm_admins(cb: CallbackQuery, state: FSMContext, config: DebtorsConfig):
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    await state.clear()
    await cb.message.edit_text(await _admins_list_text(config), parse_mode="HTML", reply_markup=kb_admins_panel())

@router.callback_query(F.data == "dbt_adm_addadmin")
async def cb_adm_addadmin(cb: CallbackQuery, state: FSMContext, config: DebtorsConfig):
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    await state.update_data(started_at=time.time())
    await state.set_state(AdminFlow.add_admin)
    await cb.message.edit_text(
        "Пришлите Telegram ID пользователя, которого нужно сделать администратором:",
        reply_markup=kb_cancel("dbt_adm_admins"),
    )

@router.message(AdminFlow.add_admin, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def adm_add_admin(msg: Message, state: FSMContext, config: DebtorsConfig):
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
    if config.bot_id is not None:
        try:
            await add_bot_admin(config.bot_id, text)
        except Exception as e:
            logger.warning(f"adm_add_admin: add_bot_admin sync failed for bot {config.bot_id}: {e}")
    logger.info(f"adm_add_admin: {text} added as bot admin by {msg.from_user.id}")
    await state.clear()
    await msg.answer(f"✅ <code>{_esc(text)}</code> добавлен.", parse_mode="HTML")
    await msg.answer(await _admins_list_text(config), parse_mode="HTML", reply_markup=kb_admins_panel())

@router.callback_query(F.data == "dbt_adm_removeadmin")
async def cb_adm_removeadmin(cb: CallbackQuery, state: FSMContext, config: DebtorsConfig):
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
        await cb.message.edit_text("Пришлите Telegram ID администратора для удаления:", reply_markup=kb_cancel("dbt_adm_admins"))

@router.callback_query(F.data.startswith("dbt_adm_rma:"))
async def cb_adm_rma(cb: CallbackQuery, state: FSMContext, config: DebtorsConfig):
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
    if config.bot_id is not None:
        try:
            await remove_bot_admin(config.bot_id, target)
        except Exception as e:
            logger.warning(f"cb_adm_rma: remove_bot_admin sync failed for bot {config.bot_id}: {e}")
    logger.info(f"cb_adm_rma: {target} removed as bot admin by {cb.from_user.id}")
    await state.clear()
    await cb.message.edit_text(await _admins_list_text(config), parse_mode="HTML", reply_markup=kb_admins_panel())

@router.message(AdminFlow.remove_admin_pick, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def adm_remove_admin_text(msg: Message, state: FSMContext, config: DebtorsConfig):
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
    if config.bot_id is not None:
        try:
            await remove_bot_admin(config.bot_id, text)
        except Exception as e:
            logger.warning(f"adm_remove_admin_text: remove_bot_admin sync failed for bot {config.bot_id}: {e}")
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
