# support_tickets

Ticket system with state that persists until closed: category-based
self-service search over a knowledge base, ticket creation, admin
reply/escalate/close, and a client satisfaction rating collected on close.

Source: `templates/support_tickets.py`. Follows the repo's usual
single-file-template conventions (`Config` dataclass, `admins_file` JSON,
`init_db()`, aiogram FSM) — see `docs/STAGE2_DESIGN.md` for the shared
scaffolding this template reuses (config_from_env/config_from_bot_row,
ConfigMiddleware, ".../data/bot_<id>_data.db" isolation by `bots.id`).

## Schema

```
kb_articles(id, category, question, answer, active)   -- active=0 = soft-deleted
tickets(id, client_user_id, client_name, category, status, satisfaction_rating,
        created_at, updated_at)
ticket_messages(id, ticket_id, sender['client'|'admin'], text, created_at)
```

No `clients` table and no phone/Contact-linking step (unlike
`vehicle_service.py`/`orders_tracker.py`): a ticket's client identity IS the
requester's Telegram `user_id` directly, captured at ticket-creation time.
Any first-time `/start` from a non-admin goes straight into the client menu.

Categories (`TICKET_CATEGORIES` in the CUSTOMIZE block) are short ascii
codes (`billing`, `technical`, `account`, `other`) shared between
`kb_articles.category` and `tickets.category` so a client's category pick
filters both the self-service search and, later, the created ticket.

## Ticket status state machine

```
new ──(admin replies)──────────► answered
new ──(admin escalates)────────► escalated
new ──(admin closes)───────────► closed
answered ──(admin escalates)───► escalated
answered ──(admin closes)──────► closed
answered ──(admin replies again)──► answered   (self-loop, just appends a message)
escalated ──(admin replies)────► escalated   (self-loop — see note below)
escalated ──(admin closes)─────► closed
closed: terminal — no further status changes; client can still view history
        and (if not yet rated) submit a satisfaction rating.
```

Every transition uses the same compare-and-swap `UPDATE ... WHERE id=? AND
status=?` pattern as `vehicle_service.py`'s `cb_req_status`, so a stale
double-tap on an admin button is a no-op on the second write instead of a
double-notify/double-log. `cb_tk_escalate`/`cb_tk_close` share one
unconditional-render shape: the CAS write is attempted only when the
precondition holds (`old_status in ("new","answered")` for escalate,
`old_status != "closed"` for close), and either way the handler falls
through to the SAME final read+render — so a stale/already-applied button
tap is a safe no-op (re-renders current state, doesn't insert a second
service note or notification) rather than a special-cased early return.

A reply on an **escalated** ticket deliberately does NOT downgrade it back
to `answered` (`admin_reply_text` special-cases `old_status == "escalated"`
to keep it there) — a plain reply must not silently erase the "this needs a
human, not just an answer" signal another admin already set.

A client reply does **not** change ticket status (deliberate simplification,
see below) — only admin actions (`tk_reply`/`tk_escalate`/`tk_close`) move
the status machine.

## Two roles, two flows

**Client** (`/start` as a non-admin):
- 🆘 Поддержка → pick a category → active `kb_articles` for that category
  shown as buttons → "🙁 Это не помогло" → free-text problem description →
  ticket created (`status='new'`) → all registered admins notified.
- 📋 Мои тикеты → own tickets (ownership-checked by `client_user_id` on every
  read — ticket ids are sequential, so this is a real IDOR surface without
  the filter) → ticket card = full transcript → "✍️ Ответить" if not closed
  (adds a `ticket_messages` row, notifies all admins as a follow-up ping);
  disabled once `status='closed'`.
- On close, the bot pushes a 1–5 rating prompt to the client directly. If
  that push fails (blocked bot) or is dismissed, the same rating buttons
  reappear as a fallback whenever the client reopens that closed ticket's
  card via "Мои тикеты" (`satisfaction_rating IS NULL` gates this).

**Admin** (`/start` as a registered admin, `admins_file` JSON, same
first-`/start`-becomes-admin bootstrap as every other template):
- 🎫 Тикеты → Активные (oldest-first — an SLA queue needs the
  longest-waiting ticket at the top, not the newest) / Закрытые / Все →
  ticket card → ✍️ Ответить (FSM reply → `ticket_messages` + `status=
  'answered'` + client notified) / 🔺 Эскалировать (`status='escalated'` +
  a distinct service-note message, no client notification) / ✅ Закрыть
  (`status='closed'` + client gets the rating push).
- 📚 База знаний → browse by category (admin browse deliberately shows
  hidden articles too, prefixed with 🗑, unlike the client-facing search) →
  ➕ Добавить статью (FSM: category buttons → question → answer) /
  ✏️ Редактировать (re-asks question+answer, updates in place) / 🗑 Скрыть
  (soft-delete: `active=0`, row and any ticket history referencing it
  untouched — no "un-hide" button, see below).
- 👥 Админы — identical add/remove pattern to every other template.

## Deliberate simplifications / known limitations

- **No un-hide for KB articles.** `kb_hide` only ever sets `active=0`; there
  is no button to flip it back to `active=1`. The article itself stays fully
  reachable and editable by an admin (browse-by-category deliberately does
  NOT filter `active=1` on the admin side, only the client-facing search
  does — see `cb_kb_cat` vs. `cb_cli_cat`), so nothing is actually lost or
  hidden from the admin; there's just no single-tap "restore" action. Cheap
  to add later (`kb_show:<id>`) if it becomes a real workflow gap.
- **No hard guard against a double-submit race on ticket/KB-article
  creation.** `client_problem_text` and `kb_answer_text`'s insert branch each
  clear their FSM state immediately before the INSERT (shrinking, not
  eliminating, the window), but there's no `_flow_submitting`-style
  in-process lock (the pattern `event_manager.py` uses for its own
  double-tap-prone final-submit step) around either insert. Two genuinely
  concurrent submits for the same flow (e.g. a double-tap-triggered resend
  arriving as two near-simultaneous webhook requests) could in principle
  create two ticket/article rows instead of one. Judged low-value to fully
  close for a single free-text submit (unlike a status *transition*, there's
  no natural "already applied" state to CAS against), so left as a residual,
  accepted risk rather than adding a lock for it.
- **Client replies don't reopen/change status.** A client message on an
  `answered` ticket leaves `status='answered'` rather than bouncing back to
  `new`. Admins are still pinged (`FOLLOWUP_ADMIN_NOTIFY`) so nothing is
  silently missed, but the status column alone won't tell an admin "this
  needs a fresh look" — they have to read the transcript. Chosen to keep the
  status machine to the 4 states the design brief specified rather than
  inventing a 5th ("reopened") state.
- **No SLA timer/alerting job.** "SLA-conscious queue" is implemented as
  oldest-first sorting on the active-tickets list, not as a background timer
  or breach notification — there's no scheduler in this template (or most of
  this repo's templates) to hang a cron-style check off of.
- **Admin broadcast is best-effort, per-recipient.** `_notify_admins` loops
  every id in `admins_file` and swallows `TelegramAPIError`/`ValueError` per
  recipient (same rationale as `event_manager.py`'s broadcast) — one blocked
  admin account never prevents the others from being notified, but there's
  no retry/queue if delivery fails.
- **Category set is fixed in code** (`TICKET_CATEGORIES`), not admin-editable
  at runtime — same customization model as every other template's CUSTOMIZE
  block (Claude edits the source when generating a bot for a specific
  business, not a runtime settings screen).
