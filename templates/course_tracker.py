# TEMPLATE: course_tracker
# USE FOR: трекер онлайн-курсов/тренингов в Telegram — админ создаёт курс и задания с
#          дедлайнами, студенты присоединяются по инвайт-ссылке и присылают решения
#          (текст/файл) прямо в бота, админ видит сводную таблицу "студент × задание →
#          статус" (не сдано/сдано/просрочено), напоминания о дедлайнах — обычными
#          сообщениями в боте, экспорт прогресса в CSV/Google Sheets
# CUSTOMIZE: sections marked with # CUSTOMIZE
from __future__ import annotations

import asyncio
import csv
import io
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from db.database import get_bot_sheets_config
from features.sheets import write_row

# ── CUSTOMIZE ────────────────────────────────────────────────────────────────
BOT_DESCRIPTION = "Трекер домашних заданий онлайн-курса: дедлайны, приём решений, сводка кто сдал/не сдал."
WELCOME_TEXT = (
    "🎓 <b>Трекер курса</b>\n\n"
    "Первый, кто напишет мне — становится администратором курсов.\n\n"
    "Выберите действие:"
)

# Q1 (спросить при /create: "Переправка заданий — несколько раз или один раз?"):
# True = студент может пересдавать (каждая новая сдача заменяет предыдущую в сводке
# как последняя попытка); False = только первая сдача принимается, повторные
# попытки отклоняются с пояснением.
ALLOW_RESUBMISSION = True

# Q2 (спросить при /create: "Редактирование дедлайна — разрешить после создания?"):
ALLOW_DEADLINE_EDIT = True

# Q3 (спросить при /create: "Оценка/комментарий админа — нужна или просто статус?"):
ENABLE_GRADING = False

# Q4 (спросить при /create: "Уведомления по расписанию — как часто напоминать?"):
# Часы СМЕЩЕНИЯ от дедлайна (отрицательное = ДО дедлайна) для студентов, ещё не
# сдавших задание — см. features/reminders.py's offset_hours semantics
# (target = deadline + offset_hours). [-24, -2] = напомнить за сутки и за 2 часа.
STUDENT_REMINDER_OFFSETS_HOURS = [-24, -2]
# Отдельное напоминание админу о том, что дедлайн прошёл (часы ПОСЛЕ дедлайна).
ADMIN_OVERDUE_OFFSET_HOURS = 1
# ── END CUSTOMIZE ─────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
router = Router()


# ── mini-app config (read-only progress views, see docs/MINIAPP_DESIGN.md) ──
miniapp_config = {
    "resources": [
        {
            "name": "courses",
            "table": "courses",
            "order_by": "created_at DESC",
            "creatable": False,
            "title": "Курсы",
            "titleField": "title",
            "fields": [
                {"name": "title", "label": "Название", "kind": "text", "list": True, "detail": True, "create": False},
                {"name": "description", "label": "Описание", "kind": "text", "list": False, "detail": True, "create": False},
            ],
        },
        {
            "name": "course_assignments",
            "table": "course_assignments",
            "order_by": "created_at DESC",
            "creatable": False,
            "title": "Задания",
            "titleField": "title",
            "fields": [
                {"name": "title", "label": "Название", "kind": "text", "list": True, "detail": True, "create": False},
                {"name": "description", "label": "Описание", "kind": "text", "list": False, "detail": True, "create": False},
                {"name": "deadline", "label": "Срок", "kind": "date", "list": True, "detail": True, "create": False},
            ],
        },
        {
            "name": "course_submissions",
            "table": "course_submissions",
            "order_by": "submitted_at DESC",
            "creatable": False,
            "title": "Сдачи",
            "titleField": "status",
            "fields": [
                {"name": "assignment_id", "label": "Задание", "kind": "text", "list": True, "detail": True, "create": False},
                {"name": "student_chat_id", "label": "Студент", "kind": "text", "list": True, "detail": True, "create": False},
                {"name": "status", "label": "Статус", "kind": "status", "list": True, "detail": True, "create": False},
            ],
        },
    ],
}


# ── reminders (features/reminders.py) ───────────────────────────────────────
# Student rule: recipient_query joins the due assignment (bound as the
# query's one `?`, per features/reminders.py's docstring) to every course
# member who has NOT yet submitted for it — reused for every offset in
# STUDENT_REMINDER_OFFSETS_HOURS (a "day before" and "2h before" ping are the
# SAME rule fired at two different offsets, not two separate rules).
_STUDENT_PENDING_QUERY = """
    SELECT cm.chat_id AS chat_id
    FROM course_assignments ca
    JOIN course_members cm ON cm.course_id = ca.course_id
    LEFT JOIN course_submissions cs
        ON cs.assignment_id = ca.id AND cs.student_chat_id = cm.chat_id
    WHERE ca.id = ? AND cs.id IS NULL
"""

reminders_config = {
    "rules": [
        {
            "id": "course_tracker_student_deadline",
            "table": "course_assignments",
            "date_field": "deadline",
            "date_format": "%Y-%m-%dT%H:%M:%S",
            "recipient_query": _STUDENT_PENDING_QUERY,
            "offsets_hours": STUDENT_REMINDER_OFFSETS_HOURS,
            "message_template": "⏰ Не забудьте сдать задание «{title}» — срок: {deadline:%d.%m %H:%M}",
        },
        {
            "id": "course_tracker_admin_overdue",
            "table": "course_assignments",
            "date_field": "deadline",
            "date_format": "%Y-%m-%dT%H:%M:%S",
            "recipient_field": "created_by_chat_id",
            "offsets_hours": [ADMIN_OVERDUE_OFFSET_HOURS],
            "message_template": "⏰ Срок задания «{title}» истёк — проверьте сводку /progress",
        },
    ],
}


# ── config ───────────────────────────────────────────────────────────────────
@dataclass
class CourseTrackerConfig:
    bot_name: str
    db_path: str
    admins_file: str
    welcome_image: Path
    display_name: str | None = None
    group_chat_id: str | None = None
    bot_id: int | None = None


def _paths_for(name: str, data_dir: Path) -> CourseTrackerConfig:
    return CourseTrackerConfig(
        bot_name=name,
        db_path=str(data_dir / f"{name}_data.db"),
        admins_file=str(data_dir / f"admins_{name}.json"),
        welcome_image=data_dir / "bot_images" / f"{name}.jpg",
    )


def config_from_env() -> CourseTrackerConfig:
    name = Path(__file__).stem
    data_dir = Path(os.getenv("DATA_DIR", "./data"))
    data_dir.mkdir(exist_ok=True)
    return _paths_for(name, data_dir)


def config_from_bot_row(bot_row: dict, data_dir: Path) -> CourseTrackerConfig:
    bot_id = bot_row["bot_id"]
    config = CourseTrackerConfig(
        bot_name=bot_row["name"],
        db_path=str(data_dir / f"bot_{bot_id}_data.db"),
        admins_file=str(data_dir / f"admins_{bot_id}.json"),
        welcome_image=data_dir / "bot_images" / f"bot_{bot_id}.jpg",
    )
    config.display_name = bot_row.get("display_name")
    config.group_chat_id = bot_row.get("group_chat_id")
    config.bot_id = bot_id
    return config


class ConfigMiddleware:
    def __init__(self, config: CourseTrackerConfig) -> None:
        self.config = config

    async def __call__(self, handler, event, data):
        data["config"] = self.config
        return await handler(event, data)


# ── admin helpers (first /start with no course_ deep-link becomes admin,
# same self-registration convention as templates/tour_operator.py etc) ──────
def _load_admins(admins_file: str) -> set:
    import json
    try:
        with open(admins_file) as f:
            data = json.load(f)
        return set(data.get("ids", []) if isinstance(data, dict) else data)
    except Exception:
        return set()


def _save_admins(admins_file: str, ids: set) -> None:
    import json
    with open(admins_file, "w") as f:
        json.dump({"ids": list(ids)}, f)


def _is_admin(user_id: int, admins_file: str) -> bool:
    return str(user_id) in _load_admins(admins_file)


# ── db ────────────────────────────────────────────────────────────────────────
async def init_db(db_path: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS courses (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                title          TEXT NOT NULL,
                description    TEXT,
                admin_chat_id  INTEGER NOT NULL,
                created_at     TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS course_members (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                course_id   INTEGER NOT NULL,
                chat_id     INTEGER NOT NULL,
                username    TEXT,
                full_name   TEXT,
                joined_at   TEXT DEFAULT (datetime('now','localtime')),
                UNIQUE(course_id, chat_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS course_assignments (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                course_id           INTEGER NOT NULL,
                title               TEXT NOT NULL,
                description         TEXT,
                deadline            TEXT,
                created_by_chat_id  INTEGER NOT NULL,
                created_at          TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS course_submissions (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                assignment_id    INTEGER NOT NULL,
                student_chat_id  INTEGER NOT NULL,
                content_type     TEXT NOT NULL,
                content_text     TEXT,
                file_id          TEXT,
                status           TEXT NOT NULL DEFAULT 'submitted',
                grade            TEXT,
                submitted_at     TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.commit()
    from features.reminders import init_reminders_tables
    await init_reminders_tables(db_path)


# ── deadline parsing ("ГГГГ-ММ-ДД ЧЧ:ММ") ───────────────────────────────────
def _parse_deadline(text: str) -> str | None:
    text = text.strip()
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    return parsed.strftime("%Y-%m-%dT%H:%M:%S")


def _format_deadline(iso: str | None) -> str:
    if not iso:
        return "без срока"
    try:
        return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%S").strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return iso


# ── FSM states ────────────────────────────────────────────────────────────────
class CreateCourseFlow(StatesGroup):
    title = State()
    description = State()


class AssignmentFlow(StatesGroup):
    title = State()
    description = State()
    deadline = State()


class SubmissionFlow(StatesGroup):
    awaiting_content = State()


class EditDeadlineFlow(StatesGroup):
    awaiting_new_deadline = State()


class GradeFlow(StatesGroup):
    awaiting_grade = State()


# ── keyboards ─────────────────────────────────────────────────────────────────
def kb_admin_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📚 Мои курсы", callback_data="ct_courses")],
        [InlineKeyboardButton(text="➕ Новый курс", callback_data="ct_new_course")],
    ])


def kb_back(cb_data: str = "ct_main") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data=cb_data)]])


def kb_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="ct_main")]])


def kb_course_detail(course_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="➕ Задание", callback_data=f"ct_new_asg:{course_id}")],
        [InlineKeyboardButton(text="👥 Студенты", callback_data=f"ct_members:{course_id}")],
        [InlineKeyboardButton(text="📊 Прогресс", callback_data=f"ct_progress:{course_id}")],
        [InlineKeyboardButton(text="📤 Экспорт CSV", callback_data=f"ct_export:{course_id}")],
        [InlineKeyboardButton(text="◀️ К курсам", callback_data="ct_courses")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_student_assignment(assignment_id: int, can_submit: bool) -> InlineKeyboardMarkup:
    rows = []
    if can_submit:
        rows.append([InlineKeyboardButton(text="📎 Сдать задание", callback_data=f"ct_submit:{assignment_id}")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="ct_my_assignments")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── /start (admin bootstrap, or student self-join via course_<id> deep link) ─
@router.message(Command("start"), F.chat.type == "private")
async def cmd_start(message: Message, command: CommandObject, state: FSMContext, config: CourseTrackerConfig):
    await state.clear()
    args = (command.args or "").strip()

    if args.startswith("course_"):
        raw_id = args[len("course_"):]
        if raw_id.isdigit():
            await _join_course(message, config, int(raw_id))
            return

    admins = _load_admins(config.admins_file)
    if not admins:
        _save_admins(config.admins_file, {str(message.from_user.id)})
    elif str(message.from_user.id) not in admins:
        # Not the admin and no valid course_<id> deep link — nothing to do
        # for this user (a student can only exist inside a course they were
        # invited to, see _join_course below).
        await message.answer(
            "👋 Чтобы присоединиться к курсу, перейдите по ссылке-приглашению от преподавателя."
        )
        return

    if config.welcome_image.exists():
        await message.answer_photo(
            FSInputFile(str(config.welcome_image)), caption=WELCOME_TEXT,
            parse_mode="HTML", reply_markup=kb_admin_main(),
        )
    else:
        await message.answer(WELCOME_TEXT, parse_mode="HTML", reply_markup=kb_admin_main())


async def _join_course(message: Message, config: CourseTrackerConfig, course_id: int) -> None:
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        course = await (await db.execute("SELECT * FROM courses WHERE id=?", (course_id,))).fetchone()
        if course is None:
            await message.answer("⚠️ Курс не найден — ссылка недействительна.")
            return
        await db.execute(
            "INSERT OR IGNORE INTO course_members (course_id, chat_id, username, full_name) "
            "VALUES (?, ?, ?, ?)",
            (course_id, message.chat.id, message.from_user.username, message.from_user.full_name),
        )
        await db.commit()
    await message.answer(
        f"🎓 Вы присоединились к курсу «{course['title']}».\n\n"
        f"Задания появятся здесь по мере публикации.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Мои задания", callback_data="ct_my_assignments")],
        ]),
    )


# ── admin: course list / create ─────────────────────────────────────────────
@router.callback_query(F.data == "ct_main")
async def cb_main(cb: CallbackQuery, state: FSMContext, config: CourseTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config.admins_file):
        return
    await state.clear()
    await cb.message.edit_text(WELCOME_TEXT, parse_mode="HTML", reply_markup=kb_admin_main())


MAX_LIST_ROWS = 40


@router.callback_query(F.data == "ct_courses")
async def cb_courses(cb: CallbackQuery, config: CourseTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config.admins_file):
        return
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT * FROM courses ORDER BY created_at DESC LIMIT ?", (MAX_LIST_ROWS,)
        )).fetchall()
    if not rows:
        await cb.message.edit_text(
            "📚 <b>Курсы</b>\n\n— пока пусто —", parse_mode="HTML", reply_markup=kb_back(),
        )
        return
    buttons = [[InlineKeyboardButton(text=r["title"], callback_data=f"ct_course:{r['id']}")] for r in rows]
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="ct_main")])
    await cb.message.edit_text(
        "📚 <b>Курсы</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(F.data == "ct_new_course")
async def cb_new_course(cb: CallbackQuery, state: FSMContext, config: CourseTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config.admins_file):
        return
    await state.set_state(CreateCourseFlow.title)
    await cb.message.edit_text("Название курса?", reply_markup=kb_cancel())


@router.message(CreateCourseFlow.title, F.text)
async def on_course_title(message: Message, state: FSMContext, config: CourseTrackerConfig):
    if not _is_admin(message.from_user.id, config.admins_file):
        return
    await state.update_data(title=message.text.strip())
    await state.set_state(CreateCourseFlow.description)
    await message.answer("Краткое описание курса (или «-» чтобы пропустить):", reply_markup=kb_cancel())


@router.message(CreateCourseFlow.description, F.text)
async def on_course_description(message: Message, state: FSMContext, config: CourseTrackerConfig):
    if not _is_admin(message.from_user.id, config.admins_file):
        return
    data = await state.get_data()
    description = None if message.text.strip() == "-" else message.text.strip()
    await state.clear()
    async with aiosqlite.connect(config.db_path) as db:
        cur = await db.execute(
            "INSERT INTO courses (title, description, admin_chat_id) VALUES (?, ?, ?)",
            (data["title"], description, message.chat.id),
        )
        await db.commit()
        course_id = cur.lastrowid
    invite_link = await _invite_link(config, message.bot, course_id)
    await message.answer(
        f"✅ Курс «{data['title']}» создан.\n\n"
        f"Пригласительная ссылка для студентов:\n<code>{invite_link}</code>",
        parse_mode="HTML", reply_markup=kb_course_detail(course_id),
    )


async def _invite_link(config: CourseTrackerConfig, bot: Bot, course_id: int) -> str:
    me = await bot.get_me()
    return f"https://t.me/{me.username}?start=course_{course_id}"


# ── admin: course detail / assignments ──────────────────────────────────────
@router.callback_query(F.data.startswith("ct_course:"))
async def cb_course_detail(cb: CallbackQuery, config: CourseTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config.admins_file):
        return
    course_id = int(cb.data.split(":", 1)[1])
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        course = await (await db.execute("SELECT * FROM courses WHERE id=?", (course_id,))).fetchone()
    if course is None:
        await cb.message.edit_text("⚠️ Курс не найден.", reply_markup=kb_back("ct_courses"))
        return
    invite_link = await _invite_link(config, cb.bot, course_id)
    text = f"📚 <b>{course['title']}</b>\n{course['description'] or ''}\n\n🔗 {invite_link}"
    await cb.message.edit_text(text.strip(), parse_mode="HTML", reply_markup=kb_course_detail(course_id))


@router.callback_query(F.data.startswith("ct_new_asg:"))
async def cb_new_assignment(cb: CallbackQuery, state: FSMContext, config: CourseTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config.admins_file):
        return
    course_id = int(cb.data.split(":", 1)[1])
    await state.set_state(AssignmentFlow.title)
    await state.update_data(course_id=course_id)
    await cb.message.edit_text("Название задания?", reply_markup=kb_cancel())


@router.message(AssignmentFlow.title, F.text)
async def on_assignment_title(message: Message, state: FSMContext, config: CourseTrackerConfig):
    if not _is_admin(message.from_user.id, config.admins_file):
        return
    await state.update_data(title=message.text.strip())
    await state.set_state(AssignmentFlow.description)
    await message.answer("Описание задания (или «-» чтобы пропустить):", reply_markup=kb_cancel())


@router.message(AssignmentFlow.description, F.text)
async def on_assignment_description(message: Message, state: FSMContext, config: CourseTrackerConfig):
    if not _is_admin(message.from_user.id, config.admins_file):
        return
    description = None if message.text.strip() == "-" else message.text.strip()
    await state.update_data(description=description)
    await state.set_state(AssignmentFlow.deadline)
    await message.answer(
        "Дедлайн в формате ГГГГ-ММ-ДД ЧЧ:ММ (или «-» без срока):", reply_markup=kb_cancel(),
    )


@router.message(AssignmentFlow.deadline, F.text)
async def on_assignment_deadline(message: Message, state: FSMContext, config: CourseTrackerConfig):
    if not _is_admin(message.from_user.id, config.admins_file):
        return
    raw = message.text.strip()
    deadline = None
    if raw != "-":
        deadline = _parse_deadline(raw)
        if deadline is None:
            await message.answer("⚠️ Не удалось разобрать дату. Формат: ГГГГ-ММ-ДД ЧЧ:ММ")
            return
    data = await state.get_data()
    await state.clear()
    async with aiosqlite.connect(config.db_path) as db:
        cur = await db.execute(
            "INSERT INTO course_assignments (course_id, title, description, deadline, created_by_chat_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (data["course_id"], data["title"], data.get("description"), deadline, message.chat.id),
        )
        await db.commit()
        assignment_id = cur.lastrowid
        students = await (await db.execute(
            "SELECT chat_id FROM course_members WHERE course_id=?", (data["course_id"],)
        )).fetchall()
    await message.answer(
        f"✅ Задание «{data['title']}» добавлено (срок: {_format_deadline(deadline)}).",
        reply_markup=kb_course_detail(data["course_id"]),
    )
    await _notify_students_new_assignment(message.bot, students, data["title"], deadline)


async def _notify_students_new_assignment(bot: Bot, students, title: str, deadline: str | None) -> None:
    """Best-effort plain-message notification (per Q4: обычные сообщения,
    не push) — one blocked/deleted student must never abort the rest, same
    isolation contract features/notifications.py's own broadcast loop uses."""
    text = f"📌 Новое задание: <b>{title}</b>\nСрок: {_format_deadline(deadline)}"
    for (chat_id,) in students:
        try:
            await bot.send_message(chat_id, text, parse_mode="HTML")
        except Exception:
            logger.warning(f"course_tracker: failed to notify student chat_id={chat_id} of new assignment")


@router.callback_query(F.data.startswith("ct_members:"))
async def cb_members(cb: CallbackQuery, config: CourseTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config.admins_file):
        return
    course_id = int(cb.data.split(":", 1)[1])
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT * FROM course_members WHERE course_id=? ORDER BY joined_at", (course_id,)
        )).fetchall()
    invite_link = await _invite_link(config, cb.bot, course_id)
    lines = [f"👥 <b>Студенты</b> ({len(rows)})\n", f"🔗 Приглашение: {invite_link}\n"]
    for r in rows:
        name = r["full_name"] or r["username"] or str(r["chat_id"])
        lines.append(f"• {name}")
    await cb.message.edit_text(
        "\n".join(lines) if rows else f"👥 Пока нет студентов.\n\n🔗 Приглашение: {invite_link}",
        parse_mode="HTML", reply_markup=kb_back(f"ct_course:{course_id}"),
    )


# ── progress table ────────────────────────────────────────────────────────────
async def _progress_rows(db_path: str, course_id: int) -> list[dict]:
    """One row per (student, assignment) pair for this course — status is
    'сдано' (submitted before deadline), 'просрочено' (submitted late, OR no
    deadline has already passed with no submission), or 'не сдано' (deadline
    still open, no submission yet)."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        members = await (await db.execute(
            "SELECT * FROM course_members WHERE course_id=? ORDER BY joined_at", (course_id,)
        )).fetchall()
        assignments = await (await db.execute(
            "SELECT * FROM course_assignments WHERE course_id=? ORDER BY created_at", (course_id,)
        )).fetchall()
        submissions = await (await db.execute(
            """
            SELECT cs.* FROM course_submissions cs
            JOIN course_assignments ca ON ca.id = cs.assignment_id
            WHERE ca.course_id = ?
            """,
            (course_id,),
        )).fetchall()

    by_pair = {(s["assignment_id"], s["student_chat_id"]): s for s in submissions}
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%S")
    out = []
    for member in members:
        for assignment in assignments:
            submission = by_pair.get((assignment["id"], member["chat_id"]))
            if submission is not None:
                status = "просрочено" if submission["status"] == "late" else "сдано"
            elif assignment["deadline"] and assignment["deadline"] < now_iso:
                status = "просрочено"
            else:
                status = "не сдано"
            out.append({
                "student": member["full_name"] or member["username"] or str(member["chat_id"]),
                "assignment": assignment["title"],
                "status": status,
                "grade": submission["grade"] if submission else None,
            })
    return out


@router.callback_query(F.data.startswith("ct_progress:"))
async def cb_progress(cb: CallbackQuery, config: CourseTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config.admins_file):
        return
    course_id = int(cb.data.split(":", 1)[1])
    rows = await _progress_rows(config.db_path, course_id)
    if not rows:
        await cb.message.edit_text(
            "📊 Пока нет ни студентов, ни заданий.", reply_markup=kb_back(f"ct_course:{course_id}"),
        )
        return
    lines = ["📊 <b>Прогресс</b>\n"]
    mark = {"сдано": "✅", "просрочено": "🔴", "не сдано": "⏳"}
    for r in rows[:MAX_LIST_ROWS]:
        lines.append(f"{mark.get(r['status'], '•')} {r['student']} — «{r['assignment']}»: {r['status']}")
    await cb.message.edit_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=kb_back(f"ct_course:{course_id}"),
    )


# ── CSV / Sheets export ──────────────────────────────────────────────────────
def _csv_safe(value: str) -> str:
    """Same CSV/Sheets-formula-injection mitigation as
    templates/orders_tracker.py's _sheet_safe() — an admin-typed course/
    assignment title starting with =+-@ would otherwise execute as a formula
    when the exported file is opened in Excel/Sheets."""
    if value and value[0] in ("=", "+", "-", "@"):
        return "'" + value
    return value


@router.callback_query(F.data.startswith("ct_export:"))
async def cb_export(cb: CallbackQuery, config: CourseTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config.admins_file):
        return
    course_id = int(cb.data.split(":", 1)[1])
    rows = await _progress_rows(config.db_path, course_id)
    if not rows:
        await cb.message.answer("📊 Нечего экспортировать.")
        return

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Студент", "Задание", "Статус", "Оценка/комментарий"])
    for r in rows:
        writer.writerow([_csv_safe(r["student"]), _csv_safe(r["assignment"]), r["status"], r["grade"] or ""])
    csv_bytes = buf.getvalue().encode("utf-8-sig")  # BOM so Excel auto-detects UTF-8

    await cb.message.answer_document(
        BufferedInputFile(csv_bytes, filename=f"course_{course_id}_progress.csv"),
        caption="📤 Экспорт прогресса (CSV)",
    )
    await _export_to_sheets(config.bot_id, course_id, rows)


SHEETS_WORKSHEET = "Прогресс"


async def _export_to_sheets(bot_id: int | None, course_id: int, rows: list[dict]) -> None:
    """Optional: only active once this bot_id has a connected spreadsheet
    (db.database.bot_sheets_config, set by the owner via the sheets connect
    FSM) — same opt-in convention templates/orders_tracker.py's own sheets
    integration follows. Never raises: a Sheets/network failure must not
    block the CSV export the admin already received above."""
    if bot_id is None:
        return
    try:
        if await get_bot_sheets_config(bot_id) is None:
            return
        for r in rows:
            await write_row(
                bot_id, SHEETS_WORKSHEET,
                [_csv_safe(r["student"]), _csv_safe(r["assignment"]), r["status"], r["grade"] or "",
                 time.strftime("%Y-%m-%d %H:%M:%S")],
            )
    except Exception:
        logger.error(f"course_tracker: failed to export to sheets for bot_id={bot_id} course_id={course_id}", exc_info=True)


# ── admin: edit deadline (Q2) ────────────────────────────────────────────────
if ALLOW_DEADLINE_EDIT:
    @router.callback_query(F.data.startswith("ct_edit_deadline:"))
    async def cb_edit_deadline(cb: CallbackQuery, state: FSMContext, config: CourseTrackerConfig):
        await cb.answer()
        if not _is_admin(cb.from_user.id, config.admins_file):
            return
        assignment_id = int(cb.data.split(":", 1)[1])
        await state.set_state(EditDeadlineFlow.awaiting_new_deadline)
        await state.update_data(assignment_id=assignment_id)
        await cb.message.edit_text("Новый дедлайн (ГГГГ-ММ-ДД ЧЧ:ММ, «-» чтобы убрать срок):", reply_markup=kb_cancel())

    @router.message(EditDeadlineFlow.awaiting_new_deadline, F.text)
    async def on_new_deadline(message: Message, state: FSMContext, config: CourseTrackerConfig):
        if not _is_admin(message.from_user.id, config.admins_file):
            return
        raw = message.text.strip()
        deadline = None
        if raw != "-":
            deadline = _parse_deadline(raw)
            if deadline is None:
                await message.answer("⚠️ Не удалось разобрать дату. Формат: ГГГГ-ММ-ДД ЧЧ:ММ")
                return
        data = await state.get_data()
        await state.clear()
        async with aiosqlite.connect(config.db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute(
                "UPDATE course_assignments SET deadline=? WHERE id=?", (deadline, data["assignment_id"]),
            )
            await db.commit()
            row = await (await db.execute(
                "SELECT course_id FROM course_assignments WHERE id=?", (data["assignment_id"],)
            )).fetchone()
        await message.answer(
            f"✅ Дедлайн обновлён: {_format_deadline(deadline)}",
            reply_markup=kb_course_detail(row["course_id"]) if row else kb_admin_main(),
        )


# ── student: assignments + submission ────────────────────────────────────────
async def _student_courses(db_path: str, chat_id: int) -> list[int]:
    async with aiosqlite.connect(db_path) as db:
        rows = await (await db.execute(
            "SELECT course_id FROM course_members WHERE chat_id=?", (chat_id,)
        )).fetchall()
    return [r[0] for r in rows]


@router.callback_query(F.data == "ct_my_assignments")
async def cb_my_assignments(cb: CallbackQuery, config: CourseTrackerConfig):
    await cb.answer()
    course_ids = await _student_courses(config.db_path, cb.from_user.id)
    if not course_ids:
        await cb.message.edit_text("Вы пока не в списке ни одного курса.")
        return
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        placeholders = ",".join("?" for _ in course_ids)
        assignments = await (await db.execute(
            f"SELECT * FROM course_assignments WHERE course_id IN ({placeholders}) ORDER BY created_at DESC",
            course_ids,
        )).fetchall()
        submitted_ids = {
            row[0] for row in await (await db.execute(
                "SELECT assignment_id FROM course_submissions WHERE student_chat_id=?", (cb.from_user.id,)
            )).fetchall()
        }
    if not assignments:
        await cb.message.edit_text("📋 Заданий пока нет.")
        return
    buttons = []
    for a in assignments[:MAX_LIST_ROWS]:
        mark = "✅" if a["id"] in submitted_ids else "⏳"
        buttons.append([InlineKeyboardButton(
            text=f"{mark} {a['title']}", callback_data=f"ct_asg:{a['id']}",
        )])
    await cb.message.edit_text(
        "📋 <b>Мои задания</b>", parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(F.data.startswith("ct_asg:"))
async def cb_assignment_detail(cb: CallbackQuery, config: CourseTrackerConfig):
    await cb.answer()
    assignment_id = int(cb.data.split(":", 1)[1])
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        assignment = await (await db.execute(
            "SELECT * FROM course_assignments WHERE id=?", (assignment_id,)
        )).fetchone()
        existing = await (await db.execute(
            "SELECT * FROM course_submissions WHERE assignment_id=? AND student_chat_id=?",
            (assignment_id, cb.from_user.id),
        )).fetchone()
    if assignment is None:
        await cb.message.edit_text("⚠️ Задание не найдено.")
        return
    can_submit = existing is None or ALLOW_RESUBMISSION
    text = (
        f"📌 <b>{assignment['title']}</b>\n{assignment['description'] or ''}\n\n"
        f"Срок: {_format_deadline(assignment['deadline'])}\n"
    )
    if existing:
        text += f"\nВаш статус: {'просрочено' if existing['status'] == 'late' else 'сдано'}"
        if not ALLOW_RESUBMISSION:
            text += "\n(повторная сдача не предусмотрена)"
    await cb.message.edit_text(
        text.strip(), parse_mode="HTML", reply_markup=kb_student_assignment(assignment_id, can_submit),
    )


@router.callback_query(F.data.startswith("ct_submit:"))
async def cb_submit_start(cb: CallbackQuery, state: FSMContext, config: CourseTrackerConfig):
    await cb.answer()
    assignment_id = int(cb.data.split(":", 1)[1])
    async with aiosqlite.connect(config.db_path) as db:
        existing = await (await db.execute(
            "SELECT 1 FROM course_submissions WHERE assignment_id=? AND student_chat_id=?",
            (assignment_id, cb.from_user.id),
        )).fetchone()
    if existing and not ALLOW_RESUBMISSION:
        await cb.message.edit_text("⚠️ Задание уже сдано, повторная отправка не разрешена.")
        return
    await state.set_state(SubmissionFlow.awaiting_content)
    await state.update_data(assignment_id=assignment_id)
    await cb.message.edit_text(
        "Отправьте решение — текстом или файлом (документ/фото):", reply_markup=kb_cancel(),
    )


@router.message(SubmissionFlow.awaiting_content, F.text | F.document | F.photo)
async def on_submission_content(message: Message, state: FSMContext, config: CourseTrackerConfig):
    data = await state.get_data()
    assignment_id = data["assignment_id"]

    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        assignment = await (await db.execute(
            "SELECT * FROM course_assignments WHERE id=?", (assignment_id,)
        )).fetchone()
    if assignment is None:
        await state.clear()
        await message.answer("⚠️ Задание не найдено.")
        return

    if message.document:
        content_type, content_text, file_id = "file", message.caption, message.document.file_id
    elif message.photo:
        content_type, content_text, file_id = "file", message.caption, message.photo[-1].file_id
    else:
        content_type, content_text, file_id = "text", message.text, None

    now_iso = time.strftime("%Y-%m-%dT%H:%M:%S")
    status = "late" if assignment["deadline"] and now_iso > assignment["deadline"] else "submitted"

    async with aiosqlite.connect(config.db_path) as db:
        if ALLOW_RESUBMISSION:
            await db.execute(
                "DELETE FROM course_submissions WHERE assignment_id=? AND student_chat_id=?",
                (assignment_id, message.chat.id),
            )
        await db.execute(
            "INSERT INTO course_submissions (assignment_id, student_chat_id, content_type, content_text, "
            "file_id, status) VALUES (?, ?, ?, ?, ?, ?)",
            (assignment_id, message.chat.id, content_type, content_text, file_id, status),
        )
        await db.commit()
    await state.clear()

    label = "сдано с опозданием" if status == "late" else "сдано"
    await message.answer(f"✅ Решение принято ({label}).")
    try:
        await message.bot.send_message(
            assignment["created_by_chat_id"],
            f"📥 {message.from_user.full_name or message.from_user.username or message.chat.id} "
            f"сдал(а) «{assignment['title']}» ({label})",
        )
    except Exception:
        logger.warning(f"course_tracker: failed to notify admin of submission for assignment_id={assignment_id}")


# ── admin: grading (Q3) ──────────────────────────────────────────────────────
if ENABLE_GRADING:
    @router.callback_query(F.data.startswith("ct_grade:"))
    async def cb_grade_start(cb: CallbackQuery, state: FSMContext, config: CourseTrackerConfig):
        await cb.answer()
        if not _is_admin(cb.from_user.id, config.admins_file):
            return
        submission_id = int(cb.data.split(":", 1)[1])
        await state.set_state(GradeFlow.awaiting_grade)
        await state.update_data(submission_id=submission_id)
        await cb.message.edit_text("Оценка/комментарий:", reply_markup=kb_cancel())

    @router.message(GradeFlow.awaiting_grade, F.text)
    async def on_grade_text(message: Message, state: FSMContext, config: CourseTrackerConfig):
        if not _is_admin(message.from_user.id, config.admins_file):
            return
        data = await state.get_data()
        await state.clear()
        async with aiosqlite.connect(config.db_path) as db:
            await db.execute(
                "UPDATE course_submissions SET grade=? WHERE id=?", (message.text.strip(), data["submission_id"]),
            )
            await db.commit()
        await message.answer("✅ Оценка сохранена.", reply_markup=kb_admin_main())


# ── MAIN ──────────────────────────────────────────────────────────────────────
async def main():
    config = config_from_env()
    bot = Bot(token=os.getenv("BOT_TOKEN"))
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(ConfigMiddleware(config))
    dp.include_router(router)
    await bot.set_my_description(BOT_DESCRIPTION)
    await init_db(config.db_path)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
