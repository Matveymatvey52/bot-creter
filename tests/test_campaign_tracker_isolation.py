"""campaign_tracker template — data isolation and domain-flow tests.

Standard criterion: two bots on the SAME template, different config, must
never mix data — even driven by the SAME Telegram user_id.

PLUS domain-specific checks matching this template's design (owner brief):
manual metric entry (no external ad-account integration), running totals
derived from an append-only metric_entries log, and the comparison report's
cost-per-lead / conversion-rate math — including the zero-leads guard and
sort order.

No real Telegram network calls, no real tokens.

Run with: python -m unittest tests.test_campaign_tracker_isolation
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import EditMessageText, SendMessage

from runtime.registry import get_template_router
from templates import campaign_tracker

FAKE_TOKEN = "123456:test-token-not-real"
ADMIN_ID = 999
OTHER_ID = 111


class FakeBotAPI:
    """Replaces Bot.__call__. Records every aiogram method call so tests can
    inspect the text actually sent/edited — same technique as
    tests/test_moderator_isolation.py's FakeBotAPI."""

    def __init__(self):
        self.calls: list = []

    async def __call__(self, request, **kwargs):
        self.calls.append(request)
        return object()

    def sent_texts(self) -> list[str]:
        out = []
        for c in self.calls:
            if isinstance(c, (SendMessage, EditMessageText)):
                out.append(c.text)
        return out


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


def _build_bot_dispatcher(config: campaign_tracker.CampaignTrackerConfig) -> tuple[Bot, Dispatcher]:
    bot = Bot(token=FAKE_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(campaign_tracker.ConfigMiddleware(config))
    dp.include_router(get_template_router("campaign_tracker"))
    return bot, dp


def _campaigns(db_path: str) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM campaigns").fetchall()
    conn.close()
    return rows


def _metric_entries(db_path: str) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM metric_entries").fetchall()
    conn.close()
    return rows


class CampaignTrackerIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._fake_api = FakeBotAPI()
        self._bot_call_patcher = patch.object(Bot, "__call__", new=self._fake_api)
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

        self.config_a = campaign_tracker.config_from_bot_row(
            {"bot_id": 901, "name": "camp_isolation_bot_a", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        self.config_b = campaign_tracker.config_from_bot_row(
            {"bot_id": 902, "name": "camp_isolation_bot_b", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await campaign_tracker.init_db(self.config_a.db_path)
        await campaign_tracker.init_db(self.config_b.db_path)
        self.bot_a, self.dp_a = _build_bot_dispatcher(self.config_a)
        self.bot_b, self.dp_b = _build_bot_dispatcher(self.config_b)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_configs_point_to_different_files(self):
        self.assertNotEqual(self.config_a.db_path, self.config_b.db_path)

    async def test_same_user_id_is_independent_admin_bootstrap_per_bot(self):
        # SAME Telegram user_id bootstraps as admin on both bots — each bot's
        # admins_file must be populated independently (different files).
        await self.dp_a.feed_webhook_update(self.bot_a, _text_update(1, ADMIN_ID, "/start"))
        await self.dp_b.feed_webhook_update(self.bot_b, _text_update(1, ADMIN_ID, "/start"))
        self.assertTrue(campaign_tracker._is_admin(ADMIN_ID, self.config_a))
        self.assertTrue(campaign_tracker._is_admin(ADMIN_ID, self.config_b))
        self.assertNotEqual(self.config_a.admins_file, self.config_b.admins_file)

    async def test_campaign_created_on_bot_a_does_not_leak_into_bot_b(self):
        await self.dp_a.feed_webhook_update(self.bot_a, _text_update(1, ADMIN_ID, "/start"))
        await self.dp_a.feed_webhook_update(self.bot_a, _text_update(2, ADMIN_ID, "➕ Новая кампания"))
        await self.dp_a.feed_webhook_update(self.bot_a, _text_update(3, ADMIN_ID, "Летняя распродажа"))
        await self.dp_a.feed_webhook_update(
            self.bot_a, _callback_update(4, ADMIN_ID, "camp_channel:Instagram")
        )
        await self.dp_a.feed_webhook_update(self.bot_a, _text_update(5, ADMIN_ID, "10000"))
        await self.dp_a.feed_webhook_update(self.bot_a, _text_update(6, ADMIN_ID, "01.03.2026"))
        await self.dp_a.feed_webhook_update(self.bot_a, _callback_update(7, ADMIN_ID, "camp_noend"))

        rows_a = _campaigns(self.config_a.db_path)
        self.assertEqual(len(rows_a), 1)
        self.assertEqual(rows_a[0]["name"], "Летняя распродажа")

        rows_b = _campaigns(self.config_b.db_path)
        self.assertEqual(rows_b, [])


class CampaignCreationFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._fake_api = FakeBotAPI()
        self._bot_call_patcher = patch.object(Bot, "__call__", new=self._fake_api)
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = campaign_tracker.config_from_bot_row(
            {"bot_id": 903, "name": "camp_flow_bot", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await campaign_tracker.init_db(self.config.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)
        await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/start"))  # bootstraps admin

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_full_creation_with_explicit_end_date(self):
        await self.dp.feed_webhook_update(self.bot, _text_update(2, ADMIN_ID, "➕ Новая кампания"))
        await self.dp.feed_webhook_update(self.bot, _text_update(3, ADMIN_ID, "Осенняя акция"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(4, ADMIN_ID, "camp_channel:Яндекс.Директ"))
        await self.dp.feed_webhook_update(self.bot, _text_update(5, ADMIN_ID, "20000"))
        await self.dp.feed_webhook_update(self.bot, _text_update(6, ADMIN_ID, "01.09.2026"))
        await self.dp.feed_webhook_update(self.bot, _text_update(7, ADMIN_ID, "30.09.2026"))

        rows = _campaigns(self.config.db_path)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["name"], "Осенняя акция")
        self.assertEqual(row["channel"], "Яндекс.Директ")
        self.assertEqual(row["budget"], 20000.0)
        self.assertEqual(row["start_date"], "2026-09-01")
        self.assertEqual(row["end_date"], "2026-09-30")
        self.assertEqual(row["status"], "active")
        self.assertEqual(row["created_by"], ADMIN_ID)

    async def test_end_date_before_start_date_is_rejected_and_reprompted(self):
        await self.dp.feed_webhook_update(self.bot, _text_update(2, ADMIN_ID, "➕ Новая кампания"))
        await self.dp.feed_webhook_update(self.bot, _text_update(3, ADMIN_ID, "Кампания"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(4, ADMIN_ID, "camp_channel:Офлайн"))
        await self.dp.feed_webhook_update(self.bot, _text_update(5, ADMIN_ID, "5000"))
        await self.dp.feed_webhook_update(self.bot, _text_update(6, ADMIN_ID, "10.05.2026"))
        await self.dp.feed_webhook_update(self.bot, _text_update(7, ADMIN_ID, "01.05.2026"))  # before start

        self.assertEqual(_campaigns(self.config.db_path), [])  # not created
        # A valid end date afterwards must still complete the flow.
        await self.dp.feed_webhook_update(self.bot, _text_update(8, ADMIN_ID, "15.05.2026"))
        rows = _campaigns(self.config.db_path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["end_date"], "2026-05-15")

    async def test_invalid_budget_is_rejected_and_reprompted(self):
        await self.dp.feed_webhook_update(self.bot, _text_update(2, ADMIN_ID, "➕ Новая кампания"))
        await self.dp.feed_webhook_update(self.bot, _text_update(3, ADMIN_ID, "Кампания"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(4, ADMIN_ID, "camp_channel:Instagram"))
        await self.dp.feed_webhook_update(self.bot, _text_update(5, ADMIN_ID, "не число"))
        self.assertEqual(_campaigns(self.config.db_path), [])
        await self.dp.feed_webhook_update(self.bot, _text_update(6, ADMIN_ID, "1000"))
        await self.dp.feed_webhook_update(self.bot, _text_update(7, ADMIN_ID, "01.01.2026"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(8, ADMIN_ID, "camp_noend"))
        rows = _campaigns(self.config.db_path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["budget"], 1000.0)

    async def test_non_admin_cannot_start_campaign_creation(self):
        await self.dp.feed_webhook_update(self.bot, _text_update(2, OTHER_ID, "➕ Новая кампания"))
        await self.dp.feed_webhook_update(self.bot, _text_update(3, OTHER_ID, "Should not become a name"))
        self.assertEqual(_campaigns(self.config.db_path), [])


class MetricsEntryAndTotalsTests(unittest.IsolatedAsyncioTestCase):
    """Manual metric entry is append-only (metric_entries); campaign totals
    are always the SUM over that log — never a separately-maintained counter
    that could drift from the log."""

    async def asyncSetUp(self):
        self._fake_api = FakeBotAPI()
        self._bot_call_patcher = patch.object(Bot, "__call__", new=self._fake_api)
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = campaign_tracker.config_from_bot_row(
            {"bot_id": 904, "name": "camp_metrics_bot", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await campaign_tracker.init_db(self.config.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)
        await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/start"))
        import aiosqlite
        async with aiosqlite.connect(self.config.db_path) as db:
            cur = await db.execute(
                "INSERT INTO campaigns (name, channel, budget, start_date, created_by) VALUES (?,?,?,?,?)",
                ("Test Campaign", "Instagram", 1000.0, "2026-01-01", ADMIN_ID)
            )
            await db.commit()
            self.campaign_id = cur.lastrowid

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def _enter_metrics(self, update_id_start: int, user_id: int, clicks: str, leads: str, sales: str):
        await self.dp.feed_webhook_update(
            self.bot, _callback_update(update_id_start, user_id, f"camp_metrics:{self.campaign_id}")
        )
        await self.dp.feed_webhook_update(self.bot, _text_update(update_id_start + 1, user_id, clicks))
        await self.dp.feed_webhook_update(self.bot, _text_update(update_id_start + 2, user_id, leads))
        await self.dp.feed_webhook_update(self.bot, _text_update(update_id_start + 3, user_id, sales))

    async def test_single_metrics_entry_is_recorded(self):
        await self._enter_metrics(10, ADMIN_ID, "100", "10", "2")
        entries = _metric_entries(self.config.db_path)
        self.assertEqual(len(entries), 1)
        self.assertEqual((entries[0]["clicks"], entries[0]["leads"], entries[0]["sales"]), (100, 10, 2))
        self.assertEqual(entries[0]["entered_by"], ADMIN_ID)

    async def test_totals_accumulate_across_multiple_entries(self):
        await self._enter_metrics(10, ADMIN_ID, "100", "10", "2")
        await self._enter_metrics(20, ADMIN_ID, "50", "5", "1")

        import aiosqlite
        async def _totals():
            async with aiosqlite.connect(self.config.db_path) as db:
                return await campaign_tracker._campaign_totals(db, self.campaign_id)
        totals = await _totals()
        self.assertEqual(totals, {"clicks": 150, "leads": 15, "sales": 3})
        self.assertEqual(len(_metric_entries(self.config.db_path)), 2)

    async def test_non_admin_cannot_enter_metrics(self):
        await self._enter_metrics(10, OTHER_ID, "100", "10", "2")
        self.assertEqual(_metric_entries(self.config.db_path), [])

    async def test_non_numeric_metric_is_rejected_and_reprompted(self):
        await self.dp.feed_webhook_update(
            self.bot, _callback_update(10, ADMIN_ID, f"camp_metrics:{self.campaign_id}")
        )
        await self.dp.feed_webhook_update(self.bot, _text_update(11, ADMIN_ID, "not a number"))
        await self.dp.feed_webhook_update(self.bot, _text_update(12, ADMIN_ID, "100"))
        await self.dp.feed_webhook_update(self.bot, _text_update(13, ADMIN_ID, "10"))
        await self.dp.feed_webhook_update(self.bot, _text_update(14, ADMIN_ID, "2"))
        entries = _metric_entries(self.config.db_path)
        self.assertEqual(len(entries), 1)
        self.assertEqual((entries[0]["clicks"], entries[0]["leads"], entries[0]["sales"]), (100, 10, 2))


class FinishAndDeleteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._fake_api = FakeBotAPI()
        self._bot_call_patcher = patch.object(Bot, "__call__", new=self._fake_api)
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = campaign_tracker.config_from_bot_row(
            {"bot_id": 905, "name": "camp_finish_bot", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await campaign_tracker.init_db(self.config.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)
        await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/start"))
        import aiosqlite
        async with aiosqlite.connect(self.config.db_path) as db:
            cur = await db.execute(
                "INSERT INTO campaigns (name, channel, budget, start_date, created_by) VALUES (?,?,?,?,?)",
                ("Test Campaign", "Instagram", 1000.0, "2026-01-01", ADMIN_ID)
            )
            await db.commit()
            self.campaign_id = cur.lastrowid

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_double_tap_finish_is_idempotent(self):
        await self.dp.feed_webhook_update(self.bot, _callback_update(10, ADMIN_ID, f"camp_finish:{self.campaign_id}"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(11, ADMIN_ID, f"camp_finish:{self.campaign_id}"))
        rows = _campaigns(self.config.db_path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "finished")

    async def test_delete_removes_campaign_and_its_metric_entries(self):
        import aiosqlite
        async with aiosqlite.connect(self.config.db_path) as db:
            await db.execute(
                "INSERT INTO metric_entries (campaign_id, clicks, leads, sales, entered_by) VALUES (?,?,?,?,?)",
                (self.campaign_id, 10, 1, 0, ADMIN_ID)
            )
            await db.commit()
        await self.dp.feed_webhook_update(self.bot, _callback_update(10, ADMIN_ID, f"camp_delete:{self.campaign_id}"))
        self.assertEqual(_campaigns(self.config.db_path), [])
        self.assertEqual(_metric_entries(self.config.db_path), [])

    async def test_non_admin_cannot_finish_or_delete(self):
        await self.dp.feed_webhook_update(self.bot, _callback_update(10, OTHER_ID, f"camp_finish:{self.campaign_id}"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(11, OTHER_ID, f"camp_delete:{self.campaign_id}"))
        rows = _campaigns(self.config.db_path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "active")


class ComparisonReportTests(unittest.IsolatedAsyncioTestCase):
    """Owner-specified design: compare campaigns by cost-per-lead and
    conversion rate. CPL = budget/leads, conversion = sales/leads*100 — both
    must guard leads==0 (division by zero) rather than crash, and campaigns
    with no leads yet must be reported separately, not ranked."""

    async def asyncSetUp(self):
        self._fake_api = FakeBotAPI()
        self._bot_call_patcher = patch.object(Bot, "__call__", new=self._fake_api)
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = campaign_tracker.config_from_bot_row(
            {"bot_id": 906, "name": "camp_compare_bot", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await campaign_tracker.init_db(self.config.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)
        await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/start"))

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def _make_campaign(self, name: str, budget: float) -> int:
        import aiosqlite
        async with aiosqlite.connect(self.config.db_path) as db:
            cur = await db.execute(
                "INSERT INTO campaigns (name, channel, budget, start_date, created_by) VALUES (?,?,?,?,?)",
                (name, "Instagram", budget, "2026-01-01", ADMIN_ID)
            )
            await db.commit()
            return cur.lastrowid

    async def _add_metrics(self, campaign_id: int, clicks: int, leads: int, sales: int):
        import aiosqlite
        async with aiosqlite.connect(self.config.db_path) as db:
            await db.execute(
                "INSERT INTO metric_entries (campaign_id, clicks, leads, sales, entered_by) VALUES (?,?,?,?,?)",
                (campaign_id, clicks, leads, sales, ADMIN_ID)
            )
            await db.commit()

    async def test_cheaper_cost_per_lead_ranks_first(self):
        cheap_id = await self._make_campaign("Cheap", 1000.0)
        await self._add_metrics(cheap_id, 100, 20, 2)  # CPL 50
        expensive_id = await self._make_campaign("Expensive", 5000.0)
        await self._add_metrics(expensive_id, 100, 10, 1)  # CPL 500

        await self.dp.feed_webhook_update(self.bot, _text_update(2, ADMIN_ID, "📊 Сравнение"))
        texts = self._fake_api.sent_texts()
        report = next(t for t in texts if "Сравнение кампаний" in t)
        self.assertLess(report.index("Cheap"), report.index("Expensive"))
        self.assertIn("CPL 50.00", report)
        self.assertIn("конверсия 10.0%", report)  # 2/20*100

    async def test_campaign_with_zero_leads_is_listed_separately_not_crashed(self):
        no_leads_id = await self._make_campaign("No Leads Yet", 2000.0)
        await self._add_metrics(no_leads_id, 50, 0, 0)

        await self.dp.feed_webhook_update(self.bot, _text_update(2, ADMIN_ID, "📊 Сравнение"))
        texts = self._fake_api.sent_texts()
        report = next(t for t in texts if "Сравнение кампаний" in t)
        self.assertIn("Без данных о заявках", report)
        self.assertIn("No Leads Yet", report)

    async def test_finished_campaigns_are_excluded_from_comparison(self):
        finished_id = await self._make_campaign("Finished", 1000.0)
        await self._add_metrics(finished_id, 100, 10, 1)
        await self.dp.feed_webhook_update(self.bot, _callback_update(2, ADMIN_ID, f"camp_finish:{finished_id}"))

        await self.dp.feed_webhook_update(self.bot, _text_update(3, ADMIN_ID, "📊 Сравнение"))
        texts = self._fake_api.sent_texts()
        self.assertTrue(any("Нет активных кампаний" in t for t in texts))

    async def test_non_admin_cannot_view_comparison(self):
        await self.dp.feed_webhook_update(self.bot, _text_update(2, OTHER_ID, "📊 Сравнение"))
        texts = self._fake_api.sent_texts()
        self.assertTrue(any("Нет доступа" in t for t in texts))
        self.assertFalse(any("Сравнение кампаний" in t for t in texts))


class ReviewFixRegressionTests(unittest.IsolatedAsyncioTestCase):
    """One test per bug found by the review-orchestrator pass (clean-code,
    qa-stability, monkey-tester) on this template. Each test reproduces the
    exact failure mode the reviewer described before the fix landed."""

    async def asyncSetUp(self):
        self._fake_api = FakeBotAPI()
        self._bot_call_patcher = patch.object(Bot, "__call__", new=self._fake_api)
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = campaign_tracker.config_from_bot_row(
            {"bot_id": 907, "name": "camp_regression_bot", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await campaign_tracker.init_db(self.config.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)
        await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/start"))

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_huge_callback_id_does_not_crash_view(self):
        # monkey-tester: a 20-digit id sails through int() (unlimited
        # precision) but overflows SQLite's 64-bit INTEGER bind. Must be
        # rejected up front with a normal message, not an uncaught
        # OverflowError deep inside db.execute.
        huge_id = "9" * 25
        await self.dp.feed_webhook_update(self.bot, _callback_update(2, ADMIN_ID, f"camp_view:{huge_id}"))
        texts = self._fake_api.sent_texts()
        self.assertIn("Некорректная кампания.", texts)

    async def test_huge_callback_id_does_not_crash_finish_or_delete(self):
        huge_id = "9" * 25
        await self.dp.feed_webhook_update(self.bot, _callback_update(2, ADMIN_ID, f"camp_finish:{huge_id}"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(3, ADMIN_ID, f"camp_delete:{huge_id}"))
        # No campaign existed at all — both must be silent no-ops, not crashes.
        self.assertEqual(_campaigns(self.config.db_path), [])

    async def test_huge_metric_value_is_rejected_not_overflowed(self):
        import aiosqlite
        async with aiosqlite.connect(self.config.db_path) as db:
            cur = await db.execute(
                "INSERT INTO campaigns (name, channel, budget, start_date, created_by) VALUES (?,?,?,?,?)",
                ("Test", "Instagram", 1000.0, "2026-01-01", ADMIN_ID)
            )
            await db.commit()
            campaign_id = cur.lastrowid
        await self.dp.feed_webhook_update(self.bot, _callback_update(2, ADMIN_ID, f"camp_metrics:{campaign_id}"))
        await self.dp.feed_webhook_update(self.bot, _text_update(3, ADMIN_ID, "9" * 20))
        texts = self._fake_api.sent_texts()
        self.assertIn("Введите целое число ≥ 0, например: 120", texts)
        self.assertEqual(_metric_entries(self.config.db_path), [])

    async def test_budget_with_exponent_notation_is_rejected_not_truncated(self):
        # monkey-tester: the old re.sub(r"[^\d.]", "", ...) stripped the "e"
        # out of "1e10", silently storing budget=110 instead of rejecting it.
        await self.dp.feed_webhook_update(self.bot, _text_update(2, ADMIN_ID, "➕ Новая кампания"))
        await self.dp.feed_webhook_update(self.bot, _text_update(3, ADMIN_ID, "Test"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(4, ADMIN_ID, "camp_channel:Instagram"))
        await self.dp.feed_webhook_update(self.bot, _text_update(5, ADMIN_ID, "1e10"))
        texts = self._fake_api.sent_texts()
        self.assertIn("Введите неотрицательное число, например: 15000", texts)
        self.assertEqual(_campaigns(self.config.db_path), [])
        # A valid budget afterwards must still complete the flow correctly.
        await self.dp.feed_webhook_update(self.bot, _text_update(6, ADMIN_ID, "15000"))
        await self.dp.feed_webhook_update(self.bot, _text_update(7, ADMIN_ID, "01.01.2026"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(8, ADMIN_ID, "camp_noend"))
        rows = _campaigns(self.config.db_path)
        self.assertEqual(rows[0]["budget"], 15000.0)

    async def test_budget_accepts_comma_as_decimal_separator(self):
        await self.dp.feed_webhook_update(self.bot, _text_update(2, ADMIN_ID, "➕ Новая кампания"))
        await self.dp.feed_webhook_update(self.bot, _text_update(3, ADMIN_ID, "Test"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(4, ADMIN_ID, "camp_channel:Instagram"))
        await self.dp.feed_webhook_update(self.bot, _text_update(5, ADMIN_ID, "1500,50"))
        await self.dp.feed_webhook_update(self.bot, _text_update(6, ADMIN_ID, "01.01.2026"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(7, ADMIN_ID, "camp_noend"))
        rows = _campaigns(self.config.db_path)
        self.assertEqual(rows[0]["budget"], 1500.50)

    async def test_metric_with_decimal_point_is_rejected_not_truncated(self):
        # monkey-tester: "10.5" used to strip the dot and silently store 105.
        import aiosqlite
        async with aiosqlite.connect(self.config.db_path) as db:
            cur = await db.execute(
                "INSERT INTO campaigns (name, channel, budget, start_date, created_by) VALUES (?,?,?,?,?)",
                ("Test", "Instagram", 1000.0, "2026-01-01", ADMIN_ID)
            )
            await db.commit()
            campaign_id = cur.lastrowid
        await self.dp.feed_webhook_update(self.bot, _callback_update(2, ADMIN_ID, f"camp_metrics:{campaign_id}"))
        await self.dp.feed_webhook_update(self.bot, _text_update(3, ADMIN_ID, "10.5"))
        texts = self._fake_api.sent_texts()
        self.assertIn("Введите целое число ≥ 0, например: 120", texts)
        self.assertEqual(_metric_entries(self.config.db_path), [])

    async def test_negative_metric_is_rejected_not_made_absolute(self):
        # monkey-tester: "-5" used to strip the sign and silently store 5.
        import aiosqlite
        async with aiosqlite.connect(self.config.db_path) as db:
            cur = await db.execute(
                "INSERT INTO campaigns (name, channel, budget, start_date, created_by) VALUES (?,?,?,?,?)",
                ("Test", "Instagram", 1000.0, "2026-01-01", ADMIN_ID)
            )
            await db.commit()
            campaign_id = cur.lastrowid
        await self.dp.feed_webhook_update(self.bot, _callback_update(2, ADMIN_ID, f"camp_metrics:{campaign_id}"))
        await self.dp.feed_webhook_update(self.bot, _text_update(3, ADMIN_ID, "-5"))
        texts = self._fake_api.sent_texts()
        self.assertIn("Введите целое число ≥ 0, например: 120", texts)
        self.assertEqual(_metric_entries(self.config.db_path), [])

    async def test_free_text_during_channel_selection_reprompts_instead_of_silent_drop(self):
        # state-db/monkey-tester: CampaignCreate.channel had only a callback
        # handler, so any text typed there matched nothing and vanished with
        # zero feedback. Must now reprompt with the channel keyboard.
        await self.dp.feed_webhook_update(self.bot, _text_update(2, ADMIN_ID, "➕ Новая кампания"))
        await self.dp.feed_webhook_update(self.bot, _text_update(3, ADMIN_ID, "Test"))
        await self.dp.feed_webhook_update(self.bot, _text_update(4, ADMIN_ID, "Instagram"))  # typed, not tapped
        texts = self._fake_api.sent_texts()
        self.assertIn("Пожалуйста, выберите канал кнопкой ниже:", texts)
        self.assertEqual(_campaigns(self.config.db_path), [])
        # The flow must still be recoverable via the actual button tap.
        await self.dp.feed_webhook_update(self.bot, _callback_update(5, ADMIN_ID, "camp_channel:Instagram"))
        await self.dp.feed_webhook_update(self.bot, _text_update(6, ADMIN_ID, "1000"))
        await self.dp.feed_webhook_update(self.bot, _text_update(7, ADMIN_ID, "01.01.2026"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(8, ADMIN_ID, "camp_noend"))
        self.assertEqual(len(_campaigns(self.config.db_path)), 1)

    async def test_campaign_deleted_mid_metrics_entry_does_not_orphan_a_row(self):
        # qa-stability: admin A opens "Внести метрики" for campaign X; admin B
        # deletes campaign X before A submits. Without the existence check,
        # this INSERTs an orphaned metric_entries row (no FK enforcement) and
        # still reports "Метрики сохранены" with zero indication of the
        # problem. Must now abort with a warning and no orphaned row.
        import aiosqlite
        async with aiosqlite.connect(self.config.db_path) as db:
            cur = await db.execute(
                "INSERT INTO campaigns (name, channel, budget, start_date, created_by) VALUES (?,?,?,?,?)",
                ("Test", "Instagram", 1000.0, "2026-01-01", ADMIN_ID)
            )
            await db.commit()
            campaign_id = cur.lastrowid

        await self.dp.feed_webhook_update(self.bot, _callback_update(2, ADMIN_ID, f"camp_metrics:{campaign_id}"))
        await self.dp.feed_webhook_update(self.bot, _text_update(3, ADMIN_ID, "100"))
        await self.dp.feed_webhook_update(self.bot, _text_update(4, ADMIN_ID, "10"))

        # Concurrent delete by another admin, mid-flow.
        async with aiosqlite.connect(self.config.db_path) as db:
            await db.execute("DELETE FROM campaigns WHERE id=?", (campaign_id,))
            await db.commit()

        await self.dp.feed_webhook_update(self.bot, _text_update(5, ADMIN_ID, "2"))
        texts = self._fake_api.sent_texts()
        self.assertIn("⚠️ Кампания была удалена — метрики не сохранены.", texts)
        self.assertEqual(_metric_entries(self.config.db_path), [])


class CampaignTrackerMiniAppConfigTests(unittest.IsolatedAsyncioTestCase):
    """miniapp_config's declared table/field names must match init_db()'s
    real schema — miniapp_api.py builds SQL directly off these names, so a
    drift here would 500 at request time instead of failing a test."""

    def test_miniapp_config_resource_names(self):
        names = {r["name"] for r in campaign_tracker.miniapp_config["resources"]}
        self.assertEqual(names, {"campaigns", "metrics"})

    def test_campaigns_resource_targets_campaigns_table(self):
        campaigns = next(r for r in campaign_tracker.miniapp_config["resources"] if r["name"] == "campaigns")
        self.assertEqual(campaigns["table"], "campaigns")
        self.assertTrue(campaigns["creatable"])
        self.assertIn("name", {f["name"] for f in campaigns["fields"]})

    def test_metrics_resource_targets_metric_entries_table(self):
        metrics = next(r for r in campaign_tracker.miniapp_config["resources"] if r["name"] == "metrics")
        self.assertEqual(metrics["table"], "metric_entries")
        self.assertFalse(metrics["creatable"])
        field_names = {f["name"] for f in metrics["fields"]}
        self.assertEqual(
            field_names,
            {"campaign_id", "clicks", "leads", "sales", "entered_by", "entered_at"},
        )

    async def test_miniapp_config_fields_match_real_schema(self):
        import aiosqlite
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "schema_check.db")
            await campaign_tracker.init_db(db_path)
            async with aiosqlite.connect(db_path) as db:
                for resource in campaign_tracker.miniapp_config["resources"]:
                    cur = await db.execute(f"PRAGMA table_info({resource['table']})")
                    real_columns = {row[1] for row in await cur.fetchall()}
                    declared = {f["name"] for f in resource["fields"]} | {"id"}
                    self.assertTrue(
                        declared.issubset(real_columns),
                        f"{resource['name']}: declared fields {declared} not all in "
                        f"real columns {real_columns}",
                    )


if __name__ == "__main__":
    unittest.main()
