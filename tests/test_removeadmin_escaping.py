"""cmd_removeadmin HTML-escaping — regression test across the admin-management
templates that share the copy-pasted /removeadmin pattern.

Bug (found in templates/moderator.py, fixed there with _esc()): unlike
cmd_addadmin (which validates parts[1].lstrip("-").isdigit() before use),
cmd_removeadmin echoed the raw /removeadmin argument straight into a
parse_mode="HTML" <code> block. Arbitrary text containing '<'/'&'/'"'/"'"
would corrupt the HTML the message is sent with. The same copy-pasted
handler existed, unfixed, in 6 other templates (inventory.py, booking_medical.py
and referral_program.py already had an _esc() helper defined but unused here;
accountant.py, booking_beauty.py, manager_secretary.py and trip_manager.py had
no _esc() helper at all). tour_operator.py has no /addadmin//removeadmin
commands (different, single-admin auto-bootstrap auth model) so it is not
covered here.

No real Telegram network calls, no real tokens.

Run with: python -m unittest tests.test_removeadmin_escaping
"""

from __future__ import annotations

import html
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import SendMessage

from runtime.registry import get_template_router
from templates import (
    accountant,
    booking_beauty,
    booking_medical,
    inventory,
    manager_secretary,
    moderator,
    referral_program,
    trip_manager,
)

FAKE_TOKEN = "123456:test-token-not-real"
ADMIN_ID = 999

# Payload exercising every character html.escape(quote=True) touches:
# '<', '>', '&', '"', "'".
MALICIOUS_ID = """</code><b>hacked</b>&pwn"quote'"""

# (module, template registry name) — every affected template shares the
# identical config_from_bot_row(bot_row, data_dir) signature.
TEMPLATES = [
    (accountant, "accountant"),
    (booking_beauty, "booking_beauty"),
    (booking_medical, "booking_medical"),
    (inventory, "inventory"),
    (manager_secretary, "manager_secretary"),
    (referral_program, "referral_program"),
    (trip_manager, "trip_manager"),
    (moderator, "moderator"),  # already-fixed template, kept as a regression check
]


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


class RemoveAdminEscapingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def _send_removeadmin(self, module, template_name: str, malicious_id: str):
        config = module.config_from_bot_row(
            {"bot_id": 1, "name": f"{template_name}_esc_test", "display_name": None, "group_chat_id": None},
            self.data_dir,
        )
        if template_name == "moderator":
            # moderator.py moved bot-admin storage from the shared
            # admins_file/_save_admins JSON pattern to a SQLite bot_admins
            # table (see backlog_moderator_admin_panel_concurrency) — seed
            # through init_db + _add_bot_admin instead of the other
            # templates' still-file-based _save_admins.
            await module.init_db(config.db_path)
            await module._add_bot_admin(config.db_path, str(ADMIN_ID))
        else:
            module._save_admins(config.admins_file, {str(ADMIN_ID)})

        bot = Bot(token=FAKE_TOKEN)
        dp = Dispatcher(storage=MemoryStorage())
        dp.update.outer_middleware(module.ConfigMiddleware(config))
        dp.include_router(get_template_router(template_name))

        call_mock = AsyncMock(return_value=MagicMock())
        with patch.object(Bot, "__call__", new=call_mock):
            await dp.feed_webhook_update(bot, _text_update(1, ADMIN_ID, f"/removeadmin {malicious_id}"))

        sent_texts = [
            c.args[0].text if c.args else c.kwargs["request"].text
            for c in call_mock.call_args_list
            if isinstance((c.args[0] if c.args else c.kwargs.get("request")), SendMessage)
        ]
        return sent_texts

    async def test_malicious_id_does_not_break_html_across_all_templates(self):
        for module, template_name in TEMPLATES:
            with self.subTest(template=template_name):
                sent_texts = await self._send_removeadmin(module, template_name, MALICIOUS_ID)
                self.assertTrue(sent_texts, f"{template_name}: no SendMessage captured")
                text = sent_texts[0]
                # The raw payload must never appear unescaped in the outgoing HTML.
                self.assertNotIn(MALICIOUS_ID, text, f"{template_name}: raw payload leaked unescaped")
                self.assertNotIn("<b>hacked</b>", text, f"{template_name}: injected tag survived unescaped")
                # And the escaped form must be present instead.
                self.assertIn(html.escape(MALICIOUS_ID), text, f"{template_name}: expected escaped payload missing")

    async def test_confirmation_message_exact_shape(self):
        for module, template_name in TEMPLATES:
            with self.subTest(template=template_name):
                sent_texts = await self._send_removeadmin(module, template_name, MALICIOUS_ID)
                expected = f"✅ <code>{html.escape(MALICIOUS_ID)}</code> удалён."
                self.assertEqual(sent_texts[0], expected, f"{template_name}: unexpected message shape")


if __name__ == "__main__":
    unittest.main()
