"""Admin-bootstrap security tests for the car_rental template.

Same criterion as tests/test_shop_catalog_isolation.py: a client who messages
the bot before the owner does must never be able to permanently seize the
admin panel by sending /start first, when bots.owner_telegram_id is known.

No real Telegram network calls, no real tokens.

Run with: python -m unittest tests.test_car_rental_isolation
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
from templates import car_rental as cr

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


def _build_bot_dispatcher(config: cr.CarRentalConfig) -> tuple[Bot, Dispatcher]:
    bot = Bot(token=FAKE_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(cr.ConfigMiddleware(config))
    dp.include_router(get_template_router("car_rental"))
    return bot, dp


class CarRentalAdminBootstrapTests(unittest.IsolatedAsyncioTestCase):
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
        became the bot admin — a client testing the bot link before the
        owner did would silently seize the admin panel. When
        bots.owner_telegram_id is known, only that user may claim the
        bootstrap admin slot."""
        config = cr.config_from_bot_row(
            {"bot_id": 801, "name": "car_rental_bot_owned", "display_name": None,
             "group_chat_id": None, "owner_telegram_id": 12345},
            self.data_dir,
        )
        await cr.init_db(config.db_path)
        bot, dp = _build_bot_dispatcher(config)

        NON_OWNER = 555  # not the owner, messages first
        await dp.feed_webhook_update(bot, _text_update(1, NON_OWNER, "/start"))
        self.assertEqual(cr._load_admins(config.admins_file), set())
        self.assertFalse(cr._is_admin(NON_OWNER, config))

        await dp.feed_webhook_update(bot, _text_update(2, 12345, "/start"))
        self.assertTrue(cr._is_admin(12345, config))
        self.assertEqual(cr._load_admins(config.admins_file), {"12345"})

    async def test_owner_is_always_admin_even_with_stale_admins_file(self):
        """Defense in depth: the DB-known owner must see the admin panel even
        if the local admins_file is empty/stale — owner_telegram_id is
        treated as an unconditional admin in _is_admin, not just at
        bootstrap time."""
        config = cr.config_from_bot_row(
            {"bot_id": 802, "name": "car_rental_bot_owned_2", "display_name": None,
             "group_chat_id": None, "owner_telegram_id": 777},
            self.data_dir,
        )
        await cr.init_db(config.db_path)
        cr._save_admins(config.admins_file, {"999999"})  # some other id, not the owner
        self.assertTrue(cr._is_admin(777, config))  # owner: always admin
        self.assertTrue(cr._is_admin(999999, config))  # still honors the file's own admin
        self.assertFalse(cr._is_admin(4242, config))  # neither owner nor in the file

    async def test_standalone_mode_keeps_first_comer_bootstrap(self):
        """When owner_telegram_id is unknown (standalone/env mode), the old
        first-comer bootstrap behavior remains the only option available."""
        config = cr.config_from_bot_row(
            {"bot_id": 803, "name": "car_rental_bot_standalone", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await cr.init_db(config.db_path)
        self.assertIsNone(config.owner_telegram_id)
        bot, dp = _build_bot_dispatcher(config)

        FIRST_COMER = 4242
        await dp.feed_webhook_update(bot, _text_update(1, FIRST_COMER, "/start"))
        self.assertTrue(cr._is_admin(FIRST_COMER, config))
        self.assertEqual(cr._load_admins(config.admins_file), {"4242"})

    async def test_bootstrap_admin_syncs_to_central_bot_admins_table(self):
        """The mini-app's admin gate (runtime.miniapp_api._admin_gate_ok)
        checks db.database.get_bot_admins(), a separate table from this
        template's local admins_file. The bootstrap grant must land in both."""
        config = cr.config_from_bot_row(
            {"bot_id": 804, "name": "car_rental_bot_synced", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await cr.init_db(config.db_path)
        bot, dp = _build_bot_dispatcher(config)
        await dp.feed_webhook_update(bot, _text_update(1, 321, "/start"))

        central_admins = await db_module.get_bot_admins(804)
        self.assertEqual(central_admins, ["321"])


if __name__ == "__main__":
    unittest.main()
