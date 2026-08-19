import unittest
import aiosqlite
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from aiogram.types import Message, CallbackQuery, User
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

from templates.course_tracker import (
    init_db, CourseTrackerConfig,
    _save_admins, _parse_due_date, _submission_status,
    cb_stu_assign, cb_a_view, _finalize_grade,
)


class TestCourseTrackerSmoke(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from aiogram import Bot
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()

        self._tmp = tempfile.TemporaryDirectory()
        self.test_data_dir = Path(self._tmp.name)

        self.bot_id_1 = 111
        self.bot_id_2 = 222

        self.config1 = CourseTrackerConfig(
            bot_name="bot1",
            db_path=str(self.test_data_dir / f"bot_{self.bot_id_1}_data.db"),
            admins_file=self.test_data_dir / f"admins_{self.bot_id_1}.json",
            welcome_image=self.test_data_dir / f"bot_{self.bot_id_1}.jpg",
        )
        self.config2 = CourseTrackerConfig(
            bot_name="bot2",
            db_path=str(self.test_data_dir / f"bot_{self.bot_id_2}_data.db"),
            admins_file=self.test_data_dir / f"admins_{self.bot_id_2}.json",
            welcome_image=self.test_data_dir / f"bot_{self.bot_id_2}.jpg",
        )

        self.admin_id = 999
        self.student_id = 555
        _save_admins(self.config1.admins_file, {str(self.admin_id)})
        _save_admins(self.config2.admins_file, {str(self.admin_id)})

        self.storage = MemoryStorage()

    async def asyncTearDown(self):
        self._bot_call_patcher.stop()
        self._tmp.cleanup()

    async def test_db_isolation_between_bots(self):
        await init_db(self.config1.db_path)
        await init_db(self.config2.db_path)

        async with aiosqlite.connect(self.config1.db_path) as db:
            await db.execute("INSERT INTO courses (name) VALUES (?)", ("Course A",))
            await db.commit()

        async with aiosqlite.connect(self.config1.db_path) as db:
            row = await (await db.execute("SELECT COUNT(*) FROM courses")).fetchone()
            self.assertEqual(row[0], 1)
        async with aiosqlite.connect(self.config2.db_path) as db:
            row = await (await db.execute("SELECT COUNT(*) FROM courses")).fetchone()
            self.assertEqual(row[0], 0)

    async def test_due_date_parsing(self):
        self.assertEqual(_parse_due_date("31.12.2026"), "2026-12-31 23:59")
        self.assertEqual(_parse_due_date("01.01.2026 09:30"), "2026-01-01 09:30")
        self.assertIsNone(_parse_due_date("not a date"))
        self.assertIsNone(_parse_due_date("32.13.2026"))

    async def test_submission_status_computed_states(self):
        past_due = "2020-01-01 00:00"
        future_due = "2999-01-01 00:00"
        self.assertEqual(_submission_status(future_due, None), "not_submitted")
        self.assertEqual(_submission_status(past_due, None), "overdue")

        submitted_row = {"status": "submitted"}
        graded_row = {"status": "graded"}
        self.assertEqual(_submission_status(past_due, submitted_row), "submitted")
        self.assertEqual(_submission_status(past_due, graded_row), "graded")

    async def _seed_course_with_assignment(self):
        await init_db(self.config1.db_path)
        async with aiosqlite.connect(self.config1.db_path) as db:
            cur = await db.execute("INSERT INTO courses (name) VALUES (?)", ("Course A",))
            course_id = cur.lastrowid
            await db.execute(
                "INSERT INTO course_students (course_id, student_id) VALUES (?,?)", (course_id, self.student_id)
            )
            cur = await db.execute(
                "INSERT INTO course_assignments (course_id, name, due_date, expected_type) VALUES (?,?,?,?)",
                (course_id, "HW1", "2999-01-01 00:00", "any"),
            )
            assignment_id = cur.lastrowid
            await db.commit()
        return course_id, assignment_id

    async def test_resubmission_upserts_and_clears_grade(self):
        _course_id, assignment_id = await self._seed_course_with_assignment()

        async def _submit(text):
            async with aiosqlite.connect(self.config1.db_path) as db:
                await db.execute(
                    "INSERT INTO course_submissions (assignment_id, student_id, submission_text, file_id, submitted_at, status, grade, admin_comment) "
                    "VALUES (?,?,?,?,datetime('now','localtime'),'submitted',NULL,NULL) "
                    "ON CONFLICT(assignment_id, student_id) DO UPDATE SET "
                    "submission_text=excluded.submission_text, file_id=excluded.file_id, "
                    "submitted_at=excluded.submitted_at, status='submitted', grade=NULL, admin_comment=NULL",
                    (assignment_id, self.student_id, text, None),
                )
                await db.commit()

        await _submit("first try")
        async with aiosqlite.connect(self.config1.db_path) as db:
            await db.execute(
                "UPDATE course_submissions SET status='graded', grade='5' WHERE assignment_id=? AND student_id=?",
                (assignment_id, self.student_id),
            )
            await db.commit()

        # Resubmit — should overwrite text and clear the prior grade.
        await _submit("second try")
        async with aiosqlite.connect(self.config1.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute(
                "SELECT * FROM course_submissions WHERE assignment_id=? AND student_id=?",
                (assignment_id, self.student_id),
            )).fetchone()
        self.assertEqual(row["submission_text"], "second try")
        self.assertEqual(row["status"], "submitted")
        self.assertIsNone(row["grade"])
        # UPSERT must not create a second row for the same (assignment, student).
        async with aiosqlite.connect(self.config1.db_path) as db:
            count = (await (await db.execute(
                "SELECT COUNT(*) FROM course_submissions WHERE assignment_id=? AND student_id=?",
                (assignment_id, self.student_id),
            )).fetchone())[0]
        self.assertEqual(count, 1)

    async def test_grading_notifies_student_and_stores_comment(self):
        _course_id, assignment_id = await self._seed_course_with_assignment()
        async with aiosqlite.connect(self.config1.db_path) as db:
            await db.execute(
                "INSERT INTO course_submissions (assignment_id, student_id, submission_text) VALUES (?,?,?)",
                (assignment_id, self.student_id, "my solution"),
            )
            await db.commit()
            submission_id = (await (await db.execute(
                "SELECT id FROM course_submissions WHERE assignment_id=? AND student_id=?",
                (assignment_id, self.student_id),
            )).fetchone())[0]

        state = FSMContext(storage=self.storage, key=MagicMock())
        await state.update_data(grade_submission_id=submission_id, grade_value="5")

        mock_bot = AsyncMock()
        message_answer = AsyncMock()
        await _finalize_grade(message_answer, state, self.config1, mock_bot, "Отлично!")

        async with aiosqlite.connect(self.config1.db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute("SELECT * FROM course_submissions WHERE id=?", (submission_id,))).fetchone()
        self.assertEqual(row["status"], "graded")
        self.assertEqual(row["grade"], "5")
        self.assertEqual(row["admin_comment"], "Отлично!")
        mock_bot.send_message.assert_called_once()
        message_answer.assert_called()

    async def test_student_sees_own_assignment_detail(self):
        _course_id, assignment_id = await self._seed_course_with_assignment()

        user = User(id=self.student_id, is_bot=False, first_name="Student")
        cb = AsyncMock(spec=CallbackQuery)
        cb.data = f"stu_assign:{assignment_id}"
        cb.answer = AsyncMock()
        cb.from_user = user
        cb.message = AsyncMock(spec=Message)
        cb.message.edit_text = AsyncMock()

        await cb_stu_assign(cb, self.config1)
        cb.message.edit_text.assert_called_once()
        text = cb.message.edit_text.call_args.args[0]
        self.assertIn("HW1", text)
        self.assertIn("Не сдано", text)

    async def test_non_admin_blocked_from_admin_assignment_view(self):
        _course_id, assignment_id = await self._seed_course_with_assignment()

        cb = AsyncMock(spec=CallbackQuery)
        cb.data = f"a_view:{assignment_id}"
        cb.answer = AsyncMock()
        cb.from_user = User(id=self.student_id, is_bot=False, first_name="Student")
        cb.message = AsyncMock(spec=Message)
        cb.message.edit_text = AsyncMock()

        await cb_a_view(cb, self.config1)
        cb.message.edit_text.assert_not_called()


if __name__ == "__main__":
    unittest.main()
