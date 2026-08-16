"""features/reminders.py tests — see docs/REMINDERS_DESIGN.md.

Covers both recipient shapes (recipient_field: car_rental-style same-row
owner; recipient_query: event_rsvp-style join), dedup via reminder_log,
the due-window match, active_field filtering, and per-row send-failure
isolation (one bad chat_id must not abort the sweep).

No real Telegram network calls (Bot.__call__ mocked), no real tokens —
same conventions as tests/test_payments_module.py.

Run with: python -m unittest tests.test_reminders_module
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
from aiogram import Bot

from features import reminders

FAKE_TOKEN = "123456:test-token-not-real"


async def _init_car_rental_fixture(db_path: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE rental_bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_user_id INTEGER NOT NULL,
                start_date TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'confirmed'
            )
        """)
        await db.commit()


async def _init_event_rsvp_fixture(db_path: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE event_details (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                title TEXT,
                event_date TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE rsvps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_user_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'confirmed'
            )
        """)
        await db.commit()


CAR_RENTAL_RULE = {
    "rules": [
        {
            "id": "car_rental_upcoming",
            "table": "rental_bookings",
            "date_field": "start_date",
            "date_format": "%Y-%m-%d %H:%M",
            "recipient_field": "client_user_id",
            "active_field": "status IN ('pending','confirmed')",
            "offsets_hours": [24],
            "message_template": "Аренда начинается {start_date:%d.%m.%Y}",
        },
    ],
}

EVENT_RSVP_RULE = {
    "rules": [
        {
            "id": "event_rsvp_upcoming",
            "table": "event_details",
            "date_field": "event_date",
            "date_format": "%Y-%m-%d %H:%M",
            "recipient_query": "SELECT client_user_id AS chat_id FROM rsvps WHERE status = 'confirmed'",
            "offsets_hours": [24],
            "message_template": "«{title}» начнётся {event_date:%d.%m %H:%M}",
        },
    ],
}


class ReminderRuleValidationTests(unittest.TestCase):
    def test_rejects_neither_recipient_field_nor_query(self):
        with self.assertRaises(ValueError):
            reminders.ReminderRule(
                id="bad", table="t", date_field="d", date_format="%Y-%m-%d",
                offsets_hours=[1], message_template="x",
            )

    def test_rejects_both_recipient_field_and_query(self):
        with self.assertRaises(ValueError):
            reminders.ReminderRule(
                id="bad", table="t", date_field="d", date_format="%Y-%m-%d",
                offsets_hours=[1], message_template="x",
                recipient_field="a", recipient_query="SELECT 1",
            )


class RecipientFieldSweepTests(unittest.IsolatedAsyncioTestCase):
    """car_rental-shaped: recipient on the same row as the date."""

    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        self.bot = Bot(token=FAKE_TOKEN)
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "fixture.db")
        await _init_car_rental_fixture(self.db_path)
        await reminders.init_db(self.db_path)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def _insert_booking(self, *, client_user_id, start_date, status="confirmed"):
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "INSERT INTO rental_bookings (client_user_id, start_date, status) VALUES (?, ?, ?)",
                (client_user_id, start_date, status),
            )
            await db.commit()
            return cur.lastrowid

    async def test_due_row_gets_reminded(self):
        now = datetime(2026, 8, 16, 10, 0)
        await self._insert_booking(client_user_id=555, start_date="2026-08-17 10:00")

        sent = await reminders.run_reminders_sweep_for_bot(self.bot, self.db_path, CAR_RENTAL_RULE, now=now)

        self.assertEqual(sent, 1)
        self.bot.__call__.assert_awaited_once()
        request = self.bot.__call__.call_args[0][0]
        self.assertEqual(request.chat_id, 555)
        self.assertIn("17.08.2026", request.text)

    async def test_not_yet_due_row_is_skipped(self):
        now = datetime(2026, 8, 16, 10, 0)
        await self._insert_booking(client_user_id=555, start_date="2026-08-25 10:00")  # far in the future

        sent = await reminders.run_reminders_sweep_for_bot(self.bot, self.db_path, CAR_RENTAL_RULE, now=now)

        self.assertEqual(sent, 0)
        self.bot.__call__.assert_not_awaited()

    async def test_second_sweep_does_not_redeliver(self):
        now = datetime(2026, 8, 16, 10, 0)
        await self._insert_booking(client_user_id=555, start_date="2026-08-17 10:00")

        first = await reminders.run_reminders_sweep_for_bot(self.bot, self.db_path, CAR_RENTAL_RULE, now=now)
        second = await reminders.run_reminders_sweep_for_bot(self.bot, self.db_path, CAR_RENTAL_RULE, now=now)

        self.assertEqual(first, 1)
        self.assertEqual(second, 0, "reminder_log dedup failed — same (rule,row,offset) fired twice")

    async def test_cancelled_booking_excluded_by_active_field(self):
        now = datetime(2026, 8, 16, 10, 0)
        await self._insert_booking(client_user_id=555, start_date="2026-08-17 10:00", status="cancelled")

        sent = await reminders.run_reminders_sweep_for_bot(self.bot, self.db_path, CAR_RENTAL_RULE, now=now)

        self.assertEqual(sent, 0)

    async def test_one_failed_send_does_not_block_others(self):
        now = datetime(2026, 8, 16, 10, 0)
        await self._insert_booking(client_user_id=111, start_date="2026-08-17 10:00")
        await self._insert_booking(client_user_id=222, start_date="2026-08-17 10:00")

        async def _send_side_effect(request, **kwargs):
            if getattr(request, "chat_id", None) == 111:
                raise RuntimeError("bot was blocked by this user")
            return MagicMock()

        with patch.object(Bot, "__call__", new=AsyncMock(side_effect=_send_side_effect)):
            sent = await reminders.run_reminders_sweep_for_bot(self.bot, self.db_path, CAR_RENTAL_RULE, now=now)

        self.assertEqual(sent, 1, "one blocked recipient must not prevent the other from being reminded")

    async def test_no_reminders_config_is_a_noop(self):
        now = datetime(2026, 8, 16, 10, 0)
        await self._insert_booking(client_user_id=555, start_date="2026-08-17 10:00")

        sent = await reminders.run_reminders_sweep_for_bot(self.bot, self.db_path, None, now=now)

        self.assertEqual(sent, 0)
        self.bot.__call__.assert_not_awaited()


class RecipientQuerySweepTests(unittest.IsolatedAsyncioTestCase):
    """event_rsvp-shaped: recipients come from a separate table (join), the
    date-bearing row (event_details) has no owner column of its own."""

    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        self.bot = Bot(token=FAKE_TOKEN)
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "fixture.db")
        await _init_event_rsvp_fixture(self.db_path)
        await reminders.init_db(self.db_path)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def _set_event(self, *, title, event_date):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO event_details (id, title, event_date) VALUES (1, ?, ?)", (title, event_date),
            )
            await db.commit()

    async def _add_rsvp(self, *, client_user_id, status="confirmed"):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO rsvps (client_user_id, status) VALUES (?, ?)", (client_user_id, status),
            )
            await db.commit()

    async def test_all_confirmed_rsvps_get_reminded(self):
        now = datetime(2026, 8, 16, 10, 0)
        await self._set_event(title="Концерт", event_date="2026-08-17 10:00")
        await self._add_rsvp(client_user_id=1)
        await self._add_rsvp(client_user_id=2)
        await self._add_rsvp(client_user_id=3, status="waitlist")  # not confirmed — excluded

        sent = await reminders.run_reminders_sweep_for_bot(self.bot, self.db_path, EVENT_RSVP_RULE, now=now)

        self.assertEqual(sent, 2)
        recipients = {call.args[0].chat_id for call in self.bot.__call__.await_args_list}
        self.assertEqual(recipients, {1, 2})

    async def test_event_with_no_confirmed_rsvps_is_marked_sent_without_error(self):
        now = datetime(2026, 8, 16, 10, 0)
        await self._set_event(title="Пустое событие", event_date="2026-08-17 10:00")

        sent = await reminders.run_reminders_sweep_for_bot(self.bot, self.db_path, EVENT_RSVP_RULE, now=now)
        self.assertEqual(sent, 0)

        # Second sweep must not re-attempt this row forever.
        sent_again = await reminders.run_reminders_sweep_for_bot(self.bot, self.db_path, EVENT_RSVP_RULE, now=now)
        self.assertEqual(sent_again, 0)
        conn = sqlite3.connect(self.db_path)
        logged = conn.execute("SELECT COUNT(*) FROM reminder_log").fetchone()[0]
        conn.close()
        self.assertEqual(logged, 1, "a zero-recipient due row should still be recorded once, not retried forever")

    async def test_unparseable_event_date_is_skipped_not_fatal(self):
        now = datetime(2026, 8, 16, 10, 0)
        await self._set_event(title="Скоро", event_date="в следующую пятницу")  # free-form, per event_rsvp.py
        await self._add_rsvp(client_user_id=1)

        sent = await reminders.run_reminders_sweep_for_bot(self.bot, self.db_path, EVENT_RSVP_RULE, now=now)

        self.assertEqual(sent, 0)
        self.bot.__call__.assert_not_awaited()


class InitDbTests(unittest.IsolatedAsyncioTestCase):
    async def test_creates_reminder_log_table_with_wal(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "fixture.db")
            await reminders.init_db(db_path)

            conn = sqlite3.connect(db_path)
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            conn.close()

            self.assertEqual(mode.lower(), "wal")
            self.assertIn("reminder_log", tables)


if __name__ == "__main__":
    unittest.main()
