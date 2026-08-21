from __future__ import annotations

import asyncio
import functools
import hashlib
import html
import json
import logging
import os
import re
import tempfile
import time
import uuid
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import ASSEMBLYAI_API_KEY
from db.database import (
    add_miniapp_config_failure,
    add_office_link,
    add_template_candidate,
    delete_bot,
    disable_bot_feature,
    enable_bot_feature,
    get_all_bots,
    get_bot,
    get_bot_by_name,
    get_bot_features,
    get_bot_cloudpayments_credentials,
    get_bot_payment_provider,
    get_bot_provider_type,
    get_bot_sheets_config,
    get_bot_yookassa_credentials,
    get_bot_yookassa_status_cache,
    get_group_task_config,
    get_office_links_for_bot,
    get_owner_payment_credentials,
    delete_group_task_config,
    remove_office_link,
    set_bot_miniapp_config,
    set_bot_office_hook_config,
    set_bot_payment_provider,
    set_bot_provider_type,
    set_bot_sheets_config,
    set_bot_voice_cashflow_config,
    set_bot_yookassa_credentials,
    set_bot_yookassa_status_cache,
    set_owner_payment_credentials,
    update_bot_status,
    update_bot_username,
)
from features import ai_dialog
from features.office_events import _EVENT_TYPES, EVENT_TYPE_LABELS
from features.sheets import get_service_account_email, verify_access
from handlers.admin_manager import _can_manage_bot, _is_owner
from handlers.create_bot import cancel_keyboard
from runtime.registry import (
    _CUSTOM_FEATURES_DIR,
    discover_features,
    infer_template_id,
    invalidate_custom_feature_cache,
    is_template_backed,
)
from runtime.registry_holder import RegistryHandle
from runtime.webhook_setup import set_miniapp_menu_button
from services.bot_runner import _make_extra_env, get_bot_logs, is_running, start_bot, stop_bot
from services.claude_service import (
    append_from_scratch_registry_wiring,
    explain_bug_fix_diff,
    fix_bot_code,
    generate_bot_code,
    improve_bot_code,
)
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
    previewing_fix = State()


class SheetsConnectFlow(StatesGroup):
    waiting_for_link = State()


class PaymentConnectFlow(StatesGroup):
    browsing_step = State()
    waiting_for_token = State()
    waiting_for_shop_id = State()
    waiting_for_secret_key = State()


class CloudpaymentsConnectFlow(StatesGroup):
    """Separate, much shorter flow from PaymentConnectFlow above — Cloudpayments
    needs only public_id + api_secret (no BotFather trip, no Telegram
    provider_token), so it doesn't share PaymentConnectFlow's 4-step wizard
    states. See cb_paychoose_cloudpayments for the entry point."""
    waiting_for_public_id = State()
    waiting_for_api_secret = State()


class AiDialogStates(StatesGroup):
    chatting = State()

router = Router()


# ── helpers ──────────────────────────────────────────────────────────────────

async def _deny_message(message: Message) -> None:
    await message.answer(_DENY_TEXT)


async def _deny_callback(callback: CallbackQuery) -> None:
    await callback.answer(_DENY_TEXT, show_alert=True)


def _visible_bots(user_id: int, bots: list[dict]) -> list[dict]:
    """Filters an enumerate-all-bots list down to what user_id may see —
    every bot for the owner, only their own bot(s) for a customer (Stage 1
    multitenancy: never let a customer discover another customer's bot
    through a picker, even read-only)."""
    if _is_owner(user_id):
        return bots
    return [b for b in bots if _can_manage_bot(user_id, b)]


async def _bot_or_deny(user_id: int, bot_id: int) -> dict | None:
    """Fetches bot_id and checks per-bot authorization — returns the bot row
    if user_id may manage it, else None (caller decides not-found vs
    forbidden messaging; most callers show a generic "Бот не найден."
    either way, matching this module's existing not-found messaging so a
    customer probing another owner's bot_id learns nothing beyond "not
    found")."""
    b = await get_bot(bot_id)
    if not b or not _can_manage_bot(user_id, b):
        return None
    return b


def _hash_bot_code(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _fix_preview_keyboard(bot_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Применить", callback_data=f"applyfix:{bot_id}"),
        InlineKeyboardButton(text="❌ Отмена", callback_data=f"cancelfix:{bot_id}"),
    ]])


def _fix_retry_keyboard(bot_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🐛 Попробовать снова", callback_data=f"fixbug:{bot_id}"),
        InlineKeyboardButton(text="◀ Назад", callback_data=f"info:{bot_id}"),
    ]])


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
    # Deliberately the very first row, alone, with its own distinct wording
    # (not another "🧩"/"🔧" icon among the grid below) — this is the new
    # free-form voice/text entry point (features/ai_dialog.py) into the SAME
    # actions the rest of this keyboard exposes as separate buttons, so it
    # needs to visually read as "a different kind of control", not just one
    # more row in the list.
    rows.append([
        InlineKeyboardButton(text="✨🎙️ Хочу наговорить/написать что изменить", callback_data=f"aidialog:{bot_id}"),
    ])
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
        InlineKeyboardButton(text="💬 Рабочая группа", callback_data=f"grouptask:{bot_id}"),
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
    bots = _visible_bots(message.from_user.id, await get_all_bots())
    if not bots:
        await message.answer("Ботов пока нет. Создай первого командой /create")
        return
    await _send_list(message.answer, bots)


# ── /stop ─────────────────────────────────────────────────────────────────────

@router.message(Command("stop"))
async def cmd_stop(message: Message):
    bots = _visible_bots(message.from_user.id, await get_all_bots())
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
    bots = _visible_bots(message.from_user.id, await get_all_bots())
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
    # Logs stay owner-only, matching runtime/factory_analytics_api.py's
    # bot_logs_handler (crash logs can contain internals a customer
    # shouldn't need to debug their own bot through raw stack traces).
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
    await callback.answer()
    chat_id = callback.message.chat.id
    try:
        await callback.message.delete()
    except Exception:
        pass
    bots = _visible_bots(callback.from_user.id, await get_all_bots())
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
    b = await _bot_or_deny(callback.from_user.id, bot_id)
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


# Human-readable Russian names for features/*.py's "# FEATURE: <name>" keys —
# owner-facing (Telegram "🧩 Фичи" panel here, and its miniapp counterpart in
# miniapp/src/screens/BotDetailPanel.tsx's own FEATURE_LABELS, kept in sync by
# hand since the two live in separate languages/runtimes). A key missing here
# falls back to the raw feature name, same fallback miniapp/src/screens/
# BotDetailPanel.tsx's featureLabel() uses.
_FEATURE_LABELS: dict[str, str] = {
    "payments": "Приём платежей",
    "sheets": "Google-таблицы",
    "notifications": "Рассылки",
    "office_events": "Обмен между ботами",
    "reminders": "Напоминания",
    "sales_analytics": "Аналитика продаж",
    "voice_intake": "Голосовой ввод",
    "sellable_items": "Каталог товаров",
    "cashflow_ledger": "Учёт денег (ДДС)",
    "excel_export": "Экспорт в Excel",
    "word_export": "Экспорт в Word",
    "group_task": "Групповые задачи",
    "channel_monitor": "Мониторинг каналов",
    "bot_feedback_entries": "Отзывы клиентов",
}


def _feature_label(name: str) -> str:
    return _FEATURE_LABELS.get(name, name)


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
            InlineKeyboardButton(
                text=f"{icon} {_feature_label(name)}", callback_data=f"togglefeature:{bot_id}:{name}"
            ),
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


# ── core actions used by the factory REST API (runtime/factory_analytics_api.py) ────
#
# recreate_bot_core / autofix_bot_core mirror what cb_recreate / cb_auto_diagnose
# do for the Telegram flow (each keeps its own separate implementation for the
# chat-message-editing version) — these return a plain dict instead of editing
# a callback.message. generate_fix_preview_core / apply_fix_core are the
# web-API counterpart of the Telegram fixbug flow's own
# _generate_and_preview_fix / cb_apply_fix split (same generate-then-apply
# shape, same main_code_hash freshness guard against a stale base file — see
# apply_fix_core's docstring). Callers are responsible for the _busy_bots
# guard — these functions assume the caller already holds it.

class TemplateBackedBotError(RuntimeError):
    """A code-rewrite path tried to overwrite a shared templates/<id>.py."""


TEMPLATE_BACKED_DENIED = (
    "🔒 Этот бот работает на общем шаблоне, а не на своей копии кода.\n\n"
    "Перегенерация и авто-правки для него отключены: они переписали бы сам "
    "шаблон, на котором держатся все остальные боты этого типа, и отправили "
    "бы эту версию в GitHub.\n\n"
    "Остальные действия — фичи, доработки, настройки — работают как обычно."
)


async def write_bot_code(bot: dict, code: str) -> None:
    """The ONE place a bot's source is written to disk and pushed to GitHub.

    Refuses outright for a template-backed bot. Every rewrite path here runs
    LLM output through this: /recreate, autofix, and both halves of fixbug.
    For a bot whose file IS templates/<id>.py that output would replace the
    hand-authored template — for every bot built on it, not just this one —
    and push the replacement (see the "🔍 Авто-диагностика" path, which can
    fire off a crashed bot without anyone pressing "regenerate").

    Raises rather than returning a flag on purpose: six callers with three
    different return conventions (dict / tuple / message edit) share this, and
    a forgotten flag check would silently let the write through. An exception
    cannot be ignored by accident. Callers each translate it into their own
    shape — and each also refuses EARLY, before spending a Claude call, so
    this raise is the safety net rather than the working path.
    """
    if is_template_backed(bot.get("file_path")):
        raise TemplateBackedBotError(bot.get("name"))
    Path(bot["file_path"]).write_text(code, encoding="utf-8")
    asyncio.create_task(push_bot_to_github(bot["name"], code))


async def recreate_bot_core(bot_id: int, creator_user_id: int) -> dict:
    """Returns {"ok": bool, "error": str|None, "bot_name": str|None}. Mirrors
    cb_recreate's full sequence: improve existing code or generate from
    scratch, re-append office-hook wiring, write the file, push to GitHub,
    persist any generated configs, restart the bot."""
    b = await get_bot(bot_id)
    if not b:
        return {"ok": False, "error": "not_found", "bot_name": None}
    if not b.get("description"):
        return {"ok": False, "error": "no_description", "bot_name": b["name"]}
    # Refused here rather than at write_bot_code below: otherwise we would burn
    # a 240s Claude call and its tokens to produce code we are going to throw
    # away. write_bot_code stays as the guarantee.
    if is_template_backed(b.get("file_path")):
        return {"ok": False, "error": "template_backed", "bot_name": b["name"]}

    current_code = ""
    if b.get("file_path"):
        try:
            current_code = Path(b["file_path"]).read_text(encoding="utf-8")
        except Exception:
            pass

    regenerating_from_scratch = not current_code
    task = (
        improve_bot_code(current_code, b.get("description", ""))
        if current_code
        else generate_bot_code(b.get("description", ""))
    )

    miniapp_config: dict | None = None
    office_hook_config: dict | None = None
    voice_cashflow_config: dict | None = None
    fallback_info: dict | None = None
    miniapp_failure_info: dict | None = None
    try:
        result = await asyncio.wait_for(task, timeout=240.0)
        if regenerating_from_scratch:
            code, miniapp_config, office_hook_config, voice_cashflow_config, fallback_info, miniapp_failure_info = result
        else:
            code = result
    except Exception as e:
        logger.error(f"recreate_bot_core: generation failed for bot_id={bot_id}: {e}")
        return {"ok": False, "error": "generation_failed", "bot_name": b["name"]}

    await stop_bot(bot_id)
    code = append_from_scratch_registry_wiring(code)
    bot_file = Path(b["file_path"])
    await write_bot_code(b, code)

    if office_hook_config:
        await set_bot_office_hook_config(bot_id, office_hook_config)
    if voice_cashflow_config:
        await set_bot_voice_cashflow_config(bot_id, voice_cashflow_config)
        if voice_cashflow_config.get("voice_intake"):
            await enable_bot_feature(bot_id, "voice_intake")
        if voice_cashflow_config.get("cashflow_ledger"):
            await enable_bot_feature(bot_id, "cashflow_ledger")
        # Unlike cb_toggle_feature's manual path (enable_feature_and_reload),
        # enable_bot_feature here was called directly — the live webhook
        # Registry's in-memory Dispatcher for this bot_id would otherwise keep
        # running without the new feature's router until something else
        # (a manual toggle) triggers a reload. start_bot() below only spawns
        # a polling subprocess; it doesn't touch the Registry entry actually
        # serving this bot's webhook traffic.
        await _reload_registry(bot_id)
    if fallback_info:
        await add_template_candidate(
            creator_user_id=creator_user_id,
            summary=b.get("description", ""),
            fallback_reason=fallback_info["reason"],
            selected_templates=fallback_info["selected_templates"],
            bot_name=b["name"],
            bot_id=bot_id,
        )
    if miniapp_failure_info:
        # See handlers/create_bot.py's equivalent block for the full
        # rationale — logged here too (recreate can regenerate the config)
        # but no Telegram notification: this code path has no chat_id to
        # notify into (recreate_bot_core is a headless helper shared by both
        # the Telegram callback and the /analytics "restart" button).
        await add_miniapp_config_failure(
            creator_user_id=creator_user_id,
            summary=b.get("description", ""),
            failure_reason=miniapp_failure_info["reason"],
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
                logger.error(f"recreate_bot_core: bot_id={bot_id} regenerated but Menu Button setup failed: {e}")

    try:
        pid = await start_bot(bot_id, str(bot_file), b["token"], extra_env=_make_extra_env(b))
        await update_bot_status(bot_id, "running", pid)
        return {"ok": True, "error": None, "bot_name": b["name"]}
    except Exception as e:
        await update_bot_status(bot_id, "error")
        return {"ok": False, "error": f"start_failed:{str(e)[-300:]}", "bot_name": b["name"]}


async def autofix_bot_core(bot_id: int) -> dict:
    """Returns {"ok": bool, "error": str|None, "bot_name": str|None}. Mirrors
    cb_auto_diagnose's sequence: build a bug_description from the bot's own
    crash log (or a generic "analyze for bugs" prompt if none), run
    fix_bot_code, re-append office-hook wiring, write, push, restart."""
    b = await get_bot(bot_id)
    if not b or not b.get("file_path") or not Path(b["file_path"]).exists():
        return {"ok": False, "error": "file_missing", "bot_name": b["name"] if b else None}
    # This path can fire automatically off a crashed bot — nobody presses
    # anything — so refusing before the Claude call matters more here than
    # anywhere else.
    if is_template_backed(b.get("file_path")):
        return {"ok": False, "error": "template_backed", "bot_name": b["name"]}

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

    try:
        fixed_code = await asyncio.wait_for(fix_bot_code(current_code, bug_description), timeout=240.0)
    except Exception:
        logger.error(f"autofix_bot_core: fix_bot_code failed for bot_id={bot_id}")
        return {"ok": False, "error": "fix_failed", "bot_name": b["name"]}

    await stop_bot(bot_id)
    fixed_code = append_from_scratch_registry_wiring(fixed_code)
    await write_bot_code(b, fixed_code)

    try:
        pid = await start_bot(bot_id, b["file_path"], b["token"], extra_env=_make_extra_env(b))
        await update_bot_status(bot_id, "running", pid)
        return {"ok": True, "error": None, "bot_name": b["name"]}
    except Exception as e:
        await update_bot_status(bot_id, "error")
        return {"ok": False, "error": f"start_failed:{str(e)[-300:]}", "bot_name": b["name"]}


async def generate_fix_preview_core(bot_id: int, bug_description: str) -> dict:
    """Returns {"ok": bool, "error": str|None, "bot_name": str|None,
    "fixed_code": str|None, "explanation": str|None, "main_code_hash": str|None}.
    Generation-only half of the web-API fixbug flow — mirrors
    _generate_and_preview_fix's sequence (run fix_bot_code, get a
    plain-language diff explanation, hash the pre-generation source) WITHOUT
    writing anything to disk or restarting the bot. Pairs with apply_fix_core
    below, which takes this function's fixed_code + main_code_hash and does
    the actual write+restart after re-validating freshness — same
    generate/apply split as the Telegram flow, so a future web UI can show
    the same preview before the owner confirms."""
    b = await get_bot(bot_id)
    if not b:
        return {"ok": False, "error": "not_found", "bot_name": None, "fixed_code": None, "explanation": None, "main_code_hash": None}
    if not b.get("file_path") or not Path(b["file_path"]).exists():
        return {"ok": False, "error": "file_missing", "bot_name": b["name"], "fixed_code": None, "explanation": None, "main_code_hash": None}

    current_code = Path(b["file_path"]).read_text(encoding="utf-8")
    try:
        fixed_code = await asyncio.wait_for(fix_bot_code(current_code, bug_description), timeout=240.0)
    except Exception:
        logger.error(f"generate_fix_preview_core: fix_bot_code failed for bot_id={bot_id}")
        return {"ok": False, "error": "fix_failed", "bot_name": b["name"], "fixed_code": None, "explanation": None, "main_code_hash": None}

    try:
        explanation = await asyncio.wait_for(
            explain_bug_fix_diff(current_code, fixed_code, bug_description), timeout=60.0
        )
    except Exception:
        logger.exception(f"generate_fix_preview_core: bot_id={bot_id} explain_bug_fix_diff failed")
        explanation = "Исправление сгенерировано. Проверь и подтверди применение."

    return {
        "ok": True,
        "error": None,
        "bot_name": b["name"],
        "fixed_code": fixed_code,
        "explanation": explanation,
        "main_code_hash": _hash_bot_code(current_code),
    }


async def apply_fix_core(bot_id: int, fixed_code: str, main_code_hash: str | None) -> dict:
    """Returns {"ok": bool, "error": str|None, "bot_name": str|None}. Apply
    half of the web-API fixbug flow — takes generate_fix_preview_core's
    output (never regenerates), re-validates main_code_hash against the
    CURRENT file on disk (same freshness guard as cb_apply_fix/
    cb_apply_custom_feature: rejects applying a fix generated against a file
    that has since changed, e.g. a concurrent /recreate), then writes,
    pushes, and restarts."""
    b = await get_bot(bot_id)
    if not b:
        return {"ok": False, "error": "not_found", "bot_name": None}
    if not b.get("file_path") or not Path(b["file_path"]).exists():
        return {"ok": False, "error": "file_missing", "bot_name": b["name"]}
    if is_template_backed(b.get("file_path")):
        return {"ok": False, "error": "template_backed", "bot_name": b["name"]}

    current_code = Path(b["file_path"]).read_text(encoding="utf-8")
    if main_code_hash is not None and _hash_bot_code(current_code) != main_code_hash:
        return {"ok": False, "error": "stale_source", "bot_name": b["name"]}

    await stop_bot(bot_id)
    fixed_code = append_from_scratch_registry_wiring(fixed_code)
    await write_bot_code(b, fixed_code)

    try:
        pid = await start_bot(bot_id, b["file_path"], b["token"], extra_env=_make_extra_env(b))
        await update_bot_status(bot_id, "running", pid)
        return {"ok": True, "error": None, "bot_name": b["name"]}
    except Exception as e:
        await update_bot_status(bot_id, "error")
        return {"ok": False, "error": f"start_failed:{str(e)[-300:]}", "bot_name": b["name"]}


async def _compatible_features(template_id: str | None, bot_id: int | None = None) -> list[dict]:
    """Features whose # COMPATIBLE_WITH: header explicitly lists this bot's
    template_id — never a blanket "all" for ordinary features (see
    runtime/registry.py's discover_features()), so a bot with an
    unrecognized/missing template_id simply has none of those from the
    static header check alone.

    Two independent ways a feature can still show up for a from-scratch bot
    (template_id is None — no `# TEMPLATE:` marker, infer_template_id() can
    never resolve one):

    1. "*" — a feature module opts into it only when it is PROVABLY
       template-agnostic (see features/notifications.py's header comment):
       the same feature works identically for every bot regardless of
       template or from-scratch origin, so it's always safe to offer.
    2. Already ENABLED for this specific bot_id — for a feature like
       voice_intake/cashflow_ledger that is NOT universally compatible (it
       needs a schema specific to that bot's own generated tables) but was
       still auto-enabled for THIS particular from-scratch bot instance by
       services/claude_service.py's generation step (see
       handlers/create_bot.py's _apply_voice_cashflow_config) — the owner
       can see and toggle off what was auto-enabled for them, without this
       ever letting them discover and enable a NEW non-"*" feature for a
       from-scratch bot that wasn't already turned on for it.

    A from-scratch bot with bot_id=None (caller has no bot row yet) only
    ever sees "*" features, never per-instance-enabled ones."""
    features = discover_features()
    compatible = [f for f in features if template_id in f["compatible_with"] or "*" in f["compatible_with"]]
    if template_id is not None or bot_id is None:
        return compatible
    enabled = set(await get_bot_features(bot_id))
    already_shown = {f["name"] for f in compatible}
    compatible += [f for f in features if f["name"] in enabled and f["name"] not in already_shown]
    return compatible


@router.callback_query(F.data.startswith("features:"))
async def cb_features_list(callback: CallbackQuery):
    await callback.answer()
    bot_id = int(callback.data.split(":")[1])
    b = await _bot_or_deny(callback.from_user.id, bot_id)
    if not b:
        await callback.message.answer("Бот не найден.")
        return
    template_id = infer_template_id(b.get("file_path"))
    compatible = await _compatible_features(template_id, bot_id)
    enabled = set(await get_bot_features(bot_id))
    sheets_config = await get_bot_sheets_config(bot_id) if "sheets" in enabled else None
    has_yookassa_creds = bool(await get_bot_yookassa_credentials(bot_id)) if "payments" in enabled else False
    text = "🧩 <b>Фичи для этого бота</b> — нажми, чтобы включить/выключить:"
    if not compatible:
        text = "🧩 Для этого бота пока нет доступных фич."
    await _edit_or_resend(
        callback,
        text,
        parse_mode="HTML",
        reply_markup=_features_keyboard(bot_id, compatible, enabled, sheets_config, has_yookassa_creds),
    )


@router.callback_query(F.data.startswith("togglefeature:"))
async def cb_toggle_feature(callback: CallbackQuery):
    _, bot_id_str, feature_name = callback.data.split(":", 2)
    bot_id = int(bot_id_str)
    if bot_id in _busy_bots:
        await callback.answer(_BUSY_TEXT, show_alert=True)
        return
    _busy_bots.add(bot_id)
    try:
        b = await _bot_or_deny(callback.from_user.id, bot_id)
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
        # For a from-scratch bot (template_id is None) turning ON a
        # non-"*" feature, _compatible_features only ever offers
        # already-ENABLED features (see its own docstring) — so
        # "not is_enabled" reaching this check for such a feature would mean
        # the owner somehow got a togglefeature: callback for a feature the
        # keyboard never actually showed them; the static COMPATIBLE_WITH/"*"
        # check below still guards that case defensively. Turning an
        # already-enabled from-scratch feature back OFF is always allowed
        # (is_enabled short-circuits the check).
        if not is_enabled and template_id not in feature["compatible_with"] and "*" not in feature["compatible_with"]:
            await callback.answer("⛔ Эта фича не подходит шаблону этого бота.", show_alert=True)
            return
        if is_enabled:
            await disable_feature_and_reload(bot_id, feature_name)
        else:
            await enable_feature_and_reload(bot_id, feature_name)
        await callback.answer("✅ Включено" if not is_enabled else "🔴 Выключено")
        compatible = await _compatible_features(template_id, bot_id)
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


# Default event_type for callers outside this UI's own multi-step picker
# (runtime/factory_analytics_api.py's REST add_office_handler/remove_office_
# handler — the miniapp dashboard's bot-detail-panel office links, which has
# no picker step of its own yet) — kept as the same "order.created" default
# this UI itself used before the officetype: step was added.
_OFFICE_EVENT_TYPE = "order.created"


# ── "Офисы" (docs/OFFICES_DESIGN.md §9, §12) ────────────────────────────────
# Full flow: office:{bot_id} panel → officeconnect:{bot_id} picks the SOURCE
# bot (all bots offered, including the panel's own) → officetarget:{source_id}
# picks the subscriber → officetype:{source}:{target} picks event_type (auto-
# skipped when only one exists in _EVENT_TYPES, which is the case today) →
# officeconfirm:{source}:{target}:{event_type} shows the "what will happen"
# screen → officedolink:... actually writes the link and shows the success
# screen with an optional "🔔 Показать в Telegram-группе" button →
# officedigestguide shows the group-binding instructions.


def _office_bots_keyboard(bots: list[dict], callback_prefix: str, back_data: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=b["name"], callback_data=f"{callback_prefix}:{b['id']}")] for b in bots]
    rows.append([InlineKeyboardButton(text="◀ Назад", callback_data=back_data)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _office_panel_text_and_keyboard(bot_id: int) -> tuple[str, InlineKeyboardMarkup]:
    links = await get_office_links_for_bot(bot_id)
    rows: list[list[InlineKeyboardButton]] = []
    if not links:
        text = "🏢 <b>Офисы</b>\n\nПока нет связей с другими ботами."
    else:
        lines = ["🏢 <b>Офисы</b>\n\nТекущие связи:"]
        for link in links:
            source_id, target_id, event_type = link["source_bot_id"], link["target_bot_id"], link["event_type"]
            label = EVENT_TYPE_LABELS.get(event_type, event_type)
            if source_id == bot_id:
                source_bot = await get_bot(bot_id)
                target_bot = await get_bot(target_id)
                arrow_label = f"📤 {source_bot['name'] if source_bot else bot_id} → {target_bot['name'] if target_bot else target_id}"
            else:
                source_bot = await get_bot(source_id)
                target_bot = await get_bot(bot_id)
                arrow_label = f"📥 {source_bot['name'] if source_bot else source_id} → {target_bot['name'] if target_bot else bot_id}"
            lines.append(f"{arrow_label} ({label})")
            rows.append([
                InlineKeyboardButton(
                    text=f"❌ {arrow_label}",
                    callback_data=f"officeunlink:{source_id}:{target_id}:{event_type}:{bot_id}",
                )
            ])
        text = "\n".join(lines)
    rows.append([InlineKeyboardButton(text="➕ Связать ботов", callback_data=f"officeconnect:{bot_id}")])
    rows.append([InlineKeyboardButton(text="◀ Назад", callback_data=f"info:{bot_id}")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("office:"))
async def cb_office_panel(callback: CallbackQuery):
    await callback.answer()
    bot_id = int(callback.data.split(":")[1])
    b = await _bot_or_deny(callback.from_user.id, bot_id)
    if not b:
        await callback.message.answer("Бот не найден.")
        return
    text, keyboard = await _office_panel_text_and_keyboard(bot_id)
    await _edit_or_resend(callback, text, parse_mode="HTML", reply_markup=keyboard)


@router.callback_query(F.data.startswith("officeconnect:"))
async def cb_office_connect_start(callback: CallbackQuery):
    """Step 1: pick the SOURCE bot — the one whose events will be published.
    Entered from a specific bot's panel, so all bots (including this one)
    are offered; picking any of them (not just "this" bot) matches the
    "выбор бота-источника" step from the design brief rather than assuming
    the panel's own bot is always the source."""
    await callback.answer()
    bot_id = int(callback.data.split(":")[1])
    b = await _bot_or_deny(callback.from_user.id, bot_id)
    if not b:
        await callback.message.answer("Бот не найден.")
        return
    # Only bots this user can manage are offered as the SOURCE — a customer
    # must never see (or be able to pick) another customer's bot here, even
    # though db.database.add_office_link would reject a cross-owner link
    # server-side anyway; the picker shouldn't expose the option at all.
    all_bots = _visible_bots(callback.from_user.id, await get_all_bots())
    if len(all_bots) < 2:
        await _edit_or_resend(
            callback,
            "🏢 Других ботов пока нет — сначала создай ещё одного, чтобы объединить их в офис.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀ Назад", callback_data=f"office:{bot_id}")]
            ]),
        )
        return
    await _edit_or_resend(
        callback,
        "🏢 Выбери бота-<b>источника</b> — того, чьи события будут уведомлять другого бота:",
        parse_mode="HTML",
        reply_markup=_office_bots_keyboard(all_bots, "officetarget", f"office:{bot_id}"),
    )


@router.callback_query(F.data.startswith("officetarget:"))
async def cb_office_pick_target(callback: CallbackQuery):
    """Step 2: pick the TARGET (subscriber) bot, excluding the source."""
    await callback.answer()
    source_id = int(callback.data.split(":")[1])
    source_bot = await _bot_or_deny(callback.from_user.id, source_id)
    if not source_bot:
        await callback.message.answer("Бот не найден.")
        return
    # Same "only what this user can manage" restriction as step 1 — a
    # customer's target picker never lists another customer's bot.
    other_bots = [
        ob for ob in _visible_bots(callback.from_user.id, await get_all_bots()) if ob["id"] != source_id
    ]
    if not other_bots:
        await _edit_or_resend(
            callback,
            "🏢 Других ботов пока нет — сначала создай ещё одного, чтобы объединить их в офис.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀ Назад", callback_data=f"office:{source_id}")]
            ]),
        )
        return
    await _edit_or_resend(
        callback,
        f"🏢 Выбери бота, которого «{source_bot['name']}» будет уведомлять:",
        parse_mode="HTML",
        reply_markup=_office_bots_keyboard(
            other_bots, f"officetype:{source_id}", f"officeconnect:{source_id}"
        ),
    )


@router.callback_query(F.data.startswith("officetype:"))
async def cb_office_pick_type(callback: CallbackQuery):
    """Step 3: pick event_type from what the source bot can publish — today
    that's every key in _EVENT_TYPES (a single, global, closed set; there is
    no per-template publish manifest yet, see features/office_events.py's
    module docstring). With exactly one event_type registered, this step is
    auto-skipped straight into the confirmation screen so the owner isn't
    shown a one-item picker for no reason; it starts prompting for real the
    day a second event_type is added to _EVENT_TYPES."""
    await callback.answer()
    _, source_id_str, target_id_str = callback.data.split(":")
    source_id, target_id = int(source_id_str), int(target_id_str)
    if source_id == target_id:
        await callback.message.answer("⚠️ Нельзя объединить бота с самим собой.")
        return
    source_bot = await _bot_or_deny(callback.from_user.id, source_id)
    target_bot = await get_bot(target_id)
    if not source_bot or not target_bot:
        await callback.message.answer("Бот не найден.")
        return

    event_types = list(_EVENT_TYPES)
    if len(event_types) == 1:
        await _render_office_confirm(callback, source_id, target_id, event_types[0])
        return

    rows = [
        [InlineKeyboardButton(
            text=EVENT_TYPE_LABELS.get(et, et),
            callback_data=f"officeconfirm:{source_id}:{target_id}:{et}",
        )]
        for et in event_types
    ]
    rows.append([InlineKeyboardButton(text="◀ Назад", callback_data=f"officeconnect:{source_id}")])
    await _edit_or_resend(
        callback,
        f"🏢 Какое событие от «{source_bot['name']}» должно уведомлять «{target_bot['name']}»?",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


async def _render_office_confirm(callback: CallbackQuery, source_id: int, target_id: int, event_type: str) -> None:
    source_bot = await get_bot(source_id)
    target_bot = await get_bot(target_id)
    if not source_bot or not target_bot:
        await callback.message.answer("Бот не найден.")
        return
    label = EVENT_TYPE_LABELS.get(event_type, event_type)
    text = (
        f"🏢 <b>Что произойдёт</b>\n\n"
        f"Бот «{source_bot['name']}» будет автоматически уведомлять бота «{target_bot['name']}» "
        f"о событии «{label}».\n\n"
        f"Это работает через сервер — боты не должны состоять в одной группе Telegram. "
        f"Задержка — доли секунды."
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="✅ Подтвердить",
            callback_data=f"officedolink:{source_id}:{target_id}:{event_type}",
        )],
        [InlineKeyboardButton(text="◀ Назад", callback_data=f"officeconnect:{source_id}")],
    ])
    await _edit_or_resend(callback, text, parse_mode="HTML", reply_markup=keyboard)


@router.callback_query(F.data.startswith("officeconfirm:"))
async def cb_office_confirm(callback: CallbackQuery):
    await callback.answer()
    # maxsplit=3: event_type is the only free-form-looking segment — bounded
    # the same way officeunlink already is, see that handler's comment.
    _, source_id_str, target_id_str, event_type = callback.data.split(":", 3)
    source_id = int(source_id_str)
    source_bot = await _bot_or_deny(callback.from_user.id, source_id)
    if not source_bot:
        await callback.message.answer("Бот не найден.")
        return
    await _render_office_confirm(callback, source_id, int(target_id_str), event_type)


@router.callback_query(F.data.startswith("officedolink:"))
async def cb_office_do_link(callback: CallbackQuery):
    """Step 4: writes the link after the owner confirmed on the "what will
    happen" screen — kept as its own step (not fused into officeconfirm)
    so re-rendering the confirmation screen (e.g. via officeconfirm: from a
    Back navigation) never itself has a side effect."""
    await callback.answer()
    _, source_id_str, target_id_str, event_type = callback.data.split(":", 3)
    source_id, target_id = int(source_id_str), int(target_id_str)
    # Review finding (kept from the original single-step flow): the picker
    # keyboards already exclude self-links, but that's a UI-layer filter, not
    # a guarantee — this handler must not rely on it. A self-link would make
    # publish_event() hand a bot's own event straight back to itself as
    # though it came from another bot, which office_events' whole contract
    # assumes never happens (source_bot_id always means an OTHER bot).
    if source_id == target_id:
        await callback.message.answer("⚠️ Нельзя объединить бота с самим собой.")
        return
    source_bot = await _bot_or_deny(callback.from_user.id, source_id)
    target_bot = await get_bot(target_id)
    if not source_bot or not target_bot:
        await callback.message.answer("Бот не найден.")
        return
    if event_type not in _EVENT_TYPES:
        await callback.message.answer("⚠️ Неизвестный тип события.")
        return
    linked = await add_office_link(source_id, target_id, event_type)
    if not linked:
        await callback.message.answer("⚠️ Нельзя связать ботов разных владельцев.")
        return
    label = EVENT_TYPE_LABELS.get(event_type, event_type)
    text, keyboard_panel = await _office_panel_text_and_keyboard(source_id)
    success_rows = list(keyboard_panel.inline_keyboard)
    success_rows.insert(0, [InlineKeyboardButton(
        text="🔔 Показать в Telegram-группе", callback_data="officedigestguide",
    )])
    await _edit_or_resend(
        callback,
        f"✅ «{source_bot['name']}» теперь уведомляет «{target_bot['name']}» о событии «{label}».\n\n{text}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=success_rows),
    )


_OFFICE_DIGEST_GUIDE_TEXT = (
    "🔔 <b>Витрина связей в Telegram</b>\n\n"
    "1. Создай новую Telegram-группу.\n"
    "2. Добавь туда Creator-бота (этого бота, через которого ты управляешь фабрикой).\n"
    "3. Готово — он будет присылать сюда сводку по связанным событиям.\n\n"
    "Клиентские боты в эту группу добавлять не нужно — они не участвуют в передаче событий, "
    "вся доставка идёт через сервер."
)


@router.callback_query(F.data == "officedigestguide")
async def cb_office_digest_guide(callback: CallbackQuery):
    if not _is_owner(callback.from_user.id):
        await _deny_callback(callback)
        return
    await callback.answer()
    await callback.message.answer(_OFFICE_DIGEST_GUIDE_TEXT, parse_mode="HTML")


@router.callback_query(F.data.startswith("officeunlink:"))
async def cb_office_unlink(callback: CallbackQuery):
    # maxsplit=4: event_type is the only free-form-looking segment here, but
    # _EVENT_TYPES (features/office_events.py) is a closed, developer-owned
    # set with no ':' in any current key — still, an unbounded split() would
    # raise ValueError (unhandled — no answer to the user) the day a future
    # event_type contains ':'. Bounding it here keeps parsing correct
    # regardless of what event_type itself contains.
    _, source_id_str, target_id_str, event_type, panel_bot_id_str = callback.data.split(":", 4)
    source_id, target_id, panel_bot_id = int(source_id_str), int(target_id_str), int(panel_bot_id_str)
    await callback.answer()
    panel_bot = await _bot_or_deny(callback.from_user.id, panel_bot_id)
    if not panel_bot:
        await callback.message.answer("Бот не найден.")
        return
    await remove_office_link(source_id, target_id, event_type)
    text, keyboard = await _office_panel_text_and_keyboard(panel_bot_id)
    await _edit_or_resend(callback, text, parse_mode="HTML", reply_markup=keyboard)


# ── "Рабочая группа" (docs/GROUP_TASK_CHANNEL_DESIGN.md §5) ─────────────────
# Unlike office:/officeconnect: above, connection is NOT confirmed through a
# button tap here — features/group_task.py's own router auto-captures
# group_chat_id from the first mention/reply update it sees in a group with
# no existing bot_group_task_config row. This panel is read-only status +
# the setup guide + a disconnect action; enable_feature_and_reload() below
# is the only WRITE this panel itself performs (turning the feature on, the
# same action "🧩 Фичи" would do, so the owner doesn't have to visit that
# screen first just to reach this one).


async def _group_task_panel_text_and_keyboard(bot_id: int, bot_row: dict) -> tuple[str, InlineKeyboardMarkup]:
    username = bot_row.get("username") or "bot"
    config = await get_group_task_config(bot_id)
    if config and config["enabled"]:
        text = (
            f"💬 <b>Рабочая группа для {html.escape(bot_row['name'])}</b>\n\n"
            "Подключено. Пиши боту задачи в группе, упоминая "
            f"@{html.escape(username)} или отвечая на его сообщения."
        )
        rows = [[InlineKeyboardButton(text="🔌 Отключить", callback_data=f"grouptaskdisconnect:{bot_id}")]]
    else:
        text = (
            f"💬 <b>Рабочая группа для {html.escape(bot_row['name'])}</b>\n\n"
            "Подключи бота к своей Telegram-группе — сможешь писать ему задачи "
            f"прямо там (<code>@{html.escape(username)} сделай то-то</code> или "
            "ответом на его сообщение), и он будет отвечать в группе. Отвечает "
            "только тебе (владельцу), другие участники группы бота не получат ответа.\n\n"
            "<b>Как подключить:</b>\n"
            "1. Создай новую Telegram-группу (или используй существующую).\n"
            f"2. Добавь туда бота @{html.escape(username)}.\n"
            f"3. Зайди в BotFather → выбери @{html.escape(username)} → "
            "<b>Bot Settings</b> → <b>Group Privacy</b> → <b>Turn off</b>. "
            "Без этого шага бот не увидит твои сообщения в группе, даже если "
            "упомянуть его по имени.\n"
            "4. Напиши в группе что угодно, упомянув бота — фабрика подхватит "
            "группу автоматически."
        )
        rows = []
    rows.append([InlineKeyboardButton(text="◀ Назад", callback_data=f"info:{bot_id}")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("grouptask:"))
async def cb_group_task_panel(callback: CallbackQuery):
    await callback.answer()
    bot_id = int(callback.data.split(":")[1])
    bot_row = await _bot_or_deny(callback.from_user.id, bot_id)
    if not bot_row:
        await callback.message.answer("Бот не найден.")
        return
    # The feature must be enabled for features/group_task.py's router to ever
    # run for this bot — same enable_feature_and_reload() the "🧩 Фичи" toggle
    # uses, so opening this panel is enough to turn the channel on without a
    # separate trip through the Features screen first. A no-op (INSERT OR
    # IGNORE under the hood) if already enabled.
    enabled_features = await get_bot_features(bot_id)
    if "group_task" not in enabled_features:
        await enable_feature_and_reload(bot_id, "group_task")
    text, keyboard = await _group_task_panel_text_and_keyboard(bot_id, bot_row)
    await _edit_or_resend(callback, text, parse_mode="HTML", reply_markup=keyboard)


@router.callback_query(F.data.startswith("grouptaskdisconnect:"))
async def cb_group_task_disconnect(callback: CallbackQuery):
    await callback.answer()
    bot_id = int(callback.data.split(":")[1])
    bot_row = await _bot_or_deny(callback.from_user.id, bot_id)
    if not bot_row:
        await callback.message.answer("Бот не найден.")
        return
    await delete_group_task_config(bot_id)
    text, keyboard = await _group_task_panel_text_and_keyboard(bot_id, bot_row)
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
    await callback.answer()
    bot_id = int(callback.data.split(":")[1])
    if not await _bot_or_deny(callback.from_user.id, bot_id):
        await callback.message.answer("Бот не найден.")
        return
    await begin_sheets_connect(bot_id, state, functools.partial(_edit_or_resend, callback))


@router.callback_query(F.data.startswith("sheetscancel:"))
async def cb_sheets_connect_cancel(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    bot_id = int(callback.data.split(":")[1])
    if not await _bot_or_deny(callback.from_user.id, bot_id):
        await callback.message.answer("Бот не найден.")
        return
    await state.clear()
    await _back_to_features_panel(bot_id, callback.message)


@router.message(SheetsConnectFlow.waiting_for_link, F.text, ~F.text.startswith("/"))
async def msg_sheets_connect_link(message: Message, state: FSMContext):
    data = await state.get_data()
    bot_id = data.get("bot_id")
    if bot_id is None:
        await state.clear()
        return
    if not await _bot_or_deny(message.from_user.id, bot_id):
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
    # No bot_id available here (only reached on a non-text message, so no
    # FSM data lookup is possible) — access was already checked when this
    # FSM state was entered (cb_sheets_connect_start above), so this is just
    # the format-reminder fallback for whoever is already mid-flow.
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

# Checklist format ("☐ сделай → скопируй") instead of prose — cuts cognitive
# load on each step down to the concrete action plus the concrete thing to
# carry into the next screen. Not real Telegram checkboxes (messages don't
# support those) — plain characters used purely as a visual marker.
_PAYMENT_STEP_1_TEXT = (
    "💳 <b>Подключение оплаты (Telegram Payments)</b>\n\n"
    "Шаг 1 из 4 — Регистрация у платёжного провайдера\n\n"
    "☐ Если у тебя ещё нет магазина в ЮKassa — зарегистрируй его по кнопке ниже\n"
    "☐ Понадобятся данные ИП/самозанятости/юрлица — это требование платёжного "
    "законодательства, тут это не обойти"
)
_PAYMENT_STEP_2_TEXT = (
    "💳 Шаг 2 из 4 — Открой BotFather\n\n"
    "☐ Перейди в чат BotFather по кнопке ниже\n"
    "☐ Отправь туда: <code>/mybots</code>\n"
    "☐ Выбери своего бота → <b>Bot Settings</b> → <b>Payments</b>"
)
_PAYMENT_STEP_3_TEXT = (
    "💳 Шаг 3 из 4 — Выбери провайдера в списке\n\n"
    "☐ В списке провайдеров выбери <b>YooKassa</b>\n"
    "☐ BotFather откроет чат с @YooKassaBot — войди там в свой аккаунт ЮKassa\n"
    "☐ Подтверди — @YooKassaBot пришлёт токен вида "
    "<code>381764678:TEST:...</code> или <code>381764678:LIVE:...</code>"
)
_PAYMENT_STEP_4_TEXT = (
    "💳 Шаг 4 из 4 — Вставь токен сюда\n\n"
    "→ Скопируй токен, который прислал @YooKassaBot, и пришли его сюда сообщением"
)

# Static screenshots of the corresponding BotFather/@YooKassaBot screen, one per
# step. Prepared and dropped in by the project owner separately (BotFather's UI
# isn't ours to snapshot automatically); step 1 has no BotFather screen yet, so
# it stays text-only. A missing/placeholder file degrades to plain text — a
# stale or absent screenshot must never block the wizard, since the caption
# always carries the instruction independent of the image.
_PAYMENT_STEP_SCREENSHOTS = {
    2: Path(__file__).resolve().parent.parent / "assets" / "payment_guide" / "step2_botfather_payments.png",
    3: Path(__file__).resolve().parent.parent / "assets" / "payment_guide" / "step3_choose_provider.png",
    4: Path(__file__).resolve().parent.parent / "assets" / "payment_guide" / "step4_token_message.png",
}


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


async def _show_payment_step(callback: CallbackQuery, bot_id: int, step: int) -> None:
    """Renders one wizard step: photo+caption when a screenshot asset exists for
    this step, otherwise the plain-text card exactly as before. Telegram can't
    turn a text message into a photo message (or back) via edit_text/edit_media,
    so a step with a screenshot always deletes the old card and resends fresh —
    the small navigation flicker is an acceptable trade for never crashing on a
    missing/placeholder asset."""
    text = _PAYMENT_STEP_TEXTS[step]
    markup = _payment_step_keyboard(bot_id, step)
    screenshot = _PAYMENT_STEP_SCREENSHOTS.get(step)
    if screenshot is not None and screenshot.exists():
        try:
            await callback.message.delete()
        except TelegramBadRequest:
            pass  # already gone / too old to delete for the bot — resend anyway
        await callback.message.answer_photo(
            FSInputFile(str(screenshot)), caption=text, parse_mode="HTML", reply_markup=markup,
        )
        return
    await _edit_or_resend(callback, text, parse_mode="HTML", reply_markup=markup)


@router.callback_query(F.data.startswith("paystart:"))
async def cb_payment_connect_start(callback: CallbackQuery, state: FSMContext):
    """Provider choice — first screen of the payments connect flow. ЮKassa
    keeps its existing 4-step BotFather wizard (paychooseyk: below); Cloudpayments
    branches into the separate, shorter CloudpaymentsConnectFlow (public_id +
    api_secret only, no Telegram provider_token at all — see docs discussion
    at https://claude.ai/code/artifact/ca3f6b07-28b3-4719-86e4-fc3b2d2536ce,
    "Вариант 1": a standalone /pay/{bot_id}/{invoice_id} page instead of a
    Telegram Payments invoice)."""
    await callback.answer()
    bot_id = int(callback.data.split(":")[1])
    if not await _bot_or_deny(callback.from_user.id, bot_id):
        await callback.message.answer("Бот не найден.")
        return
    await state.clear()
    await _edit_or_resend(
        callback,
        "💳 Какой платёжный провайдер подключить?\n\n"
        "<b>ЮKassa</b> — оплата прямо в Telegram (стандартный счёт от бота).\n"
        "<b>Cloudpayments</b> — отдельная страница оплаты по ссылке (вне Telegram).",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="ЮKassa", callback_data=f"paychooseyk:{bot_id}")],
            [InlineKeyboardButton(text="Cloudpayments", callback_data=f"paychoosecp:{bot_id}")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data=f"paycancel:{bot_id}")],
        ]),
    )


@router.callback_query(F.data.startswith("paychooseyk:"))
async def cb_payment_choose_yookassa(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    bot_id = int(callback.data.split(":")[1])
    if not await _bot_or_deny(callback.from_user.id, bot_id):
        await callback.message.answer("Бот не найден.")
        return
    # Set from the very first screen (not just at the final text-collecting
    # step) so bot_id in FSM data is state-guarded throughout steps 1-3 too —
    # matches SheetsConnectFlow's single-state-from-the-start design instead
    # of leaving an unguarded window where another flow could silently
    # clobber this wizard's stashed bot_id.
    await state.set_state(PaymentConnectFlow.browsing_step)
    await state.update_data(bot_id=bot_id)
    await _show_payment_step(callback, bot_id, 1)


_CLOUDPAYMENTS_PUBLIC_ID_RE = re.compile(r"^pk_[A-Za-z0-9]{8,64}$")


@router.callback_query(F.data.startswith("paychoosecp:"))
async def cb_payment_choose_cloudpayments(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    bot_id = int(callback.data.split(":")[1])
    if not await _bot_or_deny(callback.from_user.id, bot_id):
        await callback.message.answer("Бот не найден.")
        return
    await state.set_state(CloudpaymentsConnectFlow.waiting_for_public_id)
    await state.update_data(bot_id=bot_id)
    await _edit_or_resend(
        callback,
        "💳 Подключение Cloudpayments.\n\n"
        "1️⃣ Зарегистрируйся в личном кабинете Cloudpayments, если ещё не сделал этого.\n"
        "2️⃣ Пришли <b>Public ID</b> магазина (вида <code>pk_...</code>, раздел "
        "«Настройки → API» в личном кабинете).",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Открыть Cloudpayments", url="https://merchant.cloudpayments.ru/")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data=f"paycancel:{bot_id}")],
        ]),
    )


@router.message(CloudpaymentsConnectFlow.waiting_for_public_id, F.text, ~F.text.startswith("/"))
async def msg_cloudpayments_public_id(message: Message, state: FSMContext):
    data = await state.get_data()
    bot_id = data.get("bot_id")
    if bot_id is None:
        await state.clear()
        return
    if not await _bot_or_deny(message.from_user.id, bot_id):
        await state.clear()
        return
    public_id = message.text.strip()
    if not _CLOUDPAYMENTS_PUBLIC_ID_RE.match(public_id):
        await message.answer(
            "⚠️ Не похоже на Public ID Cloudpayments. Ожидается формат вида "
            "<code>pk_1234567890abcdef</code>. Проверь и пришли ещё раз, либо нажми ❌ Отмена.",
            parse_mode="HTML",
        )
        return
    await state.update_data(bot_id=bot_id, public_id=public_id)
    await state.set_state(CloudpaymentsConnectFlow.waiting_for_api_secret)
    await message.answer(
        "✅ Public ID сохранён.\n\n"
        "3️⃣ Теперь пришли <b>API Secret</b> (раздел «Настройки → API», "
        "показывается один раз при создании — если потерян, сгенерируй новый в кабинете).",
        parse_mode="HTML",
    )


@router.message(CloudpaymentsConnectFlow.waiting_for_public_id)
async def msg_cloudpayments_public_id_invalid(message: Message) -> None:
    await message.answer("Пришли Public ID текстом, либо нажми ❌ Отмена.")


@router.message(CloudpaymentsConnectFlow.waiting_for_api_secret, F.text, ~F.text.startswith("/"))
async def msg_cloudpayments_api_secret(message: Message, state: FSMContext):
    data = await state.get_data()
    bot_id = data.get("bot_id")
    public_id = data.get("public_id")
    if bot_id is None or not public_id:
        await state.clear()
        return
    if not await _bot_or_deny(message.from_user.id, bot_id):
        await state.clear()
        return
    api_secret = message.text.strip()
    if len(api_secret) < 8:
        await message.answer(
            "⚠️ Похоже на слишком короткий ключ. Пришли API Secret из кабинета Cloudpayments ещё раз, "
            "либо нажми ❌ Отмена.",
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
        await set_bot_provider_type(bot_id, "cloudpayments", public_id, api_secret)
    finally:
        _busy_bots.discard(bot_id)
    await state.clear()
    # Best-effort: delete the message carrying the raw API secret so it
    # doesn't sit in the chat history in plaintext (mirrors the same
    # trade-off msg_payment_secret_key already makes for ЮKassa's secret_key —
    # see that handler a bit further down).
    try:
        await message.delete()
    except TelegramBadRequest:
        pass
    await message.answer(
        "✅ Cloudpayments подключён. Теперь счета для этого бота будут отправляться "
        "ссылкой на отдельную страницу оплаты вместо стандартного Telegram-инвойса."
    )


@router.message(CloudpaymentsConnectFlow.waiting_for_api_secret)
async def msg_cloudpayments_api_secret_invalid(message: Message) -> None:
    await message.answer("Пришли API Secret текстом, либо нажми ❌ Отмена.")


@router.callback_query(F.data.startswith("paystep:"))
async def cb_payment_connect_step(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    _, bot_id_str, step_str = callback.data.split(":", 2)
    bot_id, step = int(bot_id_str), int(step_str)
    if step not in _PAYMENT_STEP_TEXTS:
        # Malformed/tampered/stale callback_data (e.g. an old inline keyboard
        # surviving a future step-range change) — ignore rather than crash on
        # a dict lookup or route through invalid FSM data.
        return
    if not await _bot_or_deny(callback.from_user.id, bot_id):
        await state.clear()
        await callback.message.answer("Бот не найден.")
        return
    if step == 4:
        # Only the final step actually collects free text.
        await state.set_state(PaymentConnectFlow.waiting_for_token)
    else:
        await state.set_state(PaymentConnectFlow.browsing_step)
    await state.update_data(bot_id=bot_id)
    await _show_payment_step(callback, bot_id, step)


@router.callback_query(F.data.startswith("paycancel:"))
async def cb_payment_connect_cancel(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    bot_id = int(callback.data.split(":")[1])
    if not await _bot_or_deny(callback.from_user.id, bot_id):
        await callback.message.answer("Бот не найден.")
        return
    await state.clear()
    await _back_to_features_panel(bot_id, callback.message)


@router.message(PaymentConnectFlow.waiting_for_token, F.text, ~F.text.startswith("/"))
async def msg_payment_connect_token(message: Message, state: FSMContext):
    data = await state.get_data()
    bot_id = data.get("bot_id")
    if bot_id is None:
        await state.clear()
        return
    if not await _bot_or_deny(message.from_user.id, bot_id):
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
    # No FSM state read here (non-text messages carry no bot_id lookup worth
    # doing) — access was already checked when this state was entered.
    await message.answer("Пришли токен от @YooKassaBot текстом, либо нажми ❌ Отмена.")


# ── step 5/6: optional ЮKassa API credentials (shopId + secret key) ─────────
# Separate from provider_token above — see services/yookassa_api.py. Purely
# additive: skipping this leaves payments fully working exactly as before,
# it only unlocks the auto-fetched-details and status-check conveniences.

@router.callback_query(F.data.startswith("payskip:"))
async def cb_payment_details_skip(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    bot_id = int(callback.data.split(":")[1])
    if not await _bot_or_deny(callback.from_user.id, bot_id):
        await callback.message.answer("Бот не найден.")
        return
    await state.clear()
    await _back_to_features_panel(bot_id, callback.message)


@router.callback_query(F.data.startswith("payownnew:"))
async def cb_payment_details_own_new(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    bot_id = int(callback.data.split(":")[1])
    if not await _bot_or_deny(callback.from_user.id, bot_id):
        await callback.message.answer("Бот не найден.")
        return
    await state.set_state(PaymentConnectFlow.waiting_for_shop_id)
    await state.update_data(bot_id=bot_id)
    await callback.message.answer(
        "Пришли <b>shopId</b> магазина (числовой ID из личного кабинета ЮKassa, "
        "раздел Интеграция → API).",
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("payreuse:"))
async def cb_payment_details_reuse(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    bot_id = int(callback.data.split(":")[1])
    if bot_id not in _busy_bots:
        _busy_bots.add(bot_id)
        try:
            if not await _bot_or_deny(callback.from_user.id, bot_id):
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
    data = await state.get_data()
    bot_id = data.get("bot_id")
    if bot_id is None:
        await state.clear()
        return
    if not await _bot_or_deny(message.from_user.id, bot_id):
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
    await message.answer("Пришли shopId текстом (число), либо нажми ❌ Отмена.")


@router.message(PaymentConnectFlow.waiting_for_secret_key, F.text, ~F.text.startswith("/"))
async def msg_payment_secret_key(message: Message, state: FSMContext):
    data = await state.get_data()
    bot_id = data.get("bot_id")
    shop_id = data.get("shop_id")
    if bot_id is None or shop_id is None:
        await state.clear()
        return
    if not await _bot_or_deny(message.from_user.id, bot_id):
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
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)


@router.callback_query(F.data.startswith("paymulti:"))
async def cb_payment_multi_connect(callback: CallbackQuery):
    await callback.answer()
    _, _source_bot_id_str, target_bot_id_str = callback.data.split(":", 2)
    target_bot_id = int(target_bot_id_str)
    target_bot = await _bot_or_deny(callback.from_user.id, target_bot_id)
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
    await callback.answer()
    bot_id = int(callback.data.split(":")[1])
    if not await _bot_or_deny(callback.from_user.id, bot_id):
        await callback.message.answer("Бот не найден.")
        return
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


async def _perform_start(bot_id: int, b: dict) -> tuple[bool, str]:
    """Core "start this bot" action shared by cb_start (button) and the AI
    dialog's start_bot tool (features/ai_dialog.py) — single source of truth
    so neither path can drift from the other. Returns (success,
    human-readable outcome) instead of raising, so both callers can render
    it their own way (edited card vs. chat reply)."""
    if is_running(bot_id):
        return True, "Уже запущен."
    try:
        pid = await start_bot(bot_id, b["file_path"], b["token"], extra_env=_make_extra_env(b))
        await update_bot_status(bot_id, "running", pid)
        return True, "Бот запущен."
    except Exception:
        await update_bot_status(bot_id, "error")
        return False, "Бот не смог запуститься — в сгенерированном коде ошибка."


async def _perform_stop(bot_id: int) -> tuple[bool, str]:
    await stop_bot(bot_id)
    await update_bot_status(bot_id, "stopped")
    return True, "Бот остановлен."


async def _perform_restart(bot_id: int, b: dict) -> tuple[bool, str]:
    await stop_bot(bot_id)
    try:
        pid = await start_bot(bot_id, b["file_path"], b["token"], extra_env=_make_extra_env(b))
        await update_bot_status(bot_id, "running", pid)
        return True, "Бот перезапущен."
    except Exception as e:
        logger.error(f"Failed to restart bot {bot_id}: {e}")
        await update_bot_status(bot_id, "error")
        return False, "Бот не смог перезапуститься — в коде ошибка."


@router.callback_query(F.data.startswith("start:"))
async def cb_start(callback: CallbackQuery):
    bot_id = int(callback.data.split(":")[1])
    if bot_id in _busy_bots:
        await callback.answer(_BUSY_TEXT, show_alert=True)
        return
    _busy_bots.add(bot_id)
    await callback.answer()
    try:
        b = await _bot_or_deny(callback.from_user.id, bot_id)
        if not b:
            await callback.message.answer("Бот не найден.")
            return
        ok, _msg = await _perform_start(bot_id, b)
        if not ok:
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
    await callback.answer()
    bot_id = int(callback.data.split(":")[1])
    b = await _bot_or_deny(callback.from_user.id, bot_id)
    if not b:
        await callback.message.answer("Бот не найден.")
        return
    await _perform_stop(bot_id)
    b["username"] = await _ensure_username(b)
    await _edit_or_resend(
        callback, _bot_text(b), parse_mode="HTML", reply_markup=_bot_keyboard(bot_id)
    )


@router.callback_query(F.data.startswith("restart:"))
async def cb_restart(callback: CallbackQuery):
    bot_id = int(callback.data.split(":")[1])
    if bot_id in _busy_bots:
        await callback.answer(_BUSY_TEXT, show_alert=True)
        return
    _busy_bots.add(bot_id)
    await callback.answer()
    try:
        b = await _bot_or_deny(callback.from_user.id, bot_id)
        if not b:
            await callback.message.answer("Бот не найден.")
            return
        ok, _msg = await _perform_restart(bot_id, b)
        if not ok:
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
    # Logs stay owner-only — see cmd_logs's comment above.
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
    b = await _bot_or_deny(callback.from_user.id, bot_id)
    if not b:
        await callback.message.answer("Бот не найден.")
        return
    name = b["name"]
    await stop_bot(bot_id)
    await delete_bot(bot_id)
    from features.group_task import invalidate_group_task_state

    invalidate_group_task_state(bot_id)
    _delete_custom_feature_file(bot_id)
    await callback.message.edit_text(
        f"✅ Бот <b>{name}</b> удалён.\n\nСоздай нового: /create",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="◀ К списку", callback_data="list")
        ]]),
    )


@router.callback_query(F.data.startswith("recreate:"))
async def cb_recreate_confirm(callback: CallbackQuery):
    """Confirmation gate in front of cb_recreate_go below.

    /recreate rewrites the bot's whole source with an LLM pass and pushes the
    result to GitHub — the most destructive thing this panel can do to a
    working bot, and previously one tap away. The other rewrite path (fixbug)
    already shows a diff preview before its "✅ Применить", so it has an
    equivalent step; this one had none.
    """
    bot_id = int(callback.data.split(":")[1])
    b = await get_bot(bot_id)
    if not b or not _can_manage_bot(callback.from_user.id, b):
        await _deny_callback(callback)
        return
    await callback.answer()
    if is_template_backed(b.get("file_path")):
        await callback.message.edit_text(TEMPLATE_BACKED_DENIED)
        return
    await callback.message.edit_text(
        f"🔄 Перегенерировать код бота <b>{b['name']}</b>?\n\n"
        "Claude перепишет весь файл бота заново по его описанию, старая версия "
        "будет заменена, а новая — отправлена в GitHub. Данные бота "
        "(записи, настройки) не тронутся.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, перегенерировать", callback_data=f"recreate_go:{bot_id}")],
            [InlineKeyboardButton(text="↩️ Отмена", callback_data=f"info:{bot_id}")],
        ]),
    )


@router.callback_query(F.data.startswith("recreate_go:"))
async def cb_recreate(callback: CallbackQuery):
    bot_id = int(callback.data.split(":")[1])
    if bot_id in _busy_bots:
        await callback.answer(_BUSY_TEXT, show_alert=True)
        return
    _busy_bots.add(bot_id)
    await callback.answer()
    try:
        b = await _bot_or_deny(callback.from_user.id, bot_id)
        if not b:
            await callback.message.edit_text("Бот не найден.")
            return
        if not b.get("description"):
            await callback.message.edit_text(
                "❌ Не могу пересоздать — описание бота не сохранилось.\n\nСоздай заново через /create.",
            )
            return
        if is_template_backed(b.get("file_path")):
            await callback.message.edit_text(TEMPLATE_BACKED_DENIED)
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
        voice_cashflow_config: dict | None = None
        fallback_info: dict | None = None
        miniapp_failure_info: dict | None = None
        try:
            result = await asyncio.wait_for(task, timeout=240.0)
            if regenerating_from_scratch:
                code, miniapp_config, office_hook_config, voice_cashflow_config, fallback_info, miniapp_failure_info = result
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

        # improve_bot_code() rewrites the WHOLE file including any previously
        # appended office-hook wiring (docs/OFFICE_HOOK_FROM_SCRATCH_BOTS.md) —
        # it's an LLM pass with no instruction to preserve that boilerplate, so
        # a from-scratch bot's config_from_bot_row/ConfigMiddleware/
        # on_office_event can silently vanish on "улучшить код" without this.
        # No-op for template-based bots (file_path always has its own
        # config_from_bot_row already) and for from-scratch code that still
        # generated the same exports on its own.
        code = append_from_scratch_registry_wiring(code)

        bot_file = Path(b["file_path"])
        await write_bot_code(b, code)

        if office_hook_config:
            await set_bot_office_hook_config(bot_id, office_hook_config)
        if voice_cashflow_config:
            await set_bot_voice_cashflow_config(bot_id, voice_cashflow_config)
            if voice_cashflow_config.get("voice_intake"):
                await enable_bot_feature(bot_id, "voice_intake")
            if voice_cashflow_config.get("cashflow_ledger"):
                await enable_bot_feature(bot_id, "cashflow_ledger")
            # See recreate_bot_core's identical comment — enable_bot_feature
            # was called directly here too, bypassing enable_feature_and_reload.
            await _reload_registry(bot_id)
        if fallback_info:
            await add_template_candidate(
                creator_user_id=callback.from_user.id,
                summary=b.get("description", ""),
                fallback_reason=fallback_info["reason"],
                selected_templates=fallback_info["selected_templates"],
                bot_name=b["name"],
                bot_id=bot_id,
            )
        if miniapp_failure_info:
            # See handlers/create_bot.py's equivalent block for the full
            # rationale. No extra Telegram notification here beyond the
            # regular "бот пересоздан" message below — this callback already
            # has the owner's chat open, so a silent log entry (surfaced via
            # /analytics) is enough rather than a second interrupting message.
            await add_miniapp_config_failure(
                creator_user_id=callback.from_user.id,
                summary=b.get("description", ""),
                failure_reason=miniapp_failure_info["reason"],
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

class _AutofixAnalyzeError(Exception):
    """Raised by _perform_autofix when the Claude analysis call itself fails
    (timeout/exception) — distinct from a successful fix whose resulting
    code just fails to restart, since callers offer different follow-ups
    for the two (retry-analysis vs delete-bot)."""


async def _perform_autofix(bot_id: int, b: dict) -> tuple[bool, str]:
    """Core auto-diagnose action shared by cb_auto_diagnose (button) and the
    AI dialog's run_autofix tool (features/ai_dialog.py). Raises
    _AutofixAnalyzeError if the Claude call itself fails; otherwise returns
    (success, human-readable outcome) for the restart-after-fix step, same
    shape as _perform_start/_perform_restart above."""
    # Before the Claude call, not at the write: this runs off a crashed bot
    # (button, or the AI dialog's run_autofix tool) without an explicit
    # "rewrite my code" from anyone.
    if is_template_backed(b.get("file_path")):
        return False, TEMPLATE_BACKED_DENIED

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

    try:
        fixed_code = await asyncio.wait_for(
            fix_bot_code(current_code, bug_description), timeout=240.0
        )
    except Exception:
        raise _AutofixAnalyzeError()

    await stop_bot(bot_id)
    # Same re-append as cb_recreate above — fix_bot_code() is also a
    # whole-file LLM rewrite that can drop the appended office-hook wiring.
    fixed_code = append_from_scratch_registry_wiring(fixed_code)
    await write_bot_code(b, fixed_code)

    try:
        pid = await start_bot(bot_id, b["file_path"], b["token"], extra_env=_make_extra_env(b))
        await update_bot_status(bot_id, "running", pid)
        return True, f"«{b['name']}» исправлен и перезапущен!"
    except Exception as e:
        await update_bot_status(bot_id, "error")
        return False, f"Код исправлен, но бот снова не запустился: {str(e)[-300:]}"


@router.callback_query(F.data.startswith("autofix:"))
async def cb_auto_diagnose(callback: CallbackQuery):
    bot_id = int(callback.data.split(":")[1])
    if bot_id in _busy_bots:
        await callback.answer(_BUSY_TEXT, show_alert=True)
        return
    _busy_bots.add(bot_id)
    await callback.answer()
    try:
        b = await _bot_or_deny(callback.from_user.id, bot_id)
        if not b or not b.get("file_path") or not Path(b["file_path"]).exists():
            await callback.message.edit_text("❌ Файл бота не найден — попробуй Перегенерировать.")
            return

        error_log = get_bot_logs(bot_id) or ""
        await callback.message.edit_text(
            f"🔍 Диагностирую <b>{b['name']}</b>...\n\n"
            + (f"<code>{html.escape(error_log[-300:])}</code>" if error_log else "Логов нет — анализирую код."),
            parse_mode="HTML",
        )

        try:
            ok, msg = await _perform_autofix(bot_id, b)
        except _AutofixAnalyzeError:
            await callback.message.edit_text(
                "⚠️ Не удалось проанализировать код. Попробуй ещё раз.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="🔍 Попробовать снова", callback_data=f"autofix:{bot_id}"),
                    InlineKeyboardButton(text="◀ Назад", callback_data=f"info:{bot_id}"),
                ]]),
            )
            return

        if ok:
            await callback.message.edit_text(
                f"✅ {msg}", parse_mode="HTML", reply_markup=_bot_keyboard(bot_id)
            )
        else:
            await callback.message.edit_text(
                f"⚠️ {html.escape(msg)}",
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
    await callback.answer()
    bot_id = int(callback.data.split(":")[1])
    b = await _bot_or_deny(callback.from_user.id, bot_id)
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


async def _generate_and_preview_fix(message: Message, state: FSMContext, bug_description: str) -> None:
    """Generation half of the fixbug flow — mirrors
    handlers/custom_features.py's _generate_and_preview: runs fix_bot_code,
    stashes the result in FSM state (never writes to disk here), and shows
    a plain-language preview with Применить/Отмена. Previously this and the
    apply step (now cb_apply_fix) were one function (_apply_fix) that wrote
    and restarted immediately with no pause — see this module's docstring-
    level comment history in handlers/custom_features.py for why that's
    risky when the owner is actively troubleshooting a live client issue."""
    data = await state.get_data()
    bot_id = data.get("fix_bot_id")
    await state.clear()
    if bot_id is None:
        return

    if bot_id in _busy_bots:
        await message.answer(_BUSY_TEXT)
        return
    _busy_bots.add(bot_id)
    try:
        b = await _bot_or_deny(message.from_user.id, bot_id)
        if not b or not b.get("file_path") or not Path(b["file_path"]).exists():
            await message.answer("❌ Файл бота не найден — попробуй Перегенерировать.")
            return

        current_code = Path(b["file_path"]).read_text(encoding="utf-8")

        fix_msg = await message.answer(f"🔧 Исправляю код <b>{html.escape(b['name'])}</b>...", parse_mode="HTML")
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
                reply_markup=_fix_retry_keyboard(bot_id),
            )
            return

        try:
            explanation = await asyncio.wait_for(
                explain_bug_fix_diff(current_code, fixed_code, bug_description), timeout=60.0
            )
        except Exception:
            logger.exception(f"_generate_and_preview_fix: bot_id={bot_id} explain_bug_fix_diff failed")
            explanation = "Исправление сгенерировано. Проверь и подтверди применение."

        try:
            await fix_msg.delete()
        except Exception:
            pass

        # fix_main_code_hash lets cb_apply_fix detect if the bot's file
        # changed since this fix was generated against it (e.g. a
        # /recreate or a second "Исправить баг" run while this preview sat
        # waiting for the owner) — same freshness-guard reasoning as
        # handlers/custom_features.py's cf_main_code_hash.
        await state.set_state(FixBotStates.previewing_fix)
        await state.set_data({
            "fix_bot_id": bot_id,
            "fix_pending_code": fixed_code,
            "fix_bug_description": bug_description,
            "fix_main_code_hash": _hash_bot_code(current_code),
        })
        preview_text = f"{explanation}\n\n<i>Проверь и подтверди применение.</i>"
        try:
            await message.answer(preview_text, parse_mode="HTML", reply_markup=_fix_preview_keyboard(bot_id))
        except TelegramBadRequest:
            # explanation is Haiku's own free-form output — fall back to
            # plain text if it produced something HTML can't parse, same
            # pattern as custom_features.py's preview send.
            logger.warning(f"_generate_and_preview_fix: bot_id={bot_id} preview HTML failed to parse, sending as plain text")
            await message.answer(preview_text, reply_markup=_fix_preview_keyboard(bot_id))
    finally:
        _busy_bots.discard(bot_id)


async def _can_manage_pending_fix(user_id: int, data: dict) -> bool:
    bot_id = data.get("fix_bot_id")
    if bot_id is None:
        return False
    b = await get_bot(bot_id)
    return bool(b) and _can_manage_bot(user_id, b)


@router.message(FixBotStates.describing_bug, F.voice)
async def msg_fix_voice(message: Message, state: FSMContext, bot: Bot):
    # Access already checked when this FSM state was entered (cb_fix_bug) and
    # re-checked inside _generate_and_preview_fix before anything is written.
    text = await _recognize_voice_fix(message, bot)
    if text:
        await _generate_and_preview_fix(message, state, text)


@router.message(FixBotStates.describing_bug, F.text, ~F.text.startswith("/"))
async def msg_fix_text(message: Message, state: FSMContext, bot: Bot):
    await _generate_and_preview_fix(message, state, message.text)


@router.message(FixBotStates.describing_bug)
async def msg_fix_unsupported(message: Message):
    await message.answer(
        "Не понял — отправь текст или голосовое с описанием бага.",
        reply_markup=cancel_keyboard(),
    )


@router.message(FixBotStates.previewing_fix)
async def msg_fix_preview_pending(message: Message, state: FSMContext):
    # A preview is already sitting in front of the owner — any message here
    # is ambiguous (new bug? more detail?) and would silently overwrite
    # fix_pending_code out from under the two buttons already shown. Same
    # guard as custom_features.py's identical check.
    data = await state.get_data()
    if not await _can_manage_pending_fix(message.from_user.id, data):
        await _deny_message(message)
        return
    await message.answer("Сначала подтверди или отмени текущее исправление кнопками выше.")


@router.callback_query(F.data.startswith("applyfix:"))
async def cb_apply_fix(callback: CallbackQuery, state: FSMContext):
    bot_id = int(callback.data.split(":")[1])
    b = await get_bot(bot_id)
    if not b or not _can_manage_bot(callback.from_user.id, b):
        await _deny_callback(callback)
        return
    if is_template_backed(b.get("file_path")):
        await callback.answer()
        await callback.message.edit_text(TEMPLATE_BACKED_DENIED)
        return
    # Check-then-add with no await in between — same double-tap race guard
    # as cb_recreate/cb_apply_custom_feature.
    if bot_id in _busy_bots:
        await callback.answer(_BUSY_TEXT, show_alert=True)
        return
    _busy_bots.add(bot_id)
    try:
        await callback.answer()
        data = await state.get_data()
        if data.get("fix_bot_id") != bot_id or "fix_pending_code" not in data:
            await callback.message.edit_text("Исправление устарело — начни заново.")
            return
        fixed_code = data["fix_pending_code"]
        stored_hash = data.get("fix_main_code_hash")
        await state.clear()

        b = await get_bot(bot_id)
        if not b or not b.get("file_path") or not Path(b["file_path"]).exists():
            await callback.message.edit_text("❌ Файл бота не найден.")
            return

        # See _generate_and_preview_fix's comment on fix_main_code_hash —
        # the actual guard against applying a fix generated against a file
        # that has since changed (e.g. a /recreate while this preview sat
        # waiting for the tap).
        current_code = Path(b["file_path"]).read_text(encoding="utf-8")
        if stored_hash is not None and _hash_bot_code(current_code) != stored_hash:
            await callback.message.edit_text(
                "⚠️ Код бота изменился с момента генерации этого исправления "
                "(например, через Перегенерировать или другое исправление) — "
                "применять устаревшее исправление рискованно. Сгенерируй заново.",
                reply_markup=_fix_retry_keyboard(bot_id),
            )
            return

        await stop_bot(bot_id)
        # Same re-append as cb_recreate — fix_bot_code() is a whole-file LLM
        # rewrite that can drop the appended office-hook wiring.
        fixed_code = append_from_scratch_registry_wiring(fixed_code)
        await write_bot_code(b, fixed_code)

        try:
            pid = await start_bot(bot_id, b["file_path"], b["token"], extra_env=_make_extra_env(b))
            await update_bot_status(bot_id, "running", pid)
            await callback.message.edit_text(
                f"✅ Бот <b>{html.escape(b['name'])}</b> исправлен и перезапущен!",
                parse_mode="HTML",
                reply_markup=_bot_keyboard(bot_id),
            )
        except Exception as e:
            await update_bot_status(bot_id, "error")
            await callback.message.edit_text(
                f"⚠️ Код исправлен, но бот не запустился.\n\n<code>{html.escape(str(e)[-300:])}</code>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="🐛 Исправить снова", callback_data=f"fixbug:{bot_id}"),
                    InlineKeyboardButton(text="🗑 Удалить", callback_data=f"delete:{bot_id}"),
                ]]),
            )
    finally:
        _busy_bots.discard(bot_id)


@router.callback_query(F.data.startswith("cancelfix:"))
async def cb_cancel_fix(callback: CallbackQuery, state: FSMContext):
    bot_id = int(callback.data.split(":")[1])
    b = await get_bot(bot_id)
    if not b or not _can_manage_bot(callback.from_user.id, b):
        await _deny_callback(callback)
        return
    await state.clear()
    await callback.answer()
    await callback.message.edit_text(
        "❌ Отменено — ничего не изменено.",
        reply_markup=_bot_keyboard(bot_id),
    )


# ── AI dialog (voice/text free-form, docs/AI_DIALOG design) ────────────────
#
# The "✨🎙️ Хочу наговорить/написать что изменить" entry point on the detail
# panel (_bot_keyboard above). Same tool-use shape as features/group_task.py
# — Claude (Sonnet) gets a small set of tool schemas (features/ai_dialog.py's
# TOOLS), a tool call is turned into a preview + ✅/❌ confirmation and NEVER
# executed straight from the model's response, and the actual execution goes
# through the SAME _perform_start/_perform_stop/_perform_restart/
# _perform_autofix/get_bot_logs helpers the ordinary buttons use — no
# separate implementation of what each action does. Unlike group_task.py,
# this runs in the owner's own 1:1 chat with the Creator bot (not a shared
# group), so authorization is the existing per-bot _bot_or_deny check plus
# aiogram's own per-user FSM scoping — no separate owner-recheck dance is
# needed the way group_task.py needs one for a multi-user group chat.

_AI_DIALOG_MODEL = "claude-sonnet-5"
_AI_DIALOG_CONTEXT_TURNS = 6
# Same rationale as features/group_task.py's _PENDING_TTL_SECONDS — a stale
# confirm tap after the bot's real state has since changed (e.g. someone
# already restarted it from the button) should not silently re-fire.
_AI_DIALOG_PENDING_TTL_SECONDS = 300
_AI_DIALOG_EXIT_WORDS = {"выйти", "стоп", "exit", "cancel", "отмена"}


def _ai_dialog_keyboard(bot_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="◀ Выйти из диалога", callback_data=f"aidialogexit:{bot_id}"),
    ]])


def _ai_dialog_confirm_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"aidok:{token}"),
        InlineKeyboardButton(text="❌ Отменить", callback_data=f"aidno:{token}"),
    ]])


@router.callback_query(F.data.startswith("aidialog:"))
async def cb_aidialog(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    bot_id = int(callback.data.split(":")[1])
    b = await _bot_or_deny(callback.from_user.id, bot_id)
    if not b:
        await callback.message.answer("Бот не найден.")
        return
    await state.set_state(AiDialogStates.chatting)
    await state.update_data(ai_dialog_bot_id=bot_id, ai_dialog_history=[], ai_dialog_pending=None)
    await callback.message.answer(
        f"🎙️ Диалог с ботом <b>{b['name']}</b>\n\n"
        "Опиши голосовым или текстом, что хочешь сделать с этим ботом — например "
        "«поставь на паузу», «перезапусти», «почему он не отвечает».\n\n"
        "Для действий, кроме показа логов, я спрошу подтверждение перед выполнением.",
        parse_mode="HTML",
        reply_markup=_ai_dialog_keyboard(bot_id),
    )


@router.callback_query(F.data.startswith("aidialogexit:"))
async def cb_aidialog_exit(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    bot_id = int(callback.data.split(":")[1])
    b = await _bot_or_deny(callback.from_user.id, bot_id)
    if not b:
        await callback.message.answer("Бот не найден.")
        return
    b["username"] = await _ensure_username(b)
    await callback.message.answer(_bot_text(b), parse_mode="HTML", reply_markup=_bot_keyboard(bot_id))


async def _ai_dialog_execute_tool(tool_name: str, bot_id: int, b: dict) -> str:
    """Executes a confirmed (or read-only) tool call through the SAME
    _perform_*/get_bot_logs helpers the detail panel's own buttons use —
    never reimplements what an action does. Returns the human-readable
    outcome shown to the owner; never raises."""
    if tool_name == "stop_bot":
        _ok, msg = await _perform_stop(bot_id)
        return msg
    if tool_name == "start_bot":
        _ok, msg = await _perform_start(bot_id, b)
        return msg
    if tool_name == "restart_bot":
        _ok, msg = await _perform_restart(bot_id, b)
        return msg
    if tool_name == "run_autofix":
        try:
            _ok, msg = await _perform_autofix(bot_id, b)
            return msg
        except _AutofixAnalyzeError:
            return "Не удалось проанализировать код. Попробуй ещё раз через «🔍 Авто-диагностика»."
    if tool_name == "show_logs":
        logs = get_bot_logs(bot_id) or "Логов нет (бот не запускался в этой сессии)."
        if len(logs) > 3500:
            logs = "...\n" + logs[-3500:]
        return logs
    return f"Неизвестное действие {tool_name!r}."


async def _ai_dialog_call_claude(bot_id: int, b: dict, history: list[tuple[str, str]]):
    from anthropic import AsyncAnthropic
    from config import ANTHROPIC_API_KEY

    template_id = (
        infer_template_id(b["file_path"]) if b.get("file_path") else None
    ) or "general"
    status = "запущен" if is_running(bot_id) else "остановлен"
    system_prompt = ai_dialog.SYSTEM_PROMPT_TEMPLATE.format(
        name=b["name"], template_id=template_id, status=status
    )
    client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    return await client.messages.create(
        model=_AI_DIALOG_MODEL,
        max_tokens=500,
        system=system_prompt,
        messages=[{"role": role, "content": content} for role, content in history],
        tools=ai_dialog.TOOLS,
    )


async def _handle_ai_dialog_text(message: Message, state: FSMContext, text: str) -> None:
    text = text.strip()
    if not text:
        return
    if text.lower() in _AI_DIALOG_EXIT_WORDS:
        data = await state.get_data()
        bot_id = data.get("ai_dialog_bot_id")
        await state.clear()
        if bot_id is not None:
            b = await _bot_or_deny(message.from_user.id, bot_id)
            if b:
                b["username"] = await _ensure_username(b)
                await message.answer(_bot_text(b), parse_mode="HTML", reply_markup=_bot_keyboard(bot_id))
                return
        await message.answer("Диалог завершён.")
        return

    data = await state.get_data()
    bot_id = data.get("ai_dialog_bot_id")
    if bot_id is None:
        await state.clear()
        return
    b = await _bot_or_deny(message.from_user.id, bot_id)
    if not b:
        await state.clear()
        await message.answer("Бот не найден.")
        return

    history: list[tuple[str, str]] = list(data.get("ai_dialog_history") or [])
    history.append(("user", text))
    del history[:-_AI_DIALOG_CONTEXT_TURNS]

    try:
        response = await _ai_dialog_call_claude(bot_id, b, history)
    except Exception:
        logger.exception(f"ai_dialog: Claude call failed — bot_id={bot_id}")
        await message.answer("⚠️ Не удалось обработать запрос, попробуй ещё раз.")
        return

    text_block = next((blk for blk in response.content if blk.type == "text"), None)
    tool_block = next((blk for blk in response.content if blk.type == "tool_use"), None)

    if tool_block is not None and tool_block.name in ai_dialog.READONLY_TOOLS:
        outcome = await _ai_dialog_execute_tool(tool_block.name, bot_id, b)
        preview = ai_dialog.describe_tool_call(tool_block.name, b["name"])
        history.append(("assistant", f"[{preview}]"))
        del history[:-_AI_DIALOG_CONTEXT_TURNS]
        await state.update_data(ai_dialog_history=history)
        await message.answer(f"{preview}\n\n{outcome}", reply_markup=_ai_dialog_keyboard(bot_id))
        return

    if tool_block is not None:
        token = uuid.uuid4().hex[:12]
        preview = ai_dialog.describe_tool_call(tool_block.name, b["name"])
        history.append(("assistant", f"[Предложил действие: {preview}]"))
        del history[:-_AI_DIALOG_CONTEXT_TURNS]
        await state.update_data(
            ai_dialog_history=history,
            ai_dialog_pending={
                "token": token,
                "tool_name": tool_block.name,
                "created_at": time.monotonic(),
            },
        )
        await message.answer(
            f"{preview}\n\nПодтвердить выполнение?",
            reply_markup=_ai_dialog_confirm_keyboard(token),
        )
        return

    reply_text = text_block.text if text_block is not None else ""
    if not reply_text:
        reply_text = "..."
    history.append(("assistant", reply_text))
    del history[:-_AI_DIALOG_CONTEXT_TURNS]
    await state.update_data(ai_dialog_history=history)
    await message.answer(reply_text, reply_markup=_ai_dialog_keyboard(bot_id))


@router.message(AiDialogStates.chatting, F.voice)
async def msg_ai_dialog_voice(message: Message, state: FSMContext, bot: Bot):
    text = await _recognize_voice_fix(message, bot)
    if text:
        await _handle_ai_dialog_text(message, state, text)


@router.message(AiDialogStates.chatting, F.text, ~F.text.startswith("/"))
async def msg_ai_dialog_text(message: Message, state: FSMContext, bot: Bot):
    await _handle_ai_dialog_text(message, state, message.text)


@router.message(AiDialogStates.chatting)
async def msg_ai_dialog_unsupported(message: Message):
    await message.answer("Не понял — отправь текст или голосовое с описанием того, что хочешь сделать.")


@router.callback_query(F.data.startswith("aidok:"))
async def cb_aidialog_confirm(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    bot_id = data.get("ai_dialog_bot_id")
    pending = data.get("ai_dialog_pending")
    token = callback.data.split(":", 1)[1] if callback.data else ""

    if bot_id is None or not pending or pending.get("token") != token:
        await callback.answer("Действие уже выполнено, отменено или устарело.", show_alert=True)
        return

    # Claim (clear) the pending entry BEFORE any await below — same
    # TOCTOU-avoidance reason as features/group_task.py's cb_grouptask_confirm:
    # a double-tap must not be able to execute a non-idempotent action twice.
    await state.update_data(ai_dialog_pending=None)

    b = await _bot_or_deny(callback.from_user.id, bot_id)
    if not b:
        await callback.answer()
        await callback.message.answer("Бот не найден.")
        return

    if time.monotonic() - pending["created_at"] > _AI_DIALOG_PENDING_TTL_SECONDS:
        await callback.answer("Действие устарело — повтори запрос заново.", show_alert=True)
        try:
            await callback.message.edit_text("⏳ Действие устарело и было отменено автоматически.")
        except Exception:
            pass
        return

    if bot_id in _busy_bots:
        await callback.answer(_BUSY_TEXT, show_alert=True)
        return

    await callback.answer()
    _busy_bots.add(bot_id)
    try:
        outcome = await _ai_dialog_execute_tool(pending["tool_name"], bot_id, b)
    finally:
        _busy_bots.discard(bot_id)

    history: list[tuple[str, str]] = list(data.get("ai_dialog_history") or [])
    history.append(("user", f"[Владелец подтвердил действие. Результат: {outcome}]"))
    del history[:-_AI_DIALOG_CONTEXT_TURNS]
    await state.update_data(ai_dialog_history=history)

    try:
        await callback.message.edit_text(f"✅ {outcome}")
    except Exception:
        await callback.message.answer(f"✅ {outcome}")
    await callback.message.answer("Что ещё сделать?", reply_markup=_ai_dialog_keyboard(bot_id))


@router.callback_query(F.data.startswith("aidno:"))
async def cb_aidialog_cancel(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    pending = data.get("ai_dialog_pending")
    token = callback.data.split(":", 1)[1] if callback.data else ""
    bot_id = data.get("ai_dialog_bot_id")
    if pending and pending.get("token") == token:
        await state.update_data(ai_dialog_pending=None)
    await callback.answer("Отменено.")
    try:
        await callback.message.edit_text("❌ Отменено.")
    except Exception:
        pass
    if bot_id is not None:
        await callback.message.answer("Что ещё сделать?", reply_markup=_ai_dialog_keyboard(bot_id))
