"""userbot-worker — separate entry point for channel_monitor's Telethon clients.

WHY THIS IS A SEPARATE PROCESS FROM runtime/combined_app.py:
combined_app.py serves every factory/tenant aiogram bot through ONE aiohttp
webhook server — a request-driven model where Telegram pushes updates to us,
handled per-request on a shared event loop with no long-lived background
connections. A Telethon userbot client is the opposite: it MUST hold its own
persistent MTProto connection to Telegram's servers, independent of any
incoming HTTP request, for as long as monitoring stays active. Mixing the two
in one process is technically possible (same event loop, `asyncio.create_task`
for each) but operationally risky: a stalled/misbehaving userbot connection
must never be able to starve or crash the webhook loop serving every rented
bot on the factory. See docs/USERBOT_CHANNEL_MONITOR_DESIGN.md §3 for the full
reasoning — this file is exactly that "new Railway service, own entry point"
the design calls for.

Model: ONE process, ONE asyncio event loop, MANY TelegramClient instances
multiplexed as asyncio tasks (not one OS process per client) — see design doc
§3 for why (operational overhead of per-client processes vs. the expected
scale on day one; sharding is the documented escape hatch if that changes,
per docs/USERBOT_FEATURE_DESIGN.md §6 q3, confirmed still the right call now
that many bots — not just one standalone product — can enable the feature).

Session ownership (docs/USERBOT_FEATURE_DESIGN.md §2, Variant A): each
userbot_sessions row lives in ITS OWN bot's per-bot db_path and is scoped to
that bot_id — there is no more single central-DB table of sessions. This
worker discovers which bots to watch by listing every bot with the
"channel_monitor" feature enabled (bot_features), then opens each bot's own
db_path to read its sessions/channels — same "iterate bots, read their own db"
shape as runtime/registry.py's reload_all().

Restart/recovery: on startup, for every channel_monitor-enabled bot, reads
that bot's userbot_sessions rows with status='active', decrypts each session,
and reconnects a TelegramClient for each — the same "restore what was
running" idea as main.py's restore_bots(), just for userbot clients instead
of subprocess-based tenant bots.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from config import DATA_DIR, TELEGRAM_API_HASH, TELEGRAM_API_ID  # noqa: E402
from db.database import (  # noqa: E402
    add_channel_post,
    get_active_monitored_channels,
    get_active_userbot_sessions,
    get_all_bots,
    get_bot_features,
    get_posts_missing_summary,
    init_channel_monitor_tables,
    set_channel_post_summary,
    set_userbot_session_status,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

SUMMARIZE_INTERVAL_SECONDS = 300  # background summarization sweep cadence


def _db_path_for_bot(bot_id: int) -> str:
    """Same per-bot db_path convention every template's config_from_bot_row
    uses (e.g. templates/moderator.py's config_from_bot_row) — this worker
    has no host template's Config object to read it from, so it's rebuilt
    directly from bot_id, matching that convention exactly."""
    return str(DATA_DIR / f"bot_{bot_id}_data.db")


async def get_channel_monitor_bot_ids() -> list[int]:
    """Every bot with the 'channel_monitor' feature currently enabled — the
    set this worker needs to watch. Re-read on every start_all() so a feature
    toggled on/off between worker restarts is picked up without a manual
    signal."""
    bots = await get_all_bots()
    enabled: list[int] = []
    for bot in bots:
        features = await get_bot_features(bot["id"])
        if "channel_monitor" in features:
            enabled.append(bot["id"])
    return enabled


class UserbotManager:
    """Owns the live TelegramClient instances for this process — one per
    active userbot_sessions row, keyed by (bot_id, session_id) since session
    ids are now only unique WITHIN one bot's own db_path, not across the
    whole factory. Kept as an instance (not module globals) so tests can
    construct a fresh manager per test without cross-test state leaking
    through module-level dicts."""

    def __init__(self) -> None:
        self.clients: dict[tuple[int, int], object] = {}   # (bot_id, session_id) -> TelegramClient
        self.tasks: dict[tuple[int, int], asyncio.Task] = {}  # (bot_id, session_id) -> run_until_disconnected task
        # Telegram numeric channel_id -> list of (bot_id, monitored_channels.id),
        # so an incoming NewMessage event (which only carries the Telegram
        # chat id) can be mapped back to every monitored_channels row (across
        # every bot) watching it. ONE-TO-MANY, not one-to-one: two different
        # bots (or, previously under the owner-scoped model, two different
        # owners of the same session) can both add the same public channel to
        # monitor, and each must get its own channel_posts entry; a plain
        # dict[channel_id, single_row] would let a second bot's row silently
        # shadow the first's, dropping that bot's monitoring with no visible
        # error.
        self._channel_row_ids_by_telegram_id: dict[int, list[tuple[int, int]]] = {}

    def _make_client(self, session_string: str):
        if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
            raise RuntimeError(
                "TELEGRAM_API_ID / TELEGRAM_API_HASH is not set — userbot_worker cannot start "
                "any client without them. Obtain both at https://my.telegram.org/apps."
            )
        from telethon import TelegramClient
        from telethon.sessions import StringSession

        return TelegramClient(StringSession(session_string), int(TELEGRAM_API_ID), TELEGRAM_API_HASH)

    async def start_all(self) -> None:
        """Boot-time restore: for every bot with channel_monitor enabled,
        every status='active' session in THAT bot's own db_path gets a live
        client. A session that fails to (re)connect is marked auth_failed
        rather than silently dropped, so it's visible in "📋 Мои каналы"/DB
        inspection instead of just quietly not running."""
        bot_ids = await get_channel_monitor_bot_ids()
        self._channel_row_ids_by_telegram_id = {}
        total_sessions = 0
        for bot_id in bot_ids:
            db_path = _db_path_for_bot(bot_id)
            await init_channel_monitor_tables(db_path)
            channels = await get_active_monitored_channels(db_path)
            for c in channels:
                tg_id = c.get("channel_id")
                if tg_id is None:
                    continue
                self._channel_row_ids_by_telegram_id.setdefault(tg_id, []).append((bot_id, c["id"]))

            sessions = await get_active_userbot_sessions(db_path)
            total_sessions += len(sessions)
            for session in sessions:
                await self.start_one(bot_id, session)
        logger.info(f"UserbotManager.start_all: {len(self.clients)}/{total_sessions} client(s) started across {len(bot_ids)} bot(s)")

    async def start_one(self, bot_id: int, session: dict) -> bool:
        session_id = session["id"]
        db_path = _db_path_for_bot(bot_id)
        session_string = session.get("session_string")
        if not session_string:
            logger.warning(f"start_one: bot_id={bot_id} session_id={session_id} has status=active but no session_string — skipping")
            return False
        try:
            client = self._make_client(session_string)
            await client.connect()
            if not await client.is_user_authorized():
                raise RuntimeError("session is no longer authorized (revoked on Telegram's side)")
        except Exception as e:
            logger.error(f"start_one: bot_id={bot_id} session_id={session_id} failed to connect: {type(e).__name__}: {e}")
            await set_userbot_session_status(db_path, session_id, "auth_failed")
            return False

        key = (bot_id, session_id)
        self._register_handlers(client, key)
        self.clients[key] = client
        self.tasks[key] = asyncio.create_task(self._run_client(client, bot_id, session_id))
        logger.info(f"start_one: bot_id={bot_id} session_id={session_id} client started")
        return True

    def _register_handlers(self, client, key: tuple[int, int]) -> None:
        from telethon import events

        @client.on(events.NewMessage())
        async def _on_new_message(event):  # pragma: no cover - exercised via _handle_event in tests
            await self._handle_event(event)

    async def _handle_event(self, event) -> None:
        """Writes a new channel post to channel_posts for EVERY monitored_channels
        row (in its own bot's db_path) watching this Telegram channel — not
        just one. Two different bots can independently monitor the same
        public channel; each must get its own channel_posts row (see the
        one-to-many map above), so a single incoming event fans out to all of
        them. Split out from the decorator above so tests can call it
        directly with a fake event instead of driving real Telethon event
        dispatch."""
        chat_id = getattr(event, "chat_id", None)
        targets = self._channel_row_ids_by_telegram_id.get(chat_id) or []
        if not targets:
            return  # not one of this process's actively-monitored channels
        message = getattr(event, "message", None)
        text = getattr(message, "message", None) if message else getattr(event, "raw_text", None)
        message_id = getattr(message, "id", None) if message else None
        for bot_id, monitored_channel_id in targets:
            db_path = _db_path_for_bot(bot_id)
            await add_channel_post(db_path, monitored_channel_id, message_id, text)
        logger.info(f"_handle_event: stored post for (bot_id, monitored_channel_id)={targets}")

    async def _run_client(self, client, bot_id: int, session_id: int) -> None:
        key = (bot_id, session_id)
        db_path = _db_path_for_bot(bot_id)
        try:
            await client.run_until_disconnected()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"_run_client: bot_id={bot_id} session_id={session_id} crashed: {type(e).__name__}: {e}")
            await set_userbot_session_status(db_path, session_id, "auth_failed")
        finally:
            self.clients.pop(key, None)
            self.tasks.pop(key, None)

    async def revoke(self, bot_id: int, session_id: int) -> None:
        """Disconnects and drops the in-memory client for a session that was
        just revoked (status='revoked', session_string wiped) — per
        docs/USERBOT_CHANNEL_MONITOR_DESIGN.md §1, the process must not keep
        holding a live connection open on a session the owner explicitly
        disconnected. Revoking one bot's session never touches any other
        bot's client (§2 Variant A isolation guarantee)."""
        key = (bot_id, session_id)
        client = self.clients.pop(key, None)
        task = self.tasks.pop(key, None)
        if task is not None:
            task.cancel()
        if client is not None:
            try:
                await client.disconnect()
            except Exception as e:
                logger.warning(f"revoke: bot_id={bot_id} session_id={session_id} disconnect raised: {type(e).__name__}: {e}")
        logger.info(f"revoke: bot_id={bot_id} session_id={session_id} client stopped")

    async def shutdown(self) -> None:
        for bot_id, session_id in list(self.clients.keys()):
            await self.revoke(bot_id, session_id)


async def _summarize_loop() -> None:
    """Background task: periodically summarizes any channel_posts with
    summary IS NULL via Gemini, across every channel_monitor-enabled bot's OWN
    db_path — never blocks post ingestion (§5 of docs/USERBOT_CHANNEL_MONITOR_DESIGN.md).
    Failures inside summarize_post already degrade to None internally; this
    loop just persists whatever comes back and keeps going."""
    from services.gemini_service import summarize_post

    while True:
        try:
            bot_ids = await get_channel_monitor_bot_ids()
            for bot_id in bot_ids:
                db_path = _db_path_for_bot(bot_id)
                posts = await get_posts_missing_summary(db_path)
                for post in posts:
                    summary = await summarize_post(post.get("text") or "")
                    if summary:
                        await set_channel_post_summary(db_path, post["id"], summary)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"_summarize_loop: sweep failed: {type(e).__name__}: {e}")
        await asyncio.sleep(SUMMARIZE_INTERVAL_SECONDS)


async def _bootstrap() -> UserbotManager:
    manager = UserbotManager()
    await manager.start_all()
    asyncio.create_task(_summarize_loop())
    return manager


async def main() -> None:
    manager = await _bootstrap()
    logger.info("userbot_worker: bootstrap complete, running")
    try:
        # Keep the process alive for as long as any client task is running;
        # if all clients drop (no active sessions at all), idle-poll instead
        # of exiting, so a session added later (via the factory bot) can
        # still be picked up on a future restart.
        while True:
            await asyncio.sleep(60)
    except asyncio.CancelledError:
        await manager.shutdown()
        raise


if __name__ == "__main__":
    if "--check-imports" in sys.argv:
        print("imports OK")
        sys.exit(0)
    asyncio.run(main())
