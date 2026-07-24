"""Stage 2 — "фабрика как житель реестра" phase.

Three scenarios, matching the phase's own Task 5:

1. A bot created through the real factory flow (handlers/create_bot.py's
   manual-token path, driven end to end) is registered into the live registry
   directly (no HTTP self-call, no manual /admin/reload) and can immediately
   answer real messages through its own Dispatcher — the main case this whole
   phase exists to prove.
2. The factory bot itself answers through /webhook/{FACTORY_BOT_ID}, i.e. the
   same aiohttp request path every tenant bot uses — not polling.
3. An unhandled exception inside the factory's own handler does not affect a
   healthy tenant bot answering through the same webhook app — proving the
   EXISTING try/except in webhook_handler (around feed_webhook_update) already
   covers the factory too, without any new supervisor/wrapper code.

No real Telegram network calls (Bot.__call__ mocked throughout), no real
tokens, no real subprocess spawning (services.bot_runner.start_bot mocked),
no real GitHub pushes (push_bot_to_github mocked).

Run with: python -m unittest tests.test_factory_registry_citizen
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram import Bot, Dispatcher, F, Router
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.context import FSMContext
from aiohttp.test_utils import TestClient, TestServer

import handlers.create_bot as create_bot_module
from db.database import delete_bot, get_bot_by_name
from runtime.registry import BotEntry, FACTORY_BOT_ID, Registry, build_factory_entry, get_template_router
from runtime.webhook_app import WEBHOOK_SECRET_HEADER, create_app
from templates import accountant

FAKE_TOKEN = "123456:test-token-not-real"
FAKE_NEW_BOT_TOKEN = "987654321:AAHalsoNotRealButLongEnoughToPass"  # handle_token() requires len(token) >= 30
SAME_USER_ID = 111

_ACCOUNTANT_SOURCE = (Path(__file__).resolve().parent.parent / "templates" / "accountant.py").read_text(encoding="utf-8")


def _mock_bot_api_call() -> AsyncMock:
    """Bot.__call__ mock whose return_value has a real string .username —
    handlers/create_bot.py's handle_token()/auto_launch_managed_bot() both do
    `(await Bot(token=token).get_me()).username`, and a bare MagicMock()'s
    .username is itself a MagicMock, which sqlite3 can't bind as a parameter."""
    return AsyncMock(return_value=MagicMock(username="mocked_username", id=1))

# aiogram Router objects can only ever be attached to ONE parent Dispatcher for
# their entire lifetime (Router.parent_router raises RuntimeError on a second
# attach) — this is true in production too (only main.py's OR
# runtime/combined_app.py's dispatcher ever includes handlers/create_bot.py's
# module-level `router` singleton, never both in the same process). Tests in
# this module that need the REAL create_bot_module.router therefore share a
# single lazily-built (bot, dispatcher) pair instead of each building their own.
_shared_factory: dict = {}


def _get_shared_factory_bot_and_dispatcher() -> tuple[Bot, Dispatcher]:
    if "dp" not in _shared_factory:
        bot = Bot(token=FAKE_TOKEN)
        dp = Dispatcher(storage=MemoryStorage())
        dp.include_router(create_bot_module.router)
        _shared_factory["bot"] = bot
        _shared_factory["dp"] = dp
    return _shared_factory["bot"], _shared_factory["dp"]


def _text_update(update_id: int, user_id: int, text: str) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 1700000000,
            "chat": {"id": user_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
            "text": text,
        },
    }


def _callback_update(update_id: int, user_id: int, data: str) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": str(update_id),
            "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
            "message": {
                "message_id": update_id,
                "date": 1700000000,
                "chat": {"id": user_id, "type": "private"},
                "text": "placeholder",
            },
            "chat_instance": "1",
            "data": data,
        },
    }


class BotCreatedThroughFactoryRespondsImmediately(unittest.IsolatedAsyncioTestCase):
    """Scenario 1 — the main case this phase closes."""

    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=_mock_bot_api_call())
        self._bot_call_patcher.start()

        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self._data_dir_patcher = patch("config.DATA_DIR", self.data_dir)
        self._data_dir_patcher.start()

        self._gen_dir_patcher = patch.object(create_bot_module, "GENERATED_BOTS_DIR", self.data_dir / "generated_bots")
        self._img_dir_patcher = patch.object(create_bot_module, "BOT_IMAGES_DIR", self.data_dir / "bot_images")
        self._avatar_dir_patcher = patch.object(create_bot_module, "AVATAR_DIR", self.data_dir / "bot_avatars")
        for p in (self._gen_dir_patcher, self._img_dir_patcher, self._avatar_dir_patcher):
            p.start()
        (self.data_dir / "generated_bots").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "bot_images").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "bot_avatars").mkdir(parents=True, exist_ok=True)

        self._github_patcher = patch.object(create_bot_module, "push_bot_to_github", new=AsyncMock())
        self._guide_patcher = patch.object(create_bot_module, "generate_bot_guide", new=AsyncMock(return_value=""))
        self._start_bot_patcher = patch.object(create_bot_module, "start_bot", new=AsyncMock(return_value=99999))
        for p in (self._github_patcher, self._guide_patcher, self._start_bot_patcher):
            p.start()

        self.registry = Registry()
        create_bot_module.set_registry(self.registry)
        self._created_bot_id: int | None = None

    async def asyncTearDown(self):
        create_bot_module.set_registry(None)
        if self._created_bot_id is not None:
            await delete_bot(self._created_bot_id)
        for p in (
            self._start_bot_patcher, self._guide_patcher, self._github_patcher,
            self._avatar_dir_patcher, self._img_dir_patcher, self._gen_dir_patcher,
            self._data_dir_patcher,
        ):
            p.stop()
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_bot_created_via_factory_flow_is_registered_and_responds_without_manual_reload(self):
        # Deliberately does NOT end in "_bot" — get_bot_by_name() strips a
        # trailing "_bot" suffix before matching (see db/database.py), which
        # would otherwise make this lookup silently miss the real row.
        bot_name = "factory_created_project"

        # Drive handlers/create_bot.py's manual-token fallback path for real:
        # pre-seed the FSM state exactly as it would look right after the
        # (mocked-out) chat-gathering/code-generation steps, then feed a
        # plausible-looking token as a real webhook update through a real
        # Dispatcher — no shortcuts around handle_token() itself.
        bot, dp = _get_shared_factory_bot_and_dispatcher()

        key = StorageKey(bot_id=bot.id, chat_id=SAME_USER_ID, user_id=SAME_USER_ID)
        state = FSMContext(storage=dp.storage, key=key)
        await state.set_state(create_bot_module.CreateBotStates.waiting_for_token)
        await state.update_data(
            bot_code=_ACCOUNTANT_SOURCE,
            bot_name=bot_name,
            bot_summary="A test bot created end-to-end through the factory flow.",
            display_name="",
        )

        await dp.feed_webhook_update(bot, _text_update(1, SAME_USER_ID, FAKE_NEW_BOT_TOKEN))

        row = await get_bot_by_name(bot_name)
        self.assertIsNotNone(row, "create_bot_record_with_admins did not create a bots-table row")
        self._created_bot_id = row["id"]

        # The actual claim of this phase: registered WITHOUT any manual
        # /admin/reload call — add_or_replace() was called directly by
        # handlers/create_bot.py's _register_new_bot_in_registry().
        entry = self.registry.get(self._created_bot_id)
        self.assertIsNotNone(entry, "new bot is not in the registry — direct add_or_replace() call didn't happen")
        self.assertEqual(entry.template_id, "accountant")

        # And it actually works: drive a real accountant-template flow through
        # the registered entry's own Dispatcher and check the SQLite file.
        await entry.dispatcher.feed_webhook_update(entry.bot, _callback_update(1, SAME_USER_ID, "proj_new"))
        await entry.dispatcher.feed_webhook_update(entry.bot, _text_update(2, SAME_USER_ID, "Factory Project"))

        db_path = entry.config.get("db_path") or str(self.data_dir / f"bot_{self._created_bot_id}_data.db")
        conn = sqlite3.connect(db_path)
        names = [r[0] for r in conn.execute("SELECT name FROM projects").fetchall()]
        conn.close()
        self.assertEqual(names, ["Factory Project"])


class FactoryAnswersThroughWebhookNotPolling(unittest.IsolatedAsyncioTestCase):
    """Scenario 2 — the factory bot itself is a normal /webhook/{bot_id} citizen."""

    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=_mock_bot_api_call())
        self._bot_call_patcher.start()
        self._env_patcher = patch.dict(__import__("os").environ, {"WEBHOOK_SECRET": "test-secret"})
        self._env_patcher.start()

        self.factory_bot, self.factory_dp = _get_shared_factory_bot_and_dispatcher()
        registry = Registry()
        registry._entries[FACTORY_BOT_ID] = build_factory_entry(self.factory_bot, self.factory_dp)

        self.app = create_app(registry)
        self.server = TestServer(self.app)
        self.client = TestClient(self.server)
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self._env_patcher.stop()
        self._bot_call_patcher.stop()

    async def test_factory_fsm_advances_via_webhook_path(self):
        resp = await self.client.post(
            f"/webhook/{FACTORY_BOT_ID}",
            json=_text_update(1, SAME_USER_ID, "/create"),
            headers={WEBHOOK_SECRET_HEADER: "test-secret"},
        )
        self.assertEqual(resp.status, 200)

        # Proof the update was really processed by handlers/create_bot.py's
        # cmd_create (not just acknowledged) — the FSM state actually moved.
        key = StorageKey(bot_id=self.factory_bot.id, chat_id=SAME_USER_ID, user_id=SAME_USER_ID)
        state = FSMContext(storage=self.factory_dp.storage, key=key)
        current_state = await state.get_state()
        self.assertIsNotNone(current_state, "factory bot's /create handler did not run via the webhook path")


class FactoryHandlerExceptionDoesNotAffectTenants(unittest.IsolatedAsyncioTestCase):
    """Scenario 3 — proves the EXISTING try/except in webhook_handler already
    isolates a failing factory handler from healthy tenant bots; no new
    supervisor/wrapper code was needed or added."""

    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=_mock_bot_api_call())
        self._bot_call_patcher.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)

    async def asyncTearDown(self):
        self._tmp.cleanup()
        self._bot_call_patcher.stop()

    async def test_broken_factory_handler_does_not_break_a_healthy_tenant_bot(self):
        registry = Registry()

        # A deliberately broken "factory" entry — its only handler raises.
        broken_router = Router()

        @broken_router.message(F.text == "/boom")
        async def _boom(message):
            raise RuntimeError("intentional failure for this test")

        factory_bot = Bot(token=FAKE_TOKEN)
        factory_dp = Dispatcher(storage=MemoryStorage())
        factory_dp.include_router(broken_router)
        registry._entries[FACTORY_BOT_ID] = build_factory_entry(factory_bot, factory_dp)

        # A real, healthy tenant bot registered the normal way.
        tenant_config = accountant.config_from_bot_row(
            {"bot_id": 501, "name": "healthy_tenant", "display_name": None, "group_chat_id": None}, self.data_dir
        )
        await accountant.init_db(tenant_config.db_path)
        tenant_bot = Bot(token=FAKE_TOKEN)
        tenant_dp = Dispatcher(storage=MemoryStorage())
        tenant_dp.update.outer_middleware(accountant.ConfigMiddleware(tenant_config))
        tenant_dp.include_router(get_template_router("accountant"))
        registry._entries[501] = BotEntry(bot=tenant_bot, dispatcher=tenant_dp, template_id="accountant", config={"bot_id": 501})

        with patch.dict(__import__("os").environ, {"WEBHOOK_SECRET": "test-secret"}):
            app = create_app(registry)
            server = TestServer(app)
            client = TestClient(server)
            await client.start_server()
            try:
                boom_resp = await client.post(
                    f"/webhook/{FACTORY_BOT_ID}",
                    json=_text_update(1, SAME_USER_ID, "/boom"),
                    headers={WEBHOOK_SECRET_HEADER: "test-secret"},
                )
                # webhook_handler's existing try/except around feed_webhook_update
                # swallows the exception and still answers Telegram with 200 —
                # unchanged, pre-existing behavior, just now also covering the
                # factory bot's own handlers.
                self.assertEqual(boom_resp.status, 200)

                tenant_resp = await client.post(
                    "/webhook/501",
                    json=_callback_update(2, SAME_USER_ID, "proj_new"),
                    headers={WEBHOOK_SECRET_HEADER: "test-secret"},
                )
                self.assertEqual(tenant_resp.status, 200)

                await client.post(
                    "/webhook/501",
                    json=_text_update(3, SAME_USER_ID, "Tenant Survives Project"),
                    headers={WEBHOOK_SECRET_HEADER: "test-secret"},
                )
            finally:
                await client.close()

        conn = sqlite3.connect(tenant_config.db_path)
        names = [r[0] for r in conn.execute("SELECT name FROM projects").fetchall()]
        conn.close()
        self.assertEqual(names, ["Tenant Survives Project"])


class FactoryEntrySurvivesAdminReload(unittest.IsolatedAsyncioTestCase):
    """Review-found gap (security + devops-logs, both Important): the factory
    has no row in `bots`, so a plain reload_all()/reload_one(FACTORY_BOT_ID)
    would silently evict (and close the Bot session of) the live factory
    entry — reachable via a single /admin/reload-all call, or an
    /admin/reload/0 call/typo. Registry.reload_one() and Registry.reload_all()
    now guard against this explicitly (see runtime/registry.py)."""

    async def asyncSetUp(self):
        self._bot_call_patcher = patch.object(Bot, "__call__", new=_mock_bot_api_call())
        self._bot_call_patcher.start()

    async def asyncTearDown(self):
        self._bot_call_patcher.stop()

    async def test_reload_all_preserves_the_factory_entry(self):
        registry = Registry()
        factory_bot = Bot(token=FAKE_TOKEN)
        factory_dp = Dispatcher(storage=MemoryStorage())
        registry._entries[FACTORY_BOT_ID] = build_factory_entry(factory_bot, factory_dp)

        await registry.reload_all()

        entry = registry.get(FACTORY_BOT_ID)
        self.assertIsNotNone(entry, "reload_all() dropped the factory entry")
        self.assertIs(entry.bot, factory_bot, "factory entry should be the SAME object — never rebuilt from a DB row")

    async def test_reload_one_on_factory_bot_id_is_a_guarded_no_op(self):
        registry = Registry()
        factory_bot = Bot(token=FAKE_TOKEN)
        factory_dp = Dispatcher(storage=MemoryStorage())
        registry._entries[FACTORY_BOT_ID] = build_factory_entry(factory_bot, factory_dp)

        with self.assertLogs("runtime.registry", level="WARNING") as logs:
            result = await registry.reload_one(FACTORY_BOT_ID)

        self.assertIsNone(result)
        entry = registry.get(FACTORY_BOT_ID)
        self.assertIsNotNone(entry, "reload_one(FACTORY_BOT_ID) removed the factory entry")
        self.assertIs(entry.bot, factory_bot)
        self.assertTrue(any("FACTORY_BOT_ID" in msg for msg in logs.output))


if __name__ == "__main__":
    unittest.main()
