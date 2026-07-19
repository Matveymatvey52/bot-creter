"""Stage 2 Phase 1 — in-memory bot registry for the webhook runtime.

Builds bot_id -> BotEntry (Bot + Dispatcher + template Router + config) from the
existing SQLite bots table (db/database.py — token decryption already happens there).
No Postgres, no process spawning: everything lives in this one process's memory.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher, Router
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import TelegramObject

from db.database import get_all_bots, get_bot

logger = logging.getLogger(__name__)

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


def _load_accountant_router() -> Router:
    # Imported lazily so importing runtime.registry never has side effects unless
    # a bot actually needs the accountant template's router.
    from templates import accountant as accountant_template

    return accountant_template.router


# template_id -> loader() -> Router. Only "accountant" is wired in Phase 1 (see
# docs/STAGE2_DESIGN.md / STAGE2_REPORT.md for why the rest are deferred).
_TEMPLATE_LOADERS: dict[str, Callable[[], Router]] = {
    "accountant": _load_accountant_router,
}

_template_router_cache: dict[str, Router] = {}


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

    The underlying template module (and its original Router) is loaded and
    cached once per process; each call here returns a new clone so it can be
    included into a new bot's Dispatcher without hitting aiogram's one-parent
    restriction (see _clone_router)."""
    if template_id not in _template_router_cache:
        loader = _TEMPLATE_LOADERS.get(template_id)
        if loader is None:
            return None
        _template_router_cache[template_id] = loader()
    return _clone_router(_template_router_cache[template_id])


class ConfigMiddleware(BaseMiddleware):
    """Generic fallback: injects the raw bot-metadata dict into data["config"] for
    templates that don't have their own typed config yet (everything except
    "accountant" — see _TEMPLATE_MIDDLEWARE_BUILDERS below for accountant's own
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


def _build_accountant_middleware(bot_row: dict[str, Any]) -> BaseMiddleware:
    # Lazy imports: keep importing runtime.registry side-effect-free unless a bot
    # actually needs this template, and keep the canonical DATA_DIR resolution
    # (config.py) as the single source of truth passed INTO the template rather
    # than re-derived inside it — see docs/STAGE2_DESIGN.md "Проверка идентичности
    # формул путей" for why that distinction matters.
    from templates import accountant as accountant_template
    from config import DATA_DIR

    acc_config = accountant_template.config_from_bot_row(bot_row, DATA_DIR)
    return accountant_template.ConfigMiddleware(acc_config)


# template_id -> builder(bot_row) -> BaseMiddleware. Templates not listed here
# fall back to the generic ConfigMiddleware above (raw dict, not yet consumed).
_TEMPLATE_MIDDLEWARE_BUILDERS: dict[str, Callable[[dict[str, Any]], BaseMiddleware]] = {
    "accountant": _build_accountant_middleware,
}


@dataclass
class BotEntry:
    bot: Bot
    dispatcher: Dispatcher
    template_id: str | None
    config: dict[str, Any] = field(default_factory=dict)


def build_entry(
    bot_id: int,
    token: str,
    template_id: str | None,
    config: dict[str, Any] | None = None,
) -> BotEntry:
    """Build one BotEntry: a Bot + a fresh Dispatcher wired to the shared template
    Router (if the template is known) and the config middleware."""
    config = dict(config or {})
    config.setdefault("bot_id", bot_id)

    bot = Bot(token=token)
    dp = Dispatcher(storage=MemoryStorage())

    middleware_builder = _TEMPLATE_MIDDLEWARE_BUILDERS.get(template_id) if template_id else None
    middleware = middleware_builder(config) if middleware_builder else ConfigMiddleware(config)
    dp.update.outer_middleware(middleware)

    router = get_template_router(template_id) if template_id else None
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
        try:
            template_id = infer_template_id(bot_row.get("file_path"))
            config = _config_from_row(bot_row)
            entry = build_entry(bot_row["id"], bot_row["token"], template_id, config)
        except Exception:
            logger.exception(
                f"add_or_replace: bot id={bot_row.get('id')} ({bot_row.get('name')}) — failed to build entry"
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
        the lock instead of returning a fresh dict."""
        new_entries: dict[int, BotEntry] = {}
        for b in await get_all_bots():
            if not b.get("token"):
                continue
            try:
                template_id = infer_template_id(b.get("file_path"))
                config = _config_from_row(b)
                new_entries[b["id"]] = build_entry(b["id"], b["token"], template_id, config)
            except Exception:
                logger.exception(f"reload_all: skipping bot id={b.get('id')} ({b.get('name')}) — failed to build entry")
        async with self._lock:
            old_entries = self._entries
            self._entries = new_entries
        for entry in old_entries.values():
            await _close_bot_session(entry.bot)


async def build_registry() -> Registry:
    """Boot-time entry point (Phase 1's name, kept for webhook_app.py's
    _bootstrap_app() — behaves identically, just returns a live Registry
    instead of a one-shot dict)."""
    registry = Registry()
    await registry.reload_all()
    return registry
