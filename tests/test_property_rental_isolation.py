"""property_rental template — data isolation, full flow (property -> lease ->
rent_payment -> overdue -> maintenance request), and CUSTOMIZE-constant tests.

Standard criterion (shared with every other template's isolation test file):
two bots on the SAME template, different config (db_path/admins_file), must
never mix data — even driven by the SAME Telegram user_id (owner OR tenant).

No real Telegram network calls, no real tokens.

Run with: python -m unittest tests.test_property_rental_isolation
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

import db.database as db_module
from runtime.registry import get_template_router
from templates import property_rental

FAKE_TOKEN = "123456:test-token-not-real"
OWNER_ID = 999
TENANT_A_ID = 555
TENANT_B_ID = 556
PROSPECT_ID = 777


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


def _photo_update(update_id: int, user_id: int) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id, "date": 1700000000,
            "chat": {"id": user_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
            "photo": [{"file_id": "FAKE_FILE_ID", "file_unique_id": "u1", "width": 10, "height": 10, "file_size": 100}],
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


def _build_bot_dispatcher(config: property_rental.PropertyRentalConfig) -> tuple[Bot, Dispatcher]:
    bot = Bot(token=FAKE_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(property_rental.ConfigMiddleware(config))
    dp.include_router(get_template_router("property_rental"))
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


class PropertyRentalIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

        self.config_a = property_rental.config_from_bot_row(
            {"bot_id": 951, "name": "property_isolation_bot_a", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        self.config_b = property_rental.config_from_bot_row(
            {"bot_id": 952, "name": "property_isolation_bot_b", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await property_rental.init_db(self.config_a.db_path)
        await property_rental.init_db(self.config_b.db_path)

        self.bot_a, self.dp_a = _build_bot_dispatcher(self.config_a)
        self.bot_b, self.dp_b = _build_bot_dispatcher(self.config_b)
        await self.dp_a.feed_webhook_update(self.bot_a, _text_update(1, OWNER_ID, "/start"))
        await self.dp_b.feed_webhook_update(self.bot_b, _text_update(1, OWNER_ID, "/start"))

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_configs_point_to_different_files(self):
        self.assertNotEqual(self.config_a.db_path, self.config_b.db_path)

    async def _add_property(self, dp, bot, address: str, start_uid: int) -> int:
        uid = start_uid
        await dp.feed_webhook_update(bot, _callback_update(uid, OWNER_ID, "prop_new")); uid += 1
        await dp.feed_webhook_update(bot, _text_update(uid, OWNER_ID, address)); uid += 1
        await dp.feed_webhook_update(bot, _callback_update(uid, OWNER_ID, "prop_area_skip")); uid += 1
        await dp.feed_webhook_update(bot, _callback_update(uid, OWNER_ID, "prop_price_skip")); uid += 1
        await dp.feed_webhook_update(bot, _callback_update(uid, OWNER_ID, "prop_photo_skip")); uid += 1
        return uid

    async def test_two_bots_same_owner_properties_not_mixed(self):
        await self._add_property(self.dp_a, self.bot_a, "ул. Ленина, 1", 10)
        await self._add_property(self.dp_b, self.bot_b, "пр. Мира, 2", 10)

        conn_a = sqlite3.connect(self.config_a.db_path)
        addr_a = conn_a.execute("SELECT address FROM properties").fetchone()[0]
        conn_a.close()
        conn_b = sqlite3.connect(self.config_b.db_path)
        addr_b = conn_b.execute("SELECT address FROM properties").fetchone()[0]
        conn_b.close()

        self.assertEqual(addr_a, "ул. Ленина, 1")
        self.assertEqual(addr_b, "пр. Мира, 2")

    async def test_two_bots_same_tenant_id_leases_not_mixed(self):
        """Same TENANT_A_ID driving both bots — must never see the other
        bot's lease."""
        await self._add_property(self.dp_a, self.bot_a, "Объект A", 10)
        await self._add_property(self.dp_b, self.bot_b, "Объект B", 10)

        # Create a lease on bot A for TENANT_A_ID.
        uid = 100
        await self.dp_a.feed_webhook_update(self.bot_a, _callback_update(uid, OWNER_ID, "lse_new")); uid += 1
        await self.dp_a.feed_webhook_update(self.bot_a, _callback_update(uid, OWNER_ID, "lse_pick_prop:1")); uid += 1
        await self.dp_a.feed_webhook_update(self.bot_a, _text_update(uid, OWNER_ID, str(TENANT_A_ID))); uid += 1
        await self.dp_a.feed_webhook_update(self.bot_a, _text_update(uid, OWNER_ID, "Иван")); uid += 1
        await self.dp_a.feed_webhook_update(self.bot_a, _callback_update(uid, OWNER_ID, "lse_phone_skip")); uid += 1
        today = date.today().isoformat()
        end = (date.today() + timedelta(days=365)).isoformat()
        await self.dp_a.feed_webhook_update(self.bot_a, _text_update(uid, OWNER_ID, today)); uid += 1
        await self.dp_a.feed_webhook_update(self.bot_a, _text_update(uid, OWNER_ID, end)); uid += 1
        await self.dp_a.feed_webhook_update(self.bot_a, _text_update(uid, OWNER_ID, "50000")); uid += 1
        await self.dp_a.feed_webhook_update(self.bot_a, _callback_update(uid, OWNER_ID, "lse_deposit_skip")); uid += 1

        conn_a = sqlite3.connect(self.config_a.db_path)
        leases_a = conn_a.execute("SELECT COUNT(*) FROM leases").fetchone()[0]
        conn_a.close()
        conn_b = sqlite3.connect(self.config_b.db_path)
        leases_b = conn_b.execute("SELECT COUNT(*) FROM leases").fetchone()[0]
        conn_b.close()

        self.assertEqual(leases_a, 1)
        self.assertEqual(leases_b, 0, "a lease created on bot A leaked into bot B's database")


class PropertyRentalFullFlowTests(unittest.IsolatedAsyncioTestCase):
    """property -> lease (auto-creates first rent_payment) -> mark overdue ->
    mark paid -> tenant submits a maintenance request -> status transitions."""

    async def asyncSetUp(self):
        self._bot_call = AsyncMock(return_value=MagicMock())
        self._bot_call_patcher = patch.object(Bot, "__call__", new=self._bot_call)
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = property_rental.config_from_bot_row(
            {"bot_id": 953, "name": "property_flow_bot", "display_name": None, "group_chat_id": None}, self.data_dir,
        )
        await property_rental.init_db(self.config.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)
        await self.dp.feed_webhook_update(self.bot, _text_update(1, OWNER_ID, "/start"))

        # Add a property.
        uid = 10
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, OWNER_ID, "prop_new")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _text_update(uid, OWNER_ID, "ул. Тестовая, 5")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, OWNER_ID, "prop_area_skip")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, OWNER_ID, "prop_price_skip")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, OWNER_ID, "prop_photo_skip")); uid += 1
        self._uid = uid

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def _create_lease(self, start_date: str, end_date: str, amount: str = "50000") -> None:
        uid = self._uid
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, OWNER_ID, "lse_new")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, OWNER_ID, "lse_pick_prop:1")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _text_update(uid, OWNER_ID, str(TENANT_A_ID))); uid += 1
        await self.dp.feed_webhook_update(self.bot, _text_update(uid, OWNER_ID, "Иван Петров")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, OWNER_ID, "lse_phone_skip")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _text_update(uid, OWNER_ID, start_date)); uid += 1
        await self.dp.feed_webhook_update(self.bot, _text_update(uid, OWNER_ID, end_date)); uid += 1
        await self.dp.feed_webhook_update(self.bot, _text_update(uid, OWNER_ID, amount)); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, OWNER_ID, "lse_deposit_skip")); uid += 1
        self._uid = uid

    async def test_lease_creation_occupies_property_and_creates_first_payment(self):
        today = date.today().isoformat()
        end = (date.today() + timedelta(days=365)).isoformat()
        await self._create_lease(today, end)

        conn = sqlite3.connect(self.config.db_path)
        status = conn.execute("SELECT status FROM properties WHERE id=1").fetchone()[0]
        lease_row = conn.execute("SELECT tenant_user_id, status FROM leases WHERE id=1").fetchone()
        payment_count = conn.execute("SELECT COUNT(*) FROM rent_payments WHERE lease_id=1").fetchone()[0]
        access_role = conn.execute("SELECT role FROM property_access WHERE user_id=?", (TENANT_A_ID,)).fetchone()[0]
        conn.close()

        self.assertEqual(status, "occupied")
        self.assertEqual(lease_row, (TENANT_A_ID, "active"))
        self.assertEqual(payment_count, 1)
        self.assertEqual(access_role, "tenant")

    async def test_overdue_sweep_flags_past_due_pending_payment(self):
        past = (date.today() - timedelta(days=10)).isoformat()
        end = (date.today() + timedelta(days=355)).isoformat()
        await self._create_lease(past, end)

        overdue = await property_rental._sweep_overdue_payments(self.config.db_path)
        self.assertEqual(len(overdue), 1)

        conn = sqlite3.connect(self.config.db_path)
        status = conn.execute("SELECT status FROM rent_payments WHERE id=1").fetchone()[0]
        conn.close()
        self.assertEqual(status, "overdue")

    async def test_mark_payment_paid_records_cashflow_entry_and_notifies_tenant(self):
        today = date.today().isoformat()
        end = (date.today() + timedelta(days=365)).isoformat()
        await self._create_lease(today, end)

        uid = self._uid
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, OWNER_ID, "pay_status:1:paid")); uid += 1

        conn = sqlite3.connect(self.config.db_path)
        status, paid_at = conn.execute("SELECT status, paid_at FROM rent_payments WHERE id=1").fetchone()
        cashflow_count = conn.execute(
            "SELECT COUNT(*) FROM cashflow_entries WHERE parent_id='1' AND type='in'"
        ).fetchone()[0]
        conn.close()

        self.assertEqual(status, "paid")
        self.assertIsNotNone(paid_at)
        self.assertEqual(cashflow_count, 1)

        tenant_texts = _sent_texts_to(self._bot_call, TENANT_A_ID)
        self.assertTrue(any("оплаченный" in t for t in tenant_texts))

    async def test_double_tap_mark_paid_records_cashflow_entry_once(self):
        today = date.today().isoformat()
        end = (date.today() + timedelta(days=365)).isoformat()
        await self._create_lease(today, end)

        uid = self._uid
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, OWNER_ID, "pay_status:1:paid")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, OWNER_ID, "pay_status:1:paid")); uid += 1

        conn = sqlite3.connect(self.config.db_path)
        cashflow_count = conn.execute("SELECT COUNT(*) FROM cashflow_entries").fetchone()[0]
        conn.close()
        self.assertEqual(cashflow_count, 1, "double-tap on mark-paid recorded the income twice")

    async def test_tenant_submits_maintenance_request_and_owner_notified(self):
        today = date.today().isoformat()
        end = (date.today() + timedelta(days=365)).isoformat()
        await self._create_lease(today, end)

        uid = self._uid
        await self.dp.feed_webhook_update(self.bot, _text_update(uid, TENANT_A_ID, "/start")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, TENANT_A_ID, "tmnt_new")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _text_update(uid, TENANT_A_ID, "Течёт кран на кухне")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, TENANT_A_ID, "tmnt_photo_skip")); uid += 1

        conn = sqlite3.connect(self.config.db_path)
        row = conn.execute(
            "SELECT tenant_user_id, description, status FROM maintenance_requests WHERE id=1"
        ).fetchone()
        conn.close()
        self.assertEqual(row, (TENANT_A_ID, "Течёт кран на кухне", "new"))

        owner_texts = _sent_texts_to(self._bot_call, OWNER_ID)
        self.assertTrue(any("Течёт кран" in t for t in owner_texts))

    async def test_maintenance_status_transitions_new_to_in_progress_to_closed(self):
        today = date.today().isoformat()
        end = (date.today() + timedelta(days=365)).isoformat()
        await self._create_lease(today, end)

        uid = self._uid
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, TENANT_A_ID, "tmnt_new")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _text_update(uid, TENANT_A_ID, "Не работает розетка")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, TENANT_A_ID, "tmnt_photo_skip")); uid += 1

        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, OWNER_ID, "mnt_status:1:in_progress")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, OWNER_ID, "mnt_status:1:closed")); uid += 1

        conn = sqlite3.connect(self.config.db_path)
        status = conn.execute("SELECT status FROM maintenance_requests WHERE id=1").fetchone()[0]
        conn.close()
        self.assertEqual(status, "closed")

        tenant_texts = _sent_texts_to(self._bot_call, TENANT_A_ID)
        self.assertTrue(any("В работе" in t for t in tenant_texts))
        self.assertTrue(any("Закрыта" in t for t in tenant_texts))

    async def test_other_tenant_cannot_view_maintenance_request(self):
        today = date.today().isoformat()
        end = (date.today() + timedelta(days=365)).isoformat()
        await self._create_lease(today, end)

        uid = self._uid
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, TENANT_A_ID, "tmnt_new")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _text_update(uid, TENANT_A_ID, "Секретная проблема")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, TENANT_A_ID, "tmnt_photo_skip")); uid += 1

        with patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock())) as bot_call:
            await self.dp.feed_webhook_update(self.bot, _callback_update(uid + 1, TENANT_B_ID, "tmnt_view:1"))
            texts = _sent_texts_to(bot_call, TENANT_B_ID)
        self.assertFalse(any("Секретная проблема" in t for t in texts), "tenant B saw tenant A's maintenance request")


class PropertyRentalViewingRequestTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._bot_call = AsyncMock(return_value=MagicMock())
        self._bot_call_patcher = patch.object(Bot, "__call__", new=self._bot_call)
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = property_rental.config_from_bot_row(
            {"bot_id": 954, "name": "property_vr_bot", "display_name": None, "group_chat_id": None}, self.data_dir,
        )
        await property_rental.init_db(self.config.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)
        await self.dp.feed_webhook_update(self.bot, _text_update(1, OWNER_ID, "/start"))
        uid = 10
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, OWNER_ID, "prop_new")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _text_update(uid, OWNER_ID, "ул. Пробная, 7")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, OWNER_ID, "prop_area_skip")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, OWNER_ID, "prop_price_skip")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, OWNER_ID, "prop_photo_skip")); uid += 1
        self._uid = uid

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_prospect_requests_viewing_and_owner_is_notified(self):
        uid = self._uid
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, PROSPECT_ID, "pub_vr_new:1")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _text_update(uid, PROSPECT_ID, "Мария")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, PROSPECT_ID, "vr_phone_skip")); uid += 1

        conn = sqlite3.connect(self.config.db_path)
        row = conn.execute(
            "SELECT property_id, requester_user_id, requester_name, status FROM viewing_requests WHERE id=1"
        ).fetchone()
        conn.close()
        self.assertEqual(row, (1, PROSPECT_ID, "Мария", "new"))

        owner_texts = _sent_texts_to(self._bot_call, OWNER_ID)
        self.assertTrue(any("заявка на просмотр" in t and "Мария" in t for t in owner_texts))

    async def test_lease_created_from_viewing_request_reuses_requester_identity(self):
        uid = self._uid
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, PROSPECT_ID, "pub_vr_new:1")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _text_update(uid, PROSPECT_ID, "Мария")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, PROSPECT_ID, "vr_phone_skip")); uid += 1

        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, OWNER_ID, "lse_from_vr:1")); uid += 1
        today = date.today().isoformat()
        end = (date.today() + timedelta(days=365)).isoformat()
        await self.dp.feed_webhook_update(self.bot, _text_update(uid, OWNER_ID, today)); uid += 1
        await self.dp.feed_webhook_update(self.bot, _text_update(uid, OWNER_ID, end)); uid += 1
        await self.dp.feed_webhook_update(self.bot, _text_update(uid, OWNER_ID, "45000")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, OWNER_ID, "lse_deposit_skip")); uid += 1

        conn = sqlite3.connect(self.config.db_path)
        row = conn.execute("SELECT tenant_user_id, tenant_name FROM leases WHERE id=1").fetchone()
        conn.close()
        self.assertEqual(row, (PROSPECT_ID, "Мария"))


class PropertyRentalCustomizeConstantTests(unittest.TestCase):
    """Proves REMINDER_DAYS_BEFORE_RENT and LATE_FEE_PERCENT (CUSTOMIZE block)
    actually affect behavior, not just declared and unused."""

    def test_reminder_days_before_rent_drives_reminders_config_offset(self):
        rule = property_rental.reminders_config["rules"][0]
        self.assertEqual(rule["offsets_hours"], [property_rental.REMINDER_DAYS_BEFORE_RENT * 24])

    def test_late_fee_percent_increases_displayed_overdue_amount(self):
        base = 10_000
        with_fee = property_rental._amount_with_late_fee(base)
        self.assertGreater(with_fee, base)
        self.assertEqual(with_fee, round(base * (1 + property_rental.LATE_FEE_PERCENT / 100)))

    def test_zero_late_fee_percent_leaves_amount_unchanged(self):
        with patch.object(property_rental, "LATE_FEE_PERCENT", 0):
            self.assertEqual(property_rental._amount_with_late_fee(10_000), 10_000)


class PropertyRentalStandaloneSmokeTest(unittest.TestCase):
    def test_config_from_env_matches_legacy_constant_shape(self):
        config = property_rental.config_from_env()
        self.assertTrue(config.db_path.endswith("property_rental_data.db"))
        self.assertEqual(config.bot_name, "property_rental")

    def test_router_and_main_entrypoint_exist(self):
        self.assertTrue(hasattr(property_rental, "router"))
        self.assertTrue(hasattr(property_rental, "main"))


class PropertyRentalAdminHijackTests(unittest.IsolatedAsyncioTestCase):
    """Security fix: whoever sent /start FIRST used to permanently become the
    bot's owner (full admin panel: properties/leases/payments/maintenance/
    admins management) — a prospective tenant or random client who messaged
    the bot before the real owner tested it could hijack that role. Same
    criterion/pattern as tests/test_shop_catalog_isolation.py's admin-hijack
    tests. Note: property_access (role_filter source table for the mini-app)
    is a SEPARATE concern from admins.json/_is_admin (Telegram-side gating) —
    these tests target the latter, which had the naive first-comer bug."""

    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

        # cmd_start now syncs the bootstrap admin into db.database.add_bot_admin
        # (central bot_admins table) -- must be redirected to a throwaway DB.
        self._central_db_path = self.data_dir / "central_bots.db"
        self._db_path_patcher = patch.object(db_module, "DB_PATH", self._central_db_path)
        self._db_path_patcher.start()
        await db_module.init_db()

    async def asyncTearDown(self):
        self._db_path_patcher.stop()
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_non_owner_messaging_first_does_not_become_admin(self):
        config = property_rental.config_from_bot_row(
            {"bot_id": 961, "name": "property_bot_owned", "display_name": None,
             "group_chat_id": None, "owner_telegram_id": 12345},
            self.data_dir,
        )
        await property_rental.init_db(config.db_path)
        bot, dp = _build_bot_dispatcher(config)

        CLIENT_ID = 555  # not the owner, messages first (e.g. a prospect)
        await dp.feed_webhook_update(bot, _text_update(1, CLIENT_ID, "/start"))
        self.assertEqual(property_rental._load_admins(config.admins_file), set())
        self.assertFalse(property_rental._is_admin(CLIENT_ID, config))

    async def test_owner_start_becomes_admin(self):
        config = property_rental.config_from_bot_row(
            {"bot_id": 962, "name": "property_bot_owned2", "display_name": None,
             "group_chat_id": None, "owner_telegram_id": 12345},
            self.data_dir,
        )
        await property_rental.init_db(config.db_path)
        bot, dp = _build_bot_dispatcher(config)

        await dp.feed_webhook_update(bot, _text_update(1, 12345, "/start"))
        self.assertTrue(property_rental._is_admin(12345, config))
        self.assertEqual(property_rental._load_admins(config.admins_file), {"12345"})

    async def test_owner_is_always_admin_even_with_stale_admins_file(self):
        config = property_rental.config_from_bot_row(
            {"bot_id": 963, "name": "property_bot_owned3", "display_name": None,
             "group_chat_id": None, "owner_telegram_id": 777},
            self.data_dir,
        )
        await property_rental.init_db(config.db_path)
        property_rental._save_admins(config.admins_file, {"999999"})
        self.assertTrue(property_rental._is_admin(777, config))  # owner: always admin
        self.assertTrue(property_rental._is_admin(999999, config))  # still honors the file's own admin
        self.assertFalse(property_rental._is_admin(4242, config))  # neither owner nor in the file

    async def test_standalone_mode_keeps_first_comer_behavior(self):
        """owner_telegram_id unknown (standalone/env mode) -- the old
        first-comer bootstrap is the only option available, so it's kept."""
        config = property_rental.config_from_bot_row(
            {"bot_id": 964, "name": "property_bot_standalone", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await property_rental.init_db(config.db_path)
        bot, dp = _build_bot_dispatcher(config)

        await dp.feed_webhook_update(bot, _text_update(1, 111, "/start"))
        self.assertEqual(property_rental._load_admins(config.admins_file), {"111"})
        self.assertTrue(property_rental._is_admin(111, config))

    async def test_bootstrap_admin_syncs_to_central_bot_admins_table(self):
        config = property_rental.config_from_bot_row(
            {"bot_id": 965, "name": "property_bot_synced", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await property_rental.init_db(config.db_path)
        bot, dp = _build_bot_dispatcher(config)
        await dp.feed_webhook_update(bot, _text_update(1, 321, "/start"))

        central_admins = await db_module.get_bot_admins(965)
        self.assertEqual(central_admins, ["321"])


if __name__ == "__main__":
    unittest.main()
