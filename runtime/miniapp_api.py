"""Mini-app REST layer — Stage 3 pilot (tour_operator).

Adds routes to the SAME aiohttp Application the webhook already runs on (see
docs/MINIAPP_DESIGN.md §2.2 for why: tour_operator's own standalone
build_web_app() is disabled in webhook mode precisely because a second
web.TCPSite on the same PORT collides with webhook_app.py's — see that
template's "Стоп: веб-часть не ложится на паттерн" docstring). This module
never binds its own server; register_routes() is called by combined_app.py
against the one Application webhook_app.create_app() already built.

Template-agnostic by design: this file has ZERO tour_operator-specific
knowledge. It reads a `miniapp_config` dict off the already-loaded template
module (same by-convention lookup runtime/registry.py already uses for
`router`/`init_db`/`config_from_bot_row`) and serves generic list/detail/
create CRUD against whatever resources that config declares. A template with
no `miniapp_config` simply has no mini-app — /api/{bot_id}/* 404s, nothing
else changes (same degrade-gracefully contract as a missing `router` in
runtime/registry.py's build_entry()).

Auth: two independent paths converging on the same `telegram_user_id`,
because the SPA is one build serving both Telegram WebView and a plain
browser tab (see docs/MINIAPP_DESIGN.md §2.1):
  - Telegram Mini App: `initData` HMAC-verified per Telegram's documented
    scheme (secret_key = HMAC_SHA256("WebAppData", bot_token), hash checked
    against every other field).
  - Browser: a short-lived, HMAC-signed token minted server-side (Telegram
    bot's own /app command generates the link) — see mint_magic_link_token().
    Deliberately NOT the raw `user_id` tour_operator.py's own /app command
    used to embed (see MINIAPP_DESIGN.md §2.1: "токен там пока что просто
    user_id без подписи/срока действия — это НАДО исправить, не копировать
    как есть").
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

import aiosqlite
from aiohttp import web

from db.database import (
    get_bot,
    get_bot_admins,
    get_bot_features,
    get_bot_miniapp_config,
    get_bot_office_hook_config,
)
from features.excel_export import MAX_FILE_SIZE, build_workbook, fetch_export_rows
from features.sales_analytics import Period, compute_metrics
from runtime.registry import FACTORY_BOT_ID, Registry, _load_template_module_async
from services.client_link import ContactFormatError, parse_contact

logger = logging.getLogger(__name__)

# The SPA build (miniapp/dist/, produced by `npm run build` — see
# miniapp/vite.config.ts's base:'./' setting, which is what makes this ONE
# build servable from any /app/{bot_id} path). Same static index.html/JS/CSS
# for every bot; bot_id only affects which API calls the loaded page makes,
# not which files get served — see docs/MINIAPP_DESIGN.md §2.3.
_MINIAPP_DIST_DIR = Path(__file__).parent.parent / "miniapp" / "dist"

# Magic-link tokens expire quickly — this is a "click the link now" flow off
# a freshly-sent Telegram message, not a durable session; a stale bookmarked
# link should stop working rather than stay a permanent bypass of Telegram
# login. The SPA is expected to exchange it for its own longer-lived cookie/
# localStorage session on first load (out of scope for this pilot's backend).
MAGIC_LINK_TTL_SECONDS = 15 * 60

# The standalone website (/site/{bot_id}, docs/MINIAPP_WEBSITE_REDESIGN_DESIGN.md
# Task B) is a "opened it on a laptop, working for a while" session, not a
# "click the link now" Telegram WebView open — 24h instead of 15min. Same
# token format/verification as the Telegram-flow token above (expiry is
# embedded in the signed payload, so _verify_magic_link_token needs no
# separate TTL-aware code path); only the TTL used at mint time differs.
SITE_LINK_TTL_SECONDS = 24 * 60 * 60


def _miniapp_secret() -> str:
    return os.getenv("MINIAPP_SECRET", "")


def mint_magic_link_token(bot_id: int, telegram_user_id: int, ttl_seconds: int = MAGIC_LINK_TTL_SECONDS) -> str:
    """Signs `bot_id:telegram_user_id:expiry` with HMAC-SHA256 over
    MINIAPP_SECRET. Called by a template's /app command handler (e.g.
    tour_operator.py's cmd_app) to build the link it sends the user — see
    that template's own TODO to stop embedding the bare user_id.

    `ttl_seconds` defaults to the short Telegram-WebView-flow TTL
    (MAGIC_LINK_TTL_SECONDS) so every existing caller (factory dashboard's
    /api/factory/session, tour_operator.py's cmd_app) is unaffected; pass a
    longer value (e.g. SITE_LINK_TTL_SECONDS) for the standalone-website flow.

    Raises RuntimeError if MINIAPP_SECRET is unset, rather than silently
    minting an unsigned/forgeable token — same fail-closed posture as
    webhook_app.py's WEBHOOK_SECRET check, just raised instead of
    request-rejected since this runs on the Telegram-handler side, not a
    request handler."""
    secret = _miniapp_secret()
    if not secret:
        raise RuntimeError("MINIAPP_SECRET is not set — cannot mint a magic-link token")
    expiry = int(time.time()) + ttl_seconds
    payload = f"{bot_id}:{telegram_user_id}:{expiry}"
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{sig}"


def mint_site_link_token(bot_id: int, telegram_user_id: int) -> str:
    """Convenience wrapper over mint_magic_link_token() with the longer
    SITE_LINK_TTL_SECONDS TTL — what a template's "🌐 Открыть на сайте"
    button (e.g. tour_operator.py's cmd_app) should call instead of
    mint_magic_link_token() directly, so the TTL choice for that flow lives
    in one place."""
    return mint_magic_link_token(bot_id, telegram_user_id, ttl_seconds=SITE_LINK_TTL_SECONDS)


def _verify_magic_link_token(token: str, bot_id: int) -> int | None:
    """Returns the embedded telegram_user_id if `token` is validly signed,
    unexpired, and scoped to this bot_id — else None. Constant-time
    signature comparison (hmac.compare_digest), same as webhook_app.py's
    WEBHOOK_SECRET check, to avoid a timing side-channel on the signature."""
    secret = _miniapp_secret()
    if not secret:
        return None
    parts = token.split(":")
    if len(parts) != 4:
        return None
    token_bot_id, user_id, expiry, sig = parts
    payload = f"{token_bot_id}:{user_id}:{expiry}"
    expected_sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected_sig):
        return None
    try:
        if token_bot_id != str(bot_id):
            return None
        if int(expiry) < int(time.time()):
            return None
        return int(user_id)
    except ValueError:
        return None


def _verify_telegram_init_data(init_data: str, bot_token: str) -> int | None:
    """Verifies Telegram Mini App `initData` per Telegram's documented HMAC
    scheme and returns the embedded telegram_user_id, or None if invalid/
    missing/malformed. secret_key = HMAC_SHA256(key="WebAppData",
    msg=bot_token); every OTHER field (sorted, minus `hash` itself) is
    HMAC_SHA256-signed with that secret_key and compared to the `hash` field.
    No expiry check here — Telegram itself controls how fresh initData is by
    only handing it to the WebView on open; this function only verifies
    authenticity, not freshness."""
    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(received_hash, expected_hash):
        return None
    user_raw = pairs.get("user")
    if not user_raw:
        return None
    try:
        user = json.loads(user_raw)
        return int(user["id"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


async def _authenticate(request: web.Request, bot_id: int, bot_token: str) -> int | None:
    """Single entry point both route handlers below call. Tries Telegram
    initData first (sent as `X-Telegram-Init-Data` header by the SPA when
    running inside the Telegram WebView), then falls back to the magic-link
    `?token=` query param (browser tab, no Telegram context available).
    Returns the authenticated telegram_user_id, or None (caller responds
    403)."""
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    if init_data:
        return _verify_telegram_init_data(init_data, bot_token)
    token = request.query.get("token", "")
    if token:
        return _verify_magic_link_token(token, bot_id)
    return None


def _bad_bot_id(bot_id_raw: str) -> web.Response | None:
    if not bot_id_raw.isdigit():
        return web.json_response({"error": "bad bot_id"}, status=404)
    return None


async def _resolve_entry_and_config(request: web.Request) -> tuple[int, Any, Any] | web.Response:
    """Shared prologue for every /api/{bot_id}/... handler: validates bot_id,
    looks the bot up in the live Registry, and loads its miniapp_config (if
    any). Returns (bot_id, entry, miniapp_config) on success, or an
    already-built error web.Response the caller should return as-is.

    miniapp_config lookup order (see docs/MINIAPP_DESIGN.md §6): the factory
    DB (db.database.bot_miniapp_config) is authoritative for anything
    generated via generate_bot_code()/custom_features — DB is checked first.
    Falls back to the template module's own `miniapp_config` attribute (the
    pre-DB pilot mechanism, still how templates/tour_operator.py's
    hand-authored config is served) only when the DB has no row for this
    bot, so existing bots created before this table existed keep working."""
    bot_id_raw = request.match_info.get("bot_id", "")
    if err := _bad_bot_id(bot_id_raw):
        return err
    bot_id = int(bot_id_raw)

    registry: Registry = request.app[REGISTRY_KEY]
    entry = registry.get(bot_id)
    if entry is None:
        return web.json_response({"error": "unknown bot"}, status=404)

    miniapp_config = await get_bot_miniapp_config(bot_id)
    if miniapp_config is None and entry.template_id:
        module = await _load_template_module_async(entry.template_id)
        miniapp_config = getattr(module, "miniapp_config", None) if module is not None else None
    if miniapp_config is None:
        return web.json_response({"error": "mini-app not available for this bot"}, status=404)

    return bot_id, entry, miniapp_config


async def bot_session_handler(request: web.Request) -> web.Response:
    """GET /api/{bot_id}/session — mints a fresh, full-TTL site token for the
    standalone website (/site/{bot_id}), modeled on
    runtime/factory_analytics_api.py's refresh_session_handler. Requires an
    already-valid credential (current token or Telegram initData) — this
    renews a live session, it doesn't mint one from nothing. The SPA
    (lib/api.ts) calls this on a timer well inside SITE_LINK_TTL_SECONDS so a
    still-open tab keeps working past the original link's TTL."""
    resolved = await _resolve_entry_and_config(request)
    if isinstance(resolved, web.Response):
        return resolved
    bot_id, entry, _config = resolved

    telegram_user_id = await _authenticate(request, bot_id, entry.bot.token)
    if telegram_user_id is None:
        return web.json_response({"error": "forbidden"}, status=403)
    token = mint_site_link_token(bot_id, telegram_user_id)
    return web.json_response({"token": token})


async def me_handler(request: web.Request) -> web.Response:
    resolved = await _resolve_entry_and_config(request)
    if isinstance(resolved, web.Response):
        return resolved
    bot_id, entry, _config = resolved

    telegram_user_id = await _authenticate(request, bot_id, entry.bot.token)
    if telegram_user_id is None:
        return web.json_response({"error": "forbidden"}, status=403)
    return web.json_response({"bot_id": bot_id, "telegram_user_id": telegram_user_id})


# Resource keys the SPA needs to render generic list/detail/create screens —
# everything except backend-only plumbing ("table", "order_by") that has no
# UI meaning and would otherwise leak the bot's raw SQL table names to the
# client for no reason. The frontend used to hardcode this exact shape by
# hand in miniapp/src/lib/resources.ts (Phase 2 design doc §2.4); this
# endpoint is what replaces that file.
#
# "children" and "ref" are the detail-card metadata: `children` declares
# sub-records to inline in the PARENT's detail screen (so a tour's
# program/hotels/guests/cashflow live on the tour itself, not in far-away
# tabs), and a field's `ref` declares it a foreign key so the form renders a
# picker over that resource's human-readable titles instead of a raw ID
# input. Both are display metadata only — neither can widen what the role
# filter and admin gate already allow (see _related_for_item).
_SCHEMA_RESOURCE_KEYS = ("name", "creatable", "title", "titleField", "children", "tableView")
_SCHEMA_FIELD_KEYS = ("name", "required", "creatable", "label", "kind", "list", "detail", "create", "ref")


def _resource_display_schema(resource: dict) -> dict:
    """Strips a miniapp_config resource down to what the SPA needs to render
    it — same shape whether the config came from a template's hand-authored
    dict or the Haiku generator, since both now emit the same display keys
    (see services/claude_service.py's MINIAPP_CONFIG_SYSTEM_PROMPT). Fields
    the config is missing (older, pre-Phase-2 configs) are simply absent from
    the response — the SPA fills sane defaults itself rather than the
    backend inventing labels it has no business guessing."""
    out = {k: resource[k] for k in _SCHEMA_RESOURCE_KEYS if k in resource}
    out["fields"] = [
        {k: field[k] for k in _SCHEMA_FIELD_KEYS if k in field}
        for field in resource.get("fields", [])
    ]
    return out


async def schema_handler(request: web.Request) -> web.Response:
    """GET /api/{bot_id}/schema — the display-schema counterpart to
    list/get/create's data endpoints below. Same auth and resolution as
    every other per-bot route; returns the resources this bot's mini-app
    should render, with display metadata only (no table/order_by/SQL
    plumbing) — see _resource_display_schema()."""
    resolved = await _resolve_entry_and_config(request)
    if isinstance(resolved, web.Response):
        return resolved
    bot_id, entry, miniapp_config = resolved

    telegram_user_id = await _authenticate(request, bot_id, entry.bot.token)
    if telegram_user_id is None:
        return web.json_response({"error": "forbidden"}, status=403)

    specs = miniapp_config.get("resources", [])
    resources = [_resource_display_schema(r) for r in specs]

    # canCreate is the ONE thing the SPA cannot work out for itself: whether
    # POST /{resource} would actually succeed for THIS viewer. Without it the
    # list screen showed a "Добавить" button on every resource and the create
    # form rendered an empty body for read-only ones, ending in a 403
    # "resource is read-only" — a form nobody could ever submit. Mirrors
    # create_resource_handler's own gates exactly (creatable flag → admin gate
    # → role filter), so a true here means the POST is genuinely allowed.
    db_path = entry.config.get("db_path") if isinstance(entry.config, dict) else None
    for out, spec in zip(resources, specs):
        out["canCreate"] = await _can_create(bot_id, spec, telegram_user_id, db_path)

    return web.json_response({"resources": resources, "bot": await _bot_identity(bot_id)})


async def _bot_identity(bot_id: int) -> dict:
    """Имя и подпись бота для шапки мини-аппа (эталон
    design/mockups/miniapp_mockup_I.html: квадрат с инициалами · название ·
    род занятий). До этого /schema отдавал только ресурсы, и SPA нечем было
    заполнить шапку — она просто отсутствовала.

    Берём ровно два поля из строки bots и ничего больше: get_bot() возвращает
    расшифрованную строку целиком, включая token, — он не должен утечь
    клиенту. display_name (то, как владелец назвал бота для людей) важнее
    технического name; description — вольный текст владельца, поэтому он
    может быть пустым, и тогда SPA просто не рисует вторую строку.
    """
    row = await get_bot(bot_id) or {}
    name = (row.get("display_name") or row.get("name") or "").strip()
    subtitle = (row.get("description") or "").strip()
    return {"name": name or None, "subtitle": subtitle or None}


async def _can_create(bot_id: int, resource: dict, telegram_user_id: int, db_path: str | None) -> bool:
    """Predicts create_resource_handler's verdict for this viewer without
    performing a write. Fails closed on any error: an unreadable DB or a
    malformed role_filter hides the create affordance rather than offering a
    button whose POST would 500."""
    if not resource.get("creatable", False):
        return False
    if resource.get("create_requires") == "bot_admin":
        return str(telegram_user_id) in await get_bot_admins(bot_id)
    if not await _admin_gate_ok(bot_id, resource, telegram_user_id):
        return False
    if resource.get("role_filter") is None:
        return True
    if not db_path:
        return False
    try:
        async with aiosqlite.connect(db_path) as db:
            await _resolve_role_filter_where(db, resource["role_filter"], telegram_user_id)
    except RoleFilterDenied:
        return False
    except Exception:
        logger.exception(
            "_can_create: bot_id=%s resource=%s — role_filter evaluation failed, hiding create",
            bot_id,
            resource.get("name"),
        )
        return False
    return True


async def features_handler(request: web.Request) -> web.Response:
    """GET /api/{bot_id}/features — which optional features the owner actually
    switched on for this bot (db.database.bot_features, the same table
    combined_app.py's reminder sweep and userbot_worker.py gate on).

    The SPA needs this because a section like "Аналитика" is no longer part of
    every bot's default navigation: it appears only when its feature is
    enabled here. Returns the raw enablement list, NOT a per-viewer verdict —
    analytics_handler stays the one place that decides owner-only access, so a
    non-owner learning that the feature exists reveals nothing they could not
    already infer from the bot's own menu."""
    resolved = await _resolve_entry_and_config(request)
    if isinstance(resolved, web.Response):
        return resolved
    bot_id, entry, _config = resolved

    telegram_user_id = await _authenticate(request, bot_id, entry.bot.token)
    if telegram_user_id is None:
        return web.json_response({"error": "forbidden"}, status=403)

    try:
        features = await get_bot_features(bot_id)
    except Exception:
        # Degrade to "no optional sections" rather than blank-screening the
        # whole app over a feature list that is purely additive navigation.
        logger.exception("features_handler: bot_id=%s — failed to read bot_features", bot_id)
        features = []
    return web.json_response({"features": features})


def _resource_spec(miniapp_config: dict, resource_name: str) -> dict | None:
    for resource in miniapp_config.get("resources", []):
        if resource.get("name") == resource_name:
            return resource
    return None


# Only :telegram_user_id is meaningful today — the engine has no other
# per-viewer identity to bind. Keeping this an explicit whitelist (rather
# than "any :identifier-looking token") is what lets _compile_role_where
# reject an unrecognized template with a clear error instead of executing
# a predicate shape nobody reviewed.
_ROLE_WHERE_ALLOWED_PARAMS = frozenset({"telegram_user_id"})

# WHY this narrow a shape: the where-template is authored by whoever writes
# miniapp_config (a human or the Haiku generator), not by an HTTP caller —
# but it still ends up interpolated into SQL text (values are bound, the
# template's SQL keywords/column names are not). Restricting the character
# set blocks the template itself from smuggling extra clauses/statements;
# real parameter binding is still what keeps VALUES safe.
_ROLE_WHERE_SAFE_RE = re.compile(r"^[A-Za-z0-9_.\s=<>!()':]+$")


class RoleFilterError(ValueError):
    """Raised when a resource's role_filter.rules[*].where doesn't match the
    whitelisted safe-predicate shape — surfaced as a 500 rather than ever
    executing an unvalidated SQL string."""


def _compile_role_where(where: str) -> tuple[str, tuple[str, ...]]:
    """Parses `:identifier`-style named placeholders out of a where-template
    and returns (sql_with_qmarks, ordered_param_names) ready for aiosqlite's
    positional binding. Rejects anything containing a placeholder outside
    _ROLE_WHERE_ALLOWED_PARAMS, or any character outside the safe-predicate
    whitelist, rather than trying to guess intent."""
    if not _ROLE_WHERE_SAFE_RE.match(where):
        raise RoleFilterError(f"role_filter where-template has disallowed characters: {where!r}")
    param_names: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in _ROLE_WHERE_ALLOWED_PARAMS:
            raise RoleFilterError(f"role_filter where-template uses unsupported placeholder :{name}")
        param_names.append(name)
        return "?"

    sql = re.sub(r":([A-Za-z_][A-Za-z0-9_]*)", _replace, where)
    return sql, tuple(param_names)


async def _resolve_viewer_roles(db: aiosqlite.Connection, resolve: dict, telegram_user_id: int) -> set[str]:
    table = resolve["table"]
    identity_column = resolve["identity_column"]
    role_column = resolve["role_column"]
    async with db.execute(
        f"SELECT {role_column} FROM {table} WHERE {identity_column} = ?", (telegram_user_id,)
    ) as cursor:
        rows = await cursor.fetchall()
    return {row[0] for row in rows}


class RoleFilterDenied(Exception):
    """Viewer holds none of role_filter.rules' declared roles and
    default_deny applies — caller translates this to an empty list (list
    endpoints) or 403 (get/create endpoints), never an unfiltered query.
    Not raised by the ownership-only shape (no `resolve`/`rules`) — that
    shape has no roles to hold or deny, it always applies its `where`."""


async def _resolve_role_filter_where(
    db: aiosqlite.Connection,
    role_filter: dict,
    telegram_user_id: int,
) -> str | None:
    """Returns the where-template that applies for this viewer, or None
    (unfiltered). Two role_filter shapes, per
    docs/MINIAPP_ROLE_SCOPING_DESIGN.md:

    - Ownership-only (no `resolve` key): `{"where": "<template>"}` applies
      unconditionally to every authenticated viewer — no role concept, no
      resolve table, no default_deny (there is nothing to hold or deny; it
      is a plain row-ownership predicate). For templates like
      habit_tracker where every user owns their own rows and there is no
      separate roles table at all, only a per-row owner column.
    - Role-resolved (has `resolve`): the original pilot shape — resolves
      the viewer's role set from `resolve`, matches the first rule in
      `rules` whose role the viewer holds, raises RoleFilterDenied when
      none match and default_deny applies (defaults True, fail closed)."""
    if "resolve" not in role_filter:
        return role_filter.get("where")

    viewer_roles = await _resolve_viewer_roles(db, role_filter["resolve"], telegram_user_id)
    matched_rule = None
    for rule in role_filter.get("rules", []):
        if rule["role"] in viewer_roles:
            matched_rule = rule
            break

    if matched_rule is None:
        if role_filter.get("default_deny", True):
            raise RoleFilterDenied()
        return None

    return matched_rule.get("where")


async def _apply_role_filter(
    db: aiosqlite.Connection,
    resource: dict,
    telegram_user_id: int,
) -> tuple[str, tuple] | None:
    """Returns None when the resource has no role_filter (today's behavior,
    unchanged) or the applicable where is None (unfiltered). Otherwise
    returns (sql_fragment, params) for the caller to AND onto its own
    query. May raise RoleFilterDenied — see _resolve_role_filter_where."""
    role_filter = resource.get("role_filter")
    if role_filter is None:
        return None

    where = await _resolve_role_filter_where(db, role_filter, telegram_user_id)
    if where is None:
        return None

    sql, param_names = _compile_role_where(where)
    params = tuple(telegram_user_id if name == "telegram_user_id" else None for name in param_names)
    return sql, params


async def _admin_gate_ok(bot_id: int, resource: dict, telegram_user_id: int) -> bool:
    """Closes docs/MINIAPP_AUTH_GATE_GAP.md: _authenticate() only proves a
    genuine Telegram identity, not that this identity is entitled to use
    the mini-app at all — and the bot's Telegram Menu Button opens
    /app/{bot_id} for EVERY user who has ever messaged the bot, not just
    admins (see that doc for the full incident). A resource with NO
    role_filter has no per-row ownership concept to fall back on, so
    without this gate any such user could list every other user's rows.

    A resource that DOES declare role_filter (either shape — role-resolved
    or ownership-only, docs/MINIAPP_ROLE_SCOPING_DESIGN.md) already scopes
    rows to what this viewer is entitled to see, so it is exempt from this
    gate — that mechanism IS the authorization for that resource (e.g.
    team_manager's worker role, or a customer's own bookings via an
    ownership-only filter). Re-checking admin membership on top would
    incorrectly lock out those legitimate non-admin viewers.

    Admin membership is checked against db.database.get_bot_admins() — the
    same factory-level admin list each template's own admins.json is kept
    in sync with (sync_bot_admins_json), already used identically by
    analytics_handler/export_excel_handler/office_tasks_handler below."""
    if resource.get("role_filter") is not None:
        return True
    admin_ids = await get_bot_admins(bot_id)
    return str(telegram_user_id) in admin_ids


async def list_resource_handler(request: web.Request) -> web.Response:
    resolved = await _resolve_entry_and_config(request)
    if isinstance(resolved, web.Response):
        return resolved
    bot_id, entry, miniapp_config = resolved

    telegram_user_id = await _authenticate(request, bot_id, entry.bot.token)
    if telegram_user_id is None:
        return web.json_response({"error": "forbidden"}, status=403)

    resource_name = request.match_info.get("resource", "")
    resource = _resource_spec(miniapp_config, resource_name)
    if resource is None:
        return web.json_response({"error": "unknown resource"}, status=404)

    if not await _admin_gate_ok(bot_id, resource, telegram_user_id):
        return web.json_response({"error": "forbidden"}, status=403)

    db_path = entry.config.get("db_path") if isinstance(entry.config, dict) else None
    if not db_path:
        return web.json_response({"error": "bot has no database configured"}, status=500)

    fields = resource["fields"]  # miniapp_config authoring contract — see module docstring
    columns = ", ".join(f["name"] for f in fields)
    order_by = resource.get("order_by", "id DESC")
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        try:
            role_clause = await _apply_role_filter(db, resource, telegram_user_id)
        except RoleFilterDenied:
            return web.json_response({"resource": resource_name, "items": []})

        sql = f"SELECT id, {columns} FROM {resource['table']}"
        params: tuple = ()
        if role_clause is not None:
            where_sql, where_params = role_clause
            sql += f" WHERE {where_sql}"
            params = where_params
        sql += f" ORDER BY {order_by} LIMIT 200"
        async with db.execute(sql, params) as cursor:
            rows = [dict(r) for r in await cursor.fetchall()]
    return web.json_response({"resource": resource_name, "items": rows})


async def get_resource_handler(request: web.Request) -> web.Response:
    resolved = await _resolve_entry_and_config(request)
    if isinstance(resolved, web.Response):
        return resolved
    bot_id, entry, miniapp_config = resolved

    telegram_user_id = await _authenticate(request, bot_id, entry.bot.token)
    if telegram_user_id is None:
        return web.json_response({"error": "forbidden"}, status=403)

    resource_name = request.match_info.get("resource", "")
    resource = _resource_spec(miniapp_config, resource_name)
    if resource is None:
        return web.json_response({"error": "unknown resource"}, status=404)

    if not await _admin_gate_ok(bot_id, resource, telegram_user_id):
        return web.json_response({"error": "forbidden"}, status=403)

    item_id_raw = request.match_info.get("item_id", "")
    if not item_id_raw.isdigit():
        return web.json_response({"error": "bad item id"}, status=404)

    db_path = entry.config.get("db_path") if isinstance(entry.config, dict) else None
    if not db_path:
        return web.json_response({"error": "bot has no database configured"}, status=500)

    fields = resource["fields"]
    columns = ", ".join(f["name"] for f in fields)
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        try:
            role_clause = await _apply_role_filter(db, resource, telegram_user_id)
        except RoleFilterDenied:
            return web.json_response({"error": "forbidden"}, status=403)

        sql = f"SELECT id, {columns} FROM {resource['table']} WHERE id = ?"
        params: tuple = (int(item_id_raw),)
        if role_clause is not None:
            where_sql, where_params = role_clause
            sql += f" AND ({where_sql})"
            params = params + where_params
        async with db.execute(sql, params) as cursor:
            row = await cursor.fetchone()
    if row is None:
        return web.json_response({"error": "not found"}, status=404)

    related = await _related_for_item(
        bot_id, miniapp_config, resource, int(item_id_raw), telegram_user_id, db_path
    )
    return web.json_response({"resource": resource_name, "item": dict(row), "related": related})


async def _related_for_item(
    bot_id: int,
    miniapp_config: dict,
    resource: dict,
    item_id: int,
    telegram_user_id: int,
    db_path: str,
) -> list[dict]:
    """Loads the sub-records a resource declares via `children`, so the detail
    screen can show a record's program/guests/attachments/payments inline
    instead of scattering them across sibling tabs the user has to find and
    cross-reference by ID.

    Each child goes through the SAME admin gate and role filter as a direct
    GET /{child} would — this endpoint is a convenience join, never a way to
    read rows the child resource itself would refuse. A child that denies or
    errors is omitted from the response rather than failing the whole detail
    view.
    """
    out: list[dict] = []
    for child in resource.get("children", []):
        child_spec = _resource_spec(miniapp_config, child.get("resource", ""))
        if child_spec is None:
            continue
        via = child.get("via", "")
        # `via` is interpolated as a column name, so it must be a column the
        # child resource already declares — not just any identifier-shaped
        # string that happens to be in the config.
        if via not in {f["name"] for f in child_spec.get("fields", [])}:
            logger.warning(
                "_related_for_item: bot_id=%s child=%s — 'via' column %r is not a declared field, skipped",
                bot_id,
                child.get("resource"),
                via,
            )
            continue
        if not await _admin_gate_ok(bot_id, child_spec, telegram_user_id):
            continue

        columns = ", ".join(f["name"] for f in child_spec["fields"])
        try:
            async with aiosqlite.connect(db_path) as db:
                db.row_factory = aiosqlite.Row
                try:
                    role_clause = await _apply_role_filter(db, child_spec, telegram_user_id)
                except RoleFilterDenied:
                    continue
                sql = f"SELECT id, {columns} FROM {child_spec['table']} WHERE {via} = ?"
                params: tuple = (item_id,)
                if role_clause is not None:
                    where_sql, where_params = role_clause
                    sql += f" AND ({where_sql})"
                    params = params + where_params
                sql += f" ORDER BY {child_spec.get('order_by', 'id DESC')} LIMIT 200"
                async with db.execute(sql, params) as cursor:
                    rows = [dict(r) for r in await cursor.fetchall()]
        except Exception:
            logger.exception(
                "_related_for_item: bot_id=%s child=%s — query failed, section omitted",
                bot_id,
                child.get("resource"),
            )
            continue

        out.append(
            {
                "resource": child_spec["name"],
                "title": child.get("title") or child_spec.get("title") or child_spec["name"],
                "as": child.get("as", "table"),
                "items": rows,
            }
        )
    return out


# Same shape as runtime/registry.py's _BOT_COMMAND_NAME_RE-style validation —
# column names come out of miniapp_config (trusted, authored per-template),
# but incoming CREATE payload KEYS come from the HTTP request; only accepting
# keys that exactly match a field the resource spec already declares (not
# arbitrary request JSON) is what keeps this safe from writing to
# unintended columns, not string-escaping the SQL text itself (values are
# always bound as aiosqlite parameters, never interpolated).
async def create_resource_handler(request: web.Request) -> web.Response:
    """POST /api/{bot_id}/{resource} — see allowed_fields below: it checks
    each field dict's "create" key (the one _SCHEMA_FIELD_KEYS/every
    template's miniapp_config actually sets, e.g. tour_operator's "status"
    field is "create": False), not "creatable" (that key only exists at the
    RESOURCE level, checked separately just below). A prior version of this
    line checked "creatable" here too, which never matched anything on a
    field dict and so silently allowed writes to every "create": False
    field regardless of its declared intent."""
    resolved = await _resolve_entry_and_config(request)
    if isinstance(resolved, web.Response):
        return resolved
    bot_id, entry, miniapp_config = resolved

    telegram_user_id = await _authenticate(request, bot_id, entry.bot.token)
    if telegram_user_id is None:
        return web.json_response({"error": "forbidden"}, status=403)

    resource_name = request.match_info.get("resource", "")
    resource = _resource_spec(miniapp_config, resource_name)
    if resource is None:
        return web.json_response({"error": "unknown resource"}, status=404)
    if not resource.get("creatable", False):
        return web.json_response({"error": "resource is read-only"}, status=403)

    # "create_requires": "bot_admin" resolves the bootstrap deadlock a
    # membership-based role_filter otherwise creates: a role_filter that scopes
    # rows to "projects you are a member of" denies the very first create,
    # because the creator holds no membership until the project exists. For
    # such resources the write is authorized by factory-level bot admin
    # instead — the boss who owns the bot creates projects, workers join one
    # by code. Reads stay role-filtered exactly as before.
    admin_authorized = resource.get("create_requires") == "bot_admin"
    if admin_authorized:
        if str(telegram_user_id) not in await get_bot_admins(bot_id):
            return web.json_response({"error": "forbidden"}, status=403)
    elif not await _admin_gate_ok(bot_id, resource, telegram_user_id):
        return web.json_response({"error": "forbidden"}, status=403)

    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    if not isinstance(payload, dict):
        return web.json_response({"error": "invalid JSON body"}, status=400)

    allowed_fields = {f["name"] for f in resource["fields"] if f.get("create", True)}
    required_fields = {f["name"] for f in resource["fields"] if f.get("required", False)}
    values = {k: v for k, v in payload.items() if k in allowed_fields}
    missing = required_fields - values.keys()
    if missing:
        return web.json_response({"error": f"missing required fields: {sorted(missing)}"}, status=400)
    if not values:
        return web.json_response({"error": "no valid fields in payload"}, status=400)

    # A "contact" field identifies the person a record is about, however the
    # admin actually knows them: "@ivanov", "Иван Петров", "+7 999 123-45-67".
    # Only @-prefixed input is held to Telegram's username rules — a phone or
    # a name is a perfectly good contact and must not be rejected for failing
    # rules that were never meant to apply to it. A contact that IS a username
    # is normalized (bare + lowercase) so an incoming update can be matched
    # against it later. See services/client_link.py for the whole rule.
    for field in resource["fields"]:
        if field.get("kind") != "contact" or field["name"] not in values:
            continue
        label = field.get("label", field["name"])
        try:
            contact = parse_contact(values[field["name"]])
        except ContactFormatError:
            return web.json_response(
                {"error": f"{label}: «@…» должен быть корректным Telegram-username"},
                status=400,
            )
        if contact is None:
            # Reaches here only for a blank string, which the required-field
            # check above can't catch (the key IS present, it's just empty).
            if field.get("required", False):
                return web.json_response({"error": f"{label}: обязательное поле"}, status=400)
            del values[field["name"]]
            continue
        values[field["name"]] = contact.value

    db_path = entry.config.get("db_path") if isinstance(entry.config, dict) else None
    if not db_path:
        return web.json_response({"error": "bot has no database configured"}, status=500)

    async with aiosqlite.connect(db_path) as db:
        try:
            # Skipped for the bot-admin path: that path exists precisely for
            # resources whose role_filter denies a viewer holding no
            # membership yet, so asking the role filter to name an owner
            # column here would raise RoleFilterDenied and 403 the very
            # bootstrap create it was meant to permit. Identity for those
            # resources comes from on_create.set instead, which is just as
            # server-controlled.
            owner_column = (
                None if admin_authorized
                else await _resolve_create_owner_column(db, resource, telegram_user_id)
            )
        except RoleFilterDenied:
            return web.json_response({"error": "forbidden"}, status=403)
        if owner_column is not None:
            # Checked against the RAW payload, not the filtered `values`: a
            # template that correctly marks its owner column create:False
            # (identity belongs to the session, not to a form field) would
            # otherwise have a spoofed value silently dropped by
            # allowed_fields — turning an explicit rejection into a quiet
            # correction and losing the signal that someone tried it.
            if owner_column in payload and payload[owner_column] != telegram_user_id:
                return web.json_response(
                    {"error": f"{owner_column} must match the authenticated user"}, status=400
                )
            values[owner_column] = telegram_user_id

        on_create = resource.get("on_create") or {}
        try:
            values.update(_on_create_values(on_create.get("set", {}), telegram_user_id))
        except OnCreateError:
            logger.exception("create: bot_id=%s resource=%s — bad on_create.set", bot_id, resource_name)
            return web.json_response({"error": "server misconfigured"}, status=500)

        columns = ", ".join(values.keys())
        placeholders = ", ".join("?" for _ in values)
        cursor = await db.execute(
            f"INSERT INTO {resource['table']} ({columns}) VALUES ({placeholders})",
            tuple(values.values()),
        )
        new_id = cursor.lastrowid

        try:
            await _apply_on_create_link(db, on_create.get("link"), new_id, telegram_user_id)
        except OnCreateError:
            # Committing the parent row without its link row would leave a
            # record its own creator cannot see (the read-side role filter
            # scopes by that link) — roll back instead of writing an orphan.
            await db.rollback()
            logger.exception("create: bot_id=%s resource=%s — on_create.link failed", bot_id, resource_name)
            return web.json_response({"error": "server misconfigured"}, status=500)

        await db.commit()
    return web.json_response({"resource": resource_name, "id": new_id}, status=201)


class OnCreateError(ValueError):
    """An `on_create` block references an unsupported token or a
    non-identifier table/column name — surfaced as a 500 rather than executing
    a shape nobody reviewed."""


_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Server-derived values an `on_create` block may request. Deliberately tiny:
# these are the only things the engine knows that the client must NOT be
# trusted to supply. Anything else in a `set` map is treated as a literal.
_ON_CREATE_TOKENS = frozenset({"telegram_user_id", "new_id", "random_code"})


def _on_create_token_value(token: Any, telegram_user_id: int, new_id: int | None) -> Any:
    if not isinstance(token, str) or token not in _ON_CREATE_TOKENS:
        return token  # literal, e.g. the string "owner"
    if token == "telegram_user_id":
        return telegram_user_id
    if token == "new_id":
        if new_id is None:
            raise OnCreateError("'new_id' is only meaningful in on_create.link")
        return new_id
    # A short, unambiguous invite/join code — templates that need one declare
    # it here rather than making a human type a unique string into a form.
    return secrets.token_hex(3).upper()


def _on_create_values(spec: Any, telegram_user_id: int) -> dict:
    """Columns the SERVER fills in on insert, overriding anything the client
    sent for them — identity columns, generated codes, fixed literals."""
    if not isinstance(spec, dict):
        raise OnCreateError(f"on_create.set must be a mapping, got {type(spec).__name__}")
    out = {}
    for column, token in spec.items():
        if not _IDENTIFIER_RE.match(str(column)):
            raise OnCreateError(f"on_create.set column is not an identifier: {column!r}")
        out[column] = _on_create_token_value(token, telegram_user_id, None)
    return out


async def _apply_on_create_link(
    db: aiosqlite.Connection,
    spec: Any,
    new_id: int,
    telegram_user_id: int,
) -> None:
    """Inserts the membership/link row that makes a just-created record
    visible to its creator. Runs inside the caller's open transaction, so the
    parent row and its link commit together or not at all."""
    if spec is None:
        return
    if not isinstance(spec, dict) or not isinstance(spec.get("columns"), dict):
        raise OnCreateError("on_create.link needs a 'table' and a 'columns' mapping")
    table = spec.get("table")
    if not _IDENTIFIER_RE.match(str(table)):
        raise OnCreateError(f"on_create.link table is not an identifier: {table!r}")

    values = {}
    for column, token in spec["columns"].items():
        if not _IDENTIFIER_RE.match(str(column)):
            raise OnCreateError(f"on_create.link column is not an identifier: {column!r}")
        values[column] = _on_create_token_value(token, telegram_user_id, new_id)

    columns = ", ".join(values.keys())
    placeholders = ", ".join("?" for _ in values)
    await db.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
        tuple(values.values()),
    )


async def _resolve_create_owner_column(db: aiosqlite.Connection, resource: dict, telegram_user_id: int) -> str | None:
    """Minimal create-time enforcement (see design doc): only handles the
    single-column-equality shape `"<column> = :telegram_user_id"` that the
    read-side where-templates already use for row ownership. Returns the
    owner column name to force to telegram_user_id, or None when
    role_filter is absent, the matched rule is unfiltered (where=None), or
    the where-template isn't this exact shape — deliberately not attempting
    to enforce anything beyond what this pilot's rules actually declare."""
    role_filter = resource.get("role_filter")
    if role_filter is None:
        return None

    where = await _resolve_role_filter_where(db, role_filter, telegram_user_id)
    if where is None:
        return None

    match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*:telegram_user_id", where.strip())
    return match.group(1) if match else None


_ANALYTICS_PERIODS: frozenset[str] = frozenset(("week", "month", "quarter"))


async def analytics_handler(request: web.Request) -> web.Response:
    """GET /api/{bot_id}/analytics?period=week|month|quarter — per-bot sales
    analytics (docs/SALES_ANALYTICS_DESIGN.md), OWNER-ONLY unlike every other
    /api/{bot_id}/* route above (those authenticate any of the bot's own
    Telegram users, since a customer viewing their own booking is a normal
    use case; analytics about ALL customers is not something a random
    customer of this bot should ever see). Ownership is checked against
    db.database.bot_admins — the same factory-level admin list templates'
    own _is_admin(uid, config.admins_file) checks read from, just consulted
    here instead of the per-bot admins.json file since this handler has no
    Config object, only the Registry entry.

    Deliberately does NOT go through _resolve_entry_and_config() (that
    function 404s when miniapp_config is absent, which is orthogonal to
    whether analytics is available — a bot can have no miniapp_config yet
    still have a usable office_hook_config, or vice versa). Only bot
    existence is required here; office_hook_config availability is instead
    what compute_metrics()'s own None return communicates."""
    bot_id_raw = request.match_info.get("bot_id", "")
    if err := _bad_bot_id(bot_id_raw):
        return err
    bot_id = int(bot_id_raw)

    registry: Registry = request.app[REGISTRY_KEY]
    entry = registry.get(bot_id)
    if entry is None:
        return web.json_response({"error": "unknown bot"}, status=404)

    telegram_user_id = await _authenticate(request, bot_id, entry.bot.token)
    if telegram_user_id is None:
        return web.json_response({"error": "forbidden"}, status=403)

    admin_ids = await get_bot_admins(bot_id)
    if str(telegram_user_id) not in admin_ids:
        return web.json_response({"error": "forbidden"}, status=403)

    period_raw = request.query.get("period", "week")
    if period_raw not in _ANALYTICS_PERIODS:
        return web.json_response({"error": "invalid period"}, status=400)
    period: Period = period_raw  # type: ignore[assignment]  # narrowed by the membership check above

    db_path = entry.config.get("db_path") if isinstance(entry.config, dict) else None
    if not db_path:
        return web.json_response({"error": "bot has no database configured"}, status=500)

    hook_config = await get_bot_office_hook_config(bot_id)
    metrics = await compute_metrics(db_path, hook_config, period)
    if metrics is None:
        return web.json_response({"error": "analytics not available for this bot"}, status=404)
    return web.json_response(metrics)


async def export_excel_handler(request: web.Request) -> web.Response:
    """GET /api/{bot_id}/export.xlsx — OWNER-ONLY download of this bot's own
    business-data table (whatever office_hook_config points at) as a styled
    .xlsx workbook, via features/excel_export.py. Same auth/ownership
    posture as analytics_handler right below (owner-only, unlike the
    any-authenticated-user routes above it — a bulk export of every
    customer's records is not something a random customer of this bot should
    ever be able to trigger, let alone download).

    Reuses fetch_export_rows()'s own "degrade to unavailable, never partially
    wrong" contract: a bot with no usable office_hook_config, or a stale one
    whose table no longer exists, 404s the same way analytics_handler does
    for compute_metrics() returning None — never a 500 or a malformed file."""
    bot_id_raw = request.match_info.get("bot_id", "")
    if err := _bad_bot_id(bot_id_raw):
        return err
    bot_id = int(bot_id_raw)

    registry: Registry = request.app[REGISTRY_KEY]
    entry = registry.get(bot_id)
    if entry is None:
        return web.json_response({"error": "unknown bot"}, status=404)

    telegram_user_id = await _authenticate(request, bot_id, entry.bot.token)
    if telegram_user_id is None:
        return web.json_response({"error": "forbidden"}, status=403)

    admin_ids = await get_bot_admins(bot_id)
    if str(telegram_user_id) not in admin_ids:
        return web.json_response({"error": "forbidden"}, status=403)

    db_path = entry.config.get("db_path") if isinstance(entry.config, dict) else None
    if not db_path:
        return web.json_response({"error": "bot has no database configured"}, status=500)

    hook_config = await get_bot_office_hook_config(bot_id)
    fetched = await fetch_export_rows(db_path, hook_config)
    if fetched is None:
        return web.json_response({"error": "export not available for this bot"}, status=404)
    columns, rows = fetched

    data = build_workbook(columns, rows, sheet_title=str(entry.template_id or "Export"))
    if len(data) > MAX_FILE_SIZE:
        # build_workbook() already logged this — surfaced to the caller as a
        # proper error response instead of trying to serve an oversized file.
        return web.json_response({"error": "export too large to deliver"}, status=413)

    return web.Response(
        body=data,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="export_{bot_id}.xlsx"'},
    )


async def office_tasks_handler(request: web.Request) -> web.Response:
    """GET /api/{bot_id}/office/tasks — owner-only, all-employees task
    overview for boss_bot's desktop dashboard (see office_dashboard_handler
    below). Reads directly from THIS bot's own `tasks` table rather than
    going through the generic miniapp_config list_resource_handler path:
    that path is per-resource CRUD for a customer viewing their OWN data,
    while this is a single fixed, owner-only aggregate view — same
    reasoning analytics_handler above gives for bypassing
    _resolve_entry_and_config(). Restricted to bot_id's whose template_id is
    "boss_bot" (the only template with a `tasks` table in this shape) so a
    mistargeted bot_id 404s instead of either querying a table that doesn't
    exist or, worse, one that coincidentally does but holds unrelated
    data."""
    bot_id_raw = request.match_info.get("bot_id", "")
    if err := _bad_bot_id(bot_id_raw):
        return err
    bot_id = int(bot_id_raw)

    registry: Registry = request.app[REGISTRY_KEY]
    entry = registry.get(bot_id)
    if entry is None or entry.template_id != "boss_bot":
        return web.json_response({"error": "office dashboard not available for this bot"}, status=404)

    telegram_user_id = await _authenticate(request, bot_id, entry.bot.token)
    if telegram_user_id is None:
        return web.json_response({"error": "forbidden"}, status=403)

    admin_ids = await get_bot_admins(bot_id)
    if str(telegram_user_id) not in admin_ids:
        return web.json_response({"error": "forbidden"}, status=403)

    db_path = entry.config.get("db_path") if isinstance(entry.config, dict) else None
    if not db_path:
        return web.json_response({"error": "bot has no database configured"}, status=500)

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, title, description, deadline, assignee_hint, status, created_at "
            "FROM tasks ORDER BY (status != 'done'), deadline IS NULL, deadline ASC LIMIT 500"
        ) as cursor:
            rows = [dict(r) for r in await cursor.fetchall()]

    now_iso = time.strftime("%Y-%m-%dT%H:%M:%S")
    for row in rows:
        row["overdue"] = bool(row["deadline"] and row["status"] != "done" and row["deadline"] < now_iso)

    return web.json_response({"tasks": rows})


# Self-contained (no build step) desktop dashboard — a boss opening this from
# a laptop browser gets a real page (sortable-by-eye table, no Telegram
# WebApp SDK dependency, no npm build required to ship it) rather than the
# mobile-first miniapp/ SPA stretched wide. Auth is entirely client-side
# against /api/{bot_id}/office/tasks (same magic-link `?token=` the page was
# opened with) — the shell itself is served unconditionally, same posture as
# serve_app_shell's SPA shell, so a bad/expired token surfaces as a normal
# "🔒 forbidden" message in-page instead of a bare 403 with no page at all.
_OFFICE_DASHBOARD_HTML = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Веб-кабинет</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; margin: 0; padding: 24px;
         background: #f7f7f8; color: #1a1a1a; }
  @media (prefers-color-scheme: dark) { body { background: #17181c; color: #e8e8ea; } }
  h1 { font-size: 20px; margin: 0 0 16px; }
  table { width: 100%; border-collapse: collapse; background: #fff; border-radius: 8px; overflow: hidden;
          box-shadow: 0 1px 3px rgba(0,0,0,.08); }
  @media (prefers-color-scheme: dark) { table { background: #23242a; } }
  th, td { text-align: left; padding: 10px 12px; border-bottom: 1px solid rgba(0,0,0,.08); font-size: 14px; }
  @media (prefers-color-scheme: dark) { th, td { border-bottom-color: rgba(255,255,255,.08); } }
  th { font-weight: 600; color: #666; }
  tr.overdue { background: rgba(220,50,50,.08); }
  .status { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 12px; }
  .status.done { background: #d7f5df; color: #1b7a37; }
  .status.open { background: #fdeacb; color: #915c00; }
  .status.overdue { background: #fbdada; color: #a4241f; }
  #msg { padding: 40px; text-align: center; color: #888; }
  .empty-cell { color: #999; }
</style>
</head>
<body>
<h1>🖥 Веб-кабинет — задачи офиса</h1>
<div id="msg">Загрузка…</div>
<table id="tbl" style="display:none">
  <thead><tr><th>Задача</th><th>Исполнитель</th><th>Срок</th><th>Статус</th></tr></thead>
  <tbody id="tbody"></tbody>
</table>
<script>
(async () => {
  const params = new URLSearchParams(location.search);
  const token = params.get("token") || "";
  const botId = location.pathname.split("/").pop();
  const msg = document.getElementById("msg");
  const tbl = document.getElementById("tbl");
  const tbody = document.getElementById("tbody");
  try {
    const resp = await fetch(`/api/${botId}/office/tasks?token=${encodeURIComponent(token)}`);
    if (resp.status === 403) { msg.textContent = "🔒 Нет доступа — ссылка устарела, откройте новую из бота."; return; }
    if (!resp.ok) { msg.textContent = "Не удалось загрузить задачи."; return; }
    const data = await resp.json();
    const tasks = data.tasks || [];
    if (!tasks.length) { msg.textContent = "Пока нет задач."; return; }
    for (const t of tasks) {
      const tr = document.createElement("tr");
      if (t.overdue) tr.className = "overdue";
      const statusClass = t.overdue ? "overdue" : (t.status === "done" ? "done" : "open");
      const statusLabel = t.overdue ? "просрочена" : (t.status === "done" ? "выполнена" : "в работе");
      tr.innerHTML = `
        <td>${escapeHtml(t.title || "")}</td>
        <td>${t.assignee_hint ? escapeHtml(t.assignee_hint) : '<span class="empty-cell">—</span>'}</td>
        <td>${t.deadline ? escapeHtml(t.deadline) : '<span class="empty-cell">—</span>'}</td>
        <td><span class="status ${statusClass}">${statusLabel}</span></td>
      `;
      tbody.appendChild(tr);
    }
    msg.style.display = "none";
    tbl.style.display = "table";
  } catch (e) {
    msg.textContent = "Ошибка загрузки.";
  }
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  }
})();
</script>
</body>
</html>
"""


async def office_dashboard_handler(request: web.Request) -> web.Response:
    bot_id_raw = request.match_info.get("bot_id", "")
    if err := _bad_bot_id(bot_id_raw):
        return err
    return web.Response(text=_OFFICE_DASHBOARD_HTML, content_type="text/html")


async def serve_app_shell(request: web.Request) -> web.Response:
    """Serves the SPA's index.html for /app/{bot_id} (and any client-side
    sub-route under it — the SPA does its own in-memory routing per
    App.tsx, so any path here just needs the same shell). bot_id is
    validated (existence + miniapp_config presence) the same way the /api/*
    handlers are, so an unknown/unconfigured bot 404s before ever serving
    the SPA shell, rather than serving a page that would just fail its own
    API calls once loaded.

    bot_id=FACTORY_BOT_ID is a special case: it has no bots-table row and no
    miniapp_config (template_id="__factory__", see runtime/registry.py), so
    _resolve_entry_and_config() would always 404 it. It gets the owner-only
    analytics dashboard instead (App.tsx's isFactoryBotPath() branches to
    FactoryDashboardScreen client-side, authed separately by
    runtime/factory_analytics_api.py's own /api/factory/* routes) — the SPA
    shell itself is served unconditionally for this one bot_id."""
    bot_id_raw = request.match_info.get("bot_id", "")
    if bot_id_raw == str(FACTORY_BOT_ID):
        index_path = _MINIAPP_DIST_DIR / "index.html"
        if not index_path.exists():
            return web.json_response(
                {"error": "mini-app build not found — run `npm run build` in miniapp/"}, status=503
            )
        return web.FileResponse(index_path)

    resolved = await _resolve_entry_and_config(request)
    if isinstance(resolved, web.Response):
        return resolved

    index_path = _MINIAPP_DIST_DIR / "index.html"
    if not index_path.exists():
        return web.json_response(
            {"error": "mini-app build not found — run `npm run build` in miniapp/"}, status=503
        )
    return web.FileResponse(index_path)


async def serve_owner_report_shell(request: web.Request) -> web.Response:
    """Serves the SPA's index.html for /owner-report — the Stage 2 system-
    owner-only cross-owner report (runtime/owner_report_api.py), a THIRD app
    distinct from both the per-bot customer app (/app/{bot_id}) and the
    owner's own "Моя фабрика" dashboard (/app/0). Same "serve the shell
    unconditionally, let the SPA's own API calls enforce auth" posture as
    serve_app_shell's FACTORY_BOT_ID special case above — App.tsx's routing
    branches on window.location.pathname client-side, and every
    /api/owner-report/* route is independently OWNER_ID-gated server-side
    regardless of whether this shell was ever reached."""
    index_path = _MINIAPP_DIST_DIR / "index.html"
    if not index_path.exists():
        return web.json_response(
            {"error": "mini-app build not found — run `npm run build` in miniapp/"}, status=503
        )
    return web.FileResponse(index_path)


def _register_static_routes(app: web.Application) -> None:
    """Registers the built SPA's JS/CSS/favicon files under the FIXED prefix
    /app-assets/, kept OUT of the /app/{bot_id}/* namespace on purpose:
    /app/{bot_id} is matched per-request against the live Registry (see
    serve_app_shell), so mixing a static-file prefix into that same path
    space would require careful route-ordering and still risks a bot_id
    that happens to collide with an asset filename. A dedicated prefix
    sidesteps that. miniapp/vite.config.ts's base:'/app-assets/' is what
    makes the built index.html's own <link>/<script> tags point here,
    identically regardless of which bot_id the shell page was loaded under.

    Serves the WHOLE dist/ directory (not just dist/assets/) under this one
    prefix — Vite's build also emits publicDir copies (favicon.svg) at
    dist/'s top level, not under dist/assets/, and both need to resolve
    under the same /app-assets/ prefix the base config already points at."""
    if _MINIAPP_DIST_DIR.exists():
        app.router.add_static("/app-assets/", _MINIAPP_DIST_DIR, name="miniapp-assets")


# Imported lazily inside register_routes (not at module top) to avoid a
# circular import: webhook_app.py doesn't import this module, so there is no
# cycle today, but keeping REGISTRY_KEY sourced from webhook_app's own
# AppKey (rather than redefining a second, string-keyed key) guarantees this
# module and webhook_app.py are reading the SAME app[...] slot Registry was
# stored under in combined_app.py's _bootstrap_app().
from runtime.webhook_app import REGISTRY_KEY  # noqa: E402


def register_routes(app: web.Application) -> None:
    """Adds mini-app routes to an already-built aiohttp Application — called
    by combined_app.py right after webhook_app.create_app() so both live on
    the same Application/process/port (see module docstring)."""
    app.router.add_get("/api/{bot_id}/me", me_handler)
    # Registered before the generic {resource} route for the same reason as
    # /schema and /analytics below — "session" is reserved the same way.
    app.router.add_get("/api/{bot_id}/session", bot_session_handler)
    # Registered before the generic {resource} route below — aiohttp matches
    # routes in registration order, and "schema" would otherwise be swallowed
    # as if it were a resource name (see docs on _resource_spec: a bot could
    # in principle even declare a resource literally named "schema", which
    # this ordering also protects against by reserving the word).
    app.router.add_get("/api/{bot_id}/schema", schema_handler)
    # Registered before the generic {resource} route for the same reason as
    # /schema above — "analytics" would otherwise be swallowed as a resource
    # name, and is reserved the same way "schema" already is.
    app.router.add_get("/api/{bot_id}/analytics", analytics_handler)
    # Reserved before the generic {resource} route for the same reason as
    # /schema and /analytics above.
    app.router.add_get("/api/{bot_id}/features", features_handler)
    # Registered before the generic {resource} route for the same reason as
    # /schema and /analytics above — "export.xlsx" would otherwise be
    # swallowed as a resource name.
    app.router.add_get("/api/{bot_id}/export.xlsx", export_excel_handler)
    # Registered before the generic {resource} route for the same reason as
    # /schema and /analytics above — "office" would otherwise be swallowed
    # as a resource name.
    app.router.add_get("/api/{bot_id}/office/tasks", office_tasks_handler)
    app.router.add_get("/api/{bot_id}/{resource}", list_resource_handler)
    app.router.add_get("/api/{bot_id}/{resource}/{item_id}", get_resource_handler)
    app.router.add_post("/api/{bot_id}/{resource}", create_resource_handler)
    app.router.add_get("/app/{bot_id}", serve_app_shell)
    # /site/{bot_id} — standalone website outside Telegram (Task B,
    # docs/MINIAPP_WEBSITE_REDESIGN_DESIGN.md §2). Same SPA shell as
    # /app/{bot_id} (App.tsx branches client-side on the path prefix to skip
    # the Telegram WebApp SDK bootstrap); same bot_id validation via
    # serve_app_shell, just reached via a different URL prefix so a plain
    # browser tab and a Telegram WebView open visibly different-looking URLs.
    app.router.add_get("/site/{bot_id}", serve_app_shell)
    app.router.add_get("/office/{bot_id}", office_dashboard_handler)
    app.router.add_get("/owner-report", serve_owner_report_shell)
    _register_static_routes(app)
