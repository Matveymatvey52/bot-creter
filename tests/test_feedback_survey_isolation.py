"""feedback_survey template — data isolation and collection-flow tests.

Standard criterion: two bots on the SAME template, different config, must
never mix data — even driven by the SAME Telegram admin user_id.

PLUS the design's central differentiator: BOTH a client self-service path
(F.chat.type == "private", no admin-gate on receipt) and an admin
manual-entry path feed the same `feedback` table, distinguished by
`source`/`client_user_id`/`client_label` — and neither path must be reachable
by the wrong actor (a client must never reach the admin panel; an admin's
manual entry must never be attributed to a real client_user_id).

No real Telegram network calls, no real tokens.

Run with: python -m unittest tests.test_feedback_survey_isolation
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

import db.database as db_module
from runtime.registry import get_template_router
from templates import feedback_survey as fb

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


def _callback_update(update_id: int, user_id: int, data: str, msg_id: int = 1) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": str(update_id),
            "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
            "message": {
                "message_id": msg_id, "date": 1700000000,
                "chat": {"id": user_id, "type": "private"}, "text": "placeholder",
            },
            "chat_instance": "1", "data": data,
        },
    }


def _build_bot_dispatcher(config: fb.FeedbackSurveyConfig) -> tuple[Bot, Dispatcher]:
    bot = Bot(token=FAKE_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(fb.ConfigMiddleware(config))
    dp.include_router(get_template_router("feedback_survey"))
    return bot, dp


def _rows(db_path: str) -> list[tuple]:
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT rating, comment, source, client_user_id, client_label FROM feedback ORDER BY id"
    ).fetchall()
    conn.close()
    return rows


class FeedbackSurveyIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

        # cmd_start now syncs the bootstrap admin into db.database.add_bot_admin
        # (central bot_admins table, used by the mini-app's _admin_gate_ok) —
        # must be redirected to a throwaway DB or it hits the real
        # data/bots.db, same reasoning as test_shop_catalog_isolation.py.
        self._central_db_path = self.data_dir / "central_bots.db"
        self._db_path_patcher = patch.object(db_module, "DB_PATH", self._central_db_path)
        self._db_path_patcher.start()
        await db_module.init_db()

        self.config_a = fb.config_from_bot_row(
            {"bot_id": 801, "name": "fb_isolation_bot_a", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        self.config_b = fb.config_from_bot_row(
            {"bot_id": 802, "name": "fb_isolation_bot_b", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await fb.init_db(self.config_a.db_path)
        await fb.init_db(self.config_b.db_path)

        self.bot_a, self.dp_a = _build_bot_dispatcher(self.config_a)
        self.bot_b, self.dp_b = _build_bot_dispatcher(self.config_b)
        # Bootstrap the SAME admin user_id on both bots.
        await self.dp_a.feed_webhook_update(self.bot_a, _text_update(1, ADMIN_ID, "/start"))
        await self.dp_b.feed_webhook_update(self.bot_b, _text_update(1, ADMIN_ID, "/start"))

    async def asyncTearDown(self):
        self._db_path_patcher.stop()
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_configs_point_to_different_files(self):
        self.assertNotEqual(self.config_a.db_path, self.config_b.db_path)

    async def test_two_bots_same_client_ratings_not_mixed(self):
        # Same CLIENT_ID rates bot A 5 stars (no comment) and bot B 2 stars (with comment).
        await self.dp_a.feed_webhook_update(self.bot_a, _text_update(10, CLIENT_ID, "/start"))
        await self.dp_a.feed_webhook_update(self.bot_a, _callback_update(11, CLIENT_ID, "fb_rate:5"))
        await self.dp_a.feed_webhook_update(self.bot_a, _callback_update(12, CLIENT_ID, "fb_comment_skip"))

        await self.dp_b.feed_webhook_update(self.bot_b, _text_update(10, CLIENT_ID, "/start"))
        await self.dp_b.feed_webhook_update(self.bot_b, _callback_update(11, CLIENT_ID, "fb_rate:2"))
        await self.dp_b.feed_webhook_update(self.bot_b, _text_update(12, CLIENT_ID, "Было плохо"))

        rows_a = _rows(self.config_a.db_path)
        rows_b = _rows(self.config_b.db_path)
        self.assertEqual(rows_a, [(5, None, "client", CLIENT_ID, None)])
        self.assertEqual(rows_b, [(2, "Было плохо", "client", CLIENT_ID, None)])

    async def test_non_owner_messaging_first_does_not_become_admin(self):
        """Security fix: previously, whoever sent /start FIRST permanently
        became the bot admin — a client testing the bot link before the
        owner did would silently seize the admin panel. When
        bots.owner_telegram_id is known, only that user may claim the
        bootstrap admin slot."""
        config = fb.config_from_bot_row(
            {"bot_id": 803, "name": "fb_owned_bot", "display_name": None,
             "group_chat_id": None, "owner_telegram_id": 12345},
            self.data_dir,
        )
        await fb.init_db(config.db_path)
        bot, dp = _build_bot_dispatcher(config)

        CLIENT_FIRST_ID = 5551  # not the owner, messages first
        await dp.feed_webhook_update(bot, _text_update(1, CLIENT_FIRST_ID, "/start"))
        self.assertEqual(fb._load_admins(config.admins_file), set())
        self.assertFalse(fb._is_admin(CLIENT_FIRST_ID, config))

        await dp.feed_webhook_update(bot, _text_update(2, 12345, "/start"))
        self.assertTrue(fb._is_admin(12345, config))
        self.assertEqual(fb._load_admins(config.admins_file), {"12345"})

    async def test_owner_is_always_admin_even_with_stale_admins_file(self):
        """Defense in depth: the DB-known owner must see the admin panel even
        if the local admins_file is empty/stale (e.g. wiped, or hijacked by a
        prior bug) — owner_telegram_id is treated as an unconditional admin
        in _is_admin, not just at bootstrap time."""
        config = fb.config_from_bot_row(
            {"bot_id": 804, "name": "fb_owned_bot_2", "display_name": None,
             "group_chat_id": None, "owner_telegram_id": 777},
            self.data_dir,
        )
        await fb.init_db(config.db_path)
        fb._save_admins(config.admins_file, {"999999"})  # some other id, not the owner
        self.assertTrue(fb._is_admin(777, config))  # owner: always admin
        self.assertTrue(fb._is_admin(999999, config))  # still honors the file's own admin
        self.assertFalse(fb._is_admin(4242, config))  # neither owner nor in the file

    async def test_bootstrap_admin_syncs_to_central_bot_admins_table(self):
        """The mini-app's admin gate (runtime.miniapp_api._admin_gate_ok)
        checks db.database.get_bot_admins(), a separate table from this
        template's local admins_file. The bootstrap grant must land in both,
        or the owner gets the Telegram admin panel but is locked out of the
        mini-app admin views."""
        config = fb.config_from_bot_row(
            {"bot_id": 805, "name": "fb_synced_bot", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await fb.init_db(config.db_path)
        bot, dp = _build_bot_dispatcher(config)
        await dp.feed_webhook_update(bot, _text_update(1, 321, "/start"))

        central_admins = await db_module.get_bot_admins(805)
        self.assertEqual(central_admins, ["321"])


class FeedbackSurveyCollectionFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = fb.config_from_bot_row(
            {"bot_id": 803, "name": "fb_flow_bot", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await fb.init_db(self.config.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)
        await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/start"))

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_client_self_service_full_flow_with_comment(self):
        await self.dp.feed_webhook_update(self.bot, _text_update(10, CLIENT_ID, "/start"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(11, CLIENT_ID, "fb_rate:4"))
        await self.dp.feed_webhook_update(self.bot, _text_update(12, CLIENT_ID, "Заходили в среду, всё понравилось"))

        rows = _rows(self.config.db_path)
        self.assertEqual(rows, [(4, "Заходили в среду, всё понравилось", "client", CLIENT_ID, None)])

    async def test_client_self_service_skip_comment(self):
        await self.dp.feed_webhook_update(self.bot, _text_update(10, CLIENT_ID, "/start"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(11, CLIENT_ID, "fb_rate:3"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(12, CLIENT_ID, "fb_comment_skip"))

        rows = _rows(self.config.db_path)
        self.assertEqual(rows, [(3, None, "client", CLIENT_ID, None)])

    async def test_admin_manual_entry_with_label_and_comment(self):
        await self.dp.feed_webhook_update(self.bot, _callback_update(10, ADMIN_ID, "fb_admin_new"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(11, ADMIN_ID, "fb_rate:5"))
        await self.dp.feed_webhook_update(self.bot, _text_update(12, ADMIN_ID, "Анна, по телефону"))
        await self.dp.feed_webhook_update(self.bot, _text_update(13, ADMIN_ID, "Очень довольна сервисом"))

        rows = _rows(self.config.db_path)
        self.assertEqual(
            rows, [(5, "Очень довольна сервисом", "admin", None, "Анна, по телефону")]
        )

    async def test_admin_manual_entry_skip_label_and_comment(self):
        await self.dp.feed_webhook_update(self.bot, _callback_update(10, ADMIN_ID, "fb_admin_new"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(11, ADMIN_ID, "fb_rate:1"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(12, ADMIN_ID, "fb_label_skip"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(13, ADMIN_ID, "fb_comment_skip"))

        rows = _rows(self.config.db_path)
        self.assertEqual(rows, [(1, None, "admin", None, None)])

    async def test_flow_cancel_mid_client_flow_does_not_insert(self):
        await self.dp.feed_webhook_update(self.bot, _text_update(10, CLIENT_ID, "/start"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(11, CLIENT_ID, "fb_rate:2"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(12, CLIENT_ID, "fb_flow_cancel"))

        self.assertEqual(_rows(self.config.db_path), [])

    async def test_out_of_range_rating_callback_is_ignored(self):
        """A hand-crafted client could send an arbitrary callback_data value
        outside the keyboard's actual buttons — must be rejected, not
        silently clamped or inserted."""
        await self.dp.feed_webhook_update(self.bot, _text_update(10, CLIENT_ID, "/start"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(11, CLIENT_ID, "fb_rate:999"))

        self.assertEqual(_rows(self.config.db_path), [])

    async def test_non_admin_cannot_reach_stats_or_admin_panel(self):
        await self.dp.feed_webhook_update(self.bot, _callback_update(10, CLIENT_ID, "fb_stats"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(11, CLIENT_ID, "fb_admin_new"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(12, CLIENT_ID, "adm_menu"))

        # None of these admin-gated actions should have inserted a row, and
        # CLIENT_ID must still not be a registered admin.
        self.assertEqual(_rows(self.config.db_path), [])
        self.assertFalse(fb._is_admin(CLIENT_ID, self.config))

    async def test_client_whitespace_only_comment_is_treated_as_skipped(self):
        """A comment that's only whitespace after strip() must store NULL
        (like the explicit skip button), not an empty string that would
        otherwise show up as a blank line in the stats' recent-comments list."""
        await self.dp.feed_webhook_update(self.bot, _text_update(10, CLIENT_ID, "/start"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(11, CLIENT_ID, "fb_rate:4"))
        await self.dp.feed_webhook_update(self.bot, _text_update(12, CLIENT_ID, "   "))

        rows = _rows(self.config.db_path)
        self.assertEqual(rows, [(4, None, "client", CLIENT_ID, None)])

    async def test_negative_admin_remove_index_is_rejected_not_wrapped(self):
        """Python's negative-index semantics would otherwise let a
        hand-crafted "adm_rm:-1" silently resolve to ids[-1] (the LAST admin)
        instead of being rejected as an invalid pick."""
        ids = fb._load_admins(self.config.admins_file)
        ids.add("111222333")
        fb._save_admins(self.config.admins_file, ids)

        await self.dp.feed_webhook_update(self.bot, _callback_update(10, ADMIN_ID, "adm_remove"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(11, ADMIN_ID, "adm_rm:-1"))

        remaining = fb._load_admins(self.config.admins_file)
        self.assertEqual(remaining, {str(ADMIN_ID), "111222333"}, "a negative index removed an admin instead of being rejected")

    async def test_double_tap_comment_skip_inserts_only_once(self):
        """After the first skip finalizes the row, state.clear() means the
        FSM no longer matches RateFlow.comment — a stale re-tap of the same
        button must be a no-op, not a duplicate row."""
        await self.dp.feed_webhook_update(self.bot, _text_update(10, CLIENT_ID, "/start"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(11, CLIENT_ID, "fb_rate:5"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(12, CLIENT_ID, "fb_comment_skip"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(13, CLIENT_ID, "fb_comment_skip"))

        self.assertEqual(_rows(self.config.db_path), [(5, None, "client", CLIENT_ID, None)])


class FeedbackSurveyStandaloneSmokeTest(unittest.TestCase):
    def test_config_from_env_matches_legacy_constant_shape(self):
        config = fb.config_from_env()
        self.assertTrue(config.db_path.endswith("feedback_survey_data.db"))
        self.assertEqual(config.bot_name, "feedback_survey")

    def test_router_and_main_entrypoint_exist(self):
        self.assertTrue(hasattr(fb, "router"))
        self.assertTrue(hasattr(fb, "main"))


if __name__ == "__main__":
    unittest.main()
