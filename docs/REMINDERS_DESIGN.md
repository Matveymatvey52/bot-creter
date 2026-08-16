# reminders.py — design (draft, awaiting approval)

## Problem

Every domain template (event_rsvp, car_rental, tour_operator, ...) has some
"thing with a date" a client should be reminded about, but each stores it in
its own schema (`event_details.event_date`, `rental_bookings.start_date`,
etc.). Today notification is purely event-driven (fires once, on booking
creation) — nothing is time-driven. We want a feature module, wired the same
way as `payments.py`, that any COMPATIBLE_WITH template can turn on without
writing bot-specific reminder code.

## Precedents found (see inventory)

- **`payments.py`**: by-convention wiring — `# FEATURE:` / `# COMPATIBLE_WITH:`
  header, `init_db(db_path)`, module-level `router` (cloned per bot),
  `bot_id` injected via outer middleware, config duck-typed against a
  `Protocol` the host's `ConfigMiddleware` already provides. This is the
  wiring pattern reminders.py should reuse as-is.
- **`miniapp_config["resources"][].fields[].kind == "date"`**: the *only*
  existing place a template already marks "this field is a date" — used by
  `event_rsvp.py`, `car_rental.py`. Nearest thing to a schema-adapter.
- **No generic cross-schema adapter exists.** `sellable_items.py` looked
  like a candidate but isn't one — it's a parallel universal table, not an
  adapter over each template's own data.
- **No periodic loop exists in `combined_app.py`.** The only precedent is
  `userbot_worker.py`'s `_summarize_loop()` (separate process, plain
  `while True` + `asyncio.sleep`). Reminders would be the first
  `asyncio.create_task` background sweep added to `combined_app.py`'s
  `_bootstrap_app()`.

## Part 1 — declaring "what to remind about"

Reuse and extend `miniapp_config` rather than invent a third declaration
mechanism. A host template already writes:

```python
miniapp_config = {
    "resources": [{
        "name": "events", "table": "event_details",
        "fields": [
            {"name": "event_date", "label": "Дата", "kind": "date", ...},
        ],
    }],
}
```

Add an optional `reminders_config` module attribute, sibling to
`miniapp_config`, independent of whether a resource is miniapp-exposed at
all (tour_operator's date-bearing tables aren't flat, per the inventory —
`resources`/`fields` may not map cleanly, so don't hard-couple to it):

```python
reminders_config = {
    "rules": [
        {
            "id": "event_upcoming",              # stable id, used in reminder_log
            "table": "event_details",
            "date_field": "event_date",           # column, must be ISO-parseable
            "date_format": "%Y-%m-%d %H:%M",       # explicit, since columns are TEXT
            "recipient_field": "creator_id",       # column holding the telegram user_id to notify
                                                    # (or "recipient_query": "<table>.user_id via join" — see below)
            "offsets_hours": [24, 2],              # remind 24h and 2h before date_field
            "message_template": "🔔 Напоминание: «{title}» начнётся {event_date:%d.%m %H:%M}",
            "active_field": "status = 'confirmed'",# optional raw SQL WHERE fragment, guards cancelled/draft rows
        },
    ],
}
```

Design choices, each defended against the concrete templates found:

- **`table` + `date_field`, not a full query** — matches `miniapp_config`'s
  existing granularity (one resource = one table); car_rental and
  event_rsvp both have a flat table with a date column, so this covers the
  two known real cases without over-generalizing.
- **`date_format` is explicit** — the inventory found `event_date` is a raw
  TEXT column, not a real SQL date type. reminders.py cannot assume format;
  make the template say so.
- **`recipient_field` is a column on the same table** — covers
  `rental_bookings.user_id`-style ownership directly. For cases where the
  recipient is one hop away (e.g. `event_rsvp` guests joined off a separate
  RSVP table), a `recipient_query` escape hatch (raw SQL returning
  `(chat_id, row_id)` pairs) is needed — flagged as an open question below
  rather than resolved now, since only one template needs it and it's not
  confirmed.
- **`offsets_hours` is a list**, not one offset — car_rental's "24h and 2h
  before" is a realistic ask and doesn't complicate the scan query.
- **`active_field` as a raw WHERE fragment** — deliberately minimal/leaky
  (SQL fragment, not a structured filter) to avoid designing a query DSL
  for a first version. Host template authors already write raw SQL in
  `init_db()`, so this matches the codebase's existing trust level for
  template code (same trust boundary payments.py's `Protocol` duck-typing
  already relies on — template code is not sandboxed).
- Tour_operator's non-flat schema is **out of scope for v1** — it doesn't
  fit the `table`+`date_field` shape, and forcing a fit would compromise
  the design for the two templates that *do* fit cleanly. Revisit once
  tour_operator's tour-date storage is confirmed.

## Part 2 — module wiring (mirrors payments.py)

`features/reminders.py`:
- Header: `# FEATURE: reminders` / `# COMPATIBLE_WITH: event_rsvp,car_rental,...`
- `init_db(db_path)` — creates one small tracking table in the host bot's
  own SQLite file (not a new DB), e.g. `reminder_log(rule_id, row_id,
  offset_hours, sent_at)`, used purely for dedup (never send the same
  offset twice for the same row). This is local to the host bot's DB, same
  as payments.py's tables.
- No `router` — reminders has no user-facing commands/handlers in v1 (pure
  background delivery), so nothing to clone into the Dispatcher. If a
  "manage my reminder rules" admin UI is wanted later, that would add a
  router the same way payments.py does.
- Reads `reminders_config` off the host template module the same way
  `runtime/miniapp_api.py` reads `miniapp_config` — a plain module attribute
  looked up by template_id, not a Protocol (there's no per-request Config
  object involved here, so duck-typing a Protocol doesn't apply; this is a
  static, load-time lookup akin to how `miniapp_config` is read).

## Part 3 — delivery (background sweep)

New: first `asyncio.create_task` loop added to `combined_app.py`'s
`_bootstrap_app()`, following the `_summarize_loop()` shape:

```python
async def _reminders_sweep_loop() -> None:
    while True:
        try:
            await _run_reminders_sweep(registry)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("reminders sweep failed")
        await asyncio.sleep(REMINDERS_SWEEP_INTERVAL_SECONDS)  # e.g. 300
```

`_run_reminders_sweep(registry)`:
1. Iterate `registry.bot_ids()` (existing pattern from `combined_app.py`).
2. For each bot with `"reminders"` in `bot_features` (same enablement table
   `payments.py` uses) and a `reminders_config` on its template module:
   for each rule, for each offset, run one `SELECT` against the host bot's
   own DB for rows where `date_field - offset_hours ≈ now` (small window,
   e.g. ±sweep interval, to avoid double-fires from clock/sweep-interval
   drift) AND not already in `reminder_log` for that `(rule_id, row_id,
   offset_hours)`.
3. Send via `entry.bot.send_message(recipient, message_template.format(...))`,
   best-effort try/except per row (mirrors car_rental's admin-notify
   try/except — one failed send must not abort the sweep or other rows).
4. On successful send, insert into `reminder_log` (dedup for next sweep).

APScheduler is **not needed** — the existing codebase precedent
(`_summarize_loop`) is a plain `while True` + `sleep`, already
process-safe for a single combined_app instance, adds no new dependency,
and matches what's already running in production. Recommend not
introducing APScheduler unless a real need for cron-expression scheduling
(vs. fixed-interval sweep) shows up later.

## Open questions before implementation

1. **`recipient_query` escape hatch** — needed for event_rsvp (recipient
   not on the same row) or can v1 skip it and only ship for car_rental-shaped
   templates (single-table, recipient on same row)?
2. **Sweep interval vs. offset granularity** — 5 min sweep matches ±few-min
   accuracy on "remind 2h before"; confirm that's acceptable (vs. e.g. a
   tighter interval near small offsets).
3. **Multi-tenant fairness** — sweep loop is O(bots × rules × rows-in-window)
   every interval; fine at current bot counts, but worth flagging as a
   backlog item once bot count grows (same shape of concern as the
   sheets-shared-quota backlog item).
4. **tour_operator** — excluded from v1 `COMPATIBLE_WITH` until its
   date-bearing table shape is confirmed; add later without touching the
   core module.

Stopping here per instructions — design only, no code written. Waiting for
confirmation before implementing.
