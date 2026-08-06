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
from unittest.mock import AsyncMock, MagicMock

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import handlers.create_bot as create_bot_module
import handlers.start as start_module

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


if __name__ == "__main__":
    unittest.main()
