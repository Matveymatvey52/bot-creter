"""Stage 2 Phase 4 — data isolation test for the manager_secretary template's
config переезд (second reference template, after accountant in Phase 2).

Same criterion as test_accountant_isolation.py: two bots on the SAME template,
different config, must never mix data — even driven by the SAME Telegram user_id.

PLUS (explicitly requested before Task 4): manager_secretary has a live
handler (handle_group_mention) that was decorative in accountant — it puts the
bot's own display_name into the Anthropic system prompt. A naive translation
could leave that read from a shared module constant instead of config, in
which case every bot on this template would introduce itself with the SAME
(last-built) name in groups. This test proves that doesn't happen: two bots
with DIFFERENT display_name, each mentioned in a group chat, must each send a
system prompt containing ITS OWN name — checked via a mocked Anthropic client
(no real network/API calls), not by checking the (also mocked) reply text.

No real Telegram/Anthropic network calls, no real tokens.

Run with: python -m unittest tests.test_manager_secretary_isolation
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from runtime.registry import get_template_router
from templates import manager_secretary

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


def _group_mention_update(update_id: int, chat_id: int, user_id: int, text: str) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 1700000000,
            "chat": {"id": chat_id, "type": "group", "title": "Test Group"},
            "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
            "text": text,
        },
    }


def _build_bot_dispatcher(config: manager_secretary.ManagerSecretaryConfig) -> tuple[Bot, Dispatcher]:
    """Mirrors runtime/registry.py's build_entry() for this template: fresh
    Dispatcher, cloned Router (Phase 1 fix), the template's own typed
    ConfigMiddleware (Phase 4)."""
    bot = Bot(token=FAKE_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(manager_secretary.ConfigMiddleware(config))
    dp.include_router(get_template_router("manager_secretary"))
    return bot, dp


class ManagerSecretaryIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()

        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

        self.config_a = manager_secretary.config_from_bot_row(
            {"name": "ms_isolation_bot_a", "display_name": "Ася", "group_chat_id": None}, self.data_dir
        )
        self.config_b = manager_secretary.config_from_bot_row(
            {"name": "ms_isolation_bot_b", "display_name": "Боря", "group_chat_id": None}, self.data_dir
        )
        await manager_secretary.init_db(self.config_a.db_path)
        await manager_secretary.init_db(self.config_b.db_path)

        self.bot_a, self.dp_a = _build_bot_dispatcher(self.config_a)
        self.bot_b, self.dp_b = _build_bot_dispatcher(self.config_b)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_configs_point_to_different_files(self):
        self.assertNotEqual(self.config_a.db_path, self.config_b.db_path)
        self.assertNotEqual(self.config_a.admins_file, self.config_b.admins_file)
        self.assertNotEqual(self.config_a.display_name, self.config_b.display_name)

    async def test_two_bots_same_user_write_leads_to_separate_db_files(self):
        # Bot A: same user leaves a lead with name "Alpha Lead"
        await self.dp_a.feed_webhook_update(self.bot_a, _text_update(1, SAME_USER_ID, "📝 Оставить заявку"))
        await self.dp_a.feed_webhook_update(self.bot_a, _text_update(2, SAME_USER_ID, "Alpha Lead"))
        await self.dp_a.feed_webhook_update(self.bot_a, _text_update(3, SAME_USER_ID, "+7 999 111-11-11"))
        await self.dp_a.feed_webhook_update(self.bot_a, _text_update(4, SAME_USER_ID, "/skip"))

        # Bot B: SAME user leaves a DIFFERENTLY named lead "Beta Lead"
        await self.dp_b.feed_webhook_update(self.bot_b, _text_update(1, SAME_USER_ID, "📝 Оставить заявку"))
        await self.dp_b.feed_webhook_update(self.bot_b, _text_update(2, SAME_USER_ID, "Beta Lead"))
        await self.dp_b.feed_webhook_update(self.bot_b, _text_update(3, SAME_USER_ID, "+7 999 222-22-22"))
        await self.dp_b.feed_webhook_update(self.bot_b, _text_update(4, SAME_USER_ID, "/skip"))

        conn_a = sqlite3.connect(self.config_a.db_path)
        names_a = [r[0] for r in conn_a.execute("SELECT name FROM leads").fetchall()]
        conn_a.close()

        conn_b = sqlite3.connect(self.config_b.db_path)
        names_b = [r[0] for r in conn_b.execute("SELECT name FROM leads").fetchall()]
        conn_b.close()

        self.assertEqual(names_a, ["Alpha Lead"])
        self.assertEqual(names_b, ["Beta Lead"])

    async def test_admin_bootstrap_isolated_per_bot(self):
        await self.dp_a.feed_webhook_update(self.bot_a, _text_update(1, SAME_USER_ID, "/start"))
        await self.dp_b.feed_webhook_update(self.bot_b, _text_update(1, 999, "/start"))

        admins_a = json.loads(self.config_a.admins_file.read_text())["ids"]
        admins_b = json.loads(self.config_b.admins_file.read_text())["ids"]

        self.assertEqual(admins_a, [str(SAME_USER_ID)])
        self.assertEqual(admins_b, ["999"])

    async def test_faqs_seeded_per_bot_db_not_shared(self):
        conn_a = sqlite3.connect(self.config_a.db_path)
        count_a = conn_a.execute("SELECT COUNT(*) FROM faqs").fetchone()[0]
        conn_a.close()
        conn_b = sqlite3.connect(self.config_b.db_path)
        count_b = conn_b.execute("SELECT COUNT(*) FROM faqs").fetchone()[0]
        conn_b.close()

        # Same seed content (accepted # CUSTOMIZE limitation), but each bot got
        # its OWN copy in its OWN file — not a single shared/global seed.
        self.assertEqual(count_a, len(manager_secretary.FAQS))
        self.assertEqual(count_b, len(manager_secretary.FAQS))
        self.assertNotEqual(self.config_a.db_path, self.config_b.db_path)

    async def test_group_mention_uses_own_display_name_in_claude_prompt(self):
        """The point explicitly requested before Task 4: prove config.display_name
        actually reaches the Anthropic call per-bot, not a shared constant."""
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(
            return_value=MagicMock(content=[MagicMock(text="mocked reply")])
        )

        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            # Bot A (display_name="Ася") is mentioned in a group.
            await self.dp_a.feed_webhook_update(
                self.bot_a, _group_mention_update(1, 5001, SAME_USER_ID, "Привет, Ася, ты тут?")
            )
            self.assertEqual(mock_client.messages.create.call_count, 1)
            system_prompt_a = mock_client.messages.create.call_args.kwargs["system"]
            self.assertIn("Ася", system_prompt_a)
            self.assertNotIn("Боря", system_prompt_a)

            mock_client.messages.create.reset_mock()

            # Bot B (display_name="Боря") is mentioned in a DIFFERENT group.
            await self.dp_b.feed_webhook_update(
                self.bot_b, _group_mention_update(1, 5002, SAME_USER_ID, "Боря, привет!")
            )
            self.assertEqual(mock_client.messages.create.call_count, 1)
            system_prompt_b = mock_client.messages.create.call_args.kwargs["system"]
            self.assertIn("Боря", system_prompt_b)
            self.assertNotIn("Ася", system_prompt_b)

    async def test_group_mention_of_wrong_name_does_not_trigger(self):
        """Bot A should NOT respond to a mention of bot B's name — proves the
        trigger check itself reads config.display_name, not a stale/shared one."""
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(
            return_value=MagicMock(content=[MagicMock(text="mocked reply")])
        )
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            await self.dp_a.feed_webhook_update(
                self.bot_a, _group_mention_update(1, 5003, SAME_USER_ID, "Боря, привет!")
            )
            self.assertEqual(mock_client.messages.create.call_count, 0)


class ManagerSecretaryStandaloneSmokeTest(unittest.TestCase):
    """Confirms the template still imports and initializes fine outside the
    webhook runtime — the subprocess model must keep working unmodified."""

    def test_config_from_env_matches_legacy_constant_shape(self):
        config = manager_secretary.config_from_env()
        self.assertTrue(config.db_path.endswith("manager_secretary_data.db"))
        self.assertTrue(str(config.admins_file).endswith("admins_manager_secretary.json"))
        self.assertEqual(config.bot_name, "manager_secretary")

    def test_router_and_main_entrypoint_exist(self):
        self.assertTrue(hasattr(manager_secretary, "router"))
        self.assertTrue(hasattr(manager_secretary, "main"))


if __name__ == "__main__":
    unittest.main()
