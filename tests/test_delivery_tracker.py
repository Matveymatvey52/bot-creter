"""delivery_tracker template — smoke tests.

Mirrors the structure/harness of tests/test_repair_tracker_isolation.py (its
structural reference): fake webhook updates fed through a real Dispatcher,
Bot.__call__ patched so no real Telegram network calls happen, per-bot
sqlite files in a tmp dir.

Covers, per the task brief:
- order creation via the client FSM flow (pickup -> dropoff -> item -> phone)
- a status change triggers a client notification
- illegal status transitions are rejected (new->in_transit skip-a-stage,
  delivered->anything terminal, accepted->new backward move)
- db isolation between two bot instances (two separate db_path files, rows
  don't leak, even driven by the same Telegram user_id)

No real Telegram network calls, no real tokens.

Run with: python -m unittest tests.test_delivery_tracker
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
from templates import delivery_tracker

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


def _build_bot_dispatcher(config: delivery_tracker.DeliveryTrackerConfig) -> tuple[Bot, Dispatcher]:
    bot = Bot(token=FAKE_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(delivery_tracker.ConfigMiddleware(config))
    dp.include_router(get_template_router("delivery_tracker"))
    return bot, dp


async def _create_delivery_via_flow(
    dp: Dispatcher, bot: Bot, client_id: int, pickup: str, dropoff: str, item: str,
    phone: str | None, start_update_id: int,
) -> int:
    """Drives the full client FSM: dlv_new -> pickup -> dropoff -> item ->
    phone (typed, or skipped via button if phone is None). Returns the next
    free update_id."""
    uid = start_update_id
    await dp.feed_webhook_update(bot, _callback_update(uid, client_id, "dlv_new")); uid += 1
    await dp.feed_webhook_update(bot, _text_update(uid, client_id, pickup)); uid += 1
    await dp.feed_webhook_update(bot, _text_update(uid, client_id, dropoff)); uid += 1
    await dp.feed_webhook_update(bot, _text_update(uid, client_id, item)); uid += 1
    if phone is not None:
        await dp.feed_webhook_update(bot, _text_update(uid, client_id, phone)); uid += 1
    else:
        await dp.feed_webhook_update(bot, _callback_update(uid, client_id, "dlv_phone_skip")); uid += 1
    return uid


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
    """Narrowed to status-change notifications (the "🔔" bell prefix used only
    by _apply_status_change's client message)."""
    return [t for t in _sent_texts_to(bot_call_mock, chat_id) if t.startswith("🔔")]


class DeliveryTrackerClientFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._bot_call = AsyncMock(return_value=MagicMock())
        self._bot_call_patcher = patch.object(Bot, "__call__", new=self._bot_call)
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = delivery_tracker.config_from_bot_row(
            {"bot_id": 1001, "name": "delivery_client_bot", "display_name": None, "group_chat_id": None}, self.data_dir,
        )
        await delivery_tracker.init_db(self.config.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)
        await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/start"))

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_client_creates_delivery_end_to_end_with_phone(self):
        await _create_delivery_via_flow(
            self.dp, self.bot, CLIENT_A_ID, "ул. Ленина 1", "ул. Мира 5", "Пакет документов", "89991234567", 10,
        )
        conn = sqlite3.connect(self.config.db_path)
        row = conn.execute(
            "SELECT client_user_id, pickup_address, dropoff_address, item_description, client_phone, status "
            "FROM deliveries"
        ).fetchone()
        conn.close()
        self.assertEqual(row, (
            CLIENT_A_ID, "ул. Ленина 1", "ул. Мира 5", "Пакет документов",
            "+7 (999) 123-45-67", "new",
        ))

    async def test_client_creates_delivery_without_phone(self):
        await _create_delivery_via_flow(self.dp, self.bot, CLIENT_A_ID, "A", "B", "Цветы", None, 10)
        conn = sqlite3.connect(self.config.db_path)
        phone = conn.execute("SELECT client_phone FROM deliveries").fetchone()[0]
        conn.close()
        self.assertIsNone(phone)

    async def test_admin_is_notified_of_new_delivery(self):
        await _create_delivery_via_flow(self.dp, self.bot, CLIENT_A_ID, "A", "B", "Букет роз", None, 10)
        admin_texts = _sent_texts_to(self._bot_call, ADMIN_ID)
        self.assertTrue(any("Новый заказ" in t and "Букет роз" in t for t in admin_texts))

    async def test_client_own_deliveries_list_shows_created_order(self):
        await _create_delivery_via_flow(self.dp, self.bot, CLIENT_A_ID, "A", "B", "Торт", None, 10)
        await self.dp.feed_webhook_update(self.bot, _callback_update(100, CLIENT_A_ID, "dlv_mine"))
        client_texts = _sent_texts_to(self._bot_call, CLIENT_A_ID)
        self.assertTrue(any("Ваши доставки" in t for t in client_texts))


class DeliveryTrackerStatusNotifyTests(unittest.IsolatedAsyncioTestCase):
    """Client must be notified automatically on EVERY status change."""

    async def asyncSetUp(self):
        self._bot_call = AsyncMock(return_value=MagicMock())
        self._bot_call_patcher = patch.object(Bot, "__call__", new=self._bot_call)
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = delivery_tracker.config_from_bot_row(
            {"bot_id": 1002, "name": "delivery_notify_bot", "display_name": None, "group_chat_id": None}, self.data_dir,
        )
        await delivery_tracker.init_db(self.config.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)
        await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/start"))
        await _create_delivery_via_flow(self.dp, self.bot, CLIENT_A_ID, "A", "B", "Посылка", None, 10)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_full_status_chain_notifies_client_at_every_step(self):
        uid = 100
        # new -> accepted (price-gated: button tap prompts, then price text applies it)
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, ADMIN_ID, "adlv_status:1:accepted")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _text_update(uid, ADMIN_ID, "350")); uid += 1
        # accepted -> picked_up
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, ADMIN_ID, "adlv_status:1:picked_up")); uid += 1
        # picked_up -> in_transit
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, ADMIN_ID, "adlv_status:1:in_transit")); uid += 1
        # in_transit -> delivered
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, ADMIN_ID, "adlv_status:1:delivered")); uid += 1

        notifications = _notification_texts_to(self._bot_call, CLIENT_A_ID)
        self.assertEqual(len(notifications), 4, f"expected one notification per transition, got: {notifications}")
        self.assertIn("«Принят»", notifications[0])
        self.assertIn("«Забрано»", notifications[1])
        self.assertIn("«В пути»", notifications[2])
        self.assertIn("«Доставлено»", notifications[3])
        for text in notifications:
            self.assertIn("№1", text)

        conn = sqlite3.connect(self.config.db_path)
        status, price = conn.execute("SELECT status, price FROM deliveries WHERE id=1").fetchone()
        conn.close()
        self.assertEqual(status, "delivered")
        self.assertEqual(price, 350)

    async def test_cancelled_reachable_from_new(self):
        await self.dp.feed_webhook_update(self.bot, _callback_update(100, ADMIN_ID, "adlv_status:1:cancelled"))
        notifications = _notification_texts_to(self._bot_call, CLIENT_A_ID)
        self.assertEqual(len(notifications), 1)
        self.assertIn("«Отменён»", notifications[0])


class DeliveryTrackerIllegalTransitionTests(unittest.IsolatedAsyncioTestCase):
    """Illegal status transitions must be silently rejected: no DB change,
    no client notification."""

    async def asyncSetUp(self):
        self._bot_call = AsyncMock(return_value=MagicMock())
        self._bot_call_patcher = patch.object(Bot, "__call__", new=self._bot_call)
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = delivery_tracker.config_from_bot_row(
            {"bot_id": 1003, "name": "delivery_illegal_bot", "display_name": None, "group_chat_id": None}, self.data_dir,
        )
        await delivery_tracker.init_db(self.config.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)
        await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/start"))
        await _create_delivery_via_flow(self.dp, self.bot, CLIENT_A_ID, "A", "B", "Документы", None, 10)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def _status_of(self) -> str:
        conn = sqlite3.connect(self.config.db_path)
        status = conn.execute("SELECT status FROM deliveries WHERE id=1").fetchone()[0]
        conn.close()
        return status

    async def test_new_to_in_transit_skip_a_stage_is_rejected(self):
        await self.dp.feed_webhook_update(self.bot, _callback_update(100, ADMIN_ID, "adlv_status:1:in_transit"))
        self.assertEqual(await self._status_of(), "new")
        self.assertEqual(_notification_texts_to(self._bot_call, CLIENT_A_ID), [])

    async def test_delivered_is_terminal_rejects_any_further_transition(self):
        # Walk it legally to delivered first.
        await self.dp.feed_webhook_update(self.bot, _callback_update(100, ADMIN_ID, "adlv_status:1:accepted"))
        await self.dp.feed_webhook_update(self.bot, _text_update(101, ADMIN_ID, "200"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(102, ADMIN_ID, "adlv_status:1:picked_up"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(103, ADMIN_ID, "adlv_status:1:in_transit"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(104, ADMIN_ID, "adlv_status:1:delivered"))
        self.assertEqual(await self._status_of(), "delivered")
        notif_count_before = len(_notification_texts_to(self._bot_call, CLIENT_A_ID))

        # Any further transition attempt is a no-op.
        await self.dp.feed_webhook_update(self.bot, _callback_update(105, ADMIN_ID, "adlv_status:1:cancelled"))
        self.assertEqual(await self._status_of(), "delivered")
        self.assertEqual(
            len(_notification_texts_to(self._bot_call, CLIENT_A_ID)), notif_count_before,
            "a no-op transition from a terminal state must not notify",
        )

    async def test_accepted_to_new_backward_move_is_rejected(self):
        await self.dp.feed_webhook_update(self.bot, _callback_update(100, ADMIN_ID, "adlv_status:1:accepted"))
        await self.dp.feed_webhook_update(self.bot, _text_update(101, ADMIN_ID, "200"))
        self.assertEqual(await self._status_of(), "accepted")

        # accepted -> new is not a legal (forward-only) transition.
        await self.dp.feed_webhook_update(self.bot, _callback_update(102, ADMIN_ID, "adlv_status:1:new"))
        self.assertEqual(await self._status_of(), "accepted")


class DeliveryTrackerIsolationTests(unittest.IsolatedAsyncioTestCase):
    """Two bots on the same template, different config (db_path/admins_file),
    must never mix data — even driven by the SAME Telegram user_id."""

    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

        self.config_a = delivery_tracker.config_from_bot_row(
            {"bot_id": 1101, "name": "delivery_isolation_bot_a", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        self.config_b = delivery_tracker.config_from_bot_row(
            {"bot_id": 1102, "name": "delivery_isolation_bot_b", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await delivery_tracker.init_db(self.config_a.db_path)
        await delivery_tracker.init_db(self.config_b.db_path)

        self.bot_a, self.dp_a = _build_bot_dispatcher(self.config_a)
        self.bot_b, self.dp_b = _build_bot_dispatcher(self.config_b)
        await self.dp_a.feed_webhook_update(self.bot_a, _text_update(1, ADMIN_ID, "/start"))
        await self.dp_b.feed_webhook_update(self.bot_b, _text_update(1, ADMIN_ID, "/start"))

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_configs_point_to_different_files(self):
        self.assertNotEqual(self.config_a.db_path, self.config_b.db_path)

    async def test_two_bots_same_client_deliveries_not_mixed(self):
        await _create_delivery_via_flow(self.dp_a, self.bot_a, CLIENT_A_ID, "A1", "B1", "Заказ бота A", None, 10)
        await _create_delivery_via_flow(self.dp_b, self.bot_b, CLIENT_A_ID, "A2", "B2", "Заказ бота B", None, 10)

        conn_a = sqlite3.connect(self.config_a.db_path)
        count_a = conn_a.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]
        item_a = conn_a.execute("SELECT item_description FROM deliveries").fetchone()[0]
        conn_a.close()
        conn_b = sqlite3.connect(self.config_b.db_path)
        count_b = conn_b.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]
        item_b = conn_b.execute("SELECT item_description FROM deliveries").fetchone()[0]
        conn_b.close()

        self.assertEqual(count_a, 1)
        self.assertEqual(count_b, 1)
        self.assertEqual(item_a, "Заказ бота A")
        self.assertEqual(item_b, "Заказ бота B")

    async def test_two_bots_same_admin_status_changes_not_mixed(self):
        await _create_delivery_via_flow(self.dp_a, self.bot_a, CLIENT_A_ID, "A1", "B1", "Товар A", None, 10)
        await _create_delivery_via_flow(self.dp_b, self.bot_b, CLIENT_A_ID, "A2", "B2", "Товар B", None, 10)

        # Same ADMIN_ID moves bot A's delivery new->accepted (price-gated).
        await self.dp_a.feed_webhook_update(self.bot_a, _callback_update(100, ADMIN_ID, "adlv_status:1:accepted"))
        await self.dp_a.feed_webhook_update(self.bot_a, _text_update(101, ADMIN_ID, "500"))

        conn_a = sqlite3.connect(self.config_a.db_path)
        status_a = conn_a.execute("SELECT status FROM deliveries WHERE id=1").fetchone()[0]
        conn_a.close()
        conn_b = sqlite3.connect(self.config_b.db_path)
        status_b = conn_b.execute("SELECT status FROM deliveries WHERE id=1").fetchone()[0]
        conn_b.close()

        self.assertEqual(status_a, "accepted")
        self.assertEqual(status_b, "new", "a status change on bot A's delivery leaked into bot B's database")


class DeliveryTrackerOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = delivery_tracker.config_from_bot_row(
            {"bot_id": 1104, "name": "delivery_ownership_bot", "display_name": None, "group_chat_id": None}, self.data_dir,
        )
        await delivery_tracker.init_db(self.config.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)
        await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/start"))
        await _create_delivery_via_flow(self.dp, self.bot, CLIENT_A_ID, "Секретный адрес A", "B", "Секретная посылка A", None, 10)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_client_b_cannot_view_client_a_delivery(self):
        with patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock())) as bot_call:
            await self.dp.feed_webhook_update(self.bot, _callback_update(100, CLIENT_B_ID, "dlv_view:1"))
            texts = _sent_texts_to(bot_call, CLIENT_B_ID)
        self.assertTrue(texts, "expected some response")
        self.assertFalse(any("Секретная посылка" in t for t in texts), "client B saw client A's delivery contents")
        self.assertTrue(any("не найдена" in t for t in texts))


class DeliveryTrackerCourierNoteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._bot_call = AsyncMock(return_value=MagicMock())
        self._bot_call_patcher = patch.object(Bot, "__call__", new=self._bot_call)
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = delivery_tracker.config_from_bot_row(
            {"bot_id": 1105, "name": "delivery_note_bot", "display_name": None, "group_chat_id": None}, self.data_dir,
        )
        await delivery_tracker.init_db(self.config.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)
        await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/start"))
        await _create_delivery_via_flow(self.dp, self.bot, CLIENT_A_ID, "A", "B", "Ящик", None, 10)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_note_saved_and_not_leaked_to_client(self):
        await self.dp.feed_webhook_update(self.bot, _callback_update(100, ADMIN_ID, "note_pick:1"))
        await self.dp.feed_webhook_update(self.bot, _text_update(101, ADMIN_ID, "Курьер: домофон не работает"))

        conn = sqlite3.connect(self.config.db_path)
        note = conn.execute("SELECT courier_note FROM deliveries WHERE id=1").fetchone()[0]
        conn.close()
        self.assertEqual(note, "Курьер: домофон не работает")

        # Status-change notification to the client must not include it.
        await self.dp.feed_webhook_update(self.bot, _callback_update(102, ADMIN_ID, "adlv_status:1:accepted"))
        await self.dp.feed_webhook_update(self.bot, _text_update(103, ADMIN_ID, "150"))
        client_texts = _sent_texts_to(self._bot_call, CLIENT_A_ID)
        self.assertTrue(client_texts)
        self.assertFalse(any("домофон" in t for t in client_texts))

        # And the client's own detail view must not include it either.
        await self.dp.feed_webhook_update(self.bot, _callback_update(104, CLIENT_A_ID, "dlv_view:1"))
        client_texts_2 = _sent_texts_to(self._bot_call, CLIENT_A_ID)
        self.assertFalse(any("домофон" in t for t in client_texts_2))


class DeliveryTrackerStandaloneSmokeTest(unittest.TestCase):
    def test_config_from_env_matches_legacy_constant_shape(self):
        config = delivery_tracker.config_from_env()
        self.assertTrue(config.db_path.endswith("delivery_tracker_data.db"))
        self.assertEqual(config.bot_name, "delivery_tracker")

    def test_router_and_main_entrypoint_exist(self):
        self.assertTrue(hasattr(delivery_tracker, "router"))
        self.assertTrue(hasattr(delivery_tracker, "main"))


class DeliveryTrackerMiniAppConfigTests(unittest.IsolatedAsyncioTestCase):
    """miniapp_config's declared table/field names must match init_db()'s
    real schema — miniapp_api.py builds SQL directly off these names, so a
    drift here would 500 at request time instead of failing a test."""

    def test_miniapp_config_resource_names(self):
        names = {r["name"] for r in delivery_tracker.miniapp_config["resources"]}
        self.assertEqual(names, {"deliveries"})

    def test_deliveries_resource_targets_deliveries_table(self):
        deliveries = next(
            r for r in delivery_tracker.miniapp_config["resources"] if r["name"] == "deliveries"
        )
        self.assertEqual(deliveries["table"], "deliveries")
        self.assertFalse(deliveries["creatable"])

    async def test_miniapp_config_fields_match_real_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "schema_check.db")
            await delivery_tracker.init_db(db_path)
            conn = sqlite3.connect(db_path)
            try:
                for resource in delivery_tracker.miniapp_config["resources"]:
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
