"""expense_tracker template — minimal smoke coverage.

Two smoke tests only, per task scope:
1. the full add-expense FSM flow (amount -> category -> date -> comment)
   actually creates a row in an isolated tmp-dir DB;
2. the period summary aggregation sums correctly PER CATEGORY (not just a
   flat total) across several expenses/categories/dates.

No real Telegram network calls, no real tokens, no real data/bots.db —
everything runs against a tempfile.TemporaryDirectory() DB, same isolation
convention as tests/test_vehicle_service_isolation.py.

Run with: python -m unittest tests.test_expense_tracker_smoke
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

import db.database as db_module
from runtime.registry import get_template_router
from templates import expense_tracker

FAKE_TOKEN = "123456:test-token-not-real"
ADMIN_ID = 999


def _text_update(update_id: int, user_id: int, text: str) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id, "date": 1700000000,
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
                "message_id": update_id, "date": 1700000000,
                "chat": {"id": user_id, "type": "private"}, "text": "placeholder",
            },
            "chat_instance": "1", "data": data,
        },
    }


def _build_bot_dispatcher(config: expense_tracker.ExpenseTrackerConfig) -> tuple[Bot, Dispatcher]:
    bot = Bot(token=FAKE_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(expense_tracker.ConfigMiddleware(config))
    dp.include_router(get_template_router("expense_tracker"))
    return bot, dp


class ExpenseTrackerAddFlowSmokeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = expense_tracker.config_from_bot_row(
            {"bot_id": 801, "name": "expense_smoke_bot", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await expense_tracker.init_db(self.config.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)
        await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/start"))

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_full_add_expense_flow_creates_row(self):
        async with aiosqlite.connect(self.config.db_path) as db:
            category_id = (await (await db.execute(
                "SELECT id FROM categories WHERE name='Еда'"
            )).fetchone())[0]

        uid = 10
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, ADMIN_ID, "exp_new")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _text_update(uid, ADMIN_ID, "543.50")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, ADMIN_ID, f"exp_cat:{category_id}")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, ADMIN_ID, "exp_date_today")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _text_update(uid, ADMIN_ID, "Обед")); uid += 1

        conn = sqlite3.connect(self.config.db_path)
        row = conn.execute(
            "SELECT amount, category_id, comment, created_by FROM expenses"
        ).fetchone()
        conn.close()

        self.assertIsNotNone(row, "add-expense flow did not create an expenses row")
        self.assertEqual(row, (543.5, category_id, "Обед", ADMIN_ID))


class ExpenseTrackerSummarySmokeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = expense_tracker.config_from_bot_row(
            {"bot_id": 802, "name": "expense_summary_bot", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await expense_tracker.init_db(self.config.db_path)

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def test_summary_sums_correctly_per_category_and_total(self):
        async with aiosqlite.connect(self.config.db_path) as db:
            food_id = (await (await db.execute("SELECT id FROM categories WHERE name='Еда'")).fetchone())[0]
            transport_id = (await (await db.execute("SELECT id FROM categories WHERE name='Транспорт'")).fetchone())[0]
            # Two expenses in "Еда" (100 + 50 = 150), one in "Транспорт" (30),
            # all inside the queried window, plus one OUTSIDE the window that
            # must NOT be counted.
            await db.execute(
                "INSERT INTO expenses (category_id, amount, expense_date, created_by) VALUES (?,?,?,?)",
                (food_id, 100.0, "2026-08-05", ADMIN_ID),
            )
            await db.execute(
                "INSERT INTO expenses (category_id, amount, expense_date, created_by) VALUES (?,?,?,?)",
                (food_id, 50.0, "2026-08-06", ADMIN_ID),
            )
            await db.execute(
                "INSERT INTO expenses (category_id, amount, expense_date, created_by) VALUES (?,?,?,?)",
                (transport_id, 30.0, "2026-08-07", ADMIN_ID),
            )
            await db.execute(
                "INSERT INTO expenses (category_id, amount, expense_date, created_by) VALUES (?,?,?,?)",
                (food_id, 999.0, "2026-07-01", ADMIN_ID),  # outside the window
            )
            await db.commit()

        text = await expense_tracker._summary_text(self.config.db_path, "2026-08-01", "2026-08-31", "август")

        self.assertIn("Еда", text)
        self.assertIn("150.00", text, "Еда total should sum 100+50=150, not include the out-of-window 999")
        self.assertIn("Транспорт", text)
        self.assertIn("30.00", text)
        self.assertIn("180.00", text, "grand total should be 150+30=180")
        self.assertNotIn("999.00", text, "expense outside the queried period leaked into the summary")
        # Highest-spend category ("Еда") must appear before the smaller one.
        self.assertLess(text.index("Еда"), text.index("Транспорт"))


class ExpenseTrackerAdminBootstrapSecurityTests(unittest.IsolatedAsyncioTestCase):
    """Security fix: previously, whoever sent /start FIRST permanently became
    the bot admin — a client testing the bot link before the owner did would
    silently seize the admin panel. See tests/test_shop_catalog_isolation.py
    for the original of this fix, applied identically here."""

    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

        self._central_db_path = self.data_dir / "central_bots.db"
        self._db_path_patcher = patch.object(db_module, "DB_PATH", self._central_db_path)
        self._db_path_patcher.start()
        await db_module.init_db()

    async def asyncTearDown(self):
        self._db_path_patcher.stop()
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_non_owner_messaging_first_does_not_become_admin(self):
        config = expense_tracker.config_from_bot_row(
            {"bot_id": 929, "name": "expense_bot_owned", "display_name": None,
             "group_chat_id": None, "owner_telegram_id": 12345},
            self.data_dir,
        )
        await expense_tracker.init_db(config.db_path)
        bot, dp = _build_bot_dispatcher(config)

        CLIENT_ID = 555  # not the owner, messages first
        await dp.feed_webhook_update(bot, _text_update(1, CLIENT_ID, "/start"))
        self.assertEqual(expense_tracker._load_admins(config.admins_file), set())
        self.assertFalse(expense_tracker._is_admin(CLIENT_ID, config))

        await dp.feed_webhook_update(bot, _text_update(2, 12345, "/start"))
        self.assertTrue(expense_tracker._is_admin(12345, config))
        self.assertEqual(expense_tracker._load_admins(config.admins_file), {"12345"})

    async def test_owner_is_always_admin_even_with_stale_admins_file(self):
        config = expense_tracker.config_from_bot_row(
            {"bot_id": 930, "name": "expense_bot_owned_2", "display_name": None,
             "group_chat_id": None, "owner_telegram_id": 777},
            self.data_dir,
        )
        await expense_tracker.init_db(config.db_path)
        expense_tracker._save_admins(config.admins_file, {"999999"})  # some other id, not the owner
        self.assertTrue(expense_tracker._is_admin(777, config))  # owner: always admin
        self.assertTrue(expense_tracker._is_admin(999999, config))  # still honors the file's own admin
        self.assertFalse(expense_tracker._is_admin(4242, config))  # neither owner nor in the file

    async def test_bootstrap_admin_syncs_to_central_bot_admins_table(self):
        config = expense_tracker.config_from_bot_row(
            {"bot_id": 931, "name": "expense_bot_synced", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await expense_tracker.init_db(config.db_path)
        bot, dp = _build_bot_dispatcher(config)
        await dp.feed_webhook_update(bot, _text_update(1, 321, "/start"))

        central_admins = await db_module.get_bot_admins(931)
        self.assertEqual(central_admins, ["321"])


if __name__ == "__main__":
    unittest.main()
