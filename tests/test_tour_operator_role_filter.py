"""tour_operator template — role_filter isolation via the real mini-app REST
layer (runtime/miniapp_api.py), using tour_operator's REAL miniapp_config and
init_db schema (not a fixture) — same harness style as
tests/test_property_rental_role_filter.py, adapted for tour_operator's
tour_access resolve table (see templates/tour_operator.py's
_TOUR_ACCESS_RESOLVE):

Ресурса "tours_public" в конфиге больше нет: тур-оператор — инструмент
одного человека, клиентов у него не бывает, и клиентский срез над той же
таблицей tours только плодил вторую вкладку «Туры» у владельца. Вместе с
ресурсом ушли и четыре теста, которые в него ходили.

Что проверяется теперь: у бота НЕ остаётся ни одного раздела, доступного
не-админу — клиент получает 403 и на "tours", и на "guests", а владелец
работает как раньше. Это и есть контракт однопользовательского бота.

Run with: python -m unittest tests.test_tour_operator_role_filter
"""

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
from templates import tour_operator

FAKE_TOKEN = "123456:test-token-not-real"
KNOWN_BOT_ID = 77
OWNER_ID = 700
CLIENT_A_ID = 601
CLIENT_B_ID = 602


class _FakeBot:
    def __init__(self, token: str) -> None:
        self.token = token


class TourOperatorRoleFilterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmpdir.name, "bot.db")
        await tour_operator.init_db(self.db_path)

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO tours (name, destination, status) VALUES ('Tour One', 'Bali', 'planning')"
            )
            await db.execute(
                "INSERT INTO tours (name, destination, status) VALUES ('Tour Two', 'Phuket', 'active')"
            )
            await db.execute("INSERT INTO tour_access (user_id, role) VALUES (?, 'owner')", (OWNER_ID,))
            await db.execute("INSERT INTO tour_access (user_id, role) VALUES (?, 'client')", (CLIENT_A_ID,))
            await db.commit()

        entry = BotEntry(
            bot=_FakeBot(FAKE_TOKEN),
            dispatcher=None,
            template_id="tour_operator",
            config={"bot_id": KNOWN_BOT_ID, "db_path": self.db_path},
        )
        registry = {KNOWN_BOT_ID: entry}
        self.app = create_app(registry)
        register_routes(self.app)

        self._module_patcher = patch(
            "runtime.miniapp_api._load_template_module_async", AsyncMock(return_value=tour_operator),
        )
        self._module_patcher.start()

        self._db_patcher = patch(
            "runtime.miniapp_api.get_bot_miniapp_config", AsyncMock(return_value=None),
        )
        self._db_patcher.start()

        # Only OWNER_ID is a real bot admin — exercised by the "tours"
        # (role_filter-less, admin-only) resource below.
        self._admins_patcher = patch(
            "runtime.miniapp_api.get_bot_admins", AsyncMock(return_value=[str(OWNER_ID)]),
        )
        self._admins_patcher.start()

        self.server = TestServer(self.app)
        self.client = TestClient(self.server)
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self._module_patcher.stop()
        self._db_patcher.stop()
        self._admins_patcher.stop()
        self._tmpdir.cleanup()

    async def _get(self, path: str, telegram_user_id: int):
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            qs = f"token={mint_magic_link_token(KNOWN_BOT_ID, telegram_user_id)}"
            return await self.client.get(f"/api/{KNOWN_BOT_ID}/{path}?{qs}")

    async def test_client_is_forbidden_from_the_admin_only_tours_resource(self):
        # "tours" has no role_filter at all — _admin_gate_ok falls back to
        # admins.json membership, which a client never has.
        resp = await self._get("tours", CLIENT_A_ID)
        self.assertEqual(resp.status, 403)

    async def test_owner_can_still_use_the_admin_only_tours_resource(self):
        resp = await self._get("tours", OWNER_ID)
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertEqual(len(body["items"]), 2)

    async def test_client_is_forbidden_from_guests_resource(self):
        resp = await self._get("guests", CLIENT_A_ID)
        self.assertEqual(resp.status, 403)


if __name__ == "__main__":
    unittest.main()
