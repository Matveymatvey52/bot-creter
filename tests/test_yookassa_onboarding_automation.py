"""Tests for the ЮKassa onboarding automation added on top of PaymentConnectFlow:
(а) GET /me auto-fetch of shop details via shop_id+secret_key, (б) owner-level
credential reuse + copy-paste BotFather block for connecting the same shop to
another bot, (в) on-demand "Проверить статус" status-check button and the
background poller in runtime/payment_status_poller.py.

Telegram's Bot API still has no way to set provider_token programmatically — all
of this sits on top of, not instead of, the existing manual BotFather step; see
tests/test_payment_connect_flow.py for that baseline.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import db.database as db_module
import handlers.manage_bots as manage_bots
from db.database import (
    create_bot_record_with_admins,
    delete_bot,
    get_all_bot_ids_with_yookassa_credentials,
    get_bot_yookassa_credentials,
    get_bot_yookassa_status_cache,
    get_owner_payment_credentials,
    set_bot_payment_provider,
    set_bot_yookassa_credentials,
    set_owner_payment_credentials,
)
from services.yookassa_api import YooKassaAuthError

FAKE_TOKEN = "123456789:AAHfakeTokenButShapedRight1234567890"
VALID_PROVIDER_TOKEN = "381764678:TEST:abcXYZ123"

FAKE_ME_RESPONSE = {
    "account_id": "123456",
    "status": "enabled",
    "test": True,
    "payment_methods": ["bank_card", "sbp"],
    "fiscalization": {"enabled": False},
}


def _make_callback(user_id: int, data: str) -> MagicMock:
    callback = MagicMock()
    callback.from_user.id = user_id
    callback.data = data
    callback.answer = AsyncMock()
    callback.message.edit_text = AsyncMock()
    callback.message.answer = AsyncMock()
    callback.message.edit_reply_markup = AsyncMock()
    callback.message.from_user.id = user_id
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


class YooKassaAutoFetchTests(unittest.IsolatedAsyncioTestCase):
    """(а) shop_id/secret_key collection → GET /me → saved + cached."""

    owner_id = 555201

    async def asyncSetUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._db_path_patcher = patch.object(
            db_module, "DB_PATH", Path(self._tmp_dir.name) / "test_yookassa_autofetch.db"
        )
        self._db_path_patcher.start()
        await db_module.init_db()
        self.bot_id = await create_bot_record_with_admins(
            name="yk_autofetch_bot", description="test", token=FAKE_TOKEN,
            file_path="templates/inventory.py", admin_ids=[str(self.owner_id)],
        )
        await set_bot_payment_provider(self.bot_id, VALID_PROVIDER_TOKEN)
        self._owner_patcher = patch.object(manage_bots, "_is_owner", lambda uid: uid == self.owner_id)
        self._owner_patcher.start()

    async def asyncTearDown(self):
        self._owner_patcher.stop()
        await delete_bot(self.bot_id)
        self._db_path_patcher.stop()
        self._tmp_dir.cleanup()

    async def test_valid_shop_id_advances_to_secret_key_step(self):
        message = _make_message(self.owner_id, "123456")
        state = _make_fsm_context(self.owner_id)
        await state.set_state(manage_bots.PaymentConnectFlow.waiting_for_shop_id)
        await state.update_data(bot_id=self.bot_id)

        await manage_bots.msg_payment_shop_id(message, state)

        self.assertEqual(await state.get_state(), manage_bots.PaymentConnectFlow.waiting_for_secret_key.state)
        data = await state.get_data()
        self.assertEqual(data.get("shop_id"), "123456")

    async def test_non_numeric_shop_id_is_rejected(self):
        message = _make_message(self.owner_id, "not-a-shop-id")
        state = _make_fsm_context(self.owner_id)
        await state.set_state(manage_bots.PaymentConnectFlow.waiting_for_shop_id)
        await state.update_data(bot_id=self.bot_id)

        await manage_bots.msg_payment_shop_id(message, state)

        self.assertEqual(await state.get_state(), manage_bots.PaymentConnectFlow.waiting_for_shop_id.state)
        message.answer.assert_awaited_once()

    async def test_valid_secret_key_fetches_and_saves_credentials(self):
        message = _make_message(self.owner_id, "live_secretkey123")
        state = _make_fsm_context(self.owner_id)
        await state.set_state(manage_bots.PaymentConnectFlow.waiting_for_secret_key)
        await state.update_data(bot_id=self.bot_id, shop_id="123456")

        with patch("handlers.manage_bots.fetch_shop_info", new=AsyncMock(return_value=FAKE_ME_RESPONSE)):
            await manage_bots.msg_payment_secret_key(message, state)

        creds = await get_bot_yookassa_credentials(self.bot_id)
        self.assertEqual(creds, ("123456", "live_secretkey123"))
        owner_creds = await get_owner_payment_credentials(self.owner_id)
        self.assertEqual(owner_creds, ("123456", "live_secretkey123"))
        cache = await get_bot_yookassa_status_cache(self.bot_id)
        self.assertEqual(cache["last_status"], "enabled")
        self.assertIsNone(await state.get_state())

    async def test_rejected_credentials_do_not_save_anything(self):
        message = _make_message(self.owner_id, "bad_secret")
        state = _make_fsm_context(self.owner_id)
        await state.set_state(manage_bots.PaymentConnectFlow.waiting_for_secret_key)
        await state.update_data(bot_id=self.bot_id, shop_id="123456")

        with patch("handlers.manage_bots.fetch_shop_info", new=AsyncMock(side_effect=YooKassaAuthError("bad"))):
            await manage_bots.msg_payment_secret_key(message, state)

        self.assertIsNone(await get_bot_yookassa_credentials(self.bot_id))

    async def test_network_error_does_not_save_and_does_not_crash(self):
        message = _make_message(self.owner_id, "some_secret")
        state = _make_fsm_context(self.owner_id)
        await state.set_state(manage_bots.PaymentConnectFlow.waiting_for_secret_key)
        await state.update_data(bot_id=self.bot_id, shop_id="123456")

        with patch("handlers.manage_bots.fetch_shop_info", new=AsyncMock(side_effect=RuntimeError("network down"))):
            await manage_bots.msg_payment_secret_key(message, state)

        self.assertIsNone(await get_bot_yookassa_credentials(self.bot_id))

    async def test_skip_button_clears_state_without_error(self):
        callback = _make_callback(self.owner_id, f"payskip:{self.bot_id}")
        state = _make_fsm_context(self.owner_id)
        await state.set_state(manage_bots.PaymentConnectFlow.waiting_for_shop_id)
        await state.update_data(bot_id=self.bot_id)

        await manage_bots.cb_payment_details_skip(callback, state)

        self.assertIsNone(await state.get_state())


class YooKassaOwnerReuseTests(unittest.IsolatedAsyncioTestCase):
    """(б) owner-level shop_id/secret_key reuse across bots + copy-paste block."""

    owner_id = 555202

    async def asyncSetUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._db_path_patcher = patch.object(
            db_module, "DB_PATH", Path(self._tmp_dir.name) / "test_yookassa_owner_reuse.db"
        )
        self._db_path_patcher.start()
        await db_module.init_db()
        self.bot_a = await create_bot_record_with_admins(
            name="yk_owner_bot_a", description="test", token=FAKE_TOKEN,
            file_path="templates/inventory.py", admin_ids=[str(self.owner_id)],
        )
        self.bot_b = await create_bot_record_with_admins(
            name="yk_owner_bot_b", description="test", token=FAKE_TOKEN,
            file_path="templates/inventory.py", admin_ids=[str(self.owner_id)],
        )
        await set_bot_payment_provider(self.bot_a, VALID_PROVIDER_TOKEN)
        await set_owner_payment_credentials(self.owner_id, "999", "live_ownersecret")
        self._owner_patcher = patch.object(manage_bots, "_is_owner", lambda uid: uid == self.owner_id)
        self._owner_patcher.start()

    async def asyncTearDown(self):
        self._owner_patcher.stop()
        await delete_bot(self.bot_a)
        await delete_bot(self.bot_b)
        self._db_path_patcher.stop()
        self._tmp_dir.cleanup()

    async def test_reuse_button_applies_saved_credentials_without_reasking(self):
        callback = _make_callback(self.owner_id, f"payreuse:{self.bot_a}")
        state = _make_fsm_context(self.owner_id)

        with patch("handlers.manage_bots.fetch_shop_info", new=AsyncMock(return_value=FAKE_ME_RESPONSE)):
            await manage_bots.cb_payment_details_reuse(callback, state)

        creds = await get_bot_yookassa_credentials(self.bot_a)
        self.assertEqual(creds, ("999", "live_ownersecret"))

    async def test_multi_bot_offer_lists_other_bot_without_payment_yet(self):
        message = _make_message(self.owner_id, "")

        await manage_bots._offer_multi_bot_connect(message, self.bot_a)

        message.answer.assert_awaited_once()
        markup = message.answer.call_args[1]["reply_markup"]
        callback_datas = [btn.callback_data for row in markup.inline_keyboard for btn in row]
        self.assertTrue(any(cd == f"paymulti:{self.bot_a}:{self.bot_b}" for cd in callback_datas))

    async def test_multi_bot_offer_skips_bots_that_already_have_payment(self):
        await set_bot_payment_provider(self.bot_b, VALID_PROVIDER_TOKEN)
        message = _make_message(self.owner_id, "")

        await manage_bots._offer_multi_bot_connect(message, self.bot_a)

        message.answer.assert_not_awaited()

    async def test_paymulti_callback_sends_copy_paste_block_with_credentials(self):
        callback = _make_callback(self.owner_id, f"paymulti:{self.bot_a}:{self.bot_b}")

        await manage_bots.cb_payment_multi_connect(callback)

        callback.message.answer.assert_awaited_once()
        text = callback.message.answer.call_args[0][0]
        self.assertIn("999", text)
        self.assertIn("live_ownersecret", text)
        self.assertIn("BotFather", text)


class YooKassaStatusCheckTests(unittest.IsolatedAsyncioTestCase):
    """(в) on-demand status-check button + poller's per-bot query surface."""

    owner_id = 555203

    async def asyncSetUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._db_path_patcher = patch.object(
            db_module, "DB_PATH", Path(self._tmp_dir.name) / "test_yookassa_status_check.db"
        )
        self._db_path_patcher.start()
        await db_module.init_db()
        self.bot_id = await create_bot_record_with_admins(
            name="yk_status_bot", description="test", token=FAKE_TOKEN,
            file_path="templates/inventory.py", admin_ids=[str(self.owner_id)],
        )
        await set_bot_payment_provider(self.bot_id, VALID_PROVIDER_TOKEN)
        self._owner_patcher = patch.object(manage_bots, "_is_owner", lambda uid: uid == self.owner_id)
        self._owner_patcher.start()

    async def asyncTearDown(self):
        self._owner_patcher.stop()
        await delete_bot(self.bot_id)
        self._db_path_patcher.stop()
        self._tmp_dir.cleanup()

    async def test_check_status_without_saved_credentials_prompts_to_connect_first(self):
        callback = _make_callback(self.owner_id, f"paycheck:{self.bot_id}")

        await manage_bots.cb_payment_check_status(callback)

        callback.message.answer.assert_awaited_once()
        text = callback.message.answer.call_args[0][0]
        self.assertIn("не сохранены", text.lower())

    async def test_check_status_refreshes_cache_and_reports(self):
        await set_bot_yookassa_credentials(self.bot_id, "111", "live_x")
        callback = _make_callback(self.owner_id, f"paycheck:{self.bot_id}")

        with patch("handlers.manage_bots.fetch_shop_info", new=AsyncMock(return_value=FAKE_ME_RESPONSE)):
            await manage_bots.cb_payment_check_status(callback)

        cache = await get_bot_yookassa_status_cache(self.bot_id)
        self.assertEqual(cache["last_status"], "enabled")
        text = callback.message.answer.call_args[0][0]
        self.assertIn("enabled", text)

    async def test_check_status_handles_auth_error_gracefully(self):
        await set_bot_yookassa_credentials(self.bot_id, "111", "live_x")
        callback = _make_callback(self.owner_id, f"paycheck:{self.bot_id}")

        with patch("handlers.manage_bots.fetch_shop_info", new=AsyncMock(side_effect=YooKassaAuthError("bad"))):
            await manage_bots.cb_payment_check_status(callback)

        callback.message.answer.assert_awaited_once()

    async def test_poller_lists_only_bots_with_credentials(self):
        before = set(await get_all_bot_ids_with_yookassa_credentials())
        self.assertNotIn(self.bot_id, before)

        await set_bot_yookassa_credentials(self.bot_id, "222", "live_y")

        after = set(await get_all_bot_ids_with_yookassa_credentials())
        self.assertIn(self.bot_id, after)


class PaymentStatusPollerTests(unittest.IsolatedAsyncioTestCase):
    owner_id = 555204

    async def asyncSetUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._db_path_patcher = patch.object(
            db_module, "DB_PATH", Path(self._tmp_dir.name) / "test_yookassa_poller.db"
        )
        self._db_path_patcher.start()
        await db_module.init_db()
        self.bot_id = await create_bot_record_with_admins(
            name="yk_poller_bot", description="test", token=FAKE_TOKEN,
            file_path="templates/inventory.py", admin_ids=[str(self.owner_id)],
        )
        await set_bot_payment_provider(self.bot_id, VALID_PROVIDER_TOKEN)
        await set_bot_yookassa_credentials(self.bot_id, "333", "live_z")

    async def asyncTearDown(self):
        await delete_bot(self.bot_id)
        self._db_path_patcher.stop()
        self._tmp_dir.cleanup()

    async def test_poll_once_updates_cache_for_credentialed_bots(self):
        from runtime.payment_status_poller import _poll_once

        with patch("runtime.payment_status_poller.fetch_shop_info", new=AsyncMock(return_value=FAKE_ME_RESPONSE)):
            await _poll_once()

        cache = await get_bot_yookassa_status_cache(self.bot_id)
        self.assertEqual(cache["last_status"], "enabled")

    async def test_poll_once_survives_one_bot_erroring(self):
        from runtime.payment_status_poller import _poll_once

        with patch("runtime.payment_status_poller.fetch_shop_info", new=AsyncMock(side_effect=RuntimeError("down"))):
            await _poll_once()  # must not raise


if __name__ == "__main__":
    unittest.main()
