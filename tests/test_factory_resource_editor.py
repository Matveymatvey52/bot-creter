"""runtime/factory_analytics_api.py's owner support-session record editor —
resource_schema_handler / resource_list_handler / resource_update_handler /
resource_delete_handler. Same fixture shape as tests/test_factory_analytics_api.py
(fake Registry + BotEntry, TestClient, no real Telegram network calls), but
this bot's registry entry points at a real temp SQLite file with an actual
table so GET/PATCH/DELETE exercise real SQL, not mocks.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

import aiosqlite
from aiohttp.test_utils import TestClient, TestServer

from db.database import create_bot_record_with_admins, delete_bot, init_db, set_bot_miniapp_config
from runtime.factory_analytics_api import register_routes
from runtime.miniapp_api import mint_magic_link_token
from runtime.registry import FACTORY_BOT_ID, BotEntry
from runtime.webhook_app import create_app

FAKE_TOKEN = "123456789:AAHfakeTokenButShapedRight1234567890"
FACTORY_TOKEN = "987654321:AAHfactoryOwnTokenAlsoShapedRight123456"
OWNER_TELEGRAM_ID = 555
OTHER_TELEGRAM_ID = 999

_MINIAPP_CONFIG = {
    "resources": [
        {
            "name": "bookings",
            "table": "bookings",
            "order_by": "id DESC",
            "creatable": True,
            "title": "Записи",
            "titleField": "client_name",
            "fields": [
                {"name": "client_name", "required": True, "label": "Клиент", "kind": "text", "list": True, "detail": True},
                {"name": "status", "label": "Статус", "kind": "status", "list": True, "detail": True},
            ],
        }
    ]
}


class _FakeBot:
    def __init__(self, token: str) -> None:
        self.token = token


class FactoryResourceEditorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await init_db()
        self.bot_id = await create_bot_record_with_admins(
            name="factory_resource_editor_bot", description="test", token=FAKE_TOKEN,
            file_path="templates/tour_operator.py", admin_ids=["1"],
        )
        await set_bot_miniapp_config(self.bot_id, _MINIAPP_CONFIG)

        tmpdir = tempfile.mkdtemp()
        self.bot_db_path = os.path.join(tmpdir, "bot.db")
        async with aiosqlite.connect(self.bot_db_path) as db:
            await db.execute(
                "CREATE TABLE bookings (id INTEGER PRIMARY KEY AUTOINCREMENT, client_name TEXT, status TEXT)"
            )
            await db.execute("INSERT INTO bookings (client_name, status) VALUES ('Иван', 'pending')")
            await db.execute("INSERT INTO bookings (client_name, status) VALUES ('Анна', 'confirmed')")
            await db.commit()

        factory_entry = BotEntry(
            bot=_FakeBot(FACTORY_TOKEN), dispatcher=None, template_id="__factory__",
            config={"bot_id": FACTORY_BOT_ID},
        )
        bot_entry = BotEntry(
            bot=_FakeBot(FAKE_TOKEN), dispatcher=None, template_id="tour_operator",
            config={"db_path": self.bot_db_path},
        )
        registry = {FACTORY_BOT_ID: factory_entry, self.bot_id: bot_entry}
        self.app = create_app(registry)
        register_routes(self.app)

        self._owner_patcher = patch("runtime.factory_analytics_api.OWNER_ID", OWNER_TELEGRAM_ID)
        self._owner_patcher.start()
        # MINIAPP_SECRET must be set for mint_magic_link_token/_verify_magic_link_token
        # to work at all (see mint_magic_link_token's fail-closed RuntimeError) —
        # every test in this class mints and/or verifies a token, so this is set
        # once for the whole fixture rather than per-test like
        # tests/test_factory_analytics_api.py's narrower with-blocks.
        self._env_patcher = patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"})
        self._env_patcher.start()

        self.server = TestServer(self.app)
        self.client = TestClient(self.server)
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self._env_patcher.stop()
        self._owner_patcher.stop()
        await delete_bot(self.bot_id)

    def _owner_token(self) -> str:
        return mint_magic_link_token(FACTORY_BOT_ID, OWNER_TELEGRAM_ID)

    async def test_schema_returns_display_metadata_without_table_or_order_by(self):
        resp = await self.client.get(f"/api/factory/bots/{self.bot_id}/schema?token={self._owner_token()}")
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        resource = body["resources"][0]
        self.assertEqual(resource["name"], "bookings")
        self.assertNotIn("table", resource)
        self.assertNotIn("order_by", resource)

    async def test_list_returns_all_rows_regardless_of_role_filter(self):
        resp = await self.client.get(f"/api/factory/bots/{self.bot_id}/bookings?token={self._owner_token()}")
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertEqual(len(body["items"]), 2)
        names = {item["client_name"] for item in body["items"]}
        self.assertEqual(names, {"Иван", "Анна"})

    async def test_update_writes_new_values(self):
        list_resp = await self.client.get(f"/api/factory/bots/{self.bot_id}/bookings?token={self._owner_token()}")
        item_id = (await list_resp.json())["items"][0]["id"]

        resp = await self.client.patch(
            f"/api/factory/bots/{self.bot_id}/bookings/{item_id}?token={self._owner_token()}",
            json={"status": "cancelled"},
        )
        self.assertEqual(resp.status, 200)

        async with aiosqlite.connect(self.bot_db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT status FROM bookings WHERE id = ?", (item_id,)) as cursor:
                row = await cursor.fetchone()
        self.assertEqual(row["status"], "cancelled")

    async def test_update_ignores_unknown_fields(self):
        list_resp = await self.client.get(f"/api/factory/bots/{self.bot_id}/bookings?token={self._owner_token()}")
        item_id = (await list_resp.json())["items"][0]["id"]

        resp = await self.client.patch(
            f"/api/factory/bots/{self.bot_id}/bookings/{item_id}?token={self._owner_token()}",
            json={"not_a_real_column": "x"},
        )
        self.assertEqual(resp.status, 400)

    async def test_update_missing_row_returns_404(self):
        resp = await self.client.patch(
            f"/api/factory/bots/{self.bot_id}/bookings/999999?token={self._owner_token()}",
            json={"status": "cancelled"},
        )
        self.assertEqual(resp.status, 404)

    async def test_delete_removes_row(self):
        list_resp = await self.client.get(f"/api/factory/bots/{self.bot_id}/bookings?token={self._owner_token()}")
        item_id = (await list_resp.json())["items"][0]["id"]

        resp = await self.client.delete(f"/api/factory/bots/{self.bot_id}/bookings/{item_id}?token={self._owner_token()}")
        self.assertEqual(resp.status, 200)

        async with aiosqlite.connect(self.bot_db_path) as db:
            async with db.execute("SELECT COUNT(*) FROM bookings") as cursor:
                (count,) = await cursor.fetchone()
        self.assertEqual(count, 1)

    async def test_delete_missing_row_returns_404(self):
        resp = await self.client.delete(f"/api/factory/bots/{self.bot_id}/bookings/999999?token={self._owner_token()}")
        self.assertEqual(resp.status, 404)

    async def test_non_owner_cannot_list(self):
        token = mint_magic_link_token(FACTORY_BOT_ID, OTHER_TELEGRAM_ID)
        resp = await self.client.get(f"/api/factory/bots/{self.bot_id}/bookings?token={token}")
        self.assertEqual(resp.status, 403)

    async def test_non_owner_cannot_update(self):
        list_resp = await self.client.get(f"/api/factory/bots/{self.bot_id}/bookings?token={self._owner_token()}")
        item = (await list_resp.json())["items"][0]
        item_id, original_status = item["id"], item["status"]
        token = mint_magic_link_token(FACTORY_BOT_ID, OTHER_TELEGRAM_ID)

        resp = await self.client.patch(
            f"/api/factory/bots/{self.bot_id}/bookings/{item_id}?token={token}",
            json={"status": "cancelled"},
        )
        self.assertEqual(resp.status, 403)

        async with aiosqlite.connect(self.bot_db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT status FROM bookings WHERE id = ?", (item_id,)) as cursor:
                row = await cursor.fetchone()
        self.assertEqual(row["status"], original_status)

    async def test_non_owner_cannot_delete(self):
        list_resp = await self.client.get(f"/api/factory/bots/{self.bot_id}/bookings?token={self._owner_token()}")
        item_id = (await list_resp.json())["items"][0]["id"]
        token = mint_magic_link_token(FACTORY_BOT_ID, OTHER_TELEGRAM_ID)

        resp = await self.client.delete(f"/api/factory/bots/{self.bot_id}/bookings/{item_id}?token={token}")
        self.assertEqual(resp.status, 403)

        async with aiosqlite.connect(self.bot_db_path) as db:
            async with db.execute("SELECT COUNT(*) FROM bookings") as cursor:
                (count,) = await cursor.fetchone()
        self.assertEqual(count, 2)

    async def test_unknown_resource_returns_404(self):
        resp = await self.client.get(f"/api/factory/bots/{self.bot_id}/not_a_resource?token={self._owner_token()}")
        self.assertEqual(resp.status, 404)


if __name__ == "__main__":
    unittest.main()
