# TEMPLATE: manager_secretary
# USE FOR: virtual assistant, FAQ bot, lead collection, менеджер, секретарь, консультант
# CUSTOMIZE: sections marked with # CUSTOMIZE
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
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
BOT_DESCRIPTION = "Виртуальный менеджер-секретарь: отвечает на вопросы, записывает заявки и передаёт их команде."
WELCOME_TEXT = (
    "👋 <b>Добро пожаловать!</b>\n\n"
    "Я — ваш виртуальный помощник.\n"
    "Отвечаю на вопросы и передаю заявки менеджерам.\n\n"
    "Выберите действие:"
)
ADMIN_NEW_LEAD = "🔔 <b>Новая заявка!</b>\n"
FAQS = [
    ("Как с вами связаться?",       "📞 Звоните: +7 (999) 000-00-00\n✉️ Пишите: info@example.com\nПн–Пт 9:00–18:00"),
    ("Какие у вас цены?",           "💰 Прайс зависит от услуги. Оставьте заявку и мы пришлём актуальные цены."),
    ("Где вы находитесь?",          "📍 г. Москва, ул. Примерная, д. 1\nМетро: Красная площадь"),
    ("Как оставить заявку?",        "Нажмите кнопку <b>📝 Оставить заявку</b> — мы перезвоним в течение часа."),
    ("Работаете ли вы в выходные?", "🗓 Суббота 10:00–16:00. Воскресенье — выходной."),
]
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


# ── admin helpers ─────────────────────────────────────────────────────────────

def _load_admins() -> set:
    try:
        return set(json.loads(ADMINS_FILE.read_text()).get("ids", []))
    except Exception:
        return set()

def _save_admins(ids: set) -> None:
    ADMINS_FILE.write_text(json.dumps({"ids": list(ids)}, ensure_ascii=False))


# ── phone normalizer ──────────────────────────────────────────────────────────

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
            CREATE TABLE IF NOT EXISTS leads (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT,
                phone      TEXT,
                question   TEXT,
                status     TEXT DEFAULT 'new',
                created_at TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS faqs (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                question   TEXT NOT NULL,
                answer     TEXT NOT NULL,
                sort_order INTEGER DEFAULT 0
            )
        """)
        await db.commit()
    await _seed_faqs()


async def _seed_faqs():
    async with aiosqlite.connect(DB_PATH) as db:
        count = (await (await db.execute("SELECT COUNT(*) FROM faqs")).fetchone())[0]
        if count == 0:
            for i, (q, a) in enumerate(FAQS):
                await db.execute("INSERT INTO faqs (question, answer, sort_order) VALUES (?,?,?)", (q, a, i))
            await db.commit()


# ── keyboards ─────────────────────────────────────────────────────────────────

def kb_main() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="❓ Частые вопросы"), KeyboardButton(text="📝 Оставить заявку")],
        [KeyboardButton(text="📞 Контакты"),       KeyboardButton(text="ℹ️ О нас")],
    ], resize_keyboard=True)

def kb_admin() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="❓ Частые вопросы"), KeyboardButton(text="📝 Оставить заявку")],
        [KeyboardButton(text="📞 Контакты"),       KeyboardButton(text="ℹ️ О нас")],
        [KeyboardButton(text="📋 Заявки"),         KeyboardButton(text="📊 Статистика")],
    ], resize_keyboard=True)

async def kb_faqs() -> InlineKeyboardMarkup:
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await (await db.execute("SELECT id, question FROM faqs ORDER BY sort_order")).fetchall()
    btns = [[InlineKeyboardButton(text=q[:60], callback_data=f"faq:{fid}")] for fid, q in rows]
    return InlineKeyboardMarkup(inline_keyboard=btns)

def kb_lead_status(lead_id: int, current: str) -> InlineKeyboardMarkup:
    statuses = [("🆕 Новая","new"), ("📞 Перезвонили","called"), ("✅ Закрыта","done")]
    btns = []
    for label, s in statuses:
        marker = "▶ " if s == current else ""
        btns.append(InlineKeyboardButton(text=marker+label, callback_data=f"lead_status:{lead_id}:{s}"))
    return InlineKeyboardMarkup(inline_keyboard=[btns])


# ── FSM ───────────────────────────────────────────────────────────────────────

class LeadFlow(StatesGroup):
    name = State(); phone = State(); question = State()


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


# ── FAQ ───────────────────────────────────────────────────────────────────────

@router.message(F.text == "❓ Частые вопросы")
async def show_faqs(msg: Message):
    await msg.answer("❓ <b>Частые вопросы:</b>\nВыберите вопрос:", parse_mode="HTML",
                     reply_markup=await kb_faqs())

@router.callback_query(F.data.startswith("faq:"))
async def cb_faq(cb: CallbackQuery):
    await cb.answer()
    faq_id = int(cb.data.split(":")[1])
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute("SELECT question, answer FROM faqs WHERE id=?", (faq_id,))).fetchone()
    if not row:
        await cb.message.answer("Вопрос не найден."); return
    q, a = row
    await cb.message.answer(f"❓ <b>{q}</b>\n\n{a}", parse_mode="HTML",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                                 InlineKeyboardButton(text="◀️ Все вопросы", callback_data="faq_back"),
                                 InlineKeyboardButton(text="📝 Оставить заявку", callback_data="lead_start"),
                             ]]))

@router.callback_query(F.data == "faq_back")
async def cb_faq_back(cb: CallbackQuery):
    await cb.answer()
    await cb.message.edit_text("❓ <b>Частые вопросы:</b>\nВыберите вопрос:",
                                parse_mode="HTML", reply_markup=await kb_faqs())


# ── LEAD FLOW ─────────────────────────────────────────────────────────────────

@router.message(F.text == "📝 Оставить заявку")
async def lead_start_msg(msg: Message, state: FSMContext):
    await state.set_state(LeadFlow.name)
    await msg.answer("📝 <b>Оставить заявку</b>\n\n👤 Введите ваше имя:", parse_mode="HTML")

@router.callback_query(F.data == "lead_start")
async def lead_start_cb(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(LeadFlow.name)
    await cb.message.answer("📝 <b>Оставить заявку</b>\n\n👤 Введите ваше имя:", parse_mode="HTML")

@router.message(LeadFlow.name, F.text)
async def lead_name(msg: Message, state: FSMContext):
    await state.update_data(name=msg.text.strip())
    await state.set_state(LeadFlow.phone)
    await msg.answer("📱 Введите номер телефона (мы перезвоним в течение часа):")

@router.message(LeadFlow.phone, F.text)
async def lead_phone(msg: Message, state: FSMContext):
    phone = _normalize_phone(msg.text)
    if not phone:
        await msg.answer("⚠️ Неверный формат. Введите номер в формате +7 999 000-00-00:"); return
    await state.update_data(phone=phone)
    await state.set_state(LeadFlow.question)
    await msg.answer("💬 Кратко опишите ваш вопрос / запрос (или нажмите /skip):")

@router.message(LeadFlow.question, F.text)
async def lead_question(msg: Message, state: FSMContext, bot: Bot):
    question = None if msg.text.strip() == "/skip" else msg.text.strip()
    await _save_lead(msg, state, bot, question)

@router.message(Command("skip"), LeadFlow.question)
async def lead_skip(msg: Message, state: FSMContext, bot: Bot):
    await _save_lead(msg, state, bot, None)

async def _save_lead(msg: Message, state: FSMContext, bot: Bot, question):
    data = await state.get_data()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO leads (name, phone, question) VALUES (?,?,?)",
            (data.get("name"), data.get("phone"), question)
        )
        lead_id = cur.lastrowid
        await db.commit()
    await state.clear()
    await msg.answer(
        f"✅ <b>Заявка принята!</b>\n\n"
        f"👤 {data.get('name')}\n"
        f"📱 {data.get('phone')}\n\n"
        f"Мы свяжемся с вами в ближайшее время.",
        parse_mode="HTML", reply_markup=kb_main()
    )
    notify = (
        ADMIN_NEW_LEAD +
        f"👤 {data.get('name')}\n📱 {data.get('phone')}" +
        (f"\n💬 {question}" if question else "")
    )
    for admin_id in _load_admins():
        try:
            await bot.send_message(int(admin_id), notify, parse_mode="HTML",
                                   reply_markup=kb_lead_status(lead_id, "new"))
        except Exception:
            pass


# ── CONTACTS / ABOUT ──────────────────────────────────────────────────────────

@router.message(F.text == "📞 Контакты")
async def show_contacts(msg: Message):
    await msg.answer(
        "📞 <b>Контакты</b>\n\n"
        "☎️ +7 (999) 000-00-00\n"
        "✉️ info@example.com\n"
        "🌐 example.com\n\n"
        "⏰ Пн–Пт: 9:00–18:00\n"
        "📍 г. Москва, ул. Примерная, д. 1",
        parse_mode="HTML"
    )

@router.message(F.text == "ℹ️ О нас")
async def show_about(msg: Message):
    await msg.answer(
        "ℹ️ <b>О компании</b>\n\n"
        "Мы — команда профессионалов, специализирующихся на ...\n\n"
        "🏆 Опыт работы: 10+ лет\n"
        "✅ Довольных клиентов: 1000+\n\n"
        "Оставьте заявку и мы поможем!",
        parse_mode="HTML"
    )


# ── ADMIN: LEADS ──────────────────────────────────────────────────────────────

@router.message(F.text == "📋 Заявки")
async def admin_leads(msg: Message):
    if str(msg.from_user.id) not in _load_admins():
        await msg.answer("⛔ Нет доступа"); return
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT * FROM leads WHERE status != 'done' ORDER BY created_at DESC LIMIT 20"
        )).fetchall()
    if not rows:
        await msg.answer("Активных заявок нет."); return
    for r in [dict(x) for x in rows]:
        status_icon = {"new": "🆕", "called": "📞", "done": "✅"}.get(r["status"], "❓")
        text = (f"{status_icon} <b>{r['name']}</b> · {r['phone']}\n"
                + (f"💬 {r['question']}\n" if r.get("question") else "")
                + f"📅 {r['created_at'][:16]}")
        await msg.answer(text, parse_mode="HTML", reply_markup=kb_lead_status(r["id"], r["status"]))

@router.callback_query(F.data.startswith("lead_status:"))
async def cb_lead_status(cb: CallbackQuery):
    await cb.answer()
    _, lead_id_str, new_status = cb.data.split(":")
    lead_id = int(lead_id_str)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE leads SET status=? WHERE id=?", (new_status, lead_id))
        row = await (await db.execute(
            "SELECT name, phone, question, created_at FROM leads WHERE id=?", (lead_id,)
        )).fetchone()
        await db.commit()
    if row:
        name, phone, question, created_at = row
        status_icon = {"new": "🆕", "called": "📞", "done": "✅"}.get(new_status, "❓")
        text = (f"{status_icon} <b>{name}</b> · {phone}\n"
                + (f"💬 {question}\n" if question else "")
                + f"📅 {created_at[:16]}")
        await cb.message.edit_text(text, parse_mode="HTML",
                                   reply_markup=kb_lead_status(lead_id, new_status))

@router.message(F.text == "📊 Статистика")
async def admin_stats(msg: Message):
    if str(msg.from_user.id) not in _load_admins():
        await msg.answer("⛔ Нет доступа"); return
    async with aiosqlite.connect(DB_PATH) as db:
        total  = (await (await db.execute("SELECT COUNT(*) FROM leads")).fetchone())[0]
        new_c  = (await (await db.execute("SELECT COUNT(*) FROM leads WHERE status='new'")).fetchone())[0]
        called = (await (await db.execute("SELECT COUNT(*) FROM leads WHERE status='called'")).fetchone())[0]
        done   = (await (await db.execute("SELECT COUNT(*) FROM leads WHERE status='done'")).fetchone())[0]
        today  = (await (await db.execute(
            "SELECT COUNT(*) FROM leads WHERE date(created_at)=date('now','localtime')"
        )).fetchone())[0]
    await msg.answer(
        f"📊 <b>Статистика заявок</b>\n\n"
        f"📋 Всего: {total}\n"
        f"🆕 Новых: {new_c}\n"
        f"📞 Перезвонили: {called}\n"
        f"✅ Закрыто: {done}\n"
        f"📅 Сегодня: {today}",
        parse_mode="HTML"
    )


# ── FAQ ADMIN COMMANDS ────────────────────────────────────────────────────────

@router.message(Command("addfaq"))
async def cmd_addfaq(msg: Message):
    if str(msg.from_user.id) not in _load_admins(): await msg.answer("⛔ Нет доступа"); return
    parts = msg.text.split("\n", 2)
    if len(parts) < 3:
        await msg.answer("Формат:\n/addfaq\nВопрос\nОтвет"); return
    question, answer = parts[1].strip(), parts[2].strip()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO faqs (question, answer) VALUES (?,?)", (question, answer))
        await db.commit()
    await msg.answer(f"✅ FAQ добавлен: <b>{question}</b>", parse_mode="HTML")

@router.message(Command("listfaq"))
async def cmd_listfaq(msg: Message):
    if str(msg.from_user.id) not in _load_admins(): await msg.answer("⛔ Нет доступа"); return
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await (await db.execute("SELECT id, question FROM faqs ORDER BY sort_order")).fetchall()
    lines = ["📋 <b>Список FAQ:</b>"] + [f"  {fid}. {q}" for fid, q in rows]
    await msg.answer("\n".join(lines), parse_mode="HTML")

@router.message(Command("delfaq"))
async def cmd_delfaq(msg: Message):
    if str(msg.from_user.id) not in _load_admins(): await msg.answer("⛔ Нет доступа"); return
    parts = msg.text.split()
    if len(parts) < 2 or not parts[1].isdigit(): await msg.answer("Использование: /delfaq <id>"); return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM faqs WHERE id=?", (int(parts[1]),))
        await db.commit()
    await msg.answer(f"✅ FAQ #{parts[1]} удалён.")


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

@router.message(F.chat.type.in_({"group","supergroup"}), F.text)
async def handle_group_mention(msg: Message):
    if not BOT_DISPLAY_NAME or not msg.text: return
    if msg.from_user and msg.from_user.is_bot: return
    if BOT_DISPLAY_NAME.lower() not in msg.text.lower(): return
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await (await db.execute("SELECT question, answer FROM faqs ORDER BY sort_order LIMIT 5")).fetchall()
    context = "\n".join(f"Q: {q}\nA: {a}" for q, a in rows)
    from anthropic import AsyncAnthropic as _C
    resp = await _C(api_key=os.getenv("ANTHROPIC_API_KEY","")).messages.create(
        model="claude-haiku-4-5-20251001", max_tokens=200,
        system=f"Ты — {BOT_DISPLAY_NAME}. Отвечай кратко. Контекст:\n{context}",
        messages=[{"role":"user","content":msg.text}]
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
