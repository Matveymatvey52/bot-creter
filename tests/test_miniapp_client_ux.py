"""Client mini-app UX overhaul — the engine behaviours the screens depend on.

Same harness style as tests/test_miniapp_api.py (fake BotEntry + registry
dict, magic-link tokens, no Telegram network), but exercised against the REAL
miniapp_config of templates/team_manager.py and templates/tour_operator.py:
the point of this change is that the ENGINE carries the behaviour and the
templates only declare metadata, so testing against a synthetic config would
prove the wrong half.

Covers:
  - create works for the owner where the resource used to answer 403
    "resource is read-only", and the created row is visible to its creator;
  - a read-only resource reports canCreate=false, so no form is ever offered;
  - a detail response carries the record's related sub-records inline;
  - cross-bot and cross-client isolation still hold on both of those paths.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import aiosqlite
from aiohttp.test_utils import TestClient, TestServer

import templates.team_manager as team_manager
import templates.tour_operator as tour_operator
from runtime.miniapp_api import mint_magic_link_token, register_routes
from runtime.registry import BotEntry
from runtime.webhook_app import create_app

FAKE_TOKEN = "123456:test-token-not-real"
BOT_A = 42
BOT_B = 43

BOSS_ID = 111  # bot admin of BOT_A — the owner in the product sense
WORKER_ID = 222  # a member of one project, never an admin
OUTSIDER_ID = 333  # authenticated Telegram user with no membership at all


class _FakeBot:
    def __init__(self, token: str) -> None:
        self.token = token


class _FakeModule:
    def __init__(self, config: dict) -> None:
        self.miniapp_config = config


async def _init_team_manager_db(db_path: str) -> None:
    """The subset of templates/team_manager.py's real schema these routes
    touch — column names and NOT NULL/UNIQUE constraints copied verbatim, so a
    drift in the template surfaces here as a failure rather than silently
    passing against a laxer fixture."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE projects (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                created_by  INTEGER NOT NULL,
                code        TEXT NOT NULL UNIQUE,
                created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.execute("""
            CREATE TABLE project_members (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id  INTEGER NOT NULL,
                user_id     INTEGER NOT NULL,
                role        TEXT NOT NULL CHECK (role IN ('owner', 'worker')),
                joined_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                UNIQUE(project_id, user_id)
            )
        """)
        await db.execute("""
            CREATE TABLE tasks (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id   INTEGER NOT NULL,
                created_by   INTEGER NOT NULL,
                assigned_to  INTEGER NOT NULL,
                text         TEXT NOT NULL,
                category     TEXT NOT NULL,
                deadline     TEXT NOT NULL,
                status       TEXT NOT NULL DEFAULT 'not_taken',
                created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.execute("""
            CREATE TABLE reports (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id       INTEGER NOT NULL,
                submitted_by  INTEGER NOT NULL,
                text          TEXT NOT NULL,
                submitted_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.execute("""
            CREATE TABLE attachments (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id  INTEGER NOT NULL,
                file_id  TEXT NOT NULL,
                type     TEXT NOT NULL,
                name     TEXT,
                size     INTEGER
            )
        """)
        await db.commit()


class _TeamManagerHarness(unittest.IsolatedAsyncioTestCase):
    """Two independent team_manager bots sharing one process — the shape that
    makes cross-bot leakage possible in the first place."""

    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_a = os.path.join(self._tmpdir.name, "a.db")
        self.db_b = os.path.join(self._tmpdir.name, "b.db")
        await _init_team_manager_db(self.db_a)
        await _init_team_manager_db(self.db_b)

        registry = {
            BOT_A: BotEntry(
                bot=_FakeBot(FAKE_TOKEN),
                dispatcher=None,
                template_id="team_manager",
                config={"bot_id": BOT_A, "db_path": self.db_a},
            ),
            BOT_B: BotEntry(
                bot=_FakeBot(FAKE_TOKEN),
                dispatcher=None,
                template_id="team_manager",
                config={"bot_id": BOT_B, "db_path": self.db_b},
            ),
        }
        self.app = create_app(registry)
        register_routes(self.app)

        self._patchers = [
            patch(
                "runtime.miniapp_api._load_template_module_async",
                AsyncMock(return_value=_FakeModule(team_manager.miniapp_config)),
            ),
            patch("runtime.miniapp_api.get_bot_miniapp_config", AsyncMock(return_value=None)),
            # BOSS_ID is the factory-level admin of BOTH bots here: if
            # isolation held only because the admin lists differed, these
            # tests would prove nothing about per-bot scoping.
            patch("runtime.miniapp_api.get_bot_admins", AsyncMock(return_value=[str(BOSS_ID)])),
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

    def auth(self, bot_id: int, user_id: int) -> str:
        return f"token={mint_magic_link_token(bot_id, telegram_user_id=user_id)}"

    async def schema_for(self, bot_id: int, user_id: int) -> dict:
        resp = await self.client.get(f"/api/{bot_id}/schema?{self.auth(bot_id, user_id)}")
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        return {r["name"]: r for r in body["resources"]}


class CreateOnFormerlyReadOnlyResourceTests(_TeamManagerHarness):
    """Point 1: "Проекты" showed a create form whose submit always answered
    403 "resource is read-only"."""

    async def test_owner_can_create_project_and_immediately_sees_it(self):
        resp = await self.client.post(
            f"/api/{BOT_A}/projects?{self.auth(BOT_A, BOSS_ID)}",
            json={"name": "Ремонт офиса"},
        )
        self.assertEqual(resp.status, 201, await resp.text())
        new_id = (await resp.json())["id"]

        # The whole point of on_create.link: without the membership row the
        # read-side role_filter would scope the creator out of the project
        # they just made, which is indistinguishable from "create silently
        # did nothing".
        listing = await self.client.get(f"/api/{BOT_A}/projects?{self.auth(BOT_A, BOSS_ID)}")
        names = [row["name"] for row in (await listing.json())["items"]]
        self.assertEqual(names, ["Ремонт офиса"])

        async with aiosqlite.connect(self.db_a) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM projects WHERE id = ?", (new_id,)) as cur:
                row = dict(await cur.fetchone())
            async with db.execute(
                "SELECT * FROM project_members WHERE project_id = ?", (new_id,)
            ) as cur:
                members = [dict(r) for r in await cur.fetchall()]

        # Server-derived columns the client never sent and must not control.
        self.assertEqual(row["created_by"], BOSS_ID)
        self.assertTrue(row["code"], "invite code must be generated, not left blank")
        self.assertEqual(len(members), 1)
        self.assertEqual((members[0]["user_id"], members[0]["role"]), (BOSS_ID, "owner"))

    async def test_schema_reports_can_create_for_owner(self):
        resources = await self.schema_for(BOT_A, BOSS_ID)
        self.assertTrue(resources["projects"]["canCreate"])

    async def test_client_without_rights_cannot_create_and_is_told_so_up_front(self):
        """Both halves of the rule: the POST is refused, AND the schema says
        so, so the SPA never renders a form this user cannot submit."""
        resp = await self.client.post(
            f"/api/{BOT_A}/projects?{self.auth(BOT_A, OUTSIDER_ID)}",
            json={"name": "Чужой проект"},
        )
        self.assertEqual(resp.status, 403)

        resources = await self.schema_for(BOT_A, OUTSIDER_ID)
        self.assertFalse(resources["projects"]["canCreate"])

        async with aiosqlite.connect(self.db_a) as db:
            async with db.execute("SELECT COUNT(*) FROM projects") as cur:
                self.assertEqual((await cur.fetchone())[0], 0)

    async def test_read_only_resource_reports_can_create_false(self):
        """Point 1's engine rule: tasks/reports/attachments stay read-only in
        the mini-app (they're created through the bot's own flows), so no
        create affordance may be offered for them at all."""
        resources = await self.schema_for(BOT_A, BOSS_ID)
        for name in ("tasks", "reports", "attachments"):
            self.assertFalse(resources[name]["canCreate"], name)

    async def test_client_supplied_created_by_cannot_override_identity(self):
        resp = await self.client.post(
            f"/api/{BOT_A}/projects?{self.auth(BOT_A, BOSS_ID)}",
            json={"name": "Подмена", "created_by": OUTSIDER_ID, "code": "HACKED"},
        )
        self.assertEqual(resp.status, 201, await resp.text())
        async with aiosqlite.connect(self.db_a) as db:
            async with db.execute("SELECT created_by, code FROM projects") as cur:
                created_by, code = await cur.fetchone()
        self.assertEqual(created_by, BOSS_ID)
        self.assertNotEqual(code, "HACKED")


class CrossBotAndCrossClientIsolationTests(_TeamManagerHarness):
    """The create path is new; these prove it did not become a way around the
    isolation the read path already enforced."""

    async def test_project_created_on_one_bot_is_invisible_on_the_other(self):
        resp = await self.client.post(
            f"/api/{BOT_A}/projects?{self.auth(BOT_A, BOSS_ID)}",
            json={"name": "Только для бота A"},
        )
        self.assertEqual(resp.status, 201)

        listing = await self.client.get(f"/api/{BOT_B}/projects?{self.auth(BOT_B, BOSS_ID)}")
        self.assertEqual(listing.status, 200)
        self.assertEqual((await listing.json())["items"], [])

    async def test_token_minted_for_one_bot_is_rejected_by_the_other(self):
        resp = await self.client.post(
            f"/api/{BOT_B}/projects?{self.auth(BOT_A, BOSS_ID)}",
            json={"name": "Чужой токен"},
        )
        self.assertEqual(resp.status, 403)

    async def test_one_members_project_is_not_visible_to_another_member(self):
        """Two workers, two projects, one bot — the multi-team scenario the
        role_filter exists for. Creating through the mini-app must not widen
        it."""
        first = await self.client.post(
            f"/api/{BOT_A}/projects?{self.auth(BOT_A, BOSS_ID)}", json={"name": "Проект A"}
        )
        second = await self.client.post(
            f"/api/{BOT_A}/projects?{self.auth(BOT_A, BOSS_ID)}", json={"name": "Проект B"}
        )
        first_id = (await first.json())["id"]
        second_id = (await second.json())["id"]

        async with aiosqlite.connect(self.db_a) as db:
            await db.execute(
                "INSERT INTO project_members (project_id, user_id, role) VALUES (?, ?, 'worker')",
                (first_id, WORKER_ID),
            )
            await db.commit()

        listing = await self.client.get(f"/api/{BOT_A}/projects?{self.auth(BOT_A, WORKER_ID)}")
        names = [row["name"] for row in (await listing.json())["items"]]
        self.assertEqual(names, ["Проект A"])

        # And the one they aren't a member of is not reachable by direct id.
        direct = await self.client.get(
            f"/api/{BOT_A}/projects/{second_id}?{self.auth(BOT_A, WORKER_ID)}"
        )
        self.assertEqual(direct.status, 404)

    async def test_outsider_sees_no_projects_at_all(self):
        await self.client.post(
            f"/api/{BOT_A}/projects?{self.auth(BOT_A, BOSS_ID)}", json={"name": "Проект A"}
        )
        listing = await self.client.get(f"/api/{BOT_A}/projects?{self.auth(BOT_A, OUTSIDER_ID)}")
        self.assertEqual(listing.status, 200)
        self.assertEqual((await listing.json())["items"], [])


class InlineRelatedRecordsTests(_TeamManagerHarness):
    """Points 3 and 7: a record's sub-records belong on the record's own
    detail card, not in sibling tabs matched up by id."""

    async def _project_with_task(self) -> tuple[int, int]:
        resp = await self.client.post(
            f"/api/{BOT_A}/projects?{self.auth(BOT_A, BOSS_ID)}", json={"name": "Проект"}
        )
        project_id = (await resp.json())["id"]
        async with aiosqlite.connect(self.db_a) as db:
            cur = await db.execute(
                "INSERT INTO tasks (project_id, created_by, assigned_to, text, category, deadline)"
                " VALUES (?, ?, ?, 'Покрасить стены', 'Ремонт', '2026-09-01')",
                (project_id, BOSS_ID, WORKER_ID),
            )
            task_id = cur.lastrowid
            await db.execute(
                "INSERT INTO reports (task_id, submitted_by, text) VALUES (?, ?, 'Готово')",
                (task_id, WORKER_ID),
            )
            await db.execute(
                "INSERT INTO attachments (task_id, file_id, type, name)"
                " VALUES (?, 'f1', 'photo', 'до.jpg')",
                (task_id,),
            )
            await db.commit()
        return project_id, task_id

    async def test_project_detail_includes_its_tasks(self):
        project_id, _ = await self._project_with_task()
        resp = await self.client.get(
            f"/api/{BOT_A}/projects/{project_id}?{self.auth(BOT_A, BOSS_ID)}"
        )
        self.assertEqual(resp.status, 200)
        body = await resp.json()
        sections = {s["resource"]: s for s in body["related"]}
        self.assertIn("tasks", sections)
        self.assertEqual([t["text"] for t in sections["tasks"]["items"]], ["Покрасить стены"])

    async def test_task_detail_shows_reports_and_attachments_together(self):
        _, task_id = await self._project_with_task()
        resp = await self.client.get(f"/api/{BOT_A}/tasks/{task_id}?{self.auth(BOT_A, BOSS_ID)}")
        self.assertEqual(resp.status, 200)
        sections = {s["resource"]: s for s in (await resp.json())["related"]}
        self.assertEqual([a["name"] for a in sections["attachments"]["items"]], ["до.jpg"])
        self.assertEqual([r["text"] for r in sections["reports"]["items"]], ["Готово"])

    async def test_related_sections_respect_the_child_role_filter(self):
        """A related section is a convenience join, never a bypass: the
        outsider cannot read a task through its project any more than
        directly."""
        project_id, _ = await self._project_with_task()
        resp = await self.client.get(
            f"/api/{BOT_A}/projects/{project_id}?{self.auth(BOT_A, OUTSIDER_ID)}"
        )
        # The parent itself is already out of reach for this viewer: a
        # role_filter that matches no rule denies the read outright.
        self.assertEqual(resp.status, 403)


class FeatureGatedSectionsTests(_TeamManagerHarness):
    """Point 6: analytics is no longer part of every bot's default navigation.
    The SPA reads this endpoint to decide whether to show the section at all."""

    async def test_features_endpoint_reports_nothing_enabled_by_default(self):
        resp = await self.client.get(f"/api/{BOT_A}/features?{self.auth(BOT_A, BOSS_ID)}")
        self.assertEqual(resp.status, 200)
        self.assertEqual((await resp.json())["features"], [])

    async def test_features_endpoint_reports_enabled_analytics(self):
        with patch(
            "runtime.miniapp_api.get_bot_features", AsyncMock(return_value=["sales_analytics"])
        ):
            resp = await self.client.get(f"/api/{BOT_A}/features?{self.auth(BOT_A, BOSS_ID)}")
        self.assertEqual((await resp.json())["features"], ["sales_analytics"])

    async def test_features_endpoint_requires_authentication(self):
        resp = await self.client.get(f"/api/{BOT_A}/features")
        self.assertEqual(resp.status, 403)


class SchemaMetadataTests(unittest.IsolatedAsyncioTestCase):
    """Metadata the engine needs from the templates, checked against the real
    configs so a template edit can't quietly drop it."""

    def test_tour_operator_children_reference_declared_resources(self):
        config = tour_operator.miniapp_config
        names = {r["name"] for r in config["resources"]}
        by_name = {r["name"]: r for r in config["resources"]}
        for resource in config["resources"]:
            for child in resource.get("children", []):
                self.assertIn(child["resource"], names, f"{resource['name']} -> {child}")
                child_fields = {f["name"] for f in by_name[child["resource"]]["fields"]}
                # `via` is interpolated as a column name by _related_for_item,
                # which skips any child whose `via` isn't a declared field —
                # a typo here would silently render an empty section.
                self.assertIn(child["via"], child_fields, f"{resource['name']} -> {child}")

    def test_tour_detail_gathers_every_sub_entity(self):
        tours = next(r for r in tour_operator.miniapp_config["resources"] if r["name"] == "tours")
        self.assertEqual(
            {c["resource"] for c in tours["children"]},
            {"program", "hotels", "locations", "guests", "dds"},
        )

    def test_foreign_keys_declare_a_ref_instead_of_asking_for_an_id(self):
        """Point 4: no user-level ID input anywhere. Every field named *_id
        that the user can fill in must carry a `ref` so the form renders a
        picker over human-readable names."""
        for module in (tour_operator, team_manager):
            for resource in module.miniapp_config["resources"]:
                for field in resource["fields"]:
                    if not field["name"].endswith("_id"):
                        continue
                    if not field.get("create", False):
                        continue
                    self.assertIn(
                        "ref",
                        field,
                        f"{module.__name__}.{resource['name']}.{field['name']}"
                        " is a creatable id field with no ref —"
                        " it would render as a raw ID input",
                    )

    def test_ref_targets_exist_and_name_a_real_label_column(self):
        for module in (tour_operator, team_manager):
            config = module.miniapp_config
            by_name = {r["name"]: r for r in config["resources"]}
            for resource in config["resources"]:
                for field in resource["fields"]:
                    ref = field.get("ref")
                    if not ref:
                        continue
                    self.assertIn(ref["resource"], by_name, f"{resource['name']}.{field['name']}")
                    target = by_name[ref["resource"]]
                    self.assertIn(
                        ref["labelField"],
                        {f["name"] for f in target["fields"]},
                        f"{resource['name']}.{field['name']} labelField",
                    )

    def test_team_manager_projects_declares_its_server_filled_columns(self):
        """projects.code and projects.created_by are NOT NULL in the real
        schema; if on_create ever stops filling them, create would 500."""
        projects = next(
            r for r in team_manager.miniapp_config["resources"] if r["name"] == "projects"
        )
        self.assertTrue(projects["creatable"])
        self.assertEqual(set(projects["on_create"]["set"]), {"created_by", "code"})
        self.assertEqual(projects["on_create"]["link"]["table"], "project_members")


if __name__ == "__main__":
    unittest.main()
