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
from unittest.mock import AsyncMock, MagicMock, patch

import features
import templates
import runtime.registry as reg
import db.database as db_module
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
        # db.database.DB_PATH is computed from DATA_DIR at import time, so
        # patching config.DATA_DIR above does NOT redirect it — patch it
        # directly too, otherwise DB calls hit the real data/bots.db (see
        # MEMORY.md "Backlog: test DB isolation").
        self.db_path_patcher = patch.object(db_module, "DB_PATH", self.tmp_dir / "bots.db")
        self.db_path_patcher.start()
        # Fresh tmp DB has no schema yet — enable_bot_feature()/
        # get_bot_features() need the bot_features table to exist.
        await db_module.init_db()

        self.bot_id = 9201
        await enable_bot_feature(self.bot_id, "fixture_dynamic_feature")

    async def asyncTearDown(self):
        await disable_bot_feature(self.bot_id, "fixture_dynamic_feature")
        self.db_path_patcher.stop()
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
        # db.database.DB_PATH is computed from DATA_DIR at import time, so
        # patching config.DATA_DIR above does NOT redirect it — patch it
        # directly too, otherwise DB calls hit the real data/bots.db (see
        # MEMORY.md "Backlog: test DB isolation").
        self.db_path_patcher = patch.object(db_module, "DB_PATH", self.tmp_dir / "bots.db")
        self.db_path_patcher.start()
        # Fresh tmp DB has no schema yet — enable_bot_feature()/
        # get_bot_features() need the bot_features table to exist.
        await db_module.init_db()
        self.bot_id = 9202

    async def asyncTearDown(self):
        self.db_path_patcher.stop()
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
        # db.database.DB_PATH is computed from DATA_DIR at import time, so
        # patching config.DATA_DIR above does NOT redirect it — patch it
        # directly too, otherwise DB calls hit the real data/bots.db (see
        # MEMORY.md "Backlog: test DB isolation").
        self.db_path_patcher = patch.object(db_module, "DB_PATH", self.tmp_dir / "bots.db")
        self.db_path_patcher.start()
        # Fresh tmp DB has no schema yet — enable_bot_feature()/
        # get_bot_features() need the bot_features table to exist.
        await db_module.init_db()

        self.bot_id = 9203
        await enable_bot_feature(self.bot_id, "broken_feature")

    async def asyncTearDown(self):
        await disable_bot_feature(self.bot_id, "broken_feature")
        self.db_path_patcher.stop()
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


_FIXTURE_BOT_ID_CAPTURE_FEATURE_SOURCE = '''
# FEATURE: fixture_bot_id_capture_feature
# COMPATIBLE_WITH: fixture_feature_host_template
from aiogram import Router

router = Router()
captured_bot_ids = []


@router.message()
async def _capture_handler(message, bot_id):
    captured_bot_ids.append(bot_id)
'''

_FIXTURE_COMMANDS_FEATURE_A_SOURCE = '''
# FEATURE: fixture_commands_feature_a
# COMPATIBLE_WITH: fixture_feature_host_template
from aiogram import Router

router = Router()
bot_commands = [("fixturea", "Fixture command A")]
'''

_FIXTURE_COMMANDS_FEATURE_B_SOURCE = '''
# FEATURE: fixture_commands_feature_b
# COMPATIBLE_WITH: fixture_feature_host_template
from aiogram import Router

router = Router()
bot_commands = [("fixtureb", "Fixture command B")]
'''


class FeatureHandlerReceivesBotIdTests(unittest.IsolatedAsyncioTestCase):
    """sellable-items-inventory's generic fix: since most host templates'
    Config dataclasses don't carry a bot_id field, a feature-level handler
    needs data["bot_id"] injected by the registry itself — see
    runtime/registry.py's _attach_bot_id_middleware()."""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self._tmp.name)
        templates.__path__.append(str(self.tmp_dir))
        features.__path__.append(str(self.tmp_dir))
        (self.tmp_dir / "fixture_feature_host_template.py").write_text(_FIXTURE_TEMPLATE_SOURCE, encoding="utf-8")
        (self.tmp_dir / "fixture_bot_id_capture_feature.py").write_text(
            _FIXTURE_BOT_ID_CAPTURE_FEATURE_SOURCE, encoding="utf-8"
        )
        self.data_dir_patcher = patch("config.DATA_DIR", self.tmp_dir)
        self.data_dir_patcher.start()
        # db.database.DB_PATH is computed from DATA_DIR at import time, so
        # patching config.DATA_DIR above does NOT redirect it — patch it
        # directly too, otherwise DB calls hit the real data/bots.db (see
        # MEMORY.md "Backlog: test DB isolation").
        self.db_path_patcher = patch.object(db_module, "DB_PATH", self.tmp_dir / "bots.db")
        self.db_path_patcher.start()
        # Fresh tmp DB has no schema yet — enable_bot_feature()/
        # get_bot_features() need the bot_features table to exist.
        await db_module.init_db()
        self.bot_id = 9301
        await enable_bot_feature(self.bot_id, "fixture_bot_id_capture_feature")

    async def asyncTearDown(self):
        await disable_bot_feature(self.bot_id, "fixture_bot_id_capture_feature")
        self.db_path_patcher.stop()
        self.data_dir_patcher.stop()
        templates.__path__ = [p for p in templates.__path__ if p != str(self.tmp_dir)]
        features.__path__ = [p for p in features.__path__ if p != str(self.tmp_dir)]
        reg._template_module_cache.pop("fixture_feature_host_template", None)
        reg._feature_module_cache.pop("fixture_bot_id_capture_feature", None)
        sys.modules.pop("templates.fixture_feature_host_template", None)
        sys.modules.pop("features.fixture_bot_id_capture_feature", None)
        self._tmp.cleanup()

    async def test_feature_handler_receives_this_bots_bot_id(self):
        entry = await reg.build_entry(
            self.bot_id, FAKE_TOKEN, "fixture_feature_host_template",
            {"bot_id": self.bot_id, "name": "bot_id_capture_bot"},
        )
        try:
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
            feature_module = sys.modules["features.fixture_bot_id_capture_feature"]
            self.assertEqual(feature_module.captured_bot_ids, [self.bot_id])
        finally:
            await entry.bot.session.close()

    async def test_bot_id_does_not_leak_between_two_different_bots(self):
        other_bot_id = 9302
        await enable_bot_feature(other_bot_id, "fixture_bot_id_capture_feature")
        try:
            entry_a = await reg.build_entry(
                self.bot_id, FAKE_TOKEN, "fixture_feature_host_template",
                {"bot_id": self.bot_id, "name": "bot_id_capture_bot_a"},
            )
            entry_b = await reg.build_entry(
                other_bot_id, FAKE_TOKEN, "fixture_feature_host_template",
                {"bot_id": other_bot_id, "name": "bot_id_capture_bot_b"},
            )
            try:
                update = {
                    "update_id": 1,
                    "message": {
                        "message_id": 1, "date": 1700000000,
                        "chat": {"id": 555, "type": "private"},
                        "from": {"id": 555, "is_bot": False, "first_name": "Test"},
                        "text": "hello",
                    },
                }
                await entry_a.dispatcher.feed_webhook_update(entry_a.bot, update)
                await entry_b.dispatcher.feed_webhook_update(entry_b.bot, {**update, "update_id": 2})
                feature_module = sys.modules["features.fixture_bot_id_capture_feature"]
                self.assertEqual(feature_module.captured_bot_ids, [self.bot_id, other_bot_id])
            finally:
                await entry_a.bot.session.close()
                await entry_b.bot.session.close()
        finally:
            await disable_bot_feature(other_bot_id, "fixture_bot_id_capture_feature")


class SetMyCommandsCollectionTests(unittest.IsolatedAsyncioTestCase):
    """A feature MAY export `bot_commands` (see sellable_items.py, the first
    real user) — runtime/registry.py collects these across every enabled
    feature and pushes ONE merged bot.set_my_commands() call, so two features
    declaring commands can never clobber each other's list."""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self._tmp.name)
        templates.__path__.append(str(self.tmp_dir))
        features.__path__.append(str(self.tmp_dir))
        (self.tmp_dir / "fixture_feature_host_template.py").write_text(_FIXTURE_TEMPLATE_SOURCE, encoding="utf-8")
        (self.tmp_dir / "fixture_commands_feature_a.py").write_text(_FIXTURE_COMMANDS_FEATURE_A_SOURCE, encoding="utf-8")
        (self.tmp_dir / "fixture_commands_feature_b.py").write_text(_FIXTURE_COMMANDS_FEATURE_B_SOURCE, encoding="utf-8")
        self.data_dir_patcher = patch("config.DATA_DIR", self.tmp_dir)
        self.data_dir_patcher.start()
        # db.database.DB_PATH is computed from DATA_DIR at import time, so
        # patching config.DATA_DIR above does NOT redirect it — patch it
        # directly too, otherwise DB calls hit the real data/bots.db (see
        # MEMORY.md "Backlog: test DB isolation").
        self.db_path_patcher = patch.object(db_module, "DB_PATH", self.tmp_dir / "bots.db")
        self.db_path_patcher.start()
        # Fresh tmp DB has no schema yet — enable_bot_feature()/
        # get_bot_features() need the bot_features table to exist.
        await db_module.init_db()
        self.bot_id = 9303
        # _last_sent_commands is a process-lifetime cache (devops-logs
        # review — see runtime/registry.py) keyed by bot_id, not reset
        # between test methods on its own. Every test method in this class
        # shares bot_id=9303, so a prior method's set_my_commands() call
        # would otherwise leak a cache entry into the next one — same reason
        # the module/sys.modules caches below are already reset per test.
        reg._last_sent_commands.pop(self.bot_id, None)
        await enable_bot_feature(self.bot_id, "fixture_commands_feature_a")
        await enable_bot_feature(self.bot_id, "fixture_commands_feature_b")

    async def asyncTearDown(self):
        await disable_bot_feature(self.bot_id, "fixture_commands_feature_a")
        await disable_bot_feature(self.bot_id, "fixture_commands_feature_b")
        self.db_path_patcher.stop()
        self.data_dir_patcher.stop()
        templates.__path__ = [p for p in templates.__path__ if p != str(self.tmp_dir)]
        features.__path__ = [p for p in features.__path__ if p != str(self.tmp_dir)]
        reg._template_module_cache.pop("fixture_feature_host_template", None)
        reg._feature_module_cache.pop("fixture_commands_feature_a", None)
        reg._feature_module_cache.pop("fixture_commands_feature_b", None)
        reg._last_sent_commands.pop(self.bot_id, None)
        sys.modules.pop("templates.fixture_feature_host_template", None)
        sys.modules.pop("features.fixture_commands_feature_a", None)
        sys.modules.pop("features.fixture_commands_feature_b", None)
        self._tmp.cleanup()

    async def test_commands_from_two_features_are_merged_into_one_call(self):
        calls = []

        async def _fake_call(*args, **kwargs):
            calls.append(args[0] if args else None)
            return MagicMock()

        with patch.object(reg.Bot, "__call__", new=AsyncMock(side_effect=_fake_call)):
            entry = await reg.build_entry(
                self.bot_id, FAKE_TOKEN, "fixture_feature_host_template",
                {"bot_id": self.bot_id, "name": "commands_merge_bot"},
            )
        try:
            set_commands_calls = [c for c in calls if type(c).__name__ == "SetMyCommands"]
            self.assertEqual(len(set_commands_calls), 1, "expected exactly ONE set_my_commands call, not one per feature")
            sent_commands = {(c.command, c.description) for c in set_commands_calls[0].commands}
            self.assertEqual(
                sent_commands,
                {("fixturea", "Fixture command A"), ("fixtureb", "Fixture command B")},
            )
        finally:
            await entry.bot.session.close()

    async def test_no_commands_declared_means_no_set_my_commands_call(self):
        # Reuses the plain fixture_dynamic_feature from the class above this
        # one (no bot_commands attribute at all) instead of the two
        # command-declaring fixtures — zero-cost path must stay zero-cost.
        await disable_bot_feature(self.bot_id, "fixture_commands_feature_a")
        await disable_bot_feature(self.bot_id, "fixture_commands_feature_b")
        (self.tmp_dir / "fixture_no_commands_feature.py").write_text(
            "# FEATURE: fixture_no_commands_feature\n"
            "# COMPATIBLE_WITH: fixture_feature_host_template\n"
            "from aiogram import Router\nrouter = Router()\n",
            encoding="utf-8",
        )
        await enable_bot_feature(self.bot_id, "fixture_no_commands_feature")
        calls = []

        async def _fake_call(*args, **kwargs):
            calls.append(args[0] if args else None)
            return MagicMock()

        try:
            with patch.object(reg.Bot, "__call__", new=AsyncMock(side_effect=_fake_call)):
                entry = await reg.build_entry(
                    self.bot_id, FAKE_TOKEN, "fixture_feature_host_template",
                    {"bot_id": self.bot_id, "name": "no_commands_bot"},
                )
            try:
                set_commands_calls = [c for c in calls if type(c).__name__ == "SetMyCommands"]
                self.assertEqual(len(set_commands_calls), 0)
            finally:
                await entry.bot.session.close()
        finally:
            await disable_bot_feature(self.bot_id, "fixture_no_commands_feature")
            reg._feature_module_cache.pop("fixture_no_commands_feature", None)
            sys.modules.pop("features.fixture_no_commands_feature", None)

    async def test_one_features_invalid_command_does_not_drop_anothers_valid_one(self):
        # Review finding: without per-entry validation, a single malformed
        # bot_command from one feature (here: uppercase, which Telegram's own
        # /setMyCommands rejects) would make the single merged
        # bot.set_my_commands() call fail outright — silently dropping
        # fixture_commands_feature_a's perfectly valid "fixturea" command too.
        await disable_bot_feature(self.bot_id, "fixture_commands_feature_b")
        (self.tmp_dir / "fixture_invalid_command_feature.py").write_text(
            "# FEATURE: fixture_invalid_command_feature\n"
            "# COMPATIBLE_WITH: fixture_feature_host_template\n"
            "from aiogram import Router\n"
            "router = Router()\n"
            'bot_commands = [("NotValid!", "Uppercase command names are rejected by Telegram")]\n',
            encoding="utf-8",
        )
        await enable_bot_feature(self.bot_id, "fixture_invalid_command_feature")
        calls = []

        async def _fake_call(*args, **kwargs):
            calls.append(args[0] if args else None)
            return MagicMock()

        try:
            with self.assertLogs("runtime.registry", level="WARNING"):
                with patch.object(reg.Bot, "__call__", new=AsyncMock(side_effect=_fake_call)):
                    entry = await reg.build_entry(
                        self.bot_id, FAKE_TOKEN, "fixture_feature_host_template",
                        {"bot_id": self.bot_id, "name": "invalid_command_bot"},
                    )
            try:
                set_commands_calls = [c for c in calls if type(c).__name__ == "SetMyCommands"]
                self.assertEqual(len(set_commands_calls), 1, "the valid command from the OTHER feature must still be pushed")
                sent_commands = {(c.command, c.description) for c in set_commands_calls[0].commands}
                self.assertEqual(sent_commands, {("fixturea", "Fixture command A")})
            finally:
                await entry.bot.session.close()
        finally:
            await disable_bot_feature(self.bot_id, "fixture_invalid_command_feature")
            reg._feature_module_cache.pop("fixture_invalid_command_feature", None)
            sys.modules.pop("features.fixture_invalid_command_feature", None)


if __name__ == "__main__":
    unittest.main()
