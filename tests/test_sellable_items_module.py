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

    async def test_buy_truncates_long_name_and_description_for_telegrams_invoice_limits(self):
        # Security review finding: NAME_MAX_LEN=100/DESCRIPTION_MAX_LEN=1000
        # (this file's own storage/display bounds) are far looser than
        # Telegram's real sendInvoice/LabeledPrice limits (title/label 32
        # chars, description 255) — without truncating specifically for the
        # invoice call, a legitimately-entered longer name/description would
        # make EVERY purchase attempt of that item fail with
        # TelegramBadRequest, silently making it permanently unbuyable.
        await set_bot_payment_provider(self.bot_id, "provider-token-for-test")
        long_name = "А" * 60
        long_description = "Б" * 500
        item_id = await sellable_items._create_item(self.db_path, long_name, long_description, 350)

        calls: list = []

        async def _fake_call(*args, **kwargs):
            calls.append(args[0] if args else None)
            return MagicMock()

        with patch.object(Bot, "__call__", new=AsyncMock(side_effect=_fake_call)):
            await self.dp.feed_webhook_update(self.bot, _callback_update(1, CLIENT_ID, f"selitem_buy:{item_id}"))

        invoices = [c for c in calls if type(c).__name__ == "SendInvoice"]
        self.assertEqual(len(invoices), 1)
        invoice = invoices[0]
        self.assertLessEqual(len(invoice.title), sellable_items.INVOICE_TITLE_MAX_LEN)
        self.assertLessEqual(len(invoice.description), sellable_items.INVOICE_DESCRIPTION_MAX_LEN)
        self.assertLessEqual(len(invoice.prices[0].label), sellable_items.INVOICE_LABEL_MAX_LEN)

        # The stored row itself must keep the FULL name/description — only
        # what's sent to Telegram's own invoice API is shortened.
        stored = await sellable_items._item_row(self.db_path, item_id)
        self.assertEqual(stored["name"], long_name)
        self.assertEqual(stored["description"], long_description)

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


def _photo_update(update_id: int, user_id: int, msg_id: int = 50) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": msg_id, "date": 1700000000,
            "chat": {"id": user_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
            "photo": [{"file_id": "AgADfake", "file_unique_id": "fake", "width": 10, "height": 10}],
        },
    }


class PanelLeakRegressionTests(SellableItemsTestCase):
    """Second review pass (monkey-testing) found the biggest bug of the
    whole file: every step of the multi-step "add item" flow past the very
    first prompt sent a fresh, UNTRACKED message instead of going through
    _replace_panel — guaranteeing 2+ permanently orphaned bot messages per
    single normal use of "add item". Fixed by routing every prompt/re-prompt
    through _replace_panel; these tests assert the fix by counting
    DeleteMessage calls (one expected per panel replacement) rather than
    trusting DB state alone."""

    async def test_full_add_flow_deletes_every_intermediate_prompt(self):
        calls: list = []

        async def _fake_call(*args, **kwargs):
            request = args[0] if args else None
            calls.append(request)
            return MagicMock(message_id=len(calls) + 1000)

        with patch.object(Bot, "__call__", new=AsyncMock(side_effect=_fake_call)):
            await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/items"))
            await self.dp.feed_webhook_update(self.bot, _callback_update(2, ADMIN_ID, "selitem_adm_add"))
            await self.dp.feed_webhook_update(self.bot, _text_update(3, ADMIN_ID, "Кофе"))
            await self.dp.feed_webhook_update(self.bot, _text_update(4, ADMIN_ID, "Арабика"))
            await self.dp.feed_webhook_update(self.bot, _text_update(5, ADMIN_ID, "350"))

        send_count = sum(1 for c in calls if type(c).__name__ == "SendMessage")
        delete_count = sum(1 for c in calls if type(c).__name__ == "DeleteMessage")
        # /items → panel 1 ("Управление позициями"), add → panel 2 (name
        # prompt), name → panel 3 (description prompt), description → panel
        # 4 (price prompt), price → panel 5 (success + list). 5 sends, and
        # every one but the very first should have deleted its predecessor.
        self.assertEqual(send_count, 5)
        self.assertEqual(delete_count, 4, "every panel step after the first must delete its predecessor — no orphans")

    async def test_abandoned_flow_prompt_is_deleted_on_next_items_command(self):
        # Went silent mid "Добавить позицию", came back later, just hit
        # /items again instead of finishing or cancelling — the abandoned
        # "Введите название" prompt must still get cleaned up.
        await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/items"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(2, ADMIN_ID, "selitem_adm_add"))

        calls: list = []

        async def _fake_call(*args, **kwargs):
            calls.append(args[0] if args else None)
            return MagicMock(message_id=999)

        with patch.object(Bot, "__call__", new=AsyncMock(side_effect=_fake_call)):
            await self.dp.feed_webhook_update(self.bot, _text_update(3, ADMIN_ID, "/items"))

        self.assertTrue(any(type(c).__name__ == "DeleteMessage" for c in calls),
                         "the abandoned 'Введите название' prompt must be deleted, not orphaned")

    async def test_invalid_input_reprompt_does_not_orphan_a_message(self):
        await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/items"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(2, ADMIN_ID, "selitem_adm_add"))

        calls: list = []

        async def _fake_call(*args, **kwargs):
            calls.append(args[0] if args else None)
            return MagicMock(message_id=1000 + len(calls))

        with patch.object(Bot, "__call__", new=AsyncMock(side_effect=_fake_call)):
            # empty name (after stripping) — must be re-prompted through the
            # SAME tracked panel, not a bare untracked reply.
            await self.dp.feed_webhook_update(self.bot, _text_update(3, ADMIN_ID, "   "))

        self.assertTrue(any(type(c).__name__ == "DeleteMessage" for c in calls),
                         "an invalid-input re-prompt must still replace (delete+resend) the tracked panel")


class ConcurrencyRegressionTests(SellableItemsTestCase):
    """Async-pooling/state-db review: two near-simultaneous updates for the
    SAME admin could both pass validation before either committed its FSM/DB
    write — (a) duplicate item creation on a double-submitted price, (b)
    edit_item_id/edit_field misrouted by two quick different-field taps.
    Fixed by _busy_admin_actions; these tests simulate the race directly via
    asyncio.gather rather than relying on real timing."""

    async def test_concurrent_price_submission_creates_only_one_item(self):
        import asyncio
        await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/items"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(2, ADMIN_ID, "selitem_adm_add"))
        await self.dp.feed_webhook_update(self.bot, _text_update(3, ADMIN_ID, "Кофе"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(4, ADMIN_ID, "selitem_adm_desc_skip"))

        await asyncio.gather(
            self.dp.feed_webhook_update(self.bot, _text_update(5, ADMIN_ID, "350")),
            self.dp.feed_webhook_update(self.bot, _text_update(6, ADMIN_ID, "350")),
        )

        self.assertEqual(len(self._items_in_db()), 1, "two concurrent price submissions must create exactly one item")

    async def test_concurrent_field_edit_taps_do_not_misroute_the_value(self):
        import asyncio
        item_id = await sellable_items._create_item(self.db_path, "Кофе", "старое описание", 350)

        await asyncio.gather(
            self.dp.feed_webhook_update(self.bot, _callback_update(1, ADMIN_ID, f"selitem_adm_field:{item_id}:name")),
            self.dp.feed_webhook_update(self.bot, _callback_update(2, ADMIN_ID, f"selitem_adm_field:{item_id}:price")),
        )
        # Whichever tap "won" the race set a definite, single edit_field —
        # submit a value consistent with EITHER outcome being internally
        # coherent is impossible to assert generically, so instead assert
        # the guard's actual contract: only one of the two field-select
        # requests could have gone through to state.set_state (the other
        # was rejected by _busy_admin_actions), so a followup submission of
        # "500" must land in exactly one field, never split/corrupt both.
        await self.dp.feed_webhook_update(self.bot, _text_update(3, ADMIN_ID, "500"))

        row = self._items_in_db()[0]
        changed_fields = []
        if row["name"] == "500":
            changed_fields.append("name")
        if str(row["price"]) == "500":
            changed_fields.append("price")
        self.assertLessEqual(len(changed_fields), 1, "a single typed value must not corrupt more than one field")
        # The untouched field must retain a sane, original-shaped value —
        # not have been silently blanked or mismatched.
        self.assertIn(row["name"], ("Кофе", "500"))


class MalformedCallbackDataTests(SellableItemsTestCase):
    """Monkey-testing review: every int(cb.data.split(...)) in the file was
    unguarded — a forged/stale callback_data (non-numeric or truncated id)
    raised an unhandled ValueError, for selitem_view:/selitem_buy: in
    particular BEFORE cb.answer() had run (stuck spinner)."""

    async def test_non_numeric_item_id_on_admin_edit_is_handled_gracefully(self):
        await self.dp.feed_webhook_update(self.bot, _callback_update(1, ADMIN_ID, "selitem_adm_edit:not-a-number"))
        # No crash (feed_webhook_update would have propagated an unhandled
        # exception) is itself the assertion here — nothing left to check.

    async def test_non_numeric_item_id_on_toggle_is_handled_gracefully(self):
        await self.dp.feed_webhook_update(self.bot, _callback_update(1, ADMIN_ID, "selitem_adm_toggle:xyz"))

    async def test_field_callback_missing_field_segment_is_handled_gracefully(self):
        await self.dp.feed_webhook_update(self.bot, _callback_update(1, ADMIN_ID, "selitem_adm_field:5"))

    async def test_non_numeric_item_id_on_client_view_answers_gracefully(self):
        calls: list = []

        async def _fake_call(*args, **kwargs):
            calls.append(args[0] if args else None)
            return MagicMock()

        with patch.object(Bot, "__call__", new=AsyncMock(side_effect=_fake_call)):
            await self.dp.feed_webhook_update(self.bot, _callback_update(1, CLIENT_ID, "selitem_view:garbage"))

        answers = [c for c in calls if type(c).__name__ == "AnswerCallbackQuery"]
        self.assertTrue(any(answers), "cb.answer() must still fire even for a malformed item id — no stuck spinner")

    async def test_non_numeric_item_id_on_buy_answers_gracefully(self):
        calls: list = []

        async def _fake_call(*args, **kwargs):
            calls.append(args[0] if args else None)
            return MagicMock()

        with patch.object(Bot, "__call__", new=AsyncMock(side_effect=_fake_call)):
            await self.dp.feed_webhook_update(self.bot, _callback_update(1, CLIENT_ID, "selitem_buy:garbage"))

        answers = [c for c in calls if type(c).__name__ == "AnswerCallbackQuery"]
        self.assertTrue(any(answers), "cb.answer() must still fire even for a malformed item id — no stuck spinner")
        method_names = [type(c).__name__ for c in calls]
        self.assertNotIn("SendInvoice", method_names)


class NonTextMidFlowTests(SellableItemsTestCase):
    async def test_photo_sent_during_add_name_gets_a_please_use_text_reply(self):
        await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/items"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(2, ADMIN_ID, "selitem_adm_add"))

        calls: list = []

        async def _fake_call(*args, **kwargs):
            calls.append(args[0] if args else None)
            return MagicMock()

        with patch.object(Bot, "__call__", new=AsyncMock(side_effect=_fake_call)):
            await self.dp.feed_webhook_update(self.bot, _photo_update(3, ADMIN_ID))

        texts = [getattr(c, "text", None) for c in calls]
        self.assertTrue(any(t and "текстом" in t for t in texts))
        self.assertEqual(len(self._items_in_db()), 0)


class WalModeTests(SellableItemsTestCase):
    async def test_init_db_sets_wal_mode(self):
        # State-db/async-pooling review: this db_path never set WAL before,
        # unlike features/payments.py's init_payments_tables — making a
        # concurrent write (e.g. hiding an item while a payment is being
        # recorded into the SAME db_path) more likely to surface as an
        # unhandled "database is locked".
        conn = sqlite3.connect(self.db_path)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        self.assertEqual(mode.lower(), "wal")


class TruncateUtf16Tests(unittest.TestCase):
    """Monkey-testing review: _short()'s plain len()-based truncation counts
    Python codepoints, not the UTF-16 code units Telegram's sendInvoice/
    LabeledPrice limits are actually measured in — an emoji-heavy name could
    pass a 32-codepoint slice while still exceeding Telegram's real 32-unit
    limit, defeating the whole point of INVOICE_TITLE_MAX_LEN."""

    def test_ascii_text_under_limit_is_unchanged(self):
        self.assertEqual(sellable_items._truncate_utf16("Кофе", 32), "Кофе")

    def test_astral_emoji_name_is_truncated_to_fit_utf16_limit(self):
        # Each of these emoji is outside the Basic Multilingual Plane — 2
        # UTF-16 code units per character, 1 Python codepoint each.
        name = "😀" * 40
        truncated = sellable_items._truncate_utf16(name, 32)
        self.assertLessEqual(len(truncated.encode("utf-16-le")) // 2, 32)
        self.assertTrue(truncated.endswith("…"))

    def test_truncation_never_raises_on_surrogate_boundary(self):
        # A length chosen so the raw UTF-16 slice lands mid-surrogate-pair —
        # must not raise UnicodeDecodeError.
        name = "😀" * 40
        try:
            sellable_items._truncate_utf16(name, 15)
        except UnicodeDecodeError:
            self.fail("_truncate_utf16 raised on a mid-surrogate-pair cut")


class AdmCancelAliasTests(SellableItemsTestCase):
    """Clean-code review: cb_adm_list/cb_adm_cancel were merged into one
    handler registered under both callback_data values — verifies the merge
    didn't silently drop the "❌ Отмена" behavior."""

    async def test_cancel_callback_still_shows_the_admin_panel(self):
        calls: list = []

        async def _fake_call(*args, **kwargs):
            calls.append(args[0] if args else None)
            return MagicMock()

        with patch.object(Bot, "__call__", new=AsyncMock(side_effect=_fake_call)):
            await self.dp.feed_webhook_update(self.bot, _callback_update(1, ADMIN_ID, "selitem_adm_cancel"))

        texts = [getattr(c, "text", None) for c in calls]
        self.assertTrue(any(t and "Управление позициями" in t for t in texts))


if __name__ == "__main__":
    unittest.main()
