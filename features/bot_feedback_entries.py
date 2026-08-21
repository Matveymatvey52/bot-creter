# FEATURE: bot_feedback_entries
# COMPATIBLE_WITH: tour_operator
"""Reusable customer-review feature module — same "plain data layer, no UI,
no router" shape as features/cashflow_ledger.py. Not to be confused with the
central bot_feedback table (db/database.py) that lets the OWNER anonymously
rate a bot from the factory dashboard: this module is for a bot's own END
CUSTOMERS leaving a review, signed with their Telegram identity, through that
bot's mini-app.

No submit-review HTTP handler lives here or in runtime/miniapp_api.py — the
generic create_resource_handler already does the whole job: it authenticates
the caller via Telegram initData (runtime/miniapp_api._authenticate),
resolves FEEDBACK_RESOURCE's "role_filter" as an ownership-only where-clause
(runtime/miniapp_api._resolve_create_owner_column), and force-sets
telegram_user_id to the authenticated caller on insert — a customer can never
submit a review as someone else. A host template just adds FEEDBACK_RESOURCE
to its own miniapp_config["resources"] (see templates/tour_operator.py) to
get POST/GET, and the owner's existing per-bot record editor
(runtime/factory_analytics_api.py's resource_update_handler/
resource_delete_handler, added for the Track A support-session tool) can
already PATCH/DELETE any resource's rows including this one — no new code
needed there either.
"""
from __future__ import annotations

import aiosqlite


async def init_feedback_table(db_path: str) -> None:
    """Adds the bot_feedback_entries table to a template's OWN per-bot
    db_path. Call this from the template's own init_db(), alongside its
    other CREATE TABLEs — this module never opens/owns a database file by
    itself, same convention as cashflow_ledger.init_cashflow_tables."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bot_feedback_entries (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_user_id INTEGER NOT NULL,
                author_name      TEXT,
                rating           INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
                comment          TEXT,
                created_at       TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.commit()


init_db = init_feedback_table


async def list_feedback(db_path: str) -> list[dict]:
    """Returns every review, newest first. Callers wanting one customer's own
    reviews should filter the result by telegram_user_id — mirrors
    cashflow_ledger.list_entries in staying a thin read, not a scoped query
    builder, since the mini-app's own list_resource_handler is what actually
    enforces per-viewer scoping via FEEDBACK_RESOURCE's role_filter."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM bot_feedback_entries ORDER BY id DESC"
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def delete_feedback(db_path: str, entry_id: int) -> bool:
    """Hard-deletes one review. Returns True if a row was actually removed."""
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("DELETE FROM bot_feedback_entries WHERE id = ?", (entry_id,))
        await db.commit()
        return cursor.rowcount > 0


# ── ready-made miniapp_config resource (docs/MINIAPP_DESIGN.md) ────────────
# A host template adds this dict as-is to its own miniapp_config["resources"]
# list (see templates/tour_operator.py) rather than hand-writing field specs
# per template, the way tour_operator's own "dds" resource wraps
# cashflow_ledger's cashflow_entries table. role_filter is the
# "ownership-only" shape (docs/MINIAPP_ROLE_SCOPING_DESIGN.md) — every
# customer sees and can create only their OWN reviews through the generic
# mini-app; the owner's separate support-session record editor
# (factory_analytics_api.py) bypasses role_filter entirely and can
# PATCH/DELETE any row, same as every other resource there.
FEEDBACK_RESOURCE: dict = {
    "name": "feedback",
    "table": "bot_feedback_entries",
    "order_by": "id DESC",
    "creatable": True,
    # Новый контракт: каждый ресурс обязан объявлять scope. Отзывы не
    # привязаны к конкретной сущности-родителю (туру, заказу) — они про
    # бота целиком, поэтому global.
    "scope": {"type": "global"},
    "title": "Отзывы",
    "titleField": "comment",
    "fields": [
        {"name": "telegram_user_id", "label": "Telegram ID", "kind": "number", "list": False, "detail": True, "create": False},
        {"name": "author_name", "label": "Имя", "kind": "text", "list": True, "detail": True, "create": True},
        {"name": "rating", "required": True, "label": "Оценка", "kind": "number", "list": True, "detail": True, "create": True},
        {"name": "comment", "label": "Комментарий", "kind": "text", "list": False, "detail": True, "create": True},
        {"name": "created_at", "creatable": False, "label": "Создано", "kind": "date", "list": False, "detail": False, "create": False},
    ],
    "role_filter": {"where": "telegram_user_id = :telegram_user_id"},
}
