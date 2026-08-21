"""Feature-toggle command tests (handlers/manage_bots.py's features:/
togglefeature: callbacks, from feature-modules-inventory Task 2).

Task 3's compatibility-rejection criterion: attempting to enable a feature not
listed in its # COMPATIBLE_WITH: header must be an explicit, immediate
rejection (callback.answer(..., show_alert=True), no bot_features row
written) — not a silent no-op discovered only later when build_entry() tries
and fails to attach it.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import features
import templates

import handlers.admin_manager as admin_manager
import handlers.manage_bots as manage_bots
import runtime.registry as reg
from db.database import create_bot_record_with_admins, delete_bot, get_bot_features

FAKE_TOKEN = "123456789:AAHfakeTokenButShapedRight1234567890"

_FIXTURE_TEMPLATE_SOURCE = '''
# TEMPLATE: fixture_toggle_template
# USE FOR: a fixture template for the feature-toggle command test
from aiogram import Router

router = Router()
'''

_FIXTURE_FEATURE_SOURCE = '''
# FEATURE: fixture_toggle_feature
# COMPATIBLE_WITH: fixture_toggle_template
from aiogram import Router

router = Router()


async def init_db(db_path):
    pass
'''


def _make_callback(user_id: int, data: str) -> MagicMock:
    callback = MagicMock()
    callback.from_user.id = user_id
    callback.data = data
    callback.answer = AsyncMock()
    callback.message.edit_text = AsyncMock()
    callback.message.answer = AsyncMock()
    return callback


class _FeatureToggleTestBase(unittest.IsolatedAsyncioTestCase):
    owner_id = 0  # overridden per subclass so parallel tests don't share an owner id

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self._tmp.name)
        templates.__path__.append(str(self.tmp_dir))
        features.__path__.append(str(self.tmp_dir))
        (self.tmp_dir / "fixture_toggle_template.py").write_text(_FIXTURE_TEMPLATE_SOURCE, encoding="utf-8")
        (self.tmp_dir / "fixture_toggle_feature.py").write_text(_FIXTURE_FEATURE_SOURCE, encoding="utf-8")

        # discover_features() scans a fixed real directory (runtime.registry._FEATURES_DIR),
        # unlike template resolution which goes through templates.__path__ — point it at
        # the fixture dir so the toggle command's compatibility check sees our fixture
        # feature instead of (or in addition to) the real features/ directory.
        self._features_dir_patcher = patch.object(reg, "_FEATURES_DIR", self.tmp_dir)
        self._features_dir_patcher.start()

        self._owner_patcher = patch.object(manage_bots, "_is_owner", lambda uid: uid == self.owner_id)
        self._owner_patcher.start()
        # cb_toggle_feature now goes through _can_manage_bot (Stage 1
        # per-bot ownership), which closes over admin_manager's OWN
        # _is_owner — patching manage_bots._is_owner alone no longer covers
        # it, so both must be patched consistently.
        self._admin_owner_patcher = patch.object(admin_manager, "_is_owner", lambda uid: uid == self.owner_id)
        self._admin_owner_patcher.start()

    async def asyncTearDown(self):
        self._admin_owner_patcher.stop()
        self._owner_patcher.stop()
        self._features_dir_patcher.stop()
        await delete_bot(self.bot_id)
        templates.__path__ = [p for p in templates.__path__ if p != str(self.tmp_dir)]
        features.__path__ = [p for p in features.__path__ if p != str(self.tmp_dir)]
        sys.modules.pop("templates.fixture_toggle_template", None)
        sys.modules.pop("features.fixture_toggle_feature", None)
        self._tmp.cleanup()


class IncompatibleFeatureToggleIsRejectedTests(_FeatureToggleTestBase):
    owner_id = 424242

    async def asyncSetUp(self):
        await super().asyncSetUp()
        # file_path points at a REAL, different template — infer_template_id()
        # reads "inventory", never listed in fixture_toggle_feature's
        # COMPATIBLE_WITH:.
        self.bot_id = await create_bot_record_with_admins(
            name="toggle_incompatible_bot", description="test", token=FAKE_TOKEN,
            file_path="templates/inventory.py", admin_ids=[str(self.owner_id)],
        )

    async def test_incompatible_feature_toggle_is_explicitly_rejected(self):
        callback = _make_callback(self.owner_id, f"togglefeature:{self.bot_id}:fixture_toggle_feature")
        await manage_bots.cb_toggle_feature(callback)

        callback.answer.assert_awaited_once()
        _, kwargs = callback.answer.call_args
        self.assertTrue(kwargs.get("show_alert"), "incompatible toggle must be an alert, not a quiet no-op")
        self.assertNotIn("fixture_toggle_feature", await get_bot_features(self.bot_id))


class CompatibleFeatureToggleEnablesAndReloadsTests(_FeatureToggleTestBase):
    owner_id = 424243

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.bot_id = await create_bot_record_with_admins(
            name="toggle_compatible_bot", description="test", token=FAKE_TOKEN,
            file_path=str(self.tmp_dir / "fixture_toggle_template.py"), admin_ids=[str(self.owner_id)],
        )
        self._fake_registry = MagicMock()
        self._fake_registry.reload_one = AsyncMock()
        manage_bots.set_registry(self._fake_registry)

    async def asyncTearDown(self):
        manage_bots.set_registry(None)
        await super().asyncTearDown()

    async def test_compatible_feature_toggle_enables_and_reloads_registry(self):
        callback = _make_callback(self.owner_id, f"togglefeature:{self.bot_id}:fixture_toggle_feature")
        await manage_bots.cb_toggle_feature(callback)

        self.assertIn("fixture_toggle_feature", await get_bot_features(self.bot_id))
        self._fake_registry.reload_one.assert_awaited_once_with(self.bot_id)

    async def test_toggling_again_disables_it(self):
        callback_on = _make_callback(self.owner_id, f"togglefeature:{self.bot_id}:fixture_toggle_feature")
        await manage_bots.cb_toggle_feature(callback_on)
        callback_off = _make_callback(self.owner_id, f"togglefeature:{self.bot_id}:fixture_toggle_feature")
        await manage_bots.cb_toggle_feature(callback_off)

        self.assertNotIn("fixture_toggle_feature", await get_bot_features(self.bot_id))
        self.assertEqual(self._fake_registry.reload_one.await_count, 2)


class FeatureLabelCompletenessTests(unittest.TestCase):
    """manage_bots._FEATURE_LABELS must name every real feature discovered
    from features/*.py's own "# FEATURE:" headers, so the owner-facing "🧩
    Фичи" panel never falls back to a raw technical key like "channel_monitor"
    (Creator-panel readability fix)."""

    def test_every_discovered_feature_has_a_label(self):
        missing = [f["name"] for f in reg.discover_features() if f["name"] not in manage_bots._FEATURE_LABELS]
        self.assertEqual(missing, [], f"features missing from _FEATURE_LABELS: {missing}")


class NonOwnerCannotToggleTests(_FeatureToggleTestBase):
    owner_id = 424244

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.bot_id = await create_bot_record_with_admins(
            name="toggle_denied_bot", description="test", token=FAKE_TOKEN,
            file_path=str(self.tmp_dir / "fixture_toggle_template.py"), admin_ids=[str(self.owner_id)],
        )

    async def test_non_owner_toggle_is_denied_and_writes_nothing(self):
        NON_OWNER_ID = 999999
        callback = _make_callback(NON_OWNER_ID, f"togglefeature:{self.bot_id}:fixture_toggle_feature")
        await manage_bots.cb_toggle_feature(callback)

        callback.answer.assert_awaited_once()
        self.assertNotIn("fixture_toggle_feature", await get_bot_features(self.bot_id))


if __name__ == "__main__":
    unittest.main()
