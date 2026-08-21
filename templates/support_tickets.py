# TEMPLATE: support_tickets
# USE FOR: тикет-система поддержки клиентов — база знаний для самообслуживания перед созданием тикета, категории с эскалацией к живому агенту, статус-флоу нового тикета (новый → отвечен → эскалирован → закрыт), очередь для админа по возрасту заявки (SLA), оценка удовлетворённости клиентом после закрытия тикета
# CUSTOMIZE: sections marked with # CUSTOMIZE
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

import aiosqlite
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
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
BOT_DESCRIPTION = (
    "Тикет-система поддержки: база знаний для самостоятельного поиска ответа, "
    "создание тикета с категорией, статус-флоу и эскалация к живому агенту, "
    "SLA-очередь по возрасту заявки для админа, оценка удовлетворённости после закрытия."
)
WELCOME_TEXT = (
    "🎫 <b>Поддержка клиентов</b>\n\n"
    "Тикеты со статус-флоу: 🆕 новый → 💬 отвечен → 🔺 эскалирован → ✅ закрыт. "
    "База знаний для самообслуживания клиентов, очередь по возрасту заявки, "
    "оценка после закрытия.\n\nВыберите действие:"
)
CLIENT_WELCOME_TEXT = "👋 Здравствуйте! Это бот поддержки.\n\nВыберите действие:"

STATUS_LABELS = {
    "new": "🆕 Новый",
    "answered": "💬 Отвечен",
    "escalated": "🔺 Эскалирован",
    "closed": "✅ Закрыт",
}

# Ticket/KB-article category codes shown as buttons on both the client's
# self-service search and the admin's knowledge-base editor — kept as short
# ascii codes (not the label text) so callback_data stays stable even if the
# Russian label wording is tweaked later.
TICKET_CATEGORIES = [
    ("billing", "💳 Оплата и биллинг"),
    ("technical", "🔧 Технические проблемы"),
    ("account", "👤 Аккаунт и доступ"),
    ("other", "❓ Другое"),
]
TICKET_CATEGORY_LABELS = dict(TICKET_CATEGORIES)

# Sent to the CLIENT when an admin replies (status -> 'answered').
NEW_REPLY_NOTIFY_TEXT = "💬 Новый ответ по вашему тикету №{ticket_id} ({category}):\n\n{text}"
# Sent to the CLIENT immediately when their ticket is closed — the bot itself
# asks for the 1-5 rating, per the design brief.
RATING_PROMPT_TEXT = "✅ Ваш тикет №{ticket_id} закрыт. Оцените, пожалуйста, качество поддержки:"
# Sent to every ADMIN when a client creates a new ticket or follows up on an
# open one — an SLA-conscious queue is useless if admins have to keep
# refreshing "Активные тикеты" to notice new activity.
NEW_TICKET_ADMIN_NOTIFY = "🆘 Новый тикет №{ticket_id} от {client_name}\nКатегория: {category}\n\n{text}"
FOLLOWUP_ADMIN_NOTIFY = "💬 Новое сообщение по тикету №{ticket_id} от {client_name}:\n\n{text}"
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
            "name": "tickets",
            "table": "tickets",
            "order_by": "created_at DESC",
            "creatable": True,
            "scope": {"type": "global"},
            "title": "Обращения",
            "titleField": "category",
            "fields": [
                {"name": "client_name", "label": "Клиент", "kind": "text", "list": True, "detail": True, "create": True},
                {"name": "category", "required": True, "label": "Категория", "kind": "text", "list": True, "detail": True, "create": True},
                {"name": "status", "label": "Статус", "kind": "status", "list": True, "detail": True, "create": False},
                {"name": "satisfaction_rating", "label": "Оценка", "kind": "number", "list": False, "detail": True, "create": False},
                {"name": "created_at", "label": "Создано", "kind": "date", "list": False, "detail": True, "create": False},
            ],
        },
        {
            "name": "kb_articles",
            "table": "kb_articles",
            "order_by": "id DESC",
            "creatable": True,
            "scope": {"type": "global"},
            "title": "База знаний",
            "titleField": "question",
            "fields": [
                {"name": "category", "required": True, "label": "Категория", "kind": "text", "list": True, "detail": True, "create": True},
                {"name": "question", "required": True, "label": "Вопрос", "kind": "text", "list": True, "detail": True, "create": True},
                {"name": "answer", "required": True, "label": "Ответ", "kind": "text", "list": False, "detail": True, "create": True},
                {"name": "active", "label": "Активна", "kind": "bool", "list": False, "detail": True, "create": True},
            ],
        },
    ],
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
    bot_id: int | None = None
    owner_telegram_id: int | None = None


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
    config.bot_id = bot_id
    config.owner_telegram_id = bot_row.get("owner_telegram_id")
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
    # The DB-known owner (bots.owner_telegram_id) is always an admin, even if
    # the local admins_file is empty/stale/hijacked — see cmd_start below for
    # why the file alone can't be trusted as the sole source of truth.
    if config.owner_telegram_id is not None and str(user_id) == str(config.owner_telegram_id):
        return True
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


def _cap_html(text: str, limit: int = 3900) -> str:
    """Final length guard applied to an ALREADY html.escape()-d, fully
    assembled message — right before it goes into edit_text/answer. Bounding
    only the raw pre-escape field (as _esc()'s max_len does) is not enough:
    html.escape() can expand a string up to ~6x (each '&'/'<'/'>'/'"' becomes
    a multi-char entity), so a field that passed its _esc() bound can still
    push the assembled message over Telegram's ~4096-char cap and make
    edit_text raise uncaught. Every caller here only wraps a short, fixed-size
    prefix (category label, "❓ <b>question</b>") in tags before this cap is
    applied, so a truncation always lands in trailing plain-escaped text, not
    mid-tag."""
    if len(text) > limit:
        return text[:limit] + "…"
    return text


async def _safe_edit_text(message: Message, text: str, **kwargs) -> None:
    """edit_text wrapper that swallows Telegram's "message is not modified"
    error — a real outcome (not a bug) when a CAS-guarded action is a no-op
    (e.g. a stale double-tap on an already-escalated/already-closed ticket
    re-renders byte-identical content) and would otherwise raise uncaught."""
    try:
        await message.edit_text(text, **kwargs)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            raise
        logger.debug(f"support_tickets: ignored no-op edit_text: {e}")


async def _notify_admins(bot: Bot, config: SupportTicketsConfig, text: str) -> None:
    """Best-effort broadcast to every registered admin — a blocked/deleted
    admin account must never break notifying the others, same
    try/except-per-recipient rationale as event_manager.py's broadcast loop."""
    for admin_id in _load_admins(config.admins_file):
        try:
            await bot.send_message(int(admin_id), text, parse_mode="HTML")
        except (TelegramAPIError, ValueError) as e:
            logger.warning(f"support_tickets: failed to notify admin {admin_id}: {e}")


# ── input length bounds ─────────────────────────────────────────────────────
MAX_PROBLEM_LEN = 2000
MAX_REPLY_LEN = 2000
MAX_QUESTION_LEN = 300
MAX_ANSWER_LEN = 2000


def _valid_text(text: str, max_len: int) -> str | None:
    text = text.strip()
    if not text or len(text) > max_len:
        return None
    return text


def _valid_admin_id(text: str) -> bool:
    """Same guard as templates/vehicle_service.py's _valid_admin_id()."""
    if not (bool(text) and text.isascii() and text.isdigit() and len(text) <= 15):
        return False
    return int(text) > 0 and str(int(text)) == text


def _valid_rating(text: str) -> int | None:
    try:
        score = int(text.strip())
    except ValueError:
        return None
    return score if 1 <= score <= 5 else None


# ── db ────────────────────────────────────────────────────────────────────────

async def init_db(db_path: str):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS kb_articles (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                category  TEXT NOT NULL,
                question  TEXT NOT NULL,
                answer    TEXT NOT NULL,
                active    INTEGER NOT NULL DEFAULT 1
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tickets (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                client_user_id        INTEGER NOT NULL,
                client_name           TEXT,
                category              TEXT NOT NULL,
                status                TEXT NOT NULL DEFAULT 'new'
                                      CHECK(status IN ('new','answered','escalated','closed')),
                satisfaction_rating   INTEGER,
                created_at            TEXT DEFAULT (datetime('now','localtime')),
                updated_at            TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS ticket_messages (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id   INTEGER NOT NULL REFERENCES tickets(id),
                sender      TEXT NOT NULL CHECK(sender IN ('client','admin')),
                text        TEXT NOT NULL,
                created_at  TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_tickets_client ON tickets(client_user_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_messages_ticket ON ticket_messages(ticket_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_kb_category_active ON kb_articles(category, active)")
        await db.commit()


# ── FSM staleness guard ─────────────────────────────────────────────────────────
# Same mechanic as templates/vehicle_service.py's FLOW_TIMEOUT_SECONDS/_flow_expired.
FLOW_TIMEOUT_SECONDS = 300


def _flow_expired(data: dict) -> bool:
    started_at = data.get("started_at")
    return started_at is None or (time.time() - started_at) > FLOW_TIMEOUT_SECONDS


# ── FSM states ───────────────────────────────────────────────────────────────

class ClientTicketFlow(StatesGroup):
    problem_text = State()   # "Это не помогло" -> free-text problem description
    reply_text = State()     # client follow-up message on an open ticket

class AdminReplyFlow(StatesGroup):
    reply_text = State()

class KBFlow(StatesGroup):
    # Shared by BOTH "add article" and "edit article" — the answer-step
    # handler branches on whether state data carries an edit_id (UPDATE) or
    # not (INSERT). Keeps the FSM surface small instead of duplicating a
    # near-identical 2-step chain twice.
    question = State()
    answer = State()

class AdminMgmtFlow(StatesGroup):
    add_admin = State()
    remove_admin_pick = State()


# ── keyboards ─────────────────────────────────────────────────────────────────

MAX_LIST_BUTTONS = 25
MAX_ADMIN_REMOVE_BUTTONS = 30

def kb_back(callback_data: str = "main_menu") -> InlineKeyboardButton:
    return InlineKeyboardButton(text="◀️ Назад", callback_data=callback_data)

def kb_flow_cancel(back_to: str = "flow_cancel") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data=back_to)]])

# ── admin main menu ──
def kb_main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎫 Тикеты", callback_data="tk_menu")],
        [InlineKeyboardButton(text="📚 База знаний", callback_data="kb_menu")],
        [InlineKeyboardButton(text="👥 Админы", callback_data="adm_menu")],
    ])

# ── admin tickets ──
def kb_tickets_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎫 Активные (по возрасту)", callback_data="tk_filter:active")],
        [InlineKeyboardButton(text="📁 Закрытые", callback_data="tk_filter:closed")],
        [InlineKeyboardButton(text="📋 Все", callback_data="tk_filter:all")],
        [kb_back()],
    ])

def kb_ticket_list(rows: list[tuple]) -> InlineKeyboardMarkup:
    btns = [
        [InlineKeyboardButton(
            text=f"№{tid} · {STATUS_LABELS.get(status, status)} · {TICKET_CATEGORY_LABELS.get(cat, cat)}",
            callback_data=f"tk_view:{tid}",
        )]
        for tid, status, cat in rows[:MAX_LIST_BUTTONS]
    ]
    btns.append([kb_back("tk_menu")])
    return InlineKeyboardMarkup(inline_keyboard=btns)

def kb_ticket_admin_detail(ticket_id: int, status: str) -> InlineKeyboardMarkup:
    rows = []
    if status != "closed":
        rows.append([InlineKeyboardButton(text="✍️ Ответить", callback_data=f"tk_reply:{ticket_id}")])
        if status != "escalated":
            rows.append([InlineKeyboardButton(text="🔺 Эскалировать", callback_data=f"tk_escalate:{ticket_id}")])
        rows.append([InlineKeyboardButton(text="✅ Закрыть", callback_data=f"tk_close:{ticket_id}")])
    rows.append([kb_back("tk_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

# ── admin knowledge base ──
def kb_kb_menu() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=label, callback_data=f"kb_cat:{code}")] for code, label in TICKET_CATEGORIES]
    rows.append([InlineKeyboardButton(text="➕ Добавить статью", callback_data="kb_add")])
    rows.append([kb_back()])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_category_pick(prefix: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=label, callback_data=f"{prefix}:{code}")] for code, label in TICKET_CATEGORIES]
    rows.append([kb_back("kb_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_kb_article_list(rows: list[tuple], category: str) -> InlineKeyboardMarkup:
    """rows are (id, question, active) — admin-side browse deliberately
    includes hidden (active=0) articles, marked with a 🗑 prefix, so a
    soft-deleted article stays reachable for editing (see cb_kb_hide's
    comment) instead of vanishing from every admin list."""
    btns = [
        [InlineKeyboardButton(
            text=("🗑 " if not active else "") + _esc(question, 40), callback_data=f"kb_view:{aid}",
        )]
        for aid, question, active in rows[:MAX_LIST_BUTTONS]
    ]
    btns.append([InlineKeyboardButton(text="➕ Добавить статью", callback_data=f"kb_add_cat:{category}")])
    btns.append([kb_back("kb_menu")])
    return InlineKeyboardMarkup(inline_keyboard=btns)

def kb_kb_article_detail(article_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"kb_edit:{article_id}")],
        [InlineKeyboardButton(text="🗑 Скрыть", callback_data=f"kb_hide:{article_id}")],
        [kb_back("kb_menu")],
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

# ── client-side ──
def kb_client_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🆘 Поддержка", callback_data="cli_support")],
        [InlineKeyboardButton(text="📋 Мои тикеты", callback_data="cli_tickets")],
    ])

def kb_client_categories() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=label, callback_data=f"cli_cat:{code}")] for code, label in TICKET_CATEGORIES]
    rows.append([kb_back("cli_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_client_kb_list(rows: list[tuple], category: str) -> InlineKeyboardMarkup:
    btns = [[InlineKeyboardButton(text=_esc(question, 40), callback_data=f"cli_kb_view:{aid}")] for aid, question in rows[:MAX_LIST_BUTTONS]]
    btns.append([InlineKeyboardButton(text="🙁 Это не помогло", callback_data=f"cli_new_ticket:{category}")])
    btns.append([kb_back("cli_support")])
    return InlineKeyboardMarkup(inline_keyboard=btns)

def kb_client_kb_article(category: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🙁 Это не помогло", callback_data=f"cli_new_ticket:{category}")],
        [kb_back(f"cli_cat:{category}")],
    ])

def kb_client_ticket_list(rows: list[tuple]) -> InlineKeyboardMarkup:
    btns = [
        [InlineKeyboardButton(
            text=f"№{tid} · {STATUS_LABELS.get(status, status)} · {TICKET_CATEGORY_LABELS.get(cat, cat)}",
            callback_data=f"cli_ticket_view:{tid}",
        )]
        for tid, status, cat in rows[:MAX_LIST_BUTTONS]
    ]
    btns.append([kb_back("cli_main")])
    return InlineKeyboardMarkup(inline_keyboard=btns)

def kb_client_ticket_detail(ticket_id: int, status: str, needs_rating: bool) -> InlineKeyboardMarkup:
    rows = []
    if status != "closed":
        rows.append([InlineKeyboardButton(text="✍️ Ответить", callback_data=f"cli_ticket_reply:{ticket_id}")])
    if needs_rating:
        rows.append(kb_rating_buttons_row(ticket_id))
    rows.append([kb_back("cli_tickets")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_rating_buttons_row(ticket_id: int) -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text=str(n), callback_data=f"sup_rate:{ticket_id}:{n}") for n in range(1, 6)]

def kb_rating_prompt(ticket_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[kb_rating_buttons_row(ticket_id)])


# ── rendering helpers ────────────────────────────────────────────────────────

async def _ticket_detail_text(db_path: str, ticket_id: int) -> tuple[str, dict] | tuple[None, None]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        ticket = await (await db.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,))).fetchone()
        if not ticket:
            return None, None
        messages = await (await db.execute(
            "SELECT sender, text, created_at FROM ticket_messages WHERE ticket_id=? ORDER BY id", (ticket_id,)
        )).fetchall()

    ticket = dict(ticket)
    lines = [
        f"🎫 <b>Тикет №{ticket['id']}</b> · {STATUS_LABELS.get(ticket['status'], ticket['status'])}\n",
        f"🏷 {TICKET_CATEGORY_LABELS.get(ticket['category'], ticket['category'])}",
        f"👤 {_esc(ticket['client_name'])}",
        f"🕐 Создан: {ticket['created_at']}\n",
        "<b>Переписка:</b>",
    ]
    for m in messages:
        who = "🙋 Клиент" if m["sender"] == "client" else "🛟 Поддержка"
        lines.append(f"{who} ({m['created_at']}):\n{_esc(m['text'], 1000)}")
    if ticket["satisfaction_rating"]:
        lines.append(f"\n⭐ Оценка клиента: {ticket['satisfaction_rating']}/5")
    return _join_bounded(lines), ticket


# ── /start ────────────────────────────────────────────────────────────────────

@router.message(Command("start"), F.chat.type == "private")
async def cmd_start(message: Message, state: FSMContext, config: SupportTicketsConfig):
    # Same reasoning as vehicle_service.py's cmd_start: /start must reset any
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
        # No phone/Contact-linking needed here (unlike vehicle_service.py) —
        # a ticket's client identity IS the Telegram user id directly, so any
        # first-time visitor can go straight into the support menu.
        await message.answer(CLIENT_WELCOME_TEXT, reply_markup=kb_client_menu())


@router.callback_query(F.data == "main_menu")
async def cb_main_menu(cb: CallbackQuery, state: FSMContext, config: SupportTicketsConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    await cb.message.edit_text(WELCOME_TEXT, parse_mode="HTML", reply_markup=kb_main_menu())


@router.callback_query(F.data == "cli_main")
async def cb_client_main(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await cb.message.edit_text(CLIENT_WELCOME_TEXT, reply_markup=kb_client_menu())


@router.callback_query(F.data == "flow_cancel")
async def cb_flow_cancel(cb: CallbackQuery, state: FSMContext, config: SupportTicketsConfig):
    await cb.answer()
    await state.clear()
    if _is_admin(cb.from_user.id, config):
        await cb.message.edit_text("Отменено.", reply_markup=kb_main_menu())
    else:
        await cb.message.edit_text("Отменено.", reply_markup=kb_client_menu())


# ── ADMIN: tickets ───────────────────────────────────────────────────────────

@router.callback_query(F.data == "tk_menu")
async def cb_tk_menu(cb: CallbackQuery, state: FSMContext, config: SupportTicketsConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    await cb.message.edit_text("🎫 <b>Тикеты</b>", parse_mode="HTML", reply_markup=kb_tickets_menu())


@router.callback_query(F.data.startswith("tk_filter:"))
async def cb_tk_filter(cb: CallbackQuery, state: FSMContext, config: SupportTicketsConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    filter_code = cb.data.split(":", 1)[1]
    async with aiosqlite.connect(config.db_path) as db:
        if filter_code == "active":
            # Oldest first — an SLA-conscious queue surfaces the
            # longest-waiting ticket at the top, not the newest.
            rows = await (await db.execute(
                "SELECT id, status, category FROM tickets WHERE status != 'closed' "
                "ORDER BY created_at ASC, id ASC LIMIT ?", (MAX_LIST_BUTTONS,)
            )).fetchall()
        elif filter_code == "closed":
            rows = await (await db.execute(
                "SELECT id, status, category FROM tickets WHERE status = 'closed' "
                "ORDER BY id DESC LIMIT ?", (MAX_LIST_BUTTONS,)
            )).fetchall()
        else:
            rows = await (await db.execute(
                "SELECT id, status, category FROM tickets ORDER BY id DESC LIMIT ?", (MAX_LIST_BUTTONS,)
            )).fetchall()
    if not rows:
        await cb.message.edit_text("Тикетов не найдено.", reply_markup=kb_tickets_menu())
        return
    await cb.message.edit_text(f"🎫 Тикеты (последние {len(rows)}):", reply_markup=kb_ticket_list(rows))


@router.callback_query(F.data.startswith("tk_view:"))
async def cb_tk_view(cb: CallbackQuery, state: FSMContext, config: SupportTicketsConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    try:
        ticket_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    text, ticket = await _ticket_detail_text(config.db_path, ticket_id)
    if text is None:
        await cb.message.edit_text("Тикет не найден.", reply_markup=kb_tickets_menu())
        return
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_ticket_admin_detail(ticket_id, ticket["status"]))


@router.callback_query(F.data.startswith("tk_reply:"))
async def cb_tk_reply(cb: CallbackQuery, state: FSMContext, config: SupportTicketsConfig):
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
        await cb.message.edit_text("Тикет не найден.", reply_markup=kb_tickets_menu())
        return
    if row[0] == "closed":
        text, ticket = await _ticket_detail_text(config.db_path, ticket_id)
        await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_ticket_admin_detail(ticket_id, ticket["status"]))
        return
    await state.clear()
    await state.set_state(AdminReplyFlow.reply_text)
    await state.update_data(started_at=time.time(), ticket_id=ticket_id)
    await cb.message.edit_text("✍️ Введите ответ клиенту:", reply_markup=kb_flow_cancel())


@router.message(AdminReplyFlow.reply_text, F.text, ~F.text.startswith("/"))
async def admin_reply_text(msg: Message, state: FSMContext, bot: Bot, config: SupportTicketsConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    text = _valid_text(msg.text, MAX_REPLY_LEN)
    if text is None:
        await msg.answer(f"Ответ не может быть пустым и должен быть короче {MAX_REPLY_LEN} символов.",
                          reply_markup=kb_flow_cancel())
        return
    ticket_id = data.get("ticket_id")
    await state.clear()

    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute("SELECT status FROM tickets WHERE id=?", (ticket_id,))).fetchone()
        if not row:
            await msg.answer("Тикет не найден.", reply_markup=kb_main_menu())
            return
        old_status = row[0]
        if old_status == "closed":
            await msg.answer("⚠️ Тикет уже закрыт, ответ не отправлен.", reply_markup=kb_tickets_menu())
            return
        # An escalated ticket stays escalated on a plain reply — a reply must
        # not silently erase the escalation signal another admin set. Only
        # new/answered move to 'answered'; escalated -> escalated (self-loop,
        # message still recorded, client still notified).
        new_status = "answered" if old_status != "escalated" else "escalated"
        # Compare-and-swap, same double-tap-safety principle as
        # vehicle_service.py's cb_req_status.
        cur = await db.execute(
            "UPDATE tickets SET status=?, updated_at=datetime('now','localtime') WHERE id=? AND status=?",
            (new_status, ticket_id, old_status),
        )
        if cur.rowcount == 0:
            await db.commit()
            await msg.answer("⚠️ Тикет уже изменён другим действием, ответ не отправлен.", reply_markup=kb_tickets_menu())
            return
        await db.execute(
            "INSERT INTO ticket_messages (ticket_id, sender, text) VALUES (?, 'admin', ?)", (ticket_id, text)
        )
        client_row = await (await db.execute(
            "SELECT client_user_id, category FROM tickets WHERE id=?", (ticket_id,)
        )).fetchone()
        await db.commit()

    note = None
    if client_row:
        client_user_id, category = client_row
        try:
            await bot.send_message(
                client_user_id,
                NEW_REPLY_NOTIFY_TEXT.format(
                    ticket_id=ticket_id, category=TICKET_CATEGORY_LABELS.get(category, category), text=_esc(text, 1000)
                ),
                parse_mode="HTML",
            )
            note = "🔔 Клиент уведомлён."
        except TelegramAPIError as e:
            logger.warning(f"support_tickets: failed to notify client for ticket {ticket_id}: {e}")
            note = "⚠️ Не удалось уведомить клиента (возможно, заблокировал бота)."

    ticket_text, ticket = await _ticket_detail_text(config.db_path, ticket_id)
    if note:
        ticket_text = f"{ticket_text}\n\n{note}"
    await msg.answer(ticket_text, parse_mode="HTML", reply_markup=kb_ticket_admin_detail(ticket_id, ticket["status"]))


@router.callback_query(F.data.startswith("tk_escalate:"))
async def cb_tk_escalate(cb: CallbackQuery, state: FSMContext, config: SupportTicketsConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    try:
        ticket_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    # Single exit point: whether the transition applies (old_status in
    # new/answered and the CAS succeeds) or not (already escalated/closed, or
    # a lost race), we always fall through to the SAME final read+render
    # below — no nested aiosqlite.connect() while this block's connection is
    # still open, matching every other call site in the file.
    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute("SELECT status FROM tickets WHERE id=?", (ticket_id,))).fetchone()
        if not row:
            await cb.message.edit_text("Тикет не найден.", reply_markup=kb_tickets_menu())
            return
        old_status = row[0]
        if old_status in ("new", "answered"):
            cur = await db.execute(
                "UPDATE tickets SET status='escalated', updated_at=datetime('now','localtime') WHERE id=? AND status=?",
                (ticket_id, old_status),
            )
            if cur.rowcount:
                # Distinct service note in the transcript — NOT a normal
                # admin reply, so it must not trigger NEW_REPLY_NOTIFY_TEXT.
                await db.execute(
                    "INSERT INTO ticket_messages (ticket_id, sender, text) VALUES (?, 'admin', ?)",
                    (ticket_id, "🔺 Тикет эскалирован на живого агента."),
                )
            await db.commit()

    text, ticket = await _ticket_detail_text(config.db_path, ticket_id)
    await _safe_edit_text(cb.message, text, parse_mode="HTML", reply_markup=kb_ticket_admin_detail(ticket_id, ticket["status"]))


@router.callback_query(F.data.startswith("tk_close:"))
async def cb_tk_close(cb: CallbackQuery, state: FSMContext, bot: Bot, config: SupportTicketsConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    try:
        ticket_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    closed_now = False
    client_user_id = None
    # Same single-exit-point / no-nested-connect shape as cb_tk_escalate.
    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute("SELECT status FROM tickets WHERE id=?", (ticket_id,))).fetchone()
        if not row:
            await cb.message.edit_text("Тикет не найден.", reply_markup=kb_tickets_menu())
            return
        old_status = row[0]
        if old_status != "closed":
            cur = await db.execute(
                "UPDATE tickets SET status='closed', updated_at=datetime('now','localtime') WHERE id=? AND status=?",
                (ticket_id, old_status),
            )
            if cur.rowcount:
                closed_now = True
                client_row = await (await db.execute(
                    "SELECT client_user_id FROM tickets WHERE id=?", (ticket_id,)
                )).fetchone()
                client_user_id = client_row[0] if client_row else None
            await db.commit()

    note = None
    if closed_now and client_user_id:
        try:
            await bot.send_message(
                client_user_id, RATING_PROMPT_TEXT.format(ticket_id=ticket_id),
                reply_markup=kb_rating_prompt(ticket_id),
            )
            note = "🔔 Клиенту отправлен запрос оценки."
        except TelegramAPIError as e:
            logger.warning(f"support_tickets: failed to send rating prompt for ticket {ticket_id}: {e}")
            note = "⚠️ Не удалось отправить клиенту запрос оценки (возможно, заблокировал бота)."

    text, ticket = await _ticket_detail_text(config.db_path, ticket_id)
    if note:
        text = f"{text}\n\n{note}"
    await _safe_edit_text(cb.message, text, parse_mode="HTML", reply_markup=kb_ticket_admin_detail(ticket_id, ticket["status"]))


# ── ADMIN: knowledge base ────────────────────────────────────────────────────

@router.callback_query(F.data == "kb_menu")
async def cb_kb_menu(cb: CallbackQuery, state: FSMContext, config: SupportTicketsConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    await cb.message.edit_text("📚 <b>База знаний</b>\n\nВыберите категорию:", parse_mode="HTML", reply_markup=kb_kb_menu())


@router.callback_query(F.data.startswith("kb_cat:"))
async def cb_kb_cat(cb: CallbackQuery, state: FSMContext, config: SupportTicketsConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    category = cb.data.split(":", 1)[1]
    async with aiosqlite.connect(config.db_path) as db:
        # No active=1 filter here (unlike the client-facing query below) —
        # admins need to see hidden articles too, to be able to reach/edit
        # them again (kb_kb_article_list marks them with 🗑).
        rows = await (await db.execute(
            "SELECT id, question, active FROM kb_articles WHERE category=? ORDER BY id DESC LIMIT ?",
            (category, MAX_LIST_BUTTONS),
        )).fetchall()
    label = TICKET_CATEGORY_LABELS.get(category, category)
    text = f"📚 {label}" if rows else f"📚 {label}\n\nСтатей пока нет."
    await cb.message.edit_text(text, reply_markup=kb_kb_article_list(rows, category))


@router.callback_query(F.data.startswith("kb_view:"))
async def cb_kb_view(cb: CallbackQuery, state: FSMContext, config: SupportTicketsConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    try:
        article_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute(
            "SELECT category, question, answer, active FROM kb_articles WHERE id=?", (article_id,)
        )).fetchone()
    if not row:
        await cb.message.edit_text("Статья не найдена.", reply_markup=kb_kb_menu())
        return
    category, question, answer, active = row
    status_note = "" if active else "\n\n🗑 <i>скрыта</i>"
    text = _cap_html(
        f"🏷 {TICKET_CATEGORY_LABELS.get(category, category)}\n\n"
        f"❓ <b>{_esc(question, MAX_QUESTION_LEN)}</b>\n\n{_esc(answer, MAX_ANSWER_LEN)}{status_note}"
    )
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_kb_article_detail(article_id))


@router.callback_query(F.data == "kb_add")
async def cb_kb_add(cb: CallbackQuery, state: FSMContext, config: SupportTicketsConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    await cb.message.edit_text("Выберите категорию новой статьи:", reply_markup=kb_category_pick("kb_add_cat"))


@router.callback_query(F.data.startswith("kb_add_cat:"))
async def cb_kb_add_cat(cb: CallbackQuery, state: FSMContext, config: SupportTicketsConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    category = cb.data.split(":", 1)[1]
    if category not in TICKET_CATEGORY_LABELS:
        return
    await state.clear()
    await state.set_state(KBFlow.question)
    await state.update_data(started_at=time.time(), category=category)
    await cb.message.edit_text("❓ Введите вопрос статьи:", reply_markup=kb_flow_cancel("kb_menu"))


@router.callback_query(F.data.startswith("kb_edit:"))
async def cb_kb_edit(cb: CallbackQuery, state: FSMContext, config: SupportTicketsConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        article_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute("SELECT category FROM kb_articles WHERE id=?", (article_id,))).fetchone()
    if not row:
        await cb.message.edit_text("Статья не найдена.", reply_markup=kb_kb_menu())
        return
    await state.clear()
    await state.set_state(KBFlow.question)
    await state.update_data(started_at=time.time(), category=row[0], edit_id=article_id)
    await cb.message.edit_text("❓ Введите новый текст вопроса:", reply_markup=kb_flow_cancel("kb_menu"))


@router.callback_query(F.data.startswith("kb_hide:"))
async def cb_kb_hide(cb: CallbackQuery, config: SupportTicketsConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        article_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute("SELECT category FROM kb_articles WHERE id=?", (article_id,))).fetchone()
        if not row:
            await cb.message.edit_text("Статья не найдена.", reply_markup=kb_kb_menu())
            return
        # Soft-delete: active=0, row and history preserved — invisible to the
        # client-side self-service search but still viewable/editable here
        # (admin-side kb_kb_article_list shows it too, marked with 🗑).
        await db.execute("UPDATE kb_articles SET active=0 WHERE id=?", (article_id,))
        await db.commit()
    category = row[0]
    async with aiosqlite.connect(config.db_path) as db:
        rows = await (await db.execute(
            "SELECT id, question, active FROM kb_articles WHERE category=? ORDER BY id DESC LIMIT ?",
            (category, MAX_LIST_BUTTONS),
        )).fetchall()
    label = TICKET_CATEGORY_LABELS.get(category, category)
    await cb.message.edit_text(f"🗑 Статья скрыта.\n\n📚 {label}", reply_markup=kb_kb_article_list(rows, category))


@router.message(KBFlow.question, F.text, ~F.text.startswith("/"))
async def kb_question_text(msg: Message, state: FSMContext):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    question = _valid_text(msg.text, MAX_QUESTION_LEN)
    if question is None:
        await msg.answer(f"Вопрос не может быть пустым и должен быть короче {MAX_QUESTION_LEN} символов.",
                          reply_markup=kb_flow_cancel("kb_menu"))
        return
    await state.update_data(pending_question=question)
    await state.set_state(KBFlow.answer)
    await msg.answer("💬 Введите текст ответа:", reply_markup=kb_flow_cancel("kb_menu"))


@router.message(KBFlow.answer, F.text, ~F.text.startswith("/"))
async def kb_answer_text(msg: Message, state: FSMContext, config: SupportTicketsConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    answer = _valid_text(msg.text, MAX_ANSWER_LEN)
    if answer is None:
        await msg.answer(f"Ответ не может быть пустым и должен быть короче {MAX_ANSWER_LEN} символов.",
                          reply_markup=kb_flow_cancel("kb_menu"))
        return
    category = data.get("category")
    question = data.get("pending_question")
    edit_id = data.get("edit_id")
    if not category or not question:
        await state.clear()
        await msg.answer("Сессия устарела, начните заново.", reply_markup=kb_main_menu())
        return
    await state.clear()
    async with aiosqlite.connect(config.db_path) as db:
        if edit_id:
            cur = await db.execute(
                "UPDATE kb_articles SET question=?, answer=? WHERE id=?", (question, answer, edit_id)
            )
            await db.commit()
            if cur.rowcount == 0:
                await msg.answer("⚠️ Статья была удалена до сохранения.", reply_markup=kb_kb_menu())
                return
            confirm = "✅ Статья обновлена."
        else:
            await db.execute(
                "INSERT INTO kb_articles (category, question, answer) VALUES (?, ?, ?)", (category, question, answer)
            )
            await db.commit()
            confirm = "✅ Статья добавлена."
    async with aiosqlite.connect(config.db_path) as db:
        rows = await (await db.execute(
            "SELECT id, question, active FROM kb_articles WHERE category=? ORDER BY id DESC LIMIT ?",
            (category, MAX_LIST_BUTTONS),
        )).fetchall()
    label = TICKET_CATEGORY_LABELS.get(category, category)
    await msg.answer(f"{confirm}\n\n📚 {label}", reply_markup=kb_kb_article_list(rows, category))


# ── ADMIN: admins ─────────────────────────────────────────────────────────────

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
    await state.clear()
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
    if config.bot_id is not None:
        try:
            await add_bot_admin(config.bot_id, text)
        except Exception as e:
            logger.warning(f"admin_add_id: add_bot_admin sync failed for bot {config.bot_id}: {e}")
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
    await state.clear()
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
    if config.bot_id is not None:
        try:
            await remove_bot_admin(config.bot_id, target)
        except Exception as e:
            logger.warning(f"cb_adm_remove_pick: remove_bot_admin sync failed for bot {config.bot_id}: {e}")
    await state.clear()
    await cb.message.edit_text(f"✅ <code>{_esc(target)}</code> удалён.", parse_mode="HTML",
                                reply_markup=kb_admins_menu())


# ── CLIENT: self-service KB search + ticket creation ─────────────────────────

@router.callback_query(F.data == "cli_support")
async def cb_cli_support(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await cb.message.edit_text("🆘 Выберите категорию вопроса:", reply_markup=kb_client_categories())


@router.callback_query(F.data.startswith("cli_cat:"))
async def cb_cli_cat(cb: CallbackQuery, state: FSMContext, config: SupportTicketsConfig):
    await cb.answer()
    await state.clear()
    category = cb.data.split(":", 1)[1]
    if category not in TICKET_CATEGORY_LABELS:
        return
    async with aiosqlite.connect(config.db_path) as db:
        rows = await (await db.execute(
            "SELECT id, question FROM kb_articles WHERE category=? AND active=1 ORDER BY id DESC LIMIT ?",
            (category, MAX_LIST_BUTTONS),
        )).fetchall()
    label = TICKET_CATEGORY_LABELS.get(category, category)
    text = f"📚 {label}\n\nВозможно, ответ уже есть здесь:" if rows else f"📚 {label}\n\nПока нет статей в этой категории."
    await cb.message.edit_text(text, reply_markup=kb_client_kb_list(rows, category))


@router.callback_query(F.data.startswith("cli_kb_view:"))
async def cb_cli_kb_view(cb: CallbackQuery, state: FSMContext, config: SupportTicketsConfig):
    await cb.answer()
    await state.clear()
    try:
        article_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute(
            "SELECT category, question, answer FROM kb_articles WHERE id=? AND active=1", (article_id,)
        )).fetchone()
    if not row:
        await cb.message.edit_text("Статья не найдена.", reply_markup=kb_client_categories())
        return
    category, question, answer = row
    text = _cap_html(f"❓ <b>{_esc(question, MAX_QUESTION_LEN)}</b>\n\n{_esc(answer, MAX_ANSWER_LEN)}")
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_client_kb_article(category))


@router.callback_query(F.data.startswith("cli_new_ticket:"))
async def cb_cli_new_ticket(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    category = cb.data.split(":", 1)[1]
    if category not in TICKET_CATEGORY_LABELS:
        return
    await state.clear()
    await state.set_state(ClientTicketFlow.problem_text)
    await state.update_data(started_at=time.time(), category=category)
    await cb.message.edit_text("✍️ Опишите вашу проблему одним сообщением:", reply_markup=kb_flow_cancel())


@router.message(ClientTicketFlow.problem_text, F.text, ~F.text.startswith("/"))
async def client_problem_text(msg: Message, state: FSMContext, bot: Bot, config: SupportTicketsConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_client_menu())
        return
    text = _valid_text(msg.text, MAX_PROBLEM_LEN)
    if text is None:
        await msg.answer(f"Описание не может быть пустым и должно быть короче {MAX_PROBLEM_LEN} символов.",
                          reply_markup=kb_flow_cancel())
        return
    category = data.get("category")
    if category not in TICKET_CATEGORY_LABELS:
        await state.clear()
        await msg.answer("Сессия устарела, начните заново.", reply_markup=kb_client_menu())
        return
    await state.clear()
    client_name = msg.from_user.full_name or str(msg.from_user.id)
    async with aiosqlite.connect(config.db_path) as db:
        cur = await db.execute(
            "INSERT INTO tickets (client_user_id, client_name, category) VALUES (?, ?, ?)",
            (msg.from_user.id, client_name, category),
        )
        ticket_id = cur.lastrowid
        await db.execute(
            "INSERT INTO ticket_messages (ticket_id, sender, text) VALUES (?, 'client', ?)", (ticket_id, text)
        )
        await db.commit()

    await msg.answer(
        f"✅ Тикет №{ticket_id} создан. Мы ответим вам здесь в этом чате.", reply_markup=kb_client_menu()
    )
    await _notify_admins(bot, config, NEW_TICKET_ADMIN_NOTIFY.format(
        ticket_id=ticket_id, client_name=_esc(client_name), category=TICKET_CATEGORY_LABELS[category], text=_esc(text, 1000)
    ))


@router.callback_query(F.data == "cli_tickets")
async def cb_cli_tickets(cb: CallbackQuery, state: FSMContext, config: SupportTicketsConfig):
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
    await cb.message.edit_text("📋 Ваши тикеты:", reply_markup=kb_client_ticket_list(rows))


@router.callback_query(F.data.startswith("cli_ticket_view:"))
async def cb_cli_ticket_view(cb: CallbackQuery, state: FSMContext, config: SupportTicketsConfig):
    await cb.answer()
    await state.clear()
    try:
        ticket_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    # Ownership check: a client must never be able to view another client's
    # ticket by guessing/incrementing the id (tickets.id is a sequential
    # AUTOINCREMENT, so this is a real IDOR risk without the client_user_id
    # filter here).
    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute(
            "SELECT id FROM tickets WHERE id=? AND client_user_id=?", (ticket_id, cb.from_user.id)
        )).fetchone()
    if not row:
        await cb.message.edit_text("Тикет не найден.", reply_markup=kb_client_menu())
        return
    text, ticket = await _ticket_detail_text(config.db_path, ticket_id)
    needs_rating = ticket["status"] == "closed" and ticket["satisfaction_rating"] is None
    await cb.message.edit_text(
        text, parse_mode="HTML", reply_markup=kb_client_ticket_detail(ticket_id, ticket["status"], needs_rating)
    )


@router.callback_query(F.data.startswith("cli_ticket_reply:"))
async def cb_cli_ticket_reply(cb: CallbackQuery, state: FSMContext, config: SupportTicketsConfig):
    await cb.answer()
    try:
        ticket_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute(
            "SELECT status FROM tickets WHERE id=? AND client_user_id=?", (ticket_id, cb.from_user.id)
        )).fetchone()
    if not row:
        await cb.message.edit_text("Тикет не найден.", reply_markup=kb_client_menu())
        return
    if row[0] == "closed":
        # Design requirement: a client must not be able to add messages to a
        # closed ticket via this button — only view history and rate it.
        text, ticket = await _ticket_detail_text(config.db_path, ticket_id)
        needs_rating = ticket["satisfaction_rating"] is None
        await cb.message.edit_text(
            text, parse_mode="HTML", reply_markup=kb_client_ticket_detail(ticket_id, ticket["status"], needs_rating)
        )
        return
    await state.clear()
    await state.set_state(ClientTicketFlow.reply_text)
    await state.update_data(started_at=time.time(), ticket_id=ticket_id)
    await cb.message.edit_text("✍️ Введите ваше сообщение:", reply_markup=kb_flow_cancel())


@router.message(ClientTicketFlow.reply_text, F.text, ~F.text.startswith("/"))
async def client_reply_text(msg: Message, state: FSMContext, bot: Bot, config: SupportTicketsConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_client_menu())
        return
    text = _valid_text(msg.text, MAX_REPLY_LEN)
    if text is None:
        await msg.answer(f"Сообщение не может быть пустым и должно быть короче {MAX_REPLY_LEN} символов.",
                          reply_markup=kb_flow_cancel())
        return
    ticket_id = data.get("ticket_id")
    await state.clear()

    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute(
            "SELECT status, client_name, category FROM tickets WHERE id=? AND client_user_id=?",
            (ticket_id, msg.from_user.id),
        )).fetchone()
        if not row:
            await msg.answer("Тикет не найден.", reply_markup=kb_client_menu())
            return
        if row[0] == "closed":
            await msg.answer("⚠️ Тикет уже закрыт, сообщение не отправлено.", reply_markup=kb_client_menu())
            return
        await db.execute(
            "INSERT INTO ticket_messages (ticket_id, sender, text) VALUES (?, 'client', ?)", (ticket_id, text)
        )
        await db.commit()
        client_name, category = row[1], row[2]

    await msg.answer(f"✅ Сообщение отправлено по тикету №{ticket_id}.", reply_markup=kb_client_menu())
    await _notify_admins(bot, config, FOLLOWUP_ADMIN_NOTIFY.format(
        ticket_id=ticket_id, client_name=_esc(client_name), text=_esc(text, 1000)
    ))


# ── rating (reachable from both the push prompt on close AND the client's
# own ticket card fallback if the push failed/was never seen) ────────────────

@router.callback_query(F.data.startswith("sup_rate:"))
async def cb_sup_rate(cb: CallbackQuery, config: SupportTicketsConfig):
    await cb.answer()
    try:
        _, ticket_id_s, score_s = cb.data.split(":", 2)
        ticket_id, score = int(ticket_id_s), int(score_s)
    except ValueError:
        return
    if not 1 <= score <= 5:
        return
    async with aiosqlite.connect(config.db_path) as db:
        # Compare-and-swap: only the ticket's own client, only once, only on
        # a closed ticket — prevents double-tap double-writes and prevents
        # anyone else from rating on the client's behalf.
        cur = await db.execute(
            "UPDATE tickets SET satisfaction_rating=? WHERE id=? AND client_user_id=? "
            "AND status='closed' AND satisfaction_rating IS NULL",
            (score, ticket_id, cb.from_user.id),
        )
        await db.commit()
    if cur.rowcount == 0:
        await cb.message.edit_text("Оценка уже сохранена ранее или недоступна.")
        return
    await cb.message.edit_text(f"🙏 Спасибо за оценку: {score}/5!")


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
