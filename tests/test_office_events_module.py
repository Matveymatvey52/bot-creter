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


# ── showcase-group digest (docs discussion "Офисы — доработка" §5) ───────────
# The Telegram showcase group is explicitly NOT the transport — every test
# above already proves delivery to real subscribers works with no showcase
# group configured at all (db.database.get_factory_showcase_group is not
# monkeypatched there, so the real function runs against no DB row / whatever
# table state exists, and _post_showcase_digest's own try/except swallows any
# failure). These tests isolate _post_showcase_digest's own behavior instead.

from runtime.registry import FACTORY_BOT_ID


def _factory_entry_with_bot(bot):
    return SimpleNamespace(bot=bot)


@pytest.mark.asyncio
async def test_publish_event_posts_digest_to_showcase_group_when_configured(monkeypatch):
    monkeypatch.setattr(office_events, "get_office_subscribers", AsyncMock(return_value=[2]))
    monkeypatch.setattr(
        "db.database.get_factory_showcase_group",
        AsyncMock(return_value={"chat_id": -100123, "enabled": True}),
    )
    monkeypatch.setattr(
        "db.database.get_bot", AsyncMock(return_value={"id": 1, "name": "Магазин Ромы"})
    )
    factory_bot = SimpleNamespace(send_message=AsyncMock())
    office_events.set_registry(
        FakeRegistry({2: _entry_with_hook(AsyncMock()), FACTORY_BOT_ID: _factory_entry_with_bot(factory_bot)})
    )

    await publish_event(1, "order.created", OrderCreatedEvent(1, 100, "RUB", 999))

    factory_bot.send_message.assert_awaited_once()
    (chat_id, text), kwargs = factory_bot.send_message.call_args
    assert chat_id == -100123
    assert "Магазин Ромы" in text
    assert kwargs.get("parse_mode") == "HTML"


@pytest.mark.asyncio
async def test_publish_event_digest_escapes_bot_name_html_metacharacters(monkeypatch):
    """A bot name is owner/LLM-controlled free text — must be HTML-escaped
    before interpolation into this parse_mode='HTML' message, or a stray
    '<'/'&' would make Telegram reject the send (see the fix's own comment
    in features/office_events.py's _post_showcase_digest)."""
    monkeypatch.setattr(office_events, "get_office_subscribers", AsyncMock(return_value=[2]))
    monkeypatch.setattr(
        "db.database.get_factory_showcase_group",
        AsyncMock(return_value={"chat_id": -100123, "enabled": True}),
    )
    monkeypatch.setattr(
        "db.database.get_bot", AsyncMock(return_value={"id": 1, "name": "<b>Рома</b> & Co"})
    )
    factory_bot = SimpleNamespace(send_message=AsyncMock())
    office_events.set_registry(
        FakeRegistry({2: _entry_with_hook(AsyncMock()), FACTORY_BOT_ID: _factory_entry_with_bot(factory_bot)})
    )

    await publish_event(1, "order.created", OrderCreatedEvent(1, 100, "RUB", 999))

    factory_bot.send_message.assert_awaited_once()
    (_, text), _ = factory_bot.send_message.call_args
    assert "<b>Рома</b> & Co" not in text
    assert "&lt;b&gt;Рома&lt;/b&gt; &amp; Co" in text


@pytest.mark.asyncio
async def test_publish_event_skips_digest_when_showcase_not_configured(monkeypatch):
    monkeypatch.setattr(office_events, "get_office_subscribers", AsyncMock(return_value=[2]))
    monkeypatch.setattr("db.database.get_factory_showcase_group", AsyncMock(return_value=None))
    factory_bot = SimpleNamespace(send_message=AsyncMock())
    office_events.set_registry(
        FakeRegistry({2: _entry_with_hook(AsyncMock()), FACTORY_BOT_ID: _factory_entry_with_bot(factory_bot)})
    )

    await publish_event(1, "order.created", OrderCreatedEvent(1, 100, "RUB", 999))

    factory_bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_publish_event_skips_digest_when_showcase_disabled(monkeypatch):
    monkeypatch.setattr(office_events, "get_office_subscribers", AsyncMock(return_value=[2]))
    monkeypatch.setattr(
        "db.database.get_factory_showcase_group",
        AsyncMock(return_value={"chat_id": -100123, "enabled": False}),
    )
    factory_bot = SimpleNamespace(send_message=AsyncMock())
    office_events.set_registry(
        FakeRegistry({2: _entry_with_hook(AsyncMock()), FACTORY_BOT_ID: _factory_entry_with_bot(factory_bot)})
    )

    await publish_event(1, "order.created", OrderCreatedEvent(1, 100, "RUB", 999))

    factory_bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_publish_event_digest_failure_does_not_affect_real_delivery(monkeypatch):
    """A broken showcase mirror (Telegram error, bot kicked, etc.) must never
    affect real subscriber delivery — same isolation contract as a failing
    subscriber hook (test_one_failing_subscriber_does_not_block_others)."""
    monkeypatch.setattr(office_events, "get_office_subscribers", AsyncMock(return_value=[2]))
    monkeypatch.setattr(
        "db.database.get_factory_showcase_group",
        AsyncMock(return_value={"chat_id": -100123, "enabled": True}),
    )
    monkeypatch.setattr("db.database.get_bot", AsyncMock(return_value={"id": 1, "name": "Магазин Ромы"}))
    factory_bot = SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError("kicked from group")))
    real_hook = AsyncMock()
    office_events.set_registry(
        FakeRegistry({2: _entry_with_hook(real_hook), FACTORY_BOT_ID: _factory_entry_with_bot(factory_bot)})
    )

    delivered = await publish_event(1, "order.created", OrderCreatedEvent(1, 100, "RUB", 999))

    assert delivered == 1
    real_hook.assert_awaited_once()


@pytest.mark.asyncio
async def test_publish_event_skips_digest_when_factory_bot_not_in_registry(monkeypatch):
    monkeypatch.setattr(office_events, "get_office_subscribers", AsyncMock(return_value=[2]))
    monkeypatch.setattr(
        "db.database.get_factory_showcase_group",
        AsyncMock(return_value={"chat_id": -100123, "enabled": True}),
    )
    # FACTORY_BOT_ID deliberately absent from the registry.
    office_events.set_registry(FakeRegistry({2: _entry_with_hook(AsyncMock())}))

    # Must not raise even though the factory bot instance is unavailable.
    delivered = await publish_event(1, "order.created", OrderCreatedEvent(1, 100, "RUB", 999))
    assert delivered == 1


# ── available_event_types_for_template ────────────────────────────────────
from features.office_events import available_event_types_for_template


def test_available_event_types_none_for_from_scratch_bot():
    assert available_event_types_for_template(None) == []


def test_available_event_types_order_created_for_payments_compatible_template():
    # shop_catalog IS in features/payments.py's own COMPATIBLE_WITH header.
    assert available_event_types_for_template("shop_catalog") == ["order.created"]


def test_available_event_types_task_assigned_for_boss_bot():
    assert available_event_types_for_template("boss_bot") == ["task.assigned"]


def test_available_event_types_empty_for_template_with_no_publisher():
    # tour_operator is compatible with office_events (can RECEIVE events) but
    # is not in payments' COMPATIBLE_WITH list and is not boss_bot, so it
    # publishes nothing.
    assert available_event_types_for_template("tour_operator") == []
