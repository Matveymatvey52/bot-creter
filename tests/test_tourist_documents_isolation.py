"""tourist_documents template — data isolation, privacy, and button-UI tests.

Standard criterion (same as every other template's isolation test): two bots on
the SAME template, different config, must never mix data — even driven by the
SAME Telegram user_id.

PLUS two checks specific to this template's own design requirements:
- privacy: passport_number, exact expiry dates, and visa_status detail must
  NEVER appear in the tourist list/card view or in the expiry-reminder
  notification — only reachable via the explicit "🗂 Открыть документ" action,
  which must also write a document_view_log row (who/when) before rendering.
- button UI: every admin action must be reachable through inline/reply
  buttons + FSM dialogs, with no raw "/command arg" backdoor — this template
  deliberately has NO argument-taking slash commands at all (unlike some
  earlier templates that kept one for backward compatibility).

No real Telegram network calls, no real tokens.

Run with: python -m unittest tests.test_tourist_documents_isolation
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from runtime.registry import get_template_router
from templates import tourist_documents as td

FAKE_TOKEN = "123456:test-token-not-real"
ADMIN_ID = 111
OTHER_ADMIN_ID = 222
NON_ADMIN_ID = 333


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


def _build_bot_dispatcher(config: td.TouristDocumentsConfig) -> tuple[Bot, Dispatcher]:
    bot = Bot(token=FAKE_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(td.ConfigMiddleware(config))
    dp.include_router(get_template_router("tourist_documents"))
    return bot, dp


async def _add_tourist_via_buttons(
    dp: Dispatcher, bot: Bot, user_id: int, start_update_id: int, *,
    full_name: str, passport_number: str, passport_expiry_ru: str,
    visa_status: str = "not_required", visa_expiry_ru: str | None = None,
    group_name: str = "Египет Август 2026", notes: str | None = None,
) -> int:
    """Drives the FULL add-tourist FSM flow purely via reply-keyboard text
    triggers and inline-button callbacks (plus free-text VALUE entry, which is
    the FSM-prompted "пришлите значение" step, not a slash command) — exactly
    as a real admin would. Returns the next free update_id."""
    uid = start_update_id
    await dp.feed_webhook_update(bot, _text_update(uid, user_id, "➕ Добавить туриста")); uid += 1
    await dp.feed_webhook_update(bot, _text_update(uid, user_id, full_name)); uid += 1
    await dp.feed_webhook_update(bot, _text_update(uid, user_id, passport_number)); uid += 1
    await dp.feed_webhook_update(bot, _text_update(uid, user_id, passport_expiry_ru)); uid += 1
    await dp.feed_webhook_update(bot, _callback_update(uid, user_id, f"td_visa:{visa_status}")); uid += 1
    if visa_status == "ready":
        await dp.feed_webhook_update(bot, _text_update(uid, user_id, visa_expiry_ru)); uid += 1
    await dp.feed_webhook_update(bot, _callback_update(uid, user_id, "td_group_new")); uid += 1
    await dp.feed_webhook_update(bot, _text_update(uid, user_id, group_name)); uid += 1
    if notes:
        await dp.feed_webhook_update(bot, _text_update(uid, user_id, notes)); uid += 1
    else:
        await dp.feed_webhook_update(bot, _callback_update(uid, user_id, "td_notes_skip")); uid += 1
    await dp.feed_webhook_update(bot, _callback_update(uid, user_id, "td_add_confirm")); uid += 1
    return uid


class TouristDocumentsIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()

        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

        self.config_a = td.config_from_bot_row(
            {"bot_id": 301, "name": "td_isolation_bot_a", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        self.config_b = td.config_from_bot_row(
            {"bot_id": 302, "name": "td_isolation_bot_b", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await td.init_db(self.config_a.db_path)
        await td.init_db(self.config_b.db_path)

        self.bot_a, self.dp_a = _build_bot_dispatcher(self.config_a)
        self.bot_b, self.dp_b = _build_bot_dispatcher(self.config_b)

        # Bootstrap the same Telegram user as admin on both bots independently.
        await self.dp_a.feed_webhook_update(self.bot_a, _text_update(1, ADMIN_ID, "/start"))
        await self.dp_b.feed_webhook_update(self.bot_b, _text_update(1, ADMIN_ID, "/start"))

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_configs_point_to_different_files(self):
        self.assertNotEqual(self.config_a.db_path, self.config_b.db_path)
        self.assertNotEqual(self.config_a.admins_file, self.config_b.admins_file)

    async def test_two_bots_same_admin_add_into_separate_db_files(self):
        await _add_tourist_via_buttons(
            self.dp_a, self.bot_a, ADMIN_ID, 100,
            full_name="Иванов Иван Иванович", passport_number="A-BOT-001",
            passport_expiry_ru="25.07.2030",
        )
        await _add_tourist_via_buttons(
            self.dp_b, self.bot_b, ADMIN_ID, 100,
            full_name="Петров Пётр Петрович", passport_number="B-BOT-002",
            passport_expiry_ru="25.07.2031",
        )

        conn_a = sqlite3.connect(self.config_a.db_path)
        rows_a = conn_a.execute("SELECT full_name, passport_number FROM tourists").fetchall()
        conn_a.close()
        conn_b = sqlite3.connect(self.config_b.db_path)
        rows_b = conn_b.execute("SELECT full_name, passport_number FROM tourists").fetchall()
        conn_b.close()

        self.assertEqual(rows_a, [("Иванов Иван Иванович", "A-BOT-001")])
        self.assertEqual(rows_b, [("Петров Пётр Петрович", "B-BOT-002")])

    async def test_same_name_different_bot_id_still_isolated(self):
        config_c = td.config_from_bot_row(
            {"bot_id": 401, "name": "duplicate_name", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        config_d = td.config_from_bot_row(
            {"bot_id": 402, "name": "duplicate_name", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        self.assertEqual(config_c.bot_name, config_d.bot_name)
        self.assertNotEqual(config_c.db_path, config_d.db_path)

        await td.init_db(config_c.db_path)
        await td.init_db(config_d.db_path)
        bot_c, dp_c = _build_bot_dispatcher(config_c)
        bot_d, dp_d = _build_bot_dispatcher(config_d)
        await dp_c.feed_webhook_update(bot_c, _text_update(1, ADMIN_ID, "/start"))
        await dp_d.feed_webhook_update(bot_d, _text_update(1, ADMIN_ID, "/start"))

        await _add_tourist_via_buttons(
            dp_c, bot_c, ADMIN_ID, 100,
            full_name="Турист C", passport_number="PASS-C", passport_expiry_ru="01.01.2030",
        )
        await _add_tourist_via_buttons(
            dp_d, bot_d, ADMIN_ID, 100,
            full_name="Турист D", passport_number="PASS-D", passport_expiry_ru="01.01.2030",
        )

        conn_c = sqlite3.connect(config_c.db_path)
        names_c = [r[0] for r in conn_c.execute("SELECT full_name FROM tourists").fetchall()]
        conn_c.close()
        conn_d = sqlite3.connect(config_d.db_path)
        names_d = [r[0] for r in conn_d.execute("SELECT full_name FROM tourists").fetchall()]
        conn_d.close()

        self.assertEqual(names_c, ["Турист C"])
        self.assertEqual(names_d, ["Турист D"])

    async def test_admin_bootstrap_isolated_per_bot(self):
        admins_a = json.loads(self.config_a.admins_file.read_text())["ids"]
        admins_b = json.loads(self.config_b.admins_file.read_text())["ids"]
        self.assertEqual(admins_a, [str(ADMIN_ID)])
        self.assertEqual(admins_b, [str(ADMIN_ID)])

        # Adding an admin on bot A must not leak into bot B's admins_file.
        await self.dp_a.feed_webhook_update(self.bot_a, _text_update(200, ADMIN_ID, "👥 Админы"))
        await self.dp_a.feed_webhook_update(self.bot_a, _callback_update(201, ADMIN_ID, "td_admin_add"))
        await self.dp_a.feed_webhook_update(self.bot_a, _text_update(202, ADMIN_ID, str(OTHER_ADMIN_ID)))

        admins_a_after = json.loads(self.config_a.admins_file.read_text())["ids"]
        admins_b_after = json.loads(self.config_b.admins_file.read_text())["ids"]
        self.assertIn(str(OTHER_ADMIN_ID), admins_a_after)
        self.assertNotIn(str(OTHER_ADMIN_ID), admins_b_after)


class TouristDocumentsPrivacyTests(unittest.IsolatedAsyncioTestCase):
    """Passport number / exact dates / visa status detail must never appear
    outside the explicit, logged "🗂 Открыть документ" action."""

    async def asyncSetUp(self):
        self._bot_call = AsyncMock(return_value=MagicMock())
        self._bot_call_patcher = patch.object(Bot, "__call__", new=self._bot_call)
        self._bot_call_patcher.start()

        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = td.config_from_bot_row(
            {"bot_id": 501, "name": "td_privacy_bot", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await td.init_db(self.config.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)
        await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/start"))

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    def _all_texts(self) -> list[str]:
        texts = []
        for call in self._bot_call.call_args_list:
            request = call.args[0] if call.args else None
            text = getattr(request, "text", None)
            if text:
                texts.append(text)
        return texts

    async def test_passport_number_never_appears_outside_open_document(self):
        secret_passport = "SECRET-PASSPORT-998877"
        next_uid = await _add_tourist_via_buttons(
            self.dp, self.bot, ADMIN_ID, 10,
            full_name="Сидоров Сидор Сидорович", passport_number=secret_passport,
            passport_expiry_ru="25.07.2030",
        )
        # The add-confirmation screen (shown ONLY to the admin who just typed
        # it, to confirm their own input) legitimately contains it — clear the
        # call log before browsing the list/card, which must NOT.
        self._bot_call.reset_mock()

        conn = sqlite3.connect(self.config.db_path)
        tourist_id, group_id = conn.execute(
            "SELECT t.id, t.tour_group_id FROM tourists t WHERE t.full_name=?", ("Сидоров Сидор Сидорович",)
        ).fetchall()[0]
        conn.close()

        uid = next_uid
        await self.dp.feed_webhook_update(self.bot, _text_update(uid, ADMIN_ID, "📋 Список туристов")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, ADMIN_ID, f"td_list_group:{group_id}")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, ADMIN_ID, f"td_tourist:{tourist_id}")); uid += 1

        card_texts = self._all_texts()
        self.assertTrue(any(card_texts), "no message rendered for the tourist list/card — test setup broken")
        self.assertFalse(
            any(secret_passport in t for t in card_texts),
            "passport number leaked into the list/card view",
        )
        self.assertTrue(
            any("Сидоров" in t for t in card_texts),
            "tourist name never appeared anywhere — card rendering itself is broken",
        )

        # NOW open the document explicitly — passport number must appear here.
        self._bot_call.reset_mock()
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, ADMIN_ID, f"td_open_doc:{tourist_id}")); uid += 1
        doc_texts = self._all_texts()
        self.assertTrue(any(secret_passport in t for t in doc_texts), "open-document action did not reveal the passport number")

        conn = sqlite3.connect(self.config.db_path)
        log_rows = conn.execute(
            "SELECT tourist_id, admin_id FROM document_view_log"
        ).fetchall()
        conn.close()
        self.assertEqual(log_rows, [(tourist_id, ADMIN_ID)], "document_view_log did not record the viewer")

    async def test_reminder_notification_excludes_passport_number_and_exact_date(self):
        secret_passport = "REMINDER-SECRET-555"
        near_expiry = (date.today() + timedelta(days=5)).strftime("%d.%m.%Y")
        await _add_tourist_via_buttons(
            self.dp, self.bot, ADMIN_ID, 10,
            full_name="Кузнецова Анна", passport_number=secret_passport,
            passport_expiry_ru=near_expiry,
        )
        self._bot_call.reset_mock()

        await td._run_expiry_check(self.bot, self.config)
        reminder_texts = self._all_texts()
        self.assertTrue(any(reminder_texts), "no reminder was sent at all — test setup broken")
        self.assertFalse(any(secret_passport in t for t in reminder_texts), "passport number leaked into the reminder")
        near_expiry_iso = (date.today() + timedelta(days=5)).isoformat()
        self.assertFalse(
            any(near_expiry_iso in t or near_expiry in t for t in reminder_texts),
            "exact expiry date leaked into the reminder",
        )
        self.assertTrue(any("Кузнецова" in t for t in reminder_texts), "reminder never identified the tourist by name")

    async def test_reminder_not_sent_twice_and_not_sent_when_far_in_future(self):
        far_expiry = (date.today() + timedelta(days=400)).strftime("%d.%m.%Y")
        await _add_tourist_via_buttons(
            self.dp, self.bot, ADMIN_ID, 10,
            full_name="Далёкий Турист", passport_number="FAR-001",
            passport_expiry_ru=far_expiry,
        )
        self._bot_call.reset_mock()
        await td._run_expiry_check(self.bot, self.config)
        self.assertEqual(self._all_texts(), [], "reminder fired for a passport expiring far in the future")

        near_expiry = (date.today() + timedelta(days=5)).strftime("%d.%m.%Y")
        await _add_tourist_via_buttons(
            self.dp, self.bot, ADMIN_ID, 200,
            full_name="Скорый Турист", passport_number="NEAR-001",
            passport_expiry_ru=near_expiry,
        )
        self._bot_call.reset_mock()
        await td._run_expiry_check(self.bot, self.config)
        first_pass_count = len(self._all_texts())
        self.assertGreater(first_pass_count, 0)

        self._bot_call.reset_mock()
        await td._run_expiry_check(self.bot, self.config)
        self.assertEqual(self._all_texts(), [], "reminder fired a second time for the same unedited tourist")


class TouristDocumentsButtonUiTests(unittest.IsolatedAsyncioTestCase):
    """No raw text command with an argument should be the primary (or even a
    working) path — every admin action goes through a button + FSM dialog."""

    async def asyncSetUp(self):
        self._bot_call = AsyncMock(return_value=MagicMock())
        self._bot_call_patcher = patch.object(Bot, "__call__", new=self._bot_call)
        self._bot_call_patcher.start()

        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = td.config_from_bot_row(
            {"bot_id": 701, "name": "td_button_bot", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await td.init_db(self.config.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)
        await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/start"))

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_full_add_tourist_flow_via_buttons_persists_all_fields(self):
        await _add_tourist_via_buttons(
            self.dp, self.bot, ADMIN_ID, 10,
            full_name="Морозова Мария", passport_number="RU-4004-778899",
            passport_expiry_ru="12.03.2029", visa_status="ready", visa_expiry_ru="01.09.2027",
            group_name="Италия Сентябрь 2026", notes="VIP клиент",
        )
        conn = sqlite3.connect(self.config.db_path)
        row = conn.execute(
            "SELECT full_name, passport_number, passport_expiry, visa_status, visa_expiry, notes "
            "FROM tourists"
        ).fetchall()
        group = conn.execute("SELECT name FROM tour_groups").fetchall()
        conn.close()
        self.assertEqual(
            row,
            [("Морозова Мария", "RU-4004-778899", "2029-03-12", "ready", "2027-09-01", "VIP клиент")],
        )
        self.assertEqual(group, [("Италия Сентябрь 2026",)])

    async def test_no_raw_slash_command_with_argument_exists_for_admin_management(self):
        """This template deliberately has ZERO argument-taking slash commands
        (unlike moderator.py, which kept /addstopword etc. for backward
        compat) — /addadmin <id> must simply do nothing (no handler claims
        it), proving the button+FSM dialog is the ONLY way in."""
        await self.dp.feed_webhook_update(self.bot, _text_update(50, ADMIN_ID, f"/addadmin {OTHER_ADMIN_ID}"))
        admins = json.loads(self.config.admins_file.read_text())["ids"]
        self.assertNotIn(str(OTHER_ADMIN_ID), admins)

        await self.dp.feed_webhook_update(self.bot, _text_update(51, ADMIN_ID, "/adddoctor Should Not Exist"))
        # Must not raise, must not create any tourist/group as a side effect.
        conn = sqlite3.connect(self.config.db_path)
        counts = conn.execute("SELECT COUNT(*) FROM tourists").fetchone()[0]
        conn.close()
        self.assertEqual(counts, 0)

    async def test_admin_add_and_remove_via_buttons_only(self):
        await self.dp.feed_webhook_update(self.bot, _text_update(50, ADMIN_ID, "👥 Админы"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(51, ADMIN_ID, "td_admin_add"))
        await self.dp.feed_webhook_update(self.bot, _text_update(52, ADMIN_ID, str(OTHER_ADMIN_ID)))
        admins = json.loads(self.config.admins_file.read_text())["ids"]
        self.assertIn(str(OTHER_ADMIN_ID), admins)

        await self.dp.feed_webhook_update(self.bot, _callback_update(53, ADMIN_ID, "td_admin_remove"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(54, ADMIN_ID, f"td_admin_rm:{OTHER_ADMIN_ID}"))
        admins_after = json.loads(self.config.admins_file.read_text())["ids"]
        self.assertNotIn(str(OTHER_ADMIN_ID), admins_after)

    async def test_non_admin_gets_blocked_and_cannot_start_any_flow(self):
        await self.dp.feed_webhook_update(self.bot, _text_update(50, NON_ADMIN_ID, "/start"))
        await self.dp.feed_webhook_update(self.bot, _text_update(51, NON_ADMIN_ID, "➕ Добавить туриста"))
        # No tourist must have been created, and no FSM state started for
        # this user (verified indirectly: sending a free-text follow-up does
        # nothing harmful and no tourist appears).
        await self.dp.feed_webhook_update(self.bot, _text_update(52, NON_ADMIN_ID, "Some Name"))
        conn = sqlite3.connect(self.config.db_path)
        count = conn.execute("SELECT COUNT(*) FROM tourists").fetchone()[0]
        conn.close()
        self.assertEqual(count, 0)

    async def test_cancel_works_mid_add_tourist_flow(self):
        await self.dp.feed_webhook_update(self.bot, _text_update(10, ADMIN_ID, "➕ Добавить туриста"))
        await self.dp.feed_webhook_update(self.bot, _text_update(11, ADMIN_ID, "Некий Турист"))
        await self.dp.feed_webhook_update(self.bot, _text_update(12, ADMIN_ID, "/cancel"))

        from aiogram.fsm.storage.base import StorageKey
        from aiogram.fsm.context import FSMContext
        key = StorageKey(bot_id=self.bot.id, chat_id=ADMIN_ID, user_id=ADMIN_ID)
        state = FSMContext(storage=self.dp.storage, key=key)
        self.assertIsNone(await state.get_state())

        conn = sqlite3.connect(self.config.db_path)
        count = conn.execute("SELECT COUNT(*) FROM tourists").fetchone()[0]
        conn.close()
        self.assertEqual(count, 0)

    async def test_editing_expiry_date_clears_notified_flag(self):
        near_expiry = (date.today() + timedelta(days=5)).strftime("%d.%m.%Y")
        await _add_tourist_via_buttons(
            self.dp, self.bot, ADMIN_ID, 10,
            full_name="Обновляемый Турист", passport_number="RENEW-001",
            passport_expiry_ru=near_expiry,
        )
        await td._run_expiry_check(self.bot, self.config)
        conn = sqlite3.connect(self.config.db_path)
        tourist_id, notified_before = conn.execute(
            "SELECT id, passport_notified_at FROM tourists WHERE full_name=?", ("Обновляемый Турист",)
        ).fetchall()[0]
        conn.close()
        self.assertIsNotNone(notified_before)

        new_expiry_ru = (date.today() + timedelta(days=800)).strftime("%d.%m.%Y")
        await self.dp.feed_webhook_update(self.bot, _callback_update(100, ADMIN_ID, f"td_edit:{tourist_id}"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(101, ADMIN_ID, f"td_edit_f:{tourist_id}:passport_expiry"))
        await self.dp.feed_webhook_update(self.bot, _text_update(102, ADMIN_ID, new_expiry_ru))

        conn = sqlite3.connect(self.config.db_path)
        notified_after = conn.execute(
            "SELECT passport_notified_at FROM tourists WHERE id=?", (tourist_id,)
        ).fetchone()[0]
        conn.close()
        self.assertIsNone(notified_after, "editing the expiry date did not clear the notified flag")

    async def test_cancelling_field_edit_does_not_leave_stale_state_that_swallows_next_message(self):
        """Regression for a review-found blocker: edit_field_start/edit_visa_set
        point their "❌ Отмена" button at td_edit:{id} (the edit menu), not the
        generic td_cancel — edit_menu must clear FSM state itself, or the admin
        stays parked in EditTourist.value and their NEXT unrelated text message
        gets silently written into the tourist's record."""
        await _add_tourist_via_buttons(
            self.dp, self.bot, ADMIN_ID, 10,
            full_name="Исходное Имя", passport_number="CANCEL-001",
            passport_expiry_ru="01.01.2030",
        )
        conn = sqlite3.connect(self.config.db_path)
        tourist_id = conn.execute("SELECT id FROM tourists WHERE full_name=?", ("Исходное Имя",)).fetchone()[0]
        conn.close()

        await self.dp.feed_webhook_update(self.bot, _callback_update(100, ADMIN_ID, f"td_edit:{tourist_id}"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(101, ADMIN_ID, f"td_edit_f:{tourist_id}:full_name"))
        # Cancel via the button attached to THIS prompt (td_edit:{id}), not /cancel.
        await self.dp.feed_webhook_update(self.bot, _callback_update(102, ADMIN_ID, f"td_edit:{tourist_id}"))
        # An unrelated chat message the admin sends afterward must NOT get
        # silently written as the new full_name.
        await self.dp.feed_webhook_update(self.bot, _text_update(103, ADMIN_ID, "Просто сообщение в чат, не поле формы"))

        conn = sqlite3.connect(self.config.db_path)
        full_name = conn.execute("SELECT full_name FROM tourists WHERE id=?", (tourist_id,)).fetchone()[0]
        conn.close()
        self.assertEqual(full_name, "Исходное Имя", "cancelling the edit left stale FSM state that swallowed the next message")

        from aiogram.fsm.storage.base import StorageKey
        from aiogram.fsm.context import FSMContext
        key = StorageKey(bot_id=self.bot.id, chat_id=ADMIN_ID, user_id=ADMIN_ID)
        state = FSMContext(storage=self.dp.storage, key=key)
        self.assertIsNone(await state.get_state())

    async def test_demoted_admin_cannot_complete_add_tourist_flow(self):
        """Regression: every step of the AddTourist wizard re-checks admin
        status, not just the entry point — a user demoted mid-flow must not
        be able to complete the INSERT."""
        await self.dp.feed_webhook_update(self.bot, _text_update(10, ADMIN_ID, "➕ Добавить туриста"))
        await self.dp.feed_webhook_update(self.bot, _text_update(11, ADMIN_ID, "Демотед Турист"))
        # Demote mid-flow.
        ids = json.loads(self.config.admins_file.read_text())["ids"]
        ids.remove(str(ADMIN_ID))
        self.config.admins_file.write_text(json.dumps({"ids": ids}))

        await self.dp.feed_webhook_update(self.bot, _text_update(12, ADMIN_ID, "PASSPORT-DEMOTED"))
        conn = sqlite3.connect(self.config.db_path)
        count = conn.execute("SELECT COUNT(*) FROM tourists").fetchone()[0]
        conn.close()
        self.assertEqual(count, 0, "a demoted user was able to continue writing through the add-tourist wizard")

    async def test_menu_button_tap_mid_edit_does_not_get_written_as_field_value(self):
        """Regression: a persistent reply-keyboard button tap while mid-FSM
        (e.g. "👥 Админы" while the bot is waiting for a new ФИО) used to
        match the state's free-text filter and get silently written as the
        literal field value — now excluded via MENU_BUTTON_TEXTS."""
        await _add_tourist_via_buttons(
            self.dp, self.bot, ADMIN_ID, 10,
            full_name="Оригинал", passport_number="MENU-001",
            passport_expiry_ru="01.01.2030",
        )
        conn = sqlite3.connect(self.config.db_path)
        tourist_id = conn.execute("SELECT id FROM tourists WHERE full_name=?", ("Оригинал",)).fetchone()[0]
        conn.close()

        await self.dp.feed_webhook_update(self.bot, _callback_update(100, ADMIN_ID, f"td_edit:{tourist_id}"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(101, ADMIN_ID, f"td_edit_f:{tourist_id}:full_name"))
        await self.dp.feed_webhook_update(self.bot, _text_update(102, ADMIN_ID, "👥 Админы"))

        conn = sqlite3.connect(self.config.db_path)
        full_name = conn.execute("SELECT full_name FROM tourists WHERE id=?", (tourist_id,)).fetchone()[0]
        conn.close()
        self.assertEqual(full_name, "Оригинал", "a menu-button tap was written as the tourist's full_name")

    async def test_admin_id_length_is_bounded(self):
        """Regression: an unbounded admin-ID string used to be accepted
        verbatim, which could blow past Telegram's message-length limit the
        next time ANY admin opened the admins panel."""
        await self.dp.feed_webhook_update(self.bot, _text_update(50, ADMIN_ID, "👥 Админы"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(51, ADMIN_ID, "td_admin_add"))
        huge_id = "1" * 5000
        await self.dp.feed_webhook_update(self.bot, _text_update(52, ADMIN_ID, huge_id))
        admins = json.loads(self.config.admins_file.read_text())["ids"]
        self.assertNotIn(huge_id, admins)


class TouristDocumentsStandaloneSmokeTest(unittest.TestCase):
    """Confirms the template still imports and initializes fine outside the
    webhook runtime — the subprocess model must keep working unmodified."""

    def test_config_from_env_matches_legacy_constant_shape(self):
        config = td.config_from_env()
        self.assertTrue(config.db_path.endswith("tourist_documents_data.db"))
        self.assertTrue(str(config.admins_file).endswith("admins_tourist_documents.json"))
        self.assertEqual(config.bot_name, "tourist_documents")

    def test_router_and_main_entrypoint_exist(self):
        self.assertTrue(hasattr(td, "router"))
        self.assertTrue(hasattr(td, "main"))


if __name__ == "__main__":
    unittest.main()
