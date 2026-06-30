from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from db.database import get_all_bots, get_bot, update_bot_status
from services.bot_runner import is_running, start_bot, stop_bot

router = Router()


@router.message(Command("list"))
async def cmd_list(message: Message):
    bots = await get_all_bots()
    if not bots:
        await message.answer("Ботов пока нет. Создай первого командой /create")
        return

    lines = ["📋 *Список ботов:*\n"]
    for b in bots:
        status = "🟢 работает" if is_running(b["id"]) else "🔴 остановлен"
        lines.append(f"*{b['name']}* (ID: `{b['id']}`) — {status}")

    lines.append("\n/stop `<id>` — остановить\n/run `<id>` — запустить")
    await message.answer("\n".join(lines), parse_mode="Markdown")


@router.message(Command("stop"))
async def cmd_stop(message: Message):
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Использование: /stop <id>")
        return

    bot_id = int(parts[1])
    bot = await get_bot(bot_id)
    if not bot:
        await message.answer(f"Бот с ID {bot_id} не найден.")
        return

    stopped = await stop_bot(bot_id)
    if stopped:
        await update_bot_status(bot_id, "stopped")
        await message.answer(f"Бот *{bot['name']}* остановлен.", parse_mode="Markdown")
    else:
        await message.answer(f"Бот *{bot['name']}* не запущен.", parse_mode="Markdown")


@router.message(Command("run"))
async def cmd_run(message: Message):
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Использование: /run <id>")
        return

    bot_id = int(parts[1])
    bot = await get_bot(bot_id)
    if not bot:
        await message.answer(f"Бот с ID {bot_id} не найден.")
        return

    if is_running(bot_id):
        await message.answer(f"Бот *{bot['name']}* уже запущен.", parse_mode="Markdown")
        return

    try:
        pid = await start_bot(bot_id, bot["file_path"], bot["token"])
        await update_bot_status(bot_id, "running", pid)
        await message.answer(f"Бот *{bot['name']}* запущен! 🚀", parse_mode="Markdown")
    except Exception as e:
        await message.answer(f"Ошибка при запуске: `{e}`", parse_mode="Markdown")
