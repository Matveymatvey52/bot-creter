# TEMPLATE: coworking_space
# USE FOR: коворкинг — бронирование конкретного места/ресурса (стол, переговорная, место на день) на слот времени, без подтверждения администратором, услуги ресепшена, регистрация гостей
# CUSTOMIZE: sections marked with # CUSTOMIZE
from __future__ import annotations

import asyncio
import html
import logging
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import aiosqlite
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

# ── DESIGN NOTE ────────────────────────────────────────────────────────────
# What makes this template different from every booking_* template already
# in this repo (booking_beauty/fitness/medical/restaurant): those book a
# SPECIALIST'S TIME (a trainer/doctor/stylist/table) — the person or the
# reservation itself is the scarce thing. Here the scarce thing is a
# physical RESOURCE (a desk, a meeting room, a full-day pass): the same
# resource can never be double-booked for an overlapping time window. That
# is the one piece of real business logic in this module.
#
# Overlap check (the correctness-critical bit): before confirming a booking,
# look for any OTHER status='confirmed' booking on the SAME resource_id AND
# SAME booking_date whose [start, end) interval overlaps the requested one.
# Standard half-open-interval overlap test:
#     NOT (existing_end <= new_start OR existing_start >= new_end)
# Two intervals that only TOUCH (one ends exactly when the other starts) do
# NOT overlap under this test — back-to-back bookings on the same resource
# are allowed, matching how real coworking desks/rooms are scheduled.
#
# Race-safety: two clients (or the same client double-tapping) can trigger
# nearly-simultaneous booking attempts for the same resource+slot. A plain
# "open a connection, SELECT, then INSERT, then commit" is NOT enough on its
# own: aiosqlite's default connection is in autocommit-like mode until a
# write statement actually runs, so a bare SELECT does not take SQLite's
# write lock — two concurrent connections can both run the overlap SELECT,
# both see "no conflict", and both then INSERT, producing two confirmed
# bookings for the same slot (verified empirically while writing this
# template's own test suite — see
# test_near_simultaneous_double_booking_does_not_double_confirm). The fix
# used here is `BEGIN IMMEDIATE` at the start of the guarded block: it
# acquires SQLite's RESERVED write lock immediately, before the SELECT runs,
# so a second connection's own `BEGIN IMMEDIATE` blocks until the first
# transaction commits or rolls back — turning "check-then-insert" into an
# atomic unit across connections, not just within one. This is the
# INSERT-vs-INSERT analogue of the compare-and-swap UPDATE pattern used
# elsewhere in this repo (orders_tracker.py / vehicle_service.py's
# status-transition `UPDATE ... WHERE status=old_status`) — there, the
# uniqueness of the row already existing lets a plain UPDATE's WHERE clause
# do the compare-and-swap; here, the row doesn't exist yet, so the
# equivalent guarantee has to come from an explicit write-lock instead.
#
# Booking is self-service and instant (status='confirmed' immediately, no
# admin-approval step) — this is a deliberate design choice matching how a
# real coworking space works: you check that a desk is free and you're
# booked, the way you'd just sit down at an open desk. This is unlike
# booking_restaurant.py, where a human host confirms a table reservation.
# There's no analogous human-in-the-loop step for a coworking resource.
# ── END DESIGN NOTE ──────────────────────────────────────────────────────────

# ── CUSTOMIZE ────────────────────────────────────────────────────────────────
BOT_DESCRIPTION = (
    "Бронирование мест в коворкинге: столы, переговорные, место на весь день. "
    "Бронь подтверждается мгновенно, без одобрения администратора. "
    "Доп. услуги ресепшена и регистрация гостей."
)
WELCOME_TEXT = (
    "🏢 <b>Коворкинг</b>\n\n"
    "Забронируйте стол, переговорную или место на весь день — подтверждение мгновенное.\n\n"
    "Выберите действие:"
)
CLIENT_WELCOME_TEXT = (
    "👋 Добро пожаловать в коворкинг!\n\nВыберите действие:"
)

# Resource types this coworking space offers. Label shown to users.
RESOURCE_TYPES = {
    "desk": "🪑 Стол",
    "meeting_room": "🗣 Переговорная",
    "full_day": "🏠 Место на весь день",
}

# Service-desk request types offered via "🙋 Доп. услуги".
SERVICE_TYPES = {
    "printer": "🖨 Принтер/печать",
    "coffee": "☕ Кофе/чай",
    "cleaning": "🧹 Уборка",
    "other": "❓ Другое",
}

# Fixed time-slot granularity — a small closed set, same convention as
# booking_restaurant.py's TIME_WINDOWS (never free-text time entry).
TIME_WINDOWS = [
    ("09:00", "11:00"),
    ("11:00", "13:00"),
    ("13:00", "15:00"),
    ("15:00", "17:00"),
    ("17:00", "19:00"),
    ("09:00", "19:00"),  # full working day — natural pairing with the 'full_day' resource type/tariff
]

TARIFFS = {
    "day_pass": "💳 Разовый пропуск",
    "membership": "🎫 Абонемент",
}

# How many days ahead a client may pick a booking date.
DAYS_AHEAD = 14
# ── END CUSTOMIZE ─────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
router = Router()


# ── config ───────────────────────────────────────────────────────────────────

@dataclass
class CoworkingSpaceConfig:
    bot_name: str
    db_path: str
    admins_file: Path
    welcome_image: Path
    display_name: str | None = None
    group_chat_id: str | None = None


def _paths_for(name: str, data_dir: Path) -> CoworkingSpaceConfig:
    return CoworkingSpaceConfig(
        bot_name=name,
        db_path=str(data_dir / f"{name}_data.db"),
        admins_file=data_dir / f"admins_{name}.json",
        welcome_image=data_dir / "bot_images" / f"{name}.jpg",
    )


def config_from_env() -> CoworkingSpaceConfig:
    """Standalone/subprocess mode."""
    name = Path(__file__).stem
    data_dir = Path(os.getenv("DATA_DIR", "./data"))
    data_dir.mkdir(exist_ok=True)
    return _paths_for(name, data_dir)


def config_from_bot_row(bot_row: dict, data_dir: Path) -> CoworkingSpaceConfig:
    """Webhook runtime mode. Paths built from bot_row["bot_id"] (bots.id, the
    physically unique AUTOINCREMENT PK) — NOT bot_row["name"] — same
    isolation reasoning as every other template's config_from_bot_row."""
    bot_id = bot_row["bot_id"]
    config = CoworkingSpaceConfig(
        bot_name=bot_row["name"],
        db_path=str(data_dir / f"bot_{bot_id}_data.db"),
        admins_file=data_dir / f"admins_{bot_id}.json",
        welcome_image=data_dir / "bot_images" / f"bot_{bot_id}.jpg",
    )
    config.display_name = bot_row.get("display_name")
    config.group_chat_id = bot_row.get("group_chat_id")
    return config


class ConfigMiddleware(BaseMiddleware):
    """Injects this bot's CoworkingSpaceConfig into data["config"]."""

    def __init__(self, config: CoworkingSpaceConfig) -> None:
        self.config = config
        super().__init__()

    async def __call__(self, handler, event, data):
        data["config"] = self.config
        return await handler(event, data)


# ── admin helpers ─────────────────────────────────────────────────────────────

def _load_admins(admins_file: Path) -> set:
    import json
    try:
        return set(json.loads(admins_file.read_text()).get("ids", []))
    except Exception:
        return set()


def _save_admins(admins_file: Path, ids: set) -> None:
    import json
    admins_file.write_text(json.dumps({"ids": list(ids)}, ensure_ascii=False))


def _is_admin(user_id: int, config: CoworkingSpaceConfig) -> bool:
    return str(user_id) in _load_admins(config.admins_file)


def _esc(value, max_len: int = 500) -> str:
    text = str(value) if value is not None else ""
    if len(text) > max_len:
        text = text[:max_len] + "…"
    return html.escape(text)


def _join_bounded(lines: list[str], limit: int = 3500) -> str:
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
            CREATE TABLE IF NOT EXISTS resources (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                name          TEXT NOT NULL,
                resource_type TEXT NOT NULL,
                is_active     INTEGER NOT NULL DEFAULT 1,
                created_at    TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bookings (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                client_user_id   INTEGER NOT NULL,
                client_name      TEXT,
                resource_id      INTEGER NOT NULL REFERENCES resources(id),
                booking_date     TEXT NOT NULL,
                time_slot_start  TEXT NOT NULL,
                time_slot_end    TEXT NOT NULL,
                tariff           TEXT NOT NULL,
                status           TEXT NOT NULL DEFAULT 'confirmed'
                                 CHECK(status IN ('confirmed','cancelled','completed')),
                created_at       TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS guest_registrations (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                booking_id  INTEGER NOT NULL REFERENCES bookings(id),
                guest_name  TEXT NOT NULL,
                created_at  TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS service_requests (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                client_user_id   INTEGER NOT NULL,
                booking_id       INTEGER,
                service_type     TEXT NOT NULL,
                status           TEXT NOT NULL DEFAULT 'requested'
                                 CHECK(status IN ('requested','done')),
                created_at       TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_bookings_resource_date ON bookings(resource_id, booking_date)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_bookings_client ON bookings(client_user_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_guest_booking ON guest_registrations(booking_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_service_status ON service_requests(status)")
        await db.commit()


# ── overlap check ────────────────────────────────────────────────────────────
# The one piece of real business logic in this template — see DESIGN NOTE
# above. `db` must be an already-open aiosqlite connection; caller controls
# the transaction boundary (no commit happens in here) so this can be reused
# both for "list only free resources" (read-only) and immediately before an
# INSERT (guarded check) on the SAME connection without releasing the write
# lock in between.

async def _has_overlap(
    db: aiosqlite.Connection, resource_id: int, booking_date: str, start: str, end: str,
    exclude_booking_id: int | None = None,
) -> bool:
    sql = (
        "SELECT 1 FROM bookings "
        "WHERE resource_id=? AND booking_date=? AND status='confirmed' "
        "AND NOT (time_slot_end <= ? OR time_slot_start >= ?)"
    )
    params: list = [resource_id, booking_date, start, end]
    if exclude_booking_id is not None:
        sql += " AND id != ?"
        params.append(exclude_booking_id)
    sql += " LIMIT 1"
    row = await (await db.execute(sql, params)).fetchone()
    return row is not None


async def _available_resources(
    db: aiosqlite.Connection, resource_type: str, booking_date: str, start: str, end: str,
) -> list[tuple]:
    """Resources of resource_type, active, with NO overlapping confirmed
    booking for the given date+slot."""
    rows = await (await db.execute(
        "SELECT id, name FROM resources WHERE resource_type=? AND is_active=1 ORDER BY id", (resource_type,)
    )).fetchall()
    free = []
    for rid, name in rows:
        if not await _has_overlap(db, rid, booking_date, start, end):
            free.append((rid, name))
    return free


# ── FSM staleness guard ─────────────────────────────────────────────────────────
FLOW_TIMEOUT_SECONDS = 300


def _flow_expired(data: dict) -> bool:
    started_at = data.get("started_at")
    return started_at is None or (time.time() - started_at) > FLOW_TIMEOUT_SECONDS


# ── date/time helpers ────────────────────────────────────────────────────────

def _upcoming_dates(days_ahead: int = DAYS_AHEAD) -> list[str]:
    today = date.today()
    return [(today + timedelta(days=i)).isoformat() for i in range(days_ahead)]


def _fmt_date_label(iso_date: str) -> str:
    try:
        d = datetime.strptime(iso_date, "%Y-%m-%d").date()
    except ValueError:
        return iso_date
    weekdays = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    return f"{d.day:02d}.{d.month:02d} ({weekdays[d.weekday()]})"


# ── FSM states ───────────────────────────────────────────────────────────────

class BookFlow(StatesGroup):
    pick_type = State()
    pick_date = State()
    pick_slot = State()
    pick_resource = State()
    pick_tariff = State()


class ServiceFlow(StatesGroup):
    pick_booking = State()
    pick_type = State()


class GuestFlow(StatesGroup):
    pick_booking = State()
    guest_name = State()


class ResourceFlow(StatesGroup):
    add_name = State()
    add_type = State()
    edit_name = State()


class AdminMgmtFlow(StatesGroup):
    add_admin = State()
    remove_admin_pick = State()


def _valid_admin_id(text: str) -> bool:
    if not (bool(text) and text.isascii() and text.isdigit() and len(text) <= 15):
        return False
    return int(text) > 0 and str(int(text)) == text


# ── keyboards ─────────────────────────────────────────────────────────────────

MAX_LIST_BUTTONS = 25


def kb_main_menu(is_admin: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🪑 Забронировать место", callback_data="book_start")],
        [InlineKeyboardButton(text="🙋 Доп. услуги", callback_data="svc_start")],
        [InlineKeyboardButton(text="👥 Зарегистрировать гостя", callback_data="guest_start")],
        [InlineKeyboardButton(text="📋 Мои брони", callback_data="my_bookings")],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton(text="⚙️ Админ-панель", callback_data="admin_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_back(callback_data: str = "main_menu") -> InlineKeyboardButton:
    return InlineKeyboardButton(text="◀️ Назад", callback_data=callback_data)


def kb_flow_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="flow_cancel")],
    ])


def kb_resource_types(prefix: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=label, callback_data=f"{prefix}:{code}")]
            for code, label in RESOURCE_TYPES.items()]
    rows.append([kb_back()])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_dates(prefix: str, back_cb: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=_fmt_date_label(d), callback_data=f"{prefix}:{d}")]
            for d in _upcoming_dates()]
    rows.append([kb_back(back_cb)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_slots(prefix: str, back_cb: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"{s}–{e}", callback_data=f"{prefix}:{s}-{e}")]
            for s, e in TIME_WINDOWS]
    rows.append([kb_back(back_cb)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_resources(resources: list[tuple], prefix: str, back_cb: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=_esc(name, 40), callback_data=f"{prefix}:{rid}")]
            for rid, name in resources[:MAX_LIST_BUTTONS]]
    rows.append([kb_back(back_cb)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_tariffs() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=label, callback_data=f"book_tariff:{code}")]
            for code, label in TARIFFS.items()]
    rows.append([kb_back()])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_service_types() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=label, callback_data=f"svc_type:{code}")]
            for code, label in SERVICE_TYPES.items()]
    rows.append([kb_back()])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_my_bookings_pick(bookings: list[tuple], prefix: str) -> InlineKeyboardMarkup:
    rows = []
    for bid, rname, bdate, start, end in bookings[:MAX_LIST_BUTTONS]:
        label = f"{_esc(rname, 20)} · {_fmt_date_label(bdate)} {start}-{end}"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"{prefix}:{bid}")])
    if prefix == "svc_book":
        rows.append([InlineKeyboardButton(text="➡️ Без привязки к брони", callback_data=f"{prefix}:0")])
    rows.append([kb_back()])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_my_bookings_list(bookings: list[tuple]) -> InlineKeyboardMarkup:
    rows = []
    for bid, rname, bdate, start, end, status, cancellable in bookings:
        rows.append([InlineKeyboardButton(
            text=f"{'✅' if status == 'confirmed' else '❌' if status == 'cancelled' else '⏹'} "
                 f"{_esc(rname, 18)} · {_fmt_date_label(bdate)} {start}-{end}",
            callback_data=f"noop",
        )])
        if cancellable:
            rows.append([InlineKeyboardButton(text="❌ Отменить", callback_data=f"my_cancel:{bid}")])
    rows.append([kb_back()])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── admin keyboards ──

def kb_admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🖥 Ресурсы", callback_data="adm_res_menu")],
        [InlineKeyboardButton(text="📅 Расписание на дату", callback_data="adm_schedule")],
        [InlineKeyboardButton(text="🙋 Заявки на услуги", callback_data="adm_svc_list")],
        [InlineKeyboardButton(text="👥 Гости", callback_data="adm_guests")],
        [InlineKeyboardButton(text="👤 Админы", callback_data="adm_admins_menu")],
        [kb_back()],
    ])


def kb_res_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить ресурс", callback_data="adm_res_add")],
        [InlineKeyboardButton(text="📋 Список ресурсов", callback_data="adm_res_list")],
        [kb_back("admin_menu")],
    ])


def kb_res_list(rows: list[tuple]) -> InlineKeyboardMarkup:
    btns = [
        [InlineKeyboardButton(
            text=f"{'🟢' if active else '🔴'} {_esc(name, 30)} · {RESOURCE_TYPES.get(rtype, rtype)}",
            callback_data=f"adm_res_view:{rid}",
        )]
        for rid, name, rtype, active in rows[:MAX_LIST_BUTTONS]
    ]
    btns.append([kb_back("adm_res_menu")])
    return InlineKeyboardMarkup(inline_keyboard=btns)


def kb_res_detail(resource_id: int, is_active: bool) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="✏️ Изменить название", callback_data=f"adm_res_edit:{resource_id}")]]
    if is_active:
        rows.append([InlineKeyboardButton(text="🚫 Деактивировать", callback_data=f"adm_res_deact:{resource_id}")])
    rows.append([kb_back("adm_res_list")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_svc_requests(rows: list[tuple]) -> InlineKeyboardMarkup:
    btns = [
        [InlineKeyboardButton(
            text=f"№{sid} · {SERVICE_TYPES.get(stype, stype)} · клиент {cid}",
            callback_data=f"adm_svc_done:{sid}",
        )]
        for sid, stype, cid in rows[:MAX_LIST_BUTTONS]
    ]
    btns.append([kb_back("admin_menu")])
    return InlineKeyboardMarkup(inline_keyboard=btns)


def kb_admins_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить админа", callback_data="adm_add")],
        [InlineKeyboardButton(text="➖ Убрать админа", callback_data="adm_remove")],
        [kb_back("admin_menu")],
    ])


MAX_ADMIN_REMOVE_BUTTONS = 30


def kb_remove_admins(ids: list[str]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=admin_id, callback_data=f"adm_rm:{i}")] for i, admin_id in enumerate(ids)]
    rows.append([kb_back("adm_admins_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── /start ────────────────────────────────────────────────────────────────────

@router.message(Command("start"), F.chat.type == "private")
async def cmd_start(message: Message, state: FSMContext, config: CoworkingSpaceConfig):
    await state.clear()
    admins = _load_admins(config.admins_file)
    first_time_admin = not admins
    if first_time_admin:
        _save_admins(config.admins_file, {str(message.from_user.id)})
        admins = {str(message.from_user.id)}

    is_admin = str(message.from_user.id) in admins
    text = WELCOME_TEXT if is_admin else CLIENT_WELCOME_TEXT
    if config.welcome_image.exists():
        await message.answer_photo(
            FSInputFile(str(config.welcome_image)), caption=text,
            parse_mode="HTML", reply_markup=kb_main_menu(is_admin),
        )
    else:
        await message.answer(text, parse_mode="HTML", reply_markup=kb_main_menu(is_admin))
    if first_time_admin:
        await message.answer(
            "👑 <b>Вы — администратор этого бота.</b>\n\nУправление другими администраторами — «⚙️ Админ-панель» → «👤 Админы».",
            parse_mode="HTML",
        )


@router.callback_query(F.data == "main_menu")
async def cb_main_menu(cb: CallbackQuery, state: FSMContext, config: CoworkingSpaceConfig):
    await cb.answer()
    await state.clear()
    is_admin = _is_admin(cb.from_user.id, config)
    await cb.message.edit_text(WELCOME_TEXT, parse_mode="HTML", reply_markup=kb_main_menu(is_admin))


@router.callback_query(F.data == "flow_cancel")
async def cb_flow_cancel(cb: CallbackQuery, state: FSMContext, config: CoworkingSpaceConfig):
    await cb.answer()
    await state.clear()
    is_admin = _is_admin(cb.from_user.id, config)
    await cb.message.edit_text("Отменено.", reply_markup=kb_main_menu(is_admin))


@router.callback_query(F.data == "noop")
async def cb_noop(cb: CallbackQuery):
    await cb.answer()


# ── BOOKING flow: resource type → date → slot → resource → tariff → confirm ──

@router.callback_query(F.data == "book_start")
async def cb_book_start(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await state.set_state(BookFlow.pick_type)
    await state.update_data(started_at=time.time())
    await cb.message.edit_text("Выберите тип места:", reply_markup=kb_resource_types("book_type"))


@router.callback_query(BookFlow.pick_type, F.data.startswith("book_type:"))
async def cb_book_type(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_main_menu(False))
        return
    resource_type = cb.data.split(":", 1)[1]
    if resource_type not in RESOURCE_TYPES:
        return
    await state.update_data(resource_type=resource_type)
    await state.set_state(BookFlow.pick_date)
    await cb.message.edit_text("Выберите дату:", reply_markup=kb_dates("book_date", "book_start"))


@router.callback_query(BookFlow.pick_date, F.data.startswith("book_date:"))
async def cb_book_date(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_main_menu(False))
        return
    booking_date = cb.data.split(":", 1)[1]
    if booking_date not in _upcoming_dates():
        return
    await state.update_data(booking_date=booking_date)
    await state.set_state(BookFlow.pick_slot)
    await cb.message.edit_text("Выберите время:", reply_markup=kb_slots("book_slot", "book_start"))


@router.callback_query(BookFlow.pick_slot, F.data.startswith("book_slot:"))
async def cb_book_slot(cb: CallbackQuery, state: FSMContext, config: CoworkingSpaceConfig):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_main_menu(False))
        return
    slot = cb.data.split(":", 1)[1]
    try:
        start, end = slot.split("-", 1)
    except ValueError:
        return
    if (start, end) not in TIME_WINDOWS:
        return
    resource_type = data.get("resource_type")
    booking_date = data.get("booking_date")
    if not resource_type or not booking_date:
        await state.clear()
        await cb.message.edit_text("Сессия устарела, начните заново.", reply_markup=kb_main_menu(False))
        return
    async with aiosqlite.connect(config.db_path) as db:
        free = await _available_resources(db, resource_type, booking_date, start, end)
    if not free:
        await cb.message.edit_text(
            "😔 На выбранное время свободных мест этого типа нет. Выберите другое время:",
            reply_markup=kb_slots("book_slot", "book_start"),
        )
        return
    await state.update_data(time_slot_start=start, time_slot_end=end)
    await state.set_state(BookFlow.pick_resource)
    await cb.message.edit_text("Выберите место:", reply_markup=kb_resources(free, "book_res", "book_start"))


@router.callback_query(BookFlow.pick_resource, F.data.startswith("book_res:"))
async def cb_book_resource(cb: CallbackQuery, state: FSMContext, config: CoworkingSpaceConfig):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_main_menu(False))
        return
    try:
        resource_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    # Re-check availability right here too — time has passed since the list
    # was rendered and another client may have just taken this resource.
    booking_date = data.get("booking_date")
    start = data.get("time_slot_start")
    end = data.get("time_slot_end")
    if not booking_date or not start or not end:
        await state.clear()
        await cb.message.edit_text("Сессия устарела, начните заново.", reply_markup=kb_main_menu(False))
        return
    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute(
            "SELECT id FROM resources WHERE id=? AND is_active=1", (resource_id,)
        )).fetchone()
        if not row:
            await cb.message.edit_text("Это место больше недоступно.", reply_markup=kb_main_menu(False))
            await state.clear()
            return
        if await _has_overlap(db, resource_id, booking_date, start, end):
            await cb.message.edit_text(
                "😔 Это место только что заняли на выбранное время. Выберите другое:",
                reply_markup=kb_slots("book_slot", "book_start"),
            )
            await state.set_state(BookFlow.pick_slot)
            return
    await state.update_data(resource_id=resource_id)
    await state.set_state(BookFlow.pick_tariff)
    await cb.message.edit_text("Выберите тариф:", reply_markup=kb_tariffs())


@router.callback_query(BookFlow.pick_tariff, F.data.startswith("book_tariff:"))
async def cb_book_tariff(cb: CallbackQuery, state: FSMContext, config: CoworkingSpaceConfig):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_main_menu(False))
        return
    tariff = cb.data.split(":", 1)[1]
    if tariff not in TARIFFS:
        return
    resource_id = data.get("resource_id")
    booking_date = data.get("booking_date")
    start = data.get("time_slot_start")
    end = data.get("time_slot_end")
    if not resource_id or not booking_date or not start or not end:
        await state.clear()
        await cb.message.edit_text("Сессия устарела, начните заново.", reply_markup=kb_main_menu(False))
        return

    await state.clear()
    client_name = cb.from_user.full_name if cb.from_user else None

    # The single guarded transaction: BEGIN IMMEDIATE acquires SQLite's write
    # lock BEFORE the overlap SELECT runs, so a second near-simultaneous
    # caller's own BEGIN IMMEDIATE blocks until this transaction commits —
    # making check-then-insert atomic ACROSS connections, not just within
    # one (a bare SELECT does not take the write lock — see DESIGN NOTE
    # above). timeout=30 gives a blocked second writer up to 30s to acquire
    # the lock rather than failing immediately with "database is locked".
    async with aiosqlite.connect(config.db_path, timeout=30) as db:
        await db.execute("BEGIN IMMEDIATE")
        res_row = await (await db.execute(
            "SELECT name, is_active FROM resources WHERE id=?", (resource_id,)
        )).fetchone()
        if not res_row or not res_row[1]:
            await db.rollback()
            await cb.message.edit_text("Это место больше недоступно.", reply_markup=kb_main_menu(False))
            return
        if await _has_overlap(db, resource_id, booking_date, start, end):
            await db.rollback()
            await cb.message.edit_text(
                "😔 Это место только что заняли на выбранное время. Попробуйте забронировать заново.",
                reply_markup=kb_main_menu(False),
            )
            return
        cur = await db.execute(
            "INSERT INTO bookings (client_user_id, client_name, resource_id, booking_date, "
            "time_slot_start, time_slot_end, tariff, status) VALUES (?,?,?,?,?,?,?, 'confirmed')",
            (cb.from_user.id, client_name, resource_id, booking_date, start, end, tariff),
        )
        booking_id = cur.lastrowid
        await db.commit()
        res_name = res_row[0]

    await cb.message.edit_text(
        f"✅ <b>Бронь подтверждена!</b>\n\n"
        f"{_esc(res_name)} · {_fmt_date_label(booking_date)} {start}-{end}\n"
        f"Тариф: {TARIFFS.get(tariff, tariff)}\n"
        f"№{booking_id}",
        parse_mode="HTML", reply_markup=kb_main_menu(_is_admin(cb.from_user.id, config)),
    )


# ── MY BOOKINGS ──────────────────────────────────────────────────────────────

async def _client_bookings(db_path: str, client_user_id: int) -> list[tuple]:
    async with aiosqlite.connect(db_path) as db:
        rows = await (await db.execute(
            "SELECT b.id, r.name, b.booking_date, b.time_slot_start, b.time_slot_end, b.status "
            "FROM bookings b JOIN resources r ON r.id = b.resource_id "
            "WHERE b.client_user_id=? ORDER BY b.booking_date DESC, b.time_slot_start DESC LIMIT ?",
            (client_user_id, MAX_LIST_BUTTONS),
        )).fetchall()
    return rows


def _is_future_slot(booking_date: str, start: str) -> bool:
    try:
        dt = datetime.strptime(f"{booking_date} {start}", "%Y-%m-%d %H:%M")
    except ValueError:
        return False
    return dt > datetime.now()


@router.callback_query(F.data == "my_bookings")
async def cb_my_bookings(cb: CallbackQuery, state: FSMContext, config: CoworkingSpaceConfig):
    await cb.answer()
    await state.clear()
    rows = await _client_bookings(config.db_path, cb.from_user.id)
    if not rows:
        await cb.message.edit_text("У вас пока нет броней.", reply_markup=kb_main_menu(_is_admin(cb.from_user.id, config)))
        return
    enriched = [
        (bid, rname, bdate, start, end, status, status == "confirmed" and _is_future_slot(bdate, start))
        for bid, rname, bdate, start, end, status in rows
    ]
    await cb.message.edit_text("📋 <b>Мои брони:</b>", parse_mode="HTML", reply_markup=kb_my_bookings_list(enriched))


@router.callback_query(F.data.startswith("my_cancel:"))
async def cb_my_cancel(cb: CallbackQuery, config: CoworkingSpaceConfig):
    await cb.answer()
    try:
        booking_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute(
            "SELECT client_user_id, booking_date, time_slot_start, status FROM bookings WHERE id=?", (booking_id,)
        )).fetchone()
        if not row:
            await cb.message.edit_text("Бронь не найдена.", reply_markup=kb_main_menu(_is_admin(cb.from_user.id, config)))
            return
        owner_id, bdate, start, status = row
        if owner_id != cb.from_user.id:
            # A client must only ever cancel their OWN booking, never another
            # client's — bid is otherwise a guessable sequential integer.
            await cb.message.edit_text("Это не ваша бронь.", reply_markup=kb_main_menu(_is_admin(cb.from_user.id, config)))
            return
        if status != "confirmed" or not _is_future_slot(bdate, start):
            await cb.message.edit_text(
                "Эту бронь уже нельзя отменить (уже отменена/завершена/началась).",
                reply_markup=kb_main_menu(_is_admin(cb.from_user.id, config)),
            )
            return
        # Compare-and-swap: WHERE status='confirmed' makes a double-tap a
        # no-op on the second write, same pattern used across this repo.
        cur = await db.execute(
            "UPDATE bookings SET status='cancelled' WHERE id=? AND status='confirmed'", (booking_id,)
        )
        await db.commit()

    rows = await _client_bookings(config.db_path, cb.from_user.id)
    enriched = [
        (bid, rname, bd, s, e, st, st == "confirmed" and _is_future_slot(bd, s))
        for bid, rname, bd, s, e, st in rows
    ]
    note = "✅ Бронь отменена." if cur.rowcount else "Бронь уже была отменена."
    await cb.message.edit_text(f"{note}\n\n📋 <b>Мои брони:</b>", parse_mode="HTML",
                                reply_markup=kb_my_bookings_list(enriched))


# ── SERVICE REQUESTS ─────────────────────────────────────────────────────────

@router.callback_query(F.data == "svc_start")
async def cb_svc_start(cb: CallbackQuery, state: FSMContext, config: CoworkingSpaceConfig):
    await cb.answer()
    await state.clear()
    rows = await _client_bookings(config.db_path, cb.from_user.id)
    upcoming = [(bid, rname, bd, s, e) for bid, rname, bd, s, e, status in rows if status == "confirmed"]
    await state.set_state(ServiceFlow.pick_booking)
    await state.update_data(started_at=time.time())
    if not upcoming:
        await state.update_data(booking_id=None)
        await state.set_state(ServiceFlow.pick_type)
        await cb.message.edit_text("Выберите тип услуги:", reply_markup=kb_service_types())
        return
    await cb.message.edit_text(
        "Выберите бронь (или продолжите без привязки):", reply_markup=kb_my_bookings_pick(upcoming, "svc_book"),
    )


@router.callback_query(ServiceFlow.pick_booking, F.data.startswith("svc_book:"))
async def cb_svc_pick_booking(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_main_menu(False))
        return
    try:
        booking_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    await state.update_data(booking_id=booking_id or None)
    await state.set_state(ServiceFlow.pick_type)
    await cb.message.edit_text("Выберите тип услуги:", reply_markup=kb_service_types())


@router.callback_query(ServiceFlow.pick_type, F.data.startswith("svc_type:"))
async def cb_svc_pick_type(cb: CallbackQuery, state: FSMContext, config: CoworkingSpaceConfig, bot: Bot):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_main_menu(False))
        return
    service_type = cb.data.split(":", 1)[1]
    if service_type not in SERVICE_TYPES:
        return
    booking_id = data.get("booking_id")
    await state.clear()
    async with aiosqlite.connect(config.db_path) as db:
        # Booking, if provided, must belong to this client — same
        # ownership-guard principle as my_cancel above.
        if booking_id:
            owner = await (await db.execute(
                "SELECT client_user_id FROM bookings WHERE id=?", (booking_id,)
            )).fetchone()
            if not owner or owner[0] != cb.from_user.id:
                booking_id = None
        cur = await db.execute(
            "INSERT INTO service_requests (client_user_id, booking_id, service_type) VALUES (?,?,?)",
            (cb.from_user.id, booking_id, service_type),
        )
        request_id = cur.lastrowid
        await db.commit()
        admin_ids = _load_admins(config.admins_file)

    await cb.message.edit_text(
        f"✅ Заявка на услугу «{SERVICE_TYPES.get(service_type)}» отправлена. №{request_id}",
        reply_markup=kb_main_menu(_is_admin(cb.from_user.id, config)),
    )
    for admin_id in admin_ids:
        try:
            await bot.send_message(
                int(admin_id),
                f"🙋 Новая заявка на услугу №{request_id}: {SERVICE_TYPES.get(service_type)} "
                f"(клиент {cb.from_user.id})",
            )
        except (TelegramAPIError, ValueError) as e:
            logger.warning(f"coworking_space: failed to notify admin {admin_id} of service request {request_id}: {e}")


# ── GUEST REGISTRATION ───────────────────────────────────────────────────────

@router.callback_query(F.data == "guest_start")
async def cb_guest_start(cb: CallbackQuery, state: FSMContext, config: CoworkingSpaceConfig):
    await cb.answer()
    await state.clear()
    rows = await _client_bookings(config.db_path, cb.from_user.id)
    upcoming = [(bid, rname, bd, s, e) for bid, rname, bd, s, e, status in rows if status == "confirmed"]
    if not upcoming:
        await cb.message.edit_text(
            "У вас нет активных броней, к которым можно привязать гостя.",
            reply_markup=kb_main_menu(_is_admin(cb.from_user.id, config)),
        )
        return
    await state.set_state(GuestFlow.pick_booking)
    await state.update_data(started_at=time.time())
    await cb.message.edit_text("Выберите бронь для гостя:", reply_markup=kb_my_bookings_pick(upcoming, "guest_book"))


@router.callback_query(GuestFlow.pick_booking, F.data.startswith("guest_book:"))
async def cb_guest_pick_booking(cb: CallbackQuery, state: FSMContext, config: CoworkingSpaceConfig):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_main_menu(False))
        return
    try:
        booking_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    async with aiosqlite.connect(config.db_path) as db:
        owner = await (await db.execute("SELECT client_user_id FROM bookings WHERE id=?", (booking_id,))).fetchone()
    if not owner or owner[0] != cb.from_user.id:
        await state.clear()
        await cb.message.edit_text("Бронь не найдена.", reply_markup=kb_main_menu(_is_admin(cb.from_user.id, config)))
        return
    await state.update_data(booking_id=booking_id)
    await state.set_state(GuestFlow.guest_name)
    await cb.message.edit_text("Введите имя гостя:", reply_markup=kb_flow_cancel())


@router.message(GuestFlow.guest_name, F.text, ~F.text.startswith("/"))
async def guest_name(msg: Message, state: FSMContext, config: CoworkingSpaceConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_main_menu(False))
        return
    name = msg.text.strip()
    if not name:
        await msg.answer("Имя не может быть пустым. Введите имя гостя:", reply_markup=kb_flow_cancel())
        return
    if len(name) > 200:
        await msg.answer("⚠️ Слишком длинное имя. Введите короче 200 символов:", reply_markup=kb_flow_cancel())
        return
    booking_id = data.get("booking_id")
    if not booking_id:
        await state.clear()
        await msg.answer("Сессия устарела, начните заново.", reply_markup=kb_main_menu(False))
        return
    await state.clear()
    async with aiosqlite.connect(config.db_path) as db:
        owner = await (await db.execute("SELECT client_user_id FROM bookings WHERE id=?", (booking_id,))).fetchone()
        if not owner or owner[0] != msg.from_user.id:
            await msg.answer("Бронь не найдена.", reply_markup=kb_main_menu(_is_admin(msg.from_user.id, config)))
            return
        await db.execute(
            "INSERT INTO guest_registrations (booking_id, guest_name) VALUES (?,?)", (booking_id, name)
        )
        await db.commit()
    await msg.answer(
        f"✅ Гость «{_esc(name)}» зарегистрирован.", parse_mode="HTML",
        reply_markup=kb_main_menu(_is_admin(msg.from_user.id, config)),
    )


# ── ADMIN: menu ──────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin_menu")
async def cb_admin_menu(cb: CallbackQuery, state: FSMContext, config: CoworkingSpaceConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    await cb.message.edit_text("⚙️ <b>Админ-панель</b>", parse_mode="HTML", reply_markup=kb_admin_menu())


# ── ADMIN: resources CRUD ────────────────────────────────────────────────────

@router.callback_query(F.data == "adm_res_menu")
async def cb_adm_res_menu(cb: CallbackQuery, state: FSMContext, config: CoworkingSpaceConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    await cb.message.edit_text("🖥 <b>Ресурсы</b>", parse_mode="HTML", reply_markup=kb_res_menu())


@router.callback_query(F.data == "adm_res_list")
async def cb_adm_res_list(cb: CallbackQuery, config: CoworkingSpaceConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    async with aiosqlite.connect(config.db_path) as db:
        rows = await (await db.execute(
            "SELECT id, name, resource_type, is_active FROM resources ORDER BY id DESC LIMIT ?", (MAX_LIST_BUTTONS,)
        )).fetchall()
    if not rows:
        await cb.message.edit_text("Ресурсов пока нет.", reply_markup=kb_res_menu())
        return
    await cb.message.edit_text("📋 Ресурсы:", reply_markup=kb_res_list(rows))


@router.callback_query(F.data.startswith("adm_res_view:"))
async def cb_adm_res_view(cb: CallbackQuery, config: CoworkingSpaceConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        resource_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute(
            "SELECT name, resource_type, is_active FROM resources WHERE id=?", (resource_id,)
        )).fetchone()
        if not row:
            await cb.message.edit_text("Ресурс не найден.", reply_markup=kb_res_menu())
            return
        booking_count = (await (await db.execute(
            "SELECT COUNT(*) FROM bookings WHERE resource_id=?", (resource_id,)
        )).fetchone())[0]
    name, rtype, is_active = row
    status = "🟢 активен" if is_active else "🔴 деактивирован"
    text = (
        f"🖥 <b>{_esc(name)}</b>\n"
        f"Тип: {RESOURCE_TYPES.get(rtype, rtype)}\n"
        f"Статус: {status}\n"
        f"Броней в истории: {booking_count}"
    )
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_res_detail(resource_id, bool(is_active)))


@router.callback_query(F.data == "adm_res_add")
async def cb_adm_res_add(cb: CallbackQuery, state: FSMContext, config: CoworkingSpaceConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    await state.set_state(ResourceFlow.add_name)
    await state.update_data(started_at=time.time())
    await cb.message.edit_text("Введите название нового ресурса (например «Стол №3»):", reply_markup=kb_flow_cancel())


@router.message(ResourceFlow.add_name, F.text, ~F.text.startswith("/"))
async def adm_res_add_name(msg: Message, state: FSMContext, config: CoworkingSpaceConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_main_menu(True))
        return
    if not _is_admin(msg.from_user.id, config):
        return
    name = msg.text.strip()
    if not name:
        await msg.answer("Название не может быть пустым. Введите название:", reply_markup=kb_flow_cancel())
        return
    if len(name) > 100:
        await msg.answer("⚠️ Слишком длинное название. Введите короче 100 символов:", reply_markup=kb_flow_cancel())
        return
    await state.update_data(pending_name=name)
    await state.set_state(ResourceFlow.add_type)
    await msg.answer("Выберите тип ресурса:", reply_markup=kb_resource_types("adm_res_type"))


@router.callback_query(ResourceFlow.add_type, F.data.startswith("adm_res_type:"))
async def cb_adm_res_add_type(cb: CallbackQuery, state: FSMContext, config: CoworkingSpaceConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_main_menu(True))
        return
    resource_type = cb.data.split(":", 1)[1]
    if resource_type not in RESOURCE_TYPES:
        return
    name = data.get("pending_name")
    if not name:
        await state.clear()
        await cb.message.edit_text("Сессия устарела, начните заново.", reply_markup=kb_res_menu())
        return
    await state.clear()
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute(
            "INSERT INTO resources (name, resource_type) VALUES (?,?)", (name, resource_type)
        )
        await db.commit()
    await cb.message.edit_text(
        f"✅ Ресурс «{_esc(name)}» ({RESOURCE_TYPES.get(resource_type)}) добавлен.",
        parse_mode="HTML", reply_markup=kb_res_menu(),
    )


@router.callback_query(F.data.startswith("adm_res_edit:"))
async def cb_adm_res_edit(cb: CallbackQuery, state: FSMContext, config: CoworkingSpaceConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        resource_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute("SELECT id FROM resources WHERE id=?", (resource_id,))).fetchone()
    if not row:
        await cb.message.edit_text("Ресурс не найден.", reply_markup=kb_res_menu())
        return
    await state.set_state(ResourceFlow.edit_name)
    await state.update_data(started_at=time.time(), resource_id=resource_id)
    await cb.message.edit_text("Введите новое название:", reply_markup=kb_flow_cancel())


@router.message(ResourceFlow.edit_name, F.text, ~F.text.startswith("/"))
async def adm_res_edit_name(msg: Message, state: FSMContext, config: CoworkingSpaceConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_main_menu(True))
        return
    if not _is_admin(msg.from_user.id, config):
        return
    name = msg.text.strip()
    if not name:
        await msg.answer("Название не может быть пустым. Введите название:", reply_markup=kb_flow_cancel())
        return
    if len(name) > 100:
        await msg.answer("⚠️ Слишком длинное название. Введите короче 100 символов:", reply_markup=kb_flow_cancel())
        return
    resource_id = data.get("resource_id")
    await state.clear()
    if not resource_id:
        await msg.answer("Сессия устарела, начните заново.", reply_markup=kb_res_menu())
        return
    async with aiosqlite.connect(config.db_path) as db:
        cur = await db.execute("UPDATE resources SET name=? WHERE id=?", (name, resource_id))
        await db.commit()
    if cur.rowcount == 0:
        await msg.answer("Ресурс не найден.", reply_markup=kb_res_menu())
        return
    await msg.answer(f"✅ Название обновлено: «{_esc(name)}»", parse_mode="HTML", reply_markup=kb_res_menu())


@router.callback_query(F.data.startswith("adm_res_deact:"))
async def cb_adm_res_deactivate(cb: CallbackQuery, config: CoworkingSpaceConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        resource_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    async with aiosqlite.connect(config.db_path) as db:
        # Soft-delete only — a resource with booking history is never hard
        # deleted, per the design brief (history must remain queryable).
        cur = await db.execute("UPDATE resources SET is_active=0 WHERE id=? AND is_active=1", (resource_id,))
        await db.commit()
    if cur.rowcount == 0:
        await cb.message.edit_text("Ресурс не найден или уже деактивирован.", reply_markup=kb_res_menu())
        return
    await cb.message.edit_text("✅ Ресурс деактивирован. Он больше не будет предлагаться для новых броней.",
                                reply_markup=kb_res_menu())


# ── ADMIN: schedule for a date ───────────────────────────────────────────────

@router.callback_query(F.data == "adm_schedule")
async def cb_adm_schedule(cb: CallbackQuery, state: FSMContext, config: CoworkingSpaceConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    await cb.message.edit_text("Выберите дату:", reply_markup=kb_dates("adm_sched_date", "admin_menu"))


@router.callback_query(F.data.startswith("adm_sched_date:"))
async def cb_adm_schedule_date(cb: CallbackQuery, config: CoworkingSpaceConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    booking_date = cb.data.split(":", 1)[1]
    async with aiosqlite.connect(config.db_path) as db:
        rows = await (await db.execute(
            "SELECT r.name, b.time_slot_start, b.time_slot_end, b.status, b.client_name, b.client_user_id "
            "FROM bookings b JOIN resources r ON r.id = b.resource_id "
            "WHERE b.booking_date=? ORDER BY r.name, b.time_slot_start", (booking_date,)
        )).fetchall()
    if not rows:
        await cb.message.edit_text(
            f"📅 {_fmt_date_label(booking_date)}: броней нет.", reply_markup=kb_dates("adm_sched_date", "admin_menu"),
        )
        return
    lines = [f"📅 <b>Расписание на {_fmt_date_label(booking_date)}:</b>\n"]
    current_resource = None
    for rname, start, end, status, client_name, client_id in rows:
        if rname != current_resource:
            lines.append(f"\n<b>{_esc(rname)}</b>")
            current_resource = rname
        icon = "✅" if status == "confirmed" else "❌" if status == "cancelled" else "⏹"
        lines.append(f"{icon} {start}-{end} · {_esc(client_name or str(client_id))}")
    await cb.message.edit_text(_join_bounded(lines), parse_mode="HTML",
                                reply_markup=kb_dates("adm_sched_date", "admin_menu"))


# ── ADMIN: service requests ──────────────────────────────────────────────────

@router.callback_query(F.data == "adm_svc_list")
async def cb_adm_svc_list(cb: CallbackQuery, config: CoworkingSpaceConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    async with aiosqlite.connect(config.db_path) as db:
        rows = await (await db.execute(
            "SELECT id, service_type, client_user_id FROM service_requests "
            "WHERE status='requested' ORDER BY id LIMIT ?", (MAX_LIST_BUTTONS,)
        )).fetchall()
    if not rows:
        await cb.message.edit_text("Открытых заявок нет.", reply_markup=kb_admin_menu())
        return
    await cb.message.edit_text("🙋 <b>Заявки на услуги:</b>", parse_mode="HTML", reply_markup=kb_svc_requests(rows))


@router.callback_query(F.data.startswith("adm_svc_done:"))
async def cb_adm_svc_done(cb: CallbackQuery, config: CoworkingSpaceConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        request_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    async with aiosqlite.connect(config.db_path) as db:
        cur = await db.execute(
            "UPDATE service_requests SET status='done' WHERE id=? AND status='requested'", (request_id,)
        )
        await db.commit()
        rows = await (await db.execute(
            "SELECT id, service_type, client_user_id FROM service_requests "
            "WHERE status='requested' ORDER BY id LIMIT ?", (MAX_LIST_BUTTONS,)
        )).fetchall()
    note = "✅ Заявка закрыта." if cur.rowcount else "Заявка уже была закрыта."
    if not rows:
        await cb.message.edit_text(f"{note}\n\nОткрытых заявок больше нет.", reply_markup=kb_admin_menu())
        return
    await cb.message.edit_text(f"{note}\n\n🙋 <b>Заявки на услуги:</b>", parse_mode="HTML",
                                reply_markup=kb_svc_requests(rows))


# ── ADMIN: guests ────────────────────────────────────────────────────────────

@router.callback_query(F.data == "adm_guests")
async def cb_adm_guests(cb: CallbackQuery, config: CoworkingSpaceConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    async with aiosqlite.connect(config.db_path) as db:
        rows = await (await db.execute(
            "SELECT g.guest_name, g.created_at, b.booking_date, b.time_slot_start, r.name "
            "FROM guest_registrations g "
            "JOIN bookings b ON b.id = g.booking_id "
            "JOIN resources r ON r.id = b.resource_id "
            "ORDER BY g.id DESC LIMIT 50"
        )).fetchall()
    if not rows:
        await cb.message.edit_text("Гостей пока нет.", reply_markup=kb_admin_menu())
        return
    lines = ["👥 <b>Гости:</b>\n"] + [
        f"• {_esc(name)} · {_esc(rname, 20)} · {_fmt_date_label(bdate)} {start}"
        for name, created_at, bdate, start, rname in rows
    ]
    await cb.message.edit_text(_join_bounded(lines), parse_mode="HTML", reply_markup=kb_admin_menu())


# ── ADMIN: admins management ─────────────────────────────────────────────────

async def _admins_list_text(config: CoworkingSpaceConfig) -> str:
    ids = sorted(_load_admins(config.admins_file))
    if not ids:
        return "👥 Пусто"
    return _join_bounded(["👥 <b>Администраторы бота:</b>\n"] + [f"• <code>{_esc(i)}</code>" for i in ids])


@router.callback_query(F.data == "adm_admins_menu")
async def cb_adm_admins_menu(cb: CallbackQuery, state: FSMContext, config: CoworkingSpaceConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    text = await _admins_list_text(config)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_admins_menu())


@router.callback_query(F.data == "adm_add")
async def cb_adm_add(cb: CallbackQuery, state: FSMContext, config: CoworkingSpaceConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.set_state(AdminMgmtFlow.add_admin)
    await state.update_data(started_at=time.time())
    await cb.message.edit_text("Введите Telegram ID нового администратора:", reply_markup=kb_flow_cancel())


@router.message(AdminMgmtFlow.add_admin, F.text, ~F.text.startswith("/"))
async def admin_add_id(msg: Message, state: FSMContext, config: CoworkingSpaceConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_main_menu(True))
        return
    if not _is_admin(msg.from_user.id, config):
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
async def cb_adm_remove(cb: CallbackQuery, state: FSMContext, config: CoworkingSpaceConfig):
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
async def cb_adm_remove_pick(cb: CallbackQuery, state: FSMContext, config: CoworkingSpaceConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_main_menu(True))
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
