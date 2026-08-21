"""Security fix test for templates/accountant.py — bootstrap admin bug (BUG 1 of
the "первый /start становится админом навсегда" fix set).

Two separate issues fixed together:
1. kb_main() used to ignore admin status entirely — every user, admin or not,
   got the same reply keyboard including the Excel/HTML export buttons (whose
   handlers ARE admin-gated, so tapping them as a non-admin just produced a
   "⛔ Нет доступа" reply after the fact — a UX leak, not silently exploitable,
   but still wrong). kb_main() now takes is_admin and hides those buttons.
2. cmd_start used to grant the bootstrap admin slot to whoever sent /start
   FIRST — a client messaging the bot before its owner could permanently
   seize admin. Now the empty-admins slot can only be claimed by
   bots.owner_telegram_id when it's known (webhook/production mode); the
   DB-known owner is also treated as always-admin regardless of admins_file
   state (defense in depth), and admin add/remove syncs into the central
   bot_admins table.

No real Telegram network calls, no real tokens.

Run with: python -m unittest tests.test_accountant_admin_gate
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
from templates import accountant

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


def _build_bot_dispatcher(config: accountant.AccountantConfig) -> tuple[Bot, Dispatcher]:
    bot = Bot(token=FAKE_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(accountant.ConfigMiddleware(config))
    dp.include_router(get_template_router("accountant"))
    return bot, dp


class AccountantAdminGateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._mock_call = self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

        # cmd_start now syncs the bootstrap admin into db.database.add_bot_admin
        # (central bot_admins table) — redirect to a throwaway DB.
        self._central_db_path = self.data_dir / "central_bots.db"
        self._db_path_patcher = patch.object(db_module, "DB_PATH", self._central_db_path)
        self._db_path_patcher.start()
        await db_module.init_db()

    async def asyncTearDown(self):
        self._db_path_patcher.stop()
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_non_owner_messaging_first_does_not_become_admin(self):
        config = accountant.config_from_bot_row(
            {"bot_id": 901, "name": "acc_owned", "display_name": None,
             "group_chat_id": None, "owner_telegram_id": 12345},
            self.data_dir,
        )
        await accountant.init_db(config.db_path)
        bot, dp = _build_bot_dispatcher(config)

        CLIENT_ID = 555  # not the owner, messages first
        await dp.feed_webhook_update(bot, _text_update(1, CLIENT_ID, "/start"))
        self.assertEqual(accountant._load_admins(config.admins_file), set())
        self.assertFalse(accountant._is_bot_admin(CLIENT_ID, config))

        await dp.feed_webhook_update(bot, _text_update(2, 12345, "/start"))
        self.assertTrue(accountant._is_bot_admin(12345, config))
        self.assertEqual(accountant._load_admins(config.admins_file), {"12345"})

    async def test_owner_is_always_admin_even_with_stale_admins_file(self):
        config = accountant.config_from_bot_row(
            {"bot_id": 902, "name": "acc_owned_2", "display_name": None,
             "group_chat_id": None, "owner_telegram_id": 777},
            self.data_dir,
        )
        await accountant.init_db(config.db_path)
        accountant._save_admins(config.admins_file, {"999999"})
        self.assertTrue(accountant._is_bot_admin(777, config))
        self.assertTrue(accountant._is_bot_admin(999999, config))
        self.assertFalse(accountant._is_bot_admin(4242, config))

    async def test_bootstrap_admin_syncs_to_central_bot_admins_table(self):
        config = accountant.config_from_bot_row(
            {"bot_id": 903, "name": "acc_synced", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await accountant.init_db(config.db_path)
        bot, dp = _build_bot_dispatcher(config)
        await dp.feed_webhook_update(bot, _text_update(1, 321, "/start"))

        central_admins = await db_module.get_bot_admins(903)
        self.assertEqual(central_admins, ["321"])

    async def test_admin_only_buttons_hidden_from_non_admin(self):
        config = accountant.config_from_bot_row(
            {"bot_id": 904, "name": "acc_ui", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await accountant.init_db(config.db_path)
        bot, dp = _build_bot_dispatcher(config)

        await dp.feed_webhook_update(bot, _text_update(1, 42, "/start"))  # 42 becomes first admin
        self._mock_call.reset_mock()
        await dp.feed_webhook_update(bot, _text_update(2, 99, "/start"))  # 99 is a plain user

        sent = [
            call.args[0] for call in self._mock_call.call_args_list
            if call.args and hasattr(call.args[0], "reply_markup") and call.args[0].reply_markup
        ]
        found_excel = any(
            btn.text == "📥 Excel"
            for method in sent for row in method.reply_markup.keyboard for btn in row
        )
        self.assertFalse(found_excel)

    async def test_admin_only_buttons_shown_to_admin(self):
        config = accountant.config_from_bot_row(
            {"bot_id": 905, "name": "acc_ui_admin", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await accountant.init_db(config.db_path)
        bot, dp = _build_bot_dispatcher(config)

        await dp.feed_webhook_update(bot, _text_update(1, 42, "/start"))  # 42 becomes first admin
        sent = [
            call.args[0] for call in self._mock_call.call_args_list
            if call.args and hasattr(call.args[0], "reply_markup") and call.args[0].reply_markup
        ]
        found_excel = any(
            btn.text == "📥 Excel"
            for method in sent for row in method.reply_markup.keyboard for btn in row
        )
        self.assertTrue(found_excel)


if __name__ == "__main__":
    unittest.main()
