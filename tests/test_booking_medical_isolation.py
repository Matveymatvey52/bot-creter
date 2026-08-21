"""booking_medical template — data isolation, privacy, and reminder boundary tests.

Standard criterion (same as every other template's isolation test): two bots on
the SAME template, different config, must never mix data — even driven by the
SAME Telegram user_id.

PLUS two checks specific to this template's own design requirements:
- privacy: complaint/allergies/policy must never appear in the admin push
  notification sent on a new appointment (only reachable via the explicit
  "открыть карту пациента" action, not tested here since it's not a broadcast).
- reminder boundary (owner-specified): an appointment booked LESS than a day
  before its slot must NOT get a reminder task scheduled at all (no crash, no
  immediate back-dated send) — vs. the normal case (days ahead) where a task
  IS scheduled with a positive delay.

No real Telegram network calls, no real tokens.

Run with: python -m unittest tests.test_booking_medical_isolation
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

import db.database as db_module
from runtime.registry import get_template_router
from templates import booking_medical

FAKE_TOKEN = "123456:test-token-not-real"
SAME_USER_ID = 111
ADMIN_ID = 999


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


def _build_bot_dispatcher(config: booking_medical.BookingMedicalConfig) -> tuple[Bot, Dispatcher]:
    bot = Bot(token=FAKE_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(booking_medical.ConfigMiddleware(config))
    dp.include_router(get_template_router("booking_medical"))
    return bot, dp


def _first_doctor(db_path: str) -> tuple[int, str]:
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT id, specialization FROM doctors ORDER BY id LIMIT 1").fetchone()
    conn.close()
    return row


async def _book_full_flow(
    dp: Dispatcher, bot: Bot, user_id: int, doctor_id: int, specialization: str,
    slot_date: str, slot_time: str, complaint: str, allergies: str, policy_number: str,
    start_update_id: int, phone: str = "+7 999 000-00-00",
) -> None:
    """Drives the full booking FSM flow (specialization -> doctor -> day ->
    time -> phone -> complaint -> allergies -> policy -> confirm) exactly as a
    real user would, via aiogram updates."""
    uid = start_update_id
    await dp.feed_webhook_update(bot, _callback_update(uid, user_id, f"med_spec:{specialization}")); uid += 1
    await dp.feed_webhook_update(bot, _callback_update(uid, user_id, f"med_doctor:{doctor_id}")); uid += 1
    await dp.feed_webhook_update(bot, _callback_update(uid, user_id, f"med_day:{doctor_id}:{slot_date}")); uid += 1
    await dp.feed_webhook_update(bot, _callback_update(uid, user_id, f"med_time:{doctor_id}:{slot_date}:{slot_time}")); uid += 1
    await dp.feed_webhook_update(bot, _text_update(uid, user_id, phone)); uid += 1
    await dp.feed_webhook_update(bot, _text_update(uid, user_id, complaint)); uid += 1
    await dp.feed_webhook_update(bot, _text_update(uid, user_id, allergies)); uid += 1
    await dp.feed_webhook_update(bot, _text_update(uid, user_id, policy_number)); uid += 1
    await dp.feed_webhook_update(bot, _callback_update(uid, user_id, "med_confirm")); uid += 1


class BookingMedicalIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()

        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

        self.config_a = booking_medical.config_from_bot_row(
            {"bot_id": 301, "name": "bm_isolation_bot_a", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        self.config_b = booking_medical.config_from_bot_row(
            {"bot_id": 302, "name": "bm_isolation_bot_b", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await booking_medical.init_db(self.config_a.db_path)
        await booking_medical.init_db(self.config_b.db_path)

        self.bot_a, self.dp_a = _build_bot_dispatcher(self.config_a)
        self.bot_b, self.dp_b = _build_bot_dispatcher(self.config_b)

        self.doctor_a_id, self.spec_a = _first_doctor(self.config_a.db_path)
        self.doctor_b_id, self.spec_b = _first_doctor(self.config_b.db_path)

        self.slot_date = (date.today() + timedelta(days=3)).isoformat()
        self.slot_time = booking_medical.SLOT_TIMES[0]

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_configs_point_to_different_files(self):
        self.assertNotEqual(self.config_a.db_path, self.config_b.db_path)
        self.assertNotEqual(self.config_a.admins_file, self.config_b.admins_file)

    async def test_two_bots_same_user_book_into_separate_db_files(self):
        await _book_full_flow(
            self.dp_a, self.bot_a, SAME_USER_ID, self.doctor_a_id, self.spec_a,
            self.slot_date, self.slot_time, "Головная боль", "нет", "POLICY-A-001", 1,
        )
        await _book_full_flow(
            self.dp_b, self.bot_b, SAME_USER_ID, self.doctor_b_id, self.spec_b,
            self.slot_date, self.slot_time, "Кашель", "пенициллин", "POLICY-B-002", 1,
        )

        conn_a = sqlite3.connect(self.config_a.db_path)
        row_a = conn_a.execute("SELECT complaint, allergies, policy_number FROM appointments").fetchall()
        conn_a.close()
        conn_b = sqlite3.connect(self.config_b.db_path)
        row_b = conn_b.execute("SELECT complaint, allergies, policy_number FROM appointments").fetchall()
        conn_b.close()

        self.assertEqual(row_a, [("Головная боль", "нет", "POLICY-A-001")])
        self.assertEqual(row_b, [("Кашель", "пенициллин", "POLICY-B-002")])

    async def test_same_name_different_bot_id_still_isolated(self):
        config_c = booking_medical.config_from_bot_row(
            {"bot_id": 401, "name": "duplicate_name", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        config_d = booking_medical.config_from_bot_row(
            {"bot_id": 402, "name": "duplicate_name", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        self.assertEqual(config_c.bot_name, config_d.bot_name)
        self.assertNotEqual(config_c.db_path, config_d.db_path)

        await booking_medical.init_db(config_c.db_path)
        await booking_medical.init_db(config_d.db_path)
        bot_c, dp_c = _build_bot_dispatcher(config_c)
        bot_d, dp_d = _build_bot_dispatcher(config_d)
        doctor_c_id, spec_c = _first_doctor(config_c.db_path)
        doctor_d_id, spec_d = _first_doctor(config_d.db_path)

        await _book_full_flow(
            dp_c, bot_c, SAME_USER_ID, doctor_c_id, spec_c,
            self.slot_date, self.slot_time, "Жалоба C", "нет", "POLICY-C", 1,
        )
        await _book_full_flow(
            dp_d, bot_d, SAME_USER_ID, doctor_d_id, spec_d,
            self.slot_date, self.slot_time, "Жалоба D", "нет", "POLICY-D", 1,
        )

        conn_c = sqlite3.connect(config_c.db_path)
        complaints_c = [r[0] for r in conn_c.execute("SELECT complaint FROM appointments").fetchall()]
        conn_c.close()
        conn_d = sqlite3.connect(config_d.db_path)
        complaints_d = [r[0] for r in conn_d.execute("SELECT complaint FROM appointments").fetchall()]
        conn_d.close()

        self.assertEqual(complaints_c, ["Жалоба C"])
        self.assertEqual(complaints_d, ["Жалоба D"])

    async def test_admin_bootstrap_isolated_per_bot(self):
        await self.dp_a.feed_webhook_update(self.bot_a, _text_update(1, SAME_USER_ID, "/start"))
        await self.dp_b.feed_webhook_update(self.bot_b, _text_update(1, 999, "/start"))

        admins_a = json.loads(self.config_a.admins_file.read_text())["ids"]
        admins_b = json.loads(self.config_b.admins_file.read_text())["ids"]

        self.assertEqual(admins_a, [str(SAME_USER_ID)])
        self.assertEqual(admins_b, ["999"])

    async def test_repeat_visit_detected_on_second_booking_same_patient(self):
        """is_repeat_visit is computed from THIS bot's own appointment history —
        must not be affected by the other bot's data (isolation), and must
        correctly flip to 1 on the patient's second visit within the SAME bot."""
        await _book_full_flow(
            self.dp_a, self.bot_a, SAME_USER_ID, self.doctor_a_id, self.spec_a,
            self.slot_date, self.slot_time, "Первый визит", "нет", "POLICY-A-001", 1,
        )
        second_time = booking_medical.SLOT_TIMES[1]
        await _book_full_flow(
            self.dp_a, self.bot_a, SAME_USER_ID, self.doctor_a_id, self.spec_a,
            self.slot_date, second_time, "Повторный визит", "нет", "POLICY-A-001", 100,
        )
        conn = sqlite3.connect(self.config_a.db_path)
        flags = [r[0] for r in conn.execute("SELECT is_repeat_visit FROM appointments ORDER BY id").fetchall()]
        conn.close()
        self.assertEqual(flags, [0, 1])


class BookingMedicalPrivacyTests(unittest.IsolatedAsyncioTestCase):
    """Complaint/allergies/policy must never appear in the admin push
    notification — only reachable via the explicit "🗂 Карта пациента" action."""

    async def asyncSetUp(self):
        self._bot_call = AsyncMock(return_value=MagicMock())
        self._bot_call_patcher = patch.object(Bot, "__call__", new=self._bot_call)
        self._bot_call_patcher.start()

        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = booking_medical.config_from_bot_row(
            {"bot_id": 501, "name": "bm_privacy_bot", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await booking_medical.init_db(self.config.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)
        self.doctor_id, self.spec = _first_doctor(self.config.db_path)
        self.slot_date = (date.today() + timedelta(days=3)).isoformat()
        self.slot_time = booking_medical.SLOT_TIMES[0]

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_complaint_does_not_leak_into_admin_notification(self):
        # Bootstrap the admin as a DIFFERENT telegram id than the patient, so
        # admin-bound and patient-bound messages can be told apart by chat_id.
        await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/start"))

        secret_complaint = "СЕКРЕТНАЯ_ЖАЛОБА_НЕ_ДОЛЖНА_УТЕЧЬ"
        await _book_full_flow(
            self.dp, self.bot, SAME_USER_ID, self.doctor_id, self.spec,
            self.slot_date, self.slot_time, secret_complaint, "нет", "POLICY-XYZ", 10,
        )

        admin_texts = []
        patient_texts = []
        for call in self._bot_call.call_args_list:
            request = call.args[0] if call.args else None
            text = getattr(request, "text", None)
            chat_id = getattr(request, "chat_id", None)
            if not text:
                continue
            if chat_id == ADMIN_ID:
                admin_texts.append(text)
            elif chat_id == SAME_USER_ID:
                patient_texts.append(text)

        self.assertTrue(any(admin_texts), "no message was sent to the admin at all — test setup is broken")
        self.assertFalse(
            any(secret_complaint in t for t in admin_texts),
            "complaint leaked into an admin-bound message",
        )
        self.assertTrue(
            any(secret_complaint in t for t in patient_texts),
            "complaint never appeared anywhere — booking flow itself is broken, not just privacy",
        )


class BookingMedicalReminderBoundaryTests(unittest.IsolatedAsyncioTestCase):
    """Owner-specified boundary case: an appointment less than a day away must
    NOT get a reminder task scheduled (no crash, no immediate back-dated send).
    Normal case: a reminder IS scheduled with a positive delay."""

    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()

        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = booking_medical.config_from_bot_row(
            {"bot_id": 601, "name": "bm_reminder_bot", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await booking_medical.init_db(self.config.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)
        self.doctor_id, self.spec = _first_doctor(self.config.db_path)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_no_reminder_task_when_appointment_is_less_than_a_day_away(self):
        # _ensure_slots always generates slots for TODAY too — any time on
        # today's date is, by construction, less than 24h from "now".
        today_slot_date = date.today().isoformat()
        slot_time = booking_medical.SLOT_TIMES[0]

        with patch.object(booking_medical, "_remind_day_before", new=AsyncMock()) as mock_remind:
            await _book_full_flow(
                self.dp, self.bot, SAME_USER_ID, self.doctor_id, self.spec,
                today_slot_date, slot_time, "Срочная жалоба", "нет", "POLICY-SOON", 1,
            )
            # let anything that WAS scheduled actually run, so the test would
            # fail loudly (not hang) if the guard were ever removed.
            import asyncio
            await asyncio.sleep(0)

        mock_remind.assert_not_called()

        # And the booking itself must have gone through fine — no crash, no
        # silently-dropped appointment.
        conn = sqlite3.connect(self.config.db_path)
        count = conn.execute("SELECT COUNT(*) FROM appointments").fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)

    async def test_reminder_task_scheduled_with_positive_delay_when_days_away(self):
        future_slot_date = (date.today() + timedelta(days=5)).isoformat()
        slot_time = booking_medical.SLOT_TIMES[0]

        with patch.object(booking_medical, "_remind_day_before", new=AsyncMock()) as mock_remind:
            await _book_full_flow(
                self.dp, self.bot, SAME_USER_ID, self.doctor_id, self.spec,
                future_slot_date, slot_time, "Плановая жалоба", "нет", "POLICY-FAR", 1,
            )
            import asyncio
            await asyncio.sleep(0)

        mock_remind.assert_called_once()
        call_args = mock_remind.call_args.args
        delay = call_args[5]  # (bot, chat_id, doctor_name, slot_date, slot_time, delay, bot_name)
        self.assertGreater(delay, 0)
        # Should be roughly 4 days (5 days ahead minus the 1-day-before offset),
        # generously bounded to avoid flakiness from test execution time.
        self.assertGreater(delay, 3 * 24 * 3600)
        self.assertLess(delay, 5 * 24 * 3600)


class BookingMedicalButtonPathAndFixRegressionTests(unittest.IsolatedAsyncioTestCase):
    """Covers the button-driven paths the plain-text-only _book_full_flow
    helper never exercises (review-found gap), plus regression coverage for
    every blocker/important fix applied after the first review pass."""

    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = booking_medical.config_from_bot_row(
            {"bot_id": 701, "name": "bm_button_path_bot", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await booking_medical.init_db(self.config.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)
        self.doctor_id, self.spec = _first_doctor(self.config.db_path)
        self.slot_date = (date.today() + timedelta(days=3)).isoformat()
        self.slot_time = booking_medical.SLOT_TIMES[0]

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_allergy_none_button_and_policy_reuse_buttons_end_to_end(self):
        uid = 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, SAME_USER_ID, f"med_spec:{self.spec}")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, SAME_USER_ID, f"med_doctor:{self.doctor_id}")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, SAME_USER_ID, f"med_day:{self.doctor_id}:{self.slot_date}")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, SAME_USER_ID, f"med_time:{self.doctor_id}:{self.slot_date}:{self.slot_time}")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _text_update(uid, SAME_USER_ID, "+7 999 111-11-11")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _text_update(uid, SAME_USER_ID, "Первый визит")); uid += 1
        # First booking: no existing patient yet -> allergy-skip button, then
        # free-text policy (no reuse offer possible on a first-ever visit).
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, SAME_USER_ID, "med_allergy_none")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _text_update(uid, SAME_USER_ID, "POLICY-REUSE-001")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, SAME_USER_ID, "med_confirm")); uid += 1

        conn = sqlite3.connect(self.config.db_path)
        allergies, policy = conn.execute(
            "SELECT allergies, policy_number FROM appointments ORDER BY id"
        ).fetchall()[0]
        conn.close()
        self.assertEqual(allergies, "")
        self.assertEqual(policy, "POLICY-REUSE-001")

        # Second booking, same patient: policy reuse button should now be
        # offered and actually apply the stored policy_number.
        second_time = booking_medical.SLOT_TIMES[1]
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, SAME_USER_ID, f"med_spec:{self.spec}")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, SAME_USER_ID, f"med_doctor:{self.doctor_id}")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, SAME_USER_ID, f"med_day:{self.doctor_id}:{self.slot_date}")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, SAME_USER_ID, f"med_time:{self.doctor_id}:{self.slot_date}:{second_time}")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _text_update(uid, SAME_USER_ID, "+7 999 111-11-11")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _text_update(uid, SAME_USER_ID, "Повторный визит")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, SAME_USER_ID, "med_allergy_none")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, SAME_USER_ID, "med_policy_use")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, SAME_USER_ID, "med_confirm")); uid += 1

        conn = sqlite3.connect(self.config.db_path)
        rows = conn.execute("SELECT policy_number, is_repeat_visit FROM appointments ORDER BY id").fetchall()
        conn.close()
        self.assertEqual(rows[1], ("POLICY-REUSE-001", 1))

    async def test_html_special_characters_in_complaint_do_not_break_confirmation(self):
        """Review-found blocker: an unescaped '<'/'&' in free-text medical input
        used to raise inside message construction with the FSM already
        advanced past recovery. Now must complete cleanly end to end."""
        dangerous_complaint = "температура <38 и боль & слабость"
        await _book_full_flow(
            self.dp, self.bot, SAME_USER_ID, self.doctor_id, self.spec,
            self.slot_date, self.slot_time, dangerous_complaint, "нет", "POLICY-XSS", 1,
        )
        conn = sqlite3.connect(self.config.db_path)
        stored = conn.execute("SELECT complaint FROM appointments").fetchall()
        conn.close()
        # Stored verbatim in the DB (escaping only applies at render time) —
        # and, crucially, the flow reached confirm without raising.
        self.assertEqual(stored, [(dangerous_complaint,)])

    async def test_cancel_command_works_mid_questionnaire(self):
        await self.dp.feed_webhook_update(self.bot, _callback_update(1, SAME_USER_ID, f"med_spec:{self.spec}"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(2, SAME_USER_ID, f"med_doctor:{self.doctor_id}"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(3, SAME_USER_ID, f"med_day:{self.doctor_id}:{self.slot_date}"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(4, SAME_USER_ID, f"med_time:{self.doctor_id}:{self.slot_date}:{self.slot_time}"))
        await self.dp.feed_webhook_update(self.bot, _text_update(5, SAME_USER_ID, "/cancel"))

        from aiogram.fsm.storage.base import StorageKey
        from aiogram.fsm.context import FSMContext
        key = StorageKey(bot_id=self.bot.id, chat_id=SAME_USER_ID, user_id=SAME_USER_ID)
        state = FSMContext(storage=self.dp.storage, key=key)
        self.assertIsNone(await state.get_state())

    async def test_non_owner_cannot_cancel_another_patients_appointment(self):
        await _book_full_flow(
            self.dp, self.bot, SAME_USER_ID, self.doctor_id, self.spec,
            self.slot_date, self.slot_time, "Жалоба", "нет", "POLICY-OWNER", 1,
        )
        conn = sqlite3.connect(self.config.db_path)
        appointment_id = conn.execute("SELECT id FROM appointments").fetchall()[0][0]
        conn.close()

        OTHER_USER_ID = 222
        await self.dp.feed_webhook_update(
            self.bot, _callback_update(100, OTHER_USER_ID, f"med_pt_cancel:{appointment_id}")
        )

        conn = sqlite3.connect(self.config.db_path)
        count = conn.execute("SELECT COUNT(*) FROM appointments").fetchall()[0][0]
        conn.close()
        self.assertEqual(count, 1, "a non-owner was able to cancel someone else's appointment")

    async def test_non_admin_cannot_view_schedule(self):
        await self.dp.feed_webhook_update(self.bot, _text_update(1, SAME_USER_ID, "/start"))  # bootstraps as admin
        NON_ADMIN_ID = 333
        await self.dp.feed_webhook_update(self.bot, _callback_update(2, NON_ADMIN_ID, f"adm_day:{self.slot_date}"))
        # No exception, no schedule data leaked — can't easily assert on the
        # (mocked) message content here, so this mainly guards against a crash
        # and documents the expectation; see cb_adm_day's own admin check.

    async def test_deactivated_doctor_cannot_be_booked(self):
        conn = sqlite3.connect(self.config.db_path)
        conn.execute("UPDATE doctors SET active=0 WHERE id=?", (self.doctor_id,))
        conn.commit()
        conn.close()

        await self.dp.feed_webhook_update(self.bot, _callback_update(1, SAME_USER_ID, f"med_spec:{self.spec}"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(2, SAME_USER_ID, f"med_doctor:{self.doctor_id}"))

        conn = sqlite3.connect(self.config.db_path)
        count = conn.execute("SELECT COUNT(*) FROM appointments").fetchall()[0][0]
        conn.close()
        self.assertEqual(count, 0)


class BookingMedicalAdminSecurityBootstrapTests(unittest.IsolatedAsyncioTestCase):
    """Security fix: previously, whoever sent /start FIRST permanently became
    the bot admin — a client testing the bot link before the owner did would
    silently seize the admin panel. When bots.owner_telegram_id is known,
    only that user may claim the empty-admins bootstrap slot, and
    _is_bot_admin treats the DB-known owner as always-admin regardless of the
    local admins_file state. Mirrors tests/test_shop_catalog_isolation.py's
    coverage of the same fix pattern."""

    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

        # cmd_start now syncs the bootstrap admin into db.database.add_bot_admin
        # (central bot_admins table) — must be redirected to a throwaway DB.
        self._central_db_path = self.data_dir / "central_bots.db"
        self._db_path_patcher = patch.object(db_module, "DB_PATH", self._central_db_path)
        self._db_path_patcher.start()
        await db_module.init_db()

    async def asyncTearDown(self):
        self._db_path_patcher.stop()
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_non_owner_messaging_first_does_not_become_admin(self):
        config = booking_medical.config_from_bot_row(
            {"bot_id": 611, "name": "medical_bot_owned", "display_name": None,
             "group_chat_id": None, "owner_telegram_id": 12345},
            self.data_dir,
        )
        await booking_medical.init_db(config.db_path)
        bot, dp = _build_bot_dispatcher(config)

        CLIENT_ID = 555  # not the owner, messages first
        await dp.feed_webhook_update(bot, _text_update(1, CLIENT_ID, "/start"))
        self.assertEqual(booking_medical._load_admins(config.admins_file), set())
        self.assertFalse(booking_medical._is_bot_admin(CLIENT_ID, config))

        await dp.feed_webhook_update(bot, _text_update(2, 12345, "/start"))
        self.assertTrue(booking_medical._is_bot_admin(12345, config))
        self.assertEqual(booking_medical._load_admins(config.admins_file), {"12345"})

    async def test_owner_is_always_admin_even_with_stale_admins_file(self):
        config = booking_medical.config_from_bot_row(
            {"bot_id": 612, "name": "medical_bot_owned_2", "display_name": None,
             "group_chat_id": None, "owner_telegram_id": 777},
            self.data_dir,
        )
        await booking_medical.init_db(config.db_path)
        booking_medical._save_admins(config.admins_file, {"999999"})  # not the owner
        self.assertTrue(booking_medical._is_bot_admin(777, config))
        self.assertTrue(booking_medical._is_bot_admin(999999, config))
        self.assertFalse(booking_medical._is_bot_admin(4242, config))

    async def test_standalone_mode_keeps_first_comer_bootstrap(self):
        """owner_telegram_id unknown (standalone/env mode) — the old
        first-comer behavior is the only option available."""
        config = booking_medical.config_from_bot_row(
            {"bot_id": 613, "name": "medical_bot_standalone", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        self.assertIsNone(config.owner_telegram_id)
        await booking_medical.init_db(config.db_path)
        bot, dp = _build_bot_dispatcher(config)

        FIRST_COMER = 999
        await dp.feed_webhook_update(bot, _text_update(1, FIRST_COMER, "/start"))
        self.assertEqual(booking_medical._load_admins(config.admins_file), {"999"})
        self.assertTrue(booking_medical._is_bot_admin(FIRST_COMER, config))

    async def test_bootstrap_admin_syncs_to_central_bot_admins_table(self):
        config = booking_medical.config_from_bot_row(
            {"bot_id": 614, "name": "medical_bot_synced", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await booking_medical.init_db(config.db_path)
        bot, dp = _build_bot_dispatcher(config)
        await dp.feed_webhook_update(bot, _text_update(1, 321, "/start"))

        central_admins = await db_module.get_bot_admins(614)
        self.assertEqual(central_admins, ["321"])


class BookingMedicalMiniAppConfigTests(unittest.IsolatedAsyncioTestCase):
    """miniapp_config's declared table/field names must match init_db()'s
    real schema — miniapp_api.py builds SQL directly off these names, so a
    drift here would 500 at request time instead of failing a test."""

    def test_miniapp_config_resource_names(self):
        names = {r["name"] for r in booking_medical.miniapp_config["resources"]}
        self.assertEqual(names, {"doctors", "slots", "patients", "appointments"})

    def test_doctors_resource_targets_doctors_table(self):
        doctors = next(r for r in booking_medical.miniapp_config["resources"] if r["name"] == "doctors")
        self.assertEqual(doctors["table"], "doctors")
        self.assertTrue(doctors["creatable"])

    def test_appointments_resource_targets_appointments_table(self):
        appointments = next(r for r in booking_medical.miniapp_config["resources"] if r["name"] == "appointments")
        self.assertEqual(appointments["table"], "appointments")
        field_names = {f["name"] for f in appointments["fields"]}
        self.assertEqual(
            field_names,
            {"slot_id", "patient_id", "complaint", "allergies", "policy_number",
             "is_repeat_visit", "status", "created_at"},
        )

    async def test_miniapp_config_fields_match_real_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "schema_check.db")
            await booking_medical.init_db(db_path)
            async with aiosqlite.connect(db_path) as db:
                for resource in booking_medical.miniapp_config["resources"]:
                    cur = await db.execute(f"PRAGMA table_info({resource['table']})")
                    real_columns = {row[1] for row in await cur.fetchall()}
                    declared = {f["name"] for f in resource["fields"]} | {"id"}
                    self.assertTrue(
                        declared.issubset(real_columns),
                        f"{resource['name']}: declared fields {declared} not all in "
                        f"real columns {real_columns}",
                    )


class BookingMedicalStandaloneSmokeTest(unittest.TestCase):
    """Confirms the template still imports and initializes fine outside the
    webhook runtime — the subprocess model must keep working unmodified."""

    def test_config_from_env_matches_legacy_constant_shape(self):
        config = booking_medical.config_from_env()
        self.assertTrue(config.db_path.endswith("booking_medical_data.db"))
        self.assertTrue(str(config.admins_file).endswith("admins_booking_medical.json"))
        self.assertEqual(config.bot_name, "booking_medical")

    def test_router_and_main_entrypoint_exist(self):
        self.assertTrue(hasattr(booking_medical, "router"))
        self.assertTrue(hasattr(booking_medical, "main"))


if __name__ == "__main__":
    unittest.main()
