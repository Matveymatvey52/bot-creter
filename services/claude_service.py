import ast as _ast

from anthropic import AsyncAnthropic
from config import ANTHROPIC_API_KEY

client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

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


async def generate_bot_code(requirements_summary: str) -> str:
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

    # Static review pass — find and fix potential runtime issues before deployment
    code = await _review_bot_code(code, requirements_summary, bot_type=bot_type)

    return code
