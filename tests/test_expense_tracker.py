"""miniapp_config coverage for the expense_tracker template.

Same criterion as tests/test_car_rental.py's CarRentalMiniAppConfigTests:
miniapp_config's declared table/field names must match init_db()'s real
schema — miniapp_api.py builds SQL directly off these names, so a drift here
would 500 at request time instead of failing a test.

Run with: python -m unittest tests.test_expense_tracker
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import aiosqlite

from templates import expense_tracker


class ExpenseTrackerMiniAppConfigTests(unittest.IsolatedAsyncioTestCase):
    def test_miniapp_config_resource_names(self):
        names = {r["name"] for r in expense_tracker.miniapp_config["resources"]}
        self.assertEqual(names, {"categories", "expenses"})

    def test_categories_resource_targets_categories_table(self):
        categories = next(r for r in expense_tracker.miniapp_config["resources"] if r["name"] == "categories")
        self.assertEqual(categories["table"], "categories")
        self.assertTrue(categories["creatable"])
        self.assertIn("name", {f["name"] for f in categories["fields"]})

    def test_expenses_resource_targets_expenses_table(self):
        expenses = next(r for r in expense_tracker.miniapp_config["resources"] if r["name"] == "expenses")
        self.assertEqual(expenses["table"], "expenses")
        self.assertTrue(expenses["creatable"])
        field_names = {f["name"] for f in expenses["fields"]}
        self.assertEqual(
            field_names,
            {"category_id", "amount", "expense_date", "comment", "created_by"},
        )

    async def test_miniapp_config_fields_match_real_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "schema_check.db")
            await expense_tracker.init_db(db_path)
            async with aiosqlite.connect(db_path) as db:
                for resource in expense_tracker.miniapp_config["resources"]:
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
