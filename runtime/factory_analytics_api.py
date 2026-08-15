"""Factory-wide analytics dashboard — owner-only REST layer.

Distinct from runtime/miniapp_api.py's generic per-bot mini-app: that module
serves ONE tenant bot's own data (rows in ITS OWN db/config.get("db_path")),
keyed by a Registry entry that must have a template_id + miniapp_config. The
factory pseudo-entry (FACTORY_BOT_ID=0, template_id="__factory__" — see
runtime/registry.py) has neither, so it can't be served through that path.
This dashboard instead reads the CENTRAL factory DB directly (db/database.py:
bots, bot_features, bot_custom_features, bot_feedback) — one row per bot the
factory has ever created, not one row per record inside a single bot.

Auth is owner-only (not per-bot-tenant-user like miniapp_api.py): only
OWNER_ID (handlers/admin_manager.py) may see this data. Reuses the same two
auth paths and HMAC scheme as miniapp_api.py (Telegram initData in the
WebView, a signed magic-link ?token= in a plain browser), scoped to bot_id=0
(the factory bot) and requiring the verified telegram_user_id to equal
OWNER_ID — see _authenticate_owner().
"""

from __future__ import annotations

import logging

from aiohttp import web

from db.database import add_bot_feedback, list_bots_with_stats
from handlers.admin_manager import OWNER_ID
from runtime.miniapp_api import _authenticate
from runtime.registry import FACTORY_BOT_ID, Registry, infer_template_id
from runtime.webhook_app import REGISTRY_KEY

logger = logging.getLogger(__name__)


async def _authenticate_owner(request: web.Request) -> bool:
    """True iff the request is authenticated (Telegram initData or magic-link
    token, both scoped to bot_id=FACTORY_BOT_ID) AND the resulting
    telegram_user_id is OWNER_ID. Fails closed: OWNER_ID unset (0) always
    returns False, same posture as admin_manager.py's _is_owner()."""
    if OWNER_ID == 0:
        return False
    registry: Registry = request.app[REGISTRY_KEY]
    factory_entry = registry.get(FACTORY_BOT_ID)
    if factory_entry is None:
        return False
    telegram_user_id = await _authenticate(request, FACTORY_BOT_ID, factory_entry.bot.token)
    return telegram_user_id == OWNER_ID


async def list_bots_handler(request: web.Request) -> web.Response:
    if not await _authenticate_owner(request):
        return web.json_response({"error": "forbidden"}, status=403)

    rows = await list_bots_with_stats()
    items = []
    for row in rows:
        features = [f for f in row["features"].split(",") if f]
        items.append(
            {
                "id": row["id"],
                "name": row["name"],
                "username": row["username"],
                "display_name": row["display_name"],
                "status": row["status"],
                "created_at": row["created_at"],
                "archived_at": row["archived_at"],
                "template": infer_template_id(row["file_path"]),
                "features": features,
                "edits_count": row["edits_count"],
                "avg_rating": row["avg_rating"],
                "feedback_count": row["feedback_count"],
            }
        )
    return web.json_response({"items": items})


async def add_feedback_handler(request: web.Request) -> web.Response:
    if not await _authenticate_owner(request):
        return web.json_response({"error": "forbidden"}, status=403)

    bot_id_raw = request.match_info.get("bot_id", "")
    if not bot_id_raw.isdigit():
        return web.json_response({"error": "bad bot_id"}, status=404)

    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    if not isinstance(payload, dict):
        return web.json_response({"error": "invalid JSON body"}, status=400)

    rating = payload.get("rating")
    if not isinstance(rating, int) or not (1 <= rating <= 5):
        return web.json_response({"error": "rating must be an integer 1-5"}, status=400)
    comment = payload.get("comment")
    if comment is not None and not isinstance(comment, str):
        return web.json_response({"error": "comment must be a string"}, status=400)

    await add_bot_feedback(int(bot_id_raw), rating, comment)
    return web.json_response({"ok": True}, status=201)


def register_routes(app: web.Application) -> None:
    """Adds owner-only analytics routes to the same Application miniapp_api's
    register_routes() already extends (see combined_app.py's _bootstrap_app,
    which calls both). Namespaced under /api/factory/ to stay clearly
    distinct from /api/{bot_id}/... 's per-tenant-bot paths — a real bot_id
    is never 'factory'."""
    app.router.add_get("/api/factory/bots", list_bots_handler)
    app.router.add_post("/api/factory/bots/{bot_id}/feedback", add_feedback_handler)
