# TEMPLATE: booking_fitness
# USE FOR: фитнес-клубы, тренажёрные залы, студии групповых занятий — запись на групповые тренировки (лист ожидания, абонемент) и отдельно на индивидуальные тренировки с тренером
# CUSTOMIZE: sections marked with # CUSTOMIZE
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import aiosqlite
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, Message, ReplyKeyboardMarkup,
)

# ── CUSTOMIZE ────────────────────────────────────────────────────────────────
# Same status as booking_beauty.py/booking_medical.py's CUSTOMIZE blocks — this
# section is per-file source-text customization Claude edits when generating a
# specific bot, not a per-bot runtime config. SPECIALIZATIONS is only the
# INITIAL menu of certification tags offered when an admin adds a trainer —
# trainers/schedule/plans themselves are runtime data (see schema below),
# added/removed by the bot's own admin panel, not by editing this file.
BOT_DESCRIPTION = "Запись в фитнес-клуб: групповые занятия по расписанию и индивидуальные тренировки с тренером."
WELCOME_TEXT = (
    "🏋 <b>Фитнес-клуб</b>\n\n"
    "Выберите, что вам нужно:\n"
    "📅 <b>Групповое занятие</b> — по расписанию, с лист ожидания если мест нет\n"
    "🧑 <b>Индивидуальная тренировка</b> — один на один с тренером\n\n"
    "Нажмите нужную кнопку ниже:"
)
OWNER_NOTIFY_TEXT = "🔔 <b>Новая запись!</b>\n"
SPECIALIZATIONS = ["Йога", "Кроссфит", "Единоборства", "Растяжка", "Персональный тренинг", "Плавание", "Функциональный тренинг"]
GROUP_DAYS_AHEAD = 14
INDIVIDUAL_DAYS_AHEAD = 14
INDIVIDUAL_SLOT_TIMES = ["09:00", "10:00", "11:00", "12:00", "14:00", "15:00", "16:00", "17:00", "18:00", "19:00"]
GROUP_DEFAULT_DURATION_MIN = 60
GROUP_CAPACITY_OPTIONS = [4, 6, 8, 10, 12, 15, 20]
SUBSCRIPTION_VISITS_OPTIONS = [4, 8, 12, 20]
SUBSCRIPTION_VALIDITY_OPTIONS = [30, 60, 90, 0]  # 0 = без ограничения (NULL expires_at)
LOW_VISITS_THRESHOLD = 2
EXPIRING_SOON_DAYS = 3
REMINDER_POLL_SECONDS = 6 * 3600
NAME_MAX_LEN = 100
MAX_RATING = 5
# 0 = занятие можно оценить сразу, как только его дата прошла (slot_date <
# today) — a trainer rating only makes sense for a session the client
# actually attended, so this is measured from the SESSION's own date, not
# from booking time. Kept as a knob (not hardcoded 0) in case an owner wants
# to wait a day or two before prompting.
RATING_PROMPT_DAYS_AFTER = 0
RATING_COMMENT_MAX_LEN = 300
STATS_RECENT_DAYS = 30  # window for attendance-by-trainer / churn heuristic in 📊 Статистика
# ── END CUSTOMIZE ─────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
router = Router()

DAYS_RU = {0: "Пн", 1: "Вт", 2: "Ср", 3: "Чт", 4: "Пт", 5: "Сб", 6: "Вс"}
MONTHS_RU = {1: "янв", 2: "фев", 3: "мар", 4: "апр", 5: "май", 6: "июн",
             7: "июл", 8: "авг", 9: "сен", 10: "окт", 11: "ноя", 12: "дек"}


def _date_label(iso_date: str) -> str:
    d = datetime.strptime(iso_date, "%Y-%m-%d")
    return f"{DAYS_RU[d.weekday()]} {d.day} {MONTHS_RU[d.month]}"


# ── config ───────────────────────────────────────────────────────────────────
# Same shape/contract as templates/booking_beauty.py's BookingBeautyConfig —
# see docs/STAGE2_DESIGN.md "Config-контракт шаблона". display_name/
# group_chat_id kept for form parity, not read anywhere in this file.

@dataclass
class BookingFitnessConfig:
    bot_name: str
    db_path: str
    admins_file: Path
    welcome_image: Path
    display_name: str | None = None
    group_chat_id: str | None = None


def _paths_for(name: str, data_dir: Path) -> BookingFitnessConfig:
    return BookingFitnessConfig(
        bot_name=name,
        db_path=str(data_dir / f"{name}_data.db"),
        admins_file=data_dir / f"admins_{name}.json",
        welcome_image=data_dir / "bot_images" / f"{name}.jpg",
    )


def config_from_env() -> BookingFitnessConfig:
    """Standalone/subprocess mode — same reasoning as booking_beauty.py's
    config_from_env()."""
    name = Path(__file__).stem
    data_dir = Path(os.getenv("DATA_DIR", "./data"))
    data_dir.mkdir(exist_ok=True)
    return _paths_for(name, data_dir)


def config_from_bot_row(bot_row: dict, data_dir: Path) -> BookingFitnessConfig:
    """Webhook runtime mode. Paths keyed by bot_row["bot_id"] (bots.id, the
    physically unique AUTOINCREMENT PK) — NOT bot_row["name"], which has no
    UNIQUE constraint. Same reasoning as booking_beauty.py's
    config_from_bot_row."""
    bot_id = bot_row["bot_id"]
    config = BookingFitnessConfig(
        bot_name=bot_row["name"],
        db_path=str(data_dir / f"bot_{bot_id}_data.db"),
        admins_file=data_dir / f"admins_{bot_id}.json",
        welcome_image=data_dir / "bot_images" / f"bot_{bot_id}.jpg",
    )
    config.display_name = bot_row.get("display_name")
    config.group_chat_id = bot_row.get("group_chat_id")
    return config


class ConfigMiddleware(BaseMiddleware):
    """Injects this bot's BookingFitnessConfig into data["config"]. Defined
    here (not imported from runtime/) to keep the template self-contained."""

    def __init__(self, config: BookingFitnessConfig) -> None:
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

def _is_bot_admin(user_id: int, config: BookingFitnessConfig) -> bool:
    return str(user_id) in _load_admins(config.admins_file)

def _esc(value, max_len: int = 500) -> str:
    """HTML-escapes AND length-bounds any user-supplied text before it goes
    into a parse_mode="HTML" message — same helper as moderator.py/
    booking_beauty.py's _esc()."""
    text = str(value) if value is not None else ""
    if len(text) > max_len:
        text = text[:max_len] + "…"
    return html.escape(text)

def _join_bounded(lines: list[str], limit: int = 3500) -> str:
    """Joins lines with a length budget, dropping only WHOLE trailing lines —
    same approach as moderator.py's _join_bounded()."""
    out: list[str] = []
    total = 0
    for line in lines:
        if total + len(line) + 1 > limit:
            out.append("…")
            break
        out.append(line)
        total += len(line) + 1
    return "\n".join(out)

def _valid_telegram_id(text: str) -> bool:
    """Same guard as moderator.py's _valid_admin_id: bounds length before
    treating input as numeric, rejects non-ASCII look-alike digits, and
    rejects "0"/leading-zero/negative forms — no real Telegram user_id is
    ever 0, negative, or leading-zero (see moderator.py's docstring for the
    full incident history this guards against)."""
    if not (bool(text) and text.isascii() and text.isdigit() and len(text) <= 15):
        return False
    return int(text) > 0 and str(int(text)) == text


# ── phone validation (reused from booking_beauty.py) ──────────────────────────

def _normalize_phone(raw: str) -> str | None:
    digits = re.sub(r"[^\d]", "", raw)
    if len(digits) == 11 and digits[0] in ("7", "8"):
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    else:
        return None
    return f"+{digits[0]} ({digits[1:4]}) {digits[4:7]}-{digits[7:9]}-{digits[9:11]}"


# ── mini-app config (see docs/MINIAPP_DESIGN.md) ────────────────────────────
# group_schedule_templates and slots are both auto-expanded/recurring
# (_ensure_group_sessions/_ensure_individual_slots) — trainers/plans are the
# owner-editable catalogs (🏋 Тренеры / 🎟 Тарифы абонционов admin panels),
# bookings/subscriptions are the resulting client records. bot_settings is a
# single toggle row, not a list of records — no resource for it.
#
# role_filter DELIBERATELY NOT added to bookings/subscriptions — read
# docs/MINIAPP_ROLE_SCOPING_DESIGN.md's "Auth-layer admin gate" section
# before changing this. _admin_gate_ok() in runtime/miniapp_api.py exempts
# ANY resource that declares role_filter from the blanket bot-admin check —
# and the ownership-only shape's `where` then applies UNCONDITIONALLY to
# EVERY authenticated viewer, admin included (there's no "admin sees
# everything" carve-out in _apply_role_filter; it isn't told who the admin
# is). This template has no roles TABLE at all — admins live in admins_file
# (a JSON file), which a SQL `where` predicate cannot see — so the
# role-resolved shape is not applicable either. Concretely: adding
# {"where": "user_id = :telegram_user_id"} here would make the bot owner's
# own /app/{bot_id} resource-editor show only bookings/subscriptions rows
# where user_id happens to equal the OWNER's own Telegram id (almost always
# none) — a straight regression of the existing owner-oversight use case,
# to buy a client-side view that already exists another way (see below).
# Net effect of leaving these two resources filter-less: unchanged from
# before this change — visible via /app/{bot_id} ONLY to bot admins (owner
# oversight, unaffected), NOT visible to a regular client through the
# mini-app. That's an acceptable trade, not a gap: a client's own
# records are already fully readable via the bot's own Telegram buttons —
# "📋 Мои записи" and "💳 Мой абонемент" — which query `WHERE user_id = ?`
# bound to the requesting Telegram user directly, no mini-app auth layer
# involved. If a future engine change lets _admin_gate_ok distinguish
# "this specific viewer is a bot admin" from "this resource has ANY
# role_filter", revisit this decision.
miniapp_config = {
    "resources": [
        {
            "name": "trainers",
            "table": "trainers",
            "order_by": "id DESC",
            "creatable": True,
            "title": "Тренеры",
            "titleField": "name",
            "fields": [
                {"name": "name", "required": True, "label": "Имя", "kind": "text", "list": True, "detail": True, "create": True},
                {"name": "specializations", "label": "Специализации", "kind": "text", "list": True, "detail": True, "create": True},
                {"name": "active", "label": "Активен", "kind": "status", "list": True, "detail": True, "create": False},
            ],
        },
        {
            "name": "group_schedule_templates",
            "table": "group_schedule_templates",
            "order_by": "day_of_week ASC, slot_time ASC",
            "creatable": True,
            "title": "Групповое расписание",
            "titleField": "class_name",
            "fields": [
                {"name": "day_of_week", "required": True, "label": "День недели", "kind": "number", "list": True, "detail": True, "create": True},
                {"name": "slot_time", "required": True, "label": "Время", "kind": "text", "list": True, "detail": True, "create": True},
                {"name": "trainer_id", "label": "ID тренера", "kind": "number", "list": False, "detail": True, "create": True},
                {"name": "class_name", "required": True, "label": "Занятие", "kind": "text", "list": True, "detail": True, "create": True},
                {"name": "capacity", "required": True, "label": "Вместимость", "kind": "number", "list": True, "detail": True, "create": True},
                {"name": "duration_min", "label": "Длительность (мин)", "kind": "number", "list": False, "detail": True, "create": True},
                {"name": "active", "label": "Активен", "kind": "status", "list": True, "detail": True, "create": False},
            ],
        },
        {
            "name": "slots",
            "table": "slots",
            "order_by": "slot_date DESC, slot_time DESC",
            "creatable": False,
            "title": "Слоты",
            "titleField": "class_name",
            "fields": [
                {"name": "session_type", "label": "Тип", "kind": "status", "list": True, "detail": True, "create": False},
                {"name": "trainer_id", "label": "ID тренера", "kind": "number", "list": False, "detail": True, "create": False},
                {"name": "slot_date", "label": "Дата", "kind": "date", "list": True, "detail": True, "create": False},
                {"name": "slot_time", "label": "Время", "kind": "text", "list": True, "detail": True, "create": False},
                {"name": "duration_min", "label": "Длительность (мин)", "kind": "number", "list": False, "detail": True, "create": False},
                {"name": "class_name", "label": "Занятие", "kind": "text", "list": True, "detail": True, "create": False},
                {"name": "capacity", "label": "Вместимость", "kind": "number", "list": False, "detail": True, "create": False},
                {"name": "status", "label": "Статус", "kind": "status", "list": True, "detail": True, "create": False},
            ],
        },
        {
            "name": "bookings",
            "table": "bookings",
            "order_by": "created_at DESC",
            "creatable": False,
            "title": "Записи",
            "titleField": "client_name",
            "fields": [
                {"name": "slot_id", "label": "ID слота", "kind": "number", "list": False, "detail": True, "create": False},
                {"name": "client_name", "label": "Имя клиента", "kind": "text", "list": True, "detail": True, "create": False},
                {"name": "client_phone", "label": "Телефон", "kind": "text", "list": False, "detail": True, "create": False},
                {"name": "status", "label": "Статус", "kind": "status", "list": True, "detail": True, "create": False},
                {"name": "subscription_id", "label": "ID абонемента", "kind": "number", "list": False, "detail": True, "create": False},
                {"name": "created_at", "label": "Создано", "kind": "date", "list": True, "detail": True, "create": False},
            ],
        },
        {
            "name": "subscription_plans",
            "table": "subscription_plans",
            "order_by": "id DESC",
            "creatable": True,
            "title": "Тарифы абонементов",
            "titleField": "name",
            "fields": [
                {"name": "name", "required": True, "label": "Название", "kind": "text", "list": True, "detail": True, "create": True},
                {"name": "total_visits", "required": True, "label": "Посещений", "kind": "number", "list": True, "detail": True, "create": True},
                {"name": "validity_days", "label": "Срок действия (дн)", "kind": "number", "list": True, "detail": True, "create": True},
                {"name": "active", "label": "Активен", "kind": "status", "list": True, "detail": True, "create": False},
            ],
        },
        {
            "name": "subscriptions",
            "table": "subscriptions",
            "order_by": "purchased_at DESC",
            "creatable": True,
            "title": "Абонементы клиентов",
            "titleField": "plan_name",
            "fields": [
                {"name": "user_id", "required": True, "label": "ID клиента", "kind": "number", "list": True, "detail": True, "create": True},
                {"name": "plan_name", "required": True, "label": "Тариф", "kind": "text", "list": True, "detail": True, "create": True},
                {"name": "total_visits", "required": True, "label": "Всего посещений", "kind": "number", "list": False, "detail": True, "create": True},
                {"name": "visits_left", "required": True, "label": "Осталось посещений", "kind": "number", "list": True, "detail": True, "create": True},
                {"name": "purchased_at", "label": "Куплен", "kind": "date", "list": False, "detail": True, "create": False},
                {"name": "expires_at", "label": "Истекает", "kind": "date", "list": True, "detail": True, "create": True},
            ],
        },
        {
            # Operational resource, same posture as bookings/slots above:
            # owner-only oversight via the admin gate, no per-client
            # role_filter for the same reason spelled out in the comment
            # above — clients see/leave their own ratings entirely through
            # Telegram (⭐ Оценить тренера), never through /app/{bot_id}.
            "name": "trainer_ratings",
            "table": "trainer_ratings",
            "order_by": "created_at DESC",
            "creatable": False,
            "title": "Оценки тренеров",
            "titleField": "comment",
            "fields": [
                {"name": "trainer_id", "label": "ID тренера", "kind": "number", "list": True, "detail": True, "create": False},
                {"name": "user_id", "label": "ID клиента", "kind": "number", "list": False, "detail": True, "create": False},
                {"name": "booking_id", "label": "ID записи", "kind": "number", "list": False, "detail": True, "create": False},
                {"name": "rating", "label": "Оценка", "kind": "number", "list": True, "detail": True, "create": False},
                {"name": "comment", "label": "Комментарий", "kind": "text", "list": True, "detail": True, "create": False},
                {"name": "created_at", "label": "Создано", "kind": "date", "list": True, "detail": True, "create": False},
            ],
        },
    ],
}


# ── db ────────────────────────────────────────────────────────────────────────

async def init_db(db_path: str):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS trainers (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                name           TEXT NOT NULL,
                specializations TEXT DEFAULT '',
                active         INTEGER DEFAULT 1
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS group_schedule_templates (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                day_of_week   INTEGER NOT NULL,
                slot_time     TEXT NOT NULL,
                trainer_id    INTEGER,
                class_name    TEXT NOT NULL,
                capacity      INTEGER NOT NULL,
                duration_min  INTEGER DEFAULT 60,
                active        INTEGER DEFAULT 1
            )
        """)
        # SHARED slot engine table — session_type is the EXPLICIT branch
        # discriminator ('group' | 'individual'). capacity is data, never the
        # thing branch logic decides on: the two menu branches choose which
        # rows to query by session_type, not by inferring "capacity==1 means
        # individual". UNIQUE(template_id, slot_date) makes group-session
        # regeneration idempotent (see _ensure_group_sessions); it's a no-op
        # for individual rows since SQLite treats every NULL template_id as
        # distinct in a UNIQUE index, so individual idempotency is handled
        # separately (a coarse per trainer+date guard, see
        # _ensure_individual_slots — same style as booking_beauty.py's
        # _ensure_slots).
        await db.execute("""
            CREATE TABLE IF NOT EXISTS slots (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                session_type  TEXT NOT NULL,
                template_id   INTEGER,
                trainer_id    INTEGER,
                slot_date     TEXT NOT NULL,
                slot_time     TEXT NOT NULL,
                duration_min  INTEGER DEFAULT 60,
                class_name    TEXT,
                capacity      INTEGER NOT NULL DEFAULT 1,
                status        TEXT DEFAULT 'active',
                UNIQUE(template_id, slot_date)
            )
        """)
        # status: 'confirmed' | 'waitlist'. subscription_id records exactly
        # which subscription a CONFIRMED booking consumed a visit from (NULL
        # if this bot's settings don't require one) — needed so cancelling
        # refunds the right subscription, not just "the user's current one",
        # which could have changed between booking and cancel.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bookings (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                slot_id         INTEGER NOT NULL,
                user_id         INTEGER NOT NULL,
                client_name     TEXT,
                client_phone    TEXT,
                status          TEXT NOT NULL,
                subscription_id INTEGER,
                created_at      TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS subscription_plans (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                name          TEXT NOT NULL,
                total_visits  INTEGER NOT NULL,
                validity_days INTEGER,
                active        INTEGER DEFAULT 1
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id              INTEGER NOT NULL,
                plan_name            TEXT NOT NULL,
                total_visits         INTEGER NOT NULL,
                visits_left          INTEGER NOT NULL,
                purchased_at         TEXT DEFAULT (datetime('now','localtime')),
                expires_at           TEXT,
                low_balance_notified INTEGER DEFAULT 0,
                expiry_notified      INTEGER DEFAULT 0,
                autorenew            INTEGER NOT NULL DEFAULT 0,
                autorenew_done       INTEGER NOT NULL DEFAULT 0
            )
        """)
        # Same try/except ALTER guard as booking_beauty.py's bookings.user_id
        # backfill — CREATE TABLE IF NOT EXISTS above is a no-op against a
        # subscriptions table created by an already-running bot BEFORE this
        # change shipped, so these two columns need a separate migration
        # path for existing installs. "duplicate column" on a fresh/already-
        # migrated DB is expected and silently ignored.
        for ddl in (
            "ALTER TABLE subscriptions ADD COLUMN autorenew INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE subscriptions ADD COLUMN autorenew_done INTEGER NOT NULL DEFAULT 0",
        ):
            try:
                await db.execute(ddl)
            except Exception:
                pass
        # One rating per booking (UNIQUE(booking_id)) — a client can only
        # attend/be-charged for a given session once, so more than one
        # rating against the same booking_id would just be the same
        # double-tap race _book_slot already guards against elsewhere,
        # replayed here. INSERT OR IGNORE at write time (see _record_rating)
        # makes that race a no-op instead of a constraint-error 500.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS trainer_ratings (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                trainer_id  INTEGER NOT NULL,
                user_id     INTEGER NOT NULL,
                booking_id  INTEGER NOT NULL UNIQUE,
                rating      INTEGER NOT NULL,
                comment     TEXT,
                created_at  TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        # Single-row settings table — see runtime toggles in the admin panel
        # ("⚙️ Настройки"). These are PER-BOT business choices (different gyms
        # run differently), never hardcoded into booking logic — every
        # booking/cancel path reads this table fresh via _get_settings().
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bot_settings (
                id                           INTEGER PRIMARY KEY CHECK (id = 1),
                individual_consumes_visits   INTEGER NOT NULL DEFAULT 0,
                group_requires_subscription  INTEGER NOT NULL DEFAULT 1
            )
        """)
        await db.execute("INSERT OR IGNORE INTO bot_settings (id) VALUES (1)")
        await db.commit()
    await _ensure_group_sessions(db_path)
    await _ensure_individual_slots(db_path)


async def _ensure_group_sessions(db_path: str) -> None:
    """Idempotent recurring-schedule generator: for every active
    group_schedule_templates row and every date in the next GROUP_DAYS_AHEAD
    days matching its day_of_week, INSERT OR IGNORE a concrete slots row.
    UNIQUE(template_id, slot_date) makes repeated calls (including after an
    admin adds a new template mid-run) safe — never duplicates, never touches
    existing rows (so already-booked sessions are untouched)."""
    today = date.today()
    async with aiosqlite.connect(db_path) as db:
        templates = await (await db.execute(
            "SELECT id, day_of_week, slot_time, trainer_id, class_name, capacity, duration_min "
            "FROM group_schedule_templates WHERE active=1"
        )).fetchall()
        for i in range(GROUP_DAYS_AHEAD):
            d = today + timedelta(days=i)
            for tpl in templates:
                tpl_id, dow, slot_time, trainer_id, class_name, capacity, duration_min = tpl
                if d.weekday() != dow:
                    continue
                await db.execute(
                    "INSERT OR IGNORE INTO slots "
                    "(session_type, template_id, trainer_id, slot_date, slot_time, duration_min, class_name, capacity) "
                    "VALUES ('group', ?, ?, ?, ?, ?, ?, ?)",
                    (tpl_id, trainer_id, d.isoformat(), slot_time, duration_min, class_name, capacity),
                )
        await db.commit()


async def _ensure_individual_slots(db_path: str) -> None:
    """Same coarse per-(trainer, date) idempotency guard as
    booking_beauty.py's _ensure_slots: if ANY individual slot already exists
    for a given trainer+date, that day is skipped entirely for that trainer —
    no duplicates, existing (possibly booked) rows never touched."""
    today = date.today()
    async with aiosqlite.connect(db_path) as db:
        trainers = await (await db.execute("SELECT id FROM trainers WHERE active=1")).fetchall()
        for (trainer_id,) in trainers:
            for i in range(INDIVIDUAL_DAYS_AHEAD):
                d = (today + timedelta(days=i)).isoformat()
                existing = (await (await db.execute(
                    "SELECT COUNT(*) FROM slots WHERE session_type='individual' AND trainer_id=? AND slot_date=?",
                    (trainer_id, d),
                )).fetchone())[0]
                if existing == 0:
                    for t in INDIVIDUAL_SLOT_TIMES:
                        await db.execute(
                            "INSERT INTO slots (session_type, trainer_id, slot_date, slot_time, capacity) "
                            "VALUES ('individual', ?, ?, ?, 1)",
                            (trainer_id, d, t),
                        )
        await db.commit()


# ── settings ─────────────────────────────────────────────────────────────────

_SETTINGS_KEYS = ("individual_consumes_visits", "group_requires_subscription")

async def _get_settings(db_path: str) -> dict:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("INSERT OR IGNORE INTO bot_settings (id) VALUES (1)")
        await db.commit()
        row = await (await db.execute(
            "SELECT individual_consumes_visits, group_requires_subscription FROM bot_settings WHERE id=1"
        )).fetchone()
    return {k: bool(row[k]) for k in _SETTINGS_KEYS}

async def _toggle_setting(db_path: str, key: str) -> None:
    if key not in _SETTINGS_KEYS:
        return
    async with aiosqlite.connect(db_path) as db:
        await db.execute("INSERT OR IGNORE INTO bot_settings (id) VALUES (1)")
        await db.execute(f"UPDATE bot_settings SET {key} = 1 - {key} WHERE id=1")
        await db.commit()


# ── shared slot engine: booking + cancel/promote (used by BOTH branches) ─────
# This is the "один движок под капотом" the two explicit menu branches both
# call into — session_type only changes WHICH rows get queried on the way in,
# never the booking/cancel/promotion mechanics themselves.

async def _active_subscription_row(db, user_id: int):
    """Must be called with an already-open aiosqlite connection (row_factory
    doesn't matter — indexed access only) so callers can run it inside their
    own BEGIN IMMEDIATE transaction."""
    return await (await db.execute(
        "SELECT id, visits_left FROM subscriptions WHERE user_id=? AND visits_left>0 "
        "AND (expires_at IS NULL OR expires_at >= date('now','localtime')) "
        "ORDER BY purchased_at DESC LIMIT 1",
        (user_id,),
    )).fetchone()


async def _get_settings_conn(db) -> dict:
    """Same as _get_settings() but reuses an already-open connection/
    transaction — used inside _book_slot/_cancel_booking so the settings read
    happens under the SAME BEGIN IMMEDIATE lock as the rest of the decision,
    not a separate connection that could race against a concurrent toggle."""
    await db.execute("INSERT OR IGNORE INTO bot_settings (id) VALUES (1)")
    row = await (await db.execute(
        "SELECT individual_consumes_visits, group_requires_subscription FROM bot_settings WHERE id=1"
    )).fetchone()
    return {k: bool(row[k]) for k in _SETTINGS_KEYS}


async def _book_slot(db_path: str, slot_id: int, user_id: int, client_name: str, client_phone: str | None) -> dict:
    """Books slot_id for user_id. Returns:
      {"ok": True, "status": "confirmed"|"waitlist", "booking_id": int, "position": int|None}
      {"ok": False, "error": "slot_gone"|"no_subscription"}

    BEGIN IMMEDIATE takes SQLite's write lock before the read-decide-write —
    same class of protection as moderator.py's _apply_escalation — so two
    concurrent bookings for the SAME slot_id (the capacity race: two users
    tapping "Подтвердить" on the last open spot at the same moment) can't both
    read "1 spot left" and both get confirmed. For an individual slot
    (capacity=1) a lost-update race here would double-book one trainer's hour
    with two different clients; for a group slot it would silently oversell
    past capacity. The lock is released at commit(), before any Telegram API
    call the caller makes with the result.
    """
    async with aiosqlite.connect(db_path, timeout=10) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        slot = await (await db.execute(
            "SELECT * FROM slots WHERE id=? AND status='active'", (slot_id,)
        )).fetchone()
        if slot is None:
            await db.commit()
            return {"ok": False, "error": "slot_gone"}

        # Review-found race: a double-tap on "✅ Записаться"/"✅ Подтвердить"
        # fires two callback updates for the same user+slot. BEGIN IMMEDIATE
        # alone only serializes the two calls — it doesn't deduplicate them —
        # so without this check the second call would see its OWN first
        # booking already counted in `confirmed` below, still find a free
        # spot on any slot with capacity>1, and insert a SECOND row for the
        # same user: double-charging one subscription visit and occupying
        # two capacity slots that should hold two different clients.
        existing = await (await db.execute(
            "SELECT id, status FROM bookings WHERE slot_id=? AND user_id=? AND status IN ('confirmed','waitlist')",
            (slot_id, user_id),
        )).fetchone()
        if existing is not None:
            await db.commit()
            return {"ok": True, "status": existing["status"], "booking_id": existing["id"], "position": None}

        confirmed = (await (await db.execute(
            "SELECT COUNT(*) FROM bookings WHERE slot_id=? AND status='confirmed'", (slot_id,)
        )).fetchone())[0]
        is_full = confirmed >= slot["capacity"]

        if is_full and slot["session_type"] == "individual":
            # Individual sessions never waitlist — a full 1-on-1 slot means
            # someone else just took it in the race window.
            await db.commit()
            return {"ok": False, "error": "slot_gone"}

        settings = await _get_settings_conn(db)
        requires_subscription = (
            settings["group_requires_subscription"] if slot["session_type"] == "group"
            else settings["individual_consumes_visits"]
        )

        subscription_id = None
        if requires_subscription:
            sub = await _active_subscription_row(db, user_id)
            if sub is None:
                await db.commit()
                return {"ok": False, "error": "no_subscription"}
            subscription_id = sub["id"]

        status = "waitlist" if is_full else "confirmed"
        cur = await db.execute(
            "INSERT INTO bookings (slot_id, user_id, client_name, client_phone, status, subscription_id) "
            "VALUES (?,?,?,?,?,?)",
            (slot_id, user_id, client_name, client_phone, status,
             subscription_id if status == "confirmed" else None),
        )
        booking_id = cur.lastrowid
        if status == "confirmed" and subscription_id is not None:
            await db.execute("UPDATE subscriptions SET visits_left = visits_left - 1 WHERE id=?", (subscription_id,))

        position = None
        if status == "waitlist":
            ahead = (await (await db.execute(
                "SELECT COUNT(*) FROM bookings WHERE slot_id=? AND status='waitlist' AND id<?",
                (slot_id, booking_id),
            )).fetchone())[0]
            position = ahead + 1

        await db.commit()
        return {"ok": True, "status": status, "booking_id": booking_id, "position": position}


async def _cancel_booking(db_path: str, booking_id: int, requesting_user_id: int | None = None) -> dict:
    """Cancels booking_id (deletes the row, same convention as
    booking_beauty.py's cb_adm_cancel), refunds a consumed visit if any, and —
    for a CONFIRMED group booking only — promotes the oldest eligible waitlist
    entry for the same slot. Returns:
      {"ok": True, "promoted": {"booking_id":..,"user_id":..,"slot":..}|None}
      {"ok": False, "error": "not_found"|"forbidden"}

    Race this specifically guards against (owner-flagged): two admins cancel
    two DIFFERENT confirmed bookings on the SAME full group slot at the same
    moment. Without a lock, both cancellations could independently SELECT the
    same "oldest waitlist row" and both promote IT — one lucky waitlisted user
    gets double-promoted (and double-charged a visit) while two freed spots
    collapse into one, and the next person in line never gets in. BEGIN
    IMMEDIATE serializes the two cancel calls per this connection's write
    lock: the second one's SELECT of "oldest waitlist row" only runs after
    the first has already flipped its candidate's status to 'confirmed' and
    committed, so it correctly sees and promotes the NEXT candidate instead.
    Same protection class as moderator.py's _apply_escalation BEGIN IMMEDIATE.
    """
    async with aiosqlite.connect(db_path, timeout=10) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        booking = await (await db.execute("SELECT * FROM bookings WHERE id=?", (booking_id,))).fetchone()
        if booking is None:
            await db.commit()
            return {"ok": False, "error": "not_found"}
        if requesting_user_id is not None and booking["user_id"] != requesting_user_id:
            await db.commit()
            return {"ok": False, "error": "forbidden"}

        slot = await (await db.execute("SELECT * FROM slots WHERE id=?", (booking["slot_id"],))).fetchone()
        await db.execute("DELETE FROM bookings WHERE id=?", (booking_id,))

        if booking["status"] == "confirmed" and booking["subscription_id"] is not None:
            await db.execute(
                "UPDATE subscriptions SET visits_left = visits_left + 1 WHERE id=?",
                (booking["subscription_id"],),
            )

        promoted = None
        if booking["status"] == "confirmed" and slot is not None and slot["session_type"] == "group":
            settings = await _get_settings_conn(db)
            candidates = await (await db.execute(
                "SELECT * FROM bookings WHERE slot_id=? AND status='waitlist' ORDER BY id ASC",
                (slot["id"],),
            )).fetchall()
            for cand in candidates:
                sub_id = None
                if settings["group_requires_subscription"]:
                    sub = await _active_subscription_row(db, cand["user_id"])
                    if sub is None:
                        # Owner-confirmed as-is: skip this candidate (their
                        # subscription ran dry while queued) and try the next
                        # one in line, rather than blocking the whole queue.
                        continue
                    sub_id = sub["id"]
                    await db.execute("UPDATE subscriptions SET visits_left = visits_left - 1 WHERE id=?", (sub_id,))
                await db.execute(
                    "UPDATE bookings SET status='confirmed', subscription_id=? WHERE id=?",
                    (sub_id, cand["id"]),
                )
                promoted = {"booking_id": cand["id"], "user_id": cand["user_id"], "slot": dict(slot)}
                break

        await db.commit()
        return {"ok": True, "promoted": promoted}


# ── trainer ratings ──────────────────────────────────────────────────────────

async def _unrated_past_bookings(db_path: str, user_id: int) -> list[dict]:
    """Confirmed bookings whose SESSION date has already passed (offset by
    RATING_PROMPT_DAYS_AFTER) and that don't yet have a trainer_ratings row —
    the candidate list for "⭐ Оценить тренера". Only bookings with a real
    trainer_id are eligible (a group slot with no trainer assigned has
    nobody to rate)."""
    cutoff = (date.today() - timedelta(days=RATING_PROMPT_DAYS_AFTER)).isoformat()
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT b.id AS booking_id, s.trainer_id, s.slot_date, s.slot_time, s.class_name, t.name AS trainer_name "
            "FROM bookings b JOIN slots s ON b.slot_id=s.id LEFT JOIN trainers t ON s.trainer_id=t.id "
            "WHERE b.user_id=? AND b.status='confirmed' AND s.trainer_id IS NOT NULL AND s.slot_date <= ? "
            "AND NOT EXISTS (SELECT 1 FROM trainer_ratings r WHERE r.booking_id = b.id) "
            "ORDER BY s.slot_date DESC LIMIT 20",
            (user_id, cutoff),
        )).fetchall()
    return [dict(r) for r in rows]


async def _record_rating(db_path: str, booking_id: int, user_id: int, rating: int, comment: str | None) -> dict:
    """Returns {"ok": True} / {"ok": False, "error": "not_found"|"forbidden"|"not_yet"|"already_rated"}.
    BEGIN IMMEDIATE isn't needed for the write itself (UNIQUE(booking_id) +
    INSERT OR IGNORE already makes a concurrent double-submit race a no-op
    at the DB level, same idea as _book_slot's existing-row check) — but the
    ownership/eligibility reads must still happen before that INSERT so a
    forged booking_id from another user (or an unfinished/未-confirmed
    booking) can't be rated."""
    rating = max(1, min(MAX_RATING, rating))
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        booking = await (await db.execute(
            "SELECT b.user_id, b.status, s.trainer_id, s.slot_date FROM bookings b "
            "JOIN slots s ON b.slot_id=s.id WHERE b.id=?", (booking_id,),
        )).fetchone()
        if booking is None or booking["trainer_id"] is None:
            return {"ok": False, "error": "not_found"}
        if booking["user_id"] != user_id:
            return {"ok": False, "error": "forbidden"}
        if booking["status"] != "confirmed":
            return {"ok": False, "error": "not_found"}
        cutoff = (date.today() - timedelta(days=RATING_PROMPT_DAYS_AFTER)).isoformat()
        if booking["slot_date"] > cutoff:
            return {"ok": False, "error": "not_yet"}
        cur = await db.execute(
            "INSERT OR IGNORE INTO trainer_ratings (trainer_id, user_id, booking_id, rating, comment) "
            "VALUES (?,?,?,?,?)",
            (booking["trainer_id"], user_id, booking_id, rating, comment),
        )
        await db.commit()
        if cur.rowcount == 0:
            return {"ok": False, "error": "already_rated"}
        return {"ok": True}


async def _trainer_rating_summary(db, trainer_id: int) -> tuple[float | None, int]:
    """Must be called with an already-open connection — same convention as
    _active_subscription_row. Returns (average rounded to 1 decimal, count);
    average is None when the trainer has no ratings yet (0/0 avoided)."""
    row = await (await db.execute(
        "SELECT AVG(rating), COUNT(*) FROM trainer_ratings WHERE trainer_id=?", (trainer_id,)
    )).fetchone()
    avg, count = row[0], row[1]
    return (round(avg, 1) if avg is not None else None, count or 0)


# ── keyboards: client menu ─────────────────────────────────────────────────────

def kb_main(is_admin: bool) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="📅 Групповое занятие"), KeyboardButton(text="🧑 Индивидуальная тренировка")],
        [KeyboardButton(text="📋 Мои записи"), KeyboardButton(text="💳 Мой абонемент")],
        [KeyboardButton(text="⭐ Оценить тренера")],
    ]
    if is_admin:
        rows.append([KeyboardButton(text="⚙️ Панель администратора")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def kb_private_cancel(callback_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data=callback_data)]])


# ── keyboards: group flow ──────────────────────────────────────────────────────

async def kb_group_days(db_path: str) -> InlineKeyboardMarkup:
    async with aiosqlite.connect(db_path) as db:
        rows = await (await db.execute(
            "SELECT DISTINCT slot_date FROM slots WHERE session_type='group' AND status='active' "
            "AND slot_date >= date('now','localtime') ORDER BY slot_date"
        )).fetchall()
    btns = [[InlineKeyboardButton(text=_date_label(d), callback_data=f"fit_gday:{d}")] for (d,) in rows]
    if not btns:
        btns.append([InlineKeyboardButton(text="Нет запланированных занятий", callback_data="fit_noop")])
    return InlineKeyboardMarkup(inline_keyboard=btns)


async def kb_group_sessions(db_path: str, slot_date: str) -> InlineKeyboardMarkup:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT s.id, s.slot_time, s.class_name, s.capacity, t.name AS trainer_name, "
            "(SELECT COUNT(*) FROM bookings b WHERE b.slot_id=s.id AND b.status='confirmed') AS taken "
            "FROM slots s LEFT JOIN trainers t ON s.trainer_id=t.id "
            "WHERE s.session_type='group' AND s.slot_date=? AND s.status='active' ORDER BY s.slot_time",
            (slot_date,),
        )).fetchall()
    btns = []
    for r in rows:
        label = f"🕖 {r['slot_time']} {r['class_name']} ({r['trainer_name'] or '—'}) — "
        label += "⏳ лист ожидания" if r["taken"] >= r["capacity"] else f"{r['taken']}/{r['capacity']}"
        btns.append([InlineKeyboardButton(text=label, callback_data=f"fit_gsess:{r['id']}")])
    if not btns:
        btns.append([InlineKeyboardButton(text="Нет занятий в этот день", callback_data="fit_noop")])
    btns.append([InlineKeyboardButton(text="◀️ Назад", callback_data="fit_gback_days")])
    return InlineKeyboardMarkup(inline_keyboard=btns)


def kb_group_session_detail(slot_id: int, slot_date: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Записаться", callback_data=f"fit_gconfirm:{slot_id}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"fit_gback_sess:{slot_date}")],
    ])


# ── keyboards: individual flow ─────────────────────────────────────────────────

async def kb_specializations(db_path: str) -> InlineKeyboardMarkup:
    async with aiosqlite.connect(db_path) as db:
        rows = await (await db.execute("SELECT specializations FROM trainers WHERE active=1")).fetchall()
    present = set()
    for (specs,) in rows:
        present.update(s.strip() for s in (specs or "").split(",") if s.strip())
    btns = [
        [InlineKeyboardButton(text=f"🏷 {s}", callback_data=f"fit_ispec:{i}")]
        for i, s in enumerate(SPECIALIZATIONS) if s in present
    ]
    if not btns:
        btns.append([InlineKeyboardButton(text="Нет доступных тренеров", callback_data="fit_noop")])
    return InlineKeyboardMarkup(inline_keyboard=btns)


async def kb_trainers_for_spec(db_path: str, spec: str) -> InlineKeyboardMarkup:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute("SELECT id, name, specializations FROM trainers WHERE active=1")).fetchall()
    btns = []
    for r in rows:
        tags = [t.strip() for t in (r["specializations"] or "").split(",")]
        if spec in tags:
            btns.append([InlineKeyboardButton(text=f"🧑 {r['name']}", callback_data=f"fit_itrainer:{r['id']}")])
    if not btns:
        btns.append([InlineKeyboardButton(text="Нет тренеров с этой специализацией", callback_data="fit_noop")])
    btns.append([InlineKeyboardButton(text="◀️ Назад", callback_data="fit_iback_spec")])
    return InlineKeyboardMarkup(inline_keyboard=btns)


async def kb_individual_days(db_path: str, trainer_id: int) -> InlineKeyboardMarkup:
    async with aiosqlite.connect(db_path) as db:
        rows = await (await db.execute(
            "SELECT DISTINCT s.slot_date FROM slots s WHERE s.session_type='individual' AND s.trainer_id=? "
            "AND s.status='active' AND s.slot_date >= date('now','localtime') "
            "AND NOT EXISTS (SELECT 1 FROM bookings b WHERE b.slot_id=s.id AND b.status='confirmed') "
            "ORDER BY s.slot_date",
            (trainer_id,),
        )).fetchall()
    btns = [[InlineKeyboardButton(text=_date_label(d), callback_data=f"fit_iday:{trainer_id}:{d}")] for (d,) in rows]
    if not btns:
        btns.append([InlineKeyboardButton(text="Нет свободных дней", callback_data="fit_noop")])
    btns.append([InlineKeyboardButton(text="◀️ Назад", callback_data="fit_iback_trainer")])
    return InlineKeyboardMarkup(inline_keyboard=btns)


async def kb_individual_times(db_path: str, trainer_id: int, slot_date: str) -> InlineKeyboardMarkup:
    async with aiosqlite.connect(db_path) as db:
        rows = await (await db.execute(
            "SELECT s.id, s.slot_time FROM slots s WHERE s.session_type='individual' AND s.trainer_id=? "
            "AND s.slot_date=? AND s.status='active' "
            "AND NOT EXISTS (SELECT 1 FROM bookings b WHERE b.slot_id=s.id AND b.status='confirmed') "
            "ORDER BY s.slot_time",
            (trainer_id, slot_date),
        )).fetchall()
    btns = [[InlineKeyboardButton(text=f"⏰ {t}", callback_data=f"fit_itime:{sid}")] for sid, t in rows]
    if not btns:
        btns.append([InlineKeyboardButton(text="Нет свободного времени", callback_data="fit_noop")])
    btns.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"fit_iback_day:{trainer_id}")])
    return InlineKeyboardMarkup(inline_keyboard=btns)


def kb_individual_confirm(slot_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"fit_iconfirm:{slot_id}"),
         InlineKeyboardButton(text="❌ Отмена", callback_data="fit_icancel")],
    ])


# ── FSM ───────────────────────────────────────────────────────────────────────

class IndividualBookFlow(StatesGroup):
    trainer = State(); day = State(); time = State(); name = State(); phone = State(); confirm = State()

class RatingFlow(StatesGroup):
    comment = State()

class AdminFlow(StatesGroup):
    trainer_name = State(); trainer_specs = State()
    sched_day = State(); sched_time = State(); sched_trainer = State(); sched_capacity = State(); sched_name = State()
    plan_name = State(); plan_visits = State(); plan_validity = State()
    issue_client = State(); issue_plan = State()
    add_admin = State(); remove_admin_pick = State()

# Same rationale as moderator.py's FLOW_TIMEOUT_SECONDS: MemoryStorage keeps a
# state until explicitly cleared — without an expiry, an admin who opens an
# add-trainer/add-plan/issue-subscription prompt and then goes quiet has every
# LATER plain message silently captured by that stale flow step.
FLOW_TIMEOUT_SECONDS = 300

def _flow_expired(data: dict) -> bool:
    started_at = data.get("started_at")
    return started_at is None or (time.time() - started_at) > FLOW_TIMEOUT_SECONDS


# ── admin panel navigation helpers (same pattern as moderator.py) ────────────

async def _replace_panel(
    bot: Bot, state: FSMContext, chat_id: int, text: str,
    reply_markup: InlineKeyboardMarkup | None = None, parse_mode: str | None = "HTML",
) -> None:
    """Delete the previously shown admin-panel message (if any) and send a
    new one, tracking its id — same delete-old/show-new mechanic as
    moderator.py's _replace_panel()."""
    data = await state.get_data()
    prev_id = data.get("panel_msg_id")
    if prev_id:
        try:
            await bot.delete_message(chat_id, prev_id)
        except Exception as e:
            logger.debug(f"_replace_panel: failed to delete old panel {prev_id} in chat {chat_id}: {e}")
    try:
        msg = await bot.send_message(chat_id, text, parse_mode=parse_mode, reply_markup=reply_markup)
    except Exception as e:
        logger.warning(f"_replace_panel: failed to send panel to chat {chat_id}: {e}")
        return
    await state.update_data(panel_msg_id=msg.message_id)


async def _clear_flow_keep_panel(state: FSMContext) -> None:
    data = await state.get_data()
    panel_msg_id = data.get("panel_msg_id")
    await state.clear()
    if panel_msg_id is not None:
        await state.update_data(panel_msg_id=panel_msg_id)


def _panel_chat_id(cb: CallbackQuery) -> int | None:
    return cb.message.chat.id if cb.message else None


# ── keyboards: admin panel ─────────────────────────────────────────────────────

def kb_admin_panel_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚙️ Настройки", callback_data="fit_adm_settings")],
        [InlineKeyboardButton(text="🏋 Тренеры", callback_data="fit_adm_trainers")],
        [InlineKeyboardButton(text="📅 Групповое расписание", callback_data="fit_adm_sched")],
        [InlineKeyboardButton(text="🎟 Тарифы абонементов", callback_data="fit_adm_plans")],
        [InlineKeyboardButton(text="💳 Выдать абонемент", callback_data="fit_adm_issue")],
        [InlineKeyboardButton(text="👥 Админы бота", callback_data="fit_adm_admins")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="fit_adm_stats")],
    ])

def _on_off(flag: bool) -> str:
    return "✅ Вкл" if flag else "⬜️ Выкл"

def kb_settings_panel(settings: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"👤 Индивидуалка списывает визиты: {_on_off(settings['individual_consumes_visits'])}",
            callback_data="fit_adm_toggle:individual_consumes_visits",
        )],
        [InlineKeyboardButton(
            text=f"🎟 Групповое требует абонемент: {_on_off(settings['group_requires_subscription'])}",
            callback_data="fit_adm_toggle:group_requires_subscription",
        )],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="fit_adm_back")],
    ])

async def kb_trainers_admin(db_path: str) -> InlineKeyboardMarkup:
    async with aiosqlite.connect(db_path) as db:
        rows = await (await db.execute("SELECT id, name, specializations FROM trainers WHERE active=1 ORDER BY name")).fetchall()
        btns = []
        for tid, name, specs in rows:
            avg, count = await _trainer_rating_summary(db, tid)
            rating_note = f" · ⭐{avg} ({count})" if count else ""
            btns.append([InlineKeyboardButton(text=f"➖ {name} ({specs}){rating_note}", callback_data=f"fit_adm_trainer_rm:{tid}")])
    btns.append([InlineKeyboardButton(text="➕ Добавить тренера", callback_data="fit_adm_trainer_add")])
    btns.append([InlineKeyboardButton(text="◀️ Назад", callback_data="fit_adm_back")])
    return InlineKeyboardMarkup(inline_keyboard=btns)

def kb_spec_toggle(selected: list[str]) -> InlineKeyboardMarkup:
    rows = []
    for i, s in enumerate(SPECIALIZATIONS):
        mark = "☑️" if s in selected else "⬜️"
        rows.append([InlineKeyboardButton(text=f"{mark} {s}", callback_data=f"fit_adm_spec_toggle:{i}")])
    rows.append([InlineKeyboardButton(text="✅ Готово", callback_data="fit_adm_spec_done")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def kb_schedule_admin(db_path: str) -> InlineKeyboardMarkup:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT g.id, g.day_of_week, g.slot_time, g.class_name, g.capacity, t.name AS trainer_name "
            "FROM group_schedule_templates g LEFT JOIN trainers t ON g.trainer_id=t.id "
            "WHERE g.active=1 ORDER BY g.day_of_week, g.slot_time"
        )).fetchall()
    btns = []
    for r in rows:
        label = f"➖ {DAYS_RU[r['day_of_week']]} {r['slot_time']} {r['class_name']} ({r['trainer_name'] or '—'}, {r['capacity']} мест)"
        btns.append([InlineKeyboardButton(text=label, callback_data=f"fit_adm_sched_rm:{r['id']}")])
    btns.append([InlineKeyboardButton(text="➕ Добавить занятие", callback_data="fit_adm_sched_add")])
    btns.append([InlineKeyboardButton(text="◀️ Назад", callback_data="fit_adm_back")])
    return InlineKeyboardMarkup(inline_keyboard=btns)

def kb_weekday_pick() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=DAYS_RU[i], callback_data=f"fit_adm_sched_day:{i}")] for i in range(7)]
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="fit_adm_flow_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def kb_trainer_pick_admin(db_path: str, callback_prefix: str) -> InlineKeyboardMarkup:
    async with aiosqlite.connect(db_path) as db:
        rows = await (await db.execute("SELECT id, name FROM trainers WHERE active=1 ORDER BY name")).fetchall()
    btns = [[InlineKeyboardButton(text=f"🧑 {name}", callback_data=f"{callback_prefix}:{tid}")] for tid, name in rows]
    if not btns:
        btns.append([InlineKeyboardButton(text="Сначала добавьте тренера", callback_data="fit_noop")])
    btns.append([InlineKeyboardButton(text="❌ Отмена", callback_data="fit_adm_flow_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=btns)

def kb_capacity_pick() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=str(n), callback_data=f"fit_adm_sched_cap:{n}") for n in GROUP_CAPACITY_OPTIONS[i:i+3]]
            for i in range(0, len(GROUP_CAPACITY_OPTIONS), 3)]
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="fit_adm_flow_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def kb_plans_admin(db_path: str) -> InlineKeyboardMarkup:
    async with aiosqlite.connect(db_path) as db:
        rows = await (await db.execute(
            "SELECT id, name, total_visits, validity_days FROM subscription_plans WHERE active=1 ORDER BY name"
        )).fetchall()
    btns = []
    for pid, name, visits, validity in rows:
        v = f"{validity} дн." if validity else "без ограничения"
        btns.append([InlineKeyboardButton(text=f"➖ {name} — {visits} посещений, {v}", callback_data=f"fit_adm_plan_rm:{pid}")])
    btns.append([InlineKeyboardButton(text="➕ Добавить тариф", callback_data="fit_adm_plan_add")])
    btns.append([InlineKeyboardButton(text="◀️ Назад", callback_data="fit_adm_back")])
    return InlineKeyboardMarkup(inline_keyboard=btns)

def kb_visits_pick() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=str(n), callback_data=f"fit_adm_plan_visits:{n}") for n in SUBSCRIPTION_VISITS_OPTIONS]]
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="fit_adm_flow_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_validity_pick() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(
        text=("без ограничения" if n == 0 else f"{n} дн."), callback_data=f"fit_adm_plan_validity:{n}"
    )] for n in SUBSCRIPTION_VALIDITY_OPTIONS]
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="fit_adm_flow_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def kb_issue_pick_client(db_path: str) -> InlineKeyboardMarkup:
    async with aiosqlite.connect(db_path) as db:
        rows = await (await db.execute(
            "SELECT DISTINCT user_id FROM bookings ORDER BY id DESC LIMIT 15"
        )).fetchall()
    btns = [[InlineKeyboardButton(text=f"👤 {uid}", callback_data=f"fit_adm_issue_pick:{uid}")] for (uid,) in rows]
    btns.append([InlineKeyboardButton(text="✏️ Ввести ID вручную", callback_data="fit_adm_issue_manual")])
    btns.append([InlineKeyboardButton(text="❌ Отмена", callback_data="fit_adm_flow_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=btns)

async def kb_issue_pick_plan(db_path: str) -> InlineKeyboardMarkup:
    async with aiosqlite.connect(db_path) as db:
        rows = await (await db.execute(
            "SELECT id, name, total_visits FROM subscription_plans WHERE active=1 ORDER BY name"
        )).fetchall()
    btns = [[InlineKeyboardButton(text=f"🎟 {name} ({visits} посещений)", callback_data=f"fit_adm_issue_plan:{pid}")]
            for pid, name, visits in rows]
    if not btns:
        btns.append([InlineKeyboardButton(text="Сначала добавьте тариф", callback_data="fit_noop")])
    btns.append([InlineKeyboardButton(text="❌ Отмена", callback_data="fit_adm_flow_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=btns)

def kb_admins_panel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить админа", callback_data="fit_adm_addadmin")],
        [InlineKeyboardButton(text="➖ Убрать админа", callback_data="fit_adm_removeadmin")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="fit_adm_back")],
    ])

MAX_ADMIN_REMOVE_BUTTONS = 30

def kb_remove_admins(ids: list[str]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=admin_id, callback_data=f"fit_adm_rma:{i}")] for i, admin_id in enumerate(ids)]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="fit_adm_admins")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── /start ────────────────────────────────────────────────────────────────────

@router.message(Command("start"), F.chat.type == "private")
async def cmd_start(message: Message, state: FSMContext, config: BookingFitnessConfig):
    # Same reasoning as moderator.py/inventory.py's cmd_start: /start must
    # reset any dangling mid-flow FSM state.
    await state.clear()
    admins = _load_admins(config.admins_file)
    first_time_admin = not admins
    if first_time_admin:
        _save_admins(config.admins_file, {str(message.from_user.id)})
    is_admin = str(message.from_user.id) in _load_admins(config.admins_file)
    kb = kb_main(is_admin)
    if config.welcome_image.exists():
        await message.answer_photo(FSInputFile(str(config.welcome_image)),
                                   caption=WELCOME_TEXT, parse_mode="HTML", reply_markup=kb)
    else:
        await message.answer(WELCOME_TEXT, parse_mode="HTML", reply_markup=kb)
    if first_time_admin:
        await message.answer(
            "👑 <b>Вы — администратор этого бота.</b>\n\n"
            "Кнопка «⚙️ Панель администратора» открывает управление тренерами, "
            "групповым расписанием, тарифами абонементов и настройками.",
            parse_mode="HTML",
        )


# ── GROUP FLOW ──────────────────────────────────────────────────────────────

@router.message(F.text == "📅 Групповое занятие")
async def group_start(msg: Message, state: FSMContext, config: BookingFitnessConfig):
    await state.clear()
    await msg.answer("📅 Выберите день:", reply_markup=await kb_group_days(config.db_path))

@router.callback_query(F.data.startswith("fit_gday:"))
async def cb_group_day(cb: CallbackQuery, config: BookingFitnessConfig):
    await cb.answer()
    slot_date = cb.data.split(":", 1)[1]
    await cb.message.edit_text(
        f"📅 {_date_label(slot_date)}\n\nВыберите занятие:",
        reply_markup=await kb_group_sessions(config.db_path, slot_date),
    )

@router.callback_query(F.data == "fit_gback_days")
async def cb_group_back_days(cb: CallbackQuery, config: BookingFitnessConfig):
    await cb.answer()
    await cb.message.edit_text("📅 Выберите день:", reply_markup=await kb_group_days(config.db_path))

@router.callback_query(F.data.startswith("fit_gback_sess:"))
async def cb_group_back_sessions(cb: CallbackQuery, config: BookingFitnessConfig):
    await cb.answer()
    slot_date = cb.data.split(":", 1)[1]
    await cb.message.edit_text(
        f"📅 {_date_label(slot_date)}\n\nВыберите занятие:",
        reply_markup=await kb_group_sessions(config.db_path, slot_date),
    )

@router.callback_query(F.data.startswith("fit_gsess:"))
async def cb_group_session(cb: CallbackQuery, config: BookingFitnessConfig):
    await cb.answer()
    slot_id = int(cb.data.split(":", 1)[1])
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            "SELECT s.*, t.name AS trainer_name, "
            "(SELECT COUNT(*) FROM bookings b WHERE b.slot_id=s.id AND b.status='confirmed') AS taken "
            "FROM slots s LEFT JOIN trainers t ON s.trainer_id=t.id WHERE s.id=?",
            (slot_id,),
        )).fetchone()
    if row is None or row["status"] != "active":
        await cb.answer("Это занятие больше не активно.", show_alert=True); return
    spots = f"{row['taken']}/{row['capacity']}" if row["taken"] < row["capacity"] else "мест нет — лист ожидания"
    await cb.message.edit_text(
        f"🏋 <b>{row['class_name']}</b>\n"
        f"📅 {_date_label(row['slot_date'])} в {row['slot_time']}\n"
        f"🧑 Тренер: {row['trainer_name'] or '—'}\n"
        f"⏱ {row['duration_min']} мин\n"
        f"👥 Занято: {spots}",
        parse_mode="HTML",
        reply_markup=kb_group_session_detail(slot_id, row["slot_date"]),
    )

@router.callback_query(F.data.startswith("fit_gconfirm:"))
async def cb_group_confirm(cb: CallbackQuery, bot: Bot, config: BookingFitnessConfig):
    await cb.answer()
    slot_id = int(cb.data.split(":", 1)[1])
    result = await _book_slot(config.db_path, slot_id, cb.from_user.id, cb.from_user.full_name, None)
    if not result["ok"]:
        if result["error"] == "no_subscription":
            await cb.message.edit_text("⚠️ Нужен активный абонемент с посещениями. Обратитесь к администратору.")
        else:
            await cb.message.edit_text("❌ Это занятие больше не активно.")
        return
    if result["status"] == "confirmed":
        await cb.message.edit_text("✅ <b>Вы записаны на занятие!</b>", parse_mode="HTML")
    else:
        await cb.message.edit_text(
            f"⏳ <b>Мест нет — вы в листе ожидания</b> (позиция {result['position']}).\n"
            f"Мы уведомим вас, если освободится место.",
            parse_mode="HTML",
        )
    for admin_id in _load_admins(config.admins_file):
        try:
            await bot.send_message(
                int(admin_id),
                OWNER_NOTIFY_TEXT + f"👥 Групповое занятие, статус: {result['status']}\n"
                f"👤 {_esc(cb.from_user.full_name)} (id {cb.from_user.id})",
                parse_mode="HTML",
            )
        except Exception:
            pass


# ── INDIVIDUAL FLOW ─────────────────────────────────────────────────────────

@router.message(F.text == "🧑 Индивидуальная тренировка")
async def individual_start(msg: Message, state: FSMContext, config: BookingFitnessConfig):
    await state.clear()
    await msg.answer("🏷 Выберите специализацию тренера:", reply_markup=await kb_specializations(config.db_path))

@router.callback_query(F.data.startswith("fit_ispec:"))
async def cb_individual_spec(cb: CallbackQuery, state: FSMContext, config: BookingFitnessConfig):
    await cb.answer()
    idx = int(cb.data.split(":", 1)[1])
    if not (0 <= idx < len(SPECIALIZATIONS)):
        return
    spec = SPECIALIZATIONS[idx]
    await state.update_data(spec=spec)
    await cb.message.edit_text(
        f"🏷 {spec}\n\nВыберите тренера:",
        reply_markup=await kb_trainers_for_spec(config.db_path, spec),
    )

@router.callback_query(F.data == "fit_iback_spec")
async def cb_individual_back_spec(cb: CallbackQuery, state: FSMContext, config: BookingFitnessConfig):
    await cb.answer()
    await state.clear()
    await cb.message.edit_text("🏷 Выберите специализацию тренера:", reply_markup=await kb_specializations(config.db_path))

@router.callback_query(F.data.startswith("fit_itrainer:"))
async def cb_individual_trainer(cb: CallbackQuery, state: FSMContext, config: BookingFitnessConfig):
    await cb.answer()
    trainer_id = int(cb.data.split(":", 1)[1])
    await state.update_data(trainer_id=trainer_id)
    await cb.message.edit_text("📅 Выберите день:", reply_markup=await kb_individual_days(config.db_path, trainer_id))

@router.callback_query(F.data == "fit_iback_trainer")
async def cb_individual_back_trainer(cb: CallbackQuery, state: FSMContext, config: BookingFitnessConfig):
    await cb.answer()
    data = await state.get_data()
    spec = data.get("spec", "")
    await cb.message.edit_text(f"🏷 {spec}\n\nВыберите тренера:", reply_markup=await kb_trainers_for_spec(config.db_path, spec))

@router.callback_query(F.data.startswith("fit_iday:"))
async def cb_individual_day(cb: CallbackQuery, state: FSMContext, config: BookingFitnessConfig):
    await cb.answer()
    _, trainer_id, slot_date = cb.data.split(":", 2)
    trainer_id = int(trainer_id)
    await state.update_data(slot_date=slot_date)
    await cb.message.edit_text(
        f"📅 {_date_label(slot_date)}\n\nВыберите время:",
        reply_markup=await kb_individual_times(config.db_path, trainer_id, slot_date),
    )

@router.callback_query(F.data.startswith("fit_iback_day:"))
async def cb_individual_back_day(cb: CallbackQuery, state: FSMContext, config: BookingFitnessConfig):
    await cb.answer()
    trainer_id = int(cb.data.split(":", 1)[1])
    await cb.message.edit_text("📅 Выберите день:", reply_markup=await kb_individual_days(config.db_path, trainer_id))

@router.callback_query(F.data.startswith("fit_itime:"))
async def cb_individual_time(cb: CallbackQuery, state: FSMContext, config: BookingFitnessConfig):
    await cb.answer()
    slot_id = int(cb.data.split(":", 1)[1])
    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute("SELECT status FROM slots WHERE id=?", (slot_id,))).fetchone()
    if not row or row[0] != "active":
        await cb.answer("Это время уже занято, выберите другое.", show_alert=True); return
    await state.update_data(slot_id=slot_id, started_at=time.time())
    await state.set_state(IndividualBookFlow.name)
    await cb.message.edit_text("👤 Введите ваше имя:")

@router.message(IndividualBookFlow.name, F.text)
async def individual_name(msg: Message, state: FSMContext):
    await state.update_data(client_name=msg.text.strip()[:NAME_MAX_LEN])
    await state.set_state(IndividualBookFlow.phone)
    await msg.answer("📱 Введите номер телефона (например: +7 999 123-45-67 или 89991234567):")

@router.message(IndividualBookFlow.phone, F.text)
async def individual_phone(msg: Message, state: FSMContext, config: BookingFitnessConfig):
    phone = _normalize_phone(msg.text.strip())
    if phone is None:
        await msg.answer(
            "❌ Неверный номер телефона.\nВведите российский номер, например: <b>+7 999 123-45-67</b>",
            parse_mode="HTML",
        )
        return
    await state.update_data(client_phone=phone)
    data = await state.get_data()
    slot_id = data["slot_id"]
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            "SELECT s.*, t.name AS trainer_name FROM slots s LEFT JOIN trainers t ON s.trainer_id=t.id WHERE s.id=?",
            (slot_id,),
        )).fetchone()
    if not row:
        await msg.answer("Слот не найден.", reply_markup=kb_main(False)); await state.clear(); return
    await state.set_state(IndividualBookFlow.confirm)
    await msg.answer(
        f"📋 <b>Подтвердите запись:</b>\n\n"
        f"🧑 Тренер: {row['trainer_name'] or '—'}\n"
        f"📅 {_date_label(row['slot_date'])} в {row['slot_time']}\n"
        f"👤 Имя: {_esc(data['client_name'])}\n"
        f"📱 Телефон: {phone}",
        parse_mode="HTML", reply_markup=kb_individual_confirm(slot_id),
    )

@router.callback_query(F.data.startswith("fit_iconfirm:"))
async def cb_individual_confirm(cb: CallbackQuery, state: FSMContext, bot: Bot, config: BookingFitnessConfig):
    await cb.answer()
    slot_id = int(cb.data.split(":", 1)[1])
    data = await state.get_data()
    result = await _book_slot(
        config.db_path, slot_id, cb.from_user.id, data.get("client_name", ""), data.get("client_phone"),
    )
    await state.clear()
    if not result["ok"]:
        if result["error"] == "no_subscription":
            await cb.message.edit_text("⚠️ Нужен активный абонемент с посещениями. Обратитесь к администратору.")
        else:
            await cb.message.edit_text("❌ Это время уже занято! Выберите другое.")
        return
    await cb.message.edit_text("✅ <b>Вы записаны на индивидуальную тренировку!</b>", parse_mode="HTML")
    for admin_id in _load_admins(config.admins_file):
        try:
            await bot.send_message(
                int(admin_id),
                OWNER_NOTIFY_TEXT + f"🧑 Индивидуальная тренировка\n"
                f"👤 {_esc(data.get('client_name', ''))} · {_esc(data.get('client_phone', ''))}",
                parse_mode="HTML",
            )
        except Exception:
            pass

@router.callback_query(F.data == "fit_icancel")
async def cb_individual_cancel_flow(cb: CallbackQuery, state: FSMContext):
    await cb.answer(); await state.clear()
    await cb.message.edit_text("Отменено.")


# ── MY BOOKINGS / CANCEL ────────────────────────────────────────────────────

@router.message(F.text == "📋 Мои записи")
async def my_bookings(msg: Message, config: BookingFitnessConfig):
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT b.id, b.status, s.session_type, s.slot_date, s.slot_time, s.class_name, t.name AS trainer_name "
            "FROM bookings b JOIN slots s ON b.slot_id=s.id LEFT JOIN trainers t ON s.trainer_id=t.id "
            "WHERE b.user_id=? AND s.slot_date >= date('now','localtime') ORDER BY s.slot_date, s.slot_time",
            (msg.from_user.id,),
        )).fetchall()
    if not rows:
        await msg.answer("📭 У вас нет активных записей.", reply_markup=kb_main(str(msg.from_user.id) in _load_admins(config.admins_file)))
        return
    btns = []
    for r in rows:
        icon = "👥" if r["session_type"] == "group" else "🧑"
        status = "" if r["status"] == "confirmed" else " (лист ожидания)"
        label = f"{icon} {_date_label(r['slot_date'])} {r['slot_time']} · {r['class_name'] or r['trainer_name'] or ''}{status}"
        btns.append([InlineKeyboardButton(text=f"❌ {label}", callback_data=f"fit_cancel:{r['id']}")])
    await msg.answer("Ваши записи (нажмите, чтобы отменить):", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))

@router.callback_query(F.data.startswith("fit_cancel:"))
async def cb_cancel_booking(cb: CallbackQuery, bot: Bot, config: BookingFitnessConfig):
    await cb.answer()
    booking_id = int(cb.data.split(":", 1)[1])
    result = await _cancel_booking(config.db_path, booking_id, requesting_user_id=cb.from_user.id)
    if not result["ok"]:
        msg = "Запись не найдена." if result["error"] == "not_found" else "Это не ваша запись."
        await cb.message.edit_text(msg); return
    await cb.message.edit_text("✅ Запись отменена.")
    promoted = result["promoted"]
    if promoted:
        slot = promoted["slot"]
        try:
            await bot.send_message(
                promoted["user_id"],
                f"🎉 <b>Освободилось место!</b>\nВы переведены из листа ожидания в подтверждённые записи:\n"
                f"🏋 {slot['class_name']}\n📅 {_date_label(slot['slot_date'])} в {slot['slot_time']}",
                parse_mode="HTML",
            )
        except Exception:
            pass


# ── TRAINER RATINGS (client-facing) ─────────────────────────────────────────

def kb_rating_stars(booking_id: int) -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton(text="⭐" * n, callback_data=f"fit_rate_stars:{booking_id}:{n}")
           for n in range(1, MAX_RATING + 1)]
    return InlineKeyboardMarkup(inline_keyboard=[row])

def kb_rating_comment_skip() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="➡️ Пропустить", callback_data="fit_rate_skip")]])

@router.message(F.text == "⭐ Оценить тренера")
async def rate_trainer_start(msg: Message, state: FSMContext, config: BookingFitnessConfig):
    await state.clear()
    candidates = await _unrated_past_bookings(config.db_path, msg.from_user.id)
    if not candidates:
        await msg.answer("У вас нет прошедших занятий, ожидающих оценки."); return
    btns = []
    for c in candidates:
        label = f"{_date_label(c['slot_date'])} {c['slot_time']} · {c['class_name'] or c['trainer_name'] or ''} ({c['trainer_name'] or '—'})"
        btns.append([InlineKeyboardButton(text=label, callback_data=f"fit_rate_pick:{c['booking_id']}")])
    await msg.answer("Выберите занятие, которое хотите оценить:", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))

@router.callback_query(F.data.startswith("fit_rate_pick:"))
async def cb_rate_pick(cb: CallbackQuery):
    await cb.answer()
    booking_id = int(cb.data.split(":", 1)[1])
    await cb.message.edit_text("⭐ Оцените тренера:", reply_markup=kb_rating_stars(booking_id))

@router.callback_query(F.data.startswith("fit_rate_stars:"))
async def cb_rate_stars(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    _, booking_id, rating = cb.data.split(":", 2)
    await state.update_data(rate_booking_id=int(booking_id), rate_value=int(rating), started_at=time.time())
    await state.set_state(RatingFlow.comment)
    await cb.message.edit_text(
        f"Оценка: {'⭐' * int(rating)}\n\nХотите добавить комментарий? Пришлите текст или нажмите «Пропустить».",
        reply_markup=kb_rating_comment_skip(),
    )

async def _finish_rating(config: BookingFitnessConfig, user_id: int, data: dict, comment: str | None) -> str:
    booking_id = data.get("rate_booking_id")
    rating = data.get("rate_value")
    if booking_id is None or rating is None:
        return "⏳ Время ожидания истекло — начните заново кнопкой «⭐ Оценить тренера»."
    result = await _record_rating(config.db_path, booking_id, user_id, rating, comment)
    if result["ok"]:
        return "✅ Спасибо за оценку!"
    if result["error"] == "already_rated":
        return "Вы уже оценили это занятие."
    return "⚠️ Не удалось сохранить оценку — попробуйте ещё раз через «⭐ Оценить тренера»."

@router.message(RatingFlow.comment, F.text)
async def rate_comment_text(msg: Message, state: FSMContext, config: BookingFitnessConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("⏳ Время ожидания истекло — начните заново кнопкой «⭐ Оценить тренера»."); return
    comment = msg.text.strip()[:RATING_COMMENT_MAX_LEN]
    reply = await _finish_rating(config, msg.from_user.id, data, comment or None)
    await state.clear()
    await msg.answer(reply)

@router.callback_query(F.data == "fit_rate_skip")
async def cb_rate_skip(cb: CallbackQuery, state: FSMContext, config: BookingFitnessConfig):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("⏳ Время ожидания истекло — начните заново кнопкой «⭐ Оценить тренера»."); return
    reply = await _finish_rating(config, cb.from_user.id, data, None)
    await state.clear()
    await cb.message.edit_text(reply)


# ── SUBSCRIPTION VIEW ────────────────────────────────────────────────────────

def kb_subscription_view(subscription_id: int, autorenew: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text=f"🔁 Автопродление: {_on_off(autorenew)}", callback_data=f"fit_sub_autorenew:{subscription_id}",
    )]])

@router.message(F.text == "💳 Мой абонемент")
async def my_subscription(msg: Message, config: BookingFitnessConfig):
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            "SELECT * FROM subscriptions WHERE user_id=? ORDER BY purchased_at DESC LIMIT 1", (msg.from_user.id,)
        )).fetchone()
    if not row:
        await msg.answer("У вас нет абонемента. Обратитесь к администратору."); return
    expiry = f"до {row['expires_at']}" if row["expires_at"] else "без ограничения по сроку"
    autorenew_note = "" if row["expires_at"] is None else (
        "\n🔁 Автопродление включено — по тому же тарифу." if row["autorenew"] else ""
    )
    await msg.answer(
        f"💳 <b>{_esc(row['plan_name'])}</b>\n"
        f"Осталось посещений: <b>{row['visits_left']}</b> из {row['total_visits']}\n"
        f"Срок действия: {expiry}{autorenew_note}",
        parse_mode="HTML",
        # Autorenew only makes sense for a subscription that actually
        # expires — a NULL expires_at (SUBSCRIPTION_VALIDITY_OPTIONS' "0 =
        # без ограничения" choice) never lapses, so there's nothing to
        # auto-renew and the toggle would be a confusing no-op.
        reply_markup=kb_subscription_view(row["id"], bool(row["autorenew"])) if row["expires_at"] else None,
    )

@router.callback_query(F.data.startswith("fit_sub_autorenew:"))
async def cb_sub_autorenew_toggle(cb: CallbackQuery, config: BookingFitnessConfig):
    await cb.answer()
    subscription_id = int(cb.data.split(":", 1)[1])
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        # Ownership check baked into the UPDATE's WHERE, not a separate
        # SELECT-then-UPDATE — same "no window for a stale read" reasoning
        # as _cancel_booking's forbidden check, just cheap enough here not
        # to need a full BEGIN IMMEDIATE transaction (a single UPDATE is
        # already atomic).
        await db.execute(
            "UPDATE subscriptions SET autorenew = 1 - autorenew WHERE id=? AND user_id=?",
            (subscription_id, cb.from_user.id),
        )
        await db.commit()
        row = await (await db.execute(
            "SELECT id, autorenew FROM subscriptions WHERE id=? AND user_id=?", (subscription_id, cb.from_user.id),
        )).fetchone()
    if row is None:
        await cb.message.edit_reply_markup(reply_markup=None); return
    await cb.message.edit_reply_markup(reply_markup=kb_subscription_view(row["id"], bool(row["autorenew"])))


# ── ADMIN PANEL ──────────────────────────────────────────────────────────────

@router.message(F.text == "⚙️ Панель администратора")
async def admin_panel_entry(msg: Message, bot: Bot, state: FSMContext, config: BookingFitnessConfig):
    if not _is_bot_admin(msg.from_user.id, config):
        await msg.answer("⛔ Нет доступа"); return
    await state.clear()
    await _replace_panel(bot, state, msg.chat.id, "⚙️ <b>Панель администратора</b>", reply_markup=kb_admin_panel_main())

@router.callback_query(F.data == "fit_adm_back")
async def cb_adm_back(cb: CallbackQuery, bot: Bot, state: FSMContext, config: BookingFitnessConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    await _clear_flow_keep_panel(state)
    await _replace_panel(bot, state, chat_id, "⚙️ <b>Панель администратора</b>", reply_markup=kb_admin_panel_main())

@router.callback_query(F.data == "fit_adm_flow_cancel")
async def cb_adm_flow_cancel(cb: CallbackQuery, bot: Bot, state: FSMContext, config: BookingFitnessConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    await _clear_flow_keep_panel(state)
    await _replace_panel(bot, state, chat_id, "⚙️ <b>Панель администратора</b>", reply_markup=kb_admin_panel_main())

@router.callback_query(F.data == "fit_noop")
async def cb_noop(cb: CallbackQuery):
    await cb.answer()


# ── admin: settings toggles ──────────────────────────────────────────────────

@router.callback_query(F.data == "fit_adm_settings")
async def cb_adm_settings(cb: CallbackQuery, bot: Bot, state: FSMContext, config: BookingFitnessConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    settings = await _get_settings(config.db_path)
    await _replace_panel(bot, state, chat_id, "⚙️ <b>Настройки бронирования</b>", reply_markup=kb_settings_panel(settings))

@router.callback_query(F.data.startswith("fit_adm_toggle:"))
async def cb_adm_toggle(cb: CallbackQuery, bot: Bot, state: FSMContext, config: BookingFitnessConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    key = cb.data.split(":", 1)[1]
    await _toggle_setting(config.db_path, key)
    settings = await _get_settings(config.db_path)
    await _replace_panel(bot, state, chat_id, "⚙️ <b>Настройки бронирования</b>", reply_markup=kb_settings_panel(settings))


# ── admin: trainers CRUD ─────────────────────────────────────────────────────

@router.callback_query(F.data == "fit_adm_trainers")
async def cb_adm_trainers(cb: CallbackQuery, bot: Bot, state: FSMContext, config: BookingFitnessConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    await _replace_panel(bot, state, chat_id, "🏋 <b>Тренеры</b>", reply_markup=await kb_trainers_admin(config.db_path))

@router.callback_query(F.data.startswith("fit_adm_trainer_rm:"))
async def cb_adm_trainer_rm(cb: CallbackQuery, bot: Bot, state: FSMContext, config: BookingFitnessConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    trainer_id = int(cb.data.split(":", 1)[1])
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute("UPDATE trainers SET active=0 WHERE id=?", (trainer_id,))
        await db.commit()
    await _replace_panel(bot, state, chat_id, "🏋 <b>Тренеры</b>", reply_markup=await kb_trainers_admin(config.db_path))

@router.callback_query(F.data == "fit_adm_trainer_add")
async def cb_adm_trainer_add(cb: CallbackQuery, bot: Bot, state: FSMContext, config: BookingFitnessConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    await _replace_panel(bot, state, chat_id, "Пришлите имя тренера:", reply_markup=kb_private_cancel("fit_adm_flow_cancel"))
    await state.update_data(started_at=time.time())
    await state.set_state(AdminFlow.trainer_name)

@router.message(AdminFlow.trainer_name, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def adm_trainer_name(msg: Message, bot: Bot, state: FSMContext, config: BookingFitnessConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await _clear_flow_keep_panel(state)
        await msg.answer("⏳ Время ожидания истекло — начните заново."); return
    if not _is_bot_admin(msg.from_user.id, config):
        await _clear_flow_keep_panel(state); return
    name = msg.text.strip()[:NAME_MAX_LEN]
    if not name:
        await msg.answer("Имя не может быть пустым."); return
    await state.update_data(trainer_name=name, selected_specs=[])
    await state.set_state(AdminFlow.trainer_specs)
    await _replace_panel(
        bot, state, msg.chat.id, f"👤 {_esc(name)}\n\nВыберите специализации:", reply_markup=kb_spec_toggle([]),
    )

@router.callback_query(F.data.startswith("fit_adm_spec_toggle:"))
async def cb_adm_spec_toggle(cb: CallbackQuery, bot: Bot, state: FSMContext, config: BookingFitnessConfig):
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    data = await state.get_data()
    if _flow_expired(data):
        await _clear_flow_keep_panel(state)
        await cb.message.edit_text("⏳ Время ожидания истекло — начните заново."); return
    idx = int(cb.data.split(":", 1)[1])
    if not (0 <= idx < len(SPECIALIZATIONS)):
        return
    spec = SPECIALIZATIONS[idx]
    selected = list(data.get("selected_specs", []))
    if spec in selected:
        selected.remove(spec)
    else:
        selected.append(spec)
    await state.update_data(selected_specs=selected)
    await cb.message.edit_reply_markup(reply_markup=kb_spec_toggle(selected))

@router.callback_query(F.data == "fit_adm_spec_done")
async def cb_adm_spec_done(cb: CallbackQuery, bot: Bot, state: FSMContext, config: BookingFitnessConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    data = await state.get_data()
    if _flow_expired(data):
        await _clear_flow_keep_panel(state)
        await _replace_panel(bot, state, chat_id, "⏳ Время ожидания истекло.", reply_markup=kb_admin_panel_main())
        return
    name = data.get("trainer_name", "").strip()
    specs = ",".join(data.get("selected_specs", []))
    if not name:
        await _clear_flow_keep_panel(state)
        await _replace_panel(bot, state, chat_id, "🏋 <b>Тренеры</b>", reply_markup=await kb_trainers_admin(config.db_path))
        return
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute("INSERT INTO trainers (name, specializations) VALUES (?,?)", (name, specs))
        await db.commit()
    await _ensure_individual_slots(config.db_path)
    await _clear_flow_keep_panel(state)
    await _replace_panel(
        bot, state, chat_id, f"✅ Тренер добавлен.\n\n🏋 <b>Тренеры</b>",
        reply_markup=await kb_trainers_admin(config.db_path),
    )


# ── admin: group schedule CRUD ───────────────────────────────────────────────

@router.callback_query(F.data == "fit_adm_sched")
async def cb_adm_sched(cb: CallbackQuery, bot: Bot, state: FSMContext, config: BookingFitnessConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    await _replace_panel(bot, state, chat_id, "📅 <b>Групповое расписание</b>", reply_markup=await kb_schedule_admin(config.db_path))

@router.callback_query(F.data.startswith("fit_adm_sched_rm:"))
async def cb_adm_sched_rm(cb: CallbackQuery, bot: Bot, state: FSMContext, config: BookingFitnessConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    tpl_id = int(cb.data.split(":", 1)[1])
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute("UPDATE group_schedule_templates SET active=0 WHERE id=?", (tpl_id,))
        await db.commit()
    await _replace_panel(bot, state, chat_id, "📅 <b>Групповое расписание</b>", reply_markup=await kb_schedule_admin(config.db_path))

@router.callback_query(F.data == "fit_adm_sched_add")
async def cb_adm_sched_add(cb: CallbackQuery, bot: Bot, state: FSMContext, config: BookingFitnessConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    await state.update_data(started_at=time.time())
    await state.set_state(AdminFlow.sched_day)
    await _replace_panel(bot, state, chat_id, "Выберите день недели:", reply_markup=kb_weekday_pick())

@router.callback_query(AdminFlow.sched_day, F.data.startswith("fit_adm_sched_day:"))
async def cb_adm_sched_day(cb: CallbackQuery, bot: Bot, state: FSMContext, config: BookingFitnessConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await _clear_flow_keep_panel(state)
        await _replace_panel(bot, state, chat_id, "⏳ Время ожидания истекло.", reply_markup=kb_admin_panel_main()); return
    dow = int(cb.data.split(":", 1)[1])
    await state.update_data(sched_day=dow)
    await state.set_state(AdminFlow.sched_time)
    await _replace_panel(
        bot, state, chat_id, "Пришлите время начала (например 19:00):", reply_markup=kb_private_cancel("fit_adm_flow_cancel"),
    )

@router.message(AdminFlow.sched_time, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def adm_sched_time(msg: Message, bot: Bot, state: FSMContext, config: BookingFitnessConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await _clear_flow_keep_panel(state)
        await msg.answer("⏳ Время ожидания истекло — начните заново."); return
    if not _is_bot_admin(msg.from_user.id, config):
        await _clear_flow_keep_panel(state); return
    text = msg.text.strip()
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", text):
        await msg.answer("Неверный формат. Пришлите время как ЧЧ:ММ, например 19:00."); return
    await state.update_data(sched_time=text)
    await state.set_state(AdminFlow.sched_trainer)
    await _replace_panel(
        bot, state, msg.chat.id, "Выберите тренера:",
        reply_markup=await kb_trainer_pick_admin(config.db_path, "fit_adm_sched_trainer"),
    )

@router.callback_query(AdminFlow.sched_trainer, F.data.startswith("fit_adm_sched_trainer:"))
async def cb_adm_sched_trainer(cb: CallbackQuery, bot: Bot, state: FSMContext, config: BookingFitnessConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await _clear_flow_keep_panel(state)
        await _replace_panel(bot, state, chat_id, "⏳ Время ожидания истекло.", reply_markup=kb_admin_panel_main()); return
    trainer_id = int(cb.data.split(":", 1)[1])
    await state.update_data(sched_trainer_id=trainer_id)
    await state.set_state(AdminFlow.sched_capacity)
    await _replace_panel(bot, state, chat_id, "Выберите вместимость группы:", reply_markup=kb_capacity_pick())

@router.callback_query(AdminFlow.sched_capacity, F.data.startswith("fit_adm_sched_cap:"))
async def cb_adm_sched_cap(cb: CallbackQuery, bot: Bot, state: FSMContext, config: BookingFitnessConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await _clear_flow_keep_panel(state)
        await _replace_panel(bot, state, chat_id, "⏳ Время ожидания истекло.", reply_markup=kb_admin_panel_main()); return
    capacity = int(cb.data.split(":", 1)[1])
    await state.update_data(sched_capacity=capacity)
    await state.set_state(AdminFlow.sched_name)
    await _replace_panel(
        bot, state, chat_id, "Пришлите название занятия (например: Йога для начинающих):",
        reply_markup=kb_private_cancel("fit_adm_flow_cancel"),
    )

@router.message(AdminFlow.sched_name, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def adm_sched_name(msg: Message, bot: Bot, state: FSMContext, config: BookingFitnessConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await _clear_flow_keep_panel(state)
        await msg.answer("⏳ Время ожидания истекло — начните заново."); return
    if not _is_bot_admin(msg.from_user.id, config):
        await _clear_flow_keep_panel(state); return
    class_name = msg.text.strip()[:NAME_MAX_LEN]
    if not class_name:
        await msg.answer("Название не может быть пустым."); return
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute(
            "INSERT INTO group_schedule_templates (day_of_week, slot_time, trainer_id, class_name, capacity, duration_min) "
            "VALUES (?,?,?,?,?,?)",
            (data["sched_day"], data["sched_time"], data["sched_trainer_id"], class_name,
             data["sched_capacity"], GROUP_DEFAULT_DURATION_MIN),
        )
        await db.commit()
    await _ensure_group_sessions(config.db_path)
    await _clear_flow_keep_panel(state)
    await _replace_panel(
        bot, state, msg.chat.id, "✅ Занятие добавлено в расписание.\n\n📅 <b>Групповое расписание</b>",
        reply_markup=await kb_schedule_admin(config.db_path),
    )


# ── admin: subscription plans CRUD ───────────────────────────────────────────

@router.callback_query(F.data == "fit_adm_plans")
async def cb_adm_plans(cb: CallbackQuery, bot: Bot, state: FSMContext, config: BookingFitnessConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    await _replace_panel(bot, state, chat_id, "🎟 <b>Тарифы абонементов</b>", reply_markup=await kb_plans_admin(config.db_path))

@router.callback_query(F.data.startswith("fit_adm_plan_rm:"))
async def cb_adm_plan_rm(cb: CallbackQuery, bot: Bot, state: FSMContext, config: BookingFitnessConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    plan_id = int(cb.data.split(":", 1)[1])
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute("UPDATE subscription_plans SET active=0 WHERE id=?", (plan_id,))
        await db.commit()
    await _replace_panel(bot, state, chat_id, "🎟 <b>Тарифы абонементов</b>", reply_markup=await kb_plans_admin(config.db_path))

@router.callback_query(F.data == "fit_adm_plan_add")
async def cb_adm_plan_add(cb: CallbackQuery, bot: Bot, state: FSMContext, config: BookingFitnessConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    await state.update_data(started_at=time.time())
    await state.set_state(AdminFlow.plan_name)
    await _replace_panel(bot, state, chat_id, "Пришлите название тарифа (например: 8 занятий/мес):", reply_markup=kb_private_cancel("fit_adm_flow_cancel"))

@router.message(AdminFlow.plan_name, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def adm_plan_name(msg: Message, bot: Bot, state: FSMContext, config: BookingFitnessConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await _clear_flow_keep_panel(state)
        await msg.answer("⏳ Время ожидания истекло — начните заново."); return
    if not _is_bot_admin(msg.from_user.id, config):
        await _clear_flow_keep_panel(state); return
    name = msg.text.strip()[:NAME_MAX_LEN]
    if not name:
        await msg.answer("Название не может быть пустым."); return
    await state.update_data(plan_name=name)
    await state.set_state(AdminFlow.plan_visits)
    await _replace_panel(bot, state, msg.chat.id, "Сколько посещений в тарифе?", reply_markup=kb_visits_pick())

@router.callback_query(AdminFlow.plan_visits, F.data.startswith("fit_adm_plan_visits:"))
async def cb_adm_plan_visits(cb: CallbackQuery, bot: Bot, state: FSMContext, config: BookingFitnessConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await _clear_flow_keep_panel(state)
        await _replace_panel(bot, state, chat_id, "⏳ Время ожидания истекло.", reply_markup=kb_admin_panel_main()); return
    visits = int(cb.data.split(":", 1)[1])
    await state.update_data(plan_visits=visits)
    await state.set_state(AdminFlow.plan_validity)
    await _replace_panel(bot, state, chat_id, "Срок действия абонемента?", reply_markup=kb_validity_pick())

@router.callback_query(AdminFlow.plan_validity, F.data.startswith("fit_adm_plan_validity:"))
async def cb_adm_plan_validity(cb: CallbackQuery, bot: Bot, state: FSMContext, config: BookingFitnessConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await _clear_flow_keep_panel(state)
        await _replace_panel(bot, state, chat_id, "⏳ Время ожидания истекло.", reply_markup=kb_admin_panel_main()); return
    validity = int(cb.data.split(":", 1)[1]) or None
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute(
            "INSERT INTO subscription_plans (name, total_visits, validity_days) VALUES (?,?,?)",
            (data["plan_name"], data["plan_visits"], validity),
        )
        await db.commit()
    await _clear_flow_keep_panel(state)
    await _replace_panel(
        bot, state, chat_id, "✅ Тариф добавлен.\n\n🎟 <b>Тарифы абонементов</b>",
        reply_markup=await kb_plans_admin(config.db_path),
    )


# ── admin: issue subscription (payment substitute — no money involved) ──────

@router.callback_query(F.data == "fit_adm_issue")
async def cb_adm_issue(cb: CallbackQuery, bot: Bot, state: FSMContext, config: BookingFitnessConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    await state.update_data(started_at=time.time())
    await _replace_panel(bot, state, chat_id, "Выберите клиента:", reply_markup=await kb_issue_pick_client(config.db_path))

@router.callback_query(F.data == "fit_adm_issue_manual")
async def cb_adm_issue_manual(cb: CallbackQuery, bot: Bot, state: FSMContext, config: BookingFitnessConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    await state.set_state(AdminFlow.issue_client)
    await _replace_panel(bot, state, chat_id, "Пришлите Telegram ID клиента:", reply_markup=kb_private_cancel("fit_adm_flow_cancel"))

@router.message(AdminFlow.issue_client, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def adm_issue_client_text(msg: Message, bot: Bot, state: FSMContext, config: BookingFitnessConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await _clear_flow_keep_panel(state)
        await msg.answer("⏳ Время ожидания истекло — начните заново."); return
    if not _is_bot_admin(msg.from_user.id, config):
        await _clear_flow_keep_panel(state); return
    text = msg.text.strip()
    if not _valid_telegram_id(text):
        await msg.answer("Пришлите числовой Telegram ID."); return
    await state.update_data(issue_user_id=int(text))
    await _replace_panel(bot, state, msg.chat.id, "Выберите тариф:", reply_markup=await kb_issue_pick_plan(config.db_path))

@router.callback_query(F.data.startswith("fit_adm_issue_pick:"))
async def cb_adm_issue_pick(cb: CallbackQuery, bot: Bot, state: FSMContext, config: BookingFitnessConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    user_id = int(cb.data.split(":", 1)[1])
    await state.update_data(issue_user_id=user_id)
    await _replace_panel(bot, state, chat_id, "Выберите тариф:", reply_markup=await kb_issue_pick_plan(config.db_path))

@router.callback_query(F.data.startswith("fit_adm_issue_plan:"))
async def cb_adm_issue_plan(cb: CallbackQuery, bot: Bot, state: FSMContext, config: BookingFitnessConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    data = await state.get_data()
    if _flow_expired(data) or "issue_user_id" not in data:
        await _clear_flow_keep_panel(state)
        await _replace_panel(bot, state, chat_id, "⏳ Время ожидания истекло.", reply_markup=kb_admin_panel_main()); return
    plan_id = int(cb.data.split(":", 1)[1])
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        plan = await (await db.execute(
            "SELECT name, total_visits, validity_days FROM subscription_plans WHERE id=?", (plan_id,)
        )).fetchone()
        if plan is None:
            await db.commit()
            await _clear_flow_keep_panel(state)
            await _replace_panel(bot, state, chat_id, "Тариф не найден.", reply_markup=kb_admin_panel_main()); return
        expires_at = None
        if plan["validity_days"]:
            expires_at = (date.today() + timedelta(days=plan["validity_days"])).isoformat()
        await db.execute(
            "INSERT INTO subscriptions (user_id, plan_name, total_visits, visits_left, expires_at) VALUES (?,?,?,?,?)",
            (data["issue_user_id"], plan["name"], plan["total_visits"], plan["total_visits"], expires_at),
        )
        await db.commit()
    await _clear_flow_keep_panel(state)
    await _replace_panel(
        bot, state, chat_id, f"✅ Абонемент «{_esc(plan['name'])}» выдан клиенту {data['issue_user_id']}.",
        reply_markup=kb_admin_panel_main(),
    )
    try:
        await bot.send_message(
            data["issue_user_id"],
            f"🎟 Вам выдан абонемент «{_esc(plan['name'])}» — {plan['total_visits']} посещений.",
            parse_mode="HTML",
        )
    except Exception:
        pass


# ── admin: bot-admins panel (same pattern as moderator.py) ──────────────────

async def _admins_list_text(config: BookingFitnessConfig) -> str:
    ids = sorted(_load_admins(config.admins_file))
    if not ids:
        return "👥 Пусто"
    return _join_bounded(["👥 <b>Администраторы бота:</b>\n"] + [f"• <code>{_esc(i)}</code>" for i in ids])

@router.callback_query(F.data == "fit_adm_admins")
async def cb_adm_admins(cb: CallbackQuery, bot: Bot, state: FSMContext, config: BookingFitnessConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    await _clear_flow_keep_panel(state)
    await _replace_panel(bot, state, chat_id, await _admins_list_text(config), reply_markup=kb_admins_panel())

@router.callback_query(F.data == "fit_adm_addadmin")
async def cb_adm_addadmin(cb: CallbackQuery, bot: Bot, state: FSMContext, config: BookingFitnessConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    await _replace_panel(
        bot, state, chat_id, "Пришлите Telegram ID пользователя, которого нужно сделать администратором:",
        reply_markup=kb_private_cancel("fit_adm_admins"),
    )
    await state.update_data(started_at=time.time())
    await state.set_state(AdminFlow.add_admin)

@router.message(AdminFlow.add_admin, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def adm_add_admin(msg: Message, bot: Bot, state: FSMContext, config: BookingFitnessConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await _clear_flow_keep_panel(state)
        await msg.answer("⏳ Время ожидания истекло — начните заново кнопкой «👥 Админы бота»."); return
    if not _is_bot_admin(msg.from_user.id, config):
        await _clear_flow_keep_panel(state)
        await msg.answer("⛔ Нет доступа"); return
    text = msg.text.strip()
    if not _valid_telegram_id(text):
        await msg.answer("Пришлите числовой Telegram ID."); return
    ids = _load_admins(config.admins_file); ids.add(text); _save_admins(config.admins_file, ids)
    logger.info(f"adm_add_admin: {text} added as bot admin by {msg.from_user.id}")
    await _clear_flow_keep_panel(state)
    await msg.answer(f"✅ <code>{_esc(text)}</code> добавлен.", parse_mode="HTML")
    await _replace_panel(bot, state, msg.chat.id, await _admins_list_text(config), reply_markup=kb_admins_panel())

@router.callback_query(F.data == "fit_adm_removeadmin")
async def cb_adm_removeadmin(cb: CallbackQuery, bot: Bot, state: FSMContext, config: BookingFitnessConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    ids = sorted(_load_admins(config.admins_file))
    if len(ids) <= 1:
        await _replace_panel(
            bot, state, chat_id, "⚠️ Нельзя удалить единственного администратора.\n\n" + await _admins_list_text(config),
            reply_markup=kb_admins_panel(),
        )
        return
    if len(ids) <= MAX_ADMIN_REMOVE_BUTTONS:
        await state.update_data(remove_admin_ids=ids)
        await _replace_panel(bot, state, chat_id, "Выберите администратора для удаления:", reply_markup=kb_remove_admins(ids))
    else:
        await state.update_data(started_at=time.time())
        await state.set_state(AdminFlow.remove_admin_pick)
        await _replace_panel(bot, state, chat_id, "Пришлите Telegram ID администратора для удаления:", reply_markup=kb_private_cancel("fit_adm_admins"))

@router.callback_query(F.data.startswith("fit_adm_rma:"))
async def cb_adm_rma(cb: CallbackQuery, bot: Bot, state: FSMContext, config: BookingFitnessConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    data = await state.get_data()
    ids = data.get("remove_admin_ids", [])
    try:
        idx = int(cb.data.split(":", 1)[1])
    except ValueError:
        idx = -1
    if idx < 0 or idx >= len(ids):
        await _replace_panel(bot, state, chat_id, await _admins_list_text(config), reply_markup=kb_admins_panel()); return
    target = ids[idx]
    current = _load_admins(config.admins_file)
    if target in current and len(current) <= 1:
        await _replace_panel(
            bot, state, chat_id, "⚠️ Нельзя удалить единственного администратора.\n\n" + await _admins_list_text(config),
            reply_markup=kb_admins_panel(),
        )
        return
    current.discard(target); _save_admins(config.admins_file, current)
    logger.info(f"cb_adm_rma: {target} removed as bot admin by {cb.from_user.id}")
    await _clear_flow_keep_panel(state)
    await _replace_panel(bot, state, chat_id, await _admins_list_text(config), reply_markup=kb_admins_panel())

@router.message(AdminFlow.remove_admin_pick, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def adm_remove_admin_text(msg: Message, bot: Bot, state: FSMContext, config: BookingFitnessConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await _clear_flow_keep_panel(state)
        await msg.answer("⏳ Время ожидания истекло — начните заново."); return
    if not _is_bot_admin(msg.from_user.id, config):
        await _clear_flow_keep_panel(state); return
    text = msg.text.strip()
    if not _valid_telegram_id(text):
        await msg.answer("Пришлите числовой Telegram ID."); return
    ids = _load_admins(config.admins_file)
    if text in ids and len(ids) <= 1:
        await msg.answer("⚠️ Нельзя удалить единственного администратора."); return
    ids.discard(text); _save_admins(config.admins_file, ids)
    logger.info(f"adm_remove_admin_text: {text} removed as bot admin by {msg.from_user.id}")
    await _clear_flow_keep_panel(state)
    await _replace_panel(bot, state, msg.chat.id, await _admins_list_text(config), reply_markup=kb_admins_panel())


# ── admin: stats ─────────────────────────────────────────────────────────────

@router.callback_query(F.data == "fit_adm_stats")
async def cb_adm_stats(cb: CallbackQuery, bot: Bot, state: FSMContext, config: BookingFitnessConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    since = (date.today() - timedelta(days=STATS_RECENT_DAYS)).isoformat()
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        total = (await (await db.execute("SELECT COUNT(*) FROM bookings")).fetchone())[0]
        confirmed = (await (await db.execute("SELECT COUNT(*) FROM bookings WHERE status='confirmed'")).fetchone())[0]
        waitlisted = (await (await db.execute("SELECT COUNT(*) FROM bookings WHERE status='waitlist'")).fetchone())[0]
        active_subs = (await (await db.execute(
            "SELECT COUNT(*) FROM subscriptions WHERE visits_left>0 AND (expires_at IS NULL OR expires_at>=date('now','localtime'))"
        )).fetchone())[0]

        # Attendance by trainer over the last STATS_RECENT_DAYS: confirmed
        # bookings for sessions that actually happened in that window
        # (slot_date, not booking created_at — "attendance" means the
        # session date, a booking made today for next month isn't
        # attendance yet). Paired with each trainer's rating summary so the
        # owner sees both numbers together without a second screen.
        attendance_rows = await (await db.execute(
            "SELECT t.id, t.name, COUNT(*) AS visits FROM bookings b "
            "JOIN slots s ON b.slot_id=s.id JOIN trainers t ON s.trainer_id=t.id "
            "WHERE b.status='confirmed' AND s.slot_date >= ? AND s.slot_date <= date('now','localtime') "
            "GROUP BY t.id ORDER BY visits DESC LIMIT 5",
            (since,),
        )).fetchall()
        trainer_lines = []
        for r in attendance_rows:
            avg, count = await _trainer_rating_summary(db, r["id"])
            rating_note = f", ⭐{avg} ({count})" if count else ""
            trainer_lines.append(f"  • {_esc(r['name'])}: {r['visits']} посещений{rating_note}")

        # Churn heuristic (owner-requested, deliberately simple): clients
        # whose subscription lapsed in the last STATS_RECENT_DAYS and who
        # have NOT purchased a newer one since — no payment/LTV modeling,
        # just "did they come back after their last subscription ran out."
        churned = (await (await db.execute(
            "SELECT COUNT(DISTINCT s1.user_id) FROM subscriptions s1 "
            "WHERE s1.expires_at IS NOT NULL AND s1.expires_at < date('now','localtime') AND s1.expires_at >= ? "
            "AND NOT EXISTS (SELECT 1 FROM subscriptions s2 WHERE s2.user_id = s1.user_id AND s2.id != s1.id "
            "AND s2.purchased_at > s1.expires_at)",
            (since,),
        )).fetchone())[0]

    text_lines = [
        "📊 <b>Статистика</b>\n",
        f"📋 Всего записей: {total}",
        f"✅ Подтверждённых: {confirmed}",
        f"⏳ В листе ожидания: {waitlisted}",
        f"💳 Активных абонементов: {active_subs}\n",
        f"👥 Посещаемость по тренерам (последние {STATS_RECENT_DAYS} дн.):",
    ]
    text_lines.extend(trainer_lines or ["  • нет данных"])
    text_lines.append(f"\n📉 Клиентов без продления за {STATS_RECENT_DAYS} дн. (отток): {churned}")
    await _replace_panel(bot, state, chat_id, "\n".join(text_lines), reply_markup=kb_admin_panel_main())


# ── subscription reminder loop ──────────────────────────────────────────────

async def _subscription_reminder_loop(bot: Bot, db_path: str, bot_name: str) -> None:
    """Periodically DMs clients whose subscription is running low on visits or
    close to expiry, once each (persisted *_notified flags, not an in-memory
    set — a subscription's low-balance state can easily outlive a process
    restart, unlike trip_manager.py's daily digest which resets naturally
    every calendar day). Also re-runs the rolling slot generators so the
    booking window keeps sliding forward as days pass, since — unlike
    init_db(), which only runs once at boot — nothing else in this template
    calls them again."""
    while True:
        try:
            await _ensure_group_sessions(db_path)
            await _ensure_individual_slots(db_path)
            async with aiosqlite.connect(db_path) as db:
                db.row_factory = aiosqlite.Row
                low = await (await db.execute(
                    "SELECT id, user_id, plan_name, visits_left FROM subscriptions "
                    "WHERE visits_left <= ? AND visits_left > 0 AND low_balance_notified=0",
                    (LOW_VISITS_THRESHOLD,),
                )).fetchall()
                for row in low:
                    try:
                        await bot.send_message(
                            row["user_id"],
                            f"⚠️ У вашего абонемента «{_esc(row['plan_name'])}» осталось всего "
                            f"{row['visits_left']} посещений. Пора продлить!",
                            parse_mode="HTML",
                        )
                    except Exception as e:
                        logger.warning(f"[{bot_name}] low-balance reminder send error for {row['user_id']}: {e}")
                    await db.execute("UPDATE subscriptions SET low_balance_notified=1 WHERE id=?", (row["id"],))
                await db.commit()

                soon = (date.today() + timedelta(days=EXPIRING_SOON_DAYS)).isoformat()
                expiring = await (await db.execute(
                    "SELECT id, user_id, plan_name, expires_at FROM subscriptions "
                    "WHERE expires_at IS NOT NULL AND expires_at <= ? AND expires_at >= date('now','localtime') "
                    "AND expiry_notified=0",
                    (soon,),
                )).fetchall()
                for row in expiring:
                    try:
                        await bot.send_message(
                            row["user_id"],
                            f"⏳ Ваш абонемент «{_esc(row['plan_name'])}» истекает {row['expires_at']}. Пора продлить!",
                            parse_mode="HTML",
                        )
                    except Exception as e:
                        logger.warning(f"[{bot_name}] expiry reminder send error for {row['user_id']}: {e}")
                    await db.execute("UPDATE subscriptions SET expiry_notified=1 WHERE id=?", (row["id"],))
                await db.commit()

                # Autorenew: subscriptions that have ALREADY lapsed
                # (expires_at strictly in the past — distinct from the
                # "expiring soon" reminder above, which fires BEFORE
                # expiry) with autorenew=1 and not yet processed. Re-issues
                # a fresh subscription under the SAME plan NAME, same shape
                # as cb_adm_issue_plan's manual issue path — no payment
                # step exists anywhere in this template, so "autorenew"
                # here means "the owner doesn't have to manually re-issue
                # it," not an actual charge. autorenew_done is set
                # regardless of whether a matching active plan still
                # exists, so a deleted/deactivated plan doesn't retry this
                # client every poll forever — the owner sees the client
                # dropped off via the churn stat instead.
                expired_autorenew = await (await db.execute(
                    "SELECT id, user_id, plan_name FROM subscriptions "
                    "WHERE autorenew=1 AND autorenew_done=0 AND expires_at IS NOT NULL "
                    "AND expires_at < date('now','localtime')",
                )).fetchall()
                for row in expired_autorenew:
                    plan = await (await db.execute(
                        "SELECT name, total_visits, validity_days FROM subscription_plans "
                        "WHERE name=? AND active=1 ORDER BY id DESC LIMIT 1",
                        (row["plan_name"],),
                    )).fetchone()
                    if plan is not None:
                        new_expires_at = (
                            (date.today() + timedelta(days=plan["validity_days"])).isoformat()
                            if plan["validity_days"] else None
                        )
                        await db.execute(
                            "INSERT INTO subscriptions (user_id, plan_name, total_visits, visits_left, expires_at, autorenew) "
                            "VALUES (?,?,?,?,?,1)",
                            (row["user_id"], plan["name"], plan["total_visits"], plan["total_visits"], new_expires_at),
                        )
                        try:
                            await bot.send_message(
                                row["user_id"],
                                f"🔁 Абонемент «{_esc(plan['name'])}» автопродлён — {plan['total_visits']} посещений.",
                                parse_mode="HTML",
                            )
                        except Exception as e:
                            logger.warning(f"[{bot_name}] autorenew notify error for {row['user_id']}: {e}")
                    await db.execute("UPDATE subscriptions SET autorenew_done=1 WHERE id=?", (row["id"],))
                await db.commit()
        except Exception:
            logger.exception(f"[{bot_name}] subscription reminder loop error")
        await asyncio.sleep(REMINDER_POLL_SECONDS)


# ── MAIN ──────────────────────────────────────────────────────────────────────

async def main():
    config = config_from_env()
    bot = Bot(token=os.getenv("BOT_TOKEN"))
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(ConfigMiddleware(config))
    dp.include_router(router)
    await bot.set_my_description(BOT_DESCRIPTION)
    await init_db(config.db_path)
    # Kept as a local so the task object isn't garbage-collected mid-run —
    # asyncio only holds a weak reference to a "fire and forget" task
    # (devops-review-found: the discarded-return-value form used elsewhere
    # in the repo is a known asyncio footgun).
    reminder_task = asyncio.create_task(_subscription_reminder_loop(bot, config.db_path, config.bot_name))
    try:
        await dp.start_polling(bot)
    finally:
        reminder_task.cancel()

if __name__ == "__main__":
    asyncio.run(main())
