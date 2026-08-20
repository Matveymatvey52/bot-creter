"""Data + settings isolation, group waitlist auto-promotion (incl. the
owner-flagged concurrent-cancel race), and menu-branch-separation tests for
the booking_fitness template.

Standard criterion (same as test_booking_beauty_isolation.py): two bots on
the SAME template, different config, must never mix data — even driven by
the SAME Telegram user_id. Extended here with the two owner-requested
per-bot settings toggles (individual_consumes_visits,
group_requires_subscription), which must also never leak between bots.

No real Telegram network calls, no real tokens.

Run with: python -m unittest tests.test_booking_fitness_isolation
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
from templates import booking_fitness as bf

FAKE_TOKEN = "123456:test-token-not-real"


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


def _build_bot_dispatcher(config: bf.BookingFitnessConfig) -> tuple[Bot, Dispatcher]:
    bot = Bot(token=FAKE_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(bf.ConfigMiddleware(config))
    dp.include_router(get_template_router("booking_fitness"))
    return bot, dp


async def _seed_trainer_and_group_slot(db_path: str, capacity: int = 1) -> int:
    """Inserts one trainer + one recurring group template for TODAY's weekday,
    generates the concrete slot via the template's own idempotent generator,
    and returns the resulting slot_id."""
    from datetime import date
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute("INSERT INTO trainers (name, specializations) VALUES ('Anna', 'Йога')")
        trainer_id = cur.lastrowid
        await db.execute(
            "INSERT INTO group_schedule_templates (day_of_week, slot_time, trainer_id, class_name, capacity) "
            "VALUES (?, '19:00', ?, 'Йога', ?)",
            (date.today().weekday(), trainer_id, capacity),
        )
        await db.commit()
    await bf._ensure_group_sessions(db_path)
    async with aiosqlite.connect(db_path) as db:
        row = await (await db.execute(
            "SELECT id FROM slots WHERE session_type='group' AND slot_date=?", (date.today().isoformat(),)
        )).fetchone()
    return row[0]


async def _grant_subscription(db_path: str, user_id: int, visits: int = 10) -> int:
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "INSERT INTO subscriptions (user_id, plan_name, total_visits, visits_left) VALUES (?, 'Test', ?, ?)",
            (user_id, visits, visits),
        )
        await db.commit()
        return cur.lastrowid


class BookingFitnessIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

        self.config_a = bf.config_from_bot_row(
            {"bot_id": 301, "name": "fit_bot_a", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        self.config_b = bf.config_from_bot_row(
            {"bot_id": 302, "name": "fit_bot_b", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await bf.init_db(self.config_a.db_path)
        await bf.init_db(self.config_b.db_path)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_configs_point_to_different_files(self):
        self.assertNotEqual(self.config_a.db_path, self.config_b.db_path)
        self.assertNotEqual(self.config_a.admins_file, self.config_b.admins_file)

    async def test_subscription_data_isolated_across_bots_same_user(self):
        """Same Telegram user_id has a subscription with DIFFERENT visits_left
        on each bot — the classic cross-bot leak this whole test class exists
        to catch, applied to the new subscriptions table."""
        SAME_USER = 555
        await _grant_subscription(self.config_a.db_path, SAME_USER, visits=3)
        await _grant_subscription(self.config_b.db_path, SAME_USER, visits=9)

        conn_a = sqlite3.connect(self.config_a.db_path)
        visits_a = conn_a.execute("SELECT visits_left FROM subscriptions WHERE user_id=?", (SAME_USER,)).fetchone()[0]
        conn_a.close()
        conn_b = sqlite3.connect(self.config_b.db_path)
        visits_b = conn_b.execute("SELECT visits_left FROM subscriptions WHERE user_id=?", (SAME_USER,)).fetchone()[0]
        conn_b.close()

        self.assertEqual(visits_a, 3)
        self.assertEqual(visits_b, 9)

    async def test_settings_flags_isolated_across_bots(self):
        """Owner-requested: bot A toggles individual_consumes_visits=True,
        bot B leaves it at the default False — the two bots' settings rows
        must never be confused, since they live in physically separate
        SQLite files (see config_from_bot_row's bot_id-keyed db_path)."""
        settings_a_before = await bf._get_settings(self.config_a.db_path)
        settings_b_before = await bf._get_settings(self.config_b.db_path)
        self.assertFalse(settings_a_before["individual_consumes_visits"])
        self.assertFalse(settings_b_before["individual_consumes_visits"])
        self.assertTrue(settings_a_before["group_requires_subscription"])
        self.assertTrue(settings_b_before["group_requires_subscription"])

        await bf._toggle_setting(self.config_a.db_path, "individual_consumes_visits")
        await bf._toggle_setting(self.config_a.db_path, "group_requires_subscription")

        settings_a_after = await bf._get_settings(self.config_a.db_path)
        settings_b_after = await bf._get_settings(self.config_b.db_path)

        self.assertTrue(settings_a_after["individual_consumes_visits"])
        self.assertFalse(settings_a_after["group_requires_subscription"])
        # bot B must be completely untouched by bot A's toggles.
        self.assertFalse(settings_b_after["individual_consumes_visits"])
        self.assertTrue(settings_b_after["group_requires_subscription"])

    async def test_settings_flag_actually_changes_booking_behavior_per_bot(self):
        """Not just a DB-row check: with group_requires_subscription=False on
        bot A, a user with NO subscription can still book; the same user on
        bot B (default True, untouched) is blocked. Confirms the flag is
        actually READ by _book_slot, not just stored."""
        await bf._toggle_setting(self.config_a.db_path, "group_requires_subscription")  # A: now False
        slot_a = await _seed_trainer_and_group_slot(self.config_a.db_path, capacity=2)
        slot_b = await _seed_trainer_and_group_slot(self.config_b.db_path, capacity=2)

        NO_SUB_USER = 777
        result_a = await bf._book_slot(self.config_a.db_path, slot_a, NO_SUB_USER, "NoSub", None)
        result_b = await bf._book_slot(self.config_b.db_path, slot_b, NO_SUB_USER, "NoSub", None)

        self.assertTrue(result_a["ok"])
        self.assertEqual(result_a["status"], "confirmed")
        self.assertFalse(result_b["ok"])
        self.assertEqual(result_b["error"], "no_subscription")

    async def test_admin_bootstrap_isolated_per_bot(self):
        bot_a, dp_a = _build_bot_dispatcher(self.config_a)
        bot_b, dp_b = _build_bot_dispatcher(self.config_b)
        await dp_a.feed_webhook_update(bot_a, _text_update(1, 111, "/start"))
        await dp_b.feed_webhook_update(bot_b, _text_update(1, 999, "/start"))

        self.assertEqual(bf._load_admins(self.config_a.admins_file), {"111"})
        self.assertEqual(bf._load_admins(self.config_b.admins_file), {"999"})


class GroupWaitlistTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = bf.config_from_bot_row(
            {"bot_id": 401, "name": "fit_waitlist_bot", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await bf.init_db(self.config.db_path)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_full_group_slot_books_confirmed_then_waitlist(self):
        slot_id = await _seed_trainer_and_group_slot(self.config.db_path, capacity=1)
        await _grant_subscription(self.config.db_path, 1)
        await _grant_subscription(self.config.db_path, 2)

        first = await bf._book_slot(self.config.db_path, slot_id, 1, "First", None)
        second = await bf._book_slot(self.config.db_path, slot_id, 2, "Second", None)

        self.assertEqual(first["status"], "confirmed")
        self.assertEqual(second["status"], "waitlist")
        self.assertEqual(second["position"], 1)

    async def test_cancel_confirmed_promotes_oldest_waitlist_entry(self):
        """Group waitlist -> auto-promotion on cancel, per template's core
        domain requirement."""
        slot_id = await _seed_trainer_and_group_slot(self.config.db_path, capacity=1)
        for uid in (1, 2, 3):
            await _grant_subscription(self.config.db_path, uid)

        first = await bf._book_slot(self.config.db_path, slot_id, 1, "First", None)
        second = await bf._book_slot(self.config.db_path, slot_id, 2, "Second", None)
        third = await bf._book_slot(self.config.db_path, slot_id, 3, "Third", None)
        self.assertEqual(first["status"], "confirmed")
        self.assertEqual(second["status"], "waitlist")
        self.assertEqual(third["status"], "waitlist")

        result = await bf._cancel_booking(self.config.db_path, first["booking_id"])
        self.assertTrue(result["ok"])
        self.assertIsNotNone(result["promoted"])
        self.assertEqual(result["promoted"]["user_id"], 2)  # oldest waitlisted, not user 3

        conn = sqlite3.connect(self.config.db_path)
        row2 = conn.execute("SELECT status FROM bookings WHERE user_id=2").fetchone()
        row3 = conn.execute("SELECT status FROM bookings WHERE user_id=3").fetchone()
        conn.close()
        self.assertEqual(row2[0], "confirmed")
        self.assertEqual(row3[0], "waitlist")

    async def test_cancel_refunds_visit_and_promotion_consumes_one(self):
        slot_id = await _seed_trainer_and_group_slot(self.config.db_path, capacity=1)
        sub1 = await _grant_subscription(self.config.db_path, 1, visits=5)
        sub2 = await _grant_subscription(self.config.db_path, 2, visits=5)

        first = await bf._book_slot(self.config.db_path, slot_id, 1, "First", None)
        await bf._book_slot(self.config.db_path, slot_id, 2, "Second", None)

        conn = sqlite3.connect(self.config.db_path)
        self.assertEqual(conn.execute("SELECT visits_left FROM subscriptions WHERE id=?", (sub1,)).fetchone()[0], 4)
        conn.close()

        await bf._cancel_booking(self.config.db_path, first["booking_id"])

        conn = sqlite3.connect(self.config.db_path)
        # user 1's refund: 4 -> 5
        self.assertEqual(conn.execute("SELECT visits_left FROM subscriptions WHERE id=?", (sub1,)).fetchone()[0], 5)
        # user 2's promotion consumed a visit: 5 -> 4
        self.assertEqual(conn.execute("SELECT visits_left FROM subscriptions WHERE id=?", (sub2,)).fetchone()[0], 4)
        conn.close()

    async def test_concurrent_cancels_on_full_slot_each_promote_a_distinct_waitlister(self):
        """Owner-flagged race: two admins cancel two DIFFERENT confirmed
        bookings on the SAME full slot at (as close to) the same moment.
        Without BEGIN IMMEDIATE serializing _cancel_booking, both cancel
        calls could read the same "oldest waitlist row" and both promote IT,
        double-charging one user's subscription while the next-in-line never
        gets in. Capacity=2 here so there are two confirmed bookings to
        cancel concurrently, and two distinct waitlisters who must each get
        exactly one of the two freed spots."""
        slot_id = await _seed_trainer_and_group_slot(self.config.db_path, capacity=2)
        for uid in (1, 2, 3, 4):
            await _grant_subscription(self.config.db_path, uid)

        b1 = await bf._book_slot(self.config.db_path, slot_id, 1, "A", None)
        b2 = await bf._book_slot(self.config.db_path, slot_id, 2, "B", None)
        b3 = await bf._book_slot(self.config.db_path, slot_id, 3, "C", None)  # waitlist #1
        b4 = await bf._book_slot(self.config.db_path, slot_id, 4, "D", None)  # waitlist #2
        self.assertEqual(b1["status"], "confirmed")
        self.assertEqual(b2["status"], "confirmed")
        self.assertEqual(b3["status"], "waitlist")
        self.assertEqual(b4["status"], "waitlist")

        # Two "admins" cancel the two confirmed bookings at the same time.
        results = await asyncio.gather(
            bf._cancel_booking(self.config.db_path, b1["booking_id"]),
            bf._cancel_booking(self.config.db_path, b2["booking_id"]),
        )
        promoted_user_ids = sorted(r["promoted"]["user_id"] for r in results if r["promoted"])

        # Both freed spots must go to BOTH distinct waitlisters (3 and 4) —
        # never the same one promoted twice, never one left unpromoted.
        self.assertEqual(promoted_user_ids, [3, 4])

        conn = sqlite3.connect(self.config.db_path)
        confirmed_count = conn.execute(
            "SELECT COUNT(*) FROM bookings WHERE slot_id=? AND status='confirmed'", (slot_id,)
        ).fetchone()[0]
        waitlist_count = conn.execute(
            "SELECT COUNT(*) FROM bookings WHERE slot_id=? AND status='waitlist'", (slot_id,)
        ).fetchone()[0]
        conn.close()
        self.assertEqual(confirmed_count, 2)  # exactly capacity, not oversold/undersold
        self.assertEqual(waitlist_count, 0)

    async def test_double_tap_same_user_same_slot_does_not_duplicate_booking(self):
        """Review-found regression test: a double-tap on "Записаться" fires
        two callback updates for the same user+slot. Before the fix,
        _book_slot() never checked for an existing booking by this user on
        this slot, so on any group slot with capacity>1 the second call
        would see its own first booking already counted and insert a SECOND
        confirmed row for the same user — double-charging a subscription
        visit and occupying two capacity slots that should hold two
        different clients."""
        slot_id = await _seed_trainer_and_group_slot(self.config.db_path, capacity=4)
        sub_id = await _grant_subscription(self.config.db_path, 1, visits=5)

        first = await bf._book_slot(self.config.db_path, slot_id, 1, "First", None)
        second = await bf._book_slot(self.config.db_path, slot_id, 1, "First", None)  # double-tap

        self.assertEqual(first["status"], "confirmed")
        self.assertEqual(second["status"], "confirmed")
        self.assertEqual(first["booking_id"], second["booking_id"])  # same row, not a new one

        conn = sqlite3.connect(self.config.db_path)
        booking_count = conn.execute(
            "SELECT COUNT(*) FROM bookings WHERE slot_id=? AND user_id=1", (slot_id,)
        ).fetchone()[0]
        confirmed_count = conn.execute(
            "SELECT COUNT(*) FROM bookings WHERE slot_id=? AND status='confirmed'", (slot_id,)
        ).fetchone()[0]
        visits_left = conn.execute("SELECT visits_left FROM subscriptions WHERE id=?", (sub_id,)).fetchone()[0]
        conn.close()

        self.assertEqual(booking_count, 1)  # not duplicated
        self.assertEqual(confirmed_count, 1)  # not double-counted against capacity
        self.assertEqual(visits_left, 4)  # only ONE visit consumed, not two

    async def test_individual_slot_never_waitlists_on_double_book_race(self):
        """Same _book_slot race protection, individual branch: two users
        confirming the SAME 1-on-1 slot concurrently must not both succeed,
        and the loser must NOT be silently waitlisted (nobody waits for a
        1-on-1 hour)."""
        async with aiosqlite.connect(self.config.db_path) as db:
            cur = await db.execute("INSERT INTO trainers (name, specializations) VALUES ('Ivan', 'Кроссфит')")
            trainer_id = cur.lastrowid
            await db.commit()
        await bf._ensure_individual_slots(self.config.db_path)
        conn = sqlite3.connect(self.config.db_path)
        slot_id = conn.execute(
            "SELECT id FROM slots WHERE session_type='individual' AND trainer_id=? LIMIT 1", (trainer_id,)
        ).fetchone()[0]
        conn.close()

        results = await asyncio.gather(
            bf._book_slot(self.config.db_path, slot_id, 10, "X", "+7 900 000-00-01"),
            bf._book_slot(self.config.db_path, slot_id, 11, "Y", "+7 900 000-00-02"),
        )
        oks = [r for r in results if r["ok"]]
        errs = [r for r in results if not r["ok"]]
        self.assertEqual(len(oks), 1)
        self.assertEqual(len(errs), 1)
        self.assertEqual(errs[0]["error"], "slot_gone")


class MenuBranchSeparationTests(unittest.IsolatedAsyncioTestCase):
    """Owner requirement: group and individual are two EXPLICIT top-level
    menu branches from the first screen, not a hidden capacity==1 fork —
    confirms both buttons exist on /start and each leads to a genuinely
    different first screen/handler, not the same code path."""

    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._mock_call = self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = bf.config_from_bot_row(
            {"bot_id": 501, "name": "fit_menu_bot", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await bf.init_db(self.config.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_start_menu_exposes_both_branches_as_distinct_buttons(self):
        await self.dp.feed_webhook_update(self.bot, _text_update(1, 42, "/start"))
        sent_texts = [
            call.args[0] for call in self._mock_call.call_args_list
            if call.args and hasattr(call.args[0], "reply_markup") and call.args[0].reply_markup
        ]
        found_group = found_individual = False
        for method in sent_texts:
            for row in method.reply_markup.keyboard:
                for btn in row:
                    if btn.text == "📅 Групповое занятие":
                        found_group = True
                    if btn.text == "🧑 Индивидуальная тренировка":
                        found_individual = True
        self.assertTrue(found_group, "group branch button missing from /start menu")
        self.assertTrue(found_individual, "individual branch button missing from /start menu")

    async def test_group_and_individual_entry_points_are_different_handlers(self):
        """The two branches must route to structurally different first
        screens (group: day picker sourced from slots WHERE session_type=
        'group'; individual: specialization picker sourced from trainers) —
        not the same handler branching internally on a capacity value."""
        self._mock_call.reset_mock()
        await self.dp.feed_webhook_update(self.bot, _text_update(2, 42, "📅 Групповое занятие"))
        group_call = self._mock_call.call_args_list[-1]
        group_text = group_call.args[0].text

        self._mock_call.reset_mock()
        await self.dp.feed_webhook_update(self.bot, _text_update(3, 42, "🧑 Индивидуальная тренировка"))
        individual_call = self._mock_call.call_args_list[-1]
        individual_text = individual_call.args[0].text

        self.assertIn("день", group_text.lower())
        self.assertIn("специализац", individual_text.lower())
        self.assertNotEqual(group_text, individual_text)


class BookingFitnessMiniAppConfigTests(unittest.IsolatedAsyncioTestCase):
    """miniapp_config's declared table/field names must match init_db()'s
    real schema — miniapp_api.py builds SQL directly off these names, so a
    drift here would 500 at request time instead of failing a test."""

    def test_miniapp_config_resource_names(self):
        names = {r["name"] for r in bf.miniapp_config["resources"]}
        self.assertEqual(
            names,
            {"trainers", "group_schedule_templates", "slots", "bookings",
             "subscription_plans", "subscriptions"},
        )

    def test_trainers_resource_targets_trainers_table(self):
        trainers = next(r for r in bf.miniapp_config["resources"] if r["name"] == "trainers")
        self.assertEqual(trainers["table"], "trainers")
        self.assertTrue(trainers["creatable"])

    def test_bookings_resource_targets_bookings_table_not_creatable(self):
        bookings = next(r for r in bf.miniapp_config["resources"] if r["name"] == "bookings")
        self.assertEqual(bookings["table"], "bookings")
        self.assertFalse(bookings["creatable"])

    def test_slots_resource_not_creatable(self):
        slots = next(r for r in bf.miniapp_config["resources"] if r["name"] == "slots")
        self.assertEqual(slots["table"], "slots")
        self.assertFalse(slots["creatable"])

    async def test_miniapp_config_fields_match_real_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "schema_check.db")
            await bf.init_db(db_path)
            async with aiosqlite.connect(db_path) as db:
                for resource in bf.miniapp_config["resources"]:
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
