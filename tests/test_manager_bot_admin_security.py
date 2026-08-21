"""Security fix: previously, whoever sent /start FIRST permanently became
templates/manager_bot.py's manager (== admin) — anyone messaging the bot
before the owner did would silently seize it, since manager_bot has no
separate client menu (non-managers just get "⛔ Нет доступа"). See
tests/test_shop_catalog_isolation.py for the original of this fix, applied
identically here.

Run with: python -m pytest tests/test_manager_bot_admin_security.py
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Message, User

import db.database as db_module
from templates import manager_bot


def _make_message(user_id: int, text: str) -> Message:
    return Message.model_construct(
        message_id=1,
        date=0,
        chat=Chat(id=user_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="Test"),
        text=text,
    )


class ManagerBotAdminBootstrapSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

        self._central_db_path = self.data_dir / "central_bots.db"
        self._db_path_patcher = patch.object(db_module, "DB_PATH", self._central_db_path)
        self._db_path_patcher.start()
        await db_module.init_db()

        self.bot = Bot(token="123:fake")
        self.storage = MemoryStorage()

    async def asyncTearDown(self):
        self._db_path_patcher.stop()
        self._bot_call_patcher.stop()
        await self.bot.session.close()
        self._tmp.cleanup()

    def _config(self, bot_id: int, owner_telegram_id) -> manager_bot.ManagerBotConfig:
        config = manager_bot.ManagerBotConfig(
            bot_name=f"bot{bot_id}",
            db_path=str(self.data_dir / f"bot_{bot_id}_data.db"),
            admins_file=str(self.data_dir / f"admins_{bot_id}.json"),
            welcome_image=self.data_dir / f"bot_{bot_id}.jpg",
        )
        config.bot_id = bot_id
        config.owner_telegram_id = owner_telegram_id
        return config

    async def test_non_owner_messaging_first_does_not_become_admin(self):
        config = self._config(935, owner_telegram_id=12345)
        state = FSMContext(storage=self.storage, key=MagicMock(chat_id=555, user_id=555, bot_id=935))

        CLIENT_ID = 555  # not the owner, messages first
        msg1 = _make_message(CLIENT_ID, "/start")
        object.__setattr__(msg1, "_bot", self.bot)
        await manager_bot.cmd_start(msg1, state, config)
        self.assertEqual(manager_bot._load_managers(config.admins_file), set())
        self.assertFalse(manager_bot._is_manager(CLIENT_ID, config))

        state2 = FSMContext(storage=self.storage, key=MagicMock(chat_id=12345, user_id=12345, bot_id=935))
        msg2 = _make_message(12345, "/start")
        object.__setattr__(msg2, "_bot", self.bot)
        await manager_bot.cmd_start(msg2, state2, config)
        self.assertTrue(manager_bot._is_manager(12345, config))
        self.assertEqual(manager_bot._load_managers(config.admins_file), {"12345"})

    async def test_owner_is_always_admin_even_with_stale_admins_file(self):
        config = self._config(936, owner_telegram_id=777)
        manager_bot._save_managers(config.admins_file, {"999999"})  # some other id, not the owner
        self.assertTrue(manager_bot._is_manager(777, config))  # owner: always admin
        self.assertTrue(manager_bot._is_manager(999999, config))  # still honors the file's own admin
        self.assertFalse(manager_bot._is_manager(4242, config))  # neither owner nor in the file

    async def test_bootstrap_admin_syncs_to_central_bot_admins_table(self):
        config = self._config(937, owner_telegram_id=None)
        state = FSMContext(storage=self.storage, key=MagicMock(chat_id=321, user_id=321, bot_id=937))
        msg = _make_message(321, "/start")
        object.__setattr__(msg, "_bot", self.bot)
        await manager_bot.cmd_start(msg, state, config)

        central_admins = await db_module.get_bot_admins(937)
        self.assertEqual(central_admins, ["321"])


if __name__ == "__main__":
    unittest.main()
