# TEMPLATE: course_tracker
# USE FOR: трекер онлайн-курсов/тренингов — админ создаёт курсы, добавляет студентов и задания с дедлайнами, студент сдаёт решение (текст или файл) прямо в чат, админ видит статус каждого студента по каждому заданию (не сдано/сдано/просрочено/оценено) и может выставить оценку/комментарий, периодические напоминания студентам о невыполненных заданиях и админу о просрочках
# CUSTOMIZE: sections marked with # CUSTOMIZE
from __future__ import annotations

import asyncio
import html
import logging
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import aiosqlite
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

# ── CUSTOMIZE ────────────────────────────────────────────────────────────────
BOT_DESCRIPTION = (
    "Трекер онлайн-курсов: администратор создаёт курсы, добавляет студентов и "
    "задания с дедлайнами, студенты сдают решения прямо в чат, администратор "
    "видит статус каждого студента по каждому заданию и может выставить оценку."
)
WELCOME_TEXT = (
    "🎓 <b>Трекер курсов — панель администратора</b>\n\n"
    "Создавайте курсы, добавляйте студентов и задания с дедлайнами, "
    "отслеживайте прогресс и выставляйте оценки.\n\n"
    "Выберите действие:"
)
CLIENT_WELCOME_TEXT = (
    "👋 Здравствуйте! Это бот для сдачи заданий по курсу.\n\n"
    "Здесь можно посмотреть свои курсы, активные задания и сдать решение."
)
# ── END CUSTOMIZE ─────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
router = Router()

EXPECTED_TYPE_LABELS = {"text": "📝 Текст", "file": "📎 Файл", "any": "📝📎 Текст или файл"}

# Resubmission is allowed (owner-confirmed default): a new submission
# overwrites the previous row for the same (assignment_id, student_id) pair
# via UPSERT, and clears any prior grade/comment so the admin re-reviews it.


# ── config ───────────────────────────────────────────────────────────────────
# Same pattern as every other template — see docs/STAGE2_DESIGN.md.

@dataclass
class CourseTrackerConfig:
    bot_name: str
    db_path: str
    admins_file: Path
    welcome_image: Path
    display_name: str | None = None
    group_chat_id: str | None = None


def _paths_for(name: str, data_dir: Path) -> CourseTrackerConfig:
    return CourseTrackerConfig(
        bot_name=name,
        db_path=str(data_dir / f"{name}_data.db"),
        admins_file=data_dir / f"admins_{name}.json",
        welcome_image=data_dir / "bot_images" / f"{name}.jpg",
    )


def config_from_env() -> CourseTrackerConfig:
    """Standalone/subprocess mode."""
    name = Path(__file__).stem
    data_dir = Path(os.getenv("DATA_DIR", "./data"))
    data_dir.mkdir(exist_ok=True)
    return _paths_for(name, data_dir)


def config_from_bot_row(bot_row: dict, data_dir: Path) -> CourseTrackerConfig:
    """Webhook runtime mode. Paths built from bot_row["bot_id"] (bots.id, the
    physically unique AUTOINCREMENT PK) — NOT bot_row["name"] — same
    per-bot data isolation as every other template's config_from_bot_row."""
    bot_id = bot_row["bot_id"]
    config = CourseTrackerConfig(
        bot_name=bot_row["name"],
        db_path=str(data_dir / f"bot_{bot_id}_data.db"),
        admins_file=data_dir / f"admins_{bot_id}.json",
        welcome_image=data_dir / "bot_images" / f"bot_{bot_id}.jpg",
    )
    config.display_name = bot_row.get("display_name")
    config.group_chat_id = bot_row.get("group_chat_id")
    return config


class ConfigMiddleware(BaseMiddleware):
    """Injects this bot's CourseTrackerConfig into data["config"]."""

    def __init__(self, config: CourseTrackerConfig) -> None:
        self.config = config
        super().__init__()

    async def __call__(self, handler, event, data):
        data["config"] = self.config
        return await handler(event, data)


# ── admin helpers ─────────────────────────────────────────────────────────────
# Bot admins (admins.json) manage ALL courses — there is no separate
# per-course ownership/role, same simplification every other template makes
# (one flat admin set per bot). Students are tracked per-course in
# course_students instead.

def _load_admins(admins_file: Path) -> set:
    try:
        return set(json.loads(admins_file.read_text()).get("ids", []))
    except Exception:
        return set()

def _save_admins(admins_file: Path, ids: set) -> None:
    admins_file.write_text(json.dumps({"ids": list(ids)}, ensure_ascii=False))

def _is_admin(user_id: int, config: CourseTrackerConfig) -> bool:
    return str(user_id) in _load_admins(config.admins_file)


def _esc(value, max_len: int = 500) -> str:
    """HTML-escapes AND length-bounds any user-supplied text before it goes into
    a parse_mode="HTML" message — same helper as every other template."""
    text = str(value) if value is not None else ""
    if len(text) > max_len:
        text = text[:max_len] + "…"
    return html.escape(text)


def _join_bounded(lines: list[str], limit: int = 3500) -> str:
    """Joins lines with a length budget, dropping only WHOLE trailing lines."""
    out: list[str] = []
    total = 0
    for line in lines:
        if total + len(line) + 1 > limit:
            out.append("…")
            break
        out.append(line)
        total += len(line) + 1
    return "\n".join(out)


def _valid_admin_id(text: str) -> bool:
    if not (bool(text) and text.isascii() and text.isdigit() and len(text) <= 15):
        return False
    return int(text) > 0 and str(int(text)) == text


_valid_student_id = _valid_admin_id  # same shape: positive numeric Telegram id


def _parse_due_date(text: str) -> str | None:
    """Accepts "ДД.ММ.ГГГГ" or "ДД.ММ.ГГГГ ЧЧ:ММ" and returns a sortable/
    comparable string "YYYY-MM-DD HH:MM" (matching sqlite's
    datetime('now','localtime') format so plain string comparison works for
    overdue checks). Missing time defaults to 23:59 (end of day)."""
    text = text.strip()
    m = re.match(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})(?:\s+(\d{1,2}):(\d{2}))?$", text)
    if not m:
        return None
    day, month, year, hour, minute = m.groups()
    hour = hour or "23"
    minute = minute or "59"
    try:
        dt = datetime(int(year), int(month), int(day), int(hour), int(minute))
    except ValueError:
        return None
    return dt.strftime("%Y-%m-%d %H:%M")


def _format_due_date(iso: str) -> str:
    try:
        dt = datetime.strptime(iso, "%Y-%m-%d %H:%M")
    except ValueError:
        return iso
    return dt.strftime("%d.%m.%Y %H:%M")


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


# ── db ────────────────────────────────────────────────────────────────────────
# DESIGN NOTE — data model: courses → course_students (roster) and
# course_assignments (per-course tasks with a due_date) → course_submissions
# (one row per (assignment_id, student_id), UPSERTed on resubmission per the
# owner-confirmed "несколько раз" default). "Status" for a given
# assignment×student pair is partly STORED (submitted/graded on the
# submission row) and partly COMPUTED (not_submitted/overdue when no
# submission row exists yet) — see _submission_status() below, mirroring the
# repair_tracker precedent of keeping only what must be stored in the table.
#
# DESIGN NOTE — grading: owner chose "статус + оценка/комментарий" over
# status-only, so course_submissions carries `grade` (free-text, e.g. "5" or
# "зачёт") and `admin_comment`, both nullable until an admin grades the
# submission via GradeFlow.
#
# DESIGN NOTE — due date editing: owner chose "да, админ может менять" —
# a_due_edit:{assignment_id} → DueDateEditFlow lets an admin update
# course_assignments.due_date at any time, including after submissions exist
# (submissions are not retroactively affected; overdue is always computed
# against the CURRENT due_date at render/notify time).
#
# DESIGN NOTE — notifications: owner chose plain in-bot messages (no separate
# push channel). _notify_loop() below runs as a background task started in
# main(), once per NOTIFY_INTERVAL_SECONDS, and sends: (a) each student a
# digest of their own not-yet-submitted assignments across all their courses,
# (b) each bot admin a digest of overdue (student, assignment) pairs across
# all courses. This is deliberately re-computed and re-sent every interval
# rather than de-duplicated with a "notified" flag — a template-scope
# reminder loop, not a one-shot alert; the owner can add de-dup later if a
# generated bot needs it.

async def init_db(db_path: str):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS courses (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                description TEXT,
                created_at  TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS course_students (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                course_id   INTEGER NOT NULL REFERENCES courses(id),
                student_id  INTEGER NOT NULL,
                student_name TEXT,
                joined_at   TEXT DEFAULT (datetime('now','localtime')),
                UNIQUE(course_id, student_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS course_assignments (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                course_id     INTEGER NOT NULL REFERENCES courses(id),
                name          TEXT NOT NULL,
                description   TEXT,
                due_date      TEXT NOT NULL,
                expected_type TEXT NOT NULL DEFAULT 'any' CHECK(expected_type IN ('text','file','any')),
                created_at    TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS course_submissions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                assignment_id   INTEGER NOT NULL REFERENCES course_assignments(id),
                student_id      INTEGER NOT NULL,
                submission_text TEXT,
                file_id         TEXT,
                submitted_at    TEXT DEFAULT (datetime('now','localtime')),
                status          TEXT NOT NULL DEFAULT 'submitted' CHECK(status IN ('submitted','graded')),
                grade           TEXT,
                admin_comment   TEXT,
                UNIQUE(assignment_id, student_id)
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_students_course ON course_students(course_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_students_student ON course_students(student_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_assignments_course ON course_assignments(course_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_submissions_assignment ON course_submissions(assignment_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_submissions_student ON course_submissions(student_id)")
        await db.commit()


async def _sync_student_name(db_path: str, student_id: int, name: str) -> None:
    """Opportunistically fills in student_name the first time a student we
    already have on a roster (added by admin via Telegram id only) actually
    talks to the bot — same "identity fills in on first contact" idea as
    other templates that can't look up a Telegram display name in advance."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE course_students SET student_name=? WHERE student_id=? AND (student_name IS NULL OR student_name != ?)",
            (name, student_id, name),
        )
        await db.commit()


def _submission_status(due_date: str, sub_row) -> str:
    """Returns one of: not_submitted / overdue / submitted / graded."""
    if sub_row is None:
        return "overdue" if due_date < _now_str() else "not_submitted"
    return "graded" if sub_row["status"] == "graded" else "submitted"


STATUS_LABELS = {
    "not_submitted": "⏳ Не сдано",
    "overdue": "❌ Просрочено",
    "submitted": "📥 На проверке",
    "graded": "✅ Оценено",
}


# ── FSM staleness guard ─────────────────────────────────────────────────────────
FLOW_TIMEOUT_SECONDS = 300


def _flow_expired(data: dict) -> bool:
    started_at = data.get("started_at")
    return started_at is None or (time.time() - started_at) > FLOW_TIMEOUT_SECONDS


# ── FSM states ───────────────────────────────────────────────────────────────

class CourseFlow(StatesGroup):
    """Admin-side: create a course (name → description)."""
    name = State()
    description = State()

class StudentAddFlow(StatesGroup):
    """Admin-side: add a student's Telegram id to a course roster."""
    student_id = State()

class AssignmentFlow(StatesGroup):
    """Admin-side: create an assignment (name → description → due date → type)."""
    name = State()
    description = State()
    due_date = State()
    expected_type = State()

class DueDateEditFlow(StatesGroup):
    due_date = State()

class SubmissionFlow(StatesGroup):
    """Student-side: send text or a file as the solution."""
    content = State()

class GradeFlow(StatesGroup):
    """Admin-side: grade (short text, e.g. "5" or "зачёт") + optional comment."""
    grade = State()
    comment = State()

class AdminMgmtFlow(StatesGroup):
    add_admin = State()
    remove_admin_pick = State()


MAX_LIST_BUTTONS = 25
MAX_ADMIN_REMOVE_BUTTONS = 30
MAX_NAME_LEN = 200
MAX_DESC_LEN = 1000
MAX_SUBMISSION_LEN = 3000
MAX_GRADE_LEN = 50
MAX_COMMENT_LEN = 1000
NOTIFY_INTERVAL_SECONDS = int(os.getenv("COURSE_NOTIFY_INTERVAL_SECONDS", str(24 * 3600)))


# ── keyboards ─────────────────────────────────────────────────────────────────

def kb_back(callback_data: str = "main_menu") -> InlineKeyboardButton:
    return InlineKeyboardButton(text="◀️ Назад", callback_data=callback_data)

def kb_flow_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="flow_cancel")],
    ])

def kb_skip_cancel(skip_cb: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭ Пропустить", callback_data=skip_cb)],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="flow_cancel")],
    ])

# ── admin menu ──
def kb_admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📚 Мои курсы", callback_data="crs_list")],
        [InlineKeyboardButton(text="➕ Новый курс", callback_data="crs_new")],
        [InlineKeyboardButton(text="👥 Админы", callback_data="adm_menu")],
    ])

# ── student menu ──
def kb_student_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📚 Мои курсы", callback_data="stu_courses")],
    ])

def _menu_for(user_id: int, config: CourseTrackerConfig) -> tuple[str, InlineKeyboardMarkup]:
    if _is_admin(user_id, config):
        return WELCOME_TEXT, kb_admin_menu()
    return CLIENT_WELCOME_TEXT, kb_student_menu()


def kb_course_list(rows: list[tuple], callback_prefix: str, back_cb: str = "main_menu") -> InlineKeyboardMarkup:
    btns = [
        [InlineKeyboardButton(text=f"📚 {_esc(name, 40)}", callback_data=f"{callback_prefix}:{cid}")]
        for cid, name in rows[:MAX_LIST_BUTTONS]
    ]
    btns.append([kb_back(back_cb)])
    return InlineKeyboardMarkup(inline_keyboard=btns)


def kb_course_detail_admin(course_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Новое задание", callback_data=f"crs_assign_new:{course_id}")],
        [InlineKeyboardButton(text="👤 Добавить студента", callback_data=f"crs_student_add:{course_id}")],
        [InlineKeyboardButton(text="📋 Задания", callback_data=f"crs_assignments:{course_id}")],
        [InlineKeyboardButton(text="📊 Прогресс", callback_data=f"crs_progress:{course_id}")],
        [kb_back("crs_list")],
    ])


def kb_assignment_list(rows: list[tuple], callback_prefix: str, back_cb: str) -> InlineKeyboardMarkup:
    """rows are (id, name) tuples."""
    btns = [
        [InlineKeyboardButton(text=f"📝 {_esc(name, 40)}", callback_data=f"{callback_prefix}:{aid}")]
        for aid, name in rows[:MAX_LIST_BUTTONS]
    ]
    btns.append([kb_back(back_cb)])
    return InlineKeyboardMarkup(inline_keyboard=btns)


def kb_assignment_detail_admin(assignment_id: int, course_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить дедлайн", callback_data=f"a_due_edit:{assignment_id}")],
        [InlineKeyboardButton(text="📬 Решения на проверку", callback_data=f"a_submissions:{assignment_id}")],
        [kb_back(f"crs_assignments:{course_id}")],
    ])


def kb_assignment_detail_student(assignment_id: int, can_submit: bool, back_cb: str) -> InlineKeyboardMarkup:
    rows = []
    if can_submit:
        rows.append([InlineKeyboardButton(text="📤 Сдать", callback_data=f"stu_submit:{assignment_id}")])
    rows.append([kb_back(back_cb)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_submission_list(rows: list[tuple], assignment_id: int) -> InlineKeyboardMarkup:
    """rows are (submission_id, student_name_or_id, status) tuples."""
    btns = [
        [InlineKeyboardButton(
            text=f"{STATUS_LABELS.get(status, status)} · {_esc(label, 30)}",
            callback_data=f"sub_view:{sid}",
        )]
        for sid, label, status in rows[:MAX_LIST_BUTTONS]
    ]
    btns.append([kb_back(f"a_view:{assignment_id}")])
    return InlineKeyboardMarkup(inline_keyboard=btns)


def kb_submission_detail(submission_id: int, assignment_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Оценить", callback_data=f"sub_grade:{submission_id}")],
        [kb_back(f"a_submissions:{assignment_id}")],
    ])


def kb_expected_type() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=f"a_type:{code}")]
        for code, label in EXPECTED_TYPE_LABELS.items()
    ] + [[InlineKeyboardButton(text="❌ Отмена", callback_data="flow_cancel")]])


def kb_admins_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить админа", callback_data="adm_add")],
        [InlineKeyboardButton(text="➖ Убрать админа", callback_data="adm_remove")],
        [kb_back()],
    ])

def kb_remove_admins(ids: list[str]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=admin_id, callback_data=f"adm_rm:{i}")] for i, admin_id in enumerate(ids)]
    rows.append([kb_back("adm_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── /start ────────────────────────────────────────────────────────────────────

@router.message(Command("start"), F.chat.type == "private")
async def cmd_start(message: Message, state: FSMContext, config: CourseTrackerConfig):
    await state.clear()
    admins = _load_admins(config.admins_file)
    first_time_admin = not admins
    if first_time_admin:
        _save_admins(config.admins_file, {str(message.from_user.id)})
        admins = {str(message.from_user.id)}

    if str(message.from_user.id) not in admins:
        await _sync_student_name(config.db_path, message.from_user.id, message.from_user.full_name)

    text, kb = _menu_for(message.from_user.id, config)
    if str(message.from_user.id) in admins and config.welcome_image.exists():
        await message.answer_photo(FSInputFile(str(config.welcome_image)), caption=text, parse_mode="HTML", reply_markup=kb)
    else:
        await message.answer(text, parse_mode="HTML", reply_markup=kb)
    if first_time_admin:
        await message.answer(
            "👑 <b>Вы — администратор этого бота.</b>\n\n"
            "Управление другими администраторами — кнопка «👥 Админы» выше.",
            parse_mode="HTML",
        )


@router.callback_query(F.data == "main_menu")
async def cb_main_menu(cb: CallbackQuery, state: FSMContext, config: CourseTrackerConfig):
    await cb.answer()
    await state.clear()
    text, kb = _menu_for(cb.from_user.id, config)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "flow_cancel")
async def cb_flow_cancel(cb: CallbackQuery, state: FSMContext, config: CourseTrackerConfig):
    await cb.answer()
    await state.clear()
    _text, kb = _menu_for(cb.from_user.id, config)
    await cb.message.edit_text("Отменено.", reply_markup=kb)


# ── ADMIN: courses ────────────────────────────────────────────────────────────

@router.callback_query(F.data == "crs_list")
async def cb_crs_list(cb: CallbackQuery, state: FSMContext, config: CourseTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    async with aiosqlite.connect(config.db_path) as db:
        rows = await (await db.execute(
            "SELECT id, name FROM courses ORDER BY id DESC LIMIT ?", (MAX_LIST_BUTTONS,)
        )).fetchall()
    if not rows:
        await cb.message.edit_text("Курсов пока нет. Создайте первый.", reply_markup=kb_admin_menu())
        return
    await cb.message.edit_text("📚 Ваши курсы:", reply_markup=kb_course_list(rows, "crs_view"))


@router.callback_query(F.data == "crs_new")
async def cb_crs_new(cb: CallbackQuery, state: FSMContext, config: CourseTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    await state.set_state(CourseFlow.name)
    await state.update_data(started_at=time.time())
    await cb.message.edit_text("📚 Название курса:", reply_markup=kb_flow_cancel())


@router.message(CourseFlow.name, F.text, ~F.text.startswith("/"))
async def crs_name_entered(msg: Message, state: FSMContext):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_admin_menu())
        return
    name = msg.text.strip()
    if not name:
        await msg.answer("Название не может быть пустым. Введите название курса:", reply_markup=kb_flow_cancel())
        return
    if len(name) > MAX_NAME_LEN:
        await msg.answer(f"⚠️ Слишком длинное название. Уложитесь в {MAX_NAME_LEN} символов:", reply_markup=kb_flow_cancel())
        return
    await state.update_data(course_name=name)
    await state.set_state(CourseFlow.description)
    await msg.answer("📝 Краткое описание курса (или «Пропустить»):", reply_markup=kb_skip_cancel("crs_desc_skip"))


async def _finalize_course(message_answer, state: FSMContext, config: CourseTrackerConfig, description: str | None) -> None:
    data = await state.get_data()
    name = data.get("course_name")
    await state.clear()
    if not name:
        await message_answer("Сессия устарела, начните заново.", reply_markup=kb_admin_menu())
        return
    async with aiosqlite.connect(config.db_path) as db:
        cur = await db.execute("INSERT INTO courses (name, description) VALUES (?,?)", (name, description))
        course_id = cur.lastrowid
        await db.commit()
    await message_answer(
        f"✅ Курс «{_esc(name)}» создан.", parse_mode="HTML",
        reply_markup=kb_course_detail_admin(course_id),
    )


@router.message(CourseFlow.description, F.text, ~F.text.startswith("/"))
async def crs_description_entered(msg: Message, state: FSMContext, config: CourseTrackerConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_admin_menu())
        return
    desc = msg.text.strip()
    if len(desc) > MAX_DESC_LEN:
        await msg.answer(f"⚠️ Слишком длинное описание. Уложитесь в {MAX_DESC_LEN} символов:", reply_markup=kb_skip_cancel("crs_desc_skip"))
        return
    await _finalize_course(msg.answer, state, config, desc or None)


@router.callback_query(CourseFlow.description, F.data == "crs_desc_skip")
async def cb_crs_desc_skip(cb: CallbackQuery, state: FSMContext, config: CourseTrackerConfig):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_admin_menu())
        return
    await _finalize_course(cb.message.answer, state, config, None)


async def _course_detail_text(db_path: str, course_id: int) -> tuple[str, str] | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute("SELECT * FROM courses WHERE id=?", (course_id,))).fetchone()
        if not row:
            return None
        n_students = (await (await db.execute(
            "SELECT COUNT(*) FROM course_students WHERE course_id=?", (course_id,)
        )).fetchone())[0]
        n_assignments = (await (await db.execute(
            "SELECT COUNT(*) FROM course_assignments WHERE course_id=?", (course_id,)
        )).fetchone())[0]
    lines = [f"📚 <b>{_esc(row['name'])}</b>\n"]
    if row["description"]:
        lines.append(_esc(row["description"]))
    lines.append(f"\n👥 Студентов: {n_students} · 📝 Заданий: {n_assignments}")
    return _join_bounded(lines), row["name"]


@router.callback_query(F.data.startswith("crs_view:"))
async def cb_crs_view(cb: CallbackQuery, config: CourseTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        course_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    res = await _course_detail_text(config.db_path, course_id)
    if res is None:
        await cb.message.edit_text("Курс не найден.", reply_markup=kb_admin_menu())
        return
    text, _name = res
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_course_detail_admin(course_id))


# ── ADMIN: add student to a course ────────────────────────────────────────────

@router.callback_query(F.data.startswith("crs_student_add:"))
async def cb_crs_student_add(cb: CallbackQuery, state: FSMContext, config: CourseTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        course_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    await state.set_state(StudentAddFlow.student_id)
    await state.update_data(started_at=time.time(), student_add_course_id=course_id)
    await cb.message.edit_text("👤 Введите Telegram ID студента:", reply_markup=kb_flow_cancel())


@router.message(StudentAddFlow.student_id, F.text, ~F.text.startswith("/"))
async def student_add_id_entered(msg: Message, state: FSMContext, config: CourseTrackerConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_admin_menu())
        return
    text = msg.text.strip()
    if not _valid_student_id(text):
        await msg.answer("Некорректный ID. Введите числовой Telegram ID студента:", reply_markup=kb_flow_cancel())
        return
    course_id = data.get("student_add_course_id")
    await state.clear()
    if not course_id:
        await msg.answer("Сессия устарела, начните заново.", reply_markup=kb_admin_menu())
        return
    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute("SELECT id FROM courses WHERE id=?", (course_id,))).fetchone()
        if not row:
            await msg.answer("Курс не найден.", reply_markup=kb_admin_menu())
            return
        await db.execute(
            "INSERT OR IGNORE INTO course_students (course_id, student_id) VALUES (?,?)",
            (course_id, int(text)),
        )
        await db.commit()
    await msg.answer(f"✅ Студент <code>{text}</code> добавлен на курс.", parse_mode="HTML", reply_markup=kb_course_detail_admin(course_id))


# ── ADMIN: assignments ────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("crs_assignments:"))
async def cb_crs_assignments(cb: CallbackQuery, config: CourseTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        course_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    async with aiosqlite.connect(config.db_path) as db:
        rows = await (await db.execute(
            "SELECT id, name FROM course_assignments WHERE course_id=? ORDER BY due_date ASC LIMIT ?",
            (course_id, MAX_LIST_BUTTONS),
        )).fetchall()
    if not rows:
        await cb.message.edit_text("Заданий пока нет.", reply_markup=kb_course_detail_admin(course_id))
        return
    await cb.message.edit_text(
        "📋 Задания курса:", reply_markup=kb_assignment_list(rows, "a_view", f"crs_view:{course_id}"),
    )


@router.callback_query(F.data.startswith("crs_assign_new:"))
async def cb_crs_assign_new(cb: CallbackQuery, state: FSMContext, config: CourseTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        course_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute("SELECT id FROM courses WHERE id=?", (course_id,))).fetchone()
    if not row:
        await cb.message.edit_text("Курс не найден.", reply_markup=kb_admin_menu())
        return
    await state.set_state(AssignmentFlow.name)
    await state.update_data(started_at=time.time(), assign_course_id=course_id)
    await cb.message.edit_text("📝 Название задания:", reply_markup=kb_flow_cancel())


@router.message(AssignmentFlow.name, F.text, ~F.text.startswith("/"))
async def assign_name_entered(msg: Message, state: FSMContext):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_admin_menu())
        return
    name = msg.text.strip()
    if not name:
        await msg.answer("Название не может быть пустым. Введите название задания:", reply_markup=kb_flow_cancel())
        return
    if len(name) > MAX_NAME_LEN:
        await msg.answer(f"⚠️ Слишком длинное название. Уложитесь в {MAX_NAME_LEN} символов:", reply_markup=kb_flow_cancel())
        return
    await state.update_data(assign_name=name)
    await state.set_state(AssignmentFlow.description)
    await msg.answer("📄 Описание задания (или «Пропустить»):", reply_markup=kb_skip_cancel("a_desc_skip"))


@router.message(AssignmentFlow.description, F.text, ~F.text.startswith("/"))
async def assign_description_entered(msg: Message, state: FSMContext):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_admin_menu())
        return
    desc = msg.text.strip()
    if len(desc) > MAX_DESC_LEN:
        await msg.answer(f"⚠️ Слишком длинное описание. Уложитесь в {MAX_DESC_LEN} символов:", reply_markup=kb_skip_cancel("a_desc_skip"))
        return
    await state.update_data(assign_description=desc or None)
    await state.set_state(AssignmentFlow.due_date)
    await msg.answer("📅 Дедлайн (формат ДД.ММ.ГГГГ или ДД.ММ.ГГГГ ЧЧ:ММ):", reply_markup=kb_flow_cancel())


@router.callback_query(AssignmentFlow.description, F.data == "a_desc_skip")
async def cb_assign_desc_skip(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_admin_menu())
        return
    await state.update_data(assign_description=None)
    await state.set_state(AssignmentFlow.due_date)
    await cb.message.edit_text("📅 Дедлайн (формат ДД.ММ.ГГГГ или ДД.ММ.ГГГГ ЧЧ:ММ):", reply_markup=kb_flow_cancel())


@router.message(AssignmentFlow.due_date, F.text, ~F.text.startswith("/"))
async def assign_due_date_entered(msg: Message, state: FSMContext):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_admin_menu())
        return
    due = _parse_due_date(msg.text)
    if due is None:
        await msg.answer(
            "❌ Не удалось распознать дату. Формат: <b>ДД.ММ.ГГГГ</b> или <b>ДД.ММ.ГГГГ ЧЧ:ММ</b>, например 31.12.2026:",
            parse_mode="HTML", reply_markup=kb_flow_cancel(),
        )
        return
    await state.update_data(assign_due_date=due)
    await state.set_state(AssignmentFlow.expected_type)
    await msg.answer("📎 Что должен сдать студент?", reply_markup=kb_expected_type())


@router.callback_query(AssignmentFlow.expected_type, F.data.startswith("a_type:"))
async def cb_assign_type(cb: CallbackQuery, state: FSMContext, config: CourseTrackerConfig):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_admin_menu())
        return
    expected_type = cb.data.split(":", 1)[1]
    if expected_type not in EXPECTED_TYPE_LABELS:
        return
    course_id = data.get("assign_course_id")
    name = data.get("assign_name")
    description = data.get("assign_description")
    due_date = data.get("assign_due_date")
    await state.clear()
    if not (course_id and name and due_date):
        await cb.message.edit_text("Сессия устарела, начните заново.", reply_markup=kb_admin_menu())
        return

    async with aiosqlite.connect(config.db_path) as db:
        cur = await db.execute(
            "INSERT INTO course_assignments (course_id, name, description, due_date, expected_type) VALUES (?,?,?,?,?)",
            (course_id, name, description, due_date, expected_type),
        )
        assignment_id = cur.lastrowid
        await db.commit()
    await cb.message.edit_text(
        f"✅ Задание «{_esc(name)}» создано. Дедлайн: {_format_due_date(due_date)}",
        parse_mode="HTML", reply_markup=kb_assignment_detail_admin(assignment_id, course_id),
    )


async def _assignment_detail_admin(db_path: str, assignment_id: int) -> tuple[str, int] | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute("SELECT * FROM course_assignments WHERE id=?", (assignment_id,))).fetchone()
        if not row:
            return None
        n_submitted = (await (await db.execute(
            "SELECT COUNT(*) FROM course_submissions WHERE assignment_id=?", (assignment_id,)
        )).fetchone())[0]
        n_students = (await (await db.execute(
            "SELECT COUNT(*) FROM course_students WHERE course_id=?", (row["course_id"],)
        )).fetchone())[0]
    lines = [f"📝 <b>{_esc(row['name'])}</b>\n"]
    if row["description"]:
        lines.append(_esc(row["description"]))
    lines.append(f"\n📅 Дедлайн: {_format_due_date(row['due_date'])}")
    lines.append(f"📎 Ожидается: {EXPECTED_TYPE_LABELS.get(row['expected_type'], row['expected_type'])}")
    lines.append(f"📬 Сдано: {n_submitted}/{n_students}")
    return _join_bounded(lines), row["course_id"]


@router.callback_query(F.data.startswith("a_view:"))
async def cb_a_view(cb: CallbackQuery, config: CourseTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        assignment_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    res = await _assignment_detail_admin(config.db_path, assignment_id)
    if res is None:
        await cb.message.edit_text("Задание не найдено.", reply_markup=kb_admin_menu())
        return
    text, course_id = res
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_assignment_detail_admin(assignment_id, course_id))


@router.callback_query(F.data.startswith("a_due_edit:"))
async def cb_a_due_edit(cb: CallbackQuery, state: FSMContext, config: CourseTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        assignment_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute("SELECT id FROM course_assignments WHERE id=?", (assignment_id,))).fetchone()
    if not row:
        await cb.message.edit_text("Задание не найдено.", reply_markup=kb_admin_menu())
        return
    await state.set_state(DueDateEditFlow.due_date)
    await state.update_data(started_at=time.time(), due_edit_assignment_id=assignment_id)
    await cb.message.edit_text("📅 Новый дедлайн (ДД.ММ.ГГГГ или ДД.ММ.ГГГГ ЧЧ:ММ):", reply_markup=kb_flow_cancel())


@router.message(DueDateEditFlow.due_date, F.text, ~F.text.startswith("/"))
async def due_date_edit_entered(msg: Message, state: FSMContext, config: CourseTrackerConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_admin_menu())
        return
    due = _parse_due_date(msg.text)
    if due is None:
        await msg.answer(
            "❌ Не удалось распознать дату. Формат: ДД.ММ.ГГГГ или ДД.ММ.ГГГГ ЧЧ:ММ:", reply_markup=kb_flow_cancel(),
        )
        return
    assignment_id = data.get("due_edit_assignment_id")
    await state.clear()
    if not assignment_id:
        await msg.answer("Сессия устарела, начните заново.", reply_markup=kb_admin_menu())
        return
    async with aiosqlite.connect(config.db_path) as db:
        cur = await db.execute("UPDATE course_assignments SET due_date=? WHERE id=?", (due, assignment_id))
        await db.commit()
    if cur.rowcount == 0:
        await msg.answer("Задание не найдено.", reply_markup=kb_admin_menu())
        return
    res = await _assignment_detail_admin(config.db_path, assignment_id)
    if res is None:
        await msg.answer("Задание не найдено.", reply_markup=kb_admin_menu())
        return
    text, course_id = res
    await msg.answer(
        f"✅ Дедлайн обновлён.\n\n{text}", parse_mode="HTML",
        reply_markup=kb_assignment_detail_admin(assignment_id, course_id),
    )


# ── ADMIN: progress table ─────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("crs_progress:"))
async def cb_crs_progress(cb: CallbackQuery, config: CourseTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        course_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        course = await (await db.execute("SELECT name FROM courses WHERE id=?", (course_id,))).fetchone()
        if not course:
            await cb.message.edit_text("Курс не найден.", reply_markup=kb_admin_menu())
            return
        students = await (await db.execute(
            "SELECT student_id, student_name FROM course_students WHERE course_id=? ORDER BY id", (course_id,)
        )).fetchall()
        assignments = await (await db.execute(
            "SELECT id, name, due_date FROM course_assignments WHERE course_id=? ORDER BY due_date ASC", (course_id,)
        )).fetchall()
        subs = await (await db.execute(
            "SELECT s.* FROM course_submissions s JOIN course_assignments a ON s.assignment_id=a.id WHERE a.course_id=?",
            (course_id,),
        )).fetchall()
    sub_by_key = {(s["assignment_id"], s["student_id"]): s for s in subs}

    if not students or not assignments:
        await cb.message.edit_text(
            "Для таблицы прогресса нужны и студенты, и задания.", reply_markup=kb_course_detail_admin(course_id),
        )
        return

    lines = [f"📊 <b>Прогресс · {_esc(course['name'])}</b>\n"]
    for a in assignments:
        lines.append(f"\n📝 <b>{_esc(a['name'], 40)}</b> (до {_format_due_date(a['due_date'])})")
        for st in students:
            label = st["student_name"] or str(st["student_id"])
            status = _submission_status(a["due_date"], sub_by_key.get((a["id"], st["student_id"])))
            grade_suffix = ""
            row = sub_by_key.get((a["id"], st["student_id"]))
            if row and row["status"] == "graded" and row["grade"]:
                grade_suffix = f" ({_esc(row['grade'], 20)})"
            lines.append(f"  • {_esc(label, 30)} — {STATUS_LABELS[status]}{grade_suffix}")
    await cb.message.edit_text(_join_bounded(lines), parse_mode="HTML", reply_markup=kb_course_detail_admin(course_id))


# ── ADMIN: submissions & grading ──────────────────────────────────────────────

@router.callback_query(F.data.startswith("a_submissions:"))
async def cb_a_submissions(cb: CallbackQuery, config: CourseTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        assignment_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT s.id AS sid, s.status AS status, "
            "COALESCE(cs.student_name, CAST(s.student_id AS TEXT)) AS label "
            "FROM course_submissions s "
            "LEFT JOIN course_students cs ON cs.student_id = s.student_id "
            "WHERE s.assignment_id=? ORDER BY s.submitted_at DESC LIMIT ?",
            (assignment_id, MAX_LIST_BUTTONS),
        )).fetchall()
    if not rows:
        await cb.message.edit_text("Пока никто не сдал.", reply_markup=kb_assignment_detail_admin(assignment_id, 0))
        return
    list_rows = [(r["sid"], r["label"], r["status"]) for r in rows]
    await cb.message.edit_text("📬 Решения на проверку:", reply_markup=kb_submission_list(list_rows, assignment_id))


async def _submission_detail_text(db_path: str, submission_id: int):
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            "SELECT s.*, COALESCE(cs.student_name, CAST(s.student_id AS TEXT)) AS label, "
            "a.name AS assignment_name, a.id AS assignment_id "
            "FROM course_submissions s "
            "JOIN course_assignments a ON a.id = s.assignment_id "
            "LEFT JOIN course_students cs ON cs.student_id = s.student_id "
            "WHERE s.id=?", (submission_id,),
        )).fetchone()
    if not row:
        return None
    lines = [
        f"📬 <b>{_esc(row['assignment_name'])}</b>",
        f"👤 {_esc(row['label'])}",
        f"🕐 Сдано: {row['submitted_at']}",
    ]
    if row["submission_text"]:
        lines.append(f"\n📝 {_esc(row['submission_text'], 1500)}")
    if row["file_id"]:
        lines.append("📎 Прикреплён файл")
    if row["status"] == "graded":
        lines.append(f"\n⭐ Оценка: {_esc(row['grade'] or '—', 20)}")
        if row["admin_comment"]:
            lines.append(f"💬 Комментарий: {_esc(row['admin_comment'])}")
    return _join_bounded(lines), row


@router.callback_query(F.data.startswith("sub_view:"))
async def cb_sub_view(cb: CallbackQuery, config: CourseTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        submission_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    res = await _submission_detail_text(config.db_path, submission_id)
    if res is None:
        await cb.message.edit_text("Решение не найдено.", reply_markup=kb_admin_menu())
        return
    text, row = res
    if row["file_id"]:
        try:
            await cb.message.answer_document(row["file_id"], caption=text, parse_mode="HTML")
            await cb.message.answer("Действия:", reply_markup=kb_submission_detail(submission_id, row["assignment_id"]))
            return
        except TelegramAPIError:
            pass
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_submission_detail(submission_id, row["assignment_id"]))


@router.callback_query(F.data.startswith("sub_grade:"))
async def cb_sub_grade(cb: CallbackQuery, state: FSMContext, config: CourseTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        submission_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute("SELECT id FROM course_submissions WHERE id=?", (submission_id,))).fetchone()
    if not row:
        await cb.message.edit_text("Решение не найдено.", reply_markup=kb_admin_menu())
        return
    await state.set_state(GradeFlow.grade)
    await state.update_data(started_at=time.time(), grade_submission_id=submission_id)
    await cb.message.edit_text("⭐ Введите оценку (например: 5 или «зачёт»):", reply_markup=kb_flow_cancel())


@router.message(GradeFlow.grade, F.text, ~F.text.startswith("/"))
async def grade_entered(msg: Message, state: FSMContext):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_admin_menu())
        return
    grade = msg.text.strip()
    if not grade:
        await msg.answer("Оценка не может быть пустой:", reply_markup=kb_flow_cancel())
        return
    if len(grade) > MAX_GRADE_LEN:
        await msg.answer(f"⚠️ Слишком длинная оценка. Уложитесь в {MAX_GRADE_LEN} символов:", reply_markup=kb_flow_cancel())
        return
    await state.update_data(grade_value=grade)
    await state.set_state(GradeFlow.comment)
    await msg.answer("💬 Комментарий студенту (или «Пропустить»):", reply_markup=kb_skip_cancel("grade_comment_skip"))


async def _finalize_grade(message_answer, state: FSMContext, config: CourseTrackerConfig, bot: Bot, comment: str | None) -> None:
    data = await state.get_data()
    submission_id = data.get("grade_submission_id")
    grade = data.get("grade_value")
    await state.clear()
    if not (submission_id and grade):
        await message_answer("Сессия устарела, начните заново.", reply_markup=kb_admin_menu())
        return
    async with aiosqlite.connect(config.db_path) as db:
        cur = await db.execute(
            "UPDATE course_submissions SET status='graded', grade=?, admin_comment=? WHERE id=?",
            (grade, comment, submission_id),
        )
        await db.commit()
        if cur.rowcount:
            row = await (await db.execute(
                "SELECT s.student_id, a.name AS assignment_name FROM course_submissions s "
                "JOIN course_assignments a ON a.id = s.assignment_id WHERE s.id=?", (submission_id,),
            )).fetchone()
        else:
            row = None
    if not row:
        await message_answer("Решение не найдено.", reply_markup=kb_admin_menu())
        return
    student_id, assignment_name = row
    text = f"⭐ Ваше задание «{_esc(assignment_name)}» оценено: {_esc(grade, 20)}"
    if comment:
        text += f"\n💬 {_esc(comment)}"
    try:
        await bot.send_message(student_id, text, parse_mode="HTML")
        note = "🔔 Студент уведомлён."
    except TelegramAPIError as e:
        logger.warning(f"course_tracker: failed to notify student {student_id} of grade: {e}")
        note = "⚠️ Не удалось уведомить студента."
    await message_answer(f"✅ Оценка сохранена. {note}")


@router.message(GradeFlow.comment, F.text, ~F.text.startswith("/"))
async def grade_comment_entered(msg: Message, state: FSMContext, config: CourseTrackerConfig, bot: Bot):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_admin_menu())
        return
    comment = msg.text.strip()
    if len(comment) > MAX_COMMENT_LEN:
        await msg.answer(f"⚠️ Слишком длинный комментарий. Уложитесь в {MAX_COMMENT_LEN} символов:", reply_markup=kb_skip_cancel("grade_comment_skip"))
        return
    await _finalize_grade(msg.answer, state, config, bot, comment or None)


@router.callback_query(GradeFlow.comment, F.data == "grade_comment_skip")
async def cb_grade_comment_skip(cb: CallbackQuery, state: FSMContext, config: CourseTrackerConfig, bot: Bot):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_admin_menu())
        return
    await _finalize_grade(cb.message.answer, state, config, bot, None)


# ── STUDENT: courses & assignments ────────────────────────────────────────────

@router.callback_query(F.data == "stu_courses")
async def cb_stu_courses(cb: CallbackQuery, state: FSMContext, config: CourseTrackerConfig):
    await cb.answer()
    await state.clear()
    async with aiosqlite.connect(config.db_path) as db:
        rows = await (await db.execute(
            "SELECT c.id, c.name FROM courses c JOIN course_students cs ON cs.course_id = c.id "
            "WHERE cs.student_id=? ORDER BY c.id DESC LIMIT ?",
            (cb.from_user.id, MAX_LIST_BUTTONS),
        )).fetchall()
    if not rows:
        await cb.message.edit_text("Вы пока не состоите ни в одном курсе.", reply_markup=kb_student_menu())
        return
    await cb.message.edit_text("📚 Ваши курсы:", reply_markup=kb_course_list(rows, "stu_course"))


@router.callback_query(F.data.startswith("stu_course:"))
async def cb_stu_course(cb: CallbackQuery, config: CourseTrackerConfig):
    await cb.answer()
    try:
        course_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    async with aiosqlite.connect(config.db_path) as db:
        member = await (await db.execute(
            "SELECT 1 FROM course_students WHERE course_id=? AND student_id=?", (course_id, cb.from_user.id)
        )).fetchone()
        if not member:
            await cb.message.edit_text("Курс не найден.", reply_markup=kb_student_menu())
            return
        rows = await (await db.execute(
            "SELECT id, name FROM course_assignments WHERE course_id=? ORDER BY due_date ASC LIMIT ?",
            (course_id, MAX_LIST_BUTTONS),
        )).fetchall()
    if not rows:
        await cb.message.edit_text("В этом курсе пока нет заданий.", reply_markup=kb_course_list([], "stu_course"))
        return
    await cb.message.edit_text("📋 Задания:", reply_markup=kb_assignment_list(rows, "stu_assign", "stu_courses"))


@router.callback_query(F.data.startswith("stu_assign:"))
async def cb_stu_assign(cb: CallbackQuery, config: CourseTrackerConfig):
    await cb.answer()
    try:
        assignment_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        a = await (await db.execute(
            "SELECT a.*, cs.student_id FROM course_assignments a "
            "JOIN course_students cs ON cs.course_id = a.course_id "
            "WHERE a.id=? AND cs.student_id=?", (assignment_id, cb.from_user.id),
        )).fetchone()
        if not a:
            await cb.message.edit_text("Задание не найдено.", reply_markup=kb_student_menu())
            return
        sub = await (await db.execute(
            "SELECT * FROM course_submissions WHERE assignment_id=? AND student_id=?",
            (assignment_id, cb.from_user.id),
        )).fetchone()
    status = _submission_status(a["due_date"], sub)
    lines = [f"📝 <b>{_esc(a['name'])}</b> · {STATUS_LABELS[status]}\n"]
    if a["description"]:
        lines.append(_esc(a["description"]))
    lines.append(f"\n📅 Дедлайн: {_format_due_date(a['due_date'])}")
    lines.append(f"📎 Ожидается: {EXPECTED_TYPE_LABELS.get(a['expected_type'], a['expected_type'])}")
    if sub:
        if sub["status"] == "graded":
            lines.append(f"\n⭐ Оценка: {_esc(sub['grade'] or '—', 20)}")
            if sub["admin_comment"]:
                lines.append(f"💬 {_esc(sub['admin_comment'])}")
        else:
            lines.append("\n📥 Ваше решение отправлено, ожидает проверки.")
    can_submit = True  # resubmission allowed (owner-confirmed default)
    await cb.message.edit_text(
        _join_bounded(lines), parse_mode="HTML",
        reply_markup=kb_assignment_detail_student(assignment_id, can_submit, f"stu_course:{a['course_id']}"),
    )


@router.callback_query(F.data.startswith("stu_submit:"))
async def cb_stu_submit(cb: CallbackQuery, state: FSMContext, config: CourseTrackerConfig):
    await cb.answer()
    try:
        assignment_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute(
            "SELECT a.expected_type FROM course_assignments a "
            "JOIN course_students cs ON cs.course_id = a.course_id "
            "WHERE a.id=? AND cs.student_id=?", (assignment_id, cb.from_user.id),
        )).fetchone()
    if not row:
        await cb.message.edit_text("Задание не найдено.", reply_markup=kb_student_menu())
        return
    await state.set_state(SubmissionFlow.content)
    await state.update_data(started_at=time.time(), submit_assignment_id=assignment_id)
    hint = {"text": "Отправьте текстовое решение:", "file": "Прикрепите файл с решением:"}.get(
        row[0], "Отправьте текст или прикрепите файл с решением:",
    )
    await cb.message.edit_text(f"📤 {hint}", reply_markup=kb_flow_cancel())


@router.message(SubmissionFlow.content, F.text | F.document, ~(F.text.startswith("/")))
async def submission_content_received(msg: Message, state: FSMContext, config: CourseTrackerConfig, bot: Bot):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_student_menu())
        return
    assignment_id = data.get("submit_assignment_id")
    if not assignment_id:
        await state.clear()
        await msg.answer("Сессия устарела, начните заново.", reply_markup=kb_student_menu())
        return

    submission_text = msg.text.strip() if msg.text else None
    file_id = msg.document.file_id if msg.document else None
    if submission_text and len(submission_text) > MAX_SUBMISSION_LEN:
        await msg.answer(f"⚠️ Слишком длинное решение. Уложитесь в {MAX_SUBMISSION_LEN} символов:", reply_markup=kb_flow_cancel())
        return
    if not submission_text and not file_id:
        await msg.answer("Отправьте текст решения или прикрепите файл:", reply_markup=kb_flow_cancel())
        return
    await state.clear()

    async with aiosqlite.connect(config.db_path) as db:
        # Resubmission overwrites the previous row and clears any prior grade —
        # owner-confirmed "несколько раз" + a resubmission should go back to
        # the review queue rather than keep a stale grade.
        await db.execute(
            "INSERT INTO course_submissions (assignment_id, student_id, submission_text, file_id, submitted_at, status, grade, admin_comment) "
            "VALUES (?,?,?,?,datetime('now','localtime'),'submitted',NULL,NULL) "
            "ON CONFLICT(assignment_id, student_id) DO UPDATE SET "
            "submission_text=excluded.submission_text, file_id=excluded.file_id, "
            "submitted_at=excluded.submitted_at, status='submitted', grade=NULL, admin_comment=NULL",
            (assignment_id, msg.from_user.id, submission_text, file_id),
        )
        await db.commit()
        row = await (await db.execute(
            "SELECT a.name, a.course_id FROM course_assignments a WHERE a.id=?", (assignment_id,)
        )).fetchone()

    await msg.answer("✅ Решение отправлено на проверку!", reply_markup=kb_student_menu())

    if row:
        assignment_name, course_id = row
        admins = _load_admins(config.admins_file)
        student_label = msg.from_user.full_name
        notify_text = f"📬 Новое решение по «{_esc(assignment_name)}» от {_esc(student_label)}"
        for admin_id in admins:
            try:
                await bot.send_message(int(admin_id), notify_text, parse_mode="HTML")
            except (TelegramAPIError, ValueError) as e:
                logger.warning(f"course_tracker: failed to notify admin {admin_id} of new submission: {e}")


# ── ADMINS menu ────────────────────────────────────────────────────────────────
# Copied verbatim (same shape) from other templates — admins.json-backed.

async def _admins_list_text(config: CourseTrackerConfig) -> str:
    ids = sorted(_load_admins(config.admins_file))
    if not ids:
        return "👥 Пусто"
    return _join_bounded(["👥 <b>Администраторы бота:</b>\n"] + [f"• <code>{_esc(i)}</code>" for i in ids])


@router.callback_query(F.data == "adm_menu")
async def cb_adm_menu(cb: CallbackQuery, state: FSMContext, config: CourseTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    text = await _admins_list_text(config)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_admins_menu())


@router.callback_query(F.data == "adm_add")
async def cb_adm_add(cb: CallbackQuery, state: FSMContext, config: CourseTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.set_state(AdminMgmtFlow.add_admin)
    await state.update_data(started_at=time.time())
    await cb.message.edit_text("Введите Telegram ID нового администратора:", reply_markup=kb_flow_cancel())


@router.message(AdminMgmtFlow.add_admin, F.text, ~F.text.startswith("/"))
async def admin_add_id(msg: Message, state: FSMContext, config: CourseTrackerConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_admin_menu())
        return
    text = msg.text.strip()
    if not _valid_admin_id(text):
        await msg.answer("Некорректный ID. Введите числовой Telegram ID.", reply_markup=kb_flow_cancel())
        return
    await state.clear()
    ids = _load_admins(config.admins_file)
    ids.add(text)
    _save_admins(config.admins_file, ids)
    await msg.answer(f"✅ <code>{text}</code> добавлен.", parse_mode="HTML", reply_markup=kb_admins_menu())


@router.callback_query(F.data == "adm_remove")
async def cb_adm_remove(cb: CallbackQuery, state: FSMContext, config: CourseTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    ids = sorted(_load_admins(config.admins_file))
    if len(ids) <= 1:
        await cb.message.edit_text("Нельзя удалить последнего администратора.", reply_markup=kb_admins_menu())
        return
    if len(ids) > MAX_ADMIN_REMOVE_BUTTONS:
        await cb.message.edit_text(
            "Слишком много админов для списка кнопок. Обратитесь к разработчику.", reply_markup=kb_admins_menu()
        )
        return
    await state.set_state(AdminMgmtFlow.remove_admin_pick)
    await state.update_data(started_at=time.time(), remove_admin_ids=ids)
    await cb.message.edit_text("Выберите администратора для удаления:", reply_markup=kb_remove_admins(ids))


@router.callback_query(AdminMgmtFlow.remove_admin_pick, F.data.startswith("adm_rm:"))
async def cb_adm_remove_pick(cb: CallbackQuery, state: FSMContext, config: CourseTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_admin_menu())
        return
    try:
        idx = int(cb.data.split(":", 1)[1])
        target = data["remove_admin_ids"][idx]
    except (ValueError, IndexError, KeyError):
        await state.clear()
        await cb.message.edit_text("Некорректный выбор.", reply_markup=kb_admins_menu())
        return
    ids = _load_admins(config.admins_file)
    if len(ids) <= 1:
        await state.clear()
        await cb.message.edit_text("Нельзя удалить последнего администратора.", reply_markup=kb_admins_menu())
        return
    ids.discard(target)
    _save_admins(config.admins_file, ids)
    await state.clear()
    await cb.message.edit_text(f"✅ <code>{_esc(target)}</code> удалён.", parse_mode="HTML", reply_markup=kb_admins_menu())


# ── notification loop ─────────────────────────────────────────────────────────

async def _run_notify_pass(config: CourseTrackerConfig, bot: Bot) -> None:
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        assignments = await (await db.execute("SELECT * FROM course_assignments")).fetchall()
        students = await (await db.execute("SELECT course_id, student_id FROM course_students")).fetchall()
        subs = await (await db.execute("SELECT assignment_id, student_id, status FROM course_submissions")).fetchall()
    sub_keys = {(s["student_id"], s["assignment_id"]): s["status"] for s in subs}
    students_by_course: dict[int, list[int]] = {}
    for s in students:
        students_by_course.setdefault(s["course_id"], []).append(s["student_id"])

    now = _now_str()
    pending_by_student: dict[int, list[str]] = {}
    overdue_for_admins: list[str] = []
    for a in assignments:
        roster = students_by_course.get(a["course_id"], [])
        for student_id in roster:
            status = sub_keys.get((student_id, a["id"]))
            if status is not None:
                continue  # already submitted (submitted or graded) — nothing to remind
            pending_by_student.setdefault(student_id, []).append(f"• {a['name']} (до {_format_due_date(a['due_date'])})")
            if a["due_date"] < now:
                overdue_for_admins.append(f"• {a['name']} — студент {student_id}")

    for student_id, lines in pending_by_student.items():
        text = "⏰ <b>Невыполненные задания:</b>\n\n" + "\n".join(lines[:MAX_LIST_BUTTONS])
        try:
            await bot.send_message(student_id, text, parse_mode="HTML")
        except TelegramAPIError as e:
            logger.warning(f"course_tracker: notify pass failed for student {student_id}: {e}")

    if overdue_for_admins:
        admins = _load_admins(config.admins_file)
        text = "🚨 <b>Просроченные задания:</b>\n\n" + "\n".join(overdue_for_admins[:MAX_LIST_BUTTONS])
        for admin_id in admins:
            try:
                await bot.send_message(int(admin_id), text, parse_mode="HTML")
            except (TelegramAPIError, ValueError) as e:
                logger.warning(f"course_tracker: notify pass failed for admin {admin_id}: {e}")


async def _notify_loop(config: CourseTrackerConfig, bot: Bot) -> None:
    """Background reminder loop — sleeps first so it doesn't fire immediately
    on every restart, then re-computes and re-sends every NOTIFY_INTERVAL_SECONDS
    (see the DESIGN NOTE above init_db for why this is not de-duplicated)."""
    while True:
        try:
            await asyncio.sleep(NOTIFY_INTERVAL_SECONDS)
            await _run_notify_pass(config, bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("course_tracker: notify loop iteration failed")


# ── MAIN ──────────────────────────────────────────────────────────────────────

async def main():
    config = config_from_env()
    bot = Bot(token=os.getenv("BOT_TOKEN"))
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(ConfigMiddleware(config))
    dp.include_router(router)
    await bot.set_my_description(BOT_DESCRIPTION)
    await init_db(config.db_path)
    asyncio.create_task(_notify_loop(config, bot))
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
