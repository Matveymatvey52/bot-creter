"""feedback_survey template — aggregation correctness tests.

Task 4's explicit requirement: average and distribution must be correct
across different datasets (empty, single value, mixed values, uneven
counts) AND the period filter (week/month/all-time) must actually exclude
rows outside the window, not just decorate the label.

Tests call templates.feedback_survey._stats_query() directly against a
tempfile db_path with hand-inserted rows (explicit created_at timestamps),
rather than going through the Telegram FSM — this isolates aggregation
correctness from collection-flow correctness (already covered by
test_feedback_survey_isolation.py).

Run with: python -m unittest tests.test_feedback_survey_stats
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import aiosqlite

from templates import feedback_survey as fb


async def _insert_raw(db_path: str, rating: int, comment: str | None, created_at: str,
                       source: str = "client", client_user_id: int | None = 1, client_label: str | None = None):
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO feedback (rating, comment, source, client_user_id, client_label, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (rating, comment, source, client_user_id, client_label, created_at),
        )
        await db.commit()


ALL_TIME = ("2000-01-01", date.today().isoformat())


class FeedbackSurveyAggregationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "stats_test.db")
        await fb.init_db(self.db_path)

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def test_empty_dataset_has_zero_count_and_no_crash(self):
        data = await fb._stats_query(self.db_path, *ALL_TIME)
        self.assertEqual(data["count"], 0)
        self.assertEqual(data["dist"], {})
        self.assertEqual(data["comments"], [])
        # _format_stats_text must handle count==0 without touching avg (None).
        text = fb._format_stats_text(data, "Всё время")
        self.assertIn("нет", text)

    async def test_single_rating_average_equals_that_rating(self):
        await _insert_raw(self.db_path, 4, None, "2026-01-01 10:00:00")
        data = await fb._stats_query(self.db_path, *ALL_TIME)
        self.assertEqual(data["count"], 1)
        self.assertAlmostEqual(data["avg"], 4.0)
        self.assertEqual(data["dist"], {4: 1})

    async def test_mixed_ratings_average_and_distribution_are_correct(self):
        # 5,5,5,1,1 -> avg = (15+2)/5 = 3.4 ; dist {5:3, 1:2}
        for rating in (5, 5, 5, 1, 1):
            await _insert_raw(self.db_path, rating, None, "2026-01-01 10:00:00")
        data = await fb._stats_query(self.db_path, *ALL_TIME)
        self.assertEqual(data["count"], 5)
        self.assertAlmostEqual(data["avg"], 3.4)
        self.assertEqual(data["dist"], {5: 3, 1: 2})

    async def test_distribution_omits_ratings_never_submitted(self):
        await _insert_raw(self.db_path, 3, None, "2026-01-01 10:00:00")
        await _insert_raw(self.db_path, 3, None, "2026-01-01 11:00:00")
        data = await fb._stats_query(self.db_path, *ALL_TIME)
        self.assertEqual(data["dist"], {3: 2})
        self.assertNotIn(1, data["dist"])
        self.assertNotIn(5, data["dist"])

    async def test_period_filter_excludes_rows_outside_window(self):
        today = date.today()
        this_month = today.replace(day=1).isoformat()
        old_date = (today.replace(day=1) - timedelta(days=400)).isoformat()

        await _insert_raw(self.db_path, 5, None, f"{today.isoformat()} 09:00:00")
        await _insert_raw(self.db_path, 1, None, f"{old_date} 09:00:00")

        month_data = await fb._stats_query(self.db_path, this_month, today.isoformat())
        self.assertEqual(month_data["count"], 1)
        self.assertEqual(month_data["dist"], {5: 1})

        all_time_data = await fb._stats_query(self.db_path, "2000-01-01", today.isoformat())
        self.assertEqual(all_time_data["count"], 2)
        self.assertEqual(all_time_data["dist"], {5: 1, 1: 1})

    async def test_week_filter_excludes_last_months_row(self):
        today = date.today()
        week_start = (today - timedelta(days=today.weekday())).isoformat()
        last_month = (today.replace(day=1) - timedelta(days=1)).isoformat()

        await _insert_raw(self.db_path, 4, None, f"{today.isoformat()} 12:00:00")
        await _insert_raw(self.db_path, 2, None, f"{last_month} 12:00:00")

        week_data = await fb._stats_query(self.db_path, week_start, today.isoformat())
        self.assertEqual(week_data["count"], 1)
        self.assertEqual(week_data["dist"], {4: 1})

    async def test_comments_are_ordered_newest_first_and_limited(self):
        for i in range(fb.RECENT_COMMENTS_LIMIT + 5):
            await _insert_raw(self.db_path, 3, f"comment-{i}", f"2026-01-01 10:{i:02d}:00")
        data = await fb._stats_query(self.db_path, *ALL_TIME)
        self.assertEqual(len(data["comments"]), fb.RECENT_COMMENTS_LIMIT)
        newest_comment = data["comments"][0][1]
        self.assertEqual(newest_comment, f"comment-{fb.RECENT_COMMENTS_LIMIT + 4}")

    async def test_rows_without_comment_are_excluded_from_recent_comments(self):
        await _insert_raw(self.db_path, 5, None, "2026-01-01 10:00:00")
        await _insert_raw(self.db_path, 4, "actually had something to say", "2026-01-01 11:00:00")
        data = await fb._stats_query(self.db_path, *ALL_TIME)
        self.assertEqual(len(data["comments"]), 1)
        self.assertEqual(data["comments"][0][1], "actually had something to say")

    async def test_admin_sourced_row_client_label_surfaces_in_formatted_text(self):
        await _insert_raw(
            self.db_path, 5, "по телефону сказала, что супер", "2026-01-01 10:00:00",
            source="admin", client_user_id=None, client_label="Анна",
        )
        data = await fb._stats_query(self.db_path, *ALL_TIME)
        text = fb._format_stats_text(data, "Всё время")
        self.assertIn("Анна", text)
        self.assertIn("по телефону сказала, что супер", text)

    async def test_bar_rendering_scales_relative_to_max_count_not_absolute(self):
        # Ratings 5 (x10) and 1 (x1): the 1-star bar must be shorter than
        # the 5-star bar, and non-empty (not rounded down to nothing).
        self.assertEqual(fb._bar(0, 10), "")
        bar_max = fb._bar(10, 10)
        bar_min = fb._bar(1, 10)
        self.assertGreater(len(bar_max), len(bar_min))
        self.assertGreaterEqual(len(bar_min), 1)


if __name__ == "__main__":
    unittest.main()
