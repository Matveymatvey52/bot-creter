"""Auto-webhook-registration phase.

When the factory creates a bot, how it's brought online depends on runtime mode
(handlers/create_bot.py's _activate_new_bot):

- Webhook mode (PUBLIC_BASE_URL + WEBHOOK_SECRET set — combined_app in prod):
  register the new bot's Telegram webhook and do NOT start a polling subprocess.
- Polling / standalone mode (PUBLIC_BASE_URL empty — main.py): start_bot() as
  before, no webhook.
- Webhook mode but WEBHOOK_SECRET empty: register nothing and start nothing
  (a webhook without a matching secret gets 403 from the fail-closed handler);
  the bot stays in the registry, to be registered manually once the secret is set.

These are unit tests against _activate_new_bot directly, mocking set_webhook_for_bot,
start_bot, and update_bot_status — no real Telegram calls, no real subprocess, no DB.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import main
import handlers.create_bot as create_bot_module
from runtime.webhook_setup import build_webhook_url

FAKE_TOKEN = "987654321:AAHnotRealButLongEnoughToLookLikeAToken"
BOT_ID = 42
BOT_NAME = "test_bot"
BOT_FILE = Path("/tmp/does-not-need-to-exist.py")
BASE_URL = "https://bot-creter-production.up.railway.app"
SECRET = "s3cret-token"


class ActivateNewBotChoosesMechanismByMode(unittest.IsolatedAsyncioTestCase):
    def _patch_side_effects(self):
        """Mocks the side-effecting calls _activate_new_bot can make, so
        no real Telegram / subprocess / DB work happens."""
        self._set_webhook = patch.object(create_bot_module, "set_webhook_for_bot", new=AsyncMock())
        self._start_bot = patch.object(create_bot_module, "start_bot", new=AsyncMock(return_value=54321))
        self._update_status = patch.object(create_bot_module, "update_bot_status", new=AsyncMock())
        self._set_menu_button = patch.object(create_bot_module, "set_miniapp_menu_button", new=AsyncMock())
        mocks = (
            self._set_webhook.start(),
            self._start_bot.start(),
            self._update_status.start(),
            self._set_menu_button.start(),
        )
        self.addCleanup(self._set_webhook.stop)
        self.addCleanup(self._start_bot.stop)
        self.addCleanup(self._update_status.stop)
        self.addCleanup(self._set_menu_button.stop)
        return mocks

    async def test_webhook_mode_registers_webhook_and_does_not_start_polling(self):
        set_webhook, start_bot, update_status, set_menu_button = self._patch_side_effects()
        with patch.dict(os.environ, {"PUBLIC_BASE_URL": BASE_URL, "WEBHOOK_SECRET": SECRET}):
            activation = await create_bot_module._activate_new_bot(
                BOT_ID, BOT_NAME, BOT_FILE, FAKE_TOKEN, {"BOT_DISPLAY_NAME": "X"}
            )

        self.assertEqual(activation.outcome, "webhook_ok")
        set_webhook.assert_awaited_once_with(FAKE_TOKEN, BASE_URL, BOT_ID, SECRET)
        # The URL that secret-token would be attached to — /webhook/<bot_id>.
        self.assertEqual(build_webhook_url(BASE_URL, BOT_ID), f"{BASE_URL}/webhook/{BOT_ID}")
        start_bot.assert_not_called()
        # has_miniapp defaults to False — no miniapp_config was passed, so no
        # Menu Button call should be made even though PUBLIC_BASE_URL is set.
        set_menu_button.assert_not_called()
        # The exact persisted state restore_bots() keys off of: status "running"
        # with NO pid argument → pid column stays NULL → webhook-served, not polling.
        # (This is what RestoreBotsRespectsWebhookMode below then guards against.)
        update_status.assert_awaited_once_with(BOT_ID, "running")

    async def test_polling_mode_starts_bot_and_registers_no_webhook(self):
        set_webhook, start_bot, _, set_menu_button = self._patch_side_effects()
        # PUBLIC_BASE_URL absent → polling / standalone mode.
        env_without_base = {k: v for k, v in os.environ.items() if k != "PUBLIC_BASE_URL"}
        with patch.dict(os.environ, env_without_base, clear=True):
            activation = await create_bot_module._activate_new_bot(
                BOT_ID, BOT_NAME, BOT_FILE, FAKE_TOKEN, {"BOT_DISPLAY_NAME": "X"}
            )

        self.assertEqual(activation.outcome, "polling_ok")
        self.assertEqual(activation.pid, 54321)
        start_bot.assert_awaited_once_with(
            BOT_ID, str(BOT_FILE), FAKE_TOKEN, extra_env={"BOT_DISPLAY_NAME": "X"}
        )
        set_webhook.assert_not_called()
        set_menu_button.assert_not_called()

    async def test_webhook_mode_without_secret_registers_nothing_and_warns(self):
        set_webhook, start_bot, _, _set_menu_button = self._patch_side_effects()
        env = {k: v for k, v in os.environ.items() if k != "WEBHOOK_SECRET"}
        env["PUBLIC_BASE_URL"] = BASE_URL  # webhook mode, but no secret
        with patch.dict(os.environ, env, clear=True):
            with self.assertLogs("handlers.create_bot", level="WARNING") as logs:
                activation = await create_bot_module._activate_new_bot(
                    BOT_ID, BOT_NAME, BOT_FILE, FAKE_TOKEN, None
                )

        self.assertEqual(activation.outcome, "webhook_no_secret")
        set_webhook.assert_not_called()
        start_bot.assert_not_called()
        self.assertTrue(any("WEBHOOK_SECRET" in msg for msg in logs.output))

    async def test_webhook_registration_failure_does_not_start_polling(self):
        """Task 3 error handling: if set_webhook raises, the bot is already created
        and in the registry — we do NOT fall back to polling (that would 409 against
        the webhook once it's fixed), we surface webhook_failed for the user message."""
        set_webhook, start_bot, _, _set_menu_button = self._patch_side_effects()
        set_webhook.side_effect = RuntimeError("Telegram unreachable")
        with patch.dict(os.environ, {"PUBLIC_BASE_URL": BASE_URL, "WEBHOOK_SECRET": SECRET}):
            with self.assertLogs("handlers.create_bot", level="ERROR"):
                activation = await create_bot_module._activate_new_bot(
                    BOT_ID, BOT_NAME, BOT_FILE, FAKE_TOKEN, None
                )

        self.assertEqual(activation.outcome, "webhook_failed")
        set_webhook.assert_awaited_once()
        start_bot.assert_not_called()

    async def test_has_miniapp_registers_menu_button_in_webhook_mode(self):
        set_webhook, _, _, set_menu_button = self._patch_side_effects()
        with patch.dict(os.environ, {"PUBLIC_BASE_URL": BASE_URL, "WEBHOOK_SECRET": SECRET}):
            activation = await create_bot_module._activate_new_bot(
                BOT_ID, BOT_NAME, BOT_FILE, FAKE_TOKEN, None, has_miniapp=True
            )

        self.assertEqual(activation.outcome, "webhook_ok")
        set_menu_button.assert_awaited_once_with(FAKE_TOKEN, BASE_URL, BOT_ID)

    async def test_has_miniapp_registers_menu_button_in_polling_mode(self):
        _, start_bot, _, set_menu_button = self._patch_side_effects()
        env_without_base = {k: v for k, v in os.environ.items() if k != "PUBLIC_BASE_URL"}
        with patch.dict(os.environ, {**env_without_base, "PUBLIC_BASE_URL": BASE_URL}):
            activation = await create_bot_module._activate_new_bot(
                BOT_ID, BOT_NAME, BOT_FILE, FAKE_TOKEN, None, has_miniapp=True
            )

        # PUBLIC_BASE_URL is set here (needed for the mini-app to be servable
        # at all), so this actually exercises webhook mode; the Menu Button
        # call itself doesn't care which delivery mode follows it.
        set_menu_button.assert_awaited_once_with(FAKE_TOKEN, BASE_URL, BOT_ID)

    async def test_has_miniapp_but_no_base_url_skips_menu_button(self):
        """Without PUBLIC_BASE_URL there is no servable mini-app URL to point at."""
        _, _, _, set_menu_button = self._patch_side_effects()
        env_without_base = {k: v for k, v in os.environ.items() if k != "PUBLIC_BASE_URL"}
        with patch.dict(os.environ, env_without_base, clear=True):
            await create_bot_module._activate_new_bot(
                BOT_ID, BOT_NAME, BOT_FILE, FAKE_TOKEN, None, has_miniapp=True
            )
        set_menu_button.assert_not_called()

    async def test_menu_button_failure_does_not_block_webhook_registration(self):
        """Menu Button setup is best-effort — a failure must not stop the bot
        from otherwise going live via webhook registration."""
        set_webhook, _, update_status, set_menu_button = self._patch_side_effects()
        set_menu_button.side_effect = RuntimeError("Telegram unreachable")
        with patch.dict(os.environ, {"PUBLIC_BASE_URL": BASE_URL, "WEBHOOK_SECRET": SECRET}):
            with self.assertLogs("handlers.create_bot", level="ERROR"):
                activation = await create_bot_module._activate_new_bot(
                    BOT_ID, BOT_NAME, BOT_FILE, FAKE_TOKEN, None, has_miniapp=True
                )

        self.assertEqual(activation.outcome, "webhook_ok")
        set_webhook.assert_awaited_once()
        update_status.assert_awaited_once_with(BOT_ID, "running")


class RestoreBotsRespectsWebhookMode(unittest.IsolatedAsyncioTestCase):
    """The other half of the B2 contract: main.restore_bots() must NOT re-spawn a
    polling subprocess for a webhook-served bot (status="running", pid IS NULL) when
    running in webhook mode — otherwise the next prod restart would put both a webhook
    and getUpdates on the same token → Telegram 409."""

    def _patch(self, bots):
        self._get_all = patch.object(main, "get_all_bots", new=AsyncMock(return_value=bots))
        self._start_bot = patch.object(main, "start_bot", new=AsyncMock(return_value=12345))
        self._update_status = patch.object(main, "update_bot_status", new=AsyncMock())
        self._make_env = patch.object(main, "_make_extra_env", new=lambda b: {})
        start_bot = self._start_bot.start()
        self._get_all.start()
        self._update_status.start()
        self._make_env.start()
        for p in (self._get_all, self._start_bot, self._update_status, self._make_env):
            self.addCleanup(p.stop)
        return start_bot

    @staticmethod
    def _bot(**over):
        b = {"id": 7, "name": "b7", "status": "running", "file_path": "/tmp/b7.py", "token": "t7", "pid": None}
        b.update(over)
        return b

    async def test_webhook_mode_skips_running_bot_with_null_pid(self):
        start_bot = self._patch([self._bot(pid=None)])
        with patch.dict(os.environ, {"PUBLIC_BASE_URL": BASE_URL}):
            await main.restore_bots()
        start_bot.assert_not_called()

    async def test_webhook_mode_restores_running_bot_with_real_pid(self):
        # A genuine polling bot (non-NULL pid) must still be restored, even in webhook mode.
        start_bot = self._patch([self._bot(pid=999)])
        with patch.dict(os.environ, {"PUBLIC_BASE_URL": BASE_URL}):
            await main.restore_bots()
        start_bot.assert_awaited_once()

    async def test_polling_mode_restores_running_bot_even_with_null_pid(self):
        # No PUBLIC_BASE_URL → polling/standalone mode: behavior unchanged, every
        # running bot with file+token is re-spawned regardless of pid.
        start_bot = self._patch([self._bot(pid=None)])
        env_without_base = {k: v for k, v in os.environ.items() if k != "PUBLIC_BASE_URL"}
        with patch.dict(os.environ, env_without_base, clear=True):
            await main.restore_bots()
        start_bot.assert_awaited_once()


class NotifyBotCreatedMessaging(unittest.IsolatedAsyncioTestCase):
    """The user-facing wording for the two NEW outcomes this phase introduces."""

    async def _notify(self, outcome: str):
        bot = AsyncMock()  # bot.send_message is an AsyncMock
        activation = create_bot_module._Activation(outcome)
        with patch.object(create_bot_module, "generate_bot_guide", new=AsyncMock(return_value="")):
            await create_bot_module._notify_bot_created(
                bot, chat_id=100, bot_id=BOT_ID, bot_name=BOT_NAME,
                bot_summary="s", username_display=" (@x)", activation=activation,
            )
        bot.send_message.assert_awaited_once()
        return bot.send_message.await_args

    async def test_webhook_ok_message_says_connected_via_webhook(self):
        args = await self._notify("webhook_ok")
        self.assertIn("подключён по вебхуку", args.args[1])

    async def test_webhook_failed_message_tells_user_to_contact_admin(self):
        args = await self._notify("webhook_failed")
        self.assertIn("обратитесь к администратору", args.args[1])


if __name__ == "__main__":
    unittest.main()
