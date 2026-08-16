# FEATURE: notifications
# COMPATIBLE_WITH: *
"""Reusable owner-broadcast feature module — see docs/NOTIFICATIONS_FEATURE_DESIGN.md
for the design this implements (§3-§6 in particular).

`COMPATIBLE_WITH: *` is a deliberate exception to discover_features()'s "no
all sentinel" rule (see runtime/registry.py's discover_features() docstring
and handlers/manage_bots.py's _compatible_features()/cb_toggle_feature(),
both of which now special-case "*" to mean "offer this feature regardless of
template_id, including from-scratch bots with template_id=None"). Safe here
specifically because this module touches NOTHING template-specific: its own
`subscribers` table (below) and the audience it tracks depend only on "did
this chat_id ever message this bot", true for every bot regardless of
template or from-scratch origin. Other feature modules must keep an explicit
COMPATIBLE_WITH list — this sentinel is not a precedent for widening theirs.

Same shape as features/payments.py: a Config Protocol duck-typed on db_path
(+ optional admins_file, same convention features/voice_intake.py's
VoiceIntakeConfig uses for admin-gating a feature with no template-specific
concept of "admin"), its own init_db(db_path) creating tables in the HOST
bot's own db_path (never a separate file), and a module-level `router`
picked up by runtime/registry.py's _load_and_include_features() via the
by-convention `router` attribute.

Unlike payments.py, this module's router is NOT idle outside its own
handlers — _track_subscriber below observes EVERY private-chat message (no
filter) at the lowest priority, purely to upsert the sender into
`subscribers`. It never calls message.answer() or otherwise responds, and
never raises past its own try/except, so it cannot interfere with or block
the host template's own handlers riding the same Dispatcher.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Protocol

import aiosqlite
from aiogram import F, Router
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

logger = logging.getLogger(__name__)

router = Router()

bot_commands: list[tuple[str, str]] = [("broadcast", "Разослать сообщение подписчикам")]

# Telegram Bot API's documented broadcast-safe throughput is ~30 msg/sec
# per bot; this stays under that with margin rather than chasing the ceiling
# — see docs/NOTIFICATIONS_FEATURE_DESIGN.md §5 for the reasoning (shared
# event loop with every other bot on this process, TelegramRetryAfter still
# respected on top of this as a second line of defense, not a replacement).
_SEND_INTERVAL_SECONDS = 1.0 / 25


class NotificationsConfig(Protocol):
    """Duck-typed like every other feature Config Protocol in this codebase
    (features/payments.py's PaymentsConfig, features/voice_intake.py's
    VoiceIntakeConfig) — db_path required, admins_file optional. A host with
    no admin-file concept (including a from-scratch bot with no such
    attribute at all) simply never sets it; _is_admin below then treats
    every user as allowed, same fail-open convention voice_intake.py uses,
    since a host that never says who its admins are hasn't opted into
    gating this feature at all."""
    db_path: str
    admins_file: str | None


def _is_admin(user_id: int, admins_file: str | None) -> bool:
    """Reproduces features/voice_intake.py's _is_admin() exactly (same
    db.database.sync_bot_admins_json {"ids": [...]} shape, same fail-open
    when admins_file is unset, same fail-closed on an unreadable file) —
    kept as a separate copy rather than an import to avoid a
    notifications -> voice_intake dependency between two otherwise
    unrelated feature modules."""
    if not admins_file:
        return True
    try:
        import json

        with open(admins_file) as f:
            data = json.load(f)
        admins = data.get("ids", []) if isinstance(data, dict) else data
    except Exception:
        return False
    return str(user_id) in admins


async def init_notifications_tables(db_path: str) -> None:
    """Adds the subscribers table to a host's OWN per-bot db_path — same
    convention as features/payments.py's init_payments_tables. WAL mode for
    the same reason payments.py sets it: _track_subscriber below writes on
    EVERY private message across every bot using this feature, and a
    concurrent broadcast read (SELECT chat_id FROM subscribers) must not
    block those writes into "database is locked" more than necessary."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS subscribers (
                chat_id          INTEGER PRIMARY KEY,
                username         TEXT,
                subscribed_at    TEXT DEFAULT (datetime('now','localtime')),
                unsubscribed_at  TEXT
            )
        """)
        await db.commit()


init_db = init_notifications_tables


async def _upsert_subscriber(db_path: str, chat_id: int, username: str | None) -> None:
    """New chat_id -> inserted subscribed. Existing chat_id that had
    unsubscribed -> re-subscribed (unsubscribed_at cleared), matching
    docs/NOTIFICATIONS_FEATURE_DESIGN.md §6's "writes to the bot again ->
    counts as re-subscribing" decision. Existing still-subscribed chat_id ->
    only username refreshed (handles a username change), subscribed_at left
    alone rather than bumped on every single message."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout=5000")
        await db.execute(
            """
            INSERT INTO subscribers (chat_id, username, subscribed_at, unsubscribed_at)
            VALUES (?, ?, datetime('now','localtime'), NULL)
            ON CONFLICT(chat_id) DO UPDATE SET
                username = excluded.username,
                unsubscribed_at = NULL
            """,
            (chat_id, username),
        )
        await db.commit()


@router.message(F.chat.type == "private")
async def _track_subscriber(message: Message, config: NotificationsConfig) -> None:
    """Deliberately the lowest-priority, non-terminating observer this
    module registers: no filter beyond "private chat", never answers, never
    re-raises. A broken upsert here (bad db_path, disk full, etc.) must
    never surface as an error response to a user just because they sent the
    host bot ANY message — same isolation reasoning as
    features/payments.py's publish_event() try/except around a
    non-critical side effect."""
    if message.from_user is None:
        return
    try:
        await _upsert_subscriber(config.db_path, message.chat.id, message.from_user.username)
    except Exception:
        logger.exception(
            f"notifications._track_subscriber: failed to upsert chat_id={message.chat.id}"
        )


class BroadcastFlow(StatesGroup):
    awaiting_content = State()
    confirm = State()


def _cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Отменить", callback_data="notif_cancel"),
    ]])


def _confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📤 Отправить", callback_data="notif_send"),
        InlineKeyboardButton(text="❌ Отменить", callback_data="notif_cancel"),
    ]])


def _unsubscribe_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔕 Отписаться от рассылок", callback_data="notif_unsub"),
    ]])


async def _count_active_subscribers(db_path: str) -> int:
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM subscribers WHERE unsubscribed_at IS NULL"
        )
        row = await cursor.fetchone()
    return row[0] if row else 0


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, state: FSMContext, config: NotificationsConfig) -> None:
    if not _is_admin(message.from_user.id, getattr(config, "admins_file", None)):
        return
    await state.set_state(BroadcastFlow.awaiting_content)
    await message.answer(
        "📢 Отправьте текст или фото с подписью для рассылки подписчикам.",
        reply_markup=_cancel_kb(),
    )


@router.message(BroadcastFlow.awaiting_content, F.text | F.photo)
async def on_broadcast_content(message: Message, state: FSMContext, config: NotificationsConfig) -> None:
    if not _is_admin(message.from_user.id, getattr(config, "admins_file", None)):
        return
    photo_id = message.photo[-1].file_id if message.photo else None
    text = message.html_text or message.caption or ""
    await state.update_data(photo_id=photo_id, text=text)
    await state.set_state(BroadcastFlow.confirm)
    count = await _count_active_subscribers(config.db_path)
    preview = f"👥 Получателей: {count}\n\n{text}"
    if photo_id:
        await message.answer_photo(photo_id, caption=preview, parse_mode="HTML", reply_markup=_confirm_kb())
    else:
        await message.answer(preview, parse_mode="HTML", reply_markup=_confirm_kb())


@router.callback_query(BroadcastFlow.confirm, F.data == "notif_cancel")
@router.callback_query(BroadcastFlow.awaiting_content, F.data == "notif_cancel")
async def on_broadcast_cancel(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb.message.edit_text("❌ Рассылка отменена.")
    await cb.answer()


@dataclass
class _BroadcastResult:
    sent: int = 0
    failed: int = 0
    unsubscribed: int = 0


async def _send_one(bot, chat_id: int, text: str, photo_id: str | None) -> None:
    if photo_id:
        await bot.send_photo(
            chat_id, photo_id, caption=text, parse_mode="HTML",
            reply_markup=_unsubscribe_kb(),
        )
    else:
        await bot.send_message(
            chat_id, text, parse_mode="HTML", reply_markup=_unsubscribe_kb(),
        )


async def _send_broadcast(
    bot,
    db_path: str,
    text: str,
    photo_id: str | None,
) -> _BroadcastResult:
    """Sequential throttled send — see docs/NOTIFICATIONS_FEATURE_DESIGN.md
    §5 for why this is a plain sleep-paced loop rather than a
    concurrency-limited gather: a broadcast to a large audience is not
    latency-sensitive for the OWNER (it already runs as a detached
    background task, see cb_broadcast_send below) but a burst of concurrent
    sends is exactly what risks tripping Telegram's per-bot rate limit this
    loop exists to avoid. One recipient's failure (blocked bot, deleted
    account, bad chat_id) is isolated per-iteration and never aborts the
    remaining sends — same isolation principle as
    runtime/registry.py's _load_and_include_features() per-feature
    try/except."""
    result = _BroadcastResult()
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT chat_id FROM subscribers WHERE unsubscribed_at IS NULL"
        )
        rows = await cursor.fetchall()
    for (chat_id,) in rows:
        try:
            await _send_one(bot, chat_id, text, photo_id)
            result.sent += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                await _send_one(bot, chat_id, text, photo_id)
                result.sent += 1
            except Exception:
                result.failed += 1
                logger.warning(f"notifications._send_broadcast: retry failed for chat_id={chat_id}")
        except TelegramForbiddenError:
            # User blocked the bot (or deleted their account) — no point
            # keeping a dead chat_id around for the next broadcast to fail
            # on again, so this is treated the same as an explicit
            # unsubscribe (see docs/NOTIFICATIONS_FEATURE_DESIGN.md §6).
            result.unsubscribed += 1
            try:
                async with aiosqlite.connect(db_path) as db:
                    await db.execute(
                        "UPDATE subscribers SET unsubscribed_at = datetime('now','localtime') "
                        "WHERE chat_id = ?",
                        (chat_id,),
                    )
                    await db.commit()
            except Exception:
                logger.exception(
                    f"notifications._send_broadcast: failed to auto-unsubscribe chat_id={chat_id}"
                )
        except Exception:
            result.failed += 1
            logger.warning(
                f"notifications._send_broadcast: send failed for chat_id={chat_id}", exc_info=True
            )
        await asyncio.sleep(_SEND_INTERVAL_SECONDS)
    return result


# asyncio only holds a WEAK reference to a task once created — per the
# stdlib docs, if nothing else references it, the event loop's garbage
# collector may destroy it mid-execution (surfaces as a broadcast that
# silently stops partway through, no error logged). A broadcast task is
# exactly the long-lived, many-await case the docs warn about, so a strong
# reference is kept here for the task's lifetime and dropped via
# add_done_callback once it finishes either way.
_background_tasks: set[asyncio.Task] = set()


@router.callback_query(BroadcastFlow.confirm, F.data == "notif_send")
async def cb_broadcast_send(cb: CallbackQuery, state: FSMContext, config: NotificationsConfig) -> None:
    if not _is_admin(cb.from_user.id, getattr(config, "admins_file", None)):
        # Re-checked here (not just at cmd_broadcast/on_broadcast_content)
        # for defense-in-depth: this is the one handler in the flow that
        # actually sends to the whole subscriber base, so it must never rely
        # solely on an admin check done earlier in the conversation — e.g.
        # if admins_file is edited to demote someone between the confirm
        # screen being shown and this callback firing.
        await cb.answer()
        return
    data = await state.get_data()
    await state.clear()
    text = data.get("text", "")
    photo_id = data.get("photo_id")
    await cb.message.edit_text("📤 Рассылка запущена в фоне, отчёт придёт по завершении…")
    await cb.answer()

    async def _run() -> None:
        try:
            result = await _send_broadcast(cb.bot, config.db_path, text, photo_id)
        except Exception:
            logger.exception("notifications.cb_broadcast_send: broadcast task crashed")
            try:
                await cb.message.answer("❌ Рассылка прервалась из-за внутренней ошибки.")
            except Exception:
                logger.exception("notifications.cb_broadcast_send: failed to report crash to owner")
            return
        try:
            await cb.message.answer(
                f"✅ Рассылка завершена.\n"
                f"Отправлено: {result.sent}\n"
                f"Ошибок: {result.failed}\n"
                f"Отписалось (не приняли сообщение): {result.unsubscribed}"
            )
        except Exception:
            logger.exception("notifications.cb_broadcast_send: failed to report result to owner")

    task = asyncio.create_task(_run())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


@router.callback_query(F.data == "notif_unsub")
async def cb_unsubscribe(cb: CallbackQuery, config: NotificationsConfig) -> None:
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute("PRAGMA busy_timeout=5000")
        await db.execute(
            "UPDATE subscribers SET unsubscribed_at = datetime('now','localtime') WHERE chat_id = ?",
            (cb.from_user.id,),
        )
        await db.commit()
    await cb.answer("Вы отписались от рассылок. Напишите боту снова, чтобы возобновить.", show_alert=True)
