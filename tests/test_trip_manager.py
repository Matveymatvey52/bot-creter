"""trip_manager template — miniapp_config schema-drift check.

miniapp_config's declared table/field names must match init_db()'s real
schema — runtime/miniapp_api.py builds SQL directly off these names, so a
drift here would 500 at request time instead of failing a test. See
templates/tour_operator.py's own miniapp_config for the pilot and
tests/test_car_rental.py for the pattern this follows.

user_prefs is intentionally excluded — it uses `user_id` as its primary key
(no `id` column).

Run with: python -m unittest tests.test_trip_manager
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import aiosqlite

from templates import trip_manager


class TripManagerMiniAppConfigTests(unittest.IsolatedAsyncioTestCase):
    def test_miniapp_config_resource_names(self):
        names = {r["name"] for r in trip_manager.miniapp_config["resources"]}
        self.assertEqual(names, {"trips", "items"})

    async def test_miniapp_config_fields_match_real_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "schema_check.db")
            await trip_manager.init_db(db_path)
            async with aiosqlite.connect(db_path) as db:
                for resource in trip_manager.miniapp_config["resources"]:
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
