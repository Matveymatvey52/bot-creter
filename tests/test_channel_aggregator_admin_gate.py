"""Security fix test for templates/channel_aggregator.py — missing admin
bootstrap bug (BUG 5 of the "первый /start становится админом навсегда" fix
set — the inverse case here).

This template reuses features/channel_monitor.py's router AS-IS, including
its _is_bot_admin() gate on every handler (checks config.admins_file), but
its own cmd_start used to never call _save_admins at all. The admins file
stayed permanently empty, so _is_bot_admin returned False for EVERYONE
including the real owner — the monitoring button was visible but
functionally dead for the whole bot.

Fixed by adding the same owner_telegram_id bootstrap pattern used by every
other template: only bots.owner_telegram_id (when known) may claim the
empty-admins slot on first /start, and the grant syncs into the central
bot_admins table.

No real Telegram network calls, no real tokens, no Telethon.

Run with: python -m unittest tests.test_channel_aggregator_admin_gate
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

import db.database as db_module
from features.channel_monitor import _is_bot_admin
from runtime.registry import get_template_router
from templates import channel_aggregator

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


def _build_bot_dispatcher(config: channel_aggregator.ChannelAggregatorConfig) -> tuple[Bot, Dispatcher]:
    bot = Bot(token=FAKE_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(channel_aggregator.ConfigMiddleware(config))
    dp.include_router(get_template_router("channel_aggregator"))
    return bot, dp


class ChannelAggregatorAdminGateTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_start_actually_bootstraps_an_admin(self):
        """The core regression: before the fix, admins_file stayed empty
        forever and _is_bot_admin was False for everyone, always."""
        config = channel_aggregator.config_from_bot_row(
            {"bot_id": 995, "name": "chan_agg_bootstrap", "display_name": None}, self.data_dir
        )
        bot, dp = _build_bot_dispatcher(config)

        SENDER_ID = 606060
        await dp.feed_webhook_update(bot, _text_update(1, SENDER_ID, "/start"))

        self.assertEqual(channel_aggregator._load_admins(config.admins_file), {str(SENDER_ID)})
        self.assertTrue(_is_bot_admin(SENDER_ID, config))

    async def test_non_owner_messaging_first_does_not_become_admin(self):
        config = channel_aggregator.config_from_bot_row(
            {"bot_id": 996, "name": "chan_agg_owned", "display_name": None, "owner_telegram_id": 12345},
            self.data_dir,
        )
        bot, dp = _build_bot_dispatcher(config)

        CLIENT_ID = 555
        await dp.feed_webhook_update(bot, _text_update(1, CLIENT_ID, "/start"))
        self.assertEqual(channel_aggregator._load_admins(config.admins_file), set())
        self.assertFalse(_is_bot_admin(CLIENT_ID, config))

        await dp.feed_webhook_update(bot, _text_update(2, 12345, "/start"))
        self.assertTrue(_is_bot_admin(12345, config))
        self.assertEqual(channel_aggregator._load_admins(config.admins_file), {"12345"})

    async def test_bootstrap_admin_syncs_to_central_bot_admins_table(self):
        config = channel_aggregator.config_from_bot_row(
            {"bot_id": 997, "name": "chan_agg_synced", "display_name": None}, self.data_dir
        )
        bot, dp = _build_bot_dispatcher(config)
        await dp.feed_webhook_update(bot, _text_update(1, 321, "/start"))

        central_admins = await db_module.get_bot_admins(997)
        self.assertEqual(central_admins, ["321"])


if __name__ == "__main__":
    unittest.main()
