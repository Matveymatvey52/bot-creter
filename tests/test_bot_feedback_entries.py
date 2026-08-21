"""features/bot_feedback_entries.py — the reusable customer-review module
(cashflow_ledger's pattern applied to reviews). Covers:
  - init_feedback_table/list_feedback/delete_feedback, the module's own
    thin DB helpers (mirrors tests/test_cashflow_ledger.py's style if one
    exists, else tests/test_factory_resource_editor.py's fixture shape).
  - the end-to-end flow this module is FOR: a customer submits a review
    through the generic mini-app (runtime/miniapp_api.create_resource_handler)
    signed with their own Telegram identity, can only see their OWN review
    back (FEEDBACK_RESOURCE's ownership-only role_filter), and the owner's
    separate support-session record editor (Track A,
    runtime/factory_analytics_api.py) can edit/delete ANY review regardless
    of role_filter.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import aiosqlite
from aiohttp.test_utils import TestClient, TestServer

from db.database import create_bot_record_with_admins, delete_bot, init_db, set_bot_miniapp_config
from features.bot_feedback_entries import (
    FEEDBACK_RESOURCE,
    delete_feedback,
    init_feedback_table,
    list_feedback,
)
from runtime.factory_analytics_api import register_routes as register_factory_routes
from runtime.miniapp_api import mint_magic_link_token
from runtime.miniapp_api import register_routes as register_miniapp_routes
from runtime.registry import FACTORY_BOT_ID, BotEntry
from runtime.webhook_app import create_app

FAKE_TOKEN = "123456789:AAHfakeTokenButShapedRight1234567890"
FACTORY_TOKEN = "987654321:AAHfactoryOwnTokenAlsoShapedRight123456"
OWNER_TELEGRAM_ID = 555
CUSTOMER_A_ID = 111
CUSTOMER_B_ID = 222

_MINIAPP_CONFIG = {"resources": [FEEDBACK_RESOURCE]}


class BotFeedbackEntriesModuleTests(unittest.IsolatedAsyncioTestCase):
    """Direct tests of the module's own DB helpers, no HTTP involved."""

    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmpdir.name, "bot.db")
        await init_feedback_table(self.db_path)

    async def asyncTearDown(self):
        self._tmpdir.cleanup()

    async def test_init_is_idempotent(self):
        await init_feedback_table(self.db_path)  # must not raise on a second call
        self.assertEqual(await list_feedback(self.db_path), [])

    async def test_list_feedback_newest_first(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO bot_feedback_entries (telegram_user_id, author_name, rating, comment) "
                "VALUES (111, 'Иван', 5, 'Отлично')"
            )
            await db.execute(
                "INSERT INTO bot_feedback_entries (telegram_user_id, author_name, rating, comment) "
                "VALUES (222, 'Анна', 3, 'Неплохо')"
            )
            await db.commit()
        rows = await list_feedback(self.db_path)
        self.assertEqual([r["author_name"] for r in rows], ["Анна", "Иван"])

    async def test_delete_feedback_removes_row(self):
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "INSERT INTO bot_feedback_entries (telegram_user_id, rating) VALUES (111, 4)"
            )
            await db.commit()
            entry_id = cur.lastrowid
        self.assertTrue(await delete_feedback(self.db_path, entry_id))
        self.assertEqual(await list_feedback(self.db_path), [])

    async def test_delete_feedback_missing_row_returns_false(self):
        self.assertFalse(await delete_feedback(self.db_path, 9999))

    async def test_rating_out_of_range_is_rejected_by_schema(self):
        async with aiosqlite.connect(self.db_path) as db:
            with self.assertRaises(aiosqlite.IntegrityError):
                await db.execute(
                    "INSERT INTO bot_feedback_entries (telegram_user_id, rating) VALUES (111, 6)"
                )
                await db.commit()


class _FakeBot:
    def __init__(self, token: str) -> None:
        self.token = token


class BotFeedbackEntriesEndToEndTests(unittest.IsolatedAsyncioTestCase):
    """Customer submits via runtime/miniapp_api.py, owner edits/deletes via
    runtime/factory_analytics_api.py's Track A resource editor — both route
    sets registered on the same app, same as production (combined_app.py)."""

    async def asyncSetUp(self):
        await init_db()
        self.bot_id = await create_bot_record_with_admins(
            name="feedback_e2e_bot", description="test", token=FAKE_TOKEN,
            file_path="templates/tour_operator.py", admin_ids=["1"],
        )
        await set_bot_miniapp_config(self.bot_id, _MINIAPP_CONFIG)

        self._tmpdir = tempfile.TemporaryDirectory()
        self.bot_db_path = os.path.join(self._tmpdir.name, "bot.db")
        await init_feedback_table(self.bot_db_path)

        factory_entry = BotEntry(
            bot=_FakeBot(FACTORY_TOKEN), dispatcher=None, template_id="__factory__",
            config={"bot_id": FACTORY_BOT_ID},
        )
        bot_entry = BotEntry(
            bot=_FakeBot(FAKE_TOKEN), dispatcher=None, template_id="tour_operator",
            config={"bot_id": self.bot_id, "db_path": self.bot_db_path},
        )
        registry = {FACTORY_BOT_ID: factory_entry, self.bot_id: bot_entry}
        self.app = create_app(registry)
        register_miniapp_routes(self.app)
        register_factory_routes(self.app)

        self._db_config_patcher = patch(
            "runtime.miniapp_api.get_bot_miniapp_config", AsyncMock(return_value=_MINIAPP_CONFIG)
        )
        self._db_config_patcher.start()

        self._owner_patcher = patch("runtime.factory_analytics_api.OWNER_ID", OWNER_TELEGRAM_ID)
        self._owner_patcher.start()
        self._env_patcher = patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"})
        self._env_patcher.start()

        self.server = TestServer(self.app)
        self.client = TestClient(self.server)
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self._env_patcher.stop()
        self._owner_patcher.stop()
        self._db_config_patcher.stop()
        self._tmpdir.cleanup()
        await delete_bot(self.bot_id)

    def _customer_token(self, telegram_user_id: int) -> str:
        return mint_magic_link_token(self.bot_id, telegram_user_id)

    def _owner_token(self) -> str:
        return mint_magic_link_token(FACTORY_BOT_ID, OWNER_TELEGRAM_ID)

    async def test_customer_submits_review_signed_with_own_identity(self):
        token = self._customer_token(CUSTOMER_A_ID)
        resp = await self.client.post(
            f"/api/{self.bot_id}/feedback?token={token}",
            json={"author_name": "Иван", "rating": 5, "comment": "Отличный сервис"},
        )
        self.assertEqual(resp.status, 201)
        rows = await list_feedback(self.bot_db_path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["telegram_user_id"], CUSTOMER_A_ID)
        self.assertEqual(rows[0]["rating"], 5)

    async def test_customer_cannot_spoof_another_users_telegram_id(self):
        token = self._customer_token(CUSTOMER_A_ID)
        resp = await self.client.post(
            f"/api/{self.bot_id}/feedback?token={token}",
            json={"telegram_user_id": CUSTOMER_B_ID, "rating": 5},
        )
        self.assertEqual(resp.status, 201)
        rows = await list_feedback(self.bot_db_path)
        self.assertEqual(rows[0]["telegram_user_id"], CUSTOMER_A_ID)

    async def test_customer_sees_only_their_own_reviews(self):
        for uid, rating in ((CUSTOMER_A_ID, 5), (CUSTOMER_B_ID, 2)):
            token = self._customer_token(uid)
            await self.client.post(f"/api/{self.bot_id}/feedback?token={token}", json={"rating": rating})

        token_a = self._customer_token(CUSTOMER_A_ID)
        resp = await self.client.get(f"/api/{self.bot_id}/feedback?token={token_a}")
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertEqual(len(body["items"]), 1)
        self.assertEqual(body["items"][0]["rating"], 5)

    async def test_owner_can_edit_any_customers_review_via_track_a(self):
        token = self._customer_token(CUSTOMER_A_ID)
        await self.client.post(f"/api/{self.bot_id}/feedback?token={token}", json={"rating": 2, "comment": "meh"})
        entry_id = (await list_feedback(self.bot_db_path))[0]["id"]

        owner_token = self._owner_token()
        resp = await self.client.patch(
            f"/api/factory/bots/{self.bot_id}/feedback/{entry_id}?token={owner_token}",
            json={"comment": "resolved by support"},
        )
        self.assertEqual(resp.status, 200)
        rows = await list_feedback(self.bot_db_path)
        self.assertEqual(rows[0]["comment"], "resolved by support")

    async def test_owner_can_delete_any_customers_review_via_track_a(self):
        token = self._customer_token(CUSTOMER_A_ID)
        await self.client.post(f"/api/{self.bot_id}/feedback?token={token}", json={"rating": 1})
        entry_id = (await list_feedback(self.bot_db_path))[0]["id"]

        owner_token = self._owner_token()
        resp = await self.client.delete(
            f"/api/factory/bots/{self.bot_id}/feedback/{entry_id}?token={owner_token}"
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(await list_feedback(self.bot_db_path), [])


if __name__ == "__main__":
    unittest.main()
