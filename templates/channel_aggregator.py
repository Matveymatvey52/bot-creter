# TEMPLATE: channel_aggregator
# USE FOR: агрегатор Telegram-каналов, мониторинг чужих каналов через личный аккаунт, сбор постов в структурированную сводку, дайджест по расписанию
# CUSTOMIZE: sections marked with # CUSTOMIZE
"""Standalone product wrapper around features/channel_monitor.py — unlike
every other COMPATIBLE_WITH:"*" template that merely CAN enable
channel_monitor as one of several optional features, this template's whole
purpose IS channel monitoring: own /start, no other domain logic.

Per docs/CHANNEL_AGGREGATOR_TEMPLATE_DESIGN.md §5 (owner decisions):
features/channel_monitor.py is reused AS-IS (auth/join FSM, FloodWait/
phone-cycling protection, _is_bot_admin on every handler) — this file only
adds its own /start screen and wires the feature's router + init_db, same
"import feature module directly, include its router" pattern already used by
templates/boss_bot.py for office_events/voice_intake (rather than requiring
the owner to discover and manually toggle "channel_monitor" in a Features
panel, which doesn't exist for a from-scratch/standalone bot).

runtime/userbot_worker.py (the separate process actually running Telethon
clients) discovers which bots to watch via bot_features ("channel_monitor"
enabled) — init_db() below auto-enables that row from db_path's own bot_id,
so a channel_aggregator bot is picked up by the worker without any manual
step, mirroring handlers/create_bot.py's _apply_voice_cashflow_config
auto-enable pattern for voice_intake/cashflow_ledger.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message

from db.database import add_bot_admin
from features import channel_monitor
from features.channel_monitor import kb_start_menu

# ── CUSTOMIZE ────────────────────────────────────────────────────────────────
BOT_DESCRIPTION = (
    "Агрегатор Telegram-каналов: собираю посты из указанных вами каналов через "
    "ваш личный аккаунт, извлекаю нужные поля по схеме (вакансии, новости, цены "
    "и т.д.) и присылаю готовый отчёт — по кнопке или по расписанию."
)
START_TEXT = (
    "📡 <b>Агрегатор каналов</b>\n\n"
    "Я собираю посты из чужих Telegram-каналов, которые вы укажете, извлекаю "
    "из них нужные данные по выбранной вами схеме (вакансии, новости, цены, "
    "недвижимость и другие пресеты — или своя схема) и присылаю готовый отчёт: "
    "по кнопке «📊 Отчёт» в любой момент, или автоматически по расписанию.\n\n"
    "Работает через ваш личный Telegram-аккаунт (не через бота) — нажмите "
    "«➕ Подключить мониторинг», чтобы начать."
)
# ── END CUSTOMIZE ─────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_BOT_ID_FROM_DB_PATH_RE = re.compile(r"bot_(\d+)_data\.db$")


@dataclass
class ChannelAggregatorConfig:
    bot_name: str
    db_path: str
    admins_file: Path
    bot_id: int | None = None
    owner_telegram_id: int | None = None


def _paths_for(name: str, data_dir: Path) -> ChannelAggregatorConfig:
    return ChannelAggregatorConfig(
        bot_name=name,
        db_path=str(data_dir / f"{name}_data.db"),
        admins_file=data_dir / f"admins_{name}.json",
    )


def config_from_env() -> ChannelAggregatorConfig:
    """Standalone/subprocess mode."""
    name = Path(__file__).stem
    data_dir = Path(os.getenv("DATA_DIR", "./data"))
    data_dir.mkdir(exist_ok=True)
    return _paths_for(name, data_dir)


def config_from_bot_row(bot_row: dict, data_dir: Path) -> ChannelAggregatorConfig:
    """Webhook runtime mode — same per-bot db_path convention as every other
    template's config_from_bot_row (isolation by bots.id, not name)."""
    bot_id = bot_row["bot_id"]
    return ChannelAggregatorConfig(
        bot_name=bot_row["name"],
        db_path=str(data_dir / f"bot_{bot_id}_data.db"),
        admins_file=data_dir / f"admins_{bot_id}.json",
        bot_id=bot_id,
        owner_telegram_id=bot_row.get("owner_telegram_id"),
    )


class ConfigMiddleware(BaseMiddleware):
    """Injects this bot's ChannelAggregatorConfig into data["config"] — same
    contract features/channel_monitor.py's ChannelMonitorConfig Protocol
    expects (db_path + admins_file), matching every other template's
    ConfigMiddleware."""

    def __init__(self, config: ChannelAggregatorConfig) -> None:
        self.config = config
        super().__init__()

    async def __call__(self, handler, event, data):
        data["config"] = self.config
        return await handler(event, data)


# ── admin bootstrap ──────────────────────────────────────────────────────────
# Security fix: this template's own cmd_start (below) used to never call
# _save_admins at all — features/channel_monitor.py's _is_bot_admin() (which
# gates every single handler this template's router reuses, see module
# docstring) checks config.admins_file, but nothing here ever populated it.
# The result was the inverse of the usual "first /start becomes admin"
# hijack bug: the admins file stayed permanently empty, so _is_bot_admin
# returned False for EVERYONE including the real owner — the monitoring
# button was visible but functionally dead for the whole bot. _load_admins/
# _save_admins are intentionally duplicated here rather than imported from
# features/channel_monitor.py, matching that module's own "no shared
# coupling" design note on its _load_admins/_is_bot_admin.
def _load_admins(admins_file: Path) -> set:
    try:
        return set(json.loads(admins_file.read_text()).get("ids", []))
    except Exception:
        return set()

def _save_admins(admins_file: Path, ids: set) -> None:
    admins_file.write_text(json.dumps({"ids": list(ids)}, ensure_ascii=False))


async def init_db(db_path: str) -> None:
    """Creates channel_monitor's tables (userbot_sessions/monitored_channels/
    channel_posts/report_schedules) in THIS bot's own db_path, then
    auto-enables the 'channel_monitor' bot_features row so
    runtime/userbot_worker.py's get_channel_monitor_bot_ids() picks this bot
    up without a manual toggle — see module docstring. bot_id is recovered
    from db_path's own filename (bot_<id>_data.db, the same convention
    runtime/userbot_worker.py's _db_path_for_bot uses) since init_db's generic
    signature (called by runtime/registry.py) only ever receives db_path, not
    bot_id. Standalone/subprocess mode (config_from_env, db_path=
    channel_aggregator_data.db) doesn't match this pattern — enable_bot_feature
    is skipped there, which is fine: the worker discovery loop is irrelevant
    outside the multi-tenant webhook runtime anyway."""
    await channel_monitor.init_db(db_path)
    match = _BOT_ID_FROM_DB_PATH_RE.search(db_path)
    if match:
        from db.database import enable_bot_feature

        bot_id = int(match.group(1))
        try:
            await enable_bot_feature(bot_id, "channel_monitor")
        except Exception as e:
            logger.warning(f"init_db: could not auto-enable channel_monitor feature for bot_id={bot_id}: {e}")


# ── mini-app config (see docs/MINIAPP_DESIGN.md, templates/car_rental.py's
# own miniapp_config for the authoring contract) ────────────────────────────
# Tables come from features/channel_monitor.py's init_db() (db.database's
# init_channel_monitor_tables), which THIS template's own init_db() calls
# above — not created directly in this file, but this bot's own db_path ends
# up with exactly these tables/columns, verified against real schema by
# CHANNEL AGGREGATOR MINIAPP CONFIG TESTS. userbot_sessions/report_schedules
# aren't exposed as resources: both are single-row-per-bot operational state
# (auth session, one active schedule) driven entirely by dedicated FSMs
# (UserbotAuthStates, _ScheduleState) with side effects (Telethon auth,
# scheduler wiring) a generic mini-app CRUD form can't replicate safely.
# channels isn't creatable either — cm_add_channel's FSM does a live Telethon
# username resolution + schema-preset pick that a plain form field can't do.
miniapp_config = {
    "resources": [
        {
            "name": "channels",
            "table": "monitored_channels",
            "order_by": "added_at DESC",
            "creatable": False,
            "scope": {"type": "global"},
            "title": "Каналы",
            "titleField": "channel_title",
            "fields": [
                {"name": "channel_username", "label": "Username", "kind": "text", "list": True, "detail": True, "create": False},
                {"name": "channel_title", "label": "Название", "kind": "text", "list": True, "detail": True, "create": False},
                {"name": "active", "label": "Активен", "kind": "status", "list": True, "detail": True, "create": False},
                {"name": "extract_schema", "label": "Схема извлечения", "kind": "text", "list": False, "detail": True, "create": False},
                {"name": "added_at", "label": "Добавлен", "kind": "date", "list": False, "detail": True, "create": False},
            ],
        },
        {
            "name": "posts",
            "table": "channel_posts",
            "order_by": "posted_at DESC",
            "creatable": False,
            "scope": {"type": "scoped", "parent": "channels", "via": "monitored_channel_id"},
            "title": "Посты",
            "titleField": "text",
            "fields": [
                {"name": "monitored_channel_id", "label": "ID канала", "kind": "number", "list": False, "detail": True, "create": False},
                {"name": "text", "label": "Текст", "kind": "text", "list": True, "detail": True, "create": False},
                {"name": "summary", "label": "Summary", "kind": "text", "list": True, "detail": True, "create": False},
                {"name": "views", "label": "Просмотры", "kind": "number", "list": True, "detail": True, "create": False},
                {"name": "forwards", "label": "Репосты", "kind": "number", "list": False, "detail": True, "create": False},
                {"name": "posted_at", "label": "Дата", "kind": "date", "list": True, "detail": True, "create": False},
            ],
        },
    ],
}


router = channel_monitor.router


@router.message(Command("start"), F.chat.type == "private")
async def cmd_start(message: Message, config: ChannelAggregatorConfig):
    admins = _load_admins(config.admins_file)
    sender_id = message.from_user.id
    # Bootstrap: only bots.owner_telegram_id (when known) may claim the
    # empty-admins slot — same first-comer protection as every other
    # template's cmd_start (see e.g. templates/shop_catalog.py). In
    # standalone/env mode (owner_telegram_id unknown) the first /start
    # sender is the only option available.
    is_owner = config.owner_telegram_id is not None and sender_id == config.owner_telegram_id
    if not admins and (is_owner or config.owner_telegram_id is None):
        _save_admins(config.admins_file, {str(sender_id)})
        if config.bot_id is not None:
            try:
                await add_bot_admin(config.bot_id, str(sender_id))
            except Exception as e:
                logger.warning(f"cmd_start: add_bot_admin sync failed for bot {config.bot_id}: {e}")
    await message.answer(START_TEXT, parse_mode="HTML", reply_markup=kb_start_menu())


async def main() -> None:
    config = config_from_env()
    bot = Bot(token=os.getenv("BOT_TOKEN"))
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(ConfigMiddleware(config))
    dp.include_router(router)
    await bot.set_my_description(BOT_DESCRIPTION)
    await init_db(config.db_path)
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    asyncio.run(main())
