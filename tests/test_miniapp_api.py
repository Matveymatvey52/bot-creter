"""Tests for runtime/miniapp_api.py — the mini-app REST layer (pilot,
tour_operator). Same style as tests/test_webhook_routing.py: a fake BotEntry
+ registry dict, no real Telegram network calls, no real bot tokens.

Auth is tested against BOTH paths (see miniapp_api.py's module docstring):
Telegram initData HMAC verification, and the magic-link token
mint/verify round trip.
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from unittest.mock import patch

import aiosqlite
from aiohttp.test_utils import TestClient, TestServer

from runtime.registry import BotEntry
from runtime.webhook_app import create_app
from runtime.miniapp_api import (
    _verify_telegram_init_data,
    mint_magic_link_token,
    register_routes,
)

FAKE_TOKEN = "123456:test-token-not-real"
KNOWN_BOT_ID = 42

FAKE_MINIAPP_CONFIG = {
    "resources": [
        {
            "name": "tours",
            "table": "tours",
            "order_by": "id DESC",
            "creatable": True,
            "fields": [
                {"name": "name", "required": True},
                {"name": "status"},
            ],
        },
        {
            "name": "readonly_res",
            "table": "tours",
            "order_by": "id DESC",
            "creatable": False,
            "fields": [{"name": "name"}],
        },
    ],
}


class _FakeModule:
    miniapp_config = FAKE_MINIAPP_CONFIG


class _FakeBot:
    """Stand-in for aiogram's Bot — only `.token` is read by miniapp_api.py."""

    def __init__(self, token: str) -> None:
        self.token = token


async def _init_db(db_path: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "CREATE TABLE tours (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, status TEXT)"
        )
        await db.execute("INSERT INTO tours (name, status) VALUES ('Bali trip', 'planning')")
        await db.commit()


class MiniAppApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmpdir.name, "bot.db")
        await _init_db(self.db_path)

        entry = BotEntry(
            bot=_FakeBot(FAKE_TOKEN),
            dispatcher=None,  # never invoked by these routes
            template_id="tour_operator",
            config={"bot_id": KNOWN_BOT_ID, "db_path": self.db_path},
        )
        registry = {KNOWN_BOT_ID: entry}
        self.app = create_app(registry)
        register_routes(self.app)

        # register_routes() only adds route handlers; the handlers themselves
        # call _load_template_module_async at request time — patched here to
        # the fake config above so these tests don't depend on
        # tour_operator.py's real schema.
        patcher = patch(
            "runtime.miniapp_api._load_template_module_async",
            return_value=_FakeModule(),
        )
        self._patcher = patcher
        patcher.start()

        self.server = TestServer(self.app)
        self.client = TestClient(self.server)
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self._patcher.stop()
        self._tmpdir.cleanup()

    # ── magic-link token auth ──────────────────────────────────────────
    async def test_valid_magic_link_token_authenticates(self):
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            token = mint_magic_link_token(KNOWN_BOT_ID, telegram_user_id=111)
            resp = await self.client.get(f"/api/{KNOWN_BOT_ID}/me?token={token}")
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertEqual(body["telegram_user_id"], 111)

    async def test_expired_magic_link_token_rejected(self):
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            token = mint_magic_link_token(KNOWN_BOT_ID, telegram_user_id=111)
            parts = token.split(":")
            parts[2] = str(int(time.time()) - 10)  # force expiry into the past
            expired = ":".join(parts)  # signature no longer matches — also covers tamper detection
            resp = await self.client.get(f"/api/{KNOWN_BOT_ID}/me?token={expired}")
        self.assertEqual(resp.status, 403)

    async def test_token_scoped_to_wrong_bot_id_rejected(self):
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            token = mint_magic_link_token(bot_id=999, telegram_user_id=111)
            resp = await self.client.get(f"/api/{KNOWN_BOT_ID}/me?token={token}")
        self.assertEqual(resp.status, 403)

    async def test_missing_secret_rejects_token_auth(self):
        with patch.dict(os.environ):
            os.environ.pop("MINIAPP_SECRET", None)
            resp = await self.client.get(f"/api/{KNOWN_BOT_ID}/me?token=whatever")
        self.assertEqual(resp.status, 403)

    async def test_no_credentials_returns_403(self):
        resp = await self.client.get(f"/api/{KNOWN_BOT_ID}/me")
        self.assertEqual(resp.status, 403)

    # ── initData auth ──────────────────────────────────────────────────
    def test_verify_telegram_init_data_accepts_correctly_signed_payload(self):
        import hashlib
        import hmac as hmac_module
        import json
        from urllib.parse import urlencode

        user = json.dumps({"id": 555, "first_name": "T"})
        fields = {"auth_date": "1700000000", "user": user}
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
        secret_key = hmac_module.new(b"WebAppData", FAKE_TOKEN.encode(), hashlib.sha256).digest()
        expected_hash = hmac_module.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        init_data = urlencode({**fields, "hash": expected_hash})

        result = _verify_telegram_init_data(init_data, FAKE_TOKEN)
        self.assertEqual(result, 555)

    def test_verify_telegram_init_data_rejects_tampered_payload(self):
        init_data = "user=%7B%22id%22%3A555%7D&auth_date=1700000000&hash=deadbeef"
        result = _verify_telegram_init_data(init_data, FAKE_TOKEN)
        self.assertIsNone(result)

    # ── resource CRUD ────────────────────────────────────────────────────
    async def _auth_query(self) -> str:
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            return f"token={mint_magic_link_token(KNOWN_BOT_ID, 111)}"

    async def test_list_resource_returns_seeded_row(self):
        qs = await self._auth_query()
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.get(f"/api/{KNOWN_BOT_ID}/tours?{qs}")
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertEqual(len(body["items"]), 1)
        self.assertEqual(body["items"][0]["name"], "Bali trip")

    async def test_unknown_resource_returns_404(self):
        qs = await self._auth_query()
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.get(f"/api/{KNOWN_BOT_ID}/not_a_resource?{qs}")
        self.assertEqual(resp.status, 404)

    async def test_get_resource_detail(self):
        qs = await self._auth_query()
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.get(f"/api/{KNOWN_BOT_ID}/tours/1?{qs}")
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertEqual(body["item"]["name"], "Bali trip")

    async def test_get_resource_detail_not_found(self):
        qs = await self._auth_query()
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.get(f"/api/{KNOWN_BOT_ID}/tours/999?{qs}")
        self.assertEqual(resp.status, 404)

    async def test_create_resource_inserts_row(self):
        qs = await self._auth_query()
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.post(
                f"/api/{KNOWN_BOT_ID}/tours?{qs}", json={"name": "Egypt trip", "status": "planning"}
            )
        self.assertEqual(resp.status, 201)
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT COUNT(*) FROM tours") as cur:
                (count,) = await cur.fetchone()
        self.assertEqual(count, 2)

    async def test_create_resource_missing_required_field_returns_400(self):
        qs = await self._auth_query()
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.post(f"/api/{KNOWN_BOT_ID}/tours?{qs}", json={"status": "planning"})
        self.assertEqual(resp.status, 400)

    async def test_create_on_readonly_resource_returns_403(self):
        qs = await self._auth_query()
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.post(f"/api/{KNOWN_BOT_ID}/readonly_res?{qs}", json={"name": "x"})
        self.assertEqual(resp.status, 403)

    async def test_create_ignores_unknown_payload_keys(self):
        """A payload key that isn't in the resource's declared fields must be
        silently dropped, not written to the DB — this is the injection
        guard miniapp_api.py's create_resource_handler docstring describes."""
        qs = await self._auth_query()
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.post(
                f"/api/{KNOWN_BOT_ID}/tours?{qs}",
                json={"name": "Peru trip", "not_a_real_column": "malicious"},
            )
        self.assertEqual(resp.status, 201)

    async def test_unknown_bot_id_returns_404(self):
        resp = await self.client.get("/api/999999/tours")
        self.assertEqual(resp.status, 404)


if __name__ == "__main__":
    unittest.main()
