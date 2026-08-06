"""Tests for the event_manager template's new pattern: deep-link guest
self-service RSVP (invite_token unpredictability + the two independent
hijack-defense layers) and door check-in (a status SEPARATE from RSVP).

Most tests call the module's own async DB helpers directly against a
tempfile db_path. A few drive the real dispatcher end-to-end (/start
g_<token>, then the evm_rsvp:<id>:yes callback) via feed_webhook_update,
same approach as test_event_manager_isolation.py / test_shop_catalog_isolation.py.

Everything lives in tempfile.TemporaryDirectory(), never data/bots.db.

Run with: python -m unittest tests.test_event_manager_rsvp_checkin
"""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from runtime.registry import get_template_router
from templates import event_manager as evm

FAKE_TOKEN = "123456:test-token-not-real"

# secrets.token_urlsafe(16) always encodes 16 bytes (128 bits) as base64url
# without padding -> a fixed 22-character string drawn from [A-Za-z0-9_-].
TOKEN_URLSAFE_RE = re.compile(r"^[A-Za-z0-9_-]{22}$")


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


def _callback_update(update_id: int, user_id: int, data: str, msg_id: int = 1) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": str(update_id),
            "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
            "message": {
                "message_id": msg_id, "date": 1700000000,
                "chat": {"id": user_id, "type": "private"}, "text": "placeholder",
            },
            "chat_instance": "1", "data": data,
        },
    }


def _build_bot_dispatcher(config: evm.EventManagerConfig) -> tuple[Bot, Dispatcher]:
    bot = Bot(token=FAKE_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(evm.ConfigMiddleware(config))
    dp.include_router(get_template_router("event_manager"))
    return bot, dp


class InviteTokenTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.config = evm._paths_for("evm_token_bot", Path(self._tmp.name))
        await evm.init_db(self.config.db_path)
        self.event_id = await evm._create_event(self.config.db_path, "Party", "2026-12-25 19:00", None, None)

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def test_invite_tokens_are_not_sequential_or_predictable(self):
        guests = [
            await evm._create_guest(self.config.db_path, self.event_id, f"Guest {i}", None)
            for i in range(5)
        ]
        tokens = [g["invite_token"] for g in guests]
        ids = [g["id"] for g in guests]

        # ids ARE sequential (plain AUTOINCREMENT) — that's expected and fine,
        # it's the token that must NOT be derivable from it.
        self.assertEqual(ids, sorted(ids))

        # all 5 tokens unique
        self.assertEqual(len(set(tokens)), 5)

        # every token is shaped like secrets.token_urlsafe(16) output
        for tok in tokens:
            self.assertRegex(tok, TOKEN_URLSAFE_RE)

        # token is not simply the guest id, or any obvious transform of it.
        # (NOT checking "str(gid) not in tok" — a single-digit id like "2"
        # will show up as a substring of a random 22-char string most of the
        # time by pure chance; that's not a signal of predictability.)
        for tok, gid in zip(tokens, ids):
            self.assertNotEqual(tok, str(gid))
            self.assertNotEqual(tok, str(gid).zfill(len(tok)))
            self.assertNotEqual(tok, hex(gid))
            self.assertFalse(tok.isdigit(), f"token {tok!r} looks like a bare sequential id")

    async def test_create_guest_generates_a_fresh_token_per_call(self):
        g1 = await evm._create_guest(self.config.db_path, self.event_id, "A", None)
        g2 = await evm._create_guest(self.config.db_path, self.event_id, "B", None)
        self.assertNotEqual(g1["invite_token"], g2["invite_token"])


class ClaimGuestTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.config = evm._paths_for("evm_claim_bot", Path(self._tmp.name))
        await evm.init_db(self.config.db_path)
        self.event_id = await evm._create_event(self.config.db_path, "Party", "2026-12-25 19:00", None, None)
        self.guest = await evm._create_guest(self.config.db_path, self.event_id, "Alice", None)

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def test_claim_unknown_token_returns_none(self):
        result = await evm._claim_guest(self.config.db_path, "does-not-exist", 1001)
        self.assertIsNone(result)

    async def test_first_claim_binds_telegram_user_id_when_null(self):
        row = await evm._guest_row(self.config.db_path, self.guest["id"])
        self.assertIsNone(row["telegram_user_id"])

        result = await evm._claim_guest(self.config.db_path, self.guest["invite_token"], 1001)
        self.assertIsNotNone(result)
        self.assertEqual(result["telegram_user_id"], 1001)

        row_after = await evm._guest_row(self.config.db_path, self.guest["id"])
        self.assertEqual(row_after["telegram_user_id"], 1001)

    async def test_same_user_reclaiming_own_link_is_idempotent(self):
        """The guest reopens their own /start deep link a second time — must
        not error, must not reassign, must return the same guest."""
        first = await evm._claim_guest(self.config.db_path, self.guest["invite_token"], 1001)
        second = await evm._claim_guest(self.config.db_path, self.guest["invite_token"], 1001)

        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["telegram_user_id"], 1001)
        row = await evm._guest_row(self.config.db_path, self.guest["id"])
        self.assertEqual(row["telegram_user_id"], 1001)

    async def test_different_user_cannot_hijack_an_already_claimed_token(self):
        """The specific hijack scenario the token design defends against: a
        SECOND, different telegram_user_id calling _claim_guest on an
        already-claimed token must NOT reassign the guest — the original
        claimer stays bound."""
        await evm._claim_guest(self.config.db_path, self.guest["invite_token"], 1001)
        result = await evm._claim_guest(self.config.db_path, self.guest["invite_token"], 2002)

        # returned row reflects the ORIGINAL claimer, not the hijacker
        self.assertEqual(result["telegram_user_id"], 1001)
        row = await evm._guest_row(self.config.db_path, self.guest["id"])
        self.assertEqual(row["telegram_user_id"], 1001)


class CheckinStatusTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.config = evm._paths_for("evm_checkin_bot", Path(self._tmp.name))
        await evm.init_db(self.config.db_path)
        self.event_id = await evm._create_event(self.config.db_path, "Party", "2026-12-25 19:00", None, None)

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def test_declined_guest_can_still_be_checked_in(self):
        guest = await evm._create_guest(self.config.db_path, self.event_id, "Bob", None)
        await evm._set_rsvp(self.config.db_path, guest["id"], "declined")
        await evm._toggle_checkin(self.config.db_path, guest["id"])

        row = await evm._guest_row(self.config.db_path, guest["id"])
        self.assertEqual(row["rsvp_status"], "declined")
        self.assertEqual(row["checked_in"], 1)
        self.assertIsNotNone(row["checked_in_at"])

    async def test_confirmed_guest_is_not_auto_checked_in(self):
        guest = await evm._create_guest(self.config.db_path, self.event_id, "Carol", None)
        await evm._set_rsvp(self.config.db_path, guest["id"], "confirmed")

        row = await evm._guest_row(self.config.db_path, guest["id"])
        self.assertEqual(row["rsvp_status"], "confirmed")
        self.assertEqual(row["checked_in"], 0)
        self.assertIsNone(row["checked_in_at"])

    async def test_toggle_checkin_flips_back_and_forth_independent_of_rsvp(self):
        guest = await evm._create_guest(self.config.db_path, self.event_id, "Dave", None)
        await evm._set_rsvp(self.config.db_path, guest["id"], "confirmed")

        await evm._toggle_checkin(self.config.db_path, guest["id"])
        row1 = await evm._guest_row(self.config.db_path, guest["id"])
        self.assertEqual(row1["checked_in"], 1)
        self.assertEqual(row1["rsvp_status"], "confirmed")

        await evm._toggle_checkin(self.config.db_path, guest["id"])
        row2 = await evm._guest_row(self.config.db_path, guest["id"])
        self.assertEqual(row2["checked_in"], 0)
        self.assertIsNone(row2["checked_in_at"])
        self.assertEqual(row2["rsvp_status"], "confirmed")  # unaffected by check-in toggle


class EventStatsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.config = evm._paths_for("evm_stats_bot", Path(self._tmp.name))
        await evm.init_db(self.config.db_path)
        self.event_id = await evm._create_event(self.config.db_path, "Party", "2026-12-25 19:00", None, None)

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def test_event_stats_counts_match_inserted(self):
        g1 = await evm._create_guest(self.config.db_path, self.event_id, "A", None)
        g2 = await evm._create_guest(self.config.db_path, self.event_id, "B", None)
        g3 = await evm._create_guest(self.config.db_path, self.event_id, "C", None)
        await evm._set_rsvp(self.config.db_path, g1["id"], "confirmed")
        await evm._set_rsvp(self.config.db_path, g2["id"], "declined")
        # g3 stays 'invited' — never answered
        await evm._toggle_checkin(self.config.db_path, g1["id"])

        stats = await evm._event_stats(self.config.db_path, self.event_id)
        self.assertEqual(stats["counts"]["confirmed"], 1)
        self.assertEqual(stats["counts"]["declined"], 1)
        self.assertEqual(stats["counts"]["invited"], 1)
        self.assertEqual(stats["total"], 3)
        self.assertEqual(stats["checked_in"], 1)


class EndToEndRsvpTests(unittest.IsolatedAsyncioTestCase):
    """Drives /start g_<token> and the evm_rsvp:<id>:yes callback through the
    real dispatcher — exercises aiogram's CommandObject payload parsing and
    both independent hijack-defense layers described in cb_rsvp's docstring."""

    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=AsyncMock(return_value=MagicMock()))
        self._mock_call = self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.config = evm._paths_for("evm_e2e_bot", Path(self._tmp.name))
        await evm.init_db(self.config.db_path)
        self.event_id = await evm._create_event(self.config.db_path, "Party", "2026-12-25 19:00", None, None)
        self.guest = await evm._create_guest(self.config.db_path, self.event_id, "Alice", None)
        self.bot, self.dp = _build_bot_dispatcher(self.config)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_start_with_guest_token_claims_the_guest(self):
        GUEST_USER = 4242
        await self.dp.feed_webhook_update(
            self.bot, _text_update(1, GUEST_USER, f"/start g_{self.guest['invite_token']}")
        )
        row = await evm._guest_row(self.config.db_path, self.guest["id"])
        self.assertEqual(row["telegram_user_id"], GUEST_USER)

    async def test_start_with_unknown_token_does_not_claim_anything(self):
        GUEST_USER = 4242
        await self.dp.feed_webhook_update(self.bot, _text_update(1, GUEST_USER, "/start g_totally-bogus-token"))
        row = await evm._guest_row(self.config.db_path, self.guest["id"])
        self.assertIsNone(row["telegram_user_id"])

    async def test_rsvp_callback_from_bound_user_updates_status(self):
        GUEST_USER = 4242
        await self.dp.feed_webhook_update(
            self.bot, _text_update(1, GUEST_USER, f"/start g_{self.guest['invite_token']}")
        )
        await self.dp.feed_webhook_update(
            self.bot, _callback_update(2, GUEST_USER, f"evm_rsvp:{self.guest['id']}:yes")
        )
        row = await evm._guest_row(self.config.db_path, self.guest["id"])
        self.assertEqual(row["rsvp_status"], "confirmed")

    async def test_rsvp_callback_from_non_bound_user_is_rejected(self):
        """Second defense layer: even if a stranger somehow gets the
        evm_rsvp:<id>:yes callback in front of them (e.g. a leaked token, or
        guessing a guest_id), cb_rsvp checks cb.from_user.id against the
        guest's bound telegram_user_id and refuses — rsvp_status must stay
        unchanged."""
        GUEST_USER = 4242
        STRANGER = 9999
        await self.dp.feed_webhook_update(
            self.bot, _text_update(1, GUEST_USER, f"/start g_{self.guest['invite_token']}")
        )
        await self.dp.feed_webhook_update(
            self.bot, _callback_update(2, STRANGER, f"evm_rsvp:{self.guest['id']}:yes")
        )
        row = await evm._guest_row(self.config.db_path, self.guest["id"])
        self.assertEqual(row["rsvp_status"], "invited")  # unchanged
        self.assertEqual(row["telegram_user_id"], GUEST_USER)  # still bound to the real guest

    async def test_rsvp_callback_before_any_claim_is_rejected(self):
        """A guest_id with telegram_user_id still NULL (nobody has opened the
        link yet) must reject any evm_rsvp callback, not just mismatched
        ones — guest["telegram_user_id"] != cb.from_user.id also fires when
        the DB value is None."""
        STRANGER = 9999
        await self.dp.feed_webhook_update(
            self.bot, _callback_update(1, STRANGER, f"evm_rsvp:{self.guest['id']}:yes")
        )
        row = await evm._guest_row(self.config.db_path, self.guest["id"])
        self.assertEqual(row["rsvp_status"], "invited")
        self.assertIsNone(row["telegram_user_id"])


if __name__ == "__main__":
    unittest.main()
