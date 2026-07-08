from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path
from urllib.parse import quote_plus

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import ASSEMBLYAI_API_KEY, BOT_TOKEN, DATA_DIR
from db.database import create_bot_record, update_bot_status
from services.bot_runner import start_bot
from services.claude_service import chat_gather_requirements, extract_bot_name, generate_bot_code
from services.telegram_api import get_managed_bot_token
from services.voice_service import transcribe_voice

router = Router()
logger = logging.getLogger(__name__)

GENERATED_BOTS_DIR = DATA_DIR / "generated_bots"
GENERATED_BOTS_DIR.mkdir(exist_ok=True)

# Set at startup via set_manager_username()
_manager_username: str = ""
_bot_id: int = 0

# user_id -> pending bot creation data (populated before user clicks the link)
_pending: dict[int, dict] = {}


def set_manager_username(username: str) -> None:
    global _manager_username
    _manager_username = username


def set_bot_id(bid: int) -> None:
    global _bot_id
    _bot_id = bid


async def _clear_user_fsm(storage, user_id: int) -> None:
    if not storage or not _bot_id or not user_id:
        return
    try:
        from aiogram.fsm.storage.base import StorageKey
        key = StorageKey(bot_id=_bot_id, chat_id=user_id, user_id=user_id)
        await storage.set_state(key=key, state=None)
        await storage.set_data(key=key, data={})
    except Exception as e:
        logger.warning(f"Could not clear FSM state for user {user_id}: {e}")


class CreateBotStates(StatesGroup):
    gathering = State()
    waiting_for_token = State()


@router.message(Command("cancel"), StateFilter("*"))
async def cmd_cancel(message: Message, state: FSMContext):
    current = await state.get_state()
    if current is None:
        await message.answer("Нечего отменять.")
        return
    _pending.pop(message.from_user.id, None)
    await state.clear()
    await message.answer("Отменено. Начни заново с /create")


@router.message(Command("create"))
async def cmd_create(message: Message, state: FSMContext):
    _pending.pop(message.from_user.id, None)
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


@router.message(CreateBotStates.gathering, F.text, ~F.text.startswith("/"))
async def handle_gathering(message: Message, state: FSMContext):
    await _process_gathering_text(message, state, message.text)


async def _process_gathering_text(message: Message, state: FSMContext, text: str):
    data = await state.get_data()
    conversation: list[dict] = data.get("conversation", [])

    conversation.append({"role": "user", "content": text})
    analyzing_msg = await message.answer("Анализирую... ⏳")

    response = await chat_gather_requirements(conversation)
    conversation.append({"role": "assistant", "content": response})

    try:
        await analyzing_msg.delete()
    except Exception:
        pass

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

        # Store pending so managed_bot update handler can find it by creator user_id
        _pending[message.from_user.id] = {
            "chat_id": message.chat.id,
            "code": code,
            "name": bot_name,
            "summary": summary,
        }

        # Build managed-bot deep link — user lands in BotFather with name/username pre-filled
        suggested_username = f"{bot_name}Bot"
        display_name = bot_name.replace("_", " ").title()
        if _manager_username:
            url = (
                f"https://t.me/newbot/{_manager_username}/"
                f"{suggested_username}?name={quote_plus(display_name)}"
            )
            button_text = "Создать бота ✨"
            instructions = (
                f"Код готов! ✅\n\n"
                f"Предлагаемый username: *@{suggested_username}*\n\n"
                f"1️⃣ Нажми кнопку ниже\n"
                f"2️⃣ Проверь имя и username в BotFather (можно изменить)\n"
                f"3️⃣ Нажми «Создать» — бот запустится автоматически!"
            )
        else:
            url = "https://t.me/BotFather?start=newbot"
            button_text = "Открыть BotFather 🤖"
            instructions = (
                f"Код готов! ✅\n\n"
                f"Предлагаемое имя: *{bot_name}_bot*\n\n"
                f"1️⃣ Нажми кнопку → BotFather\n"
                f"2️⃣ Отправь /newbot, введи имя и username\n"
                f"3️⃣ Скопируй токен и вставь сюда"
            )

        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=button_text, url=url)
        ]])
        await message.answer(instructions, parse_mode="Markdown", reply_markup=keyboard)
    else:
        await state.update_data(conversation=conversation)
        await message.answer(response)


async def auto_launch_managed_bot(managed_data: dict, bot: Bot, storage=None) -> None:
    """Called from main.py middleware when a managed_bot update arrives."""
    logger.warning(f"managed_bot RAW data: {managed_data}")

    # Try flat fields first, then nested objects
    new_bot_info = managed_data.get("bot") or managed_data.get("new_bot") or {}
    creator_info = managed_data.get("user") or managed_data.get("creator") or {}

    new_bot_id: int | None = (
        managed_data.get("bot_id")
        or new_bot_info.get("id")
        or new_bot_info.get("user_id")
    )
    creator_user_id: int | None = (
        managed_data.get("user_id")
        or managed_data.get("creator_id")
        or creator_info.get("id")
    )

    if not new_bot_id or not creator_user_id:
        logger.warning(f"managed_bot missing bot/user IDs — full data: {managed_data}")
        return

    pending = _pending.pop(creator_user_id, None)
    if not pending:
        logger.info(f"No pending creation for user {creator_user_id}")
        return

    chat_id = pending["chat_id"]

    # Get the new bot's token
    try:
        token = await get_managed_bot_token(BOT_TOKEN, new_bot_id)
    except Exception as e:
        logger.error(f"getManagedBotToken failed: {e}")
        await bot.send_message(
            chat_id,
            "Бот создан в BotFather, но не удалось получить токен автоматически 😔\n\n"
            "Скопируй токен из BotFather и отправь его сюда вручную.",
        )
        return

    # Token received — clear FSM state so user isn't stuck in waiting_for_token
    await _clear_user_fsm(storage, creator_user_id)

    # Fetch real username
    real_username: str | None = None
    try:
        async with Bot(token=token) as temp_bot:
            info = await temp_bot.get_me()
            real_username = info.username
    except Exception:
        pass

    bot_name: str = pending["name"]
    bot_code: str = pending["code"]
    bot_summary: str = pending["summary"]

    bot_file = GENERATED_BOTS_DIR / f"{bot_name}.py"
    bot_file.write_text(bot_code, encoding="utf-8")

    bot_record_id = await create_bot_record(
        name=bot_name,
        description=bot_summary,
        token=token,
        file_path=str(bot_file),
        username=real_username,
    )

    username_display = f" (@{real_username})" if real_username else ""
    try:
        pid = await start_bot(bot_record_id, str(bot_file), token)
        await update_bot_status(bot_record_id, "running", pid)
        await bot.send_message(
            chat_id,
            f"Бот *{bot_name}*{username_display} создан и запущен! 🚀\n\n"
            "Используй /list чтобы управлять ботом.",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"Failed to start managed bot {bot_record_id}: {e}")
        await update_bot_status(bot_record_id, "error")
        await bot.send_message(
            chat_id,
            f"Бот *{bot_name}*{username_display} создан, но не смог запуститься 😔\n\n"
            "Скорее всего ошибка в сгенерированном коде. Попробуй удалить и создать заново через /create.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🗑 Удалить бота", callback_data=f"delete:{bot_record_id}")
            ]]),
        )


@router.message(CreateBotStates.waiting_for_token, F.voice)
async def handle_token_voice(message: Message):
    await message.answer(
        "⚠️ Токен лучше прислать текстом — скопируйте его из переписки с @BotFather."
    )


@router.message(CreateBotStates.waiting_for_token, F.text, ~F.text.startswith("/"))
async def handle_token(message: Message, state: FSMContext, bot: Bot):
    token = message.text.strip()
    if ":" not in token or len(token) < 30:
        await message.answer("Не похоже на токен Telegram. Попробуйте ещё раз.")
        return

    _pending.pop(message.from_user.id, None)

    data = await state.get_data()
    bot_code: str = data["bot_code"]
    bot_name: str = data["bot_name"]
    bot_summary: str = data.get("bot_summary", "")

    real_username: str | None = None
    try:
        async with Bot(token=token) as temp_bot:
            bot_info = await temp_bot.get_me()
            real_username = bot_info.username
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
            f"Бот *{bot_name}*{username_display} запущен! 🚀\n\n"
            "Используй /list чтобы управлять ботом.",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"Failed to start bot {bot_id}: {e}")
        await update_bot_status(bot_id, "error")
        await message.answer(
            f"Бот *{bot_name}*{username_display} создан, но не смог запуститься 😔\n\n"
            "Скорее всего ошибка в сгенерированном коде. Попробуй удалить и создать заново.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🗑 Удалить бота", callback_data=f"delete:{bot_id}")
            ]]),
        )

    await state.clear()
