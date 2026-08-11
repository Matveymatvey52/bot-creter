"""Dynamic template discovery phase (services/claude_service.py's discover_templates()).

Main criterion this phase exists to prove: adding a new file to templates/*.py
with a valid "# TEMPLATE: <id>" / "# USE FOR: <description>" header makes it show
up in the Claude selection prompt automatically — with ZERO code changes to
claude_service.py. The fixture template used below lives in a temp directory,
never in the real templates/ (that stays untouched, per this phase's own scope).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import services.claude_service as claude_service


class DiscoverTemplatesFindsNewFixtureWithNoCodeChange(unittest.TestCase):
    """The headline claim: a brand-new template file is picked up purely by being
    dropped into the scanned directory — discover_templates() itself is never
    touched or special-cased for this fixture."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self._tmp.name)
        self._patcher = patch.object(claude_service, "_TEMPLATES_DIR", self.tmp_dir)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self._tmp.cleanup()

    def test_new_fixture_template_is_discovered_and_appears_in_the_prompt(self):
        (self.tmp_dir / "gym_membership.py").write_text(
            "# TEMPLATE: gym_membership\n"
            "# USE FOR: gym/fitness club membership tracking, абонементы, тренажёрный зал\n"
            "# CUSTOMIZE: sections marked with # CUSTOMIZE\n"
            "from __future__ import annotations\n",
            encoding="utf-8",
        )

        found = claude_service.discover_templates()
        names = [t["name"] for t in found]
        self.assertIn("gym_membership", names)
        entry = next(t for t in found if t["name"] == "gym_membership")
        self.assertEqual(entry["use_for"], "gym/fitness club membership tracking, абонементы, тренажёрный зал")

        prompt = claude_service._build_template_select_prompt()
        self.assertIn("- gym_membership — gym/fitness club membership tracking", prompt)

    def test_file_with_missing_header_is_skipped_with_warning_not_fatal(self):
        (self.tmp_dir / "broken.py").write_text(
            "# just some code, no TEMPLATE/USE FOR header at all\n"
            "from __future__ import annotations\n",
            encoding="utf-8",
        )
        (self.tmp_dir / "good.py").write_text(
            "# TEMPLATE: good\n"
            "# USE FOR: a working fixture template\n",
            encoding="utf-8",
        )

        with self.assertLogs("services.claude_service", level="WARNING") as logs:
            found = claude_service.discover_templates()

        names = [t["name"] for t in found]
        self.assertNotIn("broken", names)  # skipped, did not raise
        self.assertIn("good", names)  # a broken sibling file must not hide a good one
        self.assertTrue(any("broken.py" in msg for msg in logs.output))

    def test_file_with_template_but_no_use_for_is_also_skipped(self):
        (self.tmp_dir / "half_header.py").write_text(
            "# TEMPLATE: half_header\n"
            "# some other comment, no USE FOR line\n",
            encoding="utf-8",
        )

        with self.assertLogs("services.claude_service", level="WARNING"):
            found = claude_service.discover_templates()

        self.assertNotIn("half_header", [t["name"] for t in found])

    def test_long_use_for_line_is_not_silently_truncated(self):
        """Review-found gap: a byte/char budget (the pre-fix approach) would
        silently cut a long USE FOR: line mid-word with no error, no warning —
        just a corrupted value fed to Claude. The line-based read must capture
        it in full regardless of length."""
        long_use_for = "a" * 300 + " very specific long use case description"
        (self.tmp_dir / "verbose.py").write_text(
            f"# TEMPLATE: verbose\n# USE FOR: {long_use_for}\n",
            encoding="utf-8",
        )

        found = {t["name"]: t["use_for"] for t in claude_service.discover_templates()}
        self.assertEqual(found["verbose"], long_use_for)

    def test_underscore_prefixed_files_are_skipped_silently_no_warning(self):
        """A future shared helper module (templates/_common.py) must not be
        treated as a template, and must not spam a warning on every single call
        forever — it's not a malformed template, it's not a template at all."""
        (self.tmp_dir / "_common.py").write_text(
            "# shared helper code, not a template\n", encoding="utf-8"
        )
        (self.tmp_dir / "real.py").write_text(
            "# TEMPLATE: real\n# USE FOR: a real fixture template\n", encoding="utf-8"
        )

        with patch.object(claude_service.logger, "warning") as mock_warning:
            found = claude_service.discover_templates()

        self.assertNotIn("_common", [t["name"] for t in found])
        self.assertIn("real", [t["name"] for t in found])
        mock_warning.assert_not_called()


class DiscoverTemplatesRegressionOnRealTemplates(unittest.TestCase):
    """Regression: all existing templates/*.py files must still be found and
    correctly parsed — including tour_operator.py, whose third header line is
    "# STANDALONE" instead of "# CUSTOMIZE" (a known, accepted format divergence
    that must not affect TEMPLATE:/USE FOR: parsing). booking_medical,
    event_manager, debtors, vehicle_service, rental_equipment,
    loyalty_program, feedback_survey, and staff_scheduler were each added
    after this test was first written — every new template added purely by dropping a file into
    templates/, with zero changes to claude_service.py, needs this set
    updated to match."""

    def test_all_real_templates_are_discovered(self):
        found = claude_service.discover_templates()
        names = {t["name"] for t in found}
        self.assertEqual(
            names,
            {"accountant", "booking_beauty", "booking_fitness", "booking_medical", "campaign_tracker", "debtors", "event_manager", "expense_tracker", "feedback_survey", "inventory", "loyalty_program", "manager_secretary", "moderator", "orders_tracker", "referral_program", "rental_equipment", "shop_catalog", "staff_scheduler", "tour_operator", "tourist_documents", "trip_manager", "vehicle_service"},
        )

    def test_tour_operator_use_for_is_parsed_despite_missing_customize_line(self):
        found = {t["name"]: t["use_for"] for t in claude_service.discover_templates()}
        self.assertEqual(
            found["tour_operator"],
            "профессиональный тур-оператор, коммерческие туры, управление группами",
        )

    def test_prompt_lists_all_real_templates(self):
        prompt = claude_service._build_template_select_prompt()
        for name in ("accountant", "booking_beauty", "booking_fitness", "booking_medical", "campaign_tracker", "debtors", "inventory", "manager_secretary", "moderator", "orders_tracker", "referral_program", "shop_catalog", "tour_operator", "tourist_documents", "trip_manager"):
            self.assertIn(f"- {name} —", prompt)


if __name__ == "__main__":
    unittest.main()
