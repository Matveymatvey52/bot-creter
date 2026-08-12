"""support_tickets template — data isolation between two bot instances.

Standard criterion: two bots on the SAME template, different config, must
never mix data — even driven by the SAME Telegram user_id (both as admin and
as client). Covers tickets, ticket_messages, and kb_articles.

No real Telegram network calls, no real tokens.

Run with: python -m unittest tests.test_support_tickets_isolation
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
from templates import support_tickets as st

FAKE_TOKEN = "123456:test-token-not-real"
ADMIN_ID = 999
CLIENT_ID = 555


def _text_update(update_id: int, user_id: int, text: str) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id, "date": 1700000000,
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
                "message_id": update_id, "date": 1700000000,
                "chat": {"id": user_id, "type": "private"}, "text": "placeholder",
            },
            "chat_instance": "1", "data": data,
        },
    }


def _build_bot_dispatcher(config: st.SupportTicketsConfig) -> tuple[Bot, Dispatcher]:
    bot = Bot(token=FAKE_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(st.ConfigMiddleware(config))
    dp.include_router(get_template_router("support_tickets"))
    return bot, dp


async def _create_ticket_via_flow(dp: Dispatcher, bot: Bot, client_id: int, category: str, text: str, start_update_id: int) -> int:
    uid = start_update_id
    await dp.feed_webhook_update(bot, _text_update(uid, client_id, "/start")); uid += 1
    await dp.feed_webhook_update(bot, _callback_update(uid, client_id, "cli_support")); uid += 1
    await dp.feed_webhook_update(bot, _callback_update(uid, client_id, f"cli_cat:{category}")); uid += 1
    await dp.feed_webhook_update(bot, _callback_update(uid, client_id, f"cli_new_ticket:{category}")); uid += 1
    await dp.feed_webhook_update(bot, _text_update(uid, client_id, text)); uid += 1
    return uid


class SupportTicketsIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

        self.config_a = st.config_from_bot_row(
            {"bot_id": 901, "name": "support_bot_a", "display_name": None, "group_chat_id": None}, self.data_dir,
        )
        self.config_b = st.config_from_bot_row(
            {"bot_id": 902, "name": "support_bot_b", "display_name": None, "group_chat_id": None}, self.data_dir,
        )
        await st.init_db(self.config_a.db_path)
        await st.init_db(self.config_b.db_path)

        self.bot_a, self.dp_a = _build_bot_dispatcher(self.config_a)
        self.bot_b, self.dp_b = _build_bot_dispatcher(self.config_b)
        # Bootstrap the SAME admin user_id on both bots.
        await self.dp_a.feed_webhook_update(self.bot_a, _text_update(1, ADMIN_ID, "/start"))
        await self.dp_b.feed_webhook_update(self.bot_b, _text_update(1, ADMIN_ID, "/start"))

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_configs_point_to_different_files(self):
        self.assertNotEqual(self.config_a.db_path, self.config_b.db_path)

    async def test_tickets_created_by_same_client_id_on_different_bots_stay_separate(self):
        await _create_ticket_via_flow(self.dp_a, self.bot_a, CLIENT_ID, "billing", "Проблема A", 10)
        await _create_ticket_via_flow(self.dp_b, self.bot_b, CLIENT_ID, "technical", "Проблема B", 10)

        conn_a = sqlite3.connect(self.config_a.db_path)
        tickets_a = conn_a.execute("SELECT category FROM tickets").fetchall()
        msgs_a = conn_a.execute("SELECT text FROM ticket_messages").fetchall()
        conn_a.close()
        conn_b = sqlite3.connect(self.config_b.db_path)
        tickets_b = conn_b.execute("SELECT category FROM tickets").fetchall()
        msgs_b = conn_b.execute("SELECT text FROM ticket_messages").fetchall()
        conn_b.close()

        self.assertEqual(tickets_a, [("billing",)])
        self.assertEqual(tickets_b, [("technical",)])
        self.assertEqual(msgs_a, [("Проблема A",)])
        self.assertEqual(msgs_b, [("Проблема B",)])

    async def test_kb_articles_do_not_leak_between_bots(self):
        async with __import__("aiosqlite").connect(self.config_a.db_path) as db:
            await db.execute(
                "INSERT INTO kb_articles (category, question, answer) VALUES ('billing', 'Q-A', 'Answer A')"
            )
            await db.commit()

        conn_a = sqlite3.connect(self.config_a.db_path)
        count_a = conn_a.execute("SELECT COUNT(*) FROM kb_articles").fetchone()[0]
        conn_a.close()
        conn_b = sqlite3.connect(self.config_b.db_path)
        count_b = conn_b.execute("SELECT COUNT(*) FROM kb_articles").fetchone()[0]
        conn_b.close()

        self.assertEqual(count_a, 1)
        self.assertEqual(count_b, 0)

    async def test_admins_file_is_separate_per_bot(self):
        # ADMIN_ID was bootstrapped on both — adding a SECOND admin only to
        # bot A must not appear on bot B's admin list.
        second_admin = 42
        await self.dp_a.feed_webhook_update(self.bot_a, _callback_update(20, ADMIN_ID, "adm_menu"))
        await self.dp_a.feed_webhook_update(self.bot_a, _callback_update(21, ADMIN_ID, "adm_add"))
        await self.dp_a.feed_webhook_update(self.bot_a, _text_update(22, ADMIN_ID, str(second_admin)))

        self.assertIn(str(second_admin), st._load_admins(self.config_a.admins_file))
        self.assertNotIn(str(second_admin), st._load_admins(self.config_b.admins_file))


class SupportTicketsStandaloneSmokeTest(unittest.TestCase):
    def test_config_from_env_matches_legacy_constant_shape(self):
        config = st.config_from_env()
        self.assertTrue(config.db_path.endswith("support_tickets_data.db"))
        self.assertEqual(config.bot_name, "support_tickets")

    def test_router_and_main_entrypoint_exist(self):
        self.assertTrue(hasattr(st, "router"))
        self.assertTrue(hasattr(st, "main"))


if __name__ == "__main__":
    unittest.main()
