# Mini-app: missing admin/owner gate at the auth layer — known gap

Status: **known, not fixed here**. Found while batch-authoring `miniapp_config`
for templates/*.py (see git log — commits `feat: <template> — hardcoded
miniapp_config...`). A separate session is working the fix; this doc exists so
it isn't lost/rediscovered from scratch.

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

## Status at time of writing

Not fixed in this doc/commit. A separate session has been asked to address it
directly in `runtime/miniapp_api.py`. The 18 templates' `miniapp_config`
values are otherwise correct (schema-accurate, 1:1 with each template's
owner-facing Telegram data) and do not need re-authoring once the auth gate
is fixed — they'll simply start being properly gated.
