from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import ASSEMBLYAI_API_KEY, DATA_DIR
from db.database import create_bot_record, update_bot_status
from services.bot_runner import start_bot
from services.claude_service import chat_gather_requirements, extract_bot_name, generate_bot_code
from services.voice_service import transcribe_voice

router = Router()
logger = logging.getLogger(__name__)

GENERATED_BOTS_DIR = DATA_DIR / "generated_bots"
GENERATED_BOTS_DIR.mkdir(exist_ok=True)


class CreateBotStates(StatesGroup):
    gathering = State()
    waiting_for_token = State()


@router.message(Command("create"))
async def cmd_create(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(CreateBotStates.gathering)
    await state.update_data(conversation=[])
    await message.answer(
        "Расскажите, какого бота хотите создать.\n"
        "Опишите его назначение и функции.\n\n"
        "Можно текстом или голосовым сообщением 🎤"
    )


async def _recognize_voice(message: Message, bot: Bot) -> str | None:
    if not ASSEMBLYAI_API_KEY:
        await message.answer(
            "⚠️ Распознавание голосовых не настроено. Напишите текстом, пожалуйста."
        )
        return None

    await message.answer("🎤 Распознаю голосовое...")

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        file = await bot.get_file(message.voice.file_id)
        await bot.download_file(file.file_path, destination=tmp_path)
        text = await transcribe_voice(tmp_path)
    except Exception as e:
        logger.error(f"Voice transcription failed: {e}")
        await message.answer("Не удалось распознать голосовое 😔 Попробуйте текстом.")
        return None
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    if not text.strip():
        await message.answer("Не удалось разобрать голосовое, попробуйте ещё раз.")
        return None

    await message.answer(f"🎤 Распознал: _{text}_", parse_mode="Markdown")
    return text


@router.message(CreateBotStates.gathering, F.voice)
async def handle_gathering_voice(message: Message, state: FSMContext, bot: Bot):
    text = await _recognize_voice(message, bot)
    if text is None:
        return
    await _process_gathering_text(message, state, text)


@router.message(CreateBotStates.gathering, F.text)
async def handle_gathering(message: Message, state: FSMContext):
    await _process_gathering_text(message, state, message.text)


async def _process_gathering_text(message: Message, state: FSMContext, text: str):
    data = await state.get_data()
    conversation: list[dict] = data.get("conversation", [])

    conversation.append({"role": "user", "content": text})
    await message.answer("Анализирую... ⏳")

    response = await chat_gather_requirements(conversation)
    conversation.append({"role": "assistant", "content": response})

    if "===READY_TO_GENERATE===" in response:
        parts = response.split("===READY_TO_GENERATE===")
        summary = parts[1].strip() if len(parts) > 1 else response

        await message.answer("Отлично! Генерирую код... 🔧")
        code, bot_name = await asyncio.gather(
            generate_bot_code(summary),
            extract_bot_name(summary),
        )

        await state.update_data(conversation=conversation, bot_code=code, bot_summary=summary, bot_name=bot_name)
        await state.set_state(CreateBotStates.waiting_for_token)

        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Открыть BotFather 🤖", url="https://t.me/BotFather?start=newbot")
        ]])
        await message.answer(
            f"Код готов! ✅\n\n"
            f"Предлагаемое имя бота: *{bot_name}_bot*\n\n"
            f"1️⃣ Нажми кнопку ниже — откроется BotFather\n"
            f"2️⃣ Отправь /newbot, введи имя и username\n"
            f"3️⃣ Скопируй токен и вставь сюда",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
    else:
        await state.update_data(conversation=conversation)
        await message.answer(response)



@router.message(CreateBotStates.waiting_for_token, F.voice)
async def handle_token_voice(message: Message):
    await message.answer(
        "⚠️ Токен лучше прислать текстом — скопируйте его из переписки с @BotFather."
    )


@router.message(CreateBotStates.waiting_for_token, F.text)
async def handle_token(message: Message, state: FSMContext, bot: Bot):
    token = message.text.strip()
    if ":" not in token or len(token) < 30:
        await message.answer("Не похоже на токен Telegram. Попробуйте ещё раз.")
        return

    data = await state.get_data()
    bot_code: str = data["bot_code"]
    bot_name: str = data["bot_name"]
    bot_summary: str = data.get("bot_summary", "")

    # Get real bot username from Telegram
    real_username: str | None = None
    try:
        temp_bot = Bot(token=token)
        bot_info = await temp_bot.get_me()
        real_username = bot_info.username
        await temp_bot.session.close()
    except Exception:
        pass

    bot_file = GENERATED_BOTS_DIR / f"{bot_name}.py"
    bot_file.write_text(bot_code, encoding="utf-8")

    bot_id = await create_bot_record(
        name=bot_name,
        description=bot_summary,
        token=token,
        file_path=str(bot_file),
        username=real_username,
    )

    username_display = f" (@{real_username})" if real_username else ""
    try:
        pid = await start_bot(bot_id, str(bot_file), token)
        await update_bot_status(bot_id, "running", pid)
        await message.answer(
            f"Бот *{bot_name}*{username_display} запущен! 🚀\n"
            f"ID: `{bot_id}`\n\n"
            "Используй /list чтобы посмотреть все боты.",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"Failed to start bot {bot_id}: {e}")
        await update_bot_status(bot_id, "error")
        short_err = str(e)[:400]
        await message.answer(
            f"Бот создан (ID: `{bot_id}`){username_display}, но упал при запуске:\n```\n{short_err}\n```\n\n"
            f"Запустить снова: `/run {bot_id}`\n"
            f"Логи: `/logs {bot_id}`",
            parse_mode="Markdown",
        )

    await state.clear()
