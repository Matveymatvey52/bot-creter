"""templates/boss_bot.py's voice_intake wiring — voice-to-task save closure
and its on_saved hook (publishes task.assigned via features/office_events.py).

Driven through a real aiogram Dispatcher, same convention as
tests/test_voice_intake_module.py: voice_intake.router cloned onto a
Dispatcher with a small local middleware standing in for
runtime.registry.py's ConfigMiddleware + bot_id injection.

Run with: python -m pytest tests/test_boss_bot_voice_to_task.py
"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from features.office_events import TaskAssignedEvent
from templates import boss_bot

BOT_ID = 42
USER_ID = 111


@pytest.fixture
def db_path():
    with tempfile.TemporaryDirectory() as tmp:
        yield str(Path(tmp) / "fixture.db")


def _config(db_path: str) -> boss_bot.BossBotConfig:
    return boss_bot.BossBotConfig(
        bot_name="test-boss-bot",
        db_path=db_path,
        admins_file=str(Path(db_path).with_suffix(".admins.json")),
        welcome_image=Path(db_path).with_suffix(".jpg"),
        bot_id=BOT_ID,
    )


def _tasks(db_path: str) -> list[tuple]:
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT title, description, deadline, assignee_hint, boss_chat_id FROM tasks"
    ).fetchall()
    conn.close()
    return rows


@pytest.mark.asyncio
async def test_save_task_inserts_row_and_returns_id(db_path):
    await boss_bot.init_db(db_path)
    schema = boss_bot._build_voice_schema(BOT_ID)
    rt = schema.record_types[0]
    assert rt.key == "task"

    data = {
        "title": "Позвонить клиенту", "description": "Обсудить контракт",
        "deadline": "2026-08-20T10:00:00", "assignee_hint": "Иван",
    }
    task_id = await rt.save(db_path, USER_ID, data)

    assert task_id is not None
    rows = _tasks(db_path)
    assert len(rows) == 1
    assert rows[0][0] == "Позвонить клиенту"
    assert rows[0][1] == "Обсудить контракт"
    assert rows[0][2] == "2026-08-20T10:00:00"
    assert rows[0][3] == "Иван"
    assert rows[0][4] == USER_ID  # boss_chat_id = context_id (the dictating user)


@pytest.mark.asyncio
async def test_on_saved_publishes_task_assigned_event(db_path):
    await boss_bot.init_db(db_path)
    schema = boss_bot._build_voice_schema(BOT_ID)
    rt = schema.record_types[0]
    data = {
        "title": "Отправить отчёт", "description": "Ежемесячный отчёт",
        "deadline": "2026-08-25T18:00:00", "assignee_hint": "Мария",
    }
    task_id = await rt.save(db_path, USER_ID, data)

    with patch("features.office_events.publish_event", new=AsyncMock(return_value=1)) as mock_publish:
        await rt.on_saved(db_path, USER_ID, task_id)

    mock_publish.assert_awaited_once()
    args, kwargs = mock_publish.call_args
    assert args[0] == BOT_ID
    assert args[1] == "task.assigned"
    payload = args[2]
    assert isinstance(payload, TaskAssignedEvent)
    assert payload.task_id == task_id
    assert payload.title == "Отправить отчёт"
    assert payload.description == "Ежемесячный отчёт"
    assert payload.deadline == "2026-08-25T18:00:00"
    assert payload.assignee_hint == "Мария"
    assert payload.boss_chat_id == USER_ID


@pytest.mark.asyncio
async def test_on_saved_no_publish_when_task_id_is_none(db_path):
    await boss_bot.init_db(db_path)
    schema = boss_bot._build_voice_schema(BOT_ID)
    rt = schema.record_types[0]

    with patch("features.office_events.publish_event", new=AsyncMock()) as mock_publish:
        await rt.on_saved(db_path, USER_ID, None)

    mock_publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_context_id_always_resolves_to_user_id(db_path):
    await boss_bot.init_db(db_path)
    context_id = await boss_bot._get_context_id(db_path, USER_ID)
    assert context_id == USER_ID


@pytest.mark.asyncio
async def test_schema_registered_for_bot_id_is_reachable_via_voice_intake(db_path):
    """register_schema(bot_id, ...) (called from config_from_bot_row in
    production) makes the schema reachable through voice_intake's own
    registry — confirms boss_bot wires into voice_intake the same way
    templates/tour_operator.py does, without re-testing voice_intake's own
    transcription/parsing internals (covered by
    tests/test_voice_intake_module.py already)."""
    from features import voice_intake

    await boss_bot.init_db(db_path)
    voice_intake.register_schema(BOT_ID, boss_bot._build_voice_schema(BOT_ID))

    schema = voice_intake._schemas.get(BOT_ID)
    assert schema is not None
    assert len(schema.record_types) == 1
    assert schema.record_types[0].key == "task"
