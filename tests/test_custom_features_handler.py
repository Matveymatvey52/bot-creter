"""handlers/custom_features.py's owner flow — the first pre-apply confirmation
step in the codebase (preview with "✅ Применить"/"❌ Отмена" before anything is
written), plus its two safety gates: the forbidden-imports AST re-check and
the isolated-import subprocess smoke check, both at apply time.

Handlers are called directly, not through a real Dispatcher — same convention
as tests/test_manage_bots_features.py and tests/test_start_buttons.py (aiogram
Router objects can only attach to one Dispatcher for their whole process
lifetime).
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import handlers.admin_manager as admin_manager_module
import handlers.custom_features as cf_module
import runtime.registry as reg
from db.database import (
    add_custom_feature_record,
    create_bot_record_with_admins,
    delete_bot,
    get_bot_miniapp_config,
    get_custom_feature_history,
    init_db,
)
from services.claude_service import CustomFeatureGenerationError

FAKE_TOKEN = "123456789:AAHfakeTokenButShapedRight1234567890"

_CLEAN_PATCH_SOURCE = (
    "from aiogram import Router\n\n"
    "router = Router()\n\n"
    "@router.message()\n"
    "async def _noop(message):\n"
    "    pass\n"
)

_BROKEN_IMPORT_PATCH_SOURCE = (
    "import this_module_does_not_exist_anywhere_xyz\n"
    "from aiogram import Router\n\n"
    "router = Router()\n"
)

_FORBIDDEN_IMPORT_PATCH_SOURCE = (
    "import pandas\n"
    "from aiogram import Router\n\n"
    "router = Router()\n"
)


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


class _CustomFeatureHandlerTestBase(unittest.IsolatedAsyncioTestCase):
    owner_id = 0  # overridden per subclass so parallel tests don't share an owner id

    async def asyncSetUp(self):
        # Idempotent (CREATE TABLE IF NOT EXISTS) — see test_custom_feature_db.py's
        # identical comment for why this is here (self-sufficiency, not a fix
        # of the separately-tracked real-DB-isolation gap).
        await init_db()
        self._tmp = tempfile.TemporaryDirectory()
        self.cf_dir = Path(self._tmp.name) / "custom_features"
        self.cf_dir.mkdir()
        self._cf_dir_patcher = patch.object(cf_module, "_CUSTOM_FEATURES_DIR", self.cf_dir)
        self._cf_dir_patcher.start()
        self._registry_cf_dir_patcher = patch.object(reg, "_CUSTOM_FEATURES_DIR", self.cf_dir)
        self._registry_cf_dir_patcher.start()

        # handlers/custom_features.py migrated from _is_owner to per-bot
        # _can_manage_bot (project_multitenancy_audit_gaps memory, item 3,
        # 2026-08-19) — patch it where _can_manage_bot actually resolves it
        # (admin_manager's own module globals). Every bot row these tests
        # create has owner_telegram_id=None, so _can_manage_bot() falls
        # through to this same _is_owner check unchanged.
        self._owner_patcher = patch.object(admin_manager_module, "_is_owner", lambda uid: uid == self.owner_id)
        self._owner_patcher.start()

        self._fake_registry = MagicMock()
        self._fake_registry.reload_one = AsyncMock()
        cf_module.set_registry(self._fake_registry)

        self.bot_file = Path(self._tmp.name) / "fake_bot.py"
        self.bot_file.write_text("# fake bot file for custom_features handler tests\n", encoding="utf-8")
        self.bot_id = await create_bot_record_with_admins(
            name=f"cf_handler_bot_{self.owner_id}", description="test", token=FAKE_TOKEN,
            file_path=str(self.bot_file), admin_ids=[str(self.owner_id)],
        )

    async def asyncTearDown(self):
        cf_module.set_registry(None)
        self._owner_patcher.stop()
        self._registry_cf_dir_patcher.stop()
        self._cf_dir_patcher.stop()
        reg._custom_feature_module_cache.pop(self.bot_id, None)
        sys.modules.pop(f"custom_features_bot_{self.bot_id}", None)
        cf_module._busy_bots.discard(self.bot_id)
        await delete_bot(self.bot_id)
        self._tmp.cleanup()


class NonOwnerCannotStartOrActOnCustomFeatureTests(_CustomFeatureHandlerTestBase):
    owner_id = 600001

    async def test_non_owner_start_is_denied(self):
        non_owner = 999001
        state = _make_fsm_context(non_owner)
        callback = _make_callback(non_owner, f"customfeature:{self.bot_id}")
        await cf_module.cb_start_custom_feature(callback, state)
        callback.answer.assert_awaited()
        self.assertIsNone(await state.get_state())

    async def test_non_owner_apply_is_denied_and_writes_nothing(self):
        non_owner = 999002
        state = _make_fsm_context(non_owner)
        callback = _make_callback(non_owner, f"applycustom:{self.bot_id}")
        await cf_module.cb_apply_custom_feature(callback, state)
        self.assertFalse((self.cf_dir / f"bot_{self.bot_id}.py").exists())


class HappyPathAppliesPatchTests(_CustomFeatureHandlerTestBase):
    owner_id = 600002

    async def test_full_flow_writes_file_records_history_and_reloads(self):
        state = _make_fsm_context(self.owner_id)

        start_callback = _make_callback(self.owner_id, f"customfeature:{self.bot_id}")
        await cf_module.cb_start_custom_feature(start_callback, state)
        self.assertEqual(await state.get_state(), cf_module.CustomFeatureStates.describing_request.state)

        message = _make_message(self.owner_id, "добавь команду /ping")
        with patch.object(cf_module, "generate_custom_feature", AsyncMock(return_value=_CLEAN_PATCH_SOURCE)), \
             patch.object(cf_module, "explain_custom_feature", AsyncMock(return_value="Бот научится отвечать на /ping.")):
            await cf_module.msg_custom_feature_text(message, state)

        data = await state.get_data()
        self.assertEqual(data["cf_pending_code"], _CLEAN_PATCH_SOURCE)
        self.assertEqual(data["cf_pending_request"], "добавь команду /ping")

        apply_callback = _make_callback(self.owner_id, f"applycustom:{self.bot_id}")
        await cf_module.cb_apply_custom_feature(apply_callback, state)

        module_path = self.cf_dir / f"bot_{self.bot_id}.py"
        self.assertTrue(module_path.exists())
        self.assertEqual(module_path.read_text(encoding="utf-8"), _CLEAN_PATCH_SOURCE)
        self._fake_registry.reload_one.assert_awaited_once_with(self.bot_id)

        history = await get_custom_feature_history(self.bot_id)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["description"], "добавь команду /ping")
        self.assertIsNone(await state.get_state())


class ApplyRegeneratesMiniappConfigTests(_CustomFeatureHandlerTestBase):
    """See docs/MINIAPP_DESIGN.md §6: a successful apply must trigger a
    miniapp_config regeneration, since a custom_features patch can add its
    own tables and leave a previously-stored config stale. Fired via
    asyncio.create_task (fire-and-forget, must not block/delay the apply
    response) — patched here to run synchronously so it's observable."""

    owner_id = 600010

    async def _apply_patch_and_wait_for_regeneration(self, state):
        """asyncio.create_task is fire-and-forget in the handler; capture the
        coroutine it schedules and await it directly instead of sleeping/
        polling for it to finish on its own."""
        scheduled = []
        real_create_task = __import__("asyncio").create_task

        def _capturing_create_task(coro, *a, **kw):
            scheduled.append(coro)
            return MagicMock()  # stand-in Task, never actually scheduled

        with patch.object(cf_module.asyncio, "create_task", side_effect=_capturing_create_task):
            apply_callback = _make_callback(self.owner_id, f"applycustom:{self.bot_id}")
            await cf_module.cb_apply_custom_feature(apply_callback, state)

        # The github-push task is also scheduled via create_task — run every
        # captured coroutine; only the miniapp-config one has an observable
        # DB effect, the others are no-ops against the fakes in this test.
        for coro in scheduled:
            await coro

    async def test_apply_triggers_regeneration_that_persists_a_valid_config(self):
        state = _make_fsm_context(self.owner_id)
        start_callback = _make_callback(self.owner_id, f"customfeature:{self.bot_id}")
        await cf_module.cb_start_custom_feature(start_callback, state)

        message = _make_message(self.owner_id, "добавь таблицу заказов")
        with patch.object(cf_module, "generate_custom_feature", AsyncMock(return_value=_CLEAN_PATCH_SOURCE)), \
             patch.object(cf_module, "explain_custom_feature", AsyncMock(return_value="ok")):
            await cf_module.msg_custom_feature_text(message, state)

        fake_config = {
            "resources": [{"name": "orders", "table": "orders", "fields": [{"name": "x"}]}]
        }
        with patch.object(cf_module, "_generate_miniapp_config", AsyncMock(return_value=fake_config)):
            await self._apply_patch_and_wait_for_regeneration(state)

        self.assertEqual(await get_bot_miniapp_config(self.bot_id), fake_config)

    async def test_regeneration_failure_does_not_break_the_apply(self):
        """_generate_miniapp_config already never raises on its own (see
        tests/test_miniapp_config_claude_service.py), but the hook wrapping
        it must also survive an unexpected exception without propagating —
        the apply itself (file write, history record, reload) must have
        already fully succeeded by the time this runs."""
        state = _make_fsm_context(self.owner_id)
        start_callback = _make_callback(self.owner_id, f"customfeature:{self.bot_id}")
        await cf_module.cb_start_custom_feature(start_callback, state)

        message = _make_message(self.owner_id, "добавь команду /ping")
        with patch.object(cf_module, "generate_custom_feature", AsyncMock(return_value=_CLEAN_PATCH_SOURCE)), \
             patch.object(cf_module, "explain_custom_feature", AsyncMock(return_value="ok")):
            await cf_module.msg_custom_feature_text(message, state)

        with patch.object(cf_module, "_generate_miniapp_config", AsyncMock(side_effect=RuntimeError("boom"))):
            await self._apply_patch_and_wait_for_regeneration(state)

        module_path = self.cf_dir / f"bot_{self.bot_id}.py"
        self.assertTrue(module_path.exists())
        self._fake_registry.reload_one.assert_awaited_once_with(self.bot_id)
        self.assertIsNone(await get_bot_miniapp_config(self.bot_id))


class GenerationErrorOffersRetryTests(_CustomFeatureHandlerTestBase):
    owner_id = 600003

    async def test_generation_error_clears_state_without_a_pending_preview(self):
        state = _make_fsm_context(self.owner_id)
        await cf_module.cb_start_custom_feature(
            _make_callback(self.owner_id, f"customfeature:{self.bot_id}"), state
        )

        message = _make_message(self.owner_id, "нечто невозможное")
        with patch.object(
            cf_module, "generate_custom_feature",
            AsyncMock(side_effect=CustomFeatureGenerationError("boom")),
        ):
            await cf_module.msg_custom_feature_text(message, state)

        self.assertIsNone(await state.get_state())
        data = await state.get_data()
        self.assertNotIn("cf_pending_code", data)
        self.assertFalse((self.cf_dir / f"bot_{self.bot_id}.py").exists())


class BrokenImportIsRejectedAtApplyTests(_CustomFeatureHandlerTestBase):
    owner_id = 600004

    async def test_broken_import_deletes_file_and_skips_reload(self):
        state = _make_fsm_context(self.owner_id)
        await state.set_state(cf_module.CustomFeatureStates.describing_request)
        await state.update_data(
            cf_bot_id=self.bot_id, cf_pending_code=_BROKEN_IMPORT_PATCH_SOURCE, cf_pending_request="test",
        )
        callback = _make_callback(self.owner_id, f"applycustom:{self.bot_id}")
        await cf_module.cb_apply_custom_feature(callback, state)

        self.assertFalse((self.cf_dir / f"bot_{self.bot_id}.py").exists())
        self._fake_registry.reload_one.assert_not_awaited()
        self.assertEqual(await get_custom_feature_history(self.bot_id), [])


class StaleMainFileIsRejectedAtApplyTests(_CustomFeatureHandlerTestBase):
    """Review-driven fix: the busy-lock only spans generation + apply, not the
    open-ended time a preview sits waiting for the owner's tap — in that gap
    /recreate or "Исправить баг" (handlers/manage_bots.py) can freely rewrite
    the main bot file out from under an already-generated patch. The stored
    cf_main_code_hash must catch that instead of silently applying a patch
    against assumptions that no longer hold."""
    owner_id = 600009

    async def test_changed_main_file_blocks_apply(self):
        state = _make_fsm_context(self.owner_id)
        await state.set_state(cf_module.CustomFeatureStates.describing_request)
        await state.update_data(
            cf_bot_id=self.bot_id, cf_pending_code=_CLEAN_PATCH_SOURCE, cf_pending_request="test",
            cf_main_code_hash="deliberately-wrong-hash-simulating-a-since-changed-main-file",
        )
        callback = _make_callback(self.owner_id, f"applycustom:{self.bot_id}")
        await cf_module.cb_apply_custom_feature(callback, state)

        self.assertFalse((self.cf_dir / f"bot_{self.bot_id}.py").exists())
        self._fake_registry.reload_one.assert_not_awaited()
        (text,), kwargs = callback.message.edit_text.call_args
        self.assertIn("изменился", text)
        self.assertIsNotNone(kwargs.get("reply_markup"), "must offer a way to regenerate")

    async def test_unchanged_main_file_allows_apply(self):
        state = _make_fsm_context(self.owner_id)
        await state.set_state(cf_module.CustomFeatureStates.describing_request)
        current_hash = cf_module._hash_main_code(self.bot_file.read_text(encoding="utf-8"))
        await state.update_data(
            cf_bot_id=self.bot_id, cf_pending_code=_CLEAN_PATCH_SOURCE, cf_pending_request="test",
            cf_main_code_hash=current_hash,
        )
        callback = _make_callback(self.owner_id, f"applycustom:{self.bot_id}")
        await cf_module.cb_apply_custom_feature(callback, state)

        self.assertTrue((self.cf_dir / f"bot_{self.bot_id}.py").exists())
        self._fake_registry.reload_one.assert_awaited_once_with(self.bot_id)


class ForbiddenImportIsRejectedAtApplyTests(_CustomFeatureHandlerTestBase):
    """Defense in depth (design point 5): even if a forbidden import somehow
    reached cf_pending_code bypassing generate_custom_feature's own retry
    loop, the apply-time re-check must still catch it before any write."""
    owner_id = 600005

    async def test_forbidden_import_never_written_to_disk(self):
        state = _make_fsm_context(self.owner_id)
        await state.set_state(cf_module.CustomFeatureStates.describing_request)
        await state.update_data(
            cf_bot_id=self.bot_id, cf_pending_code=_FORBIDDEN_IMPORT_PATCH_SOURCE, cf_pending_request="test",
        )
        callback = _make_callback(self.owner_id, f"applycustom:{self.bot_id}")
        await cf_module.cb_apply_custom_feature(callback, state)

        self.assertFalse((self.cf_dir / f"bot_{self.bot_id}.py").exists())
        self._fake_registry.reload_one.assert_not_awaited()


class CancelDiscardsPendingPatchTests(_CustomFeatureHandlerTestBase):
    owner_id = 600006

    async def test_cancel_clears_state_and_writes_nothing(self):
        state = _make_fsm_context(self.owner_id)
        await state.set_state(cf_module.CustomFeatureStates.describing_request)
        await state.update_data(
            cf_bot_id=self.bot_id, cf_pending_code=_CLEAN_PATCH_SOURCE, cf_pending_request="test",
        )
        callback = _make_callback(self.owner_id, f"cancelcustom:{self.bot_id}")
        await cf_module.cb_cancel_custom_feature(callback, state)

        self.assertIsNone(await state.get_state())
        self.assertFalse((self.cf_dir / f"bot_{self.bot_id}.py").exists())


class BusyBotRejectsApplyTests(_CustomFeatureHandlerTestBase):
    owner_id = 600007

    async def test_busy_bot_apply_is_rejected(self):
        state = _make_fsm_context(self.owner_id)
        await state.set_state(cf_module.CustomFeatureStates.describing_request)
        await state.update_data(
            cf_bot_id=self.bot_id, cf_pending_code=_CLEAN_PATCH_SOURCE, cf_pending_request="test",
        )
        cf_module._busy_bots.add(self.bot_id)
        try:
            callback = _make_callback(self.owner_id, f"applycustom:{self.bot_id}")
            await cf_module.cb_apply_custom_feature(callback, state)
            self.assertFalse((self.cf_dir / f"bot_{self.bot_id}.py").exists())
        finally:
            cf_module._busy_bots.discard(self.bot_id)


class ApplyWithoutMatchingPendingDataIsRejectedTests(_CustomFeatureHandlerTestBase):
    owner_id = 600008

    async def test_apply_with_no_pending_state_shows_stale_message(self):
        state = _make_fsm_context(self.owner_id)  # nothing set — e.g. stale button from an old message
        callback = _make_callback(self.owner_id, f"applycustom:{self.bot_id}")
        await cf_module.cb_apply_custom_feature(callback, state)

        callback.answer.assert_awaited_once()
        callback.message.edit_text.assert_awaited_once()
        (text,), _ = callback.message.edit_text.call_args
        self.assertIn("устарела", text)
        self.assertFalse((self.cf_dir / f"bot_{self.bot_id}.py").exists())
        self.assertNotIn(self.bot_id, cf_module._busy_bots, "busy lock must be released even on the stale path")


class CustomFeatureHistoryTests(_CustomFeatureHandlerTestBase):
    """Owner-facing view of bot_custom_features — data layer existed
    (get_custom_feature_history) but was previously unreachable from any UI."""
    owner_id = 600010

    async def test_non_owner_is_denied(self):
        non_owner = 999010
        callback = _make_callback(non_owner, f"customfeaturehistory:{self.bot_id}")
        await cf_module.cb_custom_feature_history(callback)
        callback.answer.assert_awaited_once()
        callback.message.edit_text.assert_not_awaited()

    async def test_empty_history_says_so(self):
        callback = _make_callback(self.owner_id, f"customfeaturehistory:{self.bot_id}")
        await cf_module.cb_custom_feature_history(callback)
        (text,), _ = callback.message.edit_text.call_args
        self.assertIn("Пока пусто", text)

    async def test_populated_history_lists_description_and_date_newest_first(self):
        await add_custom_feature_record(self.bot_id, "добавь команду /ping")
        await add_custom_feature_record(self.bot_id, "добавь экспорт в CSV")
        callback = _make_callback(self.owner_id, f"customfeaturehistory:{self.bot_id}")
        await cf_module.cb_custom_feature_history(callback)
        (text,), kwargs = callback.message.edit_text.call_args
        self.assertIn("добавь команду /ping", text)
        self.assertIn("добавь экспорт в CSV", text)
        self.assertLess(
            text.index("добавь экспорт в CSV"), text.index("добавь команду /ping"),
            "most recently applied request must be listed first",
        )
        self.assertIsNotNone(kwargs.get("reply_markup"), "must offer a way back to the bot card")

    async def test_history_beyond_display_limit_is_truncated_with_a_note(self):
        for i in range(cf_module._HISTORY_DISPLAY_LIMIT + 3):
            await add_custom_feature_record(self.bot_id, f"доработка №{i}")
        callback = _make_callback(self.owner_id, f"customfeaturehistory:{self.bot_id}")
        await cf_module.cb_custom_feature_history(callback)
        (text,), _ = callback.message.edit_text.call_args
        self.assertIn(f"последние {cf_module._HISTORY_DISPLAY_LIMIT}", text)

    async def test_description_with_html_special_chars_is_escaped_not_broken(self):
        await add_custom_feature_record(self.bot_id, "добавь кнопку <script>alert(1)</script>")
        callback = _make_callback(self.owner_id, f"customfeaturehistory:{self.bot_id}")
        await cf_module.cb_custom_feature_history(callback)
        (text,), kwargs = callback.message.edit_text.call_args
        self.assertNotIn("<script>", text)
        self.assertIn("&lt;script&gt;", text)


if __name__ == "__main__":
    unittest.main()
