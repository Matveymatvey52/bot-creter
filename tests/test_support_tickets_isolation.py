"""support_tickets template — data isolation, KB-search-gated ticket creation,
admin-reply/close-notify, escalation, and double-tap safety tests.

Standard criterion: two bots on the SAME template, different config, must
never mix data — even driven by the SAME Telegram admin user_id.

PLUS the design's central differentiators from vehicle_service.py:
- a ticket-creation flow that searches the knowledge base BEFORE creating a
  ticket, only inserting a row if the client says the KB didn't help (or if
  there were no matches at all);
- a client is notified automatically when their ticket is closed (with a
  satisfaction-rating request) and when an admin sends a reply — same
  double-tap-safe / compare-and-swap mechanics as vehicle_service.py;
- an explicit client-triggered escalation button that changes ticket status
  AND notifies admins with an urgency marker.

No real Telegram network calls, no real tokens.

Run with: python -m unittest tests.test_support_tickets_isolation
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

from runtime.registry import get_template_router
from templates import support_tickets

FAKE_TOKEN = "123456:test-token-not-real"
ADMIN_ID = 999
CLIENT_ID = 555
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


def _build_bot_dispatcher(config: support_tickets.SupportTicketsConfig) -> tuple[Bot, Dispatcher]:
    bot = Bot(token=FAKE_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(support_tickets.ConfigMiddleware(config))
    dp.include_router(get_template_router("support_tickets"))
    return bot, dp


async def _create_ticket_no_kb_match(
    dp: Dispatcher, bot: Bot, client_id: int, category: str, description: str, start_update_id: int,
) -> int:
    """Drives the client FSM through: tkt_new -> pick category -> description.
    With an empty kb_articles table there are never any matches, so this
    short-circuits straight to ticket creation."""
    uid = start_update_id
    await dp.feed_webhook_update(bot, _callback_update(uid, client_id, "tkt_new")); uid += 1
    await dp.feed_webhook_update(bot, _callback_update(uid, client_id, f"tkt_cat:{category}")); uid += 1
    await dp.feed_webhook_update(bot, _text_update(uid, client_id, description)); uid += 1
    return uid


class SupportTicketsIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

        self.config_a = support_tickets.config_from_bot_row(
            {"bot_id": 1001, "name": "support_isolation_bot_a", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        self.config_b = support_tickets.config_from_bot_row(
            {"bot_id": 1002, "name": "support_isolation_bot_b", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await support_tickets.init_db(self.config_a.db_path)
        await support_tickets.init_db(self.config_b.db_path)

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

    async def test_two_bots_same_admin_tickets_not_mixed(self):
        await _create_ticket_no_kb_match(self.dp_a, self.bot_a, CLIENT_ID, "technical", "Не работает вход", 10)
        await _create_ticket_no_kb_match(self.dp_b, self.bot_b, CLIENT_ID, "billing", "Списали лишнее", 10)

        conn_a = sqlite3.connect(self.config_a.db_path)
        tickets_a = conn_a.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
        row_a = conn_a.execute("SELECT category, description FROM tickets").fetchone()
        conn_a.close()
        conn_b = sqlite3.connect(self.config_b.db_path)
        tickets_b = conn_b.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
        row_b = conn_b.execute("SELECT category, description FROM tickets").fetchone()
        conn_b.close()

        self.assertEqual(tickets_a, 1)
        self.assertEqual(tickets_b, 1)
        self.assertEqual(row_a, ("technical", "Не работает вход"))
        self.assertEqual(row_b, ("billing", "Списали лишнее"))


class SupportTicketsCreationFlowTests(unittest.IsolatedAsyncioTestCase):
    """Full ticket-creation-flow smoke tests: no-KB-match and
    KB-matched-but-didn't-help both end with a ticket row; a KB match the
    client says helped must NOT create a ticket."""

    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = support_tickets.config_from_bot_row(
            {"bot_id": 1003, "name": "support_flow_bot", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await support_tickets.init_db(self.config.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)
        await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/start"))

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_no_kb_match_creates_ticket_immediately(self):
        await _create_ticket_no_kb_match(self.dp, self.bot, CLIENT_ID, "technical", "Приложение падает", 10)
        conn = sqlite3.connect(self.config.db_path)
        row = conn.execute(
            "SELECT status, priority, client_user_id FROM tickets"
        ).fetchone()
        log_authors = [r[0] for r in conn.execute("SELECT author FROM ticket_log ORDER BY id").fetchall()]
        conn.close()
        self.assertEqual(row, ("open", "high", CLIENT_ID))
        self.assertEqual(log_authors, ["client"])

    async def test_kb_match_that_helped_does_not_create_ticket(self):
        async with aiosqlite.connect(self.config.db_path) as db:
            await db.execute(
                "INSERT INTO kb_articles (title, keywords, body) VALUES (?,?,?)",
                ("Проблема с входом", "вход пароль логин", "Сбросьте пароль по ссылке в письме."),
            )
            await db.commit()

        uid = 10
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, CLIENT_ID, "tkt_new")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, CLIENT_ID, "tkt_cat:technical")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _text_update(uid, CLIENT_ID, "не могу войти, забыл пароль")); uid += 1
        # KB match found -> client says it helped.
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, CLIENT_ID, "tkt_kb_helped")); uid += 1

        conn = sqlite3.connect(self.config.db_path)
        count = conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
        conn.close()
        self.assertEqual(count, 0, "a KB match the client said helped still created a ticket")

    async def test_kb_match_that_did_not_help_creates_ticket(self):
        async with aiosqlite.connect(self.config.db_path) as db:
            await db.execute(
                "INSERT INTO kb_articles (title, keywords, body) VALUES (?,?,?)",
                ("Проблема с входом", "вход пароль логин", "Сбросьте пароль по ссылке в письме."),
            )
            await db.commit()

        uid = 10
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, CLIENT_ID, "tkt_new")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, CLIENT_ID, "tkt_cat:technical")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _text_update(uid, CLIENT_ID, "не могу войти, забыл пароль")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, CLIENT_ID, "tkt_kb_create")); uid += 1

        conn = sqlite3.connect(self.config.db_path)
        count = conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
        conn.close()
        self.assertEqual(count, 1, "KB match that didn't help must still let the ticket through")


class SupportTicketsAdminNotifyTests(unittest.IsolatedAsyncioTestCase):
    """Admin views a ticket and transitions it through to closed -> client is
    notified (mirrors vehicle_service's ready-notify test suite)."""

    async def asyncSetUp(self):
        self._bot_call = AsyncMock(return_value=MagicMock())
        self._bot_call_patcher = patch.object(Bot, "__call__", new=self._bot_call)
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = support_tickets.config_from_bot_row(
            {"bot_id": 1004, "name": "support_notify_bot", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await support_tickets.init_db(self.config.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)
        await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/start"))

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    def _sent_texts_to(self, chat_id: int, since: int = 0) -> list[str]:
        # "since" lets a test ignore the bot messages generated by the
        # CLIENT'S OWN ticket-creation flow (which also land in their chat,
        # unlike vehicle_service.py where the client never drives any UI
        # flow themselves) and only look at what was sent AFTER a checkpoint
        # — i.e. genuinely server-initiated pushes, not responses to the
        # client's own taps.
        texts = []
        for call in self._bot_call.call_args_list[since:]:
            request = call.args[0] if call.args else None
            text = getattr(request, "text", None)
            cid = getattr(request, "chat_id", None)
            if text and cid == chat_id:
                texts.append(text)
        return texts

    async def _create_ticket(self) -> int:
        await _create_ticket_no_kb_match(self.dp, self.bot, CLIENT_ID, "technical", "Диагностика", 10)
        conn = sqlite3.connect(self.config.db_path)
        ticket_id = conn.execute("SELECT id FROM tickets").fetchone()[0]
        conn.close()
        return ticket_id

    async def test_no_notification_on_open_to_in_progress(self):
        ticket_id = await self._create_ticket()
        checkpoint = len(self._bot_call.call_args_list)
        await self.dp.feed_webhook_update(
            self.bot, _callback_update(100, ADMIN_ID, f"adm_tkt_status:{ticket_id}:in_progress")
        )
        self.assertEqual(self._sent_texts_to(CLIENT_ID, since=checkpoint), [],
                          "client was notified on a non-notified transition")

    async def test_client_is_notified_and_asked_to_rate_when_ticket_closed(self):
        ticket_id = await self._create_ticket()
        checkpoint = len(self._bot_call.call_args_list)
        await self.dp.feed_webhook_update(
            self.bot, _callback_update(100, ADMIN_ID, f"adm_tkt_status:{ticket_id}:in_progress")
        )
        await self.dp.feed_webhook_update(
            self.bot, _callback_update(101, ADMIN_ID, f"adm_tkt_status:{ticket_id}:closed")
        )

        notifications = self._sent_texts_to(CLIENT_ID, since=checkpoint)
        # Two separate messages: the closure notice and the rating prompt.
        self.assertEqual(len(notifications), 2)
        self.assertTrue(any(str(ticket_id) in t and "закрыт" in t for t in notifications))
        self.assertTrue(any("Оцените" in t for t in notifications))

        conn = sqlite3.connect(self.config.db_path)
        status, notified = conn.execute(
            "SELECT t.status, l.notified FROM tickets t "
            "JOIN ticket_status_log l ON l.ticket_id=t.id AND l.new_status='closed' WHERE t.id=?",
            (ticket_id,),
        ).fetchone()
        conn.close()
        self.assertEqual(status, "closed")
        self.assertEqual(notified, 1)

    async def test_double_tap_on_close_notifies_only_once(self):
        ticket_id = await self._create_ticket()
        checkpoint = len(self._bot_call.call_args_list)
        await self.dp.feed_webhook_update(
            self.bot, _callback_update(100, ADMIN_ID, f"adm_tkt_status:{ticket_id}:closed")
        )
        # Stale/duplicate re-tap of the same transition button.
        await self.dp.feed_webhook_update(
            self.bot, _callback_update(101, ADMIN_ID, f"adm_tkt_status:{ticket_id}:closed")
        )

        notifications = self._sent_texts_to(CLIENT_ID, since=checkpoint)
        self.assertEqual(len(notifications), 2, "double-tap on close notified the client more than once")
        conn = sqlite3.connect(self.config.db_path)
        log_rows = conn.execute(
            "SELECT COUNT(*) FROM ticket_status_log WHERE ticket_id=? AND new_status='closed'", (ticket_id,)
        ).fetchone()[0]
        conn.close()
        self.assertEqual(log_rows, 1, "double-tap inserted more than one status_log row")

    async def test_admin_reply_is_logged_and_delivered_to_client(self):
        ticket_id = await self._create_ticket()
        checkpoint = len(self._bot_call.call_args_list)
        await self.dp.feed_webhook_update(
            self.bot, _callback_update(100, ADMIN_ID, f"adm_tkt_reply:{ticket_id}")
        )
        await self.dp.feed_webhook_update(
            self.bot, _text_update(101, ADMIN_ID, "Проверьте, пожалуйста, обновление приложения.")
        )

        notifications = self._sent_texts_to(CLIENT_ID, since=checkpoint)
        self.assertEqual(len(notifications), 1)
        self.assertIn(str(ticket_id), notifications[0])
        self.assertIn("обновление приложения", notifications[0])

        conn = sqlite3.connect(self.config.db_path)
        rows = conn.execute(
            "SELECT author, body FROM ticket_log WHERE ticket_id=? ORDER BY id", (ticket_id,)
        ).fetchall()
        conn.close()
        self.assertEqual(rows[0][0], "client")
        self.assertEqual(rows[-1], ("admin", "Проверьте, пожалуйста, обновление приложения."))

    async def test_reply_flow_does_not_change_ticket_status(self):
        ticket_id = await self._create_ticket()
        await self.dp.feed_webhook_update(
            self.bot, _callback_update(100, ADMIN_ID, f"adm_tkt_reply:{ticket_id}")
        )
        await self.dp.feed_webhook_update(
            self.bot, _text_update(101, ADMIN_ID, "Работаем над этим.")
        )
        conn = sqlite3.connect(self.config.db_path)
        status = conn.execute("SELECT status FROM tickets WHERE id=?", (ticket_id,)).fetchone()[0]
        conn.close()
        self.assertEqual(status, "open")


class SupportTicketsEscalationTests(unittest.IsolatedAsyncioTestCase):
    """Owner-specified differentiator: explicit client-triggered escalation
    that changes status AND notifies admins with an urgency marker."""

    async def asyncSetUp(self):
        self._bot_call = AsyncMock(return_value=MagicMock())
        self._bot_call_patcher = patch.object(Bot, "__call__", new=self._bot_call)
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = support_tickets.config_from_bot_row(
            {"bot_id": 1005, "name": "support_escalate_bot", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await support_tickets.init_db(self.config.db_path)
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

    async def test_client_escalation_changes_status_and_notifies_admin(self):
        await _create_ticket_no_kb_match(self.dp, self.bot, CLIENT_ID, "general", "Общий вопрос", 10)
        conn = sqlite3.connect(self.config.db_path)
        ticket_id = conn.execute("SELECT id FROM tickets").fetchone()[0]
        conn.close()

        await self.dp.feed_webhook_update(
            self.bot, _callback_update(100, CLIENT_ID, f"tkt_escalate:{ticket_id}")
        )

        conn = sqlite3.connect(self.config.db_path)
        status = conn.execute("SELECT status FROM tickets WHERE id=?", (ticket_id,)).fetchone()[0]
        log_row = conn.execute(
            "SELECT old_status, new_status, changed_by FROM ticket_status_log WHERE ticket_id=?", (ticket_id,)
        ).fetchone()
        conn.close()
        self.assertEqual(status, "escalated")
        self.assertEqual(log_row, ("open", "escalated", CLIENT_ID))

        admin_notifications = self._sent_texts_to(ADMIN_ID)
        self.assertTrue(any(str(ticket_id) in t and "Эскалация" in t for t in admin_notifications))

    async def test_client_cannot_escalate_someone_elses_ticket(self):
        await _create_ticket_no_kb_match(self.dp, self.bot, CLIENT_ID, "general", "Общий вопрос", 10)
        conn = sqlite3.connect(self.config.db_path)
        ticket_id = conn.execute("SELECT id FROM tickets").fetchone()[0]
        conn.close()

        attacker_id = 777
        await self.dp.feed_webhook_update(
            self.bot, _callback_update(100, attacker_id, f"tkt_escalate:{ticket_id}")
        )

        conn = sqlite3.connect(self.config.db_path)
        status = conn.execute("SELECT status FROM tickets WHERE id=?", (ticket_id,)).fetchone()[0]
        conn.close()
        self.assertEqual(status, "open", "an unrelated user was able to escalate someone else's ticket")

    async def test_double_tap_escalation_logs_only_once(self):
        await _create_ticket_no_kb_match(self.dp, self.bot, CLIENT_ID, "general", "Общий вопрос", 10)
        conn = sqlite3.connect(self.config.db_path)
        ticket_id = conn.execute("SELECT id FROM tickets").fetchone()[0]
        conn.close()

        await self.dp.feed_webhook_update(self.bot, _callback_update(100, CLIENT_ID, f"tkt_escalate:{ticket_id}"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(101, CLIENT_ID, f"tkt_escalate:{ticket_id}"))

        conn = sqlite3.connect(self.config.db_path)
        log_rows = conn.execute(
            "SELECT COUNT(*) FROM ticket_status_log WHERE ticket_id=? AND new_status='escalated'", (ticket_id,)
        ).fetchone()[0]
        conn.close()
        self.assertEqual(log_rows, 1, "double-tap escalation inserted more than one status_log row")


class SupportTicketsRatingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = support_tickets.config_from_bot_row(
            {"bot_id": 1006, "name": "support_rating_bot", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await support_tickets.init_db(self.config.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)
        await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/start"))

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_rating_is_stored_once_and_second_tap_does_not_override(self):
        await _create_ticket_no_kb_match(self.dp, self.bot, CLIENT_ID, "general", "Вопрос", 10)
        conn = sqlite3.connect(self.config.db_path)
        ticket_id = conn.execute("SELECT id FROM tickets").fetchone()[0]
        conn.close()
        await self.dp.feed_webhook_update(
            self.bot, _callback_update(100, ADMIN_ID, f"adm_tkt_status:{ticket_id}:closed")
        )

        await self.dp.feed_webhook_update(self.bot, _callback_update(200, CLIENT_ID, f"tkt_rate:{ticket_id}:5"))
        await self.dp.feed_webhook_update(self.bot, _callback_update(201, CLIENT_ID, f"tkt_rate:{ticket_id}:1"))

        conn = sqlite3.connect(self.config.db_path)
        rating = conn.execute("SELECT satisfaction_rating FROM tickets WHERE id=?", (ticket_id,)).fetchone()[0]
        conn.close()
        self.assertEqual(rating, 5, "a second rating tap overrode the first")


class SupportTicketsKbCrudTests(unittest.IsolatedAsyncioTestCase):
    """Admin knowledge-base CRUD smoke test: add, edit, hide."""

    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = support_tickets.config_from_bot_row(
            {"bot_id": 1007, "name": "support_kb_bot", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await support_tickets.init_db(self.config.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)
        await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/start"))

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_add_edit_hide_article(self):
        uid = 10
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, ADMIN_ID, "adm_kb_new")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _text_update(uid, ADMIN_ID, "Как сбросить пароль")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _text_update(uid, ADMIN_ID, "пароль сброс")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _text_update(uid, ADMIN_ID, "Перейдите по ссылке в письме.")); uid += 1

        conn = sqlite3.connect(self.config.db_path)
        row = conn.execute("SELECT id, title, keywords, active FROM kb_articles").fetchone()
        conn.close()
        self.assertEqual(row[1:], ("Как сбросить пароль", "пароль сброс", 1))
        article_id = row[0]

        # Edit: re-enter all three fields.
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, ADMIN_ID, f"adm_kb_edit:{article_id}")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _text_update(uid, ADMIN_ID, "Как сбросить пароль (обновлено)")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _text_update(uid, ADMIN_ID, "-")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _text_update(uid, ADMIN_ID, "Новый текст статьи.")); uid += 1

        conn = sqlite3.connect(self.config.db_path)
        count = conn.execute("SELECT COUNT(*) FROM kb_articles").fetchone()[0]
        row = conn.execute("SELECT title, keywords, body FROM kb_articles WHERE id=?", (article_id,)).fetchone()
        conn.close()
        self.assertEqual(count, 1, "editing created a duplicate row instead of updating in place")
        self.assertEqual(row, ("Как сбросить пароль (обновлено)", None, "Новый текст статьи."))

        # Hide (soft-delete via active flag).
        await self.dp.feed_webhook_update(self.bot, _callback_update(uid, ADMIN_ID, f"adm_kb_toggle:{article_id}")); uid += 1
        conn = sqlite3.connect(self.config.db_path)
        active = conn.execute("SELECT active FROM kb_articles WHERE id=?", (article_id,)).fetchone()[0]
        conn.close()
        self.assertEqual(active, 0)

    async def test_hidden_article_is_excluded_from_client_kb_search(self):
        async with aiosqlite.connect(self.config.db_path) as db:
            cur = await db.execute(
                "INSERT INTO kb_articles (title, keywords, body, active) VALUES (?,?,?,0)",
                ("Скрытая статья", "оплата биллинг", "Текст."),
            )
            await db.commit()
            article_id = cur.lastrowid

        matches = await support_tickets._search_kb(self.config.db_path, "проблема с оплата биллинг")
        self.assertFalse(any(m["id"] == article_id for m in matches), "an inactive article was returned by search")


class SupportTicketsStandaloneSmokeTest(unittest.TestCase):
    def test_config_from_env_matches_legacy_constant_shape(self):
        config = support_tickets.config_from_env()
        self.assertTrue(config.db_path.endswith("support_tickets_data.db"))
        self.assertEqual(config.bot_name, "support_tickets")

    def test_router_and_main_entrypoint_exist(self):
        self.assertTrue(hasattr(support_tickets, "router"))
        self.assertTrue(hasattr(support_tickets, "main"))


if __name__ == "__main__":
    unittest.main()
