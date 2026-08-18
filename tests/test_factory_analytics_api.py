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
    async def test_non_owner_token_returns_client_view(self):
        """Any authenticated Telegram user may hit /api/factory/bots now —
        the shared dashboard's customer view (see decision: role-based
        rendering, not owner-gated). They just get is_owner=False and only
        their own bots, not a 403."""
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            token = mint_magic_link_token(FACTORY_BOT_ID, OTHER_TELEGRAM_ID)
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.get(f"/api/factory/bots?token={token}")
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertFalse(body["is_owner"])

    async def test_no_credentials_returns_403(self):
        resp = await self.client.get("/api/factory/bots")
        self.assertEqual(resp.status, 403)

    async def test_owner_id_unset_denies_owner_view(self):
        """With OWNER_ID unset (0), the real owner's credential no longer
        matches OWNER_ID, so they fall back to the customer view (is_owner
        False, only their own bots) rather than a hard 403 — the customer
        path itself never depended on OWNER_ID being configured."""
        with patch("runtime.factory_analytics_api.OWNER_ID", 0):
            qs = await self._owner_qs()
            with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
                resp = await self.client.get(f"/api/factory/bots?{qs}")
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertFalse(body["is_owner"])

    # ── refresh_session_handler ───────────────────────────────────────
    async def test_session_refresh_returns_new_valid_token(self):
        qs = await self._owner_qs()
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.get(f"/api/factory/session?{qs}")
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        new_token = body["token"]
        self.assertTrue(new_token)
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp2 = await self.client.get(f"/api/factory/bots?token={new_token}")
        self.assertEqual(resp2.status, 200)

    async def test_session_refresh_non_owner_returns_client_token(self):
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            token = mint_magic_link_token(FACTORY_BOT_ID, OTHER_TELEGRAM_ID)
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.get(f"/api/factory/session?token={token}")
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertFalse(body["is_owner"])

    async def test_session_refresh_expired_token_returns_403(self):
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            with patch("runtime.miniapp_api.MAGIC_LINK_TTL_SECONDS", -1):
                token = mint_magic_link_token(FACTORY_BOT_ID, OWNER_TELEGRAM_ID)
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.get(f"/api/factory/session?token={token}")
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

    async def test_bot_detail_tenant_owner_cannot_access_other_tenants_bot(self):
        """Owner A (a tenant, not the factory OWNER_ID) must not be able to
        read owner B's bot via bot_id — per-bot ownership, not just
        owner-vs-non-owner. Both bots belong to real tenants (owner_telegram_id
        set), neither is OWNER_TELEGRAM_ID/OTHER_TELEGRAM_ID's own bot."""
        THIRD_TELEGRAM_ID = 777
        bot_a_id = await create_bot_record_with_admins(
            name="factory_analytics_tenant_a_bot", description="test",
            token="6666:AAtenantA-fake-token-1234567890",
            file_path="templates/shop_catalog.py", admin_ids=["1"],
            owner_telegram_id=OTHER_TELEGRAM_ID,
        )
        bot_b_id = await create_bot_record_with_admins(
            name="factory_analytics_tenant_b_bot", description="test",
            token="7777:AAtenantB-fake-token-1234567890",
            file_path="templates/shop_catalog.py", admin_ids=["1"],
            owner_telegram_id=THIRD_TELEGRAM_ID,
        )
        try:
            with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
                token_a = mint_magic_link_token(FACTORY_BOT_ID, OTHER_TELEGRAM_ID)
            with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
                resp = await self.client.get(f"/api/factory/bots/{bot_b_id}?token={token_a}")
            self.assertEqual(resp.status, 403)

            # sanity: owner A CAN access their own bot A with the same token
            with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
                resp_own = await self.client.get(f"/api/factory/bots/{bot_a_id}?token={token_a}")
            self.assertEqual(resp_own.status, 200)
        finally:
            await delete_bot(bot_a_id)
            await delete_bot(bot_b_id)

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
        self.assertEqual(body["items"], [{"event_type": "order.created", "label": "новый заказ"}])

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
        await db_module.set_office_digest_group(OWNER_TELEGRAM_ID, "-100987654321")
        try:
            qs = await self._owner_qs()
            with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
                resp = await self.client.get(f"/api/factory/showcase-group?{qs}")
            self.assertEqual(resp.status, 200)
            body = await resp.json()
            self.assertTrue(body["connected"])
        finally:
            # No clear_office_digest_group() exists (see db/database.py) —
            # this table's own contract is "bind/rebind", never "unbind", so
            # cleanup goes through the raw connection directly rather than
            # inventing a delete function this feature has no real use for.
            async with aiosqlite.connect(db_module.DB_PATH) as db:
                await db.execute(
                    "DELETE FROM office_digest_group WHERE owner_telegram_id = ?", (OWNER_TELEGRAM_ID,)
                )
                await db.commit()

    async def test_showcase_group_status_non_owner_returns_403(self):
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            token = mint_magic_link_token(FACTORY_BOT_ID, OTHER_TELEGRAM_ID)
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.get(f"/api/factory/showcase-group?token={token}")
        self.assertEqual(resp.status, 403)

    # ── owner_registry_handler ────────────────────────────────────────
    async def test_owner_registry_shows_owner_per_bot(self):
        await db_module.create_bot_record_with_admins(
            name="factory_analytics_owned_bot", description="test", token="4444:AAowned-fake-token-1234567890",
            file_path="templates/shop_catalog.py", admin_ids=["1"], owner_telegram_id=OTHER_TELEGRAM_ID,
        )
        qs = await self._owner_qs()
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.get(f"/api/factory/owner-registry?{qs}")
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        by_name = {item["name"]: item for item in body["items"]}
        self.assertIn("factory_analytics_owned_bot", by_name)
        item = by_name["factory_analytics_owned_bot"]
        self.assertEqual(item["owner_telegram_id"], OTHER_TELEGRAM_ID)
        # Upgraded payload (owner product-analytics design) — same richer
        # per-bot fields as list_bots_handler.
        for key in ("features", "edits_count", "avg_rating", "feedback_count", "archived_at", "weekly_count"):
            self.assertIn(key, item)
        self.assertEqual(item["features"], [])
        self.assertEqual(item["edits_count"], 0)

    async def test_owner_registry_non_owner_returns_403(self):
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            token = mint_magic_link_token(FACTORY_BOT_ID, OTHER_TELEGRAM_ID)
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.get(f"/api/factory/owner-registry?token={token}")
        self.assertEqual(resp.status, 403)

    async def test_owner_registry_no_credentials_returns_403(self):
        resp = await self.client.get("/api/factory/owner-registry")
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

    async def test_bot_logs_owning_customer_can_view_own_bot(self):
        customer_bot_id = await db_module.create_bot_record_with_admins(
            name="factory_analytics_logs_customer_bot", description="test",
            token="5555:AAlogs-fake-token-1234567890",
            file_path="templates/shop_catalog.py", admin_ids=["1"], owner_telegram_id=OTHER_TELEGRAM_ID,
        )
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            token = mint_magic_link_token(FACTORY_BOT_ID, OTHER_TELEGRAM_ID)
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.get(f"/api/factory/bots/{customer_bot_id}/logs?token={token}")
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertEqual(body["logs"], "")

    async def test_bot_logs_non_owner_returns_403(self):
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            token = mint_magic_link_token(FACTORY_BOT_ID, OTHER_TELEGRAM_ID)
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.get(f"/api/factory/bots/{self.bot_id}/logs?token={token}")
        self.assertEqual(resp.status, 403)

    # ── creation_prompt in bot detail (item 2) ──────────────────────────
    async def test_bot_detail_includes_creation_prompt(self):
        prompt_bot_id = await create_bot_record_with_admins(
            name="factory_analytics_prompt_bot", description="test",
            token="4444:AAprompt-fake-token-1234567890",
            file_path="templates/shop_catalog.py", admin_ids=["1"],
            creation_prompt="хочу бота для продажи цветов",
        )
        qs = await self._owner_qs()
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.get(f"/api/factory/bots/{prompt_bot_id}?{qs}")
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertEqual(body["creation_prompt"], "хочу бота для продажи цветов")

    async def test_bot_detail_creation_prompt_is_none_when_absent(self):
        qs = await self._owner_qs()
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.get(f"/api/factory/bots/{self.bot_id}?{qs}")
        body = await resp.json()
        self.assertIsNone(body["creation_prompt"])

    # ── template candidates / clusters (item 1) ─────────────────────────
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
        qs = await self._owner_qs()
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.get(f"/api/factory/candidates?{qs}")
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertTrue(any(c["summary"] == "хочу бота для учёта смен курьеров" for c in body["items"]))

    async def test_candidate_clusters_non_owner_returns_403(self):
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            token = mint_magic_link_token(FACTORY_BOT_ID, OTHER_TELEGRAM_ID)
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.get(f"/api/factory/candidate-clusters?token={token}")
        self.assertEqual(resp.status, 403)

    async def test_owner_sees_candidate_clusters(self):
        """Uses the shared dev DB (may already have clusters from other
        tests/passes) — just asserts the owner-only route returns a
        well-shaped list, same posture as list_template_candidate_clusters_
        with_stats' own docstring (largest cluster first)."""
        qs = await self._owner_qs()
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.get(f"/api/factory/candidate-clusters?{qs}")
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertIsInstance(body["items"], list)

    # ── bot activity log (item 3) ───────────────────────────────────────
    async def test_bot_activity_merges_feedback_and_per_bot_db_sources(self):
        """Activity merges bot_feedback (central DB, seeded by asyncSetUp)
        with office_notes/payments rows written straight to self.bot_db_path
        — no new table involved, and a bot whose db lacks either table just
        contributes nothing from that source (see the two OperationalError
        try/excepts in bot_activity_handler)."""
        async with aiosqlite.connect(self.bot_db_path) as db:
            await db.execute(
                "CREATE TABLE office_notes (id INTEGER PRIMARY KEY, source_bot_id INTEGER, "
                "event_type TEXT, note TEXT, created_at TEXT)"
            )
            await db.execute(
                "INSERT INTO office_notes (source_bot_id, event_type, note, created_at) "
                "VALUES (1, 'order.created', 'test note', '2099-01-01 00:00:00')"
            )
            await db.execute(
                "CREATE TABLE payments (id INTEGER PRIMARY KEY, telegram_payment_charge_id TEXT, "
                "user_id INTEGER, invoice_payload TEXT, currency TEXT, total_amount INTEGER, "
                "status TEXT, created_at TEXT)"
            )
            await db.execute(
                "INSERT INTO payments (telegram_payment_charge_id, user_id, invoice_payload, "
                "currency, total_amount, status, created_at) "
                "VALUES ('ch1', 1, 'p', 'RUB', 10000, 'paid', '2099-01-02 00:00:00')"
            )
            await db.commit()

        qs = await self._owner_qs()
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.get(f"/api/factory/bots/{self.bot_id}/activity?{qs}")
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        sources = {item["source"] for item in body["items"]}
        self.assertEqual(sources, {"feedback", "office_event", "payment"})
        # most-recent-first: the 2099 payment sorts ahead of feedback/office_note
        self.assertEqual(body["items"][0]["source"], "payment")

    async def test_bot_activity_missing_per_bot_tables_degrades_to_feedback_only(self):
        """self.bot_db_path exists but has neither office_notes nor payments
        tables — both OperationalError branches should be swallowed, not
        turn into a 500, leaving just the central bot_feedback rows."""
        qs = await self._owner_qs()
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.get(f"/api/factory/bots/{self.bot_id}/activity?{qs}")
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertTrue(all(item["source"] == "feedback" for item in body["items"]))
        self.assertEqual(len(body["items"]), 2)  # the two add_bot_feedback calls in asyncSetUp

    async def test_bot_activity_forbidden_for_non_owner_non_owner_bot(self):
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            token = mint_magic_link_token(FACTORY_BOT_ID, OTHER_TELEGRAM_ID)
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.get(f"/api/factory/bots/{self.bot_id}/activity?token={token}")
        self.assertEqual(resp.status, 403)

    async def test_bot_activity_bad_bot_id_returns_404(self):
        qs = await self._owner_qs()
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.get(f"/api/factory/bots/not-a-number/activity?{qs}")
        self.assertEqual(resp.status, 404)

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
