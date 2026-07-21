"""Stage 2 Phase 7 — data isolation test for the tour_operator template's config
переезд (fifth and final reference template).

Standard criterion: two bots on the SAME template, different config, must
never mix data — even driven by the SAME Telegram user_id.

Per the owner's Phase 7 decision (docs/STAGE2_DESIGN.md "Фаза 7"), the web CRM
part (build_web_app()/REST API) is translated onto config for structural
uniformity but is NOT started by the webhook runtime — runtime/registry.py
only registers this template's Telegram router/handlers. The web part is
therefore not covered here (nothing runs in webhook mode to test); the
standalone smoke test below only confirms it still imports/builds fine for
the unchanged subprocess model.

Real external APIs (AssemblyAI transcription, raw Anthropic REST call) are
mocked by patching transcribe_voice()/parse_with_claude() directly — this
template calls them as plain module-level functions (not via an SDK client
object), so patching the functions themselves is the natural mock point, same
spirit as patching Bot.__call__/anthropic.AsyncAnthropic in the other four
templates' isolation tests. No real network calls, no real tokens.

Run with: python -m unittest tests.test_tour_operator_isolation
"""

from __future__ import annotations

import importlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from runtime.registry import get_template_router
from templates import tour_operator

FAKE_TOKEN = "123456:test-token-not-real"
SAME_USER_ID = 111


def _text_update(update_id: int, user_id: int, text: str) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 1700000000,
            "chat": {"id": user_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
            "text": text,
        },
    }


def _voice_update(update_id: int, user_id: int) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 1700000000,
            "chat": {"id": user_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
            "voice": {
                "file_id": "voice_file_id",
                "file_unique_id": "voice_unique",
                "duration": 3,
            },
        },
    }


def _callback_update(update_id: int, user_id: int, data: str) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": str(update_id),
            "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
            "message": {
                "message_id": update_id,
                "date": 1700000000,
                "chat": {"id": user_id, "type": "private"},
                "text": "placeholder",
            },
            "chat_instance": "1",
            "data": data,
        },
    }


def _build_bot_dispatcher(config: tour_operator.TourOperatorConfig) -> tuple[Bot, Dispatcher]:
    """Mirrors runtime/registry.py's build_entry() for this template: fresh
    Dispatcher, cloned Router (Phase 1 fix), the template's own typed
    ConfigMiddleware (Phase 7)."""
    bot = Bot(token=FAKE_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(tour_operator.ConfigMiddleware(config))
    dp.include_router(get_template_router("tour_operator"))
    return bot, dp


async def _create_and_activate_tour(dp: Dispatcher, bot: Bot, user_id: int, name: str, start_id: int) -> int:
    """Drives /newtrip -> name -> destination -> /skip (dates) to create a
    tour and set it active, exactly as a real user would."""
    uid = start_id
    await dp.feed_webhook_update(bot, _text_update(uid, user_id, "/newtrip")); uid += 1
    await dp.feed_webhook_update(bot, _text_update(uid, user_id, name)); uid += 1
    await dp.feed_webhook_update(bot, _text_update(uid, user_id, "Somewhere")); uid += 1
    await dp.feed_webhook_update(bot, _text_update(uid, user_id, "/skip")); uid += 1
    return uid


class TourOperatorIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # on_voice() does `sm = await m.answer(...)` then later `await sm.edit_text(...)`
        # (progressive status message: "Транскрибирую..." -> "Анализирую..." -> result) —
        # the mocked Bot.__call__'s return value needs its own edit_text as an
        # AsyncMock too, or that chained await fails ("MagicMock can't be used
        # in 'await' expression"). Every other call site just discards the
        # return value, so this is the only place that needs it.
        mock_sent_message = MagicMock(edit_text=AsyncMock())
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=mock_sent_message))
        self._bot_call_patcher.start()

        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

        self.config_a = tour_operator.config_from_bot_row(
            {"name": "to_isolation_bot_a", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        self.config_b = tour_operator.config_from_bot_row(
            {"name": "to_isolation_bot_b", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await tour_operator.init_db(self.config_a.db_path)
        await tour_operator.init_db(self.config_b.db_path)

        self.bot_a, self.dp_a = _build_bot_dispatcher(self.config_a)
        self.bot_b, self.dp_b = _build_bot_dispatcher(self.config_b)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_configs_point_to_different_files(self):
        self.assertNotEqual(self.config_a.db_path, self.config_b.db_path)
        self.assertNotEqual(self.config_a.admins_file, self.config_b.admins_file)

    async def test_two_bots_same_user_create_tours_into_separate_db_files(self):
        await _create_and_activate_tour(self.dp_a, self.bot_a, SAME_USER_ID, "Alpha Tour", 1)
        await _create_and_activate_tour(self.dp_b, self.bot_b, SAME_USER_ID, "Beta Tour", 1)

        conn_a = sqlite3.connect(self.config_a.db_path)
        names_a = [r[0] for r in conn_a.execute("SELECT name FROM tours").fetchall()]
        conn_a.close()
        conn_b = sqlite3.connect(self.config_b.db_path)
        names_b = [r[0] for r in conn_b.execute("SELECT name FROM tours").fetchall()]
        conn_b.close()

        self.assertEqual(names_a, ["Alpha Tour"])
        self.assertEqual(names_b, ["Beta Tour"])

    async def test_admin_bootstrap_isolated_per_bot(self):
        await self.dp_a.feed_webhook_update(self.bot_a, _text_update(1, SAME_USER_ID, "/start"))
        await self.dp_b.feed_webhook_update(self.bot_b, _text_update(1, 999, "/start"))

        admins_a = json.loads(Path(self.config_a.admins_file).read_text())
        admins_b = json.loads(Path(self.config_b.admins_file).read_text())

        self.assertEqual(admins_a, [SAME_USER_ID])
        self.assertEqual(admins_b, [999])

    async def test_voice_driven_entry_saved_isolated_per_bot(self):
        """Owner-requested: mock AssemblyAI/Anthropic (transcribe_voice/
        parse_with_claude), drive the voice -> confirm -> save flow on two
        bots with the SAME user_id, prove each entry lands only in its own
        bot's db file."""
        await _create_and_activate_tour(self.dp_a, self.bot_a, SAME_USER_ID, "Alpha Tour", 1)
        await _create_and_activate_tour(self.dp_b, self.bot_b, SAME_USER_ID, "Beta Tour", 1)

        parsed_a = {"type": "location", "data": {"name": "Alpha Waterfall", "region": "North"}, "confidence": 0.9}
        parsed_b = {"type": "location", "data": {"name": "Beta Cave", "region": "South"}, "confidence": 0.9}

        with patch.object(tour_operator, "transcribe_voice", new=AsyncMock(return_value="some speech")), \
             patch.object(tour_operator, "parse_with_claude", new=AsyncMock(side_effect=[parsed_a, parsed_b])):
            await self.dp_a.feed_webhook_update(self.bot_a, _voice_update(10, SAME_USER_ID))
            await self.dp_a.feed_webhook_update(self.bot_a, _callback_update(11, SAME_USER_ID, "vs_save"))

            await self.dp_b.feed_webhook_update(self.bot_b, _voice_update(10, SAME_USER_ID))
            await self.dp_b.feed_webhook_update(self.bot_b, _callback_update(11, SAME_USER_ID, "vs_save"))

        conn_a = sqlite3.connect(self.config_a.db_path)
        names_a = [r[0] for r in conn_a.execute("SELECT name FROM locations").fetchall()]
        conn_a.close()
        conn_b = sqlite3.connect(self.config_b.db_path)
        names_b = [r[0] for r in conn_b.execute("SELECT name FROM locations").fetchall()]
        conn_b.close()

        self.assertEqual(names_a, ["Alpha Waterfall"])
        self.assertEqual(names_b, ["Beta Cave"])


class TourOperatorStandaloneSmokeTest(unittest.TestCase):
    """Confirms the template still imports and initializes fine outside the
    webhook runtime — the subprocess model (bot + its own aiohttp web server)
    must keep working unmodified."""

    def test_config_from_env_matches_legacy_constant_shape(self):
        config = tour_operator.config_from_env()
        self.assertTrue(config.db_path.endswith("tour_operator.db"))
        self.assertTrue(config.admins_file.endswith("admins_tour_operator.json"))
        self.assertEqual(config.bot_name, "tour_operator")

    def test_router_main_and_web_app_entrypoints_exist(self):
        self.assertTrue(hasattr(tour_operator, "router"))
        self.assertTrue(hasattr(tour_operator, "main"))
        self.assertTrue(hasattr(tour_operator, "build_web_app"))

    def test_build_web_app_still_builds_with_a_config(self):
        config = tour_operator.config_from_env()
        app = tour_operator.build_web_app(config)
        self.assertIs(app["config"], config)


class TourOperatorWebCrmFlagTests(unittest.IsolatedAsyncioTestCase):
    """Owner-requested post-review fix (Phase 7): /app and the open_app
    callback must show a clear 'unavailable' message instead of a dead link
    when TOUR_OPERATOR_WEB_ENABLED=false — the flag runtime/registry.py sets
    in its own environment before first loading this template, since it never
    starts the web server there.

    WEB_CRM_ENABLED is a module-level constant evaluated once at import time
    (same as BOT_TOKEN/PORT/BASE_URL), so patch.dict(os.environ, ...) alone
    doesn't change it — this uses importlib.reload() inside the patched
    environment to actually re-evaluate it, then reloads again on teardown to
    restore the module's default state for any other test. The real
    environment is never touched."""

    async def asyncTearDown(self):
        importlib.reload(tour_operator)

    async def _send_app_command(self) -> list[str]:
        """Builds a fresh bot/dispatcher directly from the (possibly just
        reloaded) tour_operator.router, sends /app, and returns every sent
        message's text."""
        bot_call_mock = AsyncMock(return_value=MagicMock())
        with patch.object(Bot, "__call__", new=bot_call_mock):
            with tempfile.TemporaryDirectory() as tmp:
                config = tour_operator.config_from_bot_row(
                    {"name": "to_flag_bot", "display_name": None, "group_chat_id": None}, Path(tmp)
                )
                await tour_operator.init_db(config.db_path)
                bot = Bot(token=FAKE_TOKEN)
                dp = Dispatcher(storage=MemoryStorage())
                dp.update.outer_middleware(tour_operator.ConfigMiddleware(config))
                dp.include_router(tour_operator.router)
                await dp.feed_webhook_update(bot, _text_update(1, SAME_USER_ID, "/app"))
        return [
            call.args[0].text for call in bot_call_mock.call_args_list
            if getattr(call.args[0], "text", None)
        ]

    async def test_app_command_shows_unavailable_message_when_web_crm_disabled(self):
        with patch.dict(os.environ, {"TOUR_OPERATOR_WEB_ENABLED": "false"}):
            importlib.reload(tour_operator)
            self.assertFalse(tour_operator.WEB_CRM_ENABLED)
            sent_texts = await self._send_app_command()

        self.assertTrue(any("недоступно" in t for t in sent_texts))
        self.assertTrue(all("http://" not in t and "https://" not in t for t in sent_texts))

    async def test_app_command_shows_url_by_default(self):
        # Other tests in this file's process (TourOperatorIsolationTests'
        # asyncSetUp calls get_template_router("tour_operator"), which runs
        # the registry's own loader — the same one that permanently sets
        # this env var in a real deployment) may already have set
        # TOUR_OPERATOR_WEB_ENABLED=false as a real, intentional side effect.
        # patch.dict(os.environ) here guarantees a clean "nobody set it" slate
        # for this specific assertion and fully restores whatever was there
        # afterward — the real environment is never left touched.
        with patch.dict(os.environ):
            os.environ.pop("TOUR_OPERATOR_WEB_ENABLED", None)
            importlib.reload(tour_operator)
            self.assertTrue(tour_operator.WEB_CRM_ENABLED)
            sent_texts = await self._send_app_command()
        self.assertTrue(any("http://" in t or "https://" in t for t in sent_texts))


if __name__ == "__main__":
    unittest.main()
