from __future__ import annotations

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message

from db.database import get_all_bots, get_bot, get_bot_by_name, update_bot_status, update_bot_username
from services.bot_runner import get_bot_logs, is_running, start_bot, stop_bot

router = Router()


async def _ensure_username(b: dict) -> str:
    """Return @username for bot, fetching from Telegram API if missing."""
    if b.get("username"):
        return b["username"]
    if not b.get("token"):
        return ""
    try:
        tmp = Bot(token=b["token"])
        info = await tmp.get_me()
        await tmp.session.close()
        await update_bot_username(b["id"], info.username)
        return info.username
    except Exception:
        return ""


@router.message(Command("list"))
async def cmd_list(message: Message):
    bots = await get_all_bots()
    if not bots:
        await message.answer("Ботов пока нет. Создай первого командой /create")
        return

    lines = ["📋 *Список ботов:*\n"]
    for b in bots:
        status = "🟢 работает" if is_running(b["id"]) else "🔴 остановлен"
        username = await _ensure_username(b)
        username_str = f" (@{username})" if username else ""
        lines.append(f"*{b['name']}*{username_str} (ID: `{b['id']}`) — {status}")

    lines.append("\n/stop `<id>` — остановить\n/run `<id или @username>` — запустить\n/logs `<id>` — ошибки")
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


async def _resolve_bot(arg: str) -> dict | None:
    if arg.isdigit():
        return await get_bot(int(arg))
    return await get_bot_by_name(arg)


@router.message(Command("run"))
async def cmd_run(message: Message):
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Использование: /run <id или @username или имя>")
        return

    bot = await _resolve_bot(parts[1])
    if not bot:
        await message.answer(f"Бот `{parts[1]}` не найден.", parse_mode="Markdown")
        return

    if is_running(bot["id"]):
        await message.answer(f"Бот *{bot['name']}* уже запущен.", parse_mode="Markdown")
        return

    try:
        pid = await start_bot(bot["id"], bot["file_path"], bot["token"])
        await update_bot_status(bot["id"], "running", pid)
        await message.answer(f"Бот *{bot['name']}* запущен! 🚀", parse_mode="Markdown")
    except Exception as e:
        await update_bot_status(bot["id"], "error")
        await message.answer(
            f"Ошибка при запуске *{bot['name']}*:\n```\n{e}\n```\n\nПодробнее: /logs {bot['id']}",
            parse_mode="Markdown",
        )


@router.message(Command("logs"))
async def cmd_logs(message: Message):
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Использование: /logs <id>")
        return

    bot = await _resolve_bot(parts[1])
    if not bot:
        await message.answer("Бот не найден.")
        return

    logs = get_bot_logs(bot["id"])
    if not logs:
        await message.answer(
            f"Логов для *{bot['name']}* нет (или бот ещё не запускался в этой сессии).",
            parse_mode="Markdown",
        )
        return

    if len(logs) > 3500:
        logs = "...\n" + logs[-3500:]
    await message.answer(f"📋 Логи *{bot['name']}*:\n```\n{logs}\n```", parse_mode="Markdown")
