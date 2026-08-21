"""vehicle_service template — data isolation, client-notify-on-ready, and
per-vehicle service-history tests.

Standard criterion: two bots on the SAME template, different config, must
never mix data — even driven by the SAME Telegram admin user_id.

PLUS the design's central differentiators from orders_tracker.py:
- a service request is tied to a VEHICLE (not directly to a client), and a
  vehicle's full service history (multiple requests over time) must render
  as one unified view;
- the client is notified automatically only when a request reaches "ready"
  (not on every status change), if and only if their Telegram account is
  linked, with the same double-tap-safe / Contact-ownership-checked
  mechanics as orders_tracker.py.

No real Telegram network calls, no real tokens.

Run with: python -m unittest tests.test_vehicle_service_isolation
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
from templates import vehicle_service

FAKE_TOKEN = "123456:test-token-not-real"
ADMIN_ID = 999
CLIENT_TG_ID = 555
PHONE = "89991234567"  # normalizes to "+7 (999) 123-45-67"
PLATE = "а123вс777"    # normalizes to "А123ВС777"


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


def _build_bot_dispatcher(config: vehicle_service.VehicleServiceConfig) -> tuple[Bot, Dispatcher]:
    bot = Bot(token=FAKE_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(vehicle_service.ConfigMiddleware(config))
    dp.include_router(get_template_router("vehicle_service"))
    return bot, dp


async def _create_request_via_flow(
    dp: Dispatcher, bot: Bot, admin_id: int, plate: str, item_name: str, qty: str, start_update_id: int,
    client_phone: str = PHONE, client_name: str = "Test Client", make_model: str = "Lada Vesta",
) -> int:
    """Drives the full admin FSM: req_new -> plate -> (owner phone, client
    name, make/model, skip VIN, since the plate is never pre-registered by
    these tests so the "vehicle not found" branch always fires on the first
    request for a given plate) -> item name -> qty -> skip price -> finish.
    Returns the next free update_id."""
    uid = start_update_id
    await dp.feed_webhook_update(bot, _callback_update(uid, admin_id, "req_new")); uid += 1
    await dp.feed_webhook_update(bot, _text_update(uid, admin_id, plate)); uid += 1
    await dp.feed_webhook_update(bot, _text_update(uid, admin_id, client_phone)); uid += 1
    await dp.feed_webhook_update(bot, _text_update(uid, admin_id, client_name)); uid += 1
    await dp.feed_webhook_update(bot, _text_update(uid, admin_id, make_model)); uid += 1
    await dp.feed_webhook_update(bot, _callback_update(uid, admin_id, "veh_vin_skip")); uid += 1
    await dp.feed_webhook_update(bot, _text_update(uid, admin_id, item_name)); uid += 1
    await dp.feed_webhook_update(bot, _text_update(uid, admin_id, qty)); uid += 1
    await dp.feed_webhook_update(bot, _callback_update(uid, admin_id, "req_price_skip")); uid += 1
    await dp.feed_webhook_update(bot, _callback_update(uid, admin_id, "req_item_done")); uid += 1
    return uid


async def _create_request_for_existing_vehicle(
    dp: Dispatcher, bot: Bot, admin_id: int, plate: str, item_name: str, qty: str, start_update_id: int,
) -> int:
    """Same as above but for a plate that ALREADY has a vehicle row — drives
    the "vehicle found" short-circuit branch straight into the item loop."""
    uid = start_update_id
    await dp.feed_webhook_update(bot, _callback_update(uid, admin_id, "req_new")); uid += 1
    await dp.feed_webhook_update(bot, _text_update(uid, admin_id, plate)); uid += 1
    await dp.feed_webhook_update(bot, _text_update(uid, admin_id, item_name)); uid += 1
    await dp.feed_webhook_update(bot, _text_update(uid, admin_id, qty)); uid += 1
    await dp.feed_webhook_update(bot, _callback_update(uid, admin_id, "req_price_skip")); uid += 1
    await dp.feed_webhook_update(bot, _callback_update(uid, admin_id, "req_item_done")); uid += 1
    return uid


class VehicleServiceIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

        self.config_a = vehicle_service.config_from_bot_row(
            {"bot_id": 901, "name": "vehicle_isolation_bot_a", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        self.config_b = vehicle_service.config_from_bot_row(
            {"bot_id": 902, "name": "vehicle_isolation_bot_b", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await vehicle_service.init_db(self.config_a.db_path)
        await vehicle_service.init_db(self.config_b.db_path)

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

    async def test_two_bots_same_admin_same_plate_requests_not_mixed(self):
        await _create_request_via_flow(self.dp_a, self.bot_a, ADMIN_ID, PLATE, "Замена масла", "1", 10)
        await _create_request_via_flow(self.dp_b, self.bot_b, ADMIN_ID, PLATE, "Замена тормозов", "4", 10)

        conn_a = sqlite3.connect(self.config_a.db_path)
        requests_a = conn_a.execute("SELECT COUNT(*) FROM service_requests").fetchone()[0]
        item_a = conn_a.execute("SELECT name, qty FROM service_items").fetchone()
        conn_a.close()
        conn_b = sqlite3.connect(self.config_b.db_path)
        requests_b = conn_b.execute("SELECT COUNT(*) FROM service_requests").fetchone()[0]
        item_b = conn_b.execute("SELECT name, qty FROM service_items").fetchone()
        conn_b.close()

        self.assertEqual(requests_a, 1)
        self.assertEqual(requests_b, 1)
        self.assertEqual(item_a, ("Замена масла", 1))
        self.assertEqual(item_b, ("Замена тормозов", 4))


class VehicleServiceHistoryTests(unittest.IsolatedAsyncioTestCase):
    """Owner-specified differentiator: several service requests against the
    SAME vehicle must all be visible as one unified history."""

    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = vehicle_service.config_from_bot_row(
            {"bot_id": 903, "name": "vehicle_history_bot", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await vehicle_service.init_db(self.config.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)
        await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/start"))

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_multiple_requests_on_one_vehicle_appear_as_unified_history(self):
        uid = await _create_request_via_flow(self.dp, self.bot, ADMIN_ID, PLATE, "Замена масла", "1", 10)
        # Second request for the SAME plate — must hit the "vehicle found"
        # short-circuit (no new client/vehicle prompt) and attach to the
        # SAME vehicle_id, not create a duplicate vehicle row.
        await _create_request_for_existing_vehicle(self.dp, self.bot, ADMIN_ID, PLATE, "Замена тормозов", "2", uid)

        conn = sqlite3.connect(self.config.db_path)
        vehicle_count = conn.execute("SELECT COUNT(*) FROM vehicles").fetchone()[0]
        vehicle_id = conn.execute("SELECT id FROM vehicles WHERE plate_number=?", ("А123ВС777",)).fetchone()[0]
        request_vehicle_ids = [
            row[0] for row in conn.execute("SELECT vehicle_id FROM service_requests ORDER BY id").fetchall()
        ]
        conn.close()

        self.assertEqual(vehicle_count, 1, "a repeat plate created a second vehicle row instead of reusing it")
        self.assertEqual(request_vehicle_ids, [vehicle_id, vehicle_id])

        history_text = await vehicle_service._vehicle_detail_text(self.config.db_path, vehicle_id)
        self.assertIn("История обслуживания", history_text)
        # Both requests' ids must show up in the rendered per-vehicle history.
        for row_id in (1, 2):
            self.assertIn(f"№{row_id}", history_text)


class VehicleServiceNotifyTests(unittest.IsolatedAsyncioTestCase):
    """Owner-specified differentiator: the client must be notified
    automatically ONLY when a request reaches "ready" (not on every status
    change), if and only if linked."""

    async def asyncSetUp(self):
        self._bot_call = AsyncMock(return_value=MagicMock())
        self._bot_call_patcher = patch.object(Bot, "__call__", new=self._bot_call)
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = vehicle_service.config_from_bot_row(
            {"bot_id": 904, "name": "vehicle_notify_bot", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await vehicle_service.init_db(self.config.db_path)
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

    async def _link_client(self) -> int:
        async with aiosqlite.connect(self.config.db_path) as db:
            await db.execute("UPDATE clients SET telegram_user_id=? WHERE phone=?",
                              (CLIENT_TG_ID, vehicle_service._normalize_phone(PHONE)))
            await db.commit()
            return (await (await db.execute("SELECT id FROM service_requests")).fetchone())[0]

    async def test_no_notification_on_accepted_to_in_progress(self):
        await _create_request_via_flow(self.dp, self.bot, ADMIN_ID, PLATE, "Диагностика", "1", 10)
        request_id = await self._link_client()

        await self.dp.feed_webhook_update(
            self.bot, _callback_update(100, ADMIN_ID, f"req_status:{request_id}:in_progress")
        )
        self.assertEqual(self._sent_texts_to(CLIENT_TG_ID), [], "client was notified on a non-'ready' transition")

    async def test_linked_client_is_notified_when_request_becomes_ready(self):
        await _create_request_via_flow(self.dp, self.bot, ADMIN_ID, PLATE, "Диагностика", "1", 10)
        request_id = await self._link_client()

        await self.dp.feed_webhook_update(
            self.bot, _callback_update(100, ADMIN_ID, f"req_status:{request_id}:in_progress")
        )
        await self.dp.feed_webhook_update(
            self.bot, _callback_update(101, ADMIN_ID, f"req_status:{request_id}:ready")
        )

        notifications = self._sent_texts_to(CLIENT_TG_ID)
        self.assertEqual(len(notifications), 1)
        self.assertIn(str(request_id), notifications[0])
        self.assertIn("готов к выдаче", notifications[0])

        conn = sqlite3.connect(self.config.db_path)
        notified = conn.execute(
            "SELECT notified FROM service_status_log WHERE request_id=? AND new_status='ready'", (request_id,)
        ).fetchone()[0]
        conn.close()
        self.assertEqual(notified, 1)

    async def test_unlinked_client_is_not_notified_but_status_still_changes(self):
        await _create_request_via_flow(self.dp, self.bot, ADMIN_ID, PLATE, "Диагностика", "1", 10)
        conn = sqlite3.connect(self.config.db_path)
        request_id = conn.execute("SELECT id FROM service_requests").fetchone()[0]
        conn.close()

        await self.dp.feed_webhook_update(
            self.bot, _callback_update(100, ADMIN_ID, f"req_status:{request_id}:in_progress")
        )
        await self.dp.feed_webhook_update(
            self.bot, _callback_update(101, ADMIN_ID, f"req_status:{request_id}:ready")
        )

        conn = sqlite3.connect(self.config.db_path)
        status, notified = conn.execute(
            "SELECT status, notified FROM service_requests r "
            "JOIN service_status_log l ON l.request_id=r.id AND l.new_status='ready' WHERE r.id=?",
            (request_id,),
        ).fetchone()
        conn.close()
        self.assertEqual(status, "ready")
        self.assertEqual(notified, 0)
        self.assertEqual(self._sent_texts_to(CLIENT_TG_ID), [])

    async def test_double_tap_on_ready_transition_notifies_only_once(self):
        await _create_request_via_flow(self.dp, self.bot, ADMIN_ID, PLATE, "Диагностика", "1", 10)
        request_id = await self._link_client()
        await self.dp.feed_webhook_update(
            self.bot, _callback_update(100, ADMIN_ID, f"req_status:{request_id}:in_progress")
        )
        await self.dp.feed_webhook_update(
            self.bot, _callback_update(101, ADMIN_ID, f"req_status:{request_id}:ready")
        )
        # Stale/duplicate re-tap of the same transition button.
        await self.dp.feed_webhook_update(
            self.bot, _callback_update(102, ADMIN_ID, f"req_status:{request_id}:ready")
        )

        self.assertEqual(len(self._sent_texts_to(CLIENT_TG_ID)), 1, "double-tap notified the client twice")
        conn = sqlite3.connect(self.config.db_path)
        log_rows = conn.execute(
            "SELECT COUNT(*) FROM service_status_log WHERE request_id=? AND new_status='ready'", (request_id,)
        ).fetchone()[0]
        conn.close()
        self.assertEqual(log_rows, 1, "double-tap inserted more than one status_log row")

    async def test_forward_only_flow_rejects_skipping_a_stage(self):
        await _create_request_via_flow(self.dp, self.bot, ADMIN_ID, PLATE, "Диагностика", "1", 10)
        conn = sqlite3.connect(self.config.db_path)
        request_id = conn.execute("SELECT id FROM service_requests").fetchone()[0]
        conn.close()
        # "accepted" -> "issued" is not a legal transition (must pass through
        # in_progress and ready).
        await self.dp.feed_webhook_update(
            self.bot, _callback_update(100, ADMIN_ID, f"req_status:{request_id}:issued")
        )
        conn = sqlite3.connect(self.config.db_path)
        status = conn.execute("SELECT status FROM service_requests WHERE id=?", (request_id,)).fetchone()[0]
        conn.close()
        self.assertEqual(status, "accepted")


class VehicleServiceContactLinkSecurityTests(unittest.IsolatedAsyncioTestCase):
    """A shared Contact must belong to the sender — otherwise user A could
    link user B's phone number to their own telegram_user_id and hijack B's
    'готова к выдаче' notifications."""

    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = vehicle_service.config_from_bot_row(
            {"bot_id": 905, "name": "vehicle_contact_bot", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await vehicle_service.init_db(self.config.db_path)
        async with aiosqlite.connect(self.config.db_path) as db:
            await db.execute(
                "INSERT INTO clients (name, phone) VALUES (?, ?)",
                ("Victim", vehicle_service._normalize_phone(PHONE)),
            )
            await db.commit()
        self.bot, self.dp = _build_bot_dispatcher(self.config)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_contact_belonging_to_someone_else_is_rejected(self):
        attacker_id = 777
        await self.dp.feed_webhook_update(
            self.bot, _contact_update(1, attacker_id, PHONE, contact_user_id=CLIENT_TG_ID)
        )
        conn = sqlite3.connect(self.config.db_path)
        linked = conn.execute("SELECT telegram_user_id FROM clients WHERE phone=?",
                               (vehicle_service._normalize_phone(PHONE),)).fetchone()[0]
        conn.close()
        self.assertIsNone(linked, "a contact not belonging to the sender was accepted and linked")

    async def test_own_contact_is_linked(self):
        await self.dp.feed_webhook_update(
            self.bot, _contact_update(1, CLIENT_TG_ID, PHONE, contact_user_id=CLIENT_TG_ID)
        )
        conn = sqlite3.connect(self.config.db_path)
        linked = conn.execute("SELECT telegram_user_id FROM clients WHERE phone=?",
                               (vehicle_service._normalize_phone(PHONE),)).fetchone()[0]
        conn.close()
        self.assertEqual(linked, CLIENT_TG_ID)


class VehicleServiceAdminBootstrapSecurityTests(unittest.IsolatedAsyncioTestCase):
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
        config = vehicle_service.config_from_bot_row(
            {"bot_id": 909, "name": "vehicle_bot_owned", "display_name": None,
             "group_chat_id": None, "owner_telegram_id": 12345},
            self.data_dir,
        )
        await vehicle_service.init_db(config.db_path)
        bot, dp = _build_bot_dispatcher(config)

        CLIENT_ID = 555  # not the owner, messages first
        await dp.feed_webhook_update(bot, _text_update(1, CLIENT_ID, "/start"))
        self.assertEqual(vehicle_service._load_admins(config.admins_file), set())
        self.assertFalse(vehicle_service._is_admin(CLIENT_ID, config))

        await dp.feed_webhook_update(bot, _text_update(2, 12345, "/start"))
        self.assertTrue(vehicle_service._is_admin(12345, config))
        self.assertEqual(vehicle_service._load_admins(config.admins_file), {"12345"})

    async def test_owner_is_always_admin_even_with_stale_admins_file(self):
        config = vehicle_service.config_from_bot_row(
            {"bot_id": 910, "name": "vehicle_bot_owned_2", "display_name": None,
             "group_chat_id": None, "owner_telegram_id": 777},
            self.data_dir,
        )
        await vehicle_service.init_db(config.db_path)
        vehicle_service._save_admins(config.admins_file, {"999999"})  # some other id, not the owner
        self.assertTrue(vehicle_service._is_admin(777, config))  # owner: always admin
        self.assertTrue(vehicle_service._is_admin(999999, config))  # still honors the file's own admin
        self.assertFalse(vehicle_service._is_admin(4242, config))  # neither owner nor in the file

    async def test_bootstrap_admin_syncs_to_central_bot_admins_table(self):
        config = vehicle_service.config_from_bot_row(
            {"bot_id": 911, "name": "vehicle_bot_synced", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await vehicle_service.init_db(config.db_path)
        bot, dp = _build_bot_dispatcher(config)
        await dp.feed_webhook_update(bot, _text_update(1, 321, "/start"))

        central_admins = await db_module.get_bot_admins(911)
        self.assertEqual(central_admins, ["321"])


class VehicleServiceStandaloneSmokeTest(unittest.TestCase):
    def test_config_from_env_matches_legacy_constant_shape(self):
        config = vehicle_service.config_from_env()
        self.assertTrue(config.db_path.endswith("vehicle_service_data.db"))
        self.assertEqual(config.bot_name, "vehicle_service")

    def test_router_and_main_entrypoint_exist(self):
        self.assertTrue(hasattr(vehicle_service, "router"))
        self.assertTrue(hasattr(vehicle_service, "main"))


if __name__ == "__main__":
    unittest.main()
