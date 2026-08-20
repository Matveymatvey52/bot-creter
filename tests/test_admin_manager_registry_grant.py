"""handlers/admin_manager.py's /addadmin flow, extended to reach bot_id=0
(FACTORY_BOT_ID, the Creator bot / registry itself).

bot_id=0 has no bots-table row, so it never appeared in get_all_bots(), and
the owner had no way to grant "📋 Реестр ботов" access (handlers/start.py's
_is_creator_bot_admin) to anyone else. _bots_keyboard() now injects a
synthetic "Реестр ботов (Creator-бот)" entry for bot_id=0 alongside real
bots, and _bot_display_name() gives it a label since get_bot(0) is None.

Run with: python -m unittest tests.test_admin_manager_registry_grant
"""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import handlers.admin_manager as admin_manager_module
import handlers.start as start_module
from db.database import get_bot_admins, init_db, remove_bot_admin
from runtime.registry import FACTORY_BOT_ID

OWNER_ID = 111111
GRANTEE_ID = 555555


def _make_message(user_id: int, text: str) -> MagicMock:
    message = MagicMock()
    message.from_user.id = user_id
    message.text = text
    message.answer = AsyncMock()
    message.answer_photo = AsyncMock()
    return message


def _make_callback(user_id: int, data: str) -> MagicMock:
    callback = MagicMock()
    callback.from_user.id = user_id
    callback.data = data
    callback.answer = AsyncMock()
    callback.message.edit_text = AsyncMock()
    return callback


def _make_fsm_context(user_id: int) -> FSMContext:
    storage = MemoryStorage()
    key = StorageKey(bot_id=0, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=storage, key=key)


class RegistryAdminGrantTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await init_db()
        self._owner_patcher = patch.object(admin_manager_module, "OWNER_ID", OWNER_ID)
        self._owner_patcher.start()
        self._env_patcher = patch.dict("os.environ", {"MINIAPP_SECRET": "s3cret"})
        self._env_patcher.start()

    async def asyncTearDown(self):
        self._env_patcher.stop()
        self._owner_patcher.stop()
        await remove_bot_admin(FACTORY_BOT_ID, str(GRANTEE_ID))

    async def test_bots_keyboard_offers_registry_entry_even_with_no_tenant_bots(self):
        message = _make_message(OWNER_ID, "/addadmin")
        state = _make_fsm_context(OWNER_ID)

        with patch.object(admin_manager_module, "get_all_bots", AsyncMock(return_value=[])):
            await admin_manager_module.cmd_add_admin(message, state)

        message.answer.assert_awaited_once()
        markup = message.answer.call_args.kwargs["reply_markup"]
        callback_data_values = [
            btn.callback_data for row in markup.inline_keyboard for btn in row
        ]
        self.assertIn(f"adm_add:{FACTORY_BOT_ID}", callback_data_values)

    async def test_granting_registry_admin_unlocks_start_button(self):
        state = _make_fsm_context(OWNER_ID)
        callback = _make_callback(OWNER_ID, f"adm_add:{FACTORY_BOT_ID}")
        await state.set_state(admin_manager_module.AdminStates.choosing_bot_to_add)

        await admin_manager_module.cb_bot_selected_add(callback, state)
        self.assertEqual(await state.get_state(), admin_manager_module.AdminStates.entering_id_to_add.state)

        id_message = _make_message(OWNER_ID, str(GRANTEE_ID))
        await admin_manager_module.msg_id_to_add(id_message, state)

        admins = await get_bot_admins(FACTORY_BOT_ID)
        self.assertIn(str(GRANTEE_ID), admins)

        start_message = _make_message(GRANTEE_ID, "/start")
        await start_module.cmd_start(start_message)
        markup = start_message.answer_photo.call_args.kwargs["reply_markup"]
        labels = [btn.text for row in markup.inline_keyboard for btn in row]
        self.assertIn("📋 Реестр ботов", labels)


if __name__ == "__main__":
    unittest.main()
