from __future__ import annotations

import asyncio
import io
import logging
import os
import tempfile
from pathlib import Path
from typing import NamedTuple
from urllib.parse import quote_plus

import aiohttp
from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import ASSEMBLYAI_API_KEY, BOT_TOKEN, DATA_DIR
from db.database import (
    add_miniapp_config_failure,
    add_office_link,
    add_template_candidate,
    create_bot_record_with_admins,
    enable_bot_feature,
    get_bot,
    get_bot_by_token,
    set_bot_display_name,
    set_bot_miniapp_config,
    set_bot_office_hook_config,
    set_bot_voice_cashflow_config,
    update_bot_status,
)
from runtime.registry_holder import RegistryHandle
from runtime.webhook_setup import build_webhook_url, set_miniapp_menu_button, set_webhook_for_bot
from services.bot_runner import start_bot
from services.claude_service import (
    chat_gather_requirements,
    extract_bot_name,
    generate_bot_code,
    generate_bot_guide,
    parse_gather_result,
)
from services.attachment_service import (
    MAX_DOCUMENT_SIZE_BYTES,
    MAX_PENDING_ATTACHMENTS,
    SUPPORTED_IMAGE_MIME,
    build_image_block,
    build_text_block,
    extract_document_text,
)
from services.github_sync import push_bot_to_github
from services.link_context import (
    OAUTH_ENABLED,
    SHEET_SHARE_INSTRUCTIONS,
    LinkKind,
    extract_urls,
    resolve_link,
)
from services.telegram_api import get_managed_bot_token
from services.voice_service import transcribe_voice

router = Router()
logger = logging.getLogger(__name__)

GENERATED_BOTS_DIR = DATA_DIR / "generated_bots"
GENERATED_BOTS_DIR.mkdir(exist_ok=True)

BOT_IMAGES_DIR = DATA_DIR / "bot_images"
BOT_IMAGES_DIR.mkdir(exist_ok=True)

AVATAR_DIR = DATA_DIR / "bot_avatars"
AVATAR_DIR.mkdir(exist_ok=True)

_manager_username: str = ""
_bot_id: int = 0

# user_id -> pending bot creation data
_pending: dict[int, dict] = {}


def set_manager_username(username: str) -> None:
    global _manager_username
    _manager_username = username


def set_bot_id(bid: int) -> None:
    global _bot_id
    _bot_id = bid


# The live webhook Registry, set once by runtime/combined_app.py's bootstrap —
# only present when this router is running inside the combined process (Stage
# 2's "фабрика как житель реестра"). Still None when running under main.py's
# separate long-polling process, since no Registry exists there — new bots
# then rely purely on services.bot_runner.start_bot() (subprocess model,
# unchanged) to actually respond, exactly as they do today. See
# runtime/registry_holder.py for why this is a RegistryHandle instead of a
# bare module global.
_registry_handle = RegistryHandle()


def set_registry(registry) -> None:
    _registry_handle.set(registry)


async def _register_new_bot_in_registry(bot_id: int, bot_name: str) -> None:
    """Best-effort: registers a freshly-created bot into the live registry
    (direct in-process call — see runtime/registry.py's Registry.add_or_replace,
    untouched by this phase) so it can answer webhook traffic immediately,
    without waiting for a manual /admin/reload/{id}. Never raises — a failure
    here must not abort bot creation, since services.bot_runner.start_bot()
    (subprocess model) is what actually makes the bot respond today regardless
    of registry state; a bot present in the DB but missing from the registry
    is recoverable later via a manual reload, not a data-loss scenario."""
    if _registry_handle.value is None:
        logger.debug(
            f"No live registry available (polling-only process) — bot id={bot_id} "
            f"({bot_name}) not registered; use /admin/reload/{bot_id} once the "
            "combined app is running, if that ever applies."
        )
        return
    try:
        fresh_row = await get_bot(bot_id)
        if fresh_row is None:
            logger.error(f"Registry registration skipped for bot id={bot_id} ({bot_name}) — row vanished after creation")
            return
        entry = await _registry_handle.value.add_or_replace(fresh_row)
        if entry is None:
            logger.warning(
                f"Bot id={bot_id} ({bot_name}) created but registry registration failed — "
                f"it will only answer webhook traffic after a manual /admin/reload/{bot_id}"
            )
        else:
            logger.info(f"Bot id={bot_id} ({bot_name}) registered in the live registry")
    except Exception as e:
        logger.error(f"Registry registration raised for bot id={bot_id} ({bot_name}): {e}")


async def _apply_voice_cashflow_config(bot_id: int, voice_cashflow_config: dict | None) -> None:
    """Persists voice_cashflow_config (if non-null) and auto-enables the
    voice_intake/cashflow_ledger bot_features rows it implies — see docs/
    VOICE_CASHFLOW_FROM_SCRATCH_DESIGN.md. Auto-enabled rather than left for
    the owner to manually toggle: the owner has no way to discover or enable
    a feature for a from-scratch bot through the existing Features UI unless
    it's already been generated as relevant for that specific bot (see the
    _compatible_features per-bot-instance fix in handlers/manage_bots.py).

    Called BEFORE _register_new_bot_in_registry so the bot_features rows
    already exist the first time this bot is registered in the live
    registry — _load_and_include_features reads bot_features fresh on every
    registration/reload, so ordering here only affects whether the very
    first registration already has voice_intake/cashflow_ledger wired, not
    whether they eventually get wired at all (a later /admin/reload would
    pick them up regardless)."""
    if not voice_cashflow_config:
        return
    await set_bot_voice_cashflow_config(bot_id, voice_cashflow_config)
    if voice_cashflow_config.get("voice_intake"):
        await enable_bot_feature(bot_id, "voice_intake")
    if voice_cashflow_config.get("cashflow_ledger"):
        await enable_bot_feature(bot_id, "cashflow_ledger")


class _Activation(NamedTuple):
    """Outcome of bringing a freshly-created bot online (see _activate_new_bot).
    outcome ∈ {webhook_ok, webhook_failed, webhook_no_secret, polling_ok, polling_failed}."""
    outcome: str
    pid: int | None = None


async def _activate_new_bot(
    bot_id: int, bot_name: str, bot_file: Path, token: str, extra_env: dict | None, has_miniapp: bool = False
) -> _Activation:
    """Brings a freshly-created bot online, choosing the mechanism by runtime mode.
    Never raises — an activation failure must not undo an already-created bot (it is
    already in the DB and, in webhook mode, in the live registry via add_or_replace()).

    Webhook mode (PUBLIC_BASE_URL set — combined_app in prod): register the bot's
    Telegram webhook at /webhook/<bot_id> (served by the live registry) and do NOT
    start a polling subprocess. Webhook and long-polling are mutually exclusive ways
    to receive updates for one token — running both makes Telegram 409 the polling
    side. If WEBHOOK_SECRET is unset, a registered webhook would immediately get 403
    from the fail-closed handler (runtime/webhook_app.py), so registration is skipped;
    the bot stays in the registry and will serve once the secret is configured and the
    webhook registered manually (see docs/WEBHOOK_ACTIVATION.md). We still do NOT start
    polling in that case — we are in webhook mode.

    Polling / standalone mode (PUBLIC_BASE_URL empty — main.py): start_bot() as before.
    """
    base_url = os.getenv("PUBLIC_BASE_URL", "").strip()
    if base_url and has_miniapp:
        try:
            await set_miniapp_menu_button(token, base_url, bot_id)
        except Exception as e:
            logger.error(f"Bot id={bot_id} ({bot_name}) created but Menu Button setup failed: {e}")
    if base_url:
        secret = os.getenv("WEBHOOK_SECRET", "").strip()
        if not secret:
            logger.warning(
                f"Bot id={bot_id} ({bot_name}) created in webhook mode, but WEBHOOK_SECRET "
                "is not set — skipping webhook registration (a webhook without a matching "
                "secret is rejected 403 by the fail-closed handler). Not starting a polling "
                "subprocess either (webhook mode). Register manually once the secret is set "
                "(see docs/WEBHOOK_ACTIVATION.md)."
            )
            return _Activation("webhook_no_secret")
        try:
            await set_webhook_for_bot(token, base_url, bot_id, secret)
        except Exception as e:
            logger.error(f"Bot id={bot_id} ({bot_name}) created but webhook registration failed: {e}")
            return _Activation("webhook_failed")
        # Webhook is LIVE from here. A failure writing the DB status must NOT be
        # reported as webhook_failed (that would tell the user the webhook isn't
        # registered while it actually is, and leave the row at 'stopped'), so the
        # status write is guarded separately, outside the set_webhook try.
        try:
            await update_bot_status(bot_id, "running")
        except Exception as e:
            logger.error(f"Bot id={bot_id} ({bot_name}) webhook registered but status update failed: {e}")
        logger.info(
            f"Bot id={bot_id} ({bot_name}) webhook registered at {build_webhook_url(base_url, bot_id)}"
        )
        return _Activation("webhook_ok")

    try:
        pid = await start_bot(bot_id, str(bot_file), token, extra_env=extra_env or None)
        await update_bot_status(bot_id, "running", pid)
        return _Activation("polling_ok", pid)
    except Exception as e:
        logger.error(f"Failed to start bot {bot_id}: {e}")
        # Guarded: _activate_new_bot promises never to raise (an activation failure
        # must not undo an already-created bot), so a failing status write here must
        # not propagate out either.
        try:
            await update_bot_status(bot_id, "error")
        except Exception as status_err:
            logger.error(f"Also failed to mark bot {bot_id} as error: {status_err}")
        return _Activation("polling_failed")


_ADMIN_BLOCK = (
    "<b>👥 Управление администраторами</b>\n"
    "Команды пишутся прямо в созданный бот:\n"
    "<code>/admins</code> — список администраторов\n"
    "<code>/addadmin 123456789</code> — добавить администратора\n"
    "<code>/removeadmin 123456789</code> — убрать администратора\n\n"
    "💡 Узнать Telegram ID: попроси человека написать боту @userinfobot\n\n"
    "Управление ботом: /list"
)


async def _notify_miniapp_config_failure(bot: Bot, chat_id: int, bot_name: str) -> None:
    """Follow-up message sent right after _notify_bot_created when
    generate_bot_code's miniapp_failure_info is non-None — i.e. mini-app
    generation genuinely failed (API error/timeout/parse/validation, after
    its own retry), not the valid "this bot needs no mini-app" case. The
    owner would otherwise have no way to notice their bot silently has no
    mini-app besides stumbling onto it themselves; the failure is also
    logged via add_miniapp_config_failure for the /analytics dashboard, but
    a proactive nudge here is cheap and matches this bot's own creation
    flow being the moment the owner is already paying attention.

    Wrapped in try/except — same posture as the real_username fetch above:
    a notification failure must never break bot creation, which has already
    fully succeeded by the time this runs."""
    try:
        await bot.send_message(
            chat_id,
            f"⚠️ У бота <b>{bot_name}</b> не получилось создать мини-апп автоматически — нужна ручная проверка.",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning(f"_notify_miniapp_config_failure: bot_name={bot_name} send failed: {type(e).__name__}: {e}")


async def _notify_bot_created(
    bot: Bot, chat_id: int, bot_id: int, bot_name: str, bot_summary: str,
    username_display: str, activation: _Activation,
) -> None:
    """Sends the creator the outcome message for a freshly-created bot. Shared by
    both creation paths (auto_launch_managed_bot, handle_token) so the webhook /
    polling / failure wording lives in one place."""
    if activation.outcome in ("polling_ok", "webhook_ok"):
        try:
            guide = await generate_bot_guide(bot_name, bot_summary)
        except Exception:
            guide = ""
        launched = "запущен" if activation.outcome == "polling_ok" else "подключён по вебхуку"
        text = (
            f"✅ Бот <b>{bot_name}</b>{username_display} создан и {launched}!\n\n"
            "Вы являетесь администратором этого бота.\n\n"
        )
        if guide:
            text += guide + "\n\n"
        text += _ADMIN_BLOCK
        await bot.send_message(chat_id, text, parse_mode="HTML")
        return

    if activation.outcome in ("webhook_failed", "webhook_no_secret"):
        detail = (
            "вебхук не зарегистрирован" if activation.outcome == "webhook_failed"
            else "вебхук не настроен (не задан секрет)"
        )
        await bot.send_message(
            chat_id,
            f"✅ Бот <b>{bot_name}</b>{username_display} создан, но {detail} 😔\n\n"
            "Бот сохранён — обратитесь к администратору, чтобы завершить подключение.",
            parse_mode="HTML",
        )
        return

    # polling_failed
    await bot.send_message(
        chat_id,
        f"Бот <b>{bot_name}</b>{username_display} создан, но не смог запуститься 😔\n\n"
        "Попробуй удалить и создать заново.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🗑 Удалить бота", callback_data=f"delete:{bot_id}")
        ]]),
    )


async def _clear_user_fsm(storage, user_id: int) -> None:
    if not storage or not _bot_id or not user_id:
        return
    try:
        from aiogram.fsm.storage.base import StorageKey
        key = StorageKey(bot_id=_bot_id, chat_id=user_id, user_id=user_id)
        await storage.set_state(key=key, state=None)
        await storage.set_data(key=key, data={})
    except Exception as e:
        logger.warning(f"Could not clear FSM state for user {user_id}: {e}")


async def _set_bot_profile_photo(token: str, photo_path: str) -> None:
    try:
        async with aiohttp.ClientSession() as session:
            with open(photo_path, "rb") as f:
                form = aiohttp.FormData()
                form.add_field("photo", f, filename="avatar.jpg", content_type="image/jpeg")
                async with session.post(
                    f"https://api.telegram.org/bot{token}/setMyProfilePhoto",
                    data=form,
                ) as resp:
                    result = await resp.json()
                    if not result.get("ok"):
                        logger.warning(f"setMyProfilePhoto failed: {result}")
    except Exception as e:
        logger.warning(f"Could not set profile photo: {e}")


class CreateBotStates(StatesGroup):
    gathering = State()
    confirming = State()
    waiting_for_display_name = State()
    waiting_for_welcome_photo = State()
    waiting_for_avatar_photo = State()
    waiting_for_token = State()


# ── /cancel ───────────────────────────────────────────────────────────────────

def cancel_keyboard() -> InlineKeyboardMarkup:
    """Inline-button equivalent of typing /cancel — routes into the same
    cb_cancel handler as cmd_cancel below, so every FSM state factory-wide
    (create-bot, fixbug, custom-feature, ...) gets a tappable cancel instead
    of a raw command."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_fsm")
    ]])


def _cancelled_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀ В главное меню", callback_data="start_menu")],
        [InlineKeyboardButton(text="📋 Мои боты", callback_data="list")],
    ])


async def _do_cancel(user_id: int, state: FSMContext, answer) -> None:
    if await state.get_state() is None:
        await answer("Нечего отменять.")
        return
    _pending.pop(user_id, None)
    await state.clear()
    await answer("Отменено.", reply_markup=_cancelled_keyboard())


@router.message(Command("cancel"), StateFilter("*"))
async def cmd_cancel(message: Message, state: FSMContext):
    await _do_cancel(message.from_user.id, state, message.answer)


@router.callback_query(F.data == "cancel_fsm", StateFilter("*"))
async def cb_cancel(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await _do_cancel(callback.from_user.id, state, callback.message.answer)


# ── /create ───────────────────────────────────────────────────────────────────

async def _start_create_flow(user_id: int, answer, state: FSMContext) -> None:
    """Shared by /create (Message) and the "➕ Создать бота" button on /start
    (CallbackQuery) — callers pass their own user_id and an answer() sink
    (message.answer / callback.message.answer) so this never reads
    callback.message.from_user, which is the BOT, not the presser."""
    dropped = _pending.pop(user_id, None)
    await state.clear()
    await state.set_state(CreateBotStates.gathering)
    await state.update_data(conversation=[], pending_attachments=[])
    if dropped is not None:
        await answer(
            "⚠️ Предыдущее создание бота (ожидавшее токен от BotFather) отменено — "
            "начинаем новое. В следующий раз используйте /cancel, если хотите "
            "прервать создание осознанно."
        )
    await answer(
        "Вы можете описать текстом или записать голосовое, в котором расскажете "
        "подробно какого бота или систему ботов вы хотите сделать. А также можете "
        "присылать мне скриншоты, документы, ссылки на таблицы и т.д., чтобы я "
        "лучше понял, что мы будем автоматизировать😉"
    )


@router.message(Command("create"))
async def cmd_create(message: Message, state: FSMContext):
    await _start_create_flow(message.from_user.id, message.answer, state)


@router.callback_query(F.data == "start_create")
async def cb_start_create(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await _start_create_flow(callback.from_user.id, callback.message.answer, state)


# ── gathering ─────────────────────────────────────────────────────────────────

async def _recognize_voice(message: Message, bot: Bot) -> str | None:
    if not ASSEMBLYAI_API_KEY:
        await message.answer("⚠️ Распознавание голосовых не настроено. Напишите текстом.")
        return None
    status_msg = await message.answer("🎤 Распознаю голосовое...")
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        file = await bot.get_file(message.voice.file_id)
        await bot.download_file(file.file_path, destination=tmp_path)
        # transcribe_voice runs AssemblyAI's synchronous SDK (its own internal
        # polling, no timeout of its own) in a default-executor thread — a
        # stalled poll would otherwise hang this handler (and leak the
        # executor thread) forever. The wait_for gives the *caller* a bound;
        # the leaked thread itself is a separate, harder fix (SDK has no
        # cancellation hook), out of scope here.
        text = await asyncio.wait_for(transcribe_voice(tmp_path), timeout=120.0)
    except asyncio.TimeoutError:
        logger.error("Voice transcription timed out after 120s")
        try:
            await status_msg.delete()
        except Exception:
            pass
        await message.answer("Не удалось распознать голосовое (слишком долго) 😔 Попробуйте текстом.")
        return None
    except Exception as e:
        logger.error(f"Voice transcription failed: {e}")
        try:
            await status_msg.delete()
        except Exception:
            pass
        await message.answer("Не удалось распознать голосовое 😔 Попробуйте текстом.")
        return None
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    try:
        await status_msg.delete()
    except Exception:
        pass
    if not text.strip():
        await message.answer("Не удалось разобрать голосовое, попробуйте ещё раз.")
        return None
    await message.answer(f"🎤 Распознал: _{text}_", parse_mode="Markdown")
    return text


_GATHER_STATES = StateFilter(CreateBotStates.gathering, CreateBotStates.confirming)


def _confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Начать генерацию", callback_data="confirm_generate")
    ]])


def _continue_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="▶️ Продолжить без текста", callback_data="gathering_continue")
    ]])


@router.message(_GATHER_STATES, F.voice)
async def handle_gathering_voice(message: Message, state: FSMContext, bot: Bot):
    text = await _recognize_voice(message, bot)
    if text:
        await _process_gathering_content(message, state, text)


@router.message(_GATHER_STATES, F.text, ~F.text.startswith("/"))
async def handle_gathering(message: Message, state: FSMContext):
    await _process_gathering_content(message, state, message.text)


@router.message(_GATHER_STATES, F.photo)
async def handle_gathering_photo(message: Message, state: FSMContext, bot: Bot):
    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    buf = io.BytesIO()
    await bot.download_file(file.file_path, destination=buf)
    block = build_image_block(buf.getvalue(), "image/jpeg")
    await _add_pending_attachment(message, state, block, "📎 Принял скриншот")


@router.message(_GATHER_STATES, F.document)
async def handle_gathering_document(message: Message, state: FSMContext, bot: Bot):
    doc = message.document
    if doc.file_size and doc.file_size > MAX_DOCUMENT_SIZE_BYTES:
        await message.answer("Файл слишком большой (максимум 15 МБ). Пришлите файл поменьше.")
        return

    file = await bot.get_file(doc.file_id)
    buf = io.BytesIO()
    await bot.download_file(file.file_path, destination=buf)
    data = buf.getvalue()

    if doc.mime_type in SUPPORTED_IMAGE_MIME:
        block = build_image_block(data, doc.mime_type)
        await _add_pending_attachment(message, state, block, "📎 Принял изображение")
        return

    try:
        extracted = extract_document_text(data, doc.file_name or "")
    except Exception:
        await message.answer("Не удалось прочитать этот файл 😔 Попробуйте другой формат.")
        return

    if extracted is None:
        await message.answer(
            "Пока умею читать только PDF, Word (.docx) и Excel (.xlsx/.xls). "
            "Пришлите файл в одном из этих форматов или опишите содержимое текстом."
        )
        return

    block = build_text_block(f"[Документ: {doc.file_name}]\n{extracted}")
    await _add_pending_attachment(message, state, block, f"📎 Принял документ «{doc.file_name}»")


async def _add_pending_attachment(message: Message, state: FSMContext, block: dict, ack: str) -> None:
    data = await state.get_data()
    pending: list[dict] = data.get("pending_attachments", [])
    if len(pending) >= MAX_PENDING_ATTACHMENTS:
        await message.answer(
            f"Уже накопил {MAX_PENDING_ATTACHMENTS} вложений — напишите текст, "
            "чтобы я их обработал, прежде чем присылать ещё."
        )
        return
    pending.append(block)
    await state.update_data(pending_attachments=pending)
    await message.answer(
        f"{ack} ({len(pending)}). Жду ещё вложения или текст/голосовое с описанием — "
        "или нажмите «Продолжить».",
        reply_markup=_continue_keyboard(),
    )


@router.callback_query(F.data == "gathering_continue", _GATHER_STATES)
async def cb_gathering_continue(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    pending: list[dict] = data.get("pending_attachments", [])
    if not pending:
        await callback.message.answer("Вложений нет — напишите текст или пришлите файл.")
        return
    await _process_gathering_content(callback.message, state, None)


def _sheet_auth_choice_keyboard(url: str) -> InlineKeyboardMarkup:
    oauth_text = "🔗 Подключить Google-аккаунт" if OAUTH_ENABLED else "🔗 Google-аккаунт (скоро)"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📎 Как открыть доступ по ссылке", callback_data="sheet_share_howto")],
        [InlineKeyboardButton(text=oauth_text, callback_data="sheet_oauth_soon")],
    ])


@router.callback_query(F.data == "sheet_share_howto")
async def cb_sheet_share_howto(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(SHEET_SHARE_INSTRUCTIONS)


@router.callback_query(F.data == "sheet_oauth_soon")
async def cb_sheet_oauth_soon(callback: CallbackQuery):
    await callback.answer(
        "Подключение Google-аккаунта появится в одном из следующих обновлений. "
        "Пока проще всего дать доступ по ссылке.",
        show_alert=True,
    )


async def _resolve_message_links(message: Message, text: str) -> list[dict]:
    """Stage D: detect URLs in the gathering message, fetch context for them
    (CSV summary for public Google Sheets, page text otherwise), and return
    extra text blocks to append to the pending attachment buffer (same
    {"type": "text", ...} shape build_text_block produces for documents —
    see services/attachment_service.py's docstring, which anticipated this).
    Sends its own status/error messages; returns [] if nothing usable was found."""
    urls = extract_urls(text)
    if not urls:
        return []

    link_blocks: list[dict] = []
    for url in urls[:3]:  # cap to avoid pathological multi-link spam
        result = await resolve_link(url)
        if result.ok:
            label = "Google Sheet" if result.kind == LinkKind.GOOGLE_SHEET else "Webpage"
            link_blocks.append(build_text_block(f"[Context from {label} {url}]\n{result.context}"))
        elif result.kind == LinkKind.GOOGLE_SHEET and result.error == "not_public":
            await message.answer(
                SHEET_SHARE_INSTRUCTIONS,
                reply_markup=_sheet_auth_choice_keyboard(url),
            )
        else:
            await message.answer(f"⚠️ Не удалось прочитать ссылку {url}, продолжаю без неё.")

    return link_blocks


async def _process_gathering_content(message: Message, state: FSMContext, text: str | None) -> None:
    data = await state.get_data()
    conversation: list[dict] = data.get("conversation", [])
    pending: list[dict] = data.get("pending_attachments", [])

    blocks = list(pending)
    if text:
        blocks.append(build_text_block(text))
        blocks.extend(await _resolve_message_links(message, text))
    if not blocks:
        return

    conversation.append({"role": "user", "content": blocks})
    # Persisted BEFORE the Claude call (not just on success) so a failed call
    # can be retried from state without re-sending the user's message — same
    # reasoning as _run_generation storing bot_summary/bot_name ahead of its
    # own asyncio.wait_for call.
    await state.update_data(pending_attachments=[], conversation=conversation)

    await _call_gather_requirements(message.bot, message.chat.id, state)


async def _call_gather_requirements(bot: Bot, chat_id: int, state: FSMContext) -> None:
    """Calls chat_gather_requirements on the conversation already persisted in
    state and routes the result, same asyncio.wait_for + try/except + retry
    button shape as _run_generation's generate_bot_code call — this was
    previously the one Claude call in the /create flow with no error handling
    at all, leaving the FSM stuck in `gathering` on any API failure."""
    data = await state.get_data()
    conversation: list[dict] = data.get("conversation", [])

    analyzing_msg = await bot.send_message(chat_id, "Анализирую... ⏳")
    try:
        response = await asyncio.wait_for(chat_gather_requirements(conversation), timeout=120.0)
    except asyncio.TimeoutError:
        logger.error("chat_gather_requirements timed out after 120s")
        try:
            await analyzing_msg.delete()
        except Exception:
            pass
        await bot.send_message(
            chat_id,
            "⏱ Не получилось обработать сообщение (слишком долго). Попробуй ещё раз.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🔄 Попробовать снова", callback_data="retry_gather")
            ]]),
        )
        return
    except Exception as e:
        logger.error(f"chat_gather_requirements failed: {type(e).__name__}: {e}")
        try:
            await analyzing_msg.delete()
        except Exception:
            pass
        await bot.send_message(
            chat_id,
            f"⚠️ Не удалось обработать сообщение ({type(e).__name__}).\n\n"
            "Нажми кнопку чтобы попробовать снова.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🔄 Попробовать снова", callback_data="retry_gather")
            ]]),
        )
        return

    conversation.append({"role": "assistant", "content": response})
    try:
        await analyzing_msg.delete()
    except Exception:
        pass

    if "===READY_TO_GENERATE===" in response:
        parts = response.split("===READY_TO_GENERATE===")
        raw_payload = parts[1].strip() if len(parts) > 1 else response
        plan = parse_gather_result(raw_payload)
        if plan is None:
            await state.update_data(conversation=conversation)
            await bot.send_message(chat_id, response)
            return

        office_bots = plan["bots"]
        office_links = plan["links"]
        first_summary = office_bots[0]["summary"]
        try:
            bot_name = await extract_bot_name(first_summary)
        except Exception:
            bot_name = "my_bot"

        await state.update_data(
            conversation=conversation,
            bot_summary=first_summary,
            bot_name=bot_name,
            office_bots=office_bots,
            office_links=office_links,
            office_index=0,
            office_bot_ids={},
        )
        await state.set_state(CreateBotStates.confirming)
        if len(office_bots) > 1:
            roles = ", ".join(b["role_hint"] for b in office_bots)
            link_descriptions = [
                f"{link['source_role_hint']}→{link['target_role_hint']} ({link['event_type']})"
                for link in office_links
            ]
            links_note = f"\n🔗 Свяжу их через: {', '.join(link_descriptions)}" if link_descriptions else ""
            await bot.send_message(
                chat_id,
                f"Я готов создавать офис из {len(office_bots)} ботов: {roles}.{links_note}\n\n"
                f"Начнём с первого («{office_bots[0]['role_hint']}»). Вам есть ещё что добавить "
                "по всему офису: какие-нибудь детали, нюансы, пожелания?🤗",
                reply_markup=_confirm_keyboard(),
            )
        else:
            await bot.send_message(
                chat_id,
                "Я готов создавать бота по вашему запросу. Вам есть ещё что добавить: "
                "какие-нибудь детали, нюансы, пожелания?🤗",
                reply_markup=_confirm_keyboard(),
            )
    else:
        await state.update_data(conversation=conversation)
        await bot.send_message(chat_id, response)


@router.callback_query(F.data == "retry_gather")
async def cb_retry_gather(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await _call_gather_requirements(callback.bot, callback.message.chat.id, state)


def _office_progress_prefix(data: dict) -> str:
    """"Бот 2 из 3: accounting" prefix shown ahead of every per-bot onboarding
    question once an office/multi-bot plan is active (docs/
    MULTIBOT_OFFICE_ROUTING_DESIGN.md §4 q1: owner confirmed the BotFather
    step must repeat per bot, with a progress indicator). Empty string for
    the single-bot case, so existing single-bot messages are unchanged."""
    office_bots: list[dict] = data.get("office_bots") or []
    if len(office_bots) <= 1:
        return ""
    index: int = data.get("office_index", 0)
    role_hint = office_bots[index]["role_hint"] if index < len(office_bots) else "?"
    return f"🏢 Бот {index + 1} из {len(office_bots)}: {role_hint}\n\n"


@router.callback_query(F.data == "confirm_generate", CreateBotStates.confirming)
async def cb_confirm_generate(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(CreateBotStates.waiting_for_display_name)
    data = await state.get_data()
    await callback.message.answer(
        _office_progress_prefix(data) +
        "Отлично! Осталось пару вопросов.\n\n"
        "👤 *Как будут звать этого бота?*\n"
        "Например: Макс, Катя, Алекс — это имя для общения в групповом чате.\n\n"
        "Напишите имя или /skip чтобы пропустить.",
        parse_mode="Markdown",
    )


# ── waiting_for_display_name ──────────────────────────────────────────────────

@router.message(CreateBotStates.waiting_for_display_name, F.text, ~F.text.startswith("/"))
async def handle_display_name(message: Message, state: FSMContext):
    await state.update_data(display_name=message.text.strip())
    await _ask_welcome_photo(message, state)


@router.message(CreateBotStates.waiting_for_display_name, Command("skip"))
async def handle_display_name_skip(message: Message, state: FSMContext):
    await state.update_data(display_name="")
    await _ask_welcome_photo(message, state)


async def _ask_welcome_photo(message: Message, state: FSMContext):
    await state.set_state(CreateBotStates.waiting_for_welcome_photo)
    await message.answer(
        "📸 *Приветственное фото для бота*\n"
        "Эта картинка будет показываться пользователям при /start.\n\n"
        "Отправьте фото или /skip чтобы пропустить.",
        parse_mode="Markdown",
    )


# ── waiting_for_welcome_photo ─────────────────────────────────────────────────

@router.message(CreateBotStates.waiting_for_welcome_photo, F.photo)
async def handle_welcome_photo(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    bot_name = data.get("bot_name", "bot")
    BOT_IMAGES_DIR.mkdir(exist_ok=True)
    path = BOT_IMAGES_DIR / f"{bot_name}.jpg"
    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    await bot.download_file(file.file_path, destination=str(path))
    await state.set_state(CreateBotStates.waiting_for_avatar_photo)
    await message.answer(
        "✅ Фото сохранено!\n\n"
        "🖼 *Аватарка бота* (кружок рядом с именем)\n"
        "Отправьте фото или /skip (можно изменить позже через BotFather).",
        parse_mode="Markdown",
    )


@router.message(CreateBotStates.waiting_for_welcome_photo, Command("skip"))
async def handle_welcome_photo_skip(message: Message, state: FSMContext):
    await state.set_state(CreateBotStates.waiting_for_avatar_photo)
    await message.answer(
        "🖼 *Аватарка бота* (кружок рядом с именем)\n"
        "Отправьте фото или /skip (можно изменить позже через BotFather).",
        parse_mode="Markdown",
    )


@router.message(CreateBotStates.waiting_for_welcome_photo, F.text, ~F.text.startswith("/"))
async def handle_welcome_photo_invalid(message: Message):
    await message.answer("Отправьте фото или напишите /skip")


# ── waiting_for_avatar_photo ──────────────────────────────────────────────────

@router.message(CreateBotStates.waiting_for_avatar_photo, F.photo)
async def handle_avatar_photo(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    bot_name = data.get("bot_name", "bot")
    AVATAR_DIR.mkdir(exist_ok=True)
    path = AVATAR_DIR / f"{bot_name}.jpg"
    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    await bot.download_file(file.file_path, destination=str(path))
    await _generate_and_show_button(message, state)


@router.message(CreateBotStates.waiting_for_avatar_photo, Command("skip"))
async def handle_avatar_skip(message: Message, state: FSMContext):
    await _generate_and_show_button(message, state)


@router.message(CreateBotStates.waiting_for_avatar_photo, F.text, ~F.text.startswith("/"))
async def handle_avatar_invalid(message: Message):
    await message.answer("Отправьте фото или напишите /skip")


async def _generate_and_show_button(message: Message, state: FSMContext) -> None:
    await _run_generation(
        chat_id=message.chat.id,
        user_id=message.from_user.id,
        bot=message.bot,
        state=state,
    )


def _first_user_message(conversation: list[dict]) -> str | None:
    """The original free-text request that kicked off this /create flow —
    the FIRST role="user" entry in the gathering conversation (see
    _process_gathering_content), before any Claude clarification turned it into
    bot_summary/description. content is either a plain string or a list of
    Claude content blocks (text/image) since attachments were added — in the
    latter case this pulls the first text block. None if the conversation is
    empty/malformed or the first turn has no text at all."""
    for turn in conversation:
        if isinstance(turn, dict) and turn.get("role") == "user":
            content = turn.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        return block.get("text")
                return None
            return None
    return None


async def _run_generation(chat_id: int, user_id: int, bot: Bot, state: FSMContext) -> None:
    data = await state.get_data()
    summary: str = data.get("bot_summary", "")
    bot_name: str = data.get("bot_name", "my_bot")
    creation_prompt = _first_user_message(data.get("conversation", []))

    if not summary:
        await bot.send_message(
            chat_id,
            "⚠️ Данные о боте потеряны (бот мог перезапуститься).\n\nПожалуйста, начните заново с /create.",
        )
        await state.clear()
        return

    gen_msg = await bot.send_message(chat_id, "Генерирую код... 🔧")
    try:
        code, miniapp_config, office_hook_config, voice_cashflow_config, fallback_info, miniapp_failure_info = await asyncio.wait_for(
            generate_bot_code(summary), timeout=360.0
        )
    except asyncio.TimeoutError:
        logger.error("Code generation timed out after 360s")
        try:
            await gen_msg.delete()
        except Exception:
            pass
        await bot.send_message(
            chat_id,
            "⏱ Генерация заняла слишком много времени (>6 мин). Попробуй ещё раз — обычно со второго раза работает быстрее.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🔄 Попробовать снова", callback_data="retry_generate")
            ]]),
        )
        return
    except Exception as e:
        logger.error(f"Code generation failed: {type(e).__name__}: {e}")
        try:
            await gen_msg.delete()
        except Exception:
            pass
        await bot.send_message(
            chat_id,
            f"⚠️ Не удалось сгенерировать код ({type(e).__name__}).\n\n"
            "Нажми кнопку чтобы попробовать снова.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🔄 Попробовать снова", callback_data="retry_generate")
            ]]),
        )
        return

    try:
        await gen_msg.delete()
    except Exception:
        pass

    await state.update_data(
        bot_code=code,
        miniapp_config=miniapp_config,
        office_hook_config=office_hook_config,
        voice_cashflow_config=voice_cashflow_config,
        fallback_info=fallback_info,
        miniapp_failure_info=miniapp_failure_info,
    )
    await state.set_state(CreateBotStates.waiting_for_token)

    _pending[user_id] = {
        "chat_id": chat_id,
        "code": code,
        "name": bot_name,
        "summary": summary,
        "creation_prompt": creation_prompt,
        "display_name": data.get("display_name", ""),
        "miniapp_config": miniapp_config,
        "office_hook_config": office_hook_config,
        "voice_cashflow_config": voice_cashflow_config,
        "fallback_info": fallback_info,
        "miniapp_failure_info": miniapp_failure_info,
        "office_bots": data.get("office_bots") or [],
        "office_links": data.get("office_links") or [],
        "office_index": data.get("office_index", 0),
        "office_bot_ids": data.get("office_bot_ids") or {},
        "office_role_hint": (data.get("office_bots") or [{}])[data.get("office_index", 0)].get("role_hint")
            if data.get("office_bots") else None,
    }

    progress_prefix = _office_progress_prefix(data)
    suggested_username = f"{bot_name}Bot"
    display_name = bot_name.replace("_", " ").title()
    if _manager_username:
        url = (
            f"https://t.me/newbot/{_manager_username}/"
            f"{suggested_username}?name={quote_plus(display_name)}"
        )
        button_text = "Создать бота ✨"
        instructions = (
            f"{progress_prefix}Код готов! ✅\n\n"
            f"Предлагаемый username: *@{suggested_username}*\n\n"
            f"1️⃣ Нажми кнопку ниже\n"
            f"2️⃣ Проверь имя и username в BotFather (можно изменить)\n"
            f"3️⃣ Нажми «Создать» — бот запустится автоматически!"
        )
    else:
        url = "https://t.me/BotFather?start=newbot"
        button_text = "Открыть BotFather 🤖"
        instructions = (
            f"{progress_prefix}Код готов! ✅\n\n"
            f"Предлагаемое имя: *{bot_name}_bot*\n\n"
            f"1️⃣ Нажми кнопку → BotFather\n"
            f"2️⃣ Отправь /newbot, введи имя и username\n"
            f"3️⃣ Скопируй токен и вставь сюда"
        )

    await bot.send_message(
        chat_id,
        instructions,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=button_text, url=url)
        ]]),
    )


@router.callback_query(F.data == "retry_generate")
async def cb_retry_generate(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await _run_generation(
        chat_id=callback.message.chat.id,
        user_id=callback.from_user.id,
        bot=callback.bot,
        state=state,
    )


# ── office/multi-bot queue continuation ───────────────────────────────────────

def _office_link_summary(office_links: list[dict], office_bot_ids: dict, created: int) -> str:
    """Human-readable report of which links got created vs skipped, sent once
    the last bot in an office plan is activated (docs/
    MULTIBOT_OFFICE_ROUTING_DESIGN.md §3.3)."""
    parts = [f"🔗 Связей создано: {created} из {len(office_links)}."]
    if created < len(office_links):
        parts.append(
            "Часть связей не удалось создать (роль без реально созданного бота "
            "или связь между ботами разных владельцев) — их можно донастроить "
            "вручную в разделе «🏢 Офисы»."
        )
    return "\n".join(parts)


async def _finish_office_plan(pending: dict, bot: Bot, chat_id: int) -> None:
    """Called once the LAST bot in an office/multi-bot plan has been created —
    resolves every link's role_hint to a real bot_id and wires it up via the
    already-existing db.database.add_office_link() (docs/OFFICES_DESIGN.md's
    infra, reused as-is — see docs/MULTIBOT_OFFICE_ROUTING_DESIGN.md §3.3)."""
    office_links: list[dict] = pending.get("office_links") or []
    if not office_links:
        return
    office_bot_ids: dict = pending.get("office_bot_ids") or {}
    created = 0
    for link in office_links:
        source_id = office_bot_ids.get(link["source_role_hint"])
        target_id = office_bot_ids.get(link["target_role_hint"])
        if source_id is None or target_id is None:
            continue
        if await add_office_link(source_id, target_id, link["event_type"]):
            created += 1
    await bot.send_message(chat_id, _office_link_summary(office_links, office_bot_ids, created))


async def _continue_office_queue(
    pending: dict, bot_record_id: int, creator_user_id: int, bot: Bot, storage
) -> bool:
    """After a single bot from _pending finishes activation, records its
    role_hint -> real bot_id and either kicks off onboarding for the next bot
    in an office/multi-bot queue, or — if this was the last one — wires up
    every office_links entry and reports the result. No-op for the ordinary
    single-bot case (office_bots absent/length<=1). See docs/
    MULTIBOT_OFFICE_ROUTING_DESIGN.md §3.2 (variant A: owner repeats the
    BotFather step per bot, no way around Telegram's one-bot-per-action API)
    and §3.3 (link resolution happens only once every bot has a real id).

    Returns True if it advanced the queue to the next bot (a fresh FSM state
    was just set for creator_user_id — callers must NOT clear/overwrite it
    afterwards), False otherwise (ordinary single-bot case, or this was the
    last bot in the office and the links were just wired up)."""
    office_bots: list[dict] = pending.get("office_bots") or []
    if len(office_bots) <= 1:
        return False

    role_hint = pending.get("office_role_hint")
    office_bot_ids: dict = pending.get("office_bot_ids") or {}
    if role_hint:
        office_bot_ids[role_hint] = bot_record_id
    pending["office_bot_ids"] = office_bot_ids

    office_index: int = pending.get("office_index", 0)
    chat_id = pending["chat_id"]

    if office_index + 1 >= len(office_bots):
        await _finish_office_plan(pending, bot, chat_id)
        return False

    next_index = office_index + 1
    next_bot = office_bots[next_index]
    try:
        next_bot_name = await extract_bot_name(next_bot["summary"])
    except Exception:
        next_bot_name = f"my_bot_{next_index + 1}"

    if not storage or not _bot_id:
        logger.warning(
            f"Office queue for user {creator_user_id} cannot continue — no FSM storage available"
        )
        return False
    from aiogram.fsm.storage.base import StorageKey
    key = StorageKey(bot_id=_bot_id, chat_id=creator_user_id, user_id=creator_user_id)
    next_state = FSMContext(storage=storage, key=key)
    await next_state.set_data({
        "conversation": [],
        "bot_summary": next_bot["summary"],
        "bot_name": next_bot_name,
        "office_bots": office_bots,
        "office_links": pending.get("office_links") or [],
        "office_index": next_index,
        "office_bot_ids": office_bot_ids,
    })
    await next_state.set_state(CreateBotStates.waiting_for_display_name)
    await bot.send_message(
        chat_id,
        f"▶️ Переходим к следующему боту офиса.\n\n"
        f"🏢 Бот {next_index + 1} из {len(office_bots)}: {next_bot['role_hint']}\n\n"
        "👤 *Как будут звать этого бота?*\n"
        "Например: Макс, Катя, Алекс — это имя для общения в групповом чате.\n\n"
        "Напишите имя или /skip чтобы пропустить.",
        parse_mode="Markdown",
    )
    return True


# ── managed bot auto-launch ───────────────────────────────────────────────────

async def auto_launch_managed_bot(managed_data: dict, bot: Bot, storage=None) -> None:
    logger.debug(f"managed_bot RAW data: {managed_data}")

    new_bot_info = managed_data.get("bot") or managed_data.get("new_bot") or {}
    creator_info = managed_data.get("user") or managed_data.get("creator") or {}

    new_bot_id: int | None = (
        managed_data.get("bot_id")
        or new_bot_info.get("id")
        or new_bot_info.get("user_id")
    )
    creator_user_id: int | None = (
        managed_data.get("user_id")
        or managed_data.get("creator_id")
        or creator_info.get("id")
    )

    if not new_bot_id or not creator_user_id:
        logger.warning(f"managed_bot missing bot/user IDs — full data: {managed_data}")
        return

    pending = _pending.pop(creator_user_id, None)
    if not pending:
        logger.info(f"No pending creation for user {creator_user_id}")
        try:
            await bot.send_message(
                creator_user_id,
                "⚠️ Бот в Telegram создан, но я потерял данные для его активации "
                "(возможно, вы начали создание нового бота до завершения этого). "
                "Напишите нам в поддержку — бот не будет работать, пока мы не "
                "привяжем его вручную.",
            )
        except Exception as e:
            logger.error(f"Failed to notify user {creator_user_id} about lost pending: {e}")
        return

    chat_id = pending["chat_id"]

    try:
        token = await get_managed_bot_token(BOT_TOKEN, new_bot_id)
    except Exception as e:
        logger.error(f"getManagedBotToken failed: {e}")
        await bot.send_message(
            chat_id,
            "Бот создан в BotFather, но не удалось получить токен автоматически 😔\n\n"
            "Скопируй токен из BotFather и отправь его сюда вручную.",
        )
        return

    await _clear_user_fsm(storage, creator_user_id)

    real_username: str | None = None
    try:
        async with Bot(token=token) as temp_bot:
            info = await temp_bot.get_me()
            real_username = info.username
    except Exception:
        pass

    bot_name: str = pending["name"]
    bot_code: str = pending["code"]
    bot_summary: str = pending["summary"]
    creation_prompt: str | None = pending.get("creation_prompt")
    display_name: str = pending.get("display_name", "")
    miniapp_config: dict | None = pending.get("miniapp_config")
    office_hook_config: dict | None = pending.get("office_hook_config")
    voice_cashflow_config: dict | None = pending.get("voice_cashflow_config")
    fallback_info: dict | None = pending.get("fallback_info")
    miniapp_failure_info: dict | None = pending.get("miniapp_failure_info")

    avatar_path = AVATAR_DIR / f"{bot_name}.jpg"
    if avatar_path.exists():
        await _set_bot_profile_photo(token, str(avatar_path))
        avatar_path.unlink(missing_ok=True)

    bot_file = GENERATED_BOTS_DIR / f"{bot_name}.py"
    bot_file.write_text(bot_code, encoding="utf-8")
    asyncio.create_task(push_bot_to_github(bot_name, bot_code))

    _owner_id = os.getenv("OWNER_ID", "")
    admin_ids = [str(creator_user_id)]
    if _owner_id and _owner_id != str(creator_user_id):
        admin_ids.append(_owner_id)

    bot_record_id = await create_bot_record_with_admins(
        name=bot_name,
        description=bot_summary,
        token=token,
        file_path=str(bot_file),
        admin_ids=admin_ids,
        username=real_username,
        owner_telegram_id=creator_user_id,
        creation_prompt=creation_prompt,
    )

    if miniapp_config:
        await set_bot_miniapp_config(bot_record_id, miniapp_config)
    if office_hook_config:
        await set_bot_office_hook_config(bot_record_id, office_hook_config)
    await _apply_voice_cashflow_config(bot_record_id, voice_cashflow_config)
    if fallback_info:
        await add_template_candidate(
            creator_user_id=creator_user_id,
            summary=bot_summary,
            fallback_reason=fallback_info["reason"],
            selected_templates=fallback_info["selected_templates"],
            bot_name=bot_name,
            bot_id=bot_record_id,
        )
    if miniapp_failure_info:
        await add_miniapp_config_failure(
            creator_user_id=creator_user_id,
            summary=bot_summary,
            failure_reason=miniapp_failure_info["reason"],
            bot_name=bot_name,
            bot_id=bot_record_id,
        )

    # The welcome photo was saved during onboarding under the bot's NAME
    # (handle_welcome_photo, before this bot's row/id existed). All five
    # templates now look for it under bot_images/bot_<id>.jpg (Stage 2
    # "изоляция по bots.id") — move it into place now that the id exists.
    _welcome_photo_by_name = BOT_IMAGES_DIR / f"{bot_name}.jpg"
    if _welcome_photo_by_name.exists():
        _welcome_photo_by_name.rename(BOT_IMAGES_DIR / f"bot_{bot_record_id}.jpg")

    if display_name:
        await set_bot_display_name(bot_record_id, display_name)

    await _register_new_bot_in_registry(bot_record_id, bot_name)

    username_display = f" (@{real_username})" if real_username else ""
    extra_env = {}
    if display_name:
        extra_env["BOT_DISPLAY_NAME"] = display_name
    activation = await _activate_new_bot(bot_record_id, bot_name, bot_file, token, extra_env, has_miniapp=bool(miniapp_config))
    await _notify_bot_created(bot, chat_id, bot_record_id, bot_name, bot_summary, username_display, activation)
    if miniapp_failure_info:
        await _notify_miniapp_config_failure(bot, chat_id, bot_name)
    await _continue_office_queue(pending, bot_record_id, creator_user_id, bot, storage)


# ── manual token entry (fallback) ─────────────────────────────────────────────

@router.message(CreateBotStates.waiting_for_token, F.voice)
async def handle_token_voice(message: Message):
    await message.answer("⚠️ Токен лучше прислать текстом — скопируйте его из @BotFather.")


@router.message(CreateBotStates.waiting_for_token, F.text, ~F.text.startswith("/"))
async def handle_token(message: Message, state: FSMContext, bot: Bot):
    token = message.text.strip()
    if ":" not in token or len(token) < 30:
        await message.answer("Не похоже на токен Telegram. Попробуйте ещё раз.")
        return

    existing = await get_bot_by_token(token)
    if existing is not None:
        # A second bot row on the same token would fight the first one over
        # getUpdates (polling) or silently steal its webhook (Telegram allows
        # only one per token) — the first bot goes dark with no error
        # anywhere. Caught here, before create_bot_record_with_admins, since
        # tokens are Fernet-encrypted at rest and can't be checked with a
        # plain SQL WHERE (see get_bot_by_token's docstring).
        await message.answer(
            f"⚠️ Этот токен уже используется ботом «{existing['name']}» (id={existing['id']}). "
            "Один токен нельзя привязать к двум ботам — пришлите токен другого бота из @BotFather."
        )
        return

    _pending.pop(message.from_user.id, None)

    data = await state.get_data()
    bot_code: str = data["bot_code"]
    bot_name: str = data["bot_name"]
    bot_summary: str = data.get("bot_summary", "")
    creation_prompt = _first_user_message(data.get("conversation", []))
    display_name: str = data.get("display_name", "")
    miniapp_config: dict | None = data.get("miniapp_config")
    office_hook_config: dict | None = data.get("office_hook_config")
    voice_cashflow_config: dict | None = data.get("voice_cashflow_config")
    fallback_info: dict | None = data.get("fallback_info")
    miniapp_failure_info: dict | None = data.get("miniapp_failure_info")

    real_username: str | None = None
    try:
        async with Bot(token=token) as temp_bot:
            real_username = (await temp_bot.get_me()).username
    except Exception:
        pass

    avatar_path = AVATAR_DIR / f"{bot_name}.jpg"
    if avatar_path.exists():
        await _set_bot_profile_photo(token, str(avatar_path))
        avatar_path.unlink(missing_ok=True)

    bot_file = GENERATED_BOTS_DIR / f"{bot_name}.py"
    bot_file.write_text(bot_code, encoding="utf-8")
    asyncio.create_task(push_bot_to_github(bot_name, bot_code))

    _owner_id = os.getenv("OWNER_ID", "")
    admin_ids = [str(message.from_user.id)]
    if _owner_id and _owner_id != str(message.from_user.id):
        admin_ids.append(_owner_id)

    bot_id = await create_bot_record_with_admins(
        name=bot_name,
        description=bot_summary,
        token=token,
        file_path=str(bot_file),
        admin_ids=admin_ids,
        username=real_username,
        owner_telegram_id=message.from_user.id,
        creation_prompt=creation_prompt,
    )

    if miniapp_config:
        await set_bot_miniapp_config(bot_id, miniapp_config)
    if office_hook_config:
        await set_bot_office_hook_config(bot_id, office_hook_config)
    await _apply_voice_cashflow_config(bot_id, voice_cashflow_config)
    # fallback_info/miniapp_failure_info: this manual-token path previously
    # never threaded either through from FSM state at all (only
    # auto_launch_managed_bot's _pending-based path did) — a gap, not a
    # deliberate omission, since both signals are just as meaningful for a
    # bot created via manual token entry. Same persistence as
    # auto_launch_managed_bot below.
    if fallback_info:
        await add_template_candidate(
            creator_user_id=message.from_user.id,
            summary=bot_summary,
            fallback_reason=fallback_info["reason"],
            selected_templates=fallback_info["selected_templates"],
            bot_name=bot_name,
            bot_id=bot_id,
        )
    if miniapp_failure_info:
        await add_miniapp_config_failure(
            creator_user_id=message.from_user.id,
            summary=bot_summary,
            failure_reason=miniapp_failure_info["reason"],
            bot_name=bot_name,
            bot_id=bot_id,
        )

    # See the equivalent comment in _run_generation() above — the welcome
    # photo was saved under the bot's name before its row/id existed; all
    # five templates now look for bot_images/bot_<id>.jpg.
    _welcome_photo_by_name = BOT_IMAGES_DIR / f"{bot_name}.jpg"
    if _welcome_photo_by_name.exists():
        _welcome_photo_by_name.rename(BOT_IMAGES_DIR / f"bot_{bot_id}.jpg")

    if display_name:
        await set_bot_display_name(bot_id, display_name)

    await _register_new_bot_in_registry(bot_id, bot_name)

    username_display = f" (@{real_username})" if real_username else ""
    extra_env = {}
    if display_name:
        extra_env["BOT_DISPLAY_NAME"] = display_name
    activation = await _activate_new_bot(bot_id, bot_name, bot_file, token, extra_env, has_miniapp=bool(miniapp_config))
    await _notify_bot_created(bot, message.chat.id, bot_id, bot_name, bot_summary, username_display, activation)
    if miniapp_failure_info:
        await _notify_miniapp_config_failure(bot, message.chat.id, bot_name)

    office_pending = {
        "chat_id": message.chat.id,
        "office_bots": data.get("office_bots") or [],
        "office_links": data.get("office_links") or [],
        "office_index": data.get("office_index", 0),
        "office_bot_ids": data.get("office_bot_ids") or {},
        "office_role_hint": (data.get("office_bots") or [{}])[data.get("office_index", 0)].get("role_hint")
            if data.get("office_bots") else None,
    }
    advanced = await _continue_office_queue(office_pending, bot_id, message.from_user.id, bot, state.storage)
    if not advanced:
        await state.clear()
