"""setChatMenuButton automation — replaces the manual @BotFather /setmenubutton
step. Covers:

- runtime/webhook_setup.build_miniapp_url / set_miniapp_menu_button / reset_menu_button
  (unit tests, mocking aiogram's Bot.set_chat_menu_button — no real Telegram calls).
- Wiring: handlers/create_bot._activate_new_bot already covered in
  tests/test_auto_webhook_registration.py; this file covers the two regeneration
  paths (handlers/custom_features._regenerate_miniapp_config_after_custom_feature
  and handlers/manage_bots' recreate-bot callback) that must reset the Menu
  Button when a miniapp_config is (re)generated for an already-existing bot.
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import AsyncMock, patch

from aiogram.types import MenuButtonDefault, MenuButtonWebApp

import handlers.custom_features as custom_features_module
from runtime.webhook_setup import build_miniapp_url, reset_menu_button, set_miniapp_menu_button

FAKE_TOKEN = "987654321:AAHnotRealButLongEnoughToLookLikeAToken"
BOT_ID = 42
BASE_URL = "https://bot-creter-production.up.railway.app"


class BuildMiniappUrl(unittest.TestCase):
    def test_matches_app_bot_id_pattern(self):
        self.assertEqual(build_miniapp_url(BASE_URL, BOT_ID), f"{BASE_URL}/app/{BOT_ID}")

    def test_strips_trailing_slash_on_base_url(self):
        self.assertEqual(build_miniapp_url(f"{BASE_URL}/", BOT_ID), f"{BASE_URL}/app/{BOT_ID}")


class SetMiniappMenuButton(unittest.IsolatedAsyncioTestCase):
    async def test_calls_set_chat_menu_button_with_web_app_pointing_at_own_miniapp(self):
        mock_bot = AsyncMock()
        mock_bot.session.close = AsyncMock()
        with patch("runtime.webhook_setup.Bot", return_value=mock_bot) as bot_cls:
            await set_miniapp_menu_button(FAKE_TOKEN, BASE_URL, BOT_ID)

        bot_cls.assert_called_once_with(token=FAKE_TOKEN)
        mock_bot.set_chat_menu_button.assert_awaited_once()
        kwargs = mock_bot.set_chat_menu_button.await_args.kwargs
        menu_button = kwargs["menu_button"]
        self.assertIsInstance(menu_button, MenuButtonWebApp)
        self.assertEqual(menu_button.web_app.url, f"{BASE_URL}/app/{BOT_ID}")
        mock_bot.session.close.assert_awaited_once()

    async def test_closes_session_even_if_telegram_call_raises(self):
        mock_bot = AsyncMock()
        mock_bot.session.close = AsyncMock()
        mock_bot.set_chat_menu_button.side_effect = RuntimeError("Telegram unreachable")
        with patch("runtime.webhook_setup.Bot", return_value=mock_bot):
            with self.assertRaises(RuntimeError):
                await set_miniapp_menu_button(FAKE_TOKEN, BASE_URL, BOT_ID)
        mock_bot.session.close.assert_awaited_once()


class ResetMenuButton(unittest.IsolatedAsyncioTestCase):
    async def test_calls_set_chat_menu_button_with_default(self):
        mock_bot = AsyncMock()
        mock_bot.session.close = AsyncMock()
        with patch("runtime.webhook_setup.Bot", return_value=mock_bot) as bot_cls:
            await reset_menu_button(FAKE_TOKEN)

        bot_cls.assert_called_once_with(token=FAKE_TOKEN)
        mock_bot.set_chat_menu_button.assert_awaited_once()
        kwargs = mock_bot.set_chat_menu_button.await_args.kwargs
        self.assertIsInstance(kwargs["menu_button"], MenuButtonDefault)


class RegenerateMiniappConfigResetsMenuButton(unittest.IsolatedAsyncioTestCase):
    """handlers/custom_features._regenerate_miniapp_config_after_custom_feature
    is the "regenerate for an already-existing bot" path — after it persists a
    new miniapp_config, it must also (re)point the bot's Menu Button at it."""

    def _patch(self, generated_config, bot_row):
        self._gen = patch.object(
            custom_features_module, "_generate_miniapp_config",
            new=AsyncMock(return_value=(generated_config, None)),
        )
        self._set_cfg = patch.object(custom_features_module, "set_bot_miniapp_config", new=AsyncMock())
        self._get_bot = patch.object(custom_features_module, "get_bot", new=AsyncMock(return_value=bot_row))
        self._set_menu = patch.object(custom_features_module, "set_miniapp_menu_button", new=AsyncMock())
        mocks = (self._gen.start(), self._set_cfg.start(), self._get_bot.start(), self._set_menu.start())
        for p in (self._gen, self._set_cfg, self._get_bot, self._set_menu):
            self.addCleanup(p.stop)
        return mocks

    async def test_resets_menu_button_when_config_generated_and_base_url_set(self):
        _, set_cfg, _, set_menu = self._patch(
            generated_config={"routes": []}, bot_row={"token": FAKE_TOKEN}
        )
        with patch.dict(os.environ, {"PUBLIC_BASE_URL": BASE_URL}):
            await custom_features_module._regenerate_miniapp_config_after_custom_feature(
                BOT_ID, "main code", "patch code", "add a widget"
            )

        set_cfg.assert_awaited_once_with(BOT_ID, {"routes": []})
        set_menu.assert_awaited_once_with(FAKE_TOKEN, BASE_URL, BOT_ID)

    async def test_skips_menu_button_when_no_config_generated(self):
        _, set_cfg, _, set_menu = self._patch(generated_config=None, bot_row={"token": FAKE_TOKEN})
        with patch.dict(os.environ, {"PUBLIC_BASE_URL": BASE_URL}):
            await custom_features_module._regenerate_miniapp_config_after_custom_feature(
                BOT_ID, "main code", "patch code", "add a widget"
            )
        set_cfg.assert_not_called()
        set_menu.assert_not_called()

    async def test_skips_menu_button_when_no_base_url(self):
        _, set_cfg, _, set_menu = self._patch(
            generated_config={"routes": []}, bot_row={"token": FAKE_TOKEN}
        )
        env_without_base = {k: v for k, v in os.environ.items() if k != "PUBLIC_BASE_URL"}
        with patch.dict(os.environ, env_without_base, clear=True):
            await custom_features_module._regenerate_miniapp_config_after_custom_feature(
                BOT_ID, "main code", "patch code", "add a widget"
            )
        set_cfg.assert_awaited_once()
        set_menu.assert_not_called()

    async def test_never_raises_even_if_menu_button_call_fails(self):
        """The function's contract (see its docstring) is that it never raises —
        it's fired off fire-and-forget with nothing awaiting it."""
        _, set_cfg, _, set_menu = self._patch(
            generated_config={"routes": []}, bot_row={"token": FAKE_TOKEN}
        )
        set_menu.side_effect = RuntimeError("Telegram unreachable")
        with patch.dict(os.environ, {"PUBLIC_BASE_URL": BASE_URL}):
            await custom_features_module._regenerate_miniapp_config_after_custom_feature(
                BOT_ID, "main code", "patch code", "add a widget"
            )
        set_cfg.assert_awaited_once()
        set_menu.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
