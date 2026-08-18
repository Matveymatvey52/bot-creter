"""features/payments.py — Phase B tests.

Three criteria from the owner's brief:
  - idempotency: a redelivered successful_payment must not credit twice
  - timeout: on_pre_checkout_query must answer with ZERO I/O (no aiosqlite),
    since it shares Telegram's 10s answerPreCheckoutQuery window with every
    other bot on the same event loop (Phase A / payment-eventloop-fix)
  - isolation: one bot's provider_token must never be readable via another
    bot's id

Driven through a real aiogram Dispatcher against tests/fixtures/
payment_fixture_template.py — NOT templates/booking_fitness.py, which does
not exist yet (wiring it in is a separate, later step per the brief).

No real Telegram network calls (Bot.__call__ mocked), no real tokens.

Run with: python -m unittest tests.test_payments_module
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

import db.database as db_module
import features.office_events as office_events
from db.database import (
    create_bot_record_with_admins,
    delete_bot,
    get_bot_payment_provider,
    set_bot_payment_provider,
)
from features.office_events import OrderCreatedEvent, register_office_event_hook
from runtime.registry import _clone_router
from tests.fixtures import payment_fixture_template as fixture

FAKE_TOKEN = "123456:test-token-not-real"
USER_ID = 111


def _successful_payment_update(
    update_id: int, user_id: int, *, charge_id: str,
    provider_charge_id: str = "prov-1", payload: str = "fixture-item-1",
    currency: str = "RUB", amount: int = 10000,
) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id, "date": 1700000000,
            "chat": {"id": user_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
            "successful_payment": {
                "currency": currency,
                "total_amount": amount,
                "invoice_payload": payload,
                "telegram_payment_charge_id": charge_id,
                "provider_payment_charge_id": provider_charge_id,
            },
        },
    }


def _pre_checkout_update(
    update_id: int, user_id: int, *, query_id: str,
    payload: str = "fixture-item-1", currency: str = "RUB", amount: int = 10000,
) -> dict:
    return {
        "update_id": update_id,
        "pre_checkout_query": {
            "id": query_id,
            "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
            "currency": currency,
            "total_amount": amount,
            "invoice_payload": payload,
        },
    }


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


def _build_bot_dispatcher(config: fixture.FixtureConfig) -> tuple[Bot, Dispatcher]:
    bot = Bot(token=FAKE_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(fixture.ConfigMiddleware(config))
    dp.include_router(_clone_router(fixture.router))
    return bot, dp


class SuccessfulPaymentIdempotencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "fixture.db")
        await fixture.init_db(self.db_path)
        self.config = fixture.FixtureConfig(bot_id=9101, db_path=self.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_duplicate_delivery_does_not_double_credit(self):
        update = _successful_payment_update(1, USER_ID, charge_id="charge-abc")
        await self.dp.feed_webhook_update(self.bot, update)
        # Telegram redelivers the same update (retry after a slow/failed ack)
        # — same charge_id, new update_id, exactly what actually repeats.
        await self.dp.feed_webhook_update(self.bot, {**update, "update_id": 2})

        conn = sqlite3.connect(self.db_path)
        count, total = conn.execute(
            "SELECT COUNT(*), SUM(total_amount) FROM payments WHERE telegram_payment_charge_id='charge-abc'"
        ).fetchone()
        conn.close()
        self.assertEqual(count, 1, "duplicate successful_payment delivery inserted more than once")
        self.assertEqual(total, 10000, "duplicate delivery changed the credited amount")

    async def test_different_charges_both_recorded(self):
        await self.dp.feed_webhook_update(self.bot, _successful_payment_update(1, USER_ID, charge_id="charge-1"))
        await self.dp.feed_webhook_update(self.bot, _successful_payment_update(2, USER_ID, charge_id="charge-2"))
        conn = sqlite3.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
        conn.close()
        self.assertEqual(count, 2)

    async def test_init_payments_tables_sets_wal_mode(self):
        # Review finding: the default rollback journal makes a concurrent
        # write far more likely to surface as sqlite3.OperationalError instead
        # of the IntegrityError this module actually handles.
        conn = sqlite3.connect(self.db_path)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        self.assertEqual(mode.lower(), "wal")

    async def test_successful_payment_logs_at_info_on_credit(self):
        with self.assertLogs("features.payments", level="INFO") as log_ctx:
            await self.dp.feed_webhook_update(
                self.bot, _successful_payment_update(1, USER_ID, charge_id="charge-logged")
            )
        self.assertTrue(
            any("charge-logged" in msg for msg in log_ctx.output),
            "successful payment credit produced no INFO log line naming the charge_id",
        )


class FakeRegistry:
    """Same minimal .get(bot_id) stand-in as tests/test_office_events_module.py
    — publish_event() (called from on_successful_payment now) only ever calls
    .get() on the live registry."""
    def __init__(self, entries: dict[int, object]):
        self._entries = entries

    def get(self, bot_id: int):
        return self._entries.get(bot_id)


class SuccessfulPaymentOfficeEventsTests(unittest.IsolatedAsyncioTestCase):
    """docs/OFFICES_DESIGN.md §10 q4 — on_successful_payment must publish
    order.created from this ONE central place, and a broken/absent
    office_events wiring must never turn a successfully credited payment into
    an error response to Telegram (same isolation reasoning as
    features/office_events.py's own subscriber try/except)."""

    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "fixture.db")
        await fixture.init_db(self.db_path)
        self.config = fixture.FixtureConfig(bot_id=9201, db_path=self.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)
        office_events.set_registry(None)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()
        office_events.set_registry(None)

    async def test_publishes_order_created_to_subscribed_bot(self):
        hook = AsyncMock()
        config = {}
        register_office_event_hook(config, hook)
        office_events.set_registry(FakeRegistry({7001: SimpleNamespace(config=config)}))

        with patch.object(office_events, "get_office_subscribers", AsyncMock(return_value=[7001])):
            await self.dp.feed_webhook_update(
                self.bot, _successful_payment_update(1, USER_ID, charge_id="office-charge-1")
            )

        hook.assert_awaited_once()
        (event,), _ = hook.call_args
        self.assertEqual(event.event_type, "order.created")
        self.assertEqual(event.source_bot_id, self.config.bot_id)
        self.assertIsInstance(event.payload, OrderCreatedEvent)
        self.assertEqual(event.payload.currency, "RUB")
        self.assertEqual(event.payload.amount, 10000)
        self.assertEqual(event.payload.customer_chat_id, USER_ID)

    async def test_no_office_link_means_no_delivery_but_payment_still_credited(self):
        with patch.object(office_events, "get_office_subscribers", AsyncMock(return_value=[])):
            await self.dp.feed_webhook_update(
                self.bot, _successful_payment_update(1, USER_ID, charge_id="office-charge-2")
            )
        conn = sqlite3.connect(self.db_path)
        count = conn.execute(
            "SELECT COUNT(*) FROM payments WHERE telegram_payment_charge_id='office-charge-2'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)

    async def test_publish_event_failure_does_not_break_payment_credit(self):
        with patch.object(
            office_events, "get_office_subscribers", AsyncMock(side_effect=RuntimeError("boom"))
        ):
            # Must not raise — the credited payment above is the thing that matters.
            await self.dp.feed_webhook_update(
                self.bot, _successful_payment_update(1, USER_ID, charge_id="office-charge-3")
            )
        conn = sqlite3.connect(self.db_path)
        count = conn.execute(
            "SELECT COUNT(*) FROM payments WHERE telegram_payment_charge_id='office-charge-3'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(count, 1, "publish_event() failure must not roll back or block the payment credit")

    async def test_no_registry_means_no_crash(self):
        # office_events.set_registry(None) in asyncSetUp — publish_event()'s
        # own no-live-registry branch returns 0, must not raise here either.
        await self.dp.feed_webhook_update(
            self.bot, _successful_payment_update(1, USER_ID, charge_id="office-charge-4")
        )
        conn = sqlite3.connect(self.db_path)
        count = conn.execute(
            "SELECT COUNT(*) FROM payments WHERE telegram_payment_charge_id='office-charge-4'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)


class PreCheckoutTimeoutSafetyTests(unittest.IsolatedAsyncioTestCase):
    """Owner's explicit requirement: on_pre_checkout_query must do ZERO I/O —
    no aiosqlite, nothing that could add latency inside the 10s
    answerPreCheckoutQuery window shared across every bot on the same event
    loop (Phase A / payment-eventloop-fix). Proven by making aiosqlite.connect
    raise during the update — the handler must still answer successfully."""

    async def asyncSetUp(self):
        self._answer_calls: list = []

        async def _fake_call(*args, **kwargs):
            self._answer_calls.append(args[0] if args else None)
            return MagicMock()

        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(side_effect=_fake_call))
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "fixture.db")
        await fixture.init_db(self.db_path)
        self.config = fixture.FixtureConfig(bot_id=9102, db_path=self.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_pre_checkout_answered_without_any_db_io(self):
        with patch.object(aiosqlite, "connect", side_effect=AssertionError("on_pre_checkout_query touched the DB")):
            await self.dp.feed_webhook_update(self.bot, _pre_checkout_update(1, USER_ID, query_id="pcq-1"))

        self.assertEqual(len(self._answer_calls), 1)
        answer_request = self._answer_calls[0]
        self.assertEqual(getattr(answer_request, "pre_checkout_query_id", None), "pcq-1")
        self.assertTrue(getattr(answer_request, "ok", None))

    async def test_empty_payload_rejected_still_without_db_io(self):
        with patch.object(aiosqlite, "connect", side_effect=AssertionError("on_pre_checkout_query touched the DB")):
            await self.dp.feed_webhook_update(self.bot, _pre_checkout_update(1, USER_ID, query_id="pcq-2", payload=""))

        answer_request = self._answer_calls[0]
        self.assertFalse(getattr(answer_request, "ok", None))


class ProviderTokenIsolationTests(unittest.IsolatedAsyncioTestCase):
    """bot_payment_providers is 1:1 with bots.id — one bot's provider_token
    must never be readable through another bot's id, and must be encrypted
    at rest the same way bots.token is."""

    async def asyncSetUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._db_path_patcher = patch.object(
            db_module, "DB_PATH", Path(self._tmp_dir.name) / "test_payments_provider_isolation.db"
        )
        self._db_path_patcher.start()
        await db_module.init_db()
        self.bot_a_id = await create_bot_record_with_admins(
            name="payments_isolation_bot_a", description="test", token=FAKE_TOKEN,
            file_path="templates/inventory.py", admin_ids=["111"],
        )
        self.bot_b_id = await create_bot_record_with_admins(
            name="payments_isolation_bot_b", description="test", token=FAKE_TOKEN,
            file_path="templates/inventory.py", admin_ids=["111"],
        )

    async def asyncTearDown(self):
        # delete_bot() also removes the matching bot_payment_providers row (see
        # db/database.py) — no separate cleanup needed here.
        await delete_bot(self.bot_a_id)
        await delete_bot(self.bot_b_id)
        self._db_path_patcher.stop()
        self._tmp_dir.cleanup()

    async def test_provider_token_isolated_between_bots(self):
        await set_bot_payment_provider(self.bot_a_id, "provider-token-for-a")
        await set_bot_payment_provider(self.bot_b_id, "provider-token-for-b")

        token_a = await get_bot_payment_provider(self.bot_a_id)
        token_b = await get_bot_payment_provider(self.bot_b_id)
        self.assertEqual(token_a, "provider-token-for-a")
        self.assertEqual(token_b, "provider-token-for-b")
        self.assertNotEqual(token_a, token_b)

    async def test_bot_with_no_provider_set_gets_none_not_another_bots_token(self):
        await set_bot_payment_provider(self.bot_a_id, "provider-token-for-a")
        token_b = await get_bot_payment_provider(self.bot_b_id)
        self.assertIsNone(token_b)

    async def test_stored_token_is_encrypted_at_rest(self):
        await set_bot_payment_provider(self.bot_a_id, "provider-token-for-a")
        conn = sqlite3.connect(str(db_module.DB_PATH))
        raw = conn.execute(
            "SELECT provider_token FROM bot_payment_providers WHERE bot_id=?", (self.bot_a_id,)
        ).fetchone()[0]
        conn.close()
        self.assertNotEqual(raw, "provider-token-for-a")

    async def test_delete_bot_removes_orphaned_provider_row(self):
        await set_bot_payment_provider(self.bot_a_id, "provider-token-for-a")
        await delete_bot(self.bot_a_id)
        # asyncTearDown will call delete_bot again on an already-gone row —
        # harmless (DELETE ... WHERE matches nothing) — so create a throwaway
        # id-free check here instead of relying on teardown for the assertion.
        conn = sqlite3.connect(str(db_module.DB_PATH))
        row = conn.execute(
            "SELECT 1 FROM bot_payment_providers WHERE bot_id=?", (self.bot_a_id,)
        ).fetchone()
        conn.close()
        self.assertIsNone(row, "delete_bot left an orphaned bot_payment_providers row behind")


class CreateInvoiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._db_path_patcher = patch.object(
            db_module, "DB_PATH", Path(self._tmp_dir.name) / "test_payments_create_invoice.db"
        )
        self._db_path_patcher.start()
        await db_module.init_db()
        self.bot = Bot(token=FAKE_TOKEN)
        self.bot_id = await create_bot_record_with_admins(
            name="payments_invoice_bot", description="test", token=FAKE_TOKEN,
            file_path="templates/inventory.py", admin_ids=["111"],
        )

    async def asyncTearDown(self):
        await delete_bot(self.bot_id)
        self._bot_call_patcher.stop()
        self._db_path_patcher.stop()
        self._tmp_dir.cleanup()

    async def test_create_invoice_without_configured_provider_raises(self):
        from aiogram.types import LabeledPrice
        from features.payments import create_invoice

        with self.assertRaises(ValueError):
            await create_invoice(
                bot=self.bot, bot_id=self.bot_id, chat_id=USER_ID,
                title="t", description="d", payload="p", currency="RUB",
                prices=[LabeledPrice(label="t", amount=100)],
            )


class RefundAdminGateTests(unittest.IsolatedAsyncioTestCase):
    """Review blocker: /refund had zero admin check — any user could mark any
    charge refunded. Must be gated the same way every other admin command in
    this codebase is (config.admins_file + _load_admins)."""

    async def asyncSetUp(self):
        self._answers: list[str] = []

        async def _fake_call(*args, **kwargs):
            request = args[0] if args else None
            text = getattr(request, "text", None)
            if text:
                self._answers.append(text)
            return MagicMock()

        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(side_effect=_fake_call))
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        data_dir = Path(self._tmp.name)
        self.db_path = str(data_dir / "fixture.db")
        await fixture.init_db(self.db_path)

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO payments
                    (telegram_payment_charge_id, user_id, invoice_payload, currency, total_amount)
                VALUES ('charge-to-refund', 111, 'fixture-item-1', 'RUB', 10000)
                """
            )
            await db.commit()

        self.admins_file = data_dir / "admins.json"
        self.admins_file.write_text('{"ids": ["999"]}')
        self.config = fixture.FixtureConfig(bot_id=9103, db_path=self.db_path, admins_file=self.admins_file)
        self.bot, self.dp = _build_bot_dispatcher(self.config)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_non_admin_cannot_refund(self):
        NON_ADMIN_ID = 111
        await self.dp.feed_webhook_update(self.bot, _text_update(1, NON_ADMIN_ID, "/refund charge-to-refund"))

        self.assertTrue(any("Нет доступа" in a for a in self._answers))
        conn = sqlite3.connect(self.db_path)
        status = conn.execute(
            "SELECT status FROM payments WHERE telegram_payment_charge_id='charge-to-refund'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(status, "paid", "non-admin's /refund changed the payment status")

    async def test_admin_can_refund(self):
        ADMIN_ID = 999
        await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/refund charge-to-refund"))

        self.assertTrue(any("возвращённый" in a for a in self._answers))
        conn = sqlite3.connect(self.db_path)
        status = conn.execute(
            "SELECT status FROM payments WHERE telegram_payment_charge_id='charge-to-refund'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(status, "refunded")


if __name__ == "__main__":
    unittest.main()
