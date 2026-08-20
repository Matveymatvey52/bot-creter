from __future__ import annotations

import ast as _ast
import asyncio
import json
import logging
import re

from anthropic import AsyncAnthropic
from config import ANTHROPIC_API_KEY

client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
logger = logging.getLogger(__name__)

def _event_type_catalog_text() -> str:
    """Renders features/office_events.py's closed _EVENT_TYPES/EVENT_TYPE_LABELS
    set into the system prompt so the model only ever offers links the runtime
    can actually deliver (docs/MULTIBOT_OFFICE_ROUTING_DESIGN.md §2.1/§4 q3) —
    imported lazily to avoid a features <-> services import-time cycle (mirrors
    features/office_events.py's own lazy `from runtime.registry import
    discover_features` for the same reason)."""
    from features.office_events import EVENT_TYPE_LABELS

    return "\n".join(f'- "{k}" ({v})' for k, v in EVENT_TYPE_LABELS.items())


GATHER_SYSTEM_PROMPT = """You are a Telegram bot development assistant. Your job is to understand what bot (or connected system of bots) the user wants to create.

The user may send, at any point in the conversation, screenshots, photos, extracted text from documents (PDF/Word/Excel), or web page/spreadsheet content alongside their message. Treat all of that as part of the requirements — read screenshots and documents carefully, they often describe the exact workflow, data fields, or interface the user wants automated.

Ask 1-2 concise clarifying questions at a time to understand:
- The bot's main purpose and functionality
- Key commands or features needed
- Any specific behaviors (stores data per user, sends notifications, etc.)
- Whether this is a single bot, several independent bots, or a connected system ("office") of bots that should notify each other

If the user wants two or more bots to notify each other of events (an "office"), the ONLY event types that can actually be wired up right now are:
{event_type_catalog}
If the user asks for a connection that doesn't match one of these, tell them plainly that this specific link isn't supported yet and continue without inventing a new event type — never make one up.

When you have enough information (usually after 2-4 exchanges), output exactly:
===READY_TO_GENERATE===
followed by ONE JSON object (nothing else after it) with this exact shape:
{{"bots": [{{"role_hint": "short_snake_case_tag", "summary": "structured summary in English of this one bot, with all key requirements, including anything learned from attached images/documents/links"}}], "links": [{{"source_role_hint": "...", "target_role_hint": "...", "event_type": "..."}}]}}

Rules for this JSON:
- A single bot is just "bots" with ONE element and "links": [].
- Several independent bots the user does NOT want connected: multiple "bots" entries, "links": [].
- An "office": multiple "bots" entries plus "links" entries connecting them by role_hint.
- role_hint is a short internal tag (e.g. "orders", "accounting"), NOT a bot name or username — it only has to be unique within this one response, used to wire "links" to the right "bots" entry.
- event_type in every "links" entry MUST be one of the exact strings from the list above — never invent one.

Always respond in the same language as the user (only the JSON keys/values in the payload itself are in English). Keep questions short."""

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

MANDATORY init_db FUNCTION — every bot that stores any data must define this exact
top-level function, even if there is only one table:
  async def init_db(db_path: str) -> None:
      async with aiosqlite.connect(db_path) as db:
          await db.execute("CREATE TABLE IF NOT EXISTS ... (...)")
          # one execute() per table, all CREATE TABLE IF NOT EXISTS
          await db.commit()
  Call it from main() as: await init_db(DB_PATH)
  Rules:
  - Takes db_path as a parameter and uses ONLY that parameter for the connection —
    never reads the module-level DB_PATH global inside this function body (the bot's
    hosting registry may call init_db with a different path than DB_PATH).
  - Contains ALL CREATE TABLE statements the bot uses anywhere else in the file —
    this is the single source of truth for schema, other code must never create
    tables outside this function.
  - Must be idempotent and safe to call multiple times (IF NOT EXISTS everywhere).

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

Correct main entry point (copy exactly; if the bot has no init_db function, omit that one line):
  async def main():
      bot = Bot(token=os.getenv("BOT_TOKEN"))
      dp = Dispatcher(storage=MemoryStorage())
      dp.include_router(router)
      await init_db(DB_PATH)
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

INIT DB — pre-populate slots at startup (MANDATORY, call from main() as await init_db(DB_PATH) before polling):
  async def init_db(db_path: str) -> None:
    async with aiosqlite.connect(db_path) as db:
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


# Variant D (docs/FEATURE_CONFIGURE_DIALOG_DESIGN.md) — the factory miniapp's
# "Фичи" tab conversational configure step. Given the running clarification
# thread for ONE (bot, feature) pair, decides whether the owner's latest
# message is a concrete-enough description to enable the feature, or whether
# Claude should keep asking. Unlike classify_connect_feature_intent (a fixed
# two-feature Telegram-side classifier), this covers every feature that
# collects free text on the "Фичи" tab (sheets/notifications/reminders/
# sales_analytics/voice_intake/sellable_items/cashflow_ledger — NOT payments,
# which skips straight to the existing ЮKassa wizard, and NOT office_events,
# which has its own bot-picker UI, no free text). feature_name is passed in
# so the same prompt can ask a feature-appropriate follow-up instead of a
# generic one.
FEATURE_CONFIGURE_PROMPT_TEMPLATE = """You are helping the owner of a Telegram-bot factory (Bot-creter) configure the "{feature_name}" feature for one of their bots (template: {template_id}).

{feature_context}

You will receive the conversation so far (owner's messages and your own previous follow-up questions, if any). Decide:
- If the owner's LATEST message describes what they want clearly and completely enough to actually configure the feature — accept it.
- If it's vague, ambiguous, or missing something you'd need to know to set this up correctly — ask ONE short, specific follow-up question in Russian.

Respond with ONLY a single line of valid JSON, no markdown fences, no explanation, in exactly this shape:
{{"accepted": true or false, "reply": "<Russian text>", "config_summary": "<short Russian summary of the accepted config, or null>"}}

Rules:
- accepted=true: "reply" is a short Russian confirmation summarizing what will happen (e.g. "Записываю: имя клиента, дату записи, услугу"). "config_summary" is the same text, or a slightly more compact version — this is what gets stored and shown back to the owner later under "Изменить описание".
- accepted=false: "reply" is your follow-up question ONLY — Russian, short (1-2 sentences), friendly, specific to what's actually missing. "config_summary" must be null.
- Never accept a message that is empty, off-topic, or just says "да"/"ок" with no actual content behind it in the conversation.
- Do not ask more than one question at a time even if several things are unclear — ask the single most important one first.
- If the conversation has already gone back and forth 3+ times and the owner is clearly trying but the answers stay vague, prefer accepting with your best understanding over stalling indefinitely — summarize what you DO understand rather than blocking forever."""

_FEATURE_CONFIGURE_CONTEXT: dict[str, str] = {
    "sheets": "This feature copies new records into a Google Sheet the owner connects separately. You need to know WHAT should be written to the sheet (which records/fields).",
    "notifications": "This feature lets the owner broadcast messages to everyone who has ever messaged the bot. You need to know what kind of messages they plan to send and roughly how often.",
    "reminders": "This feature sends automatic reminder DMs to clients ahead of an upcoming date/appointment. You need to know what the reminder is about and how long before the event it should fire.",
    "sales_analytics": "This feature shows business metrics inside the bot's own mini-app. Configuration here is optional — if the owner has no particular metric in mind, standard metrics (count of records, totals) are fine to accept.",
    "voice_intake": "This feature turns voice messages into structured records. You need to know what kind of voice messages should become records (e.g. new orders, new bookings) and what info should be extracted.",
    "sellable_items": "This feature is a configurable catalog of items the bot can sell. You need to know roughly what will be sold, or an explicit confirmation to start with an empty catalog the owner will fill in via /items.",
    "cashflow_ledger": "This feature tracks money in/out. You need to know how the owner wants entries grouped or categorized (or that a single running total with no categories is fine).",
}


def _parse_feature_configure_result(raw: str) -> dict:
    """Best-effort JSON parse — malformed output degrades to "not accepted,
    generic follow-up" rather than raising or silently enabling a feature
    with no real description (see FEATURE_CONFIGURE_PROMPT_TEMPLATE's own
    "never accept ... off-topic" rule — a parse failure must fail the same
    direction as an explicit non-acceptance, never the direction that
    enables something unreviewed)."""
    fallback = {
        "accepted": False,
        "reply": "Не понял, можешь описать подробнее?",
        "config_summary": None,
    }
    try:
        data = json.loads(_strip_code_fences(raw))
    except (json.JSONDecodeError, ValueError):
        return fallback
    if not isinstance(data, dict):
        return fallback
    accepted = bool(data.get("accepted"))
    reply = data.get("reply")
    if not isinstance(reply, str) or not reply.strip():
        return fallback
    config_summary = data.get("config_summary")
    if not accepted or not isinstance(config_summary, str) or not config_summary.strip():
        config_summary = None
    return {"accepted": accepted, "reply": reply.strip(), "config_summary": config_summary}


async def assess_feature_description(feature_name: str, template_id: str | None, thread: list[dict]) -> dict:
    """thread: [{"role": "owner"|"claude", "text": str}, ...], oldest first,
    ending with the owner's latest message (the one being assessed). Returns
    {"accepted": bool, "reply": str, "config_summary": str|None} — see
    _parse_feature_configure_result for the exact contract. Haiku, same cost
    tier as classify_connect_feature_intent — this is a short judgment call,
    not code generation."""
    feature_context = _FEATURE_CONFIGURE_CONTEXT.get(
        feature_name, "Configure this feature based on what the owner describes."
    )
    system = FEATURE_CONFIGURE_PROMPT_TEMPLATE.format(
        feature_name=feature_name,
        template_id=template_id or "from-scratch (no template)",
        feature_context=feature_context,
    )
    messages = [
        {"role": "user" if turn["role"] == "owner" else "assistant", "content": turn["text"]}
        for turn in thread
    ]
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system=system,
        messages=messages,
    )
    return _parse_feature_configure_result(response.content[0].text)


# docs/TEMPLATE_CANDIDATE_CLUSTERING_DESIGN.md §3 — one incremental batch
# classification call per pass of runtime/template_candidate_clustering.py.
# Given the domains already known and a batch of new from-scratch summaries,
# routes each summary to an existing domain or proposes a new one. No
# embeddings/vector search (see §2 of the design doc for why) — this keeps
# the classify-layer entirely inside the Haiku-call pattern every other
# classifier in this file already uses (_select_template, classify_bot_type,
# classify_connect_feature_intent).
TEMPLATE_CANDIDATE_CLUSTER_PROMPT = """You group Russian-language Telegram-bot build requests into recurring domain clusters, for a bot-factory owner deciding which requests are common enough to justify building a permanent reusable template.

You will receive:
1. A list of ALREADY-KNOWN domain clusters, each with an id, a short label, and a description.
2. A batch of NEW requests to classify, each with an index, a summary of what the client asked for, and a rough bot_type tag.

For EACH new request, decide:
- It belongs to an EXISTING cluster if its underlying need genuinely matches that cluster's domain (not just superficial keyword overlap — "voice-based expense logging for a car workshop" and "voice-based expense logging for a delivery service" belong together; "car workshop booking" and "car workshop expense voice log" do NOT, different needs).
- Otherwise it needs a NEW cluster — propose a short Russian label (a few words, naming the domain, not the specific business) and a one-sentence Russian description of what unites requests in it.

Respond with ONLY a single line of valid JSON, no markdown fences, no explanation, in exactly this shape:
{"assignments": [{"index": 0, "existing_cluster_id": 3, "new_label": null, "new_description": null}, {"index": 1, "existing_cluster_id": null, "new_label": "голосовой учёт расходов для сервисов", "new_description": "Клиент хочет надиктовывать траты голосом вместо ручного ввода в таблицу."}]}

Rules:
- Exactly one of existing_cluster_id / (new_label + new_description) is non-null per assignment — never both, never neither.
- existing_cluster_id must be one of the ids from the ALREADY-KNOWN list — never invent an id.
- If two or more NEW requests in this same batch belong together, give them the SAME new_label/new_description (do not mint duplicate near-identical clusters within one batch).
- Prefer routing to an existing cluster over minting a near-duplicate new one — but do not force a match that isn't real; a wrong merge is worse than a small number of clusters.
- index must match the input index exactly, one assignment per input request, no omissions."""


def _parse_template_candidate_cluster_assignments(raw: str, batch_size: int) -> list[dict]:
    """Best-effort JSON parse — any malformed/incomplete response degrades to
    "give up on this batch" (empty list) rather than raising, since a
    classification hiccup here must not crash the periodic sweep (see
    runtime/template_candidate_clustering.py's per-pass try/except)."""
    try:
        data = json.loads(_strip_code_fences(raw))
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    assignments = data.get("assignments")
    if not isinstance(assignments, list):
        return []
    result = []
    for item in assignments:
        if not isinstance(item, dict):
            continue
        index = item.get("index")
        if not isinstance(index, int) or not (0 <= index < batch_size):
            continue
        existing_cluster_id = item.get("existing_cluster_id")
        new_label = item.get("new_label")
        new_description = item.get("new_description")
        if isinstance(existing_cluster_id, int):
            result.append(
                {"index": index, "existing_cluster_id": existing_cluster_id, "new_label": None, "new_description": None}
            )
        elif isinstance(new_label, str) and new_label.strip():
            result.append(
                {
                    "index": index,
                    "existing_cluster_id": None,
                    "new_label": new_label.strip(),
                    "new_description": new_description.strip() if isinstance(new_description, str) else None,
                }
            )
        # else: neither shape present — drop this assignment, candidate stays
        # unclustered and is retried on the next pass.
    return result


async def classify_template_candidate_clusters(known_clusters: list[dict], batch: list[dict]) -> list[dict]:
    """known_clusters: [{"id", "label", "description"}, ...] (existing
    domains). batch: [{"summary", "bot_type"}, ...] (new candidates, in the
    same order callers must map results back by "index"). Returns a list of
    {"index", "existing_cluster_id", "new_label", "new_description"} — see
    _parse_template_candidate_cluster_assignments for the exact shape and
    degrade-on-malformed-response behavior. Haiku, same cost tier as the
    other classifiers in this file; batch is expected to be small (tens of
    rows per daily pass, not thousands — see design doc §3)."""
    known_lines = (
        "\n".join(f"- id={c['id']}: {c['label']} — {c.get('description') or ''}" for c in known_clusters)
        or "(пока нет ни одного кластера)"
    )
    batch_lines = "\n".join(
        f"{i}. bot_type={item.get('bot_type') or 'general'}: {item['summary']}" for i, item in enumerate(batch)
    )
    user_content = f"ALREADY-KNOWN domain clusters:\n{known_lines}\n\nNEW requests to classify:\n{batch_lines}"
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4096,
        system=TEMPLATE_CANDIDATE_CLUSTER_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    )
    return _parse_template_candidate_cluster_assignments(response.content[0].text, len(batch))


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
        "- init_db(db_path: str) is defined at module level and called as await init_db(DB_PATH) inside main() before dp.start_polling\n"
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
    system = GATHER_SYSTEM_PROMPT.format(event_type_catalog=_event_type_catalog_text())
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2048,
        system=system,
        messages=conversation,
    )
    return response.content[0].text


def parse_gather_result(ready_payload: str) -> dict | None:
    """Parses the JSON payload GATHER_SYSTEM_PROMPT emits after
    ===READY_TO_GENERATE=== into {"bots": [{"role_hint", "summary"}, ...],
    "links": [{"source_role_hint", "target_role_hint", "event_type"}, ...]}.

    Falls back to treating the whole payload as a single bot's plain-text
    summary (docs/MULTIBOT_OFFICE_ROUTING_DESIGN.md §3.1's noted risk: Haiku
    may occasionally drop the JSON envelope under conversational pressure) —
    this keeps the single-bot path working even if the model regresses to
    the old plain-text format, at the cost of never detecting multi-bot/office
    intent in that fallback case. Returns None only if payload is empty.
    """
    payload = ready_payload.strip()
    if not payload:
        return None
    try:
        from features.office_events import EVENT_TYPE_LABELS

        parsed = json.loads(payload)
        bots = parsed.get("bots")
        if isinstance(bots, list) and bots and all(
            isinstance(b, dict) and isinstance(b.get("summary"), str) and b["summary"].strip()
            for b in bots
        ):
            links = parsed.get("links")
            # A hallucinated event_type outside the closed set (docs/
            # MULTIBOT_OFFICE_ROUTING_DESIGN.md §4 q3, decision "а") is
            # dropped silently here rather than raising — the prompt already
            # tells the model not to invent one, this is the last-resort
            # guard so a slip can't reach add_office_link() downstream.
            return {
                "bots": [
                    {"role_hint": str(b.get("role_hint") or f"bot_{i+1}"), "summary": b["summary"].strip()}
                    for i, b in enumerate(bots)
                ],
                "links": [
                    {
                        "source_role_hint": str(link["source_role_hint"]),
                        "target_role_hint": str(link["target_role_hint"]),
                        "event_type": str(link["event_type"]),
                    }
                    for link in (links if isinstance(links, list) else [])
                    if isinstance(link, dict) and link.get("source_role_hint")
                    and link.get("target_role_hint") and link.get("event_type") in EVENT_TYPE_LABELS
                ],
            }
    except (json.JSONDecodeError, TypeError, KeyError, AttributeError):
        pass
    return {"bots": [{"role_hint": "bot_1", "summary": payload}], "links": []}


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


# ── from-scratch registry wiring (docs/OFFICE_HOOK_FROM_SCRATCH_BOTS.md) ────
#
# From-scratch bots (no matching templates/*.py, GENERATE_SYSTEM_PROMPT branch
# of _generate_bot_code_inner) are free-form single-file scripts — Claude is
# NOT asked to hand-write the by-convention exports runtime/registry.py's
# build_entry() needs (config_from_bot_row/ConfigMiddleware/on_office_event),
# unlike templates/*.py. That's deliberate: those three are 100% boilerplate
# (verified identical in shape across every templates/*.py — see the design
# doc) and asking an LLM to freehand them per-bot is both wasted tokens and a
# reliability risk (one hallucinated attribute name silently breaks
# build_entry()'s router/office-hook wiring for that bot). Instead this
# function deterministically APPENDS them via plain text/AST, never an LLM
# call — same reasoning as generic_on_office_event() itself being
# data-driven, not code-generated.
#
# Requires DB_PATH and BOT_NAME as module-level globals and (optionally)
# `async def init_db(db_path: str)` — both guaranteed by GENERATE_SYSTEM_PROMPT
# ("PERSISTENT DATA" / "MANDATORY init_db FUNCTION" sections). Bots with no
# persistent data at all (no DB_PATH) skip init_db entirely — config_from_bot_row
# still needs SOME db_path, so DB_PATH is required unconditionally by the
# prompt even for bots that never write to it.

_INIT_DB_DEF_RE = re.compile(r"^async def init_db\(", re.MULTILINE)
_ENTRY_POINT_RE = re.compile(r"^if __name__ == ['\"]__main__['\"]:", re.MULTILINE)

_FROM_SCRATCH_WIRING_TEMPLATE = '''

# ── auto-appended by services.claude_service — DO NOT ask Claude to write this ──
# Registry wiring for webhook-mode registration (runtime/registry.py's build_entry()).
# Mirrors the by-convention exports every templates/*.py file provides —
# including attribute-style config.db_path/config.bot_id access, since
# runtime/registry.py's build_entry() reads typed_config.db_path directly
# (not config["db_path"]) for every module regardless of template vs
# from-scratch origin.
from types import SimpleNamespace as _SimpleNamespace

from aiogram import BaseMiddleware as _BaseMiddleware


def config_from_bot_row(bot_row: dict, data_dir) -> _SimpleNamespace:
    """Webhook runtime mode — reuses this bot's OWN DB_PATH/BOT_NAME globals
    (already unique per bot: one file per bot in data/generated_bots/), not a
    bot_row["bot_id"]-derived path like templates/*.py use, since this file's
    module-level code and handlers already close over DB_PATH directly."""
    return _SimpleNamespace(
        bot_id=bot_row.get("bot_id"),
        bot_name=BOT_NAME,
        db_path=DB_PATH,
        display_name=bot_row.get("display_name"),
        group_chat_id=bot_row.get("group_chat_id"),
    )


class ConfigMiddleware(_BaseMiddleware):
    """Injects this bot's config namespace into data["config"] — same contract
    as every templates/*.py ConfigMiddleware (identical shape, verified across
    all reference templates)."""

    def __init__(self, config: _SimpleNamespace) -> None:
        self.config = config
        super().__init__()

    async def __call__(self, handler, event, data):
        data["config"] = self.config
        return await handler(event, data)


async def on_office_event(event, config) -> None:
    """Universal office-hook fallback (docs/OFFICES_DESIGN.md §11) — same
    generic_on_office_event() every template-based bot with no hand-written
    hook gets via build_entry(), now reachable for from-scratch bots too."""
    from db.database import get_bot_office_hook_config
    from features.office_events import generic_on_office_event

    bot_id = config.bot_id
    hook_config = await get_bot_office_hook_config(bot_id) if bot_id is not None else None
    await generic_on_office_event(event, config.db_path, hook_config, bot_id=bot_id)
'''


def _needs_from_scratch_wiring(code: str) -> bool:
    """True if `code` is missing ANY of the three registry-wiring exports this
    module appends — checked so re-running this on already-wired code (e.g.
    after a cb_recreate/improve_bot_code LLM rewrite) re-appends whatever the
    rewrite dropped, instead of trusting one export's presence to mean all
    three survived. Checking only config_from_bot_row() was a false-negative
    risk: an LLM rewrite pass (IMPROVE_SYSTEM_PROMPT etc.) has no knowledge of
    this appended boilerplate and could keep config_from_bot_row while
    dropping ConfigMiddleware or on_office_event, leaving a module that
    passes this check but still crashes _build_generic_middleware with
    AttributeError at registration. Cheap substring check, not full AST —
    mirrors _mentions_payments' style."""
    return not (
        "def config_from_bot_row(" in code
        and "class ConfigMiddleware(" in code
        and "async def on_office_event(" in code
    )


_INIT_DB_FALLBACK = '''

async def init_db(db_path: str) -> None:
    """Auto-appended fallback — this bot defined no top-level init_db of its
    own (no persistent data to create tables for). runtime/registry.py's
    build_entry() calls module.init_db(config.db_path) unconditionally for
    every module (template-based or from-scratch alike, same contract every
    templates/*.py file already guarantees) — this no-op keeps that call
    from raising AttributeError for a stateless bot."""
    pass
'''


def append_from_scratch_registry_wiring(code: str) -> str:
    """Deterministically appends config_from_bot_row/ConfigMiddleware/
    on_office_event (plus a no-op init_db fallback if the bot defined none)
    to a from-scratch bot's generated code, right before its
    `if __name__ == "__main__":` block — see the module comment above for why
    this is text injection, not an LLM step. No-ops (returns code unchanged)
    if the code already has the wiring (_needs_from_scratch_wiring) or lacks
    an entry point to insert before (defensive — GENERATE_SYSTEM_PROMPT always
    produces one; _generate_bot_code_inner's own retry loop guarantees
    'asyncio.run(main())' is present by the time this runs)."""
    if not _needs_from_scratch_wiring(code):
        return code
    # Last match, not first: a column-0 `if __name__ == "__main__":` line
    # could in principle appear inside an unindented docstring/comment
    # example earlier in the file — the REAL entry point is always the last
    # such line in valid generated code (nothing meaningful follows
    # asyncio.run(main())), so this reduces (does not eliminate — the
    # _ast.parse safety net below is the real guard) the chance of splicing
    # into the wrong place.
    matches = list(_ENTRY_POINT_RE.finditer(code))
    m = matches[-1] if matches else None
    if m is None:
        logger.warning(
            "append_from_scratch_registry_wiring: no `if __name__ == '__main__':` "
            "found — skipping wiring injection, bot will only run via its own "
            "asyncio.run(main()), not through the webhook registry"
        )
        return code
    wiring = _FROM_SCRATCH_WIRING_TEMPLATE
    if not _INIT_DB_DEF_RE.search(code):
        wiring += _INIT_DB_FALLBACK
    wired = code[: m.start()] + wiring.strip() + "\n\n\n" + code[m.start() :]
    try:
        _ast.parse(wired)
    except SyntaxError:
        logger.warning(
            "append_from_scratch_registry_wiring: injected code failed to parse — "
            "keeping original code unwired (bot still works standalone, just not "
            "through the webhook registry/office-hook)"
        )
        return code
    return wired


_TEMPLATES_DIR = __import__("pathlib").Path(__file__).parent.parent / "templates"

# Same "# TEMPLATE: <id>" marker convention runtime/registry.py's
# infer_template_id() reads — a separate constant/regex lives here instead of
# importing from runtime/, to avoid a services -> runtime dependency (this
# module has its own from-scratch wiring above; that's a different mechanism,
# not a TEMPLATE:-marker path). Read by LINE COUNT, not a byte/char
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


_FREEDOM_TIER_PROMPT = """You are deciding how much freedom a code generator needs when adapting ONE existing bot template to a specific request.

Answer with exactly one word:
- "customize" — the request can be satisfied by only changing the template's text/constants (business name, welcome text, lists of services/categories/items, wording) — no new tables, no new handlers, no new FSM states, no new integrations.
- "hybrid" — the request needs the template extended: new database tables/columns, new handlers, new FSM states/steps, or functionality the template's constants cannot express, even though the template is still clearly the right starting point.

Return ONLY "customize" or "hybrid", nothing else — no explanation."""


async def _select_freedom_tier(template_code: str, requirements: str) -> str:
    """Only called when _select_template resolved to exactly one template.
    Decides whether that template needs pure # CUSTOMIZE-block editing or
    structural extension (hybrid mode, see docs/HYBRID_GENERATION_MODE_DESIGN.md).
    Cheap Haiku call, isolated from _select_template's own prompt/contract so
    that function's existing fail-closed template-name validation is
    untouched. Fails closed to "customize" (the narrower, safer freedom
    level) on any response that isn't exactly one of the two expected
    words — a wrong "customize" call costs an extra generation round trip
    later at worst; a wrong "hybrid" call skips the # CUSTOMIZE boundary
    while narrow-risk review still runs, so the fail-closed direction here
    picks the cheaper mistake, not zero risk. Takes the template's already-
    read source (not a template_name) so the caller reads the template file
    from disk exactly once for the whole hybrid pipeline instead of once
    per function."""
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=10,
        system=_FREEDOM_TIER_PROMPT,
        messages=[{
            "role": "user",
            "content": f"Requirements:\n{requirements}\n\nTemplate:\n{template_code}",
        }],
    )
    text = response.content[0].text.strip().lower()
    return "hybrid" if text == "hybrid" else "customize"


HYBRID_CUSTOMIZE_EXTRA = """

HYBRID MODE — you are extending ONE existing working template, not designing from a blank page and not merging multiple templates. Treat it as your trusted starting point:

- Preserve its working conventions (existing table names, callback_data prefixes, existing FSM state chains, existing handler structure) — extend them rather than replacing them, whenever the request can be satisfied that way.
- You ARE allowed full structural freedom when the request genuinely requires it: add new tables/columns, add new handlers, add new FSM states, add new callback routes, or restructure existing ones — do not limit yourself to # CUSTOMIZE blocks like a plain customization pass would.
- A full rewrite of a section is allowed only when the requirements genuinely cannot be expressed as an extension of what's already there — prefer the smallest structural change that satisfies the request.
- Keep the template's `# TEMPLATE: <id>` header line and its startup/persistence conventions (DB_PATH via DATA_DIR, BOT_NAME from Path(__file__).stem, CREATE TABLE IF NOT EXISTS) exactly as they already work in the template — these are required for compatibility with the bot factory's office-event hooks, mini-app config generation, and multi-bot registry, regardless of how much else you change.

Return ONLY the complete, single Python file. No markdown fences, no explanations."""


HYBRID_REVIEW_SYSTEM_PROMPT = """You are a senior Python code reviewer specializing in aiogram 3.13 Telegram bots. You are reviewing a bot that started from ONE existing template and was then EXTENDED with new structure (tables, handlers, FSM states) to satisfy a specific request (HYBRID MODE — template as trusted base, not from-scratch, not a multi-template merge).

You are given the ORIGINAL template and the HYBRID output. Your job is to catch defects specific to extending a known-good base without a full rewrite, BEFORE deployment. Check for these specific problems:

1. NEEDLESSLY RENAMED CONVENTIONS — an existing table name, callback_data prefix, or FSM state that got renamed/duplicated for no reason tied to the request (the original name still works and nothing in the requirements calls for the rename).
2. DUPLICATED STRUCTURE — a new table, handler, or state added that duplicates something the original template already had, instead of extending the original.
3. BROKEN COMPATIBILITY CONVENTIONS — the `# TEMPLATE: <id>` header line, DB_PATH/DATA_DIR/BOT_NAME pattern, or CREATE TABLE IF NOT EXISTS persistence pattern altered or removed from how the original template already had them working.
4. ORPHANED ORIGINAL CODE — a handler, table, or state from the original template left in the file but now unreachable/unused because the new structure bypasses it.
5. INCOMPLETE EXTENSION — new structure (e.g. a new table) added but not fully wired (e.g. never read from, or a new FSM state with no handler consuming it).

If you find issues: fix them and return the complete corrected code.
If the code looks correct: return it unchanged.
Return ONLY valid Python code. No markdown, no explanations."""


async def _review_hybrid_bot_code(code: str, requirements: str, original_template_code: str) -> str:
    """Template-diff-aware review pass, unique to hybrid mode — has both the
    original template and the hybrid output so it can specifically check
    whether existing conventions were needlessly renamed/duplicated/broken,
    which neither _review_bot_code nor _review_narrow_risk_code check for.
    Runs first in the hybrid pipeline (see _generate_bot_code_inner), same
    position _review_merged_bot_code holds in the synthesis pipeline, so
    later passes review the already-diff-cleaned structure."""
    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=25000,
        system=HYBRID_REVIEW_SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": (
                f"Bot requirements (for context):\n{requirements}\n\n"
                f"Original template (for comparison only):\n{original_template_code}\n\n"
                f"Hybrid-extended code to review:\n{code}"
            ),
        }],
    )
    reviewed = _strip_code_fences(response.content[0].text)
    try:
        _ast.parse(reviewed)
        return reviewed
    except SyntaxError:
        return code  # review broke the code — keep pre-review version


async def _hybrid_customize_from_template(template_code: str, requirements: str) -> str:
    """Hybrid mode: template as trusted structural base with full freedom to
    extend it (new tables/handlers/FSM), unlike _customize_from_template's
    # CUSTOMIZE-only ceiling. See docs/HYBRID_GENERATION_MODE_DESIGN.md.
    Falls back to the unmodified template on SyntaxError, same fail-safe
    _customize_from_template uses — there's always a known-good file to
    return to since exactly one template is the starting point. Takes the
    template's already-read source (not a template_name) — see
    _select_freedom_tier's docstring for why."""
    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=25000,
        system=GENERATE_SYSTEM_PROMPT + HYBRID_CUSTOMIZE_EXTRA,
        messages=[{
            "role": "user",
            "content": f"User requirements:\n{requirements}\n\nTemplate to extend:\n{template_code}",
        }],
    )
    code = _strip_code_fences(response.content[0].text)
    try:
        _ast.parse(code)
        return code
    except SyntaxError:
        return template_code  # fallback to unmodified template


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


# Narrow, risk-targeted review pass — reserved for the riskiest generation
# paths (synthesis, hybrid single-template extension, and custom_features,
# see their call sites below), plus (unconditionally, see
# generate_bot_code's from-scratch branch) the from-scratch fallback. Checks
# three things neither _review_bot_code nor _review_merged_bot_code /
# _review_hybrid_bot_code cover as a dedicated concern: FSM state-transition
# correctness, SQL parameterization / cross-bot data isolation (not checked
# ANYWHERE else in the pipeline today), and duplication/dead code — plus a
# 4th, conditional PAYMENT/MONEY SAFETY category appended only when the code
# or context mentions payments, so the prompt (and token cost) stays at its
# original size for the common non-payment case. Deliberately not run for
# plain single-template customization (the "customize" freedom tier) —
# that path only changes a `# CUSTOMIZE` block inside an already-reviewed
# template, so the narrow-risk categories don't apply. Once a request is
# routed to the "hybrid" freedom tier instead, this DOES run — see
# docs/HYBRID_GENERATION_MODE_DESIGN.md §3: structural freedom over a
# template reintroduces the same bug classes this pass exists for.
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


async def generate_bot_code(
    requirements_summary: str,
) -> tuple[str, dict | None, dict | None, dict | None, dict | None, dict | None]:
    """Generates the bot's code, then a mini-app config, an office-hook
    config, AND a voice-intake/cashflow-ledger config for it (docs/
    MINIAPP_DESIGN.md §6, docs/OFFICES_DESIGN.md §11, docs/
    VOICE_CASHFLOW_FROM_SCRATCH_DESIGN.md) — three cheap Haiku calls, none
    blocking: any failure degrades to None, same as a template with no
    miniapp_config/office_hook_config/voice_cashflow_config today.

    office_hook_config is generated for every bot regardless of how the code
    was produced (single-template customization, template synthesis, or
    fully from-scratch) — cheap to compute and harmless to store even when
    unused. Whether it's actually WIRED into a live on_office_event() hook is
    decided later, at registry build time (runtime/registry.py's
    build_entry()). For from-scratch bots (no `# TEMPLATE:` marker),
    _generate_bot_code_inner's from-scratch branch appends the by-convention
    config_from_bot_row/ConfigMiddleware/on_office_event exports via
    append_from_scratch_registry_wiring() below, and build_entry() imports
    the generated file directly by path (see docs/OFFICE_HOOK_FROM_SCRATCH_
    BOTS.md) — the same generic_on_office_event() fallback template-based
    bots get is now reachable for from-scratch bots too. voice_cashflow_config
    is generated the same way, but wired through the bot_features opt-in
    table instead (see handlers/create_bot.py, which auto-enables the
    voice_intake/cashflow_ledger features when this config is non-null,
    rather than requiring the owner to manually toggle a feature they'd have
    no way to discover).

    All three configs only depend on `code`, already generated by the time
    any of them runs, so they run concurrently via asyncio.gather rather than
    sequentially — review found that serial awaits sharing ONE caller-side
    timeout (handlers/create_bot.py's asyncio.wait_for(..., timeout=360.0) /
    handlers/manage_bots.py's cb_recreate at 240.0) meant a slow/hung Haiku
    call on any cheap step could burn the whole budget and fail an
    otherwise-successful code generation. Concurrency also keeps the
    best-case added latency from these steps to the slowest single call
    rather than their sum.

    fallback_info (docs/TEMPLATE_CANDIDATE_LOGGING_DESIGN.md) is a fifth,
    optional element: None when a single template was customized/hybrid-
    extended or two templates were synthesized successfully, otherwise a
    dict describing why generation fell through to from-scratch —
    {"reason": "no_template_match" | "customize_failed" | "hybrid_failed"
    | "synthesis_failed", "selected_templates": [...]}.
    Callers with a bot_id in hand (handlers/create_bot.py,
    handlers/manage_bots.py's cb_recreate) persist it via
    db.database.add_template_candidate so the owner can review recurring
    from-scratch requests in the /analytics dashboard. No candidate is
    logged when a template match succeeded — only the fallback cases.

    miniapp_failure_info is a sixth, optional element, same shape/posture as
    fallback_info but for _generate_miniapp_config specifically: None on
    success (including the valid "this bot needs no mini-app" answer),
    otherwise {"reason": "timeout" | "api_error" | "parse_error"
    | "validation_failed"} once _generate_miniapp_config's own single retry
    has also failed. Callers with a bot_id in hand persist it via
    db.database.add_miniapp_config_failure and notify the bot's creator —
    unlike fallback_info (which is about the bot's CODE falling back to
    from-scratch, still a working bot), a miniapp failure means the owner
    silently got a bot with no mini-app at all and would otherwise have no
    way to know without noticing its absence themselves."""
    code, fallback_info = await _generate_bot_code_inner(requirements_summary)
    (
        (miniapp_config, miniapp_failure_info),
        office_hook_config,
        voice_cashflow_config,
    ) = await asyncio.gather(
        _generate_miniapp_config(code, requirements_summary),
        _generate_office_hook_config(code, requirements_summary),
        _generate_voice_cashflow_config(code, requirements_summary),
    )
    return code, miniapp_config, office_hook_config, voice_cashflow_config, fallback_info, miniapp_failure_info


async def _generate_bot_code_inner(requirements_summary: str) -> tuple[str, dict | None]:
    # Try template(s) first — saves 5-10x tokens vs generating from scratch
    templates = await _select_template(requirements_summary)

    if len(templates) == 1:
        template_name = templates[0]
        # Single disk read for the whole single-template branch — tier
        # selection, hybrid generation, and hybrid review all need this same
        # source text, so it's read once here and threaded through instead
        # of each function re-reading the file itself.
        template_code = (_TEMPLATES_DIR / f"{template_name}.py").read_text(encoding="utf-8")
        # _select_freedom_tier and classify_bot_type are both independent
        # Haiku calls that only depend on requirements_summary/template_code
        # (neither needs generated code) — run concurrently rather than
        # sequentially, same asyncio.gather idiom generate_bot_code already
        # uses for its own independent post-generation steps. No
        # return_exceptions=True: if either call raises, the whole
        # single-template branch is unrecoverable anyway (both results are
        # required to proceed), and the exception propagates to the same
        # outer asyncio.wait_for/except Exception safety net every other
        # generation tier already relies on (handlers/create_bot.py). Known,
        # accepted tradeoff: on a failure of either call, the other keeps
        # running to completion as an orphaned task rather than being
        # cancelled (gather's default behavior) — a small wasted Haiku call,
        # not a crash risk, since both calls are cheap/fast.
        tier, bot_type = await asyncio.gather(
            _select_freedom_tier(template_code, requirements_summary),
            classify_bot_type(requirements_summary),
        )
        if tier == "hybrid":
            code = await _hybrid_customize_from_template(template_code, requirements_summary)
            if "asyncio.run(main())" in code:
                code = await _review_hybrid_bot_code(code, requirements_summary, template_code)
                code = await _review_narrow_risk_code(code, requirements_summary)
                code = await _review_bot_code(code, requirements_summary, bot_type=bot_type)
                # append_from_scratch_registry_wiring no-ops for a genuine
                # template-derived file (its # TEMPLATE: marker survived, so
                # _needs_from_scratch_wiring finds config_from_bot_row/
                # ConfigMiddleware/on_office_event already present via the
                # template's own definitions) — but the hybrid tier is
                # explicitly allowed MORE structural freedom than plain
                # customize (that's the whole point of the tier), so the
                # marker/exports surviving is not guaranteed here either.
                # Same protection the from-scratch path gets.
                return append_from_scratch_registry_wiring(code), None
            fallback_reason = "hybrid_failed"
        else:
            code = await _customize_from_template(template_name, requirements_summary)
            if "asyncio.run(main())" in code:
                code = await _review_bot_code(code, requirements_summary, bot_type=bot_type)
                # CUSTOMIZE_TEMPLATE_PROMPT never GUARANTEES the marker/
                # exports survive an LLM rewrite either — not a redundant
                # safety net, same protection as the hybrid tier above.
                return append_from_scratch_registry_wiring(code), None
            fallback_reason = "customize_failed"

    elif len(templates) == 2:
        code = await _synthesize_from_templates(templates, requirements_summary)
        if "asyncio.run(main())" in code:
            code = await _review_merged_bot_code(code, requirements_summary)
            code = await _review_narrow_risk_code(code, requirements_summary)
            bot_type = await classify_bot_type(requirements_summary)
            code = await _review_bot_code(code, requirements_summary, bot_type=bot_type)
            # _synthesize_from_templates uses GENERATE_SYSTEM_PROMPT (the
            # from-scratch prompt) + MERGE_TEMPLATES_EXTRA — synthesized code
            # is structurally from-scratch (no guaranteed # TEMPLATE: marker
            # or config_from_bot_row/ConfigMiddleware/on_office_event) even
            # though it started from two templates as reference material.
            # Without this, a synthesized bot has neither a resolvable
            # template_id NOR the appended wiring — build_entry() would
            # import it fine via file_path (module is not None) but crash
            # _build_generic_middleware with AttributeError on
            # module.config_from_bot_row, silently dropping the bot from the
            # registry (Registry.add_or_replace swallows the exception).
            return append_from_scratch_registry_wiring(code), None
        fallback_reason = "synthesis_failed"

    else:
        fallback_reason = "no_template_match"

    fallback_info = {"reason": fallback_reason, "selected_templates": templates}

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

    # Registry wiring (docs/OFFICE_HOOK_FROM_SCRATCH_BOTS.md) — deliberately
    # LAST, after every LLM review pass above: those passes aren't told about
    # this appended boilerplate and could otherwise "fix"/drop it since they
    # each see and re-emit the whole file. Text injection, not an LLM call —
    # see append_from_scratch_registry_wiring's own docstring for why.
    code = append_from_scratch_registry_wiring(code)

    return code, fallback_info


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
            "title": "Туры",
            "titleField": "name",
            "fields": [
                {"name": "name", "required": true, "label": "Название", "kind": "text", "list": false, "detail": false, "create": true},
                {"name": "destination", "label": "Направление", "kind": "text", "list": true, "detail": true, "create": true},
                {"name": "date_start", "label": "Начало", "kind": "date", "list": false, "detail": true, "create": true},
                {"name": "date_end", "label": "Окончание", "kind": "date", "list": false, "detail": true, "create": true},
                {"name": "guests_count", "label": "Гостей", "kind": "number", "list": false, "detail": true, "create": true},
                {"name": "status", "label": "Статус", "kind": "status", "list": true, "detail": true, "create": false},
                {"name": "created_at", "creatable": false, "label": "Создано", "kind": "date", "list": false, "detail": false, "create": false}
            ]
        },
        {
            "name": "guests",
            "table": "guests",
            "order_by": "created_at DESC",
            "creatable": true,
            "title": "Гости",
            "titleField": "name",
            "fields": [
                {"name": "tour_id", "required": true, "label": "ID тура", "kind": "number", "list": false, "detail": false, "create": true},
                {"name": "name", "required": true, "label": "Имя гостя", "kind": "text", "list": false, "detail": false, "create": true},
                {"name": "total_cost", "label": "Стоимость", "kind": "number", "list": true, "detail": true, "create": true},
                {"name": "prepaid", "label": "Предоплата", "kind": "number", "list": false, "detail": true, "create": true},
                {"name": "our_price", "label": "Наша цена", "kind": "number", "list": false, "detail": true, "create": false},
                {"name": "status", "label": "Статус", "kind": "status", "list": true, "detail": true, "create": false},
                {"name": "notes", "label": "Заметки", "kind": "text", "list": false, "detail": true, "create": false},
                {"name": "created_at", "creatable": false, "label": "Создано", "kind": "date", "list": false, "detail": false, "create": false}
            ]
        }
    ]
}"""

MINIAPP_CONFIG_SYSTEM_PROMPT = f"""You extract a mini-app schema from a Telegram bot's Python source code, for a factory that builds Telegram bots.

You will be given the bot's full source code (its init_db() function contains the real CREATE TABLE statements — this is your only source of truth for table and column names) and a short description of what the bot does.

Produce a JSON object describing which of the bot's SQLite tables deserve a screen in a generic mini-app (a shared list/detail/create-form UI that reads this schema by convention AND renders it for a human — you are NOT writing any UI code, only describing the data and how to label/format it).

Here is one correct real example, from a tour-operator bot (tours + guests are real tables in that bot's init_db()):

{_TOUR_OPERATOR_MINIAPP_CONFIG_EXAMPLE}

Rules:
- Respond with ONLY the JSON object, no markdown fences, no explanation.
- Top-level shape: {{"resources": [...]}}. Each resource: "name" (short identifier), "table" (MUST be a real table name from the given code's CREATE TABLE statements, verbatim), "order_by" (a real column, e.g. "created_at DESC" or "id DESC"), "creatable" (true/false), "title" (short human-readable plural name for this resource, in the SAME language as the bot description — e.g. "Туры" for a Russian tour bot, "Tours" for an English one), "titleField" (the field name whose value best identifies a single record to a human, e.g. a name/title column — pick one that exists in "fields"), "fields" (ordered list; order is the display order).
- Each field: {{"name": ..., "required": true/false (optional, default false), "creatable": true/false (optional, default true — false means the column is server-set, like a timestamp or computed value, and must never appear in the create form), "label": a short human-readable label for this field in the bot's language (e.g. "Направление" not "destination"), "kind": one of "text" | "number" | "date" | "status" (pick "status" only for a column whose value is a small fixed set of state-like strings, e.g. pending/paid/confirmed; "number" for anything numeric; "date" for a date or datetime column; "text" otherwise), "list": true/false (show this field as a chip in the compact list-row view — pick 1-3 of the most important non-identifying fields per resource, not everything), "detail": true/false (show this field in the full single-record detail view — most fields should be true here, except the record's own title/name field which is already shown as the heading), "create": true/false (show this field as an input in the create form — matches "creatable" for most fields, but a field can be creatable at the DB layer yet still deliberately left off the create form if a human would never fill it in by hand, e.g. a foreign-key id better left for a future version)}}.
- Every "table" and every field "name" MUST match a real table/column that appears in the given code's CREATE TABLE statements. Never invent a table or column that isn't there.
- Only include tables that hold user-facing records worth browsing (bookings, orders, items, clients, etc). Skip purely internal/administrative tables (admins, state, sessions, migration/version tables, FSM storage).
- If the bot has no table worth showing as a mini-app screen (e.g. a purely conversational bot with no data table, or only internal tables), respond with exactly {{"resources": []}}. This is a valid, expected answer — not an error.
- Do not include any resource whose table you are not certain is a literal, verbatim CREATE TABLE name in the given code."""


def _extract_create_table_names(bot_code: str) -> dict[str, set[str]]:
    """Best-effort scan of init_db()'s CREATE TABLE statements: table name ->
    set of column names. Regex, not a SQL parser — good enough to catch a
    hallucinated table/column (the failure mode this guards against), not
    meant to validate SQL syntax. Never raises on malformed input.

    Column list ends at the first ')' followed by either ';' (a SQL
    statement terminator — needed for templates that batch multiple CREATE
    TABLEs into one db.executescript(\"\"\"...;...;...\"\"\") block, e.g.
    templates/tour_operator.py's init_db()) OR the enclosing Python string's
    own closing '\"\"\"'/"'''" (needed for templates that call
    db.execute(\"\"\"CREATE TABLE ...\"\"\") once per table with no
    trailing ';', e.g. templates/course_tracker.py's init_db()). Requiring
    ONLY one of the two (either prior version of this pattern) silently
    dropped every table for whichever style wasn't covered, which made
    _validate_miniapp_config_against_code reject every resource as
    hallucinated even when the model got the schema exactly right."""
    tables: dict[str, set[str]] = {}
    for table_match in re.finditer(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s*\((.*?)\)\s*(?:;|\"\"\"|''')",
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


def _parse_miniapp_config(raw: str, bot_code: str) -> tuple[dict | None, str | None]:
    """Best-effort JSON parse + schema validation — any malformed shape or
    hallucinated table/column degrades to (None, reason) (never raises),
    mirroring _parse_connect_feature_intent's fallback pattern: a parsing
    hiccup here must never surface an error or block bot creation.

    Returns (config, failure_reason). failure_reason is one of
    "parse_error" (malformed JSON/shape) or "validation_failed"
    (well-formed JSON that didn't pass schema/table/column validation) on
    failure, or None on success — including the valid "no mini-app needed"
    answer ({"resources": []}), which returns (None, None) since that's not
    a failure at all, just nothing to store (see
    _generate_miniapp_config's docstring for how callers distinguish the
    two None-config cases)."""
    try:
        data = json.loads(_strip_code_fences(raw))
    except (json.JSONDecodeError, ValueError):
        return None, "parse_error"
    if not isinstance(data, dict) or not isinstance(data.get("resources"), list):
        return None, "parse_error"
    if not data["resources"]:
        return None, None  # valid "no mini-app for this bot" answer, nothing to store
    for resource in data["resources"]:
        if not isinstance(resource, dict):
            return None, "parse_error"
        if not isinstance(resource.get("name"), str) or not isinstance(resource.get("table"), str):
            return None, "parse_error"
        if not isinstance(resource.get("fields"), list) or not resource["fields"]:
            return None, "parse_error"
        field_names = set()
        for field in resource["fields"]:
            if not isinstance(field, dict) or not isinstance(field.get("name"), str):
                return None, "parse_error"
            field_names.add(field["name"])
        # titleField is optional display metadata (older/pre-Phase-2 configs
        # omit it — the frontend falls back to "#{id}" when absent, see
        # miniapp/src/lib/displaySchema.ts), but if the model DID include it,
        # it must reference a field that's actually declared on this
        # resource — same "never invent" posture as table/column names above.
        title_field = resource.get("titleField")
        if title_field is not None and title_field not in field_names:
            return None, "parse_error"
    if not _validate_miniapp_config_against_code(data, bot_code):
        return None, "validation_failed"
    return data, None


async def _generate_miniapp_config(bot_code: str, requirements_summary: str) -> tuple[dict | None, dict | None]:
    """Never raises — any failure (API error, malformed/hallucinated output)
    returns (None, failure_info), which callers treat as "no mini-app for
    this bot", exactly like a template with no miniapp_config today. Must
    never block or delay bot creation on its own error.

    Returns (config, failure_info). failure_info is None on success —
    INCLUDING the valid "no mini-app needed" case (data.get("resources") ==
    []), which is not a failure — and otherwise
    {"reason": "timeout" | "api_error" | "parse_error" | "validation_failed"}
    once a single retry has also failed (see below). Callers with a bot_id
    in hand persist failure_info via db.database.add_miniapp_config_failure
    so the owner can spot bots that silently got no mini-app (mirrors
    fallback_info/template_candidates' "never block, but never lose the
    signal either" posture — docs/TEMPLATE_CANDIDATE_LOGGING_DESIGN.md).

    One retry (a single extra Haiku call, not a loop) is attempted after
    EITHER an API failure or a parse/validation failure before giving up —
    Haiku's hallucination/malformed-output rate on this task is low enough
    that a second try clears most transient misses without meaningfully
    increasing latency (this step already runs concurrently with the other
    post-generation config calls, see generate_bot_code)."""
    for attempt in range(2):
        reason: str
        try:
            response = await asyncio.wait_for(
                client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    # 1500 was sized for a bot with 1-2 simple resources; a
                    # multi-domain bot (e.g. tour_operator: tours/program/
                    # locations/hotels/guests, ~5-10 fields each) reliably
                    # exceeds it and gets cut off mid-JSON (stop_reason
                    # "max_tokens"), which _parse_miniapp_config then reports
                    # as a plain "parse_error" — indistinguishable from a
                    # genuine hallucination, and the one retry doesn't help
                    # since the same truncation happens again. 4096 covers
                    # tour_operator's 5-resource/~40-field schema with room
                    # to spare (verified: full response fits well under it).
                    max_tokens=4096,
                    system=MINIAPP_CONFIG_SYSTEM_PROMPT,
                    messages=[{
                        "role": "user",
                        "content": f"Bot code:\n\n{bot_code}\n\nBot description:\n{requirements_summary}",
                    }],
                ),
                timeout=60.0,
            )
        except asyncio.TimeoutError:
            logger.warning(f"_generate_miniapp_config: timed out (attempt {attempt + 1}/2)")
            reason = "timeout"
        except Exception as e:
            logger.warning(f"_generate_miniapp_config: API call failed (attempt {attempt + 1}/2): {type(e).__name__}: {e}")
            reason = "api_error"
        else:
            config, parse_reason = _parse_miniapp_config(response.content[0].text, bot_code)
            if parse_reason is None:
                return config, None  # success, including the valid empty-resources answer
            logger.warning(f"_generate_miniapp_config: {parse_reason} (attempt {attempt + 1}/2)")
            reason = parse_reason
        if attempt == 1:
            return None, {"reason": reason}
    return None, {"reason": "api_error"}  # unreachable, satisfies type checkers


# ── office-hook config generation (see docs/OFFICES_DESIGN.md §11) ─────────────
#
# One cheap Haiku call per bot, same cost tier and never-blocking posture as
# _generate_miniapp_config above — reuses the SAME ground-truth extraction
# (_extract_create_table_names) so a hallucinated table/column degrades to
# None exactly like a bad miniapp_config would. Produces a tiny declarative
# {"table": ..., "match_field": ...} describing which table holds this bot's
# own "client" records and which column on it is comparable to
# OrderCreatedEvent.customer_chat_id (a Telegram chat_id/user_id — see
# features/office_events.py's OrderCreatedEvent). Read at office-event
# delivery time by features/office_events.py's generic_on_office_event(),
# NOT baked into any generated .py file — this is data, not code, same
# reasoning as miniapp_config: no per-bot code injection, no AST/syntax risk,
# and a bad/missing config just means "generic office hook can't match
# clients for this bot", not a broken bot.

_EVENT_RSVP_OFFICE_HOOK_CONFIG_EXAMPLE = (
    """{"table": "rsvps", "match_field": "client_user_id", "created_at_field": "created_at"}"""
)

OFFICE_HOOK_CONFIG_SYSTEM_PROMPT = f"""You extract a tiny "client match" hint from a Telegram bot's Python source code, for a factory that builds Telegram bots which can be linked into "офисы" (cross-bot notifications) and for that same bot's own per-bot sales analytics.

You will be given the bot's full source code (its init_db() function contains the real CREATE TABLE statements — this is your only source of truth for table and column names) and a short description of what the bot does.

Context 1 (офисы): this factory lets the owner link two of their bots so that when a customer pays in bot A, bot B (if linked) gets notified with that customer's Telegram chat_id. For bot B to react usefully, it needs to know: which of ITS OWN tables holds records about its own customers/clients, and which column on that table is a Telegram chat_id/user_id comparable to the paying customer's chat_id (so bot B can look up "do I already have a record for this same person?").

Context 2 (per-bot analytics): the SAME table/match_field pair also drives a generic sales-analytics feature (record volume over time, top repeat customers) available to any bot's own owner inside that bot's mini-app. That feature additionally needs to know which column on the chosen table stores WHEN each record was created, so it can bucket records by day/week/month.

Here is one correct real example, from an event-RSVP bot (rsvps is a real table in that bot's init_db(), client_user_id is a real INTEGER column storing the guest's Telegram user id, created_at is a real TIMESTAMP column set at insert time):

{_EVENT_RSVP_OFFICE_HOOK_CONFIG_EXAMPLE}

Rules:
- Respond with ONLY the JSON object, no markdown fences, no explanation.
- Shape: {{"table": "<real table name from init_db(), verbatim>", "match_field": "<real column name on that table, verbatim>" or null, "created_at_field": "<real column name on that table, verbatim>" or null}}.
- Pick the table that best represents this bot's own individual customer/client/guest records (e.g. bookings, clients, guests, orders, subscribers) — NOT a purely internal/administrative table (admins, settings, state, sessions, migration tables).
- match_field must be a column that stores a Telegram user id or chat id (typically named like user_id, client_id, chat_id, telegram_id, customer_id — an INTEGER column that identifies a person via Telegram, not a phone number or free-text name). If no such column exists on the chosen table, or you cannot find any table worth matching against, set match_field to null (table may still be non-null — see below) — this is a valid, expected answer.
- created_at_field must be a column that stores when each row was created (typically named like created_at, created, timestamp, booked_at — a TIMESTAMP/TEXT/DATETIME column set once at insert time, not a status or an editable date the user picks). If no such column exists, set created_at_field to null — this is a valid, expected answer and does not affect table/match_field.
- If the bot has NO table that represents individual customer/client records at all (e.g. a purely broadcast/announcement bot with no such table, or only internal tables), respond with exactly {{"table": null, "match_field": null, "created_at_field": null}}. This is a valid, expected answer — not an error.
- Never invent a table or column that isn't a literal, verbatim CREATE TABLE name/column in the given code."""


def _parse_office_hook_config(raw: str, bot_code: str) -> dict | None:
    """Best-effort JSON parse + schema validation, mirroring
    _parse_miniapp_config's fallback posture: any malformed shape or
    hallucinated table/column degrades to None (never raises).

    created_at_field is validated the same way as match_field (must be a
    real, verbatim column on the chosen table, else dropped to None) but its
    presence/absence never invalidates table/match_field — see
    features/sales_analytics.py, which treats a missing created_at_field as
    "no time-bucketed chart for this bot", not as "no analytics at all"."""
    try:
        data = json.loads(_strip_code_fences(raw))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    table = data.get("table")
    match_field = data.get("match_field")
    created_at_field = data.get("created_at_field")
    if table is None:
        return None  # valid "no client table for this bot" answer, nothing to store
    if not isinstance(table, str):
        return None
    if match_field is not None and not isinstance(match_field, str):
        return None
    if created_at_field is not None and not isinstance(created_at_field, str):
        return None

    tables = _extract_create_table_names(bot_code)
    if table not in tables:
        return None
    if match_field is not None and match_field not in tables[table]:
        return None
    if created_at_field is not None and created_at_field not in tables[table]:
        created_at_field = None
    return {"table": table, "match_field": match_field, "created_at_field": created_at_field}


async def _generate_office_hook_config(bot_code: str, requirements_summary: str) -> dict | None:
    """Never raises — any failure (API error, malformed/hallucinated output)
    returns None, which callers treat as "no office-hook config for this
    bot" — the generic office-event handler then has nothing to match
    against and only records a plain fallback note (see
    features/office_events.py's generic_on_office_event()). Must never block
    or delay bot creation on its own error."""
    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system=OFFICE_HOOK_CONFIG_SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": f"Bot code:\n\n{bot_code}\n\nBot description:\n{requirements_summary}",
            }],
        )
    except Exception as e:
        logger.warning(f"_generate_office_hook_config: API call failed: {type(e).__name__}: {e}")
        return None
    return _parse_office_hook_config(response.content[0].text, bot_code)


# ── voice-intake / cashflow-ledger config generation (docs/
#    VOICE_CASHFLOW_FROM_SCRATCH_DESIGN.md) ──────────────────────────────────
#
# One cheap Haiku call per bot, same cost tier and never-blocking posture as
# _generate_miniapp_config/_generate_office_hook_config above — reuses the
# SAME ground-truth extraction (_extract_create_table_names) so a
# hallucinated table/column degrades the voice_intake portion to None exactly
# like a bad miniapp_config would. Unlike miniapp_config/office_hook_config,
# this ALSO classifies whether the bot needs these features at all (most
# bots don't) — a bot with neither concept just gets
# {"voice_intake": None, "cashflow_ledger": False}, the expected common case.
#
# voice_intake's declarative shape here matches features/voice_intake.py's
# register_data_driven_schema() input contract exactly (record_types with
# table/fields/column, optional context_table/context_column) — see that
# function's own docstring for the full shape. cashflow_ledger needs no
# schema of its own (features/cashflow_ledger.py's cashflow_entries table is
# fixed, not per-bot) — just a boolean.

_TOUR_OPERATOR_VOICE_CASHFLOW_CONFIG_EXAMPLE = """{
    "voice_intake": {
        "context_table": "user_prefs",
        "context_column": "active_tour_id",
        "record_types": [
            {
                "key": "location", "label": "ЛиП", "icon": "📍",
                "prompt_desc": "name, region, category, status, notes",
                "table": "locations",
                "context_column": "tour_id",
                "fields": [
                    {"name": "name", "label": "Название", "column": "name"},
                    {"name": "region", "label": "Регион", "column": "region"},
                    {"name": "category", "label": "Категория", "column": "category"},
                    {"name": "status", "label": "Статус", "column": "status"},
                    {"name": "notes", "label": "Заметки", "column": "notes"}
                ]
            }
        ]
    },
    "cashflow_ledger": true
}"""

VOICE_CASHFLOW_CONFIG_SYSTEM_PROMPT = f"""You extract a voice-intake schema and a cashflow-ledger flag from a Telegram bot's Python source code, for a factory that builds Telegram bots.

You will be given the bot's full source code (its init_db() function contains the real CREATE TABLE statements — this is your only source of truth for table and column names) and a short description of what the bot does.

Context: this factory has two optional reusable features. (1) "voice_intake" lets an admin send a voice message that gets transcribed and parsed into a structured record, saved into one of the bot's own tables — useful for bots where an admin manually enters records that a voice message could fill faster (e.g. a new booking, a new inventory item, a new expense entry). (2) "cashflow_ledger" is a simple money in/out ledger table — useful for bots that track business income/expenses (tour operators, accountants, expense trackers), NOT for bots that only handle a fixed product price or a single payment flow (that's a different, already-existing "payments" feature — do not confuse the two).

Here is one correct real example, from a tour-operator bot (locations and user_prefs are real tables in that bot's init_db(), tour_id/active_tour_id/name/region/category/status/notes are real columns):

{_TOUR_OPERATOR_VOICE_CASHFLOW_CONFIG_EXAMPLE}

Rules:
- Respond with ONLY the JSON object, no markdown fences, no explanation.
- Top-level shape: {{"voice_intake": {{...}} | null, "cashflow_ledger": true | false}}.
- Set "voice_intake" to null if this bot has no table worth voice-filling, or if manual data entry isn't really part of this bot's workflow (e.g. a purely conversational bot, or a bot whose only writes are payment records). This is a valid, expected, and common answer — most bots should get null here.
- When voice_intake is not null: "context_table"/"context_column" describe a table holding a "currently active X per admin" concept (e.g. tour_operator's active_tour_id in user_prefs) — set BOTH to null if the bot has no such concept (most bots won't; every record_type is then saved unconditionally, with no per-record-type "context_column" either).
- Each record_type: "key" (short identifier), "label" (human-readable, in the bot's own language), "icon" (one emoji), "prompt_desc" (one line listing this record type's fields, comma-separated, for a parsing prompt), "table" (MUST be a real table name from the given code's CREATE TABLE statements, verbatim), "context_column" (a real column on "table" linking each row to the context_table row — null if this record type has no such link, e.g. a top-level/standalone table), "fields" (list of {{"name": short field key used in parsed JSON, "label": human-readable label in the bot's language, "column": MUST be a real column name on "table", verbatim}}).
- Every "table" and every "column" MUST match a real table/column that appears in the given code's CREATE TABLE statements. Never invent a table or column that isn't there.
- Set "cashflow_ledger" to true only if the bot's own purpose clearly involves tracking money movement (income/expenses/cash flow) as an ongoing ledger, not a single price or payment. Most bots should get false here.
- If you cannot find any table worth voice-filling AND the bot has no cashflow-ledger need, respond with exactly {{"voice_intake": null, "cashflow_ledger": false}}. This is a valid, expected, and very common answer — not an error."""


def _validate_voice_cashflow_config(data: dict, bot_code: str) -> dict | None:
    """Best-effort schema + ground-truth validation, mirroring
    _parse_miniapp_config's fallback posture: any malformed shape or
    hallucinated table/column degrades voice_intake to None (never the whole
    response — cashflow_ledger is a plain bool with nothing to hallucinate
    against, so it's kept even if voice_intake fails validation)."""
    if not isinstance(data, dict):
        return None
    cashflow_ledger = data.get("cashflow_ledger")
    if not isinstance(cashflow_ledger, bool):
        cashflow_ledger = False
    voice_intake = data.get("voice_intake")
    if voice_intake is not None:
        from features.voice_intake import _validate_data_driven_config

        if not isinstance(voice_intake, dict):
            voice_intake = None
        else:
            tables = _extract_create_table_names(bot_code)
            if not tables or not _validate_data_driven_config(voice_intake, tables):
                voice_intake = None
    if voice_intake is None and not cashflow_ledger:
        return None  # valid "neither feature for this bot" answer, nothing to store
    return {"voice_intake": voice_intake, "cashflow_ledger": cashflow_ledger}


def _parse_voice_cashflow_config(raw: str, bot_code: str) -> dict | None:
    try:
        data = json.loads(_strip_code_fences(raw))
    except (json.JSONDecodeError, ValueError):
        return None
    return _validate_voice_cashflow_config(data, bot_code)


async def _generate_voice_cashflow_config(bot_code: str, requirements_summary: str) -> dict | None:
    """Never raises — any failure (API error, malformed/hallucinated output)
    returns None, which callers treat as "neither feature for this bot",
    exactly like a template with no voice_intake/cashflow_ledger today. Must
    never block or delay bot creation on its own error."""
    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1200,
            system=VOICE_CASHFLOW_CONFIG_SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": f"Bot code:\n\n{bot_code}\n\nBot description:\n{requirements_summary}",
            }],
        )
        raw = response.content[0].text
    except Exception as e:
        # Covers both the API call itself and an unexpected response shape
        # (e.g. an empty content list from a refusal or a max_tokens cutoff
        # with no content emitted yet) — response.content[0] must stay
        # inside this try, not after it, to keep this function's "never
        # raises" contract: generate_bot_code()'s asyncio.gather has no
        # return_exceptions=True, so an uncaught exception here would abort
        # the sibling miniapp_config/office_hook_config results too, not
        # just this one config.
        logger.warning(f"_generate_voice_cashflow_config: API call failed: {type(e).__name__}: {e}")
        return None
    return _parse_voice_cashflow_config(raw, bot_code)


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
