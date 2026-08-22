# TEMPLATE: expense_tracker
# USE FOR: личный/командный учёт расходов — сумма+категория+дата+комментарий,
#          настраиваемые категории трат (без хардкода), сводка за период
#          (по категориям, топ-категории, итого: текущий/прошлый месяц/произвольный
#          период). НЕ бухгалтерия бизнеса — без двойной записи, без контрагентов,
#          без учёта доходов (для этого см. accountant)
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
from datetime import date, timedelta
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
BOT_DESCRIPTION = "Учёт расходов: сумма, категория, дата, комментарий. Настраиваемые категории. Сводка за период по категориям."
WELCOME_TEXT = (
    "💸 <b>Учёт расходов</b>\n\n"
    "Личный или командный трекер трат — НЕ бухгалтерия бизнеса (без доходов, "
    "без контрагентов, без двойной записи). Записывайте траты по категориям и "
    "смотрите сводку за любой период.\n\nВыберите действие:"
)
NO_ACCESS_TEXT = "Это закрытый трекер расходов — у вас нет доступа."
CURRENCY_SYMBOL = "₽"
# Seeded once on first init_db() via INSERT OR IGNORE — a starting point the
# admin can freely add to / deactivate, not a fixed set (design requirement:
# categories are configurable, never hardcoded at the schema level).
DEFAULT_CATEGORIES = ["Еда", "Транспорт", "Жильё", "Развлечения", "Прочее"]
CATEGORY_NAME_MAX_LEN = 50
COMMENT_MAX_LEN = 300
AMOUNT_MAX = 100_000_000
MAX_PAST_DAYS = 5 * 365  # sanity bound for a manually-typed expense date
# ── END CUSTOMIZE ─────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
router = Router()


# ── config ───────────────────────────────────────────────────────────────────
# Same pattern as every other template — see docs/STAGE2_DESIGN.md.

@dataclass
class ExpenseTrackerConfig:
    bot_name: str
    db_path: str
    admins_file: Path
    welcome_image: Path
    display_name: str | None = None
    group_chat_id: str | None = None
    bot_id: int | None = None
    owner_telegram_id: int | None = None


def _paths_for(name: str, data_dir: Path) -> ExpenseTrackerConfig:
    return ExpenseTrackerConfig(
        bot_name=name,
        db_path=str(data_dir / f"{name}_data.db"),
        admins_file=data_dir / f"admins_{name}.json",
        welcome_image=data_dir / "bot_images" / f"{name}.jpg",
    )


def config_from_env() -> ExpenseTrackerConfig:
    """Standalone/subprocess mode."""
    name = Path(__file__).stem
    data_dir = Path(os.getenv("DATA_DIR", "./data"))
    data_dir.mkdir(exist_ok=True)
    return _paths_for(name, data_dir)


def config_from_bot_row(bot_row: dict, data_dir: Path) -> ExpenseTrackerConfig:
    """Webhook runtime mode. Paths built from bot_row["bot_id"] (bots.id, the
    physically unique AUTOINCREMENT PK) — NOT bot_row["name"] — same reasoning
    as every other template's config_from_bot_row (see docs/STAGE2_DESIGN.md
    "Изоляция по bots.id")."""
    bot_id = bot_row["bot_id"]
    config = ExpenseTrackerConfig(
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
    """Injects this bot's ExpenseTrackerConfig into data["config"]."""

    def __init__(self, config: ExpenseTrackerConfig) -> None:
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

def _is_admin(user_id: int, config: ExpenseTrackerConfig) -> bool:
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


# ── amount / date parsing ────────────────────────────────────────────────────

def _parse_amount(text: str) -> float | None:
    """Decimal amounts allowed (unlike debtors.py's whole-unit-only rule) —
    real expenses routinely have kopecks/cents. Same NaN/inf guard as
    vehicle_service.py's _parse_price: float("nan") doesn't raise ValueError
    and NaN comparisons are always False, so without this check "nan" would
    pass the bounds check below and silently poison every SUM(amount) total."""
    try:
        amount = float(text.strip().replace(",", "."))
    except ValueError:
        return None
    if not math.isfinite(amount) or amount <= 0 or amount > AMOUNT_MAX:
        return None
    return amount


def _parse_expense_date(text: str) -> str | None:
    """ДД.ММ.ГГГГ -> ISO 'YYYY-MM-DD'. Rejects future dates (an expense can't
    happen in the future) and anything more than MAX_PAST_DAYS back (typo
    guard, e.g. a stray "01.01.2001") — same bounded-sanity-check spirit as
    every other template's input parsers."""
    text = text.strip()
    parts = text.split(".")
    if len(parts) != 3:
        return None
    day_s, month_s, year_s = parts
    if not (day_s.isdigit() and month_s.isdigit() and year_s.isdigit() and len(year_s) == 4):
        return None
    try:
        parsed = date(int(year_s), int(month_s), int(day_s))
    except ValueError:
        return None
    today = date.today()
    if parsed > today or (today - parsed).days > MAX_PAST_DAYS:
        return None
    return parsed.isoformat()


def _fmt_date(iso_date: str) -> str:
    y, m, d = iso_date.split("-")
    return f"{d}.{m}.{y}"


# ── db ────────────────────────────────────────────────────────────────────────

async def init_db(db_path: str):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS categories (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                is_active   INTEGER NOT NULL DEFAULT 1,
                created_at  TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS expenses (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                category_id   INTEGER NOT NULL REFERENCES categories(id),
                amount        REAL NOT NULL CHECK(amount > 0),
                expense_date  TEXT NOT NULL,
                comment       TEXT,
                created_by    INTEGER NOT NULL,
                created_at    TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_expenses_date ON expenses(expense_date)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_expenses_category ON expenses(category_id)")
        # Seed defaults only if the table is empty — never re-inserts after an
        # admin has deactivated/curated their own category list.
        existing = await (await db.execute("SELECT COUNT(*) FROM categories")).fetchone()
        if existing[0] == 0:
            await db.executemany(
                "INSERT INTO categories (name) VALUES (?)", [(c,) for c in DEFAULT_CATEGORIES]
            )
        await db.commit()


miniapp_config = {
    "resources": [
        {
            "name": "categories",
            "table": "categories",
            "order_by": "name",
            "creatable": True,
            "scope": {"type": "global"},
            "title": "Категории",
            "titleField": "name",
            "fields": [
                {"name": "name", "required": True, "label": "Название", "kind": "text", "list": True, "detail": True, "create": True},
                {"name": "is_active", "label": "Активна", "kind": "status", "list": True, "detail": True, "create": False},
            ],
        },
        {
            "name": "expenses",
            "table": "expenses",
            "order_by": "expense_date DESC",
            "creatable": True,
            "scope": {"type": "global"},
            "title": "Расходы",
            "titleField": "comment",
            "fields": [
                {"name": "category_id", "required": True, "label": "Категория", "kind": "number", "list": False, "detail": True, "create": True, "ref": {"resource": "categories", "labelField": "name"}},
                {"name": "amount", "required": True, "label": "Сумма", "kind": "number", "list": True, "detail": True, "create": True},
                {"name": "expense_date", "required": True, "label": "Дата", "kind": "date", "list": True, "detail": True, "create": True},
                {"name": "comment", "label": "Комментарий", "kind": "text", "list": False, "detail": True, "create": True},
                {"name": "created_by", "label": "Кем добавлено", "kind": "number", "list": False, "detail": True, "create": False},
            ],
        },
    ],
}


async def _active_categories(db_path: str) -> list[tuple[int, str]]:
    async with aiosqlite.connect(db_path) as db:
        rows = await (await db.execute(
            "SELECT id, name FROM categories WHERE is_active=1 ORDER BY name"
        )).fetchall()
    return [(r[0], r[1]) for r in rows]


# ── FSM staleness guard ─────────────────────────────────────────────────────────
# Same mechanic as templates/orders_tracker.py's FLOW_TIMEOUT_SECONDS/_flow_expired.
FLOW_TIMEOUT_SECONDS = 300


def _flow_expired(data: dict) -> bool:
    started_at = data.get("started_at")
    return started_at is None or (time.time() - started_at) > FLOW_TIMEOUT_SECONDS


# ── FSM states ───────────────────────────────────────────────────────────────

class ExpenseFlow(StatesGroup):
    amount = State()
    category = State()      # callback-only: pick from active categories, no typed alternative
    date = State()           # callback-only: "Сегодня" / "Вчера" / "Другая дата"
    date_custom = State()    # text ДД.ММ.ГГГГ, only reached via "Другая дата"
    comment = State()        # text OR "Без комментария" callback

class CategoryFlow(StatesGroup):
    add_name = State()

class SummaryFlow(StatesGroup):
    period_start = State()
    period_end = State()

class AdminMgmtFlow(StatesGroup):
    add_admin = State()
    remove_admin_pick = State()


def _valid_admin_id(text: str) -> bool:
    """Same guard as templates/orders_tracker.py's _valid_admin_id()."""
    if not (bool(text) and text.isascii() and text.isdigit() and len(text) <= 15):
        return False
    return int(text) > 0 and str(int(text)) == text


# ── keyboards ─────────────────────────────────────────────────────────────────

def kb_main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить расход", callback_data="exp_new")],
        [InlineKeyboardButton(text="📊 Сводка", callback_data="sum_menu")],
        [InlineKeyboardButton(text="⚙️ Категории", callback_data="cat_menu")],
        [InlineKeyboardButton(text="👥 Админы", callback_data="adm_menu")],
    ])

def kb_back(callback_data: str = "main_menu") -> InlineKeyboardButton:
    return InlineKeyboardButton(text="◀️ Назад", callback_data=callback_data)

def kb_flow_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="flow_cancel")],
    ])

MAX_LIST_BUTTONS = 25

# ── add-expense flow ──
def kb_category_pick(categories: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=_esc(name, 40), callback_data=f"exp_cat:{cid}")]
        for cid, name in categories[:MAX_LIST_BUTTONS]
    ]
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="flow_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_date_pick() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Сегодня", callback_data="exp_date_today")],
        [InlineKeyboardButton(text="📅 Вчера", callback_data="exp_date_yesterday")],
        [InlineKeyboardButton(text="✏️ Другая дата", callback_data="exp_date_custom")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="flow_cancel")],
    ])

def kb_comment_skip() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭ Без комментария", callback_data="exp_comment_skip")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="flow_cancel")],
    ])

def kb_expense_done() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Ещё расход", callback_data="exp_new")],
        [InlineKeyboardButton(text="🏠 Меню", callback_data="main_menu")],
    ])

# ── categories menu ──
def kb_categories_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить категорию", callback_data="cat_add")],
        [InlineKeyboardButton(text="📋 Список категорий", callback_data="cat_list")],
        [kb_back()],
    ])

def kb_category_list(categories: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=_esc(name, 40), callback_data=f"cat_view:{cid}")]
        for cid, name in categories[:MAX_LIST_BUTTONS]
    ]
    rows.append([kb_back("cat_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_category_detail(category_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Деактивировать", callback_data=f"cat_deactivate:{category_id}")],
        [kb_back("cat_list")],
    ])

# ── summary menu ──
def kb_summary_periods() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📆 Текущий месяц", callback_data="sum_period:this_month")],
        [InlineKeyboardButton(text="📆 Прошлый месяц", callback_data="sum_period:last_month")],
        [InlineKeyboardButton(text="🗓 Свой период", callback_data="sum_period:custom")],
        [kb_back()],
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

async def _expense_confirmation_text(db_path: str, expense_id: int) -> str | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            "SELECT e.id, e.amount, e.expense_date, e.comment, c.name AS category_name "
            "FROM expenses e JOIN categories c ON e.category_id = c.id WHERE e.id=?",
            (expense_id,),
        )).fetchone()
    if not row:
        return None
    lines = [
        "✅ <b>Расход добавлен</b>\n",
        f"💰 {row['amount']:.2f} {CURRENCY_SYMBOL}",
        f"🏷 {_esc(row['category_name'])}",
        f"📅 {_fmt_date(row['expense_date'])}",
    ]
    if row["comment"]:
        lines.append(f"📝 {_esc(row['comment'])}")
    return "\n".join(lines)


async def _summary_text(db_path: str, start_iso: str, end_iso: str, period_label: str) -> str:
    async with aiosqlite.connect(db_path) as db:
        rows = await (await db.execute(
            "SELECT c.name, SUM(e.amount) AS total, COUNT(*) AS cnt "
            "FROM expenses e JOIN categories c ON e.category_id = c.id "
            "WHERE e.expense_date BETWEEN ? AND ? "
            "GROUP BY e.category_id ORDER BY total DESC",
            (start_iso, end_iso),
        )).fetchall()

    lines = [f"📊 <b>Сводка · {period_label}</b>\n"]
    if not rows:
        lines.append("Расходов за этот период нет.")
        return _join_bounded(lines)

    grand_total = sum(r[1] for r in rows)
    medals = ["🥇", "🥈", "🥉"]
    for i, (name, total, cnt) in enumerate(rows):
        prefix = medals[i] if i < len(medals) else "•"
        pct = (total / grand_total * 100) if grand_total else 0
        lines.append(f"{prefix} {_esc(name)}: {total:.2f} {CURRENCY_SYMBOL} ({pct:.0f}%, {cnt} шт.)")
    lines.append(f"\n💰 <b>Итого: {grand_total:.2f} {CURRENCY_SYMBOL}</b>")
    return _join_bounded(lines)


def _month_bounds(year: int, month: int) -> tuple[str, str]:
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) - timedelta(days=1) if month == 12 else date(year, month + 1, 1) - timedelta(days=1)
    return start.isoformat(), end.isoformat()


# ── /start ────────────────────────────────────────────────────────────────────

@router.message(Command("start"), F.chat.type == "private")
async def cmd_start(message: Message, state: FSMContext, config: ExpenseTrackerConfig):
    # Same reasoning as orders_tracker.py's cmd_start: /start must reset any
    # dangling mid-flow FSM state before showing a menu.
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
        _save_admins(config.admins_file, {str(sender_id)})
        admins = {str(sender_id)}
        if config.bot_id is not None:
            try:
                await add_bot_admin(config.bot_id, str(sender_id))
            except Exception as e:
                logger.warning(f"cmd_start: add_bot_admin sync failed for bot {config.bot_id}: {e}")

    if str(message.from_user.id) not in admins:
        await message.answer(NO_ACCESS_TEXT)
        return

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
            "Чтобы вести расходы командой, добавьте остальных через «👥 Админы».",
            parse_mode="HTML",
        )


@router.callback_query(F.data == "main_menu")
async def cb_main_menu(cb: CallbackQuery, state: FSMContext, config: ExpenseTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    await cb.message.edit_text(WELCOME_TEXT, parse_mode="HTML", reply_markup=kb_main_menu())


@router.callback_query(F.data == "flow_cancel")
async def cb_flow_cancel(cb: CallbackQuery, state: FSMContext, config: ExpenseTrackerConfig):
    await cb.answer()
    await state.clear()
    if not _is_admin(cb.from_user.id, config):
        return
    await cb.message.edit_text("Отменено.", reply_markup=kb_main_menu())


# ── ADD EXPENSE flow: amount → category → date → comment ─────────────────────

@router.callback_query(F.data == "exp_new")
async def cb_exp_new(cb: CallbackQuery, state: FSMContext, config: ExpenseTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    await state.set_state(ExpenseFlow.amount)
    await state.update_data(started_at=time.time())
    await cb.message.edit_text(f"💰 Введите сумму расхода ({CURRENCY_SYMBOL}):", reply_markup=kb_flow_cancel())


@router.message(ExpenseFlow.amount, F.text, ~F.text.startswith("/"))
async def expense_amount(msg: Message, state: FSMContext, config: ExpenseTrackerConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    amount = _parse_amount(msg.text)
    if amount is None:
        await msg.answer("Введите положительное число, например: 543.50", reply_markup=kb_flow_cancel())
        return
    categories = await _active_categories(config.db_path)
    if not categories:
        await state.clear()
        await msg.answer(
            "⚠️ Нет активных категорий. Добавьте категорию через «⚙️ Категории».",
            reply_markup=kb_main_menu(),
        )
        return
    await state.update_data(pending_amount=amount)
    await state.set_state(ExpenseFlow.category)
    await msg.answer("🏷 Выберите категорию:", reply_markup=kb_category_pick(categories))


@router.callback_query(ExpenseFlow.category, F.data.startswith("exp_cat:"))
async def cb_expense_category(cb: CallbackQuery, state: FSMContext, config: ExpenseTrackerConfig):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    try:
        category_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute(
            "SELECT id FROM categories WHERE id=? AND is_active=1", (category_id,)
        )).fetchone()
    if not row:
        # Stale button — category was deactivated by another admin mid-flow.
        categories = await _active_categories(config.db_path)
        await cb.message.edit_text(
            "⚠️ Категория больше не активна. Выберите другую:", reply_markup=kb_category_pick(categories)
        )
        return
    await state.update_data(pending_category_id=category_id)
    await state.set_state(ExpenseFlow.date)
    await cb.message.edit_text("📅 Дата расхода:", reply_markup=kb_date_pick())


async def _set_expense_date_and_advance(cb: CallbackQuery, state: FSMContext, iso_date: str) -> None:
    await state.update_data(pending_date=iso_date)
    await state.set_state(ExpenseFlow.comment)
    await cb.message.edit_text(
        "📝 Комментарий (необязательно) — отправьте текст или нажмите «Пропустить»:",
        reply_markup=kb_comment_skip(),
    )


@router.callback_query(ExpenseFlow.date, F.data == "exp_date_today")
async def cb_expense_date_today(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    await _set_expense_date_and_advance(cb, state, date.today().isoformat())


@router.callback_query(ExpenseFlow.date, F.data == "exp_date_yesterday")
async def cb_expense_date_yesterday(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    await _set_expense_date_and_advance(cb, state, (date.today() - timedelta(days=1)).isoformat())


@router.callback_query(ExpenseFlow.date, F.data == "exp_date_custom")
async def cb_expense_date_custom(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    await state.set_state(ExpenseFlow.date_custom)
    await cb.message.edit_text("Введите дату в формате ДД.ММ.ГГГГ, например: 05.08.2026", reply_markup=kb_flow_cancel())


@router.message(ExpenseFlow.date_custom, F.text, ~F.text.startswith("/"))
async def expense_date_custom(msg: Message, state: FSMContext):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    iso_date = _parse_expense_date(msg.text)
    if iso_date is None:
        await msg.answer(
            "❌ Неверная дата. Введите в формате ДД.ММ.ГГГГ, не в будущем, например: 05.08.2026",
            reply_markup=kb_flow_cancel(),
        )
        return
    await state.update_data(pending_date=iso_date)
    await state.set_state(ExpenseFlow.comment)
    await msg.answer(
        "📝 Комментарий (необязательно) — отправьте текст или нажмите «Пропустить»:",
        reply_markup=kb_comment_skip(),
    )


async def _finalize_expense(message_answer, state: FSMContext, config: ExpenseTrackerConfig, user_id: int,
                             comment: str | None) -> None:
    data = await state.get_data()
    amount = data.get("pending_amount")
    category_id = data.get("pending_category_id")
    expense_date = data.get("pending_date")
    if amount is None or category_id is None or expense_date is None:
        await state.clear()
        await message_answer("Сессия устарела, начните заново.", reply_markup=kb_main_menu())
        return
    await state.clear()
    async with aiosqlite.connect(config.db_path) as db:
        cur = await db.execute(
            "INSERT INTO expenses (category_id, amount, expense_date, comment, created_by) VALUES (?,?,?,?,?)",
            (category_id, amount, expense_date, comment, user_id),
        )
        expense_id = cur.lastrowid
        await db.commit()
    text = await _expense_confirmation_text(config.db_path, expense_id)
    await message_answer(text, parse_mode="HTML", reply_markup=kb_expense_done())


@router.message(ExpenseFlow.comment, F.text, ~F.text.startswith("/"))
async def expense_comment(msg: Message, state: FSMContext, config: ExpenseTrackerConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    comment = msg.text.strip()
    if len(comment) > COMMENT_MAX_LEN:
        await msg.answer(
            f"⚠️ Комментарий должен быть короче {COMMENT_MAX_LEN} символов, или нажмите «Пропустить»:",
            reply_markup=kb_comment_skip(),
        )
        return
    await _finalize_expense(msg.answer, state, config, msg.from_user.id, comment or None)


@router.callback_query(ExpenseFlow.comment, F.data == "exp_comment_skip")
async def cb_expense_comment_skip(cb: CallbackQuery, state: FSMContext, config: ExpenseTrackerConfig):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    await _finalize_expense(cb.message.answer, state, config, cb.from_user.id, None)


# ── CATEGORIES menu ──────────────────────────────────────────────────────────

@router.callback_query(F.data == "cat_menu")
async def cb_cat_menu(cb: CallbackQuery, state: FSMContext, config: ExpenseTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    await cb.message.edit_text("⚙️ <b>Категории</b>", parse_mode="HTML", reply_markup=kb_categories_menu())


@router.callback_query(F.data == "cat_list")
async def cb_cat_list(cb: CallbackQuery, config: ExpenseTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    categories = await _active_categories(config.db_path)
    if not categories:
        await cb.message.edit_text("Активных категорий нет.", reply_markup=kb_categories_menu())
        return
    await cb.message.edit_text("📋 Категории:", reply_markup=kb_category_list(categories))


@router.callback_query(F.data.startswith("cat_view:"))
async def cb_cat_view(cb: CallbackQuery, config: ExpenseTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        category_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute(
            "SELECT name FROM categories WHERE id=? AND is_active=1", (category_id,)
        )).fetchone()
        if not row:
            categories = await _active_categories(config.db_path)
            await cb.message.edit_text("Категория не найдена.", reply_markup=kb_category_list(categories))
            return
        count = (await (await db.execute(
            "SELECT COUNT(*) FROM expenses WHERE category_id=?", (category_id,)
        )).fetchone())[0]
    text = f"🏷 <b>{_esc(row[0])}</b>\n\nРасходов в этой категории: {count}"
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_category_detail(category_id))


@router.callback_query(F.data == "cat_add")
async def cb_cat_add(cb: CallbackQuery, state: FSMContext, config: ExpenseTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.set_state(CategoryFlow.add_name)
    await state.update_data(started_at=time.time())
    await cb.message.edit_text("Введите название новой категории:", reply_markup=kb_flow_cancel())


@router.message(CategoryFlow.add_name, F.text, ~F.text.startswith("/"))
async def category_add_name(msg: Message, state: FSMContext, config: ExpenseTrackerConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    name = msg.text.strip()
    if not name:
        await msg.answer("Название не может быть пустым. Введите название категории:", reply_markup=kb_flow_cancel())
        return
    if len(name) > CATEGORY_NAME_MAX_LEN:
        await msg.answer(
            f"⚠️ Название должно быть короче {CATEGORY_NAME_MAX_LEN} символов. Введите название категории:",
            reply_markup=kb_flow_cancel(),
        )
        return
    async with aiosqlite.connect(config.db_path) as db:
        # Uniqueness checked in application code among ACTIVE categories only
        # (not a DB UNIQUE constraint) — deliberately, so a deactivated
        # category's name can be reused for a fresh one without a leftover
        # constraint collision. See design notes: no reactivation feature,
        # re-adding the same name is the intended path back.
        existing = await (await db.execute(
            "SELECT id FROM categories WHERE is_active=1 AND lower(name)=lower(?)", (name,)
        )).fetchone()
        if existing:
            await state.clear()
            await msg.answer(f"⚠️ Категория «{_esc(name)}» уже есть.", parse_mode="HTML",
                              reply_markup=kb_categories_menu())
            return
        await db.execute("INSERT INTO categories (name) VALUES (?)", (name,))
        await db.commit()
    await state.clear()
    await msg.answer(f"✅ Категория «{_esc(name)}» добавлена.", parse_mode="HTML", reply_markup=kb_categories_menu())


@router.callback_query(F.data.startswith("cat_deactivate:"))
async def cb_cat_deactivate(cb: CallbackQuery, config: ExpenseTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        category_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    async with aiosqlite.connect(config.db_path) as db:
        # Compare-and-swap on is_active=1 makes a double-tap a no-op on the
        # second write — same principle as vehicle_service.py's cb_req_status.
        cur = await db.execute(
            "UPDATE categories SET is_active=0 WHERE id=? AND is_active=1", (category_id,)
        )
        await db.commit()
    categories = await _active_categories(config.db_path)
    if cur.rowcount == 0:
        await cb.message.edit_text("Категория уже не активна.", reply_markup=kb_category_list(categories))
        return
    await cb.message.edit_text("🗑 Категория деактивирована. История расходов по ней сохранена.",
                                reply_markup=kb_category_list(categories))


# ── SUMMARY menu ──────────────────────────────────────────────────────────────

@router.callback_query(F.data == "sum_menu")
async def cb_sum_menu(cb: CallbackQuery, state: FSMContext, config: ExpenseTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    await cb.message.edit_text("📊 <b>Сводка</b>\n\nВыберите период:", parse_mode="HTML",
                                reply_markup=kb_summary_periods())


@router.callback_query(F.data == "sum_period:this_month")
async def cb_sum_this_month(cb: CallbackQuery, config: ExpenseTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    today = date.today()
    start, end = _month_bounds(today.year, today.month)
    text = await _summary_text(config.db_path, start, end, "текущий месяц")
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_summary_periods())


@router.callback_query(F.data == "sum_period:last_month")
async def cb_sum_last_month(cb: CallbackQuery, config: ExpenseTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    today = date.today()
    first_of_this_month = date(today.year, today.month, 1)
    last_month_day = first_of_this_month - timedelta(days=1)
    start, end = _month_bounds(last_month_day.year, last_month_day.month)
    text = await _summary_text(config.db_path, start, end, "прошлый месяц")
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_summary_periods())


@router.callback_query(F.data == "sum_period:custom")
async def cb_sum_custom(cb: CallbackQuery, state: FSMContext, config: ExpenseTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.set_state(SummaryFlow.period_start)
    await state.update_data(started_at=time.time())
    await cb.message.edit_text("Введите начало периода в формате ДД.ММ.ГГГГ:", reply_markup=kb_flow_cancel())


@router.message(SummaryFlow.period_start, F.text, ~F.text.startswith("/"))
async def summary_period_start(msg: Message, state: FSMContext):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    iso_date = _parse_expense_date(msg.text)
    if iso_date is None:
        await msg.answer("❌ Неверная дата. Введите в формате ДД.ММ.ГГГГ, не в будущем:", reply_markup=kb_flow_cancel())
        return
    await state.update_data(period_start=iso_date)
    await state.set_state(SummaryFlow.period_end)
    await msg.answer("Введите конец периода в формате ДД.ММ.ГГГГ:", reply_markup=kb_flow_cancel())


@router.message(SummaryFlow.period_end, F.text, ~F.text.startswith("/"))
async def summary_period_end(msg: Message, state: FSMContext, config: ExpenseTrackerConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    iso_date = _parse_expense_date(msg.text)
    if iso_date is None:
        await msg.answer("❌ Неверная дата. Введите в формате ДД.ММ.ГГГГ, не в будущем:", reply_markup=kb_flow_cancel())
        return
    start = data.get("period_start")
    if start is None:
        await state.clear()
        await msg.answer("Сессия устарела, начните заново.", reply_markup=kb_main_menu())
        return
    if iso_date < start:
        await msg.answer(
            "⚠️ Конец периода раньше начала. Введите конец периода в формате ДД.ММ.ГГГГ:",
            reply_markup=kb_flow_cancel(),
        )
        return
    await state.clear()
    label = f"{_fmt_date(start)} – {_fmt_date(iso_date)}"
    text = await _summary_text(config.db_path, start, iso_date, label)
    await msg.answer(text, parse_mode="HTML", reply_markup=kb_summary_periods())


# ── ADMINS menu ────────────────────────────────────────────────────────────────

async def _admins_list_text(config: ExpenseTrackerConfig) -> str:
    ids = sorted(_load_admins(config.admins_file))
    if not ids:
        return "👥 Пусто"
    return _join_bounded(["👥 <b>Администраторы бота:</b>\n"] + [f"• <code>{_esc(i)}</code>" for i in ids])


@router.callback_query(F.data == "adm_menu")
async def cb_adm_menu(cb: CallbackQuery, state: FSMContext, config: ExpenseTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    text = await _admins_list_text(config)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_admins_menu())


@router.callback_query(F.data == "adm_add")
async def cb_adm_add(cb: CallbackQuery, state: FSMContext, config: ExpenseTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.set_state(AdminMgmtFlow.add_admin)
    await state.update_data(started_at=time.time())
    await cb.message.edit_text("Введите Telegram ID нового администратора:", reply_markup=kb_flow_cancel())


@router.message(AdminMgmtFlow.add_admin, F.text, ~F.text.startswith("/"))
async def admin_add_id(msg: Message, state: FSMContext, config: ExpenseTrackerConfig):
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
async def cb_adm_remove(cb: CallbackQuery, state: FSMContext, config: ExpenseTrackerConfig):
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
async def cb_adm_remove_pick(cb: CallbackQuery, state: FSMContext, config: ExpenseTrackerConfig):
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
