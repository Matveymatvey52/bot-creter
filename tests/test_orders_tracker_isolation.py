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

import db.database as db_module
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


class OrdersTrackerSheetsIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """features/sheets.py integration: a status change writes a row to the
    connected spreadsheet (if any), and a "📊 Таблица заказов" main-menu
    button is shown/hidden based on whether bot_sheets_config has a row for
    this bot_id. get_bot_sheets_config/write_row are patched at the
    templates.orders_tracker module level (where they were imported) rather
    than exercising real gspread/network calls — same boundary
    test_sheets_module.py/test_sheets_connect_flow.py already draw."""

    async def asyncSetUp(self):
        self._bot_call = AsyncMock(return_value=MagicMock())
        self._bot_call_patcher = patch.object(Bot, "__call__", new=self._bot_call)
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = orders_tracker.config_from_bot_row(
            {"bot_id": 905, "name": "orders_sheets_bot", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await orders_tracker.init_db(self.config.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)
        await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/start"))

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def _order_id(self) -> int:
        conn = sqlite3.connect(self.config.db_path)
        order_id = conn.execute("SELECT id FROM orders ORDER BY id DESC LIMIT 1").fetchone()[0]
        conn.close()
        return order_id

    def _order_status(self, order_id: int) -> str:
        conn = sqlite3.connect(self.config.db_path)
        status = conn.execute("SELECT status FROM orders WHERE id=?", (order_id,)).fetchone()[0]
        conn.close()
        return status

    def _last_markup_to(self, chat_id: int):
        for call in reversed(self._bot_call.call_args_list):
            request = call.args[0] if call.args else None
            if getattr(request, "chat_id", None) == chat_id:
                markup = getattr(request, "reply_markup", None)
                if markup is not None:
                    return markup
        return None

    @patch("templates.orders_tracker.write_row", new_callable=AsyncMock)
    @patch("templates.orders_tracker.get_bot_sheets_config", new_callable=AsyncMock)
    async def test_status_change_writes_row_when_sheets_connected(self, mock_get_config, mock_write_row):
        mock_get_config.return_value = {"spreadsheet_id": "SHEET123", "sheet_title": "Orders", "connected_at": "now"}
        await _create_order_via_flow(self.dp, self.bot, ADMIN_ID, PHONE, "Widget", "1", 10, customer_name="Ann")
        order_id = await self._order_id()

        await self.dp.feed_webhook_update(
            self.bot, _callback_update(100, ADMIN_ID, f"ord_status:{order_id}:in_progress")
        )

        mock_write_row.assert_awaited_once()
        bot_id, worksheet, row = mock_write_row.call_args.args
        self.assertEqual(bot_id, self.config.bot_id)
        self.assertEqual(worksheet, orders_tracker.SHEETS_WORKSHEET)
        self.assertEqual(row[0], order_id)
        self.assertEqual(row[1], orders_tracker.STATUS_LABELS["in_progress"])
        self.assertEqual(row[2], "Ann")
        self.assertEqual(row[3], orders_tracker._normalize_phone(PHONE))

    @patch("templates.orders_tracker.write_row", new_callable=AsyncMock)
    @patch("templates.orders_tracker.get_bot_sheets_config", new_callable=AsyncMock)
    async def test_status_change_does_not_write_when_sheets_not_connected(self, mock_get_config, mock_write_row):
        mock_get_config.return_value = None
        await _create_order_via_flow(self.dp, self.bot, ADMIN_ID, PHONE, "Widget", "1", 10)
        order_id = await self._order_id()

        await self.dp.feed_webhook_update(
            self.bot, _callback_update(100, ADMIN_ID, f"ord_status:{order_id}:in_progress")
        )

        mock_write_row.assert_not_awaited()
        self.assertEqual(self._order_status(order_id), "in_progress")

    @patch("templates.orders_tracker.write_row", new_callable=AsyncMock)
    @patch("templates.orders_tracker.get_bot_sheets_config", new_callable=AsyncMock)
    async def test_sheets_write_failure_does_not_break_status_change(self, mock_get_config, mock_write_row):
        mock_get_config.return_value = {"spreadsheet_id": "SHEET123", "sheet_title": "Orders", "connected_at": "now"}
        mock_write_row.side_effect = RuntimeError("gspread boom")
        await _create_order_via_flow(self.dp, self.bot, ADMIN_ID, PHONE, "Widget", "1", 10)
        order_id = await self._order_id()

        # Must not raise/propagate — the status change is committed before
        # the sheets write is even attempted.
        await self.dp.feed_webhook_update(
            self.bot, _callback_update(100, ADMIN_ID, f"ord_status:{order_id}:in_progress")
        )

        self.assertEqual(self._order_status(order_id), "in_progress")

    @patch("templates.orders_tracker.get_bot_sheets_config", new_callable=AsyncMock)
    async def test_menu_shows_sheet_button_only_when_connected(self, mock_get_config):
        mock_get_config.return_value = {"spreadsheet_id": "SHEET123", "sheet_title": "Orders", "connected_at": "now"}
        await self.dp.feed_webhook_update(self.bot, _callback_update(50, ADMIN_ID, "main_menu"))
        markup = self._last_markup_to(ADMIN_ID)
        self.assertTrue(any(
            btn.callback_data == "ord_sheet_link" for row in markup.inline_keyboard for btn in row
        ), "sheet-connected bot did not show the '📊 Таблица заказов' menu button")

        mock_get_config.return_value = None
        await self.dp.feed_webhook_update(self.bot, _callback_update(51, ADMIN_ID, "main_menu"))
        markup = self._last_markup_to(ADMIN_ID)
        self.assertFalse(any(
            btn.callback_data == "ord_sheet_link" for row in markup.inline_keyboard for btn in row
        ), "sheet-disconnected bot still showed the '📊 Таблица заказов' menu button")

    @patch("templates.orders_tracker.get_bot_sheets_config", new_callable=AsyncMock)
    async def test_sheet_link_button_sends_spreadsheet_url(self, mock_get_config):
        mock_get_config.return_value = {"spreadsheet_id": "SHEET123", "sheet_title": "Orders", "connected_at": "now"}
        await self.dp.feed_webhook_update(self.bot, _callback_update(60, ADMIN_ID, "ord_sheet_link"))

        sent_urls = [
            getattr(call.args[0], "text", "")
            for call in self._bot_call.call_args_list
            if getattr(call.args[0], "chat_id", None) == ADMIN_ID
        ]
        self.assertTrue(
            any("docs.google.com/spreadsheets/d/SHEET123" in text for text in sent_urls),
            f"no message contained the spreadsheet link: {sent_urls}",
        )

    @patch("templates.orders_tracker.get_bot_sheets_config", new_callable=AsyncMock)
    async def test_sheet_link_tap_after_disconnect_reraces_to_main_menu(self, mock_get_config):
        """The button is only ever rendered when connected (see
        test_menu_shows_sheet_button_only_when_connected), but a tap can still
        land after the owner disconnects the sheet in between render and tap
        — cb_ord_sheet_link must re-render the main menu, not send a broken
        link or crash."""
        mock_get_config.return_value = None
        await self.dp.feed_webhook_update(self.bot, _callback_update(60, ADMIN_ID, "ord_sheet_link"))

        sent_texts = [
            getattr(call.args[0], "text", "")
            for call in self._bot_call.call_args_list
            if getattr(call.args[0], "chat_id", None) == ADMIN_ID
        ]
        self.assertFalse(any("docs.google.com" in text for text in sent_texts))

    @patch("templates.orders_tracker.write_row", new_callable=AsyncMock)
    @patch("templates.orders_tracker.get_bot_sheets_config", new_callable=AsyncMock)
    async def test_customer_name_starting_with_equals_is_neutralized_before_writing(
        self, mock_get_config, mock_write_row
    ):
        """Spreadsheet-formula-injection guard: a customer name an admin typed
        as free text must never reach write_row able to be interpreted as a
        Sheets formula by whoever later opens the connected spreadsheet."""
        mock_get_config.return_value = {"spreadsheet_id": "SHEET123", "sheet_title": "Orders", "connected_at": "now"}
        await _create_order_via_flow(
            self.dp, self.bot, ADMIN_ID, PHONE, "Widget", "1", 10,
            customer_name='=HYPERLINK("http://evil.example","click")',
        )
        order_id = await self._order_id()

        await self.dp.feed_webhook_update(
            self.bot, _callback_update(100, ADMIN_ID, f"ord_status:{order_id}:in_progress")
        )

        mock_write_row.assert_awaited_once()
        _, _, row = mock_write_row.call_args.args
        self.assertFalse(row[2].startswith("="), f"formula-triggering cell was NOT neutralized: {row[2]!r}")
        self.assertTrue(row[2].startswith("'="), f"expected an apostrophe-escaped formula, got: {row[2]!r}")


class OrdersTrackerPriceValidationTests(unittest.IsolatedAsyncioTestCase):
    """Regression test for the nan-price bug: float("nan") does not raise
    ValueError, and NaN comparisons are always False, so "nan"/"NaN"/"-nan"
    used to slip past _parse_price's bounds check and get stored as an order
    item's price, poisoning every downstream SUM(qty*price) total. Same
    defect, same fix (math.isfinite), as vehicle_service.py's _parse_price."""

    async def asyncSetUp(self):
        self._bot_call = AsyncMock(return_value=MagicMock())
        self._bot_call_patcher = patch.object(Bot, "__call__", new=self._bot_call)
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = orders_tracker.config_from_bot_row(
            {"bot_id": 906, "name": "orders_price_bot", "display_name": None, "group_chat_id": None}, self.data_dir
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

    def test_parse_price_rejects_nan_variants_without_raising(self):
        for text in ("nan", "NaN", "-nan", "NAN", "inf", "-inf", "Infinity"):
            with self.subTest(text=text):
                self.assertIsNone(orders_tracker._parse_price(text))

    async def test_nan_price_input_is_rejected_with_guidance_and_not_saved(self):
        """Drives the real FSM up to the price step and types "nan" — must not
        raise, must re-prompt with the same "enter a number" guidance as any
        other bad input, and the item must never be persisted with that
        price."""
        uid = 10
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, ADMIN_ID, "ord_new")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _text_update(uid, ADMIN_ID, PHONE)); uid += 1
        await self.dp.feed_webhook_update(self.bot, _text_update(uid, ADMIN_ID, "Test Customer")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _text_update(uid, ADMIN_ID, "Widget")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _text_update(uid, ADMIN_ID, "1")); uid += 1

        await self.dp.feed_webhook_update(self.bot, _text_update(uid, ADMIN_ID, "nan")); uid += 1

        texts = self._sent_texts_to(ADMIN_ID)
        self.assertIn("Введите число, например: 199.90, или нажмите «Без цены»", texts)
        # Confirm the FSM did NOT advance past the price step: finishing the
        # item now (skip price) must produce exactly one item, not a
        # duplicate/finalized one from the rejected "nan" input.
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, ADMIN_ID, "ord_price_skip")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, ADMIN_ID, "ord_item_done")); uid += 1

        conn = sqlite3.connect(self.config.db_path)
        items = conn.execute("SELECT name, qty, price FROM order_items").fetchall()
        conn.close()
        self.assertEqual(items, [("Widget", 1, None)])


class OrdersTrackerStandaloneSmokeTest(unittest.TestCase):
    def test_config_from_env_matches_legacy_constant_shape(self):
        config = orders_tracker.config_from_env()
        self.assertTrue(config.db_path.endswith("orders_tracker_data.db"))
        self.assertEqual(config.bot_name, "orders_tracker")

    def test_router_and_main_entrypoint_exist(self):
        self.assertTrue(hasattr(orders_tracker, "router"))
        self.assertTrue(hasattr(orders_tracker, "main"))


class OrdersTrackerAdminBootstrapSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

        # cmd_start now syncs the bootstrap admin into db.database.add_bot_admin
        # (central bot_admins table, used by the mini-app's _admin_gate_ok) —
        # must be redirected to a throwaway DB or it hits the real
        # data/bots.db, same reasoning as test_shop_catalog_isolation.py.
        self._central_db_path = self.data_dir / "central_bots.db"
        self._db_path_patcher = patch.object(db_module, "DB_PATH", self._central_db_path)
        self._db_path_patcher.start()
        await db_module.init_db()

    async def asyncTearDown(self):
        self._db_path_patcher.stop()
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_non_owner_messaging_first_does_not_become_admin(self):
        """Security fix: previously, whoever sent /start FIRST permanently
        became the bot admin — a client testing the bot link before the
        owner did would silently seize the admin panel. When
        bots.owner_telegram_id is known, only that user may claim the
        bootstrap admin slot."""
        config = orders_tracker.config_from_bot_row(
            {"bot_id": 903, "name": "orders_bot_owned", "display_name": None,
             "group_chat_id": None, "owner_telegram_id": 12345},
            self.data_dir,
        )
        await orders_tracker.init_db(config.db_path)
        bot, dp = _build_bot_dispatcher(config)

        CLIENT_ID = 555  # not the owner, messages first
        await dp.feed_webhook_update(bot, _text_update(1, CLIENT_ID, "/start"))
        self.assertEqual(orders_tracker._load_admins(config.admins_file), set())
        self.assertFalse(orders_tracker._is_admin(CLIENT_ID, config))

        await dp.feed_webhook_update(bot, _text_update(2, 12345, "/start"))
        self.assertTrue(orders_tracker._is_admin(12345, config))
        self.assertEqual(orders_tracker._load_admins(config.admins_file), {"12345"})

    async def test_owner_is_always_admin_even_with_stale_admins_file(self):
        """Defense in depth: the DB-known owner must see the admin panel even
        if the local admins_file is empty/stale (e.g. wiped, or hijacked by a
        prior bug) — owner_telegram_id is treated as an unconditional admin
        in _is_admin, not just at bootstrap time."""
        config = orders_tracker.config_from_bot_row(
            {"bot_id": 904, "name": "orders_bot_owned_2", "display_name": None,
             "group_chat_id": None, "owner_telegram_id": 777},
            self.data_dir,
        )
        await orders_tracker.init_db(config.db_path)
        orders_tracker._save_admins(config.admins_file, {"999999"})  # some other id, not the owner
        self.assertTrue(orders_tracker._is_admin(777, config))  # owner: always admin
        self.assertTrue(orders_tracker._is_admin(999999, config))  # still honors the file's own admin
        self.assertFalse(orders_tracker._is_admin(4242, config))  # neither owner nor in the file

    async def test_bootstrap_admin_syncs_to_central_bot_admins_table(self):
        """The mini-app's admin gate (runtime.miniapp_api._admin_gate_ok)
        checks db.database.get_bot_admins(), a separate table from this
        template's local admins_file. The bootstrap grant must land in both,
        or the owner gets the Telegram admin panel but is locked out of the
        mini-app admin views."""
        config = orders_tracker.config_from_bot_row(
            {"bot_id": 905, "name": "orders_bot_synced", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await orders_tracker.init_db(config.db_path)
        bot, dp = _build_bot_dispatcher(config)
        await dp.feed_webhook_update(bot, _text_update(1, 321, "/start"))

        central_admins = await db_module.get_bot_admins(905)
        self.assertEqual(central_admins, ["321"])


if __name__ == "__main__":
    unittest.main()
