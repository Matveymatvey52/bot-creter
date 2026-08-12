# TEMPLATE: support_tickets
# USE FOR: техподдержка/хелпдеск с тикетами — категории обращений с приоритетом и SLA, поиск по базе знаний перед созданием тикета, статус-флоу тикета (открыт → в работе → ждём ответа клиента → закрыт, плюс эскалация к специалисту), автоуведомление клиента при ответе поддержки и закрытии тикета, оценка удовлетворённости после закрытия
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
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)

# ── CUSTOMIZE ────────────────────────────────────────────────────────────────
# Same status as every other template's CUSTOMIZE block: per-file source-text
# customization Claude edits when generating a specific bot, not per-bot
# runtime state (that's config.db_path/admins_file below).
BOT_DESCRIPTION = "Техподдержка с тикетами: категории/приоритет/SLA, база знаний перед созданием тикета, эскалация к специалисту, оценка после закрытия."
WELCOME_TEXT = (
    "🎫 <b>Служба поддержки</b>\n\n"
    "Тикеты клиентов с категорией, приоритетом и SLA. База знаний для "
    "самопомощи, эскалация к специалисту по кнопке, автоуведомление клиента "
    "об ответе и закрытии, оценка удовлетворённости.\n\nВыберите действие:"
)
CLIENT_WELCOME_TEXT = (
    "👋 Здравствуйте! Это служба поддержки.\n\n"
    "Можете создать новый тикет или посмотреть свои текущие обращения."
)
# code -> {label, priority, sla_hours}. Priority and SLA are DERIVED from the
# category the client picks — this is the single place that mapping lives.
TICKET_CATEGORIES = {
    "technical": {"label": "🛠 Техническая проблема", "priority": "high", "sla_hours": 4},
    "billing": {"label": "💳 Оплата и биллинг", "priority": "high", "sla_hours": 4},
    "account": {"label": "👤 Аккаунт и доступ", "priority": "medium", "sla_hours": 12},
    "general": {"label": "❓ Общий вопрос", "priority": "low", "sla_hours": 24},
}
PRIORITY_LABELS = {"high": "🔴 Высокий", "medium": "🟡 Средний", "low": "🟢 Низкий"}
STATUS_LABELS = {
    "open": "🆕 Открыт",
    "in_progress": "⚙️ В работе",
    "waiting_response": "⏳ Ждём вашего ответа",
    "escalated": "📞 Эскалирован специалисту",
    "closed": "✅ Закрыт",
}
# Text sent to the CLIENT (not the admin) when their ticket crosses into this
# status. Only the transitions the client actually cares about — same
# philosophy as templates/vehicle_service.py's STATUS_NOTIFY_TEXT (don't spam
# on every internal state change).
STATUS_NOTIFY_TEXT = {
    "waiting_response": "💬 По вашему тикету №{ticket_id} есть новая информация от поддержки — загляните в «📋 Мои тикеты».",
    "closed": "✅ Ваш тикет №{ticket_id} закрыт. Спасибо за обращение!",
}
# ── END CUSTOMIZE ─────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
router = Router()

# Explicit forward-only flow, same shape as templates/vehicle_service.py's
# STATUS_TRANSITIONS: no backward moves except the deliberate in_progress <->
# waiting_response cycle (an admin asks something, client replies, admin
# keeps working — a real back-and-forth, not a status regression). "closed"
# is reachable from every non-terminal status and is itself terminal.
STATUS_TRANSITIONS = {
    "open": ["in_progress", "waiting_response", "escalated", "closed"],
    "in_progress": ["waiting_response", "escalated", "closed"],
    "waiting_response": ["in_progress", "escalated", "closed"],
    "escalated": ["in_progress", "closed"],
    "closed": [],
}


# ── config ───────────────────────────────────────────────────────────────────
# Same pattern as every other template — see docs/STAGE2_DESIGN.md.

@dataclass
class SupportTicketsConfig:
    bot_name: str
    db_path: str
    admins_file: Path
    welcome_image: Path
    display_name: str | None = None
    group_chat_id: str | None = None


def _paths_for(name: str, data_dir: Path) -> SupportTicketsConfig:
    return SupportTicketsConfig(
        bot_name=name,
        db_path=str(data_dir / f"{name}_data.db"),
        admins_file=data_dir / f"admins_{name}.json",
        welcome_image=data_dir / "bot_images" / f"{name}.jpg",
    )


def config_from_env() -> SupportTicketsConfig:
    """Standalone/subprocess mode."""
    name = Path(__file__).stem
    data_dir = Path(os.getenv("DATA_DIR", "./data"))
    data_dir.mkdir(exist_ok=True)
    return _paths_for(name, data_dir)


def config_from_bot_row(bot_row: dict, data_dir: Path) -> SupportTicketsConfig:
    """Webhook runtime mode. Paths built from bot_row["bot_id"] (bots.id, the
    physically unique AUTOINCREMENT PK) — NOT bot_row["name"] — same reasoning
    as every other template's config_from_bot_row (see docs/STAGE2_DESIGN.md
    "Изоляция по bots.id")."""
    bot_id = bot_row["bot_id"]
    config = SupportTicketsConfig(
        bot_name=bot_row["name"],
        db_path=str(data_dir / f"bot_{bot_id}_data.db"),
        admins_file=data_dir / f"admins_{bot_id}.json",
        welcome_image=data_dir / "bot_images" / f"bot_{bot_id}.jpg",
    )
    config.display_name = bot_row.get("display_name")
    config.group_chat_id = bot_row.get("group_chat_id")
    return config


class ConfigMiddleware(BaseMiddleware):
    """Injects this bot's SupportTicketsConfig into data["config"]."""

    def __init__(self, config: SupportTicketsConfig) -> None:
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

def _is_admin(user_id: int, config: SupportTicketsConfig) -> bool:
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


# ── db ────────────────────────────────────────────────────────────────────────

async def init_db(db_path: str):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tickets (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                client_user_id       INTEGER NOT NULL,
                client_name          TEXT NOT NULL,
                category             TEXT NOT NULL,
                priority             TEXT NOT NULL CHECK(priority IN ('low','medium','high')),
                description          TEXT NOT NULL,
                status               TEXT NOT NULL DEFAULT 'open'
                                     CHECK(status IN ('open','in_progress','waiting_response','escalated','closed')),
                sla_deadline         TEXT NOT NULL,
                satisfaction_rating  INTEGER CHECK(satisfaction_rating IS NULL OR satisfaction_rating BETWEEN 1 AND 5),
                created_at           TEXT DEFAULT (datetime('now','localtime')),
                updated_at           TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS ticket_status_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id   INTEGER NOT NULL,
                old_status  TEXT,
                new_status  TEXT NOT NULL,
                changed_by  INTEGER,
                notified    INTEGER DEFAULT 0,
                changed_at  TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        # The "conversation": client's initial description is logged here as
        # the first row (author='client') at ticket creation, and any admin
        # reply is appended (author='admin') and forwarded to the client as a
        # bot message — the SIMPLER option the design brief allows instead of
        # a full chat-thread UI, while still satisfying "ответ администратора
        # с уведомлением клиенту".
        await db.execute("""
            CREATE TABLE IF NOT EXISTS ticket_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id   INTEGER NOT NULL,
                author      TEXT NOT NULL CHECK(author IN ('client','admin')),
                body        TEXT NOT NULL,
                created_at  TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS kb_articles (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                title       TEXT NOT NULL,
                keywords    TEXT,
                body        TEXT NOT NULL,
                active      INTEGER NOT NULL DEFAULT 1,
                created_at  TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_tickets_client ON tickets(client_user_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_ticket_log_ticket ON ticket_log(ticket_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_kb_active ON kb_articles(active)")
        await db.commit()


# ── FSM staleness guard ─────────────────────────────────────────────────────────
# Same mechanic as templates/vehicle_service.py's FLOW_TIMEOUT_SECONDS/_flow_expired.
FLOW_TIMEOUT_SECONDS = 300

MAX_DESCRIPTION_LEN = 1500
MAX_REPLY_LEN = 2000
MAX_KB_TITLE_LEN = 120
MAX_KB_KEYWORDS_LEN = 200
MAX_KB_BODY_LEN = 2000


def _flow_expired(data: dict) -> bool:
    started_at = data.get("started_at")
    return started_at is None or (time.time() - started_at) > FLOW_TIMEOUT_SECONDS


# ── FSM states ───────────────────────────────────────────────────────────────

class TicketFlow(StatesGroup):
    description = State()   # client: free-text problem description, after category pick

class AdminReplyFlow(StatesGroup):
    text = State()           # admin: free-text reply forwarded to the client

class KbFlow(StatesGroup):
    title = State()
    keywords = State()
    body = State()

class AdminMgmtFlow(StatesGroup):
    add_admin = State()
    remove_admin_pick = State()


def _valid_admin_id(text: str) -> bool:
    """Same guard as templates/vehicle_service.py's _valid_admin_id()."""
    if not (bool(text) and text.isascii() and text.isdigit() and len(text) <= 15):
        return False
    return int(text) > 0 and str(int(text)) == text


# ── knowledge-base search ────────────────────────────────────────────────────
# Simple LIKE match against title/keywords — no need for real full-text
# search per the design brief. The dynamic SQL below only ever interpolates a
# FIXED "(lower(title) LIKE ? OR ...)" fragment repeated N times (N = number
# of search terms, capped at 8 by _kb_search_terms) — every actual VALUE
# still goes through a "?" placeholder, so this is not a SQL-injection risk
# despite the f-string.

def _kb_search_terms(text: str) -> list[str]:
    words = re.findall(r"\w{3,}", text.lower())
    seen: list[str] = []
    for w in words:
        if w not in seen:
            seen.append(w)
    return seen[:8]


async def _search_kb(db_path: str, query_text: str, limit: int = 3) -> list:
    terms = _kb_search_terms(query_text)
    if not terms:
        return []
    conditions = " OR ".join(["(lower(title) LIKE ? OR lower(COALESCE(keywords,'')) LIKE ?)"] * len(terms))
    params: list = []
    for t in terms:
        params.extend([f"%{t}%", f"%{t}%"])
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            f"SELECT id, title, body FROM kb_articles WHERE active=1 AND ({conditions}) ORDER BY id DESC LIMIT ?",
            (*params, limit),
        )).fetchall()
    return rows


# ── keyboards ─────────────────────────────────────────────────────────────────

def kb_main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎫 Тикеты", callback_data="adm_tkt_menu")],
        [InlineKeyboardButton(text="📚 База знаний", callback_data="adm_kb_menu")],
        [InlineKeyboardButton(text="👥 Админы", callback_data="adm_menu")],
    ])

def kb_client_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🆕 Создать тикет", callback_data="tkt_new")],
        [InlineKeyboardButton(text="📋 Мои тикеты", callback_data="tkt_my")],
    ])

def kb_back(callback_data: str = "main_menu") -> InlineKeyboardButton:
    return InlineKeyboardButton(text="◀️ Назад", callback_data=callback_data)

def kb_flow_cancel() -> InlineKeyboardMarkup:
    # Admin-only flows' shared cancel button — same principle as
    # vehicle_service.py's kb_flow_cancel(). Client flows use
    # kb_tkt_flow_cancel() instead since flow_cancel is admin-gated.
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="flow_cancel")],
    ])

def kb_tkt_flow_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="tkt_flow_cancel")],
    ])

MAX_LIST_BUTTONS = 25
MAX_ADMIN_REMOVE_BUTTONS = 30

# ── tickets menu (admin) ──
def kb_tickets_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Список тикетов", callback_data="adm_tkt_list")],
        [kb_back()],
    ])

_STATUS_FILTERS = [
    ("open", "🆕 Открытые"), ("in_progress", "⚙️ В работе"),
    ("waiting_response", "⏳ Ждут клиента"), ("escalated", "📞 Эскалированные"),
    ("closed", "✅ Закрытые"), ("all", "📋 Все"),
]

def kb_status_filters() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=label, callback_data=f"adm_tkt_filter:{code}")]
            for code, label in _STATUS_FILTERS]
    rows.append([kb_back("adm_tkt_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_ticket_list(rows: list[tuple]) -> InlineKeyboardMarkup:
    btns = [
        [InlineKeyboardButton(
            text=f"№{tid} · {STATUS_LABELS.get(status, status)} · {PRIORITY_LABELS.get(priority, priority)}",
            callback_data=f"adm_tkt_view:{tid}",
        )]
        for tid, status, category, priority in rows[:MAX_LIST_BUTTONS]
    ]
    btns.append([kb_back("adm_tkt_list")])
    return InlineKeyboardMarkup(inline_keyboard=btns)

def kb_admin_ticket_detail(ticket_id: int, status: str) -> InlineKeyboardMarkup:
    rows = []
    for target in STATUS_TRANSITIONS.get(status, []):
        icon = {"closed": "✅ Закрыть", "escalated": "📞 Эскалировать"}.get(
            target, f"▶️ {STATUS_LABELS.get(target, target)}"
        )
        rows.append([InlineKeyboardButton(text=icon, callback_data=f"adm_tkt_status:{ticket_id}:{target}")])
    if status != "closed":
        rows.append([InlineKeyboardButton(text="✉️ Ответить клиенту", callback_data=f"adm_tkt_reply:{ticket_id}")])
    rows.append([kb_back("adm_tkt_list")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

# ── category / ticket-creation (client) ──
def kb_category_pick() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=info["label"], callback_data=f"tkt_cat:{code}")]
            for code, info in TICKET_CATEGORIES.items()]
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="tkt_flow_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_search_results() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Это помогло, закрыть", callback_data="tkt_kb_helped")],
        [InlineKeyboardButton(text="❌ Не помогло, создать тикет", callback_data="tkt_kb_create")],
    ])

# ── my tickets (client) ──
def kb_my_ticket_list(rows: list[tuple]) -> InlineKeyboardMarkup:
    btns = [
        [InlineKeyboardButton(
            text=f"№{tid} · {STATUS_LABELS.get(status, status)} · {TICKET_CATEGORIES.get(cat, {}).get('label', cat)}",
            callback_data=f"tkt_my_view:{tid}",
        )]
        for tid, status, cat in rows[:MAX_LIST_BUTTONS]
    ]
    btns.append([InlineKeyboardButton(text="◀️ Назад", callback_data="tkt_client_menu")])
    return InlineKeyboardMarkup(inline_keyboard=btns)

def kb_client_ticket_detail(ticket_id: int, status: str, rating_set: bool = False) -> InlineKeyboardMarkup:
    rows = []
    if status in ("open", "in_progress"):
        rows.append([InlineKeyboardButton(text="📞 Эскалировать к специалисту", callback_data=f"tkt_escalate:{ticket_id}")])
    if status == "closed" and not rating_set:
        rows.append(_rating_buttons(ticket_id))
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="tkt_my")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def _rating_buttons(ticket_id: int) -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text=str(n), callback_data=f"tkt_rate:{ticket_id}:{n}") for n in range(1, 6)]

def kb_rating_only(ticket_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[_rating_buttons(ticket_id)])

# ── knowledge base (admin) ──
def kb_kb_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить статью", callback_data="adm_kb_new")],
        [InlineKeyboardButton(text="📋 Список статей", callback_data="adm_kb_list")],
        [kb_back()],
    ])

def kb_kb_list(rows: list[tuple]) -> InlineKeyboardMarkup:
    btns = [
        [InlineKeyboardButton(
            text=("🙈 " if not active else "") + _esc(title, 40),
            callback_data=f"adm_kb_view:{aid}",
        )]
        for aid, title, active in rows[:MAX_LIST_BUTTONS]
    ]
    btns.append([kb_back("adm_kb_menu")])
    return InlineKeyboardMarkup(inline_keyboard=btns)

def kb_kb_detail(article_id: int, active: bool) -> InlineKeyboardMarkup:
    toggle_text = "🙈 Скрыть" if active else "👁 Показать"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"adm_kb_edit:{article_id}")],
        [InlineKeyboardButton(text=toggle_text, callback_data=f"adm_kb_toggle:{article_id}")],
        [kb_back("adm_kb_list")],
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

async def _ticket_detail_text(db_path: str, ticket_id: int, extra_note: str | None = None) -> str | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        t = await (await db.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,))).fetchone()
        if not t:
            return None
        log_rows = await (await db.execute(
            "SELECT author, body FROM ticket_log WHERE ticket_id=? ORDER BY id", (ticket_id,)
        )).fetchall()

    # Fall back to the raw category code if the bot was reconfigured after
    # this ticket was created (TICKET_CATEGORIES no longer has that key) —
    # _esc() here is defense-in-depth since t["category"] is normally only
    # ever one of our own trusted CUSTOMIZE dict keys, never client text.
    info = TICKET_CATEGORIES.get(t["category"], {"label": _esc(t["category"])})
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sla_breached = t["status"] != "closed" and t["sla_deadline"] < now_str
    lines = [
        f"🎫 <b>Тикет №{t['id']}</b> · {STATUS_LABELS.get(t['status'], t['status'])}\n",
        f"{info.get('label', _esc(t['category']))} · {PRIORITY_LABELS.get(t['priority'], t['priority'])}",
        f"👤 {_esc(t['client_name'])}",
        f"🕐 Создан: {t['created_at']}",
    ]
    if t["status"] != "closed":
        lines.append("⚠️ SLA просрочен" if sla_breached else f"⏰ Ответить до {t['sla_deadline']}")
    if t["satisfaction_rating"] is not None:
        lines.append(f"⭐ Оценка клиента: {t['satisfaction_rating']}/5")
    lines.append("\n<b>Обращение:</b>")
    author_labels = {"client": "👤 Клиент", "admin": "🛠 Поддержка"}
    for row in log_rows:
        lines.append(f"• {author_labels.get(row['author'], row['author'])}: {_esc(row['body'], 400)}")
    if extra_note:
        lines.append(f"\n{extra_note}")
    return _join_bounded(lines)


# ── /start ────────────────────────────────────────────────────────────────────

@router.message(Command("start"), F.chat.type == "private")
async def cmd_start(message: Message, state: FSMContext, config: SupportTicketsConfig):
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
        await message.answer(CLIENT_WELCOME_TEXT, reply_markup=kb_client_menu())


@router.callback_query(F.data == "main_menu")
async def cb_main_menu(cb: CallbackQuery, state: FSMContext, config: SupportTicketsConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    await cb.message.edit_text(WELCOME_TEXT, parse_mode="HTML", reply_markup=kb_main_menu())


@router.callback_query(F.data == "tkt_client_menu")
async def cb_tkt_client_menu(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await cb.message.edit_text(CLIENT_WELCOME_TEXT, reply_markup=kb_client_menu())


@router.callback_query(F.data == "flow_cancel")
async def cb_flow_cancel(cb: CallbackQuery, state: FSMContext, config: SupportTicketsConfig):
    await cb.answer()
    await state.clear()
    if not _is_admin(cb.from_user.id, config):
        return
    await cb.message.edit_text("Отменено.", reply_markup=kb_main_menu())


@router.callback_query(F.data == "tkt_flow_cancel")
async def cb_tkt_flow_cancel(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await cb.message.edit_text("Отменено.", reply_markup=kb_client_menu())


# ── NEW TICKET flow (client): category -> description -> KB search -> create ──

@router.callback_query(F.data == "tkt_new")
async def cb_tkt_new(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await cb.message.edit_text("Выберите категорию обращения:", reply_markup=kb_category_pick())


@router.callback_query(F.data.startswith("tkt_cat:"))
async def cb_tkt_cat(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    code = cb.data.split(":", 1)[1]
    if code not in TICKET_CATEGORIES:
        return
    await state.set_state(TicketFlow.description)
    await state.update_data(started_at=time.time(), category=code)
    await cb.message.edit_text(
        f"{TICKET_CATEGORIES[code]['label']}\n\n📝 Опишите проблему подробнее:",
        reply_markup=kb_tkt_flow_cancel(),
    )


async def _finalize_ticket(
    message_answer, state: FSMContext, config: SupportTicketsConfig, bot: Bot,
    client_user_id: int, client_name: str,
) -> None:
    data = await state.get_data()
    category = data.get("category")
    description = data.get("description")
    if not category or category not in TICKET_CATEGORIES or not description:
        # Double-tap guard, same principle as vehicle_service.py's
        # cb_request_item_done: a second concurrent tap sees empty/cleared
        # data and hits this branch instead of creating a duplicate ticket.
        await state.clear()
        await message_answer("Тикет уже создан или сессия устарела.", reply_markup=kb_client_menu())
        return
    await state.clear()
    info = TICKET_CATEGORIES[category]
    sla_deadline = (datetime.now() + timedelta(hours=info["sla_hours"])).strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(config.db_path) as db:
        cur = await db.execute(
            "INSERT INTO tickets (client_user_id, client_name, category, priority, description, sla_deadline) "
            "VALUES (?,?,?,?,?,?)",
            (client_user_id, client_name, category, info["priority"], description, sla_deadline),
        )
        ticket_id = cur.lastrowid
        await db.execute(
            "INSERT INTO ticket_log (ticket_id, author, body) VALUES (?, 'client', ?)",
            (ticket_id, description),
        )
        await db.commit()

    text = await _ticket_detail_text(config.db_path, ticket_id)
    await message_answer(
        f"✅ Тикет создан!\n\n{text}", parse_mode="HTML",
        reply_markup=kb_client_ticket_detail(ticket_id, "open"),
    )
    for admin_id in _load_admins(config.admins_file):
        try:
            await bot.send_message(
                int(admin_id),
                f"🆕 <b>Новый тикет №{ticket_id}</b> · {PRIORITY_LABELS.get(info['priority'], info['priority'])}\n"
                f"{info['label']}\n👤 {_esc(client_name)}\n\n{_esc(description, 300)}",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning(f"support_tickets: failed to notify admin {admin_id} of new ticket {ticket_id}: {e}")


@router.message(TicketFlow.description, F.text, ~F.text.startswith("/"))
async def tkt_description(msg: Message, state: FSMContext, config: SupportTicketsConfig, bot: Bot):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_client_menu())
        return
    description = msg.text.strip()
    if not description:
        await msg.answer("Описание не может быть пустым. Опишите проблему:", reply_markup=kb_tkt_flow_cancel())
        return
    if len(description) > MAX_DESCRIPTION_LEN:
        await msg.answer(
            f"⚠️ Слишком длинное описание (макс {MAX_DESCRIPTION_LEN} симв.). Сократите и отправьте снова:",
            reply_markup=kb_tkt_flow_cancel(),
        )
        return
    category = data.get("category")
    if category not in TICKET_CATEGORIES:
        await state.clear()
        await msg.answer("Сессия устарела, начните заново.", reply_markup=kb_client_menu())
        return
    await state.update_data(description=description)

    matches = await _search_kb(config.db_path, f"{TICKET_CATEGORIES[category]['label']} {description}")
    if matches:
        # Waiting on a button tap next, not more text — clear the STATE but
        # keep the DATA (category/description/started_at), same pattern as
        # vehicle_service.py's _finalize_item setting state to None mid-flow.
        await state.set_state(None)
        lines = ["📚 <b>Возможно, вам поможет:</b>\n"]
        for m in matches:
            lines.append(f"<b>{_esc(m['title'])}</b>\n{_esc(m['body'], 300)}\n")
        await msg.answer(_join_bounded(lines), parse_mode="HTML", reply_markup=kb_search_results())
    else:
        await _finalize_ticket(msg.answer, state, config, bot, msg.from_user.id, msg.from_user.full_name)


@router.callback_query(F.data == "tkt_kb_helped")
async def cb_tkt_kb_helped(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_client_menu())
        return
    await state.clear()
    await cb.message.edit_text(
        "Рады, что помогли! Обращайтесь, если понадобится ещё что-то.", reply_markup=kb_client_menu(),
    )


@router.callback_query(F.data == "tkt_kb_create")
async def cb_tkt_kb_create(cb: CallbackQuery, state: FSMContext, config: SupportTicketsConfig, bot: Bot):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_client_menu())
        return
    await _finalize_ticket(cb.message.answer, state, config, bot, cb.from_user.id, cb.from_user.full_name)


# ── MY TICKETS (client) ──────────────────────────────────────────────────────

@router.callback_query(F.data == "tkt_my")
async def cb_tkt_my(cb: CallbackQuery, state: FSMContext, config: SupportTicketsConfig):
    await cb.answer()
    await state.clear()
    async with aiosqlite.connect(config.db_path) as db:
        rows = await (await db.execute(
            "SELECT id, status, category FROM tickets WHERE client_user_id=? ORDER BY id DESC LIMIT ?",
            (cb.from_user.id, MAX_LIST_BUTTONS),
        )).fetchall()
    if not rows:
        await cb.message.edit_text("У вас пока нет тикетов.", reply_markup=kb_client_menu())
        return
    await cb.message.edit_text("📋 Ваши тикеты:", reply_markup=kb_my_ticket_list(rows))


@router.callback_query(F.data.startswith("tkt_my_view:"))
async def cb_tkt_my_view(cb: CallbackQuery, config: SupportTicketsConfig):
    await cb.answer()
    try:
        ticket_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute(
            "SELECT client_user_id, status, satisfaction_rating FROM tickets WHERE id=?", (ticket_id,)
        )).fetchone()
    # Ownership check: same response for "doesn't exist" and "belongs to
    # someone else" so a client can't fish for which ticket IDs exist by
    # probing tkt_my_view:<id> with arbitrary ids.
    if not row or row[0] != cb.from_user.id:
        await cb.message.edit_text("Тикет не найден.", reply_markup=kb_client_menu())
        return
    text = await _ticket_detail_text(config.db_path, ticket_id)
    await cb.message.edit_text(
        text, parse_mode="HTML",
        reply_markup=kb_client_ticket_detail(ticket_id, row[1], rating_set=row[2] is not None),
    )


@router.callback_query(F.data.startswith("tkt_escalate:"))
async def cb_tkt_escalate(cb: CallbackQuery, bot: Bot, config: SupportTicketsConfig):
    await cb.answer()
    try:
        ticket_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute(
            "SELECT client_user_id, status FROM tickets WHERE id=?", (ticket_id,)
        )).fetchone()
        if not row or row[0] != cb.from_user.id:
            await cb.message.edit_text("Тикет не найден.", reply_markup=kb_client_menu())
            return
        old_status = row[1]
        if old_status not in ("open", "in_progress", "waiting_response"):
            text = await _ticket_detail_text(config.db_path, ticket_id)
            await cb.message.edit_text(text, parse_mode="HTML",
                                        reply_markup=kb_client_ticket_detail(ticket_id, old_status))
            return
        # Compare-and-swap: double-tap-safe, same principle as
        # vehicle_service.py's cb_req_status.
        cur = await db.execute(
            "UPDATE tickets SET status='escalated', updated_at=datetime('now','localtime') WHERE id=? AND status=?",
            (ticket_id, old_status),
        )
        if cur.rowcount == 0:
            await db.commit()
            text = await _ticket_detail_text(config.db_path, ticket_id)
            await cb.message.edit_text(text, parse_mode="HTML",
                                        reply_markup=kb_client_ticket_detail(ticket_id, "escalated"))
            return
        await db.execute(
            "INSERT INTO ticket_status_log (ticket_id, old_status, new_status, changed_by) VALUES (?,?,?,?)",
            (ticket_id, old_status, "escalated", cb.from_user.id),
        )
        await db.commit()

    text = await _ticket_detail_text(config.db_path, ticket_id, extra_note="📞 Запрос передан специалисту.")
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_client_ticket_detail(ticket_id, "escalated"))

    for admin_id in _load_admins(config.admins_file):
        try:
            await bot.send_message(
                int(admin_id),
                f"🚨 <b>Эскалация тикета №{ticket_id}</b> — клиент запросил специалиста.",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning(f"support_tickets: failed to notify admin {admin_id} of escalation on ticket {ticket_id}: {e}")


@router.callback_query(F.data.startswith("tkt_rate:"))
async def cb_tkt_rate(cb: CallbackQuery, config: SupportTicketsConfig):
    await cb.answer()
    try:
        _, ticket_id_s, score_s = cb.data.split(":", 2)
        ticket_id = int(ticket_id_s)
        score = int(score_s)
    except ValueError:
        return
    if score < 1 or score > 5:
        return
    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute(
            "SELECT client_user_id, status FROM tickets WHERE id=?", (ticket_id,)
        )).fetchone()
        if not row or row[0] != cb.from_user.id:
            await cb.message.edit_text("Тикет не найден.", reply_markup=kb_client_menu())
            return
        if row[1] != "closed":
            return
        # Compare-and-swap on "still unrated" — double-tap-safe, same
        # principle as every other status-mutating callback in this file.
        cur = await db.execute(
            "UPDATE tickets SET satisfaction_rating=? WHERE id=? AND satisfaction_rating IS NULL",
            (score, ticket_id),
        )
        await db.commit()
        rated_now = cur.rowcount > 0

    text = await _ticket_detail_text(
        config.db_path, ticket_id, extra_note="⭐ Спасибо за оценку!" if rated_now else None,
    )
    await cb.message.edit_text(text, parse_mode="HTML",
                                reply_markup=kb_client_ticket_detail(ticket_id, "closed", rating_set=True))


# ── TICKETS menu (admin) ──────────────────────────────────────────────────────

@router.callback_query(F.data == "adm_tkt_menu")
async def cb_adm_tkt_menu(cb: CallbackQuery, state: FSMContext, config: SupportTicketsConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    await cb.message.edit_text("🎫 <b>Тикеты</b>", parse_mode="HTML", reply_markup=kb_tickets_menu())


@router.callback_query(F.data == "adm_tkt_list")
async def cb_adm_tkt_list(cb: CallbackQuery, config: SupportTicketsConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await cb.message.edit_text("Выберите фильтр по статусу:", reply_markup=kb_status_filters())


@router.callback_query(F.data.startswith("adm_tkt_filter:"))
async def cb_adm_tkt_filter(cb: CallbackQuery, config: SupportTicketsConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    status = cb.data.split(":", 1)[1]
    async with aiosqlite.connect(config.db_path) as db:
        if status == "all":
            rows = await (await db.execute(
                "SELECT id, status, category, priority FROM tickets ORDER BY id DESC LIMIT ?", (MAX_LIST_BUTTONS,)
            )).fetchall()
        else:
            rows = await (await db.execute(
                "SELECT id, status, category, priority FROM tickets WHERE status=? ORDER BY id DESC LIMIT ?",
                (status, MAX_LIST_BUTTONS),
            )).fetchall()
    if not rows:
        await cb.message.edit_text("Тикетов не найдено.", reply_markup=kb_status_filters())
        return
    await cb.message.edit_text(f"📋 Тикеты (последние {len(rows)}):", reply_markup=kb_ticket_list(rows))


@router.callback_query(F.data.startswith("adm_tkt_view:"))
async def cb_adm_tkt_view(cb: CallbackQuery, config: SupportTicketsConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        ticket_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute("SELECT status FROM tickets WHERE id=?", (ticket_id,))).fetchone()
    if not row:
        await cb.message.edit_text("Тикет не найден.", reply_markup=kb_status_filters())
        return
    text = await _ticket_detail_text(config.db_path, ticket_id)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_admin_ticket_detail(ticket_id, row[0]))


@router.callback_query(F.data.startswith("adm_tkt_status:"))
async def cb_adm_tkt_status(cb: CallbackQuery, bot: Bot, config: SupportTicketsConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        _, ticket_id_s, new_status = cb.data.split(":", 2)
        ticket_id = int(ticket_id_s)
    except ValueError:
        return
    if new_status not in STATUS_LABELS:
        return

    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute(
            "SELECT status, client_user_id FROM tickets WHERE id=?", (ticket_id,)
        )).fetchone()
        if not row:
            await cb.message.edit_text("Тикет не найден.", reply_markup=kb_status_filters())
            return
        old_status, client_user_id = row
        if new_status not in STATUS_TRANSITIONS.get(old_status, []):
            # Stale button (already transitioned, or a double-tap on an
            # already-applied transition) — re-render instead of a silent
            # no-op, same principle as vehicle_service.py's cb_req_status.
            text = await _ticket_detail_text(config.db_path, ticket_id)
            await cb.message.edit_text(text, parse_mode="HTML",
                                        reply_markup=kb_admin_ticket_detail(ticket_id, old_status))
            return
        # Compare-and-swap: WHERE status=old_status makes a double-tap a
        # no-op on the second write instead of double-logging/notifying.
        cur = await db.execute(
            "UPDATE tickets SET status=?, updated_at=datetime('now','localtime') WHERE id=? AND status=?",
            (new_status, ticket_id, old_status),
        )
        if cur.rowcount == 0:
            await db.commit()
            text = await _ticket_detail_text(config.db_path, ticket_id)
            await cb.message.edit_text(text, parse_mode="HTML",
                                        reply_markup=kb_admin_ticket_detail(ticket_id, new_status))
            return
        await db.execute(
            "INSERT INTO ticket_status_log (ticket_id, old_status, new_status, changed_by) VALUES (?,?,?,?)",
            (ticket_id, old_status, new_status, cb.from_user.id),
        )
        await db.commit()

    note = None
    notify_text = STATUS_NOTIFY_TEXT.get(new_status)
    if notify_text:
        try:
            await bot.send_message(client_user_id, notify_text.format(ticket_id=ticket_id))
            note = "🔔 Клиент уведомлён."
        except TelegramAPIError as e:
            logger.warning(f"support_tickets: failed to notify client for ticket {ticket_id}: {e}")
            note = "⚠️ Не удалось уведомить клиента (возможно, заблокировал бота)."
        async with aiosqlite.connect(config.db_path) as db:
            await db.execute(
                "UPDATE ticket_status_log SET notified=? WHERE ticket_id=? AND new_status=? "
                "AND id=(SELECT MAX(id) FROM ticket_status_log WHERE ticket_id=? AND new_status=?)",
                (1 if note == "🔔 Клиент уведомлён." else 0, ticket_id, new_status, ticket_id, new_status),
            )
            await db.commit()

    if new_status == "closed":
        # Ask for a satisfaction rating as a SEPARATE message with its own
        # inline buttons — deliberately not merged into notify_text above
        # since it carries a keyboard, not just plain text.
        try:
            await bot.send_message(
                client_user_id,
                f"⭐ Оцените, пожалуйста, качество поддержки по тикету №{ticket_id}:",
                reply_markup=kb_rating_only(ticket_id),
            )
        except TelegramAPIError as e:
            logger.warning(f"support_tickets: failed to send rating prompt for ticket {ticket_id}: {e}")

    text = await _ticket_detail_text(config.db_path, ticket_id, extra_note=note)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_admin_ticket_detail(ticket_id, new_status))


@router.callback_query(F.data.startswith("adm_tkt_reply:"))
async def cb_adm_tkt_reply(cb: CallbackQuery, state: FSMContext, config: SupportTicketsConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        ticket_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute("SELECT status FROM tickets WHERE id=?", (ticket_id,))).fetchone()
    if not row:
        await cb.message.edit_text("Тикет не найден.", reply_markup=kb_status_filters())
        return
    if row[0] == "closed":
        text = await _ticket_detail_text(config.db_path, ticket_id)
        await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_admin_ticket_detail(ticket_id, "closed"))
        return
    await state.clear()
    await state.set_state(AdminReplyFlow.text)
    await state.update_data(started_at=time.time(), ticket_id=ticket_id)
    await cb.message.edit_text("✉️ Введите ответ клиенту:", reply_markup=kb_flow_cancel())


@router.message(AdminReplyFlow.text, F.text, ~F.text.startswith("/"))
async def admin_reply_text(msg: Message, state: FSMContext, bot: Bot, config: SupportTicketsConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    ticket_id = data.get("ticket_id")
    reply_text = msg.text.strip()
    if not reply_text:
        await msg.answer("Ответ не может быть пустым. Введите текст ответа:", reply_markup=kb_flow_cancel())
        return
    if len(reply_text) > MAX_REPLY_LEN:
        await msg.answer(f"⚠️ Слишком длинный ответ (макс {MAX_REPLY_LEN} симв.).", reply_markup=kb_flow_cancel())
        return
    if not ticket_id:
        await state.clear()
        await msg.answer("Сессия устарела, начните заново.", reply_markup=kb_main_menu())
        return
    await state.clear()

    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute(
            "SELECT client_user_id, status FROM tickets WHERE id=?", (ticket_id,)
        )).fetchone()
        if not row:
            await msg.answer("Тикет не найден.", reply_markup=kb_status_filters())
            return
        client_user_id, status = row
        await db.execute(
            "INSERT INTO ticket_log (ticket_id, author, body) VALUES (?, 'admin', ?)",
            (ticket_id, reply_text),
        )
        await db.commit()

    note = None
    try:
        await bot.send_message(
            client_user_id,
            f"✉️ <b>Ответ по тикету №{ticket_id}:</b>\n\n{_esc(reply_text)}",
            parse_mode="HTML",
        )
        note = "🔔 Ответ отправлен клиенту."
    except TelegramAPIError as e:
        logger.warning(f"support_tickets: failed to deliver admin reply for ticket {ticket_id}: {e}")
        note = "⚠️ Не удалось доставить ответ клиенту (возможно, заблокировал бота)."

    text = await _ticket_detail_text(config.db_path, ticket_id, extra_note=note)
    await msg.answer(text, parse_mode="HTML", reply_markup=kb_admin_ticket_detail(ticket_id, status))


# ── KNOWLEDGE BASE (admin) ────────────────────────────────────────────────────

async def _kb_detail_text_and_kb(db_path: str, article_id: int):
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute("SELECT * FROM kb_articles WHERE id=?", (article_id,))).fetchone()
    if not row:
        return None, None
    status = "👁 Видна клиентам" if row["active"] else "🙈 Скрыта"
    text = (
        f"📚 <b>{_esc(row['title'])}</b> · {status}\n\n"
        f"{_esc(row['body'], 1000)}\n\n"
        f"🔑 Ключевые слова: {_esc(row['keywords']) if row['keywords'] else '—'}"
    )
    return text, bool(row["active"])


@router.callback_query(F.data == "adm_kb_menu")
async def cb_adm_kb_menu(cb: CallbackQuery, state: FSMContext, config: SupportTicketsConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    await cb.message.edit_text("📚 <b>База знаний</b>", parse_mode="HTML", reply_markup=kb_kb_menu())


@router.callback_query(F.data == "adm_kb_list")
async def cb_adm_kb_list(cb: CallbackQuery, config: SupportTicketsConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    async with aiosqlite.connect(config.db_path) as db:
        rows = await (await db.execute(
            "SELECT id, title, active FROM kb_articles ORDER BY id DESC LIMIT ?", (MAX_LIST_BUTTONS,)
        )).fetchall()
    if not rows:
        await cb.message.edit_text("Статей пока нет.", reply_markup=kb_kb_menu())
        return
    await cb.message.edit_text("📋 Статьи базы знаний:", reply_markup=kb_kb_list(rows))


@router.callback_query(F.data.startswith("adm_kb_view:"))
async def cb_adm_kb_view(cb: CallbackQuery, config: SupportTicketsConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        article_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    text, active = await _kb_detail_text_and_kb(config.db_path, article_id)
    if text is None:
        await cb.message.edit_text("Статья не найдена.", reply_markup=kb_kb_menu())
        return
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_kb_detail(article_id, active))


@router.callback_query(F.data.startswith("adm_kb_toggle:"))
async def cb_adm_kb_toggle(cb: CallbackQuery, config: SupportTicketsConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        article_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute("SELECT active FROM kb_articles WHERE id=?", (article_id,))).fetchone()
        if not row:
            await cb.message.edit_text("Статья не найдена.", reply_markup=kb_kb_menu())
            return
        new_active = 0 if row[0] else 1
        await db.execute("UPDATE kb_articles SET active=? WHERE id=?", (new_active, article_id))
        await db.commit()
    text, active = await _kb_detail_text_and_kb(config.db_path, article_id)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_kb_detail(article_id, active))


@router.callback_query(F.data == "adm_kb_new")
async def cb_adm_kb_new(cb: CallbackQuery, state: FSMContext, config: SupportTicketsConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    await state.set_state(KbFlow.title)
    await state.update_data(started_at=time.time())
    await cb.message.edit_text("📝 Введите заголовок статьи:", reply_markup=kb_flow_cancel())


@router.callback_query(F.data.startswith("adm_kb_edit:"))
async def cb_adm_kb_edit(cb: CallbackQuery, state: FSMContext, config: SupportTicketsConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        article_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute("SELECT id FROM kb_articles WHERE id=?", (article_id,))).fetchone()
    if not row:
        await cb.message.edit_text("Статья не найдена.", reply_markup=kb_kb_menu())
        return
    await state.clear()
    await state.set_state(KbFlow.title)
    await state.update_data(started_at=time.time(), kb_edit_id=article_id)
    await cb.message.edit_text("📝 Введите новый заголовок статьи:", reply_markup=kb_flow_cancel())


@router.message(KbFlow.title, F.text, ~F.text.startswith("/"))
async def kb_flow_title(msg: Message, state: FSMContext):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    title = msg.text.strip()
    if not title:
        await msg.answer("Заголовок не может быть пустым. Введите заголовок статьи:", reply_markup=kb_flow_cancel())
        return
    if len(title) > MAX_KB_TITLE_LEN:
        await msg.answer(f"⚠️ Слишком длинный заголовок (макс {MAX_KB_TITLE_LEN} симв.).", reply_markup=kb_flow_cancel())
        return
    await state.update_data(pending_title=title)
    await state.set_state(KbFlow.keywords)
    await msg.answer("🔑 Ключевые слова через пробел (или «-», чтобы пропустить):", reply_markup=kb_flow_cancel())


@router.message(KbFlow.keywords, F.text, ~F.text.startswith("/"))
async def kb_flow_keywords(msg: Message, state: FSMContext):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    raw = msg.text.strip()
    if len(raw) > MAX_KB_KEYWORDS_LEN:
        await msg.answer(
            f"⚠️ Слишком длинный список ключевых слов (макс {MAX_KB_KEYWORDS_LEN} симв.).",
            reply_markup=kb_flow_cancel(),
        )
        return
    keywords = None if raw == "-" else raw
    await state.update_data(pending_keywords=keywords)
    await state.set_state(KbFlow.body)
    await msg.answer("📄 Введите текст ответа (тело статьи):", reply_markup=kb_flow_cancel())


@router.message(KbFlow.body, F.text, ~F.text.startswith("/"))
async def kb_flow_body(msg: Message, state: FSMContext, config: SupportTicketsConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    body = msg.text.strip()
    if not body:
        await msg.answer("Текст ответа не может быть пустым. Введите текст:", reply_markup=kb_flow_cancel())
        return
    if len(body) > MAX_KB_BODY_LEN:
        await msg.answer(f"⚠️ Слишком длинный текст (макс {MAX_KB_BODY_LEN} симв.).", reply_markup=kb_flow_cancel())
        return
    title = data.get("pending_title")
    if not title:
        await state.clear()
        await msg.answer("Сессия устарела, начните заново.", reply_markup=kb_main_menu())
        return
    keywords = data.get("pending_keywords")
    edit_id = data.get("kb_edit_id")
    await state.clear()

    async with aiosqlite.connect(config.db_path) as db:
        if edit_id:
            cur = await db.execute(
                "UPDATE kb_articles SET title=?, keywords=?, body=? WHERE id=?",
                (title, keywords, body, edit_id),
            )
            if cur.rowcount == 0:
                await db.commit()
                await msg.answer("⚠️ Статья была удалена во время редактирования.", reply_markup=kb_kb_menu())
                return
        else:
            await db.execute(
                "INSERT INTO kb_articles (title, keywords, body) VALUES (?,?,?)",
                (title, keywords, body),
            )
        await db.commit()
    await msg.answer(f"✅ Статья сохранена: {_esc(title)}", parse_mode="HTML", reply_markup=kb_kb_menu())


# ── ADMINS menu ────────────────────────────────────────────────────────────────

async def _admins_list_text(config: SupportTicketsConfig) -> str:
    ids = sorted(_load_admins(config.admins_file))
    if not ids:
        return "👥 Пусто"
    return _join_bounded(["👥 <b>Администраторы бота:</b>\n"] + [f"• <code>{_esc(i)}</code>" for i in ids])


@router.callback_query(F.data == "adm_menu")
async def cb_adm_menu(cb: CallbackQuery, state: FSMContext, config: SupportTicketsConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    text = await _admins_list_text(config)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_admins_menu())


@router.callback_query(F.data == "adm_add")
async def cb_adm_add(cb: CallbackQuery, state: FSMContext, config: SupportTicketsConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.set_state(AdminMgmtFlow.add_admin)
    await state.update_data(started_at=time.time())
    await cb.message.edit_text("Введите Telegram ID нового администратора:", reply_markup=kb_flow_cancel())


@router.message(AdminMgmtFlow.add_admin, F.text, ~F.text.startswith("/"))
async def admin_add_id(msg: Message, state: FSMContext, config: SupportTicketsConfig):
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
async def cb_adm_remove(cb: CallbackQuery, state: FSMContext, config: SupportTicketsConfig):
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
async def cb_adm_remove_pick(cb: CallbackQuery, state: FSMContext, config: SupportTicketsConfig):
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
