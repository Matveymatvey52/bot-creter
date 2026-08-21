"""food_delivery template — smoke tests covering the full order lifecycle.

Mirrors the harness of tests/test_delivery_tracker.py / tests/test_shop_catalog.py:
fake webhook updates fed through a real Dispatcher, Bot.__call__ patched so no
real Telegram network calls happen, per-bot sqlite files in a tmp dir.

Covers, per the task brief:
- menu browsing (categories -> items, stop-list hides an item from "add to cart")
- cart -> checkout flow (zone/address/time/comment/phone/promo/points/payment)
- minimum order amount enforcement
- full status lifecycle new -> accepted -> cooking -> courier(assign) -> delivered,
  with a client notification at every step
- illegal status transitions rejected
- loyalty points: earned on delivery, spendable on a later order
- promo code discount applied at checkout
- db isolation between two bot instances
- ownership: client B cannot view client A's order
- miniapp_config schema-drift check

No real Telegram network calls, no real tokens.

Run with: python -m unittest tests.test_food_delivery
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from runtime.registry import get_template_router
from templates import food_delivery

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


def _build_bot_dispatcher(config: food_delivery.FoodDeliveryConfig) -> tuple[Bot, Dispatcher]:
    bot = Bot(token=FAKE_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(food_delivery.ConfigMiddleware(config))
    dp.include_router(get_template_router("food_delivery"))
    return bot, dp


def _sent_texts_to(bot_call_mock, chat_id: int) -> list[str]:
    texts = []
    for call in bot_call_mock.call_args_list:
        request = call.args[0] if call.args else None
        text = getattr(request, "text", None)
        cid = getattr(request, "chat_id", None)
        if text and cid == chat_id:
            texts.append(text)
    return texts


def _notification_texts_to(bot_call_mock, chat_id: int) -> list[str]:
    return [t for t in _sent_texts_to(bot_call_mock, chat_id) if t.startswith("🔔")]


async def _seed_menu(dp: Dispatcher, bot: Bot, uid: int, price: int = 600) -> int:
    """Admin adds one category + one item (price >= MIN_ORDER_AMOUNT by
    default so a single unit already clears checkout's minimum). Returns the
    next free update_id."""
    await dp.feed_webhook_update(bot, _callback_update(uid, ADMIN_ID, "fd_adm_menu")); uid += 1
    await dp.feed_webhook_update(bot, _callback_update(uid, ADMIN_ID, "fd_adm_categories")); uid += 1
    await dp.feed_webhook_update(bot, _callback_update(uid, ADMIN_ID, "fd_adm_cat_add")); uid += 1
    await dp.feed_webhook_update(bot, _text_update(uid, ADMIN_ID, "Пицца")); uid += 1
    await dp.feed_webhook_update(bot, _callback_update(uid, ADMIN_ID, "fd_adm_items")); uid += 1
    await dp.feed_webhook_update(bot, _callback_update(uid, ADMIN_ID, "fd_adm_item_add")); uid += 1
    await dp.feed_webhook_update(bot, _callback_update(uid, ADMIN_ID, "fd_adm_item_cat:1")); uid += 1
    await dp.feed_webhook_update(bot, _text_update(uid, ADMIN_ID, "Маргарита")); uid += 1
    await dp.feed_webhook_update(bot, _callback_update(uid, ADMIN_ID, "fd_adm_item_desc_skip")); uid += 1
    await dp.feed_webhook_update(bot, _text_update(uid, ADMIN_ID, str(price))); uid += 1
    await dp.feed_webhook_update(bot, _callback_update(uid, ADMIN_ID, "fd_adm_item_photo_skip")); uid += 1
    return uid


async def _add_item_to_cart(dp: Dispatcher, bot: Bot, client_id: int, uid: int) -> int:
    await dp.feed_webhook_update(bot, _callback_update(uid, client_id, "fd_menu")); uid += 1
    await dp.feed_webhook_update(bot, _callback_update(uid, client_id, "fd_cat:1")); uid += 1
    await dp.feed_webhook_update(bot, _callback_update(uid, client_id, "fd_add:1")); uid += 1
    return uid


async def _checkout_via_flow(
    dp: Dispatcher, bot: Bot, client_id: int, uid: int,
    promo: str | None = None, points: int | None = None, pay: str = "fd_pay_cash",
) -> int:
    """Drives the full client checkout FSM after the cart already has items:
    fd_checkout_start -> zone -> address -> asap -> comment skip -> phone ->
    promo(text or skip) -> points(text or skip) -> payment method -> confirm."""
    await dp.feed_webhook_update(bot, _callback_update(uid, client_id, "fd_checkout_start")); uid += 1
    await dp.feed_webhook_update(bot, _callback_update(uid, client_id, "fd_zone:0")); uid += 1
    await dp.feed_webhook_update(bot, _text_update(uid, client_id, "ул. Ленина 1")); uid += 1
    await dp.feed_webhook_update(bot, _callback_update(uid, client_id, "fd_time_asap")); uid += 1
    await dp.feed_webhook_update(bot, _callback_update(uid, client_id, "fd_comment_skip")); uid += 1
    await dp.feed_webhook_update(bot, _text_update(uid, client_id, "89991234567")); uid += 1
    if promo is not None:
        await dp.feed_webhook_update(bot, _text_update(uid, client_id, promo)); uid += 1
    else:
        await dp.feed_webhook_update(bot, _callback_update(uid, client_id, "fd_promo_skip")); uid += 1
    if points is not None:
        await dp.feed_webhook_update(bot, _text_update(uid, client_id, str(points))); uid += 1
    else:
        await dp.feed_webhook_update(bot, _callback_update(uid, client_id, "fd_points_skip")); uid += 1
    await dp.feed_webhook_update(bot, _callback_update(uid, client_id, pay)); uid += 1
    await dp.feed_webhook_update(bot, _callback_update(uid, client_id, "fd_checkout_confirm")); uid += 1
    return uid


class FoodDeliveryOrderLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._bot_call = AsyncMock(return_value=MagicMock())
        self._bot_call_patcher = patch.object(Bot, "__call__", new=self._bot_call)
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = food_delivery.config_from_bot_row(
            {"bot_id": 2001, "name": "food_delivery_bot", "display_name": None, "group_chat_id": None}, self.data_dir,
        )
        await food_delivery.init_db(self.config.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)
        await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/start"))
        await self.dp.feed_webhook_update(self.bot, _text_update(2, CLIENT_A_ID, "/start"))

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    def _conn(self):
        return sqlite3.connect(self.config.db_path)

    async def test_full_order_lifecycle_with_courier_and_notifications(self):
        uid = await _seed_menu(self.dp, self.bot, 10)
        uid = await _add_item_to_cart(self.dp, self.bot, CLIENT_A_ID, uid)
        uid = await _checkout_via_flow(self.dp, self.bot, CLIENT_A_ID, uid)

        conn = self._conn()
        status = conn.execute("SELECT status FROM orders WHERE id=1").fetchone()[0]
        conn.close()
        self.assertEqual(status, "new")

        # Add a courier so the cooking -> courier transition can complete.
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, ADMIN_ID, "fd_adm_couriers")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, ADMIN_ID, "fd_adm_courier_add")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _text_update(uid, ADMIN_ID, "Иван")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, ADMIN_ID, "fd_adm_courier_phone_skip")); uid += 1

        # Walk the full status chain.
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, ADMIN_ID, "fd_adm_order_status:1:accepted")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, ADMIN_ID, "fd_adm_order_status:1:cooking")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, ADMIN_ID, "fd_adm_order_status:1:courier")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, ADMIN_ID, "fd_courier_assign:1:1")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, ADMIN_ID, "fd_adm_order_status:1:delivered")); uid += 1

        notifications = _notification_texts_to(self._bot_call, CLIENT_A_ID)
        self.assertEqual(len(notifications), 4, f"expected one notification per transition, got: {notifications}")
        self.assertIn("«Принят»", notifications[0])
        self.assertIn("«Готовится»", notifications[1])
        self.assertIn("«У курьера»", notifications[2])
        self.assertIn("«Доставлено»", notifications[3])

        conn = self._conn()
        status, courier_id, points_earned = conn.execute(
            "SELECT status, courier_id, points_earned FROM orders WHERE id=1"
        ).fetchone()
        conn.close()
        self.assertEqual(status, "delivered")
        self.assertEqual(courier_id, 1)
        self.assertEqual(points_earned, 6)  # 600 total // 100 rate

    async def test_illegal_transition_new_to_courier_skip_stage_rejected(self):
        uid = await _seed_menu(self.dp, self.bot, 10)
        uid = await _add_item_to_cart(self.dp, self.bot, CLIENT_A_ID, uid)
        uid = await _checkout_via_flow(self.dp, self.bot, CLIENT_A_ID, uid)

        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, ADMIN_ID, "fd_adm_order_status:1:courier"))
        conn = self._conn()
        status = conn.execute("SELECT status FROM orders WHERE id=1").fetchone()[0]
        conn.close()
        self.assertEqual(status, "new")
        self.assertEqual(_notification_texts_to(self._bot_call, CLIENT_A_ID), [])

    async def test_cancelled_reachable_from_new(self):
        uid = await _seed_menu(self.dp, self.bot, 10)
        uid = await _add_item_to_cart(self.dp, self.bot, CLIENT_A_ID, uid)
        uid = await _checkout_via_flow(self.dp, self.bot, CLIENT_A_ID, uid)

        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, ADMIN_ID, "fd_adm_order_status:1:cancelled"))
        notifications = _notification_texts_to(self._bot_call, CLIENT_A_ID)
        self.assertEqual(len(notifications), 1)
        self.assertIn("«Отменён»", notifications[0])

    async def test_min_order_amount_enforced(self):
        # Item priced BELOW MIN_ORDER_AMOUNT — checkout must be refused.
        uid = await _seed_menu(self.dp, self.bot, 10, price=food_delivery.MIN_ORDER_AMOUNT - 1)
        uid = await _add_item_to_cart(self.dp, self.bot, CLIENT_A_ID, uid)
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, CLIENT_A_ID, "fd_checkout_start")); uid += 1

        conn = self._conn()
        count = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        conn.close()
        self.assertEqual(count, 0)
        texts = _sent_texts_to(self._bot_call, CLIENT_A_ID)
        self.assertTrue(any("Минимальная сумма заказа" in t for t in texts))

    async def test_stop_list_item_cannot_be_added_to_cart(self):
        uid = await _seed_menu(self.dp, self.bot, 10)
        # Toggle the item off the stop-list ("В наличии" -> off).
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, ADMIN_ID, "fd_adm_item_stop:1")); uid += 1

        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, CLIENT_A_ID, "fd_add:1")); uid += 1
        conn = self._conn()
        cart_count = conn.execute("SELECT COUNT(*) FROM cart_items WHERE user_id=?", (CLIENT_A_ID,)).fetchone()[0]
        conn.close()
        self.assertEqual(cart_count, 0)

    async def test_promo_code_discount_applied(self):
        uid = await _seed_menu(self.dp, self.bot, 10, price=1000)
        # Admin creates a 10%-off promo code, unlimited uses.
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, ADMIN_ID, "fd_adm_promo")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, ADMIN_ID, "fd_adm_promo_add")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _text_update(uid, ADMIN_ID, "SAVE10")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _text_update(uid, ADMIN_ID, "10")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, ADMIN_ID, "fd_adm_promo_uses_skip")); uid += 1

        uid = await _add_item_to_cart(self.dp, self.bot, CLIENT_A_ID, uid)
        uid = await _checkout_via_flow(self.dp, self.bot, CLIENT_A_ID, uid, promo="SAVE10")

        conn = self._conn()
        subtotal, discount, total, promo_code = conn.execute(
            "SELECT subtotal, discount_amount, total, promo_code FROM orders WHERE id=1"
        ).fetchone()
        conn.close()
        self.assertEqual(subtotal, 1000)
        self.assertEqual(discount, 100)
        self.assertEqual(total, 900)
        self.assertEqual(promo_code, "SAVE10")

    async def test_loyalty_points_earned_on_delivery_and_spendable_next_order(self):
        uid = await _seed_menu(self.dp, self.bot, 10, price=1000)
        uid = await _add_item_to_cart(self.dp, self.bot, CLIENT_A_ID, uid)
        uid = await _checkout_via_flow(self.dp, self.bot, CLIENT_A_ID, uid)

        # Walk order #1 to delivered — needs a courier for cooking->courier.
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, ADMIN_ID, "fd_adm_couriers")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, ADMIN_ID, "fd_adm_courier_add")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _text_update(uid, ADMIN_ID, "Пётр")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, ADMIN_ID, "fd_adm_courier_phone_skip")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, ADMIN_ID, "fd_adm_order_status:1:accepted")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, ADMIN_ID, "fd_adm_order_status:1:cooking")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, ADMIN_ID, "fd_adm_order_status:1:courier")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, ADMIN_ID, "fd_courier_assign:1:1")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, ADMIN_ID, "fd_adm_order_status:1:delivered")); uid += 1

        balance = await food_delivery._points_balance(self.config.db_path, CLIENT_A_ID)
        self.assertEqual(balance, 10)  # 1000 // 100

        # Second order — spend 5 of those 10 points as a discount.
        uid = await _add_item_to_cart(self.dp, self.bot, CLIENT_A_ID, uid)
        uid = await _checkout_via_flow(self.dp, self.bot, CLIENT_A_ID, uid, points=5)

        conn = self._conn()
        points_used, total = conn.execute(
            "SELECT points_used, total FROM orders WHERE id=2"
        ).fetchone()
        conn.close()
        self.assertEqual(points_used, 5)
        self.assertEqual(total, 995)  # 1000 - 5

        balance_after = await food_delivery._points_balance(self.config.db_path, CLIENT_A_ID)
        self.assertEqual(balance_after, 5)


class FoodDeliveryOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = food_delivery.config_from_bot_row(
            {"bot_id": 2002, "name": "food_delivery_ownership_bot", "display_name": None, "group_chat_id": None}, self.data_dir,
        )
        await food_delivery.init_db(self.config.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)
        await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/start"))
        uid = await _seed_menu(self.dp, self.bot, 10)
        uid = await _add_item_to_cart(self.dp, self.bot, CLIENT_A_ID, uid)
        self.next_uid = await _checkout_via_flow(self.dp, self.bot, CLIENT_A_ID, uid)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_client_b_cannot_view_client_a_order(self):
        with patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock())) as bot_call:
            await self.dp.feed_webhook_update(self.bot, _callback_update(self.next_uid, CLIENT_B_ID, "fd_order_view:1"))
            texts = _sent_texts_to(bot_call, CLIENT_B_ID)
        self.assertTrue(texts, "expected some response")
        self.assertFalse(any("ул. Ленина" in t for t in texts), "client B saw client A's order contents")
        self.assertTrue(any("не найден" in t for t in texts))


class FoodDeliveryIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config_a = food_delivery.config_from_bot_row(
            {"bot_id": 2101, "name": "food_delivery_isolation_bot_a", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        self.config_b = food_delivery.config_from_bot_row(
            {"bot_id": 2102, "name": "food_delivery_isolation_bot_b", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await food_delivery.init_db(self.config_a.db_path)
        await food_delivery.init_db(self.config_b.db_path)
        self.bot_a, self.dp_a = _build_bot_dispatcher(self.config_a)
        self.bot_b, self.dp_b = _build_bot_dispatcher(self.config_b)
        await self.dp_a.feed_webhook_update(self.bot_a, _text_update(1, ADMIN_ID, "/start"))
        await self.dp_b.feed_webhook_update(self.bot_b, _text_update(1, ADMIN_ID, "/start"))

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_configs_point_to_different_files(self):
        self.assertNotEqual(self.config_a.db_path, self.config_b.db_path)

    async def test_two_bots_same_client_orders_not_mixed(self):
        uid_a = await _seed_menu(self.dp_a, self.bot_a, 10)
        uid_a = await _add_item_to_cart(self.dp_a, self.bot_a, CLIENT_A_ID, uid_a)
        await _checkout_via_flow(self.dp_a, self.bot_a, CLIENT_A_ID, uid_a)

        uid_b = await _seed_menu(self.dp_b, self.bot_b, 10)
        uid_b = await _add_item_to_cart(self.dp_b, self.bot_b, CLIENT_A_ID, uid_b)
        await _checkout_via_flow(self.dp_b, self.bot_b, CLIENT_A_ID, uid_b)

        conn_a = sqlite3.connect(self.config_a.db_path)
        count_a = conn_a.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        conn_a.close()
        conn_b = sqlite3.connect(self.config_b.db_path)
        count_b = conn_b.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        conn_b.close()

        self.assertEqual(count_a, 1)
        self.assertEqual(count_b, 1)


class FoodDeliveryStandaloneSmokeTest(unittest.TestCase):
    def test_config_from_env_matches_legacy_constant_shape(self):
        config = food_delivery.config_from_env()
        self.assertTrue(config.db_path.endswith("food_delivery_data.db"))
        self.assertEqual(config.bot_name, "food_delivery")

    def test_router_and_main_entrypoint_exist(self):
        self.assertTrue(hasattr(food_delivery, "router"))
        self.assertTrue(hasattr(food_delivery, "main"))


class FoodDeliveryMiniAppConfigTests(unittest.IsolatedAsyncioTestCase):
    """miniapp_config's declared table/field names must match init_db()'s real
    schema — runtime/miniapp_api.py builds SQL directly off these names, so a
    drift here would 500 at request time instead of failing a test."""

    def test_miniapp_config_resource_names(self):
        names = {r["name"] for r in food_delivery.miniapp_config["resources"]}
        self.assertEqual(names, {"menu_items", "orders", "my_orders", "couriers"})

    def test_my_orders_has_ownership_role_filter_orders_does_not(self):
        resources = {r["name"]: r for r in food_delivery.miniapp_config["resources"]}
        self.assertIsNone(resources["orders"].get("role_filter"))
        self.assertEqual(
            resources["my_orders"]["role_filter"], {"where": "client_user_id = :telegram_user_id"},
        )

    async def test_miniapp_config_fields_match_real_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "schema_check.db")
            await food_delivery.init_db(db_path)
            conn = sqlite3.connect(db_path)
            try:
                for resource in food_delivery.miniapp_config["resources"]:
                    cur = conn.execute(f"PRAGMA table_info({resource['table']})")
                    real_columns = {row[1] for row in cur.fetchall()}
                    declared = {f["name"] for f in resource["fields"]} | {"id"}
                    self.assertTrue(
                        declared.issubset(real_columns),
                        f"{resource['name']}: declared fields {declared} not all in "
                        f"real columns {real_columns}",
                    )
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
