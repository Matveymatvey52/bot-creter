"""Factory-wide analytics dashboard — owner-only REST layer.

Distinct from runtime/miniapp_api.py's generic per-bot mini-app: that module
serves ONE tenant bot's own data (rows in ITS OWN db/config.get("db_path")),
keyed by a Registry entry that must have a template_id + miniapp_config. The
factory pseudo-entry (FACTORY_BOT_ID=0, template_id="__factory__" — see
runtime/registry.py) has neither, so it can't be served through that path.
This dashboard instead reads the CENTRAL factory DB directly (db/database.py:
bots, bot_features, bot_custom_features, bot_feedback, template_candidates) —
one row per bot the factory has ever created, not one row per record inside
a single bot.

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

from db.database import (
    add_bot_admin,
    add_bot_feedback,
    add_office_link,
    clear_bot_feature_config,
    delete_bot as db_delete_bot,
    get_bot,
    get_bot_admins,
    get_bot_feature_config,
    get_bot_features,
    get_bot_office_hook_config,
    get_bot_sheets_config,
    get_bot_yookassa_credentials,
    get_office_digest_group,
    get_office_links_for_bot,
    list_bots_with_stats,
    list_template_candidate_clusters_with_stats,
    list_template_candidates,
    remove_bot_admin,
    remove_office_link,
    set_bot_feature_description,
    set_bot_feature_thread,
    update_bot_status,
)
from features.office_events import EVENT_TYPE_LABELS, available_event_types_for_template
from features.sales_analytics import weekly_record_count
from handlers.admin_manager import OWNER_ID
from handlers.manage_bots import (
    _busy_bots,
    _BUSY_TEXT,
    _OFFICE_EVENT_TYPE,
    apply_fix_core,
    autofix_bot_core,
    disable_feature_and_reload,
    enable_feature_and_reload,
    recreate_bot_core,
)
from runtime.miniapp_api import _authenticate, mint_magic_link_token
from runtime.registry import FACTORY_BOT_ID, Registry, discover_features, infer_template_id
from runtime.webhook_app import REGISTRY_KEY
from services.bot_runner import _make_extra_env, get_bot_logs, is_running, start_bot, stop_bot
from services.claude_service import assess_feature_description

logger = logging.getLogger(__name__)

# Features that skip the Variant D free-text step entirely (approved design
# — see the "Дополнение к дизайну вкладки «Фичи»" conversation): payments
# goes straight to the existing multi-step ЮKassa wizard, which is a
# Telegram-only flow not reproduced over REST here (the SPA tells the owner
# to finish payments setup in Telegram); office_events has its own bot-picker
# UI with no free text at all. Every other feature in discover_features()
# goes through /configure.
_NO_FREE_TEXT_FEATURES = {"payments", "office_events"}


async def _authenticate_owner(request: web.Request) -> bool:
    """True iff the request is authenticated (Telegram initData or magic-link
    token, both scoped to bot_id=FACTORY_BOT_ID) AND the resulting
    telegram_user_id is OWNER_ID. Fails closed: OWNER_ID unset (0) always
    returns False, same posture as admin_manager.py's _is_owner()."""
    if OWNER_ID == 0:
        return False
    telegram_user_id = await _authenticate_factory_user(request)
    return telegram_user_id == OWNER_ID


async def _authenticate_factory_user(request: web.Request) -> int | None:
    """Same credential check as _authenticate_owner (Telegram initData or
    magic-link token scoped to FACTORY_BOT_ID), but without the OWNER_ID
    restriction — any authenticated Telegram user is accepted. Used by
    routes the customer-facing view of the shared dashboard also hits (see
    docs decision: one codebase, role-based rendering, not owner-gated
    routes for everything)."""
    registry: Registry = request.app[REGISTRY_KEY]
    factory_entry = registry.get(FACTORY_BOT_ID)
    if factory_entry is None:
        return None
    return await _authenticate(request, FACTORY_BOT_ID, factory_entry.bot.token)


async def _authenticate_bot_access(request: web.Request, bot_id: int) -> bool:
    """True iff the request is an authenticated OWNER_ID, or an authenticated
    customer who owns this specific bot_id (bots.owner_telegram_id). Used by
    routes the customer view of the shared dashboard needs for their OWN
    bots only (bot detail, feedback) — everything else stays owner-only."""
    telegram_user_id = await _authenticate_factory_user(request)
    if telegram_user_id is None:
        return False
    if telegram_user_id == OWNER_ID:
        return True
    bot_row = await get_bot(bot_id)
    return bot_row is not None and bot_row.get("owner_telegram_id") == telegram_user_id


async def refresh_session_handler(request: web.Request) -> web.Response:
    """GET /api/factory/session — mints a fresh, full-TTL magic-link token for
    the owner, scoped to FACTORY_BOT_ID. mint_magic_link_token()'s docstring
    on MAGIC_LINK_TTL_SECONDS (15 min) always meant a dashboard session was
    expected to renew itself rather than keep replaying the one token from
    the original /start link — that renewal was never wired up, so an owner
    who kept the dashboard open past 15 minutes (or came back to it later
    without re-opening from Telegram) got "forbidden" on every subsequent
    click even though the very first load had worked. The SPA (factoryApi.ts)
    now calls this on a timer well inside the TTL and swaps in the returned
    token, so a still-valid session keeps itself alive instead of expiring
    mid-use. Requires an already-valid credential (current token or Telegram
    initData) — this refreshes a live session, it doesn't mint one from
    nothing."""
    telegram_user_id = await _authenticate_factory_user(request)
    if telegram_user_id is None:
        return web.json_response({"error": "forbidden"}, status=403)
    token = mint_magic_link_token(FACTORY_BOT_ID, telegram_user_id)
    return web.json_response({"token": token, "is_owner": telegram_user_id == OWNER_ID})


async def list_bots_handler(request: web.Request) -> web.Response:
    telegram_user_id = await _authenticate_factory_user(request)
    if telegram_user_id is None:
        return web.json_response({"error": "forbidden"}, status=403)
    is_owner = telegram_user_id == OWNER_ID

    registry: Registry = request.app[REGISTRY_KEY]
    rows = await list_bots_with_stats(owner_telegram_id=None if is_owner else telegram_user_id)
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
                "weekly_count": await _weekly_count_for_bot(registry, row["id"]),
            }
        )
    return web.json_response({"items": items, "is_owner": is_owner})


async def _weekly_count_for_bot(registry: Registry, bot_id: int) -> int | None:
    """Records this bot's own data got in the last 7 days, for the dashboard
    card's headline metric — see features/sales_analytics.weekly_record_count.
    None (not 0) if unavailable so the SPA can distinguish "genuinely zero
    new records this week" from "no data source to count at all" (e.g. bot
    not currently running, so no live db_path — or a template like
    moderator whose office_hook_config is intentionally absent, same
    posture as compute_metrics()'s own None return)."""
    entry = registry.get(bot_id)
    if entry is None:
        return None
    db_path = entry.config.get("db_path") if isinstance(entry.config, dict) else None
    if not db_path:
        return None
    hook_config = await get_bot_office_hook_config(bot_id)
    return await weekly_record_count(db_path, hook_config)


async def add_feedback_handler(request: web.Request) -> web.Response:
    bot_id_raw = request.match_info.get("bot_id", "")
    if not bot_id_raw.isdigit():
        return web.json_response({"error": "bad bot_id"}, status=404)
    if not await _authenticate_bot_access(request, int(bot_id_raw)):
        return web.json_response({"error": "forbidden"}, status=403)

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


async def list_template_candidates_handler(request: web.Request) -> web.Response:
    """"Кандидаты на новый шаблон" section (docs/TEMPLATE_CANDIDATE_LOGGING_DESIGN.md)
    — raw rows, most recent first, no server-side clustering (MVP decision:
    let the owner read the actual requirement text). creator_user_id is
    deliberately excluded from the response, same posture as list_bots_handler
    excluding `token` — an internal Telegram user id the dashboard UI has no
    use for."""
    if not await _authenticate_owner(request):
        return web.json_response({"error": "forbidden"}, status=403)

    rows = await list_template_candidates()
    items = [
        {
            "id": row["id"],
            "bot_id": row["bot_id"],
            "bot_name": row["bot_name"],
            "summary": row["summary"],
            "fallback_reason": row["fallback_reason"],
            "selected_templates": row["selected_templates"],
            "bot_type": row["bot_type"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]
    return web.json_response({"items": items})


async def list_template_candidate_clusters_handler(request: web.Request) -> web.Response:
    """"Топ незакрытых паттернов" section
    (docs/TEMPLATE_CANDIDATE_CLUSTERING_DESIGN.md §4) — clusters an
    incremental background pass (runtime/template_candidate_clustering.py)
    has already assigned candidates to, largest first. Candidates not yet
    picked up by a pass (cluster_id IS NULL) are excluded here on purpose —
    they still show up in list_template_candidates_handler's raw feed above."""
    if not await _authenticate_owner(request):
        return web.json_response({"error": "forbidden"}, status=403)

    rows = await list_template_candidate_clusters_with_stats()
    items = [
        {
            "id": row["id"],
            "label": row["label"],
            "description": row["description"],
            "count": row["count"],
            "first_seen": row["first_seen"],
            "last_seen": row["last_seen"],
            "examples": row["examples"],
        }
        for row in rows
    ]
    return web.json_response({"items": items})


def _bot_id_from_match_info(request: web.Request) -> int | None:
    raw = request.match_info.get("bot_id", "")
    return int(raw) if raw.isdigit() else None


async def bot_detail_handler(request: web.Request) -> web.Response:
    """GET /api/factory/bots/{id} — everything the detail panel's "Обзор" tab
    needs in one call: status, template, admins, office links, and per-feature
    enabled/pending/off state (see feature_status_items below)."""
    bot_id = _bot_id_from_match_info(request)
    if bot_id is None:
        return web.json_response({"error": "bad bot_id"}, status=404)
    if not await _authenticate_bot_access(request, bot_id):
        return web.json_response({"error": "forbidden"}, status=403)
    b = await get_bot(bot_id)
    if not b:
        return web.json_response({"error": "not found"}, status=404)

    template_id = infer_template_id(b.get("file_path"))
    admins = await get_bot_admins(bot_id)
    offices = await get_office_links_for_bot(bot_id)
    features = await _feature_status_items(bot_id, template_id)
    return web.json_response(
        {
            "id": b["id"],
            "name": b["name"],
            "username": b.get("username"),
            "display_name": b.get("display_name"),
            "status": b.get("status"),
            "running": is_running(bot_id),
            "template": template_id,
            "created_at": b.get("created_at"),
            "admins": admins,
            "offices": offices,
            "features": features,
        }
    )


async def _feature_status_items(bot_id: int, template_id: str | None) -> list[dict]:
    """One entry per feature compatible with this bot's template — see
    handlers/manage_bots.py's _compatible_features for the exact
    compatibility rule this mirrors. state is "on" (enabled + has a config
    row, or is payments/office_events which don't need one), "pending" (a
    bot_feature_config row exists with no accepted description yet — the
    owner started the configure dialog but Claude hasn't accepted an
    answer), or "off"."""
    compatible = [f for f in discover_features() if template_id in f["compatible_with"] or "*" in f["compatible_with"]]
    enabled = set(await get_bot_features(bot_id))
    items = []
    for f in compatible:
        name = f["name"]
        cfg = await get_bot_feature_config(bot_id, name)
        if name in enabled:
            state = "on"
        elif cfg and cfg.get("thread"):
            state = "pending"
        else:
            state = "off"
        items.append(
            {
                "name": name,
                "state": state,
                "description": cfg.get("description") if cfg else None,
                "thread": cfg.get("thread") if cfg else [],
                "no_free_text": name in _NO_FREE_TEXT_FEATURES,
            }
        )
    return items


async def _guard_action(bot_id: int, action) -> web.Response:
    """Shared _busy_bots guard for the mutating single-bot endpoints below —
    same contract as handlers/manage_bots.py's own _busy_bots checks (one
    mutating operation per bot at a time)."""
    if bot_id in _busy_bots:
        return web.json_response({"error": _BUSY_TEXT}, status=409)
    _busy_bots.add(bot_id)
    try:
        return await action()
    finally:
        _busy_bots.discard(bot_id)


async def start_bot_handler(request: web.Request) -> web.Response:
    bot_id = _bot_id_from_match_info(request)
    if bot_id is None:
        return web.json_response({"error": "bad bot_id"}, status=404)
    if not await _authenticate_bot_access(request, bot_id):
        return web.json_response({"error": "forbidden"}, status=403)

    async def action():
        b = await get_bot(bot_id)
        if not b:
            return web.json_response({"error": "not found"}, status=404)
        if is_running(bot_id):
            return web.json_response({"ok": True, "already_running": True})
        try:
            pid = await start_bot(bot_id, b["file_path"], b["token"], extra_env=_make_extra_env(b))
            await update_bot_status(bot_id, "running", pid)
            return web.json_response({"ok": True})
        except Exception as e:
            await update_bot_status(bot_id, "error")
            logger.error(f"start_bot_handler: bot_id={bot_id} failed: {e}")
            return web.json_response({"error": "start_failed"}, status=500)

    return await _guard_action(bot_id, action)


async def stop_bot_handler(request: web.Request) -> web.Response:
    bot_id = _bot_id_from_match_info(request)
    if bot_id is None:
        return web.json_response({"error": "bad bot_id"}, status=404)
    if not await _authenticate_bot_access(request, bot_id):
        return web.json_response({"error": "forbidden"}, status=403)
    b = await get_bot(bot_id)
    if not b:
        return web.json_response({"error": "not found"}, status=404)
    await stop_bot(bot_id)
    await update_bot_status(bot_id, "stopped")
    return web.json_response({"ok": True})


async def restart_bot_handler(request: web.Request) -> web.Response:
    bot_id = _bot_id_from_match_info(request)
    if bot_id is None:
        return web.json_response({"error": "bad bot_id"}, status=404)
    if not await _authenticate_bot_access(request, bot_id):
        return web.json_response({"error": "forbidden"}, status=403)

    async def action():
        b = await get_bot(bot_id)
        if not b:
            return web.json_response({"error": "not found"}, status=404)
        await stop_bot(bot_id)
        try:
            pid = await start_bot(bot_id, b["file_path"], b["token"], extra_env=_make_extra_env(b))
            await update_bot_status(bot_id, "running", pid)
            return web.json_response({"ok": True})
        except Exception as e:
            await update_bot_status(bot_id, "error")
            logger.error(f"restart_bot_handler: bot_id={bot_id} failed: {e}")
            return web.json_response({"error": "start_failed"}, status=500)

    return await _guard_action(bot_id, action)


async def delete_bot_handler(request: web.Request) -> web.Response:
    bot_id = _bot_id_from_match_info(request)
    if bot_id is None:
        return web.json_response({"error": "bad bot_id"}, status=404)
    if not await _authenticate_bot_access(request, bot_id):
        return web.json_response({"error": "forbidden"}, status=403)

    async def action():
        b = await get_bot(bot_id)
        if not b:
            return web.json_response({"error": "not found"}, status=404)
        await stop_bot(bot_id)
        await db_delete_bot(bot_id)
        return web.json_response({"ok": True})

    return await _guard_action(bot_id, action)


async def bot_logs_handler(request: web.Request) -> web.Response:
    if not await _authenticate_owner(request):
        return web.json_response({"error": "forbidden"}, status=403)
    bot_id = _bot_id_from_match_info(request)
    if bot_id is None:
        return web.json_response({"error": "bad bot_id"}, status=404)
    b = await get_bot(bot_id)
    if not b:
        return web.json_response({"error": "not found"}, status=404)
    logs = get_bot_logs(bot_id) or ""
    if len(logs) > 3500:
        logs = "...\n" + logs[-3500:]
    return web.json_response({"logs": logs})


async def recreate_bot_handler(request: web.Request) -> web.Response:
    bot_id = _bot_id_from_match_info(request)
    if bot_id is None:
        return web.json_response({"error": "bad bot_id"}, status=404)
    telegram_user_id = await _authenticate_factory_user(request)
    if telegram_user_id is None or not await _authenticate_bot_access(request, bot_id):
        return web.json_response({"error": "forbidden"}, status=403)

    async def action():
        result = await recreate_bot_core(bot_id, telegram_user_id)
        status = 200 if result["ok"] else 422
        return web.json_response(result, status=status)

    return await _guard_action(bot_id, action)


async def autofix_bot_handler(request: web.Request) -> web.Response:
    bot_id = _bot_id_from_match_info(request)
    if bot_id is None:
        return web.json_response({"error": "bad bot_id"}, status=404)
    if not await _authenticate_bot_access(request, bot_id):
        return web.json_response({"error": "forbidden"}, status=403)

    async def action():
        result = await autofix_bot_core(bot_id)
        status = 200 if result["ok"] else 422
        return web.json_response(result, status=status)

    return await _guard_action(bot_id, action)


async def fixbug_bot_handler(request: web.Request) -> web.Response:
    bot_id = _bot_id_from_match_info(request)
    if bot_id is None:
        return web.json_response({"error": "bad bot_id"}, status=404)
    if not await _authenticate_bot_access(request, bot_id):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    description = payload.get("description") if isinstance(payload, dict) else None
    if not isinstance(description, str) or not description.strip():
        return web.json_response({"error": "description is required"}, status=400)

    async def action():
        result = await apply_fix_core(bot_id, description.strip())
        status = 200 if result["ok"] else 422
        return web.json_response(result, status=status)

    return await _guard_action(bot_id, action)


async def list_features_handler(request: web.Request) -> web.Response:
    """GET /api/factory/bots/{id}/features — the "Фичи" tab's own list, same
    shape as bot_detail_handler's "features" key (kept as a separate route
    too so the tab can refresh itself without re-fetching the whole detail
    panel)."""
    bot_id = _bot_id_from_match_info(request)
    if bot_id is None:
        return web.json_response({"error": "bad bot_id"}, status=404)
    if not await _authenticate_bot_access(request, bot_id):
        return web.json_response({"error": "forbidden"}, status=403)
    b = await get_bot(bot_id)
    if not b:
        return web.json_response({"error": "not found"}, status=404)
    template_id = infer_template_id(b.get("file_path"))
    return web.json_response({"items": await _feature_status_items(bot_id, template_id)})


async def disable_feature_handler(request: web.Request) -> web.Response:
    """POST /api/factory/bots/{id}/features/{name}/disable — turning OFF is
    instant, no dialog (see the approved Variant D design: "Выключение
    (on→off) остаётся мгновенным"). Also clears any stored config/thread so
    a later re-enable starts the configure dialog fresh rather than silently
    reusing a stale description."""
    bot_id = _bot_id_from_match_info(request)
    feature_name = request.match_info.get("name", "")
    if bot_id is None or not feature_name:
        return web.json_response({"error": "bad request"}, status=404)
    if not await _authenticate_bot_access(request, bot_id):
        return web.json_response({"error": "forbidden"}, status=403)
    if bot_id in _busy_bots:
        return web.json_response({"error": _BUSY_TEXT}, status=409)
    _busy_bots.add(bot_id)
    try:
        await disable_feature_and_reload(bot_id, feature_name)
        await clear_bot_feature_config(bot_id, feature_name)
        return web.json_response({"ok": True})
    finally:
        _busy_bots.discard(bot_id)


async def cancel_feature_configure_handler(request: web.Request) -> web.Response:
    """POST /api/factory/bots/{id}/features/{name}/cancel — the configure
    dialog's "Отмена" at any step: tumbler reverts to off, nothing is saved
    (see the approved design's "Отмена на любом шаге")."""
    bot_id = _bot_id_from_match_info(request)
    feature_name = request.match_info.get("name", "")
    if bot_id is None or not feature_name:
        return web.json_response({"error": "bad request"}, status=404)
    if not await _authenticate_bot_access(request, bot_id):
        return web.json_response({"error": "forbidden"}, status=403)
    await clear_bot_feature_config(bot_id, feature_name)
    return web.json_response({"ok": True})


async def configure_feature_handler(request: web.Request) -> web.Response:
    """POST /api/factory/bots/{id}/features/{name}/configure — body
    {"message": str}, the owner's latest reply in the free-text configure
    dialog (Variant D). Appends it to the stored thread, asks
    assess_feature_description whether it's enough, and either:
      - accepts: enables the feature, persists the accepted description,
        clears the thread, returns {"status": "enabled", "description": ...}
      - needs more: persists the updated thread (with Claude's follow-up
        appended), returns {"status": "needs_clarification", "reply": ...}

    payments/office_events are rejected here (see _NO_FREE_TEXT_FEATURES) —
    the frontend must not call this route for them; payments routes the
    owner to the Telegram ЮKassa wizard instead, office_events has its own
    bot-picker endpoints."""
    bot_id = _bot_id_from_match_info(request)
    feature_name = request.match_info.get("name", "")
    if bot_id is None or not feature_name:
        return web.json_response({"error": "bad request"}, status=404)
    if not await _authenticate_bot_access(request, bot_id):
        return web.json_response({"error": "forbidden"}, status=403)
    if feature_name in _NO_FREE_TEXT_FEATURES:
        return web.json_response({"error": "feature has no free-text configure step"}, status=400)

    b = await get_bot(bot_id)
    if not b:
        return web.json_response({"error": "not found"}, status=404)
    template_id = infer_template_id(b.get("file_path"))
    feat = next((f for f in discover_features() if f["name"] == feature_name), None)
    if feat is None or (template_id not in feat["compatible_with"] and "*" not in feat["compatible_with"]):
        return web.json_response({"error": "feature not compatible with this bot"}, status=400)

    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    message = payload.get("message") if isinstance(payload, dict) else None
    if not isinstance(message, str) or not message.strip():
        return web.json_response({"error": "message is required"}, status=400)

    if bot_id in _busy_bots:
        return web.json_response({"error": _BUSY_TEXT}, status=409)
    _busy_bots.add(bot_id)
    try:
        existing = await get_bot_feature_config(bot_id, feature_name)
        thread = list(existing["thread"]) if existing else []
        thread.append({"role": "owner", "text": message.strip()})

        try:
            result = await assess_feature_description(feature_name, template_id, thread)
        except Exception as e:
            logger.error(f"configure_feature_handler: assess_feature_description failed bot_id={bot_id} feature={feature_name}: {e}")
            return web.json_response({"error": "assessment_failed"}, status=502)

        if result["accepted"] and result["config_summary"]:
            await set_bot_feature_description(bot_id, feature_name, result["config_summary"])
            await enable_feature_and_reload(bot_id, feature_name)
            return web.json_response(
                {"status": "enabled", "description": result["config_summary"], "reply": result["reply"]}
            )

        thread.append({"role": "claude", "text": result["reply"]})
        await set_bot_feature_thread(bot_id, feature_name, thread)
        return web.json_response({"status": "needs_clarification", "reply": result["reply"], "thread": thread})
    finally:
        _busy_bots.discard(bot_id)


async def list_offices_handler(request: web.Request) -> web.Response:
    bot_id = _bot_id_from_match_info(request)
    if bot_id is None:
        return web.json_response({"error": "bad bot_id"}, status=404)
    if not await _authenticate_bot_access(request, bot_id):
        return web.json_response({"error": "forbidden"}, status=403)
    links = await get_office_links_for_bot(bot_id)
    return web.json_response({"items": links})


async def list_office_event_types_handler(request: web.Request) -> web.Response:
    """GET /api/factory/bots/{id}/offices/event-types — event types THIS bot
    (as a prospective SOURCE) can plausibly publish, for the office-link
    wizard's event-type picker (approved design step 2: "выбор типа события
    из списка, который поддерживает бот-источник"). Derived from the bot's
    own template_id via features/office_events.py's
    available_event_types_for_template() — see that function's docstring for
    why this is a strict, explicit subset of the COMPATIBLE_WITH list rather
    than "every event type this template could theoretically receive"."""
    bot_id = _bot_id_from_match_info(request)
    if bot_id is None:
        return web.json_response({"error": "bad bot_id"}, status=404)
    if not await _authenticate_bot_access(request, bot_id):
        return web.json_response({"error": "forbidden"}, status=403)
    b = await get_bot(bot_id)
    if not b:
        return web.json_response({"error": "not found"}, status=404)
    template_id = infer_template_id(b.get("file_path"))
    event_types = available_event_types_for_template(template_id)
    items = [{"event_type": et, "label": EVENT_TYPE_LABELS.get(et, et)} for et in event_types]
    return web.json_response({"items": items})


async def add_office_handler(request: web.Request) -> web.Response:
    """POST /api/factory/bots/{id}/offices — body {"target_bot_id": int,
    "event_type": str}. This bot (source) will notify target_bot_id about
    event_type events. event_type must be one of the types
    list_office_event_types_handler would offer for this bot's own
    template — re-validated here rather than trusted from the client, same
    "never trust the picker, re-check server-side" posture as
    configure_feature_handler's own template-compatibility re-check."""
    bot_id = _bot_id_from_match_info(request)
    if bot_id is None:
        return web.json_response({"error": "bad bot_id"}, status=404)
    if not await _authenticate_bot_access(request, bot_id):
        return web.json_response({"error": "forbidden"}, status=403)
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    target_bot_id = payload.get("target_bot_id") if isinstance(payload, dict) else None
    event_type = payload.get("event_type") if isinstance(payload, dict) else None
    if not isinstance(target_bot_id, int):
        return web.json_response({"error": "target_bot_id is required"}, status=400)
    if not isinstance(event_type, str) or not event_type:
        return web.json_response({"error": "event_type is required"}, status=400)
    if target_bot_id == bot_id:
        return web.json_response({"error": "cannot link a bot to itself"}, status=400)
    source = await get_bot(bot_id)
    target = await get_bot(target_bot_id)
    if not source or not target:
        return web.json_response({"error": "not found"}, status=404)
    source_template = infer_template_id(source.get("file_path"))
    if event_type not in available_event_types_for_template(source_template):
        return web.json_response({"error": "event_type not available for this bot's template"}, status=400)
    linked = await add_office_link(bot_id, target_bot_id, event_type)
    if not linked:
        return web.json_response({"error": "cannot link bots owned by different customers"}, status=403)
    return web.json_response({"ok": True}, status=201)


async def remove_office_handler(request: web.Request) -> web.Response:
    """DELETE /api/factory/bots/{id}/offices/{target_id}?event_type=...
    — removes the (bot_id, target_id, event_type) link. bot_id in the URL is
    always the SOURCE side, matching add_office_handler above; a link where
    this bot is the TARGET is removed from the other bot's own detail panel
    instead (each panel only ever displays/manages links where IT is the
    source, same posture as the mockup's single-arrow-per-row list).
    event_type is a query param (not a path segment) since it can contain a
    '.' (e.g. "order.created") that would otherwise need URL-segment escaping
    for no real benefit — defaults to _OFFICE_EVENT_TYPE for callers created
    before the event-type picker existed (the Telegram-side officeconnect
    flow still only ever creates order.created links)."""
    bot_id = _bot_id_from_match_info(request)
    target_raw = request.match_info.get("target_id", "")
    if bot_id is None or not target_raw.isdigit():
        return web.json_response({"error": "bad request"}, status=404)
    if not await _authenticate_bot_access(request, bot_id):
        return web.json_response({"error": "forbidden"}, status=403)
    event_type = request.query.get("event_type", _OFFICE_EVENT_TYPE)
    await remove_office_link(bot_id, int(target_raw), event_type)
    return web.json_response({"ok": True})


async def showcase_group_status_handler(request: web.Request) -> web.Response:
    """GET /api/factory/showcase-group — whether the optional office-events
    digest group (db/database.py's office_digest_group, docs/OFFICES_DESIGN.md
    §12 "витрина") is bound. No chat_id/title in the response — the SPA
    never needs to render it, only whether the guide/success screen should
    show "connected" state. Bound either from the miniapp's own success
    screen (not yet wired to write this table — see BotDetailPanel.tsx's
    ShowcaseGroupGuide, currently a read-only guide) or from the
    Telegram-side "🏢 Как витрина связей офисов" button (main.py's
    build_group_router()), both converging on this one table."""
    if not await _authenticate_owner(request):
        return web.json_response({"error": "forbidden"}, status=403)
    chat_id = await get_office_digest_group(OWNER_ID)
    return web.json_response({"connected": chat_id is not None})


async def list_admins_handler(request: web.Request) -> web.Response:
    bot_id = _bot_id_from_match_info(request)
    if bot_id is None:
        return web.json_response({"error": "bad bot_id"}, status=404)
    if not await _authenticate_bot_access(request, bot_id):
        return web.json_response({"error": "forbidden"}, status=403)
    admins = await get_bot_admins(bot_id)
    return web.json_response({"items": admins})


async def add_admin_handler(request: web.Request) -> web.Response:
    bot_id = _bot_id_from_match_info(request)
    if bot_id is None:
        return web.json_response({"error": "bad bot_id"}, status=404)
    if not await _authenticate_bot_access(request, bot_id):
        return web.json_response({"error": "forbidden"}, status=403)
    b = await get_bot(bot_id)
    if not b:
        return web.json_response({"error": "not found"}, status=404)
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    telegram_id = payload.get("telegram_id") if isinstance(payload, dict) else None
    if not isinstance(telegram_id, str) or not telegram_id.strip().isdigit():
        return web.json_response({"error": "telegram_id must be a numeric string"}, status=400)
    await add_bot_admin(bot_id, telegram_id.strip())
    return web.json_response({"ok": True}, status=201)


async def remove_admin_handler(request: web.Request) -> web.Response:
    bot_id = _bot_id_from_match_info(request)
    telegram_id = request.match_info.get("telegram_id", "")
    if bot_id is None or not telegram_id:
        return web.json_response({"error": "bad request"}, status=404)
    if not await _authenticate_bot_access(request, bot_id):
        return web.json_response({"error": "forbidden"}, status=403)
    await remove_bot_admin(bot_id, telegram_id)
    return web.json_response({"ok": True})


def register_routes(app: web.Application) -> None:
    """Adds owner-only analytics + bot-management routes to the same
    Application miniapp_api's register_routes() already extends (see
    combined_app.py's _bootstrap_app, which calls both). Namespaced under
    /api/factory/ to stay clearly distinct from /api/{bot_id}/... 's
    per-tenant-bot paths — a real bot_id is never 'factory'."""
    app.router.add_get("/api/factory/session", refresh_session_handler)
    app.router.add_get("/api/factory/bots", list_bots_handler)
    app.router.add_post("/api/factory/bots/{bot_id}/feedback", add_feedback_handler)
    app.router.add_get("/api/factory/candidates", list_template_candidates_handler)
    app.router.add_get("/api/factory/candidate-clusters", list_template_candidate_clusters_handler)

    # Detail panel — level 2 of the dashboard (docs discussion: "Детальная
    # панель бота — макет уровня 2").
    app.router.add_get("/api/factory/bots/{bot_id}", bot_detail_handler)
    app.router.add_post("/api/factory/bots/{bot_id}/start", start_bot_handler)
    app.router.add_post("/api/factory/bots/{bot_id}/stop", stop_bot_handler)
    app.router.add_post("/api/factory/bots/{bot_id}/restart", restart_bot_handler)
    app.router.add_delete("/api/factory/bots/{bot_id}", delete_bot_handler)
    app.router.add_get("/api/factory/bots/{bot_id}/logs", bot_logs_handler)
    app.router.add_post("/api/factory/bots/{bot_id}/recreate", recreate_bot_handler)
    app.router.add_post("/api/factory/bots/{bot_id}/autofix", autofix_bot_handler)
    app.router.add_post("/api/factory/bots/{bot_id}/fixbug", fixbug_bot_handler)

    app.router.add_get("/api/factory/bots/{bot_id}/features", list_features_handler)
    app.router.add_post("/api/factory/bots/{bot_id}/features/{name}/disable", disable_feature_handler)
    app.router.add_post("/api/factory/bots/{bot_id}/features/{name}/configure", configure_feature_handler)
    app.router.add_post("/api/factory/bots/{bot_id}/features/{name}/cancel", cancel_feature_configure_handler)

    app.router.add_get("/api/factory/bots/{bot_id}/offices", list_offices_handler)
    app.router.add_get("/api/factory/bots/{bot_id}/offices/event-types", list_office_event_types_handler)
    app.router.add_post("/api/factory/bots/{bot_id}/offices", add_office_handler)
    app.router.add_delete("/api/factory/bots/{bot_id}/offices/{target_id}", remove_office_handler)
    app.router.add_get("/api/factory/showcase-group", showcase_group_status_handler)

    app.router.add_get("/api/factory/bots/{bot_id}/admins", list_admins_handler)
    app.router.add_post("/api/factory/bots/{bot_id}/admins", add_admin_handler)
    app.router.add_delete("/api/factory/bots/{bot_id}/admins/{telegram_id}", remove_admin_handler)
