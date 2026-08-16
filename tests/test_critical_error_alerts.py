"""features/office_events.py — report_critical_error()'s delivery, rate-limit,
sanitization, and graceful-degradation contract, per
docs/CRITICAL_ALERTS_DESIGN.md.

Same fake-Registry pattern as tests/test_office_events_module.py: a plain
object with .get() stands in for runtime.registry.Registry so these tests
don't need real aiogram Bot/Dispatcher construction. handlers.admin_manager's
OWNER_ID is monkeypatched directly (report_critical_error imports it locally
at call time, so patching the source module is what actually takes effect).

Run with: python -m pytest tests/test_critical_error_alerts.py
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import features.office_events as office_events
import handlers.admin_manager as admin_manager
from features.office_events import (
    _CRITICAL_ERROR_DETAIL_MAX_LEN,
    _CRITICAL_ERROR_RATE_LIMIT_SECONDS,
    _sanitize_detail,
    report_critical_error,
)
from runtime.registry import FACTORY_BOT_ID


class FakeRegistry:
    def __init__(self, entries: dict[int, object]):
        self._entries = entries

    def get(self, bot_id: int):
        return self._entries.get(bot_id)


def _factory_entry(bot=None):
    return SimpleNamespace(bot=bot or AsyncMock())


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    office_events.set_registry(None)
    office_events._critical_error_last_sent.clear()
    monkeypatch.setattr(admin_manager, "OWNER_ID", 555)
    yield
    office_events.set_registry(None)
    office_events._critical_error_last_sent.clear()


@pytest.mark.asyncio
async def test_delivers_alert_to_owner_via_factory_bot():
    bot = AsyncMock()
    office_events.set_registry(FakeRegistry({FACTORY_BOT_ID: _factory_entry(bot)}))

    await report_critical_error(7, "unhandled_exception", ValueError("boom"))

    bot.send_message.assert_awaited_once()
    (chat_id, text), _ = bot.send_message.call_args
    assert chat_id == 555
    assert "7" in text
    assert "unhandled_exception" in text
    assert "boom" in text


@pytest.mark.asyncio
async def test_no_registry_degrades_gracefully_without_raising():
    # office_events.set_registry(None) via fixture — no live registry at all.
    await report_critical_error(7, "unhandled_exception", ValueError("boom"))


@pytest.mark.asyncio
async def test_factory_bot_missing_from_registry_degrades_gracefully():
    office_events.set_registry(FakeRegistry({}))  # FACTORY_BOT_ID not present
    await report_critical_error(7, "unhandled_exception", ValueError("boom"))


@pytest.mark.asyncio
async def test_owner_id_unset_degrades_gracefully(monkeypatch):
    monkeypatch.setattr(admin_manager, "OWNER_ID", 0)
    bot = AsyncMock()
    office_events.set_registry(FakeRegistry({FACTORY_BOT_ID: _factory_entry(bot)}))

    await report_critical_error(7, "unhandled_exception", ValueError("boom"))

    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_message_failure_degrades_gracefully():
    bot = AsyncMock()
    bot.send_message.side_effect = RuntimeError("owner blocked the bot")
    office_events.set_registry(FakeRegistry({FACTORY_BOT_ID: _factory_entry(bot)}))

    # Must not propagate — this is the last line of defense in an already-
    # exceptional path.
    await report_critical_error(7, "unhandled_exception", ValueError("boom"))


@pytest.mark.asyncio
async def test_repeated_identical_error_is_rate_limited():
    bot = AsyncMock()
    office_events.set_registry(FakeRegistry({FACTORY_BOT_ID: _factory_entry(bot)}))

    await report_critical_error(7, "unhandled_exception", ValueError("boom"))
    await report_critical_error(7, "unhandled_exception", ValueError("boom"))
    await report_critical_error(7, "unhandled_exception", ValueError("boom"))

    assert bot.send_message.await_count == 1


@pytest.mark.asyncio
async def test_different_bot_same_error_is_not_rate_limited_together():
    bot = AsyncMock()
    office_events.set_registry(FakeRegistry({FACTORY_BOT_ID: _factory_entry(bot)}))

    await report_critical_error(7, "unhandled_exception", ValueError("boom"))
    await report_critical_error(8, "unhandled_exception", ValueError("boom"))

    assert bot.send_message.await_count == 2


@pytest.mark.asyncio
async def test_different_error_message_same_bot_is_not_rate_limited_together():
    bot = AsyncMock()
    office_events.set_registry(FakeRegistry({FACTORY_BOT_ID: _factory_entry(bot)}))

    await report_critical_error(7, "unhandled_exception", ValueError("boom"))
    await report_critical_error(7, "unhandled_exception", ValueError("totally different failure"))

    assert bot.send_message.await_count == 2


@pytest.mark.asyncio
async def test_rate_limit_window_expiry_allows_resend(monkeypatch):
    bot = AsyncMock()
    office_events.set_registry(FakeRegistry({FACTORY_BOT_ID: _factory_entry(bot)}))

    fake_now = [1000.0]
    monkeypatch.setattr(office_events.time, "monotonic", lambda: fake_now[0])

    await report_critical_error(7, "unhandled_exception", ValueError("boom"))
    fake_now[0] += _CRITICAL_ERROR_RATE_LIMIT_SECONDS + 1
    await report_critical_error(7, "unhandled_exception", ValueError("boom"))

    assert bot.send_message.await_count == 2


def test_sanitize_detail_redacts_telegram_bot_token():
    text = "request to https://api.telegram.org/bot123456789:AAABBBCCCDDDEEEFFFGGGHHHIIIJJJ/sendMessage failed"
    sanitized = _sanitize_detail(text)
    assert "123456789:AAABBBCCCDDDEEEFFFGGGHHHIIIJJJ" not in sanitized
    assert "[redacted-token]" in sanitized


def test_sanitize_detail_redacts_bearer_token():
    text = "auth failed: Bearer sk-abcdefghijklmnopqrstuvwxyz012345"
    sanitized = _sanitize_detail(text)
    assert "sk-abcdefghijklmnopqrstuvwxyz012345" not in sanitized
    assert "[redacted-token]" in sanitized


def test_sanitize_detail_redacts_url_credentials():
    text = "connection to postgres://myuser:supersecret@db.internal:5432/prod failed"
    sanitized = _sanitize_detail(text)
    assert "myuser:supersecret" not in sanitized
    assert "[redacted-credentials]" in sanitized


def test_sanitize_detail_truncates_long_text():
    sanitized = _sanitize_detail("x" * 5000)
    assert len(sanitized) < 5000
    assert sanitized.endswith("(truncated)")


@pytest.mark.asyncio
async def test_alert_text_never_contains_raw_token_end_to_end():
    bot = AsyncMock()
    office_events.set_registry(FakeRegistry({FACTORY_BOT_ID: _factory_entry(bot)}))

    exc = RuntimeError(
        "POST https://api.telegram.org/bot123456789:AAABBBCCCDDDEEEFFFGGGHHHIIIJJJ/sendMessage timed out"
    )
    await report_critical_error(7, "webhook_failure", exc)

    (_, text), _ = bot.send_message.call_args
    assert "123456789:AAABBBCCCDDDEEEFFFGGGHHHIIIJJJ" not in text


def test_sanitize_detail_redacts_key_value_shaped_secret():
    # Review finding: shape-based patterns (token=, Bearer, URL creds) miss
    # a plain "secret_key=<value>"/"api_key: <value>" shape, which is how
    # this codebase's own payment/API-key config values are most likely to
    # surface in a wrapped exception message (e.g. features/payments.py
    # raising with the YooKassa secret_key it tried to use).
    # Deliberately NOT shaped like a real provider's key prefix (e.g. Stripe's
    # sk_live_...) — GitHub's push protection flags that shape even in test
    # fixtures with obviously-fake values, since it can't tell a real key
    # from a synthetic one that merely looks right.
    text = "Invalid secret_key=fakeTestSecretValueNotARealKey1234567890 for shop 12345"
    sanitized = _sanitize_detail(text)
    assert "fakeTestSecretValueNotARealKey1234567890" not in sanitized
    assert "[redacted-secret]" in sanitized


def test_sanitize_detail_redacts_known_process_secret_value(monkeypatch):
    # Review finding: shape-based regexes miss opaque values with no
    # recognizable prefix (e.g. a base64 service-account blob). Values this
    # process's own config.py actually holds are redacted by literal match
    # as a second layer — this test uses ANTHROPIC_API_KEY since it has no
    # dashes/prefix shape any of the regexes above would catch on their own.
    import config

    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "totallyOpaqueValueNoRecognizableShape123")
    text = "Claude API call failed with key totallyOpaqueValueNoRecognizableShape123 (unauthorized)"
    sanitized = _sanitize_detail(text)
    assert "totallyOpaqueValueNoRecognizableShape123" not in sanitized
    assert "[redacted-secret]" in sanitized


@pytest.mark.asyncio
async def test_distinct_errors_with_same_long_prefix_are_not_rate_limited_together():
    # Review finding: the rate-limit key used to be derived from the
    # TRUNCATED detail text, so two distinct errors sharing a long common
    # prefix but differing only after the _CRITICAL_ERROR_DETAIL_MAX_LEN cut
    # point would collide into the same key and the second one would be
    # silently suppressed. Now the key is derived from the full (untruncated)
    # scrubbed text, so these two must both be delivered.
    bot = AsyncMock()
    office_events.set_registry(FakeRegistry({FACTORY_BOT_ID: _factory_entry(bot)}))

    common_prefix = "x" * (_CRITICAL_ERROR_DETAIL_MAX_LEN + 50)
    await report_critical_error(7, "unhandled_exception", ValueError(common_prefix + " order=1001"))
    await report_critical_error(7, "unhandled_exception", ValueError(common_prefix + " order=1002"))

    assert bot.send_message.await_count == 2
