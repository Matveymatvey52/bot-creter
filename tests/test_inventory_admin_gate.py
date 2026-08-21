"""Security fix test for templates/inventory.py — bootstrap admin bug
(BUG 3 of the "первый /start становится админом навсегда" fix set).

kb_main()'s 4 buttons (➕ Приход, ➖ Расход, 📦 Остатки, 📋 История) are open to
every team member by design — none of them are admin-gated, so kb_main()
does not need an is_admin parameter here. The actual vulnerability was
purely in cmd_start: whoever sent /start FIRST permanently became the
bot's admin (the only user allowed to run /additem, /removeitem,
/addsupplier, /addadmin, ...), letting a non-owner client who messages the
bot before its owner seize those commands.

Fixed: only bots.owner_telegram_id (when known) may claim the empty-admins
bootstrap slot; the DB-known owner is always-admin regardless of
admins_file state (defense in depth); /addadmin, /removeadmin sync into
the central bot_admins table.

No real Telegram network calls, no real tokens.

Run with: python -m unittest tests.test_inventory_admin_gate
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
from templates import inventory

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


def _build_bot_dispatcher(config: inventory.InventoryConfig) -> tuple[Bot, Dispatcher]:
    bot = Bot(token=FAKE_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(inventory.ConfigMiddleware(config))
    dp.include_router(get_template_router("inventory"))
    return bot, dp


class InventoryAdminGateTests(unittest.IsolatedAsyncioTestCase):
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
        config = inventory.config_from_bot_row(
            {"bot_id": 971, "name": "inv_owned", "display_name": None,
             "group_chat_id": None, "owner_telegram_id": 12345},
            self.data_dir,
        )
        await inventory.init_db(config.db_path)
        bot, dp = _build_bot_dispatcher(config)

        CLIENT_ID = 555
        await dp.feed_webhook_update(bot, _text_update(1, CLIENT_ID, "/start"))
        self.assertEqual(inventory._load_admins(config.admins_file), set())
        self.assertFalse(inventory._is_bot_admin(CLIENT_ID, config))

        await dp.feed_webhook_update(bot, _text_update(2, 12345, "/start"))
        self.assertTrue(inventory._is_bot_admin(12345, config))
        self.assertEqual(inventory._load_admins(config.admins_file), {"12345"})

    async def test_owner_is_always_admin_even_with_stale_admins_file(self):
        config = inventory.config_from_bot_row(
            {"bot_id": 972, "name": "inv_owned_2", "display_name": None,
             "group_chat_id": None, "owner_telegram_id": 777},
            self.data_dir,
        )
        await inventory.init_db(config.db_path)
        inventory._save_admins(config.admins_file, {"999999"})
        self.assertTrue(inventory._is_bot_admin(777, config))
        self.assertTrue(inventory._is_bot_admin(999999, config))
        self.assertFalse(inventory._is_bot_admin(4242, config))

    async def test_bootstrap_admin_syncs_to_central_bot_admins_table(self):
        config = inventory.config_from_bot_row(
            {"bot_id": 973, "name": "inv_synced", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await inventory.init_db(config.db_path)
        bot, dp = _build_bot_dispatcher(config)
        await dp.feed_webhook_update(bot, _text_update(1, 321, "/start"))

        central_admins = await db_module.get_bot_admins(973)
        self.assertEqual(central_admins, ["321"])

    async def test_non_owner_cannot_run_additem_after_first_start(self):
        """End-to-end: a non-owner who messages first must still be denied
        the admin-only /additem command afterward."""
        config = inventory.config_from_bot_row(
            {"bot_id": 974, "name": "inv_e2e", "display_name": None,
             "group_chat_id": None, "owner_telegram_id": 999},
            self.data_dir,
        )
        await inventory.init_db(config.db_path)
        bot, dp = _build_bot_dispatcher(config)

        CLIENT_ID = 42
        await dp.feed_webhook_update(bot, _text_update(1, CLIENT_ID, "/start"))
        await dp.feed_webhook_update(bot, _text_update(2, CLIENT_ID, "/additem SKU1 | Item"))
        items = await inventory._active_items(config.db_path)
        self.assertEqual(items, [])


if __name__ == "__main__":
    unittest.main()
