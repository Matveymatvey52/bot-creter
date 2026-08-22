# TEMPLATE: team_manager
# USE FOR: управление проектами и командами — владелец создаёт проект, получает
#          16-символьный код-приглашение, участники присоединяются по коду как
#          исполнители; любой владелец может назначить со-владельца; владельцы
#          создают задания (категория, дедлайн, вложения), исполнители сдают
#          отчёты (текст + вложения); фильтруемые/сортируемые списки заданий,
#          просроченные задания помечаются фоновой проверкой, уведомления —
#          обычными сообщениями бота
# CUSTOMIZE: sections marked with # CUSTOMIZE
from __future__ import annotations

import asyncio
import html
import logging
import os
import secrets
import string
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import aiosqlite
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

# ── CUSTOMIZE ────────────────────────────────────────────────────────────────
BOT_DESCRIPTION = "Управление проектами и командами: приглашение по коду, задания с дедлайнами, отчёты, напоминания."
WELCOME_TEXT = (
    "🗂 <b>Управление проектами</b>\n\n"
    "Создавайте проекты, приглашайте команду по коду, ставьте задания с дедлайнами "
    "и принимайте отчёты о выполнении.\n\n"
    "Выберите действие:"
)

# Любой владелец проекта может назначить со-владельцем другого участника —
# если False, единственный владелец остаётся один навсегда (роль "worker"
# для всех остальных без возможности повышения через UI).
ALLOW_MULTIOWNER = True

MAX_FILES_PER_TASK = 5      # CUSTOMIZE: вложений на задание/отчёт
MAX_FILE_SIZE_MB = 50       # CUSTOMIZE: максимальный размер одного файла

# Часы ДО дедлайна для заблаговременного напоминания — объявлена, но пока не
# используется: этот шаблон уже несёт один фоновый sweep-цикл (overdue), и
# добавление второго независимого расписания (advance-reminder) поверх него
# means tracking a separate "already reminded" flag per task without
# reusing the overdue sweep's state transition, so wiring it in is left for
# a follow-up rather than folded in here just to consume the constant.
DEFAULT_REMINDER_HOURS = 24

CODE_ALPHABET = string.ascii_uppercase + string.digits
CODE_LENGTH = 16
OVERDUE_SWEEP_INTERVAL_SECONDS = 300
# ── END CUSTOMIZE ─────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
router = Router()


# ── mini-app config (role-scoped read view) ─────────────────────────────────
# Was flat/unscoped (every viewer saw every row) until
# docs/MINIAPP_ROLE_SCOPING_DESIGN.md's role_filter addition to
# runtime/miniapp_api.py — this is the pilot template for that feature.
# `resolve` always points at project_members: it's the only table in this
# schema mapping a Telegram user id to a role. `resolve` itself returns a
# flat per-bot role set (does this viewer hold "owner" ANYWHERE), but every
# rule below re-scopes to specific projects via its `where` predicate — an
# "owner" rule is never unfiltered, it's always `project_id IN (... WHERE
# role = 'owner')` (directly, or through tasks for reports/attachments) —
# see docs/MINIAPP_ROLE_SCOPING_DESIGN.md for why the contract doesn't need
# resolve itself to be project-scoped to express this.
_PROJECT_MEMBERS_RESOLVE = {
    "table": "project_members",
    "identity_column": "user_id",
    "role_column": "role",
}

miniapp_config = {
    "resources": [
        {
            "name": "projects",
            "table": "projects",
            "order_by": "created_at DESC",
            "creatable": True,
            "scope": {"type": "global"},
            # Reads stay membership-scoped by role_filter below, but the FIRST
            # project can't be created by a member (nobody is a member of a
            # project that doesn't exist yet) — so the write is authorized by
            # factory-level bot admin instead. See runtime/miniapp_api.py's
            # create_resource_handler for the full reasoning.
            "create_requires": "bot_admin",
            # The two NOT NULL columns the client must never supply: identity
            # comes from the authenticated session, and the invite code is
            # generated rather than typed. The link row is what makes the new
            # project visible to its creator at all — without it, role_filter
            # below would scope the creator right back out of their own
            # project.
            "on_create": {
                "set": {"created_by": "telegram_user_id", "code": "random_code"},
                "link": {
                    "table": "project_members",
                    "columns": {
                        "project_id": "new_id",
                        "user_id": "telegram_user_id",
                        "role": "owner",
                    },
                },
            },
            "title": "Проекты",
            "titleField": "name",
            "children": [
                {"resource": "tasks", "via": "project_id", "title": "Задания", "as": "table"},
            ],
            "fields": [
                {"name": "name", "required": True, "label": "Название", "kind": "text", "list": True, "detail": True, "create": True},
                {"name": "code", "label": "Код приглашения", "kind": "text", "list": False, "detail": True, "create": False},
                {"name": "created_at", "label": "Создан", "kind": "date", "list": False, "detail": True, "create": False},
            ],
            # Membership, not ownership-of-all: even an "owner" role only
            # owns specific projects, never every project on the bot — so
            # both rules share the same membership predicate.
            "role_filter": {
                "resolve": _PROJECT_MEMBERS_RESOLVE,
                "rules": [
                    {
                        "role": "owner",
                        "where": "id IN (SELECT project_id FROM project_members WHERE user_id = :telegram_user_id)",
                    },
                    {
                        "role": "worker",
                        "where": "id IN (SELECT project_id FROM project_members WHERE user_id = :telegram_user_id)",
                    },
                ],
                "default_deny": True,
            },
        },
        {
            "name": "tasks",
            "table": "tasks",
            "order_by": "deadline ASC",
            "creatable": False,
            "scope": {"type": "scoped", "parent": "projects", "via": "project_id"},
            "title": "Задания",
            "titleField": "text",
            "children": [
                {"resource": "reports", "via": "task_id", "title": "Отчёты", "as": "table"},
                {"resource": "attachments", "via": "task_id", "title": "Вложения", "as": "table"},
            ],
            "fields": [
                # Declared so the projects resource can join its tasks through
                # it (runtime/miniapp_api.py's `children`/`via` contract only
                # joins on a column the child actually declares). Shown as the
                # project's name, never as a bare id.
                {"name": "project_id", "label": "Проект", "kind": "number", "list": False, "detail": True, "create": False, "ref": {"resource": "projects", "labelField": "name"}},
                {"name": "text", "label": "Задание", "kind": "text", "list": True, "detail": True, "create": False},
                {"name": "category", "label": "Категория", "kind": "text", "list": True, "detail": True, "create": False},
                {"name": "deadline", "label": "Дедлайн", "kind": "date", "list": True, "detail": True, "create": False},
                {"name": "status", "label": "Статус", "kind": "status", "list": True, "detail": True, "create": False},
            ],
            "role_filter": {
                "resolve": _PROJECT_MEMBERS_RESOLVE,
                "rules": [
                    {
                        "role": "owner",
                        "where": "project_id IN (SELECT project_id FROM project_members WHERE user_id = :telegram_user_id AND role = 'owner')",
                    },
                    {"role": "worker", "where": "assigned_to = :telegram_user_id"},
                ],
                "default_deny": True,
            },
        },
        {
            "name": "reports",
            "table": "reports",
            "order_by": "submitted_at DESC",
            "creatable": False,
            "scope": {"type": "scoped", "parent": "tasks", "via": "task_id"},
            "title": "Отчёты",
            "titleField": "text",
            "fields": [
                {"name": "task_id", "label": "Задание", "kind": "number", "list": True, "detail": True, "create": False, "ref": {"resource": "tasks", "labelField": "text"}},
                {"name": "text", "label": "Отчёт", "kind": "text", "list": False, "detail": True, "create": False},
                {"name": "submitted_at", "label": "Сдан", "kind": "date", "list": True, "detail": True, "create": False},
            ],
            # reports has no assigned_to/project_id of its own — scoped
            # through the owning task, same as attachments below.
            "role_filter": {
                "resolve": _PROJECT_MEMBERS_RESOLVE,
                "rules": [
                    {
                        "role": "owner",
                        "where": "task_id IN (SELECT id FROM tasks WHERE project_id IN (SELECT project_id FROM project_members WHERE user_id = :telegram_user_id AND role = 'owner'))",
                    },
                    {
                        "role": "worker",
                        "where": "task_id IN (SELECT id FROM tasks WHERE assigned_to = :telegram_user_id)",
                    },
                ],
                "default_deny": True,
            },
        },
        {
            "name": "attachments",
            "table": "attachments",
            "order_by": "id DESC",
            "creatable": False,
            "scope": {"type": "scoped", "parent": "tasks", "via": "task_id"},
            "title": "Вложения",
            "titleField": "name",
            "fields": [
                {"name": "task_id", "label": "Задание", "kind": "number", "list": True, "detail": True, "create": False, "ref": {"resource": "tasks", "labelField": "text"}},
                {"name": "name", "label": "Файл", "kind": "text", "list": True, "detail": True, "create": False},
                {"name": "type", "label": "Тип", "kind": "text", "list": False, "detail": True, "create": False},
            ],
            "role_filter": {
                "resolve": _PROJECT_MEMBERS_RESOLVE,
                "rules": [
                    {
                        "role": "owner",
                        "where": "task_id IN (SELECT id FROM tasks WHERE project_id IN (SELECT project_id FROM project_members WHERE user_id = :telegram_user_id AND role = 'owner'))",
                    },
                    {
                        "role": "worker",
                        "where": "task_id IN (SELECT id FROM tasks WHERE assigned_to = :telegram_user_id)",
                    },
                ],
                "default_deny": True,
            },
        },
    ],
}


# ── config ───────────────────────────────────────────────────────────────────
@dataclass
class TeamManagerConfig:
    bot_name: str
    db_path: str
    admins_file: str
    welcome_image: Path
    display_name: str | None = None
    group_chat_id: str | None = None
    bot_id: int | None = None


def _paths_for(name: str, data_dir: Path) -> TeamManagerConfig:
    return TeamManagerConfig(
        bot_name=name,
        db_path=str(data_dir / f"{name}_data.db"),
        admins_file=str(data_dir / f"admins_{name}.json"),
        welcome_image=data_dir / "bot_images" / f"{name}.jpg",
    )


def config_from_env() -> TeamManagerConfig:
    name = Path(__file__).stem
    data_dir = Path(os.getenv("DATA_DIR", "./data"))
    data_dir.mkdir(exist_ok=True)
    return _paths_for(name, data_dir)


def config_from_bot_row(bot_row: dict, data_dir: Path) -> TeamManagerConfig:
    bot_id = bot_row["bot_id"]
    config = TeamManagerConfig(
        bot_name=bot_row["name"],
        db_path=str(data_dir / f"bot_{bot_id}_data.db"),
        admins_file=str(data_dir / f"admins_{bot_id}.json"),
        welcome_image=data_dir / "bot_images" / f"bot_{bot_id}.jpg",
    )
    config.display_name = bot_row.get("display_name")
    config.group_chat_id = bot_row.get("group_chat_id")
    config.bot_id = bot_id
    return config


class ConfigMiddleware(BaseMiddleware):
    def __init__(self, config: TeamManagerConfig) -> None:
        self.config = config
        super().__init__()

    async def __call__(self, handler, event, data):
        data["config"] = self.config
        return await handler(event, data)


# ── helpers ──────────────────────────────────────────────────────────────────
def _esc(value, max_len: int = 500) -> str:
    text = str(value) if value is not None else ""
    if len(text) > max_len:
        text = text[:max_len] + "…"
    return html.escape(text)


def _generate_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def _parse_deadline(text: str) -> str | None:
    text = text.strip()
    try:
        parsed = datetime.strptime(text, "%d.%m.%Y %H:%M")
    except ValueError:
        return None
    return parsed.strftime("%Y-%m-%dT%H:%M:%S")


def _format_deadline(iso: str) -> str:
    try:
        return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%S").strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return iso


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


STATUS_LABELS = {
    "not_taken": "не взято",
    "in_progress": "в работе",
    "done": "выполнено",
    "overdue": "просрочено",
}
STATUS_MARK = {"not_taken": "⏳", "in_progress": "🔧", "done": "✅", "overdue": "🔴"}


# ── db ────────────────────────────────────────────────────────────────────────
async def init_db(db_path: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS projects (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                created_by  INTEGER NOT NULL,
                code        TEXT NOT NULL UNIQUE,
                created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS project_members (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id  INTEGER NOT NULL,
                user_id     INTEGER NOT NULL,
                role        TEXT NOT NULL CHECK (role IN ('owner', 'worker')),
                joined_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                UNIQUE(project_id, user_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id   INTEGER NOT NULL,
                created_by   INTEGER NOT NULL,
                assigned_to  INTEGER NOT NULL,
                text         TEXT NOT NULL,
                category     TEXT NOT NULL,
                deadline     TEXT NOT NULL,
                status       TEXT NOT NULL CHECK (status IN ('not_taken', 'in_progress', 'done', 'overdue')) DEFAULT 'not_taken',
                created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS attachments (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id  INTEGER NOT NULL,
                file_id  TEXT NOT NULL,
                type     TEXT NOT NULL CHECK (type IN ('photo', 'file')),
                name     TEXT,
                size     INTEGER
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id       INTEGER NOT NULL,
                submitted_by  INTEGER NOT NULL,
                text          TEXT NOT NULL,
                submitted_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS report_attachments (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                report_id  INTEGER NOT NULL,
                file_id    TEXT NOT NULL,
                type       TEXT NOT NULL CHECK (type IN ('photo', 'file')),
                name       TEXT,
                size       INTEGER
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_tasks_assigned ON tasks(assigned_to)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_members_user ON project_members(user_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_members_project ON project_members(project_id)")
        await db.commit()


async def _create_project(db_path: str, name: str, created_by: int, max_attempts: int = 10) -> dict:
    """Codes are unique within THIS bot's own db (per-bot scoping), same as
    course_tracker.py's course/admin data never crossing bot_id boundaries —
    there is no global namespace to collide against."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        for _ in range(max_attempts):
            code = _generate_code()
            cur = await db.execute("SELECT 1 FROM projects WHERE code = ?", (code,))
            if await cur.fetchone():
                continue
            cur = await db.execute(
                "INSERT INTO projects (name, created_by, code) VALUES (?, ?, ?)", (name, created_by, code),
            )
            project_id = cur.lastrowid
            await db.execute(
                "INSERT INTO project_members (project_id, user_id, role) VALUES (?, ?, 'owner')",
                (project_id, created_by),
            )
            await db.commit()
            row = await (await db.execute("SELECT * FROM projects WHERE id = ?", (project_id,))).fetchone()
            return dict(row)
    raise RuntimeError("team_manager: exhausted attempts generating a unique project code")


async def _get_project_by_code(db_path: str, code: str) -> dict | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute("SELECT * FROM projects WHERE code = ?", (code,))).fetchone()
        return dict(row) if row else None


async def _get_project(db_path: str, project_id: int) -> dict | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute("SELECT * FROM projects WHERE id = ?", (project_id,))).fetchone()
        return dict(row) if row else None


async def _get_role(db_path: str, project_id: int, user_id: int) -> str | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            "SELECT role FROM project_members WHERE project_id = ? AND user_id = ?", (project_id, user_id),
        )).fetchone()
        return row["role"] if row else None


async def _join_project(db_path: str, code: str, user_id: int) -> dict:
    project = await _get_project_by_code(db_path, code)
    if project is None:
        raise ValueError("project_not_found")
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "SELECT 1 FROM project_members WHERE project_id = ? AND user_id = ?", (project["id"], user_id),
        )
        if await cur.fetchone():
            raise ValueError("already_member")
        await db.execute(
            "INSERT INTO project_members (project_id, user_id, role) VALUES (?, ?, 'worker')",
            (project["id"], user_id),
        )
        await db.commit()
    return project


async def _list_members(db_path: str, project_id: int) -> list[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT * FROM project_members WHERE project_id = ? ORDER BY joined_at ASC", (project_id,),
        )).fetchall()
        return [dict(r) for r in rows]


async def _promote_to_owner(db_path: str, project_id: int, target_user_id: int) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE project_members SET role = 'owner' WHERE project_id = ? AND user_id = ?",
            (project_id, target_user_id),
        )
        await db.commit()


async def _list_user_projects(db_path: str, user_id: int) -> list[dict]:
    """Every (project, role) pair for a person — the basis of the
    multi-project/multi-role switcher."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            """SELECT p.id, p.name, p.code, pm.role
               FROM project_members pm JOIN projects p ON p.id = pm.project_id
               WHERE pm.user_id = ? ORDER BY pm.joined_at ASC""",
            (user_id,),
        )).fetchall()
        return [dict(r) for r in rows]


async def _list_categories(db_path: str, project_id: int) -> list[str]:
    async with aiosqlite.connect(db_path) as db:
        rows = await (await db.execute(
            "SELECT DISTINCT category FROM tasks WHERE project_id = ? ORDER BY category ASC", (project_id,),
        )).fetchall()
        return [r[0] for r in rows]


async def _create_task(
    db_path: str, project_id: int, created_by: int, assigned_to: int,
    text: str, category: str, deadline: str, attachments: list[dict] | None = None,
) -> dict:
    attachments = attachments or []
    if len(attachments) > MAX_FILES_PER_TASK:
        raise ValueError("too_many_attachments")
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            """INSERT INTO tasks (project_id, created_by, assigned_to, text, category, deadline, status)
               VALUES (?, ?, ?, ?, ?, ?, 'not_taken')""",
            (project_id, created_by, assigned_to, text, category, deadline),
        )
        task_id = cur.lastrowid
        for att in attachments:
            await db.execute(
                "INSERT INTO attachments (task_id, file_id, type, name, size) VALUES (?, ?, ?, ?, ?)",
                (task_id, att["file_id"], att["type"], att.get("name"), att.get("size")),
            )
        await db.commit()
    return await _get_task(db_path, task_id)


async def _get_task(db_path: str, task_id: int) -> dict | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))).fetchone()
        if row is None:
            return None
        task = dict(row)
        atts = await (await db.execute(
            "SELECT file_id, type, name, size FROM attachments WHERE task_id = ?", (task_id,),
        )).fetchall()
        task["attachments"] = [dict(a) for a in atts]
        return task


async def _mark_in_progress(db_path: str, task_id: int) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE tasks SET status = 'in_progress' WHERE id = ? AND status = 'not_taken'", (task_id,),
        )
        await db.commit()


async def _submit_report(db_path: str, task_id: int, submitted_by: int, text: str, attachments: list[dict] | None = None) -> dict:
    attachments = attachments or []
    if len(attachments) > MAX_FILES_PER_TASK:
        raise ValueError("too_many_attachments")
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "INSERT INTO reports (task_id, submitted_by, text) VALUES (?, ?, ?)", (task_id, submitted_by, text),
        )
        report_id = cur.lastrowid
        for att in attachments:
            await db.execute(
                "INSERT INTO report_attachments (report_id, file_id, type, name, size) VALUES (?, ?, ?, ?, ?)",
                (report_id, att["file_id"], att["type"], att.get("name"), att.get("size")),
            )
        await db.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (task_id,))
        await db.commit()
    return await _get_task(db_path, task_id)


async def _list_tasks(
    db_path: str, project_id: int, worker_id: int | None = None, category: str | None = None,
    status: str | None = None, sort_by: str = "deadline", sort_dir: str = "asc",
) -> list[dict]:
    """Combinable AND filters (worker/category/status) plus one of two
    orthogonal sort axes — ported from team-manager-bot's db/database.py
    list_tasks() query-building logic, translated onto this schema."""
    if sort_by not in ("deadline", "created_at"):
        raise ValueError("invalid sort_by")
    if sort_dir not in ("asc", "desc"):
        raise ValueError("invalid sort_dir")
    clauses = ["project_id = ?"]
    params: list = [project_id]
    if worker_id is not None:
        clauses.append("assigned_to = ?")
        params.append(worker_id)
    if category is not None:
        clauses.append("category = ?")
        params.append(category)
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    where = " AND ".join(clauses)
    order = "ASC" if sort_dir == "asc" else "DESC"
    query = f"SELECT * FROM tasks WHERE {where} ORDER BY {sort_by} {order}, id {order}"
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(query, params)).fetchall()
        return [dict(r) for r in rows]


async def _sweep_overdue(db_path: str, now: str | None = None) -> list[dict]:
    now = now or _now_iso()
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT * FROM tasks WHERE status IN ('not_taken', 'in_progress') AND deadline < ?", (now,),
        )).fetchall()
        rows = [dict(r) for r in rows]
        if rows:
            await db.executemany("UPDATE tasks SET status = 'overdue' WHERE id = ?", [(r["id"],) for r in rows])
            await db.commit()
    return rows


# ── notifications (plain bot.send_message, best-effort) ────────────────────
async def _notify_task_assigned(bot: Bot, task: dict) -> None:
    text = (
        f"📌 Новое задание: <b>{_esc(task['text'])}</b>\n"
        f"Категория: {_esc(task['category'])}\n"
        f"Дедлайн: {_format_deadline(task['deadline'])}"
    )
    try:
        await bot.send_message(task["assigned_to"], text, parse_mode="HTML")
    except Exception:
        logger.warning(f"team_manager: failed to notify assignee {task['assigned_to']} of new task {task['id']}")


async def _notify_owners(bot: Bot, db_path: str, project_id: int, text: str) -> None:
    members = await _list_members(db_path, project_id)
    for m in members:
        if m["role"] != "owner":
            continue
        try:
            await bot.send_message(m["user_id"], text, parse_mode="HTML")
        except Exception:
            logger.warning(f"team_manager: failed to notify owner {m['user_id']} in project {project_id}")


async def _notify_report_submitted(bot: Bot, db_path: str, task: dict) -> None:
    text = f"📥 По заданию «{_esc(task['text'])}» получен отчёт."
    await _notify_owners(bot, db_path, task["project_id"], text)


async def _notify_task_overdue(bot: Bot, db_path: str, task: dict) -> None:
    text = f"🔴 Задание «{_esc(task['text'])}» просрочено."
    await _notify_owners(bot, db_path, task["project_id"], text)


# ── background overdue sweep (same asyncio.create_task + while-True loop
# pattern as templates/habit_tracker.py / trip_manager.py's own reminder
# loops — started from main() below, cancelled on shutdown) ────────────────
async def _overdue_sweep_loop(bot: Bot, db_path: str) -> None:
    while True:
        try:
            overdue_tasks = await _sweep_overdue(db_path)
            for task in overdue_tasks:
                await _notify_task_overdue(bot, db_path, task)
        except Exception:
            logger.exception("team_manager: overdue sweep failed")
        await asyncio.sleep(OVERDUE_SWEEP_INTERVAL_SECONDS)


# ── FSM states ───────────────────────────────────────────────────────────────
class CreateProjectFlow(StatesGroup):
    name = State()


class JoinProjectFlow(StatesGroup):
    code = State()


class NewCategoryFlow(StatesGroup):
    awaiting_text = State()


class TaskFlow(StatesGroup):
    text = State()
    deadline = State()
    attachments = State()


class ReportFlow(StatesGroup):
    text = State()
    attachments = State()


# ── keyboards ─────────────────────────────────────────────────────────────────
def kb_main(is_any_owner: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="➕ Новый проект", callback_data="tm_new_project")],
        [InlineKeyboardButton(text="🔑 Присоединиться по коду", callback_data="tm_join")],
        [InlineKeyboardButton(text="📋 Мои задания", callback_data="tm_my_tasks")],
    ]
    if is_any_owner:
        rows.append([InlineKeyboardButton(text="🗂 Управление", callback_data="tm_manage")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_back(cb_data: str = "tm_main") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data=cb_data)]])


def kb_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="tm_main")]])


def kb_project_switcher(projects: list[dict], target: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text=f"{p['name']} ({'владелец' if p['role'] == 'owner' else 'исполнитель'})",
            callback_data=f"{target}:{p['id']}",
        )]
        for p in projects
    ]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="tm_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_manage_project(project_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Новое задание", callback_data=f"tm_new_task:{project_id}")],
        [InlineKeyboardButton(text="📋 Все задания", callback_data=f"tm_tasks:{project_id}:0:0:0:deadline:asc")],
        [InlineKeyboardButton(text="👥 Участники", callback_data=f"tm_members:{project_id}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="tm_manage")],
    ])


def kb_task_detail(task_id: int, project_id: int, is_owner: bool, is_assignee: bool, back_cb: str) -> InlineKeyboardMarkup:
    rows = []
    if is_assignee:
        rows.append([InlineKeyboardButton(text="📎 Сдать отчёт", callback_data=f"tm_report:{task_id}")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=back_cb)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_attachments_done(target: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Готово", callback_data=f"{target}_done")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="tm_main")],
    ])


# ── /start ────────────────────────────────────────────────────────────────────
@router.message(Command("start"), F.chat.type == "private")
async def cmd_start(message: Message, state: FSMContext, config: TeamManagerConfig):
    await state.clear()
    projects = await _list_user_projects(config.db_path, message.from_user.id)
    is_any_owner = any(p["role"] == "owner" for p in projects)
    if config.welcome_image.exists():
        await message.answer_photo(
            FSInputFile(str(config.welcome_image)), caption=WELCOME_TEXT,
            parse_mode="HTML", reply_markup=kb_main(is_any_owner),
        )
    else:
        await message.answer(WELCOME_TEXT, parse_mode="HTML", reply_markup=kb_main(is_any_owner))


@router.callback_query(F.data == "tm_main")
async def cb_main(cb: CallbackQuery, state: FSMContext, config: TeamManagerConfig):
    await cb.answer()
    await state.clear()
    projects = await _list_user_projects(config.db_path, cb.from_user.id)
    is_any_owner = any(p["role"] == "owner" for p in projects)
    await cb.message.edit_text(WELCOME_TEXT, parse_mode="HTML", reply_markup=kb_main(is_any_owner))


# ── project creation ─────────────────────────────────────────────────────────
@router.callback_query(F.data == "tm_new_project")
async def cb_new_project(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(CreateProjectFlow.name)
    await cb.message.edit_text("Название нового проекта?", reply_markup=kb_cancel())


@router.message(CreateProjectFlow.name, F.text)
async def on_project_name(message: Message, state: FSMContext, config: TeamManagerConfig):
    await state.clear()
    name = message.text.strip()[:200]
    if not name:
        await message.answer("Название не может быть пустым.", reply_markup=kb_cancel())
        return
    project = await _create_project(config.db_path, name, message.from_user.id)
    await message.answer(
        f"✅ Проект «{_esc(name)}» создан.\n\n"
        f"Код для приглашения участников:\n<code>{project['code']}</code>",
        parse_mode="HTML", reply_markup=kb_manage_project(project["id"]),
    )


# ── join by code ─────────────────────────────────────────────────────────────
@router.callback_query(F.data == "tm_join")
async def cb_join(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(JoinProjectFlow.code)
    await cb.message.edit_text("Введите код приглашения (16 символов):", reply_markup=kb_cancel())


@router.message(JoinProjectFlow.code, F.text)
async def on_join_code(message: Message, state: FSMContext, config: TeamManagerConfig):
    await state.clear()
    code = message.text.strip().upper()
    try:
        project = await _join_project(config.db_path, code, message.from_user.id)
    except ValueError as e:
        if str(e) == "already_member":
            await message.answer("Вы уже участник этого проекта.")
        else:
            await message.answer("⚠️ Проект с таким кодом не найден.", reply_markup=kb_cancel())
        return
    await message.answer(
        f"✅ Вы присоединились к проекту «{_esc(project['name'])}» как исполнитель.",
        parse_mode="HTML", reply_markup=kb_back(),
    )


# ── manage: project switcher (owner-only projects) ──────────────────────────
@router.callback_query(F.data == "tm_manage")
async def cb_manage(cb: CallbackQuery, config: TeamManagerConfig):
    await cb.answer()
    projects = await _list_user_projects(config.db_path, cb.from_user.id)
    owned = [p for p in projects if p["role"] == "owner"]
    if not owned:
        await cb.message.edit_text("Вы пока не владелец ни одного проекта.", reply_markup=kb_back())
        return
    if len(owned) == 1:
        await cb.message.edit_text(
            f"🗂 <b>{_esc(owned[0]['name'])}</b>", parse_mode="HTML", reply_markup=kb_manage_project(owned[0]["id"]),
        )
        return
    await cb.message.edit_text(
        "Выберите проект:", reply_markup=kb_project_switcher(owned, "tm_manage_p"),
    )


@router.callback_query(F.data.startswith("tm_manage_p:"))
async def cb_manage_pick(cb: CallbackQuery, config: TeamManagerConfig):
    await cb.answer()
    project_id = int(cb.data.split(":", 1)[1])
    project = await _get_project(config.db_path, project_id)
    if project is None or await _get_role(config.db_path, project_id, cb.from_user.id) != "owner":
        await cb.message.edit_text("⚠️ Проект не найден.", reply_markup=kb_back())
        return
    await cb.message.edit_text(
        f"🗂 <b>{_esc(project['name'])}</b>", parse_mode="HTML", reply_markup=kb_manage_project(project_id),
    )


# ── members / promotion ──────────────────────────────────────────────────────
@router.callback_query(F.data.startswith("tm_members:"))
async def cb_members(cb: CallbackQuery, config: TeamManagerConfig):
    await cb.answer()
    project_id = int(cb.data.split(":", 1)[1])
    if await _get_role(config.db_path, project_id, cb.from_user.id) != "owner":
        return
    members = await _list_members(config.db_path, project_id)
    lines = [f"👥 <b>Участники</b> ({len(members)})\n"]
    rows = []
    for m in members:
        role_label = "владелец" if m["role"] == "owner" else "исполнитель"
        lines.append(f"• <code>{m['user_id']}</code> — {role_label}")
        if ALLOW_MULTIOWNER and m["role"] == "worker":
            rows.append([InlineKeyboardButton(
                text=f"⭐ Сделать со-владельцем: {m['user_id']}", callback_data=f"tm_promote:{project_id}:{m['user_id']}",
            )])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"tm_manage_p:{project_id}")])
    await cb.message.edit_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("tm_promote:"))
async def cb_promote(cb: CallbackQuery, config: TeamManagerConfig):
    await cb.answer()
    if not ALLOW_MULTIOWNER:
        return
    _, project_id_raw, target_raw = cb.data.split(":", 2)
    project_id, target_user_id = int(project_id_raw), int(target_raw)
    if await _get_role(config.db_path, project_id, cb.from_user.id) != "owner":
        return
    if await _get_role(config.db_path, project_id, target_user_id) is None:
        await cb.message.edit_text("⚠️ Участник не найден.", reply_markup=kb_back(f"tm_members:{project_id}"))
        return
    await _promote_to_owner(config.db_path, project_id, target_user_id)
    try:
        await cb.bot.send_message(target_user_id, "⭐ Вас назначили со-владельцем проекта.")
    except Exception:
        logger.warning(f"team_manager: failed to notify {target_user_id} of promotion in project {project_id}")
    cb.data = f"tm_members:{project_id}"
    await cb_members(cb, config)


# ── task creation (owner-only) ───────────────────────────────────────────────
@router.callback_query(F.data.startswith("tm_new_task:"))
async def cb_new_task(cb: CallbackQuery, state: FSMContext, config: TeamManagerConfig):
    await cb.answer()
    project_id = int(cb.data.split(":", 1)[1])
    if await _get_role(config.db_path, project_id, cb.from_user.id) != "owner":
        return
    members = await _list_members(config.db_path, project_id)
    if not members:
        await cb.message.edit_text("В проекте пока нет участников.", reply_markup=kb_back(f"tm_manage_p:{project_id}"))
        return
    rows = [
        [InlineKeyboardButton(text=f"👤 {m['user_id']}", callback_data=f"tm_task_assignee:{project_id}:{m['user_id']}")]
        for m in members
    ]
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="tm_main")])
    await cb.message.edit_text("Кому назначить задание?", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("tm_task_assignee:"))
async def cb_task_assignee(cb: CallbackQuery, state: FSMContext, config: TeamManagerConfig):
    await cb.answer()
    _, project_id_raw, assignee_raw = cb.data.split(":", 2)
    project_id, assignee = int(project_id_raw), int(assignee_raw)
    if await _get_role(config.db_path, project_id, cb.from_user.id) != "owner":
        return
    await state.set_state(TaskFlow.text)
    await state.update_data(project_id=project_id, assigned_to=assignee)
    await cb.message.edit_text("Текст задания?", reply_markup=kb_cancel())


@router.message(TaskFlow.text, F.text)
async def on_task_text(message: Message, state: FSMContext, config: TeamManagerConfig):
    await state.update_data(text=message.text.strip()[:1000])
    data = await state.get_data()
    categories = await _list_categories(config.db_path, data["project_id"])
    rows = [[InlineKeyboardButton(text=c, callback_data=f"tm_task_cat:{c}")] for c in categories[:20]]
    rows.append([InlineKeyboardButton(text="✏️ Новая категория", callback_data="tm_task_cat_new")])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="tm_main")])
    await message.answer("Категория задания?", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(TaskFlow.text, F.data.startswith("tm_task_cat:"))
async def cb_task_category_pick(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    category = cb.data.split(":", 1)[1]
    await state.update_data(category=category)
    await state.set_state(TaskFlow.deadline)
    await cb.message.edit_text(
        "Дедлайн в формате ДД.ММ.ГГГГ ЧЧ:ММ (пример: 25.12.2026 18:00):", reply_markup=kb_cancel(),
    )


@router.callback_query(TaskFlow.text, F.data == "tm_task_cat_new")
async def cb_task_category_new(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(NewCategoryFlow.awaiting_text)
    await cb.message.edit_text("Введите название новой категории:", reply_markup=kb_cancel())


@router.message(NewCategoryFlow.awaiting_text, F.text)
async def on_new_category_text(message: Message, state: FSMContext):
    category = message.text.strip()[:100]
    if not category:
        await message.answer("Категория не может быть пустой.", reply_markup=kb_cancel())
        return
    await state.update_data(category=category)
    await state.set_state(TaskFlow.deadline)
    await message.answer(
        "Дедлайн в формате ДД.ММ.ГГГГ ЧЧ:ММ (пример: 25.12.2026 18:00):", reply_markup=kb_cancel(),
    )


@router.message(TaskFlow.deadline, F.text)
async def on_task_deadline(message: Message, state: FSMContext):
    deadline = _parse_deadline(message.text)
    if deadline is None:
        await message.answer(
            "⚠️ Не удалось разобрать дату. Формат: ДД.ММ.ГГГГ ЧЧ:ММ (пример: 25.12.2026 18:00)",
            reply_markup=kb_cancel(),
        )
        return
    await state.update_data(deadline=deadline, attachments=[])
    await state.set_state(TaskFlow.attachments)
    await message.answer(
        f"Прикрепите до {MAX_FILES_PER_TASK} файлов (фото/документ) или нажмите «Готово»:",
        reply_markup=kb_attachments_done("tm_task_att"),
    )


def _file_from_message(message: Message) -> dict | None:
    """Aiogram's own file_id is stored directly — this is a real
    simplification vs. the source project's web-upload flow, which had to
    push browser-picked bytes back through a Telegram DM just to obtain a
    file_id. Here the user is already sending straight into the bot, so the
    file_id aiogram hands us is already the one Telegram will accept again."""
    if message.document:
        size = message.document.file_size or 0
        return {"file_id": message.document.file_id, "type": "file", "name": message.document.file_name, "size": size}
    if message.photo:
        photo = message.photo[-1]
        return {"file_id": photo.file_id, "type": "photo", "name": None, "size": photo.file_size or 0}
    return None


@router.message(TaskFlow.attachments, F.document | F.photo)
async def on_task_attachment(message: Message, state: FSMContext):
    data = await state.get_data()
    attachments = data.get("attachments", [])
    if len(attachments) >= MAX_FILES_PER_TASK:
        await message.answer(f"Достигнут лимит {MAX_FILES_PER_TASK} файлов.", reply_markup=kb_attachments_done("tm_task_att"))
        return
    att = _file_from_message(message)
    if att is None:
        return
    if att["size"] and att["size"] > MAX_FILE_SIZE_MB * 1024 * 1024:
        await message.answer(f"⚠️ Файл больше {MAX_FILE_SIZE_MB} МБ, не сохранён.", reply_markup=kb_attachments_done("tm_task_att"))
        return
    attachments.append(att)
    await state.update_data(attachments=attachments)
    await message.answer(
        f"📎 Добавлено ({len(attachments)}/{MAX_FILES_PER_TASK}).", reply_markup=kb_attachments_done("tm_task_att"),
    )


@router.callback_query(TaskFlow.attachments, F.data == "tm_task_att_done")
async def cb_task_attachments_done(cb: CallbackQuery, state: FSMContext, config: TeamManagerConfig):
    await cb.answer()
    data = await state.get_data()
    await state.clear()
    task = await _create_task(
        config.db_path, data["project_id"], cb.from_user.id, data["assigned_to"],
        data["text"], data["category"], data["deadline"], data.get("attachments"),
    )
    await cb.message.edit_text(
        f"✅ Задание создано: «{_esc(task['text'])}»\nДедлайн: {_format_deadline(task['deadline'])}",
        parse_mode="HTML", reply_markup=kb_manage_project(data["project_id"]),
    )
    await _notify_task_assigned(cb.bot, task)


# ── task list: worker "my tasks" ─────────────────────────────────────────────
@router.callback_query(F.data == "tm_my_tasks")
async def cb_my_tasks(cb: CallbackQuery, config: TeamManagerConfig):
    await cb.answer()
    projects = await _list_user_projects(config.db_path, cb.from_user.id)
    if not projects:
        await cb.message.edit_text("Вы пока не в одном проекте.", reply_markup=kb_back())
        return
    if len(projects) == 1:
        await _render_my_tasks(cb, config, projects[0]["id"])
        return
    await cb.message.edit_text("Выберите проект:", reply_markup=kb_project_switcher(projects, "tm_my_tasks_p"))


@router.callback_query(F.data.startswith("tm_my_tasks_p:"))
async def cb_my_tasks_pick(cb: CallbackQuery, config: TeamManagerConfig):
    await cb.answer()
    project_id = int(cb.data.split(":", 1)[1])
    await _render_my_tasks(cb, config, project_id)


async def _render_my_tasks(cb: CallbackQuery, config: TeamManagerConfig, project_id: int) -> None:
    tasks = await _list_tasks(config.db_path, project_id, worker_id=cb.from_user.id, sort_by="deadline", sort_dir="asc")
    if not tasks:
        await cb.message.edit_text("📋 У вас пока нет заданий в этом проекте.", reply_markup=kb_back())
        return
    rows = [
        [InlineKeyboardButton(
            text=f"{STATUS_MARK.get(t['status'], '•')} {t['text'][:40]} — {_format_deadline(t['deadline'])}",
            callback_data=f"tm_task:{t['id']}:tm_my_tasks",
        )]
        for t in tasks[:40]
    ]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="tm_main")])
    await cb.message.edit_text(
        "📋 <b>Мои задания</b> (по возрастанию дедлайна)", parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


# ── task list: owner filters/sort ────────────────────────────────────────────
def _tasks_cb_data(project_id: int, worker_idx: int, category_idx: int, status_idx: int, sort_by: str, sort_dir: str) -> str:
    return f"tm_tasks:{project_id}:{worker_idx}:{category_idx}:{status_idx}:{sort_by}:{sort_dir}"


STATUS_FILTER_OPTIONS = [None, "not_taken", "in_progress", "done", "overdue"]


async def _resolve_owner_task_filters(db_path: str, project_id: int, worker_idx: int, category_idx: int, status_idx: int):
    members = await _list_members(db_path, project_id)
    categories = await _list_categories(db_path, project_id)
    worker_id = None if worker_idx == 0 else members[worker_idx - 1]["user_id"] if worker_idx - 1 < len(members) else None
    category = None if category_idx == 0 else (categories[category_idx - 1] if category_idx - 1 < len(categories) else None)
    status = STATUS_FILTER_OPTIONS[status_idx] if status_idx < len(STATUS_FILTER_OPTIONS) else None
    return members, categories, worker_id, category, status


@router.callback_query(F.data.startswith("tm_tasks:"))
async def cb_owner_tasks(cb: CallbackQuery, config: TeamManagerConfig):
    await cb.answer()
    _, project_id_raw, worker_idx_raw, category_idx_raw, status_idx_raw, sort_by, sort_dir = cb.data.split(":", 6)
    project_id, worker_idx, category_idx, status_idx = int(project_id_raw), int(worker_idx_raw), int(category_idx_raw), int(status_idx_raw)
    if await _get_role(config.db_path, project_id, cb.from_user.id) != "owner":
        return

    members, categories, worker_id, category, status = await _resolve_owner_task_filters(
        config.db_path, project_id, worker_idx, category_idx, status_idx,
    )
    tasks = await _list_tasks(
        config.db_path, project_id, worker_id=worker_id, category=category, status=status,
        sort_by=sort_by, sort_dir=sort_dir,
    )

    lines = [f"📋 <b>Задания проекта</b> ({len(tasks)})\n"]
    rows = []
    for t in tasks[:30]:
        rows.append([InlineKeyboardButton(
            text=f"{STATUS_MARK.get(t['status'], '•')} {t['text'][:35]} — {_format_deadline(t['deadline'])}",
            callback_data=f"tm_task:{t['id']}:{_tasks_cb_data(project_id, worker_idx, category_idx, status_idx, sort_by, sort_dir)}",
        )])

    worker_next = (worker_idx + 1) % (len(members) + 1)
    category_next = (category_idx + 1) % (len(categories) + 1)
    status_next = (status_idx + 1) % len(STATUS_FILTER_OPTIONS)
    worker_label = "Все" if worker_idx == 0 else str(members[worker_idx - 1]["user_id"])
    category_label = "Все" if category_idx == 0 else categories[category_idx - 1]
    status_label = "Все" if status is None else STATUS_LABELS.get(status, status)
    sort_by_next = "created_at" if sort_by == "deadline" else "deadline"
    sort_dir_next = "desc" if sort_dir == "asc" else "asc"
    sort_by_label = "дедлайн" if sort_by == "deadline" else "дата создания"
    sort_dir_label = "↑" if sort_dir == "asc" else "↓"

    rows.append([InlineKeyboardButton(
        text=f"👤 Исполнитель: {worker_label}",
        callback_data=_tasks_cb_data(project_id, worker_next, category_idx, status_idx, sort_by, sort_dir),
    )])
    rows.append([InlineKeyboardButton(
        text=f"🏷 Категория: {category_label}",
        callback_data=_tasks_cb_data(project_id, worker_idx, category_next, status_idx, sort_by, sort_dir),
    )])
    rows.append([InlineKeyboardButton(
        text=f"📌 Статус: {status_label}",
        callback_data=_tasks_cb_data(project_id, worker_idx, category_idx, status_next, sort_by, sort_dir),
    )])
    rows.append([InlineKeyboardButton(
        text=f"↕️ Сортировка: {sort_by_label} {sort_dir_label}",
        callback_data=_tasks_cb_data(project_id, worker_idx, category_idx, status_idx, sort_by_next, sort_dir),
    )])
    rows.append([InlineKeyboardButton(
        text=f"🔀 Направление: {sort_dir_label}",
        callback_data=_tasks_cb_data(project_id, worker_idx, category_idx, status_idx, sort_by, sort_dir_next),
    )])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"tm_manage_p:{project_id}")])

    if not tasks:
        lines.append("— пусто —")
    await cb.message.edit_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


# ── task detail / report submission ──────────────────────────────────────────
@router.callback_query(F.data.startswith("tm_task:"))
async def cb_task_detail(cb: CallbackQuery, config: TeamManagerConfig):
    await cb.answer()
    parts = cb.data.split(":", 2)
    task_id = int(parts[1])
    back_cb = parts[2] if len(parts) > 2 else "tm_main"
    task = await _get_task(config.db_path, task_id)
    if task is None:
        await cb.message.edit_text("⚠️ Задание не найдено.", reply_markup=kb_back())
        return
    role = await _get_role(config.db_path, task["project_id"], cb.from_user.id)
    if role is None:
        return
    is_owner = role == "owner"
    is_assignee = task["assigned_to"] == cb.from_user.id

    if task["status"] == "not_taken" and is_assignee:
        await _mark_in_progress(config.db_path, task_id)
        task["status"] = "in_progress"

    lines = [
        f"📌 <b>{_esc(task['text'])}</b>",
        f"Категория: {_esc(task['category'])}",
        f"Дедлайн: {_format_deadline(task['deadline'])}",
        f"Статус: {STATUS_LABELS.get(task['status'], task['status'])}",
    ]
    if task["attachments"]:
        lines.append(f"Вложений: {len(task['attachments'])}")
    await cb.message.edit_text(
        "\n".join(lines), parse_mode="HTML",
        reply_markup=kb_task_detail(task_id, task["project_id"], is_owner, is_assignee, back_cb),
    )
    for att in task["attachments"]:
        try:
            if att["type"] == "photo":
                await cb.message.answer_photo(att["file_id"], caption=att.get("name"))
            else:
                await cb.message.answer_document(att["file_id"], caption=att.get("name"))
        except Exception:
            logger.warning(f"team_manager: failed to resend attachment for task {task_id}")


@router.callback_query(F.data.startswith("tm_report:"))
async def cb_report_start(cb: CallbackQuery, state: FSMContext, config: TeamManagerConfig):
    await cb.answer()
    task_id = int(cb.data.split(":", 1)[1])
    task = await _get_task(config.db_path, task_id)
    if task is None or task["assigned_to"] != cb.from_user.id:
        return
    await state.set_state(ReportFlow.text)
    await state.update_data(task_id=task_id)
    await cb.message.edit_text("Текст отчёта?", reply_markup=kb_cancel())


@router.message(ReportFlow.text, F.text)
async def on_report_text(message: Message, state: FSMContext):
    await state.update_data(text=message.text.strip()[:2000], attachments=[])
    await state.set_state(ReportFlow.attachments)
    await message.answer(
        f"Прикрепите до {MAX_FILES_PER_TASK} файлов (фото/документ) или нажмите «Готово»:",
        reply_markup=kb_attachments_done("tm_report_att"),
    )


@router.message(ReportFlow.attachments, F.document | F.photo)
async def on_report_attachment(message: Message, state: FSMContext):
    data = await state.get_data()
    attachments = data.get("attachments", [])
    if len(attachments) >= MAX_FILES_PER_TASK:
        await message.answer(f"Достигнут лимит {MAX_FILES_PER_TASK} файлов.", reply_markup=kb_attachments_done("tm_report_att"))
        return
    att = _file_from_message(message)
    if att is None:
        return
    if att["size"] and att["size"] > MAX_FILE_SIZE_MB * 1024 * 1024:
        await message.answer(f"⚠️ Файл больше {MAX_FILE_SIZE_MB} МБ, не сохранён.", reply_markup=kb_attachments_done("tm_report_att"))
        return
    attachments.append(att)
    await state.update_data(attachments=attachments)
    await message.answer(
        f"📎 Добавлено ({len(attachments)}/{MAX_FILES_PER_TASK}).", reply_markup=kb_attachments_done("tm_report_att"),
    )


@router.callback_query(ReportFlow.attachments, F.data == "tm_report_att_done")
async def cb_report_attachments_done(cb: CallbackQuery, state: FSMContext, config: TeamManagerConfig):
    await cb.answer()
    data = await state.get_data()
    await state.clear()
    task = await _submit_report(config.db_path, data["task_id"], cb.from_user.id, data["text"], data.get("attachments"))
    await cb.message.edit_text("✅ Отчёт отправлен.", reply_markup=kb_back())
    await _notify_report_submitted(cb.bot, config.db_path, task)


# ── MAIN ──────────────────────────────────────────────────────────────────────
async def main():
    config = config_from_env()
    bot = Bot(token=os.getenv("BOT_TOKEN"))
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(ConfigMiddleware(config))
    dp.include_router(router)
    await bot.set_my_description(BOT_DESCRIPTION)
    await init_db(config.db_path)
    sweep_task = asyncio.create_task(_overdue_sweep_loop(bot, config.db_path))
    try:
        await dp.start_polling(bot)
    finally:
        sweep_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
