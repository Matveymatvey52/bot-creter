"""db/database.py — channel_monitor schema and access functions.

Covers table creation (init_channel_monitor_tables) and insert/get/update for
userbot_sessions, monitored_channels, channel_posts — all scoped to a per-bot
db_path (docs/USERBOT_FEATURE_DESIGN.md §2 Variant A: one session per bot_id,
isolated). Run against tests/conftest.py's isolated_db fixture (a throwaway
tmp_path file) — never the real data/bots.db, and never the shared central
DB_PATH the way the old owner-scoped design used.
"""
from __future__ import annotations

import sqlite3

import pytest

from db.database import (
    add_channel_post,
    add_monitored_channel,
    create_userbot_session,
    get_active_monitored_channels,
    get_active_userbot_sessions,
    get_monitored_channel,
    get_monitored_channels_by_bot,
    get_posts_missing_summary,
    get_recent_posts_for_bot,
    get_userbot_session,
    get_userbot_sessions_by_bot,
    init_channel_monitor_tables,
    revoke_userbot_session,
    set_channel_post_summary,
    set_monitored_channel_active,
    set_userbot_session_active,
    set_userbot_session_status,
)

pytestmark = pytest.mark.asyncio

BOT_A = 111
BOT_B = 222


async def test_init_db_creates_new_tables(isolated_db):
    await init_channel_monitor_tables(isolated_db)
    conn = sqlite3.connect(isolated_db)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert {"userbot_sessions", "monitored_channels", "channel_posts"} <= tables


async def test_init_db_is_idempotent(isolated_db):
    await init_channel_monitor_tables(isolated_db)
    await init_channel_monitor_tables(isolated_db)  # must not raise on second call (CREATE TABLE IF NOT EXISTS)


async def test_create_userbot_session_defaults_to_pending(isolated_db, userbot_key):
    await init_channel_monitor_tables(isolated_db)
    session_id = await create_userbot_session(isolated_db, bot_id=BOT_A, phone="+79991234567")
    row = await get_userbot_session(isolated_db, session_id)
    assert row["status"] == "pending"
    assert row["bot_id"] == BOT_A
    assert row["phone"] == "+79991234567"
    assert row["session_string"] is None


async def test_set_userbot_session_active_encrypts_and_stores(isolated_db, userbot_key):
    await init_channel_monitor_tables(isolated_db)
    session_id = await create_userbot_session(isolated_db, BOT_A, "+79991234567")
    await set_userbot_session_active(isolated_db, session_id, "fake-session-string-value")
    row = await get_userbot_session(isolated_db, session_id)
    assert row["status"] == "active"
    assert row["session_string"] == "fake-session-string-value"

    # Verify it's actually encrypted at rest, not plaintext.
    conn = sqlite3.connect(isolated_db)
    raw = conn.execute("SELECT session_string FROM userbot_sessions WHERE id=?", (session_id,)).fetchone()[0]
    conn.close()
    assert raw != "fake-session-string-value"
    assert "fake-session-string-value" not in raw


async def test_revoke_userbot_session_wipes_session_string(isolated_db, userbot_key):
    await init_channel_monitor_tables(isolated_db)
    session_id = await create_userbot_session(isolated_db, BOT_A, "+79991234567")
    await set_userbot_session_active(isolated_db, session_id, "fake-session-string-value")
    await revoke_userbot_session(isolated_db, session_id)
    row = await get_userbot_session(isolated_db, session_id)
    assert row["status"] == "revoked"
    assert row["session_string"] is None


async def test_set_userbot_session_status(isolated_db, userbot_key):
    await init_channel_monitor_tables(isolated_db)
    session_id = await create_userbot_session(isolated_db, BOT_A, "+79991234567")
    await set_userbot_session_status(isolated_db, session_id, "auth_failed")
    row = await get_userbot_session(isolated_db, session_id)
    assert row["status"] == "auth_failed"


async def test_get_userbot_sessions_by_bot_isolated(isolated_db, userbot_key):
    """A single db_path holds only one bot's rows in production (per-bot
    db_path, §2 Variant A) — but this test still verifies the bot_id filter
    itself doesn't leak a row tagged with another bot_id, in case two bots'
    rows ever land in the same file (e.g. shared test db)."""
    await init_channel_monitor_tables(isolated_db)
    s1 = await create_userbot_session(isolated_db, BOT_A, "+7111")
    s2 = await create_userbot_session(isolated_db, BOT_B, "+7222")
    rows_a = await get_userbot_sessions_by_bot(isolated_db, BOT_A)
    ids = {r["id"] for r in rows_a}
    assert s1 in ids
    assert s2 not in ids


async def test_get_active_userbot_sessions_only_active(isolated_db, userbot_key):
    await init_channel_monitor_tables(isolated_db)
    s1 = await create_userbot_session(isolated_db, BOT_A, "+7111")
    s2 = await create_userbot_session(isolated_db, BOT_A, "+7222")
    await set_userbot_session_active(isolated_db, s1, "session-a")
    active = await get_active_userbot_sessions(isolated_db)
    ids = {r["id"] for r in active}
    assert s1 in ids
    assert s2 not in ids


async def test_monitored_channels_crud(isolated_db):
    await init_channel_monitor_tables(isolated_db)
    channel_id = await add_monitored_channel(
        isolated_db, BOT_A, channel_username="somechannel", channel_id=-1001234, channel_title="Some Channel",
    )
    row = await get_monitored_channel(isolated_db, channel_id)
    assert row["channel_username"] == "somechannel"
    assert row["active"] == 1

    channels = await get_monitored_channels_by_bot(isolated_db, BOT_A)
    assert any(c["id"] == channel_id for c in channels)

    await set_monitored_channel_active(isolated_db, channel_id, False)
    row = await get_monitored_channel(isolated_db, channel_id)
    assert row["active"] == 0

    active_only = await get_active_monitored_channels(isolated_db)
    assert not any(c["id"] == channel_id for c in active_only)


async def test_monitored_channels_isolated_by_bot(isolated_db):
    await init_channel_monitor_tables(isolated_db)
    await add_monitored_channel(isolated_db, BOT_A, "chan_a", -1, "A")
    await add_monitored_channel(isolated_db, BOT_B, "chan_b", -2, "B")
    owned_by_a = await get_monitored_channels_by_bot(isolated_db, BOT_A)
    assert len(owned_by_a) == 1
    assert owned_by_a[0]["channel_username"] == "chan_a"


async def test_channel_posts_crud(isolated_db):
    await init_channel_monitor_tables(isolated_db)
    channel_id = await add_monitored_channel(isolated_db, BOT_A, "chan", -1, "Chan")
    post_id = await add_channel_post(isolated_db, channel_id, message_id=42, text="hello world")
    missing = await get_posts_missing_summary(isolated_db)
    assert any(p["id"] == post_id for p in missing)

    await set_channel_post_summary(isolated_db, post_id, "short summary")
    missing_after = await get_posts_missing_summary(isolated_db)
    assert not any(p["id"] == post_id for p in missing_after)


async def test_get_recent_posts_for_bot_only_active_channels(isolated_db):
    await init_channel_monitor_tables(isolated_db)
    active_channel = await add_monitored_channel(isolated_db, BOT_A, "active_chan", -1, "Active")
    inactive_channel = await add_monitored_channel(isolated_db, BOT_A, "inactive_chan", -2, "Inactive")
    await set_monitored_channel_active(isolated_db, inactive_channel, False)

    await add_channel_post(isolated_db, active_channel, 1, "post from active channel")
    await add_channel_post(isolated_db, inactive_channel, 1, "post from inactive channel")

    feed = await get_recent_posts_for_bot(isolated_db, BOT_A)
    texts = {p["text"] for p in feed}
    assert "post from active channel" in texts
    assert "post from inactive channel" not in texts


async def test_get_recent_posts_for_bot_isolated_by_bot(isolated_db):
    await init_channel_monitor_tables(isolated_db)
    channel_a = await add_monitored_channel(isolated_db, BOT_A, "a", -1, "A")
    channel_b = await add_monitored_channel(isolated_db, BOT_B, "b", -2, "B")
    await add_channel_post(isolated_db, channel_a, 1, "post a")
    await add_channel_post(isolated_db, channel_b, 1, "post b")

    feed_a = await get_recent_posts_for_bot(isolated_db, BOT_A)
    texts = {p["text"] for p in feed_a}
    assert "post a" in texts
    assert "post b" not in texts
