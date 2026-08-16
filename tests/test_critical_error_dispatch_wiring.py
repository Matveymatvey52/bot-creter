"""runtime/registry.py's register_critical_error_handler() — proves the
aiogram dp.errors() hook actually fires report_critical_error() with the
right bot_id/category, per docs/CRITICAL_ALERTS_DESIGN.md §3's "ошибка на
уровне aiogram-диспетчера" row.

Constructs a bare Dispatcher directly (no build_entry()/templates/DB
involved — this test is only about the errors() wiring itself, not the rest
of build_entry()'s template-resolution machinery already covered by
tests/test_office_events_registry_wiring.py).

Run with: python -m pytest tests/test_critical_error_dispatch_wiring.py
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from aiogram import Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ErrorEvent, Update

from runtime.registry import register_critical_error_handler


@pytest.mark.asyncio
async def test_errors_hook_calls_report_critical_error_with_bot_id_and_category():
    dp = Dispatcher(storage=MemoryStorage())

    # register_critical_error_handler() does `from features.office_events
    # import report_critical_error` at REGISTRATION time (binding a local
    # name inside its closure) — patching the module attribute only takes
    # effect for imports that happen after the patch, so the patch context
    # must be entered before register_critical_error_handler() runs, not
    # just before dp.errors.trigger().
    with patch("features.office_events.report_critical_error", new=AsyncMock()) as mock_report:
        register_critical_error_handler(dp, bot_id=42)

        fake_exc = RuntimeError("handler exploded")
        error_event = ErrorEvent(update=Update(update_id=1), exception=fake_exc)

        # dp.errors() registers into dp.errors — invoke its trigger chain
        # exactly as aiogram would on a real dispatch failure, without
        # needing to feed a full fake Update through the whole routing
        # stack (message parsing, chat/user construction) that isn't what
        # this test is verifying.
        handled = await dp.errors.trigger(error_event)

    mock_report.assert_awaited_once_with(42, "unhandled_exception", fake_exc)
    assert handled is True
