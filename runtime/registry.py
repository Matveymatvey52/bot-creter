"""Stage 2 Phase 1 — in-memory bot registry for the webhook runtime.

Builds bot_id -> BotEntry (Bot + Dispatcher + template Router + config) from the
existing SQLite bots table (db/database.py — token decryption already happens there).
No Postgres, no process spawning: everything lives in this one process's memory.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher, Router
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, TelegramObject

from config import DATA_DIR
from db.database import get_all_bots, get_bot, get_bot_features

logger = logging.getLogger(__name__)

# Reserved bot_id for the factory bot's own entry in the registry (see
# build_factory_entry() below and docs/STAGE2_DESIGN.md "Фабрика как житель
# реестра"). 0 is safe by construction, not by convention: bots.id is
# `INTEGER PRIMARY KEY AUTOINCREMENT` in db/database.py, and SQLite's
# AUTOINCREMENT always starts at 1 and only increases — a real tenant bot can
# never be assigned id=0, so there is no collision to guard against. The
# factory itself has no row in `bots` at all (deliberately — see
# docs/STAGE2_DESIGN.md for why a real row was rejected).
FACTORY_BOT_ID = 0

_TEMPLATE_MARKER_RE = re.compile(r"^#\s*TEMPLATE:\s*(\S+)", re.MULTILINE)


def infer_template_id(file_path: str | None) -> str | None:
    """Best-effort: reads the '# TEMPLATE: <id>' marker comment that templates/*.py
    files carry as their first line. Custom Claude-generated bots not based on a
    fixed template won't have this marker — returns None for those (expected)."""
    if not file_path:
        return None
    try:
        head = Path(file_path).read_text(encoding="utf-8")[:200]
    except (OSError, UnicodeDecodeError):
        return None
    m = _TEMPLATE_MARKER_RE.search(head)
    return m.group(1) if m else None


_FEATURES_DIR = Path(__file__).parent.parent / "features"
_FEATURE_HEADER_MAX_LINES = 10
_FEATURE_LINE_RE = re.compile(r"^#\s*FEATURE:\s*(\S+)", re.MULTILINE)
_COMPATIBLE_WITH_RE = re.compile(r"^#\s*COMPATIBLE_WITH:\s*(.+)$", re.MULTILINE)

# Telegram's own /setMyCommands shape — see _load_and_include_features()'s
# bot_commands validation.
_BOT_COMMAND_NAME_RE = re.compile(r"^[a-z0-9_]{1,32}$")


def discover_features() -> list[dict[str, Any]]:
    """Scans features/*.py for '# FEATURE: <name>' / '# COMPATIBLE_WITH: <template_id,
    template_id, ...>' header comments and returns [{"name": ..., "compatible_with":
    [...]}, ...] for every file that has both. Deliberately a SEPARATE parser from
    services/claude_service.py's discover_templates() (copied pattern, not shared code
    — see feature-modules-inventory findings): that function lives in LLM-prompt
    territory and its USE FOR: is free text for a model to read, while COMPATIBLE_WITH:
    here is a strict, explicit list of template_id's checked programmatically before a
    feature can be enabled for a bot — no "all" sentinel, by design (adding a 13th
    template must never silently make it compatible with every existing feature).

    A file missing either marker (or unreadable) is skipped with a warning, not fatal.
    Files whose name starts with "_" are skipped silently — same convention as
    discover_templates(), for the same reason (a future features/_common.py helper
    module shouldn't warn on every call)."""
    results: list[dict[str, Any]] = []
    if not _FEATURES_DIR.exists():
        return results
    for path in sorted(_FEATURES_DIR.glob("*.py")):
        if path.stem.startswith("_"):
            continue
        try:
            with path.open(encoding="utf-8") as f:
                head = "".join(next(f, "") for _ in range(_FEATURE_HEADER_MAX_LINES))
        except (OSError, UnicodeDecodeError) as e:
            logger.warning(f"discover_features: could not read {path.name}: {e}")
            continue
        name_match = _FEATURE_LINE_RE.search(head)
        compat_match = _COMPATIBLE_WITH_RE.search(head)
        if not name_match or not compat_match:
            logger.warning(f"discover_features: {path.name} missing # FEATURE:/# COMPATIBLE_WITH: header — skipped")
            continue
        compatible_with = [t.strip() for t in compat_match.group(1).split(",") if t.strip()]
        results.append({"name": name_match.group(1), "compatible_with": compatible_with})
    return results


# Some templates read an env var at MODULE IMPORT TIME to decide their own
# behavior (tour_operator's WEB_CRM_ENABLED constant — see docs/STAGE2_DESIGN.md
# "Фаза 7"), and that decision can't be changed after the module is first
# imported. This registry never starts tour_operator's web CRM server (only
# its Telegram router/handlers are registered here — see "Стоп: веб-часть не
# ложится на паттерн"), so the override below must be applied BEFORE that
# template's first import — see _load_template_module(). Deliberately a small,
# explicit, narrow exception table — NOT a general mechanism (e.g. a new
# template-header marker) — since this is a one-off need of one template, not
# a pattern other templates are expected to share.
_PRE_IMPORT_ENV_OVERRIDES: dict[str, dict[str, str]] = {
    "tour_operator": {"TOUR_OPERATOR_WEB_ENABLED": "false"},
}

_template_module_cache: dict[str, ModuleType] = {}


def _load_template_module(template_id: str) -> ModuleType | None:
    """Loads templates/<template_id>.py by CONVENTION, not a hardcoded
    per-template import — every template module is expected to expose `router`,
    `config_from_bot_row(bot_row, data_dir)`, `ConfigMiddleware`, and
    `init_db(db_path)` under exactly these names (verified uniform across all
    5 reference templates — see docs/STAGE2_DESIGN.md "Динамическая
    регистрация шаблонов"). Adding a new templates/*.py file needs NO change
    here, as long as it follows this convention — this is what makes the
    template library scalable past the current 5 without touching this file.

    Cached per process (imported once) — this is also the ONLY point where
    _PRE_IMPORT_ENV_OVERRIDES is applied, exactly once, right before a
    template's FIRST import.

    Returns None if there's no templates/<template_id>.py at all, OR if it
    exists but fails to import for any reason (syntax error, missing
    third-party dependency, etc. — not just ImportError, since a brand-new
    hand-edited template file is exactly where those show up first). The full
    exception is logged here (with traceback) before returning None, since
    this is the only place that still has it — callers only get told the
    module didn't load, which is enough for their own bot_id-contextualized
    warning but not for diagnosing WHY."""
    if template_id in _template_module_cache:
        return _template_module_cache[template_id]
    _apply_pre_import_overrides(template_id, caller="_load_template_module")
    try:
        module = importlib.import_module(f"templates.{template_id}")
    except Exception:
        logger.exception(f"_load_template_module: failed to import templates.{template_id}")
        return None
    _template_module_cache[template_id] = module
    return module


def _apply_pre_import_overrides(template_id: str, *, caller: str) -> None:
    overrides = _PRE_IMPORT_ENV_OVERRIDES.get(template_id)
    if overrides:
        logger.info(f"{caller}: applying pre-import env override for {template_id!r}: {overrides}")
        os.environ.update(overrides)


async def _load_template_module_async(template_id: str) -> ModuleType | None:
    """Async counterpart of _load_template_module(), used only by build_entry()'s
    runtime path (webhook processing). importlib.import_module() is a blocking
    call — on a template's first import it can hold the shared combined_app.py
    event loop long enough to blow Telegram's 10s answerPreCheckoutQuery window
    for a payment on a COMPLETELY DIFFERENT bot (see payment-subsystem-inventory
    findings). Offloading it to run_in_executor's default thread pool fixes that
    without changing behavior: CPython's import lock is already thread-safe/
    reentrant, so importing from a worker thread is safe.

    os.environ.update() (for _PRE_IMPORT_ENV_OVERRIDES) still runs synchronously
    on the calling coroutine, BEFORE the executor submission — os.environ is
    process-global, not thread-local, so the worker thread sees it regardless,
    but doing it here keeps the ordering explicit and this function's own
    control flow simple.

    Shares _template_module_cache with the sync _load_template_module — the
    cache-hit path (every import after the first) stays fully synchronous, no
    thread hop needed. get_template_router() (no runtime caller, only tests)
    keeps calling the sync version unchanged."""
    if template_id in _template_module_cache:
        return _template_module_cache[template_id]
    _apply_pre_import_overrides(template_id, caller="_load_template_module_async")
    loop = asyncio.get_running_loop()
    try:
        module = await loop.run_in_executor(None, importlib.import_module, f"templates.{template_id}")
    except Exception:
        logger.exception(f"_load_template_module_async: failed to import templates.{template_id}")
        return None
    _template_module_cache[template_id] = module
    return module


_GENERATED_BOT_MODULE_NAME_RE = re.compile(r"[^A-Za-z0-9_]")


async def _load_generated_bot_module_async(bot_id: int, file_path: str) -> ModuleType | None:
    """Imports a from-scratch bot's own file DIRECTLY by path — the counterpart
    to _load_template_module_async for bots with no `# TEMPLATE:` marker (no
    match in the shared templates/ library, GENERATE_SYSTEM_PROMPT branch of
    services/claude_service.py's _generate_bot_code_inner). Those files live in
    data/generated_bots/, not the `templates` package, so
    importlib.import_module(f"templates.{template_id}") can never resolve them
    regardless of what template_id is — see docs/OFFICE_HOOK_FROM_SCRATCH_BOTS.md
    for why this needed a second resolution path, not a tweak to the first.

    services/claude_service.py's append_from_scratch_registry_wiring()
    guarantees config_from_bot_row/ConfigMiddleware/on_office_event are present
    in every from-scratch bot's file by the time it's saved — this function
    only handles the import mechanics, build_entry() below reads those
    attributes exactly like it does for a templates/*.py module.

    Deliberately UNCACHED, unlike _load_template_module_async's
    _template_module_cache: templates/*.py only changes via a git deploy
    (safe to cache for the process lifetime), but a from-scratch bot's file
    CAN change at runtime — handlers/manage_bots.py's cb_recreate overwrites
    the same file_path in place via improve_bot_code(). Caching by bot_id
    would silently keep serving pre-regeneration code/router until the next
    process restart. build_entry() only runs at registration/reload (owner
    action), not per-update, so re-importing every time is cheap enough that
    correctness wins over the micro-optimization.

    Module name includes bot_id so a from-scratch bot can never collide with
    a real templates.* module of the same filename stem."""
    loop = asyncio.get_running_loop()

    def _import() -> ModuleType:
        module_name = f"_generated_bot_{bot_id}_{_GENERATED_BOT_MODULE_NAME_RE.sub('_', Path(file_path).stem)}"
        spec = importlib.util.spec_from_file_location(module_name, file_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"could not build an import spec for {file_path!r}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    try:
        return await loop.run_in_executor(None, _import)
    except Exception:
        logger.exception(
            f"_load_generated_bot_module_async: failed to import bot_id={bot_id} from {file_path!r}"
        )
        return None


_feature_module_cache: dict[str, ModuleType] = {}


async def _load_feature_module_async(feature_name: str) -> ModuleType | None:
    """Async counterpart of _load_template_module_async, for features/<feature_name>.py.
    Same event-loop-safety rationale: importlib.import_module() blocks, and a feature's
    first import must not stall the shared combined_app.py event loop long enough to
    blow another bot's answerPreCheckoutQuery window. Separate cache from
    _template_module_cache — feature_name and template_id are different namespaces, a
    name collision between the two must never resolve to the same cached module."""
    if feature_name in _feature_module_cache:
        return _feature_module_cache[feature_name]
    loop = asyncio.get_running_loop()
    try:
        module = await loop.run_in_executor(None, importlib.import_module, f"features.{feature_name}")
    except Exception:
        logger.exception(f"_load_feature_module_async: failed to import features.{feature_name}")
        return None
    _feature_module_cache[feature_name] = module
    return module


def _clone_router(source: Router) -> Router:
    """Returns a fresh Router carrying the same handler registrations as `source`.

    aiogram forbids attaching the same Router instance to more than one parent
    (Dispatcher) for its whole lifetime — Router.include_router raises RuntimeError
    the second time. Since every bot of a given template needs its own Dispatcher,
    each one needs its own attachable Router; the handler callbacks and filter
    objects themselves are stateless and safe to share by reference.
    """
    clone = Router(name=f"{source.name}-clone")
    for event_name, observer in source.observers.items():
        target = clone.observers[event_name]
        for handler in observer.handlers:
            raw_filters = [f.callback for f in handler.filters]
            target.register(handler.callback, *raw_filters)
    return clone


def get_template_router(template_id: str) -> Router | None:
    """Returns a fresh, attachable Router carrying template_id's handlers.

    The underlying template module (and its original Router) is loaded via
    _load_template_module and cached once per process; each call here returns
    a new clone so it can be included into a new bot's Dispatcher without
    hitting aiogram's one-parent restriction (see _clone_router)."""
    module = _load_template_module(template_id)
    if module is None:
        return None
    router = getattr(module, "router", None)
    if router is None:
        return None
    return _clone_router(router)


def get_template_reminders_config(template_id: str) -> dict[str, Any] | None:
    """Returns template_id's own module-level `reminders_config` dict (see
    features/reminders.py's module docstring for the shape), or None if the
    template doesn't declare one. Same load-once-per-process caching as
    get_template_router() above, via _load_template_module — this is a
    plain dict, not a Router, so no cloning is needed (nothing here is
    mutated per-bot)."""
    module = _load_template_module(template_id)
    if module is None:
        return None
    return getattr(module, "reminders_config", None)


class ConfigMiddleware(BaseMiddleware):
    """Generic fallback: injects the raw bot-metadata dict into data["config"] for
    templates that don't have their own typed config yet (everything except
    "accountant" — see _build_generic_middleware below for accountant's own
    typed AccountantConfig + middleware, defined in templates/accountant.py
    itself per Stage 2 Phase 2 — see docs/STAGE2_DESIGN.md)."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        super().__init__()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        data["config"] = self.config
        return await handler(event, data)


async def _build_generic_middleware(bot_row: dict[str, Any], module: ModuleType) -> tuple[BaseMiddleware, Any]:
    """Generalizes what used to be five near-identical _build_*_middleware
    functions (one per template) into one, using the same by-convention
    attribute names _load_template_module relies on: config_from_bot_row,
    init_db, ConfigMiddleware. Keeps the canonical DATA_DIR resolution
    (config.py) as the single source of truth passed INTO the template rather
    than re-derived inside it — see docs/STAGE2_DESIGN.md "Проверка
    идентичности формул путей" for why that distinction matters.

    init_db(config.db_path) runs here — the first point where this bot's own
    resolved db_path exists — so a bot registered purely through the registry
    (never run as a subprocess) still gets its tables created before it can
    receive updates. See docs/STAGE2_DESIGN.md "init_db при регистрации":
    idempotent (CREATE TABLE IF NOT EXISTS), safe to call on every
    registration/reload. If it raises, build_entry()'s caller
    (add_or_replace/reload_all) already wraps this in try/except — the bot
    simply doesn't get registered.

    Returns (middleware, typed_config) rather than just the middleware —
    build_entry() needs the typed config's own .db_path to hand to any feature
    modules enabled for this bot (see _load_and_include_features), since
    features intentionally reuse the host template's own per-bot db_path
    (feature-modules-inventory decision: additional tables in the existing
    db_path, not a separate db file per feature)."""
    from config import DATA_DIR

    config = module.config_from_bot_row(bot_row, DATA_DIR)
    await module.init_db(config.db_path)
    return module.ConfigMiddleware(config), config


def _attach_bot_id_middleware(router: Router, bot_id: int) -> None:
    """Injects data["bot_id"] = bot_id ahead of every event a feature router's
    own handlers receive — see sellable-items-inventory: unlike
    features/payments.py's create_invoice(), which takes bot_id as an
    explicit caller-supplied parameter, a feature-level HANDLER (e.g.
    sellable_items' own "buy" callback) has no template to thread it through.
    Most host templates' own Config dataclasses don't carry a bot_id field at
    all (only orders_tracker.py added one so far, for features/sheets.py calls
    — see docs/STAGE2_DESIGN.md's per-template Config contracts) so a feature
    can't rely on `config.bot_id` either. This is a generic registry-level
    fix, usable by any future feature, not just this one.

    Attached to the CLONED per-bot router only, via .outer_middleware() on
    every event-type observer it has (Router has no single dp.update-style
    entry point the way Dispatcher does) — never to the shared source Router
    or the host template's own router, so one bot's bot_id can never leak
    into another bot's handlers or into the host template's own data dict."""
    async def _inject(handler, event, data):
        data["bot_id"] = bot_id
        return await handler(event, data)

    for observer in router.observers.values():
        observer.outer_middleware(_inject)


# Telegram's own /setMyCommands description length limit — clean-code review
# found this was a bare magic number inline below.
_BOT_COMMAND_DESCRIPTION_MAX_LEN = 256

# Last command set actually pushed to Telegram per bot_id, for the lifetime
# of this process — devops-logs review found bot.set_my_commands() was
# called unconditionally on EVERY build_entry()/reload_one()/reload_all(),
# for every bot with a command-declaring feature enabled, even when nothing
# changed since the last call. reload_all() runs on every process start
# (Railway redeploy), so this scales linearly with the number of such bots
# on every deploy. Comparing against this cache also fixes an adjacent gap:
# without it, a bot whose LAST command-declaring feature gets disabled would
# keep its stale "/items" entry in Telegram's menu forever, since the old
# code only ever called set_my_commands when collected_commands was
# non-empty and never explicitly cleared it.
_last_sent_commands: dict[int, frozenset[tuple[str, str]]] = {}


async def _register_voice_intake_schema(module: ModuleType, bot_id: int, db_path: str) -> None:
    """Fetches this bot's generated bot_voice_cashflow_config row (docs/
    VOICE_CASHFLOW_FROM_SCRATCH_DESIGN.md) and, if it has a non-null
    voice_intake section, builds and registers a data-driven VoiceSchema for
    it via module.register_data_driven_schema() — the from-scratch
    counterpart to a template hand-calling voice_intake.register_schema(...)
    itself inside its own config_from_bot_row (see
    templates/tour_operator.py). Called from _load_and_include_features only
    when feature_name == "voice_intake", right after that module's own
    init_db(db_path) and before its router is included, so a from-scratch
    bot's voice message handler never runs against a stale/missing schema.

    Re-validates the stored config against db_path's CURRENT real tables
    (features/voice_intake.py's own _validate_data_driven_config, using the
    same _extract_create_table_names ground-truth extractor
    services/claude_service.py's generation step validates against) rather
    than trusting the DB row was still accurate — the underlying bot file
    can be regenerated/edited (handlers/manage_bots.py's cb_recreate,
    custom_features) between when this config was generated and any later
    registry reload, and a stale table/column reference here would otherwise
    surface as a raw sqlite3.OperationalError deep inside a live voice
    handler instead of a clean, logged skip at registration time.

    Best-effort like every other step in this loop's try/except: any failure
    (missing register_data_driven_schema on an older/patched module,
    corrupt config, validation failure) is logged and skipped — the
    voice_intake router still loads and responds, just with no schema
    registered for this bot_id (on_voice's own "no registered VoiceSchema"
    branch then handles it gracefully)."""
    register = getattr(module, "register_data_driven_schema", None)
    if register is None:
        return
    from db.database import get_bot_voice_cashflow_config

    config = await get_bot_voice_cashflow_config(bot_id)
    if not config or not config.get("voice_intake"):
        return
    voice_intake_config = config["voice_intake"]
    validate = getattr(module, "_validate_data_driven_config", None)
    if validate is not None:
        tables = _extract_create_table_names_for_registry(db_path)
        if not tables or not validate(voice_intake_config, tables):
            logger.warning(
                f"_register_voice_intake_schema: bot_id={bot_id} voice_cashflow_config "
                "failed re-validation against current db schema — skipped"
            )
            return
    register(bot_id, voice_intake_config)


def _extract_create_table_names_for_registry(db_path: str) -> dict[str, set[str]]:
    """Reads db_path's REAL current sqlite schema (sqlite_master), not the
    bot's .py source — unlike services/claude_service.py's own
    _extract_create_table_names (which regex-scans generated CODE before any
    table exists), this runs at registry-load time, after init_db has
    already created the tables, so introspecting the live database is both
    simpler and more accurate than re-parsing source text a second time.
    Synchronous (sqlite3, not aiosqlite) since this is only ever called from
    inside an already-running event loop's feature-loading step for one bot
    at a time — a blocking few-row PRAGMA read is not worth the ceremony of
    a second async connection here."""
    import sqlite3

    tables: dict[str, set[str]] = {}
    try:
        conn = sqlite3.connect(db_path)
        try:
            cur = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            for (table_name,) in cur.fetchall():
                col_cur = conn.execute(f"PRAGMA table_info({table_name})")
                tables[table_name] = {row[1] for row in col_cur.fetchall()}
        finally:
            conn.close()
    except sqlite3.Error:
        logger.warning(f"_extract_create_table_names_for_registry: could not read {db_path!r}")
        return {}
    return tables


async def _load_and_include_features(dp: Dispatcher, bot: Bot, bot_id: int, db_path: str) -> None:
    """Loads and wires every feature enabled for this bot (db/database.py's
    bot_features table) into its Dispatcher, the same clone-then-include_router
    pattern as the main template (see _clone_router). Each feature is isolated
    in its own try/except — one broken/misconfigured feature module must not
    take the bot's main template down with it (unlike an unresolved
    template_id, which already aborts the whole build_entry() call via the
    caller's try/except in add_or_replace()/reload_all()).

    A bot with no enabled features (the overwhelming majority, at least until
    this system sees adoption) costs one extra DB query and nothing else —
    get_bot_features() returns an empty list and this loop body never runs.

    A feature module MAY also export `bot_commands: list[tuple[str, str]]`
    (command, description) — see sellable-items-inventory: features can carry
    their own user-facing entry points (unlike payments.py/sheets.py, which
    are pure libraries with no UI of their own) but have no template menu to
    add a button to without editing every compatible templates/*.py file.
    Collected across every enabled feature and pushed once via
    bot.set_my_commands(scope=private chats only) — a single call, not one
    per feature, so two features declaring commands never clobber each
    other's list the way two independent set_my_commands() calls would."""
    feature_names = await get_bot_features(bot_id)
    collected_commands: dict[str, str] = {}
    for feature_name in feature_names:
        try:
            module = await _load_feature_module_async(feature_name)
            if module is None:
                logger.warning(
                    f"_load_and_include_features: bot_id={bot_id} feature={feature_name!r} "
                    "failed to import — skipped, bot continues without it"
                )
                continue
            init_db = getattr(module, "init_db", None)
            if init_db is not None:
                await init_db(db_path)
            if feature_name == "voice_intake":
                await _register_voice_intake_schema(module, bot_id, db_path)
            raw_router = getattr(module, "router", None)
            if raw_router is None:
                logger.warning(
                    f"_load_and_include_features: bot_id={bot_id} feature={feature_name!r} "
                    "module has no 'router' attribute — skipped"
                )
                continue
            cloned_router = _clone_router(raw_router)
            _attach_bot_id_middleware(cloned_router, bot_id)
            dp.include_router(cloned_router)
            try:
                for command, description in getattr(module, "bot_commands", None) or []:
                    # Validated per-entry (Telegram's own /setcommands shape:
                    # lowercase ASCII/digits/underscore, 1-32 chars, non-empty
                    # description) — review found that without this, ONE
                    # feature declaring a single malformed command would make
                    # the eventual bot.set_my_commands() call below reject the
                    # WHOLE merged list, silently dropping every OTHER
                    # feature's valid commands from this bot's "/" menu too.
                    # Skipping just the bad entry keeps that blast radius to
                    # itself.
                    if not _BOT_COMMAND_NAME_RE.match(command) or not description or len(description) > _BOT_COMMAND_DESCRIPTION_MAX_LEN:
                        logger.warning(
                            f"_load_and_include_features: bot_id={bot_id} feature={feature_name!r} "
                            f"declared an invalid bot_command {command!r} — skipped, router still loaded"
                        )
                        continue
                    collected_commands[command] = description
            except (TypeError, ValueError):
                # A malformed bot_commands attribute entirely (not an iterable
                # of 2-tuples at all) must not be blamed on the router load —
                # dp.include_router() above already succeeded, so the
                # feature's own handlers genuinely work; only its command
                # menu entry is missing. Keeping this in its own try/except
                # (rather than letting the outer one below catch it) keeps
                # that outer except's "skipped, bot continues without it" log
                # accurate for what it actually means: the router itself
                # never got attached.
                logger.warning(
                    f"_load_and_include_features: bot_id={bot_id} feature={feature_name!r} "
                    "has a malformed bot_commands attribute — ignored, router still loaded"
                )
        except Exception:
            logger.exception(
                f"_load_and_include_features: bot_id={bot_id} feature={feature_name!r} "
                "raised while loading — skipped, bot continues without it"
            )
    current_commands = frozenset(collected_commands.items())
    previous_commands = _last_sent_commands.get(bot_id)
    if previous_commands is None and not current_commands:
        # Never sent anything for this bot, and there's nothing to send now
        # — skip entirely, preserving the zero-cost path for the
        # overwhelming majority of bots with no command-declaring features.
        pass
    elif previous_commands != current_commands:
        try:
            await bot.set_my_commands(
                [BotCommand(command=c, description=d) for c, d in collected_commands.items()],
                scope=BotCommandScopeAllPrivateChats(),
            )
            _last_sent_commands[bot_id] = current_commands
        except Exception:
            logger.exception(f"_load_and_include_features: bot_id={bot_id} failed to set bot commands")


# Lives under DATA_DIR — the ONLY location in this project that survives a
# Railway redeploy (see db/database.py's DB_PATH and handlers/create_bot.py's
# GENERATED_BOTS_DIR, both DATA_DIR-relative; .railwayignore excludes the repo
# checkout's own working tree from persistence). A path under the repo
# checkout (BASE_DIR) would be wiped by nixpacks' fresh git-checkout on every
# deploy, silently deleting every applied custom_features patch with no
# recovery path (bot_custom_features only logs a text description, never the
# code itself). Loaded via importlib.util.spec_from_file_location rather than
# the `custom_features` namespace package used by templates/ and features/:
# DATA_DIR is not guaranteed to be on sys.path or under this repo's root, so
# plain package-relative import_module("custom_features.bot_<id>") could not
# reliably find it there; loading straight from a known file path sidesteps
# that entirely and matches how the file is already located (module_path.exists()).
_CUSTOM_FEATURES_DIR = DATA_DIR / "custom_features"
_custom_feature_module_cache: dict[int, ModuleType] = {}


def _load_custom_feature_module_sync(bot_id: int, module_path: Path) -> ModuleType:
    module_name = f"custom_features_bot_{bot_id}"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


async def _load_custom_feature_module_async(bot_id: int) -> ModuleType | None:
    """Loads custom_features/bot_<id>.py for this bot, if it exists — the ONLY
    place in this registry where the module backing a cache entry can be
    REWRITTEN ON DISK while this process is still running (templates/ and
    features/ only ever change via a deploy+process-restart; custom_features
    changes live, via handlers/custom_features.py's "✅ Применить" step).

    Checks file existence with a plain stat() BEFORE attempting the import —
    the overwhelming majority of bots have no customization at all, same
    zero-cost-for-the-common-case reasoning _load_and_include_features'
    docstring gives for a bot with no enabled features. Cached per bot_id
    once imported; the cache is intentionally NOT invalidated by mtime
    (polling mtime on every webhook update would cost a stat() per update
    for a case that almost never happens) — see invalidate_custom_feature_cache
    below, which the only writer of these files calls explicitly instead."""
    if bot_id in _custom_feature_module_cache:
        return _custom_feature_module_cache[bot_id]
    module_path = _CUSTOM_FEATURES_DIR / f"bot_{bot_id}.py"
    if not module_path.exists():
        return None
    loop = asyncio.get_running_loop()
    try:
        module = await loop.run_in_executor(None, _load_custom_feature_module_sync, bot_id, module_path)
    except Exception:
        logger.exception(f"_load_custom_feature_module_async: failed to import custom_features/bot_{bot_id}.py")
        return None
    _custom_feature_module_cache[bot_id] = module
    return module


def invalidate_custom_feature_cache(bot_id: int) -> None:
    """Must be called whenever custom_features/bot_<id>.py is (re)written on
    disk, BEFORE the next reload_one(bot_id) — handlers/custom_features.py's
    apply step is the only caller. Clears BOTH this module's own cache dict
    AND sys.modules[f"custom_features_bot_{bot_id}"]: a stale sys.modules entry
    would otherwise never get re-executed from the rewritten file. No other
    module in this registry needs this — templates/ and features/ are never
    rewritten by a live, running process."""
    _custom_feature_module_cache.pop(bot_id, None)
    sys.modules.pop(f"custom_features_bot_{bot_id}", None)


async def _load_and_include_custom_feature(dp: Dispatcher, bot_id: int, db_path: str) -> None:
    """Loads and wires this bot's own custom_features/bot_<id>.py (if any)
    into its Dispatcher — same clone-then-include_router pattern and per-bot
    try/except isolation as _load_and_include_features, so one broken custom
    patch can never take the bot's main template router down with it.

    Deliberately WEBHOOK-ONLY: only called from build_entry() below.
    services/bot_runner.py's subprocess model does not load this — the same
    known gap regular bot_features already has there (feature-modules-
    inventory), not widened further by this addition."""
    try:
        module = await _load_custom_feature_module_async(bot_id)
        if module is None:
            return
        init_db = getattr(module, "init_db", None)
        if init_db is not None:
            await init_db(db_path)
        raw_router = getattr(module, "router", None)
        if raw_router is None:
            logger.warning(
                f"_load_and_include_custom_feature: bot_id={bot_id} "
                "custom_features module has no 'router' attribute — skipped"
            )
            return
        dp.include_router(_clone_router(raw_router))
    except Exception:
        logger.exception(
            f"_load_and_include_custom_feature: bot_id={bot_id} "
            "raised while loading — skipped, bot continues without it"
        )


@dataclass
class BotEntry:
    bot: Bot
    dispatcher: Dispatcher
    template_id: str | None
    config: dict[str, Any] = field(default_factory=dict)


def build_factory_entry(bot: Bot, dispatcher: Dispatcher) -> BotEntry:
    """Wraps the factory bot's own already-configured Bot + Dispatcher
    (routers and middleware attached by the caller — runtime/combined_app.py,
    which owns handlers/create_bot.py's router and main.py's
    ManagedBotMiddleware; this module must not import from either, to keep
    the dependency direction one-way) into a BotEntry.

    Deliberately bypasses build_entry()/_load_template_module() entirely —
    the factory bot is not a tenant, has no row in the `bots` table, and no
    per-bot config/db_path to resolve. `template_id="__factory__"` is purely
    a label for logs/debugging; it is never looked up against any
    templates/*.py module since this function never calls
    get_template_router()/build_entry() at all.

    Callers insert the returned entry directly under FACTORY_BOT_ID (see
    docs/STAGE2_DESIGN.md "Фабрика как житель реестра" for why — this mirrors
    the owner's explicit choice, not add_or_replace()/build_entry(), which
    stay untouched for the tenant path)."""
    return BotEntry(bot=bot, dispatcher=dispatcher, template_id="__factory__", config={"bot_id": FACTORY_BOT_ID})


async def build_entry(
    bot_id: int,
    token: str,
    template_id: str | None,
    config: dict[str, Any] | None = None,
    file_path: str | None = None,
) -> BotEntry:
    """Build one BotEntry: a Bot + a fresh Dispatcher wired to the shared template
    Router (if the template is known) and the config middleware.

    For a known template, _build_generic_middleware also calls that template's
    own init_db(config.db_path) before returning — see docs/STAGE2_DESIGN.md
    "init_db при регистрации". This is what lets a bot registered purely
    through the registry (never run as a standalone subprocess) actually have
    tables to write to.

    If template_id doesn't resolve to any templates/*.py module, this does NOT
    raise or refuse to register the bot (unchanged from before this phase) —
    it logs a WARNING (loud, visible in Deploy Logs) and falls back to the
    generic ConfigMiddleware with no router. Previously this was a silent
    partial registration with no log at all — the bot would sit in the
    registry answering nothing, and nobody would know why.

    `file_path` (optional): for a from-scratch generated bot (no `# TEMPLATE:`
    marker, template_id is None), this is the bot's own .py file under
    data/generated_bots/. See docs/OFFICE_HOOK_FROM_SCRATCH_BOTS.md — such
    files are appended with config_from_bot_row/ConfigMiddleware/
    on_office_event by services.claude_service.append_from_scratch_registry_
    wiring at generation time, so importing them directly here (instead of
    via the templates.<id> namespace) gives them the SAME module-based
    office-hook wiring path template-based bots already get below — no
    separate code path needed. Deliberately still attempted even when
    template_id is set (mutually exclusive in practice: a bot resolves to
    EITHER a template marker OR a from-scratch file, never both) only as a
    fallback if the template path failed to resolve, since a from-scratch bot
    always has a file_path but never a template_id."""
    config = dict(config or {})
    config.setdefault("bot_id", bot_id)

    bot = Bot(token=token)
    dp = Dispatcher(storage=MemoryStorage())

    module = await _load_template_module_async(template_id) if template_id else None
    if module is None and file_path:
        module = await _load_generated_bot_module_async(bot_id, file_path)
    if template_id and module is None:
        logger.warning(
            f"build_entry: bot_id={bot_id} has template_id={template_id!r} that does not "
            "match any templates/*.py module — registering with generic middleware and "
            "NO router; this bot will not respond to any update until its template_id is "
            "fixed or a matching template is added"
        )
    elif not template_id and module is None and file_path:
        # From-scratch bot (no `# TEMPLATE:` marker) whose own file failed to
        # import directly — _load_generated_bot_module_async already logged
        # the full exception; this WARNING is the bot_id-contextualized
        # summary callers get (same split as _load_template_module_async's
        # own exception logging vs this function's warnings).
        logger.warning(
            f"build_entry: bot_id={bot_id} is a from-scratch bot (file_path={file_path!r}) "
            "that failed to import directly — registering with generic middleware and NO "
            "router; this bot will not respond to any update until the file's syntax/imports "
            "are fixed"
        )

    typed_config = None
    if module is not None:
        middleware, typed_config = await _build_generic_middleware(config, module)
    else:
        middleware = ConfigMiddleware(config)
    dp.update.outer_middleware(middleware)

    router = None
    if module is not None:
        raw_router = getattr(module, "router", None)
        if raw_router is None:
            # Module imported fine but doesn't follow the naming convention —
            # same silent-half-registration risk as an unresolved template_id
            # (init_db/config DID run via _build_generic_middleware above, but
            # there's no router at all: no handlers, every update unanswered).
            logger.warning(
                f"build_entry: bot_id={bot_id} template_id={template_id!r} module has no "
                "'router' attribute — registering with working middleware/DB but NO "
                "handlers; this bot will not respond to any update until the template "
                "module exposes a module-level `router`"
            )
        else:
            router = _clone_router(raw_router)
    if router is not None:
        dp.include_router(router)

    # Feature routers ride on top of an already-working template — no db_path
    # to give them (and nothing meaningful to attach to) if the template itself
    # never resolved. Same condition covers the per-bot custom_features patch.
    if typed_config is not None:
        await _load_and_include_features(dp, bot, bot_id, typed_config.db_path)
        await _load_and_include_custom_feature(dp, bot_id, typed_config.db_path)
        # runtime/miniapp_api.py's handlers read entry.config.get("db_path")
        # (the BotEntry.config dict below, not typed_config) — without this,
        # every mini-app write/read call 500s with "bot has no database
        # configured" even though the bot itself works fine, since
        # typed_config was otherwise only used internally above.
        config["db_path"] = typed_config.db_path

    # "Офисы" (docs/OFFICES_DESIGN.md) — by-convention opt-in, same pattern as
    # `router`/`init_db`/`config_from_bot_row`: a template that wants to
    # RECEIVE office events exposes a module-level on_office_event(event,
    # config) coroutine; features/office_events.py's publish_event() looks it
    # up via BotEntry.config["on_office_event"] set here. Deliberately checked
    # even when typed_config is None (module resolved but has no
    # config_from_bot_row-driven db_path) — this hook doesn't need db_path,
    # unlike the feature-loading block above. Imported locally (not at module
    # top) to avoid a circular import: features/office_events.py imports
    # runtime.registry_holder, not runtime.registry itself, so this is a
    # one-way dependency edge (registry -> office_events), never the reverse.
    if module is not None:
        on_office_event = getattr(module, "on_office_event", None)
        if on_office_event is not None:
            from features.office_events import register_office_event_hook

            async def _office_event_hook(event, _handler=on_office_event, _config=typed_config or config):
                await _handler(event, _config)

            register_office_event_hook(config, _office_event_hook)
        elif typed_config is not None:
            # docs/OFFICES_DESIGN.md §11 — the universal fallback: a
            # resolved bot (module is not None, so it has a real db_path via
            # typed_config) with NO hand-written on_office_event still gets
            # wired to features/office_events.py's generic_on_office_event(),
            # driven by this bot's own bot_office_hook_config row (may be
            # None — generic_on_office_event degrades to a plain fallback
            # note in that case, never raises). Covers template-based bots
            # AND from-scratch bots whose appended wiring (see
            # append_from_scratch_registry_wiring) didn't define
            # on_office_event for some reason — in practice the appended
            # wiring always defines it, so from-scratch bots normally hit
            # the branch above instead, not this one.
            from db.database import get_bot_office_hook_config
            from features.office_events import generic_on_office_event, register_office_event_hook

            async def _generic_office_event_hook(event, _bot_id=bot_id, _db_path=typed_config.db_path):
                hook_config = await get_bot_office_hook_config(_bot_id)
                await generic_on_office_event(event, _db_path, hook_config, bot_id=_bot_id)

            register_office_event_hook(config, _generic_office_event_hook)

    return BotEntry(bot=bot, dispatcher=dp, template_id=template_id, config=config)


def _config_from_row(b: dict[str, Any]) -> dict[str, Any]:
    return {
        "bot_id": b["id"],
        "name": b["name"],
        "display_name": b.get("display_name"),
        "group_chat_id": b.get("group_chat_id"),
    }


async def _close_bot_session(bot: Bot) -> None:
    try:
        await bot.session.close()
    except Exception:
        logger.exception("Failed to close bot session while evicting a registry entry")


class Registry:
    """Live, in-memory bot_id -> BotEntry registry (Stage 2 Phase 3).

    Wraps a plain dict with methods that let the webhook process pick up bots
    created/edited/deleted/restarted *after* startup, without a process restart
    (Phase 1's build_registry() only ran once, at boot).

    `get()` stays a plain synchronous dict-like lookup — deliberately NOT behind
    the lock. CPython's GIL makes a single dict.get()/__setitem__ atomic already;
    the lock's job here is only to serialize the *write* side (add_or_replace/
    remove/reload_all) against each other, e.g. two concurrent reload_all() calls,
    so a reader never has to await anything on the hot webhook path. Keeping the
    lookup synchronous also means this class is a drop-in replacement for the
    plain dict callers already use (webhook_app.py's webhook_handler, and Phase 1's
    tests that construct a raw dict directly) — nothing there needs to change.
    """

    def __init__(self) -> None:
        self._entries: dict[int, BotEntry] = {}
        self._lock = asyncio.Lock()

    def get(self, bot_id: int) -> BotEntry | None:
        return self._entries.get(bot_id)

    def __len__(self) -> int:
        return len(self._entries)

    def bot_ids(self) -> list[int]:
        return list(self._entries.keys())

    async def add_or_replace(self, bot_row: dict[str, Any]) -> BotEntry | None:
        """Builds a BotEntry from a bots-table row and inserts/replaces it under
        that bot's id. Returns None (registry left untouched) if the row has no
        token yet (bot not fully created). The old entry's Bot session (if any)
        is closed only AFTER the swap, outside the lock — building the new entry
        and closing the old one both do I/O/awaits, which must never happen while
        the lock is held (see Task 2 note on reload_all for the same pattern)."""
        if not bot_row.get("token"):
            return None
        template_id = None  # so the except block below can't NameError on it
        try:
            template_id = infer_template_id(bot_row.get("file_path"))
            config = _config_from_row(bot_row)
            entry = await build_entry(
                bot_row["id"], bot_row["token"], template_id, config, file_path=bot_row.get("file_path")
            )
        except Exception:
            logger.exception(
                f"add_or_replace: bot id={bot_row.get('id')} template={template_id!r} "
                f"({bot_row.get('name')}) — failed to build entry"
            )
            return None
        async with self._lock:
            old = self._entries.get(bot_row["id"])
            self._entries[bot_row["id"]] = entry
        if old is not None:
            await _close_bot_session(old.bot)
        return entry

    async def remove(self, bot_id: int) -> bool:
        async with self._lock:
            entry = self._entries.pop(bot_id, None)
        if entry is None:
            return False
        await _close_bot_session(entry.bot)
        return True

    async def reload_one(self, bot_id: int) -> BotEntry | None:
        """Re-reads one bot from the DB and rebuilds its entry in place. If the
        bot was deleted (or its token cleared), it's removed from the registry."""
        if bot_id == FACTORY_BOT_ID:
            logger.warning(
                "reload_one called with FACTORY_BOT_ID — skipping (factory entry is not reloadable from DB)"
            )
            return None
        bot_row = await get_bot(bot_id)
        if bot_row is None:
            await self.remove(bot_id)
            return None
        entry = await self.add_or_replace(bot_row)
        if entry is None:
            await self.remove(bot_id)
        return entry

    async def reload_all(self) -> None:
        """Full rebuild — same source data and per-bot error isolation as Phase
        1's build_registry(), but swaps the registry's contents in place under
        the lock instead of returning a fresh dict. The factory bot
        (FACTORY_BOT_ID) has no row in `bots` to rebuild from — its entry is
        preserved across the swap (and its Bot session left open) instead of
        being dropped and closed like a deleted tenant's would be."""
        new_entries: dict[int, BotEntry] = {}
        for b in await get_all_bots():
            if not b.get("token"):
                continue
            template_id = None  # so the except block below can't NameError on it
            try:
                template_id = infer_template_id(b.get("file_path"))
                config = _config_from_row(b)
                new_entries[b["id"]] = await build_entry(
                    b["id"], b["token"], template_id, config, file_path=b.get("file_path")
                )
            except Exception:
                logger.exception(
                    f"reload_all: skipping bot id={b.get('id')} template={template_id!r} "
                    f"({b.get('name')}) — failed to build entry"
                )
        async with self._lock:
            old_entries = self._entries
            factory_entry = old_entries.get(FACTORY_BOT_ID)
            if factory_entry is not None:
                new_entries[FACTORY_BOT_ID] = factory_entry
                logger.info("factory entry preserved across reload_all")
            self._entries = new_entries
        for bot_id, entry in old_entries.items():
            if bot_id == FACTORY_BOT_ID:
                continue  # preserved into new_entries above — its session must stay open
            await _close_bot_session(entry.bot)


async def build_registry() -> Registry:
    """Boot-time entry point (Phase 1's name, kept for webhook_app.py's
    _bootstrap_app() — behaves identically, just returns a live Registry
    instead of a one-shot dict)."""
    registry = Registry()
    await registry.reload_all()
    return registry
