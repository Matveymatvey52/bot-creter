"""db/database.py's bot_custom_features append-only history log —
add_custom_feature_record/get_custom_feature_history. bot_id is deliberately
NOT unique/PK (unlike bot_sheets_config), so a bot re-patched multiple times
accumulates one row per apply; get_custom_feature_history must return them
newest-first.
"""
from __future__ import annotations

import unittest

from db.database import (
    add_custom_feature_record,
    create_bot_record_with_admins,
    delete_bot,
    get_custom_feature_history,
    init_db,
)

FAKE_TOKEN = "123456789:AAHfakeTokenButShapedRight1234567890"


class CustomFeatureHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Idempotent (CREATE TABLE IF NOT EXISTS) — makes this file
        # self-sufficient regardless of whether the app has ever been run
        # against this worktree's data/bots.db before. Does not touch the
        # pre-existing real-DB-isolation gap tracked separately (this still
        # writes to the real DB_PATH, same as every other test that hits it).
        await init_db()
        self.bot_id = await create_bot_record_with_admins(
            name="cf_db_test_bot", description="test", token=FAKE_TOKEN,
            file_path="templates/inventory.py", admin_ids=["1"],
        )

    async def asyncTearDown(self):
        await delete_bot(self.bot_id)

    async def test_no_history_for_a_never_patched_bot(self):
        self.assertEqual(await get_custom_feature_history(self.bot_id), [])

    async def test_multiple_records_accumulate_and_are_returned_newest_first(self):
        await add_custom_feature_record(self.bot_id, "первая доработка")
        await add_custom_feature_record(self.bot_id, "вторая доработка")

        history = await get_custom_feature_history(self.bot_id)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["description"], "вторая доработка")
        self.assertEqual(history[1]["description"], "первая доработка")
        self.assertIsNotNone(history[0]["applied_at"])
        self.assertIsNotNone(history[0]["id"])

    async def test_history_is_scoped_to_its_own_bot_id(self):
        other_bot_id = await create_bot_record_with_admins(
            name="cf_db_test_other_bot", description="test", token=FAKE_TOKEN,
            file_path="templates/inventory.py", admin_ids=["1"],
        )
        try:
            await add_custom_feature_record(self.bot_id, "для первого бота")
            await add_custom_feature_record(other_bot_id, "для второго бота")

            self.assertEqual(len(await get_custom_feature_history(self.bot_id)), 1)
            self.assertEqual(len(await get_custom_feature_history(other_bot_id)), 1)
        finally:
            await delete_bot(other_bot_id)


if __name__ == "__main__":
    unittest.main()
