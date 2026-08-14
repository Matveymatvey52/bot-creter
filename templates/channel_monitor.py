# TEMPLATE: channel_monitor
# USE FOR: мониторинг чужих Telegram-каналов через личный аккаунт (userbot/Telethon),
#          суммаризация постов через Gemini, лента в боте фабрики
# CUSTOMIZE: sections marked with # CUSTOMIZE
#
# This is a standalone PRODUCT template, not a factory feature module — see
# docs/USERBOT_CHANNEL_MONITOR_DESIGN.md. It drives an aiogram FSM to
# authorize a Telethon userbot client and manage what it monitors; the actual
# long-lived Telethon connections and post ingestion live in a SEPARATE
# process, runtime/userbot_worker.py (§3 of the design doc) — this file only
# talks to Telegram's Bot API (aiogram) plus the DB tables both processes
# share (db/database.py's userbot_sessions / monitored_channels /
# channel_posts).
from __future__ import annotations

import html
import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import TELEGRAM_API_HASH, TELEGRAM_API_ID
from db.database import (
    add_monitored_channel,
    create_userbot_session,
    get_monitored_channels_by_owner,
    get_recent_posts_for_owner,
    get_userbot_sessions_by_owner,
    revoke_userbot_session,
    set_monitored_channel_active,
    set_userbot_session_active,
    set_userbot_session_status,
)

# ── CUSTOMIZE ────────────────────────────────────────────────────────────────
BOT_DESCRIPTION = "Мониторинг чужих Telegram-каналов через личный аккаунт: лента постов с авто-суммаризацией."
WELCOME_TEXT = (
    "📡 <b>Мониторинг каналов</b>\n\n"
    "Слежу за чужими Telegram-каналами через ваш личный аккаунт и собираю "
    "их посты в одну ленту — с кратким summary через Gemini.\n\n"
    "Нажмите «➕ Подключить мониторинг», чтобы начать."
)
# ── END CUSTOMIZE ─────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
router = Router()

MAX_LIST_BUTTONS = 30
FEED_LIMIT = 15
TEXT_PREVIEW_LEN = 400

RISK_TEXT = (
    "⚠️ Мониторинг чужих каналов работает через ваш <b>личный Telegram-аккаунт</b>, "
    "а не через бота. Это значит:\n\n"
    "• Telegram может ограничить или заблокировать этот аккаунт, если сочтёт его "
    "поведение автоматизированным (массовое вступление в каналы, частые запросы) — "
    "такой риск реален и неустраним полностью на нашей стороне.\n"
    "• Массовый сбор контента из чужих каналов может нарушать условия использования "
    "Telegram — ответственность за то, какие каналы вы мониторите и как используете "
    "собранные данные, лежит на вас.\n"
    "• Мы технически предоставляем инструмент для авторизации и мониторинга, но не "
    "гарантируем сохранность вашего аккаунта и не несём ответственности за его "
    "блокировку.\n\n"
    "Продолжая, вы подтверждаете, что понимаете эти риски."
)

MAX_CODE_ATTEMPTS = 3
MAX_2FA_ATTEMPTS = 3

# Security-review finding: without this, a Telegram user could type a
# different phone number into the bot on every /start -> cm_connect cycle,
# making this bot send a real Telegram login code to an arbitrary phone
# number over and over — an SMS/code-bombing proxy against numbers the
# requester doesn't own. Telegram's own FloodWaitError eventually kicks in,
# but only per phone number / per app, not per REQUESTING Telegram user — so
# it doesn't stop one user from cycling through many different victim
# numbers. This is a per-from_user.id cooldown, independent of and in
# addition to FloodWaitError handling below.
PHONE_REQUEST_COOLDOWN_SECONDS = 30
_last_phone_request_at: dict[int, float] = {}


# ── FSM ──────────────────────────────────────────────────────────────────────

class UserbotAuthStates(StatesGroup):
    waiting_risk_ack = State()
    waiting_phone = State()
    waiting_code = State()
    waiting_2fa_password = State()


# ── keyboards ────────────────────────────────────────────────────────────────

def kb_start_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Подключить мониторинг", callback_data="cm_connect")],
        [InlineKeyboardButton(text="📋 Мои каналы", callback_data="cm_channels")],
        [InlineKeyboardButton(text="📰 Лента", callback_data="cm_feed")],
    ])


def kb_risk_ack() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Понимаю и продолжаю", callback_data="cm_risk_ack")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cm_cancel")],
    ])


def kb_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Отмена", callback_data="cm_cancel")
    ]])


def kb_channels(channels: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for ch in channels[:MAX_LIST_BUTTONS]:
        label = ch.get("channel_title") or ch.get("channel_username") or f"#{ch['id']}"
        state_icon = "🟢" if ch.get("active") else "⚪️"
        label = f"{state_icon} {label}"
        rows.append([InlineKeyboardButton(text=label[:60], callback_data=f"cm_toggle:{ch['id']}")])
    rows.append([InlineKeyboardButton(text="➕ Добавить канал", callback_data="cm_add_channel")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="cm_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_back_to_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⬅️ Назад", callback_data="cm_menu")
    ]])


# ── helpers ──────────────────────────────────────────────────────────────────

def _esc(value, max_len: int = 500) -> str:
    text = str(value) if value is not None else ""
    if len(text) > max_len:
        text = text[:max_len] + "…"
    return html.escape(text)


def _make_client(session_string: str = ""):
    """Builds a fresh Telethon TelegramClient bound to a StringSession.

    Isolated into its own function (rather than inlined at each call site) so
    tests can patch templates.channel_monitor._make_client instead of
    reaching into telethon.TelegramClient directly — no real network calls
    happen in tests. Raises a clear error if TELEGRAM_API_ID/API_HASH aren't
    configured, instead of Telethon's own more opaque failure deep inside
    connect().
    """
    if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
        raise RuntimeError(
            "TELEGRAM_API_ID / TELEGRAM_API_HASH is not set in .env — required to authorize "
            "a userbot session. Obtain both at https://my.telegram.org/apps and set them as "
            "environment variables."
        )
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    return TelegramClient(StringSession(session_string), int(TELEGRAM_API_ID), TELEGRAM_API_HASH)


async def _channels_panel_text(owner_user_id: int) -> tuple[str, list[dict]]:
    channels = await get_monitored_channels_by_owner(owner_user_id)
    if not channels:
        return "У вас пока нет подключённых каналов.", channels
    lines = ["📋 <b>Ваши каналы</b> (нажмите, чтобы включить/выключить):\n"]
    for ch in channels[:MAX_LIST_BUTTONS]:
        icon = "🟢" if ch.get("active") else "⚪️"
        title = ch.get("channel_title") or ch.get("channel_username") or f"#{ch['id']}"
        lines.append(f"{icon} {_esc(title, 80)}")
    return "\n".join(lines), channels


async def _feed_text(owner_user_id: int) -> str:
    posts = await get_recent_posts_for_owner(owner_user_id, limit=FEED_LIMIT)
    if not posts:
        return "📰 Лента пуста — постов из активных каналов пока нет."
    lines = ["📰 <b>Лента</b>\n"]
    for p in posts:
        title = p.get("channel_title") or p.get("channel_username") or "канал"
        body = p.get("summary") or p.get("text") or ""
        lines.append(f"<b>{_esc(title, 60)}</b>\n{_esc(body, TEXT_PREVIEW_LEN)}\n")
    return "\n".join(lines)


# ── handlers: entry ──────────────────────────────────────────────────────────

@router.message(Command("start"), F.chat.type == "private")
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(WELCOME_TEXT, parse_mode="HTML", reply_markup=kb_start_menu())


@router.callback_query(F.data == "cm_menu")
async def cb_menu(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await cb.message.edit_text(WELCOME_TEXT, parse_mode="HTML", reply_markup=kb_start_menu())


@router.callback_query(F.data == "cm_cancel")
async def cb_cancel(cb: CallbackQuery, state: FSMContext):
    await cb.answer("Отменено")
    await state.clear()
    await cb.message.edit_text(WELCOME_TEXT, parse_mode="HTML", reply_markup=kb_start_menu())


# ── handlers: auth FSM ───────────────────────────────────────────────────────

@router.callback_query(F.data == "cm_connect")
async def cb_connect_start(cb: CallbackQuery, state: FSMContext):
    """§6 of the design doc: the risk screen must be shown BEFORE the phone
    number is ever requested — this is the entry point, not waiting_phone."""
    await cb.answer()
    await state.set_state(UserbotAuthStates.waiting_risk_ack)
    await cb.message.edit_text(RISK_TEXT, parse_mode="HTML", reply_markup=kb_risk_ack())


@router.callback_query(UserbotAuthStates.waiting_risk_ack, F.data == "cm_risk_ack")
async def cb_risk_ack(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(UserbotAuthStates.waiting_phone)
    await cb.message.edit_text(
        "Введите номер телефона аккаунта, который будет вести мониторинг, "
        "в международном формате (например +79991234567):",
        reply_markup=kb_cancel(),
    )


@router.message(UserbotAuthStates.waiting_phone, F.text, ~F.text.startswith("/"))
async def on_phone_entered(message: Message, state: FSMContext):
    phone = message.text.strip()

    import time

    now = time.monotonic()
    last = _last_phone_request_at.get(message.from_user.id)
    if last is not None and (now - last) < PHONE_REQUEST_COOLDOWN_SECONDS:
        remaining = int(PHONE_REQUEST_COOLDOWN_SECONDS - (now - last))
        await message.answer(
            f"⏳ Подождите ещё {remaining} сек. перед следующей попыткой ввода номера.",
            reply_markup=kb_cancel(),
        )
        return
    _last_phone_request_at[message.from_user.id] = now

    from telethon.errors import FloodWaitError, PhoneNumberInvalidError

    try:
        client = _make_client()
    except RuntimeError as e:
        logger.error(f"on_phone_entered: {e}")
        await state.clear()
        await message.answer(f"⚠️ {_esc(str(e), 300)}")
        return

    try:
        await client.connect()
        sent = await client.send_code_request(phone)
    except PhoneNumberInvalidError:
        await message.answer(
            "❌ Некорректный номер телефона. Введите номер в международном формате "
            "(например +79991234567):",
            reply_markup=kb_cancel(),
        )
        return
    except FloodWaitError as e:
        # Never retry sooner than Telegram says — see design doc §2.
        await state.clear()
        await message.answer(
            f"⏳ Telegram просит подождать {e.seconds} секунд перед следующей попыткой. "
            "Попробуйте снова позже.",
        )
        return
    except Exception as e:
        logger.error(f"on_phone_entered: send_code_request failed: {type(e).__name__}: {e}")
        await state.clear()
        await message.answer("⚠️ Не удалось начать авторизацию. Попробуйте позже.")
        return
    finally:
        # client stays connected across the FSM turn (see _get_pending_client
        # docstring) — only disconnect on the failure paths above where no
        # further step will resume this same client.
        pass

    session_id = await create_userbot_session(message.from_user.id, phone)
    await state.update_data(
        phone=phone,
        phone_code_hash=sent.phone_code_hash,
        session_id=session_id,
        code_attempts=0,
        session_string=client.session.save(),
    )
    await client.disconnect()
    await state.set_state(UserbotAuthStates.waiting_code)
    await message.answer(
        "📩 Код отправлен в ваш аккаунт Telegram. Введите его сюда:",
        reply_markup=kb_cancel(),
    )


async def _resume_client(state_data: dict):
    """Rebuilds a TelegramClient from the in-progress StringSession stashed in
    FSM state between turns — Telethon clients aren't picklable/storable
    directly in aiogram's FSM storage, so each turn reconnects using the
    session string captured after send_code_request/sign_in. This is only
    ever the in-progress (not yet 'active') session for one auth attempt."""
    client = _make_client(state_data.get("session_string", ""))
    await client.connect()
    return client


@router.message(UserbotAuthStates.waiting_code, F.text, ~F.text.startswith("/"))
async def on_code_entered(message: Message, state: FSMContext):
    from telethon.errors import (
        FloodWaitError, PhoneCodeExpiredError, PhoneCodeInvalidError, SessionPasswordNeededError,
    )

    code = message.text.strip()
    data = await state.get_data()
    phone = data.get("phone")
    phone_code_hash = data.get("phone_code_hash")
    session_id = data.get("session_id")
    attempts = data.get("code_attempts", 0)

    client = await _resume_client(data)
    try:
        await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
    except PhoneCodeInvalidError:
        attempts += 1
        await client.disconnect()
        if attempts >= MAX_CODE_ATTEMPTS:
            await state.clear()
            if session_id:
                await set_userbot_session_status(session_id, "auth_failed")
            await message.answer("❌ Слишком много неверных попыток. Начните авторизацию заново.")
            return
        await state.update_data(code_attempts=attempts)
        await message.answer(
            f"❌ Неверный код ({attempts}/{MAX_CODE_ATTEMPTS}). Введите код ещё раз:",
            reply_markup=kb_cancel(),
        )
        return
    except PhoneCodeExpiredError:
        await client.disconnect()
        await state.set_state(UserbotAuthStates.waiting_phone)
        if session_id:
            await set_userbot_session_status(session_id, "auth_failed")
        await message.answer(
            "⌛ Код устарел. Введите номер телефона заново:",
            reply_markup=kb_cancel(),
        )
        return
    except SessionPasswordNeededError:
        await state.update_data(session_string=client.session.save())
        await client.disconnect()
        await state.set_state(UserbotAuthStates.waiting_2fa_password)
        await state.update_data(password_attempts=0)
        await message.answer(
            "🔐 На аккаунте включена облачная 2FA-защита. Введите пароль:",
            reply_markup=kb_cancel(),
        )
        return
    except FloodWaitError as e:
        await client.disconnect()
        await state.clear()
        if session_id:
            await set_userbot_session_status(session_id, "auth_failed")
        await message.answer(
            f"⏳ Telegram просит подождать {e.seconds} секунд перед следующей попыткой. "
            "Попробуйте снова позже.",
        )
        return
    except Exception as e:
        logger.error(f"on_code_entered: sign_in failed: {type(e).__name__}: {e}")
        await client.disconnect()
        await state.clear()
        if session_id:
            await set_userbot_session_status(session_id, "auth_failed")
        await message.answer("⚠️ Не удалось войти. Попробуйте позже.")
        return

    await _finish_auth_success(message, state, client, session_id)


@router.message(UserbotAuthStates.waiting_2fa_password, F.text, ~F.text.startswith("/"))
async def on_2fa_password_entered(message: Message, state: FSMContext):
    from telethon.errors import FloodWaitError, PasswordHashInvalidError

    password = message.text.strip()
    data = await state.get_data()
    session_id = data.get("session_id")
    attempts = data.get("password_attempts", 0)

    client = await _resume_client(data)
    try:
        await client.sign_in(password=password)
    except PasswordHashInvalidError:
        attempts += 1
        await client.disconnect()
        if attempts >= MAX_2FA_ATTEMPTS:
            await state.clear()
            if session_id:
                await set_userbot_session_status(session_id, "auth_failed")
            await message.answer("❌ Слишком много неверных попыток. Начните авторизацию заново.")
            return
        await state.update_data(password_attempts=attempts)
        await message.answer(
            f"❌ Неверный пароль ({attempts}/{MAX_2FA_ATTEMPTS}). Введите пароль ещё раз:",
            reply_markup=kb_cancel(),
        )
        return
    except FloodWaitError as e:
        await client.disconnect()
        await state.clear()
        if session_id:
            await set_userbot_session_status(session_id, "auth_failed")
        await message.answer(
            f"⏳ Telegram просит подождать {e.seconds} секунд перед следующей попыткой. "
            "Попробуйте снова позже.",
        )
        return
    except Exception as e:
        logger.error(f"on_2fa_password_entered: sign_in failed: {type(e).__name__}: {e}")
        await client.disconnect()
        await state.clear()
        if session_id:
            await set_userbot_session_status(session_id, "auth_failed")
        await message.answer("⚠️ Не удалось войти. Попробуйте позже.")
        return

    await _finish_auth_success(message, state, client, session_id)


async def _finish_auth_success(message: Message, state: FSMContext, client, session_id: int | None) -> None:
    """§2 step 4: session.save() → encrypted → DB, status='active'. Never
    logs or echoes the session string itself."""
    session_string = client.session.save()
    await client.disconnect()
    await state.clear()
    if session_id:
        await set_userbot_session_active(session_id, session_string)
    await message.answer(
        "✅ Мониторинг подключён! Теперь можно добавить каналы для отслеживания.\n\n"
        + RISK_TEXT,
        parse_mode="HTML",
        reply_markup=kb_start_menu(),
    )


# ── handlers: channel list / toggle / feed ───────────────────────────────────

@router.callback_query(F.data == "cm_channels")
async def cb_channels(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    text, channels = await _channels_panel_text(cb.from_user.id)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_channels(channels))


@router.callback_query(F.data.startswith("cm_toggle:"))
async def cb_toggle_channel(cb: CallbackQuery):
    channel_id = int(cb.data.split(":", 1)[1])
    channels = await get_monitored_channels_by_owner(cb.from_user.id)
    target = next((c for c in channels if c["id"] == channel_id), None)
    if not target:
        await cb.answer("Канал не найден", show_alert=True)
        return
    new_active = not target.get("active")
    await set_monitored_channel_active(channel_id, new_active)
    await cb.answer("Включено" if new_active else "Выключено")
    text, channels = await _channels_panel_text(cb.from_user.id)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_channels(channels))


@router.callback_query(F.data == "cm_add_channel")
async def cb_add_channel_start(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(_AddChannelState.waiting_username)
    await cb.message.edit_text(
        "Введите @username канала, который нужно отслеживать:",
        reply_markup=kb_cancel(),
    )


class _AddChannelState(StatesGroup):
    waiting_username = State()


@router.message(_AddChannelState.waiting_username, F.text, ~F.text.startswith("/"))
async def on_channel_username_entered(message: Message, state: FSMContext):
    """Resolves the given @username via the OWNER's active userbot client and
    joins it if public — per design doc §4. A private/inaccessible channel
    gets an explicit error, never a silent no-op."""
    username = message.text.strip().lstrip("@")
    await state.clear()

    sessions = await get_userbot_sessions_by_owner(message.from_user.id)
    active_session = next((s for s in sessions if s.get("status") == "active" and s.get("session_string")), None)
    if not active_session:
        await message.answer(
            "⚠️ Сначала подключите мониторинг («➕ Подключить мониторинг»), затем добавляйте каналы.",
            reply_markup=kb_back_to_menu(),
        )
        return

    from telethon.errors import ChannelPrivateError, UsernameInvalidError, UsernameNotOccupiedError
    from telethon.tl.functions.channels import JoinChannelRequest

    client = _make_client(active_session["session_string"])
    try:
        await client.connect()
        entity = await client.get_entity(username)
        await client(JoinChannelRequest(entity))
    except (ChannelPrivateError,):
        await client.disconnect()
        await message.answer(
            "🔒 Этот канал закрытый — мониторинг возможен только если ваш аккаунт уже "
            "состоит в нём или получит приглашение отдельно.",
            reply_markup=kb_back_to_menu(),
        )
        return
    except (UsernameInvalidError, UsernameNotOccupiedError):
        await client.disconnect()
        await message.answer(
            "❌ Канал с таким @username не найден. Проверьте имя и попробуйте ещё раз.",
            reply_markup=kb_back_to_menu(),
        )
        return
    except Exception as e:
        logger.error(f"on_channel_username_entered: resolve/join failed: {type(e).__name__}: {e}")
        await client.disconnect()
        await message.answer(
            "⚠️ Не удалось добавить канал (закрытый, недоступен, или временная ошибка "
            "Telegram). Попробуйте позже.",
            reply_markup=kb_back_to_menu(),
        )
        return

    channel_id = getattr(entity, "id", None)
    channel_title = getattr(entity, "title", None) or username
    await client.disconnect()
    await add_monitored_channel(message.from_user.id, username, channel_id, channel_title)
    await message.answer(
        f"✅ Канал <b>{_esc(channel_title)}</b> добавлен и будет отслеживаться.",
        parse_mode="HTML",
        reply_markup=kb_back_to_menu(),
    )


@router.callback_query(F.data == "cm_feed")
async def cb_feed(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    text = await _feed_text(cb.from_user.id)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_back_to_menu())


# ── handlers: revoke (referenced by design doc §1; exposed via /disconnect) ──

@router.message(Command("disconnect"), F.chat.type == "private")
async def cmd_disconnect(message: Message, state: FSMContext):
    await state.clear()
    sessions = await get_userbot_sessions_by_owner(message.from_user.id)
    active = [s for s in sessions if s.get("status") == "active"]
    if not active:
        await message.answer("У вас нет активного подключения мониторинга.")
        return
    for s in active:
        await revoke_userbot_session(s["id"])
    await message.answer(
        "🔴 Мониторинг отключён. Сессия удалена — для повторного подключения потребуется "
        "полная авторизация заново.",
    )
