"""features/voice_intake.py — schema registration, transcription/parsing
dispatch, save/on_saved wiring, and per-bot_id isolation.

Driven through a real aiogram Dispatcher, same convention as
test_payments_module.py / test_sellable_items_module.py: voice_intake.router
cloned onto a Dispatcher with a small local middleware standing in for
runtime.registry.py's own ConfigMiddleware + bot_id injection (see that
module's _attach_bot_id_middleware).

_transcribe_voice and _parse_with_claude (both real-network calls in
production) are patched at the module level in every test — no real
AssemblyAI/Anthropic calls, no real tokens.

Run with: python -m unittest tests.test_voice_intake_module
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

import features.voice_intake as voice_intake
from features.voice_intake import VoiceFieldSpec, VoiceRecordType, VoiceSchema
from runtime.registry import _clone_router

FAKE_TOKEN = "123456:test-token-not-real"
USER_ID = 111


@dataclass
class FixtureConfig:
    db_path: str
    admins_file: str | None = None


class ConfigAndBotIdMiddleware:
    """Stands in for runtime/registry.py's ConfigMiddleware + the bot_id
    injection _load_and_include_features() does for every feature router."""

    def __init__(self, config: FixtureConfig, bot_id: int) -> None:
        self.config = config
        self.bot_id = bot_id

    async def __call__(self, handler, event, data):
        data["config"] = self.config
        data["bot_id"] = self.bot_id
        return await handler(event, data)


def _voice_update(update_id: int, user_id: int) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id, "date": 1700000000,
            "chat": {"id": user_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
            "voice": {
                "file_id": f"voice-{update_id}", "file_unique_id": f"u{update_id}",
                "duration": 3,
            },
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


def _build_dispatcher(config: FixtureConfig, bot_id: int) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(ConfigAndBotIdMiddleware(config, bot_id))
    dp.include_router(_clone_router(voice_intake.router))
    return dp


# ── Fixture DB helpers (a minimal "notes" host, plus tour_operator-shaped one) ─
async def _init_notes_db(db_path: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                book_id INTEGER, title TEXT, body TEXT
            );
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                book_id INTEGER, text TEXT, due TEXT
            );
            CREATE TABLE IF NOT EXISTS books (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT
            );
        """)
        await db.commit()


async def _notes_get_context_id(db_path: str, user_id) -> int | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT id FROM books LIMIT 1") as c:
            row = await c.fetchone()
            return row["id"] if row else None


async def _save_note(db_path: str, book_id, d: dict):
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "INSERT INTO notes(book_id,title,body) VALUES(?,?,?)",
            (book_id, d.get("title", "Untitled"), d.get("body")),
        )
        await db.commit()
        return cur.lastrowid


async def _save_reminder(db_path: str, book_id, d: dict):
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO reminders(book_id,text,due) VALUES(?,?,?)",
            (book_id, d.get("text", ""), d.get("due")),
        )
        await db.commit()


def _notes_schema() -> VoiceSchema:
    return VoiceSchema(
        get_context_id=_notes_get_context_id,
        no_context_message="⚠️ No book yet.",
        record_types=[
            VoiceRecordType(
                key="note", label="Note", icon="📝",
                prompt_desc="title, body",
                fields=[VoiceFieldSpec("title", "Title"), VoiceFieldSpec("body", "Body")],
                save=_save_note,
            ),
            VoiceRecordType(
                key="reminder", label="Reminder", icon="⏰",
                prompt_desc="text, due(YYYY-MM-DD)",
                fields=[VoiceFieldSpec("text", "Text"), VoiceFieldSpec("due", "Due")],
                save=_save_reminder,
            ),
        ],
    )


async def _init_tour_db(db_path: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS tours (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT
            );
            CREATE TABLE IF NOT EXISTS locations (
                id INTEGER PRIMARY KEY AUTOINCREMENT, tour_id INTEGER, name TEXT
            );
            CREATE TABLE IF NOT EXISTS user_prefs (
                user_id TEXT PRIMARY KEY, active_tour_id INTEGER
            );
        """)
        await db.commit()


async def _tour_get_context_id(db_path: str, user_id) -> int | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT t.id FROM tours t JOIN user_prefs u ON t.id=u.active_tour_id WHERE u.user_id=?",
            (str(user_id),),
        ) as c:
            row = await c.fetchone()
            return row["id"] if row else None


async def _tour_set_active(db_path: str, user_id, tour_id) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO user_prefs(user_id,active_tour_id) VALUES(?,?)"
            " ON CONFLICT(user_id) DO UPDATE SET active_tour_id=excluded.active_tour_id",
            (str(user_id), tour_id),
        )
        await db.commit()


async def _save_tour_location(db_path: str, tour_id, d: dict):
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO locations(tour_id,name) VALUES(?,?)",
            (tour_id, d.get("name", "Unnamed")),
        )
        await db.commit()


async def _save_new_tour(db_path: str, _context_id, d: dict) -> int:
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute("INSERT INTO tours(name) VALUES(?)", (d.get("name", "New tour"),))
        await db.commit()
        return cur.lastrowid


async def _on_new_tour_saved(db_path: str, user_id: int, new_tour_id) -> None:
    await _tour_set_active(db_path, user_id, new_tour_id)


def _tour_schema() -> VoiceSchema:
    return VoiceSchema(
        get_context_id=_tour_get_context_id,
        no_context_message="⚠️ Нет активного тура. Создайте: /newtrip",
        record_types=[
            VoiceRecordType(
                key="new_tour", label="Новый тур", icon="🌍",
                prompt_desc="name",
                fields=[VoiceFieldSpec("name", "Название")],
                save=_save_new_tour,
                on_saved=_on_new_tour_saved,
            ),
            VoiceRecordType(
                key="location", label="ЛиП", icon="📍",
                prompt_desc="name",
                fields=[VoiceFieldSpec("name", "Название")],
                save=_save_tour_location,
            ),
        ],
    )


class VoiceIntakeTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=AsyncMock()))
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.bot = Bot(token=FAKE_TOKEN)
        voice_intake._schemas.clear()

    async def asyncTearDown(self):
        voice_intake._schemas.clear()
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    def _tmp_db(self, name: str) -> str:
        return str(Path(self._tmp.name) / name)


class TourOperatorSchemaTests(VoiceIntakeTestCase):
    """Criterion 1: voice -> structured record works for the tour_operator-shaped schema."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.db_path = self._tmp_db("tour.db")
        await _init_tour_db(self.db_path)
        self.bot_id = 501
        voice_intake.register_schema(self.bot_id, _tour_schema())
        self.config = FixtureConfig(db_path=self.db_path)
        self.dp = _build_dispatcher(self.config, self.bot_id)

        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("INSERT INTO tours(name) VALUES('Bali trip')")
            await db.commit()
            self.tour_id = cur.lastrowid
        await _tour_set_active(self.db_path, USER_ID, self.tour_id)

    async def test_voice_to_location_record(self):
        with patch.object(voice_intake, "_transcribe_voice", new=AsyncMock(return_value="Beach spot, great sunset")), \
             patch.object(voice_intake, "_parse_with_claude", new=AsyncMock(
                 return_value={"type": "location", "data": {"name": "Beach spot"}, "confidence": 0.9})):
            await self.dp.feed_webhook_update(self.bot, _voice_update(1, USER_ID))
            await self.dp.feed_webhook_update(self.bot, _callback_update(2, USER_ID, "vs_save"))

        conn = sqlite3.connect(self.db_path)
        rows = conn.execute("SELECT tour_id, name FROM locations").fetchall()
        conn.close()
        self.assertEqual(rows, [(self.tour_id, "Beach spot")])

    async def test_new_tour_save_returns_id_and_invokes_on_saved(self):
        with patch.object(voice_intake, "_transcribe_voice", new=AsyncMock(return_value="New tour to Japan")), \
             patch.object(voice_intake, "_parse_with_claude", new=AsyncMock(
                 return_value={"type": "new_tour", "data": {"name": "Japan trip"}, "confidence": 0.95})):
            await self.dp.feed_webhook_update(self.bot, _voice_update(1, USER_ID))
            await self.dp.feed_webhook_update(self.bot, _callback_update(2, USER_ID, "vs_save"))

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        tours = conn.execute("SELECT * FROM tours WHERE name='Japan trip'").fetchall()
        self.assertEqual(len(tours), 1)
        new_tour_id = tours[0]["id"]
        active = conn.execute(
            "SELECT active_tour_id FROM user_prefs WHERE user_id=?", (str(USER_ID),)
        ).fetchone()
        conn.close()
        # on_saved (=_on_new_tour_saved) must have flipped active_tour_id to
        # the newly created tour, not left the earlier "Bali trip" active.
        self.assertEqual(active[0], new_tour_id)

    async def test_no_context_id_replies_and_does_not_transcribe(self):
        other_user = 222  # never called _tour_set_active
        transcribe_mock = AsyncMock(return_value="text")
        with patch.object(voice_intake, "_transcribe_voice", new=transcribe_mock):
            await self.dp.feed_webhook_update(self.bot, _voice_update(1, other_user))
        transcribe_mock.assert_not_awaited()

    async def test_unknown_type_bails_without_setting_fsm_state(self):
        with patch.object(voice_intake, "_transcribe_voice", new=AsyncMock(return_value="gibberish")), \
             patch.object(voice_intake, "_parse_with_claude", new=AsyncMock(
                 return_value={"type": "unknown", "data": {}, "confidence": 0})):
            await self.dp.feed_webhook_update(self.bot, _voice_update(1, USER_ID))

        storage = self.dp.storage
        from aiogram.fsm.storage.base import StorageKey
        key = StorageKey(bot_id=self.bot.id, chat_id=USER_ID, user_id=USER_ID)
        state = await storage.get_state(key)
        self.assertIsNone(state)

    async def test_empty_transcription_bails_without_crashing(self):
        with patch.object(voice_intake, "_transcribe_voice", new=AsyncMock(return_value="")):
            # Must not raise.
            await self.dp.feed_webhook_update(self.bot, _voice_update(1, USER_ID))

        storage = self.dp.storage
        from aiogram.fsm.storage.base import StorageKey
        key = StorageKey(bot_id=self.bot.id, chat_id=USER_ID, user_id=USER_ID)
        state = await storage.get_state(key)
        self.assertIsNone(state)


class NotesSchemaTests(VoiceIntakeTestCase):
    """Criterion 2: a SECOND, unrelated schema (not tour_operator-shaped)
    proves the module is genuinely reusable, not tour_operator-specific."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.db_path = self._tmp_db("notes.db")
        await _init_notes_db(self.db_path)
        self.bot_id = 502
        voice_intake.register_schema(self.bot_id, _notes_schema())
        self.config = FixtureConfig(db_path=self.db_path)
        self.dp = _build_dispatcher(self.config, self.bot_id)

        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("INSERT INTO books(name) VALUES('My Book')")
            await db.commit()
            self.book_id = cur.lastrowid

    async def test_voice_to_note_record(self):
        with patch.object(voice_intake, "_transcribe_voice", new=AsyncMock(return_value="Remember the milk")), \
             patch.object(voice_intake, "_parse_with_claude", new=AsyncMock(
                 return_value={"type": "note", "data": {"title": "Milk", "body": "buy milk"}, "confidence": 0.8})):
            await self.dp.feed_webhook_update(self.bot, _voice_update(1, USER_ID))
            await self.dp.feed_webhook_update(self.bot, _callback_update(2, USER_ID, "vs_save"))

        conn = sqlite3.connect(self.db_path)
        rows = conn.execute("SELECT book_id, title, body FROM notes").fetchall()
        conn.close()
        self.assertEqual(rows, [(self.book_id, "Milk", "buy milk")])

    async def test_voice_to_reminder_record(self):
        with patch.object(voice_intake, "_transcribe_voice", new=AsyncMock(return_value="Call mom tomorrow")), \
             patch.object(voice_intake, "_parse_with_claude", new=AsyncMock(
                 return_value={"type": "reminder", "data": {"text": "Call mom", "due": "2026-08-15"}, "confidence": 0.7})):
            await self.dp.feed_webhook_update(self.bot, _voice_update(1, USER_ID))
            await self.dp.feed_webhook_update(self.bot, _callback_update(2, USER_ID, "vs_save"))

        conn = sqlite3.connect(self.db_path)
        rows = conn.execute("SELECT book_id, text, due FROM reminders").fetchall()
        conn.close()
        self.assertEqual(rows, [(self.book_id, "Call mom", "2026-08-15")])

    async def test_vs_cancel_clears_state_without_saving(self):
        with patch.object(voice_intake, "_transcribe_voice", new=AsyncMock(return_value="Remember the milk")), \
             patch.object(voice_intake, "_parse_with_claude", new=AsyncMock(
                 return_value={"type": "note", "data": {"title": "Milk"}, "confidence": 0.8})):
            await self.dp.feed_webhook_update(self.bot, _voice_update(1, USER_ID))
            await self.dp.feed_webhook_update(self.bot, _callback_update(2, USER_ID, "vs_cancel"))

        conn = sqlite3.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
        conn.close()
        self.assertEqual(count, 0)


class BotIsolationTests(VoiceIntakeTestCase):
    """Criterion 3: two different bot_ids with two different schemas never
    cross-resolve each other's record types/tables."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.tour_db = self._tmp_db("tour_iso.db")
        self.notes_db = self._tmp_db("notes_iso.db")
        await _init_tour_db(self.tour_db)
        await _init_notes_db(self.notes_db)

        self.tour_bot_id = 601
        self.notes_bot_id = 602
        voice_intake.register_schema(self.tour_bot_id, _tour_schema())
        voice_intake.register_schema(self.notes_bot_id, _notes_schema())

        async with aiosqlite.connect(self.tour_db) as db:
            cur = await db.execute("INSERT INTO tours(name) VALUES('Iso tour')")
            await db.commit()
            self.tour_id = cur.lastrowid
        await _tour_set_active(self.tour_db, USER_ID, self.tour_id)
        async with aiosqlite.connect(self.notes_db) as db:
            cur = await db.execute("INSERT INTO books(name) VALUES('Iso book')")
            await db.commit()
            self.book_id = cur.lastrowid

        self.tour_dp = _build_dispatcher(FixtureConfig(db_path=self.tour_db), self.tour_bot_id)
        self.notes_dp = _build_dispatcher(FixtureConfig(db_path=self.notes_db), self.notes_bot_id)

    async def test_each_bot_resolves_its_own_schema_not_the_others(self):
        # Send a "note"-typed parse to the notes bot and a "location"-typed
        # parse to the tour bot, concurrently-registered — each dispatcher
        # must only ever see ITS bot_id's schema, proven by each successfully
        # resolving its own record type (an unrecognized type would bail out
        # at the "unknown/not-in-schema" branch instead of saving).
        with patch.object(voice_intake, "_transcribe_voice", new=AsyncMock(return_value="note text")), \
             patch.object(voice_intake, "_parse_with_claude", new=AsyncMock(
                 return_value={"type": "note", "data": {"title": "N"}, "confidence": 0.9})):
            await self.notes_dp.feed_webhook_update(self.bot, _voice_update(1, USER_ID))
            await self.notes_dp.feed_webhook_update(self.bot, _callback_update(2, USER_ID, "vs_save"))

        with patch.object(voice_intake, "_transcribe_voice", new=AsyncMock(return_value="loc text")), \
             patch.object(voice_intake, "_parse_with_claude", new=AsyncMock(
                 return_value={"type": "location", "data": {"name": "L"}, "confidence": 0.9})):
            await self.tour_dp.feed_webhook_update(self.bot, _voice_update(3, USER_ID))
            await self.tour_dp.feed_webhook_update(self.bot, _callback_update(4, USER_ID, "vs_save"))

        notes_conn = sqlite3.connect(self.notes_db)
        notes_count = notes_conn.execute("SELECT COUNT(*) FROM notes WHERE title='N'").fetchone()[0]
        notes_conn.close()
        tour_conn = sqlite3.connect(self.tour_db)
        loc_count = tour_conn.execute("SELECT COUNT(*) FROM locations WHERE name='L'").fetchone()[0]
        tour_conn.close()

        self.assertEqual(notes_count, 1)
        self.assertEqual(loc_count, 1)

    async def test_notes_type_sent_to_tour_bot_is_treated_as_unknown(self):
        # "note" is not a record type in the tour schema — the tour bot must
        # reject it (bail out) rather than somehow saving it anywhere.
        with patch.object(voice_intake, "_transcribe_voice", new=AsyncMock(return_value="note text")), \
             patch.object(voice_intake, "_parse_with_claude", new=AsyncMock(
                 return_value={"type": "note", "data": {"title": "Should not save"}, "confidence": 0.9})):
            await self.tour_dp.feed_webhook_update(self.bot, _voice_update(1, USER_ID))

        storage = self.tour_dp.storage
        from aiogram.fsm.storage.base import StorageKey
        key = StorageKey(bot_id=self.bot.id, chat_id=USER_ID, user_id=USER_ID)
        state = await storage.get_state(key)
        self.assertIsNone(state)


# ── Data-driven mode (from-scratch bots — register_data_driven_schema) ──────
async def _init_inventory_db(db_path: str) -> None:
    """A minimal from-scratch-shaped bot: a top-level `warehouses` table (the
    context concept, tour_operator's `user_prefs`/`tours` equivalent) and an
    `items` table linked to it — no hand-written closures anywhere, matching
    what services/claude_service.py's _generate_voice_cashflow_config would
    produce for a bot like this."""
    async with aiosqlite.connect(db_path) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS warehouses (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, user_id TEXT
            );
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                warehouse_id INTEGER, title TEXT, quantity INTEGER
            );
        """)
        await db.commit()


_INVENTORY_DATA_DRIVEN_CONFIG = {
    "context_table": "warehouses",
    "context_column": "id",
    "record_types": [
        {
            "key": "item", "label": "Item", "icon": "📦",
            "prompt_desc": "title, quantity",
            "table": "items",
            "context_column": "warehouse_id",
            "fields": [
                {"name": "title", "label": "Title", "column": "title"},
                {"name": "quantity", "label": "Quantity", "column": "quantity"},
            ],
        },
    ],
}


class DataDrivenSchemaTests(VoiceIntakeTestCase):
    """register_data_driven_schema builds a fully working VoiceSchema from a
    plain dict — the from-scratch counterpart to TourOperatorSchemaTests'
    hand-written closures above. Same voice -> confirm -> save flow, driven
    through the real router, proving the generic _generic_save/
    _generic_get_context_id path behaves identically to a closure-mode
    schema from the outside."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.db_path = self._tmp_db("inventory.db")
        await _init_inventory_db(self.db_path)
        self.bot_id = 601
        voice_intake.register_data_driven_schema(self.bot_id, _INVENTORY_DATA_DRIVEN_CONFIG)
        self.config = FixtureConfig(db_path=self.db_path)
        self.dp = _build_dispatcher(self.config, self.bot_id)

        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("INSERT INTO warehouses(name, user_id) VALUES('Main', ?)", (str(USER_ID),))
            await db.commit()
            self.warehouse_id = cur.lastrowid

    async def test_voice_to_item_record_via_generic_save(self):
        with patch.object(voice_intake, "_transcribe_voice", new=AsyncMock(return_value="Ten hammers")), \
             patch.object(voice_intake, "_parse_with_claude", new=AsyncMock(
                 return_value={"type": "item", "data": {"title": "Hammer", "quantity": 10}, "confidence": 0.9})):
            await self.dp.feed_webhook_update(self.bot, _voice_update(1, USER_ID))
            await self.dp.feed_webhook_update(self.bot, _callback_update(2, USER_ID, "vs_save"))

        conn = sqlite3.connect(self.db_path)
        rows = conn.execute("SELECT warehouse_id, title, quantity FROM items").fetchall()
        conn.close()
        self.assertEqual(rows, [(self.warehouse_id, "Hammer", 10)])

    async def test_no_context_id_replies_and_does_not_transcribe(self):
        other_user_id = USER_ID + 1  # no warehouses row keyed by this user_id
        transcribe_mock = AsyncMock(return_value="doesn't matter")
        with patch.object(voice_intake, "_transcribe_voice", new=transcribe_mock):
            await self.dp.feed_webhook_update(self.bot, _voice_update(1, other_user_id))
        transcribe_mock.assert_not_called()

    async def test_unmapped_field_is_dropped_not_inserted(self):
        # "notes" isn't in column_map for the "item" record type — must be
        # silently dropped (parsed but never written), same as a closure
        # that simply doesn't reference an extra key in `d`.
        with patch.object(voice_intake, "_transcribe_voice", new=AsyncMock(return_value="text")), \
             patch.object(voice_intake, "_parse_with_claude", new=AsyncMock(
                 return_value={
                     "type": "item",
                     "data": {"title": "Wrench", "quantity": 3, "notes": "should be dropped"},
                     "confidence": 0.9,
                 })):
            await self.dp.feed_webhook_update(self.bot, _voice_update(1, USER_ID))
            await self.dp.feed_webhook_update(self.bot, _callback_update(2, USER_ID, "vs_save"))

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM items WHERE title='Wrench'").fetchone()
        conn.close()
        self.assertNotIn("notes", row.keys())


class DataDrivenNoContextConceptTests(VoiceIntakeTestCase):
    """A schema with no context_table/context_column at all (most from-scratch
    bots — no per-user "active X" concept) must accept every record
    unconditionally rather than block on _schema_has_context_concept."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.db_path = self._tmp_db("standalone.db")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "CREATE TABLE IF NOT EXISTS expenses (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, amount REAL)"
            )
            await db.commit()
        self.bot_id = 602
        voice_intake.register_data_driven_schema(self.bot_id, {
            "context_table": None,
            "context_column": None,
            "record_types": [{
                "key": "expense", "label": "Expense", "icon": "💸",
                "prompt_desc": "title, amount",
                "table": "expenses",
                "context_column": None,
                "fields": [
                    {"name": "title", "label": "Title", "column": "title"},
                    {"name": "amount", "label": "Amount", "column": "amount"},
                ],
            }],
        })
        self.config = FixtureConfig(db_path=self.db_path)
        self.dp = _build_dispatcher(self.config, self.bot_id)

    async def test_saves_without_any_context_gating(self):
        with patch.object(voice_intake, "_transcribe_voice", new=AsyncMock(return_value="Lunch 500")), \
             patch.object(voice_intake, "_parse_with_claude", new=AsyncMock(
                 return_value={"type": "expense", "data": {"title": "Lunch", "amount": 500}, "confidence": 0.9})):
            await self.dp.feed_webhook_update(self.bot, _voice_update(1, USER_ID))
            await self.dp.feed_webhook_update(self.bot, _callback_update(2, USER_ID, "vs_save"))

        conn = sqlite3.connect(self.db_path)
        rows = conn.execute("SELECT title, amount FROM expenses").fetchall()
        conn.close()
        self.assertEqual(rows, [("Lunch", 500)])


class ValidateDataDrivenConfigTests(unittest.TestCase):
    """_validate_data_driven_config — the defensive re-check registry.py runs
    against the bot's CURRENT real tables before ever trusting a stored
    config, mirroring _validate_miniapp_config_against_code's posture."""

    def setUp(self):
        self.tables = {"items": {"id", "warehouse_id", "title", "quantity"}, "warehouses": {"id", "name", "user_id"}}

    def test_valid_config_passes(self):
        self.assertTrue(voice_intake._validate_data_driven_config(_INVENTORY_DATA_DRIVEN_CONFIG, self.tables))

    def test_hallucinated_table_fails(self):
        bad = {**_INVENTORY_DATA_DRIVEN_CONFIG, "record_types": [{
            **_INVENTORY_DATA_DRIVEN_CONFIG["record_types"][0], "table": "does_not_exist",
        }]}
        self.assertFalse(voice_intake._validate_data_driven_config(bad, self.tables))

    def test_hallucinated_column_fails(self):
        bad_rt = dict(_INVENTORY_DATA_DRIVEN_CONFIG["record_types"][0])
        bad_rt["fields"] = [{"name": "title", "label": "Title", "column": "not_a_real_column"}]
        bad = {**_INVENTORY_DATA_DRIVEN_CONFIG, "record_types": [bad_rt]}
        self.assertFalse(voice_intake._validate_data_driven_config(bad, self.tables))

    def test_empty_record_types_fails(self):
        self.assertFalse(voice_intake._validate_data_driven_config({"record_types": []}, self.tables))

    def test_missing_record_types_key_fails(self):
        self.assertFalse(voice_intake._validate_data_driven_config({}, self.tables))

    def test_context_table_not_in_real_tables_fails(self):
        bad = {**_INVENTORY_DATA_DRIVEN_CONFIG, "context_table": "not_real"}
        self.assertFalse(voice_intake._validate_data_driven_config(bad, self.tables))

    def test_none_context_table_and_column_is_valid(self):
        config = {
            "context_table": None, "context_column": None,
            "record_types": [_INVENTORY_DATA_DRIVEN_CONFIG["record_types"][0]],
        }
        self.assertTrue(voice_intake._validate_data_driven_config(config, self.tables))


if __name__ == "__main__":
    unittest.main()
