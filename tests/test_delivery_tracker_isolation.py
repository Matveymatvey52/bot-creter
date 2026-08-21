"""Admin-bootstrap security tests for templates/delivery_tracker.py — same
criterion as tests/test_shop_catalog_isolation.py's admin tests: whoever
sends /start FIRST must NOT permanently become admin when bots
.owner_telegram_id is known; only the real owner may claim the bootstrap
slot, and the DB-known owner is always treated as admin regardless of the
local admins_file state.

No real Telegram network calls, no real tokens.

Run with: python -m unittest tests.test_delivery_tracker_isolation
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
from templates import delivery_tracker as dt

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


def _build_bot_dispatcher(config: dt.DeliveryTrackerConfig) -> tuple[Bot, Dispatcher]:
    bot = Bot(token=FAKE_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(dt.ConfigMiddleware(config))
    dp.include_router(get_template_router("delivery_tracker"))
    return bot, dp


class DeliveryTrackerAdminBootstrapTests(unittest.IsolatedAsyncioTestCase):
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
        """Security fix: previously, whoever sent /start FIRST permanently
        became the bot admin. When bots.owner_telegram_id is known, only
        that user may claim the bootstrap admin slot."""
        config = dt.config_from_bot_row(
            {"bot_id": 601, "name": "delivery_bot_owned", "display_name": None,
             "group_chat_id": None, "owner_telegram_id": 12345},
            self.data_dir,
        )
        await dt.init_db(config.db_path)
        bot, dp = _build_bot_dispatcher(config)

        CLIENT_ID = 555  # not the owner, messages first
        await dp.feed_webhook_update(bot, _text_update(1, CLIENT_ID, "/start"))
        self.assertEqual(dt._load_admins(config.admins_file), set())
        self.assertFalse(dt._is_admin(CLIENT_ID, config))

        await dp.feed_webhook_update(bot, _text_update(2, 12345, "/start"))
        self.assertTrue(dt._is_admin(12345, config))
        self.assertEqual(dt._load_admins(config.admins_file), {"12345"})

    async def test_owner_is_always_admin_even_with_stale_admins_file(self):
        """Defense in depth: the DB-known owner must count as admin even if
        the local admins_file is empty/stale — owner_telegram_id is an
        unconditional admin check, not just at bootstrap time."""
        config = dt.config_from_bot_row(
            {"bot_id": 602, "name": "delivery_bot_owned_2", "display_name": None,
             "group_chat_id": None, "owner_telegram_id": 777},
            self.data_dir,
        )
        await dt.init_db(config.db_path)
        dt._save_admins(config.admins_file, {"999999"})  # some other id, not the owner
        self.assertTrue(dt._is_admin(777, config))  # owner: always admin
        self.assertTrue(dt._is_admin(999999, config))  # still honors the file's own admin
        self.assertFalse(dt._is_admin(4242, config))  # neither owner nor in the file

    async def test_standalone_mode_keeps_first_comer_bootstrap(self):
        """owner_telegram_id=None (standalone/env mode) must keep the OLD
        first-comer bootstrap as the only available option — there is no DB
        owner to defer to."""
        config = dt.config_from_bot_row(
            {"bot_id": 603, "name": "delivery_bot_standalone", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await dt.init_db(config.db_path)
        bot, dp = _build_bot_dispatcher(config)

        FIRST_USER = 111
        await dp.feed_webhook_update(bot, _text_update(1, FIRST_USER, "/start"))
        self.assertEqual(dt._load_admins(config.admins_file), {"111"})
        self.assertTrue(dt._is_admin(FIRST_USER, config))

    async def test_bootstrap_admin_syncs_to_central_bot_admins_table(self):
        """The mini-app's admin gate (runtime.miniapp_api._admin_gate_ok)
        checks db.database.get_bot_admins() — a separate table from this
        template's local admins_file. The bootstrap grant must land in both."""
        config = dt.config_from_bot_row(
            {"bot_id": 604, "name": "delivery_bot_synced", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await dt.init_db(config.db_path)
        bot, dp = _build_bot_dispatcher(config)
        await dp.feed_webhook_update(bot, _text_update(1, 321, "/start"))

        central_admins = await db_module.get_bot_admins(604)
        self.assertEqual(central_admins, ["321"])

    async def test_configs_point_to_different_files(self):
        config_a = dt.config_from_bot_row(
            {"bot_id": 701, "name": "delivery_bot_a", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        config_b = dt.config_from_bot_row(
            {"bot_id": 702, "name": "delivery_bot_b", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        self.assertNotEqual(config_a.db_path, config_b.db_path)
        self.assertNotEqual(config_a.admins_file, config_b.admins_file)


if __name__ == "__main__":
    unittest.main()
