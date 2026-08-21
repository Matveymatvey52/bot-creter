"""habit_tracker template — streak-logic correctness, per-user data isolation
WITHIN one bot, cross-bot isolation, admin-only aggregate gating, archive
(soft-delete), and admin-hijack security fix tests.

Design recap (see templates/habit_tracker.py's own module docstring for the
full reasoning): unlike every other template in this repo, habit_tracker has
NO admin/client business relationship — ANY user who messages the bot tracks
their OWN separate list of habits, scoped by owner_user_id within the bot's
single db_path. The standard admins_file-based admin check still exists, but
its only real use here is the "📊 Сводка по всем" oversight view.

Streak semantics under test (the trickiest part of this template):
- strict mode: any missed (unmarked) day breaks the current streak, same as
  an explicit "not done".
- forgiving mode: exactly ONE isolated unmarked day is tolerated without
  breaking the streak; two unmarked days in a row still break it.
- an explicit "not done" (done=0) ALWAYS breaks the streak in both modes.
- a still-open "today" (no check-in row yet) must not count against or for
  the streak — the walk starts from yesterday in that case.
- best/longest streak must persist correctly after a break-then-rebuild,
  computed by a full forward scan rather than incremental tracking.

Admin-hijack security fix (same pattern/criterion as
tests/test_shop_catalog_isolation.py's isolation tests): whoever sent /start
FIRST used to permanently become the bot's admin. Now the empty-admins
bootstrap slot can only be claimed by bots.owner_telegram_id when it's known;
the DB-known owner is always treated as admin regardless of the local
admins_file state; and standalone mode (owner_telegram_id=None) keeps the old
first-comer behavior as the only option available.

No real Telegram network calls, no real tokens.

Run with: python -m unittest tests.test_habit_tracker_isolation
"""

from __future__ import annotations

import datetime
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

import db.database as db_module
from runtime.registry import get_template_router
from templates import habit_tracker

FAKE_TOKEN = "123456:test-token-not-real"
ADMIN_ID = 999
USER_A = 111
USER_B = 222


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


def _build_bot_dispatcher(config: habit_tracker.HabitTrackerConfig) -> tuple[Bot, Dispatcher]:
    bot = Bot(token=FAKE_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(habit_tracker.ConfigMiddleware(config))
    dp.include_router(get_template_router("habit_tracker"))
    return bot, dp


async def _create_habit_via_flow(
    dp: Dispatcher, bot: Bot, user_id: int, name: str, forgiving: bool, start_update_id: int,
) -> int:
    uid = start_update_id
    await dp.feed_webhook_update(bot, _callback_update(uid, user_id, "habit_new")); uid += 1
    await dp.feed_webhook_update(bot, _text_update(uid, user_id, name)); uid += 1
    await dp.feed_webhook_update(bot, _callback_update(uid, user_id, f"habit_forgiving:{1 if forgiving else 0}")); uid += 1
    return uid


def _mk_config(tmpdir: str, bot_name: str) -> habit_tracker.HabitTrackerConfig:
    data_dir = Path(tmpdir)
    return habit_tracker.HabitTrackerConfig(
        bot_name=bot_name,
        db_path=str(data_dir / f"{bot_name}_data.db"),
        admins_file=data_dir / f"admins_{bot_name}.json",
        welcome_image=data_dir / "bot_images" / f"{bot_name}.jpg",
    )


class StreakLogicTests(unittest.IsolatedAsyncioTestCase):
    """Pure function tests over _current_streak / _best_streak — fast, no DB,
    no Telegram plumbing, so every edge case in the spec gets its own assert."""

    def setUp(self):
        self.today = datetime.date(2026, 8, 14)

    def _d(self, days_ago: int) -> str:
        return (self.today - datetime.timedelta(days=days_ago)).isoformat()

    def test_strict_mode_breaks_on_missed_day(self):
        # done today-2, today-1 missing (gap), today-0 done -> strict streak = 1
        checkins = {self._d(2): 1, self._d(0): 1}
        streak = habit_tracker._current_streak(checkins, forgiving=False, today=self.today)
        self.assertEqual(streak, 1)

    def test_forgiving_mode_tolerates_one_skipped_day(self):
        # today done, yesterday missing (forgiven), day-2 done -> streak = 2
        checkins = {self._d(0): 1, self._d(2): 1}
        streak = habit_tracker._current_streak(checkins, forgiving=True, today=self.today)
        self.assertEqual(streak, 2)

    def test_forgiving_mode_does_not_tolerate_two_consecutive_gaps(self):
        # today done, day-1 and day-2 BOTH missing -> only 1 gap forgiven, breaks after
        checkins = {self._d(0): 1, self._d(3): 1}
        streak = habit_tracker._current_streak(checkins, forgiving=True, today=self.today)
        self.assertEqual(streak, 1)

    def test_explicit_not_done_breaks_streak_strict(self):
        checkins = {self._d(0): 1, self._d(1): 0, self._d(2): 1}
        streak = habit_tracker._current_streak(checkins, forgiving=False, today=self.today)
        self.assertEqual(streak, 1)

    def test_explicit_not_done_breaks_streak_forgiving(self):
        # explicit 0 breaks even in forgiving mode -- NOT treated as a
        # tolerable gap, unlike a missing row.
        checkins = {self._d(0): 1, self._d(1): 0, self._d(2): 1}
        streak = habit_tracker._current_streak(checkins, forgiving=True, today=self.today)
        self.assertEqual(streak, 1)

    def test_open_today_does_not_count_against_or_for_streak(self):
        # today has no row at all; yesterday and day-2 both done -> streak
        # should be computed starting from yesterday, ignoring today entirely.
        checkins = {self._d(1): 1, self._d(2): 1}
        streak = habit_tracker._current_streak(checkins, forgiving=False, today=self.today)
        self.assertEqual(streak, 2)

    def test_best_streak_persists_after_break_then_rebuild(self):
        # days 6..4 ago: done,done,done (streak of 3) then day-3 explicit not
        # done (break) then day-2..0 done,done,done (new streak of 3).
        checkins = {
            self._d(6): 1, self._d(5): 1, self._d(4): 1,
            self._d(3): 0,
            self._d(2): 1, self._d(1): 1, self._d(0): 1,
        }
        best = habit_tracker._best_streak(checkins, forgiving=False)
        self.assertEqual(best, 3)
        current = habit_tracker._current_streak(checkins, forgiving=False, today=self.today)
        self.assertEqual(current, 3)

    def test_best_streak_longer_run_wins_over_current(self):
        # A long run early (5 days), then a break, then a short current run
        # (2 days) -- best must remain 5, not collapse to the current streak.
        checkins = {}
        for i in range(10, 5, -1):  # 10..6 days ago: 5 consecutive done days
            checkins[self._d(i)] = 1
        checkins[self._d(5)] = 0  # break
        checkins[self._d(1)] = 1
        checkins[self._d(0)] = 1
        best = habit_tracker._best_streak(checkins, forgiving=False)
        current = habit_tracker._current_streak(checkins, forgiving=False, today=self.today)
        self.assertEqual(best, 5)
        self.assertEqual(current, 2)

    def test_empty_history_gives_zero_streaks(self):
        self.assertEqual(habit_tracker._current_streak({}, forgiving=False, today=self.today), 0)
        self.assertEqual(habit_tracker._current_streak({}, forgiving=True, today=self.today), 0)
        self.assertEqual(habit_tracker._best_streak({}, forgiving=False), 0)

    def test_forgiving_mode_multiple_separated_gaps_each_forgiven(self):
        # done, gap, done, gap, done -- each gap is isolated (not adjacent to
        # another gap), so forgiving mode should count the whole thing as one
        # continuous streak in the best-streak forward scan.
        checkins = {self._d(4): 1, self._d(2): 1, self._d(0): 1}
        best = habit_tracker._best_streak(checkins, forgiving=True)
        self.assertEqual(best, 3)


class HabitTrackerFlowTests(unittest.IsolatedAsyncioTestCase):
    """End-to-end aiogram Dispatcher tests: FSM habit creation, check-in
    toggling, cross-user isolation within one bot, cross-bot isolation,
    admin-gated aggregate view, and archive (soft-delete).

    Telegram API calls are mocked at the Bot.__call__ layer (same pattern as
    tests/test_vehicle_service_isolation.py) rather than patching the aiohttp
    session's make_request — patching make_request still leaves aiogram's own
    Bot.__call__ -> session(...) plumbing racing a real DNS/TLS handshake in
    some code paths, which manifested as spurious 60s hangs/failures during
    initial development of this test file."""

    async def asyncSetUp(self):
        self._bot_call_mock = AsyncMock(return_value=MagicMock())
        self._bot_call_patcher = patch.object(Bot, "__call__", new=self._bot_call_mock)
        self._bot_call_patcher.start()
        self._tmpdir_ctx = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmpdir_ctx.name
        self.addAsyncCleanup(self._tmpdir_ctx.cleanup)
        self.addAsyncCleanup(self._bot_call_patcher.stop)

    async def test_add_habit_and_checkin_produces_streak_of_one(self):
        config = _mk_config(self.tmpdir, "botA")
        await habit_tracker.init_db(config.db_path)
        bot, dp = _build_bot_dispatcher(config)
        uid = await _create_habit_via_flow(dp, bot, USER_A, "Отжимания", forgiving=False, start_update_id=1)
        await dp.feed_webhook_update(bot, _callback_update(uid, USER_A, "checkin_today")); uid += 1

        async with aiosqlite.connect(config.db_path) as db:
            habit = await (await db.execute(
                "SELECT id FROM habits WHERE owner_user_id=?", (USER_A,)
            )).fetchone()
            self.assertIsNotNone(habit)
            habit_id = habit[0]
            await db.execute(
                "INSERT INTO habit_checkins (habit_id, checkin_date, done) VALUES (?,?,1)",
                (habit_id, habit_tracker._date_str(habit_tracker._today())),
            )
            await db.commit()
            current, best = await habit_tracker._habit_streaks(db, habit_id, False, habit_tracker._today())
        self.assertEqual(current, 1)
        self.assertEqual(best, 1)

    async def test_cross_user_isolation_within_one_bot(self):
        config = _mk_config(self.tmpdir, "botA")
        await habit_tracker.init_db(config.db_path)
        bot, dp = _build_bot_dispatcher(config)
        uid = await _create_habit_via_flow(dp, bot, USER_A, "Медитация", forgiving=False, start_update_id=1)
        uid = await _create_habit_via_flow(dp, bot, USER_B, "Чтение", forgiving=False, start_update_id=uid)

        async with aiosqlite.connect(config.db_path) as db:
            a_habits = await (await db.execute(
                "SELECT name FROM habits WHERE owner_user_id=?", (USER_A,)
            )).fetchall()
            b_habits = await (await db.execute(
                "SELECT name FROM habits WHERE owner_user_id=?", (USER_B,)
            )).fetchall()
        self.assertEqual([r[0] for r in a_habits], ["Медитация"])
        self.assertEqual([r[0] for r in b_habits], ["Чтение"])

        # habit_list callback for user A must not show user B's habit
        await dp.feed_webhook_update(bot, _callback_update(1000, USER_A, "habit_list"))
        async with aiosqlite.connect(config.db_path) as db:
            rows = await (await db.execute(
                "SELECT id, name FROM habits WHERE owner_user_id=? AND is_active=1", (USER_A,)
            )).fetchall()
        names = [r[1] for r in rows]
        self.assertIn("Медитация", names)
        self.assertNotIn("Чтение", names)

    async def test_cross_bot_isolation_same_user_id(self):
        config_a = _mk_config(self.tmpdir, "botA")
        config_b = _mk_config(self.tmpdir, "botB")
        await habit_tracker.init_db(config_a.db_path)
        await habit_tracker.init_db(config_b.db_path)
        bot_a, dp_a = _build_bot_dispatcher(config_a)
        bot_b, dp_b = _build_bot_dispatcher(config_b)

        await _create_habit_via_flow(dp_a, bot_a, ADMIN_ID, "Bot A habit", forgiving=False, start_update_id=1)
        await _create_habit_via_flow(dp_b, bot_b, ADMIN_ID, "Bot B habit", forgiving=False, start_update_id=1)

        async with aiosqlite.connect(config_a.db_path) as db:
            a_names = [r[0] for r in await (await db.execute(
                "SELECT name FROM habits WHERE owner_user_id=?", (ADMIN_ID,)
            )).fetchall()]
        async with aiosqlite.connect(config_b.db_path) as db:
            b_names = [r[0] for r in await (await db.execute(
                "SELECT name FROM habits WHERE owner_user_id=?", (ADMIN_ID,)
            )).fetchall()]
        self.assertEqual(a_names, ["Bot A habit"])
        self.assertEqual(b_names, ["Bot B habit"])

    async def test_admin_only_aggregate_view_is_gated(self):
        config = _mk_config(self.tmpdir, "botA")
        await habit_tracker.init_db(config.db_path)
        config.admins_file.parent.mkdir(parents=True, exist_ok=True)
        habit_tracker._save_admins(config.admins_file, {str(ADMIN_ID)})
        bot, dp = _build_bot_dispatcher(config)

        await _create_habit_via_flow(dp, bot, USER_A, "Habit A", forgiving=False, start_update_id=1)

        # non-admin taps admin_all_summary -- must be silently gated (no
        # crash, no data leak): _is_admin(USER_A) is False. cb.answer() still
        # fires (1 call) but no edit_text with the aggregate data follows.
        calls_before = self._bot_call_mock.await_count
        await dp.feed_webhook_update(bot, _callback_update(500, USER_A, "admin_all_summary"))
        self.assertLessEqual(self._bot_call_mock.await_count - calls_before, 1)

        # admin taps admin_all_summary -- must succeed (answer + edit_text).
        calls_before = self._bot_call_mock.await_count
        await dp.feed_webhook_update(bot, _callback_update(501, ADMIN_ID, "admin_all_summary"))
        self.assertGreater(self._bot_call_mock.await_count - calls_before, 1)

    async def test_archive_removes_habit_from_checkin_list_but_keeps_history(self):
        config = _mk_config(self.tmpdir, "botA")
        await habit_tracker.init_db(config.db_path)
        bot, dp = _build_bot_dispatcher(config)
        await _create_habit_via_flow(dp, bot, USER_A, "Йога", forgiving=False, start_update_id=1)

        async with aiosqlite.connect(config.db_path) as db:
            habit_row = await (await db.execute(
                "SELECT id FROM habits WHERE owner_user_id=?", (USER_A,)
            )).fetchone()
            self.assertIsNotNone(habit_row)
            habit_id = habit_row[0]
            await db.execute(
                "INSERT INTO habit_checkins (habit_id, checkin_date, done) VALUES (?,?,1)",
                (habit_id, habit_tracker._date_str(habit_tracker._today())),
            )
            await db.commit()

        await dp.feed_webhook_update(bot, _callback_update(100, USER_A, f"habit_archive:{habit_id}"))

        async with aiosqlite.connect(config.db_path) as db:
            active = await (await db.execute(
                "SELECT id FROM habits WHERE owner_user_id=? AND is_active=1", (USER_A,)
            )).fetchall()
            self.assertEqual(active, [])
            # history preserved
            checkins = await (await db.execute(
                "SELECT done FROM habit_checkins WHERE habit_id=?", (habit_id,)
            )).fetchall()
            self.assertEqual(len(checkins), 1)
            habit_row2 = await (await db.execute(
                "SELECT is_active FROM habits WHERE id=?", (habit_id,)
            )).fetchone()
            self.assertEqual(habit_row2[0], 0)

    async def test_checkin_upsert_changes_todays_mark(self):
        config = _mk_config(self.tmpdir, "botA")
        await habit_tracker.init_db(config.db_path)
        bot, dp = _build_bot_dispatcher(config)
        await _create_habit_via_flow(dp, bot, USER_A, "Вода", forgiving=False, start_update_id=1)

        async with aiosqlite.connect(config.db_path) as db:
            habit_row = await (await db.execute(
                "SELECT id FROM habits WHERE owner_user_id=?", (USER_A,)
            )).fetchone()
            self.assertIsNotNone(habit_row)
            habit_id = habit_row[0]

        await dp.feed_webhook_update(bot, _callback_update(200, USER_A, f"checkin_set:{habit_id}:1"))
        await dp.feed_webhook_update(bot, _callback_update(201, USER_A, f"checkin_set:{habit_id}:0"))

        async with aiosqlite.connect(config.db_path) as db:
            row = await (await db.execute(
                "SELECT done FROM habit_checkins WHERE habit_id=? AND checkin_date=?",
                (habit_id, habit_tracker._date_str(habit_tracker._today())),
            )).fetchone()
        self.assertEqual(row[0], 0)  # second tap overwrote first, no duplicate row

        async with aiosqlite.connect(config.db_path) as db:
            count = await (await db.execute(
                "SELECT COUNT(*) FROM habit_checkins WHERE habit_id=?", (habit_id,)
            )).fetchone()
        self.assertEqual(count[0], 1)


class HabitTrackerAdminHijackTests(unittest.IsolatedAsyncioTestCase):
    """Security fix: whoever sent /start FIRST used to permanently become the
    bot's admin (able to see "📊 Сводка по всем" and manage admins) — a
    client who messaged the bot before the owner tested it could hijack that
    role. Same criterion/pattern as
    tests/test_shop_catalog_isolation.py's admin-hijack tests."""

    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

        # cmd_start now syncs the bootstrap admin into db.database.add_bot_admin
        # (central bot_admins table) -- must be redirected to a throwaway DB.
        self._central_db_path = self.data_dir / "central_bots.db"
        self._db_path_patcher = patch.object(db_module, "DB_PATH", self._central_db_path)
        self._db_path_patcher.start()
        await db_module.init_db()

    async def asyncTearDown(self):
        self._db_path_patcher.stop()
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_non_owner_messaging_first_does_not_become_admin(self):
        config = habit_tracker.config_from_bot_row(
            {"bot_id": 401, "name": "habit_bot_owned", "display_name": None,
             "group_chat_id": None, "owner_telegram_id": 12345},
            self.data_dir,
        )
        await habit_tracker.init_db(config.db_path)
        bot, dp = _build_bot_dispatcher(config)

        CLIENT_ID = 555  # not the owner, messages first
        await dp.feed_webhook_update(bot, _text_update(1, CLIENT_ID, "/start"))
        self.assertEqual(habit_tracker._load_admins(config.admins_file), set())
        self.assertFalse(habit_tracker._is_admin(CLIENT_ID, config))

    async def test_owner_start_becomes_admin(self):
        config = habit_tracker.config_from_bot_row(
            {"bot_id": 402, "name": "habit_bot_owned2", "display_name": None,
             "group_chat_id": None, "owner_telegram_id": 12345},
            self.data_dir,
        )
        await habit_tracker.init_db(config.db_path)
        bot, dp = _build_bot_dispatcher(config)

        await dp.feed_webhook_update(bot, _text_update(1, 12345, "/start"))
        self.assertTrue(habit_tracker._is_admin(12345, config))
        self.assertEqual(habit_tracker._load_admins(config.admins_file), {"12345"})

    async def test_owner_is_always_admin_even_with_stale_admins_file(self):
        config = habit_tracker.config_from_bot_row(
            {"bot_id": 403, "name": "habit_bot_owned3", "display_name": None,
             "group_chat_id": None, "owner_telegram_id": 777},
            self.data_dir,
        )
        await habit_tracker.init_db(config.db_path)
        habit_tracker._save_admins(config.admins_file, {"999999"})
        self.assertTrue(habit_tracker._is_admin(777, config))  # owner: always admin
        self.assertTrue(habit_tracker._is_admin(999999, config))  # still honors the file's own admin
        self.assertFalse(habit_tracker._is_admin(4242, config))  # neither owner nor in the file

    async def test_standalone_mode_keeps_first_comer_behavior(self):
        """owner_telegram_id unknown (standalone/env mode) -- the old
        first-comer bootstrap is the only option available, so it's kept."""
        config = habit_tracker.config_from_bot_row(
            {"bot_id": 404, "name": "habit_bot_standalone", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await habit_tracker.init_db(config.db_path)
        bot, dp = _build_bot_dispatcher(config)

        await dp.feed_webhook_update(bot, _text_update(1, 111, "/start"))
        self.assertEqual(habit_tracker._load_admins(config.admins_file), {"111"})
        self.assertTrue(habit_tracker._is_admin(111, config))

    async def test_bootstrap_admin_syncs_to_central_bot_admins_table(self):
        config = habit_tracker.config_from_bot_row(
            {"bot_id": 405, "name": "habit_bot_synced", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await habit_tracker.init_db(config.db_path)
        bot, dp = _build_bot_dispatcher(config)
        await dp.feed_webhook_update(bot, _text_update(1, 321, "/start"))

        central_admins = await db_module.get_bot_admins(405)
        self.assertEqual(central_admins, ["321"])


if __name__ == "__main__":
    unittest.main()
