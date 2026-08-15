"""build_entry()'s by-convention on_office_event wiring (runtime/registry.py),
per docs/OFFICES_DESIGN.md §3 — proves a template exposing a module-level
on_office_event(event, config) coroutine gets it registered into
BotEntry.config["on_office_event"], reachable by features/office_events.py's
publish_event(), and that a template WITHOUT one is unaffected.

Same fixture-template-via-templates.__path__ technique as
tests/test_template_dynamic_registry.py — see that file's module docstring
for why (runtime/registry.py resolves templates via
importlib.import_module("templates.<id>"), not a scannable directory).

Run with: python -m pytest tests/test_office_events_registry_wiring.py
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import templates
import db.database as db_module
import runtime.registry as reg

FAKE_TOKEN = "123456789:AAHfakeTokenButShapedRight1234567890"

_FIXTURE_WITH_HOOK = '''
# TEMPLATE: fixture_office_events_template
# USE FOR: a fixture template exposing on_office_event, for registry wiring tests
from dataclasses import dataclass
from aiogram import Router

router = Router()
received_events = []


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


async def on_office_event(event, config):
    received_events.append((event, config))
'''

_FIXTURE_WITHOUT_HOOK = '''
# TEMPLATE: fixture_office_events_no_hook_template
# USE FOR: a fixture template with NO on_office_event, for registry wiring tests
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


class BuildEntryRegistersOfficeEventHook(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self._tmp.name)
        templates.__path__.append(str(self.tmp_dir))
        (self.tmp_dir / "fixture_office_events_template.py").write_text(_FIXTURE_WITH_HOOK, encoding="utf-8")
        (self.tmp_dir / "fixture_office_events_no_hook_template.py").write_text(
            _FIXTURE_WITHOUT_HOOK, encoding="utf-8"
        )

        # build_entry() calls _load_and_include_features(), which reads
        # bot_features from db.database.DB_PATH — must be redirected to a
        # throwaway DB, same as tests/test_template_dynamic_registry.py's
        # RealTemplatesStillRegisterCorrectly, or this hits the real
        # data/bots.db (see MEMORY.md "Backlog: test DB isolation").
        self._data_dir = Path(self._tmp.name) / "data"
        self._data_dir.mkdir()
        self._config_patcher = patch("config.DATA_DIR", self._data_dir)
        self._config_patcher.start()
        self._db_path_patcher = patch.object(db_module, "DB_PATH", self._data_dir / "bots.db")
        self._db_path_patcher.start()
        await db_module.init_db()

    async def asyncTearDown(self):
        self._db_path_patcher.stop()
        self._config_patcher.stop()
        templates.__path__ = [p for p in templates.__path__ if p != str(self.tmp_dir)]
        for name in ("fixture_office_events_template", "fixture_office_events_no_hook_template"):
            reg._template_module_cache.pop(name, None)
            sys.modules.pop(f"templates.{name}", None)
        self._tmp.cleanup()

    async def test_hook_is_registered_and_carries_the_correct_typed_config(self):
        entry = await reg.build_entry(
            9101, FAKE_TOKEN, "fixture_office_events_template", {"bot_id": 9101, "name": "fixture_bot"}
        )
        try:
            hook = entry.config.get("on_office_event")
            self.assertIsNotNone(hook)

            from features.office_events import OfficeEvent, OrderCreatedEvent

            event = OfficeEvent(
                event_type="order.created",
                source_bot_id=1,
                payload=OrderCreatedEvent(order_id=1, amount=100, currency="RUB", customer_chat_id=555),
            )
            await hook(event)

            fixture_module = sys.modules["templates.fixture_office_events_template"]
            self.assertEqual(len(fixture_module.received_events), 1)
            received_event, received_config = fixture_module.received_events[0]
            self.assertIs(received_event, event)
            # The template's OWN typed config (from config_from_bot_row), not the
            # raw registry dict — same config shape its other handlers receive.
            self.assertEqual(received_config.bot_id, 9101)
        finally:
            await entry.bot.session.close()

    async def test_template_without_hand_written_hook_gets_the_generic_fallback(self):
        # docs/OFFICES_DESIGN.md §11 — a template-resolved bot (has typed_config
        # via config_from_bot_row) with no hand-written on_office_event still
        # gets SOME hook wired now (the generic one), not none at all — this
        # is the whole point of the auto-generated fallback. See
        # tests/test_office_events_generic_hook.py for the generic hook's own
        # behavior in isolation.
        entry = await reg.build_entry(
            9102,
            FAKE_TOKEN,
            "fixture_office_events_no_hook_template",
            {"bot_id": 9102, "name": "fixture_bot_no_hook"},
        )
        try:
            self.assertIn("on_office_event", entry.config)
        finally:
            await entry.bot.session.close()

    async def test_generic_fallback_records_a_note_with_no_office_hook_config_row(self):
        # No bot_office_hook_config row was ever written for bot_id=9103 —
        # the generic hook must still record its plain fallback note (never
        # crash, never no-op silently) via features/office_events.py's own
        # office_notes CREATE TABLE IF NOT EXISTS.
        import sqlite3

        from features.office_events import OfficeEvent, OrderCreatedEvent

        entry = await reg.build_entry(
            9103,
            FAKE_TOKEN,
            "fixture_office_events_no_hook_template",
            {"bot_id": 9103, "name": "fixture_bot_no_hook_2"},
        )
        try:
            hook = entry.config["on_office_event"]
            event = OfficeEvent(
                event_type="order.created",
                source_bot_id=1,
                payload=OrderCreatedEvent(order_id=1, amount=100, currency="RUB", customer_chat_id=555),
            )
            await hook(event)

            db_path = self._data_dir / "bot_9103_fixture.db"
            conn = sqlite3.connect(str(db_path))
            rows = conn.execute("SELECT source_bot_id, event_type, note FROM office_notes").fetchall()
            conn.close()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][0], 1)
            self.assertEqual(rows[0][1], "order.created")
        finally:
            await entry.bot.session.close()
