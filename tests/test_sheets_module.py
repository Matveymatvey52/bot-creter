"""features/sheets.py — Task 3 tests (sheets-feature-inventory design).

Three criteria from the owner's brief:
  - write_row/read_data raise ValueError when bot_id has no connected sheet
    (same contract as create_invoice() raising with no configured provider)
  - verify_access resolves cleanly (returns None, never raises) when the
    Service Account can't open a spreadsheet_id — persistence-before-access-
    check is tested at the FSM level (tests/test_sheets_connect_flow.py),
    since verify_access itself never touches bot_sheets_config
  - gspread's synchronous calls are actually offloaded via asyncio.to_thread —
    a slow Sheets API round trip on one bot must not block a concurrent bot's
    request on the same shared event loop, same class of proof as
    tests/test_payment_eventloop_fix.py's accountant/trip_manager checks

Run with: python -m unittest tests.test_sheets_module
"""
from __future__ import annotations

import asyncio
import base64
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import gspread

import features.sheets as sheets_module
from db.database import create_bot_record_with_admins, delete_bot, set_bot_sheets_config

FAKE_TOKEN = "123456789:AAHfakeTokenButShapedRight1234567890"


class WriteRowWithoutConnectedSheetRaisesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot_id = await create_bot_record_with_admins(
            name="sheets_no_connect_bot", description="test", token=FAKE_TOKEN,
            file_path="templates/inventory.py", admin_ids=["1"],
        )

    async def asyncTearDown(self):
        await delete_bot(self.bot_id)

    async def test_write_row_raises_value_error_without_connected_sheet(self):
        with self.assertRaises(ValueError):
            await sheets_module.write_row(self.bot_id, "Sheet1", ["a", "b"])

    async def test_read_data_raises_value_error_without_connected_sheet(self):
        with self.assertRaises(ValueError):
            await sheets_module.read_data(self.bot_id, "Sheet1")


class VerifyAccessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._client_patcher = patch.object(sheets_module, "_get_client", new=AsyncMock())
        self.mock_get_client = self._client_patcher.start()
        self.mock_gc = MagicMock()
        self.mock_get_client.return_value = self.mock_gc

    async def asyncTearDown(self):
        self._client_patcher.stop()

    async def test_returns_title_on_success(self):
        fake_spreadsheet = MagicMock()
        fake_spreadsheet.title = "My Sheet"
        self.mock_gc.open_by_key.return_value = fake_spreadsheet

        result = await sheets_module.verify_access("some-id")

        self.assertEqual(result, "My Sheet")

    async def test_returns_none_on_not_found_instead_of_raising(self):
        self.mock_gc.open_by_key.side_effect = gspread.exceptions.SpreadsheetNotFound()

        result = await sheets_module.verify_access("bad-id")

        self.assertIsNone(result)


class WriteRowDoesNotBlockEventLoopTests(unittest.IsolatedAsyncioTestCase):
    """Proves append_row's synchronous gspread call is actually offloaded to a
    worker thread (asyncio.to_thread), not run inline on the shared loop —
    same measurement technique as test_payment_eventloop_fix.py."""

    SLOW_SECONDS = 0.3

    async def asyncSetUp(self):
        self.bot_id = await create_bot_record_with_admins(
            name="sheets_blocking_bot", description="test", token=FAKE_TOKEN,
            file_path="templates/inventory.py", admin_ids=["1"],
        )
        await set_bot_sheets_config(self.bot_id, "fake-spreadsheet-id", "Fake Sheet")

        self._client_patcher = patch.object(sheets_module, "_get_client", new=AsyncMock())
        mock_get_client = self._client_patcher.start()
        mock_gc = MagicMock()
        mock_get_client.return_value = mock_gc

        def _slow_append_row(row):
            time.sleep(self.SLOW_SECONDS)

        fake_worksheet = MagicMock()
        fake_worksheet.append_row.side_effect = _slow_append_row
        fake_spreadsheet = MagicMock()
        fake_spreadsheet.worksheet.return_value = fake_worksheet
        mock_gc.open_by_key.return_value = fake_spreadsheet

    async def asyncTearDown(self):
        self._client_patcher.stop()
        await delete_bot(self.bot_id)

    async def test_slow_gspread_call_does_not_block_other_bot(self):
        loop = asyncio.get_running_loop()

        async def _other_bot_tick():
            for _ in range(20):
                await asyncio.sleep(0)
            return loop.time()

        t0 = loop.time()
        write_task = asyncio.create_task(sheets_module.write_row(self.bot_id, "Sheet1", ["x"]))
        other_task = asyncio.create_task(_other_bot_tick())
        other_finished_at, _ = await asyncio.gather(other_task, write_task)

        elapsed_other = other_finished_at - t0
        self.assertLess(
            elapsed_other, self.SLOW_SECONDS / 2,
            f"a concurrent lightweight task took {elapsed_other:.3f}s while write_row's "
            f"{self.SLOW_SECONDS}s gspread call ran — the sync call is blocking the event loop",
        )

    async def test_before_fix_regression_would_have_blocked_the_other_bot(self):
        """Patches asyncio.to_thread back to a direct call (the pre-fix shape)
        to prove the assertion above is actually discriminating."""
        loop = asyncio.get_running_loop()

        async def _other_bot_tick():
            for _ in range(20):
                await asyncio.sleep(0)
            return loop.time()

        async def _regressed_to_thread(func, /, *args, **kwargs):
            return func(*args, **kwargs)

        t0 = loop.time()
        with patch.object(sheets_module.asyncio, "to_thread", _regressed_to_thread):
            write_task = asyncio.create_task(sheets_module.write_row(self.bot_id, "Sheet1", ["x"]))
            other_task = asyncio.create_task(_other_bot_tick())
            other_finished_at, _ = await asyncio.gather(other_task, write_task)

        elapsed_other = other_finished_at - t0
        self.assertGreater(
            elapsed_other, self.SLOW_SECONDS * 0.8,
            f"regressed (pre-fix) path only took {elapsed_other:.3f}s — "
            "test no longer reproduces the blocking bug",
        )


class LoadCredentialsInfoTests(unittest.TestCase):
    """_load_credentials_info() — GOOGLE_SHEETS_SA_KEY_B64 (in-memory, added
    because pasting raw JSON through Railway's Console proved unreliable)
    vs the legacy GOOGLE_SHEETS_SA_KEY_PATH file, and priority between them."""

    FAKE_INFO = {"type": "service_account", "client_email": "fake@example.iam.gserviceaccount.com"}

    def setUp(self):
        self._b64_patcher = patch.object(sheets_module, "GOOGLE_SHEETS_SA_KEY_B64", None)
        self._path_patcher = patch.object(sheets_module, "GOOGLE_SHEETS_SA_KEY_PATH", None)
        self._b64_patcher.start()
        self._path_patcher.start()

    def tearDown(self):
        self._path_patcher.stop()
        self._b64_patcher.stop()

    def test_decodes_b64_env_var_into_credentials_dict(self):
        b64_value = base64.b64encode(json.dumps(self.FAKE_INFO).encode()).decode()
        with patch.object(sheets_module, "GOOGLE_SHEETS_SA_KEY_B64", b64_value):
            self.assertEqual(sheets_module._load_credentials_info(), self.FAKE_INFO)

    def test_invalid_b64_returns_none_instead_of_raising(self):
        with patch.object(sheets_module, "GOOGLE_SHEETS_SA_KEY_B64", "not valid base64!!!"):
            self.assertIsNone(sheets_module._load_credentials_info())

    def test_falls_back_to_file_path_when_b64_not_set(self):
        # Regression: the pre-existing route must keep working unchanged now
        # that _B64 is checked first.
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(self.FAKE_INFO, f)
            key_path = f.name
        try:
            with patch.object(sheets_module, "GOOGLE_SHEETS_SA_KEY_PATH", key_path):
                self.assertEqual(sheets_module._load_credentials_info(), self.FAKE_INFO)
        finally:
            Path(key_path).unlink(missing_ok=True)

    def test_b64_takes_priority_over_file_path_when_both_are_set(self):
        b64_info = {**self.FAKE_INFO, "client_email": "from-b64@example.iam.gserviceaccount.com"}
        b64_value = base64.b64encode(json.dumps(b64_info).encode()).decode()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(self.FAKE_INFO, f)  # different email — proves which source actually won
            key_path = f.name
        try:
            with patch.object(sheets_module, "GOOGLE_SHEETS_SA_KEY_B64", b64_value), \
                 patch.object(sheets_module, "GOOGLE_SHEETS_SA_KEY_PATH", key_path):
                self.assertEqual(sheets_module._load_credentials_info(), b64_info)
        finally:
            Path(key_path).unlink(missing_ok=True)

    def test_returns_none_when_neither_is_set(self):
        self.assertIsNone(sheets_module._load_credentials_info())


class GetServiceAccountEmailUsesB64WhenSetTests(unittest.IsolatedAsyncioTestCase):
    """End-to-end through the async public function, not just the sync helper."""

    async def asyncSetUp(self):
        sheets_module._sa_email = None  # module-level cache from a previous test/call
        info = {"type": "service_account", "client_email": "b64-email@example.iam.gserviceaccount.com"}
        b64_value = base64.b64encode(json.dumps(info).encode()).decode()
        self._patcher = patch.object(sheets_module, "GOOGLE_SHEETS_SA_KEY_B64", b64_value)
        self._patcher.start()

    async def asyncTearDown(self):
        self._patcher.stop()
        sheets_module._sa_email = None

    async def test_returns_email_decoded_from_b64_var(self):
        email = await sheets_module.get_service_account_email()
        self.assertEqual(email, "b64-email@example.iam.gserviceaccount.com")


if __name__ == "__main__":
    unittest.main()
