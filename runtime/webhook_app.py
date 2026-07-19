"""Stage 2 Phase 1 — multi-tenant webhook server skeleton.

Separate entry point from main.py (which stays the working polling bot).
Run locally with: python -m runtime.webhook_app

Not wired into any live deployment — see docs/STAGE2_DESIGN.md / STAGE2_REPORT.md.
"""

from __future__ import annotations

import hmac
import logging
import os

from aiohttp import web

from runtime.registry import BotEntry, Registry, build_registry

logger = logging.getLogger(__name__)

REGISTRY_KEY = web.AppKey("registry", Registry)


async def health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def webhook_handler(request: web.Request) -> web.Response:
    bot_id_raw = request.match_info.get("bot_id", "")
    if not bot_id_raw.isdigit():
        return web.json_response({"error": "bad bot_id"}, status=404)
    bot_id = int(bot_id_raw)

    # PHASE 1 TRADE-OFF: if WEBHOOK_SECRET isn't set, the check is skipped (fail-open).
    # Fine for local smoke-testing with no real webhook registered; before any real
    # deploy this must become fail-closed (refuse to start, or reject all requests)
    # when the secret is missing — see STAGE2_REPORT.md.
    secret = os.getenv("WEBHOOK_SECRET", "")
    if secret:
        header = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(header, secret):
            return web.json_response({"error": "forbidden"}, status=403)

    registry: Registry = request.app[REGISTRY_KEY]
    entry = registry.get(bot_id)
    if entry is None:
        return web.json_response({"error": "unknown bot"}, status=404)

    update_data = await request.json()
    try:
        await entry.dispatcher.feed_webhook_update(entry.bot, update_data)
    except Exception:
        logger.exception(f"Failed to process webhook update for bot_id={bot_id}")
    return web.json_response({"ok": True})


def _admin_secret_ok(request: web.Request) -> bool:
    """Fail-CLOSED — deliberately the opposite trade-off from webhook_handler's
    public endpoint. This is an internal control surface (force-rebuild any
    bot's Dispatcher from the DB); an unset WEBHOOK_SECRET must deny access
    here, not silently skip the check."""
    secret = os.getenv("WEBHOOK_SECRET", "")
    if not secret:
        return False
    header = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    return hmac.compare_digest(header, secret)


async def admin_reload_one(request: web.Request) -> web.Response:
    if not _admin_secret_ok(request):
        return web.json_response({"error": "forbidden"}, status=403)
    bot_id_raw = request.match_info.get("bot_id", "")
    if not bot_id_raw.isdigit():
        return web.json_response({"error": "bad bot_id"}, status=404)
    bot_id = int(bot_id_raw)

    registry: Registry = request.app[REGISTRY_KEY]
    entry = await registry.reload_one(bot_id)
    logger.info(f"admin_reload_one: bot_id={bot_id} found={entry is not None}")
    return web.json_response({"bot_id": bot_id, "found": entry is not None})


async def admin_reload_all(request: web.Request) -> web.Response:
    if not _admin_secret_ok(request):
        return web.json_response({"error": "forbidden"}, status=403)

    registry: Registry = request.app[REGISTRY_KEY]
    await registry.reload_all()
    logger.info(f"admin_reload_all: {len(registry)} bot(s) in registry")
    return web.json_response({"count": len(registry)})


def create_app(registry: Registry | None = None) -> web.Application:
    """Build the aiohttp Application. `registry` can be pre-populated (used by tests);
    otherwise it's built from the DB when the app starts (see _bootstrap_app)."""
    app = web.Application()
    app[REGISTRY_KEY] = registry if registry is not None else Registry()
    app.router.add_post("/webhook/{bot_id}", webhook_handler)
    app.router.add_get("/health", health)
    app.router.add_post("/admin/reload/{bot_id}", admin_reload_one)
    app.router.add_post("/admin/reload-all", admin_reload_all)
    return app


async def _bootstrap_app() -> web.Application:
    registry = await build_registry()
    logger.info(f"Webhook registry built: {len(registry)} bot(s)")
    return create_app(registry)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    port = int(os.getenv("PORT", "8080"))
    web.run_app(_bootstrap_app(), port=port)


if __name__ == "__main__":
    main()
