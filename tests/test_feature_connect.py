"""Conversational feature-connect flow tests — feature-connect-conversational
Task 3.

Covers: cheap-prefilter cost control before the classifier ever runs, correct
recognition -> the same DB writes + registry reload as the button path,
button-based (never silent-guess) disambiguation when a bot reference is
ambiguous, explicit refusals for incompatible/already-connected features, the
voice path routing identically to the text path, and authorization parity
with the existing owner-only surfaces (/list, togglefeature:).
"""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import handlers.feature_connect as feature_connect
import handlers.general as general
import handlers.manage_bots as manage_bots
import services.claude_service as claude_service
from db.database import (
    create_bot_record_with_admins,
    delete_bot,
    enable_bot_feature,
    get_bot_features,
)

FAKE_TOKEN = "123456789:AAHfakeTokenButShapedRight1234567890"


def _make_message(user_id: int, text: str) -> MagicMock:
    message = MagicMock()
    message.from_user.id = user_id
    message.text = text
    message.answer = AsyncMock()
    return message


def _make_callback(user_id: int, data: str) -> MagicMock:
    callback = MagicMock()
    callback.from_user.id = user_id
    callback.data = data
    callback.answer = AsyncMock()
    callback.message.answer = AsyncMock()
    callback.message.edit_text = AsyncMock()
    return callback


def _fresh_state(user_id: int) -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=0, chat_id=user_id, user_id=user_id))


def _intent(is_connect: bool = True, feature: str | None = None, bot_query: str | None = None) -> dict:
    return {"is_connect_request": is_connect, "feature": feature, "bot_query": bot_query}


def _callback_datas(reply_markup) -> set[str]:
    return {btn.callback_data for row in reply_markup.inline_keyboard for btn in row}


# ── classifier output parsing (services/claude_service.py) ────────────────────

class ClassifierJsonParsingTests(unittest.TestCase):
    def test_parses_well_formed_json(self):
        raw = '{"is_connect_request": true, "feature": "sheets", "bot_query": "магазин"}'
        result = claude_service._parse_connect_feature_intent(raw)
        self.assertEqual(result, {"is_connect_request": True, "feature": "sheets", "bot_query": "магазин"})

    def test_strips_markdown_code_fences(self):
        raw = '```json\n{"is_connect_request": false, "feature": null, "bot_query": null}\n```'
        result = claude_service._parse_connect_feature_intent(raw)
        self.assertFalse(result["is_connect_request"])
        self.assertIsNone(result["feature"])

    def test_malformed_json_degrades_to_not_a_connect_request(self):
        result = claude_service._parse_connect_feature_intent("this is not json")
        self.assertEqual(result, {"is_connect_request": False, "feature": None, "bot_query": None})

    def test_unknown_feature_value_is_dropped_not_passed_through(self):
        raw = '{"is_connect_request": true, "feature": "banana", "bot_query": null}'
        result = claude_service._parse_connect_feature_intent(raw)
        self.assertIsNone(result["feature"])

    def test_blank_bot_query_becomes_none(self):
        raw = '{"is_connect_request": true, "feature": "payments", "bot_query": "   "}'
        result = claude_service._parse_connect_feature_intent(raw)
        self.assertIsNone(result["bot_query"])


# ── cost control: classifier must not run on every message (задача 4) ─────────

class PrefilterCostControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_ordinary_chit_chat_never_calls_classifier(self):
        text = "Привет! Как погода?"
        with patch.object(feature_connect, "classify_connect_feature_intent", AsyncMock()) as mock_classify:
            handled = await feature_connect.maybe_handle_connect_request(_make_message(1, text), text)
        self.assertFalse(handled)
        mock_classify.assert_not_awaited()

    async def test_bot_creation_talk_never_calls_classifier(self):
        text = "Хочу бота который принимает заказы и ведёт учёт клиентов"
        with patch.object(feature_connect, "classify_connect_feature_intent", AsyncMock()) as mock_classify:
            handled = await feature_connect.maybe_handle_connect_request(_make_message(1, text), text)
        self.assertFalse(handled)
        mock_classify.assert_not_awaited()

    async def test_feature_keyword_present_gates_classifier_call(self):
        text = "подключи таблицы к моему боту"
        with patch.object(
            feature_connect, "classify_connect_feature_intent", AsyncMock(return_value=_intent(False))
        ) as mock_classify:
            handled = await feature_connect.maybe_handle_connect_request(_make_message(1, text), text)
        mock_classify.assert_awaited_once_with(text)
        self.assertFalse(handled)  # classifier said no -> falls through to ask_assistant

    async def test_bare_action_verb_without_a_feature_word_never_calls_classifier(self):
        # "подключи"/"включи"/"отключи" alone are common generic verbs
        # ("включи бота", "подключи админа") — must not alone be enough to
        # spend a Claude call; a genuine connect request also names the
        # feature (or says "фича"/"feature" outright).
        for text in ("включи бота обратно", "подключи нового админа", "отключи уведомления"):
            with patch.object(feature_connect, "classify_connect_feature_intent", AsyncMock()) as mock_classify:
                handled = await feature_connect.maybe_handle_connect_request(_make_message(1, text), text)
            self.assertFalse(handled, text)
            mock_classify.assert_not_awaited()

    async def test_action_verb_plus_feature_mention_gates_classifier_call(self):
        text = "подключи фичу к моему боту"
        with patch.object(
            feature_connect, "classify_connect_feature_intent", AsyncMock(return_value=_intent(False))
        ) as mock_classify:
            handled = await feature_connect.maybe_handle_connect_request(_make_message(1, text), text)
        mock_classify.assert_awaited_once_with(text)
        self.assertFalse(handled)

    async def test_classifier_error_is_swallowed_and_falls_through(self):
        text = "подключи оплату к боту"
        with patch.object(
            feature_connect, "classify_connect_feature_intent", AsyncMock(side_effect=RuntimeError("boom"))
        ):
            handled = await feature_connect.maybe_handle_connect_request(_make_message(1, text), text)
        self.assertFalse(handled)


# ── general.py's hook wiring ────────────────────────────────────────────────

class GeneralTextHookWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_handled_connect_request_skips_ask_assistant(self):
        message = _make_message(1, "подключи таблицы к моему боту")
        with patch.object(general, "_is_owner", lambda uid: True), \
             patch.object(general, "maybe_handle_connect_request", AsyncMock(return_value=True)), \
             patch.object(general, "ask_assistant", AsyncMock()) as mock_ask:
            await general.general_text(message)
        mock_ask.assert_not_called()

    async def test_unhandled_message_falls_through_to_ask_assistant_unchanged(self):
        message = _make_message(1, "как дела?")
        with patch.object(general, "_is_owner", lambda uid: True), \
             patch.object(general, "maybe_handle_connect_request", AsyncMock(return_value=False)), \
             patch.object(general, "get_all_bots", AsyncMock(return_value=[])), \
             patch.object(general, "ask_assistant", AsyncMock(return_value="ответ")) as mock_ask:
            await general.general_text(message)
        mock_ask.assert_awaited_once()


# ── confident single-bot / single-feature recognition ──────────────────────────

class ConfidentSingleBotRequestShowsPreviewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot_id = await create_bot_record_with_admins(
            name="fconn_shop_bot", description="test", token=FAKE_TOKEN,
            file_path="templates/inventory.py", admin_ids=["1"],
        )
        self.bot_row = {
            "id": self.bot_id, "name": "fconn_shop_bot", "username": None,
            "display_name": None, "file_path": "templates/inventory.py",
        }

    async def asyncTearDown(self):
        await delete_bot(self.bot_id)

    async def test_confident_request_shows_confirm_and_cancel_buttons(self):
        message = _make_message(1, "подключи таблицы к fconn_shop_bot")
        with patch.object(feature_connect, "get_all_bots", AsyncMock(return_value=[self.bot_row])), \
             patch.object(feature_connect, "classify_connect_feature_intent",
                           AsyncMock(return_value=_intent(True, "sheets", "fconn_shop_bot"))):
            handled = await feature_connect.maybe_handle_connect_request(message, message.text)

        self.assertTrue(handled)
        message.answer.assert_awaited()
        text, kwargs = message.answer.call_args[0][0], message.answer.call_args[1]
        self.assertIn("Google Таблиц", text)
        datas = _callback_datas(kwargs["reply_markup"])
        self.assertIn(f"fconn_confirm:{self.bot_id}:sheets", datas)
        self.assertIn("fconn_cancel", datas)
        # Nothing written yet — this is only a preview, confirmation is required.
        self.assertEqual(await get_bot_features(self.bot_id), [])

    async def test_single_owned_bot_and_no_name_mentioned_auto_selects(self):
        message = _make_message(1, "включи оплату")
        with patch.object(feature_connect, "get_all_bots", AsyncMock(return_value=[self.bot_row])), \
             patch.object(feature_connect, "classify_connect_feature_intent",
                           AsyncMock(return_value=_intent(True, "payments", None))):
            handled = await feature_connect.maybe_handle_connect_request(message, message.text)
        self.assertTrue(handled)
        text = message.answer.call_args[0][0]
        self.assertIn("💳", text)  # preview shown directly, not a bot-choice prompt

    async def test_unclear_feature_asks_which_feature_via_buttons(self):
        message = _make_message(1, "подключи какую-нибудь фичу к fconn_shop_bot")
        with patch.object(feature_connect, "get_all_bots", AsyncMock(return_value=[self.bot_row])), \
             patch.object(feature_connect, "classify_connect_feature_intent",
                           AsyncMock(return_value=_intent(True, None, "fconn_shop_bot"))):
            handled = await feature_connect.maybe_handle_connect_request(message, message.text)
        self.assertTrue(handled)
        kwargs = message.answer.call_args[1]
        datas = _callback_datas(kwargs["reply_markup"])
        self.assertEqual(datas, {f"fconn_feat:{self.bot_id}:sheets", f"fconn_feat:{self.bot_id}:payments"})


# ── ambiguous bot reference: must ask, never guess ─────────────────────────────

class AmbiguousBotReferenceAsksBeforeActingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot_id_a = await create_bot_record_with_admins(
            name="fconn_shop_a", description="t", token=FAKE_TOKEN,
            file_path="templates/inventory.py", admin_ids=["1"],
        )
        self.bot_id_b = await create_bot_record_with_admins(
            name="fconn_shop_b", description="t", token=FAKE_TOKEN,
            file_path="templates/inventory.py", admin_ids=["1"],
        )
        self.bots = [
            {"id": self.bot_id_a, "name": "fconn_shop_a", "username": None, "display_name": None, "file_path": "templates/inventory.py"},
            {"id": self.bot_id_b, "name": "fconn_shop_b", "username": None, "display_name": None, "file_path": "templates/inventory.py"},
        ]

    async def asyncTearDown(self):
        await delete_bot(self.bot_id_a)
        await delete_bot(self.bot_id_b)

    async def test_multiple_bots_and_no_name_triggers_disambiguation_not_a_guess(self):
        message = _make_message(1, "подключи таблицы")
        with patch.object(feature_connect, "get_all_bots", AsyncMock(return_value=self.bots)), \
             patch.object(feature_connect, "classify_connect_feature_intent",
                           AsyncMock(return_value=_intent(True, "sheets", None))):
            handled = await feature_connect.maybe_handle_connect_request(message, message.text)

        self.assertTrue(handled)
        text, kwargs = message.answer.call_args[0][0], message.answer.call_args[1]
        self.assertIn("какой из них", text.lower())
        datas = _callback_datas(kwargs["reply_markup"])
        self.assertEqual(datas, {f"fconn_bot:sheets:{self.bot_id_a}", f"fconn_bot:sheets:{self.bot_id_b}"})
        # Nothing silently enabled while ambiguous.
        self.assertEqual(await get_bot_features(self.bot_id_a), [])
        self.assertEqual(await get_bot_features(self.bot_id_b), [])

    async def test_ambiguous_name_substring_also_triggers_disambiguation(self):
        message = _make_message(1, "подключи таблицы к fconn_shop")
        with patch.object(feature_connect, "get_all_bots", AsyncMock(return_value=self.bots)), \
             patch.object(feature_connect, "classify_connect_feature_intent",
                           AsyncMock(return_value=_intent(True, "sheets", "fconn_shop"))):
            handled = await feature_connect.maybe_handle_connect_request(message, message.text)
        self.assertTrue(handled)
        datas = _callback_datas(message.answer.call_args[1]["reply_markup"])
        self.assertEqual(datas, {f"fconn_bot:sheets:{self.bot_id_a}", f"fconn_bot:sheets:{self.bot_id_b}"})

    async def test_picking_a_bot_via_button_advances_to_preview(self):
        callback = _make_callback(1, f"fconn_bot:sheets:{self.bot_id_a}")
        with patch.object(feature_connect, "_is_owner", lambda uid: True):
            await feature_connect.cb_fconn_bot(callback)
        text, kwargs = callback.message.answer.call_args[0][0], callback.message.answer.call_args[1]
        self.assertIn("Google Таблиц", text)
        self.assertIn(f"fconn_confirm:{self.bot_id_a}:sheets", _callback_datas(kwargs["reply_markup"]))


# ── unknown bot name: explicit refusal, not silence ─────────────────────────────

class UnknownBotNameRefusalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot_id = await create_bot_record_with_admins(
            name="fconn_known_bot", description="t", token=FAKE_TOKEN,
            file_path="templates/inventory.py", admin_ids=["1"],
        )
        self.bot_row = {"id": self.bot_id, "name": "fconn_known_bot", "username": None, "display_name": None, "file_path": "templates/inventory.py"}

    async def asyncTearDown(self):
        await delete_bot(self.bot_id)

    async def test_unmatched_bot_name_gets_explicit_refusal(self):
        message = _make_message(1, "подключи таблицы к несуществующий_бот_xyz")
        with patch.object(feature_connect, "get_all_bots", AsyncMock(return_value=[self.bot_row])), \
             patch.object(feature_connect, "classify_connect_feature_intent",
                           AsyncMock(return_value=_intent(True, "sheets", "несуществующий_бот_xyz"))):
            handled = await feature_connect.maybe_handle_connect_request(message, message.text)
        self.assertTrue(handled)
        text = message.answer.call_args[0][0]
        self.assertIn("не нашёл", text.lower())


# ── incompatible feature: explicit refusal, not silence ─────────────────────────

class IncompatibleFeatureRefusalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # boss_bot is a real template not listed in sheets/payments'
        # # COMPATIBLE_WITH: header (features/sheets.py, features/payments.py) —
        # it's a digital-office coordination bot with no money/record flow
        # either feature fits. (Was templates/channel_monitor.py, but that
        # template was retired in favor of features/channel_monitor.py — see
        # docs/USERBOT_FEATURE_DESIGN.md.)
        self.bot_id = await create_bot_record_with_admins(
            name="fconn_vehicle_bot", description="t", token=FAKE_TOKEN,
            file_path="templates/boss_bot.py", admin_ids=["1"],
        )
        self.bot_row = {"id": self.bot_id, "name": "fconn_vehicle_bot", "username": None, "display_name": None, "file_path": "templates/boss_bot.py"}

    async def asyncTearDown(self):
        await delete_bot(self.bot_id)

    async def test_incompatible_template_gets_explicit_refusal_via_preview(self):
        message = _make_message(1, "подключи оплату к fconn_vehicle_bot")
        with patch.object(feature_connect, "get_all_bots", AsyncMock(return_value=[self.bot_row])), \
             patch.object(feature_connect, "classify_connect_feature_intent",
                           AsyncMock(return_value=_intent(True, "payments", "fconn_vehicle_bot"))):
            handled = await feature_connect.maybe_handle_connect_request(message, message.text)
        self.assertTrue(handled)
        text = message.answer.call_args[0][0]
        self.assertIn("не подходит", text.lower())
        self.assertEqual(await get_bot_features(self.bot_id), [])

    async def test_incompatible_template_is_also_rejected_at_confirm_time(self):
        # Defense in depth: even if a stale confirm button somehow reaches the
        # server (e.g. template changed between preview and tap), the confirm
        # callback re-checks compatibility itself rather than trusting the
        # preview step blindly.
        callback = _make_callback(1, f"fconn_confirm:{self.bot_id}:payments")
        state = _fresh_state(1)
        with patch.object(feature_connect, "_is_owner", lambda uid: True):
            await feature_connect.cb_fconn_confirm(callback, state)
        # cb_fconn_confirm edits the tapped message in place (same
        # _edit_or_resend pattern as the rest of manage_bots.py's panels)
        # rather than sending a new one — see the fconn_cancel/fconn_confirm
        # "stale button" fix.
        text = callback.message.edit_text.call_args[0][0]
        self.assertIn("не подходит", text.lower())
        self.assertEqual(await get_bot_features(self.bot_id), [])


# ── already-connected feature: explicit refusal, not silence ───────────────────

class AlreadyConnectedRefusalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot_id = await create_bot_record_with_admins(
            name="fconn_paid_bot", description="t", token=FAKE_TOKEN,
            file_path="templates/inventory.py", admin_ids=["1"],
        )
        await enable_bot_feature(self.bot_id, "payments")
        self.bot_row = {"id": self.bot_id, "name": "fconn_paid_bot", "username": None, "display_name": None, "file_path": "templates/inventory.py"}

    async def asyncTearDown(self):
        await delete_bot(self.bot_id)

    async def test_already_connected_payments_gets_explicit_refusal(self):
        message = _make_message(1, "включи оплату у fconn_paid_bot")
        with patch.object(feature_connect, "get_all_bots", AsyncMock(return_value=[self.bot_row])), \
             patch.object(feature_connect, "classify_connect_feature_intent",
                           AsyncMock(return_value=_intent(True, "payments", "fconn_paid_bot"))):
            handled = await feature_connect.maybe_handle_connect_request(message, message.text)
        self.assertTrue(handled)
        text = message.answer.call_args[0][0]
        self.assertIn("уже", text.lower())


class SheetsToggledButNeverLinkedStillOffersConnectTests(unittest.IsolatedAsyncioTestCase):
    """sheets can be enabled in bot_features without a spreadsheet ever having
    been linked (e.g. an abandoned button flow) — the conversational path must
    still offer to finish connecting it rather than reporting "already
    connected" for a feature that was only half set up."""

    async def asyncSetUp(self):
        self.bot_id = await create_bot_record_with_admins(
            name="fconn_dangling_sheets_bot", description="t", token=FAKE_TOKEN,
            file_path="templates/inventory.py", admin_ids=["1"],
        )
        await enable_bot_feature(self.bot_id, "sheets")
        self.bot_row = {"id": self.bot_id, "name": "fconn_dangling_sheets_bot", "username": None, "display_name": None, "file_path": "templates/inventory.py"}

    async def asyncTearDown(self):
        await delete_bot(self.bot_id)

    async def test_dangling_sheets_toggle_still_shows_confirm(self):
        message = _make_message(1, "подключи таблицы к fconn_dangling_sheets_bot")
        with patch.object(feature_connect, "get_all_bots", AsyncMock(return_value=[self.bot_row])), \
             patch.object(feature_connect, "classify_connect_feature_intent",
                           AsyncMock(return_value=_intent(True, "sheets", "fconn_dangling_sheets_bot"))):
            handled = await feature_connect.maybe_handle_connect_request(message, message.text)
        self.assertTrue(handled)
        kwargs = message.answer.call_args[1]
        self.assertIn(f"fconn_confirm:{self.bot_id}:sheets", _callback_datas(kwargs["reply_markup"]))


# ── equivalence with the button path (задача 3's core criterion) ──────────────

class ConfirmEquivalentToButtonToggleTests(unittest.IsolatedAsyncioTestCase):
    owner_id = 777001

    async def asyncSetUp(self):
        self.bot_id_button = await create_bot_record_with_admins(
            name="fconn_equiv_button", description="t", token=FAKE_TOKEN,
            file_path="templates/inventory.py", admin_ids=[str(self.owner_id)],
        )
        self.bot_id_convo = await create_bot_record_with_admins(
            name="fconn_equiv_convo", description="t", token=FAKE_TOKEN,
            file_path="templates/inventory.py", admin_ids=[str(self.owner_id)],
        )
        self._owner_patcher_mb = patch.object(manage_bots, "_is_owner", lambda uid: uid == self.owner_id)
        self._owner_patcher_mb.start()
        self._owner_patcher_fc = patch.object(feature_connect, "_is_owner", lambda uid: uid == self.owner_id)
        self._owner_patcher_fc.start()
        self._fake_registry = MagicMock()
        self._fake_registry.reload_one = AsyncMock()
        manage_bots.set_registry(self._fake_registry)

    async def asyncTearDown(self):
        manage_bots.set_registry(None)
        self._owner_patcher_fc.stop()
        self._owner_patcher_mb.stop()
        await delete_bot(self.bot_id_button)
        await delete_bot(self.bot_id_convo)

    async def test_conversational_confirm_enables_payments_identically_to_button_toggle(self):
        button_cb = _make_callback(self.owner_id, f"togglefeature:{self.bot_id_button}:payments")
        await manage_bots.cb_toggle_feature(button_cb)

        convo_cb = _make_callback(self.owner_id, f"fconn_confirm:{self.bot_id_convo}:payments")
        state = _fresh_state(self.owner_id)
        await feature_connect.cb_fconn_confirm(convo_cb, state)

        self.assertEqual(await get_bot_features(self.bot_id_button), await get_bot_features(self.bot_id_convo))
        self.assertIn("payments", await get_bot_features(self.bot_id_convo))
        self.assertEqual(self._fake_registry.reload_one.await_count, 2)
        self._fake_registry.reload_one.assert_any_await(self.bot_id_button)
        self._fake_registry.reload_one.assert_any_await(self.bot_id_convo)

    async def test_conversational_confirm_for_sheets_enters_same_fsm_state_as_button(self):
        with patch.object(manage_bots, "get_service_account_email", AsyncMock(return_value="sa@example.com")):
            state = _fresh_state(self.owner_id)
            convo_cb = _make_callback(self.owner_id, f"fconn_confirm:{self.bot_id_convo}:sheets")
            await feature_connect.cb_fconn_confirm(convo_cb, state)

        self.assertEqual(await state.get_state(), manage_bots.SheetsConnectFlow.waiting_for_link.state)
        data = await state.get_data()
        self.assertEqual(data.get("bot_id"), self.bot_id_convo)
        self.assertIn("sheets", await get_bot_features(self.bot_id_convo))


class CancelIsNoOpTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_writes_nothing_and_just_acknowledges(self):
        callback = _make_callback(1, "fconn_cancel")
        with patch.object(feature_connect, "_is_owner", lambda uid: True):
            await feature_connect.cb_fconn_cancel(callback)
        callback.answer.assert_awaited_once()
        # Edits the preview message in place (removing its "✅ Подключить"/
        # "❌ Отмена" buttons) rather than sending a new message that would
        # leave the original, still-clickable "✅ Подключить" button live.
        callback.message.edit_text.assert_awaited_once()
        callback.message.answer.assert_not_awaited()
        self.assertIn("отменено", callback.message.edit_text.call_args[0][0].lower())


# ── voice path routes identically to text ──────────────────────────────────────

class VoicePathRoutesLikeTextTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot_id = await create_bot_record_with_admins(
            name="fconn_voice_bot", description="t", token=FAKE_TOKEN,
            file_path="templates/inventory.py", admin_ids=["1"],
        )
        self.bot_row = {"id": self.bot_id, "name": "fconn_voice_bot", "username": None, "display_name": None, "file_path": "templates/inventory.py"}

    async def asyncTearDown(self):
        await delete_bot(self.bot_id)

    async def test_transcribed_voice_reaches_same_dispatch_as_text(self):
        message = MagicMock()
        message.from_user.id = 1
        message.voice.file_id = "voice123"
        message.answer = AsyncMock(return_value=MagicMock(delete=AsyncMock()))
        fake_bot = MagicMock()
        fake_bot.get_file = AsyncMock(return_value=MagicMock(file_path="voice.ogg"))
        fake_bot.download_file = AsyncMock()

        with patch.object(general, "_is_owner", lambda uid: True), \
             patch.object(general, "ASSEMBLYAI_API_KEY", "fake-key"), \
             patch.object(general, "transcribe_voice", AsyncMock(return_value="подключи таблицы к fconn_voice_bot")), \
             patch.object(feature_connect, "get_all_bots", AsyncMock(return_value=[self.bot_row])), \
             patch.object(feature_connect, "classify_connect_feature_intent",
                           AsyncMock(return_value=_intent(True, "sheets", "fconn_voice_bot"))), \
             patch.object(general, "ask_assistant", AsyncMock()) as mock_ask:
            await general.general_voice(message, fake_bot)

        mock_ask.assert_not_called()
        found_preview = any(
            "Google Таблиц" in call.args[0]
            for call in message.answer.await_args_list
            if call.args
        )
        self.assertTrue(found_preview, "voice-transcribed connect request should reach the same preview as text")
        self.assertEqual(await get_bot_features(self.bot_id), [])  # preview only, not yet confirmed


# ── authorization parity with existing owner-only surfaces ────────────────────

class AuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot_id = await create_bot_record_with_admins(
            name="fconn_auth_bot", description="t", token=FAKE_TOKEN,
            file_path="templates/inventory.py", admin_ids=["1"],
        )

    async def asyncTearDown(self):
        await delete_bot(self.bot_id)

    async def test_non_owner_text_never_reaches_classifier_or_ask_assistant(self):
        message = _make_message(999999, "подключи оплату к fconn_auth_bot")
        with patch.object(general, "_is_owner", lambda uid: False), \
             patch.object(feature_connect, "classify_connect_feature_intent", AsyncMock()) as mock_classify, \
             patch.object(general, "ask_assistant", AsyncMock()) as mock_ask:
            await general.general_text(message)
        mock_classify.assert_not_awaited()
        mock_ask.assert_not_awaited()
        message.answer.assert_not_awaited()
        self.assertEqual(await get_bot_features(self.bot_id), [])

    async def test_non_owner_confirm_callback_denied_and_writes_nothing(self):
        with patch.object(feature_connect, "_is_owner", lambda uid: False):
            callback = _make_callback(999999, f"fconn_confirm:{self.bot_id}:payments")
            state = _fresh_state(999999)
            await feature_connect.cb_fconn_confirm(callback, state)
        callback.answer.assert_awaited_once()
        callback.message.answer.assert_not_awaited()
        self.assertEqual(await get_bot_features(self.bot_id), [])

    async def test_non_owner_bot_pick_feature_pick_and_cancel_are_all_denied(self):
        with patch.object(feature_connect, "_is_owner", lambda uid: False):
            cb_bot = _make_callback(999999, f"fconn_bot:sheets:{self.bot_id}")
            await feature_connect.cb_fconn_bot(cb_bot)
            cb_feat = _make_callback(999999, f"fconn_feat:{self.bot_id}:sheets")
            await feature_connect.cb_fconn_feat(cb_feat)
            cb_cancel = _make_callback(999999, "fconn_cancel")
            await feature_connect.cb_fconn_cancel(cb_cancel)

        for cb in (cb_bot, cb_feat, cb_cancel):
            cb.answer.assert_awaited_once()
            cb.message.answer.assert_not_awaited()
        self.assertEqual(await get_bot_features(self.bot_id), [])


# ── graceful fallback on unexpected errors (review-orchestrator BLOCKER) ──────

class GracefulFallbackOnUnexpectedErrorTests(unittest.IsolatedAsyncioTestCase):
    """A DB hiccup ("database is locked") or a Telegram API error must never
    leave the owner with zero response — the same guarantee ask_assistant's
    own try/except already provides, extended to every step of the
    conversational connect flow (general.py's hook AND the DB reads inside
    the fconn_bot:/fconn_feat: callbacks)."""

    async def test_general_text_hook_falls_back_when_maybe_handle_raises(self):
        message = _make_message(1, "подключи таблицы к боту")
        with patch.object(general, "_is_owner", lambda uid: True), \
             patch.object(general, "maybe_handle_connect_request",
                           AsyncMock(side_effect=RuntimeError("database is locked"))), \
             patch.object(general, "ask_assistant", AsyncMock()) as mock_ask:
            await general.general_text(message)
        mock_ask.assert_not_called()  # don't also spend a second Claude call on the same message
        message.answer.assert_awaited_once()
        self.assertIn("что-то пошло не так", message.answer.call_args[0][0].lower())

    async def test_general_voice_hook_falls_back_when_maybe_handle_raises(self):
        message = MagicMock()
        message.from_user.id = 1
        message.voice.file_id = "voice123"
        message.answer = AsyncMock(return_value=MagicMock(delete=AsyncMock()))
        fake_bot = MagicMock()
        fake_bot.get_file = AsyncMock(return_value=MagicMock(file_path="voice.ogg"))
        fake_bot.download_file = AsyncMock()

        with patch.object(general, "_is_owner", lambda uid: True), \
             patch.object(general, "ASSEMBLYAI_API_KEY", "fake-key"), \
             patch.object(general, "transcribe_voice", AsyncMock(return_value="подключи оплату")), \
             patch.object(general, "maybe_handle_connect_request",
                           AsyncMock(side_effect=RuntimeError("database is locked"))), \
             patch.object(general, "ask_assistant", AsyncMock()) as mock_ask:
            await general.general_voice(message, fake_bot)

        mock_ask.assert_not_called()
        last_text = message.answer.await_args_list[-1].args[0]
        self.assertIn("что-то пошло не так", last_text.lower())

    async def test_maybe_handle_connect_request_degrades_when_db_read_fails(self):
        # classify succeeds and commits to "this is a connect request", but the
        # very next DB read (get_all_bots, inside _start_connect_flow) blows up.
        message = _make_message(1, "подключи таблицы к магазину")
        with patch.object(feature_connect, "get_all_bots", AsyncMock(side_effect=RuntimeError("database is locked"))), \
             patch.object(feature_connect, "classify_connect_feature_intent",
                           AsyncMock(return_value=_intent(True, "sheets", "магазину"))):
            handled = await feature_connect.maybe_handle_connect_request(message, message.text)
        # Already committed to handling this message -> must not also fire ask_assistant.
        self.assertTrue(handled)
        message.answer.assert_awaited()
        self.assertIn("что-то пошло не так", message.answer.call_args[0][0].lower())

    async def test_fconn_feat_callback_degrades_when_get_bot_raises(self):
        callback = _make_callback(1, "fconn_feat:12345:sheets")
        with patch.object(feature_connect, "_is_owner", lambda uid: True), \
             patch.object(feature_connect, "get_bot", AsyncMock(side_effect=RuntimeError("database is locked"))):
            await feature_connect.cb_fconn_feat(callback)
        callback.message.answer.assert_awaited_once()
        self.assertIn("что-то пошло не так", callback.message.answer.call_args[0][0].lower())

    async def test_fconn_bot_callback_degrades_when_get_bot_raises(self):
        callback = _make_callback(1, "fconn_bot:sheets:12345")
        with patch.object(feature_connect, "_is_owner", lambda uid: True), \
             patch.object(feature_connect, "get_bot", AsyncMock(side_effect=RuntimeError("database is locked"))):
            await feature_connect.cb_fconn_bot(callback)
        callback.message.answer.assert_awaited_once()
        self.assertIn("что-то пошло не так", callback.message.answer.call_args[0][0].lower())


# ── busy-guard against rapid double-taps (monkey-tester SHOULD-FIX) ───────────

class BusyGuardOnIntermediateCallbacksTests(unittest.IsolatedAsyncioTestCase):
    async def test_fconn_bot_rejects_reentrant_tap_for_the_same_bot(self):
        bot_id = 424299
        manage_bots._busy_bots.add(bot_id)
        try:
            callback = _make_callback(1, f"fconn_bot:sheets:{bot_id}")
            with patch.object(feature_connect, "_is_owner", lambda uid: True):
                await feature_connect.cb_fconn_bot(callback)
            callback.answer.assert_awaited_once_with(manage_bots._BUSY_TEXT, show_alert=True)
            callback.message.answer.assert_not_awaited()
        finally:
            manage_bots._busy_bots.discard(bot_id)

    async def test_fconn_feat_rejects_reentrant_tap_for_the_same_bot(self):
        bot_id = 424300
        manage_bots._busy_bots.add(bot_id)
        try:
            callback = _make_callback(1, f"fconn_feat:{bot_id}:sheets")
            with patch.object(feature_connect, "_is_owner", lambda uid: True):
                await feature_connect.cb_fconn_feat(callback)
            callback.answer.assert_awaited_once_with(manage_bots._BUSY_TEXT, show_alert=True)
            callback.message.answer.assert_not_awaited()
        finally:
            manage_bots._busy_bots.discard(bot_id)

    async def test_fconn_bot_releases_the_lock_after_handling(self):
        bot_id = 424301
        self.assertNotIn(bot_id, manage_bots._busy_bots)
        callback = _make_callback(1, f"fconn_bot:sheets:{bot_id}")
        with patch.object(feature_connect, "_is_owner", lambda uid: True), \
             patch.object(feature_connect, "get_bot", AsyncMock(return_value=None)):
            await feature_connect.cb_fconn_bot(callback)
        self.assertNotIn(bot_id, manage_bots._busy_bots)  # released, not leaked


# ── classifier spam dedup/cooldown (monkey-tester + задача 4 cost) ────────────

class ClassifierSpamDedupTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeating_the_same_text_does_not_reclassify_within_cooldown(self):
        feature_connect._last_classify_call.clear()
        text = "подключи таблицы к магазину"
        with patch.object(feature_connect, "get_all_bots", AsyncMock(return_value=[])), \
             patch.object(feature_connect, "classify_connect_feature_intent",
                           AsyncMock(return_value=_intent(True, "sheets", "магазину"))) as mock_classify:
            for _ in range(50):
                await feature_connect.maybe_handle_connect_request(_make_message(555, text), text)
        mock_classify.assert_awaited_once()  # 50 identical messages -> 1 Claude call, not 50

    async def test_different_text_is_never_suppressed(self):
        feature_connect._last_classify_call.clear()
        with patch.object(feature_connect, "get_all_bots", AsyncMock(return_value=[])), \
             patch.object(feature_connect, "classify_connect_feature_intent",
                           AsyncMock(return_value=_intent(False))) as mock_classify:
            await feature_connect.maybe_handle_connect_request(_make_message(556, "подключи таблицы к а"), "подключи таблицы к а")
            await feature_connect.maybe_handle_connect_request(_make_message(556, "подключи таблицы к б"), "подключи таблицы к б")
        self.assertEqual(mock_classify.await_count, 2)

    async def test_different_users_are_never_cross_suppressed(self):
        feature_connect._last_classify_call.clear()
        text = "подключи оплату к боту"
        with patch.object(feature_connect, "get_all_bots", AsyncMock(return_value=[])), \
             patch.object(feature_connect, "classify_connect_feature_intent",
                           AsyncMock(return_value=_intent(False))) as mock_classify:
            await feature_connect.maybe_handle_connect_request(_make_message(557, text), text)
            await feature_connect.maybe_handle_connect_request(_make_message(558, text), text)
        self.assertEqual(mock_classify.await_count, 2)

    async def test_cooldown_expires_and_allows_reclassification(self):
        feature_connect._last_classify_call.clear()
        text = "подключи таблицы к магазину"
        with patch.object(feature_connect, "get_all_bots", AsyncMock(return_value=[])), \
             patch.object(feature_connect, "classify_connect_feature_intent",
                           AsyncMock(return_value=_intent(False))) as mock_classify:
            await feature_connect.maybe_handle_connect_request(_make_message(559, text), text)
            # Simulate the cooldown window having elapsed.
            feature_connect._last_classify_call[559] = (
                feature_connect._last_classify_call[559][0],
                feature_connect._last_classify_call[559][1] - feature_connect._CLASSIFY_COOLDOWN_SECONDS - 1,
            )
            await feature_connect.maybe_handle_connect_request(_make_message(559, text), text)
        self.assertEqual(mock_classify.await_count, 2)


if __name__ == "__main__":
    unittest.main()
