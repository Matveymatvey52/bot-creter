from __future__ import annotations

import asyncio
import functools
import html
import logging
import re
import tempfile
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import ASSEMBLYAI_API_KEY
from db.database import (
    delete_bot,
    disable_bot_feature,
    enable_bot_feature,
    get_all_bots,
    get_bot,
    get_bot_by_name,
    get_bot_features,
    get_bot_sheets_config,
    set_bot_sheets_config,
    update_bot_status,
    update_bot_username,
)
from features.sheets import get_service_account_email, verify_access
from handlers.admin_manager import _is_owner
from runtime.registry import _CUSTOM_FEATURES_DIR, discover_features, infer_template_id, invalidate_custom_feature_cache
from services.bot_runner import _make_extra_env, get_bot_logs, is_running, start_bot, stop_bot
from services.claude_service import fix_bot_code, generate_bot_code, improve_bot_code
from services.github_sync import push_bot_to_github
from services.voice_service import transcribe_voice


logger = logging.getLogger(__name__)

_DENY_TEXT = "⛔ Управление ботами доступно только владельцу."
_BUSY_TEXT = "⏳ Для этого бота уже выполняется операция — подожди её завершения."

# Guards recreate/autofix/fixbug (expensive Claude calls + file writes) against
# a double click starting a second call for the same bot before the first finishes.
_busy_bots: set[int] = set()

# The live webhook Registry, set once by runtime/combined_app.py's bootstrap —
# same pattern as handlers/create_bot.py's own _registry/set_registry(). Needed
# so toggling a feature can call reload_one(bot_id) and have it take effect
# immediately, instead of only on the next full registry rebuild.
_registry = None


def set_registry(registry) -> None:
    global _registry
    _registry = registry


class FixBotStates(StatesGroup):
    describing_bug = State()


class SheetsConnectFlow(StatesGroup):
    waiting_for_link = State()

router = Router()


# ── helpers ──────────────────────────────────────────────────────────────────

async def _deny_message(message: Message) -> None:
    await message.answer(_DENY_TEXT)


async def _deny_callback(callback: CallbackQuery) -> None:
    await callback.answer(_DENY_TEXT, show_alert=True)


async def _edit_or_resend(callback: CallbackQuery, text: str, **kwargs) -> None:
    try:
        await callback.message.edit_text(text, **kwargs)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            return  # card already shows this exact state — nothing to do
        await callback.message.answer(text, **kwargs)


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


def _delete_custom_feature_file(bot_id: int) -> None:
    """Deletes this bot's custom_features/bot_<id>.py (if any) and evicts it
    from the registry's module cache — cb_delete's own cleanup, since
    delete_bot() only clears the bot_custom_features DB row, not the file on
    disk or runtime.registry's in-memory cache of it (found in review:
    without this, a deleted bot_id leaves an orphaned file behind forever)."""
    invalidate_custom_feature_cache(bot_id)
    (_CUSTOM_FEATURES_DIR / f"bot_{bot_id}.py").unlink(missing_ok=True)


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
        InlineKeyboardButton(text="🧩 Фичи", callback_data=f"features:{bot_id}"),
        InlineKeyboardButton(text="🧩➕ Доработка", callback_data=f"customfeature:{bot_id}"),
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
    if not _is_owner(message.from_user.id):
        await _deny_message(message)
        return
    bots = await get_all_bots()
    if not bots:
        await message.answer("Ботов пока нет. Создай первого командой /create")
        return
    await _send_list(message.answer, bots)


# ── /stop ─────────────────────────────────────────────────────────────────────

@router.message(Command("stop"))
async def cmd_stop(message: Message):
    if not _is_owner(message.from_user.id):
        await _deny_message(message)
        return
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
    if not _is_owner(message.from_user.id):
        await _deny_message(message)
        return
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
    if not _is_owner(message.from_user.id):
        await _deny_message(message)
        return
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
        f"📋 Логи <b>{b['name']}</b>:\n<pre>{html.escape(logs)}</pre>",
        parse_mode="HTML",
        reply_markup=back_kb,
    )


# ── callbacks ─────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "list")
async def cb_list(callback: CallbackQuery):
    if not _is_owner(callback.from_user.id):
        await _deny_callback(callback)
        return
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
    if not _is_owner(callback.from_user.id):
        await _deny_callback(callback)
        return
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


def _features_keyboard(
    bot_id: int, compatible: list[dict], enabled: set[str], sheets_config: dict | None = None
) -> InlineKeyboardMarkup:
    rows = []
    for feature in compatible:
        name = feature["name"]
        icon = "✅" if name in enabled else "⬜"
        rows.append([
            InlineKeyboardButton(text=f"{icon} {name}", callback_data=f"togglefeature:{bot_id}:{name}"),
        ])
        # "sheets" needs a spreadsheet_id per bot beyond a plain on/off toggle
        # (unlike payments, which is wired factory-side against the DB with
        # no button UI at all — see sheets-feature-inventory) — this second
        # row only appears once the feature itself is enabled.
        if name == "sheets" and name in enabled:
            label = "📊 Подключить Google Таблицу"
            if sheets_config:
                # Telegram caps inline button text at 64 chars — a real
                # spreadsheet title (unbounded, comes from Google) can
                # easily blow that and make edit_text/send raise
                # BUTTON_TEXT_INVALID on every future render of this panel.
                shown = sheets_config["sheet_title"] or sheets_config["spreadsheet_id"]
                if len(shown) > 30:
                    shown = shown[:29] + "…"
                label = f"📊 Таблица: {shown} (изменить)"
            rows.append([InlineKeyboardButton(text=label, callback_data=f"sheetsconnect:{bot_id}")])
    rows.append([InlineKeyboardButton(text="◀ Назад", callback_data=f"info:{bot_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _compatible_features(template_id: str | None) -> list[dict]:
    """Features whose # COMPATIBLE_WITH: header explicitly lists this bot's
    template_id — never "all" (see runtime/registry.py's discover_features()),
    so a bot with an unrecognized/missing template_id simply has none."""
    return [f for f in discover_features() if template_id in f["compatible_with"]]


@router.callback_query(F.data.startswith("features:"))
async def cb_features_list(callback: CallbackQuery):
    if not _is_owner(callback.from_user.id):
        await _deny_callback(callback)
        return
    await callback.answer()
    bot_id = int(callback.data.split(":")[1])
    b = await get_bot(bot_id)
    if not b:
        await callback.message.answer("Бот не найден.")
        return
    template_id = infer_template_id(b.get("file_path"))
    compatible = await _compatible_features(template_id)
    enabled = set(await get_bot_features(bot_id))
    sheets_config = await get_bot_sheets_config(bot_id) if "sheets" in enabled else None
    text = "🧩 <b>Фичи для этого бота</b> — нажми, чтобы включить/выключить:"
    if not compatible:
        text = "🧩 Для этого бота пока нет доступных фич (нет совместимых по шаблону)."
    await _edit_or_resend(
        callback,
        text,
        parse_mode="HTML",
        reply_markup=_features_keyboard(bot_id, compatible, enabled, sheets_config),
    )


@router.callback_query(F.data.startswith("togglefeature:"))
async def cb_toggle_feature(callback: CallbackQuery):
    if not _is_owner(callback.from_user.id):
        await _deny_callback(callback)
        return
    _, bot_id_str, feature_name = callback.data.split(":", 2)
    bot_id = int(bot_id_str)
    if bot_id in _busy_bots:
        await callback.answer(_BUSY_TEXT, show_alert=True)
        return
    _busy_bots.add(bot_id)
    try:
        b = await get_bot(bot_id)
        if not b:
            await callback.answer("Бот не найден.", show_alert=True)
            return
        template_id = infer_template_id(b.get("file_path"))
        feature = next((f for f in discover_features() if f["name"] == feature_name), None)
        if feature is None:
            await callback.answer("Фича не найдена.", show_alert=True)
            return
        enabled = set(await get_bot_features(bot_id))
        is_enabled = feature_name in enabled
        if not is_enabled and template_id not in feature["compatible_with"]:
            await callback.answer("⛔ Эта фича не подходит шаблону этого бота.", show_alert=True)
            return
        if is_enabled:
            await disable_bot_feature(bot_id, feature_name)
        else:
            await enable_bot_feature(bot_id, feature_name)
        if _registry is not None:
            await _registry.reload_one(bot_id)
        else:
            logger.debug(f"cb_toggle_feature: no live registry available — bot_id={bot_id} feature={feature_name!r} toggled in DB only")
        await callback.answer("✅ Включено" if not is_enabled else "🔴 Выключено")
        compatible = await _compatible_features(template_id)
        new_enabled = set(await get_bot_features(bot_id))
        new_sheets_config = await get_bot_sheets_config(bot_id) if "sheets" in new_enabled else None
        await _edit_or_resend(
            callback,
            "🧩 <b>Фичи для этого бота</b> — нажми, чтобы включить/выключить:",
            parse_mode="HTML",
            reply_markup=_features_keyboard(bot_id, compatible, new_enabled, new_sheets_config),
        )
    finally:
        _busy_bots.discard(bot_id)


_SPREADSHEET_ID_RE = re.compile(r"/d/([a-zA-Z0-9_-]{20,})")
_BARE_SPREADSHEET_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{20,}$")


def _parse_spreadsheet_id(text: str) -> str | None:
    text = text.strip()
    m = _SPREADSHEET_ID_RE.search(text)
    if m:
        return m.group(1)
    if _BARE_SPREADSHEET_ID_RE.match(text):
        return text
    return None


_SHEETS_CONNECT_TEXT_TEMPLATE = (
    "📊 <b>Подключение Google Таблицы</b>\n\n"
    "⚠️ Доступ к таблице получит <b>общий сервисный аккаунт фабрики</b> — "
    "один и тот же для ВСЕХ ботов на этой платформе, а не отдельный робот "
    "лично под твоего бота. Он уже имеет доступ ко всем таблицам, которые "
    "расшарили другие владельцы ботов через эту же фабрику. Если это "
    "неприемлемо (например, в таблице чувствительные данные) — не "
    "подключай её сюда.\n\n"
    "Как подключить:\n"
    "1. Открой свою Google Таблицу → «Настройки доступа» → «Добавить пользователей».\n"
    "2. Вставь этот email, выдай роль «Редактор»:\n"
    "<code>{sa_email}</code>\n"
    "3. Пришли сюда ссылку на таблицу.\n\n"
    "Для отмены — кнопка ниже."
)


def _sheets_cancel_keyboard(bot_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"sheetscancel:{bot_id}")]
    ])


async def _back_to_features_panel(bot_id: int, edit_target) -> None:
    b = await get_bot(bot_id)
    if not b:
        await edit_target.answer("Бот не найден.")
        return
    template_id = infer_template_id(b.get("file_path"))
    compatible = await _compatible_features(template_id)
    enabled = set(await get_bot_features(bot_id))
    sheets_config = await get_bot_sheets_config(bot_id) if "sheets" in enabled else None
    await edit_target.answer(
        "🧩 <b>Фичи для этого бота</b> — нажми, чтобы включить/выключить:",
        parse_mode="HTML",
        reply_markup=_features_keyboard(bot_id, compatible, enabled, sheets_config),
    )


@router.callback_query(F.data.startswith("sheetsconnect:"))
async def cb_sheets_connect_start(callback: CallbackQuery, state: FSMContext):
    if not _is_owner(callback.from_user.id):
        await _deny_callback(callback)
        return
    await callback.answer()
    bot_id = int(callback.data.split(":")[1])
    sa_email = await get_service_account_email()
    if not sa_email:
        await callback.message.answer(
            "⚠️ Фича sheets не настроена на сервере (нет GOOGLE_SHEETS_SA_KEY_PATH). "
            "Обратитесь к администратору фабрики."
        )
        return
    await state.set_state(SheetsConnectFlow.waiting_for_link)
    await state.update_data(bot_id=bot_id)
    await _edit_or_resend(
        callback,
        _SHEETS_CONNECT_TEXT_TEMPLATE.format(sa_email=sa_email),
        parse_mode="HTML",
        reply_markup=_sheets_cancel_keyboard(bot_id),
    )


@router.callback_query(F.data.startswith("sheetscancel:"))
async def cb_sheets_connect_cancel(callback: CallbackQuery, state: FSMContext):
    if not _is_owner(callback.from_user.id):
        await _deny_callback(callback)
        return
    await callback.answer()
    bot_id = int(callback.data.split(":")[1])
    await state.clear()
    await _back_to_features_panel(bot_id, callback.message)


@router.message(SheetsConnectFlow.waiting_for_link, F.text, ~F.text.startswith("/"))
async def msg_sheets_connect_link(message: Message, state: FSMContext):
    if not _is_owner(message.from_user.id):
        return
    data = await state.get_data()
    bot_id = data.get("bot_id")
    if bot_id is None:
        await state.clear()
        return
    spreadsheet_id = _parse_spreadsheet_id(message.text)
    if spreadsheet_id is None:
        await message.answer(
            "⚠️ Не похоже на ссылку или ID Google Таблицы. Пришли ссылку вида "
            "https://docs.google.com/spreadsheets/d/.../ или сам ID."
        )
        return
    if bot_id in _busy_bots:
        await message.answer(_BUSY_TEXT)
        return
    _busy_bots.add(bot_id)
    try:
        sa_email = await get_service_account_email()
        title = await verify_access(spreadsheet_id)

        # verify_access() is a slow network round trip — the owner could tap
        # "❌ Отмена" (or start connecting a different bot) while it's in
        # flight. Re-check the FSM is still exactly where we left it before
        # writing anything or claiming success against a screen the owner
        # has already navigated away from.
        current_state = await state.get_state()
        current_data = await state.get_data()
        if current_state != SheetsConnectFlow.waiting_for_link.state or current_data.get("bot_id") != bot_id:
            logger.info(f"msg_sheets_connect_link: bot_id={bot_id} flow state changed during verify_access — discarding result")
            return

        if title is None:
            await message.answer(
                f"⚠️ Не удалось открыть таблицу. Либо ссылка/ID неверны, либо доступ ещё "
                f"не выдан {sa_email}. Проверь и пришли ссылку ещё раз, либо нажми ❌ Отмена."
            )
            return
        await set_bot_sheets_config(bot_id, spreadsheet_id, title)
        await state.clear()
        await message.answer(
            f"✅ Подключено: «{title}». Бот теперь может читать и писать в эту таблицу "
            "через общий сервисный аккаунт фабрики."
        )
        await _back_to_features_panel(bot_id, message)
    finally:
        _busy_bots.discard(bot_id)


@router.message(SheetsConnectFlow.waiting_for_link)
async def msg_sheets_connect_invalid(message: Message) -> None:
    if not _is_owner(message.from_user.id):
        return
    await message.answer("Пришли ссылку на Google Таблицу текстом, либо нажми ❌ Отмена.")


@router.callback_query(F.data.startswith("start:"))
async def cb_start(callback: CallbackQuery):
    if not _is_owner(callback.from_user.id):
        await _deny_callback(callback)
        return
    bot_id = int(callback.data.split(":")[1])
    if bot_id in _busy_bots:
        await callback.answer(_BUSY_TEXT, show_alert=True)
        return
    _busy_bots.add(bot_id)
    await callback.answer()
    try:
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
            await _edit_or_resend(
                callback,
                "❌ Бот не смог запуститься — в сгенерированном коде ошибка.\n\n"
                "Удали и создай заново через /create.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="🗑 Удалить и пересоздать", callback_data=f"delete:{bot_id}")
                ]]),
            )
            return
        b["username"] = await _ensure_username(b)
        await _edit_or_resend(
            callback, _bot_text(b), parse_mode="HTML", reply_markup=_bot_keyboard(bot_id)
        )
    finally:
        _busy_bots.discard(bot_id)


@router.callback_query(F.data.startswith("stop:"))
async def cb_stop(callback: CallbackQuery):
    if not _is_owner(callback.from_user.id):
        await _deny_callback(callback)
        return
    await callback.answer()
    bot_id = int(callback.data.split(":")[1])
    b = await get_bot(bot_id)
    if not b:
        await callback.message.answer("Бот не найден.")
        return
    await stop_bot(bot_id)
    await update_bot_status(bot_id, "stopped")
    b["username"] = await _ensure_username(b)
    await _edit_or_resend(
        callback, _bot_text(b), parse_mode="HTML", reply_markup=_bot_keyboard(bot_id)
    )


@router.callback_query(F.data.startswith("restart:"))
async def cb_restart(callback: CallbackQuery):
    if not _is_owner(callback.from_user.id):
        await _deny_callback(callback)
        return
    bot_id = int(callback.data.split(":")[1])
    if bot_id in _busy_bots:
        await callback.answer(_BUSY_TEXT, show_alert=True)
        return
    _busy_bots.add(bot_id)
    await callback.answer()
    try:
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
            await _edit_or_resend(
                callback,
                "❌ Бот не смог перезапуститься — в коде ошибка.\n\n"
                "Удали и создай заново через /create.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="🗑 Удалить", callback_data=f"delete:{bot_id}")
                ]]),
            )
            return
        b["username"] = await _ensure_username(b)
        await _edit_or_resend(
            callback, _bot_text(b), parse_mode="HTML", reply_markup=_bot_keyboard(bot_id)
        )
    finally:
        _busy_bots.discard(bot_id)


@router.callback_query(F.data.startswith("logs:"))
async def cb_logs(callback: CallbackQuery):
    if not _is_owner(callback.from_user.id):
        await _deny_callback(callback)
        return
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
    if not _is_owner(callback.from_user.id):
        await _deny_callback(callback)
        return
    bot_id = int(callback.data.split(":")[1])
    # Was previously the only mutating handler in this file with no
    # _busy_bots check at all — a bot could be deleted mid-generation or
    # mid-apply of a custom feature (or mid-recreate/autofix/fixbug),
    # racing whichever of those was about to write to the very bot row/file
    # this deletes.
    if bot_id in _busy_bots:
        await callback.answer(_BUSY_TEXT, show_alert=True)
        return
    await callback.answer()
    b = await get_bot(bot_id)
    if not b:
        await callback.message.answer("Бот не найден.")
        return
    name = b["name"]
    await stop_bot(bot_id)
    await delete_bot(bot_id)
    _delete_custom_feature_file(bot_id)
    await callback.message.edit_text(
        f"✅ Бот <b>{name}</b> удалён.\n\nСоздай нового: /create",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="◀ К списку", callback_data="list")
        ]]),
    )


@router.callback_query(F.data.startswith("recreate:"))
async def cb_recreate(callback: CallbackQuery):
    if not _is_owner(callback.from_user.id):
        await _deny_callback(callback)
        return
    bot_id = int(callback.data.split(":")[1])
    if bot_id in _busy_bots:
        await callback.answer(_BUSY_TEXT, show_alert=True)
        return
    _busy_bots.add(bot_id)
    await callback.answer()
    try:
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
                f"⚠️ Код сгенерирован, но бот не запустился.\n\n<code>{html.escape(str(e)[-300:])}</code>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="🔄 Перегенерировать снова", callback_data=f"recreate:{bot_id}"),
                    InlineKeyboardButton(text="🗑 Удалить", callback_data=f"delete:{bot_id}"),
                ]]),
            )
    finally:
        _busy_bots.discard(bot_id)


# ── auto-diagnose ─────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("autofix:"))
async def cb_auto_diagnose(callback: CallbackQuery):
    if not _is_owner(callback.from_user.id):
        await _deny_callback(callback)
        return
    bot_id = int(callback.data.split(":")[1])
    if bot_id in _busy_bots:
        await callback.answer(_BUSY_TEXT, show_alert=True)
        return
    _busy_bots.add(bot_id)
    await callback.answer()
    try:
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
            + (f"<code>{html.escape(error_log[-300:])}</code>" if error_log else "Логов нет — анализирую код."),
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
                f"⚠️ Код исправлен, но бот снова не запустился:\n<code>{html.escape(str(e)[-300:])}</code>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="🔍 Диагностировать снова", callback_data=f"autofix:{bot_id}"),
                    InlineKeyboardButton(text="🗑 Удалить", callback_data=f"delete:{bot_id}"),
                ]]),
            )
    finally:
        _busy_bots.discard(bot_id)


# ── fix bug ───────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("fixbug:"))
async def cb_fix_bug(callback: CallbackQuery, state: FSMContext):
    if not _is_owner(callback.from_user.id):
        await _deny_callback(callback)
        return
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
    await message.answer(f"🎤 Распознал: <i>{html.escape(text)}</i>", parse_mode="HTML")
    return text


async def _apply_fix(message: Message, state: FSMContext, bug_description: str, bot: Bot) -> None:
    data = await state.get_data()
    bot_id = data["fix_bot_id"]
    await state.clear()

    if bot_id in _busy_bots:
        await message.answer(_BUSY_TEXT)
        return
    _busy_bots.add(bot_id)
    try:
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
                f"⚠️ Код исправлен, но бот не запустился.\n\n<code>{html.escape(str(e)[-300:])}</code>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="🐛 Исправить снова", callback_data=f"fixbug:{bot_id}"),
                    InlineKeyboardButton(text="🗑 Удалить", callback_data=f"delete:{bot_id}"),
                ]]),
            )
    finally:
        _busy_bots.discard(bot_id)


@router.message(FixBotStates.describing_bug, F.voice)
async def msg_fix_voice(message: Message, state: FSMContext, bot: Bot):
    if not _is_owner(message.from_user.id):
        await _deny_message(message)
        return
    text = await _recognize_voice_fix(message, bot)
    if text:
        await _apply_fix(message, state, text, bot)


@router.message(FixBotStates.describing_bug, F.text, ~F.text.startswith("/"))
async def msg_fix_text(message: Message, state: FSMContext, bot: Bot):
    if not _is_owner(message.from_user.id):
        await _deny_message(message)
        return
    await _apply_fix(message, state, message.text, bot)


@router.message(FixBotStates.describing_bug)
async def msg_fix_unsupported(message: Message):
    if not _is_owner(message.from_user.id):
        await _deny_message(message)
        return
    await message.answer("Не понял — отправь текст или голосовое с описанием бага. /cancel — отменить.")
