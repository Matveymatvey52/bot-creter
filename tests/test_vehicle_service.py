"""vehicle_service template — miniapp_config schema-drift check.

miniapp_config's declared table/field names must match init_db()'s real
schema — runtime/miniapp_api.py builds SQL directly off these names, so a
drift here would 500 at request time instead of failing a test. See
templates/tour_operator.py's own miniapp_config for the pilot and
tests/test_car_rental.py for the pattern this follows.

Run with: python -m unittest tests.test_vehicle_service
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import aiosqlite

from templates import vehicle_service


class VehicleServiceMiniAppConfigTests(unittest.IsolatedAsyncioTestCase):
    def test_miniapp_config_resource_names(self):
        names = {r["name"] for r in vehicle_service.miniapp_config["resources"]}
        self.assertEqual(names, {"service_requests", "vehicles"})

    async def test_miniapp_config_fields_match_real_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "schema_check.db")
            await vehicle_service.init_db(db_path)
            async with aiosqlite.connect(db_path) as db:
                for resource in vehicle_service.miniapp_config["resources"]:
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
