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
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

import aiosqlite
from aiohttp import web

from db.database import get_bot_admins, get_bot_miniapp_config, get_bot_office_hook_config
from features.sales_analytics import Period, compute_metrics
from runtime.registry import FACTORY_BOT_ID, Registry, _load_template_module_async

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


def _miniapp_secret() -> str:
    return os.getenv("MINIAPP_SECRET", "")


def mint_magic_link_token(bot_id: int, telegram_user_id: int) -> str:
    """Signs `bot_id:telegram_user_id:expiry` with HMAC-SHA256 over
    MINIAPP_SECRET. Called by a template's /app command handler (e.g.
    tour_operator.py's cmd_app) to build the link it sends the user — see
    that template's own TODO to stop embedding the bare user_id.

    Raises RuntimeError if MINIAPP_SECRET is unset, rather than silently
    minting an unsigned/forgeable token — same fail-closed posture as
    webhook_app.py's WEBHOOK_SECRET check, just raised instead of
    request-rejected since this runs on the Telegram-handler side, not a
    request handler."""
    secret = _miniapp_secret()
    if not secret:
        raise RuntimeError("MINIAPP_SECRET is not set — cannot mint a magic-link token")
    expiry = int(time.time()) + MAGIC_LINK_TTL_SECONDS
    payload = f"{bot_id}:{telegram_user_id}:{expiry}"
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{sig}"


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
_SCHEMA_RESOURCE_KEYS = ("name", "creatable", "title", "titleField")
_SCHEMA_FIELD_KEYS = ("name", "required", "creatable", "label", "kind", "list", "detail", "create")


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

    resources = [_resource_display_schema(r) for r in miniapp_config.get("resources", [])]
    return web.json_response({"resources": resources})


def _resource_spec(miniapp_config: dict, resource_name: str) -> dict | None:
    for resource in miniapp_config.get("resources", []):
        if resource.get("name") == resource_name:
            return resource
    return None


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

    db_path = entry.config.get("db_path") if isinstance(entry.config, dict) else None
    if not db_path:
        return web.json_response({"error": "bot has no database configured"}, status=500)

    fields = resource["fields"]  # miniapp_config authoring contract — see module docstring
    columns = ", ".join(f["name"] for f in fields)
    order_by = resource.get("order_by", "id DESC")
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT id, {columns} FROM {resource['table']} ORDER BY {order_by} LIMIT 200"
        ) as cursor:
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
        async with db.execute(
            f"SELECT id, {columns} FROM {resource['table']} WHERE id = ?", (int(item_id_raw),)
        ) as cursor:
            row = await cursor.fetchone()
    if row is None:
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response({"resource": resource_name, "item": dict(row)})


# Same shape as runtime/registry.py's _BOT_COMMAND_NAME_RE-style validation —
# column names come out of miniapp_config (trusted, authored per-template),
# but incoming CREATE payload KEYS come from the HTTP request; only accepting
# keys that exactly match a field the resource spec already declares (not
# arbitrary request JSON) is what keeps this safe from writing to
# unintended columns, not string-escaping the SQL text itself (values are
# always bound as aiosqlite parameters, never interpolated).
async def create_resource_handler(request: web.Request) -> web.Response:
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

    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    if not isinstance(payload, dict):
        return web.json_response({"error": "invalid JSON body"}, status=400)

    allowed_fields = {f["name"] for f in resource["fields"] if f.get("creatable", True)}
    required_fields = {f["name"] for f in resource["fields"] if f.get("required", False)}
    values = {k: v for k, v in payload.items() if k in allowed_fields}
    missing = required_fields - values.keys()
    if missing:
        return web.json_response({"error": f"missing required fields: {sorted(missing)}"}, status=400)
    if not values:
        return web.json_response({"error": "no valid fields in payload"}, status=400)

    db_path = entry.config.get("db_path") if isinstance(entry.config, dict) else None
    if not db_path:
        return web.json_response({"error": "bot has no database configured"}, status=500)

    columns = ", ".join(values.keys())
    placeholders = ", ".join("?" for _ in values)
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            f"INSERT INTO {resource['table']} ({columns}) VALUES ({placeholders})",
            tuple(values.values()),
        )
        await db.commit()
        new_id = cursor.lastrowid
    return web.json_response({"resource": resource_name, "id": new_id}, status=201)


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
    app.router.add_get("/api/{bot_id}/{resource}", list_resource_handler)
    app.router.add_get("/api/{bot_id}/{resource}/{item_id}", get_resource_handler)
    app.router.add_post("/api/{bot_id}/{resource}", create_resource_handler)
    app.router.add_get("/app/{bot_id}", serve_app_shell)
    app.router.add_get("/owner-report", serve_owner_report_shell)
    _register_static_routes(app)
