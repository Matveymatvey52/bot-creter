"""templates/channel_monitor.py — userbot authorization FSM.

Drives the real aiogram Dispatcher/Router against fake Message/CallbackQuery
updates, with templates.channel_monitor._make_client patched to return a fake
Telethon client instead of touching the real network — the dependency-
injection seam the module docstring calls out. Bot.__call__ is patched the
same way tests/test_moderator_isolation.py does it (FakeBotAPI), and
runtime.registry._clone_router avoids aiogram's one-parent-Router restriction
across tests, same pattern the live registry uses for multiple bots sharing
one template module.

Covers the happy path (phone -> code -> active), the 2FA branch, and every
Telethon error this module is required to handle explicitly (design doc §2).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from runtime.registry import _clone_router
from templates import channel_monitor

FAKE_TOKEN = "222222:test-token-not-real"
OWNER_ID = 555


class FakeBotAPI:
    """Same shape as tests/test_moderator_isolation.py's FakeBotAPI: replaces
    Bot.__call__, records every aiogram method object, hands back an
    incrementing fake message_id for anything that needs one."""

    def __init__(self):
        self.calls: list = []
        self._next_id = 1

    async def __call__(self, request, **kwargs):
        self.calls.append(request)
        msg_id = self._next_id
        self._next_id += 1
        return SimpleNamespace(message_id=msg_id, chat=SimpleNamespace(id=OWNER_ID))

    def sent_texts(self) -> list[str]:
        return [c.text for c in self.calls if isinstance(c, (SendMessage, EditMessageText))]

    def last_text(self) -> str | None:
        texts = self.sent_texts()
        return texts[-1] if texts else None


def _make_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(_clone_router(channel_monitor.router))
    return dp


def _user_chat():
    user = User(id=OWNER_ID, is_bot=False, first_name="Owner")
    chat = Chat(id=OWNER_ID, type="private")
    return user, chat


def _message_update(text: str, update_id: int) -> Update:
    user, chat = _user_chat()
    msg = Message(message_id=update_id, date=0, chat=chat, from_user=user, text=text)
    return Update(update_id=update_id, message=msg)


def _callback_update(data: str, update_id: int) -> Update:
    user, chat = _user_chat()
    msg = Message(message_id=update_id, date=0, chat=chat, from_user=user, text="menu")
    cb = CallbackQuery(id=str(update_id), from_user=user, chat_instance="x", data=data, message=msg)
    return Update(update_id=update_id, callback_query=cb)


class FakeTelethonClient:
    """Fakes the subset of TelegramClient's async API templates/channel_monitor.py
    calls: connect/disconnect, send_code_request, sign_in, session.save().
    .sign_in_side_effect returns an exception instance to raise, or None to
    succeed — lets each test script exactly one sign_in() outcome per call."""

    def __init__(self, phone_code_hash="hash123", sign_in_side_effect=None, save_value="encoded-session-string"):
        self.connected = False
        self.phone_code_hash = phone_code_hash
        self.sign_in_side_effect = sign_in_side_effect or (lambda *a, **k: None)
        self.session = SimpleNamespace(save=lambda: save_value)
        self.sign_in_calls: list = []

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def send_code_request(self, phone):
        return SimpleNamespace(phone_code_hash=self.phone_code_hash)

    async def sign_in(self, *args, **kwargs):
        self.sign_in_calls.append((args, kwargs))
        result = self.sign_in_side_effect(*args, **kwargs)
        if result is not None:
            raise result
        return SimpleNamespace()


@pytest.fixture
def api_patch():
    api = FakeBotAPI()
    with patch.object(Bot, "__call__", new=api):
        yield api


@pytest.fixture
def bot():
    return Bot(token=FAKE_TOKEN)


@pytest.fixture(autouse=True)
def _reset_phone_request_cooldown():
    """channel_monitor._last_phone_request_at is a module-level, per-process
    throttle (security-review addition) — tests all use the same OWNER_ID, so
    it must be cleared between tests or a later test's first phone entry gets
    rejected by an earlier test's cooldown."""
    channel_monitor._last_phone_request_at.clear()
    yield
    channel_monitor._last_phone_request_at.clear()


async def _feed(dp, bot, update):
    await dp.feed_update(bot, update)


@pytest.mark.asyncio
async def test_happy_path_phone_code_to_active(isolated_db, userbot_key, monkeypatch, bot, api_patch):
    dp = _make_dispatcher()
    from db.database import get_userbot_sessions_by_owner, init_db
    await init_db()

    client = FakeTelethonClient()
    monkeypatch.setattr(channel_monitor, "_make_client", lambda session_string="": client)

    await _feed(dp, bot, _callback_update("cm_connect", 1))
    await _feed(dp, bot, _callback_update("cm_risk_ack", 2))
    await _feed(dp, bot, _message_update("+79991234567", 3))
    await _feed(dp, bot, _message_update("12345", 4))

    assert "одключ" in api_patch.last_text()  # "Мониторинг подключён"

    sessions = await get_userbot_sessions_by_owner(OWNER_ID)
    assert len(sessions) == 1
    assert sessions[0]["status"] == "active"
    assert sessions[0]["session_string"] == "encoded-session-string"


@pytest.mark.asyncio
async def test_2fa_path(isolated_db, userbot_key, monkeypatch, bot, api_patch):
    dp = _make_dispatcher()
    from db.database import get_userbot_sessions_by_owner, init_db
    await init_db()

    from telethon.errors import SessionPasswordNeededError

    call_count = {"n": 0}

    def sign_in_effect(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return SessionPasswordNeededError(request=None)
        return None  # second call (password) succeeds

    client = FakeTelethonClient(sign_in_side_effect=sign_in_effect)
    monkeypatch.setattr(channel_monitor, "_make_client", lambda session_string="": client)

    await _feed(dp, bot, _callback_update("cm_connect", 1))
    await _feed(dp, bot, _callback_update("cm_risk_ack", 2))
    await _feed(dp, bot, _message_update("+79991234567", 3))
    await _feed(dp, bot, _message_update("12345", 4))
    assert "2FA" in api_patch.last_text() or "пароль" in api_patch.last_text().lower()

    await _feed(dp, bot, _message_update("mysecretpassword", 5))
    assert "одключ" in api_patch.last_text()

    sessions = await get_userbot_sessions_by_owner(OWNER_ID)
    assert sessions[0]["status"] == "active"


@pytest.mark.asyncio
async def test_phone_code_invalid_stays_in_waiting_code(isolated_db, userbot_key, monkeypatch, bot, api_patch):
    dp = _make_dispatcher()
    from db.database import init_db
    await init_db()

    from telethon.errors import PhoneCodeInvalidError

    client = FakeTelethonClient(sign_in_side_effect=lambda *a, **k: PhoneCodeInvalidError(request=None))
    monkeypatch.setattr(channel_monitor, "_make_client", lambda session_string="": client)

    await _feed(dp, bot, _callback_update("cm_connect", 1))
    await _feed(dp, bot, _callback_update("cm_risk_ack", 2))
    await _feed(dp, bot, _message_update("+79991234567", 3))
    await _feed(dp, bot, _message_update("wrong-code", 4))

    assert "Неверный код" in api_patch.last_text()

    state = dp.fsm.resolve_context(bot, OWNER_ID, OWNER_ID)
    assert (await state.get_state()) == channel_monitor.UserbotAuthStates.waiting_code.state


@pytest.mark.asyncio
async def test_phone_code_invalid_too_many_attempts_marks_auth_failed(isolated_db, userbot_key, monkeypatch, bot, api_patch):
    dp = _make_dispatcher()
    from db.database import get_userbot_sessions_by_owner, init_db
    await init_db()

    from telethon.errors import PhoneCodeInvalidError

    client = FakeTelethonClient(sign_in_side_effect=lambda *a, **k: PhoneCodeInvalidError(request=None))
    monkeypatch.setattr(channel_monitor, "_make_client", lambda session_string="": client)

    await _feed(dp, bot, _callback_update("cm_connect", 1))
    await _feed(dp, bot, _callback_update("cm_risk_ack", 2))
    await _feed(dp, bot, _message_update("+79991234567", 3))
    for i, code in enumerate(["c1", "c2", "c3"], start=4):
        await _feed(dp, bot, _message_update(code, i))

    assert "Слишком много" in api_patch.last_text()
    state = dp.fsm.resolve_context(bot, OWNER_ID, OWNER_ID)
    assert (await state.get_state()) is None

    sessions = await get_userbot_sessions_by_owner(OWNER_ID)
    assert sessions[0]["status"] == "auth_failed"


@pytest.mark.asyncio
async def test_phone_code_expired_returns_to_waiting_phone(isolated_db, userbot_key, monkeypatch, bot, api_patch):
    dp = _make_dispatcher()
    from db.database import init_db
    await init_db()

    from telethon.errors import PhoneCodeExpiredError

    client = FakeTelethonClient(sign_in_side_effect=lambda *a, **k: PhoneCodeExpiredError(request=None))
    monkeypatch.setattr(channel_monitor, "_make_client", lambda session_string="": client)

    await _feed(dp, bot, _callback_update("cm_connect", 1))
    await _feed(dp, bot, _callback_update("cm_risk_ack", 2))
    await _feed(dp, bot, _message_update("+79991234567", 3))
    await _feed(dp, bot, _message_update("expired-code", 4))

    assert "устарел" in api_patch.last_text()
    state = dp.fsm.resolve_context(bot, OWNER_ID, OWNER_ID)
    assert (await state.get_state()) == channel_monitor.UserbotAuthStates.waiting_phone.state


@pytest.mark.asyncio
async def test_password_hash_invalid_stays_in_2fa_state(isolated_db, userbot_key, monkeypatch, bot, api_patch):
    dp = _make_dispatcher()
    from db.database import init_db
    await init_db()

    from telethon.errors import PasswordHashInvalidError, SessionPasswordNeededError

    call_count = {"n": 0}

    def sign_in_effect(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return SessionPasswordNeededError(request=None)
        return PasswordHashInvalidError(request=None)

    client = FakeTelethonClient(sign_in_side_effect=sign_in_effect)
    monkeypatch.setattr(channel_monitor, "_make_client", lambda session_string="": client)

    await _feed(dp, bot, _callback_update("cm_connect", 1))
    await _feed(dp, bot, _callback_update("cm_risk_ack", 2))
    await _feed(dp, bot, _message_update("+79991234567", 3))
    await _feed(dp, bot, _message_update("12345", 4))
    await _feed(dp, bot, _message_update("wrong-password", 5))

    assert "Неверный пароль" in api_patch.last_text()
    state = dp.fsm.resolve_context(bot, OWNER_ID, OWNER_ID)
    assert (await state.get_state()) == channel_monitor.UserbotAuthStates.waiting_2fa_password.state


@pytest.mark.asyncio
async def test_flood_wait_reports_exact_seconds_and_does_not_retry(isolated_db, userbot_key, monkeypatch, bot, api_patch):
    dp = _make_dispatcher()
    from db.database import init_db
    await init_db()

    from telethon.errors import FloodWaitError

    client = FakeTelethonClient(sign_in_side_effect=lambda *a, **k: FloodWaitError(request=None, capture=87))
    monkeypatch.setattr(channel_monitor, "_make_client", lambda session_string="": client)

    await _feed(dp, bot, _callback_update("cm_connect", 1))
    await _feed(dp, bot, _callback_update("cm_risk_ack", 2))
    await _feed(dp, bot, _message_update("+79991234567", 3))
    await _feed(dp, bot, _message_update("some-code", 4))

    assert "87" in api_patch.last_text()
    assert "подожд" in api_patch.last_text().lower()
    # FSM must be reset, not left waiting to auto-retry.
    state = dp.fsm.resolve_context(bot, OWNER_ID, OWNER_ID)
    assert (await state.get_state()) is None


@pytest.mark.asyncio
async def test_flood_wait_on_send_code_request(isolated_db, userbot_key, monkeypatch, bot, api_patch):
    """FloodWaitError can also come from send_code_request (waiting_phone
    step), not only sign_in — covered separately since it's a different call
    site in the module."""
    dp = _make_dispatcher()
    from db.database import init_db
    await init_db()

    from telethon.errors import FloodWaitError

    class FloodingClient(FakeTelethonClient):
        async def send_code_request(self, phone):
            raise FloodWaitError(request=None, capture=42)

    monkeypatch.setattr(channel_monitor, "_make_client", lambda session_string="": FloodingClient())

    await _feed(dp, bot, _callback_update("cm_connect", 1))
    await _feed(dp, bot, _callback_update("cm_risk_ack", 2))
    await _feed(dp, bot, _message_update("+79991234567", 3))

    assert "42" in api_patch.last_text()
    state = dp.fsm.resolve_context(bot, OWNER_ID, OWNER_ID)
    assert (await state.get_state()) is None


@pytest.mark.asyncio
async def test_phone_number_invalid_stays_in_waiting_phone(isolated_db, userbot_key, monkeypatch, bot, api_patch):
    dp = _make_dispatcher()
    from db.database import init_db
    await init_db()

    from telethon.errors import PhoneNumberInvalidError

    class RejectingClient(FakeTelethonClient):
        async def send_code_request(self, phone):
            raise PhoneNumberInvalidError(request=None)

    monkeypatch.setattr(channel_monitor, "_make_client", lambda session_string="": RejectingClient())

    await _feed(dp, bot, _callback_update("cm_connect", 1))
    await _feed(dp, bot, _callback_update("cm_risk_ack", 2))
    await _feed(dp, bot, _message_update("not-a-real-phone", 3))

    assert "номер" in api_patch.last_text().lower()
    state = dp.fsm.resolve_context(bot, OWNER_ID, OWNER_ID)
    assert (await state.get_state()) == channel_monitor.UserbotAuthStates.waiting_phone.state


@pytest.mark.asyncio
async def test_phone_request_cooldown_blocks_rapid_reentry(isolated_db, userbot_key, monkeypatch, bot, api_patch):
    """Security-review addition: a user re-entering waiting_phone and typing a
    NEW phone number right away must be throttled, not allowed to trigger
    another real send_code_request immediately — prevents using this bot to
    spam Telegram login codes at arbitrary phone numbers."""
    dp = _make_dispatcher()
    from db.database import init_db
    await init_db()

    client = FakeTelethonClient()
    monkeypatch.setattr(channel_monitor, "_make_client", lambda session_string="": client)

    await _feed(dp, bot, _callback_update("cm_connect", 1))
    await _feed(dp, bot, _callback_update("cm_risk_ack", 2))
    await _feed(dp, bot, _message_update("+79991111111", 3))
    first_send_code_calls = len(client.sign_in_calls)  # sign_in not yet called, just sanity baseline

    # Cancel back to menu and immediately try again with a DIFFERENT number.
    await _feed(dp, bot, _callback_update("cm_cancel", 4))
    await _feed(dp, bot, _callback_update("cm_connect", 5))
    await _feed(dp, bot, _callback_update("cm_risk_ack", 6))
    await _feed(dp, bot, _message_update("+79992222222", 7))

    assert "Подождите" in api_patch.last_text()
    # Still in waiting_phone (rejected before touching Telethon at all) — not
    # advanced to waiting_code.
    state = dp.fsm.resolve_context(bot, OWNER_ID, OWNER_ID)
    assert (await state.get_state()) == channel_monitor.UserbotAuthStates.waiting_phone.state


@pytest.mark.asyncio
async def test_missing_api_credentials_fails_explicitly(isolated_db, userbot_key, monkeypatch, bot, api_patch):
    dp = _make_dispatcher()
    from db.database import init_db
    await init_db()

    monkeypatch.setattr(channel_monitor, "TELEGRAM_API_ID", None)
    monkeypatch.setattr(channel_monitor, "TELEGRAM_API_HASH", None)

    await _feed(dp, bot, _callback_update("cm_connect", 1))
    await _feed(dp, bot, _callback_update("cm_risk_ack", 2))
    await _feed(dp, bot, _message_update("+79991234567", 3))

    assert "TELEGRAM_API_ID" in api_patch.last_text()


@pytest.mark.asyncio
async def test_risk_screen_shown_before_phone_requested(isolated_db, userbot_key, bot, api_patch):
    """§6 of the design doc: risk text + explicit ack button must appear
    BEFORE any phone number is ever requested."""
    dp = _make_dispatcher()
    from db.database import init_db
    await init_db()

    await _feed(dp, bot, _callback_update("cm_connect", 1))

    edited = [c for c in api_patch.calls if isinstance(c, EditMessageText)]
    assert edited, "cm_connect must edit the message to show the risk screen"
    assert "личный Telegram-аккаунт" in edited[-1].text
    assert edited[-1].reply_markup is not None
    button_texts = [b.text for row in edited[-1].reply_markup.inline_keyboard for b in row]
    assert "✅ Понимаю и продолжаю" in button_texts

    state = dp.fsm.resolve_context(bot, OWNER_ID, OWNER_ID)
    assert (await state.get_state()) == channel_monitor.UserbotAuthStates.waiting_risk_ack.state
