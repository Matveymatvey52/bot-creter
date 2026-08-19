"""templates/manager_bot.py's on_office_event() — the office_events
subscriber that copies task.assigned into incoming_tasks and best-effort
notifies registered managers.

Calls on_office_event() directly (a plain async function, not a router
handler — registry.py wraps it into a hook, but the function itself needs no
Dispatcher/Bot), same style as tests/test_event_rsvp_office_events.py.

Run with: python -m pytest tests/test_manager_bot_office_event.py
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from features.office_events import OfficeEvent, TaskAssignedEvent
from templates import manager_bot

SOURCE_BOT_ID = 7
BOT_ID = 42


@pytest.fixture
def db_path():
    with tempfile.TemporaryDirectory() as tmp:
        yield str(Path(tmp) / "fixture.db")


def _config(db_path: str) -> manager_bot.ManagerBotConfig:
    return manager_bot.ManagerBotConfig(
        bot_name="test-manager-bot",
        db_path=db_path,
        admins_file=str(Path(db_path).with_suffix(".admins.json")),
        welcome_image=Path(db_path).with_suffix(".jpg"),
        bot_id=BOT_ID,
    )


def _task_assigned_event(task_id: int = 1) -> OfficeEvent:
    return OfficeEvent(
        event_type="task.assigned",
        source_bot_id=SOURCE_BOT_ID,
        payload=TaskAssignedEvent(
            task_id=task_id, title="Позвонить клиенту", description="Обсудить контракт",
            deadline="2026-08-20T10:00:00", assignee_hint="Иван", boss_chat_id=555,
        ),
    )


def _incoming_tasks(db_path: str) -> list[tuple]:
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT source_task_id, source_bot_id, title, description, deadline, "
        "assignee_hint, status, boss_chat_id FROM incoming_tasks"
    ).fetchall()
    conn.close()
    return rows


@pytest.mark.asyncio
async def test_on_office_event_creates_incoming_task_row(db_path):
    await manager_bot.init_db(db_path)
    config = _config(db_path)

    with patch("features.office_events._registry_handle") as mock_handle:
        mock_handle.value = None  # no live registry — DB write must still happen
        await manager_bot.on_office_event(_task_assigned_event(task_id=99), config)

    rows = _incoming_tasks(db_path)
    assert len(rows) == 1
    assert rows[0][0] == 99  # source_task_id
    assert rows[0][1] == SOURCE_BOT_ID
    assert rows[0][2] == "Позвонить клиенту"
    assert rows[0][3] == "Обсудить контракт"
    assert rows[0][4] == "2026-08-20T10:00:00"
    assert rows[0][5] == "Иван"
    assert rows[0][6] == "new"
    assert rows[0][7] == 555


@pytest.mark.asyncio
async def test_on_office_event_ignores_non_task_assigned_events(db_path):
    await manager_bot.init_db(db_path)
    config = _config(db_path)
    event = OfficeEvent(event_type="order.created", source_bot_id=SOURCE_BOT_ID, payload=None)

    await manager_bot.on_office_event(event, config)

    assert _incoming_tasks(db_path) == []


@pytest.mark.asyncio
async def test_on_office_event_never_raises_on_db_error(db_path):
    # db_path never initialized (no incoming_tasks table) — must be swallowed
    # and logged, not propagated, same isolation guarantee
    # features/office_events.py's publish_event() already provides one layer
    # up, kept defensive here too per event_rsvp.py's own precedent.
    config = _config(db_path)
    await manager_bot.on_office_event(_task_assigned_event(), config)


@pytest.mark.asyncio
async def test_notify_managers_pushes_live_message_when_registry_available(db_path):
    await manager_bot.init_db(db_path)
    config = _config(db_path)
    manager_bot._save_managers(manager_bot._managers_file(config), {"555"})

    fake_bot = MagicMock()
    fake_bot.send_message = AsyncMock()
    fake_entry = MagicMock()
    fake_entry.bot = fake_bot
    fake_registry = MagicMock()
    fake_registry.get = MagicMock(return_value=fake_entry)

    with patch("features.office_events._registry_handle") as mock_handle:
        mock_handle.value = fake_registry
        await manager_bot.on_office_event(_task_assigned_event(task_id=1), config)

    fake_bot.send_message.assert_awaited_once()
    args, kwargs = fake_bot.send_message.call_args
    assert args[0] == 555
    assert "Позвонить клиенту" in args[1]


@pytest.mark.asyncio
async def test_notify_managers_noop_when_no_registry(db_path):
    await manager_bot.init_db(db_path)
    config = _config(db_path)
    manager_bot._save_managers(manager_bot._managers_file(config), {"555"})

    with patch("features.office_events._registry_handle") as mock_handle:
        mock_handle.value = None
        # Should not raise even though there's no live registry to push through.
        await manager_bot.on_office_event(_task_assigned_event(task_id=2), config)


@pytest.mark.asyncio
async def test_publish_completion_sends_task_completed_event(db_path):
    """_publish_completion (called from cb_done) must publish task.completed
    back to the office_events bus with the ORIGINATING boss_bot task_id
    (source_task_id), so boss_bot's on_office_event can match it against its
    own tasks table — see templates/boss_bot.py's on_office_event."""
    await manager_bot.init_db(db_path)
    config = _config(db_path)

    row = {"source_task_id": 9, "id": 1}

    with patch("features.office_events.publish_event", new_callable=AsyncMock) as mock_publish:
        await manager_bot._publish_completion(config, row)

    mock_publish.assert_awaited_once()
    args, _kwargs = mock_publish.call_args
    assert args[0] == BOT_ID
    assert args[1] == "task.completed"
    assert args[2].task_id == 9


@pytest.mark.asyncio
async def test_publish_completion_noop_without_source_task_id(db_path):
    """A row with no source_task_id (e.g. hand-created outside the office
    link) must never publish — there'd be nothing on boss_bot's side to
    match against."""
    config = _config(db_path)
    row = {"source_task_id": None, "id": 1}

    with patch("features.office_events.publish_event", new_callable=AsyncMock) as mock_publish:
        await manager_bot._publish_completion(config, row)

    mock_publish.assert_not_awaited()
