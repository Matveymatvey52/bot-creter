# TEMPLATE: campaign_tracker
# USE FOR: учёт рекламных кампаний, сравнение эффективности каналов, стоимость заявки, конверсия, маркетинг
# CUSTOMIZE: sections marked with # CUSTOMIZE
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
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
# runtime state (that's config.db_path/admins_file below).
BOT_DESCRIPTION = "Учёт рекламных кампаний: бюджет, период, ручной ввод метрик, сравнение по стоимости заявки и конверсии."
WELCOME_TEXT = (
    "📊 <b>Учёт рекламных кампаний</b>\n\n"
    "Создавайте кампании, вносите метрики (переходы/заявки/продажи) вручную "
    "и сравнивайте эффективность каналов.\n\n"
    "Выберите действие:"
)
CHANNELS = ["Instagram", "Яндекс.Директ", "Офлайн"]
# ── END CUSTOMIZE ─────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
router = Router()

MAIN_BUTTONS = frozenset({"➕ Новая кампания", "📋 Кампании", "📊 Сравнение"})
_DATE_RE = re.compile(r"^\d{1,2}\.\d{1,2}\.\d{4}$")
# SQLite's INTEGER column is a signed 64-bit int — binding anything outside
# this range raises OverflowError. Callback ids and manually-typed metric
# counts are both bounded against it below so a malformed/absurd input is
# rejected up front instead of crashing the handler mid-query.
_SQLITE_INT_MAX = 2**63 - 1
_MAX_METRIC_VALUE = 10**9
_MAX_BUDGET = 10**12


def _esc(value, max_len: int = 200) -> str:
    """HTML-escapes AND length-bounds any user-supplied text before it goes into
    a parse_mode="HTML" message — same helper/rationale as templates/
    booking_medical.py's _esc(): a free-text campaign name could otherwise
    contain '<'/'&' and break message rendering."""
    text = str(value) if value is not None else ""
    if len(text) > max_len:
        text = text[:max_len] + "…"
    return html.escape(text)


def _parse_date(raw: str) -> str | None:
    """Accepts ДД.ММ.ГГГГ (also ДД/ММ/ГГГГ), returns ISO 'YYYY-MM-DD' for
    storage/sorting, or None if unparsable — same convention as
    templates/tour_operator.py's date input handling."""
    cleaned = raw.strip().replace("/", ".")
    if not _DATE_RE.match(cleaned):
        return None
    try:
        return datetime.strptime(cleaned, "%d.%m.%Y").strftime("%Y-%m-%d")
    except ValueError:
        return None


def _fmt_date(iso: str | None) -> str:
    if not iso:
        return "бессрочно"
    return datetime.strptime(iso, "%Y-%m-%d").strftime("%d.%m.%Y")


# ── config ───────────────────────────────────────────────────────────────────
# Same pattern as every other template — see docs/STAGE2_DESIGN.md.

@dataclass
class CampaignTrackerConfig:
    bot_name: str
    db_path: str
    admins_file: Path
    welcome_image: Path
    display_name: str | None = None
    group_chat_id: str | None = None
    bot_id: int | None = None
    owner_telegram_id: int | None = None


def _paths_for(name: str, data_dir: Path) -> CampaignTrackerConfig:
    return CampaignTrackerConfig(
        bot_name=name,
        db_path=str(data_dir / f"{name}_data.db"),
        admins_file=data_dir / f"admins_{name}.json",
        welcome_image=data_dir / "bot_images" / f"{name}.jpg",
    )


def config_from_env() -> CampaignTrackerConfig:
    """Standalone/subprocess mode."""
    name = Path(__file__).stem
    data_dir = Path(os.getenv("DATA_DIR", "./data"))
    data_dir.mkdir(exist_ok=True)
    return _paths_for(name, data_dir)


def config_from_bot_row(bot_row: dict, data_dir: Path) -> CampaignTrackerConfig:
    """Webhook runtime mode. Paths built from bot_row["bot_id"] (bots.id, the
    physically unique AUTOINCREMENT PK) — NOT bot_row["name"] — same reasoning
    as every other template's config_from_bot_row (see docs/STAGE2_DESIGN.md
    "Изоляция по bots.id")."""
    bot_id = bot_row["bot_id"]
    config = CampaignTrackerConfig(
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
    """Injects this bot's CampaignTrackerConfig into data["config"]."""

    def __init__(self, config: CampaignTrackerConfig) -> None:
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


def _is_admin(user_id: int, config: CampaignTrackerConfig) -> bool:
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
            CREATE TABLE IF NOT EXISTS campaigns (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                channel     TEXT NOT NULL,
                budget      REAL NOT NULL DEFAULT 0,
                start_date  TEXT NOT NULL,
                end_date    TEXT,
                status      TEXT NOT NULL DEFAULT 'active',
                created_by  INTEGER NOT NULL,
                created_at  TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS metric_entries (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
                clicks      INTEGER NOT NULL DEFAULT 0,
                leads       INTEGER NOT NULL DEFAULT 0,
                sales       INTEGER NOT NULL DEFAULT 0,
                entered_by  INTEGER NOT NULL,
                entered_at  TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.commit()


# ── mini-app config (see docs/MINIAPP_DESIGN.md, templates/car_rental.py's
# own miniapp_config for the authoring contract) ────────────────────────────
# metric_entries is not creatable here: rows come from the multi-step
# MetricsEntry FSM (clicks -> leads -> sales, all bound to an existing
# campaign_id) — a generic single-form create would let the mini-app insert
# an orphaned entry with no existence check, unlike metrics_sales() above.
miniapp_config = {
    "resources": [
        {
            "name": "campaigns",
            "table": "campaigns",
            "order_by": "created_at DESC",
            "creatable": True,
            "scope": {"type": "global"},
            "title": "Кампании",
            "titleField": "name",
            "fields": [
                {"name": "name", "required": True, "label": "Название", "kind": "text", "list": True, "detail": True, "create": True},
                {"name": "channel", "required": True, "label": "Канал", "kind": "text", "list": True, "detail": True, "create": True},
                {"name": "budget", "label": "Бюджет", "kind": "number", "list": True, "detail": True, "create": True},
                {"name": "start_date", "required": True, "label": "Начало", "kind": "date", "list": False, "detail": True, "create": True},
                {"name": "end_date", "label": "Окончание", "kind": "date", "list": False, "detail": True, "create": True},
                {"name": "status", "label": "Статус", "kind": "status", "list": True, "detail": True, "create": False},
                {"name": "created_by", "label": "Создал", "kind": "number", "list": False, "detail": True, "create": False},
                {"name": "created_at", "label": "Создано", "kind": "date", "list": False, "detail": True, "create": False},
            ],
        },
        {
            "name": "metrics",
            "table": "metric_entries",
            "order_by": "entered_at DESC",
            "creatable": False,
            "scope": {"type": "scoped", "parent": "campaigns", "via": "campaign_id"},
            "title": "Метрики",
            "titleField": "entered_at",
            "fields": [
                {"name": "campaign_id", "label": "ID кампании", "kind": "number", "list": True, "detail": True, "create": False},
                {"name": "clicks", "label": "Переходы", "kind": "number", "list": True, "detail": True, "create": False},
                {"name": "leads", "label": "Заявки", "kind": "number", "list": True, "detail": True, "create": False},
                {"name": "sales", "label": "Продажи", "kind": "number", "list": True, "detail": True, "create": False},
                {"name": "entered_by", "label": "Внёс", "kind": "number", "list": False, "detail": True, "create": False},
                {"name": "entered_at", "label": "Дата", "kind": "date", "list": True, "detail": True, "create": False},
            ],
        },
    ],
}


async def _campaign_totals(db: aiosqlite.Connection, campaign_id: int) -> dict:
    row = await (await db.execute(
        "SELECT COALESCE(SUM(clicks),0), COALESCE(SUM(leads),0), COALESCE(SUM(sales),0) "
        "FROM metric_entries WHERE campaign_id=?", (campaign_id,)
    )).fetchone()
    return {"clicks": row[0], "leads": row[1], "sales": row[2]}


async def _load_campaign_with_totals(db: aiosqlite.Connection, campaign_id: int) -> tuple[dict, dict] | None:
    """Shared "fetch campaign row + its metric totals" glue — was duplicated
    verbatim across cb_view_campaign/cb_finish_campaign/metrics_sales
    (review-found triplication). Returns None if the campaign doesn't exist
    (deleted concurrently, or a stale/forged callback id)."""
    db.row_factory = aiosqlite.Row
    row = await (await db.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,))).fetchone()
    if not row:
        return None
    totals = await _campaign_totals(db, campaign_id)
    return dict(row), totals


# ── FSM ───────────────────────────────────────────────────────────────────────

class CampaignCreate(StatesGroup):
    name = State(); channel = State(); budget = State(); start_date = State(); end_date = State()

class MetricsEntry(StatesGroup):
    clicks = State(); leads = State(); sales = State()


# ── keyboards ─────────────────────────────────────────────────────────────────

def kb_main(is_admin: bool = False) -> ReplyKeyboardMarkup:
    # Security fix: this bot is team-only — every button here (➕ Новая
    # кампания, 📋 Кампании, 📊 Сравнение) is behind an _is_admin check in its
    # own handler already. The keyboard used to be sent unconditionally to
    # everyone, with only a text warning ("⛔ этот бот доступен только
    # команде проекта") as the "gate" — not a real one, since the buttons
    # were already visible and tappable. Non-admins now get an empty
    # keyboard instead.
    if not is_admin:
        return ReplyKeyboardMarkup(keyboard=[], resize_keyboard=True)
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="➕ Новая кампания")],
        [KeyboardButton(text="📋 Кампании"), KeyboardButton(text="📊 Сравнение")],
    ], resize_keyboard=True)


def kb_channels() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=ch, callback_data=f"camp_channel:{ch}")] for ch in CHANNELS
    ])


def kb_end_date() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="♾ Бессрочно", callback_data="camp_noend")],
    ])


def kb_campaign_actions(campaign_id: int, status: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="➕ Внести метрики", callback_data=f"camp_metrics:{campaign_id}")]]
    if status == "active":
        rows.append([InlineKeyboardButton(text="✅ Завершить", callback_data=f"camp_finish:{campaign_id}")])
    rows.append([InlineKeyboardButton(text="🗑 Удалить", callback_data=f"camp_delete:{campaign_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── /start ────────────────────────────────────────────────────────────────────

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext, config: CampaignTrackerConfig):
    await state.clear()
    admins = _load_admins(config.admins_file)
    sender_id = message.from_user.id
    # Security fix: this used to grant admin to whoever sent /start FIRST,
    # letting anyone who messages the bot before its owner does permanently
    # seize the whole (team-only) bot. When bots.owner_telegram_id is known,
    # only that user may claim the empty-admins bootstrap slot; in
    # standalone/env mode (owner_telegram_id unknown) the old first-comer
    # behavior is kept as the only option available.
    is_owner = config.owner_telegram_id is not None and sender_id == config.owner_telegram_id
    first_time_admin = not admins and (is_owner or config.owner_telegram_id is None)
    if first_time_admin:
        _save_admins(config.admins_file, {str(sender_id)})
        if config.bot_id is not None:
            try:
                await add_bot_admin(config.bot_id, str(sender_id))
            except Exception as e:
                logger.warning(f"cmd_start: add_bot_admin sync failed for bot {config.bot_id}: {e}")

    is_admin_now = _is_admin(sender_id, config)
    if config.welcome_image.exists():
        await message.answer_photo(FSInputFile(str(config.welcome_image)),
                                    caption=WELCOME_TEXT, parse_mode="HTML", reply_markup=kb_main(is_admin_now))
    else:
        await message.answer(WELCOME_TEXT, parse_mode="HTML", reply_markup=kb_main(is_admin_now))

    if first_time_admin:
        await message.answer(
            "👑 <b>Вы — администратор этого бота.</b>\n\n"
            "Управление администраторами:\n"
            "<code>/addadmin ID</code> — добавить администратора\n"
            "<code>/removeadmin ID</code> — убрать администратора\n"
            "<code>/admins</code> — список администраторов",
            parse_mode="HTML"
        )
    elif not _is_admin(message.from_user.id, config):
        await message.answer("⛔ Этот бот доступен только команде проекта.")


# ── NEW CAMPAIGN ──────────────────────────────────────────────────────────────

@router.message(F.text == "➕ Новая кампания")
async def new_campaign_start(msg: Message, state: FSMContext, config: CampaignTrackerConfig):
    await state.clear()
    if not _is_admin(msg.from_user.id, config):
        await msg.answer("⛔ Нет доступа"); return
    await state.set_state(CampaignCreate.name)
    await msg.answer("📝 Название кампании:")


@router.message(CampaignCreate.name, F.text, ~F.text.in_(MAIN_BUTTONS))
async def new_campaign_name(msg: Message, state: FSMContext):
    await state.update_data(name=msg.text.strip())
    await state.set_state(CampaignCreate.channel)
    await msg.answer("📡 Выберите канал:", reply_markup=kb_channels())


@router.callback_query(CampaignCreate.channel, F.data.startswith("camp_channel:"))
async def new_campaign_channel(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    channel = cb.data.split(":", 1)[1]
    await state.update_data(channel=channel)
    await state.set_state(CampaignCreate.budget)
    try:
        await cb.message.edit_text(f"📡 Канал: {_esc(channel)}")
    except Exception:
        pass
    await cb.message.answer("💰 Бюджет кампании (цифрами):")


@router.message(CampaignCreate.channel, F.text, ~F.text.in_(MAIN_BUTTONS))
async def new_campaign_channel_wrong_input(msg: Message):
    """Every other creation step has a text handler that reprompts on bad
    input; this state previously had ONLY a callback_query handler, so free
    text here matched nothing and was silently dropped with zero feedback —
    review-found gap. Re-sends the same inline keyboard rather than just an
    error, since that's the only way to actually proceed from this state."""
    await msg.answer("Пожалуйста, выберите канал кнопкой ниже:", reply_markup=kb_channels())


def _parse_budget(text: str) -> float | None:
    """Same strict-match rationale as _parse_nonneg_int (see its docstring):
    the previous re.sub(r"[^\d.]", "", ...) strip silently mangled input
    instead of rejecting it, e.g. "1e10" -> "110" (stripped the 'e', off by
    ~8 orders of magnitude) with no error shown. Comma is normalized to a
    decimal point first since Russian input conventionally uses "1500,50"
    for a fractional amount. Bounded against _MAX_BUDGET to keep the stored
    value a normal display-able number (an unbounded huge string would
    otherwise parse to float('inf') without raising)."""
    cleaned = text.strip().replace(",", ".")
    if not re.fullmatch(r"\d+(\.\d+)?", cleaned):
        return None
    value = float(cleaned)
    if value > _MAX_BUDGET:
        return None
    return value


@router.message(CampaignCreate.budget, F.text, ~F.text.in_(MAIN_BUTTONS))
async def new_campaign_budget(msg: Message, state: FSMContext):
    budget = _parse_budget(msg.text)
    if budget is None:
        await msg.answer("Введите неотрицательное число, например: 15000"); return
    await state.update_data(budget=budget)
    await state.set_state(CampaignCreate.start_date)
    await msg.answer("📅 Дата начала (ДД.ММ.ГГГГ):")


@router.message(CampaignCreate.start_date, F.text, ~F.text.in_(MAIN_BUTTONS))
async def new_campaign_start_date(msg: Message, state: FSMContext):
    start_date = _parse_date(msg.text)
    if not start_date:
        await msg.answer("Не удалось разобрать дату. Формат: ДД.ММ.ГГГГ, например: 01.03.2026"); return
    await state.update_data(start_date=start_date)
    await state.set_state(CampaignCreate.end_date)
    await msg.answer("📅 Дата окончания (ДД.ММ.ГГГГ) или нажмите «Бессрочно»:", reply_markup=kb_end_date())


@router.callback_query(CampaignCreate.end_date, F.data == "camp_noend")
async def new_campaign_no_end(cb: CallbackQuery, state: FSMContext, config: CampaignTrackerConfig):
    await cb.answer()
    try:
        await cb.message.edit_text("📅 Дата окончания: бессрочно")
    except Exception:
        pass
    await _finish_campaign_creation(cb.message, cb.from_user.id, state, config, end_date=None)


@router.message(CampaignCreate.end_date, F.text, ~F.text.in_(MAIN_BUTTONS))
async def new_campaign_end_date(msg: Message, state: FSMContext, config: CampaignTrackerConfig):
    end_date = _parse_date(msg.text)
    if not end_date:
        await msg.answer("Не удалось разобрать дату. Формат: ДД.ММ.ГГГГ, например: 30.04.2026, "
                          "или нажмите «Бессрочно» выше."); return
    data = await state.get_data()
    if end_date < data["start_date"]:
        await msg.answer("Дата окончания раньше даты начала — введите ещё раз."); return
    await _finish_campaign_creation(msg, msg.from_user.id, state, config, end_date=end_date)


async def _finish_campaign_creation(
    msg: Message, user_id: int, state: FSMContext, config: CampaignTrackerConfig, end_date: str | None
) -> None:
    data = await state.get_data()
    async with aiosqlite.connect(config.db_path) as db:
        cur = await db.execute(
            "INSERT INTO campaigns (name, channel, budget, start_date, end_date, created_by) "
            "VALUES (?,?,?,?,?,?)",
            (data["name"], data["channel"], data["budget"], data["start_date"], end_date, user_id)
        )
        await db.commit()
        campaign_id = cur.lastrowid
    await state.clear()
    logger.info(f"campaign_tracker: campaign {campaign_id} ({data['name']!r}) created by user {user_id}")
    await msg.answer(
        f"✅ Кампания <b>{_esc(data['name'])}</b> создана (ID {campaign_id}).",
        parse_mode="HTML", reply_markup=kb_main(True)
    )


# ── CAMPAIGN LIST / VIEW ──────────────────────────────────────────────────────

def _campaign_card_text(row: dict, totals: dict) -> str:
    cpl = (row["budget"] / totals["leads"]) if totals["leads"] else None
    conv = (totals["sales"] / totals["leads"] * 100) if totals["leads"] else None
    status_label = "🟢 активна" if row["status"] == "active" else "⚪️ завершена"
    return (
        f"📊 <b>{_esc(row['name'])}</b> ({status_label})\n"
        f"📡 Канал: {_esc(row['channel'])}\n"
        f"💰 Бюджет: {row['budget']:.2f}\n"
        f"📅 {_fmt_date(row['start_date'])} — {_fmt_date(row['end_date'])}\n\n"
        f"🖱 Переходы: {totals['clicks']}\n"
        f"📩 Заявки: {totals['leads']}\n"
        f"💵 Продажи: {totals['sales']}\n"
        f"💸 Стоимость заявки: {f'{cpl:.2f}' if cpl is not None else '—'}\n"
        f"📈 Конверсия в продажу: {f'{conv:.1f}%' if conv is not None else '—'}"
    )


@router.message(F.text == "📋 Кампании")
async def list_campaigns(msg: Message, state: FSMContext, config: CampaignTrackerConfig):
    await state.clear()
    if not _is_admin(msg.from_user.id, config):
        await msg.answer("⛔ Нет доступа"); return
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT id, name, status FROM campaigns ORDER BY status='finished', created_at DESC"
        )).fetchall()
    if not rows:
        await msg.answer("Пока нет ни одной кампании. Нажмите «➕ Новая кампания»."); return
    btns = []
    for r in rows:
        prefix = "🟢" if r["status"] == "active" else "⚪️"
        label = f"{prefix} {r['name']}"
        short = label if len(label) <= 50 else label[:47] + "…"
        btns.append([InlineKeyboardButton(text=short, callback_data=f"camp_view:{r['id']}")])
    await msg.answer("📋 <b>Кампании:</b>", parse_mode="HTML",
                      reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))


def _parse_callback_id(data: str) -> int | None:
    """callback_data is attacker-influenced (any Telegram client can send
    arbitrary callback_query.data for a button it was never shown), so a
    non-numeric or absurdly large id must not crash the handler — same
    reasoning as templates/referral_program.py's cb_confirm(). Bounding
    against _SQLITE_INT_MAX here (rather than only catching OverflowError at
    each db.execute call site) is deliberate: Python's int() has unlimited
    precision and never raises OverflowError itself, so a 20+ digit id would
    otherwise sail through this parse and only blow up later, uncaught, at
    whichever db.execute binds it. campaign ids are AUTOINCREMENT starting at
    1, so a non-positive value is also rejected here rather than deferred to
    a "not found" DB lookup."""
    try:
        value = int(data.split(":", 1)[1])
    except (IndexError, ValueError):
        return None
    if not (0 < value <= _SQLITE_INT_MAX):
        return None
    return value


@router.callback_query(F.data.startswith("camp_view:"))
async def cb_view_campaign(cb: CallbackQuery, config: CampaignTrackerConfig):
    if not _is_admin(cb.from_user.id, config):
        await cb.answer("⛔ Нет доступа", show_alert=True); return
    await cb.answer()
    campaign_id = _parse_callback_id(cb.data)
    if campaign_id is None:
        try:
            await cb.message.edit_text("Некорректная кампания.")
        except Exception:
            pass
        return
    async with aiosqlite.connect(config.db_path) as db:
        loaded = await _load_campaign_with_totals(db, campaign_id)
    if loaded is None:
        try:
            await cb.message.edit_text("Кампания не найдена (возможно, удалена).")
        except Exception:
            pass
        return
    row, totals = loaded
    try:
        await cb.message.edit_text(
            _campaign_card_text(row, totals), parse_mode="HTML",
            reply_markup=kb_campaign_actions(campaign_id, row["status"])
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("camp_finish:"))
async def cb_finish_campaign(cb: CallbackQuery, config: CampaignTrackerConfig):
    if not _is_admin(cb.from_user.id, config):
        await cb.answer("⛔ Нет доступа", show_alert=True); return
    campaign_id = _parse_callback_id(cb.data)
    if campaign_id is None:
        await cb.answer(); return
    async with aiosqlite.connect(config.db_path) as db:
        # Status-guarded UPDATE — a double-tap on "Завершить" is a no-op the
        # second time instead of re-triggering the flow, same technique as
        # referral_program.py's cb_confirm() guard.
        await db.execute(
            "UPDATE campaigns SET status='finished' WHERE id=? AND status='active'", (campaign_id,)
        )
        await db.commit()
        loaded = await _load_campaign_with_totals(db, campaign_id)
    if loaded is None:
        await cb.answer(); return
    row, totals = loaded
    logger.info(f"campaign_tracker: campaign {campaign_id} finished by user {cb.from_user.id}")
    await cb.answer("Кампания завершена")
    try:
        await cb.message.edit_text(
            _campaign_card_text(row, totals), parse_mode="HTML",
            reply_markup=kb_campaign_actions(campaign_id, row["status"])
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("camp_delete:"))
async def cb_delete_campaign(cb: CallbackQuery, config: CampaignTrackerConfig):
    if not _is_admin(cb.from_user.id, config):
        await cb.answer("⛔ Нет доступа", show_alert=True); return
    campaign_id = _parse_callback_id(cb.data)
    if campaign_id is None:
        await cb.answer(); return
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute("DELETE FROM metric_entries WHERE campaign_id=?", (campaign_id,))
        await db.execute("DELETE FROM campaigns WHERE id=?", (campaign_id,))
        await db.commit()
    logger.info(f"campaign_tracker: campaign {campaign_id} deleted by user {cb.from_user.id}")
    await cb.answer("Удалено")
    try:
        await cb.message.edit_text("🗑 Кампания удалена.")
    except Exception:
        pass


# ── METRICS ENTRY ─────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("camp_metrics:"))
async def cb_metrics_start(cb: CallbackQuery, state: FSMContext, config: CampaignTrackerConfig):
    if not _is_admin(cb.from_user.id, config):
        await cb.answer("⛔ Нет доступа", show_alert=True); return
    campaign_id = _parse_callback_id(cb.data)
    if campaign_id is None:
        await cb.answer(); return
    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute("SELECT id FROM campaigns WHERE id=?", (campaign_id,))).fetchone()
    if not row:
        await cb.answer("Кампания не найдена", show_alert=True); return
    await cb.answer()
    # Defensive clear before starting a fresh flow — CampaignCreate and
    # MetricsEntry share one FSMContext storage record per user (aiogram
    # keys it by user+chat+bot, not by StatesGroup), so an abandoned
    # CampaignCreate flow could otherwise leave stale keys behind. Their key
    # names don't currently collide, but clearing removes the reliance on
    # that staying true (review-found fragility, not a live bug).
    await state.clear()
    await state.set_state(MetricsEntry.clicks)
    await state.update_data(campaign_id=campaign_id)
    await cb.message.answer("🖱 Переходы (число, 0 если нет):")


def _parse_nonneg_int(text: str) -> int | None:
    """Strict digit-only match, not a strip-then-parse — stripping stray
    characters (the old approach) silently mangled input instead of
    rejecting it: "10.5" -> "105" (10x too large) and "-5" -> "5" (sign
    silently dropped) both passed as valid. fullmatch rejects both instead,
    forcing a re-prompt. Also bounded against _MAX_METRIC_VALUE — well
    within _SQLITE_INT_MAX but generous for any real ad-campaign count — so
    a huge pasted number is rejected here rather than raising OverflowError
    at the INSERT in metrics_sales()."""
    cleaned = text.strip()
    if not re.fullmatch(r"\d+", cleaned):
        return None
    value = int(cleaned)
    if value > _MAX_METRIC_VALUE:
        return None
    return value


@router.message(MetricsEntry.clicks, F.text, ~F.text.in_(MAIN_BUTTONS))
async def metrics_clicks(msg: Message, state: FSMContext):
    clicks = _parse_nonneg_int(msg.text)
    if clicks is None:
        await msg.answer("Введите целое число ≥ 0, например: 120"); return
    await state.update_data(clicks=clicks)
    await state.set_state(MetricsEntry.leads)
    await msg.answer("📩 Заявки (число, 0 если нет):")


@router.message(MetricsEntry.leads, F.text, ~F.text.in_(MAIN_BUTTONS))
async def metrics_leads(msg: Message, state: FSMContext):
    leads = _parse_nonneg_int(msg.text)
    if leads is None:
        await msg.answer("Введите целое число ≥ 0, например: 15"); return
    await state.update_data(leads=leads)
    await state.set_state(MetricsEntry.sales)
    await msg.answer("💵 Продажи (число, 0 если нет):")


@router.message(MetricsEntry.sales, F.text, ~F.text.in_(MAIN_BUTTONS))
async def metrics_sales(msg: Message, state: FSMContext, config: CampaignTrackerConfig):
    sales = _parse_nonneg_int(msg.text)
    if sales is None:
        await msg.answer("Введите целое число ≥ 0, например: 3"); return
    data = await state.get_data()
    async with aiosqlite.connect(config.db_path) as db:
        # Existence check BEFORE the insert — without it, a campaign deleted
        # by another admin while this one is mid-flow (metric_entries has no
        # PRAGMA foreign_keys=ON enforcing its REFERENCES campaigns(id)) would
        # still get an orphaned row inserted, and the flow would still say
        # "Метрики сохранены" with no indication anything was wrong
        # (review-found race).
        db.row_factory = aiosqlite.Row
        exists = await (await db.execute(
            "SELECT 1 FROM campaigns WHERE id=?", (data["campaign_id"],)
        )).fetchone()
        if not exists:
            await state.clear()
            await msg.answer("⚠️ Кампания была удалена — метрики не сохранены.", reply_markup=kb_main(True))
            return
        await db.execute(
            "INSERT INTO metric_entries (campaign_id, clicks, leads, sales, entered_by) VALUES (?,?,?,?,?)",
            (data["campaign_id"], data["clicks"], data["leads"], sales, msg.from_user.id)
        )
        await db.commit()
        loaded = await _load_campaign_with_totals(db, data["campaign_id"])
    await state.clear()
    logger.info(
        f"campaign_tracker: metrics entered for campaign {data['campaign_id']} by user {msg.from_user.id} "
        f"(clicks={data['clicks']}, leads={data['leads']}, sales={sales})"
    )
    await msg.answer("✅ Метрики сохранены.", reply_markup=kb_main(True))
    if loaded:
        row, totals = loaded
        await msg.answer(
            _campaign_card_text(row, totals), parse_mode="HTML",
            reply_markup=kb_campaign_actions(data["campaign_id"], row["status"])
        )


# ── COMPARISON ─────────────────────────────────────────────────────────────────

@router.message(F.text == "📊 Сравнение")
async def compare_campaigns(msg: Message, state: FSMContext, config: CampaignTrackerConfig):
    await state.clear()
    if not _is_admin(msg.from_user.id, config):
        await msg.answer("⛔ Нет доступа"); return
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT id, name, channel, budget FROM campaigns WHERE status='active' ORDER BY created_at"
        )).fetchall()
        if not rows:
            await msg.answer("Нет активных кампаний для сравнения."); return
        ranked, no_data = [], []
        for r in rows:
            totals = await _campaign_totals(db, r["id"])
            if totals["leads"]:
                cpl = r["budget"] / totals["leads"]
                conv = totals["sales"] / totals["leads"] * 100
                ranked.append((r, totals, cpl, conv))
            else:
                no_data.append(r)

    lines = ["📊 <b>Сравнение кампаний</b> (по стоимости заявки, лучшие сверху):\n"]
    if ranked:
        ranked.sort(key=lambda t: t[2])
        for r, totals, cpl, conv in ranked:
            lines.append(
                f"• <b>{_esc(r['name'])}</b> ({_esc(r['channel'])}) — "
                f"CPL {cpl:.2f}, конверсия {conv:.1f}%, заявок {totals['leads']}"
            )
    else:
        lines.append("Пока ни у одной активной кампании нет заявок.")
    if no_data:
        lines.append("\n<b>Без данных о заявках:</b>")
        for r in no_data:
            lines.append(f"• {_esc(r['name'])} ({_esc(r['channel'])})")
    await msg.answer("\n".join(lines), parse_mode="HTML")


# ── ADMIN COMMANDS ────────────────────────────────────────────────────────────

@router.message(Command("addadmin"))
async def cmd_addadmin(msg: Message, config: CampaignTrackerConfig):
    if not _is_admin(msg.from_user.id, config): await msg.answer("⛔ Нет доступа"); return
    parts = msg.text.split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit(): await msg.answer("Использование: /addadmin <id>"); return
    ids = _load_admins(config.admins_file); ids.add(parts[1]); _save_admins(config.admins_file, ids)
    if config.bot_id is not None:
        try:
            await add_bot_admin(config.bot_id, parts[1])
        except Exception as e:
            logger.warning(f"cmd_addadmin: add_bot_admin sync failed for bot {config.bot_id}: {e}")
    await msg.answer(f"✅ <code>{parts[1]}</code> добавлен.", parse_mode="HTML")

@router.message(Command("removeadmin"))
async def cmd_removeadmin(msg: Message, config: CampaignTrackerConfig):
    if not _is_admin(msg.from_user.id, config): await msg.answer("⛔ Нет доступа"); return
    parts = msg.text.split()
    if len(parts) < 2: await msg.answer("Использование: /removeadmin <id>"); return
    ids = _load_admins(config.admins_file)
    if parts[1] in ids and len(ids) == 1:
        # An emptied admins file is indistinguishable from "never bootstrapped"
        # (see cmd_start's first_time_admin check) — removing the last admin
        # would hand admin rights to whoever sends /start next.
        await msg.answer("⚠️ Нельзя удалить последнего администратора."); return
    ids.discard(parts[1]); _save_admins(config.admins_file, ids)
    if config.bot_id is not None:
        try:
            await remove_bot_admin(config.bot_id, parts[1])
        except Exception as e:
            logger.warning(f"cmd_removeadmin: remove_bot_admin sync failed for bot {config.bot_id}: {e}")
    await msg.answer(f"✅ <code>{parts[1]}</code> удалён.", parse_mode="HTML")

@router.message(Command("admins"))
async def cmd_admins(msg: Message, config: CampaignTrackerConfig):
    if not _is_admin(msg.from_user.id, config): await msg.answer("⛔ Нет доступа"); return
    ids = _load_admins(config.admins_file)
    await msg.answer("👥 " + ("\n".join(f"• <code>{i}</code>" for i in ids) or "Пусто"), parse_mode="HTML")


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
