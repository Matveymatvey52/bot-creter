"""Owner flow for custom_features/bot_<id>.py — point doctoring of ONE specific
bot, physically isolated from generated_bots/<name>.py so a future template
re-customization of the main file never clobbers it (see runtime/registry.py's
_load_and_include_custom_feature and services/claude_service.py's
generate_custom_feature/CUSTOM_FEATURE_SYSTEM_PROMPT for the rest of the design).

Flow: owner taps "🧩➕ Доработка" on a bot card -> describes the request
(voice/text) -> Claude generates a small additive Router module (main bot file
given as read-only context) -> syntax + forbidden-imports checked, with one
retry, inside generate_custom_feature itself -> Haiku translates the patch into
a plain-language preview -> owner taps "✅ Применить"/"❌ Отмена" -> on apply:
a content-hash freshness check, forbidden-imports re-check (defense in depth),
file written, isolated-import smoke-checked in a SEPARATE subprocess (never in
this process's own sys.modules), then the registry's per-bot_id module cache
is invalidated and reload_one() picks up the new router live.

This is the first pre-apply confirmation step in the codebase — every existing
Claude-driven code path (recreate/autofix/fixbug in handlers/manage_bots.py)
writes and restarts immediately with no preview at all.
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import os
import sys
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from db.database import add_custom_feature_record, get_bot, get_custom_feature_history, set_bot_miniapp_config
from handlers.admin_manager import _is_owner
from handlers.create_bot import cancel_keyboard
from handlers.manage_bots import _bot_keyboard, _busy_bots, _recognize_voice_fix
from runtime.registry import _CUSTOM_FEATURES_DIR, invalidate_custom_feature_cache
from runtime.registry_holder import RegistryHandle
from runtime.webhook_setup import set_miniapp_menu_button
from services.claude_service import (
    CustomFeatureGenerationError,
    _generate_miniapp_config,
    check_forbidden_imports,
    explain_custom_feature,
    generate_custom_feature,
)
from services.github_sync import push_bot_to_github

logger = logging.getLogger(__name__)

_DENY_TEXT = "⛔ Управление ботами доступно только владельцу."
_BUSY_TEXT = "⏳ Для этого бота уже выполняется операция — подожди её завершения."

# Loads a single file by path via importlib.util.spec_from_file_location — the
# SAME mechanism runtime/registry.py's own loader uses (see its module-level
# comment on _CUSTOM_FEATURES_DIR for why: DATA_DIR isn't guaranteed to sit
# under any sys.path entry, so a plain package-relative import wouldn't
# reliably find it there). Executed via `python -c <script> <module_path>` in
# a SEPARATE interpreter — never in this process's own sys.modules — so a
# broken generated module can't corrupt this process's own import state even
# transiently.
_ISOLATED_IMPORT_CHECK_SCRIPT = (
    "import importlib.util, sys\n"
    "spec = importlib.util.spec_from_file_location('smoke_check', sys.argv[1])\n"
    "module = importlib.util.module_from_spec(spec)\n"
    "spec.loader.exec_module(module)\n"
)

# Same live webhook Registry as handlers/manage_bots.py's own RegistryHandle —
# separate instance, separate set_registry(), same convention handlers/create_bot.py
# established first (each handler module that needs registry access owns its
# own pointer, wired up individually by runtime/combined_app.py's bootstrap).
_registry_handle = RegistryHandle()


def set_registry(registry) -> None:
    _registry_handle.set(registry)


class CustomFeatureStates(StatesGroup):
    describing_request = State()


router = Router()


async def _deny_message(message: Message) -> None:
    await message.answer(_DENY_TEXT)


async def _deny_callback(callback: CallbackQuery) -> None:
    await callback.answer(_DENY_TEXT, show_alert=True)


def _retry_keyboard(bot_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🧩➕ Попробовать снова", callback_data=f"customfeature:{bot_id}"),
        InlineKeyboardButton(text="◀ Назад", callback_data=f"info:{bot_id}"),
    ]])


def _preview_keyboard(bot_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Применить", callback_data=f"applycustom:{bot_id}"),
        InlineKeyboardButton(text="❌ Отмена", callback_data=f"cancelcustom:{bot_id}"),
    ]])


def _hash_main_code(main_code: str) -> str:
    return hashlib.sha256(main_code.encode("utf-8")).hexdigest()


@router.callback_query(F.data.startswith("customfeature:"))
async def cb_start_custom_feature(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(callback.from_user.id):
        await _deny_callback(callback)
        return
    bot_id = int(callback.data.split(":")[1])
    b = await get_bot(bot_id)
    if not b or not b.get("file_path") or not Path(b["file_path"]).exists():
        await callback.answer()
        await callback.message.edit_text("❌ Файл бота не найден — попробуй Перегенерировать.")
        return
    await callback.answer()
    await state.set_state(CustomFeatureStates.describing_request)
    # set_data (not update_data) — a stray tap of "🧩➕ Доработка" on a
    # DIFFERENT bot while a previous request/preview is still in this same
    # FSM slot must fully replace it, not merge on top of it and leave a
    # cf_pending_code for a bot_id no longer reflected by cf_bot_id.
    await state.set_data({"cf_bot_id": bot_id})
    await callback.message.edit_text(
        f"🧩➕ Доработка для <b>{html.escape(b['name'])}</b>\n\n"
        "Опиши, что добавить — голосовым или текстом. Это добавит новую возможность, "
        "не трогая то, что уже работает.",
        parse_mode="HTML",
        reply_markup=cancel_keyboard(),
    )


async def _generate_and_preview(message: Message, state: FSMContext, request_text: str) -> None:
    data = await state.get_data()
    bot_id = data.get("cf_bot_id")
    await state.clear()
    if bot_id is None:
        return

    if bot_id in _busy_bots:
        await message.answer(_BUSY_TEXT)
        return
    _busy_bots.add(bot_id)
    try:
        b = await get_bot(bot_id)
        if not b or not b.get("file_path") or not Path(b["file_path"]).exists():
            await message.answer("❌ Файл бота не найден — попробуй Перегенерировать.")
            return
        main_code = Path(b["file_path"]).read_text(encoding="utf-8")

        status_msg = await message.answer(f"🔧 Генерирую доработку для <b>{html.escape(b['name'])}</b>...", parse_mode="HTML")
        try:
            patch_code = await asyncio.wait_for(
                generate_custom_feature(main_code, request_text), timeout=240.0
            )
        except CustomFeatureGenerationError as e:
            logger.warning(f"_generate_and_preview: bot_id={bot_id} generation rejected: {e}")
            try:
                await status_msg.delete()
            except Exception:
                pass
            await message.answer(
                "⚠️ Не смог сгенерировать доработку без запрещённых библиотек или без синтаксических ошибок. "
                "Попробуй переформулировать запрос попроще.",
                reply_markup=_retry_keyboard(bot_id),
            )
            return
        except Exception:
            logger.exception(f"_generate_and_preview: bot_id={bot_id} generation failed")
            try:
                await status_msg.delete()
            except Exception:
                pass
            await message.answer(
                "⚠️ Не удалось сгенерировать доработку. Попробуй ещё раз.",
                reply_markup=_retry_keyboard(bot_id),
            )
            return

        try:
            explanation = await asyncio.wait_for(
                explain_custom_feature(patch_code, request_text), timeout=60.0
            )
        except Exception:
            logger.exception(f"_generate_and_preview: bot_id={bot_id} explain_custom_feature failed")
            explanation = "Доработка сгенерирована. Проверь и подтверди применение."

        try:
            await status_msg.delete()
        except Exception:
            pass

        # cf_main_code_hash lets the apply step (cb_apply_custom_feature)
        # detect if generated_bots/<name>.py changed since this patch was
        # generated against it (e.g. a /recreate or "Исправить баг" run
        # while this preview sat waiting for the owner) — the busy-lock
        # itself only covers THIS generation call, not the open-ended time
        # the preview then waits on the owner's tap, so this is the actual
        # guard against applying a patch against stale assumptions.
        await state.set_state(CustomFeatureStates.describing_request)
        await state.set_data({
            "cf_bot_id": bot_id,
            "cf_pending_code": patch_code,
            "cf_pending_request": request_text,
            "cf_main_code_hash": _hash_main_code(main_code),
        })
        preview_text = f"{explanation}\n\n<i>Проверь и подтверди применение.</i>"
        try:
            await message.answer(preview_text, parse_mode="HTML", reply_markup=_preview_keyboard(bot_id))
        except TelegramBadRequest:
            # explanation is Haiku's own free-form output — if it produced
            # something HTML can't parse (unbalanced tag, stray '<'), fall
            # back to plain text rather than losing the preview entirely.
            logger.warning(f"_generate_and_preview: bot_id={bot_id} preview HTML failed to parse, sending as plain text")
            await message.answer(preview_text, reply_markup=_preview_keyboard(bot_id))
    finally:
        _busy_bots.discard(bot_id)


@router.message(CustomFeatureStates.describing_request, F.voice)
async def msg_custom_feature_voice(message: Message, state: FSMContext, bot: Bot) -> None:
    if not _is_owner(message.from_user.id):
        await _deny_message(message)
        return
    data = await state.get_data()
    if "cf_pending_code" in data:
        # Same guard as msg_custom_feature_text below — a preview is already
        # sitting in front of the owner, don't let a voice message silently
        # overwrite it.
        await message.answer("Сначала подтверди или отмени текущую доработку кнопками выше.")
        return
    text = await _recognize_voice_fix(message, bot)
    if text:
        await _generate_and_preview(message, state, text)


@router.message(CustomFeatureStates.describing_request, F.text, ~F.text.startswith("/"))
async def msg_custom_feature_text(message: Message, state: FSMContext) -> None:
    if not _is_owner(message.from_user.id):
        await _deny_message(message)
        return
    data = await state.get_data()
    if "cf_pending_code" in data:
        # A preview is already sitting in front of the owner — a new text
        # message here is ambiguous (new request? more detail?) and would
        # silently overwrite cf_pending_code out from under the two buttons
        # already shown. Ask them to resolve the current preview first
        # instead of guessing (see backlog_create_flow_pending_race for why a
        # second concurrent "pending" quietly clobbering the first is exactly
        # the failure shape to avoid).
        await message.answer("Сначала подтверди или отмени текущую доработку кнопками выше.")
        return
    request_text = (message.text or "").strip()
    if not request_text:
        await message.answer("Пустое сообщение — опиши, что добавить.")
        return
    await _generate_and_preview(message, state, request_text)


@router.message(CustomFeatureStates.describing_request)
async def msg_custom_feature_fallback(message: Message, state: FSMContext) -> None:
    if not _is_owner(message.from_user.id):
        await _deny_message(message)
        return
    data = await state.get_data()
    if "cf_pending_code" in data:
        return  # preview pending — only the buttons or /cancel act on it
    await message.answer(
        "Не понял — отправь текст или голосовое с описанием доработки.",
        reply_markup=cancel_keyboard(),
    )


async def _regenerate_miniapp_config_after_custom_feature(
    bot_id: int, main_code: str, patch_code: str, description: str
) -> None:
    """Runs after a custom_features patch is applied — see the call site's
    comment. Never raises: _generate_miniapp_config already never raises, and
    a failure to persist here must not surface anywhere the owner would see
    it (this task is created fire-and-forget, nothing awaits it)."""
    try:
        combined_code = f"{main_code}\n\n# ── custom_features/bot_{bot_id}.py ──\n{patch_code}"
        miniapp_config = await _generate_miniapp_config(combined_code, description)
        if miniapp_config:
            await set_bot_miniapp_config(bot_id, miniapp_config)
            base_url = os.getenv("PUBLIC_BASE_URL", "").strip()
            if base_url:
                bot_row = await get_bot(bot_id)
                if bot_row and bot_row.get("token"):
                    await set_miniapp_menu_button(bot_row["token"], base_url, bot_id)
    except Exception as e:
        logger.warning(f"_regenerate_miniapp_config_after_custom_feature: bot_id={bot_id} failed: {type(e).__name__}: {e}")


@router.callback_query(F.data.startswith("applycustom:"))
async def cb_apply_custom_feature(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(callback.from_user.id):
        await _deny_callback(callback)
        return
    bot_id = int(callback.data.split(":")[1])
    # Check-then-add with NO await in between — closes a double-tap race a
    # prior version of this handler had (busy-check before await
    # callback.answer(), a real network suspension point, .add() only after;
    # two near-simultaneous taps could both pass the check before either
    # flagged itself busy). Mirrors handlers/manage_bots.py's cb_recreate,
    # which already adds to _busy_bots before its own callback.answer() for
    # the same reason.
    if bot_id in _busy_bots:
        await callback.answer(_BUSY_TEXT, show_alert=True)
        return
    _busy_bots.add(bot_id)
    try:
        await callback.answer()
        data = await state.get_data()
        if data.get("cf_bot_id") != bot_id or "cf_pending_code" not in data:
            await callback.message.edit_text("Доработка устарела — начни заново.")
            return
        patch_code = data["cf_pending_code"]
        request_text = data["cf_pending_request"]
        stored_hash = data.get("cf_main_code_hash")
        await state.clear()

        b = await get_bot(bot_id)
        if not b or not b.get("file_path") or not Path(b["file_path"]).exists():
            await callback.message.edit_text("❌ Файл бота не найден.")
            return

        # See _generate_and_preview's comment on cf_main_code_hash — this is
        # the actual guard against applying a patch generated against a main
        # file that has since changed (e.g. a /recreate while the preview sat
        # waiting for this tap). stored_hash is None only for FSM data that
        # predates this field (shouldn't happen in practice) — skip rather
        # than block in that case.
        current_main_code = Path(b["file_path"]).read_text(encoding="utf-8")
        if stored_hash is not None and _hash_main_code(current_main_code) != stored_hash:
            await callback.message.edit_text(
                "⚠️ Основной файл бота изменился с момента генерации этой доработки "
                "(например, через Перегенерировать или Исправить баг) — применять "
                "устаревший патч рискованно. Сгенерируй заново.",
                reply_markup=_retry_keyboard(bot_id),
            )
            return

        # Defense in depth (design point 5): re-check right before the write,
        # in case anything changed between generation and this tap. Cheap —
        # pure AST walk, no I/O. generate_custom_feature() already guarantees
        # patch_code parses, so a SyntaxError here would be a genuine surprise;
        # treated as a hard failure rather than silently proceeding.
        #
        # This check rejects UNVETTED imports only, not what an allowed one
        # (os/aiosqlite/aiohttp) can do once wired into the live Dispatcher —
        # see services/claude_service.py's _ALLOWED_ROOT_MODULES comment for
        # the real blast radius (env secrets, cross-bot token exfiltration
        # via the shared bots.db, arbitrary outbound HTTP).
        try:
            forbidden = check_forbidden_imports(patch_code)
        except SyntaxError:
            await callback.message.edit_text(
                "⚠️ Доработка повреждена (синтаксическая ошибка) — не применяю.",
                reply_markup=_retry_keyboard(bot_id),
            )
            return
        if forbidden:
            await callback.message.edit_text(
                f"⚠️ Доработка использует запрещённые библиотеки ({', '.join(forbidden)}) — не применяю.",
                reply_markup=_retry_keyboard(bot_id),
            )
            return

        _CUSTOM_FEATURES_DIR.mkdir(parents=True, exist_ok=True)
        module_path = _CUSTOM_FEATURES_DIR / f"bot_{bot_id}.py"
        module_path.write_text(patch_code, encoding="utf-8")

        # Isolated import smoke-check in a SEPARATE interpreter, spawned
        # non-blockingly (asyncio.create_subprocess_exec) — a synchronous
        # subprocess.run() here would freeze this process's ONE shared event
        # loop (runtime/combined_app.py serves every tenant bot's webhook on
        # it) for up to the full timeout, including any bot mid-payment-flow
        # with Telegram's hard 10s answerPreCheckoutQuery window. A broken
        # module must not enter THIS process's own sys.modules even
        # transiently — a subprocess that exits non-zero is cheap to discard.
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-c", _ISOLATED_IMPORT_CHECK_SCRIPT, str(module_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=15.0)
        except asyncio.TimeoutError:
            if proc is not None:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
            module_path.unlink(missing_ok=True)
            await callback.message.edit_text(
                "⚠️ Проверка доработки не уложилась в таймаут — не применяю.",
                reply_markup=_retry_keyboard(bot_id),
            )
            return

        if proc.returncode != 0:
            module_path.unlink(missing_ok=True)
            err = (stderr or b"").decode(errors="replace").strip()[-500:]
            await callback.message.edit_text(
                f"⚠️ Доработка не прошла проверку импорта:\n<code>{html.escape(err)}</code>\n\n"
                "Попробуй переформулировать запрос.",
                parse_mode="HTML",
                reply_markup=_retry_keyboard(bot_id),
            )
            return

        await add_custom_feature_record(bot_id, request_text)
        invalidate_custom_feature_cache(bot_id)
        if _registry_handle.value is not None:
            await _registry_handle.value.reload_one(bot_id)
        else:
            logger.debug(f"cb_apply_custom_feature: no live registry available — bot_id={bot_id} written to disk only")
        asyncio.create_task(push_bot_to_github(f"bot_{bot_id}", patch_code, subdir="custom_features"))

        # Regenerate miniapp_config (see docs/MINIAPP_DESIGN.md §6) — a
        # custom_features module can add its own tables (same DB_PATH as the
        # main bot, see CUSTOM_FEATURE_SYSTEM_PROMPT's aiosqlite guidance), so
        # the previously stored config can go stale relative to the real
        # schema. Fire-and-forget: never blocks or fails the apply itself —
        # same never-raises contract as generate_bot_code's own call to this
        # function. Combined source (main file + patch) so the schema scan
        # sees every real table, not just the main file's.
        asyncio.create_task(
            _regenerate_miniapp_config_after_custom_feature(bot_id, current_main_code, patch_code, b.get("description", ""))
        )

        await callback.message.edit_text(
            f"✅ Доработка для <b>{html.escape(b['name'])}</b> применена и подключена.",
            parse_mode="HTML",
            reply_markup=_bot_keyboard(bot_id),
        )
    finally:
        _busy_bots.discard(bot_id)


# bot_custom_features can grow unbounded over a bot's lifetime (one row per
# successful apply, never pruned) — cap what one message shows so it stays
# readable and can't approach Telegram's 4096-char message limit; the DB
# layer itself still returns the full history (get_custom_feature_history has
# no LIMIT), only this display is truncated.
_HISTORY_DISPLAY_LIMIT = 15
_HISTORY_DESCRIPTION_CHAR_LIMIT = 300


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


@router.callback_query(F.data.startswith("customfeaturehistory:"))
async def cb_custom_feature_history(callback: CallbackQuery) -> None:
    if not _is_owner(callback.from_user.id):
        await _deny_callback(callback)
        return
    bot_id = int(callback.data.split(":")[1])
    b = await get_bot(bot_id)
    if not b:
        await callback.answer()
        await callback.message.edit_text("❌ Бот не найден.")
        return
    await callback.answer()

    history = await get_custom_feature_history(bot_id)
    header = f"🕘 История доработок для <b>{html.escape(b['name'])}</b>\n\n"
    if not history:
        text = header + "Пока пусто — доработок ещё не применялось."
    else:
        shown = history[:_HISTORY_DISPLAY_LIMIT]
        lines = [
            f"• {html.escape(entry['applied_at'][:16])} — "
            f"{html.escape(_truncate(entry['description'], _HISTORY_DESCRIPTION_CHAR_LIMIT))}"
            for entry in shown
        ]
        footer = ""
        if len(history) > len(shown):
            footer = f"\n\n<i>Показаны последние {len(shown)} из {len(history)}.</i>"
        text = header + "\n".join(lines) + footer

    back_keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="◀ Назад", callback_data=f"info:{bot_id}"),
    ]])
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=back_keyboard)
    except TelegramBadRequest:
        # description is the owner's own free-form request text — if it
        # contains something HTML can't parse (stray '<', unbalanced tag),
        # fall back to plain text rather than losing the history view, same
        # pattern as _generate_and_preview's explanation fallback above.
        logger.warning(f"cb_custom_feature_history: bot_id={bot_id} history HTML failed to parse, sending as plain text")
        await callback.message.edit_text(text, reply_markup=back_keyboard)


@router.callback_query(F.data.startswith("cancelcustom:"))
async def cb_cancel_custom_feature(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_owner(callback.from_user.id):
        await _deny_callback(callback)
        return
    bot_id = int(callback.data.split(":")[1])
    await state.clear()
    await callback.answer()
    await callback.message.edit_text(
        "❌ Отменено — ничего не изменено.",
        reply_markup=_bot_keyboard(bot_id),
    )
