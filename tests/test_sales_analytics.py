import os
import unittest
import asyncio
import aiosqlite
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from aiogram.types import Message, CallbackQuery, User, Chat
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from datetime import datetime

# Import the template
from templates.sales_analytics import (
    init_db, SalesAnalyticsConfig, 
    _save_entry, cb_report_period, 
    _save_admins
)

class TestSalesAnalyticsSmoke(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Global patch for Bot calls to avoid RuntimeError: This method is not mounted...
        from aiogram import Bot
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._bot_call_patcher.start()
        
        self._tmp = tempfile.TemporaryDirectory()
        self.test_data_dir = Path(self._tmp.name)
        
        self.bot_id_1 = 123
        self.bot_id_2 = 456
        
        self.config1 = SalesAnalyticsConfig(
            bot_name="bot1",
            db_path=str(self.test_data_dir / f"bot_{self.bot_id_1}_data.db"),
            admins_file=self.test_data_dir / f"admins_{self.bot_id_1}.json",
            welcome_image=self.test_data_dir / f"bot_{self.bot_id_1}.jpg"
        )
        self.config2 = SalesAnalyticsConfig(
            bot_name="bot2",
            db_path=str(self.test_data_dir / f"bot_{self.bot_id_2}_data.db"),
            admins_file=self.test_data_dir / f"admins_{self.bot_id_2}.json",
            welcome_image=self.test_data_dir / f"bot_{self.bot_id_2}.jpg"
        )
        
        # Setup admin
        self.admin_id = 111
        _save_admins(self.config1.admins_file, {str(self.admin_id)})
        _save_admins(self.config2.admins_file, {str(self.admin_id)})

        self.storage = MemoryStorage()

    async def asyncTearDown(self):
        self._bot_call_patcher.stop()
        self._tmp.cleanup()

    async def test_db_isolation_between_bots(self):
        await init_db(self.config1.db_path)
        await init_db(self.config2.db_path)
        
        # Add sale to bot 1
        async with aiosqlite.connect(self.config1.db_path) as db:
            await db.execute("INSERT INTO analytics_entries (entry_type, amount) VALUES (?,?)", ("sale", 1000))
            await db.commit()
            
        # Check bot 1 has 1 entry, bot 2 has 0
        async with aiosqlite.connect(self.config1.db_path) as db:
            row = await (await db.execute("SELECT COUNT(*) FROM analytics_entries")).fetchone()
            self.assertEqual(row[0], 1)
            
        async with aiosqlite.connect(self.config2.db_path) as db:
            row = await (await db.execute("SELECT COUNT(*) FROM analytics_entries")).fetchone()
            self.assertEqual(row[0], 0)

    async def test_add_entries_flow(self):
        await init_db(self.config1.db_path)
        
        user = User(id=self.admin_id, is_bot=False, first_name="Admin")
        chat = Chat(id=self.admin_id, type="private")
        # In aiogram 3.x, message needs a bot instance for .answer() if not mocked
        mock_bot = MagicMock()
        message = Message(message_id=1, date=datetime.now(), chat=chat, from_user=user, text="Test description")
        message.bot = mock_bot
        # We also need to mock .answer() specifically because it's called in _save_entry
        message.answer = AsyncMock()
        
        # Mock FSM
        state = FSMContext(storage=self.storage, key=MagicMock())
        await state.set_data({"etype": "sale", "amount": 5000})
        
        # Test _save_entry
        await _save_entry(message, state, self.config1, "Test description")
        
        async with aiosqlite.connect(self.config1.db_path) as db:
            row = await (await db.execute("SELECT entry_type, amount, description FROM analytics_entries")).fetchone()
            self.assertEqual(row[0], "sale")
            self.assertEqual(row[1], 5000)
            self.assertEqual(row[2], "Test description")
        
        message.answer.assert_called()

    async def test_report_generation(self):
        await init_db(self.config1.db_path)
        
        # Add diverse entries
        async with aiosqlite.connect(self.config1.db_path) as db:
            await db.execute("INSERT INTO analytics_entries (entry_type, amount) VALUES (?,?)", ("sale", 1000))
            await db.execute("INSERT INTO analytics_entries (entry_type, amount) VALUES (?,?)", ("sale", 2000))
            await db.execute("INSERT INTO analytics_entries (entry_type, amount) VALUES (?,?)", ("lead", 5000))
            await db.execute("INSERT INTO analytics_entries (entry_type, amount) VALUES (?,?)", ("expense", 500))
            await db.commit()
            
        # Mock CallbackQuery for report
        cb = AsyncMock(spec=CallbackQuery)
        cb.data = "rep:all"
        cb.answer = AsyncMock()
        cb.from_user = User(id=self.admin_id, is_bot=False, first_name="Admin")
        cb.message = AsyncMock(spec=Message)
        cb.message.edit_text = AsyncMock()
        
        await cb_report_period(cb, self.config1)
        
        # Verify output contains calculated values
        args, kwargs = cb.message.edit_text.call_args
        text = args[0]
        self.assertIn("3,000", text) # Total sales
        self.assertIn("5,000", text) # Total leads
        self.assertIn("500", text)   # Total expenses
        self.assertIn("1,500", text) # Avg check
        self.assertIn("2,500", text) # Profit

if __name__ == "__main__":
    unittest.main()
