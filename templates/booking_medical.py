# TEMPLATE: booking_medical
# USE FOR: медицинские клиники, стоматологии, приём врача — запись к специалисту по специализации, анкета жалоб/аллергий, полис
# CUSTOMIZE: sections marked with # CUSTOMIZE
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
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
# Same status as booking_beauty.py's CUSTOMIZE block: per-file source-text
# customization Claude edits when generating a specific bot, not per-bot
# runtime state. DOCTORS is only the INITIAL seed (see _seed_doctors below) —
# admins can add/remove doctors afterward via /adddoctor, /removedoctor.
BOT_DESCRIPTION = "Онлайн-запись на приём к врачу. Выберите специализацию, врача и удобное время."
WELCOME_TEXT = (
    "🏥 <b>Запись на приём</b>\n\n"
    "Выберите специализацию и врача — запишем вас на удобное время.\n\n"
    "Нажмите <b>📅 Записаться на приём</b>:"
)
OWNER_NOTIFY_TEXT = "🔔 <b>Новая запись на приём!</b>\n"
DOCTORS = [
    ("Иванова Анна Сергеевна", "Терапевт"),
    ("Петров Игорь Викторович", "Стоматолог"),
    ("Сидорова Ольга Дмитриевна", "Педиатр"),
]
DAYS_AHEAD = 14
SLOT_TIMES = ["09:00", "10:00", "11:00", "12:00", "14:00", "15:00", "16:00", "17:00"]
SLOT_DURATION_MIN = 30
# ── END CUSTOMIZE ─────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
router = Router()

DAYS_RU = {0: "Пн", 1: "Вт", 2: "Ср", 3: "Чт", 4: "Пт", 5: "Сб", 6: "Вс"}
MONTHS_RU = {1: "янв", 2: "фев", 3: "мар", 4: "апр", 5: "май", 6: "июн",
             7: "июл", 8: "авг", 9: "сен", 10: "окт", 11: "ноя", 12: "дек"}


def _date_label(slot_date: str) -> str:
    d = datetime.strptime(slot_date, "%Y-%m-%d")
    return f"{DAYS_RU[d.weekday()]} {d.day} {MONTHS_RU[d.month]}"


def _esc(value, max_len: int = 500) -> str:
    """HTML-escapes AND length-bounds any user-supplied text before it goes into
    a parse_mode="HTML" message. Review-found blocker: complaint/allergies/
    policy_number/full_name are all free-text patient input — an unescaped
    '<'/'&' (a very plausible medical complaint like "температура <38") or a
    message over Telegram's ~4096-char limit would raise inside message
    construction with no recovery path. Every place that inserts one of these
    fields into an HTML message must go through this."""
    text = str(value) if value is not None else ""
    if len(text) > max_len:
        text = text[:max_len] + "…"
    return html.escape(text)


# ── config ───────────────────────────────────────────────────────────────────
# Same pattern as templates/booking_beauty.py — see docs/STAGE2_DESIGN.md
# "Config-контракт". No excel/html export in this template.

@dataclass
class BookingMedicalConfig:
    bot_name: str
    db_path: str
    admins_file: Path
    welcome_image: Path
    display_name: str | None = None
    group_chat_id: str | None = None


def _paths_for(name: str, data_dir: Path) -> BookingMedicalConfig:
    return BookingMedicalConfig(
        bot_name=name,
        db_path=str(data_dir / f"{name}_data.db"),
        admins_file=data_dir / f"admins_{name}.json",
        welcome_image=data_dir / "bot_images" / f"{name}.jpg",
    )


def config_from_env() -> BookingMedicalConfig:
    """Standalone/subprocess mode."""
    name = Path(__file__).stem
    data_dir = Path(os.getenv("DATA_DIR", "./data"))
    data_dir.mkdir(exist_ok=True)
    return _paths_for(name, data_dir)


def config_from_bot_row(bot_row: dict, data_dir: Path) -> BookingMedicalConfig:
    """Webhook runtime mode. Paths built from bot_row["bot_id"] (bots.id, the
    physically unique AUTOINCREMENT PK) — NOT bot_row["name"] — same reasoning
    as every other template's config_from_bot_row (see docs/STAGE2_DESIGN.md
    "Изоляция по bots.id")."""
    bot_id = bot_row["bot_id"]
    config = BookingMedicalConfig(
        bot_name=bot_row["name"],
        db_path=str(data_dir / f"bot_{bot_id}_data.db"),
        admins_file=data_dir / f"admins_{bot_id}.json",
        welcome_image=data_dir / "bot_images" / f"bot_{bot_id}.jpg",
    )
    config.display_name = bot_row.get("display_name")
    config.group_chat_id = bot_row.get("group_chat_id")
    return config


class ConfigMiddleware(BaseMiddleware):
    """Injects this bot's BookingMedicalConfig into data["config"]."""

    def __init__(self, config: BookingMedicalConfig) -> None:
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


# ── phone validation (same as booking_beauty.py) ─────────────────────────────

def _normalize_phone(raw: str) -> str | None:
    digits = re.sub(r"[^\d]", "", raw)
    if len(digits) == 11 and digits[0] in ("7", "8"):
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    else:
        return None
    return f"+{digits[0]} ({digits[1:4]}) {digits[4:7]}-{digits[7:9]}-{digits[9:11]}"


# ── mini-app config (see docs/MINIAPP_DESIGN.md) ────────────────────────────
# Sensitive appointment fields (complaint/allergies/policy_number) are shown
# only in appointments' detail view, not list — mirrors the bot's own privacy
# boundary (🗂 Карта пациента is a deliberate, explicit action, never
# broadcast) — the mini-app is owner-only, same audience as that admin action.
miniapp_config = {
    "resources": [
        {
            "name": "doctors",
            "table": "doctors",
            "order_by": "id DESC",
            "creatable": True,
            "title": "Врачи",
            "titleField": "name",
            "fields": [
                {"name": "name", "required": True, "label": "Имя", "kind": "text", "list": True, "detail": True, "create": True},
                {"name": "specialization", "required": True, "label": "Специализация", "kind": "text", "list": True, "detail": True, "create": True},
                {"name": "active", "label": "Активен", "kind": "status", "list": True, "detail": True, "create": False},
            ],
        },
        {
            "name": "slots",
            "table": "slots",
            "order_by": "slot_date DESC, slot_time DESC",
            "creatable": False,
            "title": "Слоты",
            "titleField": "slot_time",
            "fields": [
                {"name": "doctor_id", "label": "ID врача", "kind": "number", "list": False, "detail": True, "create": False},
                {"name": "slot_date", "label": "Дата", "kind": "date", "list": True, "detail": True, "create": False},
                {"name": "slot_time", "label": "Время", "kind": "text", "list": True, "detail": True, "create": False},
                {"name": "duration_min", "label": "Длительность (мин)", "kind": "number", "list": False, "detail": True, "create": False},
                {"name": "status", "label": "Статус", "kind": "status", "list": True, "detail": True, "create": False},
            ],
        },
        {
            "name": "patients",
            "table": "patients",
            "order_by": "created_at DESC",
            "creatable": False,
            "title": "Пациенты",
            "titleField": "full_name",
            "fields": [
                {"name": "user_id", "label": "ID пользователя", "kind": "number", "list": False, "detail": True, "create": False},
                {"name": "full_name", "label": "ФИО", "kind": "text", "list": True, "detail": True, "create": False},
                {"name": "phone", "label": "Телефон", "kind": "text", "list": True, "detail": True, "create": False},
                {"name": "policy_number", "label": "Полис", "kind": "text", "list": False, "detail": True, "create": False},
                {"name": "created_at", "label": "Создан", "kind": "date", "list": False, "detail": True, "create": False},
            ],
        },
        {
            "name": "appointments",
            "table": "appointments",
            "order_by": "created_at DESC",
            "creatable": False,
            "title": "Записи на приём",
            "titleField": "status",
            "fields": [
                {"name": "slot_id", "label": "ID слота", "kind": "number", "list": False, "detail": True, "create": False},
                {"name": "patient_id", "label": "ID пациента", "kind": "number", "list": False, "detail": True, "create": False},
                {"name": "complaint", "label": "Жалоба", "kind": "text", "list": False, "detail": True, "create": False},
                {"name": "allergies", "label": "Аллергии", "kind": "text", "list": False, "detail": True, "create": False},
                {"name": "policy_number", "label": "Полис", "kind": "text", "list": False, "detail": True, "create": False},
                {"name": "is_repeat_visit", "label": "Повторный визит", "kind": "status", "list": True, "detail": True, "create": False},
                {"name": "status", "label": "Статус", "kind": "status", "list": True, "detail": True, "create": False},
                {"name": "created_at", "label": "Создано", "kind": "date", "list": True, "detail": True, "create": False},
            ],
        },
    ],
}


# ── db ────────────────────────────────────────────────────────────────────────

async def init_db(db_path: str):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS doctors (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                name           TEXT NOT NULL,
                specialization TEXT NOT NULL,
                active         INTEGER DEFAULT 1
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS slots (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                doctor_id    INTEGER NOT NULL REFERENCES doctors(id),
                slot_date    TEXT NOT NULL,
                slot_time    TEXT NOT NULL,
                duration_min INTEGER DEFAULT 30,
                status       TEXT DEFAULT 'active'
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS patients (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id       INTEGER UNIQUE,
                full_name     TEXT,
                phone         TEXT,
                policy_number TEXT,
                created_at    TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS appointments (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                slot_id         INTEGER UNIQUE REFERENCES slots(id),
                patient_id      INTEGER NOT NULL REFERENCES patients(id),
                complaint       TEXT,
                allergies       TEXT,
                policy_number   TEXT,
                is_repeat_visit INTEGER DEFAULT 0,
                status          TEXT DEFAULT 'active',
                created_at      TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.commit()
    await _seed_doctors(db_path)
    await _ensure_slots(db_path)


async def _seed_doctors(db_path: str):
    # Idempotent by design: only seeds if the table is completely empty (same
    # COUNT==0 guard convention as manager_secretary.py's _seed_faqs) — never
    # re-inserts or touches doctors added/removed afterward via admin commands.
    async with aiosqlite.connect(db_path) as db:
        count = (await (await db.execute("SELECT COUNT(*) FROM doctors")).fetchone())[0]
        if count == 0:
            for name, spec in DOCTORS:
                await db.execute("INSERT INTO doctors (name, specialization) VALUES (?,?)", (name, spec))
            await db.commit()


async def _ensure_slots(db_path: str):
    # Idempotent per (doctor_id, slot_date) — same coarse per-date guard
    # pattern as booking_beauty.py's _ensure_slots: if ANY slot already exists
    # for that doctor+date, the whole day is skipped, existing rows (including
    # booked ones) are never touched. Slots are per-doctor, not a shared pool —
    # the key domain difference from booking_beauty. Also called right after
    # /adddoctor so a newly added doctor gets slots immediately, not only on
    # the next init_db().
    today = datetime.now().date()
    async with aiosqlite.connect(db_path) as db:
        doctor_ids = [r[0] for r in await (await db.execute(
            "SELECT id FROM doctors WHERE active=1"
        )).fetchall()]
        for doctor_id in doctor_ids:
            for i in range(DAYS_AHEAD):
                d = (today + timedelta(days=i)).isoformat()
                existing = (await (await db.execute(
                    "SELECT COUNT(*) FROM slots WHERE doctor_id=? AND slot_date=?", (doctor_id, d)
                )).fetchone())[0]
                if existing == 0:
                    for t in SLOT_TIMES:
                        await db.execute(
                            "INSERT INTO slots (doctor_id, slot_date, slot_time, duration_min) VALUES (?,?,?,?)",
                            (doctor_id, d, t, SLOT_DURATION_MIN)
                        )
        await db.commit()


async def _get_or_create_patient(db, user_id: int, full_name: str, phone: str, policy_number: str) -> int:
    row = await (await db.execute("SELECT id FROM patients WHERE user_id=?", (user_id,))).fetchone()
    if row:
        patient_id = row[0]
        await db.execute(
            "UPDATE patients SET full_name=?, phone=?, policy_number=? WHERE id=?",
            (full_name, phone, policy_number, patient_id)
        )
        return patient_id
    cur = await db.execute(
        "INSERT INTO patients (user_id, full_name, phone, policy_number) VALUES (?,?,?,?)",
        (user_id, full_name, phone, policy_number)
    )
    return cur.lastrowid


# ── keyboards ─────────────────────────────────────────────────────────────────

def kb_main() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📅 Записаться на приём")],
        [KeyboardButton(text="📋 Мои записи"), KeyboardButton(text="❌ Отменить запись")],
    ], resize_keyboard=True)

def kb_admin() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📅 Записаться на приём")],
        [KeyboardButton(text="📋 Мои записи"), KeyboardButton(text="❌ Отменить запись")],
        [KeyboardButton(text="🗂 Все записи"), KeyboardButton(text="📊 Статистика")],
    ], resize_keyboard=True)

async def kb_specializations(db_path: str) -> InlineKeyboardMarkup:
    async with aiosqlite.connect(db_path) as db:
        rows = await (await db.execute(
            "SELECT DISTINCT specialization FROM doctors WHERE active=1 ORDER BY specialization"
        )).fetchall()
    rows_kb = [[InlineKeyboardButton(text=f"🏥 {r[0]}", callback_data=f"med_spec:{r[0]}")] for r in rows]
    rows_kb.append([InlineKeyboardButton(text="❌ Отмена", callback_data="med_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows_kb)

async def kb_doctors(db_path: str, specialization: str) -> InlineKeyboardMarkup:
    async with aiosqlite.connect(db_path) as db:
        rows = await (await db.execute(
            "SELECT id, name FROM doctors WHERE specialization=? AND active=1 ORDER BY name",
            (specialization,)
        )).fetchall()
    rows_kb = [[InlineKeyboardButton(text=f"👨‍⚕️ {r[1]}", callback_data=f"med_doctor:{r[0]}")] for r in rows]
    rows_kb.append([InlineKeyboardButton(text="◀️ Назад", callback_data="med_to_spec")])
    return InlineKeyboardMarkup(inline_keyboard=rows_kb)

def kb_days(doctor_id: int) -> InlineKeyboardMarkup:
    today = datetime.now().date()
    rows = []
    for i in range(DAYS_AHEAD):
        d = today + timedelta(days=i)
        rows.append([InlineKeyboardButton(
            text=_date_label(d.isoformat()), callback_data=f"med_day:{doctor_id}:{d.isoformat()}"
        )])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="med_to_doctors")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def kb_times(db_path: str, doctor_id: int, slot_date: str) -> InlineKeyboardMarkup:
    async with aiosqlite.connect(db_path) as db:
        rows = await (await db.execute(
            "SELECT slot_time FROM slots WHERE doctor_id=? AND slot_date=? AND status='active' ORDER BY slot_time",
            (doctor_id, slot_date)
        )).fetchall()
    btns = [[InlineKeyboardButton(text=f"⏰ {r[0]}", callback_data=f"med_time:{doctor_id}:{slot_date}:{r[0]}")] for r in rows]
    if not btns:
        btns.append([InlineKeyboardButton(text="❌ Нет свободных слотов", callback_data="med_noslots")])
    btns.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"med_to_days:{doctor_id}")])
    return InlineKeyboardMarkup(inline_keyboard=btns)

def kb_allergies_skip() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🚫 Нет аллергий", callback_data="med_allergy_none")
    ]])

def kb_policy_choice(existing_policy: str) -> InlineKeyboardMarkup:
    # Button text (unlike message text) isn't HTML and has its own length
    # limit — bound defensively since policy_number is free-text user input.
    short_policy = existing_policy if len(existing_policy) <= 30 else existing_policy[:27] + "…"
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"✅ Использовать {short_policy}", callback_data="med_policy_use"),
        InlineKeyboardButton(text="✏️ Ввести другой", callback_data="med_policy_new"),
    ]])

def kb_confirm() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подтвердить", callback_data="med_confirm"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="med_cancel"),
    ]])


# ── FSM ───────────────────────────────────────────────────────────────────────

class BookFlow(StatesGroup):
    specialization = State(); doctor = State(); day = State(); time = State()
    phone = State(); complaint = State(); allergies = State(); policy = State(); confirm = State()


# ── /start ────────────────────────────────────────────────────────────────────

@router.message(Command("start"))
async def cmd_start(message: Message, config: BookingMedicalConfig):
    admins = _load_admins(config.admins_file)
    first_time_admin = not admins
    if first_time_admin:
        _save_admins(config.admins_file, {str(message.from_user.id)})
    is_admin = str(message.from_user.id) in _load_admins(config.admins_file)
    kb = kb_admin() if is_admin else kb_main()
    if config.welcome_image.exists():
        await message.answer_photo(FSInputFile(str(config.welcome_image)),
                                   caption=WELCOME_TEXT, parse_mode="HTML", reply_markup=kb)
    else:
        await message.answer(WELCOME_TEXT, parse_mode="HTML", reply_markup=kb)
    if first_time_admin:
        await message.answer(
            "👑 <b>Вы — администратор этого бота.</b>\n\n"
            "Вам доступны дополнительные кнопки:\n"
            "🗂 <b>Все записи</b> — расписание по всем врачам\n"
            "📊 <b>Статистика</b> — сводка по записям\n\n"
            "Управление врачами:\n"
            "<code>/adddoctor Имя Фамилия | Специализация</code>\n"
            "<code>/removedoctor ID</code> — деактивировать\n"
            "<code>/doctors</code> — список\n\n"
            "Управление администраторами:\n"
            "<code>/addadmin ID</code> — добавить администратора\n"
            "<code>/removeadmin ID</code> — убрать администратора\n"
            "<code>/admins</code> — список администраторов",
            parse_mode="HTML"
        )


# ── /cancel ───────────────────────────────────────────────────────────────────
# Review-found gap: no way out of the multi-step intake questionnaire once
# started (a photo/voice/sticker mid-flow just gets ignored, no handler
# matches). /cancel works from ANY state, same pattern as handlers/create_bot.py's
# cmd_cancel.

@router.message(Command("cancel"), StateFilter("*"))
async def cmd_cancel(message: Message, state: FSMContext):
    if await state.get_state() is None:
        await message.answer("Нечего отменять.")
        return
    await state.clear()
    await message.answer("Отменено. Начните заново: 📅 Записаться на приём")


# ── BOOK FLOW: specialization → doctor → day → time → phone → complaint → allergies → policy → confirm ─

@router.message(F.text == "📅 Записаться на приём")
async def book_start(msg: Message, state: FSMContext, config: BookingMedicalConfig):
    await state.set_state(BookFlow.specialization)
    await msg.answer("🏥 Выберите специализацию врача:", reply_markup=await kb_specializations(config.db_path))

@router.callback_query(F.data == "med_to_spec")
async def cb_to_spec(cb: CallbackQuery, state: FSMContext, config: BookingMedicalConfig):
    await cb.answer()
    await state.set_state(BookFlow.specialization)
    await cb.message.edit_text("🏥 Выберите специализацию врача:", reply_markup=await kb_specializations(config.db_path))

@router.callback_query(F.data.startswith("med_spec:"))
async def cb_specialization(cb: CallbackQuery, state: FSMContext, config: BookingMedicalConfig):
    await cb.answer()
    specialization = cb.data.split(":", 1)[1]
    await state.update_data(specialization=specialization)
    await state.set_state(BookFlow.doctor)
    await cb.message.edit_text(
        f"✅ Специализация: {specialization}\n\n👨‍⚕️ Выберите врача:",
        reply_markup=await kb_doctors(config.db_path, specialization)
    )

@router.callback_query(F.data == "med_to_doctors")
async def cb_to_doctors(cb: CallbackQuery, state: FSMContext, config: BookingMedicalConfig):
    await cb.answer()
    data = await state.get_data()
    specialization = data.get("specialization", "")
    await state.set_state(BookFlow.doctor)
    await cb.message.edit_text(
        f"✅ Специализация: {specialization}\n\n👨‍⚕️ Выберите врача:",
        reply_markup=await kb_doctors(config.db_path, specialization)
    )

@router.callback_query(F.data.startswith("med_doctor:"))
async def cb_doctor(cb: CallbackQuery, state: FSMContext, config: BookingMedicalConfig):
    await cb.answer()
    doctor_id = int(cb.data.split(":", 1)[1])
    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute(
            "SELECT name FROM doctors WHERE id=? AND active=1", (doctor_id,)
        )).fetchone()
    if not row:
        # Narrow race, review-found: an admin can deactivate a doctor while a
        # patient still has an outdated keyboard open — treat as unavailable
        # rather than silently proceeding to book against a deactivated doctor.
        await cb.answer("Этот врач больше не принимает записи. Выберите другого.", show_alert=True)
        return
    doctor_name = row[0]
    await state.update_data(doctor_id=doctor_id, doctor_name=doctor_name)
    await state.set_state(BookFlow.day)
    data = await state.get_data()
    await cb.message.edit_text(
        f"✅ Специализация: {data.get('specialization', '')}\n👨‍⚕️ Врач: {doctor_name}\n\n📅 Выберите день:",
        reply_markup=kb_days(doctor_id)
    )

@router.callback_query(F.data.startswith("med_to_days:"))
async def cb_to_days(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    doctor_id = int(cb.data.split(":", 1)[1])
    await state.set_state(BookFlow.day)
    data = await state.get_data()
    await cb.message.edit_text(
        f"✅ Специализация: {data.get('specialization', '')}\n👨‍⚕️ Врач: {data.get('doctor_name', '')}\n\n📅 Выберите день:",
        reply_markup=kb_days(doctor_id)
    )

@router.callback_query(F.data.startswith("med_day:"))
async def cb_day(cb: CallbackQuery, state: FSMContext, config: BookingMedicalConfig):
    await cb.answer()
    _, doctor_id_s, slot_date = cb.data.split(":", 2)
    doctor_id = int(doctor_id_s)
    await state.update_data(slot_date=slot_date)
    await state.set_state(BookFlow.time)
    data = await state.get_data()
    await cb.message.edit_text(
        f"✅ Врач: {data.get('doctor_name', '')}\n📅 {_date_label(slot_date)}\n\n⏰ Выберите время:",
        reply_markup=await kb_times(config.db_path, doctor_id, slot_date)
    )

@router.callback_query(F.data.startswith("med_noslots"))
async def cb_noslots(cb: CallbackQuery):
    await cb.answer("На этот день нет свободных слотов. Выберите другой день.", show_alert=True)

@router.callback_query(F.data.startswith("med_time:"))
async def cb_time(cb: CallbackQuery, state: FSMContext, config: BookingMedicalConfig):
    await cb.answer()
    _, doctor_id_s, slot_date, slot_time = cb.data.split(":", 3)
    doctor_id = int(doctor_id_s)
    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute(
            "SELECT id, status FROM slots WHERE doctor_id=? AND slot_date=? AND slot_time=?",
            (doctor_id, slot_date, slot_time)
        )).fetchone()
    if not row or row[1] != "active":
        await cb.answer("Это время уже занято, выберите другое.", show_alert=True); return
    slot_id = row[0]
    await state.update_data(slot_id=slot_id, slot_time=slot_time)
    await state.set_state(BookFlow.phone)
    data = await state.get_data()
    await cb.message.edit_text(
        f"✅ Врач: {data.get('doctor_name', '')}\n📅 {_date_label(slot_date)} в {slot_time}\n\n"
        f"📱 Введите номер телефона (например: +7 999 123-45-67 или 89991234567):"
    )

@router.message(BookFlow.phone, F.text, ~F.text.startswith("/"))
async def book_phone(msg: Message, state: FSMContext):
    # Review-found blocker: this template originally never collected a
    # phone number at all (kb_doctors/adm_day/adm_card all display one, but
    # nothing ever populated it) — admins had no way to actually reach a
    # patient. _normalize_phone is the same validator booking_beauty.py uses.
    phone = _normalize_phone(msg.text.strip())
    if phone is None:
        await msg.answer(
            "❌ Неверный номер телефона.\n"
            "Введите российский номер, например: <b>+7 999 123-45-67</b> или <b>89991234567</b>",
            parse_mode="HTML"
        )
        return
    await state.update_data(client_phone=phone)
    await state.set_state(BookFlow.complaint)
    await msg.answer("💬 Опишите жалобу / причину визита:")

@router.message(BookFlow.complaint, F.text, ~F.text.startswith("/"))
async def book_complaint(msg: Message, state: FSMContext):
    await state.update_data(complaint=msg.text.strip())
    await state.set_state(BookFlow.allergies)
    await msg.answer(
        "⚠️ Есть ли у вас аллергии на лекарства? Опишите или нажмите «Нет аллергий»:",
        reply_markup=kb_allergies_skip()
    )

@router.callback_query(F.data == "med_allergy_none")
async def cb_allergy_none(cb: CallbackQuery, state: FSMContext, config: BookingMedicalConfig):
    await cb.answer()
    await state.update_data(allergies="")
    await _ask_policy(cb.message, state, config, cb.from_user.id)

@router.message(BookFlow.allergies, F.text, ~F.text.startswith("/"))
async def book_allergies(msg: Message, state: FSMContext, config: BookingMedicalConfig):
    await state.update_data(allergies=msg.text.strip())
    await _ask_policy(msg, state, config, msg.from_user.id)


async def _ask_policy(msg: Message, state: FSMContext, config: BookingMedicalConfig, user_id: int) -> None:
    """If this user_id already has a patients row with a policy_number on file
    (a returning patient), offer to reuse it instead of retyping — otherwise
    ask as free text. This is the autofill half of the "повторный визит"
    requirement; is_repeat_visit itself is computed separately at confirm
    time from appointment history, not from this lookup."""
    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute(
            "SELECT policy_number FROM patients WHERE user_id=?", (user_id,)
        )).fetchone()
    existing_policy = row[0] if row and row[0] else None
    await state.set_state(BookFlow.policy)
    if existing_policy:
        await state.update_data(existing_policy=existing_policy)
        await msg.answer(
            f"📄 У вас есть сохранённый полис: <b>{_esc(existing_policy)}</b>",
            parse_mode="HTML", reply_markup=kb_policy_choice(existing_policy)
        )
    else:
        await msg.answer("📄 Введите номер страхового полиса:")


@router.callback_query(F.data == "med_policy_use")
async def cb_policy_use(cb: CallbackQuery, state: FSMContext, config: BookingMedicalConfig):
    await cb.answer()
    data = await state.get_data()
    await state.update_data(policy_number=data.get("existing_policy", ""))
    await _show_confirmation(cb.message, state, config)

@router.callback_query(F.data == "med_policy_new")
async def cb_policy_new(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await cb.message.edit_text("📄 Введите номер страхового полиса:")

@router.message(BookFlow.policy, F.text, ~F.text.startswith("/"))
async def book_policy(msg: Message, state: FSMContext, config: BookingMedicalConfig):
    await state.update_data(policy_number=msg.text.strip())
    await _show_confirmation(msg, state, config)


async def _show_confirmation(msg: Message, state: FSMContext, config: BookingMedicalConfig):
    data = await state.get_data()
    text = (
        f"📋 <b>Подтвердите запись:</b>\n\n"
        f"🏥 {_esc(data.get('specialization', ''))}\n"
        f"👨‍⚕️ Врач: {_esc(data.get('doctor_name', ''))}\n"
        f"📅 {_date_label(data['slot_date'])} в {_esc(data.get('slot_time', ''))}\n"
        f"💬 Жалоба: {_esc(data.get('complaint', ''))}\n"
        f"⚠️ Аллергии: {_esc(data.get('allergies')) or 'нет'}\n"
        f"📄 Полис: {_esc(data.get('policy_number', ''))}"
    )
    # Review-found blocker: send BEFORE advancing state, inside try/except —
    # a malformed field could only get here already escaped/bounded by _esc()
    # above, but this stays defensive against any other send failure (e.g. a
    # transient network error) instead of leaving the user stuck in a state
    # with no rendered screen and no way forward.
    try:
        await msg.answer(text, parse_mode="HTML", reply_markup=kb_confirm())
    except Exception as e:
        logger.error(f"_show_confirmation failed to send: {e}")
        await state.clear()
        await msg.answer("⚠️ Не удалось показать подтверждение. Начните заново: 📅 Записаться на приём")
        return
    await state.set_state(BookFlow.confirm)


@router.callback_query(F.data == "med_confirm")
async def cb_confirm(cb: CallbackQuery, state: FSMContext, bot: Bot, config: BookingMedicalConfig):
    await cb.answer()
    data = await state.get_data()
    slot_id = data.get("slot_id")
    user_id = cb.from_user.id
    full_name = cb.from_user.full_name or ""

    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute("SELECT status FROM slots WHERE id=?", (slot_id,))).fetchone()
        if not row or row[0] != "active":
            await cb.message.edit_text("❌ Это время уже занято! Выберите другое.")
            await state.clear(); return

        patient_id = await _get_or_create_patient(
            db, user_id, full_name, data.get("client_phone", ""), data.get("policy_number", "")
        )
        existing_count = (await (await db.execute(
            "SELECT COUNT(*) FROM appointments WHERE patient_id=?", (patient_id,)
        )).fetchone())[0]
        is_repeat_visit = 1 if existing_count > 0 else 0

        await db.execute("UPDATE slots SET status='booked' WHERE id=?", (slot_id,))
        try:
            await db.execute(
                "INSERT INTO appointments (slot_id, patient_id, complaint, allergies, policy_number, is_repeat_visit) "
                "VALUES (?,?,?,?,?,?)",
                (slot_id, patient_id, data.get("complaint", ""), data.get("allergies", ""),
                 data.get("policy_number", ""), is_repeat_visit)
            )
        except sqlite3.IntegrityError:
            # Review-found: double-tap on "✅ Подтвердить" — appointments.slot_id
            # UNIQUE catches the race on the losing request (no duplicate row),
            # but without this the loser previously got no response at all.
            await db.rollback()
            await cb.message.edit_text("❌ Это время только что заняли! Выберите другое.")
            await state.clear(); return
        await db.commit()

    await state.clear()
    slot_date = data["slot_date"]
    slot_time = data.get("slot_time", "")
    doctor_name = data.get("doctor_name", "")

    await cb.message.edit_text(
        f"✅ <b>Запись подтверждена!</b>\n\n"
        f"👨‍⚕️ {_esc(doctor_name)}\n"
        f"📅 {_date_label(slot_date)} в {_esc(slot_time)}\n\n"
        f"Ждём вас! Напомним за день до визита. Если нужно отменить — нажмите ❌ Отменить запись.",
        parse_mode="HTML"
    )

    # Admin notification — DELIBERATELY excludes complaint/allergies/policy
    # (privacy requirement): only enough info to prepare for the visit is
    # pushed proactively. Sensitive fields are reachable ONLY via the explicit
    # "🗂 Карта пациента" action below (adm_card), never broadcast here.
    repeat_icon = " 🔁" if is_repeat_visit else ""
    for admin_id in _load_admins(config.admins_file):
        try:
            await bot.send_message(
                int(admin_id),
                OWNER_NOTIFY_TEXT +
                f"🏥 {_esc(data.get('specialization', ''))}\n"
                f"👨‍⚕️ {_esc(doctor_name)}\n"
                f"📅 {_date_label(slot_date)} {_esc(slot_time)}{repeat_icon}\n"
                f"👤 {_esc(full_name)}",
                parse_mode="HTML",
            )
        except Exception:
            pass

    # Reminder — one day before the visit. Scheduled as a one-shot task
    # created DURING this live request (same mechanism as trip_manager.py's
    # _remind), not a persistent background loop — this sidesteps the
    # documented "_digest_loop doesn't run in webhook mode" gap entirely,
    # since there's no boot-time wiring dependency: the task lives on
    # whichever event loop is already running this request (webhook or
    # polling), for as long as the process stays up.
    #
    # Explicit boundary case (owner-specified): if the appointment is LESS
    # than a day away, remind_at has already passed — delay would be <= 0.
    # We do NOT create the task at all in that case (no back-dated/immediate
    # send) — this is a deliberate deviation from trip_manager._remind, which
    # sends immediately if its own delay <= 0. A reminder whose entire point
    # is "the day before" has no correct behavior once that day is gone, so
    # skipping it entirely is more correct here than sending it late.
    try:
        slot_dt = datetime.strptime(f"{slot_date} {slot_time}", "%Y-%m-%d %H:%M")
        remind_at_dt = slot_dt - timedelta(days=1)
        delay = (remind_at_dt - datetime.now()).total_seconds()
    except ValueError:
        delay = -1
    if delay > 0:
        asyncio.create_task(_remind_day_before(bot, user_id, doctor_name, slot_date, slot_time, delay, config.bot_name))


async def _remind_day_before(bot: Bot, chat_id: int, doctor_name: str, slot_date: str, slot_time: str, delay: float, bot_name: str):
    # No medical content here either (same privacy boundary as the admin
    # notification) — a reminder only needs doctor + time.
    try:
        await asyncio.sleep(delay)
        await bot.send_message(
            chat_id,
            f"⏰ <b>Напоминание:</b> завтра, {_date_label(slot_date)} в {slot_time}, "
            f"у вас приём к врачу {doctor_name}.",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"[{bot_name}] Reminder error: {e}")


@router.callback_query(F.data == "med_cancel")
async def cb_book_cancel(cb: CallbackQuery, state: FSMContext):
    await cb.answer(); await state.clear()
    await cb.message.edit_text("Отменено.")
    await cb.message.answer("Чем могу помочь?", reply_markup=kb_main())


# ── MY APPOINTMENTS ───────────────────────────────────────────────────────────

@router.message(F.text == "📋 Мои записи")
async def my_appointments(msg: Message, config: BookingMedicalConfig):
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT s.slot_date, s.slot_time, d.name AS doctor_name, d.specialization "
            "FROM appointments a "
            "JOIN slots s ON a.slot_id = s.id "
            "JOIN doctors d ON s.doctor_id = d.id "
            "JOIN patients p ON a.patient_id = p.id "
            "WHERE p.user_id = ? ORDER BY s.slot_date, s.slot_time",
            (msg.from_user.id,)
        )).fetchall()
    if not rows:
        await msg.answer("📭 У вас нет активных записей.", reply_markup=kb_main()); return
    lines = ["📋 <b>Ваши записи:</b>\n"]
    for r in [dict(x) for x in rows]:
        lines.append(f"• {_date_label(r['slot_date'])} {r['slot_time']} · {_esc(r['doctor_name'])} ({_esc(r['specialization'])})")
    await msg.answer("\n".join(lines), parse_mode="HTML", reply_markup=kb_main())


# ── CANCEL APPOINTMENT ────────────────────────────────────────────────────────

@router.message(F.text == "❌ Отменить запись")
async def cancel_start(msg: Message, config: BookingMedicalConfig):
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT a.id, s.slot_date, s.slot_time, d.name AS doctor_name "
            "FROM appointments a "
            "JOIN slots s ON a.slot_id = s.id "
            "JOIN doctors d ON s.doctor_id = d.id "
            "JOIN patients p ON a.patient_id = p.id "
            "WHERE p.user_id = ? AND s.slot_date >= date('now','localtime') "
            "ORDER BY s.slot_date",
            (msg.from_user.id,)
        )).fetchall()
    if not rows:
        await msg.answer("Актуальных записей не найдено.", reply_markup=kb_main()); return
    btns = []
    for r in [dict(x) for x in rows]:
        btns.append([InlineKeyboardButton(
            text=f"❌ {_date_label(r['slot_date'])} {r['slot_time']} · {r['doctor_name']}",
            callback_data=f"med_pt_cancel:{r['id']}"
        )])
    await msg.answer("Выберите запись для отмены:", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))

@router.callback_query(F.data.startswith("med_pt_cancel:"))
async def cb_pt_cancel(cb: CallbackQuery, config: BookingMedicalConfig):
    await cb.answer()
    appointment_id = int(cb.data.split(":")[1])
    async with aiosqlite.connect(config.db_path) as db:
        # Review-found: must confirm the appointment actually belongs to the
        # caller — appointment_id is a plain autoincrement int, guessable/
        # enumerable, and cancelling someone else's visit by id alone would
        # otherwise be possible.
        row = await (await db.execute(
            "SELECT a.slot_id FROM appointments a JOIN patients p ON a.patient_id = p.id "
            "WHERE a.id=? AND p.user_id=?",
            (appointment_id, cb.from_user.id)
        )).fetchone()
        if not row:
            await cb.message.edit_text("Запись не найдена."); return
        slot_id = row[0]
        await db.execute("UPDATE slots SET status='active' WHERE id=?", (slot_id,))
        await db.execute("DELETE FROM appointments WHERE id=?", (appointment_id,))
        await db.commit()
    await cb.message.edit_text("✅ Запись отменена. Слот освобождён.")


# ── ADMIN: ALL APPOINTMENTS — day selector → schedule (no medical fields) ─────

@router.message(F.text == "🗂 Все записи")
async def all_appointments(msg: Message, config: BookingMedicalConfig):
    if str(msg.from_user.id) not in _load_admins(config.admins_file):
        await msg.answer("⛔ Нет доступа"); return
    async with aiosqlite.connect(config.db_path) as db:
        rows = await (await db.execute(
            "SELECT DISTINCT s.slot_date FROM appointments a JOIN slots s ON a.slot_id=s.id "
            "WHERE s.slot_date >= date('now','localtime') ORDER BY s.slot_date LIMIT 30"
        )).fetchall()
    if not rows:
        await msg.answer("Актуальных записей нет."); return
    btns = [[InlineKeyboardButton(text=f"📅 {_date_label(r[0])}", callback_data=f"adm_day:{r[0]}")] for r in rows]
    await msg.answer("📅 Выберите день для просмотра расписания:", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))

@router.callback_query(F.data.startswith("adm_day:"))
async def cb_adm_day(cb: CallbackQuery, config: BookingMedicalConfig):
    if str(cb.from_user.id) not in _load_admins(config.admins_file):
        await cb.answer("⛔ Нет доступа", show_alert=True); return
    await cb.answer()
    slot_date = cb.data.split(":", 1)[1]
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT a.id, a.is_repeat_visit, s.slot_time, d.name AS doctor_name, d.specialization, "
            "p.full_name, p.phone "
            "FROM appointments a "
            "JOIN slots s ON a.slot_id = s.id "
            "JOIN doctors d ON s.doctor_id = d.id "
            "JOIN patients p ON a.patient_id = p.id "
            "WHERE s.slot_date=? ORDER BY s.slot_time",
            (slot_date,)
        )).fetchall()
    lines = [f"📅 <b>{_date_label(slot_date)} — расписание</b>\n"]
    btns = []
    for r in [dict(x) for x in rows]:
        repeat_icon = " 🔁" if r["is_repeat_visit"] else ""
        name = r.get("full_name", "") or "—"
        lines.append(
            f"⏰ <b>{_esc(r['slot_time'])}</b>  👨‍⚕️ {_esc(r['doctor_name'])} ({_esc(r['specialization'])}){repeat_icon}\n"
            f"   👤 {_esc(name)}  📱 {_esc(r.get('phone', '') or '—')}\n"
        )
        short_name = name if len(name) <= 20 else name[:17] + "…"
        btns.append([InlineKeyboardButton(
            text=f"🗂 Карта: {short_name} {r['slot_time']}",
            callback_data=f"adm_card:{r['id']}"
        )])
    text = "\n".join(lines)
    if len(text) > 3500: text = text[:3400] + "\n…"
    btns.append([InlineKeyboardButton(text="◀️ К выбору дня", callback_data="adm_day_back")])
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))

@router.callback_query(F.data == "adm_day_back")
async def cb_adm_day_back(cb: CallbackQuery, config: BookingMedicalConfig):
    if str(cb.from_user.id) not in _load_admins(config.admins_file):
        await cb.answer("⛔ Нет доступа", show_alert=True); return
    await cb.answer()
    async with aiosqlite.connect(config.db_path) as db:
        rows = await (await db.execute(
            "SELECT DISTINCT s.slot_date FROM appointments a JOIN slots s ON a.slot_id=s.id "
            "WHERE s.slot_date >= date('now','localtime') ORDER BY s.slot_date LIMIT 30"
        )).fetchall()
    btns = [[InlineKeyboardButton(text=f"📅 {_date_label(r[0])}", callback_data=f"adm_day:{r[0]}")] for r in rows]
    await cb.message.edit_text("📅 Выберите день:", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))


# ── ADMIN: "открыть карту пациента" — the ONLY place complaint/allergies/ ─────
# ── policy/history are ever shown, always as an explicit, private action ─────

@router.callback_query(F.data.startswith("adm_card:"))
async def cb_adm_card(cb: CallbackQuery, config: BookingMedicalConfig):
    if str(cb.from_user.id) not in _load_admins(config.admins_file):
        await cb.answer("⛔ Нет доступа", show_alert=True); return
    await cb.answer()
    appointment_id = int(cb.data.split(":", 1)[1])
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            "SELECT a.complaint, a.allergies, a.policy_number, a.patient_id, "
            "p.full_name, p.phone, d.name AS doctor_name, d.specialization, "
            "s.slot_date, s.slot_time "
            "FROM appointments a "
            "JOIN patients p ON a.patient_id = p.id "
            "JOIN slots s ON a.slot_id = s.id "
            "JOIN doctors d ON s.doctor_id = d.id "
            "WHERE a.id=?", (appointment_id,)
        )).fetchone()
        if not row:
            await cb.message.answer("Запись не найдена."); return
        row = dict(row)
        history = await (await db.execute(
            "SELECT s.slot_date, d.name, a.complaint FROM appointments a "
            "JOIN slots s ON a.slot_id=s.id JOIN doctors d ON s.doctor_id=d.id "
            "WHERE a.patient_id=? AND a.id != ? ORDER BY s.slot_date DESC LIMIT 5",
            (row["patient_id"], appointment_id)
        )).fetchall()
    text = (
        f"🗂 <b>Карта пациента</b>\n\n"
        f"👤 {_esc(row['full_name']) or '—'} · 📱 {_esc(row['phone']) or '—'}\n"
        f"🏥 {_esc(row['specialization'])} · 👨‍⚕️ {_esc(row['doctor_name'])}\n"
        f"📅 {_date_label(row['slot_date'])} {_esc(row['slot_time'])}\n\n"
        f"💬 Жалоба: {_esc(row['complaint']) or '—'}\n"
        f"⚠️ Аллергии: {_esc(row['allergies']) or 'нет'}\n"
        f"📄 Полис: {_esc(row['policy_number']) or '—'}\n"
    )
    if history:
        text += "\n<b>История визитов:</b>\n"
        for h in history:
            text += f"• {_date_label(h[0])} · {_esc(h[1])} · {_esc(h[2]) or '—'}\n"
    if len(text) > 3500:
        text = text[:3400] + "\n…"
    try:
        await cb.message.answer(text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"cb_adm_card failed to send: {e}")
        await cb.message.answer("⚠️ Не удалось отобразить карту пациента.")


@router.message(F.text == "📊 Статистика")
async def admin_stats(msg: Message, config: BookingMedicalConfig):
    if str(msg.from_user.id) not in _load_admins(config.admins_file):
        await msg.answer("⛔ Нет доступа"); return
    async with aiosqlite.connect(config.db_path) as db:
        total = (await (await db.execute("SELECT COUNT(*) FROM appointments")).fetchone())[0]
        future = (await (await db.execute(
            "SELECT COUNT(*) FROM appointments a JOIN slots s ON a.slot_id=s.id WHERE s.slot_date>=date('now','localtime')"
        )).fetchone())[0]
        repeat = (await (await db.execute(
            "SELECT COUNT(*) FROM appointments WHERE is_repeat_visit=1"
        )).fetchone())[0]
        by_spec = await (await db.execute(
            "SELECT d.specialization, COUNT(*) FROM appointments a "
            "JOIN slots s ON a.slot_id=s.id JOIN doctors d ON s.doctor_id=d.id "
            "GROUP BY d.specialization ORDER BY 2 DESC"
        )).fetchall()
    lines = [f"📊 <b>Статистика</b>\n",
             f"📋 Всего записей: {total}",
             f"📅 Предстоящих: {future}",
             f"🔁 Повторных визитов: {repeat}",
             "\n<b>По специализациям:</b>"] + [f"  🏥 {s}: {c}" for s, c in by_spec]
    await msg.answer("\n".join(lines), parse_mode="HTML")


# ── ADMIN: doctors ────────────────────────────────────────────────────────────

@router.message(Command("adddoctor"))
async def cmd_adddoctor(msg: Message, config: BookingMedicalConfig):
    if str(msg.from_user.id) not in _load_admins(config.admins_file):
        await msg.answer("⛔ Нет доступа"); return
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2 or "|" not in parts[1]:
        await msg.answer("Использование: /adddoctor Имя Фамилия | Специализация"); return
    name, spec = (p.strip() for p in parts[1].split("|", 1))
    if not name or not spec:
        await msg.answer("Использование: /adddoctor Имя Фамилия | Специализация"); return
    if len(name) > 40 or len(spec) > 40:
        # Review-found: an overly long name/specialization breaks kb_doctors'
        # button text (BUTTON_TEXT_INVALID) for the WHOLE specialization list,
        # not just this doctor — reject at input time instead.
        await msg.answer("⚠️ Имя и специализация должны быть короче 40 символов."); return
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute("INSERT INTO doctors (name, specialization) VALUES (?,?)", (name, spec))
        await db.commit()
    await _ensure_slots(config.db_path)
    await msg.answer(f"✅ Врач <b>{_esc(name)}</b> ({_esc(spec)}) добавлен.", parse_mode="HTML")

@router.message(Command("removedoctor"))
async def cmd_removedoctor(msg: Message, config: BookingMedicalConfig):
    if str(msg.from_user.id) not in _load_admins(config.admins_file):
        await msg.answer("⛔ Нет доступа"); return
    parts = msg.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        await msg.answer("Использование: /removedoctor <id>"); return
    doctor_id = int(parts[1])
    async with aiosqlite.connect(config.db_path) as db:
        cur = await db.execute("UPDATE doctors SET active=0 WHERE id=?", (doctor_id,))
        await db.commit()
    if cur.rowcount == 0:
        await msg.answer(f"Врач #{doctor_id} не найден."); return
    await msg.answer(f"✅ Врач #{doctor_id} деактивирован (история записей сохранена).")

@router.message(Command("doctors"))
async def cmd_doctors(msg: Message, config: BookingMedicalConfig):
    if str(msg.from_user.id) not in _load_admins(config.admins_file):
        await msg.answer("⛔ Нет доступа"); return
    async with aiosqlite.connect(config.db_path) as db:
        rows = await (await db.execute(
            "SELECT id, name, specialization, active FROM doctors ORDER BY specialization, name"
        )).fetchall()
    if not rows:
        await msg.answer("Список врачей пуст."); return
    lines = ["👨‍⚕️ <b>Врачи:</b>\n"]
    for r in rows:
        status = "✅" if r[3] else "🚫"
        lines.append(f"{status} #{r[0]} {_esc(r[1])} — {_esc(r[2])}")
    await msg.answer("\n".join(lines), parse_mode="HTML")


# ── ADMIN COMMANDS ────────────────────────────────────────────────────────────

@router.message(Command("addadmin"))
async def cmd_addadmin(msg: Message, config: BookingMedicalConfig):
    if str(msg.from_user.id) not in _load_admins(config.admins_file): await msg.answer("⛔ Нет доступа"); return
    parts = msg.text.split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit(): await msg.answer("Использование: /addadmin <id>"); return
    ids = _load_admins(config.admins_file); ids.add(parts[1]); _save_admins(config.admins_file, ids)
    await msg.answer(f"✅ <code>{parts[1]}</code> добавлен.", parse_mode="HTML")

@router.message(Command("removeadmin"))
async def cmd_removeadmin(msg: Message, config: BookingMedicalConfig):
    if str(msg.from_user.id) not in _load_admins(config.admins_file): await msg.answer("⛔ Нет доступа"); return
    parts = msg.text.split()
    if len(parts) < 2: await msg.answer("Использование: /removeadmin <id>"); return
    ids = _load_admins(config.admins_file); ids.discard(parts[1]); _save_admins(config.admins_file, ids)
    await msg.answer(f"✅ <code>{_esc(parts[1])}</code> удалён.", parse_mode="HTML")

@router.message(Command("admins"))
async def cmd_admins(msg: Message, config: BookingMedicalConfig):
    if str(msg.from_user.id) not in _load_admins(config.admins_file): await msg.answer("⛔ Нет доступа"); return
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
