from __future__ import annotations

import asyncio
import functools
import logging
import tempfile
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import ASSEMBLYAI_API_KEY
from db.database import delete_bot, get_all_bots, get_bot, get_bot_by_name, update_bot_status, update_bot_username
from services.bot_runner import _make_extra_env, get_bot_logs, is_running, start_bot, stop_bot
from services.claude_service import fix_bot_code, generate_bot_code, improve_bot_code
from services.github_sync import push_bot_to_github
from services.voice_service import transcribe_voice


logger = logging.getLogger(__name__)


class FixBotStates(StatesGroup):
    describing_bug = State()

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
        InlineKeyboardButton(text="🔍 Авто-диагностика", callback_data=f"autofix:{bot_id}"),
        InlineKeyboardButton(text="🐛 Исправить баг", callback_data=f"fixbug:{bot_id}"),
    ])
    rows.append([
        InlineKeyboardButton(text="🔄 Перегенерировать (немного улучшим код)", callback_data=f"recreate:{bot_id}"),
    ])
    rows.append([
        InlineKeyboardButton(text="◀ К списку", callback_data="list"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _bot_text(b: dict) -> str:
    icon = _status_icon(b["id"])
    username = b.get("username") or ""
    username_line = f"@{username}\n" if username else ""
    status = "работает" if is_running(b["id"]) else "остановлен"
    return (
        f"🤖 <b>{b['name']}</b>\n"
        f"{username_line}"
        f"Статус: {icon} {status}\n"
        f"ID: <code>{b['id']}</code>"
    )


async def _send_list(send_fn, bots: list[dict]) -> None:
    for b in bots:
        b["username"] = await _ensure_username(b)
    await send_fn(
        "📋 <b>Мои боты</b> — нажми на бота для управления:",
        parse_mode="HTML",
        reply_markup=_list_keyboard(bots),
    )


# ── /list ─────────────────────────────────────────────────────────────────────

@router.message(Command("list"))
async def cmd_list(message: Message):
    bots = await get_all_bots()
    if not bots:
        await message.answer("Ботов пока нет. Создай первого командой /create")
        return
    await _send_list(message.answer, bots)


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
    await message.answer("Выбери бота для остановки:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


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
    await message.answer("Выбери бота для запуска:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


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
    bot_id = b["id"]
    logs = get_bot_logs(bot_id)
    back_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="◀ К боту", callback_data=f"info:{bot_id}"),
        InlineKeyboardButton(text="◀ К списку", callback_data="list"),
    ]])
    if not logs:
        await send_fn(
            f"Логов для <b>{b['name']}</b> нет (бот не запускался в этой сессии).",
            parse_mode="HTML",
            reply_markup=back_kb,
        )
        return
    if len(logs) > 3500:
        logs = "...\n" + logs[-3500:]
    await send_fn(
        f"📋 Логи <b>{b['name']}</b>:\n<pre>{logs}</pre>",
        parse_mode="HTML",
        reply_markup=back_kb,
    )


# ── callbacks ─────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "list")
async def cb_list(callback: CallbackQuery):
    await callback.answer()
    chat_id = callback.message.chat.id
    try:
        await callback.message.delete()
    except Exception:
        pass
    bots = await get_all_bots()
    if not bots:
        await callback.bot.send_message(chat_id, "Ботов пока нет. Создай первого командой /create")
        return
    for b in bots:
        b["username"] = await _ensure_username(b)
    await callback.bot.send_message(
        chat_id,
        "📋 <b>Мои боты</b> — нажми на бота для управления:",
        parse_mode="HTML",
        reply_markup=_list_keyboard(bots),
    )


@router.callback_query(F.data.startswith("info:"))
async def cb_info(callback: CallbackQuery):
    await callback.answer()
    chat_id = callback.message.chat.id
    try:
        await callback.message.delete()
    except Exception:
        pass
    bot_id = int(callback.data.split(":")[1])
    b = await get_bot(bot_id)
    if not b:
        await callback.bot.send_message(chat_id, "Бот не найден.")
        return
    b["username"] = await _ensure_username(b)
    await callback.bot.send_message(
        chat_id,
        _bot_text(b),
        parse_mode="HTML",
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
    try:
        pid = await start_bot(bot_id, b["file_path"], b["token"], extra_env=_make_extra_env(b))
        await update_bot_status(bot_id, "running", pid)
    except Exception as e:
        await update_bot_status(bot_id, "error")
        await callback.message.edit_text(
            "❌ Бот не смог запуститься — в сгенерированном коде ошибка.\n\n"
            "Удали и создай заново через /create.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🗑 Удалить и пересоздать", callback_data=f"delete:{bot_id}")
            ]]),
        )
        return
    b["username"] = await _ensure_username(b)
    await callback.message.edit_text(
        _bot_text(b), parse_mode="HTML", reply_markup=_bot_keyboard(bot_id)
    )


@router.callback_query(F.data.startswith("stop:"))
async def cb_stop(callback: CallbackQuery):
    await callback.answer()
    bot_id = int(callback.data.split(":")[1])
    b = await get_bot(bot_id)
    if not b:
        await callback.message.answer("Бот не найден.")
        return
    await stop_bot(bot_id)
    await update_bot_status(bot_id, "stopped")
    b["username"] = await _ensure_username(b)
    try:
        await callback.message.edit_text(
            _bot_text(b), parse_mode="HTML", reply_markup=_bot_keyboard(bot_id)
        )
    except Exception:
        await callback.message.answer(
            _bot_text(b), parse_mode="HTML", reply_markup=_bot_keyboard(bot_id)
        )


@router.callback_query(F.data.startswith("restart:"))
async def cb_restart(callback: CallbackQuery):
    await callback.answer()
    bot_id = int(callback.data.split(":")[1])
    b = await get_bot(bot_id)
    if not b:
        await callback.message.answer("Бот не найден.")
        return
    await stop_bot(bot_id)
    try:
        pid = await start_bot(bot_id, b["file_path"], b["token"], extra_env=_make_extra_env(b))
        await update_bot_status(bot_id, "running", pid)
    except Exception as e:
        logger.error(f"Failed to restart bot {bot_id}: {e}")
        await update_bot_status(bot_id, "error")
        await callback.message.edit_text(
            "❌ Бот не смог перезапуститься — в коде ошибка.\n\n"
            "Удали и создай заново через /create.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🗑 Удалить", callback_data=f"delete:{bot_id}")
            ]]),
        )
        return
    b["username"] = await _ensure_username(b)
    try:
        await callback.message.edit_text(
            _bot_text(b), parse_mode="HTML", reply_markup=_bot_keyboard(bot_id)
        )
    except Exception:
        await callback.message.answer(
            _bot_text(b), parse_mode="HTML", reply_markup=_bot_keyboard(bot_id)
        )


@router.callback_query(F.data.startswith("logs:"))
async def cb_logs(callback: CallbackQuery):
    await callback.answer()
    bot_id = int(callback.data.split(":")[1])
    b = await get_bot(bot_id)
    if not b:
        await callback.message.answer("Бот не найден.")
        return
    chat_id = callback.message.chat.id
    try:
        await callback.message.delete()
    except Exception:
        pass
    await _send_logs(functools.partial(callback.bot.send_message, chat_id), b)


@router.callback_query(F.data.startswith("delete:"))
async def cb_delete(callback: CallbackQuery):
    await callback.answer()
    bot_id = int(callback.data.split(":")[1])
    b = await get_bot(bot_id)
    if not b:
        await callback.message.answer("Бот не найден.")
        return
    name = b["name"]
    await stop_bot(bot_id)
    await delete_bot(bot_id)
    await callback.message.edit_text(
        f"✅ Бот <b>{name}</b> удалён.\n\nСоздай нового: /create",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="◀ К списку", callback_data="list")
        ]]),
    )


@router.callback_query(F.data.startswith("recreate:"))
async def cb_recreate(callback: CallbackQuery):
    await callback.answer()
    bot_id = int(callback.data.split(":")[1])
    b = await get_bot(bot_id)
    if not b:
        await callback.message.edit_text("Бот не найден.")
        return
    if not b.get("description"):
        await callback.message.edit_text(
            "❌ Не могу пересоздать — описание бота не сохранилось.\n\nСоздай заново через /create.",
        )
        return

    current_code = ""
    if b.get("file_path"):
        try:
            current_code = Path(b["file_path"]).read_text(encoding="utf-8")
        except Exception:
            pass

    if current_code:
        await callback.message.edit_text(f"✨ Улучшаю код <b>{b['name']}</b>...", parse_mode="HTML")
        task = improve_bot_code(current_code, b.get("description", ""))
    else:
        await callback.message.edit_text(f"🔧 Генерирую код для <b>{b['name']}</b>...", parse_mode="HTML")
        task = generate_bot_code(b.get("description", ""))

    try:
        code = await asyncio.wait_for(task, timeout=240.0)
    except Exception as e:
        logger.error(f"Failed to regenerate bot {bot_id}: {e}")
        await callback.message.edit_text(
            "⚠️ Не удалось улучшить код. Попробуй ещё раз.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🔄 Попробовать снова", callback_data=f"recreate:{bot_id}"),
                InlineKeyboardButton(text="◀ Назад", callback_data=f"info:{bot_id}"),
            ]]),
        )
        return

    await stop_bot(bot_id)

    bot_file = Path(b["file_path"])
    bot_file.write_text(code, encoding="utf-8")
    asyncio.create_task(push_bot_to_github(b["name"], code))

    try:
        pid = await start_bot(bot_id, str(bot_file), b["token"], extra_env=_make_extra_env(b))
        await update_bot_status(bot_id, "running", pid)
        await callback.message.edit_text(
            f"✅ Бот <b>{b['name']}</b> пересоздан и запущен!\n\n"
            f"Код обновлён с учётом последних улучшений.",
            parse_mode="HTML",
            reply_markup=_bot_keyboard(bot_id),
        )
    except Exception as e:
        await update_bot_status(bot_id, "error")
        await callback.message.edit_text(
            f"⚠️ Код сгенерирован, но бот не запустился.\n\n<code>{str(e)[-300:]}</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🔄 Перегенерировать снова", callback_data=f"recreate:{bot_id}"),
                InlineKeyboardButton(text="🗑 Удалить", callback_data=f"delete:{bot_id}"),
            ]]),
        )


# ── auto-diagnose ─────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("autofix:"))
async def cb_auto_diagnose(callback: CallbackQuery):
    await callback.answer()
    bot_id = int(callback.data.split(":")[1])
    b = await get_bot(bot_id)
    if not b or not b.get("file_path") or not Path(b["file_path"]).exists():
        await callback.message.edit_text("❌ Файл бота не найден — попробуй Перегенерировать.")
        return

    current_code = Path(b["file_path"]).read_text(encoding="utf-8")
    error_log = get_bot_logs(bot_id) or ""

    if error_log:
        bug_description = f"Bot crashed with the following error:\n{error_log}"
    else:
        bug_description = (
            "The bot is not working correctly but no crash log is available. "
            "Analyze the code carefully, find potential bugs (wrong imports, missing asyncio.run(main()), "
            "incorrect aiogram 3.x patterns, missing error handling) and fix them."
        )

    await callback.message.edit_text(
        f"🔍 Диагностирую <b>{b['name']}</b>...\n\n"
        + (f"<code>{error_log[-300:]}</code>" if error_log else "Логов нет — анализирую код."),
        parse_mode="HTML",
    )

    try:
        fixed_code = await asyncio.wait_for(
            fix_bot_code(current_code, bug_description), timeout=240.0
        )
    except Exception:
        await callback.message.edit_text(
            "⚠️ Не удалось проанализировать код. Попробуй ещё раз.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🔍 Попробовать снова", callback_data=f"autofix:{bot_id}"),
                InlineKeyboardButton(text="◀ Назад", callback_data=f"info:{bot_id}"),
            ]]),
        )
        return

    await stop_bot(bot_id)
    Path(b["file_path"]).write_text(fixed_code, encoding="utf-8")
    asyncio.create_task(push_bot_to_github(b["name"], fixed_code))

    try:
        pid = await start_bot(bot_id, b["file_path"], b["token"], extra_env=_make_extra_env(b))
        await update_bot_status(bot_id, "running", pid)
        await callback.message.edit_text(
            f"✅ <b>{b['name']}</b> исправлен и перезапущен!",
            parse_mode="HTML",
            reply_markup=_bot_keyboard(bot_id),
        )
    except Exception as e:
        await update_bot_status(bot_id, "error")
        await callback.message.edit_text(
            f"⚠️ Код исправлен, но бот снова не запустился:\n<code>{str(e)[-300:]}</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🔍 Диагностировать снова", callback_data=f"autofix:{bot_id}"),
                InlineKeyboardButton(text="🗑 Удалить", callback_data=f"delete:{bot_id}"),
            ]]),
        )


# ── fix bug ───────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("fixbug:"))
async def cb_fix_bug(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    bot_id = int(callback.data.split(":")[1])
    b = await get_bot(bot_id)
    if not b or not b.get("file_path") or not Path(b["file_path"]).exists():
        await callback.message.edit_text("❌ Файл бота не найден — попробуй Перегенерировать.")
        return
    await state.set_state(FixBotStates.describing_bug)
    await state.update_data(fix_bot_id=bot_id)
    await callback.message.edit_text(
        f"🐛 Исправляем <b>{b['name']}</b>\n\n"
        "Опиши баг или что нужно улучшить — голосовым или текстом.\n\n"
        "/cancel — отменить",
        parse_mode="HTML",
    )


async def _recognize_voice_fix(message: Message, bot: Bot) -> str | None:
    if not ASSEMBLYAI_API_KEY:
        await message.answer("⚠️ Голосовые не настроены. Напиши текстом.")
        return None
    status_msg = await message.answer("🎤 Распознаю голосовое...")
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        file = await bot.get_file(message.voice.file_id)
        await bot.download_file(file.file_path, destination=tmp_path)
        text = await transcribe_voice(tmp_path)
    except Exception:
        try:
            await status_msg.delete()
        except Exception:
            pass
        await message.answer("Не удалось распознать, попробуй текстом.")
        return None
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    try:
        await status_msg.delete()
    except Exception:
        pass
    if not text.strip():
        await message.answer("Не удалось разобрать голосовое, попробуй ещё раз.")
        return None
    await message.answer(f"🎤 Распознал: <i>{text}</i>", parse_mode="HTML")
    return text


async def _apply_fix(message: Message, state: FSMContext, bug_description: str, bot: Bot) -> None:
    data = await state.get_data()
    bot_id = data["fix_bot_id"]
    await state.clear()

    b = await get_bot(bot_id)
    if not b:
        await message.answer("Бот не найден.")
        return

    current_code = Path(b["file_path"]).read_text(encoding="utf-8")

    fix_msg = await message.answer(f"🔧 Исправляю код <b>{b['name']}</b>...", parse_mode="HTML")
    try:
        fixed_code = await asyncio.wait_for(
            fix_bot_code(current_code, bug_description), timeout=240.0
        )
    except Exception:
        try:
            await fix_msg.delete()
        except Exception:
            pass
        await message.answer(
            "⚠️ Не удалось исправить код. Попробуй ещё раз.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🐛 Попробовать снова", callback_data=f"fixbug:{bot_id}"),
            ]]),
        )
        return

    try:
        await fix_msg.delete()
    except Exception:
        pass

    await stop_bot(bot_id)
    Path(b["file_path"]).write_text(fixed_code, encoding="utf-8")
    asyncio.create_task(push_bot_to_github(b["name"], fixed_code))

    try:
        pid = await start_bot(bot_id, b["file_path"], b["token"], extra_env=_make_extra_env(b))
        await update_bot_status(bot_id, "running", pid)
        await message.answer(
            f"✅ Бот <b>{b['name']}</b> исправлен и перезапущен!",
            parse_mode="HTML",
            reply_markup=_bot_keyboard(bot_id),
        )
    except Exception as e:
        await update_bot_status(bot_id, "error")
        await message.answer(
            f"⚠️ Код исправлен, но бот не запустился.\n\n<code>{str(e)[-300:]}</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🐛 Исправить снова", callback_data=f"fixbug:{bot_id}"),
                InlineKeyboardButton(text="🗑 Удалить", callback_data=f"delete:{bot_id}"),
            ]]),
        )


@router.message(FixBotStates.describing_bug, F.voice)
async def msg_fix_voice(message: Message, state: FSMContext, bot: Bot):
    text = await _recognize_voice_fix(message, bot)
    if text:
        await _apply_fix(message, state, text, bot)


@router.message(FixBotStates.describing_bug, F.text, ~F.text.startswith("/"))
async def msg_fix_text(message: Message, state: FSMContext, bot: Bot):
    await _apply_fix(message, state, message.text, bot)
