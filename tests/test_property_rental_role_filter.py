"""property_rental template — role_filter isolation via the real mini-app REST
layer (runtime/miniapp_api.py), using property_rental's REAL miniapp_config
and init_db schema (not a fixture) — same harness style as
tests/test_miniapp_api.py, extended to prove the ownership claims in
docs/MINIAPP_ROLE_SCOPING_DESIGN.md actually hold for this template:

- owner sees every lease/payment/property/maintenance-request.
- tenant A cannot see tenant B's lease or rent_payments, even by guessing a
  numeric /leases/<id> or /rent_payments/<id> URL.
- a tenant CAN create their own maintenance_requests row via the mini-app,
  and tenant_user_id is force-set to the authenticated caller (cannot spoof
  another tenant's id in the POST body).

Run with: python -m unittest tests.test_property_rental_role_filter
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiosqlite
from aiohttp.test_utils import TestClient, TestServer

from runtime.registry import BotEntry
from runtime.webhook_app import create_app
from runtime.miniapp_api import mint_magic_link_token, register_routes
from templates import property_rental

FAKE_TOKEN = "123456:test-token-not-real"
KNOWN_BOT_ID = 42
OWNER_ID = 900
TENANT_A_ID = 555
TENANT_B_ID = 556


class _FakeBot:
    def __init__(self, token: str) -> None:
        self.token = token


class PropertyRentalRoleFilterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmpdir.name, "bot.db")
        await property_rental.init_db(self.db_path)

        # Seed: one property, two leases (tenant A and tenant B), one payment
        # per lease, one maintenance request per tenant, owner granted access.
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("INSERT INTO properties (address, status) VALUES ('Объект 1', 'occupied')")
            await db.execute("INSERT INTO properties (address, status) VALUES ('Объект 2', 'occupied')")
            await db.execute(
                "INSERT INTO leases (property_id, tenant_user_id, tenant_name, start_date, end_date, "
                "monthly_amount, status) VALUES (1, ?, 'Tenant A', '2026-01-01', '2027-01-01', 40000, 'active')",
                (TENANT_A_ID,),
            )
            await db.execute(
                "INSERT INTO leases (property_id, tenant_user_id, tenant_name, start_date, end_date, "
                "monthly_amount, status) VALUES (2, ?, 'Tenant B', '2026-01-01', '2027-01-01', 45000, 'active')",
                (TENANT_B_ID,),
            )
            await db.execute(
                "INSERT INTO rent_payments (lease_id, period, amount, due_date, status) "
                "VALUES (1, '2026-09', 40000, '2026-09-01', 'pending')"
            )
            await db.execute(
                "INSERT INTO rent_payments (lease_id, period, amount, due_date, status) "
                "VALUES (2, '2026-09', 45000, '2026-09-01', 'pending')"
            )
            await db.execute(
                "INSERT INTO maintenance_requests (lease_id, property_id, tenant_user_id, description) "
                "VALUES (1, 1, ?, 'Секрет арендатора A')", (TENANT_A_ID,),
            )
            await db.execute("INSERT INTO property_access (user_id, role) VALUES (?, 'owner')", (OWNER_ID,))
            await db.execute("INSERT INTO property_access (user_id, role) VALUES (?, 'tenant')", (TENANT_A_ID,))
            await db.execute("INSERT INTO property_access (user_id, role) VALUES (?, 'tenant')", (TENANT_B_ID,))
            await db.commit()

        entry = BotEntry(
            bot=_FakeBot(FAKE_TOKEN),
            dispatcher=None,
            template_id="property_rental",
            config={"bot_id": KNOWN_BOT_ID, "db_path": self.db_path},
        )
        registry = {KNOWN_BOT_ID: entry}
        self.app = create_app(registry)
        register_routes(self.app)

        self._module_patcher = patch(
            "runtime.miniapp_api._load_template_module_async",
            AsyncMock(return_value=property_rental),
        )
        self._module_patcher.start()

        self._db_patcher = patch(
            "runtime.miniapp_api.get_bot_miniapp_config", AsyncMock(return_value=None)
        )
        self._db_patcher.start()

        # No role_filter-less resource is exercised here except
        # viewing_requests (not tested below), so admin membership only
        # matters as a fallback — declared for completeness/parity with
        # test_miniapp_api.py's own harness.
        self._admins_patcher = patch(
            "runtime.miniapp_api.get_bot_admins", AsyncMock(return_value=[str(OWNER_ID)])
        )
        self._admins_patcher.start()

        self.server = TestServer(self.app)
        self.client = TestClient(self.server)
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self._module_patcher.stop()
        self._db_patcher.stop()
        self._admins_patcher.stop()
        self._tmpdir.cleanup()

    async def _qs(self, telegram_user_id: int) -> str:
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            return f"token={mint_magic_link_token(KNOWN_BOT_ID, telegram_user_id)}"

    async def _get(self, path: str, telegram_user_id: int):
        qs = await self._qs(telegram_user_id)
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            return await self.client.get(f"/api/{KNOWN_BOT_ID}/{path}?{qs}")

    async def test_owner_sees_every_lease(self):
        resp = await self._get("leases", OWNER_ID)
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertEqual(len(body["items"]), 2)

    async def test_tenant_sees_only_own_lease(self):
        resp = await self._get("leases", TENANT_A_ID)
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertEqual(len(body["items"]), 1)
        self.assertEqual(body["items"][0]["tenant_user_id"], TENANT_A_ID)

    async def test_tenant_cannot_fetch_other_tenants_lease_by_id(self):
        # Tenant A guesses lease id 2, which belongs to tenant B.
        resp = await self._get("leases/2", TENANT_A_ID)
        self.assertEqual(resp.status, 404)

    async def test_tenant_can_fetch_own_lease_by_id(self):
        resp = await self._get("leases/1", TENANT_A_ID)
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertEqual(body["item"]["tenant_user_id"], TENANT_A_ID)

    async def test_tenant_sees_only_own_rent_payments(self):
        resp = await self._get("rent_payments", TENANT_B_ID)
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertEqual(len(body["items"]), 1)
        self.assertEqual(body["items"][0]["lease_id"], 2)

    async def test_owner_sees_every_rent_payment(self):
        resp = await self._get("rent_payments", OWNER_ID)
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertEqual(len(body["items"]), 2)

    async def test_tenant_cannot_see_other_tenants_maintenance_request(self):
        resp = await self._get("maintenance_requests", TENANT_B_ID)
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertEqual(body["items"], [])

    async def test_tenant_sees_own_maintenance_request(self):
        resp = await self._get("maintenance_requests", TENANT_A_ID)
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertEqual(len(body["items"]), 1)
        self.assertEqual(body["items"][0]["description"], "Секрет арендатора A")

    async def test_tenant_can_create_own_maintenance_request(self):
        qs = await self._qs(TENANT_B_ID)
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.post(
                f"/api/{KNOWN_BOT_ID}/maintenance_requests?{qs}",
                json={"lease_id": 2, "property_id": 2, "description": "Не работает свет"},
            )
        self.assertEqual(resp.status, 201)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute(
                "SELECT tenant_user_id FROM maintenance_requests ORDER BY id DESC LIMIT 1"
            )).fetchone()
        self.assertEqual(row["tenant_user_id"], TENANT_B_ID)

    async def test_tenant_cannot_spoof_another_tenants_id_on_create(self):
        # Tenant B tries to submit a maintenance request claiming to be
        # tenant A — create_resource_handler must force tenant_user_id to
        # the AUTHENTICATED caller, rejecting a mismatched explicit value.
        qs = await self._qs(TENANT_B_ID)
        with patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}):
            resp = await self.client.post(
                f"/api/{KNOWN_BOT_ID}/maintenance_requests?{qs}",
                json={"lease_id": 1, "property_id": 1, "description": "spoofed", "tenant_user_id": TENANT_A_ID},
            )
        self.assertEqual(resp.status, 400)

    async def test_properties_owner_unfiltered_tenant_scoped_to_own_leased_unit(self):
        owner_resp = await self._get("properties", OWNER_ID)
        owner_body = await owner_resp.json()
        self.assertEqual(len(owner_body["items"]), 2)

        tenant_resp = await self._get("properties", TENANT_A_ID)
        tenant_body = await tenant_resp.json()
        self.assertEqual(len(tenant_body["items"]), 1)
        self.assertEqual(tenant_body["items"][0]["address"], "Объект 1")

    async def test_stranger_with_no_property_access_row_gets_empty_lists(self):
        # An authenticated Telegram user who is neither an owner nor a
        # tenant (never appears in property_access) must get default_deny's
        # empty-list behavior, not an unfiltered/leaked view.
        stranger_id = 12345
        resp = await self._get("leases", stranger_id)
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        self.assertEqual(body["items"], [])


if __name__ == "__main__":
    unittest.main()
