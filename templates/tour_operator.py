# TEMPLATE: tour_operator
# USE FOR: профессиональный тур-оператор, коммерческие туры, управление группами
# STANDALONE: полноценный CRM — Telegram бот + aiohttp веб-приложение

import asyncio
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path

import aiohttp
import aiosqlite
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN      = os.getenv("BOT_TOKEN", "")
ANTHROPIC_KEY  = os.getenv("ANTHROPIC_API_KEY", "")
ASSEMBLYAI_KEY = os.getenv("ASSEMBLYAI_API_KEY", "")
PORT           = int(os.getenv("PORT", "8080"))
DATA_DIR       = Path(os.getenv("DATA_DIR", "./data"))
DATA_DIR.mkdir(exist_ok=True)

BOT_NAME      = Path(__file__).stem
DB_PATH       = str(DATA_DIR / f"{BOT_NAME}.db")
ADMINS_FILE   = str(DATA_DIR / f"admins_{BOT_NAME}.json")
WELCOME_IMAGE = DATA_DIR / "bot_images" / f"{BOT_NAME}.jpg"

RAILWAY_DOMAIN = os.getenv("RAILWAY_PUBLIC_DOMAIN", "")
BASE_URL       = f"https://{RAILWAY_DOMAIN}" if RAILWAY_DOMAIN else f"http://localhost:{PORT}"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
router = Router()

# ── Admin ─────────────────────────────────────────────────────────────────────
def _load_admins():
    if not os.path.exists(ADMINS_FILE):
        return []
    try:
        with open(ADMINS_FILE) as f:
            return json.load(f)
    except Exception:
        return []

def _save_admins(lst):
    with open(ADMINS_FILE, "w") as f:
        json.dump(lst, f)

def _is_admin(uid):
    admins = _load_admins()
    if not admins:
        _save_admins([uid])
        return True
    return uid in admins

# ── Database ──────────────────────────────────────────────────────────────────
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS tours (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                name         TEXT NOT NULL,
                destination  TEXT,
                date_start   TEXT,
                date_end     TEXT,
                guests_count INTEGER DEFAULT 0,
                status       TEXT DEFAULT 'planning',
                created_at   TEXT DEFAULT (datetime('now','localtime'))
            );
            CREATE TABLE IF NOT EXISTS program (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                tour_id        INTEGER,
                day_num        INTEGER,
                date           TEXT,
                time           TEXT,
                title          TEXT NOT NULL,
                emoji          TEXT DEFAULT '🔥',
                cost_fixed     REAL DEFAULT 0,
                cost_variable  REAL DEFAULT 0,
                cost_team      REAL DEFAULT 0,
                cost_extra     REAL DEFAULT 0,
                tasks          TEXT,
                contractor_req TEXT,
                created_at     TEXT DEFAULT (datetime('now','localtime'))
            );
            CREATE TABLE IF NOT EXISTS locations (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                tour_id    INTEGER,
                region     TEXT,
                category   TEXT,
                status     TEXT DEFAULT '2️⃣',
                name       TEXT NOT NULL,
                hours      TEXT,
                cost       TEXT,
                notes      TEXT,
                maps_link  TEXT,
                contacts   TEXT,
                website    TEXT,
                youtube    TEXT,
                instagram  TEXT,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            );
            CREATE TABLE IF NOT EXISTS hotels (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                tour_id      INTEGER,
                region       TEXT,
                name         TEXT NOT NULL,
                rating       REAL,
                rooms_info   TEXT,
                booking_cost TEXT,
                our_cost     TEXT,
                notes        TEXT,
                contacts     TEXT,
                maps_link    TEXT,
                booking_link TEXT,
                status       TEXT DEFAULT '2️⃣',
                created_at   TEXT DEFAULT (datetime('now','localtime'))
            );
            CREATE TABLE IF NOT EXISTS guests (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                tour_id    INTEGER,
                name       TEXT NOT NULL,
                total_cost REAL DEFAULT 0,
                prepaid    REAL DEFAULT 0,
                our_price  REAL DEFAULT 0,
                status     TEXT DEFAULT 'not_paid',
                notes      TEXT,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            );
            CREATE TABLE IF NOT EXISTS dds (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                tour_id     INTEGER,
                date        TEXT,
                amount_rub  REAL DEFAULT 0,
                amount_usd  REAL DEFAULT 0,
                amount_idr  REAL DEFAULT 0,
                description TEXT,
                entity      TEXT,
                type        TEXT DEFAULT 'out',
                created_at  TEXT DEFAULT (datetime('now','localtime'))
            );
            CREATE TABLE IF NOT EXISTS user_prefs (
                user_id        TEXT PRIMARY KEY,
                active_tour_id INTEGER
            );
        """)
        await db.commit()

async def get_active_tour(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT t.* FROM tours t JOIN user_prefs u ON t.id=u.active_tour_id WHERE u.user_id=?",
            (str(uid),),
        ) as c:
            row = await c.fetchone()
            return dict(row) if row else None

async def set_active_tour(uid, tid):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO user_prefs(user_id,active_tour_id) VALUES(?,?)"
            " ON CONFLICT(user_id) DO UPDATE SET active_tour_id=excluded.active_tour_id",
            (str(uid), tid),
        )
        await db.commit()

# ── Voice → text (AssemblyAI REST) ───────────────────────────────────────────
async def transcribe_voice(bot, voice):
    tg_file = await bot.get_file(voice.file_id)
    audio_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{tg_file.file_path}"
    async with aiohttp.ClientSession() as s:
        async with s.get(audio_url) as r:
            audio_bytes = await r.read()
        async with s.post(
            "https://api.assemblyai.com/v2/upload",
            headers={"authorization": ASSEMBLYAI_KEY, "content-type": "audio/ogg"},
            data=audio_bytes,
        ) as r:
            up = await r.json()
        upload_url = up.get("upload_url", "")
        if not upload_url:
            return ""
        async with s.post(
            "https://api.assemblyai.com/v2/transcript",
            headers={"authorization": ASSEMBLYAI_KEY, "content-type": "application/json"},
            json={"audio_url": upload_url, "language_code": "ru"},
        ) as r:
            tx = await r.json()
        tx_id = tx.get("id", "")
        if not tx_id:
            return ""
        for _ in range(60):
            await asyncio.sleep(3)
            async with s.get(
                f"https://api.assemblyai.com/v2/transcript/{tx_id}",
                headers={"authorization": ASSEMBLYAI_KEY},
            ) as r:
                res = await r.json()
            if res.get("status") == "completed":
                return res.get("text", "")
            if res.get("status") == "error":
                return ""
    return ""

# ── Text → structured data (Claude) ──────────────────────────────────────────
_PARSE_PROMPT = """Ты помощник менеджера туров. Разбери текст и верни JSON.

Тип записи:
- location (ЛиП): name, region, category, status(✅/2️⃣/❗/❌/—), hours, cost, notes, maps_link, contacts, website, youtube, instagram
- program: day_num(int), date(YYYY-MM-DD), time(HH:MM), title, emoji, cost_fixed, cost_variable, cost_team, cost_extra, tasks, contractor_req
- hotel: name, region, rating(float 1-5), rooms_info, booking_cost, our_cost, notes, contacts, maps_link, booking_link, status
- guest: name, total_cost, prepaid, our_price, status(not_paid/partial/paid/refund), notes
- dds: date(YYYY-MM-DD), amount_rub, amount_usd, amount_idr, description, entity, type(in/out)
- unknown

Ответ ТОЛЬКО JSON без пояснений:
{"type":"...","data":{...},"confidence":0.0}

Текст: {text}"""

async def parse_with_claude(text):
    async with aiohttp.ClientSession() as s:
        async with s.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 512,
                "messages": [{"role": "user", "content": _PARSE_PROMPT.format(text=text)}],
            },
        ) as r:
            res = await r.json()
    try:
        raw = res["content"][0]["text"]
        raw = re.sub(r"^```(?:json)?\n?", "", raw.strip())
        raw = re.sub(r"\n?```$", "", raw.strip())
        return json.loads(raw)
    except Exception:
        return {"type": "unknown", "data": {}, "confidence": 0}

# ── FSM States ────────────────────────────────────────────────────────────────
class NewTour(StatesGroup):
    name        = State()
    destination = State()
    dates       = State()

class VoicePend(StatesGroup):
    confirm = State()

# ── Keyboards ─────────────────────────────────────────────────────────────────
def mk(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)

def btn(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)

def main_kb():
    return mk([
        [btn("🌍 Мои туры", "tours_list"), btn("➕ Новый тур", "new_tour")],
        [btn("🌐 Веб-приложение", "open_app")],
        [btn("📅 Программа", "sec_program"), btn("📍 ЛиП", "sec_lip")],
        [btn("🏨 Отели", "sec_hotels"),     btn("👥 Гости", "sec_guests")],
        [btn("💰 ДДС", "sec_dds")],
    ])

# ── Voice helpers ─────────────────────────────────────────────────────────────
_TYPE_ICON = {"location": "📍", "program": "📅", "hotel": "🏨", "guest": "👥", "dds": "💰"}
_TYPE_NAME = {"location": "ЛиП", "program": "Программа", "hotel": "Отель",
              "guest": "Гость", "dds": "ДДС", "unknown": "Неизвестно"}
_FIELD_LBL = {
    "name": "Название", "region": "Регион", "category": "Категория", "status": "Статус",
    "hours": "Часы", "cost": "Стоимость", "notes": "Заметки", "maps_link": "Карта",
    "contacts": "Контакты", "website": "Сайт", "youtube": "YouTube", "instagram": "Instagram",
    "day_num": "День", "date": "Дата", "time": "Время", "title": "Название", "emoji": "Эмодзи",
    "cost_fixed": "Фикс. расходы", "cost_variable": "Пер. расходы",
    "cost_team": "Ком. расходы", "cost_extra": "Доп. расходы",
    "tasks": "Задачи", "contractor_req": "Подрядчик",
    "rating": "Рейтинг", "rooms_info": "Номера", "booking_cost": "Букинг цена",
    "our_cost": "Наша цена", "booking_link": "Букинг ссылка",
    "total_cost": "Полная ст-ть", "prepaid": "Предоплата", "our_price": "Наша цена",
    "amount_rub": "₽ Рубли", "amount_usd": "$ Доллары", "amount_idr": "Rp Рупии",
    "description": "Описание", "entity": "Контрагент", "type": "Тип",
}

def format_parsed(parsed, text):
    t = parsed.get("type", "unknown")
    d = parsed.get("data", {})
    c = parsed.get("confidence", 0)
    snippet = text[:200] + ("..." if len(text) > 200 else "")
    lines = [
        f"🎤 <i>{snippet}</i>",
        f"\n<b>{_TYPE_ICON.get(t, '❓')} {_TYPE_NAME.get(t, '?')}</b>  <i>{int(c * 100)}% уверенность</i>",
    ]
    for k, v in d.items():
        if v is not None and v != "" and v != 0:
            lines.append(f"  • {_FIELD_LBL.get(k, k)}: <b>{v}</b>")
    return "\n".join(lines)

async def save_entry(tour_id, kind, d):
    async with aiosqlite.connect(DB_PATH) as db:
        if kind == "location":
            await db.execute(
                "INSERT INTO locations(tour_id,region,category,status,name,hours,cost,notes,maps_link,contacts,website,youtube,instagram) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (tour_id, d.get("region"), d.get("category"), d.get("status", "2️⃣"),
                 d.get("name", "Без названия"), d.get("hours"), d.get("cost"), d.get("notes"),
                 d.get("maps_link"), d.get("contacts"), d.get("website"), d.get("youtube"), d.get("instagram")),
            )
        elif kind == "program":
            await db.execute(
                "INSERT INTO program(tour_id,day_num,date,time,title,emoji,cost_fixed,cost_variable,cost_team,cost_extra,tasks,contractor_req) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (tour_id, d.get("day_num"), d.get("date"), d.get("time"),
                 d.get("title", "Мероприятие"), d.get("emoji", "🔥"),
                 d.get("cost_fixed", 0), d.get("cost_variable", 0),
                 d.get("cost_team", 0), d.get("cost_extra", 0),
                 d.get("tasks"), d.get("contractor_req")),
            )
        elif kind == "hotel":
            await db.execute(
                "INSERT INTO hotels(tour_id,region,name,rating,rooms_info,booking_cost,our_cost,notes,contacts,maps_link,booking_link,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (tour_id, d.get("region"), d.get("name", "Отель"), d.get("rating"),
                 d.get("rooms_info"), d.get("booking_cost"), d.get("our_cost"),
                 d.get("notes"), d.get("contacts"), d.get("maps_link"),
                 d.get("booking_link"), d.get("status", "2️⃣")),
            )
        elif kind == "guest":
            await db.execute(
                "INSERT INTO guests(tour_id,name,total_cost,prepaid,our_price,status,notes) VALUES(?,?,?,?,?,?,?)",
                (tour_id, d.get("name", "Гость"), d.get("total_cost", 0),
                 d.get("prepaid", 0), d.get("our_price", 0), d.get("status", "not_paid"), d.get("notes")),
            )
        elif kind == "dds":
            await db.execute(
                "INSERT INTO dds(tour_id,date,amount_rub,amount_usd,amount_idr,description,entity,type) VALUES(?,?,?,?,?,?,?,?)",
                (tour_id, d.get("date"), d.get("amount_rub", 0), d.get("amount_usd", 0),
                 d.get("amount_idr", 0), d.get("description"), d.get("entity"), d.get("type", "out")),
            )
        await db.commit()

# ── Bot handlers ──────────────────────────────────────────────────────────────
@router.message(Command("start"))
async def cmd_start(m: Message, state: FSMContext):
    if not _is_admin(m.from_user.id):
        await m.answer("⛔ Нет доступа.")
        return
    await state.clear()
    tour = await get_active_tour(m.from_user.id)
    tour_line = f"\n\n🌍 Активный тур: <b>{tour['name']}</b>" if tour else "\n\n⚠️ Нет тура. Создайте: /newtrip"
    text = (
        f"👋 {m.from_user.first_name}!\n\n"
        "🗺️ <b>Tour Operator CRM</b>\n"
        "Профессиональный инструмент управления коммерческими турами."
        f"{tour_line}\n\n"
        "🎤 Отправьте голосовое → данные добавятся автоматически\n"
        "🌐 /app — открыть веб-приложение"
    )
    if WELCOME_IMAGE.exists():
        await m.answer_photo(FSInputFile(str(WELCOME_IMAGE)), caption=text,
                             parse_mode="HTML", reply_markup=main_kb())
    else:
        await m.answer(text, parse_mode="HTML", reply_markup=main_kb())

@router.message(Command("newtrip"))
async def cmd_newtrip(m: Message, state: FSMContext):
    if not _is_admin(m.from_user.id):
        return
    await state.set_state(NewTour.name)
    await m.answer("🆕 <b>Новый тур</b>\n\nВведите название:", parse_mode="HTML")

@router.message(NewTour.name)
async def fsm_name(m: Message, state: FSMContext):
    await state.update_data(name=m.text.strip())
    await state.set_state(NewTour.destination)
    await m.answer("📍 Направление (куда едем?):")

@router.message(NewTour.destination)
async def fsm_dest(m: Message, state: FSMContext):
    await state.update_data(destination=m.text.strip())
    await state.set_state(NewTour.dates)
    await m.answer(
        "📅 Даты тура (например: 15.08.2025 – 22.08.2025)\n<i>/skip — пропустить</i>",
        parse_mode="HTML",
    )

@router.message(Command("skip"), NewTour.dates)
async def fsm_skip_dates(m: Message, state: FSMContext):
    await _finish_tour(m, state, None, None)

@router.message(NewTour.dates)
async def fsm_dates(m: Message, state: FSMContext):
    ds = de = None
    for raw in re.findall(r"(\d{1,2}[./]\d{1,2}[./]\d{4})", m.text):
        try:
            pd = datetime.strptime(raw.replace("/", "."), "%d.%m.%Y").strftime("%Y-%m-%d")
            if ds is None:
                ds = pd
            else:
                de = pd
                break
        except ValueError:
            pass
    await _finish_tour(m, state, ds, de)

async def _finish_tour(m, state, ds, de):
    data = await state.get_data()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO tours(name,destination,date_start,date_end) VALUES(?,?,?,?)",
            (data["name"], data.get("destination"), ds, de),
        )
        tid = cur.lastrowid
        await db.commit()
    await set_active_tour(m.from_user.id, tid)
    await state.clear()
    await m.answer(
        f"✅ Тур <b>{data['name']}</b> создан!\n\nДобавляйте данные голосом или через /app",
        parse_mode="HTML", reply_markup=main_kb(),
    )

@router.message(Command("tours"))
async def cmd_tours(m: Message):
    if not _is_admin(m.from_user.id):
        return
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM tours ORDER BY created_at DESC") as c:
            tours = [dict(r) for r in await c.fetchall()]
    if not tours:
        await m.answer("Нет туров. Создайте: /newtrip")
        return
    active = await get_active_tour(m.from_user.id)
    aid = active["id"] if active else None
    rows = []
    for t in tours:
        mark = "✅ " if t["id"] == aid else ""
        dest = f" · {t['destination']}" if t.get("destination") else ""
        rows.append([btn(f"{mark}{t['name']}{dest}", f"sw_tour_{t['id']}")])
    rows.append([btn("➕ Новый тур", "new_tour")])
    await m.answer("🌍 <b>Туры</b> (нажми для переключения):", parse_mode="HTML", reply_markup=mk(rows))

@router.message(Command("app"))
async def cmd_app(m: Message):
    if not _is_admin(m.from_user.id):
        return
    url = f"{BASE_URL}/app?token={m.from_user.id}"
    await m.answer(
        f"🌐 <b>Веб-приложение</b>\n\n"
        f'<a href="{url}">Открыть CRM →</a>\n\n<code>{url}</code>',
        parse_mode="HTML", disable_web_page_preview=True,
    )

@router.message(Command("lip"))
async def cmd_lip(m: Message):
    if not _is_admin(m.from_user.id):
        return
    tour = await get_active_tour(m.from_user.id)
    if not tour:
        await m.answer("⚠️ Нет активного тура. /newtrip")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM locations WHERE tour_id=? ORDER BY region,category,name", (tour["id"],)
        ) as c:
            items = [dict(r) for r in await c.fetchall()]
    if not items:
        await m.answer(f"📍 <b>ЛиП — {tour['name']}</b>\n\nПусто. Отправьте голосовое.", parse_mode="HTML")
        return
    by_region = {}
    for it in items:
        by_region.setdefault(it.get("region") or "Общее", []).append(it)
    lines = [f"📍 <b>ЛиП — {tour['name']}</b>"]
    for reg, locs in by_region.items():
        lines.append(f"\n📌 <b>{reg}</b>")
        for loc in locs:
            cat = f"[{loc['category']}] " if loc.get("category") else ""
            lines.append(f"  {loc.get('status', '—')} {cat}{loc['name']}")
    await m.answer("\n".join(lines), parse_mode="HTML")

# ── Voice handler ─────────────────────────────────────────────────────────────
@router.message(F.voice)
async def on_voice(m: Message, state: FSMContext):
    if not _is_admin(m.from_user.id):
        return
    tour = await get_active_tour(m.from_user.id)
    if not tour:
        await m.answer("⚠️ Нет активного тура. Создайте: /newtrip")
        return
    sm = await m.answer("🎤 Транскрибирую…")
    text = await transcribe_voice(m.bot, m.voice)
    if not text:
        await sm.edit_text("❌ Не удалось распознать речь. Попробуйте ещё раз.")
        return
    await sm.edit_text(f"🤖 Анализирую данные…\n\n<i>«{text[:200]}»</i>", parse_mode="HTML")
    parsed = await parse_with_claude(text)
    if parsed.get("type") == "unknown":
        await sm.edit_text(
            f"❓ Не удалось определить тип записи.\n\n<i>«{text}»</i>",
            parse_mode="HTML",
        )
        return
    await state.set_state(VoicePend.confirm)
    await state.update_data(parsed=parsed, tour_id=tour["id"])
    await sm.edit_text(
        format_parsed(parsed, text),
        parse_mode="HTML",
        reply_markup=mk([[
            btn("✅ Сохранить", "vs_save"),
            btn("✏️ Исправить", "vs_edit"),
            btn("❌ Отменить", "vs_cancel"),
        ]]),
    )

@router.callback_query(VoicePend.confirm, F.data == "vs_save")
async def vs_save(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    await save_entry(d["tour_id"], d["parsed"]["type"], d["parsed"].get("data", {}))
    await state.clear()
    icon = _TYPE_ICON.get(d["parsed"]["type"], "✅")
    name = _TYPE_NAME.get(d["parsed"]["type"], "Запись")
    await cb.message.edit_text(f"✅ {icon} {name} сохранена!")
    await cb.answer("Сохранено!")

@router.callback_query(VoicePend.confirm, F.data.in_({"vs_edit", "vs_cancel"}))
async def vs_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("✏️ Отменено. Отправьте новое голосовое с уточнёнными данными.")
    await cb.answer()

# ── Inline callbacks ──────────────────────────────────────────────────────────
@router.callback_query(F.data == "new_tour")
async def cb_new_tour(cb: CallbackQuery, state: FSMContext):
    await state.set_state(NewTour.name)
    await cb.message.answer("🆕 <b>Новый тур</b>\n\nВведите название:", parse_mode="HTML")
    await cb.answer()

@router.callback_query(F.data == "open_app")
async def cb_open_app(cb: CallbackQuery):
    url = f"{BASE_URL}/app?token={cb.from_user.id}"
    await cb.message.answer(
        f'🌐 <a href="{url}">Открыть CRM →</a>\n<code>{url}</code>',
        parse_mode="HTML", disable_web_page_preview=True,
    )
    await cb.answer()

@router.callback_query(F.data == "tours_list")
async def cb_tours_list(cb: CallbackQuery):
    await cmd_tours(cb.message)
    await cb.answer()

@router.callback_query(F.data.startswith("sw_tour_"))
async def cb_sw_tour(cb: CallbackQuery):
    tid = int(cb.data.split("_")[-1])
    await set_active_tour(cb.from_user.id, tid)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT name FROM tours WHERE id=?", (tid,)) as c:
            row = await c.fetchone()
    if row:
        await cb.message.edit_text(f"✅ Активный тур: <b>{row['name']}</b>", parse_mode="HTML")
        await cb.answer(f"Тур: {row['name']}")

@router.callback_query(F.data.startswith("sec_"))
async def cb_section(cb: CallbackQuery):
    tour = await get_active_tour(cb.from_user.id)
    if not tour:
        await cb.answer("Нет активного тура! /newtrip", show_alert=True)
        return
    url = f"{BASE_URL}/app?token={cb.from_user.id}"
    tabs = {"sec_program": "📅 Программа", "sec_lip": "📍 ЛиП",
            "sec_hotels": "🏨 Отели", "sec_guests": "👥 Гости", "sec_dds": "💰 ДДС"}
    label = tabs.get(cb.data, "Раздел")
    await cb.message.answer(
        f"{label} — <b>{tour['name']}</b>\n\n"
        f'<a href="{url}">Открыть в веб-приложении →</a>',
        parse_mode="HTML", disable_web_page_preview=True,
    )
    await cb.answer()

# ── REST API helpers ──────────────────────────────────────────────────────────
def jresp(data, status=200):
    return web.Response(
        text=json.dumps(data, ensure_ascii=False, default=str),
        content_type="application/json",
        status=status,
    )

async def check_auth(req):
    tok = req.headers.get("X-Token") or req.rel_url.query.get("token", "")
    try:
        return _is_admin(int(tok)) if tok else False
    except (ValueError, TypeError):
        return False

def rows_to_list(rows):
    return [dict(r) for r in rows]

# ── Tours API ─────────────────────────────────────────────────────────────────
async def api_tours_get(req):
    if not await check_auth(req):
        return jresp({"e": "Unauthorized"}, 401)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM tours ORDER BY created_at DESC") as c:
            return jresp(rows_to_list(await c.fetchall()))

async def api_tours_post(req):
    if not await check_auth(req):
        return jresp({"e": "Unauthorized"}, 401)
    b = await req.json()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO tours(name,destination,date_start,date_end,guests_count,status) VALUES(?,?,?,?,?,?)",
            (b.get("name", "Тур"), b.get("destination"), b.get("date_start"),
             b.get("date_end"), b.get("guests_count", 0), b.get("status", "planning")),
        )
        tid = cur.lastrowid
        await db.commit()
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM tours WHERE id=?", (tid,)) as c:
            return jresp(dict(await c.fetchone()), 201)

async def api_tour_put(req):
    if not await check_auth(req):
        return jresp({"e": "Unauthorized"}, 401)
    tid = int(req.match_info["id"])
    b = await req.json()
    flds = ["name", "destination", "date_start", "date_end", "guests_count", "status"]
    sets = ", ".join(f"{f}=?" for f in flds if f in b)
    vals = [b[f] for f in flds if f in b] + [tid]
    async with aiosqlite.connect(DB_PATH) as db:
        if sets:
            await db.execute(f"UPDATE tours SET {sets} WHERE id=?", vals)
            await db.commit()
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM tours WHERE id=?", (tid,)) as c:
            return jresp(dict(await c.fetchone()))

async def api_tour_delete(req):
    if not await check_auth(req):
        return jresp({"e": "Unauthorized"}, 401)
    tid = int(req.match_info["id"])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM tours WHERE id=?", (tid,))
        for t in ("program", "locations", "hotels", "guests", "dds"):
            await db.execute(f"DELETE FROM {t} WHERE tour_id=?", (tid,))
        await db.commit()
    return jresp({"ok": True})

# ── Generic CRUD ──────────────────────────────────────────────────────────────
def _make_crud(table, ins_cols, ins_fn, upd_flds, order_by="id"):
    async def _get(req):
        if not await check_auth(req):
            return jresp({"e": "Unauthorized"}, 401)
        tid = int(req.match_info["tour_id"])
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"SELECT * FROM {table} WHERE tour_id=? ORDER BY {order_by}", (tid,)
            ) as c:
                return jresp(rows_to_list(await c.fetchall()))

    async def _post(req):
        if not await check_auth(req):
            return jresp({"e": "Unauthorized"}, 401)
        tid = int(req.match_info["tour_id"])
        b = await req.json()
        vals = ins_fn(b, tid)
        ph = ",".join(["?"] * len(vals))
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(f"INSERT INTO {table}({ins_cols}) VALUES({ph})", vals)
            rid = cur.lastrowid
            await db.commit()
            db.row_factory = aiosqlite.Row
            async with db.execute(f"SELECT * FROM {table} WHERE id=?", (rid,)) as c:
                return jresp(dict(await c.fetchone()), 201)

    async def _put(req):
        if not await check_auth(req):
            return jresp({"e": "Unauthorized"}, 401)
        rid = int(req.match_info["id"])
        b = await req.json()
        sets = ", ".join(f"{f}=?" for f in upd_flds if f in b)
        vals = [b[f] for f in upd_flds if f in b] + [rid]
        async with aiosqlite.connect(DB_PATH) as db:
            if sets:
                await db.execute(f"UPDATE {table} SET {sets} WHERE id=?", vals)
                await db.commit()
            db.row_factory = aiosqlite.Row
            async with db.execute(f"SELECT * FROM {table} WHERE id=?", (rid,)) as c:
                return jresp(dict(await c.fetchone()))

    async def _delete(req):
        if not await check_auth(req):
            return jresp({"e": "Unauthorized"}, 401)
        rid = int(req.match_info["id"])
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(f"DELETE FROM {table} WHERE id=?", (rid,))
            await db.commit()
        return jresp({"ok": True})

    return _get, _post, _put, _delete

prog_get, prog_post, prog_put, prog_del = _make_crud(
    "program",
    "tour_id,day_num,date,time,title,emoji,cost_fixed,cost_variable,cost_team,cost_extra,tasks,contractor_req",
    lambda b, tid: (tid, b.get("day_num"), b.get("date"), b.get("time"),
                    b.get("title", "Мероприятие"), b.get("emoji", "🔥"),
                    b.get("cost_fixed", 0), b.get("cost_variable", 0),
                    b.get("cost_team", 0), b.get("cost_extra", 0),
                    b.get("tasks"), b.get("contractor_req")),
    ["day_num", "date", "time", "title", "emoji", "cost_fixed", "cost_variable",
     "cost_team", "cost_extra", "tasks", "contractor_req"],
    order_by="day_num,date,time",
)
loc_get, loc_post, loc_put, loc_del = _make_crud(
    "locations",
    "tour_id,region,category,status,name,hours,cost,notes,maps_link,contacts,website,youtube,instagram",
    lambda b, tid: (tid, b.get("region"), b.get("category"), b.get("status", "2️⃣"),
                    b.get("name", "Локация"), b.get("hours"), b.get("cost"), b.get("notes"),
                    b.get("maps_link"), b.get("contacts"), b.get("website"),
                    b.get("youtube"), b.get("instagram")),
    ["region", "category", "status", "name", "hours", "cost", "notes",
     "maps_link", "contacts", "website", "youtube", "instagram"],
    order_by="region,category,name",
)
hot_get, hot_post, hot_put, hot_del = _make_crud(
    "hotels",
    "tour_id,region,name,rating,rooms_info,booking_cost,our_cost,notes,contacts,maps_link,booking_link,status",
    lambda b, tid: (tid, b.get("region"), b.get("name", "Отель"), b.get("rating"),
                    b.get("rooms_info"), b.get("booking_cost"), b.get("our_cost"),
                    b.get("notes"), b.get("contacts"), b.get("maps_link"),
                    b.get("booking_link"), b.get("status", "2️⃣")),
    ["region", "name", "rating", "rooms_info", "booking_cost", "our_cost",
     "notes", "contacts", "maps_link", "booking_link", "status"],
    order_by="region,name",
)
gst_get, gst_post, gst_put, gst_del = _make_crud(
    "guests",
    "tour_id,name,total_cost,prepaid,our_price,status,notes",
    lambda b, tid: (tid, b.get("name", "Гость"), b.get("total_cost", 0),
                    b.get("prepaid", 0), b.get("our_price", 0),
                    b.get("status", "not_paid"), b.get("notes")),
    ["name", "total_cost", "prepaid", "our_price", "status", "notes"],
    order_by="name",
)
dds_get, dds_post, dds_put, dds_del = _make_crud(
    "dds",
    "tour_id,date,amount_rub,amount_usd,amount_idr,description,entity,type",
    lambda b, tid: (tid, b.get("date"), b.get("amount_rub", 0), b.get("amount_usd", 0),
                    b.get("amount_idr", 0), b.get("description"), b.get("entity"),
                    b.get("type", "out")),
    ["date", "amount_rub", "amount_usd", "amount_idr", "description", "entity", "type"],
    order_by="date DESC,created_at DESC",
)

# ── HTML SPA ──────────────────────────────────────────────────────────────────
HTML_APP = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tour Operator CRM</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0f172a;color:#e2e8f0;min-height:100vh}
:root{--bg:#0f172a;--card:#1e293b;--border:#334155;--accent:#f97316;--green:#22c55e;--red:#ef4444;--blue:#3b82f6;--yellow:#eab308;--muted:#94a3b8}
.hdr{background:#1e293b;border-bottom:1px solid #334155;padding:0 16px;display:flex;align-items:center;gap:10px;flex-wrap:wrap;position:sticky;top:0;z-index:100}
.hdr h1{font-size:1rem;font-weight:700;color:#f97316;white-space:nowrap;padding:13px 0}
.t-sel{background:#0f172a;border:1px solid #334155;color:#e2e8f0;padding:5px 9px;border-radius:6px;font-size:.82rem;max-width:220px}
.btn{padding:6px 12px;border-radius:6px;border:none;cursor:pointer;font-size:.8rem;font-weight:600;transition:.15s}
.ba{background:#f97316;color:#fff}.ba:hover{background:#ea6c0a}
.bg2{background:transparent;border:1px solid #334155;color:#e2e8f0}.bg2:hover{background:#1e293b}
.tabs{background:#1e293b;border-bottom:1px solid #334155;display:flex;overflow-x:auto;padding:0 16px;gap:2px}
.tab{padding:9px 14px;cursor:pointer;font-size:.82rem;border-bottom:2px solid transparent;white-space:nowrap;color:#94a3b8;transition:.15s}
.tab.act{color:#f97316;border-bottom-color:#f97316}.tab:hover{color:#e2e8f0}
.cnt{padding:16px;max-width:1400px;margin:0 auto}
.pane{display:none}.pane.act{display:block}
.sum{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-bottom:16px}
.sc{background:#1e293b;border:1px solid #334155;border-radius:9px;padding:12px}
.sc .lb{font-size:.72rem;color:#94a3b8;margin-bottom:3px}
.sc .vl{font-size:1.3rem;font-weight:700}
.sc .sb{font-size:.7rem;color:#94a3b8}
.tb{display:flex;align-items:center;gap:8px;margin-bottom:12px;flex-wrap:wrap}
.tb input,.tb select{background:#0f172a;border:1px solid #334155;color:#e2e8f0;padding:6px 10px;border-radius:6px;font-size:.82rem}
.tb input{flex:1;min-width:140px}
.tw{overflow-x:auto;border-radius:9px;border:1px solid #334155}
table{width:100%;border-collapse:collapse;font-size:.8rem}
th{background:#162032;color:#94a3b8;font-weight:600;padding:9px 10px;text-align:left;white-space:nowrap}
td{padding:8px 10px;border-top:1px solid #334155;vertical-align:top}
tr:hover td{background:#192536}
.acts{display:flex;gap:4px;white-space:nowrap}
.ib{background:none;border:none;cursor:pointer;padding:3px 6px;border-radius:4px;font-size:.85rem;transition:.15s}
.ib:hover{background:#334155}
.bdg{display:inline-flex;align-items:center;padding:2px 7px;border-radius:10px;font-size:.72rem;font-weight:600}
.bg{background:rgba(34,197,94,.15);color:#22c55e}
.bb{background:rgba(59,130,246,.15);color:#3b82f6}
.by{background:rgba(234,179,8,.15);color:#eab308}
.br{background:rgba(239,68,68,.15);color:#ef4444}
.bm{background:rgba(148,163,184,.15);color:#94a3b8}
.mo-ov{position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:500;display:none;align-items:center;justify-content:center;padding:12px}
.mo-ov.open{display:flex}
.mo{background:#1e293b;border:1px solid #334155;border-radius:13px;width:100%;max-width:540px;max-height:92vh;overflow-y:auto}
.mo-h{padding:14px 18px;border-bottom:1px solid #334155;display:flex;align-items:center;justify-content:space-between}
.mo-h h2{font-size:.95rem}
.mo-b{padding:18px}
.mo-f{padding:13px 18px;border-top:1px solid #334155;display:flex;justify-content:flex-end;gap:8px}
.fr{margin-bottom:12px}
label{display:block;font-size:.75rem;color:#94a3b8;margin-bottom:4px}
input,select,textarea{width:100%;background:#0f172a;border:1px solid #334155;color:#e2e8f0;padding:7px 10px;border-radius:6px;font-size:.82rem;font-family:inherit}
input:focus,select:focus,textarea:focus{outline:2px solid #f97316;border-color:transparent}
textarea{resize:vertical;min-height:64px}
.fr2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.fr3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px}
.empty{text-align:center;padding:44px 16px;color:#94a3b8}
.loading{text-align:center;padding:28px;color:#94a3b8}
.cin{color:#22c55e}.cout{color:#ef4444}
.dtots{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin-bottom:14px}
.dtc{background:#1e293b;border:1px solid #334155;border-radius:8px;padding:11px}
.dtc .cur{font-size:.68rem;color:#94a3b8}
.dtc .din{color:#22c55e;font-size:.95rem;font-weight:700}
.dtc .dout{color:#ef4444;font-size:.95rem;font-weight:700}
</style>
</head>
<body>
<div class="hdr">
  <h1>🗺️ Tour CRM</h1>
  <select class="t-sel" id="tourSel" onchange="switchTour(this.value)">
    <option value="">— выберите тур —</option>
  </select>
  <button class="btn ba" onclick="openModal('tour')">➕ Тур</button>
  <span id="tourInfo" style="font-size:.75rem;color:#94a3b8"></span>
</div>
<div class="tabs">
  <div class="tab act" onclick="switchTab('program',0)">📅 Программа</div>
  <div class="tab" onclick="switchTab('lip',1)">📍 ЛиП</div>
  <div class="tab" onclick="switchTab('hotels',2)">🏨 Отели</div>
  <div class="tab" onclick="switchTab('guests',3)">👥 Гости</div>
  <div class="tab" onclick="switchTab('dds',4)">💰 ДДС</div>
</div>
<div class="cnt">

<div class="pane act" id="pane-program">
  <div class="sum" id="prog-sum"></div>
  <div class="tb">
    <input placeholder="Поиск по программе…" id="prog-q" oninput="renderProg()">
    <button class="btn ba" onclick="openModal('program')">➕ Добавить</button>
  </div>
  <div class="tw"><table>
    <thead><tr><th>День</th><th>Дата</th><th>Время</th><th>Em</th><th>Название</th><th>Фикс ₽</th><th>Пер ₽</th><th>Ком ₽</th><th>Доп ₽</th><th>Задачи</th><th>Подрядчик</th><th></th></tr></thead>
    <tbody id="prog-tb"><tr><td colspan="12" class="loading">Загрузка…</td></tr></tbody>
  </table></div>
</div>

<div class="pane" id="pane-lip">
  <div class="sum" id="lip-sum"></div>
  <div class="tb">
    <input placeholder="Поиск…" id="lip-q" oninput="renderLip()">
    <select id="lip-st" onchange="renderLip()"><option value="">Все статусы</option><option>✅</option><option>2️⃣</option><option>❗</option><option>❌</option><option>—</option></select>
    <select id="lip-cat" onchange="renderLip()"><option value="">Все категории</option></select>
    <select id="lip-reg" onchange="renderLip()"><option value="">Все регионы</option></select>
    <button class="btn ba" onclick="openModal('lip')">➕ Добавить</button>
  </div>
  <div class="tw"><table>
    <thead><tr><th>Статус</th><th>Категория</th><th>Регион</th><th>Название</th><th>Часы</th><th>Стоимость</th><th>Контакты</th><th>Заметки</th><th>Ссылки</th><th></th></tr></thead>
    <tbody id="lip-tb"><tr><td colspan="10" class="loading">Загрузка…</td></tr></tbody>
  </table></div>
</div>

<div class="pane" id="pane-hotels">
  <div class="sum" id="hot-sum"></div>
  <div class="tb">
    <input placeholder="Поиск отелей…" id="hot-q" oninput="renderHot()">
    <select id="hot-st" onchange="renderHot()"><option value="">Все статусы</option><option>✅</option><option>2️⃣</option><option>❗</option><option>❌</option></select>
    <select id="hot-reg" onchange="renderHot()"><option value="">Все регионы</option></select>
    <button class="btn ba" onclick="openModal('hotels')">➕ Добавить</button>
  </div>
  <div class="tw"><table>
    <thead><tr><th>Статус</th><th>Регион</th><th>Название</th><th>Рейтинг</th><th>Номера</th><th>Букинг</th><th>Наша цена</th><th>Контакты</th><th>Заметки</th><th></th></tr></thead>
    <tbody id="hot-tb"><tr><td colspan="10" class="loading">Загрузка…</td></tr></tbody>
  </table></div>
</div>

<div class="pane" id="pane-guests">
  <div class="sum" id="gst-sum"></div>
  <div class="tb">
    <input placeholder="Поиск гостей…" id="gst-q" oninput="renderGst()">
    <select id="gst-st" onchange="renderGst()"><option value="">Все статусы</option><option value="not_paid">Не оплачено</option><option value="partial">Частично</option><option value="paid">Оплачено</option><option value="refund">Возврат</option></select>
    <button class="btn ba" onclick="openModal('guests')">➕ Добавить</button>
  </div>
  <div class="tw"><table>
    <thead><tr><th>Гость</th><th>Полная ст-ть</th><th>Предоплата</th><th>Наша цена</th><th>Долг</th><th>Прибыль</th><th>Статус</th><th>Заметки</th><th></th></tr></thead>
    <tbody id="gst-tb"><tr><td colspan="9" class="loading">Загрузка…</td></tr></tbody>
  </table></div>
</div>

<div class="pane" id="pane-dds">
  <div class="dtots" id="dds-tots"></div>
  <div class="tb">
    <input placeholder="Поиск…" id="dds-q" oninput="renderDds()">
    <select id="dds-t" onchange="renderDds()"><option value="">Приход и расход</option><option value="in">⬆️ Приход</option><option value="out">⬇️ Расход</option></select>
    <button class="btn ba" onclick="openModal('dds')">➕ Добавить</button>
  </div>
  <div class="tw"><table>
    <thead><tr><th>Дата</th><th>Тип</th><th>₽ Рубли</th><th>$ Доллары</th><th>Rp Рупии</th><th>Описание</th><th>Контрагент</th><th></th></tr></thead>
    <tbody id="dds-tb"><tr><td colspan="8" class="loading">Загрузка…</td></tr></tbody>
  </table></div>
</div>

</div>

<div class="mo-ov" id="moOv" onclick="if(event.target===this)closeModal()">
  <div class="mo">
    <div class="mo-h"><h2 id="moTitle">Добавить</h2><button class="ib" onclick="closeModal()" style="font-size:1.1rem">✕</button></div>
    <div class="mo-b" id="moBody"></div>
    <div class="mo-f"><button class="btn bg2" onclick="closeModal()">Отмена</button><button class="btn ba" onclick="submitModal()">Сохранить</button></div>
  </div>
</div>

<script>
const TOKEN=new URLSearchParams(location.search).get('token')||localStorage.getItem('crm_tk')||'';
if(TOKEN)localStorage.setItem('crm_tk',TOKEN);
const H={'X-Token':TOKEN,'Content-Type':'application/json'};
let tours=[],activeTid=null;
let progData=[],lipData=[],hotData=[],gstData=[],ddsData=[];
let moType=null,moEditId=null,curTab='program';

async function api(method,path,body){
  const r=await fetch(path,{method,headers:H,body:body?JSON.stringify(body):undefined});
  if(!r.ok)throw new Error(await r.text());
  return r.json();
}

async function loadTours(){
  try{
    tours=await api('GET','/api/tours');
    const sel=document.getElementById('tourSel');
    sel.innerHTML='<option value="">— выберите тур —</option>'+
      tours.map(t=>`<option value="${t.id}">${t.name}${t.destination?' — '+t.destination:''}</option>`).join('');
    if(!activeTid&&tours.length){activeTid=tours[0].id;sel.value=activeTid;loadTab();}
    else if(activeTid)sel.value=activeTid;
    updateInfo();
  }catch(e){console.error(e);}
}

function updateInfo(){
  const t=tours.find(x=>x.id==activeTid);
  const el=document.getElementById('tourInfo');
  if(t)el.textContent=[t.date_start,t.date_end].filter(Boolean).join(' – ')+(t.guests_count?' | '+t.guests_count+' гостей':'');
  else el.textContent='';
}

function switchTour(id){activeTid=id?parseInt(id):null;updateInfo();loadTab();}

function switchTab(name,idx){
  curTab=name;
  document.querySelectorAll('.tab').forEach((t,i)=>t.classList.toggle('act',i===idx));
  document.querySelectorAll('.pane').forEach(p=>p.classList.remove('act'));
  document.getElementById('pane-'+name).classList.add('act');
  loadTab();
}

function loadTab(){
  if(!activeTid)return;
  if(curTab==='program')loadProg();
  else if(curTab==='lip')loadLip();
  else if(curTab==='hotels')loadHot();
  else if(curTab==='guests')loadGst();
  else if(curTab==='dds')loadDds();
}

async function loadProg(){
  progData=await api('GET',`/api/tours/${activeTid}/program`);
  const tot=progData.reduce((s,r)=>s+(r.cost_fixed||0)+(r.cost_variable||0)+(r.cost_team||0)+(r.cost_extra||0),0);
  const days=new Set(progData.map(r=>r.day_num).filter(Boolean)).size;
  document.getElementById('prog-sum').innerHTML=`
    <div class="sc"><div class="lb">Мероприятий</div><div class="vl">${progData.length}</div></div>
    <div class="sc"><div class="lb">Дней</div><div class="vl">${days}</div></div>
    <div class="sc"><div class="lb">Бюджет</div><div class="vl">${fmt(tot)}</div><div class="sb">₽</div></div>`;
  renderProg();
}
function renderProg(){
  const q=(document.getElementById('prog-q').value||'').toLowerCase();
  const rows=progData.filter(r=>!q||(r.title+' '+(r.tasks||'')+' '+(r.contractor_req||'')).toLowerCase().includes(q));
  const tb=document.getElementById('prog-tb');
  if(!rows.length){tb.innerHTML='<tr><td colspan="12"><div class="empty">Нет данных</div></td></tr>';return;}
  tb.innerHTML=rows.map(r=>`<tr>
    <td>${r.day_num||''}</td><td>${r.date||''}</td><td>${r.time||''}</td>
    <td>${r.emoji||'🔥'}</td><td><b>${esc(r.title)}</b></td>
    <td>${r.cost_fixed?fmt(r.cost_fixed):''}</td><td>${r.cost_variable?fmt(r.cost_variable):''}</td>
    <td>${r.cost_team?fmt(r.cost_team):''}</td><td>${r.cost_extra?fmt(r.cost_extra):''}</td>
    <td style="max-width:150px;white-space:pre-wrap">${esc(r.tasks||'')}</td>
    <td>${esc(r.contractor_req||'')}</td>
    <td class="acts"><button class="ib" onclick="openModal('program',${r.id})">✏️</button><button class="ib" onclick="del('program',${r.id})">🗑️</button></td>
  </tr>`).join('');
}

async function loadLip(){
  lipData=await api('GET',`/api/tours/${activeTid}/locations`);
  fillSel('lip-cat',[...new Set(lipData.map(r=>r.category).filter(Boolean))],'Все категории');
  fillSel('lip-reg',[...new Set(lipData.map(r=>r.region).filter(Boolean))],'Все регионы');
  const byS={};lipData.forEach(r=>byS[r.status]=(byS[r.status]||0)+1);
  document.getElementById('lip-sum').innerHTML=`
    <div class="sc"><div class="lb">Всего мест</div><div class="vl">${lipData.length}</div></div>
    ${[['✅','bg','Подходит'],['2️⃣','bb','Резерв'],['❗','by','Разведать'],['❌','br','Не берём'],['—','bm','Не подходит']].map(([s,c,l])=>
      `<div class="sc"><div class="lb"><span class="bdg ${c}">${s}</span> ${l}</div><div class="vl">${byS[s]||0}</div></div>`).join('')}`;
  renderLip();
}
function renderLip(){
  const q=(document.getElementById('lip-q').value||'').toLowerCase();
  const sf=document.getElementById('lip-st').value,cf=document.getElementById('lip-cat').value,rf=document.getElementById('lip-reg').value;
  const rows=lipData.filter(r=>(!q||(r.name+' '+(r.notes||'')+' '+(r.contacts||'')).toLowerCase().includes(q))&&(!sf||r.status===sf)&&(!cf||r.category===cf)&&(!rf||r.region===rf));
  const tb=document.getElementById('lip-tb');
  if(!rows.length){tb.innerHTML='<tr><td colspan="10"><div class="empty">Нет данных</div></td></tr>';return;}
  tb.innerHTML=rows.map(r=>`<tr>
    <td>${sBdg(r.status)}</td><td>${esc(r.category||'')}</td><td>${esc(r.region||'')}</td>
    <td><b>${esc(r.name)}</b></td><td>${esc(r.hours||'')}</td><td>${esc(r.cost||'')}</td>
    <td>${esc(r.contacts||'')}</td><td style="max-width:180px">${esc(r.notes||'')}</td>
    <td>${lnks(r)}</td>
    <td class="acts"><button class="ib" onclick="openModal('lip',${r.id})">✏️</button><button class="ib" onclick="del('locations',${r.id})">🗑️</button></td>
  </tr>`).join('');
}
function lnks(r){
  let s='';
  if(r.maps_link)s+=`<a href="${r.maps_link}" target="_blank" style="color:#3b82f6">🗺️</a> `;
  if(r.website)s+=`<a href="${r.website}" target="_blank" style="color:#94a3b8">🌐</a> `;
  if(r.youtube)s+=`<a href="${r.youtube}" target="_blank" style="color:#ef4444">▶️</a> `;
  if(r.instagram)s+=`<a href="${r.instagram}" target="_blank" style="color:#f97316">📸</a>`;
  return s;
}

async function loadHot(){
  hotData=await api('GET',`/api/tours/${activeTid}/hotels`);
  fillSel('hot-reg',[...new Set(hotData.map(r=>r.region).filter(Boolean))],'Все регионы');
  const byS={};hotData.forEach(r=>byS[r.status]=(byS[r.status]||0)+1);
  document.getElementById('hot-sum').innerHTML=`
    <div class="sc"><div class="lb">Всего отелей</div><div class="vl">${hotData.length}</div></div>
    ${[['✅','bg','Подходит'],['2️⃣','bb','Резерв'],['❗','by','Разведать'],['❌','br','Не берём']].map(([s,c,l])=>
      `<div class="sc"><div class="lb"><span class="bdg ${c}">${s}</span> ${l}</div><div class="vl">${byS[s]||0}</div></div>`).join('')}`;
  renderHot();
}
function renderHot(){
  const q=(document.getElementById('hot-q').value||'').toLowerCase();
  const sf=document.getElementById('hot-st').value,rf=document.getElementById('hot-reg').value;
  const rows=hotData.filter(r=>(!q||(r.name+' '+(r.region||'')).toLowerCase().includes(q))&&(!sf||r.status===sf)&&(!rf||r.region===rf));
  const tb=document.getElementById('hot-tb');
  if(!rows.length){tb.innerHTML='<tr><td colspan="10"><div class="empty">Нет данных</div></td></tr>';return;}
  tb.innerHTML=rows.map(r=>`<tr>
    <td>${sBdg(r.status)}</td><td>${esc(r.region||'')}</td><td><b>${esc(r.name)}</b></td>
    <td>${r.rating?'⭐ '+r.rating:''}</td><td>${esc(r.rooms_info||'')}</td>
    <td>${esc(r.booking_cost||'')}</td><td>${esc(r.our_cost||'')}</td>
    <td>${esc(r.contacts||'')}</td><td style="max-width:160px">${esc(r.notes||'')}</td>
    <td class="acts"><button class="ib" onclick="openModal('hotels',${r.id})">✏️</button><button class="ib" onclick="del('hotels',${r.id})">🗑️</button></td>
  </tr>`).join('');
}

async function loadGst(){
  gstData=await api('GET',`/api/tours/${activeTid}/guests`);
  const tot=gstData.reduce((s,r)=>s+(r.total_cost||0),0);
  const pre=gstData.reduce((s,r)=>s+(r.prepaid||0),0);
  const pro=gstData.reduce((s,r)=>s+(r.total_cost||0)-(r.our_price||0),0);
  const paid=gstData.filter(r=>r.status==='paid').length;
  document.getElementById('gst-sum').innerHTML=`
    <div class="sc"><div class="lb">Гостей</div><div class="vl">${gstData.length}</div><div class="sb">${paid} оплатили</div></div>
    <div class="sc"><div class="lb">Выручка</div><div class="vl">${fmt(tot)}</div><div class="sb">₽</div></div>
    <div class="sc"><div class="lb">Предоплата</div><div class="vl">${fmt(pre)}</div><div class="sb">₽</div></div>
    <div class="sc"><div class="lb">Прибыль</div><div class="vl" style="color:${pro>=0?'#22c55e':'#ef4444'}">${fmt(pro)}</div><div class="sb">₽</div></div>`;
  renderGst();
}
function renderGst(){
  const q=(document.getElementById('gst-q').value||'').toLowerCase();
  const sf=document.getElementById('gst-st').value;
  const rows=gstData.filter(r=>(!q||r.name.toLowerCase().includes(q))&&(!sf||r.status===sf));
  const tb=document.getElementById('gst-tb');
  if(!rows.length){tb.innerHTML='<tr><td colspan="9"><div class="empty">Нет данных</div></td></tr>';return;}
  tb.innerHTML=rows.map(r=>{
    const debt=(r.total_cost||0)-(r.prepaid||0);
    const pro=(r.total_cost||0)-(r.our_price||0);
    return `<tr>
      <td><b>${esc(r.name)}</b></td><td>${fmt(r.total_cost)}</td><td>${fmt(r.prepaid)}</td>
      <td>${fmt(r.our_price)}</td>
      <td style="color:${debt>0?'#ef4444':debt<0?'#3b82f6':'#22c55e'}">${fmt(debt)}</td>
      <td style="color:${pro>0?'#22c55e':pro<0?'#ef4444':'#94a3b8'}">${fmt(pro)}</td>
      <td>${gBdg(r.status)}</td><td>${esc(r.notes||'')}</td>
      <td class="acts"><button class="ib" onclick="openModal('guests',${r.id})">✏️</button><button class="ib" onclick="del('guests',${r.id})">🗑️</button></td>
    </tr>`;
  }).join('');
}

async function loadDds(){
  ddsData=await api('GET',`/api/tours/${activeTid}/dds`);
  const calc=t=>({
    r:ddsData.filter(x=>x.type===t).reduce((s,x)=>s+(x.amount_rub||0),0),
    u:ddsData.filter(x=>x.type===t).reduce((s,x)=>s+(x.amount_usd||0),0),
    i:ddsData.filter(x=>x.type===t).reduce((s,x)=>s+(x.amount_idr||0),0),
  });
  const I=calc('in'),O=calc('out');
  document.getElementById('dds-tots').innerHTML=`
    <div class="dtc"><div class="cur">₽ Рубли</div><div class="din">+ ${fmt(I.r)}</div><div class="dout">− ${fmt(O.r)}</div></div>
    <div class="dtc"><div class="cur">$ Доллары</div><div class="din">+ ${fmtd(I.u)}</div><div class="dout">− ${fmtd(O.u)}</div></div>
    <div class="dtc"><div class="cur">Rp Рупии</div><div class="din">+ ${fmt(I.i)}</div><div class="dout">− ${fmt(O.i)}</div></div>`;
  renderDds();
}
function renderDds(){
  const q=(document.getElementById('dds-q').value||'').toLowerCase();
  const tf=document.getElementById('dds-t').value;
  const rows=ddsData.filter(r=>(!q||(r.description||'').toLowerCase().includes(q)||(r.entity||'').toLowerCase().includes(q))&&(!tf||r.type===tf));
  const tb=document.getElementById('dds-tb');
  if(!rows.length){tb.innerHTML='<tr><td colspan="8"><div class="empty">Нет данных</div></td></tr>';return;}
  tb.innerHTML=rows.map(r=>`<tr>
    <td>${r.date||''}</td>
    <td><span class="bdg ${r.type==='in'?'bg':'br'}">${r.type==='in'?'⬆️ Приход':'⬇️ Расход'}</span></td>
    <td class="${r.type==='in'?'cin':'cout'}">${r.amount_rub?fmt(r.amount_rub):''}</td>
    <td class="${r.type==='in'?'cin':'cout'}">${r.amount_usd?fmtd(r.amount_usd):''}</td>
    <td class="${r.type==='in'?'cin':'cout'}">${r.amount_idr?fmt(r.amount_idr):''}</td>
    <td>${esc(r.description||'')}</td><td>${esc(r.entity||'')}</td>
    <td class="acts"><button class="ib" onclick="openModal('dds',${r.id})">✏️</button><button class="ib" onclick="del('dds',${r.id})">🗑️</button></td>
  </tr>`).join('');
}

async function del(type,id){
  if(!confirm('Удалить запись?'))return;
  await api('DELETE',`/api/${type}/${id}`);
  loadTab();
}

function openModal(type,id){
  moType=type;moEditId=id||null;
  const names={program:'мероприятие',lip:'локацию',hotels:'отель',guests:'гостя',dds:'транзакцию',tour:'тур'};
  document.getElementById('moTitle').textContent=(id?'Редактировать ':'Добавить ')+names[type];
  let data={};
  if(id){const map={program:progData,lip:lipData,hotels:hotData,guests:gstData,dds:ddsData};data=map[type]?.find(r=>r.id==id)||{};}
  document.getElementById('moBody').innerHTML=formHtml(type,data);
  document.getElementById('moOv').classList.add('open');
}
function closeModal(){document.getElementById('moOv').classList.remove('open');moType=null;moEditId=null;}

async function submitModal(){
  const body=collectForm();
  try{
    if(moType==='tour'){
      const t=await api('POST','/api/tours',body);
      await loadTours();activeTid=t.id;document.getElementById('tourSel').value=t.id;updateInfo();loadTab();
    }else if(moEditId){
      const ep={program:'program',lip:'locations',hotels:'hotels',guests:'guests',dds:'dds'};
      await api('PUT',`/api/${ep[moType]}/${moEditId}`,body);
    }else{
      const ep={program:`tours/${activeTid}/program`,lip:`tours/${activeTid}/locations`,hotels:`tours/${activeTid}/hotels`,guests:`tours/${activeTid}/guests`,dds:`tours/${activeTid}/dds`};
      await api('POST',`/api/${ep[moType]}`,body);
    }
    closeModal();loadTab();
  }catch(e){alert('Ошибка: '+e.message);}
}

function collectForm(){
  const data={};
  document.getElementById('moBody').querySelectorAll('[name]').forEach(el=>{
    const v=el.value.trim();
    const nums=['day_num','cost_fixed','cost_variable','cost_team','cost_extra','total_cost','prepaid','our_price','amount_rub','amount_usd','amount_idr','rating','guests_count'];
    data[el.name]=nums.includes(el.name)?(v?parseFloat(v):0):(v||null);
  });
  return data;
}

function gv(d,f,def){return d[f]!=null&&d[f]!==''?d[f]:(def!=null?def:'');}

function formHtml(type,d){
  if(type==='tour')return `
    <div class="fr"><label>Название *</label><input name="name" value="${esc(gv(d,'name'))}" placeholder="Северная Осетия август 2025"></div>
    <div class="fr"><label>Направление</label><input name="destination" value="${esc(gv(d,'destination'))}" placeholder="Владикавказ, Казбеги…"></div>
    <div class="fr2">
      <div class="fr"><label>Дата начала</label><input type="date" name="date_start" value="${gv(d,'date_start')}"></div>
      <div class="fr"><label>Дата окончания</label><input type="date" name="date_end" value="${gv(d,'date_end')}"></div>
    </div>
    <div class="fr2">
      <div class="fr"><label>Кол-во гостей</label><input type="number" name="guests_count" value="${gv(d,'guests_count',0)}"></div>
      <div class="fr"><label>Статус</label><select name="status">
        <option value="planning"${gv(d,'status')==='planning'?' selected':''}>🗒️ Планирование</option>
        <option value="active"${gv(d,'status')==='active'?' selected':''}>✅ Активный</option>
        <option value="done"${gv(d,'status')==='done'?' selected':''}>🏁 Завершён</option>
        <option value="cancelled"${gv(d,'status')==='cancelled'?' selected':''}>❌ Отменён</option>
      </select></div>
    </div>`;

  if(type==='program')return `
    <div class="fr"><label>Название *</label><input name="title" value="${esc(gv(d,'title'))}" placeholder="Поездка на Казбек…"></div>
    <div class="fr3">
      <div class="fr"><label>День №</label><input type="number" name="day_num" value="${gv(d,'day_num')}" min="1"></div>
      <div class="fr"><label>Дата</label><input type="date" name="date" value="${gv(d,'date')}"></div>
      <div class="fr"><label>Время</label><input type="time" name="time" value="${gv(d,'time')}"></div>
    </div>
    <div class="fr"><label>Эмодзи</label><input name="emoji" value="${esc(gv(d,'emoji','🔥'))}"></div>
    <div class="fr2">
      <div class="fr"><label>Фикс. расходы ₽</label><input type="number" name="cost_fixed" value="${gv(d,'cost_fixed',0)}" step="0.01"></div>
      <div class="fr"><label>Перем. расходы ₽</label><input type="number" name="cost_variable" value="${gv(d,'cost_variable',0)}" step="0.01"></div>
    </div>
    <div class="fr2">
      <div class="fr"><label>Командные ₽</label><input type="number" name="cost_team" value="${gv(d,'cost_team',0)}" step="0.01"></div>
      <div class="fr"><label>Доп. расходы ₽</label><input type="number" name="cost_extra" value="${gv(d,'cost_extra',0)}" step="0.01"></div>
    </div>
    <div class="fr"><label>Задачи</label><textarea name="tasks">${esc(gv(d,'tasks'))}</textarea></div>
    <div class="fr"><label>Требования к подрядчику</label><textarea name="contractor_req">${esc(gv(d,'contractor_req'))}</textarea></div>`;

  if(type==='lip')return `
    <div class="fr"><label>Название *</label><input name="name" value="${esc(gv(d,'name'))}" placeholder="Цей Донифарс, Горный приют…"></div>
    <div class="fr2">
      <div class="fr"><label>Регион</label><input name="region" value="${esc(gv(d,'region'))}" placeholder="Дигория, Казбек…"></div>
      <div class="fr"><label>Категория</label><input name="category" value="${esc(gv(d,'category'))}" placeholder="Треккинг, Еда…"></div>
    </div>
    <div class="fr"><label>Статус</label><select name="status">
      <option value="✅"${gv(d,'status')==='✅'?' selected':''}>✅ Подходит</option>
      <option value="2️⃣"${gv(d,'status','2️⃣')==='2️⃣'?' selected':''}>2️⃣ Резерв</option>
      <option value="❗"${gv(d,'status')==='❗'?' selected':''}>❗ Разведать</option>
      <option value="❌"${gv(d,'status')==='❌'?' selected':''}>❌ Не берём</option>
      <option value="—"${gv(d,'status')==='—'?' selected':''}>— Не подходит</option>
    </select></div>
    <div class="fr2">
      <div class="fr"><label>Часы работы</label><input name="hours" value="${esc(gv(d,'hours'))}" placeholder="9:00–18:00"></div>
      <div class="fr"><label>Стоимость</label><input name="cost" value="${esc(gv(d,'cost'))}" placeholder="500 ₽/чел"></div>
    </div>
    <div class="fr"><label>Контакты</label><input name="contacts" value="${esc(gv(d,'contacts'))}" placeholder="+7 900 000-00-00"></div>
    <div class="fr"><label>Заметки</label><textarea name="notes">${esc(gv(d,'notes'))}</textarea></div>
    <div class="fr"><label>Google Maps</label><input name="maps_link" value="${esc(gv(d,'maps_link'))}"></div>
    <div class="fr3">
      <div class="fr"><label>Сайт</label><input name="website" value="${esc(gv(d,'website'))}"></div>
      <div class="fr"><label>YouTube</label><input name="youtube" value="${esc(gv(d,'youtube'))}"></div>
      <div class="fr"><label>Instagram</label><input name="instagram" value="${esc(gv(d,'instagram'))}"></div>
    </div>`;

  if(type==='hotels')return `
    <div class="fr"><label>Название *</label><input name="name" value="${esc(gv(d,'name'))}" placeholder="Гостиница Алания…"></div>
    <div class="fr2">
      <div class="fr"><label>Регион</label><input name="region" value="${esc(gv(d,'region'))}"></div>
      <div class="fr"><label>Рейтинг (1-5)</label><input type="number" name="rating" value="${gv(d,'rating')}" min="1" max="5" step="0.1"></div>
    </div>
    <div class="fr"><label>Статус</label><select name="status">
      <option value="✅"${gv(d,'status')==='✅'?' selected':''}>✅ Подходит</option>
      <option value="2️⃣"${gv(d,'status','2️⃣')==='2️⃣'?' selected':''}>2️⃣ Резерв</option>
      <option value="❗"${gv(d,'status')==='❗'?' selected':''}>❗ Разведать</option>
      <option value="❌"${gv(d,'status')==='❌'?' selected':''}>❌ Не берём</option>
    </select></div>
    <div class="fr"><label>Информация о номерах</label><textarea name="rooms_info">${esc(gv(d,'rooms_info'))}</textarea></div>
    <div class="fr2">
      <div class="fr"><label>Цена Booking</label><input name="booking_cost" value="${esc(gv(d,'booking_cost'))}"></div>
      <div class="fr"><label>Наша цена</label><input name="our_cost" value="${esc(gv(d,'our_cost'))}"></div>
    </div>
    <div class="fr"><label>Контакты</label><input name="contacts" value="${esc(gv(d,'contacts'))}"></div>
    <div class="fr"><label>Заметки</label><textarea name="notes">${esc(gv(d,'notes'))}</textarea></div>
    <div class="fr2">
      <div class="fr"><label>Google Maps</label><input name="maps_link" value="${esc(gv(d,'maps_link'))}"></div>
      <div class="fr"><label>Ссылка Booking</label><input name="booking_link" value="${esc(gv(d,'booking_link'))}"></div>
    </div>`;

  if(type==='guests')return `
    <div class="fr"><label>Имя *</label><input name="name" value="${esc(gv(d,'name'))}" placeholder="Иван Иванов"></div>
    <div class="fr3">
      <div class="fr"><label>Полная ст-ть ₽</label><input type="number" name="total_cost" value="${gv(d,'total_cost',0)}" step="0.01"></div>
      <div class="fr"><label>Предоплата ₽</label><input type="number" name="prepaid" value="${gv(d,'prepaid',0)}" step="0.01"></div>
      <div class="fr"><label>Наша цена ₽</label><input type="number" name="our_price" value="${gv(d,'our_price',0)}" step="0.01"></div>
    </div>
    <div class="fr"><label>Статус оплаты</label><select name="status">
      <option value="not_paid"${gv(d,'status','not_paid')==='not_paid'?' selected':''}>❌ Не оплачено</option>
      <option value="partial"${gv(d,'status')==='partial'?' selected':''}>🔶 Частично</option>
      <option value="paid"${gv(d,'status')==='paid'?' selected':''}>✅ Оплачено</option>
      <option value="refund"${gv(d,'status')==='refund'?' selected':''}>↩️ Возврат</option>
    </select></div>
    <div class="fr"><label>Заметки</label><textarea name="notes">${esc(gv(d,'notes'))}</textarea></div>`;

  if(type==='dds')return `
    <div class="fr"><label>Тип</label><select name="type">
      <option value="out"${gv(d,'type','out')==='out'?' selected':''}>⬇️ Расход</option>
      <option value="in"${gv(d,'type')==='in'?' selected':''}>⬆️ Приход</option>
    </select></div>
    <div class="fr"><label>Дата</label><input type="date" name="date" value="${gv(d,'date',new Date().toISOString().slice(0,10))}"></div>
    <div class="fr"><label>Описание</label><input name="description" value="${esc(gv(d,'description'))}" placeholder="Оплата водителя, Аренда жилья…"></div>
    <div class="fr"><label>Контрагент</label><input name="entity" value="${esc(gv(d,'entity'))}" placeholder="ИП Иванов…"></div>
    <div class="fr3">
      <div class="fr"><label>₽ Рубли</label><input type="number" name="amount_rub" value="${gv(d,'amount_rub',0)}" step="0.01"></div>
      <div class="fr"><label>$ Доллары</label><input type="number" name="amount_usd" value="${gv(d,'amount_usd',0)}" step="0.01"></div>
      <div class="fr"><label>Rp Рупии</label><input type="number" name="amount_idr" value="${gv(d,'amount_idr',0)}" step="1"></div>
    </div>`;
  return '<p>Неизвестный тип</p>';
}

function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
function fmt(n){return Number(n||0).toLocaleString('ru-RU',{maximumFractionDigits:0});}
function fmtd(n){return Number(n||0).toLocaleString('ru-RU',{minimumFractionDigits:2,maximumFractionDigits:2});}
function fillSel(id,opts,ph){const s=document.getElementById(id),cur=s.value;s.innerHTML=`<option value="">${ph}</option>`+opts.map(o=>`<option value="${esc(o)}">${esc(o)}</option>`).join('');if(cur)s.value=cur;}
function sBdg(s){const m={'✅':'bg','2️⃣':'bb','❗':'by','❌':'br','—':'bm'};return `<span class="bdg ${m[s]||'bm'}">${s||'—'}</span>`;}
function gBdg(s){const m={paid:'bg',partial:'by',not_paid:'br',refund:'bb'};const l={paid:'✅ Оплачено',partial:'🔶 Частично',not_paid:'❌ Не оплачено',refund:'↩️ Возврат'};return `<span class="bdg ${m[s]||'bm'}">${l[s]||s||'—'}</span>`;}

loadTours();
setInterval(()=>{if(activeTid)loadTab();},30000);
</script>
</body>
</html>"""

# ── Web server ─────────────────────────────────────────────────────────────────
async def serve_app(req: web.Request):
    tok = req.rel_url.query.get("token", "")
    if not tok or not tok.isdigit() or not _is_admin(int(tok)):
        return web.Response(
            text="<h1 style='font-family:sans-serif;color:#ef4444'>403 Forbidden</h1><p>Invalid token.</p>",
            content_type="text/html", status=403,
        )
    return web.Response(text=HTML_APP, content_type="text/html")

def build_web_app():
    app = web.Application()
    app.router.add_get("/app", serve_app)
    app.router.add_get("/api/tours", api_tours_get)
    app.router.add_post("/api/tours", api_tours_post)
    app.router.add_put("/api/tours/{id}", api_tour_put)
    app.router.add_delete("/api/tours/{id}", api_tour_delete)
    app.router.add_get("/api/tours/{tour_id}/program", prog_get)
    app.router.add_post("/api/tours/{tour_id}/program", prog_post)
    app.router.add_put("/api/program/{id}", prog_put)
    app.router.add_delete("/api/program/{id}", prog_del)
    app.router.add_get("/api/tours/{tour_id}/locations", loc_get)
    app.router.add_post("/api/tours/{tour_id}/locations", loc_post)
    app.router.add_put("/api/locations/{id}", loc_put)
    app.router.add_delete("/api/locations/{id}", loc_del)
    app.router.add_get("/api/tours/{tour_id}/hotels", hot_get)
    app.router.add_post("/api/tours/{tour_id}/hotels", hot_post)
    app.router.add_put("/api/hotels/{id}", hot_put)
    app.router.add_delete("/api/hotels/{id}", hot_del)
    app.router.add_get("/api/tours/{tour_id}/guests", gst_get)
    app.router.add_post("/api/tours/{tour_id}/guests", gst_post)
    app.router.add_put("/api/guests/{id}", gst_put)
    app.router.add_delete("/api/guests/{id}", gst_del)
    app.router.add_get("/api/tours/{tour_id}/dds", dds_get)
    app.router.add_post("/api/tours/{tour_id}/dds", dds_post)
    app.router.add_put("/api/dds/{id}", dds_put)
    app.router.add_delete("/api/dds/{id}", dds_del)
    return app

# ── Main ───────────────────────────────────────────────────────────────────────
async def main():
    await init_db()

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    web_app = build_web_app()
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Web: {BASE_URL}")

    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    asyncio.run(main())
