# TEMPLATE: booking_beauty
# USE FOR: beauty salons, nail/hair/brow/lash studios, spa — онлайн запись
# CUSTOMIZE: sections marked with # CUSTOMIZE

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, Message, ReplyKeyboardMarkup,
)

# ── CUSTOMIZE ────────────────────────────────────────────────────────────────
BOT_DESCRIPTION = "Онлайн-запись в студию красоты. Выберите услугу, дату, время — и вы записаны!"
WELCOME_TEXT = (
    "💅 <b>Онлайн-запись</b>\n\n"
    "Запишитесь к нашим мастерам прямо в Telegram!\n"
    "Выберите удобное время и мы вас ждём.\n\n"
    "Нажмите <b>📅 Записаться</b>:"
)
OWNER_NOTIFY_TEXT = "🔔 <b>Новая запись!</b>\n"
SERVICES = ["💅 Маникюр", "💅 Педикюр", "💇 Стрижка", "💆 Уход за лицом", "👁 Брови / ресницы"]
MASTERS = ["Анна", "Мария", "Екатерина"]
DAYS_AHEAD = 14
SLOT_TIMES = ["10:00", "11:00", "12:00", "13:00", "14:00", "15:00", "16:00", "17:00", "18:00", "19:00"]
SLOT_PRICE = 0
# ── END CUSTOMIZE ─────────────────────────────────────────────────────────────

BOT_NAME = Path(__file__).stem
DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = str(DATA_DIR / f"{BOT_NAME}_data.db")
WELCOME_IMAGE = DATA_DIR / "bot_images" / f"{BOT_NAME}.jpg"
ADMINS_FILE = DATA_DIR / f"admins_{BOT_NAME}.json"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
router = Router()

DAYS_RU = {0: "Пн", 1: "Вт", 2: "Ср", 3: "Чт", 4: "Пт", 5: "Сб", 6: "Вс"}
MONTHS_RU = {1: "янв", 2: "фев", 3: "мар", 4: "апр", 5: "май", 6: "июн",
             7: "июл", 8: "авг", 9: "сен", 10: "окт", 11: "ноя", 12: "дек"}


# ── admin helpers ─────────────────────────────────────────────────────────────

def _load_admins() -> set:
    try:
        return set(json.loads(ADMINS_FILE.read_text()).get("ids", []))
    except Exception:
        return set()

def _save_admins(ids: set) -> None:
    ADMINS_FILE.write_text(json.dumps({"ids": list(ids)}, ensure_ascii=False))


# ── phone validation ──────────────────────────────────────────────────────────

def _normalize_phone(raw: str) -> str | None:
    digits = re.sub(r"[^\d]", "", raw)
    if len(digits) == 11 and digits[0] in ("7", "8"):
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    else:
        return None
    return f"+{digits[0]} ({digits[1:4]}) {digits[4:7]}-{digits[7:9]}-{digits[9:11]}"


# ── db ────────────────────────────────────────────────────────────────────────

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS slots (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                slot_date    TEXT NOT NULL,
                slot_time    TEXT NOT NULL,
                duration_min INTEGER DEFAULT 60,
                master       TEXT,
                service      TEXT,
                price        REAL DEFAULT 0,
                status       TEXT DEFAULT 'active'
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bookings (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                slot_id      INTEGER UNIQUE REFERENCES slots(id),
                client_name  TEXT,
                client_phone TEXT,
                service      TEXT,
                comment      TEXT,
                created_at   TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.commit()
    await _ensure_slots()


async def _ensure_slots():
    today = datetime.now().date()
    async with aiosqlite.connect(DB_PATH) as db:
        for i in range(DAYS_AHEAD):
            d = (today + timedelta(days=i)).isoformat()
            existing = (await (await db.execute(
                "SELECT COUNT(*) FROM slots WHERE slot_date=?", (d,)
            )).fetchone())[0]
            if existing == 0:
                for t in SLOT_TIMES:
                    for master in MASTERS:
                        await db.execute(
                            "INSERT INTO slots (slot_date, slot_time, master, price) VALUES (?,?,?,?)",
                            (d, t, master, SLOT_PRICE)
                        )
        await db.commit()


# ── keyboards ─────────────────────────────────────────────────────────────────

def kb_main() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📅 Записаться")],
        [KeyboardButton(text="📋 Мои записи"), KeyboardButton(text="❌ Отменить запись")],
    ], resize_keyboard=True)

def kb_admin() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📅 Записаться")],
        [KeyboardButton(text="📋 Мои записи"), KeyboardButton(text="❌ Отменить запись")],
        [KeyboardButton(text="🗂 Все записи"), KeyboardButton(text="📊 Статистика")],
    ], resize_keyboard=True)

def kb_services() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=s, callback_data=f"book_svc:{s}")] for s in SERVICES]
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="book_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_days() -> InlineKeyboardMarkup:
    today = datetime.now().date()
    rows = []
    for i in range(DAYS_AHEAD):
        d = today + timedelta(days=i)
        label = f"{DAYS_RU[d.weekday()]} {d.day} {MONTHS_RU[d.month]}"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"book_day:{d.isoformat()}")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="book_to_service")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def kb_times(slot_date: str) -> InlineKeyboardMarkup:
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await (await db.execute(
            "SELECT DISTINCT slot_time FROM slots WHERE slot_date=? AND status='active' ORDER BY slot_time",
            (slot_date,)
        )).fetchall()
    btns = []
    for (t,) in rows:
        btns.append([InlineKeyboardButton(text=f"⏰ {t}", callback_data=f"book_time:{slot_date}:{t}")])
    if not btns:
        btns.append([InlineKeyboardButton(text="❌ Нет свободных слотов", callback_data="book_noslots")])
    btns.append([InlineKeyboardButton(text="◀️ Назад", callback_data="book_to_days")])
    return InlineKeyboardMarkup(inline_keyboard=btns)

async def kb_masters_for_time(slot_date: str, slot_time: str) -> InlineKeyboardMarkup:
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await (await db.execute(
            "SELECT id, master FROM slots WHERE slot_date=? AND slot_time=? AND status='active' ORDER BY master",
            (slot_date, slot_time)
        )).fetchall()
    btns = [[InlineKeyboardButton(text=f"👩 {r[1]}", callback_data=f"book_slot:{r[0]}")] for r in rows]
    btns.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"book_to_times:{slot_date}")])
    return InlineKeyboardMarkup(inline_keyboard=btns)

def kb_confirm(slot_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"book_confirm:{slot_id}"),
         InlineKeyboardButton(text="❌ Отмена",      callback_data="book_cancel")],
    ])

def kb_adm_booking(booking_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🗑 Отменить запись", callback_data=f"adm_cancel:{booking_id}"),
    ]])


# ── FSM ───────────────────────────────────────────────────────────────────────

class BookFlow(StatesGroup):
    service = State(); day = State(); time = State(); slot = State()
    name = State(); phone = State(); confirm = State()

class FindFlow(StatesGroup):
    phone = State()

class CancelFlow(StatesGroup):
    phone = State()


# ── /start ────────────────────────────────────────────────────────────────────

@router.message(Command("start"))
async def cmd_start(message: Message):
    admins = _load_admins()
    if not admins:
        _save_admins({str(message.from_user.id)})
    is_admin = str(message.from_user.id) in _load_admins()
    kb = kb_admin() if is_admin else kb_main()
    if WELCOME_IMAGE.exists():
        await message.answer_photo(FSInputFile(str(WELCOME_IMAGE)),
                                   caption=WELCOME_TEXT, parse_mode="HTML", reply_markup=kb)
    else:
        await message.answer(WELCOME_TEXT, parse_mode="HTML", reply_markup=kb)


# ── BOOK FLOW: service → day → time → master → name → phone → confirm ─────────

@router.message(F.text == "📅 Записаться")
async def book_start(msg: Message, state: FSMContext):
    await state.set_state(BookFlow.service)
    await msg.answer("💅 Выберите услугу:", reply_markup=kb_services())

@router.callback_query(F.data.startswith("book_svc:"))
async def cb_service(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    service = cb.data.split(":", 1)[1]
    await state.update_data(service=service)
    await state.set_state(BookFlow.day)
    await cb.message.edit_text(f"✅ Услуга: {service}\n\n📅 Выберите день:", reply_markup=kb_days())

@router.callback_query(F.data == "book_to_service")
async def cb_to_service(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(BookFlow.service)
    await cb.message.edit_text("💅 Выберите услугу:", reply_markup=kb_services())

@router.callback_query(F.data.startswith("book_day:"))
async def cb_day(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    slot_date = cb.data.split(":")[1]
    await state.update_data(slot_date=slot_date)
    await state.set_state(BookFlow.time)
    data = await state.get_data()
    d = datetime.strptime(slot_date, "%Y-%m-%d")
    label = f"{DAYS_RU[d.weekday()]} {d.day} {MONTHS_RU[d.month]}"
    await cb.message.edit_text(
        f"✅ Услуга: {data.get('service', '')}\n📅 {label}\n\n⏰ Выберите время:",
        reply_markup=await kb_times(slot_date)
    )

@router.callback_query(F.data == "book_to_days")
async def cb_to_days(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(BookFlow.day)
    data = await state.get_data()
    await cb.message.edit_text(
        f"✅ Услуга: {data.get('service', '')}\n\n📅 Выберите день:",
        reply_markup=kb_days()
    )

@router.callback_query(F.data.startswith("book_time:"))
async def cb_time(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    _, slot_date, slot_time = cb.data.split(":", 2)
    await state.update_data(slot_time=slot_time)
    data = await state.get_data()
    d = datetime.strptime(slot_date, "%Y-%m-%d")
    label = f"{DAYS_RU[d.weekday()]} {d.day} {MONTHS_RU[d.month]}"

    async with aiosqlite.connect(DB_PATH) as db:
        available = await (await db.execute(
            "SELECT id, master FROM slots WHERE slot_date=? AND slot_time=? AND status='active' ORDER BY master",
            (slot_date, slot_time)
        )).fetchall()

    if not available:
        await cb.answer("Это время уже занято, выберите другое.", show_alert=True); return

    if len(available) == 1:
        slot_id, master = available[0]
        await state.update_data(slot_id=slot_id, master=master)
        await state.set_state(BookFlow.name)
        await cb.message.edit_text(
            f"✅ Услуга: {data.get('service', '')}\n"
            f"📅 {label} в {slot_time}\n"
            f"👩 Мастер: {master}\n\n"
            f"👤 Введите ваше имя:"
        )
    else:
        await state.set_state(BookFlow.slot)
        await cb.message.edit_text(
            f"✅ Услуга: {data.get('service', '')}\n"
            f"📅 {label} в {slot_time}\n\n"
            f"👩 Выберите мастера:",
            reply_markup=await kb_masters_for_time(slot_date, slot_time)
        )

@router.callback_query(F.data.startswith("book_to_times:"))
async def cb_to_times(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    slot_date = cb.data.split(":", 2)[1]
    await state.update_data(slot_date=slot_date)
    await state.set_state(BookFlow.time)
    data = await state.get_data()
    d = datetime.strptime(slot_date, "%Y-%m-%d")
    label = f"{DAYS_RU[d.weekday()]} {d.day} {MONTHS_RU[d.month]}"
    await cb.message.edit_text(
        f"✅ Услуга: {data.get('service', '')}\n📅 {label}\n\n⏰ Выберите время:",
        reply_markup=await kb_times(slot_date)
    )

@router.callback_query(F.data.startswith("book_slot:"))
async def cb_slot(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    slot_id = int(cb.data.split(":")[1])
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute(
            "SELECT status, master, slot_date, slot_time FROM slots WHERE id=?", (slot_id,)
        )).fetchone()
    if not row or row[0] != "active":
        await cb.answer("Это время уже занято!", show_alert=True); return
    _, master, slot_date, slot_time = row
    await state.update_data(slot_id=slot_id, master=master)
    await state.set_state(BookFlow.name)
    data = await state.get_data()
    d = datetime.strptime(slot_date, "%Y-%m-%d")
    label = f"{DAYS_RU[d.weekday()]} {d.day} {MONTHS_RU[d.month]}"
    await cb.message.edit_text(
        f"✅ Услуга: {data.get('service', '')}\n"
        f"📅 {label} в {slot_time}\n"
        f"👩 Мастер: {master}\n\n"
        f"👤 Введите ваше имя:"
    )

@router.message(BookFlow.name, F.text)
async def book_name(msg: Message, state: FSMContext):
    await state.update_data(client_name=msg.text.strip())
    await state.set_state(BookFlow.phone)
    await msg.answer("📱 Введите номер телефона (например: +7 999 123-45-67 или 89991234567):")

@router.message(BookFlow.phone, F.text)
async def book_phone(msg: Message, state: FSMContext):
    phone = _normalize_phone(msg.text.strip())
    if phone is None:
        await msg.answer(
            "❌ Неверный номер телефона.\n"
            "Введите российский номер, например: <b>+7 999 123-45-67</b> или <b>89991234567</b>",
            parse_mode="HTML"
        )
        return
    await state.update_data(client_phone=phone)
    data = await state.get_data()
    slot_id = data["slot_id"]
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute(
            "SELECT slot_date, slot_time, master FROM slots WHERE id=?", (slot_id,)
        )).fetchone()
    if not row:
        await msg.answer("Слот не найден.", reply_markup=kb_main()); await state.clear(); return
    slot_date, slot_time, master = row
    d = datetime.strptime(slot_date, "%Y-%m-%d")
    date_ru = f"{DAYS_RU[d.weekday()]} {d.day} {MONTHS_RU[d.month]}"
    await state.set_state(BookFlow.confirm)
    await msg.answer(
        f"📋 <b>Подтвердите запись:</b>\n\n"
        f"💅 Услуга: {data['service']}\n"
        f"📅 {date_ru} в {slot_time}\n"
        f"👩 Мастер: {master}\n"
        f"👤 Имя: {data['client_name']}\n"
        f"📱 Телефон: {phone}",
        parse_mode="HTML", reply_markup=kb_confirm(slot_id)
    )

@router.callback_query(F.data.startswith("book_confirm:"))
async def cb_confirm(cb: CallbackQuery, state: FSMContext, bot: Bot):
    await cb.answer()
    slot_id = int(cb.data.split(":")[1])
    data = await state.get_data()
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute("SELECT status FROM slots WHERE id=?", (slot_id,))).fetchone()
        if not row or row[0] != "active":
            await cb.message.edit_text("❌ Это время уже занято! Выберите другое.")
            await state.clear(); return
        await db.execute("UPDATE slots SET status='booked' WHERE id=?", (slot_id,))
        cur = await db.execute(
            "INSERT INTO bookings (slot_id, client_name, client_phone, service) VALUES (?,?,?,?)",
            (slot_id, data.get("client_name"), data.get("client_phone"), data.get("service"))
        )
        booking_id = cur.lastrowid
        slot_row = await (await db.execute(
            "SELECT slot_date, slot_time, master FROM slots WHERE id=?", (slot_id,)
        )).fetchone()
        await db.commit()
    await state.clear()
    slot_date, slot_time, master = slot_row
    d = datetime.strptime(slot_date, "%Y-%m-%d")
    date_ru = f"{DAYS_RU[d.weekday()]} {d.day} {MONTHS_RU[d.month]}"
    await cb.message.edit_text(
        f"✅ <b>Запись подтверждена!</b>\n\n"
        f"💅 {data.get('service', '')}\n"
        f"📅 {date_ru} в {slot_time}\n"
        f"👩 Мастер: {master}\n\n"
        f"Ждём вас! Если нужно отменить — нажмите ❌ Отменить запись.",
        parse_mode="HTML"
    )
    for admin_id in _load_admins():
        try:
            await bot.send_message(
                int(admin_id),
                OWNER_NOTIFY_TEXT +
                f"💅 {data.get('service', '')}\n"
                f"📅 {date_ru} {slot_time}\n"
                f"👩 {master}\n"
                f"👤 {data.get('client_name', '')} · {data.get('client_phone', '')}",
                parse_mode="HTML", reply_markup=kb_adm_booking(booking_id)
            )
        except Exception:
            pass

@router.callback_query(F.data == "book_noslots")
async def cb_noslots(cb: CallbackQuery):
    await cb.answer("На этот день нет свободных слотов. Выберите другой день.", show_alert=True)

@router.callback_query(F.data == "book_cancel")
async def cb_book_cancel(cb: CallbackQuery, state: FSMContext):
    await cb.answer(); await state.clear()
    await cb.message.edit_text("Отменено.")
    await cb.message.answer("Чем могу помочь?", reply_markup=kb_main())


# ── MY BOOKINGS ───────────────────────────────────────────────────────────────

@router.message(F.text == "📋 Мои записи")
async def my_bookings_start(msg: Message, state: FSMContext):
    await state.set_state(FindFlow.phone)
    await msg.answer("📱 Введите ваш номер телефона, чтобы найти записи:")

@router.message(FindFlow.phone, F.text)
async def my_bookings_find(msg: Message, state: FSMContext):
    await state.clear()
    raw = msg.text.strip()
    norm = _normalize_phone(raw)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT b.*, s.slot_date, s.slot_time, s.master FROM bookings b "
            "JOIN slots s ON b.slot_id=s.id "
            "WHERE b.client_phone=? OR b.client_phone=? ORDER BY s.slot_date, s.slot_time",
            (raw, norm or raw)
        )).fetchall()
    if not rows:
        await msg.answer("Записей с этим номером не найдено.", reply_markup=kb_main()); return
    lines = ["📋 <b>Ваши записи:</b>\n"]
    for r in [dict(x) for x in rows]:
        d = datetime.strptime(r["slot_date"], "%Y-%m-%d")
        date_ru = f"{DAYS_RU[d.weekday()]} {d.day} {MONTHS_RU[d.month]}"
        lines.append(f"• {date_ru} {r['slot_time']} · {r['master']} · {r.get('service', '')}")
    await msg.answer("\n".join(lines), parse_mode="HTML", reply_markup=kb_main())


# ── CANCEL BOOKING ────────────────────────────────────────────────────────────

@router.message(F.text == "❌ Отменить запись")
async def cancel_start(msg: Message, state: FSMContext):
    await state.set_state(CancelFlow.phone)
    await msg.answer("📱 Введите ваш номер телефона:")

@router.message(CancelFlow.phone, F.text)
async def cancel_find(msg: Message, state: FSMContext):
    await state.clear()
    raw = msg.text.strip()
    norm = _normalize_phone(raw)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT b.id, s.slot_date, s.slot_time, s.master FROM bookings b "
            "JOIN slots s ON b.slot_id=s.id "
            "WHERE (b.client_phone=? OR b.client_phone=?) AND s.slot_date >= date('now','localtime') "
            "ORDER BY s.slot_date",
            (raw, norm or raw)
        )).fetchall()
    if not rows:
        await msg.answer("Актуальных записей не найдено.", reply_markup=kb_main()); return
    btns = []
    for r in [dict(x) for x in rows]:
        d = datetime.strptime(r["slot_date"], "%Y-%m-%d")
        date_ru = f"{DAYS_RU[d.weekday()]} {d.day} {MONTHS_RU[d.month]}"
        btns.append([InlineKeyboardButton(
            text=f"❌ {date_ru} {r['slot_time']} · {r['master']}",
            callback_data=f"adm_cancel:{r['id']}"
        )])
    await msg.answer("Выберите запись для отмены:", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))

@router.callback_query(F.data.startswith("adm_cancel:"))
async def cb_adm_cancel(cb: CallbackQuery):
    await cb.answer()
    booking_id = int(cb.data.split(":")[1])
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute("SELECT slot_id FROM bookings WHERE id=?", (booking_id,))).fetchone()
        if not row:
            await cb.message.edit_text("Запись не найдена."); return
        slot_id = row[0]
        await db.execute("UPDATE slots SET status='active' WHERE id=?", (slot_id,))
        await db.execute("DELETE FROM bookings WHERE id=?", (booking_id,))
        await db.commit()
    await cb.message.edit_text("✅ Запись отменена. Слот освобождён.")


# ── ADMIN: ALL BOOKINGS — day selector → chronological schedule ───────────────

@router.message(F.text == "🗂 Все записи")
async def all_bookings(msg: Message):
    if str(msg.from_user.id) not in _load_admins():
        await msg.answer("⛔ Нет доступа"); return
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await (await db.execute(
            "SELECT DISTINCT s.slot_date FROM bookings b JOIN slots s ON b.slot_id=s.id "
            "WHERE s.slot_date >= date('now','localtime') ORDER BY s.slot_date LIMIT 30"
        )).fetchall()
    if not rows:
        await msg.answer("Актуальных записей нет."); return
    btns = []
    for (slot_date,) in rows:
        d = datetime.strptime(slot_date, "%Y-%m-%d")
        label = f"📅 {DAYS_RU[d.weekday()]} {d.day} {MONTHS_RU[d.month]}"
        btns.append([InlineKeyboardButton(text=label, callback_data=f"adm_day:{slot_date}")])
    await msg.answer("📅 Выберите день для просмотра расписания:",
                     reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))

@router.callback_query(F.data.startswith("adm_day:"))
async def cb_adm_day(cb: CallbackQuery):
    await cb.answer()
    slot_date = cb.data.split(":")[1]
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT b.id, b.client_name, b.client_phone, b.service, s.slot_time, s.master "
            "FROM bookings b JOIN slots s ON b.slot_id=s.id "
            "WHERE s.slot_date=? ORDER BY s.slot_time, s.master",
            (slot_date,)
        )).fetchall()
    d = datetime.strptime(slot_date, "%Y-%m-%d")
    date_ru = f"{DAYS_RU[d.weekday()]} {d.day} {MONTHS_RU[d.month]}"
    lines = [f"📅 <b>{date_ru} — расписание</b>\n"]
    for r in [dict(x) for x in rows]:
        lines.append(
            f"⏰ <b>{r['slot_time']}</b>  👩 {r['master']}\n"
            f"   💅 {r.get('service', '')}\n"
            f"   👤 {r.get('client_name', '')}  📱 {r.get('client_phone', '')}\n"
        )
    text = "\n".join(lines)
    if len(text) > 4000: text = text[:3900] + "\n…"
    await cb.message.edit_text(
        text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="◀️ К выбору дня", callback_data="adm_day_back")
        ]])
    )

@router.callback_query(F.data == "adm_day_back")
async def cb_adm_day_back(cb: CallbackQuery):
    await cb.answer()
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await (await db.execute(
            "SELECT DISTINCT s.slot_date FROM bookings b JOIN slots s ON b.slot_id=s.id "
            "WHERE s.slot_date >= date('now','localtime') ORDER BY s.slot_date LIMIT 30"
        )).fetchall()
    btns = []
    for (slot_date,) in rows:
        d = datetime.strptime(slot_date, "%Y-%m-%d")
        label = f"📅 {DAYS_RU[d.weekday()]} {d.day} {MONTHS_RU[d.month]}"
        btns.append([InlineKeyboardButton(text=label, callback_data=f"adm_day:{slot_date}")])
    await cb.message.edit_text(
        "📅 Выберите день:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=btns)
    )

@router.message(F.text == "📊 Статистика")
async def admin_stats(msg: Message):
    if str(msg.from_user.id) not in _load_admins():
        await msg.answer("⛔ Нет доступа"); return
    async with aiosqlite.connect(DB_PATH) as db:
        total  = (await (await db.execute("SELECT COUNT(*) FROM bookings")).fetchone())[0]
        future = (await (await db.execute(
            "SELECT COUNT(*) FROM bookings b JOIN slots s ON b.slot_id=s.id WHERE s.slot_date>=date('now','localtime')"
        )).fetchone())[0]
        by_mst = await (await db.execute(
            "SELECT s.master, COUNT(*) FROM bookings b JOIN slots s ON b.slot_id=s.id GROUP BY s.master"
        )).fetchall()
        by_svc = await (await db.execute(
            "SELECT b.service, COUNT(*) FROM bookings b GROUP BY b.service ORDER BY 2 DESC LIMIT 5"
        )).fetchall()
    lines = [f"📊 <b>Статистика</b>\n",
             f"📋 Всего записей: {total}",
             f"📅 Предстоящих: {future}",
             "\n<b>По мастерам:</b>"] + [f"  👩 {m}: {c}" for m, c in by_mst] + \
            ["\n<b>Топ услуг:</b>"] + [f"  💅 {s or '—'}: {c}" for s, c in by_svc]
    await msg.answer("\n".join(lines), parse_mode="HTML")


# ── ADMIN COMMANDS ────────────────────────────────────────────────────────────

@router.message(Command("addadmin"))
async def cmd_addadmin(msg: Message):
    if str(msg.from_user.id) not in _load_admins(): await msg.answer("⛔ Нет доступа"); return
    parts = msg.text.split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit(): await msg.answer("Использование: /addadmin <id>"); return
    ids = _load_admins(); ids.add(parts[1]); _save_admins(ids)
    await msg.answer(f"✅ <code>{parts[1]}</code> добавлен.", parse_mode="HTML")

@router.message(Command("removeadmin"))
async def cmd_removeadmin(msg: Message):
    if str(msg.from_user.id) not in _load_admins(): await msg.answer("⛔ Нет доступа"); return
    parts = msg.text.split()
    if len(parts) < 2: await msg.answer("Использование: /removeadmin <id>"); return
    ids = _load_admins(); ids.discard(parts[1]); _save_admins(ids)
    await msg.answer(f"✅ <code>{parts[1]}</code> удалён.", parse_mode="HTML")

@router.message(Command("admins"))
async def cmd_admins(msg: Message):
    if str(msg.from_user.id) not in _load_admins(): await msg.answer("⛔ Нет доступа"); return
    ids = _load_admins()
    await msg.answer("👥 " + ("\n".join(f"• <code>{i}</code>" for i in ids) or "Пусто"), parse_mode="HTML")


# ── MAIN ──────────────────────────────────────────────────────────────────────

async def main():
    bot = Bot(token=os.getenv("BOT_TOKEN"))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await bot.set_my_description(BOT_DESCRIPTION)
    await init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
