# TEMPLATE: booking_restaurant
# USE FOR: бронирование столиков в ресторане/кафе — бронь на компанию (не слот на человека), временное окно вместо точной минуты, повод как отдельное поле, депозит для банкетов от N человек, дедлайн бесплатной отмены за сутки
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
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
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
# runtime state (that's config.db_path/admins_file below). TIME_WINDOWS/
# DAYS_AHEAD/BANQUET_THRESHOLD/CANCEL_FREE_HOURS are shared read-only
# generation parameters — the DATA they gate (reservations) is per-bot via
# config.db_path, only this list's CONTENT is shared.
BOT_DESCRIPTION = "Бронирование столиков: гости, дата, временное окно, повод. Банкеты от порога гостей требуют депозит."
WELCOME_TEXT = (
    "🍽 <b>Бронирование столиков</b>\n\n"
    "Гости, дата, временное окно и (по желанию) повод — бронь создаётся и "
    "ждёт подтверждения администратора.\n\nВыберите действие:"
)
CUSTOMER_WELCOME_TEXT = "👋 Здравствуйте! Забронируйте столик или посмотрите свои брони:"
# Fixed set of time windows the restaurant seats guests in — a WINDOW
# ("18:00–20:00"), not an exact-minute slot, per the design brief: a table
# reservation is booked against a coarse service window, not a point in time.
TIME_WINDOWS = [
    ("12:00", "14:00"), ("14:00", "16:00"), ("16:00", "18:00"),
    ("18:00", "20:00"), ("20:00", "22:00"), ("22:00", "23:30"),
]
DAYS_AHEAD = 14
# From this many guests (inclusive) a reservation is treated as a banquet —
# deposit_required=1 is set automatically at creation, no admin step needed.
BANQUET_THRESHOLD = 8
# Deadline for penalty-free cancellation. Crossing it does NOT block
# cancellation (no in-bot penalty mechanism) — it only changes the wording
# shown to the client, per the design brief ("просто честная информация").
CANCEL_FREE_HOURS = 24
MAX_GUESTS = 500
MAX_TABLE_CAPACITY = 1000
# ── END CUSTOMIZE ─────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
router = Router()

DAYS_RU = {0: "Пн", 1: "Вт", 2: "Ср", 3: "Чт", 4: "Пт", 5: "Сб", 6: "Вс"}
MONTHS_RU = {1: "янв", 2: "фев", 3: "мар", 4: "апр", 5: "май", 6: "июн",
             7: "июл", 8: "авг", 9: "сен", 10: "окт", 11: "ноя", 12: "дек"}

STATUS_LABELS = {
    "pending": "⏳ Ожидает подтверждения",
    "confirmed": "✅ Подтверждена",
    "cancelled": "❌ Отменена",
}


# ── config ───────────────────────────────────────────────────────────────────
# Same pattern as every other template — see docs/STAGE2_DESIGN.md.

@dataclass
class BookingRestaurantConfig:
    bot_name: str
    db_path: str
    admins_file: Path
    welcome_image: Path
    display_name: str | None = None
    group_chat_id: str | None = None


def _paths_for(name: str, data_dir: Path) -> BookingRestaurantConfig:
    return BookingRestaurantConfig(
        bot_name=name,
        db_path=str(data_dir / f"{name}_data.db"),
        admins_file=data_dir / f"admins_{name}.json",
        welcome_image=data_dir / "bot_images" / f"{name}.jpg",
    )


def config_from_env() -> BookingRestaurantConfig:
    """Standalone/subprocess mode."""
    name = Path(__file__).stem
    data_dir = Path(os.getenv("DATA_DIR", "./data"))
    data_dir.mkdir(exist_ok=True)
    return _paths_for(name, data_dir)


def config_from_bot_row(bot_row: dict, data_dir: Path) -> BookingRestaurantConfig:
    """Webhook runtime mode. Paths built from bot_row["bot_id"] (bots.id, the
    physically unique AUTOINCREMENT PK) — NOT bot_row["name"] — same reasoning
    as every other template's config_from_bot_row (see docs/STAGE2_DESIGN.md
    "Изоляция по bots.id")."""
    bot_id = bot_row["bot_id"]
    config = BookingRestaurantConfig(
        bot_name=bot_row["name"],
        db_path=str(data_dir / f"bot_{bot_id}_data.db"),
        admins_file=data_dir / f"admins_{bot_id}.json",
        welcome_image=data_dir / "bot_images" / f"bot_{bot_id}.jpg",
    )
    config.display_name = bot_row.get("display_name")
    config.group_chat_id = bot_row.get("group_chat_id")
    return config


class ConfigMiddleware(BaseMiddleware):
    """Injects this bot's BookingRestaurantConfig into data["config"]."""

    def __init__(self, config: BookingRestaurantConfig) -> None:
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

def _is_admin(user_id: int, config: BookingRestaurantConfig) -> bool:
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


# ── phone normalization ────────────────────────────────────────────────────────
# Same RU-phone formula as templates/vehicle_service.py's _normalize_phone() —
# reused verbatim per the design brief (don't reinvent it). Used here purely
# as a contact field on the reservation (this template has no phone-based
# identity linking: client_user_id is already known from the Telegram update
# that started the booking flow).

def _normalize_phone(raw: str) -> str | None:
    digits = re.sub(r"[^\d]", "", raw)
    if len(digits) == 11 and digits[0] in ("7", "8"):
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    else:
        return None
    return f"+{digits[0]} ({digits[1:4]}) {digits[4:7]}-{digits[7:9]}-{digits[9:11]}"


def _valid_admin_id(text: str) -> bool:
    """Same guard as templates/orders_tracker.py's _valid_admin_id()."""
    if not (bool(text) and text.isascii() and text.isdigit() and len(text) <= 15):
        return False
    return int(text) > 0 and str(int(text)) == text


def _valid_int_in_range(text: str, max_value: int) -> int | None:
    """Shared bound-checked integer parser for guests_count/capacity input.
    Review-found crash (monkey-tester): str.isdigit() returns True for
    Unicode "digit" characters that aren't decimal digits (e.g. superscript
    "²", circled "①") — int() then raises ValueError on them uncaught,
    silently dropping the update with no reply. isascii() (same guard as
    _valid_admin_id above) rules those out before int() ever sees them."""
    text = text.strip()
    if not (text.isascii() and text.isdigit()):
        return None
    n = int(text)
    if n <= 0 or n > max_value:
        return None
    return n


def _valid_guests_count(text: str) -> int | None:
    return _valid_int_in_range(text, MAX_GUESTS)


def _valid_capacity(text: str) -> int | None:
    return _valid_int_in_range(text, MAX_TABLE_CAPACITY)


def _date_label(date_str: str) -> str:
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    return f"{DAYS_RU[d.weekday()]} {d.day} {MONTHS_RU[d.month]}"


def _hours_until(date_str: str, time_str: str) -> float:
    dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    return (dt - datetime.now()).total_seconds() / 3600


# ── db ────────────────────────────────────────────────────────────────────────

async def init_db(db_path: str):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tables (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                name      TEXT NOT NULL,
                capacity  INTEGER NOT NULL,
                active    INTEGER NOT NULL DEFAULT 1
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS reservations (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                client_user_id      INTEGER NOT NULL,
                client_name         TEXT,
                client_phone        TEXT,
                guests_count        INTEGER NOT NULL,
                date                TEXT NOT NULL,
                time_window_start   TEXT NOT NULL,
                time_window_end     TEXT NOT NULL,
                occasion            TEXT,
                deposit_required    INTEGER NOT NULL DEFAULT 0,
                deposit_confirmed   INTEGER NOT NULL DEFAULT 0,
                status              TEXT NOT NULL DEFAULT 'pending'
                                    CHECK(status IN ('pending','confirmed','cancelled')),
                created_at          TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_reservations_date ON reservations(date)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_reservations_client ON reservations(client_user_id)")
        await db.commit()


async def _total_capacity(db_path: str) -> int:
    async with aiosqlite.connect(db_path) as db:
        row = await (await db.execute(
            "SELECT COALESCE(SUM(capacity), 0) FROM tables WHERE active=1"
        )).fetchone()
    return row[0]


async def _window_headroom_text(db_path: str, date: str, window_start: str, window_end: str) -> str:
    """Review-found (state-db/async-pooling/monkey-tester, independently):
    the comment on _available_windows below used to claim "the admin sees
    remaining headroom on the reservation card itself when deciding" — that
    was aspirational, not implemented: _reservation_detail_text never
    rendered it, and cb_admres_confirm never checked it, so an admin could
    confirm several pending reservations for the same window past total
    capacity with zero warning. This is the actual headroom line, wired into
    _reservation_detail_text for pending reservations (see show_headroom
    below) so the claim is now true. Deliberately informational, not a hard
    block — confirming past capacity remains an admin decision, per the
    design brief (pending reservations don't reserve capacity by themselves)."""
    total = await _total_capacity(db_path)
    async with aiosqlite.connect(db_path) as db:
        row = await (await db.execute(
            "SELECT COALESCE(SUM(guests_count), 0) FROM reservations "
            "WHERE date=? AND time_window_start=? AND time_window_end=? AND status='confirmed'",
            (date, window_start, window_end),
        )).fetchone()
    confirmed = row[0]
    remaining = total - confirmed
    warn = " ⚠️ уже переполнено" if remaining < 0 else ""
    return f"📊 Занято в этом окне: {confirmed}/{total} мест{warn}"


async def _available_windows(db_path: str, date: str, guests_count: int) -> list[tuple[str, str]]:
    """Windows whose remaining capacity (total active-table capacity minus
    guests already CONFIRMED for that exact date+window) can still fit this
    party. Deliberately checked against status='confirmed' only, per the
    design brief's literal wording ("вместимость... против уже подтверждённых
    броней") — pending reservations don't reserve capacity yet, they're an
    admin decision still in flight; see _window_headroom_text for what the
    admin actually sees on the reservation card when deciding."""
    total = await _total_capacity(db_path)
    if total <= 0:
        return []
    async with aiosqlite.connect(db_path) as db:
        booked = {}
        rows = await (await db.execute(
            "SELECT time_window_start, time_window_end, SUM(guests_count) "
            "FROM reservations WHERE date=? AND status='confirmed' "
            "GROUP BY time_window_start, time_window_end",
            (date,),
        )).fetchall()
        for start, end, taken in rows:
            booked[(start, end)] = taken
    return [
        (start, end) for start, end in TIME_WINDOWS
        if total - booked.get((start, end), 0) >= guests_count
    ]


# ── FSM staleness guard ─────────────────────────────────────────────────────────
# Same mechanic as templates/orders_tracker.py's FLOW_TIMEOUT_SECONDS/_flow_expired.
FLOW_TIMEOUT_SECONDS = 300


def _flow_expired(data: dict) -> bool:
    started_at = data.get("started_at")
    return started_at is None or (time.time() - started_at) > FLOW_TIMEOUT_SECONDS


# ── FSM states ───────────────────────────────────────────────────────────────

class ReservationFlow(StatesGroup):
    guests_count = State()
    occasion_text = State()
    phone = State()

class TableFlow(StatesGroup):
    add_name = State()
    add_capacity = State()
    edit_name = State()
    edit_capacity = State()

class AdminMgmtFlow(StatesGroup):
    add_admin = State()
    remove_admin_pick = State()


# ── keyboards ─────────────────────────────────────────────────────────────────

def kb_client_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🍽 Забронировать столик", callback_data="book_new")],
        [InlineKeyboardButton(text="📋 Мои брони", callback_data="my_res")],
    ])

def kb_admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Брони на дату", callback_data="admres_menu")],
        [InlineKeyboardButton(text="🍽 Столы", callback_data="tables_menu")],
        [InlineKeyboardButton(text="👥 Админы", callback_data="adm_menu")],
    ])

def kb_back(callback_data: str = "main_menu") -> InlineKeyboardButton:
    return InlineKeyboardButton(text="◀️ Назад", callback_data=callback_data)

def kb_flow_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="flow_cancel")],
    ])

MAX_LIST_BUTTONS = 25

# ── date/window pickers (shared by client booking flow AND admin "Брони на дату") ──

def kb_days(callback_prefix: str, back_callback: str) -> InlineKeyboardMarkup:
    today = datetime.now().date()
    rows = []
    for i in range(DAYS_AHEAD):
        d = today + timedelta(days=i)
        label = f"{DAYS_RU[d.weekday()]} {d.day} {MONTHS_RU[d.month]}"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"{callback_prefix}:{d.isoformat()}")])
    rows.append([kb_back(back_callback)])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_windows(windows: list[tuple[str, str]], date: str) -> InlineKeyboardMarkup:
    # Review-found blocker: callback_data used to embed "{start}:{end}" raw
    # ("res_window:18:00:20:00") — since each half already contains a colon,
    # cb_res_window's split(":", 2) sliced it wrong on EVERY window (parsed
    # window_start="18", window_end="00:20:00"), corrupting every reservation's
    # stored window and silently breaking the whole capacity check. Encoding
    # the TIME_WINDOWS index instead is unambiguous AND doubles as revalidation
    # against the trusted whitelist — a forged callback_data can only fail to
    # parse or reference a real window, never inject an arbitrary time range.
    rows = [
        [InlineKeyboardButton(text=f"⏰ {start}–{end}", callback_data=f"res_window:{TIME_WINDOWS.index((start, end))}")]
        for start, end in windows
    ]
    rows.append([InlineKeyboardButton(text="◀️ Другая дата", callback_data="res_back_days")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_occasion() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎂 Указать повод", callback_data="res_occ_add")],
        [InlineKeyboardButton(text="➡️ Пропустить", callback_data="res_occ_skip")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="flow_cancel")],
    ])

# ── client "Мои брони" ──
def kb_my_reservations(rows: list[tuple]) -> InlineKeyboardMarkup:
    btns = [
        [InlineKeyboardButton(
            text=f"№{rid} · {STATUS_LABELS.get(status, status)} · {date} {start}–{end}",
            callback_data=f"res_view:{rid}",
        )]
        for rid, status, date, start, end in rows[:MAX_LIST_BUTTONS]
    ]
    btns.append([kb_back("main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=btns)

def kb_reservation_detail_client(reservation_id: int, status: str) -> InlineKeyboardMarkup:
    rows = []
    if status in ("pending", "confirmed"):
        rows.append([InlineKeyboardButton(text="❌ Отменить", callback_data=f"res_do_cancel:{reservation_id}")])
    rows.append([kb_back("my_res")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

# ── admin "Брони на дату" ──
def kb_admin_reservation_list(rows: list[tuple], date: str) -> InlineKeyboardMarkup:
    btns = [
        [InlineKeyboardButton(
            text=f"№{rid} · {STATUS_LABELS.get(status, status)} · {guests}чел · {start}–{end}",
            callback_data=f"admres_view:{rid}",
        )]
        for rid, status, guests, start, end in rows[:MAX_LIST_BUTTONS]
    ]
    btns.append([kb_back("admres_menu")])
    return InlineKeyboardMarkup(inline_keyboard=btns)

def kb_reservation_detail_admin(reservation_id: int, status: str, deposit_required: int, deposit_confirmed: int) -> InlineKeyboardMarkup:
    rows = []
    if status == "pending":
        rows.append([InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"admres_confirm:{reservation_id}")])
        rows.append([InlineKeyboardButton(text="❌ Отклонить", callback_data=f"admres_reject:{reservation_id}")])
    if deposit_required and not deposit_confirmed:
        rows.append([InlineKeyboardButton(text="💰 Отметить депозит оплачен", callback_data=f"admres_deposit:{reservation_id}")])
    rows.append([kb_back("admres_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

# ── admin "Столы" ──
def kb_tables_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить стол", callback_data="tbl_new")],
        [kb_back()],
    ])

def kb_table_list(rows: list[tuple]) -> InlineKeyboardMarkup:
    btns = [
        [InlineKeyboardButton(
            text=f"{'🟢' if active else '⚪️'} {_esc(name, 30)} · {capacity} мест",
            callback_data=f"tbl_view:{tid}",
        )]
        for tid, name, capacity, active in rows[:MAX_LIST_BUTTONS]
    ]
    btns.append([kb_back("tables_menu")])
    return InlineKeyboardMarkup(inline_keyboard=btns)

def kb_table_detail(table_id: int, active: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Название", callback_data=f"tbl_edit_name:{table_id}")],
        [InlineKeyboardButton(text="✏️ Вместимость", callback_data=f"tbl_edit_cap:{table_id}")],
        [InlineKeyboardButton(
            text="🗑 Скрыть" if active else "♻️ Показать",
            callback_data=f"tbl_toggle:{table_id}",
        )],
        [kb_back("tables_menu")],
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

async def _reservation_detail_text(
    db_path: str, reservation_id: int, extra_note: str | None = None, show_headroom: bool = False,
) -> str | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        res = await (await db.execute(
            "SELECT * FROM reservations WHERE id=?", (reservation_id,)
        )).fetchone()
    if not res:
        return None
    lines = [
        f"🍽 <b>Бронь №{res['id']}</b> · {STATUS_LABELS.get(res['status'], res['status'])}\n",
        f"👥 Гостей: {res['guests_count']}",
        f"📅 {_date_label(res['date'])} ({res['date']})",
        f"⏰ {res['time_window_start']}–{res['time_window_end']}",
    ]
    if res["occasion"]:
        lines.append(f"🎂 Повод: {_esc(res['occasion'])}")
    if res["client_name"]:
        lines.append(f"👤 {_esc(res['client_name'])}")
    if res["client_phone"]:
        lines.append(f"📞 {_esc(res['client_phone'])}")
    if res["deposit_required"]:
        deposit_state = "✅ подтверждён" if res["deposit_confirmed"] else "⏳ ожидает подтверждения"
        lines.append(f"💰 Депозит для банкета: {deposit_state}")
    # Admin-only, and only while the decision is still open — a confirmed/
    # cancelled reservation's window headroom isn't actionable information
    # anymore, and showing restaurant-wide occupancy to the CLIENT's own
    # "Мои брони" view would be an unrelated internal-data disclosure.
    if show_headroom and res["status"] == "pending":
        lines.append(await _window_headroom_text(
            db_path, res["date"], res["time_window_start"], res["time_window_end"]
        ))
    lines.append(f"🕐 Создана: {res['created_at']}")
    if extra_note:
        lines.append(f"\n{extra_note}")
    return _join_bounded(lines)


async def _edit_text_safe(message: Message, text: str, **kwargs) -> None:
    """Review-found (monkey-tester): a 3rd+ tap on admres_confirm/reject/
    deposit (after the first two taps already made the CAS-guard a no-op and
    left the card unchanged) calls edit_text with the exact same text/markup
    as what's already on screen — Telegram rejects that with "message is not
    modified", uncaught, and the update is silently dropped. Same helper/
    rationale as templates/feedback_survey.py's _edit_text_safe: swallow only
    that specific error, anything else re-raises."""
    try:
        await message.edit_text(text, **kwargs)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


# ── /start ────────────────────────────────────────────────────────────────────

@router.message(Command("start"), F.chat.type == "private")
async def cmd_start(message: Message, state: FSMContext, config: BookingRestaurantConfig):
    # Same reasoning as every other template's cmd_start: /start must reset
    # any dangling mid-flow FSM state before showing a menu.
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
                parse_mode="HTML", reply_markup=kb_admin_menu(),
            )
        else:
            await message.answer(WELCOME_TEXT, parse_mode="HTML", reply_markup=kb_admin_menu())
        if first_time_admin:
            await message.answer(
                "👑 <b>Вы — администратор этого бота.</b>\n\n"
                "Управление другими администраторами — кнопка «👥 Админы» выше.",
                parse_mode="HTML",
            )
    else:
        await message.answer(CUSTOMER_WELCOME_TEXT, reply_markup=kb_client_menu())


@router.callback_query(F.data == "main_menu")
async def cb_main_menu(cb: CallbackQuery, state: FSMContext, config: BookingRestaurantConfig):
    await cb.answer()
    await state.clear()
    if _is_admin(cb.from_user.id, config):
        await cb.message.edit_text(WELCOME_TEXT, parse_mode="HTML", reply_markup=kb_admin_menu())
    else:
        await cb.message.edit_text(CUSTOMER_WELCOME_TEXT, reply_markup=kb_client_menu())


@router.callback_query(F.data == "flow_cancel")
async def cb_flow_cancel(cb: CallbackQuery, state: FSMContext, config: BookingRestaurantConfig):
    await cb.answer()
    await state.clear()
    if _is_admin(cb.from_user.id, config):
        await cb.message.edit_text("Отменено.", reply_markup=kb_admin_menu())
    else:
        await cb.message.edit_text("Отменено.", reply_markup=kb_client_menu())


# ── CLIENT: new reservation flow ────────────────────────────────────────────
# guests_count (text) -> date (callback) -> window (callback, capacity-checked)
# -> occasion (callback: add/skip, add branches into free text) -> phone
# (text) -> finalize (insert pending + banquet-deposit flag + notify admins).

@router.callback_query(F.data == "book_new")
async def cb_book_new(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await state.set_state(ReservationFlow.guests_count)
    await state.update_data(started_at=time.time())
    await cb.message.edit_text(
        f"👥 На сколько гостей забронировать столик? (до {MAX_GUESTS})",
        reply_markup=kb_flow_cancel(),
    )


@router.message(ReservationFlow.guests_count, F.text, ~F.text.startswith("/"))
async def res_guests_count(msg: Message, state: FSMContext):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_client_menu())
        return
    guests = _valid_guests_count(msg.text)
    if guests is None:
        await msg.answer(f"Введите целое число гостей от 1 до {MAX_GUESTS}:", reply_markup=kb_flow_cancel())
        return
    await state.update_data(guests_count=guests)
    await state.set_state(None)
    await msg.answer("📅 Выберите дату:", reply_markup=kb_days("res_day", "book_new"))


@router.callback_query(F.data.startswith("res_day:"))
async def cb_res_day(cb: CallbackQuery, state: FSMContext, config: BookingRestaurantConfig):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data) or "guests_count" not in data:
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_client_menu())
        return
    date = cb.data.split(":", 1)[1]
    windows = await _available_windows(config.db_path, date, data["guests_count"])
    await state.update_data(date=date)
    if not windows:
        await cb.message.edit_text(
            f"😔 На {_date_label(date)} нет окон, вмещающих {data['guests_count']} гостей.\n"
            "Выберите другую дату:",
            reply_markup=kb_days("res_day", "book_new"),
        )
        return
    await cb.message.edit_text(
        f"⏰ {_date_label(date)} · выберите временное окно:",
        reply_markup=kb_windows(windows, date),
    )


@router.callback_query(F.data == "res_back_days")
async def cb_res_back_days(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data) or "guests_count" not in data:
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_client_menu())
        return
    await cb.message.edit_text("📅 Выберите дату:", reply_markup=kb_days("res_day", "book_new"))


@router.callback_query(F.data.startswith("res_window:"))
async def cb_res_window(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data) or "guests_count" not in data or "date" not in data:
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_client_menu())
        return
    try:
        idx = int(cb.data.split(":", 1)[1])
        start, end = TIME_WINDOWS[idx]
    except (ValueError, IndexError):
        # Malformed/forged callback_data — refuse rather than store garbage.
        return
    await state.update_data(window_start=start, window_end=end)
    await cb.message.edit_text("🎂 Указать повод визита?", reply_markup=kb_occasion())


@router.callback_query(F.data == "res_occ_skip")
async def cb_res_occ_skip(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data) or "window_start" not in data:
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_client_menu())
        return
    await state.update_data(occasion=None)
    await state.set_state(ReservationFlow.phone)
    await cb.message.edit_text("📞 Введите номер телефона для связи:", reply_markup=kb_flow_cancel())


@router.callback_query(F.data == "res_occ_add")
async def cb_res_occ_add(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data) or "window_start" not in data:
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_client_menu())
        return
    await state.set_state(ReservationFlow.occasion_text)
    await cb.message.edit_text("🎂 Введите повод (например: день рождения):", reply_markup=kb_flow_cancel())


@router.message(ReservationFlow.occasion_text, F.text, ~F.text.startswith("/"))
async def res_occasion_text(msg: Message, state: FSMContext):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_client_menu())
        return
    occasion = msg.text.strip()
    if not occasion:
        await msg.answer("Повод не может быть пустым. Введите повод:", reply_markup=kb_flow_cancel())
        return
    if len(occasion) > 200:
        await msg.answer("⚠️ Слишком длинно. Введите повод короче 200 символов:", reply_markup=kb_flow_cancel())
        return
    await state.update_data(occasion=occasion)
    await state.set_state(ReservationFlow.phone)
    await msg.answer("📞 Введите номер телефона для связи:", reply_markup=kb_flow_cancel())


@router.message(ReservationFlow.phone, F.text, ~F.text.startswith("/"))
async def res_phone(msg: Message, state: FSMContext, config: BookingRestaurantConfig, bot: Bot):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_client_menu())
        return
    phone = _normalize_phone(msg.text)
    if phone is None:
        await msg.answer("Не удалось распознать номер. Введите номер телефона, например: +7 999 123-45-67",
                          reply_markup=kb_flow_cancel())
        return

    guests_count = data.get("guests_count")
    date = data.get("date")
    window_start = data.get("window_start")
    window_end = data.get("window_end")
    if not (guests_count and date and window_start and window_end):
        await state.clear()
        await msg.answer("Сессия устарела, начните заново.", reply_markup=kb_client_menu())
        return

    deposit_required = 1 if guests_count >= BANQUET_THRESHOLD else 0
    occasion = data.get("occasion")
    client_name = msg.from_user.full_name
    await state.clear()

    async with aiosqlite.connect(config.db_path) as db:
        cur = await db.execute(
            "INSERT INTO reservations "
            "(client_user_id, client_name, client_phone, guests_count, date, "
            "time_window_start, time_window_end, occasion, deposit_required) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (msg.from_user.id, client_name, phone, guests_count, date,
             window_start, window_end, occasion, deposit_required),
        )
        reservation_id = cur.lastrowid
        await db.commit()

    note = None
    if deposit_required:
        note = (f"🎉 Для банкета от {BANQUET_THRESHOLD} человек требуется депозит. "
                "Администратор свяжется с вами по депозиту.")
    text = await _reservation_detail_text(config.db_path, reservation_id, extra_note=note)
    await msg.answer(f"✅ Бронь создана и ждёт подтверждения!\n\n{text}", parse_mode="HTML",
                      reply_markup=kb_client_menu())

    for admin_id in _load_admins(config.admins_file):
        try:
            await bot.send_message(
                int(admin_id),
                f"🔔 <b>Новая бронь!</b>\n\n{text}",
                parse_mode="HTML",
                reply_markup=kb_reservation_detail_admin(reservation_id, "pending", deposit_required, 0),
            )
        except TelegramAPIError as e:
            logger.warning(f"booking_restaurant: failed to notify admin {admin_id} of new reservation: {e}")


# ── CLIENT: "Мои брони" ─────────────────────────────────────────────────────

@router.callback_query(F.data == "my_res")
async def cb_my_res(cb: CallbackQuery, state: FSMContext, config: BookingRestaurantConfig):
    await cb.answer()
    await state.clear()
    async with aiosqlite.connect(config.db_path) as db:
        rows = await (await db.execute(
            "SELECT id, status, date, time_window_start, time_window_end FROM reservations "
            "WHERE client_user_id=? ORDER BY id DESC LIMIT ?",
            (cb.from_user.id, MAX_LIST_BUTTONS),
        )).fetchall()
    if not rows:
        await cb.message.edit_text("📭 У вас пока нет броней.", reply_markup=kb_client_menu())
        return
    await cb.message.edit_text("📋 Ваши брони:", reply_markup=kb_my_reservations(rows))


@router.callback_query(F.data.startswith("res_view:"))
async def cb_res_view(cb: CallbackQuery, config: BookingRestaurantConfig):
    await cb.answer()
    try:
        reservation_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute(
            "SELECT client_user_id, status FROM reservations WHERE id=?", (reservation_id,)
        )).fetchone()
    if not row or row[0] != cb.from_user.id:
        await cb.message.edit_text("Бронь не найдена.", reply_markup=kb_client_menu())
        return
    text = await _reservation_detail_text(config.db_path, reservation_id)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_reservation_detail_client(reservation_id, row[1]))


@router.callback_query(F.data.startswith("res_do_cancel:"))
async def cb_res_do_cancel(cb: CallbackQuery, config: BookingRestaurantConfig):
    await cb.answer()
    try:
        reservation_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute(
            "SELECT client_user_id, status, date, time_window_start FROM reservations WHERE id=?",
            (reservation_id,),
        )).fetchone()
        if not row or row[0] != cb.from_user.id:
            await cb.message.edit_text("Бронь не найдена.", reply_markup=kb_client_menu())
            return
        client_user_id, status, date, window_start = row
        if status == "cancelled":
            text = await _reservation_detail_text(config.db_path, reservation_id)
            await cb.message.edit_text(text, parse_mode="HTML",
                                        reply_markup=kb_reservation_detail_client(reservation_id, status))
            return
        # Compare-and-swap: WHERE status=status makes a double-tap a no-op on
        # the second write — same principle as templates/vehicle_service.py's
        # cb_req_status.
        cur = await db.execute(
            "UPDATE reservations SET status='cancelled' WHERE id=? AND status=?",
            (reservation_id, status),
        )
        await db.commit()
    late_note = None
    if cur.rowcount and _hours_until(date, window_start) < CANCEL_FREE_HOURS:
        late_note = (f"⚠️ Отмена сделана позже чем за {CANCEL_FREE_HOURS} ч. до брони — "
                     "это просто информация, штрафов в боте нет.")
    text = await _reservation_detail_text(config.db_path, reservation_id, extra_note=late_note)
    await cb.message.edit_text(f"❌ Бронь отменена.\n\n{text}", parse_mode="HTML",
                                reply_markup=kb_reservation_detail_client(reservation_id, "cancelled"))


# ── ADMIN: "Брони на дату" ──────────────────────────────────────────────────

@router.callback_query(F.data == "admres_menu")
async def cb_admres_menu(cb: CallbackQuery, state: FSMContext, config: BookingRestaurantConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    await cb.message.edit_text("📅 Выберите дату:", reply_markup=kb_days("admres_day", "main_menu"))


@router.callback_query(F.data.startswith("admres_day:"))
async def cb_admres_day(cb: CallbackQuery, config: BookingRestaurantConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    date = cb.data.split(":", 1)[1]
    async with aiosqlite.connect(config.db_path) as db:
        rows = await (await db.execute(
            "SELECT id, status, guests_count, time_window_start, time_window_end FROM reservations "
            "WHERE date=? ORDER BY time_window_start, id LIMIT ?",
            (date, MAX_LIST_BUTTONS),
        )).fetchall()
    if not rows:
        await cb.message.edit_text(f"На {_date_label(date)} броней нет.", reply_markup=kb_days("admres_day", "main_menu"))
        return
    await cb.message.edit_text(f"📅 Брони на {_date_label(date)}:", reply_markup=kb_admin_reservation_list(rows, date))


@router.callback_query(F.data.startswith("admres_view:"))
async def cb_admres_view(cb: CallbackQuery, config: BookingRestaurantConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        reservation_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute(
            "SELECT status, deposit_required, deposit_confirmed FROM reservations WHERE id=?", (reservation_id,)
        )).fetchone()
    if not row:
        await cb.message.edit_text("Бронь не найдена.", reply_markup=kb_admin_menu())
        return
    text = await _reservation_detail_text(config.db_path, reservation_id, show_headroom=True)
    await cb.message.edit_text(text, parse_mode="HTML",
                                reply_markup=kb_reservation_detail_admin(reservation_id, row[0], row[1], row[2]))


async def _admres_notify_client(bot: Bot, config: BookingRestaurantConfig, client_user_id: int, text: str) -> str | None:
    try:
        await bot.send_message(client_user_id, text)
        return "🔔 Клиент уведомлён."
    except TelegramAPIError as e:
        logger.warning(f"booking_restaurant: failed to notify client {client_user_id}: {e}")
        return "⚠️ Не удалось уведомить клиента (возможно, заблокировал бота)."


@router.callback_query(F.data.startswith("admres_confirm:"))
async def cb_admres_confirm(cb: CallbackQuery, bot: Bot, config: BookingRestaurantConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        reservation_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute(
            "SELECT status, client_user_id, deposit_required, deposit_confirmed FROM reservations WHERE id=?",
            (reservation_id,),
        )).fetchone()
        if not row:
            await cb.message.edit_text("Бронь не найдена.", reply_markup=kb_admin_menu())
            return
        _, client_user_id, deposit_required, deposit_confirmed = row
        # Fixed-status guard (WHERE status='pending'), NOT "WHERE status=old_status":
        # on a double-tap old_status is already 'confirmed' by the first tap, and
        # 'confirmed'='confirmed' would match and re-notify the client — same
        # class of bug this pattern must avoid as cb_admres_deposit's fixed
        # "WHERE deposit_confirmed=0" guard below.
        cur = await db.execute(
            "UPDATE reservations SET status='confirmed' WHERE id=? AND status='pending'",
            (reservation_id,),
        )
        await db.commit()
    note = None
    if cur.rowcount:
        note = await _admres_notify_client(
            bot, config, client_user_id, f"✅ Ваша бронь №{reservation_id} подтверждена!"
        )
    # Review-found (clean-code): status/deposit_required/deposit_confirmed
    # were re-fetched from the DB here even though the UPDATE above only ever
    # touches `status`, and its outcome is already known from cur.rowcount —
    # no second round-trip needed (matches cb_admres_deposit's own style,
    # which never re-queries either).
    new_status = "confirmed" if cur.rowcount else row[0]
    text = await _reservation_detail_text(config.db_path, reservation_id, extra_note=note)
    await _edit_text_safe(cb.message, text, parse_mode="HTML",
                           reply_markup=kb_reservation_detail_admin(
                               reservation_id, new_status, deposit_required, deposit_confirmed))


@router.callback_query(F.data.startswith("admres_reject:"))
async def cb_admres_reject(cb: CallbackQuery, bot: Bot, config: BookingRestaurantConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        reservation_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute(
            "SELECT status, client_user_id, deposit_required, deposit_confirmed FROM reservations WHERE id=?",
            (reservation_id,),
        )).fetchone()
        if not row:
            await cb.message.edit_text("Бронь не найдена.", reply_markup=kb_admin_menu())
            return
        old_status, client_user_id, deposit_required, deposit_confirmed = row
        # Same fixed-status guard as cb_admres_confirm above — only a
        # 'pending' reservation can be rejected, so a double-tap (where
        # old_status is already 'cancelled' from the first tap) can't
        # self-match and re-notify.
        cur = await db.execute(
            "UPDATE reservations SET status='cancelled' WHERE id=? AND status='pending'",
            (reservation_id,),
        )
        await db.commit()
    note = None
    if cur.rowcount:
        note = await _admres_notify_client(
            bot, config, client_user_id, f"❌ Ваша бронь №{reservation_id} отклонена администратором."
        )
    # Same fix as cb_admres_confirm — no second round-trip needed, the UPDATE
    # only ever touches `status` and its outcome is already known.
    new_status = "cancelled" if cur.rowcount else old_status
    text = await _reservation_detail_text(config.db_path, reservation_id, extra_note=note)
    await _edit_text_safe(cb.message, text, parse_mode="HTML",
                           reply_markup=kb_reservation_detail_admin(
                               reservation_id, new_status, deposit_required, deposit_confirmed))


@router.callback_query(F.data.startswith("admres_deposit:"))
async def cb_admres_deposit(cb: CallbackQuery, bot: Bot, config: BookingRestaurantConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        reservation_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute(
            "SELECT status, client_user_id, deposit_required, deposit_confirmed FROM reservations WHERE id=?",
            (reservation_id,),
        )).fetchone()
        if not row:
            await cb.message.edit_text("Бронь не найдена.", reply_markup=kb_admin_menu())
            return
        status, client_user_id, deposit_required, deposit_confirmed = row
        if not deposit_required or deposit_confirmed:
            text = await _reservation_detail_text(config.db_path, reservation_id)
            await _edit_text_safe(cb.message, text, parse_mode="HTML",
                                   reply_markup=kb_reservation_detail_admin(reservation_id, status, deposit_required, deposit_confirmed))
            return
        cur = await db.execute(
            "UPDATE reservations SET deposit_confirmed=1 WHERE id=? AND deposit_confirmed=0",
            (reservation_id,),
        )
        await db.commit()
    note = None
    if cur.rowcount:
        note = await _admres_notify_client(
            bot, config, client_user_id, f"💰 Депозит для вашей брони №{reservation_id} отмечен как оплаченный."
        )
    text = await _reservation_detail_text(config.db_path, reservation_id, extra_note=note)
    await _edit_text_safe(cb.message, text, parse_mode="HTML",
                           reply_markup=kb_reservation_detail_admin(reservation_id, status, deposit_required, 1))


# ── ADMIN: "Столы" ───────────────────────────────────────────────────────────

@router.callback_query(F.data == "tables_menu")
async def cb_tables_menu(cb: CallbackQuery, state: FSMContext, config: BookingRestaurantConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    async with aiosqlite.connect(config.db_path) as db:
        rows = await (await db.execute(
            "SELECT id, name, capacity, active FROM tables ORDER BY active DESC, id"
        )).fetchall()
    if not rows:
        await cb.message.edit_text("🍽 Столов пока нет.", reply_markup=kb_tables_menu())
        return
    await cb.message.edit_text("🍽 <b>Столы</b>", parse_mode="HTML", reply_markup=kb_table_list(rows))


@router.callback_query(F.data == "tbl_new")
async def cb_tbl_new(cb: CallbackQuery, state: FSMContext, config: BookingRestaurantConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    await state.set_state(TableFlow.add_name)
    await state.update_data(started_at=time.time())
    await cb.message.edit_text("📝 Введите название/номер стола:", reply_markup=kb_flow_cancel())


@router.message(TableFlow.add_name, F.text, ~F.text.startswith("/"))
async def tbl_add_name(msg: Message, state: FSMContext):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_admin_menu())
        return
    name = msg.text.strip()
    if not name:
        await msg.answer("Название не может быть пустым. Введите название стола:", reply_markup=kb_flow_cancel())
        return
    if len(name) > 100:
        await msg.answer("⚠️ Слишком длинное название. Введите короче 100 символов:", reply_markup=kb_flow_cancel())
        return
    await state.update_data(pending_name=name)
    await state.set_state(TableFlow.add_capacity)
    await msg.answer("🔢 Вместимость (число гостей):", reply_markup=kb_flow_cancel())


@router.message(TableFlow.add_capacity, F.text, ~F.text.startswith("/"))
async def tbl_add_capacity(msg: Message, state: FSMContext, config: BookingRestaurantConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_admin_menu())
        return
    capacity = _valid_capacity(msg.text)
    if capacity is None:
        await msg.answer("Введите целое число от 1 до 1000:", reply_markup=kb_flow_cancel())
        return
    name = data.get("pending_name")
    if not name:
        await state.clear()
        await msg.answer("Сессия устарела, начните заново.", reply_markup=kb_admin_menu())
        return
    await state.clear()
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute("INSERT INTO tables (name, capacity) VALUES (?,?)", (name, capacity))
        await db.commit()
    await msg.answer(f"✅ Стол добавлен: {_esc(name)} ({capacity} мест)", parse_mode="HTML",
                      reply_markup=kb_tables_menu())


@router.callback_query(F.data.startswith("tbl_view:"))
async def cb_tbl_view(cb: CallbackQuery, config: BookingRestaurantConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        table_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute(
            "SELECT name, capacity, active FROM tables WHERE id=?", (table_id,)
        )).fetchone()
    if not row:
        await cb.message.edit_text("Стол не найден.", reply_markup=kb_tables_menu())
        return
    name, capacity, active = row
    status = "🟢 активен" if active else "⚪️ скрыт"
    await cb.message.edit_text(
        f"🍽 <b>{_esc(name)}</b>\n👥 Вместимость: {capacity}\n{status}",
        parse_mode="HTML", reply_markup=kb_table_detail(table_id, active),
    )


@router.callback_query(F.data.startswith("tbl_toggle:"))
async def cb_tbl_toggle(cb: CallbackQuery, config: BookingRestaurantConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        table_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute("SELECT name, capacity, active FROM tables WHERE id=?", (table_id,))).fetchone()
        if not row:
            await cb.message.edit_text("Стол не найден.", reply_markup=kb_tables_menu())
            return
        name, capacity, old_active = row
        new_active = 0 if old_active else 1
        # Review-found (monkey-tester): plain read-then-write with no CAS
        # guard — two near-simultaneous taps could double-flip the flag back
        # to its original value with no error, silently no-op'ing from the
        # admin's perspective. WHERE active=old_active makes the loser of a
        # race a safe no-op (still re-renders the current, correct state)
        # instead of both taps applying.
        cur = await db.execute(
            "UPDATE tables SET active=? WHERE id=? AND active=?", (new_active, table_id, old_active)
        )
        await db.commit()
        if cur.rowcount == 0:
            row = await (await db.execute("SELECT name, capacity, active FROM tables WHERE id=?", (table_id,))).fetchone()
            name, capacity, new_active = row
    status = "🟢 активен" if new_active else "⚪️ скрыт"
    await _edit_text_safe(
        cb.message, f"🍽 <b>{_esc(name)}</b>\n👥 Вместимость: {capacity}\n{status}",
        parse_mode="HTML", reply_markup=kb_table_detail(table_id, new_active),
    )


@router.callback_query(F.data.startswith("tbl_edit_name:"))
async def cb_tbl_edit_name(cb: CallbackQuery, state: FSMContext, config: BookingRestaurantConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        table_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    await state.clear()
    await state.set_state(TableFlow.edit_name)
    await state.update_data(started_at=time.time(), table_id=table_id)
    await cb.message.edit_text("📝 Введите новое название стола:", reply_markup=kb_flow_cancel())


@router.message(TableFlow.edit_name, F.text, ~F.text.startswith("/"))
async def tbl_edit_name(msg: Message, state: FSMContext, config: BookingRestaurantConfig):
    data = await state.get_data()
    if _flow_expired(data) or "table_id" not in data:
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_admin_menu())
        return
    name = msg.text.strip()
    if not name or len(name) > 100:
        await msg.answer("Название должно быть непустым и короче 100 символов:", reply_markup=kb_flow_cancel())
        return
    table_id = data["table_id"]
    await state.clear()
    async with aiosqlite.connect(config.db_path) as db:
        cur = await db.execute("UPDATE tables SET name=? WHERE id=?", (name, table_id))
        await db.commit()
    if cur.rowcount == 0:
        await msg.answer("Стол не найден.", reply_markup=kb_tables_menu())
        return
    await msg.answer(f"✅ Название обновлено: {_esc(name)}", parse_mode="HTML", reply_markup=kb_tables_menu())


@router.callback_query(F.data.startswith("tbl_edit_cap:"))
async def cb_tbl_edit_cap(cb: CallbackQuery, state: FSMContext, config: BookingRestaurantConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        table_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    await state.clear()
    await state.set_state(TableFlow.edit_capacity)
    await state.update_data(started_at=time.time(), table_id=table_id)
    await cb.message.edit_text("🔢 Введите новую вместимость:", reply_markup=kb_flow_cancel())


@router.message(TableFlow.edit_capacity, F.text, ~F.text.startswith("/"))
async def tbl_edit_capacity(msg: Message, state: FSMContext, config: BookingRestaurantConfig):
    data = await state.get_data()
    if _flow_expired(data) or "table_id" not in data:
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_admin_menu())
        return
    capacity = _valid_capacity(msg.text)
    if capacity is None:
        await msg.answer("Введите целое число от 1 до 1000:", reply_markup=kb_flow_cancel())
        return
    table_id = data["table_id"]
    await state.clear()
    async with aiosqlite.connect(config.db_path) as db:
        cur = await db.execute("UPDATE tables SET capacity=? WHERE id=?", (capacity, table_id))
        await db.commit()
    if cur.rowcount == 0:
        await msg.answer("Стол не найден.", reply_markup=kb_tables_menu())
        return
    await msg.answer(f"✅ Вместимость обновлена: {capacity}", reply_markup=kb_tables_menu())


# ── ADMIN: admin management ─────────────────────────────────────────────────

@router.callback_query(F.data == "adm_menu")
async def cb_adm_menu(cb: CallbackQuery, state: FSMContext, config: BookingRestaurantConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    await cb.message.edit_text("👥 <b>Администраторы</b>", parse_mode="HTML", reply_markup=kb_admins_menu())


@router.callback_query(F.data == "adm_add")
async def cb_adm_add(cb: CallbackQuery, state: FSMContext, config: BookingRestaurantConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    await state.set_state(AdminMgmtFlow.add_admin)
    await state.update_data(started_at=time.time())
    await cb.message.edit_text("🔢 Введите Telegram user_id нового администратора:", reply_markup=kb_flow_cancel())


@router.message(AdminMgmtFlow.add_admin, F.text, ~F.text.startswith("/"))
async def adm_add_id(msg: Message, state: FSMContext, config: BookingRestaurantConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_admin_menu())
        return
    text = msg.text.strip()
    if not _valid_admin_id(text):
        await msg.answer("Введите корректный числовой user_id:", reply_markup=kb_flow_cancel())
        return
    await state.clear()
    ids = _load_admins(config.admins_file)
    ids.add(text)
    _save_admins(config.admins_file, ids)
    await msg.answer(f"✅ <code>{_esc(text)}</code> добавлен как администратор.", parse_mode="HTML",
                      reply_markup=kb_admins_menu())


@router.callback_query(F.data == "adm_remove")
async def cb_adm_remove(cb: CallbackQuery, state: FSMContext, config: BookingRestaurantConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    ids = sorted(_load_admins(config.admins_file))[:MAX_ADMIN_REMOVE_BUTTONS]
    if len(ids) <= 1:
        await cb.message.edit_text("Нельзя удалить последнего администратора.", reply_markup=kb_admins_menu())
        return
    await state.clear()
    await state.set_state(AdminMgmtFlow.remove_admin_pick)
    await state.update_data(started_at=time.time(), remove_admin_ids=ids)
    await cb.message.edit_text("Выберите администратора для удаления:", reply_markup=kb_remove_admins(ids))


@router.callback_query(AdminMgmtFlow.remove_admin_pick, F.data.startswith("adm_rm:"))
async def cb_adm_remove_pick(cb: CallbackQuery, state: FSMContext, config: BookingRestaurantConfig):
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
