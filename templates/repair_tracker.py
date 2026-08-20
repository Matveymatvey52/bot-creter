# TEMPLATE: repair_tracker
# USE FOR: универсальный трекер заявок мастерской по ремонту (электроника, обувь, часы, бытовая техника, ателье — любой ремонтный бизнес): приём заявки от клиента, статус-флоу заявки с автоуведомлением клиента на КАЖДОЕ изменение статуса, ввод ориентировочной цены при переводе в работу, приватные заметки администратора
# CUSTOMIZE: sections marked with # CUSTOMIZE
from __future__ import annotations

import asyncio
import html
import logging
import json
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
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

# ── CUSTOMIZE ────────────────────────────────────────────────────────────────
# Same status as every other template's CUSTOMIZE block: per-file source-text
# customization Claude edits when generating a specific bot, not per-bot
# runtime state (that's config.db_path/admins_file below).
BOT_DESCRIPTION = (
    "Приём заявок в ремонтную мастерскую: клиент оставляет заявку, статус-флоу "
    "(новая → диагностика → в работе → готова → выдана), клиент автоматически "
    "уведомляется о каждом изменении статуса, у администратора — приватные заметки."
)
WELCOME_TEXT = (
    "🔧 <b>Мастерская — трекер заявок на ремонт</b>\n\n"
    "Приём заявок, статус-флоу: новая → диагностика → в работе → готова → "
    "выдана. Клиент уведомляется о каждом изменении статуса.\n\n"
    "Выберите действие:"
)
CLIENT_WELCOME_TEXT = (
    "👋 Здравствуйте! Это бот мастерской по ремонту.\n\n"
    "Оставьте заявку — и мы сообщим вам о каждом изменении её статуса."
)
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
            "name": "repair_tickets",
            "table": "repair_tickets",
            "order_by": "created_at DESC",
            "creatable": True,
            "title": "Заявки на ремонт",
            "titleField": "item_description",
            "fields": [
                {"name": "client_name", "label": "Клиент", "kind": "text", "list": True, "detail": True, "create": True},
                {"name": "client_phone", "label": "Телефон", "kind": "text", "list": False, "detail": True, "create": True},
                {"name": "item_description", "required": True, "label": "Устройство", "kind": "text", "list": True, "detail": True, "create": True},
                {"name": "issue_description", "required": True, "label": "Проблема", "kind": "text", "list": False, "detail": True, "create": True},
                {"name": "status", "label": "Статус", "kind": "status", "list": True, "detail": True, "create": False},
                {"name": "estimated_price", "label": "Оценка стоимости", "kind": "number", "list": False, "detail": True, "create": False},
                {"name": "actual_price", "label": "Итоговая стоимость", "kind": "number", "list": True, "detail": True, "create": False},
                {"name": "created_at", "label": "Создана", "kind": "date", "list": False, "detail": True, "create": False},
            ],
        },
    ],
}

# Explicit forward-only flow, same shape as templates/vehicle_service.py's
# STATUS_TRANSITIONS: no backward moves. "cancelled" is reachable from any
# non-terminal status as a side-branch (same precedent already confirmed with
# the owner for orders_tracker/vehicle_service).
STATUS_TRANSITIONS = {
    "new": ["diagnosing", "cancelled"],
    "diagnosing": ["in_progress", "cancelled"],
    "in_progress": ["ready", "cancelled"],
    "ready": ["given_out", "cancelled"],
    "given_out": [],
    "cancelled": [],
}

# "Активные заявки" = everything except the two terminal statuses.
TERMINAL_STATUSES = ("given_out", "cancelled")

# Labels WITH emoji — used on buttons and in the admin/client detail cards.
STATUS_LABELS = {
    "new": "🆕 Новая",
    "diagnosing": "🔍 Диагностика",
    "in_progress": "⚙️ В работе",
    "ready": "✅ Готова",
    "given_out": "📤 Выдана",
    "cancelled": "❌ Отменена",
}

# Plain labels (no emoji) — used inside the client notification's «quoted»
# phrase, matching the design brief's own example wording verbatim
# ("статус изменён на «В работе»").
STATUS_LABEL_PLAIN = {
    "new": "Новая",
    "diagnosing": "Диагностика",
    "in_progress": "В работе",
    "ready": "Готова",
    "given_out": "Выдана",
    "cancelled": "Отменена",
}


# ── config ───────────────────────────────────────────────────────────────────
# Same pattern as every other template — see docs/STAGE2_DESIGN.md.

@dataclass
class RepairTrackerConfig:
    bot_name: str
    db_path: str
    admins_file: Path
    welcome_image: Path
    display_name: str | None = None
    group_chat_id: str | None = None


def _paths_for(name: str, data_dir: Path) -> RepairTrackerConfig:
    return RepairTrackerConfig(
        bot_name=name,
        db_path=str(data_dir / f"{name}_data.db"),
        admins_file=data_dir / f"admins_{name}.json",
        welcome_image=data_dir / "bot_images" / f"{name}.jpg",
    )


def config_from_env() -> RepairTrackerConfig:
    """Standalone/subprocess mode."""
    name = Path(__file__).stem
    data_dir = Path(os.getenv("DATA_DIR", "./data"))
    data_dir.mkdir(exist_ok=True)
    return _paths_for(name, data_dir)


def config_from_bot_row(bot_row: dict, data_dir: Path) -> RepairTrackerConfig:
    """Webhook runtime mode. Paths built from bot_row["bot_id"] (bots.id, the
    physically unique AUTOINCREMENT PK) — NOT bot_row["name"] — same reasoning
    as every other template's config_from_bot_row (see docs/STAGE2_DESIGN.md
    "Изоляция по bots.id"). This is the ENTIRE data-isolation guarantee: two
    bots running this same module get two distinct db_path/admins_file pairs
    derived from their own bot_id, so even the identical Telegram admin
    user_id driving both bots can never see the other bot's rows — every
    query below is scoped to config.db_path, which is per-bot."""
    bot_id = bot_row["bot_id"]
    config = RepairTrackerConfig(
        bot_name=bot_row["name"],
        db_path=str(data_dir / f"bot_{bot_id}_data.db"),
        admins_file=data_dir / f"admins_{bot_id}.json",
        welcome_image=data_dir / "bot_images" / f"bot_{bot_id}.jpg",
    )
    config.display_name = bot_row.get("display_name")
    config.group_chat_id = bot_row.get("group_chat_id")
    return config


class ConfigMiddleware(BaseMiddleware):
    """Injects this bot's RepairTrackerConfig into data["config"]."""

    def __init__(self, config: RepairTrackerConfig) -> None:
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

def _is_admin(user_id: int, config: RepairTrackerConfig) -> bool:
    return str(user_id) in _load_admins(config.admins_file)


def _esc(value, max_len: int = 500) -> str:
    """HTML-escapes AND length-bounds any user-supplied text before it goes into
    a parse_mode="HTML" message — same helper/rationale as every other
    template's _esc() (e.g. templates/vehicle_service.py)."""
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


# ── phone normalization ────────────────────────────────────────────────────────
# Same RU-phone formula as templates/vehicle_service.py's _normalize_phone(),
# reused verbatim per the project convention (don't reinvent this) — a phone
# typed by the client normalizes to a stable, consistently-formatted string.

def _normalize_phone(raw: str) -> str | None:
    digits = re.sub(r"[^\d]", "", raw)
    if len(digits) == 11 and digits[0] in ("7", "8"):
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    else:
        return None
    return f"+{digits[0]} ({digits[1:4]}) {digits[4:7]}-{digits[7:9]}-{digits[9:11]}"


def _valid_price(text: str) -> int | None:
    try:
        price = int(text.strip())
    except ValueError:
        return None
    if price < 0 or price > 1_000_000_000:
        return None
    return price


def _valid_admin_id(text: str) -> bool:
    """Same guard as templates/vehicle_service.py's _valid_admin_id()."""
    if not (bool(text) and text.isascii() and text.isdigit() and len(text) <= 15):
        return False
    return int(text) > 0 and str(int(text)) == text


# ── db ────────────────────────────────────────────────────────────────────────
# DESIGN NOTE — data model: a single `repair_tickets` table (the schema is
# fixed by the design brief, no per-client/per-item side tables like
# vehicle_service's clients/vehicles). client_user_id is captured DIRECTLY
# from the Telegram user who runs the "📝 Оставить заявку" flow — this is a
# deliberate simplification versus vehicle_service/orders_tracker's
# phone-lookup + shared-Contact linking dance: those templates need linking
# because a client record can pre-exist (created by an admin) before the
# client ever opens the bot. Here a ticket is only ever created BY the client
# themselves, in the same flow that captures client_user_id, so ownership is
# established by construction — no separate linking step, no risk of user A
# hijacking user B's notifications. "Мои заявки" and the ticket-detail view
# both still enforce `WHERE client_user_id = <requesting user's own id>` on
# every read, so even a hand-crafted callback_data guessing another ticket's
# id cannot leak it — this is this template's equivalent of vehicle_service's
# Contact-ownership check, just enforced by a WHERE clause instead of a
# Contact.user_id comparison.
#
# DESIGN NOTE — notifications: unlike vehicle_service (client-notify on
# reaching "ready" ONLY), this template notifies the client on EVERY status
# change, per the brief. _apply_status_change() below does one
# compare-and-swap UPDATE (`WHERE id=? AND status=?`) per transition — same
# double-tap-safety principle as vehicle_service/orders_tracker: a duplicate
# tap on an already-applied transition button hits rowcount=0 and is a no-op,
# so the client is never notified twice for one real change. There is no
# separate status-log table (vehicle_service has one, with a `notified`
# column) — not needed here since nothing downstream reads transition
# history, and the CAS UPDATE's rowcount is itself sufficient to know whether
# THIS call actually performed the change and must notify.
#
# DESIGN NOTE — status graph: STATUS_TRANSITIONS above is forward-only
# (new → diagnosing → in_progress → ready → given_out) with "cancelled"
# reachable as a side-branch from any non-terminal status, exactly mirroring
# vehicle_service/orders_tracker's precedent. The one stateful branch is
# diagnosing → in_progress, which per the brief must collect a price BEFORE
# applying: cb_atkt_status() detects that specific (old_status, target) pair
# and diverts into StatusPriceFlow instead of calling _apply_status_change()
# immediately; every other transition applies directly.
#
# KNOWN LIMITATION (left as-is, not fixed): `actual_price` exists in the
# required schema but no flow in the brief ever populates it (only
# estimated_price, captured once at diagnosing→in_progress) — it stays NULL
# until some future admin flow is added to set it (e.g. at the ready/
# given_out step). Not invented here since the brief doesn't specify one.

async def init_db(db_path: str):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS repair_tickets (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                client_user_id      INTEGER NOT NULL,
                client_name         TEXT,
                client_phone        TEXT,
                item_description    TEXT NOT NULL,
                issue_description   TEXT NOT NULL,
                status              TEXT NOT NULL DEFAULT 'new'
                                    CHECK(status IN ('new','diagnosing','in_progress','ready','given_out','cancelled')),
                estimated_price     INTEGER,
                actual_price        INTEGER,
                admin_note          TEXT,
                created_at          TEXT DEFAULT (datetime('now','localtime')),
                updated_at          TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_tickets_client ON repair_tickets(client_user_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_tickets_status ON repair_tickets(status)")
        await db.commit()


# ── FSM staleness guard ─────────────────────────────────────────────────────────
# Same mechanic as templates/vehicle_service.py's FLOW_TIMEOUT_SECONDS/_flow_expired.
FLOW_TIMEOUT_SECONDS = 300


def _flow_expired(data: dict) -> bool:
    started_at = data.get("started_at")
    return started_at is None or (time.time() - started_at) > FLOW_TIMEOUT_SECONDS


# ── FSM states ───────────────────────────────────────────────────────────────

class TicketFlow(StatesGroup):
    """Client-side: describe item → describe issue → optional phone → create."""
    item = State()
    issue = State()
    phone = State()

class StatusPriceFlow(StatesGroup):
    """Admin-side: the ONE transition (diagnosing → in_progress) that needs a
    price collected before it applies."""
    price = State()

class NoteFlow(StatesGroup):
    """Admin-side: free-text note attached to a ticket, never shown to the client."""
    text = State()

class AdminMgmtFlow(StatesGroup):
    add_admin = State()
    remove_admin_pick = State()


MAX_LIST_BUTTONS = 25
MAX_ADMIN_REMOVE_BUTTONS = 30
MAX_ITEM_LEN = 200
MAX_ISSUE_LEN = 1000
MAX_NOTE_LEN = 1000


# ── keyboards ─────────────────────────────────────────────────────────────────

def kb_back(callback_data: str = "main_menu") -> InlineKeyboardButton:
    return InlineKeyboardButton(text="◀️ Назад", callback_data=callback_data)

def kb_flow_cancel() -> InlineKeyboardMarkup:
    # Single shared "flow_cancel" callback for every multi-step chain (ticket
    # creation, price entry, note entry) — same principle as
    # vehicle_service.py's kb_flow_cancel().
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="flow_cancel")],
    ])

def kb_phone_skip() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭ Пропустить", callback_data="tkt_phone_skip")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="flow_cancel")],
    ])

# ── client menu ──
def kb_client_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Оставить заявку", callback_data="tkt_new")],
        [InlineKeyboardButton(text="📋 Мои заявки", callback_data="tkt_mine")],
    ])

# ── admin menu ──
def kb_admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎫 Активные заявки", callback_data="atkt_active")],
        [InlineKeyboardButton(text="📋 Все заявки", callback_data="atkt_all")],
        [InlineKeyboardButton(text="💬 Добавить заметку", callback_data="atkt_note")],
        [InlineKeyboardButton(text="👥 Админы", callback_data="adm_menu")],
    ])

def _menu_for(user_id: int, config: RepairTrackerConfig) -> tuple[str, InlineKeyboardMarkup]:
    if _is_admin(user_id, config):
        return WELCOME_TEXT, kb_admin_menu()
    return CLIENT_WELCOME_TEXT, kb_client_menu()


def kb_ticket_list(rows: list[tuple], callback_prefix: str, back_cb: str) -> InlineKeyboardMarkup:
    """Shared list-rendering for both the client's "Мои заявки" and every
    admin ticket list — rows are (id, status, item_description) tuples."""
    btns = [
        [InlineKeyboardButton(
            text=f"№{tid} · {STATUS_LABELS.get(status, status)} · {_esc(item, 25)}",
            callback_data=f"{callback_prefix}:{tid}",
        )]
        for tid, status, item in rows[:MAX_LIST_BUTTONS]
    ]
    btns.append([kb_back(back_cb)])
    return InlineKeyboardMarkup(inline_keyboard=btns)


_STATUS_FILTERS = [(code, STATUS_LABELS[code]) for code in STATUS_LABELS] + [("all", "📋 Все")]

def kb_status_filters() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=label, callback_data=f"atkt_filter:{code}")] for code, label in _STATUS_FILTERS]
    rows.append([kb_back("main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_ticket_detail_admin(ticket_id: int, status: str) -> InlineKeyboardMarkup:
    rows = []
    for target in STATUS_TRANSITIONS.get(status, []):
        icon = "❌ Отменить" if target == "cancelled" else f"▶️ {STATUS_LABELS.get(target, target)}"
        rows.append([InlineKeyboardButton(text=icon, callback_data=f"atkt_status:{ticket_id}:{target}")])
    rows.append([InlineKeyboardButton(text="💬 Заметка", callback_data=f"note_pick:{ticket_id}")])
    rows.append([kb_back("atkt_all")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_ticket_detail_client(ticket_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[kb_back("tkt_mine")]])

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

async def _admin_ticket_detail(db_path: str, ticket_id: int, extra_note: str | None = None) -> tuple[str, str] | None:
    """Returns (rendered_html, status) for the admin-facing detail card, or
    None if the ticket doesn't exist. Includes admin_note — this view is
    ADMIN-ONLY, never reused for the client-facing card below."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute("SELECT * FROM repair_tickets WHERE id=?", (ticket_id,))).fetchone()
    if not row:
        return None
    lines = [
        f"🎫 <b>Заявка №{row['id']}</b> · {STATUS_LABELS.get(row['status'], row['status'])}\n",
        f"👤 {_esc(row['client_name'] or 'Без имени')}"
        + (f" · {_esc(row['client_phone'])}" if row["client_phone"] else ""),
        f"🔧 Предмет: {_esc(row['item_description'])}",
        f"💬 Неисправность: {_esc(row['issue_description'])}",
    ]
    if row["estimated_price"] is not None:
        lines.append(f"💰 Ориентировочно: {row['estimated_price']}")
    if row["actual_price"] is not None:
        lines.append(f"💵 Итог: {row['actual_price']}")
    if row["admin_note"]:
        lines.append(f"📝 Заметка: {_esc(row['admin_note'])}")
    lines.append(f"🕐 Создана: {row['created_at']} · Обновлена: {row['updated_at']}")
    if extra_note:
        lines.append(f"\n{extra_note}")
    return _join_bounded(lines), row["status"]


def _client_ticket_text(row) -> str:
    """Client-facing detail card — deliberately excludes admin_note (never
    shown to the client, per the brief)."""
    lines = [
        f"🎫 <b>Заявка №{row['id']}</b> · {STATUS_LABELS.get(row['status'], row['status'])}\n",
        f"🔧 Предмет: {_esc(row['item_description'])}",
        f"💬 Неисправность: {_esc(row['issue_description'])}",
    ]
    if row["estimated_price"] is not None:
        lines.append(f"💰 Ориентировочная стоимость: {row['estimated_price']}")
    if row["actual_price"] is not None:
        lines.append(f"💵 Итоговая стоимость: {row['actual_price']}")
    lines.append(f"🕐 Создана: {row['created_at']}")
    return _join_bounded(lines)


async def _apply_status_change(
    config: RepairTrackerConfig, bot: Bot, ticket_id: int, old_status: str, new_status: str,
    estimated_price: int | None = None,
) -> tuple[bool, str | None]:
    """Compare-and-swap status UPDATE (`WHERE id=? AND status=?`) + client
    notification. Returns (applied, note):
    - applied=False means rowcount was 0 — either a stale/double-tapped
      button (another admin, or a re-tap on an already-applied transition),
      NOT an error; caller re-renders the ticket's actual current state.
    - applied=True means THIS call performed the transition — the client is
      then notified (best-effort; a TelegramAPIError, e.g. the client
      blocked the bot, does not roll back the status change)."""
    async with aiosqlite.connect(config.db_path) as db:
        if estimated_price is not None:
            cur = await db.execute(
                "UPDATE repair_tickets SET status=?, estimated_price=?, updated_at=datetime('now','localtime') "
                "WHERE id=? AND status=?",
                (new_status, estimated_price, ticket_id, old_status),
            )
        else:
            cur = await db.execute(
                "UPDATE repair_tickets SET status=?, updated_at=datetime('now','localtime') WHERE id=? AND status=?",
                (new_status, ticket_id, old_status),
            )
        if cur.rowcount == 0:
            await db.commit()
            return False, None
        row = await (await db.execute(
            "SELECT client_user_id, item_description FROM repair_tickets WHERE id=?", (ticket_id,)
        )).fetchone()
        await db.commit()

    note = None
    if row:
        client_user_id, item_description = row
        label = STATUS_LABEL_PLAIN.get(new_status, new_status)
        text = (
            f"🔔 Ваша заявка №{ticket_id}: статус изменён на «{label}».\n"
            f"Предмет: {_esc(item_description)}"
        )
        try:
            await bot.send_message(client_user_id, text)
            note = "🔔 Клиент уведомлён."
        except TelegramAPIError as e:
            logger.warning(f"repair_tracker: failed to notify client for ticket {ticket_id}: {e}")
            note = "⚠️ Не удалось уведомить клиента (возможно, заблокировал бота)."
    return True, note


# ── /start ────────────────────────────────────────────────────────────────────

@router.message(Command("start"), F.chat.type == "private")
async def cmd_start(message: Message, state: FSMContext, config: RepairTrackerConfig):
    # Same reasoning as every other template's cmd_start: /start must reset
    # any dangling mid-flow FSM state before showing a menu.
    await state.clear()
    admins = _load_admins(config.admins_file)
    first_time_admin = not admins
    if first_time_admin:
        _save_admins(config.admins_file, {str(message.from_user.id)})
        admins = {str(message.from_user.id)}

    text, kb = _menu_for(message.from_user.id, config)
    if str(message.from_user.id) in admins and config.welcome_image.exists():
        await message.answer_photo(FSInputFile(str(config.welcome_image)), caption=text, parse_mode="HTML", reply_markup=kb)
    else:
        await message.answer(text, parse_mode="HTML", reply_markup=kb)
    if first_time_admin:
        await message.answer(
            "👑 <b>Вы — администратор этого бота.</b>\n\n"
            "Управление другими администраторами — кнопка «👥 Админы» выше.",
            parse_mode="HTML",
        )


@router.callback_query(F.data == "main_menu")
async def cb_main_menu(cb: CallbackQuery, state: FSMContext, config: RepairTrackerConfig):
    await cb.answer()
    await state.clear()
    text, kb = _menu_for(cb.from_user.id, config)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "flow_cancel")
async def cb_flow_cancel(cb: CallbackQuery, state: FSMContext, config: RepairTrackerConfig):
    await cb.answer()
    await state.clear()
    _text, kb = _menu_for(cb.from_user.id, config)
    await cb.message.edit_text("Отменено.", reply_markup=kb)


# ── CLIENT: new ticket flow (item → issue → phone[optional] → create) ────────

@router.callback_query(F.data == "tkt_new")
async def cb_tkt_new(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await state.set_state(TicketFlow.item)
    await state.update_data(started_at=time.time())
    await cb.message.edit_text(
        "📝 Что нужно отремонтировать? (например: наушники Sony WH-1000)",
        reply_markup=kb_flow_cancel(),
    )


@router.message(TicketFlow.item, F.text, ~F.text.startswith("/"))
async def tkt_item(msg: Message, state: FSMContext):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_client_menu())
        return
    item = msg.text.strip()
    if not item:
        await msg.answer("Описание не может быть пустым. Что нужно отремонтировать?", reply_markup=kb_flow_cancel())
        return
    if len(item) > MAX_ITEM_LEN:
        await msg.answer(f"⚠️ Слишком длинное описание. Уложитесь в {MAX_ITEM_LEN} символов:", reply_markup=kb_flow_cancel())
        return
    await state.update_data(item_description=item)
    await state.set_state(TicketFlow.issue)
    await msg.answer("🔧 Опишите неисправность:", reply_markup=kb_flow_cancel())


@router.message(TicketFlow.issue, F.text, ~F.text.startswith("/"))
async def tkt_issue(msg: Message, state: FSMContext):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_client_menu())
        return
    issue = msg.text.strip()
    if not issue:
        await msg.answer("Описание не может быть пустым. Опишите неисправность:", reply_markup=kb_flow_cancel())
        return
    if len(issue) > MAX_ISSUE_LEN:
        await msg.answer(f"⚠️ Слишком длинное описание. Уложитесь в {MAX_ISSUE_LEN} символов:", reply_markup=kb_flow_cancel())
        return
    await state.update_data(issue_description=issue)
    await state.set_state(TicketFlow.phone)
    await msg.answer(
        "📱 Укажите номер телефона для связи (или нажмите «Пропустить»):", reply_markup=kb_phone_skip(),
    )


async def _finalize_ticket(message_answer, state: FSMContext, config: RepairTrackerConfig, bot: Bot, from_user, phone: str | None) -> None:
    data = await state.get_data()
    item = data.get("item_description")
    issue = data.get("issue_description")
    if not item or not issue:
        await state.clear()
        await message_answer("Сессия устарела, начните заново.", reply_markup=kb_client_menu())
        return
    await state.clear()
    name = from_user.full_name
    async with aiosqlite.connect(config.db_path) as db:
        cur = await db.execute(
            "INSERT INTO repair_tickets (client_user_id, client_name, client_phone, item_description, issue_description) "
            "VALUES (?,?,?,?,?)",
            (from_user.id, name, phone, item, issue),
        )
        ticket_id = cur.lastrowid
        await db.commit()

    await message_answer(
        f"✅ Заявка №{ticket_id} создана! Мы свяжемся с вами по мере обработки.", reply_markup=kb_client_menu(),
    )

    admins = _load_admins(config.admins_file)
    notify_text = (
        f"🆕 Новая заявка №{ticket_id}\n"
        f"👤 {_esc(name)}" + (f" · {_esc(phone)}" if phone else "") + "\n"
        f"🔧 {_esc(item)}\n💬 {_esc(issue)}"
    )
    for admin_id in admins:
        try:
            await bot.send_message(int(admin_id), notify_text)
        except (TelegramAPIError, ValueError) as e:
            # ValueError covers a corrupt/non-numeric entry in admins.json —
            # one bad admin id must not crash the ticket-creation flow for
            # the client, who has already gotten their own confirmation above.
            logger.warning(f"repair_tracker: failed to notify admin {admin_id} of new ticket {ticket_id}: {e}")


@router.message(TicketFlow.phone, F.text, ~F.text.startswith("/"))
async def tkt_phone(msg: Message, state: FSMContext, config: RepairTrackerConfig, bot: Bot):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_client_menu())
        return
    phone = _normalize_phone(msg.text.strip())
    if phone is None:
        await msg.answer(
            "❌ Не удалось распознать номер. Введите номер, например <b>+7 999 123-45-67</b>, "
            "или нажмите «Пропустить»:", parse_mode="HTML", reply_markup=kb_phone_skip(),
        )
        return
    await _finalize_ticket(msg.answer, state, config, bot, msg.from_user, phone)


@router.callback_query(TicketFlow.phone, F.data == "tkt_phone_skip")
async def cb_tkt_phone_skip(cb: CallbackQuery, state: FSMContext, config: RepairTrackerConfig, bot: Bot):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_client_menu())
        return
    await _finalize_ticket(cb.message.answer, state, config, bot, cb.from_user, None)


# ── CLIENT: "Мои заявки" ──────────────────────────────────────────────────────

@router.callback_query(F.data == "tkt_mine")
async def cb_tkt_mine(cb: CallbackQuery, state: FSMContext, config: RepairTrackerConfig):
    await cb.answer()
    await state.clear()
    async with aiosqlite.connect(config.db_path) as db:
        rows = await (await db.execute(
            "SELECT id, status, item_description FROM repair_tickets WHERE client_user_id=? ORDER BY id DESC LIMIT ?",
            (cb.from_user.id, MAX_LIST_BUTTONS),
        )).fetchall()
    if not rows:
        await cb.message.edit_text("У вас пока нет заявок.", reply_markup=kb_client_menu())
        return
    await cb.message.edit_text("📋 Ваши заявки:", reply_markup=kb_ticket_list(rows, "tkt_view", "main_menu"))


@router.callback_query(F.data.startswith("tkt_view:"))
async def cb_tkt_view(cb: CallbackQuery, config: RepairTrackerConfig):
    await cb.answer()
    try:
        ticket_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    # Ownership check: this is the client-side equivalent of vehicle_service's
    # Contact.user_id guard — a hand-crafted callback_data guessing another
    # client's ticket id must not leak it. "not found" is deliberately used
    # for BOTH "doesn't exist" and "exists but isn't yours" so the response
    # never confirms/denies another ticket's existence.
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            "SELECT * FROM repair_tickets WHERE id=? AND client_user_id=?", (ticket_id, cb.from_user.id)
        )).fetchone()
    if not row:
        await cb.message.edit_text("Заявка не найдена.", reply_markup=kb_client_menu())
        return
    await cb.message.edit_text(_client_ticket_text(row), parse_mode="HTML", reply_markup=kb_ticket_detail_client(ticket_id))


# ── ADMIN: ticket lists ──────────────────────────────────────────────────────

@router.callback_query(F.data == "atkt_active")
async def cb_atkt_active(cb: CallbackQuery, state: FSMContext, config: RepairTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    placeholders = ",".join("?" * len(TERMINAL_STATUSES))
    async with aiosqlite.connect(config.db_path) as db:
        rows = await (await db.execute(
            f"SELECT id, status, item_description FROM repair_tickets WHERE status NOT IN ({placeholders}) "
            "ORDER BY id DESC LIMIT ?",
            (*TERMINAL_STATUSES, MAX_LIST_BUTTONS),
        )).fetchall()
    if not rows:
        await cb.message.edit_text("Активных заявок нет.", reply_markup=kb_admin_menu())
        return
    await cb.message.edit_text(f"🎫 Активные заявки ({len(rows)}):", reply_markup=kb_ticket_list(rows, "atkt_view", "main_menu"))


@router.callback_query(F.data == "atkt_all")
async def cb_atkt_all(cb: CallbackQuery, config: RepairTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await cb.message.edit_text("Выберите фильтр по статусу:", reply_markup=kb_status_filters())


@router.callback_query(F.data.startswith("atkt_filter:"))
async def cb_atkt_filter(cb: CallbackQuery, config: RepairTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    status = cb.data.split(":", 1)[1]
    async with aiosqlite.connect(config.db_path) as db:
        if status == "all":
            rows = await (await db.execute(
                "SELECT id, status, item_description FROM repair_tickets ORDER BY id DESC LIMIT ?", (MAX_LIST_BUTTONS,)
            )).fetchall()
        else:
            rows = await (await db.execute(
                "SELECT id, status, item_description FROM repair_tickets WHERE status=? ORDER BY id DESC LIMIT ?",
                (status, MAX_LIST_BUTTONS),
            )).fetchall()
    if not rows:
        await cb.message.edit_text("Заявок не найдено.", reply_markup=kb_status_filters())
        return
    await cb.message.edit_text(f"📋 Заявки (последние {len(rows)}):", reply_markup=kb_ticket_list(rows, "atkt_view", "atkt_all"))


@router.callback_query(F.data.startswith("atkt_view:"))
async def cb_atkt_view(cb: CallbackQuery, config: RepairTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        ticket_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    res = await _admin_ticket_detail(config.db_path, ticket_id)
    if res is None:
        await cb.message.edit_text("Заявка не найдена.", reply_markup=kb_admin_menu())
        return
    text, status = res
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_ticket_detail_admin(ticket_id, status))


# ── ADMIN: status transitions (diagnosing→in_progress needs a price first) ───

@router.callback_query(F.data.startswith("atkt_status:"))
async def cb_atkt_status(cb: CallbackQuery, state: FSMContext, config: RepairTrackerConfig, bot: Bot):
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
        row = await (await db.execute("SELECT status FROM repair_tickets WHERE id=?", (ticket_id,))).fetchone()
    if not row:
        await cb.message.edit_text("Заявка не найдена.", reply_markup=kb_admin_menu())
        return
    old_status = row[0]
    if new_status not in STATUS_TRANSITIONS.get(old_status, []):
        # Stale button (already transitioned by another admin, or a
        # double-tap on an already-applied transition) — re-render instead
        # of silently no-op'ing, same principle as vehicle_service.py's
        # cb_req_status.
        res = await _admin_ticket_detail(config.db_path, ticket_id)
        if res:
            text, status = res
            await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_ticket_detail_admin(ticket_id, status))
        return

    if old_status == "diagnosing" and new_status == "in_progress":
        # The ONE transition that needs a price collected before it applies.
        await state.set_state(StatusPriceFlow.price)
        await state.update_data(started_at=time.time(), price_ticket_id=ticket_id, price_old_status=old_status)
        await cb.message.edit_text(
            "💰 Введите ориентировочную стоимость ремонта (число, например 1500):", reply_markup=kb_flow_cancel(),
        )
        return

    applied, note = await _apply_status_change(config, bot, ticket_id, old_status, new_status)
    res = await _admin_ticket_detail(config.db_path, ticket_id, extra_note=note if applied else None)
    if res is None:
        await cb.message.edit_text("Заявка не найдена.", reply_markup=kb_admin_menu())
        return
    text, status = res
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_ticket_detail_admin(ticket_id, status))


@router.message(StatusPriceFlow.price, F.text, ~F.text.startswith("/"))
async def status_price_entered(msg: Message, state: FSMContext, config: RepairTrackerConfig, bot: Bot):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_admin_menu())
        return
    price = _valid_price(msg.text)
    if price is None:
        await msg.answer("Введите целое число от 0 до 1000000000, например: 1500", reply_markup=kb_flow_cancel())
        return
    ticket_id = data.get("price_ticket_id")
    old_status = data.get("price_old_status")
    if not ticket_id or not old_status:
        await state.clear()
        await msg.answer("Сессия устарела, начните заново.", reply_markup=kb_admin_menu())
        return
    await state.clear()

    applied, note = await _apply_status_change(config, bot, ticket_id, old_status, "in_progress", estimated_price=price)
    res = await _admin_ticket_detail(
        config.db_path, ticket_id,
        extra_note=note if applied else "⚠️ Заявка уже была изменена — попробуйте снова.",
    )
    if res is None:
        await msg.answer("Заявка не найдена.", reply_markup=kb_admin_menu())
        return
    text, status = res
    await msg.answer(text, parse_mode="HTML", reply_markup=kb_ticket_detail_admin(ticket_id, status))


# ── ADMIN: private note ───────────────────────────────────────────────────────

@router.callback_query(F.data == "atkt_note")
async def cb_atkt_note_menu(cb: CallbackQuery, config: RepairTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    async with aiosqlite.connect(config.db_path) as db:
        rows = await (await db.execute(
            "SELECT id, status, item_description FROM repair_tickets ORDER BY id DESC LIMIT ?", (MAX_LIST_BUTTONS,)
        )).fetchall()
    if not rows:
        await cb.message.edit_text("Заявок пока нет.", reply_markup=kb_admin_menu())
        return
    await cb.message.edit_text("Выберите заявку для заметки:", reply_markup=kb_ticket_list(rows, "note_pick", "main_menu"))


@router.callback_query(F.data.startswith("note_pick:"))
async def cb_note_pick(cb: CallbackQuery, state: FSMContext, config: RepairTrackerConfig):
    """Reached either from the "💬 Добавить заметку" list, OR as a shortcut
    button directly on a ticket's own admin detail card — same callback_data
    shape either way, so one handler covers both entry points."""
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        ticket_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute("SELECT id FROM repair_tickets WHERE id=?", (ticket_id,))).fetchone()
    if not row:
        await cb.message.edit_text("Заявка не найдена.", reply_markup=kb_admin_menu())
        return
    await state.set_state(NoteFlow.text)
    await state.update_data(started_at=time.time(), note_ticket_id=ticket_id)
    await cb.message.edit_text(
        "💬 Введите текст заметки (видна только администраторам, клиенту не показывается):",
        reply_markup=kb_flow_cancel(),
    )


@router.message(NoteFlow.text, F.text, ~F.text.startswith("/"))
async def note_text_entered(msg: Message, state: FSMContext, config: RepairTrackerConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_admin_menu())
        return
    note = msg.text.strip()
    if not note:
        await msg.answer("Заметка не может быть пустой. Введите текст заметки:", reply_markup=kb_flow_cancel())
        return
    if len(note) > MAX_NOTE_LEN:
        await msg.answer(f"⚠️ Слишком длинная заметка. Уложитесь в {MAX_NOTE_LEN} символов:", reply_markup=kb_flow_cancel())
        return
    ticket_id = data.get("note_ticket_id")
    await state.clear()
    if not ticket_id:
        await msg.answer("Сессия устарела, начните заново.", reply_markup=kb_admin_menu())
        return

    async with aiosqlite.connect(config.db_path) as db:
        cur = await db.execute(
            "UPDATE repair_tickets SET admin_note=?, updated_at=datetime('now','localtime') WHERE id=?",
            (note, ticket_id),
        )
        await db.commit()
    if cur.rowcount == 0:
        await msg.answer("Заявка не найдена (возможно, удалена).", reply_markup=kb_admin_menu())
        return

    res = await _admin_ticket_detail(config.db_path, ticket_id, extra_note="✅ Заметка сохранена.")
    if res is None:
        await msg.answer("Заявка не найдена.", reply_markup=kb_admin_menu())
        return
    text, status = res
    await msg.answer(text, parse_mode="HTML", reply_markup=kb_ticket_detail_admin(ticket_id, status))


# ── ADMINS menu ────────────────────────────────────────────────────────────────
# Copied verbatim (same shape) from templates/vehicle_service.py — every
# template uses this identical admins.json-backed pattern.

async def _admins_list_text(config: RepairTrackerConfig) -> str:
    ids = sorted(_load_admins(config.admins_file))
    if not ids:
        return "👥 Пусто"
    return _join_bounded(["👥 <b>Администраторы бота:</b>\n"] + [f"• <code>{_esc(i)}</code>" for i in ids])


@router.callback_query(F.data == "adm_menu")
async def cb_adm_menu(cb: CallbackQuery, state: FSMContext, config: RepairTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    text = await _admins_list_text(config)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_admins_menu())


@router.callback_query(F.data == "adm_add")
async def cb_adm_add(cb: CallbackQuery, state: FSMContext, config: RepairTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.set_state(AdminMgmtFlow.add_admin)
    await state.update_data(started_at=time.time())
    await cb.message.edit_text("Введите Telegram ID нового администратора:", reply_markup=kb_flow_cancel())


@router.message(AdminMgmtFlow.add_admin, F.text, ~F.text.startswith("/"))
async def admin_add_id(msg: Message, state: FSMContext, config: RepairTrackerConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_admin_menu())
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
async def cb_adm_remove(cb: CallbackQuery, state: FSMContext, config: RepairTrackerConfig):
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
async def cb_adm_remove_pick(cb: CallbackQuery, state: FSMContext, config: RepairTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_admin_menu())
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
    await cb.message.edit_text(f"✅ <code>{_esc(target)}</code> удалён.", parse_mode="HTML", reply_markup=kb_admins_menu())


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
