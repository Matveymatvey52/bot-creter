"""Runs scripts/check_miniapp_drift.py's core check against every
templates/*.py file that declares a module-level miniapp_config, as a normal
part of the test suite (see that script's own module docstring for the full
rationale). Only HARD mismatches (hallucinated/renamed table or column) are
asserted on — SOFT warnings are heuristic-only and never fail this test,
same posture as the standalone script's exit code.
"""
from __future__ import annotations

import asyncio
import unittest


def setUpModule() -> None:
    """check_template() imports template modules, and some of them touch
    asyncio.get_event_loop() at import time. An IsolatedAsyncioTestCase that
    ran earlier in the same process (e.g. tests/test_miniapp_scope.py) closes
    the loop it made and leaves the thread without one, so this file would
    fail on a missing loop rather than on any real drift — a failure that
    depends on test ordering and says nothing about the code under test.
    Give the thread a loop back before importing anything."""
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

from scripts.check_miniapp_drift import (
    TEMPLATES_DIR,
    _template_files_with_miniapp_config,
    check_template,
)


class MiniappConfigMatchesTemplateSchema(unittest.TestCase):
    def test_every_template_with_miniapp_config_has_no_hard_mismatch(self):
        template_files = _template_files_with_miniapp_config()
        self.assertGreater(
            len(template_files), 0,
            "expected at least one templates/*.py with a module-level miniapp_config",
        )
        for template_path in template_files:
            with self.subTest(template=template_path.name):
                hard_errors, _soft_warnings = check_template(template_path)
                self.assertEqual(
                    hard_errors, [],
                    f"{template_path.name}: miniapp_config drifted from its own schema: {hard_errors}",
                )


class ScopeCheckActuallyFailsOnBrokenInput(unittest.TestCase):
    """A check that never fails proves nothing. The scope rules were added
    to check_template() specifically to stop docs/SCOPE_AUDIT_STAGE_A.md's
    defect from reappearing, so each rule is exercised against a template
    deliberately broken in that exact way — a real file on disk run through
    the real check_template(), not a stubbed config, since the interesting
    half of the check is the comparison against CREATE TABLE.

    Guard against rot: if someone weakens the rules, these fail loudly
    rather than the suite quietly going green on a broken codebase."""

    #: A minimal but complete template: two tables, two resources, one of
    #: which genuinely belongs to the other.
    GOOD = '''
import aiosqlite

async def init_db(db_path):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS folders (
                id   INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS notes (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                folder_id INTEGER,
                body      TEXT
            )
        """)

miniapp_config = {
    "resources": [
        {
            "name": "folders",
            "table": "folders",
            "creatable": True,
            "scope": {"type": "global"},
            "title": "Папки",
            "titleField": "name",
            "fields": [{"name": "name", "label": "Название", "kind": "text"}],
        },
        {
            "name": "notes",
            "table": "notes",
            "creatable": True,
            "scope": {"type": "scoped", "parent": "folders", "via": "folder_id"},
            "title": "Заметки",
            "titleField": "body",
            "fields": [
                {"name": "folder_id", "label": "Папка", "kind": "number"},
                {"name": "body", "label": "Текст", "kind": "text"},
            ],
        },
    ],
}
'''

    def _check(self, source: str) -> list[str]:
        """Writes source as a real templates/*.py, runs the real check on it,
        removes it again. Uses the templates package itself because
        check_template() imports by module name."""
        import importlib
        import sys
        import uuid

        stem = f"_scope_probe_{uuid.uuid4().hex[:8]}"
        path = TEMPLATES_DIR / f"{stem}.py"
        path.write_text(source, encoding="utf-8")
        try:
            return check_template(path)[0]
        finally:
            sys.modules.pop(f"templates.{stem}", None)
            importlib.invalidate_caches()
            path.unlink(missing_ok=True)

    def test_the_control_case_passes(self):
        """Without this, every assertion below could be passing for the
        wrong reason — e.g. a probe template that fails to import at all."""
        self.assertEqual(self._check(self.GOOD), [])

    def test_a_resource_with_no_scope_is_a_hard_error(self):
        broken = self.GOOD.replace(
            '            "scope": {"type": "scoped", "parent": "folders", "via": "folder_id"},\n', ""
        )
        errors = self._check(broken)
        self.assertTrue(
            any('no "scope" declared' in e for e in errors),
            f"an undeclared scope must fail the build, got: {errors}",
        )

    def test_scoped_by_a_column_that_does_not_exist_is_a_hard_error(self):
        broken = self.GOOD.replace('"via": "folder_id"', '"via": "owner_id"')
        errors = self._check(broken)
        self.assertTrue(
            any("not a real column" in e for e in errors),
            f"a scope pointing at a phantom column must fail the build, got: {errors}",
        )

    def test_scoped_to_a_parent_that_is_not_a_resource_is_a_hard_error(self):
        broken = self.GOOD.replace('"parent": "folders"', '"parent": "cabinets"')
        errors = self._check(broken)
        self.assertTrue(
            any("is not a resource in this config" in e for e in errors),
            f"a scope naming an unknown parent must fail the build, got: {errors}",
        )

    def test_an_unnameable_parent_is_a_hard_error(self):
        """A parent with no titleField cannot be shown in the context line,
        which is the whole point of declaring the scope."""
        broken = self.GOOD.replace('            "titleField": "name",\n', "", 1)
        errors = self._check(broken)
        self.assertTrue(
            any("cannot be named in the UI" in e for e in errors),
            f"a parent with no displayable name must fail the build, got: {errors}",
        )


if __name__ == "__main__":
    unittest.main()
