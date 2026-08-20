"""handlers/manage_bots.py's preview-gated fixbug flow (Telegram) and its
web-API counterpart in runtime/factory_analytics_api.py — both now split
generate (fix_bot_code + explain_bug_fix_diff, no disk write) from apply
(main_code_hash freshness check + write + restart), the same shape
tests/test_custom_features_handler.py already exercises for custom_features.

Handlers are called directly, not through a real Dispatcher — same
convention as test_custom_features_handler.py and test_manage_bots_features.py.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import handlers.admin_manager as admin_manager_module
import handlers.manage_bots as mb_module
from db.database import create_bot_record_with_admins, delete_bot

FAKE_TOKEN = "123456789:AAHfakeTokenButShapedRight1234567890"

_ORIGINAL_CODE = "# original bot code\nimport asyncio\nasyncio.run(None)\n"
_FIXED_CODE = "# fixed bot code\nimport asyncio\nasyncio.run(None)\n"


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
    status = MagicMock()
    status.delete = AsyncMock()
    message.answer = AsyncMock(return_value=status)
    return message


def _make_fsm_context(user_id: int) -> FSMContext:
    storage = MemoryStorage()
    key = StorageKey(bot_id=0, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=storage, key=key)


class _FixBugHandlerTestBase(unittest.IsolatedAsyncioTestCase):
    owner_id = 0  # overridden per subclass so parallel tests don't share an owner id

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.bot_file = Path(self._tmp.name) / "fake_bot.py"
        self.bot_file.write_text(_ORIGINAL_CODE, encoding="utf-8")
        self.bot_id = await create_bot_record_with_admins(
            name=f"fixbug_handler_bot_{self.owner_id}", description="test", token=FAKE_TOKEN,
            file_path=str(self.bot_file), admin_ids=[str(self.owner_id)],
        )
        self._owner_patcher = patch.object(admin_manager_module, "_is_owner", lambda uid: uid == self.owner_id)
        self._owner_patcher.start()

        self._start_bot_patcher = patch.object(mb_module, "start_bot", AsyncMock(return_value=123))
        self._start_bot_patcher.start()
        self._stop_bot_patcher = patch.object(mb_module, "stop_bot", AsyncMock())
        self._stop_bot_patcher.start()
        self._push_patcher = patch.object(mb_module, "push_bot_to_github", AsyncMock())
        self._push_patcher.start()

    async def asyncTearDown(self):
        self._push_patcher.stop()
        self._stop_bot_patcher.stop()
        self._start_bot_patcher.stop()
        self._owner_patcher.stop()
        mb_module._busy_bots.discard(self.bot_id)
        await delete_bot(self.bot_id)
        self._tmp.cleanup()


class NonOwnerCannotApplyFixTests(_FixBugHandlerTestBase):
    owner_id = 700001

    async def test_non_owner_apply_is_denied_and_writes_nothing(self):
        non_owner = 999101
        state = _make_fsm_context(non_owner)
        callback = _make_callback(non_owner, f"applyfix:{self.bot_id}")
        await mb_module.cb_apply_fix(callback, state)
        self.assertEqual(self.bot_file.read_text(encoding="utf-8"), _ORIGINAL_CODE)


class HappyPathPreviewThenApplyTests(_FixBugHandlerTestBase):
    owner_id = 700002

    async def test_generate_stores_preview_without_writing(self):
        state = _make_fsm_context(self.owner_id)
        await state.set_state(mb_module.FixBotStates.describing_bug)
        await state.update_data(fix_bot_id=self.bot_id)

        message = _make_message(self.owner_id, "кнопка не работает")
        with patch.object(mb_module, "fix_bot_code", AsyncMock(return_value=_FIXED_CODE)), \
             patch.object(mb_module, "explain_bug_fix_diff", AsyncMock(return_value="Кнопка починена.")):
            await mb_module._generate_and_preview_fix(message, state, message.text)

        # Nothing written to disk yet — this is the whole point of the split.
        self.assertEqual(self.bot_file.read_text(encoding="utf-8"), _ORIGINAL_CODE)
        data = await state.get_data()
        self.assertEqual(data["fix_pending_code"], _FIXED_CODE)
        self.assertEqual(
            data["fix_main_code_hash"], mb_module._hash_bot_code(_ORIGINAL_CODE),
        )
        self.assertEqual(await state.get_state(), mb_module.FixBotStates.previewing_fix.state)

    async def test_apply_writes_fixed_code_and_restarts(self):
        state = _make_fsm_context(self.owner_id)
        await state.set_state(mb_module.FixBotStates.describing_bug)
        await state.update_data(fix_bot_id=self.bot_id)
        message = _make_message(self.owner_id, "кнопка не работает")
        with patch.object(mb_module, "fix_bot_code", AsyncMock(return_value=_FIXED_CODE)), \
             patch.object(mb_module, "explain_bug_fix_diff", AsyncMock(return_value="Кнопка починена.")):
            await mb_module._generate_and_preview_fix(message, state, message.text)

        apply_callback = _make_callback(self.owner_id, f"applyfix:{self.bot_id}")
        await mb_module.cb_apply_fix(apply_callback, state)

        self.assertEqual(self.bot_file.read_text(encoding="utf-8"), _FIXED_CODE)
        mb_module.start_bot.assert_awaited_once()
        self.assertIsNone(await state.get_state())

    async def test_cancel_leaves_file_untouched(self):
        state = _make_fsm_context(self.owner_id)
        await state.set_state(mb_module.FixBotStates.describing_bug)
        await state.update_data(fix_bot_id=self.bot_id)
        message = _make_message(self.owner_id, "кнопка не работает")
        with patch.object(mb_module, "fix_bot_code", AsyncMock(return_value=_FIXED_CODE)), \
             patch.object(mb_module, "explain_bug_fix_diff", AsyncMock(return_value="Кнопка починена.")):
            await mb_module._generate_and_preview_fix(message, state, message.text)

        cancel_callback = _make_callback(self.owner_id, f"cancelfix:{self.bot_id}")
        await mb_module.cb_cancel_fix(cancel_callback, state)

        self.assertEqual(self.bot_file.read_text(encoding="utf-8"), _ORIGINAL_CODE)
        self.assertIsNone(await state.get_state())
        mb_module.start_bot.assert_not_awaited()


class StaleSourceRejectedAtApplyTests(_FixBugHandlerTestBase):
    """The freshness guard — a fix generated against one version of the file
    must not be applied if the file changed underneath the preview (e.g. a
    concurrent /recreate), same scenario custom_features.py's
    cf_main_code_hash check covers."""

    owner_id = 700003

    async def test_stale_hash_blocks_apply_and_writes_nothing(self):
        state = _make_fsm_context(self.owner_id)
        await state.set_state(mb_module.FixBotStates.describing_bug)
        await state.update_data(fix_bot_id=self.bot_id)
        message = _make_message(self.owner_id, "кнопка не работает")
        with patch.object(mb_module, "fix_bot_code", AsyncMock(return_value=_FIXED_CODE)), \
             patch.object(mb_module, "explain_bug_fix_diff", AsyncMock(return_value="Кнопка починена.")):
            await mb_module._generate_and_preview_fix(message, state, message.text)

        # Simulate a concurrent recreate/second-fix changing the file while
        # this preview sat waiting for the owner's tap.
        self.bot_file.write_text("# changed by a concurrent operation\n", encoding="utf-8")

        apply_callback = _make_callback(self.owner_id, f"applyfix:{self.bot_id}")
        await mb_module.cb_apply_fix(apply_callback, state)

        self.assertEqual(self.bot_file.read_text(encoding="utf-8"), "# changed by a concurrent operation\n")
        mb_module.start_bot.assert_not_awaited()


class WebApiGenerateApplySplitTests(_FixBugHandlerTestBase):
    """generate_fix_preview_core / apply_fix_core — the headless pair
    runtime/factory_analytics_api.py's fixbug_preview_handler /
    fixbug_apply_handler call."""

    owner_id = 700004

    async def test_generate_preview_core_returns_fixed_code_without_writing(self):
        with patch.object(mb_module, "fix_bot_code", AsyncMock(return_value=_FIXED_CODE)), \
             patch.object(mb_module, "explain_bug_fix_diff", AsyncMock(return_value="explained")):
            result = await mb_module.generate_fix_preview_core(self.bot_id, "кнопка не работает")

        self.assertTrue(result["ok"])
        self.assertEqual(result["fixed_code"], _FIXED_CODE)
        self.assertEqual(result["main_code_hash"], mb_module._hash_bot_code(_ORIGINAL_CODE))
        self.assertEqual(self.bot_file.read_text(encoding="utf-8"), _ORIGINAL_CODE)

    async def test_apply_core_rejects_stale_hash(self):
        with patch.object(mb_module, "fix_bot_code", AsyncMock(return_value=_FIXED_CODE)), \
             patch.object(mb_module, "explain_bug_fix_diff", AsyncMock(return_value="explained")):
            preview = await mb_module.generate_fix_preview_core(self.bot_id, "кнопка не работает")

        self.bot_file.write_text("# changed concurrently\n", encoding="utf-8")

        result = await mb_module.apply_fix_core(self.bot_id, preview["fixed_code"], preview["main_code_hash"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "stale_source")
        self.assertEqual(self.bot_file.read_text(encoding="utf-8"), "# changed concurrently\n")

    async def test_apply_core_writes_and_restarts_on_fresh_hash(self):
        with patch.object(mb_module, "fix_bot_code", AsyncMock(return_value=_FIXED_CODE)), \
             patch.object(mb_module, "explain_bug_fix_diff", AsyncMock(return_value="explained")):
            preview = await mb_module.generate_fix_preview_core(self.bot_id, "кнопка не работает")

        result = await mb_module.apply_fix_core(self.bot_id, preview["fixed_code"], preview["main_code_hash"])
        self.assertTrue(result["ok"])
        self.assertEqual(self.bot_file.read_text(encoding="utf-8"), _FIXED_CODE)
        mb_module.start_bot.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
