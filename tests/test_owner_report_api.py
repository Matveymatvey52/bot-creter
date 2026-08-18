"""Tests for runtime/owner_report_api.py — the Stage 2 system-owner-only
cross-owner report (bot registry table + merged activity feed). Same style
as tests/test_factory_analytics_api.py: a fake BotEntry + registry, no real
Telegram network calls, no real bot tokens.

Covers: owner-only auth (wrong user / missing OWNER_ID / no credentials
all 403), bot registry shape (owner/template/payments/edits/feedback
aggregation), and activity feed shape + owner/bot filtering + pagination.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

import aiosqlite
from aiohttp.test_utils import TestClient, TestServer

import db.database as db_module
from db.database import (
    add_template_candidate,
    create_bot_record_with_admins,
    delete_bot,
    init_db,
)
from runtime.miniapp_api import mint_magic_link_token
from runtime.owner_report_api import register_routes
from runtime.registry import FACTORY_BOT_ID, BotEntry, Registry
from runtime.webhook_app import create_app

FAKE_TOKEN = "111222333:AAHfakeOwnerReportToken1234567890"
FACTORY_TOKEN = "444555666:AAHfactoryOwnerReportToken1234567890"
OWNER_TELEGRAM_ID = 777
CUSTOMER_TELEGRAM_ID = 888
OTHER_TELEGRAM_ID = 999


class _FakeBot:
    def __init__(self, token: str) -> None:
        self.token = token


class OwnerReportApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await init_db()

        self.bot_id = await create_bot_record_with_admins(
            name="owner_report_test_bot", description="test", token=FAKE_TOKEN,
            file_path="templates/tour_operator.py", admin_ids=["1"],
            owner_telegram_id=CUSTOMER_TELEGRAM_ID, creation_prompt="a booking bot for my cafe",
        )
        await db_module.enable_bot_feature(self.bot_id, "payments")
        await db_module.add_custom_feature_record(self.bot_id, "make button blue")
        await db_module.add_bot_feedback(self.bot_id, 4, "works well")
        await db_module.add_bot_feedback(self.bot_id, 5, None)
        await db_module.set_bot_payment_provider(self.bot_id, "provider-token")

        # from-scratch bot: no template marker, has a template_candidates row
        self.scratch_bot_id = await create_bot_record_with_admins(
            name="owner_report_scratch_bot", description="test", token="222333444:AAHscratch1234567890",
            file_path="generated_bots/scratch_owner_report.py", admin_ids=["1"],
            owner_telegram_id=OWNER_TELEGRAM_ID,
        )
        os.makedirs("generated_bots", exist_ok=True)
        with open("generated_bots/scratch_owner_report.py", "w") as f:
            f.write("# not a template file\n")
        await add_template_candidate(
            creator_user_id=OWNER_TELEGRAM_ID, summary="something niche", fallback_reason="no_template_match",
            selected_templates=[], bot_type="niche_thing", bot_id=self.scratch_bot_id,
        )

        entry = BotEntry(
            bot=_FakeBot(FACTORY_TOKEN),
            dispatcher=None,
            template_id="__factory__",
            config={"bot_id": FACTORY_BOT_ID},
        )
        tmpdir = tempfile.mkdtemp()
        self.bot_db_path = os.path.join(tmpdir, "bot.db")
        bot_entry = BotEntry(
            bot=_FakeBot(FAKE_TOKEN),
            dispatcher=None,
            template_id="tour_operator",
            config={"db_path": self.bot_db_path},
        )
        # office_notes + payments rows in this bot's own per-bot db, for the
        # activity feed / last-activity / data-volume checks.
        async with aiosqlite.connect(self.bot_db_path) as db:
            await db.execute("""
                CREATE TABLE office_notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_bot_id INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    note TEXT NOT NULL,
                    created_at TEXT DEFAULT (datetime('now','localtime'))
                )
            """)
            await db.execute(
                "INSERT INTO office_notes (source_bot_id, event_type, note) VALUES (?, ?, ?)",
                (999, "order.created", "Получено событие от связанного бота"),
            )
            await db.execute("""
                CREATE TABLE payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_payment_charge_id TEXT NOT NULL UNIQUE,
                    provider_payment_charge_id TEXT,
                    user_id INTEGER NOT NULL,
                    invoice_payload TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    total_amount INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'paid',
                    created_at TEXT DEFAULT (datetime('now','localtime'))
                )
            """)
            await db.execute(
                "INSERT INTO payments (telegram_payment_charge_id, user_id, invoice_payload, currency, total_amount) "
                "VALUES (?, ?, ?, ?, ?)",
                ("charge_1", 12345, "payload", "RUB", 5000),
            )
            await db.commit()

        registry = Registry()
        registry._entries[FACTORY_BOT_ID] = entry
        registry._entries[self.bot_id] = bot_entry
        self.app = create_app(registry)
        register_routes(self.app)

        self._owner_patcher = patch("runtime.factory_analytics_api.OWNER_ID", OWNER_TELEGRAM_ID)
        self._owner_patcher.start()

        self.server = TestServer(self.app)
        self.client = TestClient(self.server)
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self._owner_patcher.stop()
        await delete_bot(self.bot_id)
        await delete_bot(self.scratch_bot_id)
        if os.path.exists("generated_bots/scratch_owner_report.py"):
            os.remove("generated_bots/scratch_owner_report.py")

    async def _owner_qs(self) -> str:
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            token = mint_magic_link_token(FACTORY_BOT_ID, OWNER_TELEGRAM_ID)
        return f"token={token}"

    async def _other_qs(self, telegram_id: int) -> str:
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            token = mint_magic_link_token(FACTORY_BOT_ID, telegram_id)
        return f"token={token}"

    # ── auth ──────────────────────────────────────────────────────────
    async def test_no_credentials_returns_403_for_bots(self):
        resp = await self.client.get("/api/owner-report/bots")
        self.assertEqual(resp.status, 403)

    async def test_no_credentials_returns_403_for_activity(self):
        resp = await self.client.get("/api/owner-report/activity")
        self.assertEqual(resp.status, 403)

    async def test_non_owner_customer_gets_403(self):
        """Unlike factory_analytics_api's shared dashboard, there is no
        customer-facing variant of this report — any non-OWNER_ID user is
        rejected outright."""
        qs = await self._other_qs(CUSTOMER_TELEGRAM_ID)
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.get(f"/api/owner-report/bots?{qs}")
        self.assertEqual(resp.status, 403)

    async def test_owner_id_unset_denies_access(self):
        with patch("runtime.factory_analytics_api.OWNER_ID", 0):
            qs = await self._owner_qs()
            with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
                resp = await self.client.get(f"/api/owner-report/bots?{qs}")
        self.assertEqual(resp.status, 403)

    async def test_owner_can_access_bots(self):
        qs = await self._owner_qs()
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.get(f"/api/owner-report/bots?{qs}")
        self.assertEqual(resp.status, 200)

    # ── list_bots_handler ─────────────────────────────────────────────
    async def test_bot_registry_shape_and_aggregation(self):
        qs = await self._owner_qs()
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.get(f"/api/owner-report/bots?{qs}")
        body = await resp.json()
        item = next(i for i in body["items"] if i["id"] == self.bot_id)
        self.assertEqual(item["template"], "tour_operator")
        self.assertEqual(item["owner_telegram_id"], CUSTOMER_TELEGRAM_ID)
        self.assertEqual(item["owner_display"], str(CUSTOMER_TELEGRAM_ID))
        self.assertEqual(item["creation_prompt"], "a booking bot for my cafe")
        self.assertEqual(item["edits_count"], 1)
        self.assertEqual(item["feedback_count"], 2)
        self.assertAlmostEqual(item["avg_rating"], 4.5)
        self.assertTrue(item["payments_connected"])
        self.assertIsNotNone(item["last_activity_at"])
        self.assertIsNotNone(item["approx_data_volume_bytes"])
        self.assertNotIn("token", item)

    async def test_bot_registry_from_scratch_bot_shows_bot_type(self):
        qs = await self._owner_qs()
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.get(f"/api/owner-report/bots?{qs}")
        body = await resp.json()
        item = next(i for i in body["items"] if i["id"] == self.scratch_bot_id)
        self.assertIn("from-scratch", item["template"])
        self.assertIn("niche_thing", item["template"])
        self.assertFalse(item["payments_connected"])

    async def test_bot_registry_no_live_registry_entry_degrades_gracefully(self):
        """scratch_bot_id has no BotEntry in the fake registry (not
        currently running) — last_activity_at should fall back to the
        central-DB value (None here, no feedback/edits) and data volume
        should be None, not an error."""
        qs = await self._owner_qs()
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.get(f"/api/owner-report/bots?{qs}")
        body = await resp.json()
        item = next(i for i in body["items"] if i["id"] == self.scratch_bot_id)
        self.assertIsNone(item["approx_data_volume_bytes"])

    # ── list_activity_handler ────────────────────────────────────────
    async def test_activity_feed_merges_all_sources(self):
        qs = await self._owner_qs()
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.get(f"/api/owner-report/activity?{qs}")
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        sources = {item["source"] for item in body["items"]}
        self.assertIn("feedback", sources)
        self.assertIn("office_event", sources)
        self.assertIn("payment", sources)
        payment_entry = next(i for i in body["items"] if i["source"] == "payment")
        self.assertEqual(payment_entry["telegram_user_id"], 12345)
        self.assertEqual(payment_entry["bot_id"], self.bot_id)

    async def test_activity_feed_filters_by_bot_id(self):
        qs = await self._owner_qs()
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.get(f"/api/owner-report/activity?{qs}&bot_id={self.scratch_bot_id}")
        body = await resp.json()
        self.assertTrue(all(item["bot_id"] == self.scratch_bot_id for item in body["items"]))

    async def test_activity_feed_filters_by_owner_id(self):
        qs = await self._owner_qs()
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.get(f"/api/owner-report/activity?{qs}&owner_id={CUSTOMER_TELEGRAM_ID}")
        body = await resp.json()
        self.assertTrue(all(item["owner_telegram_id"] == CUSTOMER_TELEGRAM_ID for item in body["items"]))
        self.assertTrue(any(item["source"] == "feedback" for item in body["items"]))

    async def test_activity_feed_pagination(self):
        qs = await self._owner_qs()
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.get(f"/api/owner-report/activity?{qs}&limit=1&offset=0")
        body = await resp.json()
        self.assertEqual(len(body["items"]), 1)
        self.assertEqual(body["limit"], 1)
        self.assertGreaterEqual(body["total"], 3)

    # ── list_template_candidates_handler / list_template_candidate_clusters_handler ──
    # Moved here from tests/test_factory_analytics_api.py (owner instruction,
    # 2026-08-18): these routes moved from /api/factory/... to
    # /api/owner-report/... alongside the frontend sections that consume them.
    async def test_candidates_non_owner_returns_403(self):
        qs = await self._other_qs(CUSTOMER_TELEGRAM_ID)
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.get(f"/api/owner-report/candidates?{qs}")
        self.assertEqual(resp.status, 403)

    async def test_owner_sees_template_candidates(self):
        await add_template_candidate(
            creator_user_id=OWNER_TELEGRAM_ID,
            summary="хочу бота для учёта смен курьеров",
            fallback_reason="no_template_match",
            selected_templates=[],
            bot_type="general",
        )
        await add_template_candidate(
            creator_user_id=OWNER_TELEGRAM_ID,
            summary="магазин с бонусной программой",
            fallback_reason="synthesis_failed",
            selected_templates=["shop_catalog", "referral_program"],
            bot_type="ecommerce",
            bot_id=self.bot_id,
        )

        qs = await self._owner_qs()
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.get(f"/api/owner-report/candidates?{qs}")
        self.assertEqual(resp.status, 200)
        body = await resp.json()

        item = next(i for i in body["items"] if i["summary"] == "магазин с бонусной программой")
        self.assertEqual(item["bot_id"], self.bot_id)
        self.assertEqual(item["fallback_reason"], "synthesis_failed")
        self.assertEqual(item["selected_templates"], ["shop_catalog", "referral_program"])
        self.assertEqual(item["bot_type"], "ecommerce")

        no_match_item = next(i for i in body["items"] if i["summary"] == "хочу бота для учёта смен курьеров")
        self.assertIsNone(no_match_item["bot_id"])
        self.assertEqual(no_match_item["selected_templates"], [])

    async def test_candidate_clusters_non_owner_returns_403(self):
        qs = await self._other_qs(CUSTOMER_TELEGRAM_ID)
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.get(f"/api/owner-report/candidate-clusters?{qs}")
        self.assertEqual(resp.status, 403)

    async def test_owner_sees_template_candidate_clusters(self):
        qs = await self._owner_qs()
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.get(f"/api/owner-report/candidate-clusters?{qs}")
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertIsInstance(body["items"], list)
