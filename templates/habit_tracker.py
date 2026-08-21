# TEMPLATE: habit_tracker
# USE FOR: персональный трекер привычек — ежедневные отметки выполнено/не выполнено по своим привычкам, счётчик серии (streak) подряд идущих дней, опциональный "прощающий" режим (один пропущенный день не сбрасывает серию) против строгого режима
# CUSTOMIZE: sections marked with # CUSTOMIZE
from __future__ import annotations

import asyncio
import datetime
import html
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

import aiosqlite
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

from db.database import add_bot_admin, remove_bot_admin

# ── CUSTOMIZE ────────────────────────────────────────────────────────────────
BOT_DESCRIPTION = "Персональный трекер привычек: ежедневные отметки, серии (streak), прощающий/строгий режим, еженедельная сводка."
WELCOME_TEXT = (
    "🌱 <b>Трекер привычек</b>\n\n"
    "Каждый пользователь ведёт СВОЙ собственный список привычек — данные "
    "других людей вам не видны.\n\nВыберите действие:"
)
# ── END CUSTOMIZE ─────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
router = Router()

# ── design note ──────────────────────────────────────────────────────────────
# This template is deliberately NOT admin/client shaped like every other
# template in this repo (no shop-staff vs customer relationship). Any user who
# messages the bot manages their OWN separate list of habits, scoped by
# owner_user_id within this bot's single db_path — a family or small team can
# share one bot instance and each person's data stays private to them. So the
# core flow (add/checkin/view/archive) is intentionally NOT gated behind
# _is_admin — see the spec that produced this file. The project's standard
# config.admins_file-based admin check still exists (for consistency with
# every other template's admin-management boilerplate), and gets exactly one
# genuine use here: "📊 Сводка по всем" — an oversight view for a bot run by
# e.g. a coach or family organizer who wants to see everyone's streaks.
#
# Streak computation (the trickiest part): walk backward day-by-day starting
# from YESTERDAY if today has no check-in row yet (a still-open "today"
# shouldn't count against or for the streak until the user actually marks it
# one way or the other), or from TODAY if today already has a row. A day with
# done=1 extends the streak. A day with done=0 (explicit "not done") ALWAYS
# breaks the streak, strict or forgiving. A day with NO row at all (skipped):
# in strict mode this breaks the streak exactly like an explicit 0; in
# forgiving mode a SINGLE isolated gap is tolerated (walk continues past it
# without incrementing the streak for that day), but a SECOND unmarked day
# immediately adjacent (before another done=1 is seen) breaks the streak even
# in forgiving mode — only one isolated gap is forgiven, not an indefinite
# run of gaps. The "one gap already used" flag resets back to available the
# moment we hit another done=1, so multiple SEPARATED single-day gaps across
# a long history each get forgiven independently; it's only two-in-a-row that
# breaks it.
#
# Best/longest streak is computed by a single forward scan over the FULL
# check-in history (oldest to newest) applying the same run-breaking rules,
# tracking the max run length seen — not maintained incrementally column-wise,
# so it stays correct after any break-then-rebuild sequence without extra
# bookkeeping columns. Both streaks are recomputed on read (habit lists are
# small per user; O(days-of-history) per habit is cheap and avoids any
# incremental-update bug class entirely).


# ── config ───────────────────────────────────────────────────────────────────

@dataclass
class HabitTrackerConfig:
    bot_name: str
    db_path: str
    admins_file: Path
    welcome_image: Path
    display_name: str | None = None
    group_chat_id: str | None = None
    bot_id: int | None = None
    owner_telegram_id: int | None = None


def _paths_for(name: str, data_dir: Path) -> HabitTrackerConfig:
    return HabitTrackerConfig(
        bot_name=name,
        db_path=str(data_dir / f"{name}_data.db"),
        admins_file=data_dir / f"admins_{name}.json",
        welcome_image=data_dir / "bot_images" / f"{name}.jpg",
    )


def config_from_env() -> HabitTrackerConfig:
    """Standalone/subprocess mode."""
    name = Path(__file__).stem
    data_dir = Path(os.getenv("DATA_DIR", "./data"))
    data_dir.mkdir(exist_ok=True)
    return _paths_for(name, data_dir)


def config_from_bot_row(bot_row: dict, data_dir: Path) -> HabitTrackerConfig:
    """Webhook runtime mode. Paths built from bot_row["bot_id"] (bots.id, the
    physically unique AUTOINCREMENT PK) — NOT bot_row["name"] — same reasoning
    as every other template's config_from_bot_row."""
    bot_id = bot_row["bot_id"]
    config = HabitTrackerConfig(
        bot_name=bot_row["name"],
        db_path=str(data_dir / f"bot_{bot_id}_data.db"),
        admins_file=data_dir / f"admins_{bot_id}.json",
        welcome_image=data_dir / "bot_images" / f"bot_{bot_id}.jpg",
    )
    config.display_name = bot_row.get("display_name")
    config.group_chat_id = bot_row.get("group_chat_id")
    config.bot_id = bot_id
    config.owner_telegram_id = bot_row.get("owner_telegram_id")
    return config


class ConfigMiddleware(BaseMiddleware):
    """Injects this bot's HabitTrackerConfig into data["config"]."""

    def __init__(self, config: HabitTrackerConfig) -> None:
        self.config = config
        super().__init__()

    async def __call__(self, handler, event, data):
        data["config"] = self.config
        return await handler(event, data)


# ── admin helpers ─────────────────────────────────────────────────────────────

def _load_admins(admins_file: Path) -> set:
    try:
        return set(json.loads(admins_file.read_text()).get("ids", []))
    except Exception:
        return set()

def _save_admins(admins_file: Path, ids: set) -> None:
    admins_file.write_text(json.dumps({"ids": list(ids)}, ensure_ascii=False))

def _is_admin(user_id: int, config: HabitTrackerConfig) -> bool:
    # The DB-known owner (bots.owner_telegram_id) is always an admin, even if
    # the local admins_file is empty/stale/hijacked — see cmd_start below for
    # why the file alone can't be trusted as the sole source of truth.
    if config.owner_telegram_id is not None and str(user_id) == str(config.owner_telegram_id):
        return True
    return str(user_id) in _load_admins(config.admins_file)


def _esc(value, max_len: int = 500) -> str:
    """HTML-escapes AND length-bounds any user-supplied text before it goes into
    a parse_mode="HTML" message — same helper/rationale as templates/
    vehicle_service.py's _esc()."""
    text = str(value) if value is not None else ""
    if len(text) > max_len:
        text = text[:max_len] + "…"
    return html.escape(text)


def _join_bounded(lines: list[str], limit: int = 3500) -> str:
    """Joins lines with a length budget, dropping only WHOLE trailing lines —
    same rationale as templates/inventory.py's _join_bounded()."""
    out: list[str] = []
    total = 0
    for line in lines:
        if total + len(line) + 1 > limit:
            out.append("…")
            break
        out.append(line)
        total += len(line) + 1
    return "\n".join(out)


# ── date helpers ─────────────────────────────────────────────────────────────

def _today() -> datetime.date:
    # Server-local date, same convention as datetime('now','localtime') used
    # throughout every other template's timestamp columns.
    return datetime.datetime.now().date()

def _date_str(d: datetime.date) -> str:
    return d.isoformat()


# ── db ────────────────────────────────────────────────────────────────────────

async def init_db(db_path: str):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS habits (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_user_id    INTEGER NOT NULL,
                name             TEXT NOT NULL,
                forgiving_mode   INTEGER NOT NULL DEFAULT 0,
                is_active        INTEGER NOT NULL DEFAULT 1,
                created_at       TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS habit_checkins (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                habit_id      INTEGER NOT NULL,
                checkin_date  TEXT NOT NULL,
                done          INTEGER NOT NULL,
                created_at    TEXT DEFAULT (datetime('now','localtime')),
                UNIQUE(habit_id, checkin_date)
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_habits_owner ON habits(owner_user_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_checkins_habit ON habit_checkins(habit_id)")
        await db.commit()


# Every user manages their OWN habits (see "design note" above) — there is no
# admin/client split for creation, so neither resource is creatable here. The
# mini-app mirrors the one legitimate owner-facing view this template already
# has via Telegram ("📊 Сводка по всем"): a read-only cross-user browse of
# habits/check-ins, owner_user_id included so each row's owner is visible.
miniapp_config = {
    "resources": [
        {
            "name": "habits",
            "table": "habits",
            "order_by": "owner_user_id, id",
            "creatable": False,
            "scope": {"type": "global"},
            "title": "Привычки",
            "titleField": "name",
            "fields": [
                {"name": "owner_user_id", "label": "Владелец", "kind": "number", "list": True, "detail": True, "create": False},
                {"name": "name", "label": "Название", "kind": "text", "list": True, "detail": True, "create": False},
                {"name": "forgiving_mode", "label": "Режим", "kind": "status", "list": False, "detail": True, "create": False},
                {"name": "is_active", "label": "Активна", "kind": "status", "list": True, "detail": True, "create": False},
            ],
            # Ownership-only role_filter (docs/MINIAPP_ROLE_SCOPING_DESIGN.md
            # "Ownership-only filters") — every user of this bot owns their
            # own habits directly via owner_user_id, there is no separate
            # roles table to resolve against. Without this every viewer of
            # /app/{bot_id} could see every other user's habits.
            "role_filter": {"where": "owner_user_id = :telegram_user_id"},
        },
        {
            "name": "habit_checkins",
            "table": "habit_checkins",
            "order_by": "checkin_date DESC",
            "creatable": False,
            "scope": {"type": "scoped", "parent": "habits", "via": "habit_id"},
            "title": "Отметки",
            "titleField": "checkin_date",
            "fields": [
                {"name": "habit_id", "label": "ID привычки", "kind": "number", "list": True, "detail": True, "create": False},
                {"name": "checkin_date", "label": "Дата", "kind": "date", "list": True, "detail": True, "create": False},
                {"name": "done", "label": "Выполнено", "kind": "status", "list": True, "detail": True, "create": False},
            ],
            # Checkins have no owner column directly — scope through the
            # parent habit's owner, same join-through pattern as
            # team_manager's reports/attachments resources.
            "role_filter": {
                "where": "habit_id IN (SELECT id FROM habits WHERE owner_user_id = :telegram_user_id)"
            },
        },
    ],
}


# ── streak logic ─────────────────────────────────────────────────────────────

async def _checkin_map(db: aiosqlite.Connection, habit_id: int) -> dict[str, int]:
    rows = await (await db.execute(
        "SELECT checkin_date, done FROM habit_checkins WHERE habit_id=? ORDER BY checkin_date", (habit_id,)
    )).fetchall()
    return {r[0]: r[1] for r in rows}


def _current_streak(checkins: dict[str, int], forgiving: bool, today: datetime.date) -> int:
    """Walk backward from the most recent day that should count. If today has
    no row yet, start from yesterday instead — a still-open "today" must not
    count against (or for) the streak until the user actually marks it."""
    start = today if _date_str(today) in checkins else today - datetime.timedelta(days=1)

    streak = 0
    gap_used = False
    d = start
    while True:
        key = _date_str(d)
        if key in checkins:
            done = checkins[key]
            if done == 1:
                streak += 1
                d -= datetime.timedelta(days=1)
                continue
            else:
                # explicit "not done" ALWAYS breaks the streak, both modes
                break
        else:
            # unmarked/skipped day
            if forgiving and not gap_used:
                gap_used = True
                d -= datetime.timedelta(days=1)
                continue
            break
    return streak


def _best_streak(checkins: dict[str, int], forgiving: bool) -> int:
    """Forward scan over full history applying the same run-breaking rules,
    tracking the max run length seen. Correct after any break-then-rebuild
    sequence since it's recomputed from scratch every time, not maintained
    incrementally."""
    if not checkins:
        return 0
    dates = sorted(checkins.keys())
    first = datetime.date.fromisoformat(dates[0])
    last = datetime.date.fromisoformat(dates[-1])

    best = 0
    current = 0
    gap_used = False
    d = first
    while d <= last:
        key = _date_str(d)
        if key in checkins:
            done = checkins[key]
            if done == 1:
                current += 1
                gap_used = False
                best = max(best, current)
            else:
                current = 0
                gap_used = False
        else:
            if forgiving and not gap_used and current > 0:
                # tolerate a single isolated gap mid-run without breaking it
                gap_used = True
            else:
                current = 0
                gap_used = False
        d += datetime.timedelta(days=1)
    return best


async def _habit_streaks(db: aiosqlite.Connection, habit_id: int, forgiving: bool, today: datetime.date) -> tuple[int, int]:
    checkins = await _checkin_map(db, habit_id)
    return _current_streak(checkins, forgiving, today), _best_streak(checkins, forgiving)


# ── FSM staleness guard ─────────────────────────────────────────────────────────
FLOW_TIMEOUT_SECONDS = 300


def _flow_expired(data: dict) -> bool:
    started_at = data.get("started_at")
    return started_at is None or (time.time() - started_at) > FLOW_TIMEOUT_SECONDS


# ── FSM states ───────────────────────────────────────────────────────────────

class HabitFlow(StatesGroup):
    name = State()
    forgiving = State()

class AdminMgmtFlow(StatesGroup):
    add_admin = State()
    remove_admin_pick = State()


def _valid_admin_id(text: str) -> bool:
    if not (bool(text) and text.isascii() and text.isdigit() and len(text) <= 15):
        return False
    return int(text) > 0 and str(int(text)) == text


MAX_HABIT_NAME_LEN = 100
MAX_LIST_BUTTONS = 25
MAX_ADMIN_REMOVE_BUTTONS = 30
HISTORY_DAYS = 7
DAY_ICONS = {1: "✅", 0: "❌"}  # unmarked falls back to "⚪" at render time


# ── keyboards ─────────────────────────────────────────────────────────────────

def kb_main_menu() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="✅ Отметить сегодня", callback_data="checkin_today")],
        [InlineKeyboardButton(text="➕ Добавить привычку", callback_data="habit_new")],
        [InlineKeyboardButton(text="📊 Мои привычки", callback_data="habit_list")],
        [InlineKeyboardButton(text="📅 Недельная сводка", callback_data="week_summary")],
        [InlineKeyboardButton(text="👥 Админы", callback_data="adm_menu")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_main_menu_admin() -> InlineKeyboardMarkup:
    rows = kb_main_menu().inline_keyboard[:-1] + [
        [InlineKeyboardButton(text="📊 Сводка по всем", callback_data="admin_all_summary")],
        [InlineKeyboardButton(text="👥 Админы", callback_data="adm_menu")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_back(callback_data: str = "main_menu") -> InlineKeyboardButton:
    return InlineKeyboardButton(text="◀️ Назад", callback_data=callback_data)

def kb_flow_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="flow_cancel")],
    ])

def kb_forgiving_choice() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🕊 Прощающий режим", callback_data="habit_forgiving:1")],
        [InlineKeyboardButton(text="🎯 Строгий режим", callback_data="habit_forgiving:0")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="flow_cancel")],
    ])

def kb_checkin_list(rows: list[tuple], today_map: dict[int, int]) -> InlineKeyboardMarkup:
    btns = []
    for hid, name in rows[:MAX_LIST_BUTTONS]:
        mark = today_map.get(hid)
        icon = DAY_ICONS.get(mark, "⚪")
        btns.append([InlineKeyboardButton(text=f"{icon} {_esc(name, 40)}", callback_data=f"checkin_habit:{hid}")])
    btns.append([kb_back()])
    return InlineKeyboardMarkup(inline_keyboard=btns)

def kb_checkin_toggle(habit_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Выполнено", callback_data=f"checkin_set:{habit_id}:1"),
            InlineKeyboardButton(text="❌ Не выполнено", callback_data=f"checkin_set:{habit_id}:0"),
        ],
        [kb_back("checkin_today")],
    ])

def kb_habit_list(rows: list[tuple]) -> InlineKeyboardMarkup:
    btns = [
        [InlineKeyboardButton(text=f"{_esc(name, 40)}", callback_data=f"habit_view:{hid}")]
        for hid, name in rows[:MAX_LIST_BUTTONS]
    ]
    btns.append([kb_back()])
    return InlineKeyboardMarkup(inline_keyboard=btns)

def kb_habit_detail(habit_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Архивировать", callback_data=f"habit_archive:{habit_id}")],
        [kb_back("habit_list")],
    ])

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
async def cmd_start(message: Message, state: FSMContext, config: HabitTrackerConfig):
    await state.clear()
    admins = _load_admins(config.admins_file)
    sender_id = message.from_user.id
    # Bug fixed here: this used to grant admin to whoever sent /start FIRST,
    # which lets any client who messages the bot before the owner does
    # permanently seize the admin panel. When bots.owner_telegram_id is known
    # (webhook/production mode), only that user may claim the empty-admins
    # bootstrap slot; a non-owner sending /start first now just gets the
    # regular menu. In standalone/env mode (owner_telegram_id unknown) the
    # old first-comer behavior is kept as the only option available.
    is_owner = config.owner_telegram_id is not None and sender_id == config.owner_telegram_id
    first_time_admin = not admins and (is_owner or config.owner_telegram_id is None)
    if first_time_admin:
        _save_admins(config.admins_file, {str(sender_id)})
        admins = {str(sender_id)}
        if config.bot_id is not None:
            try:
                await add_bot_admin(config.bot_id, str(sender_id))
            except Exception as e:
                logger.warning(f"cmd_start: add_bot_admin sync failed for bot {config.bot_id}: {e}")

    is_admin = _is_admin(sender_id, config)
    menu = kb_main_menu_admin() if is_admin else kb_main_menu()
    if config.welcome_image.exists():
        await message.answer_photo(
            FSInputFile(str(config.welcome_image)), caption=WELCOME_TEXT,
            parse_mode="HTML", reply_markup=menu,
        )
    else:
        await message.answer(WELCOME_TEXT, parse_mode="HTML", reply_markup=menu)
    if first_time_admin:
        await message.answer(
            "👑 <b>Вы — администратор этого бота.</b>\n\n"
            "Это даёт доступ только к «📊 Сводка по всем» и управлению "
            "админами — ваши собственные привычки работают как у любого "
            "другого пользователя.", parse_mode="HTML",
        )


def _menu_for(user_id: int, config: HabitTrackerConfig) -> InlineKeyboardMarkup:
    return kb_main_menu_admin() if _is_admin(user_id, config) else kb_main_menu()


@router.callback_query(F.data == "main_menu")
async def cb_main_menu(cb: CallbackQuery, state: FSMContext, config: HabitTrackerConfig):
    await cb.answer()
    await state.clear()
    await cb.message.edit_text(WELCOME_TEXT, parse_mode="HTML", reply_markup=_menu_for(cb.from_user.id, config))


@router.callback_query(F.data == "flow_cancel")
async def cb_flow_cancel(cb: CallbackQuery, state: FSMContext, config: HabitTrackerConfig):
    await cb.answer()
    await state.clear()
    await cb.message.edit_text("Отменено.", reply_markup=_menu_for(cb.from_user.id, config))


# ── CHECK-IN today ────────────────────────────────────────────────────────────

@router.callback_query(F.data == "checkin_today")
async def cb_checkin_today(cb: CallbackQuery, state: FSMContext, config: HabitTrackerConfig):
    await cb.answer()
    await state.clear()
    owner = cb.from_user.id
    today = _date_str(_today())
    async with aiosqlite.connect(config.db_path) as db:
        habits = await (await db.execute(
            "SELECT id, name FROM habits WHERE owner_user_id=? AND is_active=1 ORDER BY id", (owner,)
        )).fetchall()
        today_rows = await (await db.execute(
            "SELECT h.id, c.done FROM habits h LEFT JOIN habit_checkins c "
            "ON c.habit_id=h.id AND c.checkin_date=? WHERE h.owner_user_id=? AND h.is_active=1",
            (today, owner),
        )).fetchall()
    today_map = {hid: done for hid, done in today_rows if done is not None}
    if not habits:
        await cb.message.edit_text(
            "У вас пока нет активных привычек. Добавьте первую!",
            reply_markup=_menu_for(owner, config),
        )
        return
    await cb.message.edit_text(
        "✅ <b>Отметить сегодня</b>\n\nВыберите привычку:", parse_mode="HTML",
        reply_markup=kb_checkin_list(habits, today_map),
    )


@router.callback_query(F.data.startswith("checkin_habit:"))
async def cb_checkin_habit(cb: CallbackQuery, config: HabitTrackerConfig):
    await cb.answer()
    try:
        habit_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    owner = cb.from_user.id
    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute(
            "SELECT name FROM habits WHERE id=? AND owner_user_id=? AND is_active=1", (habit_id, owner)
        )).fetchone()
    if not row:
        await cb.message.edit_text("Привычка не найдена.", reply_markup=_menu_for(owner, config))
        return
    await cb.message.edit_text(
        f"Отметить «{_esc(row[0])}» на сегодня:", reply_markup=kb_checkin_toggle(habit_id),
    )


@router.callback_query(F.data.startswith("checkin_set:"))
async def cb_checkin_set(cb: CallbackQuery, config: HabitTrackerConfig):
    await cb.answer()
    try:
        _, habit_id_s, done_s = cb.data.split(":", 2)
        habit_id = int(habit_id_s)
        done = int(done_s)
    except ValueError:
        return
    if done not in (0, 1):
        return
    owner = cb.from_user.id
    today = _date_str(_today())
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        habit = await (await db.execute(
            "SELECT id, name, forgiving_mode FROM habits WHERE id=? AND owner_user_id=? AND is_active=1",
            (habit_id, owner),
        )).fetchone()
        if not habit:
            await cb.message.edit_text("Привычка не найдена.", reply_markup=_menu_for(owner, config))
            return
        # Upsert: user may re-tap to change today's mark before the day ends.
        await db.execute(
            "INSERT INTO habit_checkins (habit_id, checkin_date, done) VALUES (?,?,?) "
            "ON CONFLICT(habit_id, checkin_date) DO UPDATE SET done=excluded.done",
            (habit_id, today, done),
        )
        await db.commit()
        current, best = await _habit_streaks(db, habit_id, bool(habit["forgiving_mode"]), _today())

    icon = "✅" if done == 1 else "❌"
    await cb.message.edit_text(
        f"{icon} «{_esc(habit['name'])}» отмечено на сегодня.\n\n"
        f"🔥 Текущая серия: {current}\n🏆 Лучшая серия: {best}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [kb_back("checkin_today")],
        ]),
    )


# ── ADD HABIT flow ────────────────────────────────────────────────────────────

@router.callback_query(F.data == "habit_new")
async def cb_habit_new(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await state.set_state(HabitFlow.name)
    await state.update_data(started_at=time.time())
    await cb.message.edit_text("📝 Введите название новой привычки:", reply_markup=kb_flow_cancel())


@router.message(HabitFlow.name, F.text, ~F.text.startswith("/"))
async def habit_new_name(msg: Message, state: FSMContext):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    name = msg.text.strip()
    if not name:
        await msg.answer("Название не может быть пустым. Введите название привычки:", reply_markup=kb_flow_cancel())
        return
    if len(name) > MAX_HABIT_NAME_LEN:
        await msg.answer(
            f"⚠️ Слишком длинное название (максимум {MAX_HABIT_NAME_LEN} символов). Введите короче:",
            reply_markup=kb_flow_cancel(),
        )
        return
    await state.update_data(pending_name=name)
    await state.set_state(HabitFlow.forgiving)
    await msg.answer(
        "Выберите режим:\n\n"
        "🕊 <b>Прощающий</b> — один пропущенный день не сбрасывает серию.\n"
        "🎯 <b>Строгий</b> — любой пропуск сбрасывает серию.",
        parse_mode="HTML", reply_markup=kb_forgiving_choice(),
    )


@router.callback_query(HabitFlow.forgiving, F.data.startswith("habit_forgiving:"))
async def cb_habit_forgiving(cb: CallbackQuery, state: FSMContext, config: HabitTrackerConfig):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=_menu_for(cb.from_user.id, config))
        return
    name = data.get("pending_name")
    if not name:
        await state.clear()
        await cb.message.edit_text("Сессия устарела, начните заново.", reply_markup=_menu_for(cb.from_user.id, config))
        return
    try:
        forgiving = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    if forgiving not in (0, 1):
        return
    await state.clear()
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute(
            "INSERT INTO habits (owner_user_id, name, forgiving_mode) VALUES (?,?,?)",
            (cb.from_user.id, name, forgiving),
        )
        await db.commit()
    mode_label = "🕊 прощающий" if forgiving else "🎯 строгий"
    await cb.message.edit_text(
        f"✅ Привычка «{_esc(name)}» создана. Режим: {mode_label}.",
        reply_markup=_menu_for(cb.from_user.id, config),
    )


# ── MY HABITS list ────────────────────────────────────────────────────────────

@router.callback_query(F.data == "habit_list")
async def cb_habit_list(cb: CallbackQuery, state: FSMContext, config: HabitTrackerConfig):
    await cb.answer()
    await state.clear()
    owner = cb.from_user.id
    async with aiosqlite.connect(config.db_path) as db:
        rows = await (await db.execute(
            "SELECT id, name FROM habits WHERE owner_user_id=? AND is_active=1 ORDER BY id", (owner,)
        )).fetchall()
    if not rows:
        await cb.message.edit_text("У вас пока нет активных привычек.", reply_markup=_menu_for(owner, config))
        return
    await cb.message.edit_text("📊 <b>Мои привычки</b>", parse_mode="HTML", reply_markup=kb_habit_list(rows))


async def _habit_detail_text(db_path: str, habit_id: int, owner_user_id: int) -> str | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        habit = await (await db.execute(
            "SELECT * FROM habits WHERE id=? AND owner_user_id=?", (habit_id, owner_user_id)
        )).fetchone()
        if not habit:
            return None
        current, best = await _habit_streaks(db, habit_id, bool(habit["forgiving_mode"]), _today())
        checkins = await _checkin_map(db, habit_id)

    mode_label = "🕊 прощающий" if habit["forgiving_mode"] else "🎯 строгий"
    lines = [
        f"📌 <b>{_esc(habit['name'])}</b>\n",
        f"Режим: {mode_label}",
        f"🔥 Текущая серия: {current}",
        f"🏆 Лучшая серия: {best}\n",
        f"<b>Последние {HISTORY_DAYS} дней:</b>",
    ]
    today = _today()
    for i in range(HISTORY_DAYS - 1, -1, -1):
        d = today - datetime.timedelta(days=i)
        key = _date_str(d)
        mark = checkins.get(key)
        icon = DAY_ICONS.get(mark, "⚪")
        lines.append(f"{d.strftime('%d.%m')} {icon}")
    return _join_bounded(lines)


@router.callback_query(F.data.startswith("habit_view:"))
async def cb_habit_view(cb: CallbackQuery, config: HabitTrackerConfig):
    await cb.answer()
    try:
        habit_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    text = await _habit_detail_text(config.db_path, habit_id, cb.from_user.id)
    if text is None:
        await cb.message.edit_text("Привычка не найдена.", reply_markup=_menu_for(cb.from_user.id, config))
        return
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_habit_detail(habit_id))


@router.callback_query(F.data.startswith("habit_archive:"))
async def cb_habit_archive(cb: CallbackQuery, config: HabitTrackerConfig):
    await cb.answer()
    try:
        habit_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    owner = cb.from_user.id
    async with aiosqlite.connect(config.db_path) as db:
        cur = await db.execute(
            "UPDATE habits SET is_active=0 WHERE id=? AND owner_user_id=? AND is_active=1",
            (habit_id, owner),
        )
        await db.commit()
    if cur.rowcount == 0:
        await cb.message.edit_text("Привычка не найдена или уже архивирована.", reply_markup=_menu_for(owner, config))
        return
    await cb.message.edit_text("🗑 Привычка архивирована. История сохранена.", reply_markup=_menu_for(owner, config))


# ── WEEKLY SUMMARY ────────────────────────────────────────────────────────────

@router.callback_query(F.data == "week_summary")
async def cb_week_summary(cb: CallbackQuery, state: FSMContext, config: HabitTrackerConfig):
    await cb.answer()
    await state.clear()
    owner = cb.from_user.id
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        habits = await (await db.execute(
            "SELECT id, name, forgiving_mode FROM habits WHERE owner_user_id=? AND is_active=1 ORDER BY id", (owner,)
        )).fetchall()
        if not habits:
            await cb.message.edit_text("У вас пока нет активных привычек.", reply_markup=_menu_for(owner, config))
            return
        today = _today()
        lines = ["📅 <b>Недельная сводка</b>\n"]
        for habit in habits:
            checkins = await _checkin_map(db, habit["id"])
            current, _best = await _habit_streaks(db, habit["id"], bool(habit["forgiving_mode"]), today)
            day_icons = []
            for i in range(HISTORY_DAYS - 1, -1, -1):
                d = today - datetime.timedelta(days=i)
                mark = checkins.get(_date_str(d))
                day_icons.append(DAY_ICONS.get(mark, "⚪"))
            lines.append(f"<b>{_esc(habit['name'], 40)}</b> · 🔥{current}")
            lines.append(" ".join(day_icons))
    await cb.message.edit_text(_join_bounded(lines), parse_mode="HTML", reply_markup=_menu_for(owner, config))


# ── ADMIN-ONLY: aggregate view ────────────────────────────────────────────────

@router.callback_query(F.data == "admin_all_summary")
async def cb_admin_all_summary(cb: CallbackQuery, state: FSMContext, config: HabitTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        habits = await (await db.execute(
            "SELECT id, owner_user_id, name, forgiving_mode FROM habits WHERE is_active=1 "
            "ORDER BY owner_user_id, id"
        )).fetchall()
        if not habits:
            await cb.message.edit_text("Привычек пока нет ни у кого.", reply_markup=kb_main_menu_admin())
            return
        today = _today()
        lines = ["📊 <b>Сводка по всем пользователям</b>\n"]
        current_owner = None
        for habit in habits:
            if habit["owner_user_id"] != current_owner:
                current_owner = habit["owner_user_id"]
                lines.append(f"\n👤 <code>{current_owner}</code>")
            current, best = await _habit_streaks(db, habit["id"], bool(habit["forgiving_mode"]), today)
            lines.append(f"  • {_esc(habit['name'], 40)} — 🔥{current} / 🏆{best}")
    await cb.message.edit_text(_join_bounded(lines), parse_mode="HTML", reply_markup=kb_main_menu_admin())


# ── ADMINS menu (management boilerplate, same shape as every other template) ──

async def _admins_list_text(config: HabitTrackerConfig) -> str:
    ids = sorted(_load_admins(config.admins_file))
    if not ids:
        return "👥 Пусто"
    return _join_bounded(["👥 <b>Администраторы бота:</b>\n"] + [f"• <code>{_esc(i)}</code>" for i in ids])


@router.callback_query(F.data == "adm_menu")
async def cb_adm_menu(cb: CallbackQuery, state: FSMContext, config: HabitTrackerConfig):
    await cb.answer()
    await state.clear()
    text = await _admins_list_text(config)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_admins_menu())


@router.callback_query(F.data == "adm_add")
async def cb_adm_add(cb: CallbackQuery, state: FSMContext, config: HabitTrackerConfig):
    await cb.answer()
    await state.set_state(AdminMgmtFlow.add_admin)
    await state.update_data(started_at=time.time())
    await cb.message.edit_text("Введите Telegram ID нового администратора:", reply_markup=kb_flow_cancel())


@router.message(AdminMgmtFlow.add_admin, F.text, ~F.text.startswith("/"))
async def admin_add_id(msg: Message, state: FSMContext, config: HabitTrackerConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
        return
    text = msg.text.strip()
    if not _valid_admin_id(text):
        await msg.answer("Некорректный ID. Введите числовой Telegram ID.", reply_markup=kb_flow_cancel())
        return
    await state.clear()
    ids = _load_admins(config.admins_file)
    ids.add(text)
    _save_admins(config.admins_file, ids)
    if config.bot_id is not None:
        try:
            await add_bot_admin(config.bot_id, text)
        except Exception as e:
            logger.warning(f"admin_add_id: add_bot_admin sync failed for bot {config.bot_id}: {e}")
    await msg.answer(f"✅ <code>{text}</code> добавлен.", parse_mode="HTML", reply_markup=kb_admins_menu())


@router.callback_query(F.data == "adm_remove")
async def cb_adm_remove(cb: CallbackQuery, state: FSMContext, config: HabitTrackerConfig):
    await cb.answer()
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
async def cb_adm_remove_pick(cb: CallbackQuery, state: FSMContext, config: HabitTrackerConfig):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_main_menu())
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
    if config.bot_id is not None:
        try:
            await remove_bot_admin(config.bot_id, target)
        except Exception as e:
            logger.warning(f"cb_adm_remove_pick: remove_bot_admin sync failed for bot {config.bot_id}: {e}")
    await state.clear()
    await cb.message.edit_text(f"✅ <code>{_esc(target)}</code> удалён.", parse_mode="HTML",
                                reply_markup=kb_admins_menu())


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
