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

This is the actual live prod entry point: Railway's BOT_SCRIPT is set to
runtime/combined_app.py, webhook mode is enabled, and this has been confirmed
in production (see docs/WEBHOOK_ACTIVATION.md). main.py (long-polling) remains
for local development/debugging only — it is not what runs in prod.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import web

# Позволяет запускать этот файл напрямую (python runtime/combined_app.py, как
# задаёт BOT_SCRIPT в Railway), а не только через python -m runtime.combined_app.
# main.py этого не требует (лежит в корне), webhook_app.py не требует по той же
# причине, что и раньше — он никогда не запускался напрямую, только через -m
# (см. его докстринг).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from config import BOT_TOKEN
from db.database import init_db as init_bots_db
from handlers.admin_manager import OWNER_ID, router as admin_router
from handlers.create_bot import router as create_router, set_bot_id, set_manager_username, set_registry
from handlers.custom_features import router as custom_features_router, set_registry as set_custom_features_registry
from handlers.feature_connect import router as feature_connect_router
from handlers.general import router as general_router
from handlers.manage_bots import router as manage_router, set_registry as set_manage_bots_registry
from handlers.start import router as start_router
from main import ManagedBotMiddleware, build_group_router, restore_bots
from db.database import get_bot_features
from features import reminders
from features.office_events import set_registry as set_office_events_registry
from runtime.registry import (
    FACTORY_BOT_ID,
    build_factory_entry,
    build_registry,
    get_template_reminders_config,
    register_critical_error_handler,
)
from runtime.factory_analytics_api import register_routes as register_factory_analytics_routes
from runtime.miniapp_api import register_routes as register_miniapp_routes
from runtime.template_candidate_clustering import template_candidate_clustering_loop
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
    register_critical_error_handler(dp, FACTORY_BOT_ID)
    dp.update.outer_middleware(ManagedBotMiddleware(storage))
    dp.include_router(admin_router)
    dp.include_router(start_router)
    dp.include_router(create_router)
    dp.include_router(manage_router)
    dp.include_router(feature_connect_router)  # fconn_* callbacks only — order-independent
    dp.include_router(custom_features_router)
    dp.include_router(general_router)  # must be last — catch-all
    dp.include_router(build_group_router())
    return dp


# docs/REMINDERS_DESIGN.md Part 3: fixed-interval sweep, same shape as
# runtime/userbot_worker.py's own _summarize_loop() (plain while True +
# sleep — no APScheduler dependency, matches this codebase's only existing
# background-loop precedent). Module-level so tests can import/tune it
# without needing a real event loop running _bootstrap_app().
REMINDERS_SWEEP_INTERVAL_SECONDS = 300


async def _run_reminders_sweep(registry) -> None:
    """One pass over every registered tenant bot with "reminders" enabled
    (db/database.py's bot_features — same enablement table payments.py
    uses) and a reminders_config on its template module. The factory bot
    (FACTORY_BOT_ID) has template_id="__factory__", which never resolves to
    a templates/*.py module, so get_template_reminders_config() returns
    None for it and it is skipped without any special-casing here."""
    for bot_id in registry.bot_ids():
        entry = registry.get(bot_id)
        if entry is None or not entry.template_id:
            continue
        try:
            enabled_features = await get_bot_features(bot_id)
        except Exception:
            logger.exception(f"_run_reminders_sweep: bot_id={bot_id} — failed to read bot_features, skipped")
            continue
        if "reminders" not in enabled_features:
            continue
        reminders_config = get_template_reminders_config(entry.template_id)
        if not reminders_config:
            continue
        db_path = entry.config.get("db_path")
        if not db_path:
            logger.warning(f"_run_reminders_sweep: bot_id={bot_id} has reminders enabled but no db_path — skipped")
            continue
        try:
            sent = await reminders.run_reminders_sweep_for_bot(entry.bot, db_path, reminders_config)
            if sent:
                logger.info(f"_run_reminders_sweep: bot_id={bot_id} sent {sent} reminder(s)")
        except Exception:
            # One bot's broken reminders_config/db must never abort the
            # sweep for every other bot — same isolation principle as
            # features/payments.py's office_events publish try/except.
            logger.exception(f"_run_reminders_sweep: bot_id={bot_id} — sweep failed")


async def _reminders_sweep_loop(registry) -> None:
    while True:
        try:
            await _run_reminders_sweep(registry)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("_reminders_sweep_loop: unexpected error escaped _run_reminders_sweep")
        await asyncio.sleep(REMINDERS_SWEEP_INTERVAL_SECONDS)


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
    set_manage_bots_registry(registry)
    set_custom_features_registry(registry)
    set_office_events_registry(registry)

    logger.info(f"Combined registry built: {len(registry)} bot(s), including the factory bot")
    app = create_app(registry)
    # Same Application/process/port as the webhook routes above — a template's
    # own web.TCPSite (e.g. tour_operator.py's build_web_app()) is explicitly
    # NOT started here for exactly this reason (see that template's "Стоп:
    # веб-часть не ложится на паттерн" docstring); the mini-app REST layer
    # avoids that trap by registering onto the existing app instead of binding
    # its own port. See docs/MINIAPP_DESIGN.md §2.2.
    register_miniapp_routes(app)
    register_factory_analytics_routes(app)

    # aiohttp's own on_startup/on_cleanup hooks, not a bare asyncio.create_task
    # here in _bootstrap_app() — _bootstrap_app() runs to completion BEFORE
    # web.run_app()'s event loop takes over serving requests (it's the
    # coroutine passed to web.run_app(), not a request handler), so a task
    # created here would start running detached from the app's own lifecycle
    # (nothing would ever cancel it on shutdown, and tests that build an app
    # via this function without serving it would leak the task). on_cleanup
    # guarantees cancellation happens even if the process is stopped via
    # web.run_app()'s normal signal handling.
    async def _start_reminders_sweep(app: web.Application) -> None:
        app["reminders_sweep_task"] = asyncio.create_task(_reminders_sweep_loop(registry))

    async def _stop_reminders_sweep(app: web.Application) -> None:
        task = app.get("reminders_sweep_task")
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    app.on_startup.append(_start_reminders_sweep)
    app.on_cleanup.append(_stop_reminders_sweep)

    # docs/TEMPLATE_CANDIDATE_CLUSTERING_DESIGN.md §3 — same on_startup/
    # on_cleanup shape as the reminders sweep above, and for the same
    # reason (task must be tied to the app's own lifecycle, not detached).
    async def _start_template_candidate_clustering(app: web.Application) -> None:
        app["template_candidate_clustering_task"] = asyncio.create_task(template_candidate_clustering_loop())

    async def _stop_template_candidate_clustering(app: web.Application) -> None:
        task = app.get("template_candidate_clustering_task")
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    app.on_startup.append(_start_template_candidate_clustering)
    app.on_cleanup.append(_stop_template_candidate_clustering)

    return app


def main() -> None:
    port = int(os.getenv("PORT", "8080"))
    web.run_app(_bootstrap_app(), port=port)


if __name__ == "__main__":
    # Used by tests/test_combined_app_entrypoint.py to verify the sys.path fix
    # above without actually starting the bootstrap (real Telegram API calls,
    # DB writes, binding a port) — by the time argv is checked, every
    # module-level import (including the ones the sys.path fix exists for) has
    # already either succeeded or raised, so this only needs to short-circuit
    # main() itself.
    if "--check-imports" in sys.argv:
        print("imports OK")
        sys.exit(0)
    main()
