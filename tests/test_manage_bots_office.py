"""Creator-bot "🏢 Офисы" UI (handlers/manage_bots.py's office:/officeconnect:/
officelink:/officeunlink: callbacks), per docs/OFFICES_DESIGN.md §9.

Same conventions as tests/test_manage_bots_features.py: real bots.db rows via
create_bot_record_with_admins, _is_owner patched per test class, MagicMock
callbacks with AsyncMock answer()/message methods.
"""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import handlers.manage_bots as manage_bots
from db.database import (
    add_office_link,
    create_bot_record_with_admins,
    delete_bot,
    get_office_links_for_bot,
)

FAKE_TOKEN = "123456789:AAHfakeTokenButShapedRight1234567890"


def _make_callback(user_id: int, data: str) -> MagicMock:
    callback = MagicMock()
    callback.from_user.id = user_id
    callback.data = data
    callback.answer = AsyncMock()
    callback.message.edit_text = AsyncMock()
    callback.message.answer = AsyncMock()
    return callback


class _OfficeUiTestBase(unittest.IsolatedAsyncioTestCase):
    owner_id = 0  # overridden per subclass so parallel tests don't share an owner id

    async def asyncSetUp(self):
        self._owner_patcher = patch.object(manage_bots, "_is_owner", lambda uid: uid == self.owner_id)
        self._owner_patcher.start()
        self.bot_a = await create_bot_record_with_admins(
            name="office_ui_bot_a", description="test", token=FAKE_TOKEN,
            file_path="templates/tour_operator.py", admin_ids=[str(self.owner_id)],
        )
        self.bot_b = await create_bot_record_with_admins(
            name="office_ui_bot_b", description="test", token=FAKE_TOKEN,
            file_path="templates/event_rsvp.py", admin_ids=[str(self.owner_id)],
        )

    async def asyncTearDown(self):
        self._owner_patcher.stop()
        await delete_bot(self.bot_a)
        await delete_bot(self.bot_b)


class OfficePanelEmptyStateTests(_OfficeUiTestBase):
    owner_id = 525201

    async def test_panel_shows_no_links_by_default(self):
        callback = _make_callback(self.owner_id, f"office:{self.bot_a}")
        await manage_bots.cb_office_panel(callback)

        callback.answer.assert_awaited_once()
        callback.message.edit_text.assert_awaited_once()
        text = callback.message.edit_text.call_args.args[0]
        self.assertIn("Пока нет связей", text)


class OfficeConnectListsOtherBotsTests(_OfficeUiTestBase):
    owner_id = 525202

    async def test_connect_screen_lists_other_bots_not_self(self):
        callback = _make_callback(self.owner_id, f"officeconnect:{self.bot_a}")
        await manage_bots.cb_office_connect_start(callback)

        callback.message.edit_text.assert_awaited_once()
        _, kwargs = callback.message.edit_text.call_args
        button_texts = [
            btn.text for row in kwargs["reply_markup"].inline_keyboard for btn in row
        ]
        self.assertIn("office_ui_bot_b", button_texts)
        self.assertNotIn("office_ui_bot_a", button_texts)


class OfficeLinkCreatesLinkTests(_OfficeUiTestBase):
    owner_id = 525203

    async def test_officelink_creates_link_visible_in_db(self):
        callback = _make_callback(self.owner_id, f"officelink:{self.bot_a}:{self.bot_b}")
        await manage_bots.cb_office_link(callback)

        links = await get_office_links_for_bot(self.bot_a)
        self.assertEqual(links, [{
            "source_bot_id": self.bot_a, "target_bot_id": self.bot_b, "event_type": "order.created",
        }])
        callback.message.edit_text.assert_awaited_once()
        text = callback.message.edit_text.call_args.args[0]
        self.assertIn("office_ui_bot_a", text)
        self.assertIn("office_ui_bot_b", text)

    async def test_officelink_rejects_self_link(self):
        callback = _make_callback(self.owner_id, f"officelink:{self.bot_a}:{self.bot_a}")
        await manage_bots.cb_office_link(callback)

        self.assertEqual(await get_office_links_for_bot(self.bot_a), [])
        callback.message.answer.assert_awaited_once()
        text = callback.message.answer.call_args.args[0]
        self.assertIn("нельзя", text.lower())

    async def test_officelink_is_idempotent_via_add_office_link(self):
        callback1 = _make_callback(self.owner_id, f"officelink:{self.bot_a}:{self.bot_b}")
        await manage_bots.cb_office_link(callback1)
        callback2 = _make_callback(self.owner_id, f"officelink:{self.bot_a}:{self.bot_b}")
        await manage_bots.cb_office_link(callback2)

        links = await get_office_links_for_bot(self.bot_a)
        self.assertEqual(len(links), 1)


class OfficeUnlinkRemovesLinkTests(_OfficeUiTestBase):
    owner_id = 525204

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await add_office_link(self.bot_a, self.bot_b, "order.created")

    async def test_officeunlink_removes_link(self):
        callback = _make_callback(
            self.owner_id, f"officeunlink:{self.bot_a}:{self.bot_b}:order.created:{self.bot_a}"
        )
        await manage_bots.cb_office_unlink(callback)

        self.assertEqual(await get_office_links_for_bot(self.bot_a), [])
        callback.message.edit_text.assert_awaited_once()
        text = callback.message.edit_text.call_args.args[0]
        self.assertIn("Пока нет связей", text)

    async def test_panel_shows_existing_link_both_directions(self):
        callback_a = _make_callback(self.owner_id, f"office:{self.bot_a}")
        await manage_bots.cb_office_panel(callback_a)
        text_a = callback_a.message.edit_text.call_args.args[0]
        self.assertIn("📤", text_a)

        callback_b = _make_callback(self.owner_id, f"office:{self.bot_b}")
        await manage_bots.cb_office_panel(callback_b)
        text_b = callback_b.message.edit_text.call_args.args[0]
        self.assertIn("📥", text_b)


class OfficeUiDeniesNonOwnerTests(_OfficeUiTestBase):
    owner_id = 525205

    async def test_non_owner_cannot_view_office_panel(self):
        NON_OWNER_ID = 999999
        callback = _make_callback(NON_OWNER_ID, f"office:{self.bot_a}")
        await manage_bots.cb_office_panel(callback)

        callback.message.edit_text.assert_not_awaited()

    async def test_non_owner_cannot_create_link(self):
        NON_OWNER_ID = 999999
        callback = _make_callback(NON_OWNER_ID, f"officelink:{self.bot_a}:{self.bot_b}")
        await manage_bots.cb_office_link(callback)

        self.assertEqual(await get_office_links_for_bot(self.bot_a), [])


if __name__ == "__main__":
    unittest.main()
