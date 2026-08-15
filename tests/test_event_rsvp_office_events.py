"""templates/event_rsvp.py's on_office_event() — the first real office_events
subscriber, per docs/OFFICES_DESIGN.md §8 Вариант 1 (tour_operator → event_rsvp).

Calls on_office_event() directly (it's a plain async function, not a router
handler — registry.py wraps it into a hook, but the function itself needs no
Dispatcher/Bot) with a real per-test sqlite db_path, same isolation style as
tests/test_event_rsvp.py.

Run with: python -m pytest tests/test_event_rsvp_office_events.py
"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import aiosqlite
import pytest

from features.office_events import OfficeEvent, OrderCreatedEvent
from templates import event_rsvp

CLIENT_WITH_RSVP = 111
CLIENT_WITHOUT_RSVP = 222


@pytest.fixture
def db_path():
    with tempfile.TemporaryDirectory() as tmp:
        yield str(Path(tmp) / "fixture.db")


def _config(db_path: str) -> event_rsvp.EventRsvpConfig:
    return event_rsvp.EventRsvpConfig(
        bot_name="test-event-rsvp",
        db_path=db_path,
        admins_file=Path(db_path).with_suffix(".admins.json"),
        welcome_image=Path(db_path).with_suffix(".jpg"),
        bot_id=42,
    )


async def _add_rsvp(db_path: str, client_user_id: int, status: str = "confirmed") -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO rsvps (client_user_id, client_name, guests_count, status) VALUES (?, ?, 1, ?)",
            (client_user_id, "Test Guest", status),
        )
        await db.commit()


def _order_created_event(customer_chat_id: int, source_bot_id: int = 7) -> OfficeEvent:
    return OfficeEvent(
        event_type="order.created",
        source_bot_id=source_bot_id,
        payload=OrderCreatedEvent(order_id=1, amount=5000, currency="RUB", customer_chat_id=customer_chat_id),
    )


def _office_notes(db_path: str) -> list[tuple]:
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT source_bot_id, event_type, note FROM office_notes").fetchall()
    conn.close()
    return rows


@pytest.mark.asyncio
async def test_records_note_when_customer_has_confirmed_rsvp(db_path):
    await event_rsvp.init_db(db_path)
    await _add_rsvp(db_path, CLIENT_WITH_RSVP, status="confirmed")

    await event_rsvp.on_office_event(_order_created_event(CLIENT_WITH_RSVP, source_bot_id=7), _config(db_path))

    notes = _office_notes(db_path)
    assert len(notes) == 1
    assert notes[0][0] == 7
    assert notes[0][1] == "order.created"
    assert str(CLIENT_WITH_RSVP) in notes[0][2]


@pytest.mark.asyncio
async def test_records_note_when_customer_has_waitlist_rsvp(db_path):
    await event_rsvp.init_db(db_path)
    await _add_rsvp(db_path, CLIENT_WITH_RSVP, status="waitlist")

    await event_rsvp.on_office_event(_order_created_event(CLIENT_WITH_RSVP), _config(db_path))

    assert len(_office_notes(db_path)) == 1


@pytest.mark.asyncio
async def test_no_note_when_customer_has_no_rsvp(db_path):
    await event_rsvp.init_db(db_path)

    await event_rsvp.on_office_event(_order_created_event(CLIENT_WITHOUT_RSVP), _config(db_path))

    assert _office_notes(db_path) == []


@pytest.mark.asyncio
async def test_no_note_when_customers_only_rsvp_is_cancelled(db_path):
    await event_rsvp.init_db(db_path)
    await _add_rsvp(db_path, CLIENT_WITH_RSVP, status="cancelled")

    await event_rsvp.on_office_event(_order_created_event(CLIENT_WITH_RSVP), _config(db_path))

    assert _office_notes(db_path) == []


@pytest.mark.asyncio
async def test_ignores_non_order_created_event_type(db_path):
    await event_rsvp.init_db(db_path)
    await _add_rsvp(db_path, CLIENT_WITH_RSVP, status="confirmed")
    event = OfficeEvent(event_type="some.other.type", source_bot_id=7, payload=None)

    await event_rsvp.on_office_event(event, _config(db_path))

    assert _office_notes(db_path) == []


@pytest.mark.asyncio
async def test_never_raises_on_db_error(db_path):
    # db_path never initialized (no rsvps/office_notes tables) — must be
    # swallowed and logged, not propagated, same isolation guarantee
    # features/office_events.py's publish_event() already provides one layer
    # up, kept defensive here too per this function's own docstring.
    await event_rsvp.on_office_event(_order_created_event(CLIENT_WITH_RSVP), _config(db_path))
