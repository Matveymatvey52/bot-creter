# TEMPLATE: feedback_survey
# USE FOR: сбор отзывов и оценок от клиентов — числовая оценка по настраиваемой шкале (1-5 / 1-10) с опциональным комментарием; агрегированная статистика: средний балл, распределение оценок по значениям, последние комментарии, фильтр по периоду (неделя/месяц/всё время)
# CUSTOMIZE: sections marked with # CUSTOMIZE
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import aiosqlite
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)

from db.database import add_bot_admin, remove_bot_admin

# ── CUSTOMIZE ────────────────────────────────────────────────────────────────
# Same status as every other template's CUSTOMIZE block: per-file source-text
# customization Claude edits when generating a specific bot, not per-bot
# runtime state (that's config.db_path/admins_file below). Feedback records
# themselves are runtime data, added via the client-facing flow or the admin's
# own "Внести отзыв вручную".
BOT_DESCRIPTION = "Сбор отзывов клиентов: оценка по шкале + комментарий, статистика по среднему баллу и распределению."
WELCOME_TEXT = (
    "⭐ <b>Отзывы клиентов</b>\n\n"
    "Смотрите статистику по собранным оценкам или внесите отзыв, "
    "полученный не через бота.\n\nВыберите действие:"
)
CLIENT_WELCOME_TEXT = (
    "⭐ Здравствуйте!\n\n"
    "Будем рады узнать ваше мнение — оцените нас:"
)
COMMENT_PROMPT_TEXT = (
    "💬 Хотите добавить комментарий?\n"
    "Можете написать, когда вы у нас были, и любые детали — что понравилось "
    "или что стоит улучшить."
)
RATING_MIN = 1
RATING_MAX = 5            # CUSTOMIZE: например 10 для шкалы 1-10
RATING_EMOJI = "⭐"
COMMENT_MAX_LEN = 500
LABEL_MAX_LEN = 100        # "от кого" пометка в ручном вводе админа
RECENT_COMMENTS_LIMIT = 10
# ── END CUSTOMIZE ─────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
router = Router()

# Same staleness guard as templates/orders_tracker.py's FLOW_TIMEOUT_SECONDS/
# _flow_expired: without it, a client who taps a rating and then goes quiet
# for a long time has their next unrelated plain-text message silently
# swallowed into a half-finished rating flow from a different session.
FLOW_TIMEOUT_SECONDS = 300


def _flow_expired(data: dict) -> bool:
    started_at = data.get("started_at")
    return started_at is None or (time.time() - started_at) > FLOW_TIMEOUT_SECONDS


async def _edit_text_safe(message: Message, text: str, **kwargs) -> None:
    """Two near-simultaneous taps of the same button can both pass the FSM
    state filter before either has advanced the state (MemoryStorage reads
    don't suspend), so the loser redundantly repeats the identical
    edit_text call — Telegram rejects that with "message is not modified".
    The state itself was already correctly advanced by the winner, so this
    is harmless; swallow only that specific error, anything else re-raises."""
    try:
        await message.edit_text(text, **kwargs)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


# ── config ───────────────────────────────────────────────────────────────────
# Same pattern as every other template — see docs/STAGE2_DESIGN.md.

@dataclass
class FeedbackSurveyConfig:
    bot_name: str
    db_path: str
    admins_file: Path
    welcome_image: Path
    display_name: str | None = None
    group_chat_id: str | None = None
    # None in standalone/subprocess mode (config_from_env); set from bots.id
    # in webhook mode (config_from_bot_row). Not used by this template's own
    # logic (no sheets/payments integration wired yet) but kept for shape
    # parity with every other template's Config, per docs/STAGE2_DESIGN.md.
    bot_id: int | None = None
    owner_telegram_id: int | None = None


def _paths_for(name: str, data_dir: Path) -> FeedbackSurveyConfig:
    return FeedbackSurveyConfig(
        bot_name=name,
        db_path=str(data_dir / f"{name}_data.db"),
        admins_file=data_dir / f"admins_{name}.json",
        welcome_image=data_dir / "bot_images" / f"{name}.jpg",
    )


def config_from_env() -> FeedbackSurveyConfig:
    """Standalone/subprocess mode."""
    name = Path(__file__).stem
    data_dir = Path(os.getenv("DATA_DIR", "./data"))
    data_dir.mkdir(exist_ok=True)
    return _paths_for(name, data_dir)


def config_from_bot_row(bot_row: dict, data_dir: Path) -> FeedbackSurveyConfig:
    """Webhook runtime mode. Paths built from bot_row["bot_id"] (bots.id, the
    physically unique AUTOINCREMENT PK) — NOT bot_row["name"] — same reasoning
    as every other template's config_from_bot_row (see docs/STAGE2_DESIGN.md
    "Изоляция по bots.id")."""
    bot_id = bot_row["bot_id"]
    config = FeedbackSurveyConfig(
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
    """Injects this bot's FeedbackSurveyConfig into data["config"]."""

    def __init__(self, config: FeedbackSurveyConfig) -> None:
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

def _is_admin(user_id: int, config: FeedbackSurveyConfig) -> bool:
    # The DB-known owner (bots.owner_telegram_id) is always an admin, even if
    # the local admins_file is empty/stale/hijacked — see cmd_start below for
    # why the file alone can't be trusted as the sole source of truth.
    if config.owner_telegram_id is not None and str(user_id) == str(config.owner_telegram_id):
        return True
    return str(user_id) in _load_admins(config.admins_file)


def _valid_admin_id(text: str) -> bool:
    """Same guard as templates/moderator.py's _valid_admin_id() — rejects
    unbounded-length strings, non-ASCII look-alike digits, and 0/leading-zero
    phantom ids that would inflate the admin list without ever matching
    _is_admin()."""
    if not (bool(text) and text.isascii() and text.isdigit() and len(text) <= 15):
        return False
    return int(text) > 0 and str(int(text)) == text


def _esc(value, max_len: int = 500) -> str:
    """HTML-escapes AND length-bounds any user-supplied text before it goes into
    a parse_mode="HTML" message — same helper/rationale as every other
    template's _esc()."""
    text = str(value) if value is not None else ""
    if len(text) > max_len:
        text = text[:max_len] + "…"
    return html.escape(text)


def _join_bounded(lines: list[str], limit: int = 3500) -> str:
    """Joins lines with a length budget, dropping only WHOLE trailing lines —
    same rationale as every other template's _join_bounded()."""
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

async def init_db(db_path: str):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS feedback (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                rating         INTEGER NOT NULL,
                comment        TEXT,
                source         TEXT NOT NULL DEFAULT 'client' CHECK(source IN ('client','admin')),
                client_user_id INTEGER,
                client_label   TEXT,
                created_at     TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_feedback_created_at ON feedback(created_at)")
        await db.commit()


miniapp_config = {
    "resources": [
        {
            "name": "feedback",
            "table": "feedback",
            "order_by": "created_at DESC",
            "creatable": True,
            "scope": {"type": "global"},
            "title": "Отзывы",
            "titleField": "client_label",
            "fields": [
                {"name": "rating", "required": True, "label": "Оценка", "kind": "number", "list": True, "detail": True, "create": True},
                {"name": "comment", "label": "Комментарий", "kind": "text", "list": False, "detail": True, "create": True},
                {"name": "source", "label": "Источник", "kind": "status", "list": True, "detail": True, "create": False},
                {"name": "client_label", "label": "От кого", "kind": "text", "list": True, "detail": True, "create": True},
            ],
        },
    ],
}


async def _insert_feedback(
    db_path: str, rating: int, comment: str | None, source: str,
    client_user_id: int | None, client_label: str | None,
) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO feedback (rating, comment, source, client_user_id, client_label) "
            "VALUES (?, ?, ?, ?, ?)",
            (rating, comment, source, client_user_id, client_label),
        )
        await db.commit()


# ── stats rendering ──────────────────────────────────────────────────────────

def _bar(count: int, max_count: int, width: int = 10) -> str:
    if count <= 0 or max_count <= 0:
        return ""
    filled = max(1, round((count / max_count) * width))
    return "█" * filled


async def _stats_query(db_path: str, d_from: str, d_to: str) -> dict:
    """Fetches the raw aggregation for [d_from, d_to] (inclusive, ISO dates)
    with no formatting — kept separate from _format_stats_text so aggregation
    correctness (average/distribution) can be tested directly against
    structured data instead of parsing rendered HTML."""
    async with aiosqlite.connect(db_path) as db:
        count, avg = await (await db.execute(
            "SELECT COUNT(*), AVG(rating) FROM feedback WHERE date(created_at) BETWEEN ? AND ?",
            (d_from, d_to),
        )).fetchone()
        dist_rows = await (await db.execute(
            "SELECT rating, COUNT(*) FROM feedback WHERE date(created_at) BETWEEN ? AND ? GROUP BY rating",
            (d_from, d_to),
        )).fetchall()
        comment_rows = await (await db.execute(
            "SELECT rating, comment, created_at, source, client_label FROM feedback "
            "WHERE comment IS NOT NULL AND date(created_at) BETWEEN ? AND ? "
            "ORDER BY id DESC LIMIT ?",
            (d_from, d_to, RECENT_COMMENTS_LIMIT),
        )).fetchall()
    return {
        "count": count,
        "avg": avg,
        "dist": {rating: c for rating, c in dist_rows},
        "comments": comment_rows,
    }


def _format_stats_text(data: dict, label: str) -> str:
    lines = [f"📊 <b>Статистика — {label}</b>\n"]
    if not data["count"]:
        lines.append("Оценок пока нет за этот период.")
        return _join_bounded(lines)

    dist = data["dist"]
    max_count = max(dist.values()) if dist else 0

    lines.append(f"Средний балл: <b>{data['avg']:.1f}</b> {RATING_EMOJI} ({data['count']} оценок)\n")
    lines.append("<b>Распределение:</b>")
    for n in range(RATING_MAX, RATING_MIN - 1, -1):
        c = dist.get(n, 0)
        lines.append(f"{n} {RATING_EMOJI} {_bar(c, max_count)} {c}")

    if data["comments"]:
        lines.append("\n<b>Последние комментарии:</b>")
        for rating, comment, created_at, source, client_label in data["comments"]:
            who = f" ({_esc(client_label, LABEL_MAX_LEN)})" if source == "admin" and client_label else ""
            lines.append(f"• {rating}{RATING_EMOJI}{who} — {_esc(comment, COMMENT_MAX_LEN)} · {created_at}")

    return _join_bounded(lines)


async def _stats_text(db_path: str, d_from: str, d_to: str, label: str) -> str:
    return _format_stats_text(await _stats_query(db_path, d_from, d_to), label)


# ── FSM states ───────────────────────────────────────────────────────────────
# Shared by BOTH collection paths (owner decision: both a client self-service
# flow and an admin manual-entry flow feed the same table, distinguished by
# feedback.source) — state data's "source" key ("client"/"admin") picks which
# branch each step takes. The admin branch alone visits `client_label`; the
# client branch skips straight from `rating` to `comment`.

class RateFlow(StatesGroup):
    rating = State()
    client_label = State()   # admin path only
    comment = State()

class AdminMgmtFlow(StatesGroup):
    add_admin = State()
    remove_admin_pick = State()


# ── keyboards ─────────────────────────────────────────────────────────────────

def kb_back(callback_data: str = "main_menu") -> InlineKeyboardButton:
    return InlineKeyboardButton(text="◀️ Назад", callback_data=callback_data)

def kb_flow_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="fb_flow_cancel")],
    ])

def kb_skip_cancel(skip_callback_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭️ Пропустить", callback_data=skip_callback_data)],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="fb_flow_cancel")],
    ])

def kb_main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="fb_stats")],
        [InlineKeyboardButton(text="➕ Внести отзыв вручную", callback_data="fb_admin_new")],
        [InlineKeyboardButton(text="👥 Админы", callback_data="adm_menu")],
    ])

def kb_rating() -> InlineKeyboardMarkup:
    numbers = list(range(RATING_MIN, RATING_MAX + 1))
    rows = [
        [
            InlineKeyboardButton(text=f"{n} {RATING_EMOJI}", callback_data=f"fb_rate:{n}")
            for n in numbers[i:i + 5]
        ]
        for i in range(0, len(numbers), 5)
    ]
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="fb_flow_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_period() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Неделя", callback_data="fbperiod:week"),
         InlineKeyboardButton(text="📅 Месяц", callback_data="fbperiod:month")],
        [InlineKeyboardButton(text="📅 Всё время", callback_data="fbperiod:all")],
        [kb_back()],
    ])

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


# ── /start ────────────────────────────────────────────────────────────────────

@router.message(Command("start"), F.chat.type == "private")
async def cmd_start(message: Message, state: FSMContext, config: FeedbackSurveyConfig):
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
    # regular client flow. In standalone/env mode (owner_telegram_id unknown)
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
        return

    # Owner decision: the client submits directly, no admin-gate on receipt —
    # anyone who isn't a registered admin lands straight in the rating flow.
    await state.set_state(RateFlow.rating)
    await state.update_data(source="client", started_at=time.time())
    await message.answer(CLIENT_WELCOME_TEXT, parse_mode="HTML", reply_markup=kb_rating())


@router.callback_query(F.data == "main_menu")
async def cb_main_menu(cb: CallbackQuery, state: FSMContext, config: FeedbackSurveyConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    await cb.message.edit_text(WELCOME_TEXT, parse_mode="HTML", reply_markup=kb_main_menu())


@router.callback_query(F.data == "fb_flow_cancel")
async def cb_flow_cancel(cb: CallbackQuery, state: FSMContext, config: FeedbackSurveyConfig):
    await cb.answer()
    await state.clear()
    if _is_admin(cb.from_user.id, config):
        await cb.message.edit_text(WELCOME_TEXT, parse_mode="HTML", reply_markup=kb_main_menu())
    else:
        await cb.message.edit_text("Хорошо, может быть в другой раз! 🙂")


# ── rating flow (shared by client self-service and admin manual entry) ────────

@router.callback_query(F.data == "fb_admin_new")
async def cb_admin_new(cb: CallbackQuery, state: FSMContext, config: FeedbackSurveyConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    await state.set_state(RateFlow.rating)
    await state.update_data(source="admin", started_at=time.time())
    await cb.message.edit_text("Выберите оценку:", reply_markup=kb_rating())


@router.callback_query(RateFlow.rating, F.data.startswith("fb_rate:"))
async def cb_rate_pick(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.")
        return
    try:
        rating = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    if not (RATING_MIN <= rating <= RATING_MAX):
        return
    await state.update_data(rating=rating)

    if data.get("source") == "admin":
        await state.set_state(RateFlow.client_label)
        await _edit_text_safe(
            cb.message, "От кого отзыв? Например, имя или телефон (необязательно):",
            reply_markup=kb_skip_cancel("fb_label_skip"),
        )
    else:
        await state.set_state(RateFlow.comment)
        await _edit_text_safe(
            cb.message, COMMENT_PROMPT_TEXT, parse_mode="HTML", reply_markup=kb_skip_cancel("fb_comment_skip"),
        )


@router.message(RateFlow.client_label, F.text, ~F.text.startswith("/"))
async def rate_label_text(msg: Message, state: FSMContext):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.")
        return
    await state.update_data(client_label=msg.text.strip()[:LABEL_MAX_LEN])
    await state.set_state(RateFlow.comment)
    await msg.answer(COMMENT_PROMPT_TEXT, parse_mode="HTML", reply_markup=kb_skip_cancel("fb_comment_skip"))


@router.callback_query(RateFlow.client_label, F.data == "fb_label_skip")
async def cb_label_skip(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.")
        return
    await state.set_state(RateFlow.comment)
    await _edit_text_safe(cb.message, COMMENT_PROMPT_TEXT, parse_mode="HTML", reply_markup=kb_skip_cancel("fb_comment_skip"))


async def _finalize_feedback(event, state: FSMContext, config: FeedbackSurveyConfig, comment: str | None) -> None:
    data = await state.get_data()
    rating = data.get("rating")
    source = data.get("source", "client")
    client_label = data.get("client_label")
    client_user_id = event.from_user.id if source == "client" else None
    await state.clear()
    await _insert_feedback(config.db_path, rating, comment, source, client_user_id, client_label)

    if source == "admin":
        text, markup = "✅ Отзыв сохранён.", kb_main_menu()
    else:
        text, markup = "🙏 Спасибо за отзыв!", None

    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, reply_markup=markup)
    else:
        await event.answer(text, reply_markup=markup)


@router.message(RateFlow.comment, F.text, ~F.text.startswith("/"))
async def rate_comment_text(msg: Message, state: FSMContext, config: FeedbackSurveyConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.")
        return
    # A whitespace-only comment must behave like an explicit skip (NULL, so
    # _stats_query's "WHERE comment IS NOT NULL" excludes it) rather than
    # storing "" and showing up as a blank line in recent comments.
    comment = msg.text.strip()[:COMMENT_MAX_LEN]
    await _finalize_feedback(msg, state, config, comment or None)


@router.callback_query(RateFlow.comment, F.data == "fb_comment_skip")
async def cb_comment_skip(cb: CallbackQuery, state: FSMContext, config: FeedbackSurveyConfig):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.")
        return
    await _finalize_feedback(cb, state, config, None)


# ── STATS ────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "fb_stats")
async def cb_stats_menu(cb: CallbackQuery, state: FSMContext, config: FeedbackSurveyConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    await cb.message.edit_text("📊 Выберите период:", reply_markup=kb_period())


@router.callback_query(F.data.startswith("fbperiod:"))
async def cb_stats_period(cb: CallbackQuery, config: FeedbackSurveyConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    period = cb.data.split(":", 1)[1]
    today = date.today()
    if period == "week":
        d_from = (today - timedelta(days=today.weekday())).isoformat(); d_to = today.isoformat(); label = "Эта неделя"
    elif period == "month":
        d_from = today.replace(day=1).isoformat(); d_to = today.isoformat(); label = "Этот месяц"
    else:
        d_from = "2000-01-01"; d_to = today.isoformat(); label = "Всё время"

    text = await _stats_text(config.db_path, d_from, d_to, label)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_period())


# ── ADMINS menu ────────────────────────────────────────────────────────────────

async def _admins_list_text(config: FeedbackSurveyConfig) -> str:
    ids = sorted(_load_admins(config.admins_file))
    if not ids:
        return "👥 Пусто"
    return _join_bounded(["👥 <b>Администраторы бота:</b>\n"] + [f"• <code>{_esc(i)}</code>" for i in ids])


@router.callback_query(F.data == "adm_menu")
async def cb_adm_menu(cb: CallbackQuery, state: FSMContext, config: FeedbackSurveyConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    text = await _admins_list_text(config)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_admins_menu())


@router.callback_query(F.data == "adm_add")
async def cb_adm_add(cb: CallbackQuery, state: FSMContext, config: FeedbackSurveyConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.set_state(AdminMgmtFlow.add_admin)
    await state.update_data(started_at=time.time())
    await cb.message.edit_text("Введите Telegram ID нового администратора:", reply_markup=kb_flow_cancel())


@router.message(AdminMgmtFlow.add_admin, F.text, ~F.text.startswith("/"))
async def admin_add_id(msg: Message, state: FSMContext, config: FeedbackSurveyConfig):
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
    logger.info(f"feedback_survey: {text} added as bot admin by {msg.from_user.id}")
    await msg.answer(f"✅ <code>{text}</code> добавлен.", parse_mode="HTML", reply_markup=kb_admins_menu())


@router.callback_query(F.data == "adm_remove")
async def cb_adm_remove(cb: CallbackQuery, state: FSMContext, config: FeedbackSurveyConfig):
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
async def cb_adm_remove_pick(cb: CallbackQuery, state: FSMContext, config: FeedbackSurveyConfig):
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
        remove_admin_ids = data["remove_admin_ids"]
        # Explicit bounds check: Python's negative-index wraparound would
        # otherwise let a hand-crafted "adm_rm:-1" silently resolve to the
        # LAST id in the list instead of being rejected as an invalid pick.
        if not (0 <= idx < len(remove_admin_ids)):
            raise IndexError(idx)
        target = remove_admin_ids[idx]
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
    logger.info(f"feedback_survey: {target} removed as bot admin by {cb.from_user.id}")
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
