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


async def _review_bot_code(code: str, requirements: str) -> str:
    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=32000,
        system=REVIEW_SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": f"Bot requirements (for context):\n{requirements}\n\nGenerated code to review:\n{code}",
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
        max_tokens=32000,
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
            max_tokens=32000,
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
            max_tokens=32000,
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


async def ask_assistant(user_message: str, bots_summary: str = "") -> str:
    system = ASSISTANT_SYSTEM_PROMPT
    if bots_summary:
        system += f"\n\nБоты пользователя:\n{bots_summary}"
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1200,
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
    user_msg = f"Create a Telegram bot with these requirements:\n\n{requirements_summary}"
    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=32000,
        system=GENERATE_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    code = _strip_code_fences(response.content[0].text)

    # Validate syntax; if broken ask Claude to fix it once
    try:
        _ast.parse(code)
    except SyntaxError as e:
        fix_response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=32000,
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
            max_tokens=32000,
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
    code = await _review_bot_code(code, requirements_summary)

    return code
