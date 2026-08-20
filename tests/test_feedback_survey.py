"""miniapp_config coverage for the feedback_survey template.

Same criterion as tests/test_car_rental.py's CarRentalMiniAppConfigTests:
miniapp_config's declared table/field names must match init_db()'s real
schema — miniapp_api.py builds SQL directly off these names, so a drift here
would 500 at request time instead of failing a test.

Run with: python -m unittest tests.test_feedback_survey
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import aiosqlite

from templates import feedback_survey


class FeedbackSurveyMiniAppConfigTests(unittest.IsolatedAsyncioTestCase):
    def test_miniapp_config_resource_names(self):
        names = {r["name"] for r in feedback_survey.miniapp_config["resources"]}
        self.assertEqual(names, {"feedback"})

    def test_feedback_resource_targets_feedback_table(self):
        feedback = next(r for r in feedback_survey.miniapp_config["resources"] if r["name"] == "feedback")
        self.assertEqual(feedback["table"], "feedback")
        self.assertTrue(feedback["creatable"])
        field_names = {f["name"] for f in feedback["fields"]}
        self.assertEqual(field_names, {"rating", "comment", "source", "client_label"})

    async def test_miniapp_config_fields_match_real_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "schema_check.db")
            await feedback_survey.init_db(db_path)
            async with aiosqlite.connect(db_path) as db:
                for resource in feedback_survey.miniapp_config["resources"]:
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
