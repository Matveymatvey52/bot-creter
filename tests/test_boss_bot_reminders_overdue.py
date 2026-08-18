"""templates/boss_bot.py's reminders_config — run_reminders_sweep_for_bot
against the real `tasks` table/rule shape, verifying an overdue (deadline
already passed by ~1h) task is picked up and a status='done' task is not.

No real Telegram network calls (Bot.send_message mocked), same convention as
tests/test_reminders_module.py.

Run with: python -m pytest tests/test_boss_bot_reminders_overdue.py
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from features import reminders
from templates import boss_bot

BOSS_CHAT_ID = 555


@pytest.fixture
def db_path():
    with tempfile.TemporaryDirectory() as tmp:
        yield str(Path(tmp) / "fixture.db")


async def _insert_task(
    db_path: str, title: str, deadline: str, status: str = "open", boss_chat_id: int = BOSS_CHAT_ID,
) -> int:
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "INSERT INTO tasks (title, description, deadline, assignee_hint, status, boss_chat_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (title, "desc", deadline, "Иван", status, boss_chat_id),
        )
        await db.commit()
        return cur.lastrowid


@pytest.mark.asyncio
async def test_sweep_sends_reminder_for_overdue_task(db_path):
    await boss_bot.init_db(db_path)
    now = datetime(2026, 8, 20, 12, 0, 0)
    # offsets_hours=[-1] means the sweep looks for deadlines whose
    # (deadline + (-1h)) instant is "now" — i.e. a deadline ~1h in the past.
    overdue_deadline = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
    await _insert_task(db_path, "Просроченная задача", overdue_deadline, status="open")

    fake_bot = MagicMock()
    fake_bot.send_message = AsyncMock()

    sent = await reminders.run_reminders_sweep_for_bot(
        fake_bot, db_path, boss_bot.reminders_config, now=now,
    )

    assert sent == 1
    fake_bot.send_message.assert_awaited_once()
    args, kwargs = fake_bot.send_message.call_args
    assert args[0] == BOSS_CHAT_ID
    assert "Просроченная задача" in args[1]


@pytest.mark.asyncio
async def test_sweep_skips_done_task(db_path):
    await boss_bot.init_db(db_path)
    now = datetime(2026, 8, 20, 12, 0, 0)
    overdue_deadline = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
    await _insert_task(db_path, "Готовая задача", overdue_deadline, status="done")

    fake_bot = MagicMock()
    fake_bot.send_message = AsyncMock()

    sent = await reminders.run_reminders_sweep_for_bot(
        fake_bot, db_path, boss_bot.reminders_config, now=now,
    )

    assert sent == 0
    fake_bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_sweep_skips_task_not_yet_overdue(db_path):
    await boss_bot.init_db(db_path)
    now = datetime(2026, 8, 20, 12, 0, 0)
    # Deadline still hours in the future — not due under offset -1 yet.
    future_deadline = (now + timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%S")
    await _insert_task(db_path, "Будущая задача", future_deadline, status="open")

    fake_bot = MagicMock()
    fake_bot.send_message = AsyncMock()

    sent = await reminders.run_reminders_sweep_for_bot(
        fake_bot, db_path, boss_bot.reminders_config, now=now,
    )

    assert sent == 0
    fake_bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_sweep_does_not_resend_same_task(db_path):
    await boss_bot.init_db(db_path)
    now = datetime(2026, 8, 20, 12, 0, 0)
    overdue_deadline = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
    await _insert_task(db_path, "Повтор", overdue_deadline, status="open")

    fake_bot = MagicMock()
    fake_bot.send_message = AsyncMock()

    first = await reminders.run_reminders_sweep_for_bot(fake_bot, db_path, boss_bot.reminders_config, now=now)
    second = await reminders.run_reminders_sweep_for_bot(fake_bot, db_path, boss_bot.reminders_config, now=now)

    assert first == 1
    assert second == 0
