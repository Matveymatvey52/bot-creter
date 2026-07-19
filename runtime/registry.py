"""Stage 2 Phase 1 — in-memory bot registry for the webhook runtime.

Builds bot_id -> BotEntry (Bot + Dispatcher + template Router + config) from the
existing SQLite bots table (db/database.py — token decryption already happens there).
No Postgres, no process spawning: everything lives in this one process's memory.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher, Router
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import TelegramObject

from db.database import get_all_bots

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
    """Injects this bot's config dict into every handler's `data`, under data["config"].

    NOTE: this is plumbing, not yet consumption — see STAGE2_REPORT.md. The
    accountant template's handlers still read module-level constants (DB_PATH,
    ADMINS_FILE) rather than data["config"]; wiring the template itself to use
    the injected config is separate follow-up work.
    """

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
    dp.update.outer_middleware(ConfigMiddleware(config))

    router = get_template_router(template_id) if template_id else None
    if router is not None:
        dp.include_router(router)

    return BotEntry(bot=bot, dispatcher=dp, template_id=template_id, config=config)


async def build_registry() -> dict[int, BotEntry]:
    """Build the in-memory bot_id -> BotEntry registry from the existing bots table.
    Bots without a token (not yet fully created) are skipped. Each bot is built in
    isolation — one bad row (invalid token, unreadable file) is logged and skipped
    rather than crashing the whole bootstrap and taking every other bot down with it.

    KNOWN PHASE 1 LIMITATION: this only runs once, at process startup. A bot
    created via /create after the webhook server is already running will not
    appear in the registry (and its webhook calls will 404) until the process
    is restarted. Picking this up live is follow-up work, not done here.
    """
    registry: dict[int, BotEntry] = {}
    for b in await get_all_bots():
        if not b.get("token"):
            continue
        try:
            template_id = infer_template_id(b.get("file_path"))
            config = {
                "bot_id": b["id"],
                "name": b["name"],
                "display_name": b.get("display_name"),
                "group_chat_id": b.get("group_chat_id"),
            }
            registry[b["id"]] = build_entry(b["id"], b["token"], template_id, config)
        except Exception:
            logger.exception(f"build_registry: skipping bot id={b.get('id')} ({b.get('name')}) — failed to build entry")
    return registry
