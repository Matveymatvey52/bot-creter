"""support_tickets template — end-to-end flow tests.

Covers the design's headline path: knowledge-base self-service search BEFORE
a ticket is created, ticket creation -> admin notified -> admin replies ->
client notified -> admin closes -> client gets a rating prompt -> rating
recorded in satisfaction_rating. Also covers the escalation transition and
knowledge-base add/soft-delete.

No real Telegram network calls, no real tokens.

Run with: python -m unittest tests.test_support_tickets_ticket_flow
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
from templates import support_tickets as st

FAKE_TOKEN = "123456:test-token-not-real"
ADMIN_ID = 999
CLIENT_ID = 555


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


def _build_bot_dispatcher(config: st.SupportTicketsConfig) -> tuple[Bot, Dispatcher]:
    bot = Bot(token=FAKE_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(st.ConfigMiddleware(config))
    dp.include_router(get_template_router("support_tickets"))
    return bot, dp


class SupportTicketsFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._bot_call = AsyncMock(return_value=MagicMock())
        self._bot_call_patcher = patch.object(Bot, "__call__", new=self._bot_call)
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = st.config_from_bot_row(
            {"bot_id": 950, "name": "support_flow_bot", "display_name": None, "group_chat_id": None}, self.data_dir,
        )
        await st.init_db(self.config.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)
        await self.dp.feed_webhook_update(self.bot, _text_update(1, ADMIN_ID, "/start"))
        self._uid = 100

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    def _next_uid(self) -> int:
        self._uid += 1
        return self._uid

    async def _send_text(self, dp, bot, user_id, text):
        await dp.feed_webhook_update(bot, _text_update(self._next_uid(), user_id, text))

    async def _send_cb(self, dp, bot, user_id, data):
        await dp.feed_webhook_update(bot, _callback_update(self._next_uid(), user_id, data))

    def _sent_texts_to(self, chat_id: int) -> list[str]:
        texts = []
        for call in self._bot_call.call_args_list:
            request = call.args[0] if call.args else None
            text = getattr(request, "text", None) or getattr(request, "caption", None)
            cid = getattr(request, "chat_id", None)
            if text and cid == chat_id:
                texts.append(text)
        return texts

    async def _add_kb_article(self, category: str, question: str, answer: str):
        await self._send_cb(self.dp, self.bot, ADMIN_ID, "kb_menu")
        await self._send_cb(self.dp, self.bot, ADMIN_ID, "kb_add")
        await self._send_cb(self.dp, self.bot, ADMIN_ID, f"kb_add_cat:{category}")
        await self._send_text(self.dp, self.bot, ADMIN_ID, question)
        await self._send_text(self.dp, self.bot, ADMIN_ID, answer)

    # ── headline path ────────────────────────────────────────────────────

    async def test_kb_search_then_ticket_then_reply_then_close_then_rating(self):
        await self._add_kb_article("billing", "Как оплатить?", "Картой в разделе Оплата.")

        # Client searches the KB BEFORE creating a ticket.
        await self.dp.feed_webhook_update(self.bot, _text_update(self._next_uid(), CLIENT_ID, "/start"))
        await self._send_cb(self.dp, self.bot, CLIENT_ID, "cli_support")
        await self._send_cb(self.dp, self.bot, CLIENT_ID, "cli_cat:billing")

        conn = sqlite3.connect(self.config.db_path)
        article_id = conn.execute("SELECT id FROM kb_articles").fetchone()[0]
        conn.close()
        await self._send_cb(self.dp, self.bot, CLIENT_ID, f"cli_kb_view:{article_id}")

        # KB didn't help -> creates a ticket.
        await self._send_cb(self.dp, self.bot, CLIENT_ID, "cli_new_ticket:billing")
        await self._send_text(self.dp, self.bot, CLIENT_ID, "Оплата не проходит, ошибка 500")

        conn = sqlite3.connect(self.config.db_path)
        ticket_id, status, category = conn.execute(
            "SELECT id, status, category FROM tickets WHERE client_user_id=?", (CLIENT_ID,)
        ).fetchone()
        conn.close()
        self.assertEqual(status, "new")
        self.assertEqual(category, "billing")

        # Admin was notified of the new ticket.
        admin_notifications = self._sent_texts_to(ADMIN_ID)
        self.assertTrue(any(f"№{ticket_id}" in t for t in admin_notifications))

        # Admin sees it in the active queue and replies.
        await self._send_cb(self.dp, self.bot, ADMIN_ID, "tk_menu")
        await self._send_cb(self.dp, self.bot, ADMIN_ID, "tk_filter:active")
        await self._send_cb(self.dp, self.bot, ADMIN_ID, f"tk_view:{ticket_id}")
        await self._send_cb(self.dp, self.bot, ADMIN_ID, f"tk_reply:{ticket_id}")
        await self._send_text(self.dp, self.bot, ADMIN_ID, "Повторите попытку оплаты, мы это уже исправили.")

        conn = sqlite3.connect(self.config.db_path)
        status = conn.execute("SELECT status FROM tickets WHERE id=?", (ticket_id,)).fetchone()[0]
        msg_count = conn.execute("SELECT COUNT(*) FROM ticket_messages WHERE ticket_id=?", (ticket_id,)).fetchone()[0]
        conn.close()
        self.assertEqual(status, "answered")
        self.assertEqual(msg_count, 2)  # client's original + admin's reply

        client_notifications = self._sent_texts_to(CLIENT_ID)
        self.assertTrue(any(f"№{ticket_id}" in t and "Повторите попытку" in t for t in client_notifications))

        # Admin closes -> client gets a rating prompt.
        await self._send_cb(self.dp, self.bot, ADMIN_ID, f"tk_close:{ticket_id}")
        conn = sqlite3.connect(self.config.db_path)
        status = conn.execute("SELECT status FROM tickets WHERE id=?", (ticket_id,)).fetchone()[0]
        conn.close()
        self.assertEqual(status, "closed")

        rating_prompts = self._sent_texts_to(CLIENT_ID)
        self.assertTrue(any("закрыт" in t and f"№{ticket_id}" in t for t in rating_prompts))

        # Client rates 5/5.
        await self._send_cb(self.dp, self.bot, CLIENT_ID, f"sup_rate:{ticket_id}:5")
        conn = sqlite3.connect(self.config.db_path)
        rating = conn.execute("SELECT satisfaction_rating FROM tickets WHERE id=?", (ticket_id,)).fetchone()[0]
        conn.close()
        self.assertEqual(rating, 5)

    async def test_client_cannot_reply_to_a_closed_ticket(self):
        await self.dp.feed_webhook_update(self.bot, _text_update(self._next_uid(), CLIENT_ID, "/start"))
        await self._send_cb(self.dp, self.bot, CLIENT_ID, "cli_support")
        await self._send_cb(self.dp, self.bot, CLIENT_ID, "cli_new_ticket:other")
        await self._send_text(self.dp, self.bot, CLIENT_ID, "Проблема")

        conn = sqlite3.connect(self.config.db_path)
        ticket_id = conn.execute("SELECT id FROM tickets WHERE client_user_id=?", (CLIENT_ID,)).fetchone()[0]
        conn.close()

        await self._send_cb(self.dp, self.bot, ADMIN_ID, f"tk_close:{ticket_id}")

        # The reply button itself must refuse to start the FSM on a closed ticket.
        await self._send_cb(self.dp, self.bot, CLIENT_ID, f"cli_ticket_reply:{ticket_id}")
        await self._send_text(self.dp, self.bot, CLIENT_ID, "Ещё сообщение")

        conn = sqlite3.connect(self.config.db_path)
        msg_count = conn.execute("SELECT COUNT(*) FROM ticket_messages WHERE ticket_id=?", (ticket_id,)).fetchone()[0]
        conn.close()
        self.assertEqual(msg_count, 1, "a message was appended to a closed ticket")

    async def test_double_rating_is_rejected_after_first(self):
        await self.dp.feed_webhook_update(self.bot, _text_update(self._next_uid(), CLIENT_ID, "/start"))
        await self._send_cb(self.dp, self.bot, CLIENT_ID, "cli_support")
        await self._send_cb(self.dp, self.bot, CLIENT_ID, "cli_new_ticket:other")
        await self._send_text(self.dp, self.bot, CLIENT_ID, "Проблема")
        conn = sqlite3.connect(self.config.db_path)
        ticket_id = conn.execute("SELECT id FROM tickets WHERE client_user_id=?", (CLIENT_ID,)).fetchone()[0]
        conn.close()
        await self._send_cb(self.dp, self.bot, ADMIN_ID, f"tk_close:{ticket_id}")

        await self._send_cb(self.dp, self.bot, CLIENT_ID, f"sup_rate:{ticket_id}:2")
        await self._send_cb(self.dp, self.bot, CLIENT_ID, f"sup_rate:{ticket_id}:5")

        conn = sqlite3.connect(self.config.db_path)
        rating = conn.execute("SELECT satisfaction_rating FROM tickets WHERE id=?", (ticket_id,)).fetchone()[0]
        conn.close()
        self.assertEqual(rating, 2, "second rating attempt overwrote the first")

    # ── escalation ───────────────────────────────────────────────────────

    async def test_escalation_sets_status_and_adds_distinct_service_note_without_client_notify(self):
        await self.dp.feed_webhook_update(self.bot, _text_update(self._next_uid(), CLIENT_ID, "/start"))
        await self._send_cb(self.dp, self.bot, CLIENT_ID, "cli_support")
        await self._send_cb(self.dp, self.bot, CLIENT_ID, "cli_new_ticket:technical")
        await self._send_text(self.dp, self.bot, CLIENT_ID, "Не работает интеграция")
        conn = sqlite3.connect(self.config.db_path)
        ticket_id = conn.execute("SELECT id FROM tickets WHERE client_user_id=?", (CLIENT_ID,)).fetchone()[0]
        conn.close()

        await self._send_cb(self.dp, self.bot, ADMIN_ID, f"tk_escalate:{ticket_id}")

        conn = sqlite3.connect(self.config.db_path)
        status = conn.execute("SELECT status FROM tickets WHERE id=?", (ticket_id,)).fetchone()[0]
        messages = [r[0] for r in conn.execute(
            "SELECT text FROM ticket_messages WHERE ticket_id=? ORDER BY id", (ticket_id,)
        ).fetchall()]
        conn.close()
        self.assertEqual(status, "escalated")
        self.assertTrue(any("эскалирован" in m for m in messages))

        # Escalation is NOT a normal admin reply -> must not trigger
        # NEW_REPLY_NOTIFY_TEXT to the client.
        client_notifications = self._sent_texts_to(CLIENT_ID)
        self.assertFalse(any("Новый ответ" in t for t in client_notifications))

    async def test_double_escalate_is_a_noop_on_second_tap(self):
        await self.dp.feed_webhook_update(self.bot, _text_update(self._next_uid(), CLIENT_ID, "/start"))
        await self._send_cb(self.dp, self.bot, CLIENT_ID, "cli_support")
        await self._send_cb(self.dp, self.bot, CLIENT_ID, "cli_new_ticket:technical")
        await self._send_text(self.dp, self.bot, CLIENT_ID, "Проблема")
        conn = sqlite3.connect(self.config.db_path)
        ticket_id = conn.execute("SELECT id FROM tickets WHERE client_user_id=?", (CLIENT_ID,)).fetchone()[0]
        conn.close()

        await self._send_cb(self.dp, self.bot, ADMIN_ID, f"tk_escalate:{ticket_id}")
        await self._send_cb(self.dp, self.bot, ADMIN_ID, f"tk_escalate:{ticket_id}")

        conn = sqlite3.connect(self.config.db_path)
        note_count = conn.execute(
            "SELECT COUNT(*) FROM ticket_messages WHERE ticket_id=? AND text LIKE '%эскалирован%'", (ticket_id,)
        ).fetchone()[0]
        conn.close()
        self.assertEqual(note_count, 1, "a stale double-tap on escalate inserted a second service note")

    # ── knowledge base add / soft-delete ────────────────────────────────

    async def test_kb_add_then_soft_delete_hides_it_from_client_search_but_keeps_the_row(self):
        await self._add_kb_article("account", "Как сменить пароль?", "В настройках профиля.")
        conn = sqlite3.connect(self.config.db_path)
        article_id, active = conn.execute("SELECT id, active FROM kb_articles").fetchone()
        conn.close()
        self.assertEqual(active, 1)

        # Visible to a client before hiding.
        await self.dp.feed_webhook_update(self.bot, _text_update(self._next_uid(), CLIENT_ID, "/start"))
        await self._send_cb(self.dp, self.bot, CLIENT_ID, "cli_support")
        await self._send_cb(self.dp, self.bot, CLIENT_ID, "cli_cat:account")
        conn = sqlite3.connect(self.config.db_path)
        visible_before = conn.execute("SELECT COUNT(*) FROM kb_articles WHERE category='account' AND active=1").fetchone()[0]
        conn.close()
        self.assertEqual(visible_before, 1)

        # Admin hides (soft-deletes) it.
        await self._send_cb(self.dp, self.bot, ADMIN_ID, f"kb_hide:{article_id}")

        conn = sqlite3.connect(self.config.db_path)
        row = conn.execute("SELECT active FROM kb_articles WHERE id=?", (article_id,)).fetchone()
        visible_after = conn.execute("SELECT COUNT(*) FROM kb_articles WHERE category='account' AND active=1").fetchone()[0]
        conn.close()
        self.assertIsNotNone(row, "soft-delete must not remove the row")
        self.assertEqual(row[0], 0)
        self.assertEqual(visible_after, 0)

    async def test_kb_edit_updates_question_and_answer_in_place(self):
        await self._add_kb_article("other", "Old question?", "Old answer.")
        conn = sqlite3.connect(self.config.db_path)
        article_id = conn.execute("SELECT id FROM kb_articles").fetchone()[0]
        conn.close()

        await self._send_cb(self.dp, self.bot, ADMIN_ID, f"kb_edit:{article_id}")
        await self._send_text(self.dp, self.bot, ADMIN_ID, "New question?")
        await self._send_text(self.dp, self.bot, ADMIN_ID, "New answer.")

        conn = sqlite3.connect(self.config.db_path)
        question, answer = conn.execute(
            "SELECT question, answer FROM kb_articles WHERE id=?", (article_id,)
        ).fetchone()
        count = conn.execute("SELECT COUNT(*) FROM kb_articles").fetchone()[0]
        conn.close()
        self.assertEqual(question, "New question?")
        self.assertEqual(answer, "New answer.")
        self.assertEqual(count, 1, "edit must update in place, not insert a new row")


if __name__ == "__main__":
    unittest.main()
