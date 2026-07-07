from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from db.database import delete_bot, get_all_bots, get_bot, get_bot_by_name, update_bot_status, update_bot_username
from services.bot_runner import get_bot_logs, is_running, start_bot, stop_bot

router = Router()


# ── helpers ──────────────────────────────────────────────────────────────────

async def _ensure_username(b: dict) -> str:
    if b.get("username"):
        return b["username"]
    if not b.get("token"):
        return ""
    try:
        async with Bot(token=b["token"]) as tmp:
            info = await tmp.get_me()
        await update_bot_username(b["id"], info.username)
        return info.username
    except Exception:
        return ""


def _status_icon(bot_id: int) -> str:
    return "🟢" if is_running(bot_id) else "🔴"


def _list_keyboard(bots: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for b in bots:
        icon = _status_icon(b["id"])
        username = b.get("username") or ""
        label = f"{icon} {b['name']}" + (f"  @{username}" if username else "")
        rows.append([InlineKeyboardButton(text=label, callback_data=f"info:{b['id']}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _bot_keyboard(bot_id: int) -> InlineKeyboardMarkup:
    running = is_running(bot_id)
    rows = []
    if running:
        rows.append([
            InlineKeyboardButton(text="🔴 Остановить", callback_data=f"stop:{bot_id}"),
            InlineKeyboardButton(text="🔁 Перезапустить", callback_data=f"restart:{bot_id}"),
        ])
    else:
        rows.append([
            InlineKeyboardButton(text="🟢 Запустить", callback_data=f"start:{bot_id}"),
        ])
    rows.append([
        InlineKeyboardButton(text="📋 Логи", callback_data=f"logs:{bot_id}"),
        InlineKeyboardButton(text="🗑 Удалить", callback_data=f"delete:{bot_id}"),
    ])
    rows.append([
        InlineKeyboardButton(text="◀ Назад", callback_data="list"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _bot_text(b: dict) -> str:
    icon = _status_icon(b["id"])
    username = b.get("username") or ""
    username_line = f"@{username}\n" if username else ""
    status = "работает" if is_running(b["id"]) else "остановлен"
    return (
        f"🤖 *{b['name']}*\n"
        f"{username_line}"
        f"Статус: {icon} {status}\n"
        f"ID: `{b['id']}`"
    )


# ── /list ─────────────────────────────────────────────────────────────────────

@router.message(Command("list"))
async def cmd_list(message: Message):
    bots = await get_all_bots()
    if not bots:
        await message.answer("Ботов пока нет. Создай первого командой /create")
        return
    for b in bots:
        b["username"] = await _ensure_username(b)
    await message.answer(
        "📋 *Мои боты* — нажми на бота для управления:",
        parse_mode="Markdown",
        reply_markup=_list_keyboard(bots),
    )


# ── /stop ─────────────────────────────────────────────────────────────────────

@router.message(Command("stop"))
async def cmd_stop(message: Message):
    bots = await get_all_bots()
    running = [b for b in bots if is_running(b["id"])]
    if not running:
        await message.answer("Нет запущенных ботов.")
        return
    for b in running:
        b["username"] = await _ensure_username(b)
    rows = [[InlineKeyboardButton(
        text=f"🔴 {b['name']}" + (f"  @{b['username']}" if b.get("username") else ""),
        callback_data=f"stop:{b['id']}"
    )] for b in running]
    await message.answer(
        "Выбери бота для остановки:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


# ── /run ──────────────────────────────────────────────────────────────────────

@router.message(Command("run"))
async def cmd_run(message: Message):
    bots = await get_all_bots()
    stopped = [b for b in bots if not is_running(b["id"])]
    if not stopped:
        await message.answer("Все боты уже запущены.")
        return
    for b in stopped:
        b["username"] = await _ensure_username(b)
    rows = [[InlineKeyboardButton(
        text=f"🟢 {b['name']}" + (f"  @{b['username']}" if b.get("username") else ""),
        callback_data=f"start:{b['id']}"
    )] for b in stopped]
    await message.answer(
        "Выбери бота для запуска:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


# ── /logs ─────────────────────────────────────────────────────────────────────

@router.message(Command("logs"))
async def cmd_logs(message: Message):
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Использование: /logs <id>")
        return
    b = await (get_bot(int(parts[1])) if parts[1].isdigit() else get_bot_by_name(parts[1]))
    if not b:
        await message.answer("Бот не найден.")
        return
    await _send_logs(message.answer, b)


async def _send_logs(send_fn, b: dict) -> None:
    logs = get_bot_logs(b["id"])
    if not logs:
        await send_fn(
            f"Логов для *{b['name']}* нет (или бот ещё не запускался в этой сессии).",
            parse_mode="Markdown",
        )
        return
    if len(logs) > 3500:
        logs = "...\n" + logs[-3500:]
    await send_fn(f"📋 Логи *{b['name']}*:\n```\n{logs}\n```", parse_mode="Markdown")


# ── callbacks ─────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "list")
async def cb_list(callback: CallbackQuery):
    await callback.answer()
    bots = await get_all_bots()
    if not bots:
        await callback.message.edit_text("Ботов пока нет. Создай первого командой /create")
        return
    for b in bots:
        b["username"] = await _ensure_username(b)
    await callback.message.edit_text(
        "📋 *Мои боты* — нажми на бота для управления:",
        parse_mode="Markdown",
        reply_markup=_list_keyboard(bots),
    )


@router.callback_query(F.data.startswith("info:"))
async def cb_info(callback: CallbackQuery):
    await callback.answer()
    bot_id = int(callback.data.split(":")[1])
    b = await get_bot(bot_id)
    if not b:
        await callback.message.answer("Бот не найден.")
        return
    b["username"] = await _ensure_username(b)
    try:
        await callback.message.edit_text(
            _bot_text(b),
            parse_mode="Markdown",
            reply_markup=_bot_keyboard(bot_id),
        )
    except Exception:
        await callback.message.answer(
            _bot_text(b),
            parse_mode="Markdown",
            reply_markup=_bot_keyboard(bot_id),
        )


@router.callback_query(F.data.startswith("start:"))
async def cb_start(callback: CallbackQuery):
    await callback.answer()
    bot_id = int(callback.data.split(":")[1])
    b = await get_bot(bot_id)
    if not b:
        await callback.message.answer("Бот не найден.")
        return
    if is_running(bot_id):
        await callback.message.answer("Уже запущен.")
        return
    err_text: str | None = None
    try:
        pid = await start_bot(bot_id, b["file_path"], b["token"])
        await update_bot_status(bot_id, "running", pid)
    except Exception as e:
        await update_bot_status(bot_id, "error")
        err_text = str(e)[:300]
    b["username"] = await _ensure_username(b)
    try:
        await callback.message.edit_text(
            _bot_text(b), parse_mode="Markdown", reply_markup=_bot_keyboard(bot_id)
        )
    except Exception:
        pass
    if err_text:
        await callback.message.answer(
            f"❌ Ошибка при запуске:\n```\n{err_text}\n```\n\nСм. /logs {bot_id}",
            parse_mode="Markdown",
        )


@router.callback_query(F.data.startswith("stop:"))
async def cb_stop(callback: CallbackQuery):
    await callback.answer()
    bot_id = int(callback.data.split(":")[1])
    b = await get_bot(bot_id)
    if not b:
        await callback.message.answer("Бот не найден.")
        return
    stopped = await stop_bot(bot_id)
    if stopped:
        await update_bot_status(bot_id, "stopped")
    b["username"] = await _ensure_username(b)
    try:
        await callback.message.edit_text(
            _bot_text(b), parse_mode="Markdown", reply_markup=_bot_keyboard(bot_id)
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("restart:"))
async def cb_restart(callback: CallbackQuery):
    await callback.answer()
    bot_id = int(callback.data.split(":")[1])
    b = await get_bot(bot_id)
    if not b:
        await callback.message.answer("Бот не найден.")
        return
    await stop_bot(bot_id)
    err_text: str | None = None
    try:
        pid = await start_bot(bot_id, b["file_path"], b["token"])
        await update_bot_status(bot_id, "running", pid)
    except Exception as e:
        await update_bot_status(bot_id, "error")
        err_text = str(e)[:300]
    b["username"] = await _ensure_username(b)
    try:
        await callback.message.edit_text(
            _bot_text(b), parse_mode="Markdown", reply_markup=_bot_keyboard(bot_id)
        )
    except Exception:
        pass
    if err_text:
        await callback.message.answer(
            f"❌ Ошибка при перезапуске:\n```\n{err_text}\n```",
            parse_mode="Markdown",
        )


@router.callback_query(F.data.startswith("logs:"))
async def cb_logs(callback: CallbackQuery):
    await callback.answer()
    bot_id = int(callback.data.split(":")[1])
    b = await get_bot(bot_id)
    if not b:
        await callback.message.answer("Бот не найден.")
        return
    await _send_logs(callback.message.answer, b)


@router.callback_query(F.data.startswith("delete:"))
async def cb_delete(callback: CallbackQuery):
    await callback.answer()
    bot_id = int(callback.data.split(":")[1])
    b = await get_bot(bot_id)
    if not b:
        await callback.message.answer("Бот не найден.")
        return
    await stop_bot(bot_id)
    await delete_bot(bot_id)
    await callback.message.edit_text(
        f"🗑 Бот *{b['name']}* удалён.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="◀ К списку", callback_data="list")
        ]]),
    )
