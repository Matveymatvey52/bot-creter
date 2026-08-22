"""Which miniapp_config governs a bot: the stored snapshot or the template.

The bug this pins: bots 12/13/14 ran templates/*.py directly, but each also had
a bot_miniapp_config row generated once at creation (2026-08-20). Because the DB
was consulted FIRST, that snapshot shadowed the template forever — the bots kept
serving a config with no `children`, no `ref` and projects not creatable long
after the templates had those, and the rows had to be deleted by hand on
production to unstick them.

So: a template-backed bot ignores the snapshot; a custom bot keeps it.
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

import templates.team_manager as team_manager
from runtime.miniapp_api import load_miniapp_config
from runtime.registry import BotEntry, is_template_backed

# Shaped like a real stored row, but unmistakable if it ever leaks through: no
# template declares a resource by this name.
STALE_SNAPSHOT = {
    "resources": [
        {
            "name": "stale_marker",
            "table": "projects",
            "creatable": False,
            "fields": [{"name": "name"}],
        }
    ]
}


def _entry(file_path: str | None, template_id: str | None) -> BotEntry:
    return BotEntry(
        bot=None,  # never touched by load_miniapp_config
        dispatcher=None,
        template_id=template_id,
        config={"bot_id": 1, "file_path": file_path},
    )


class IsTemplateBackedTests(unittest.TestCase):
    def test_repo_template_path_is_template_backed(self):
        for path in (
            "templates/team_manager.py",
            "templates/tour_operator.py",
            "./templates/shop_catalog.py",
        ):
            self.assertTrue(is_template_backed(path), path)

    def test_generated_bots_copy_is_not_template_backed(self):
        """A copy under generated_bots/ keeps the template's own '# TEMPLATE:'
        marker, so a marker-based check would wrongly claim it — but the copy
        may have been rewritten since, and its snapshot is still its truth.
        This is why the check is on the PATH."""
        for path in (
            "/data/generated_bots/orders_test.py",
            "/data/generated_bots/tour_operator.py",  # same basename, still a copy
            "generated_bots/custom.py",
        ):
            self.assertFalse(is_template_backed(path), path)

    def test_missing_or_odd_paths_are_not_template_backed(self):
        for path in (None, "", "   "):
            self.assertFalse(is_template_backed(path), repr(path))


class LoadMiniappConfigTests(unittest.IsolatedAsyncioTestCase):
    async def test_template_bot_ignores_its_stale_snapshot(self):
        entry = _entry("templates/team_manager.py", "team_manager")
        with patch(
            "runtime.miniapp_api.get_bot_miniapp_config",
            AsyncMock(return_value=STALE_SNAPSHOT),
        ) as stored:
            cfg = await load_miniapp_config(13, entry)

        self.assertIs(cfg, team_manager.miniapp_config)
        names = {r["name"] for r in cfg["resources"]}
        self.assertNotIn("stale_marker", names)
        # The concrete regressions the snapshot was hiding.
        projects = next(r for r in cfg["resources"] if r["name"] == "projects")
        self.assertTrue(projects["creatable"])
        self.assertTrue(projects.get("children"))
        stored.assert_not_awaited()

    async def test_custom_bot_still_uses_its_snapshot(self):
        entry = _entry("/data/generated_bots/orders_test.py", None)
        with patch(
            "runtime.miniapp_api.get_bot_miniapp_config",
            AsyncMock(return_value=STALE_SNAPSHOT),
        ):
            cfg = await load_miniapp_config(11, entry)
        self.assertIs(cfg, STALE_SNAPSHOT)

    async def test_custom_bot_from_a_template_copy_keeps_its_snapshot(self):
        """The copy carries template_id (inferred from the '# TEMPLATE:' marker
        it inherited), so this is the case that would silently regress if the
        rule keyed off template_id instead of the path."""
        entry = _entry("/data/generated_bots/my_shop.py", "shop_catalog")
        with patch(
            "runtime.miniapp_api.get_bot_miniapp_config",
            AsyncMock(return_value=STALE_SNAPSHOT),
        ):
            cfg = await load_miniapp_config(20, entry)
        self.assertIs(cfg, STALE_SNAPSHOT)

    async def test_custom_bot_with_no_snapshot_falls_back_to_its_template(self):
        """Unchanged behaviour for bots created before the table existed."""
        entry = _entry("/data/generated_bots/my_team.py", "team_manager")
        with patch("runtime.miniapp_api.get_bot_miniapp_config", AsyncMock(return_value=None)):
            cfg = await load_miniapp_config(21, entry)
        self.assertIs(cfg, team_manager.miniapp_config)

    async def test_bot_with_neither_snapshot_nor_template_has_no_mini_app(self):
        entry = _entry("/data/generated_bots/plain.py", None)
        with patch("runtime.miniapp_api.get_bot_miniapp_config", AsyncMock(return_value=None)):
            self.assertIsNone(await load_miniapp_config(22, entry))

    async def test_entry_without_file_path_still_resolves(self):
        """An entry built without a file_path (older callers, the factory bot)
        must not crash — it simply takes the ordinary snapshot-first path."""
        entry = BotEntry(
            bot=None, dispatcher=None, template_id="team_manager", config={"bot_id": 1}
        )
        with patch("runtime.miniapp_api.get_bot_miniapp_config", AsyncMock(return_value=None)):
            cfg = await load_miniapp_config(23, entry)
        self.assertIs(cfg, team_manager.miniapp_config)


if __name__ == "__main__":
    unittest.main()
