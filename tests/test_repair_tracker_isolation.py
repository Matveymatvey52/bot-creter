"""repair_tracker template — data isolation, client-ticket-creation, and
notify-on-every-status-change tests.

Standard criterion (shared with every other template's isolation test file):
two bots on the SAME template, different config (db_path/admins_file), must
never mix data — even driven by the SAME Telegram user_id (admin OR client).

repair_tracker's own differentiators from vehicle_service.py (its structural
reference):
- a ticket's `client_user_id` is captured DIRECTLY from the Telegram user who
  runs the client-side ticket-creation flow — there is no separate phone/
  Contact-linking step, since (unlike vehicle_service's pre-existing
  client/vehicle records) a ticket is only ever created BY its own client, in
  the same flow that captures their id. Ownership is therefore established
  by construction, and enforced on every read via `WHERE client_user_id=?` —
  this is this template's equivalent of vehicle_service's Contact.user_id
  ownership check, just expressed as a SQL predicate instead of a comparison
  against a shared Contact payload. Proven here by
  RepairTrackerOwnershipTests: a hand-crafted callback_data guessing another
  client's ticket id must not leak it.
- the client is notified on EVERY status change (not just one specific
  status like vehicle_service's "ready") — proven by
  RepairTrackerStatusNotifyTests, which walks a ticket through the full
  new -> diagnosing -> in_progress -> ready -> given_out chain and asserts a
  distinct notification at each step.
- the diagnosing -> in_progress transition is the one place the admin flow
  goes stateful: it must collect a price BEFORE applying, unlike every other
  transition which applies directly from a single button tap. Proven by
  RepairTrackerPriceGateTests.
- double-tap-safety is achieved via a single compare-and-swap UPDATE
  (`WHERE id=? AND status=?`) with no separate status-log table (unlike
  vehicle_service's service_status_log+notified column) — the UPDATE's own
  rowcount is sufficient to know whether THIS call actually applied the
  transition and must notify. Proven by RepairTrackerDoubleTapTests.
- admin_note must never appear in the client-facing ticket text. Proven by
  RepairTrackerAdminNoteTests.

No real Telegram network calls, no real tokens.

Run with: python -m unittest tests.test_repair_tracker_isolation
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

import db.database as db_module
from runtime.registry import get_template_router
from templates import repair_tracker

FAKE_TOKEN = "123456:test-token-not-real"
ADMIN_ID = 999
CLIENT_A_ID = 555
CLIENT_B_ID = 556


def _text_update(update_id: int, user_id: int, text: str) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id, "date": 1700000000,
            "chat": {"id": user_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
            "text": text,
        },
    }


def _callback_update(update_id: int, user_id: int, data: str) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": str(update_id),
            "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
            "message": {
                "message_id": update_id, "date": 1700000000,
                "chat": {"id": user_id, "type": "private"}, "text": "placeholder",
            },
            "chat_instance": "1", "data": data,
        },
    }


def _build_bot_dispatcher(config: repair_tracker.RepairTrackerConfig) -> tuple[Bot, Dispatcher]:
    bot = Bot(token=FAKE_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(repair_tracker.ConfigMiddleware(config))
    dp.include_router(get_template_router("repair_tracker"))
    return bot, dp


async def _create_ticket_via_flow(
    dp: Dispatcher, bot: Bot, client_id: int, item: str, issue: str, phone: str | None, start_update_id: int,
) -> int:
    """Drives the full client FSM: tkt_new -> item -> issue -> phone (typed,
    or skipped via button if phone is None). Returns the next free update_id."""
    uid = start_update_id
    await dp.feed_webhook_update(bot, _callback_update(uid, client_id, "tkt_new")); uid += 1
    await dp.feed_webhook_update(bot, _text_update(uid, client_id, item)); uid += 1
    await dp.feed_webhook_update(bot, _text_update(uid, client_id, issue)); uid += 1
    if phone is not None:
        await dp.feed_webhook_update(bot, _text_update(uid, client_id, phone)); uid += 1
    else:
        await dp.feed_webhook_update(bot, _callback_update(uid, client_id, "tkt_phone_skip")); uid += 1
    return uid


def _sent_texts_to(bot_call_mock, chat_id: int) -> list[str]:
    """Collects every text sent/edited toward chat_id — covers both
    SendMessage and EditMessageText Telegram API requests, same helper shape
    as vehicle_service.py's own test suite."""
    texts = []
    for call in bot_call_mock.call_args_list:
        request = call.args[0] if call.args else None
        text = getattr(request, "text", None)
        cid = getattr(request, "chat_id", None)
        if text and cid == chat_id:
            texts.append(text)
    return texts


def _notification_texts_to(bot_call_mock, chat_id: int) -> list[str]:
    """Same as _sent_texts_to but narrowed to status-change notifications
    (the "🔔" bell prefix used only by _apply_status_change's client message)
    — a client chat also receives its own ticket-creation-flow prompts/
    confirmation via the SAME chat_id, which _sent_texts_to would otherwise
    conflate with the notifications under test here."""
    return [t for t in _sent_texts_to(bot_call_mock, chat_id) if t.startswith("🔔")]


class RepairTrackerIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

        self.config_a = repair_tracker.config_from_bot_row(
            {"bot_id": 951, "name": "repair_isolation_bot_a", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        self.config_b = repair_tracker.config_from_bot_row(
            {"bot_id": 952, "name": "repair_isolation_bot_b", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await repair_tracker.init_db(self.config_a.db_path)
        await repair_tracker.init_db(self.config_b.db_path)

        self.bot_a, self.dp_a = _build_bot_dispatcher(self.config_a)
        self.bot_b, self.dp_b = _build_bot_dispatcher(self.config_b)
        # Bootstrap the SAME admin user_id on both bots.
        await self.dp_a.feed_webhook_update(self.bot_a, _text_update(1, ADMIN_ID, "/start"))
        await self.dp_b.feed_webhook_update(self.bot_b, _text_update(1, ADMIN_ID, "/start"))

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_configs_point_to_different_files(self):
        self.assertNotEqual(self.config_a.db_path, self.config_b.db_path)

    async def test_two_bots_same_client_tickets_not_mixed(self):
        # Same CLIENT_A_ID driving both bots — must never see the other bot's rows.
        await _create_ticket_via_flow(self.dp_a, self.bot_a, CLIENT_A_ID, "Наушники Sony", "Не работает левый динамик", None, 10)
        await _create_ticket_via_flow(self.dp_b, self.bot_b, CLIENT_A_ID, "Часы Casio", "Треснуло стекло", None, 10)

        conn_a = sqlite3.connect(self.config_a.db_path)
        tickets_a = conn_a.execute("SELECT COUNT(*) FROM repair_tickets").fetchone()[0]
        item_a = conn_a.execute("SELECT item_description FROM repair_tickets").fetchone()[0]
        conn_a.close()
        conn_b = sqlite3.connect(self.config_b.db_path)
        tickets_b = conn_b.execute("SELECT COUNT(*) FROM repair_tickets").fetchone()[0]
        item_b = conn_b.execute("SELECT item_description FROM repair_tickets").fetchone()[0]
        conn_b.close()

        self.assertEqual(tickets_a, 1)
        self.assertEqual(tickets_b, 1)
        self.assertEqual(item_a, "Наушники Sony")
        self.assertEqual(item_b, "Часы Casio")

    async def test_two_bots_same_admin_status_changes_not_mixed(self):
        await _create_ticket_via_flow(self.dp_a, self.bot_a, CLIENT_A_ID, "Телефон", "Не включается", None, 10)
        await _create_ticket_via_flow(self.dp_b, self.bot_b, CLIENT_A_ID, "Ноутбук", "Не заряжается", None, 10)

        # Same ADMIN_ID moves bot A's ticket new->diagnosing.
        await self.dp_a.feed_webhook_update(self.bot_a, _callback_update(100, ADMIN_ID, "atkt_status:1:diagnosing"))

        conn_a = sqlite3.connect(self.config_a.db_path)
        status_a = conn_a.execute("SELECT status FROM repair_tickets WHERE id=1").fetchone()[0]
        conn_a.close()
        conn_b = sqlite3.connect(self.config_b.db_path)
        status_b = conn_b.execute("SELECT status FROM repair_tickets WHERE id=1").fetchone()[0]
        conn_b.close()

        self.assertEqual(status_a, "diagnosing")
        self.assertEqual(status_b, "new", "a status change on bot A's ticket leaked into bot B's database")


class RepairTrackerClientFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._bot_call = AsyncMock(return_value=MagicMock())
        self._bot_call_patcher = patch.object(Bot, "__call__", new=self._bot_call)
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = repair_tracker.config_from_bot_row(
            {"bot_id": 953, "name": "repair_client_bot", "display_name": None, "group_chat_id": None}, self.data_dir,
        )
        await repair_tracker.init_db(self.config.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)
        await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/start"))

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_client_creates_ticket_end_to_end_with_phone(self):
        await _create_ticket_via_flow(
            self.dp, self.bot, CLIENT_A_ID, "Наушники Sony WH-1000", "Не работает левый динамик", "89991234567", 10,
        )
        conn = sqlite3.connect(self.config.db_path)
        row = conn.execute(
            "SELECT client_user_id, item_description, issue_description, client_phone, status "
            "FROM repair_tickets"
        ).fetchone()
        conn.close()
        self.assertEqual(row, (
            CLIENT_A_ID, "Наушники Sony WH-1000", "Не работает левый динамик",
            "+7 (999) 123-45-67", "new",
        ))

    async def test_client_creates_ticket_without_phone(self):
        await _create_ticket_via_flow(self.dp, self.bot, CLIENT_A_ID, "Часы Casio", "Треснуло стекло", None, 10)
        conn = sqlite3.connect(self.config.db_path)
        phone = conn.execute("SELECT client_phone FROM repair_tickets").fetchone()[0]
        conn.close()
        self.assertIsNone(phone)

    async def test_admin_is_notified_of_new_ticket(self):
        await _create_ticket_via_flow(self.dp, self.bot, CLIENT_A_ID, "Кроссовки", "Отклеилась подошва", None, 10)
        admin_texts = _sent_texts_to(self._bot_call, ADMIN_ID)
        self.assertTrue(any("Новая заявка" in t and "Кроссовки" in t for t in admin_texts))

    async def test_client_own_tickets_list_shows_created_ticket(self):
        await _create_ticket_via_flow(self.dp, self.bot, CLIENT_A_ID, "Планшет", "Разбит экран", None, 10)
        await self.dp.feed_webhook_update(self.bot, _callback_update(100, CLIENT_A_ID, "tkt_mine"))
        client_texts = _sent_texts_to(self._bot_call, CLIENT_A_ID)
        self.assertTrue(any("Ваши заявки" in t for t in client_texts))


class RepairTrackerStatusNotifyTests(unittest.IsolatedAsyncioTestCase):
    """Owner-specified differentiator: the client must be notified
    automatically on EVERY status change (not just one specific status)."""

    async def asyncSetUp(self):
        self._bot_call = AsyncMock(return_value=MagicMock())
        self._bot_call_patcher = patch.object(Bot, "__call__", new=self._bot_call)
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = repair_tracker.config_from_bot_row(
            {"bot_id": 954, "name": "repair_notify_bot", "display_name": None, "group_chat_id": None}, self.data_dir,
        )
        await repair_tracker.init_db(self.config.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)
        await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/start"))
        await _create_ticket_via_flow(self.dp, self.bot, CLIENT_A_ID, "Стиральная машина", "Не сливает воду", None, 10)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_full_status_chain_notifies_client_at_every_step(self):
        uid = 100
        # new -> diagnosing (no price needed)
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, ADMIN_ID, "atkt_status:1:diagnosing")); uid += 1
        # diagnosing -> in_progress (price-gated: button tap prompts, then price text applies it)
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, ADMIN_ID, "atkt_status:1:in_progress")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _text_update(uid, ADMIN_ID, "1500")); uid += 1
        # in_progress -> ready
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, ADMIN_ID, "atkt_status:1:ready")); uid += 1
        # ready -> given_out
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, ADMIN_ID, "atkt_status:1:given_out")); uid += 1

        notifications = _notification_texts_to(self._bot_call, CLIENT_A_ID)
        self.assertEqual(len(notifications), 4, f"expected one notification per transition, got: {notifications}")
        self.assertIn("«Диагностика»", notifications[0])
        self.assertIn("«В работе»", notifications[1])
        self.assertIn("«Готова»", notifications[2])
        self.assertIn("«Выдана»", notifications[3])
        for text in notifications:
            self.assertIn("№1", text)

        conn = sqlite3.connect(self.config.db_path)
        status, estimated_price = conn.execute(
            "SELECT status, estimated_price FROM repair_tickets WHERE id=1"
        ).fetchone()
        conn.close()
        self.assertEqual(status, "given_out")
        self.assertEqual(estimated_price, 1500)

    async def test_cancelled_reachable_from_new(self):
        await self.dp.feed_webhook_update(self.bot, _callback_update(100, ADMIN_ID, "atkt_status:1:cancelled"))
        notifications = _notification_texts_to(self._bot_call, CLIENT_A_ID)
        self.assertEqual(len(notifications), 1)
        self.assertIn("«Отменена»", notifications[0])

    async def test_forward_only_flow_rejects_skipping_a_stage(self):
        # "new" -> "ready" is not a legal transition (must pass through
        # diagnosing and in_progress).
        await self.dp.feed_webhook_update(self.bot, _callback_update(100, ADMIN_ID, "atkt_status:1:ready"))
        conn = sqlite3.connect(self.config.db_path)
        status = conn.execute("SELECT status FROM repair_tickets WHERE id=1").fetchone()[0]
        conn.close()
        self.assertEqual(status, "new")
        self.assertEqual(_notification_texts_to(self._bot_call, CLIENT_A_ID), [])

    async def test_given_out_and_cancelled_are_terminal(self):
        await self.dp.feed_webhook_update(self.bot, _callback_update(100, ADMIN_ID, "atkt_status:1:cancelled"))
        # Any further transition attempt on a cancelled ticket is a no-op.
        await self.dp.feed_webhook_update(self.bot, _callback_update(101, ADMIN_ID, "atkt_status:1:diagnosing"))
        conn = sqlite3.connect(self.config.db_path)
        status = conn.execute("SELECT status FROM repair_tickets WHERE id=1").fetchone()[0]
        conn.close()
        self.assertEqual(status, "cancelled")
        self.assertEqual(len(_notification_texts_to(self._bot_call, CLIENT_A_ID)), 1, "a no-op transition must not notify")


class RepairTrackerPriceGateTests(unittest.IsolatedAsyncioTestCase):
    """diagnosing -> in_progress is the one transition that must collect a
    price BEFORE applying — every other transition applies directly."""

    async def asyncSetUp(self):
        self._bot_call = AsyncMock(return_value=MagicMock())
        self._bot_call_patcher = patch.object(Bot, "__call__", new=self._bot_call)
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = repair_tracker.config_from_bot_row(
            {"bot_id": 955, "name": "repair_price_bot", "display_name": None, "group_chat_id": None}, self.data_dir,
        )
        await repair_tracker.init_db(self.config.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)
        await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/start"))
        await _create_ticket_via_flow(self.dp, self.bot, CLIENT_A_ID, "Пылесос", "Не включается", None, 10)
        await self.dp.feed_webhook_update(self.bot, _callback_update(100, ADMIN_ID, "atkt_status:1:diagnosing"))

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_tapping_in_progress_does_not_apply_until_price_given(self):
        await self.dp.feed_webhook_update(self.bot, _callback_update(200, ADMIN_ID, "atkt_status:1:in_progress"))
        conn = sqlite3.connect(self.config.db_path)
        status, price = conn.execute("SELECT status, estimated_price FROM repair_tickets WHERE id=1").fetchone()
        conn.close()
        self.assertEqual(status, "diagnosing", "status must not change until a price is entered")
        self.assertIsNone(price)

    async def test_invalid_price_text_is_rejected_and_reprompted(self):
        await self.dp.feed_webhook_update(self.bot, _callback_update(200, ADMIN_ID, "atkt_status:1:in_progress"))
        await self.dp.feed_webhook_update(self.bot, _text_update(201, ADMIN_ID, "not-a-number"))
        conn = sqlite3.connect(self.config.db_path)
        status = conn.execute("SELECT status FROM repair_tickets WHERE id=1").fetchone()[0]
        conn.close()
        self.assertEqual(status, "diagnosing")

    async def test_valid_price_applies_transition_and_stores_estimate(self):
        await self.dp.feed_webhook_update(self.bot, _callback_update(200, ADMIN_ID, "atkt_status:1:in_progress"))
        await self.dp.feed_webhook_update(self.bot, _text_update(201, ADMIN_ID, "2500"))
        conn = sqlite3.connect(self.config.db_path)
        status, price = conn.execute("SELECT status, estimated_price FROM repair_tickets WHERE id=1").fetchone()
        conn.close()
        self.assertEqual(status, "in_progress")
        self.assertEqual(price, 2500)
        # asyncSetUp already drove new->diagnosing (its own notification), so
        # this transition's notification is the SECOND one, not the only one.
        notifications = _notification_texts_to(self._bot_call, CLIENT_A_ID)
        self.assertEqual(len(notifications), 2)
        self.assertIn("«В работе»", notifications[-1])


class RepairTrackerDoubleTapTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._bot_call = AsyncMock(return_value=MagicMock())
        self._bot_call_patcher = patch.object(Bot, "__call__", new=self._bot_call)
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = repair_tracker.config_from_bot_row(
            {"bot_id": 956, "name": "repair_doubletap_bot", "display_name": None, "group_chat_id": None}, self.data_dir,
        )
        await repair_tracker.init_db(self.config.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)
        await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/start"))
        await _create_ticket_via_flow(self.dp, self.bot, CLIENT_A_ID, "Смартфон", "Разбит экран", None, 10)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_double_tap_on_same_transition_notifies_only_once(self):
        await self.dp.feed_webhook_update(self.bot, _callback_update(100, ADMIN_ID, "atkt_status:1:diagnosing"))
        # Stale/duplicate re-tap of the same already-applied transition.
        await self.dp.feed_webhook_update(self.bot, _callback_update(101, ADMIN_ID, "atkt_status:1:diagnosing"))

        self.assertEqual(len(_notification_texts_to(self._bot_call, CLIENT_A_ID)), 1, "double-tap notified the client twice")
        conn = sqlite3.connect(self.config.db_path)
        status = conn.execute("SELECT status FROM repair_tickets WHERE id=1").fetchone()[0]
        conn.close()
        self.assertEqual(status, "diagnosing")


class RepairTrackerOwnershipTests(unittest.IsolatedAsyncioTestCase):
    """A client must never be able to view another client's ticket, even by
    guessing/crafting a tkt_view:<id> callback_data for an id they don't own —
    this template's equivalent of vehicle_service's Contact-ownership check."""

    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = repair_tracker.config_from_bot_row(
            {"bot_id": 957, "name": "repair_ownership_bot", "display_name": None, "group_chat_id": None}, self.data_dir,
        )
        await repair_tracker.init_db(self.config.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)
        await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/start"))
        await _create_ticket_via_flow(self.dp, self.bot, CLIENT_A_ID, "Секретный ремонт клиента A", "Проблема A", None, 10)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_client_b_cannot_view_client_a_ticket(self):
        with patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock())) as bot_call:
            await self.dp.feed_webhook_update(self.bot, _callback_update(100, CLIENT_B_ID, "tkt_view:1"))
            texts = _sent_texts_to(bot_call, CLIENT_B_ID)
        self.assertTrue(texts, "expected some response")
        self.assertFalse(any("Секретный ремонт" in t for t in texts), "client B saw client A's ticket contents")
        self.assertTrue(any("не найдена" in t for t in texts))

    async def test_owner_can_view_their_own_ticket(self):
        with patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock())) as bot_call:
            await self.dp.feed_webhook_update(self.bot, _callback_update(100, CLIENT_A_ID, "tkt_view:1"))
            texts = _sent_texts_to(bot_call, CLIENT_A_ID)
        self.assertTrue(any("Секретный ремонт" in t for t in texts))


class RepairTrackerAdminNoteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._bot_call = AsyncMock(return_value=MagicMock())
        self._bot_call_patcher = patch.object(Bot, "__call__", new=self._bot_call)
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = repair_tracker.config_from_bot_row(
            {"bot_id": 958, "name": "repair_note_bot", "display_name": None, "group_chat_id": None}, self.data_dir,
        )
        await repair_tracker.init_db(self.config.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)
        await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/start"))
        await _create_ticket_via_flow(self.dp, self.bot, CLIENT_A_ID, "Телевизор", "Не включается", None, 10)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_note_saved_and_not_leaked_to_client(self):
        await self.dp.feed_webhook_update(self.bot, _callback_update(100, ADMIN_ID, "note_pick:1"))
        await self.dp.feed_webhook_update(self.bot, _text_update(101, ADMIN_ID, "Клиент грубит по телефону"))

        conn = sqlite3.connect(self.config.db_path)
        note = conn.execute("SELECT admin_note FROM repair_tickets WHERE id=1").fetchone()[0]
        conn.close()
        self.assertEqual(note, "Клиент грубит по телефону")

        admin_texts = _sent_texts_to(self._bot_call, ADMIN_ID)
        self.assertTrue(any("Клиент грубит по телефону" in t for t in admin_texts))

        # Trigger a status change (which sends the client a notification) and
        # confirm the note text never appears in anything sent to the client.
        await self.dp.feed_webhook_update(self.bot, _callback_update(102, ADMIN_ID, "atkt_status:1:diagnosing"))
        client_texts = _sent_texts_to(self._bot_call, CLIENT_A_ID)
        self.assertTrue(client_texts)
        self.assertFalse(any("Клиент грубит по телефону" in t for t in client_texts))

        # And the client's own ticket-detail view must not include it either.
        await self.dp.feed_webhook_update(self.bot, _callback_update(103, CLIENT_A_ID, "tkt_view:1"))
        client_texts_2 = _sent_texts_to(self._bot_call, CLIENT_A_ID)
        self.assertFalse(any("Клиент грубит по телефону" in t for t in client_texts_2))


class RepairTrackerNonAdminAccessTests(unittest.IsolatedAsyncioTestCase):
    """Admin-only surfaces (ticket browsing/status changes/notes/admin
    management) must be inert for a non-admin caller, not just hidden from
    their menu — a crafted callback_data must not work either."""

    async def asyncSetUp(self):
        self._bot_call = AsyncMock(return_value=MagicMock())
        self._bot_call_patcher = patch.object(Bot, "__call__", new=self._bot_call)
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = repair_tracker.config_from_bot_row(
            {"bot_id": 959, "name": "repair_nonadmin_bot", "display_name": None, "group_chat_id": None}, self.data_dir,
        )
        await repair_tracker.init_db(self.config.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)
        await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/start"))
        await _create_ticket_via_flow(self.dp, self.bot, CLIENT_A_ID, "Утюг", "Не греет", None, 10)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_non_admin_cannot_browse_active_tickets(self):
        await self.dp.feed_webhook_update(self.bot, _callback_update(100, CLIENT_A_ID, "atkt_active"))
        texts = _sent_texts_to(self._bot_call, CLIENT_A_ID)
        self.assertFalse(any("Активные заявки" in t for t in texts))

    async def test_non_admin_cannot_change_status(self):
        await self.dp.feed_webhook_update(self.bot, _callback_update(100, CLIENT_A_ID, "atkt_status:1:diagnosing"))
        conn = sqlite3.connect(self.config.db_path)
        status = conn.execute("SELECT status FROM repair_tickets WHERE id=1").fetchone()[0]
        conn.close()
        self.assertEqual(status, "new", "a non-admin was able to change a ticket's status")

    async def test_non_admin_cannot_open_admin_management(self):
        await self.dp.feed_webhook_update(self.bot, _callback_update(100, CLIENT_A_ID, "adm_menu"))
        texts = _sent_texts_to(self._bot_call, CLIENT_A_ID)
        self.assertFalse(any("Администраторы бота" in t for t in texts))


class RepairTrackerAdminBootstrapSecurityTests(unittest.IsolatedAsyncioTestCase):
    """Security fix: previously, whoever sent /start FIRST permanently became
    the bot admin — a client testing the bot link before the owner did would
    silently seize the admin panel. See tests/test_shop_catalog_isolation.py
    for the original of this fix, applied identically here."""

    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

        self._central_db_path = self.data_dir / "central_bots.db"
        self._db_path_patcher = patch.object(db_module, "DB_PATH", self._central_db_path)
        self._db_path_patcher.start()
        await db_module.init_db()

    async def asyncTearDown(self):
        self._db_path_patcher.stop()
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_non_owner_messaging_first_does_not_become_admin(self):
        config = repair_tracker.config_from_bot_row(
            {"bot_id": 906, "name": "repair_bot_owned", "display_name": None,
             "group_chat_id": None, "owner_telegram_id": 12345},
            self.data_dir,
        )
        await repair_tracker.init_db(config.db_path)
        bot, dp = _build_bot_dispatcher(config)

        CLIENT_ID = 555  # not the owner, messages first
        await dp.feed_webhook_update(bot, _text_update(1, CLIENT_ID, "/start"))
        self.assertEqual(repair_tracker._load_admins(config.admins_file), set())
        self.assertFalse(repair_tracker._is_admin(CLIENT_ID, config))

        await dp.feed_webhook_update(bot, _text_update(2, 12345, "/start"))
        self.assertTrue(repair_tracker._is_admin(12345, config))
        self.assertEqual(repair_tracker._load_admins(config.admins_file), {"12345"})

    async def test_owner_is_always_admin_even_with_stale_admins_file(self):
        config = repair_tracker.config_from_bot_row(
            {"bot_id": 907, "name": "repair_bot_owned_2", "display_name": None,
             "group_chat_id": None, "owner_telegram_id": 777},
            self.data_dir,
        )
        await repair_tracker.init_db(config.db_path)
        repair_tracker._save_admins(config.admins_file, {"999999"})  # some other id, not the owner
        self.assertTrue(repair_tracker._is_admin(777, config))  # owner: always admin
        self.assertTrue(repair_tracker._is_admin(999999, config))  # still honors the file's own admin
        self.assertFalse(repair_tracker._is_admin(4242, config))  # neither owner nor in the file

    async def test_bootstrap_admin_syncs_to_central_bot_admins_table(self):
        config = repair_tracker.config_from_bot_row(
            {"bot_id": 908, "name": "repair_bot_synced", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await repair_tracker.init_db(config.db_path)
        bot, dp = _build_bot_dispatcher(config)
        await dp.feed_webhook_update(bot, _text_update(1, 321, "/start"))

        central_admins = await db_module.get_bot_admins(908)
        self.assertEqual(central_admins, ["321"])


class RepairTrackerStandaloneSmokeTest(unittest.TestCase):
    def test_config_from_env_matches_legacy_constant_shape(self):
        config = repair_tracker.config_from_env()
        self.assertTrue(config.db_path.endswith("repair_tracker_data.db"))
        self.assertEqual(config.bot_name, "repair_tracker")

    def test_router_and_main_entrypoint_exist(self):
        self.assertTrue(hasattr(repair_tracker, "router"))
        self.assertTrue(hasattr(repair_tracker, "main"))


if __name__ == "__main__":
    unittest.main()
