"""services/claude_service.py's check_forbidden_imports — the AST-based
allowlist gate from the custom_features design (design point 5). Pure unit
tests, no Anthropic API calls: exercises the AST walk directly, not
generate_custom_feature's retry loop (that needs a live client and is out of
scope for a fast unit suite).
"""
from __future__ import annotations

import unittest

from services.claude_service import check_forbidden_imports


class CleanCodeHasNoViolationsTests(unittest.TestCase):
    def test_allowed_stdlib_and_aiogram_imports_pass(self):
        code = (
            "import asyncio\nimport aiosqlite\nfrom pathlib import Path\n"
            "from aiogram import Router\nrouter = Router()\n"
        )
        self.assertEqual(check_forbidden_imports(code), [])

    def test_no_imports_at_all_passes(self):
        self.assertEqual(check_forbidden_imports("x = 1\n"), [])


class ForbiddenImportsAreFlaggedTests(unittest.TestCase):
    def test_plain_import_of_forbidden_package(self):
        self.assertEqual(check_forbidden_imports("import pandas\n"), ["pandas"])

    def test_from_import_of_forbidden_package(self):
        self.assertEqual(check_forbidden_imports("from sqlalchemy import Column\n"), ["sqlalchemy"])

    def test_multiple_forbidden_imports_all_reported(self):
        code = "import pandas\nimport requests\nfrom PIL import Image\n"
        self.assertEqual(check_forbidden_imports(code), ["pandas", "requests", "PIL"])

    def test_submodule_import_is_checked_by_root_name(self):
        # os.path -> root "os", allowed; xml.etree.ElementTree -> root "xml", not allowed
        code = "import os.path\nimport xml.etree.ElementTree\n"
        self.assertEqual(check_forbidden_imports(code), ["xml"])

    def test_mixed_clean_and_forbidden_only_reports_forbidden(self):
        code = "import aiosqlite\nimport numpy\nfrom aiogram import Router\n"
        self.assertEqual(check_forbidden_imports(code), ["numpy"])


class SyntaxErrorPropagatesTests(unittest.TestCase):
    def test_unparseable_code_raises_syntax_error(self):
        with self.assertRaises(SyntaxError):
            check_forbidden_imports("def broken(:\n")


if __name__ == "__main__":
    unittest.main()
