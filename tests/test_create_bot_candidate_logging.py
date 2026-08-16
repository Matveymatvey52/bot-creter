"""handlers/create_bot.py's wiring of fallback_info into
db.database.add_template_candidate (docs/TEMPLATE_CANDIDATE_LOGGING_DESIGN.md).

Exercises auto_launch_managed_bot directly with a controlled _pending entry,
mocking every heavy side effect (Telegram API, subprocess launch, registry
mutation, GitHub push) the same way tests/test_factory_registry_citizen.py
does, plus the DB layer to assert exactly what auto_launch_managed_bot
passes to add_template_candidate once the real bot_id exists — this is the
whole point of the design's "record with a real bot_id" decision.
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import handlers.create_bot as create_bot_module

CREATOR_USER_ID = 555
NEW_BOT_ID = 987654321


class AutoLaunchLogsTemplateCandidateWhenFallbackOccurred(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        create_bot_module._pending[CREATOR_USER_ID] = {
            "chat_id": 111,
            "code": "print('hi')\nasyncio.run(main())",
            "name": "my_novel_bot",
            "summary": "хочу бота для планирования смен курьеров по этажам",
            "display_name": "",
            "miniapp_config": None,
            "office_hook_config": None,
            "fallback_info": {"reason": "no_template_match", "selected_templates": []},
        }
        self._patches = [
            patch.object(create_bot_module, "get_managed_bot_token", AsyncMock(return_value="123:faketoken")),
            patch.object(create_bot_module, "create_bot_record_with_admins", AsyncMock(return_value=42)),
            patch.object(create_bot_module, "add_template_candidate", AsyncMock()),
            patch.object(create_bot_module, "set_bot_miniapp_config", AsyncMock()),
            patch.object(create_bot_module, "set_bot_office_hook_config", AsyncMock()),
            patch.object(create_bot_module, "set_bot_display_name", AsyncMock()),
            patch.object(create_bot_module, "_register_new_bot_in_registry", AsyncMock()),
            patch.object(create_bot_module, "_activate_new_bot", AsyncMock(return_value=MagicMock())),
            patch.object(create_bot_module, "_notify_bot_created", AsyncMock()),
            patch.object(create_bot_module, "_clear_user_fsm", AsyncMock()),
            patch.object(create_bot_module, "push_bot_to_github", MagicMock()),
            patch.object(create_bot_module.asyncio, "create_task", MagicMock()),
        ]
        self.mocks = {p.attribute: p.start() for p in self._patches}
        self.addAsyncCleanup(self._stop_patches)

    async def _stop_patches(self):
        for p in self._patches:
            p.stop()
        create_bot_module._pending.pop(CREATOR_USER_ID, None)

    async def test_fallback_info_is_persisted_with_the_real_bot_id(self):
        fake_bot = MagicMock()
        fake_bot.send_message = AsyncMock()

        with patch("handlers.create_bot.Bot") as fake_bot_cls:
            fake_bot_cls.return_value.__aenter__ = AsyncMock(
                return_value=MagicMock(get_me=AsyncMock(return_value=MagicMock(username="new_bot_username")))
            )
            fake_bot_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            await create_bot_module.auto_launch_managed_bot(
                {"bot_id": NEW_BOT_ID, "user_id": CREATOR_USER_ID}, fake_bot,
            )

        self.mocks["add_template_candidate"].assert_awaited_once_with(
            creator_user_id=CREATOR_USER_ID,
            summary="хочу бота для планирования смен курьеров по этажам",
            fallback_reason="no_template_match",
            selected_templates=[],
            bot_name="my_novel_bot",
            bot_id=42,
        )

    async def test_no_candidate_logged_when_fallback_info_is_none(self):
        create_bot_module._pending[CREATOR_USER_ID]["fallback_info"] = None
        fake_bot = MagicMock()
        fake_bot.send_message = AsyncMock()

        with patch("handlers.create_bot.Bot") as fake_bot_cls:
            fake_bot_cls.return_value.__aenter__ = AsyncMock(
                return_value=MagicMock(get_me=AsyncMock(return_value=MagicMock(username="new_bot_username")))
            )
            fake_bot_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            await create_bot_module.auto_launch_managed_bot(
                {"bot_id": NEW_BOT_ID, "user_id": CREATOR_USER_ID}, fake_bot,
            )

        self.mocks["add_template_candidate"].assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
