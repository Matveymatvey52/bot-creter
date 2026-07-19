"""Stage 2 Phase 3 — live registry update tests.

Covers the criteria from the phase's Task 4: a bot added after the webhook
server started must start routing (404 -> 200), a removed bot must stop
(200 -> 404), reload_one must pick up a changed DB row, and none of this may
crash or drop live entries when a webhook update races a reload.

No real Telegram network calls (Bot.__call__ is mocked), no real tokens.

Run with: python -m unittest tests.test_registry_reload
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram import Bot
from aiohttp.test_utils import TestClient, TestServer

from db.database import (
    create_bot_record_with_admins,
    delete_bot,
    init_db,
    set_bot_display_name,
)
from runtime.registry import Registry
from runtime.webhook_app import create_app

FAKE_TOKEN = "123456:test-token-not-real"


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


class RegistryLiveUpdateTests(unittest.IsolatedAsyncioTestCase):
    """Uses the real SQLite bots table (temp DATA_DIR from the test harness env,
    see how the test is invoked) so reload_one exercises the actual db.database
    read path, not a stand-in."""

    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        await init_db()

    async def asyncTearDown(self):
        self._bot_call_patcher.stop()

    async def test_add_or_replace_makes_a_new_bot_start_routing(self):
        registry = Registry()
        app = create_app(registry)
        server = TestServer(app)
        client = TestClient(server)
        await client.start_server()
        try:
            bot_id = await create_bot_record_with_admins(
                name="reload_test_bot_a",
                description="test",
                token=FAKE_TOKEN,
                file_path="",
                admin_ids=["111"],
            )
            try:
                # Not in the registry yet — 404.
                resp = await client.post(f"/webhook/{bot_id}", json=_fake_update())
                self.assertEqual(resp.status, 404)

                # add_or_replace picks it up from a bots-table row shape.
                added = await registry.add_or_replace(
                    {"id": bot_id, "name": "reload_test_bot_a", "token": FAKE_TOKEN,
                     "file_path": "", "display_name": None, "group_chat_id": None}
                )
                self.assertIsNotNone(added)

                # Now it routes (200), not 404.
                resp = await client.post(f"/webhook/{bot_id}", json=_fake_update())
                self.assertEqual(resp.status, 200)

                # remove() takes it back out — 404 again.
                removed = await registry.remove(bot_id)
                self.assertTrue(removed)
                resp = await client.post(f"/webhook/{bot_id}", json=_fake_update())
                self.assertEqual(resp.status, 404)
            finally:
                await delete_bot(bot_id)
        finally:
            await client.close()

    async def test_reload_one_picks_up_changed_bot_row(self):
        registry = Registry()
        bot_id = await create_bot_record_with_admins(
            name="reload_test_bot_b",
            description="test",
            token=FAKE_TOKEN,
            file_path="",
            admin_ids=["111"],
        )
        try:
            entry1 = await registry.reload_one(bot_id)
            self.assertIsNotNone(entry1)
            self.assertIsNone(entry1.config.get("display_name"))

            # Change the underlying DB row...
            await set_bot_display_name(bot_id, "Renamed Bot")
            # ...reload_one must reflect the new value, not a stale cached one.
            entry2 = await registry.reload_one(bot_id)
            self.assertIsNotNone(entry2)
            self.assertEqual(entry2.config.get("display_name"), "Renamed Bot")

            # reload_one on a bot that no longer exists removes it cleanly.
            await delete_bot(bot_id)
            entry3 = await registry.reload_one(bot_id)
            self.assertIsNone(entry3)
            self.assertIsNone(registry.get(bot_id))
        finally:
            # idempotent — bot may already be deleted above
            await delete_bot(bot_id)

    async def test_concurrent_webhook_traffic_survives_reload_all(self):
        """A reload_all() running concurrently with live webhook traffic for an
        unrelated, still-existing bot must never make that bot's lookups fail
        or raise.

        NOTE on what this does/doesn't prove: asyncio only switches coroutines
        at an `await`, and there is no `await` between `old_entries = self._entries`
        and `self._entries = new_entries` in Registry.reload_all (nor would there
        be one in a hypothetical clear()+refill() under the same lock) — so no
        concurrent get() could observe an in-between state in *either* design;
        this test can't by itself distinguish the two. What building `new_entries`
        fully before acquiring the lock actually buys is exception-safety: one bad
        row failing mid-build (see reload_all's per-row try/except) never touches
        the live registry. What THIS test verifies is the practical guarantee that
        matters: no crash and no dropped lookups for an unrelated bot under
        concurrent get()/reload_all() traffic."""
        registry = Registry()
        bot_id = await create_bot_record_with_admins(
            name="reload_test_bot_c",
            description="test",
            token=FAKE_TOKEN,
            file_path="",
            admin_ids=["111"],
        )
        try:
            await registry.reload_one(bot_id)
            self.assertIsNotNone(registry.get(bot_id))

            errors: list[BaseException] = []
            misses: list[int] = []

            async def hammer_get(n: int) -> None:
                for _ in range(n):
                    entry = registry.get(bot_id)
                    if entry is None:
                        misses.append(1)
                    await asyncio.sleep(0)

            async def hammer_reload(n: int) -> None:
                for _ in range(n):
                    await registry.reload_all()

            async def guarded(coro) -> None:
                try:
                    await coro
                except BaseException as e:  # noqa: BLE001 - want to see ANY crash
                    errors.append(e)

            await asyncio.gather(
                guarded(hammer_get(200)),
                guarded(hammer_get(200)),
                guarded(hammer_reload(20)),
                guarded(hammer_reload(20)),
            )

            self.assertEqual(errors, [])
            self.assertEqual(misses, [], "get() saw the bot missing mid-reload — swap was not atomic")
        finally:
            await delete_bot(bot_id)


if __name__ == "__main__":
    unittest.main()
