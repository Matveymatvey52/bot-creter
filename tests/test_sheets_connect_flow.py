"""Sheets-connect FSM tests (handlers/manage_bots.py's sheetsconnect:/
SheetsConnectFlow — sheets-feature-inventory Task 2).

Task 3's persistence-before-access-check criterion: a spreadsheet_id/link the
shared Service Account can't actually open must NEVER reach bot_sheets_config
— verify_access() runs before set_bot_sheets_config(), not after, and a
rejected link leaves the FSM in the same waiting_for_link state for a retry.

Also enforces the owner's explicit requirement that the connect flow's text
honestly discloses the shared (not per-bot) Service Account — a silent/
neutral wording would defeat the whole point of asking for this flow.
"""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import handlers.admin_manager as admin_manager
import handlers.manage_bots as manage_bots
from db.database import create_bot_record_with_admins, delete_bot, get_bot_sheets_config

FAKE_TOKEN = "123456789:AAHfakeTokenButShapedRight1234567890"
FAKE_SA_EMAIL = "bot-creator@bot-creator-504417.iam.gserviceaccount.com"


def _make_callback(user_id: int, data: str) -> MagicMock:
    callback = MagicMock()
    callback.from_user.id = user_id
    callback.data = data
    callback.answer = AsyncMock()
    callback.message.edit_text = AsyncMock()
    callback.message.answer = AsyncMock()
    return callback


def _make_message(user_id: int, text: str) -> MagicMock:
    message = MagicMock()
    message.from_user.id = user_id
    message.text = text
    message.answer = AsyncMock()
    return message


def _make_fsm_context(user_id: int) -> FSMContext:
    storage = MemoryStorage()
    key = StorageKey(bot_id=0, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=storage, key=key)


class FeaturesKeyboardTruncatesLongSheetTitleTests(unittest.TestCase):
    """Telegram caps inline button text at 64 chars — a real spreadsheet
    title (unbounded, comes from Google) previously blew that limit and made
    every future render of this bot's features panel raise
    BUTTON_TEXT_INVALID (review-orchestrator finding, fixed here)."""

    def test_long_title_is_truncated_in_button_label(self):
        long_title = "A" * 100
        markup = manage_bots._features_keyboard(
            bot_id=1,
            compatible=[{"name": "sheets", "compatible_with": ["inventory"]}],
            enabled={"sheets"},
            sheets_config={"spreadsheet_id": "abc", "sheet_title": long_title},
        )
        sheets_row = next(
            row for row in markup.inline_keyboard
            for btn in row if btn.callback_data == "sheetsconnect:1"
        )
        button_text = sheets_row[0].text
        self.assertLessEqual(len(button_text), 64, f"button text is {len(button_text)} chars — exceeds Telegram's limit")


class SheetsConnectStartTests(unittest.IsolatedAsyncioTestCase):
    owner_id = 555001

    async def asyncSetUp(self):
        self.bot_id = await create_bot_record_with_admins(
            name="sheets_start_bot", description="test", token=FAKE_TOKEN,
            file_path="templates/inventory.py", admin_ids=[str(self.owner_id)],
        )
        self._owner_patcher = patch.object(manage_bots, "_is_owner", lambda uid: uid == self.owner_id)
        self._owner_patcher.start()
        # sheets-connect handlers now go through _can_manage_bot (Stage 1
        # per-bot ownership), which closes over admin_manager's OWN
        # _is_owner — patch both consistently.
        self._admin_owner_patcher = patch.object(admin_manager, "_is_owner", lambda uid: uid == self.owner_id)
        self._admin_owner_patcher.start()
        self._email_patcher = patch.object(
            manage_bots, "get_service_account_email", AsyncMock(return_value=FAKE_SA_EMAIL)
        )
        self._email_patcher.start()

    async def asyncTearDown(self):
        self._email_patcher.stop()
        self._admin_owner_patcher.stop()
        self._owner_patcher.stop()
        await delete_bot(self.bot_id)

    async def test_shows_connect_steps_with_sa_email(self):
        callback = _make_callback(self.owner_id, f"sheetsconnect:{self.bot_id}")
        state = _make_fsm_context(self.owner_id)

        await manage_bots.cb_sheets_connect_start(callback, state)

        callback.message.edit_text.assert_awaited_once()
        text = callback.message.edit_text.call_args[0][0]
        self.assertIn(FAKE_SA_EMAIL, text)
        # Owner's revised requirement (UX review, 2026-08-15): the shared-SA
        # risk explanation was confirmed as a non-issue — sheets.py's
        # read_data/write_row/verify_access always key strictly off
        # bot_sheets_config's per-bot_id spreadsheet_id via gc.open_by_key,
        # never enumerating/listing the SA's other shared spreadsheets — so
        # the warning was removed entirely in favor of a plain connect guide.
        self.assertNotIn("общий сервисный аккаунт", text.lower())
        self.assertEqual(await state.get_state(), manage_bots.SheetsConnectFlow.waiting_for_link.state)

    async def test_non_owner_is_denied_and_state_is_not_set(self):
        callback = _make_callback(999999, f"sheetsconnect:{self.bot_id}")
        state = _make_fsm_context(999999)

        await manage_bots.cb_sheets_connect_start(callback, state)

        callback.answer.assert_awaited_once()
        callback.message.edit_text.assert_not_awaited()
        self.assertIsNone(await state.get_state())


class SheetsConnectLinkTests(unittest.IsolatedAsyncioTestCase):
    owner_id = 555002

    async def asyncSetUp(self):
        self.bot_id = await create_bot_record_with_admins(
            name="sheets_link_bot", description="test", token=FAKE_TOKEN,
            file_path="templates/inventory.py", admin_ids=[str(self.owner_id)],
        )
        self._owner_patcher = patch.object(manage_bots, "_is_owner", lambda uid: uid == self.owner_id)
        self._owner_patcher.start()
        # sheets-connect handlers now go through _can_manage_bot (Stage 1
        # per-bot ownership), which closes over admin_manager's OWN
        # _is_owner — patch both consistently.
        self._admin_owner_patcher = patch.object(admin_manager, "_is_owner", lambda uid: uid == self.owner_id)
        self._admin_owner_patcher.start()

    async def asyncTearDown(self):
        self._admin_owner_patcher.stop()
        self._owner_patcher.stop()
        await delete_bot(self.bot_id)

    async def test_inaccessible_spreadsheet_is_rejected_before_saving(self):
        with patch.object(manage_bots, "verify_access", AsyncMock(return_value=None)):
            message = _make_message(
                self.owner_id, "https://docs.google.com/spreadsheets/d/abc123def456ghijklmno/edit"
            )
            state = _make_fsm_context(self.owner_id)
            await state.set_state(manage_bots.SheetsConnectFlow.waiting_for_link)
            await state.update_data(bot_id=self.bot_id)

            await manage_bots.msg_sheets_connect_link(message, state)

            self.assertIsNone(await get_bot_sheets_config(self.bot_id))
            message.answer.assert_awaited_once()
            self.assertIn("не удалось открыть", message.answer.call_args[0][0].lower())
            # Left in the same state so the owner can just resend a corrected link.
            self.assertEqual(await state.get_state(), manage_bots.SheetsConnectFlow.waiting_for_link.state)

    async def test_accessible_spreadsheet_is_saved_and_state_cleared(self):
        with patch.object(manage_bots, "verify_access", AsyncMock(return_value="My Sheet")):
            message = _make_message(self.owner_id, "abc123def456ghijklmno")
            state = _make_fsm_context(self.owner_id)
            await state.set_state(manage_bots.SheetsConnectFlow.waiting_for_link)
            await state.update_data(bot_id=self.bot_id)

            await manage_bots.msg_sheets_connect_link(message, state)

            saved = await get_bot_sheets_config(self.bot_id)
            self.assertIsNotNone(saved)
            self.assertEqual(saved["spreadsheet_id"], "abc123def456ghijklmno")
            self.assertEqual(saved["sheet_title"], "My Sheet")
            self.assertIsNone(await state.get_state())

    async def test_unparseable_text_never_calls_verify_access(self):
        with patch.object(manage_bots, "verify_access", AsyncMock()) as mock_verify:
            message = _make_message(self.owner_id, "hello there, not a link")
            state = _make_fsm_context(self.owner_id)
            await state.set_state(manage_bots.SheetsConnectFlow.waiting_for_link)
            await state.update_data(bot_id=self.bot_id)

            await manage_bots.msg_sheets_connect_link(message, state)

            mock_verify.assert_not_awaited()
            self.assertIsNone(await get_bot_sheets_config(self.bot_id))
            message.answer.assert_awaited_once()
            self.assertIn("не похоже", message.answer.call_args[0][0].lower())


if __name__ == "__main__":
    unittest.main()
