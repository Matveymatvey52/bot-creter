# booking_restaurant — design notes

Brief companion to `templates/booking_restaurant.py`. Follows the same
config/admin/FSM conventions as every other template (see `docs/STAGE2_DESIGN.md`
for the shared factory-level contract); this doc only covers what's specific
to this template.

## Schema

- `tables(id, name, capacity, active)` — soft-deletable (hide, don't delete)
  via `active`.
- `reservations(id, client_user_id, client_name, client_phone, guests_count,
  date, time_window_start, time_window_end, occasion, deposit_required,
  deposit_confirmed, status, created_at)` — status ∈ `pending|confirmed|cancelled`.

No per-slot table assignment: a reservation books a party against the
restaurant's *aggregate* capacity for a date+window, not a specific table.
Matches the design brief ("бронь стола на компанию, не слот на человека") —
the admin decides seating manually; the bot only tracks whether there's
still room.

## Key decisions

**Time windows, not exact minutes.** `TIME_WINDOWS` is a fixed list of
2-hour service windows (`12:00–14:00` … `22:00–23:30`). A reservation is
booked against one whole window, matching "временное окно, не точная минута".

**Capacity check is against CONFIRMED reservations only**, not pending ones
(`_available_windows`). This is the literal wording of the design brief
("против уже подтверждённых броней"). Consequence: two clients can both see
a window as available and both submit pending requests that together exceed
capacity — this is intentional; the admin is the actual capacity arbiter and
sees the reservation card (with guest count) before confirming. A stricter
"reserve capacity at pending time" model was considered and rejected as
scope creep beyond what the brief asked for, and it would need an expiry
mechanism for abandoned pending requests to avoid permanently squatting
capacity.

**Banquet deposit is fully automatic.** `guests_count >= BANQUET_THRESHOLD`
(default 8) sets `deposit_required=1` at creation time — no admin decision
needed to trigger it. The admin's only deposit action is marking it paid
(`💰 Отметить депозит оплачен`), which is disabled once already confirmed
(idempotent, no re-notify on a repeat tap).

**Cancellation is never blocked, only annotated.** `CANCEL_FREE_HOURS` (24)
only changes the wording shown after a client cancels — "просто честная
информация", no in-bot penalty logic, per the brief.

**Client identity, not phone-linking.** Unlike `vehicle_service.py` /
`orders_tracker.py`, this template doesn't need a Contact-based phone→
telegram_user_id linking flow: the client is already authenticated by the
Telegram update that started their own booking flow. Phone number is
collected purely as a contact field on the reservation, reusing
`vehicle_service.py`'s `_normalize_phone()` formula verbatim.

## Compare-and-swap gotcha (found via the test suite, fixed)

`cb_admres_confirm`/`cb_admres_reject` originally re-used the freshly-read
`old_status` as the `WHERE status=?` guard. On a double-tap this self-matches
(`'confirmed'='confirmed'`) and re-sends the client notification. Fixed to a
literal `WHERE status='pending'` guard, matching `cb_admres_deposit`'s
already-correct fixed `WHERE deposit_confirmed=0` pattern. Covered by
`test_double_tap_confirm_notifies_only_once` / `test_double_tap_reject_notifies_only_once`.

## Known gaps / deliberately out of scope

- No real table-assignment UI (which physical table seats which party) —
  admin handles that outside the bot, same abstraction level as the design
  brief.
- No expiry/auto-cancel of stale `pending` reservations.
- No SMS/email fallback if a Telegram notification fails — only the
  in-message note ("не удалось уведомить клиента").
- `features/sheets.py` / `features/payments.py` COMPATIBLE_WITH now include
  `booking_restaurant`, but no bolt-on wiring for either exists in this file
  (a bot owner could still enable them from the "🧩 Фичи" panel; deposit
  invoicing via `payments.py` would be a natural follow-up, not built here).
