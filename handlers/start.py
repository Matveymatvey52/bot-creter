from pathlib import Path

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

router = Router()

WELCOME_IMAGE = Path(__file__).resolve().parent.parent / "assets" / "welcome.png"

_WELCOME_CAPTION = (
    "👋 Привет! Я Bot-Creator — создаю Telegram-ботов по твоему описанию.\n\n"
    "Опиши, какого бота хочешь — текстом или голосовым 🎤 или перейди к своим уже созданным ботам."
)


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
    if WELCOME_IMAGE.exists():
        await message.answer_photo(FSInputFile(WELCOME_IMAGE), caption=_WELCOME_CAPTION, reply_markup=_start_keyboard())
    else:
        await message.answer(_WELCOME_CAPTION, reply_markup=_start_keyboard())


@router.callback_query(F.data == "start_menu")
async def cb_start_menu(callback: CallbackQuery):
    # Same content as /start — this is Баг 3's "◀ В главное меню" target,
    # reuses _WELCOME_CAPTION/_start_keyboard rather than duplicating the text.
    await callback.answer()
    if WELCOME_IMAGE.exists():
        await callback.message.answer_photo(FSInputFile(WELCOME_IMAGE), caption=_WELCOME_CAPTION, reply_markup=_start_keyboard())
    else:
        await callback.message.answer(_WELCOME_CAPTION, reply_markup=_start_keyboard())
