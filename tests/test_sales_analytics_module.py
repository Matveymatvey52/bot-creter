"""features/sales_analytics.py — compute_metrics() and its identifier-safety
helpers. See docs/SALES_ANALYTICS_DESIGN.md.

Same defensive-SQL posture as tests/test_office_events_registry_wiring.py's
coverage of generic_on_office_event(): a stale/hallucinated office_hook_config
must only ever degrade metrics (or return None), never raise or produce
unsafe SQL. Uses a real temp sqlite file (not a mock) since the whole point
under test is real SQL against a real schema.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta

import aiosqlite

from features.sales_analytics import compute_metrics, weekly_record_count, _resolve_config


def _now_iso(days_ago: float = 0) -> str:
    # Naive/local time, not UTC — features/sales_analytics.py's cutoffs are
    # computed via SQLite's datetime('now','localtime', ...) (see
    # _period_start_modifier's docstring), matching how templates actually
    # write created_at. Using a UTC timestamp here would test the wrong
    # thing on any machine whose local timezone isn't UTC.
    dt = datetime.now() - timedelta(days=days_ago)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


async def _make_db(rows: list[tuple[int, str, str]]) -> str:
    """rows: (customer_id, created_at_iso, table='bookings')."""
    tmpdir = tempfile.mkdtemp()
    db_path = os.path.join(tmpdir, "bot.db")
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "CREATE TABLE bookings (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "customer_chat_id INTEGER, created_at TEXT, notes TEXT)"
        )
        for customer_id, created_at, notes in rows:
            await db.execute(
                "INSERT INTO bookings (customer_chat_id, created_at, notes) VALUES (?, ?, ?)",
                (customer_id, created_at, notes),
            )
        await db.commit()
    return db_path


class ResolveConfigTests(unittest.TestCase):
    def test_none_hook_config_returns_none(self):
        self.assertIsNone(_resolve_config(None))

    def test_empty_dict_returns_none(self):
        self.assertIsNone(_resolve_config({}))

    def test_missing_table_returns_none(self):
        self.assertIsNone(_resolve_config({"match_field": "x"}))

    def test_valid_full_config_resolves(self):
        result = _resolve_config({"table": "bookings", "match_field": "customer_chat_id", "created_at_field": "created_at"})
        self.assertEqual(result, ("bookings", "customer_chat_id", "created_at"))

    def test_table_only_resolves_with_none_fields(self):
        result = _resolve_config({"table": "bookings", "match_field": None, "created_at_field": None})
        self.assertEqual(result, ("bookings", None, None))

    def test_non_identifier_table_returns_none(self):
        # e.g. a hallucinated/malicious value like "bookings; DROP TABLE x"
        self.assertIsNone(_resolve_config({"table": "bookings; DROP TABLE x"}))

    def test_non_identifier_match_field_degrades_field_only(self):
        result = _resolve_config({"table": "bookings", "match_field": "bad field!"})
        self.assertEqual(result, ("bookings", None, None))

    def test_non_identifier_created_at_field_degrades_field_only(self):
        result = _resolve_config({"table": "bookings", "created_at_field": "bad;field"})
        self.assertEqual(result, ("bookings", None, None))


class ComputeMetricsTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_hook_config_returns_none(self):
        db_path = await _make_db([])
        result = await compute_metrics(db_path, None, "week")
        self.assertIsNone(result)

    async def test_hallucinated_table_returns_none(self):
        db_path = await _make_db([])
        hook_config = {"table": "does_not_exist", "match_field": None, "created_at_field": None}
        result = await compute_metrics(db_path, hook_config, "week")
        self.assertIsNone(result)

    async def test_total_count_with_no_match_or_created_at_field(self):
        db_path = await _make_db([(1, _now_iso(), "a"), (2, _now_iso(), "b")])
        hook_config = {"table": "bookings", "match_field": None, "created_at_field": None}
        result = await compute_metrics(db_path, hook_config, "week")
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["volume"], [])
        self.assertIsNone(result["delta_pct"])
        self.assertEqual(result["top_customers"], [])
        self.assertIsNone(result["new_vs_returning"])

    async def test_hallucinated_match_field_degrades_that_metric_only(self):
        db_path = await _make_db([(1, _now_iso(), "a")])
        hook_config = {"table": "bookings", "match_field": "no_such_column", "created_at_field": "created_at"}
        result = await compute_metrics(db_path, hook_config, "week")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["top_customers"], [])  # match_field silently dropped
        self.assertEqual(len(result["volume"]), 1)  # created_at_field still valid

    async def test_hallucinated_created_at_field_degrades_that_metric_only(self):
        db_path = await _make_db([(1, _now_iso(), "a")])
        hook_config = {"table": "bookings", "match_field": "customer_chat_id", "created_at_field": "no_such_column"}
        result = await compute_metrics(db_path, hook_config, "week")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["volume"], [])
        self.assertEqual(len(result["top_customers"]), 1)  # match_field still valid

    async def test_volume_over_time_buckets_by_day_within_period(self):
        db_path = await _make_db(
            [
                (1, _now_iso(), "today"),
                (2, _now_iso(), "today too"),
                (3, _now_iso(days_ago=1), "yesterday"),
                (4, _now_iso(days_ago=30), "outside week"),
            ]
        )
        hook_config = {"table": "bookings", "match_field": None, "created_at_field": "created_at"}
        result = await compute_metrics(db_path, hook_config, "week")
        self.assertEqual(result["total"], 4)
        total_bucketed = sum(p["count"] for p in result["volume"])
        self.assertEqual(total_bucketed, 3)  # the 30-days-ago row excluded from a week window

    async def test_top_customers_ranked_by_record_count(self):
        db_path = await _make_db(
            [
                (100, _now_iso(), "a"),
                (100, _now_iso(), "b"),
                (100, _now_iso(), "c"),
                (200, _now_iso(), "d"),
            ]
        )
        hook_config = {"table": "bookings", "match_field": "customer_chat_id", "created_at_field": None}
        result = await compute_metrics(db_path, hook_config, "week")
        self.assertEqual(result["top_customers"][0], {"match_field": 100, "count": 3})
        self.assertEqual(result["top_customers"][1], {"match_field": 200, "count": 1})

    async def test_new_vs_returning_within_period(self):
        # customer 1: first record 10 days ago (outside week), another 1 day ago -> returning
        # customer 2: only record is today -> new
        db_path = await _make_db(
            [
                (1, _now_iso(days_ago=10), "old"),
                (1, _now_iso(days_ago=1), "recent"),
                (2, _now_iso(), "brand new"),
            ]
        )
        hook_config = {"table": "bookings", "match_field": "customer_chat_id", "created_at_field": "created_at"}
        result = await compute_metrics(db_path, hook_config, "week")
        self.assertEqual(result["new_vs_returning"], {"new": 1, "returning": 1})

    async def test_delta_pct_none_when_prior_period_empty(self):
        db_path = await _make_db([(1, _now_iso(), "a")])
        hook_config = {"table": "bookings", "match_field": None, "created_at_field": "created_at"}
        result = await compute_metrics(db_path, hook_config, "week")
        self.assertIsNone(result["delta_pct"])

    async def test_delta_pct_computed_when_prior_period_has_records(self):
        db_path = await _make_db(
            [
                (1, _now_iso(), "current a"),
                (2, _now_iso(), "current b"),
                (3, _now_iso(days_ago=10), "prior a"),  # within the prior 7-14 day window
            ]
        )
        hook_config = {"table": "bookings", "match_field": None, "created_at_field": "created_at"}
        result = await compute_metrics(db_path, hook_config, "week")
        # current=2, prior=1 -> +100%
        self.assertEqual(result["delta_pct"], 100.0)

    async def test_sql_injection_style_column_name_never_executes(self):
        db_path = await _make_db([(1, _now_iso(), "a")])
        hook_config = {
            "table": "bookings",
            "match_field": "customer_chat_id\" ; DROP TABLE bookings; --",
            "created_at_field": None,
        }
        result = await compute_metrics(db_path, hook_config, "week")
        self.assertEqual(result["total"], 1)  # table still exists — nothing dropped
        self.assertEqual(result["top_customers"], [])  # malicious field simply rejected

    async def test_reserved_word_column_name_is_quoted_safely(self):
        tmpdir = tempfile.mkdtemp()
        db_path = os.path.join(tmpdir, "bot.db")
        async with aiosqlite.connect(db_path) as db:
            # "order" and "group" are SQLite reserved words but valid column
            # names — same case features/office_events.py's own tests cover.
            await db.execute('CREATE TABLE "order" ("group" INTEGER, created_at TEXT)')
            await db.execute('INSERT INTO "order" ("group", created_at) VALUES (?, ?)', (7, _now_iso()))
            await db.commit()
        hook_config = {"table": "order", "match_field": "group", "created_at_field": "created_at"}
        result = await compute_metrics(db_path, hook_config, "week")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["top_customers"], [{"match_field": 7, "count": 1}])


class WeeklyRecordCountTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_hook_config_returns_none(self):
        db_path = await _make_db([])
        result = await weekly_record_count(db_path, None)
        self.assertIsNone(result)

    async def test_hallucinated_table_returns_none(self):
        db_path = await _make_db([])
        hook_config = {"table": "does_not_exist", "match_field": None, "created_at_field": None}
        result = await weekly_record_count(db_path, hook_config)
        self.assertIsNone(result)

    async def test_counts_only_rows_within_the_last_week(self):
        db_path = await _make_db(
            [
                (1, _now_iso(), "today"),
                (2, _now_iso(days_ago=6), "within week"),
                (3, _now_iso(days_ago=30), "outside week"),
            ]
        )
        hook_config = {"table": "bookings", "match_field": None, "created_at_field": "created_at"}
        result = await weekly_record_count(db_path, hook_config)
        self.assertEqual(result, 2)

    async def test_no_created_at_field_falls_back_to_all_time_count(self):
        db_path = await _make_db([(1, _now_iso(days_ago=90), "old"), (2, _now_iso(), "new")])
        hook_config = {"table": "bookings", "match_field": None, "created_at_field": None}
        result = await weekly_record_count(db_path, hook_config)
        self.assertEqual(result, 2)  # no way to scope to "this week" — total row count instead

    async def test_hallucinated_created_at_field_falls_back_to_all_time_count(self):
        db_path = await _make_db([(1, _now_iso(days_ago=90), "old"), (2, _now_iso(), "new")])
        hook_config = {"table": "bookings", "match_field": None, "created_at_field": "no_such_column"}
        result = await weekly_record_count(db_path, hook_config)
        self.assertEqual(result, 2)


if __name__ == "__main__":
    unittest.main()
