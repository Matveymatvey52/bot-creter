"""features/sellable_items.py — CRUD, storefront/buy flow, and isolation.

Driven through a real aiogram Dispatcher, same convention as
test_payments_module.py: sellable_items.router + payments.router cloned onto
one Dispatcher, with a small local middleware standing in for
runtime.registry.py's own bot_id-injection (see test_feature_dynamic_registry.py
for the test that the REAL registry mechanism itself works — this file only
tests sellable_items.py's own handlers).

No real Telegram network calls (Bot.__call__ mocked), no real tokens.

Run with: python -m unittest tests.test_sellable_items_module
"""
from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

import features.sellable_items as sellable_items
from db.database import (
    create_bot_record_with_admins,
    delete_bot,
    enable_bot_feature,
    set_bot_payment_provider,
)
from features.payments import init_payments_tables
from runtime.registry import _clone_router

FAKE_TOKEN = "123456:test-token-not-real"
ADMIN_ID = 999
CLIENT_ID = 111


@dataclass
class FixtureConfig:
    db_path: str
    admins_file: Path


class ConfigAndBotIdMiddleware:
    """Stands in for runtime/registry.py's ConfigMiddleware + the bot_id
    injection _load_and_include_features() now does for every feature router
    — see that module's _attach_bot_id_middleware()."""

    def __init__(self, config: FixtureConfig, bot_id: int) -> None:
        self.config = config
        self.bot_id = bot_id

    async def __call__(self, handler, event, data):
        data["config"] = self.config
        data["bot_id"] = self.bot_id
        return await handler(event, data)


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


def _build_dispatcher(config: FixtureConfig, bot_id: int, *, include_payments: bool = True) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(ConfigAndBotIdMiddleware(config, bot_id))
    dp.include_router(_clone_router(sellable_items.router))
    if include_payments:
        from features import payments
        dp.include_router(_clone_router(payments.router))
    return dp


class SellableItemsTestCase(unittest.IsolatedAsyncioTestCase):
    """Shared setup: a real bot row (for payments' bot_payment_providers /
    bot_features lookups) + a temp db_path/admins_file, ADMIN_ID pre-seeded as
    this fixture bot's own admin."""

    include_payments_feature = True

    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()

        self._tmp = tempfile.TemporaryDirectory()
        data_dir = Path(self._tmp.name)
        self.db_path = str(data_dir / "fixture.db")
        await sellable_items.init_db(self.db_path)
        await init_payments_tables(self.db_path)

        self.admins_file = data_dir / "admins.json"
        self.admins_file.write_text('{"ids": ["999"]}')

        self.bot_id = await create_bot_record_with_admins(
            name=f"sellable_items_test_bot_{id(self)}", description="test", token=FAKE_TOKEN,
            file_path="templates/inventory.py", admin_ids=[str(ADMIN_ID)],
        )
        if self.include_payments_feature:
            await enable_bot_feature(self.bot_id, "payments")

        self.config = FixtureConfig(db_path=self.db_path, admins_file=self.admins_file)
        self.bot = Bot(token=FAKE_TOKEN)
        self.dp = _build_dispatcher(self.config, self.bot_id)

    async def asyncTearDown(self):
        await delete_bot(self.bot_id)
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    def _items_in_db(self) -> list[sqlite3.Row]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM bot_sellable_items ORDER BY id").fetchall()
        conn.close()
        return rows


class AdminCrudTests(SellableItemsTestCase):
    async def test_add_item_full_flow_creates_row(self):
        await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/items"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(2, ADMIN_ID, "selitem_adm_add"))
        await self.dp.feed_webhook_update(self.bot, _text_update(3, ADMIN_ID, "Кофе"))
        await self.dp.feed_webhook_update(self.bot, _text_update(4, ADMIN_ID, "Свежемолотый арабика"))
        await self.dp.feed_webhook_update(self.bot, _text_update(5, ADMIN_ID, "350"))

        rows = self._items_in_db()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "Кофе")
        self.assertEqual(rows[0]["description"], "Свежемолотый арабика")
        self.assertEqual(rows[0]["price"], 350)
        self.assertEqual(rows[0]["active"], 1)

    async def test_add_item_skipping_description(self):
        await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/items"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(2, ADMIN_ID, "selitem_adm_add"))
        await self.dp.feed_webhook_update(self.bot, _text_update(3, ADMIN_ID, "Чай"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(4, ADMIN_ID, "selitem_adm_desc_skip"))
        await self.dp.feed_webhook_update(self.bot, _text_update(5, ADMIN_ID, "150"))

        rows = self._items_in_db()
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["description"])
        self.assertEqual(rows[0]["price"], 150)

    async def test_invalid_price_is_rejected_and_reprompts(self):
        await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/items"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(2, ADMIN_ID, "selitem_adm_add"))
        await self.dp.feed_webhook_update(self.bot, _text_update(3, ADMIN_ID, "Кофе"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(4, ADMIN_ID, "selitem_adm_desc_skip"))
        await self.dp.feed_webhook_update(self.bot, _text_update(5, ADMIN_ID, "не число"))
        await self.dp.feed_webhook_update(self.bot, _text_update(6, ADMIN_ID, "-5"))
        await self.dp.feed_webhook_update(self.bot, _text_update(7, ADMIN_ID, "0"))
        self.assertEqual(len(self._items_in_db()), 0, "non-numeric/zero/negative price must not create an item")

        await self.dp.feed_webhook_update(self.bot, _text_update(8, ADMIN_ID, "500"))
        rows = self._items_in_db()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["price"], 500)

    async def test_edit_field_updates_row(self):
        item_id = await sellable_items._create_item(self.db_path, "Кофе", "desc", 350)
        await self.dp.feed_webhook_update(self.bot, _callback_update(1, ADMIN_ID, f"selitem_adm_field:{item_id}:price"))
        await self.dp.feed_webhook_update(self.bot, _text_update(2, ADMIN_ID, "400"))

        rows = self._items_in_db()
        self.assertEqual(rows[0]["price"], 400)
        self.assertEqual(rows[0]["name"], "Кофе", "editing price must not touch other fields")

    async def test_toggle_active_hides_and_shows_item(self):
        item_id = await sellable_items._create_item(self.db_path, "Кофе", None, 350)
        await self.dp.feed_webhook_update(self.bot, _callback_update(1, ADMIN_ID, f"selitem_adm_toggle:{item_id}"))
        self.assertEqual(self._items_in_db()[0]["active"], 0)

        await self.dp.feed_webhook_update(self.bot, _callback_update(2, ADMIN_ID, f"selitem_adm_toggle:{item_id}"))
        self.assertEqual(self._items_in_db()[0]["active"], 1, "toggling twice must not delete the row — soft on/off only")

    async def test_hidden_item_is_never_hard_deleted(self):
        item_id = await sellable_items._create_item(self.db_path, "Кофе", None, 350)
        await self.dp.feed_webhook_update(self.bot, _callback_update(1, ADMIN_ID, f"selitem_adm_toggle:{item_id}"))
        rows = self._items_in_db()
        self.assertEqual(len(rows), 1, "hiding must never DELETE the row")
        self.assertEqual(rows[0]["active"], 0)


class NonAdminCannotManageItemsTests(SellableItemsTestCase):
    async def test_non_admin_add_callback_is_ignored(self):
        await self.dp.feed_webhook_update(self.bot, _callback_update(1, CLIENT_ID, "selitem_adm_add"))
        self.assertEqual(len(self._items_in_db()), 0, "a non-admin must not be able to open the add-item flow")

    async def test_non_admin_edit_field_callback_is_ignored(self):
        item_id = await sellable_items._create_item(self.db_path, "Кофе", None, 350)
        await self.dp.feed_webhook_update(self.bot, _callback_update(1, CLIENT_ID, f"selitem_adm_field:{item_id}:price"))
        await self.dp.feed_webhook_update(self.bot, _text_update(2, CLIENT_ID, "1"))
        self.assertEqual(self._items_in_db()[0]["price"], 350, "a non-admin must not be able to edit an item's price")

    async def test_non_admin_toggle_callback_is_ignored(self):
        item_id = await sellable_items._create_item(self.db_path, "Кофе", None, 350)
        await self.dp.feed_webhook_update(self.bot, _callback_update(1, CLIENT_ID, f"selitem_adm_toggle:{item_id}"))
        self.assertEqual(self._items_in_db()[0]["active"], 1, "a non-admin must not be able to hide an item")

    async def test_items_command_shows_storefront_not_admin_panel_to_non_admin(self):
        self._answers: list = []

        async def _fake_call(*args, **kwargs):
            self._answers.append(args[0] if args else None)
            return MagicMock()

        with patch.object(Bot, "__call__", new=AsyncMock(side_effect=_fake_call)):
            await self.dp.feed_webhook_update(self.bot, _text_update(1, CLIENT_ID, "/items"))
        texts = [getattr(r, "text", None) or getattr(r, "caption", None) for r in self._answers]
        self.assertTrue(any(t and "Доступные позиции" in t for t in texts))
        self.assertFalse(any(t and "Управление позициями" in t for t in texts))


class StorefrontTests(SellableItemsTestCase):
    async def test_hidden_items_are_excluded_from_storefront(self):
        visible_id = await sellable_items._create_item(self.db_path, "Видимый", None, 100)
        hidden_id = await sellable_items._create_item(self.db_path, "Скрытый", None, 200)
        await sellable_items._toggle_item_active(self.db_path, hidden_id)

        items = await sellable_items._active_items(self.db_path)
        ids = {it["id"] for it in items}
        self.assertIn(visible_id, ids)
        self.assertNotIn(hidden_id, ids)

    async def test_view_hidden_item_is_rejected(self):
        item_id = await sellable_items._create_item(self.db_path, "Скрытый", None, 200)
        await sellable_items._toggle_item_active(self.db_path, item_id)

        self._alerts: list = []

        async def _fake_call(*args, **kwargs):
            request = args[0] if args else None
            if getattr(request, "show_alert", None):
                self._alerts.append(request)
            return MagicMock()

        with patch.object(Bot, "__call__", new=AsyncMock(side_effect=_fake_call)):
            await self.dp.feed_webhook_update(self.bot, _callback_update(1, CLIENT_ID, f"selitem_view:{item_id}"))
        self.assertTrue(any(self._alerts), "viewing a hidden item must be rejected with an alert")


class BuyFlowTests(SellableItemsTestCase):
    async def test_buy_without_payments_feature_enabled_refuses_and_sends_no_invoice(self):
        # Deliberately override the default fixture (payments enabled) — this
        # is the exact hazard the module docstring warns about: sellable_items
        # on, payments off, no pre_checkout_query handler exists at all.
        from db.database import disable_bot_feature
        await disable_bot_feature(self.bot_id, "payments")

        item_id = await sellable_items._create_item(self.db_path, "Кофе", None, 350)
        calls: list = []

        async def _fake_call(*args, **kwargs):
            calls.append(args[0] if args else None)
            return MagicMock()

        with patch.object(Bot, "__call__", new=AsyncMock(side_effect=_fake_call)):
            await self.dp.feed_webhook_update(self.bot, _callback_update(1, CLIENT_ID, f"selitem_buy:{item_id}"))

        method_names = [type(c).__name__ for c in calls]
        self.assertNotIn("SendInvoice", method_names, "must not send an invoice when 'payments' isn't enabled")

    async def test_buy_without_configured_provider_shows_friendly_error(self):
        item_id = await sellable_items._create_item(self.db_path, "Кофе", None, 350)
        # "payments" IS enabled (default fixture) but no provider token was
        # ever set for this bot — create_invoice() itself raises ValueError.
        calls: list = []

        async def _fake_call(*args, **kwargs):
            request = args[0] if args else None
            calls.append(request)
            return MagicMock()

        with patch.object(Bot, "__call__", new=AsyncMock(side_effect=_fake_call)):
            await self.dp.feed_webhook_update(self.bot, _callback_update(1, CLIENT_ID, f"selitem_buy:{item_id}"))

        texts = [getattr(c, "text", None) for c in calls]
        self.assertTrue(any(t and "недоступна" in t for t in texts))

    async def test_buy_with_provider_configured_sends_invoice_for_correct_amount(self):
        await set_bot_payment_provider(self.bot_id, "provider-token-for-test")
        item_id = await sellable_items._create_item(self.db_path, "Кофе", "Арабика", 350)

        calls: list = []

        async def _fake_call(*args, **kwargs):
            calls.append(args[0] if args else None)
            return MagicMock()

        with patch.object(Bot, "__call__", new=AsyncMock(side_effect=_fake_call)):
            await self.dp.feed_webhook_update(self.bot, _callback_update(1, CLIENT_ID, f"selitem_buy:{item_id}"))

        invoices = [c for c in calls if type(c).__name__ == "SendInvoice"]
        self.assertEqual(len(invoices), 1)
        invoice = invoices[0]
        self.assertEqual(invoice.title, "Кофе")
        self.assertEqual(invoice.prices[0].amount, 35000, "350 rubles must become 35000 (kopecks) in the invoice")
        self.assertTrue(invoice.payload.startswith(f"sellable_item:{item_id}:"))

    async def test_buy_survives_unexpected_exception_from_create_invoice(self):
        # Review finding: cb_buy originally only caught ValueError/
        # TelegramBadRequest — anything else (a bare TelegramForbiddenError,
        # a sqlite3.Error, or here a plain RuntimeError standing in for "some
        # other unexpected failure") must still reach the buyer as a friendly
        # message instead of an unhandled exception with total silence.
        await set_bot_payment_provider(self.bot_id, "provider-token-for-test")
        item_id = await sellable_items._create_item(self.db_path, "Кофе", None, 350)

        calls: list = []

        async def _fake_call(*args, **kwargs):
            calls.append(args[0] if args else None)
            return MagicMock()

        with patch.object(sellable_items, "create_invoice", new=AsyncMock(side_effect=RuntimeError("boom"))):
            with patch.object(Bot, "__call__", new=AsyncMock(side_effect=_fake_call)):
                await self.dp.feed_webhook_update(self.bot, _callback_update(1, CLIENT_ID, f"selitem_buy:{item_id}"))

        texts = [getattr(c, "text", None) for c in calls]
        self.assertTrue(any(t and "не удалось" in t.lower() for t in texts), "buyer must get SOME error message, not silence")

    async def test_buy_hidden_item_is_rejected_before_any_invoice(self):
        await set_bot_payment_provider(self.bot_id, "provider-token-for-test")
        item_id = await sellable_items._create_item(self.db_path, "Кофе", None, 350)
        await sellable_items._toggle_item_active(self.db_path, item_id)

        calls: list = []

        async def _fake_call(*args, **kwargs):
            calls.append(args[0] if args else None)
            return MagicMock()

        with patch.object(Bot, "__call__", new=AsyncMock(side_effect=_fake_call)):
            await self.dp.feed_webhook_update(self.bot, _callback_update(1, CLIENT_ID, f"selitem_buy:{item_id}"))

        method_names = [type(c).__name__ for c in calls]
        self.assertNotIn("SendInvoice", method_names, "a hidden item must never be purchasable")


class FlowTimeoutTests(SellableItemsTestCase):
    async def test_stale_add_flow_is_rejected_after_timeout(self):
        await self.dp.feed_webhook_update(self.bot, _callback_update(1, ADMIN_ID, "selitem_adm_add"))
        with patch.object(time, "time", return_value=time.time() + sellable_items.FLOW_TIMEOUT_SECONDS + 1):
            await self.dp.feed_webhook_update(self.bot, _text_update(2, ADMIN_ID, "Кофе"))
        self.assertEqual(len(self._items_in_db()), 0, "a message answered after the flow timeout must not create an item")


class IsolationTests(unittest.IsolatedAsyncioTestCase):
    """Two different bots (two different db_path files) must never mix
    sellable items, even if driven through handlers with the same admin
    user_id — standard criterion, same as every other template/feature
    isolation test in this project."""

    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        data_dir = Path(self._tmp.name)

        self.db_path_a = str(data_dir / "a.db")
        self.db_path_b = str(data_dir / "b.db")
        await sellable_items.init_db(self.db_path_a)
        await sellable_items.init_db(self.db_path_b)

        self.admins_file = data_dir / "admins.json"
        self.admins_file.write_text('{"ids": ["999"]}')

        self.bot_id_a = await create_bot_record_with_admins(
            name=f"sellable_items_isolation_a_{id(self)}", description="test", token=FAKE_TOKEN,
            file_path="templates/inventory.py", admin_ids=[str(ADMIN_ID)],
        )
        self.bot_id_b = await create_bot_record_with_admins(
            name=f"sellable_items_isolation_b_{id(self)}", description="test", token=FAKE_TOKEN,
            file_path="templates/inventory.py", admin_ids=[str(ADMIN_ID)],
        )
        self.config_a = FixtureConfig(db_path=self.db_path_a, admins_file=self.admins_file)
        self.config_b = FixtureConfig(db_path=self.db_path_b, admins_file=self.admins_file)
        self.bot = Bot(token=FAKE_TOKEN)
        self.dp_a = _build_dispatcher(self.config_a, self.bot_id_a, include_payments=False)
        self.dp_b = _build_dispatcher(self.config_b, self.bot_id_b, include_payments=False)

    async def asyncTearDown(self):
        await delete_bot(self.bot_id_a)
        await delete_bot(self.bot_id_b)
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_item_created_on_bot_a_is_invisible_on_bot_b(self):
        await self.dp_a.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/items"))
        await self.dp_a.feed_webhook_update(self.bot, _callback_update(2, ADMIN_ID, "selitem_adm_add"))
        await self.dp_a.feed_webhook_update(self.bot, _text_update(3, ADMIN_ID, "Только у A"))
        await self.dp_a.feed_webhook_update(self.bot, _callback_update(4, ADMIN_ID, "selitem_adm_desc_skip"))
        await self.dp_a.feed_webhook_update(self.bot, _text_update(5, ADMIN_ID, "100"))

        items_a = await sellable_items._all_items(self.db_path_a)
        items_b = await sellable_items._all_items(self.db_path_b)
        self.assertEqual(len(items_a), 1)
        self.assertEqual(len(items_b), 0, "an item added on bot A must not leak into bot B's own db_path")


if __name__ == "__main__":
    unittest.main()
