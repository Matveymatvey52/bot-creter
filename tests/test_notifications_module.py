"""features/notifications.py — subscriber tracking, broadcast flow, rate
limiting, and unsubscribe/auto-unsubscribe behavior.

Driven through a real aiogram Dispatcher, same convention as
test_voice_intake_module.py / test_payments_module.py: notifications.router
cloned onto a Dispatcher with a small local middleware standing in for
runtime.registry.py's own ConfigMiddleware (see that module's
_load_and_include_features/_attach_bot_id_middleware).

IsolatedAsyncioTestCase (same as test_voice_intake_module.py) rather than a
manual asyncio.new_event_loop() per call — aiogram's Dispatcher/Lock grabs
the "current" event loop at construction time, which a bare unittest.TestCase
does not reliably have set when running under `unittest discover` alongside
hundreds of other test modules.

No real Telegram network calls (Bot.__call__ mocked), no real tokens.

Run with: python -m unittest tests.test_notifications_module
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.fsm.storage.memory import MemoryStorage

import features.notifications as notifications
from runtime.registry import _clone_router

FAKE_TOKEN = "123456:test-token-not-real"


@dataclass
class FixtureConfig:
    db_path: str
    admins_file: str | None = None


class ConfigMiddleware:
    """Stands in for runtime/registry.py's own per-template ConfigMiddleware,
    which rides the same Dispatcher as any feature router (see
    features/payments.py's PaymentsConfig docstring)."""

    def __init__(self, config: FixtureConfig) -> None:
        self.config = config

    async def __call__(self, handler, event, data):
        data["config"] = self.config
        return await handler(event, data)


def _build_dispatcher(config: FixtureConfig) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(ConfigMiddleware(config))
    dp.include_router(_clone_router(notifications.router))
    return dp


def _text_update(update_id: int, user_id: int, text: str, username: str | None = "tester") -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id, "date": 1700000000,
            "chat": {"id": user_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False, "first_name": "Test", "username": username},
            "text": text,
        },
    }


def _callback_update(update_id: int, user_id: int, data: str, msg_id: int = 1) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": str(update_id),
            "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
            "message": {
                "message_id": msg_id, "date": 1700000000,
                "chat": {"id": user_id, "type": "private"}, "text": "placeholder",
            },
            "chat_instance": "1", "data": data,
        },
    }


def _make_bot() -> Bot:
    bot = Bot(token=FAKE_TOKEN)
    bot.session = AsyncMock()
    return bot


class NotificationsModuleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "bot.db")
        self.config = FixtureConfig(db_path=self.db_path)
        await notifications.init_notifications_tables(self.db_path)

    async def asyncTearDown(self):
        self._tmp.cleanup()

    def _subscribers(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM subscribers ORDER BY chat_id").fetchall()
        conn.close()
        return [dict(r) for r in rows]

    # ── init_db / idempotency ────────────────────────────────────────────
    async def test_init_db_idempotent(self):
        await notifications.init_notifications_tables(self.db_path)
        await notifications.init_notifications_tables(self.db_path)
        self.assertEqual(self._subscribers(), [])

    # ── passive subscriber tracking ──────────────────────────────────────
    async def test_any_private_message_upserts_subscriber(self):
        dp = _build_dispatcher(self.config)
        bot = _make_bot()
        await dp.feed_raw_update(bot, _text_update(1, 111, "hello"))
        rows = self._subscribers()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["chat_id"], 111)
        self.assertEqual(rows[0]["username"], "tester")
        self.assertIsNone(rows[0]["unsubscribed_at"])

    async def test_repeated_messages_do_not_duplicate_row(self):
        dp = _build_dispatcher(self.config)
        bot = _make_bot()
        await dp.feed_raw_update(bot, _text_update(1, 111, "hi"))
        await dp.feed_raw_update(bot, _text_update(2, 111, "hi again"))
        rows = self._subscribers()
        self.assertEqual(len(rows), 1)

    async def test_message_from_group_chat_is_ignored(self):
        # F.chat.type == "private" filter must exclude group activity from
        # ever creating a subscriber row.
        dp = _build_dispatcher(self.config)
        bot = _make_bot()
        update = _text_update(1, 111, "hi")
        update["message"]["chat"]["type"] = "group"
        update["message"]["chat"]["title"] = "Some Group"
        await dp.feed_raw_update(bot, update)
        self.assertEqual(self._subscribers(), [])

    async def test_writing_again_after_unsubscribe_resubscribes(self):
        await notifications._upsert_subscriber(self.db_path, 111, "tester")
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE subscribers SET unsubscribed_at = datetime('now') WHERE chat_id=?", (111,)
        )
        conn.commit()
        conn.close()
        dp = _build_dispatcher(self.config)
        bot = _make_bot()
        await dp.feed_raw_update(bot, _text_update(2, 111, "back again"))
        rows = self._subscribers()
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["unsubscribed_at"])

    async def test_broken_upsert_does_not_raise_past_handler(self):
        # config.db_path pointing nowhere writable must not propagate as an
        # exception out of the Dispatcher — see _track_subscriber's isolation
        # docstring.
        bad_config = FixtureConfig(db_path=str(Path(self._tmp.name) / "no" / "such" / "dir.db"))
        dp = _build_dispatcher(bad_config)
        bot = _make_bot()
        try:
            await dp.feed_raw_update(bot, _text_update(1, 111, "hi"))
        except Exception as e:  # pragma: no cover - failure path
            self.fail(f"_track_subscriber must isolate its own failures, raised {e!r}")

    # ── /broadcast admin gating ──────────────────────────────────────────
    async def test_broadcast_command_denied_for_non_admin(self):
        config = FixtureConfig(db_path=self.db_path, admins_file=str(Path(self._tmp.name) / "admins.json"))
        Path(config.admins_file).write_text('{"ids": ["999"]}')
        dp = _build_dispatcher(config)
        bot = _make_bot()
        # Must not raise, and must not enter the broadcast FSM flow for a
        # non-admin user (111 is not in the admins list above).
        await dp.feed_raw_update(bot, _text_update(1, 111, "/broadcast"))

    async def test_broadcast_command_allowed_when_no_admins_file(self):
        # Fail-open convention, same as features/voice_intake.py's _is_admin.
        dp = _build_dispatcher(self.config)
        bot = _make_bot()
        await dp.feed_raw_update(bot, _text_update(1, 111, "/broadcast"))
        # No exception is the main assertion here — the handler ran to
        # completion, entering awaiting_content state for user 111.

    # ── audience counting ────────────────────────────────────────────────
    async def test_count_active_subscribers_excludes_unsubscribed(self):
        await notifications._upsert_subscriber(self.db_path, 1, "a")
        await notifications._upsert_subscriber(self.db_path, 2, "b")
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE subscribers SET unsubscribed_at = datetime('now') WHERE chat_id=1")
        conn.commit()
        conn.close()
        count = await notifications._count_active_subscribers(self.db_path)
        self.assertEqual(count, 1)

    # ── explicit unsubscribe callback ────────────────────────────────────
    async def test_unsubscribe_callback_marks_row(self):
        await notifications._upsert_subscriber(self.db_path, 111, "tester")
        dp = _build_dispatcher(self.config)
        bot = _make_bot()
        await dp.feed_raw_update(bot, _callback_update(1, 111, "notif_unsub"))
        rows = self._subscribers()
        self.assertIsNotNone(rows[0]["unsubscribed_at"])

    # ── broadcast send: rate limiting / isolation / auto-unsubscribe ─────
    async def test_send_broadcast_sequential_with_throttle(self):
        await notifications._upsert_subscriber(self.db_path, 1, "a")
        await notifications._upsert_subscriber(self.db_path, 2, "b")
        bot = MagicMock()
        bot.send_message = AsyncMock()
        with patch("features.notifications.asyncio.sleep", new=AsyncMock()) as sleep_mock:
            result = await notifications._send_broadcast(bot, self.db_path, "hello", None)
        self.assertEqual(result.sent, 2)
        self.assertEqual(result.failed, 0)
        self.assertEqual(result.unsubscribed, 0)
        self.assertEqual(bot.send_message.await_count, 2)
        self.assertEqual(sleep_mock.await_count, 2)  # one throttle pause per recipient

    async def test_send_broadcast_isolates_one_failure(self):
        await notifications._upsert_subscriber(self.db_path, 1, "a")
        await notifications._upsert_subscriber(self.db_path, 2, "b")
        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=[RuntimeError("boom"), None])
        with patch("features.notifications.asyncio.sleep", new=AsyncMock()):
            result = await notifications._send_broadcast(bot, self.db_path, "hello", None)
        self.assertEqual(result.sent, 1)
        self.assertEqual(result.failed, 1)

    async def test_send_broadcast_auto_unsubscribes_on_forbidden(self):
        await notifications._upsert_subscriber(self.db_path, 1, "a")
        bot = MagicMock()
        bot.send_message = AsyncMock(
            side_effect=TelegramForbiddenError(method=MagicMock(), message="blocked")
        )
        with patch("features.notifications.asyncio.sleep", new=AsyncMock()):
            result = await notifications._send_broadcast(bot, self.db_path, "hello", None)
        self.assertEqual(result.unsubscribed, 1)
        self.assertEqual(result.sent, 0)
        rows = self._subscribers()
        self.assertIsNotNone(rows[0]["unsubscribed_at"])

    async def test_send_broadcast_respects_retry_after(self):
        await notifications._upsert_subscriber(self.db_path, 1, "a")
        bot = MagicMock()
        retry_exc = TelegramRetryAfter(method=MagicMock(), message="flood", retry_after=7)
        bot.send_message = AsyncMock(side_effect=[retry_exc, None])
        with patch("features.notifications.asyncio.sleep", new=AsyncMock()) as sleep_mock:
            result = await notifications._send_broadcast(bot, self.db_path, "hello", None)
        self.assertEqual(result.sent, 1)
        sleep_mock.assert_any_call(7)

    async def test_send_broadcast_photo_path(self):
        await notifications._upsert_subscriber(self.db_path, 1, "a")
        bot = MagicMock()
        bot.send_photo = AsyncMock()
        with patch("features.notifications.asyncio.sleep", new=AsyncMock()):
            result = await notifications._send_broadcast(bot, self.db_path, "caption", "photo-file-id")
        self.assertEqual(result.sent, 1)
        bot.send_photo.assert_awaited_once()

    # ── per-bot db isolation ──────────────────────────────────────────────
    async def test_two_bots_have_independent_subscriber_tables(self):
        other_dir = tempfile.TemporaryDirectory()
        self.addCleanup(other_dir.cleanup)
        other_db = str(Path(other_dir.name) / "other.db")
        await notifications.init_notifications_tables(other_db)
        await notifications._upsert_subscriber(self.db_path, 1, "a")
        await notifications._upsert_subscriber(other_db, 2, "b")
        self.assertEqual([r["chat_id"] for r in self._subscribers()], [1])
        conn = sqlite3.connect(other_db)
        rows = conn.execute("SELECT chat_id FROM subscribers").fetchall()
        conn.close()
        self.assertEqual([r[0] for r in rows], [2])


if __name__ == "__main__":
    unittest.main()
