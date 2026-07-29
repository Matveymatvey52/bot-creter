# TEMPLATE: moderator
# USE FOR: модерация чата, антиспам, стоп-слова, лестница предупреждение-мут-бан, журнал модерации
# CUSTOMIZE: sections marked with # CUSTOMIZE
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import aiosqlite
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    ChatMemberAdministrator, ChatMemberUpdated, ChatPermissions, FSInputFile, Message,
)

# ── CUSTOMIZE ────────────────────────────────────────────────────────────────
# Same status as every other template's CUSTOMIZE block: per-file source-text
# customization Claude edits when generating a specific bot, not per-bot
# runtime state (that's config.db_path/admins_file below).
BOT_DESCRIPTION = "Модерация группового чата: антиспам, стоп-слова, лестница предупреждение → мут → бан, журнал модерации."
WELCOME_TEXT = (
    "🛡 <b>Модератор чата</b>\n\n"
    "Добавьте меня в группу и выдайте права администратора (пункты «Удаление "
    "сообщений» и «Блокировка пользователей») — и я начну следить за порядком: "
    "спам-ссылки, стоп-слова, лестница предупреждение → мут → бан.\n\n"
    "Команды внутри группы (только для администраторов группы):\n"
    "<code>/checkrights</code> — проверить, хватает ли у меня прав\n"
    "<code>/addstopword слово</code>, <code>/removestopword слово</code>, <code>/stopwords</code>\n"
    "<code>/setmaxwarnings N</code> — порог предупреждений до мута\n\n"
    "Здесь, в личных сообщениях:\n"
    "<code>/modlog</code> — сводный журнал модерации по всем вашим группам"
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
    admins_file.write_text(json.dumps({"ids": list(ids)}, ensure_ascii=False))


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


@router.message(F.chat.type.in_({"group", "supergroup"}), F.text | F.caption, ~F.text.startswith("/"))
async def moderate_message(msg: Message, bot: Bot, config: ModeratorConfig):
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
        reason = "стоп-слово"

    if reason is None:
        return

    await _moderate_safely(bot, config, msg.chat.id, "delete_message", msg.delete())
    # Escalation runs regardless of whether the delete above succeeded — a
    # missing "delete" right must not also silently suppress warn/mute/ban
    # tracking; each elevated action reports its own failure independently.
    await _apply_escalation(bot, config, msg.chat.id, msg.from_user, reason, settings["max_warnings"], settings["mute_minutes"])


# ── group settings commands (Telegram-native admin/creator only) ─────────────

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
    await msg.answer(f"✅ Стоп-слово «{_esc(word)}» добавлено.", parse_mode="HTML")

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
        await msg.answer(f"Стоп-слово «{_esc(word)}» не найдено.", parse_mode="HTML"); return
    await msg.answer(f"✅ Стоп-слово «{_esc(word)}» удалено.", parse_mode="HTML")

@router.message(Command("stopwords"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_stopwords(msg: Message, bot: Bot, config: ModeratorConfig):
    if not await _is_group_admin(bot, msg.chat.id, msg.from_user.id):
        await msg.answer("⛔ Команда доступна только администраторам группы."); return
    async with aiosqlite.connect(config.db_path) as db:
        rows = await (await db.execute(
            "SELECT word FROM stopwords WHERE chat_id=? ORDER BY word", (msg.chat.id,)
        )).fetchall()
    if not rows:
        await msg.answer("Список стоп-слов пуст."); return
    await msg.answer(_join_bounded(["🚫 <b>Стоп-слова:</b>\n"] + [f"• {_esc(r[0])}" for r in rows]), parse_mode="HTML")

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


# ── private: /start, /modlog, admins_file management ──────────────────────────

@router.message(Command("start"), F.chat.type == "private")
async def cmd_start(message: Message, config: ModeratorConfig):
    admins = _load_admins(config.admins_file)
    first_time_admin = not admins
    if first_time_admin:
        _save_admins(config.admins_file, {str(message.from_user.id)})
    if config.welcome_image.exists():
        await message.answer_photo(FSInputFile(str(config.welcome_image)), caption=WELCOME_TEXT, parse_mode="HTML")
    else:
        await message.answer(WELCOME_TEXT, parse_mode="HTML")
    if first_time_admin:
        await message.answer(
            "👑 <b>Вы — администратор этого бота.</b>\n\n"
            "Управление администраторами бота (доступ к /modlog):\n"
            "<code>/addadmin ID</code> — добавить\n"
            "<code>/removeadmin ID</code> — убрать\n"
            "<code>/admins</code> — список",
            parse_mode="HTML",
        )

@router.message(Command("modlog"), F.chat.type == "private")
async def cmd_modlog(msg: Message, config: ModeratorConfig):
    if str(msg.from_user.id) not in _load_admins(config.admins_file):
        await msg.answer("⛔ Нет доступа"); return
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT chat_id, user_id, action, reason, created_at FROM moderation_log ORDER BY id DESC LIMIT 30"
        )).fetchall()
    if not rows:
        await msg.answer("Журнал модерации пуст."); return
    icon = {"warn": "⚠️", "mute": "🔇", "ban": "🚫"}
    lines = ["📋 <b>Журнал модерации (последние 30):</b>\n"]
    for r in rows:
        lines.append(
            f"{r['created_at']} {icon.get(r['action'], '•')} чат {r['chat_id']} · "
            f"пользователь {r['user_id']} · {_esc(r['reason']) if r['reason'] else '—'}"
        )
    await msg.answer(_join_bounded(lines), parse_mode="HTML")

@router.message(Command("addadmin"))
async def cmd_addadmin(msg: Message, config: ModeratorConfig):
    if str(msg.from_user.id) not in _load_admins(config.admins_file): await msg.answer("⛔ Нет доступа"); return
    parts = msg.text.split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit(): await msg.answer("Использование: /addadmin <id>"); return
    ids = _load_admins(config.admins_file); ids.add(parts[1]); _save_admins(config.admins_file, ids)
    await msg.answer(f"✅ <code>{parts[1]}</code> добавлен.", parse_mode="HTML")

@router.message(Command("removeadmin"))
async def cmd_removeadmin(msg: Message, config: ModeratorConfig):
    if str(msg.from_user.id) not in _load_admins(config.admins_file): await msg.answer("⛔ Нет доступа"); return
    parts = msg.text.split()
    if len(parts) < 2: await msg.answer("Использование: /removeadmin <id>"); return
    ids = _load_admins(config.admins_file); ids.discard(parts[1]); _save_admins(config.admins_file, ids)
    # Review-found: unlike cmd_addadmin (validates isdigit() before this point),
    # this echoed the raw argument straight into a parse_mode="HTML" <code>
    # block — arbitrary text containing '<'/'&' would break the HTML the
    # message is sent with (send fails outright, not an XSS risk here since
    # there's no browser rendering it, but still a real correctness bug).
    await msg.answer(f"✅ <code>{_esc(parts[1])}</code> удалён.", parse_mode="HTML")

@router.message(Command("admins"))
async def cmd_admins(msg: Message, config: ModeratorConfig):
    if str(msg.from_user.id) not in _load_admins(config.admins_file): await msg.answer("⛔ Нет доступа"); return
    ids = _load_admins(config.admins_file)
    await msg.answer("👥 " + ("\n".join(f"• <code>{i}</code>" for i in ids) or "Пусто"), parse_mode="HTML")


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
