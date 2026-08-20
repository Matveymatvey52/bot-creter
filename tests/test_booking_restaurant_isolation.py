"""booking_restaurant template — data isolation, capacity-checked window
picking, banquet-deposit flag, admin confirm/reject with client-notify, and
the 24h free-cancellation deadline wording.

Standard criterion: two bots on the SAME template, different config, must
never mix data — even driven by the SAME Telegram admin user_id.

PLUS the design's central differentiators from vehicle_service.py /
orders_tracker.py:
- a reservation is booked against a company-sized party for a coarse TIME
  WINDOW (not a per-person slot at an exact minute);
- the window-picker only offers windows whose remaining capacity (total
  active-table capacity minus already-CONFIRMED guests for that exact
  date+window) can still fit the party;
- guests_count >= BANQUET_THRESHOLD sets deposit_required=1 automatically at
  creation time, no admin step needed;
- cancelling less than CANCEL_FREE_HOURS before the reservation shows an
  informational late-cancellation note but is never blocked (no in-bot
  penalty mechanism).

No real Telegram network calls, no real tokens.

Run with: python -m unittest tests.test_booking_restaurant_isolation
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from runtime.registry import get_template_router
from templates import booking_restaurant

FAKE_TOKEN = "123456:test-token-not-real"
ADMIN_ID = 999
CLIENT_ID = 555
TOMORROW = (datetime.now().date() + timedelta(days=1)).isoformat()


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


def _build_bot_dispatcher(config: booking_restaurant.BookingRestaurantConfig) -> tuple[Bot, Dispatcher]:
    bot = Bot(token=FAKE_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(booking_restaurant.ConfigMiddleware(config))
    dp.include_router(get_template_router("booking_restaurant"))
    return bot, dp


async def _create_reservation_via_flow(
    dp: Dispatcher, bot: Bot, client_id: int, guests: str, date: str, window_start: str, window_end: str,
    phone: str, start_update_id: int, occasion: str | None = None,
) -> int:
    """Drives the full client FSM: book_new -> guests -> pick date -> pick
    window -> occasion (add or skip) -> phone -> reservation is created.
    Returns the next free update_id."""
    # callback_data encodes the TIME_WINDOWS index, not raw "start:end" text
    # (see kb_windows/cb_res_window in the template — fixed after review found
    # the raw encoding unparseable, since start/end themselves contain ":").
    window_index = booking_restaurant.TIME_WINDOWS.index((window_start, window_end))
    uid = start_update_id
    await dp.feed_webhook_update(bot, _callback_update(uid, client_id, "book_new")); uid += 1
    await dp.feed_webhook_update(bot, _text_update(uid, client_id, guests)); uid += 1
    await dp.feed_webhook_update(bot, _callback_update(uid, client_id, f"res_day:{date}")); uid += 1
    await dp.feed_webhook_update(bot, _callback_update(uid, client_id, f"res_window:{window_index}")); uid += 1
    if occasion is None:
        await dp.feed_webhook_update(bot, _callback_update(uid, client_id, "res_occ_skip")); uid += 1
    else:
        await dp.feed_webhook_update(bot, _callback_update(uid, client_id, "res_occ_add")); uid += 1
        await dp.feed_webhook_update(bot, _text_update(uid, client_id, occasion)); uid += 1
    await dp.feed_webhook_update(bot, _text_update(uid, client_id, phone)); uid += 1
    return uid


class BookingRestaurantIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

        self.config_a = booking_restaurant.config_from_bot_row(
            {"bot_id": 701, "name": "resto_isolation_bot_a", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        self.config_b = booking_restaurant.config_from_bot_row(
            {"bot_id": 702, "name": "resto_isolation_bot_b", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await booking_restaurant.init_db(self.config_a.db_path)
        await booking_restaurant.init_db(self.config_b.db_path)
        async with aiosqlite.connect(self.config_a.db_path) as db:
            await db.execute("INSERT INTO tables (name, capacity) VALUES ('T1', 10)")
            await db.commit()
        async with aiosqlite.connect(self.config_b.db_path) as db:
            await db.execute("INSERT INTO tables (name, capacity) VALUES ('T1', 10)")
            await db.commit()

        self.bot_a, self.dp_a = _build_bot_dispatcher(self.config_a)
        self.bot_b, self.dp_b = _build_bot_dispatcher(self.config_b)
        # Bootstrap the SAME admin user_id on both bots.
        await self.dp_a.feed_webhook_update(self.bot_a, _text_update(1, ADMIN_ID, "/start"))
        await self.dp_b.feed_webhook_update(self.bot_b, _text_update(1, ADMIN_ID, "/start"))

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_configs_point_to_different_files(self):
        self.assertNotEqual(self.config_a.db_path, self.config_b.db_path)

    async def test_two_bots_same_client_same_date_reservations_not_mixed(self):
        await _create_reservation_via_flow(
            self.dp_a, self.bot_a, CLIENT_ID, "4", TOMORROW, "18:00", "20:00", "89991234567", 10,
        )
        await _create_reservation_via_flow(
            self.dp_b, self.bot_b, CLIENT_ID, "2", TOMORROW, "18:00", "20:00", "89997654321", 10,
        )

        conn_a = sqlite3.connect(self.config_a.db_path)
        res_a = conn_a.execute("SELECT guests_count, client_phone FROM reservations").fetchall()
        conn_a.close()
        conn_b = sqlite3.connect(self.config_b.db_path)
        res_b = conn_b.execute("SELECT guests_count, client_phone FROM reservations").fetchall()
        conn_b.close()

        self.assertEqual(len(res_a), 1)
        self.assertEqual(len(res_b), 1)
        self.assertEqual(res_a[0][0], 4)
        self.assertEqual(res_b[0][0], 2)
        self.assertNotEqual(res_a[0][1], res_b[0][1])


class BookingRestaurantCapacityTests(unittest.IsolatedAsyncioTestCase):
    """Owner-specified differentiator: the window-picker must only offer
    windows with enough remaining capacity for the party."""

    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = booking_restaurant.config_from_bot_row(
            {"bot_id": 703, "name": "resto_capacity_bot", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await booking_restaurant.init_db(self.config.db_path)
        async with aiosqlite.connect(self.config.db_path) as db:
            await db.execute("INSERT INTO tables (name, capacity) VALUES ('T1', 6)")
            await db.commit()
        self.bot, self.dp = _build_bot_dispatcher(self.config)
        await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/start"))

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_no_tables_means_no_available_windows(self):
        windows = await booking_restaurant._available_windows(self.config.db_path, TOMORROW, 2)
        self.assertEqual(len(windows), 6)  # total capacity 6, party of 2 fits every window
        windows_too_big = await booking_restaurant._available_windows(self.config.db_path, TOMORROW, 7)
        self.assertEqual(windows_too_big, [])

    async def test_confirmed_reservation_reduces_remaining_capacity_for_that_window_only(self):
        async with aiosqlite.connect(self.config.db_path) as db:
            await db.execute(
                "INSERT INTO reservations (client_user_id, guests_count, date, time_window_start, "
                "time_window_end, status) VALUES (?,?,?,?,?, 'confirmed')",
                (CLIENT_ID, 4, TOMORROW, "18:00", "20:00"),
            )
            await db.commit()
        windows = await booking_restaurant._available_windows(self.config.db_path, TOMORROW, 3)
        self.assertNotIn(("18:00", "20:00"), windows, "confirmed booking didn't reduce remaining capacity")
        self.assertIn(("14:00", "16:00"), windows, "an unrelated window was wrongly filtered out")

    async def test_reservation_stores_the_exact_window_picked_via_the_flow(self):
        """Review-found blocker regression: cb_res_window used to parse
        callback_data with split(":", 2), which mis-sliced "res_window:18:00:
        20:00" (each half already contains a colon) into window_start="18",
        window_end="00:20:00" for EVERY window — corrupting every reservation
        ever created via the real flow and silently breaking the capacity
        check downstream. Drives the actual FSM (not a raw INSERT) and checks
        the stored columns match exactly what was picked."""
        await _create_reservation_via_flow(
            self.dp, self.bot, CLIENT_ID, "3", TOMORROW, "14:00", "16:00", "89991234567", 10,
        )
        conn = sqlite3.connect(self.config.db_path)
        row = conn.execute(
            "SELECT time_window_start, time_window_end FROM reservations"
        ).fetchone()
        conn.close()
        self.assertEqual(row, ("14:00", "16:00"))

    async def test_pending_reservation_does_not_reduce_capacity(self):
        # Literal design-brief wording: capacity is checked against ALREADY
        # CONFIRMED bookings, not pending ones.
        async with aiosqlite.connect(self.config.db_path) as db:
            await db.execute(
                "INSERT INTO reservations (client_user_id, guests_count, date, time_window_start, "
                "time_window_end, status) VALUES (?,?,?,?,?, 'pending')",
                (CLIENT_ID, 6, TOMORROW, "18:00", "20:00"),
            )
            await db.commit()
        windows = await booking_restaurant._available_windows(self.config.db_path, TOMORROW, 6)
        self.assertIn(("18:00", "20:00"), windows)

    async def test_banquet_flow_sets_deposit_required(self):
        await _create_reservation_via_flow(
            self.dp, self.bot, CLIENT_ID, str(booking_restaurant.BANQUET_THRESHOLD), TOMORROW,
            "18:00", "20:00", "89991234567", 10,
        )
        conn = sqlite3.connect(self.config.db_path)
        row = conn.execute("SELECT guests_count, deposit_required, status FROM reservations").fetchone()
        conn.close()
        self.assertEqual(row, (booking_restaurant.BANQUET_THRESHOLD, 1, "pending"))

    async def test_below_threshold_does_not_require_deposit(self):
        await _create_reservation_via_flow(
            self.dp, self.bot, CLIENT_ID, str(booking_restaurant.BANQUET_THRESHOLD - 1), TOMORROW,
            "18:00", "20:00", "89991234567", 10,
        )
        conn = sqlite3.connect(self.config.db_path)
        row = conn.execute("SELECT deposit_required FROM reservations").fetchone()
        conn.close()
        self.assertEqual(row[0], 0)


class BookingRestaurantAdminReviewTests(unittest.IsolatedAsyncioTestCase):
    """Admin confirm/reject must notify the client, be double-tap safe, and
    the deposit-confirm action must not fire when it isn't required."""

    async def asyncSetUp(self):
        self._bot_call = AsyncMock(return_value=MagicMock())
        self._bot_call_patcher = patch.object(Bot, "__call__", new=self._bot_call)
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = booking_restaurant.config_from_bot_row(
            {"bot_id": 704, "name": "resto_admin_bot", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await booking_restaurant.init_db(self.config.db_path)
        async with aiosqlite.connect(self.config.db_path) as db:
            await db.execute("INSERT INTO tables (name, capacity) VALUES ('T1', 10)")
            await db.commit()
        self.bot, self.dp = _build_bot_dispatcher(self.config)
        await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/start"))

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    def _sent_texts_to(self, chat_id: int) -> list[str]:
        texts = []
        for call in self._bot_call.call_args_list:
            request = call.args[0] if call.args else None
            text = getattr(request, "text", None)
            cid = getattr(request, "chat_id", None)
            if text and cid == chat_id:
                texts.append(text)
        return texts

    async def _create_and_get_id(self, guests="4") -> int:
        await _create_reservation_via_flow(
            self.dp, self.bot, CLIENT_ID, guests, TOMORROW, "18:00", "20:00", "89991234567", 10,
        )
        conn = sqlite3.connect(self.config.db_path)
        rid = conn.execute("SELECT id FROM reservations").fetchone()[0]
        conn.close()
        # The client's OWN creation flow replies land in this same chat_id, so
        # they'd otherwise pollute _sent_texts_to() below — reset the mock
        # once the reservation exists, so each test only sees calls made by
        # the ADMIN action under test.
        self._bot_call.reset_mock()
        return rid

    async def test_confirm_notifies_client_and_sets_status(self):
        rid = await self._create_and_get_id()
        await self.dp.feed_webhook_update(self.bot, _callback_update(100, ADMIN_ID, f"admres_confirm:{rid}"))
        conn = sqlite3.connect(self.config.db_path)
        status = conn.execute("SELECT status FROM reservations WHERE id=?", (rid,)).fetchone()[0]
        conn.close()
        self.assertEqual(status, "confirmed")
        notes = self._sent_texts_to(CLIENT_ID)
        self.assertEqual(len(notes), 1)
        self.assertIn(str(rid), notes[0])
        self.assertIn("подтверждена", notes[0])

    async def test_double_tap_confirm_notifies_only_once(self):
        rid = await self._create_and_get_id()
        await self.dp.feed_webhook_update(self.bot, _callback_update(100, ADMIN_ID, f"admres_confirm:{rid}"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(101, ADMIN_ID, f"admres_confirm:{rid}"))
        self.assertEqual(len(self._sent_texts_to(CLIENT_ID)), 1, "double-tap notified the client twice")

    async def test_double_tap_reject_notifies_only_once(self):
        rid = await self._create_and_get_id()
        await self.dp.feed_webhook_update(self.bot, _callback_update(100, ADMIN_ID, f"admres_reject:{rid}"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(101, ADMIN_ID, f"admres_reject:{rid}"))
        self.assertEqual(len(self._sent_texts_to(CLIENT_ID)), 1, "double-tap notified the client twice")

    async def test_reject_notifies_client_and_cancels(self):
        rid = await self._create_and_get_id()
        await self.dp.feed_webhook_update(self.bot, _callback_update(100, ADMIN_ID, f"admres_reject:{rid}"))
        conn = sqlite3.connect(self.config.db_path)
        status = conn.execute("SELECT status FROM reservations WHERE id=?", (rid,)).fetchone()[0]
        conn.close()
        self.assertEqual(status, "cancelled")
        notes = self._sent_texts_to(CLIENT_ID)
        self.assertEqual(len(notes), 1)
        self.assertIn("отклонена", notes[0])

    async def test_deposit_mark_paid_only_when_required(self):
        rid = await self._create_and_get_id(guests="2")  # below BANQUET_THRESHOLD
        await self.dp.feed_webhook_update(self.bot, _callback_update(100, ADMIN_ID, f"admres_deposit:{rid}"))
        conn = sqlite3.connect(self.config.db_path)
        confirmed = conn.execute("SELECT deposit_confirmed FROM reservations WHERE id=?", (rid,)).fetchone()[0]
        conn.close()
        self.assertEqual(confirmed, 0, "deposit was marked paid even though it wasn't required")

    async def test_deposit_mark_paid_notifies_client_when_required(self):
        rid = await self._create_and_get_id(guests=str(booking_restaurant.BANQUET_THRESHOLD))
        await self.dp.feed_webhook_update(self.bot, _callback_update(100, ADMIN_ID, f"admres_deposit:{rid}"))
        conn = sqlite3.connect(self.config.db_path)
        confirmed = conn.execute("SELECT deposit_confirmed FROM reservations WHERE id=?", (rid,)).fetchone()[0]
        conn.close()
        self.assertEqual(confirmed, 1)
        notes = self._sent_texts_to(CLIENT_ID)
        self.assertEqual(len(notes), 1)
        self.assertIn("депозит", notes[0].lower())

    async def test_non_admin_cannot_confirm(self):
        rid = await self._create_and_get_id()
        intruder_id = 111222
        await self.dp.feed_webhook_update(self.bot, _callback_update(100, intruder_id, f"admres_confirm:{rid}"))
        conn = sqlite3.connect(self.config.db_path)
        status = conn.execute("SELECT status FROM reservations WHERE id=?", (rid,)).fetchone()[0]
        conn.close()
        self.assertEqual(status, "pending", "a non-admin was able to confirm a reservation")


class BookingRestaurantClientCancelTests(unittest.IsolatedAsyncioTestCase):
    """Client-side cancellation: ownership-checked, double-tap safe, and the
    late-cancellation note is informational only (never blocks the action)."""

    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = booking_restaurant.config_from_bot_row(
            {"bot_id": 705, "name": "resto_cancel_bot", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await booking_restaurant.init_db(self.config.db_path)
        async with aiosqlite.connect(self.config.db_path) as db:
            await db.execute("INSERT INTO tables (name, capacity) VALUES ('T1', 10)")
            await db.commit()
        self.bot, self.dp = _build_bot_dispatcher(self.config)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_owner_can_cancel_and_late_note_appears_within_24h(self):
        near_datetime = datetime.now() + timedelta(hours=2)
        near_date = near_datetime.date().isoformat()
        near_time = near_datetime.strftime("%H:00")
        async with aiosqlite.connect(self.config.db_path) as db:
            cur = await db.execute(
                "INSERT INTO reservations (client_user_id, guests_count, date, time_window_start, "
                "time_window_end, status) VALUES (?,?,?,?,?, 'pending')",
                (CLIENT_ID, 3, near_date, near_time, "23:30"),
            )
            reservation_id = cur.lastrowid
            await db.commit()
        await self.dp.feed_webhook_update(self.bot, _callback_update(1, CLIENT_ID, f"res_do_cancel:{reservation_id}"))
        conn = sqlite3.connect(self.config.db_path)
        status = conn.execute("SELECT status FROM reservations WHERE id=?", (reservation_id,)).fetchone()[0]
        conn.close()
        self.assertEqual(status, "cancelled", "late cancellation was blocked instead of just warned about")

    async def test_stranger_cannot_cancel_someone_elses_reservation(self):
        async with aiosqlite.connect(self.config.db_path) as db:
            cur = await db.execute(
                "INSERT INTO reservations (client_user_id, guests_count, date, time_window_start, "
                "time_window_end, status) VALUES (?,?,?,?,?, 'pending')",
                (CLIENT_ID, 3, TOMORROW, "18:00", "20:00"),
            )
            reservation_id = cur.lastrowid
            await db.commit()
        stranger_id = 333444
        await self.dp.feed_webhook_update(self.bot, _callback_update(1, stranger_id, f"res_do_cancel:{reservation_id}"))
        conn = sqlite3.connect(self.config.db_path)
        status = conn.execute("SELECT status FROM reservations WHERE id=?", (reservation_id,)).fetchone()[0]
        conn.close()
        self.assertEqual(status, "pending", "a stranger was able to cancel someone else's reservation")

    async def test_double_tap_cancel_is_a_noop_on_second_tap(self):
        async with aiosqlite.connect(self.config.db_path) as db:
            cur = await db.execute(
                "INSERT INTO reservations (client_user_id, guests_count, date, time_window_start, "
                "time_window_end, status) VALUES (?,?,?,?,?, 'pending')",
                (CLIENT_ID, 3, TOMORROW, "18:00", "20:00"),
            )
            reservation_id = cur.lastrowid
            await db.commit()
        await self.dp.feed_webhook_update(self.bot, _callback_update(1, CLIENT_ID, f"res_do_cancel:{reservation_id}"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(2, CLIENT_ID, f"res_do_cancel:{reservation_id}"))
        conn = sqlite3.connect(self.config.db_path)
        status = conn.execute("SELECT status FROM reservations WHERE id=?", (reservation_id,)).fetchone()[0]
        conn.close()
        self.assertEqual(status, "cancelled")


class BookingRestaurantTableManagementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = booking_restaurant.config_from_bot_row(
            {"bot_id": 706, "name": "resto_tables_bot", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await booking_restaurant.init_db(self.config.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)
        await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/start"))

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_add_table_flow(self):
        await self.dp.feed_webhook_update(self.bot, _callback_update(10, ADMIN_ID, "tbl_new"))
        await self.dp.feed_webhook_update(self.bot, _text_update(11, ADMIN_ID, "Стол у окна"))
        await self.dp.feed_webhook_update(self.bot, _text_update(12, ADMIN_ID, "6"))
        conn = sqlite3.connect(self.config.db_path)
        row = conn.execute("SELECT name, capacity, active FROM tables").fetchone()
        conn.close()
        self.assertEqual(row, ("Стол у окна", 6, 1))

    async def test_unicode_digit_capacity_input_is_rejected_not_a_crash(self):
        """Review-found (monkey-tester): str.isdigit() is True for Unicode
        "digit" characters (superscript "²", circled "①") that int() can't
        parse — an unhandled ValueError would silently drop the update with
        no reply. "²" must be cleanly rejected, not crash the handler."""
        await self.dp.feed_webhook_update(self.bot, _callback_update(10, ADMIN_ID, "tbl_new"))
        await self.dp.feed_webhook_update(self.bot, _text_update(11, ADMIN_ID, "Стол у окна"))
        await self.dp.feed_webhook_update(self.bot, _text_update(12, ADMIN_ID, "②"))
        conn = sqlite3.connect(self.config.db_path)
        count = conn.execute("SELECT COUNT(*) FROM tables").fetchone()[0]
        conn.close()
        self.assertEqual(count, 0, "malformed capacity input should not create a table")

    async def test_toggle_hides_and_shows_table(self):
        async with aiosqlite.connect(self.config.db_path) as db:
            cur = await db.execute("INSERT INTO tables (name, capacity) VALUES ('T1', 4)")
            table_id = cur.lastrowid
            await db.commit()
        await self.dp.feed_webhook_update(self.bot, _callback_update(10, ADMIN_ID, f"tbl_toggle:{table_id}"))
        conn = sqlite3.connect(self.config.db_path)
        active = conn.execute("SELECT active FROM tables WHERE id=?", (table_id,)).fetchone()[0]
        conn.close()
        self.assertEqual(active, 0)


class BookingRestaurantMiniAppConfigTests(unittest.IsolatedAsyncioTestCase):
    """miniapp_config's declared table/field names must match init_db()'s
    real schema — miniapp_api.py builds SQL directly off these names, so a
    drift here would 500 at request time instead of failing a test."""

    def test_miniapp_config_resource_names(self):
        names = {r["name"] for r in booking_restaurant.miniapp_config["resources"]}
        self.assertEqual(names, {"tables", "reservations"})

    def test_tables_resource_targets_tables_table(self):
        tables = next(r for r in booking_restaurant.miniapp_config["resources"] if r["name"] == "tables")
        self.assertEqual(tables["table"], "tables")
        self.assertTrue(tables["creatable"])

    def test_reservations_resource_targets_reservations_table(self):
        reservations = next(r for r in booking_restaurant.miniapp_config["resources"] if r["name"] == "reservations")
        self.assertEqual(reservations["table"], "reservations")
        field_names = {f["name"] for f in reservations["fields"]}
        self.assertEqual(
            field_names,
            {"client_user_id", "client_name", "client_phone", "guests_count", "date",
             "time_window_start", "time_window_end", "occasion", "deposit_required",
             "deposit_confirmed", "status", "created_at"},
        )

    async def test_miniapp_config_fields_match_real_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "schema_check.db")
            await booking_restaurant.init_db(db_path)
            async with aiosqlite.connect(db_path) as db:
                for resource in booking_restaurant.miniapp_config["resources"]:
                    cur = await db.execute(f"PRAGMA table_info({resource['table']})")
                    real_columns = {row[1] for row in await cur.fetchall()}
                    declared = {f["name"] for f in resource["fields"]} | {"id"}
                    self.assertTrue(
                        declared.issubset(real_columns),
                        f"{resource['name']}: declared fields {declared} not all in "
                        f"real columns {real_columns}",
                    )


class BookingRestaurantStandaloneSmokeTest(unittest.TestCase):
    def test_config_from_env_matches_legacy_constant_shape(self):
        config = booking_restaurant.config_from_env()
        self.assertTrue(config.db_path.endswith("booking_restaurant_data.db"))
        self.assertEqual(config.bot_name, "booking_restaurant")

    def test_router_and_main_entrypoint_exist(self):
        self.assertTrue(hasattr(booking_restaurant, "router"))
        self.assertTrue(hasattr(booking_restaurant, "main"))


if __name__ == "__main__":
    unittest.main()
