"""Stage 2 Phase 1 — in-memory bot registry for the webhook runtime.

Builds bot_id -> BotEntry (Bot + Dispatcher + template Router + config) from the
existing SQLite bots table (db/database.py — token decryption already happens there).
No Postgres, no process spawning: everything lives in this one process's memory.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher, Router
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import TelegramObject

from db.database import get_all_bots, get_bot

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


async def _build_generic_middleware(bot_row: dict[str, Any], module: ModuleType) -> BaseMiddleware:
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
    simply doesn't get registered."""
    from config import DATA_DIR

    config = module.config_from_bot_row(bot_row, DATA_DIR)
    await module.init_db(config.db_path)
    return module.ConfigMiddleware(config)


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
    registry answering nothing, and nobody would know why."""
    config = dict(config or {})
    config.setdefault("bot_id", bot_id)

    bot = Bot(token=token)
    dp = Dispatcher(storage=MemoryStorage())

    module = await _load_template_module_async(template_id) if template_id else None
    if template_id and module is None:
        logger.warning(
            f"build_entry: bot_id={bot_id} has template_id={template_id!r} that does not "
            "match any templates/*.py module — registering with generic middleware and "
            "NO router; this bot will not respond to any update until its template_id is "
            "fixed or a matching template is added"
        )

    middleware = await _build_generic_middleware(config, module) if module is not None else ConfigMiddleware(config)
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
            entry = await build_entry(bot_row["id"], bot_row["token"], template_id, config)
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
                new_entries[b["id"]] = await build_entry(b["id"], b["token"], template_id, config)
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
