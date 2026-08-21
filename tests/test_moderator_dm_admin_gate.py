"""Security fix test for templates/moderator.py — DM admin-panel bootstrap
bug (BUG 6 of the "первый /start становится админом навсегда" fix set).

Group moderation itself (mute/ban/warn) is untouched and stays safe — it's
gated by a LIVE Telegram admin-status check (_is_group_admin →
bot.get_chat_member), never by admins_file/bot_admins. This file is only
about the separate private-chat (DM) admin panel:

1. _claim_first_bot_admin was already race-free (BEGIN IMMEDIATE — two
   people /start-ing the empty bot at once can't both win), but "whoever
   DMs the bot first" was still the rule. A client who messages the bot
   before its owner does could permanently seize bot-admin (journal access,
   /addadmin, /removeadmin). Fixed with the same owner_telegram_id pattern
   used by every other template: only bots.owner_telegram_id (when known)
   may attempt the atomic claim.
2. kb_start_menu()'s "👥 Админы" / "📜 Журнал модерации" buttons were shown
   to EVERY private-chat user unconditionally — the handlers behind them
   already re-checked _is_bot_admin, but the buttons were visible/tappable
   first. kb_start_menu() now takes is_admin and hides those two buttons
   for non-admins (⚙️ Настроить группу stays visible — its own gating is a
   live per-group admin check done later in that flow).

No real Telegram network calls, no real tokens.

Run with: python -m unittest tests.test_moderator_dm_admin_gate
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from runtime.registry import get_template_router
from templates import moderator

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


def _build_bot_dispatcher(config: moderator.ModeratorConfig) -> tuple[Bot, Dispatcher]:
    bot = Bot(token=FAKE_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(moderator.ConfigMiddleware(config))
    dp.include_router(get_template_router("moderator"))
    return bot, dp


class ModeratorDmAdminGateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._mock_call = AsyncMock(return_value=MagicMock())
        self._bot_call_patcher = patch.object(Bot, "__call__", new=self._mock_call)
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_non_owner_dming_first_does_not_become_bot_admin(self):
        config = moderator.config_from_bot_row(
            {"bot_id": 981, "name": "mod_owned", "display_name": None,
             "group_chat_id": None, "owner_telegram_id": 12345},
            self.data_dir,
        )
        await moderator.init_db(config.db_path)
        bot, dp = _build_bot_dispatcher(config)

        CLIENT_ID = 555  # not the owner, DMs first
        await dp.feed_webhook_update(bot, _text_update(1, CLIENT_ID, "/start"))
        self.assertFalse(await moderator._is_bot_admin(CLIENT_ID, config))

        await dp.feed_webhook_update(bot, _text_update(2, 12345, "/start"))
        self.assertTrue(await moderator._is_bot_admin(12345, config))

    async def test_owner_still_claims_bootstrap_when_owner_telegram_id_unset(self):
        """Standalone/env mode fallback: when owner_telegram_id is unknown,
        the first /start sender is still the only option available."""
        config = moderator.config_from_bot_row(
            {"bot_id": 982, "name": "mod_standalone", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await moderator.init_db(config.db_path)
        bot, dp = _build_bot_dispatcher(config)

        await dp.feed_webhook_update(bot, _text_update(1, 4242, "/start"))
        self.assertTrue(await moderator._is_bot_admin(4242, config))

    async def test_admins_and_journal_buttons_hidden_from_non_admin(self):
        config = moderator.config_from_bot_row(
            {"bot_id": 983, "name": "mod_ui", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await moderator.init_db(config.db_path)
        bot, dp = _build_bot_dispatcher(config)

        await dp.feed_webhook_update(bot, _text_update(1, 42, "/start"))  # 42 becomes first bot-admin
        self._mock_call.reset_mock()
        await dp.feed_webhook_update(bot, _text_update(2, 99, "/start"))  # 99 is a plain user

        sent = [
            call.args[0] for call in self._mock_call.call_args_list
            if call.args and hasattr(call.args[0], "reply_markup") and call.args[0].reply_markup
        ]
        found_admins_btn = any(
            btn.text == "👥 Админы"
            for method in sent for row in method.reply_markup.inline_keyboard for btn in row
        )
        found_modlog_btn = any(
            btn.text == "📜 Журнал модерации"
            for method in sent for row in method.reply_markup.inline_keyboard for btn in row
        )
        found_group_btn = any(
            btn.text == "⚙️ Настроить группу"
            for method in sent for row in method.reply_markup.inline_keyboard for btn in row
        )
        self.assertFalse(found_admins_btn)
        self.assertFalse(found_modlog_btn)
        self.assertTrue(found_group_btn)  # not admin-gated — stays visible

    async def test_admins_and_journal_buttons_shown_to_bot_admin(self):
        config = moderator.config_from_bot_row(
            {"bot_id": 984, "name": "mod_ui_admin", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await moderator.init_db(config.db_path)
        bot, dp = _build_bot_dispatcher(config)

        await dp.feed_webhook_update(bot, _text_update(1, 42, "/start"))  # 42 becomes first bot-admin
        sent = [
            call.args[0] for call in self._mock_call.call_args_list
            if call.args and hasattr(call.args[0], "reply_markup") and call.args[0].reply_markup
        ]
        found_admins_btn = any(
            btn.text == "👥 Админы"
            for method in sent for row in method.reply_markup.inline_keyboard for btn in row
        )
        self.assertTrue(found_admins_btn)


if __name__ == "__main__":
    unittest.main()
