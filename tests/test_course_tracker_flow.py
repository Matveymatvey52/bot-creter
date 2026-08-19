"""templates/course_tracker.py end-to-end flow: admin bootstrap, course
creation, student self-join via course_<id> deep link, assignment creation
+ student notification, text submission, and the progress table both
displays and exports build from the same underlying data.

Handlers called directly (not through a real Dispatcher/aiogram polling),
same convention as tests/test_start_buttons.py and
tests/test_manager_bot_office_event.py.

Run with: python -m pytest tests/test_course_tracker_flow.py
"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.filters import CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from templates import course_tracker

ADMIN_ID = 111
STUDENT_ID = 222


@pytest.fixture
def db_path():
    with tempfile.TemporaryDirectory() as tmp:
        yield str(Path(tmp) / "fixture.db")


def _config(db_path: str) -> course_tracker.CourseTrackerConfig:
    return course_tracker.CourseTrackerConfig(
        bot_name="test-course-tracker",
        db_path=db_path,
        admins_file=str(Path(db_path).with_suffix(".admins.json")),
        welcome_image=Path(db_path).with_suffix(".jpg"),
        bot_id=None,  # sheets export stays a no-op — covered separately
    )


def _fsm(user_id: int) -> FSMContext:
    storage = MemoryStorage()
    key = StorageKey(bot_id=0, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=storage, key=key)


def _message(user_id: int, text: str | None = None, **kwargs) -> MagicMock:
    message = MagicMock()
    message.chat.id = user_id
    message.from_user.id = user_id
    message.from_user.username = f"user{user_id}"
    message.from_user.full_name = f"User {user_id}"
    message.text = text
    message.caption = None
    message.document = None
    message.photo = None
    message.answer = AsyncMock()
    message.answer_photo = AsyncMock()
    message.answer_document = AsyncMock()
    message.edit_text = AsyncMock()
    message.bot = MagicMock()
    message.bot.send_message = AsyncMock()
    message.bot.get_me = AsyncMock(return_value=MagicMock(username="test_course_bot"))
    for k, v in kwargs.items():
        setattr(message, k, v)
    return message


def _callback(user_id: int, data: str) -> MagicMock:
    cb = MagicMock()
    cb.from_user.id = user_id
    cb.data = data
    cb.answer = AsyncMock()
    cb.message = _message(user_id)
    cb.bot = cb.message.bot
    return cb


async def _create_course(db_path: str, admin_id: int, title: str = "Python 101") -> int:
    """Drives the admin FSM flow through cb_new_course -> on_course_title ->
    on_course_description, same sequence a real conversation would produce,
    and returns the new course_id."""
    config = _config(db_path)
    state = _fsm(admin_id)
    cb = _callback(admin_id, "ct_new_course")
    await course_tracker.cb_new_course(cb, state, config)

    msg = _message(admin_id, text=title)
    await course_tracker.on_course_title(msg, state, config)

    msg2 = _message(admin_id, text="-")
    await course_tracker.on_course_description(msg2, state, config)

    async with __import__("aiosqlite").connect(db_path) as db:
        cur = await db.execute("SELECT id FROM courses WHERE title=?", (title,))
        row = await cur.fetchone()
    return row[0]


@pytest.mark.asyncio
async def test_cmd_start_bootstraps_first_user_as_admin(db_path):
    await course_tracker.init_db(db_path)
    config = _config(db_path)
    state = _fsm(ADMIN_ID)
    message = _message(ADMIN_ID)
    command = CommandObject(prefix="/", command="start", args=None)

    await course_tracker.cmd_start(message, command, state, config)

    assert course_tracker._is_admin(ADMIN_ID, config.admins_file)
    message.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_second_user_without_deep_link_is_not_admin(db_path):
    await course_tracker.init_db(db_path)
    config = _config(db_path)
    course_tracker._save_admins(config.admins_file, {str(ADMIN_ID)})

    state = _fsm(STUDENT_ID)
    message = _message(STUDENT_ID)
    command = CommandObject(prefix="/", command="start", args=None)
    await course_tracker.cmd_start(message, command, state, config)

    assert not course_tracker._is_admin(STUDENT_ID, config.admins_file)
    message.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_full_course_flow_creates_course_assignment_and_progress(db_path):
    await course_tracker.init_db(db_path)
    config = _config(db_path)
    course_tracker._save_admins(config.admins_file, {str(ADMIN_ID)})

    course_id = await _create_course(db_path, ADMIN_ID)
    assert course_id is not None

    # Student self-joins via the course_<id> deep link.
    state = _fsm(STUDENT_ID)
    message = _message(STUDENT_ID)
    command = CommandObject(prefix="/", command="start", args=f"course_{course_id}")
    await course_tracker.cmd_start(message, command, state, config)

    conn = sqlite3.connect(db_path)
    members = conn.execute("SELECT chat_id FROM course_members WHERE course_id=?", (course_id,)).fetchall()
    conn.close()
    assert members == [(STUDENT_ID,)]

    # Admin creates an assignment with no deadline ("-").
    admin_state = _fsm(ADMIN_ID)
    cb = _callback(ADMIN_ID, f"ct_new_asg:{course_id}")
    await course_tracker.cb_new_assignment(cb, admin_state, config)
    await course_tracker.on_assignment_title(_message(ADMIN_ID, text="ДЗ 1"), admin_state, config)
    await course_tracker.on_assignment_description(_message(ADMIN_ID, text="-"), admin_state, config)
    deadline_msg = _message(ADMIN_ID, text="-")
    await course_tracker.on_assignment_deadline(deadline_msg, admin_state, config)
    deadline_msg.bot.send_message.assert_awaited_once()  # student notified of new assignment

    conn = sqlite3.connect(db_path)
    assignment_id = conn.execute(
        "SELECT id FROM course_assignments WHERE course_id=?", (course_id,)
    ).fetchone()[0]
    conn.close()

    # Before any submission: progress shows "не сдано".
    rows = await course_tracker._progress_rows(db_path, course_id)
    assert len(rows) == 1
    assert rows[0]["status"] == "не сдано"

    # Student submits a text solution.
    submit_state = _fsm(STUDENT_ID)
    await submit_state.set_state(course_tracker.SubmissionFlow.awaiting_content)
    await submit_state.update_data(assignment_id=assignment_id)
    submission_msg = _message(STUDENT_ID, text="my solution")
    await course_tracker.on_submission_content(submission_msg, submit_state, config)

    rows = await course_tracker._progress_rows(db_path, course_id)
    assert rows[0]["status"] == "сдано"


@pytest.mark.asyncio
async def test_resubmission_blocked_when_disallowed(db_path, monkeypatch):
    monkeypatch.setattr(course_tracker, "ALLOW_RESUBMISSION", False)
    await course_tracker.init_db(db_path)
    config = _config(db_path)
    course_tracker._save_admins(config.admins_file, {str(ADMIN_ID)})
    course_id = await _create_course(db_path, ADMIN_ID)

    state = _fsm(STUDENT_ID)
    await course_tracker.cmd_start(
        _message(STUDENT_ID), CommandObject(prefix="/", command="start", args=f"course_{course_id}"),
        state, config,
    )

    admin_state = _fsm(ADMIN_ID)
    await course_tracker.cb_new_assignment(_callback(ADMIN_ID, f"ct_new_asg:{course_id}"), admin_state, config)
    await course_tracker.on_assignment_title(_message(ADMIN_ID, text="ДЗ 1"), admin_state, config)
    await course_tracker.on_assignment_description(_message(ADMIN_ID, text="-"), admin_state, config)
    await course_tracker.on_assignment_deadline(_message(ADMIN_ID, text="-"), admin_state, config)

    conn = sqlite3.connect(db_path)
    assignment_id = conn.execute("SELECT id FROM course_assignments").fetchone()[0]
    conn.close()

    submit_state = _fsm(STUDENT_ID)
    await submit_state.set_state(course_tracker.SubmissionFlow.awaiting_content)
    await submit_state.update_data(assignment_id=assignment_id)
    await course_tracker.on_submission_content(_message(STUDENT_ID, text="attempt 1"), submit_state, config)

    # Second attempt: cb_submit_start must refuse before even entering the FSM.
    cb = _callback(STUDENT_ID, f"ct_submit:{assignment_id}")
    retry_state = _fsm(STUDENT_ID)
    await course_tracker.cb_submit_start(cb, retry_state, config)

    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM course_submissions").fetchone()[0]
    conn.close()
    assert count == 1
    assert await retry_state.get_state() is None


def test_parse_deadline_accepts_expected_format():
    assert course_tracker._parse_deadline("2026-09-01 10:00") == "2026-09-01T10:00:00"


def test_parse_deadline_rejects_garbage():
    assert course_tracker._parse_deadline("not a date") is None
