"""Security fix test for templates/campaign_tracker.py — bootstrap admin bug
(BUG 2 of the "первый /start становится админом навсегда" fix set).

This bot is entirely team-only: every button in kb_main() (➕ Новая
кампания, 📋 Кампании, 📊 Сравнение) was already admin-gated in its own
handler, but the keyboard containing them was sent unconditionally to
EVERYONE — the only "protection" for a non-admin was a text warning sent
AFTER the keyboard, which is not a real gate since the buttons were already
visible and tappable. kb_main() now takes is_admin and returns an empty
keyboard for non-admins.

Also fixed: whoever sent /start FIRST used to permanently become admin —
now only bots.owner_telegram_id (when known) may claim the empty-admins
bootstrap slot, the DB-known owner is always-admin regardless of
admins_file state, and /addadmin, /removeadmin sync into the central
bot_admins table.

No real Telegram network calls, no real tokens.

Run with: python -m unittest tests.test_campaign_tracker_admin_gate
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import SendMessage, SendPhoto

import db.database as db_module
from runtime.registry import get_template_router
from templates import campaign_tracker

FAKE_TOKEN = "123456:test-token-not-real"


class FakeBotAPI:
    def __init__(self):
        self.calls: list = []

    async def __call__(self, request, **kwargs):
        self.calls.append(request)
        return object()


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


def _build_bot_dispatcher(config: campaign_tracker.CampaignTrackerConfig) -> tuple[Bot, Dispatcher]:
    bot = Bot(token=FAKE_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(campaign_tracker.ConfigMiddleware(config))
    dp.include_router(get_template_router("campaign_tracker"))
    return bot, dp


class CampaignTrackerAdminGateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._fake_api = FakeBotAPI()
        self._bot_call_patcher = patch.object(Bot, "__call__", new=self._fake_api)
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

    async def test_non_owner_messaging_first_does_not_become_admin(self):
        config = campaign_tracker.config_from_bot_row(
            {"bot_id": 951, "name": "camp_owned", "display_name": None,
             "group_chat_id": None, "owner_telegram_id": 12345},
            self.data_dir,
        )
        await campaign_tracker.init_db(config.db_path)
        bot, dp = _build_bot_dispatcher(config)

        CLIENT_ID = 555
        await dp.feed_webhook_update(bot, _text_update(1, CLIENT_ID, "/start"))
        self.assertEqual(campaign_tracker._load_admins(config.admins_file), set())
        self.assertFalse(campaign_tracker._is_admin(CLIENT_ID, config))

        await dp.feed_webhook_update(bot, _text_update(2, 12345, "/start"))
        self.assertTrue(campaign_tracker._is_admin(12345, config))
        self.assertEqual(campaign_tracker._load_admins(config.admins_file), {"12345"})

    async def test_owner_is_always_admin_even_with_stale_admins_file(self):
        config = campaign_tracker.config_from_bot_row(
            {"bot_id": 952, "name": "camp_owned_2", "display_name": None,
             "group_chat_id": None, "owner_telegram_id": 777},
            self.data_dir,
        )
        await campaign_tracker.init_db(config.db_path)
        campaign_tracker._save_admins(config.admins_file, {"999999"})
        self.assertTrue(campaign_tracker._is_admin(777, config))
        self.assertTrue(campaign_tracker._is_admin(999999, config))
        self.assertFalse(campaign_tracker._is_admin(4242, config))

    async def test_bootstrap_admin_syncs_to_central_bot_admins_table(self):
        config = campaign_tracker.config_from_bot_row(
            {"bot_id": 953, "name": "camp_synced", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await campaign_tracker.init_db(config.db_path)
        bot, dp = _build_bot_dispatcher(config)
        await dp.feed_webhook_update(bot, _text_update(1, 321, "/start"))

        central_admins = await db_module.get_bot_admins(953)
        self.assertEqual(central_admins, ["321"])

    async def test_non_admin_gets_empty_keyboard(self):
        config = campaign_tracker.config_from_bot_row(
            {"bot_id": 954, "name": "camp_ui", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await campaign_tracker.init_db(config.db_path)
        bot, dp = _build_bot_dispatcher(config)

        await dp.feed_webhook_update(bot, _text_update(1, 42, "/start"))  # 42 becomes first admin
        self._fake_api.calls.clear()
        await dp.feed_webhook_update(bot, _text_update(2, 99, "/start"))  # 99 is a plain user

        sends = [c for c in self._fake_api.calls if isinstance(c, (SendMessage, SendPhoto))]
        self.assertTrue(sends)
        for req in sends:
            markup = getattr(req, "reply_markup", None)
            if markup is not None and hasattr(markup, "keyboard"):
                self.assertEqual(markup.keyboard, [])

    async def test_admin_gets_full_keyboard(self):
        config = campaign_tracker.config_from_bot_row(
            {"bot_id": 955, "name": "camp_ui_admin", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        await campaign_tracker.init_db(config.db_path)
        bot, dp = _build_bot_dispatcher(config)

        await dp.feed_webhook_update(bot, _text_update(1, 42, "/start"))  # 42 becomes first admin

        sends = [c for c in self._fake_api.calls if isinstance(c, (SendMessage, SendPhoto))]
        found_new_campaign = any(
            hasattr(req, "reply_markup") and req.reply_markup and hasattr(req.reply_markup, "keyboard")
            and any(btn.text == "➕ Новая кампания" for row in req.reply_markup.keyboard for btn in row)
            for req in sends
        )
        self.assertTrue(found_new_campaign)


if __name__ == "__main__":
    unittest.main()
