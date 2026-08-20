"""handlers/start.py's "➕ Создать бота" / "📋 Мои боты" buttons (added per the
owner's project-wide button-over-text-command rule).

Handlers are called directly, not through a real Dispatcher — aiogram Router
objects can only ever attach to one parent Dispatcher for their whole process
lifetime (see tests/test_factory_registry_citizen.py's own note on this), and
handlers/create_bot.py's/handlers/manage_bots.py's routers are already
attached elsewhere in this suite. This matches the existing convention in
tests/test_manage_bots_features.py and tests/test_sheets_connect_flow.py.

Main risk this closes: a callback handler that reads callback.message.from_user
instead of callback.from_user would silently act as the BOT's own identity
(callback.message is the message the bot itself sent), not the presser's — for
"➕ Создать бота" that would mean handlers/create_bot.py's _pending dict and FSM
state get keyed by the wrong user.

Run with: python -m unittest tests.test_start_buttons
"""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import handlers.create_bot as create_bot_module
import handlers.start as start_module
from db.database import add_bot_admin, init_db, remove_bot_admin
from runtime.registry import FACTORY_BOT_ID

PRESSER_USER_ID = 222


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
    callback.message.answer = AsyncMock()
    return callback


def _make_fsm_context(user_id: int) -> FSMContext:
    storage = MemoryStorage()
    key = StorageKey(bot_id=0, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=storage, key=key)


class StartMessageButtonsTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_message_carries_create_and_list_buttons(self):
        message = _make_message(PRESSER_USER_ID, "/start")

        await start_module.cmd_start(message)

        # assets/welcome.png exists in this repo, so cmd_start takes the
        # answer_photo path, not plain answer — matching real behavior.
        message.answer_photo.assert_awaited_once()
        markup = message.answer_photo.call_args.kwargs["reply_markup"]
        callback_data_values = [
            btn.callback_data for row in markup.inline_keyboard for btn in row
        ]
        self.assertIn("start_create", callback_data_values)
        # "list" must match handlers/manage_bots.py's existing cb_list
        # callback_data exactly, or the button silently does nothing.
        self.assertIn("list", callback_data_values)


class StartCreateButtonIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        create_bot_module._pending.pop(PRESSER_USER_ID, None)

    async def test_create_button_uses_the_pressers_own_identity_not_the_bots(self):
        # Pre-seed a stale pending entry under the PRESSER's real id — handle_token()'s
        # own convention is _pending.pop(user_id, None) on every fresh /create-equivalent
        # entry. If cb_start_create used callback.message.from_user.id (the bot's own
        # identity, a different MagicMock id here) instead of callback.from_user.id, this
        # entry would NOT be cleared.
        create_bot_module._pending[PRESSER_USER_ID] = {"stale": "data"}

        callback = _make_callback(PRESSER_USER_ID, "start_create")
        state = _make_fsm_context(PRESSER_USER_ID)

        await create_bot_module.cb_start_create(callback, state)

        self.assertNotIn(
            PRESSER_USER_ID, create_bot_module._pending,
            "stale pending entry for the presser's own id was not cleared — "
            "cb_start_create is not reading callback.from_user.id",
        )
        self.assertEqual(await state.get_state(), create_bot_module.CreateBotStates.gathering.state)
        callback.answer.assert_awaited_once()
        callback.message.answer.assert_awaited_once()


class StartRegistryButtonTests(unittest.IsolatedAsyncioTestCase):
    """The "📋 Реестр ботов" button (multitenancy stage 3, item 4) — visible
    to any admin of the Creator bot itself (bot_admins for bot_id=0), reusing
    the exact same get_bot_admins(bot_id) + str(telegram_user_id) membership
    check runtime/miniapp_api.py's compute_metrics route already uses to gate
    a bot's own analytics to that bot's admins. OWNER_ID is also accepted as
    a fallback, since bot_id=0 has no bots-table row and so can never be
    targeted by /addadmin — see handlers/start.py's _is_creator_bot_admin."""

    ADMIN_USER_ID = 424242

    async def asyncSetUp(self):
        await init_db()
        await add_bot_admin(FACTORY_BOT_ID, str(self.ADMIN_USER_ID))
        self._env_patcher = patch.dict("os.environ", {"MINIAPP_SECRET": "s3cret"})
        self._env_patcher.start()

    async def asyncTearDown(self):
        self._env_patcher.stop()
        await remove_bot_admin(FACTORY_BOT_ID, str(self.ADMIN_USER_ID))

    async def test_creator_bot_admin_sees_registry_button(self):
        message = _make_message(self.ADMIN_USER_ID, "/start")

        await start_module.cmd_start(message)

        message.answer_photo.assert_awaited_once()
        markup = message.answer_photo.call_args.kwargs["reply_markup"]
        labels = [btn.text for row in markup.inline_keyboard for btn in row]
        self.assertIn("📋 Реестр ботов", labels)

    async def test_non_admin_does_not_see_registry_button(self):
        message = _make_message(PRESSER_USER_ID, "/start")

        await start_module.cmd_start(message)

        message.answer_photo.assert_awaited_once()
        markup = message.answer_photo.call_args.kwargs["reply_markup"]
        labels = [btn.text for row in markup.inline_keyboard for btn in row]
        self.assertNotIn("📋 Реестр ботов", labels)

    async def test_owner_id_sees_registry_button_without_bot_admins_row(self):
        # bot_id=0 has no bots-table row, so OWNER_ID can never be added to
        # bot_admins through the normal /addadmin flow — this is the only
        # path that lets the owner actually reach the registry button.
        owner_id = 999999
        with patch.object(start_module, "_OWNER_ID", owner_id):
            message = _make_message(owner_id, "/start")

            await start_module.cmd_start(message)

        message.answer_photo.assert_awaited_once()
        markup = message.answer_photo.call_args.kwargs["reply_markup"]
        labels = [btn.text for row in markup.inline_keyboard for btn in row]
        self.assertIn("📋 Реестр ботов", labels)


if __name__ == "__main__":
    unittest.main()
