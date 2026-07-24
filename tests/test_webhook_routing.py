"""Stage 2 Phase 1 — local smoke test for the webhook routing skeleton.

No real Telegram network calls and no real bot tokens: the fake bot's router
only records incoming updates locally, it never calls a Bot API method.

Run with: python -m unittest tests.test_webhook_routing
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from aiogram import Bot, Dispatcher, Router
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp.test_utils import TestClient, TestServer

from runtime.registry import BotEntry, ConfigMiddleware
from runtime.webhook_app import WEBHOOK_SECRET_HEADER, create_app

FAKE_TOKEN = "123456:test-token-not-real"
KNOWN_BOT_ID = 42


def _fake_update(text: str = "/start") -> dict:
    return {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "date": 1700000000,
            "chat": {"id": 111, "type": "private"},
            "from": {"id": 111, "is_bot": False, "first_name": "Test"},
            "text": text,
        },
    }


def _build_fake_entry(received: list) -> BotEntry:
    """A minimal bot entry: its only handler records the message text — no
    outbound Bot API calls, so no real network/token is ever needed."""
    router = Router()

    @router.message()
    async def _record(message, config: dict):  # noqa: ANN001 - test helper
        received.append((message.text, config.get("bot_id")))

    bot = Bot(token=FAKE_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    config = {"bot_id": KNOWN_BOT_ID, "name": "fake_bot"}
    dp.update.outer_middleware(ConfigMiddleware(config))
    dp.include_router(router)
    return BotEntry(bot=bot, dispatcher=dp, template_id="fake", config=config)


class WebhookRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.received: list = []
        registry = {KNOWN_BOT_ID: _build_fake_entry(self.received)}
        self.app = create_app(registry)
        self.server = TestServer(self.app)
        self.client = TestClient(self.server)
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_known_bot_routes_update_to_its_dispatcher(self):
        # /webhook/* is fail-closed on an unset WEBHOOK_SECRET (see
        # STAGE2_REPORT.md "WEBHOOK_SECRET fail-closed") — this test is about
        # bot_id routing, not secret validation, so it sets a matching
        # secret/header pair to actually reach the routing logic.
        with patch.dict(os.environ, {"WEBHOOK_SECRET": "expected-secret"}):
            resp = await self.client.post(
                f"/webhook/{KNOWN_BOT_ID}", json=_fake_update("/start"),
                headers={WEBHOOK_SECRET_HEADER: "expected-secret"},
            )
        self.assertEqual(resp.status, 200)
        self.assertEqual(self.received, [("/start", KNOWN_BOT_ID)])

    async def test_unknown_bot_id_returns_404(self):
        with patch.dict(os.environ, {"WEBHOOK_SECRET": "expected-secret"}):
            resp = await self.client.post(
                "/webhook/999999", json=_fake_update(),
                headers={WEBHOOK_SECRET_HEADER: "expected-secret"},
            )
        self.assertEqual(resp.status, 404)
        self.assertEqual(self.received, [])

    async def test_missing_secret_is_fail_closed(self):
        """The main case this phase closes: an unset/empty WEBHOOK_SECRET must
        reject the request (403), not silently let it through (the old Phase 1
        fail-open trade-off). Explicitly clears the env var (restored after
        the `with` block) rather than relying on it being ambiently unset."""
        with patch.dict(os.environ):
            os.environ.pop("WEBHOOK_SECRET", None)
            resp = await self.client.post(f"/webhook/{KNOWN_BOT_ID}", json=_fake_update())
        self.assertEqual(resp.status, 403)
        body = await resp.json()
        self.assertEqual(body.get("error"), "webhook secret not configured")
        self.assertEqual(self.received, [])

    async def test_secret_configured_but_header_missing_returns_403(self):
        """Distinct from the wrong-header case below: no
        X-Telegram-Bot-Api-Secret-Token header sent at all."""
        with patch.dict(os.environ, {"WEBHOOK_SECRET": "expected-secret"}):
            resp = await self.client.post(f"/webhook/{KNOWN_BOT_ID}", json=_fake_update())
        self.assertEqual(resp.status, 403)
        self.assertEqual(self.received, [])

    async def test_wrong_secret_header_returns_403(self):
        with patch.dict(os.environ, {"WEBHOOK_SECRET": "expected-secret"}):
            resp = await self.client.post(
                f"/webhook/{KNOWN_BOT_ID}",
                json=_fake_update(),
                headers={WEBHOOK_SECRET_HEADER: "wrong-secret"},
            )
        self.assertEqual(resp.status, 403)
        self.assertEqual(self.received, [])

    async def test_correct_secret_header_is_accepted(self):
        with patch.dict(os.environ, {"WEBHOOK_SECRET": "expected-secret"}):
            resp = await self.client.post(
                f"/webhook/{KNOWN_BOT_ID}",
                json=_fake_update("/hello"),
                headers={WEBHOOK_SECRET_HEADER: "expected-secret"},
            )
        self.assertEqual(resp.status, 200)
        self.assertEqual(self.received, [("/hello", KNOWN_BOT_ID)])

    async def test_health_endpoint(self):
        resp = await self.client.get("/health")
        self.assertEqual(resp.status, 200)

    async def test_health_endpoint_ignores_webhook_secret_state(self):
        """/health has no secret check at all and must stay reachable for
        monitoring regardless of whether WEBHOOK_SECRET is set — explicitly
        exercised under both states, not just whatever the ambient env is."""
        with patch.dict(os.environ):
            os.environ.pop("WEBHOOK_SECRET", None)
            resp = await self.client.get("/health")
            self.assertEqual(resp.status, 200)

        with patch.dict(os.environ, {"WEBHOOK_SECRET": "expected-secret"}):
            resp = await self.client.get("/health")
            self.assertEqual(resp.status, 200)


if __name__ == "__main__":
    unittest.main()
