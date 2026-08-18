"""main.py's build_group_router()'s cb_set_group callback.

Stage 1 multitenancy rollout: the "officedigest" target (system-wide showcase
feature) stays owner-only, but "all"/a specific bot_id no longer blanket-deny
non-owners — a customer gets scoped to bots they own (bots.owner_telegram_id),
via _can_manage_bot, instead of being denied outright. See
MEMORY.md's multitenancy project note.

Uses the isolated_db fixture (tests/conftest.py) so this never touches the
real data/bots.db.

Run with: python -m pytest tests/test_main_set_group_owner_gate.py
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import aiosqlite

import db.database as db_module
import handlers.admin_manager as admin_manager
from db.database import create_bot_record_with_admins, init_db
from main import build_group_router

FAKE_TOKEN = "123456789:AAHfakeTokenButShapedRight1234567890"


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
async def test_non_owner_denied_for_officedigest(isolated_db, monkeypatch):
    """officedigest stays a system-wide, owner-only showcase feature — not
    opened up to customers in Stage 1."""
    monkeypatch.setattr(admin_manager, "OWNER_ID", 999999)
    handler = _find_set_group_handler(build_group_router())
    callback = _make_callback(user_id=111, data="setgroup:officedigest:-100123")

    await handler(callback)

    callback.answer.assert_awaited_once()
    args, kwargs = callback.answer.call_args
    assert kwargs.get("show_alert") is True
    callback.message.edit_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_owner_passes_gate_for_officedigest(isolated_db, monkeypatch):
    await init_db()
    monkeypatch.setattr(admin_manager, "OWNER_ID", 555)
    handler = _find_set_group_handler(build_group_router())
    callback = _make_callback(user_id=555, data="setgroup:officedigest:-100123")

    try:
        await handler(callback)

        callback.answer.assert_awaited_once_with()
        callback.message.edit_text.assert_awaited_once()
    finally:
        # This writes to the REAL office_digest_group table (not
        # isolated_db-backed) — clean up so it doesn't leak into
        # tests/test_factory_analytics_api.py's showcase-group tests, which
        # use the same owner id (555).
        async with aiosqlite.connect(db_module.DB_PATH) as db:
            await db.execute("DELETE FROM office_digest_group WHERE owner_telegram_id = 555")
            await db.commit()


@pytest.mark.asyncio
async def test_owner_passes_gate_for_all_with_no_bots(isolated_db, monkeypatch):
    await init_db()
    monkeypatch.setattr(admin_manager, "OWNER_ID", 555)
    handler = _find_set_group_handler(build_group_router())
    callback = _make_callback(user_id=555, data="setgroup:all:-100123")

    await handler(callback)

    callback.answer.assert_awaited_once_with()
    callback.message.edit_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_customer_scoped_to_own_bots_for_all(isolated_db, monkeypatch):
    """A non-owner customer's "✅ Всем ботам" targets only the bot(s) they
    own — never another owner's bot."""
    await init_db()
    monkeypatch.setattr(admin_manager, "OWNER_ID", 555)
    owned_bot = await create_bot_record_with_admins(
        name="customer_owned_bot", description="d", token=FAKE_TOKEN,
        file_path="templates/tour_operator.py", admin_ids=[], owner_telegram_id=111,
    )
    others_bot = await create_bot_record_with_admins(
        name="other_customer_bot", description="d", token=FAKE_TOKEN,
        file_path="templates/tour_operator.py", admin_ids=[], owner_telegram_id=222,
    )
    handler = _find_set_group_handler(build_group_router())
    callback = _make_callback(user_id=111, data="setgroup:all:-100123")

    await handler(callback)

    callback.answer.assert_awaited_once_with()
    callback.message.edit_text.assert_awaited_once()
    text = callback.message.edit_text.call_args.args[0]
    assert "customer_owned_bot" in text
    assert "other_customer_bot" not in text


@pytest.mark.asyncio
async def test_customer_denied_for_another_customers_specific_bot(isolated_db, monkeypatch):
    await init_db()
    monkeypatch.setattr(admin_manager, "OWNER_ID", 555)
    others_bot = await create_bot_record_with_admins(
        name="other_customer_bot", description="d", token=FAKE_TOKEN,
        file_path="templates/tour_operator.py", admin_ids=[], owner_telegram_id=222,
    )
    handler = _find_set_group_handler(build_group_router())
    callback = _make_callback(user_id=111, data=f"setgroup:{others_bot}:-100123")

    await handler(callback)

    callback.answer.assert_awaited_once()
    args, kwargs = callback.answer.call_args
    assert kwargs.get("show_alert") is True
    callback.message.edit_text.assert_not_awaited()
