# FEATURE: office_events
# COMPATIBLE_WITH: accountant, booking_beauty, booking_fitness, booking_medical, booking_restaurant, boss_bot, campaign_tracker, car_rental, coworking_space, debtors, delivery_tracker, event_manager, event_rsvp, expense_tracker, feedback_survey, habit_tracker, inventory, loyalty_program, manager_bot, manager_secretary, moderator, orders_tracker, referral_program, rental_equipment, repair_tracker, shop_catalog, staff_scheduler, support_tickets, tour_operator, tourist_documents, trip_manager, vehicle_service
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

import html
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from db.database import get_bot, get_office_subscribers
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


@dataclass(frozen=True)
class TaskAssignedEvent:
    task_id: int
    title: str
    description: str
    deadline: str          # ISO 8601
    assignee_hint: str
    boss_chat_id: int


_EVENT_TYPES: dict[str, type] = {
    "order.created": OrderCreatedEvent,
    "task.assigned": TaskAssignedEvent,
}

# Human-readable labels for the picker/confirmation screens in both
# handlers/manage_bots.py's "🏢 Офисы" Telegram flow AND the miniapp's
# office-link wizard (runtime/factory_analytics_api.py's
# list_office_event_types_handler) — kept alongside _EVENT_TYPES so a new
# event_type can't be added there without a caller-facing label. Falls back
# to the raw event_type string if a key is somehow missing (see
# EVENT_TYPE_LABELS.get usage) rather than raising in a UI render path.
EVENT_TYPE_LABELS: dict[str, str] = {
    "order.created": "новый заказ",
    "task.assigned": "новая задача",
}

# Which template_ids actually PUBLISH each event_type — a strict subset of
# COMPATIBLE_WITH above (which only gates who can RECEIVE/subscribe). Used by
# the miniapp office-link wizard (runtime/factory_analytics_api.py's
# list_office_event_types_handler) to offer only event types the chosen
# SOURCE bot can realistically emit, instead of the full _EVENT_TYPES set —
# e.g. offering "task.assigned" for a shop_catalog bot would create a link
# that will simply never fire. The Telegram-side wizard (handlers/
# manage_bots.py's cb_office_pick_type) still offers every _EVENT_TYPES key
# unfiltered — this is an additional, stricter check layered on top for the
# miniapp surface, not a replacement for that flow. Deliberately explicit
# (not derived from scanning templates/*.py for publish_event() calls at
# runtime) for the same "no silent widening" reason _EVENT_TYPES itself is a
# closed dict: a new publisher call site must be a deliberate addition here,
# not an automatic side effect of adding a publish_event() call somewhere.
#
# order.created's publisher set is None (not a fixed set) because its only
# publisher, features/payments.py's on_successful_payment, fires for EVERY
# template payments is compatible with — that list already lives in
# payments.py's own "# COMPATIBLE_WITH:" header (parsed by
# runtime/registry.py's discover_features(), the same mechanism every other
# feature's compatibility list goes through) rather than a second hand-kept
# copy here that could drift from it.
_EVENT_TYPE_PUBLISHER_TEMPLATES: dict[str, set[str] | None] = {
    "order.created": None,
    "task.assigned": {"boss_bot"},
}


def available_event_types_for_template(template_id: str | None) -> list[str]:
    """Event types `template_id` can plausibly PUBLISH as a source bot — see
    _EVENT_TYPE_PUBLISHER_TEMPLATES. order.created's availability is derived
    from features/payments.py's own COMPATIBLE_WITH header via
    runtime/registry.py's discover_features(), its only current publisher.
    A from-scratch bot (template_id=None) can never publish anything through
    this closed-dataclass mechanism, so it always gets an empty list. Local
    import of discover_features to avoid a runtime.registry <-> this module
    import cycle at module load time (registry.py imports feature modules
    dynamically, not the reverse, but this keeps the dependency one-way at
    parse time too)."""
    if template_id is None:
        return []
    from runtime.registry import discover_features

    payments_compatible: set[str] = set()
    for feature in discover_features():
        if feature["name"] == "payments":
            payments_compatible = set(feature["compatible_with"])
            break

    available = []
    for event_type, publishers in _EVENT_TYPE_PUBLISHER_TEMPLATES.items():
        if publishers is None:
            if event_type == "order.created" and template_id in payments_compatible:
                available.append(event_type)
        elif template_id in publishers:
            available.append(event_type)
    return available

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


async def _mirror_to_digest_group(registry, source_bot_id: int, event_type: str) -> None:
    """Best-effort, read-only mirror of ONE line per office_event into the
    owner's bound showcase group (db.database.get_office_digest_group), if
    any — see docs/OFFICES_DESIGN.md §12. Sent via the FACTORY bot, never a
    tenant bot: the group is a Creator-bot-owned "витрина", not a chat any
    client bot participates in (client bots are never added to it — see the
    design doc's "клиентские боты в группу не добавляются").

    Deliberately isolated from the real delivery loop in publish_event(): a
    digest-group failure (group deleted, factory bot kicked, no group bound
    at all) must never affect actual subscriber delivery, so this is called
    unconditionally before the subscriber loop and swallows every exception
    itself, same isolation contract as every other office_events failure
    mode in this module."""
    from db.database import get_office_digest_group
    from runtime.registry import FACTORY_BOT_ID

    try:
        source_bot = await get_bot(source_bot_id)
        # Per-owner showcase group (Stage 1 multitenancy) — mirror only to
        # the group bound by THIS bot's own owner, never another owner's
        # group. A source bot with no owner_telegram_id on file (shouldn't
        # happen post-backfill, but defensive) has nothing to mirror to.
        owner_telegram_id = source_bot.get("owner_telegram_id") if source_bot else None
        if owner_telegram_id is None:
            return
        chat_id = await get_office_digest_group(owner_telegram_id)
        if not chat_id:
            return
        factory_entry = registry.get(FACTORY_BOT_ID)
        if factory_entry is None:
            return
        # source_bot["name"] is owner/LLM-controlled free text (see
        # handlers/create_bot.py's naming flow) — escaped before
        # interpolation, same convention every other user/LLM-text-derived
        # message in this codebase follows (e.g. features/sellable_items.py,
        # templates/channel_monitor.py). This message has no parse_mode set
        # (plain text), so escaping here is defense-in-depth against a
        # future caller adding parse_mode="HTML" rather than a fix for a
        # live rendering bug — plain-text send_message doesn't interpret
        # '<'/'&' at all, but keeping the escape means switching to HTML
        # formatting later can't silently reintroduce injection.
        raw_name = source_bot["name"] if source_bot else str(source_bot_id)
        source_name = html.escape(raw_name)
        label = EVENT_TYPE_LABELS.get(event_type, event_type)
        text = f"🏢 «{source_name}»: {label}"
        await factory_entry.bot.send_message(chat_id, text)
    except Exception:
        logger.warning(
            f"_mirror_to_digest_group: failed to mirror event_type={event_type!r} "
            f"source_bot_id={source_bot_id} to digest group",
            exc_info=True,
        )


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

    await _mirror_to_digest_group(registry, source_bot_id, event_type)

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


# ── critical-error alerts to the owner (docs/CRITICAL_ALERTS_DESIGN.md) ─────
#
# report_critical_error() is the ONE call every interception point (aiogram
# errors.router, runtime/webhook_app.py's dispatch except, future payment/
# quota call sites) should use, so every alert has the same shape and goes
# through the same rate-limit/sanitization — see design doc §5 "единообразие".
#
# Deliberately NOT routed through publish_event()/bot_office_links: v1 scope
# is owner-facing alerts only (design doc §4), not a general-purpose event
# subscribers can opt into. Reusing bot_office_links would require inserting
# a link row for every bot at creation time, with the same silent-gap risk
# already known for other manual-linkage flows (see
# docs/CRITICAL_ALERTS_DESIGN.md §2's "ключевой вывод"). The delivery target
# is always FACTORY_BOT_ID, unconditionally, resolved fresh from the live
# registry on every call — never cached, so a factory-bot reload/restart is
# picked up without any code here needing to know about it.

# How long a given (bot_id, category, message) combination is suppressed
# after being sent once. In-memory only — same "no persistence" MVP
# trade-off as the rest of this module (module docstring above); a process
# restart clears it, which just means the next occurrence sends immediately,
# not that anything breaks.
_CRITICAL_ERROR_RATE_LIMIT_SECONDS = 300

# Cap so a bot producing many DISTINCT error signatures (not just repeats of
# the same one) can't grow this dict unboundedly for the lifetime of the
# process — same reasoning as any other in-memory cache with no owner-driven
# eviction. Oldest entries are dropped first when the cap is hit.
_CRITICAL_ERROR_RATE_LIMIT_MAX_KEYS = 2000

# key -> last-sent monotonic timestamp
_critical_error_last_sent: dict[tuple[int, str, str], float] = {}

# Longest a single traceback/exception message is allowed to be once
# sanitized, before being sent to the owner. Long enough to be useful, short
# enough that one runaway error can't itself become a spam/flood problem via
# Telegram's own message-length limits or by burying the owner's chat.
_CRITICAL_ERROR_DETAIL_MAX_LEN = 500

def _known_secret_values() -> list[str]:
    """Actual secret VALUES this process's own env currently holds, so
    _sanitize_detail can redact them by literal match — catches anything a
    shape-based regex below would miss (e.g. an opaque base64 service-
    account key with no recognizable prefix). Read from config.py fresh on
    every call (not cached at import time) so a value rotated via env
    without a restart is still covered; the cost is negligible next to one
    Telegram API call. Only covers process-level secrets (config.py's own
    env vars) — a tenant bot's own per-bot secret (e.g. its YooKassa key,
    encrypted in the bots table) is NOT covered here, since this process
    never holds it in plaintext outside that bot's own request handling."""
    import config

    return [
        config.BOT_TOKEN,
        config.ANTHROPIC_API_KEY,
        config.ASSEMBLYAI_API_KEY,
        config.ENCRYPTION_KEY,
        config.GOOGLE_SHEETS_SA_KEY_B64,
        config.USERBOT_ENCRYPTION_KEY,
        config.GEMINI_API_KEY,
    ]


# Values that look like they came from something secret-shaped (bot tokens,
# API keys, connection strings) get replaced before the message ever leaves
# the process — a leaked exception message is exactly the kind of thing that
# can carry these (e.g. an aiohttp client error embedding the request URL
# with a token query param). Conservative and pattern-based, not a claim of
# completeness: this is a best-effort scrub for the SHAPES most likely to
# appear in this codebase's own exceptions, not a general secret scanner.
# No leading \b: a real token embedded in a URL path is literally
# "bot<digits>:<hash>" (e.g. api.telegram.org/bot123456789:AAA...) — the "t"
# right before the digits is a word character too, so \b wouldn't match
# there at all and the token would slip through unredacted. The trailing
# \b plus the 30-char minimum on the hash segment is enough to avoid
# matching on plain digit:digit ratios elsewhere in a message.
_TELEGRAM_TOKEN_RE = re.compile(r"\d{6,12}:[A-Za-z0-9_-]{30,}\b")
_BEARER_TOKEN_RE = re.compile(r"\b(Bearer|token)[\s=:]+[A-Za-z0-9_\-.]{16,}\b", re.IGNORECASE)
_URL_CREDENTIALS_RE = re.compile(r"(://)[^/\s:@]+:[^/\s:@]+@")
# key=/secret=/api_key=/password=<value> in a query string or a formatted
# f-string (e.g. "Invalid secret_key sk_live_xxx for shop 12345",
# "...?key=AIzaSyXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX") — review found the
# earlier three patterns miss exactly this shape, which is how this
# codebase's own YooKassa/Google Sheets/API-key config values would most
# likely surface in an exception message (features/payments.py, config.py's
# GOOGLE_SHEETS_SA_KEY_B64/ANTHROPIC_API_KEY/etc.).
_KEY_VALUE_SECRET_RE = re.compile(
    r"\b(api[_-]?key|secret[_-]?key|secret|password|access[_-]?token)\s*[=:]\s*\S{8,}",
    re.IGNORECASE,
)


def _scrub_secrets(text: str) -> str:
    """Best-effort scrub of secret-shaped substrings — no length cap here
    (see _sanitize_detail_for_sending below for why that's kept separate).
    Never raises — a sanitizer that itself fails must not block an alert
    the owner otherwise needs to see, so any unexpected input just falls
    through unchanged rather than propagating.

    Two layers, in order: (1) known process-level secret VALUES (from
    config.py's own env vars) are redacted by literal substring match first
    — this catches anything shape-based patterns below would miss, at the
    cost of only covering secrets this process actually knows about (not
    per-bot secrets like a tenant's own YooKassa key, which live encrypted
    in the bots table, never in this process's env); (2) shape-based
    patterns catch everything else on a best-effort basis. Neither layer
    claims completeness — see module comment above."""
    try:
        for secret_value in _known_secret_values():
            if secret_value:
                text = text.replace(secret_value, "[redacted-secret]")
        text = _TELEGRAM_TOKEN_RE.sub("[redacted-token]", text)
        text = _BEARER_TOKEN_RE.sub("[redacted-token]", text)
        text = _URL_CREDENTIALS_RE.sub(r"\1[redacted-credentials]@", text)
        text = _KEY_VALUE_SECRET_RE.sub(lambda m: f"{m.group(1)}=[redacted-secret]", text)
    except Exception:
        logger.warning("_scrub_secrets: pattern substitution failed — using unscrubbed text", exc_info=True)
    return text


def _cap_length(text: str) -> str:
    """Hard length cap applied only to the text actually SENT to the owner
    (see _sanitize_detail's docstring for why this is kept separate from
    scrubbing/rate-limit-keying)."""
    if len(text) > _CRITICAL_ERROR_DETAIL_MAX_LEN:
        return text[:_CRITICAL_ERROR_DETAIL_MAX_LEN] + "… (truncated)"
    return text


def _sanitize_detail(text: str) -> str:
    """_scrub_secrets() plus the hard length cap actually sent to the owner.
    Kept as two separate steps (not fused) because report_critical_error's
    rate-limit key needs the SCRUBBED-BUT-UNTRUNCATED text: keying on the
    truncated text would collide for any two distinct errors sharing a long
    common prefix (e.g. the same exception type wrapping a long URL/payload
    dump, differing only after the cap) — review flagged this as a real
    "distinct errors silently suppressed" risk, see _rate_limit_key's own
    docstring below. This wrapper exists for callers (and tests) that just
    want the final send-ready text in one call; report_critical_error itself
    calls _scrub_secrets() and _cap_length() separately for that reason."""
    return _cap_length(_scrub_secrets(text))


def _rate_limit_key(bot_id: int, category: str, detail: str) -> tuple[int, str, str]:
    """Groups by the sanitized, truncated detail text itself — two errors
    with the same category but different messages are different keys (both
    get delivered), while retries of the literal same failure collapse to
    one key and get suppressed. Deliberately not a hash of the raw traceback
    (which would vary per-frame on things like memory addresses in repr()
    output for some exception types) — the sanitized/truncated text is
    already the stable, owner-facing signal."""
    return (bot_id, category, detail)


def _should_send(key: tuple[int, str, str]) -> bool:
    """True if this exact (bot_id, category, detail) hasn't fired in the
    last _CRITICAL_ERROR_RATE_LIMIT_SECONDS. Evicts the oldest entries once
    the dict exceeds _CRITICAL_ERROR_RATE_LIMIT_MAX_KEYS, so a bot cycling
    through many distinct errors can't grow this unboundedly (see module
    comment above) — done here, on the write path, rather than a separate
    background sweep, since this module has no other periodic task to hang
    one off of."""
    now = time.monotonic()
    last_sent = _critical_error_last_sent.get(key)
    if last_sent is not None and now - last_sent < _CRITICAL_ERROR_RATE_LIMIT_SECONDS:
        return False
    if len(_critical_error_last_sent) >= _CRITICAL_ERROR_RATE_LIMIT_MAX_KEYS:
        oldest_key = min(_critical_error_last_sent, key=_critical_error_last_sent.get)
        del _critical_error_last_sent[oldest_key]
    _critical_error_last_sent[key] = now
    return True


async def report_critical_error(bot_id: int, category: str, exc: BaseException) -> None:
    """Delivers a short, sanitized alert about `exc` (raised while processing
    an update/request for `bot_id`) to the owner via the factory bot, if:
    - this exact (bot_id, category, sanitized detail) hasn't already fired
      within the rate-limit window (_should_send), and
    - the factory bot is actually reachable in the live registry right now.

    Never raises. This is deliberately the LAST line of defense in an
    already-exceptional path (an error handler, or an except block around
    per-update dispatch) — an exception escaping FROM here would either mask
    the original error or crash a process that's already mid-failure-handling.
    Every failure mode below (no registry, factory bot missing, send_message
    itself raising — e.g. owner blocked the bot, OWNER_ID unset) degrades to
    a single logger call, never a raised exception.

    Deliberately synchronous with the caller (awaited, not fire-and-forget
    like publish_event() is by convention) — callers are already inside an
    except block for something that already went wrong, so there's no
    separate "publisher's own request" to protect from this call's latency
    the way publish_event()'s docstring describes; a couple hundred ms for
    one Telegram API call is an acceptable cost here."""
    from handlers.admin_manager import OWNER_ID
    from runtime.registry import FACTORY_BOT_ID

    try:
        raw_detail = f"{type(exc).__name__}: {exc}"
        scrubbed_detail = _scrub_secrets(raw_detail)
    except Exception:
        scrubbed_detail = "(failed to format exception detail)"

    # Rate-limit key uses the FULL scrubbed text, not the truncated one sent
    # to the owner — see _sanitize_detail's docstring for why keying on the
    # truncated text would collide two distinct errors sharing a long common
    # prefix.
    key = _rate_limit_key(bot_id, category, scrubbed_detail)
    if not _should_send(key):
        logger.info(
            f"report_critical_error: bot_id={bot_id} category={category!r} suppressed "
            "by rate limit (identical error already reported recently)"
        )
        return

    detail = _cap_length(scrubbed_detail)

    if not OWNER_ID:
        logger.warning(
            f"report_critical_error: OWNER_ID not set — cannot deliver alert for "
            f"bot_id={bot_id} category={category!r} detail={detail!r}"
        )
        return

    registry = _registry_handle.value
    if registry is None:
        logger.warning(
            f"report_critical_error: no live registry — cannot deliver alert for "
            f"bot_id={bot_id} category={category!r} detail={detail!r}"
        )
        return

    factory_entry = registry.get(FACTORY_BOT_ID)
    if factory_entry is None:
        logger.warning(
            f"report_critical_error: FACTORY_BOT_ID not in live registry — cannot deliver "
            f"alert for bot_id={bot_id} category={category!r} detail={detail!r}"
        )
        return

    occurred_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    text = (
        f"⚠️ Критическая ошибка\n\n"
        f"Бот: {bot_id}\n"
        f"Категория: {category}\n"
        f"Время: {occurred_at}\n\n"
        f"{detail}"
    )
    try:
        await factory_entry.bot.send_message(OWNER_ID, text)
    except Exception:
        logger.exception(
            f"report_critical_error: failed to deliver alert to owner — "
            f"bot_id={bot_id} category={category!r} detail={detail!r}"
        )
