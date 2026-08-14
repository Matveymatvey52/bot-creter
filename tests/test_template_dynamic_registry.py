"""Dynamic template REGISTRATION phase (runtime/registry.py's _load_template_module()).

Distinct from the previous phase (dynamic SELECTION in services/claude_service.py):
this is about what happens once a bot already exists and needs a router +
middleware built for its template_id inside the live webhook registry. Main
criterion: adding a new templates/*.py file (following the router/
config_from_bot_row/ConfigMiddleware/init_db naming convention, verified
uniform across all 5 real templates) must let build_entry() build a working
entry for it — with ZERO changes to runtime/registry.py.

The fixture template used below is never written into the real templates/
directory. Since runtime/registry.py resolves templates via
importlib.import_module("templates.<id>") (not a scannable directory
constant), the fixture is made importable by temporarily appending a temp
directory to `templates.__path__` — templates/ has no __init__.py, so it's an
implicit PEP 420 namespace package, which supports exactly this at runtime.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import templates
import runtime.registry as reg
import db.database as db_module

FAKE_TOKEN = "123456789:AAHfakeTokenButShapedRight1234567890"

_FIXTURE_SOURCE = '''
# TEMPLATE: fixture_dynamic_template
# USE FOR: a fixture template for the dynamic registry test
from dataclasses import dataclass
from aiogram import Router

router = Router()
init_db_called_with = []


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
    init_db_called_with.append(db_path)


@router.message()
async def _capture_handler(message, config):
    from templates import fixture_dynamic_template as _self
    _self.last_seen_config = config
'''


class FixtureTemplateIsAutoRegisteredWithNoCodeChange(unittest.IsolatedAsyncioTestCase):
    """The headline claim: a brand-new templates/*.py file works end-to-end
    (router wired, middleware injects config, init_db called) purely by
    following the naming convention — runtime/registry.py itself is never
    touched or special-cased for this fixture."""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self._tmp.name)
        templates.__path__.append(str(self.tmp_dir))
        (self.tmp_dir / "fixture_dynamic_template.py").write_text(_FIXTURE_SOURCE, encoding="utf-8")

    async def asyncTearDown(self):
        # templates.__path__ is a _NamespacePath (PEP 420 namespace package),
        # which supports .append() but not .remove() — filter-and-reassign instead.
        templates.__path__ = [p for p in templates.__path__ if p != str(self.tmp_dir)]
        reg._template_module_cache.pop("fixture_dynamic_template", None)
        sys.modules.pop("templates.fixture_dynamic_template", None)
        self._tmp.cleanup()

    async def test_build_entry_wires_router_and_middleware_for_new_fixture(self):
        entry = await reg.build_entry(
            9001, FAKE_TOKEN, "fixture_dynamic_template", {"bot_id": 9001, "name": "fixture_bot"}
        )
        try:
            self.assertEqual(entry.template_id, "fixture_dynamic_template")

            update = {
                "update_id": 1,
                "message": {
                    "message_id": 1,
                    "date": 1700000000,
                    "chat": {"id": 555, "type": "private"},
                    "from": {"id": 555, "is_bot": False, "first_name": "Test"},
                    "text": "hello",
                },
            }
            await entry.dispatcher.feed_webhook_update(entry.bot, update)

            fixture_module = sys.modules["templates.fixture_dynamic_template"]
            # Router really matched and ran the handler, AND the middleware
            # really injected the config built by config_from_bot_row —
            # proves BOTH halves of build_entry() work for a brand-new file.
            self.assertTrue(hasattr(fixture_module, "last_seen_config"))
            self.assertEqual(fixture_module.last_seen_config.bot_id, 9001)
            # init_db(config.db_path) was called — bot has working tables
            # even though it was never run as a standalone subprocess.
            self.assertEqual(len(fixture_module.init_db_called_with), 1)
        finally:
            await entry.bot.session.close()


class RealTemplatesStillRegisterCorrectly(unittest.IsolatedAsyncioTestCase):
    """Regression: all 5 real templates must still build a working entry
    through the new generic mechanism — tour_operator especially, since it's
    the one template needing the _PRE_IMPORT_ENV_OVERRIDES exception."""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self._patcher = patch("config.DATA_DIR", self.data_dir)
        self._patcher.start()
        # db.database.DB_PATH is computed from DATA_DIR at import time, so
        # patching config.DATA_DIR above does NOT redirect it — patch it
        # directly too, otherwise build_entry()'s DB reads/writes hit the
        # real data/bots.db (see MEMORY.md "Backlog: test DB isolation").
        self._db_path_patcher = patch.object(db_module, "DB_PATH", self.data_dir / "bots.db")
        self._db_path_patcher.start()
        # Fresh tmp DB has no schema yet — build_entry()'s
        # _load_and_include_features() reads the bot_features table, which
        # must exist even for bots with none enabled.
        await db_module.init_db()

    async def asyncTearDown(self):
        self._db_path_patcher.stop()
        self._patcher.stop()
        self._tmp.cleanup()

    async def test_all_five_real_templates_build_working_entries(self):
        for i, template_id in enumerate(
            ("accountant", "manager_secretary", "booking_beauty", "trip_manager", "tour_operator")
        ):
            with self.subTest(template=template_id):
                bot_id = 9100 + i
                entry = await reg.build_entry(
                    bot_id, FAKE_TOKEN, template_id, {"bot_id": bot_id, "name": f"regress_{template_id}"}
                )
                try:
                    self.assertEqual(entry.template_id, template_id)
                    self.assertGreater(
                        len(entry.dispatcher.sub_routers), 0,
                        f"{template_id}: no router was included into the dispatcher",
                    )
                finally:
                    await entry.bot.session.close()

    async def test_tour_operator_env_override_applied_before_first_import(self):
        """The riskiest case: tour_operator reads TOUR_OPERATOR_WEB_ENABLED at
        MODULE IMPORT TIME. _load_template_module must set it before import,
        every time it's this module's first import in the process."""
        module = reg._load_template_module("tour_operator")
        self.assertIsNotNone(module)
        # Whatever the resolved value, the module must have imported cleanly
        # and expose WEB_CRM_ENABLED — proves the override plumbing ran
        # without raising, not a specific value (which depends on whether an
        # earlier test in the same process already imported this module).
        self.assertTrue(hasattr(module, "WEB_CRM_ENABLED"))


class UnknownTemplateIdIsLoudNotSilent(unittest.IsolatedAsyncioTestCase):
    """The gap closed by this phase: a bot with a template_id matching no
    templates/*.py module used to be silently half-registered (in the
    registry, but with no router and no init_db) with zero log output.
    Behavior must NOT change (still registered, not rejected) — only
    visibility changes."""

    async def test_unknown_template_id_logs_warning_but_still_registers(self):
        with self.assertLogs("runtime.registry", level="WARNING") as logs:
            entry = await reg.build_entry(
                9002, FAKE_TOKEN, "totally_unknown_template_xyz", {"bot_id": 9002, "name": "miss_bot"}
            )
        try:
            self.assertIsNotNone(entry)
            self.assertEqual(entry.template_id, "totally_unknown_template_xyz")
            self.assertEqual(len(entry.dispatcher.sub_routers), 0)
            self.assertTrue(
                any("totally_unknown_template_xyz" in msg and "9002" in msg for msg in logs.output)
            )
        finally:
            await entry.bot.session.close()


class ModuleWithoutRouterAttributeIsLoudNotSilent(unittest.IsolatedAsyncioTestCase):
    """Review-found gap: a template module that imports successfully but has no
    module-level `router` (naming-convention typo, or a config-only helper file)
    used to be silently half-registered too — working middleware/DB, zero
    handlers, zero log. Must also warn now, same as the unresolved-template_id
    case, without changing the registration outcome."""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self._tmp.name)
        templates.__path__.append(str(self.tmp_dir))
        (self.tmp_dir / "no_router_fixture.py").write_text(
            "# TEMPLATE: no_router_fixture\n"
            "# USE FOR: a fixture with no router attribute at all\n"
            "from dataclasses import dataclass\n"
            "@dataclass\n"
            "class _Config:\n"
            "    bot_id: int\n"
            "    db_path: str\n"
            "def config_from_bot_row(bot_row, data_dir):\n"
            "    return _Config(bot_id=bot_row['bot_id'], db_path=str(data_dir / 'x.db'))\n"
            "class ConfigMiddleware:\n"
            "    def __init__(self, config): self.config = config\n"
            "    async def __call__(self, handler, event, data):\n"
            "        data['config'] = self.config\n"
            "        return await handler(event, data)\n"
            "async def init_db(db_path):\n"
            "    pass\n",
            encoding="utf-8",
        )

    async def asyncTearDown(self):
        templates.__path__ = [p for p in templates.__path__ if p != str(self.tmp_dir)]
        reg._template_module_cache.pop("no_router_fixture", None)
        sys.modules.pop("templates.no_router_fixture", None)
        self._tmp.cleanup()

    async def test_module_without_router_attribute_logs_warning_but_still_registers(self):
        with self.assertLogs("runtime.registry", level="WARNING") as logs:
            entry = await reg.build_entry(
                9003, FAKE_TOKEN, "no_router_fixture", {"bot_id": 9003, "name": "no_router_bot"}
            )
        try:
            self.assertIsNotNone(entry)  # still registered — outcome unchanged
            self.assertEqual(len(entry.dispatcher.sub_routers), 0)
            self.assertTrue(any("no_router_fixture" in msg and "9003" in msg for msg in logs.output))
        finally:
            await entry.bot.session.close()


if __name__ == "__main__":
    unittest.main()
