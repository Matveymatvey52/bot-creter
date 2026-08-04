"""orders_tracker template — data isolation and customer-notify-on-status-change tests.

Standard criterion: two bots on the SAME template, different config, must
never mix data — even driven by the SAME Telegram admin user_id.

PLUS the design's central differentiator from inventory.py: an order's
customer must be notified automatically when the order's status changes
(if, and only if, their Telegram account is linked), a double-tap on a
status-transition button must not double-notify, and the phone-link security
check (a shared Contact must belong to the sender) must actually reject a
mismatched contact.

No real Telegram network calls, no real tokens.

Run with: python -m unittest tests.test_orders_tracker_isolation
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from runtime.registry import get_template_router
from templates import orders_tracker

FAKE_TOKEN = "123456:test-token-not-real"
ADMIN_ID = 999
CUSTOMER_TG_ID = 555
PHONE = "89991234567"  # normalizes to "+7 (999) 123-45-67"


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


def _contact_update(update_id: int, user_id: int, phone_number: str, contact_user_id: int) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id, "date": 1700000000,
            "chat": {"id": user_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
            "contact": {"phone_number": phone_number, "first_name": "Test", "user_id": contact_user_id},
        },
    }


def _build_bot_dispatcher(config: orders_tracker.OrdersTrackerConfig) -> tuple[Bot, Dispatcher]:
    bot = Bot(token=FAKE_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(orders_tracker.ConfigMiddleware(config))
    dp.include_router(get_template_router("orders_tracker"))
    return bot, dp


async def _create_order_via_flow(
    dp: Dispatcher, bot: Bot, admin_id: int, phone: str, item_name: str, qty: str, start_update_id: int,
    customer_name: str = "Test Customer",
) -> int:
    """Drives the full admin FSM: ord_new -> phone -> (customer name, since the
    phone is never pre-registered by these tests, so the "customer not found"
    branch always fires on the first order for a given phone) -> item name ->
    qty -> skip price -> finish. Returns the next free update_id."""
    uid = start_update_id
    await dp.feed_webhook_update(bot, _callback_update(uid, admin_id, "ord_new")); uid += 1
    await dp.feed_webhook_update(bot, _text_update(uid, admin_id, phone)); uid += 1
    await dp.feed_webhook_update(bot, _text_update(uid, admin_id, customer_name)); uid += 1
    await dp.feed_webhook_update(bot, _text_update(uid, admin_id, item_name)); uid += 1
    await dp.feed_webhook_update(bot, _text_update(uid, admin_id, qty)); uid += 1
    await dp.feed_webhook_update(bot, _callback_update(uid, admin_id, "ord_price_skip")); uid += 1
    await dp.feed_webhook_update(bot, _callback_update(uid, admin_id, "ord_item_done")); uid += 1
    return uid


class OrdersTrackerIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

        self.config_a = orders_tracker.config_from_bot_row(
            {"bot_id": 901, "name": "orders_isolation_bot_a", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        self.config_b = orders_tracker.config_from_bot_row(
            {"bot_id": 902, "name": "orders_isolation_bot_b", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await orders_tracker.init_db(self.config_a.db_path)
        await orders_tracker.init_db(self.config_b.db_path)

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

    async def test_two_bots_same_admin_same_phone_orders_not_mixed(self):
        await _create_order_via_flow(self.dp_a, self.bot_a, ADMIN_ID, PHONE, "Widget A", "2", 10)
        await _create_order_via_flow(self.dp_b, self.bot_b, ADMIN_ID, PHONE, "Widget B", "5", 10)

        conn_a = sqlite3.connect(self.config_a.db_path)
        orders_a = conn_a.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        item_a = conn_a.execute("SELECT name, qty FROM order_items").fetchone()
        conn_a.close()
        conn_b = sqlite3.connect(self.config_b.db_path)
        orders_b = conn_b.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        item_b = conn_b.execute("SELECT name, qty FROM order_items").fetchone()
        conn_b.close()

        self.assertEqual(orders_a, 1)
        self.assertEqual(orders_b, 1)
        self.assertEqual(item_a, ("Widget A", 2))
        self.assertEqual(item_b, ("Widget B", 5))


class OrdersTrackerNotifyTests(unittest.IsolatedAsyncioTestCase):
    """Owner-specified differentiator from inventory.py: the customer must be
    notified automatically on a status transition, if and only if linked."""

    async def asyncSetUp(self):
        self._bot_call = AsyncMock(return_value=MagicMock())
        self._bot_call_patcher = patch.object(Bot, "__call__", new=self._bot_call)
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = orders_tracker.config_from_bot_row(
            {"bot_id": 903, "name": "orders_notify_bot", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await orders_tracker.init_db(self.config.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)
        await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/start"))

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    def _sent_texts_to(self, chat_id: int) -> list[str]:
        texts = []
        for call in self._bot_call.call_args_list:
            request = call.args[0] if call.args else None
            text = getattr(request, "text", None)
            cid = getattr(request, "chat_id", None)
            if text and cid == chat_id:
                texts.append(text)
        return texts

    async def test_linked_customer_is_notified_on_status_change(self):
        await _create_order_via_flow(self.dp, self.bot, ADMIN_ID, PHONE, "Widget", "1", 10)
        async with aiosqlite.connect(self.config.db_path) as db:
            await db.execute("UPDATE customers SET telegram_user_id=? WHERE phone=?",
                              (CUSTOMER_TG_ID, orders_tracker._normalize_phone(PHONE)))
            await db.commit()
            order_id = (await (await db.execute("SELECT id FROM orders")).fetchone())[0]

        await self.dp.feed_webhook_update(
            self.bot, _callback_update(100, ADMIN_ID, f"ord_status:{order_id}:in_progress")
        )

        notifications = self._sent_texts_to(CUSTOMER_TG_ID)
        self.assertEqual(len(notifications), 1)
        self.assertIn(str(order_id), notifications[0])

        conn = sqlite3.connect(self.config.db_path)
        notified = conn.execute(
            "SELECT notified FROM order_status_log WHERE order_id=? AND new_status='in_progress'", (order_id,)
        ).fetchone()[0]
        conn.close()
        self.assertEqual(notified, 1)

    async def test_unlinked_customer_is_not_notified_but_status_still_changes(self):
        await _create_order_via_flow(self.dp, self.bot, ADMIN_ID, PHONE, "Widget", "1", 10)
        conn = sqlite3.connect(self.config.db_path)
        order_id = conn.execute("SELECT id FROM orders").fetchone()[0]
        conn.close()

        await self.dp.feed_webhook_update(
            self.bot, _callback_update(100, ADMIN_ID, f"ord_status:{order_id}:in_progress")
        )

        conn = sqlite3.connect(self.config.db_path)
        status, notified = conn.execute(
            "SELECT status, notified FROM orders o JOIN order_status_log l ON l.order_id=o.id WHERE o.id=?",
            (order_id,),
        ).fetchone()
        conn.close()
        self.assertEqual(status, "in_progress")
        self.assertEqual(notified, 0)
        self.assertEqual(self._sent_texts_to(CUSTOMER_TG_ID), [])

    async def test_double_tap_on_status_transition_notifies_only_once(self):
        await _create_order_via_flow(self.dp, self.bot, ADMIN_ID, PHONE, "Widget", "1", 10)
        async with aiosqlite.connect(self.config.db_path) as db:
            await db.execute("UPDATE customers SET telegram_user_id=? WHERE phone=?",
                              (CUSTOMER_TG_ID, orders_tracker._normalize_phone(PHONE)))
            await db.commit()
            order_id = (await (await db.execute("SELECT id FROM orders")).fetchone())[0]

        await self.dp.feed_webhook_update(
            self.bot, _callback_update(100, ADMIN_ID, f"ord_status:{order_id}:in_progress")
        )
        # Stale/duplicate re-tap of the same transition button.
        await self.dp.feed_webhook_update(
            self.bot, _callback_update(101, ADMIN_ID, f"ord_status:{order_id}:in_progress")
        )

        self.assertEqual(len(self._sent_texts_to(CUSTOMER_TG_ID)), 1, "double-tap notified the customer twice")
        conn = sqlite3.connect(self.config.db_path)
        log_rows = conn.execute(
            "SELECT COUNT(*) FROM order_status_log WHERE order_id=? AND new_status='in_progress'", (order_id,)
        ).fetchone()[0]
        conn.close()
        self.assertEqual(log_rows, 1, "double-tap inserted more than one status_log row")

    async def test_forward_only_flow_rejects_skipping_a_stage(self):
        await _create_order_via_flow(self.dp, self.bot, ADMIN_ID, PHONE, "Widget", "1", 10)
        conn = sqlite3.connect(self.config.db_path)
        order_id = conn.execute("SELECT id FROM orders").fetchone()[0]
        conn.close()
        # "new" -> "shipped" is not a legal transition (must pass through in_progress).
        await self.dp.feed_webhook_update(
            self.bot, _callback_update(100, ADMIN_ID, f"ord_status:{order_id}:shipped")
        )
        conn = sqlite3.connect(self.config.db_path)
        status = conn.execute("SELECT status FROM orders WHERE id=?", (order_id,)).fetchone()[0]
        conn.close()
        self.assertEqual(status, "new")


class OrdersTrackerContactLinkSecurityTests(unittest.IsolatedAsyncioTestCase):
    """A shared Contact must belong to the sender — otherwise user A could
    link user B's phone number to their own telegram_user_id and hijack B's
    order-status notifications."""

    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = orders_tracker.config_from_bot_row(
            {"bot_id": 904, "name": "orders_contact_bot", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await orders_tracker.init_db(self.config.db_path)
        async with aiosqlite.connect(self.config.db_path) as db:
            await db.execute(
                "INSERT INTO customers (name, phone) VALUES (?, ?)",
                ("Victim", orders_tracker._normalize_phone(PHONE)),
            )
            await db.commit()
        self.bot, self.dp = _build_bot_dispatcher(self.config)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_contact_belonging_to_someone_else_is_rejected(self):
        attacker_id = 777
        await self.dp.feed_webhook_update(
            self.bot, _contact_update(1, attacker_id, PHONE, contact_user_id=CUSTOMER_TG_ID)
        )
        conn = sqlite3.connect(self.config.db_path)
        linked = conn.execute("SELECT telegram_user_id FROM customers WHERE phone=?",
                               (orders_tracker._normalize_phone(PHONE),)).fetchone()[0]
        conn.close()
        self.assertIsNone(linked, "a contact not belonging to the sender was accepted and linked")

    async def test_own_contact_is_linked(self):
        await self.dp.feed_webhook_update(
            self.bot, _contact_update(1, CUSTOMER_TG_ID, PHONE, contact_user_id=CUSTOMER_TG_ID)
        )
        conn = sqlite3.connect(self.config.db_path)
        linked = conn.execute("SELECT telegram_user_id FROM customers WHERE phone=?",
                               (orders_tracker._normalize_phone(PHONE),)).fetchone()[0]
        conn.close()
        self.assertEqual(linked, CUSTOMER_TG_ID)


class OrdersTrackerStandaloneSmokeTest(unittest.TestCase):
    def test_config_from_env_matches_legacy_constant_shape(self):
        config = orders_tracker.config_from_env()
        self.assertTrue(config.db_path.endswith("orders_tracker_data.db"))
        self.assertEqual(config.bot_name, "orders_tracker")

    def test_router_and_main_entrypoint_exist(self):
        self.assertTrue(hasattr(orders_tracker, "router"))
        self.assertTrue(hasattr(orders_tracker, "main"))


if __name__ == "__main__":
    unittest.main()
