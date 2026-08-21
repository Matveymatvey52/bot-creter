"""Security/correctness fix test for templates/sales_analytics.py — bootstrap
admin bug (BUG 4 of the "первый /start становится админом навсегда" fix set).

This one is the inverse of a privilege-escalation bug: cmd_start used to call
_save_admins(config.admins_file, {"ids": [str(sender_id)]}) — a DICT, not the
set _save_admins expects. _save_admins does json.dumps({"ids": list(ids)});
list() on a dict yields its KEYS, so the admins file was written as
{"ids": ["ids"]} — the literal string "ids", never the real Telegram id.
Every subsequent _is_admin() check failed for everyone, including the real
owner: the bot was fail-closed but completely unusable.

Fixed by passing a real set, and (preferred, per the fix brief) unifying
with the same owner_telegram_id pattern used by every other template: only
bots.owner_telegram_id (when known) may claim the empty-admins bootstrap
slot, and the DB-known owner is always-admin regardless of admins_file
state (defense in depth).

No real Telegram network calls, no real tokens.

Run with: python -m unittest tests.test_sales_analytics_admin_gate
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

import db.database as db_module
from runtime.registry import get_template_router
from templates import sales_analytics

FAKE_TOKEN = "123456:test-token-not-real"


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


def _build_bot_dispatcher(config: sales_analytics.SalesAnalyticsConfig) -> tuple[Bot, Dispatcher]:
    bot = Bot(token=FAKE_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(sales_analytics.ConfigMiddleware(config))
    dp.include_router(get_template_router("sales_analytics"))
    return bot, dp


class SalesAnalyticsAdminGateTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_bootstrap_grants_real_admin_not_the_literal_string_ids(self):
        """The core regression: after /start, the admins file must contain
        the real Telegram id, and _is_admin(sender_id) must be True — not
        the {"ids": ["ids"]} corruption the old dict-vs-set bug produced."""
        config = sales_analytics.config_from_bot_row(
            {"bot_id": 991, "name": "sales_bug4", "display_name": None}, self.data_dir
        )
        await sales_analytics.init_db(config.db_path)
        bot, dp = _build_bot_dispatcher(config)

        SENDER_ID = 424242
        await dp.feed_webhook_update(bot, _text_update(1, SENDER_ID, "/start"))

        stored = sales_analytics._load_admins(config.admins_file)
        self.assertEqual(stored, {str(SENDER_ID)})
        self.assertNotIn("ids", stored)
        self.assertTrue(sales_analytics._is_admin(SENDER_ID, config))

    async def test_non_owner_messaging_first_does_not_become_admin(self):
        config = sales_analytics.config_from_bot_row(
            {"bot_id": 992, "name": "sales_owned", "display_name": None, "owner_telegram_id": 12345},
            self.data_dir,
        )
        await sales_analytics.init_db(config.db_path)
        bot, dp = _build_bot_dispatcher(config)

        CLIENT_ID = 555
        await dp.feed_webhook_update(bot, _text_update(1, CLIENT_ID, "/start"))
        self.assertEqual(sales_analytics._load_admins(config.admins_file), set())
        self.assertFalse(sales_analytics._is_admin(CLIENT_ID, config))

        await dp.feed_webhook_update(bot, _text_update(2, 12345, "/start"))
        self.assertTrue(sales_analytics._is_admin(12345, config))
        self.assertEqual(sales_analytics._load_admins(config.admins_file), {"12345"})

    async def test_owner_is_always_admin_even_with_stale_admins_file(self):
        config = sales_analytics.config_from_bot_row(
            {"bot_id": 993, "name": "sales_owned_2", "display_name": None, "owner_telegram_id": 777},
            self.data_dir,
        )
        await sales_analytics.init_db(config.db_path)
        sales_analytics._save_admins(config.admins_file, {"999999"})
        self.assertTrue(sales_analytics._is_admin(777, config))
        self.assertTrue(sales_analytics._is_admin(999999, config))
        self.assertFalse(sales_analytics._is_admin(4242, config))

    async def test_bootstrap_admin_syncs_to_central_bot_admins_table(self):
        config = sales_analytics.config_from_bot_row(
            {"bot_id": 994, "name": "sales_synced", "display_name": None}, self.data_dir
        )
        await sales_analytics.init_db(config.db_path)
        bot, dp = _build_bot_dispatcher(config)
        await dp.feed_webhook_update(bot, _text_update(1, 321, "/start"))

        central_admins = await db_module.get_bot_admins(994)
        self.assertEqual(central_admins, ["321"])


if __name__ == "__main__":
    unittest.main()
