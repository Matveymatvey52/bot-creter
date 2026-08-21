"""Data isolation, balance-correctness, and rate-isolation tests for the
loyalty_program template.

Standard criterion (same as test_debtors_isolation.py): two bots on the SAME
template, different config, must never mix data — even driven by the SAME
Telegram user_id. Everything lives in a tempfile.TemporaryDirectory, never
data/bots.db.

Balance-correctness criterion (this template's own headline claim): the
signed-ledger design means "баланс = SUM(points)" over a client's entries,
where a redemption is a NEW row with the opposite sign, never an edit of an
existing accrual row. These tests exercise mixed accrual+redemption
sequences (including fractional points from proportional accrual) and
assert the running balance at each step.

Rate-isolation criterion (this template's owner-mandated configurable
setting): two bots must be able to run different accrual rates
independently, and changing one bot's rate must never affect the other's.

No real Telegram network calls, no real tokens.

Run with: python -m unittest tests.test_loyalty_program_isolation
"""

from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

import db.database as db_module
from runtime.registry import get_template_router
from templates import loyalty_program as loy

FAKE_TOKEN = "123456:test-token-not-real"


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


def _group_text_update(update_id: int, user_id: int, chat_id: int, text: str) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id, "date": 1700000000,
            "chat": {"id": chat_id, "type": "group", "title": "Test Group"},
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


def _build_bot_dispatcher(config: loy.LoyaltyConfig) -> tuple[Bot, Dispatcher]:
    bot = Bot(token=FAKE_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(loy.ConfigMiddleware(config))
    dp.include_router(get_template_router("loyalty_program"))
    return bot, dp


class LoyaltyIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

        self.config_a = loy.config_from_bot_row(
            {"bot_id": 621, "name": "loyalty_bot_a", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        self.config_b = loy.config_from_bot_row(
            {"bot_id": 622, "name": "loyalty_bot_b", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await loy.init_db(self.config_a.db_path)
        await loy.init_db(self.config_b.db_path)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_configs_point_to_different_files(self):
        self.assertNotEqual(self.config_a.db_path, self.config_b.db_path)
        self.assertNotEqual(self.config_a.admins_file, self.config_b.admins_file)

    async def test_clients_isolated_across_bots(self):
        """A client added on bot A must not appear on bot B, even though both
        DBs share the same schema and could theoretically collide on
        AUTOINCREMENT id if pointed at the same file."""
        id_a = await loy._insert_client(self.config_a.db_path, "Alice", None)
        id_b = await loy._insert_client(self.config_b.db_path, "Bob", None)
        await loy._insert_entry(self.config_a.db_path, id_a, 10, "accrual", 1000, None)
        await loy._insert_entry(self.config_b.db_path, id_b, 20, "accrual", 2000, None)

        clients_a = await loy._clients_with_balance(self.config_a.db_path)
        clients_b = await loy._clients_with_balance(self.config_b.db_path)

        self.assertEqual(len(clients_a), 1)
        self.assertEqual(clients_a[0]["name"], "Alice")
        self.assertEqual(clients_a[0]["balance"], 10)

        self.assertEqual(len(clients_b), 1)
        self.assertEqual(clients_b[0]["name"], "Bob")
        self.assertEqual(clients_b[0]["balance"], 20)

    async def test_admin_bootstrap_isolated_per_bot(self):
        bot_a, dp_a = _build_bot_dispatcher(self.config_a)
        bot_b, dp_b = _build_bot_dispatcher(self.config_b)
        await dp_a.feed_webhook_update(bot_a, _text_update(1, 111, "/start"))
        await dp_b.feed_webhook_update(bot_b, _text_update(1, 999, "/start"))

        self.assertEqual(loy._load_admins(self.config_a.admins_file), {"111"})
        self.assertEqual(loy._load_admins(self.config_b.admins_file), {"999"})

    async def test_same_user_id_sees_only_own_bots_clients(self):
        """Cross-bot leak check with the SAME Telegram user_id acting as admin
        on both bots — this is the exact bug class every isolation test in
        this codebase exists to catch."""
        SAME_USER = 555
        id_a = await loy._insert_client(self.config_a.db_path, "Only on A", None)
        await loy._insert_entry(self.config_a.db_path, id_a, 5, "accrual", 500, None)

        bot_a, dp_a = _build_bot_dispatcher(self.config_a)
        bot_b, dp_b = _build_bot_dispatcher(self.config_b)
        await dp_a.feed_webhook_update(bot_a, _text_update(1, SAME_USER, "/start"))
        await dp_b.feed_webhook_update(bot_b, _text_update(1, SAME_USER, "/start"))

        clients_a = await loy._clients_with_balance(self.config_a.db_path)
        clients_b = await loy._clients_with_balance(self.config_b.db_path)
        self.assertEqual(len(clients_a), 1)
        self.assertEqual(len(clients_b), 0)


class RateIsolationTests(unittest.IsolatedAsyncioTestCase):
    """Owner-mandated configurable-per-bot business rule: accrual rate must
    never leak or default identically across bots that set it differently."""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config_a = loy.config_from_bot_row(
            {"bot_id": 631, "name": "loyalty_rate_bot_a", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        self.config_b = loy.config_from_bot_row(
            {"bot_id": 632, "name": "loyalty_rate_bot_b", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await loy.init_db(self.config_a.db_path)
        await loy.init_db(self.config_b.db_path)

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def test_both_bots_start_at_the_same_default_rate(self):
        self.assertEqual(await loy._get_rate(self.config_a.db_path), loy.DEFAULT_POINTS_RATE)
        self.assertEqual(await loy._get_rate(self.config_b.db_path), loy.DEFAULT_POINTS_RATE)

    async def test_changing_rate_on_bot_a_does_not_affect_bot_b(self):
        await loy._set_rate(self.config_a.db_path, 50.0)
        self.assertEqual(await loy._get_rate(self.config_a.db_path), 50.0)
        self.assertEqual(await loy._get_rate(self.config_b.db_path), loy.DEFAULT_POINTS_RATE)

    async def test_independent_rates_produce_independent_accrual_amounts(self):
        await loy._set_rate(self.config_a.db_path, 50.0)   # 1 балл = 50₽
        await loy._set_rate(self.config_b.db_path, 200.0)  # 1 балл = 200₽

        id_a = await loy._insert_client(self.config_a.db_path, "Client A", None)
        id_b = await loy._insert_client(self.config_b.db_path, "Client B", None)

        rate_a = await loy._get_rate(self.config_a.db_path)
        rate_b = await loy._get_rate(self.config_b.db_path)
        points_a = loy._compute_points(1000, rate_a)
        points_b = loy._compute_points(1000, rate_b)
        await loy._insert_entry(self.config_a.db_path, id_a, points_a, "accrual", 1000, None)
        await loy._insert_entry(self.config_b.db_path, id_b, points_b, "accrual", 1000, None)

        stats_a = await loy._client_stats(self.config_a.db_path, id_a)
        stats_b = await loy._client_stats(self.config_b.db_path, id_b)
        self.assertEqual(stats_a["balance"], 20)  # 1000 / 50
        self.assertEqual(stats_b["balance"], 5)   # 1000 / 200

    async def test_rate_edit_flow_updates_only_the_target_bot(self):
        ADMIN_ID = 42
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        try:
            bot_a, dp_a = _build_bot_dispatcher(self.config_a)
            await dp_a.feed_webhook_update(bot_a, _text_update(1, ADMIN_ID, "/start"))
            await dp_a.feed_webhook_update(bot_a, _callback_update(2, ADMIN_ID, "loy_rate_edit"))
            await dp_a.feed_webhook_update(bot_a, _text_update(3, ADMIN_ID, "77"))
        finally:
            self._bot_call_patcher.stop()

        self.assertEqual(await loy._get_rate(self.config_a.db_path), 77.0)
        self.assertEqual(await loy._get_rate(self.config_b.db_path), loy.DEFAULT_POINTS_RATE)


class BalanceCorrectnessTests(unittest.IsolatedAsyncioTestCase):
    """Headline claim of the signed-ledger design: баланс = SUM(points), with
    a redemption as a new row of opposite sign rather than an edit."""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = loy.config_from_bot_row(
            {"bot_id": 701, "name": "loyalty_balance_bot", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await loy.init_db(self.config.db_path)

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def test_single_accrual_gives_positive_balance(self):
        client_id = await loy._insert_client(self.config.db_path, "Ivan", None)
        points = loy._compute_points(1000, loy.DEFAULT_POINTS_RATE)
        await loy._insert_entry(self.config.db_path, client_id, points, "accrual", 1000, None)
        stats = await loy._client_stats(self.config.db_path, client_id)
        self.assertEqual(stats["balance"], 10)  # 1000 / 100

    async def test_fractional_points_from_proportional_accrual(self):
        """150₽ at the default 100₽/балл rate earns 1.5 балла, not floored
        to 1 — this is the owner-confirmed design choice."""
        client_id = await loy._insert_client(self.config.db_path, "Ivan", None)
        points = loy._compute_points(150, loy.DEFAULT_POINTS_RATE)
        self.assertEqual(points, 1.5)
        await loy._insert_entry(self.config.db_path, client_id, points, "accrual", 150, None)
        stats = await loy._client_stats(self.config.db_path, client_id)
        self.assertEqual(stats["balance"], 1.5)

    async def test_redemption_is_a_new_row_not_an_edit(self):
        client_id = await loy._insert_client(self.config.db_path, "Ivan", None)
        entry_id = await loy._insert_entry(self.config.db_path, client_id, 10, "accrual", 1000, None)
        result = await loy._redeem_points(self.config.db_path, client_id, 3, "скидка 3 балла")
        self.assertTrue(result["ok"])

        original = await loy._entry_row(self.config.db_path, entry_id)
        self.assertEqual(original["points"], 10)  # untouched

        entries = await loy._client_entries(self.config.db_path, client_id)
        self.assertEqual(len(entries), 2)

        stats = await loy._client_stats(self.config.db_path, client_id)
        self.assertEqual(stats["balance"], 7)

    async def test_mixed_accrual_and_redemption_sequence_running_balance(self):
        """Simulates a realistic mixed history: accrue, redeem part, accrue
        more, redeem fully — balance must be correct after EVERY step, not
        just at the end."""
        client_id = await loy._insert_client(self.config.db_path, "Ivan", None)
        expected = 0

        for op, note in [
            ("accrue:5", None),      # +5  -> 5
            ("redeem:2", "скидка"),   # -2  -> 3
            ("accrue:4", None),      # +4  -> 7
            ("redeem:7", "подарок"),  # -7  -> 0
        ]:
            kind, amount_s = op.split(":")
            amount = float(amount_s)
            if kind == "accrue":
                await loy._insert_entry(self.config.db_path, client_id, amount, "accrual", amount * loy.DEFAULT_POINTS_RATE, None)
                expected += amount
            else:
                result = await loy._redeem_points(self.config.db_path, client_id, amount, note)
                self.assertTrue(result["ok"], f"redemption of {amount} should succeed at balance {expected}")
                expected -= amount
            stats = await loy._client_stats(self.config.db_path, client_id)
            self.assertEqual(stats["balance"], expected, f"after {op!r}: expected {expected}")

    async def test_redemption_exceeding_balance_is_rejected_and_does_not_write(self):
        client_id = await loy._insert_client(self.config.db_path, "Ivan", None)
        await loy._insert_entry(self.config.db_path, client_id, 5, "accrual", 500, None)

        result = await loy._redeem_points(self.config.db_path, client_id, 10, "слишком много")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "insufficient_balance")
        self.assertEqual(result["balance"], 5)

        entries = await loy._client_entries(self.config.db_path, client_id)
        self.assertEqual(len(entries), 1)  # no redemption row was written
        stats = await loy._client_stats(self.config.db_path, client_id)
        self.assertEqual(stats["balance"], 5)  # unchanged

    async def test_redemption_of_exact_balance_succeeds(self):
        client_id = await loy._insert_client(self.config.db_path, "Ivan", None)
        await loy._insert_entry(self.config.db_path, client_id, 10, "accrual", 1000, None)
        result = await loy._redeem_points(self.config.db_path, client_id, 10, None)
        self.assertTrue(result["ok"])
        stats = await loy._client_stats(self.config.db_path, client_id)
        self.assertEqual(stats["balance"], 0)

    async def test_deleting_an_entry_recalculates_balance(self):
        client_id = await loy._insert_client(self.config.db_path, "Ivan", None)
        e1 = await loy._insert_entry(self.config.db_path, client_id, 10, "accrual", 1000, None)
        e2 = await loy._insert_entry(self.config.db_path, client_id, -4, "redemption", None, "скидка")
        stats = await loy._client_stats(self.config.db_path, client_id)
        self.assertEqual(stats["balance"], 6)

        await loy._delete_entry(self.config.db_path, e2)
        stats = await loy._client_stats(self.config.db_path, client_id)
        self.assertEqual(stats["balance"], 10)
        self.assertIsNotNone(e1)

    async def test_deleting_client_cascades_entries(self):
        client_id = await loy._insert_client(self.config.db_path, "Ivan", None)
        await loy._insert_entry(self.config.db_path, client_id, 5, "accrual", 500, None)
        await loy._insert_entry(self.config.db_path, client_id, -1, "redemption", None, None)

        await loy._delete_client(self.config.db_path, client_id)

        self.assertIsNone(await loy._client_row(self.config.db_path, client_id))
        conn = sqlite3.connect(self.config.db_path)
        remaining = conn.execute("SELECT COUNT(*) FROM point_entries WHERE client_id=?", (client_id,)).fetchone()[0]
        conn.close()
        self.assertEqual(remaining, 0)

    async def test_client_with_no_entries_has_zero_balance(self):
        client_id = await loy._insert_client(self.config.db_path, "Empty", None)
        stats = await loy._client_stats(self.config.db_path, client_id)
        self.assertEqual(stats["balance"], 0)
        self.assertIsNone(stats["last_entry_date"])

    async def test_sort_by_balance_descending(self):
        c1 = await loy._insert_client(self.config.db_path, "Small", None)
        c2 = await loy._insert_client(self.config.db_path, "Big", None)
        await loy._insert_entry(self.config.db_path, c1, 1, "accrual", 100, None)
        await loy._insert_entry(self.config.db_path, c2, 9, "accrual", 900, None)

        all_clients = await loy._clients_with_balance(self.config.db_path)
        sorted_clients = loy._sort_clients(all_clients, "balance")
        self.assertEqual([c["name"] for c in sorted_clients], ["Big", "Small"])

    async def test_sort_by_name_default(self):
        await loy._insert_client(self.config.db_path, "Zoe", None)
        await loy._insert_client(self.config.db_path, "Anna", None)
        all_clients = await loy._clients_with_balance(self.config.db_path)
        sorted_clients = loy._sort_clients(all_clients, "name")
        self.assertEqual([c["name"] for c in sorted_clients], ["Anna", "Zoe"])

    async def test_money_parsing_accepts_kopecks_rejects_non_positive(self):
        self.assertEqual(loy._parse_money("199.99"), 199.99)
        self.assertIsNone(loy._parse_money("0"))
        self.assertIsNone(loy._parse_money("abc"))
        self.assertIsNone(loy._parse_money(str(loy.AMOUNT_MAX + 1)))

    async def test_money_parsing_strips_a_typed_minus_same_as_debtors_convention(self):
        """Same convention as debtors.py's _parse_amount: a typed "-" is
        stripped like any other non-digit character rather than rejected —
        there's no direction ambiguity to guard here anyway, since accrual
        only ever adds points."""
        self.assertEqual(loy._parse_money("-50"), 50.0)

    async def test_points_computation_rounds_to_two_decimals(self):
        self.assertEqual(loy._compute_points(100, 3), 33.33)

    async def test_fmt_num_trims_trailing_zeros(self):
        self.assertEqual(loy._fmt_num(2.0), "2")
        self.assertEqual(loy._fmt_num(1.5), "1.5")
        self.assertEqual(loy._fmt_num(0.0), "0")
        self.assertEqual(loy._fmt_num(33.33), "33.33")


class ButtonOnlyUIAndAccessTests(unittest.IsolatedAsyncioTestCase):
    """Owner-mandated permanent design rule: no raw text command lists —
    every action is a clickable button. Also confirms the whole bot is
    admin-gated (internal business tool, no separate client role)."""

    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._mock_call = self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = loy.config_from_bot_row(
            {"bot_id": 801, "name": "loyalty_ui_bot", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await loy.init_db(self.config.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_start_menu_is_buttons_not_text_commands(self):
        await self.dp.feed_webhook_update(self.bot, _text_update(1, 42, "/start"))  # 42 becomes admin
        sent = [
            call.args[0] for call in self._mock_call.call_args_list
            if call.args and hasattr(call.args[0], "reply_markup") and call.args[0].reply_markup
        ]
        found_clients_btn = any(
            btn.text == "👥 Клиенты"
            for method in sent for row in method.reply_markup.keyboard for btn in row
        )
        self.assertTrue(found_clients_btn)

    async def test_non_admin_gets_no_access_message_not_the_menu(self):
        await self.dp.feed_webhook_update(self.bot, _text_update(1, 42, "/start"))  # 42 becomes first admin
        self._mock_call.reset_mock()
        await self.dp.feed_webhook_update(self.bot, _text_update(2, 99, "/start"))  # 99 is not an admin

        texts = [
            call.args[0].text for call in self._mock_call.call_args_list
            if call.args and hasattr(call.args[0], "text")
        ]
        self.assertTrue(any(loy.NO_ACCESS_TEXT in t for t in texts if t))
        no_keyboards = [
            call.args[0] for call in self._mock_call.call_args_list
            if call.args and hasattr(call.args[0], "reply_markup") and call.args[0].reply_markup
        ]
        self.assertEqual(no_keyboards, [])

    async def test_non_admin_cannot_list_clients(self):
        await self.dp.feed_webhook_update(self.bot, _text_update(1, 42, "/start"))  # 42 becomes admin
        self._mock_call.reset_mock()
        await self.dp.feed_webhook_update(self.bot, _text_update(2, 99, "👥 Клиенты"))
        texts = [
            call.args[0].text for call in self._mock_call.call_args_list
            if call.args and hasattr(call.args[0], "text")
        ]
        self.assertTrue(any(loy.NO_ACCESS_TEXT in t for t in texts if t))

    async def test_redeem_button_hidden_for_zero_balance(self):
        client_id = await loy._insert_client(self.config.db_path, "Ivan", None)
        kb = loy.kb_client_card(client_id, 0)
        all_buttons = [b.text for row in kb.inline_keyboard for b in row]
        self.assertFalse(any("Списать" in t for t in all_buttons))

    async def test_redeem_button_shown_for_positive_balance(self):
        kb = loy.kb_client_card(1, 5)
        all_buttons = [b.text for row in kb.inline_keyboard for b in row]
        self.assertTrue(any("Списать" in t for t in all_buttons))

    async def test_redeem_start_blocked_at_zero_balance_via_alert_not_flow(self):
        """Even if a stale button somehow reaches the handler (e.g. balance
        dropped to 0 between renders), cb_redeem_start must not enter the
        RedeemFlow — it should short-circuit with an alert."""
        ADMIN_ID = 42
        await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/start"))
        client_id = await loy._insert_client(self.config.db_path, "Ivan", None)

        await self.dp.feed_webhook_update(self.bot, _callback_update(2, ADMIN_ID, f"loy_redeem:{client_id}"))
        # No clean public API to read FSM state generically across storages in
        # this codebase's other tests, so assert via behavior instead: sending
        # a plain-text message afterward must NOT be captured as a redemption
        # amount (i.e. no new point_entries row appears).
        await self.dp.feed_webhook_update(self.bot, _text_update(3, ADMIN_ID, "5"))
        entries = await loy._client_entries(self.config.db_path, client_id)
        self.assertEqual(len(entries), 0)


class GroupChatLeakTests(unittest.IsolatedAsyncioTestCase):
    """Review-found in debtors.py: top-level menu handlers matching a
    persistent-keyboard button text must be private-chat-only — otherwise a
    group member typing the exact same text leaks client/admin data into the
    group. Locking this in from the start rather than waiting for a review
    to find it."""

    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._mock_call = self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = loy.config_from_bot_row(
            {"bot_id": 901, "name": "loyalty_group_bot", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await loy.init_db(self.config.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_clients_list_text_in_a_group_chat_does_not_leak_the_list(self):
        ADMIN_ID = 42
        GROUP_CHAT_ID = -100123456
        await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/start"))
        client_id = await loy._insert_client(self.config.db_path, "Ivan", None)
        await loy._insert_entry(self.config.db_path, client_id, 5, "accrual", 500, None)

        self._mock_call.reset_mock()
        update = _group_text_update(2, ADMIN_ID, GROUP_CHAT_ID, "👥 Клиенты")
        await self.dp.feed_webhook_update(self.bot, update)

        sent_texts = [
            call.args[0].text for call in self._mock_call.call_args_list
            if call.args and hasattr(call.args[0], "text") and call.args[0].text
        ]
        self.assertFalse(any("Ivan" in t or "Клиенты" in t for t in sent_texts))

    async def test_admins_list_text_in_a_group_chat_does_not_leak_admin_ids(self):
        ADMIN_ID = 42
        GROUP_CHAT_ID = -100123456
        await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/start"))

        self._mock_call.reset_mock()
        update = _group_text_update(2, ADMIN_ID, GROUP_CHAT_ID, "⚙️ Админы бота")
        await self.dp.feed_webhook_update(self.bot, update)

        sent_texts = [
            call.args[0].text for call in self._mock_call.call_args_list
            if call.args and hasattr(call.args[0], "text") and call.args[0].text
        ]
        self.assertFalse(any(str(ADMIN_ID) in t for t in sent_texts))

    async def test_settings_text_in_a_group_chat_does_not_leak_rate(self):
        ADMIN_ID = 42
        GROUP_CHAT_ID = -100123456
        await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/start"))

        self._mock_call.reset_mock()
        update = _group_text_update(2, ADMIN_ID, GROUP_CHAT_ID, "💱 Настройки")
        await self.dp.feed_webhook_update(self.bot, update)

        sent_texts = [
            call.args[0].text for call in self._mock_call.call_args_list
            if call.args and hasattr(call.args[0], "text") and call.args[0].text
        ]
        self.assertFalse(any("Настройки начисления" in t for t in sent_texts))


class ConcurrencyAndForgedInputTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._mock_call = self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = loy.config_from_bot_row(
            {"bot_id": 902, "name": "loyalty_concurrency_bot", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await loy.init_db(self.config.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_concurrent_double_submit_redemption_does_not_double_deduct(self):
        """A fast double-tap on "Пропустить" (or a duplicate note submit)
        racing itself must not deduct the same redemption twice — the
        _busy_entry_saves guard must make the second concurrent call a
        no-op, same pattern as debtors.py's equivalent test."""
        client_id = await loy._insert_client(self.config.db_path, "Ivan", None)
        await loy._insert_entry(self.config.db_path, client_id, 10, "accrual", 1000, None)
        state_data = {"started_at": 0, "client_id": client_id, "points": 4}

        class _FakeState:
            def __init__(self, data):
                self._data = dict(data)
            async def get_data(self):
                return dict(self._data)
            async def clear(self):
                self._data = {}

        fake_msg = MagicMock()
        fake_msg.edit_text = AsyncMock()
        fake_msg.answer = AsyncMock()

        state_a = _FakeState(state_data)
        state_b = _FakeState(state_data)
        import asyncio as _asyncio
        await _asyncio.gather(
            loy._save_redemption_and_reply(fake_msg, state_a, self.config, "first", edit=True),
            loy._save_redemption_and_reply(fake_msg, state_b, self.config, "second", edit=True),
        )

        entries = await loy._client_entries(self.config.db_path, client_id)
        redemptions = [e for e in entries if e["kind"] == "redemption"]
        self.assertEqual(len(redemptions), 1)
        stats = await loy._client_stats(self.config.db_path, client_id)
        self.assertEqual(stats["balance"], 6)

    async def test_forged_non_numeric_callback_data_does_not_crash(self):
        """Review-found pattern (debtors.py): int(cb.data.split(...)) without
        a guard would raise on a forged/malformed callback_data (Telegram's
        API does not restrict callback_query.data to values the bot actually
        sent). _parse_id must make every such handler degrade gracefully."""
        ADMIN_ID = 42
        await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/start"))

        for data in ("loy_view:not_a_number", "loy_del_confirm:xyz", "loy_entry_del:abc:def", "loy_accrue:nope", "loy_redeem:nope"):
            with self.subTest(data=data):
                await self.dp.feed_webhook_update(self.bot, _callback_update(99, ADMIN_ID, data))


class ReviewFixRegressionTests(unittest.IsolatedAsyncioTestCase):
    """Locks in fixes made during review-orchestrator's pre-commit review
    (matches the convention established by debtors.py's own
    ReviewFixRegressionTests)."""

    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._mock_call = self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = loy.config_from_bot_row(
            {"bot_id": 903, "name": "loyalty_regression_bot", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await loy.init_db(self.config.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_concurrent_double_submit_new_client_does_not_create_duplicates(self):
        """Review-found: unlike the redemption-save path, _finish_new_client
        had no double-submit guard — a race between the text-contact message
        and the "Без контакта" callback (or a fast double-tap on either)
        could insert the same new client twice. _busy_client_creates must
        make the second concurrent call a no-op."""
        state_data = {"started_at": 0, "name": "Ivan"}

        class _FakeState:
            def __init__(self, data):
                self._data = dict(data)
            async def get_data(self):
                return dict(self._data)
            async def clear(self):
                self._data = {}
            async def set_state(self, *_a, **_kw):
                pass

        fake_msg = MagicMock()
        fake_msg.edit_text = AsyncMock()
        fake_msg.answer = AsyncMock()

        state_a = _FakeState(state_data)
        state_b = _FakeState(state_data)
        await asyncio.gather(
            loy._finish_new_client(fake_msg, state_a, self.config, "Ivan", None, edit=True),
            loy._finish_new_client(fake_msg, state_b, self.config, "Ivan", None, edit=True),
        )

        clients = await loy._clients_with_balance(self.config.db_path)
        self.assertEqual(len(clients), 1)

    async def test_busy_entry_saves_does_not_collide_across_bots_sharing_client_id(self):
        """Review-found: _busy_entry_saves used to be keyed by client_id
        alone, but this module is a SINGLE shared instance across every bot
        running this template in the webhook multi-tenant runner (see
        runtime/registry.py's per-process module caching) — two different
        bots' client #1 would collide in that one set. Keying by
        (db_path, client_id) must let concurrent redemptions on DIFFERENT
        bots for the "same" client_id proceed independently."""
        other_config = loy.config_from_bot_row(
            {"bot_id": 904, "name": "loyalty_regression_bot_2", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await loy.init_db(other_config.db_path)

        client_id = await loy._insert_client(self.config.db_path, "Ivan", None)
        other_client_id = await loy._insert_client(other_config.db_path, "Ivan", None)
        self.assertEqual(client_id, other_client_id)  # both AUTOINCREMENT from 1 -> same numeric id
        await loy._insert_entry(self.config.db_path, client_id, 10, "accrual", 1000, None)
        await loy._insert_entry(other_config.db_path, other_client_id, 10, "accrual", 1000, None)

        class _FakeState:
            def __init__(self, data):
                self._data = dict(data)
            async def get_data(self):
                return dict(self._data)
            async def clear(self):
                self._data = {}

        fake_msg = MagicMock()
        fake_msg.edit_text = AsyncMock()
        fake_msg.answer = AsyncMock()

        state_this = _FakeState({"started_at": 0, "client_id": client_id, "points": 3})
        state_other = _FakeState({"started_at": 0, "client_id": other_client_id, "points": 4})
        await asyncio.gather(
            loy._save_redemption_and_reply(fake_msg, state_this, self.config, "here", edit=True),
            loy._save_redemption_and_reply(fake_msg, state_other, other_config, "there", edit=True),
        )

        stats_this = await loy._client_stats(self.config.db_path, client_id)
        stats_other = await loy._client_stats(other_config.db_path, other_client_id)
        self.assertEqual(stats_this["balance"], 7)   # 10 - 3, not swallowed by the other bot's guard slot
        self.assertEqual(stats_other["balance"], 6)  # 10 - 4

    async def test_entry_del_with_too_few_callback_parts_does_not_crash(self):
        """Review-found: cb.data.split(":", 2) unpacked into 3 variables
        raises ValueError on a forged/truncated callback_data with fewer
        than 2 colons (e.g. "loy_entry_del:1") — must degrade gracefully."""
        ADMIN_ID = 42
        await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/start"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(2, ADMIN_ID, "loy_entry_del:1"))  # only 1 colon

    async def test_parse_id_rejects_values_outside_sqlite_int64_range(self):
        """Review-found: Python's int() parses an arbitrarily large digit
        string without raising, but binding that value into an aiosqlite
        query later raises an unhandled OverflowError. _parse_id must reject
        it up front instead."""
        self.assertIsNone(loy._parse_id(str(loy._SQLITE_INT_MAX + 1)))
        self.assertIsNone(loy._parse_id(str(loy._SQLITE_INT_MIN - 1)))
        self.assertEqual(loy._parse_id(str(loy._SQLITE_INT_MAX)), loy._SQLITE_INT_MAX)

    async def test_forged_huge_id_in_callback_does_not_crash(self):
        ADMIN_ID = 42
        await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/start"))
        huge = "9" * 40
        for data in (f"loy_view:{huge}", f"loy_entry_del:{huge}:{huge}"):
            with self.subTest(data=data):
                await self.dp.feed_webhook_update(self.bot, _callback_update(2, ADMIN_ID, data))


class LoyaltyAdminBootstrapSecurityTests(unittest.IsolatedAsyncioTestCase):
    """Security fix: previously, whoever sent /start FIRST permanently became
    the bot admin — a client testing the bot link before the owner did would
    silently seize the admin panel. See tests/test_shop_catalog_isolation.py
    for the original of this fix, applied identically here."""

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
        config = loy.config_from_bot_row(
            {"bot_id": 932, "name": "loyalty_bot_owned", "display_name": None,
             "group_chat_id": None, "owner_telegram_id": 12345},
            self.data_dir,
        )
        await loy.init_db(config.db_path)
        bot, dp = _build_bot_dispatcher(config)

        CLIENT_ID = 555  # not the owner, messages first
        await dp.feed_webhook_update(bot, _text_update(1, CLIENT_ID, "/start"))
        self.assertEqual(loy._load_admins(config.admins_file), set())
        self.assertFalse(loy._is_bot_admin(CLIENT_ID, config))

        await dp.feed_webhook_update(bot, _text_update(2, 12345, "/start"))
        self.assertTrue(loy._is_bot_admin(12345, config))
        self.assertEqual(loy._load_admins(config.admins_file), {"12345"})

    async def test_owner_is_always_admin_even_with_stale_admins_file(self):
        config = loy.config_from_bot_row(
            {"bot_id": 933, "name": "loyalty_bot_owned_2", "display_name": None,
             "group_chat_id": None, "owner_telegram_id": 777},
            self.data_dir,
        )
        await loy.init_db(config.db_path)
        loy._save_admins(config.admins_file, {"999999"})  # some other id, not the owner
        self.assertTrue(loy._is_bot_admin(777, config))  # owner: always admin
        self.assertTrue(loy._is_bot_admin(999999, config))  # still honors the file's own admin
        self.assertFalse(loy._is_bot_admin(4242, config))  # neither owner nor in the file

    async def test_bootstrap_admin_syncs_to_central_bot_admins_table(self):
        config = loy.config_from_bot_row(
            {"bot_id": 934, "name": "loyalty_bot_synced", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await loy.init_db(config.db_path)
        bot, dp = _build_bot_dispatcher(config)
        await dp.feed_webhook_update(bot, _text_update(1, 321, "/start"))

        central_admins = await db_module.get_bot_admins(934)
        self.assertEqual(central_admins, ["321"])


if __name__ == "__main__":
    unittest.main()
