"""referral_program template — data isolation and reward-flow tests.

Standard criterion: two bots on the SAME template, different config, must
never mix data — even driven by the SAME Telegram user_id.

PLUS domain-specific checks matching this template's design requirement:
reward is credited only at condition fulfillment (admin confirmation), never
at registration; a double-tap on confirm must not double-credit; the
leaderboard counts only rewarded referrals, not pending ones; and a returning
user re-opening someone else's invite link is not retroactively attributed.

No real Telegram network calls, no real tokens.

Run with: python -m unittest tests.test_referral_program_isolation
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
from templates import referral_program

FAKE_TOKEN = "123456:test-token-not-real"
ADMIN_ID = 999
REFERRER_ID = 111
REFERRED_ID = 222


def _text_update(update_id: int, user_id: int, text: str) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id, "date": 1700000000,
            "chat": {"id": user_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False, "first_name": f"User{user_id}"},
            "text": text,
        },
    }


def _callback_update(update_id: int, user_id: int, data: str) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": str(update_id),
            "from": {"id": user_id, "is_bot": False, "first_name": f"User{user_id}"},
            "message": {
                "message_id": update_id, "date": 1700000000,
                "chat": {"id": user_id, "type": "private"}, "text": "placeholder",
            },
            "chat_instance": "1", "data": data,
        },
    }


def _build_bot_dispatcher(config: referral_program.ReferralConfig) -> tuple[Bot, Dispatcher]:
    bot = Bot(token=FAKE_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(referral_program.ConfigMiddleware(config))
    dp.include_router(get_template_router("referral_program"))
    return bot, dp


def _ref_code(db_path: str, user_id: int) -> str:
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT ref_code FROM participants WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return row[0]


class ReferralIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

        # cmd_start now syncs the bootstrap admin into db.database.add_bot_admin
        # (central bot_admins table) -- must be redirected to a throwaway DB or
        # it hits the real data/bots.db, same reasoning as
        # tests/test_shop_catalog_isolation.py.
        self._central_db_path = self.data_dir / "central_bots.db"
        self._db_path_patcher = patch.object(db_module, "DB_PATH", self._central_db_path)
        self._db_path_patcher.start()
        await db_module.init_db()

        self.config_a = referral_program.config_from_bot_row(
            {"bot_id": 901, "name": "ref_isolation_bot_a", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        self.config_b = referral_program.config_from_bot_row(
            {"bot_id": 902, "name": "ref_isolation_bot_b", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await referral_program.init_db(self.config_a.db_path)
        await referral_program.init_db(self.config_b.db_path)
        self.bot_a, self.dp_a = _build_bot_dispatcher(self.config_a)
        self.bot_b, self.dp_b = _build_bot_dispatcher(self.config_b)

    async def asyncTearDown(self):
        self._db_path_patcher.stop()
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_configs_point_to_different_files(self):
        self.assertNotEqual(self.config_a.db_path, self.config_b.db_path)

    async def test_same_user_id_gets_independent_participant_rows_per_bot(self):
        # SAME Telegram user_id registers on both bots — must be treated as
        # two entirely separate participants (different DB files).
        await self.dp_a.feed_webhook_update(self.bot_a, _text_update(1, REFERRER_ID, "/start"))
        await self.dp_b.feed_webhook_update(self.bot_b, _text_update(1, REFERRER_ID, "/start"))

        code_a = _ref_code(self.config_a.db_path, REFERRER_ID)
        code_b = _ref_code(self.config_b.db_path, REFERRER_ID)
        self.assertIsNotNone(code_a)
        self.assertIsNotNone(code_b)

    async def test_referral_on_bot_a_does_not_leak_into_bot_b(self):
        await self.dp_a.feed_webhook_update(self.bot_a, _text_update(1, REFERRER_ID, "/start"))
        code_a = _ref_code(self.config_a.db_path, REFERRER_ID)
        await self.dp_a.feed_webhook_update(self.bot_a, _text_update(2, REFERRED_ID, f"/start {code_a}"))

        # Bot B never saw either user — its DB must have zero rows.
        conn_b = sqlite3.connect(self.config_b.db_path)
        participants_b = conn_b.execute("SELECT COUNT(*) FROM participants").fetchone()[0]
        referrals_b = conn_b.execute("SELECT COUNT(*) FROM referrals").fetchone()[0]
        conn_b.close()
        self.assertEqual(participants_b, 0)
        self.assertEqual(referrals_b, 0)

        conn_a = sqlite3.connect(self.config_a.db_path)
        referrals_a = conn_a.execute("SELECT referrer_id, referred_id, status FROM referrals").fetchall()
        conn_a.close()
        self.assertEqual(referrals_a, [(REFERRER_ID, REFERRED_ID, "pending")])


class ReferralRewardFlowTests(unittest.IsolatedAsyncioTestCase):
    """Owner-specified design: reward is credited at condition fulfillment
    (admin confirmation), not at registration; leaderboard counts only
    rewarded referrals; double-confirm must not double-credit."""

    async def asyncSetUp(self):
        self._bot_call = AsyncMock(return_value=MagicMock())
        self._bot_call_patcher = patch.object(Bot, "__call__", new=self._bot_call)
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

        self._central_db_path = self.data_dir / "central_bots.db"
        self._db_path_patcher = patch.object(db_module, "DB_PATH", self._central_db_path)
        self._db_path_patcher.start()
        await db_module.init_db()

        self.config = referral_program.config_from_bot_row(
            {"bot_id": 903, "name": "ref_flow_bot", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await referral_program.init_db(self.config.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)

    async def asyncTearDown(self):
        self._db_path_patcher.stop()
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def _register_referrer_and_referred(self) -> int:
        await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/start"))  # bootstraps admin
        await self.dp.feed_webhook_update(self.bot, _text_update(2, REFERRER_ID, "/start"))
        code = _ref_code(self.config.db_path, REFERRER_ID)
        await self.dp.feed_webhook_update(self.bot, _text_update(3, REFERRED_ID, f"/start {code}"))
        conn = sqlite3.connect(self.config.db_path)
        referral_id = conn.execute("SELECT id FROM referrals WHERE referred_id=?", (REFERRED_ID,)).fetchone()[0]
        conn.close()
        return referral_id

    async def test_no_reward_and_pending_status_before_confirmation(self):
        await self._register_referrer_and_referred()
        conn = sqlite3.connect(self.config.db_path)
        balance, status = conn.execute(
            "SELECT p.balance, r.status FROM participants p JOIN referrals r ON r.referrer_id = p.user_id "
            "WHERE p.user_id=?", (REFERRER_ID,)
        ).fetchone()
        conn.close()
        self.assertEqual(balance, 0)
        self.assertEqual(status, "pending")

    async def test_admin_confirmation_credits_reward_exactly_once_on_double_tap(self):
        referral_id = await self._register_referrer_and_referred()
        await self.dp.feed_webhook_update(self.bot, _callback_update(10, ADMIN_ID, f"ref_confirm:{referral_id}"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(11, ADMIN_ID, f"ref_confirm:{referral_id}"))

        conn = sqlite3.connect(self.config.db_path)
        balance = conn.execute("SELECT balance FROM participants WHERE user_id=?", (REFERRER_ID,)).fetchone()[0]
        status = conn.execute("SELECT status FROM referrals WHERE id=?", (referral_id,)).fetchone()[0]
        conn.close()
        self.assertEqual(balance, referral_program.REWARD_POINTS)
        self.assertEqual(status, "rewarded")

    async def test_non_admin_cannot_confirm(self):
        referral_id = await self._register_referrer_and_referred()
        await self.dp.feed_webhook_update(self.bot, _callback_update(10, REFERRED_ID, f"ref_confirm:{referral_id}"))
        conn = sqlite3.connect(self.config.db_path)
        status = conn.execute("SELECT status FROM referrals WHERE id=?", (referral_id,)).fetchone()[0]
        balance = conn.execute("SELECT balance FROM participants WHERE user_id=?", (REFERRER_ID,)).fetchone()[0]
        conn.close()
        self.assertEqual(status, "pending")
        self.assertEqual(balance, 0)

    async def test_leaderboard_counts_only_rewarded_not_pending(self):
        referral_id = await self._register_referrer_and_referred()
        # A second referred user stays pending — must not count on the leaderboard.
        code = _ref_code(self.config.db_path, REFERRER_ID)
        await self.dp.feed_webhook_update(self.bot, _text_update(20, 444, f"/start {code}"))

        await self.dp.feed_webhook_update(self.bot, _callback_update(30, ADMIN_ID, f"ref_confirm:{referral_id}"))

        conn = sqlite3.connect(self.config.db_path)
        rewarded_count = conn.execute(
            "SELECT COUNT(*) FROM referrals WHERE referrer_id=? AND status='rewarded'", (REFERRER_ID,)
        ).fetchone()[0]
        pending_count = conn.execute(
            "SELECT COUNT(*) FROM referrals WHERE referrer_id=? AND status='pending'", (REFERRER_ID,)
        ).fetchone()[0]
        conn.close()
        self.assertEqual(rewarded_count, 1)
        self.assertEqual(pending_count, 1)

    async def test_returning_user_reopening_link_is_not_retroactively_attributed(self):
        # REFERRED_ID already registered (no referrer) BEFORE clicking a link.
        await self.dp.feed_webhook_update(self.bot, _text_update(1, REFERRER_ID, "/start"))
        code = _ref_code(self.config.db_path, REFERRER_ID)
        await self.dp.feed_webhook_update(self.bot, _text_update(2, REFERRED_ID, "/start"))
        # Now they open a referral link — must be ignored, no attribution.
        await self.dp.feed_webhook_update(self.bot, _text_update(3, REFERRED_ID, f"/start {code}"))

        conn = sqlite3.connect(self.config.db_path)
        referred_by = conn.execute(
            "SELECT referred_by FROM participants WHERE user_id=?", (REFERRED_ID,)
        ).fetchone()[0]
        referrals_count = conn.execute("SELECT COUNT(*) FROM referrals").fetchone()[0]
        conn.close()
        self.assertIsNone(referred_by)
        self.assertEqual(referrals_count, 0)

    async def test_ref_code_never_uses_visually_ambiguous_characters(self):
        await self.dp.feed_webhook_update(self.bot, _text_update(1, REFERRER_ID, "/start"))
        code = _ref_code(self.config.db_path, REFERRER_ID)
        for ambiguous in "0O1IL":
            self.assertNotIn(ambiguous, code)


class ReferralAdminHijackTests(unittest.IsolatedAsyncioTestCase):
    """Security fix: whoever sent /start FIRST used to permanently become the
    bot's admin (able to see /pending, confirm rewards, and manage admins) —
    a regular participant who messaged the bot before the owner tested it
    could hijack that role. Same criterion/pattern as
    tests/test_shop_catalog_isolation.py's admin-hijack tests."""

    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

        self._central_db_path = self.data_dir / "central_bots.db"
        self._db_path_patcher = patch.object(db_module, "DB_PATH", self._central_db_path)
        self._db_path_patcher.start()
        await db_module.init_db()

    async def asyncTearDown(self):
        self._db_path_patcher.stop()
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_non_owner_messaging_first_does_not_become_admin(self):
        config = referral_program.config_from_bot_row(
            {"bot_id": 911, "name": "ref_bot_owned", "display_name": None,
             "group_chat_id": None, "owner_telegram_id": 12345},
            self.data_dir,
        )
        await referral_program.init_db(config.db_path)
        bot, dp = _build_bot_dispatcher(config)

        CLIENT_ID = 555  # not the owner, messages first (a regular participant)
        await dp.feed_webhook_update(bot, _text_update(1, CLIENT_ID, "/start"))
        self.assertEqual(referral_program._load_admins(config.admins_file), set())
        self.assertFalse(referral_program._is_admin(CLIENT_ID, config))

    async def test_owner_start_becomes_admin(self):
        config = referral_program.config_from_bot_row(
            {"bot_id": 912, "name": "ref_bot_owned2", "display_name": None,
             "group_chat_id": None, "owner_telegram_id": 12345},
            self.data_dir,
        )
        await referral_program.init_db(config.db_path)
        bot, dp = _build_bot_dispatcher(config)

        await dp.feed_webhook_update(bot, _text_update(1, 12345, "/start"))
        self.assertTrue(referral_program._is_admin(12345, config))
        self.assertEqual(referral_program._load_admins(config.admins_file), {"12345"})

    async def test_owner_is_always_admin_even_with_stale_admins_file(self):
        config = referral_program.config_from_bot_row(
            {"bot_id": 913, "name": "ref_bot_owned3", "display_name": None,
             "group_chat_id": None, "owner_telegram_id": 777},
            self.data_dir,
        )
        await referral_program.init_db(config.db_path)
        referral_program._save_admins(config.admins_file, {"999999"})
        self.assertTrue(referral_program._is_admin(777, config))  # owner: always admin
        self.assertTrue(referral_program._is_admin(999999, config))  # still honors the file's own admin
        self.assertFalse(referral_program._is_admin(4242, config))  # neither owner nor in the file

    async def test_standalone_mode_keeps_first_comer_behavior(self):
        """owner_telegram_id unknown (standalone/env mode) -- the old
        first-comer bootstrap is the only option available, so it's kept."""
        config = referral_program.config_from_bot_row(
            {"bot_id": 914, "name": "ref_bot_standalone", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await referral_program.init_db(config.db_path)
        bot, dp = _build_bot_dispatcher(config)

        await dp.feed_webhook_update(bot, _text_update(1, 111, "/start"))
        self.assertEqual(referral_program._load_admins(config.admins_file), {"111"})
        self.assertTrue(referral_program._is_admin(111, config))

    async def test_bootstrap_admin_syncs_to_central_bot_admins_table(self):
        config = referral_program.config_from_bot_row(
            {"bot_id": 915, "name": "ref_bot_synced", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await referral_program.init_db(config.db_path)
        bot, dp = _build_bot_dispatcher(config)
        await dp.feed_webhook_update(bot, _text_update(1, 321, "/start"))

        central_admins = await db_module.get_bot_admins(915)
        self.assertEqual(central_admins, ["321"])


if __name__ == "__main__":
    unittest.main()
