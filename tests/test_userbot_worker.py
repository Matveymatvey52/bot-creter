"""runtime/userbot_worker.py — UserbotManager: boot-time restore of active
clients, revoke stopping a client, and event handling routing new posts into
channel_posts — now scoped per-bot (docs/USERBOT_FEATURE_DESIGN.md §2 Variant
A: one userbot session per bot_id, stored in that bot's own db_path).

All Telethon interaction is faked via UserbotManager._make_client (patched
per test) — no real network calls. get_channel_monitor_bot_ids/get_all_bots/
get_bot_features are patched directly rather than driving the real bots
table, since this module's job is per-bot-db_path plumbing, not bot
discovery itself (covered elsewhere).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from db.database import (
    add_monitored_channel,
    create_userbot_session,
    get_userbot_session,
    init_channel_monitor_tables,
    set_monitored_channel_active,
    set_userbot_session_active,
)
from runtime.userbot_worker import UserbotManager

pytestmark = pytest.mark.asyncio

BOT_A = 111
BOT_B = 222


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
    await init_channel_monitor_tables(isolated_db)
    active_id = await create_userbot_session(isolated_db, BOT_A, "+7111")
    await set_userbot_session_active(isolated_db, active_id, "session-a")
    pending_id = await create_userbot_session(isolated_db, BOT_A, "+7112")  # stays 'pending', no session_string

    monkeypatch.setattr("runtime.userbot_worker.get_channel_monitor_bot_ids", AsyncMock(return_value=[BOT_A]))
    monkeypatch.setattr("runtime.userbot_worker._db_path_for_bot", lambda bot_id: isolated_db)

    fake_clients = {}

    def make_client(session_string):
        c = FakeTelethonClient()
        fake_clients[session_string] = c
        return c

    monkeypatch.setattr(manager, "_make_client", make_client)
    await manager.start_all()

    assert (BOT_A, active_id) in manager.clients
    assert (BOT_A, pending_id) not in manager.clients
    assert len(manager.clients) == 1


async def test_start_one_marks_auth_failed_on_connect_error(isolated_db, userbot_key, monkeypatch, manager):
    await init_channel_monitor_tables(isolated_db)
    session_id = await create_userbot_session(isolated_db, BOT_A, "+7111")
    await set_userbot_session_active(isolated_db, session_id, "session-a")

    monkeypatch.setattr("runtime.userbot_worker._db_path_for_bot", lambda bot_id: isolated_db)

    def make_client(session_string):
        return FakeTelethonClient(connect_raises=ConnectionError("network down"))

    monkeypatch.setattr(manager, "_make_client", make_client)
    started = await manager.start_one(BOT_A, await get_userbot_session(isolated_db, session_id))

    assert started is False
    assert (BOT_A, session_id) not in manager.clients
    row = await get_userbot_session(isolated_db, session_id)
    assert row["status"] == "auth_failed"


async def test_start_one_marks_auth_failed_when_no_longer_authorized(isolated_db, userbot_key, monkeypatch, manager):
    await init_channel_monitor_tables(isolated_db)
    session_id = await create_userbot_session(isolated_db, BOT_A, "+7111")
    await set_userbot_session_active(isolated_db, session_id, "session-a")

    monkeypatch.setattr("runtime.userbot_worker._db_path_for_bot", lambda bot_id: isolated_db)
    monkeypatch.setattr(manager, "_make_client", lambda s: FakeTelethonClient(authorized=False))
    started = await manager.start_one(BOT_A, await get_userbot_session(isolated_db, session_id))

    assert started is False
    row = await get_userbot_session(isolated_db, session_id)
    assert row["status"] == "auth_failed"


async def test_start_one_skips_session_with_no_session_string(isolated_db, userbot_key, manager, monkeypatch):
    await init_channel_monitor_tables(isolated_db)
    session_id = await create_userbot_session(isolated_db, BOT_A, "+7111")  # 'pending', no session_string
    row = await get_userbot_session(isolated_db, session_id)

    monkeypatch.setattr("runtime.userbot_worker._db_path_for_bot", lambda bot_id: isolated_db)
    started = await manager.start_one(BOT_A, row)

    assert started is False
    assert (BOT_A, session_id) not in manager.clients


async def test_revoke_disconnects_and_removes_client(isolated_db, userbot_key, monkeypatch, manager):
    await init_channel_monitor_tables(isolated_db)
    session_id = await create_userbot_session(isolated_db, BOT_A, "+7111")
    await set_userbot_session_active(isolated_db, session_id, "session-a")

    monkeypatch.setattr("runtime.userbot_worker._db_path_for_bot", lambda bot_id: isolated_db)
    fake_client = FakeTelethonClient()
    monkeypatch.setattr(manager, "_make_client", lambda s: fake_client)
    await manager.start_one(BOT_A, await get_userbot_session(isolated_db, session_id))
    assert (BOT_A, session_id) in manager.clients

    await manager.revoke(BOT_A, session_id)

    assert (BOT_A, session_id) not in manager.clients
    assert (BOT_A, session_id) not in manager.tasks
    assert fake_client.disconnected is True


async def test_revoke_on_unknown_session_is_a_noop(manager):
    # Must not raise even if this session_id was never started.
    await manager.revoke(BOT_A, 999999)


async def test_handle_event_stores_post_for_monitored_channel(isolated_db, manager, monkeypatch):
    await init_channel_monitor_tables(isolated_db)
    channel_row_id = await add_monitored_channel(isolated_db, BOT_A, "somechan", channel_id=-1001111, channel_title="Some Chan")
    manager._channel_row_ids_by_telegram_id = {-1001111: [(BOT_A, channel_row_id)]}
    monkeypatch.setattr("runtime.userbot_worker._db_path_for_bot", lambda bot_id: isolated_db)

    event = SimpleNamespace(
        chat_id=-1001111,
        message=SimpleNamespace(id=42, message="hello from the channel"),
    )
    await manager._handle_event(event)

    from db.database import get_recent_posts_for_bot
    posts = await get_recent_posts_for_bot(isolated_db, BOT_A)
    assert any(p["text"] == "hello from the channel" for p in posts)


async def test_handle_event_ignores_unmonitored_channel(isolated_db, manager, monkeypatch):
    await init_channel_monitor_tables(isolated_db)
    manager._channel_row_ids_by_telegram_id = {}  # nothing monitored
    monkeypatch.setattr("runtime.userbot_worker._db_path_for_bot", lambda bot_id: isolated_db)

    event = SimpleNamespace(chat_id=-1009999, message=SimpleNamespace(id=1, message="stray post"))
    await manager._handle_event(event)  # must not raise, must not write anything

    from db.database import get_posts_missing_summary
    missing = await get_posts_missing_summary(isolated_db)
    assert not any(p["text"] == "stray post" for p in missing)


async def test_handle_event_fans_out_to_multiple_bots_of_same_channel(tmp_path, manager, monkeypatch):
    """Two different bots independently monitor the same public Telegram
    channel — a one-to-one channel_id mapping would silently drop one of
    them; each bot's own monitored_channels row (in its own db_path) must get
    its own post."""
    db_a = str(tmp_path / "bot_a.db")
    db_b = str(tmp_path / "bot_b.db")
    await init_channel_monitor_tables(db_a)
    await init_channel_monitor_tables(db_b)
    row_a = await add_monitored_channel(db_a, BOT_A, "shared", channel_id=-1005555, channel_title="Shared")
    row_b = await add_monitored_channel(db_b, BOT_B, "shared", channel_id=-1005555, channel_title="Shared")
    manager._channel_row_ids_by_telegram_id = {-1005555: [(BOT_A, row_a), (BOT_B, row_b)]}

    db_paths = {BOT_A: db_a, BOT_B: db_b}
    monkeypatch.setattr("runtime.userbot_worker._db_path_for_bot", lambda bot_id: db_paths[bot_id])

    event = SimpleNamespace(
        chat_id=-1005555,
        message=SimpleNamespace(id=7, message="shared channel post"),
    )
    await manager._handle_event(event)

    from db.database import get_recent_posts_for_bot
    posts_a = await get_recent_posts_for_bot(db_a, BOT_A)
    posts_b = await get_recent_posts_for_bot(db_b, BOT_B)
    assert any(p["text"] == "shared channel post" for p in posts_a)
    assert any(p["text"] == "shared channel post" for p in posts_b)


async def test_start_all_only_registers_active_monitored_channels(isolated_db, userbot_key, monkeypatch, manager):
    await init_channel_monitor_tables(isolated_db)
    session_id = await create_userbot_session(isolated_db, BOT_A, "+7111")
    await set_userbot_session_active(isolated_db, session_id, "session-a")
    active_channel = await add_monitored_channel(isolated_db, BOT_A, "active", channel_id=-1, channel_title="Active")
    inactive_channel = await add_monitored_channel(isolated_db, BOT_A, "inactive", channel_id=-2, channel_title="Inactive")
    await set_monitored_channel_active(isolated_db, inactive_channel, False)

    monkeypatch.setattr("runtime.userbot_worker.get_channel_monitor_bot_ids", AsyncMock(return_value=[BOT_A]))
    monkeypatch.setattr("runtime.userbot_worker._db_path_for_bot", lambda bot_id: isolated_db)
    monkeypatch.setattr(manager, "_make_client", lambda s: FakeTelethonClient())
    await manager.start_all()

    assert -1 in manager._channel_row_ids_by_telegram_id
    assert -2 not in manager._channel_row_ids_by_telegram_id


async def test_get_channel_monitor_bot_ids_filters_by_feature(monkeypatch):
    from runtime.userbot_worker import get_channel_monitor_bot_ids

    all_bots = [{"id": BOT_A}, {"id": BOT_B}, {"id": 333}]
    monkeypatch.setattr("runtime.userbot_worker.get_all_bots", AsyncMock(return_value=all_bots))

    async def fake_get_bot_features(bot_id):
        return ["channel_monitor"] if bot_id in (BOT_A, 333) else ["payments"]

    monkeypatch.setattr("runtime.userbot_worker.get_bot_features", fake_get_bot_features)

    result = await get_channel_monitor_bot_ids()
    assert set(result) == {BOT_A, 333}
