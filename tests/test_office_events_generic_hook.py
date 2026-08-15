"""features/office_events.py's generic_on_office_event() — the universal,
data-driven fallback for template-based bots with no hand-written
on_office_event, per docs/OFFICES_DESIGN.md §11.

Calls generic_on_office_event() directly against a real per-test sqlite
db_path (same isolation style as tests/test_event_rsvp_office_events.py) —
it's a plain async function, not a router handler.

Run with: python -m pytest tests/test_office_events_generic_hook.py
"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import aiosqlite
import pytest

from features.office_events import OfficeEvent, OrderCreatedEvent, generic_on_office_event

CUSTOMER_WITH_MATCH = 111
CUSTOMER_WITHOUT_MATCH = 222


@pytest.fixture
def db_path():
    with tempfile.TemporaryDirectory() as tmp:
        yield str(Path(tmp) / "fixture.db")


def _order_created_event(customer_chat_id: int | None, source_bot_id: int = 7) -> OfficeEvent:
    payload = (
        OrderCreatedEvent(order_id=1, amount=5000, currency="RUB", customer_chat_id=customer_chat_id)
        if customer_chat_id is not None
        else None
    )
    return OfficeEvent(event_type="order.created", source_bot_id=source_bot_id, payload=payload)


def _office_notes(db_path: str) -> list[tuple]:
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT source_bot_id, event_type, note FROM office_notes").fetchall()
    conn.close()
    return rows


async def _make_table(db_path: str, table: str, column: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY, "{column}" INTEGER)')
        await db.commit()


async def _insert_row(db_path: str, table: str, column: str, value: int) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(f'INSERT INTO "{table}" ("{column}") VALUES (?)', (value,))
        await db.commit()


@pytest.mark.asyncio
async def test_no_hook_config_still_records_fallback_note(db_path):
    await generic_on_office_event(_order_created_event(CUSTOMER_WITH_MATCH), db_path, None)

    notes = _office_notes(db_path)
    assert len(notes) == 1
    assert notes[0][0] == 7
    assert notes[0][1] == "order.created"
    assert str(CUSTOMER_WITH_MATCH) in notes[0][2]


@pytest.mark.asyncio
async def test_creates_office_notes_table_if_missing(db_path):
    # db_path has no tables at all yet — generic_on_office_event must create
    # office_notes itself, same as templates/event_rsvp.py's init_db() does
    # for its own hand-written hook.
    await generic_on_office_event(_order_created_event(CUSTOMER_WITH_MATCH), db_path, None)
    assert len(_office_notes(db_path)) == 1


@pytest.mark.asyncio
async def test_matches_customer_against_configured_table_and_field(db_path):
    await _make_table(db_path, "clients", "telegram_id")
    await _insert_row(db_path, "clients", "telegram_id", CUSTOMER_WITH_MATCH)

    await generic_on_office_event(
        _order_created_event(CUSTOMER_WITH_MATCH),
        db_path,
        {"table": "clients", "match_field": "telegram_id"},
    )

    notes = _office_notes(db_path)
    assert len(notes) == 1
    assert "clients" in notes[0][2]
    assert "telegram_id" in notes[0][2]


@pytest.mark.asyncio
async def test_no_match_found_still_records_plain_note_without_match_text(db_path):
    await _make_table(db_path, "clients", "telegram_id")
    # no matching row inserted

    await generic_on_office_event(
        _order_created_event(CUSTOMER_WITHOUT_MATCH),
        db_path,
        {"table": "clients", "match_field": "telegram_id"},
    )

    notes = _office_notes(db_path)
    assert len(notes) == 1
    assert "Найдено совпадение" not in notes[0][2]


@pytest.mark.asyncio
async def test_hallucinated_table_degrades_to_plain_note_not_a_crash(db_path):
    await generic_on_office_event(
        _order_created_event(CUSTOMER_WITH_MATCH),
        db_path,
        {"table": "table_that_does_not_exist", "match_field": "telegram_id"},
    )

    notes = _office_notes(db_path)
    assert len(notes) == 1
    assert "Найдено совпадение" not in notes[0][2]


@pytest.mark.asyncio
async def test_hallucinated_column_degrades_to_plain_note_not_a_crash(db_path):
    await _make_table(db_path, "clients", "telegram_id")

    await generic_on_office_event(
        _order_created_event(CUSTOMER_WITH_MATCH),
        db_path,
        {"table": "clients", "match_field": "column_that_does_not_exist"},
    )

    notes = _office_notes(db_path)
    assert len(notes) == 1
    assert "Найдено совпадение" not in notes[0][2]


@pytest.mark.asyncio
async def test_sql_injection_attempt_in_table_name_is_rejected(db_path):
    # A hallucinated/malicious table value containing SQL syntax must never
    # reach an f-string SQL statement — _IDENTIFIER_RE rejects it outright,
    # degrading to a plain fallback note instead of executing anything.
    await generic_on_office_event(
        _order_created_event(CUSTOMER_WITH_MATCH),
        db_path,
        {"table": "clients; DROP TABLE office_notes; --", "match_field": "telegram_id"},
    )

    notes = _office_notes(db_path)
    assert len(notes) == 1  # office_notes must still exist and be queryable


@pytest.mark.asyncio
async def test_reserved_word_table_and_column_names_still_match_and_still_record_note(db_path):
    # "order" and "group" are valid SQLite column/table identifiers but SQL
    # reserved words — bare (unquoted) use in a query is a SyntaxError. Both
    # table and match_field are quoted internally specifically so a real
    # bot whose generated schema uses names like these (plausible for
    # orders_tracker/shop_catalog templates) still gets a working match AND
    # still gets its tier-1 fallback note even if the match query fails.
    await _make_table(db_path, "order", "group")
    await _insert_row(db_path, "order", "group", CUSTOMER_WITH_MATCH)

    await generic_on_office_event(
        _order_created_event(CUSTOMER_WITH_MATCH),
        db_path,
        {"table": "order", "match_field": "group"},
    )

    notes = _office_notes(db_path)
    assert len(notes) == 1
    assert "Найдено совпадение" in notes[0][2]


@pytest.mark.asyncio
async def test_missing_customer_chat_id_skips_match_attempt(db_path):
    await _make_table(db_path, "clients", "telegram_id")

    event = OfficeEvent(event_type="order.created", source_bot_id=7, payload=None)
    await generic_on_office_event(event, db_path, {"table": "clients", "match_field": "telegram_id"})

    notes = _office_notes(db_path)
    assert len(notes) == 1


@pytest.mark.asyncio
async def test_never_raises_on_unwritable_db_path():
    # A path in a directory that doesn't exist — aiosqlite.connect will fail.
    await generic_on_office_event(
        _order_created_event(CUSTOMER_WITH_MATCH), "/nonexistent-dir-xyz/fixture.db", None
    )
