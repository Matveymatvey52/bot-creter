"""miniapp_config coverage for the event_manager template.

Same criterion as tests/test_car_rental.py's CarRentalMiniAppConfigTests:
miniapp_config's declared table/field names must match init_db()'s real
schema — miniapp_api.py builds SQL directly off these names, so a drift here
would 500 at request time instead of failing a test.

Run with: python -m unittest tests.test_event_manager
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import aiosqlite

from templates import event_manager


class EventManagerMiniAppConfigTests(unittest.IsolatedAsyncioTestCase):
    def test_miniapp_config_resource_names(self):
        names = {r["name"] for r in event_manager.miniapp_config["resources"]}
        self.assertEqual(names, {"events", "guests"})

    def test_events_resource_targets_events_table(self):
        events = next(r for r in event_manager.miniapp_config["resources"] if r["name"] == "events")
        self.assertEqual(events["table"], "events")
        self.assertTrue(events["creatable"])
        self.assertIn("name", {f["name"] for f in events["fields"]})

    def test_guests_resource_targets_guests_table(self):
        guests = next(r for r in event_manager.miniapp_config["resources"] if r["name"] == "guests")
        self.assertEqual(guests["table"], "guests")
        self.assertFalse(guests["creatable"])
        field_names = {f["name"] for f in guests["fields"]}
        self.assertEqual(
            field_names,
            {"event_id", "name", "contact", "rsvp_status", "checked_in"},
        )

    async def test_miniapp_config_fields_match_real_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "schema_check.db")
            await event_manager.init_db(db_path)
            async with aiosqlite.connect(db_path) as db:
                for resource in event_manager.miniapp_config["resources"]:
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
