"""registry-init-db phase — proves the actual blocker this phase closes: a bot
registered purely through the registry (never run as a standalone subprocess,
no init_db() called by the test itself in asyncSetUp — unlike the template
isolation tests) ends up with working tables and can actually write data
through its real Dispatcher.

Deliberately drives the real Registry.add_or_replace()/reload_all() path (not
config_from_bot_row()+init_db() called directly, which is what the isolation
tests do) — that IS the code path this phase changes.

runtime/registry.py's _build_*_middleware functions hardcode
`from config import DATA_DIR` (the project's canonical data dir, not
overridable via a parameter, unlike config_from_bot_row's own `data_dir` arg)
— so every test here patches config.DATA_DIR to a temp directory, to avoid
writing real bot data into the project's real data directory.

No real Telegram network calls (Bot.__call__ mocked), no real tokens.

Run with: python -m unittest tests.test_registry_init_db
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram import Bot

from db.database import create_bot_record_with_admins, delete_bot
from runtime.registry import Registry
from templates import booking_beauty

FAKE_TOKEN = "123456:test-token-not-real"
_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
ACCOUNTANT_FILE_PATH = str(_TEMPLATES_DIR / "accountant.py")
BOOKING_BEAUTY_FILE_PATH = str(_TEMPLATES_DIR / "booking_beauty.py")


def _text_update(update_id: int, user_id: int, text: str) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 1700000000,
            "chat": {"id": user_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
            "text": text,
        },
    }


def _callback_update(update_id: int, user_id: int, data: str) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": str(update_id),
            "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
            "message": {
                "message_id": update_id,
                "date": 1700000000,
                "chat": {"id": user_id, "type": "private"},
                "text": "placeholder",
            },
            "chat_instance": "1",
            "data": data,
        },
    }


class RegistryInitDbTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self._data_dir_patcher = patch("config.DATA_DIR", self.data_dir)
        self._data_dir_patcher.start()

    async def asyncTearDown(self):
        self._data_dir_patcher.stop()
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_bot_registered_only_through_registry_has_working_tables(self):
        """The main case this phase closes: no init_db() call anywhere in this
        test — only Registry.add_or_replace(), exactly what the webhook
        runtime itself does. If registry-triggered init_db were missing, this
        would fail with 'no such table: projects'."""
        registry = Registry()
        bot_row = {
            "id": 5001, "name": "registry_only_bot", "token": FAKE_TOKEN,
            "file_path": ACCOUNTANT_FILE_PATH, "display_name": None, "group_chat_id": None,
        }
        entry = await registry.add_or_replace(bot_row)
        self.assertIsNotNone(entry)

        await entry.dispatcher.feed_webhook_update(entry.bot, _callback_update(1, 111, "proj_new"))
        await entry.dispatcher.feed_webhook_update(entry.bot, _text_update(2, 111, "Registry-born Project"))

        db_path = str(self.data_dir / "bot_5001_data.db")
        conn = sqlite3.connect(db_path)
        names = [r[0] for r in conn.execute("SELECT name FROM projects").fetchall()]
        conn.close()
        self.assertEqual(names, ["Registry-born Project"])

    async def test_repeated_registration_no_duplicates_booking_beauty_slot_stays_booked(self):
        """Repeated registration (simulating repeated admin_reload_one/
        reload_all calls) must not duplicate booking_beauty's generated
        slots, and must not disturb an already-booked slot."""
        registry = Registry()
        bot_row = {
            "id": 5002, "name": "registry_bb_bot", "token": FAKE_TOKEN,
            "file_path": BOOKING_BEAUTY_FILE_PATH, "display_name": None, "group_chat_id": None,
        }
        entry = await registry.add_or_replace(bot_row)
        self.assertIsNotNone(entry)

        db_path = str(self.data_dir / "bot_5002_data.db")
        conn = sqlite3.connect(db_path)
        count_before = conn.execute("SELECT COUNT(*) FROM slots").fetchone()[0]
        conn.close()
        self.assertGreater(count_before, 0)

        slot_date = date.today().isoformat()
        slot_time = booking_beauty.SLOT_TIMES[0]
        master = booking_beauty.MASTERS[0]
        bot, dp = entry.bot, entry.dispatcher
        uid = 1
        await dp.feed_webhook_update(bot, _callback_update(uid, 111, f"book_svc:{booking_beauty.SERVICES[0]}")); uid += 1
        await dp.feed_webhook_update(bot, _callback_update(uid, 111, f"book_day:{slot_date}")); uid += 1
        await dp.feed_webhook_update(bot, _callback_update(uid, 111, f"book_time:{slot_date}:{slot_time}")); uid += 1

        conn = sqlite3.connect(db_path)
        slot_id = conn.execute(
            "SELECT id FROM slots WHERE slot_date=? AND slot_time=? AND master=? AND status='active'",
            (slot_date, slot_time, master),
        ).fetchone()[0]
        conn.close()

        await dp.feed_webhook_update(bot, _callback_update(uid, 111, f"book_slot:{slot_id}")); uid += 1
        await dp.feed_webhook_update(bot, _text_update(uid, 111, "Registry Client")); uid += 1
        await dp.feed_webhook_update(bot, _text_update(uid, 111, "+7 999 000-00-00")); uid += 1
        await dp.feed_webhook_update(bot, _callback_update(uid, 111, f"book_confirm:{slot_id}")); uid += 1

        # Re-register the SAME bot twice more — simulates repeated
        # admin_reload_one()/reload_all() calls hitting init_db() again.
        await registry.add_or_replace(bot_row)
        await registry.add_or_replace(bot_row)

        conn = sqlite3.connect(db_path)
        count_after = conn.execute("SELECT COUNT(*) FROM slots").fetchone()[0]
        booking_count = conn.execute("SELECT COUNT(*) FROM bookings").fetchone()[0]
        booked_status = conn.execute("SELECT status FROM slots WHERE id=?", (slot_id,)).fetchone()[0]
        conn.close()

        self.assertEqual(count_after, count_before, "repeated registration duplicated slot rows")
        self.assertEqual(booking_count, 1, "repeated registration lost the booking")
        self.assertEqual(booked_status, "booked", "repeated registration reset a booked slot back to active")

    async def test_init_db_failure_keeps_bot_out_of_registry_and_is_logged(self):
        registry = Registry()
        bot_row = {
            "id": 5003, "name": "failing_bot", "token": FAKE_TOKEN,
            "file_path": ACCOUNTANT_FILE_PATH, "display_name": None, "group_chat_id": None,
        }
        with patch("templates.accountant.init_db", new=AsyncMock(side_effect=RuntimeError("disk full"))):
            with self.assertLogs("runtime.registry", level="ERROR") as log_ctx:
                entry = await registry.add_or_replace(bot_row)

        self.assertIsNone(entry)
        self.assertIsNone(registry.get(5003))
        self.assertTrue(
            any("5003" in msg and "accountant" in msg for msg in log_ctx.output),
            f"expected a log line naming bot_id=5003 and template=accountant, got: {log_ctx.output}",
        )

    async def test_reload_all_isolates_one_bots_init_db_failure_from_others(self):
        """One bot's init_db() failing during reload_all() must not prevent
        the other, healthy bot from loading — same per-bot error isolation
        reload_all() already had before this phase, now also covering
        init_db() failures specifically."""
        good_id = await create_bot_record_with_admins(
            name="reload_all_good_bot", description="test", token=FAKE_TOKEN,
            file_path=ACCOUNTANT_FILE_PATH, admin_ids=["111"],
        )
        bad_id = await create_bot_record_with_admins(
            name="reload_all_bad_bot", description="test", token=FAKE_TOKEN,
            file_path=BOOKING_BEAUTY_FILE_PATH, admin_ids=["111"],
        )
        try:
            registry = Registry()
            with patch("templates.booking_beauty.init_db", new=AsyncMock(side_effect=RuntimeError("boom"))):
                with self.assertLogs("runtime.registry", level="ERROR") as log_ctx:
                    await registry.reload_all()

            self.assertIsNotNone(registry.get(good_id))
            self.assertIsNone(registry.get(bad_id))
            self.assertTrue(
                any(str(bad_id) in msg and "booking_beauty" in msg for msg in log_ctx.output),
                f"expected a log line naming bot_id={bad_id} and template=booking_beauty, got: {log_ctx.output}",
            )
        finally:
            await delete_bot(good_id)
            await delete_bot(bad_id)


if __name__ == "__main__":
    unittest.main()
