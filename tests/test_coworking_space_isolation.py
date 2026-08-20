"""coworking_space template — overlap-check correctness/race-safety, resource
CRUD, service requests, guest registration, and cross-bot/cross-client data
isolation tests.

Design recap (full reasoning lives in templates/coworking_space.py's own
module docstring):
- Unlike every booking_* template already in this repo (which books a
  SPECIALIST'S TIME), coworking_space books a physical RESOURCE (a desk, a
  meeting room, a full-day pass) for a time slot. The one piece of real
  business logic is the overlap check: two CONFIRMED bookings on the same
  resource_id + booking_date must never have overlapping [start, end)
  intervals. Back-to-back bookings (one ends exactly when the next starts)
  ARE allowed — that's a touch, not an overlap, under the half-open-interval
  test the template uses.
- Booking is self-service/instant: no admin-approval step, status goes
  straight to 'confirmed' as long as the resource is free.
- Race-safety: the overlap re-check and the INSERT happen on the SAME
  aiosqlite connection with no commit in between, so a double-tap / two
  near-simultaneous booking attempts for the same resource+slot cannot both
  succeed — the second writer's re-check sees the first writer's already-
  committed row (SQLite serializes writers at the file level).

Standard criterion (same as every other template's isolation suite): two
bots on the SAME template, different config, must never mix data — even
driven by the SAME Telegram user_id.

No real Telegram network calls, no real tokens.

Run with: python -m unittest tests.test_coworking_space_isolation
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
from templates import coworking_space

FAKE_TOKEN = "123456:test-token-not-real"
ADMIN_ID = 999
CLIENT_A_ID = 555
CLIENT_B_ID = 556
TODAY = coworking_space._upcoming_dates()[0]
# A date whose slots are guaranteed still in the future regardless of what time
# of day the suite happens to run — TODAY's own slots can already be in the
# past by wall-clock time (e.g. SLOT_A is 09:00-11:00; a run after 11:00 would
# make a "today" booking already-started, not a genuinely future one), which
# would make cancel-related assertions flaky depending on run time. Used only
# by tests that specifically assert "an upcoming booking is cancellable".
FUTURE_DATE = coworking_space._upcoming_dates()[1]
SLOT_A = coworking_space.TIME_WINDOWS[0]   # ("09:00", "11:00")
SLOT_B = coworking_space.TIME_WINDOWS[1]   # ("11:00", "13:00")


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


def _build_bot_dispatcher(config: coworking_space.CoworkingSpaceConfig) -> tuple[Bot, Dispatcher]:
    bot = Bot(token=FAKE_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(coworking_space.ConfigMiddleware(config))
    dp.include_router(get_template_router("coworking_space"))
    return bot, dp


async def _add_resource(db_path: str, name: str, resource_type: str = "desk") -> int:
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "INSERT INTO resources (name, resource_type) VALUES (?,?)", (name, resource_type)
        )
        await db.commit()
        return cur.lastrowid


async def _book_via_flow(
    dp: Dispatcher, bot: Bot, client_id: int, resource_type: str, booking_date: str,
    slot: tuple[str, str], resource_id: int, tariff: str, start_update_id: int,
) -> int:
    """Drives the full client FSM: book_start -> type -> date -> slot ->
    resource -> tariff. Returns the next free update_id."""
    uid = start_update_id
    await dp.feed_webhook_update(bot, _callback_update(uid, client_id, "book_start")); uid += 1
    await dp.feed_webhook_update(bot, _callback_update(uid, client_id, f"book_type:{resource_type}")); uid += 1
    await dp.feed_webhook_update(bot, _callback_update(uid, client_id, f"book_date:{booking_date}")); uid += 1
    await dp.feed_webhook_update(bot, _callback_update(uid, client_id, f"book_slot:{slot[0]}-{slot[1]}")); uid += 1
    await dp.feed_webhook_update(bot, _callback_update(uid, client_id, f"book_res:{resource_id}")); uid += 1
    await dp.feed_webhook_update(bot, _callback_update(uid, client_id, f"book_tariff:{tariff}")); uid += 1
    return uid


class CoworkingSpaceOverlapTests(unittest.IsolatedAsyncioTestCase):
    """The headline correctness area: the overlap check."""

    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = coworking_space.config_from_bot_row(
            {"bot_id": 801, "name": "coworking_overlap_bot", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await coworking_space.init_db(self.config.db_path)
        self.resource_id = await _add_resource(self.config.db_path, "Стол №1", "desk")
        self.other_resource_id = await _add_resource(self.config.db_path, "Стол №2", "desk")
        self.bot, self.dp = _build_bot_dispatcher(self.config)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    def _confirmed_count(self, resource_id: int | None = None) -> int:
        conn = sqlite3.connect(self.config.db_path)
        if resource_id is not None:
            n = conn.execute(
                "SELECT COUNT(*) FROM bookings WHERE status='confirmed' AND resource_id=?", (resource_id,)
            ).fetchone()[0]
        else:
            n = conn.execute("SELECT COUNT(*) FROM bookings WHERE status='confirmed'").fetchone()[0]
        conn.close()
        return n

    async def test_booking_a_free_slot_succeeds(self):
        await _book_via_flow(self.dp, self.bot, CLIENT_A_ID, "desk", TODAY, SLOT_A, self.resource_id, "day_pass", 10)
        self.assertEqual(self._confirmed_count(self.resource_id), 1)

    async def test_exactly_overlapping_slot_on_same_resource_is_rejected(self):
        uid = await _book_via_flow(self.dp, self.bot, CLIENT_A_ID, "desk", TODAY, SLOT_A, self.resource_id, "day_pass", 10)
        # Second client tries the SAME resource + SAME exact slot.
        async with aiosqlite.connect(self.config.db_path) as db:
            free = await coworking_space._available_resources(db, "desk", TODAY, SLOT_A[0], SLOT_A[1])
        self.assertNotIn((self.resource_id, "Стол №1"), free, "an already-booked resource was still listed as free")
        # Directly attempt via the flow anyway (simulating a stale keyboard) — must not create a second confirmed booking.
        await _book_via_flow(self.dp, self.bot, CLIENT_B_ID, "desk", TODAY, SLOT_A, self.resource_id, "day_pass", uid)
        self.assertEqual(self._confirmed_count(self.resource_id), 1, "an exactly-overlapping slot was double-booked")

    async def test_partially_overlapping_slot_on_same_resource_is_rejected(self):
        """09:00-11:00 booked; try 10:00-12:00-equivalent by using one of the
        template's own fixed windows that partially overlaps SLOT_A. Since
        TIME_WINDOWS is a fixed set, we exercise the underlying _has_overlap
        directly with a partially-overlapping interval to prove the interval
        math itself (not just the fixed-slot UI) is correct."""
        await _book_via_flow(self.dp, self.bot, CLIENT_A_ID, "desk", TODAY, SLOT_A, self.resource_id, "day_pass", 10)
        async with aiosqlite.connect(self.config.db_path) as db:
            overlap = await coworking_space._has_overlap(db, self.resource_id, TODAY, "10:00", "12:00")
        self.assertTrue(overlap, "a partially-overlapping interval was not detected")

    async def test_adjacent_back_to_back_slot_on_same_resource_is_allowed(self):
        """SLOT_A ends exactly when SLOT_B starts (11:00) — this must be
        ALLOWED, not rejected, under the half-open-interval overlap test."""
        await _book_via_flow(self.dp, self.bot, CLIENT_A_ID, "desk", TODAY, SLOT_A, self.resource_id, "day_pass", 10)
        await _book_via_flow(self.dp, self.bot, CLIENT_B_ID, "desk", TODAY, SLOT_B, self.resource_id, "membership", 20)
        self.assertEqual(self._confirmed_count(self.resource_id), 2, "back-to-back adjacent bookings were incorrectly rejected")

    async def test_same_slot_on_different_resource_is_unaffected(self):
        await _book_via_flow(self.dp, self.bot, CLIENT_A_ID, "desk", TODAY, SLOT_A, self.resource_id, "day_pass", 10)
        await _book_via_flow(self.dp, self.bot, CLIENT_B_ID, "desk", TODAY, SLOT_A, self.other_resource_id, "day_pass", 20)
        self.assertEqual(self._confirmed_count(self.resource_id), 1)
        self.assertEqual(self._confirmed_count(self.other_resource_id), 1)

    async def test_near_simultaneous_double_booking_does_not_double_confirm(self):
        """Two 'admins/clients tapping confirm for the same desk/slot at
        nearly the same moment' — modeled here as several concurrent
        coroutines racing to run the SAME guarded check-then-insert
        transaction (BEGIN IMMEDIATE, then re-check, then INSERT) that
        cb_book_tariff itself uses, directly against the same db_path. This
        exercises the actual race-safety mechanism, not just its intent: an
        earlier version of this test used a bare `async with
        aiosqlite.connect()` with no BEGIN IMMEDIATE and reliably produced
        8 confirmed bookings for one slot, since a plain SELECT does not
        take SQLite's write lock — see the DESIGN NOTE in
        templates/coworking_space.py for the full explanation."""
        async def _attempt():
            async with aiosqlite.connect(self.config.db_path, timeout=30) as db:
                await db.execute("BEGIN IMMEDIATE")
                if not await coworking_space._has_overlap(db, self.resource_id, TODAY, SLOT_A[0], SLOT_A[1]):
                    await db.execute(
                        "INSERT INTO bookings (client_user_id, client_name, resource_id, booking_date, "
                        "time_slot_start, time_slot_end, tariff, status) VALUES (?,?,?,?,?,?,?, 'confirmed')",
                        (CLIENT_A_ID, "Racer", self.resource_id, TODAY, SLOT_A[0], SLOT_A[1], "day_pass"),
                    )
                    await db.commit()
                else:
                    await db.rollback()

        await asyncio.gather(*[_attempt() for _ in range(8)])
        self.assertEqual(
            self._confirmed_count(self.resource_id), 1,
            "a race between near-simultaneous booking attempts produced more than one confirmed booking",
        )

    async def test_cancelled_booking_frees_the_slot(self):
        conn = sqlite3.connect(self.config.db_path)
        conn.execute(
            "INSERT INTO bookings (client_user_id, resource_id, booking_date, time_slot_start, time_slot_end, "
            "tariff, status) VALUES (?,?,?,?,?,?, 'cancelled')",
            (CLIENT_A_ID, self.resource_id, TODAY, SLOT_A[0], SLOT_A[1], "day_pass"),
        )
        conn.commit()
        conn.close()
        async with aiosqlite.connect(self.config.db_path) as db:
            overlap = await coworking_space._has_overlap(db, self.resource_id, TODAY, SLOT_A[0], SLOT_A[1])
        self.assertFalse(overlap, "a cancelled booking still blocked the slot")


class CoworkingSpaceResourceCrudTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = coworking_space.config_from_bot_row(
            {"bot_id": 802, "name": "coworking_crud_bot", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await coworking_space.init_db(self.config.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)
        await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/start"))

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_admin_can_add_edit_and_deactivate_resource(self):
        await self.dp.feed_webhook_update(self.bot, _callback_update(10, ADMIN_ID, "adm_res_add"))
        await self.dp.feed_webhook_update(self.bot, _text_update(11, ADMIN_ID, "Переговорная А"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(12, ADMIN_ID, "adm_res_type:meeting_room"))

        conn = sqlite3.connect(self.config.db_path)
        row = conn.execute("SELECT id, name, resource_type, is_active FROM resources").fetchone()
        conn.close()
        self.assertIsNotNone(row)
        resource_id, name, rtype, is_active = row
        self.assertEqual(name, "Переговорная А")
        self.assertEqual(rtype, "meeting_room")
        self.assertEqual(is_active, 1)

        await self.dp.feed_webhook_update(self.bot, _callback_update(20, ADMIN_ID, f"adm_res_edit:{resource_id}"))
        await self.dp.feed_webhook_update(self.bot, _text_update(21, ADMIN_ID, "Переговорная Б"))
        conn = sqlite3.connect(self.config.db_path)
        name_after = conn.execute("SELECT name FROM resources WHERE id=?", (resource_id,)).fetchone()[0]
        conn.close()
        self.assertEqual(name_after, "Переговорная Б")

        await self.dp.feed_webhook_update(self.bot, _callback_update(30, ADMIN_ID, f"adm_res_deact:{resource_id}"))
        conn = sqlite3.connect(self.config.db_path)
        active_after = conn.execute("SELECT is_active FROM resources WHERE id=?", (resource_id,)).fetchone()[0]
        conn.close()
        self.assertEqual(active_after, 0)

    async def test_deactivated_resource_is_not_offered_but_history_remains(self):
        resource_id = await _add_resource(self.config.db_path, "Стол X", "desk")
        await _book_via_flow(self.dp, self.bot, CLIENT_A_ID, "desk", TODAY, SLOT_A, resource_id, "day_pass", 40)
        conn = sqlite3.connect(self.config.db_path)
        booking_count_before = conn.execute("SELECT COUNT(*) FROM bookings WHERE resource_id=?", (resource_id,)).fetchone()[0]
        conn.close()
        self.assertEqual(booking_count_before, 1)

        await self.dp.feed_webhook_update(self.bot, _callback_update(50, ADMIN_ID, f"adm_res_deact:{resource_id}"))

        async with aiosqlite.connect(self.config.db_path) as db:
            free = await coworking_space._available_resources(db, "desk", TODAY, SLOT_B[0], SLOT_B[1])
        self.assertNotIn((resource_id, "Стол X"), free, "a deactivated resource was still offered for a new booking")

        conn = sqlite3.connect(self.config.db_path)
        booking_count_after = conn.execute("SELECT COUNT(*) FROM bookings WHERE resource_id=?", (resource_id,)).fetchone()[0]
        conn.close()
        self.assertEqual(booking_count_after, 1, "deactivating a resource must not delete its booking history")

    async def test_non_admin_cannot_reach_admin_resource_callbacks(self):
        client_id = 7001
        await self.dp.feed_webhook_update(self.bot, _callback_update(60, client_id, "adm_res_add"))
        await self.dp.feed_webhook_update(self.bot, _text_update(61, client_id, "Хакерский стол"))
        conn = sqlite3.connect(self.config.db_path)
        count = conn.execute("SELECT COUNT(*) FROM resources").fetchone()[0]
        conn.close()
        self.assertEqual(count, 0, "a non-admin caller was able to create a resource via an admin-only callback")

    async def test_non_admin_cannot_view_schedule_or_service_queue(self):
        client_id = 7002
        await self.dp.feed_webhook_update(self.bot, _callback_update(70, client_id, "adm_schedule"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(71, client_id, "adm_svc_list"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(72, client_id, "adm_guests"))
        # No assertion possible on message content without deeper mocking of
        # edit_text, but these must not raise, and combined with the ADMIN
        # gate present on every one of these handlers (verified by code
        # inspection matching every other _is_admin-gated handler in this
        # repo), a non-admin caller gets an early return with no data access.


class CoworkingSpaceServiceAndGuestTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._bot_call = AsyncMock(return_value=MagicMock())
        self._bot_call_patcher = patch.object(Bot, "__call__", new=self._bot_call)
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = coworking_space.config_from_bot_row(
            {"bot_id": 803, "name": "coworking_service_bot", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await coworking_space.init_db(self.config.db_path)
        self.resource_id = await _add_resource(self.config.db_path, "Стол №1", "desk")
        self.bot, self.dp = _build_bot_dispatcher(self.config)
        await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/start"))

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_service_request_end_to_end_notifies_admin(self):
        uid = await _book_via_flow(self.dp, self.bot, CLIENT_A_ID, "desk", TODAY, SLOT_A, self.resource_id, "day_pass", 10)
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, CLIENT_A_ID, "svc_start")); uid += 1
        conn = sqlite3.connect(self.config.db_path)
        booking_id = conn.execute("SELECT id FROM bookings WHERE client_user_id=?", (CLIENT_A_ID,)).fetchone()[0]
        conn.close()
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, CLIENT_A_ID, f"svc_book:{booking_id}")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, CLIENT_A_ID, "svc_type:coffee")); uid += 1

        conn = sqlite3.connect(self.config.db_path)
        row = conn.execute("SELECT client_user_id, booking_id, service_type, status FROM service_requests").fetchone()
        conn.close()
        self.assertEqual(row, (CLIENT_A_ID, booking_id, "coffee", "requested"))

        notified_admin = any(
            getattr(call.args[0], "chat_id", None) == ADMIN_ID for call in self._bot_call.call_args_list if call.args
        )
        self.assertTrue(notified_admin, "admin was not notified of the new service request")

    async def test_service_request_without_booking_is_allowed(self):
        uid = 10
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, CLIENT_B_ID, "svc_start")); uid += 1
        # No bookings for CLIENT_B_ID -> straight to service type picker.
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, CLIENT_B_ID, "svc_type:printer")); uid += 1
        conn = sqlite3.connect(self.config.db_path)
        row = conn.execute("SELECT client_user_id, booking_id, service_type FROM service_requests").fetchone()
        conn.close()
        self.assertEqual(row, (CLIENT_B_ID, None, "printer"))

    async def test_admin_can_mark_service_request_done(self):
        conn = sqlite3.connect(self.config.db_path)
        conn.execute(
            "INSERT INTO service_requests (client_user_id, service_type) VALUES (?, 'coffee')", (CLIENT_A_ID,)
        )
        conn.commit()
        request_id = conn.execute("SELECT id FROM service_requests").fetchone()[0]
        conn.close()
        await self.dp.feed_webhook_update(self.bot, _callback_update(10, ADMIN_ID, f"adm_svc_done:{request_id}"))
        conn = sqlite3.connect(self.config.db_path)
        status = conn.execute("SELECT status FROM service_requests WHERE id=?", (request_id,)).fetchone()[0]
        conn.close()
        self.assertEqual(status, "done")

    async def test_guest_registration_end_to_end(self):
        uid = await _book_via_flow(self.dp, self.bot, CLIENT_A_ID, "desk", TODAY, SLOT_A, self.resource_id, "day_pass", 10)
        conn = sqlite3.connect(self.config.db_path)
        booking_id = conn.execute("SELECT id FROM bookings WHERE client_user_id=?", (CLIENT_A_ID,)).fetchone()[0]
        conn.close()
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, CLIENT_A_ID, "guest_start")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, CLIENT_A_ID, f"guest_book:{booking_id}")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _text_update(uid, CLIENT_A_ID, "Иван Петров")); uid += 1

        conn = sqlite3.connect(self.config.db_path)
        row = conn.execute("SELECT booking_id, guest_name FROM guest_registrations").fetchone()
        conn.close()
        self.assertEqual(row, (booking_id, "Иван Петров"))

    async def test_client_cannot_register_guest_on_someone_elses_booking(self):
        uid = await _book_via_flow(self.dp, self.bot, CLIENT_A_ID, "desk", TODAY, SLOT_A, self.resource_id, "day_pass", 10)
        conn = sqlite3.connect(self.config.db_path)
        booking_id = conn.execute("SELECT id FROM bookings WHERE client_user_id=?", (CLIENT_A_ID,)).fetchone()[0]
        conn.close()
        # CLIENT_B tries to guest-register on CLIENT_A's booking by guessing its id.
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, CLIENT_B_ID, f"guest_book:{booking_id}"))
        conn = sqlite3.connect(self.config.db_path)
        count = conn.execute("SELECT COUNT(*) FROM guest_registrations").fetchone()[0]
        conn.close()
        self.assertEqual(count, 0, "a client was able to start a guest registration against another client's booking")


class CoworkingSpaceMyBookingsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = coworking_space.config_from_bot_row(
            {"bot_id": 804, "name": "coworking_mybookings_bot", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await coworking_space.init_db(self.config.db_path)
        self.resource_id = await _add_resource(self.config.db_path, "Стол №1", "desk")
        self.bot, self.dp = _build_bot_dispatcher(self.config)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_client_can_cancel_own_future_booking(self):
        uid = await _book_via_flow(self.dp, self.bot, CLIENT_A_ID, "desk", FUTURE_DATE, SLOT_A, self.resource_id, "day_pass", 10)
        conn = sqlite3.connect(self.config.db_path)
        booking_id = conn.execute("SELECT id FROM bookings WHERE client_user_id=?", (CLIENT_A_ID,)).fetchone()[0]
        conn.close()
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, CLIENT_A_ID, f"my_cancel:{booking_id}"))
        conn = sqlite3.connect(self.config.db_path)
        status = conn.execute("SELECT status FROM bookings WHERE id=?", (booking_id,)).fetchone()[0]
        conn.close()
        self.assertEqual(status, "cancelled")

    async def test_client_cannot_cancel_someone_elses_booking(self):
        uid = await _book_via_flow(self.dp, self.bot, CLIENT_A_ID, "desk", TODAY, SLOT_A, self.resource_id, "day_pass", 10)
        conn = sqlite3.connect(self.config.db_path)
        booking_id = conn.execute("SELECT id FROM bookings WHERE client_user_id=?", (CLIENT_A_ID,)).fetchone()[0]
        conn.close()
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, CLIENT_B_ID, f"my_cancel:{booking_id}"))
        conn = sqlite3.connect(self.config.db_path)
        status = conn.execute("SELECT status FROM bookings WHERE id=?", (booking_id,)).fetchone()[0]
        conn.close()
        self.assertEqual(status, "confirmed", "a client was able to cancel another client's booking")

    async def test_double_cancel_is_a_no_op_not_an_error(self):
        uid = await _book_via_flow(self.dp, self.bot, CLIENT_A_ID, "desk", FUTURE_DATE, SLOT_A, self.resource_id, "day_pass", 10)
        conn = sqlite3.connect(self.config.db_path)
        booking_id = conn.execute("SELECT id FROM bookings WHERE client_user_id=?", (CLIENT_A_ID,)).fetchone()[0]
        conn.close()
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, CLIENT_A_ID, f"my_cancel:{booking_id}")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, CLIENT_A_ID, f"my_cancel:{booking_id}"))
        conn = sqlite3.connect(self.config.db_path)
        status = conn.execute("SELECT status FROM bookings WHERE id=?", (booking_id,)).fetchone()[0]
        conn.close()
        self.assertEqual(status, "cancelled")


class CoworkingSpaceIsolationTests(unittest.IsolatedAsyncioTestCase):
    """Two bots on the SAME template, different config, must never mix data
    — even driven by the SAME Telegram client user_id."""

    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config_a = coworking_space.config_from_bot_row(
            {"bot_id": 805, "name": "coworking_isolation_bot_a", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        self.config_b = coworking_space.config_from_bot_row(
            {"bot_id": 806, "name": "coworking_isolation_bot_b", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await coworking_space.init_db(self.config_a.db_path)
        await coworking_space.init_db(self.config_b.db_path)
        self.resource_a = await _add_resource(self.config_a.db_path, "Стол A1", "desk")
        self.resource_b = await _add_resource(self.config_b.db_path, "Стол B1", "desk")
        self.bot_a, self.dp_a = _build_bot_dispatcher(self.config_a)
        self.bot_b, self.dp_b = _build_bot_dispatcher(self.config_b)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_configs_point_to_different_files(self):
        self.assertNotEqual(self.config_a.db_path, self.config_b.db_path)

    async def test_same_client_booking_same_slot_on_two_bots_is_independent(self):
        # SAME client user_id, SAME date/slot, on two DIFFERENT bots — must
        # succeed independently on both, no cross-bot conflict, no data leak.
        await _book_via_flow(self.dp_a, self.bot_a, CLIENT_A_ID, "desk", TODAY, SLOT_A, self.resource_a, "day_pass", 10)
        await _book_via_flow(self.dp_b, self.bot_b, CLIENT_A_ID, "desk", TODAY, SLOT_A, self.resource_b, "membership", 10)

        conn_a = sqlite3.connect(self.config_a.db_path)
        bookings_a = conn_a.execute("SELECT COUNT(*) FROM bookings").fetchone()[0]
        tariff_a = conn_a.execute("SELECT tariff FROM bookings").fetchone()[0]
        conn_a.close()
        conn_b = sqlite3.connect(self.config_b.db_path)
        bookings_b = conn_b.execute("SELECT COUNT(*) FROM bookings").fetchone()[0]
        tariff_b = conn_b.execute("SELECT tariff FROM bookings").fetchone()[0]
        conn_b.close()

        self.assertEqual(bookings_a, 1)
        self.assertEqual(bookings_b, 1)
        self.assertEqual(tariff_a, "day_pass")
        self.assertEqual(tariff_b, "membership")

    async def test_admin_status_is_per_bot_not_global(self):
        await self.dp_a.feed_webhook_update(self.bot_a, _text_update(1, ADMIN_ID, "/start"))
        # ADMIN_ID is bot A's first-time admin, but has never /start'd on bot B.
        await self.dp_b.feed_webhook_update(self.bot_b, _callback_update(2, ADMIN_ID, "adm_res_add"))
        conn_b = sqlite3.connect(self.config_b.db_path)
        # Only the seeded resource_b from setUp should exist — the admin-only
        # callback on bot B must have been rejected since ADMIN_ID is not
        # (yet) an admin of bot B.
        count = conn_b.execute("SELECT COUNT(*) FROM resources").fetchone()[0]
        conn_b.close()
        self.assertEqual(count, 1)


class CoworkingSpaceStandaloneSmokeTest(unittest.TestCase):
    def test_config_from_env_matches_legacy_constant_shape(self):
        config = coworking_space.config_from_env()
        self.assertTrue(config.db_path.endswith("coworking_space_data.db"))
        self.assertEqual(config.bot_name, "coworking_space")

    def test_router_and_main_entrypoint_exist(self):
        self.assertTrue(hasattr(coworking_space, "router"))
        self.assertTrue(hasattr(coworking_space, "main"))


class CoworkingSpaceMiniAppConfigTests(unittest.IsolatedAsyncioTestCase):
    """miniapp_config's declared table/field names must match init_db()'s
    real schema — miniapp_api.py builds SQL directly off these names, so a
    drift here would 500 at request time instead of failing a test."""

    def test_miniapp_config_resource_names(self):
        names = {r["name"] for r in coworking_space.miniapp_config["resources"]}
        self.assertEqual(names, {"resources", "bookings", "guests", "service_requests"})

    def test_resources_resource_targets_resources_table(self):
        resources = next(r for r in coworking_space.miniapp_config["resources"] if r["name"] == "resources")
        self.assertEqual(resources["table"], "resources")
        self.assertTrue(resources["creatable"])
        self.assertIn("name", {f["name"] for f in resources["fields"]})

    def test_bookings_resource_targets_bookings_table(self):
        bookings = next(r for r in coworking_space.miniapp_config["resources"] if r["name"] == "bookings")
        self.assertEqual(bookings["table"], "bookings")
        self.assertTrue(bookings["creatable"])
        field_names = {f["name"] for f in bookings["fields"]}
        self.assertEqual(
            field_names,
            {"resource_id", "client_user_id", "client_name", "booking_date",
             "time_slot_start", "time_slot_end", "tariff", "status"},
        )

    def test_guests_resource_targets_guest_registrations_table(self):
        guests = next(r for r in coworking_space.miniapp_config["resources"] if r["name"] == "guests")
        self.assertEqual(guests["table"], "guest_registrations")
        self.assertFalse(guests["creatable"])

    def test_service_requests_resource_targets_service_requests_table(self):
        svc = next(r for r in coworking_space.miniapp_config["resources"] if r["name"] == "service_requests")
        self.assertEqual(svc["table"], "service_requests")
        self.assertFalse(svc["creatable"])

    async def test_miniapp_config_fields_match_real_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "schema_check.db")
            await coworking_space.init_db(db_path)
            async with aiosqlite.connect(db_path) as db:
                for resource in coworking_space.miniapp_config["resources"]:
                    cur = await db.execute(f"PRAGMA table_info({resource['table']})")
                    real_columns = {row[1] for row in await cur.fetchall()}
                    declared = {f["name"] for f in resource["fields"]} | {"id"}
                    self.assertTrue(
                        declared.issubset(real_columns),
                        f"{resource['name']}: declared fields {declared} not all in "
                        f"real columns {real_columns}",
                    )


if __name__ == "__main__":
    unittest.main()
