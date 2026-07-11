# TEMPLATE: trip_manager
# USE FOR: travel routes, trip planning, маршруты, путешествия, поездки
# CUSTOMIZE: sections marked with # CUSTOMIZE

import asyncio
import calendar
import json
import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove,
)
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

# ── CUSTOMIZE ────────────────────────────────────────────────────────────────
BOT_DESCRIPTION = "Менеджер маршрутов и путешествий. Храню перелёты, отели, трансферы, номера броней, цены и предоплаты."
WELCOME_TEXT = (
    "🗺 <b>Менеджер маршрутов</b>\n\n"
    "Систематизирую всю информацию по поездкам:\n"
    "✈️ Перелёты · 🏨 Отели · 🚗 Трансферы\n"
    "🎯 Активности · 💰 Расходы · 📋 Задачи\n\n"
    "Выберите действие:"
)
# ── END CUSTOMIZE ─────────────────────────────────────────────────────────────

BOT_NAME = Path(__file__).stem
DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = str(DATA_DIR / f"{BOT_NAME}_data.db")
EXCEL_PATH = str(DATA_DIR / f"{BOT_NAME}_data.xlsx")
WELCOME_IMAGE = DATA_DIR / "bot_images" / f"{BOT_NAME}.jpg"
ADMINS_FILE = DATA_DIR / f"admins_{BOT_NAME}.json"
TELEGRAPH_API = "https://api.telegra.ph"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
router = Router()

ITEM_TYPES = {
    "flight":   "✈️ Перелёт",
    "hotel":    "🏨 Отель",
    "transfer": "🚗 Трансфер",
    "activity": "🎯 Активность",
    "expense":  "💰 Расход",
    "task":     "📋 Задача",
    "other":    "📌 Другое",
}


# ── admin helpers ─────────────────────────────────────────────────────────────

def _load_admins() -> set:
    try:
        return set(json.loads(ADMINS_FILE.read_text()).get("ids", []))
    except Exception:
        return set()

def _save_admins(ids: set) -> None:
    ADMINS_FILE.write_text(json.dumps({"ids": list(ids)}, ensure_ascii=False))


# ── db ────────────────────────────────────────────────────────────────────────

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS trips (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_prefs (
                user_id        TEXT PRIMARY KEY,
                active_trip_id INTEGER
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS items (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                trip_id     INTEGER NOT NULL REFERENCES trips(id),
                item_type   TEXT DEFAULT 'other',
                title       TEXT NOT NULL,
                destination TEXT,
                date_start  TEXT,
                date_end    TEXT,
                link        TEXT,
                confirm_num TEXT,
                price       REAL,
                prepayment  REAL,
                currency    TEXT DEFAULT 'RUB',
                notes       TEXT,
                remind_at   TEXT,
                status      TEXT DEFAULT 'active',
                created_at  TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.execute(
            "CREATE TABLE IF NOT EXISTS _meta (key TEXT PRIMARY KEY, value TEXT)"
        )
        await db.commit()


# ── trip helpers ──────────────────────────────────────────────────────────────

async def _all_trips() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute("SELECT * FROM trips ORDER BY id DESC")).fetchall()
        return [dict(r) for r in rows]

async def _get_active_trip(user_id: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            "SELECT t.* FROM user_prefs p JOIN trips t ON p.active_trip_id=t.id WHERE p.user_id=?",
            (user_id,)
        )).fetchone()
        if row:
            return dict(row)
        row = await (await db.execute("SELECT * FROM trips ORDER BY id DESC LIMIT 1")).fetchone()
        return dict(row) if row else None

async def _set_active_trip(user_id: str, trip_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO user_prefs (user_id, active_trip_id) VALUES (?,?)",
            (user_id, trip_id)
        )
        await db.commit()

async def _trip_items(trip_id: int, status=None) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if status:
            cur = await db.execute(
                "SELECT * FROM items WHERE trip_id=? AND status=? ORDER BY date_start,id",
                (trip_id, status)
            )
        else:
            cur = await db.execute(
                "SELECT * FROM items WHERE trip_id=? ORDER BY date_start,id", (trip_id,)
            )
        return [dict(r) for r in await cur.fetchall()]


# ── date validation ───────────────────────────────────────────────────────────

def _parse_date(text: str) -> str | None:
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
    if not (2000 <= y <= 2100): return None
    if not (1 <= mo <= 12): return None
    if not (1 <= d <= calendar.monthrange(y, mo)[1]): return None
    return f"{y:04d}-{mo:02d}-{d:02d}"


# ── keyboards ─────────────────────────────────────────────────────────────────

def kb_main() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="➕ Добавить"),    KeyboardButton(text="📋 Маршрут")],
        [KeyboardButton(text="🔍 Поиск"),       KeyboardButton(text="📊 Итоги")],
        [KeyboardButton(text="🗂 Путешествия"), KeyboardButton(text="📥 Excel")],
    ], resize_keyboard=True)

def kb_types() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=v, callback_data=f"itype:{k}")] for k, v in ITEM_TYPES.items()]
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="icancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_skip(step: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⏭ Пропустить", callback_data=f"iskip:{step}"),
        InlineKeyboardButton(text="❌ Отмена",     callback_data="icancel"),
    ]])

def kb_item(item_id: int, status: str = "active") -> InlineKeyboardMarkup:
    if status == "done":
        action_btn = InlineKeyboardButton(text="↩️ Вернуть", callback_data=f"iundone:{item_id}")
    else:
        action_btn = InlineKeyboardButton(text="✅ Выполнено", callback_data=f"idone:{item_id}")
    return InlineKeyboardMarkup(inline_keyboard=[
        [action_btn, InlineKeyboardButton(text="🗑 Удалить", callback_data=f"idel:{item_id}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="iback")],
    ])

def kb_confirm_del(item_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"idel_ok:{item_id}"),
        InlineKeyboardButton(text="◀️ Отмена",      callback_data=f"iview:{item_id}"),
    ]])

def kb_trips(trips: list[dict]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"🗺 {t['name']}", callback_data=f"trip_sel:{t['id']}")] for t in trips]
    rows.append([InlineKeyboardButton(text="➕ Новое путешествие", callback_data="trip_new")])
    rows.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="trip_panel_close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── helpers ───────────────────────────────────────────────────────────────────

def _fmt_price(price, prepay, currency="RUB") -> str:
    sym = {"RUB": "₽", "USD": "$", "EUR": "€", "TRY": "₺"}.get(currency, currency)
    parts = []
    if price:
        parts.append(f"{price:,.0f} {sym}")
    if prepay:
        parts.append(f"предоплата {prepay:,.0f} {sym}")
    return " · ".join(parts) if parts else "—"

def _fmt_item(r: dict) -> str:
    icon = ITEM_TYPES.get(r["item_type"], "📌").split()[0]
    lines = [f"{icon} <b>{r['title']}</b>"]
    if r.get("destination"):  lines.append(f"📍 {r['destination']}")
    dates = " – ".join(filter(None, [r.get("date_start"), r.get("date_end")]))
    if dates: lines.append(f"📅 {dates}")
    p = _fmt_price(r.get("price"), r.get("prepayment"), r.get("currency", "RUB"))
    if p != "—": lines.append(f"💰 {p}")
    if r.get("confirm_num"): lines.append(f"🔖 <code>{r['confirm_num']}</code>")
    if r.get("link"):        lines.append(f"🔗 {r['link']}")
    if r.get("notes"):       lines.append(f"📝 {r['notes']}")
    if r.get("status") == "done": lines.append("✅ Выполнено")
    return "\n".join(lines)


# ── FSM ───────────────────────────────────────────────────────────────────────

class Add(StatesGroup):
    type = State(); title = State(); destination = State()
    date_start = State(); date_end = State(); link = State()
    confirm_num = State(); price = State(); prepayment = State()
    notes = State(); remind = State()

class Search(StatesGroup):
    q = State()

class TripCreate(StatesGroup):
    name = State()

_STEPS = [
    ("destination", Add.destination, "📍 <b>Куда / место</b> (город, адрес):"),
    ("date_start",  Add.date_start,  "📅 <b>Дата начала</b> (например: 25.07.2025):"),
    ("date_end",    Add.date_end,    "📅 <b>Дата окончания</b> (например: 28.07.2025):"),
    ("link",        Add.link,        "🔗 <b>Ссылка на бронирование:</b>"),
    ("confirm_num", Add.confirm_num, "🔖 <b>Номер подтверждения / брони:</b>"),
    ("price",       Add.price,       "💰 <b>Стоимость</b> (только цифры, например 15000):"),
    ("prepayment",  Add.prepayment,  "💳 <b>Предоплата</b> (цифрами, если была):"),
    ("notes",       Add.notes,       "📝 <b>Заметки / пометки:</b>"),
    ("remind",      Add.remind,      "⏰ <b>Напомнить за</b> (например: 1д / 3ч / 30м):"),
]
_STEP_MAP = {s[0]: i for i, s in enumerate(_STEPS)}


# ── /start ────────────────────────────────────────────────────────────────────

@router.message(Command("start"))
async def cmd_start(message: Message):
    admins = _load_admins()
    if not admins:
        _save_admins({str(message.from_user.id)})
    if WELCOME_IMAGE.exists():
        await message.answer_photo(FSInputFile(str(WELCOME_IMAGE)),
                                   caption=WELCOME_TEXT, parse_mode="HTML", reply_markup=kb_main())
    else:
        await message.answer(WELCOME_TEXT, parse_mode="HTML", reply_markup=kb_main())
    if str(message.from_user.id) in _load_admins():
        await message.answer(
            "🔧 <b>Админ:</b> /excel · /publish · /weblink\n"
            "/admins · /addadmin · /removeadmin", parse_mode="HTML"
        )


# ── TRIPS PANEL ───────────────────────────────────────────────────────────────

@router.message(F.text == "🗂 Путешествия")
async def trips_panel(msg: Message):
    trips = await _all_trips()
    if not trips:
        await msg.answer(
            "У вас ещё нет путешествий.\nСоздайте первое!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Новое путешествие", callback_data="trip_new")],
            ])
        )
        return
    active = await _get_active_trip(str(msg.from_user.id))
    active_name = f"🗺 <b>Текущее:</b> {active['name']}\n\n" if active else ""
    await msg.answer(
        active_name + "Выберите путешествие или создайте новое:",
        parse_mode="HTML",
        reply_markup=kb_trips(trips)
    )

@router.callback_query(F.data == "trip_list")
async def cb_trip_list(cb: CallbackQuery):
    await cb.answer()
    trips = await _all_trips()
    active = await _get_active_trip(str(cb.from_user.id))
    active_name = f"🗺 <b>Текущее:</b> {active['name']}\n\n" if active else ""
    await cb.message.edit_text(
        active_name + "Выберите путешествие:",
        parse_mode="HTML",
        reply_markup=kb_trips(trips)
    )

@router.callback_query(F.data.startswith("trip_sel:"))
async def cb_trip_sel(cb: CallbackQuery):
    await cb.answer()
    trip_id = int(cb.data.split(":")[1])
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute("SELECT name FROM trips WHERE id=?", (trip_id,))).fetchone()
    if not row:
        await cb.message.edit_text("Путешествие не найдено."); return
    await _set_active_trip(str(cb.from_user.id), trip_id)
    await cb.message.edit_text(f"✅ Активное путешествие: <b>{row[0]}</b>", parse_mode="HTML")
    await cb.message.answer("Что сделаем?", reply_markup=kb_main())

@router.callback_query(F.data == "trip_new")
async def cb_trip_new(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(TripCreate.name)
    await cb.message.edit_text(
        "✏️ <b>Введите название путешествия</b>\n"
        "Например: Турция 2025, Командировка Москва, Отпуск в Сочи:",
        parse_mode="HTML"
    )

@router.message(TripCreate.name, F.text)
async def trip_create_name(msg: Message, state: FSMContext):
    name = msg.text.strip()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("INSERT INTO trips (name) VALUES (?)", (name,))
        trip_id = cur.lastrowid
        await db.commit()
    await _set_active_trip(str(msg.from_user.id), trip_id)
    await state.clear()
    await msg.answer(
        f"✅ Создано путешествие <b>{name}</b>!\n"
        f"Теперь добавляйте пункты маршрута — нажмите ➕ Добавить.",
        parse_mode="HTML", reply_markup=kb_main()
    )

@router.callback_query(F.data.startswith("trip_del:"))
async def cb_trip_del(cb: CallbackQuery):
    await cb.answer()
    trip_id = int(cb.data.split(":")[1])
    async with aiosqlite.connect(DB_PATH) as db:
        cnt = (await (await db.execute("SELECT COUNT(*) FROM items WHERE trip_id=?", (trip_id,))).fetchone())[0]
        row = await (await db.execute("SELECT name FROM trips WHERE id=?", (trip_id,))).fetchone()
    if not row:
        await cb.message.edit_text("Путешествие не найдено."); return
    name = row[0]
    await cb.message.edit_text(
        f"🗑 Удалить путешествие <b>{name}</b>?\n"
        f"Вместе с ним будет удалено <b>{cnt} пунктов</b> маршрута.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"trip_del_ok:{trip_id}"),
             InlineKeyboardButton(text="◀️ Отмена",      callback_data="trip_list")],
        ])
    )

@router.callback_query(F.data.startswith("trip_del_ok:"))
async def cb_trip_del_ok(cb: CallbackQuery):
    await cb.answer()
    trip_id = int(cb.data.split(":")[1])
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute("SELECT name FROM trips WHERE id=?", (trip_id,))).fetchone()
        await db.execute("DELETE FROM items WHERE trip_id=?", (trip_id,))
        await db.execute("DELETE FROM trips WHERE id=?", (trip_id,))
        await db.execute("DELETE FROM user_prefs WHERE active_trip_id=?", (trip_id,))
        await db.commit()
    name = row[0] if row else "—"
    await cb.message.edit_text(f"🗑 Путешествие <b>{name}</b> удалено.", parse_mode="HTML")
    await cb.message.answer("Что сделаем?", reply_markup=kb_main())

@router.callback_query(F.data == "trip_panel_close")
async def cb_trip_close(cb: CallbackQuery):
    await cb.answer()
    await cb.message.delete()


# ── ADD FLOW ──────────────────────────────────────────────────────────────────

@router.message(F.text == "➕ Добавить")
async def add_start(message: Message, state: FSMContext):
    trip = await _get_active_trip(str(message.from_user.id))
    if not trip:
        await message.answer(
            "Сначала создайте путешествие — нажмите 🗂 Путешествия.",
            reply_markup=kb_main()
        )
        return
    await state.update_data(trip_id=trip["id"])
    await state.set_state(Add.type)
    await message.answer(f"🗺 <b>{trip['name']}</b>\n\nВыберите тип:", parse_mode="HTML", reply_markup=kb_types())

@router.callback_query(F.data.startswith("itype:"))
async def cb_type(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    itype = cb.data.split(":")[1]
    await state.update_data(item_type=itype)
    await state.set_state(Add.title)
    await cb.message.edit_text(
        f"{ITEM_TYPES[itype]}\n\n✏️ <b>Название</b> (например: «Рейс SU1234», «Hilton Istanbul»):",
        parse_mode="HTML"
    )

@router.message(Add.title, F.text)
async def add_title(msg: Message, state: FSMContext):
    await state.update_data(title=msg.text.strip())
    await state.set_state(Add.destination)
    await msg.answer("📍 <b>Куда / место</b> (город, адрес):",
                     parse_mode="HTML", reply_markup=kb_skip("destination"))

@router.message(Add.destination, F.text, ~F.text.startswith("/"))
async def add_destination(msg: Message, state: FSMContext):
    await state.update_data(destination=msg.text.strip())
    await state.set_state(Add.date_start)
    await msg.answer("📅 <b>Дата начала</b> (например: 25.07.2025):",
                     parse_mode="HTML", reply_markup=kb_skip("date_start"))

@router.message(Add.date_start, F.text, ~F.text.startswith("/"))
async def add_date_start(msg: Message, state: FSMContext):
    parsed = _parse_date(msg.text)
    if parsed is None:
        await msg.answer("❌ Неверный формат даты. Введите, например: <b>25.07.2025</b>", parse_mode="HTML")
        return
    await state.update_data(date_start=parsed)
    await state.set_state(Add.date_end)
    await msg.answer("📅 <b>Дата окончания</b> (например: 28.07.2025):",
                     parse_mode="HTML", reply_markup=kb_skip("date_end"))

@router.message(Add.date_end, F.text, ~F.text.startswith("/"))
async def add_date_end(msg: Message, state: FSMContext):
    parsed = _parse_date(msg.text)
    if parsed is None:
        await msg.answer("❌ Неверный формат даты. Введите, например: <b>28.07.2025</b>", parse_mode="HTML")
        return
    await state.update_data(date_end=parsed)
    await state.set_state(Add.link)
    await msg.answer("🔗 <b>Ссылка на бронирование:</b>",
                     parse_mode="HTML", reply_markup=kb_skip("link"))

@router.message(Add.link, F.text, ~F.text.startswith("/"))
async def add_link(msg: Message, state: FSMContext):
    await state.update_data(link=msg.text.strip())
    await state.set_state(Add.confirm_num)
    await msg.answer("🔖 <b>Номер подтверждения / брони:</b>",
                     parse_mode="HTML", reply_markup=kb_skip("confirm_num"))

@router.message(Add.confirm_num, F.text, ~F.text.startswith("/"))
async def add_confirm(msg: Message, state: FSMContext):
    await state.update_data(confirm_num=msg.text.strip())
    await state.set_state(Add.price)
    await msg.answer("💰 <b>Стоимость</b> (только цифры, например 15000):",
                     parse_mode="HTML", reply_markup=kb_skip("price"))

@router.message(Add.price, F.text, ~F.text.startswith("/"))
async def add_price(msg: Message, state: FSMContext):
    cleaned = re.sub(r"[^\d.]", "", msg.text)
    try:
        await state.update_data(price=float(cleaned))
    except ValueError:
        await msg.answer("Введите число, например: 15000"); return
    await state.set_state(Add.prepayment)
    await msg.answer("💳 <b>Предоплата</b> (цифрами, если была):",
                     parse_mode="HTML", reply_markup=kb_skip("prepayment"))

@router.message(Add.prepayment, F.text, ~F.text.startswith("/"))
async def add_prepay(msg: Message, state: FSMContext):
    cleaned = re.sub(r"[^\d.]", "", msg.text)
    try:
        await state.update_data(prepayment=float(cleaned))
    except ValueError:
        await msg.answer("Введите число"); return
    await state.set_state(Add.notes)
    await msg.answer("📝 <b>Заметки / пометки:</b>",
                     parse_mode="HTML", reply_markup=kb_skip("notes"))

@router.message(Add.notes, F.text, ~F.text.startswith("/"))
async def add_notes(msg: Message, state: FSMContext):
    await state.update_data(notes=msg.text.strip())
    await state.set_state(Add.remind)
    await msg.answer("⏰ <b>Напомнить за</b> (например: 1д / 3ч / 30м):",
                     parse_mode="HTML", reply_markup=kb_skip("remind"))

@router.message(Add.remind, F.text, ~F.text.startswith("/"))
async def add_remind(msg: Message, state: FSMContext, bot: Bot):
    await state.update_data(remind_raw=msg.text.strip())
    await _finalize(msg, state, bot)


async def _finalize(msg: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    remind_raw = data.pop("remind_raw", None)
    remind_at = None
    date_start = data.get("date_start")
    if remind_raw and date_start:
        try:
            d = datetime.strptime(date_start, "%Y-%m-%d")
            m = re.match(r"(\d+)([дdhHчcm])", remind_raw.lower())
            if m:
                n, u = int(m.group(1)), m.group(2)
                delta = timedelta(days=n) if u in "дd" else (timedelta(hours=n) if u in "чh" else timedelta(minutes=n))
                remind_at = (d - delta).isoformat()
        except Exception:
            pass

    trip_id = data.get("trip_id")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO items (trip_id,item_type,title,destination,date_start,date_end,link,confirm_num,price,prepayment,notes,remind_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (trip_id, data.get("item_type", "other"), data.get("title", ""), data.get("destination"),
             data.get("date_start"), data.get("date_end"), data.get("link"),
             data.get("confirm_num"), data.get("price"), data.get("prepayment"),
             data.get("notes"), remind_at)
        )
        await db.commit()

    await state.clear()
    icon = ITEM_TYPES.get(data.get("item_type", "other"), "📌").split()[0]
    lines = [f"✅ <b>Сохранено!</b>\n", f"{icon} {data.get('title', '')}"]
    if data.get("destination"): lines.append(f"📍 {data['destination']}")
    if data.get("date_start"):  lines.append(f"📅 {data['date_start']}")
    if data.get("price"):       lines.append(f"💰 {data['price']:,.0f} ₽")
    await msg.answer("\n".join(lines), parse_mode="HTML", reply_markup=kb_main())
    if remind_at:
        asyncio.create_task(_remind(bot, msg.chat.id, data.get("title", ""), remind_at))


async def _remind(bot: Bot, chat_id: int, title: str, remind_at: str):
    try:
        delay = (datetime.fromisoformat(remind_at) - datetime.now()).total_seconds()
        if delay > 0:
            await asyncio.sleep(delay)
        await bot.send_message(chat_id, f"⏰ <b>Напоминание:</b> {title}", parse_mode="HTML")
    except Exception as e:
        logger.error(f"Reminder error: {e}")


# ── SKIP ──────────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("iskip:"))
async def cb_skip(cb: CallbackQuery, state: FSMContext, bot: Bot):
    await cb.answer()
    step = cb.data.split(":")[1]
    if step == "remind":
        await _finalize(cb.message, state, bot)
        return
    idx = _STEP_MAP.get(step, -1)
    if idx + 1 < len(_STEPS):
        _, next_state, prompt = _STEPS[idx + 1]
        await state.set_state(next_state)
        next_step = _STEPS[idx + 1][0]
        await cb.message.edit_text(prompt, parse_mode="HTML",
                                   reply_markup=kb_skip(next_step))

@router.callback_query(F.data == "icancel")
async def cb_icancel(cb: CallbackQuery, state: FSMContext):
    await cb.answer(); await state.clear()
    await cb.message.edit_text("Отменено.")
    await cb.message.answer("Что сделаем?", reply_markup=kb_main())


# ── ROUTE VIEW ────────────────────────────────────────────────────────────────

@router.message(F.text == "📋 Маршрут")
async def show_route(msg: Message):
    trip = await _get_active_trip(str(msg.from_user.id))
    if not trip:
        await msg.answer("Сначала создайте путешествие — нажмите 🗂 Путешествия.", reply_markup=kb_main()); return
    items = await _trip_items(trip["id"], "active")
    if not items:
        await msg.answer(f"🗺 <b>{trip['name']}</b>\n\nМаршрут пуст. Нажмите ➕ Добавить.",
                         parse_mode="HTML", reply_markup=kb_main()); return

    by_date: dict[str, list] = {}
    no_date = []
    for item in items:
        d = item.get("date_start") or ""
        (by_date.setdefault(d, []) if d else no_date).append(item)

    lines = [f"🗺 <b>{trip['name']}</b>\n"]
    for d in sorted(by_date):
        try:
            dt = datetime.strptime(d, "%Y-%m-%d")
            lines.append(f"\n<b>📅 {dt.strftime('%d.%m.%Y')}</b>")
        except Exception:
            lines.append(f"\n<b>📅 {d}</b>")
        for i in by_date[d]:
            icon = ITEM_TYPES.get(i["item_type"], "📌").split()[0]
            line = f"  {icon} {i['title']}"
            p = _fmt_price(i.get("price"), i.get("prepayment"))
            if p != "—": line += f" · {p}"
            if i.get("confirm_num"): line += f"\n     🔖 <code>{i['confirm_num']}</code>"
            lines.append(line)
    if no_date:
        lines.append("\n<b>📌 Без даты</b>")
        for i in no_date:
            lines.append(f"  {ITEM_TYPES.get(i['item_type'], '📌').split()[0]} {i['title']}")

    total_price = sum(i.get("price") or 0 for i in items)
    total_pre   = sum(i.get("prepayment") or 0 for i in items)
    lines.append(f"\n💼 Итого: {len(items)} пунктов")
    if total_price:
        lines.append(f"💰 Сумма: {total_price:,.0f} ₽  |  Предоплачено: {total_pre:,.0f} ₽")
        lines.append(f"📌 Осталось оплатить: {total_price - total_pre:,.0f} ₽")

    btn_rows = [[InlineKeyboardButton(
        text=f"{ITEM_TYPES.get(i['item_type'], '📌').split()[0]} {i['title'][:28]}",
        callback_data=f"iview:{i['id']}"
    )] for i in items[:20]]
    text = "\n".join(lines)
    if len(text) > 4000: text = text[:3900] + "\n…"
    await msg.answer(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=btn_rows))


@router.callback_query(F.data.startswith("iview:"))
async def cb_view(cb: CallbackQuery):
    await cb.answer()
    item_id = int(cb.data.split(":")[1])
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute("SELECT * FROM items WHERE id=?", (item_id,))).fetchone()
    if not row:
        await cb.message.answer("Не найдено."); return
    r = dict(row)
    await cb.message.answer(_fmt_item(r), parse_mode="HTML", reply_markup=kb_item(item_id, r.get("status", "active")))

@router.callback_query(F.data.startswith("idel:"))
async def cb_del(cb: CallbackQuery):
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=kb_confirm_del(int(cb.data.split(":")[1])))

@router.callback_query(F.data.startswith("idel_ok:"))
async def cb_del_ok(cb: CallbackQuery):
    await cb.answer()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM items WHERE id=?", (int(cb.data.split(":")[1]),))
        await db.commit()
    await cb.message.edit_text("🗑 Удалено.")

@router.callback_query(F.data.startswith("idone:"))
async def cb_done(cb: CallbackQuery):
    await cb.answer()
    item_id = int(cb.data.split(":")[1])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE items SET status='done' WHERE id=?", (item_id,))
        await db.commit()
    await cb.message.edit_text("✅ Отмечено как выполнено!", reply_markup=kb_item(item_id, "done"))

@router.callback_query(F.data.startswith("iundone:"))
async def cb_undone(cb: CallbackQuery):
    await cb.answer()
    item_id = int(cb.data.split(":")[1])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE items SET status='active' WHERE id=?", (item_id,))
        await db.commit()
    await cb.message.edit_text("↩️ Возвращено в активные.", reply_markup=kb_item(item_id, "active"))

@router.callback_query(F.data == "iback")
async def cb_iback(cb: CallbackQuery):
    await cb.answer(); await cb.message.delete()


# ── SEARCH ────────────────────────────────────────────────────────────────────

@router.message(F.text == "🔍 Поиск")
async def search_start(msg: Message, state: FSMContext):
    await state.set_state(Search.q)
    await msg.answer("🔍 Введите слово (название, город, номер брони):", reply_markup=ReplyKeyboardRemove())

@router.message(Search.q, F.text)
async def search_do(msg: Message, state: FSMContext):
    await state.clear()
    trip = await _get_active_trip(str(msg.from_user.id))
    q = f"%{msg.text.strip()}%"
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if trip:
            rows = await (await db.execute(
                "SELECT * FROM items WHERE trip_id=? AND (title LIKE ? OR destination LIKE ? OR confirm_num LIKE ? OR notes LIKE ?)",
                (trip["id"], q, q, q, q)
            )).fetchall()
        else:
            rows = await (await db.execute(
                "SELECT * FROM items WHERE title LIKE ? OR destination LIKE ? OR confirm_num LIKE ? OR notes LIKE ?",
                (q, q, q, q)
            )).fetchall()
    if not rows:
        await msg.answer("Ничего не найдено.", reply_markup=kb_main()); return
    lines = [f"🔍 Найдено: {len(rows)}\n"]
    for r in [dict(x) for x in rows[:10]]:
        icon = ITEM_TYPES.get(r["item_type"], "📌").split()[0]
        lines.append(f"{icon} <b>{r['title']}</b>")
        if r.get("date_start"): lines.append(f"   📅 {r['date_start']}")
        if r.get("confirm_num"): lines.append(f"   🔖 <code>{r['confirm_num']}</code>")
    await msg.answer("\n".join(lines), parse_mode="HTML", reply_markup=kb_main())


# ── STATS ─────────────────────────────────────────────────────────────────────

@router.message(F.text == "📊 Итоги")
async def show_stats(msg: Message):
    trips = await _all_trips()
    if not trips:
        await msg.answer("Нет путешествий. Нажмите 🗂 Путешествия чтобы создать."); return
    if len(trips) == 1:
        await _show_trip_stats(msg, trips[0])
        return
    rows = [[InlineKeyboardButton(text=f"🗺 {t['name']}", callback_data=f"stats_trip:{t['id']}")] for t in trips]
    await msg.answer("📊 Выберите путешествие для итогов:",
                     reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

@router.callback_query(F.data.startswith("stats_trip:"))
async def cb_stats_trip(cb: CallbackQuery):
    await cb.answer()
    trip_id = int(cb.data.split(":")[1])
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute("SELECT * FROM trips WHERE id=?", (trip_id,))).fetchone()
    if not row:
        await cb.message.edit_text("Путешествие не найдено."); return
    await cb.message.delete()
    await _show_trip_stats(cb.message, dict(row))

async def _show_trip_stats(msg: Message, trip: dict):
    async with aiosqlite.connect(DB_PATH) as db:
        total = (await (await db.execute(
            "SELECT COUNT(*) FROM items WHERE trip_id=?", (trip["id"],)
        )).fetchone())[0]
        done = (await (await db.execute(
            "SELECT COUNT(*) FROM items WHERE trip_id=? AND status='done'", (trip["id"],)
        )).fetchone())[0]
        cost = (await (await db.execute(
            "SELECT COALESCE(SUM(price),0) FROM items WHERE trip_id=? AND status='active'", (trip["id"],)
        )).fetchone())[0]
        paid = (await (await db.execute(
            "SELECT COALESCE(SUM(prepayment),0) FROM items WHERE trip_id=? AND status='active'", (trip["id"],)
        )).fetchone())[0]
        by_type = await (await db.execute(
            "SELECT item_type, COUNT(*), COALESCE(SUM(price),0) FROM items WHERE trip_id=? GROUP BY item_type",
            (trip["id"],)
        )).fetchall()
    lines = [f"📊 <b>Итоги: {trip['name']}</b>\n",
             f"📋 Пунктов: {total} (выполнено: {done})"]
    if cost:
        lines += [f"💰 Общая стоимость: {cost:,.0f} ₽",
                  f"💳 Оплачено: {paid:,.0f} ₽",
                  f"📌 Осталось оплатить: {cost - paid:,.0f} ₽"]
    lines.append("\n<b>По типам:</b>")
    for itype, cnt, ttl in by_type:
        label = ITEM_TYPES.get(itype, itype)
        lines.append(f"  {label}: {cnt}" + (f" · {ttl:,.0f} ₽" if ttl else ""))
    await msg.answer("\n".join(lines), parse_mode="HTML", reply_markup=kb_main())


# ── EXCEL ─────────────────────────────────────────────────────────────────────

@router.message(F.text == "📥 Excel")
@router.message(Command("excel"))
async def cmd_excel(msg: Message):
    if str(msg.from_user.id) not in _load_admins():
        await msg.answer("⛔ Нет доступа"); return
    trips = await _all_trips()
    if not trips:
        await msg.answer("Данных нет."); return
    wb = Workbook()
    wb.remove(wb.active)
    hdrs = ["Тип", "Название", "Куда", "Дата нач.", "Дата кон.", "Ссылка",
            "Подтверждение", "Цена", "Предоплата", "Заметки", "Статус"]
    hfill = PatternFill("solid", fgColor="1F4E79")
    hfont = Font(bold=True, color="FFFFFF")
    border = Border(left=Side(style="thin"), right=Side(style="thin"),
                    top=Side(style="thin"),  bottom=Side(style="thin"))
    fills = [PatternFill("solid", fgColor="FFFFFF"), PatternFill("solid", fgColor="DCE6F1")]
    total_fill = PatternFill("solid", fgColor="FFF2CC")
    total_font = Font(bold=True)

    for trip in trips:
        items = await _trip_items(trip["id"])
        ws = wb.create_sheet(title=trip["name"][:31])
        for c, h in enumerate(hdrs, 1):
            cell = ws.cell(1, c, h); cell.fill = hfill; cell.font = hfont; cell.border = border
            cell.alignment = Alignment(horizontal="center")
        total_price = 0; total_pre = 0
        for ri, item in enumerate(items, 2):
            vals = [ITEM_TYPES.get(item["item_type"], ""), item["title"],
                    item.get("destination", ""), item.get("date_start", ""), item.get("date_end", ""),
                    item.get("link", ""), item.get("confirm_num", ""),
                    item.get("price"), item.get("prepayment"), item.get("notes", ""), item.get("status", "")]
            for c, v in enumerate(vals, 1):
                cell = ws.cell(ri, c, v); cell.fill = fills[ri % 2]; cell.border = border
            total_price += item.get("price") or 0
            total_pre   += item.get("prepayment") or 0
        tr = len(items) + 2
        ws.cell(tr, 1, "ИТОГО").font = total_font; ws.cell(tr, 1).fill = total_fill
        ws.cell(tr, 8, total_price).font = total_font; ws.cell(tr, 8).fill = total_fill
        ws.cell(tr, 9, total_pre).font = total_font; ws.cell(tr, 9).fill = total_fill
        ws.cell(tr, 11, f"Осталось: {total_price - total_pre:,.0f} ₽").font = total_font
        ws.cell(tr, 11).fill = total_fill
        for col in ws.columns:
            ws.column_dimensions[col[0].column_letter].width = min(
                max(max(len(str(c.value or "")) for c in col) + 2, 10), 40
            )
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = f"A1:K{len(items) + 1}"

    wb.save(EXCEL_PATH)
    await msg.answer_document(FSInputFile(EXCEL_PATH),
                              caption=f"📊 Таблица маршрутов ({len(trips)} путешествий)")


# ── TELEGRAPH ─────────────────────────────────────────────────────────────────

async def _tg_token() -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute("SELECT value FROM _meta WHERE key='tg_token'")).fetchone()
    if row: return row[0]
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{TELEGRAPH_API}/createAccount",
                          data={"short_name": BOT_NAME[:31], "author_name": "TripBot"}) as r:
            token = (await r.json())["result"]["access_token"]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO _meta VALUES ('tg_token',?)", (token,)); await db.commit()
    return token

def _tg_nodes_items(trip_name: str, items: list[dict]) -> list:
    nodes = [{"tag": "h3", "children": [trip_name]}]
    for item in items:
        icon = ITEM_TYPES.get(item["item_type"], "📌").split()[0]
        parts = [f"{icon} {item['title']}"]
        if item.get("destination"): parts.append(f"| {item['destination']}")
        if item.get("date_start"):  parts.append(f"| {item['date_start']}")
        if item.get("date_end") and item.get("date_end") != item.get("date_start"):
            parts.append(f"– {item['date_end']}")
        p = _fmt_price(item.get("price"), item.get("prepayment"))
        if p != "—": parts.append(f"| {p}")
        if item.get("confirm_num"): parts.append(f"| #{item['confirm_num']}")
        if item.get("notes"):       parts.append(f"| {item['notes']}")
        nodes.append({"tag": "p", "children": [" ".join(parts)]})
    total_price = sum(i.get("price") or 0 for i in items)
    total_pre   = sum(i.get("prepayment") or 0 for i in items)
    if total_price:
        nodes.append({"tag": "p", "children": [
            f"Итого: {total_price:,.0f} ₽  |  Оплачено: {total_pre:,.0f} ₽  |  "
            f"Осталось оплатить: {total_price - total_pre:,.0f} ₽"
        ]})
    return nodes

async def _publish_trip(trip: dict, items: list[dict]) -> str:
    token = await _tg_token()
    nodes = _tg_nodes_items(trip["name"], items)
    key_path = f"tg_path_{trip['id']}"
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute("SELECT value FROM _meta WHERE key=?", (key_path,))).fetchone()
    page_path = row[0] if row else None
    async with aiohttp.ClientSession() as s:
        ep = f"{TELEGRAPH_API}/{'editPage/' + page_path if page_path else 'createPage'}"
        result = (await (await s.post(ep, json={
            "access_token": token,
            "title": f"Маршрут: {trip['name']}"[:256],
            "content": nodes,
            "return_content": False
        })).json())["result"]
    if not page_path:
        page_path = result["path"]
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT OR REPLACE INTO _meta VALUES (?,?)", (key_path, page_path))
            await db.commit()
    return f"https://telegra.ph/{page_path}"

@router.message(Command("publish"))
async def cmd_publish(msg: Message):
    if str(msg.from_user.id) not in _load_admins():
        await msg.answer("⛔ Нет доступа"); return
    trip = await _get_active_trip(str(msg.from_user.id))
    if not trip:
        await msg.answer("Нет активного путешествия."); return
    status_msg = await msg.answer("⏳ Публикую...")
    items = await _trip_items(trip["id"])
    if not items:
        await status_msg.edit_text("Данных нет."); return
    url = await _publish_trip(trip, items)
    await status_msg.edit_text(
        f"✅ <b>Опубликовано!</b>\n\n🔗 {url}\n\nОбновить: /publish", parse_mode="HTML"
    )

@router.message(Command("weblink"))
async def cmd_weblink(msg: Message):
    trip = await _get_active_trip(str(msg.from_user.id))
    if not trip:
        await msg.answer("Нет активного путешествия."); return
    key_path = f"tg_path_{trip['id']}"
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute("SELECT value FROM _meta WHERE key=?", (key_path,))).fetchone()
    if row:
        url = f"https://telegra.ph/{row[0]}"
        await msg.answer(f"🔗 <b>Онлайн-маршрут:</b>\n{url}\n\nОбновить: /publish", parse_mode="HTML")
    else:
        await msg.answer("Нажмите /publish для первой публикации.")


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


# ── GROUP SUPPORT ─────────────────────────────────────────────────────────────

BOT_DISPLAY_NAME = os.getenv("BOT_DISPLAY_NAME", "").strip()
GROUP_CHAT_ID    = os.getenv("GROUP_CHAT_ID", "").strip()

@router.message(F.chat.type.in_({"group", "supergroup"}), F.text)
async def handle_group_mention(msg: Message, bot: Bot):
    if not BOT_DISPLAY_NAME or not msg.text: return
    if msg.from_user and msg.from_user.is_bot: return
    if BOT_DISPLAY_NAME.lower() not in msg.text.lower(): return
    from anthropic import AsyncAnthropic as _C
    resp = await _C(api_key=os.getenv("ANTHROPIC_API_KEY", "")).messages.create(
        model="claude-haiku-4-5-20251001", max_tokens=300,
        system=f"Ты — {BOT_DISPLAY_NAME}, менеджер маршрутов. Кратко ответь.",
        messages=[{"role": "user", "content": msg.text}]
    )
    await msg.reply(resp.content[0].text)


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
