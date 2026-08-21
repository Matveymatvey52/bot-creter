"""Data isolation and balance-correctness tests for the debtors template.

Standard criterion (same as test_shop_catalog_isolation.py): two bots on the
SAME template, different config, must never mix data — even driven by the
SAME Telegram user_id. Everything lives in a tempfile.TemporaryDirectory,
never data/bots.db.

Balance-correctness criterion (this template's own headline claim): the
signed-ledger design means "остаток = SUM(amount)" over a debtor's entries,
where a partial repayment is a NEW row with the opposite sign, never an edit
of the original debt row. These tests exercise mixed debt+repayment
sequences and assert the running balance at each step.

No real Telegram network calls, no real tokens.

Run with: python -m unittest tests.test_debtors_isolation
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
from templates import debtors as dbt

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


def _build_bot_dispatcher(config: dbt.DebtorsConfig) -> tuple[Bot, Dispatcher]:
    bot = Bot(token=FAKE_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(dbt.ConfigMiddleware(config))
    dp.include_router(get_template_router("debtors"))
    return bot, dp


class DebtorsIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

        self.config_a = dbt.config_from_bot_row(
            {"bot_id": 611, "name": "debtors_bot_a", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        self.config_b = dbt.config_from_bot_row(
            {"bot_id": 612, "name": "debtors_bot_b", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await dbt.init_db(self.config_a.db_path)
        await dbt.init_db(self.config_b.db_path)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_configs_point_to_different_files(self):
        self.assertNotEqual(self.config_a.db_path, self.config_b.db_path)
        self.assertNotEqual(self.config_a.admins_file, self.config_b.admins_file)

    async def test_debtors_isolated_across_bots(self):
        """A debtor added on bot A must not appear on bot B, even though both
        DBs share the same schema and could theoretically collide on
        AUTOINCREMENT id if pointed at the same file."""
        id_a = await dbt._insert_debtor(self.config_a.db_path, "Alice", None)
        id_b = await dbt._insert_debtor(self.config_b.db_path, "Bob", None)
        await dbt._insert_entry(self.config_a.db_path, id_a, 1000, "loan")
        await dbt._insert_entry(self.config_b.db_path, id_b, 2000, "loan")

        debtors_a = await dbt._debtors_with_balance(self.config_a.db_path)
        debtors_b = await dbt._debtors_with_balance(self.config_b.db_path)

        self.assertEqual(len(debtors_a), 1)
        self.assertEqual(debtors_a[0]["name"], "Alice")
        self.assertEqual(debtors_a[0]["balance"], 1000)

        self.assertEqual(len(debtors_b), 1)
        self.assertEqual(debtors_b[0]["name"], "Bob")
        self.assertEqual(debtors_b[0]["balance"], 2000)

    async def test_admin_bootstrap_isolated_per_bot(self):
        bot_a, dp_a = _build_bot_dispatcher(self.config_a)
        bot_b, dp_b = _build_bot_dispatcher(self.config_b)
        await dp_a.feed_webhook_update(bot_a, _text_update(1, 111, "/start"))
        await dp_b.feed_webhook_update(bot_b, _text_update(1, 999, "/start"))

        self.assertEqual(dbt._load_admins(self.config_a.admins_file), {"111"})
        self.assertEqual(dbt._load_admins(self.config_b.admins_file), {"999"})

    async def test_same_user_id_sees_only_own_bots_debtors(self):
        """Cross-bot leak check with the SAME Telegram user_id acting as admin
        on both bots — this is the exact bug class every isolation test in
        this codebase exists to catch."""
        SAME_USER = 555
        id_a = await dbt._insert_debtor(self.config_a.db_path, "Only on A", None)
        await dbt._insert_entry(self.config_a.db_path, id_a, 500, None)

        bot_a, dp_a = _build_bot_dispatcher(self.config_a)
        bot_b, dp_b = _build_bot_dispatcher(self.config_b)
        await dp_a.feed_webhook_update(bot_a, _text_update(1, SAME_USER, "/start"))
        await dp_b.feed_webhook_update(bot_b, _text_update(1, SAME_USER, "/start"))

        debtors_a = await dbt._debtors_with_balance(self.config_a.db_path)
        debtors_b = await dbt._debtors_with_balance(self.config_b.db_path)
        self.assertEqual(len(debtors_a), 1)
        self.assertEqual(len(debtors_b), 0)


class BalanceCorrectnessTests(unittest.IsolatedAsyncioTestCase):
    """Headline claim of the signed-ledger design: остаток = SUM(amount),
    with a repayment as a new row of opposite sign rather than an edit."""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = dbt.config_from_bot_row(
            {"bot_id": 701, "name": "debtors_balance_bot", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await dbt.init_db(self.config.db_path)

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def test_single_debt_gives_positive_balance(self):
        debtor_id = await dbt._insert_debtor(self.config.db_path, "Ivan", None)
        await dbt._insert_entry(self.config.db_path, debtor_id, 1000, "дал в долг на бензин")
        stats = await dbt._debtor_stats(self.config.db_path, debtor_id)
        self.assertEqual(stats["balance"], 1000)

    async def test_partial_repayment_is_a_new_row_not_an_edit(self):
        debtor_id = await dbt._insert_debtor(self.config.db_path, "Ivan", None)
        entry_id = await dbt._insert_entry(self.config.db_path, debtor_id, 1000, "дал в долг")
        await dbt._insert_entry(self.config.db_path, debtor_id, -300, "частичный возврат")

        original = await dbt._entry_row(self.config.db_path, entry_id)
        self.assertEqual(original["amount"], 1000)  # untouched

        entries = await dbt._debtor_entries(self.config.db_path, debtor_id)
        self.assertEqual(len(entries), 2)

        stats = await dbt._debtor_stats(self.config.db_path, debtor_id)
        self.assertEqual(stats["balance"], 700)

    async def test_mixed_debt_and_repayment_sequence_running_balance(self):
        """Simulates a realistic mixed history: lend, repay part, lend more,
        repay fully, then borrow from them — balance must be correct after
        EVERY step, not just at the end."""
        debtor_id = await dbt._insert_debtor(self.config.db_path, "Ivan", None)
        expected = 0

        for amount, note in [
            (500, "дал в долг на бензин"),      # +500  -> 500
            (-200, "вернул часть"),              # -200  -> 300
            (300, "занял на аренду"),            # +300  -> 600
            (-600, "погасил всё"),               # -600  -> 0
            (-150, "я занял у него"),             # -150  -> -150 (owner now owes)
            (150, "я вернул"),                    # +150  -> 0
        ]:
            await dbt._insert_entry(self.config.db_path, debtor_id, amount, note)
            expected += amount
            stats = await dbt._debtor_stats(self.config.db_path, debtor_id)
            self.assertEqual(stats["balance"], expected, f"after {note!r}: expected {expected}")

    async def test_overpayment_flips_balance_direction(self):
        """A repayment larger than the outstanding debt is allowed (money
        really can change hands that way) and correctly flips the sign."""
        debtor_id = await dbt._insert_debtor(self.config.db_path, "Ivan", None)
        await dbt._insert_entry(self.config.db_path, debtor_id, 100, "дал в долг")
        await dbt._insert_entry(self.config.db_path, debtor_id, -250, "вернул с запасом")
        stats = await dbt._debtor_stats(self.config.db_path, debtor_id)
        self.assertEqual(stats["balance"], -150)  # now the owner owes the debtor

    async def test_deleting_an_entry_recalculates_balance(self):
        debtor_id = await dbt._insert_debtor(self.config.db_path, "Ivan", None)
        e1 = await dbt._insert_entry(self.config.db_path, debtor_id, 1000, "loan")
        e2 = await dbt._insert_entry(self.config.db_path, debtor_id, -400, "repay")
        stats = await dbt._debtor_stats(self.config.db_path, debtor_id)
        self.assertEqual(stats["balance"], 600)

        await dbt._delete_entry(self.config.db_path, e2)
        stats = await dbt._debtor_stats(self.config.db_path, debtor_id)
        self.assertEqual(stats["balance"], 1000)

    async def test_deleting_debtor_cascades_entries(self):
        debtor_id = await dbt._insert_debtor(self.config.db_path, "Ivan", None)
        await dbt._insert_entry(self.config.db_path, debtor_id, 500, None)
        await dbt._insert_entry(self.config.db_path, debtor_id, -100, None)

        await dbt._delete_debtor(self.config.db_path, debtor_id)

        self.assertIsNone(await dbt._debtor_row(self.config.db_path, debtor_id))
        conn = sqlite3.connect(self.config.db_path)
        remaining = conn.execute("SELECT COUNT(*) FROM debt_entries WHERE debtor_id=?", (debtor_id,)).fetchone()[0]
        conn.close()
        self.assertEqual(remaining, 0)

    async def test_debtor_with_no_entries_has_zero_balance(self):
        debtor_id = await dbt._insert_debtor(self.config.db_path, "Empty", None)
        stats = await dbt._debtor_stats(self.config.db_path, debtor_id)
        self.assertEqual(stats["balance"], 0)
        self.assertIsNone(stats["last_entry_date"])

    async def test_sort_owed_to_me_descending(self):
        d1 = await dbt._insert_debtor(self.config.db_path, "Small", None)
        d2 = await dbt._insert_debtor(self.config.db_path, "Big", None)
        await dbt._insert_entry(self.config.db_path, d1, 100, None)
        await dbt._insert_entry(self.config.db_path, d2, 900, None)

        all_debtors = await dbt._debtors_with_balance(self.config.db_path)
        sorted_debtors = dbt._sort_debtors(all_debtors, "owed_to_me")
        self.assertEqual([d["name"] for d in sorted_debtors], ["Big", "Small"])

    async def test_sort_i_owe_ascending_most_negative_first(self):
        d1 = await dbt._insert_debtor(self.config.db_path, "OweLittle", None)
        d2 = await dbt._insert_debtor(self.config.db_path, "OweALot", None)
        await dbt._insert_entry(self.config.db_path, d1, -50, None)
        await dbt._insert_entry(self.config.db_path, d2, -500, None)

        all_debtors = await dbt._debtors_with_balance(self.config.db_path)
        sorted_debtors = dbt._sort_debtors(all_debtors, "i_owe")
        self.assertEqual([d["name"] for d in sorted_debtors], ["OweALot", "OweLittle"])

    async def test_filter_settled_includes_zero_balance_and_no_entries(self):
        d1 = await dbt._insert_debtor(self.config.db_path, "Settled", None)
        await dbt._insert_entry(self.config.db_path, d1, 100, None)
        await dbt._insert_entry(self.config.db_path, d1, -100, None)
        d2 = await dbt._insert_debtor(self.config.db_path, "NeverUsed", None)
        d3 = await dbt._insert_debtor(self.config.db_path, "StillOwed", None)
        await dbt._insert_entry(self.config.db_path, d3, 50, None)

        all_debtors = await dbt._debtors_with_balance(self.config.db_path)
        settled = dbt._filter_debtors(all_debtors, "settled")
        self.assertEqual({d["name"] for d in settled}, {"Settled", "NeverUsed"})

    async def test_amount_parsing_rejects_non_positive_and_absurd_values(self):
        self.assertIsNone(dbt._parse_amount("0"))
        self.assertIsNone(dbt._parse_amount("abc"))
        self.assertIsNone(dbt._parse_amount(str(dbt.AMOUNT_MAX + 1)))
        self.assertEqual(dbt._parse_amount("1 500"), 1500)

    async def test_amount_parsing_rejects_fractional_whole_units_only(self):
        """Review-found: _fmt_amount always displays with zero decimals
        (",.0f"), used everywhere a balance is shown to the admin. Silently
        accepting "250.5" would let a real fractional remainder round away
        on screen, becoming indistinguishable from a fully settled debt —
        so fractional input must be rejected at the boundary instead,
        matching shop_catalog.py's whole-currency-unit convention."""
        self.assertIsNone(dbt._parse_amount("250.5"))
        self.assertIsNone(dbt._parse_amount("250,5"))
        self.assertEqual(dbt._parse_amount("250.0"), 250.0)

    async def test_amount_parsing_strips_sign_direction_comes_from_buttons_not_typed_minus(self):
        """Direction ("мне должны"/"я должен") is chosen via the ➕/➖ buttons
        before this prompt is even shown — a typed "-100" is not a valid way
        to express direction, so the minus is stripped like any other
        non-digit character (same convention as accountant.py's amount
        parser), yielding a plain positive magnitude rather than a rejection."""
        self.assertEqual(dbt._parse_amount("-100"), 100.0)


class ButtonOnlyUIAndAccessTests(unittest.IsolatedAsyncioTestCase):
    """Owner-mandated permanent design rule: no raw text command lists —
    every action is a clickable button. Also confirms the whole bot is
    admin-gated (personal owner tool, no separate client role)."""

    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._mock_call = self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = dbt.config_from_bot_row(
            {"bot_id": 801, "name": "debtors_ui_bot", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await dbt.init_db(self.config.db_path)
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
        found_debtors_btn = any(
            btn.text == "👥 Должники"
            for method in sent for row in method.reply_markup.keyboard for btn in row
        )
        self.assertTrue(found_debtors_btn)

    async def test_non_admin_gets_no_access_message_not_the_menu(self):
        await self.dp.feed_webhook_update(self.bot, _text_update(1, 42, "/start"))  # 42 becomes first admin
        self._mock_call.reset_mock()
        await self.dp.feed_webhook_update(self.bot, _text_update(2, 99, "/start"))  # 99 is not an admin

        texts = [
            call.args[0].text for call in self._mock_call.call_args_list
            if call.args and hasattr(call.args[0], "text")
        ]
        self.assertTrue(any(dbt.NO_ACCESS_TEXT in t for t in texts if t))
        no_keyboards = [
            call.args[0] for call in self._mock_call.call_args_list
            if call.args and hasattr(call.args[0], "reply_markup") and call.args[0].reply_markup
        ]
        self.assertEqual(no_keyboards, [])

    async def test_non_admin_cannot_list_debtors(self):
        await self.dp.feed_webhook_update(self.bot, _text_update(1, 42, "/start"))  # 42 becomes admin
        self._mock_call.reset_mock()
        await self.dp.feed_webhook_update(self.bot, _text_update(2, 99, "👥 Должники"))
        texts = [
            call.args[0].text for call in self._mock_call.call_args_list
            if call.args and hasattr(call.args[0], "text")
        ]
        self.assertTrue(any(dbt.NO_ACCESS_TEXT in t for t in texts if t))

    async def test_reminder_button_hidden_for_zero_or_negative_balance(self):
        debtor_id = await dbt._insert_debtor(self.config.db_path, "Ivan", None)
        await dbt._insert_entry(self.config.db_path, debtor_id, -50, None)  # owner owes -> balance < 0
        kb = dbt.kb_debtor_card(debtor_id, -50)
        all_buttons = [b.text for row in kb.inline_keyboard for b in row]
        self.assertFalse(any("Напомнить" in t for t in all_buttons))

    async def test_reminder_button_shown_for_positive_balance(self):
        kb = dbt.kb_debtor_card(1, 500)
        all_buttons = [b.text for row in kb.inline_keyboard for b in row]
        self.assertTrue(any("Напомнить" in t for t in all_buttons))

    async def test_reminder_does_not_send_message_to_debtor_only_shows_copyable_text(self):
        """Domain rule: this is NOT a payment/collection tool — the bot must
        never message the debtor directly, only render text for the admin to
        copy. There's no debtor Telegram identity stored at all, so a direct
        send is structurally impossible, but this test locks in that the
        reminder handler only calls answer() on the admin's own chat."""
        ADMIN_ID = 42
        await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/start"))
        debtor_id = await dbt._insert_debtor(self.config.db_path, "Ivan", None)
        await dbt._insert_entry(self.config.db_path, debtor_id, 1000, "дал в долг")

        self._mock_call.reset_mock()
        update = _callback_update(2, ADMIN_ID, f"dbt_remind:{debtor_id}")
        await self.dp.feed_webhook_update(self.bot, update)

        sent = [call.args[0] for call in self._mock_call.call_args_list if call.args]
        send_messages = [m for m in sent if m.__class__.__name__ == "SendMessage"]
        self.assertTrue(len(send_messages) >= 1)
        for m in send_messages:
            self.assertEqual(m.chat_id, ADMIN_ID)
        self.assertTrue(any("Ivan" in m.text or "Скопируйте" in m.text for m in send_messages))


class ReviewFixRegressionTests(unittest.IsolatedAsyncioTestCase):
    """Locks in fixes made during pre-commit review (matches the convention
    established by shop_catalog.py's own ReviewFixRegressionTests)."""

    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._mock_call = self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = dbt.config_from_bot_row(
            {"bot_id": 901, "name": "debtors_regression_bot", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await dbt.init_db(self.config.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_debtors_list_text_in_a_group_chat_does_not_leak_the_list(self):
        """Review-found: debtors_list/admins_panel_entry were the only two
        top-level handlers missing F.chat.type == "private" — matching the
        exact button text in a GROUP chat the bot happens to be in must not
        post personal debt data where every group member can see it."""
        ADMIN_ID = 42
        GROUP_CHAT_ID = -100123456
        await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/start"))  # 42 becomes admin
        debtor_id = await dbt._insert_debtor(self.config.db_path, "Ivan", None)
        await dbt._insert_entry(self.config.db_path, debtor_id, 1000, "дал в долг")

        self._mock_call.reset_mock()
        update = _group_text_update(2, ADMIN_ID, GROUP_CHAT_ID, "👥 Должники")
        await self.dp.feed_webhook_update(self.bot, update)

        sent_texts = [
            call.args[0].text for call in self._mock_call.call_args_list
            if call.args and hasattr(call.args[0], "text") and call.args[0].text
        ]
        self.assertFalse(any("Ivan" in t or "Должники" in t for t in sent_texts))

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

    async def test_concurrent_skip_and_text_description_does_not_double_insert(self):
        """Review-found: a fast double-tap on "Пропустить" racing a text
        description submit used to be able to insert the same debt entry
        twice, since state.clear() happened AFTER the insert with no guard
        in between. The _busy_entry_saves guard must make the second
        concurrent call a no-op."""
        debtor_id = await dbt._insert_debtor(self.config.db_path, "Ivan", None)
        state_data = {"started_at": 0, "debtor_id": debtor_id, "amount": 500, "sign": 1}

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
            dbt._save_entry_and_reply(fake_msg, state_a, self.config, "first", edit=True),
            dbt._save_entry_and_reply(fake_msg, state_b, self.config, "second", edit=True),
        )

        entries = await dbt._debtor_entries(self.config.db_path, debtor_id)
        self.assertEqual(len(entries), 1)
        stats = await dbt._debtor_stats(self.config.db_path, debtor_id)
        self.assertEqual(stats["balance"], 500)

    async def test_forged_non_numeric_callback_data_does_not_crash(self):
        """Review-found: int(cb.data.split(...)) without a guard would raise
        on a forged/malformed callback_data (Telegram's API does not
        restrict callback_query.data to values the bot actually sent).
        _parse_id must make every such handler degrade gracefully instead."""
        ADMIN_ID = 42
        await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/start"))

        for data in ("dbt_view:not_a_number", "dbt_del_confirm:xyz", "dbt_entry_del:abc:def"):
            with self.subTest(data=data):
                # Must not raise — feed_webhook_update would propagate any
                # unhandled exception from inside the handler.
                await self.dp.feed_webhook_update(self.bot, _callback_update(99, ADMIN_ID, data))


class DebtorsMiniAppConfigTests(unittest.IsolatedAsyncioTestCase):
    """miniapp_config's declared table/field names must match init_db()'s
    real schema — miniapp_api.py builds SQL directly off these names, so a
    drift here would 500 at request time instead of failing a test."""

    def test_miniapp_config_resource_names(self):
        names = {r["name"] for r in dbt.miniapp_config["resources"]}
        self.assertEqual(names, {"debtors", "debt_entries"})

    def test_debtors_resource_targets_debtors_table(self):
        debtors = next(r for r in dbt.miniapp_config["resources"] if r["name"] == "debtors")
        self.assertEqual(debtors["table"], "debtors")
        self.assertTrue(debtors["creatable"])

    def test_debt_entries_resource_targets_debt_entries_table(self):
        entries = next(r for r in dbt.miniapp_config["resources"] if r["name"] == "debt_entries")
        self.assertEqual(entries["table"], "debt_entries")
        field_names = {f["name"] for f in entries["fields"]}
        self.assertEqual(field_names, {"debtor_id", "amount", "description", "entry_date"})

    async def test_miniapp_config_fields_match_real_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "schema_check.db")
            await dbt.init_db(db_path)
            conn = sqlite3.connect(db_path)
            try:
                for resource in dbt.miniapp_config["resources"]:
                    cur = conn.execute(f"PRAGMA table_info({resource['table']})")
                    real_columns = {row[1] for row in cur.fetchall()}
                    declared = {f["name"] for f in resource["fields"]} | {"id"}
                    self.assertTrue(
                        declared.issubset(real_columns),
                        f"{resource['name']}: declared fields {declared} not all in "
                        f"real columns {real_columns}",
                    )
            finally:
                conn.close()


class DebtorsAdminBootstrapSecurityTests(unittest.IsolatedAsyncioTestCase):
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
        config = dbt.config_from_bot_row(
            {"bot_id": 926, "name": "debtors_bot_owned", "display_name": None,
             "group_chat_id": None, "owner_telegram_id": 12345},
            self.data_dir,
        )
        await dbt.init_db(config.db_path)
        bot, dp = _build_bot_dispatcher(config)

        CLIENT_ID = 555  # not the owner, messages first
        await dp.feed_webhook_update(bot, _text_update(1, CLIENT_ID, "/start"))
        self.assertEqual(dbt._load_admins(config.admins_file), set())
        self.assertFalse(dbt._is_bot_admin(CLIENT_ID, config))

        await dp.feed_webhook_update(bot, _text_update(2, 12345, "/start"))
        self.assertTrue(dbt._is_bot_admin(12345, config))
        self.assertEqual(dbt._load_admins(config.admins_file), {"12345"})

    async def test_owner_is_always_admin_even_with_stale_admins_file(self):
        config = dbt.config_from_bot_row(
            {"bot_id": 927, "name": "debtors_bot_owned_2", "display_name": None,
             "group_chat_id": None, "owner_telegram_id": 777},
            self.data_dir,
        )
        await dbt.init_db(config.db_path)
        dbt._save_admins(config.admins_file, {"999999"})  # some other id, not the owner
        self.assertTrue(dbt._is_bot_admin(777, config))  # owner: always admin
        self.assertTrue(dbt._is_bot_admin(999999, config))  # still honors the file's own admin
        self.assertFalse(dbt._is_bot_admin(4242, config))  # neither owner nor in the file

    async def test_bootstrap_admin_syncs_to_central_bot_admins_table(self):
        config = dbt.config_from_bot_row(
            {"bot_id": 928, "name": "debtors_bot_synced", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await dbt.init_db(config.db_path)
        bot, dp = _build_bot_dispatcher(config)
        await dp.feed_webhook_update(bot, _text_update(1, 321, "/start"))

        central_admins = await db_module.get_bot_admins(928)
        self.assertEqual(central_admins, ["321"])


if __name__ == "__main__":
    unittest.main()
