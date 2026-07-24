from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path
from urllib.parse import quote_plus

import aiohttp
from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import ASSEMBLYAI_API_KEY, BOT_TOKEN, DATA_DIR
from db.database import create_bot_record_with_admins, get_bot, set_bot_display_name, update_bot_status
from services.bot_runner import start_bot
from services.claude_service import chat_gather_requirements, extract_bot_name, generate_bot_code, generate_bot_guide
from services.github_sync import push_bot_to_github
from services.telegram_api import get_managed_bot_token
from services.voice_service import transcribe_voice

router = Router()
logger = logging.getLogger(__name__)

GENERATED_BOTS_DIR = DATA_DIR / "generated_bots"
GENERATED_BOTS_DIR.mkdir(exist_ok=True)

BOT_IMAGES_DIR = DATA_DIR / "bot_images"
BOT_IMAGES_DIR.mkdir(exist_ok=True)

AVATAR_DIR = DATA_DIR / "bot_avatars"
AVATAR_DIR.mkdir(exist_ok=True)

_manager_username: str = ""
_bot_id: int = 0

# user_id -> pending bot creation data
_pending: dict[int, dict] = {}


def set_manager_username(username: str) -> None:
    global _manager_username
    _manager_username = username


def set_bot_id(bid: int) -> None:
    global _bot_id
    _bot_id = bid


# The live webhook Registry, set once by runtime/combined_app.py's bootstrap —
# only present when this router is running inside the combined process (Stage
# 2's "фабрика как житель реестра"). Still None when running under main.py's
# separate long-polling process, since no Registry exists there — new bots
# then rely purely on services.bot_runner.start_bot() (subprocess model,
# unchanged) to actually respond, exactly as they do today.
_registry = None


def set_registry(registry) -> None:
    global _registry
    _registry = registry


async def _register_new_bot_in_registry(bot_id: int, bot_name: str) -> None:
    """Best-effort: registers a freshly-created bot into the live registry
    (direct in-process call — see runtime/registry.py's Registry.add_or_replace,
    untouched by this phase) so it can answer webhook traffic immediately,
    without waiting for a manual /admin/reload/{id}. Never raises — a failure
    here must not abort bot creation, since services.bot_runner.start_bot()
    (subprocess model) is what actually makes the bot respond today regardless
    of registry state; a bot present in the DB but missing from the registry
    is recoverable later via a manual reload, not a data-loss scenario."""
    if _registry is None:
        logger.debug(
            f"No live registry available (polling-only process) — bot id={bot_id} "
            f"({bot_name}) not registered; use /admin/reload/{bot_id} once the "
            "combined app is running, if that ever applies."
        )
        return
    try:
        fresh_row = await get_bot(bot_id)
        if fresh_row is None:
            logger.error(f"Registry registration skipped for bot id={bot_id} ({bot_name}) — row vanished after creation")
            return
        entry = await _registry.add_or_replace(fresh_row)
        if entry is None:
            logger.warning(
                f"Bot id={bot_id} ({bot_name}) created but registry registration failed — "
                f"it will only answer webhook traffic after a manual /admin/reload/{bot_id}"
            )
        else:
            logger.info(f"Bot id={bot_id} ({bot_name}) registered in the live registry")
    except Exception as e:
        logger.error(f"Registry registration raised for bot id={bot_id} ({bot_name}): {e}")


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


async def _set_bot_profile_photo(token: str, photo_path: str) -> None:
    try:
        async with aiohttp.ClientSession() as session:
            with open(photo_path, "rb") as f:
                form = aiohttp.FormData()
                form.add_field("photo", f, filename="avatar.jpg", content_type="image/jpeg")
                async with session.post(
                    f"https://api.telegram.org/bot{token}/setMyProfilePhoto",
                    data=form,
                ) as resp:
                    result = await resp.json()
                    if not result.get("ok"):
                        logger.warning(f"setMyProfilePhoto failed: {result}")
    except Exception as e:
        logger.warning(f"Could not set profile photo: {e}")


class CreateBotStates(StatesGroup):
    gathering = State()
    waiting_for_display_name = State()
    waiting_for_welcome_photo = State()
    waiting_for_avatar_photo = State()
    waiting_for_token = State()


# ── /cancel ───────────────────────────────────────────────────────────────────

@router.message(Command("cancel"), StateFilter("*"))
async def cmd_cancel(message: Message, state: FSMContext):
    if await state.get_state() is None:
        await message.answer("Нечего отменять.")
        return
    _pending.pop(message.from_user.id, None)
    await state.clear()
    await message.answer("Отменено. Начни заново с /create")


# ── /create ───────────────────────────────────────────────────────────────────

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


# ── gathering ─────────────────────────────────────────────────────────────────

async def _recognize_voice(message: Message, bot: Bot) -> str | None:
    if not ASSEMBLYAI_API_KEY:
        await message.answer("⚠️ Распознавание голосовых не настроено. Напишите текстом.")
        return None
    status_msg = await message.answer("🎤 Распознаю голосовое...")
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        file = await bot.get_file(message.voice.file_id)
        await bot.download_file(file.file_path, destination=tmp_path)
        text = await transcribe_voice(tmp_path)
    except Exception as e:
        logger.error(f"Voice transcription failed: {e}")
        try:
            await status_msg.delete()
        except Exception:
            pass
        await message.answer("Не удалось распознать голосовое 😔 Попробуйте текстом.")
        return None
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    try:
        await status_msg.delete()
    except Exception:
        pass
    if not text.strip():
        await message.answer("Не удалось разобрать голосовое, попробуйте ещё раз.")
        return None
    await message.answer(f"🎤 Распознал: _{text}_", parse_mode="Markdown")
    return text


@router.message(CreateBotStates.gathering, F.voice)
async def handle_gathering_voice(message: Message, state: FSMContext, bot: Bot):
    text = await _recognize_voice(message, bot)
    if text:
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
        try:
            bot_name = await extract_bot_name(summary)
        except Exception:
            bot_name = "my_bot"

        await state.update_data(
            conversation=conversation,
            bot_summary=summary,
            bot_name=bot_name,
        )
        await state.set_state(CreateBotStates.waiting_for_display_name)
        await message.answer(
            "Отлично! Осталось пару вопросов.\n\n"
            "👤 *Как будут звать этого бота?*\n"
            "Например: Макс, Катя, Алекс — это имя для общения в групповом чате.\n\n"
            "Напишите имя или /skip чтобы пропустить.",
            parse_mode="Markdown",
        )
    else:
        await state.update_data(conversation=conversation)
        await message.answer(response)


# ── waiting_for_display_name ──────────────────────────────────────────────────

@router.message(CreateBotStates.waiting_for_display_name, F.text, ~F.text.startswith("/"))
async def handle_display_name(message: Message, state: FSMContext):
    await state.update_data(display_name=message.text.strip())
    await _ask_welcome_photo(message, state)


@router.message(CreateBotStates.waiting_for_display_name, Command("skip"))
async def handle_display_name_skip(message: Message, state: FSMContext):
    await state.update_data(display_name="")
    await _ask_welcome_photo(message, state)


async def _ask_welcome_photo(message: Message, state: FSMContext):
    await state.set_state(CreateBotStates.waiting_for_welcome_photo)
    await message.answer(
        "📸 *Приветственное фото для бота*\n"
        "Эта картинка будет показываться пользователям при /start.\n\n"
        "Отправьте фото или /skip чтобы пропустить.",
        parse_mode="Markdown",
    )


# ── waiting_for_welcome_photo ─────────────────────────────────────────────────

@router.message(CreateBotStates.waiting_for_welcome_photo, F.photo)
async def handle_welcome_photo(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    bot_name = data.get("bot_name", "bot")
    BOT_IMAGES_DIR.mkdir(exist_ok=True)
    path = BOT_IMAGES_DIR / f"{bot_name}.jpg"
    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    await bot.download_file(file.file_path, destination=str(path))
    await state.set_state(CreateBotStates.waiting_for_avatar_photo)
    await message.answer(
        "✅ Фото сохранено!\n\n"
        "🖼 *Аватарка бота* (кружок рядом с именем)\n"
        "Отправьте фото или /skip (можно изменить позже через BotFather).",
        parse_mode="Markdown",
    )


@router.message(CreateBotStates.waiting_for_welcome_photo, Command("skip"))
async def handle_welcome_photo_skip(message: Message, state: FSMContext):
    await state.set_state(CreateBotStates.waiting_for_avatar_photo)
    await message.answer(
        "🖼 *Аватарка бота* (кружок рядом с именем)\n"
        "Отправьте фото или /skip (можно изменить позже через BotFather).",
        parse_mode="Markdown",
    )


@router.message(CreateBotStates.waiting_for_welcome_photo, F.text, ~F.text.startswith("/"))
async def handle_welcome_photo_invalid(message: Message):
    await message.answer("Отправьте фото или напишите /skip")


# ── waiting_for_avatar_photo ──────────────────────────────────────────────────

@router.message(CreateBotStates.waiting_for_avatar_photo, F.photo)
async def handle_avatar_photo(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    bot_name = data.get("bot_name", "bot")
    AVATAR_DIR.mkdir(exist_ok=True)
    path = AVATAR_DIR / f"{bot_name}.jpg"
    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    await bot.download_file(file.file_path, destination=str(path))
    await _generate_and_show_button(message, state)


@router.message(CreateBotStates.waiting_for_avatar_photo, Command("skip"))
async def handle_avatar_skip(message: Message, state: FSMContext):
    await _generate_and_show_button(message, state)


@router.message(CreateBotStates.waiting_for_avatar_photo, F.text, ~F.text.startswith("/"))
async def handle_avatar_invalid(message: Message):
    await message.answer("Отправьте фото или напишите /skip")


async def _generate_and_show_button(message: Message, state: FSMContext) -> None:
    await _run_generation(
        chat_id=message.chat.id,
        user_id=message.from_user.id,
        bot=message.bot,
        state=state,
    )


async def _run_generation(chat_id: int, user_id: int, bot: Bot, state: FSMContext) -> None:
    data = await state.get_data()
    summary: str = data.get("bot_summary", "")
    bot_name: str = data.get("bot_name", "my_bot")

    if not summary:
        await bot.send_message(
            chat_id,
            "⚠️ Данные о боте потеряны (бот мог перезапуститься).\n\nПожалуйста, начните заново с /create.",
        )
        await state.clear()
        return

    gen_msg = await bot.send_message(chat_id, "Генерирую код... 🔧")
    try:
        code = await asyncio.wait_for(generate_bot_code(summary), timeout=360.0)
    except asyncio.TimeoutError:
        logger.error("Code generation timed out after 240s")
        try:
            await gen_msg.delete()
        except Exception:
            pass
        await bot.send_message(
            chat_id,
            "⏱ Генерация заняла слишком много времени (>6 мин). Попробуй ещё раз — обычно со второго раза работает быстрее.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🔄 Попробовать снова", callback_data="retry_generate")
            ]]),
        )
        return
    except Exception as e:
        logger.error(f"Code generation failed: {type(e).__name__}: {e}")
        try:
            await gen_msg.delete()
        except Exception:
            pass
        await bot.send_message(
            chat_id,
            f"⚠️ Не удалось сгенерировать код ({type(e).__name__}).\n\n"
            "Нажми кнопку чтобы попробовать снова.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🔄 Попробовать снова", callback_data="retry_generate")
            ]]),
        )
        return

    try:
        await gen_msg.delete()
    except Exception:
        pass

    await state.update_data(bot_code=code)
    await state.set_state(CreateBotStates.waiting_for_token)

    _pending[user_id] = {
        "chat_id": chat_id,
        "code": code,
        "name": bot_name,
        "summary": summary,
        "display_name": data.get("display_name", ""),
    }

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

    await bot.send_message(
        chat_id,
        instructions,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=button_text, url=url)
        ]]),
    )


@router.callback_query(F.data == "retry_generate")
async def cb_retry_generate(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await _run_generation(
        chat_id=callback.message.chat.id,
        user_id=callback.from_user.id,
        bot=callback.bot,
        state=state,
    )


# ── managed bot auto-launch ───────────────────────────────────────────────────

async def auto_launch_managed_bot(managed_data: dict, bot: Bot, storage=None) -> None:
    logger.debug(f"managed_bot RAW data: {managed_data}")

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

    await _clear_user_fsm(storage, creator_user_id)

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
    display_name: str = pending.get("display_name", "")

    avatar_path = AVATAR_DIR / f"{bot_name}.jpg"
    if avatar_path.exists():
        await _set_bot_profile_photo(token, str(avatar_path))
        avatar_path.unlink(missing_ok=True)

    bot_file = GENERATED_BOTS_DIR / f"{bot_name}.py"
    bot_file.write_text(bot_code, encoding="utf-8")
    asyncio.create_task(push_bot_to_github(bot_name, bot_code))

    _owner_id = os.getenv("OWNER_ID", "")
    admin_ids = [str(creator_user_id)]
    if _owner_id and _owner_id != str(creator_user_id):
        admin_ids.append(_owner_id)

    bot_record_id = await create_bot_record_with_admins(
        name=bot_name,
        description=bot_summary,
        token=token,
        file_path=str(bot_file),
        admin_ids=admin_ids,
        username=real_username,
    )

    # The welcome photo was saved during onboarding under the bot's NAME
    # (handle_welcome_photo, before this bot's row/id existed). All five
    # templates now look for it under bot_images/bot_<id>.jpg (Stage 2
    # "изоляция по bots.id") — move it into place now that the id exists.
    _welcome_photo_by_name = BOT_IMAGES_DIR / f"{bot_name}.jpg"
    if _welcome_photo_by_name.exists():
        _welcome_photo_by_name.rename(BOT_IMAGES_DIR / f"bot_{bot_record_id}.jpg")

    if display_name:
        await set_bot_display_name(bot_record_id, display_name)

    await _register_new_bot_in_registry(bot_record_id, bot_name)

    username_display = f" (@{real_username})" if real_username else ""
    extra_env = {}
    if display_name:
        extra_env["BOT_DISPLAY_NAME"] = display_name
    try:
        pid = await start_bot(bot_record_id, str(bot_file), token, extra_env=extra_env or None)
        await update_bot_status(bot_record_id, "running", pid)
        try:
            guide = await generate_bot_guide(bot_name, bot_summary)
        except Exception:
            guide = ""
        admin_block = (
            "<b>👥 Управление администраторами</b>\n"
            "Команды пишутся прямо в созданный бот:\n"
            "<code>/admins</code> — список администраторов\n"
            "<code>/addadmin 123456789</code> — добавить администратора\n"
            "<code>/removeadmin 123456789</code> — убрать администратора\n\n"
            "💡 Узнать Telegram ID: попроси человека написать боту @userinfobot\n\n"
            "Управление ботом: /list"
        )
        text = f"✅ Бот <b>{bot_name}</b>{username_display} создан и запущен!\n\nВы являетесь администратором этого бота.\n\n"
        if guide:
            text += guide + "\n\n"
        text += admin_block
        await bot.send_message(chat_id, text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Failed to start managed bot {bot_record_id}: {e}")
        await update_bot_status(bot_record_id, "error")
        await bot.send_message(
            chat_id,
            f"Бот *{bot_name}*{username_display} создан, но не смог запуститься 😔\n\n"
            "Попробуй удалить и создать заново через /create.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🗑 Удалить бота", callback_data=f"delete:{bot_record_id}")
            ]]),
        )


# ── manual token entry (fallback) ─────────────────────────────────────────────

@router.message(CreateBotStates.waiting_for_token, F.voice)
async def handle_token_voice(message: Message):
    await message.answer("⚠️ Токен лучше прислать текстом — скопируйте его из @BotFather.")


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
    display_name: str = data.get("display_name", "")

    real_username: str | None = None
    try:
        async with Bot(token=token) as temp_bot:
            real_username = (await temp_bot.get_me()).username
    except Exception:
        pass

    avatar_path = AVATAR_DIR / f"{bot_name}.jpg"
    if avatar_path.exists():
        await _set_bot_profile_photo(token, str(avatar_path))
        avatar_path.unlink(missing_ok=True)

    bot_file = GENERATED_BOTS_DIR / f"{bot_name}.py"
    bot_file.write_text(bot_code, encoding="utf-8")
    asyncio.create_task(push_bot_to_github(bot_name, bot_code))

    _owner_id = os.getenv("OWNER_ID", "")
    admin_ids = [str(message.from_user.id)]
    if _owner_id and _owner_id != str(message.from_user.id):
        admin_ids.append(_owner_id)

    bot_id = await create_bot_record_with_admins(
        name=bot_name,
        description=bot_summary,
        token=token,
        file_path=str(bot_file),
        admin_ids=admin_ids,
        username=real_username,
    )

    # See the equivalent comment in _run_generation() above — the welcome
    # photo was saved under the bot's name before its row/id existed; all
    # five templates now look for bot_images/bot_<id>.jpg.
    _welcome_photo_by_name = BOT_IMAGES_DIR / f"{bot_name}.jpg"
    if _welcome_photo_by_name.exists():
        _welcome_photo_by_name.rename(BOT_IMAGES_DIR / f"bot_{bot_id}.jpg")

    if display_name:
        await set_bot_display_name(bot_id, display_name)

    await _register_new_bot_in_registry(bot_id, bot_name)

    username_display = f" (@{real_username})" if real_username else ""
    extra_env = {}
    if display_name:
        extra_env["BOT_DISPLAY_NAME"] = display_name
    try:
        pid = await start_bot(bot_id, str(bot_file), token, extra_env=extra_env or None)
        await update_bot_status(bot_id, "running", pid)
        try:
            guide = await generate_bot_guide(bot_name, bot_summary)
        except Exception:
            guide = ""
        admin_block = (
            "<b>👥 Управление администраторами</b>\n"
            "Команды пишутся прямо в созданный бот:\n"
            "<code>/admins</code> — список администраторов\n"
            "<code>/addadmin 123456789</code> — добавить администратора\n"
            "<code>/removeadmin 123456789</code> — убрать администратора\n\n"
            "💡 Узнать Telegram ID: попроси человека написать боту @userinfobot\n\n"
            "Управление ботом: /list"
        )
        text = f"✅ Бот <b>{bot_name}</b>{username_display} создан и запущен!\n\nВы являетесь администратором этого бота.\n\n"
        if guide:
            text += guide + "\n\n"
        text += admin_block
        await message.answer(text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Failed to start bot {bot_id}: {e}")
        await update_bot_status(bot_id, "error")
        await message.answer(
            f"Бот *{bot_name}*{username_display} создан, но не смог запуститься 😔\n\n"
            "Попробуй удалить и создать заново.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🗑 Удалить бота", callback_data=f"delete:{bot_id}")
            ]]),
        )

    await state.clear()
