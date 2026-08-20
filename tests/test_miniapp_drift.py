"""Runs scripts/check_miniapp_drift.py's core check against every
templates/*.py file that declares a module-level miniapp_config, as a normal
part of the test suite (see that script's own module docstring for the full
rationale). Only HARD mismatches (hallucinated/renamed table or column) are
asserted on — SOFT warnings are heuristic-only and never fail this test,
same posture as the standalone script's exit code.
"""
from __future__ import annotations

import unittest

from scripts.check_miniapp_drift import _template_files_with_miniapp_config, check_template


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


if __name__ == "__main__":
    unittest.main()
