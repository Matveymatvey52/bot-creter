"""Tests for runtime/factory_analytics_api.py — the owner-only factory-wide
analytics dashboard. Same style as tests/test_miniapp_api.py: a fake
BotEntry + registry, no real Telegram network calls, no real bot tokens.

Covers: owner-only auth (wrong user / missing OWNER_ID / no credentials),
list_bots_handler's shape (features/edits_count/avg_rating aggregation via
db.database.list_bots_with_stats), and add_feedback_handler's validation.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import aiosqlite
from aiohttp.test_utils import TestClient, TestServer

import db.database as db_module
from db.database import (
    add_template_candidate,
    create_bot_record_with_admins,
    delete_bot,
    init_db,
    set_bot_office_hook_config,
)
from runtime.factory_analytics_api import register_routes
from runtime.miniapp_api import mint_magic_link_token
from runtime.registry import FACTORY_BOT_ID, BotEntry
from runtime.webhook_app import create_app

FAKE_TOKEN = "123456789:AAHfakeTokenButShapedRight1234567890"
FACTORY_TOKEN = "987654321:AAHfactoryOwnTokenAlsoShapedRight123456"
OWNER_TELEGRAM_ID = 555
OTHER_TELEGRAM_ID = 999


class _FakeBot:
    def __init__(self, token: str) -> None:
        self.token = token


class FactoryAnalyticsApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await init_db()
        self.bot_id = await create_bot_record_with_admins(
            name="factory_analytics_test_bot", description="test", token=FAKE_TOKEN,
            file_path="templates/tour_operator.py", admin_ids=["1"],
        )
        await db_module.enable_bot_feature(self.bot_id, "sheets")
        await db_module.enable_bot_feature(self.bot_id, "payments")
        await db_module.add_custom_feature_record(self.bot_id, "make button blue")
        await db_module.add_bot_feedback(self.bot_id, 4, "works well")
        await db_module.add_bot_feedback(self.bot_id, 5, None)

        entry = BotEntry(
            bot=_FakeBot(FACTORY_TOKEN),
            dispatcher=None,
            template_id="__factory__",
            config={"bot_id": FACTORY_BOT_ID},
        )
        # A second registry entry for self.bot_id (its own real db_path, a
        # temp sqlite file) — needed so list_bots_handler's weekly_count
        # aggregation (runtime/factory_analytics_api.py's
        # _weekly_count_for_bot) has a live Registry entry + db_path to read,
        # same as the real webhook process would have for a running bot.
        tmpdir = tempfile.mkdtemp()
        self.bot_db_path = os.path.join(tmpdir, "bot.db")
        bot_entry = BotEntry(
            bot=_FakeBot(FAKE_TOKEN),
            dispatcher=None,
            template_id="tour_operator",
            config={"db_path": self.bot_db_path},
        )
        # A second, shop_catalog-templated bot — sheets/voice_intake are NOT
        # in tour_operator's own COMPATIBLE_WITH list (see features/sheets.py,
        # features/voice_intake.py), so the Variant D configure-dialog tests
        # below need a template that actually offers sheets.
        self.shop_bot_id = await create_bot_record_with_admins(
            name="factory_analytics_shop_bot", description="test", token="3333:AAshop-fake-token-1234567890",
            file_path="templates/shop_catalog.py", admin_ids=["1"],
        )

        registry = {FACTORY_BOT_ID: entry, self.bot_id: bot_entry}
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
        await delete_bot(self.shop_bot_id)

    async def _owner_qs(self) -> str:
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            token = mint_magic_link_token(FACTORY_BOT_ID, OWNER_TELEGRAM_ID)
        return f"token={token}"

    # ── auth ──────────────────────────────────────────────────────────
    async def test_non_owner_token_returns_403(self):
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            token = mint_magic_link_token(FACTORY_BOT_ID, OTHER_TELEGRAM_ID)
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.get(f"/api/factory/bots?token={token}")
        self.assertEqual(resp.status, 403)

    async def test_no_credentials_returns_403(self):
        resp = await self.client.get("/api/factory/bots")
        self.assertEqual(resp.status, 403)

    async def test_owner_id_unset_fails_closed(self):
        with patch("runtime.factory_analytics_api.OWNER_ID", 0):
            qs = await self._owner_qs()
            with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
                resp = await self.client.get(f"/api/factory/bots?{qs}")
        self.assertEqual(resp.status, 403)

    # ── list_bots_handler ─────────────────────────────────────────────
    async def test_owner_sees_bot_list_with_aggregated_stats(self):
        qs = await self._owner_qs()
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.get(f"/api/factory/bots?{qs}")
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        item = next(i for i in body["items"] if i["id"] == self.bot_id)
        self.assertEqual(item["template"], "tour_operator")
        self.assertEqual(sorted(item["features"]), ["payments", "sheets"])
        self.assertEqual(item["edits_count"], 1)
        self.assertEqual(item["feedback_count"], 2)
        self.assertAlmostEqual(item["avg_rating"], 4.5)
        self.assertNotIn("token", item)

    async def test_bot_list_includes_weekly_count_from_office_hook_config(self):
        async with aiosqlite.connect(self.bot_db_path) as db:
            await db.execute(
                "CREATE TABLE bookings (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT)"
            )
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            old = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
            await db.execute("INSERT INTO bookings (created_at) VALUES (?)", (now,))
            await db.execute("INSERT INTO bookings (created_at) VALUES (?)", (now,))
            await db.execute("INSERT INTO bookings (created_at) VALUES (?)", (old,))
            await db.commit()
        await set_bot_office_hook_config(
            self.bot_id, {"table": "bookings", "match_field": None, "created_at_field": "created_at"}
        )

        qs = await self._owner_qs()
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.get(f"/api/factory/bots?{qs}")
        body = await resp.json()
        item = next(i for i in body["items"] if i["id"] == self.bot_id)
        self.assertEqual(item["weekly_count"], 2)  # the 30-days-ago row excluded

    async def test_bot_list_weekly_count_is_none_without_office_hook_config(self):
        qs = await self._owner_qs()
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.get(f"/api/factory/bots?{qs}")
        body = await resp.json()
        item = next(i for i in body["items"] if i["id"] == self.bot_id)
        self.assertIsNone(item["weekly_count"])

    # ── add_feedback_handler ─────────────────────────────────────────
    async def test_add_feedback_valid_rating_persists(self):
        qs = await self._owner_qs()
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.post(
                f"/api/factory/bots/{self.bot_id}/feedback?{qs}", json={"rating": 3, "comment": "meh"}
            )
        self.assertEqual(resp.status, 201)
        history = await db_module.get_bot_feedback(self.bot_id)
        self.assertEqual(len(history), 3)

    async def test_add_feedback_invalid_rating_returns_400(self):
        qs = await self._owner_qs()
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.post(
                f"/api/factory/bots/{self.bot_id}/feedback?{qs}", json={"rating": 7}
            )
        self.assertEqual(resp.status, 400)

    async def test_add_feedback_non_owner_returns_403(self):
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            token = mint_magic_link_token(FACTORY_BOT_ID, OTHER_TELEGRAM_ID)
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.post(
                f"/api/factory/bots/{self.bot_id}/feedback?token={token}", json={"rating": 5}
            )
        self.assertEqual(resp.status, 403)

    async def test_add_feedback_bad_bot_id_returns_404(self):
        qs = await self._owner_qs()
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.post(f"/api/factory/bots/not-a-number/feedback?{qs}", json={"rating": 5})
        self.assertEqual(resp.status, 404)

    # ── list_template_candidates_handler ─────────────────────────────
    async def test_candidates_non_owner_returns_403(self):
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            token = mint_magic_link_token(FACTORY_BOT_ID, OTHER_TELEGRAM_ID)
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.get(f"/api/factory/candidates?token={token}")
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
            resp = await self.client.get(f"/api/factory/candidates?{qs}")
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

    # ── bot_detail_handler ────────────────────────────────────────────
    async def test_bot_detail_returns_admins_offices_and_features(self):
        qs = await self._owner_qs()
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.get(f"/api/factory/bots/{self.bot_id}?{qs}")
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertEqual(body["id"], self.bot_id)
        self.assertEqual(body["admins"], ["1"])
        self.assertEqual(body["offices"], [])
        names = {f["name"] for f in body["features"]}
        # self.bot_id is a tour_operator bot — sheets/payments are NOT in
        # tour_operator's own COMPATIBLE_WITH lists (see features/sheets.py,
        # features/payments.py) even though asyncSetUp enables them in the
        # DB — _feature_status_items filters by template compatibility, so
        # neither should surface here. cashflow_ledger IS compatible with
        # tour_operator (see features/cashflow_ledger.py) but was never
        # enabled — it should show up in the list with state "off".
        self.assertNotIn("sheets", names)
        self.assertNotIn("payments", names)
        self.assertIn("cashflow_ledger", names)
        ledger_item = next(f for f in body["features"] if f["name"] == "cashflow_ledger")
        self.assertEqual(ledger_item["state"], "off")
        self.assertFalse(ledger_item["no_free_text"])

    async def test_bot_detail_unknown_bot_returns_404(self):
        qs = await self._owner_qs()
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.get(f"/api/factory/bots/999999?{qs}")
        self.assertEqual(resp.status, 404)

    async def test_bot_detail_non_owner_returns_403(self):
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            token = mint_magic_link_token(FACTORY_BOT_ID, OTHER_TELEGRAM_ID)
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.get(f"/api/factory/bots/{self.bot_id}?token={token}")
        self.assertEqual(resp.status, 403)

    # ── admins ────────────────────────────────────────────────────────
    async def test_add_and_remove_admin(self):
        qs = await self._owner_qs()
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.post(
                f"/api/factory/bots/{self.bot_id}/admins?{qs}", json={"telegram_id": "424242"}
            )
        self.assertEqual(resp.status, 201)
        admins = await db_module.get_bot_admins(self.bot_id)
        self.assertIn("424242", admins)

        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.delete(f"/api/factory/bots/{self.bot_id}/admins/424242?{qs}")
        self.assertEqual(resp.status, 200)
        admins = await db_module.get_bot_admins(self.bot_id)
        self.assertNotIn("424242", admins)

    async def test_add_admin_rejects_non_numeric_id(self):
        qs = await self._owner_qs()
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.post(
                f"/api/factory/bots/{self.bot_id}/admins?{qs}", json={"telegram_id": "not-a-number"}
            )
        self.assertEqual(resp.status, 400)

    # ── offices ───────────────────────────────────────────────────────
    async def test_add_and_remove_office_link(self):
        # self.bot_id is a tour_operator bot — tour_operator publishes NO
        # event type (not in features/payments.py's own COMPATIBLE_WITH list,
        # and not boss_bot — see features/office_events.py's
        # _EVENT_TYPE_PUBLISHER_TEMPLATES), so this link uses self.shop_bot_id
        # (shop_catalog, which IS compatible with payments) as the source.
        other_bot_id = await create_bot_record_with_admins(
            name="factory_analytics_test_other_bot", description="test", token="1111:AAother-fake-token-1234567",
            file_path="templates/shop_catalog.py", admin_ids=["1"],
        )
        try:
            qs = await self._owner_qs()
            with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
                resp = await self.client.post(
                    f"/api/factory/bots/{self.shop_bot_id}/offices?{qs}",
                    json={"target_bot_id": other_bot_id, "event_type": "order.created"},
                )
            self.assertEqual(resp.status, 201)
            links = await db_module.get_office_links_for_bot(self.shop_bot_id)
            self.assertEqual(len(links), 1)
            self.assertEqual(links[0]["event_type"], "order.created")

            with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
                resp = await self.client.delete(
                    f"/api/factory/bots/{self.shop_bot_id}/offices/{other_bot_id}"
                    f"?event_type=order.created&{qs}"
                )
            self.assertEqual(resp.status, 200)
            links = await db_module.get_office_links_for_bot(self.shop_bot_id)
            self.assertEqual(links, [])
        finally:
            await delete_bot(other_bot_id)

    async def test_add_office_link_rejects_self_link(self):
        qs = await self._owner_qs()
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.post(
                f"/api/factory/bots/{self.bot_id}/offices?{qs}", json={"target_bot_id": self.bot_id}
            )
        self.assertEqual(resp.status, 400)

    async def test_add_office_link_rejects_event_type_not_available_for_source_template(self):
        # self.bot_id is tour_operator — publishes NO event type (not
        # payments-compatible, not boss_bot). Must be rejected even though
        # order.created is a real, registered event type in general.
        other_bot_id = await create_bot_record_with_admins(
            name="factory_analytics_reject_target_bot", description="test",
            token="4444:AAreject-fake-token-1234567890", file_path="templates/shop_catalog.py", admin_ids=["1"],
        )
        try:
            qs = await self._owner_qs()
            with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
                resp = await self.client.post(
                    f"/api/factory/bots/{self.bot_id}/offices?{qs}",
                    json={"target_bot_id": other_bot_id, "event_type": "order.created"},
                )
            self.assertEqual(resp.status, 400)
        finally:
            await delete_bot(other_bot_id)

    # ── list_office_event_types_handler ────────────────────────────────
    async def test_office_event_types_for_shop_catalog_bot(self):
        qs = await self._owner_qs()
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.get(f"/api/factory/bots/{self.shop_bot_id}/offices/event-types?{qs}")
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertEqual(body["items"], [{"event_type": "order.created", "label": "Новый заказ"}])

    async def test_office_event_types_empty_for_tour_operator_bot(self):
        qs = await self._owner_qs()
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.get(f"/api/factory/bots/{self.bot_id}/offices/event-types?{qs}")
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertEqual(body["items"], [])

    async def test_office_event_types_non_owner_returns_403(self):
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            token = mint_magic_link_token(FACTORY_BOT_ID, OTHER_TELEGRAM_ID)
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.get(f"/api/factory/bots/{self.shop_bot_id}/offices/event-types?token={token}")
        self.assertEqual(resp.status, 403)

    # ── showcase_group_status_handler ────────────────────────────────
    async def test_showcase_group_status_disconnected_by_default(self):
        qs = await self._owner_qs()
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.get(f"/api/factory/showcase-group?{qs}")
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertFalse(body["connected"])

    async def test_showcase_group_status_connected_after_set(self):
        await db_module.set_factory_showcase_group(-100987654321)
        try:
            qs = await self._owner_qs()
            with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
                resp = await self.client.get(f"/api/factory/showcase-group?{qs}")
            self.assertEqual(resp.status, 200)
            body = await resp.json()
            self.assertTrue(body["connected"])
        finally:
            await db_module.clear_factory_showcase_group()

    async def test_showcase_group_status_non_owner_returns_403(self):
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            token = mint_magic_link_token(FACTORY_BOT_ID, OTHER_TELEGRAM_ID)
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.get(f"/api/factory/showcase-group?token={token}")
        self.assertEqual(resp.status, 403)

    # ── features: disable is instant, no dialog ──────────────────────
    async def test_disable_feature_is_instant_and_clears_config(self):
        await db_module.set_bot_feature_description(self.bot_id, "sheets", "Записываю имена клиентов")
        qs = await self._owner_qs()
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.post(f"/api/factory/bots/{self.bot_id}/features/sheets/disable?{qs}")
        self.assertEqual(resp.status, 200)
        enabled = await db_module.get_bot_features(self.bot_id)
        self.assertNotIn("sheets", enabled)
        cfg = await db_module.get_bot_feature_config(self.bot_id, "sheets")
        self.assertIsNone(cfg)

    # ── features: Variant D configure dialog ──────────────────────────
    async def test_configure_feature_accepted_enables_and_saves_description(self):
        qs = await self._owner_qs()
        with patch(
            "runtime.factory_analytics_api.assess_feature_description",
            AsyncMock(
                return_value={
                    "accepted": True,
                    "reply": "Записываю: имя клиента, дату записи",
                    "config_summary": "Записываю: имя клиента, дату записи",
                }
            ),
        ):
            with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
                resp = await self.client.post(
                    f"/api/factory/bots/{self.shop_bot_id}/features/sheets/configure?{qs}",
                    json={"message": "имя клиента и дата записи"},
                )
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertEqual(body["status"], "enabled")
        enabled = await db_module.get_bot_features(self.shop_bot_id)
        self.assertIn("sheets", enabled)
        cfg = await db_module.get_bot_feature_config(self.shop_bot_id, "sheets")
        self.assertEqual(cfg["description"], "Записываю: имя клиента, дату записи")
        self.assertEqual(cfg["thread"], [])

    async def test_configure_feature_needs_clarification_does_not_enable(self):
        qs = await self._owner_qs()
        with patch(
            "runtime.factory_analytics_api.assess_feature_description",
            AsyncMock(
                return_value={
                    "accepted": False,
                    "reply": "Уточни, что именно записывать?",
                    "config_summary": None,
                }
            ),
        ):
            with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
                resp = await self.client.post(
                    f"/api/factory/bots/{self.shop_bot_id}/features/sheets/configure?{qs}",
                    json={"message": "что-нибудь полезное"},
                )
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertEqual(body["status"], "needs_clarification")
        self.assertEqual(body["reply"], "Уточни, что именно записывать?")
        enabled = await db_module.get_bot_features(self.shop_bot_id)
        self.assertNotIn("sheets", enabled)
        cfg = await db_module.get_bot_feature_config(self.shop_bot_id, "sheets")
        self.assertIsNone(cfg["description"])
        self.assertEqual(len(cfg["thread"]), 2)

    async def test_configure_feature_rejects_payments_and_office_events(self):
        qs = await self._owner_qs()
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.post(
                f"/api/factory/bots/{self.bot_id}/features/payments/configure?{qs}",
                json={"message": "что угодно"},
            )
        self.assertEqual(resp.status, 400)

    async def test_configure_feature_incompatible_template_returns_400(self):
        # sheets is NOT in tour_operator's own COMPATIBLE_WITH list (see
        # features/sheets.py) — self.bot_id is a tour_operator bot.
        qs = await self._owner_qs()
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.post(
                f"/api/factory/bots/{self.bot_id}/features/sheets/configure?{qs}",
                json={"message": "что угодно"},
            )
        self.assertEqual(resp.status, 400)

    async def test_cancel_feature_configure_clears_config(self):
        await db_module.set_bot_feature_thread(self.bot_id, "reminders", [{"role": "owner", "text": "hi"}])
        qs = await self._owner_qs()
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.post(f"/api/factory/bots/{self.bot_id}/features/reminders/cancel?{qs}")
        self.assertEqual(resp.status, 200)
        cfg = await db_module.get_bot_feature_config(self.bot_id, "reminders")
        self.assertIsNone(cfg)

    # ── lifecycle: stop/logs (no subprocess needed — bot never started) ─
    async def test_stop_bot_updates_status(self):
        qs = await self._owner_qs()
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.post(f"/api/factory/bots/{self.bot_id}/stop?{qs}")
        self.assertEqual(resp.status, 200)
        bot = await db_module.get_bot(self.bot_id)
        self.assertEqual(bot["status"], "stopped")

    async def test_bot_logs_returns_empty_string_when_never_started(self):
        qs = await self._owner_qs()
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.get(f"/api/factory/bots/{self.bot_id}/logs?{qs}")
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertEqual(body["logs"], "")

    async def test_delete_bot_removes_record(self):
        deletable_id = await create_bot_record_with_admins(
            name="factory_analytics_deletable_bot", description="test", token="2222:AAdeletable-fake-tok-123456",
            file_path="templates/shop_catalog.py", admin_ids=["1"],
        )
        qs = await self._owner_qs()
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.delete(f"/api/factory/bots/{deletable_id}?{qs}")
        self.assertEqual(resp.status, 200)
        self.assertIsNone(await db_module.get_bot(deletable_id))
