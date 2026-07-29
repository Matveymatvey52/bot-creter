"""moderator template — data isolation, escalation ladder, and the three
rights-checking mechanisms.

Standard criterion: two bots on the SAME template, different config, must
never mix data — even driven by the SAME chat_id/user_id.

PLUS the requirement unique to this template (see docs/STAGE2_DESIGN.md
"ГЛАВНОЕ ТРЕБОВАНИЕ ВЛАДЕЛЬЦА"): the bot cannot moderate without Telegram
admin rights in a given group, and silent failure is unacceptable. Three
mechanisms cover it, each with its own test class below:
  1. on_bot_membership_changed (my_chat_member, no direction filter)
  2. /checkrights (manual live re-check)
  3. _moderate_safely (explicit DM to admins_file admins on a real API failure)

No real Telegram network calls, no real tokens — Bot.__call__ is replaced
with a FakeBotAPI that inspects the aiogram method object and returns
configured ChatMember fixtures / raises Telegram* errors on demand.

Run with: python -m unittest tests.test_moderator_isolation
"""

from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import aiosqlite
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import BanChatMember, GetChatMember, RestrictChatMember, SendMessage
from aiogram.types import ChatMemberAdministrator, ChatMemberMember, User

from runtime.registry import get_template_router
from templates import moderator

FAKE_TOKEN = "123456:test-token-not-real"
BOT_USER_ID = 123456  # matches FAKE_TOKEN's numeric prefix (Bot.id derivation)


def _admin_member(user_id: int, can_delete: bool = True, can_restrict: bool = True) -> ChatMemberAdministrator:
    return ChatMemberAdministrator(
        status="administrator",
        user=User(id=user_id, is_bot=False, first_name="Admin"),
        can_be_edited=False,
        is_anonymous=False,
        can_manage_chat=True,
        can_delete_messages=can_delete,
        can_manage_video_chats=True,
        can_restrict_members=can_restrict,
        can_promote_members=False,
        can_change_info=True,
        can_invite_users=True,
        can_post_stories=False,
        can_edit_stories=False,
        can_delete_stories=False,
    )


def _plain_member(user_id: int) -> ChatMemberMember:
    return ChatMemberMember(user=User(id=user_id, is_bot=False, first_name="User"))


class FakeBotAPI:
    """Replaces Bot.__call__. Every aiogram method call is recorded in .calls.
    get_chat_member() answers are looked up from .chat_member_responses (keyed
    by (chat_id, user_id)), defaulting to a plain non-admin member. Method
    TYPES listed in .fail_methods raise TelegramForbiddenError instead of
    succeeding — simulates the bot losing rights at the moment of a real
    moderation action (mechanism #3)."""

    def __init__(self):
        self.calls: list = []
        self.chat_member_responses: dict[tuple[int, int], object] = {}
        self.fail_methods: set[type] = set()
        self.retry_after_methods: set[type] = set()

    async def __call__(self, request, **kwargs):
        self.calls.append(request)
        if isinstance(request, GetChatMember):
            key = (request.chat_id, request.user_id)
            return self.chat_member_responses.get(key, _plain_member(request.user_id))
        if type(request) in self.retry_after_methods:
            raise TelegramRetryAfter(method=request, message="Too Many Requests: retry later", retry_after=1)
        if type(request) in self.fail_methods:
            raise TelegramForbiddenError(method=request, message="Forbidden: bot has no rights")
        return object()

    def sent_texts(self, chat_id: int | None = None) -> list[str]:
        out = []
        for c in self.calls:
            if isinstance(c, SendMessage) and (chat_id is None or c.chat_id == chat_id):
                out.append(c.text)
        return out


def _group_text_update(update_id: int, chat_id: int, user_id: int, text: str) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id, "date": 1700000000,
            "chat": {"id": chat_id, "type": "supergroup", "title": "Test Group"},
            "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
            "text": text,
        },
    }


def _group_photo_caption_update(update_id: int, chat_id: int, user_id: int, caption: str) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id, "date": 1700000000,
            "chat": {"id": chat_id, "type": "supergroup", "title": "Test Group"},
            "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
            "photo": [{"file_id": "AgADfake", "file_unique_id": "u1", "width": 90, "height": 90}],
            "caption": caption,
        },
    }


def _bot_user_json(status: str, extra: dict | None = None) -> dict:
    base = {"status": status, "user": {"id": BOT_USER_ID, "is_bot": True, "first_name": "ModBot"}}
    if extra:
        base.update(extra)
    return base


_FULL_ADMIN_RIGHTS_JSON = {
    "can_be_edited": False, "is_anonymous": False, "can_manage_chat": True,
    "can_delete_messages": True, "can_manage_video_chats": True, "can_restrict_members": True,
    "can_promote_members": False, "can_change_info": True, "can_invite_users": True,
    "can_post_stories": False, "can_edit_stories": False, "can_delete_stories": False,
}
_PARTIAL_ADMIN_RIGHTS_JSON = dict(_FULL_ADMIN_RIGHTS_JSON, can_restrict_members=False)


def _my_chat_member_update(update_id: int, chat_id: int, old_status_json: dict, new_status_json: dict) -> dict:
    return {
        "update_id": update_id,
        "my_chat_member": {
            "chat": {"id": chat_id, "type": "supergroup", "title": "Test Group"},
            "from": {"id": 555, "is_bot": False, "first_name": "GroupAdmin"},
            "date": 1700000000,
            "old_chat_member": old_status_json,
            "new_chat_member": new_status_json,
        },
    }


def _build_bot_dispatcher(config: moderator.ModeratorConfig) -> tuple[Bot, Dispatcher]:
    bot = Bot(token=FAKE_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(moderator.ConfigMiddleware(config))
    dp.include_router(get_template_router("moderator"))
    return bot, dp


class ModeratorIsolationTests(unittest.IsolatedAsyncioTestCase):
    """Two bots on the moderator template, different config, driven by the
    SAME chat_id/user_id — warnings/logs must never mix between them."""

    async def asyncSetUp(self):
        self.fake_api = FakeBotAPI()
        self._patcher = patch.object(Bot, "__call__", new=self.fake_api)
        self._patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config_a = moderator.config_from_bot_row(
            {"bot_id": 701, "name": "mod_a", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        self.config_b = moderator.config_from_bot_row(
            {"bot_id": 702, "name": "mod_b", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await moderator.init_db(self.config_a.db_path)
        await moderator.init_db(self.config_b.db_path)
        self.bot_a, self.dp_a = _build_bot_dispatcher(self.config_a)
        self.bot_b, self.dp_b = _build_bot_dispatcher(self.config_b)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._patcher.stop()

    async def test_configs_point_to_different_files(self):
        self.assertNotEqual(self.config_a.db_path, self.config_b.db_path)

    async def test_same_chat_same_user_violations_not_mixed_between_bots(self):
        SHARED_CHAT, SHARED_USER = -100999, 4242
        await self.dp_a.feed_webhook_update(self.bot_a, _group_text_update(1, SHARED_CHAT, SHARED_USER, "click http://spam.example"))
        await self.dp_b.feed_webhook_update(self.bot_b, _group_text_update(1, SHARED_CHAT, SHARED_USER, "click http://spam.example"))
        await self.dp_b.feed_webhook_update(self.bot_b, _group_text_update(2, SHARED_CHAT, SHARED_USER, "http://spam2.example"))

        conn_a = sqlite3.connect(self.config_a.db_path)
        count_a = conn_a.execute(
            "SELECT count FROM warnings WHERE user_id=? AND chat_id=?", (SHARED_USER, SHARED_CHAT)
        ).fetchone()[0]
        conn_a.close()
        conn_b = sqlite3.connect(self.config_b.db_path)
        count_b = conn_b.execute(
            "SELECT count FROM warnings WHERE user_id=? AND chat_id=?", (SHARED_USER, SHARED_CHAT)
        ).fetchone()[0]
        conn_b.close()

        self.assertEqual(count_a, 1)
        self.assertEqual(count_b, 2)

    async def test_stopwords_and_settings_and_log_not_mixed_between_bots(self):
        """Review-found coverage gap: only `warnings` was previously asserted
        for cross-bot isolation. stopwords/chat_settings/moderation_log are
        equally per-bot data (same db_path split as warnings) and must be
        checked too, on the SAME shared chat_id used above."""
        SHARED_CHAT = -100888
        async with aiosqlite.connect(self.config_a.db_path) as db:
            await db.execute("INSERT INTO stopwords (chat_id, word) VALUES (?,?)", (SHARED_CHAT, "onlyinA"))
            await db.execute(
                "INSERT INTO chat_settings (chat_id, max_warnings) VALUES (?,7)", (SHARED_CHAT,)
            )
            await db.commit()

        await self.dp_b.feed_webhook_update(
            self.bot_b, _group_text_update(1, SHARED_CHAT, 5151, "click http://spam.example")
        )

        conn_a = sqlite3.connect(self.config_a.db_path)
        words_a = conn_a.execute("SELECT word FROM stopwords WHERE chat_id=?", (SHARED_CHAT,)).fetchall()
        max_warn_a = conn_a.execute(
            "SELECT max_warnings FROM chat_settings WHERE chat_id=?", (SHARED_CHAT,)
        ).fetchone()[0]
        log_a = conn_a.execute("SELECT COUNT(*) FROM moderation_log WHERE chat_id=?", (SHARED_CHAT,)).fetchone()[0]
        conn_a.close()

        conn_b = sqlite3.connect(self.config_b.db_path)
        words_b = conn_b.execute("SELECT word FROM stopwords WHERE chat_id=?", (SHARED_CHAT,)).fetchall()
        max_warn_b = conn_b.execute(
            "SELECT max_warnings FROM chat_settings WHERE chat_id=?", (SHARED_CHAT,)
        ).fetchone()[0]
        log_b = conn_b.execute("SELECT COUNT(*) FROM moderation_log WHERE chat_id=?", (SHARED_CHAT,)).fetchone()[0]
        conn_b.close()

        self.assertEqual(words_a, [("onlyinA",)])
        self.assertEqual(words_b, [])  # bot B never saw that stopword row
        self.assertEqual(max_warn_a, 7)
        self.assertNotEqual(max_warn_b, 7)  # bot B's default (3), not bot A's override
        self.assertEqual(log_b, 1)  # bot B's own violation from the link above
        self.assertEqual(log_a, 0)  # bot A never processed anything in this chat


class ModeratorCaptionBypassTests(unittest.IsolatedAsyncioTestCase):
    """Review-found blocker: the antispam filter used to match F.text only, so
    a spam link/stopword sent as a PHOTO CAPTION never reached moderate_message
    at all — a full bypass of the template's core purpose. Fixed to match
    F.text | F.caption, with the "/" command-skip scoped to msg.text only (a
    caption starting with "/" is still just content, not a real command)."""

    CHAT_ID = -100777
    USER_ID = 6161

    async def asyncSetUp(self):
        self.fake_api = FakeBotAPI()
        self._patcher = patch.object(Bot, "__call__", new=self.fake_api)
        self._patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = moderator.config_from_bot_row(
            {"bot_id": 709, "name": "mod_caption", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await moderator.init_db(self.config.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._patcher.stop()

    async def test_spam_link_in_photo_caption_is_moderated(self):
        await self.dp.feed_webhook_update(
            self.bot, _group_photo_caption_update(1, self.CHAT_ID, self.USER_ID, "check this out http://spam.example")
        )
        conn = sqlite3.connect(self.config.db_path)
        row = conn.execute(
            "SELECT reason FROM moderation_log WHERE user_id=? AND chat_id=?", (self.USER_ID, self.CHAT_ID)
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row, "a spam link hidden in a photo caption was not moderated at all")
        self.assertEqual(row[0], "спам-ссылка")

    async def test_caption_starting_with_slash_is_still_scanned_not_treated_as_a_command(self):
        await self.dp.feed_webhook_update(
            self.bot, _group_photo_caption_update(1, self.CHAT_ID, self.USER_ID, "/fake http://spam.example")
        )
        conn = sqlite3.connect(self.config.db_path)
        row = conn.execute(
            "SELECT reason FROM moderation_log WHERE user_id=? AND chat_id=?", (self.USER_ID, self.CHAT_ID)
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row, "a caption starting with '/' was wrongly exempted as if it were a real command")


class ModeratorEscalationLadderTests(unittest.IsolatedAsyncioTestCase):
    """warn -> mute -> ban, one violation per stage (max_warnings=1)."""

    CHAT_ID = -100111
    USER_ID = 777

    async def asyncSetUp(self):
        self.fake_api = FakeBotAPI()
        self._patcher = patch.object(Bot, "__call__", new=self.fake_api)
        self._patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = moderator.config_from_bot_row(
            {"bot_id": 703, "name": "mod_ladder", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await moderator.init_db(self.config.db_path)
        async with aiosqlite.connect(self.config.db_path) as db:
            await db.execute(
                "INSERT INTO chat_settings (chat_id, max_warnings, mute_minutes) VALUES (?,1,10)", (self.CHAT_ID,)
            )
            await db.commit()
        self.bot, self.dp = _build_bot_dispatcher(self.config)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._patcher.stop()

    async def test_warn_then_mute_then_ban(self):
        await self.dp.feed_webhook_update(self.bot, _group_text_update(1, self.CHAT_ID, self.USER_ID, "spam http://a.example"))
        await self.dp.feed_webhook_update(self.bot, _group_text_update(2, self.CHAT_ID, self.USER_ID, "spam http://b.example"))
        await self.dp.feed_webhook_update(self.bot, _group_text_update(3, self.CHAT_ID, self.USER_ID, "spam http://c.example"))

        conn = sqlite3.connect(self.config.db_path)
        count, stage = conn.execute(
            "SELECT count, stage FROM warnings WHERE user_id=? AND chat_id=?", (self.USER_ID, self.CHAT_ID)
        ).fetchone()
        actions = [r[0] for r in conn.execute("SELECT action FROM moderation_log ORDER BY id").fetchall()]
        conn.close()

        self.assertEqual((count, stage), (0, "banned"))
        self.assertEqual(actions, ["warn", "mute", "ban"])

        restrict_calls = [c for c in self.fake_api.calls if isinstance(c, RestrictChatMember)]
        ban_calls = [c for c in self.fake_api.calls if isinstance(c, BanChatMember)]
        self.assertEqual(len(restrict_calls), 1)
        self.assertEqual(restrict_calls[0].user_id, self.USER_ID)
        self.assertEqual(len(ban_calls), 1)
        self.assertEqual(ban_calls[0].user_id, self.USER_ID)

    async def test_concurrent_violations_do_not_lose_an_update(self):
        """Review-found race: SELECT-decide-UPDATE with no lock let two near-
        simultaneous violations from the same user (a spam burst — exactly
        this bot's core scenario) both read the same count and both write the
        same result. Fired via asyncio.gather to actually interleave at the
        `await` points inside _apply_escalation, not just called sequentially.
        Fixed with BEGIN IMMEDIATE serializing the read-decide-write per
        (user_id, chat_id) — the concurrent pair must behave exactly like two
        SEQUENTIAL calls: first warn (count=1), second warn (count=2), never
        both landing on count=1."""
        concurrent_user = User(id=9191, is_bot=False, first_name="Concurrent")
        await asyncio.gather(
            moderator._apply_escalation(self.bot, self.config, self.CHAT_ID, concurrent_user, "спам-ссылка", 2, 10),
            moderator._apply_escalation(self.bot, self.config, self.CHAT_ID, concurrent_user, "спам-ссылка", 2, 10),
        )
        conn = sqlite3.connect(self.config.db_path)
        count, stage = conn.execute(
            "SELECT count, stage FROM warnings WHERE user_id=? AND chat_id=?", (concurrent_user.id, self.CHAT_ID)
        ).fetchone()
        log_count = conn.execute(
            "SELECT COUNT(*) FROM moderation_log WHERE user_id=?", (concurrent_user.id,)
        ).fetchone()[0]
        conn.close()
        self.assertEqual(log_count, 2, "a concurrent violation was lost — moderation_log should have exactly 2 rows")
        self.assertEqual((count, stage), (2, "warn"))

    async def test_stopword_triggers_same_ladder_as_link(self):
        async with aiosqlite.connect(self.config.db_path) as db:
            await db.execute("INSERT INTO stopwords (chat_id, word) VALUES (?,?)", (self.CHAT_ID, "badword"))
            await db.commit()
        await self.dp.feed_webhook_update(self.bot, _group_text_update(1, self.CHAT_ID, self.USER_ID, "this has BadWord in it"))

        conn = sqlite3.connect(self.config.db_path)
        row = conn.execute(
            "SELECT reason FROM moderation_log WHERE user_id=? AND chat_id=?", (self.USER_ID, self.CHAT_ID)
        ).fetchone()
        conn.close()
        self.assertEqual(row[0], "стоп-слово")


class ModeratorAdminExclusionTests(unittest.IsolatedAsyncioTestCase):
    """A group's own Telegram admin/creator is never moderated, even if their
    message would otherwise match a link/stopword."""

    CHAT_ID = -100222
    ADMIN_SENDER_ID = 888

    async def asyncSetUp(self):
        self.fake_api = FakeBotAPI()
        self.fake_api.chat_member_responses[(self.CHAT_ID, self.ADMIN_SENDER_ID)] = _admin_member(self.ADMIN_SENDER_ID)
        self._patcher = patch.object(Bot, "__call__", new=self.fake_api)
        self._patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = moderator.config_from_bot_row(
            {"bot_id": 704, "name": "mod_admin_excl", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await moderator.init_db(self.config.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._patcher.stop()

    async def test_group_admin_message_with_link_is_not_moderated(self):
        await self.dp.feed_webhook_update(
            self.bot, _group_text_update(1, self.CHAT_ID, self.ADMIN_SENDER_ID, "check http://totally-fine.example")
        )
        conn = sqlite3.connect(self.config.db_path)
        count = conn.execute("SELECT COUNT(*) FROM warnings").fetchone()[0]
        conn.close()
        self.assertEqual(count, 0)


class ModeratorGroupCommandAuthorityTests(unittest.IsolatedAsyncioTestCase):
    """/addstopword & co require live Telegram admin/creator status in THAT
    group — not admins_file (see docs/STAGE2_DESIGN.md)."""

    CHAT_ID = -100333
    NON_ADMIN_ID = 111
    GROUP_ADMIN_ID = 222

    async def asyncSetUp(self):
        self.fake_api = FakeBotAPI()
        self.fake_api.chat_member_responses[(self.CHAT_ID, self.GROUP_ADMIN_ID)] = _admin_member(self.GROUP_ADMIN_ID)
        self._patcher = patch.object(Bot, "__call__", new=self.fake_api)
        self._patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = moderator.config_from_bot_row(
            {"bot_id": 705, "name": "mod_cmds", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await moderator.init_db(self.config.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._patcher.stop()

    async def test_non_admin_cannot_add_stopword(self):
        await self.dp.feed_webhook_update(
            self.bot, _group_text_update(1, self.CHAT_ID, self.NON_ADMIN_ID, "/addstopword badword")
        )
        conn = sqlite3.connect(self.config.db_path)
        count = conn.execute("SELECT COUNT(*) FROM stopwords").fetchone()[0]
        conn.close()
        self.assertEqual(count, 0)
        self.assertTrue(any("⛔" in t for t in self.fake_api.sent_texts(self.CHAT_ID)))

    async def test_group_admin_can_add_and_list_stopwords(self):
        await self.dp.feed_webhook_update(
            self.bot, _group_text_update(1, self.CHAT_ID, self.GROUP_ADMIN_ID, "/addstopword badword")
        )
        conn = sqlite3.connect(self.config.db_path)
        rows = conn.execute("SELECT word FROM stopwords WHERE chat_id=?", (self.CHAT_ID,)).fetchall()
        conn.close()
        self.assertEqual(rows, [("badword",)])


class ModeratorRightsMechanism1_MyChatMemberTests(unittest.IsolatedAsyncioTestCase):
    """Mechanism #1: on_bot_membership_changed reacts to ANY status change of
    the bot itself — insufficient rights get instructions (even on the very
    first "added as plain member" step), sufficient rights stay silent."""

    CHAT_ID = -100444

    async def asyncSetUp(self):
        self.fake_api = FakeBotAPI()
        self._patcher = patch.object(Bot, "__call__", new=self.fake_api)
        self._patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = moderator.config_from_bot_row(
            {"bot_id": 706, "name": "mod_rights1", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await moderator.init_db(self.config.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._patcher.stop()

    async def test_added_as_plain_member_sends_instructions(self):
        await self.dp.feed_webhook_update(
            self.bot,
            _my_chat_member_update(1, self.CHAT_ID, _bot_user_json("left"), _bot_user_json("member")),
        )
        texts = self.fake_api.sent_texts(self.CHAT_ID)
        self.assertEqual(len(texts), 1)
        self.assertIn("недостаточно прав", texts[0])

    async def test_promoted_to_full_admin_stays_silent(self):
        await self.dp.feed_webhook_update(
            self.bot,
            _my_chat_member_update(1, self.CHAT_ID, _bot_user_json("member"), _bot_user_json("administrator", _FULL_ADMIN_RIGHTS_JSON)),
        )
        self.assertEqual(self.fake_api.sent_texts(self.CHAT_ID), [])

    async def test_demoted_to_partial_admin_rights_sends_instructions_again(self):
        await self.dp.feed_webhook_update(
            self.bot,
            _my_chat_member_update(
                1, self.CHAT_ID,
                _bot_user_json("administrator", _FULL_ADMIN_RIGHTS_JSON),
                _bot_user_json("administrator", _PARTIAL_ADMIN_RIGHTS_JSON),
            ),
        )
        texts = self.fake_api.sent_texts(self.CHAT_ID)
        self.assertEqual(len(texts), 1)
        self.assertIn("недостаточно прав", texts[0])


class ModeratorRightsMechanism2_CheckRightsCommandTests(unittest.IsolatedAsyncioTestCase):
    """Mechanism #2: /checkrights — group-admin-gated, always explicit
    (success message included, unlike mechanism #1's silent-on-OK)."""

    CHAT_ID = -100555
    GROUP_ADMIN_ID = 333
    NON_ADMIN_ID = 444

    async def asyncSetUp(self):
        self.fake_api = FakeBotAPI()
        self.fake_api.chat_member_responses[(self.CHAT_ID, self.GROUP_ADMIN_ID)] = _admin_member(self.GROUP_ADMIN_ID)
        self._patcher = patch.object(Bot, "__call__", new=self.fake_api)
        self._patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = moderator.config_from_bot_row(
            {"bot_id": 707, "name": "mod_rights2", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await moderator.init_db(self.config.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._patcher.stop()

    async def test_non_admin_caller_denied_without_checking_bot_status(self):
        await self.dp.feed_webhook_update(
            self.bot, _group_text_update(1, self.CHAT_ID, self.NON_ADMIN_ID, "/checkrights")
        )
        # Only ONE GetChatMember call — for the caller. The bot's own status
        # must never be looked up if the caller isn't even a group admin.
        gcm_calls = [c for c in self.fake_api.calls if isinstance(c, GetChatMember)]
        self.assertEqual(len(gcm_calls), 1)
        self.assertEqual(gcm_calls[0].user_id, self.NON_ADMIN_ID)
        self.assertTrue(any("⛔" in t for t in self.fake_api.sent_texts(self.CHAT_ID)))

    async def test_admin_caller_bot_insufficient_rights(self):
        self.fake_api.chat_member_responses[(self.CHAT_ID, BOT_USER_ID)] = _plain_member(BOT_USER_ID)
        await self.dp.feed_webhook_update(
            self.bot, _group_text_update(1, self.CHAT_ID, self.GROUP_ADMIN_ID, "/checkrights")
        )
        texts = self.fake_api.sent_texts(self.CHAT_ID)
        self.assertTrue(any("недостаточно прав" in t for t in texts))

    async def test_admin_caller_bot_sufficient_rights(self):
        self.fake_api.chat_member_responses[(self.CHAT_ID, BOT_USER_ID)] = _admin_member(BOT_USER_ID)
        await self.dp.feed_webhook_update(
            self.bot, _group_text_update(1, self.CHAT_ID, self.GROUP_ADMIN_ID, "/checkrights")
        )
        texts = self.fake_api.sent_texts(self.CHAT_ID)
        self.assertTrue(any("Всё в порядке" in t for t in texts))


class ModeratorRightsMechanism3_RuntimeFailureTests(unittest.IsolatedAsyncioTestCase):
    """Mechanism #3: a real moderation action failing at the moment it runs
    (rights revoked mid-flight) must DM admins_file admins explicitly — not
    fail silently, not spam the group."""

    CHAT_ID = -100666
    USER_ID = 999
    OWNER_ADMIN_ID = 5000

    async def asyncSetUp(self):
        self.fake_api = FakeBotAPI()
        self._patcher = patch.object(Bot, "__call__", new=self.fake_api)
        self._patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = moderator.config_from_bot_row(
            {"bot_id": 708, "name": "mod_rights3", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await moderator.init_db(self.config.db_path)
        moderator._save_admins(self.config.admins_file, {str(self.OWNER_ADMIN_ID)})
        self.bot, self.dp = _build_bot_dispatcher(self.config)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._patcher.stop()

    async def test_delete_failure_dms_admins_file_admin_not_the_group(self):
        from aiogram.methods import DeleteMessage
        self.fake_api.fail_methods.add(DeleteMessage)

        await self.dp.feed_webhook_update(
            self.bot, _group_text_update(1, self.CHAT_ID, self.USER_ID, "spam http://x.example")
        )

        dm_texts = self.fake_api.sent_texts(self.OWNER_ADMIN_ID)
        self.assertEqual(len(dm_texts), 1)
        self.assertIn("Не удалось выполнить модерацию", dm_texts[0])
        self.assertIn("delete_message", dm_texts[0])
        # The group itself must NOT receive the internal failure notice.
        group_texts = self.fake_api.sent_texts(self.CHAT_ID)
        self.assertFalse(any("Не удалось выполнить модерацию" in t for t in group_texts))

        # Escalation must still proceed despite the delete failure.
        conn = sqlite3.connect(self.config.db_path)
        action = conn.execute("SELECT action FROM moderation_log WHERE user_id=?", (self.USER_ID,)).fetchone()[0]
        conn.close()
        self.assertEqual(action, "warn")

    async def test_flood_control_retry_after_is_caught_not_left_unhandled(self):
        """Review-found blocker: _moderate_safely used to catch only
        TelegramBadRequest/TelegramForbiddenError. TelegramRetryAfter (flood
        control, HTTP 429) is NOT a subclass of either — exactly the error
        Telegram throws under heavy delete/mute/ban traffic, i.e. during the
        spam wave this whole mechanism exists for. An uncaught RetryAfter here
        would blow up moderate_message with no DM to admins and no log write
        at all. Fixed by catching the common TelegramAPIError base instead."""
        from aiogram.methods import DeleteMessage
        self.fake_api.retry_after_methods.add(DeleteMessage)

        await self.dp.feed_webhook_update(
            self.bot, _group_text_update(1, self.CHAT_ID, self.USER_ID, "spam http://retry.example")
        )

        dm_texts = self.fake_api.sent_texts(self.OWNER_ADMIN_ID)
        self.assertEqual(len(dm_texts), 1, "TelegramRetryAfter escaped _moderate_safely unhandled — no admin DM sent")

        conn = sqlite3.connect(self.config.db_path)
        action = conn.execute("SELECT action FROM moderation_log WHERE user_id=?", (self.USER_ID,)).fetchone()
        conn.close()
        self.assertIsNotNone(action, "escalation never ran — the RetryAfter exception must have propagated unhandled")
        self.assertEqual(action[0], "warn")

    async def test_mute_failure_dms_admin_and_log_still_shows_mute(self):
        from aiogram.methods import RestrictChatMember
        self.fake_api.fail_methods.add(RestrictChatMember)
        async with aiosqlite.connect(self.config.db_path) as db:
            await db.execute(
                "INSERT INTO chat_settings (chat_id, max_warnings, mute_minutes) VALUES (?,1,10)", (self.CHAT_ID,)
            )
            await db.commit()

        await self.dp.feed_webhook_update(self.bot, _group_text_update(1, self.CHAT_ID, self.USER_ID, "spam http://a.example"))
        await self.dp.feed_webhook_update(self.bot, _group_text_update(2, self.CHAT_ID, self.USER_ID, "spam http://b.example"))

        dm_texts = self.fake_api.sent_texts(self.OWNER_ADMIN_ID)
        self.assertTrue(any("mute" in t for t in dm_texts))

        conn = sqlite3.connect(self.config.db_path)
        stage = conn.execute("SELECT stage FROM warnings WHERE user_id=?", (self.USER_ID,)).fetchone()[0]
        actions = [r[0] for r in conn.execute("SELECT action FROM moderation_log ORDER BY id").fetchall()]
        conn.close()
        self.assertEqual(stage, "muted")
        self.assertEqual(actions, ["warn", "mute"])


class ModeratorStandaloneSmokeTest(unittest.TestCase):
    def test_config_from_env_matches_legacy_constant_shape(self):
        config = moderator.config_from_env()
        self.assertTrue(config.db_path.endswith("moderator_data.db"))
        self.assertEqual(config.bot_name, "moderator")

    def test_router_and_main_entrypoint_exist(self):
        self.assertTrue(hasattr(moderator, "router"))
        self.assertTrue(hasattr(moderator, "main"))


if __name__ == "__main__":
    unittest.main()
