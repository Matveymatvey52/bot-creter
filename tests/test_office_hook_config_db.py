"""db/database.py's bot_office_hook_config table — set_bot_office_hook_config/
get_bot_office_hook_config/delete_bot_office_hook_config (see
docs/OFFICES_DESIGN.md §11). Same upsert shape as bot_miniapp_config, one row
per bot, storing a small JSON blob rather than fixed columns.
"""
from __future__ import annotations

import unittest

from db.database import (
    create_bot_record_with_admins,
    delete_bot,
    delete_bot_office_hook_config,
    get_bot_office_hook_config,
    init_db,
    set_bot_office_hook_config,
)

FAKE_TOKEN = "123456789:AAHfakeTokenButShapedRight1234567890"

SAMPLE_CONFIG = {"table": "clients", "match_field": "telegram_id"}


class OfficeHookConfigDbTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await init_db()
        self.bot_id = await create_bot_record_with_admins(
            name="office_hook_cfg_db_test_bot", description="test", token=FAKE_TOKEN,
            file_path="templates/inventory.py", admin_ids=["1"],
        )

    async def asyncTearDown(self):
        await delete_bot(self.bot_id)

    async def test_no_config_for_a_bot_that_never_got_one(self):
        self.assertIsNone(await get_bot_office_hook_config(self.bot_id))

    async def test_set_then_get_round_trips_the_dict(self):
        await set_bot_office_hook_config(self.bot_id, SAMPLE_CONFIG)
        stored = await get_bot_office_hook_config(self.bot_id)
        self.assertEqual(stored, SAMPLE_CONFIG)

    async def test_set_twice_upserts_rather_than_erroring(self):
        await set_bot_office_hook_config(self.bot_id, SAMPLE_CONFIG)
        updated = {"table": "orders", "match_field": None}
        await set_bot_office_hook_config(self.bot_id, updated)
        self.assertEqual(await get_bot_office_hook_config(self.bot_id), updated)

    async def test_delete_removes_the_row(self):
        await set_bot_office_hook_config(self.bot_id, SAMPLE_CONFIG)
        await delete_bot_office_hook_config(self.bot_id)
        self.assertIsNone(await get_bot_office_hook_config(self.bot_id))

    async def test_delete_bot_also_cleans_up_office_hook_config(self):
        await set_bot_office_hook_config(self.bot_id, SAMPLE_CONFIG)
        await delete_bot(self.bot_id)
        self.assertIsNone(await get_bot_office_hook_config(self.bot_id))


if __name__ == "__main__":
    unittest.main()
