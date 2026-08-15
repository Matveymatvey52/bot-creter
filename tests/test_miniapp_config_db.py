"""db/database.py's bot_miniapp_config table — set_bot_miniapp_config/
get_bot_miniapp_config/delete_bot_miniapp_config (see docs/MINIAPP_DESIGN.md
§6). One row per bot (bot_id PRIMARY KEY), same upsert shape as
bot_sheets_config, storing a JSON blob rather than fixed columns.
"""
from __future__ import annotations

import unittest

from db.database import (
    create_bot_record_with_admins,
    delete_bot,
    delete_bot_miniapp_config,
    get_bot_miniapp_config,
    init_db,
    set_bot_miniapp_config,
)

FAKE_TOKEN = "123456789:AAHfakeTokenButShapedRight1234567890"

SAMPLE_CONFIG = {
    "resources": [
        {
            "name": "tours",
            "table": "tours",
            "order_by": "created_at DESC",
            "creatable": True,
            "fields": [
                {"name": "name", "required": True},
                {"name": "status"},
            ],
        },
    ],
}


class MiniappConfigDbTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Idempotent (CREATE TABLE IF NOT EXISTS) — same pattern as
        # test_custom_feature_db.py, self-sufficient regardless of prior runs.
        await init_db()
        self.bot_id = await create_bot_record_with_admins(
            name="miniapp_cfg_db_test_bot", description="test", token=FAKE_TOKEN,
            file_path="templates/inventory.py", admin_ids=["1"],
        )

    async def asyncTearDown(self):
        await delete_bot(self.bot_id)

    async def test_no_config_for_a_bot_that_never_got_one(self):
        self.assertIsNone(await get_bot_miniapp_config(self.bot_id))

    async def test_set_then_get_round_trips_the_dict(self):
        await set_bot_miniapp_config(self.bot_id, SAMPLE_CONFIG)
        stored = await get_bot_miniapp_config(self.bot_id)
        self.assertEqual(stored, SAMPLE_CONFIG)

    async def test_set_twice_upserts_rather_than_erroring(self):
        await set_bot_miniapp_config(self.bot_id, SAMPLE_CONFIG)
        updated = {"resources": []}
        await set_bot_miniapp_config(self.bot_id, updated)
        self.assertEqual(await get_bot_miniapp_config(self.bot_id), updated)

    async def test_delete_removes_the_row(self):
        await set_bot_miniapp_config(self.bot_id, SAMPLE_CONFIG)
        await delete_bot_miniapp_config(self.bot_id)
        self.assertIsNone(await get_bot_miniapp_config(self.bot_id))

    async def test_delete_bot_also_cleans_up_miniapp_config(self):
        await set_bot_miniapp_config(self.bot_id, SAMPLE_CONFIG)
        await delete_bot(self.bot_id)
        self.assertIsNone(await get_bot_miniapp_config(self.bot_id))
        # asyncTearDown will call delete_bot again — harmless no-op on an
        # already-deleted row/bot, matches delete_bot's idempotent DELETE.


if __name__ == "__main__":
    unittest.main()
