# FEATURE: cashflow_ledger
# COMPATIBLE_WITH: accountant, property_rental, tour_operator
"""Reusable cash-flow ledger (ДДС) feature module — extracted from
templates/tour_operator.py's `dds` table/handlers (see cashflow-ledger-inventory
for the original code this replaces). Records money in/out with an optional
multi-currency breakdown and gives callers running totals — no UI, no aiogram
router, no HTTP endpoints of its own; same "plain data layer" shape as
features/sheets.py, since every host template's own section/keyboard/REST
wiring is too UI-specific to generalize.

Unlike features/payments.py (bot-wide, one ledger per bot) this ledger is
scoped by an OPTIONAL `parent_id` — tour_operator passes tour_id so each tour
gets its own ДДС; a bot with no natural sub-entity (e.g. accountant, tracking
one shared cash flow for the whole business) just leaves it None. parent_id is
an opaque TEXT column, never a foreign key: this module doesn't know what
table (if any) the caller's IDs come from, so it can't declare or enforce a
REFERENCES constraint — that stays the host template's job if it wants one.

Entries are immutable in this module's own API — there is no update/void
helper here, only record_entry() (insert) and delete_entry() (hard delete, for
a host's own "✏️/🗑️" CRUD UI). Callers building a REST CRUD layer on top (as
tour_operator's `_make_crud` did) query/write the `cashflow_entries` table
directly; that's expected and fine, same as sellable_items reads from
payments' own `payments` table read-only.
"""
from __future__ import annotations

from typing import Protocol

import aiosqlite


class CashflowConfig(Protocol):
    """Any template Config dataclass with a db_path works — duck-typed so this
    module doesn't need to know about any specific template's Config shape."""
    db_path: str


async def init_cashflow_tables(db_path: str) -> None:
    """Adds the cashflow_entries table to a template's OWN per-bot db_path.
    Call this from the template's own init_db(), alongside its other CREATE
    TABLEs — this module never opens/owns a database file by itself."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS cashflow_entries (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                parent_id    TEXT,
                date         TEXT,
                amount_rub   REAL DEFAULT 0,
                amount_usd   REAL DEFAULT 0,
                amount_idr   REAL DEFAULT 0,
                description  TEXT,
                entity       TEXT,
                type         TEXT NOT NULL DEFAULT 'out' CHECK(type IN ('in','out')),
                created_at   TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.commit()


init_db = init_cashflow_tables


async def record_entry(
    db_path: str,
    *,
    parent_id: str | int | None = None,
    date: str | None = None,
    amount_rub: float = 0,
    amount_usd: float = 0,
    amount_idr: float = 0,
    description: str | None = None,
    entity: str | None = None,
    type: str = "out",
) -> int:
    """Inserts one ДДС entry, returns its id. parent_id is stored as TEXT
    (str() of whatever the caller passes) since this module treats it as an
    opaque scoping key, not a typed foreign key — see module docstring."""
    if type not in ("in", "out"):
        raise ValueError(f"cashflow_ledger.record_entry: type must be 'in' or 'out', got {type!r}")
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout=5000")
        cur = await db.execute(
            "INSERT INTO cashflow_entries(parent_id,date,amount_rub,amount_usd,amount_idr,description,entity,type) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (None if parent_id is None else str(parent_id), date, amount_rub, amount_usd,
             amount_idr, description, entity, type),
        )
        await db.commit()
        return cur.lastrowid


async def delete_entry(db_path: str, entry_id: int) -> bool:
    """Hard-deletes one entry. Returns True if a row was actually removed."""
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute("DELETE FROM cashflow_entries WHERE id=?", (entry_id,))
        await db.commit()
        return cur.rowcount > 0


async def list_entries(db_path: str, parent_id: str | int | None = None) -> list[dict]:
    """Returns entries newest-first, scoped to parent_id (pass the same value
    used in record_entry() to get that entity's own entries; None means the
    bot-wide/no-parent entries)."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        if parent_id is None:
            async with db.execute(
                "SELECT * FROM cashflow_entries WHERE parent_id IS NULL ORDER BY date DESC, id DESC"
            ) as c:
                rows = await c.fetchall()
        else:
            async with db.execute(
                "SELECT * FROM cashflow_entries WHERE parent_id=? ORDER BY date DESC, id DESC",
                (str(parent_id),),
            ) as c:
                rows = await c.fetchall()
        return [dict(r) for r in rows]


async def get_totals(db_path: str, parent_id: str | int | None = None) -> dict:
    """Returns {"in": {"rub":.., "usd":.., "idr":..}, "out": {...}, "balance_rub":..}
    — balance_rub is in - out on the rub column only, matching tour_operator's
    original ДДС balance line (rub is the primary currency; usd/idr are
    informational conversions, not summed together)."""
    scope_sql = "parent_id IS NULL" if parent_id is None else "parent_id=?"
    params = () if parent_id is None else (str(parent_id),)
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT type, COALESCE(SUM(amount_rub),0) rub, COALESCE(SUM(amount_usd),0) usd, "
            f"COALESCE(SUM(amount_idr),0) idr FROM cashflow_entries WHERE {scope_sql} GROUP BY type",
            params,
        ) as c:
            rows = {r["type"]: {"rub": r["rub"], "usd": r["usd"], "idr": r["idr"]} for r in await c.fetchall()}
    inflow = rows.get("in", {"rub": 0, "usd": 0, "idr": 0})
    outflow = rows.get("out", {"rub": 0, "usd": 0, "idr": 0})
    return {"in": inflow, "out": outflow, "balance_rub": inflow["rub"] - outflow["rub"]}
