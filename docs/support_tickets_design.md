# support_tickets template — design notes

Brief per-template notes, kept short (this codebase has no history of
per-template design docs beyond `docs/STAGE2_DESIGN.md`).

## Schema summary

- `tickets` — one row per ticket: `client_user_id`/`client_name`, `category`
  (key into the `TICKET_CATEGORIES` CUSTOMIZE dict), `priority` (derived from
  category, `CHECK`'d to low/medium/high), `description`, `status`
  (`CHECK`'d forward-only enum: open → in_progress → waiting_response →
  escalated → closed), `sla_deadline` (a stored, precomputed deadline —
  simpler and more testable than recomputing from category+created_at+lookup
  on every render), `satisfaction_rating` (nullable, `CHECK`'d 1–5, set only
  at close).
- `ticket_status_log` — same shape as `vehicle_service.py`'s
  `service_status_log`: old/new status, who changed it, whether the client
  was notified.
- `ticket_log` — the "conversation": the client's initial description is the
  first row (`author='client'`), and any admin reply is appended
  (`author='admin'`) and forwarded to the client as a bot message. This is
  the SIMPLER option the design brief explicitly allowed instead of a full
  chat-thread UI, while still satisfying "admin's reply reaches the client".
- `kb_articles` — `title`/`keywords`/`body`/`active` (soft-delete flag).

## Flow summary

- Client `/start` → client menu (🆕 Создать тикет / 📋 Мои тикеты). No
  Contact-linking needed (unlike `vehicle_service.py`) — the client is
  identified directly by their Telegram `user_id`, so there's no phone-based
  identity step at all.
- New ticket: pick category (fixed buttons, each mapped to priority + SLA
  hours in the `TICKET_CATEGORIES` CUSTOMIZE dict) → free-text description →
  **before** inserting anything, `_search_kb()` does a simple `LIKE`-based
  search over `kb_articles.title`/`keywords`. If there are matches, the
  client is shown them with "✅ Это помогло, закрыть" / "❌ Не помогло,
  создать тикет"; only the second path (or zero matches) creates the ticket
  row. This is the KB-search-BEFORE-ticket-creation gate the design brief
  calls out as the headline differentiator from a plain FAQ bot.
- Ticket created → `status='open'`, `sla_deadline` set from the category →
  every admin in `admins_<bot_id>.json` is notified (same
  `for admin_id in _load_admins(...): bot.send_message(...)` loop as
  `booking_fitness.py`).
- "📋 Мои тикеты" → ownership-checked list/detail (a client can never view or
  escalate another client's ticket by guessing an id — same response for
  "doesn't exist" and "belongs to someone else"). While open/in_progress, a
  "📞 Эскалировать к специалисту" button does a compare-and-swap status
  update to `escalated` and notifies admins with an urgency-marked message.
- Admin ticket card: forward-only status buttons (`STATUS_TRANSITIONS`,
  `closed` terminal), all mutations are `UPDATE ... WHERE id=? AND status=?`
  compare-and-swap (double-tap-safe, mirrors `vehicle_service.py`'s
  `cb_req_status`). A transition to `waiting_response` or `closed` sends the
  client a plain-text notification (`STATUS_NOTIFY_TEXT`); closing
  additionally sends a **separate** message with 1–5 rating buttons. A
  dedicated "✉️ Ответить клиенту" button (independent of status) starts a
  one-field FSM that logs the reply into `ticket_log` and forwards it to the
  client — this is what actually satisfies "ответ администратора с
  уведомлением клиенту" for the common case of an admin replying without
  necessarily changing status.
- "📚 База знаний": add/edit (single 3-step FSM reused for both, keyed by an
  optional `kb_edit_id` in FSM data)/hide (toggle `active`) — same CRUD shape
  as `vehicle_service.py`'s table management.
- "👥 Админы": copied verbatim from `vehicle_service.py`.

## Key decisions

- **SLA is a stored column, not computed on read.** `sla_deadline` is
  written once at creation (`datetime.now() + timedelta(hours=...)`), not
  derived from `category` + `created_at` + a lookup at render time. Simpler
  and means changing `TICKET_CATEGORIES`' SLA hours later never silently
  changes an already-created ticket's deadline.
- **KB search is a plain multi-term `LIKE`, not real full-text search** — the
  design brief explicitly allows this. Search terms come from
  `re.findall(r"\w{3,}", ...)` over `category label + description`, capped
  at 8 terms; the resulting SQL is built by repeating a **fixed** `(lower(title)
  LIKE ? OR ...)` fragment N times (N bounded by the 8-term cap, not user
  data) — every actual value still goes through a `?` placeholder, so this
  is not a SQL-injection risk despite being an f-string.
- **No Contact/phone linking.** Unlike `vehicle_service.py`/`orders_tracker.py`,
  a client here is just their Telegram `user_id` — there's no "which real-world
  customer is this" ambiguity to resolve, so the whole Contact-sharing dance
  is unnecessary and was deliberately left out.
- **Reply delivery is decoupled from status transitions.** An admin can send
  a free-text reply via "✉️ Ответить клиенту" without changing the ticket's
  status, and can also change status (which independently notifies for
  `waiting_response`/`closed`). This mirrors the design brief's own
  observation that not every status change needs to reach the client, but a
  reply always should.

## Known limitations / deliberately out of scope

- No real full-text search (see above) — acceptable per the design brief.
- No edit/delete of a client's own ticket description after creation.
- No admin-side bulk actions (bulk close, bulk reassign).
- No SLA-breach push notification to admins (the breach indicator is
  rendered on-demand in the ticket card, not proactively pushed) — would
  need a background scheduler, which no other template in this repo has
  either.
- `sheets` feature module was added to support_tickets' `COMPATIBLE_WITH`
  (useful for exporting ticket logs); `payments` was deliberately left off —
  this template never moves money.
