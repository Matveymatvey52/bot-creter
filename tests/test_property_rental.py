"""property_rental template — miniapp_config schema-drift check.

miniapp_config's declared table/field names must match init_db()'s real
schema — runtime/miniapp_api.py builds SQL directly off these names, so a
drift here would 500 at request time instead of failing a test. See
tests/test_repair_tracker.py for the pattern this follows.

Run with: python -m unittest tests.test_property_rental
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import aiosqlite

from templates import property_rental


class PropertyRentalMiniAppConfigTests(unittest.IsolatedAsyncioTestCase):
    def test_miniapp_config_resource_names(self):
        names = {r["name"] for r in property_rental.miniapp_config["resources"]}
        self.assertEqual(names, {"properties", "leases", "rent_payments", "maintenance_requests"})

    async def test_miniapp_config_fields_match_real_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "schema_check.db")
            await property_rental.init_db(db_path)
            async with aiosqlite.connect(db_path) as db:
                for resource in property_rental.miniapp_config["resources"]:
                    cur = await db.execute(f"PRAGMA table_info({resource['table']})")
                    real_columns = {row[1] for row in await cur.fetchall()}
                    declared = {f["name"] for f in resource["fields"]} | {"id"}
                    self.assertTrue(
                        declared.issubset(real_columns),
                        f"{resource['name']}: declared fields {declared} not all in "
                        f"real columns {real_columns}",
                    )

    def test_role_filter_resolve_table_is_property_access(self):
        for resource in property_rental.miniapp_config["resources"]:
            rf = resource.get("role_filter")
            if rf is None:
                continue
            self.assertEqual(rf["resolve"]["table"], "property_access")
            self.assertEqual(rf["resolve"]["identity_column"], "user_id")
            self.assertEqual(rf["resolve"]["role_column"], "role")
            self.assertTrue(rf.get("default_deny", True))

    def test_viewing_requests_has_no_role_filter(self):
        # Prospective tenants aren't authenticated bot users yet — this
        # resource stays behind the plain admin-only gate (see the
        # role_filter design comment near miniapp_config in the template).
        names = {r["name"] for r in property_rental.miniapp_config["resources"]}
        self.assertNotIn("viewing_requests", names)

    async def test_property_access_table_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "schema_check2.db")
            await property_rental.init_db(db_path)
            async with aiosqlite.connect(db_path) as db:
                cur = await db.execute("PRAGMA table_info(property_access)")
                cols = {row[1] for row in await cur.fetchall()}
            self.assertEqual(cols, {"id", "user_id", "role"})

    async def test_cashflow_ledger_tables_initialized(self):
        # features/cashflow_ledger.py's init_cashflow_tables must be called
        # from this template's own init_db (by-convention wiring).
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "schema_check3.db")
            await property_rental.init_db(db_path)
            async with aiosqlite.connect(db_path) as db:
                cur = await db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='cashflow_entries'"
                )
                row = await cur.fetchone()
            self.assertIsNotNone(row)


if __name__ == "__main__":
    unittest.main()
