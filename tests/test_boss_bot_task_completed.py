"""templates/boss_bot.py's on_office_event() — the task.completed back-sync
subscriber that clears tasks.status so features/reminders.py's overdue
sweep stops firing for a task a manager already finished.

Run with: python -m pytest tests/test_boss_bot_task_completed.py
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import aiosqlite
import pytest

from features.office_events import OfficeEvent, TaskCompletedEvent
from templates import boss_bot

SOURCE_BOT_ID = 42
BOT_ID = 7


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


async def _insert_task(db_path: str, title: str, status: str = "open") -> int:
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "INSERT INTO tasks (title, description, deadline, assignee_hint, status, boss_chat_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (title, "desc", "2026-08-20T10:00:00", "Иван", status, 555),
        )
        await db.commit()
        return cur.lastrowid


async def _task_status(db_path: str, task_id: int) -> str:
    async with aiosqlite.connect(db_path) as db:
        row = await (await db.execute("SELECT status FROM tasks WHERE id=?", (task_id,))).fetchone()
    return row[0]


@pytest.mark.asyncio
async def test_on_office_event_marks_task_done(db_path):
    await boss_bot.init_db(db_path)
    task_id = await _insert_task(db_path, "Позвонить клиенту")
    config = _config(db_path)

    event = OfficeEvent(
        event_type="task.completed", source_bot_id=SOURCE_BOT_ID,
        payload=TaskCompletedEvent(task_id=task_id, completed_by="Иван"),
    )
    await boss_bot.on_office_event(event, config)

    assert await _task_status(db_path, task_id) == "done"


@pytest.mark.asyncio
async def test_on_office_event_ignores_other_event_types(db_path):
    await boss_bot.init_db(db_path)
    task_id = await _insert_task(db_path, "Позвонить клиенту")
    config = _config(db_path)

    event = OfficeEvent(event_type="task.assigned", source_bot_id=SOURCE_BOT_ID, payload=None)
    await boss_bot.on_office_event(event, config)

    assert await _task_status(db_path, task_id) == "open"


@pytest.mark.asyncio
async def test_on_office_event_never_raises_on_db_error(db_path):
    # db_path never initialized (no tasks table) — must be swallowed and
    # logged, not propagated, same isolation guarantee every other
    # on_office_event subscriber in this codebase provides.
    config = _config(db_path)
    event = OfficeEvent(
        event_type="task.completed", source_bot_id=SOURCE_BOT_ID,
        payload=TaskCompletedEvent(task_id=1, completed_by="Иван"),
    )
    await boss_bot.on_office_event(event, config)
