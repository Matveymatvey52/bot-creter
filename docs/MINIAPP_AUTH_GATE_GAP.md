# Mini-app: missing admin/owner gate at the auth layer — FIXED

Status: **fixed**, `runtime/miniapp_api.py`'s `_admin_gate_ok()`. Found while
batch-authoring `miniapp_config` for templates/*.py (see git log — commits
`feat: <template> — hardcoded miniapp_config...`). This doc is kept as the
record of the gap and the fix, since docs/MINIAPP_ROLE_SCOPING_DESIGN.md
covers the row-scoping half only.

## The gap

`runtime/miniapp_api.py`'s `_authenticate()` verifies that a request carries a
*genuine* Telegram identity (signed WebApp `initData`, or a valid HMAC magic-
link token) — but it does **not** check whether that identity is authorized
to use this bot's mini-app at all. Every `/api/{bot_id}/<resource>` list/
detail/create handler treats "authenticated" as "authorized."

Separately, `runtime/webhook_setup.py`'s `set_miniapp_menu_button()` is called
automatically at bot-creation time (`handlers/create_bot.py`, also
`handlers/manage_bots.py`/`handlers/custom_features.py` on reconfigure) and
sets the bot's Telegram **Menu Button** — via `setChatMenuButton` with no
`chat_id`, i.e. the bot-wide default for every private chat — to open
`/app/{bot_id}` as a Telegram Mini App. This is not admin-only UI: **any user
who has ever messaged the bot gets a menu button that opens the mini-app**,
and opening it as a real Telegram Mini App hands the frontend genuine, validly
signed `initData` for that user — no forged token needed.

## Combined effect

For every one of the 18 first-half templates now carrying a (flat, no
`role_filter`) `miniapp_config` — `accountant`, `booking_beauty`,
`booking_fitness`, `booking_medical`, `booking_restaurant`, `boss_bot`,
`campaign_tracker`, `car_rental`, `channel_aggregator`, `course_tracker`,
`coworking_space`, `debtors`, `delivery_tracker`, `event_manager`,
`event_rsvp`, `expense_tracker`, `feedback_survey`, `habit_tracker` — **any
regular bot user (client/customer/employee, not just the bot's admin) can
open the Menu Button and see every other user's rows** for every resource the
config declares: every booking, every client's phone number, every debtor's
balance, every other user's private habit list, etc. In the Telegram bot
itself this same data is correctly gated behind each template's own
`_is_admin()`/`_is_bot_admin()` check (an `admins.json` file per bot, outside
the SQL database) — the mini-app has no equivalent gate.

`habit_tracker.py` is the sharpest example: its whole design is "each Telegram
user's habit list is private to them," yet its `miniapp_config` (as currently
authored) exposes literally every user's habits/checkins to literally every
other user of that bot via the mini-app.

## Why `role_filter` (docs/MINIAPP_ROLE_SCOPING_DESIGN.md) doesn't close this

`role_filter` is a narrower, complementary mechanism: it scopes a resource's
*rows* once a viewer is already established as legitimately allowed to use
the mini-app (e.g. `team_manager`'s owner-sees-all vs worker-sees-own-tasks).
It requires `resolve.table` to be a real SQL table in the bot's own database
mapping `identity_column → role_column`. None of these 18 templates have such
a table — their only privilege signal is `admins.json` membership, which
lives outside the bot's SQLite database entirely and `role_filter.resolve`
has no way to query.

Fixing this gap needs an auth-layer change (e.g. `_resolve_entry_and_config`/
`_authenticate` consulting `db.database.get_bot_admins()` or an equivalent
per-bot admin check, analogous to how `analytics_handler`/`export_handler`/
`office_tasks_handler` already gate themselves in this same file), not a
per-template `miniapp_config` change — `role_filter` operates strictly after
that gate, not instead of it.

## Fix

`runtime/miniapp_api.py`'s `_admin_gate_ok(bot_id, resource, telegram_user_id)`
is called by `list_resource_handler`, `get_resource_handler`, and
`create_resource_handler` right after resource lookup, before any row is
read or written. It is a per-resource gate, not a per-request one: a
resource with **no** `role_filter` key requires the viewer to be listed in
`db.database.get_bot_admins(bot_id)` (the same factory-level admin list
each template's own `admins.json` is synced from — see
`sync_bot_admins_json`), else the handler returns 403. A resource that
**does** declare `role_filter` (either shape — role-resolved or
ownership-only, docs/MINIAPP_ROLE_SCOPING_DESIGN.md) skips this gate
entirely: that mechanism already scopes rows to what the viewer is
entitled to see, so it fully subsumes the admin-only gate for that
resource — a customer viewing their own booking, or `team_manager`'s
worker viewing only their assigned tasks, is exactly what `role_filter`
exists to allow. `_admin_gate_ok` never re-checks resources that already
opted into `role_filter`.

The 18 templates named above needed **zero** `miniapp_config` changes —
their configs were already schema-accurate, 1:1 with each template's
owner-facing Telegram data; they simply started being properly gated once
`_admin_gate_ok` shipped. `team_manager` and `habit_tracker` (the two
templates with `role_filter`) are unaffected by this gate — their rows
were already scoped per-viewer and remain so.

## Status at time of writing

Fixed. See docs/MINIAPP_ROLE_SCOPING_DESIGN.md's "Auth-layer admin gate"
section for how this interacts with `role_filter`.
