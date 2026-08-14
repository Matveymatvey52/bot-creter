"""runtime/userbot_worker.py — UserbotManager: boot-time restore of active
clients, revoke stopping a client, and event handling routing new posts into
channel_posts. All Telethon interaction is faked via UserbotManager._make_client
(patched per test) — no real network calls.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from db.database import (
    add_monitored_channel,
    create_userbot_session,
    get_userbot_session,
    init_db,
    set_monitored_channel_active,
    set_userbot_session_active,
)
from runtime.userbot_worker import UserbotManager

pytestmark = pytest.mark.asyncio


class FakeTelethonClient:
    def __init__(self, authorized=True, connect_raises=None):
        self.authorized = authorized
        self.connect_raises = connect_raises
        self.connected = False
        self.disconnected = False
        self.event_handlers = []
        self.run_until_disconnected = AsyncMock()

    async def connect(self):
        if self.connect_raises:
            raise self.connect_raises
        self.connected = True

    async def disconnect(self):
        self.disconnected = True

    async def is_user_authorized(self):
        return self.authorized

    def on(self, event_filter):
        def _decorator(fn):
            self.event_handlers.append(fn)
            return fn
        return _decorator


@pytest.fixture
def manager(monkeypatch):
    mgr = UserbotManager()
    return mgr


async def test_start_all_starts_only_active_sessions(isolated_db, userbot_key, monkeypatch, manager):
    await init_db()
    active_id = await create_userbot_session(111, "+7111")
    await set_userbot_session_active(active_id, "session-a")
    pending_id = await create_userbot_session(222, "+7222")  # stays 'pending', no session_string

    fake_clients = {}

    def make_client(session_string):
        c = FakeTelethonClient()
        fake_clients[session_string] = c
        return c

    monkeypatch.setattr(manager, "_make_client", make_client)
    await manager.start_all()

    assert active_id in manager.clients
    assert pending_id not in manager.clients
    assert len(manager.clients) == 1


async def test_start_one_marks_auth_failed_on_connect_error(isolated_db, userbot_key, monkeypatch, manager):
    await init_db()
    session_id = await create_userbot_session(111, "+7111")
    await set_userbot_session_active(session_id, "session-a")

    def make_client(session_string):
        return FakeTelethonClient(connect_raises=ConnectionError("network down"))

    monkeypatch.setattr(manager, "_make_client", make_client)
    started = await manager.start_one(await get_userbot_session(session_id))

    assert started is False
    assert session_id not in manager.clients
    row = await get_userbot_session(session_id)
    assert row["status"] == "auth_failed"


async def test_start_one_marks_auth_failed_when_no_longer_authorized(isolated_db, userbot_key, monkeypatch, manager):
    await init_db()
    session_id = await create_userbot_session(111, "+7111")
    await set_userbot_session_active(session_id, "session-a")

    monkeypatch.setattr(manager, "_make_client", lambda s: FakeTelethonClient(authorized=False))
    started = await manager.start_one(await get_userbot_session(session_id))

    assert started is False
    row = await get_userbot_session(session_id)
    assert row["status"] == "auth_failed"


async def test_start_one_skips_session_with_no_session_string(isolated_db, userbot_key, manager):
    await init_db()
    session_id = await create_userbot_session(111, "+7111")  # 'pending', no session_string
    row = await get_userbot_session(session_id)

    started = await manager.start_one(row)

    assert started is False
    assert session_id not in manager.clients


async def test_revoke_disconnects_and_removes_client(isolated_db, userbot_key, monkeypatch, manager):
    await init_db()
    session_id = await create_userbot_session(111, "+7111")
    await set_userbot_session_active(session_id, "session-a")

    fake_client = FakeTelethonClient()
    monkeypatch.setattr(manager, "_make_client", lambda s: fake_client)
    await manager.start_one(await get_userbot_session(session_id))
    assert session_id in manager.clients

    await manager.revoke(session_id)

    assert session_id not in manager.clients
    assert session_id not in manager.tasks
    assert fake_client.disconnected is True


async def test_revoke_on_unknown_session_is_a_noop(manager):
    # Must not raise even if this session_id was never started.
    await manager.revoke(999999)


async def test_handle_event_stores_post_for_monitored_channel(isolated_db, manager):
    await init_db()
    channel_row_id = await add_monitored_channel(111, "somechan", channel_id=-1001111, channel_title="Some Chan")
    manager._channel_row_ids_by_telegram_id = {-1001111: [channel_row_id]}

    event = SimpleNamespace(
        chat_id=-1001111,
        message=SimpleNamespace(id=42, message="hello from the channel"),
    )
    await manager._handle_event(event)

    from db.database import get_recent_posts_for_owner
    posts = await get_recent_posts_for_owner(111)
    assert any(p["text"] == "hello from the channel" for p in posts)


async def test_handle_event_ignores_unmonitored_channel(isolated_db, manager):
    await init_db()
    manager._channel_row_ids_by_telegram_id = {}  # nothing monitored

    event = SimpleNamespace(chat_id=-1009999, message=SimpleNamespace(id=1, message="stray post"))
    await manager._handle_event(event)  # must not raise, must not write anything

    from db.database import get_posts_missing_summary
    missing = await get_posts_missing_summary()
    assert not any(p["text"] == "stray post" for p in missing)


async def test_handle_event_fans_out_to_multiple_owners_of_same_channel(isolated_db, manager):
    """Two different owners independently monitor the same public Telegram
    channel — a one-to-one channel_id mapping would silently drop one of
    them; each owner's monitored_channels row must get its own post."""
    await init_db()
    row_owner_a = await add_monitored_channel(111, "shared", channel_id=-1005555, channel_title="Shared")
    row_owner_b = await add_monitored_channel(222, "shared", channel_id=-1005555, channel_title="Shared")
    manager._channel_row_ids_by_telegram_id = {-1005555: [row_owner_a, row_owner_b]}

    event = SimpleNamespace(
        chat_id=-1005555,
        message=SimpleNamespace(id=7, message="shared channel post"),
    )
    await manager._handle_event(event)

    from db.database import get_recent_posts_for_owner
    posts_a = await get_recent_posts_for_owner(111)
    posts_b = await get_recent_posts_for_owner(222)
    assert any(p["text"] == "shared channel post" for p in posts_a)
    assert any(p["text"] == "shared channel post" for p in posts_b)


async def test_start_all_only_registers_active_monitored_channels(isolated_db, userbot_key, monkeypatch, manager):
    await init_db()
    session_id = await create_userbot_session(111, "+7111")
    await set_userbot_session_active(session_id, "session-a")
    active_channel = await add_monitored_channel(111, "active", channel_id=-1, channel_title="Active")
    inactive_channel = await add_monitored_channel(111, "inactive", channel_id=-2, channel_title="Inactive")
    await set_monitored_channel_active(inactive_channel, False)

    monkeypatch.setattr(manager, "_make_client", lambda s: FakeTelethonClient())
    await manager.start_all()

    assert -1 in manager._channel_row_ids_by_telegram_id
    assert -2 not in manager._channel_row_ids_by_telegram_id
