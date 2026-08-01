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
    "Настройка (запрещённые слова, порог предупреждений, проверка прав) выполняется "
    "прямо в группе, где я состою. Здесь, в личных сообщениях, доступны кнопки ниже:"
)
# ── END CUSTOMIZE ─────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
router = Router()

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
    return config


class ConfigMiddleware(BaseMiddleware):
    """Injects this bot's ModeratorConfig into data["config"]."""

    def __init__(self, config: ModeratorConfig) -> None:
        self.config = config
        super().__init__()

    async def __call__(self, handler, event, data):
        data["config"] = self.config
        return await handler(event, data)


# ── admin helpers (admins_file — bot owner's admins, used for /modlog) ───────

def _load_admins(admins_file: Path) -> set:
    try:
        return set(json.loads(admins_file.read_text()).get("ids", []))
    except Exception:
        return set()

def _save_admins(admins_file: Path, ids: set) -> None:
    # Review-found: the admin-panel button flow added 3 more write sites on
    # top of the pre-existing 2 (raw /addadmin, /removeadmin) — a crash mid
    # write (process kill, disk full) on a plain write_text() truncates the
    # file first, so a following read sees invalid JSON and _load_admins
    # silently returns an EMPTY set. Combined with cmd_start's "first
    # /start-er becomes the bot's admin" bootstrap, that would hand admin
    # access to whoever happens to message the bot next. Write to a sibling
    # temp file and atomically replace instead — a crash mid-write leaves the
    # ORIGINAL file untouched.
    tmp_path = admins_file.with_suffix(admins_file.suffix + ".tmp")
    tmp_path.write_text(json.dumps({"ids": list(ids)}, ensure_ascii=False))
    tmp_path.replace(admins_file)


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
        await db.commit()


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

RIGHTS_INSTRUCTIONS = (
    "⚠️ <b>У бота недостаточно прав для модерации в этой группе.</b>\n\n"
    "Чтобы включить модерацию:\n"
    "1. Настройки группы → «Администраторы»\n"
    "2. Выберите бота (или «Добавить администратора» → найдите бота)\n"
    "3. Включите «Удаление сообщений» и «Блокировка пользователей»\n"
    "4. Сохраните\n\n"
    "После этого проверьте права командой /checkrights."
)


async def _check_and_report_rights(bot: Bot, chat_id: int, member, silent_if_ok: bool) -> bool:
    """`member` is a ChatMember describing the BOT's OWN status in chat_id —
    either straight from a my_chat_member update, or freshly fetched via
    get_chat_member. Returns whether rights are sufficient.

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
    try:
        if has_rights:
            if not silent_if_ok:
                await bot.send_message(chat_id, "✅ Всё в порядке, у бота есть права на удаление/мут/бан.")
        else:
            await bot.send_message(chat_id, RIGHTS_INSTRUCTIONS, parse_mode="HTML")
    except Exception as e:
        logger.error(f"_check_and_report_rights: failed to message chat {chat_id}: {e}")
    return has_rights


@router.my_chat_member()
async def on_bot_membership_changed(update: ChatMemberUpdated, bot: Bot):
    """No direction filter (not just IS_NOT_MEMBER >> IS_MEMBER) — on Telegram
    the bot is typically added as a plain member first, then promoted to admin
    as a SEPARATE later action; a narrower filter would miss the moment rights
    actually arrive. Reads update.new_chat_member directly instead of issuing
    a fresh get_chat_member call — the bot's new status is already IN this
    update."""
    if update.chat.type not in ("group", "supergroup"):
        return
    await _check_and_report_rights(bot, update.chat.id, update.new_chat_member, silent_if_ok=True)


@router.message(Command("checkrights"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_checkrights(msg: Message, bot: Bot):
    if not await _is_group_admin(bot, msg.chat.id, msg.from_user.id):
        await msg.answer("⛔ Команда доступна только администраторам группы.")
        return
    try:
        bot_member = await bot.get_chat_member(msg.chat.id, bot.id)
    except Exception as e:
        logger.error(f"cmd_checkrights: get_chat_member for bot failed in chat {msg.chat.id}: {e}")
        await msg.answer("⚠️ Не удалось проверить права — попробуйте ещё раз позже.")
        return
    await _check_and_report_rights(bot, msg.chat.id, bot_member, silent_if_ok=False)


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
        for admin_id in _load_admins(config.admins_file):
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

def kb_start_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Проверить права", callback_data="mod_help:rights")],
        [InlineKeyboardButton(text="🚫 Запрещённые слова", callback_data="mod_help:stopwords")],
        [InlineKeyboardButton(text="👥 Админы", callback_data="mod_admins")],
        [InlineKeyboardButton(text="📜 Журнал модерации", callback_data="mod_modlog")],
    ])

# "🔍 Проверить права" / "🚫 Запрещённые слова" only redirect (see Variant A
# discussion): these act on a SPECIFIC group's chat_id, which a private /start
# message has no way to know — the group itself is where /checkrights and the
# /stopwords panel actually run. "👥 Админы" / "📜 Журнал модерации" need no
# group context (admins_file/moderation_log are global to this bot), so their
# buttons execute directly instead.
_START_HELP_TEXTS = {
    "rights": "🔍 Эта функция работает в группе, где я состою — отправьте /checkrights прямо в группе.",
    "stopwords": "🚫 Управление запрещёнными словами (и порогом предупреждений) настраивается в группе, где я состою — отправьте /stopwords прямо в группе.",
}


async def _replace_panel(
    bot: Bot, state: FSMContext, chat_id: int, text: str,
    reply_markup: InlineKeyboardMarkup | None = None, parse_mode: str | None = None,
) -> None:
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
    prompt/pick-list message from the step just finished was never deleted)."""
    data = await state.get_data()
    prev_id = data.get("panel_msg_id")
    if prev_id:
        try:
            await bot.delete_message(chat_id, prev_id)
        except Exception:
            pass
    # Review-found: unlike every other Telegram call in this file, this send
    # had no try/except — an unbounded admin ID (see _admins_list_text) or a
    # blocked/deactivated user would raise here unhandled, on EVERY future
    # panel render, for every admin, until fixed by hand.
    try:
        msg = await bot.send_message(chat_id, text, parse_mode=parse_mode, reply_markup=reply_markup)
    except Exception as e:
        logger.warning(f"_replace_panel: failed to send panel to chat {chat_id}: {e}")
        return
    await state.update_data(panel_msg_id=msg.message_id)


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


def _is_bot_admin(user_id: int, config: ModeratorConfig) -> bool:
    return str(user_id) in _load_admins(config.admins_file)


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
    ASCII) — silently creating a permanently-unmatchable phantom admin."""
    core = text.lstrip("-")
    return bool(core) and core.isascii() and core.isdigit() and len(core) <= 15


async def _admins_list_text(config: ModeratorConfig) -> str:
    ids = _load_admins(config.admins_file)
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
    admins = _load_admins(config.admins_file)
    first_time_admin = not admins
    if first_time_admin:
        _save_admins(config.admins_file, {str(message.from_user.id)})
    if config.welcome_image.exists():
        await message.answer_photo(
            FSInputFile(str(config.welcome_image)), caption=WELCOME_TEXT, parse_mode="HTML",
            reply_markup=kb_start_menu(),
        )
    else:
        await message.answer(WELCOME_TEXT, parse_mode="HTML", reply_markup=kb_start_menu())
    if first_time_admin:
        await message.answer(
            "👑 <b>Вы — администратор этого бота.</b>\n\n"
            "Управление администраторами бота (доступ к журналу модерации) — "
            "кнопка «👥 Админы» выше.",
            parse_mode="HTML",
        )

@router.callback_query(F.data.startswith("mod_help:"))
async def cb_start_help(cb: CallbackQuery, bot: Bot, state: FSMContext):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    # Review-found: navigating to a DIFFERENT top-level destination must
    # cancel any AdminPanelFlow left dangling by a previous "➕ Добавить
    # админа"/"➖ Убрать админа" press — otherwise the next ordinary message
    # this user sends here (or after mod_admins/mod_modlog) still gets
    # silently captured as an admin id. _clear_flow_keep_panel (not a bare
    # state.clear()) so the delete-old step in _replace_panel right below
    # still knows which earlier panel message to remove.
    await _clear_flow_keep_panel(state)
    text = _START_HELP_TEXTS.get(cb.data.split(":", 1)[1])
    if text:
        await _replace_panel(bot, state, chat_id, text)

@router.callback_query(F.data == "mod_admins")
async def cb_admins(cb: CallbackQuery, bot: Bot, state: FSMContext, config: ModeratorConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
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
    if not _is_bot_admin(cb.from_user.id, config):
        await cb.message.answer("⛔ Нет доступа"); return
    await _clear_flow_keep_panel(state)
    await _replace_panel(bot, state, chat_id, await _modlog_text(config), parse_mode="HTML")

@router.message(Command("modlog"), F.chat.type == "private")
async def cmd_modlog(msg: Message, config: ModeratorConfig):
    if not _is_bot_admin(msg.from_user.id, config):
        await msg.answer("⛔ Нет доступа"); return
    await msg.answer(await _modlog_text(config), parse_mode="HTML")

@router.message(Command("addadmin"))
async def cmd_addadmin(msg: Message, config: ModeratorConfig):
    if not _is_bot_admin(msg.from_user.id, config): await msg.answer("⛔ Нет доступа"); return
    parts = msg.text.split()
    if len(parts) < 2 or not _valid_admin_id(parts[1]): await msg.answer("Использование: /addadmin <id>"); return
    ids = _load_admins(config.admins_file); ids.add(parts[1]); _save_admins(config.admins_file, ids)
    logger.info(f"cmd_addadmin: {parts[1]} added as bot admin by {msg.from_user.id}")
    await msg.answer(f"✅ <code>{parts[1]}</code> добавлен.", parse_mode="HTML")

@router.message(Command("removeadmin"))
async def cmd_removeadmin(msg: Message, config: ModeratorConfig):
    if not _is_bot_admin(msg.from_user.id, config): await msg.answer("⛔ Нет доступа"); return
    parts = msg.text.split()
    if len(parts) < 2: await msg.answer("Использование: /removeadmin <id>"); return
    ids = _load_admins(config.admins_file)
    # Review-found: removing the last remaining bot admin hands the role to
    # whoever happens to message /start next (cmd_start's bootstrap) — refuse
    # rather than silently emptying admins_file.
    if parts[1] in ids and len(ids) <= 1:
        await msg.answer("⚠️ Нельзя удалить единственного администратора."); return
    ids.discard(parts[1]); _save_admins(config.admins_file, ids)
    logger.info(f"cmd_removeadmin: {parts[1]} removed as bot admin by {msg.from_user.id}")
    # Review-found: unlike cmd_addadmin (validates isdigit() before this point),
    # this echoed the raw argument straight into a parse_mode="HTML" <code>
    # block — arbitrary text containing '<'/'&' would break the HTML the
    # message is sent with (send fails outright, not an XSS risk here since
    # there's no browser rendering it, but still a real correctness bug).
    await msg.answer(f"✅ <code>{_esc(parts[1])}</code> удалён.", parse_mode="HTML")

@router.message(Command("admins"))
async def cmd_admins(msg: Message, config: ModeratorConfig):
    if not _is_bot_admin(msg.from_user.id, config): await msg.answer("⛔ Нет доступа"); return
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
    if not _is_bot_admin(cb.from_user.id, config):
        await cb.message.answer("⛔ Нет доступа"); return
    await _replace_panel(
        bot, state, chat_id,
        "Пришлите Telegram ID пользователя, которого нужно сделать администратором:",
        reply_markup=kb_private_cancel(),
    )
    await state.update_data(started_at=time.time())
    await state.set_state(AdminPanelFlow.add_admin)

@router.message(AdminPanelFlow.add_admin, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def adminpanel_add_admin(msg: Message, bot: Bot, state: FSMContext, config: ModeratorConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("⏳ Время ожидания истекло — начните заново кнопкой «👥 Админы»."); return
    if not _is_bot_admin(msg.from_user.id, config):
        await state.clear()
        await msg.answer("⛔ Нет доступа"); return
    text = msg.text.strip()
    if not _valid_admin_id(text):
        await msg.answer("Пришлите числовой Telegram ID."); return
    ids = _load_admins(config.admins_file); ids.add(text); _save_admins(config.admins_file, ids)
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
    if not _is_bot_admin(cb.from_user.id, config):
        await cb.message.answer("⛔ Нет доступа"); return
    ids = sorted(_load_admins(config.admins_file))
    if not ids:
        await _replace_panel(bot, state, chat_id, "Список администраторов пуст.")
        return
    # Review-found: same last-admin guard as cmd_removeadmin — block the
    # no-op-but-dangerous case upfront instead of offering a picker that
    # would empty admins_file on the very next tap.
    if len(ids) == 1:
        await _replace_panel(
            bot, state, chat_id,
            "⚠️ Вы — единственный администратор бота. Сначала добавьте ещё одного "
            "через «➕ Добавить админа», прежде чем убирать себя.",
            reply_markup=kb_admins_panel(), parse_mode="HTML",
        )
        return
    if len(ids) <= MAX_ADMIN_REMOVE_BUTTONS:
        await _replace_panel(
            bot, state, chat_id, "Выберите администратора для удаления:",
            reply_markup=kb_remove_admins(ids),
        )
        await state.update_data(remove_admin_ids=ids, started_at=time.time())
        await state.set_state(AdminPanelFlow.remove_admin_pick)
    else:
        await _replace_panel(
            bot, state, chat_id, "Админов много — пришлите ID для удаления текстом:",
            reply_markup=kb_private_cancel(),
        )
        await state.update_data(started_at=time.time())
        await state.set_state(AdminPanelFlow.remove_admin_text)

@router.callback_query(AdminPanelFlow.remove_admin_pick, F.data.startswith("mod_rma:"))
async def cb_removeadmin_pick(cb: CallbackQuery, bot: Bot, state: FSMContext, config: ModeratorConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        await state.clear()
        await cb.message.answer("⛔ Нет доступа"); return
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await _replace_panel(bot, state, chat_id, "⏳ Время ожидания истекло — откройте заново кнопкой «👥 Админы».")
        return
    ids = data.get("remove_admin_ids", [])
    # Same malformed/stale-callback guard as the group panel's cb_removeword_pick.
    try:
        idx = int(cb.data.split(":", 1)[1])
    except ValueError:
        idx = -1
    if idx < 0 or idx >= len(ids):
        await state.clear()
        await _replace_panel(bot, state, chat_id, "Список устарел — откройте заново кнопкой «👥 Админы».")
        return
    target = ids[idx]
    admins = _load_admins(config.admins_file)
    # Review-found: unconditionally reporting "✅ Убрано" even if the target
    # had ALREADY been removed (stale snapshot vs. a concurrent change) was a
    # false-success message — mirror the group panel's cb_removeword_pick,
    # which checks this honestly instead of assuming the snapshot still holds.
    if target not in admins:
        await _clear_flow_keep_panel(state)
        await cb.message.answer(f"«{_esc(target)}» уже не администратор.", parse_mode="HTML")
        await _replace_panel(
            bot, state, chat_id, await _admins_list_text(config),
            reply_markup=kb_admins_panel(), parse_mode="HTML",
        )
        return
    if len(admins) <= 1:
        await _clear_flow_keep_panel(state)
        await _replace_panel(
            bot, state, chat_id, "⚠️ Нельзя удалить единственного администратора.",
            reply_markup=kb_admins_panel(), parse_mode="HTML",
        )
        return
    admins.discard(target)
    _save_admins(config.admins_file, admins)
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
        await state.clear()
        await msg.answer("⏳ Время ожидания истекло — начните заново кнопкой «👥 Админы»."); return
    if not _is_bot_admin(msg.from_user.id, config):
        await state.clear()
        await msg.answer("⛔ Нет доступа"); return
    target = msg.text.strip()
    admins = _load_admins(config.admins_file)
    if target not in admins:
        await msg.answer(f"«{_esc(target)}» не найден среди администраторов.", parse_mode="HTML"); return
    if len(admins) <= 1:
        await _clear_flow_keep_panel(state)
        await msg.answer("⚠️ Нельзя удалить единственного администратора.")
        await _replace_panel(
            bot, state, msg.chat.id, await _admins_list_text(config),
            reply_markup=kb_admins_panel(), parse_mode="HTML",
        )
        return
    admins.discard(target)
    _save_admins(config.admins_file, admins)
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
    is_admin = _is_bot_admin(cb.from_user.id, config)
    await _clear_flow_keep_panel(state)
    await cb.message.answer("Отменено.")
    if is_admin:
        await _replace_panel(
            bot, state, chat_id, await _admins_list_text(config),
            reply_markup=kb_admins_panel(), parse_mode="HTML",
        )

@router.message(Command("cancel"), F.chat.type == "private", StateFilter("*"))
async def cmd_private_cancel(msg: Message, bot: Bot, state: FSMContext, config: ModeratorConfig):
    if await state.get_state() is None:
        await msg.answer("Нечего отменять."); return
    is_admin = _is_bot_admin(msg.from_user.id, config)
    await _clear_flow_keep_panel(state)
    await msg.answer("Отменено.")
    if is_admin:
        await _replace_panel(
            bot, state, msg.chat.id, await _admins_list_text(config),
            reply_markup=kb_admins_panel(), parse_mode="HTML",
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
