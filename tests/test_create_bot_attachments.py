"""handlers/create_bot.py's multimedia-context gathering: screenshots/photos
(vision), documents (PDF/Word/Excel text extraction), the pending-attachment
buffer, and the new `confirming` state final question before generation.

Handlers are called directly (see tests/test_start_buttons.py's note on why —
create_bot's router is already attached to a live Dispatcher elsewhere in the
suite).

Run with: python -m unittest tests.test_create_bot_attachments
"""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import handlers.create_bot as create_bot_module

BOT_ID = 999
USER_ID = 12345


def _state() -> FSMContext:
    storage = MemoryStorage()
    key = StorageKey(bot_id=BOT_ID, chat_id=USER_ID, user_id=USER_ID)
    return FSMContext(storage=storage, key=key)


def _fake_bot(payload: bytes = b"filebytes") -> MagicMock:
    bot = MagicMock()
    bot.get_file = AsyncMock(return_value=MagicMock(file_path="remote/path"))

    async def _download(file_path, destination=None, **kwargs):
        destination.write(payload)
        return destination

    bot.download_file = AsyncMock(side_effect=_download)
    return bot


def _message(**overrides) -> MagicMock:
    message = MagicMock()
    message.from_user.id = USER_ID
    message.answer = AsyncMock(return_value=MagicMock(delete=AsyncMock()))
    # _process_gathering_content routes its Claude-call response through
    # message.bot.send_message (not message.answer) so the same code path can
    # be re-entered from a callback's retry button — see cb_retry_gather.
    message.bot.send_message = AsyncMock(return_value=MagicMock(delete=AsyncMock()))
    for k, v in overrides.items():
        setattr(message, k, v)
    return message


class GatheringPhotoTests(unittest.IsolatedAsyncioTestCase):
    async def test_photo_buffers_image_block_and_acks(self):
        state = _state()
        await state.set_state(create_bot_module.CreateBotStates.gathering)
        await state.update_data(conversation=[], pending_attachments=[])

        photo = MagicMock(file_id="ph1")
        message = _message(photo=[photo])
        bot = _fake_bot(b"\xff\xd8fakejpeg")

        await create_bot_module.handle_gathering_photo(message, state, bot)

        data = await state.get_data()
        pending = data["pending_attachments"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["type"], "image")
        self.assertEqual(pending[0]["source"]["media_type"], "image/jpeg")
        message.answer.assert_awaited()

    async def test_attachment_buffer_caps_at_max(self):
        state = _state()
        await state.set_state(create_bot_module.CreateBotStates.gathering)
        full = [{"type": "text", "text": "x"}] * create_bot_module.MAX_PENDING_ATTACHMENTS
        await state.update_data(conversation=[], pending_attachments=full)

        photo = MagicMock(file_id="ph2")
        message = _message(photo=[photo])
        bot = _fake_bot()

        await create_bot_module.handle_gathering_photo(message, state, bot)

        data = await state.get_data()
        self.assertEqual(len(data["pending_attachments"]), create_bot_module.MAX_PENDING_ATTACHMENTS)
        self.assertIn("Уже накопил", message.answer.await_args.args[0])


class GatheringDocumentTests(unittest.IsolatedAsyncioTestCase):
    async def test_pdf_document_extracted_into_pending(self):
        state = _state()
        await state.set_state(create_bot_module.CreateBotStates.gathering)
        await state.update_data(conversation=[], pending_attachments=[])

        doc = MagicMock(file_id="doc1", file_name="brief.pdf", mime_type="application/pdf", file_size=1000)
        message = _message(document=doc)
        bot = _fake_bot(b"%PDF-fake")

        with patch.object(create_bot_module, "extract_document_text", return_value="Extracted PDF text"):
            await create_bot_module.handle_gathering_document(message, state, bot)

        data = await state.get_data()
        pending = data["pending_attachments"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["type"], "text")
        self.assertIn("Extracted PDF text", pending[0]["text"])
        self.assertIn("brief.pdf", pending[0]["text"])

    async def test_unsupported_document_rejected_with_message(self):
        state = _state()
        await state.set_state(create_bot_module.CreateBotStates.gathering)
        await state.update_data(conversation=[], pending_attachments=[])

        doc = MagicMock(file_id="doc2", file_name="archive.zip", mime_type="application/zip", file_size=1000)
        message = _message(document=doc)
        bot = _fake_bot(b"PK\x03\x04")

        with patch.object(create_bot_module, "extract_document_text", return_value=None):
            await create_bot_module.handle_gathering_document(message, state, bot)

        data = await state.get_data()
        self.assertEqual(data["pending_attachments"], [])
        self.assertIn("PDF", message.answer.await_args.args[0])

    async def test_oversized_document_rejected_without_download(self):
        state = _state()
        await state.set_state(create_bot_module.CreateBotStates.gathering)
        await state.update_data(conversation=[], pending_attachments=[])

        doc = MagicMock(
            file_id="doc3",
            file_name="huge.pdf",
            mime_type="application/pdf",
            file_size=create_bot_module.MAX_DOCUMENT_SIZE_BYTES + 1,
        )
        message = _message(document=doc)
        bot = _fake_bot()

        await create_bot_module.handle_gathering_document(message, state, bot)

        bot.get_file.assert_not_awaited()
        self.assertIn("слишком большой", message.answer.await_args.args[0])

    async def test_image_sent_as_document_becomes_image_block(self):
        state = _state()
        await state.set_state(create_bot_module.CreateBotStates.gathering)
        await state.update_data(conversation=[], pending_attachments=[])

        doc = MagicMock(file_id="doc4", file_name="shot.png", mime_type="image/png", file_size=1000)
        message = _message(document=doc)
        bot = _fake_bot(b"\x89PNGfake")

        await create_bot_module.handle_gathering_document(message, state, bot)

        data = await state.get_data()
        pending = data["pending_attachments"]
        self.assertEqual(pending[0]["type"], "image")
        self.assertEqual(pending[0]["source"]["media_type"], "image/png")


class ProcessGatheringContentTests(unittest.IsolatedAsyncioTestCase):
    async def test_merges_pending_attachments_with_text_and_clears_buffer(self):
        state = _state()
        image_block = {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "abc"}}
        await state.update_data(conversation=[], pending_attachments=[image_block])
        message = _message()

        with patch.object(create_bot_module, "chat_gather_requirements", AsyncMock(return_value="Tell me more?")):
            await create_bot_module._process_gathering_content(message, state, "here's context")

        data = await state.get_data()
        self.assertEqual(data["pending_attachments"], [])
        conversation = data["conversation"]
        self.assertEqual(conversation[0]["role"], "user")
        blocks = conversation[0]["content"]
        self.assertEqual(blocks[0], image_block)
        self.assertEqual(blocks[1], {"type": "text", "text": "here's context"})

    async def test_ready_marker_moves_to_confirming_with_button(self):
        state = _state()
        await state.update_data(conversation=[], pending_attachments=[])
        message = _message()

        ready_response = "===READY_TO_GENERATE===\nA reminders bot"
        with patch.object(create_bot_module, "chat_gather_requirements", AsyncMock(return_value=ready_response)), \
             patch.object(create_bot_module, "extract_bot_name", AsyncMock(return_value="reminders_bot")):
            await create_bot_module._process_gathering_content(message, state, "build me a reminders bot")

        self.assertEqual(await state.get_state(), create_bot_module.CreateBotStates.confirming.state)
        data = await state.get_data()
        self.assertEqual(data["bot_name"], "reminders_bot")
        self.assertIn("A reminders bot", data["bot_summary"])

        final_call = message.bot.send_message.await_args_list[-1]
        self.assertIn("Я готов создавать бота", final_call.args[1])
        self.assertIn("reply_markup", final_call.kwargs)

    async def test_no_op_when_no_text_and_no_pending_attachments(self):
        state = _state()
        await state.update_data(conversation=[], pending_attachments=[])
        message = _message()

        with patch.object(create_bot_module, "chat_gather_requirements", AsyncMock()) as mock_chat:
            await create_bot_module._process_gathering_content(message, state, None)

        mock_chat.assert_not_awaited()
        message.bot.send_message.assert_not_awaited()


class ConfirmingStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirm_generate_moves_to_waiting_for_display_name(self):
        state = _state()
        await state.set_state(create_bot_module.CreateBotStates.confirming)
        await state.update_data(conversation=[], bot_summary="x", bot_name="x")

        callback = MagicMock()
        callback.from_user.id = USER_ID
        callback.answer = AsyncMock()
        callback.message.answer = AsyncMock()

        await create_bot_module.cb_confirm_generate(callback, state)

        self.assertEqual(
            await state.get_state(),
            create_bot_module.CreateBotStates.waiting_for_display_name.state,
        )
        callback.message.answer.assert_awaited()

    async def test_gathering_continue_processes_buffered_attachments(self):
        state = _state()
        await state.set_state(create_bot_module.CreateBotStates.gathering)
        image_block = {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "abc"}}
        await state.update_data(conversation=[], pending_attachments=[image_block])

        callback = MagicMock()
        callback.from_user.id = USER_ID
        callback.answer = AsyncMock()
        callback.message.answer = AsyncMock(return_value=MagicMock(delete=AsyncMock()))
        callback.message.bot.send_message = AsyncMock(return_value=MagicMock(delete=AsyncMock()))

        with patch.object(create_bot_module, "chat_gather_requirements", AsyncMock(return_value="What's this for?")):
            await create_bot_module.cb_gathering_continue(callback, state)

        data = await state.get_data()
        self.assertEqual(data["pending_attachments"], [])
        self.assertEqual(len(data["conversation"]), 2)

    async def test_gathering_continue_with_empty_buffer_tells_user(self):
        state = _state()
        await state.set_state(create_bot_module.CreateBotStates.gathering)
        await state.update_data(conversation=[], pending_attachments=[])

        callback = MagicMock()
        callback.from_user.id = USER_ID
        callback.answer = AsyncMock()
        callback.message.answer = AsyncMock()

        await create_bot_module.cb_gathering_continue(callback, state)

        callback.message.answer.assert_awaited_once()
        self.assertIn("Вложений нет", callback.message.answer.await_args.args[0])


class FirstUserMessageTests(unittest.TestCase):
    def test_extracts_text_from_string_content(self):
        conv = [{"role": "user", "content": "hello"}]
        self.assertEqual(create_bot_module._first_user_message(conv), "hello")

    def test_extracts_text_from_block_content(self):
        conv = [{"role": "user", "content": [
            {"type": "image", "source": {}},
            {"type": "text", "text": "build a bot"},
        ]}]
        self.assertEqual(create_bot_module._first_user_message(conv), "build a bot")

    def test_none_when_first_turn_is_image_only(self):
        conv = [{"role": "user", "content": [{"type": "image", "source": {}}]}]
        self.assertIsNone(create_bot_module._first_user_message(conv))


if __name__ == "__main__":
    unittest.main()
