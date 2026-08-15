# FEATURE: office_events
# COMPATIBLE_WITH: accountant, booking_beauty, booking_fitness, booking_medical, booking_restaurant, campaign_tracker, car_rental, coworking_space, debtors, delivery_tracker, event_manager, event_rsvp, expense_tracker, feedback_survey, habit_tracker, inventory, loyalty_program, manager_secretary, moderator, orders_tracker, referral_program, rental_equipment, repair_tracker, shop_catalog, staff_scheduler, support_tickets, tour_operator, tourist_documents, trip_manager, vehicle_service
"""MVP of "офисы" — fire-and-forget event exchange between two bots owned by
the same factory instance (docs/OFFICES_DESIGN.md, variant A).

Deliberately the SIMPLEST of the three designed variants: no persistence, no
retry, no synchronous return value. A publisher calls publish_event(); this
module looks up subscribers in db.database's bot_office_links table and hands
the event to each subscribed bot's OWN on_office_event() hook, if it has one.
A subscriber that's offline (not in the live Registry — reload_one() removed
it, or it was never registered) or raises simply doesn't receive it — losing
delivery on a down/errored subscriber is an accepted MVP tradeoff (see
docs/OFFICES_DESIGN.md §6.3), not a bug to fix here.

Delivery needs the LIVE Registry (runtime.registry.Registry) to resolve a
target bot_id to its running Bot/config — the same one webhook_app.py's
request handler already uses. This module cannot import runtime.registry at
call time without a live instance (Registry is constructed once, at
combined_app.py's bootstrap, same ordering problem handlers/manage_bots.py's
own RegistryHandle already solves) — see set_registry() below, mirroring that
exact pattern one more time for this module.

Unlike features/payments.py or features/sheets.py, this module has NO
init_db and NO router: it never owns per-bot tables (bot_office_links lives
in the factory's own bots.db, via db.database — see docs/OFFICES_DESIGN.md
§3's "не в per-bot файлах") and there's no Telegram-native event to hook.
Every call is programmatic, from a host template's own handler, exactly like
features/sheets.py's write_row()/read_data().
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from db.database import get_office_subscribers
from runtime.registry_holder import RegistryHandle

logger = logging.getLogger(__name__)

_registry_handle = RegistryHandle()


def set_registry(registry) -> None:
    """Called once by runtime/combined_app.py's bootstrap, same as
    handlers/manage_bots.py's set_registry() — see runtime/registry_holder.py
    for why this can't just be a module-level import-time constant."""
    _registry_handle.set(registry)


# Deliberately a CLOSED, explicit set of event types with fixed payload
# fields — see docs/OFFICES_DESIGN.md §4/§6.4's decision against a free-form
# dict: a publisher passing an arbitrary payload could accidentally leak
# fields (a customer phone number, an internal note) into a target bot's own
# database that the subscriber never asked for and the publisher never
# meant to send. Adding a new event type means adding a new dataclass here
# AND registering it in _EVENT_TYPES below — deliberately not automatic, for
# the same "no silent widening" reason discover_features()'s COMPATIBLE_WITH
# has no "all" sentinel (see runtime/registry.py's discover_features
# docstring).
@dataclass(frozen=True)
class OrderCreatedEvent:
    order_id: int
    amount: int
    currency: str
    customer_chat_id: int


_EVENT_TYPES: dict[str, type] = {
    "order.created": OrderCreatedEvent,
}


@dataclass(frozen=True)
class OfficeEvent:
    """What a subscriber's on_office_event() hook actually receives — wraps
    the typed payload with the source bot_id, since a subscriber may be
    linked to more than one source and needs to tell them apart. Mirrors
    _attach_bot_id_middleware's naming (bot_id) for the RECEIVING bot's own
    id — that field is injected separately by the host template's own
    dispatcher plumbing, not carried on this event, so on_office_event()
    callers never need to pass it explicitly."""
    event_type: str
    source_bot_id: int
    payload: Any = field(default=None)


async def publish_event(source_bot_id: int, event_type: str, payload: object) -> int:
    """Fire-and-forget: delivers `payload` to every bot currently subscribed
    to (source_bot_id, event_type). Returns the number of subscribers the
    event was successfully handed to (0 if none, or if delivery to all of
    them failed) — callers that don't care can ignore the return value, same
    as features/payments.py's create_invoice() callers mostly do with its
    return.

    payload MUST be an instance of the dataclass registered for event_type in
    _EVENT_TYPES — raises ValueError otherwise, so a caller that passes the
    wrong shape (or a raw dict) fails loudly at the call site instead of
    silently reaching a subscriber with fields it doesn't expect.

    Never raises for a delivery failure — each subscriber is isolated in its
    own try/except, same reasoning as
    runtime/registry.py's _load_and_include_features(): one broken/offline
    subscriber must never block or fail delivery to the others, and must
    never propagate back to interrupt the publisher's own webhook handling."""
    expected_type = _EVENT_TYPES.get(event_type)
    if expected_type is None:
        raise ValueError(f"publish_event: unknown event_type {event_type!r} — not in _EVENT_TYPES")
    if not isinstance(payload, expected_type):
        raise ValueError(
            f"publish_event: event_type {event_type!r} expects a {expected_type.__name__} payload, "
            f"got {type(payload).__name__}"
        )

    registry = _registry_handle.value
    if registry is None:
        logger.debug(
            f"publish_event: no live registry available — event_type={event_type!r} "
            f"source_bot_id={source_bot_id} dropped (expected under main.py's polling process)"
        )
        return 0

    subscriber_ids = await get_office_subscribers(source_bot_id, event_type)
    if not subscriber_ids:
        return 0

    event = OfficeEvent(event_type=event_type, source_bot_id=source_bot_id, payload=payload)
    delivered = 0
    for target_bot_id in subscriber_ids:
        try:
            entry = registry.get(target_bot_id)
            if entry is None:
                logger.info(
                    f"publish_event: target_bot_id={target_bot_id} not in live registry "
                    f"(offline/deleted) — event_type={event_type!r} dropped for it"
                )
                continue
            hook = entry.config.get("on_office_event") if isinstance(entry.config, dict) else None
            if hook is None:
                logger.info(
                    f"publish_event: target_bot_id={target_bot_id} has no on_office_event hook "
                    f"registered — event_type={event_type!r} dropped for it"
                )
                continue
            await hook(event)
            delivered += 1
        except Exception:
            logger.exception(
                f"publish_event: delivery to target_bot_id={target_bot_id} raised — "
                f"event_type={event_type!r} source_bot_id={source_bot_id}, skipped"
            )
    return delivered


def register_office_event_hook(config: dict[str, Any], hook) -> None:
    """Lets a template's own config_from_bot_row()/init_db() (or, more
    commonly, runtime/registry.py's build_entry() on the template's behalf —
    see there) register its on_office_event(event) coroutine into the
    per-bot config dict publish_event() reads back via entry.config. A plain
    dict key rather than a new BotEntry field: BotEntry.config is already a
    free-form per-bot dict (see runtime/registry.py's _config_from_row), and
    adding a dedicated dataclass field there would require every OTHER
    template with no interest in office_events to still carry a None for it."""
    config["on_office_event"] = hook


# ── generic office-hook (docs/OFFICES_DESIGN.md §11) ────────────────────────
#
# The universal fallback wired by runtime/registry.py's build_entry() for any
# TEMPLATE-based bot that has NO hand-written on_office_event of its own —
# lets a customer's own generated bot react to office_events without the
# owner (or Claude) ever writing per-bot Python for it. Driven by
# bot_office_hook_config (db/database.py), itself produced by a cheap Haiku
# call at bot-creation/regeneration time (services/claude_service.py's
# _generate_office_hook_config) — see that module for the {"table":...,
# "match_field":...} shape.
#
# Deliberately data-driven, not code-generation: no per-bot Python is ever
# written or eval'd for this — table/column names are read back from the
# bot's OWN sqlite schema (via PRAGMA table_info) at call time and checked
# against a strict identifier allowlist before ever being interpolated into
# SQL, so a hallucinated or stale hook_config can only ever degrade to "no
# match attempted", never to malformed/unsafe SQL.

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


async def _table_columns(db, table: str) -> set[str] | None:
    """Real column names for `table` in this connection's own database, or
    None if the table doesn't exist. PRAGMA table_info doesn't accept bound
    parameters for the table name — table is validated against
    _IDENTIFIER_RE by the only caller (generic_on_office_event) before this
    is ever called, so the f-string below can't carry attacker-controlled
    SQL even though it isn't a bind parameter. Quoted for the same reserved-word
    reason as the SELECT in generic_on_office_event (e.g. table="order")."""
    async with db.execute(f'PRAGMA table_info("{table}")') as cursor:
        rows = await cursor.fetchall()
    if not rows:
        return None
    return {row[1] for row in rows}


async def generic_on_office_event(
    event: OfficeEvent, db_path: str, hook_config: dict[str, Any] | None, bot_id: int | None = None
) -> None:
    """The universal, auto-wired on_office_event() body for template-based
    bots with no hand-written hook of their own. Two-tier reaction, per
    docs/OFFICES_DESIGN.md §11:

    1. ALWAYS records a plain fallback note in this bot's own office_notes
       table (created here, IF NOT EXISTS, on first use — no template's
       init_db() needs to know about this table).
    2. IF hook_config names a real table+match_field (both re-validated
       against this bot's actual schema at call time, not just trusted from
       generation time), additionally checks whether event.payload's
       customer identifier already has a matching row there, and notes that
       too — the "this office event is about someone I already have a
       record for" signal docs/OFFICES_DESIGN.md §8's hand-written
       tour_operator/event_rsvp pair demonstrates, generalized.

    The tier-2 match attempt is isolated in its OWN try/except — review
    found that table/match_field could be a real column whose name happens
    to be a SQLite reserved word (e.g. "order", "group", "check" — all valid
    CREATE TABLE column names, all requiring quoting when used bare in a
    SELECT). Without isolation, that SyntaxError would abort BEFORE the
    tier-1 fallback INSERT, breaking the "ALWAYS records a note" guarantee
    above for exactly the templates (orders_tracker, shop_catalog,
    delivery_tracker, ...) most likely to hit it. Quoting both identifiers
    in double-quotes (safe here specifically because _IDENTIFIER_RE already
    rejects any input containing a quote character) avoids the failure mode
    entirely; the isolated try/except is defense in depth on top of that.

    Never raises — same isolation guarantee every other office_events
    subscriber in this codebase provides (features/office_events.py's own
    publish_event() isolates each subscriber, but this stays defensive on
    principle, same as templates/event_rsvp.py's hand-written
    on_office_event)."""
    import aiosqlite

    customer_chat_id = getattr(event.payload, "customer_chat_id", None)
    note = (
        f"Получено событие '{event.event_type}' от связанного бота (bot_id={event.source_bot_id})"
        + (f", клиент {customer_chat_id}." if customer_chat_id is not None else ".")
    )
    try:
        async with aiosqlite.connect(db_path) as db:
            await db.execute("PRAGMA busy_timeout=5000")
            await db.execute("""
                CREATE TABLE IF NOT EXISTS office_notes (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_bot_id   INTEGER NOT NULL,
                    event_type      TEXT NOT NULL,
                    note            TEXT NOT NULL,
                    created_at      TEXT DEFAULT (datetime('now','localtime'))
                )
            """)

            table = hook_config.get("table") if hook_config else None
            match_field = hook_config.get("match_field") if hook_config else None
            if (
                customer_chat_id is not None
                and isinstance(table, str) and _IDENTIFIER_RE.match(table)
                and isinstance(match_field, str) and _IDENTIFIER_RE.match(match_field)
            ):
                try:
                    columns = await _table_columns(db, table)
                    if columns and match_field in columns:
                        async with db.execute(
                            f'SELECT 1 FROM "{table}" WHERE "{match_field}" = ? LIMIT 1',
                            (customer_chat_id,),
                        ) as cursor:
                            matched = (await cursor.fetchone()) is not None
                        if matched:
                            note += f" Найдено совпадение в таблице '{table}' по полю '{match_field}'."
                except Exception:
                    logger.warning(
                        f"generic_on_office_event: match attempt against table={table!r} "
                        f"match_field={match_field!r} failed (stale/hallucinated hook_config?) — "
                        f"bot_id={bot_id} event_type={event.event_type}, falling back to plain note",
                        exc_info=True,
                    )

            await db.execute(
                "INSERT INTO office_notes (source_bot_id, event_type, note) VALUES (?, ?, ?)",
                (event.source_bot_id, event.event_type, note),
            )
            await db.commit()
    except Exception:
        logger.exception(
            f"generic_on_office_event: failed to record office note — "
            f"bot_id={bot_id} event_type={event.event_type} source_bot_id={event.source_bot_id} db_path={db_path}"
        )
