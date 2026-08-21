# TEMPLATE: referral_program
# USE FOR: реферальная программа — персональный код/ссылка, начисление награды после выполнения условия (не при регистрации), лидерборд по успешным приглашениям
# CUSTOMIZE: sections marked with # CUSTOMIZE
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import secrets
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import aiosqlite
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, Message, ReplyKeyboardMarkup,
)

from db.database import add_bot_admin, remove_bot_admin

# ── CUSTOMIZE ────────────────────────────────────────────────────────────────
# Same status as every other template's CUSTOMIZE block: per-file source-text
# customization Claude edits when generating a specific bot, not per-bot
# runtime state (that's config.db_path/admins_file below).
BOT_DESCRIPTION = "Реферальная программа: персональные ссылки, награда за приглашённых после выполнения условия, лидерборд."
WELCOME_TEXT = (
    "🎁 <b>Реферальная программа</b>\n\n"
    "Приглашайте друзей по своей персональной ссылке — как только приглашённый "
    "выполнит условие, вы получите награду.\n\n"
    "Выберите действие:"
)
CONDITION_LABEL = "Первая покупка"  # что должен сделать приглашённый, чтобы засчиталась награда
REWARD_POINTS = 100
REWARD_UNIT_LABEL = "баллов"
# ── END CUSTOMIZE ─────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
router = Router()

# ── mini-app config (see docs/MINIAPP_DESIGN.md, templates/tour_operator.py's
# own miniapp_config for the pilot) ─────────────────────────────────────────
# Declarative schema read by runtime/miniapp_api.py's generic CRUD handlers —
# NOT executable code (see that module's docstring). `table` and each field's
# `name` must match init_db()'s CREATE TABLE columns below exactly. Only
# `referrals` is exposed — `participants` uses `user_id` as its primary key
# (no `id` column), which runtime/miniapp_api.py's generic handlers don't
# support (they SELECT/UPDATE by a single `id` column).
miniapp_config = {
    "resources": [
        {
            "name": "referrals",
            "table": "referrals",
            "order_by": "created_at DESC",
            "creatable": False,
            "scope": {"type": "global"},
            "title": "Рефералы",
            "titleField": "status",
            "fields": [
                {"name": "referrer_id", "required": True, "label": "ID пригласившего", "kind": "number", "list": True, "detail": True, "create": False},
                {"name": "referred_id", "required": True, "label": "ID приглашённого", "kind": "number", "list": True, "detail": True, "create": False},
                {"name": "status", "label": "Статус", "kind": "status", "list": True, "detail": True, "create": False},
                {"name": "created_at", "label": "Создано", "kind": "date", "list": False, "detail": True, "create": False},
                {"name": "rewarded_at", "label": "Награда выдана", "kind": "date", "list": False, "detail": True, "create": False},
            ],
        },
    ],
}

# Excludes 0/O/1/I/L — a code shared verbally or in a low-res screenshot must
# not depend on the reader telling those apart.
_CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
_CODE_LEN = 6


def _esc(value, max_len: int = 200) -> str:
    """HTML-escapes AND length-bounds any user-supplied text before it goes into
    a parse_mode="HTML" message — same helper/rationale as templates/
    booking_medical.py's _esc(): a free-text Telegram full_name could otherwise
    contain '<'/'&' and break message rendering."""
    text = str(value) if value is not None else ""
    if len(text) > max_len:
        text = text[:max_len] + "…"
    return html.escape(text)


# ── config ───────────────────────────────────────────────────────────────────
# Same pattern as every other template — see docs/STAGE2_DESIGN.md.

@dataclass
class ReferralConfig:
    bot_name: str
    db_path: str
    admins_file: Path
    welcome_image: Path
    display_name: str | None = None
    group_chat_id: str | None = None
    bot_id: int | None = None
    owner_telegram_id: int | None = None


def _paths_for(name: str, data_dir: Path) -> ReferralConfig:
    return ReferralConfig(
        bot_name=name,
        db_path=str(data_dir / f"{name}_data.db"),
        admins_file=data_dir / f"admins_{name}.json",
        welcome_image=data_dir / "bot_images" / f"{name}.jpg",
    )


def config_from_env() -> ReferralConfig:
    """Standalone/subprocess mode."""
    name = Path(__file__).stem
    data_dir = Path(os.getenv("DATA_DIR", "./data"))
    data_dir.mkdir(exist_ok=True)
    return _paths_for(name, data_dir)


def config_from_bot_row(bot_row: dict, data_dir: Path) -> ReferralConfig:
    """Webhook runtime mode. Paths built from bot_row["bot_id"] (bots.id, the
    physically unique AUTOINCREMENT PK) — NOT bot_row["name"] — same reasoning
    as every other template's config_from_bot_row (see docs/STAGE2_DESIGN.md
    "Изоляция по bots.id")."""
    bot_id = bot_row["bot_id"]
    config = ReferralConfig(
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
    """Injects this bot's ReferralConfig into data["config"]."""

    def __init__(self, config: ReferralConfig) -> None:
        self.config = config
        super().__init__()

    async def __call__(self, handler, event, data):
        data["config"] = self.config
        return await handler(event, data)


# ── admin helpers ─────────────────────────────────────────────────────────────

def _load_admins(admins_file: Path) -> set:
    try:
        return set(json.loads(admins_file.read_text()).get("ids", []))
    except Exception:
        return set()

def _save_admins(admins_file: Path, ids: set) -> None:
    admins_file.write_text(json.dumps({"ids": list(ids)}, ensure_ascii=False))

def _is_admin(user_id: int, config: ReferralConfig) -> bool:
    # The DB-known owner (bots.owner_telegram_id) is always an admin, even if
    # the local admins_file is empty/stale/hijacked — see cmd_start below for
    # why the file alone can't be trusted as the sole source of truth.
    if config.owner_telegram_id is not None and str(user_id) == str(config.owner_telegram_id):
        return True
    return str(user_id) in _load_admins(config.admins_file)


# ── db ────────────────────────────────────────────────────────────────────────

async def init_db(db_path: str):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS participants (
                user_id     INTEGER PRIMARY KEY,
                full_name   TEXT,
                ref_code    TEXT NOT NULL UNIQUE,
                referred_by INTEGER REFERENCES participants(user_id),
                balance     INTEGER NOT NULL DEFAULT 0,
                joined_at   TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS referrals (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id INTEGER NOT NULL REFERENCES participants(user_id),
                referred_id INTEGER NOT NULL UNIQUE REFERENCES participants(user_id),
                status      TEXT NOT NULL DEFAULT 'pending',
                created_at  TEXT DEFAULT (datetime('now','localtime')),
                rewarded_at TEXT
            )
        """)
        await db.commit()


def _generate_ref_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LEN))


async def _insert_participant_with_unique_code(
    db: aiosqlite.Connection, user_id: int, full_name: str, referred_by: int | None
) -> str:
    """Retries on a ref_code collision only — a UNIQUE violation on user_id
    itself is a caller bug (must pre-check existence) and is re-raised."""
    for _ in range(5):
        code = _generate_ref_code()
        try:
            await db.execute(
                "INSERT INTO participants (user_id, full_name, ref_code, referred_by) VALUES (?,?,?,?)",
                (user_id, full_name, code, referred_by)
            )
            return code
        except sqlite3.IntegrityError as e:
            if "ref_code" not in str(e):
                raise
    raise RuntimeError("could not generate a unique referral code after 5 attempts")


# ── keyboards ─────────────────────────────────────────────────────────────────

def kb_main() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🔗 Моя ссылка"), KeyboardButton(text="🏆 Лидерборд")],
    ], resize_keyboard=True)


# ── /start (registration + referral attribution) ─────────────────────────────

@router.message(Command("start"))
async def cmd_start(message: Message, command: CommandObject, config: ReferralConfig):
    admins = _load_admins(config.admins_file)
    user_id = message.from_user.id
    # Bug fixed here: this used to grant admin to whoever sent /start FIRST,
    # which lets any client who messages the bot before the owner does
    # permanently seize the admin panel. When bots.owner_telegram_id is known
    # (webhook/production mode), only that user may claim the empty-admins
    # bootstrap slot; a non-owner sending /start first now just gets the
    # regular participant flow. In standalone/env mode (owner_telegram_id
    # unknown) the old first-comer behavior is kept as the only option
    # available.
    is_owner = config.owner_telegram_id is not None and user_id == config.owner_telegram_id
    first_time_admin = not admins and (is_owner or config.owner_telegram_id is None)
    if first_time_admin:
        _save_admins(config.admins_file, {str(user_id)})
        if config.bot_id is not None:
            try:
                await add_bot_admin(config.bot_id, str(user_id))
            except Exception as e:
                logger.warning(f"cmd_start: add_bot_admin sync failed for bot {config.bot_id}: {e}")
    async with aiosqlite.connect(config.db_path) as db:
        existing = await (await db.execute(
            "SELECT 1 FROM participants WHERE user_id=?", (user_id,)
        )).fetchone()
        if not existing:
            # Attribution happens ONLY for a brand-new participant — a
            # returning user re-opening someone else's invite link must not
            # be re-attributed after the fact (design: no retroactive
            # attribution, one referrer per user forever).
            referrer_id = None
            if command.args:
                row = await (await db.execute(
                    "SELECT user_id FROM participants WHERE ref_code=?", (command.args.strip().upper(),)
                )).fetchone()
                if row:
                    referrer_id = row[0]
            try:
                await _insert_participant_with_unique_code(
                    db, user_id, message.from_user.full_name or "", referrer_id
                )
                if referrer_id:
                    await db.execute(
                        "INSERT INTO referrals (referrer_id, referred_id) VALUES (?,?)",
                        (referrer_id, user_id)
                    )
                await db.commit()
            except sqlite3.IntegrityError:
                # Two concurrent /start updates for the SAME brand-new user_id
                # (double-tap, Telegram update retry) both pass the `not
                # existing` check above; the loser hits participants.user_id's
                # PRIMARY KEY here (ref_code collisions are already retried
                # inside _insert_participant_with_unique_code, so any
                # IntegrityError reaching this point is a user_id collision).
                # The winner already registered this user — just continue to
                # the welcome message below instead of crashing the update.
                await db.rollback()

    if config.welcome_image.exists():
        await message.answer_photo(FSInputFile(str(config.welcome_image)),
                                   caption=WELCOME_TEXT, parse_mode="HTML", reply_markup=kb_main())
    else:
        await message.answer(WELCOME_TEXT, parse_mode="HTML", reply_markup=kb_main())
    if first_time_admin:
        await message.answer(
            "👑 <b>Вы — администратор этого бота.</b>\n\n"
            f"<code>/pending</code> — заявки, ожидающие подтверждения условия "
            f"(«{_esc(CONDITION_LABEL)}»)\n\n"
            "Управление администраторами:\n"
            "<code>/addadmin ID</code> — добавить администратора\n"
            "<code>/removeadmin ID</code> — убрать администратора\n"
            "<code>/admins</code> — список администраторов",
            parse_mode="HTML"
        )


# ── MY LINK ───────────────────────────────────────────────────────────────────

@router.message(F.text == "🔗 Моя ссылка")
async def show_my_link(msg: Message, config: ReferralConfig):
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        participant = await (await db.execute(
            "SELECT ref_code, balance FROM participants WHERE user_id=?", (msg.from_user.id,)
        )).fetchone()
        if not participant:
            await msg.answer("Сначала отправьте /start.")
            return
        counts = dict(await (await db.execute(
            "SELECT status, COUNT(*) FROM referrals WHERE referrer_id=? GROUP BY status", (msg.from_user.id,)
        )).fetchall())

    pending = counts.get("pending", 0)
    rewarded = counts.get("rewarded", 0)
    bot_user = await msg.bot.get_me()
    link = f"https://t.me/{bot_user.username}?start={participant['ref_code']}"
    await msg.answer(
        f"🔗 <b>Ваша реферальная ссылка:</b>\n{_esc(link)}\n\n"
        f"Код: <code>{participant['ref_code']}</code>\n\n"
        f"👥 Приглашено: {pending + rewarded}\n"
        f"⏳ Ожидает подтверждения: {pending}\n"
        f"✅ Награждено: {rewarded}\n\n"
        f"💰 Баланс: <b>{participant['balance']}</b> {_esc(REWARD_UNIT_LABEL)}",
        parse_mode="HTML"
    )


# ── LEADERBOARD ────────────────────────────────────────────────────────────────

@router.message(F.text == "🏆 Лидерборд")
async def show_leaderboard(msg: Message, config: ReferralConfig):
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT p.full_name, p.user_id, COUNT(*) AS c FROM referrals r "
            "JOIN participants p ON p.user_id = r.referrer_id "
            "WHERE r.status='rewarded' GROUP BY r.referrer_id ORDER BY c DESC LIMIT 10"
        )).fetchall()
    if not rows:
        await msg.answer("Пока никто не привёл ни одного успешного реферала.")
        return
    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 <b>Лидерборд по успешным приглашениям:</b>\n"]
    for i, r in enumerate(rows):
        prefix = medals[i] if i < 3 else f"{i + 1}."
        name = _esc(r["full_name"]) if r["full_name"] else f"Участник {r['user_id']}"
        lines.append(f"{prefix} {name} — {r['c']}")
    await msg.answer("\n".join(lines), parse_mode="HTML")


# ── ADMIN: pending referrals + reward confirmation ────────────────────────────

@router.message(Command("pending"))
async def cmd_pending(msg: Message, config: ReferralConfig):
    if not _is_admin(msg.from_user.id, config):
        await msg.answer("⛔ Нет доступа"); return
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT r.id, ref.full_name AS referrer_name, ref.user_id AS referrer_user_id, "
            "rd.full_name AS referred_name, rd.user_id AS referred_user_id "
            "FROM referrals r "
            "JOIN participants ref ON ref.user_id = r.referrer_id "
            "JOIN participants rd ON rd.user_id = r.referred_id "
            "WHERE r.status='pending' ORDER BY r.created_at LIMIT 20"
        )).fetchall()
    if not rows:
        await msg.answer("Нет заявок, ожидающих подтверждения."); return
    btns = []
    for r in rows:
        referrer_label = r["referrer_name"] or f"ID {r['referrer_user_id']}"
        referred_label = r["referred_name"] or f"ID {r['referred_user_id']}"
        label = f"{referred_label} ← {referrer_label}"
        short = label if len(label) <= 50 else label[:47] + "…"
        btns.append([InlineKeyboardButton(text=short, callback_data=f"ref_confirm:{r['id']}")])
    await msg.answer(
        f"⏳ <b>Заявки на подтверждение</b> («{_esc(CONDITION_LABEL)}»):",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=btns)
    )

@router.callback_query(F.data.startswith("ref_confirm:"))
async def cb_confirm(cb: CallbackQuery, bot: Bot, config: ReferralConfig):
    if not _is_admin(cb.from_user.id, config):
        await cb.answer("⛔ Нет доступа", show_alert=True); return
    await cb.answer()
    try:
        # callback_data is attacker-influenced (any Telegram client can send
        # arbitrary callback_query.data for a button it was never shown), so
        # a non-numeric or absurdly large id must not crash the handler.
        referral_id = int(cb.data.split(":", 1)[1])
    except (IndexError, ValueError):
        try:
            await cb.message.edit_text("Некорректная заявка.")
        except Exception:
            pass
        return

    async with aiosqlite.connect(config.db_path) as db:
        # Atomic status-guarded UPDATE — protects against a double-tap (two
        # admins, or the same admin twice) crediting the referrer's balance
        # twice for the same referral, same technique as inventory.py's
        # confirm guard but enforced at the DB row level instead of in-memory
        # FSM data, since this button has no per-user FSM state to check.
        try:
            cur = await db.execute(
                "UPDATE referrals SET status='rewarded', rewarded_at=datetime('now','localtime') "
                "WHERE id=? AND status='pending'",
                (referral_id,)
            )
        except OverflowError:
            # referral_id too large for SQLite's 64-bit INTEGER bind.
            try:
                await cb.message.edit_text("Некорректная заявка.")
            except Exception:
                pass
            return
        if cur.rowcount == 0:
            await db.commit()
            try:
                await cb.message.edit_text("Эта заявка уже обработана.")
            except Exception:
                pass
            return

        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            "SELECT r.referrer_id, rd.full_name AS referred_name FROM referrals r "
            "JOIN participants rd ON rd.user_id = r.referred_id WHERE r.id=?",
            (referral_id,)
        )).fetchone()
        referrer_id, referred_name = row["referrer_id"], row["referred_name"]
        await db.execute(
            "UPDATE participants SET balance = balance + ? WHERE user_id=?",
            (REWARD_POINTS, referrer_id)
        )
        await db.commit()

    try:
        await cb.message.edit_text(f"✅ Подтверждено: {referred_name or f'ID {referral_id}'}")
    except Exception:
        pass
    try:
        await bot.send_message(
            referrer_id,
            f"🎉 Ваш реферал выполнил условие! Начислено <b>{REWARD_POINTS}</b> {_esc(REWARD_UNIT_LABEL)}.",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.warning(f"reward notification to referrer {referrer_id} failed: {e}")


# ── ADMIN COMMANDS ────────────────────────────────────────────────────────────

@router.message(Command("addadmin"))
async def cmd_addadmin(msg: Message, config: ReferralConfig):
    if not _is_admin(msg.from_user.id, config): await msg.answer("⛔ Нет доступа"); return
    parts = msg.text.split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit(): await msg.answer("Использование: /addadmin <id>"); return
    ids = _load_admins(config.admins_file); ids.add(parts[1]); _save_admins(config.admins_file, ids)
    if config.bot_id is not None:
        try:
            await add_bot_admin(config.bot_id, parts[1])
        except Exception as e:
            logger.warning(f"cmd_addadmin: add_bot_admin sync failed for bot {config.bot_id}: {e}")
    await msg.answer(f"✅ <code>{parts[1]}</code> добавлен.", parse_mode="HTML")

@router.message(Command("removeadmin"))
async def cmd_removeadmin(msg: Message, config: ReferralConfig):
    if not _is_admin(msg.from_user.id, config): await msg.answer("⛔ Нет доступа"); return
    parts = msg.text.split()
    if len(parts) < 2: await msg.answer("Использование: /removeadmin <id>"); return
    ids = _load_admins(config.admins_file)
    if parts[1] in ids and len(ids) == 1:
        # An emptied admins file is indistinguishable from "never bootstrapped"
        # (see cmd_start's first_time_admin check) — removing the last admin
        # would hand admin rights to whoever sends /start next.
        await msg.answer("⚠️ Нельзя удалить последнего администратора."); return
    ids.discard(parts[1]); _save_admins(config.admins_file, ids)
    if config.bot_id is not None:
        try:
            await remove_bot_admin(config.bot_id, parts[1])
        except Exception as e:
            logger.warning(f"cmd_removeadmin: remove_bot_admin sync failed for bot {config.bot_id}: {e}")
    await msg.answer(f"✅ <code>{_esc(parts[1])}</code> удалён.", parse_mode="HTML")

@router.message(Command("admins"))
async def cmd_admins(msg: Message, config: ReferralConfig):
    if not _is_admin(msg.from_user.id, config): await msg.answer("⛔ Нет доступа"); return
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
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
