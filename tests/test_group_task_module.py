"""features/group_task.py — on_group_message()'s gating/auto-connect/reply
contract, per docs/GROUP_TASK_CHANNEL_DESIGN.md.

Uses the isolated_db fixture (tests/conftest.py) so this never touches the
real data/bots.db — see MEMORY.md "Backlog: test DB isolation" (same reason
tests/test_office_events_db.py uses it; unlike the older
tests/test_manage_bots_office.py, which predates that fixture and still
hits the real DB). Anthropic client mocked out (no real API call), OWNER_ID
patched per test via monkeypatch.

Run with: python -m pytest tests/test_group_task_module.py
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import features.group_task as group_task
from db.database import (
    create_bot_record_with_admins,
    delete_group_task_config,
    get_group_task_config,
    init_db,
    set_group_task_config,
)

FAKE_TOKEN = "123456:test-token-not-real"
BOT_USERNAME = "test_group_task_bot"
OWNER_ID = 620100


async def _make_bot_row() -> int:
    await init_db()
    return await create_bot_record_with_admins(
        "group-task-bot", "desc", FAKE_TOKEN, "templates/tour_operator.py", []
    )


def _make_message(
    *,
    user_id: int,
    text: str,
    chat_id: int = -100123456,
    reply_to_bot: bool = False,
):
    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id)
    message.text = text
    message.caption = None
    message.chat = SimpleNamespace(id=chat_id, type="supergroup")
    message.reply = AsyncMock()
    if reply_to_bot:
        message.reply_to_message = SimpleNamespace(
            from_user=SimpleNamespace(is_bot=True, username=BOT_USERNAME)
        )
    else:
        message.reply_to_message = None
    return message


def _make_bot():
    bot = MagicMock()
    bot.get_me = AsyncMock(return_value=SimpleNamespace(username=BOT_USERNAME))
    return bot


@pytest.fixture(autouse=True)
def _owner_and_context(monkeypatch):
    monkeypatch.setattr(group_task, "OWNER_ID", OWNER_ID)
    group_task._conversation_context.clear()
    group_task._last_reply_sent.clear()
    yield
    group_task._conversation_context.clear()
    group_task._last_reply_sent.clear()


@pytest.mark.asyncio
async def test_ignores_message_from_non_owner(isolated_db):
    bot_id = await _make_bot_row()
    message = _make_message(user_id=999999, text=f"@{BOT_USERNAME} hello")

    await group_task.on_group_message(message, _make_bot(), bot_id)

    message.reply.assert_not_awaited()
    assert await get_group_task_config(bot_id) is None


@pytest.mark.asyncio
async def test_ignores_message_with_no_mention_or_reply(isolated_db):
    bot_id = await _make_bot_row()
    message = _make_message(user_id=OWNER_ID, text="just chatting, no mention here")

    await group_task.on_group_message(message, _make_bot(), bot_id)

    message.reply.assert_not_awaited()
    assert await get_group_task_config(bot_id) is None


@pytest.mark.asyncio
async def test_first_addressed_message_auto_connects_and_confirms(isolated_db):
    bot_id = await _make_bot_row()
    message = _make_message(user_id=OWNER_ID, text=f"@{BOT_USERNAME} привет", chat_id=-100999)

    await group_task.on_group_message(message, _make_bot(), bot_id)

    config = await get_group_task_config(bot_id)
    assert config["group_chat_id"] == -100999
    assert config["enabled"] == 1
    message.reply.assert_awaited_once()
    assert "Готово" in message.reply.call_args.args[0]


@pytest.mark.asyncio
async def test_reply_to_bot_message_also_auto_connects(isolated_db):
    bot_id = await _make_bot_row()
    message = _make_message(user_id=OWNER_ID, text="do this task", reply_to_bot=True)

    await group_task.on_group_message(message, _make_bot(), bot_id)

    assert await get_group_task_config(bot_id) is not None
    message.reply.assert_awaited_once()


@pytest.mark.asyncio
async def test_ignores_addressed_message_from_a_different_group(isolated_db):
    bot_id = await _make_bot_row()
    await set_group_task_config(bot_id, -100111)
    message = _make_message(user_id=OWNER_ID, text=f"@{BOT_USERNAME} task", chat_id=-100222)

    with patch("anthropic.AsyncAnthropic") as mock_client_cls:
        await group_task.on_group_message(message, _make_bot(), bot_id)

    mock_client_cls.assert_not_called()
    message.reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_addressed_message_in_bound_group_gets_a_haiku_reply(isolated_db):
    bot_id = await _make_bot_row()
    await set_group_task_config(bot_id, -100333)
    fake_response = SimpleNamespace(content=[SimpleNamespace(text="Сделаю.")])
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=fake_response)
    message = _make_message(user_id=OWNER_ID, text=f"@{BOT_USERNAME} сделай отчёт", chat_id=-100333)

    with patch("anthropic.AsyncAnthropic", return_value=mock_client):
        await group_task.on_group_message(message, _make_bot(), bot_id)

    mock_client.messages.create.assert_awaited_once()
    message.reply.assert_awaited_once_with("Сделаю.")


@pytest.mark.asyncio
async def test_rate_limit_suppresses_rapid_second_call(isolated_db):
    bot_id = await _make_bot_row()
    await set_group_task_config(bot_id, -100333)
    fake_response = SimpleNamespace(content=[SimpleNamespace(text="ok")])
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=fake_response)
    message1 = _make_message(user_id=OWNER_ID, text=f"@{BOT_USERNAME} one", chat_id=-100333)
    message2 = _make_message(user_id=OWNER_ID, text=f"@{BOT_USERNAME} two", chat_id=-100333)

    with patch("anthropic.AsyncAnthropic", return_value=mock_client):
        await group_task.on_group_message(message1, _make_bot(), bot_id)
        await group_task.on_group_message(message2, _make_bot(), bot_id)

    assert mock_client.messages.create.await_count == 1
    message2.reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_config_is_ignored(isolated_db, monkeypatch):
    bot_id = await _make_bot_row()
    await set_group_task_config(bot_id, -100333)
    # simulate a disabled row directly — there's no public "disable without
    # deleting" API yet, this exercises the enabled-check branch only.
    import aiosqlite

    import db.database as db_module

    async with aiosqlite.connect(db_module.DB_PATH) as db:
        await db.execute("UPDATE bot_group_task_config SET enabled = 0 WHERE bot_id = ?", (bot_id,))
        await db.commit()

    message = _make_message(user_id=OWNER_ID, text=f"@{BOT_USERNAME} task", chat_id=-100333)
    with patch("anthropic.AsyncAnthropic") as mock_client_cls:
        await group_task.on_group_message(message, _make_bot(), bot_id)

    mock_client_cls.assert_not_called()
    message.reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_group_task_config_disconnects(isolated_db):
    bot_id = await _make_bot_row()
    await set_group_task_config(bot_id, -100333)
    await delete_group_task_config(bot_id)
    assert await get_group_task_config(bot_id) is None


@pytest.mark.asyncio
async def test_invalidate_group_task_state_clears_in_memory_dicts(isolated_db):
    bot_id = await _make_bot_row()
    await set_group_task_config(bot_id, -100333)
    group_task._conversation_context[bot_id] = [("user", "hi")]
    group_task._last_reply_sent[bot_id] = 123.0

    group_task.invalidate_group_task_state(bot_id)

    assert bot_id not in group_task._conversation_context
    assert bot_id not in group_task._last_reply_sent


def test_invalidate_group_task_state_is_a_noop_for_unknown_bot_id():
    group_task.invalidate_group_task_state(999999999)  # must not raise
