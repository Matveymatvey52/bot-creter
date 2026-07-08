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
- Include basic error handling
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
- DB_PATH = os.path.join(os.getenv("DATA_DIR", "./data"), "bot_data.db")
- Always use CREATE TABLE IF NOT EXISTS (never DROP, never DELETE all rows)
- This ensures data survives bot restarts and code updates
- Every user record, appointment, entry must be stored in SQLite, never in memory dicts

CRITICAL — correct aiogram 3.x imports only:
  from aiogram import Bot, Dispatcher, F, Router
  from aiogram.filters import Command, CommandStart
  from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
  from aiogram.fsm.context import FSMContext
  from aiogram.fsm.state import State, StatesGroup
  from aiogram.fsm.storage.memory import MemoryStorage
  import aiosqlite, asyncio, logging, os

FORBIDDEN — these do NOT exist in aiogram 3, never use them:
  - ChatType (use F.chat.type == "private" instead)
  - Text filter (use F.text or F.text.startswith(...) instead)
  - from aiogram.dispatcher.filters import anything
  - from aiogram.contrib import anything

Correct main entry point:
  async def main():
      bot = Bot(token=os.getenv("BOT_TOKEN"))
      dp = Dispatcher(storage=MemoryStorage())
      dp.include_router(router)
      await dp.start_polling(bot)

  if __name__ == "__main__":
      asyncio.run(main())

Return ONLY valid Python code. No markdown fences. No explanations."""


async def chat_gather_requirements(conversation: list[dict]) -> str:
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
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
        max_tokens=8192,
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
            max_tokens=8192,
            system=GENERATE_SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": user_msg},
                {"role": "assistant", "content": code},
                {"role": "user", "content": f"SyntaxError on line {e.lineno}: {e.msg}. Return ONLY corrected Python code, no markdown."},
            ],
        )
        code = _strip_code_fences(fix_response.content[0].text)
        _ast.parse(code)  # raises if still broken — caught upstream

    return code
