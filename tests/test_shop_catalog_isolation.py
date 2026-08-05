"""Data isolation, cart accumulation/persistence, checkout double-tap safety,
and menu/button-only-UI tests for the shop_catalog template.

Standard criterion (same as test_booking_fitness_isolation.py): two bots on
the SAME template, different config, must never mix data — even driven by
the SAME Telegram user_id. Everything lives in a tempfile.TemporaryDirectory,
never data/bots.db.

No real Telegram network calls, no real tokens.

Run with: python -m unittest tests.test_shop_catalog_isolation
"""

from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from runtime.registry import get_template_router
from templates import shop_catalog as sc

FAKE_TOKEN = "123456:test-token-not-real"


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


def _callback_update(update_id: int, user_id: int, data: str, msg_id: int = 1) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": str(update_id),
            "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
            "message": {
                "message_id": msg_id, "date": 1700000000,
                "chat": {"id": user_id, "type": "private"}, "text": "placeholder",
            },
            "chat_instance": "1", "data": data,
        },
    }


def _photo_callback_update(update_id: int, user_id: int, data: str, msg_id: int = 1) -> dict:
    """Same as a normal callback update, but the message being reacted to is a
    PHOTO message (photo_file_id set) — the shape cb_product_detail's photo
    branch produces via answer_photo()."""
    return {
        "update_id": update_id,
        "callback_query": {
            "id": str(update_id),
            "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
            "message": {
                "message_id": msg_id, "date": 1700000000,
                "chat": {"id": user_id, "type": "private"},
                "caption": "placeholder",
                "photo": [{"file_id": "fake_file_id", "file_unique_id": "fake_unique", "width": 10, "height": 10}],
            },
            "chat_instance": "1", "data": data,
        },
    }


def _build_bot_dispatcher(config: sc.ShopCatalogConfig) -> tuple[Bot, Dispatcher]:
    bot = Bot(token=FAKE_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(sc.ConfigMiddleware(config))
    dp.include_router(get_template_router("shop_catalog"))
    return bot, dp


async def _seed_category_and_product(db_path: str, price: int = 500) -> tuple[int, int]:
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute("INSERT INTO categories (name) VALUES ('Test Category')")
        category_id = cur.lastrowid
        cur = await db.execute(
            "INSERT INTO products (category_id, name, description, price) VALUES (?, 'Test Product', 'desc', ?)",
            (category_id, price),
        )
        product_id = cur.lastrowid
        await db.commit()
    return category_id, product_id


class ShopCatalogIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

        self.config_a = sc.config_from_bot_row(
            {"bot_id": 601, "name": "shop_bot_a", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        self.config_b = sc.config_from_bot_row(
            {"bot_id": 602, "name": "shop_bot_b", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await sc.init_db(self.config_a.db_path)
        await sc.init_db(self.config_b.db_path)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_configs_point_to_different_files(self):
        self.assertNotEqual(self.config_a.db_path, self.config_b.db_path)
        self.assertNotEqual(self.config_a.admins_file, self.config_b.admins_file)

    async def test_cart_isolated_across_bots_same_user(self):
        """Same Telegram user_id has DIFFERENT cart contents on each bot — the
        classic cross-bot leak this whole test class exists to catch."""
        SAME_USER = 555
        _, product_a = await _seed_category_and_product(self.config_a.db_path, price=100)
        _, product_b = await _seed_category_and_product(self.config_b.db_path, price=200)

        await sc._add_to_cart(self.config_a.db_path, SAME_USER, product_a)
        await sc._add_to_cart(self.config_a.db_path, SAME_USER, product_a)  # qty=2 on bot A
        await sc._add_to_cart(self.config_b.db_path, SAME_USER, product_b)  # qty=1 on bot B

        rows_a = await sc._cart_rows(self.config_a.db_path, SAME_USER)
        rows_b = await sc._cart_rows(self.config_b.db_path, SAME_USER)

        self.assertEqual(len(rows_a), 1)
        self.assertEqual(rows_a[0]["qty"], 2)
        self.assertEqual(sc._cart_total(rows_a), 200)

        self.assertEqual(len(rows_b), 1)
        self.assertEqual(rows_b[0]["qty"], 1)
        self.assertEqual(sc._cart_total(rows_b), 200)

    async def test_orders_isolated_across_bots(self):
        SAME_USER = 777
        await _seed_category_and_product(self.config_a.db_path, price=300)
        await _seed_category_and_product(self.config_b.db_path, price=900)
        product_a = (await sc._active_products_admin(self.config_a.db_path))[0]["id"]
        product_b = (await sc._active_products_admin(self.config_b.db_path))[0]["id"]
        await sc._add_to_cart(self.config_a.db_path, SAME_USER, product_a)
        await sc._add_to_cart(self.config_b.db_path, SAME_USER, product_b)

        result_a = await sc._create_order(self.config_a.db_path, SAME_USER, "Client", "+7 900 000-00-01")
        result_b = await sc._create_order(self.config_b.db_path, SAME_USER, "Client", "+7 900 000-00-02")

        self.assertTrue(result_a["ok"]); self.assertTrue(result_b["ok"])
        self.assertEqual(result_a["total"], 300)
        self.assertEqual(result_b["total"], 900)

        orders_a = await sc._user_orders(self.config_a.db_path, SAME_USER)
        orders_b = await sc._user_orders(self.config_b.db_path, SAME_USER)
        self.assertEqual(len(orders_a), 1)
        self.assertEqual(len(orders_b), 1)

    async def test_admin_bootstrap_isolated_per_bot(self):
        bot_a, dp_a = _build_bot_dispatcher(self.config_a)
        bot_b, dp_b = _build_bot_dispatcher(self.config_b)
        await dp_a.feed_webhook_update(bot_a, _text_update(1, 111, "/start"))
        await dp_b.feed_webhook_update(bot_b, _text_update(1, 999, "/start"))

        self.assertEqual(sc._load_admins(self.config_a.admins_file), {"111"})
        self.assertEqual(sc._load_admins(self.config_b.admins_file), {"999"})


class CartAndCheckoutTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = sc.config_from_bot_row(
            {"bot_id": 701, "name": "shop_cart_bot", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await sc.init_db(self.config.db_path)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_cart_accumulates_across_multiple_adds(self):
        """Owner requirement: the cart accumulates positions until checkout —
        not a one-shot pick."""
        _, product_id = await _seed_category_and_product(self.config.db_path, price=150)
        await sc._add_to_cart(self.config.db_path, 1, product_id)
        await sc._add_to_cart(self.config.db_path, 1, product_id)
        await sc._add_to_cart(self.config.db_path, 1, product_id)

        rows = await sc._cart_rows(self.config.db_path, 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["qty"], 3)
        self.assertEqual(sc._cart_total(rows), 450)

    async def test_cart_survives_a_fresh_connection_reused_process(self):
        """Cart must be a DB row, not FSM/MemoryStorage state — this is what
        lets it be reconstructed identically from a brand-new aiosqlite
        connection, i.e. what would happen across a process restart."""
        _, product_id = await _seed_category_and_product(self.config.db_path, price=250)
        await sc._add_to_cart(self.config.db_path, 42, product_id)

        conn = sqlite3.connect(self.config.db_path)
        row = conn.execute("SELECT qty FROM cart_items WHERE user_id=42 AND product_id=?", (product_id,)).fetchone()
        conn.close()
        self.assertEqual(row[0], 1)

    async def test_decrement_below_one_removes_line(self):
        _, product_id = await _seed_category_and_product(self.config.db_path)
        await sc._add_to_cart(self.config.db_path, 1, product_id)
        await sc._change_cart_qty(self.config.db_path, 1, product_id, -1)
        rows = await sc._cart_rows(self.config.db_path, 1)
        self.assertEqual(rows, [])

    async def test_checkout_creates_order_snapshot_and_empties_cart(self):
        _, product_id = await _seed_category_and_product(self.config.db_path, price=400)
        await sc._add_to_cart(self.config.db_path, 1, product_id)
        await sc._add_to_cart(self.config.db_path, 1, product_id)  # qty=2

        result = await sc._create_order(self.config.db_path, 1, "Ivan", "+7 900 000-00-00")
        self.assertTrue(result["ok"])
        self.assertEqual(result["total"], 800)

        cart_after = await sc._cart_rows(self.config.db_path, 1)
        self.assertEqual(cart_after, [])

        items = await sc._order_items(self.config.db_path, result["order_id"])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["qty"], 2)
        self.assertEqual(items[0]["price_snapshot"], 400)

    async def test_checkout_double_tap_does_not_duplicate_order(self):
        """Review-anticipated double-tap regression: two near-simultaneous
        _create_order calls for the same user must produce exactly ONE order,
        not two, since Telegram does not auto-clear an inline keyboard just
        because the message changed."""
        _, product_id = await _seed_category_and_product(self.config.db_path, price=100)
        await sc._add_to_cart(self.config.db_path, 1, product_id)

        results = await asyncio.gather(
            sc._create_order(self.config.db_path, 1, "A", "+7 900 000-00-01"),
            sc._create_order(self.config.db_path, 1, "A", "+7 900 000-00-01"),
        )
        oks = [r for r in results if r["ok"]]
        empties = [r for r in results if not r["ok"]]
        self.assertEqual(len(oks), 1)
        self.assertEqual(len(empties), 1)
        self.assertEqual(empties[0]["error"], "cart_empty")

        orders = await sc._user_orders(self.config.db_path, 1)
        self.assertEqual(len(orders), 1)

    async def test_price_edit_does_not_retroactively_change_past_order(self):
        """order_items snapshots name/price at checkout time — a later admin
        price edit must not rewrite history."""
        _, product_id = await _seed_category_and_product(self.config.db_path, price=100)
        await sc._add_to_cart(self.config.db_path, 1, product_id)
        result = await sc._create_order(self.config.db_path, 1, "A", "+7 900 000-00-01")

        async with aiosqlite.connect(self.config.db_path) as db:
            await db.execute("UPDATE products SET price=999 WHERE id=?", (product_id,))
            await db.commit()

        items = await sc._order_items(self.config.db_path, result["order_id"])
        self.assertEqual(items[0]["price_snapshot"], 100)

    async def test_deactivated_category_hidden_from_client_catalog(self):
        category_id, _ = await _seed_category_and_product(self.config.db_path)
        async with aiosqlite.connect(self.config.db_path) as db:
            await db.execute("UPDATE categories SET active=0 WHERE id=?", (category_id,))
            await db.commit()
        cats = await sc._active_categories(self.config.db_path)
        self.assertEqual(cats, [])

    async def test_product_deactivated_after_being_cart_is_excluded_from_cart_and_checkout(self):
        """Review-found: a product an admin deactivates AFTER a buyer already
        added it to their cart must not remain checkout-able — the cart must
        match every client-facing catalog query, which already hides inactive
        products."""
        _, product_id = await _seed_category_and_product(self.config.db_path, price=250)
        await sc._add_to_cart(self.config.db_path, 1, product_id)

        async with aiosqlite.connect(self.config.db_path) as db:
            await db.execute("UPDATE products SET active=0 WHERE id=?", (product_id,))
            await db.commit()

        rows = await sc._cart_rows(self.config.db_path, 1)
        self.assertEqual(rows, [])

        result = await sc._create_order(self.config.db_path, 1, "A", "+7 900 000-00-01")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "cart_empty")


class ButtonOnlyUITests(unittest.IsolatedAsyncioTestCase):
    """Owner-mandated permanent design rule: no raw text command lists — every
    action is a clickable button. Confirms /start's menu is buttons-only and
    that the admin button is gated to actual admins."""

    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._mock_call = self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = sc.config_from_bot_row(
            {"bot_id": 801, "name": "shop_ui_bot", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await sc.init_db(self.config.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_start_menu_is_buttons_not_text_commands(self):
        await self.dp.feed_webhook_update(self.bot, _text_update(1, 42, "/start"))
        sent = [
            call.args[0] for call in self._mock_call.call_args_list
            if call.args and hasattr(call.args[0], "reply_markup") and call.args[0].reply_markup
        ]
        found_catalog = found_cart = found_orders = False
        for method in sent:
            for row in method.reply_markup.keyboard:
                for btn in row:
                    if btn.text == "🛍 Каталог":
                        found_catalog = True
                    if btn.text == "🛒 Корзина":
                        found_cart = True
                    if btn.text == "📦 Мои заказы":
                        found_orders = True
        self.assertTrue(found_catalog)
        self.assertTrue(found_cart)
        self.assertTrue(found_orders)

    async def test_admin_panel_hidden_from_non_admin(self):
        await self.dp.feed_webhook_update(self.bot, _text_update(1, 42, "/start"))  # 42 becomes first admin
        self._mock_call.reset_mock()
        await self.dp.feed_webhook_update(self.bot, _text_update(2, 99, "/start"))  # 99 is a plain user
        sent = [
            call.args[0] for call in self._mock_call.call_args_list
            if call.args and hasattr(call.args[0], "reply_markup") and call.args[0].reply_markup
        ]
        found_admin_btn = any(
            btn.text == "⚙️ Панель администратора"
            for method in sent for row in method.reply_markup.keyboard for btn in row
        )
        self.assertFalse(found_admin_btn)


class ReviewFixRegressionTests(unittest.IsolatedAsyncioTestCase):
    """Review-found: tapping "◀️ Назад" on a product detail screen that HAS a
    photo (cb_product_detail's photo branch sends a photo message, not an
    edited text message) must not call editMessageText on that photo message
    — Telegram's editMessageText cannot turn a photo message into a text
    message, so cb_product_back must delete+resend in that case instead."""

    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._mock_call = self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = sc.config_from_bot_row(
            {"bot_id": 901, "name": "shop_regression_bot", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await sc.init_db(self.config.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_back_from_photo_product_detail_deletes_and_resends_not_edits_text(self):
        category_id, _ = await _seed_category_and_product(self.config.db_path)
        update = _photo_callback_update(1, 555, f"shop_prod_back:{category_id}")
        await self.dp.feed_webhook_update(self.bot, update)

        called_methods = [call.args[0].__class__.__name__ for call in self._mock_call.call_args_list if call.args]
        self.assertNotIn("EditMessageText", called_methods)
        self.assertIn("DeleteMessage", called_methods)
        self.assertIn("SendMessage", called_methods)

    async def test_admin_order_status_change_updates_status_and_renders_new_panel(self):
        """Review-found blocker: cb_adm_order_status used to call
        cb_adm_order_detail(cb, ...) directly, reusing the SAME CallbackQuery
        — a second cb.answer() for the same callback_query_id AND re-parsing
        cb.data as "shop_adm_order:<id>" while it actually held
        "shop_adm_order_status:<id>:<status>" (int("<id>:<status>") raising).
        The order status DID get updated (that write happens before the
        crash), but the admin never saw a working panel afterward. This test
        asserts the fixed behavior: status updates AND the follow-up panel
        message (proving _render_order_detail actually completed, not just
        the DB write) is sent."""
        ADMIN_ID = 555
        await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/start"))
        _, product_id = await _seed_category_and_product(self.config.db_path, price=300)
        await sc._add_to_cart(self.config.db_path, ADMIN_ID, product_id)
        order = await sc._create_order(self.config.db_path, ADMIN_ID, "Admin", "+7 900 000-00-01")
        self.assertTrue(order["ok"])

        self._mock_call.reset_mock()
        update = _callback_update(2, ADMIN_ID, f"shop_adm_order_status:{order['order_id']}:processing")
        await self.dp.feed_webhook_update(self.bot, update)

        updated_order = await sc._order_row(self.config.db_path, order["order_id"])
        self.assertEqual(updated_order["status"], "processing")

        sent_texts = [
            call.args[0].text for call in self._mock_call.call_args_list
            if call.args and call.args[0].__class__.__name__ == "SendMessage"
        ]
        self.assertTrue(
            any("В обработке" in t and f"№{order['order_id']}" in t for t in sent_texts),
            f"expected a re-rendered order panel showing the new status, got: {sent_texts}",
        )


if __name__ == "__main__":
    unittest.main()
