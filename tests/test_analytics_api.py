"""runtime/miniapp_api.py's analytics_handler (GET /api/{bot_id}/analytics)
— see docs/SALES_ANALYTICS_DESIGN.md. Same fixture style as
tests/test_miniapp_api.py: fake BotEntry + registry dict, magic-link token
auth, real temp sqlite file.

Central contracts under test: (1) owner-only — a valid, authenticated
non-admin telegram_user_id must still be rejected; (2) 404 when the bot has
no usable office_hook_config, distinct from the 403 "not the owner" case, so
the SPA can tell "hide this tab" from "you're not allowed here" (both
currently render the same way client-side, but the backend must still
distinguish them for anyone else consuming this API)."""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import aiosqlite
from aiohttp.test_utils import TestClient, TestServer

from runtime.registry import BotEntry
from runtime.webhook_app import create_app
from runtime.miniapp_api import mint_magic_link_token, register_routes

FAKE_TOKEN = "123456:test-token-not-real"
KNOWN_BOT_ID = 77
OWNER_TELEGRAM_ID = 555
NON_OWNER_TELEGRAM_ID = 999


class _FakeBot:
    def __init__(self, token: str) -> None:
        self.token = token


async def _make_db_with_bookings() -> str:
    tmpdir = tempfile.mkdtemp()
    db_path = os.path.join(tmpdir, "bot.db")
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "CREATE TABLE bookings (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "customer_chat_id INTEGER, created_at TEXT DEFAULT (datetime('now', 'localtime')))"
        )
        await db.execute("INSERT INTO bookings (customer_chat_id) VALUES (1)")
        await db.execute("INSERT INTO bookings (customer_chat_id) VALUES (1)")
        await db.commit()
    return db_path


class AnalyticsApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db_path = await _make_db_with_bookings()
        entry = BotEntry(
            bot=_FakeBot(FAKE_TOKEN),
            dispatcher=None,
            template_id="event_rsvp",
            config={"bot_id": KNOWN_BOT_ID, "db_path": self.db_path},
        )
        self.app = create_app({KNOWN_BOT_ID: entry})
        register_routes(self.app)

        self._admins_patcher = patch(
            "runtime.miniapp_api.get_bot_admins", AsyncMock(return_value=[str(OWNER_TELEGRAM_ID)])
        )
        self._admins_patcher.start()

        self._hook_config_patcher = patch(
            "runtime.miniapp_api.get_bot_office_hook_config",
            AsyncMock(
                return_value={
                    "table": "bookings",
                    "match_field": "customer_chat_id",
                    "created_at_field": "created_at",
                }
            ),
        )
        self._hook_config_patcher.start()

        self.server = TestServer(self.app)
        self.client = TestClient(self.server)
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self._admins_patcher.stop()
        self._hook_config_patcher.stop()

    async def _owner_query(self) -> str:
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            return f"token={mint_magic_link_token(KNOWN_BOT_ID, OWNER_TELEGRAM_ID)}"

    async def _non_owner_query(self) -> str:
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            return f"token={mint_magic_link_token(KNOWN_BOT_ID, NON_OWNER_TELEGRAM_ID)}"

    async def test_owner_gets_metrics(self):
        qs = await self._owner_query()
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.get(f"/api/{KNOWN_BOT_ID}/analytics?period=week&{qs}")
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertEqual(body["total"], 2)
        self.assertEqual(body["top_customers"], [{"match_field": 1, "count": 2}])

    async def test_non_owner_gets_403(self):
        qs = await self._non_owner_query()
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.get(f"/api/{KNOWN_BOT_ID}/analytics?period=week&{qs}")
        self.assertEqual(resp.status, 403)

    async def test_no_credentials_returns_403(self):
        resp = await self.client.get(f"/api/{KNOWN_BOT_ID}/analytics?period=week")
        self.assertEqual(resp.status, 403)

    async def test_unknown_bot_id_returns_404(self):
        qs = await self._owner_query()
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.get(f"/api/999999/analytics?period=week&{qs}")
        self.assertEqual(resp.status, 404)

    async def test_no_office_hook_config_returns_404(self):
        with patch(
            "runtime.miniapp_api.get_bot_office_hook_config", AsyncMock(return_value=None)
        ):
            qs = await self._owner_query()
            with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
                resp = await self.client.get(f"/api/{KNOWN_BOT_ID}/analytics?period=week&{qs}")
        self.assertEqual(resp.status, 404)

    async def test_invalid_period_returns_400(self):
        qs = await self._owner_query()
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.get(f"/api/{KNOWN_BOT_ID}/analytics?period=year&{qs}")
        self.assertEqual(resp.status, 400)

    async def test_default_period_is_week(self):
        qs = await self._owner_query()
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.get(f"/api/{KNOWN_BOT_ID}/analytics?{qs}")
        self.assertEqual(resp.status, 200)

    async def test_missing_db_path_returns_500(self):
        entry = BotEntry(
            bot=_FakeBot(FAKE_TOKEN),
            dispatcher=None,
            template_id="event_rsvp",
            config={"bot_id": KNOWN_BOT_ID},  # no db_path
        )
        app = create_app({KNOWN_BOT_ID: entry})
        register_routes(app)
        server = TestServer(app)
        client = TestClient(server)
        await client.start_server()
        try:
            qs = await self._owner_query()
            with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
                resp = await client.get(f"/api/{KNOWN_BOT_ID}/analytics?period=week&{qs}")
            self.assertEqual(resp.status, 500)
        finally:
            await client.close()

    async def test_analytics_route_reserved_before_generic_resource_route(self):
        # "analytics" must never be swallowed as a resource name — same
        # ordering guarantee as "schema" (see register_routes' comment).
        qs = await self._owner_query()
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.get(f"/api/{KNOWN_BOT_ID}/analytics?period=week&{qs}")
        body = await resp.json()
        self.assertNotIn("resource", body)  # list_resource_handler's shape, not analytics_handler's


if __name__ == "__main__":
    unittest.main()
