"""Data isolation tests for the event_manager template.

Standard criterion (same as every other test_*_isolation.py in tests/): two
bots on the event_manager template, different EventManagerConfig (different
db_path/admins_file, built via _paths_for under two different tempdirs),
driven by the SAME Telegram user_id, must never mix data — event created on
bot A must not appear in bot B's event list, admin of bot A must not be admin
of bot B, etc.

Also covers the admin-bootstrap security fix (same criterion as
tests/test_shop_catalog_isolation.py's admin tests): whoever sends /start
FIRST must NOT permanently become admin when bots.owner_telegram_id is
known; only the real owner may claim the bootstrap slot, and the DB-known
owner is always treated as admin regardless of the local admins_file state.

Everything lives in tempfile.TemporaryDirectory(), never data/bots.db.

Run with: python -m unittest tests.test_event_manager_isolation
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram import Bot, Dispatcher
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import db.database as db_module
from runtime.registry import get_template_router
from templates import event_manager as evm

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


def _build_bot_dispatcher(config: evm.EventManagerConfig) -> tuple[Bot, Dispatcher]:
    bot = Bot(token=FAKE_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(evm.ConfigMiddleware(config))
    dp.include_router(get_template_router("event_manager"))
    return bot, dp


class EventManagerIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        # Two SEPARATE tempdirs — not just two names under one tempdir — to
        # match how two independently-deployed bots would never share a
        # filesystem location either.
        self._tmp_a = tempfile.TemporaryDirectory()
        self._tmp_b = tempfile.TemporaryDirectory()
        self.config_a = evm._paths_for("event_bot_a", Path(self._tmp_a.name))
        self.config_b = evm._paths_for("event_bot_b", Path(self._tmp_b.name))
        await evm.init_db(self.config_a.db_path)
        await evm.init_db(self.config_b.db_path)

    async def asyncTearDown(self):
        self._tmp_a.cleanup()
        self._tmp_b.cleanup()
        self._bot_call_patcher.stop()

    async def test_configs_point_to_different_files(self):
        self.assertNotEqual(self.config_a.db_path, self.config_b.db_path)
        self.assertNotEqual(self.config_a.admins_file, self.config_b.admins_file)

    async def test_events_isolated_across_bots(self):
        await evm._create_event(self.config_a.db_path, "Event A", "2026-12-25 19:00", None, None)
        await evm._create_event(self.config_b.db_path, "Event B", "2026-12-31 20:00", None, None)

        events_a = await evm._active_events(self.config_a.db_path)
        events_b = await evm._active_events(self.config_b.db_path)
        self.assertEqual(len(events_a), 1)
        self.assertEqual(len(events_b), 1)
        self.assertEqual(events_a[0]["name"], "Event A")
        self.assertEqual(events_b[0]["name"], "Event B")

    async def test_guests_isolated_across_bots_same_user_claims(self):
        """Same Telegram user_id claims a guest slot on BOTH bots — the
        claims must not leak into each other's guest tables."""
        SAME_USER = 555
        event_a = await evm._create_event(self.config_a.db_path, "Event A", "2026-12-25 19:00", None, None)
        event_b = await evm._create_event(self.config_b.db_path, "Event B", "2026-12-31 20:00", None, None)
        guest_a = await evm._create_guest(self.config_a.db_path, event_a, "Alice", None)
        guest_b = await evm._create_guest(self.config_b.db_path, event_b, "Alice", None)

        await evm._claim_guest(self.config_a.db_path, guest_a["invite_token"], SAME_USER)
        await evm._claim_guest(self.config_b.db_path, guest_b["invite_token"], SAME_USER)

        row_a = await evm._guest_row(self.config_a.db_path, guest_a["id"])
        row_b = await evm._guest_row(self.config_b.db_path, guest_b["id"])
        self.assertEqual(row_a["telegram_user_id"], SAME_USER)
        self.assertEqual(row_b["telegram_user_id"], SAME_USER)

        # Bot A's guest table must not contain bot B's guest, and vice versa.
        guests_a = await evm._guests_for_event(self.config_a.db_path, event_a)
        guests_b = await evm._guests_for_event(self.config_b.db_path, event_b)
        self.assertEqual(len(guests_a), 1)
        self.assertEqual(len(guests_b), 1)
        self.assertNotEqual(guest_a["invite_token"], guest_b["invite_token"])

    async def test_first_start_makes_sender_sole_admin_per_bot(self):
        bot_a, dp_a = _build_bot_dispatcher(self.config_a)
        bot_b, dp_b = _build_bot_dispatcher(self.config_b)
        await dp_a.feed_webhook_update(bot_a, _text_update(1, 111, "/start"))
        await dp_b.feed_webhook_update(bot_b, _text_update(1, 999, "/start"))

        self.assertEqual(evm._load_admins(self.config_a.admins_file), {"111"})
        self.assertEqual(evm._load_admins(self.config_b.admins_file), {"999"})
        # Admin of bot A is NOT admin of bot B, and vice versa.
        self.assertFalse(evm._is_bot_admin(111, self.config_b))
        self.assertFalse(evm._is_bot_admin(999, self.config_a))

    async def test_second_starter_on_same_bot_is_not_made_admin(self):
        bot_a, dp_a = _build_bot_dispatcher(self.config_a)
        await dp_a.feed_webhook_update(bot_a, _text_update(1, 111, "/start"))
        await dp_a.feed_webhook_update(bot_a, _text_update(2, 222, "/start"))
        self.assertEqual(evm._load_admins(self.config_a.admins_file), {"111"})
        self.assertFalse(evm._is_bot_admin(222, self.config_a))

    async def test_create_event_via_fsm_flow_inserts_row_scoped_to_bots_own_db(self):
        """Drives the actual create-event FSM (name -> date -> skip location
        -> skip limit) through the dispatcher and confirms the row lands in
        THIS bot's db_path only — the other bot's db stays empty even though
        the same admin user_id is used on both."""
        bot_a, dp_a = _build_bot_dispatcher(self.config_a)
        bot_b, dp_b = _build_bot_dispatcher(self.config_b)
        ADMIN = 222
        await dp_a.feed_webhook_update(bot_a, _text_update(1, ADMIN, "/start"))  # becomes admin of A
        await dp_b.feed_webhook_update(bot_b, _text_update(1, ADMIN, "/start"))  # becomes admin of B too (diff bot)

        await dp_a.feed_webhook_update(bot_a, _callback_update(2, ADMIN, "evm_event_new"))
        await dp_a.feed_webhook_update(bot_a, _text_update(3, ADMIN, "Wedding"))
        await dp_a.feed_webhook_update(bot_a, _text_update(4, ADMIN, "25.12.2026 19:00"))
        await dp_a.feed_webhook_update(bot_a, _callback_update(5, ADMIN, "evm_ev_loc_skip"))
        await dp_a.feed_webhook_update(bot_a, _callback_update(6, ADMIN, "evm_ev_limit_skip"))

        events_a = await evm._active_events(self.config_a.db_path)
        events_b = await evm._active_events(self.config_b.db_path)
        self.assertEqual(len(events_a), 1)
        self.assertEqual(events_a[0]["name"], "Wedding")
        self.assertEqual(events_a[0]["event_dt"], "2026-12-25 19:00")
        self.assertIsNone(events_a[0]["location"])
        self.assertIsNone(events_a[0]["guest_limit"])
        self.assertEqual(len(events_b), 0)


class DoubleTapRegressionTests(unittest.IsolatedAsyncioTestCase):
    """Review-found: rapid double-tap on the FINAL step of a create-event /
    add-guest FSM flow, or on the broadcast-reminder confirm button, used to
    fire the underlying DB insert / send loop TWICE — aiogram dispatches each
    callback_query/message as its own asyncio task, so editing the panel
    first does not by itself prevent a second, already-in-flight tap from
    also reaching the handler. Fixed with module-level in-flight guards
    (_flow_submitting / _broadcasting_events) in event_manager.py. These
    tests drive the module's own functions directly with asyncio.gather to
    simulate the near-simultaneous double tap."""

    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.config = evm._paths_for("evm_dtap_bot", Path(self._tmp.name))
        await evm.init_db(self.config.db_path)
        self.bot = Bot(token=FAKE_TOKEN)
        storage = MemoryStorage()
        key = StorageKey(bot_id=self.bot.id, chat_id=777, user_id=777)
        self.state = FSMContext(storage=storage, key=key)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_double_tap_on_create_event_final_step_inserts_only_one_row(self):
        await self.state.update_data(ev_name="Wedding", ev_dt="2026-12-25 19:00", ev_location=None)
        await asyncio.gather(
            evm._finish_create_event(self.bot, self.state, 777, self.config, None),
            evm._finish_create_event(self.bot, self.state, 777, self.config, None),
        )
        events = await evm._active_events(self.config.db_path)
        self.assertEqual(len(events), 1)

    async def test_double_tap_on_add_guest_final_step_inserts_only_one_row(self):
        event_id = await evm._create_event(self.config.db_path, "Party", "2026-12-25 19:00", None, None)
        await self.state.update_data(guest_event_id=event_id, gu_name="Alice")
        await asyncio.gather(
            evm._finish_add_guest(self.bot, self.state, 777, self.config, None),
            evm._finish_add_guest(self.bot, self.state, 777, self.config, None),
        )
        guests = await evm._guests_for_event(self.config.db_path, event_id)
        self.assertEqual(len(guests), 1)

    async def test_double_tap_on_remind_broadcast_sends_only_one_reminder_per_guest(self):
        bot, dp = _build_bot_dispatcher(self.config)
        ADMIN = 888
        await dp.feed_webhook_update(bot, _text_update(1, ADMIN, "/start"))  # becomes admin
        event_id = await evm._create_event(self.config.db_path, "Party", "2026-12-25 19:00", None, None)
        guest = await evm._create_guest(self.config.db_path, event_id, "Bob", None)
        await evm._claim_guest(self.config.db_path, guest["invite_token"], 4242)

        with patch.object(Bot, "send_message", new=AsyncMock(return_value=MagicMock())) as mock_send:
            await asyncio.gather(
                dp.feed_webhook_update(bot, _callback_update(2, ADMIN, f"evm_remind_yes:{event_id}")),
                dp.feed_webhook_update(bot, _callback_update(3, ADMIN, f"evm_remind_yes:{event_id}")),
            )
            reminder_calls = [
                c for c in mock_send.call_args_list
                if c.args and c.args[0] == 4242
            ]
            self.assertEqual(
                len(reminder_calls), 1,
                f"expected exactly one reminder sent to guest 4242, got {len(reminder_calls)}",
            )


class AdminBootstrapSecurityTests(unittest.IsolatedAsyncioTestCase):
    """Security fix: previously, whoever sent /start FIRST permanently
    became the bot admin — a guest who opened the bot's link before the
    organizer configured it would silently seize the "📅 Мероприятия" admin
    panel. When bots.owner_telegram_id is known, only that user may claim
    the bootstrap admin slot; the DB-known owner is also always treated as
    admin regardless of the local admins_file state."""

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
        config = evm.config_from_bot_row(
            {"bot_id": 601, "name": "event_bot_owned", "display_name": None,
             "group_chat_id": None, "owner_telegram_id": 12345},
            self.data_dir,
        )
        await evm.init_db(config.db_path)
        bot, dp = _build_bot_dispatcher(config)

        CLIENT_ID = 555  # not the owner, messages first
        await dp.feed_webhook_update(bot, _text_update(1, CLIENT_ID, "/start"))
        self.assertEqual(evm._load_admins(config.admins_file), set())
        self.assertFalse(evm._is_bot_admin(CLIENT_ID, config))

        await dp.feed_webhook_update(bot, _text_update(2, 12345, "/start"))
        self.assertTrue(evm._is_bot_admin(12345, config))
        self.assertEqual(evm._load_admins(config.admins_file), {"12345"})

    async def test_owner_is_always_admin_even_with_stale_admins_file(self):
        config = evm.config_from_bot_row(
            {"bot_id": 602, "name": "event_bot_owned_2", "display_name": None,
             "group_chat_id": None, "owner_telegram_id": 777},
            self.data_dir,
        )
        await evm.init_db(config.db_path)
        evm._save_admins(config.admins_file, {"999999"})  # some other id, not the owner
        self.assertTrue(evm._is_bot_admin(777, config))  # owner: always admin
        self.assertTrue(evm._is_bot_admin(999999, config))  # still honors the file's own admin
        self.assertFalse(evm._is_bot_admin(4242, config))  # neither owner nor in the file

    async def test_standalone_mode_keeps_first_comer_bootstrap(self):
        """owner_telegram_id=None (standalone/env mode) must keep the OLD
        first-comer bootstrap as the only available option — there is no DB
        owner to defer to."""
        config = evm.config_from_bot_row(
            {"bot_id": 603, "name": "event_bot_standalone", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await evm.init_db(config.db_path)
        bot, dp = _build_bot_dispatcher(config)

        FIRST_USER = 111
        await dp.feed_webhook_update(bot, _text_update(1, FIRST_USER, "/start"))
        self.assertEqual(evm._load_admins(config.admins_file), {"111"})
        self.assertTrue(evm._is_bot_admin(FIRST_USER, config))

    async def test_bootstrap_admin_syncs_to_central_bot_admins_table(self):
        """The mini-app's admin gate (runtime.miniapp_api._admin_gate_ok)
        checks db.database.get_bot_admins() — a separate table from this
        template's local admins_file. The bootstrap grant must land in both."""
        config = evm.config_from_bot_row(
            {"bot_id": 604, "name": "event_bot_synced", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await evm.init_db(config.db_path)
        bot, dp = _build_bot_dispatcher(config)
        await dp.feed_webhook_update(bot, _text_update(1, 321, "/start"))

        central_admins = await db_module.get_bot_admins(604)
        self.assertEqual(central_admins, ["321"])


if __name__ == "__main__":
    unittest.main()
