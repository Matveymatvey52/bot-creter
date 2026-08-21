# TEMPLATE: sales_analytics
# USE FOR: бизнес-аналитика, учёт продаж, воронка заявок, контроль расходов, KPI продаж
# CUSTOMIZE: sections marked with # CUSTOMIZE
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup,
    Message,
)

from db.database import add_bot_admin

# ── CUSTOMIZE ────────────────────────────────────────────────────────────────
BOT_DESCRIPTION = "Аналитика продаж, заявок и расходов. Сводки за день, неделю и месяц."
WELCOME_TEXT = (
    "📈 <b>Бизнес-Аналитика</b>\n\n"
    "Я помогу фиксировать продажи, заявки и расходы, чтобы вы всегда видели актуальную картину бизнеса.\n\n"
    "Выберите действие:"
)
# ── END CUSTOMIZE ─────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
router = Router()

@dataclass
class SalesAnalyticsConfig:
    bot_name: str
    db_path: str
    admins_file: Path
    welcome_image: Path
    display_name: str | None = None
    bot_id: int | None = None
    owner_telegram_id: int | None = None

def config_from_env() -> SalesAnalyticsConfig:
    name = Path(__file__).stem
    data_dir = Path(os.getenv("DATA_DIR", "./data"))
    data_dir.mkdir(exist_ok=True)
    return SalesAnalyticsConfig(
        bot_name=name,
        db_path=str(data_dir / f"{name}_data.db"),
        admins_file=data_dir / f"admins_{name}.json",
        welcome_image=data_dir / "bot_images" / f"{name}.jpg",
    )

def config_from_bot_row(bot_row: dict, data_dir: Path) -> SalesAnalyticsConfig:
    bot_id = bot_row["bot_id"]
    return SalesAnalyticsConfig(
        bot_name=bot_row["name"],
        db_path=str(data_dir / f"bot_{bot_id}_data.db"),
        admins_file=data_dir / f"admins_{bot_id}.json",
        welcome_image=data_dir / "bot_images" / f"bot_{bot_id}.jpg",
        display_name=bot_row.get("display_name"),
        bot_id=bot_id,
        owner_telegram_id=bot_row.get("owner_telegram_id"),
    )

class ConfigMiddleware(BaseMiddleware):
    def __init__(self, config: SalesAnalyticsConfig) -> None:
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

def _is_admin(user_id: int, config: SalesAnalyticsConfig) -> bool:
    # The DB-known owner (bots.owner_telegram_id) is always an admin, even if
    # the local admins_file is empty/stale/hijacked — same defense-in-depth
    # rationale as templates/shop_catalog.py's _is_bot_admin.
    if config.owner_telegram_id is not None and str(user_id) == str(config.owner_telegram_id):
        return True
    return str(user_id) in _load_admins(config.admins_file)

# ── db ────────────────────────────────────────────────────────────────────────

async def init_db(db_path: str):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS analytics_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_type TEXT NOT NULL,   -- 'sale' | 'lead' | 'expense'
                amount INTEGER,
                description TEXT,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.commit()

# ── keyboards ─────────────────────────────────────────────────────────────────

def kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить запись", callback_data="add_entry")],
        [InlineKeyboardButton(text="📊 Отчёт", callback_data="report_menu")],
        [InlineKeyboardButton(text="📈 Динамика", callback_data="dynamics_menu")],
    ])

def kb_entry_types() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Продажа", callback_data="etype:sale")],
        [InlineKeyboardButton(text="📋 Заявка", callback_data="etype:lead")],
        [InlineKeyboardButton(text="💸 Расход", callback_data="etype:expense")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")],
    ])

def kb_skip_description() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭ Пропустить", callback_data="skip_desc")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")],
    ])

def kb_periods(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Сегодня", callback_data=f"{prefix}:today"),
         InlineKeyboardButton(text="📅 Неделя", callback_data=f"{prefix}:week")],
        [InlineKeyboardButton(text="📅 Этот месяц", callback_data=f"{prefix}:month"),
         InlineKeyboardButton(text="📅 Всё время", callback_data=f"{prefix}:all")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="main_menu")],
    ])

# ── FSM ───────────────────────────────────────────────────────────────────────

class AddEntry(StatesGroup):
    etype = State()
    amount = State()
    description = State()

# ── handlers ──────────────────────────────────────────────────────────────────

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext, config: SalesAnalyticsConfig):
    await state.clear()
    admins = _load_admins(config.admins_file)
    sender_id = message.from_user.id
    # Bug fix: _save_admins() takes a set of ids and does
    # json.dumps({"ids": list(ids)}) — the old call here passed a DICT
    # ({"ids": [str(sender_id)]}) instead of a set. list() on a dict yields
    # its KEYS, so the file was written as {"ids": ["ids"]} — the literal
    # string "ids", never the real Telegram id. No one could ever pass the
    # _is_admin check afterward, including the owner (fail-closed but
    # unusable). Also applies the owner_telegram_id pattern used by every
    # other template: only bots.owner_telegram_id (when known) may claim
    # the empty-admins bootstrap slot, not whoever sends /start first.
    is_owner = config.owner_telegram_id is not None and sender_id == config.owner_telegram_id
    first_time_admin = not admins and (is_owner or config.owner_telegram_id is None)
    if first_time_admin:
        _save_admins(config.admins_file, {str(sender_id)})
        if config.bot_id is not None:
            try:
                await add_bot_admin(config.bot_id, str(sender_id))
            except Exception as e:
                logger.warning(f"cmd_start: add_bot_admin sync failed for bot {config.bot_id}: {e}")

    if not _is_admin(sender_id, config):
        return

    if config.welcome_image.exists():
        await message.answer_photo(FSInputFile(str(config.welcome_image)),
                                   caption=WELCOME_TEXT, parse_mode="HTML", reply_markup=kb_main())
    else:
        await message.answer(WELCOME_TEXT, parse_mode="HTML", reply_markup=kb_main())

@router.callback_query(F.data == "main_menu")
async def cb_main_menu(cb: CallbackQuery, state: FSMContext, config: SalesAnalyticsConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config): return
    await state.clear()
    await cb.message.edit_text(WELCOME_TEXT, parse_mode="HTML", reply_markup=kb_main())

@router.callback_query(F.data == "cancel")
async def cb_cancel(cb: CallbackQuery, state: FSMContext, config: SalesAnalyticsConfig):
    await cb.answer("Отменено")
    await cb_main_menu(cb, state, config)

# ── adding entries ──

@router.callback_query(F.data == "add_entry")
async def cb_add_entry(cb: CallbackQuery, state: FSMContext, config: SalesAnalyticsConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config): return
    await state.set_state(AddEntry.etype)
    await cb.message.edit_text("Выберите тип записи:", reply_markup=kb_entry_types())

@router.callback_query(AddEntry.etype, F.data.startswith("etype:"))
async def cb_etype_selected(cb: CallbackQuery, state: FSMContext, config: SalesAnalyticsConfig):
    await cb.answer()
    etype = cb.data.split(":")[1]
    await state.update_data(etype=etype)
    await state.set_state(AddEntry.amount)
    
    prompts = {"sale": "сумму продажи", "lead": "потенциальную сумму заявки (или 0)", "expense": "сумму расхода"}
    await cb.message.edit_text(f"Введите {prompts.get(etype, 'сумму')} (только цифры):")

@router.message(AddEntry.amount)
async def msg_amount(message: Message, state: FSMContext, config: SalesAnalyticsConfig):
    if not _is_admin(message.from_user.id, config): return
    amount_str = re.sub(r"[^\d]", "", message.text)
    if not amount_str:
        await message.answer("Пожалуйста, введите число (например, 5000):")
        return
    
    await state.update_data(amount=int(amount_str))
    await state.set_state(AddEntry.description)
    await message.answer("Добавьте описание (опционально):", reply_markup=kb_skip_description())

@router.callback_query(AddEntry.description, F.data == "skip_desc")
async def cb_skip_desc(cb: CallbackQuery, state: FSMContext, config: SalesAnalyticsConfig):
    await cb.answer()
    await _save_entry(cb.message, state, config, None)

@router.message(AddEntry.description)
async def msg_description(message: Message, state: FSMContext, config: SalesAnalyticsConfig):
    if not _is_admin(message.from_user.id, config): return
    await _save_entry(message, state, config, message.text)

async def _save_entry(message: Message, state: FSMContext, config: SalesAnalyticsConfig, desc: str | None):
    data = await state.get_data()
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute(
            "INSERT INTO analytics_entries (entry_type, amount, description) VALUES (?, ?, ?)",
            (data["etype"], data["amount"], desc)
        )
        await db.commit()
    
    etype_names = {"sale": "💰 Продажа", "lead": "📋 Заявка", "expense": "💸 Расход"}
    await state.clear()
    await message.answer(
        f"✅ Запись добавлена!\n\n"
        f"<b>Тип:</b> {etype_names.get(data['etype'])}\n"
        f"<b>Сумма:</b> {data['amount']:,} ₽\n"
        f"<b>Описание:</b> {desc or '—'}",
        parse_mode="HTML", reply_markup=kb_main()
    )

# ── reports ──

def _get_period_sql(period: str) -> str:
    if period == "today":
        return "date(created_at) = date('now', 'localtime')"
    if period == "week":
        return "date(created_at) >= date('now', 'localtime', '-7 days')"
    if period == "month":
        return "date(created_at) >= date('now', 'localtime', '-30 days')"
    return "1=1"

@router.callback_query(F.data == "report_menu")
async def cb_report_menu(cb: CallbackQuery, config: SalesAnalyticsConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config): return
    await cb.message.edit_text("📊 <b>Отчёт</b>\n\nВыберите период:", parse_mode="HTML", reply_markup=kb_periods("rep"))

@router.callback_query(F.data.startswith("rep:"))
async def cb_report_period(cb: CallbackQuery, config: SalesAnalyticsConfig):
    await cb.answer()
    period = cb.data.split(":")[1]
    where = _get_period_sql(period)
    
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(f"""
            SELECT entry_type, SUM(amount) as total_amount, COUNT(*) as cnt 
            FROM analytics_entries WHERE {where} GROUP BY entry_type
        """)).fetchall()
    
    stats = {r["entry_type"]: (r["total_amount"], r["cnt"]) for r in rows}
    
    sale_val, sale_cnt = stats.get("sale", (0, 0))
    lead_val, lead_cnt = stats.get("lead", (0, 0))
    exp_val, exp_cnt = stats.get("expense", (0, 0))
    
    avg_sale = sale_val / sale_cnt if sale_cnt > 0 else 0
    
    period_names = {"today": "сегодня", "week": "неделю", "month": "этот месяц", "all": "всё время"}
    text = (
        f"📊 <b>Сводка за {period_names.get(period)}</b>\n\n"
        f"💰 <b>Продажи:</b> {sale_val:,} ₽ ({sale_cnt} шт.)\n"
        f"📋 <b>Заявки:</b> {lead_val:,} ₽ ({lead_cnt} шт.)\n"
        f"💸 <b>Расходы:</b> {exp_val:,} ₽ ({exp_cnt} шт.)\n\n"
        f"💎 <b>Средний чек:</b> {avg_sale:,.0f} ₽\n"
        f"⚖️ <b>Прибыль (П-Р):</b> {sale_val - exp_val:,} ₽"
    )
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_periods("rep"))

# ── dynamics ──

@router.callback_query(F.data == "dynamics_menu")
async def cb_dynamics_menu(cb: CallbackQuery, config: SalesAnalyticsConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config): return
    await cb.message.edit_text("📈 <b>Динамика</b>\n\nВыберите период для детализации по дням:", 
                               parse_mode="HTML", reply_markup=kb_periods("dyn"))

@router.callback_query(F.data.startswith("dyn:"))
async def cb_dynamics_period(cb: CallbackQuery, config: SalesAnalyticsConfig):
    await cb.answer()
    period = cb.data.split(":")[1]
    where = _get_period_sql(period)
    
    async with aiosqlite.connect(config.db_path) as db:
        rows = await (await db.execute(f"""
            SELECT date(created_at) as day, entry_type, SUM(amount)
            FROM analytics_entries WHERE {where}
            GROUP BY day, entry_type ORDER BY day DESC, entry_type
        """)).fetchall()
    
    if not rows:
        await cb.message.edit_text("Записей за этот период нет.", reply_markup=kb_periods("dyn"))
        return

    days_data = {}
    for day, etype, amount in rows:
        if day not in days_data: days_data[day] = {"sale": 0, "lead": 0, "expense": 0}
        days_data[day][etype] = amount
    
    lines = [f"📈 <b>Динамика:</b>\n"]
    for day, vals in sorted(days_data.items(), reverse=True)[:15]: # Лимит 15 дней для компактности
        lines.append(f"📅 <b>{day}</b>")
        lines.append(f"  💰 {vals['sale']:,} | 📋 {vals['lead']:,} | 💸 {vals['expense']:,}")
    
    if len(days_data) > 15:
        lines.append("\n<i>...показаны последние 15 дней</i>")

    await cb.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=kb_periods("dyn"))

# ── MAIN ──────────────────────────────────────────────────────────────────────

async def main():
    config = config_from_env()
    bot = Bot(token=os.getenv("BOT_TOKEN", "fake_token"))
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(ConfigMiddleware(config))
    dp.include_router(router)
    await bot.set_my_description(BOT_DESCRIPTION)
    await init_db(config.db_path)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
