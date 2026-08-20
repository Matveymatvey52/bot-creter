"""templates/team_manager.py end-to-end flow: project creation with unique
code, join-by-code, task creation with category/deadline/attachment-limit
enforcement, report submission, filter/sort combinations, multi-project/
multi-role, notification firing, and miniapp_config shape validation.

Handlers called directly (not through a real Dispatcher/aiogram polling),
same convention as tests/test_course_tracker_flow.py.

Run with: python -m pytest tests/test_team_manager_flow.py
"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from templates import team_manager

OWNER_ID = 111
WORKER_ID = 222
WORKER2_ID = 333


@pytest.fixture
def db_path():
    with tempfile.TemporaryDirectory() as tmp:
        yield str(Path(tmp) / "fixture.db")


def _config(db_path: str) -> team_manager.TeamManagerConfig:
    return team_manager.TeamManagerConfig(
        bot_name="test-team-manager",
        db_path=db_path,
        admins_file=str(Path(db_path).with_suffix(".admins.json")),
        welcome_image=Path(db_path).with_suffix(".jpg"),
        bot_id=None,
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
    message.bot.get_me = AsyncMock(return_value=MagicMock(username="test_team_bot"))
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


async def _create_project(db_path: str, owner_id: int, name: str = "Проект А") -> dict:
    return await team_manager._create_project(db_path, name, owner_id)


@pytest.mark.asyncio
async def test_template_registers_router_and_miniapp_config():
    assert team_manager.router is not None
    assert isinstance(team_manager.miniapp_config, dict)


@pytest.mark.asyncio
async def test_miniapp_config_shape_is_well_formed():
    """Structural validation only — this template's miniapp_config is a flat,
    unscoped view (no role-based auth, matching what runtime/miniapp_api.py's
    generic engine can actually express)."""
    resources = team_manager.miniapp_config["resources"]
    assert isinstance(resources, list) and len(resources) >= 4
    names = {r["name"] for r in resources}
    assert {"projects", "tasks", "reports", "attachments"} <= names
    for r in resources:
        assert isinstance(r["table"], str)
        assert isinstance(r["order_by"], str)
        assert isinstance(r["creatable"], bool)
        assert isinstance(r["title"], str)
        assert isinstance(r["titleField"], str)
        for f in r["fields"]:
            assert {"name", "label", "kind", "list", "detail", "create"} <= set(f.keys())


@pytest.mark.asyncio
async def test_create_project_generates_unique_code_and_owner_membership(db_path):
    await team_manager.init_db(db_path)
    project = await _create_project(db_path, OWNER_ID)
    assert len(project["code"]) == team_manager.CODE_LENGTH
    role = await team_manager._get_role(db_path, project["id"], OWNER_ID)
    assert role == "owner"

    project2 = await _create_project(db_path, OWNER_ID, name="Проект Б")
    assert project2["code"] != project["code"]


@pytest.mark.asyncio
async def test_join_by_code_adds_worker(db_path):
    await team_manager.init_db(db_path)
    project = await _create_project(db_path, OWNER_ID)

    state = _fsm(WORKER_ID)
    await state.set_state(team_manager.JoinProjectFlow.code)
    msg = _message(WORKER_ID, text=project["code"])
    await team_manager.on_join_code(msg, state, _config(db_path))

    role = await team_manager._get_role(db_path, project["id"], WORKER_ID)
    assert role == "worker"
    msg.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_join_by_bad_code_reports_not_found(db_path):
    await team_manager.init_db(db_path)
    await _create_project(db_path, OWNER_ID)

    state = _fsm(WORKER_ID)
    await state.set_state(team_manager.JoinProjectFlow.code)
    msg = _message(WORKER_ID, text="NOTAREALCODE1234")
    await team_manager.on_join_code(msg, state, _config(db_path))

    role = await team_manager._get_role(db_path, 1, WORKER_ID)
    assert role is None


@pytest.mark.asyncio
async def test_promote_to_owner_multi_owner(db_path):
    await team_manager.init_db(db_path)
    project = await _create_project(db_path, OWNER_ID)
    await team_manager._join_project(db_path, project["code"], WORKER_ID)

    config = _config(db_path)
    cb = _callback(OWNER_ID, f"tm_promote:{project['id']}:{WORKER_ID}")
    await team_manager.cb_promote(cb, config)

    role = await team_manager._get_role(db_path, project["id"], WORKER_ID)
    assert role == "owner"
    cb.bot.send_message.assert_awaited_once()  # promoted worker notified


@pytest.mark.asyncio
async def test_create_task_enforces_attachment_limit(db_path):
    await team_manager.init_db(db_path)
    project = await _create_project(db_path, OWNER_ID)
    await team_manager._join_project(db_path, project["code"], WORKER_ID)

    too_many = [{"file_id": f"f{i}", "type": "file", "name": "x", "size": 10} for i in range(team_manager.MAX_FILES_PER_TASK + 1)]
    with pytest.raises(ValueError):
        await team_manager._create_task(
            db_path, project["id"], OWNER_ID, WORKER_ID, "Сделать X", "разработка",
            "2026-12-25T18:00:00", too_many,
        )


@pytest.mark.asyncio
async def test_create_task_with_category_and_deadline_fires_assignment_notification(db_path):
    await team_manager.init_db(db_path)
    project = await _create_project(db_path, OWNER_ID)
    await team_manager._join_project(db_path, project["code"], WORKER_ID)
    config = _config(db_path)

    state = _fsm(OWNER_ID)
    cb = _callback(OWNER_ID, f"tm_task_assignee:{project['id']}:{WORKER_ID}")
    await team_manager.cb_task_assignee(cb, state, config)
    await team_manager.on_task_text(_message(OWNER_ID, text="Сделать отчёт"), state, config)
    cat_cb = _callback(OWNER_ID, "tm_task_cat:разработка")
    # No prior categories exist, so exercise the "new category" text path instead.
    new_cb = _callback(OWNER_ID, "tm_task_cat_new")
    await team_manager.cb_task_category_new(new_cb, state)
    await team_manager.on_new_category_text(_message(OWNER_ID, text="разработка"), state)
    await team_manager.on_task_deadline(_message(OWNER_ID, text="25.12.2026 18:00"), state)

    done_cb = _callback(OWNER_ID, "tm_task_att_done")
    await team_manager.cb_task_attachments_done(done_cb, state, config)

    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT text, category, deadline, assigned_to FROM tasks").fetchone()
    conn.close()
    assert row == ("Сделать отчёт", "разработка", "2026-12-25T18:00:00", WORKER_ID)
    done_cb.bot.send_message.assert_awaited_once()  # worker notified of new task


@pytest.mark.asyncio
async def test_deadline_parsing_rejects_bad_format(db_path):
    assert team_manager._parse_deadline("25.12.2026 18:00") == "2026-12-25T18:00:00"
    assert team_manager._parse_deadline("not a date") is None


@pytest.mark.asyncio
async def test_report_submission_marks_task_done_and_notifies_owner(db_path):
    await team_manager.init_db(db_path)
    project = await _create_project(db_path, OWNER_ID)
    await team_manager._join_project(db_path, project["code"], WORKER_ID)
    task = await team_manager._create_task(
        db_path, project["id"], OWNER_ID, WORKER_ID, "Сделать X", "разработка", "2026-12-25T18:00:00",
    )
    config = _config(db_path)

    state = _fsm(WORKER_ID)
    cb = _callback(WORKER_ID, f"tm_report:{task['id']}")
    await team_manager.cb_report_start(cb, state, config)
    await team_manager.on_report_text(_message(WORKER_ID, text="Готово"), state)
    done_cb = _callback(WORKER_ID, "tm_report_att_done")
    await team_manager.cb_report_attachments_done(done_cb, state, config)

    conn = sqlite3.connect(db_path)
    status = conn.execute("SELECT status FROM tasks WHERE id=?", (task["id"],)).fetchone()[0]
    report_count = conn.execute("SELECT COUNT(*) FROM reports WHERE task_id=?", (task["id"],)).fetchone()[0]
    conn.close()
    assert status == "done"
    assert report_count == 1
    done_cb.bot.send_message.assert_awaited_once()  # owner notified of report


@pytest.mark.asyncio
async def test_report_submission_enforces_attachment_limit(db_path):
    await team_manager.init_db(db_path)
    project = await _create_project(db_path, OWNER_ID)
    await team_manager._join_project(db_path, project["code"], WORKER_ID)
    task = await team_manager._create_task(
        db_path, project["id"], OWNER_ID, WORKER_ID, "Сделать X", "разработка", "2026-12-25T18:00:00",
    )
    too_many = [{"file_id": f"f{i}", "type": "file", "name": "x", "size": 10} for i in range(team_manager.MAX_FILES_PER_TASK + 1)]
    with pytest.raises(ValueError):
        await team_manager._submit_report(db_path, task["id"], WORKER_ID, "text", too_many)


@pytest.mark.asyncio
async def test_worker_task_list_sorted_soonest_first(db_path):
    await team_manager.init_db(db_path)
    project = await _create_project(db_path, OWNER_ID)
    await team_manager._join_project(db_path, project["code"], WORKER_ID)
    await team_manager._create_task(db_path, project["id"], OWNER_ID, WORKER_ID, "Позже", "cat", "2026-12-30T10:00:00")
    await team_manager._create_task(db_path, project["id"], OWNER_ID, WORKER_ID, "Раньше", "cat", "2026-12-01T10:00:00")

    tasks = await team_manager._list_tasks(db_path, project["id"], worker_id=WORKER_ID, sort_by="deadline", sort_dir="asc")
    assert [t["text"] for t in tasks] == ["Раньше", "Позже"]


@pytest.mark.asyncio
async def test_owner_filter_and_sort_combinations(db_path):
    await team_manager.init_db(db_path)
    project = await _create_project(db_path, OWNER_ID)
    await team_manager._join_project(db_path, project["code"], WORKER_ID)
    await team_manager._join_project(db_path, project["code"], WORKER2_ID)
    await team_manager._create_task(db_path, project["id"], OWNER_ID, WORKER_ID, "T1", "design", "2026-12-01T10:00:00")
    await team_manager._create_task(db_path, project["id"], OWNER_ID, WORKER2_ID, "T2", "dev", "2026-12-05T10:00:00")

    by_worker = await team_manager._list_tasks(db_path, project["id"], worker_id=WORKER_ID)
    assert [t["text"] for t in by_worker] == ["T1"]

    by_category = await team_manager._list_tasks(db_path, project["id"], category="dev")
    assert [t["text"] for t in by_category] == ["T2"]

    desc = await team_manager._list_tasks(db_path, project["id"], sort_by="deadline", sort_dir="desc")
    assert [t["text"] for t in desc] == ["T2", "T1"]

    by_status = await team_manager._list_tasks(db_path, project["id"], status="not_taken")
    assert len(by_status) == 2


@pytest.mark.asyncio
async def test_overdue_sweep_flags_and_returns_expired_tasks(db_path):
    await team_manager.init_db(db_path)
    project = await _create_project(db_path, OWNER_ID)
    await team_manager._join_project(db_path, project["code"], WORKER_ID)
    task = await team_manager._create_task(
        db_path, project["id"], OWNER_ID, WORKER_ID, "Просрочу", "cat", "2000-01-01T00:00:00",
    )

    swept = await team_manager._sweep_overdue(db_path)
    assert [t["id"] for t in swept] == [task["id"]]

    conn = sqlite3.connect(db_path)
    status = conn.execute("SELECT status FROM tasks WHERE id=?", (task["id"],)).fetchone()[0]
    conn.close()
    assert status == "overdue"


@pytest.mark.asyncio
async def test_multi_project_multi_role_switcher(db_path):
    await team_manager.init_db(db_path)
    project_a = await _create_project(db_path, OWNER_ID, name="A")
    project_b = await _create_project(db_path, WORKER_ID, name="B")
    await team_manager._join_project(db_path, project_b["code"], OWNER_ID)

    memberships = await team_manager._list_user_projects(db_path, OWNER_ID)
    roles = {m["id"]: m["role"] for m in memberships}
    assert roles[project_a["id"]] == "owner"
    assert roles[project_b["id"]] == "worker"


@pytest.mark.asyncio
async def test_notify_task_overdue_reaches_all_owners(db_path):
    await team_manager.init_db(db_path)
    project = await _create_project(db_path, OWNER_ID)
    await team_manager._join_project(db_path, project["code"], WORKER_ID)
    await team_manager._promote_to_owner(db_path, project["id"], WORKER_ID)
    task = await team_manager._create_task(
        db_path, project["id"], OWNER_ID, WORKER_ID, "T", "cat", "2000-01-01T00:00:00",
    )
    bot = MagicMock()
    bot.send_message = AsyncMock()

    await team_manager._notify_task_overdue(bot, db_path, task)

    assert bot.send_message.await_count == 2  # both co-owners notified
