"""Stage 2 Phase 2 — data isolation test for the accountant template's config переезд.

Main criterion of this phase: two bots on the SAME template, registered with
different config, must never write to each other's SQLite file — even when
driven by the SAME Telegram user id (the worst case for accidental global state).

No real Telegram network calls, no real tokens (aiogram doesn't validate the
token string at Bot() construction time).

Run with: python -m unittest tests.test_accountant_isolation
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from runtime.registry import get_template_router
from templates import accountant

FAKE_TOKEN = "123456:test-token-not-real"
SAME_USER_ID = 111  # deliberately identical across both bots — the case most
                     # likely to reveal accidental shared/global state


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


def _build_bot_dispatcher(config: accountant.AccountantConfig) -> tuple[Bot, Dispatcher]:
    """Mirrors exactly what runtime/registry.py's build_entry() does for an
    accountant-templated bot: fresh Dispatcher, cloned Router (Phase 1's fix —
    the same Router object can't attach to two Dispatchers), accountant's own
    typed ConfigMiddleware (Phase 2)."""
    bot = Bot(token=FAKE_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(accountant.ConfigMiddleware(config))
    dp.include_router(get_template_router("accountant"))
    return bot, dp


class AccountantIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Every Bot API call (message.answer, cb.answer, edit_text, ...) ultimately
        # goes through Bot.__call__ — patch it so handlers never make a real
        # network request to Telegram with the fake token (this is what caused
        # the test to hang before this fix).
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()

        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

        self.config_a = accountant.config_from_bot_row(
            {"name": "isolation_bot_a", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        self.config_b = accountant.config_from_bot_row(
            {"name": "isolation_bot_b", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await accountant.init_db(self.config_a.db_path)
        await accountant.init_db(self.config_b.db_path)

        self.bot_a, self.dp_a = _build_bot_dispatcher(self.config_a)
        self.bot_b, self.dp_b = _build_bot_dispatcher(self.config_b)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_configs_point_to_different_files(self):
        self.assertNotEqual(self.config_a.db_path, self.config_b.db_path)
        self.assertNotEqual(self.config_a.admins_file, self.config_b.admins_file)

    async def test_two_bots_same_user_write_to_separate_db_files(self):
        # Bot A: same user creates project "Alpha Project"
        await self.dp_a.feed_webhook_update(self.bot_a, _callback_update(1, SAME_USER_ID, "proj_new"))
        await self.dp_a.feed_webhook_update(self.bot_a, _text_update(2, SAME_USER_ID, "Alpha Project"))

        # Bot B: SAME user creates a DIFFERENTLY named project "Beta Project"
        await self.dp_b.feed_webhook_update(self.bot_b, _callback_update(1, SAME_USER_ID, "proj_new"))
        await self.dp_b.feed_webhook_update(self.bot_b, _text_update(2, SAME_USER_ID, "Beta Project"))

        conn_a = sqlite3.connect(self.config_a.db_path)
        names_a = [r[0] for r in conn_a.execute("SELECT name FROM projects").fetchall()]
        conn_a.close()

        conn_b = sqlite3.connect(self.config_b.db_path)
        names_b = [r[0] for r in conn_b.execute("SELECT name FROM projects").fetchall()]
        conn_b.close()

        # The core assertion: each bot's file has ONLY its own data — no mixing.
        self.assertEqual(names_a, ["Alpha Project"])
        self.assertEqual(names_b, ["Beta Project"])

    async def test_admin_bootstrap_isolated_per_bot(self):
        await self.dp_a.feed_webhook_update(self.bot_a, _text_update(1, SAME_USER_ID, "/start"))
        await self.dp_b.feed_webhook_update(self.bot_b, _text_update(1, 999, "/start"))

        admins_a = accountant._load_admins(self.config_a.admins_file)
        admins_b = accountant._load_admins(self.config_b.admins_file)

        self.assertEqual(admins_a, {str(SAME_USER_ID)})
        self.assertEqual(admins_b, {"999"})
        self.assertNotEqual(admins_a, admins_b)


class AccountantStandaloneSmokeTest(unittest.TestCase):
    """Confirms the template still imports and initializes fine outside the
    webhook runtime — the subprocess model must keep working unmodified."""

    def test_config_from_env_matches_legacy_constant_shape(self):
        config = accountant.config_from_env()
        self.assertTrue(config.db_path.endswith("accountant_data.db"))
        self.assertTrue(str(config.admins_file).endswith("admins_accountant.json"))
        self.assertEqual(config.bot_name, "accountant")

    def test_router_and_main_entrypoint_exist(self):
        self.assertTrue(hasattr(accountant, "router"))
        self.assertTrue(hasattr(accountant, "main"))


if __name__ == "__main__":
    unittest.main()
