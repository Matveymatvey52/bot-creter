import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from config import DATA_DIR
from db.database import create_bot_record, update_bot_status
from services.bot_runner import start_bot
from services.claude_service import chat_gather_requirements, generate_bot_code

router = Router()
logger = logging.getLogger(__name__)

GENERATED_BOTS_DIR = DATA_DIR / "generated_bots"
GENERATED_BOTS_DIR.mkdir(exist_ok=True)


class CreateBotStates(StatesGroup):
    gathering = State()
    waiting_for_name = State()
    waiting_for_token = State()


@router.message(Command("create"))
async def cmd_create(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(CreateBotStates.gathering)
    await state.update_data(conversation=[])
    await message.answer(
        "Расскажите, какого бота хотите создать.\n"
        "Опишите его назначение и функции."
    )


@router.message(CreateBotStates.gathering)
async def handle_gathering(message: Message, state: FSMContext):
    data = await state.get_data()
    conversation: list[dict] = data.get("conversation", [])

    conversation.append({"role": "user", "content": message.text})
    await message.answer("Анализирую... ⏳")

    response = await chat_gather_requirements(conversation)
    conversation.append({"role": "assistant", "content": response})

    if "===READY_TO_GENERATE===" in response:
        parts = response.split("===READY_TO_GENERATE===")
        summary = parts[1].strip() if len(parts) > 1 else response

        await message.answer("Отлично! Генерирую код... 🔧")
        code = await generate_bot_code(summary)

        await state.update_data(conversation=conversation, bot_code=code, bot_summary=summary)
        await state.set_state(CreateBotStates.waiting_for_name)
        await message.answer(
            "Код готов! ✅\n\n"
            "Как назовём этого бота?\n"
            "(только латиница, цифры, подчёркивание — например: task_manager)"
        )
    else:
        await state.update_data(conversation=conversation)
        await message.answer(response)


@router.message(CreateBotStates.waiting_for_name)
async def handle_name(message: Message, state: FSMContext):
    raw = message.text.strip().replace(" ", "_")
    name = "".join(c for c in raw if c.isalnum() or c == "_")
    if not name:
        await message.answer("Пожалуйста, введите корректное имя (буквы, цифры, подчёркивание).")
        return

    await state.update_data(bot_name=name)
    await state.set_state(CreateBotStates.waiting_for_token)
    await message.answer(
        f"Имя: *{name}*\n\n"
        "Создайте бота через @BotFather и пришлите мне его токен.\n"
        "Токен выглядит так: `1234567890:ABCdef...`",
        parse_mode="Markdown",
    )


@router.message(CreateBotStates.waiting_for_token)
async def handle_token(message: Message, state: FSMContext):
    token = message.text.strip()
    if ":" not in token or len(token) < 30:
        await message.answer("Не похоже на токен Telegram. Попробуйте ещё раз.")
        return

    data = await state.get_data()
    bot_code: str = data["bot_code"]
    bot_name: str = data["bot_name"]
    bot_summary: str = data.get("bot_summary", "")

    bot_file = GENERATED_BOTS_DIR / f"{bot_name}.py"
    bot_file.write_text(bot_code, encoding="utf-8")

    bot_id = await create_bot_record(
        name=bot_name,
        description=bot_summary,
        token=token,
        file_path=str(bot_file),
    )

    try:
        pid = await start_bot(bot_id, str(bot_file), token)
        await update_bot_status(bot_id, "running", pid)
        await message.answer(
            f"Бот *{bot_name}* запущен! 🚀\n"
            f"ID: `{bot_id}`\n\n"
            "Используй /list чтобы посмотреть все боты.",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"Failed to start bot {bot_id}: {e}")
        await update_bot_status(bot_id, "error")
        await message.answer(
            f"Бот создан (ID: `{bot_id}`), но не смог запуститься:\n`{e}`\n\n"
            "Используй `/run {id}` чтобы попробовать снова.",
            parse_mode="Markdown",
        )

    await state.clear()
