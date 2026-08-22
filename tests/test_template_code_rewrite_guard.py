"""No code-rewrite path may overwrite a shared templates/<id>.py.

Bots 12/13/14 have file_path = templates/<id>.py — they RUN the repo's own
template file. Every LLM rewrite path (/recreate, auto-diagnose, both halves of
fixbug) wrote its output straight to file_path and pushed it to GitHub, so any
of them on such a bot would have replaced the hand-authored template for every
bot built on it. Auto-diagnose is the sharpest edge: it fires off a crashed bot
without anyone pressing "regenerate".

Each of the six sites is checked twice: refused for a template-backed bot with
the file and GitHub untouched, and still working for a custom one.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import handlers.manage_bots as manage_bots
from runtime.registry import is_template_backed

CUSTOM_SOURCE = "# TEMPLATE: team_manager\nprint('my own copy')\n"
LLM_OUTPUT = "print('rewritten by an LLM')\n"


def _callback(data: str, user_id: int = 111) -> MagicMock:
    cb = MagicMock()
    cb.from_user.id = user_id
    cb.data = data
    cb.answer = AsyncMock()
    cb.message.edit_text = AsyncMock()
    cb.message.answer = AsyncMock()
    return cb


class _GuardCase(unittest.IsolatedAsyncioTestCase):
    """Two bots: one running the repo's template, one running its own copy.

    The copy deliberately carries the same "# TEMPLATE:" marker a real one
    would — if the guard keyed off that marker instead of the path, the custom
    bot would be wrongly refused and these tests would catch it.
    """

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.custom_path = os.path.join(self._tmp.name, "my_bot.py")
        with open(self.custom_path, "w", encoding="utf-8") as f:
            f.write(CUSTOM_SOURCE)

        self.template_bot = {
            "id": 13,
            "name": "team_manager_demo",
            "file_path": "templates/team_manager.py",
            "description": "демо на шаблоне",
            "token": "123:fake",
        }
        self.custom_bot = {
            "id": 11,
            "name": "my_bot",
            "file_path": self.custom_path,
            "description": "свой бот",
            "token": "123:fake",
        }
        self.push = patch.object(manage_bots, "push_bot_to_github", AsyncMock())
        self.push_mock = self.push.start()

    async def asyncTearDown(self):
        self.push.stop()
        self._tmp.cleanup()
        manage_bots._busy_bots.discard(13)
        manage_bots._busy_bots.discard(11)

    def assert_template_untouched(self):
        """The real repo file must still be exactly what git has."""
        with open("templates/team_manager.py", encoding="utf-8") as f:
            self.assertIn("miniapp_config", f.read())
        self.push_mock.assert_not_called()


class WriteBotCodeTests(_GuardCase):
    async def test_refuses_a_template_backed_bot(self):
        with self.assertRaises(manage_bots.TemplateBackedBotError):
            await manage_bots.write_bot_code(self.template_bot, LLM_OUTPUT)
        self.assert_template_untouched()

    async def test_writes_and_pushes_for_a_custom_bot(self):
        await manage_bots.write_bot_code(self.custom_bot, LLM_OUTPUT)
        with open(self.custom_path, encoding="utf-8") as f:
            self.assertEqual(f.read(), LLM_OUTPUT)
        self.push_mock.assert_called_once()

    def test_the_guard_keys_off_the_path_not_the_template_marker(self):
        self.assertTrue(is_template_backed(self.template_bot["file_path"]))
        # Same marker inside the file, different path — must NOT be refused.
        self.assertFalse(is_template_backed(self.custom_bot["file_path"]))


class RecreateCoreTests(_GuardCase):
    async def test_template_bot_is_refused_before_any_llm_call(self):
        with patch.object(manage_bots, "improve_bot_code", AsyncMock()) as llm, \
             patch.object(manage_bots, "get_bot", AsyncMock(return_value=self.template_bot)):
            result = await manage_bots.recreate_bot_core(13, creator_user_id=111)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "template_backed")
        # The whole point of refusing early: no 240s call, no tokens spent.
        llm.assert_not_called()
        self.assert_template_untouched()


class AutofixCoreTests(_GuardCase):
    async def test_template_bot_is_refused_before_any_llm_call(self):
        """Sharpest edge: this path can fire automatically off a crash."""
        with patch.object(manage_bots, "fix_bot_code", AsyncMock()) as llm, \
             patch.object(manage_bots, "get_bot", AsyncMock(return_value=self.template_bot)):
            result = await manage_bots.autofix_bot_core(13)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "template_backed")
        llm.assert_not_called()
        self.assert_template_untouched()


class ApplyFixCoreTests(_GuardCase):
    async def test_template_bot_is_refused(self):
        with patch.object(manage_bots, "get_bot", AsyncMock(return_value=self.template_bot)):
            result = await manage_bots.apply_fix_core(13, LLM_OUTPUT, main_code_hash=None)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "template_backed")
        self.assert_template_untouched()

    async def test_custom_bot_still_applies_its_fix(self):
        with patch.object(manage_bots, "get_bot", AsyncMock(return_value=self.custom_bot)), \
             patch.object(manage_bots, "stop_bot", AsyncMock()), \
             patch.object(manage_bots, "start_bot", AsyncMock(return_value=42)), \
             patch.object(manage_bots, "update_bot_status", AsyncMock()), \
             patch.object(manage_bots, "append_from_scratch_registry_wiring", lambda c: c), \
             patch.object(manage_bots, "_make_extra_env", lambda b: {}):
            result = await manage_bots.apply_fix_core(11, LLM_OUTPUT, main_code_hash=None)
        self.assertTrue(result["ok"], result)
        with open(self.custom_path, encoding="utf-8") as f:
            self.assertEqual(f.read(), LLM_OUTPUT)
        self.push_mock.assert_called_once()


class PerformAutofixTests(_GuardCase):
    async def test_template_bot_is_refused_before_any_llm_call(self):
        """Also reached by the AI dialog's run_autofix tool, not just a button."""
        with patch.object(manage_bots, "fix_bot_code", AsyncMock()) as llm:
            ok, message = await manage_bots._perform_autofix(13, self.template_bot)
        self.assertFalse(ok)
        self.assertEqual(message, manage_bots.TEMPLATE_BACKED_DENIED)
        llm.assert_not_called()
        self.assert_template_untouched()


class RecreateConfirmationTests(_GuardCase):
    async def test_first_tap_only_asks_and_rewrites_nothing(self):
        cb = _callback("recreate:11")
        with patch.object(manage_bots, "get_bot", AsyncMock(return_value=self.custom_bot)), \
             patch.object(manage_bots, "_can_manage_bot", lambda uid, b: True), \
             patch.object(manage_bots, "improve_bot_code", AsyncMock()) as llm:
            await manage_bots.cb_recreate_confirm(cb)

        llm.assert_not_called()
        self.push_mock.assert_not_called()
        cb.message.edit_text.assert_awaited_once()
        kwargs = cb.message.edit_text.call_args.kwargs
        buttons = [
            b.callback_data for row in kwargs["reply_markup"].inline_keyboard for b in row
        ]
        self.assertIn("recreate_go:11", buttons)
        self.assertIn("info:11", buttons)
        # The bot's own file is untouched until the second tap.
        with open(self.custom_path, encoding="utf-8") as f:
            self.assertEqual(f.read(), CUSTOM_SOURCE)

    async def test_template_bot_is_refused_at_the_confirmation_step(self):
        cb = _callback("recreate:13")
        with patch.object(manage_bots, "get_bot", AsyncMock(return_value=self.template_bot)), \
             patch.object(manage_bots, "_can_manage_bot", lambda uid, b: True):
            await manage_bots.cb_recreate_confirm(cb)

        cb.message.edit_text.assert_awaited_once_with(manage_bots.TEMPLATE_BACKED_DENIED)
        self.assert_template_untouched()


class TelegramRewritePathTests(_GuardCase):
    async def test_cb_recreate_refuses_a_template_bot(self):
        cb = _callback("recreate_go:13")
        with patch.object(manage_bots, "_bot_or_deny", AsyncMock(return_value=self.template_bot)), \
             patch.object(manage_bots, "improve_bot_code", AsyncMock()) as llm:
            await manage_bots.cb_recreate(cb)
        llm.assert_not_called()
        self.assert_template_untouched()

    async def test_cb_apply_fix_refuses_a_template_bot(self):
        cb = _callback("applyfix:13")
        state = MagicMock()
        state.get_data = AsyncMock(
            return_value={"fix_bot_id": 13, "fix_pending_code": LLM_OUTPUT}
        )
        with patch.object(manage_bots, "get_bot", AsyncMock(return_value=self.template_bot)), \
             patch.object(manage_bots, "_can_manage_bot", lambda uid, b: True):
            await manage_bots.cb_apply_fix(cb, state)
        cb.message.edit_text.assert_awaited_once_with(manage_bots.TEMPLATE_BACKED_DENIED)
        self.assert_template_untouched()
        # And the busy-lock must not be left held on the refused bot.
        self.assertNotIn(13, manage_bots._busy_bots)


if __name__ == "__main__":
    unittest.main()
