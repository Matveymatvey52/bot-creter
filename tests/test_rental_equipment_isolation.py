"""Data isolation, "can't double-issue an item", and overdue-list tests for
the rental_equipment template.

Standard isolation criterion (same as every other *_isolation.py test in this
repo): two bots on the SAME template, different config, must never mix data —
even driven by the SAME Telegram user_id. Everything lives in a
tempfile.TemporaryDirectory, never data/bots.db.

Headline domain invariant this template exists to enforce: one inventory
item can never be actively rented out twice at the same time — a second
attempt must get an explicit refusal, never a silent overwrite of the first
active rental.

No real Telegram network calls, no real tokens.

Run with: python -m unittest tests.test_rental_equipment_isolation
"""

from __future__ import annotations

import asyncio
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
from templates import rental_equipment as rnt

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


def _build_bot_dispatcher(config: rnt.RentalEquipmentConfig) -> tuple[Bot, Dispatcher]:
    bot = Bot(token=FAKE_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(rnt.ConfigMiddleware(config))
    dp.include_router(get_template_router("rental_equipment"))
    return bot, dp


async def _insert_item(db_path: str, name: str, tag: str, status: str = "available") -> int:
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "INSERT INTO items (name, tag, status) VALUES (?, ?, ?)", (name, tag, status)
        )
        await db.commit()
        return cur.lastrowid


async def _insert_client(db_path: str, name: str, contact: str | None = None) -> int:
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute("INSERT INTO clients (name, contact) VALUES (?, ?)", (name, contact))
        await db.commit()
        return cur.lastrowid


class RentalEquipmentIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

        self.config_a = rnt.config_from_bot_row(
            {"bot_id": 901, "name": "rental_bot_a", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        self.config_b = rnt.config_from_bot_row(
            {"bot_id": 902, "name": "rental_bot_b", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await rnt.init_db(self.config_a.db_path)
        await rnt.init_db(self.config_b.db_path)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_configs_point_to_different_files(self):
        self.assertNotEqual(self.config_a.db_path, self.config_b.db_path)
        self.assertNotEqual(self.config_a.admins_file, self.config_b.admins_file)

    async def test_items_isolated_across_bots(self):
        """An item added on bot A must not appear on bot B, even though both
        DBs share the same schema and could theoretically collide on
        AUTOINCREMENT id if pointed at the same file."""
        await _insert_item(self.config_a.db_path, "Дрель", "INV-A1")
        await _insert_item(self.config_b.db_path, "Перфоратор", "INV-B1")

        async with aiosqlite.connect(self.config_a.db_path) as db:
            rows_a = await (await db.execute("SELECT name FROM items")).fetchall()
        async with aiosqlite.connect(self.config_b.db_path) as db:
            rows_b = await (await db.execute("SELECT name FROM items")).fetchall()

        self.assertEqual([r[0] for r in rows_a], ["Дрель"])
        self.assertEqual([r[0] for r in rows_b], ["Перфоратор"])

    async def test_admin_bootstrap_isolated_per_bot(self):
        bot_a, dp_a = _build_bot_dispatcher(self.config_a)
        bot_b, dp_b = _build_bot_dispatcher(self.config_b)
        await dp_a.feed_webhook_update(bot_a, _text_update(1, 111, "/start"))
        await dp_b.feed_webhook_update(bot_b, _text_update(1, 999, "/start"))

        self.assertEqual(rnt._load_admins(self.config_a.admins_file), {"111"})
        self.assertEqual(rnt._load_admins(self.config_b.admins_file), {"999"})

    async def test_same_user_id_sees_only_own_bots_items(self):
        """Cross-bot leak check with the SAME Telegram user_id acting as admin
        on both bots — the exact bug class every isolation test in this
        codebase exists to catch."""
        SAME_USER = 555
        await _insert_item(self.config_a.db_path, "Only on A", "INV-ONLY-A")

        bot_a, dp_a = _build_bot_dispatcher(self.config_a)
        bot_b, dp_b = _build_bot_dispatcher(self.config_b)
        await dp_a.feed_webhook_update(bot_a, _text_update(1, SAME_USER, "/start"))
        await dp_b.feed_webhook_update(bot_b, _text_update(1, SAME_USER, "/start"))

        async with aiosqlite.connect(self.config_a.db_path) as db:
            rows_a = await (await db.execute("SELECT name FROM items")).fetchall()
        async with aiosqlite.connect(self.config_b.db_path) as db:
            rows_b = await (await db.execute("SELECT name FROM items")).fetchall()
        self.assertEqual(len(rows_a), 1)
        self.assertEqual(len(rows_b), 0)


class CannotDoubleIssueItemTests(unittest.IsolatedAsyncioTestCase):
    """Headline domain invariant: одна единица инвентаря не может быть выдана
    дважды одновременно. Both the ORM-level helper (_issue_item) and the
    schema-level partial unique index are exercised."""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = rnt.config_from_bot_row(
            {"bot_id": 903, "name": "rental_double_issue_bot", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await rnt.init_db(self.config.db_path)
        self.item_id = await _insert_item(self.config.db_path, "Дрель", "INV-100")
        self.client_1 = await _insert_client(self.config.db_path, "Иван")
        self.client_2 = await _insert_client(self.config.db_path, "Пётр")

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def test_first_issue_succeeds_and_flips_item_status(self):
        rental_id = await rnt._issue_item(
            self.config.db_path, self.item_id, self.client_1, "2099-01-01", None, 42
        )
        self.assertIsNotNone(rental_id)
        async with aiosqlite.connect(self.config.db_path) as db:
            row = await (await db.execute("SELECT status FROM items WHERE id=?", (self.item_id,))).fetchone()
        self.assertEqual(row[0], "rented")

    async def test_second_issue_of_same_item_is_explicitly_refused(self):
        """The core requirement: no silent overwrite. A second attempt to
        issue an already-rented item must return None (explicit refusal
        surfaced by the handler), and the ORIGINAL rental must remain
        untouched — not double-booked, not reassigned to the second client."""
        first_rental_id = await rnt._issue_item(
            self.config.db_path, self.item_id, self.client_1, "2099-01-01", None, 42
        )
        self.assertIsNotNone(first_rental_id)

        second_rental_id = await rnt._issue_item(
            self.config.db_path, self.item_id, self.client_2, "2099-02-01", None, 99
        )
        self.assertIsNone(second_rental_id)

        async with aiosqlite.connect(self.config.db_path) as db:
            db.row_factory = aiosqlite.Row
            active_rentals = await (await db.execute(
                "SELECT id, client_id FROM rentals WHERE item_id=? AND status='active'", (self.item_id,)
            )).fetchall()
        self.assertEqual(len(active_rentals), 1)
        self.assertEqual(active_rentals[0]["id"], first_rental_id)
        self.assertEqual(active_rentals[0]["client_id"], self.client_1)

    async def test_double_issue_attempted_concurrently_only_one_wins(self):
        """Simulates the real race: two admins tap "Выдать" on the same item
        at nearly the same moment. asyncio.gather runs both _issue_item
        coroutines concurrently — exactly one must succeed."""
        results = await asyncio.gather(
            rnt._issue_item(self.config.db_path, self.item_id, self.client_1, "2099-01-01", None, 42),
            rnt._issue_item(self.config.db_path, self.item_id, self.client_2, "2099-01-01", None, 99),
        )
        succeeded = [r for r in results if r is not None]
        failed = [r for r in results if r is None]
        self.assertEqual(len(succeeded), 1)
        self.assertEqual(len(failed), 1)

        async with aiosqlite.connect(self.config.db_path) as db:
            active_count = (await (await db.execute(
                "SELECT COUNT(*) FROM rentals WHERE item_id=? AND status='active'", (self.item_id,)
            )).fetchone())[0]
        self.assertEqual(active_count, 1)

    async def test_partial_unique_index_rejects_a_second_active_rental_row_directly(self):
        """Schema-level backstop, independent of _issue_item's own
        compare-and-swap: even a raw INSERT that bypasses the application
        helper cannot create two 'active' rows for the same item_id."""
        conn = sqlite3.connect(self.config.db_path)
        conn.execute(
            "INSERT INTO rentals (item_id, client_id, due_date, status) VALUES (?, ?, ?, 'active')",
            (self.item_id, self.client_1, "2099-01-01"),
        )
        conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO rentals (item_id, client_id, due_date, status) VALUES (?, ?, ?, 'active')",
                (self.item_id, self.client_2, "2099-02-01"),
            )
        conn.close()

    async def test_reissue_allowed_after_item_is_returned(self):
        """Not over-strict: once the first rental is properly closed via
        _return_item, the item becomes available again and CAN be issued to
        someone else — the invariant is "not twice AT ONCE", not "never
        again"."""
        first_rental_id = await rnt._issue_item(
            self.config.db_path, self.item_id, self.client_1, "2099-01-01", None, 42
        )
        ok = await rnt._return_item(self.config.db_path, first_rental_id, "В порядке", damaged=False)
        self.assertTrue(ok)

        second_rental_id = await rnt._issue_item(
            self.config.db_path, self.item_id, self.client_2, "2099-02-01", None, 99
        )
        self.assertIsNotNone(second_rental_id)

    async def test_damaged_return_sends_item_to_repair_not_available(self):
        rental_id = await rnt._issue_item(
            self.config.db_path, self.item_id, self.client_1, "2099-01-01", None, 42
        )
        ok = await rnt._return_item(self.config.db_path, rental_id, "Повреждено: треснул корпус", damaged=True)
        self.assertTrue(ok)
        async with aiosqlite.connect(self.config.db_path) as db:
            row = await (await db.execute("SELECT status FROM items WHERE id=?", (self.item_id,))).fetchone()
        self.assertEqual(row[0], "repair")

    async def test_double_return_of_same_rental_is_a_safe_noop_on_second_call(self):
        rental_id = await rnt._issue_item(
            self.config.db_path, self.item_id, self.client_1, "2099-01-01", None, 42
        )
        first_ok = await rnt._return_item(self.config.db_path, rental_id, "В порядке", damaged=False)
        second_ok = await rnt._return_item(self.config.db_path, rental_id, "В порядке", damaged=False)
        self.assertTrue(first_ok)
        self.assertFalse(second_ok)

    async def test_full_issue_flow_via_bot_refuses_second_issue_of_same_item(self):
        """End-to-end through the actual FSM handlers (not just the DB
        helper): drives the real "Выдать" callback twice on the same item via
        the item's own detail-card shortcut and asserts the second attempt's
        confirm step is refused, not silently accepted."""
        bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        mock_call = bot_call_patcher.start()
        try:
            bot, dp = _build_bot_dispatcher(self.config)
            ADMIN = 42
            await dp.feed_webhook_update(bot, _text_update(1, ADMIN, "/start"))

            # First admin issues the item to client_1 via the item-shortcut flow.
            await dp.feed_webhook_update(bot, _callback_update(2, ADMIN, f"rnt_new_for:{self.item_id}"))
            await dp.feed_webhook_update(bot, _callback_update(3, ADMIN, f"rnt_pick_client:{self.client_1}"))
            near_future_due = (date.today() + timedelta(days=30)).strftime("%d.%m.%Y")
            await dp.feed_webhook_update(bot, _text_update(4, ADMIN, near_future_due))
            await dp.feed_webhook_update(bot, _callback_update(5, ADMIN, "rnt_deposit_skip"))
            await dp.feed_webhook_update(bot, _callback_update(6, ADMIN, "rnt_confirm"))

            async with aiosqlite.connect(self.config.db_path) as db:
                row = await (await db.execute("SELECT status FROM items WHERE id=?", (self.item_id,))).fetchone()
            self.assertEqual(row[0], "rented")

            # Second admin tries the SAME item-shortcut again — item is no
            # longer 'available', so cb_rnt_new_for must refuse up front.
            mock_call.reset_mock()
            await dp.feed_webhook_update(bot, _callback_update(7, ADMIN, f"rnt_new_for:{self.item_id}"))
            texts = [
                call.args[0].text for call in mock_call.call_args_list
                if call.args and hasattr(call.args[0], "text") and call.args[0].text
            ]
            self.assertTrue(any("недоступна" in t for t in texts))

            async with aiosqlite.connect(self.config.db_path) as db:
                active_count = (await (await db.execute(
                    "SELECT COUNT(*) FROM rentals WHERE item_id=? AND status='active'", (self.item_id,)
                )).fetchone())[0]
            self.assertEqual(active_count, 1)
        finally:
            bot_call_patcher.stop()


class OverdueListTests(unittest.IsolatedAsyncioTestCase):
    """Список того, что должно было вернуться, но ещё не вернулось: due_date
    в прошлом AND status still 'active'."""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = rnt.config_from_bot_row(
            {"bot_id": 904, "name": "rental_overdue_bot", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await rnt.init_db(self.config.db_path)
        self.client_id = await _insert_client(self.config.db_path, "Клиент")

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def _issue_raw(self, tag: str, due_date: str, status: str = "active") -> int:
        item_id = await _insert_item(self.config.db_path, f"Item {tag}", tag, status="rented")
        async with aiosqlite.connect(self.config.db_path) as db:
            cur = await db.execute(
                "INSERT INTO rentals (item_id, client_id, due_date, status) VALUES (?, ?, ?, ?)",
                (item_id, self.client_id, due_date, status),
            )
            await db.commit()
            return cur.lastrowid

    async def test_overdue_query_includes_past_due_active_rentals(self):
        await self._issue_raw("OVERDUE-1", "2020-01-01", status="active")
        async with aiosqlite.connect(self.config.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(
                "SELECT r.id FROM rentals r WHERE r.status='active' AND r.due_date < ?",
                ("2099-01-01",),
            )).fetchall()
        self.assertEqual(len(rows), 1)

    async def test_overdue_query_excludes_future_due_active_rentals(self):
        await self._issue_raw("FUTURE-1", "2099-12-31", status="active")
        today = date.today().isoformat()
        async with aiosqlite.connect(self.config.db_path) as db:
            rows = await (await db.execute(
                "SELECT id FROM rentals WHERE status='active' AND due_date < ?", (today,)
            )).fetchall()
        self.assertEqual(len(rows), 0)

    async def test_overdue_query_excludes_already_returned_rentals(self):
        """A rental that's past its due date but already closed (status
        'returned') must NOT show up as overdue — it's history, not a
        pending problem."""
        await self._issue_raw("RETURNED-LATE-1", "2020-01-01", status="returned")
        today = date.today().isoformat()
        async with aiosqlite.connect(self.config.db_path) as db:
            rows = await (await db.execute(
                "SELECT id FROM rentals WHERE status='active' AND due_date < ?", (today,)
            )).fetchall()
        self.assertEqual(len(rows), 0)

    async def test_overdue_callback_shows_only_overdue_items_sorted_by_due_date(self):
        """End-to-end through the actual "⏰ Просроченные" handler."""
        await self._issue_raw("OLD-1", "2020-01-01", status="active")
        await self._issue_raw("OLD-2", "2020-06-01", status="active")
        await self._issue_raw("FUTURE-1", "2099-12-31", status="active")
        await self._issue_raw("DONE-1", "2020-01-01", status="returned")

        bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        mock_call = bot_call_patcher.start()
        try:
            bot, dp = _build_bot_dispatcher(self.config)
            ADMIN = 77
            await dp.feed_webhook_update(bot, _text_update(1, ADMIN, "/start"))
            mock_call.reset_mock()
            await dp.feed_webhook_update(bot, _callback_update(2, ADMIN, "rnt_overdue"))

            edits = [
                call.args[0] for call in mock_call.call_args_list
                if call.args and hasattr(call.args[0], "reply_markup") and call.args[0].reply_markup
            ]
            self.assertTrue(edits)
            kb = edits[-1].reply_markup
            button_texts = [b.text for row in kb.inline_keyboard for b in row]
            overdue_buttons = [t for t in button_texts if "OLD-1" in t or "OLD-2" in t]
            self.assertEqual(len(overdue_buttons), 2)
            self.assertFalse(any("FUTURE-1" in t for t in button_texts))
            self.assertFalse(any("DONE-1" in t for t in button_texts))
        finally:
            bot_call_patcher.stop()


class ReviewFixRegressionTests(unittest.IsolatedAsyncioTestCase):
    """Locks in the four review-found fixes applied after the initial
    review-orchestrator pass (state-db + monkey-tester sub-agents)."""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = rnt.config_from_bot_row(
            {"bot_id": 905, "name": "rental_regression_bot", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await rnt.init_db(self.config.db_path)

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def test_deposit_negative_zero_is_normalized_to_positive_zero(self):
        self.assertEqual(rnt._parse_deposit("-0"), 0.0)
        self.assertFalse(str(rnt._parse_deposit("-0")).startswith("-"))

    async def test_due_date_far_in_the_future_is_rejected(self):
        self.assertIsNone(rnt._parse_due_date("31.12.9999"))
        self.assertIsNone(rnt._parse_due_date("2099-01-01"))

    async def test_due_date_within_cap_is_accepted(self):
        near_future = (date.today() + timedelta(days=30)).strftime("%d.%m.%Y")
        self.assertIsNotNone(rnt._parse_due_date(near_future))

    async def test_revoked_admin_cannot_self_reinstate_via_stale_add_admin_flow(self):
        """Review-found (monkey-tester): admin_add_id didn't re-check
        _is_admin, unlike its sibling cb_adm_remove_pick — a revoked admin
        with an open "add admin" prompt could silently re-add themselves."""
        bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        bot_call_patcher.start()
        try:
            bot, dp = _build_bot_dispatcher(self.config)
            REVOKED_ADMIN = 42
            await dp.feed_webhook_update(bot, _text_update(1, REVOKED_ADMIN, "/start"))
            await dp.feed_webhook_update(bot, _callback_update(2, REVOKED_ADMIN, "adm_menu"))
            await dp.feed_webhook_update(bot, _callback_update(3, REVOKED_ADMIN, "adm_add"))

            # Revoked out-of-band (simulating another admin removing them).
            rnt._save_admins(self.config.admins_file, set())

            await dp.feed_webhook_update(bot, _text_update(4, REVOKED_ADMIN, "777"))

            self.assertEqual(rnt._load_admins(self.config.admins_file), set())
        finally:
            bot_call_patcher.stop()

    async def test_revoked_admin_cannot_close_a_rental_via_stale_return_flow(self):
        """Review-found (monkey-tester): _finalize_return_message (the
        text-input damage-note path) didn't re-check _is_admin, unlike its
        callback sibling _finalize_return."""
        client_id = await _insert_client(self.config.db_path, "Клиент")
        item_id = await _insert_item(self.config.db_path, "Дрель", "INV-REV-1")
        rental_id = await rnt._issue_item(self.config.db_path, item_id, client_id, "2030-01-01", None, 1)

        bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        bot_call_patcher.start()
        try:
            bot, dp = _build_bot_dispatcher(self.config)
            REVOKED_ADMIN = 42
            await dp.feed_webhook_update(bot, _text_update(1, REVOKED_ADMIN, "/start"))
            await dp.feed_webhook_update(bot, _callback_update(2, REVOKED_ADMIN, f"ret_start:{rental_id}"))
            await dp.feed_webhook_update(bot, _callback_update(3, REVOKED_ADMIN, "ret_damaged"))

            # Revoked out-of-band while the damage-note prompt is open.
            rnt._save_admins(self.config.admins_file, set())

            await dp.feed_webhook_update(bot, _text_update(4, REVOKED_ADMIN, "разбит корпус"))

            async with aiosqlite.connect(self.config.db_path) as db:
                row = await (await db.execute("SELECT status FROM rentals WHERE id=?", (rental_id,))).fetchone()
            self.assertEqual(row[0], "active")  # NOT closed by the revoked admin
        finally:
            bot_call_patcher.stop()


class RentalEquipmentAdminBootstrapSecurityTests(unittest.IsolatedAsyncioTestCase):
    """Security fix: previously, whoever sent /start FIRST permanently became
    the bot admin — a client testing the bot link before the owner did would
    silently seize the admin panel. See tests/test_shop_catalog_isolation.py
    for the original of this fix, applied identically here."""

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
        config = rnt.config_from_bot_row(
            {"bot_id": 912, "name": "rental_bot_owned", "display_name": None,
             "group_chat_id": None, "owner_telegram_id": 12345},
            self.data_dir,
        )
        await rnt.init_db(config.db_path)
        bot, dp = _build_bot_dispatcher(config)

        CLIENT_ID = 555  # not the owner, messages first
        await dp.feed_webhook_update(bot, _text_update(1, CLIENT_ID, "/start"))
        self.assertEqual(rnt._load_admins(config.admins_file), set())
        self.assertFalse(rnt._is_admin(CLIENT_ID, config))

        await dp.feed_webhook_update(bot, _text_update(2, 12345, "/start"))
        self.assertTrue(rnt._is_admin(12345, config))
        self.assertEqual(rnt._load_admins(config.admins_file), {"12345"})

    async def test_owner_is_always_admin_even_with_stale_admins_file(self):
        config = rnt.config_from_bot_row(
            {"bot_id": 913, "name": "rental_bot_owned_2", "display_name": None,
             "group_chat_id": None, "owner_telegram_id": 777},
            self.data_dir,
        )
        await rnt.init_db(config.db_path)
        rnt._save_admins(config.admins_file, {"999999"})  # some other id, not the owner
        self.assertTrue(rnt._is_admin(777, config))  # owner: always admin
        self.assertTrue(rnt._is_admin(999999, config))  # still honors the file's own admin
        self.assertFalse(rnt._is_admin(4242, config))  # neither owner nor in the file

    async def test_bootstrap_admin_syncs_to_central_bot_admins_table(self):
        config = rnt.config_from_bot_row(
            {"bot_id": 914, "name": "rental_bot_synced", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await rnt.init_db(config.db_path)
        bot, dp = _build_bot_dispatcher(config)
        await dp.feed_webhook_update(bot, _text_update(1, 321, "/start"))

        central_admins = await db_module.get_bot_admins(914)
        self.assertEqual(central_admins, ["321"])


if __name__ == "__main__":
    unittest.main()
