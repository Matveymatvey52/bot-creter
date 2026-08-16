# FEATURE: reminders
# COMPATIBLE_WITH: event_rsvp, car_rental
"""Reusable "remind clients about an upcoming date" feature module — see
docs/REMINDERS_DESIGN.md for the design this implements.

Unlike features/payments.py, this module exposes no `router` — v1 has no
user-facing commands/handlers, only a background sweep (driven from
runtime/combined_app.py, see there) that reads each host bot's own tables
and sends best-effort DMs. `init_db(db_path)` still follows the same
by-convention hook runtime/registry.py's build_entry()/_load_and_include_
features() already look for, adding one small dedup-tracking table
(reminder_log) to the host template's OWN per-bot db_path — same "no
separate db per feature" rule payments.py documents.

A host template opts in by:
  1. Listing itself in COMPATIBLE_WITH above (checked programmatically by
     runtime/registry.py's discover_features(), same as any other feature).
  2. Enabling "reminders" in bot_features (db/database.py) for a given bot.
  3. Exporting a module-level `reminders_config` dict (see ReminderRule
     below for the shape) describing which of its own tables/columns carry
     a reminder-worthy date and how to find the recipient.

Two recipient shapes are supported, matching the two real templates found
during design (docs/REMINDERS_DESIGN.md open question #1, resolved before
implementation — NOT deferred, since event_rsvp is one of only two
COMPATIBLE_WITH templates and skipping it would leave half of v1 unusable):
  - `recipient_field`: recipient's telegram user_id is a column on the SAME
    row as date_field (car_rental.rental_bookings.client_user_id).
  - `recipient_query`: the date-bearing row has no single owner — recipients
    are found via a separate query, e.g. event_rsvp's `rsvps` table joined
    off nothing (event_details is a singleton with no owner column at all).
    The query MUST select exactly one column aliased `chat_id`. If the query
    text contains a `?` placeholder, the date-bearing row's own `id` is bound
    to it (for a future per-row owner join); event_rsvp's query has none,
    since every confirmed rsvp is a recipient regardless of which row (there
    is only ever one) triggered the sweep.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import aiosqlite
from aiogram import Bot

logger = logging.getLogger(__name__)

# How close "now" must be to a (date_field - offset_hours) instant for a
# sweep to consider it due. Must be >= the actual sweep interval (see
# runtime/combined_app.py's REMINDERS_SWEEP_INTERVAL_SECONDS) or a reminder
# whose due instant falls between two sweeps would never be caught by either.
DUE_WINDOW_MINUTES = 10


@dataclass(frozen=True)
class ReminderRule:
    id: str
    table: str
    date_field: str
    date_format: str
    offsets_hours: list[int]
    message_template: str
    recipient_field: str | None = None
    recipient_query: str | None = None
    active_field: str | None = None

    def __post_init__(self) -> None:
        if bool(self.recipient_field) == bool(self.recipient_query):
            raise ValueError(
                f"reminders rule {self.id!r}: exactly one of recipient_field/"
                "recipient_query must be set, not both/neither"
            )


def _parse_rules(reminders_config: dict[str, Any] | None) -> list[ReminderRule]:
    if not reminders_config:
        return []
    rules: list[ReminderRule] = []
    for raw in reminders_config.get("rules", []):
        try:
            rules.append(ReminderRule(
                id=raw["id"],
                table=raw["table"],
                date_field=raw["date_field"],
                date_format=raw["date_format"],
                offsets_hours=list(raw["offsets_hours"]),
                message_template=raw["message_template"],
                recipient_field=raw.get("recipient_field"),
                recipient_query=raw.get("recipient_query"),
                active_field=raw.get("active_field"),
            ))
        except (KeyError, ValueError) as e:
            logger.warning(f"reminders: malformed rule in reminders_config — skipped: {e}")
    return rules


async def init_reminders_tables(db_path: str) -> None:
    """Adds the dedup-tracking table to a template's OWN per-bot db_path —
    this module never opens/owns a database file by itself, same convention
    as features/payments.py's init_payments_tables()."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS reminder_log (
                rule_id       TEXT NOT NULL,
                row_id        INTEGER NOT NULL,
                offset_hours  INTEGER NOT NULL,
                sent_at       TEXT DEFAULT (datetime('now','localtime')),
                PRIMARY KEY (rule_id, row_id, offset_hours)
            )
        """)
        await db.commit()


init_db = init_reminders_tables


async def _due_rows(
    db: aiosqlite.Connection, rule: ReminderRule, offset_hours: int, now: datetime,
) -> list[aiosqlite.Row]:
    """One row per due (rule, row, offset) combination not yet in
    reminder_log. `active_field` is a raw SQL WHERE fragment written by the
    trusted template author (same trust level as init_db()'s own CREATE
    TABLE statements and miniapp_config's table/field names — never
    request-controlled), not request input, so it is safe to splice
    directly; only the datetime bounds below are ever bound as parameters."""
    window = timedelta(minutes=DUE_WINDOW_MINUTES)
    target = now + timedelta(hours=offset_hours)
    lo = (target - window).strftime(rule.date_format)
    hi = (target + window).strftime(rule.date_format)

    where = f"{rule.date_field} BETWEEN ? AND ?"
    if rule.active_field:
        where += f" AND ({rule.active_field})"
    where += f"""
        AND NOT EXISTS (
            SELECT 1 FROM reminder_log
            WHERE reminder_log.rule_id = ? AND reminder_log.row_id = {rule.table}.id
              AND reminder_log.offset_hours = ?
        )
    """
    query = f"SELECT * FROM {rule.table} WHERE {where}"
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(query, (lo, hi, rule.id, offset_hours))
    rows = await cursor.fetchall()
    await cursor.close()
    return rows


async def _recipients_for_row(
    db: aiosqlite.Connection, rule: ReminderRule, row: aiosqlite.Row,
) -> list[int]:
    if rule.recipient_field:
        value = row[rule.recipient_field]
        return [int(value)] if value is not None else []
    # recipient_query — see module docstring: must select exactly a `chat_id`
    # column. The date-bearing row's own id is bound as the query's only `?`
    # IF it has one — event_rsvp's query needs none (event_details is a
    # singleton, so every confirmed rsvp is a recipient regardless of which
    # row triggered the sweep), while a future multi-row template with a
    # real per-row owner join (e.g. "... WHERE event_id = ?") would need it.
    params = (row["id"],) if "?" in rule.recipient_query else ()
    cursor = await db.execute(rule.recipient_query, params)
    recipient_rows = await cursor.fetchall()
    await cursor.close()
    return [int(r[0]) for r in recipient_rows if r[0] is not None]


async def run_reminders_sweep_for_bot(
    bot: Bot, db_path: str, reminders_config: dict[str, Any] | None, *, now: datetime | None = None,
) -> int:
    """Checks every rule in reminders_config against db_path, sends any due
    reminder as a best-effort DM, and records it in reminder_log so the next
    sweep never re-sends it. Returns the number of reminders actually sent.

    One row's send failure (blocked bot, deleted chat, ...) must never abort
    the sweep for other rows/rules/bots — mirrors templates/car_rental.py's
    own admin-notify try/except, and features/payments.py's isolation
    principle that one broken piece must not break unrelated work."""
    rules = _parse_rules(reminders_config)
    if not rules:
        return 0
    now = now or datetime.now()
    sent = 0
    async with aiosqlite.connect(db_path) as db:
        for rule in rules:
            for offset_hours in rule.offsets_hours:
                try:
                    rows = await _due_rows(db, rule, offset_hours, now)
                except Exception:
                    logger.exception(
                        f"reminders: bot db={db_path!r} rule={rule.id!r} offset={offset_hours} "
                        "— failed to query due rows, skipping this rule/offset"
                    )
                    continue
                for row in rows:
                    try:
                        recipients = await _recipients_for_row(db, rule, row)
                    except Exception:
                        logger.exception(
                            f"reminders: bot db={db_path!r} rule={rule.id!r} row_id={row['id']} "
                            "— failed to resolve recipients, skipping this row"
                        )
                        continue
                    text = _render_message(rule, row)
                    any_sent = False
                    for chat_id in recipients:
                        try:
                            await bot.send_message(chat_id, text)
                            any_sent = True
                            sent += 1
                        except Exception as e:
                            logger.warning(
                                f"reminders: bot db={db_path!r} rule={rule.id!r} row_id={row['id']} "
                                f"chat_id={chat_id} — send failed: {type(e).__name__}: {e}"
                            )
                    if any_sent or not recipients:
                        # Recorded even with zero recipients (e.g. an event
                        # nobody RSVP'd to yet) — the alternative is retrying
                        # every sweep forever for a row that will never gain
                        # recipients once its date has passed.
                        await db.execute(
                            "INSERT OR IGNORE INTO reminder_log (rule_id, row_id, offset_hours) VALUES (?, ?, ?)",
                            (rule.id, row["id"], offset_hours),
                        )
                        # Committed immediately, not batched to one commit at
                        # sweep-end — review finding: a crash between sending
                        # a message and a batched end-of-sweep commit would
                        # lose every already-sent row's dedup record in that
                        # pass, causing them all to resend on the next sweep.
                        # Committing per-row shrinks that window to just this
                        # one row.
                        await db.commit()
    return sent


def _render_message(rule: ReminderRule, row: aiosqlite.Row) -> str:
    fields = {key: row[key] for key in row.keys()}
    raw_date = fields.get(rule.date_field)
    if isinstance(raw_date, str):
        try:
            fields[rule.date_field] = datetime.strptime(raw_date, rule.date_format)
        except ValueError:
            pass  # leave as the raw string — template's format() spec (if any) just won't apply
    try:
        return rule.message_template.format(**fields)
    except (KeyError, IndexError):
        logger.warning(f"reminders: rule={rule.id!r} message_template references a missing field — sent raw template")
        return rule.message_template
