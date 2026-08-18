"""Stage 1 multitenancy rollout: per-bot ownership authorization.

Covers:
  - handlers/admin_manager.py's _can_manage_bot (owner / owning customer /
    non-owner customer)
  - db/database.py's add_office_link cross-owner rejection
  - a couple of representative handlers/manage_bots.py callbacks exercised
    as owner / owning customer / non-owner customer, to lock in the
    _can_manage_bot wiring end to end.

Uses real bots.db rows via create_bot_record_with_admins (same convention as
tests/test_manage_bots_office.py) since these tests care specifically about
bots.owner_telegram_id, which isolated_db's channel_monitor-focused fixture
doesn't touch.

Run with: python -m pytest tests/test_multitenancy_stage1.py
"""
from __future__ import annotations

import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import handlers.admin_manager as admin_manager
import handlers.manage_bots as manage_bots
from db.database import (
    add_office_link,
    create_bot_record_with_admins,
    delete_bot,
    get_office_links_for_bot,
    init_db,
)

FAKE_TOKEN = "123456789:AAHfakeTokenButShapedRight1234567890"


class CanManageBotTests(unittest.TestCase):
    def test_owner_can_manage_any_bot(self):
        with patch.object(admin_manager, "OWNER_ID", 555):
            self.assertTrue(admin_manager._can_manage_bot(555, {"owner_telegram_id": 111}))
            self.assertTrue(admin_manager._can_manage_bot(555, {"owner_telegram_id": None}))

    def test_owning_customer_can_manage_own_bot(self):
        with patch.object(admin_manager, "OWNER_ID", 555):
            self.assertTrue(admin_manager._can_manage_bot(111, {"owner_telegram_id": 111}))

    def test_non_owner_customer_cannot_manage_others_bot(self):
        with patch.object(admin_manager, "OWNER_ID", 555):
            self.assertFalse(admin_manager._can_manage_bot(111, {"owner_telegram_id": 222}))

    def test_customer_cannot_manage_unowned_bot(self):
        """A bot with owner_telegram_id=None (shouldn't happen post-backfill,
        but defensive) is manageable only by the system owner, never by an
        arbitrary customer."""
        with patch.object(admin_manager, "OWNER_ID", 555):
            self.assertFalse(admin_manager._can_manage_bot(111, {"owner_telegram_id": None}))


def _make_callback(user_id: int, data: str) -> MagicMock:
    callback = MagicMock()
    callback.from_user.id = user_id
    callback.data = data
    callback.answer = AsyncMock()
    callback.message.edit_text = AsyncMock()
    callback.message.answer = AsyncMock()
    callback.message.delete = AsyncMock()
    callback.bot.send_message = AsyncMock()
    return callback


class AddOfficeLinkOwnershipTests(unittest.IsolatedAsyncioTestCase):
    OWNER_ID = 730100

    async def asyncSetUp(self):
        await init_db()
        self._owner_id_patcher = patch.dict(os.environ, {"OWNER_ID": str(self.OWNER_ID)})
        self._owner_id_patcher.start()
        self.owned_a = await create_bot_record_with_admins(
            name="mt_owned_a", description="t", token=FAKE_TOKEN,
            file_path="templates/tour_operator.py", admin_ids=[], owner_telegram_id=111,
        )
        self.owned_b = await create_bot_record_with_admins(
            name="mt_owned_b", description="t", token=FAKE_TOKEN,
            file_path="templates/tour_operator.py", admin_ids=[], owner_telegram_id=111,
        )
        self.others_bot = await create_bot_record_with_admins(
            name="mt_others_bot", description="t", token=FAKE_TOKEN,
            file_path="templates/tour_operator.py", admin_ids=[], owner_telegram_id=222,
        )
        self.system_owner_bot = await create_bot_record_with_admins(
            name="mt_system_owner_bot", description="t", token=FAKE_TOKEN,
            file_path="templates/tour_operator.py", admin_ids=[], owner_telegram_id=self.OWNER_ID,
        )

    async def asyncTearDown(self):
        self._owner_id_patcher.stop()
        for bot_id in (self.owned_a, self.owned_b, self.others_bot, self.system_owner_bot):
            await delete_bot(bot_id)

    async def test_same_owner_link_is_created(self):
        linked = await add_office_link(self.owned_a, self.owned_b, "order.created")
        self.assertTrue(linked)
        links = await get_office_links_for_bot(self.owned_a)
        self.assertEqual(len(links), 1)

    async def test_cross_owner_link_is_rejected(self):
        linked = await add_office_link(self.owned_a, self.others_bot, "order.created")
        self.assertFalse(linked)
        self.assertEqual(await get_office_links_for_bot(self.owned_a), [])

    async def test_system_owner_bot_can_bridge_to_customer_bot(self):
        linked = await add_office_link(self.system_owner_bot, self.owned_a, "order.created")
        self.assertTrue(linked)
        links = await get_office_links_for_bot(self.system_owner_bot)
        self.assertEqual(len(links), 1)


class ManageBotsHandlerOwnershipTests(unittest.IsolatedAsyncioTestCase):
    """cb_info exercised as owner / owning customer / non-owner customer —
    representative of the ~50 handlers converted from _is_owner to
    _can_manage_bot in handlers/manage_bots.py."""

    OWNER_ID = 730200
    CUSTOMER_ID = 111
    OTHER_CUSTOMER_ID = 222

    async def asyncSetUp(self):
        self._admin_owner_patcher = patch.object(
            admin_manager, "_is_owner", lambda uid: uid == self.OWNER_ID
        )
        self._admin_owner_patcher.start()
        self._mb_owner_patcher = patch.object(
            manage_bots, "_is_owner", lambda uid: uid == self.OWNER_ID
        )
        self._mb_owner_patcher.start()
        await init_db()
        self.customer_bot = await create_bot_record_with_admins(
            name="mt_handler_customer_bot", description="t", token=FAKE_TOKEN,
            file_path="templates/tour_operator.py", admin_ids=[], owner_telegram_id=self.CUSTOMER_ID,
        )

    async def asyncTearDown(self):
        self._mb_owner_patcher.stop()
        self._admin_owner_patcher.stop()
        await delete_bot(self.customer_bot)

    async def test_owner_can_view_customers_bot(self):
        callback = _make_callback(self.OWNER_ID, f"info:{self.customer_bot}")
        await manage_bots.cb_info(callback)
        callback.bot.send_message.assert_awaited_once()
        text = callback.bot.send_message.call_args.args[1]
        self.assertIn("mt_handler_customer_bot", text)

    async def test_owning_customer_can_view_own_bot(self):
        callback = _make_callback(self.CUSTOMER_ID, f"info:{self.customer_bot}")
        await manage_bots.cb_info(callback)
        callback.bot.send_message.assert_awaited_once()
        text = callback.bot.send_message.call_args.args[1]
        self.assertIn("mt_handler_customer_bot", text)

    async def test_non_owner_customer_cannot_view_others_bot(self):
        callback = _make_callback(self.OTHER_CUSTOMER_ID, f"info:{self.customer_bot}")
        await manage_bots.cb_info(callback)
        callback.bot.send_message.assert_awaited_once()
        text = callback.bot.send_message.call_args.args[1]
        # Same "не найден" message as an actually-missing bot_id — a
        # non-owner probing another customer's bot_id learns nothing beyond
        # "not found", never that the bot exists but isn't theirs.
        self.assertIn("не найден", text)
        self.assertNotIn("mt_handler_customer_bot", text)


if __name__ == "__main__":
    unittest.main()
