# Mini-app role-scoped resource filtering — frozen contract

Status: implemented, pilot template `team_manager`; extended with an
ownership-only shape, applied to `habit_tracker`. This is the FROZEN
`miniapp_config` schema addition — other sessions batch-generating
`miniapp_config` for the remaining templates should read this file directly
rather than re-deriving the shape.

**Two `role_filter` shapes exist — pick whichever matches the template's
actual data model, don't force one onto the other:**
1. Role-resolved (below) — a real roles table exists (e.g.
   `project_members`), multiple named roles, coarse allow/deny per role.
2. Ownership-only (see "Ownership-only filters" section) — no roles table
   at all, every row just has an owner column and every user only ever
   sees their own rows. This is likely the MORE COMMON shape for
   single-owner bots (each Telegram user is their own tenant, no
   admin/worker split) — check for this first before assuming a template
   needs the heavier role-resolved shape.

## Schema addition

A resource in `miniapp_config["resources"]` may carry an entirely optional
`role_filter` key. Absence of the key means exactly today's flat,
unfiltered behavior — byte-for-byte unchanged, pinned by a regression test
against `templates/course_tracker.py`'s real config.

```python
"resources": [{
    "name": "tasks", "table": "tasks", ...,   # unchanged
    "role_filter": {
        "resolve": {
            "table": "project_members",
            "identity_column": "user_id",
            "role_column": "role",
        },
        "rules": [
            {"role": "owner", "where": None},
            {"role": "worker", "where": "assigned_to = :telegram_user_id"},
        ],
        "default_deny": True,
    },
}]
```

This matches the locked contract exactly — no deviation was needed.

## Semantics

- **Role resolution**: `SELECT {role_column} FROM {resolve.table} WHERE
  {resolve.identity_column} = :telegram_user_id` — the full SET of roles
  the viewer holds (0, 1, or many rows; e.g. a `team_manager` user can be
  `owner` in one project and `worker` in another — resolution doesn't
  distinguish per-project, only "does this viewer hold this role
  anywhere").
- **Rule matching**: rules are evaluated top-to-bottom; the FIRST rule
  whose `role` is a member of the viewer's role set wins. This is a
  membership test, not a single scalar lookup.
- **`where: None`** → run the resource's existing query unfiltered, exactly
  as today.
- **`where: "<template>"`** → a named-placeholder predicate
  (`:telegram_user_id` is the only supported placeholder for the pilot)
  appended as `AND (<template>)` to the base query — for list endpoints
  (no existing WHERE) this becomes the sole WHERE clause; for get/single
  endpoints (existing `WHERE id = ?`) it is combined with `AND`. The
  placeholder is bound as a real parameter, never string-interpolated.
  Only whitelisted placeholder names are accepted; anything else is
  rejected with a clear error rather than executed.
- **`default_deny`**: if omitted, **defaults to `True`** — this is the
  safer default (fail closed) and is what the implementation does even
  when the key is left out of a hand-authored config. A viewer matching no
  declared role gets an empty list (`list_resource_handler`) or 403
  (`get_resource_handler` / `create_resource_handler`), never a silent
  fallback to the unfiltered query.
- **`create_resource_handler`**: minimal enforcement only — if the
  resource declares a `role_filter` and the matched rule's `where`
  references `:telegram_user_id` bound to a specific row-ownership column
  (detected by parsing the same placeholder template used for reads), the
  create payload's corresponding field is forced to the authenticated
  `telegram_user_id` if present in the payload, and rejected (400) if the
  client tries to set it to something else. This is deliberately minimal:
  it does not attempt general-purpose enforcement of arbitrary `where`
  shapes beyond the single-column-equality template this pilot supports.
  No template in the current 10-template list has a role-scoped +
  creatable resource, so this path has no live caller yet but exists so
  future configs don't have to add engine code to get it.

## Why `default_deny` defaults to `True`

The whole point of adding `role_filter` is to narrow the audience below
"any authenticated user of this bot." A default of `False` would mean a
config author who declares `resolve`/`rules` but forgets `default_deny`
silently gets the pre-role-filter unfiltered behavior for anyone outside
the declared roles — the exact bug this feature exists to prevent. Fail
closed.

## team_manager.py resource mapping

- **`tasks`**: `owner` → `where: None` (sees all tasks in the bot's DB —
  note the resolve table is NOT project-scoped by this filter, see
  Limitations below); `worker` → `where: "assigned_to =
  :telegram_user_id"`.
- **`projects`**: same `role_filter.resolve` (`project_members`), but
  BOTH `owner` and `worker` rules use `where: "id IN (SELECT project_id
  FROM project_members WHERE user_id = :telegram_user_id)"` — a project
  admin should only see projects they are actually a member of, same as a
  worker; there's no "owner sees every project on the bot" concept in this
  schema, unlike `tasks` where owner-role genuinely means "sees everything
  visible in the app."
- **`reports`**: mirrors `tasks` — `owner` → unfiltered; `worker` →
  `where: "task_id IN (SELECT id FROM tasks WHERE assigned_to =
  :telegram_user_id)"` (reports don't carry `assigned_to` directly, so the
  predicate joins through `tasks`).
- **`attachments`**: same shape as `reports`, same `task_id IN (SELECT id
  FROM tasks WHERE assigned_to = :telegram_user_id)` predicate for
  `worker`.

## Known limitation (documented, not fixed here)

`tasks`'s `owner` rule (`where: None`) sees every task across every
project on this bot instance, not just projects that person owns —
because `project_members` role resolution here is global-per-bot, not
project-scoped (the contract's `resolve` step returns a flat role set,
not `(project_id, role)` pairs). This matches the pilot's scope ("read-
scoping the existing flat CRUD resources by role", not a full
multi-tenant-within-a-bot redesign) and is an honest carry-over of the
same limitation the pre-existing flat `miniapp_config` had. A future
iteration could extend `resolve` to return scoped role sets, but that is
out of scope for this pilot and not implemented.

## Ownership-only filters (no roles table)

For templates where every user of the bot only ever owns/sees their own
rows — no separate admin/worker split, no roles table at all, just a
per-row owner column — `role_filter` can omit `resolve`/`rules` entirely:

```python
"role_filter": {"where": "owner_user_id = :telegram_user_id"}
```

- No role resolution happens at all; the `where` template applies
  unconditionally to every authenticated viewer.
- `RoleFilterDenied`/`default_deny` do not apply to this shape — there is
  no role to hold or lack, so a viewer who owns zero rows simply gets an
  empty (correctly scoped) result, never a 403.
- Same safe-predicate whitelist and `:telegram_user_id`-only placeholder
  binding as the role-resolved shape — no new engine trust boundary.
- Resources without a direct owner column (e.g. a child row referencing a
  parent that has one) scope through a subquery, same join-through pattern
  as `team_manager`'s `reports`/`attachments`:
  `"habit_id IN (SELECT id FROM habits WHERE owner_user_id = :telegram_user_id)"`.

**Applied to `habit_tracker.py`** (found by a peer session while reading
this contract — every user's habits/checkins were previously visible to
every other user of the same bot through `/app/{bot_id}`, a real privacy
leak in the pre-existing flat config):
- `habits`: `{"where": "owner_user_id = :telegram_user_id"}`.
- `habit_checkins`: `{"where": "habit_id IN (SELECT id FROM habits WHERE owner_user_id = :telegram_user_id)"}`
  (checkins have no owner column directly, scoped through the parent habit).

## Backward compatibility

The 9 other existing `miniapp_config` templates (`boss_bot`,
`course_tracker`, `event_manager`, `campaign_tracker`, `event_rsvp`,
`accountant`, `manager_bot`, `tour_operator`, `car_rental`) declare no
`role_filter` key anywhere and are unaffected — pinned by
`tests/test_miniapp_api.py`'s regression test against
`course_tracker.py`'s real config object.
