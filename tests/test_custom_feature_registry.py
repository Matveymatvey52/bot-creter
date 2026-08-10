"""Dynamic custom_features loading in runtime/registry.py (custom_features
design Task 2, mirroring tests/test_feature_dynamic_registry.py's structure
for the bot_features/features case).

Main criterion: a custom_features/bot_<id>.py file, if present, gets its
router wired into that bot's Dispatcher and its init_db(db_path) called with
the SAME db_path the host template itself uses — purely by bot_id-keyed file
presence, no DB row involved (unlike bot_features).

The invalidation tests below cover the one risk this feature introduces that
templates/features never had: custom_features/bot_<id>.py can be REWRITTEN
on disk while the process is still running (the owner's "✅ Применить" step),
so a stale entry in Python's own sys.modules (not just this registry's cache
dict) must be cleared explicitly or a second build_entry() silently reuses
the old bytecode instead of the rewritten file.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import templates
import runtime.registry as reg
from db.database import init_db

FAKE_TOKEN = "123456789:AAHfakeTokenButShapedRight1234567890"

_FIXTURE_TEMPLATE_SOURCE = '''
# TEMPLATE: fixture_cf_host_template
# USE FOR: a fixture template that hosts a custom_features patch for the dynamic loading test
from dataclasses import dataclass
from aiogram import Router

router = Router()


@dataclass
class FixtureConfig:
    bot_id: int
    db_path: str


def config_from_bot_row(bot_row, data_dir):
    return FixtureConfig(bot_id=bot_row["bot_id"], db_path=str(data_dir / f"bot_{bot_row['bot_id']}_fixture.db"))


class ConfigMiddleware:
    def __init__(self, config):
        self.config = config

    async def __call__(self, handler, event, data):
        data["config"] = self.config
        return await handler(event, data)


async def init_db(db_path):
    pass
'''

_FIXTURE_CUSTOM_FEATURE_V1 = '''
from aiogram import Router

router = Router()
VERSION = "v1"
init_db_called_with = []


async def init_db(db_path):
    init_db_called_with.append(db_path)
'''

_FIXTURE_CUSTOM_FEATURE_V2 = '''
from aiogram import Router

router = Router()
VERSION = "v2"
'''

_FIXTURE_BROKEN_CUSTOM_FEATURE = "raise RuntimeError('deliberately broken import')\n"


class _CustomFeatureRegistryTestBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # build_entry() also calls _load_and_include_features(), which reads
        # the real bot_features table — idempotent init_db() makes this file
        # self-sufficient regardless of run order (see
        # tests/test_custom_feature_db.py's identical comment).
        await init_db()
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self._tmp.name)
        templates.__path__.append(str(self.tmp_dir))
        (self.tmp_dir / "fixture_cf_host_template.py").write_text(_FIXTURE_TEMPLATE_SOURCE, encoding="utf-8")
        self.data_dir_patcher = patch("config.DATA_DIR", self.tmp_dir)
        self.data_dir_patcher.start()

        self._cf_tmp = tempfile.TemporaryDirectory()
        self.cf_dir = Path(self._cf_tmp.name)
        self._cf_dir_patcher = patch.object(reg, "_CUSTOM_FEATURES_DIR", self.cf_dir)
        self._cf_dir_patcher.start()

    async def asyncTearDown(self):
        self._cf_dir_patcher.stop()
        self.data_dir_patcher.stop()
        templates.__path__ = [p for p in templates.__path__ if p != str(self.tmp_dir)]
        reg._template_module_cache.pop("fixture_cf_host_template", None)
        sys.modules.pop("templates.fixture_cf_host_template", None)
        for bot_id in getattr(self, "_used_bot_ids", []):
            reg._custom_feature_module_cache.pop(bot_id, None)
            sys.modules.pop(f"custom_features_bot_{bot_id}", None)
        self._tmp.cleanup()
        self._cf_tmp.cleanup()

    async def _build(self, bot_id: int):
        self._used_bot_ids = getattr(self, "_used_bot_ids", []) + [bot_id]
        return await reg.build_entry(
            bot_id, FAKE_TOKEN, "fixture_cf_host_template",
            {"bot_id": bot_id, "name": f"cf_bot_{bot_id}"},
        )


class CustomFeatureIsAutoLoadedByFilePresenceTests(_CustomFeatureRegistryTestBase):
    async def test_router_and_init_db_wired_purely_by_bot_id_file(self):
        bot_id = 9401
        (self.cf_dir / f"bot_{bot_id}.py").write_text(_FIXTURE_CUSTOM_FEATURE_V1, encoding="utf-8")

        entry = await self._build(bot_id)
        try:
            self.assertEqual(len(entry.dispatcher.sub_routers), 2, "expected host router + custom_features router")
            module = sys.modules[f"custom_features_bot_{bot_id}"]
            expected_db_path = str(self.tmp_dir / f"bot_{bot_id}_fixture.db")
            self.assertEqual(module.init_db_called_with, [expected_db_path])
        finally:
            await entry.bot.session.close()


class BotWithNoCustomFeatureFileIsUnaffectedTests(_CustomFeatureRegistryTestBase):
    async def test_no_file_means_only_host_router_included(self):
        bot_id = 9402
        entry = await self._build(bot_id)
        try:
            self.assertEqual(len(entry.dispatcher.sub_routers), 1)
        finally:
            await entry.bot.session.close()


class BrokenCustomFeatureDoesNotTakeDownHostTemplateTests(_CustomFeatureRegistryTestBase):
    async def test_broken_import_is_skipped_host_template_still_registers(self):
        bot_id = 9403
        (self.cf_dir / f"bot_{bot_id}.py").write_text(_FIXTURE_BROKEN_CUSTOM_FEATURE, encoding="utf-8")

        with self.assertLogs("runtime.registry", level="ERROR"):
            entry = await self._build(bot_id)
        try:
            self.assertEqual(len(entry.dispatcher.sub_routers), 1)
        finally:
            await entry.bot.session.close()


class CacheInvalidationTests(_CustomFeatureRegistryTestBase):
    """The novel risk custom_features introduces: unlike templates/features,
    this file can be rewritten WHILE the process is alive."""

    async def test_without_invalidation_a_rewrite_is_silently_ignored(self):
        bot_id = 9404
        module_path = self.cf_dir / f"bot_{bot_id}.py"
        module_path.write_text(_FIXTURE_CUSTOM_FEATURE_V1, encoding="utf-8")

        entry1 = await self._build(bot_id)
        await entry1.bot.session.close()
        self.assertEqual(sys.modules[f"custom_features_bot_{bot_id}"].VERSION, "v1")

        module_path.write_text(_FIXTURE_CUSTOM_FEATURE_V2, encoding="utf-8")
        entry2 = await self._build(bot_id)  # deliberately NOT calling invalidate_custom_feature_cache
        try:
            self.assertEqual(
                sys.modules[f"custom_features_bot_{bot_id}"].VERSION, "v1",
                "without invalidation the stale v1 module must still be the one in sys.modules",
            )
        finally:
            await entry2.bot.session.close()

    async def test_invalidate_custom_feature_cache_picks_up_the_rewrite(self):
        bot_id = 9405
        module_path = self.cf_dir / f"bot_{bot_id}.py"
        module_path.write_text(_FIXTURE_CUSTOM_FEATURE_V1, encoding="utf-8")

        entry1 = await self._build(bot_id)
        await entry1.bot.session.close()
        self.assertEqual(sys.modules[f"custom_features_bot_{bot_id}"].VERSION, "v1")

        module_path.write_text(_FIXTURE_CUSTOM_FEATURE_V2, encoding="utf-8")
        reg.invalidate_custom_feature_cache(bot_id)
        entry2 = await self._build(bot_id)
        try:
            self.assertEqual(sys.modules[f"custom_features_bot_{bot_id}"].VERSION, "v2")
        finally:
            await entry2.bot.session.close()

    async def test_invalidate_is_a_no_op_for_a_bot_id_never_loaded(self):
        reg.invalidate_custom_feature_cache(999999)  # must not raise


if __name__ == "__main__":
    unittest.main()
