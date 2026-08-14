# delivery_tracker — design notes

Brief companion to `templates/delivery_tracker.py`. Follows the same
config/admin/FSM conventions as every other template (see
`docs/STAGE2_DESIGN.md` for the shared factory-level contract); this doc
only covers what's specific to this template. Structural reference:
`templates/repair_tracker.py` — same shape (FSM order intake, forward-only
status flow with a "cancelled" side-branch, client auto-notification on
every status change, admin-only private note, price collected at one
specific transition).

## Schema

- `deliveries(id, client_user_id, client_name, client_phone,
  pickup_address, dropoff_address, item_description, status, price,
  courier_note, created_at, updated_at)` — a single table, no per-item
  catalog. `item_description` is free text ("what and where to deliver"),
  not a product reference — this template is deliberately NOT a catalog/shop
  flow (that's `shop_catalog.py`).

## Key decisions

**client_user_id captured directly at creation.** Same simplification as
`repair_tracker.py`'s `repair_tickets`: a delivery is only ever created BY
the client themselves, in the same flow that captures their Telegram
user_id, so ownership is established by construction. "Мои доставки" and the
detail view both enforce `WHERE client_user_id=?` on every read — this
template's equivalent of `vehicle_service.py`'s Contact-ownership check,
expressed as a SQL predicate instead of a Contact.user_id comparison.

**Status transitions are forward-only**, same shape as
`repair_tracker.py`'s `STATUS_TRANSITIONS`: `new → accepted → picked_up →
in_transit → delivered`, with `cancelled` reachable as a side-branch from any
non-terminal status, no backward moves. The admin's status handler
(`cb_adlv_status`) uses the same compare-and-swap (`UPDATE ... WHERE
status=old_status`) as every other template's status handler, so a
double-tap on a stale button is a silent no-op on the second write rather
than a duplicate transition/notification.

**Price is collected at `new → accepted`, before the transition applies.**
This is the one stateful branch: `cb_adlv_status()` detects that specific
`(old_status, target)` pair and diverts into `StatusPriceFlow` instead of
calling `_apply_status_change()` immediately — every other transition
applies directly from a single button tap, mirroring
`repair_tracker.py`'s `diagnosing → in_progress` price gate exactly (just at
a different point in the chain, per the brief's "price set at 'accepted'").

**Client is notified on every status change**, not just one terminal
status — `_apply_status_change()` sends a best-effort `bot.send_message`
after every successfully-applied transition, catching `TelegramAPIError` so
a client who blocked the bot never crashes the admin's flow. The status
change itself is never rolled back on a failed notification.

**`courier_note` is admin-only**, set/updated via `NoteFlow`, and
deliberately excluded from `_client_delivery_text()` and from the client
notification body in `_apply_status_change()` — never rendered anywhere in a
client-facing surface, same guarantee as `repair_tracker.py`'s `admin_note`.

**"Все доставки" uses a status-filter menu** (`kb_status_filters()`,
`adlv_filter:<status|all>`), same pattern as `repair_tracker.py` — one
button per status value plus an "all" catch-all, rather than paging through
the full history unfiltered.

**Multi-bot isolation** is achieved purely through per-bot `db_path`
(`bot_<bot_id>_data.db`) and `admins_file` (`admins_<bot_id>.json`), derived
from `bots.id` in `config_from_bot_row()` — no `bot_id` column on
`deliveries` is needed since each bot's rows live in a physically separate
SQLite file. Proven in `tests/test_delivery_tracker.py`'s
`DeliveryTrackerIsolationTests`: the same Telegram admin/client user_id
driving two different bot configs never sees the other bot's rows.

## Deferred / known gaps

- No SQLite busy-timeout retry/backoff beyond aiosqlite's default connect
  behavior — same known limitation as every other template using plain
  compare-and-swap UPDATEs without `BEGIN IMMEDIATE` (this template has no
  overlap/race-prone insert like `car_rental.py`'s date-range booking, so the
  weaker guarantee is acceptable here).
- No admin-side manual delivery creation — only the client FSM inserts into
  `deliveries`; an admin can only react to orders clients create, same
  precedent as `car_rental.py`.
- No courier assignment/multi-courier routing — `courier_note` is a single
  free-text field per delivery, not a courier-identity/assignment system.
  Out of scope per the brief (a small single-courier-service tracker, not a
  dispatch platform).
