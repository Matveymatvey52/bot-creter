"""main.py's build_group_router()'s cb_set_group callback — owner-only gate.

Pre-existing gap found during offices review: cb_set_group had no
_is_owner() check, unlike every other owner-gated handler in the app (see
MEMORY.md's offices project note). This locks in the fix.

Uses the isolated_db fixture (tests/conftest.py) so this never touches the
real data/bots.db.

Run with: python -m pytest tests/test_main_set_group_owner_gate.py
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import handlers.admin_manager as admin_manager
from db.database import init_db
from main import build_group_router


def _find_set_group_handler(router):
    for observer in router.callback_query.handlers:
        cb = observer.callback
        if cb.__name__ == "cb_set_group":
            return cb
    raise AssertionError("cb_set_group handler not found in group router")


def _make_callback(*, user_id: int, data: str):
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id)
    callback.data = data
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()
    return callback


@pytest.mark.asyncio
async def test_non_owner_is_denied_and_no_db_write(isolated_db, monkeypatch):
    monkeypatch.setattr(admin_manager, "OWNER_ID", 999999)
    handler = _find_set_group_handler(build_group_router())
    callback = _make_callback(user_id=111, data="setgroup:all:-100123")

    await handler(callback)

    callback.answer.assert_awaited_once()
    args, kwargs = callback.answer.call_args
    assert kwargs.get("show_alert") is True
    callback.message.edit_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_owner_passes_gate_and_proceeds(isolated_db, monkeypatch):
    await init_db()
    monkeypatch.setattr(admin_manager, "OWNER_ID", 555)
    handler = _find_set_group_handler(build_group_router())
    callback = _make_callback(user_id=555, data="setgroup:all:-100123")

    await handler(callback)

    callback.answer.assert_awaited_once_with()
    callback.message.edit_text.assert_awaited_once()
