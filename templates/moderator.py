# TEMPLATE: moderator
# USE FOR: модерация чата, антиспам, запрещённые слова, лестница предупреждение-мут-бан, журнал модерации
# CUSTOMIZE: sections marked with # CUSTOMIZE
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import aiosqlite
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery, ChatMemberAdministrator, ChatMemberUpdated, ChatPermissions, FSInputFile,
    InlineKeyboardButton, InlineKeyboardMarkup, Message,
)

# ── CUSTOMIZE ────────────────────────────────────────────────────────────────
# Same status as every other template's CUSTOMIZE block: per-file source-text
# customization Claude edits when generating a specific bot, not per-bot
# runtime state (that's config.db_path/admins_file below).
BOT_DESCRIPTION = "Модерация группового чата: антиспам, запрещённые слова, лестница предупреждение → мут → бан, журнал модерации."
WELCOME_TEXT = (
    "🛡 <b>Модератор чата</b>\n\n"
    "Добавьте меня в группу и выдайте права администратора (пункты «Удаление "
    "сообщений» и «Блокировка пользователей») — и я начну следить за порядком: "
    "спам-ссылки, запрещённые слова, лестница предупреждение → мут → бан.\n\n"
    "Вся настройка (запрещённые слова, порог предупреждений, проверка прав) — "
    "прямо здесь, в личном чате: кнопка «⚙️ Настроить группу» ниже, дальше "
    "выберите нужную группу из списка."
)
# ── END CUSTOMIZE ─────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
router = Router()

# ── mini-app config (see docs/MINIAPP_DESIGN.md, templates/tour_operator.py's
# own miniapp_config for the pilot) ─────────────────────────────────────────
# Declarative schema read by runtime/miniapp_api.py's generic CRUD handlers —
# NOT executable code (see that module's docstring). `table` and each field's
# `name` must match init_db()'s CREATE TABLE columns below exactly.
# Only moderation_log is exposed here — every other table in this template's
# schema (chat_settings, stopwords, warnings, known_groups, dm_fallback_sent,
# bot_admins) uses a composite or non-`id` primary key
# (chat_id/user_id/word), which runtime/miniapp_api.py's generic handlers
# don't support (they SELECT/UPDATE by a single `id` column).
miniapp_config = {
    "resources": [
        {
            "name": "moderation_log",
            "table": "moderation_log",
            "order_by": "created_at DESC",
            "creatable": False,
            "title": "Журнал модерации",
            "titleField": "action",
            "fields": [
                {"name": "chat_id", "label": "ID чата", "kind": "number", "list": False, "detail": True, "create": False},
                {"name": "user_id", "label": "ID пользователя", "kind": "number", "list": True, "detail": True, "create": False},
                {"name": "action", "label": "Действие", "kind": "status", "list": True, "detail": True, "create": False},
                {"name": "reason", "label": "Причина", "kind": "text", "list": False, "detail": True, "create": False},
                {"name": "created_at", "label": "Дата", "kind": "date", "list": True, "detail": True, "create": False},
            ],
        },
    ],
}

# Per-chat locks guarding _replace_panel's read-delete-send-write sequence —
# see _replace_panel's docstring. A fast double-tap on a panel button (or two
# updates landing in the same getUpdates batch) could otherwise both read the
# same stale panel_msg_id, both send a new panel message, and the later
# state.update_data() win — leaking the other message (never tracked, never
# deleted on the next navigation). Module-level dict is fine here: it only
# ever grows by one entry per DISTINCT private chat the bot has ever shown a
# panel in, and Lock objects are tiny.
_panel_locks: dict[int, asyncio.Lock] = {}

# Same link-detection regex as the generic per-bot MODERATOR_EXTRA prompt in
# services/claude_service.py — kept consistent so a Claude-generated custom
# moderator bot and this fixed template flag the same things.
LINK_PATTERN = re.compile(
    r"(https?://|t\.me/|@[a-zA-Z0-9_]{5,}|bit\.ly|tinyurl\.com|vk\.cc)",
    re.IGNORECASE,
)


def _esc(value, max_len: int = 500) -> str:
    """HTML-escapes AND length-bounds any user-supplied text before it goes into
    a parse_mode="HTML" message — same helper/rationale as other templates'
    _esc(): free-text reasons/stopwords/error text could otherwise contain
    '<'/'&' and break message rendering, or run long enough to hit Telegram's
    message-length limit."""
    text = str(value) if value is not None else ""
    if len(text) > max_len:
        text = text[:max_len] + "…"
    return html.escape(text)


def _join_bounded(lines: list[str], limit: int = 3500) -> str:
    """Joins lines with a length budget, dropping only WHOLE trailing lines —
    same approach as other templates' _join_bounded(): a raw character-offset
    slice can land inside an open HTML span and produce unbalanced HTML that
    Telegram's parse_mode="HTML" rejects outright."""
    out: list[str] = []
    total = 0
    for line in lines:
        if total + len(line) + 1 > limit:
            out.append("…")
            break
        out.append(line)
        total += len(line) + 1
    return "\n".join(out)


# ── config ───────────────────────────────────────────────────────────────────
# Same pattern as every other template — see docs/STAGE2_DESIGN.md. No domain
# fields: settings/stopwords/warnings are per-chat_id DATA inside this bot's
# own db_path, not per-bot runtime config.

@dataclass
class ModeratorConfig:
    bot_name: str
    db_path: str
    admins_file: Path
    welcome_image: Path
    display_name: str | None = None
    group_chat_id: str | None = None
    bot_id: int | None = None
    owner_telegram_id: int | None = None


def _paths_for(name: str, data_dir: Path) -> ModeratorConfig:
    return ModeratorConfig(
        bot_name=name,
        db_path=str(data_dir / f"{name}_data.db"),
        admins_file=data_dir / f"admins_{name}.json",
        welcome_image=data_dir / "bot_images" / f"{name}.jpg",
    )


def config_from_env() -> ModeratorConfig:
    """Standalone/subprocess mode."""
    name = Path(__file__).stem
    data_dir = Path(os.getenv("DATA_DIR", "./data"))
    data_dir.mkdir(exist_ok=True)
    return _paths_for(name, data_dir)


def config_from_bot_row(bot_row: dict, data_dir: Path) -> ModeratorConfig:
    """Webhook runtime mode. Paths built from bot_row["bot_id"] (bots.id, the
    physically unique AUTOINCREMENT PK) — NOT bot_row["name"] — same reasoning
    as every other template's config_from_bot_row (see docs/STAGE2_DESIGN.md
    "Изоляция по bots.id")."""
    bot_id = bot_row["bot_id"]
    config = ModeratorConfig(
        bot_name=bot_row["name"],
        db_path=str(data_dir / f"bot_{bot_id}_data.db"),
        admins_file=data_dir / f"admins_{bot_id}.json",
        welcome_image=data_dir / "bot_images" / f"bot_{bot_id}.jpg",
    )
    config.display_name = bot_row.get("display_name")
    config.group_chat_id = bot_row.get("group_chat_id")
    config.bot_id = bot_id
    config.owner_telegram_id = bot_row.get("owner_telegram_id")
    return config


class ConfigMiddleware(BaseMiddleware):
    """Injects this bot's ModeratorConfig into data["config"]."""

    def __init__(self, config: ModeratorConfig) -> None:
        self.config = config
        super().__init__()

    async def __call__(self, handler, event, data):
        data["config"] = self.config
        return await handler(event, data)


# ── admin helpers (bot_admins table — bot owner's admins, used for /modlog) ──
# Storage used to be a JSON admins_file with a plain read-modify-write (no
# locking), which meant two admins changing the set at the same time could
# silently clobber each other's change. Replaced with the bot_admins SQLite
# table below, using the same BEGIN IMMEDIATE locking as _apply_escalation
# and _claim_dm_fallback_slot elsewhere in this file. _load_admins (JSON) is
# kept ONLY for _migrate_admins_file_to_db's one-time import — every runtime
# read/write goes through the async helpers below instead.

def _load_admins(admins_file: Path) -> set:
    try:
        return set(json.loads(admins_file.read_text()).get("ids", []))
    except Exception:
        return set()


async def _list_bot_admins(db_path: str) -> set:
    async with aiosqlite.connect(db_path) as db:
        rows = await (await db.execute("SELECT id FROM bot_admins")).fetchall()
    return {r[0] for r in rows}


async def _is_bot_admin_db(db_path: str, user_id: int) -> bool:
    async with aiosqlite.connect(db_path) as db:
        row = await (await db.execute(
            "SELECT 1 FROM bot_admins WHERE id=?", (str(user_id),)
        )).fetchone()
    return row is not None


async def _add_bot_admin(db_path: str, admin_id: str) -> None:
    async with aiosqlite.connect(db_path, timeout=10) as db:
        await db.execute("INSERT OR IGNORE INTO bot_admins (id) VALUES (?)", (admin_id,))
        await db.commit()


async def _replace_bot_admins(db_path: str, ids: set) -> None:
    """Replaces the whole bot_admins set — same replace-not-merge semantics
    as the old JSON _save_admins. Not used by any runtime handler (they all
    go through _add_bot_admin/_remove_bot_admin, which change one id at a
    time); exists for test fixtures that need to seed/overwrite the full
    admin set in one call."""
    async with aiosqlite.connect(db_path, timeout=10) as db:
        await db.execute("DELETE FROM bot_admins")
        if ids:
            await db.executemany("INSERT INTO bot_admins (id) VALUES (?)", [(i,) for i in ids])
        await db.commit()


async def _remove_bot_admin(db_path: str, admin_id: str) -> str:
    """Atomically removes admin_id from bot_admins, refusing to remove the
    last remaining admin — same BEGIN IMMEDIATE pattern as
    _claim_dm_fallback_slot, closing the race the old in-memory
    `len(ids) <= 1` check couldn't: two admins concurrently removing two
    DIFFERENT other admins, each reading a stale count that still included
    the other's target, could previously both proceed and empty the set.
    Returns "removed", "not_found", or "last_admin"."""
    async with aiosqlite.connect(db_path, timeout=10) as db:
        await db.execute("BEGIN IMMEDIATE")
        row = await (await db.execute("SELECT 1 FROM bot_admins WHERE id=?", (admin_id,))).fetchone()
        if row is None:
            await db.commit()
            return "not_found"
        count = (await (await db.execute("SELECT COUNT(*) FROM bot_admins")).fetchone())[0]
        if count <= 1:
            await db.commit()
            return "last_admin"
        await db.execute("DELETE FROM bot_admins WHERE id=?", (admin_id,))
        await db.commit()
        return "removed"


async def _claim_first_bot_admin(db_path: str, user_id: str) -> bool:
    """Atomically bootstraps user_id as the bot's first admin iff bot_admins
    is currently empty — same BEGIN IMMEDIATE pattern as the rest of this
    section, closing a TOCTOU the old cmd_start had: two people /start-ing
    the fresh bot at nearly the same moment could both see an empty admin
    set and both get told "you're the admin", even though only one of them
    should have been. Returns True iff THIS call won the bootstrap."""
    async with aiosqlite.connect(db_path, timeout=10) as db:
        await db.execute("BEGIN IMMEDIATE")
        count = (await (await db.execute("SELECT COUNT(*) FROM bot_admins")).fetchone())[0]
        if count > 0:
            await db.commit()
            return False
        await db.execute("INSERT INTO bot_admins (id) VALUES (?)", (user_id,))
        await db.commit()
        return True


async def _is_group_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Live Telegram-status check of the CALLING user in THIS group — the
    authority for /addstopword & co is native Telegram admin/creator status,
    NOT admins_file (see docs/STAGE2_DESIGN.md "Настройка стоп-слов/порогов —
    ... НЕ через admins_file бота")."""
    try:
        member = await bot.get_chat_member(chat_id, user_id)
    except Exception:
        return False
    return member.status in ("administrator", "creator")


# ── db ────────────────────────────────────────────────────────────────────────

async def init_db(db_path: str):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS chat_settings (
                chat_id      INTEGER PRIMARY KEY,
                delete_links INTEGER DEFAULT 1,
                max_warnings INTEGER DEFAULT 3,
                mute_minutes INTEGER DEFAULT 60
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS stopwords (
                chat_id INTEGER,
                word    TEXT,
                PRIMARY KEY (chat_id, word)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS warnings (
                user_id        INTEGER,
                chat_id        INTEGER,
                count          INTEGER DEFAULT 0,
                stage          TEXT DEFAULT 'warn',
                last_violation TEXT,
                PRIMARY KEY (user_id, chat_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS moderation_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id    INTEGER NOT NULL,
                user_id    INTEGER NOT NULL,
                action     TEXT NOT NULL,
                reason     TEXT,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        # Groups this bot instance currently belongs to — populated/kept fresh by
        # on_bot_membership_changed, read by the private-chat group picker (see
        # docs/STAGE2_DESIGN.md "Настройка из личного чата с выбором группы").
        # Row removed on leave/kick so a group the bot can no longer act in never
        # shows up as a selectable option.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS known_groups (
                chat_id    INTEGER PRIMARY KEY,
                chat_title TEXT,
                added_at   TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        # Tracks which groups have already received the ONE-TIME "напишите мне
        # /start в личку" fallback notice (see _notify_recipient_privately_
        # with_group_fallback below) — a Telegram platform limit means a bot
        # can't DM someone who's never opened a chat with it first, so on
        # DM failure this template posts that single instruction into the
        # group instead. Without this table, EVERY subsequent rights-related
        # event in a group whose admin still hasn't DMed the bot (membership
        # changes, repeated /checkrights) would post the same notice again,
        # right back to the group-spam problem this whole feature exists to
        # avoid.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS dm_fallback_sent (
                chat_id INTEGER PRIMARY KEY,
                sent_at TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        # Bot-admin IDs (who may use /addadmin, the "👥 Админы" panel, /modlog).
        # Replaces the old admins_file JSON store — a plain read-modify-write
        # over a file has no cross-call locking, so two admins adding/removing
        # different IDs at the same time could silently clobber each other's
        # change (whichever _save_admins finished last won). SQLite's
        # BEGIN IMMEDIATE (see _add_bot_admin/_remove_bot_admin) closes that
        # race the same way _apply_escalation and _claim_dm_fallback_slot
        # already do elsewhere in this file.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bot_admins (
                id TEXT PRIMARY KEY
            )
        """)
        await db.commit()
    await _migrate_admins_file_to_db(db_path)


def _admins_file_for_db_path(db_path: str) -> Path:
    """Derives the legacy admins_file path from db_path — both are built from
    the same stem by _paths_for/config_from_bot_row ("{name}_data.db" /
    "admins_{name}.json", or "bot_{id}_data.db" / "admins_{id}.json"), so the
    "_data" suffix is the only part that differs. init_db only receives
    db_path (see runtime/registry.py's generic `module.init_db(config.db_path)`
    call, shared by every template), so migration has to work from that alone
    rather than taking a ModeratorConfig."""
    stem = Path(db_path).stem
    if stem.endswith("_data"):
        stem = stem[: -len("_data")]
    return Path(db_path).with_name(f"admins_{stem}.json")


async def _migrate_admins_file_to_db(db_path: str) -> None:
    """One-time import of the legacy admins_file JSON store into bot_admins —
    runs on every startup but is a no-op once the table is non-empty, so it's
    safe to call unconditionally. The JSON file is left on disk untouched
    (not deleted) as a backup; bot_admins is the only store read/written from
    here on."""
    admins_file = _admins_file_for_db_path(db_path)
    if not admins_file.exists():
        return
    async with aiosqlite.connect(db_path, timeout=10) as db:
        await db.execute("BEGIN IMMEDIATE")
        row = await (await db.execute("SELECT 1 FROM bot_admins LIMIT 1")).fetchone()
        if row is not None:
            await db.commit()
            return
        ids = _load_admins(admins_file)
        if ids:
            await db.executemany(
                "INSERT OR IGNORE INTO bot_admins (id) VALUES (?)",
                [(i,) for i in ids],
            )
            logger.info(f"_migrate_admins_file_to_db: imported {len(ids)} admin id(s) from {admins_file}")
        await db.commit()


# ── known_groups (private-chat group picker) ──────────────────────────────────

async def _upsert_known_group(db_path: str, chat_id: int, chat_title: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO known_groups (chat_id, chat_title) VALUES (?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET chat_title=excluded.chat_title",
            (chat_id, chat_title),
        )
        await db.commit()


async def _forget_known_group(db_path: str, chat_id: int) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute("DELETE FROM known_groups WHERE chat_id=?", (chat_id,))
        await db.commit()


async def _known_groups(db_path: str) -> list[tuple[int, str]]:
    async with aiosqlite.connect(db_path) as db:
        rows = await (await db.execute(
            "SELECT chat_id, chat_title FROM known_groups ORDER BY chat_title COLLATE NOCASE, chat_id"
        )).fetchall()
    return [(r[0], r[1]) for r in rows]


# ── rights checking (see docs/STAGE2_DESIGN.md "ГЛАВНОЕ ТРЕБОВАНИЕ ВЛАДЕЛЬЦА")
# ──────────────────────────────────────────────────────────────────────────────
# Moderation (delete/mute/ban) is physically impossible without the bot having
# Telegram admin rights IN THAT SPECIFIC GROUP — an external action the group's
# own admin has to take, not a bot setting. Three mechanisms below cover it:
#  1. on_bot_membership_changed — reacts to ANY change of the bot's own status
#     (not just "added"), since granting rights is usually a SECOND, separate
#     action after the bot is first added as a plain member.
#  2. /checkrights — manual re-check at any time.
#  3. _moderate_safely — explicit failure notice (to admins_file admins, not
#     the group) if a real moderation action fails at the moment it runs.
#
# All three DM the relevant admin(s) privately rather than posting into the
# group (see _notify_recipient_privately_with_group_fallback) — the owner's
# requirement is that rights/status notices reach a specific admin, not the
# whole group. Telegram, however, never lets a bot message-first a user who
# hasn't opened a chat with it — so #1 and #2 fall back to ONE short group
# notice ("write me /start first") if that specific DM bounces, tracked in
# dm_fallback_sent so it's posted at most once per group ever.

RIGHTS_INSTRUCTIONS = (
    "⚠️ <b>У бота недостаточно прав для модерации в этой группе.</b>\n\n"
    "Чтобы включить модерацию:\n"
    "1. Настройки группы → «Администраторы»\n"
    "2. Выберите бота (или «Добавить администратора» → найдите бота)\n"
    "3. Включите «Удаление сообщений» и «Блокировка пользователей»\n"
    "4. Сохраните\n\n"
    "После этого проверьте права командой /checkrights."
)


def _rights_status_text(member) -> tuple[bool, str]:
    """Pure form of the rights check — no I/O, just `member` (the BOT's OWN
    ChatMember status in some chat) -> (has_rights, human-readable status
    text). Split out from _check_and_report_rights so the private-chat
    per-group panel (which must show rights status WITHOUT posting into the
    group — see docs/STAGE2_DESIGN.md "Настройка из личного чата") can reuse
    the exact same check without a side-effecting send.

    ChatMemberAdministrator.can_delete_messages + can_restrict_members is the
    one combination that covers all three moderation actions (delete via
    delete_message, mute via restrict_chat_member, ban via ban_chat_member —
    restrict_chat_member and ban_chat_member are BOTH gated by the single
    can_restrict_members field). Every other status (member/left/restricted/
    banned) is treated as insufficient without further field inspection."""
    has_rights = (
        isinstance(member, ChatMemberAdministrator)
        and member.can_delete_messages
        and member.can_restrict_members
    )
    if has_rights:
        return True, "✅ Всё в порядке, у бота есть права на удаление/мут/бан."
    return False, RIGHTS_INSTRUCTIONS


DM_UNREACHABLE_FALLBACK_TEXT = (
    "Не могу написать вам в личку — сначала напишите мне /start в личных "
    "сообщениях, тогда все уведомления будут приходить только вам."
)


def _is_dm_unreachable_error(e: Exception) -> bool:
    """True for the Telegram error shapes that specifically mean 'this user
    can't currently be DMed first' — either they've never opened a chat with
    the bot (TelegramForbiddenError "bot can't initiate conversation with a
    user", TelegramBadRequest "chat not found") or they blocked the bot after
    previously using it (TelegramForbiddenError "bot was blocked by the
    user" — same practical dead end: Telegram won't deliver anything to them
    either way, and the admin needs to /start or unblock the bot again
    before it can reach them). Anything else (rate limits, network errors,
    server errors) must NOT trigger the group fallback — that would turn a
    transient blip into unnecessary group spam."""
    text = str(e).lower()
    return (
        "can't initiate conversation" in text
        or "chat not found" in text
        or "bot was blocked by the user" in text
    )


async def _claim_dm_fallback_slot(db_path: str, chat_id: int) -> bool:
    """Atomically claims the one-time group-fallback slot for chat_id —
    returns True iff THIS call is the one that should actually post the
    fallback message (a later caller, or one that lost the race, gets
    False). BEGIN IMMEDIATE takes SQLite's write lock before the read, so a
    concurrent caller's own BEGIN IMMEDIATE blocks (up to `timeout` below)
    until this transaction's commit() releases it — same pattern as
    _apply_escalation's warn/mute/ban race fix elsewhere in this file, and
    for the same reason: two near-simultaneous rights events for the same
    group (e.g. "added" immediately followed by "promoted") must never both
    read the slot as unclaimed.

    The slot is marked claimed BEFORE the group send is attempted (not
    after) — the only way to guarantee two concurrent callers can't both
    win. Trade-off: if the send itself then fails (bot kicked mid-send,
    network error), the slot stays claimed and won't be retried on a later
    event. Accepted deliberately: the alternative (marking after a
    successful send) reopens the exact TOCTOU gap this function exists to
    close, in exchange for guarding against a rarer failure."""
    async with aiosqlite.connect(db_path, timeout=10) as db:
        await db.execute("BEGIN IMMEDIATE")
        row = await (await db.execute(
            "SELECT 1 FROM dm_fallback_sent WHERE chat_id=?", (chat_id,)
        )).fetchone()
        if row is not None:
            await db.commit()
            return False
        await db.execute("INSERT INTO dm_fallback_sent (chat_id) VALUES (?)", (chat_id,))
        await db.commit()
    return True


async def _forget_dm_fallback_sent(db_path: str, chat_id: int) -> None:
    """Companion to _forget_known_group — called on the same bot-left/kicked
    event. Without this, a group the bot was removed from and later re-added
    to (a different admin, a repurposed group, or the same admin trying
    again after fixing the DM issue) would inherit the OLD instance's
    fallback-already-sent state forever, even though this is functionally a
    fresh membership and deserves its own one-time notice if the new admin
    also can't be DMed."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("DELETE FROM dm_fallback_sent WHERE chat_id=?", (chat_id,))
        await db.commit()


async def _notify_recipient_privately_with_group_fallback(
    bot: Bot, config: ModeratorConfig, group_chat_id: int, recipient_id: int, text: str, *, parse_mode: str | None = None,
) -> None:
    """DMs `text` to recipient_id. Telegram's platform limit means a bot can
    never message-first a user who hasn't opened a chat with it — if THAT is
    why the DM failed, falls back to posting DM_UNREACHABLE_FALLBACK_TEXT
    into group_chat_id, but only the FIRST time this happens for that group
    (see _claim_dm_fallback_slot/dm_fallback_sent) so a group whose admin
    still hasn't DMed the bot doesn't get the same instruction re-posted on
    every later rights event."""
    try:
        await bot.send_message(recipient_id, text, parse_mode=parse_mode)
        return
    except TelegramAPIError as e:
        if not _is_dm_unreachable_error(e):
            logger.error(f"_notify_recipient_privately_with_group_fallback: DM to {recipient_id} failed (group {group_chat_id}): {e}")
            return
        logger.info(
            f"_notify_recipient_privately_with_group_fallback: DM to {recipient_id} unreachable ({e}) — "
            f"considering group fallback for chat {group_chat_id}"
        )
    except Exception as e:
        logger.error(f"_notify_recipient_privately_with_group_fallback: unexpected error DMing {recipient_id} (group {group_chat_id}): {e}")
        return

    try:
        won_the_slot = await _claim_dm_fallback_slot(config.db_path, group_chat_id)
    except Exception as e:
        # Fails CLOSED (no notification at all this time) rather than risking
        # a duplicate post on a DB hiccup — matches every other failure path
        # in this function, which logs and gives up rather than guessing.
        logger.error(f"_notify_recipient_privately_with_group_fallback: failed to check/claim fallback slot for chat {group_chat_id}: {e}")
        return
    if not won_the_slot:
        logger.info(f"_notify_recipient_privately_with_group_fallback: fallback already sent for chat {group_chat_id} — skipping")
        return
    try:
        await bot.send_message(group_chat_id, DM_UNREACHABLE_FALLBACK_TEXT)
    except Exception as e:
        logger.error(f"_notify_recipient_privately_with_group_fallback: group fallback to {group_chat_id} also failed: {e}")


async def _check_and_report_rights(
    bot: Bot, config: ModeratorConfig, chat_id: int, member, recipient_id: int, silent_if_ok: bool,
) -> bool:
    """DMs the _rights_status_text() result to recipient_id (falling back to
    a one-time group notice per _notify_recipient_privately_with_group_
    fallback if that DM can't be delivered) — used by the two group-facing
    mechanisms (on_bot_membership_changed, /checkrights). Returns whether
    rights are sufficient."""
    has_rights, text = _rights_status_text(member)
    if has_rights and silent_if_ok:
        return has_rights
    await _notify_recipient_privately_with_group_fallback(bot, config, chat_id, recipient_id, text, parse_mode="HTML")
    return has_rights


# Statuses in which the bot is still actually present in the chat — kept in
# known_groups. The complement (left/kicked) means the bot can no longer act
# there at all, so the row is dropped instead of lingering as a dead option
# in the private-chat picker.
_PRESENT_STATUSES = {"member", "administrator", "restricted"}
_REMOVED_STATUSES = {"left", "kicked"}


@router.my_chat_member()
async def on_bot_membership_changed(update: ChatMemberUpdated, bot: Bot, config: ModeratorConfig):
    """No direction filter (not just IS_NOT_MEMBER >> IS_MEMBER) — on Telegram
    the bot is typically added as a plain member first, then promoted to admin
    as a SEPARATE later action; a narrower filter would miss the moment rights
    actually arrive. Reads update.new_chat_member directly instead of issuing
    a fresh get_chat_member call — the bot's new status is already IN this
    update.

    Also the sole write point for known_groups (see docs/STAGE2_DESIGN.md
    "Настройка из личного чата с выбором группы") — every membership change
    either upserts (bot still present: title kept fresh in case the group was
    renamed) or removes (bot left/kicked) this chat's row, so the private-chat
    picker always reflects groups the bot can currently act in. A removal
    also clears dm_fallback_sent for the same chat_id (see
    _forget_dm_fallback_sent) — a later re-add is a fresh membership and
    deserves its own one-time fallback notice if it hits the same DM problem.

    The rights notice (if any) is DMed to update.from_user — the Telegram
    user who actually performed this membership change (added the bot,
    promoted/demoted it), delivered directly in the my_chat_member event
    itself, so no extra API call is needed to find them. update.from_user is
    a REQUIRED field on Telegram's my_chat_member update (aiogram's
    ChatMemberUpdated model enforces this at parse time — a payload missing
    it would fail validation before ever reaching this handler), so there's
    no "no actor" case to guard against here."""
    if update.chat.type not in ("group", "supergroup"):
        return
    status = update.new_chat_member.status
    if status in _PRESENT_STATUSES:
        title = update.chat.title or str(update.chat.id)
        await _upsert_known_group(config.db_path, update.chat.id, title)
        logger.info(f"on_bot_membership_changed: known_groups upsert chat_id={update.chat.id} title={title!r} status={status}")
        # Only check/report rights while the bot is actually still present —
        # for a left/kicked event below there is nothing to have rights
        # OVER anymore. Review-found: with the OLD group-posting behavior
        # this was harmless (the bot can't post into a group it was just
        # removed from, so the send silently failed and got logged); but now
        # that the notice is DMed to update.from_user, running this
        # unconditionally would successfully deliver a confusing "insufficient
        # rights" DM to whoever just KICKED the bot.
        await _check_and_report_rights(
            bot, config, update.chat.id, update.new_chat_member, update.from_user.id, silent_if_ok=True,
        )
    elif status in _REMOVED_STATUSES:
        await _forget_known_group(config.db_path, update.chat.id)
        await _forget_dm_fallback_sent(config.db_path, update.chat.id)
        logger.info(f"on_bot_membership_changed: known_groups forget chat_id={update.chat.id} status={status}")


@router.message(Command("checkrights"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_checkrights(msg: Message, bot: Bot, config: ModeratorConfig):
    if not await _is_group_admin(bot, msg.chat.id, msg.from_user.id):
        await msg.answer("⛔ Команда доступна только администраторам группы.")
        return
    try:
        bot_member = await bot.get_chat_member(msg.chat.id, bot.id)
    except Exception as e:
        logger.error(f"cmd_checkrights: get_chat_member for bot failed in chat {msg.chat.id}: {e}")
        await msg.answer("⚠️ Не удалось проверить права — попробуйте ещё раз позже.")
        return
    await _check_and_report_rights(bot, config, msg.chat.id, bot_member, msg.from_user.id, silent_if_ok=False)


async def _moderate_safely(bot: Bot, config: ModeratorConfig, chat_id: int, action: str, coro) -> bool:
    """Runs a moderation action (delete/mute/ban) that requires elevated
    Telegram rights. On failure (most commonly: rights were revoked or never
    granted) — logs AND explicitly DMs config.admins_file admins (not the
    group itself: spamming the group would (a) flood it with one error per
    offending message, and (b) tell rule-breakers moderation is broken).
    /checkrights remains the group's own way to check status at any time."""
    try:
        await coro
        return True
    except TelegramAPIError as e:
        # Catches the whole Telegram* hierarchy (not just BadRequest/Forbidden) —
        # review-found gap: TelegramRetryAfter (flood control) and
        # TelegramNetworkError/TelegramServerError are NOT subclasses of either
        # of those two, and flood control is exactly what tends to fire during
        # the spam wave this wrapper exists to handle. A narrower except here
        # would let the exception escape moderate_message() unhandled, silently
        # dropping the violation from warnings/moderation_log with no DM either.
        logger.error(f"_moderate_safely: action={action} chat_id={chat_id} failed: {e}")
        for admin_id in await _list_bot_admins(config.db_path):
            try:
                await bot.send_message(
                    int(admin_id),
                    f"🚫 <b>Не удалось выполнить модерацию (чат {chat_id})</b>\n\n"
                    f"Действие: {_esc(action)}\nОшибка: {_esc(e)}\n\n"
                    "Вероятная причина — у бота не хватает прав администратора в этой группе. "
                    "Проверить можно командой /checkrights прямо в группе.",
                    parse_mode="HTML",
                )
            except Exception as notify_err:
                logger.warning(f"_moderate_safely: failed to DM admin {admin_id}: {notify_err}")
        return False


# ── moderation: detection + escalation ladder ─────────────────────────────────

async def _apply_escalation(
    bot: Bot, config: ModeratorConfig, chat_id: int, user, reason: str, max_warnings: int, mute_minutes: int,
) -> None:
    """warn (count < max_warnings) -> mute (count reaches max_warnings) -> ban
    (next violation while already muted) — see docs/STAGE2_DESIGN.md "Лестница
    эскалации". One `stage` column, no separate mute/ban counters."""
    user_id = user.id
    async with aiosqlite.connect(config.db_path, timeout=10) as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            "INSERT OR IGNORE INTO warnings (user_id, chat_id, count, stage) VALUES (?,?,0,'warn')",
            (user_id, chat_id),
        )
        await db.commit()
        # Review-found race: a bare SELECT-decide-UPDATE here let two near-
        # simultaneous violations from the SAME user (a spam burst — exactly
        # this bot's core scenario) both read the same count/stage and both
        # write the same result — a lost update that skips/delays warn->mute
        # ->ban and can double-fire the public notice. BEGIN IMMEDIATE takes
        # SQLite's write lock right here, before the read, so a concurrent
        # call's own BEGIN IMMEDIATE blocks (up to `timeout` above) until this
        # transaction's commit() below releases it — serializing the whole
        # read-decide-write per (user_id, chat_id) without locking any other
        # row. The lock is released at commit(), before the network calls
        # further down each branch, so it's never held across an await on
        # Telegram's API.
        await db.execute("BEGIN IMMEDIATE")
        row = await (await db.execute(
            "SELECT count, stage FROM warnings WHERE user_id=? AND chat_id=?", (user_id, chat_id)
        )).fetchone()
        count, stage = row["count"], row["stage"]

        if stage == "warn" and count < max_warnings:
            count += 1
            await db.execute(
                "UPDATE warnings SET count=?, last_violation=datetime('now','localtime') "
                "WHERE user_id=? AND chat_id=?",
                (count, user_id, chat_id),
            )
            await db.execute(
                "INSERT INTO moderation_log (chat_id, user_id, action, reason) VALUES (?,?,?,?)",
                (chat_id, user_id, "warn", reason),
            )
            await db.commit()
            try:
                await bot.send_message(
                    chat_id,
                    f"⚠️ {user.mention_html()}, нарушение правил ({_esc(reason)}). "
                    f"Предупреждение {count}/{max_warnings}.",
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.warning(f"_apply_escalation: warn notice failed in chat {chat_id}: {e}")
            return

        if stage == "warn":  # count >= max_warnings
            await db.execute(
                "UPDATE warnings SET stage='muted', count=0, last_violation=datetime('now','localtime') "
                "WHERE user_id=? AND chat_id=?",
                (user_id, chat_id),
            )
            await db.execute(
                "INSERT INTO moderation_log (chat_id, user_id, action, reason) VALUES (?,?,?,?)",
                (chat_id, user_id, "mute", reason),
            )
            await db.commit()
            ok = await _moderate_safely(
                bot, config, chat_id, "mute",
                bot.restrict_chat_member(
                    chat_id, user_id,
                    permissions=ChatPermissions(can_send_messages=False),
                    until_date=timedelta(minutes=mute_minutes),
                ),
            )
            if ok:
                try:
                    await bot.send_message(
                        chat_id,
                        f"🔇 {user.mention_html()} получил мут на {mute_minutes} мин. за повторные нарушения.",
                        parse_mode="HTML",
                    )
                except Exception as e:
                    logger.warning(f"_apply_escalation: mute notice failed in chat {chat_id}: {e}")
            return

        # stage == "muted": already muted once before -> next violation bans.
        if stage == "muted":
            await db.execute(
                "UPDATE warnings SET stage='banned', last_violation=datetime('now','localtime') "
                "WHERE user_id=? AND chat_id=?",
                (user_id, chat_id),
            )
            await db.execute(
                "INSERT INTO moderation_log (chat_id, user_id, action, reason) VALUES (?,?,?,?)",
                (chat_id, user_id, "ban", reason),
            )
            await db.commit()
            ok = await _moderate_safely(bot, config, chat_id, "ban", bot.ban_chat_member(chat_id, user_id))
            if ok:
                try:
                    await bot.send_message(
                        chat_id, f"🚫 {user.mention_html()} заблокирован за повторные нарушения.", parse_mode="HTML"
                    )
                except Exception as e:
                    logger.warning(f"_apply_escalation: ban notice failed in chat {chat_id}: {e}")
            return
        # stage == "banned": already banned, nothing further to do.


@router.message(
    F.chat.type.in_({"group", "supergroup"}), F.text | F.caption, ~F.text.startswith("/"),
    StateFilter(None),
)
async def moderate_message(msg: Message, bot: Bot, config: ModeratorConfig):
    # StateFilter(None) added for the button-driven admin panel below: while a
    # SPECIFIC admin is mid-flow (e.g. just pressed "➕ Добавить запрещённое
    # слово" and is about to type the word itself), their next plain-text
    # reply in the group must reach the ModPanelFlow message handler, not get
    # scanned/deleted as ordinary content here. Router dispatch stops at the
    # FIRST handler whose entire filter set passes (see the "/addstopword"
    # note just below) — without this, this handler's filters would still
    # all pass for that admin's reply and "claim" the update, and the
    # ModPanelFlow handler (filtered on that specific state) would never see
    # it. StateFilter checks the state keyed by THIS message's own (chat,
    # user) pair, so every other, non-flow message in the group (including
    # from other users, or from this same admin once the flow ends) is
    # unaffected and still scanned normally.

    # Review-found bypass: the old filter matched F.text only, so a spam link/
    # stopword sent as a PHOTO CAPTION (msg.caption, a completely separate
    # field from msg.text) never reached this handler at all — full antispam
    # bypass. Widened to F.text | F.caption.
    #
    # `~F.text.startswith("/")` MUST stay a ROUTER-LEVEL filter, not an
    # in-body `if ...: return` — aiogram stops propagating an update to
    # further handlers as soon as ONE handler's filters all pass and it runs,
    # regardless of what the handler's body does. An in-body early return on a
    # "/addstopword ..." message would make THIS handler "claim" the update
    # and silently swallow it, so it would never reach cmd_addstopword's own
    # Command()-filtered handler below (this was a real, review-triggered
    # regression during the fix — caught by the existing command tests).
    # `~F.text.startswith("/")` resolves to True (filter passes) when
    # msg.text is None, i.e. for a caption-only message — so this correctly
    # exempts real TEXT commands only, while any caption (even one starting
    # with "/", which Telegram/aiogram never treats as a real command since
    # Command() only ever looks at .text) still reaches this handler and gets
    # scanned as ordinary content.
    content = msg.text or msg.caption or ""

    if not msg.from_user or msg.from_user.is_bot:
        return
    try:
        member = await bot.get_chat_member(msg.chat.id, msg.from_user.id)
        if member.status in ("administrator", "creator"):
            return
    except Exception as e:
        # Review-found: this used to fail open SILENTLY (no log at all) — a
        # transient get_chat_member error (e.g. rate-limiting during the very
        # spam flood this bot exists to handle) would treat the sender as an
        # admin and skip moderation with zero trace. Still fails open (safer
        # than falsely accusing a real admin of spam on a flaky API call), but
        # now it's loud, matching every other except-branch in this file.
        logger.warning(
            f"moderate_message: get_chat_member failed for sender "
            f"{msg.from_user.id if msg.from_user else '?'} in chat {msg.chat.id}: {e} — "
            "failing open (treating as admin, skipping moderation for this message)"
        )
        return

    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("INSERT OR IGNORE INTO chat_settings (chat_id) VALUES (?)", (msg.chat.id,))
        await db.commit()
        settings = await (await db.execute(
            "SELECT delete_links, max_warnings, mute_minutes FROM chat_settings WHERE chat_id=?", (msg.chat.id,)
        )).fetchone()
        stopword_rows = await (await db.execute(
            "SELECT word FROM stopwords WHERE chat_id=?", (msg.chat.id,)
        )).fetchall()

    stopwords = [r["word"] for r in stopword_rows]
    text_lower = content.lower()
    reason = None
    if settings["delete_links"] and LINK_PATTERN.search(content):
        reason = "спам-ссылка"
    elif any(word and word in text_lower for word in stopwords):
        reason = "запрещённое слово"

    if reason is None:
        return

    await _moderate_safely(bot, config, msg.chat.id, "delete_message", msg.delete())
    # Escalation runs regardless of whether the delete above succeeded — a
    # missing "delete" right must not also silently suppress warn/mute/ban
    # tracking; each elevated action reports its own failure independently.
    await _apply_escalation(bot, config, msg.chat.id, msg.from_user, reason, settings["max_warnings"], settings["mute_minutes"])


# ── group settings commands (Telegram-native admin/creator only) ─────────────
# Raw text commands (/addstopword, /removestopword, /stopwords, /setmaxwarnings)
# stay fully functional for backward compatibility with anyone who types them
# by hand, but the primary UI is the button panel below cmd_stopwords — see
# docs/STAGE2_DESIGN.md "никаких сырых текстовых списков команд".

async def _stopwords_for_chat(db_path: str, chat_id: int) -> list[str]:
    async with aiosqlite.connect(db_path) as db:
        rows = await (await db.execute(
            "SELECT word FROM stopwords WHERE chat_id=? ORDER BY word", (chat_id,)
        )).fetchall()
    return [r[0] for r in rows]


def _stopwords_panel_text(words: list[str]) -> str:
    if not words:
        return "Список запрещённых слов пуст."
    return _join_bounded(["🚫 <b>Запрещённые слова:</b>\n"] + [f"• {_esc(w)}" for w in words])


def kb_stopwords_panel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить запрещённое слово", callback_data="mod_addword")],
        [InlineKeyboardButton(text="➖ Убрать запрещённое слово", callback_data="mod_removeword")],
        [InlineKeyboardButton(text="⚙️ Порог предупреждений", callback_data="mod_setwarn")],
    ])

def kb_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Отмена", callback_data="mod_cancel")
    ]])

# Kept small enough that the resulting keyboard never risks Telegram's
# per-message inline-button limits — above this, removal falls back to the
# "type the word" flow instead of one button per word.
MAX_REMOVE_BUTTONS = 30

def kb_remove_words(words: list[str]) -> InlineKeyboardMarkup:
    rows = []
    for i, w in enumerate(words):
        label = w if len(w) <= 40 else w[:37] + "…"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"mod_rmw:{i}")])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="mod_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


class ModPanelFlow(StatesGroup):
    add_word = State(); remove_word_pick = State(); remove_word_text = State(); max_warnings = State()

# Review-found blocker: MemoryStorage keeps a state until explicitly cleared —
# with no expiry, an admin who presses a panel button and then gets
# distracted has every LATER plain message they send in the group (as long
# as it happens to look like a valid word/number) silently accepted as if it
# were the flow's reply — e.g. an unrelated one-word chat message becomes a
# new запрещённое слово, or a stray short number silently rewrites the
# warning threshold. Every flow-start handler stamps `started_at`; every
# continuation checks it first and expires the flow instead of acting on
# stale input.
FLOW_TIMEOUT_SECONDS = 300


def _flow_expired(data: dict) -> bool:
    started_at = data.get("started_at")
    return started_at is None or (time.time() - started_at) > FLOW_TIMEOUT_SECONDS


def _panel_chat_id(cb: CallbackQuery) -> int | None:
    """cb.message is None if Telegram can no longer resolve the original
    message (older than 48h, or deleted) — every panel callback needs a
    chat_id to check admin status against, so this is checked first."""
    return cb.message.chat.id if cb.message else None


async def _reject_non_admin_callback(cb: CallbackQuery, bot: Bot, chat_id: int) -> bool:
    """The /stopwords panel message is visible to the WHOLE group, not just
    the admin who ran the command — any member can tap its buttons. Unlike a
    private-chat FSM (only the conversation's own user can ever reply into
    it), every callback here must re-verify the PRESSER's live Telegram
    admin/creator status before acting. Returns True (and already answered
    the callback with an alert) if the press was rejected."""
    if await _is_group_admin(bot, chat_id, cb.from_user.id):
        return False
    await cb.answer("⛔ Команда доступна только администраторам группы.", show_alert=True)
    return True


@router.message(Command("addstopword"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_addstopword(msg: Message, bot: Bot, config: ModeratorConfig):
    if not await _is_group_admin(bot, msg.chat.id, msg.from_user.id):
        await msg.answer("⛔ Команда доступна только администраторам группы."); return
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await msg.answer("Использование: /addstopword слово"); return
    word = parts[1].strip().lower()
    # Review-found: an upper bound alone still let a 1-char stopword (e.g. a
    # typo'd "/addstopword a") through — since matching is substring-based
    # (see moderate_message above), that would flag nearly every message in
    # the chat. A lower bound catches the fat-finger case without needing a
    # real word-boundary matcher.
    if len(word) < 2 or len(word) > 100:
        await msg.answer("⚠️ Слово должно быть от 2 до 100 символов."); return
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute("INSERT OR IGNORE INTO stopwords (chat_id, word) VALUES (?,?)", (msg.chat.id, word))
        await db.commit()
    await msg.answer(f"✅ Добавлено в список запрещённых слов: «{_esc(word)}»", parse_mode="HTML")

@router.message(Command("removestopword"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_removestopword(msg: Message, bot: Bot, config: ModeratorConfig):
    if not await _is_group_admin(bot, msg.chat.id, msg.from_user.id):
        await msg.answer("⛔ Команда доступна только администраторам группы."); return
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await msg.answer("Использование: /removestopword слово"); return
    word = parts[1].strip().lower()
    async with aiosqlite.connect(config.db_path) as db:
        cur = await db.execute("DELETE FROM stopwords WHERE chat_id=? AND word=?", (msg.chat.id, word))
        await db.commit()
    if cur.rowcount == 0:
        await msg.answer(f"«{_esc(word)}» не найдено в списке запрещённых слов.", parse_mode="HTML"); return
    await msg.answer(f"✅ Убрано из списка запрещённых слов: «{_esc(word)}»", parse_mode="HTML")

@router.message(Command("stopwords"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_stopwords(msg: Message, bot: Bot, config: ModeratorConfig):
    if not await _is_group_admin(bot, msg.chat.id, msg.from_user.id):
        await msg.answer("⛔ Команда доступна только администраторам группы."); return
    words = await _stopwords_for_chat(config.db_path, msg.chat.id)
    await msg.answer(_stopwords_panel_text(words), parse_mode="HTML", reply_markup=kb_stopwords_panel())

@router.message(Command("setmaxwarnings"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_setmaxwarnings(msg: Message, bot: Bot, config: ModeratorConfig):
    if not await _is_group_admin(bot, msg.chat.id, msg.from_user.id):
        await msg.answer("⛔ Команда доступна только администраторам группы."); return
    parts = msg.text.split()
    # Review-found: isdigit() doesn't bound length — a message with a
    # thousands-of-digits numeral passed isdigit() fine, then int() on it hit
    # CPython 3.11+'s integer-string conversion length guard and raised
    # ValueError unhandled, crashing this handler with no reply. The length
    # check runs BEFORE int() specifically to keep the conversion itself safe.
    if len(parts) < 2 or not parts[1].isdigit() or len(parts[1]) > 3 or not (1 <= int(parts[1]) <= 100):
        await msg.answer("Использование: /setmaxwarnings N (целое число от 1 до 100)"); return
    n = int(parts[1])
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute("INSERT OR IGNORE INTO chat_settings (chat_id) VALUES (?)", (msg.chat.id,))
        await db.execute("UPDATE chat_settings SET max_warnings=? WHERE chat_id=?", (n, msg.chat.id))
        await db.commit()
    await msg.answer(f"✅ Порог предупреждений установлен: {n}.")


# ── group settings panel: button-driven FSM (add/remove word, set threshold) ─

@router.callback_query(F.data == "mod_addword")
async def cb_addword_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer("⚠️ Сообщение недоступно — откройте панель заново: /stopwords", show_alert=True); return
    if await _reject_non_admin_callback(cb, bot, chat_id):
        return
    await cb.answer()
    await cb.message.edit_text("Пришлите слово, которое нужно заблокировать:", reply_markup=kb_cancel())
    await state.update_data(started_at=time.time())
    await state.set_state(ModPanelFlow.add_word)

@router.message(ModPanelFlow.add_word, F.chat.type.in_({"group", "supergroup"}), F.text, ~F.text.startswith("/"))
async def modpanel_add_word(msg: Message, bot: Bot, state: FSMContext, config: ModeratorConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("⏳ Время ожидания истекло — начните заново: /stopwords"); return
    # Review-found: admin status can change between the button press and this
    # reply (rights revoked mid-flow) — re-verify rather than trusting the
    # check already done when the flow started.
    if not await _is_group_admin(bot, msg.chat.id, msg.from_user.id):
        await state.clear()
        await msg.answer("⛔ Действие отменено — вы больше не администратор группы."); return
    word = msg.text.strip().lower()
    # Same bounds as cmd_addstopword, plus a no-whitespace check — this flow's
    # prompt explicitly asks for ONE word, unlike the raw command which simply
    # takes everything after the command as a single (space-permitting) arg.
    if " " in word or len(word) < 2 or len(word) > 100:
        await msg.answer("⚠️ Пришлите одно слово (без пробелов), от 2 до 100 символов."); return
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute("INSERT OR IGNORE INTO stopwords (chat_id, word) VALUES (?,?)", (msg.chat.id, word))
        await db.commit()
    await state.clear()
    await msg.answer(f"✅ Добавлено в список запрещённых слов: «{_esc(word)}»", parse_mode="HTML")
    words = await _stopwords_for_chat(config.db_path, msg.chat.id)
    await msg.answer(_stopwords_panel_text(words), parse_mode="HTML", reply_markup=kb_stopwords_panel())


@router.callback_query(F.data == "mod_removeword")
async def cb_removeword_start(cb: CallbackQuery, bot: Bot, state: FSMContext, config: ModeratorConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer("⚠️ Сообщение недоступно — откройте панель заново: /stopwords", show_alert=True); return
    if await _reject_non_admin_callback(cb, bot, chat_id):
        return
    await cb.answer()
    words = await _stopwords_for_chat(config.db_path, chat_id)
    if not words:
        await cb.message.edit_text("Список запрещённых слов пуст — убирать нечего.")
        return
    if len(words) <= MAX_REMOVE_BUTTONS:
        await state.update_data(remove_words=words, started_at=time.time())
        await cb.message.edit_text("Выберите слово для удаления:", reply_markup=kb_remove_words(words))
        await state.set_state(ModPanelFlow.remove_word_pick)
    else:
        await state.update_data(started_at=time.time())
        await cb.message.edit_text("Слов много — пришлите слово для удаления текстом:", reply_markup=kb_cancel())
        await state.set_state(ModPanelFlow.remove_word_text)

@router.callback_query(ModPanelFlow.remove_word_pick, F.data.startswith("mod_rmw:"))
async def cb_removeword_pick(cb: CallbackQuery, bot: Bot, state: FSMContext, config: ModeratorConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer("⚠️ Сообщение недоступно — откройте панель заново: /stopwords", show_alert=True); return
    if await _reject_non_admin_callback(cb, bot, chat_id):
        return
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("⏳ Время ожидания истекло — откройте панель заново: /stopwords")
        return
    words = data.get("remove_words", [])
    # Review-found: a malformed/stale "mod_rmw:<non-numeric>" callback_data
    # used to raise an unhandled ValueError here — treat it the same as an
    # out-of-range index (stale snapshot) instead of crashing the handler.
    try:
        idx = int(cb.data.split(":", 1)[1])
    except ValueError:
        idx = -1
    if idx < 0 or idx >= len(words):
        await state.clear()
        await cb.message.edit_text("Список устарел — откройте заново командой /stopwords.")
        return
    word = words[idx]
    async with aiosqlite.connect(config.db_path) as db:
        cur = await db.execute("DELETE FROM stopwords WHERE chat_id=? AND word=?", (chat_id, word))
        await db.commit()
    await state.clear()
    # Review-found: unlike the two other removal paths (raw command, text
    # fallback), this used to report "✅ Убрано" unconditionally even if the
    # word had already been removed (e.g. by another admin) between the
    # snapshot being taken and this press — a false-success message.
    if cur.rowcount == 0:
        await cb.message.edit_text(f"«{_esc(word)}» уже не в списке запрещённых слов.", parse_mode="HTML")
        return
    await cb.message.edit_text(f"✅ Убрано из списка запрещённых слов: «{_esc(word)}»", parse_mode="HTML")
    remaining = await _stopwords_for_chat(config.db_path, chat_id)
    await cb.message.answer(_stopwords_panel_text(remaining), parse_mode="HTML", reply_markup=kb_stopwords_panel())

@router.message(ModPanelFlow.remove_word_pick, F.chat.type.in_({"group", "supergroup"}), F.text, ~F.text.startswith("/"))
async def modpanel_remove_word_pick_stray_text(msg: Message) -> None:
    # Review-found gap: this state previously had NO message handler at all —
    # an admin who types instead of tapping one of the pick-buttons got
    # silently ignored with zero reply. Typed text is intentionally NOT
    # accepted as a word here (unlike remove_word_text below) — the buttons
    # are the only valid input in THIS state, since removal-by-button relies
    # on the exact list snapshot taken when the panel was shown.
    await msg.answer("Пожалуйста, выберите слово кнопкой выше, либо отправьте /cancel.")

@router.message(ModPanelFlow.remove_word_text, F.chat.type.in_({"group", "supergroup"}), F.text, ~F.text.startswith("/"))
async def modpanel_remove_word_text(msg: Message, bot: Bot, state: FSMContext, config: ModeratorConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("⏳ Время ожидания истекло — начните заново: /stopwords"); return
    if not await _is_group_admin(bot, msg.chat.id, msg.from_user.id):
        await state.clear()
        await msg.answer("⛔ Действие отменено — вы больше не администратор группы."); return
    word = msg.text.strip().lower()
    async with aiosqlite.connect(config.db_path) as db:
        cur = await db.execute("DELETE FROM stopwords WHERE chat_id=? AND word=?", (msg.chat.id, word))
        await db.commit()
    await state.clear()
    if cur.rowcount == 0:
        await msg.answer(f"«{_esc(word)}» не найдено в списке запрещённых слов.", parse_mode="HTML"); return
    await msg.answer(f"✅ Убрано из списка запрещённых слов: «{_esc(word)}»", parse_mode="HTML")
    words = await _stopwords_for_chat(config.db_path, msg.chat.id)
    await msg.answer(_stopwords_panel_text(words), parse_mode="HTML", reply_markup=kb_stopwords_panel())


@router.callback_query(F.data == "mod_setwarn")
async def cb_setwarn_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer("⚠️ Сообщение недоступно — откройте панель заново: /stopwords", show_alert=True); return
    if await _reject_non_admin_callback(cb, bot, chat_id):
        return
    await cb.answer()
    await cb.message.edit_text(
        "Пришлите число — новый порог предупреждений до мута (целое от 1 до 100):",
        reply_markup=kb_cancel(),
    )
    await state.update_data(started_at=time.time())
    await state.set_state(ModPanelFlow.max_warnings)

@router.message(ModPanelFlow.max_warnings, F.chat.type.in_({"group", "supergroup"}), F.text, ~F.text.startswith("/"))
async def modpanel_max_warnings(msg: Message, bot: Bot, state: FSMContext, config: ModeratorConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("⏳ Время ожидания истекло — начните заново: /stopwords"); return
    if not await _is_group_admin(bot, msg.chat.id, msg.from_user.id):
        await state.clear()
        await msg.answer("⛔ Действие отменено — вы больше не администратор группы."); return
    text = msg.text.strip()
    # Same length-before-int() guard as cmd_setmaxwarnings (see its comment).
    if not text.isdigit() or len(text) > 3 or not (1 <= int(text) <= 100):
        await msg.answer("Введите целое число от 1 до 100."); return
    n = int(text)
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute("INSERT OR IGNORE INTO chat_settings (chat_id) VALUES (?)", (msg.chat.id,))
        await db.execute("UPDATE chat_settings SET max_warnings=? WHERE chat_id=?", (n, msg.chat.id))
        await db.commit()
    await state.clear()
    await msg.answer(f"✅ Порог предупреждений установлен: {n}.")


@router.callback_query(F.data == "mod_cancel")
async def cb_mod_cancel(cb: CallbackQuery, bot: Bot, state: FSMContext):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    # Review-found: this was the one panel callback that skipped the
    # admin re-check — any group member could tap "❌ Отмена" and rewrite the
    # shared panel message to "Отменено.", while the REAL admin's own FSM
    # state (keyed by their own user_id, not the presser's) kept silently
    # waiting for a reply that would never come — a misleading no-op for
    # everyone involved.
    if await _reject_non_admin_callback(cb, bot, chat_id):
        return
    await cb.answer()
    await state.clear()
    await cb.message.edit_text("Отменено.")

@router.message(Command("cancel"), F.chat.type.in_({"group", "supergroup"}), StateFilter("*"))
async def cmd_mod_cancel(msg: Message, state: FSMContext):
    if await state.get_state() is None:
        await msg.answer("Нечего отменять."); return
    await state.clear()
    await msg.answer("Отменено.")


# ── private: /start, /modlog, admins_file management ──────────────────────────

def kb_start_menu(is_admin: bool = False) -> InlineKeyboardMarkup:
    # Security fix: "👥 Админы" and "📜 Журнал модерации" used to be shown to
    # EVERY private-chat user unconditionally — the handlers behind them
    # (cb_mod_admins/cb_mod_modlog) already re-check _is_bot_admin, but the
    # buttons themselves were visible and tappable by non-admins first. Only
    # group-scoped moderation is unaffected by this — that stays gated by
    # live Telegram admin status (_is_group_admin), never admins_file/
    # bot_admins.
    rows = [[InlineKeyboardButton(text="⚙️ Настроить группу", callback_data="mod_pick_group")]]
    if is_admin:
        rows.append([InlineKeyboardButton(text="👥 Админы", callback_data="mod_admins")])
        rows.append([InlineKeyboardButton(text="📜 Журнал модерации", callback_data="mod_modlog")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

# "⚙️ Настроить группу" lists known_groups (populated by
# on_bot_membership_changed) and, once a SPECIFIC group is picked and the
# presser's own live admin status in THAT group is verified, shows the
# per-group panel (rights status + stopwords + threshold) — see
# docs/STAGE2_DESIGN.md "Настройка из личного чата с выбором группы". This
# replaced the old "🔍 Проверить права"/"🚫 Запрещённые слова" buttons, which
# used to just redirect the user back to the group with instructions to run
# /checkrights or /stopwords there — that redirect is gone; the private
# per-group panel now does the actual work.
# "👥 Админы" / "📜 Журнал модерации" need no group context (admins_file/
# moderation_log are global to this bot), so their buttons execute directly.


async def _replace_panel(
    bot: Bot, state: FSMContext, chat_id: int, text: str,
    reply_markup: InlineKeyboardMarkup | None = None, parse_mode: str | None = None,
) -> bool:
    """Private-chat panel navigation: delete the previously shown panel
    message (if any) and send a new one, tracking its id for next time — same
    delete-old/show-new mechanic as templates/tour_operator.py's cb_section
    (there: `section_msg_id`; here: `panel_msg_id`). Reserved for NAVIGABLE
    panel screens only (the rights/stopwords redirects, the admins panel and
    its add/remove prompts, the modlog panel) — NEVER for one-off messages
    like the /start welcome or an action confirmation ("✅ Добавлено ..."),
    which must stay visible in the chat history instead of vanishing on the
    next navigation.

    Callers that are ENDING a flow (success, cancel, or a stale/expired
    state) must clear flow-only state data BEFORE calling this — but through
    `_clear_flow_keep_panel`, not a bare `state.clear()`, since a bare clear()
    would also wipe `panel_msg_id` and make the delete-old step below a no-op
    (review-found: every flow-completion path originally did `state.clear()`
    THEN called this function, so `prev_id` below was always None and the
    prompt/pick-list message from the step just finished was never deleted).

    Returns True iff the new panel message was actually sent. Callers that
    are STARTING a flow off the back of this call (setting an FSM state that
    expects a reply to a prompt this function was supposed to show) must
    check this — review-found: previously always returned None either way,
    so cb_addadmin_start/cb_removeadmin_start etc. would set FSM state to
    expect a reply even when the prompt itself silently failed to send,
    leaving the user's next ordinary message misinterpreted as a flow answer
    with no prompt ever shown."""
    lock = _panel_locks.setdefault(chat_id, asyncio.Lock())
    async with lock:
        data = await state.get_data()
        prev_id = data.get("panel_msg_id")
        if prev_id:
            try:
                await bot.delete_message(chat_id, prev_id)
            except Exception as e:
                # Expected in the common case (already 48h+ old, or the user
                # deleted it themselves) — debug level, but logged instead of a
                # bare pass so a genuinely unexpected failure isn't invisible.
                logger.debug(f"_replace_panel: failed to delete old panel {prev_id} in chat {chat_id}: {e}")
        # Review-found: unlike every other Telegram call in this file, this send
        # had no try/except — an unbounded admin ID (see _admins_list_text) or a
        # blocked/deactivated user would raise here unhandled, on EVERY future
        # panel render, for every admin, until fixed by hand.
        try:
            msg = await bot.send_message(chat_id, text, parse_mode=parse_mode, reply_markup=reply_markup)
        except Exception as e:
            logger.warning(f"_replace_panel: failed to send panel to chat {chat_id}: {e}")
            return False
        await state.update_data(panel_msg_id=msg.message_id)
        return True


async def _clear_flow_keep_panel(state: FSMContext) -> None:
    """Clears FSM flow state/data (the active AdminPanelFlow step, any
    flow-only keys like remove_admin_ids/started_at) while preserving
    panel_msg_id, so a following _replace_panel() call still knows which
    earlier panel message to delete. See _replace_panel's docstring."""
    data = await state.get_data()
    panel_msg_id = data.get("panel_msg_id")
    await state.clear()
    if panel_msg_id is not None:
        await state.update_data(panel_msg_id=panel_msg_id)


async def _is_bot_admin(user_id: int, config: ModeratorConfig) -> bool:
    return await _is_bot_admin_db(config.db_path, user_id)


# AdminPanelFlow reuses FLOW_TIMEOUT_SECONDS/_flow_expired (defined above,
# next to ModPanelFlow) for the same reason: without it, a private-chat admin
# who taps "➕ Добавить админа" and then navigates elsewhere (or just goes
# quiet) has their next ordinary numeric-looking message (a phone number, a
# date, anyone else's ID mentioned in conversation) silently accepted as a
# NEW bot-admin.


def kb_admins_panel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить админа", callback_data="mod_addadmin")],
        [InlineKeyboardButton(text="➖ Убрать админа", callback_data="mod_removeadmin")],
    ])

def kb_private_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Отмена", callback_data="mod_admin_cancel")
    ]])

# Same rationale as MAX_REMOVE_BUTTONS on the group stopwords panel — above
# this, removal falls back to a "type the ID" flow instead of one button per admin.
MAX_ADMIN_REMOVE_BUTTONS = 30

def kb_remove_admins(ids: list[str]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=admin_id, callback_data=f"mod_rma:{i}")] for i, admin_id in enumerate(ids)]
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="mod_admin_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


class AdminPanelFlow(StatesGroup):
    add_admin = State(); remove_admin_pick = State(); remove_admin_text = State()


def _valid_admin_id(text: str) -> bool:
    """Same bound-length-before-treating-as-numeric guard as
    cmd_setmaxwarnings elsewhere in this file — an unbounded "digit" string
    still passes a bare isdigit() check, then blows past Telegram's
    message-length limit on every future _admins_list_text render (see its
    _join_bounded fix below) until fixed by hand. isascii() additionally
    rejects Unicode look-alike digits (e.g. fullwidth "１２３"), which pass
    str.isdigit() but can never match a REAL Telegram user id (always plain
    ASCII) — silently creating a permanently-unmatchable phantom admin.

    Review-found: this used to lstrip("-") before validating, so "-5" /
    "-999999999999" passed as a "valid" admin id even though real
    from_user.id values are always positive — same phantom-admin trap as
    the fullwidth-digit case above, except reachable through completely
    ordinary input, and capable of silently burning the "last admin" slot
    (len(ids) grows, but the new entry can never match _is_bot_admin).

    Review-found: same trap again for "0" and leading-zero strings like
    "007" — both pass isdigit(), but no real Telegram user_id is ever 0
    (Telegram ids start at 1) or ever rendered with a leading zero
    (str(user_id) never produces one), so either one is a phantom entry
    that inflates len(ids) past the last-admin guard's count check without
    ever being able to match _is_bot_admin. Concretely: the sole admin adds
    "0", the guard now sees 2 admins and lets them remove themselves, and
    the bot is left permanently admin-less through the normal UI. Requiring
    str(int(text)) == text rejects "0" (int 0, not > 0) and every
    leading-zero form (int(text) round-trips to a shorter string) in one
    check, without needing a separate startswith("0") special case."""
    if not (bool(text) and text.isascii() and text.isdigit() and len(text) <= 15):
        return False
    return int(text) > 0 and str(int(text)) == text


async def _admins_list_text(config: ModeratorConfig) -> str:
    # Review-found: iterated the raw set (Python string-hash order, varies
    # between process restarts) while cb_removeadmin_start's picker builds
    # its numbered buttons from sorted(_load_admins(...)) — the displayed
    # list and the button order could silently disagree. Sort both the same
    # way so "admin #2 in the list" always means the same id as "button #2".
    ids = sorted(await _list_bot_admins(config.db_path))
    if not ids:
        return "👥 Пусто"
    return _join_bounded(["👥 <b>Администраторы бота:</b>\n"] + [f"• <code>{_esc(i)}</code>" for i in ids])


async def _modlog_text(config: ModeratorConfig) -> str:
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT chat_id, user_id, action, reason, created_at FROM moderation_log ORDER BY id DESC LIMIT 30"
        )).fetchall()
    if not rows:
        return "Журнал модерации пуст."
    icon = {"warn": "⚠️", "mute": "🔇", "ban": "🚫"}
    lines = ["📋 <b>Журнал модерации (последние 30):</b>\n"]
    for r in rows:
        lines.append(
            f"{r['created_at']} {icon.get(r['action'], '•')} чат {r['chat_id']} · "
            f"пользователь {r['user_id']} · {_esc(r['reason']) if r['reason'] else '—'}"
        )
    return _join_bounded(lines)


@router.message(Command("start"), F.chat.type == "private")
async def cmd_start(message: Message, state: FSMContext, config: ModeratorConfig):
    # Same reasoning as inventory.py's cmd_start: /start must reset any
    # dangling mid-flow FSM state (e.g. an abandoned "add admin" prompt) —
    # otherwise the user's very next plain-text message gets silently
    # captured as an admin ID for a flow they believed they'd left.
    await state.clear()
    sender_id = message.from_user.id
    # Security fix: _claim_first_bot_admin was already race-free (BEGIN
    # IMMEDIATE), but "whoever /start-s the empty bot first wins" was still
    # the rule — a client DMing the bot before its owner does could
    # permanently seize the bot-admin role (journal access, /addadmin,
    # /removeadmin). When bots.owner_telegram_id is known, only that user's
    # /start is allowed to attempt the atomic claim; in standalone/env mode
    # (owner_telegram_id unknown) the old first-comer behavior is kept as
    # the only option available.
    is_owner = config.owner_telegram_id is not None and sender_id == config.owner_telegram_id
    first_time_admin = False
    if is_owner or config.owner_telegram_id is None:
        first_time_admin = await _claim_first_bot_admin(config.db_path, str(sender_id))
    is_admin_now = await _is_bot_admin(sender_id, config)
    if config.welcome_image.exists():
        await message.answer_photo(
            FSInputFile(str(config.welcome_image)), caption=WELCOME_TEXT, parse_mode="HTML",
            reply_markup=kb_start_menu(is_admin_now),
        )
    else:
        await message.answer(WELCOME_TEXT, parse_mode="HTML", reply_markup=kb_start_menu(is_admin_now))
    if first_time_admin:
        await message.answer(
            "👑 <b>Вы — администратор этого бота.</b>\n\n"
            "Управление администраторами бота (доступ к журналу модерации) — "
            "кнопка «👥 Админы» выше.",
            parse_mode="HTML",
        )

# ── private: group picker + per-group config panel ────────────────────────────
# "⚙️ Настроить группу" — see docs/STAGE2_DESIGN.md "Настройка из личного чата
# с выбором группы". known_groups (populated by on_bot_membership_changed)
# lists the groups the bot currently belongs to; picking one stores its
# chat_id in FSM state (`selected_group`) for the duration of that group's
# config session. THE security property this whole flow exists for: chat_id
# only ever enters from callback_data at the PICK step (mod_group:<id>), and
# is verified there via a LIVE get_chat_member(chat_id, presser's user_id)
# before being trusted/stored — never admins_file, never "the bot happens to
# know this group". Every downstream sub-action (add/remove word, set
# threshold, recheck rights) re-reads chat_id from FSM state AND re-verifies
# the presser's live admin status in THAT chat_id again, independently — not
# relying on the entry check alone, since a stale/forged FSM state (rights
# revoked mid-session, or state tampered with by any other means) must be
# rejected at every single step, not just once at the door.

def kb_group_list(groups: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    # Review-found: unlike kb_remove_words/kb_remove_admins elsewhere in this
    # file, this had no cap — a bot moderating enough groups to exceed
    # Telegram's inline-keyboard limits would have its send_message fail
    # AFTER _replace_panel already deleted the previous screen, i.e. the
    # user presses "⚙️ Настроить группу" and sees nothing at all. Same
    # MAX_REMOVE_BUTTONS cap as those two (groups are pre-sorted
    # alphabetically by _known_groups' own SQL ORDER BY).
    rows = [
        [InlineKeyboardButton(text=title, callback_data=f"mod_group:{chat_id}")]
        for chat_id, title in groups[:MAX_REMOVE_BUTTONS]
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _group_list_text(groups: list[tuple[int, str]]) -> str:
    if len(groups) > MAX_REMOVE_BUTTONS:
        return (
            f"Выберите группу для настройки (показаны первые {MAX_REMOVE_BUTTONS} из {len(groups)}):"
        )
    return "Выберите группу для настройки:"

def kb_group_panel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить запрещённое слово", callback_data="modp_addword")],
        [InlineKeyboardButton(text="➖ Убрать запрещённое слово", callback_data="modp_removeword")],
        [InlineKeyboardButton(text="⚙️ Порог предупреждений", callback_data="modp_setwarn")],
        [InlineKeyboardButton(text="🔄 Перепроверить права", callback_data="modp_recheck")],
        [InlineKeyboardButton(text="⬅️ К списку групп", callback_data="mod_pick_group")],
    ])

def kb_private_group_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Отмена", callback_data="modp_cancel")
    ]])

def kb_private_remove_words(words: list[str]) -> InlineKeyboardMarkup:
    rows = []
    for i, w in enumerate(words):
        label = w if len(w) <= 40 else w[:37] + "…"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"modp_rmw:{i}")])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="modp_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _group_panel_text(bot: Bot, config: ModeratorConfig, chat_id: int) -> str:
    """Rights status (read-only summary, via the pure _rights_status_text —
    unlike _check_and_report_rights, this never posts INTO the group) +
    current stopwords list, combined into one private-chat panel screen."""
    try:
        bot_member = await bot.get_chat_member(chat_id, bot.id)
        _, rights_text = _rights_status_text(bot_member)
    except Exception as e:
        logger.warning(f"_group_panel_text: get_chat_member for bot failed in chat {chat_id}: {e}")
        rights_text = "⚠️ Не удалось проверить права бота — попробуйте «🔄 Перепроверить права»."
    words = await _stopwords_for_chat(config.db_path, chat_id)
    return rights_text + "\n\n" + _stopwords_panel_text(words)


async def _clear_flow_keep_panel_and_group(state: FSMContext) -> None:
    """Like _clear_flow_keep_panel, but ALSO preserves selected_group — used
    when a sub-flow below (add/remove word, set threshold) finishes and
    returns to the SAME group's panel. Navigating away to a genuinely
    different top-level destination (Admins/Modlog/back to the group list)
    must drop the selection instead, via the plain _clear_flow_keep_panel."""
    data = await state.get_data()
    panel_msg_id = data.get("panel_msg_id")
    selected_group = data.get("selected_group")
    await state.clear()
    keep: dict = {}
    if panel_msg_id is not None:
        keep["panel_msg_id"] = panel_msg_id
    if selected_group is not None:
        keep["selected_group"] = selected_group
    if keep:
        await state.update_data(**keep)


class PrivateModPanelFlow(StatesGroup):
    add_word = State(); remove_word_pick = State(); remove_word_text = State(); max_warnings = State()

_PRIVATE_GROUP_FLOW_STATES = {
    str(PrivateModPanelFlow.add_word), str(PrivateModPanelFlow.remove_word_pick),
    str(PrivateModPanelFlow.remove_word_text), str(PrivateModPanelFlow.max_warnings),
}


@router.callback_query(F.data == "mod_pick_group")
async def cb_pick_group(cb: CallbackQuery, bot: Bot, state: FSMContext, config: ModeratorConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    # Leaving to a different top-level destination (including away from a
    # previously selected group's own panel) — drop selected_group along
    # with any dangling flow, same reasoning as navigating to Admins/Modlog.
    await _clear_flow_keep_panel(state)
    groups = await _known_groups(config.db_path)
    if not groups:
        await _replace_panel(
            bot, state, chat_id,
            "Бот пока не состоит ни в одной группе. Добавьте его в группу и "
            "выдайте права администратора — тогда группа появится здесь.",
        )
        return
    await _replace_panel(bot, state, chat_id, _group_list_text(groups), reply_markup=kb_group_list(groups))


@router.callback_query(F.data.startswith("mod_group:"))
async def cb_select_group(cb: CallbackQuery, bot: Bot, state: FSMContext, config: ModeratorConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    try:
        target = int(cb.data.split(":", 1)[1])
    except ValueError:
        await cb.answer("Некорректный выбор — откройте список заново.", show_alert=True)
        return
    # Live check of the PRESSER's own admin status in THIS chat_id — not
    # admins_file, not "the bot is in this group". See section docstring.
    if not await _is_group_admin(bot, target, cb.from_user.id):
        logger.warning(f"cb_select_group: user {cb.from_user.id} denied access to chat {target} (not a live admin there)")
        await cb.answer("⛔ Вы не администратор этой группы.", show_alert=True)
        return
    # Review-found: switching to a DIFFERENT group while a sub-flow
    # (add/remove word, set threshold) was still open for the PREVIOUS
    # selected_group used to leave that flow's state (and its own
    # selected_group) dangling — a message that arrives right after the
    # switch (e.g. a slow client, or the user retyping) would still be
    # captured by that old flow's continuation handler, which reads
    # chat_id from FSM state and would now silently see the NEW group
    # instead of the one the word was actually meant for. Clearing here,
    # same as cb_pick_group already does when leaving to the group list.
    await _clear_flow_keep_panel(state)
    await state.update_data(selected_group=target)
    text = await _group_panel_text(bot, config, target)
    await _replace_panel(bot, state, chat_id, text, reply_markup=kb_group_panel(), parse_mode="HTML")


@router.callback_query(F.data == "modp_recheck")
async def cb_private_recheck_rights(cb: CallbackQuery, bot: Bot, state: FSMContext, config: ModeratorConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    data = await state.get_data()
    target = data.get("selected_group")
    if target is None:
        await _replace_panel(bot, state, chat_id, "Сначала выберите группу.", reply_markup=kb_group_list(await _known_groups(config.db_path)))
        return
    if not await _is_group_admin(bot, target, cb.from_user.id):
        logger.warning(f"cb_private_recheck_rights: user {cb.from_user.id} denied access to chat {target}")
        await cb.answer("⛔ Вы не администратор этой группы.", show_alert=True)
        return
    text = await _group_panel_text(bot, config, target)
    await _replace_panel(bot, state, chat_id, text, reply_markup=kb_group_panel(), parse_mode="HTML")


@router.callback_query(F.data == "modp_addword")
async def cb_private_addword_start(cb: CallbackQuery, bot: Bot, state: FSMContext, config: ModeratorConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    data = await state.get_data()
    target = data.get("selected_group")
    if target is None or not await _is_group_admin(bot, target, cb.from_user.id):
        logger.warning(f"cb_private_addword_start: user {cb.from_user.id} denied access to chat {target}")
        await cb.answer("⛔ Нет доступа — выберите группу заново.", show_alert=True)
        return
    await _replace_panel(bot, state, chat_id, "Пришлите слово, которое нужно заблокировать:", reply_markup=kb_private_group_cancel())
    await state.update_data(started_at=time.time())
    await state.set_state(PrivateModPanelFlow.add_word)

@router.message(PrivateModPanelFlow.add_word, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def privatepanel_add_word(msg: Message, bot: Bot, state: FSMContext, config: ModeratorConfig):
    data = await state.get_data()
    target = data.get("selected_group")
    if _flow_expired(data) or target is None:
        await _clear_flow_keep_panel(state)
        await msg.answer("⏳ Время ожидания истекло — начните заново кнопкой «⚙️ Настроить группу»."); return
    if not await _is_group_admin(bot, target, msg.from_user.id):
        logger.warning(f"privatepanel_add_word: user {msg.from_user.id} denied access to chat {target}")
        await _clear_flow_keep_panel(state)
        await msg.answer("⛔ Действие отменено — вы больше не администратор этой группы."); return
    word = msg.text.strip().lower()
    if " " in word or len(word) < 2 or len(word) > 100:
        await msg.answer("⚠️ Пришлите одно слово (без пробелов), от 2 до 100 символов."); return
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute("INSERT OR IGNORE INTO stopwords (chat_id, word) VALUES (?,?)", (target, word))
        await db.commit()
    await _clear_flow_keep_panel_and_group(state)
    await msg.answer(f"✅ Добавлено в список запрещённых слов: «{_esc(word)}»", parse_mode="HTML")
    text = await _group_panel_text(bot, config, target)
    await _replace_panel(bot, state, msg.chat.id, text, reply_markup=kb_group_panel(), parse_mode="HTML")


@router.callback_query(F.data == "modp_removeword")
async def cb_private_removeword_start(cb: CallbackQuery, bot: Bot, state: FSMContext, config: ModeratorConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    data = await state.get_data()
    target = data.get("selected_group")
    if target is None or not await _is_group_admin(bot, target, cb.from_user.id):
        logger.warning(f"cb_private_removeword_start: user {cb.from_user.id} denied access to chat {target}")
        await cb.answer("⛔ Нет доступа — выберите группу заново.", show_alert=True)
        return
    words = await _stopwords_for_chat(config.db_path, target)
    if not words:
        await _replace_panel(bot, state, chat_id, "Список запрещённых слов пуст — убирать нечего.", reply_markup=kb_group_panel())
        return
    if len(words) <= MAX_REMOVE_BUTTONS:
        await state.update_data(remove_words=words, started_at=time.time())
        await _replace_panel(bot, state, chat_id, "Выберите слово для удаления:", reply_markup=kb_private_remove_words(words))
        await state.set_state(PrivateModPanelFlow.remove_word_pick)
    else:
        await state.update_data(started_at=time.time())
        await _replace_panel(bot, state, chat_id, "Слов много — пришлите слово для удаления текстом:", reply_markup=kb_private_group_cancel())
        await state.set_state(PrivateModPanelFlow.remove_word_text)

@router.callback_query(PrivateModPanelFlow.remove_word_pick, F.data.startswith("modp_rmw:"))
async def cb_private_removeword_pick(cb: CallbackQuery, bot: Bot, state: FSMContext, config: ModeratorConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    data = await state.get_data()
    target = data.get("selected_group")
    if _flow_expired(data) or target is None:
        await _clear_flow_keep_panel(state)
        await _replace_panel(bot, state, chat_id, "⏳ Время ожидания истекло — откройте заново кнопкой «⚙️ Настроить группу».")
        return
    if not await _is_group_admin(bot, target, cb.from_user.id):
        logger.warning(f"cb_private_removeword_pick: user {cb.from_user.id} denied access to chat {target}")
        await _clear_flow_keep_panel(state)
        await cb.message.answer("⛔ Вы больше не администратор этой группы.")
        return
    words = data.get("remove_words", [])
    try:
        idx = int(cb.data.split(":", 1)[1])
    except ValueError:
        idx = -1
    if idx < 0 or idx >= len(words):
        await _clear_flow_keep_panel_and_group(state)
        text = await _group_panel_text(bot, config, target)
        await _replace_panel(bot, state, chat_id, "Список устарел.\n\n" + text, reply_markup=kb_group_panel(), parse_mode="HTML")
        return
    word = words[idx]
    async with aiosqlite.connect(config.db_path) as db:
        cur = await db.execute("DELETE FROM stopwords WHERE chat_id=? AND word=?", (target, word))
        await db.commit()
    await _clear_flow_keep_panel_and_group(state)
    prefix = (
        f"«{_esc(word)}» уже не в списке запрещённых слов.\n\n" if cur.rowcount == 0
        else f"✅ Убрано из списка запрещённых слов: «{_esc(word)}»\n\n"
    )
    text = await _group_panel_text(bot, config, target)
    await _replace_panel(bot, state, chat_id, prefix + text, reply_markup=kb_group_panel(), parse_mode="HTML")

@router.message(PrivateModPanelFlow.remove_word_pick, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def privatepanel_remove_word_pick_stray_text(msg: Message) -> None:
    await msg.answer("Пожалуйста, выберите слово кнопкой выше, либо отправьте /cancel.")

@router.message(PrivateModPanelFlow.remove_word_text, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def privatepanel_remove_word_text(msg: Message, bot: Bot, state: FSMContext, config: ModeratorConfig):
    data = await state.get_data()
    target = data.get("selected_group")
    if _flow_expired(data) or target is None:
        await _clear_flow_keep_panel(state)
        await msg.answer("⏳ Время ожидания истекло — начните заново кнопкой «⚙️ Настроить группу»."); return
    if not await _is_group_admin(bot, target, msg.from_user.id):
        logger.warning(f"privatepanel_remove_word_text: user {msg.from_user.id} denied access to chat {target}")
        await _clear_flow_keep_panel(state)
        await msg.answer("⛔ Действие отменено — вы больше не администратор этой группы."); return
    word = msg.text.strip().lower()
    async with aiosqlite.connect(config.db_path) as db:
        cur = await db.execute("DELETE FROM stopwords WHERE chat_id=? AND word=?", (target, word))
        await db.commit()
    await _clear_flow_keep_panel_and_group(state)
    if cur.rowcount == 0:
        await msg.answer(f"«{_esc(word)}» не найдено в списке запрещённых слов.", parse_mode="HTML"); return
    await msg.answer(f"✅ Убрано из списка запрещённых слов: «{_esc(word)}»", parse_mode="HTML")
    text = await _group_panel_text(bot, config, target)
    await _replace_panel(bot, state, msg.chat.id, text, reply_markup=kb_group_panel(), parse_mode="HTML")


@router.callback_query(F.data == "modp_setwarn")
async def cb_private_setwarn_start(cb: CallbackQuery, bot: Bot, state: FSMContext):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    data = await state.get_data()
    target = data.get("selected_group")
    if target is None or not await _is_group_admin(bot, target, cb.from_user.id):
        logger.warning(f"cb_private_setwarn_start: user {cb.from_user.id} denied access to chat {target}")
        await cb.answer("⛔ Нет доступа — выберите группу заново.", show_alert=True)
        return
    await _replace_panel(
        bot, state, chat_id,
        "Пришлите число — новый порог предупреждений до мута (целое от 1 до 100):",
        reply_markup=kb_private_group_cancel(),
    )
    await state.update_data(started_at=time.time())
    await state.set_state(PrivateModPanelFlow.max_warnings)

@router.message(PrivateModPanelFlow.max_warnings, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def privatepanel_max_warnings(msg: Message, bot: Bot, state: FSMContext, config: ModeratorConfig):
    data = await state.get_data()
    target = data.get("selected_group")
    if _flow_expired(data) or target is None:
        await _clear_flow_keep_panel(state)
        await msg.answer("⏳ Время ожидания истекло — начните заново кнопкой «⚙️ Настроить группу»."); return
    if not await _is_group_admin(bot, target, msg.from_user.id):
        logger.warning(f"privatepanel_max_warnings: user {msg.from_user.id} denied access to chat {target}")
        await _clear_flow_keep_panel(state)
        await msg.answer("⛔ Действие отменено — вы больше не администратор этой группы."); return
    text_in = msg.text.strip()
    if not text_in.isdigit() or len(text_in) > 3 or not (1 <= int(text_in) <= 100):
        await msg.answer("Введите целое число от 1 до 100."); return
    n = int(text_in)
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute("INSERT OR IGNORE INTO chat_settings (chat_id) VALUES (?)", (target,))
        await db.execute("UPDATE chat_settings SET max_warnings=? WHERE chat_id=?", (n, target))
        await db.commit()
    await _clear_flow_keep_panel_and_group(state)
    await msg.answer(f"✅ Порог предупреждений установлен: {n}.")
    text = await _group_panel_text(bot, config, target)
    await _replace_panel(bot, state, msg.chat.id, text, reply_markup=kb_group_panel(), parse_mode="HTML")


@router.callback_query(F.data == "modp_cancel")
async def cb_private_mod_cancel(cb: CallbackQuery, bot: Bot, state: FSMContext, config: ModeratorConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    data = await state.get_data()
    target = data.get("selected_group")
    # Review-found: rendering the group panel here without re-checking admin
    # status leaked that group's rights status + full stopwords list to a
    # user whose admin rights were revoked (or who was kicked) mid-flow — the
    # one screen in this section that skipped the "every sub-action
    # re-verifies live admin status" rule the whole flow is built on.
    still_admin = target is not None and await _is_group_admin(bot, target, cb.from_user.id)
    if still_admin:
        await _clear_flow_keep_panel_and_group(state)
        text = await _group_panel_text(bot, config, target)
        await _replace_panel(bot, state, chat_id, text, reply_markup=kb_group_panel(), parse_mode="HTML")
    else:
        await _clear_flow_keep_panel(state)
        await _replace_panel(bot, state, chat_id, "Отменено.")


@router.callback_query(F.data == "mod_admins")
async def cb_admins(cb: CallbackQuery, bot: Bot, state: FSMContext, config: ModeratorConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not await _is_bot_admin(cb.from_user.id, config):
        await cb.message.answer("⛔ Нет доступа"); return
    await _clear_flow_keep_panel(state)
    await _replace_panel(
        bot, state, chat_id, await _admins_list_text(config),
        reply_markup=kb_admins_panel(), parse_mode="HTML",
    )

@router.callback_query(F.data == "mod_modlog")
async def cb_modlog(cb: CallbackQuery, bot: Bot, state: FSMContext, config: ModeratorConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not await _is_bot_admin(cb.from_user.id, config):
        await cb.message.answer("⛔ Нет доступа"); return
    await _clear_flow_keep_panel(state)
    await _replace_panel(bot, state, chat_id, await _modlog_text(config), parse_mode="HTML")

@router.message(Command("modlog"), F.chat.type == "private")
async def cmd_modlog(msg: Message, config: ModeratorConfig):
    if not await _is_bot_admin(msg.from_user.id, config):
        await msg.answer("⛔ Нет доступа"); return
    await msg.answer(await _modlog_text(config), parse_mode="HTML")

@router.message(Command("addadmin"))
async def cmd_addadmin(msg: Message, config: ModeratorConfig):
    if not await _is_bot_admin(msg.from_user.id, config): await msg.answer("⛔ Нет доступа"); return
    parts = msg.text.split()
    if len(parts) < 2 or not _valid_admin_id(parts[1]): await msg.answer("Использование: /addadmin <id>"); return
    await _add_bot_admin(config.db_path, parts[1])
    logger.info(f"cmd_addadmin: {parts[1]} added as bot admin by {msg.from_user.id}")
    await msg.answer(f"✅ <code>{parts[1]}</code> добавлен.", parse_mode="HTML")

@router.message(Command("removeadmin"))
async def cmd_removeadmin(msg: Message, config: ModeratorConfig):
    if not await _is_bot_admin(msg.from_user.id, config): await msg.answer("⛔ Нет доступа"); return
    parts = msg.text.split()
    if len(parts) < 2: await msg.answer("Использование: /removeadmin <id>"); return
    # Review-found: removing the last remaining bot admin hands the role to
    # whoever happens to message /start next (cmd_start's bootstrap) — refuse
    # rather than silently emptying the admin set. _remove_bot_admin enforces
    # this atomically now, closing a race the old in-memory count check couldn't.
    result = await _remove_bot_admin(config.db_path, parts[1])
    if result == "last_admin":
        await msg.answer("⚠️ Нельзя удалить единственного администратора."); return
    logger.info(f"cmd_removeadmin: {parts[1]} removed as bot admin by {msg.from_user.id} (result={result})")
    # Review-found: unlike cmd_addadmin (validates isdigit() before this point),
    # this echoed the raw argument straight into a parse_mode="HTML" <code>
    # block — arbitrary text containing '<'/'&' would break the HTML the
    # message is sent with (send fails outright, not an XSS risk here since
    # there's no browser rendering it, but still a real correctness bug).
    await msg.answer(f"✅ <code>{_esc(parts[1])}</code> удалён.", parse_mode="HTML")

@router.message(Command("admins"))
async def cmd_admins(msg: Message, config: ModeratorConfig):
    if not await _is_bot_admin(msg.from_user.id, config): await msg.answer("⛔ Нет доступа"); return
    await msg.answer(await _admins_list_text(config), parse_mode="HTML")


# ── admins panel: button-driven FSM (add/remove bot admin) ───────────────────
# Private chat only, unlike the group stopwords panel — no "visible to the
# whole group" concern here (only this one user is in this conversation with
# the bot), so no per-press live-admin re-check is needed beyond the
# admins_file membership check already done when each screen is entered.
# Every continuation handler DOES re-check _flow_expired + admin membership
# though (see FLOW_TIMEOUT_SECONDS/_flow_expired, reused from the group panel)
# — access could still be revoked mid-flow, and the flow itself could go stale.

@router.callback_query(F.data == "mod_addadmin")
async def cb_addadmin_start(cb: CallbackQuery, bot: Bot, state: FSMContext, config: ModeratorConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not await _is_bot_admin(cb.from_user.id, config):
        await cb.message.answer("⛔ Нет доступа"); return
    sent = await _replace_panel(
        bot, state, chat_id,
        "Пришлите Telegram ID пользователя, которого нужно сделать администратором:",
        reply_markup=kb_private_cancel(),
    )
    if not sent:
        # Prompt never reached the user — don't arm a state that expects a
        # reply to a message they never saw (see _replace_panel's docstring).
        await cb.message.answer("⚠️ Не удалось открыть панель, попробуйте ещё раз.")
        return
    await state.update_data(started_at=time.time())
    await state.set_state(AdminPanelFlow.add_admin)

@router.message(AdminPanelFlow.add_admin, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def adminpanel_add_admin(msg: Message, bot: Bot, state: FSMContext, config: ModeratorConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await _clear_flow_keep_panel(state)
        await msg.answer("⏳ Время ожидания истекло — начните заново кнопкой «👥 Админы»."); return
    if not await _is_bot_admin(msg.from_user.id, config):
        await _clear_flow_keep_panel(state)
        await msg.answer("⛔ Нет доступа"); return
    text = msg.text.strip()
    if not _valid_admin_id(text):
        await msg.answer("Пришлите числовой Telegram ID."); return
    await _add_bot_admin(config.db_path, text)
    logger.info(f"adminpanel_add_admin: {text} added as bot admin by {msg.from_user.id}")
    await _clear_flow_keep_panel(state)
    await msg.answer(f"✅ <code>{_esc(text)}</code> добавлен.", parse_mode="HTML")
    await _replace_panel(
        bot, state, msg.chat.id, await _admins_list_text(config),
        reply_markup=kb_admins_panel(), parse_mode="HTML",
    )


@router.callback_query(F.data == "mod_removeadmin")
async def cb_removeadmin_start(cb: CallbackQuery, bot: Bot, state: FSMContext, config: ModeratorConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not await _is_bot_admin(cb.from_user.id, config):
        await cb.message.answer("⛔ Нет доступа"); return
    ids = sorted(await _list_bot_admins(config.db_path))
    if not ids:
        await _replace_panel(bot, state, chat_id, "Список администраторов пуст.")
        return
    # Review-found: same last-admin guard as cmd_removeadmin — block the
    # no-op-but-dangerous case upfront instead of offering a picker that
    # would empty admins_file on the very next tap. Note this is a UX
    # shortcut, not the enforcement point — _remove_bot_admin re-checks
    # atomically at actual removal time (cb_removeadmin_pick/
    # adminpanel_remove_admin_text below), so a race between this check and
    # the tap can't actually empty the set.
    if len(ids) == 1:
        await _replace_panel(
            bot, state, chat_id,
            "⚠️ Вы — единственный администратор бота. Сначала добавьте ещё одного "
            "через «➕ Добавить админа», прежде чем убирать себя.",
            reply_markup=kb_admins_panel(), parse_mode="HTML",
        )
        return
    if len(ids) <= MAX_ADMIN_REMOVE_BUTTONS:
        sent = await _replace_panel(
            bot, state, chat_id, "Выберите администратора для удаления:",
            reply_markup=kb_remove_admins(ids),
        )
        if not sent:
            await cb.message.answer("⚠️ Не удалось открыть панель, попробуйте ещё раз.")
            return
        await state.update_data(remove_admin_ids=ids, started_at=time.time())
        await state.set_state(AdminPanelFlow.remove_admin_pick)
    else:
        sent = await _replace_panel(
            bot, state, chat_id, "Админов много — пришлите ID для удаления текстом:",
            reply_markup=kb_private_cancel(),
        )
        if not sent:
            await cb.message.answer("⚠️ Не удалось открыть панель, попробуйте ещё раз.")
            return
        await state.update_data(started_at=time.time())
        await state.set_state(AdminPanelFlow.remove_admin_text)

@router.callback_query(AdminPanelFlow.remove_admin_pick, F.data.startswith("mod_rma:"))
async def cb_removeadmin_pick(cb: CallbackQuery, bot: Bot, state: FSMContext, config: ModeratorConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not await _is_bot_admin(cb.from_user.id, config):
        await _clear_flow_keep_panel(state)
        await cb.message.answer("⛔ Нет доступа"); return
    data = await state.get_data()
    if _flow_expired(data):
        await _clear_flow_keep_panel(state)
        await _replace_panel(bot, state, chat_id, "⏳ Время ожидания истекло — откройте заново кнопкой «👥 Админы».")
        return
    ids = data.get("remove_admin_ids", [])
    # Same malformed/stale-callback guard as the group panel's cb_removeword_pick.
    try:
        idx = int(cb.data.split(":", 1)[1])
    except ValueError:
        idx = -1
    if idx < 0 or idx >= len(ids):
        await _clear_flow_keep_panel(state)
        await _replace_panel(bot, state, chat_id, "Список устарел — откройте заново кнопкой «👥 Админы».")
        return
    target = ids[idx]
    # Review-found: unconditionally reporting "✅ Убрано" even if the target
    # had ALREADY been removed (stale snapshot vs. a concurrent change) was a
    # false-success message — mirror the group panel's cb_removeword_pick,
    # which checks this honestly instead of assuming the snapshot still holds.
    # _remove_bot_admin does both the not-found and last-admin checks
    # atomically against the current DB state, not the stale `ids` snapshot
    # this picker's buttons were built from.
    result = await _remove_bot_admin(config.db_path, target)
    if result == "not_found":
        await _clear_flow_keep_panel(state)
        await cb.message.answer(f"«{_esc(target)}» уже не администратор.", parse_mode="HTML")
        await _replace_panel(
            bot, state, chat_id, await _admins_list_text(config),
            reply_markup=kb_admins_panel(), parse_mode="HTML",
        )
        return
    if result == "last_admin":
        await _clear_flow_keep_panel(state)
        await _replace_panel(
            bot, state, chat_id, "⚠️ Нельзя удалить единственного администратора.",
            reply_markup=kb_admins_panel(), parse_mode="HTML",
        )
        return
    logger.info(f"cb_removeadmin_pick: {target} removed as bot admin by {cb.from_user.id}")
    await _clear_flow_keep_panel(state)
    await cb.message.answer(f"✅ <code>{_esc(target)}</code> удалён.", parse_mode="HTML")
    await _replace_panel(
        bot, state, chat_id, await _admins_list_text(config),
        reply_markup=kb_admins_panel(), parse_mode="HTML",
    )

@router.message(AdminPanelFlow.remove_admin_pick, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def adminpanel_remove_admin_pick_stray_text(msg: Message) -> None:
    # Same gap-fix as the group panel's modpanel_remove_word_pick_stray_text —
    # this state previously had no message handler, so typed text here was
    # silently dropped with zero reply.
    await msg.answer("Пожалуйста, выберите администратора кнопкой выше, либо отправьте /cancel.")

@router.message(AdminPanelFlow.remove_admin_text, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def adminpanel_remove_admin_text(msg: Message, bot: Bot, state: FSMContext, config: ModeratorConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await _clear_flow_keep_panel(state)
        await msg.answer("⏳ Время ожидания истекло — начните заново кнопкой «👥 Админы»."); return
    if not await _is_bot_admin(msg.from_user.id, config):
        await _clear_flow_keep_panel(state)
        await msg.answer("⛔ Нет доступа"); return
    target = msg.text.strip()
    result = await _remove_bot_admin(config.db_path, target)
    if result == "not_found":
        await msg.answer(f"«{_esc(target)}» не найден среди администраторов.", parse_mode="HTML"); return
    if result == "last_admin":
        await _clear_flow_keep_panel(state)
        await msg.answer("⚠️ Нельзя удалить единственного администратора.")
        await _replace_panel(
            bot, state, msg.chat.id, await _admins_list_text(config),
            reply_markup=kb_admins_panel(), parse_mode="HTML",
        )
        return
    logger.info(f"adminpanel_remove_admin_text: {target} removed as bot admin by {msg.from_user.id}")
    await _clear_flow_keep_panel(state)
    await msg.answer(f"✅ <code>{_esc(target)}</code> удалён.", parse_mode="HTML")
    await _replace_panel(
        bot, state, msg.chat.id, await _admins_list_text(config),
        reply_markup=kb_admins_panel(), parse_mode="HTML",
    )


@router.callback_query(F.data == "mod_admin_cancel")
async def cb_admin_cancel(cb: CallbackQuery, bot: Bot, state: FSMContext, config: ModeratorConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    is_admin = await _is_bot_admin(cb.from_user.id, config)
    await _clear_flow_keep_panel(state)
    await cb.message.answer("Отменено.")
    if is_admin:
        await _replace_panel(
            bot, state, chat_id, await _admins_list_text(config),
            reply_markup=kb_admins_panel(), parse_mode="HTML",
        )

@router.message(Command("cancel"), F.chat.type == "private", StateFilter("*"))
async def cmd_private_cancel(msg: Message, bot: Bot, state: FSMContext, config: ModeratorConfig):
    current_state = await state.get_state()
    if current_state is None:
        await msg.answer("Нечего отменять."); return
    # A raw /cancel mid a per-group sub-flow (add/remove word, set threshold)
    # must return to THAT group's panel, not unconditionally fall through to
    # the admins panel below — same distinction _clear_flow_keep_panel_and_group
    # vs _clear_flow_keep_panel draws elsewhere in this section.
    if current_state in _PRIVATE_GROUP_FLOW_STATES:
        data = await state.get_data()
        target = data.get("selected_group")
        # Same re-check as cb_private_mod_cancel — showing the group panel
        # here without it would leak that group's rights/stopwords to a user
        # whose admin status was revoked mid-flow.
        still_admin = target is not None and await _is_group_admin(bot, target, msg.from_user.id)
        await msg.answer("Отменено.")
        if still_admin:
            await _clear_flow_keep_panel_and_group(state)
            text = await _group_panel_text(bot, config, target)
            await _replace_panel(bot, state, msg.chat.id, text, reply_markup=kb_group_panel(), parse_mode="HTML")
        else:
            await _clear_flow_keep_panel(state)
        return
    is_admin = await _is_bot_admin(msg.from_user.id, config)
    await _clear_flow_keep_panel(state)
    await msg.answer("Отменено.")
    if is_admin:
        await _replace_panel(
            bot, state, msg.chat.id, await _admins_list_text(config),
            reply_markup=kb_admins_panel(), parse_mode="HTML",
        )


# ── private: orphaned-flow catch-all ──────────────────────────────────────────
# Review-found: MemoryStorage has no persistence — a process restart (Railway
# redeploy) wipes every in-flight FSM state instantly. Before this handler, a
# user mid-flow (e.g. having just been shown "Пришлите Telegram ID...") whose
# next message arrived after a restart matched NO AdminPanelFlow/ModPanelFlow
# state handler at all (state is gone) and fell through with total silence —
# the FLOW_TIMEOUT_SECONDS/_flow_expired mechanism only covers a user going
# quiet, not the bot itself losing its memory of the conversation. Registered
# last (aiogram tries handlers in registration order within a router) so it
# only ever fires when nothing more specific — including every StateFilter
# above — matched first.
@router.message(F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def private_orphaned_message_catchall(msg: Message) -> None:
    await msg.answer(
        "⏳ Сессия сброшена (перезапуск бота) — начните заново кнопкой «👥 Админы» "
        "или «⚙️ Настроить группу», либо командой /start."
    )


# ── MAIN ──────────────────────────────────────────────────────────────────────

async def main():
    config = config_from_env()
    bot = Bot(token=os.getenv("BOT_TOKEN"))
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(ConfigMiddleware(config))
    dp.include_router(router)
    await bot.set_my_description(BOT_DESCRIPTION)
    await init_db(config.db_path)
    # my_chat_member is NOT delivered by default long-polling — must be listed
    # explicitly, or on_bot_membership_changed (mechanism #1 of the rights
    # check) never fires in standalone/subprocess mode.
    await dp.start_polling(bot, allowed_updates=["message", "callback_query", "chat_member", "my_chat_member"])

if __name__ == "__main__":
    asyncio.run(main())
