# FEATURE: sales_analytics
# COMPATIBLE_WITH: accountant, booking_beauty, booking_fitness, booking_medical, booking_restaurant, campaign_tracker, car_rental, channel_monitor, coworking_space, debtors, delivery_tracker, event_manager, event_rsvp, expense_tracker, feedback_survey, habit_tracker, inventory, loyalty_program, manager_secretary, moderator, orders_tracker, referral_program, rental_equipment, repair_tracker, shop_catalog, staff_scheduler, support_tickets, tour_operator, tourist_documents, trip_manager, vehicle_service
"""Generic per-bot sales/records analytics — see docs/SALES_ANALYTICS_DESIGN.md.

Not to be confused with runtime/factory_analytics_api.py (the FACTORY owner's
cross-bot dashboard reading the central `bots`/`bot_feedback` tables). This
module computes stats about a SINGLE bot's own business data, for that bot's
OWN owner, surfaced inside that bot's mini-app (see runtime/miniapp_api.py's
analytics_handler, which is the only caller of compute_metrics() below).

Listed COMPATIBLE_WITH every current template (same posture as
features/payments.py's own broad list) because real gating is NOT the
template whitelist — it's whether a bot has a usable office_hook_config
(db/database.py's bot_office_hook_config, produced by
services/claude_service.py's _generate_office_hook_config). A bot enabled for
this feature but with no {"table", "match_field"} (e.g. moderator, whose
admin-only tables aren't customer records) simply has no analytics data to
show — same "degrade to unavailable, never partially wrong" contract
office_hook_config's other consumer (features/office_events.py's
generic_on_office_event) already follows.

No init_db() here (unlike payments.py) — this module owns no tables of its
own, it only reads whatever table office_hook_config already points at. No
aiogram `router` either: the whole point of this feature (per the product
owner's explicit "mini-app only, no chat command" decision) is that its UI
lives entirely in the SPA, not as bot chat commands — runtime/registry.py's
feature loader tolerates a feature module with no `router` attribute the same
way it tolerates one with no `init_db` (both looked up via getattr(...,
None)).

Same identifier-safety posture as generic_on_office_event(): table/column
names from office_hook_config are re-validated against this bot's ACTUAL
sqlite schema (PRAGMA table_info) at call time, checked against a strict
identifier allowlist, and double-quoted before ever being interpolated into
SQL — a stale or hallucinated office_hook_config can only degrade to "no
metrics", never to malformed/unsafe SQL.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Literal

import aiosqlite

logger = logging.getLogger(__name__)

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

Period = Literal["week", "month", "quarter"]

_PERIOD_DAYS: dict[Period, int] = {"week": 7, "month": 30, "quarter": 90}

_TOP_CUSTOMERS_LIMIT = 10


async def _table_columns(db: aiosqlite.Connection, table: str) -> set[str] | None:
    """Same helper as features/office_events.py's _table_columns — kept as a
    separate copy rather than a shared import, since these two features have
    no other coupling and a shared util module would be the only reason for
    either to import the other (see feedback_review_agents guidance: avoid
    speculative shared abstractions for a two-line helper)."""
    async with db.execute(f'PRAGMA table_info("{table}")') as cursor:
        rows = await cursor.fetchall()
    if not rows:
        return None
    return {row[1] for row in rows}


def _resolve_config(hook_config: dict[str, Any] | None) -> tuple[str, str | None, str | None] | None:
    """Extracts and identifier-validates (table, match_field, created_at_field)
    from a raw office_hook_config dict. Returns None if there's no usable
    table at all (nothing to compute analytics from) — this is the "feature
    not applicable to this bot" signal callers turn into a 404."""
    if not hook_config:
        return None
    table = hook_config.get("table")
    match_field = hook_config.get("match_field")
    created_at_field = hook_config.get("created_at_field")
    if not isinstance(table, str) or not _IDENTIFIER_RE.match(table):
        return None
    if match_field is not None and (not isinstance(match_field, str) or not _IDENTIFIER_RE.match(match_field)):
        match_field = None
    if created_at_field is not None and (
        not isinstance(created_at_field, str) or not _IDENTIFIER_RE.match(created_at_field)
    ):
        created_at_field = None
    return table, match_field, created_at_field


async def compute_metrics(db_path: str, hook_config: dict[str, Any] | None, period: Period) -> dict[str, Any] | None:
    """Computes generic record-volume/repeat-customer metrics for one bot's
    own data, using nothing but office_hook_config's {table, match_field,
    created_at_field} — no domain knowledge of what a "record" represents.

    Returns None if the bot has no usable office_hook_config (nothing to
    compute against — see _resolve_config) or the table it names doesn't
    actually exist in this bot's OWN schema right now (stale config, e.g.
    after a /recreate that changed the schema without regenerating the hook
    config — re-checked here the same defensive way
    generic_on_office_event() re-checks before every use, never trusted
    blindly from generation time).

    Never raises — any per-metric failure (e.g. match_field/created_at_field
    turning out to collide with a SQLite reserved word) is caught and that
    metric is simply omitted, mirroring generic_on_office_event()'s isolated
    try/except around its own optional tier-2 lookup."""
    resolved = _resolve_config(hook_config)
    if resolved is None:
        return None
    table, match_field, created_at_field = resolved

    async with aiosqlite.connect(db_path) as db:
        columns = await _table_columns(db, table)
        if columns is None:
            logger.warning(f"compute_metrics: table {table!r} from office_hook_config no longer exists — stale config")
            return None
        if match_field is not None and match_field not in columns:
            match_field = None
        if created_at_field is not None and created_at_field not in columns:
            created_at_field = None

        result: dict[str, Any] = {"total": 0, "volume": [], "delta_pct": None, "top_customers": [], "new_vs_returning": None}

        try:
            async with db.execute(f'SELECT COUNT(*) FROM "{table}"') as cursor:
                row = await cursor.fetchone()
                result["total"] = row[0] if row else 0
        except Exception:
            logger.warning(f"compute_metrics: total count failed for table={table!r}", exc_info=True)
            return None  # can't even count rows — nothing else here can work either

        if created_at_field is not None:
            try:
                result["volume"] = await _volume_over_time(db, table, created_at_field, period)
                result["delta_pct"] = await _period_delta(db, table, created_at_field, period)
            except Exception:
                logger.warning(
                    f"compute_metrics: time-bucketed metrics failed for table={table!r} "
                    f"created_at_field={created_at_field!r}", exc_info=True,
                )

        if match_field is not None:
            try:
                result["top_customers"] = await _top_customers(db, table, match_field)
                result["new_vs_returning"] = await _new_vs_returning(db, table, match_field, created_at_field, period)
            except Exception:
                logger.warning(
                    f"compute_metrics: customer metrics failed for table={table!r} "
                    f"match_field={match_field!r}", exc_info=True,
                )

    return result


def _period_start_modifier(period: Period, *, offset_periods: int = 0) -> str:
    """A SQLite datetime() modifier string like '-7 days', for use as
    datetime('now', 'localtime', <this>) in the queries below — computed
    entirely INSIDE SQLite rather than in Python (contrast: an earlier
    version computed the cutoff as datetime.now(timezone.utc) in Python and
    passed it as a bound parameter). That mattered because templates store
    created_at via SQLite's own datetime('now','localtime') (see e.g.
    templates/booking_medical.py, templates/accountant.py) — a Python-side
    UTC cutoff compared lexicographically against a server-local-time string
    silently skews every period boundary by the server's UTC offset (wrong
    day in the volume chart near midnight, wrong new/returning bucketing)
    unless the server happens to run in UTC, which nothing in this repo
    pins. Anchoring the cutoff to SQLite's own 'now','localtime' instead
    guarantees it's computed with the exact same clock/timezone basis the
    stored timestamps were written with, regardless of server TZ."""
    days = _PERIOD_DAYS[period]
    return f"-{days * (offset_periods + 1)} days"


async def _volume_over_time(
    db: aiosqlite.Connection, table: str, created_at_field: str, period: Period
) -> list[dict[str, Any]]:
    """COUNT(*) grouped by day within the requested period — the SPA draws
    this as a simple bar chart. date() on the stored created_at_field works
    whether it's an ISO 'YYYY-MM-DD HH:MM:SS' TEXT column or an SQLite
    TIMESTAMP default (both formats already used across templates, e.g.
    datetime('now','localtime') defaults) — a value SQLite's date() can't
    parse is simply excluded from GROUP BY (SQLite returns NULL for it,
    grouped together), not a crash."""
    modifier = _period_start_modifier(period)
    async with db.execute(
        f'''SELECT date("{created_at_field}") AS d, COUNT(*) AS c
            FROM "{table}"
            WHERE "{created_at_field}" >= datetime('now', 'localtime', ?)
              AND date("{created_at_field}") IS NOT NULL
            GROUP BY d ORDER BY d''',
        (modifier,),
    ) as cursor:
        rows = await cursor.fetchall()
    return [{"date": r[0], "count": r[1]} for r in rows]


async def _period_delta(db: aiosqlite.Connection, table: str, created_at_field: str, period: Period) -> float | None:
    """% change in record count: current period vs the immediately preceding
    one of equal length. None (not 0.0) if the prior period had zero records
    — a "from 0 to N" delta has no meaningful percentage, and the SPA is
    expected to render that as "—", not a misleading "+inf%" or "0%"."""
    current_modifier = _period_start_modifier(period)
    prior_modifier = _period_start_modifier(period, offset_periods=1)

    async def _count_since(start_modifier: str, end_modifier: str | None) -> int:
        if end_modifier is None:
            query = f'''SELECT COUNT(*) FROM "{table}"
                        WHERE "{created_at_field}" >= datetime('now', 'localtime', ?)'''
            params: tuple = (start_modifier,)
        else:
            query = f'''SELECT COUNT(*) FROM "{table}"
                        WHERE "{created_at_field}" >= datetime('now', 'localtime', ?)
                          AND "{created_at_field}" < datetime('now', 'localtime', ?)'''
            params = (start_modifier, end_modifier)
        async with db.execute(query, params) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0

    current = await _count_since(current_modifier, None)
    prior = await _count_since(prior_modifier, current_modifier)
    if prior == 0:
        return None
    return round((current - prior) / prior * 100, 1)


async def _top_customers(db: aiosqlite.Connection, table: str, match_field: str) -> list[dict[str, Any]]:
    """Top repeat customers by raw record count on match_field — deliberately
    ALL-TIME (not period-scoped), since "who is our most loyal customer" is
    naturally a lifetime question, unlike volume/delta which are inherently
    about a recent window."""
    async with db.execute(
        f'''SELECT "{match_field}" AS m, COUNT(*) AS c
            FROM "{table}" WHERE "{match_field}" IS NOT NULL
            GROUP BY m ORDER BY c DESC LIMIT ?''',
        (_TOP_CUSTOMERS_LIMIT,),
    ) as cursor:
        rows = await cursor.fetchall()
    return [{"match_field": r[0], "count": r[1]} for r in rows]


async def _new_vs_returning(
    db: aiosqlite.Connection, table: str, match_field: str, created_at_field: str | None, period: Period
) -> dict[str, int] | None:
    """Within the requested period: how many DISTINCT match_field values had
    their FIRST-EVER record in this window (new) vs already had at least one
    earlier record (returning). Requires created_at_field — without it there
    is no way to tell "first" from "repeat" chronologically, so this returns
    None (caller already only invokes it when match_field is set; the
    created_at_field check is still needed here since a bot can have
    match_field without created_at_field)."""
    if created_at_field is None:
        return None
    modifier = _period_start_modifier(period)
    # The cutoff is fetched from SQLite itself (rather than computed in
    # Python) so the later Python-side `first_seen >= since` string
    # comparison stays on the SAME clock/timezone basis as first_seen
    # itself (both ultimately derived from SQLite's 'now','localtime') —
    # see _period_start_modifier's docstring for why a Python-UTC value
    # compared against server-localtime strings would skew this.
    async with db.execute("SELECT datetime('now', 'localtime', ?)", (modifier,)) as cursor:
        (since,) = await cursor.fetchone()
    async with db.execute(
        f'''SELECT "{match_field}" AS m, MIN("{created_at_field}") AS first_seen
            FROM "{table}" WHERE "{match_field}" IS NOT NULL
            GROUP BY m''',
    ) as cursor:
        first_seen_rows = await cursor.fetchall()
    async with db.execute(
        f'''SELECT DISTINCT "{match_field}" AS m FROM "{table}"
            WHERE "{match_field}" IS NOT NULL AND "{created_at_field}" >= datetime('now', 'localtime', ?)''',
        (modifier,),
    ) as cursor:
        active_rows = await cursor.fetchall()

    first_seen = {r[0]: r[1] for r in first_seen_rows}
    active_ids = {r[0] for r in active_rows}
    new_count = sum(1 for m in active_ids if first_seen.get(m, "") >= since)
    returning_count = len(active_ids) - new_count
    return {"new": new_count, "returning": returning_count}
