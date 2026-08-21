"""car_rental template — booking flow, date-overlap rejection, concurrent-
booking race-safety, and multi-bot isolation tests.

Standard criterion: two bots on the SAME template, different config, must
never mix data — even driven by the SAME Telegram admin/client user_id.

PLUS the design's central differentiator from booking_fitness.py (time-slot
capacity) and booking_restaurant.py (fixed time windows): a car_rental
booking is a date RANGE against a specific rental_items row, and the overlap
check is inclusive-range overlap, not slot/capacity matching.

No real Telegram network calls, no real tokens.

Run with: python -m unittest tests.test_car_rental
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from runtime.registry import get_template_router
from templates import car_rental

FAKE_TOKEN = "123456:test-token-not-real"
ADMIN_ID = 999
CLIENT_ID = 555
CLIENT_ID_2 = 556
PHONE = "89991234567"  # normalizes to "+7 (999) 123-45-67"


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


def _contact_update(update_id: int, user_id: int, phone_number: str, contact_user_id: int) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id, "date": 1700000000,
            "chat": {"id": user_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
            "contact": {"phone_number": phone_number, "first_name": "Test", "user_id": contact_user_id},
        },
    }


def _build_bot_dispatcher(config: car_rental.CarRentalConfig) -> tuple[Bot, Dispatcher]:
    bot = Bot(token=FAKE_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(car_rental.ConfigMiddleware(config))
    dp.include_router(get_template_router("car_rental"))
    return bot, dp


async def _drive_booking_flow(
    dp: Dispatcher, bot: Bot, client_id: int, item_id: int, start_date: str, end_date: str,
    start_update_id: int, phone: str = PHONE,
) -> int:
    """Drives the full client FSM: cli_book -> pick item -> start date -> end
    date -> contact. Returns the next free update_id."""
    uid = start_update_id
    await dp.feed_webhook_update(bot, _callback_update(uid, client_id, "cli_book")); uid += 1
    await dp.feed_webhook_update(bot, _callback_update(uid, client_id, f"cli_pick:{item_id}")); uid += 1
    await dp.feed_webhook_update(bot, _text_update(uid, client_id, start_date)); uid += 1
    await dp.feed_webhook_update(bot, _text_update(uid, client_id, end_date)); uid += 1
    await dp.feed_webhook_update(bot, _contact_update(uid, client_id, phone, contact_user_id=client_id)); uid += 1
    return uid


class CarRentalIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

        self.config_a = car_rental.config_from_bot_row(
            {"bot_id": 801, "name": "car_rental_isolation_bot_a", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        self.config_b = car_rental.config_from_bot_row(
            {"bot_id": 802, "name": "car_rental_isolation_bot_b", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await car_rental.init_db(self.config_a.db_path)
        await car_rental.init_db(self.config_b.db_path)

        self.bot_a, self.dp_a = _build_bot_dispatcher(self.config_a)
        self.bot_b, self.dp_b = _build_bot_dispatcher(self.config_b)
        # Bootstrap the SAME admin user_id on both bots.
        await self.dp_a.feed_webhook_update(self.bot_a, _text_update(1, ADMIN_ID, "/start"))
        await self.dp_b.feed_webhook_update(self.bot_b, _text_update(1, ADMIN_ID, "/start"))

        # One rental_items row per bot, same name, DIFFERENT ids (independent
        # AUTOINCREMENT sequences) — the point being that a booking on bot A's
        # item must never be visible from bot B's db_path.
        async with aiosqlite.connect(self.config_a.db_path) as db:
            cur = await db.execute("INSERT INTO rental_items (name, price_per_day) VALUES ('Kia Rio', 2000)")
            self.item_a = cur.lastrowid
            await db.commit()
        async with aiosqlite.connect(self.config_b.db_path) as db:
            cur = await db.execute("INSERT INTO rental_items (name, price_per_day) VALUES ('Kia Rio', 2000)")
            self.item_b = cur.lastrowid
            await db.commit()

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_configs_point_to_different_files(self):
        self.assertNotEqual(self.config_a.db_path, self.config_b.db_path)

    async def test_two_bots_same_client_same_item_bookings_not_mixed(self):
        await _drive_booking_flow(self.dp_a, self.bot_a, CLIENT_ID, self.item_a, "2026-09-01", "2026-09-05", 10)
        await _drive_booking_flow(self.dp_b, self.bot_b, CLIENT_ID, self.item_b, "2026-10-01", "2026-10-05", 10)

        conn_a = sqlite3.connect(self.config_a.db_path)
        bookings_a = conn_a.execute("SELECT COUNT(*) FROM rental_bookings").fetchone()[0]
        row_a = conn_a.execute("SELECT start_date, end_date FROM rental_bookings").fetchone()
        conn_a.close()
        conn_b = sqlite3.connect(self.config_b.db_path)
        bookings_b = conn_b.execute("SELECT COUNT(*) FROM rental_bookings").fetchone()[0]
        row_b = conn_b.execute("SELECT start_date, end_date FROM rental_bookings").fetchone()
        conn_b.close()

        self.assertEqual(bookings_a, 1)
        self.assertEqual(bookings_b, 1)
        self.assertEqual(row_a, ("2026-09-01", "2026-09-05"))
        self.assertEqual(row_b, ("2026-10-01", "2026-10-05"))

    async def test_items_isolated_across_bots(self):
        conn_a = sqlite3.connect(self.config_a.db_path)
        items_a = conn_a.execute("SELECT COUNT(*) FROM rental_items").fetchone()[0]
        conn_a.close()
        conn_b = sqlite3.connect(self.config_b.db_path)
        items_b = conn_b.execute("SELECT COUNT(*) FROM rental_items").fetchone()[0]
        conn_b.close()
        self.assertEqual(items_a, 1)
        self.assertEqual(items_b, 1)


class CarRentalBookingFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._bot_call = AsyncMock(return_value=MagicMock())
        self._bot_call_patcher = patch.object(Bot, "__call__", new=self._bot_call)
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = car_rental.config_from_bot_row(
            {"bot_id": 803, "name": "car_rental_flow_bot", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await car_rental.init_db(self.config.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)
        await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/start"))
        async with aiosqlite.connect(self.config.db_path) as db:
            cur = await db.execute("INSERT INTO rental_items (name, price_per_day) VALUES ('Kia Rio', 2000)")
            self.item_id = cur.lastrowid
            await db.commit()

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

    async def test_booking_creates_pending_row_and_notifies_admin(self):
        await _drive_booking_flow(self.dp, self.bot, CLIENT_ID, self.item_id, "2026-09-01", "2026-09-05", 10)

        conn = sqlite3.connect(self.config.db_path)
        row = conn.execute(
            "SELECT status, client_user_id, client_phone, start_date, end_date FROM rental_bookings"
        ).fetchone()
        conn.close()
        self.assertEqual(row[0], "pending")
        self.assertEqual(row[1], CLIENT_ID)
        self.assertEqual(row[2], "+7 (999) 123-45-67")
        self.assertEqual((row[3], row[4]), ("2026-09-01", "2026-09-05"))

        admin_notes = self._sent_texts_to(ADMIN_ID)
        self.assertTrue(any("Новая заявка" in t for t in admin_notes))

    async def test_second_overlapping_booking_is_rejected(self):
        await _drive_booking_flow(self.dp, self.bot, CLIENT_ID, self.item_id, "2026-09-01", "2026-09-10", 10)
        uid = await _drive_booking_flow(
            self.dp, self.bot, CLIENT_ID_2, self.item_id, "2026-09-05", "2026-09-15", 20,
        )

        conn = sqlite3.connect(self.config.db_path)
        count = conn.execute("SELECT COUNT(*) FROM rental_bookings").fetchone()[0]
        conn.close()
        self.assertEqual(count, 1, "an overlapping booking was inserted despite the conflict")

    async def test_non_overlapping_booking_is_accepted(self):
        await _drive_booking_flow(self.dp, self.bot, CLIENT_ID, self.item_id, "2026-09-01", "2026-09-05", 10)
        await _drive_booking_flow(self.dp, self.bot, CLIENT_ID_2, self.item_id, "2026-09-06", "2026-09-10", 20)

        conn = sqlite3.connect(self.config.db_path)
        count = conn.execute("SELECT COUNT(*) FROM rental_bookings").fetchone()[0]
        conn.close()
        self.assertEqual(count, 2)

    async def test_cancelled_booking_does_not_block_overlap(self):
        await _drive_booking_flow(self.dp, self.bot, CLIENT_ID, self.item_id, "2026-09-01", "2026-09-10", 10)
        conn = sqlite3.connect(self.config.db_path)
        booking_id = conn.execute("SELECT id FROM rental_bookings").fetchone()[0]
        conn.close()

        await self.dp.feed_webhook_update(
            self.bot, _callback_update(50, ADMIN_ID, f"bk_status:{booking_id}:cancelled")
        )
        await _drive_booking_flow(self.dp, self.bot, CLIENT_ID_2, self.item_id, "2026-09-05", "2026-09-15", 60)

        conn = sqlite3.connect(self.config.db_path)
        rows = conn.execute("SELECT status FROM rental_bookings ORDER BY id").fetchall()
        conn.close()
        self.assertEqual([r[0] for r in rows], ["cancelled", "pending"])

    async def test_admin_confirm_transition_notifies_client(self):
        await _drive_booking_flow(self.dp, self.bot, CLIENT_ID, self.item_id, "2026-09-01", "2026-09-05", 10)
        conn = sqlite3.connect(self.config.db_path)
        booking_id = conn.execute("SELECT id FROM rental_bookings").fetchone()[0]
        conn.close()

        await self.dp.feed_webhook_update(
            self.bot, _callback_update(50, ADMIN_ID, f"bk_status:{booking_id}:confirmed")
        )

        conn = sqlite3.connect(self.config.db_path)
        status = conn.execute("SELECT status FROM rental_bookings WHERE id=?", (booking_id,)).fetchone()[0]
        conn.close()
        self.assertEqual(status, "confirmed")
        client_notes = self._sent_texts_to(CLIENT_ID)
        self.assertTrue(any("Подтверждена" in t for t in client_notes))

    async def test_forward_only_flow_rejects_skipping_a_stage(self):
        await _drive_booking_flow(self.dp, self.bot, CLIENT_ID, self.item_id, "2026-09-01", "2026-09-05", 10)
        conn = sqlite3.connect(self.config.db_path)
        booking_id = conn.execute("SELECT id FROM rental_bookings").fetchone()[0]
        conn.close()
        # "pending" -> "completed" is not a legal transition (must pass
        # through confirmed).
        await self.dp.feed_webhook_update(
            self.bot, _callback_update(50, ADMIN_ID, f"bk_status:{booking_id}:completed")
        )
        conn = sqlite3.connect(self.config.db_path)
        status = conn.execute("SELECT status FROM rental_bookings WHERE id=?", (booking_id,)).fetchone()[0]
        conn.close()
        self.assertEqual(status, "pending")

    async def test_contact_belonging_to_someone_else_is_rejected(self):
        uid = 10
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, CLIENT_ID, "cli_book")); uid += 1
        await self.dp.feed_webhook_update(
            self.bot, _callback_update(uid, CLIENT_ID, f"cli_pick:{self.item_id}")
        ); uid += 1
        await self.dp.feed_webhook_update(self.bot, _text_update(uid, CLIENT_ID, "2026-09-01")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _text_update(uid, CLIENT_ID, "2026-09-05")); uid += 1
        # Attacker submits someone ELSE's contact.
        await self.dp.feed_webhook_update(
            self.bot, _contact_update(uid, CLIENT_ID, PHONE, contact_user_id=CLIENT_ID_2)
        )
        conn = sqlite3.connect(self.config.db_path)
        count = conn.execute("SELECT COUNT(*) FROM rental_bookings").fetchone()[0]
        conn.close()
        self.assertEqual(count, 0, "a contact not belonging to the sender was accepted and created a booking")

    async def test_deactivated_item_not_offered_and_admin_actions_require_admin(self):
        await self.dp.feed_webhook_update(
            self.bot, _callback_update(70, ADMIN_ID, f"it_deactivate:{self.item_id}")
        )
        conn = sqlite3.connect(self.config.db_path)
        active = conn.execute("SELECT active FROM rental_items WHERE id=?", (self.item_id,)).fetchone()[0]
        conn.close()
        self.assertEqual(active, 0)

        # Non-admin cannot deactivate/list items or bookings — the handlers
        # must no-op instead of performing the action.
        async with aiosqlite.connect(self.config.db_path) as db:
            cur = await db.execute("INSERT INTO rental_items (name, price_per_day) VALUES ('Honda', 3000)")
            other_item_id = cur.lastrowid
            await db.commit()
        await self.dp.feed_webhook_update(
            self.bot, _callback_update(71, CLIENT_ID, f"it_deactivate:{other_item_id}")
        )
        conn = sqlite3.connect(self.config.db_path)
        active = conn.execute("SELECT active FROM rental_items WHERE id=?", (other_item_id,)).fetchone()[0]
        conn.close()
        self.assertEqual(active, 1, "a non-admin was able to deactivate a rental item")


class CarRentalRaceSafetyTests(unittest.IsolatedAsyncioTestCase):
    """Owner-flagged race: two clients booking overlapping date ranges on the
    SAME item concurrently must not both succeed — same protection class as
    templates/booking_fitness.py's _book_slot double-book test."""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = car_rental.config_from_bot_row(
            {"bot_id": 804, "name": "car_rental_race_bot", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await car_rental.init_db(self.config.db_path)
        async with aiosqlite.connect(self.config.db_path) as db:
            cur = await db.execute("INSERT INTO rental_items (name, price_per_day) VALUES ('Kia Rio', 2000)")
            self.item_id = cur.lastrowid
            await db.commit()

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def test_concurrent_overlapping_bookings_only_one_succeeds(self):
        results = await asyncio.gather(
            car_rental._create_booking_if_free(
                self.config.db_path, self.item_id, CLIENT_ID, "Client A", "+7 900 000-00-01",
                "2026-09-01", "2026-09-10",
            ),
            car_rental._create_booking_if_free(
                self.config.db_path, self.item_id, CLIENT_ID_2, "Client B", "+7 900 000-00-02",
                "2026-09-05", "2026-09-15",
            ),
        )
        oks = [r for r in results if r["ok"]]
        errs = [r for r in results if not r["ok"]]
        self.assertEqual(len(oks), 1)
        self.assertEqual(len(errs), 1)
        self.assertEqual(errs[0]["error"], "overlap")

        conn = sqlite3.connect(self.config.db_path)
        count = conn.execute("SELECT COUNT(*) FROM rental_bookings").fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)

    async def test_concurrent_non_overlapping_bookings_both_succeed(self):
        results = await asyncio.gather(
            car_rental._create_booking_if_free(
                self.config.db_path, self.item_id, CLIENT_ID, "Client A", "+7 900 000-00-01",
                "2026-09-01", "2026-09-05",
            ),
            car_rental._create_booking_if_free(
                self.config.db_path, self.item_id, CLIENT_ID_2, "Client B", "+7 900 000-00-02",
                "2026-09-06", "2026-09-10",
            ),
        )
        oks = [r for r in results if r["ok"]]
        self.assertEqual(len(oks), 2)


class CarRentalOverlapUnitTests(unittest.IsolatedAsyncioTestCase):
    """Direct unit coverage of the inclusive date-range overlap boundary
    conditions, independent of the FSM."""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.db_path = str(self.data_dir / "overlap_test.db")
        await car_rental.init_db(self.db_path)
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("INSERT INTO rental_items (name, price_per_day) VALUES ('Kia Rio', 2000)")
            self.item_id = cur.lastrowid
            await db.commit()

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def test_adjacent_ranges_touching_at_boundary_are_treated_as_overlap(self):
        """[2026-09-01, 2026-09-05] and [2026-09-05, 2026-09-10] share the
        09-05 day — inclusive overlap per the spec's NOT(new_end<start OR
        new_start>end) formula, so this must be rejected, not accepted."""
        first = await car_rental._create_booking_if_free(
            self.db_path, self.item_id, CLIENT_ID, "A", "+7", "2026-09-01", "2026-09-05",
        )
        self.assertTrue(first["ok"])
        second = await car_rental._create_booking_if_free(
            self.db_path, self.item_id, CLIENT_ID_2, "B", "+7", "2026-09-05", "2026-09-10",
        )
        self.assertFalse(second["ok"])
        self.assertEqual(second["error"], "overlap")

    async def test_truly_adjacent_non_touching_ranges_do_not_overlap(self):
        first = await car_rental._create_booking_if_free(
            self.db_path, self.item_id, CLIENT_ID, "A", "+7", "2026-09-01", "2026-09-04",
        )
        self.assertTrue(first["ok"])
        second = await car_rental._create_booking_if_free(
            self.db_path, self.item_id, CLIENT_ID_2, "B", "+7", "2026-09-05", "2026-09-10",
        )
        self.assertTrue(second["ok"])

    async def test_booking_gone_item_rejected(self):
        result = await car_rental._create_booking_if_free(
            self.db_path, 99999, CLIENT_ID, "A", "+7", "2026-09-01", "2026-09-05",
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "item_gone")

    async def test_deactivated_item_rejects_new_bookings(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("UPDATE rental_items SET active=0 WHERE id=?", (self.item_id,))
            await db.commit()
        result = await car_rental._create_booking_if_free(
            self.db_path, self.item_id, CLIENT_ID, "A", "+7", "2026-09-01", "2026-09-05",
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "item_gone")


class CarRentalStandaloneSmokeTest(unittest.TestCase):
    def test_config_from_env_matches_legacy_constant_shape(self):
        config = car_rental.config_from_env()
        self.assertTrue(config.db_path.endswith("car_rental_data.db"))
        self.assertEqual(config.bot_name, "car_rental")

    def test_router_and_main_entrypoint_exist(self):
        self.assertTrue(hasattr(car_rental, "router"))
        self.assertTrue(hasattr(car_rental, "main"))

    def test_date_parsing_rejects_bad_format_and_past_dates(self):
        self.assertIsNone(car_rental._parse_date("not-a-date"))
        self.assertIsNone(car_rental._parse_date("2020-01-01"))
        self.assertIsNone(car_rental._parse_date("2026-13-40"))


class CarRentalMiniAppConfigTests(unittest.IsolatedAsyncioTestCase):
    """miniapp_config's declared table/field names must match init_db()'s
    real schema — miniapp_api.py builds SQL directly off these names, so a
    drift here would 500 at request time instead of failing a test."""

    def test_miniapp_config_resource_names(self):
        names = {r["name"] for r in car_rental.miniapp_config["resources"]}
        self.assertEqual(names, {"cars", "bookings"})

    def test_cars_resource_targets_rental_items_table(self):
        cars = next(r for r in car_rental.miniapp_config["resources"] if r["name"] == "cars")
        self.assertEqual(cars["table"], "rental_items")
        self.assertTrue(cars["creatable"])
        self.assertIn("name", {f["name"] for f in cars["fields"]})

    def test_bookings_resource_targets_rental_bookings_table(self):
        bookings = next(r for r in car_rental.miniapp_config["resources"] if r["name"] == "bookings")
        self.assertEqual(bookings["table"], "rental_bookings")
        field_names = {f["name"] for f in bookings["fields"]}
        self.assertEqual(
            field_names,
            {"item_id", "client_user_id", "client_username", "client_name", "client_phone",
             "start_date", "end_date", "status", "created_at"},
        )

    async def test_miniapp_config_fields_match_real_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "schema_check.db")
            await car_rental.init_db(db_path)
            async with aiosqlite.connect(db_path) as db:
                for resource in car_rental.miniapp_config["resources"]:
                    cur = await db.execute(f"PRAGMA table_info({resource['table']})")
                    real_columns = {row[1] for row in await cur.fetchall()}
                    declared = {f["name"] for f in resource["fields"]} | {"id"}
                    self.assertTrue(
                        declared.issubset(real_columns),
                        f"{resource['name']}: declared fields {declared} not all in "
                        f"real columns {real_columns}",
                    )


class CarRentalAppCommandTests(unittest.IsolatedAsyncioTestCase):
    """/app command + the "🌐 Веб-приложение" menu button — same magic-link
    pattern as templates/tour_operator.py's cmd_app (see
    tests/test_tour_operator_isolation.py's equivalent tests)."""

    async def _send(self, text_or_callback: str, is_callback: bool = False) -> list[str]:
        bot_call_mock = AsyncMock(return_value=MagicMock())
        with patch.object(Bot, "__call__", new=bot_call_mock):
            with tempfile.TemporaryDirectory() as tmp:
                config = car_rental.config_from_bot_row(
                    {"bot_id": 901, "name": "car_rental_app_bot", "display_name": None, "group_chat_id": None},
                    Path(tmp),
                )
                await car_rental.init_db(config.db_path)
                bot, dp = _build_bot_dispatcher(config)
                await dp.feed_webhook_update(bot, _text_update(1, ADMIN_ID, "/start"))
                if is_callback:
                    await dp.feed_webhook_update(bot, _callback_update(2, ADMIN_ID, text_or_callback))
                else:
                    await dp.feed_webhook_update(bot, _text_update(2, ADMIN_ID, text_or_callback))
        return [
            call.args[0].text for call in bot_call_mock.call_args_list
            if getattr(call.args[0], "text", None)
        ]

    async def test_app_command_shows_url_when_miniapp_secret_configured(self):
        with patch.dict(os.environ, {"MINIAPP_SECRET": "test-secret"}):
            sent_texts = await self._send("/app")
        self.assertTrue(any("http://" in t or "https://" in t for t in sent_texts))

    async def test_app_command_shows_unavailable_message_when_miniapp_secret_missing(self):
        with patch.dict(os.environ):
            os.environ.pop("MINIAPP_SECRET", None)
            sent_texts = await self._send("/app")
        self.assertTrue(any("не настроен MINIAPP_SECRET" in t for t in sent_texts))
        self.assertTrue(all("http://" not in t and "https://" not in t for t in sent_texts))

    async def test_miniapp_link_button_shows_url_when_configured(self):
        with patch.dict(os.environ, {"MINIAPP_SECRET": "test-secret"}):
            sent_texts = await self._send("miniapp_link", is_callback=True)
        self.assertTrue(any("http://" in t or "https://" in t for t in sent_texts))


if __name__ == "__main__":
    unittest.main()
