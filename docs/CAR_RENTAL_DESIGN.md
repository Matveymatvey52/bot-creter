# car_rental — design notes

Brief companion to `templates/car_rental.py`. Follows the same config/admin/
FSM conventions as every other template (see `docs/STAGE2_DESIGN.md` for the
shared factory-level contract); this doc only covers what's specific to this
template.

## Schema

- `rental_items(id, name, description, price_per_day, active)` — a specific
  unit of equipment/vehicle. Soft-deletable (hide, don't delete) via `active`.
- `rental_bookings(id, item_id, client_user_id, client_name, client_phone,
  start_date, end_date, status, created_at)` — `start_date`/`end_date` are
  `YYYY-MM-DD` strings; `status` ∈ `pending|confirmed|cancelled|completed`.

Unlike `booking_fitness.py`/`booking_restaurant.py` (fixed time slots/
windows), a booking here is a **date range against one specific item row** —
double-booking is a date-range overlap problem, not a slot/capacity problem.

## Key decisions

**Overlap check is per-item, inclusive-range.** Two ranges `[start_date,
end_date]` for the SAME `item_id` overlap iff
`NOT (new_end < existing_start OR new_start > existing_end)`. This treats a
shared boundary day as a conflict (a booking ending 2026-09-05 and one
starting 2026-09-05 both need the unit that day) — deliberately the
conservative reading, matching typical single-unit rental turnaround where
the same physical car/bike can't be in two places on the same calendar day.

**Only `pending`/`confirmed` bookings block.** `cancelled`/`completed`
bookings are excluded from the overlap check (`BLOCKING_STATUSES`), so a
cancelled hold or a finished rental frees the date range immediately for a
new booking on the same item.

**Race-safety: `BEGIN IMMEDIATE` around read-decide-write.** Same protection
class as `templates/booking_fitness.py`'s `_book_slot`, `templates/
event_manager.py`'s link flow, `templates/moderator.py`'s
`_apply_escalation`, and `templates/shop_catalog.py`'s checkout — this is the
project's established pattern for "read current state, decide, write" races,
reused verbatim rather than inventing a new mechanism:

```python
async with aiosqlite.connect(db_path, timeout=10) as db:
    await db.execute("BEGIN IMMEDIATE")
    # 1. item still active?
    # 2. any pending/confirmed booking on this item overlapping the range?
    # 3. if free: INSERT the booking
    await db.commit()
```

`BEGIN IMMEDIATE` takes SQLite's write lock before the read, so two clients
racing to book overlapping ranges on the same item can't both read "no
conflict" and both insert — the second transaction blocks (up to `timeout`)
until the first commits, then re-reads and correctly sees the first
booking's row. The lock is released at `commit()`, before any Telegram API
call is made with the result. `_create_booking_if_free()` is the single
choke point for every booking creation (both the client FSM's final step and
any future admin-side manual booking) — nothing else inserts into
`rental_bookings`.

**Status transitions are forward-only**, same shape as `vehicle_service.py`'s
`STATUS_TRANSITIONS`: `pending → confirmed → completed`, with `cancelled`
reachable as a side-branch from any non-terminal status, no backward moves.
The admin's status-change handler uses the same compare-and-swap
(`UPDATE ... WHERE status=old_status`) as every other template's status
handler, so a double-tap on a stale button is a silent no-op on the second
write rather than a duplicate transition/notification.

**Client is notified on every admin status change** (not just one terminal
status) — simpler than `vehicle_service.py`'s "only notify on ready" because
a rental booking has no single natural "done, come pick it up" moment the
way a service request does; the client cares about pending→confirmed and
any→cancelled equally.

## Deferred / known gaps

- No admin-side manual booking creation (only the client FSM inserts into
  `rental_bookings`); an admin can only react to bookings clients create.
  Would reuse `_create_booking_if_free()` directly if added later.
- No SQLite busy-timeout retry/backoff beyond aiosqlite's `timeout=10` on the
  booking connection — under very high concurrent load on ONE item, a
  transaction could still hit `database is locked` past that window. Same
  accepted tradeoff as the reference templates this pattern was copied from.
- `price_per_day` is not used anywhere in cost calculation (no total-price
  display on a booking) — schema has the column per the required spec, but
  no derived "total = days × price" line was requested and none was added.
