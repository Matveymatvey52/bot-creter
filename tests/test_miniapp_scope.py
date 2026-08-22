"""Parent scoping in the shared mini-app engine (docs/SCOPE_AUDIT_STAGE_A.md).

The audit's finding was that the engine had no concept of a parent at all:
every SCOPED section listed every parent's rows under a bare title, while the
same section in Telegram was correctly filtered and named. These tests pin the
engine behaviour that replaces it, exercised against the REAL miniapp_config
of templates/tour_operator.py — the ДДС section the owner actually reported.

Harness style copied from tests/test_miniapp_client_ux.py (fake BotEntry +
registry dict, magic-link tokens, no Telegram network).

Covers:
  - a scoped list filtered to one parent excludes the other parent's rows,
    and totals computed over the response exclude them too;
  - a GLOBAL resource refuses ?parent= and carries NO parent machinery at
    all in its response — the mockup-E bug made structurally impossible;
  - unparented rows are reported as their own group, never guessed at;
  - a required field blank or whitespace-only is refused, for every kind,
    which is what stops a blank parent from creating an orphan;
  - validate_scope_declarations() rejects the authoring mistakes CI must catch.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import aiosqlite
from aiohttp.test_utils import TestClient, TestServer

import templates.tour_operator as tour_operator
from runtime.miniapp_api import (
    ScopeError,
    mint_magic_link_token,
    register_routes,
    resource_scope,
    validate_scope_declarations,
)
from runtime.registry import BotEntry
from runtime.webhook_app import create_app

FAKE_TOKEN = "123456:test-token-not-real"
BOT_A = 42

OWNER_ID = 111  # factory-level admin of the bot

SOCHI = 1
BALI = 2


class _FakeBot:
    def __init__(self, token: str) -> None:
        self.token = token


class _FakeModule:
    def __init__(self, config: dict) -> None:
        self.miniapp_config = config


async def _init_tour_db(db_path: str) -> None:
    """The subset of templates/tour_operator.py's real schema these routes
    touch, plus features/cashflow_ledger.py's own table — column names copied
    verbatim so a drift in either surfaces here rather than passing against a
    laxer fixture."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE tours (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                name         TEXT NOT NULL,
                destination  TEXT,
                date_start   TEXT,
                date_end     TEXT,
                guests_count INTEGER,
                status       TEXT DEFAULT 'draft',
                created_at   TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.execute("""
            CREATE TABLE guests (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                tour_id        INTEGER,
                name           TEXT NOT NULL,
                total_cost     REAL DEFAULT 0,
                prepaid        REAL DEFAULT 0,
                our_price      REAL DEFAULT 0,
                status         TEXT DEFAULT 'new',
                notes          TEXT,
                client_user_id INTEGER,
                created_at     TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.execute("""
            CREATE TABLE cashflow_entries (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                parent_id    TEXT,
                date         TEXT,
                amount_rub   REAL DEFAULT 0,
                amount_usd   REAL DEFAULT 0,
                amount_idr   REAL DEFAULT 0,
                description  TEXT,
                entity       TEXT,
                type         TEXT NOT NULL DEFAULT 'out' CHECK(type IN ('in','out')),
                created_at   TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.execute("CREATE TABLE tour_access (user_id INTEGER PRIMARY KEY, role TEXT)")

        await db.execute("INSERT INTO tours(id,name) VALUES(?,?)", (SOCHI, "Сочи"))
        await db.execute("INSERT INTO tours(id,name) VALUES(?,?)", (BALI, "Бали"))

        # Two parents' worth of money, deliberately different magnitudes so a
        # total that accidentally spans both is impossible to mistake for a
        # correct one.
        await db.executemany(
            "INSERT INTO cashflow_entries(parent_id,type,amount_rub,description) VALUES(?,?,?,?)",
            [
                (str(SOCHI), "in", 100.0, "аванс Сочи"),
                (str(SOCHI), "out", 40.0, "трансфер Сочи"),
                (str(BALI), "in", 7000.0, "аванс Бали"),
                (str(BALI), "out", 3000.0, "виллы Бали"),
            ],
        )
        await db.executemany(
            "INSERT INTO guests(tour_id,name,total_cost) VALUES(?,?,?)",
            [(SOCHI, "Иванов", 50.0), (BALI, "Петров", 900.0)],
        )
        await db.commit()


class _TourHarness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_a = os.path.join(self._tmpdir.name, "a.db")
        await _init_tour_db(self.db_a)

        registry = {
            BOT_A: BotEntry(
                bot=_FakeBot(FAKE_TOKEN),
                dispatcher=None,
                template_id="tour_operator",
                config={"bot_id": BOT_A, "db_path": self.db_a},
            ),
        }
        self.app = create_app(registry)
        register_routes(self.app)

        self._patchers = [
            patch(
                "runtime.miniapp_api._load_template_module_async",
                AsyncMock(return_value=_FakeModule(tour_operator.miniapp_config)),
            ),
            patch("runtime.miniapp_api.get_bot_miniapp_config", AsyncMock(return_value=None)),
            patch("runtime.miniapp_api.get_bot_admins", AsyncMock(return_value=[str(OWNER_ID)])),
            patch("runtime.miniapp_api.get_bot_features", AsyncMock(return_value=[])),
            patch.dict(os.environ, {"MINIAPP_SECRET": "s3cret"}),
        ]
        for p in self._patchers:
            p.start()

        self.server = TestServer(self.app)
        self.client = TestClient(self.server)
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        for p in reversed(self._patchers):
            p.stop()
        self._tmpdir.cleanup()

    def auth(self, user_id: int = OWNER_ID) -> str:
        return f"token={mint_magic_link_token(BOT_A, telegram_user_id=user_id)}"

    async def list_of(self, resource: str, parent: str | None = None, user_id: int = OWNER_ID):
        query = self.auth(user_id)
        if parent is not None:
            query += f"&parent={parent}"
        return await self.client.get(f"/api/{BOT_A}/{resource}?{query}")


class ScopedListFiltersByParentTests(_TourHarness):
    """The reported defect: ДДС showed every tour's operations at once."""

    async def test_list_scoped_to_one_parent_excludes_the_other(self):
        resp = await self.list_of("dds", parent=str(SOCHI))
        self.assertEqual(resp.status, 200)
        body = await resp.json()

        descriptions = {row["description"] for row in body["items"]}
        self.assertEqual(descriptions, {"аванс Сочи", "трансфер Сочи"})
        self.assertNotIn("аванс Бали", descriptions)

    async def test_totals_over_the_filtered_selection_exclude_the_other_parent(self):
        resp = await self.list_of("dds", parent=str(SOCHI))
        items = (await resp.json())["items"]

        inflow = sum(r["amount_rub"] for r in items if r["type"] == "in")
        outflow = sum(r["amount_rub"] for r in items if r["type"] == "out")
        self.assertEqual(inflow, 100.0)
        self.assertEqual(outflow, 40.0)
        self.assertEqual(inflow - outflow, 60.0)
        # Bali's 7000/3000 would swamp these if the selection spanned parents.

    async def test_second_parent_gets_its_own_rows(self):
        resp = await self.list_of("dds", parent=str(BALI))
        items = (await resp.json())["items"]
        self.assertEqual({r["description"] for r in items}, {"аванс Бали", "виллы Бали"})

    async def test_another_scoped_resource_filters_too(self):
        resp = await self.list_of("guests", parent=str(SOCHI))
        items = (await resp.json())["items"]
        self.assertEqual([r["name"] for r in items], ["Иванов"])

    async def test_response_names_the_parent_for_the_header(self):
        resp = await self.list_of("dds", parent=str(SOCHI))
        body = await resp.json()
        self.assertEqual(body["scope"]["parentTitle"], "Туры")
        self.assertEqual(body["scope"]["parentId"], str(SOCHI))
        self.assertTrue(all(r["parent_label"] == "Сочи" for r in body["items"]))

    async def test_summary_mode_labels_every_row_with_its_own_parent(self):
        """Without ?parent= the rows of all parents come back — which is only
        usable because each row carries the name that tells them apart."""
        resp = await self.list_of("dds")
        body = await resp.json()
        self.assertEqual(len(body["items"]), 4)
        self.assertEqual(
            {r["description"]: r["parent_label"] for r in body["items"]},
            {
                "аванс Сочи": "Сочи",
                "трансфер Сочи": "Сочи",
                "аванс Бали": "Бали",
                "виллы Бали": "Бали",
            },
        )
        self.assertIsNone(body["scope"]["parentId"])


class ParentPickerOptionsTests(_TourHarness):
    """The picker's choices come from the engine, so the client never has to
    know which column carries a parent's name."""

    async def test_scoped_response_offers_every_parent_to_switch_to(self):
        resp = await self.list_of("dds", parent=str(SOCHI))
        options = (await resp.json())["scope"]["options"]
        self.assertEqual(
            {o["label"] for o in options},
            {"Сочи", "Бали"},
        )

    async def test_options_are_offered_in_summary_mode_too(self):
        resp = await self.list_of("dds")
        options = (await resp.json())["scope"]["options"]
        self.assertEqual({o["id"] for o in options}, {str(SOCHI), str(BALI)})

    async def test_options_are_ids_and_labels_only(self):
        """Whatever else lives on a tour row stays out of the picker."""
        resp = await self.list_of("guests", parent=str(BALI))
        for option in (await resp.json())["scope"]["options"]:
            self.assertEqual(set(option), {"id", "label"})


class GlobalResourceHasNoParentMachineryTests(_TourHarness):
    """The guarantee the owner asked to be structural rather than careful:
    a global resource cannot be asked about a parent, and nothing in its
    response even offers a place to render one. This is the mockup-E bug
    (a tour shown on the hotel reference table) made unwritable."""

    async def test_parent_query_on_a_global_resource_is_refused(self):
        resp = await self.list_of("tours", parent=str(SOCHI))
        self.assertEqual(resp.status, 400)
        self.assertIn("global", (await resp.json())["error"])

    async def test_global_response_carries_no_scope_block(self):
        resp = await self.list_of("tours")
        body = await resp.json()
        self.assertEqual(resp.status, 200)
        self.assertNotIn("scope", body)

    async def test_global_rows_carry_no_parent_label(self):
        resp = await self.list_of("tours")
        for row in (await resp.json())["items"]:
            self.assertNotIn("parent_label", row)

    async def test_resource_scope_collapses_global_to_none(self):
        """The single line every parent-touching branch keys off. If this
        ever returned a dict for a global resource, the guarantee above
        would become a matter of remembering to check."""
        self.assertIsNone(resource_scope({"scope": {"type": "global"}}))
        self.assertIsNone(resource_scope({}))  # undeclared degrades the same way


class UnparentedRowsTests(_TourHarness):
    """Orphans are surfaced as their own group and never attributed by guess
    — the owner ruled out date/proximity matching outright."""

    async def _insert_orphan(self):
        async with aiosqlite.connect(self.db_a) as db:
            await db.execute(
                "INSERT INTO cashflow_entries(parent_id,type,amount_rub,description) "
                "VALUES(NULL,'out',5.0,'без тура')"
            )
            await db.commit()

    async def test_orphan_has_no_parent_label_rather_than_a_guessed_one(self):
        await self._insert_orphan()
        resp = await self.list_of("dds")
        orphans = [r for r in (await resp.json())["items"] if r["description"] == "без тура"]
        self.assertEqual(len(orphans), 1)
        self.assertIsNone(orphans[0]["parent_label"])

    async def test_parent_none_returns_only_unparented_rows(self):
        await self._insert_orphan()
        resp = await self.list_of("dds", parent="none")
        items = (await resp.json())["items"]
        self.assertEqual([r["description"] for r in items], ["без тура"])

    async def test_orphan_does_not_appear_under_any_real_parent(self):
        await self._insert_orphan()
        for tour_id in (SOCHI, BALI):
            resp = await self.list_of("dds", parent=str(tour_id))
            descriptions = {r["description"] for r in (await resp.json())["items"]}
            self.assertNotIn("без тура", descriptions)


class BlankRequiredFieldTests(_TourHarness):
    """§6.2 of the audit: ДДС accepted a create with no tour and produced a
    row invisible in every Telegram section afterwards. Fixing it needs BOTH
    required on the field and an emptiness check the key-presence test missed."""

    async def _post_dds(self, payload: dict):
        return await self.client.post(f"/api/{BOT_A}/dds?{self.auth()}", json=payload)

    async def _dds_row_count(self) -> int:
        async with aiosqlite.connect(self.db_a) as db:
            async with db.execute("SELECT COUNT(*) FROM cashflow_entries") as cur:
                return (await cur.fetchone())[0]

    async def test_missing_parent_is_refused(self):
        before = await self._dds_row_count()
        resp = await self._post_dds({"type": "out", "amount_rub": 10, "description": "x"})
        self.assertEqual(resp.status, 400)
        self.assertIn("parent_id", (await resp.json())["error"])
        self.assertEqual(await self._dds_row_count(), before)

    async def test_blank_parent_is_refused(self):
        """The key IS present, so the old set-difference check waved it through."""
        before = await self._dds_row_count()
        resp = await self._post_dds(
            {"parent_id": "", "type": "out", "amount_rub": 10, "description": "x"}
        )
        self.assertEqual(resp.status, 400)
        self.assertEqual(await self._dds_row_count(), before)

    async def test_whitespace_only_parent_is_refused(self):
        before = await self._dds_row_count()
        resp = await self._post_dds(
            {"parent_id": "   ", "type": "out", "amount_rub": 10, "description": "x"}
        )
        self.assertEqual(resp.status, 400)
        self.assertEqual(await self._dds_row_count(), before)

    async def test_null_parent_is_refused(self):
        before = await self._dds_row_count()
        resp = await self._post_dds(
            {"parent_id": None, "type": "out", "amount_rub": 10, "description": "x"}
        )
        self.assertEqual(resp.status, 400)
        self.assertEqual(await self._dds_row_count(), before)

    async def test_a_real_parent_still_creates(self):
        """The check must not have made the endpoint unusable."""
        resp = await self._post_dds(
            {"parent_id": str(SOCHI), "type": "out", "amount_rub": 10, "description": "ок"}
        )
        self.assertEqual(resp.status, 201)
        listed = await self.list_of("dds", parent=str(SOCHI))
        self.assertIn("ок", {r["description"] for r in (await listed.json())["items"]})


class ScopeDeclarationValidationTests(unittest.TestCase):
    """What CI enforces. Runtime tolerates an absent declaration (older
    generated configs must keep rendering); the build does not."""

    def test_the_real_tour_operator_config_is_valid(self):
        self.assertEqual(validate_scope_declarations(tour_operator.miniapp_config), [])

    def test_missing_scope_is_an_error(self):
        errors = validate_scope_declarations(
            {"resources": [{"name": "dds", "table": "cashflow_entries", "fields": []}]}
        )
        self.assertEqual(len(errors), 1)
        self.assertIn('no "scope" declared', errors[0])

    def test_parent_must_be_a_real_resource(self):
        errors = validate_scope_declarations(
            {
                "resources": [
                    {
                        "name": "dds",
                        "scope": {"type": "scoped", "parent": "nope", "via": "parent_id"},
                        "fields": [{"name": "parent_id"}],
                    }
                ]
            }
        )
        self.assertTrue(any("is not a resource in this config" in e for e in errors))

    def test_via_must_be_a_declared_field(self):
        errors = validate_scope_declarations(
            {
                "resources": [
                    {"name": "tours", "titleField": "name", "scope": {"type": "global"},
                     "fields": [{"name": "name"}]},
                    {
                        "name": "dds",
                        "scope": {"type": "scoped", "parent": "tours", "via": "tour_id"},
                        "fields": [{"name": "parent_id"}],
                    },
                ]
            }
        )
        self.assertTrue(any("is not among this resource" in e for e in errors))

    def test_parent_must_be_nameable_in_the_ui(self):
        errors = validate_scope_declarations(
            {
                "resources": [
                    {"name": "tours", "scope": {"type": "global"}, "fields": []},
                    {
                        "name": "dds",
                        "scope": {"type": "scoped", "parent": "tours", "via": "parent_id"},
                        "fields": [{"name": "parent_id"}],
                    },
                ]
            }
        )
        self.assertTrue(any("cannot be named in the UI" in e for e in errors))

    def test_unknown_scope_type_raises(self):
        with self.assertRaises(ScopeError):
            resource_scope({"scope": {"type": "sometimes"}})

    def test_scoped_without_via_raises(self):
        with self.assertRaises(ScopeError):
            resource_scope({"scope": {"type": "scoped", "parent": "tours"}})


if __name__ == "__main__":
    unittest.main()
