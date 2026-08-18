"""features/group_task.py — on_group_message()'s gating/auto-connect/reply
contract, per docs/GROUP_TASK_CHANNEL_DESIGN.md, plus the tool-use mode
(create/update/hide sellable_items, refund payments) per
docs/GROUP_TASK_TOOL_USE_DESIGN.md.

Uses the isolated_db fixture (tests/conftest.py) so this never touches the
real data/bots.db — see MEMORY.md "Backlog: test DB isolation" (same reason
tests/test_office_events_db.py uses it; unlike the older
tests/test_manage_bots_office.py, which predates that fixture and still
hits the real DB). Anthropic client mocked out (no real API call), OWNER_ID
patched per test via monkeypatch.

Run with: python -m pytest tests/test_group_task_module.py
"""
from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import features.group_task as group_task
from db.database import (
    create_bot_record_with_admins,
    delete_group_task_config,
    enable_bot_feature,
    get_group_task_config,
    init_db,
    set_group_task_config,
)
from features.payments import init_payments_tables
from features.sellable_items import init_db as init_sellable_items_db

FAKE_TOKEN = "123456:test-token-not-real"
BOT_USERNAME = "test_group_task_bot"
OWNER_ID = 620100


@dataclass
class FixtureConfig:
    db_path: str


async def _make_bot_row() -> int:
    await init_db()
    return await create_bot_record_with_admins(
        "group-task-bot", "desc", FAKE_TOKEN, "templates/tour_operator.py", []
    )


def _make_config(tmp_path, bot_id: int) -> FixtureConfig:
    return FixtureConfig(db_path=str(tmp_path / f"bot_{bot_id}_data.db"))


def _make_message(
    *,
    user_id: int,
    text: str,
    chat_id: int = -100123456,
    reply_to_bot: bool = False,
):
    message = MagicMock()
    message.from_user = SimpleNamespace(id=user_id)
    message.text = text
    message.caption = None
    message.chat = SimpleNamespace(id=chat_id, type="supergroup")
    message.reply = AsyncMock()
    if reply_to_bot:
        message.reply_to_message = SimpleNamespace(
            from_user=SimpleNamespace(is_bot=True, username=BOT_USERNAME)
        )
    else:
        message.reply_to_message = None
    return message


def _make_bot():
    bot = MagicMock()
    bot.get_me = AsyncMock(return_value=SimpleNamespace(username=BOT_USERNAME))
    return bot


def _make_callback(*, user_id: int, data: str, chat_id: int):
    callback = MagicMock()
    callback.from_user = SimpleNamespace(id=user_id)
    callback.data = data
    callback.message = MagicMock()
    callback.message.chat = SimpleNamespace(id=chat_id)
    callback.message.edit_text = AsyncMock()
    callback.answer = AsyncMock()
    return callback


@pytest.fixture(autouse=True)
def _owner_and_context(monkeypatch):
    monkeypatch.setattr(group_task, "OWNER_ID", OWNER_ID)
    monkeypatch.setattr(group_task, "_is_owner", lambda uid: uid == OWNER_ID)
    group_task._conversation_context.clear()
    group_task._last_reply_sent.clear()
    group_task._pending_actions.clear()
    yield
    group_task._conversation_context.clear()
    group_task._last_reply_sent.clear()
    group_task._pending_actions.clear()


@pytest.mark.asyncio
async def test_ignores_message_from_non_owner(isolated_db, tmp_path):
    bot_id = await _make_bot_row()
    message = _make_message(user_id=999999, text=f"@{BOT_USERNAME} hello")

    await group_task.on_group_message(message, _make_bot(), bot_id, _make_config(tmp_path, bot_id))

    message.reply.assert_not_awaited()
    assert await get_group_task_config(bot_id) is None


@pytest.mark.asyncio
async def test_ignores_message_with_no_mention_or_reply(isolated_db, tmp_path):
    bot_id = await _make_bot_row()
    message = _make_message(user_id=OWNER_ID, text="just chatting, no mention here")

    await group_task.on_group_message(message, _make_bot(), bot_id, _make_config(tmp_path, bot_id))

    message.reply.assert_not_awaited()
    assert await get_group_task_config(bot_id) is None


@pytest.mark.asyncio
async def test_first_addressed_message_auto_connects_and_confirms(isolated_db, tmp_path):
    bot_id = await _make_bot_row()
    message = _make_message(user_id=OWNER_ID, text=f"@{BOT_USERNAME} привет", chat_id=-100999)

    await group_task.on_group_message(message, _make_bot(), bot_id, _make_config(tmp_path, bot_id))

    config = await get_group_task_config(bot_id)
    assert config["group_chat_id"] == -100999
    assert config["enabled"] == 1
    message.reply.assert_awaited_once()
    assert "Готово" in message.reply.call_args.args[0]


@pytest.mark.asyncio
async def test_reply_to_bot_message_also_auto_connects(isolated_db, tmp_path):
    bot_id = await _make_bot_row()
    message = _make_message(user_id=OWNER_ID, text="do this task", reply_to_bot=True)

    await group_task.on_group_message(message, _make_bot(), bot_id, _make_config(tmp_path, bot_id))

    assert await get_group_task_config(bot_id) is not None
    message.reply.assert_awaited_once()


@pytest.mark.asyncio
async def test_ignores_addressed_message_from_a_different_group(isolated_db, tmp_path):
    bot_id = await _make_bot_row()
    await set_group_task_config(bot_id, -100111)
    message = _make_message(user_id=OWNER_ID, text=f"@{BOT_USERNAME} task", chat_id=-100222)

    with patch("anthropic.AsyncAnthropic") as mock_client_cls:
        await group_task.on_group_message(message, _make_bot(), bot_id, _make_config(tmp_path, bot_id))

    mock_client_cls.assert_not_called()
    message.reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_addressed_message_in_bound_group_gets_a_haiku_reply(isolated_db, tmp_path):
    bot_id = await _make_bot_row()
    await set_group_task_config(bot_id, -100333)
    fake_response = SimpleNamespace(content=[SimpleNamespace(type="text", text="Сделаю.")])
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=fake_response)
    message = _make_message(user_id=OWNER_ID, text=f"@{BOT_USERNAME} сделай отчёт", chat_id=-100333)

    with patch("anthropic.AsyncAnthropic", return_value=mock_client):
        await group_task.on_group_message(message, _make_bot(), bot_id, _make_config(tmp_path, bot_id))

    mock_client.messages.create.assert_awaited_once()
    assert mock_client.messages.create.call_args.kwargs["model"] == group_task._CHAT_MODEL
    assert "tools" not in mock_client.messages.create.call_args.kwargs
    message.reply.assert_awaited_once_with("Сделаю.")


@pytest.mark.asyncio
async def test_rate_limit_suppresses_rapid_second_call(isolated_db, tmp_path):
    bot_id = await _make_bot_row()
    await set_group_task_config(bot_id, -100333)
    fake_response = SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")])
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=fake_response)
    message1 = _make_message(user_id=OWNER_ID, text=f"@{BOT_USERNAME} one", chat_id=-100333)
    message2 = _make_message(user_id=OWNER_ID, text=f"@{BOT_USERNAME} two", chat_id=-100333)
    config = _make_config(tmp_path, bot_id)

    with patch("anthropic.AsyncAnthropic", return_value=mock_client):
        await group_task.on_group_message(message1, _make_bot(), bot_id, config)
        await group_task.on_group_message(message2, _make_bot(), bot_id, config)

    assert mock_client.messages.create.await_count == 1
    message2.reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_config_is_ignored(isolated_db, tmp_path):
    bot_id = await _make_bot_row()
    await set_group_task_config(bot_id, -100333)
    # simulate a disabled row directly — there's no public "disable without
    # deleting" API yet, this exercises the enabled-check branch only.
    import aiosqlite

    import db.database as db_module

    async with aiosqlite.connect(db_module.DB_PATH) as db:
        await db.execute("UPDATE bot_group_task_config SET enabled = 0 WHERE bot_id = ?", (bot_id,))
        await db.commit()

    message = _make_message(user_id=OWNER_ID, text=f"@{BOT_USERNAME} task", chat_id=-100333)
    with patch("anthropic.AsyncAnthropic") as mock_client_cls:
        await group_task.on_group_message(message, _make_bot(), bot_id, _make_config(tmp_path, bot_id))

    mock_client_cls.assert_not_called()
    message.reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_group_task_config_disconnects(isolated_db):
    bot_id = await _make_bot_row()
    await set_group_task_config(bot_id, -100333)
    await delete_group_task_config(bot_id)
    assert await get_group_task_config(bot_id) is None


@pytest.mark.asyncio
async def test_invalidate_group_task_state_clears_in_memory_dicts(isolated_db):
    bot_id = await _make_bot_row()
    await set_group_task_config(bot_id, -100333)
    group_task._conversation_context[bot_id] = [("user", "hi")]
    group_task._last_reply_sent[bot_id] = 123.0
    group_task._pending_actions[bot_id] = {"token": "x"}

    group_task.invalidate_group_task_state(bot_id)

    assert bot_id not in group_task._conversation_context
    assert bot_id not in group_task._last_reply_sent
    assert bot_id not in group_task._pending_actions


def test_invalidate_group_task_state_is_a_noop_for_unknown_bot_id():
    group_task.invalidate_group_task_state(999999999)  # must not raise


# ── Tool-use mode ────────────────────────────────────────────────────────

async def _make_bot_row_with_sellable_items(tmp_path) -> tuple[int, FixtureConfig]:
    bot_id = await _make_bot_row()
    await enable_bot_feature(bot_id, "sellable_items")
    config = _make_config(tmp_path, bot_id)
    await init_sellable_items_db(config.db_path)
    return bot_id, config


async def _make_bot_row_with_payments(tmp_path) -> tuple[int, FixtureConfig]:
    bot_id = await _make_bot_row()
    await enable_bot_feature(bot_id, "payments")
    config = _make_config(tmp_path, bot_id)
    await init_payments_tables(config.db_path)
    return bot_id, config


def _tool_use_response(name: str, tool_input: dict, tool_id: str = "toolu_1"):
    return SimpleNamespace(content=[SimpleNamespace(type="tool_use", name=name, input=tool_input, id=tool_id)])


@pytest.mark.asyncio
async def test_no_tools_offered_when_no_tool_feature_enabled(isolated_db, tmp_path):
    bot_id = await _make_bot_row()
    await set_group_task_config(bot_id, -100333)
    fake_response = SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")])
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=fake_response)
    message = _make_message(user_id=OWNER_ID, text=f"@{BOT_USERNAME} расскажи", chat_id=-100333)

    with patch("anthropic.AsyncAnthropic", return_value=mock_client):
        await group_task.on_group_message(message, _make_bot(), bot_id, _make_config(tmp_path, bot_id))

    assert "tools" not in mock_client.messages.create.call_args.kwargs
    assert mock_client.messages.create.call_args.kwargs["model"] == group_task._CHAT_MODEL


@pytest.mark.asyncio
async def test_tool_call_proposes_confirmation_and_does_not_write(isolated_db, tmp_path):
    bot_id, config = await _make_bot_row_with_sellable_items(tmp_path)
    await set_group_task_config(bot_id, -100333)
    tool_input = {"name": "Экскурсия", "price": 500}
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(
        return_value=_tool_use_response("create_sellable_item", tool_input)
    )
    message = _make_message(
        user_id=OWNER_ID, text=f"@{BOT_USERNAME} добавь позицию Экскурсия за 500", chat_id=-100333
    )

    with patch("anthropic.AsyncAnthropic", return_value=mock_client):
        await group_task.on_group_message(message, _make_bot(), bot_id, config)

    assert mock_client.messages.create.call_args.kwargs["model"] == group_task._TOOL_MODEL
    assert len(mock_client.messages.create.call_args.kwargs["tools"]) == 3  # sellable_items tools only
    message.reply.assert_awaited_once()
    reply_text, kwargs = message.reply.call_args.args[0], message.reply.call_args.kwargs
    assert "Экскурсия" in reply_text
    assert "reply_markup" in kwargs

    from features.sellable_items import _all_items

    assert await _all_items(config.db_path) == []  # not executed yet
    assert bot_id in group_task._pending_actions


@pytest.mark.asyncio
async def test_confirm_executes_create_sellable_item(isolated_db, tmp_path):
    bot_id, config = await _make_bot_row_with_sellable_items(tmp_path)
    await set_group_task_config(bot_id, -100333)
    group_task._pending_actions[bot_id] = {
        "token": "tok1",
        "tool_name": "create_sellable_item",
        "tool_input": {"name": "Экскурсия", "price": 500},
        "chat_id": -100333,
        "created_at": __import__("time").monotonic(),
    }
    callback = _make_callback(user_id=OWNER_ID, data="gt_ok:tok1", chat_id=-100333)

    await group_task.cb_grouptask_confirm(callback, bot_id, config)

    from features.sellable_items import _all_items

    items = await _all_items(config.db_path)
    assert len(items) == 1
    assert items[0]["name"] == "Экскурсия"
    assert items[0]["price"] == 500
    assert bot_id not in group_task._pending_actions
    callback.message.edit_text.assert_awaited_once()
    assert "✅" in callback.message.edit_text.call_args.args[0]


@pytest.mark.asyncio
async def test_double_confirm_only_executes_once(isolated_db, tmp_path):
    """Regression for a TOCTOU race: two near-simultaneous ✅ taps (double-tap
    or Telegram redelivering the callback) must not both create the item —
    the pending entry has to be claimed (popped) before the first await, not
    after, so the second concurrent call sees pending is None."""
    bot_id, config = await _make_bot_row_with_sellable_items(tmp_path)
    await set_group_task_config(bot_id, -100333)
    group_task._pending_actions[bot_id] = {
        "token": "tok1",
        "tool_name": "create_sellable_item",
        "tool_input": {"name": "Экскурсия", "price": 500},
        "chat_id": -100333,
        "created_at": __import__("time").monotonic(),
    }
    callback1 = _make_callback(user_id=OWNER_ID, data="gt_ok:tok1", chat_id=-100333)
    callback2 = _make_callback(user_id=OWNER_ID, data="gt_ok:tok1", chat_id=-100333)

    import asyncio

    await asyncio.gather(
        group_task.cb_grouptask_confirm(callback1, bot_id, config),
        group_task.cb_grouptask_confirm(callback2, bot_id, config),
    )

    from features.sellable_items import _all_items

    items = await _all_items(config.db_path)
    assert len(items) == 1


@pytest.mark.asyncio
async def test_confirm_rejects_non_owner(isolated_db, tmp_path):
    bot_id, config = await _make_bot_row_with_sellable_items(tmp_path)
    await set_group_task_config(bot_id, -100333)
    group_task._pending_actions[bot_id] = {
        "token": "tok1",
        "tool_name": "create_sellable_item",
        "tool_input": {"name": "Экскурсия", "price": 500},
        "chat_id": -100333,
        "created_at": __import__("time").monotonic(),
    }
    callback = _make_callback(user_id=999999, data="gt_ok:tok1", chat_id=-100333)

    await group_task.cb_grouptask_confirm(callback, bot_id, config)

    from features.sellable_items import _all_items

    assert await _all_items(config.db_path) == []
    assert bot_id in group_task._pending_actions  # not consumed
    callback.message.edit_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirm_rejects_stale_token(isolated_db, tmp_path):
    bot_id, config = await _make_bot_row_with_sellable_items(tmp_path)
    await set_group_task_config(bot_id, -100333)
    group_task._pending_actions[bot_id] = {
        "token": "tok1",
        "tool_name": "create_sellable_item",
        "tool_input": {"name": "Экскурсия", "price": 500},
        "chat_id": -100333,
        "created_at": __import__("time").monotonic(),
    }
    callback = _make_callback(user_id=OWNER_ID, data="gt_ok:wrong-token", chat_id=-100333)

    await group_task.cb_grouptask_confirm(callback, bot_id, config)

    from features.sellable_items import _all_items

    assert await _all_items(config.db_path) == []
    assert bot_id in group_task._pending_actions


@pytest.mark.asyncio
async def test_confirm_expired_pending_action_does_not_execute(isolated_db, tmp_path, monkeypatch):
    bot_id, config = await _make_bot_row_with_sellable_items(tmp_path)
    await set_group_task_config(bot_id, -100333)
    group_task._pending_actions[bot_id] = {
        "token": "tok1",
        "tool_name": "create_sellable_item",
        "tool_input": {"name": "Экскурсия", "price": 500},
        "chat_id": -100333,
        "created_at": 0.0,
    }
    monkeypatch.setattr(group_task.time, "monotonic", lambda: 999999.0)
    callback = _make_callback(user_id=OWNER_ID, data="gt_ok:tok1", chat_id=-100333)

    await group_task.cb_grouptask_confirm(callback, bot_id, config)

    from features.sellable_items import _all_items

    assert await _all_items(config.db_path) == []
    assert bot_id not in group_task._pending_actions


@pytest.mark.asyncio
async def test_cancel_clears_pending_action_and_does_not_write(isolated_db, tmp_path):
    bot_id, config = await _make_bot_row_with_sellable_items(tmp_path)
    await set_group_task_config(bot_id, -100333)
    group_task._pending_actions[bot_id] = {
        "token": "tok1",
        "tool_name": "create_sellable_item",
        "tool_input": {"name": "Экскурсия", "price": 500},
        "chat_id": -100333,
        "created_at": __import__("time").monotonic(),
    }
    callback = _make_callback(user_id=OWNER_ID, data="gt_no:tok1", chat_id=-100333)

    await group_task.cb_grouptask_cancel(callback, bot_id)

    from features.sellable_items import _all_items

    assert await _all_items(config.db_path) == []
    assert bot_id not in group_task._pending_actions
    callback.message.edit_text.assert_awaited_once()
    assert "Отменено" in callback.message.edit_text.call_args.args[0]


@pytest.mark.asyncio
async def test_confirm_refund_payment(isolated_db, tmp_path):
    bot_id, config = await _make_bot_row_with_payments(tmp_path)
    await set_group_task_config(bot_id, -100333)

    import aiosqlite

    async with aiosqlite.connect(config.db_path) as db:
        await db.execute(
            "INSERT INTO payments (telegram_payment_charge_id, user_id, invoice_payload, currency, total_amount) "
            "VALUES (?, ?, ?, ?, ?)",
            ("charge_abc", 111, "payload", "RUB", 1000),
        )
        await db.commit()

    group_task._pending_actions[bot_id] = {
        "token": "tok1",
        "tool_name": "refund_payment",
        "tool_input": {"telegram_payment_charge_id": "charge_abc"},
        "chat_id": -100333,
        "created_at": __import__("time").monotonic(),
    }
    callback = _make_callback(user_id=OWNER_ID, data="gt_ok:tok1", chat_id=-100333)

    await group_task.cb_grouptask_confirm(callback, bot_id, config)

    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await (
            await db.execute(
                "SELECT status FROM payments WHERE telegram_payment_charge_id=?", ("charge_abc",)
            )
        ).fetchone()
    assert row["status"] == "refunded"
    assert "✅" in callback.message.edit_text.call_args.args[0]


@pytest.mark.asyncio
async def test_confirm_bad_tool_input_reports_error_without_raising(isolated_db, tmp_path):
    bot_id, config = await _make_bot_row_with_sellable_items(tmp_path)
    await set_group_task_config(bot_id, -100333)
    group_task._pending_actions[bot_id] = {
        "token": "tok1",
        "tool_name": "update_sellable_item",
        "tool_input": {"item_id": 999, "field": "price", "value": "500"},
        "chat_id": -100333,
        "created_at": __import__("time").monotonic(),
    }
    callback = _make_callback(user_id=OWNER_ID, data="gt_ok:tok1", chat_id=-100333)

    await group_task.cb_grouptask_confirm(callback, bot_id, config)

    assert "❌" in callback.message.edit_text.call_args.args[0]
    assert bot_id not in group_task._pending_actions
