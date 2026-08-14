"""event_rsvp template — capacity/waitlist correctness, race-safety on
concurrent registration near-full-capacity, and waitlist auto-promotion on
cancellation.

No real Telegram network calls, no real tokens.

Run with: python -m unittest tests.test_event_rsvp
"""

from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from runtime.registry import get_template_router
from templates import event_rsvp

FAKE_TOKEN = "123456:test-token-not-real"
ADMIN_ID = 999
GUEST_A = 111
GUEST_B = 222


def _text_update(update_id: int, user_id: int, text: str) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id, "date": 1700000000,
            "chat": {"id": user_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
            "text": text,
        },
    }


def _callback_update(update_id: int, user_id: int, data: str) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": str(update_id),
            "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
            "message": {
                "message_id": update_id, "date": 1700000000,
                "chat": {"id": user_id, "type": "private"}, "text": "placeholder",
            },
            "chat_instance": "1", "data": data,
        },
    }


def _build_bot_dispatcher(config: event_rsvp.EventRsvpConfig) -> tuple[Bot, Dispatcher]:
    bot = Bot(token=FAKE_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(event_rsvp.ConfigMiddleware(config))
    dp.include_router(get_template_router("event_rsvp"))
    return bot, dp


async def _setup_event(config: event_rsvp.EventRsvpConfig, admin_id: int, capacity: int, start_update_id: int, dp, bot) -> int:
    """Drives the admin setup flow: title -> skip description -> capacity.
    Returns next free update_id. (Date is left unset — not needed by any
    capacity/waitlist test.)"""
    uid = start_update_id
    await dp.feed_webhook_update(bot, _callback_update(uid, admin_id, "setup_title")); uid += 1
    await dp.feed_webhook_update(bot, _text_update(uid, admin_id, "Test Event")); uid += 1
    await dp.feed_webhook_update(bot, _callback_update(uid, admin_id, "setup_desc_skip")); uid += 1
    await dp.feed_webhook_update(bot, _callback_update(uid, admin_id, "setup_capacity")); uid += 1
    await dp.feed_webhook_update(bot, _text_update(uid, admin_id, str(capacity))); uid += 1
    return uid


async def _register_via_flow(
    dp: Dispatcher, bot: Bot, guest_id: int, guests: int, name: str, phone: str, start_update_id: int,
) -> int:
    """Drives the guest RSVP flow: rsvp_new -> guests count -> name -> phone.
    Returns next free update_id."""
    uid = start_update_id
    await dp.feed_webhook_update(bot, _callback_update(uid, guest_id, "rsvp_new")); uid += 1
    await dp.feed_webhook_update(bot, _callback_update(uid, guest_id, f"rsvp_guests:{guests}")); uid += 1
    await dp.feed_webhook_update(bot, _text_update(uid, guest_id, name)); uid += 1
    await dp.feed_webhook_update(bot, _text_update(uid, guest_id, phone)); uid += 1
    return uid


class EventRsvpIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config_a = event_rsvp.config_from_bot_row(
            {"bot_id": 801, "name": "event_rsvp_bot_a", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        self.config_b = event_rsvp.config_from_bot_row(
            {"bot_id": 802, "name": "event_rsvp_bot_b", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await event_rsvp.init_db(self.config_a.db_path)
        await event_rsvp.init_db(self.config_b.db_path)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_configs_point_to_different_files(self):
        self.assertNotEqual(self.config_a.db_path, self.config_b.db_path)

    async def test_two_bots_same_admin_rsvps_not_mixed(self):
        bot_a, dp_a = _build_bot_dispatcher(self.config_a)
        bot_b, dp_b = _build_bot_dispatcher(self.config_b)
        await dp_a.feed_webhook_update(bot_a, _text_update(1, ADMIN_ID, "/start"))
        await dp_b.feed_webhook_update(bot_b, _text_update(1, ADMIN_ID, "/start"))
        uid = await _setup_event(self.config_a, ADMIN_ID, 10, 10, dp_a, bot_a)
        await _setup_event(self.config_b, ADMIN_ID, 10, 10, dp_b, bot_b)

        await _register_via_flow(dp_a, bot_a, GUEST_A, 2, "Alice", "111", uid)

        conn_a = sqlite3.connect(self.config_a.db_path)
        count_a = conn_a.execute("SELECT COUNT(*) FROM rsvps").fetchone()[0]
        conn_a.close()
        conn_b = sqlite3.connect(self.config_b.db_path)
        count_b = conn_b.execute("SELECT COUNT(*) FROM rsvps").fetchone()[0]
        conn_b.close()

        self.assertEqual(count_a, 1)
        self.assertEqual(count_b, 0)


class EventRsvpCapacityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = event_rsvp.config_from_bot_row(
            {"bot_id": 803, "name": "event_rsvp_capacity_bot", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await event_rsvp.init_db(self.config.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)
        await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/start"))

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_registration_within_capacity_is_confirmed(self):
        uid = await _setup_event(self.config, ADMIN_ID, 5, 10, self.dp, self.bot)
        await _register_via_flow(self.dp, self.bot, GUEST_A, 3, "Alice", "111", uid)
        conn = sqlite3.connect(self.config.db_path)
        status = conn.execute("SELECT status FROM rsvps WHERE client_user_id=?", (GUEST_A,)).fetchone()[0]
        conn.close()
        self.assertEqual(status, "confirmed")

    async def test_registration_exceeding_capacity_goes_to_waitlist(self):
        uid = await _setup_event(self.config, ADMIN_ID, 3, 10, self.dp, self.bot)
        uid = await _register_via_flow(self.dp, self.bot, GUEST_A, 3, "Alice", "111", uid)
        await _register_via_flow(self.dp, self.bot, GUEST_B, 1, "Bob", "222", uid)
        conn = sqlite3.connect(self.config.db_path)
        status_a = conn.execute("SELECT status FROM rsvps WHERE client_user_id=?", (GUEST_A,)).fetchone()[0]
        status_b = conn.execute("SELECT status FROM rsvps WHERE client_user_id=?", (GUEST_B,)).fetchone()[0]
        conn.close()
        self.assertEqual(status_a, "confirmed")
        self.assertEqual(status_b, "waitlist")

    async def test_no_capacity_configured_always_confirms(self):
        # setup_title only, capacity left unset (NULL) — no cap means never waitlisted.
        uid = 10
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, ADMIN_ID, "setup_title")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _text_update(uid, ADMIN_ID, "Open Event")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, ADMIN_ID, "setup_desc_skip")); uid += 1
        await _register_via_flow(self.dp, self.bot, GUEST_A, 5, "Alice", "111", uid)
        conn = sqlite3.connect(self.config.db_path)
        status = conn.execute("SELECT status FROM rsvps WHERE client_user_id=?", (GUEST_A,)).fetchone()[0]
        conn.close()
        self.assertEqual(status, "confirmed")


class EventRsvpWaitlistPromotionTests(unittest.IsolatedAsyncioTestCase):
    """Cancelling a confirmed RSVP must auto-promote the oldest waitlist
    entry that now fits, and notify that guest."""

    async def asyncSetUp(self):
        self._bot_call = AsyncMock(return_value=MagicMock())
        self._bot_call_patcher = patch.object(Bot, "__call__", new=self._bot_call)
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = event_rsvp.config_from_bot_row(
            {"bot_id": 804, "name": "event_rsvp_promo_bot", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await event_rsvp.init_db(self.config.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)
        await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/start"))

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    def _promotion_notifications_to(self, chat_id: int) -> list[str]:
        """Counts only the proactive "seat freed up" push (bot.send_message
        from _promote_from_waitlist), not every SendMessage/EditMessageText
        that chat_id's OWN FSM-driven actions triggered as bot replies —
        those share the same chat_id since guest and bot chat 1:1."""
        texts = []
        for call in self._bot_call.call_args_list:
            request = call.args[0] if call.args else None
            text = getattr(request, "text", None)
            cid = getattr(request, "chat_id", None)
            if text and cid == chat_id and "Место освободилось" in text:
                texts.append(text)
        return texts

    async def test_cancelling_confirmed_promotes_oldest_waitlisted_guest(self):
        uid = await _setup_event(self.config, ADMIN_ID, 2, 10, self.dp, self.bot)
        uid = await _register_via_flow(self.dp, self.bot, GUEST_A, 2, "Alice", "111", uid)
        uid = await _register_via_flow(self.dp, self.bot, GUEST_B, 1, "Bob", "222", uid)

        conn = sqlite3.connect(self.config.db_path)
        rsvp_a_id, status_a = conn.execute(
            "SELECT id, status FROM rsvps WHERE client_user_id=?", (GUEST_A,)
        ).fetchone()
        status_b = conn.execute("SELECT status FROM rsvps WHERE client_user_id=?", (GUEST_B,)).fetchone()[0]
        conn.close()
        self.assertEqual(status_a, "confirmed")
        self.assertEqual(status_b, "waitlist")

        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, GUEST_A, f"rsvp_cancel:{rsvp_a_id}"))

        conn = sqlite3.connect(self.config.db_path)
        status_a_after = conn.execute("SELECT status FROM rsvps WHERE client_user_id=?", (GUEST_A,)).fetchone()[0]
        status_b_after = conn.execute("SELECT status FROM rsvps WHERE client_user_id=?", (GUEST_B,)).fetchone()[0]
        conn.close()
        self.assertEqual(status_a_after, "cancelled")
        self.assertEqual(status_b_after, "confirmed")
        self.assertEqual(len(self._promotion_notifications_to(GUEST_B)), 1)

    async def test_cancelling_waitlisted_entry_does_not_promote_anyone(self):
        uid = await _setup_event(self.config, ADMIN_ID, 2, 10, self.dp, self.bot)
        uid = await _register_via_flow(self.dp, self.bot, GUEST_A, 2, "Alice", "111", uid)
        uid = await _register_via_flow(self.dp, self.bot, GUEST_B, 1, "Bob", "222", uid)

        conn = sqlite3.connect(self.config.db_path)
        rsvp_b_id = conn.execute("SELECT id FROM rsvps WHERE client_user_id=?", (GUEST_B,)).fetchone()[0]
        conn.close()

        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, GUEST_B, f"rsvp_cancel:{rsvp_b_id}"))

        conn = sqlite3.connect(self.config.db_path)
        status_a = conn.execute("SELECT status FROM rsvps WHERE client_user_id=?", (GUEST_A,)).fetchone()[0]
        status_b = conn.execute("SELECT status FROM rsvps WHERE client_user_id=?", (GUEST_B,)).fetchone()[0]
        conn.close()
        self.assertEqual(status_a, "confirmed")
        self.assertEqual(status_b, "cancelled")

    async def test_promotion_skips_waitlisted_party_too_big_for_freed_seats(self):
        # capacity 3: Alice confirms 3 (full). Bob(2) and Carol(1) waitlist in
        # that order. Alice cancels 3 seats -> Bob(2) fits and is promoted,
        # NOT skipped in favor of smaller Carol — FIFO order is preserved as
        # long as the oldest candidate fits.
        uid = await _setup_event(self.config, ADMIN_ID, 3, 10, self.dp, self.bot)
        uid = await _register_via_flow(self.dp, self.bot, GUEST_A, 3, "Alice", "111", uid)
        uid = await _register_via_flow(self.dp, self.bot, GUEST_B, 2, "Bob", "222", uid)
        CAROL_ID = 333
        uid = await _register_via_flow(self.dp, self.bot, CAROL_ID, 1, "Carol", "333", uid)

        conn = sqlite3.connect(self.config.db_path)
        rsvp_a_id = conn.execute("SELECT id FROM rsvps WHERE client_user_id=?", (GUEST_A,)).fetchone()[0]
        conn.close()

        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, GUEST_A, f"rsvp_cancel:{rsvp_a_id}"))

        conn = sqlite3.connect(self.config.db_path)
        status_b = conn.execute("SELECT status FROM rsvps WHERE client_user_id=?", (GUEST_B,)).fetchone()[0]
        status_c = conn.execute("SELECT status FROM rsvps WHERE client_user_id=?", (CAROL_ID,)).fetchone()[0]
        conn.close()
        self.assertEqual(status_b, "confirmed")
        self.assertEqual(status_c, "waitlist")

    async def test_double_tap_cancel_promotes_only_once(self):
        uid = await _setup_event(self.config, ADMIN_ID, 1, 10, self.dp, self.bot)
        uid = await _register_via_flow(self.dp, self.bot, GUEST_A, 1, "Alice", "111", uid)
        uid = await _register_via_flow(self.dp, self.bot, GUEST_B, 1, "Bob", "222", uid)

        conn = sqlite3.connect(self.config.db_path)
        rsvp_a_id = conn.execute("SELECT id FROM rsvps WHERE client_user_id=?", (GUEST_A,)).fetchone()[0]
        conn.close()

        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, GUEST_A, f"rsvp_cancel:{rsvp_a_id}")); uid += 1
        # Stale/duplicate re-tap of the same cancel button.
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, GUEST_A, f"rsvp_cancel:{rsvp_a_id}")); uid += 1

        self.assertEqual(len(self._promotion_notifications_to(GUEST_B)), 1, "double-tap cancel promoted the waitlist guest twice")
        conn = sqlite3.connect(self.config.db_path)
        confirmed_count = conn.execute("SELECT COUNT(*) FROM rsvps WHERE status='confirmed'").fetchone()[0]
        conn.close()
        self.assertEqual(confirmed_count, 1, "double-tap cancel resulted in more than one confirmed rsvp")

    async def test_ownership_check_rejects_cancelling_another_guests_rsvp(self):
        uid = await _setup_event(self.config, ADMIN_ID, 5, 10, self.dp, self.bot)
        uid = await _register_via_flow(self.dp, self.bot, GUEST_A, 1, "Alice", "111", uid)
        conn = sqlite3.connect(self.config.db_path)
        rsvp_a_id = conn.execute("SELECT id FROM rsvps WHERE client_user_id=?", (GUEST_A,)).fetchone()[0]
        conn.close()

        # GUEST_B tries to cancel GUEST_A's rsvp by guessing the id.
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, GUEST_B, f"rsvp_cancel:{rsvp_a_id}"))

        conn = sqlite3.connect(self.config.db_path)
        status_a = conn.execute("SELECT status FROM rsvps WHERE id=?", (rsvp_a_id,)).fetchone()[0]
        conn.close()
        self.assertEqual(status_a, "confirmed", "another guest was able to cancel someone else's rsvp")


class EventRsvpRaceSafetyTests(unittest.IsolatedAsyncioTestCase):
    """Two guests racing for the last remaining seat(s), driven through real
    asyncio concurrency (asyncio.gather), not sequential calls — this is the
    scenario the compare-and-swap / single-connection-transaction design in
    _confirm_or_waitlist() exists to protect against."""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = event_rsvp.config_from_bot_row(
            {"bot_id": 805, "name": "event_rsvp_race_bot", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await event_rsvp.init_db(self.config.db_path)
        async with aiosqlite.connect(self.config.db_path) as db:
            await db.execute(
                "INSERT INTO event_details (id, title, capacity) VALUES (1, 'Race Event', 1)"
            )
            await db.commit()

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def test_concurrent_registration_for_last_seat_confirms_exactly_one(self):
        # Capacity is 1, empty. Two guests register concurrently for 1 seat
        # each. Exactly one must land confirmed, the other waitlist — never
        # both confirmed (would overbook) and never both waitlisted (would
        # strand a free seat).
        results = await asyncio.gather(
            event_rsvp._confirm_or_waitlist(self.config.db_path, GUEST_A, "Alice", "111", 1),
            event_rsvp._confirm_or_waitlist(self.config.db_path, GUEST_B, "Bob", "222", 1),
        )
        self.assertEqual(sorted(results), ["confirmed", "waitlist"])

        conn = sqlite3.connect(self.config.db_path)
        confirmed_count = conn.execute("SELECT COUNT(*) FROM rsvps WHERE status='confirmed'").fetchone()[0]
        total_confirmed_guests = conn.execute(
            "SELECT COALESCE(SUM(guests_count),0) FROM rsvps WHERE status='confirmed'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(confirmed_count, 1)
        self.assertEqual(total_confirmed_guests, 1)

    async def test_many_concurrent_registrations_never_overbook(self):
        # Capacity 1 stays fixed from asyncSetUp; fire 10 concurrent
        # registrants at it and assert the confirmed total never exceeds
        # capacity no matter how the event loop interleaves them.
        guest_ids = list(range(1000, 1010))
        await asyncio.gather(*[
            event_rsvp._confirm_or_waitlist(self.config.db_path, gid, f"Guest{gid}", str(gid), 1)
            for gid in guest_ids
        ])
        conn = sqlite3.connect(self.config.db_path)
        confirmed_count = conn.execute("SELECT COUNT(*) FROM rsvps WHERE status='confirmed'").fetchone()[0]
        waitlist_count = conn.execute("SELECT COUNT(*) FROM rsvps WHERE status='waitlist'").fetchone()[0]
        total_rows = conn.execute("SELECT COUNT(*) FROM rsvps").fetchone()[0]
        conn.close()
        self.assertEqual(confirmed_count, 1, "capacity was overbooked under concurrent registration")
        self.assertEqual(waitlist_count, 9)
        self.assertEqual(total_rows, 10)

    async def test_concurrent_registrations_near_full_capacity_never_overbook(self):
        # Capacity 5, already 4 seats occupied by a pre-existing confirmed
        # rsvp (near-full, matching the ТЗ's literal "почти заполненной
        # вместимости" scenario) — 6 more guests race for the last 1 seat,
        # each requesting 1 seat.
        async with aiosqlite.connect(self.config.db_path) as db:
            await db.execute("UPDATE event_details SET capacity=5 WHERE id=1")
            await db.execute(
                "INSERT INTO rsvps (client_user_id, client_name, client_phone, guests_count, status) "
                "VALUES (0, 'Preexisting', '000', 4, 'confirmed')"
            )
            await db.commit()

        guest_ids = list(range(2000, 2006))
        await asyncio.gather(*[
            event_rsvp._confirm_or_waitlist(self.config.db_path, gid, f"Guest{gid}", str(gid), 1)
            for gid in guest_ids
        ])
        conn = sqlite3.connect(self.config.db_path)
        total_confirmed_guests = conn.execute(
            "SELECT COALESCE(SUM(guests_count),0) FROM rsvps WHERE status='confirmed'"
        ).fetchone()[0]
        conn.close()
        self.assertLessEqual(total_confirmed_guests, 5, "concurrent registration near-full capacity overbooked")
        self.assertEqual(total_confirmed_guests, 5, "the one remaining seat should have been claimed by exactly one guest")


if __name__ == "__main__":
    unittest.main()
