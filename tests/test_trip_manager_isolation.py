"""Stage 2 Phase 6 — data isolation test for the trip_manager template's config
переезд (fourth reference template, after accountant/manager_secretary/booking_beauty).

Standard criterion: two bots on the SAME template, different config, must
never mix data — even driven by the SAME Telegram user_id.

PLUS two checks specific to this template's own mechanics (per the Phase 6
design note, docs/STAGE2_DESIGN.md "Фаза 6"):
- display_name in handle_group_mention — same class of live usage as
  manager_secretary's, own name must reach the Anthropic system prompt per bot
  (not a shared/last-built constant), and a mention of the WRONG bot's name
  must not trigger a reply — mirrors test_manager_secretary_isolation.py.
- _remind() — the one background-task mechanic new to this template (not
  present in accountant/manager_secretary/booking_beauty). Confirms a
  reminder scheduled by one bot fires through THAT bot's own Bot/chat, with
  the item correctly persisted in that bot's own db file, not the other bot's.
  (The _digest_loop() mechanic is deliberately NOT tested here in webhook mode
  — runtime/registry.py does not start it for webhook-registered bots, see
  docs/STAGE2_DESIGN.md "Стоп: _digest_loop() не ложится на паттерн" — the
  standalone smoke test below only confirms main()/config_from_env() still
  wire it for the subprocess model, unchanged.)

No real Telegram/Anthropic network calls, no real tokens.

Run with: python -m unittest tests.test_trip_manager_isolation
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from runtime.registry import get_template_router
from templates import trip_manager

FAKE_TOKEN = "123456:test-token-not-real"
SAME_USER_ID = 111


def _text_update(update_id: int, user_id: int, text: str) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 1700000000,
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
                "message_id": update_id,
                "date": 1700000000,
                "chat": {"id": user_id, "type": "private"},
                "text": "placeholder",
            },
            "chat_instance": "1",
            "data": data,
        },
    }


def _group_mention_update(update_id: int, chat_id: int, user_id: int, text: str) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 1700000000,
            "chat": {"id": chat_id, "type": "group", "title": "Test Group"},
            "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
            "text": text,
        },
    }


def _build_bot_dispatcher(config: trip_manager.TripManagerConfig) -> tuple[Bot, Dispatcher]:
    """Mirrors runtime/registry.py's build_entry() for this template: fresh
    Dispatcher, cloned Router (Phase 1 fix), the template's own typed
    ConfigMiddleware (Phase 6)."""
    bot = Bot(token=FAKE_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(trip_manager.ConfigMiddleware(config))
    dp.include_router(get_template_router("trip_manager"))
    return bot, dp


async def _create_trip(dp: Dispatcher, bot: Bot, user_id: int, name: str, start_id: int) -> int:
    uid = start_id
    await dp.feed_webhook_update(bot, _callback_update(uid, user_id, "trip_new")); uid += 1
    await dp.feed_webhook_update(bot, _text_update(uid, user_id, name)); uid += 1
    return uid


async def _add_item_with_past_reminder(
    dp: Dispatcher, bot: Bot, user_id: int, title: str, start_id: int
) -> int:
    """Drives the full Add FSM flow (type -> title -> destination -> date_start
    -> ... -> remind), skipping every optional field except date_start (needed
    so a reminder can be computed) and remind (the field under test). Uses a
    date_start far in the past + remind_raw="0m" so the computed remind_at is
    already due — _remind()'s delay is negative, it sends immediately with no
    real asyncio.sleep, keeping the test fast and deterministic."""
    uid = start_id
    await dp.feed_webhook_update(bot, _text_update(uid, user_id, "➕ Добавить")); uid += 1
    await dp.feed_webhook_update(bot, _callback_update(uid, user_id, "itype:hotel")); uid += 1
    await dp.feed_webhook_update(bot, _text_update(uid, user_id, title)); uid += 1
    await dp.feed_webhook_update(bot, _callback_update(uid, user_id, "iskip:destination")); uid += 1
    await dp.feed_webhook_update(bot, _text_update(uid, user_id, "20.07.2024")); uid += 1  # date_start (past)
    await dp.feed_webhook_update(bot, _callback_update(uid, user_id, "iskip:time_start")); uid += 1
    await dp.feed_webhook_update(bot, _callback_update(uid, user_id, "iskip:date_end")); uid += 1
    await dp.feed_webhook_update(bot, _callback_update(uid, user_id, "iskip:link")); uid += 1
    await dp.feed_webhook_update(bot, _callback_update(uid, user_id, "iskip:confirm_num")); uid += 1
    await dp.feed_webhook_update(bot, _callback_update(uid, user_id, "iskip:price")); uid += 1
    await dp.feed_webhook_update(bot, _callback_update(uid, user_id, "iskip:prepayment")); uid += 1
    await dp.feed_webhook_update(bot, _callback_update(uid, user_id, "iskip:notes")); uid += 1
    await dp.feed_webhook_update(bot, _text_update(uid, user_id, "0m")); uid += 1  # remind (message path -> _finalize)
    return uid


class TripManagerIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._bot_call_mock = AsyncMock(return_value=MagicMock())
        self._bot_call_patcher = patch.object(Bot, "__call__", new=self._bot_call_mock)
        self._bot_call_patcher.start()

        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

        self.config_a = trip_manager.config_from_bot_row(
            {"bot_id": 101, "name": "tm_isolation_bot_a", "display_name": "Марта", "group_chat_id": None}, self.data_dir
        )
        self.config_b = trip_manager.config_from_bot_row(
            {"bot_id": 102, "name": "tm_isolation_bot_b", "display_name": "Тревел", "group_chat_id": None}, self.data_dir
        )
        await trip_manager.init_db(self.config_a.db_path)
        await trip_manager.init_db(self.config_b.db_path)

        self.bot_a, self.dp_a = _build_bot_dispatcher(self.config_a)
        self.bot_b, self.dp_b = _build_bot_dispatcher(self.config_b)

    async def asyncTearDown(self):
        # Let any still-pending reminder tasks (should be none by teardown time
        # in these tests, but be safe) finish before the temp dir disappears.
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_configs_point_to_different_files(self):
        self.assertNotEqual(self.config_a.db_path, self.config_b.db_path)
        self.assertNotEqual(self.config_a.admins_file, self.config_b.admins_file)
        self.assertNotEqual(self.config_a.display_name, self.config_b.display_name)

    async def test_two_bots_same_user_create_trips_into_separate_db_files(self):
        await _create_trip(self.dp_a, self.bot_a, SAME_USER_ID, "Alpha Trip", 1)
        await _create_trip(self.dp_b, self.bot_b, SAME_USER_ID, "Beta Trip", 1)

        conn_a = sqlite3.connect(self.config_a.db_path)
        names_a = [r[0] for r in conn_a.execute("SELECT name FROM trips").fetchall()]
        conn_a.close()
        conn_b = sqlite3.connect(self.config_b.db_path)
        names_b = [r[0] for r in conn_b.execute("SELECT name FROM trips").fetchall()]
        conn_b.close()

        self.assertEqual(names_a, ["Alpha Trip"])
        self.assertEqual(names_b, ["Beta Trip"])

    async def test_admin_bootstrap_isolated_per_bot(self):
        await self.dp_a.feed_webhook_update(self.bot_a, _text_update(1, SAME_USER_ID, "/start"))
        await self.dp_b.feed_webhook_update(self.bot_b, _text_update(1, 999, "/start"))

        admins_a = json.loads(self.config_a.admins_file.read_text())["ids"]
        admins_b = json.loads(self.config_b.admins_file.read_text())["ids"]

        self.assertEqual(admins_a, [str(SAME_USER_ID)])
        self.assertEqual(admins_b, ["999"])

    async def test_same_name_different_bot_id_still_isolated(self):
        """The actual case this phase closes: bots.name has no UNIQUE
        constraint, so two DB rows can share the exact same name. Before this
        phase, config_from_bot_row() built paths from bot_row["name"] — two
        same-named bots would have shared one db/admins file. Now paths are
        built from bot_row["bot_id"] (the physically unique PK), so even
        identical names must not collide."""
        config_c = trip_manager.config_from_bot_row(
            {"bot_id": 201, "name": "duplicate_name", "display_name": "Вера", "group_chat_id": None}, self.data_dir
        )
        config_d = trip_manager.config_from_bot_row(
            {"bot_id": 202, "name": "duplicate_name", "display_name": "Гриша", "group_chat_id": None}, self.data_dir
        )
        self.assertEqual(config_c.bot_name, config_d.bot_name)
        self.assertNotEqual(config_c.db_path, config_d.db_path)
        self.assertNotEqual(config_c.admins_file, config_d.admins_file)

        await trip_manager.init_db(config_c.db_path)
        await trip_manager.init_db(config_d.db_path)
        bot_c, dp_c = _build_bot_dispatcher(config_c)
        bot_d, dp_d = _build_bot_dispatcher(config_d)

        await _create_trip(dp_c, bot_c, SAME_USER_ID, "Gamma Trip", 1)
        await _create_trip(dp_d, bot_d, SAME_USER_ID, "Delta Trip", 1)

        conn_c = sqlite3.connect(config_c.db_path)
        names_c = [r[0] for r in conn_c.execute("SELECT name FROM trips").fetchall()]
        conn_c.close()
        conn_d = sqlite3.connect(config_d.db_path)
        names_d = [r[0] for r in conn_d.execute("SELECT name FROM trips").fetchall()]
        conn_d.close()

        self.assertEqual(names_c, ["Gamma Trip"])
        self.assertEqual(names_d, ["Delta Trip"])

    async def test_group_mention_uses_own_display_name_in_claude_prompt(self):
        """Same class of trap as manager_secretary's display_name check: proves
        config.display_name (not a shared/module constant) reaches the Anthropic
        call per-bot."""
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(
            return_value=MagicMock(content=[MagicMock(text="mocked reply")])
        )

        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            await self.dp_a.feed_webhook_update(
                self.bot_a, _group_mention_update(1, 5001, SAME_USER_ID, "Марта, привет!")
            )
            self.assertEqual(mock_client.messages.create.call_count, 1)
            system_prompt_a = mock_client.messages.create.call_args.kwargs["system"]
            self.assertIn("Марта", system_prompt_a)
            self.assertNotIn("Тревел", system_prompt_a)

            mock_client.messages.create.reset_mock()

            await self.dp_b.feed_webhook_update(
                self.bot_b, _group_mention_update(1, 5002, SAME_USER_ID, "Тревел, привет!")
            )
            self.assertEqual(mock_client.messages.create.call_count, 1)
            system_prompt_b = mock_client.messages.create.call_args.kwargs["system"]
            self.assertIn("Тревел", system_prompt_b)
            self.assertNotIn("Марта", system_prompt_b)

    async def test_group_mention_of_wrong_name_does_not_trigger(self):
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(
            return_value=MagicMock(content=[MagicMock(text="mocked reply")])
        )
        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            await self.dp_a.feed_webhook_update(
                self.bot_a, _group_mention_update(1, 5003, SAME_USER_ID, "Тревел, привет!")
            )
            self.assertEqual(mock_client.messages.create.call_count, 0)

    async def test_reminder_fires_through_correct_bot_and_persists_in_own_db(self):
        """The mechanic unique to trip_manager (Task 1 finding): _remind() is a
        fire-and-forget asyncio.create_task spawned from _finalize(). Drives
        bot A through the full add-item flow with a reminder already due
        (past date_start + "0m"), then bot B through a plain add with NO
        reminder. Confirms: (1) bot A's item, including its computed
        remind_at, lands only in bot A's own db; (2) the reminder task, once
        awaited to completion, sent through bot A's own Bot/chat with the
        item's own title — not bot B's, and bot B's item has no remind_at."""
        await _create_trip(self.dp_a, self.bot_a, SAME_USER_ID, "Alpha Trip", 1)
        await _add_item_with_past_reminder(
            self.dp_a, self.bot_a, SAME_USER_ID, "Alpha Reminder Item", 100
        )

        await _create_trip(self.dp_b, self.bot_b, SAME_USER_ID, "Beta Trip", 1)
        # Bot B adds an item too, but never enters a remind_raw (uses the skip
        # path for the remind step instead of a message) — no reminder should
        # ever be scheduled for it.
        uid = 100
        await self.dp_b.feed_webhook_update(self.bot_b, _text_update(uid, SAME_USER_ID, "➕ Добавить")); uid += 1
        await self.dp_b.feed_webhook_update(self.bot_b, _callback_update(uid, SAME_USER_ID, "itype:flight")); uid += 1
        await self.dp_b.feed_webhook_update(self.bot_b, _text_update(uid, SAME_USER_ID, "Beta No-Reminder Item")); uid += 1
        for step in ("destination", "date_start", "time_start", "date_end", "link",
                     "confirm_num", "price", "prepayment", "notes", "remind"):
            await self.dp_b.feed_webhook_update(self.bot_b, _callback_update(uid, SAME_USER_ID, f"iskip:{step}")); uid += 1

        # Let the fire-and-forget reminder task (scheduled for bot A only) run
        # to completion — its delay is negative (date_start is in the past),
        # so it sends immediately without a real asyncio.sleep.
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        await asyncio.gather(*pending)

        conn_a = sqlite3.connect(self.config_a.db_path)
        row_a = conn_a.execute("SELECT title, remind_at FROM items").fetchone()
        conn_a.close()
        conn_b = sqlite3.connect(self.config_b.db_path)
        row_b = conn_b.execute("SELECT title, remind_at FROM items").fetchone()
        conn_b.close()

        self.assertEqual(row_a[0], "Alpha Reminder Item")
        self.assertIsNotNone(row_a[1], "bot A's item should have a computed remind_at")
        self.assertEqual(row_b[0], "Beta No-Reminder Item")
        self.assertIsNone(row_b[1], "bot B's item took the skip path and must have no remind_at")

        # Inspect the shared Bot.__call__ mock for the one SendMessage the
        # reminder task made — must carry bot A's chat_id and item title.
        reminder_calls = [
            call for call in self._bot_call_mock.call_args_list
            if (getattr(call.args[0], "text", None) or "").startswith("⏰ <b>Напоминание:</b>")
        ]
        self.assertEqual(len(reminder_calls), 1)
        sent_method = reminder_calls[0].args[0]
        self.assertEqual(sent_method.chat_id, SAME_USER_ID)
        self.assertIn("Alpha Reminder Item", sent_method.text)


class TripManagerStandaloneSmokeTest(unittest.TestCase):
    """Confirms the template still imports and initializes fine outside the
    webhook runtime — the subprocess model (including _digest_loop, which the
    webhook runtime deliberately does not start) must keep working unmodified."""

    def test_config_from_env_matches_legacy_constant_shape(self):
        config = trip_manager.config_from_env()
        self.assertTrue(config.db_path.endswith("trip_manager_data.db"))
        self.assertTrue(str(config.admins_file).endswith("admins_trip_manager.json"))
        self.assertTrue(config.excel_path.endswith("trip_manager_data.xlsx"))
        self.assertEqual(config.bot_name, "trip_manager")

    def test_router_main_and_digest_loop_entrypoints_exist(self):
        self.assertTrue(hasattr(trip_manager, "router"))
        self.assertTrue(hasattr(trip_manager, "main"))
        self.assertTrue(hasattr(trip_manager, "_digest_loop"))


if __name__ == "__main__":
    unittest.main()
