from pathlib import Path

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

router = Router()

WELCOME_IMAGE = Path(__file__).resolve().parent.parent / "assets" / "welcome.png"


def _start_keyboard() -> InlineKeyboardMarkup:
    # "list" callback_data matches handlers/manage_bots.py's existing cb_list
    # handler exactly — reused as-is, not duplicated. "start_create"
    # matches handlers/create_bot.py's cb_start_create (added alongside this).
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать бота", callback_data="start_create")],
        [InlineKeyboardButton(text="📋 Мои боты", callback_data="list")],
    ])


@router.message(Command("start"))
async def cmd_start(message: Message):
    caption = (
        "👋 Привет! Я Bot-Creator — создаю Telegram-ботов по твоему описанию.\n\n"
        "Команды:\n"
        "/create — создать нового бота\n"
        "/list — мои боты и управление ими\n\n"
        "Начни с /create! Можно текстом или голосовым 🎤"
    )
    if WELCOME_IMAGE.exists():
        await message.answer_photo(FSInputFile(WELCOME_IMAGE), caption=caption, reply_markup=_start_keyboard())
    else:
        await message.answer(caption, reply_markup=_start_keyboard())
