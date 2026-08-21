# TEMPLATE: staff_scheduler
# USE FOR: расписание работы сотрудников (не расписание занятий для клиентов) — учёт смен по сотруднику и по дню, явное предупреждение о пересечении смен при добавлении, обзор на текущую и следующую неделю
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
from datetime import date, timedelta
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
# runtime state. Employees/shifts themselves are runtime data, added/removed
# via this bot's own menus, not by editing this file.
BOT_DESCRIPTION = "Расписание работы сотрудников: смены по дням и по сотруднику, предупреждение о пересечении смен, обзор на текущую и следующую неделю."
WELCOME_TEXT = (
    "📅 <b>Расписание сотрудников</b>\n\n"
    "Учёт РАБОЧИХ смен сотрудников (не запись клиентов на занятия) — кто, "
    "когда и с какого по какое время работает. Обзор на текущую и следующую "
    "неделю, по дням или по сотруднику.\n\nВыберите действие:"
)
NO_ACCESS_TEXT = "Это внутренний инструмент администратора — у вас нет доступа."
# Почасовая сетка времени начала/конца смены — подогнать под реальные часы
# работы бизнеса. Ночные смены поддержаны (см. _find_conflicts): конец <=
# начала трактуется как переход через полночь.
SHIFT_TIME_OPTIONS = [f"{h:02d}:00" for h in range(6, 24)]
WINDOW_DAYS = 14  # текущая + следующая неделя (2 x 7 дней, с понедельника)
NAME_MAX_LEN = 100
POSITION_MAX_LEN = 100
CONTACT_MAX_LEN = 200
NOTE_MAX_LEN = 300
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
            "name": "employees",
            "table": "employees",
            "order_by": "id DESC",
            "creatable": True,
            "title": "Сотрудники",
            "titleField": "name",
            "fields": [
                {"name": "name", "required": True, "label": "Имя", "kind": "text", "list": True, "detail": True, "create": True},
                {"name": "position", "label": "Должность", "kind": "text", "list": True, "detail": True, "create": True},
                {"name": "contact", "required": True, "label": "Контакт", "kind": "text", "list": False, "detail": True, "create": True},
                {"name": "active", "label": "Активен", "kind": "bool", "list": False, "detail": True, "create": True},
            ],
        },
        {
            "name": "shifts",
            "table": "shifts",
            "order_by": "shift_date DESC",
            "creatable": True,
            "title": "Смены",
            "titleField": "shift_date",
            "fields": [
                {"name": "employee_id", "required": True, "label": "ID сотрудника", "kind": "number", "list": True, "detail": True, "create": True},
                {"name": "shift_date", "required": True, "label": "Дата", "kind": "date", "list": True, "detail": True, "create": True},
                {"name": "start_time", "required": True, "label": "Начало", "kind": "text", "list": True, "detail": True, "create": True},
                {"name": "end_time", "required": True, "label": "Окончание", "kind": "text", "list": True, "detail": True, "create": True},
                {"name": "note", "label": "Заметка", "kind": "text", "list": False, "detail": True, "create": True},
                {"name": "created_at", "label": "Создана", "kind": "date", "list": False, "detail": True, "create": False},
            ],
        },
    ],
}

DAYS_RU = {0: "Пн", 1: "Вт", 2: "Ср", 3: "Чт", 4: "Пт", 5: "Сб", 6: "Вс"}
MONTHS_RU = {1: "янв", 2: "фев", 3: "мар", 4: "апр", 5: "май", 6: "июн",
             7: "июл", 8: "авг", 9: "сен", 10: "окт", 11: "ноя", 12: "дек"}


def _date_label(iso_date: str) -> str:
    d = date.fromisoformat(iso_date)
    return f"{DAYS_RU[d.weekday()]} {d.day} {MONTHS_RU[d.month]}"


# Same rationale as every other template's FLOW_TIMEOUT_SECONDS: MemoryStorage
# keeps a state until explicitly cleared — without an expiry, an admin who
# opens an add-shift/add-employee prompt and goes quiet has every LATER plain
# message silently captured by that stale flow step.
FLOW_TIMEOUT_SECONDS = 300


def _flow_expired(data: dict) -> bool:
    started_at = data.get("started_at")
    return started_at is None or (time.time() - started_at) > FLOW_TIMEOUT_SECONDS


def _esc(value, max_len: int = 500) -> str:
    """HTML-escapes AND length-bounds any user-supplied text before it goes
    into a parse_mode="HTML" message — same helper/rationale as
    debtors.py's/accountant.py's _esc()."""
    text = str(value) if value is not None else ""
    if len(text) > max_len:
        text = text[:max_len] + "…"
    return html.escape(text)


def _short(label: str, max_len: int = 40) -> str:
    return label if len(label) <= max_len else label[:max_len - 1] + "…"


def _valid_telegram_id(text: str) -> bool:
    """Same guard as every other template's admin-id validator."""
    if not (bool(text) and text.isascii() and text.isdigit() and len(text) <= 15):
        return False
    return int(text) > 0 and str(int(text)) == text


def _parse_id(text: str) -> int | None:
    """callback_data is bot-generated, but Telegram's API doesn't stop a
    custom client from sending arbitrary callback_query data — int() on an
    unguarded split() would raise on a forged value. Same rationale as
    debtors.py's _parse_id."""
    try:
        return int(text)
    except ValueError:
        return None


# ── week window ──────────────────────────────────────────────────────────────

def _week_window(today: date) -> list[str]:
    """Monday of the current ISO week through 13 days later — 2 full Mon-Sun
    weeks, regardless of which weekday "today" happens to be."""
    monday = today - timedelta(days=today.weekday())
    return [(monday + timedelta(days=i)).isoformat() for i in range(WINDOW_DAYS)]


# ── shift-overlap detection ────────────────────────────────────────────────

def _to_minutes(t: str) -> int:
    hh, mm = t.split(":")
    return int(hh) * 60 + int(mm)


def _shift_range_minutes(anchor: date, shift_date: str, start: str, end: str) -> tuple[int, int]:
    """Minute range on a timeline anchored at `anchor` 00:00. end<=start means
    the shift crosses into the next calendar day (night shift, e.g. 22:00-06:00)."""
    day_offset = (date.fromisoformat(shift_date) - anchor).days
    start_m = day_offset * 1440 + _to_minutes(start)
    end_offset = day_offset if _to_minutes(end) > _to_minutes(start) else day_offset + 1
    end_m = end_offset * 1440 + _to_minutes(end)
    return start_m, end_m


def _shifts_overlap(date_a: str, start_a: str, end_a: str, date_b: str, start_b: str, end_b: str) -> bool:
    anchor = min(date.fromisoformat(date_a), date.fromisoformat(date_b))
    s1, e1 = _shift_range_minutes(anchor, date_a, start_a, end_a)
    s2, e2 = _shift_range_minutes(anchor, date_b, start_b, end_b)
    return s1 < e2 and s2 < e1


# ── config ───────────────────────────────────────────────────────────────────
# Same shape/contract as every other template's Config dataclass — see
# docs/STAGE2_DESIGN.md "Config-контракт шаблона".

@dataclass
class StaffSchedulerConfig:
    bot_name: str
    db_path: str
    admins_file: Path
    welcome_image: Path
    display_name: str | None = None
    group_chat_id: str | None = None
    bot_id: int | None = None
    owner_telegram_id: int | None = None


def _paths_for(name: str, data_dir: Path) -> StaffSchedulerConfig:
    return StaffSchedulerConfig(
        bot_name=name,
        db_path=str(data_dir / f"{name}_data.db"),
        admins_file=data_dir / f"admins_{name}.json",
        welcome_image=data_dir / "bot_images" / f"{name}.jpg",
    )


def config_from_env() -> StaffSchedulerConfig:
    """Standalone/subprocess mode."""
    name = Path(__file__).stem
    data_dir = Path(os.getenv("DATA_DIR", "./data"))
    data_dir.mkdir(exist_ok=True)
    return _paths_for(name, data_dir)


def config_from_bot_row(bot_row: dict, data_dir: Path) -> StaffSchedulerConfig:
    """Webhook runtime mode. Paths keyed by bot_row["bot_id"] (bots.id, the
    physically unique AUTOINCREMENT PK) — NOT bot_row["name"] — same
    reasoning as every other template's config_from_bot_row."""
    bot_id = bot_row["bot_id"]
    config = StaffSchedulerConfig(
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
    """Injects this bot's StaffSchedulerConfig into data["config"]."""

    def __init__(self, config: StaffSchedulerConfig) -> None:
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

def _is_bot_admin(user_id: int, config: StaffSchedulerConfig) -> bool:
    # The DB-known owner (bots.owner_telegram_id) is always an admin, even if
    # the local admins_file is empty/stale/hijacked — see cmd_start below for
    # why the file alone can't be trusted as the sole source of truth.
    if config.owner_telegram_id is not None and str(user_id) == str(config.owner_telegram_id):
        return True
    return str(user_id) in _load_admins(config.admins_file)


# ── db ────────────────────────────────────────────────────────────────────────

async def init_db(db_path: str):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS employees (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                name     TEXT NOT NULL,
                position TEXT,
                contact  TEXT NOT NULL,
                active   INTEGER NOT NULL DEFAULT 1
            )
        """)
        # end_time <= start_time means the shift crosses into the next
        # calendar day (night shift) — see _shift_range_minutes.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS shifts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                employee_id INTEGER NOT NULL REFERENCES employees(id),
                shift_date  TEXT NOT NULL,
                start_time  TEXT NOT NULL,
                end_time    TEXT NOT NULL,
                note        TEXT,
                created_at  TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_shifts_employee_date ON shifts(employee_id, shift_date)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_shifts_date ON shifts(shift_date)")
        await db.commit()


async def _insert_employee(db_path: str, name: str, position: str | None, contact: str) -> int:
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "INSERT INTO employees (name, position, contact) VALUES (?,?,?)",
            (name, position, contact),
        )
        await db.commit()
        return cur.lastrowid

async def _employee_row(db_path: str, employee_id: int) -> dict | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute("SELECT * FROM employees WHERE id=?", (employee_id,))).fetchone()
        return dict(row) if row else None

async def _active_employees(db_path: str) -> list[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT * FROM employees WHERE active=1 ORDER BY name"
        )).fetchall()
        return [dict(r) for r in rows]

async def _all_employees(db_path: str) -> list[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT * FROM employees ORDER BY active DESC, name"
        )).fetchall()
        return [dict(r) for r in rows]

async def _deactivate_employee(db_path: str, employee_id: int) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute("UPDATE employees SET active=0 WHERE id=?", (employee_id,))
        await db.commit()


async def _insert_shift(db_path: str, employee_id: int, shift_date: str, start: str, end: str, note: str | None) -> int:
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "INSERT INTO shifts (employee_id, shift_date, start_time, end_time, note) VALUES (?,?,?,?,?)",
            (employee_id, shift_date, start, end, note),
        )
        await db.commit()
        return cur.lastrowid

async def _shift_row_with_employee(db_path: str, shift_id: int) -> dict | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute("""
            SELECT s.*, e.name AS employee_name, e.position AS employee_position
            FROM shifts s JOIN employees e ON e.id = s.employee_id
            WHERE s.id=?
        """, (shift_id,))).fetchone()
        return dict(row) if row else None

async def _delete_shift(db_path: str, shift_id: int) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute("DELETE FROM shifts WHERE id=?", (shift_id,))
        await db.commit()

async def _shifts_for_day(db_path: str, shift_date: str) -> list[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute("""
            SELECT s.*, e.name AS employee_name
            FROM shifts s JOIN employees e ON e.id = s.employee_id
            WHERE s.shift_date=?
            ORDER BY s.start_time, s.id
        """, (shift_date,))).fetchall()
        return [dict(r) for r in rows]

async def _shifts_for_employee_window(db_path: str, employee_id: int, window: list[str]) -> list[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute("""
            SELECT * FROM shifts
            WHERE employee_id=? AND shift_date BETWEEN ? AND ?
            ORDER BY shift_date, start_time
        """, (employee_id, window[0], window[-1]))).fetchall()
        return [dict(r) for r in rows]

async def _shifts_for_employee_on_dates(db_path: str, employee_id: int, dates: list[str]) -> list[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        placeholders = ",".join("?" * len(dates))
        rows = await (await db.execute(
            f"SELECT * FROM shifts WHERE employee_id=? AND shift_date IN ({placeholders})",
            (employee_id, *dates),
        )).fetchall()
        return [dict(r) for r in rows]

async def _find_conflicts(db_path: str, employee_id: int, shift_date: str, start: str, end: str,
                           exclude_shift_id: int | None = None) -> list[dict]:
    d = date.fromisoformat(shift_date)
    candidates = await _shifts_for_employee_on_dates(
        db_path, employee_id, [(d - timedelta(days=1)).isoformat(), shift_date]
    )
    conflicts = []
    for c in candidates:
        if exclude_shift_id is not None and c["id"] == exclude_shift_id:
            continue
        if _shifts_overlap(shift_date, start, end, c["shift_date"], c["start_time"], c["end_time"]):
            conflicts.append(c)
    return conflicts


# Guards shift-confirm against a rapid double-tap inserting the same shift
# twice — same pattern/rationale as debtors.py's _busy_entry_saves, keyed by
# the tapping user's id since a single admin is what double-taps.
_busy_shift_confirms: set[int] = set()


# ── keyboards: main ─────────────────────────────────────────────────────────

def kb_main() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="➕ Добавить смену")],
        [KeyboardButton(text="📅 Расписание на неделю")],
        [KeyboardButton(text="🧑 Расписание по сотруднику")],
        [KeyboardButton(text="👥 Сотрудники")],
        [KeyboardButton(text="⚙️ Админы бота")],
    ], resize_keyboard=True)


def kb_cancel(callback_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data=callback_data)]])


# ── keyboards: add-shift flow ─────────────────────────────────────────────────

def kb_pick_employee(employees: list[dict], prefix: str) -> InlineKeyboardMarkup:
    rows = []
    for e in employees:
        label = e["name"] if not e["position"] else f"{e['name']} ({e['position']})"
        rows.append([InlineKeyboardButton(text=_short(f"🧑 {label}", 45), callback_data=f"{prefix}:{e['id']}")])
    if not rows:
        rows.append([InlineKeyboardButton(text="Сотрудников нет", callback_data="ssc_noop")])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="ssc_shift_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_pick_date(window: list[str]) -> InlineKeyboardMarkup:
    rows = []
    for i in range(0, WINDOW_DAYS, 2):
        if i == 7:
            rows.append([InlineKeyboardButton(text="── Следующая неделя ──", callback_data="ssc_noop")])
        chunk = window[i:i + 2]
        rows.append([InlineKeyboardButton(text=_date_label(d), callback_data=f"ssc_shift_date:{d}") for d in chunk])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="ssc_shift_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_pick_time(prefix: str) -> InlineKeyboardMarkup:
    rows = []
    for i in range(0, len(SHIFT_TIME_OPTIONS), 4):
        chunk = SHIFT_TIME_OPTIONS[i:i + 4]
        rows.append([InlineKeyboardButton(text=t, callback_data=f"{prefix}:{t}") for t in chunk])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="ssc_shift_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_note_step() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭ Без заметки", callback_data="ssc_shift_skip_note")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="ssc_shift_cancel")],
    ])

def kb_shift_confirm(has_conflict: bool) -> InlineKeyboardMarkup:
    confirm_label = "⚠️ Всё равно добавить" if has_conflict else "✅ Подтвердить"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=confirm_label, callback_data="ssc_shift_confirm")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="ssc_shift_cancel")],
    ])


# ── keyboards: week/day view ──────────────────────────────────────────────────

def kb_pick_day(window: list[str]) -> InlineKeyboardMarkup:
    rows = []
    for i in range(0, WINDOW_DAYS, 2):
        if i == 7:
            rows.append([InlineKeyboardButton(text="── Следующая неделя ──", callback_data="ssc_noop")])
        chunk = window[i:i + 2]
        rows.append([InlineKeyboardButton(text=_date_label(d), callback_data=f"ssc_day:{d}") for d in chunk])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_shift_list(shifts: list[dict], back_callback: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text=_short(f"🕐 {s['start_time']}–{s['end_time']} {s['employee_name']}", 45),
            callback_data=f"ssc_shift_view:{s['id']}",
        )] for s in shifts
    ]
    if not rows:
        rows.append([InlineKeyboardButton(text="Смен нет", callback_data="ssc_noop")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=back_callback)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── keyboards: employee picker (view-by-employee) ─────────────────────────────

def kb_pick_employee_view(employees: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for e in employees:
        mark = "" if e["active"] else "❌ "
        label = e["name"] if not e["position"] else f"{e['name']} ({e['position']})"
        rows.append([InlineKeyboardButton(text=_short(f"🧑 {mark}{label}", 45), callback_data=f"ssc_vemp_view:{e['id']}")])
    if not rows:
        rows.append([InlineKeyboardButton(text="Сотрудников нет", callback_data="ssc_noop")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── keyboards: shift detail / delete ──────────────────────────────────────────

def kb_shift_detail(shift_id: int, back_callback: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"ssc_shift_del_ask:{shift_id}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=back_callback)],
    ])

def kb_shift_delete_confirm(shift_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"ssc_shift_del_confirm:{shift_id}"),
        InlineKeyboardButton(text="❌ Отмена", callback_data=f"ssc_shift_view:{shift_id}"),
    ]])


# ── keyboards: employee management ────────────────────────────────────────────

def kb_employees_mgmt(employees: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for e in employees:
        label = e["name"] if not e["position"] else f"{e['name']} ({e['position']})"
        rows.append([InlineKeyboardButton(text=_short(f"➖ {label}", 40), callback_data=f"ssc_emgmt_rm_ask:{e['id']}")])
    rows.append([InlineKeyboardButton(text="➕ Добавить сотрудника", callback_data="ssc_emgmt_add")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_position_step() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭ Без должности", callback_data="ssc_emgmt_skip_position")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="ssc_emgmt_list")],
    ])

def kb_employee_delete_confirm(employee_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"ssc_emgmt_rm_confirm:{employee_id}"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="ssc_emgmt_list"),
    ]])


# ── keyboards: admins panel (same pattern as debtors.py/moderator.py) ────────

def kb_admins_panel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить админа", callback_data="ssc_adm_addadmin")],
        [InlineKeyboardButton(text="➖ Убрать админа", callback_data="ssc_adm_removeadmin")],
    ])

MAX_ADMIN_REMOVE_BUTTONS = 30

def kb_remove_admins(ids: list[str]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=admin_id, callback_data=f"ssc_adm_rma:{i}")] for i, admin_id in enumerate(ids)]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="ssc_adm_admins")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── FSM ───────────────────────────────────────────────────────────────────────

class ShiftFlow(StatesGroup):
    note = State()   # only step needing raw text; employee/date/start/end are button taps

class EmployeeFlow(StatesGroup):
    name = State(); position = State(); contact = State()

class AdminFlow(StatesGroup):
    add_admin = State(); remove_admin_pick = State()


# ── /start ────────────────────────────────────────────────────────────────────

@router.message(Command("start"), F.chat.type == "private")
async def cmd_start(message: Message, state: FSMContext, config: StaffSchedulerConfig):
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
        admins = {str(sender_id)}
        _save_admins(config.admins_file, admins)
        if config.bot_id is not None:
            try:
                await add_bot_admin(config.bot_id, str(sender_id))
            except Exception as e:
                logger.warning(f"cmd_start: add_bot_admin sync failed for bot {config.bot_id}: {e}")
    is_admin = _is_bot_admin(sender_id, config)
    if not is_admin:
        await message.answer(NO_ACCESS_TEXT)
        return
    if config.welcome_image.exists():
        await message.answer_photo(FSInputFile(str(config.welcome_image)),
                                   caption=WELCOME_TEXT, parse_mode="HTML", reply_markup=kb_main())
    else:
        await message.answer(WELCOME_TEXT, parse_mode="HTML", reply_markup=kb_main())
    if first_time_admin:
        await message.answer(
            "👑 <b>Вы — администратор этого бота.</b>\n\n"
            "Кнопка «👥 Сотрудники» — заведите первого сотрудника, чтобы можно "
            "было добавлять смены.",
            parse_mode="HTML",
        )


@router.callback_query(F.data == "ssc_noop")
async def cb_noop(cb: CallbackQuery):
    await cb.answer()


# ── ADD SHIFT ─────────────────────────────────────────────────────────────────

@router.message(F.text == "➕ Добавить смену", F.chat.type == "private")
async def shift_new_entry(msg: Message, state: FSMContext, config: StaffSchedulerConfig):
    if not _is_bot_admin(msg.from_user.id, config):
        await msg.answer(NO_ACCESS_TEXT); return
    await state.clear()
    employees = await _active_employees(config.db_path)
    if not employees:
        await msg.answer(
            "Сначала добавьте хотя бы одного сотрудника.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Добавить сотрудника", callback_data="ssc_emgmt_add")],
            ]),
        )
        return
    await state.update_data(started_at=time.time())
    await msg.answer("🧑 Выберите сотрудника:", reply_markup=kb_pick_employee(employees, "ssc_shift_employee"))

@router.callback_query(F.data.startswith("ssc_shift_employee:"))
async def cb_shift_pick_employee(cb: CallbackQuery, state: FSMContext, config: StaffSchedulerConfig):
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("⏳ Время ожидания истекло — начните заново кнопкой «➕ Добавить смену»."); return
    employee_id = _parse_id(cb.data.split(":", 1)[1])
    employee = None if employee_id is None else await _employee_row(config.db_path, employee_id)
    if employee is None or not employee["active"]:
        await cb.message.edit_text("Сотрудник не найден или удалён."); return
    await state.update_data(started_at=time.time(), employee_id=employee_id)
    window = _week_window(date.today())
    await cb.message.edit_text(
        f"📅 Смена для <b>{_esc(employee['name'])}</b>. Выберите дату:",
        parse_mode="HTML", reply_markup=kb_pick_date(window),
    )

@router.callback_query(F.data.startswith("ssc_shift_date:"))
async def cb_shift_pick_date(cb: CallbackQuery, state: FSMContext, config: StaffSchedulerConfig):
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    data = await state.get_data()
    if _flow_expired(data) or "employee_id" not in data:
        await state.clear()
        await cb.message.edit_text("⏳ Время ожидания истекло — начните заново кнопкой «➕ Добавить смену»."); return
    shift_date = cb.data.split(":", 1)[1]
    try:
        date.fromisoformat(shift_date)
    except ValueError:
        await cb.message.edit_text("Некорректная дата."); return
    await state.update_data(started_at=time.time(), shift_date=shift_date)
    await cb.message.edit_text(
        f"🕐 {_date_label(shift_date)} — выберите время начала смены:",
        reply_markup=kb_pick_time("ssc_shift_start"),
    )

@router.callback_query(F.data.startswith("ssc_shift_start:"))
async def cb_shift_pick_start(cb: CallbackQuery, state: FSMContext, config: StaffSchedulerConfig):
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    data = await state.get_data()
    if _flow_expired(data) or "shift_date" not in data:
        await state.clear()
        await cb.message.edit_text("⏳ Время ожидания истекло — начните заново кнопкой «➕ Добавить смену»."); return
    start_time = cb.data.split(":", 1)[1]
    await state.update_data(started_at=time.time(), start_time=start_time)
    await cb.message.edit_text(
        f"🕐 Начало {start_time} — выберите время окончания смены "
        f"(если смена ночная и заканчивается на следующий день, выберите время после полуночи):",
        reply_markup=kb_pick_time("ssc_shift_end"),
    )

@router.callback_query(F.data.startswith("ssc_shift_end:"))
async def cb_shift_pick_end(cb: CallbackQuery, state: FSMContext, config: StaffSchedulerConfig):
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    data = await state.get_data()
    if _flow_expired(data) or "start_time" not in data:
        await state.clear()
        await cb.message.edit_text("⏳ Время ожидания истекло — начните заново кнопкой «➕ Добавить смену»."); return
    end_time = cb.data.split(":", 1)[1]
    await state.update_data(started_at=time.time(), end_time=end_time)
    await state.set_state(ShiftFlow.note)
    await cb.message.edit_text(
        "📝 Заметка к смене (необязательно) — отправьте текст или нажмите «Без заметки»:",
        reply_markup=kb_note_step(),
    )

@router.message(ShiftFlow.note, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def shift_note_text(msg: Message, state: FSMContext, config: StaffSchedulerConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("⏳ Время ожидания истекло — начните заново кнопкой «➕ Добавить смену»."); return
    if not _is_bot_admin(msg.from_user.id, config):
        await state.clear(); return
    note = msg.text.strip()[:NOTE_MAX_LEN] or None
    await _shift_confirm_screen(msg, state, config, note)

@router.callback_query(ShiftFlow.note, F.data == "ssc_shift_skip_note")
async def shift_note_skip(cb: CallbackQuery, state: FSMContext, config: StaffSchedulerConfig):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("⏳ Время ожидания истекло — начните заново кнопкой «➕ Добавить смену»."); return
    if not _is_bot_admin(cb.from_user.id, config):
        await state.clear(); return
    await _shift_confirm_screen(cb.message, state, config, None, edit=True)

async def _shift_confirm_screen(msg: Message, state: FSMContext, config: StaffSchedulerConfig,
                                 note: str | None, edit: bool = False) -> None:
    data = await state.get_data()
    employee = await _employee_row(config.db_path, data["employee_id"])
    if employee is None:
        await state.clear()
        text = "Сотрудник не найден — начните заново."
        (await msg.edit_text(text)) if edit else (await msg.answer(text))
        return
    await state.update_data(note=note)
    shift_date, start, end = data["shift_date"], data["start_time"], data["end_time"]
    conflicts = await _find_conflicts(config.db_path, employee["id"], shift_date, start, end)
    lines = [
        "📌 <b>Новая смена</b>\n",
        f"🧑 {_esc(employee['name'])}",
        f"📅 {_date_label(shift_date)}",
        f"🕐 {start}–{end}" + (" (за полночь)" if _to_minutes(end) <= _to_minutes(start) else ""),
    ]
    if note:
        lines.append(f"📝 {_esc(note)}")
    if conflicts:
        lines.append("\n⚠️ <b>Пересекается с уже назначенной сменой этого сотрудника:</b>")
        for c in conflicts:
            lines.append(f"  • {_date_label(c['shift_date'])}, {c['start_time']}–{c['end_time']}")
    text = "\n".join(lines)
    kb = kb_shift_confirm(bool(conflicts))
    (await msg.edit_text(text, parse_mode="HTML", reply_markup=kb)) if edit \
        else (await msg.answer(text, parse_mode="HTML", reply_markup=kb))

@router.callback_query(F.data == "ssc_shift_confirm")
async def cb_shift_confirm(cb: CallbackQuery, state: FSMContext, config: StaffSchedulerConfig):
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    data = await state.get_data()
    if _flow_expired(data) or "employee_id" not in data or "shift_date" not in data:
        await state.clear()
        await cb.message.edit_text("⏳ Время ожидания истекло — начните заново кнопкой «➕ Добавить смену»."); return
    user_id = cb.from_user.id
    if user_id in _busy_shift_confirms:
        # Rapid double-tap guard — same rationale as debtors.py's
        # _busy_entry_saves: without this a fast double-tap on "Подтвердить"
        # fires this handler twice before the first INSERT's state.clear()
        # lands, silently creating two identical shifts.
        return
    _busy_shift_confirms.add(user_id)
    try:
        shift_id = await _insert_shift(
            config.db_path, data["employee_id"], data["shift_date"],
            data["start_time"], data["end_time"], data.get("note"),
        )
        await state.clear()
    finally:
        _busy_shift_confirms.discard(user_id)
    shift = await _shift_row_with_employee(config.db_path, shift_id)
    await cb.message.edit_text(
        f"✅ Смена добавлена.\n\n🧑 {_esc(shift['employee_name'])}\n"
        f"📅 {_date_label(shift['shift_date'])}\n🕐 {shift['start_time']}–{shift['end_time']}",
        parse_mode="HTML",
    )

@router.callback_query(F.data == "ssc_shift_cancel")
async def cb_shift_cancel(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await cb.message.edit_text("❌ Добавление смены отменено.")


# ── WEEK VIEW: BY DAY ──────────────────────────────────────────────────────────

async def _render_day_shifts(db_path: str, shift_date: str) -> tuple[str, InlineKeyboardMarkup]:
    shifts = await _shifts_for_day(db_path, shift_date)
    text = f"📅 <b>{_date_label(shift_date)}</b>\n\n" + (
        "Смен не назначено." if not shifts else "Нажмите на смену для просмотра/удаления:"
    )
    return text, kb_shift_list(shifts, back_callback="ssc_days")

@router.message(F.text == "📅 Расписание на неделю", F.chat.type == "private")
async def week_view_entry(msg: Message, state: FSMContext, config: StaffSchedulerConfig):
    if not _is_bot_admin(msg.from_user.id, config):
        await msg.answer(NO_ACCESS_TEXT); return
    await state.clear()
    window = _week_window(date.today())
    await msg.answer("📅 Текущая и следующая неделя. Выберите день:", reply_markup=kb_pick_day(window))

@router.callback_query(F.data == "ssc_days")
async def cb_days_back(cb: CallbackQuery, state: FSMContext, config: StaffSchedulerConfig):
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    await state.clear()
    window = _week_window(date.today())
    await cb.message.edit_text("📅 Текущая и следующая неделя. Выберите день:", reply_markup=kb_pick_day(window))

@router.callback_query(F.data.startswith("ssc_day:"))
async def cb_day_view(cb: CallbackQuery, state: FSMContext, config: StaffSchedulerConfig):
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    await state.clear()
    shift_date = cb.data.split(":", 1)[1]
    try:
        date.fromisoformat(shift_date)
    except ValueError:
        await cb.message.edit_text("Некорректная дата."); return
    await state.update_data(nav_kind="day", nav_value=shift_date)
    text, kb = await _render_day_shifts(config.db_path, shift_date)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


# ── VIEW BY EMPLOYEE ────────────────────────────────────────────────────────

async def _render_employee_shifts(db_path: str, employee_id: int) -> tuple[str, InlineKeyboardMarkup] | tuple[None, None]:
    employee = await _employee_row(db_path, employee_id)
    if employee is None:
        return None, None
    window = _week_window(date.today())
    shifts = await _shifts_for_employee_window(db_path, employee_id, window)
    label = employee["name"] if not employee["position"] else f"{employee['name']} ({employee['position']})"
    text = f"🧑 <b>{_esc(label)}</b>\n\n" + (
        "Смен на текущую и следующую неделю нет." if not shifts else "Нажмите на смену для просмотра/удаления:"
    )
    return text, kb_shift_list(shifts, back_callback="ssc_vemp_list")

@router.message(F.text == "🧑 Расписание по сотруднику", F.chat.type == "private")
async def employee_view_entry(msg: Message, state: FSMContext, config: StaffSchedulerConfig):
    if not _is_bot_admin(msg.from_user.id, config):
        await msg.answer(NO_ACCESS_TEXT); return
    await state.clear()
    employees = await _all_employees(config.db_path)
    await msg.answer("🧑 Выберите сотрудника:", reply_markup=kb_pick_employee_view(employees))

@router.callback_query(F.data == "ssc_vemp_list")
async def cb_vemp_list(cb: CallbackQuery, state: FSMContext, config: StaffSchedulerConfig):
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    await state.clear()
    employees = await _all_employees(config.db_path)
    await cb.message.edit_text("🧑 Выберите сотрудника:", reply_markup=kb_pick_employee_view(employees))

@router.callback_query(F.data.startswith("ssc_vemp_view:"))
async def cb_vemp_view(cb: CallbackQuery, state: FSMContext, config: StaffSchedulerConfig):
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    await state.clear()
    employee_id = _parse_id(cb.data.split(":", 1)[1])
    text, kb = (None, None) if employee_id is None else await _render_employee_shifts(config.db_path, employee_id)
    if text is None:
        await cb.message.edit_text("Сотрудник не найден.", reply_markup=kb_cancel("ssc_vemp_list")); return
    await state.update_data(nav_kind="emp", nav_value=employee_id)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


# ── SHIFT DETAIL / DELETE ──────────────────────────────────────────────────

async def _nav_back_callback(state: FSMContext) -> str:
    data = await state.get_data()
    if data.get("nav_kind") == "emp":
        return "ssc_vemp_list"
    return "ssc_days"

async def _render_nav(config: StaffSchedulerConfig, state: FSMContext) -> tuple[str, InlineKeyboardMarkup] | tuple[None, None]:
    data = await state.get_data()
    if data.get("nav_kind") == "day" and data.get("nav_value"):
        return await _render_day_shifts(config.db_path, data["nav_value"])
    if data.get("nav_kind") == "emp" and data.get("nav_value") is not None:
        return await _render_employee_shifts(config.db_path, data["nav_value"])
    window = _week_window(date.today())
    return "📅 Текущая и следующая неделя. Выберите день:", kb_pick_day(window)

@router.callback_query(F.data.startswith("ssc_shift_view:"))
async def cb_shift_view(cb: CallbackQuery, state: FSMContext, config: StaffSchedulerConfig):
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    shift_id = _parse_id(cb.data.split(":", 1)[1])
    shift = None if shift_id is None else await _shift_row_with_employee(config.db_path, shift_id)
    if shift is None:
        await cb.message.edit_text("Смена не найдена."); return
    label = shift["employee_name"] if not shift["employee_position"] else f"{shift['employee_name']} ({shift['employee_position']})"
    lines = [
        "📌 <b>Смена</b>\n",
        f"🧑 {_esc(label)}",
        f"📅 {_date_label(shift['shift_date'])}",
        f"🕐 {shift['start_time']}–{shift['end_time']}" +
        (" (за полночь)" if _to_minutes(shift["end_time"]) <= _to_minutes(shift["start_time"]) else ""),
    ]
    if shift["note"]:
        lines.append(f"📝 {_esc(shift['note'])}")
    back = await _nav_back_callback(state)
    await cb.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=kb_shift_detail(shift_id, back))

@router.callback_query(F.data.startswith("ssc_shift_del_ask:"))
async def cb_shift_del_ask(cb: CallbackQuery, config: StaffSchedulerConfig):
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    shift_id = _parse_id(cb.data.split(":", 1)[1])
    shift = None if shift_id is None else await _shift_row_with_employee(config.db_path, shift_id)
    if shift is None:
        await cb.message.edit_text("Смена не найдена."); return
    await cb.message.edit_text(
        f"⚠️ Удалить смену <b>{_esc(shift['employee_name'])}</b>, "
        f"{_date_label(shift['shift_date'])} {shift['start_time']}–{shift['end_time']}?",
        parse_mode="HTML", reply_markup=kb_shift_delete_confirm(shift_id),
    )

@router.callback_query(F.data.startswith("ssc_shift_del_confirm:"))
async def cb_shift_del_confirm(cb: CallbackQuery, state: FSMContext, config: StaffSchedulerConfig):
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    shift_id = _parse_id(cb.data.split(":", 1)[1])
    if shift_id is not None:
        await _delete_shift(config.db_path, shift_id)
    text, kb = await _render_nav(config, state)
    await cb.message.edit_text(f"🗑 Смена удалена.\n\n{text}", parse_mode="HTML", reply_markup=kb)


# ── EMPLOYEE MANAGEMENT ─────────────────────────────────────────────────────

@router.message(F.text == "👥 Сотрудники", F.chat.type == "private")
async def employees_mgmt_entry(msg: Message, state: FSMContext, config: StaffSchedulerConfig):
    if not _is_bot_admin(msg.from_user.id, config):
        await msg.answer(NO_ACCESS_TEXT); return
    await state.clear()
    employees = await _active_employees(config.db_path)
    await msg.answer("👥 <b>Сотрудники</b>", parse_mode="HTML", reply_markup=kb_employees_mgmt(employees))

@router.callback_query(F.data == "ssc_emgmt_list")
async def cb_emgmt_list(cb: CallbackQuery, state: FSMContext, config: StaffSchedulerConfig):
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    await state.clear()
    employees = await _active_employees(config.db_path)
    await cb.message.edit_text("👥 <b>Сотрудники</b>", parse_mode="HTML", reply_markup=kb_employees_mgmt(employees))

@router.callback_query(F.data == "ssc_emgmt_add")
async def cb_emgmt_add(cb: CallbackQuery, state: FSMContext, config: StaffSchedulerConfig):
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    await state.update_data(started_at=time.time())
    await state.set_state(EmployeeFlow.name)
    await cb.message.edit_text("✏️ Введите имя сотрудника:", reply_markup=kb_cancel("ssc_emgmt_list"))

@router.message(EmployeeFlow.name, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def emgmt_name(msg: Message, state: FSMContext, config: StaffSchedulerConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("⏳ Время ожидания истекло — начните заново кнопкой «👥 Сотрудники»."); return
    if not _is_bot_admin(msg.from_user.id, config):
        await state.clear(); return
    name = msg.text.strip()[:NAME_MAX_LEN]
    if not name:
        await msg.answer("Имя не может быть пустым."); return
    await state.update_data(started_at=time.time(), name=name)
    await state.set_state(EmployeeFlow.position)
    await msg.answer("💼 Должность (необязательно) — отправьте текст или нажмите «Без должности»:",
                     reply_markup=kb_position_step())

@router.message(EmployeeFlow.position, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def emgmt_position(msg: Message, state: FSMContext, config: StaffSchedulerConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("⏳ Время ожидания истекло — начните заново кнопкой «👥 Сотрудники»."); return
    if not _is_bot_admin(msg.from_user.id, config):
        await state.clear(); return
    position = msg.text.strip()[:POSITION_MAX_LEN] or None
    await state.update_data(started_at=time.time(), position=position)
    await state.set_state(EmployeeFlow.contact)
    await msg.answer("📞 Контакт сотрудника (телефон/@username):", reply_markup=kb_cancel("ssc_emgmt_list"))

@router.callback_query(EmployeeFlow.position, F.data == "ssc_emgmt_skip_position")
async def emgmt_skip_position(cb: CallbackQuery, state: FSMContext, config: StaffSchedulerConfig):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("⏳ Время ожидания истекло — начните заново кнопкой «👥 Сотрудники»."); return
    if not _is_bot_admin(cb.from_user.id, config):
        await state.clear(); return
    await state.update_data(started_at=time.time(), position=None)
    await state.set_state(EmployeeFlow.contact)
    await cb.message.edit_text("📞 Контакт сотрудника (телефон/@username):", reply_markup=kb_cancel("ssc_emgmt_list"))

@router.message(EmployeeFlow.contact, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def emgmt_contact(msg: Message, state: FSMContext, config: StaffSchedulerConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("⏳ Время ожидания истекло — начните заново кнопкой «👥 Сотрудники»."); return
    if not _is_bot_admin(msg.from_user.id, config):
        await state.clear(); return
    contact = msg.text.strip()[:CONTACT_MAX_LEN]
    if not contact:
        await msg.answer("Контакт не может быть пустым."); return
    await _insert_employee(config.db_path, data["name"], data.get("position"), contact)
    await state.clear()
    employees = await _active_employees(config.db_path)
    await msg.answer(
        f"✅ Сотрудник <b>{_esc(data['name'])}</b> добавлен.",
        parse_mode="HTML",
    )
    await msg.answer("👥 <b>Сотрудники</b>", parse_mode="HTML", reply_markup=kb_employees_mgmt(employees))

@router.callback_query(F.data.startswith("ssc_emgmt_rm_ask:"))
async def cb_emgmt_rm_ask(cb: CallbackQuery, config: StaffSchedulerConfig):
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    employee_id = _parse_id(cb.data.split(":", 1)[1])
    employee = None if employee_id is None else await _employee_row(config.db_path, employee_id)
    if employee is None:
        await cb.message.edit_text("Сотрудник не найден."); return
    await cb.message.edit_text(
        f"⚠️ Удалить сотрудника <b>{_esc(employee['name'])}</b>? "
        f"Он перестанет предлагаться при добавлении новых смен, но его прошлые "
        f"смены останутся в расписании.",
        parse_mode="HTML", reply_markup=kb_employee_delete_confirm(employee_id),
    )

@router.callback_query(F.data.startswith("ssc_emgmt_rm_confirm:"))
async def cb_emgmt_rm_confirm(cb: CallbackQuery, config: StaffSchedulerConfig):
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    employee_id = _parse_id(cb.data.split(":", 1)[1])
    if employee_id is not None:
        await _deactivate_employee(config.db_path, employee_id)
    employees = await _active_employees(config.db_path)
    await cb.message.edit_text("🗑 Сотрудник удалён.\n\n👥 <b>Сотрудники</b>", parse_mode="HTML",
                               reply_markup=kb_employees_mgmt(employees))


# ── ADMIN: bot-admins panel (same pattern as debtors.py/moderator.py) ────────

async def _admins_list_text(config: StaffSchedulerConfig) -> str:
    ids = sorted(_load_admins(config.admins_file))
    if not ids:
        return "👥 Пусто"
    return "👥 <b>Администраторы бота:</b>\n\n" + "\n".join(f"• <code>{_esc(i)}</code>" for i in ids)

@router.message(F.text == "⚙️ Админы бота", F.chat.type == "private")
async def admins_panel_entry(msg: Message, state: FSMContext, config: StaffSchedulerConfig):
    if not _is_bot_admin(msg.from_user.id, config):
        await msg.answer(NO_ACCESS_TEXT); return
    await state.clear()
    await msg.answer(await _admins_list_text(config), parse_mode="HTML", reply_markup=kb_admins_panel())

@router.callback_query(F.data == "ssc_adm_admins")
async def cb_adm_admins(cb: CallbackQuery, state: FSMContext, config: StaffSchedulerConfig):
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    await state.clear()
    await cb.message.edit_text(await _admins_list_text(config), parse_mode="HTML", reply_markup=kb_admins_panel())

@router.callback_query(F.data == "ssc_adm_addadmin")
async def cb_adm_addadmin(cb: CallbackQuery, state: FSMContext, config: StaffSchedulerConfig):
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    await state.update_data(started_at=time.time())
    await state.set_state(AdminFlow.add_admin)
    await cb.message.edit_text(
        "Пришлите Telegram ID пользователя, которого нужно сделать администратором:",
        reply_markup=kb_cancel("ssc_adm_admins"),
    )

@router.message(AdminFlow.add_admin, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def adm_add_admin(msg: Message, state: FSMContext, config: StaffSchedulerConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("⏳ Время ожидания истекло — начните заново кнопкой «⚙️ Админы бота»."); return
    if not _is_bot_admin(msg.from_user.id, config):
        await state.clear(); return
    text = msg.text.strip()
    if not _valid_telegram_id(text):
        await msg.answer("Пришлите числовой Telegram ID."); return
    ids = _load_admins(config.admins_file); ids.add(text); _save_admins(config.admins_file, ids)
    if config.bot_id is not None:
        try:
            await add_bot_admin(config.bot_id, text)
        except Exception as e:
            logger.warning(f"adm_add_admin: add_bot_admin sync failed for bot {config.bot_id}: {e}")
    logger.info(f"adm_add_admin: {text} added as bot admin by {msg.from_user.id}")
    await state.clear()
    await msg.answer(f"✅ <code>{_esc(text)}</code> добавлен.", parse_mode="HTML")
    await msg.answer(await _admins_list_text(config), parse_mode="HTML", reply_markup=kb_admins_panel())

@router.callback_query(F.data == "ssc_adm_removeadmin")
async def cb_adm_removeadmin(cb: CallbackQuery, state: FSMContext, config: StaffSchedulerConfig):
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    ids = sorted(_load_admins(config.admins_file))
    if len(ids) <= 1:
        await cb.message.edit_text(
            "⚠️ Нельзя удалить единственного администратора.\n\n" + await _admins_list_text(config),
            parse_mode="HTML", reply_markup=kb_admins_panel(),
        )
        return
    if len(ids) <= MAX_ADMIN_REMOVE_BUTTONS:
        await state.update_data(remove_admin_ids=ids)
        await cb.message.edit_text("Выберите администратора для удаления:", reply_markup=kb_remove_admins(ids))
    else:
        await state.update_data(started_at=time.time())
        await state.set_state(AdminFlow.remove_admin_pick)
        await cb.message.edit_text("Пришлите Telegram ID администратора для удаления:", reply_markup=kb_cancel("ssc_adm_admins"))

@router.callback_query(F.data.startswith("ssc_adm_rma:"))
async def cb_adm_rma(cb: CallbackQuery, state: FSMContext, config: StaffSchedulerConfig):
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    data = await state.get_data()
    ids = data.get("remove_admin_ids", [])
    idx = _parse_id(cb.data.split(":", 1)[1])
    if idx is None or idx < 0 or idx >= len(ids):
        await cb.message.edit_text(await _admins_list_text(config), parse_mode="HTML", reply_markup=kb_admins_panel()); return
    target = ids[idx]
    current = _load_admins(config.admins_file)
    if target in current and len(current) <= 1:
        await cb.message.edit_text(
            "⚠️ Нельзя удалить единственного администратора.\n\n" + await _admins_list_text(config),
            parse_mode="HTML", reply_markup=kb_admins_panel(),
        )
        return
    current.discard(target); _save_admins(config.admins_file, current)
    if config.bot_id is not None:
        try:
            await remove_bot_admin(config.bot_id, target)
        except Exception as e:
            logger.warning(f"cb_adm_rma: remove_bot_admin sync failed for bot {config.bot_id}: {e}")
    logger.info(f"cb_adm_rma: {target} removed as bot admin by {cb.from_user.id}")
    await state.clear()
    await cb.message.edit_text(await _admins_list_text(config), parse_mode="HTML", reply_markup=kb_admins_panel())

@router.message(AdminFlow.remove_admin_pick, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def adm_remove_admin_text(msg: Message, state: FSMContext, config: StaffSchedulerConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("⏳ Время ожидания истекло — начните заново."); return
    if not _is_bot_admin(msg.from_user.id, config):
        await state.clear(); return
    text = msg.text.strip()
    if not _valid_telegram_id(text):
        await msg.answer("Пришлите числовой Telegram ID."); return
    ids = _load_admins(config.admins_file)
    if text in ids and len(ids) <= 1:
        await msg.answer("⚠️ Нельзя удалить единственного администратора."); return
    ids.discard(text); _save_admins(config.admins_file, ids)
    if config.bot_id is not None:
        try:
            await remove_bot_admin(config.bot_id, text)
        except Exception as e:
            logger.warning(f"adm_remove_admin_text: remove_bot_admin sync failed for bot {config.bot_id}: {e}")
    logger.info(f"adm_remove_admin_text: {text} removed as bot admin by {msg.from_user.id}")
    await state.clear()
    await msg.answer(await _admins_list_text(config), parse_mode="HTML", reply_markup=kb_admins_panel())


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
