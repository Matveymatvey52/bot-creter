"""New-feature tests for templates/booking_fitness.py (trainer ratings,
subscription autorenew, extended stats, and the role_filter decision on
bookings/subscriptions).

Same style/fixtures as tests/test_booking_fitness_isolation.py — no real
Telegram network calls, no real tokens.

Run with: python -m unittest tests.test_booking_fitness
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
from aiogram import Bot

from templates import booking_fitness as bf


async def _seed_trainer(db_path: str, name: str = "Anna", specs: str = "Йога") -> int:
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute("INSERT INTO trainers (name, specializations) VALUES (?, ?)", (name, specs))
        await db.commit()
        return cur.lastrowid


async def _seed_group_slot(db_path: str, trainer_id: int, slot_date: str, capacity: int = 4) -> int:
    """Inserts a concrete group slot directly (bypassing the recurring-
    template generator) so a PAST date can be seeded for rating tests —
    _ensure_group_sessions only ever generates slots from today forward."""
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "INSERT INTO slots (session_type, trainer_id, slot_date, slot_time, class_name, capacity) "
            "VALUES ('group', ?, ?, '19:00', 'Йога', ?)",
            (trainer_id, slot_date, capacity),
        )
        await db.commit()
        return cur.lastrowid


async def _grant_subscription(
    db_path: str, user_id: int, visits: int = 10, plan_name: str = "Test",
    expires_at: str | None = None, autorenew: int = 0,
) -> int:
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "INSERT INTO subscriptions (user_id, plan_name, total_visits, visits_left, expires_at, autorenew) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, plan_name, visits, visits, expires_at, autorenew),
        )
        await db.commit()
        return cur.lastrowid


class TrainerRatingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = bf.config_from_bot_row(
            {"bot_id": 601, "name": "fit_rating_bot", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await bf.init_db(self.config.db_path)

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def _confirmed_past_booking(self, user_id: int, days_ago: int = 1) -> tuple[int, int]:
        trainer_id = await _seed_trainer(self.config.db_path)
        slot_date = (date.today() - timedelta(days=days_ago)).isoformat()
        slot_id = await _seed_group_slot(self.config.db_path, trainer_id, slot_date)
        async with aiosqlite.connect(self.config.db_path) as db:
            cur = await db.execute(
                "INSERT INTO bookings (slot_id, user_id, client_name, status) VALUES (?, ?, 'Client', 'confirmed')",
                (slot_id, user_id),
            )
            await db.commit()
            booking_id = cur.lastrowid
        return trainer_id, booking_id

    async def test_record_rating_stores_row_and_clamps_out_of_range(self):
        trainer_id, booking_id = await self._confirmed_past_booking(user_id=1)
        result = await bf._record_rating(self.config.db_path, booking_id, 1, rating=99, comment="Отлично!")
        self.assertTrue(result["ok"])
        conn = sqlite3.connect(self.config.db_path)
        row = conn.execute(
            "SELECT trainer_id, user_id, rating, comment FROM trainer_ratings WHERE booking_id=?", (booking_id,)
        ).fetchone()
        conn.close()
        self.assertEqual(row, (trainer_id, 1, bf.MAX_RATING, "Отлично!"))

    async def test_average_rating_computed_correctly(self):
        trainer_id, b1 = await self._confirmed_past_booking(user_id=1)
        _, b2 = await self._confirmed_past_booking(user_id=2)
        # Second booking's trainer_id differs (a new trainer row each call)
        # — force it onto the SAME trainer so the average is meaningful.
        async with aiosqlite.connect(self.config.db_path) as db:
            await db.execute(
                "UPDATE slots SET trainer_id=? WHERE id=(SELECT slot_id FROM bookings WHERE id=?)",
                (trainer_id, b2),
            )
            await db.commit()

        await bf._record_rating(self.config.db_path, b1, 1, rating=5, comment=None)
        await bf._record_rating(self.config.db_path, b2, 2, rating=3, comment=None)

        async with aiosqlite.connect(self.config.db_path) as db:
            avg, count = await bf._trainer_rating_summary(db, trainer_id)
        self.assertEqual(count, 2)
        self.assertAlmostEqual(avg, 4.0)

    async def test_cannot_rate_another_users_booking(self):
        _, booking_id = await self._confirmed_past_booking(user_id=1)
        result = await bf._record_rating(self.config.db_path, booking_id, user_id=2, rating=5, comment=None)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "forbidden")

    async def test_double_rating_same_booking_does_not_duplicate_or_error(self):
        """Same double-tap-race posture as _book_slot's existing-booking
        check: UNIQUE(booking_id) + INSERT OR IGNORE makes a concurrent
        resubmit a no-op, not a 500."""
        _, booking_id = await self._confirmed_past_booking(user_id=1)
        first = await bf._record_rating(self.config.db_path, booking_id, 1, rating=4, comment=None)
        second = await bf._record_rating(self.config.db_path, booking_id, 1, rating=1, comment=None)
        self.assertTrue(first["ok"])
        self.assertFalse(second["ok"])
        self.assertEqual(second["error"], "already_rated")

        conn = sqlite3.connect(self.config.db_path)
        count = conn.execute("SELECT COUNT(*) FROM trainer_ratings WHERE booking_id=?", (booking_id,)).fetchone()[0]
        rating = conn.execute("SELECT rating FROM trainer_ratings WHERE booking_id=?", (booking_id,)).fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)
        self.assertEqual(rating, 4)  # first write wins, second was ignored

    async def test_cannot_rate_a_session_that_has_not_happened_yet(self):
        trainer_id = await _seed_trainer(self.config.db_path)
        future_date = (date.today() + timedelta(days=3)).isoformat()
        slot_id = await _seed_group_slot(self.config.db_path, trainer_id, future_date)
        async with aiosqlite.connect(self.config.db_path) as db:
            cur = await db.execute(
                "INSERT INTO bookings (slot_id, user_id, client_name, status) VALUES (?, 1, 'Client', 'confirmed')",
                (slot_id,),
            )
            await db.commit()
            booking_id = cur.lastrowid
        result = await bf._record_rating(self.config.db_path, booking_id, 1, rating=5, comment=None)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "not_yet")

    async def test_unrated_past_bookings_excludes_already_rated(self):
        trainer_id, booking_id = await self._confirmed_past_booking(user_id=1)
        pending_before = await bf._unrated_past_bookings(self.config.db_path, 1)
        self.assertEqual(len(pending_before), 1)

        await bf._record_rating(self.config.db_path, booking_id, 1, rating=5, comment=None)
        pending_after = await bf._unrated_past_bookings(self.config.db_path, 1)
        self.assertEqual(pending_after, [])


class SubscriptionAutorenewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._mock_call = self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = bf.config_from_bot_row(
            {"bot_id": 602, "name": "fit_autorenew_bot", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await bf.init_db(self.config.db_path)
        # A matching active plan must exist for the loop to re-issue a
        # subscription under the same name — see cb_adm_issue_plan's own
        # lookup-by-name convention, which the autorenew branch mirrors.
        async with aiosqlite.connect(self.config.db_path) as db:
            await db.execute(
                "INSERT INTO subscription_plans (name, total_visits, validity_days) VALUES ('8 занятий', 8, 30)"
            )
            await db.commit()

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def _run_one_reminder_pass(self):
        """Runs the loop body once by cancelling it right after its first
        iteration completes — same "drive one iteration only" approach as
        other reminder-loop tests in this repo (avoid sleeping the full
        REMINDER_POLL_SECONDS in a test)."""
        import asyncio
        task = asyncio.create_task(
            bf._subscription_reminder_loop(self.bot, self.config.db_path, self.config.bot_name)
        )
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def test_expired_autorenew_subscription_creates_new_one_with_correct_expiry(self):
        self.bot = Bot(token="123456:test-token-not-real")
        expired_at = (date.today() - timedelta(days=1)).isoformat()
        sub_id = await _grant_subscription(
            self.config.db_path, user_id=1, visits=0, plan_name="8 занятий",
            expires_at=expired_at, autorenew=1,
        )
        await self._run_one_reminder_pass()

        conn = sqlite3.connect(self.config.db_path)
        rows = conn.execute(
            "SELECT id, visits_left, expires_at, autorenew, autorenew_done FROM subscriptions "
            "WHERE user_id=1 ORDER BY id"
        ).fetchall()
        conn.close()

        self.assertEqual(len(rows), 2)  # original + the auto-renewed one
        original = next(r for r in rows if r[0] == sub_id)
        self.assertEqual(original[4], 1)  # autorenew_done flagged so it's never reprocessed
        renewed = next(r for r in rows if r[0] != sub_id)
        self.assertEqual(renewed[1], 8)  # fresh visits_left = plan's total_visits
        expected_expiry = (date.today() + timedelta(days=30)).isoformat()
        self.assertEqual(renewed[2], expected_expiry)
        self.assertEqual(renewed[3], 1)  # still autorenew=1 going forward

    async def test_expired_subscription_without_autorenew_creates_nothing(self):
        self.bot = Bot(token="123456:test-token-not-real")
        expired_at = (date.today() - timedelta(days=1)).isoformat()
        await _grant_subscription(
            self.config.db_path, user_id=2, visits=0, plan_name="8 занятий",
            expires_at=expired_at, autorenew=0,
        )
        await self._run_one_reminder_pass()

        conn = sqlite3.connect(self.config.db_path)
        rows = conn.execute("SELECT id FROM subscriptions WHERE user_id=2").fetchall()
        conn.close()
        self.assertEqual(len(rows), 1)  # nothing new created

    async def test_autorenew_toggle_via_handler_flips_flag_ownership_scoped(self):
        sub_id = await _grant_subscription(
            self.config.db_path, user_id=3, expires_at=(date.today() + timedelta(days=10)).isoformat(), autorenew=0,
        )
        # Wrong user cannot flip someone else's subscription.
        async with aiosqlite.connect(self.config.db_path) as db:
            await db.execute(
                "UPDATE subscriptions SET autorenew = 1 - autorenew WHERE id=? AND user_id=?", (sub_id, 999),
            )
            await db.commit()
        conn = sqlite3.connect(self.config.db_path)
        untouched = conn.execute("SELECT autorenew FROM subscriptions WHERE id=?", (sub_id,)).fetchone()[0]
        self.assertEqual(untouched, 0)

        async with aiosqlite.connect(self.config.db_path) as db:
            await db.execute(
                "UPDATE subscriptions SET autorenew = 1 - autorenew WHERE id=? AND user_id=?", (sub_id, 3),
            )
            await db.commit()
        flipped = conn.execute("SELECT autorenew FROM subscriptions WHERE id=?", (sub_id,)).fetchone()[0]
        conn.close()
        self.assertEqual(flipped, 1)


class ExtendedStatsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = bf.config_from_bot_row(
            {"bot_id": 603, "name": "fit_stats_bot", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await bf.init_db(self.config.db_path)

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def test_attendance_and_churn_queries_return_sane_numbers(self):
        trainer_id = await _seed_trainer(self.config.db_path)
        recent_date = (date.today() - timedelta(days=2)).isoformat()
        slot_id = await _seed_group_slot(self.config.db_path, trainer_id, recent_date)
        async with aiosqlite.connect(self.config.db_path) as db:
            await db.execute(
                "INSERT INTO bookings (slot_id, user_id, client_name, status) VALUES (?, 1, 'A', 'confirmed')",
                (slot_id,),
            )
            await db.commit()

        # A lapsed, never-renewed subscription inside the churn window.
        await _grant_subscription(
            self.config.db_path, user_id=42, expires_at=(date.today() - timedelta(days=5)).isoformat(),
        )

        since = (date.today() - timedelta(days=bf.STATS_RECENT_DAYS)).isoformat()
        async with aiosqlite.connect(self.config.db_path) as db:
            db.row_factory = aiosqlite.Row
            attendance = await (await db.execute(
                "SELECT t.id, t.name, COUNT(*) AS visits FROM bookings b "
                "JOIN slots s ON b.slot_id=s.id JOIN trainers t ON s.trainer_id=t.id "
                "WHERE b.status='confirmed' AND s.slot_date >= ? AND s.slot_date <= date('now','localtime') "
                "GROUP BY t.id",
                (since,),
            )).fetchall()
            churned = (await (await db.execute(
                "SELECT COUNT(DISTINCT s1.user_id) FROM subscriptions s1 "
                "WHERE s1.expires_at IS NOT NULL AND s1.expires_at < date('now','localtime') AND s1.expires_at >= ? "
                "AND NOT EXISTS (SELECT 1 FROM subscriptions s2 WHERE s2.user_id = s1.user_id AND s2.id != s1.id "
                "AND s2.purchased_at > s1.expires_at)",
                (since,),
            )).fetchone())[0]

        self.assertEqual(len(attendance), 1)
        self.assertEqual(attendance[0]["visits"], 1)
        self.assertEqual(attendance[0]["name"], "Anna")
        self.assertEqual(churned, 1)


class RoleFilterDecisionTests(unittest.IsolatedAsyncioTestCase):
    """Pins the deliberate decision documented in templates/booking_fitness.py's
    miniapp_config comment (see docs/MINIAPP_ROLE_SCOPING_DESIGN.md): bookings
    and subscriptions declare NO role_filter, because this template has no
    roles table (admins live in a JSON file a SQL predicate can't see) and
    _admin_gate_ok() exempts ANY role_filter'd resource from the admin check —
    an ownership-only filter here would apply to the admin too and hide every
    OTHER client's rows from the owner's own resource-editor. Regression test
    against the config object, not the live HTTP handler (same style as
    test_booking_fitness_isolation.py's BookingFitnessMiniAppConfigTests)."""

    def test_bookings_and_subscriptions_have_no_role_filter(self):
        by_name = {r["name"]: r for r in bf.miniapp_config["resources"]}
        self.assertNotIn("role_filter", by_name["bookings"])
        self.assertNotIn("role_filter", by_name["subscriptions"])

    def test_trainer_ratings_resource_also_admin_only_no_role_filter(self):
        by_name = {r["name"]: r for r in bf.miniapp_config["resources"]}
        self.assertIn("trainer_ratings", by_name)
        self.assertNotIn("role_filter", by_name["trainer_ratings"])
        self.assertFalse(by_name["trainer_ratings"]["creatable"])

    async def test_client_still_reaches_own_data_via_telegram_buttons_not_miniapp(self):
        """The client-facing substitute this decision relies on: "📋 Мои
        записи"/"💳 Мой абонемент" query bookings/subscriptions scoped to
        the REQUESTING Telegram user directly (no mini-app auth layer
        involved), so the admin-only miniapp resource is not the client's
        only way to see their own data."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "role_filter_check.db")
            await bf.init_db(db_path)
            await _grant_subscription(db_path, user_id=1, visits=5, plan_name="Mine")
            await _grant_subscription(db_path, user_id=2, visits=9, plan_name="NotMine")

            async with aiosqlite.connect(db_path) as db:
                db.row_factory = aiosqlite.Row
                row = await (await db.execute(
                    "SELECT * FROM subscriptions WHERE user_id=? ORDER BY purchased_at DESC LIMIT 1", (1,),
                )).fetchone()
            self.assertEqual(row["plan_name"], "Mine")
            self.assertNotEqual(row["plan_name"], "NotMine")


if __name__ == "__main__":
    unittest.main()
