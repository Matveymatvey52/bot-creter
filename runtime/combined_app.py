"""Stage 2 — "фабрика как житель реестра" combined entry point.

Runs the exact same webhook aiohttp server as runtime/webhook_app.py, but ALSO
registers the factory bot (handlers/create_bot.py's router + the rest of
main.py's dispatcher setup — previously only reachable via main.py's separate
long-polling process) as one more entry in the SAME live Registry, under the
reserved FACTORY_BOT_ID sentinel (see runtime/registry.py). The factory bot
now answers through the exact same /webhook/{bot_id} path and WEBHOOK_SECRET
check as any tenant bot — no separate route, no separate security logic.

No separate concurrent loop for the factory either: once it's a webhook
citizen (not polling), its processing happens per-request through the SAME
aiohttp request-handling coroutine as any tenant bot — there's nothing
"perpetual" left that could fail independently and need its own supervisor.
See docs/STAGE2_DESIGN.md "Фабрика как житель реестра" for the inventory that
established this (initially expected to need a two-task supervisor; turned out
not to).

NOT wired into any live deployment. main.py (long-polling) is still the actual
start command — see start.sh/railway.toml. Switching to this file is a
separate, later, explicitly-confirmed decision (this phase's own Task 6).
"""

from __future__ import annotations

import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import web

from config import BOT_TOKEN
from db.database import init_db as init_bots_db
from handlers.admin_manager import OWNER_ID, router as admin_router
from handlers.create_bot import router as create_router, set_bot_id, set_manager_username, set_registry
from handlers.general import router as general_router
from handlers.manage_bots import router as manage_router
from handlers.start import router as start_router
from main import ManagedBotMiddleware, build_group_router, restore_bots
from runtime.registry import FACTORY_BOT_ID, build_factory_entry, build_registry
from runtime.webhook_app import create_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


async def _build_factory_dispatcher() -> Dispatcher:
    """Same router set + middleware as main.py's own main(), so the factory
    bot keeps its full command surface when answering via webhook instead of
    polling. build_group_router() is shared with main.py (see there) rather
    than duplicated."""
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)
    dp.update.outer_middleware(ManagedBotMiddleware(storage))
    dp.include_router(admin_router)
    dp.include_router(start_router)
    dp.include_router(create_router)
    dp.include_router(manage_router)
    dp.include_router(general_router)  # must be last — catch-all
    dp.include_router(build_group_router())
    return dp


async def _bootstrap_app() -> web.Application:
    if not OWNER_ID:
        logger.warning(
            "OWNER_ID is not set (or 0) — bot management commands will be unavailable to everyone. "
            "Set OWNER_ID in .env to your Telegram user ID."
        )

    await init_bots_db()

    factory_bot = Bot(token=BOT_TOKEN)
    try:
        me = await factory_bot.get_me()
        set_manager_username(me.username)
        set_bot_id(me.id)
        logger.info(f"Factory bot: @{me.username} (id={me.id})")
    except Exception as e:
        logger.warning(f"Could not fetch factory bot info: {e}")

    try:
        await factory_bot.set_my_description(
            "Создаю Telegram-ботов по твоему описанию 🤖\n\n"
            "⚡ Быстро — бот готов за несколько минут\n"
            "🧩 Умно — понимаю задачи и делаю лучшее решение\n"
            "🛡 Надёжно — стабильные боты на долгую перспективу\n\n"
            "Нажми «Начать» и опиши своего бота!"
        )
        await factory_bot.set_my_short_description("Создаю Telegram-ботов по описанию за несколько минут")
    except Exception as e:
        logger.warning(f"Could not set factory bot description: {e}")

    # Tenant bots still run as their own OS subprocesses (services/bot_runner) —
    # unchanged by this phase. Restoring them on startup is unrelated to the
    # registry/webhook path below, same as it is in main.py today.
    await restore_bots()

    registry = await build_registry()

    factory_dispatcher = await _build_factory_dispatcher()
    factory_entry = build_factory_entry(factory_bot, factory_dispatcher)
    # Direct insertion, bypassing add_or_replace()/build_entry() — the owner's
    # explicit choice (see docs/STAGE2_DESIGN.md): the factory isn't a
    # bots-table row, so add_or_replace()'s bot_row-shaped contract doesn't
    # apply to it. reload_all() preserves the factory entry (guarded in
    # Registry.reload_all), and reload_one(FACTORY_BOT_ID) is a guarded no-op
    # (see Registry.reload_one) — a live /admin/reload* call can no longer
    # evict the factory bot from the registry.
    registry._entries[FACTORY_BOT_ID] = factory_entry

    # So bot creation (handlers/create_bot.py) can register newly-created
    # tenant bots into this SAME live registry directly — same process now,
    # no HTTP self-call needed. See handlers/create_bot.py's set_registry().
    set_registry(registry)

    logger.info(f"Combined registry built: {len(registry)} bot(s), including the factory bot")
    return create_app(registry)


def main() -> None:
    port = int(os.getenv("PORT", "8080"))
    web.run_app(_bootstrap_app(), port=port)


if __name__ == "__main__":
    main()
