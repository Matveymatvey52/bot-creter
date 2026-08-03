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
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import aiosqlite
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import (
    AnswerCallbackQuery, BanChatMember, DeleteMessage, EditMessageText, GetChatMember, RestrictChatMember,
    SendMessage,
)
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
        self.responses: list = []  # parallel to .calls — the value __call__ returned for each
        self.chat_member_responses: dict[tuple[int, int], object] = {}
        self.fail_methods: set[type] = set()
        self.retry_after_methods: set[type] = set()
        self._next_message_id = 1

    async def __call__(self, request, **kwargs):
        self.calls.append(request)
        if isinstance(request, GetChatMember):
            key = (request.chat_id, request.user_id)
            resp = self.chat_member_responses.get(key, _plain_member(request.user_id))
            self.responses.append(resp)
            return resp
        if type(request) in self.retry_after_methods:
            self.responses.append(None)
            raise TelegramRetryAfter(method=request, message="Too Many Requests: retry later", retry_after=1)
        if type(request) in self.fail_methods:
            self.responses.append(None)
            raise TelegramForbiddenError(method=request, message="Forbidden: bot has no rights")
        # moderator.py's _replace_panel reads .message_id off a send_message
        # response to track the panel it just showed — a bare object() (the
        # old stand-in, fine when nothing read attributes off it) doesn't
        # have one, so hand back an incrementing fake id instead. Kept in
        # .responses (parallel to .calls) so tests can look up exactly which
        # id a specific earlier send got, to assert it's the one later deleted.
        msg_id = self._next_message_id
        self._next_message_id += 1
        resp = SimpleNamespace(message_id=msg_id)
        self.responses.append(resp)
        return resp

    def last_sent_message_id(self, chat_id: int | None = None) -> int | None:
        for call, resp in zip(reversed(self.calls), reversed(self.responses)):
            if isinstance(call, SendMessage) and (chat_id is None or call.chat_id == chat_id):
                return resp.message_id
        return None

    def sent_texts(self, chat_id: int | None = None) -> list[str]:
        out = []
        for c in self.calls:
            if isinstance(c, SendMessage) and (chat_id is None or c.chat_id == chat_id):
                out.append(c.text)
        return out

    def edited_texts(self) -> list[str]:
        return [c.text for c in self.calls if isinstance(c, EditMessageText)]

    def alert_texts(self) -> list[str]:
        return [c.text for c in self.calls if isinstance(c, AnswerCallbackQuery) and c.show_alert]

    def deleted_message_ids(self, chat_id: int | None = None) -> list[int]:
        return [
            c.message_id for c in self.calls
            if isinstance(c, DeleteMessage) and (chat_id is None or c.chat_id == chat_id)
        ]


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


def _group_callback_update(update_id: int, chat_id: int, user_id: int, data: str) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": str(update_id),
            "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
            "message": {
                "message_id": update_id, "date": 1700000000,
                "chat": {"id": chat_id, "type": "supergroup", "title": "Test Group"}, "text": "placeholder",
            },
            "chat_instance": "1", "data": data,
        },
    }


def _private_text_update(update_id: int, user_id: int, text: str) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id, "date": 1700000000,
            "chat": {"id": user_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
            "text": text,
        },
    }


def _private_callback_update(update_id: int, user_id: int, data: str) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": str(update_id),
            "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
            "message": {
                "message_id": update_id, "date": 1700000000,
                "chat": {"id": user_id, "type": "private"}, "text": "placeholder",
            },
            "chat_instance": "1", "data": data,
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
        self.assertEqual(row[0], "запрещённое слово")


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


class ModeratorStartButtonMenuTests(unittest.IsolatedAsyncioTestCase):
    """/start's welcome message shows buttons instead of a raw command list;
    the group-scoped buttons (rights/stopwords) only redirect (no group
    context available from a private chat — see Variant A discussion),
    while /admins and /modlog buttons execute directly since they need none."""

    USER_ID = 12345

    async def asyncSetUp(self):
        self.fake_api = FakeBotAPI()
        self._patcher = patch.object(Bot, "__call__", new=self.fake_api)
        self._patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = moderator.config_from_bot_row(
            {"bot_id": 710, "name": "mod_start", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await moderator.init_db(self.config.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._patcher.stop()

    async def test_start_shows_buttons_not_a_raw_command_list(self):
        await self.dp.feed_webhook_update(self.bot, _private_text_update(1, self.USER_ID, "/start"))
        # Two sends for the very first user (welcome + one-time "you're the
        # bot admin" onboarding note) — the welcome message is always first.
        sends = [c for c in self.fake_api.calls if isinstance(c, SendMessage)]
        self.assertEqual(len(sends), 2)
        self.assertIsNotNone(sends[0].reply_markup)
        button_texts = {
            b.text for row in sends[0].reply_markup.inline_keyboard for b in row
        }
        self.assertEqual(
            button_texts,
            {"⚙️ Настроить группу", "👥 Админы", "📜 Журнал модерации"},
        )
        # No raw "/addstopword"-style command list in the welcome text itself.
        self.assertNotIn("/addstopword", sends[0].text)

    async def test_admins_button_executes_directly(self):
        await self.dp.feed_webhook_update(self.bot, _private_text_update(1, self.USER_ID, "/start"))
        await self.dp.feed_webhook_update(self.bot, _private_callback_update(2, self.USER_ID, "mod_admins"))
        texts = self.fake_api.sent_texts(self.USER_ID)
        self.assertTrue(any(str(self.USER_ID) in t for t in texts))

    async def test_modlog_button_executes_directly(self):
        await self.dp.feed_webhook_update(self.bot, _private_text_update(1, self.USER_ID, "/start"))
        await self.dp.feed_webhook_update(self.bot, _private_callback_update(2, self.USER_ID, "mod_modlog"))
        texts = self.fake_api.sent_texts(self.USER_ID)
        self.assertTrue(any("Журнал модерации пуст" in t for t in texts))

    async def test_switching_panels_deletes_the_previous_panel_message(self):
        """delete-old/show-new, same mechanic as tour_operator.py's
        cb_section — /start's welcome message itself is never touched (it's
        a one-off, not a tracked panel); only navigation BETWEEN the 4
        destination screens deletes the previous one."""
        uid = 1
        await self.dp.feed_webhook_update(self.bot, _private_text_update(uid, self.USER_ID, "/start")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _private_callback_update(uid, self.USER_ID, "mod_admins")); uid += 1
        admins_panel_id = self.fake_api.last_sent_message_id(self.USER_ID)
        self.assertEqual(self.fake_api.deleted_message_ids(self.USER_ID), [], "nothing to delete yet — first panel opened")

        await self.dp.feed_webhook_update(self.bot, _private_callback_update(uid, self.USER_ID, "mod_modlog")); uid += 1
        self.assertEqual(self.fake_api.deleted_message_ids(self.USER_ID), [admins_panel_id])
        modlog_panel_id = self.fake_api.last_sent_message_id(self.USER_ID)

        await self.dp.feed_webhook_update(self.bot, _private_callback_update(uid, self.USER_ID, "mod_pick_group")); uid += 1
        self.assertEqual(self.fake_api.deleted_message_ids(self.USER_ID), [admins_panel_id, modlog_panel_id])

    async def test_start_welcome_message_is_never_deleted_by_panel_navigation(self):
        uid = 1
        await self.dp.feed_webhook_update(self.bot, _private_text_update(uid, self.USER_ID, "/start")); uid += 1
        welcome_id = self.fake_api.last_sent_message_id(self.USER_ID)
        await self.dp.feed_webhook_update(self.bot, _private_callback_update(uid, self.USER_ID, "mod_admins")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _private_callback_update(uid, self.USER_ID, "mod_modlog")); uid += 1
        self.assertNotIn(welcome_id, self.fake_api.deleted_message_ids(self.USER_ID))


class ModeratorAdminsPanelButtonTests(unittest.IsolatedAsyncioTestCase):
    """Private-chat admins panel: "👥 Админы" now shows add/remove buttons
    instead of the raw /addadmin, /removeadmin command list; add/remove each
    drive a short FSM dialog, reusing the delete-old/show-new panel mechanic."""

    OWNER_ID = 700
    OTHER_ADMIN_ID = 701

    async def asyncSetUp(self):
        self.fake_api = FakeBotAPI()
        self._patcher = patch.object(Bot, "__call__", new=self.fake_api)
        self._patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = moderator.config_from_bot_row(
            {"bot_id": 712, "name": "mod_admins_panel", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await moderator.init_db(self.config.db_path)
        moderator._save_admins(self.config.admins_file, {str(self.OWNER_ID)})
        self.bot, self.dp = _build_bot_dispatcher(self.config)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._patcher.stop()

    async def test_admins_panel_shows_add_remove_buttons(self):
        await self.dp.feed_webhook_update(self.bot, _private_callback_update(1, self.OWNER_ID, "mod_admins"))
        sends = [c for c in self.fake_api.calls if isinstance(c, SendMessage)]
        self.assertEqual(len(sends), 1)
        button_texts = {b.text for row in sends[0].reply_markup.inline_keyboard for b in row}
        self.assertEqual(button_texts, {"➕ Добавить админа", "➖ Убрать админа"})
        # No raw "/addadmin"-style instructions in the panel text itself.
        self.assertNotIn("/addadmin", sends[0].text)

    async def test_admins_list_text_order_matches_removal_picker_order(self):
        """Review-found: _admins_list_text iterated the raw set (Python
        string-hash order) while cb_removeadmin_start's picker always builds
        buttons from sorted(_load_admins(...)) — the displayed list and the
        numbered removal buttons could silently disagree on order. Both must
        now agree on sorted() order."""
        ids = {"9", "700", "80", str(self.OTHER_ADMIN_ID)}
        moderator._save_admins(self.config.admins_file, {str(self.OWNER_ID)} | ids)
        text = await moderator._admins_list_text(self.config)
        listed_order = [line.split("<code>")[1].split("</code>")[0] for line in text.splitlines() if "<code>" in line]
        self.assertEqual(listed_order, sorted({str(self.OWNER_ID)} | ids))

    async def test_add_admin_flow_via_buttons(self):
        uid = 1
        await self.dp.feed_webhook_update(self.bot, _private_callback_update(uid, self.OWNER_ID, "mod_addadmin")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _private_text_update(uid, self.OWNER_ID, str(self.OTHER_ADMIN_ID))); uid += 1

        ids = moderator._load_admins(self.config.admins_file)
        self.assertIn(str(self.OTHER_ADMIN_ID), ids)
        self.assertTrue(any("добавлен" in t for t in self.fake_api.sent_texts(self.OWNER_ID)))

    async def test_add_admin_rejects_non_numeric_id(self):
        uid = 1
        await self.dp.feed_webhook_update(self.bot, _private_callback_update(uid, self.OWNER_ID, "mod_addadmin")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _private_text_update(uid, self.OWNER_ID, "not-an-id")); uid += 1
        ids = moderator._load_admins(self.config.admins_file)
        self.assertNotIn("not-an-id", ids)

    async def test_non_admin_cannot_open_addadmin(self):
        non_admin = 999
        await self.dp.feed_webhook_update(self.bot, _private_callback_update(1, non_admin, "mod_addadmin"))
        ids_before = moderator._load_admins(self.config.admins_file)
        # The prompt was never shown, so a follow-up ID-looking message must
        # not be captured by the flow either.
        await self.dp.feed_webhook_update(self.bot, _private_text_update(2, non_admin, "12345"))
        self.assertEqual(moderator._load_admins(self.config.admins_file), ids_before)
        self.assertTrue(any("Нет доступа" in t for t in self.fake_api.sent_texts(non_admin)))

    async def test_remove_admin_flow_via_pick_buttons(self):
        moderator._save_admins(self.config.admins_file, {str(self.OWNER_ID), str(self.OTHER_ADMIN_ID)})
        uid = 1
        await self.dp.feed_webhook_update(self.bot, _private_callback_update(uid, self.OWNER_ID, "mod_removeadmin")); uid += 1
        sends = [c for c in self.fake_api.calls if isinstance(c, SendMessage)]
        pick_kb = sends[-1].reply_markup
        target_data = next(
            b.callback_data for row in pick_kb.inline_keyboard for b in row
            if b.text == str(self.OTHER_ADMIN_ID)
        )
        await self.dp.feed_webhook_update(self.bot, _private_callback_update(uid, self.OWNER_ID, target_data)); uid += 1

        ids = moderator._load_admins(self.config.admins_file)
        self.assertNotIn(str(self.OTHER_ADMIN_ID), ids)
        self.assertIn(str(self.OWNER_ID), ids)

    async def test_remove_admin_falls_back_to_text_when_too_many_admins(self):
        many_ids = {str(self.OWNER_ID)} | {str(1000 + i) for i in range(moderator.MAX_ADMIN_REMOVE_BUTTONS + 1)}
        moderator._save_admins(self.config.admins_file, many_ids)
        uid = 1
        await self.dp.feed_webhook_update(self.bot, _private_callback_update(uid, self.OWNER_ID, "mod_removeadmin")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _private_text_update(uid, self.OWNER_ID, "1000")); uid += 1
        ids = moderator._load_admins(self.config.admins_file)
        self.assertNotIn("1000", ids)

    async def test_stray_text_while_picking_admin_gets_a_reply_not_silence(self):
        moderator._save_admins(self.config.admins_file, {str(self.OWNER_ID), str(self.OTHER_ADMIN_ID)})
        uid = 1
        await self.dp.feed_webhook_update(self.bot, _private_callback_update(uid, self.OWNER_ID, "mod_removeadmin")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _private_text_update(uid, self.OWNER_ID, str(self.OTHER_ADMIN_ID))); uid += 1
        self.assertTrue(any("кнопкой" in t for t in self.fake_api.sent_texts(self.OWNER_ID)))
        ids = moderator._load_admins(self.config.admins_file)
        self.assertIn(str(self.OTHER_ADMIN_ID), ids, "typed text must not be accepted as a removal in the pick-button state")

    async def test_cancel_returns_to_admins_panel(self):
        uid = 1
        await self.dp.feed_webhook_update(self.bot, _private_callback_update(uid, self.OWNER_ID, "mod_addadmin")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _private_callback_update(uid, self.OWNER_ID, "mod_admin_cancel")); uid += 1
        # The cancelled prompt must not still be listening for an ID.
        await self.dp.feed_webhook_update(self.bot, _private_text_update(uid, self.OWNER_ID, str(self.OTHER_ADMIN_ID))); uid += 1
        ids = moderator._load_admins(self.config.admins_file)
        self.assertNotIn(str(self.OTHER_ADMIN_ID), ids)
        # The admins panel (with its add/remove buttons) must be reshown after cancel.
        panel_sends = [
            c for c in self.fake_api.calls
            if isinstance(c, SendMessage) and c.reply_markup and any(
                b.callback_data == "mod_addadmin" for row in c.reply_markup.inline_keyboard for b in row
            )
        ]
        self.assertGreaterEqual(len(panel_sends), 1)

    async def test_navigating_from_addadmin_prompt_to_modlog_deletes_the_prompt(self):
        """Cross-flow navigation: the admin add-prompt is itself a tracked
        panel — opening a DIFFERENT destination from /start must delete it,
        just like switching between the 4 top-level destinations does."""
        uid = 1
        await self.dp.feed_webhook_update(self.bot, _private_callback_update(uid, self.OWNER_ID, "mod_admins")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _private_callback_update(uid, self.OWNER_ID, "mod_addadmin")); uid += 1
        prompt_id = self.fake_api.last_sent_message_id(self.OWNER_ID)
        await self.dp.feed_webhook_update(self.bot, _private_callback_update(uid, self.OWNER_ID, "mod_modlog")); uid += 1
        self.assertIn(prompt_id, self.fake_api.deleted_message_ids(self.OWNER_ID))

    async def test_navigating_away_from_addadmin_prompt_cancels_the_flow(self):
        """Review-found blocker: navigating to a DIFFERENT destination used to
        leave AdminPanelFlow.add_admin still active — the user's next
        ordinary numeric-looking message (unrelated to admin management)
        would then be silently accepted as a new bot admin."""
        uid = 1
        await self.dp.feed_webhook_update(self.bot, _private_callback_update(uid, self.OWNER_ID, "mod_addadmin")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _private_callback_update(uid, self.OWNER_ID, "mod_modlog")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _private_text_update(uid, self.OWNER_ID, "5551234")); uid += 1
        ids = moderator._load_admins(self.config.admins_file)
        self.assertNotIn("5551234", ids, "an unrelated number after navigating away was captured as a new admin")

    async def test_successful_add_admin_deletes_the_prompt_message(self):
        """Review-found blocker: every flow-completion path called
        state.clear() BEFORE _replace_panel(), wiping panel_msg_id first so
        the prompt/pick-list from the step just finished was never deleted —
        this asserts the fix (_clear_flow_keep_panel) actually works."""
        uid = 1
        await self.dp.feed_webhook_update(self.bot, _private_callback_update(uid, self.OWNER_ID, "mod_addadmin")); uid += 1
        prompt_id = self.fake_api.last_sent_message_id(self.OWNER_ID)
        await self.dp.feed_webhook_update(self.bot, _private_text_update(uid, self.OWNER_ID, str(self.OTHER_ADMIN_ID))); uid += 1
        self.assertIn(prompt_id, self.fake_api.deleted_message_ids(self.OWNER_ID))

    async def test_successful_remove_admin_pick_deletes_the_pick_list_message(self):
        moderator._save_admins(self.config.admins_file, {str(self.OWNER_ID), str(self.OTHER_ADMIN_ID)})
        uid = 1
        await self.dp.feed_webhook_update(self.bot, _private_callback_update(uid, self.OWNER_ID, "mod_removeadmin")); uid += 1
        pick_list_id = self.fake_api.last_sent_message_id(self.OWNER_ID)
        sends = [c for c in self.fake_api.calls if isinstance(c, SendMessage)]
        target_data = next(
            b.callback_data for row in sends[-1].reply_markup.inline_keyboard for b in row
            if b.text == str(self.OTHER_ADMIN_ID)
        )
        await self.dp.feed_webhook_update(self.bot, _private_callback_update(uid, self.OWNER_ID, target_data)); uid += 1
        self.assertIn(pick_list_id, self.fake_api.deleted_message_ids(self.OWNER_ID))

    async def test_cannot_remove_last_admin_via_pick_button(self):
        uid = 1
        await self.dp.feed_webhook_update(self.bot, _private_callback_update(uid, self.OWNER_ID, "mod_removeadmin")); uid += 1
        sends = [c for c in self.fake_api.calls if isinstance(c, SendMessage)]
        self.assertTrue(any("единственный администратор" in s.text for s in sends))
        ids = moderator._load_admins(self.config.admins_file)
        self.assertEqual(ids, {str(self.OWNER_ID)})

    async def test_cannot_remove_last_admin_via_text_fallback(self):
        """Defense-in-depth: the text-fallback path is entered while the
        admin count is still > MAX_ADMIN_REMOVE_BUTTONS, but by the time the
        typed ID is processed a CONCURRENT change (another admin, or this
        same admin via a second device) has already dropped the count to 1 —
        the handler's own re-check must refuse rather than trust the
        snapshot from when the flow started."""
        many_ids = {str(self.OWNER_ID)} | {str(1000 + i) for i in range(moderator.MAX_ADMIN_REMOVE_BUTTONS + 1)}
        moderator._save_admins(self.config.admins_file, many_ids)
        uid = 1
        await self.dp.feed_webhook_update(self.bot, _private_callback_update(uid, self.OWNER_ID, "mod_removeadmin")); uid += 1
        # Simulate everyone else being removed concurrently, via another path.
        moderator._save_admins(self.config.admins_file, {str(self.OWNER_ID)})
        await self.dp.feed_webhook_update(self.bot, _private_text_update(uid, self.OWNER_ID, str(self.OWNER_ID))); uid += 1
        ids = moderator._load_admins(self.config.admins_file)
        self.assertEqual(ids, {str(self.OWNER_ID)})
        self.assertTrue(any("единственного администратора" in t for t in self.fake_api.sent_texts(self.OWNER_ID)))

    async def test_removeadmin_pick_reports_honestly_if_already_removed(self):
        moderator._save_admins(self.config.admins_file, {str(self.OWNER_ID), str(self.OTHER_ADMIN_ID)})
        uid = 1
        await self.dp.feed_webhook_update(self.bot, _private_callback_update(uid, self.OWNER_ID, "mod_removeadmin")); uid += 1
        sends = [c for c in self.fake_api.calls if isinstance(c, SendMessage)]
        target_data = next(
            b.callback_data for row in sends[-1].reply_markup.inline_keyboard for b in row
            if b.text == str(self.OTHER_ADMIN_ID)
        )
        # Removed by another path (raw command) between the snapshot and the button press.
        moderator._save_admins(self.config.admins_file, {str(self.OWNER_ID)})
        await self.dp.feed_webhook_update(self.bot, _private_callback_update(uid, self.OWNER_ID, target_data)); uid += 1
        self.assertTrue(any("уже не администратор" in t for t in self.fake_api.sent_texts(self.OWNER_ID)))

    async def test_add_admin_rejects_oversized_id(self):
        """Review-found blocker: an unbounded numeric string would still pass
        isdigit(), get stored, and then blow past Telegram's message-length
        limit on EVERY future panel render for ALL admins."""
        uid = 1
        await self.dp.feed_webhook_update(self.bot, _private_callback_update(uid, self.OWNER_ID, "mod_addadmin")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _private_text_update(uid, self.OWNER_ID, "1" * 50)); uid += 1
        ids = moderator._load_admins(self.config.admins_file)
        self.assertNotIn("1" * 50, ids)

    async def test_add_admin_rejects_unicode_lookalike_digits(self):
        """Fullwidth Unicode digits pass str.isdigit() but can never match a
        real Telegram user id (always plain ASCII) — would create a
        permanently-unmatchable phantom admin entry."""
        uid = 1
        await self.dp.feed_webhook_update(self.bot, _private_callback_update(uid, self.OWNER_ID, "mod_addadmin")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _private_text_update(uid, self.OWNER_ID, "１２３")); uid += 1
        ids = moderator._load_admins(self.config.admins_file)
        self.assertNotIn("１２３", ids)

    async def test_add_admin_rejects_zero_and_leading_zeros(self):
        """Review-found lockout: "0" and leading-zero strings ("007") pass a
        bare isdigit() check but can never match a real Telegram user_id
        (never 0, never leading-zero), so they'd inflate len(ids) past the
        last-admin guard without ever being removable/matchable — the sole
        admin could add "0", remove themselves (guard sees 2 admins), and
        permanently lose admin access through the normal UI."""
        for phantom_id in ("0", "007", "00"):
            uid = 1
            await self.dp.feed_webhook_update(self.bot, _private_callback_update(uid, self.OWNER_ID, "mod_addadmin")); uid += 1
            await self.dp.feed_webhook_update(self.bot, _private_text_update(uid, self.OWNER_ID, phantom_id)); uid += 1
            ids = moderator._load_admins(self.config.admins_file)
            self.assertNotIn(phantom_id, ids, f"{phantom_id!r} must not be accepted as an admin id")
            self.assertTrue(
                any("числовой" in t for t in self.fake_api.sent_texts(self.OWNER_ID)),
                "rejection must produce a clear message to the admin",
            )

    async def test_add_admin_flow_expires_like_group_flow(self):
        uid = 1
        await self.dp.feed_webhook_update(self.bot, _private_callback_update(uid, self.OWNER_ID, "mod_addadmin")); uid += 1
        with patch("templates.moderator.time") as fake_time:
            fake_time.time.return_value = time.time() + moderator.FLOW_TIMEOUT_SECONDS + 1
            await self.dp.feed_webhook_update(self.bot, _private_text_update(uid, self.OWNER_ID, str(self.OTHER_ADMIN_ID))); uid += 1
        ids = moderator._load_admins(self.config.admins_file)
        self.assertNotIn(str(self.OTHER_ADMIN_ID), ids)
        self.assertTrue(any("Время ожидания истекло" in t for t in self.fake_api.sent_texts(self.OWNER_ID)))


class ModeratorStopwordsPanelButtonTests(unittest.IsolatedAsyncioTestCase):
    """Group-side /stopwords panel: buttons for add/remove/threshold, each
    driving a short FSM dialog, re-verifying live group-admin status on every
    button press (the panel message is visible to the WHOLE group, not just
    whoever ran /stopwords)."""

    CHAT_ID = -100999
    ADMIN_ID = 501
    OTHER_ADMIN_ID = 502
    NON_ADMIN_ID = 503

    async def asyncSetUp(self):
        self.fake_api = FakeBotAPI()
        self.fake_api.chat_member_responses[(self.CHAT_ID, self.ADMIN_ID)] = _admin_member(self.ADMIN_ID)
        self.fake_api.chat_member_responses[(self.CHAT_ID, self.OTHER_ADMIN_ID)] = _admin_member(self.OTHER_ADMIN_ID)
        self._patcher = patch.object(Bot, "__call__", new=self.fake_api)
        self._patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = moderator.config_from_bot_row(
            {"bot_id": 711, "name": "mod_panel", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await moderator.init_db(self.config.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._patcher.stop()

    async def test_stopwords_command_shows_panel_buttons(self):
        await self.dp.feed_webhook_update(self.bot, _group_text_update(1, self.CHAT_ID, self.ADMIN_ID, "/stopwords"))
        sends = [c for c in self.fake_api.calls if isinstance(c, SendMessage)]
        self.assertEqual(len(sends), 1)
        button_texts = {b.text for row in sends[0].reply_markup.inline_keyboard for b in row}
        self.assertEqual(
            button_texts,
            {"➕ Добавить запрещённое слово", "➖ Убрать запрещённое слово", "⚙️ Порог предупреждений"},
        )

    async def test_add_word_flow_via_buttons(self):
        uid = 1
        await self.dp.feed_webhook_update(self.bot, _group_text_update(uid, self.CHAT_ID, self.ADMIN_ID, "/stopwords")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _group_callback_update(uid, self.CHAT_ID, self.ADMIN_ID, "mod_addword")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _group_text_update(uid, self.CHAT_ID, self.ADMIN_ID, "badword")); uid += 1

        conn = sqlite3.connect(self.config.db_path)
        rows = conn.execute("SELECT word FROM stopwords WHERE chat_id=?", (self.CHAT_ID,)).fetchall()
        conn.close()
        self.assertEqual(rows, [("badword",)])
        self.assertTrue(any("Добавлено в список запрещённых слов" in t for t in self.fake_api.sent_texts(self.CHAT_ID)))

    async def test_add_word_rejects_multi_word_input(self):
        uid = 1
        await self.dp.feed_webhook_update(self.bot, _group_callback_update(uid, self.CHAT_ID, self.ADMIN_ID, "mod_addword")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _group_text_update(uid, self.CHAT_ID, self.ADMIN_ID, "two words")); uid += 1
        conn = sqlite3.connect(self.config.db_path)
        count = conn.execute("SELECT COUNT(*) FROM stopwords").fetchone()[0]
        conn.close()
        self.assertEqual(count, 0)

    async def test_non_admin_pressing_addword_is_rejected_with_alert(self):
        await self.dp.feed_webhook_update(
            self.bot, _group_callback_update(1, self.CHAT_ID, self.NON_ADMIN_ID, "mod_addword")
        )
        self.assertTrue(any("администраторам" in t for t in self.fake_api.alert_texts()))
        # No prompt was edited in — the non-admin's press did nothing.
        self.assertEqual(self.fake_api.edited_texts(), [])

    async def test_remove_word_flow_via_pick_buttons(self):
        async with aiosqlite.connect(self.config.db_path) as db:
            await db.execute("INSERT INTO stopwords (chat_id, word) VALUES (?,?)", (self.CHAT_ID, "removeme"))
            await db.commit()
        uid = 1
        await self.dp.feed_webhook_update(self.bot, _group_callback_update(uid, self.CHAT_ID, self.ADMIN_ID, "mod_removeword")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _group_callback_update(uid, self.CHAT_ID, self.ADMIN_ID, "mod_rmw:0")); uid += 1

        conn = sqlite3.connect(self.config.db_path)
        count = conn.execute("SELECT COUNT(*) FROM stopwords WHERE chat_id=?", (self.CHAT_ID,)).fetchone()[0]
        conn.close()
        self.assertEqual(count, 0)
        self.assertTrue(any("Убрано из списка запрещённых слов" in t for t in self.fake_api.edited_texts()))

    async def test_remove_word_falls_back_to_text_when_too_many_words(self):
        async with aiosqlite.connect(self.config.db_path) as db:
            for i in range(moderator.MAX_REMOVE_BUTTONS + 1):
                await db.execute("INSERT INTO stopwords (chat_id, word) VALUES (?,?)", (self.CHAT_ID, f"word{i}"))
            await db.commit()
        uid = 1
        await self.dp.feed_webhook_update(self.bot, _group_callback_update(uid, self.CHAT_ID, self.ADMIN_ID, "mod_removeword")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _group_text_update(uid, self.CHAT_ID, self.ADMIN_ID, "word3")); uid += 1

        conn = sqlite3.connect(self.config.db_path)
        rows = {r[0] for r in conn.execute("SELECT word FROM stopwords WHERE chat_id=?", (self.CHAT_ID,)).fetchall()}
        conn.close()
        self.assertNotIn("word3", rows)

    async def test_set_max_warnings_flow_via_buttons(self):
        uid = 1
        await self.dp.feed_webhook_update(self.bot, _group_callback_update(uid, self.CHAT_ID, self.ADMIN_ID, "mod_setwarn")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _group_text_update(uid, self.CHAT_ID, self.ADMIN_ID, "10")); uid += 1

        conn = sqlite3.connect(self.config.db_path)
        n = conn.execute("SELECT max_warnings FROM chat_settings WHERE chat_id=?", (self.CHAT_ID,)).fetchone()[0]
        conn.close()
        self.assertEqual(n, 10)
        self.assertTrue(any("Порог предупреждений установлен: 10" in t for t in self.fake_api.sent_texts(self.CHAT_ID)))

    async def test_set_max_warnings_rejects_out_of_range(self):
        uid = 1
        await self.dp.feed_webhook_update(self.bot, _group_callback_update(uid, self.CHAT_ID, self.ADMIN_ID, "mod_setwarn")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _group_text_update(uid, self.CHAT_ID, self.ADMIN_ID, "0")); uid += 1
        conn = sqlite3.connect(self.config.db_path)
        row = conn.execute("SELECT max_warnings FROM chat_settings WHERE chat_id=?", (self.CHAT_ID,)).fetchone()
        conn.close()
        self.assertIsNone(row)  # never wrote a row — input was rejected first

    async def test_cancel_button_clears_state(self):
        uid = 1
        await self.dp.feed_webhook_update(self.bot, _group_callback_update(uid, self.CHAT_ID, self.ADMIN_ID, "mod_addword")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _group_callback_update(uid, self.CHAT_ID, self.ADMIN_ID, "mod_cancel")); uid += 1
        # A plain follow-up message must NOT be swallowed as the pending "add word" reply.
        await self.dp.feed_webhook_update(self.bot, _group_text_update(uid, self.CHAT_ID, self.ADMIN_ID, "just chatting")); uid += 1
        conn = sqlite3.connect(self.config.db_path)
        count = conn.execute("SELECT COUNT(*) FROM stopwords").fetchone()[0]
        conn.close()
        self.assertEqual(count, 0)

    async def test_non_admin_pressing_cancel_does_not_clear_the_real_admins_flow(self):
        """Review-found: cb_mod_cancel used to be the one panel callback that
        skipped the admin re-check — any group member could tap "❌ Отмена"
        and edit the shared panel message, even though it's the REAL admin's
        FSM state (keyed by the admin's own user_id) that actually matters."""
        uid = 1
        await self.dp.feed_webhook_update(self.bot, _group_callback_update(uid, self.CHAT_ID, self.ADMIN_ID, "mod_addword")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _group_callback_update(uid, self.CHAT_ID, self.NON_ADMIN_ID, "mod_cancel")); uid += 1
        self.assertTrue(any("администраторам" in t for t in self.fake_api.alert_texts()))
        # The admin's own flow must still be alive: their next word still lands.
        await self.dp.feed_webhook_update(self.bot, _group_text_update(uid, self.CHAT_ID, self.ADMIN_ID, "stillpending")); uid += 1
        conn = sqlite3.connect(self.config.db_path)
        rows = conn.execute("SELECT word FROM stopwords WHERE chat_id=?", (self.CHAT_ID,)).fetchall()
        conn.close()
        self.assertEqual(rows, [("stillpending",)])

    async def test_stale_flow_expires_instead_of_accepting_unrelated_later_message(self):
        """Review-found blocker: ModPanelFlow states had no expiry — a distracted
        admin's ANY later short message (unrelated ordinary chat) would silently
        be accepted as the pending flow's reply. FLOW_TIMEOUT_SECONDS + the
        started_at stamp fix this; simulate the clock moving past the window."""
        uid = 1
        await self.dp.feed_webhook_update(self.bot, _group_callback_update(uid, self.CHAT_ID, self.ADMIN_ID, "mod_addword")); uid += 1
        with patch("templates.moderator.time") as fake_time:
            fake_time.time.return_value = time.time() + moderator.FLOW_TIMEOUT_SECONDS + 1
            await self.dp.feed_webhook_update(self.bot, _group_text_update(uid, self.CHAT_ID, self.ADMIN_ID, "toolate")); uid += 1
        conn = sqlite3.connect(self.config.db_path)
        rows = conn.execute("SELECT word FROM stopwords WHERE chat_id=?", (self.CHAT_ID,)).fetchall()
        conn.close()
        self.assertEqual(rows, [], "a stale flow accepted an unrelated later message as its reply")
        self.assertTrue(any("Время ожидания истекло" in t for t in self.fake_api.sent_texts(self.CHAT_ID)))

    async def test_admin_rights_revoked_mid_flow_cancels_instead_of_acting(self):
        uid = 1
        await self.dp.feed_webhook_update(self.bot, _group_callback_update(uid, self.CHAT_ID, self.ADMIN_ID, "mod_addword")); uid += 1
        # Rights revoked between the button press and the reply.
        self.fake_api.chat_member_responses[(self.CHAT_ID, self.ADMIN_ID)] = _plain_member(self.ADMIN_ID)
        await self.dp.feed_webhook_update(self.bot, _group_text_update(uid, self.CHAT_ID, self.ADMIN_ID, "badword")); uid += 1
        conn = sqlite3.connect(self.config.db_path)
        count = conn.execute("SELECT COUNT(*) FROM stopwords").fetchone()[0]
        conn.close()
        self.assertEqual(count, 0)

    async def test_stray_text_while_picking_a_word_gets_a_reply_not_silence(self):
        async with aiosqlite.connect(self.config.db_path) as db:
            await db.execute("INSERT INTO stopwords (chat_id, word) VALUES (?,?)", (self.CHAT_ID, "removeme"))
            await db.commit()
        uid = 1
        await self.dp.feed_webhook_update(self.bot, _group_callback_update(uid, self.CHAT_ID, self.ADMIN_ID, "mod_removeword")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _group_text_update(uid, self.CHAT_ID, self.ADMIN_ID, "removeme")); uid += 1
        self.assertTrue(any("кнопкой" in t for t in self.fake_api.sent_texts(self.CHAT_ID)))
        conn = sqlite3.connect(self.config.db_path)
        count = conn.execute("SELECT COUNT(*) FROM stopwords WHERE chat_id=?", (self.CHAT_ID,)).fetchone()[0]
        conn.close()
        self.assertEqual(count, 1, "typed text must not be accepted as a removal in the pick-button state")

    async def test_removeword_pick_already_removed_reports_honestly(self):
        async with aiosqlite.connect(self.config.db_path) as db:
            await db.execute("INSERT INTO stopwords (chat_id, word) VALUES (?,?)", (self.CHAT_ID, "racy"))
            await db.commit()
        uid = 1
        await self.dp.feed_webhook_update(self.bot, _group_callback_update(uid, self.CHAT_ID, self.ADMIN_ID, "mod_removeword")); uid += 1
        # Someone else removes it first (raw command), between the snapshot and the button press.
        async with aiosqlite.connect(self.config.db_path) as db:
            await db.execute("DELETE FROM stopwords WHERE chat_id=? AND word=?", (self.CHAT_ID, "racy"))
            await db.commit()
        await self.dp.feed_webhook_update(self.bot, _group_callback_update(uid, self.CHAT_ID, self.ADMIN_ID, "mod_rmw:0")); uid += 1
        self.assertTrue(any("уже не в списке" in t for t in self.fake_api.edited_texts()))

    async def test_removeword_pick_malformed_callback_data_does_not_crash(self):
        async with aiosqlite.connect(self.config.db_path) as db:
            await db.execute("INSERT INTO stopwords (chat_id, word) VALUES (?,?)", (self.CHAT_ID, "removeme"))
            await db.commit()
        uid = 1
        await self.dp.feed_webhook_update(self.bot, _group_callback_update(uid, self.CHAT_ID, self.ADMIN_ID, "mod_removeword")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _group_callback_update(uid, self.CHAT_ID, self.ADMIN_ID, "mod_rmw:not-a-number")); uid += 1
        self.assertTrue(any("устарел" in t for t in self.fake_api.edited_texts()))
        conn = sqlite3.connect(self.config.db_path)
        count = conn.execute("SELECT COUNT(*) FROM stopwords WHERE chat_id=?", (self.CHAT_ID,)).fetchone()[0]
        conn.close()
        self.assertEqual(count, 1, "the word must survive a malformed callback instead of being guessed at")

    async def test_mid_flow_reply_is_not_deleted_as_ordinary_moderated_content(self):
        """StateFilter(None) regression guard: without it, moderate_message
        would claim the admin's "badword" reply (group chat, not a command)
        before the ModPanelFlow handler ever saw it."""
        uid = 1
        await self.dp.feed_webhook_update(self.bot, _group_callback_update(uid, self.CHAT_ID, self.ADMIN_ID, "mod_addword")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _group_text_update(uid, self.CHAT_ID, self.ADMIN_ID, "badword")); uid += 1
        conn = sqlite3.connect(self.config.db_path)
        rows = conn.execute("SELECT word FROM stopwords WHERE chat_id=?", (self.CHAT_ID,)).fetchall()
        conn.close()
        self.assertEqual(rows, [("badword",)], "the FSM reply was swallowed by moderate_message instead")

    async def test_other_admin_can_independently_start_own_flow(self):
        """FSM state is keyed per (chat, user) — a second admin pressing the
        SAME panel buttons must not collide with the first admin's in-flight
        flow."""
        uid = 1
        await self.dp.feed_webhook_update(self.bot, _group_callback_update(uid, self.CHAT_ID, self.ADMIN_ID, "mod_addword")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _group_callback_update(uid, self.CHAT_ID, self.OTHER_ADMIN_ID, "mod_setwarn")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _group_text_update(uid, self.CHAT_ID, self.OTHER_ADMIN_ID, "5")); uid += 1
        await self.dp.feed_webhook_update(self.bot, _group_text_update(uid, self.CHAT_ID, self.ADMIN_ID, "goodword")); uid += 1

        conn = sqlite3.connect(self.config.db_path)
        max_warn = conn.execute("SELECT max_warnings FROM chat_settings WHERE chat_id=?", (self.CHAT_ID,)).fetchone()[0]
        words = conn.execute("SELECT word FROM stopwords WHERE chat_id=?", (self.CHAT_ID,)).fetchall()
        conn.close()
        self.assertEqual(max_warn, 5)
        self.assertEqual(words, [("goodword",)])


class ModeratorTerminologyTests(unittest.TestCase):
    """Owner requirement: the user-facing term is "запрещённое слово", never
    the old "стоп-слово" — in buttons, confirmations, warning texts, or
    /modlog entries. Internal identifiers (the /addstopword-family COMMAND
    NAMES, the `stopwords` DB table/column) are explicitly allowed to keep
    their old names per the task; only text a Telegram user actually SEES is
    in scope here."""

    def test_no_old_term_in_source_outside_the_design_doc_reference(self):
        source_path = Path(moderator.__file__)
        lines = source_path.read_text(encoding="utf-8").splitlines()
        leaked = [
            (i + 1, line) for i, line in enumerate(lines)
            if "стоп-слов" in line.lower() and "stage2_design.md" not in line.lower()
        ]
        self.assertEqual(leaked, [], f"old term 'стоп-слово' leaked into source: {leaked}")

    def test_bot_description_and_welcome_text_use_new_term(self):
        self.assertNotIn("стоп", moderator.BOT_DESCRIPTION.lower())
        self.assertNotIn("стоп", moderator.WELCOME_TEXT.lower())


class ModeratorGroupPickerTests(unittest.IsolatedAsyncioTestCase):
    """"⚙️ Настроить группу" — known_groups is populated by
    on_bot_membership_changed as the bot is added to/removed from groups; the
    private-chat picker lists exactly those groups, and picking one requires
    the PRESSER's own live admin status in THAT SPECIFIC group — being an
    admin of one group the bot moderates must not grant access to another."""

    CHAT_A = -100111
    CHAT_B = -100222
    ADMIN_A_ID = 9001   # admin of CHAT_A only
    ADMIN_B_ID = 9002   # admin of CHAT_B only

    async def asyncSetUp(self):
        self.fake_api = FakeBotAPI()
        self.fake_api.chat_member_responses[(self.CHAT_A, self.ADMIN_A_ID)] = _admin_member(self.ADMIN_A_ID)
        self.fake_api.chat_member_responses[(self.CHAT_B, self.ADMIN_B_ID)] = _admin_member(self.ADMIN_B_ID)
        self._patcher = patch.object(Bot, "__call__", new=self.fake_api)
        self._patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = moderator.config_from_bot_row(
            {"bot_id": 720, "name": "mod_picker", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await moderator.init_db(self.config.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)
        # Populate known_groups the same way production does — drive the bot
        # being added as a member through on_bot_membership_changed, not a
        # direct DB write.
        await self.dp.feed_webhook_update(
            self.bot, _my_chat_member_update(1, self.CHAT_A, _bot_user_json("left"), _bot_user_json("member")),
        )
        await self.dp.feed_webhook_update(
            self.bot, _my_chat_member_update(2, self.CHAT_B, _bot_user_json("left"), _bot_user_json("member")),
        )

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._patcher.stop()

    async def test_known_groups_persisted_on_membership_change(self):
        groups = await moderator._known_groups(self.config.db_path)
        self.assertEqual({g[0] for g in groups}, {self.CHAT_A, self.CHAT_B})

    async def test_group_removed_from_known_groups_on_kick(self):
        await self.dp.feed_webhook_update(
            self.bot,
            _my_chat_member_update(3, self.CHAT_A, _bot_user_json("member"), _bot_user_json("kicked", {"until_date": 0})),
        )
        groups = await moderator._known_groups(self.config.db_path)
        self.assertEqual({g[0] for g in groups}, {self.CHAT_B})

    async def test_empty_picker_when_bot_in_no_groups(self):
        tmp2 = tempfile.TemporaryDirectory()
        try:
            config2 = moderator.config_from_bot_row(
                {"bot_id": 721, "name": "mod_picker_empty", "display_name": None, "group_chat_id": None},
                Path(tmp2.name),
            )
            await moderator.init_db(config2.db_path)
            bot2, dp2 = _build_bot_dispatcher(config2)
            await dp2.feed_webhook_update(bot2, _private_callback_update(1, self.ADMIN_A_ID, "mod_pick_group"))
            texts = self.fake_api.sent_texts(self.ADMIN_A_ID)
            self.assertTrue(any("не состоит ни в одной группе" in t for t in texts))
        finally:
            tmp2.cleanup()

    async def test_picker_lists_both_known_groups(self):
        await self.dp.feed_webhook_update(self.bot, _private_callback_update(10, self.ADMIN_A_ID, "mod_pick_group"))
        sends = [c for c in self.fake_api.calls if isinstance(c, SendMessage) and c.chat_id == self.ADMIN_A_ID]
        buttons = {b.callback_data for row in sends[-1].reply_markup.inline_keyboard for b in row}
        self.assertEqual(buttons, {f"mod_group:{self.CHAT_A}", f"mod_group:{self.CHAT_B}"})

    async def test_admin_of_a_can_open_a_panel(self):
        await self.dp.feed_webhook_update(self.bot, _private_callback_update(10, self.ADMIN_A_ID, "mod_pick_group"))
        await self.dp.feed_webhook_update(
            self.bot, _private_callback_update(11, self.ADMIN_A_ID, f"mod_group:{self.CHAT_A}")
        )
        texts = self.fake_api.sent_texts(self.ADMIN_A_ID)
        self.assertTrue(any("запрещённых слов" in t.lower() for t in texts))

    async def test_admin_of_b_cannot_open_a_panel_via_picker(self):
        """The exact scenario from the design review: an admin of group B
        must not be able to configure group A, even though the bot moderates
        both and both appear in the picker."""
        await self.dp.feed_webhook_update(self.bot, _private_callback_update(10, self.ADMIN_B_ID, "mod_pick_group"))
        await self.dp.feed_webhook_update(
            self.bot, _private_callback_update(11, self.ADMIN_B_ID, f"mod_group:{self.CHAT_A}")
        )
        self.assertTrue(any("⛔" in t for t in self.fake_api.alert_texts()))
        texts = self.fake_api.sent_texts(self.ADMIN_B_ID)
        self.assertFalse(any("запрещённых слов" in t.lower() for t in texts))

    async def test_admin_of_a_can_add_word_end_to_end(self):
        await self.dp.feed_webhook_update(self.bot, _private_callback_update(10, self.ADMIN_A_ID, "mod_pick_group"))
        await self.dp.feed_webhook_update(
            self.bot, _private_callback_update(11, self.ADMIN_A_ID, f"mod_group:{self.CHAT_A}")
        )
        await self.dp.feed_webhook_update(self.bot, _private_callback_update(12, self.ADMIN_A_ID, "modp_addword"))
        await self.dp.feed_webhook_update(self.bot, _private_text_update(13, self.ADMIN_A_ID, "badword"))
        self.assertIn("badword", await moderator._stopwords_for_chat(self.config.db_path, self.CHAT_A))

    async def test_word_added_via_a_panel_does_not_leak_into_b(self):
        await self.dp.feed_webhook_update(self.bot, _private_callback_update(10, self.ADMIN_A_ID, "mod_pick_group"))
        await self.dp.feed_webhook_update(
            self.bot, _private_callback_update(11, self.ADMIN_A_ID, f"mod_group:{self.CHAT_A}")
        )
        await self.dp.feed_webhook_update(self.bot, _private_callback_update(12, self.ADMIN_A_ID, "modp_addword"))
        await self.dp.feed_webhook_update(self.bot, _private_text_update(13, self.ADMIN_A_ID, "badword"))
        self.assertNotIn("badword", await moderator._stopwords_for_chat(self.config.db_path, self.CHAT_B))


class ModeratorPrivatePanelCrossGroupSecurityTests(unittest.IsolatedAsyncioTestCase):
    """The scenario explicitly called out in review: an admin of group B must
    be rejected on EVERY sub-action inside group A's private panel — not just
    at the initial picker step — even when `selected_group` in their own FSM
    state points at A through some means OTHER than the legitimate picker
    (here: injected directly into storage, simulating a forged/stale state),
    rather than a real "you are admin of A" pick via cb_select_group. Each
    modp_* handler must independently call get_chat_member(A, presser) and
    reject — this proves the check actually runs and actually blocks on every
    single sub-action, not merely "the design says it should"."""

    CHAT_A = -100333   # admin_b has NO rights here
    CHAT_B = -100444   # admin_b's own group
    ADMIN_B_ID = 9101

    async def asyncSetUp(self):
        self.fake_api = FakeBotAPI()
        # admin_b IS an admin of B; for CHAT_A, no entry is registered, so
        # FakeBotAPI's default get_chat_member answer (a plain non-admin
        # member) applies — exactly "admin of B, not of A".
        self.fake_api.chat_member_responses[(self.CHAT_B, self.ADMIN_B_ID)] = _admin_member(self.ADMIN_B_ID)
        self._patcher = patch.object(Bot, "__call__", new=self.fake_api)
        self._patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.config = moderator.config_from_bot_row(
            {"bot_id": 730, "name": "mod_crossgroup", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await moderator.init_db(self.config.db_path)
        # Seed group A with a pre-existing stopword and a non-default
        # threshold, so any successful mutation from the attempts below would
        # be immediately detectable.
        async with aiosqlite.connect(self.config.db_path) as db:
            await db.execute("INSERT INTO stopwords (chat_id, word) VALUES (?, ?)", (self.CHAT_A, "existingword"))
            await db.execute("INSERT INTO chat_settings (chat_id, max_warnings) VALUES (?, ?)", (self.CHAT_A, 3))
            await db.commit()
        self.bot, self.dp = _build_bot_dispatcher(self.config)
        await moderator._upsert_known_group(self.config.db_path, self.CHAT_A, "Group A")
        await moderator._upsert_known_group(self.config.db_path, self.CHAT_B, "Group B")
        # Forge admin_b's OWN FSM state to point selected_group at A —
        # WITHOUT ever going through cb_select_group's live admin check. This
        # is the "not through the legitimate picker" scenario from review.
        key = StorageKey(bot_id=self.bot.id, chat_id=self.ADMIN_B_ID, user_id=self.ADMIN_B_ID)
        self.forged_state = FSMContext(storage=self.dp.storage, key=key)
        await self.forged_state.update_data(selected_group=self.CHAT_A, panel_msg_id=None)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._patcher.stop()

    async def _gcm_calls_on_a_by_b(self):
        return [
            c for c in self.fake_api.calls
            if isinstance(c, GetChatMember) and c.chat_id == self.CHAT_A and c.user_id == self.ADMIN_B_ID
        ]

    async def test_recheck_rights_denied(self):
        await self.dp.feed_webhook_update(self.bot, _private_callback_update(1, self.ADMIN_B_ID, "modp_recheck"))
        self.assertEqual(len(await self._gcm_calls_on_a_by_b()), 1)
        self.assertTrue(any("⛔" in t for t in self.fake_api.alert_texts()))

    async def test_addword_start_denied_no_flow_started(self):
        await self.dp.feed_webhook_update(self.bot, _private_callback_update(1, self.ADMIN_B_ID, "modp_addword"))
        self.assertEqual(len(await self._gcm_calls_on_a_by_b()), 1)
        self.assertTrue(any("⛔" in t for t in self.fake_api.alert_texts()))
        await self.dp.feed_webhook_update(self.bot, _private_text_update(2, self.ADMIN_B_ID, "sneakyword"))
        self.assertNotIn("sneakyword", await moderator._stopwords_for_chat(self.config.db_path, self.CHAT_A))

    async def test_addword_continuation_denied_even_with_flow_state_forged_too(self):
        # Forge the FULL flow state (not just selected_group) to simulate the
        # attacker having somehow reached the "type the word" step directly,
        # bypassing cb_private_addword_start's own check entirely.
        await self.forged_state.update_data(started_at=time.time())
        await self.forged_state.set_state(moderator.PrivateModPanelFlow.add_word)
        await self.dp.feed_webhook_update(self.bot, _private_text_update(1, self.ADMIN_B_ID, "sneakyword"))
        self.assertEqual(len(await self._gcm_calls_on_a_by_b()), 1)
        self.assertNotIn("sneakyword", await moderator._stopwords_for_chat(self.config.db_path, self.CHAT_A))

    async def test_removeword_start_denied_no_words_removed(self):
        await self.dp.feed_webhook_update(self.bot, _private_callback_update(1, self.ADMIN_B_ID, "modp_removeword"))
        self.assertEqual(len(await self._gcm_calls_on_a_by_b()), 1)
        self.assertTrue(any("⛔" in t for t in self.fake_api.alert_texts()))
        self.assertIn("existingword", await moderator._stopwords_for_chat(self.config.db_path, self.CHAT_A))

    async def test_removeword_text_continuation_denied(self):
        await self.forged_state.update_data(started_at=time.time())
        await self.forged_state.set_state(moderator.PrivateModPanelFlow.remove_word_text)
        await self.dp.feed_webhook_update(self.bot, _private_text_update(1, self.ADMIN_B_ID, "existingword"))
        self.assertEqual(len(await self._gcm_calls_on_a_by_b()), 1)
        self.assertIn("existingword", await moderator._stopwords_for_chat(self.config.db_path, self.CHAT_A))

    async def test_removeword_pick_continuation_denied(self):
        await self.forged_state.update_data(started_at=time.time(), remove_words=["existingword"])
        await self.forged_state.set_state(moderator.PrivateModPanelFlow.remove_word_pick)
        await self.dp.feed_webhook_update(self.bot, _private_callback_update(1, self.ADMIN_B_ID, "modp_rmw:0"))
        self.assertEqual(len(await self._gcm_calls_on_a_by_b()), 1)
        self.assertIn("existingword", await moderator._stopwords_for_chat(self.config.db_path, self.CHAT_A))

    async def test_setwarn_start_denied(self):
        await self.dp.feed_webhook_update(self.bot, _private_callback_update(1, self.ADMIN_B_ID, "modp_setwarn"))
        self.assertEqual(len(await self._gcm_calls_on_a_by_b()), 1)
        self.assertTrue(any("⛔" in t for t in self.fake_api.alert_texts()))

    async def test_setwarn_continuation_denied_threshold_unchanged(self):
        await self.forged_state.update_data(started_at=time.time())
        await self.forged_state.set_state(moderator.PrivateModPanelFlow.max_warnings)
        await self.dp.feed_webhook_update(self.bot, _private_text_update(1, self.ADMIN_B_ID, "77"))
        self.assertEqual(len(await self._gcm_calls_on_a_by_b()), 1)
        async with aiosqlite.connect(self.config.db_path) as db:
            row = await (await db.execute(
                "SELECT max_warnings FROM chat_settings WHERE chat_id=?", (self.CHAT_A,)
            )).fetchone()
        self.assertEqual(row[0], 3)


class ModeratorGroupSwitchFlowClearTests(unittest.IsolatedAsyncioTestCase):
    """Review-found: switching to a different group via the picker while a
    sub-flow (e.g. add-word) was still open for the PREVIOUSLY selected group
    used to leave that flow's state dangling — cb_select_group overwrote
    selected_group but never cleared it. A message meant for group A's
    add-word prompt, arriving after the switch to B, would then be silently
    captured by A's still-active flow — which now reads the NEW group from
    FSM state — and land in B's stopwords instead of A's."""

    CHAT_A = -100555
    CHAT_B = -100666
    ADMIN_ID = 9500  # admin of BOTH groups

    async def asyncSetUp(self):
        self.fake_api = FakeBotAPI()
        self.fake_api.chat_member_responses[(self.CHAT_A, self.ADMIN_ID)] = _admin_member(self.ADMIN_ID)
        self.fake_api.chat_member_responses[(self.CHAT_B, self.ADMIN_ID)] = _admin_member(self.ADMIN_ID)
        self._patcher = patch.object(Bot, "__call__", new=self.fake_api)
        self._patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.config = moderator.config_from_bot_row(
            {"bot_id": 740, "name": "mod_switch", "display_name": None, "group_chat_id": None}, Path(self._tmp.name)
        )
        await moderator.init_db(self.config.db_path)
        self.bot, self.dp = _build_bot_dispatcher(self.config)
        await moderator._upsert_known_group(self.config.db_path, self.CHAT_A, "Group A")
        await moderator._upsert_known_group(self.config.db_path, self.CHAT_B, "Group B")

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._patcher.stop()

    async def test_switching_group_mid_addword_flow_does_not_leak_word_into_new_group(self):
        await self.dp.feed_webhook_update(self.bot, _private_callback_update(1, self.ADMIN_ID, f"mod_group:{self.CHAT_A}"))
        await self.dp.feed_webhook_update(self.bot, _private_callback_update(2, self.ADMIN_ID, "modp_addword"))
        # Switch to B via the picker WITHOUT finishing A's add-word flow.
        await self.dp.feed_webhook_update(self.bot, _private_callback_update(3, self.ADMIN_ID, f"mod_group:{self.CHAT_B}"))
        # A stray plain-text message arrives — would have been captured as
        # A's pending word before the fix, ending up in B's stopwords instead.
        await self.dp.feed_webhook_update(self.bot, _private_text_update(4, self.ADMIN_ID, "leakyword"))
        self.assertNotIn("leakyword", await moderator._stopwords_for_chat(self.config.db_path, self.CHAT_A))
        self.assertNotIn("leakyword", await moderator._stopwords_for_chat(self.config.db_path, self.CHAT_B))


class ModeratorPrivateCancelRevokedAdminTests(unittest.IsolatedAsyncioTestCase):
    """Review-found: "❌ Отмена" and /cancel mid a private group sub-flow used
    to render that group's rights-status + full stopwords list WITHOUT
    re-checking the presser's live admin status — the one screen in the
    private-panel section that skipped the "every sub-action re-verifies
    live admin status" rule. A user kicked/demoted mid-flow could still see
    the group's current panel content through the cancel path alone."""

    CHAT_A = -100777
    ADMIN_ID = 9600

    async def asyncSetUp(self):
        self.fake_api = FakeBotAPI()
        self.fake_api.chat_member_responses[(self.CHAT_A, self.ADMIN_ID)] = _admin_member(self.ADMIN_ID)
        self._patcher = patch.object(Bot, "__call__", new=self.fake_api)
        self._patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.config = moderator.config_from_bot_row(
            {"bot_id": 741, "name": "mod_cancel_revoked", "display_name": None, "group_chat_id": None},
            Path(self._tmp.name),
        )
        await moderator.init_db(self.config.db_path)
        async with aiosqlite.connect(self.config.db_path) as db:
            await db.execute("INSERT INTO stopwords (chat_id, word) VALUES (?, ?)", (self.CHAT_A, "secretword"))
            await db.commit()
        self.bot, self.dp = _build_bot_dispatcher(self.config)
        await moderator._upsert_known_group(self.config.db_path, self.CHAT_A, "Group A")

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._patcher.stop()

    async def test_cancel_button_does_not_leak_panel_after_rights_revoked(self):
        await self.dp.feed_webhook_update(self.bot, _private_callback_update(1, self.ADMIN_ID, f"mod_group:{self.CHAT_A}"))
        await self.dp.feed_webhook_update(self.bot, _private_callback_update(2, self.ADMIN_ID, "modp_addword"))
        del self.fake_api.chat_member_responses[(self.CHAT_A, self.ADMIN_ID)]  # rights revoked mid-flow
        # Only what the CANCEL action itself sends is in scope — group
        # selection legitimately showed the panel (incl. secretword) earlier,
        # while the user was still a live admin.
        calls_before = len(self.fake_api.calls)
        await self.dp.feed_webhook_update(self.bot, _private_callback_update(3, self.ADMIN_ID, "modp_cancel"))
        new_calls = self.fake_api.calls[calls_before:]
        texts = [c.text for c in new_calls if isinstance(c, (SendMessage, EditMessageText))]
        self.assertFalse(any("secretword" in t for t in texts))

    async def test_slash_cancel_does_not_leak_panel_after_rights_revoked(self):
        await self.dp.feed_webhook_update(self.bot, _private_callback_update(1, self.ADMIN_ID, f"mod_group:{self.CHAT_A}"))
        await self.dp.feed_webhook_update(self.bot, _private_callback_update(2, self.ADMIN_ID, "modp_addword"))
        del self.fake_api.chat_member_responses[(self.CHAT_A, self.ADMIN_ID)]  # rights revoked mid-flow
        calls_before = len(self.fake_api.calls)
        await self.dp.feed_webhook_update(self.bot, _private_text_update(3, self.ADMIN_ID, "/cancel"))
        new_calls = self.fake_api.calls[calls_before:]
        texts = [c.text for c in new_calls if isinstance(c, (SendMessage, EditMessageText))]
        self.assertFalse(any("secretword" in t for t in texts))


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
