"""Dynamic FEATURE loading phase (runtime/registry.py's build_entry() extension
from feature-modules-inventory Task 2).

Main criterion, mirroring test_template_dynamic_registry.py's for templates: a
bot with a feature_name enabled in bot_features must get that feature's router
wired into its Dispatcher, and its init_db(db_path) called with the SAME
db_path the host template itself uses — purely by dropping a features/<name>.py
file following the router/init_db naming convention, with ZERO changes to
runtime/registry.py itself.

Fixture host template + fixture feature are made importable the same way
test_template_dynamic_registry.py does it (namespace-package __path__ append)
— templates/ and features/ both have no __init__.py.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import features
import templates
import runtime.registry as reg
from db.database import disable_bot_feature, enable_bot_feature

FAKE_TOKEN = "123456789:AAHfakeTokenButShapedRight1234567890"

_FIXTURE_TEMPLATE_SOURCE = '''
# TEMPLATE: fixture_feature_host_template
# USE FOR: a fixture template that hosts a feature for the dynamic feature-loading test
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

_FIXTURE_FEATURE_SOURCE = '''
# FEATURE: fixture_dynamic_feature
# COMPATIBLE_WITH: fixture_feature_host_template
from aiogram import Router

router = Router()
init_db_called_with = []


async def init_db(db_path):
    init_db_called_with.append(db_path)


@router.message()
async def _capture_handler(message, config):
    from features import fixture_dynamic_feature as _self
    _self.last_seen_config = config
'''


class FeatureIsAutoLoadedOntoHostTemplateWithNoCodeChange(unittest.IsolatedAsyncioTestCase):
    """The headline claim: a brand-new features/*.py file, enabled for a bot
    via bot_features, gets its router wired into that bot's Dispatcher AND its
    init_db(db_path) called with the host template's OWN db_path — purely by
    naming convention, runtime/registry.py never touched or special-cased for
    this fixture."""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self._tmp.name)
        templates.__path__.append(str(self.tmp_dir))
        features.__path__.append(str(self.tmp_dir))
        (self.tmp_dir / "fixture_feature_host_template.py").write_text(_FIXTURE_TEMPLATE_SOURCE, encoding="utf-8")
        (self.tmp_dir / "fixture_dynamic_feature.py").write_text(_FIXTURE_FEATURE_SOURCE, encoding="utf-8")

        self.data_dir_patcher = patch("config.DATA_DIR", self.tmp_dir)
        self.data_dir_patcher.start()

        self.bot_id = 9201
        await enable_bot_feature(self.bot_id, "fixture_dynamic_feature")

    async def asyncTearDown(self):
        await disable_bot_feature(self.bot_id, "fixture_dynamic_feature")
        self.data_dir_patcher.stop()
        templates.__path__ = [p for p in templates.__path__ if p != str(self.tmp_dir)]
        features.__path__ = [p for p in features.__path__ if p != str(self.tmp_dir)]
        reg._template_module_cache.pop("fixture_feature_host_template", None)
        reg._feature_module_cache.pop("fixture_dynamic_feature", None)
        sys.modules.pop("templates.fixture_feature_host_template", None)
        sys.modules.pop("features.fixture_dynamic_feature", None)
        self._tmp.cleanup()

    async def test_build_entry_wires_feature_router_and_shares_host_db_path(self):
        entry = await reg.build_entry(
            self.bot_id, FAKE_TOKEN, "fixture_feature_host_template",
            {"bot_id": self.bot_id, "name": "fixture_feature_bot"},
        )
        try:
            self.assertEqual(len(entry.dispatcher.sub_routers), 2, "expected host router + feature router")

            update = {
                "update_id": 1,
                "message": {
                    "message_id": 1, "date": 1700000000,
                    "chat": {"id": 555, "type": "private"},
                    "from": {"id": 555, "is_bot": False, "first_name": "Test"},
                    "text": "hello",
                },
            }
            await entry.dispatcher.feed_webhook_update(entry.bot, update)

            feature_module = sys.modules["features.fixture_dynamic_feature"]
            self.assertTrue(hasattr(feature_module, "last_seen_config"))
            expected_db_path = str(self.tmp_dir / f"bot_{self.bot_id}_fixture.db")
            self.assertEqual(feature_module.last_seen_config.db_path, expected_db_path)
            self.assertEqual(len(feature_module.init_db_called_with), 1)
            self.assertEqual(feature_module.init_db_called_with[0], expected_db_path)
        finally:
            await entry.bot.session.close()


class BotWithNoEnabledFeaturesIsUnaffectedTests(unittest.IsolatedAsyncioTestCase):
    """Zero-cost path: a bot with no bot_features rows (everyone, today) must
    build exactly as before — no feature loading attempted, no extra router."""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self._tmp.name)
        templates.__path__.append(str(self.tmp_dir))
        (self.tmp_dir / "fixture_feature_host_template.py").write_text(_FIXTURE_TEMPLATE_SOURCE, encoding="utf-8")
        self.data_dir_patcher = patch("config.DATA_DIR", self.tmp_dir)
        self.data_dir_patcher.start()
        self.bot_id = 9202

    async def asyncTearDown(self):
        self.data_dir_patcher.stop()
        templates.__path__ = [p for p in templates.__path__ if p != str(self.tmp_dir)]
        reg._template_module_cache.pop("fixture_feature_host_template", None)
        sys.modules.pop("templates.fixture_feature_host_template", None)
        self._tmp.cleanup()

    async def test_no_enabled_features_means_only_the_template_router_is_included(self):
        entry = await reg.build_entry(
            self.bot_id, FAKE_TOKEN, "fixture_feature_host_template",
            {"bot_id": self.bot_id, "name": "no_feature_bot"},
        )
        try:
            self.assertEqual(len(entry.dispatcher.sub_routers), 1)
        finally:
            await entry.bot.session.close()


class BrokenFeatureDoesNotTakeDownTheHostTemplateTests(unittest.IsolatedAsyncioTestCase):
    """One feature's own failure (bad import, in this case) must not abort the
    whole bot's registration — the host template still gets its router, the
    broken feature is skipped with a loud warning instead."""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self._tmp.name)
        templates.__path__.append(str(self.tmp_dir))
        features.__path__.append(str(self.tmp_dir))
        (self.tmp_dir / "fixture_feature_host_template.py").write_text(_FIXTURE_TEMPLATE_SOURCE, encoding="utf-8")
        (self.tmp_dir / "broken_feature.py").write_text(
            "# FEATURE: broken_feature\n"
            "# COMPATIBLE_WITH: fixture_feature_host_template\n"
            "raise RuntimeError('deliberately broken import')\n",
            encoding="utf-8",
        )
        self.data_dir_patcher = patch("config.DATA_DIR", self.tmp_dir)
        self.data_dir_patcher.start()

        self.bot_id = 9203
        await enable_bot_feature(self.bot_id, "broken_feature")

    async def asyncTearDown(self):
        await disable_bot_feature(self.bot_id, "broken_feature")
        self.data_dir_patcher.stop()
        templates.__path__ = [p for p in templates.__path__ if p != str(self.tmp_dir)]
        features.__path__ = [p for p in features.__path__ if p != str(self.tmp_dir)]
        reg._template_module_cache.pop("fixture_feature_host_template", None)
        reg._feature_module_cache.pop("broken_feature", None)
        sys.modules.pop("templates.fixture_feature_host_template", None)
        sys.modules.pop("features.broken_feature", None)
        self._tmp.cleanup()

    async def test_broken_feature_is_skipped_host_template_still_registers(self):
        with self.assertLogs("runtime.registry", level="WARNING"):
            entry = await reg.build_entry(
                self.bot_id, FAKE_TOKEN, "fixture_feature_host_template",
                {"bot_id": self.bot_id, "name": "broken_feature_bot"},
            )
        try:
            self.assertEqual(len(entry.dispatcher.sub_routers), 1)  # host template's router only
        finally:
            await entry.bot.session.close()


if __name__ == "__main__":
    unittest.main()
