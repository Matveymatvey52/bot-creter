"""Stage E: office/multi-bot gathering — JSON plan parsing
(services/claude_service.parse_gather_result), READY_TO_GENERATE wiring for
several bots (handlers/create_bot._process_gathering_content), and the
BotFather-repeat-per-bot queue continuation + office_links wiring at the end
(handlers/create_bot._continue_office_queue / _finish_office_plan). See
docs/MULTIBOT_OFFICE_ROUTING_DESIGN.md for the design this implements.

Run with: python -m unittest tests.test_office_multibot_gather
"""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import handlers.create_bot as create_bot_module
from services.claude_service import parse_gather_result

BOT_ID = 999
USER_ID = 12345


def _state(storage: MemoryStorage | None = None) -> FSMContext:
    storage = storage or MemoryStorage()
    key = StorageKey(bot_id=BOT_ID, chat_id=USER_ID, user_id=USER_ID)
    return FSMContext(storage=storage, key=key)


def _message() -> MagicMock:
    message = MagicMock()
    message.from_user.id = USER_ID
    message.answer = AsyncMock(return_value=MagicMock(delete=AsyncMock()))
    # _process_gathering_content routes its Claude-call response through
    # message.bot.send_message (not message.answer) so the same code path can
    # be re-entered from a callback's retry button — see cb_retry_gather.
    message.bot.send_message = AsyncMock(return_value=MagicMock(delete=AsyncMock()))
    return message


class ParseGatherResultTests(unittest.TestCase):
    def test_single_bot_json_parses_with_empty_links(self):
        payload = '{"bots": [{"role_hint": "orders", "summary": "a shop bot"}], "links": []}'
        result = parse_gather_result(payload)
        self.assertEqual(len(result["bots"]), 1)
        self.assertEqual(result["links"], [])

    def test_multi_bot_office_parses_links(self):
        payload = (
            '{"bots": ['
            '{"role_hint": "orders", "summary": "shop bot"},'
            '{"role_hint": "acc", "summary": "accounting bot"}'
            '], "links": [{"source_role_hint": "orders", "target_role_hint": "acc", "event_type": "order.created"}]}'
        )
        result = parse_gather_result(payload)
        self.assertEqual(len(result["bots"]), 2)
        self.assertEqual(result["links"][0]["event_type"], "order.created")

    def test_hallucinated_event_type_is_dropped(self):
        payload = (
            '{"bots": [{"role_hint": "a", "summary": "s1"}, {"role_hint": "b", "summary": "s2"}], '
            '"links": [{"source_role_hint": "a", "target_role_hint": "b", "event_type": "made.up.type"}]}'
        )
        result = parse_gather_result(payload)
        self.assertEqual(result["links"], [])

    def test_plain_text_legacy_fallback_is_single_bot(self):
        result = parse_gather_result("Just a plain-text bot summary, no JSON.")
        self.assertEqual(len(result["bots"]), 1)
        self.assertIn("plain-text", result["bots"][0]["summary"])
        self.assertEqual(result["links"], [])

    def test_empty_payload_returns_none(self):
        self.assertIsNone(parse_gather_result("   "))


class ProcessGatheringContentOfficeTests(unittest.IsolatedAsyncioTestCase):
    async def test_multi_bot_response_stores_office_plan(self):
        state = _state()
        await state.update_data(conversation=[], pending_attachments=[])
        message = _message()

        ready_response = (
            "===READY_TO_GENERATE===\n"
            '{"bots": [{"role_hint": "orders", "summary": "orders bot"}, '
            '{"role_hint": "acc", "summary": "accounting bot"}], '
            '"links": [{"source_role_hint": "orders", "target_role_hint": "acc", "event_type": "order.created"}]}'
        )
        with patch.object(create_bot_module, "chat_gather_requirements", AsyncMock(return_value=ready_response)), \
             patch.object(create_bot_module, "extract_bot_name", AsyncMock(return_value="orders_bot")):
            await create_bot_module._process_gathering_content(message, state, "build an office")

        data = await state.get_data()
        self.assertEqual(len(data["office_bots"]), 2)
        self.assertEqual(data["office_index"], 0)
        self.assertEqual(data["bot_summary"], "orders bot")
        final_call = message.bot.send_message.await_args_list[-1]
        self.assertIn("офис из 2 ботов", final_call.args[1])


class ContinueOfficeQueueTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        create_bot_module.set_bot_id(BOT_ID)

    async def test_no_op_for_single_bot_plan(self):
        pending = {"chat_id": 1, "office_bots": [{"role_hint": "solo", "summary": "s"}]}
        bot = MagicMock()
        bot.send_message = AsyncMock()
        advanced = await create_bot_module._continue_office_queue(pending, 42, USER_ID, bot, MemoryStorage())
        self.assertFalse(advanced)
        bot.send_message.assert_not_awaited()

    async def test_advances_to_next_bot_and_sets_fsm_state(self):
        storage = MemoryStorage()
        pending = {
            "chat_id": 1,
            "office_role_hint": "orders",
            "office_bot_ids": {},
            "office_index": 0,
            "office_bots": [
                {"role_hint": "orders", "summary": "orders bot"},
                {"role_hint": "acc", "summary": "accounting bot"},
            ],
            "office_links": [
                {"source_role_hint": "orders", "target_role_hint": "acc", "event_type": "order.created"}
            ],
        }
        bot = MagicMock()
        bot.send_message = AsyncMock()

        with patch.object(create_bot_module, "extract_bot_name", AsyncMock(return_value="accounting_bot")):
            advanced = await create_bot_module._continue_office_queue(pending, 111, USER_ID, bot, storage)

        self.assertTrue(advanced)
        self.assertEqual(pending["office_bot_ids"]["orders"], 111)

        next_state = _state(storage)
        self.assertEqual(await next_state.get_state(), create_bot_module.CreateBotStates.waiting_for_display_name.state)
        data = await next_state.get_data()
        self.assertEqual(data["bot_summary"], "accounting bot")
        self.assertEqual(data["office_index"], 1)
        self.assertEqual(data["office_bot_ids"], {"orders": 111})
        bot.send_message.assert_awaited()

    async def test_last_bot_wires_office_links_and_reports(self):
        pending = {
            "chat_id": 1,
            "office_role_hint": "acc",
            "office_bot_ids": {"orders": 111},
            "office_index": 1,
            "office_bots": [
                {"role_hint": "orders", "summary": "orders bot"},
                {"role_hint": "acc", "summary": "accounting bot"},
            ],
            "office_links": [
                {"source_role_hint": "orders", "target_role_hint": "acc", "event_type": "order.created"}
            ],
        }
        bot = MagicMock()
        bot.send_message = AsyncMock()

        with patch.object(create_bot_module, "add_office_link", AsyncMock(return_value=True)) as mock_link:
            advanced = await create_bot_module._continue_office_queue(pending, 222, USER_ID, bot, MemoryStorage())

        self.assertFalse(advanced)
        mock_link.assert_awaited_once_with(111, 222, "order.created")
        summary_call = bot.send_message.await_args
        self.assertIn("Связей создано: 1 из 1", summary_call.args[1])

    async def test_last_bot_reports_skipped_link_when_role_missing(self):
        pending = {
            "chat_id": 1,
            "office_role_hint": "acc",
            "office_bot_ids": {},  # "orders" role never got a real bot_id
            "office_index": 1,
            "office_bots": [
                {"role_hint": "orders", "summary": "orders bot"},
                {"role_hint": "acc", "summary": "accounting bot"},
            ],
            "office_links": [
                {"source_role_hint": "orders", "target_role_hint": "acc", "event_type": "order.created"}
            ],
        }
        bot = MagicMock()
        bot.send_message = AsyncMock()

        with patch.object(create_bot_module, "add_office_link", AsyncMock(return_value=True)) as mock_link:
            await create_bot_module._continue_office_queue(pending, 222, USER_ID, bot, MemoryStorage())

        mock_link.assert_not_awaited()
        summary_call = bot.send_message.await_args
        self.assertIn("Связей создано: 0 из 1", summary_call.args[1])


if __name__ == "__main__":
    unittest.main()
