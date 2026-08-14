# event_rsvp — design notes

Brief companion to `templates/event_rsvp.py`. Follows the same
config/admin/FSM conventions as every other template (see
`docs/STAGE2_DESIGN.md` for the shared factory-level contract); this doc
only covers what's specific to this template.

## Schema

- `event_details` — exactly one row (`id INTEGER PRIMARY KEY CHECK (id = 1)`):
  one bot = one event. `title`, `description`, `event_date` are free text set
  via the admin's "⚙️ Настроить мероприятие" flow; `capacity` is the total
  seat count (`NULL` = unlimited, never waitlists).
- `rsvps` — one row per registration attempt. `client_user_id` is the
  Telegram user id (guests are identified by their Telegram account, no
  phone-linking step needed since the guest IS the requester — unlike
  `orders_tracker`/`vehicle_service` where a customer isn't necessarily the
  one chatting with the bot). `guests_count` is the party size (1-5, capped
  by `MAX_GUESTS`). `status` is `confirmed` / `waitlist` / `cancelled` — a
  cancelled row is kept, not deleted, so history/audit stays intact and a
  guest's "Моя регистрация" screen can still explain what happened.

## Key decisions

- **Occupied seats are always re-derived, never stored as a counter.**
  `_confirmed_guest_count()` runs `SUM(guests_count) WHERE status='confirmed'`
  on every capacity check instead of maintaining a running total on
  `event_details`. A stored counter can drift from the actual `rsvps` rows
  under any bug or manual DB edit; a `SUM` query can't drift by construction.
  The extra query cost is negligible at the scale a single event's guest
  list operates at.

- **Race-safety needed an explicit `BEGIN IMMEDIATE`, not just "one
  connection block."** The first implementation put the capacity
  read-then-decide-then-insert inside a single `async with
  aiosqlite.connect(...)` block and assumed that was sufficient — matching
  the pattern used for the compare-and-swap `UPDATE ... WHERE status=X` seen
  throughout the other templates. It is NOT sufficient for a **read-first**
  sequence: SQLite's default `isolation_level=""` only takes the write lock
  at the first WRITE statement, so a bare `SELECT` runs lock-free, and two
  concurrent connections can both read "1 seat free" before either has
  written. This was caught by
  `tests/test_event_rsvp.py::EventRsvpRaceSafetyTests` (driven via real
  `asyncio.gather`, not sequential calls) — it overbooked on the first run.
  Fix: `await db.execute("BEGIN IMMEDIATE")` before the capacity SELECT in
  `_confirm_or_waitlist()`, which forces SQLite to take the RESERVED write
  lock up front, so the second concurrent caller blocks until the first
  commits and then re-reads a database that already reflects the first
  booking. `cb_rsvp_cancel`'s promotion path did NOT need this fix — its
  compare-and-swap `UPDATE rsvps SET status='cancelled' WHERE id=? AND
  status=?` is itself the first write in that block, so the lock is already
  held before the promotion read runs.

- **Waitlist promotion is FIFO by id, and skips (doesn't reorder around) a
  party too big for the freed seats.** `_promote_from_waitlist()` always
  looks at the single oldest `waitlist` row; if it doesn't fit in the
  currently-free capacity, no one is promoted this cancellation (not "try the
  next-oldest that does fit"). Reordering around a large party would be a
  fairness surprise a guest who registered first wouldn't expect, and the
  brief says "the next one from the waitlist" (singular, implying strict
  order) — the next cancellation, or an admin manually clearing space, will
  eventually free enough for that party.

- **Guests are identified by `client_user_id` (Telegram id), no phone-link
  step.** Every other template with a customer-facing role (orders_tracker,
  vehicle_service) needs a Contact-share step because the admin creates
  records for customers who aren't necessarily chatting with the bot
  themselves. Here the guest always IS the one interacting with `/start`, so
  `client_user_id` is trustworthy from the first callback — the phone number
  collected during registration is contact info for the organizer's records
  only (e.g. a phone-based guest list at the door), not a lookup key.

- **Ownership check on cancel.** `cb_rsvp_cancel` verifies
  `owner_id == cb.from_user.id` before honoring `rsvp_cancel:{id}` — without
  it, any guest could cancel any other guest's registration by guessing (or
  incrementing) an id in the callback_data. Same class of check as
  `orders_tracker.py`'s Contact-ownership verification, applied here to
  callback_data instead of a shared Contact object.

- **Lowering capacity does not retroactively waitlist already-confirmed
  guests.** `setup_capacity` accepts any positive integer, including one
  below the current confirmed headcount — it just means no *new* seat opens
  up until enough cancellations bring occupied back under the new cap.
  Bumping existing confirmed guests to waitlist as a side effect of an admin
  editing a number would be a surprising, unrequested behavior change to
  someone who already has a confirmed seat.

## Known gaps / deliberately out of scope

- Non-text input (photo, sticker, voice, etc.) sent while in an FSM state
  (`RsvpFlow.name`/`phone`, any `EventSetupFlow.*` step, `AdminMgmtFlow.
  add_admin`, or typing instead of tapping in `remove_admin_pick`) is
  silently dropped by aiogram — no handler matches, so the user gets no
  response and must know to send `/start` to escape. This is the SAME
  pattern used by every other template in this repo (all state-scoped
  message handlers are filtered `F.text, ~F.text.startswith("/")` with no
  generic non-text fallback) — not a regression specific to event_rsvp, and
  left as-is for consistency with house style rather than fixed unilaterally
  in one template.

- No SMS/email fallback if a guest blocks the bot before their waitlist
  promotion notification — `TelegramAPIError` is caught and logged, same as
  every other template's notify-on-status-change path; the guest's
  `rsvps.status` still correctly becomes `confirmed` even if the push fails.
- No per-event multi-tenancy — one bot instance is hardwired to exactly one
  event (`event_details.id` is `CHECK (id = 1)`), matching the brief's "один
  бот = одно мероприятие."
- No capacity check on the admin's `setup_capacity` step against
  in-progress FSM registrations racing the admin's own capacity edit — an
  edge case judged out of scope: an admin changing capacity mid-event is
  rare, and the worst case is a guest who was about to be waitlisted instead
  reads a stale "seats available" summary for one message round-trip before
  their own registration re-checks capacity fresh at insert time (which it
  always does — `_confirm_or_waitlist` re-reads `event_details.capacity`
  itself, so no guest can ever be wrongly overbooked by this).
