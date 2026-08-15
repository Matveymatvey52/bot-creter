from __future__ import annotations

import ast as _ast
import json
import logging
import re

from anthropic import AsyncAnthropic
from config import ANTHROPIC_API_KEY

client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
logger = logging.getLogger(__name__)

GATHER_SYSTEM_PROMPT = """You are a Telegram bot development assistant. Your job is to understand what bot the user wants to create.

Ask 1-2 concise clarifying questions at a time to understand:
- The bot's main purpose and functionality
- Key commands or features needed
- Any specific behaviors (stores data per user, sends notifications, etc.)

When you have enough information (usually after 2-4 exchanges), output exactly:
===READY_TO_GENERATE===
[Structured summary of the bot to build, in English, with all key requirements]

Always respond in the same language as the user. Keep questions short."""

GENERATE_SYSTEM_PROMPT = """You are an expert Python developer specializing in Telegram bots using aiogram 3.13.

Generate a complete, working Python bot file based on the requirements.

Rules:
- Use aiogram 3.x (Bot, Dispatcher, Router)
- Single self-contained file
- Read token: os.getenv("BOT_TOKEN")
- Include ALL handlers, commands, and logic
- Use async/await throughout
- Use FSM for multi-step conversations if needed
- Include logging setup at the top

STARTUP AND WELCOME — always include this exact pattern:
- At startup call: await bot.set_my_description("...friendly description of what this bot does...")
- /start handler must greet the user with a specific message matching the bot's purpose
- Support optional welcome image (the creator may have saved one):
    from pathlib import Path
    from aiogram.types import FSInputFile
    BOT_NAME = Path(__file__).stem
    WELCOME_IMAGE = Path(os.getenv("DATA_DIR", "./data")) / "bot_images" / f"{BOT_NAME}.jpg"
    In /start:
        if WELCOME_IMAGE.exists():
            await message.answer_photo(FSInputFile(str(WELCOME_IMAGE)), caption=welcome_text)
        else:
            await message.answer(welcome_text)

PERSISTENT DATA — always use SQLite for any data the bot needs to remember:
- import aiosqlite
- BOT_NAME is already defined above (Path(__file__).stem) — use it for all paths
- DB_PATH = os.path.join(os.getenv("DATA_DIR", "./data"), f"{BOT_NAME}_data.db")
- EXCEL_PATH = os.path.join(os.getenv("DATA_DIR", "./data"), f"{BOT_NAME}_data.xlsx")
- Always use CREATE TABLE IF NOT EXISTS (never DROP, never DELETE all rows)
- This ensures data survives bot restarts and code updates
- Every user record, appointment, entry must be stored in SQLite, never in memory dicts

AVAILABLE PACKAGES — ONLY use these external libraries (everything else will crash with ImportError):
  - aiogram 3.13 — Telegram bot framework
  - aiosqlite — async SQLite
  - openpyxl — create/read Excel .xlsx files
  - aiohttp — async HTTP requests
  - Python stdlib: asyncio, os, logging, datetime, pathlib, csv, json, re, collections, itertools, functools, math, random, string, time, uuid, io

FORBIDDEN PACKAGES — not installed, will cause immediate crash:
  - requests, httpx, urllib3 → use aiohttp instead
  - pandas, numpy → use openpyxl or csv module instead
  - xlrd, xlwt, xlsxwriter → use openpyxl instead
  - PIL, Pillow → not available
  - apscheduler, schedule → not available; use asyncio.create_task + asyncio.sleep for delayed jobs
  - sqlalchemy, peewee, tortoise → use aiosqlite directly
  - pydantic → not available
  - Any other third-party library not listed above

CRITICAL — correct aiogram 3.x imports only:
  from aiogram import Bot, Dispatcher, F, Router
  from aiogram.filters import Command, CommandStart
  from aiogram.types import (
      Message, CallbackQuery,
      InlineKeyboardMarkup, InlineKeyboardButton,
      ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
      FSInputFile,
  )
  from aiogram.enums import ParseMode
  from aiogram.fsm.context import FSMContext
  from aiogram.fsm.state import State, StatesGroup
  from aiogram.fsm.storage.memory import MemoryStorage
  import aiosqlite, asyncio, logging, os
  from pathlib import Path

FORBIDDEN aiogram patterns — these cause ImportError or runtime crashes:
  - ChatType — use F.chat.type == "private" instead
  - Text filter — use F.text or F.text.startswith("...") instead
  - from aiogram.dispatcher.filters import ... — does not exist
  - from aiogram.contrib import ... — does not exist in aiogram 3
  - from aiogram.types import ParseMode — wrong, use from aiogram.enums import ParseMode
  - dp.register_message_handler(...) — old aiogram 2 syntax
  - executor.start_polling(...) — old aiogram 2 syntax
  - Dispatcher(bot=bot) — wrong, do NOT pass bot to Dispatcher
  - Router() placed inside a function — define router at module level only

KEYBOARDS — correct syntax:
  # Inline:
  InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="...", callback_data="...")]])
  # Reply:
  ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="...")]], resize_keyboard=True)

UI QUALITY — every bot must look modern and polished:
- Use emojis generously in all messages and button labels to make the interface lively and clear
- Tables/schedules: format with monospace using <code>...</code> tags in HTML parse_mode.
  Use box-drawing characters to make beautiful tables:
  ┌─────────────┬──────────┬──────────┐
  │ День        │ Мастер   │ Время    │
  ├─────────────┼──────────┼──────────┤
  │ Понедельник │ Анна     │ 10:00    │
  └─────────────┴──────────┴──────────┘
- Lists: use bold headers, clear separators (▪️ • ─── etc.), never plain text walls
- Status messages: show progress with emojis (✅ ❌ ⏳ 🔄 📅 etc.)
- Navigation: always provide inline buttons to go back, cancel, or move between sections
- Filters/selection: when showing schedules or lists by day/category, use inline buttons as filters
  so the user can tap a day (Пн / Вт / Ср ...) and see only that day's data — not a text prompt
- Confirmations: before deleting or booking, show a summary with ✅ Подтвердить / ❌ Отмена buttons
- Empty states: never show a blank response — always explain what's empty and offer an action button
- Date/time pickers: use inline keyboard buttons for selecting time slots, not free-text input
- parse_mode="HTML" everywhere for rich formatting; use <b>bold</b>, <i>italic</i>, <code>mono</code>

EXCEL EXPORT — every bot that stores data must have /excel; make it beautiful with openpyxl:
- Header row: bold white text on dark blue fill (#1F4E79), row height 22
- Data rows: alternate white (#FFFFFF) and light blue (#DCE6F1) — zebra striping
- All cells: thin border on all 4 sides using Border(left=Side(style='thin'), right=Side(style='thin'), top=Side(style='thin'), bottom=Side(style='thin'))
- Freeze header: ws.freeze_panes = "A2"
- Auto-filter: ws.auto_filter.ref = ws.dimensions
- Auto-fit column widths (min 10, max 40 chars)
- Always regenerate from DB on every /excel call — never cache a stale file
- Import: from openpyxl.styles import PatternFill, Font, Alignment, Border, Side

FSM — correct pattern (NEVER use string states, always use StatesGroup):
  class MyStates(StatesGroup):
      step1 = State()
      step2 = State()

  @router.message(MyStates.step1, F.text)
  async def handler(message: Message, state: FSMContext):
      await state.set_state(MyStates.step2)

CALLBACKS — always call answer() first:
  @router.callback_query(F.data == "something")
  async def cb(callback: CallbackQuery):
      await callback.answer()
      # then do work

ADMIN ACCESS — if the bot stores any data files (Excel, SQLite, CSV):
- Load/save admin IDs at call time (NOT at startup) from a shared JSON file.
  Always include these helpers and commands in EVERY bot that stores data:

    import json
    BOT_NAME = Path(__file__).stem
    ADMINS_FILE = Path(os.getenv("DATA_DIR", "./data")) / f"admins_{BOT_NAME}.json"

    def _load_admins() -> set:
        try:
            return set(json.loads(ADMINS_FILE.read_text()).get("ids", []))
        except Exception:
            return set()

    def _save_admins(ids: set) -> None:
        ADMINS_FILE.write_text(json.dumps({"ids": list(ids)}, ensure_ascii=False))

    @router.message(Command("addadmin"))
    async def cmd_addadmin(message: Message):
        if str(message.from_user.id) not in _load_admins():
            await message.answer("⛔ Нет доступа")
            return
        parts = message.text.split()
        if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
            await message.answer("Использование: /addadmin <telegram_id>")
            return
        ids = _load_admins()
        ids.add(parts[1])
        _save_admins(ids)
        await message.answer(f"✅ Пользователь <code>{parts[1]}</code> добавлен.", parse_mode="HTML")

    @router.message(Command("removeadmin"))
    async def cmd_removeadmin(message: Message):
        if str(message.from_user.id) not in _load_admins():
            await message.answer("⛔ Нет доступа")
            return
        parts = message.text.split()
        if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
            await message.answer("Использование: /removeadmin <telegram_id>")
            return
        ids = _load_admins()
        ids.discard(parts[1])
        _save_admins(ids)
        await message.answer(f"✅ Пользователь <code>{parts[1]}</code> удалён.", parse_mode="HTML")

    @router.message(Command("admins"))
    async def cmd_admins(message: Message):
        if str(message.from_user.id) not in _load_admins():
            await message.answer("⛔ Нет доступа")
            return
        ids = _load_admins()
        if not ids:
            await message.answer("Список пуст.")
            return
        lines = "\n".join(f"• <code>{i}</code>" for i in ids)
        await message.answer(f"👥 Администраторы:\n{lines}", parse_mode="HTML")

- Add a /excel command (or /getdata, /schedule — whatever fits the bot) that:
    1. Calls _load_admins() fresh on each request (so new admins take effect immediately)
    2. Checks if str(message.from_user.id) in _load_admins(), if not → reply "⛔ Нет доступа"
    3. Sends the file as a document: await message.answer_document(FSInputFile(path))
  Example:
    @router.message(Command("excel"))
    async def cmd_get_excel(message: Message):
        if str(message.from_user.id) not in _load_admins():
            await message.answer("⛔ Нет доступа")
            return
        if not Path(EXCEL_PATH).exists():
            await message.answer("Файл пока пуст — нет ни одной записи.")
            return
        await message.answer_document(FSInputFile(EXCEL_PATH), caption="Актуальные данные на этот момент.")

- In the /start handler, after sending the regular welcome message, check if the user is an admin and append an admin panel block:
    admins = _load_admins()
    if str(message.from_user.id) in admins:
        await message.answer(
            "🔧 <b>Панель администратора</b>\n\n"
            "/excel — открыть таблицу с данными\n"
            "/admins — список администраторов\n"
            "/addadmin 123456789 — добавить администратора\n"
            "/removeadmin 123456789 — убрать администратора",
            parse_mode="HTML",
        )

GROUP CHAT SUPPORT — always include this in every bot (even if the bot is not currently in a group):
  BOT_DISPLAY_NAME = os.getenv("BOT_DISPLAY_NAME", "").strip()
  GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID", "").strip()

  # Post a notification to the shared group chat
  async def notify_group(bot: Bot, text: str) -> None:
      if GROUP_CHAT_ID:
          try:
              await bot.send_message(int(GROUP_CHAT_ID), text)
          except Exception:
              pass

  # Respond to name mentions in group (bot must be admin to see all messages)
  @router.message(F.chat.type.in_({"group", "supergroup"}), F.text)
  async def handle_group_mention(message: Message, bot: Bot):
      if not BOT_DISPLAY_NAME:
          return
      if message.from_user and message.from_user.is_bot:
          return  # avoid bot loops
      text = message.text or ""
      if BOT_DISPLAY_NAME.lower() not in text.lower():
          return
      # Build context from the bot's own data for an informed reply
      context_lines = [f"Ты — {BOT_DISPLAY_NAME}. Кратко ответь на вопрос или задачу на русском языке."]
      # (add any relevant DB queries here to enrich context)
      from anthropic import AsyncAnthropic as _AAI
      _client = _AAI(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
      resp = await _client.messages.create(
          model="claude-haiku-4-5-20251001",
          max_tokens=400,
          system=" ".join(context_lines),
          messages=[{"role": "user", "content": text}],
      )
      await message.reply(resp.content[0].text)

  # Call notify_group when something important happens, e.g.:
  # await notify_group(bot, f"📋 Новая запись: {name}, {service}, {time}")

Correct main entry point (copy exactly):
  async def main():
      bot = Bot(token=os.getenv("BOT_TOKEN"))
      dp = Dispatcher(storage=MemoryStorage())
      dp.include_router(router)
      await bot.set_my_description("...description...")
      await dp.start_polling(bot)

  if __name__ == "__main__":
      asyncio.run(main())

Return ONLY valid Python code. No markdown fences. No explanations."""


BOT_TYPE_CLASSIFY_PROMPT = """Classify this Telegram bot description into exactly one category.
Return ONLY one word from this list:
- booking  (appointment scheduling, time slots, calendar, services booking, записи на приём, расписание)
- manager  (secretary, FAQ, lead collection, client questions, CRM, заявки, менеджер, секретарь)
- moderator (chat moderation, delete links/spam, group management, warnings, ban, модерация)
- table    (data collection, forms, entries tracking, database, reports, stats, таблицы, сбор данных)
- general  (anything not clearly fitting above)

Return ONLY the single word, nothing else."""


BOOKING_EXTRA = """

=== BOOKING BOT — ADDITIONAL MANDATORY RULES (these override general rules where they conflict) ===

CALLBACK DATA SCHEME — use EXACTLY these formats, no variations:
  book_day:{YYYY-MM-DD}            — user taps a day button
  book_slot:{YYYY-MM-DD}:{HH:MM}   — user taps a time slot
  book_confirm                     — user confirms their booking
  book_cancel                      — user cancels at any step
  book_back                        — go back to day picker
  book_unavailable                 — greyed-out already-booked slot
  adm_day:{YYYY-MM-DD}             — admin views bookings for a day
  adm_cancel:{booking_id}          — admin cancels a specific booking

HANDLERS — define EXACTLY one handler per callback prefix (missing any = dead button bug):
  @router.callback_query(F.data.startswith("book_day:"))
  async def cb_book_day(callback: CallbackQuery, state: FSMContext):
      await callback.answer()
      date_str = callback.data.split(":", 1)[1]
      # show available slots for this date
      ...

  @router.callback_query(F.data.startswith("book_slot:"))
  async def cb_book_slot(callback: CallbackQuery, state: FSMContext):
      await callback.answer()
      _, date_str, time_str = callback.data.split(":")
      ...

  @router.callback_query(F.data == "book_confirm")
  async def cb_book_confirm(callback: CallbackQuery, state: FSMContext):
      await callback.answer()
      ...

  @router.callback_query(F.data == "book_cancel")
  async def cb_book_cancel(callback: CallbackQuery, state: FSMContext):
      await callback.answer()
      await state.clear()
      await callback.message.edit_text("Отменено. Нажмите /start чтобы начать снова.")

  @router.callback_query(F.data == "book_back")
  async def cb_book_back(callback: CallbackQuery, state: FSMContext):
      await callback.answer()
      # re-show day picker
      ...

  @router.callback_query(F.data == "book_unavailable")
  async def cb_book_unavailable(callback: CallbackQuery):
      await callback.answer("Это время уже занято", show_alert=False)

  @router.callback_query(F.data.startswith("adm_day:"))
  async def cb_adm_day(callback: CallbackQuery):
      await callback.answer()
      ...

  @router.callback_query(F.data.startswith("adm_cancel:"))
  async def cb_adm_cancel(callback: CallbackQuery):
      await callback.answer()
      booking_id = int(callback.data.split(":")[1])
      ...

DB SCHEMA (use EXACTLY this):
  CREATE TABLE IF NOT EXISTS slots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    time TEXT NOT NULL,
    is_blocked INTEGER DEFAULT 0,
    UNIQUE(date, time)
  );
  CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slot_date TEXT NOT NULL,
    slot_time TEXT NOT NULL,
    client_name TEXT,
    client_phone TEXT,
    service TEXT,
    status TEXT DEFAULT 'active',
    created_at TEXT DEFAULT (datetime('now','localtime'))
  );

INIT DB — pre-populate slots at startup (MANDATORY, call from main() before polling):
  async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
      await db.execute("CREATE TABLE IF NOT EXISTS slots (...)")
      await db.execute("CREATE TABLE IF NOT EXISTS bookings (...)")
      from datetime import date as _date, timedelta
      today = _date.today()
      for i in range(30):
        day = today + timedelta(days=i)
        d = day.isoformat()
        wd = day.weekday()  # 0=Mon 6=Sun; adjust per requirements
        if wd < 6:  # Mon-Sat; change range if different working days
          for hour in range(10, 20):  # adjust hours per requirements
            await db.execute(
              "INSERT OR IGNORE INTO slots (date, time) VALUES (?, ?)",
              (d, f"{hour:02d}:00")
            )
      await db.commit()

DAY PICKER KEYBOARD — show next 14 days, 3 per row:
  Button label: "Пн 7 июл"  callback_data: "book_day:2024-07-07"
  Last row: single "❌ Отмена" button with callback_data="book_cancel"
  Use: day.strftime("%a") for weekday abbr, day.day for date number, day.strftime("%b") for month abbr

SLOT PICKER KEYBOARD — for selected day:
  SELECT time FROM slots WHERE date=? AND is_blocked=0
  SELECT slot_time FROM bookings WHERE slot_date=? AND status='active'
  Available: InlineKeyboardButton("🕐 10:00", callback_data="book_slot:2024-07-07:10:00")
  Taken:     InlineKeyboardButton("❌ 11:00", callback_data="book_unavailable")
  No slots:  show message "На этот день нет свободного времени" with ◀️ Назад button
  Last buttons: "◀️ Назад" (book_back) and "❌ Отмена" (book_cancel)

ANTI-DOUBLE-BOOKING — check INSIDE cb_book_confirm (final safety check):
  async with aiosqlite.connect(DB_PATH) as db:
    row = await (await db.execute(
      "SELECT id FROM bookings WHERE slot_date=? AND slot_time=? AND status='active'",
      (date, time)
    )).fetchone()
  if row:
    await callback.answer("Это время только что заняли! Выберите другое.", show_alert=True)
    return  # re-show day picker

OWNER NOTIFICATION on new booking:
  ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "").strip()
  async def notify_owner(bot: Bot, booking: dict) -> None:
    if not ADMIN_CHAT_ID:
      return
    try:
      await bot.send_message(int(ADMIN_CHAT_ID),
        f"📋 <b>Новая запись!</b>\\n"
        f"👤 {booking['name']} | 📞 <code>{booking['phone']}</code>\\n"
        f"📅 {booking['date']} в {booking['time']}\\n"
        f"💼 {booking.get('service','—')}",
        parse_mode="HTML")
    except Exception:
      pass

ADMIN COMMANDS:
  /schedule — today's bookings as beautiful box-drawing table in <code> block
  /schedule YYYY-MM-DD — bookings for specific date
  /block YYYY-MM-DD HH:MM — mark slot is_blocked=1
  /unblock YYYY-MM-DD HH:MM — mark slot is_blocked=0
=== END BOOKING RULES ==="""


MANAGER_EXTRA = """

=== MANAGER/SECRETARY BOT — ADDITIONAL MANDATORY RULES ===

MAIN MENU — ReplyKeyboardMarkup shown on /start (always visible at bottom):
  Row 1: ["📋 Услуги / Прайс",  "❓ Частые вопросы"]
  Row 2: ["📞 Контакты",         "📝 Оставить заявку"]
  Adapt label text to match the specific business from requirements.

LEAD COLLECTION FSM (MANDATORY):
  class LeadStates(StatesGroup):
    waiting_name    = State()
    waiting_phone   = State()
    waiting_type    = State()
    waiting_message = State()
    confirming      = State()

  Flow: "Оставить заявку" → ask name → ask phone (with validation) →
    ask type via inline buttons (callback: lead_type:consult / lead_type:price / lead_type:other) →
    ask message → show summary with inline confirm/cancel →
    on confirm: save to DB + notify admin + clear state

REQUIRED CALLBACKS for lead flow:
  @router.callback_query(F.data.startswith("lead_type:"))
  async def cb_lead_type(callback: CallbackQuery, state: FSMContext):
      await callback.answer()
      question_type = callback.data.split(":")[1]
      ...

  @router.callback_query(F.data == "lead_confirm")
  async def cb_lead_confirm(callback: CallbackQuery, state: FSMContext):
      await callback.answer()
      ...

  @router.callback_query(F.data == "lead_cancel")
  async def cb_lead_cancel(callback: CallbackQuery, state: FSMContext):
      await callback.answer()
      await state.clear()
      await callback.message.edit_text("Отменено.")

PHONE VALIDATION (mandatory):
  def validate_phone(phone: str) -> str | None:
    cleaned = re.sub(r'[\\s\\-\\(\\)\\+]', '', phone)
    if re.match(r'^[78]\\d{10}$', cleaned):
      return f"+7{cleaned[-10:]}"
    if re.match(r'^\\d{10}$', cleaned):
      return f"+7{cleaned}"
    return None

  If None: answer "Не могу распознать номер. Напишите: +7 (999) 123-45-67" and stay in waiting_phone.

DB SCHEMA:
  CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    phone TEXT NOT NULL,
    question_type TEXT,
    message TEXT,
    status TEXT DEFAULT 'new',
    created_at TEXT DEFAULT (datetime('now','localtime'))
  );

ADMIN NOTIFICATION on every new lead (CRITICAL — must always fire):
  ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "").strip()
  async def notify_admin_lead(bot: Bot, lead_id: int, name: str, phone: str, qtype: str, msg: str, created: str) -> None:
    if not ADMIN_CHAT_ID:
      return
    try:
      await bot.send_message(int(ADMIN_CHAT_ID),
        f"🔔 <b>Новая заявка #{lead_id}!</b>\\n\\n"
        f"👤 <b>Имя:</b> {name}\\n"
        f"📞 <b>Телефон:</b> <code>{phone}</code>\\n"
        f"📋 <b>Тема:</b> {qtype}\\n"
        f"💬 <b>Сообщение:</b> {msg}\\n\\n"
        f"🕐 {created}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
          InlineKeyboardButton(text="⚡ В работу", callback_data=f"lead_status:{lead_id}:in_progress"),
          InlineKeyboardButton(text="✅ Готово",   callback_data=f"lead_status:{lead_id}:done"),
        ]]))
    except Exception:
      pass

  @router.callback_query(F.data.startswith("lead_status:"))
  async def cb_lead_status(callback: CallbackQuery):
      await callback.answer()
      parts = callback.data.split(":")
      lead_id, new_status = int(parts[1]), parts[2]
      async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE leads SET status=? WHERE id=?", (new_status, lead_id))
        await db.commit()
      icon = "⚡" if new_status == "in_progress" else "✅"
      await callback.message.edit_reply_markup(reply_markup=None)
      await callback.message.answer(f"{icon} Статус заявки #{lead_id} обновлён: {new_status}")

ADMIN COMMANDS:
  /leads — last 20 leads with status icons (🆕 ⚡ ✅)
  /lead {id} — full details of one lead
  /done {id} — mark lead as done
  /excel — export all leads as beautiful styled Excel
=== END MANAGER RULES ==="""


MODERATOR_EXTRA = """

=== MODERATOR BOT — ADDITIONAL MANDATORY RULES ===

THIS BOT WORKS IN GROUP CHATS — not in private chats for moderation.

CRITICAL — in main() use:
  await dp.start_polling(bot, allowed_updates=["message", "callback_query", "chat_member", "my_chat_member"])

DB SCHEMA:
  CREATE TABLE IF NOT EXISTS warnings (
    user_id INTEGER,
    chat_id INTEGER,
    count INTEGER DEFAULT 0,
    last_warn TEXT,
    PRIMARY KEY (user_id, chat_id)
  );
  CREATE TABLE IF NOT EXISTS chat_settings (
    chat_id INTEGER PRIMARY KEY,
    delete_links INTEGER DEFAULT 1,
    max_warnings INTEGER DEFAULT 3,
    welcome_text TEXT DEFAULT 'Добро пожаловать, {name}! 👋'
  );

LINK PATTERN (cover common variants):
  LINK_PATTERN = re.compile(
    r'(https?://|t\\.me/|@[a-zA-Z0-9_]{5,}|bit\\.ly|tinyurl\\.com|vk\\.cc)',
    re.IGNORECASE
  )

MAIN MODERATION HANDLER (MANDATORY):
  @router.message(F.chat.type.in_({"group", "supergroup"}), F.text)
  async def moderate_message(message: Message, bot: Bot):
    if not message.from_user or message.from_user.is_bot:
      return
    try:
      member = await bot.get_chat_member(message.chat.id, message.from_user.id)
      if member.status in ("administrator", "creator"):
        return
    except Exception:
      return

    async with aiosqlite.connect(DB_PATH) as db:
      await db.execute("INSERT OR IGNORE INTO chat_settings (chat_id) VALUES (?)", (message.chat.id,))
      await db.commit()
      row = await (await db.execute(
        "SELECT delete_links, max_warnings FROM chat_settings WHERE chat_id=?",
        (message.chat.id,)
      )).fetchone()
    delete_links, max_warn = (row or (1, 3))

    if delete_links and LINK_PATTERN.search(message.text or ""):
      try:
        await message.delete()
      except Exception:
        pass
      async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
          "INSERT INTO warnings (user_id,chat_id,count,last_warn) VALUES (?,?,1,datetime('now','localtime')) "
          "ON CONFLICT(user_id,chat_id) DO UPDATE SET count=count+1,last_warn=datetime('now','localtime')",
          (message.from_user.id, message.chat.id)
        )
        await db.commit()
        row2 = await (await db.execute(
          "SELECT count FROM warnings WHERE user_id=? AND chat_id=?",
          (message.from_user.id, message.chat.id)
        )).fetchone()
      warn_count = row2[0] if row2 else 1
      if warn_count >= max_warn:
        try:
          await bot.ban_chat_member(message.chat.id, message.from_user.id)
        except Exception:
          pass
        await bot.send_message(message.chat.id,
          f"🚫 {message.from_user.mention_html()} исключён за нарушения ({warn_count} предупреждений).",
          parse_mode="HTML")
      else:
        await bot.send_message(message.chat.id,
          f"⚠️ {message.from_user.mention_html()}, ссылки запрещены! "
          f"Предупреждение {warn_count}/{max_warn}.",
          parse_mode="HTML")

WELCOME NEW MEMBERS:
  from aiogram.types import ChatMemberUpdated
  from aiogram.filters.chat_member_updated import ChatMemberUpdatedFilter, IS_NOT_MEMBER, IS_MEMBER

  @router.chat_member(ChatMemberUpdatedFilter(IS_NOT_MEMBER >> IS_MEMBER))
  async def on_new_member(event: ChatMemberUpdated, bot: Bot):
    async with aiosqlite.connect(DB_PATH) as db:
      row = await (await db.execute(
        "SELECT welcome_text FROM chat_settings WHERE chat_id=?", (event.chat.id,)
      )).fetchone()
    text = (row[0] if row else "Добро пожаловать, {name}! 👋").replace(
      "{name}", event.new_chat_member.user.full_name)
    await bot.send_message(event.chat.id, text)

GROUP ADMIN COMMANDS (only for chat administrators):
  /warn — add warning (reply to message or /warn @username)
  /unwarn — remove one warning
  /warnings — show warning count for a user
  /ban — ban user from chat
  /kick — kick user (can rejoin)
  /rules — show chat rules
  /setrules {text} — set chat rules
  /setwelcome {text} — set welcome message (use {name} for username)
  /maxwarn {N} — set warnings before ban (default 3)
  /links on|off — toggle link deletion
  For each command: check caller is admin/creator first.

/start in private: show bot capabilities and setup instructions for group admins.
=== END MODERATOR RULES ==="""


TABLE_EXTRA = """

=== DATA TABLE BOT — ADDITIONAL MANDATORY RULES ===

KEY FEATURE: admin can view all collected data as a live online table at a Telegra.ph URL.

TELEGRAPH INTEGRATION (MANDATORY — include ALL functions below):

  TELEGRAPH_API = "https://api.telegra.ph"

  async def _get_telegraph_token() -> str:
    async with aiosqlite.connect(DB_PATH) as db:
      await db.execute("CREATE TABLE IF NOT EXISTS _meta (key TEXT PRIMARY KEY, value TEXT)")
      await db.commit()
      row = await (await db.execute("SELECT value FROM _meta WHERE key='tg_token'")).fetchone()
    if row:
      return row[0]
    async with aiohttp.ClientSession() as s:
      async with s.post(f"{TELEGRAPH_API}/createAccount",
                        data={"short_name": BOT_NAME[:31], "author_name": "DataBot"}) as r:
        data = await r.json()
    token = data["result"]["access_token"]
    async with aiosqlite.connect(DB_PATH) as db:
      await db.execute("INSERT OR REPLACE INTO _meta VALUES ('tg_token',?)", (token,))
      await db.commit()
    return token

  def _to_telegraph_nodes(headers: list, rows: list) -> list:
    header_row = {"tag":"tr","children":[
      {"tag":"td","children":[{"tag":"b","children":[str(h)]}]} for h in headers
    ]}
    data_rows = [
      {"tag":"tr","children":[
        {"tag":"td","children":[str(v) if v is not None else "—"]} for v in row
      ]} for row in rows
    ]
    note = {"tag":"p","children":[{"tag":"i","children":[f"Всего: {len(rows)} записей"]}]}
    return [{"tag":"table","children":[header_row]+data_rows}, note]

  async def publish_to_telegraph(title: str, headers: list, rows: list) -> str:
    token = await _get_telegraph_token()
    nodes = _to_telegraph_nodes(headers, rows)
    async with aiosqlite.connect(DB_PATH) as db:
      row = await (await db.execute("SELECT value FROM _meta WHERE key='tg_path'")).fetchone()
    page_path = row[0] if row else None
    async with aiohttp.ClientSession() as s:
      endpoint = f"{TELEGRAPH_API}/editPage/{page_path}" if page_path else f"{TELEGRAPH_API}/createPage"
      async with s.post(endpoint, json={
        "access_token": token, "title": title[:256],
        "content": nodes, "return_content": False
      }) as r:
        result = (await r.json())["result"]
    if not page_path:
      page_path = result["path"]
      async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO _meta VALUES ('tg_path',?)", (page_path,))
        await db.commit()
    url = f"https://telegra.ph/{page_path}"
    async with aiosqlite.connect(DB_PATH) as db:
      await db.execute("INSERT OR REPLACE INTO _meta VALUES ('tg_url',?)", (url,))
      await db.commit()
    return url

  @router.message(Command("publish"))
  async def cmd_publish(message: Message):
    if str(message.from_user.id) not in _load_admins():
      await message.answer("⛔ Нет доступа")
      return
    status = await message.answer("⏳ Публикую таблицу...")
    async with aiosqlite.connect(DB_PATH) as db:
      cursor = await db.execute("SELECT * FROM entries ORDER BY created_at DESC LIMIT 300")
      rows = await cursor.fetchall()
      headers = [d[0] for d in cursor.description]
    try:
      await status.delete()
    except Exception:
      pass
    if not rows:
      await message.answer("Данных нет — нечего публиковать.")
      return
    url = await publish_to_telegraph(f"Данные — {BOT_NAME}", headers, rows)
    await message.answer(
      f"✅ <b>Таблица опубликована!</b>\\n\\n"
      f"🔗 {url}\\n\\n"
      f"Ссылка постоянная — при следующем /publish обновится по той же ссылке.",
      parse_mode="HTML"
    )

  @router.message(Command("weblink"))
  async def cmd_weblink(message: Message):
    if str(message.from_user.id) not in _load_admins():
      await message.answer("⛔ Нет доступа")
      return
    async with aiosqlite.connect(DB_PATH) as db:
      await db.execute("CREATE TABLE IF NOT EXISTS _meta (key TEXT PRIMARY KEY, value TEXT)")
      row = await (await db.execute("SELECT value FROM _meta WHERE key='tg_url'")).fetchone()
    if row:
      await message.answer(
        f"🔗 <b>Онлайн-таблица:</b>\\n{row[0]}\\n\\n"
        f"Обновить данные: /publish",
        parse_mode="HTML"
      )
    else:
      await message.answer("Таблица ещё не опубликована.\\nИспользуй /publish для первой публикации.")

DB SCHEMA — always include _meta + entries tables:
  CREATE TABLE IF NOT EXISTS _meta (key TEXT PRIMARY KEY, value TEXT);
  CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- columns based on what this bot collects (name, phone, amount, date, note, etc.)
    created_at TEXT DEFAULT (datetime('now','localtime'))
  );

IN-TELEGRAM TABLE (/view command — MANDATORY):
  Show last 15 entries as HTML-formatted box-drawing table inside <code>...</code>.
  If more than 15 rows exist, add a note "(показаны последние 15 из N всего)"
  Columns wider than 12 chars: truncate with "…"
  Always include ID column so admin can use /delete {id}

ADMIN COMMANDS (all require admin check with _load_admins()):
  /view — in-Telegram formatted table (last 15 rows)
  /publish — publish/update Telegra.ph online table
  /weblink — get the persistent online table URL
  /excel — download as styled Excel file
  /stats — total entries, today's count, any useful aggregate
  /delete {id} — remove one entry by ID (ask confirmation first via inline button)

AUTO-PUBLISH: after saving each new entry, optionally fire:
  asyncio.create_task(publish_to_telegraph(f"Данные — {BOT_NAME}", headers, all_rows))
=== END TABLE RULES ==="""


_BOT_TYPE_EXTRAS: dict[str, str] = {
    "booking": BOOKING_EXTRA,
    "manager": MANAGER_EXTRA,
    "moderator": MODERATOR_EXTRA,
    "table": TABLE_EXTRA,
    "general": "",
}


async def classify_bot_type(summary: str) -> str:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=10,
        system=BOT_TYPE_CLASSIFY_PROMPT,
        messages=[{"role": "user", "content": summary}],
    )
    result = response.content[0].text.strip().lower().split()[0]
    return result if result in _BOT_TYPE_EXTRAS else "general"


# handlers/feature_connect.py's narrow classifier — decides whether a free-form
# owner message (text, or a voice transcript run through the exact same path)
# is a request to CONNECT an already-existing platform feature (sheets/payments)
# to one of the owner's already-created bots, as opposed to /create-style talk
# about building something new, or ordinary conversation that belongs to
# ask_assistant(). This is called ONLY after a cheap keyword pre-filter
# (handlers/feature_connect.py's _looks_like_feature_request) already matched —
# see that module for the cost-control rationale — so it does not run on every
# message, only on ones that already look feature-connect-shaped.
CONNECT_FEATURE_CLASSIFY_PROMPT = """You classify a single message from the owner of a Telegram-bot factory (Bot-creter). Decide whether the message is a request to CONNECT an already-existing platform feature to one of the owner's already-created bots.

Available features (exactly these two, no others exist):
- "sheets" — Google Sheets / Google Таблицы integration (bot reads/writes a connected spreadsheet)
- "payments" — Telegram Payments (bot can accept payments)

Respond with ONLY a single line of valid JSON, no markdown fences, no explanation, in exactly this shape:
{"is_connect_request": true or false, "feature": "sheets" or "payments" or null, "bot_query": "<verbatim bot name/username the user referenced, or null>"}

Rules:
- is_connect_request is true ONLY when the user is clearly asking to turn on / connect / link / attach one of the two features above to a bot they already own. Examples that ARE connect requests: "подключи таблицы к моему боту", "включи оплату у Ромы", "хочу чтобы бот писал в гугл таблицу", "connect sheets to my shop bot".
- is_connect_request is false for: requests to CREATE a new bot or build NEW custom functionality (that's a different flow, not this one), questions about how something works, general chit-chat, status/log requests, anything not clearly a connect-an-existing-feature request. When unsure, prefer false — a false negative just falls back to normal conversation, which is safe.
- feature must be exactly "sheets", "payments", or null if the message doesn't clearly name one of these two (e.g. it names some other capability, or names none at all).
- bot_query is the bot's name/username/nickname exactly as the user referred to it (e.g. "магазин", "@my_shop_bot", "Рома", "мой бот для записи") — null if the user did not reference a specific bot.
- Never invent a feature or bot_query that isn't actually implied by the text."""


def _parse_connect_feature_intent(raw: str) -> dict:
    """Best-effort JSON parse of the classifier's output — any malformed or
    unexpected shape degrades to "not a connect request" (never raises), since
    a parsing hiccup here must fall back to ordinary conversation, not surface
    an error to the owner or silently misroute to the wrong bot/feature."""
    fallback = {"is_connect_request": False, "feature": None, "bot_query": None}
    try:
        data = json.loads(_strip_code_fences(raw))
    except (json.JSONDecodeError, ValueError):
        return fallback
    if not isinstance(data, dict):
        return fallback
    feature = data.get("feature")
    if feature not in ("sheets", "payments"):
        feature = None
    bot_query = data.get("bot_query")
    if not isinstance(bot_query, str) or not bot_query.strip():
        bot_query = None
    else:
        bot_query = bot_query.strip()
    return {
        "is_connect_request": bool(data.get("is_connect_request")),
        "feature": feature,
        "bot_query": bot_query,
    }


async def classify_connect_feature_intent(text: str) -> dict:
    """Returns {"is_connect_request": bool, "feature": "sheets"|"payments"|None,
    "bot_query": str|None}. Haiku, tiny max_tokens — same cost tier as
    classify_bot_type()/extract_bot_name(), not the sonnet model used for code
    generation. Callers must have already run a cheap local pre-filter (see
    handlers/feature_connect.py) before invoking this — see that module for why."""
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        system=CONNECT_FEATURE_CLASSIFY_PROMPT,
        messages=[{"role": "user", "content": text}],
    )
    return _parse_connect_feature_intent(response.content[0].text)


REVIEW_SYSTEM_PROMPT = """You are a senior Python code reviewer specializing in aiogram 3.13 Telegram bots.

You will receive a generated bot's Python code. Your job is to find and fix ALL potential runtime issues BEFORE the bot is deployed.

Check for these specific problems:
1. FSM DEAD ENDS — every state must have handlers; users must never get stuck with no way out
2. UNHANDLED INPUT TYPES — if a handler expects F.text, what happens if user sends photo/sticker/voice instead?
3. NONE/EMPTY CRASHES — any place where message.text, callback.data, or DB results could be None and cause AttributeError
4. MISSING FALLBACK HANDLERS — unexpected messages/callbacks should be caught gracefully, not silently ignored
5. CALLBACK DATA MISMATCHES — every callback_data string used in keyboards must have a matching handler
6. DB ERRORS NOT CAUGHT — aiosqlite calls that could fail if DB file doesn't exist yet or table is empty
7. IMPORT ERRORS — any import that is not in the allowed packages list
8. ASYNCIO ENTRY POINT — file must end with: if __name__ == "__main__": asyncio.run(main())

If you find issues: fix them and return the complete corrected code.
If the code looks correct: return it unchanged.
Return ONLY valid Python code. No markdown, no explanations."""


_TYPE_REVIEW_HINTS: dict[str, str] = {
    "booking": (
        "\nEXTRA CHECK FOR BOOKING BOTS — verify ALL of these exist:\n"
        "- Handler for F.data.startswith('book_day:')\n"
        "- Handler for F.data.startswith('book_slot:')\n"
        "- Handler for F.data == 'book_confirm'\n"
        "- Handler for F.data == 'book_cancel'\n"
        "- Handler for F.data == 'book_back'\n"
        "- Handler for F.data == 'book_unavailable'\n"
        "- Handler for F.data.startswith('adm_day:')\n"
        "- Handler for F.data.startswith('adm_cancel:')\n"
        "- init_db() is called inside main() before dp.start_polling\n"
        "- Anti-double-booking check inside book_confirm handler\n"
        "If ANY handler is missing — add a stub that answers the callback and shows an error message."
    ),
    "manager": (
        "\nEXTRA CHECK FOR MANAGER BOTS — verify ALL of these exist:\n"
        "- Handler for F.data.startswith('lead_type:')\n"
        "- Handler for F.data == 'lead_confirm'\n"
        "- Handler for F.data == 'lead_cancel'\n"
        "- validate_phone() function is defined and used in waiting_phone state\n"
        "- notify_admin_lead() or similar is called after saving lead\n"
        "- All FSM states have handlers (no dead ends)\n"
        "If ANY handler is missing — add it."
    ),
    "moderator": (
        "\nEXTRA CHECK FOR MODERATOR BOTS — verify ALL of these exist:\n"
        "- main() calls dp.start_polling with allowed_updates including 'chat_member'\n"
        "- moderate_message handler filters F.chat.type.in_({'group','supergroup'})\n"
        "- Handler checks member.status before acting (never moderate admins)\n"
        "- on_new_member / welcome handler using ChatMemberUpdatedFilter\n"
        "- LINK_PATTERN is defined with re.compile\n"
        "If missing — add them."
    ),
    "table": (
        "\nEXTRA CHECK FOR TABLE BOTS — verify ALL of these exist:\n"
        "- _get_telegraph_token() async function\n"
        "- _to_telegraph_nodes() or similar function\n"
        "- publish_to_telegraph() async function\n"
        "- /publish command handler\n"
        "- /weblink command handler\n"
        "- /view command handler (in-Telegram table)\n"
        "- _meta table created in init_db\n"
        "If ANY is missing — add it."
    ),
}


async def _review_bot_code(code: str, requirements: str, bot_type: str = "general") -> str:
    extra_hint = _TYPE_REVIEW_HINTS.get(bot_type, "")
    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=25000,
        system=REVIEW_SYSTEM_PROMPT + extra_hint,
        messages=[{
            "role": "user",
            "content": f"Bot type: {bot_type}\nBot requirements (for context):\n{requirements}\n\nGenerated code to review:\n{code}",
        }],
    )
    reviewed = _strip_code_fences(response.content[0].text)
    try:
        _ast.parse(reviewed)
        return reviewed
    except SyntaxError:
        return code  # review broke the code — keep original


FIX_SYSTEM_PROMPT = """You are an expert Python developer specializing in Telegram bots using aiogram 3.13.

You will receive an existing bot's Python code and a description of a bug or improvement request.
Your task: fix the bug / apply the improvement and return the complete corrected Python file.

Rules:
- Return ONLY complete valid Python code. No markdown fences, no explanations.
- Keep all existing functionality intact — only change what's needed to fix the described issue.
- Follow all the same constraints as the original code (aiogram 3.13, aiosqlite, openpyxl, aiohttp only).
- The file must end with asyncio.run(main())."""


async def fix_bot_code(current_code: str, bug_description: str) -> str:
    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=25000,
        system=FIX_SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": f"Bug / improvement request:\n{bug_description}\n\nCurrent bot code:\n{current_code}",
        }],
    )
    code = _strip_code_fences(response.content[0].text)
    try:
        _ast.parse(code)
    except SyntaxError as e:
        fix_response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=25000,
            system=FIX_SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": f"Bug / improvement request:\n{bug_description}\n\nCurrent bot code:\n{current_code}"},
                {"role": "assistant", "content": code},
                {"role": "user", "content": f"SyntaxError on line {e.lineno}: {e.msg}. Return ONLY corrected Python code."},
            ],
        )
        code = _strip_code_fences(fix_response.content[0].text)
        _ast.parse(code)
    if "asyncio.run(main())" not in code:
        cont = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=25000,
            system=FIX_SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": f"Bug / improvement request:\n{bug_description}\n\nCurrent bot code:\n{current_code}"},
                {"role": "assistant", "content": code},
                {"role": "user", "content": "Code was cut off. Complete it and end with asyncio.run(main()). Return ONLY complete Python code."},
            ],
        )
        code = _strip_code_fences(cont.content[0].text)
        _ast.parse(code)
    return code


IMPROVE_SYSTEM_PROMPT = """You are an expert Python developer specializing in Telegram bots using aiogram 3.13.

You will receive an existing working bot's Python code. Your task: improve it without rewriting from scratch.

What to improve:
- UI/UX: apply modern formatting — use box-drawing characters for tables inside <code> blocks, add emojis to all messages and buttons, use inline keyboards for date/time/filter selection instead of text input, add confirmation screens, handle empty states gracefully
- Missing features: if the description mentions something not yet implemented, add it
- Code quality: fix any obvious bugs, ensure all handlers are robust

Rules:
- Return ONLY complete valid Python code. No markdown fences, no explanations.
- Keep ALL existing functionality — do not remove any features.
- Minimise changes: edit only what needs improving, keep the rest as-is.
- Follow original constraints (aiogram 3.13, aiosqlite, openpyxl, aiohttp only).
- The file must end with asyncio.run(main()).
- parse_mode="HTML" for all rich messages; use <b>bold</b>, <i>italic</i>, <code>mono</code>.

ADMIN PANEL — apply these improvements if the bot has admin logic:
1. /start handler: detect if this is the first admin (admins list was empty before adding). If so, send a SEPARATE follow-up message:
   "👑 <b>Вы — администратор этого бота.</b>\\n\\nВам доступны дополнительные кнопки:\\n🗂 <b>Все записи</b> — полное расписание\\n📊 <b>Статистика</b> — сводка\\n\\nУправление администраторами:\\n<code>/addadmin ID</code> — добавить\\n<code>/removeadmin ID</code> — убрать\\n<code>/admins</code> — список"
2. Bookings/records table: add column user_id INTEGER if missing (ALTER TABLE ... ADD COLUMN inside try/except). Save from_user.id as user_id on every INSERT.
3. "Мои записи" / my bookings button: query by user_id directly — remove phone-input FSM step entirely.
4. "Отменить запись" / cancel booking button: same — query by user_id directly, no phone prompt.
5. Add /cleardata admin-only command: deletes all rows from bookings/appointments tables, resets slots to active, confirms "🗑 Все записи удалены."

EXCEL EXPORT — if the bot has a /excel or export function, make it beautiful:
- Header row: bold white text on dark blue background (#1F4E79), row height 22
- Data rows: alternate between white (#FFFFFF) and light blue (#DCE6F1) — zebra striping
- All cells: thin border on all 4 sides (Border(left=Side(style='thin'), ...))
- Freeze the header row: ws.freeze_panes = "A2"
- Auto-filter on header row: ws.auto_filter.ref = ws.dimensions
- Column widths: auto-fit based on content (min 10, max 40)
- Center-align header cells; left-align data cells
- Number/date columns: apply proper number format
- Use openpyxl.styles: PatternFill, Font, Alignment, Border, Side"""


async def improve_bot_code(current_code: str, description: str) -> str:
    """Improve existing bot code without full regeneration — saves tokens."""
    prompt = (
        f"Bot description (for context):\n{description}\n\n"
        f"Current bot code to improve:\n{current_code}"
    )
    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=25000,
        system=IMPROVE_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    code = _strip_code_fences(response.content[0].text)
    try:
        _ast.parse(code)
    except SyntaxError as e:
        fix_response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=25000,
            system=IMPROVE_SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": code},
                {"role": "user", "content": f"SyntaxError on line {e.lineno}: {e.msg}. Return ONLY corrected Python code."},
            ],
        )
        code = _strip_code_fences(fix_response.content[0].text)
        _ast.parse(code)
    if "asyncio.run(main())" not in code:
        cont = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=25000,
            system=IMPROVE_SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": code},
                {"role": "user", "content": "Code was cut off. Complete it ending with asyncio.run(main()). Return ONLY complete Python code."},
            ],
        )
        code = _strip_code_fences(cont.content[0].text)
        _ast.parse(code)
    return code


ASSISTANT_SYSTEM_PROMPT = """Ты — умный ассистент бота Bot-creter, который создаёт Telegram-ботов.

Ты отвечаешь на вопросы пользователя о его ботах, помогаешь разобраться с настройками и даёшь инструкции.

Что умеет Bot-creter:
- /create — создать нового бота (описываешь что нужно, бот генерируется автоматически)
- /list — управление ботами (запустить, остановить, логи, перегенерировать, исправить баг)
- /addadmin, /removeadmin, /admins — управление администраторами каждого бота
- Группа ботов: можно добавить несколько ботов в один Telegram-чат, дать каждому имя, и они будут отзываться на имя и общаться в группе

Если пользователь спрашивает про группу ботов — объясни:
1. Создай группу в Telegram и добавь туда нужных ботов + себя
2. Сделай каждого бота администратором группы (Участники → бот → Сделать администратором) — это позволит им видеть все сообщения
3. Напиши мне ID группы — для этого добавь меня (@boticsCREATOR_bot) в группу, я автоматически запомню её ID
4. После этого боты смогут общаться в группе по именам

Отвечай коротко и по-русски. Если не знаешь точного ответа — честно скажи."""


async def generate_bot_guide(bot_name: str, summary: str) -> str:
    """Generate a personalized guide for the bot owner after creation."""
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=600,
        system=(
            "Ты пишешь короткую персональную справку владельцу Telegram-бота сразу после его создания. "
            "Пиши по-русски, кратко и конкретно. Используй HTML-теги (<b>, <code>). "
            "НЕ пиши про команды /admins /addadmin /removeadmin — это будет отдельным блоком. "
            "НЕ пиши 'данные хранятся в таблице Excel' — данные хранятся в базе данных на сервере. "
            "Если бот собирает данные — упомяни команду <code>/excel</code> для выгрузки базы в .xlsx файл. "
            "Если не собирает — не упоминай /excel вообще."
        ),
        messages=[{"role": "user", "content": (
            f"Бот называется: {bot_name}\n\n"
            f"Что умеет этот бот (требования):\n{summary}\n\n"
            "Напиши 3-5 строк: что делает этот конкретный бот, его основные команды для пользователей, "
            "и если собирает данные — как их получить. Только про этот бот, без общих слов."
        )}],
    )
    return response.content[0].text.strip()


async def ask_assistant(user_message: str, bots_summary: str = "") -> str:
    system = ASSISTANT_SYSTEM_PROMPT
    if bots_summary:
        system += f"\n\nБоты пользователя:\n{bots_summary}"
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=900,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


async def chat_gather_requirements(conversation: list[dict]) -> str:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2048,
        system=GATHER_SYSTEM_PROMPT,
        messages=conversation,
    )
    return response.content[0].text


async def extract_bot_name(requirements_summary: str) -> str:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=32,
        system="Extract a short snake_case bot filename (no 'bot' suffix, max 20 chars, only a-z 0-9 _). Return ONLY the name, nothing else.",
        messages=[{"role": "user", "content": requirements_summary}],
    )
    raw = response.content[0].text.strip().lower()
    name = "".join(c for c in raw.replace(" ", "_") if c.isalnum() or c == "_")[:20]
    return name or "my_bot"


def _strip_code_fences(code: str) -> str:
    code = code.strip()
    if code.startswith("```"):
        first_newline = code.index("\n") if "\n" in code else len(code)
        code = code[first_newline:].strip()
        if code.endswith("```"):
            code = code[:-3].strip()
    return code


_TEMPLATES_DIR = __import__("pathlib").Path(__file__).parent.parent / "templates"

# Same "# TEMPLATE: <id>" marker convention runtime/registry.py's
# infer_template_id() reads (that file untouched, out of this phase's scope —
# a separate constant/regex lives here instead of importing from runtime/, to
# avoid a services -> runtime dependency). Read by LINE COUNT, not a byte/char
# budget: a byte-sliced read (registry.py's own approach) risks silently
# truncating mid-line if a future USE FOR: description runs long — reading
# whole lines instead means TEMPLATE:/USE FOR: either match in full or don't
# match at all, never a silently half-parsed value. Uses a file handle (not
# Path.read_text()) so only the first few lines are ever actually read off
# disk — relevant once the library grows well past 5 templates.
_TEMPLATE_HEADER_MAX_LINES = 10
_TEMPLATE_LINE_RE = re.compile(r"^#\s*TEMPLATE:\s*(\S+)", re.MULTILINE)
_USE_FOR_LINE_RE = re.compile(r"^#\s*USE FOR:\s*(.+)$", re.MULTILINE)


def discover_templates() -> list[dict[str, str]]:
    """Scans templates/*.py for '# TEMPLATE: <id>' / '# USE FOR: <description>'
    header comments and returns [{"name": ..., "use_for": ...}, ...] for every
    file that has both. This is what makes adding a new template to templates/
    show up in Claude's selection prompt automatically — no code change here
    needed (see _build_template_select_prompt below).

    A file missing either marker (or unreadable) is skipped with a warning,
    not fatal — one broken/incomplete file in templates/ must not take down
    template selection for every other template. Files whose name starts with
    "_" (e.g. a future shared templates/_common.py helper module) are skipped
    silently, not warned about — such files aren't meant to be templates at
    all, so warning on every single call forever would just be noise."""
    results: list[dict[str, str]] = []
    if not _TEMPLATES_DIR.exists():
        return results
    for path in sorted(_TEMPLATES_DIR.glob("*.py")):
        if path.stem.startswith("_"):
            continue
        try:
            with path.open(encoding="utf-8") as f:
                head = "".join(next(f, "") for _ in range(_TEMPLATE_HEADER_MAX_LINES))
        except (OSError, UnicodeDecodeError) as e:
            logger.warning(f"discover_templates: could not read {path.name}: {e}")
            continue
        name_match = _TEMPLATE_LINE_RE.search(head)
        use_for_match = _USE_FOR_LINE_RE.search(head)
        if not name_match or not use_for_match:
            logger.warning(f"discover_templates: {path.name} missing # TEMPLATE:/# USE FOR: header — skipped")
            continue
        results.append({"name": name_match.group(1), "use_for": use_for_match.group(1).strip()})
    return results


_TEMPLATE_SELECT_PROMPT_HEADER = (
    'You are a bot template selector. Given a bot description, choose the best '
    'matching template or return "none".\n\nAvailable templates:\n'
)
_TEMPLATE_SELECT_PROMPT_FOOTER = (
    '\n\nDecide how many templates to return:\n'
    '- ONE template name if it alone covers 60%+ of the request\'s core functionality — '
    'even if a second template touches some minor part of it. A dominant template plus '
    'a small bolt-on feature is still ONE template.\n'
    '- TWO template names, comma-separated with no space (e.g. "shop_catalog,loyalty_program"), '
    'ONLY if the request clearly describes two distinct business domains and NEITHER template '
    'alone covers 60%+ of the request — each of the two must independently cover a substantial, '
    'largely non-overlapping share of the requirements.\n'
    '- "none" if nothing matches 60%+ combined.\n'
    'When in doubt between one template and two, prefer ONE — a false-positive merge is more '
    'expensive than picking the closest single template.\n'
    'Return ONLY the template name(s) or "none", nothing else — no explanation.'
)


def _build_template_select_prompt() -> str:
    """Same wording/format as the old hardcoded TEMPLATE_SELECT_PROMPT — only the
    template list itself is now sourced from discover_templates() instead of being
    manually typed, so adding a templates/*.py file needs no edit here."""
    lines = "\n".join(f"- {t['name']} — {t['use_for']}" for t in discover_templates())
    return _TEMPLATE_SELECT_PROMPT_HEADER + lines + _TEMPLATE_SELECT_PROMPT_FOOTER

MERGE_TEMPLATES_EXTRA = """

SYNTHESIS MODE — the user's requirements span two existing bot domains. You will receive two working reference templates below. Do NOT concatenate or copy them as-is — design ONE new, internally consistent bot that covers both domains as a single coherent product.

Reference templates are for STYLE and PROVEN PATTERNS ONLY (DB schema shapes, FSM structures, keyboard patterns, admin helpers, Excel export). Reuse the sub-patterns you need, then adapt everything so the result reads as one bot, not two files stapled together.

Resolve naming conflicts explicitly:
- If both templates define a StatesGroup covering the same step (e.g. both have a "waiting_phone" state), merge into ONE state chain — never keep duplicate parallel states.
- If both templates create a table with the same name for different data (e.g. both have "orders" or "clients"), rename one — never let two unrelated tables share a name, and never let one silently overwrite the other's schema.
- If both templates use the same callback_data string for different actions (e.g. both use "confirm"/"cancel"), prefix them by domain so handlers cannot cross-fire.
- If both templates define admin helpers (_load_admins/_save_admins) or a users table, KEEP ONLY ONE COPY.

Single coherent identity and menu:
- Build ONE /start menu exposing both domains as sections of the SAME bot — never a menu that forks into "template A mode" vs "template B mode".
- If both domains track the same real-world person (e.g. a customer who both buys and earns points), they MUST share one identity/record — never split the same user across two disconnected tables with no link between them.

Do not drop functionality: every core feature described in the requirements for BOTH domains must be present in the final bot. Do not include a reference template's feature that the requirements don't call for.

Return ONLY the complete, single Python file. No markdown fences, no explanations."""


MERGE_REVIEW_SYSTEM_PROMPT = """You are a senior Python code reviewer specializing in aiogram 3.13 Telegram bots. You are reviewing a bot that was SYNTHESIZED from two different template domains into one file.

Your job is to find and fix problems specific to merging two domains into one bot BEFORE it is deployed. Check for these specific problems:

1. DUPLICATE/COLLIDING TABLE NAMES — two unrelated tables sharing a name (one silently overwriting the other's schema), or the same real-world entity (e.g. a customer) split across two disconnected tables with no shared key
2. DUPLICATE STATE GROUPS — two StatesGroup classes covering the same step that should have been merged into one chain
3. COLLIDING CALLBACK_DATA — the two domains using the same callback_data string for different actions, causing the wrong handler to fire
4. DUPLICATED ADMIN/SHARED HELPERS — _load_admins/_save_admins, users table, or other shared infrastructure defined twice instead of once
5. FORKED MENU — a /start menu that just splits into "domain A mode" / "domain B mode" instead of one integrated menu
6. LOST FUNCTIONALITY — a feature required by either domain that is missing from the merged code
7. UNUSED LEFTOVER CODE — helpers/constants copied from a reference template but never called

If you find issues: fix them and return the complete corrected code.
If the code looks correct: return it unchanged.
Return ONLY valid Python code. No markdown, no explanations."""


CUSTOMIZE_TEMPLATE_PROMPT = """You are a Telegram bot customizer. You receive a working bot template and user requirements.

Your task: customize the template for the specific use case WITHOUT changing any logic, handlers, FSM states, or database schema.

ONLY change these sections (marked with # CUSTOMIZE in the template):
- BOT_DESCRIPTION — update to match the specific business/use case
- WELCOME_TEXT — update greeting to match the specific bot purpose
- SERVICES / MASTERS / FAQS / EXPENSE_CATEGORIES / INCOME_CATEGORIES etc. — update lists to match requirements
- Any other constants in the # CUSTOMIZE block

DO NOT change:
- Any function definitions or handlers
- FSM states or callback_data formats
- Database schema or queries
- Import statements
- The main() function
- Anything outside the # CUSTOMIZE...# END CUSTOMIZE block

Return ONLY the complete modified Python code. No markdown fences. No explanations."""


async def _select_template(summary: str) -> list[str]:
    """Returns 0, 1, or 2 template names (e.g. ['trip_manager'] or
    ['shop_catalog', 'loyalty_program']), per the threshold in
    _TEMPLATE_SELECT_PROMPT_FOOTER. Any response that doesn't cleanly resolve
    to real template files — garbage text, a name that doesn't exist on disk,
    more than 2 names — falls back to an empty list rather than guessing,
    since generate_bot_code's caller treats [] the same as the old None (fall
    through to generating from scratch)."""
    if not _TEMPLATES_DIR.exists():
        return []
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=30,
        system=_build_template_select_prompt(),
        messages=[{"role": "user", "content": summary}],
    )
    text = response.content[0].text.strip().lower()
    if not text:
        return []  # blank/garbage response — never guess, fall through to scratch generation
    # Collapse whitespace around commas BEFORE taking the first whitespace-delimited
    # token — otherwise a model response like "shop_catalog, loyalty_program" (a space
    # after the comma despite the prompt saying not to) would have its second name cut
    # off by .split()[0] alone, silently degrading a two-template response into a
    # single-template one instead of failing closed to [] as intended.
    raw = re.sub(r"\s*,\s*", ",", text).split()[0]
    if raw == "none":
        return []
    names = [n for n in dict.fromkeys(part.strip() for part in raw.split(",")) if n]
    if not names or len(names) > 2:
        return []
    valid = [n for n in names if (_TEMPLATES_DIR / f"{n}.py").exists()]
    return valid if len(valid) == len(names) else []


async def _customize_from_template(template_name: str, requirements: str) -> str:
    """Customizes a template for specific requirements. Returns Python code."""
    template_path = _TEMPLATES_DIR / f"{template_name}.py"
    template_code = template_path.read_text(encoding="utf-8")
    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=25000,
        system=CUSTOMIZE_TEMPLATE_PROMPT,
        messages=[{
            "role": "user",
            "content": f"User requirements:\n{requirements}\n\nTemplate to customize:\n{template_code}",
        }],
    )
    code = _strip_code_fences(response.content[0].text)
    try:
        _ast.parse(code)
        return code
    except SyntaxError:
        return template_code  # fallback to unmodified template


async def _synthesize_from_templates(template_names: list[str], requirements: str) -> str:
    """Synthesizes ONE new bot from two reference templates (SYNTHESIS MODE).
    Unlike _customize_from_template, this is allowed to change schema/FSM/
    handlers freely — that's the whole point, see MERGE_TEMPLATES_EXTRA."""
    name_a, name_b = template_names
    code_a = (_TEMPLATES_DIR / f"{name_a}.py").read_text(encoding="utf-8")
    code_b = (_TEMPLATES_DIR / f"{name_b}.py").read_text(encoding="utf-8")
    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=25000,
        system=GENERATE_SYSTEM_PROMPT + MERGE_TEMPLATES_EXTRA,
        messages=[{
            "role": "user",
            "content": (
                f"User requirements:\n{requirements}\n\n"
                f"Reference template A ({name_a}):\n{code_a}\n\n"
                f"Reference template B ({name_b}):\n{code_b}"
            ),
        }],
    )
    code = _strip_code_fences(response.content[0].text)
    try:
        _ast.parse(code)
        return code
    except SyntaxError:
        return ""  # caller checks for asyncio.run(main()); empty string falls through to scratch generation


async def _review_merged_bot_code(code: str, requirements: str) -> str:
    """Merge-specific review pass — catches defects unique to synthesizing two
    domains (duplicate tables/FSM groups, colliding callback_data, forked
    menus) that the generic _review_bot_code checklist doesn't cover. Must run
    BEFORE _review_bot_code so the generic pass reviews the final, already
    de-duplicated structure rather than a pre-fix draft."""
    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=25000,
        system=MERGE_REVIEW_SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": f"Bot requirements (for context):\n{requirements}\n\nSynthesized code to review:\n{code}",
        }],
    )
    reviewed = _strip_code_fences(response.content[0].text)
    try:
        _ast.parse(reviewed)
        return reviewed
    except SyntaxError:
        return code  # review broke the code — keep pre-review version


# Narrow, risk-targeted review pass — reserved for the two riskiest generation
# paths (synthesis and custom_features, see their call sites below), plus
# (unconditionally, see generate_bot_code's from-scratch branch) the
# from-scratch fallback. Checks three things neither _review_bot_code nor
# _review_merged_bot_code cover as a dedicated concern: FSM state-transition
# correctness, SQL parameterization / cross-bot data isolation (not checked
# ANYWHERE else in the pipeline today), and duplication/dead code — plus a
# 4th, conditional PAYMENT/MONEY SAFETY category appended only when the code
# or context mentions payments, so the prompt (and token cost) stays at its
# original size for the common non-payment case. Deliberately not run for
# plain single-template customization — that path only changes a
# `# CUSTOMIZE` block inside an already-reviewed template, so the narrow-risk
# categories don't apply.
NARROW_RISK_REVIEW_SYSTEM_PROMPT = """You are a senior Python code reviewer specializing in aiogram 3.13 Telegram bots. You are reviewing code from one of the highest-risk generation paths in an automated bot-factory: synthesized from two templates, generated from scratch, or a from-scratch patch written for one specific bot's unique request. Focus ONLY on these risk categories — do not comment on anything else, do not restyle, do not "improve" unrelated code:

1. FSM STATE-TRANSITION CORRECTNESS
   - Every state a handler/keyboard can put the FSM into must have a handler that consumes it and either advances to the next state or clears state — no dead ends.
   - No handler silently skips, re-enters, or double-advances a state.
   - Every cancel/back/completion path calls state.clear() (or the equivalent) — no orphaned FSM state left behind after a flow ends.

2. DATA ISOLATION / SQL SAFETY
   - Every SQL query must interpolate any user-supplied or bot-instance value via aiosqlite parameter placeholders (?) — flag ANY query built with f-strings, .format(), %-formatting, or string concatenation of a variable into SQL text.
   - Any query against a table that could be shared or reused across bot instances must filter by this bot's own identifying column — flag anything that could read or write another bot's/another instance's rows.
   - Flag any write path that could expand this code's reach beyond its own bot's own data.

3. DUPLICATION / DEAD CODE
   - Two functions/handlers doing materially the same thing where one should just call the other.
   - Copy-pasted blocks that should be one shared helper.
   - Any defined function, constant, import, or handler that is never referenced anywhere in the file.

For each real issue found in these categories: fix it directly in the code, minimally, without touching anything else. If nothing in these categories is wrong, return the code unchanged — do not invent problems to justify a change.

Return ONLY valid Python code. No markdown, no explanations."""

# Appended to NARROW_RISK_REVIEW_SYSTEM_PROMPT only when the code/context
# mentions payments — see _mentions_payments below. Kept as a separate
# constant (rather than always in the base prompt) so the common
# non-payment review call stays at its original prompt size.
PAYMENT_SAFETY_REVIEW_EXTRA = """

4. PAYMENT/MONEY SAFETY
   - Every payment/invoice handler must verify the amount and currency come from server-side/bot-defined values, never trusted unvalidated user input.
   - Successful-payment handlers (e.g. F.successful_payment) must be idempotent — a duplicate webhook/update for the same payment must not double-credit, double-fulfill, or double-charge.
   - Every payment record write must be tied to the paying user's own id and this bot's own instance — flag anything that could credit/fulfill the wrong user or bot.
   - Flag any path where a purchase/fulfillment action can be reached WITHOUT a corresponding verified successful-payment event (e.g. granting the item on button click instead of on payment confirmation)."""


def _mentions_payments(*texts: str) -> bool:
    """True if any of the given strings mention payments/invoices — gates
    whether PAYMENT_SAFETY_REVIEW_EXTRA is appended to the narrow-risk
    prompt. Deliberately cheap (substring check, no LLM call) since this
    runs on every narrow-risk review regardless of outcome."""
    haystack = "\n".join(texts).lower()
    return "payment" in haystack or "invoice" in haystack


async def _review_narrow_risk_code(code: str, context: str, reference_code: str = "") -> str:
    """FSM-transition / SQL-safety-and-data-isolation / duplication (+ payment
    safety when relevant) pass. See NARROW_RISK_REVIEW_SYSTEM_PROMPT's
    comment above for scope and why it's reserved for synthesis,
    from-scratch generation, and custom_features. `context` is free-form —
    the requirements summary for synthesis/from-scratch, or the owner's
    feature-request text for custom_features. `reference_code`, when given, is the existing
    bot's main file shown READ-ONLY so the reviewer can also flag new
    table/callback names that collide with it — used for custom_features
    only; synthesis already gets collision checking from
    _review_merged_bot_code so it calls this with reference_code empty.
    If code/context/reference_code mentions payments or invoices,
    PAYMENT_SAFETY_REVIEW_EXTRA is appended to the prompt for this call only
    — see _mentions_payments. Same auto-apply contract as
    _review_bot_code/_review_merged_bot_code: returns the full corrected
    file, or the pre-review code unchanged if the reviewed output doesn't
    parse."""
    user_content = f"Context (for reference, not to be reproduced):\n{context}\n\n"
    if reference_code:
        user_content += (
            f"Existing bot code shown READ-ONLY, for collision checks only — "
            f"do not reproduce or modify it:\n{reference_code}\n\n"
        )
    user_content += f"Code to review:\n{code}"

    system = NARROW_RISK_REVIEW_SYSTEM_PROMPT
    if _mentions_payments(code, context, reference_code):
        system += PAYMENT_SAFETY_REVIEW_EXTRA

    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=25000 if not reference_code else 8000,  # custom_features patches are small modules
        system=system,
        messages=[{"role": "user", "content": user_content}],
    )
    reviewed = _strip_code_fences(response.content[0].text)
    try:
        _ast.parse(reviewed)
        return reviewed
    except SyntaxError:
        return code  # review broke the code — keep pre-review version


async def generate_bot_code(requirements_summary: str) -> tuple[str, dict | None]:
    """Generates the bot's code, then a mini-app config for it (see
    docs/MINIAPP_DESIGN.md §6) — a single cheap Haiku call, never blocking:
    if it fails or produces something that doesn't match the generated
    code's actual tables, the bot is still returned with miniapp_config=None
    (plain Telegram bot, no mini-app, same as any template without one)."""
    code = await _generate_bot_code_inner(requirements_summary)
    miniapp_config = await _generate_miniapp_config(code, requirements_summary)
    return code, miniapp_config


async def _generate_bot_code_inner(requirements_summary: str) -> str:
    # Try template(s) first — saves 5-10x tokens vs generating from scratch
    templates = await _select_template(requirements_summary)

    if len(templates) == 1:
        code = await _customize_from_template(templates[0], requirements_summary)
        if "asyncio.run(main())" in code:
            bot_type = await classify_bot_type(requirements_summary)
            code = await _review_bot_code(code, requirements_summary, bot_type=bot_type)
            return code

    elif len(templates) == 2:
        code = await _synthesize_from_templates(templates, requirements_summary)
        if "asyncio.run(main())" in code:
            code = await _review_merged_bot_code(code, requirements_summary)
            code = await _review_narrow_risk_code(code, requirements_summary)
            bot_type = await classify_bot_type(requirements_summary)
            code = await _review_bot_code(code, requirements_summary, bot_type=bot_type)
            return code

    bot_type = await classify_bot_type(requirements_summary)
    extra = _BOT_TYPE_EXTRAS.get(bot_type, "")
    system = GENERATE_SYSTEM_PROMPT + extra

    user_msg = f"Create a Telegram bot with these requirements:\n\n{requirements_summary}"
    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=25000,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
    )
    code = _strip_code_fences(response.content[0].text)

    # Validate syntax; if broken ask Claude to fix it once
    try:
        _ast.parse(code)
    except SyntaxError as e:
        fix_response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=25000,
            system=GENERATE_SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": user_msg},
                {"role": "assistant", "content": code},
                {"role": "user", "content": f"SyntaxError on line {e.lineno}: {e.msg}. Return ONLY corrected Python code, no markdown."},
            ],
        )
        code = _strip_code_fences(fix_response.content[0].text)
        _ast.parse(code)  # raises if still broken — caught upstream

    # If code was truncated by token limit it won't have an entry point
    if "asyncio.run(main())" not in code:
        fix_response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=25000,
            system=GENERATE_SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": user_msg},
                {"role": "assistant", "content": code},
                {"role": "user", "content": "The code was cut off and is missing asyncio.run(main()). Complete the code from where it stopped and finish with the correct main() function and asyncio.run(main()). Return ONLY the complete Python code, no markdown."},
            ],
        )
        code = _strip_code_fences(fix_response.content[0].text)
        _ast.parse(code)

    # Narrow-risk pass first (FSM transitions / SQL isolation / duplication,
    # plus payment safety if requirements_summary mentions payments) — same
    # ordering as the synthesis branch above, so the generic pass below
    # reviews the already-narrow-fixed code rather than a pre-fix draft.
    code = await _review_narrow_risk_code(code, requirements_summary)

    # Static review pass — find and fix potential runtime issues before deployment
    code = await _review_bot_code(code, requirements_summary, bot_type=bot_type)

    return code


# ── mini-app config generation (see docs/MINIAPP_DESIGN.md §6) ─────────────────
#
# One cheap Haiku call per bot, same cost tier as classify_bot_type()/
# extract_bot_name() — NOT a second code-generation pass. Reads the bot's own
# already-generated code (its init_db()'s CREATE TABLE statements are the
# ground truth) and produces a declarative {"resources": [...]} schema in the
# same shape as templates/tour_operator.py's hand-written miniapp_config,
# which is given as the only few-shot example. Never blocks bot creation:
# any failure (network, malformed JSON, hallucinated table/column names)
# degrades to miniapp_config=None — the bot is just a plain Telegram bot,
# exactly like any template without a miniapp_config today.

_TOUR_OPERATOR_MINIAPP_CONFIG_EXAMPLE = """{
    "resources": [
        {
            "name": "tours",
            "table": "tours",
            "order_by": "created_at DESC",
            "creatable": true,
            "fields": [
                {"name": "name", "required": true},
                {"name": "destination"},
                {"name": "date_start"},
                {"name": "date_end"},
                {"name": "guests_count"},
                {"name": "status"},
                {"name": "created_at", "creatable": false}
            ]
        },
        {
            "name": "guests",
            "table": "guests",
            "order_by": "created_at DESC",
            "creatable": true,
            "fields": [
                {"name": "tour_id", "required": true},
                {"name": "name", "required": true},
                {"name": "total_cost"},
                {"name": "prepaid"},
                {"name": "our_price"},
                {"name": "status"},
                {"name": "notes"},
                {"name": "created_at", "creatable": false}
            ]
        }
    ]
}"""

MINIAPP_CONFIG_SYSTEM_PROMPT = f"""You extract a mini-app schema from a Telegram bot's Python source code, for a factory that builds Telegram bots.

You will be given the bot's full source code (its init_db() function contains the real CREATE TABLE statements — this is your only source of truth for table and column names) and a short description of what the bot does.

Produce a JSON object describing which of the bot's SQLite tables deserve a screen in a generic mini-app (a shared list/detail/create-form UI that reads this schema by convention — you are NOT writing any UI code, only describing the data).

Here is one correct real example, from a tour-operator bot (tours + guests are real tables in that bot's init_db()):

{_TOUR_OPERATOR_MINIAPP_CONFIG_EXAMPLE}

Rules:
- Respond with ONLY the JSON object, no markdown fences, no explanation.
- Top-level shape: {{"resources": [...]}}. Each resource: "name" (short identifier), "table" (MUST be a real table name from the given code's CREATE TABLE statements, verbatim), "order_by" (a real column, e.g. "created_at DESC" or "id DESC"), "creatable" (true/false), "fields" (list of {{"name": ..., "required": true/false (optional, default false), "creatable": true/false (optional, default true)}}).
- Every "table" and every field "name" MUST match a real table/column that appears in the given code's CREATE TABLE statements. Never invent a table or column that isn't there.
- Only include tables that hold user-facing records worth browsing (bookings, orders, items, clients, etc). Skip purely internal/administrative tables (admins, state, sessions, migration/version tables, FSM storage).
- If the bot has no table worth showing as a mini-app screen (e.g. a purely conversational bot with no data table, or only internal tables), respond with exactly {{"resources": []}}. This is a valid, expected answer — not an error.
- Do not include any resource whose table you are not certain is a literal, verbatim CREATE TABLE name in the given code."""


def _extract_create_table_names(bot_code: str) -> dict[str, set[str]]:
    """Best-effort scan of init_db()'s CREATE TABLE statements: table name ->
    set of column names. Regex, not a SQL parser — good enough to catch a
    hallucinated table/column (the failure mode this guards against), not
    meant to validate SQL syntax. Never raises on malformed input."""
    tables: dict[str, set[str]] = {}
    for table_match in re.finditer(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s*\((.*?)\)\s*(?:\"\"\"|''')",
        bot_code,
        re.IGNORECASE | re.DOTALL,
    ):
        table_name = table_match.group(1)
        columns_blob = table_match.group(2)
        columns: set[str] = set()
        for line in columns_blob.split(","):
            line = line.strip()
            col_match = re.match(r"(\w+)", line)
            if col_match and col_match.group(1).upper() not in (
                "PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "CONSTRAINT",
            ):
                columns.add(col_match.group(1))
        tables[table_name] = columns
    return tables


def _validate_miniapp_config_against_code(config: dict, bot_code: str) -> bool:
    """True only if every resource's table and every field's name is a real,
    verbatim match against the bot's own CREATE TABLE statements. Any single
    mismatch invalidates the whole config (dropped wholesale, not patched) —
    a partially-hallucinated schema is worse than no mini-app at all."""
    tables = _extract_create_table_names(bot_code)
    if not tables:
        return False
    for resource in config.get("resources", []):
        table = resource.get("table")
        if not isinstance(table, str) or table not in tables:
            return False
        columns = tables[table]
        for field in resource.get("fields", []):
            name = field.get("name") if isinstance(field, dict) else None
            if not isinstance(name, str) or name not in columns:
                return False
    return True


def _parse_miniapp_config(raw: str, bot_code: str) -> dict | None:
    """Best-effort JSON parse + schema validation — any malformed shape or
    hallucinated table/column degrades to None (never raises), mirroring
    _parse_connect_feature_intent's fallback pattern: a parsing hiccup here
    must never surface an error or block bot creation."""
    try:
        data = json.loads(_strip_code_fences(raw))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("resources"), list):
        return None
    if not data["resources"]:
        return None  # valid "no mini-app for this bot" answer, nothing to store
    for resource in data["resources"]:
        if not isinstance(resource, dict):
            return None
        if not isinstance(resource.get("name"), str) or not isinstance(resource.get("table"), str):
            return None
        if not isinstance(resource.get("fields"), list) or not resource["fields"]:
            return None
        for field in resource["fields"]:
            if not isinstance(field, dict) or not isinstance(field.get("name"), str):
                return None
    if not _validate_miniapp_config_against_code(data, bot_code):
        return None
    return data


async def _generate_miniapp_config(bot_code: str, requirements_summary: str) -> dict | None:
    """Never raises — any failure (API error, malformed/hallucinated output)
    returns None, which callers treat as "no mini-app for this bot", exactly
    like a template with no miniapp_config today. Must never block or delay
    bot creation on its own error."""
    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1500,
            system=MINIAPP_CONFIG_SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": f"Bot code:\n\n{bot_code}\n\nBot description:\n{requirements_summary}",
            }],
        )
    except Exception as e:
        logger.warning(f"_generate_miniapp_config: API call failed: {type(e).__name__}: {e}")
        return None
    return _parse_miniapp_config(response.content[0].text, bot_code)


# ── custom_features (point doctoring of one specific bot) ──────────────────────
#
# Distinct from generate_bot_code/fix_bot_code/improve_bot_code above: those all
# read AND write the whole bot file (thousands of lines both ways). This writes
# only a small, additive custom_features/bot_<id>.py module (see
# runtime/registry.py's _load_and_include_custom_feature) — the main bot file is
# given as READ-ONLY context so Claude can match its DB_PATH/admin-check/naming
# conventions, but is never touched, so a future template re-customization of
# generated_bots/<name>.py never clobbers this file.

CUSTOM_FEATURE_SYSTEM_PROMPT = """You write ONE small, self-contained aiogram 3.13 Router module that adds a single point feature to an ALREADY WORKING Telegram bot.

You will be given: (1) the full existing bot code (READ-ONLY — for context and naming conventions only, do NOT reproduce or modify it), (2) a description of the feature to add.

Output contract:
- Return ONLY the new module's code — nothing from the existing bot file.
- Must define a module-level `router = Router()`.
- If new persistent data is needed, also define `async def init_db(db_path: str) -> None` using aiosqlite, CREATE TABLE IF NOT EXISTS with table names that do not collide with any CREATE TABLE already present in the existing bot code shown to you.
- Reuse the existing bot's conventions exactly: same DB_PATH/DATA_DIR resolution, same parse_mode="HTML" style.
- Do NOT redefine any command/callback handler that already exists in the shown code — this module is additive only, never a replacement.
- This module CANNOT import anything from the existing bot file — it is a separate script, not a package, and has no importable name. If the existing code has an admin-check pattern (e.g. loading admin IDs from a JSON file), REIMPLEMENT the same logic inline in this module using the same underlying data source (e.g. the same admins JSON file path) — never call a function that only exists in the other file, that will crash with NameError the first time a real user reaches it.

AVAILABLE PACKAGES — ONLY use these external libraries (everything else will crash with ImportError):
  - aiogram 3.13 — Telegram bot framework
  - aiosqlite — async SQLite
  - openpyxl — create/read Excel .xlsx files
  - aiohttp — async HTTP requests
  - Python stdlib: asyncio, os, logging, datetime, pathlib, csv, json, re, collections, itertools, functools, math, random, string, time, uuid, io

FORBIDDEN PACKAGES — not installed, will cause immediate crash:
  - requests, httpx, urllib3 → use aiohttp instead
  - pandas, numpy → use openpyxl or csv module instead
  - xlrd, xlwt, xlsxwriter → use openpyxl instead
  - PIL, Pillow → not available
  - apscheduler, schedule → not available; use asyncio.create_task + asyncio.sleep for delayed jobs
  - sqlalchemy, peewee, tortoise → use aiosqlite directly
  - pydantic → not available
  - Any other third-party library not listed above

Return ONLY valid Python code. No markdown fences. No explanations."""


# Same allowlist as CUSTOM_FEATURE_SYSTEM_PROMPT's "AVAILABLE PACKAGES" section
# above, turned into a real set instead of only prose — the prompt asking
# Claude nicely is not a control, this is. Deliberately an ALLOWLIST, not a
# blocklist of the "FORBIDDEN PACKAGES" named above: a blocklist only catches
# names someone thought to write down, an allowlist rejects everything not
# already vetted, including packages nobody has thought to forbid yet.
#
# THIS IS NOT A SANDBOX — it blocks importing UNVETTED packages, it does
# nothing to restrict what the VETTED ones can do once this code is running
# inside the live factory-bot process (runtime/registry.py wires the router
# straight into a real Dispatcher, no subprocess/container isolation). The
# real, concrete blast radius of every package left in this set:
#   - os + pathlib: arbitrary filesystem read/write with this process's own
#     permissions, INCLUDING os.environ — every secret this process holds
#     (ENCRYPTION_KEY, ANTHROPIC_API_KEY, GITHUB_TOKEN, BOT_TOKEN) is a plain
#     os.getenv() call away from generated code.
#   - aiosqlite: direct read/write access to the SHARED data/bots.db — not
#     just this bot's own rows. Every bot's Telegram token lives there,
#     Fernet-encrypted (db/database.py's _encrypt_token) — encrypted is not
#     the same as inaccessible: combined with the os.environ access above
#     (ENCRYPTION_KEY is one of the env vars readable that way), a
#     deliberately malicious patch can decrypt and exfiltrate every bot's
#     token on the factory, not only the one it was generated for.
#   - aiohttp: arbitrary outbound HTTP from inside this trusted process — the
#     exfiltration channel for anything read above, plus reaches whatever
#     network the factory process itself can reach.
# Accepted today ONLY because the sole trigger for custom_features is the
# factory owner's own request, generated fresh each time and shown to them
# for approval before it's written — there is no untrusted third party in
# this loop. This stops being an acceptable trade-off the moment that
# assumption changes (e.g. a bot's own end users being able to influence what
# gets generated). See backlog_custom_features_known_gaps memory for the
# cheap partial mitigation not yet implemented (AST-flagging os.system/eval/
# exec/__import__) and the real fix (actual process/container isolation).
_ALLOWED_ROOT_MODULES = {
    "aiogram", "aiosqlite", "openpyxl", "aiohttp",
    "asyncio", "os", "logging", "datetime", "pathlib", "csv", "json", "re",
    "collections", "itertools", "functools", "math", "random", "string", "time",
    "uuid", "io",
}


def _forbidden_imports_from_tree(tree: _ast.AST) -> list[str]:
    violations: list[str] = []
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Import):
            violations += [
                n.name.split(".")[0] for n in node.names
                if n.name.split(".")[0] not in _ALLOWED_ROOT_MODULES
            ]
        elif isinstance(node, _ast.ImportFrom) and node.module:
            root = node.module.split(".")[0]
            if root not in _ALLOWED_ROOT_MODULES:
                violations.append(root)
    return violations


def check_forbidden_imports(code: str) -> list[str]:
    """Public entry point — parses `code` fresh and returns the root module
    names of any import not in _ALLOWED_ROOT_MODULES (empty list = clean).
    Called twice in the custom_features flow: once inside
    generate_custom_feature's own retry loop (via _forbidden_imports_from_tree,
    reusing the AST already parsed for the syntax check — no second parse), and
    again by handlers/custom_features.py's apply-time gate right before the
    isolated-import subprocess check, as defense in depth against anything
    changing between generation and the owner pressing "Применить". Raises
    SyntaxError if `code` doesn't parse — callers that already validated syntax
    separately won't hit this; callers that haven't should check syntax first.

    This rejects UNVETTED imports only — it is not a sandbox, see
    _ALLOWED_ROOT_MODULES' own comment above for the real blast radius of the
    packages it does allow (os.environ/data.db/network access from inside the
    live factory process)."""
    return _forbidden_imports_from_tree(_ast.parse(code))


class CustomFeatureGenerationError(Exception):
    """Raised by generate_custom_feature when Claude can't produce a
    syntactically valid, allowlist-compliant patch even after one retry.
    Callers (handlers/custom_features.py) must treat this as a generation
    failure — show the owner a retry prompt, never fall back to showing a
    broken/non-compliant preview."""


def _custom_feature_violations(code: str) -> tuple[str | None, list[str]]:
    """Returns (syntax_error_message, forbidden_imports). forbidden_imports is
    only meaningful when syntax_error_message is None — code that doesn't
    parse can't be AST-walked for imports either."""
    try:
        tree = _ast.parse(code)
    except SyntaxError as e:
        return f"SyntaxError on line {e.lineno}: {e.msg}", []
    return None, _forbidden_imports_from_tree(tree)


async def generate_custom_feature(main_code: str, request_text: str) -> str:
    """Generates one custom_features/bot_<id>.py module. `main_code` is the
    bot's full existing file, given as read-only context only (never modified,
    never returned) — see the module-level comment above for why the whole
    file is sent rather than a hand-extracted excerpt. One retry on syntax
    error OR forbidden import, same one-shot-retry shape as fix_bot_code's
    SyntaxError handling; raises CustomFeatureGenerationError if the retry
    still doesn't come back clean.

    Once the code passes that hard gate, it also gets one
    _review_narrow_risk_code pass — this from-scratch path is otherwise the
    only generation path in the whole pipeline with zero LLM-based code
    review. If that pass's own output fails the same hard gate (should be
    rare — it only ever edits code that already passed), the narrow-review
    edit is discarded and the pre-review code (already known-good) is
    returned instead — no second retry, so this can't turn into an unbounded
    loop, and no exception, since the pre-review code is already a valid
    result."""
    user_msg = (
        f"Feature request from the bot owner:\n{request_text}\n\n"
        f"Existing bot code (READ-ONLY — for context and conventions only):\n{main_code}"
    )
    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8000,
        system=CUSTOM_FEATURE_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    code = _strip_code_fences(response.content[0].text)

    syntax_err, forbidden = _custom_feature_violations(code)
    if syntax_err or forbidden:
        problem = syntax_err or (
            f"You used forbidden imports: {', '.join(forbidden)}. "
            "Rewrite using ONLY the allowed packages listed in the system prompt."
        )
        retry_response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=8000,
            system=CUSTOM_FEATURE_SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": user_msg},
                {"role": "assistant", "content": code},
                {"role": "user", "content": f"{problem} Return ONLY corrected Python code, no markdown."},
            ],
        )
        code = _strip_code_fences(retry_response.content[0].text)
        syntax_err, forbidden = _custom_feature_violations(code)
        if syntax_err or forbidden:
            raise CustomFeatureGenerationError(syntax_err or f"forbidden imports: {', '.join(forbidden)}")

    pre_review_code = code
    code = await _review_narrow_risk_code(code, request_text, reference_code=main_code)
    syntax_err, forbidden = _custom_feature_violations(code)
    if syntax_err or forbidden:
        code = pre_review_code  # narrow review broke the hard gate — keep the already-valid pre-review code

    return code


EXPLAIN_CUSTOM_FEATURE_PROMPT = """Ты объясняешь владельцу Telegram-бота, что изменится в его боте после точечной доработки — ДО того, как изменение применится.

Пиши по-русски, 3-5 строк, простым языком, без кода и технических терминов (не говори "роутер", "хендлер", "aiosqlite", "модуль" и т.п.). Опиши, что теперь сможет делать бот или его пользователи, с точки зрения обычного человека. Используй HTML-теги (<b>, <i>) для акцентов.

Если в коде появляется новая команда (например /excel) — обязательно назови её."""


async def explain_custom_feature(patch_code: str, request_text: str) -> str:
    """Haiku-sized plain-language translation of a generated custom_features
    patch, shown to the owner alongside "✅ Применить"/"❌ Отмена" BEFORE the
    patch is written to disk — see generate_bot_guide for the closest existing
    analog (same idea, but that one runs AFTER creation; this is the first
    pre-apply confirmation step in the codebase)."""
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=500,
        system=EXPLAIN_CUSTOM_FEATURE_PROMPT,
        messages=[{
            "role": "user",
            "content": f"Владелец попросил:\n{request_text}\n\nСгенерированный код доработки:\n{patch_code}",
        }],
    )
    return response.content[0].text.strip()
