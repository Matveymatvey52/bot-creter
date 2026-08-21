"""property_rental template — features/reminders.py integration.

Proves reminders_config's recipient_query (join through leases, since
rent_payments carries no tenant_user_id of its own) resolves the right
tenant and fires at the configured REMINDER_DAYS_BEFORE_RENT offset — not
before, not after.

Run with: python -m unittest tests.test_property_rental_reminders
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

from features.reminders import init_reminders_tables, run_reminders_sweep_for_bot
from templates import property_rental

TENANT_ID = 555


class PropertyRentalRemindersTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self._tmp.name) / "reminders_bot.db")
        await property_rental.init_db(self.db_path)
        # reminder_log is normally created by runtime/registry.py's
        # _load_and_include_features() when "reminders" is enabled for a
        # given bot (see that function's own init_db(db_path) call site) —
        # property_rental.py's own init_db does NOT create it (same as
        # car_rental.py, the only other reminders_config-bearing template),
        # so tests exercising the sweep directly need it created explicitly.
        await init_reminders_tables(self.db_path)

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def _seed_lease_and_payment(self, due_date: str) -> None:
        import aiosqlite

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO properties (address, status) VALUES ('Тестовый объект', 'occupied')"
            )
            await db.execute(
                "INSERT INTO leases (property_id, tenant_user_id, tenant_name, start_date, end_date, "
                "monthly_amount, status) VALUES (1, ?, 'Иван', '2026-01-01', '2027-01-01', 50000, 'active')",
                (TENANT_ID,),
            )
            await db.execute(
                "INSERT INTO rent_payments (lease_id, period, amount, due_date, status) "
                "VALUES (1, '2026-09', 50000, ?, 'pending')",
                (due_date,),
            )
            await db.commit()

    async def test_reminder_fires_at_configured_offset(self):
        offset_hours = property_rental.REMINDER_DAYS_BEFORE_RENT * 24
        now = datetime(2026, 9, 1, 12, 0, 0)
        due = (now + timedelta(hours=offset_hours)).strftime("%Y-%m-%d")
        await self._seed_lease_and_payment(due)

        bot = AsyncMock()
        sent = await run_reminders_sweep_for_bot(bot, self.db_path, property_rental.reminders_config, now=now)

        self.assertEqual(sent, 1)
        bot.send_message.assert_awaited_once()
        args, kwargs = bot.send_message.await_args
        self.assertEqual(args[0], TENANT_ID)
        self.assertIn("50000", args[1])

    async def test_reminder_does_not_fire_far_from_due_date(self):
        now = datetime(2026, 9, 1, 12, 0, 0)
        due = (now + timedelta(days=30)).strftime("%Y-%m-%d")  # far outside the offset window
        await self._seed_lease_and_payment(due)

        bot = AsyncMock()
        sent = await run_reminders_sweep_for_bot(bot, self.db_path, property_rental.reminders_config, now=now)
        self.assertEqual(sent, 0)
        bot.send_message.assert_not_awaited()

    async def test_reminder_skips_already_paid_payment(self):
        import aiosqlite

        offset_hours = property_rental.REMINDER_DAYS_BEFORE_RENT * 24
        now = datetime(2026, 9, 1, 12, 0, 0)
        due = (now + timedelta(hours=offset_hours)).strftime("%Y-%m-%d")
        await self._seed_lease_and_payment(due)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("UPDATE rent_payments SET status='paid' WHERE id=1")
            await db.commit()

        bot = AsyncMock()
        sent = await run_reminders_sweep_for_bot(bot, self.db_path, property_rental.reminders_config, now=now)
        self.assertEqual(sent, 0, "active_field='pending' should exclude an already-paid payment")


if __name__ == "__main__":
    unittest.main()
