"""payment-eventloop-fix — regression tests for the two run_in_executor wraps.

Context: payment-subsystem-inventory found that a synchronous
importlib.import_module() (first import of a new template, runtime/registry.py)
and openpyxl's Workbook.save() (accountant.py / trip_manager.py's /excel) both
run on combined_app.py's single shared event loop. Either one holding the loop
long enough can blow Telegram's 10s answerPreCheckoutQuery window for a payment
on a COMPLETELY DIFFERENT bot. Both are now offloaded via run_in_executor.

Two things need proving, not just asserting:
1. The fix actually decouples one bot's slow blocking call from another bot's
   concurrent request (registry.py side) — with a "before" companion test that
   reproduces the pre-fix stall, so this suite would fail if the fix were
   reverted, not just if it were broken.
2. Wrapping wb.save() in run_in_executor introduces the first `await` inside
   /excel's previously-atomic body — opening a window for a double tap on the
   SAME bot to start a second export concurrently. That window is closed by the
   busy-set guard added alongside the run_in_executor wrap; this suite proves
   the second tap gets "already running" instead of racing on the same file.
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import templates
import runtime.registry as reg
from templates import accountant, trip_manager
from openpyxl import Workbook

FAKE_TOKEN = "123456789:AAHfakeTokenButShapedRight1234567890"
SLOW_IMPORT_SECONDS = 0.3

_SLOW_IMPORT_FIXTURE_SOURCE = f'''
# TEMPLATE: slow_import_fixture
import time
time.sleep({SLOW_IMPORT_SECONDS})
from aiogram import Router

router = Router()


class FixtureConfig:
    def __init__(self, bot_id, db_path):
        self.bot_id = bot_id
        self.db_path = db_path


def config_from_bot_row(bot_row, data_dir):
    return FixtureConfig(bot_row["bot_id"], str(data_dir / f"bot_{{bot_row['bot_id']}}_slow.db"))


class ConfigMiddleware:
    def __init__(self, config):
        self.config = config

    async def __call__(self, handler, event, data):
        data["config"] = self.config
        return await handler(event, data)


async def init_db(db_path):
    pass
'''

_FAST_FIXTURE_SOURCE = '''
# TEMPLATE: fast_fixture
from aiogram import Router

router = Router()


class FixtureConfig:
    def __init__(self, bot_id, db_path):
        self.bot_id = bot_id
        self.db_path = db_path


def config_from_bot_row(bot_row, data_dir):
    return FixtureConfig(bot_row["bot_id"], str(data_dir / f"bot_{bot_row['bot_id']}_fast.db"))


class ConfigMiddleware:
    def __init__(self, config):
        self.config = config

    async def __call__(self, handler, event, data):
        data["config"] = self.config
        return await handler(event, data)


async def init_db(db_path):
    pass
'''


class SlowTemplateImportDoesNotBlockConcurrentBot(unittest.IsolatedAsyncioTestCase):
    """The Task 1 finding, proven: a slow first-import of one bot's template
    must not delay a concurrently-registered different bot on a different
    template. Includes a "before" companion that patches out the executor wrap
    to reproduce the pre-fix stall — this is what makes the "after" assertion
    meaningful rather than incidentally true."""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self._tmp.name)
        templates.__path__.append(str(self.tmp_dir))
        (self.tmp_dir / "slow_import_fixture.py").write_text(_SLOW_IMPORT_FIXTURE_SOURCE, encoding="utf-8")
        (self.tmp_dir / "fast_fixture.py").write_text(_FAST_FIXTURE_SOURCE, encoding="utf-8")

    async def asyncTearDown(self):
        templates.__path__ = [p for p in templates.__path__ if p != str(self.tmp_dir)]
        for tid in ("slow_import_fixture", "fast_fixture"):
            reg._template_module_cache.pop(tid, None)
            sys.modules.pop(f"templates.{tid}", None)
        self._tmp.cleanup()

    async def _timed_build_entry(self, loop, bot_id, template_id, name):
        entry = await reg.build_entry(bot_id, FAKE_TOKEN, template_id, {"bot_id": bot_id, "name": name})
        return loop.time(), entry

    async def test_after_fix_fast_bot_is_not_delayed_by_slow_bot(self):
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        # Both tasks are created back-to-back, BEFORE either gets to run — this
        # matters: if the slow task's first scheduled step ran synchronously to
        # completion (the pre-fix shape), it would consume its full 0.3s before
        # the fast task ever got a turn, and a timer started only after an
        # intervening `await` would miss that delay entirely (see the
        # regression test below, which hit exactly this measurement bug during
        # development).
        task_slow = asyncio.create_task(self._timed_build_entry(loop, 9201, "slow_import_fixture", "slow_bot"))
        task_fast = asyncio.create_task(self._timed_build_entry(loop, 9202, "fast_fixture", "fast_bot"))
        (t_slow, entry_slow), (t_fast, entry_fast) = await asyncio.gather(task_slow, task_fast)
        elapsed_fast = t_fast - t0

        try:
            self.assertEqual(entry_fast.template_id, "fast_fixture")
            self.assertLess(
                elapsed_fast, SLOW_IMPORT_SECONDS / 2,
                f"fast bot's build_entry finished after {elapsed_fast:.3f}s — "
                "the slow bot's import blocked the shared loop",
            )
        finally:
            await entry_slow.bot.session.close()
            await entry_fast.bot.session.close()

    async def test_before_fix_regression_would_have_blocked_the_fast_bot(self):
        """Patches _load_template_module_async back to a direct (non-executor)
        import — the exact pre-fix shape — to prove the assertion above is
        actually discriminating, not just always true regardless of the fix."""

        async def _regressed(template_id):
            if template_id in reg._template_module_cache:
                return reg._template_module_cache[template_id]
            reg._apply_pre_import_overrides(template_id, caller="regressed-test-path")
            import importlib
            module = importlib.import_module(f"templates.{template_id}")
            reg._template_module_cache[template_id] = module
            return module

        loop = asyncio.get_running_loop()
        t0 = loop.time()
        with patch.object(reg, "_load_template_module_async", _regressed):
            task_slow = asyncio.create_task(self._timed_build_entry(loop, 9301, "slow_import_fixture", "slow_bot"))
            task_fast = asyncio.create_task(self._timed_build_entry(loop, 9302, "fast_fixture", "fast_bot"))
            (t_slow, entry_slow), (t_fast, entry_fast) = await asyncio.gather(task_slow, task_fast)
        elapsed_fast = t_fast - t0

        try:
            self.assertGreater(
                elapsed_fast, SLOW_IMPORT_SECONDS * 0.8,
                f"regressed (pre-fix) path only took {elapsed_fast:.3f}s to reach the fast bot — "
                "test no longer reproduces the bug",
            )
        finally:
            await entry_slow.bot.session.close()
            await entry_fast.bot.session.close()


def _slow_save(original_save, delay):
    def _save(self, filename):
        time.sleep(delay)
        return original_save(self, filename)
    return _save


class _FakeUser:
    def __init__(self, user_id: int):
        self.id = user_id


class _FakeMessage:
    def __init__(self, user_id: int):
        self.from_user = _FakeUser(user_id)
        self.answers: list[str] = []
        self.documents: list = []

    async def answer(self, text, *args, **kwargs):
        self.answers.append(text)

    async def answer_document(self, document, *args, **kwargs):
        self.documents.append(document)


class _FakeState:
    async def clear(self):
        pass


class AccountantExcelDoubleTapDoesNotRace(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = accountant.config_from_bot_row(
            {"bot_id": 501, "name": "excel_dtap_bot", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await accountant.init_db(self.config.db_path)
        conn = sqlite3.connect(self.config.db_path)
        conn.execute("INSERT INTO projects (name) VALUES ('P1')")
        conn.commit()
        conn.close()
        accountant._save_admins(self.config.admins_file, {"1"})

    async def asyncTearDown(self):
        accountant._busy_excel_exports.discard(self.config.excel_path)
        self._tmp.cleanup()

    async def test_second_tap_gets_busy_reply_not_a_race(self):
        msg1, msg2 = _FakeMessage(1), _FakeMessage(1)
        state1, state2 = _FakeState(), _FakeState()

        with patch.object(Workbook, "save", _slow_save(Workbook.save, 0.2)):
            task1 = asyncio.create_task(accountant.cmd_excel(msg1, state1, self.config))
            await asyncio.sleep(0.05)  # let task1 pass the guard check and reach the slowed save()
            task2 = asyncio.create_task(accountant.cmd_excel(msg2, state2, self.config))
            await asyncio.gather(task1, task2)

        self.assertEqual(msg2.answers, [accountant._EXCEL_BUSY_TEXT])
        self.assertEqual(msg2.documents, [])
        self.assertEqual(len(msg1.documents), 1)
        self.assertNotIn(self.config.excel_path, accountant._busy_excel_exports)


class TripManagerExcelDoubleTapDoesNotRace(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = trip_manager.config_from_bot_row(
            {"bot_id": 601, "name": "excel_dtap_bot", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await trip_manager.init_db(self.config.db_path)
        conn = sqlite3.connect(self.config.db_path)
        conn.execute("INSERT INTO trips (name) VALUES ('T1')")
        conn.commit()
        conn.close()
        trip_manager._save_admins(self.config.admins_file, {"1"})

    async def asyncTearDown(self):
        trip_manager._busy_excel_exports.discard(self.config.excel_path)
        self._tmp.cleanup()

    async def test_second_tap_gets_busy_reply_not_a_race(self):
        msg1, msg2 = _FakeMessage(1), _FakeMessage(1)

        with patch.object(Workbook, "save", _slow_save(Workbook.save, 0.2)):
            task1 = asyncio.create_task(trip_manager.cmd_excel(msg1, self.config))
            await asyncio.sleep(0.05)
            task2 = asyncio.create_task(trip_manager.cmd_excel(msg2, self.config))
            await asyncio.gather(task1, task2)

        self.assertEqual(msg2.answers, [trip_manager._EXCEL_BUSY_TEXT])
        self.assertEqual(msg2.documents, [])
        self.assertEqual(len(msg1.documents), 1)
        self.assertNotIn(self.config.excel_path, trip_manager._busy_excel_exports)


if __name__ == "__main__":
    unittest.main()
