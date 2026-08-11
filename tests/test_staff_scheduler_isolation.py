"""Data isolation + conflict-detection smoke tests for the staff_scheduler template.

Standard isolation criterion (same as every other *_isolation.py test in this
repo): two bots on the SAME template, different config, must never mix data.
Everything lives in a tempfile.TemporaryDirectory, never data/bots.db.

Domain-specific criterion (this template's own headline claim, per
docs/STAGE2_DESIGN.md "Фаза «новый шаблон: staff_scheduler»"): a shift that
overlaps an existing shift for the SAME employee must trigger an explicit
warning on the confirmation screen, never a silent save.

No real Telegram network calls, no real tokens.

Run with: python -m unittest tests.test_staff_scheduler_isolation
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from runtime.registry import get_template_router
from templates import staff_scheduler as ssc

FAKE_TOKEN = "123456:test-token-not-real"


def _text_update(update_id: int, user_id: int, text: str) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id, "date": 1700000000,
            "chat": {"id": user_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
            "text": text,
        },
    }


def _callback_update(update_id: int, user_id: int, data: str, msg_id: int = 1) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": str(update_id),
            "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
            "message": {
                "message_id": msg_id, "date": 1700000000,
                "chat": {"id": user_id, "type": "private"}, "text": "placeholder",
            },
            "chat_instance": "1", "data": data,
        },
    }


def _build_bot_dispatcher(config: ssc.StaffSchedulerConfig) -> tuple[Bot, Dispatcher]:
    bot = Bot(token=FAKE_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(ssc.ConfigMiddleware(config))
    dp.include_router(get_template_router("staff_scheduler"))
    return bot, dp


class StaffSchedulerIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

        self.config_a = ssc.config_from_bot_row(
            {"bot_id": 901, "name": "scheduler_bot_a", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        self.config_b = ssc.config_from_bot_row(
            {"bot_id": 902, "name": "scheduler_bot_b", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await ssc.init_db(self.config_a.db_path)
        await ssc.init_db(self.config_b.db_path)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_configs_point_to_different_files(self):
        self.assertNotEqual(self.config_a.db_path, self.config_b.db_path)
        self.assertNotEqual(self.config_a.admins_file, self.config_b.admins_file)

    async def test_employees_and_shifts_isolated_across_bots(self):
        """An employee (and their shift) added on bot A must not appear on
        bot B, even though both DBs share the same schema and could
        theoretically collide on AUTOINCREMENT id if pointed at the same file."""
        emp_a = await ssc._insert_employee(self.config_a.db_path, "Alice", "Barista", "+100")
        emp_b = await ssc._insert_employee(self.config_b.db_path, "Bob", "Cashier", "+200")
        today = date.today().isoformat()
        await ssc._insert_shift(self.config_a.db_path, emp_a, today, "09:00", "17:00", None)
        await ssc._insert_shift(self.config_b.db_path, emp_b, today, "10:00", "18:00", None)

        employees_a = await ssc._active_employees(self.config_a.db_path)
        employees_b = await ssc._active_employees(self.config_b.db_path)
        self.assertEqual([e["name"] for e in employees_a], ["Alice"])
        self.assertEqual([e["name"] for e in employees_b], ["Bob"])

        shifts_a = await ssc._shifts_for_day(self.config_a.db_path, today)
        shifts_b = await ssc._shifts_for_day(self.config_b.db_path, today)
        self.assertEqual([s["employee_name"] for s in shifts_a], ["Alice"])
        self.assertEqual([s["employee_name"] for s in shifts_b], ["Bob"])

    async def test_start_menu_is_buttons_not_text_commands(self):
        """Owner-mandated design rule: no raw text command lists — every
        action is a clickable button."""
        bot, dp = _build_bot_dispatcher(self.config_a)
        with patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock())) as mock_call:
            await dp.feed_webhook_update(bot, _text_update(1, 42, "/start"))  # 42 becomes admin
            sent = [
                call.args[0] for call in mock_call.call_args_list
                if call.args and hasattr(call.args[0], "reply_markup") and call.args[0].reply_markup
            ]
            found_add_shift_btn = any(
                btn.text == "➕ Добавить смену"
                for method in sent for row in method.reply_markup.keyboard for btn in row
            )
            self.assertTrue(found_add_shift_btn)

    async def test_non_admin_gets_no_access_not_the_menu(self):
        bot, dp = _build_bot_dispatcher(self.config_a)
        with patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock())) as mock_call:
            await dp.feed_webhook_update(bot, _text_update(1, 42, "/start"))  # 42 becomes first admin
            mock_call.reset_mock()
            await dp.feed_webhook_update(bot, _text_update(2, 99, "/start"))  # 99 is not an admin
            texts = [
                call.args[0].text for call in mock_call.call_args_list
                if call.args and hasattr(call.args[0], "text")
            ]
            self.assertTrue(any(ssc.NO_ACCESS_TEXT in t for t in texts if t))


class ShiftConflictDetectionTests(unittest.IsolatedAsyncioTestCase):
    """Unit-level coverage of the overlap formula itself, including the
    night-shift (crosses-midnight) case explicitly called out in the design."""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = ssc.config_from_bot_row(
            {"bot_id": 903, "name": "scheduler_conflict_bot", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await ssc.init_db(self.config.db_path)
        self.emp = await ssc._insert_employee(self.config.db_path, "Alice", None, "+100")

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def test_overlapping_same_day_shifts_are_flagged(self):
        await ssc._insert_shift(self.config.db_path, self.emp, "2026-08-17", "09:00", "17:00", None)
        conflicts = await ssc._find_conflicts(self.config.db_path, self.emp, "2026-08-17", "10:00", "18:00")
        self.assertEqual(len(conflicts), 1)

    async def test_non_overlapping_same_day_shifts_are_not_flagged(self):
        await ssc._insert_shift(self.config.db_path, self.emp, "2026-08-17", "09:00", "12:00", None)
        conflicts = await ssc._find_conflicts(self.config.db_path, self.emp, "2026-08-17", "13:00", "17:00")
        self.assertEqual(conflicts, [])

    async def test_back_to_back_shifts_touching_at_boundary_are_not_flagged(self):
        """09:00-17:00 immediately followed by 17:00-21:00 is a legitimate
        shift handover, not an overlap — strict inequality in the interval
        formula, not <=."""
        await ssc._insert_shift(self.config.db_path, self.emp, "2026-08-17", "09:00", "17:00", None)
        conflicts = await ssc._find_conflicts(self.config.db_path, self.emp, "2026-08-17", "17:00", "21:00")
        self.assertEqual(conflicts, [])

    async def test_overnight_shift_conflicts_with_next_morning_shift(self):
        """A night shift 22:00-06:00 (end<=start => crosses into the next
        calendar day) must be detected as overlapping a 05:00-09:00 shift
        recorded on the FOLLOWING date."""
        await ssc._insert_shift(self.config.db_path, self.emp, "2026-08-17", "22:00", "06:00", None)
        conflicts = await ssc._find_conflicts(self.config.db_path, self.emp, "2026-08-18", "05:00", "09:00")
        self.assertEqual(len(conflicts), 1)

    async def test_different_employees_never_conflict(self):
        other = await ssc._insert_employee(self.config.db_path, "Bob", None, "+200")
        await ssc._insert_shift(self.config.db_path, self.emp, "2026-08-17", "09:00", "17:00", None)
        conflicts = await ssc._find_conflicts(self.config.db_path, other, "2026-08-17", "09:00", "17:00")
        self.assertEqual(conflicts, [])


class AddShiftFlowEndToEndTests(unittest.IsolatedAsyncioTestCase):
    """Drives the real FSM through the bot dispatcher — proves the conflict
    warning is surfaced ON SCREEN before any save, not just correct in the
    underlying _find_conflicts() helper."""

    ADMIN_ID = 42

    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._mock_call = self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = ssc.config_from_bot_row(
            {"bot_id": 904, "name": "scheduler_flow_bot", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await ssc.init_db(self.config.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)
        await self.dp.feed_webhook_update(self.bot, _text_update(1, self.ADMIN_ID, "/start"))  # becomes admin
        self.emp = await ssc._insert_employee(self.config.db_path, "Alice", "Barista", "+100")
        self.window = ssc._week_window(date.today())

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def _drive_to_confirm(self, update_id_start: int, shift_date: str, start: str, end: str):
        uid = update_id_start
        # "➕ Добавить смену" seeds FSM data with started_at — skipping it makes
        # the very first callback below look like a stale/expired flow.
        await self.dp.feed_webhook_update(self.bot, _text_update(uid, self.ADMIN_ID, "➕ Добавить смену")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, self.ADMIN_ID, f"ssc_shift_employee:{self.emp}")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, self.ADMIN_ID, f"ssc_shift_date:{shift_date}")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, self.ADMIN_ID, f"ssc_shift_start:{start}")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, self.ADMIN_ID, f"ssc_shift_end:{end}")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, self.ADMIN_ID, "ssc_shift_skip_note")); uid += 1
        return uid

    async def test_add_shift_without_conflict_shows_plain_confirm(self):
        self._mock_call.reset_mock()
        await self._drive_to_confirm(10, self.window[0], "09:00", "17:00")

        edits = [call.args[0] for call in self._mock_call.call_args_list if hasattr(call.args[0], "text")]
        confirm_screen = edits[-1]
        self.assertNotIn("⚠️", confirm_screen.text)
        buttons = [b.text for row in confirm_screen.reply_markup.inline_keyboard for b in row]
        self.assertIn("✅ Подтвердить", buttons)

    async def test_overlapping_shift_shows_explicit_warning_not_silent(self):
        await ssc._insert_shift(self.config.db_path, self.emp, self.window[0], "09:00", "17:00", None)

        self._mock_call.reset_mock()
        await self._drive_to_confirm(20, self.window[0], "10:00", "18:00")

        edits = [call.args[0] for call in self._mock_call.call_args_list if hasattr(call.args[0], "text")]
        confirm_screen = edits[-1]
        self.assertIn("⚠️", confirm_screen.text)
        self.assertIn("Пересекается", confirm_screen.text)
        buttons = [b.text for row in confirm_screen.reply_markup.inline_keyboard for b in row]
        self.assertIn("⚠️ Всё равно добавить", buttons)
        self.assertNotIn("✅ Подтвердить", buttons)

    async def test_confirming_despite_conflict_saves_both_shifts(self):
        """The warning is a speed bump, not a hard block — tapping confirm
        after seeing it must still save, per the owner's design: explicit
        warning, not a silent write, and not a refusal either."""
        await ssc._insert_shift(self.config.db_path, self.emp, self.window[0], "09:00", "17:00", None)
        next_id = await self._drive_to_confirm(30, self.window[0], "10:00", "18:00")
        await self.dp.feed_webhook_update(self.bot, _callback_update(next_id, self.ADMIN_ID, "ssc_shift_confirm"))

        shifts = await ssc._shifts_for_day(self.config.db_path, self.window[0])
        self.assertEqual(len(shifts), 2)


if __name__ == "__main__":
    unittest.main()
