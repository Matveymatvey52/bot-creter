"""inventory template — data isolation and low-stock alert tests.

Standard criterion: two bots on the SAME template, different config, must
never mix data — even driven by the SAME Telegram user_id/SKU.

PLUS a check specific to this template's own design requirement: the
low-stock alert must fire only on the CROSSING event (stock goes from at/above
threshold to below it), not on every subsequent movement while already below —
otherwise every following sale of an already-low item would re-spam admins.

No real Telegram network calls, no real tokens.

Run with: python -m unittest tests.test_inventory_isolation
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
from templates import inventory

FAKE_TOKEN = "123456:test-token-not-real"
SAME_USER_ID = 111
ADMIN_ID = 999


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


def _build_bot_dispatcher(config: inventory.InventoryConfig) -> tuple[Bot, Dispatcher]:
    bot = Bot(token=FAKE_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(inventory.ConfigMiddleware(config))
    dp.include_router(get_template_router("inventory"))
    return bot, dp


async def _do_movement(
    dp: Dispatcher, bot: Bot, user_id: int, item_id: int, direction_button: str,
    qty: str, reason_code: str, note: str, start_update_id: int,
) -> None:
    uid = start_update_id
    await dp.feed_webhook_update(bot, _text_update(uid, user_id, direction_button)); uid += 1
    await dp.feed_webhook_update(bot, _callback_update(uid, user_id, f"inv_item:{item_id}")); uid += 1
    await dp.feed_webhook_update(bot, _text_update(uid, user_id, qty)); uid += 1
    await dp.feed_webhook_update(bot, _callback_update(uid, user_id, f"inv_reason:{reason_code}")); uid += 1
    await dp.feed_webhook_update(bot, _text_update(uid, user_id, note)); uid += 1
    await dp.feed_webhook_update(bot, _callback_update(uid, user_id, "inv_confirm")); uid += 1


class InventoryIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

        self.config_a = inventory.config_from_bot_row(
            {"bot_id": 801, "name": "inv_isolation_bot_a", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        self.config_b = inventory.config_from_bot_row(
            {"bot_id": 802, "name": "inv_isolation_bot_b", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await inventory.init_db(self.config_a.db_path)
        await inventory.init_db(self.config_b.db_path)

        async with aiosqlite.connect(self.config_a.db_path) as db:
            await db.execute("INSERT INTO items (sku, name, low_stock_threshold) VALUES ('SKU-A', 'Item A', 5)")
            await db.commit()
        async with aiosqlite.connect(self.config_b.db_path) as db:
            await db.execute("INSERT INTO items (sku, name, low_stock_threshold) VALUES ('SKU-A', 'Item A', 5)")
            await db.commit()

        self.bot_a, self.dp_a = _build_bot_dispatcher(self.config_a)
        self.bot_b, self.dp_b = _build_bot_dispatcher(self.config_b)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_configs_point_to_different_files(self):
        self.assertNotEqual(self.config_a.db_path, self.config_b.db_path)

    async def test_two_bots_same_user_same_sku_movements_not_mixed(self):
        await _do_movement(self.dp_a, self.bot_a, SAME_USER_ID, 1, "➕ Приход", "20", "purchase", "заказ A", 1)
        await _do_movement(self.dp_b, self.bot_b, SAME_USER_ID, 1, "➕ Приход", "50", "purchase", "заказ B", 1)

        conn_a = sqlite3.connect(self.config_a.db_path)
        stock_a = conn_a.execute("SELECT COALESCE(SUM(change_qty),0) FROM stock_movements").fetchone()[0]
        conn_a.close()
        conn_b = sqlite3.connect(self.config_b.db_path)
        stock_b = conn_b.execute("SELECT COALESCE(SUM(change_qty),0) FROM stock_movements").fetchone()[0]
        conn_b.close()

        self.assertEqual(stock_a, 20)
        self.assertEqual(stock_b, 50)

    async def test_stock_computed_not_stored_and_signed_correctly(self):
        await _do_movement(self.dp_a, self.bot_a, SAME_USER_ID, 1, "➕ Приход", "20", "purchase", "заказ", 1)
        await _do_movement(self.dp_a, self.bot_a, SAME_USER_ID, 1, "➖ Расход", "8", "sale", "продажа", 100)
        conn = sqlite3.connect(self.config_a.db_path)
        stock = conn.execute("SELECT COALESCE(SUM(change_qty),0) FROM stock_movements WHERE item_id=1").fetchone()[0]
        rows = conn.execute("SELECT change_qty, reason FROM stock_movements ORDER BY id").fetchall()
        conn.close()
        self.assertEqual(stock, 12)
        self.assertEqual(rows, [(20, "purchase"), (-8, "sale")])


class InventoryLowStockAlertTests(unittest.IsolatedAsyncioTestCase):
    """Owner-specified: alert fires ONLY on the crossing event."""

    async def asyncSetUp(self):
        self._bot_call = AsyncMock(return_value=MagicMock())
        self._bot_call_patcher = patch.object(Bot, "__call__", new=self._bot_call)
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = inventory.config_from_bot_row(
            {"bot_id": 803, "name": "inv_alert_bot", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await inventory.init_db(self.config.db_path)
        async with aiosqlite.connect(self.config.db_path) as db:
            await db.execute("INSERT INTO suppliers (name, contact) VALUES ('ACME', '+7 999 000-00-00')")
            await db.execute(
                "INSERT INTO items (sku, name, supplier_id, low_stock_threshold) VALUES ('SKU-X', 'Widget', 1, 5)"
            )
            await db.commit()
        self.bot, self.dp = _build_bot_dispatcher(self.config)
        await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/start"))  # bootstraps admin

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    def _admin_alert_texts(self) -> list[str]:
        texts = []
        for call in self._bot_call.call_args_list:
            request = call.args[0] if call.args else None
            text = getattr(request, "text", None)
            chat_id = getattr(request, "chat_id", None)
            if text and chat_id == ADMIN_ID and "Низкий остаток" in text:
                texts.append(text)
        return texts

    async def test_alert_fires_on_crossing_and_includes_supplier(self):
        # Stock starts at 0, add 10 (purchase), then sell 6 -> stock 4, crosses below threshold 5.
        await _do_movement(self.dp, self.bot, SAME_USER_ID, 1, "➕ Приход", "10", "purchase", "n", 10)
        await _do_movement(self.dp, self.bot, SAME_USER_ID, 1, "➖ Расход", "6", "sale", "n", 20)
        alerts = self._admin_alert_texts()
        self.assertEqual(len(alerts), 1)
        self.assertIn("ACME", alerts[0])

    async def test_alert_does_not_repeat_on_next_movement_while_still_below(self):
        await _do_movement(self.dp, self.bot, SAME_USER_ID, 1, "➕ Приход", "10", "purchase", "n", 10)
        await _do_movement(self.dp, self.bot, SAME_USER_ID, 1, "➖ Расход", "6", "sale", "n", 20)  # crosses, alert #1
        await _do_movement(self.dp, self.bot, SAME_USER_ID, 1, "➖ Расход", "1", "sale", "n", 30)  # still below, no new alert
        alerts = self._admin_alert_texts()
        self.assertEqual(len(alerts), 1, "alert re-fired on a movement that did not cross the threshold")


class InventoryFixRegressionTests(unittest.IsolatedAsyncioTestCase):
    """Regression coverage for the two blockers found by review: a double-tap
    on confirm must not double-insert a movement, and a расход that would
    drive stock negative must be rejected."""

    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = inventory.config_from_bot_row(
            {"bot_id": 804, "name": "inv_fix_bot", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await inventory.init_db(self.config.db_path)
        async with aiosqlite.connect(self.config.db_path) as db:
            await db.execute("INSERT INTO items (sku, name, low_stock_threshold) VALUES ('SKU-Z', 'Zeta', 0)")
            await db.commit()
        self.bot, self.dp = _build_bot_dispatcher(self.config)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_double_confirm_tap_does_not_double_insert(self):
        uid = 1
        await self.dp.feed_webhook_update(self.bot, _text_update(uid, SAME_USER_ID, "➕ Приход")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, SAME_USER_ID, "inv_item:1")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _text_update(uid, SAME_USER_ID, "20")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, SAME_USER_ID, "inv_reason:purchase")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _text_update(uid, SAME_USER_ID, "n")); uid += 1
        # Simulate a stale/duplicate re-tap of the same confirm button.
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, SAME_USER_ID, "inv_confirm")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, SAME_USER_ID, "inv_confirm")); uid += 1

        conn = sqlite3.connect(self.config.db_path)
        count = conn.execute("SELECT COUNT(*) FROM stock_movements").fetchone()[0]
        conn.close()
        self.assertEqual(count, 1, "double-tap on confirm inserted more than one movement")

    async def test_movement_that_would_go_negative_is_rejected(self):
        uid = 1
        await self.dp.feed_webhook_update(self.bot, _text_update(uid, SAME_USER_ID, "➖ Расход")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, SAME_USER_ID, "inv_item:1")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _text_update(uid, SAME_USER_ID, "5")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, SAME_USER_ID, "inv_reason:sale")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _text_update(uid, SAME_USER_ID, "n")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, SAME_USER_ID, "inv_confirm")); uid += 1

        conn = sqlite3.connect(self.config.db_path)
        count = conn.execute("SELECT COUNT(*) FROM stock_movements").fetchone()[0]
        conn.close()
        self.assertEqual(count, 0, "a movement that would drive stock negative was recorded anyway")


class InventoryStandaloneSmokeTest(unittest.TestCase):
    def test_config_from_env_matches_legacy_constant_shape(self):
        config = inventory.config_from_env()
        self.assertTrue(config.db_path.endswith("inventory_data.db"))
        self.assertEqual(config.bot_name, "inventory")

    def test_router_and_main_entrypoint_exist(self):
        self.assertTrue(hasattr(inventory, "router"))
        self.assertTrue(hasattr(inventory, "main"))


if __name__ == "__main__":
    unittest.main()
