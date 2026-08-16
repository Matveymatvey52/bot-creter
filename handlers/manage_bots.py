from __future__ import annotations

import asyncio
import functools
import html
import json
import logging
import os
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
    add_office_link,
    add_template_candidate,
    delete_bot,
    disable_bot_feature,
    enable_bot_feature,
    get_all_bots,
    get_bot,
    get_bot_by_name,
    get_bot_features,
    get_bot_payment_provider,
    get_bot_sheets_config,
    get_bot_yookassa_credentials,
    get_bot_yookassa_status_cache,
    get_office_links_for_bot,
    get_owner_payment_credentials,
    remove_office_link,
    set_bot_miniapp_config,
    set_bot_office_hook_config,
    set_bot_payment_provider,
    set_bot_sheets_config,
    set_bot_yookassa_credentials,
    set_bot_yookassa_status_cache,
    set_owner_payment_credentials,
    update_bot_status,
    update_bot_username,
)
from features.sheets import get_service_account_email, verify_access
from handlers.admin_manager import _is_owner
from handlers.create_bot import cancel_keyboard
from runtime.registry import _CUSTOM_FEATURES_DIR, discover_features, infer_template_id, invalidate_custom_feature_cache
from runtime.registry_holder import RegistryHandle
from runtime.webhook_setup import set_miniapp_menu_button
from services.bot_runner import _make_extra_env, get_bot_logs, is_running, start_bot, stop_bot
from services.claude_service import fix_bot_code, generate_bot_code, improve_bot_code
from services.github_sync import push_bot_to_github
from services.voice_service import transcribe_voice
from services.yookassa_api import YooKassaAuthError, fetch_shop_info


logger = logging.getLogger(__name__)

_DENY_TEXT = "⛔ Управление ботами доступно только владельцу."
_BUSY_TEXT = "⏳ Для этого бота уже выполняется операция — подожди её завершения."

# Guards recreate/autofix/fixbug (expensive Claude calls + file writes) against
# a double click starting a second call for the same bot before the first finishes.
_busy_bots: set[int] = set()

# The live webhook Registry, set once by runtime/combined_app.py's bootstrap —
# same pattern as handlers/create_bot.py's own RegistryHandle/set_registry().
# Needed so toggling a feature can call reload_one(bot_id) and have it take
# effect immediately, instead of only on the next full registry rebuild.
_registry_handle = RegistryHandle()


def set_registry(registry) -> None:
    _registry_handle.set(registry)


class FixBotStates(StatesGroup):
    describing_bug = State()


class SheetsConnectFlow(StatesGroup):
    waiting_for_link = State()


class PaymentConnectFlow(StatesGroup):
    browsing_step = State()
    waiting_for_token = State()
    waiting_for_shop_id = State()
    waiting_for_secret_key = State()

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
        InlineKeyboardButton(text="🕘 История доработок", callback_data=f"customfeaturehistory:{bot_id}"),
    ])
    rows.append([
        InlineKeyboardButton(text="🏢 Офисы", callback_data=f"office:{bot_id}"),
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
    bot_id: int,
    compatible: list[dict],
    enabled: set[str],
    sheets_config: dict | None = None,
    has_yookassa_creds: bool = False,
) -> InlineKeyboardMarkup:
    rows = []
    for feature in compatible:
        name = feature["name"]
        icon = "✅" if name in enabled else "⬜"
        rows.append([
            InlineKeyboardButton(text=f"{icon} {name}", callback_data=f"togglefeature:{bot_id}:{name}"),
        ])
        # "sheets" needs a spreadsheet_id per bot beyond a plain on/off toggle —
        # this second row only appears once the feature itself is enabled.
        if name == "payments" and name in enabled:
            rows.append([
                InlineKeyboardButton(text="💳 Как подключить оплату", callback_data=f"paystart:{bot_id}")
            ])
            if has_yookassa_creds:
                rows.append([
                    InlineKeyboardButton(text="🔄 Проверить статус ЮKassa", callback_data=f"paycheck:{bot_id}")
                ])
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


async def _reload_registry(bot_id: int) -> None:
    if _registry_handle.value is not None:
        await _registry_handle.value.reload_one(bot_id)
    else:
        logger.debug(f"_reload_registry: no live registry available — bot_id={bot_id} DB-only change")


async def enable_feature_and_reload(bot_id: int, feature_name: str) -> None:
    """Enables `feature_name` for `bot_id` and reloads it in the live registry —
    the exact state-change cb_toggle_feature's "turn on" branch performs. Shared
    by cb_toggle_feature (button path) and handlers/feature_connect.py's confirm
    callback (conversational path) so enabling a feature does the identical thing
    regardless of which entry point triggered it — not two independent copies."""
    await enable_bot_feature(bot_id, feature_name)
    await _reload_registry(bot_id)


async def disable_feature_and_reload(bot_id: int, feature_name: str) -> None:
    """Symmetric with enable_feature_and_reload — extracted from cb_toggle_feature's
    "turn off" branch for the same reason (single implementation, not duplicated)."""
    await disable_bot_feature(bot_id, feature_name)
    await _reload_registry(bot_id)


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
    has_yookassa_creds = bool(await get_bot_yookassa_credentials(bot_id)) if "payments" in enabled else False
    text = "🧩 <b>Фичи для этого бота</b> — нажми, чтобы включить/выключить:"
    if not compatible:
        text = "🧩 Для этого бота пока нет доступных фич (нет совместимых по шаблону)."
    await _edit_or_resend(
        callback,
        text,
        parse_mode="HTML",
        reply_markup=_features_keyboard(bot_id, compatible, enabled, sheets_config, has_yookassa_creds),
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
            await disable_feature_and_reload(bot_id, feature_name)
        else:
            await enable_feature_and_reload(bot_id, feature_name)
        await callback.answer("✅ Включено" if not is_enabled else "🔴 Выключено")
        compatible = await _compatible_features(template_id)
        new_enabled = set(await get_bot_features(bot_id))
        new_sheets_config = await get_bot_sheets_config(bot_id) if "sheets" in new_enabled else None
        new_has_yookassa_creds = (
            bool(await get_bot_yookassa_credentials(bot_id)) if "payments" in new_enabled else False
        )
        await _edit_or_resend(
            callback,
            "🧩 <b>Фичи для этого бота</b> — нажми, чтобы включить/выключить:",
            parse_mode="HTML",
            reply_markup=_features_keyboard(bot_id, compatible, new_enabled, new_sheets_config, new_has_yookassa_creds),
        )
    finally:
        _busy_bots.discard(bot_id)


_SPREADSHEET_ID_RE = re.compile(r"/d/([a-zA-Z0-9_-]{20,})")
_BARE_SPREADSHEET_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{20,}$")


# ── "Офисы" (docs/OFFICES_DESIGN.md §9) ─────────────────────────────────────
# Only event_type in _EVENT_TYPES right now is "order.created" — office:{bot_id}
# always subscribes the OTHER bot to notifications FROM this bot (X → Y, this
# bot X is always source), no event-type picker shown yet. When a second
# event_type is added to features/office_events.py, insert a picker step
# between officeconnect and the add_office_link() call below rather than
# reworking this flow — see §9's "Ограничение первой итерации" note.
_OFFICE_EVENT_TYPE = "order.created"


async def _office_panel_text_and_keyboard(bot_id: int) -> tuple[str, InlineKeyboardMarkup]:
    links = await get_office_links_for_bot(bot_id)
    rows: list[list[InlineKeyboardButton]] = []
    if not links:
        text = "🏢 <b>Офисы</b>\n\nПока нет связей с другими ботами."
    else:
        lines = ["🏢 <b>Офисы</b>\n\nТекущие связи:"]
        for link in links:
            source_id, target_id, event_type = link["source_bot_id"], link["target_bot_id"], link["event_type"]
            if source_id == bot_id:
                source_bot = await get_bot(bot_id)
                target_bot = await get_bot(target_id)
                arrow_label = f"📤 {source_bot['name'] if source_bot else bot_id} → {target_bot['name'] if target_bot else target_id}"
            else:
                source_bot = await get_bot(source_id)
                target_bot = await get_bot(bot_id)
                arrow_label = f"📥 {source_bot['name'] if source_bot else source_id} → {target_bot['name'] if target_bot else bot_id}"
            lines.append(f"{arrow_label} ({event_type})")
            rows.append([
                InlineKeyboardButton(
                    text=f"❌ {arrow_label}",
                    callback_data=f"officeunlink:{source_id}:{target_id}:{event_type}:{bot_id}",
                )
            ])
        text = "\n".join(lines)
    rows.append([InlineKeyboardButton(text="➕ Подключить к другому боту", callback_data=f"officeconnect:{bot_id}")])
    rows.append([InlineKeyboardButton(text="◀ Назад", callback_data=f"info:{bot_id}")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("office:"))
async def cb_office_panel(callback: CallbackQuery):
    if not _is_owner(callback.from_user.id):
        await _deny_callback(callback)
        return
    await callback.answer()
    bot_id = int(callback.data.split(":")[1])
    b = await get_bot(bot_id)
    if not b:
        await callback.message.answer("Бот не найден.")
        return
    text, keyboard = await _office_panel_text_and_keyboard(bot_id)
    await _edit_or_resend(callback, text, parse_mode="HTML", reply_markup=keyboard)


@router.callback_query(F.data.startswith("officeconnect:"))
async def cb_office_connect_start(callback: CallbackQuery):
    if not _is_owner(callback.from_user.id):
        await _deny_callback(callback)
        return
    await callback.answer()
    bot_id = int(callback.data.split(":")[1])
    b = await get_bot(bot_id)
    if not b:
        await callback.message.answer("Бот не найден.")
        return
    other_bots = [ob for ob in await get_all_bots() if ob["id"] != bot_id]
    if not other_bots:
        await _edit_or_resend(
            callback,
            "🏢 Других ботов пока нет — сначала создай ещё одного, чтобы объединить их в офис.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀ Назад", callback_data=f"office:{bot_id}")]
            ]),
        )
        return
    rows = [
        [InlineKeyboardButton(text=ob["name"], callback_data=f"officelink:{bot_id}:{ob['id']}")]
        for ob in other_bots
    ]
    rows.append([InlineKeyboardButton(text="◀ Назад", callback_data=f"office:{bot_id}")])
    await _edit_or_resend(
        callback,
        f"🏢 Выбери бота, которого «{b['name']}» будет уведомлять о новых заказах:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("officelink:"))
async def cb_office_link(callback: CallbackQuery):
    if not _is_owner(callback.from_user.id):
        await _deny_callback(callback)
        return
    _, source_id_str, target_id_str = callback.data.split(":")
    source_id, target_id = int(source_id_str), int(target_id_str)
    await callback.answer()
    # Review finding: officeconnect:'s own keyboard already excludes bot_id
    # from the target list, but that's a UI-layer filter, not a guarantee —
    # this handler must not rely on it. A self-link would make publish_event()
    # hand a bot's own order.created straight back to itself as though it
    # came from another bot, which office_events' whole contract assumes
    # never happens (source_bot_id always means an OTHER bot).
    if source_id == target_id:
        await callback.message.answer("⚠️ Нельзя объединить бота с самим собой.")
        return
    source_bot = await get_bot(source_id)
    target_bot = await get_bot(target_id)
    if not source_bot or not target_bot:
        await callback.message.answer("Бот не найден.")
        return
    await add_office_link(source_id, target_id, _OFFICE_EVENT_TYPE)
    text, keyboard = await _office_panel_text_and_keyboard(source_id)
    await _edit_or_resend(
        callback,
        f"✅ «{source_bot['name']}» теперь уведомляет «{target_bot['name']}» о новых заказах.\n\n{text}",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


@router.callback_query(F.data.startswith("officeunlink:"))
async def cb_office_unlink(callback: CallbackQuery):
    if not _is_owner(callback.from_user.id):
        await _deny_callback(callback)
        return
    # maxsplit=4: event_type is the only free-form-looking segment here, but
    # _EVENT_TYPES (features/office_events.py) is a closed, developer-owned
    # set with no ':' in any current key — still, an unbounded split() would
    # raise ValueError (unhandled — no answer to the user) the day a future
    # event_type contains ':'. Bounding it here keeps parsing correct
    # regardless of what event_type itself contains.
    _, source_id_str, target_id_str, event_type, panel_bot_id_str = callback.data.split(":", 4)
    source_id, target_id, panel_bot_id = int(source_id_str), int(target_id_str), int(panel_bot_id_str)
    await callback.answer()
    panel_bot = await get_bot(panel_bot_id)
    if not panel_bot:
        await callback.message.answer("Бот не найден.")
        return
    await remove_office_link(source_id, target_id, event_type)
    text, keyboard = await _office_panel_text_and_keyboard(panel_bot_id)
    await _edit_or_resend(callback, text, parse_mode="HTML", reply_markup=keyboard)


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
    "Чтобы бот мог читать и писать в таблицу, дай ему доступ:\n\n"
    "1. Открой свою Google Таблицу → «Настройки доступа» → «Добавить пользователей».\n"
    "2. Вставь этот email, выдай роль «Редактор»:\n"
    "<code>{sa_email}</code>\n"
    "3. Пришли сюда ссылку на таблицу."
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
    has_yookassa_creds = bool(await get_bot_yookassa_credentials(bot_id)) if "payments" in enabled else False
    await edit_target.answer(
        "🧩 <b>Фичи для этого бота</b> — нажми, чтобы включить/выключить:",
        parse_mode="HTML",
        reply_markup=_features_keyboard(bot_id, compatible, enabled, sheets_config, has_yookassa_creds),
    )


async def begin_sheets_connect(bot_id: int, state: FSMContext, present) -> bool:
    """Starts the sheets-link collection step: sends the SA-email instructions and
    puts the FSM into SheetsConnectFlow.waiting_for_link with bot_id stored — the
    exact state msg_sheets_connect_link (below) expects. `present` is an
    async (text, **kwargs) -> None sink (matches the existing _send_list/_send_logs
    convention in this module), so the caller controls whether that lands as an
    edited message (button path, via functools.partial(_edit_or_resend, callback))
    or a fresh one (conversational path, via message.answer). Shared by
    cb_sheets_connect_start (button path) and handlers/feature_connect.py's confirm
    callback (conversational path) — both reach the SAME FSM state through the SAME
    function, so the next message either path is followed by is handled by the one
    existing msg_sheets_connect_link handler, not a second implementation.

    Returns False (message sent, FSM untouched) if sheets isn't configured
    server-side — same guard cb_sheets_connect_start always had."""
    sa_email = await get_service_account_email()
    if not sa_email:
        await present(
            "⚠️ Фича sheets не настроена на сервере (нет GOOGLE_SHEETS_SA_KEY_PATH). "
            "Обратитесь к администратору фабрики."
        )
        return False
    await state.set_state(SheetsConnectFlow.waiting_for_link)
    await state.update_data(bot_id=bot_id)
    await present(
        _SHEETS_CONNECT_TEXT_TEMPLATE.format(sa_email=sa_email),
        parse_mode="HTML",
        reply_markup=_sheets_cancel_keyboard(bot_id),
    )
    return True


@router.callback_query(F.data.startswith("sheetsconnect:"))
async def cb_sheets_connect_start(callback: CallbackQuery, state: FSMContext):
    if not _is_owner(callback.from_user.id):
        await _deny_callback(callback)
        return
    await callback.answer()
    bot_id = int(callback.data.split(":")[1])
    await begin_sheets_connect(bot_id, state, functools.partial(_edit_or_resend, callback))


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


# ── payment provider onboarding wizard ──────────────────────────────────────
# Telegram's Bot API has no method to programmatically attach a payment
# provider to a bot — the provider_token can only be issued by BotFather after
# the owner manually picks a provider there, which forwards them into that
# provider's own bot (e.g. @YooKassaBot) to confirm. Nothing here bypasses
# that; this wizard only removes the confusion around *which* buttons to
# press, and replaces the factory-side-only DB write set_bot_payment_provider
# used to require with a validated owner-facing flow.

_PROVIDER_TOKEN_RE = re.compile(r"^\d+:(TEST|LIVE):.{1,256}$")

_PAYMENT_STEP_1_TEXT = (
    "💳 <b>Подключение оплаты (Telegram Payments)</b>\n\n"
    "Шаг 1 из 4 — Регистрация у платёжного провайдера\n\n"
    "Если у тебя ещё нет магазина в ЮKassa — зарегистрируй его по кнопке ниже. "
    "Понадобятся данные ИП/самозанятости/юрлица — это требование платёжного "
    "законодательства, тут это не обойти."
)
_PAYMENT_STEP_2_TEXT = (
    "💳 Шаг 2 из 4 — Открой BotFather\n\n"
    "В открывшемся чате отправь:\n"
    "<code>/mybots</code> → выбери своего бота → <b>Bot Settings</b> → <b>Payments</b>."
)
_PAYMENT_STEP_3_TEXT = (
    "💳 Шаг 3 из 4 — Выбери провайдера в списке\n\n"
    "В списке провайдеров выбери <b>YooKassa</b> — BotFather откроет чат с "
    "@YooKassaBot. Войди там в свой аккаунт ЮKassa и подтверди — он пришлёт "
    "тебе токен вида <code>381764678:TEST:...</code> или <code>381764678:LIVE:...</code>."
)
_PAYMENT_STEP_4_TEXT = (
    "💳 Шаг 4 из 4 — Вставь токен сюда\n\n"
    "Скопируй токен, который прислал @YooKassaBot, и пришли его сюда сообщением."
)


def _payment_step_keyboard(bot_id: int, step: int) -> InlineKeyboardMarkup:
    rows = []
    if step == 1:
        rows.append([InlineKeyboardButton(text="🔗 Зарегистрироваться в ЮKassa", url="https://yookassa.ru/joinups")])
    if step == 2:
        rows.append([InlineKeyboardButton(text="🔗 Открыть BotFather", url="https://t.me/BotFather")])
    nav = []
    if step > 1:
        nav.append(InlineKeyboardButton(text="◀ Назад", callback_data=f"paystep:{bot_id}:{step - 1}"))
    if step < 4:
        nav.append(InlineKeyboardButton(text="Далее ▶️", callback_data=f"paystep:{bot_id}:{step + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data=f"paycancel:{bot_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


_PAYMENT_STEP_TEXTS = {
    1: _PAYMENT_STEP_1_TEXT,
    2: _PAYMENT_STEP_2_TEXT,
    3: _PAYMENT_STEP_3_TEXT,
    4: _PAYMENT_STEP_4_TEXT,
}


@router.callback_query(F.data.startswith("paystart:"))
async def cb_payment_connect_start(callback: CallbackQuery, state: FSMContext):
    if not _is_owner(callback.from_user.id):
        await _deny_callback(callback)
        return
    await callback.answer()
    bot_id = int(callback.data.split(":")[1])
    if not await get_bot(bot_id):
        await callback.message.answer("Бот не найден.")
        return
    # Set from the very first screen (not just at the final text-collecting
    # step) so bot_id in FSM data is state-guarded throughout steps 1-3 too —
    # matches SheetsConnectFlow's single-state-from-the-start design instead
    # of leaving an unguarded window where another flow could silently
    # clobber this wizard's stashed bot_id.
    await state.set_state(PaymentConnectFlow.browsing_step)
    await state.update_data(bot_id=bot_id)
    await _edit_or_resend(
        callback,
        _PAYMENT_STEP_1_TEXT,
        parse_mode="HTML",
        reply_markup=_payment_step_keyboard(bot_id, 1),
    )


@router.callback_query(F.data.startswith("paystep:"))
async def cb_payment_connect_step(callback: CallbackQuery, state: FSMContext):
    if not _is_owner(callback.from_user.id):
        await _deny_callback(callback)
        return
    await callback.answer()
    _, bot_id_str, step_str = callback.data.split(":", 2)
    bot_id, step = int(bot_id_str), int(step_str)
    if step not in _PAYMENT_STEP_TEXTS:
        # Malformed/tampered/stale callback_data (e.g. an old inline keyboard
        # surviving a future step-range change) — ignore rather than crash on
        # a dict lookup or route through invalid FSM data.
        return
    if not await get_bot(bot_id):
        await state.clear()
        await callback.message.answer("Бот не найден.")
        return
    if step == 4:
        # Only the final step actually collects free text.
        await state.set_state(PaymentConnectFlow.waiting_for_token)
    else:
        await state.set_state(PaymentConnectFlow.browsing_step)
    await state.update_data(bot_id=bot_id)
    await _edit_or_resend(
        callback,
        _PAYMENT_STEP_TEXTS[step],
        parse_mode="HTML",
        reply_markup=_payment_step_keyboard(bot_id, step),
    )


@router.callback_query(F.data.startswith("paycancel:"))
async def cb_payment_connect_cancel(callback: CallbackQuery, state: FSMContext):
    if not _is_owner(callback.from_user.id):
        await _deny_callback(callback)
        return
    await callback.answer()
    bot_id = int(callback.data.split(":")[1])
    await state.clear()
    await _back_to_features_panel(bot_id, callback.message)


@router.message(PaymentConnectFlow.waiting_for_token, F.text, ~F.text.startswith("/"))
async def msg_payment_connect_token(message: Message, state: FSMContext):
    if not _is_owner(message.from_user.id):
        return
    data = await state.get_data()
    bot_id = data.get("bot_id")
    if bot_id is None:
        await state.clear()
        return
    token = message.text.strip()
    if not _PROVIDER_TOKEN_RE.match(token):
        await message.answer(
            "⚠️ Не похоже на токен от @YooKassaBot. Ожидается формат вида "
            "<code>381764678:TEST:...</code> или <code>381764678:LIVE:...</code>. "
            "Проверь и пришли ещё раз, либо нажми ❌ Отмена.",
            parse_mode="HTML",
        )
        return
    if bot_id in _busy_bots:
        await message.answer(_BUSY_TEXT)
        return
    _busy_bots.add(bot_id)
    try:
        if not await get_bot(bot_id):
            await state.clear()
            await message.answer("Бот не найден.")
            return
        await set_bot_payment_provider(bot_id, token)
    finally:
        _busy_bots.discard(bot_id)

    # Step 5 (optional): if the owner already has shop_id/secret_key on file from a
    # previous bot, offer to reuse them straight away instead of re-asking — this is
    # the "(б) переиспользование" half of the improvement; the actual provider_token
    # above still had to go through BotFather manually, no way around that.
    owner_creds = await get_owner_payment_credentials(message.from_user.id)
    await state.set_state(PaymentConnectFlow.waiting_for_shop_id)
    await state.update_data(bot_id=bot_id)
    if owner_creds:
        shop_id, _ = owner_creds
        await message.answer(
            "✅ Платёжный провайдер подключён. Бот теперь может принимать оплату.\n\n"
            f"💳 У тебя уже есть сохранённые данные ЮKassa (shopId <code>{html.escape(shop_id)}</code>). "
            "Использовать их для этого бота тоже, или ввести другие?",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Использовать те же", callback_data=f"payreuse:{bot_id}")],
                [InlineKeyboardButton(text="✏️ Ввести другие", callback_data=f"payownnew:{bot_id}")],
                [InlineKeyboardButton(text="⏭ Пропустить", callback_data=f"payskip:{bot_id}")],
            ]),
        )
        return
    await message.answer(
        "✅ Платёжный провайдер подключён. Бот теперь может принимать оплату.\n\n"
        "💳 Хочешь, чтобы бот сам подтянул детали магазина (методы оплаты, тестовый/боевой "
        "режим) через API ЮKassa? Пришли <b>shopId</b> магазина (числовой ID из личного "
        "кабинета ЮKassa, раздел Интеграция → API), либо нажми «Пропустить».",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⏭ Пропустить", callback_data=f"payskip:{bot_id}")],
        ]),
    )


@router.message(PaymentConnectFlow.waiting_for_token)
async def msg_payment_connect_invalid(message: Message) -> None:
    if not _is_owner(message.from_user.id):
        return
    await message.answer("Пришли токен от @YooKassaBot текстом, либо нажми ❌ Отмена.")


# ── step 5/6: optional ЮKassa API credentials (shopId + secret key) ─────────
# Separate from provider_token above — see services/yookassa_api.py. Purely
# additive: skipping this leaves payments fully working exactly as before,
# it only unlocks the auto-fetched-details and status-check conveniences.

@router.callback_query(F.data.startswith("payskip:"))
async def cb_payment_details_skip(callback: CallbackQuery, state: FSMContext):
    if not _is_owner(callback.from_user.id):
        await _deny_callback(callback)
        return
    await callback.answer()
    bot_id = int(callback.data.split(":")[1])
    await state.clear()
    await _back_to_features_panel(bot_id, callback.message)


@router.callback_query(F.data.startswith("payownnew:"))
async def cb_payment_details_own_new(callback: CallbackQuery, state: FSMContext):
    if not _is_owner(callback.from_user.id):
        await _deny_callback(callback)
        return
    await callback.answer()
    bot_id = int(callback.data.split(":")[1])
    await state.set_state(PaymentConnectFlow.waiting_for_shop_id)
    await state.update_data(bot_id=bot_id)
    await callback.message.answer(
        "Пришли <b>shopId</b> магазина (числовой ID из личного кабинета ЮKassa, "
        "раздел Интеграция → API).",
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("payreuse:"))
async def cb_payment_details_reuse(callback: CallbackQuery, state: FSMContext):
    if not _is_owner(callback.from_user.id):
        await _deny_callback(callback)
        return
    await callback.answer()
    bot_id = int(callback.data.split(":")[1])
    if bot_id not in _busy_bots:
        _busy_bots.add(bot_id)
        try:
            if not await get_bot(bot_id):
                await state.clear()
                await callback.message.answer("Бот не найден.")
                return
            owner_creds = await get_owner_payment_credentials(callback.from_user.id)
            if not owner_creds:
                await callback.message.answer("Сохранённые данные не найдены, введи shopId заново.")
                await state.set_state(PaymentConnectFlow.waiting_for_shop_id)
                await state.update_data(bot_id=bot_id)
                return
            shop_id, secret_key = owner_creds
            await _apply_yookassa_credentials(callback.message, bot_id, shop_id, secret_key)
        finally:
            _busy_bots.discard(bot_id)
    await state.clear()
    await _offer_multi_bot_connect(callback.message, bot_id)


@router.message(PaymentConnectFlow.waiting_for_shop_id, F.text, ~F.text.startswith("/"))
async def msg_payment_shop_id(message: Message, state: FSMContext):
    if not _is_owner(message.from_user.id):
        return
    data = await state.get_data()
    bot_id = data.get("bot_id")
    if bot_id is None:
        await state.clear()
        return
    shop_id = message.text.strip()
    if not shop_id.isdigit():
        await message.answer("⚠️ shopId — это число. Проверь в личном кабинете ЮKassa (Интеграция → API) и пришли ещё раз.")
        return
    await state.update_data(bot_id=bot_id, shop_id=shop_id)
    await state.set_state(PaymentConnectFlow.waiting_for_secret_key)
    await message.answer(
        "Теперь пришли <b>secret key</b> (тоже из раздела Интеграция → API — "
        "строка вида <code>live_AbCdEf...</code> или <code>test_AbCdEf...</code>).",
        parse_mode="HTML",
    )


@router.message(PaymentConnectFlow.waiting_for_shop_id)
async def msg_payment_shop_id_invalid(message: Message) -> None:
    if not _is_owner(message.from_user.id):
        return
    await message.answer("Пришли shopId текстом (число), либо нажми ❌ Отмена.")


@router.message(PaymentConnectFlow.waiting_for_secret_key, F.text, ~F.text.startswith("/"))
async def msg_payment_secret_key(message: Message, state: FSMContext):
    if not _is_owner(message.from_user.id):
        return
    data = await state.get_data()
    bot_id = data.get("bot_id")
    shop_id = data.get("shop_id")
    if bot_id is None or shop_id is None:
        await state.clear()
        return
    secret_key = message.text.strip()
    if bot_id in _busy_bots:
        await message.answer(_BUSY_TEXT)
        return
    _busy_bots.add(bot_id)
    try:
        if not await get_bot(bot_id):
            await state.clear()
            await message.answer("Бот не найден.")
            return
        await _apply_yookassa_credentials(message, bot_id, shop_id, secret_key)
    finally:
        _busy_bots.discard(bot_id)
    await state.clear()
    await _offer_multi_bot_connect(message, bot_id)


@router.message(PaymentConnectFlow.waiting_for_secret_key)
async def msg_payment_secret_key_invalid(message: Message) -> None:
    if not _is_owner(message.from_user.id):
        return
    await message.answer("Пришли secret key текстом, либо нажми ❌ Отмена.")


async def _apply_yookassa_credentials(present: Message, bot_id: int, shop_id: str, secret_key: str) -> None:
    """Calls GET /me, saves shop_id/secret_key for this bot AND as the owner's reusable
    default, caches the status/payment_methods snapshot. Any failure here (bad
    credentials, network error) is reported but doesn't roll back provider_token —
    payments already work without this, it's purely the auto-fetched-details layer."""
    try:
        info = await fetch_shop_info(shop_id, secret_key)
    except YooKassaAuthError:
        await present.answer(
            "⚠️ ЮKassa не приняла shopId/secret key — проверь их в личном кабинете и "
            "подключи детали позже через «Проверить статус» в панели фич."
        )
        return
    except Exception:
        logger.exception(f"_apply_yookassa_credentials: GET /me failed for bot_id={bot_id}")
        await present.answer(
            "⚠️ Не удалось связаться с ЮKassa (сеть/сервис недоступен). Данные не сохранены — "
            "попробуй ещё раз позже через «Проверить статус» в панели фич."
        )
        return

    await set_bot_yookassa_credentials(bot_id, shop_id, secret_key)
    await set_owner_payment_credentials(present.from_user.id, shop_id, secret_key)
    await set_bot_yookassa_status_cache(bot_id, str(info.get("status", "unknown")), json.dumps(info.get("payment_methods", [])))
    await present.answer(_format_shop_info(info), parse_mode="HTML")


def _format_shop_info(info: dict) -> str:
    status = info.get("status", "unknown")
    mode = "🧪 тестовый режим" if info.get("test") else "🔴 боевой режим (реальные платежи)"
    methods = info.get("payment_methods") or []
    methods_line = ", ".join(str(m) for m in methods) if methods else "не указаны"
    status_emoji = "✅" if status == "enabled" else "⚠️"
    return (
        f"{status_emoji} Магазин ЮKassa подключён к API.\n"
        f"Статус аккаунта: <code>{html.escape(status)}</code>\n"
        f"Режим: {mode}\n"
        f"Доступные методы оплаты: {html.escape(methods_line)}"
    )


async def _offer_multi_bot_connect(present: Message, bot_id: int) -> None:
    """(б) Мульти-бот привязка: lists the owner's OTHER bots that don't have a
    provider_token yet and offers a per-bot copy-paste BotFather block. Telegram's
    Bot API has no way to set provider_token programmatically (see the module-level
    comment above PaymentConnectFlow's wizard) — this only removes the friction of
    re-typing shop_id/secret_key from memory for every bot's BotFather trip."""
    all_bots = await get_all_bots()
    candidates = []
    for b in all_bots:
        if b["id"] == bot_id:
            continue
        if await get_bot_payment_provider(b["id"]):
            continue
        candidates.append(b)
    if not candidates:
        return
    rows = [
        [InlineKeyboardButton(text=b.get("display_name") or b["name"], callback_data=f"paymulti:{bot_id}:{b['id']}")]
        for b in candidates[:20]
    ]
    rows.append([InlineKeyboardButton(text="Не сейчас", callback_data=f"paymultiskip:{bot_id}")])
    await present.answer(
        "🔗 Подключить эту же оплату к другим твоим ботам?\n\n"
        "Провести токен через BotFather всё равно придётся для каждого бота отдельно "
        "(Telegram этого не автоматизирует), но я подготовлю тебе готовый блок с уже "
        "подставленными shopId и secret key — не нужно искать их заново.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("paymultiskip:"))
async def cb_payment_multi_skip(callback: CallbackQuery):
    if not _is_owner(callback.from_user.id):
        await _deny_callback(callback)
        return
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)


@router.callback_query(F.data.startswith("paymulti:"))
async def cb_payment_multi_connect(callback: CallbackQuery):
    if not _is_owner(callback.from_user.id):
        await _deny_callback(callback)
        return
    await callback.answer()
    _, _source_bot_id_str, target_bot_id_str = callback.data.split(":", 2)
    target_bot_id = int(target_bot_id_str)
    target_bot = await get_bot(target_bot_id)
    if not target_bot:
        await callback.message.answer("Бот не найден.")
        return
    owner_creds = await get_owner_payment_credentials(callback.from_user.id)
    if not owner_creds:
        await callback.message.answer("Сохранённые данные ЮKassa не найдены.")
        return
    shop_id, secret_key = owner_creds
    bot_label = html.escape(target_bot.get("display_name") or target_bot["name"])
    await callback.message.answer(
        f"💳 Подключение оплаты к боту «{bot_label}»\n\n"
        "1️⃣ Открой @BotFather → <code>/mybots</code> → выбери этого бота → "
        "<b>Bot Settings</b> → <b>Payments</b> → <b>YooKassa</b>.\n"
        "2️⃣ В открывшемся чате с @YooKassaBot введи:\n\n"
        f"shopId: <code>{html.escape(shop_id)}</code>\n"
        f"secret key: <code>{html.escape(secret_key)}</code>\n\n"
        "3️⃣ @YooKassaBot пришлёт токен вида <code>381764678:LIVE:...</code> — скопируй "
        "его и вернись в панель фич этого бота, чтобы вставить через «Подключить оплату».",
        parse_mode="HTML",
    )


# ── (в) статус готовности магазина ───────────────────────────────────────────

def _format_status_line(cache: dict | None) -> str:
    if not cache:
        return "Статус ещё не проверялся."
    status = cache.get("last_status") or "unknown"
    checked_at = cache.get("last_checked_at") or "—"
    status_emoji = "✅" if status == "enabled" else "⚠️"
    return f"{status_emoji} Статус: <code>{html.escape(status)}</code> (проверено: {checked_at})"


@router.callback_query(F.data.startswith("paycheck:"))
async def cb_payment_check_status(callback: CallbackQuery):
    """'Проверить статус' button — re-runs GET /me on demand using the bot's saved
    shop_id/secret_key and reports whether the shop is ready to accept payments."""
    if not _is_owner(callback.from_user.id):
        await _deny_callback(callback)
        return
    await callback.answer()
    bot_id = int(callback.data.split(":")[1])
    creds = await get_bot_yookassa_credentials(bot_id)
    if not creds:
        await callback.message.answer(
            "⚠️ Для этого бота не сохранены shopId/secret key ЮKassa — подключи их через "
            "мастер оплаты (⚙️ Подключить оплату), чтобы включить автопроверку статуса."
        )
        return
    shop_id, secret_key = creds
    try:
        info = await fetch_shop_info(shop_id, secret_key)
    except YooKassaAuthError:
        await callback.message.answer("⚠️ ЮKassa отклонила сохранённые shopId/secret key — данные могли устареть.")
        return
    except Exception:
        logger.exception(f"cb_payment_check_status: GET /me failed for bot_id={bot_id}")
        await callback.message.answer("⚠️ Не удалось связаться с ЮKassa. Попробуй ещё раз чуть позже.")
        return
    await set_bot_yookassa_status_cache(bot_id, str(info.get("status", "unknown")), json.dumps(info.get("payment_methods", [])))
    await callback.message.answer(_format_shop_info(info), parse_mode="HTML")


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

        regenerating_from_scratch = not current_code
        if current_code:
            await callback.message.edit_text(f"✨ Улучшаю код <b>{b['name']}</b>...", parse_mode="HTML")
            task = improve_bot_code(current_code, b.get("description", ""))
        else:
            await callback.message.edit_text(f"🔧 Генерирую код для <b>{b['name']}</b>...", parse_mode="HTML")
            task = generate_bot_code(b.get("description", ""))

        miniapp_config: dict | None = None
        office_hook_config: dict | None = None
        fallback_info: dict | None = None
        try:
            result = await asyncio.wait_for(task, timeout=240.0)
            if regenerating_from_scratch:
                code, miniapp_config, office_hook_config, fallback_info = result
            else:
                code = result
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

        if office_hook_config:
            await set_bot_office_hook_config(bot_id, office_hook_config)
        if fallback_info:
            await add_template_candidate(
                creator_user_id=callback.from_user.id,
                summary=b.get("description", ""),
                fallback_reason=fallback_info["reason"],
                selected_templates=fallback_info["selected_templates"],
                bot_name=b["name"],
                bot_id=bot_id,
            )
        if miniapp_config:
            await set_bot_miniapp_config(bot_id, miniapp_config)
            base_url = os.getenv("PUBLIC_BASE_URL", "").strip()
            if base_url and b.get("token"):
                try:
                    await set_miniapp_menu_button(b["token"], base_url, bot_id)
                except Exception as e:
                    logger.error(f"Bot id={bot_id} regenerated but Menu Button setup failed: {e}")

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
        "Опиши баг или что нужно улучшить — голосовым или текстом.",
        parse_mode="HTML",
        reply_markup=cancel_keyboard(),
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
    await message.answer(
        "Не понял — отправь текст или голосовое с описанием бага.",
        reply_markup=cancel_keyboard(),
    )
