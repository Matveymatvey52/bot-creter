from pathlib import Path

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import FSInputFile, Message

router = Router()

WELCOME_IMAGE = Path(__file__).resolve().parent.parent / "assets" / "welcome.png"


@router.message(Command("start"))
async def cmd_start(message: Message):
    caption = (
        "👋 Привет! Я Bot-Creator — создаю Telegram-ботов по твоему описанию.\n\n"
        "Команды:\n"
        "/create — создать нового бота\n"
        "/list — мои боты и управление ими\n\n"
        "Начни с /create! Можно текстом или голосовым 🎤"
    )
    await message.answer(caption)
