from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

router = Router()


@router.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "👋 Привет! Я Bot-Creator — создаю Telegram-ботов по твоему описанию.\n\n"
        "Команды:\n"
        "/create — создать нового бота\n"
        "/list — список созданных ботов\n"
        "/stop <id> — остановить бота\n"
        "/run <id> — запустить бота\n\n"
        "Начни с /create!"
    )
