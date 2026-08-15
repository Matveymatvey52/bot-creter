"""db/database.py's bot_office_links CRUD — add/remove/get office links, and
delete_bot()'s cleanup of dangling links, per docs/OFFICES_DESIGN.md §3/§4.

Uses the isolated_db fixture (tests/conftest.py) so this never touches the
real data/bots.db — see MEMORY.md "Backlog: test DB isolation".

Run with: python -m pytest tests/test_office_events_db.py
"""
from __future__ import annotations

import pytest

from db.database import (
    add_office_link,
    create_bot_record_with_admins,
    delete_bot,
    get_office_links_for_bot,
    get_office_subscribers,
    init_db,
    remove_office_link,
)

FAKE_TOKEN = "123456:test-token-not-real"


async def _make_bots(n: int) -> list[int]:
    await init_db()
    ids = []
    for i in range(n):
        ids.append(
            await create_bot_record_with_admins(f"bot-{i}", "desc", FAKE_TOKEN, f"generated_bots/bot_{i}.py", [])
        )
    return ids


@pytest.mark.asyncio
async def test_no_subscribers_by_default(isolated_db):
    a, b = await _make_bots(2)
    assert await get_office_subscribers(a, "order.created") == []


@pytest.mark.asyncio
async def test_add_and_get_subscriber(isolated_db):
    a, b = await _make_bots(2)
    await add_office_link(a, b, "order.created")
    assert await get_office_subscribers(a, "order.created") == [b]


@pytest.mark.asyncio
async def test_multiple_subscribers_same_event(isolated_db):
    a, b, c = await _make_bots(3)
    await add_office_link(a, b, "order.created")
    await add_office_link(a, c, "order.created")
    subscribers = await get_office_subscribers(a, "order.created")
    assert set(subscribers) == {b, c}


@pytest.mark.asyncio
async def test_subscription_is_scoped_to_event_type(isolated_db):
    a, b = await _make_bots(2)
    await add_office_link(a, b, "order.created")
    assert await get_office_subscribers(a, "order.cancelled") == []


@pytest.mark.asyncio
async def test_subscription_is_scoped_to_source_bot(isolated_db):
    a, b, c = await _make_bots(3)
    await add_office_link(a, b, "order.created")
    # c publishing the same event_type must not pick up a's subscribers
    assert await get_office_subscribers(c, "order.created") == []


@pytest.mark.asyncio
async def test_add_office_link_is_idempotent(isolated_db):
    a, b = await _make_bots(2)
    await add_office_link(a, b, "order.created")
    await add_office_link(a, b, "order.created")  # must not raise, must not duplicate
    assert await get_office_subscribers(a, "order.created") == [b]


@pytest.mark.asyncio
async def test_remove_office_link(isolated_db):
    a, b = await _make_bots(2)
    await add_office_link(a, b, "order.created")
    await remove_office_link(a, b, "order.created")
    assert await get_office_subscribers(a, "order.created") == []


@pytest.mark.asyncio
async def test_remove_office_link_missing_is_a_noop(isolated_db):
    a, b = await _make_bots(2)
    await remove_office_link(a, b, "order.created")  # never existed — must not raise


@pytest.mark.asyncio
async def test_delete_bot_cleans_up_links_where_bot_is_source(isolated_db):
    a, b = await _make_bots(2)
    await add_office_link(a, b, "order.created")
    await delete_bot(a)
    assert await get_office_subscribers(a, "order.created") == []


@pytest.mark.asyncio
async def test_delete_bot_cleans_up_links_where_bot_is_target(isolated_db):
    a, b = await _make_bots(2)
    await add_office_link(a, b, "order.created")
    await delete_bot(b)
    assert await get_office_subscribers(a, "order.created") == []


@pytest.mark.asyncio
async def test_get_office_links_for_bot_empty_by_default(isolated_db):
    a, b = await _make_bots(2)
    assert await get_office_links_for_bot(a) == []


@pytest.mark.asyncio
async def test_get_office_links_for_bot_as_source(isolated_db):
    a, b = await _make_bots(2)
    await add_office_link(a, b, "order.created")
    links = await get_office_links_for_bot(a)
    assert links == [{"source_bot_id": a, "target_bot_id": b, "event_type": "order.created"}]


@pytest.mark.asyncio
async def test_get_office_links_for_bot_as_target(isolated_db):
    a, b = await _make_bots(2)
    await add_office_link(a, b, "order.created")
    links = await get_office_links_for_bot(b)
    assert links == [{"source_bot_id": a, "target_bot_id": b, "event_type": "order.created"}]


@pytest.mark.asyncio
async def test_get_office_links_for_bot_excludes_unrelated_links(isolated_db):
    a, b, c = await _make_bots(3)
    await add_office_link(a, b, "order.created")
    assert await get_office_links_for_bot(c) == []
