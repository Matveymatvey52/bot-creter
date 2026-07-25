"""Regression test for the Railway prod crash: ModuleNotFoundError: No module
named 'config' when runtime/combined_app.py is launched the way Railway
actually launches it — `python runtime/combined_app.py` (BOT_SCRIPT set to a
file path in start.sh: `exec python ${BOT_SCRIPT:-main.py}`), NOT
`python -m runtime.combined_app`.

This is exactly the class of bug ordinary unit tests can't catch: importing
the module directly in-process (`import runtime.combined_app`, or any test
that does so) always has the project root on sys.path already (pytest/
unittest add it, or it's the CWD), so it can never reproduce a bug that is
caused BY how sys.path gets populated for a specific invocation form. Only a
real subprocess, launched the same way Railway launches it, can catch this.

Uses `python runtime/combined_app.py --check-imports` (not the real bootstrap)
so this test doesn't hit the network, spawn subprocesses, bind a port, or
touch the real DB — see the `if __name__ == "__main__":` guard in
combined_app.py for what --check-imports actually short-circuits.
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

from cryptography.fernet import Fernet

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_TEST_ENV = {
    **os.environ,
    "BOT_TOKEN": "123456:test-token-not-real",
    "ANTHROPIC_API_KEY": "test",
    "ENCRYPTION_KEY": Fernet.generate_key().decode(),
}


class CombinedAppCanBeLaunchedDirectlyLikeRailwayDoes(unittest.TestCase):
    def test_direct_file_invocation_does_not_raise_modulenotfounderror(self):
        """Reproduces Railway's actual invocation: `python runtime/combined_app.py`
        (a file path passed to `python`, exactly what start.sh's
        `exec python ${BOT_SCRIPT:-main.py}` does when
        BOT_SCRIPT=runtime/combined_app.py) — NOT `python -m runtime.combined_app`,
        which behaves differently and was never the actual bug."""
        result = subprocess.run(
            [sys.executable, "runtime/combined_app.py", "--check-imports"],
            cwd=str(PROJECT_ROOT),
            env=_TEST_ENV,
            capture_output=True,
            text=True,
            timeout=15,
        )

        self.assertNotIn(
            "ModuleNotFoundError",
            result.stderr,
            f"runtime/combined_app.py raised ModuleNotFoundError when launched "
            f"the way Railway launches it (python runtime/combined_app.py). "
            f"stderr:\n{result.stderr}",
        )
        self.assertEqual(
            result.returncode, 0,
            f"expected a clean exit after --check-imports; got {result.returncode}. "
            f"stderr:\n{result.stderr}",
        )
        self.assertIn("imports OK", result.stdout)

    def test_module_invocation_form_still_works_too(self):
        """python -m runtime.combined_app was never broken (see STAGE2_REPORT.md's
        inventory for this fix) — this just guards against a future change
        accidentally breaking THAT invocation form while fixing the other."""
        result = subprocess.run(
            [sys.executable, "-m", "runtime.combined_app", "--check-imports"],
            cwd=str(PROJECT_ROOT),
            env=_TEST_ENV,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertNotIn("ModuleNotFoundError", result.stderr)
        self.assertEqual(result.returncode, 0)
        self.assertIn("imports OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
