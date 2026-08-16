"""db/database.py's template_candidates append-only log —
add_template_candidate/list_template_candidates (docs/TEMPLATE_CANDIDATE_LOGGING_DESIGN.md).
Covers: bot_id nullability (row logged before the bot record exists),
selected_templates round-tripping through JSON, and newest-first ordering.
"""
from __future__ import annotations

import unittest

from db.database import (
    add_template_candidate,
    create_bot_record_with_admins,
    delete_bot,
    init_db,
    list_template_candidates,
)

FAKE_TOKEN = "123456789:AAHfakeTokenButShapedRight1234567890"


class TemplateCandidateLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Idempotent (CREATE TABLE IF NOT EXISTS) — same real-DB-isolation
        # posture as the rest of this test suite (see backlog memory on
        # test DB isolation); not addressed by this change.
        await init_db()
        self.bot_id = await create_bot_record_with_admins(
            name="tc_db_test_bot", description="test", token=FAKE_TOKEN,
            file_path="templates/inventory.py", admin_ids=["1"],
        )

    async def asyncTearDown(self):
        await delete_bot(self.bot_id)

    async def test_candidate_with_no_bot_id_is_logged_and_listed(self):
        """The intended write path (handlers/create_bot.py) only has a real
        bot_id AFTER the bot record is created — bot_id must be optional so
        a from-scratch generation whose bot creation later fails is still
        captured."""
        await add_template_candidate(
            creator_user_id=555,
            summary="хочу бота для учёта смен курьеров",
            fallback_reason="no_template_match",
            selected_templates=[],
        )
        rows = await list_template_candidates()
        row = next(r for r in rows if r["summary"] == "хочу бота для учёта смен курьеров")
        self.assertIsNone(row["bot_id"])
        self.assertEqual(row["fallback_reason"], "no_template_match")
        self.assertEqual(row["selected_templates"], [])
        self.assertIsNone(row["bot_type"])
        self.assertIsNone(row["bot_name"])
        self.assertIsNotNone(row["created_at"])

    async def test_selected_templates_round_trips_through_json(self):
        await add_template_candidate(
            creator_user_id=555,
            summary="магазин с реферальной программой лояльности",
            fallback_reason="synthesis_failed",
            selected_templates=["shop_catalog", "referral_program"],
            bot_type="ecommerce",
            bot_name="loyalty_shop",
            bot_id=self.bot_id,
        )
        rows = await list_template_candidates()
        row = next(r for r in rows if r["summary"] == "магазин с реферальной программой лояльности")
        self.assertEqual(row["bot_id"], self.bot_id)
        self.assertEqual(row["selected_templates"], ["shop_catalog", "referral_program"])
        self.assertEqual(row["bot_type"], "ecommerce")
        self.assertEqual(row["bot_name"], "loyalty_shop")

    async def test_listed_newest_first(self):
        await add_template_candidate(
            creator_user_id=555, summary="первый кандидат — старый",
            fallback_reason="no_template_match", selected_templates=[],
        )
        await add_template_candidate(
            creator_user_id=555, summary="второй кандидат — новый",
            fallback_reason="no_template_match", selected_templates=[],
        )
        rows = await list_template_candidates()
        summaries = [r["summary"] for r in rows]
        self.assertLess(
            summaries.index("второй кандидат — новый"),
            summaries.index("первый кандидат — старый"),
        )


if __name__ == "__main__":
    unittest.main()
