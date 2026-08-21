# TEMPLATE: boss_bot
# USE FOR: начальник надиктовывает задачи голосом — бот распознаёт речь, сохраняет
#          задачу (название/описание/дедлайн/на кого) и публикует её связанному
#          manager_bot через office_events; напоминает о просрочке через reminders
# CUSTOMIZE: sections marked with # CUSTOMIZE
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

from db.database import add_bot_admin
from features import office_events, voice_intake
from features.office_events import TaskAssignedEvent, TaskCompletedEvent
from features.voice_intake import VoiceFieldSpec, VoiceRecordType, VoiceSchema

# ── CUSTOMIZE ────────────────────────────────────────────────────────────────
BOT_DESCRIPTION = "Начальник надиктовывает задачи голосом — бот сохраняет их и передаёт связанному боту-менеджеру."
WELCOME_TEXT = (
    "👔 <b>Бот начальника</b>\n\n"
    "Отправьте голосовое сообщение с задачей — я распознаю название, описание, "
    "срок и исполнителя, сохраню и передам связанному боту.\n\n"
    "Выберите действие:"
)
# ── END CUSTOMIZE ─────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
router = Router()


# ── mini-app config (see docs/MINIAPP_DESIGN.md, templates/tour_operator.py's
# own miniapp_config for the pilot) — creation is voice-only, so "tasks" is
# read-only through the mini-app (creatable: False at the resource level and
# on every field, matching the convention templates/event_rsvp.py's
# non-creatable fields use for server-set columns).
miniapp_config = {
    "resources": [
        {
            "name": "tasks",
            "table": "tasks",
            "order_by": "created_at DESC",
            "creatable": False,
            "title": "Задачи",
            "titleField": "title",
            "fields": [
                {"name": "title", "label": "Название", "kind": "text", "list": True, "detail": True, "create": False},
                {"name": "description", "label": "Описание", "kind": "text", "list": False, "detail": True, "create": False},
                {"name": "deadline", "label": "Срок", "kind": "date", "list": True, "detail": True, "create": False},
                {"name": "status", "label": "Статус", "kind": "status", "list": True, "detail": True, "create": False},
                {"name": "assignee_hint", "label": "Исполнитель", "kind": "text", "list": False, "detail": True, "create": False},
            ],
        },
    ],
}


# ── reminders (features/reminders.py, docs/REMINDERS_DESIGN.md) ────────────
# offsets_hours=[-1]: fires roughly 1 hour AFTER the stored deadline instant
# (a negative offset means "target = deadline + offset_hours", so -1 here
# actually shifts the target FORWARD by... no — see features/reminders.py's
# ReminderRule: target = date + timedelta(hours=offset_hours). offset_hours=-1
# on a deadline that has ALREADY passed by ~1h is what we want to catch as
# "now overdue" — i.e. offset_hours=-1 makes the sweep look for rows whose
# deadline is ~1 hour in the FUTURE relative to "now - 1h", which is exactly
# "deadline was 1 hour ago". recipient_field=boss_chat_id (same row) —
# tasks.boss_chat_id is set at save time from the voice message's sender.
reminders_config = {
    "rules": [
        {
            "id": "boss_bot_task_overdue",
            "table": "tasks",
            "date_field": "deadline",
            "date_format": "%Y-%m-%dT%H:%M:%S",
            "recipient_field": "boss_chat_id",
            "offsets_hours": [-1],
            "active_field": "status != 'done'",
            "message_template": "⏰ Просрочена задача «{title}» (срок был {deadline:%d.%m %H:%M})",
        },
    ],
}


# ── config ───────────────────────────────────────────────────────────────────
@dataclass
class BossBotConfig:
    bot_name: str
    db_path: str
    admins_file: str
    welcome_image: Path
    display_name: str | None = None
    group_chat_id: str | None = None
    bot_id: int | None = None
    owner_telegram_id: int | None = None


def _paths_for(name: str, data_dir: Path) -> BossBotConfig:
    return BossBotConfig(
        bot_name=name,
        db_path=str(data_dir / f"{name}_data.db"),
        admins_file=str(data_dir / f"admins_{name}.json"),
        welcome_image=data_dir / "bot_images" / f"{name}.jpg",
    )


def config_from_env() -> BossBotConfig:
    """Standalone/subprocess mode."""
    name = Path(__file__).stem
    data_dir = Path(os.getenv("DATA_DIR", "./data"))
    data_dir.mkdir(exist_ok=True)
    return _paths_for(name, data_dir)


def config_from_bot_row(bot_row: dict, data_dir: Path) -> BossBotConfig:
    """Webhook runtime mode. Paths built from bot_row["bot_id"] (bots.id, the
    physically unique AUTOINCREMENT PK) — same isolation convention as every
    other template's config_from_bot_row (see docs/STAGE2_DESIGN.md
    "Изоляция по bots.id"). Registers this bot's voice-intake schema here,
    since bot_id is only known from this point on and the schema's on_saved
    hook needs bot_id captured in a closure to call office_events.publish_event
    (mirrors templates/tour_operator.py's config_from_bot_row, which registers
    its own schema at the exact same point for the exact same reason)."""
    bot_id = bot_row["bot_id"]
    config = BossBotConfig(
        bot_name=bot_row["name"],
        db_path=str(data_dir / f"bot_{bot_id}_data.db"),
        admins_file=str(data_dir / f"admins_{bot_id}.json"),
        welcome_image=data_dir / "bot_images" / f"bot_{bot_id}.jpg",
    )
    config.display_name = bot_row.get("display_name")
    config.group_chat_id = bot_row.get("group_chat_id")
    config.bot_id = bot_id
    config.owner_telegram_id = bot_row.get("owner_telegram_id")
    voice_intake.register_schema(bot_id, _build_voice_schema(bot_id))
    return config


class ConfigMiddleware:
    """Injects this bot's BossBotConfig into every handler's data["config"] —
    same self-contained pattern as every other templates/*.py."""

    def __init__(self, config: BossBotConfig) -> None:
        self.config = config

    async def __call__(self, handler, event, data):
        data["config"] = self.config
        return await handler(event, data)


# ── admin helpers ─────────────────────────────────────────────────────────────
def _load_admins(admins_file: str) -> set:
    try:
        with open(admins_file) as f:
            data = json.load(f)
        return set(data.get("ids", []) if isinstance(data, dict) else data)
    except Exception:
        return set()


def _save_admins(admins_file: str, ids: set) -> None:
    with open(admins_file, "w") as f:
        json.dump({"ids": list(ids)}, f)


def _is_admin(user_id: int, config: BossBotConfig) -> bool:
    # The DB-known owner (bots.owner_telegram_id) is always an admin, even if
    # the local admins_file is empty/stale/hijacked — see cmd_start below for
    # why the file alone can't be trusted as the sole source of truth.
    if config.owner_telegram_id is not None and str(user_id) == str(config.owner_telegram_id):
        return True
    return str(user_id) in _load_admins(config.admins_file)


# ── db ────────────────────────────────────────────────────────────────────────
async def init_db(db_path: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                title          TEXT NOT NULL,
                description    TEXT,
                deadline       TEXT,
                assignee_hint  TEXT,
                status         TEXT NOT NULL DEFAULT 'open',
                boss_chat_id   INTEGER,
                created_at     TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.commit()
    from features.reminders import init_reminders_tables
    await init_reminders_tables(db_path)


# ── voice_intake schema (features/voice_intake.py) ──────────────────────────
# Closure mode (host-registered), same as templates/tour_operator.py's
# _build_voice_schema — NOT data-driven mode, per the plan's decision to test
# against today's host-registered voice_intake.
async def _get_context_id(db_path: str, user_id: int) -> int:
    """No real "active X" concept for boss_bot — a task always attaches
    directly to the user who dictated it, so context resolution is trivial
    (always succeeds, never blocks saving). Returning the user_id itself
    just gives _resolve_context_id a non-None value; the save closure below
    doesn't even use it (task rows have no context/parent column)."""
    return user_id


def _build_voice_schema(bot_id: int) -> VoiceSchema:
    async def _save_task(db_path: str, _context_id, d: dict) -> int:
        async with aiosqlite.connect(db_path) as db:
            cur = await db.execute(
                "INSERT INTO tasks (title, description, deadline, assignee_hint, boss_chat_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    d.get("title", "Задача"), d.get("description"), d.get("deadline"),
                    d.get("assignee_hint"), _context_id,
                ),
            )
            await db.commit()
            return cur.lastrowid

    async def _on_task_saved(db_path: str, user_id: int, task_id) -> None:
        """Publishes task.assigned to any bot linked via bot_office_links —
        bot_id is captured in this closure at schema-registration time (see
        config_from_bot_row above), since on_saved's own signature
        (db_path, user_id, save_result) carries no bot_id — same reasoning
        the plan/module docstring gives for why this can't come from
        voice_intake itself."""
        if task_id is None:
            return
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute("SELECT * FROM tasks WHERE id=?", (task_id,))).fetchone()
        if row is None:
            return
        try:
            await office_events.publish_event(
                bot_id, "task.assigned",
                TaskAssignedEvent(
                    task_id=row["id"],
                    title=row["title"] or "",
                    description=row["description"] or "",
                    deadline=row["deadline"] or "",
                    assignee_hint=row["assignee_hint"] or "",
                    boss_chat_id=row["boss_chat_id"] or user_id,
                ),
            )
        except Exception:
            logger.exception(f"boss_bot: failed to publish task.assigned for task_id={task_id} bot_id={bot_id}")

    return VoiceSchema(
        get_context_id=_get_context_id,
        no_context_message="",  # never shown — _get_context_id always resolves
        language_code="ru",
        record_types=[
            VoiceRecordType(
                key="task", label="Задача", icon="📋",
                prompt_desc="title, description, deadline(YYYY-MM-DDTHH:MM:SS), assignee_hint",
                fields=[
                    VoiceFieldSpec("title", "Название"),
                    VoiceFieldSpec("description", "Описание"),
                    VoiceFieldSpec("deadline", "Срок"),
                    VoiceFieldSpec("assignee_hint", "Исполнитель"),
                ],
                save=_save_task,
                on_saved=_on_task_saved,
            ),
        ],
    )


# ── office_events subscriber: task.completed back-sync ──────────────────────
# Auto-wired by runtime/registry.py's build_entry() the same by-convention
# way manager_bot.py's own on_office_event is — see that module's docstring.
# Clears tasks.status here so features/reminders.py's boss_bot_task_overdue
# rule (which filters on status != 'done') stops firing for a task that was
# actually finished before its deadline; without this, a manager marking a
# task done in manager_bot's OWN incoming_tasks table never touched this
# row, and the boss kept getting "просрочена" pings for work that was
# already complete.
async def on_office_event(event, config: BossBotConfig) -> None:
    if event.event_type != "task.completed":
        return
    payload = event.payload
    if not isinstance(payload, TaskCompletedEvent):
        return
    try:
        async with aiosqlite.connect(config.db_path) as db:
            await db.execute(
                "UPDATE tasks SET status='done' WHERE id=? AND status != 'done'", (payload.task_id,)
            )
            await db.commit()
    except Exception:
        logger.exception(
            f"boss_bot: failed to apply task.completed for task_id={payload.task_id} bot_id={config.bot_id}"
        )


# ── keyboards ─────────────────────────────────────────────────────────────────
def kb_main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Мои задачи", callback_data="bb_tasks")],
        [InlineKeyboardButton(text="🖥 Веб-кабинет", callback_data="bb_office")],
        [InlineKeyboardButton(text="🌐 Веб-приложение", callback_data="bb_app")],
    ])


def kb_back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="bb_main")]])


# ── /start ────────────────────────────────────────────────────────────────────
@router.message(Command("start"), F.chat.type == "private")
async def cmd_start(message: Message, state: FSMContext, config: BossBotConfig):
    await state.clear()
    admins = _load_admins(config.admins_file)
    sender_id = message.from_user.id
    # Bug fixed here: this used to grant admin to whoever sent /start FIRST,
    # which lets anyone who messages the bot before the owner does
    # permanently seize the admin panel (boss_bot has no separate client
    # menu — non-admins just get "Нет доступа"). When bots.owner_telegram_id
    # is known (webhook/production mode), only that user may claim the
    # empty-admins bootstrap slot. In standalone/env mode (owner_telegram_id
    # unknown) the old first-comer behavior is kept as the only option
    # available.
    is_owner = config.owner_telegram_id is not None and sender_id == config.owner_telegram_id
    if not admins and (is_owner or config.owner_telegram_id is None):
        _save_admins(config.admins_file, {str(sender_id)})
        admins = {str(sender_id)}
        if config.bot_id is not None:
            try:
                await add_bot_admin(config.bot_id, str(sender_id))
            except Exception as e:
                logger.warning(f"cmd_start: add_bot_admin sync failed for bot {config.bot_id}: {e}")
    if not _is_admin(sender_id, config):
        await message.answer("⛔ Нет доступа.")
        return
    if config.welcome_image.exists():
        await message.answer_photo(
            FSInputFile(str(config.welcome_image)), caption=WELCOME_TEXT,
            parse_mode="HTML", reply_markup=kb_main_menu(),
        )
    else:
        await message.answer(WELCOME_TEXT, parse_mode="HTML", reply_markup=kb_main_menu())


def _miniapp_url(bot_id: int, telegram_user_id: int) -> str | None:
    from runtime.miniapp_api import mint_magic_link_token

    try:
        token = mint_magic_link_token(bot_id, telegram_user_id)
    except RuntimeError:
        return None
    RAILWAY_DOMAIN = os.getenv("RAILWAY_PUBLIC_DOMAIN", "")
    PORT = int(os.getenv("PORT", "8080"))
    base_url = f"https://{RAILWAY_DOMAIN}" if RAILWAY_DOMAIN else f"http://localhost:{PORT}"
    return f"{base_url}/app/{bot_id}?token={token}"


@router.callback_query(F.data == "bb_main")
async def cb_main(cb, state: FSMContext, config: BossBotConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    await cb.message.edit_text(WELCOME_TEXT, parse_mode="HTML", reply_markup=kb_main_menu())


@router.callback_query(F.data == "bb_app")
async def cb_app(cb, config: BossBotConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    url = _miniapp_url(config.bot_id, cb.from_user.id) if config.bot_id is not None else None
    if not url:
        await cb.message.edit_text(
            "🌐 Веб-приложение временно недоступно (не настроен MINIAPP_SECRET).",
            reply_markup=kb_back(),
        )
        return
    await cb.message.edit_text(
        f'🌐 <a href="{url}">Открыть →</a>\n<code>{url}</code>',
        parse_mode="HTML", disable_web_page_preview=True, reply_markup=kb_back(),
    )


def _office_dashboard_url(bot_id: int, telegram_user_id: int) -> str | None:
    """Same magic-link auth as _miniapp_url, but points at /office/{bot_id} —
    the plain-browser, desktop-oriented dashboard (runtime/miniapp_api.py's
    office_dashboard_handler), NOT the Telegram-WebApp-shaped SPA under
    /app/{bot_id}. Separate route/token issuance from _miniapp_url on
    purpose: same signing mechanism, different surface, so either can be
    disabled/changed independently later."""
    from runtime.miniapp_api import mint_magic_link_token

    try:
        token = mint_magic_link_token(bot_id, telegram_user_id)
    except RuntimeError:
        return None
    RAILWAY_DOMAIN = os.getenv("RAILWAY_PUBLIC_DOMAIN", "")
    PORT = int(os.getenv("PORT", "8080"))
    base_url = f"https://{RAILWAY_DOMAIN}" if RAILWAY_DOMAIN else f"http://localhost:{PORT}"
    return f"{base_url}/office/{bot_id}?token={token}"


@router.callback_query(F.data == "bb_office")
async def cb_office(cb, config: BossBotConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    url = _office_dashboard_url(config.bot_id, cb.from_user.id) if config.bot_id is not None else None
    if not url:
        await cb.message.edit_text(
            "🖥 Веб-кабинет временно недоступен (не настроен MINIAPP_SECRET).",
            reply_markup=kb_back(),
        )
        return
    await cb.message.edit_text(
        f'🖥 <b>Веб-кабинет</b> — все задачи всех сотрудников, удобно с ноутбука:\n\n'
        f'<a href="{url}">Открыть →</a>\n<code>{url}</code>',
        parse_mode="HTML", disable_web_page_preview=True, reply_markup=kb_back(),
    )


MAX_LIST_ROWS = 40


@router.callback_query(F.data == "bb_tasks")
async def cb_tasks(cb, config: BossBotConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (MAX_LIST_ROWS,)
        )).fetchall()
    if not rows:
        await cb.message.edit_text(
            "📋 <b>Задачи</b>\n\n— пока пусто, отправьте голосовое —",
            parse_mode="HTML", reply_markup=kb_back(),
        )
        return
    lines = ["📋 <b>Задачи</b>\n"]
    for r in rows:
        mark = "✅" if r["status"] == "done" else "🔸"
        deadline = f" · до {r['deadline']}" if r["deadline"] else ""
        lines.append(f"{mark} {r['title']}{deadline}")
    await cb.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=kb_back())


# ── MAIN ──────────────────────────────────────────────────────────────────────
async def main():
    config = config_from_env()
    bot = Bot(token=os.getenv("BOT_TOKEN"))
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(ConfigMiddleware(config))
    dp.include_router(router)
    dp.include_router(voice_intake.router)
    await bot.set_my_description(BOT_DESCRIPTION)
    await init_db(config.db_path)
    voice_intake.register_schema(0, _build_voice_schema(0))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
