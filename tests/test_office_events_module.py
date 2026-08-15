"""features/office_events.py — publish_event()'s delivery/isolation contract,
per docs/OFFICES_DESIGN.md variant A (fire-and-forget).

Uses a fake Registry (plain object with .get()) instead of a real
runtime.registry.Registry — publish_event() only ever calls .get(bot_id) on
it, so a minimal stand-in keeps these tests independent of aiogram Bot/
Dispatcher construction. db.database.get_office_subscribers is monkeypatched
directly rather than hitting a real/isolated sqlite file — this module's own
contract (delivery given a subscriber list) is what's under test here, not
the DB query itself (see tests/test_office_events_db.py for that).

Run with: python -m pytest tests/test_office_events_module.py
"""
from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import features.office_events as office_events
from features.office_events import OrderCreatedEvent, publish_event, register_office_event_hook


@pytest.fixture(autouse=True)
def reset_registry():
    office_events.set_registry(None)
    yield
    office_events.set_registry(None)


class FakeRegistry:
    def __init__(self, entries: dict[int, object]):
        self._entries = entries

    def get(self, bot_id: int):
        return self._entries.get(bot_id)


def _entry_with_hook(hook):
    config = {}
    register_office_event_hook(config, hook)
    return SimpleNamespace(config=config)


@pytest.mark.asyncio
async def test_publish_event_rejects_unknown_event_type():
    with pytest.raises(ValueError):
        await publish_event(1, "not.a.real.event", OrderCreatedEvent(1, 100, "RUB", 999))


@pytest.mark.asyncio
async def test_publish_event_rejects_mismatched_payload_type():
    @dataclass
    class WrongShape:
        foo: str

    with pytest.raises(ValueError):
        await publish_event(1, "order.created", WrongShape(foo="bar"))


@pytest.mark.asyncio
async def test_publish_event_with_no_live_registry_returns_zero(monkeypatch):
    monkeypatch.setattr(office_events, "get_office_subscribers", AsyncMock(return_value=[2]))
    delivered = await publish_event(1, "order.created", OrderCreatedEvent(1, 100, "RUB", 999))
    assert delivered == 0


@pytest.mark.asyncio
async def test_publish_event_with_no_subscribers_returns_zero(monkeypatch):
    monkeypatch.setattr(office_events, "get_office_subscribers", AsyncMock(return_value=[]))
    office_events.set_registry(FakeRegistry({}))
    delivered = await publish_event(1, "order.created", OrderCreatedEvent(1, 100, "RUB", 999))
    assert delivered == 0


@pytest.mark.asyncio
async def test_publish_event_delivers_to_subscriber_hook(monkeypatch):
    monkeypatch.setattr(office_events, "get_office_subscribers", AsyncMock(return_value=[2]))
    hook = AsyncMock()
    office_events.set_registry(FakeRegistry({2: _entry_with_hook(hook)}))

    payload = OrderCreatedEvent(order_id=42, amount=1500, currency="RUB", customer_chat_id=777)
    delivered = await publish_event(1, "order.created", payload)

    assert delivered == 1
    hook.assert_awaited_once()
    (event,), _ = hook.call_args
    assert event.event_type == "order.created"
    assert event.source_bot_id == 1
    assert event.payload is payload


@pytest.mark.asyncio
async def test_publish_event_skips_target_not_in_live_registry(monkeypatch):
    monkeypatch.setattr(office_events, "get_office_subscribers", AsyncMock(return_value=[2]))
    office_events.set_registry(FakeRegistry({}))  # bot 2 not registered (offline/deleted)
    delivered = await publish_event(1, "order.created", OrderCreatedEvent(1, 100, "RUB", 999))
    assert delivered == 0


@pytest.mark.asyncio
async def test_publish_event_skips_target_with_no_hook(monkeypatch):
    monkeypatch.setattr(office_events, "get_office_subscribers", AsyncMock(return_value=[2]))
    office_events.set_registry(FakeRegistry({2: SimpleNamespace(config={})}))
    delivered = await publish_event(1, "order.created", OrderCreatedEvent(1, 100, "RUB", 999))
    assert delivered == 0


@pytest.mark.asyncio
async def test_one_failing_subscriber_does_not_block_others(monkeypatch):
    monkeypatch.setattr(office_events, "get_office_subscribers", AsyncMock(return_value=[2, 3]))
    failing_hook = AsyncMock(side_effect=RuntimeError("boom"))
    ok_hook = AsyncMock()
    office_events.set_registry(
        FakeRegistry({2: _entry_with_hook(failing_hook), 3: _entry_with_hook(ok_hook)})
    )

    delivered = await publish_event(1, "order.created", OrderCreatedEvent(1, 100, "RUB", 999))

    assert delivered == 1
    ok_hook.assert_awaited_once()


@pytest.mark.asyncio
async def test_publish_event_never_raises_for_subscriber_hook_exception(monkeypatch):
    monkeypatch.setattr(office_events, "get_office_subscribers", AsyncMock(return_value=[2]))
    failing_hook = AsyncMock(side_effect=RuntimeError("boom"))
    office_events.set_registry(FakeRegistry({2: _entry_with_hook(failing_hook)}))

    # Must not propagate — a broken subscriber must never interrupt the publisher.
    delivered = await publish_event(1, "order.created", OrderCreatedEvent(1, 100, "RUB", 999))
    assert delivered == 0
