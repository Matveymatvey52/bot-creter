# TEMPLATE: manager_bot
# USE FOR: получает задачи от связанного boss_bot через office_events, копирует
#          их к себе (incoming_tasks) и уведомляет зарегистрированных менеджеров;
#          менеджер отмечает задачу "выполнено" локально (без обратной синхронизации)
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
from features import office_events

# ── CUSTOMIZE ────────────────────────────────────────────────────────────────
BOT_DESCRIPTION = "Получает задачи от связанного бота-начальника и уведомляет менеджеров."
WELCOME_TEXT = (
    "🗂️ <b>Бот менеджера</b>\n\n"
    "Здесь появляются задачи от связанного бота-начальника. "
    "Отметьте задачу выполненной кнопкой, когда закончите.\n\n"
    "Выберите действие:"
)
# ── END CUSTOMIZE ─────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
router = Router()


# ── mini-app config ──────────────────────────────────────────────────────────
miniapp_config = {
    "resources": [
        {
            "name": "incoming_tasks",
            "table": "incoming_tasks",
            "order_by": "created_at DESC",
            "creatable": False,
            "scope": {"type": "global"},
            "title": "Входящие задачи",
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


# ── config ───────────────────────────────────────────────────────────────────
@dataclass
class ManagerBotConfig:
    bot_name: str
    db_path: str
    admins_file: str
    welcome_image: Path
    display_name: str | None = None
    group_chat_id: str | None = None
    bot_id: int | None = None
    owner_telegram_id: int | None = None


def _paths_for(name: str, data_dir: Path) -> ManagerBotConfig:
    return ManagerBotConfig(
        bot_name=name,
        db_path=str(data_dir / f"{name}_data.db"),
        admins_file=str(data_dir / f"admins_{name}.json"),
        welcome_image=data_dir / "bot_images" / f"{name}.jpg",
    )


def config_from_env() -> ManagerBotConfig:
    name = Path(__file__).stem
    data_dir = Path(os.getenv("DATA_DIR", "./data"))
    data_dir.mkdir(exist_ok=True)
    return _paths_for(name, data_dir)


def config_from_bot_row(bot_row: dict, data_dir: Path) -> ManagerBotConfig:
    bot_id = bot_row["bot_id"]
    config = ManagerBotConfig(
        bot_name=bot_row["name"],
        db_path=str(data_dir / f"bot_{bot_id}_data.db"),
        admins_file=str(data_dir / f"admins_{bot_id}.json"),
        welcome_image=data_dir / "bot_images" / f"bot_{bot_id}.jpg",
    )
    config.display_name = bot_row.get("display_name")
    config.group_chat_id = bot_row.get("group_chat_id")
    config.bot_id = bot_id
    config.owner_telegram_id = bot_row.get("owner_telegram_id")
    return config


class ConfigMiddleware:
    def __init__(self, config: ManagerBotConfig) -> None:
        self.config = config

    async def __call__(self, handler, event, data):
        data["config"] = self.config
        return await handler(event, data)


# ── managers registry (bootstrap-style, {"ids": [...]} — same convention
# db.database.sync_bot_admins_json / voice_intake._is_admin already use) ────
def _load_managers(managers_file: str) -> set:
    try:
        with open(managers_file) as f:
            data = json.load(f)
        return set(data.get("ids", []) if isinstance(data, dict) else data)
    except Exception:
        return set()


def _save_managers(managers_file: str, ids: set) -> None:
    with open(managers_file, "w") as f:
        json.dump({"ids": list(ids)}, f)


def _managers_file(config: ManagerBotConfig) -> str:
    """Reuses admins_file itself as the managers list — this template has no
    separate "admin vs manager" distinction (a manager here IS the bot's
    admin, per the plan's "один manager_bot с несколькими
    пользователями-менеджерами внутри" decision), so a second file would
    only duplicate the same {"ids": [...]} the admin bootstrap already
    writes below."""
    return config.admins_file


def _is_manager(user_id: int, config: ManagerBotConfig) -> bool:
    # The DB-known owner (bots.owner_telegram_id) is always a manager, even
    # if the local managers file (== admins_file) is empty/stale/hijacked —
    # see cmd_start below for why the file alone can't be trusted as the
    # sole source of truth.
    if config.owner_telegram_id is not None and str(user_id) == str(config.owner_telegram_id):
        return True
    return str(user_id) in _load_managers(_managers_file(config))


# ── db ────────────────────────────────────────────────────────────────────────
async def init_db(db_path: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS incoming_tasks (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                source_task_id  INTEGER,
                source_bot_id   INTEGER NOT NULL,
                title           TEXT NOT NULL,
                description     TEXT,
                deadline        TEXT,
                assignee_hint   TEXT,
                status          TEXT NOT NULL DEFAULT 'new',
                boss_chat_id    INTEGER,
                created_at      TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.commit()


# ── office_events subscriber ─────────────────────────────────────────────────
# By-convention hook: a module-level on_office_event(event, config) is what
# makes runtime/registry.py's build_entry() register this template as an
# office_events subscriber for any bot linked to it via bot_office_links —
# same wiring templates/event_rsvp.py's own on_office_event uses. Called with
# ONLY (event, config), no Bot instance (see build_entry()'s
# _office_event_hook) — a live push therefore needs its OWN Bot lookup via
# the live Registry. Rather than adding a second, separately-bootstrapped
# RegistryHandle to this template (which would need its own set_registry()
# wired into runtime/combined_app.py's bootstrap — an extra moving part with
# its own chance of being forgotten), this module reuses
# features/office_events.py's ALREADY-WIRED _registry_handle via its module
# reference below — that module's set_registry() is called once at
# combined_app.py's bootstrap today, and nothing about reading its handle
# from here requires it to know about manager_bot.py at all. Mirrors how
# features/office_events.py's own report_critical_error() resolves a live
# Bot from ITS handle (_registry_handle.value.get(bot_id).bot) — same
# resolution shape, just borrowing an already-wired handle instead of
# duplicating one.
async def on_office_event(event, config: ManagerBotConfig) -> None:
    if event.event_type != "task.assigned":
        return
    payload = event.payload
    if payload is None:
        return
    try:
        async with aiosqlite.connect(config.db_path) as db:
            cur = await db.execute(
                "INSERT INTO incoming_tasks "
                "(source_task_id, source_bot_id, title, description, deadline, assignee_hint, boss_chat_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    getattr(payload, "task_id", None), event.source_bot_id,
                    getattr(payload, "title", "") or "Задача",
                    getattr(payload, "description", None),
                    getattr(payload, "deadline", None),
                    getattr(payload, "assignee_hint", None),
                    getattr(payload, "boss_chat_id", None),
                ),
            )
            await db.commit()
            incoming_id = cur.lastrowid
    except Exception:
        logger.exception(
            f"manager_bot.on_office_event: failed to record incoming_tasks row — "
            f"source_bot_id={event.source_bot_id} bot_id={config.bot_id}"
        )
        return

    await _notify_managers(config, getattr(payload, "title", "") or "Задача", incoming_id)


async def _notify_managers(config: ManagerBotConfig, title: str, incoming_id: int) -> None:
    """Best-effort live push to every registered manager, via the live
    Registry's own Bot instance for THIS bot (config.bot_id) — resolved
    fresh on every call, never cached, same posture
    features/office_events.py's report_critical_error() documents for why it
    re-resolves the registry/bot on every call rather than caching either.
    Degrades silently (logged, not raised) if the registry isn't available
    (e.g. under main.py's standalone polling process, or before
    combined_app.py's bootstrap has called set_registry()) — DB-only record
    from on_office_event above already happened by the time this runs, so a
    missing live push here never loses the underlying data, only the
    real-time notification."""
    if config.bot_id is None:
        return
    registry = office_events._registry_handle.value
    if registry is None:
        logger.info(
            f"manager_bot._notify_managers: no live registry — incoming_id={incoming_id} "
            f"recorded but no live push sent (expected outside the webhook runtime)"
        )
        return
    entry = registry.get(config.bot_id)
    if entry is None:
        logger.info(f"manager_bot._notify_managers: bot_id={config.bot_id} not in live registry — skipped push")
        return
    managers = _load_managers(_managers_file(config))
    if not managers:
        return
    text = f"📥 Новая задача: <b>{title}</b>"
    for manager_id in managers:
        try:
            await entry.bot.send_message(int(manager_id), text, parse_mode="HTML")
        except Exception as e:
            logger.warning(f"manager_bot._notify_managers: failed to notify manager_id={manager_id}: {e}")


# ── keyboards ─────────────────────────────────────────────────────────────────
def kb_main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 Входящие задачи", callback_data="mb_tasks")],
        [InlineKeyboardButton(text="🌐 Веб-приложение", callback_data="mb_app")],
    ])


def kb_back() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="mb_main")]])


# ── /start ────────────────────────────────────────────────────────────────────
@router.message(Command("start"), F.chat.type == "private")
async def cmd_start(message: Message, state: FSMContext, config: ManagerBotConfig):
    await state.clear()
    managers = _load_managers(_managers_file(config))
    sender_id = message.from_user.id
    # Bug fixed here: this used to grant manager (== admin) status to
    # whoever sent /start FIRST, which lets anyone who messages the bot
    # before the owner does permanently seize it (manager_bot has no
    # separate client menu — non-managers just get "Нет доступа"). When
    # bots.owner_telegram_id is known (webhook/production mode), only that
    # user may claim the empty-managers bootstrap slot. In standalone/env
    # mode (owner_telegram_id unknown) the old first-comer behavior is kept
    # as the only option available.
    is_owner = config.owner_telegram_id is not None and sender_id == config.owner_telegram_id
    if not managers and (is_owner or config.owner_telegram_id is None):
        _save_managers(_managers_file(config), {str(sender_id)})
        managers = {str(sender_id)}
        if config.bot_id is not None:
            try:
                await add_bot_admin(config.bot_id, str(sender_id))
            except Exception as e:
                logger.warning(f"cmd_start: add_bot_admin sync failed for bot {config.bot_id}: {e}")
    if not _is_manager(sender_id, config):
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


@router.callback_query(F.data == "mb_main")
async def cb_main(cb, state: FSMContext, config: ManagerBotConfig):
    await cb.answer()
    if not _is_manager(cb.from_user.id, config):
        return
    await state.clear()
    await cb.message.edit_text(WELCOME_TEXT, parse_mode="HTML", reply_markup=kb_main_menu())


@router.callback_query(F.data == "mb_app")
async def cb_app(cb, config: ManagerBotConfig):
    await cb.answer()
    if not _is_manager(cb.from_user.id, config):
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


MAX_LIST_ROWS = 40


def _kb_task_row(task_id: int, status: str) -> list[InlineKeyboardButton]:
    if status == "done":
        return []
    return [InlineKeyboardButton(text=f"✅ Выполнено #{task_id}", callback_data=f"mb_done:{task_id}")]


@router.callback_query(F.data == "mb_tasks")
async def cb_tasks(cb, config: ManagerBotConfig):
    await cb.answer()
    if not _is_manager(cb.from_user.id, config):
        return
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT * FROM incoming_tasks ORDER BY created_at DESC LIMIT ?", (MAX_LIST_ROWS,)
        )).fetchall()
    if not rows:
        await cb.message.edit_text(
            "📥 <b>Входящие задачи</b>\n\n— пока пусто —",
            parse_mode="HTML", reply_markup=kb_back(),
        )
        return
    lines = ["📥 <b>Входящие задачи</b>\n"]
    buttons = []
    for r in rows:
        mark = "✅" if r["status"] == "done" else "🔸"
        deadline = f" · до {r['deadline']}" if r["deadline"] else ""
        lines.append(f"{mark} #{r['id']} {r['title']}{deadline}")
        row_buttons = _kb_task_row(r["id"], r["status"])
        if row_buttons:
            buttons.append(row_buttons)
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="mb_main")])
    await cb.message.edit_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(F.data.startswith("mb_done:"))
async def cb_done(cb, config: ManagerBotConfig):
    await cb.answer()
    if not _is_manager(cb.from_user.id, config):
        return
    try:
        task_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "UPDATE incoming_tasks SET status='done' WHERE id=? AND status != 'done'", (task_id,)
        )
        await db.commit()
        row = None
        if cur.rowcount:
            row = await (await db.execute("SELECT * FROM incoming_tasks WHERE id=?", (task_id,))).fetchone()
    if row is not None:
        await _publish_completion(config, row)
    await cb_tasks(cb, config)


async def _publish_completion(config: ManagerBotConfig, row) -> None:
    """Back-syncs completion to the ORIGINATING boss_bot via a
    task.completed office event, so features/reminders.py's overdue sweep
    over there stops firing for a task that was actually finished on time.
    Requires a bot_office_links row (manager_bot -> boss_bot, task.completed)
    set up the same way the boss_bot -> manager_bot task.assigned link is —
    see handlers/manage_bots.py's "🏢 Офисы" flow. Silently does nothing if
    that link is missing (row["source_task_id"] is None, or no live
    subscriber) — same fire-and-forget degrade-gracefully contract every
    other office_events publisher in this codebase already accepts (see
    features/office_events.py's publish_event docstring)."""
    if config.bot_id is None or row["source_task_id"] is None:
        return
    from features import office_events
    from features.office_events import TaskCompletedEvent

    try:
        await office_events.publish_event(
            config.bot_id, "task.completed",
            TaskCompletedEvent(task_id=row["source_task_id"], completed_by=config.display_name or config.bot_name),
        )
    except Exception:
        logger.exception(
            f"manager_bot: failed to publish task.completed for source_task_id={row['source_task_id']} "
            f"bot_id={config.bot_id}"
        )


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
