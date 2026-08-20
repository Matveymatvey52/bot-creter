"""Payment-connect FSM tests (handlers/manage_bots.py's paystart:/paystep:/
paycancel: — PaymentConnectFlow, the owner-facing wizard around
set_bot_payment_provider).

Telegram's Bot API has no method to programmatically attach a payment
provider to a bot, so this wizard can't skip BotFather — it only walks the
owner through the 4 required steps and validates the provider_token they
paste back in step 4 before it's saved (Fernet-encrypted, same as
bots.token) via set_bot_payment_provider.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import handlers.admin_manager as admin_manager
import handlers.manage_bots as manage_bots
from db.database import create_bot_record_with_admins, delete_bot, get_bot_payment_provider

FAKE_TOKEN = "123456789:AAHfakeTokenButShapedRight1234567890"
VALID_PROVIDER_TOKEN = "381764678:TEST:abcXYZ123"


def _make_callback(user_id: int, data: str) -> MagicMock:
    callback = MagicMock()
    callback.from_user.id = user_id
    callback.data = data
    callback.answer = AsyncMock()
    callback.message.edit_text = AsyncMock()
    callback.message.answer = AsyncMock()
    callback.message.answer_photo = AsyncMock()
    callback.message.delete = AsyncMock()
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


class PaymentConnectStartTests(unittest.IsolatedAsyncioTestCase):
    owner_id = 555101

    async def asyncSetUp(self):
        self.bot_id = await create_bot_record_with_admins(
            name="payment_start_bot", description="test", token=FAKE_TOKEN,
            file_path="templates/inventory.py", admin_ids=[str(self.owner_id)],
        )
        self._owner_patcher = patch.object(manage_bots, "_is_owner", lambda uid: uid == self.owner_id)
        self._owner_patcher.start()
        # payment-connect handlers now go through _can_manage_bot (Stage 1
        # per-bot ownership), which closes over admin_manager's OWN
        # _is_owner — patch both consistently.
        self._admin_owner_patcher = patch.object(admin_manager, "_is_owner", lambda uid: uid == self.owner_id)
        self._admin_owner_patcher.start()

    async def asyncTearDown(self):
        self._admin_owner_patcher.stop()
        self._owner_patcher.stop()
        await delete_bot(self.bot_id)

    async def test_shows_provider_choice_screen(self):
        callback = _make_callback(self.owner_id, f"paystart:{self.bot_id}")
        state = _make_fsm_context(self.owner_id)

        await manage_bots.cb_payment_connect_start(callback, state)

        callback.message.edit_text.assert_awaited_once()
        text = callback.message.edit_text.call_args[0][0]
        self.assertIn("ЮKassa", text)
        self.assertIn("Cloudpayments", text)
        markup = callback.message.edit_text.call_args[1]["reply_markup"]
        callback_datas = [btn.callback_data for row in markup.inline_keyboard for btn in row if btn.callback_data]
        self.assertIn(f"paychooseyk:{self.bot_id}", callback_datas)
        self.assertIn(f"paychoosecp:{self.bot_id}", callback_datas)

    async def test_choosing_yookassa_shows_step_1_with_registration_link(self):
        callback = _make_callback(self.owner_id, f"paychooseyk:{self.bot_id}")
        state = _make_fsm_context(self.owner_id)

        await manage_bots.cb_payment_choose_yookassa(callback, state)

        callback.message.edit_text.assert_awaited_once()
        text = callback.message.edit_text.call_args[0][0]
        self.assertIn("Шаг 1 из 4", text)
        markup = callback.message.edit_text.call_args[1]["reply_markup"]
        urls = [btn.url for row in markup.inline_keyboard for btn in row if btn.url]
        self.assertTrue(any("yookassa.ru" in u for u in urls))

    async def test_non_owner_is_denied(self):
        callback = _make_callback(999999, f"paystart:{self.bot_id}")
        state = _make_fsm_context(999999)

        await manage_bots.cb_payment_connect_start(callback, state)

        callback.answer.assert_awaited_once()
        callback.message.edit_text.assert_not_awaited()


class PaymentConnectStepNavigationTests(unittest.IsolatedAsyncioTestCase):
    owner_id = 555102

    async def asyncSetUp(self):
        self.bot_id = await create_bot_record_with_admins(
            name="payment_step_bot", description="test", token=FAKE_TOKEN,
            file_path="templates/inventory.py", admin_ids=[str(self.owner_id)],
        )
        self._owner_patcher = patch.object(manage_bots, "_is_owner", lambda uid: uid == self.owner_id)
        self._owner_patcher.start()
        # payment-connect handlers now go through _can_manage_bot (Stage 1
        # per-bot ownership), which closes over admin_manager's OWN
        # _is_owner — patch both consistently.
        self._admin_owner_patcher = patch.object(admin_manager, "_is_owner", lambda uid: uid == self.owner_id)
        self._admin_owner_patcher.start()

    async def asyncTearDown(self):
        self._admin_owner_patcher.stop()
        self._owner_patcher.stop()
        await delete_bot(self.bot_id)

    async def test_step_2_shows_botfather_link_and_uses_browsing_state_not_token_state(self):
        callback = _make_callback(self.owner_id, f"paystep:{self.bot_id}:2")
        state = _make_fsm_context(self.owner_id)

        await manage_bots.cb_payment_connect_step(callback, state)

        text = callback.message.edit_text.call_args[0][0]
        self.assertIn("Шаг 2 из 4", text)
        markup = callback.message.edit_text.call_args[1]["reply_markup"]
        urls = [btn.url for row in markup.inline_keyboard for btn in row if btn.url]
        self.assertTrue(any("t.me/BotFather" in u for u in urls))
        # Steps 1-3 use browsing_step (not waiting_for_token) — state-guarded
        # from the first screen so bot_id in FSM data can't be silently
        # clobbered by an unrelated flow, but no text handler is listening.
        self.assertEqual(await state.get_state(), manage_bots.PaymentConnectFlow.browsing_step.state)

    async def test_step_4_enters_waiting_for_token_state(self):
        callback = _make_callback(self.owner_id, f"paystep:{self.bot_id}:4")
        state = _make_fsm_context(self.owner_id)

        await manage_bots.cb_payment_connect_step(callback, state)

        text = callback.message.edit_text.call_args[0][0]
        self.assertIn("Шаг 4 из 4", text)
        self.assertEqual(await state.get_state(), manage_bots.PaymentConnectFlow.waiting_for_token.state)
        data = await state.get_data()
        self.assertEqual(data.get("bot_id"), self.bot_id)

    async def test_out_of_range_step_is_ignored_not_a_crash(self):
        callback = _make_callback(self.owner_id, f"paystep:{self.bot_id}:99")
        state = _make_fsm_context(self.owner_id)

        await manage_bots.cb_payment_connect_step(callback, state)

        callback.message.edit_text.assert_not_awaited()

    async def test_nonexistent_bot_id_does_not_enter_wizard(self):
        callback = _make_callback(self.owner_id, "paystart:999999999")
        state = _make_fsm_context(self.owner_id)

        await manage_bots.cb_payment_connect_start(callback, state)

        callback.message.edit_text.assert_not_awaited()
        self.assertIsNone(await state.get_state())

    async def test_cancel_clears_state_and_returns_to_features_panel(self):
        callback = _make_callback(self.owner_id, f"paycancel:{self.bot_id}")
        state = _make_fsm_context(self.owner_id)
        await state.set_state(manage_bots.PaymentConnectFlow.waiting_for_token)
        await state.update_data(bot_id=self.bot_id)

        await manage_bots.cb_payment_connect_cancel(callback, state)

        self.assertIsNone(await state.get_state())
        callback.message.answer.assert_awaited_once()


class PaymentConnectTokenTests(unittest.IsolatedAsyncioTestCase):
    owner_id = 555103

    async def asyncSetUp(self):
        self.bot_id = await create_bot_record_with_admins(
            name="payment_token_bot", description="test", token=FAKE_TOKEN,
            file_path="templates/inventory.py", admin_ids=[str(self.owner_id)],
        )
        self._owner_patcher = patch.object(manage_bots, "_is_owner", lambda uid: uid == self.owner_id)
        self._owner_patcher.start()
        # payment-connect handlers now go through _can_manage_bot (Stage 1
        # per-bot ownership), which closes over admin_manager's OWN
        # _is_owner — patch both consistently.
        self._admin_owner_patcher = patch.object(admin_manager, "_is_owner", lambda uid: uid == self.owner_id)
        self._admin_owner_patcher.start()

    async def asyncTearDown(self):
        self._admin_owner_patcher.stop()
        self._owner_patcher.stop()
        await delete_bot(self.bot_id)

    async def test_valid_token_is_saved_and_advances_to_shop_id_step(self):
        message = _make_message(self.owner_id, VALID_PROVIDER_TOKEN)
        state = _make_fsm_context(self.owner_id)
        await state.set_state(manage_bots.PaymentConnectFlow.waiting_for_token)
        await state.update_data(bot_id=self.bot_id)

        await manage_bots.msg_payment_connect_token(message, state)

        saved = await get_bot_payment_provider(self.bot_id)
        self.assertEqual(saved, VALID_PROVIDER_TOKEN)
        # provider_token success now offers the optional (а)/(б) shopId/secret key
        # step instead of clearing immediately — skipping it (payskip:) still
        # ends the wizard exactly like before.
        self.assertEqual(await state.get_state(), manage_bots.PaymentConnectFlow.waiting_for_shop_id.state)

    async def test_live_token_is_also_accepted(self):
        live_token = "381764678:LIVE:abcXYZ123"
        message = _make_message(self.owner_id, live_token)
        state = _make_fsm_context(self.owner_id)
        await state.set_state(manage_bots.PaymentConnectFlow.waiting_for_token)
        await state.update_data(bot_id=self.bot_id)

        await manage_bots.msg_payment_connect_token(message, state)

        self.assertEqual(await get_bot_payment_provider(self.bot_id), live_token)

    async def test_malformed_token_is_rejected_and_state_kept_for_retry(self):
        message = _make_message(self.owner_id, "not-a-real-token")
        state = _make_fsm_context(self.owner_id)
        await state.set_state(manage_bots.PaymentConnectFlow.waiting_for_token)
        await state.update_data(bot_id=self.bot_id)

        await manage_bots.msg_payment_connect_token(message, state)

        self.assertIsNone(await get_bot_payment_provider(self.bot_id))
        message.answer.assert_awaited_once()
        self.assertIn("не похоже", message.answer.call_args[0][0].lower())
        self.assertEqual(await state.get_state(), manage_bots.PaymentConnectFlow.waiting_for_token.state)

    async def test_token_missing_mode_segment_is_rejected(self):
        message = _make_message(self.owner_id, "381764678:abcXYZ123")
        state = _make_fsm_context(self.owner_id)
        await state.set_state(manage_bots.PaymentConnectFlow.waiting_for_token)
        await state.update_data(bot_id=self.bot_id)

        await manage_bots.msg_payment_connect_token(message, state)

        self.assertIsNone(await get_bot_payment_provider(self.bot_id))

    async def test_non_owner_message_is_ignored(self):
        message = _make_message(999999, VALID_PROVIDER_TOKEN)
        state = _make_fsm_context(999999)
        await state.set_state(manage_bots.PaymentConnectFlow.waiting_for_token)
        await state.update_data(bot_id=self.bot_id)

        await manage_bots.msg_payment_connect_token(message, state)

        self.assertIsNone(await get_bot_payment_provider(self.bot_id))
        message.answer.assert_not_awaited()


class PaymentStepScreenshotTests(unittest.IsolatedAsyncioTestCase):
    """_show_payment_step: screenshot-present -> photo+caption via delete+resend;
    screenshot-missing/placeholder -> plain-text fallback via _edit_or_resend,
    exactly like before the screenshots feature existed. Real screenshot files
    are supplied by the project owner separately (see _PAYMENT_STEP_SCREENSHOTS
    docstring) — these tests only prove the wizard degrades gracefully around
    whatever is or isn't on disk, using a throwaway placeholder PNG."""
    owner_id = 555104

    async def asyncSetUp(self):
        self.bot_id = await create_bot_record_with_admins(
            name="payment_screenshot_bot", description="test", token=FAKE_TOKEN,
            file_path="templates/inventory.py", admin_ids=[str(self.owner_id)],
        )
        self._owner_patcher = patch.object(manage_bots, "_is_owner", lambda uid: uid == self.owner_id)
        self._owner_patcher.start()
        # payment-connect handlers now go through _can_manage_bot (Stage 1
        # per-bot ownership), which closes over admin_manager's OWN
        # _is_owner — patch both consistently.
        self._admin_owner_patcher = patch.object(admin_manager, "_is_owner", lambda uid: uid == self.owner_id)
        self._admin_owner_patcher.start()
        self._tmpdir = tempfile.TemporaryDirectory()
        self._placeholder_path = Path(self._tmpdir.name) / "step2_placeholder.png"
        self._placeholder_path.write_bytes(b"\x89PNG\r\n\x1a\n")  # minimal PNG magic bytes, not a real image

    async def asyncTearDown(self):
        self._admin_owner_patcher.stop()
        self._owner_patcher.stop()
        self._tmpdir.cleanup()
        await delete_bot(self.bot_id)

    async def test_step_with_existing_screenshot_sends_photo_and_deletes_old_card(self):
        callback = _make_callback(self.owner_id, f"paystep:{self.bot_id}:2")
        state = _make_fsm_context(self.owner_id)
        with patch.dict(manage_bots._PAYMENT_STEP_SCREENSHOTS, {2: self._placeholder_path}):
            await manage_bots.cb_payment_connect_step(callback, state)

        callback.message.delete.assert_awaited_once()
        callback.message.answer_photo.assert_awaited_once()
        callback.message.edit_text.assert_not_awaited()
        caption = callback.message.answer_photo.call_args.kwargs["caption"]
        self.assertIn("Шаг 2 из 4", caption)

    async def test_step_with_missing_screenshot_falls_back_to_text(self):
        missing_path = Path(self._tmpdir.name) / "does_not_exist.png"
        callback = _make_callback(self.owner_id, f"paystep:{self.bot_id}:2")
        state = _make_fsm_context(self.owner_id)
        with patch.dict(manage_bots._PAYMENT_STEP_SCREENSHOTS, {2: missing_path}):
            await manage_bots.cb_payment_connect_step(callback, state)

        callback.message.answer_photo.assert_not_awaited()
        callback.message.delete.assert_not_awaited()
        callback.message.edit_text.assert_awaited_once()
        text = callback.message.edit_text.call_args[0][0]
        self.assertIn("Шаг 2 из 4", text)

    async def test_step_without_configured_screenshot_uses_text_as_before(self):
        # Step 1 has no entry in _PAYMENT_STEP_SCREENSHOTS at all.
        callback = _make_callback(self.owner_id, f"paystart:{self.bot_id}")
        state = _make_fsm_context(self.owner_id)

        await manage_bots.cb_payment_connect_start(callback, state)

        callback.message.answer_photo.assert_not_awaited()
        callback.message.edit_text.assert_awaited_once()

    async def test_delete_failure_on_stale_card_does_not_crash_the_step(self):
        from aiogram.exceptions import TelegramBadRequest

        callback = _make_callback(self.owner_id, f"paystep:{self.bot_id}:2")
        callback.message.delete = AsyncMock(
            side_effect=TelegramBadRequest(method=MagicMock(), message="message to delete not found")
        )
        state = _make_fsm_context(self.owner_id)
        with patch.dict(manage_bots._PAYMENT_STEP_SCREENSHOTS, {2: self._placeholder_path}):
            await manage_bots.cb_payment_connect_step(callback, state)

        callback.message.answer_photo.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
