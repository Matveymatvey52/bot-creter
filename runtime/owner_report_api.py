"""Stage 2 of the multitenancy rollout — SYSTEM OWNER cross-owner report.

A THIRD, fully separate web app from the two the miniapp SPA already serves
(the per-bot customer app under /app/{bot_id}, and the owner's own "Моя
фабрика" dashboard under /app/0 — runtime/factory_analytics_api.py). This one
lives at its own top-level route (/owner-report) and is not reachable from
either of those apps' own navigation — see miniapp/src/App.tsx's routing and
runtime/miniapp_api.py's serve_owner_report_shell.

Where factory_analytics_api.py's dashboard is scoped per-owner (an owner sees
only THEIR bots; the system OWNER_ID sees everyone's, via the same routes),
this module is unconditionally OWNER_ID-only for every route — there is no
customer-facing variant of "list every owner's bots and activity across the
whole factory". Reuses _authenticate_owner's exact fail-closed posture
(OWNER_ID == 0 always denies) rather than redefining it — see that function's
own docstring in factory_analytics_api.py.

Two capabilities (see docs — this is Stage 2 of an already-approved 4-stage
design, not a redesign):

1. Activity feed — built entirely from data already logged elsewhere; no new
   access-log table (explicitly rejected in the design: cost/privacy over
   completeness). Three sources merged into one chronological timeline:
     - office_notes: cross-bot office_events activity, one per-bot sqlite
       table (features/office_events.py's generic_on_office_event) — no
       telegram_user_id in that table by design (only a customer_chat_id is
       occasionally embedded in the free-text note), so this source's
       "who" is "n/a" unless the note text happens to mention it.
     - bot_feedback: ratings/comments, central DB, no telegram_user_id
       column either (see db/database.py's bot_feedback table — the miniapp
       feedback form never collected who left it).
     - payments: features/payments.py's per-bot `payments` table — the ONE
       source that actually has a telegram_user_id (payments.user_id).
   Since office_notes/payments live in each bot's OWN per-bot sqlite file
   (not this central DB), there is no single SQL query that can produce this
   feed — it's assembled here by walking the live Registry's bots with a
   resolved db_path, reading each one's recent rows, and merging with the
   central bot_feedback query (db.database.list_feedback_activity).

2. Bot registry table — one row per bot, db.database.list_bots_for_owner_report()
   plus template/from-scratch resolution (runtime.registry.infer_template_id +
   template_candidates) and two things only the live Registry can answer:
   last-activity timestamp (folds in office_notes/payments' own MAX(created_at),
   on top of the central last_edit_at/last_feedback_at columns) and an
   approximate data-volume figure (os.path.getsize on the bot's db_path file —
   deliberately NOT opening/summing every table's row count across every
   per-bot sqlite file, which the design doc flags as a "nice to have", not a
   requirement, precisely because it would be slow/fragile at any real bot
   count).
"""

from __future__ import annotations

import logging
import os

import aiosqlite
from aiohttp import web

from db.database import (
    get_all_bots,
    list_bots_for_owner_report,
    list_feedback_activity,
    list_template_candidate_clusters_with_stats,
    list_template_candidates,
)
from runtime.factory_analytics_api import _authenticate_owner
from runtime.registry import FACTORY_BOT_ID, Registry, infer_template_id
from runtime.webhook_app import REGISTRY_KEY

logger = logging.getLogger(__name__)

# Recent rows pulled per-bot per-source when assembling the activity feed —
# a cap, not a real page size: each bot's own office_notes/payments table is
# read independently (they're separate sqlite files, no cross-bot LIMIT is
# possible in one query), then everything is merged and re-sorted in Python
# before the real limit/offset pagination is applied. Generous enough that a
# single very-active bot doesn't starve the merged feed of its own recent
# rows before pagination even sees them, small enough to keep each per-bot
# file read cheap.
_PER_BOT_SOURCE_LIMIT = 100

# Hard cap on how many bots' per-bot sqlite files get opened for one activity
# feed request — an internal tool with a runaway bot count should degrade
# (skip the oldest-created excess bots) rather than the request itself
# timing out opening hundreds of files one at a time.
_MAX_BOTS_SCANNED_FOR_ACTIVITY = 200


async def _authenticate_system_owner(request: web.Request) -> bool:
    """Same as factory_analytics_api._authenticate_owner: Telegram initData
    or magic-link token, scoped to FACTORY_BOT_ID, and the resulting
    telegram_user_id must equal OWNER_ID. Every route in this module uses
    this — unlike factory_analytics_api.py, there is no per-customer variant
    of any route here."""
    return await _authenticate_owner(request)


def _template_or_from_scratch(file_path: str | None, from_scratch_bot_types: dict[int, str], bot_id: int) -> str:
    template_id = infer_template_id(file_path)
    if template_id:
        return template_id
    bot_type = from_scratch_bot_types.get(bot_id)
    return f"from-scratch ({bot_type})" if bot_type else "from-scratch"


async def _bot_activity_and_volume(registry: Registry, bot_id: int, central_last_activity: str | None) -> tuple[str | None, int | None]:
    """Returns (last_activity_at, approx_data_volume_bytes) for one bot.
    last_activity_at folds in this bot's own office_notes/payments MAX(created_at)
    on top of whatever central_last_activity (max of bot_feedback.created_at,
    bot_custom_features.applied_at) was already computed by the caller from
    list_bots_for_owner_report()'s SQL join — both are plain 'YYYY-MM-DD
    HH:MM:SS'-shaped strings (SQLite's own datetime()/CURRENT_TIMESTAMP
    defaults), so a lexicographic max is a valid chronological max.

    Returns (central_last_activity, None) unchanged if the bot has no live
    Registry entry or no resolved db_path (not currently running, or a
    registry-less state) — same "degrade to unavailable" contract as
    features/sales_analytics.py's weekly_record_count."""
    entry = registry.get(bot_id)
    if entry is None:
        return central_last_activity, None
    db_path = entry.config.get("db_path") if isinstance(entry.config, dict) else None
    if not db_path:
        return central_last_activity, None

    volume_bytes: int | None = None
    try:
        volume_bytes = os.path.getsize(db_path)
    except OSError:
        volume_bytes = None

    last_activity = central_last_activity
    try:
        async with aiosqlite.connect(db_path) as db:
            for table in ("office_notes", "payments"):
                try:
                    async with db.execute(f'SELECT MAX(created_at) FROM "{table}"') as cursor:
                        row = await cursor.fetchone()
                except aiosqlite.OperationalError:
                    continue  # table doesn't exist on this bot — not every template has payments/office_notes
                if row and row[0] and (last_activity is None or row[0] > last_activity):
                    last_activity = row[0]
    except Exception:
        logger.warning(f"_bot_activity_and_volume: failed to read db_path for bot_id={bot_id}", exc_info=True)

    return last_activity, volume_bytes


async def list_bots_handler(request: web.Request) -> web.Response:
    """GET /api/owner-report/bots — the bot registry table. Every column the
    approved design asked for; see module docstring for what's live-Registry
    -derived vs. straight off the central DB."""
    if not await _authenticate_system_owner(request):
        return web.json_response({"error": "forbidden"}, status=403)

    registry: Registry = request.app[REGISTRY_KEY]
    rows = await list_bots_for_owner_report()
    candidates = await list_template_candidates()
    from_scratch_bot_types = {
        c["bot_id"]: c["bot_type"] for c in candidates if c["bot_id"] is not None and c["bot_type"]
    }

    items = []
    for row in rows:
        features = [f for f in row["features"].split(",") if f]
        central_last_activity = max(
            (t for t in (row["last_feedback_at"], row["last_edit_at"]) if t),
            default=None,
        )
        last_activity, volume_bytes = await _bot_activity_and_volume(registry, row["id"], central_last_activity)
        items.append(
            {
                "id": row["id"],
                "name": row["name"],
                "username": row["username"],
                "display_name": row["display_name"],
                "status": row["status"],
                "created_at": row["created_at"],
                "archived_at": row["archived_at"],
                "owner_telegram_id": row["owner_telegram_id"],
                # No username-resolution source exists anywhere in the data for
                # an owner/customer (bot_admins only stores numeric telegram_id
                # strings — see db/database.py) — the numeric id is shown as-is,
                # per the design's own "if nothing has it, just show the
                # numeric id" fallback.
                "owner_display": str(row["owner_telegram_id"]) if row["owner_telegram_id"] is not None else None,
                "creation_prompt": row["creation_prompt"],
                "template": _template_or_from_scratch(row["file_path"], from_scratch_bot_types, row["id"]),
                "features": features,
                "edits_count": row["edits_count"],
                "avg_rating": row["avg_rating"],
                "feedback_count": row["feedback_count"],
                "payments_connected": bool(row["payments_connected"]),
                "last_activity_at": last_activity,
                "approx_data_volume_bytes": volume_bytes,
            }
        )
    return web.json_response({"items": items})


async def _office_notes_activity(registry: Registry, bots_by_id: dict[int, dict]) -> list[dict]:
    """Reads recent office_notes rows out of every bot's OWN per-bot sqlite
    file (there is no central table — see module docstring). Skipped
    entirely for a bot with no live Registry entry/db_path, or where the
    table doesn't exist (most templates never receive an office_event)."""
    entries: list[dict] = []
    for bot_id in registry.bot_ids()[:_MAX_BOTS_SCANNED_FOR_ACTIVITY]:
        if bot_id == FACTORY_BOT_ID or bot_id not in bots_by_id:
            continue
        entry = registry.get(bot_id)
        if entry is None:
            continue
        db_path = entry.config.get("db_path") if isinstance(entry.config, dict) else None
        if not db_path:
            continue
        bot_row = bots_by_id[bot_id]
        try:
            async with aiosqlite.connect(db_path) as db:
                db.row_factory = aiosqlite.Row
                try:
                    async with db.execute(
                        "SELECT id, source_bot_id, event_type, note, created_at FROM office_notes "
                        "ORDER BY created_at DESC LIMIT ?",
                        (_PER_BOT_SOURCE_LIMIT,),
                    ) as cursor:
                        rows = await cursor.fetchall()
                except aiosqlite.OperationalError:
                    continue  # this bot never received an office_event — no office_notes table
        except Exception:
            logger.warning(f"_office_notes_activity: failed to read db_path for bot_id={bot_id}", exc_info=True)
            continue
        for r in rows:
            entries.append(
                {
                    "source": "office_event",
                    "bot_id": bot_id,
                    "bot_name": bot_row["name"],
                    "owner_telegram_id": bot_row["owner_telegram_id"],
                    "telegram_user_id": None,  # office_notes has no user identity column — see module docstring
                    "event_type": r["event_type"],
                    "detail": r["note"],
                    "created_at": r["created_at"],
                }
            )
    return entries


async def _payments_activity(registry: Registry, bots_by_id: dict[int, dict]) -> list[dict]:
    """Same shape as _office_notes_activity above but for each bot's own
    `payments` table (features/payments.py) — the one source with a real
    telegram_user_id (payments.user_id)."""
    entries: list[dict] = []
    for bot_id in registry.bot_ids()[:_MAX_BOTS_SCANNED_FOR_ACTIVITY]:
        if bot_id == FACTORY_BOT_ID or bot_id not in bots_by_id:
            continue
        entry = registry.get(bot_id)
        if entry is None:
            continue
        db_path = entry.config.get("db_path") if isinstance(entry.config, dict) else None
        if not db_path:
            continue
        bot_row = bots_by_id[bot_id]
        try:
            async with aiosqlite.connect(db_path) as db:
                db.row_factory = aiosqlite.Row
                try:
                    async with db.execute(
                        "SELECT id, user_id, total_amount, currency, status, created_at FROM payments "
                        "ORDER BY created_at DESC LIMIT ?",
                        (_PER_BOT_SOURCE_LIMIT,),
                    ) as cursor:
                        rows = await cursor.fetchall()
                except aiosqlite.OperationalError:
                    continue  # payments feature not enabled/never used on this bot
        except Exception:
            logger.warning(f"_payments_activity: failed to read db_path for bot_id={bot_id}", exc_info=True)
            continue
        for r in rows:
            entries.append(
                {
                    "source": "payment",
                    "bot_id": bot_id,
                    "bot_name": bot_row["name"],
                    "owner_telegram_id": bot_row["owner_telegram_id"],
                    "telegram_user_id": r["user_id"],
                    "event_type": r["status"],  # 'paid' or 'refunded'
                    "detail": f"{r['total_amount']} {r['currency']}",
                    "created_at": r["created_at"],
                }
            )
    return entries


def _feedback_activity_to_entries(rows: list[dict]) -> list[dict]:
    entries = []
    for r in rows:
        entries.append(
            {
                "source": "feedback",
                "bot_id": r["bot_id"],
                "bot_name": r["bot_name"],
                "owner_telegram_id": r["owner_telegram_id"],
                "telegram_user_id": None,  # bot_feedback has no user identity column either
                "event_type": f"rating={r['rating']}",
                "detail": r["comment"],
                "created_at": r["created_at"],
            }
        )
    return entries


def _parse_int_query(request: web.Request, name: str) -> int | None:
    raw = request.query.get(name)
    if raw is None or not raw.strip().lstrip("-").isdigit():
        return None
    return int(raw)


async def list_activity_handler(request: web.Request) -> web.Response:
    """GET /api/owner-report/activity — the merged, paginated activity feed.
    Optional query params: owner_id, bot_id (both filters — see module
    docstring on why office_notes/payments are read per-bot rather than
    queried), limit (default 50, capped at 200), offset (default 0).

    Pagination is applied AFTER merging all three sources in Python (there
    is no single backing query to paginate against — see module docstring),
    so "page 2" of a very large combined feed is O(all rows read so far) —
    acceptable for an internal tool at this data volume, called out here
    rather than hidden."""
    if not await _authenticate_system_owner(request):
        return web.json_response({"error": "forbidden"}, status=403)

    owner_filter = _parse_int_query(request, "owner_id")
    bot_filter = _parse_int_query(request, "bot_id")
    limit_raw = _parse_int_query(request, "limit")
    limit = max(1, min(limit_raw, 200)) if limit_raw is not None else 50
    offset = max(0, _parse_int_query(request, "offset") or 0)

    registry: Registry = request.app[REGISTRY_KEY]
    all_bots = await get_all_bots()
    bots_by_id = {b["id"]: b for b in all_bots}
    if owner_filter is not None:
        bots_by_id = {bid: b for bid, b in bots_by_id.items() if b.get("owner_telegram_id") == owner_filter}
    if bot_filter is not None:
        bots_by_id = {bid: b for bid, b in bots_by_id.items() if bid == bot_filter}

    feedback_rows = await list_feedback_activity(owner_telegram_id=owner_filter, bot_id=bot_filter)
    entries = _feedback_activity_to_entries(feedback_rows)
    entries += await _office_notes_activity(registry, bots_by_id)
    entries += await _payments_activity(registry, bots_by_id)

    entries.sort(key=lambda e: e["created_at"] or "", reverse=True)
    total = len(entries)
    page = entries[offset : offset + limit]
    return web.json_response({"items": page, "total": total, "limit": limit, "offset": offset})


async def list_template_candidates_handler(request: web.Request) -> web.Response:
    """"Кандидаты на новый шаблон" section (docs/TEMPLATE_CANDIDATE_LOGGING_DESIGN.md)
    — raw rows, most recent first, no server-side clustering (MVP decision:
    let the owner read the actual requirement text). creator_user_id is
    deliberately excluded from the response, same posture as list_bots_handler
    excluding `token` — an internal Telegram user id the dashboard UI has no
    use for. Moved here from factory_analytics_api.py (owner instruction,
    2026-08-18): this section belongs to the owner-only cross-owner report,
    not "Моя фабрика" — it was never per-owner data to begin with."""
    if not await _authenticate_system_owner(request):
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
    they still show up in list_template_candidates_handler's raw feed above.
    Moved here from factory_analytics_api.py alongside the handler above —
    same reasoning."""
    if not await _authenticate_system_owner(request):
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


def register_routes(app: web.Application) -> None:
    """Adds the owner-only cross-owner report routes to the same Application
    factory_analytics_api.py/miniapp_api.py already extend (see
    combined_app.py's _bootstrap_app). Namespaced under /api/owner-report/ —
    clearly distinct from both /api/factory/... (the per-owner dashboard)
    and /api/{bot_id}/... (per-tenant-bot routes)."""
    app.router.add_get("/api/owner-report/bots", list_bots_handler)
    app.router.add_get("/api/owner-report/activity", list_activity_handler)
    app.router.add_get("/api/owner-report/candidates", list_template_candidates_handler)
    app.router.add_get("/api/owner-report/candidate-clusters", list_template_candidate_clusters_handler)
