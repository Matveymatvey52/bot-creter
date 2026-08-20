"""services/claude_service.py's _bounded_bug_fix_diff — the unified-diff
builder that feeds explain_bug_fix_diff's Haiku preview call. Pure unit
tests, no Anthropic API calls: exercises the diff/truncation logic directly,
same scope as test_custom_feature_claude_service.py's check_forbidden_imports
tests.
"""
from __future__ import annotations

import unittest

from services.claude_service import _BUG_FIX_DIFF_LINE_LIMIT, _bounded_bug_fix_diff


class SmallDiffIsNotTruncatedTests(unittest.TestCase):
    def test_single_line_change_appears_in_full(self):
        before = "a = 1\nb = 2\n"
        after = "a = 1\nb = 3\n"
        diff = _bounded_bug_fix_diff(before, after)
        self.assertIn("-b = 2", diff)
        self.assertIn("+b = 3", diff)
        self.assertNotIn("more diff lines omitted", diff)

    def test_identical_code_produces_empty_diff(self):
        code = "a = 1\nb = 2\n"
        self.assertEqual(_bounded_bug_fix_diff(code, code), "")


class LargeDiffIsBoundedTests(unittest.TestCase):
    def test_diff_over_line_limit_is_truncated_with_marker(self):
        before = "\n".join(f"line{i} = {i}" for i in range(1000)) + "\n"
        after = "\n".join(f"line{i} = {i + 1}" for i in range(1000)) + "\n"
        diff = _bounded_bug_fix_diff(before, after)
        lines = diff.splitlines()
        # +1 for the appended "... N more diff lines omitted" marker line.
        self.assertEqual(len(lines), _BUG_FIX_DIFF_LINE_LIMIT + 1)
        self.assertTrue(lines[-1].startswith("... ("))
        self.assertIn("more diff lines omitted", lines[-1])


if __name__ == "__main__":
    unittest.main()
