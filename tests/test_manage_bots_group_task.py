"""Creator-bot "💬 Рабочая группа" UI (handlers/manage_bots.py's grouptask:/
grouptaskdisconnect: callbacks), per docs/GROUP_TASK_CHANNEL_DESIGN.md §5.

Uses the isolated_db fixture (tests/conftest.py) so this never touches the
real data/bots.db — see MEMORY.md "Backlog: test DB isolation" (unlike the
older tests/test_manage_bots_office.py, which predates that fixture).

Run with: python -m pytest tests/test_manage_bots_group_task.py
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import handlers.manage_bots as manage_bots
from db.database import create_bot_record_with_admins, get_bot_features, get_group_task_config, init_db, set_group_task_config

FAKE_TOKEN = "123456:test-token-not-real"
OWNER_ID = 620200


def _make_callback(user_id: int, data: str) -> MagicMock:
    callback = MagicMock()
    callback.from_user.id = user_id
    callback.data = data
    callback.answer = AsyncMock()
    callback.message.edit_text = AsyncMock()
    callback.message.answer = AsyncMock()
    return callback


async def _make_bot_row(username: str = "group_task_ui_bot") -> int:
    await init_db()
    return await create_bot_record_with_admins(
        "group-task-ui-bot", "desc", FAKE_TOKEN, "templates/tour_operator.py", [], username=username,
    )


@pytest.fixture(autouse=True)
def _owner(monkeypatch):
    monkeypatch.setattr(manage_bots, "_is_owner", lambda uid: uid == OWNER_ID)


@pytest.mark.asyncio
async def test_panel_shows_setup_guide_before_connecting(isolated_db):
    bot_id = await _make_bot_row()
    callback = _make_callback(OWNER_ID, f"grouptask:{bot_id}")

    await manage_bots.cb_group_task_panel(callback)

    callback.answer.assert_awaited_once()
    callback.message.edit_text.assert_awaited_once()
    text = callback.message.edit_text.call_args.args[0]
    assert "Group Privacy" in text
    assert "group_task_ui_bot" in text


@pytest.mark.asyncio
async def test_panel_enables_the_feature(isolated_db):
    bot_id = await _make_bot_row()
    callback = _make_callback(OWNER_ID, f"grouptask:{bot_id}")

    with patch.object(manage_bots, "_reload_registry", new=AsyncMock()):
        await manage_bots.cb_group_task_panel(callback)

    assert "group_task" in await get_bot_features(bot_id)


@pytest.mark.asyncio
async def test_panel_shows_connected_state_after_binding(isolated_db):
    bot_id = await _make_bot_row()
    await set_group_task_config(bot_id, -100333)
    callback = _make_callback(OWNER_ID, f"grouptask:{bot_id}")

    with patch.object(manage_bots, "_reload_registry", new=AsyncMock()):
        await manage_bots.cb_group_task_panel(callback)

    text = callback.message.edit_text.call_args.args[0]
    assert "Подключено" in text
    button_texts = [
        btn.text
        for row in callback.message.edit_text.call_args.kwargs["reply_markup"].inline_keyboard
        for btn in row
    ]
    assert "🔌 Отключить" in button_texts


@pytest.mark.asyncio
async def test_disconnect_removes_config(isolated_db):
    bot_id = await _make_bot_row()
    await set_group_task_config(bot_id, -100333)
    callback = _make_callback(OWNER_ID, f"grouptaskdisconnect:{bot_id}")

    await manage_bots.cb_group_task_disconnect(callback)

    assert await get_group_task_config(bot_id) is None
    text = callback.message.edit_text.call_args.args[0]
    assert "Group Privacy" in text  # back to the setup-guide state


@pytest.mark.asyncio
async def test_non_owner_denied(isolated_db):
    bot_id = await _make_bot_row()
    callback = _make_callback(999999, f"grouptask:{bot_id}")

    await manage_bots.cb_group_task_panel(callback)

    callback.message.edit_text.assert_not_awaited()
    assert await get_group_task_config(bot_id) is None


@pytest.mark.asyncio
async def test_missing_bot_is_handled_gracefully(isolated_db):
    await init_db()
    callback = _make_callback(OWNER_ID, "grouptask:999999999")

    await manage_bots.cb_group_task_panel(callback)

    callback.message.answer.assert_awaited_once()
    assert "не найден" in callback.message.answer.call_args.args[0].lower()
