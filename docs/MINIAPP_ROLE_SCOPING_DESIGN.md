# Mini-app role-scoped resource filtering — frozen contract

Status: implemented, pilot template `team_manager`; extended with an
ownership-only shape, applied to `habit_tracker`. This is the FROZEN
`miniapp_config` schema addition — other sessions batch-generating
`miniapp_config` for the remaining templates should read this file directly
rather than re-deriving the shape.

## Auth-layer admin gate (a separate, lower layer — read this first)

`role_filter` (this whole doc) answers "which rows does an already-
authorized viewer see." It does NOT answer "is this viewer authorized to
use the mini-app at all" — that question sits one layer below, in
`runtime/miniapp_api.py`'s `_admin_gate_ok()` (docs/MINIAPP_AUTH_GATE_GAP.md
has the full incident writeup). The two layers compose as:

1. `_authenticate()` — is this a genuine Telegram identity? (HMAC-verified
   `initData` or magic-link token)
2. `_admin_gate_ok()` — is this identity entitled to use this resource at
   all? A resource with no `role_filter` requires bot-admin membership
   (`db.database.get_bot_admins()`); a resource WITH `role_filter` (either
   shape below) is exempt from this gate — its own row-scoping already
   answers the entitlement question, so re-checking admin membership on
   top would incorrectly lock out a legitimate non-admin viewer (e.g.
   `team_manager`'s worker role, or any future per-customer ownership
   shape).
3. `role_filter` (this doc) — for resources that passed step 2, which rows
   does this specific viewer see.

**Practical rule for anyone authoring a new `miniapp_config`:** declaring
`role_filter` on a resource is what opts it OUT of the blanket admin-only
gate. A resource meant for regular bot users (not just admins) — a
customer viewing their own bookings, an employee viewing their own tasks —
MUST declare `role_filter` (ownership-only shape at minimum, `{"where":
"<owner_column> = :telegram_user_id"}`) or it will 403 for everyone except
bot admins.

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

- **`tasks`**: `owner` → `where: "project_id IN (SELECT project_id FROM
  project_members WHERE user_id = :telegram_user_id AND role = 'owner')"`;
  `worker` → `where: "assigned_to = :telegram_user_id"`. Owner is scoped to
  the specific projects they own, never every task on the bot instance —
  see "Per-related-record scoping" below for why the flat `resolve` step
  doesn't need to change to express this.
- **`projects`**: same `role_filter.resolve` (`project_members`), but
  BOTH `owner` and `worker` rules use `where: "id IN (SELECT project_id
  FROM project_members WHERE user_id = :telegram_user_id)"` — a project
  admin should only see projects they are actually a member of, same as a
  worker; there's no "owner sees every project on the bot" concept in this
  schema.
- **`reports`**: mirrors `tasks` — `owner` → `where: "task_id IN (SELECT
  id FROM tasks WHERE project_id IN (SELECT project_id FROM
  project_members WHERE user_id = :telegram_user_id AND role =
  'owner'))"`; `worker` → `where: "task_id IN (SELECT id FROM tasks WHERE
  assigned_to = :telegram_user_id)"` (reports don't carry `assigned_to` or
  `project_id` directly, so both predicates join through `tasks`).
- **`attachments`**: same shape as `reports`, same nested-subquery
  predicates for both `owner` and `worker`.

## Per-related-record scoping (part of the general contract)

A role held "somewhere" (the flat set `resolve` returns) is not the same
claim as a role held **for the specific record being read**. The contract
does not need a distinct format for this — a rule's `where` is an
arbitrary predicate over the resource's own table, so scoping by a
related record's owning id (e.g. `project_id`) is just a subquery against
the same `resolve.table`, filtered to the matched role:

```
"where": "<fk_or_id_column> IN (SELECT <related_id> FROM <resolve.table>
           WHERE <resolve.identity_column> = :telegram_user_id
             AND <resolve.role_column> = '<this rule's role>')"
```

When the resource's own table doesn't carry the related id directly (e.g.
`reports`/`attachments` only have `task_id`, not `project_id`), nest an
additional `SELECT id FROM <bridge_table> WHERE <same subquery>` join, as
`tasks` mediates for `reports`/`attachments` above.

**Any future template with multiple independent teams/tenants sharing one
bot instance must use this pattern for every role whose `where` would
otherwise be `None` (unfiltered).** An unfiltered `where` for a role that
is held per-related-record (not globally, e.g. per-bot admin) is very
likely a cross-tenant data leak, not a legitimate "sees everything" role.
`where: None` should be reserved for roles that generically mean
"administers this entire bot instance," not "administers some subset of
records I'm a member of."

## Formerly: known limitation (fixed)

Earlier versions of `tasks`/`reports`/`attachments` used `where: None` for
`owner`, meaning any user who was `owner` in ANY project saw every task
across ALL projects on the bot instance — including projects run by
unrelated teams sharing the same bot. Confirmed as a real, frequent
scenario (multiple independent teams using one `team_manager` bot
simultaneously) and fixed by replacing `where: None` with the
per-related-record subqueries above. No `role_filter` engine or contract
change was needed — `_apply_role_filter` in `runtime/miniapp_api.py`
already executes arbitrary `where` predicates with nested subqueries (the
`projects` resource proves this); the bug was purely in what
`team_manager.py`'s config declared.

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
