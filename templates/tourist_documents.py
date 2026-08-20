# TEMPLATE: tourist_documents
# USE FOR: турагентство — учёт документов туристов (паспорт, виза), привязка к туру/группе, напоминания об истечении срока, конфиденциальный доступ с журналом просмотров
# CUSTOMIZE: sections marked with # CUSTOMIZE
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import aiosqlite
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.filters import Command, StateFilter
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
# runtime state (that's config.db_path/admins_file below).
BOT_DESCRIPTION = "Учёт документов туристов: паспорт, виза, привязка к туру/группе, напоминания об истечении срока."
WELCOME_TEXT = (
    "🛂 <b>Документы туристов</b>\n\n"
    "Внутренний инструмент турагентства для учёта паспортных и визовых данных.\n\n"
    "Доступ есть только у сотрудников агентства. Все действия — через кнопки ниже."
)
NOT_ADMIN_TEXT = "🔒 Это внутренний инструмент турагентства — доступ есть только у сотрудников."
# Both passport_expiry and visa_expiry trigger a reminder this many days before
# the date, once, until the field is edited (which clears the notified flag —
# see _run_expiry_check / EDIT_FIELD_LABELS handling below).
EXPIRY_REMINDER_DAYS_BEFORE = 30
EXPIRY_CHECK_INTERVAL_SECONDS = 6 * 3600
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
            "name": "tourists",
            "table": "tourists",
            "order_by": "created_at DESC",
            "creatable": True,
            "title": "Туристы",
            "titleField": "full_name",
            "fields": [
                {"name": "full_name", "required": True, "label": "ФИО", "kind": "text", "list": True, "detail": True, "create": True},
                {"name": "passport_number", "required": True, "label": "Номер паспорта", "kind": "text", "list": False, "detail": True, "create": True},
                {"name": "passport_expiry", "required": True, "label": "Паспорт действителен до", "kind": "date", "list": True, "detail": True, "create": True},
                {"name": "visa_status", "label": "Статус визы", "kind": "status", "list": True, "detail": True, "create": True},
                {"name": "visa_expiry", "label": "Виза действительна до", "kind": "date", "list": False, "detail": True, "create": True},
                {"name": "tour_group_id", "label": "ID группы", "kind": "number", "list": False, "detail": True, "create": True},
                {"name": "notes", "label": "Заметки", "kind": "text", "list": False, "detail": True, "create": True},
                {"name": "created_at", "label": "Создан", "kind": "date", "list": False, "detail": True, "create": False},
            ],
        },
        {
            "name": "tour_groups",
            "table": "tour_groups",
            "order_by": "id DESC",
            "creatable": True,
            "title": "Группы",
            "titleField": "name",
            "fields": [
                {"name": "name", "required": True, "label": "Название", "kind": "text", "list": True, "detail": True, "create": True},
                {"name": "active", "label": "Активна", "kind": "bool", "list": True, "detail": True, "create": True},
            ],
        },
    ],
}

VISA_STATUS_LABELS = {
    "not_required": "🚫 Не требуется",
    "submitted":    "📤 Подана",
    "ready":        "✅ Готова",
    "rejected":     "❌ Отклонена",
}
MAX_GROUP_BUTTONS = 30
MAX_NAME_BUTTON_LEN = 45
TOURIST_NOT_FOUND_TEXT = "Турист не найден — возможно, уже удалён."
# Shared by both the AddTourist wizard and edit_field_apply, so the two flows
# can't silently drift apart on what's a valid value (clean-code review note).
FULL_NAME_LEN = (2, 200)
PASSPORT_NUMBER_LEN = (3, 50)
GROUP_NAME_LEN = (2, 60)
NOTES_MAX_LEN = 500
# Every persistent reply-keyboard label from kb_admin() — excluded from every
# free-text FSM entry step below (~F.text.in_(MENU_BUTTON_TEXTS)) alongside
# the existing ~F.text.startswith("/") guard. Review-found: without this, a
# menu-button tap while mid-flow (e.g. tapping "👥 Админы" while the bot is
# waiting for a new ФИО) matches the state's F.text filter just as well as
# real input and gets silently written as literal field data — for
# edit_field_apply/group_name_entry specifically, straight into an UPDATE/
# INSERT with no confirmation screen to catch it.
MENU_BUTTON_TEXTS = frozenset({
    "➕ Добавить туриста", "📋 Список туристов", "🗂 Группы/туры",
    "📜 Журнал просмотров", "👥 Админы",
})


def _esc(value, max_len: int = 500) -> str:
    """HTML-escapes AND length-bounds any user-supplied text before it goes into
    a parse_mode="HTML" message — same helper/rationale as every other
    template's _esc(): free-text ФИО/заметки/номер паспорта could otherwise
    contain '<'/'&' and break message rendering, or run long enough to hit
    Telegram's message-length limit."""
    text = str(value) if value is not None else ""
    if len(text) > max_len:
        text = text[:max_len] + "…"
    return html.escape(text)


def _join_bounded(lines: list[str], limit: int = 3500) -> str:
    """Joins lines with a length budget, dropping only WHOLE trailing lines —
    same approach as other templates' _join_bounded(): a raw character-offset
    slice can land inside an open HTML span and produce unbalanced HTML that
    Telegram's parse_mode="HTML" rejects outright."""
    out: list[str] = []
    total = 0
    for line in lines:
        if total + len(line) + 1 > limit:
            out.append("…")
            break
        out.append(line)
        total += len(line) + 1
    return "\n".join(out)


def _parse_date(text: str) -> str | None:
    """Accepts ДД.ММ.ГГГГ (and ./- variants) or ISO YYYY-MM-DD — same shape as
    trip_manager.py's _parse_date. Returns ISO or None."""
    text = text.strip()
    m = re.fullmatch(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})", text)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m = re.fullmatch(r"(\d{4})[.\-](\d{1,2})[.\-](\d{1,2})", text)
        if m:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        else:
            return None
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return None


def _len_ok(value: str, bounds: tuple[int, int]) -> bool:
    lo, hi = bounds
    return lo <= len(value) <= hi


def _fmt_date_ru(iso_date: str | None) -> str:
    if not iso_date:
        return "—"
    try:
        dt = datetime.strptime(iso_date, "%Y-%m-%d")
        return dt.strftime("%d.%m.%Y")
    except ValueError:
        return iso_date


# ── config ───────────────────────────────────────────────────────────────────
# Same pattern as every other template — see docs/STAGE2_DESIGN.md.

@dataclass
class TouristDocumentsConfig:
    bot_name: str
    db_path: str
    admins_file: Path
    welcome_image: Path
    display_name: str | None = None
    group_chat_id: str | None = None


def _paths_for(name: str, data_dir: Path) -> TouristDocumentsConfig:
    return TouristDocumentsConfig(
        bot_name=name,
        db_path=str(data_dir / f"{name}_data.db"),
        admins_file=data_dir / f"admins_{name}.json",
        welcome_image=data_dir / "bot_images" / f"{name}.jpg",
    )


def config_from_env() -> TouristDocumentsConfig:
    """Standalone/subprocess mode."""
    name = Path(__file__).stem
    data_dir = Path(os.getenv("DATA_DIR", "./data"))
    data_dir.mkdir(exist_ok=True)
    return _paths_for(name, data_dir)


def config_from_bot_row(bot_row: dict, data_dir: Path) -> TouristDocumentsConfig:
    """Webhook runtime mode. Paths built from bot_row["bot_id"] (bots.id, the
    physically unique AUTOINCREMENT PK) — NOT bot_row["name"] — same reasoning
    as every other template's config_from_bot_row (see docs/STAGE2_DESIGN.md
    "Изоляция по bots.id")."""
    bot_id = bot_row["bot_id"]
    config = TouristDocumentsConfig(
        bot_name=bot_row["name"],
        db_path=str(data_dir / f"bot_{bot_id}_data.db"),
        admins_file=data_dir / f"admins_{bot_id}.json",
        welcome_image=data_dir / "bot_images" / f"bot_{bot_id}.jpg",
    )
    config.display_name = bot_row.get("display_name")
    config.group_chat_id = bot_row.get("group_chat_id")
    return config


class ConfigMiddleware(BaseMiddleware):
    """Injects this bot's TouristDocumentsConfig into data["config"]."""

    def __init__(self, config: TouristDocumentsConfig) -> None:
        self.config = config
        super().__init__()

    async def __call__(self, handler, event, data):
        data["config"] = self.config
        return await handler(event, data)


# ── admin helpers ─────────────────────────────────────────────────────────────
# Deliberately the same admins_file/_load_admins pattern as every other
# template — no RBAC, no separate roles: anyone in admins_file is fully
# trusted staff (see design note: this template is staff-only, there is no
# customer-facing role at all).

def _load_admins(admins_file: Path) -> set:
    try:
        return set(json.loads(admins_file.read_text()).get("ids", []))
    except Exception:
        return set()

def _save_admins(admins_file: Path, ids: set) -> None:
    admins_file.write_text(json.dumps({"ids": list(ids)}, ensure_ascii=False))


def _is_admin(user_id: int, admins_file: Path) -> bool:
    return str(user_id) in _load_admins(admins_file)


# ── db ────────────────────────────────────────────────────────────────────────

async def init_db(db_path: str):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tour_groups (
                id     INTEGER PRIMARY KEY AUTOINCREMENT,
                name   TEXT NOT NULL,
                active INTEGER DEFAULT 1
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tourists (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                full_name             TEXT NOT NULL,
                passport_number       TEXT NOT NULL,
                passport_expiry       TEXT NOT NULL,
                visa_status           TEXT NOT NULL DEFAULT 'not_required',
                visa_expiry           TEXT,
                tour_group_id         INTEGER REFERENCES tour_groups(id),
                notes                 TEXT,
                passport_notified_at  TEXT,
                visa_notified_at      TEXT,
                created_at            TEXT DEFAULT (datetime('now','localtime')),
                updated_at            TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        # The audit trail this template exists to guarantee: every time an
        # admin opens a tourist's full document (passport number, exact
        # dates), one row is written here BEFORE the data is ever rendered —
        # see cb_open_doc below. Never exposes the document CONTENT itself,
        # only who looked and when (same class of boundary as booking_medical's
        # "карта пациента" but with a persisted log, per the owner's explicit
        # requirement).
        await db.execute("""
            CREATE TABLE IF NOT EXISTS document_view_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                tourist_id INTEGER NOT NULL REFERENCES tourists(id),
                admin_id   INTEGER NOT NULL,
                viewed_at  TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.commit()


async def _active_groups(db_path: str) -> list[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT id, name FROM tour_groups WHERE active=1 ORDER BY name"
        )).fetchall()
    return [dict(r) for r in rows]


def _needs_attention(row: dict, threshold_iso: str) -> bool:
    """Generic (deliberately non-specific) attention flag used ONLY in list
    views — see the privacy note on kb_tourist_list below for why this never
    reveals WHICH document or WHAT date."""
    if row["passport_expiry"] and row["passport_expiry"] <= threshold_iso:
        return True
    if row["visa_status"] == "ready" and row["visa_expiry"] and row["visa_expiry"] <= threshold_iso:
        return True
    return False


# ── keyboards ─────────────────────────────────────────────────────────────────

def kb_admin() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="➕ Добавить туриста")],
        [KeyboardButton(text="📋 Список туристов"), KeyboardButton(text="🗂 Группы/туры")],
        [KeyboardButton(text="📜 Журнал просмотров"), KeyboardButton(text="👥 Админы")],
    ], resize_keyboard=True)

def kb_cancel(callback_data: str = "td_cancel") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Отмена", callback_data=callback_data)
    ]])

def kb_visa_status() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=label, callback_data=f"td_visa:{code}")]
            for code, label in VISA_STATUS_LABELS.items()]
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="td_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_notes_skip() -> InlineKeyboardMarkup:
    # Review-found gap: this was the only FSM-entry keyboard in the file with
    # no "❌ Отмена" — an admin at this step had no button-driven way out.
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⏭ Пропустить", callback_data="td_notes_skip"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="td_cancel"),
    ]])

def kb_group_pick(groups: list[dict], prefix: str) -> InlineKeyboardMarkup:
    # Kept small enough to never risk Telegram's per-message inline-button
    # limits — same MAX_* bound convention as moderator.py's MAX_REMOVE_BUTTONS.
    rows = [[InlineKeyboardButton(text=f"🗂 {g['name']}", callback_data=f"{prefix}:{g['id']}")]
            for g in groups[:MAX_GROUP_BUTTONS]]
    rows.append([InlineKeyboardButton(text="➕ Новая группа", callback_data=f"{prefix}_new")])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="td_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_tourist_list(rows: list[dict], threshold_iso: str) -> InlineKeyboardMarkup:
    # Privacy: label is name-only (+ a generic flag) — never passport number,
    # never an exact date, never which document is the problem. Full detail
    # only ever renders behind the logged "🗂 Открыть документ" action.
    btns = []
    for r in rows:
        flag = "⚠️ " if _needs_attention(r, threshold_iso) else ""
        label = r["full_name"] if len(r["full_name"]) <= MAX_NAME_BUTTON_LEN else r["full_name"][:MAX_NAME_BUTTON_LEN - 3] + "…"
        btns.append([InlineKeyboardButton(text=f"{flag}{label}", callback_data=f"td_tourist:{r['id']}")])
    btns.append([InlineKeyboardButton(text="◀️ К группам", callback_data="td_list_back")])
    return InlineKeyboardMarkup(inline_keyboard=btns)

def kb_tourist_card(tourist_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗂 Открыть документ", callback_data=f"td_open_doc:{tourist_id}")],
        [InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"td_edit:{tourist_id}"),
         InlineKeyboardButton(text="🗑 Удалить", callback_data=f"td_del:{tourist_id}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="td_list_back")],
    ])

def kb_confirm_delete(tourist_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"td_del_ok:{tourist_id}"),
        InlineKeyboardButton(text="◀️ Отмена", callback_data=f"td_tourist:{tourist_id}"),
    ]])

def kb_confirm_add() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Сохранить", callback_data="td_add_confirm"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="td_cancel"),
    ]])

EDIT_FIELD_LABELS = {
    "full_name":       ("✏️ ФИО", "Пришлите новое ФИО:"),
    "passport_number": ("🛂 Номер паспорта", "Пришлите новый номер паспорта:"),
    "passport_expiry": ("📅 Срок паспорта", "Пришлите новую дату окончания паспорта (ДД.ММ.ГГГГ):"),
    "visa_expiry":     ("📅 Срок визы", "Пришлите новую дату окончания визы (ДД.ММ.ГГГГ):"),
    "notes":           ("📝 Заметки", "Пришлите новые заметки:"),
}

def kb_edit_fields(tourist_id: int) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=label, callback_data=f"td_edit_f:{tourist_id}:{field}")]
            for field, (label, _prompt) in EDIT_FIELD_LABELS.items()]
    rows.append([InlineKeyboardButton(text="🛂 Статус визы", callback_data=f"td_edit_visa:{tourist_id}")])
    rows.append([InlineKeyboardButton(text="🗂 Группа", callback_data=f"td_edit_group:{tourist_id}")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"td_tourist:{tourist_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_edit_visa_status(tourist_id: int) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=label, callback_data=f"td_edit_visa_set:{tourist_id}:{code}")]
            for code, label in VISA_STATUS_LABELS.items()]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"td_edit:{tourist_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_groups_panel(groups: list[dict]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"🗂 {g['name']}", callback_data="td_noop"),
             InlineKeyboardButton(text="🚫 Деактивировать", callback_data=f"td_group_deact:{g['id']}")]
            for g in groups]
    rows.append([InlineKeyboardButton(text="➕ Добавить группу", callback_data="td_group_add")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_admins_panel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить администратора", callback_data="td_admin_add")],
        [InlineKeyboardButton(text="➖ Убрать администратора", callback_data="td_admin_remove")],
    ])

def kb_admin_remove_list(ids: list[str]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"➖ {i}", callback_data=f"td_admin_rm:{i}")] for i in ids]
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="td_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── FSM ───────────────────────────────────────────────────────────────────────

class AddTourist(StatesGroup):
    full_name = State(); passport_number = State(); passport_expiry = State()
    visa_status = State(); visa_expiry = State()
    group_pick = State(); new_group_name = State()
    notes = State(); confirm = State()

class EditTourist(StatesGroup):
    value = State()

class GroupNameEntry(StatesGroup):
    name = State()

class AdminFlow(StatesGroup):
    add_id = State()


@router.message(Command("cancel"), StateFilter("*"))
async def cmd_cancel(msg: Message, state: FSMContext):
    if await state.get_state() is None:
        await msg.answer("Нечего отменять.")
        return
    await state.clear()
    await msg.answer("Отменено.", reply_markup=kb_admin())

@router.callback_query(F.data == "td_cancel", StateFilter("*"))
async def cb_cancel(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await cb.message.edit_text("Отменено.")

@router.callback_query(F.data == "td_noop")
async def cb_noop(cb: CallbackQuery):
    await cb.answer()


# ── /start ────────────────────────────────────────────────────────────────────

@router.message(Command("start"))
async def cmd_start(message: Message, config: TouristDocumentsConfig):
    admins = _load_admins(config.admins_file)
    first_time_admin = not admins
    if first_time_admin:
        _save_admins(config.admins_file, {str(message.from_user.id)})
    is_admin = first_time_admin or _is_admin(message.from_user.id, config.admins_file)

    if not is_admin:
        await message.answer(NOT_ADMIN_TEXT)
        return

    if config.welcome_image.exists():
        await message.answer_photo(FSInputFile(str(config.welcome_image)),
                                    caption=WELCOME_TEXT, parse_mode="HTML", reply_markup=kb_admin())
    else:
        await message.answer(WELCOME_TEXT, parse_mode="HTML", reply_markup=kb_admin())
    if first_time_admin:
        await message.answer(
            "👑 <b>Вы — администратор этого бота.</b>\n\n"
            "Все действия доступны через кнопки меню ниже.",
            parse_mode="HTML",
        )


# ── ADD TOURIST ───────────────────────────────────────────────────────────────

@router.message(F.text == "➕ Добавить туриста")
async def add_tourist_start(msg: Message, state: FSMContext, config: TouristDocumentsConfig):
    if not _is_admin(msg.from_user.id, config.admins_file):
        await msg.answer(NOT_ADMIN_TEXT); return
    await state.set_state(AddTourist.full_name)
    await msg.answer("👤 Пришлите ФИО туриста:", reply_markup=kb_cancel())

@router.message(AddTourist.full_name, F.text, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTON_TEXTS))
async def add_full_name(msg: Message, state: FSMContext, config: TouristDocumentsConfig):
    # Every FSM-continuation handler in this flow re-checks admin status, not
    # just the entry point (add_tourist_start) — review-found gap: without
    # this, a user demoted mid-flow (or someone who kept a stale message from
    # when they WERE admin) could still complete privileged writes further
    # down the wizard. state.clear() removes the stale data too, closing the
    # "keep an old button around" variant of the same issue.
    if not _is_admin(msg.from_user.id, config.admins_file):
        await state.clear(); await msg.answer(NOT_ADMIN_TEXT); return
    name = msg.text.strip()
    if not _len_ok(name, FULL_NAME_LEN):
        await msg.answer("⚠️ ФИО должно быть от 2 до 200 символов. Повторите:"); return
    await state.update_data(full_name=name)
    await state.set_state(AddTourist.passport_number)
    await msg.answer("🛂 Пришлите номер паспорта:", reply_markup=kb_cancel())

@router.message(AddTourist.passport_number, F.text, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTON_TEXTS))
async def add_passport_number(msg: Message, state: FSMContext, config: TouristDocumentsConfig):
    if not _is_admin(msg.from_user.id, config.admins_file):
        await state.clear(); await msg.answer(NOT_ADMIN_TEXT); return
    number = msg.text.strip()
    if not _len_ok(number, PASSPORT_NUMBER_LEN):
        await msg.answer("⚠️ Номер паспорта должен быть от 3 до 50 символов. Повторите:"); return
    await state.update_data(passport_number=number)
    await state.set_state(AddTourist.passport_expiry)
    await msg.answer("📅 Пришлите дату окончания действия паспорта (например: 25.07.2030):", reply_markup=kb_cancel())

# No ~F.text.in_(MENU_BUTTON_TEXTS) guard here (unlike full_name/passport_number/
# notes/group_name_entry/edit_field_apply) — deliberately, not an oversight:
# _parse_date() below already rejects any menu-button label as a side effect
# of it not being a valid date, so the extra filter would be redundant.
@router.message(AddTourist.passport_expiry, F.text, ~F.text.startswith("/"))
async def add_passport_expiry(msg: Message, state: FSMContext, config: TouristDocumentsConfig):
    if not _is_admin(msg.from_user.id, config.admins_file):
        await state.clear(); await msg.answer(NOT_ADMIN_TEXT); return
    parsed = _parse_date(msg.text)
    if not parsed:
        await msg.answer("⚠️ Неверный формат даты. Пришлите в формате 25.07.2030:"); return
    await state.update_data(passport_expiry=parsed)
    await state.set_state(AddTourist.visa_status)
    await msg.answer("🛂 Статус визы:", reply_markup=kb_visa_status())

@router.callback_query(AddTourist.visa_status, F.data.startswith("td_visa:"))
async def add_visa_status(cb: CallbackQuery, state: FSMContext, config: TouristDocumentsConfig):
    if not _is_admin(cb.from_user.id, config.admins_file):
        await cb.answer("⛔ Нет доступа", show_alert=True); await state.clear(); return
    await cb.answer()
    status = cb.data.split(":", 1)[1]
    if status not in VISA_STATUS_LABELS:
        return
    await state.update_data(visa_status=status)
    if status == "ready":
        await state.set_state(AddTourist.visa_expiry)
        # Review-found: the only AddTourist prompt missing a Cancel button —
        # /cancel still works, but this screen dropped the inline affordance
        # every other step in the wizard has.
        await cb.message.edit_text("📅 Пришлите дату окончания действия визы (например: 25.07.2027):", reply_markup=kb_cancel())
        return
    await state.update_data(visa_expiry=None)
    await _ask_group(cb.message, state, config, answer_only=True)

# Same reasoning as add_passport_expiry above — _parse_date() rejects any
# menu-button text, so MENU_BUTTON_TEXTS would be redundant here too.
@router.message(AddTourist.visa_expiry, F.text, ~F.text.startswith("/"))
async def add_visa_expiry(msg: Message, state: FSMContext, config: TouristDocumentsConfig):
    if not _is_admin(msg.from_user.id, config.admins_file):
        await state.clear(); await msg.answer(NOT_ADMIN_TEXT); return
    parsed = _parse_date(msg.text)
    if not parsed:
        await msg.answer("⚠️ Неверный формат даты. Пришлите в формате 25.07.2027:"); return
    await state.update_data(visa_expiry=parsed)
    await _ask_group(msg, state, config, answer_only=True)


async def _ask_group(msg: Message, state: FSMContext, config: TouristDocumentsConfig, *, answer_only: bool) -> None:
    groups = await _active_groups(config.db_path)
    await state.set_state(AddTourist.group_pick)
    text = "🗂 Выберите группу/тур:" if groups else "🗂 Групп пока нет — создайте первую:"
    kb = kb_group_pick(groups, "td_group")
    if answer_only:
        await msg.answer(text, reply_markup=kb)
    else:
        await msg.edit_text(text, reply_markup=kb)

@router.callback_query(AddTourist.group_pick, F.data.startswith("td_group:"))
async def add_group_pick(cb: CallbackQuery, state: FSMContext, config: TouristDocumentsConfig):
    if not _is_admin(cb.from_user.id, config.admins_file):
        await cb.answer("⛔ Нет доступа", show_alert=True); await state.clear(); return
    await cb.answer()
    group_id = int(cb.data.split(":", 1)[1])
    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute("SELECT name FROM tour_groups WHERE id=? AND active=1", (group_id,))).fetchone()
    if not row:
        await cb.answer("Эта группа больше не активна.", show_alert=True); return
    await state.update_data(tour_group_id=group_id, tour_group_name=row[0])
    await state.set_state(AddTourist.notes)
    await cb.message.edit_text("📝 Заметки (необязательно) — пришлите текст или нажмите «Пропустить»:", reply_markup=kb_notes_skip())

@router.callback_query(AddTourist.group_pick, F.data == "td_group_new")
async def add_group_new(cb: CallbackQuery, state: FSMContext, config: TouristDocumentsConfig):
    if not _is_admin(cb.from_user.id, config.admins_file):
        await cb.answer("⛔ Нет доступа", show_alert=True); await state.clear(); return
    await cb.answer()
    await state.update_data(group_ctx="add_tourist")
    await state.set_state(GroupNameEntry.name)
    await cb.message.edit_text("🗂 Пришлите название новой группы/тура:", reply_markup=kb_cancel())

@router.callback_query(AddTourist.notes, F.data == "td_notes_skip")
async def add_notes_skip(cb: CallbackQuery, state: FSMContext, config: TouristDocumentsConfig):
    # Review-found: this had NO state filter at all before — reachable via a
    # stale button from an old message at any later time (even after losing
    # admin access), since it wasn't gated to AddTourist.notes like every
    # sibling handler in this flow. Now scoped + re-checked like the rest.
    if not _is_admin(cb.from_user.id, config.admins_file):
        await cb.answer("⛔ Нет доступа", show_alert=True); await state.clear(); return
    await cb.answer()
    await state.update_data(notes="")
    await _show_add_confirmation(cb.message, state, answer_only=False)

@router.message(AddTourist.notes, F.text, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTON_TEXTS))
async def add_notes(msg: Message, state: FSMContext, config: TouristDocumentsConfig):
    if not _is_admin(msg.from_user.id, config.admins_file):
        await state.clear(); await msg.answer(NOT_ADMIN_TEXT); return
    notes = msg.text.strip()
    if len(notes) > NOTES_MAX_LEN:
        await msg.answer("⚠️ Заметки должны быть короче 500 символов. Повторите:"); return
    await state.update_data(notes=notes)
    await _show_add_confirmation(msg, state, answer_only=True)


async def _show_add_confirmation(msg: Message, state: FSMContext, *, answer_only: bool) -> None:
    data = await state.get_data()
    text = (
        f"📋 <b>Подтвердите данные туриста:</b>\n\n"
        f"👤 {_esc(data.get('full_name', ''))}\n"
        f"🛂 Паспорт: {_esc(data.get('passport_number', ''))}\n"
        f"📅 Срок паспорта: {_fmt_date_ru(data.get('passport_expiry'))}\n"
        f"🛂 Виза: {VISA_STATUS_LABELS.get(data.get('visa_status', 'not_required'))}\n"
    )
    if data.get("visa_expiry"):
        text += f"📅 Срок визы: {_fmt_date_ru(data.get('visa_expiry'))}\n"
    text += f"🗂 Группа: {_esc(data.get('tour_group_name', '—'))}\n"
    if data.get("notes"):
        text += f"📝 Заметки: {_esc(data.get('notes'))}\n"
    await state.set_state(AddTourist.confirm)
    if answer_only:
        await msg.answer(text, parse_mode="HTML", reply_markup=kb_confirm_add())
    else:
        await msg.edit_text(text, parse_mode="HTML", reply_markup=kb_confirm_add())

@router.callback_query(AddTourist.confirm, F.data == "td_add_confirm")
async def add_confirm(cb: CallbackQuery, state: FSMContext, config: TouristDocumentsConfig):
    if not _is_admin(cb.from_user.id, config.admins_file):
        await cb.answer("⛔ Нет доступа", show_alert=True); await state.clear(); return
    await cb.answer()
    data = await state.get_data()
    # Review-found: clearing state BEFORE the INSERT (not after) narrows the
    # double-tap window — aiogram doesn't serialize updates for the same chat
    # by default, so two near-simultaneous presses of "✅ Сохранить" could
    # otherwise both pass the AddTourist.confirm state filter and both INSERT
    # a duplicate tourist row with real passport data. Not a full fix (the
    # two dispatches can still both read the pre-clear state before either
    # writes), but the same class of gap already exists in trip_manager.py's
    # add-item confirm and isn't solved there either — narrowing it here is
    # cheap and strictly better, a real fix would need a DB-level uniqueness
    # guard which is out of scope for this template.
    await state.clear()
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute(
            "INSERT INTO tourists (full_name, passport_number, passport_expiry, visa_status, "
            "visa_expiry, tour_group_id, notes) VALUES (?,?,?,?,?,?,?)",
            (data.get("full_name", ""), data.get("passport_number", ""), data.get("passport_expiry"),
             data.get("visa_status", "not_required"), data.get("visa_expiry"),
             data.get("tour_group_id"), data.get("notes") or None)
        )
        await db.commit()
    await cb.message.edit_text(f"✅ Турист <b>{_esc(data.get('full_name', ''))}</b> добавлен.", parse_mode="HTML")
    await cb.message.answer("Что дальше?", reply_markup=kb_admin())


# ── GROUP NAME ENTRY (shared: standalone panel + inline "новая группа") ──────

@router.message(GroupNameEntry.name, F.text, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTON_TEXTS))
async def group_name_entry(msg: Message, state: FSMContext, config: TouristDocumentsConfig):
    if not _is_admin(msg.from_user.id, config.admins_file):
        await state.clear(); await msg.answer(NOT_ADMIN_TEXT); return
    name = msg.text.strip()
    if not _len_ok(name, GROUP_NAME_LEN):
        await msg.answer("⚠️ Название должно быть от 2 до 60 символов. Повторите:"); return
    async with aiosqlite.connect(config.db_path) as db:
        cur = await db.execute("INSERT INTO tour_groups (name) VALUES (?)", (name,))
        await db.commit()
        new_id = cur.lastrowid

    data = await state.get_data()
    ctx = data.get("group_ctx")
    if ctx == "add_tourist":
        # Keep the rest of the in-progress AddTourist data (full_name,
        # passport fields, etc.) intact — only the STATE pointer moves;
        # update_data merges rather than replaces.
        await state.update_data(tour_group_id=new_id, tour_group_name=name)
        await state.set_state(AddTourist.notes)
        await msg.answer(f"✅ Группа «{_esc(name)}» создана и выбрана.", parse_mode="HTML")
        await msg.answer("📝 Заметки (необязательно) — пришлите текст или нажмите «Пропустить»:", reply_markup=kb_notes_skip())
        return

    await state.clear()
    await msg.answer(f"✅ Группа «{_esc(name)}» создана.", parse_mode="HTML")
    groups = await _active_groups(config.db_path)
    await msg.answer("🗂 <b>Группы/туры:</b>", parse_mode="HTML", reply_markup=kb_groups_panel(groups))


# ── GROUPS PANEL ──────────────────────────────────────────────────────────────

@router.message(F.text == "🗂 Группы/туры")
async def groups_panel(msg: Message, config: TouristDocumentsConfig):
    if not _is_admin(msg.from_user.id, config.admins_file):
        await msg.answer(NOT_ADMIN_TEXT); return
    groups = await _active_groups(config.db_path)
    text = "🗂 <b>Группы/туры:</b>" if groups else "Групп пока нет."
    await msg.answer(text, parse_mode="HTML", reply_markup=kb_groups_panel(groups))

@router.callback_query(F.data == "td_group_add")
async def group_add_start(cb: CallbackQuery, state: FSMContext, config: TouristDocumentsConfig):
    if not _is_admin(cb.from_user.id, config.admins_file):
        await cb.answer("⛔ Нет доступа", show_alert=True); return
    await cb.answer()
    await state.update_data(group_ctx="standalone")
    await state.set_state(GroupNameEntry.name)
    await cb.message.edit_text("🗂 Пришлите название новой группы/тура:", reply_markup=kb_cancel())

@router.callback_query(F.data.startswith("td_group_deact:"))
async def group_deactivate(cb: CallbackQuery, config: TouristDocumentsConfig):
    if not _is_admin(cb.from_user.id, config.admins_file):
        await cb.answer("⛔ Нет доступа", show_alert=True); return
    await cb.answer()
    group_id = int(cb.data.split(":", 1)[1])
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute("UPDATE tour_groups SET active=0 WHERE id=?", (group_id,))
        await db.commit()
    groups = await _active_groups(config.db_path)
    text = "🗂 <b>Группы/туры:</b>" if groups else "Групп пока нет."
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_groups_panel(groups))


# ── TOURIST LIST (privacy: name + generic flag only — see kb_tourist_list) ───

async def _groups_pick_markup(db_path: str) -> tuple[InlineKeyboardMarkup, bool]:
    """Shared by list_tourists_start and list_back (clean-code review note:
    these two were a near-verbatim duplicate of this exact query+keyboard).
    The bool tells the caller whether there's anything to show at all — only
    list_tourists_start's first-ever view needs that to render an empty-state
    message instead of an empty keyboard."""
    groups = await _active_groups(db_path)
    async with aiosqlite.connect(db_path) as db:
        ungrouped = (await (await db.execute(
            "SELECT COUNT(*) FROM tourists WHERE tour_group_id IS NULL"
        )).fetchone())[0]
    rows = [[InlineKeyboardButton(text=f"🗂 {g['name']}", callback_data=f"td_list_group:{g['id']}")] for g in groups]
    if ungrouped:
        rows.append([InlineKeyboardButton(text=f"👤 Без группы ({ungrouped})", callback_data="td_list_group:none")])
    return InlineKeyboardMarkup(inline_keyboard=rows), bool(groups or ungrouped)

@router.message(F.text == "📋 Список туристов")
async def list_tourists_start(msg: Message, config: TouristDocumentsConfig):
    if not _is_admin(msg.from_user.id, config.admins_file):
        await msg.answer(NOT_ADMIN_TEXT); return
    kb, has_any = await _groups_pick_markup(config.db_path)
    if not has_any:
        await msg.answer("Список туристов пуст. Добавьте туриста кнопкой «➕ Добавить туриста»."); return
    await msg.answer("🗂 Выберите группу:", reply_markup=kb)

@router.callback_query(F.data.startswith("td_list_group:"))
async def list_tourists_by_group(cb: CallbackQuery, config: TouristDocumentsConfig):
    if not _is_admin(cb.from_user.id, config.admins_file):
        await cb.answer("⛔ Нет доступа", show_alert=True); return
    await cb.answer()
    raw = cb.data.split(":", 1)[1]
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        if raw == "none":
            rows = await (await db.execute(
                "SELECT id, full_name, passport_expiry, visa_status, visa_expiry "
                "FROM tourists WHERE tour_group_id IS NULL ORDER BY full_name"
            )).fetchall()
        else:
            rows = await (await db.execute(
                "SELECT id, full_name, passport_expiry, visa_status, visa_expiry "
                "FROM tourists WHERE tour_group_id=? ORDER BY full_name", (int(raw),)
            )).fetchall()
    if not rows:
        await cb.message.edit_text("В этой группе туристов пока нет.")
        return
    threshold = (date.today() + timedelta(days=EXPIRY_REMINDER_DAYS_BEFORE)).isoformat()
    await cb.message.edit_text("👤 Выберите туриста:", reply_markup=kb_tourist_list([dict(r) for r in rows], threshold))

@router.callback_query(F.data == "td_list_back")
async def list_back(cb: CallbackQuery, config: TouristDocumentsConfig):
    await cb.answer()
    kb, _has_any = await _groups_pick_markup(config.db_path)
    await cb.message.edit_text("🗂 Выберите группу:", reply_markup=kb)


# ── TOURIST CARD (summary only — no passport/visa specifics, see doc open) ───

@router.callback_query(F.data.startswith("td_tourist:"))
async def tourist_card(cb: CallbackQuery, config: TouristDocumentsConfig):
    if not _is_admin(cb.from_user.id, config.admins_file):
        await cb.answer("⛔ Нет доступа", show_alert=True); return
    await cb.answer()
    tourist_id = int(cb.data.split(":", 1)[1])
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            "SELECT t.id, t.full_name, t.passport_expiry, t.visa_status, t.visa_expiry, "
            "g.name AS group_name "
            "FROM tourists t LEFT JOIN tour_groups g ON t.tour_group_id = g.id WHERE t.id=?",
            (tourist_id,)
        )).fetchone()
    if not row:
        await cb.message.edit_text(TOURIST_NOT_FOUND_TEXT)
        return
    row = dict(row)
    threshold = (date.today() + timedelta(days=EXPIRY_REMINDER_DAYS_BEFORE)).isoformat()
    flag_text = "⚠️ Требует внимания — откройте документ" if _needs_attention(row, threshold) else "✅ Всё в порядке"
    text = (
        f"👤 <b>{_esc(row['full_name'])}</b>\n"
        f"🗂 Группа: {_esc(row['group_name']) or '—'}\n"
        f"{flag_text}\n\n"
        f"Паспортные и визовые данные скрыты — откройте документ, чтобы посмотреть."
    )
    try:
        await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_tourist_card(tourist_id))
    except Exception as e:
        logger.error(f"tourist_card failed to render: {e}")


@router.callback_query(F.data.startswith("td_open_doc:"))
async def open_document(cb: CallbackQuery, config: TouristDocumentsConfig):
    """The ONE place passport_number/visa_status/exact dates render — logs the
    viewer + timestamp to document_view_log BEFORE the data is shown, per the
    owner's explicit audit requirement."""
    if not _is_admin(cb.from_user.id, config.admins_file):
        await cb.answer("⛔ Нет доступа", show_alert=True); return
    await cb.answer()
    tourist_id = int(cb.data.split(":", 1)[1])
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            "SELECT t.*, g.name AS group_name FROM tourists t "
            "LEFT JOIN tour_groups g ON t.tour_group_id = g.id WHERE t.id=?", (tourist_id,)
        )).fetchone()
        if not row:
            await cb.message.answer(TOURIST_NOT_FOUND_TEXT)
            return
        await db.execute(
            "INSERT INTO document_view_log (tourist_id, admin_id) VALUES (?,?)",
            (tourist_id, cb.from_user.id)
        )
        await db.commit()
    row = dict(row)
    text = (
        f"🗂 <b>Документ туриста</b>\n\n"
        f"👤 {_esc(row['full_name'])}\n"
        f"🗂 Группа: {_esc(row['group_name']) or '—'}\n"
        f"🛂 Паспорт: <code>{_esc(row['passport_number'])}</code>\n"
        f"📅 Срок паспорта: {_fmt_date_ru(row['passport_expiry'])}\n"
        f"🛂 Виза: {VISA_STATUS_LABELS.get(row['visa_status'], row['visa_status'])}\n"
        f"📅 Срок визы: {_fmt_date_ru(row['visa_expiry'])}\n"
    )
    if row.get("notes"):
        text += f"📝 Заметки: {_esc(row['notes'])}\n"
    try:
        await cb.message.answer(text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"open_document failed to send: {e}")
        await cb.message.answer("⚠️ Не удалось показать документ.")


# ── DELETE TOURIST ────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("td_del:"))
async def delete_tourist_confirm(cb: CallbackQuery, config: TouristDocumentsConfig):
    if not _is_admin(cb.from_user.id, config.admins_file):
        await cb.answer("⛔ Нет доступа", show_alert=True); return
    await cb.answer()
    tourist_id = int(cb.data.split(":", 1)[1])
    await cb.message.edit_text("🗑 Удалить туриста? Это действие необратимо.", reply_markup=kb_confirm_delete(tourist_id))

@router.callback_query(F.data.startswith("td_del_ok:"))
async def delete_tourist(cb: CallbackQuery, config: TouristDocumentsConfig):
    if not _is_admin(cb.from_user.id, config.admins_file):
        await cb.answer("⛔ Нет доступа", show_alert=True); return
    await cb.answer()
    tourist_id = int(cb.data.split(":", 1)[1])
    async with aiosqlite.connect(config.db_path) as db:
        # Child before parent — harmless under SQLite (no PRAGMA foreign_keys
        # here), but review-found: reversing this order would raise a
        # foreign-key violation under a stricter engine (e.g. Postgres),
        # since document_view_log.tourist_id REFERENCES tourists(id).
        await db.execute("DELETE FROM document_view_log WHERE tourist_id=?", (tourist_id,))
        cur = await db.execute("DELETE FROM tourists WHERE id=?", (tourist_id,))
        await db.commit()
    if cur.rowcount == 0:
        # Review-found: same false-confirmation class as edit_field_apply —
        # another admin already deleted this tourist while this one had the
        # confirm screen open.
        await cb.message.edit_text(TOURIST_NOT_FOUND_TEXT)
        return
    await cb.message.edit_text("✅ Турист удалён.")


# ── EDIT TOURIST ──────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("td_edit:"))
async def edit_menu(cb: CallbackQuery, state: FSMContext, config: TouristDocumentsConfig):
    if not _is_admin(cb.from_user.id, config.admins_file):
        await cb.answer("⛔ Нет доступа", show_alert=True); return
    await cb.answer()
    # Review-found blocker: edit_field_start/edit_visa_set point their "❌
    # Отмена" button here (not at the generic td_cancel) — without clearing
    # state, an admin who cancels mid single-field edit stays parked in
    # EditTourist.value with a stale edit_field/edit_tourist_id, and their
    # NEXT unrelated text message (anything not starting with "/") gets
    # silently written into that tourist's record by edit_field_apply. Safe
    # to clear unconditionally: this handler is also reached with no active
    # state (from the tourist card's own "✏️ Редактировать" button), where
    # clearing a no-op state is a harmless no-op.
    await state.clear()
    tourist_id = int(cb.data.split(":", 1)[1])
    await cb.message.edit_text("✏️ Что изменить?", reply_markup=kb_edit_fields(tourist_id))

@router.callback_query(F.data.startswith("td_edit_f:"))
async def edit_field_start(cb: CallbackQuery, state: FSMContext, config: TouristDocumentsConfig):
    if not _is_admin(cb.from_user.id, config.admins_file):
        await cb.answer("⛔ Нет доступа", show_alert=True); return
    await cb.answer()
    _, tourist_id_s, field = cb.data.split(":", 2)
    tourist_id = int(tourist_id_s)
    _label, prompt = EDIT_FIELD_LABELS[field]
    await state.set_state(EditTourist.value)
    await state.update_data(edit_tourist_id=tourist_id, edit_field=field)
    await cb.message.edit_text(prompt, reply_markup=kb_cancel(f"td_edit:{tourist_id}"))

@router.message(EditTourist.value, F.text, ~F.text.startswith("/"), ~F.text.in_(MENU_BUTTON_TEXTS))
async def edit_field_apply(msg: Message, state: FSMContext, config: TouristDocumentsConfig):
    data = await state.get_data()
    field = data["edit_field"]
    tourist_id = data["edit_tourist_id"]
    value = msg.text.strip()

    if field in ("passport_expiry", "visa_expiry"):
        parsed = _parse_date(value)
        if not parsed:
            await msg.answer("⚠️ Неверный формат даты. Пришлите в формате 25.07.2030:"); return
        value = parsed
    elif field == "full_name":
        if not _len_ok(value, FULL_NAME_LEN):
            await msg.answer("⚠️ ФИО должно быть от 2 до 200 символов. Повторите:"); return
    elif field == "passport_number":
        if not _len_ok(value, PASSPORT_NUMBER_LEN):
            await msg.answer("⚠️ Номер паспорта должен быть от 3 до 50 символов. Повторите:"); return
    elif field == "notes":
        if len(value) > NOTES_MAX_LEN:
            await msg.answer("⚠️ Заметки должны быть короче 500 символов. Повторите:"); return

    # field is one of the fixed EDIT_FIELD_LABELS keys checked above — never
    # user-controlled — so building the column name into the SQL text here
    # cannot be used for injection; parameter binding is still used for value.
    if field == "passport_expiry":
        sql = "UPDATE tourists SET passport_expiry=?, passport_notified_at=NULL, updated_at=datetime('now','localtime') WHERE id=?"
    elif field == "visa_expiry":
        sql = "UPDATE tourists SET visa_expiry=?, visa_notified_at=NULL, updated_at=datetime('now','localtime') WHERE id=?"
    elif field == "full_name":
        sql = "UPDATE tourists SET full_name=?, updated_at=datetime('now','localtime') WHERE id=?"
    elif field == "passport_number":
        sql = "UPDATE tourists SET passport_number=?, updated_at=datetime('now','localtime') WHERE id=?"
    else:
        sql = "UPDATE tourists SET notes=?, updated_at=datetime('now','localtime') WHERE id=?"

    async with aiosqlite.connect(config.db_path) as db:
        cur = await db.execute(sql, (value, tourist_id))
        await db.commit()

    await state.clear()
    if cur.rowcount == 0:
        # Review-found: an UPDATE affecting 0 rows (tourist deleted by
        # another admin while this one had the edit prompt open) used to
        # still report "✅ Изменено." — a false confirmation of a write that
        # never happened.
        await msg.answer(TOURIST_NOT_FOUND_TEXT)
        return
    await msg.answer("✅ Изменено.")


@router.callback_query(F.data.startswith("td_edit_visa:"))
async def edit_visa_start(cb: CallbackQuery, config: TouristDocumentsConfig):
    if not _is_admin(cb.from_user.id, config.admins_file):
        await cb.answer("⛔ Нет доступа", show_alert=True); return
    await cb.answer()
    tourist_id = int(cb.data.split(":", 1)[1])
    await cb.message.edit_text("🛂 Новый статус визы:", reply_markup=kb_edit_visa_status(tourist_id))

@router.callback_query(F.data.startswith("td_edit_visa_set:"))
async def edit_visa_set(cb: CallbackQuery, state: FSMContext, config: TouristDocumentsConfig):
    if not _is_admin(cb.from_user.id, config.admins_file):
        await cb.answer("⛔ Нет доступа", show_alert=True); return
    await cb.answer()
    _, tourist_id_s, status = cb.data.split(":", 2)
    tourist_id = int(tourist_id_s)
    async with aiosqlite.connect(config.db_path) as db:
        if status == "ready":
            await db.execute("UPDATE tourists SET visa_status=? WHERE id=?", (status, tourist_id))
        else:
            # Leaving "ready" (or was never ready) — an expiry date without an
            # issued visa is meaningless, and clearing it also clears the
            # notified flag naturally (fresh NULL, nothing to fire on).
            await db.execute(
                "UPDATE tourists SET visa_status=?, visa_expiry=NULL, visa_notified_at=NULL WHERE id=?",
                (status, tourist_id)
            )
        await db.commit()
    if status == "ready":
        await state.set_state(EditTourist.value)
        await state.update_data(edit_tourist_id=tourist_id, edit_field="visa_expiry")
        await cb.message.edit_text("📅 Пришлите дату окончания действия визы (например: 25.07.2027):", reply_markup=kb_cancel(f"td_edit:{tourist_id}"))
        return
    await cb.message.edit_text("✅ Статус визы изменён.")


@router.callback_query(F.data.startswith("td_edit_group:"))
async def edit_group_start(cb: CallbackQuery, config: TouristDocumentsConfig):
    if not _is_admin(cb.from_user.id, config.admins_file):
        await cb.answer("⛔ Нет доступа", show_alert=True); return
    await cb.answer()
    tourist_id = int(cb.data.split(":", 1)[1])
    groups = await _active_groups(config.db_path)
    if not groups:
        await cb.message.edit_text(
            "Активных групп нет — создайте группу через «🗂 Группы/туры», затем вернитесь сюда.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="◀️ Назад", callback_data=f"td_edit:{tourist_id}")
            ]])
        )
        return
    rows = [[InlineKeyboardButton(text=f"🗂 {g['name']}", callback_data=f"td_edit_group_set:{tourist_id}:{g['id']}")] for g in groups]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"td_edit:{tourist_id}")])
    await cb.message.edit_text("🗂 Выберите новую группу:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

@router.callback_query(F.data.startswith("td_edit_group_set:"))
async def edit_group_set(cb: CallbackQuery, config: TouristDocumentsConfig):
    if not _is_admin(cb.from_user.id, config.admins_file):
        await cb.answer("⛔ Нет доступа", show_alert=True); return
    await cb.answer()
    _, tourist_id_s, group_id_s = cb.data.split(":", 2)
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute("UPDATE tourists SET tour_group_id=? WHERE id=?", (int(group_id_s), int(tourist_id_s)))
        await db.commit()
    await cb.message.edit_text("✅ Группа изменена.")


# ── VIEW LOG (admin-only, metadata only — never document content) ────────────

@router.message(F.text == "📜 Журнал просмотров")
async def view_log(msg: Message, config: TouristDocumentsConfig):
    if not _is_admin(msg.from_user.id, config.admins_file):
        await msg.answer(NOT_ADMIN_TEXT); return
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT l.admin_id, l.viewed_at, t.full_name FROM document_view_log l "
            "JOIN tourists t ON l.tourist_id = t.id ORDER BY l.id DESC LIMIT 30"
        )).fetchall()
    if not rows:
        await msg.answer("Журнал просмотров пуст."); return
    lines = ["📜 <b>Журнал просмотров (последние 30):</b>\n"]
    for r in rows:
        lines.append(f"{r['viewed_at']} · администратор {r['admin_id']} открыл документ {_esc(r['full_name'])}")
    await msg.answer(_join_bounded(lines), parse_mode="HTML")


# ── ADMINS PANEL ──────────────────────────────────────────────────────────────

@router.message(F.text == "👥 Админы")
async def admins_panel(msg: Message, config: TouristDocumentsConfig):
    if not _is_admin(msg.from_user.id, config.admins_file):
        await msg.answer(NOT_ADMIN_TEXT); return
    ids = _load_admins(config.admins_file)
    # _join_bounded (not a plain join) — defensive against a pre-existing
    # admins_file entry from before admin_add_apply's length bound existed;
    # an unbounded id string here would otherwise produce a message over
    # Telegram's ~4096-char limit and permanently break this panel for
    # everyone (review-found).
    text = "👥 " + (_join_bounded([f"• <code>{_esc(i)}</code>" for i in ids]) or "Пусто")
    await msg.answer(text, parse_mode="HTML", reply_markup=kb_admins_panel())

@router.callback_query(F.data == "td_admin_add")
async def admin_add_start(cb: CallbackQuery, state: FSMContext, config: TouristDocumentsConfig):
    if not _is_admin(cb.from_user.id, config.admins_file):
        await cb.answer("⛔ Нет доступа", show_alert=True); return
    await cb.answer()
    await state.set_state(AdminFlow.add_id)
    await cb.message.edit_text("Пришлите Telegram ID нового администратора:", reply_markup=kb_cancel())

# No MENU_BUTTON_TEXTS guard here either — the isdigit() check below rejects
# any menu-button label the same way _parse_date does for the two handlers
# above.
@router.message(AdminFlow.add_id, F.text, ~F.text.startswith("/"))
async def admin_add_apply(msg: Message, state: FSMContext, config: TouristDocumentsConfig):
    if not _is_admin(msg.from_user.id, config.admins_file):
        await state.clear(); await msg.answer(NOT_ADMIN_TEXT); return
    value = msg.text.strip()
    # Real Telegram user IDs are positive and, as of now, well under 15
    # digits — the upper bound is the actual fix here (review-found: an
    # unbounded digit string saved verbatim would blow past Telegram's
    # ~4096-char message limit the next time ANY admin opens "👥 Админы",
    # permanently breaking the one screen that could remove it again).
    # Dropped the old lstrip("-") allowance too: unlike group-scoped
    # templates, this bot never runs in a group chat, so there's no
    # legitimate negative (chat/group) id to admit here.
    if not (value.isdigit() and 1 <= len(value) <= 15):
        await msg.answer("⚠️ Пришлите числовой Telegram ID (до 15 цифр):"); return
    ids = _load_admins(config.admins_file)
    ids.add(value)
    _save_admins(config.admins_file, ids)
    await state.clear()
    await msg.answer(f"✅ <code>{_esc(value)}</code> добавлен.", parse_mode="HTML", reply_markup=kb_admin())

@router.callback_query(F.data == "td_admin_remove")
async def admin_remove_start(cb: CallbackQuery, config: TouristDocumentsConfig):
    if not _is_admin(cb.from_user.id, config.admins_file):
        await cb.answer("⛔ Нет доступа", show_alert=True); return
    await cb.answer()
    ids = sorted(_load_admins(config.admins_file))
    if not ids:
        await cb.message.edit_text("Список администраторов пуст.")
        return
    await cb.message.edit_text("Выберите администратора для удаления:", reply_markup=kb_admin_remove_list(ids))

@router.callback_query(F.data.startswith("td_admin_rm:"))
async def admin_remove_apply(cb: CallbackQuery, config: TouristDocumentsConfig):
    if not _is_admin(cb.from_user.id, config.admins_file):
        await cb.answer("⛔ Нет доступа", show_alert=True); return
    await cb.answer()
    target = cb.data.split(":", 1)[1]
    ids = _load_admins(config.admins_file)
    ids.discard(target)
    _save_admins(config.admins_file, ids)
    await cb.message.edit_text(f"✅ <code>{_esc(target)}</code> удалён.", parse_mode="HTML")


# ── EXPIRY REMINDER LOOP ──────────────────────────────────────────────────────
# Same shape/limitation as trip_manager.py's _digest_loop: only started from
# main() (standalone/subprocess mode). The webhook runtime (runtime/registry.py)
# deliberately does NOT start background loops for any template — see
# docs/STAGE2_DESIGN.md "Стоп: _digest_loop() не ложится на паттерн". This is
# an accepted, documented gap shared by every template with a periodic scan,
# not something new introduced here.

async def _run_expiry_check(bot: Bot, config: TouristDocumentsConfig) -> None:
    admins = _load_admins(config.admins_file)
    if not admins:
        return
    threshold = (date.today() + timedelta(days=EXPIRY_REMINDER_DAYS_BEFORE)).isoformat()
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT t.id, t.full_name, g.name AS group_name, "
            "t.passport_expiry, t.passport_notified_at, "
            "t.visa_status, t.visa_expiry, t.visa_notified_at "
            "FROM tourists t LEFT JOIN tour_groups g ON t.tour_group_id = g.id"
        )).fetchall()
        for r in rows:
            passport_due = r["passport_expiry"] <= threshold and not r["passport_notified_at"]
            visa_due = (
                r["visa_status"] == "ready" and r["visa_expiry"]
                and r["visa_expiry"] <= threshold and not r["visa_notified_at"]
            )
            if not passport_due and not visa_due:
                continue
            group_label = r["group_name"] or "без группы"
            # Privacy: same generic wording as the "needs attention" list flag
            # — never says which document or the actual date, only that the
            # card needs opening (logged) to find out.
            text = (
                f"⚠️ <b>Требуется внимание</b>\n\n"
                f"У туриста {_esc(r['full_name'])} ({_esc(group_label)}) приближается "
                f"срок действия документа.\n\nОткройте карточку туриста для деталей."
            )
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🗂 Открыть карточку", callback_data=f"td_tourist:{r['id']}")
            ]])
            # Review-found: only mark notified if at least one admin actually
            # received it — if every send fails (all admins blocked the bot,
            # stale IDs, a Telegram outage), marking notified anyway would
            # silently and permanently suppress the reminder for this tourist
            # (nothing but a manual date edit ever clears the flag).
            sent_ok = False
            for admin_id in admins:
                try:
                    await bot.send_message(int(admin_id), text, parse_mode="HTML", reply_markup=kb)
                    sent_ok = True
                except Exception as e:
                    logger.warning(f"[{config.bot_name}] _run_expiry_check: failed to notify admin {admin_id} for tourist {r['id']}: {e}")
            if not sent_ok:
                continue
            now = datetime.now().isoformat()
            if passport_due:
                await db.execute("UPDATE tourists SET passport_notified_at=? WHERE id=?", (now, r["id"]))
            if visa_due:
                await db.execute("UPDATE tourists SET visa_notified_at=? WHERE id=?", (now, r["id"]))
            # Committed per-row (not once after the whole loop) — review-found:
            # a mid-loop exception would otherwise roll back every notified
            # flag set so far in this pass even though those admins were
            # already messaged, causing duplicate reminders on the next cycle.
            await db.commit()


async def _expiry_check_loop(bot: Bot, config: TouristDocumentsConfig) -> None:
    while True:
        try:
            await _run_expiry_check(bot, config)
        except Exception as e:
            logger.error(f"[{config.bot_name}] _expiry_check_loop error: {e}")
        await asyncio.sleep(EXPIRY_CHECK_INTERVAL_SECONDS)


# ── MAIN ──────────────────────────────────────────────────────────────────────

async def main():
    config = config_from_env()
    bot = Bot(token=os.getenv("BOT_TOKEN"))
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(ConfigMiddleware(config))
    dp.include_router(router)
    await bot.set_my_description(BOT_DESCRIPTION)
    await init_db(config.db_path)
    asyncio.create_task(_expiry_check_loop(bot, config))
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
