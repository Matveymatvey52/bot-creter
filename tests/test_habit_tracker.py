"""miniapp_config coverage for the habit_tracker template.

Same criterion as tests/test_car_rental.py's CarRentalMiniAppConfigTests:
miniapp_config's declared table/field names must match init_db()'s real
schema — miniapp_api.py builds SQL directly off these names, so a drift here
would 500 at request time instead of failing a test.

Run with: python -m unittest tests.test_habit_tracker
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import aiosqlite

from templates import habit_tracker


class HabitTrackerMiniAppConfigTests(unittest.IsolatedAsyncioTestCase):
    def test_miniapp_config_resource_names(self):
        names = {r["name"] for r in habit_tracker.miniapp_config["resources"]}
        self.assertEqual(names, {"habits", "habit_checkins"})

    def test_habits_resource_targets_habits_table(self):
        habits = next(r for r in habit_tracker.miniapp_config["resources"] if r["name"] == "habits")
        self.assertEqual(habits["table"], "habits")
        self.assertFalse(habits["creatable"])
        field_names = {f["name"] for f in habits["fields"]}
        self.assertEqual(field_names, {"owner_user_id", "name", "forgiving_mode", "is_active"})

    def test_habit_checkins_resource_targets_habit_checkins_table(self):
        checkins = next(r for r in habit_tracker.miniapp_config["resources"] if r["name"] == "habit_checkins")
        self.assertEqual(checkins["table"], "habit_checkins")
        self.assertFalse(checkins["creatable"])
        field_names = {f["name"] for f in checkins["fields"]}
        self.assertEqual(field_names, {"habit_id", "checkin_date", "done"})

    async def test_miniapp_config_fields_match_real_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "schema_check.db")
            await habit_tracker.init_db(db_path)
            async with aiosqlite.connect(db_path) as db:
                for resource in habit_tracker.miniapp_config["resources"]:
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
