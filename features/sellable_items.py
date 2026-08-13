# FEATURE: sellable_items
# COMPATIBLE_WITH: accountant, booking_beauty, booking_fitness, booking_medical, campaign_tracker, event_manager, inventory, manager_secretary, moderator, orders_tracker, referral_program, shop_catalog, tour_operator, tourist_documents, trip_manager
"""Reusable "sellable items" catalog feature — a bot owner's own configurable
list of named/priced/described things to sell, layered ENTIRELY on top of
features/payments.py (see sellable-items-inventory for the design). Unlike
payments.py itself (deliberately opaque about WHAT is being paid for) and
sheets.py (no UI of its own at all), this is the first feature module with
its own interactive, button-driven UI living inside a tenant bot — no
templates/*.py file is touched to get there.

Entry point is a single admin-gated command, `/items` (see `bot_commands`
below — runtime/registry.py's _load_and_include_features() registers it via
bot.set_my_commands() so it shows up in Telegram's native "/" menu without
any host template needing a button for it). The SAME command branches by
_is_bot_admin(): the bot's own admin gets the management panel (add/edit/
hide positions), anyone else gets the buy-capable storefront.

Depends on features/payments.py's create_invoice() for the actual charge —
this module never touches bot_payment_providers or Telegram's
pre_checkout_query/successful_payment itself. It deliberately does NOT
register its own successful_payment handler: that event is already claimed
by payments.router (same Dispatcher, same event) and aiogram stops
propagating an update at the first handler that doesn't skip it, so a second
handler on the same event would simply never fire. Which item a completed
payment was for is recoverable, read-only, straight out of payments.py's own
`payments` table by parsing the `sellable_item:<item_id>:<nonce>`
invoice_payload this module writes when it creates the invoice.

Hard runtime dependency: "payments" must ALSO be enabled in bot_features for
the same bot, or nothing here can actually charge anyone — see cb_buy's
explicit get_bot_features() guard. Enabling "payments" only wires up
pre_checkout_query/successful_payment handling; it does not gate
create_invoice() itself (that only checks bot_payment_providers), so without
this guard a bot could send an invoice with literally nobody listening for
Telegram's pre_checkout_query, which Telegram then auto-fails after its 10s
timeout — the guard here is what stops that failure mode before it starts.

Second review pass (sellable-items-incomplete-review) ran the 6 categories
the first pass's session-limit failure had left uncovered — security,
async-pooling, state-db, monkey-tester, devops-logs, clean-code — and found
real issues fixed in this revision: Telegram's actual sendInvoice/
LabeledPrice length limits are tighter than this file's own NAME_MAX_LEN/
DESCRIPTION_MAX_LEN (see INVOICE_*_MAX_LEN); every intermediate prompt in
the multi-step "add item" flow was sent as a fresh, untracked message
instead of going through _replace_panel, guaranteeing an orphaned message
per step on every normal use (see the panel-replace calls throughout
adm_add_name/cb_adm_add_desc_skip/adm_add_description/adm_add_price/
adm_edit_value below); a double-submitted price message could create two
items, and two quick taps on different "edit field" buttons could race
edit_item_id/edit_field and silently misroute a typed value to the wrong
field (see _busy_admin_actions); this db_path never set WAL/busy_timeout or
caught sqlite3.Error the way payments.py does; every `int(cb.data.split(...))`
was unguarded, able to raise before cb.answer() had run.
"""
from __future__ import annotations

import html
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Protocol
from uuid import uuid4

import aiosqlite
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, Message,
)

from db.database import get_bot_features
from features.payments import create_invoice

logger = logging.getLogger(__name__)

router = Router()

# Consumed by runtime/registry.py's _load_and_include_features() — see its
# own docstring for why this is a list of (command, description) tuples
# collected across every enabled feature rather than each feature calling
# bot.set_my_commands() itself.
bot_commands: list[tuple[str, str]] = [("items", "Мои позиции для продажи")]

# Same bounds/format as templates/shop_catalog.py's own product fields — no
# reason for a client-facing "priced thing with a name" to look any
# different depending on which feature created it.
NAME_MAX_LEN = 100
DESCRIPTION_MAX_LEN = 1000
PRICE_MAX = 10_000_000  # целые рубли, без копеек — как и в shop_catalog
CURRENCY = "RUB"
CURRENCY_SYMBOL = "₽"

# Telegram's OWN sendInvoice/LabeledPrice limits — much tighter than
# NAME_MAX_LEN/DESCRIPTION_MAX_LEN above (which only bound what's stored and
# shown in this bot's own messages/buttons). Security review found that
# without truncating specifically for the invoice call, any item whose name
# is 33-100 chars or description 256-1000 chars would make create_invoice()
# raise TelegramBadRequest on EVERY purchase attempt — a self-inflicted "this
# item is permanently unbuyable" bug the admin has no way to notice until a
# client actually tries to buy it. See cb_buy / _truncate_utf16.
INVOICE_TITLE_MAX_LEN = 32
INVOICE_LABEL_MAX_LEN = 32
INVOICE_DESCRIPTION_MAX_LEN = 255

# Same rationale as shop_catalog.py's FLOW_TIMEOUT_SECONDS: MemoryStorage
# keeps FSM state until explicitly cleared, so an admin who opens "Добавить
# позицию" and goes quiet must not have every later message silently
# swallowed by a stale flow step forever.
FLOW_TIMEOUT_SECONDS = 300

# Repeated literals pulled into constants (clean-code review) — used
# throughout the admin flow below.
ADMIN_PANEL_TITLE = "💰 <b>Управление позициями</b>"
ITEM_NOT_FOUND_TEXT = f"Позиция не найдена — возможно, уже удалена.\n\n{ADMIN_PANEL_TITLE}"
FLOW_EXPIRED_TEXT = "⏳ Время ожидания истекло — начните заново из /items."
FLOW_CORRUPTED_TEXT = "⚠️ Что-то пошло не так — начните заново из /items."
STOREFRONT_TITLE = "🛍 <b>Доступные позиции</b>"
SAVE_FAILED_TEXT = "⚠️ Не удалось сохранить изменения. Попробуйте ещё раз из /items."


class SellableItemsConfig(Protocol):
    """Any template Config with db_path + admins_file works — duck-typed, same
    convention as payments.py's PaymentsConfig. Verified uniform (same
    {"ids": [...]} JSON shape) across all 15 templates in COMPATIBLE_WITH
    above; admins_file itself is `str` on tour_operator.py and `Path`
    everywhere else, both accepted since Path(admins_file) below works for
    either."""
    db_path: str
    admins_file: Path | str


# ── small helpers (duplicated on purpose — no shared features/_common.py
# exists yet in this project; every template independently defines the same
# _esc/_short/_load_admins helpers, this follows the same convention) ───────

def _esc(value, max_len: int = 500) -> str:
    text = str(value) if value is not None else ""
    if len(text) > max_len:
        text = text[:max_len] + "…"
    return html.escape(text)


def _short(label: str, max_len: int = 60) -> str:
    return label if len(label) <= max_len else label[:max_len - 1] + "…"


def _truncate_utf16(text: str, max_units: int) -> str:
    """Telegram's own sendInvoice/LabeledPrice length limits are counted in
    UTF-16 code units, not Python codepoints (monkey-testing review) — a
    name built mostly from astral-plane emoji (2 UTF-16 units each in
    Telegram's counting, 1 Python codepoint each) can pass a plain
    len()-based truncation like _short() while still exceeding Telegram's
    real limit, defeating INVOICE_*_MAX_LEN's entire purpose. Truncates by
    encoding to UTF-16 first so the result is guaranteed to fit."""
    encoded = text.encode("utf-16-le")
    if len(encoded) <= max_units * 2:
        return text
    truncated = encoded[: (max_units - 1) * 2]
    # A cut mid-surrogate-pair (mid-emoji) would raise on decode — drop the
    # dangling lone surrogate instead of crashing.
    return truncated.decode("utf-16-le", errors="ignore") + "…"


def _load_admins(admins_file: Path | str) -> set[str]:
    try:
        return set(json.loads(Path(admins_file).read_text()).get("ids", []))
    except Exception as e:
        # Devops-logs review: this used to fail silently — if admins_file is
        # ever corrupted/unreadable (disk issue, concurrent-write race), the
        # bot's own admin would be silently locked out of the management
        # panel (falls back to the client storefront) with zero trace in the
        # logs explaining why. Still returns an empty set (same fail-closed
        # behavior — never treat unreadable-admins as "everyone is admin"),
        # just no longer silent about it.
        logger.warning(f"_load_admins: failed to read {admins_file!r}: {e}")
        return set()


def _is_bot_admin(user_id: int, config: SellableItemsConfig) -> bool:
    return str(user_id) in _load_admins(config.admins_file)


def _parse_price(text: str) -> int | None:
    """Same guard shape as templates/orders_tracker.py's _parse_price — bounds
    length before treating input as numeric and rejects non-ASCII look-alike
    digits, so e.g. full-width digits can't sneak past int()."""
    text = text.strip().replace(" ", "")
    if not (text.isascii() and text.isdigit() and 0 < len(text) <= 9):
        return None
    value = int(text)
    return value if 1 <= value <= PRICE_MAX else None


def _parse_item_id(cb_data: str) -> int | None:
    """Every selitem_*:<item_id>[:...] callback needs this. Monkey-testing
    review: a forged or stale callback_data with a non-numeric or missing id
    segment must not raise an unhandled ValueError out of the handler — for
    cb_view/cb_buy in particular that would happen BEFORE cb.answer() has
    run, leaving the tap's spinner stuck on the caller's client until
    Telegram's own timeout. Callers get None back and answer the callback
    gracefully instead."""
    try:
        return int(cb_data.split(":", 1)[1])
    except (IndexError, ValueError):
        return None


# In-memory guard against the SAME admin racing two of their own concurrent
# taps/messages against this feature's shared FSMContext for one bot
# (async-pooling + state-db review): aiogram's default MemoryStorage has no
# atomic read-modify-write across concurrent handler invocations, so two
# near-simultaneous updates (double-tap, a flaky client redelivering, two
# devices) can both pass their own validation before either commits its
# state change. Confirmed reachable: (a) a double-submitted price message
# could create TWO items for one "add" submission (adm_add_price), (b) two
# quick taps on DIFFERENT "edit field" buttons could race
# edit_item_id/edit_field, silently routing the next typed value to the
# WRONG field (cb_adm_field). Same in-memory busy-set pattern as
# handlers/manage_bots.py's _busy_bots — a plain set's
# membership-check-then-add is atomic under asyncio's cooperative scheduling
# as long as no `await` runs between them, which holds everywhere below.
_busy_admin_actions: set[tuple[str, int]] = set()


def _admin_busy_key(config: SellableItemsConfig, user_id: int) -> tuple[str, int]:
    return (str(config.db_path), user_id)


# ── db ────────────────────────────────────────────────────────────────────

async def init_db(db_path: str) -> None:
    """Adds bot_sellable_items to the host template's OWN db_path — same
    convention as payments.py's init_payments_tables: this module never opens
    its own database file. No bot_id column — isolation between bots already
    comes from one db_path per bot, same as every other feature/template
    table in this project.

    Sets journal_mode=WAL for the same reason payments.py does — state-db/
    async-pooling review found this was the one write path sharing this
    db_path (once "payments" is also enabled, which cb_buy already requires
    for purchases) that did NOT set WAL, making a concurrent write (e.g. an
    admin hiding an item at the same moment a payment is being recorded)
    more likely to surface as an unhandled "database is locked" instead of
    the sqlite3.Error the write helpers below now explicitly catch."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bot_sellable_items (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                description TEXT,
                price       INTEGER NOT NULL CHECK(price > 0),
                active      INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
                created_at  TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.commit()


async def _all_items(db_path: str) -> list[dict]:
    """Admin-facing: every item regardless of active flag, newest first (an
    admin looking for "the thing I just added" wants it on top)."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT * FROM bot_sellable_items ORDER BY id DESC"
        )).fetchall()
        return [dict(r) for r in rows]


async def _active_items(db_path: str) -> list[dict]:
    """Client-facing: only what's actually for sale."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT * FROM bot_sellable_items WHERE active=1 ORDER BY name"
        )).fetchall()
        return [dict(r) for r in rows]


async def _item_row(db_path: str, item_id: int) -> dict | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            "SELECT * FROM bot_sellable_items WHERE id=?", (item_id,)
        )).fetchone()
        return dict(row) if row else None


async def _create_item(db_path: str, name: str, description: str | None, price: int) -> int:
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout=5000")
        try:
            cur = await db.execute(
                "INSERT INTO bot_sellable_items (name, description, price) VALUES (?,?,?)",
                (name, description, price),
            )
            await db.commit()
        except sqlite3.Error:
            # Same posture as features/payments.py's on_successful_payment:
            # log with enough context to diagnose, then re-raise so the
            # caller can tell the admin something went wrong instead of the
            # write silently vanishing (state-db/async-pooling review — this
            # db_path has no WAL/busy_timeout guarantee before init_db ran,
            # and even with it, a genuinely full disk etc. can still fail).
            logger.error(f"_create_item: FAILED to insert name={name!r} price={price}", exc_info=True)
            raise
        return cur.lastrowid


_EDITABLE_FIELDS = ("name", "description", "price")


async def _update_item_field(db_path: str, item_id: int, field: str, value) -> None:
    # field is interpolated into SQL below — never accept anything outside
    # this fixed allowlist, even though every caller in this file already
    # only ever passes one of these three literals (defense in depth: this
    # function has no way to know a future caller won't pass through raw
    # callback_data unchecked).
    if field not in _EDITABLE_FIELDS:
        raise ValueError(f"_update_item_field: unexpected field {field!r}")
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout=5000")
        try:
            await db.execute(f"UPDATE bot_sellable_items SET {field}=? WHERE id=?", (value, item_id))
            await db.commit()
        except sqlite3.Error:
            logger.error(f"_update_item_field: FAILED item_id={item_id} field={field!r}", exc_info=True)
            raise


async def _toggle_item_active(db_path: str, item_id: int) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout=5000")
        try:
            await db.execute("UPDATE bot_sellable_items SET active = 1 - active WHERE id=?", (item_id,))
            await db.commit()
        except sqlite3.Error:
            logger.error(f"_toggle_item_active: FAILED item_id={item_id}", exc_info=True)
            raise


# ── FSM ───────────────────────────────────────────────────────────────────

class AdminFlow(StatesGroup):
    add_name = State()
    add_description = State()
    add_price = State()
    edit_value = State()


def _flow_expired(data: dict) -> bool:
    started_at = data.get("started_at")
    return started_at is None or (time.time() - started_at) > FLOW_TIMEOUT_SECONDS


# ── panel navigation (same pattern as shop_catalog.py/moderator.py) ────────

async def _replace_panel(
    bot: Bot, state: FSMContext, chat_id: int, text: str,
    reply_markup: InlineKeyboardMarkup | None = None, parse_mode: str | None = "HTML",
) -> None:
    data = await state.get_data()
    prev_id = data.get("panel_msg_id")
    if prev_id:
        try:
            await bot.delete_message(chat_id, prev_id)
        except Exception as e:
            logger.debug(f"_replace_panel: failed to delete old panel {prev_id} in chat {chat_id}: {e}")
        finally:
            # Cleared unconditionally (delete succeeded OR failed — e.g. the
            # message was already gone) so a LATER send_message failure below
            # doesn't leave a stale panel_msg_id behind: review found the next
            # _replace_panel call would otherwise keep retrying to delete the
            # same already-gone message on every subsequent admin action.
            await state.update_data(panel_msg_id=None)
    try:
        msg = await bot.send_message(chat_id, text, parse_mode=parse_mode, reply_markup=reply_markup)
    except Exception as e:
        logger.warning(f"_replace_panel: failed to send panel to chat {chat_id}: {e}")
        return
    await state.update_data(panel_msg_id=msg.message_id)


async def _clear_flow_keep_panel(state: FSMContext) -> None:
    data = await state.get_data()
    panel_msg_id = data.get("panel_msg_id")
    await state.clear()
    if panel_msg_id is not None:
        await state.update_data(panel_msg_id=panel_msg_id)


def _cb_chat_id(cb: CallbackQuery) -> int | None:
    """Same defensive guard as templates/shop_catalog.py's own
    _panel_chat_id(): CallbackQuery.message is typed as optional, and even
    when present can be an aiogram InaccessibleMessage (a >48h-old or
    otherwise no-longer-editable message Telegram stops returning full data
    for) rather than a real Message. `.chat` is present on both, so reading
    it here is safe either way — callers just need a chat_id to fall back to
    bot.send_message()/bot.delete_message() instead of message-bound
    .edit_text()/.answer(), which InaccessibleMessage does NOT have."""
    return cb.message.chat.id if cb.message else None


async def _edit_or_send(
    cb: CallbackQuery, text: str, reply_markup: InlineKeyboardMarkup | None = None, parse_mode: str | None = "HTML",
) -> None:
    """Client-storefront screens edit the callback's own message in place
    when possible (no message spam), but fall back to sending a fresh
    message via the bot-level API when cb.message isn't a real, editable
    Message — an InaccessibleMessage (see _cb_chat_id) has no .edit_text()
    at all, and Telegram can also reject an edit outright (e.g. "message is
    not modified" or the message was deleted between the tap and this call)."""
    chat_id = _cb_chat_id(cb)
    if chat_id is None:
        return
    if isinstance(cb.message, Message):
        try:
            await cb.message.edit_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
            return
        except Exception as e:
            logger.debug(f"_edit_or_send: edit_text failed, falling back to a fresh message: {e}")
    try:
        await cb.bot.send_message(chat_id, text, parse_mode=parse_mode, reply_markup=reply_markup)
    except Exception as e:
        logger.warning(f"_edit_or_send: failed to send fallback message to chat {chat_id}: {e}")


# ── keyboards: admin ─────────────────────────────────────────────────────

def kb_admin_items(items: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for it in items:
        icon = "✅" if it["active"] else "🚫"
        label = _short(f"{icon} {it['name']} — {it['price']}{CURRENCY_SYMBOL}")
        rows.append([InlineKeyboardButton(text=label, callback_data=f"selitem_adm_edit:{it['id']}")])
    rows.append([InlineKeyboardButton(text="➕ Добавить позицию", callback_data="selitem_adm_add")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _item_detail_text(item: dict, *, prefix: str = "") -> str:
    status = "✅ активна" if item["active"] else "🚫 скрыта"
    description = _esc(item["description"], DESCRIPTION_MAX_LEN) if item["description"] else "—"
    return (
        f"{prefix}💰 <b>{_esc(item['name'])}</b>\n\n"
        f"{description}\n\n"
        f"Цена: {item['price']}{CURRENCY_SYMBOL}\n"
        f"Статус: {status}"
    )


def kb_item_edit_menu(item: dict) -> InlineKeyboardMarkup:
    toggle_label = "🚫 Скрыть" if item["active"] else "✅ Показать"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Название", callback_data=f"selitem_adm_field:{item['id']}:name")],
        [InlineKeyboardButton(text="📝 Описание", callback_data=f"selitem_adm_field:{item['id']}:description")],
        [InlineKeyboardButton(text="💰 Цена", callback_data=f"selitem_adm_field:{item['id']}:price")],
        [InlineKeyboardButton(text=toggle_label, callback_data=f"selitem_adm_toggle:{item['id']}")],
        [InlineKeyboardButton(text="◀️ К списку", callback_data="selitem_adm_list")],
    ])


def kb_cancel(callback_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data=callback_data)]])


def kb_skip_cancel(skip_cb: str, cancel_cb: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏩ Пропустить", callback_data=skip_cb)],
        [InlineKeyboardButton(text="❌ Отмена", callback_data=cancel_cb)],
    ])


# ── keyboards: client storefront ────────────────────────────────────────

def kb_storefront(items: list[dict]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text=_short(f"{it['name']} — {it['price']}{CURRENCY_SYMBOL}"),
            callback_data=f"selitem_view:{it['id']}",
        )] for it in items
    ]
    if not rows:
        rows.append([InlineKeyboardButton(text="Пока нет доступных позиций", callback_data="selitem_noop")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_item_detail(item_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Купить", callback_data=f"selitem_buy:{item_id}")],
        [InlineKeyboardButton(text="◀️ К списку", callback_data="selitem_list")],
    ])


# ── /items entry point ──────────────────────────────────────────────────

@router.message(Command("items"), F.chat.type == "private")
async def cmd_items(message: Message, bot: Bot, state: FSMContext, config: SellableItemsConfig) -> None:
    # _clear_flow_keep_panel, NOT a bare state.clear() — monkey-testing
    # review found a bare clear() here throws away panel_msg_id from any
    # PRIOR abandoned flow (went silent mid "Добавить позицию", came back
    # later, hit /items) before _replace_panel below gets a chance to read
    # and delete it — leaving that stale prompt as a permanent orphan.
    await _clear_flow_keep_panel(state)
    if _is_bot_admin(message.from_user.id, config):
        items = await _all_items(config.db_path)
        await _replace_panel(bot, state, message.chat.id, ADMIN_PANEL_TITLE, reply_markup=kb_admin_items(items))
    else:
        items = await _active_items(config.db_path)
        try:
            await message.answer(STOREFRONT_TITLE, parse_mode="HTML", reply_markup=kb_storefront(items))
        except Exception as e:
            # Monkey-testing review: this was the one outbound call in the
            # whole file with zero exception handling.
            logger.warning(f"cmd_items: failed to send storefront to chat {message.chat.id}: {e}")


# ── admin: list / add ────────────────────────────────────────────────────

async def cb_adm_list(cb: CallbackQuery, bot: Bot, state: FSMContext, config: SellableItemsConfig) -> None:
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    chat_id = _cb_chat_id(cb)
    if chat_id is None:
        return
    await _clear_flow_keep_panel(state)
    items = await _all_items(config.db_path)
    await _replace_panel(bot, state, chat_id, ADMIN_PANEL_TITLE, reply_markup=kb_admin_items(items))


# "◀️ К списку" and "❌ Отмена" render an identical screen — clean-code
# review found these were two byte-for-byte identical handler bodies under
# different names; registered once, under both callback_data values.
router.callback_query.register(cb_adm_list, F.data == "selitem_adm_list")
router.callback_query.register(cb_adm_list, F.data == "selitem_adm_cancel")


@router.callback_query(F.data == "selitem_adm_add")
async def cb_adm_add(cb: CallbackQuery, bot: Bot, state: FSMContext, config: SellableItemsConfig) -> None:
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    chat_id = _cb_chat_id(cb)
    if chat_id is None:
        return
    await _replace_panel(bot, state, chat_id, "Введите название позиции:", reply_markup=kb_cancel("selitem_adm_cancel"))
    await state.update_data(started_at=time.time())
    await state.set_state(AdminFlow.add_name)


@router.message(AdminFlow.add_name, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def adm_add_name(message: Message, bot: Bot, state: FSMContext, config: SellableItemsConfig) -> None:
    data = await state.get_data()
    chat_id = message.chat.id
    if _flow_expired(data):
        await _clear_flow_keep_panel(state)
        await _replace_panel(bot, state, chat_id, FLOW_EXPIRED_TEXT)
        return
    if not _is_bot_admin(message.from_user.id, config):
        await _clear_flow_keep_panel(state)
        return
    name = message.text.strip()[:NAME_MAX_LEN]
    if not name:
        # Re-prompt through _replace_panel (not a bare reply) — monkey-testing
        # review found every intermediate step in this flow used to send an
        # untracked message, orphaning one bot message per step forever.
        await _replace_panel(
            bot, state, chat_id, "Название не может быть пустым. Введите название позиции:",
            reply_markup=kb_cancel("selitem_adm_cancel"),
        )
        return
    await state.update_data(new_name=name)
    await state.set_state(AdminFlow.add_description)
    await _replace_panel(
        bot, state, chat_id, "Введите описание позиции (или нажмите «Пропустить»):",
        reply_markup=kb_skip_cancel("selitem_adm_desc_skip", "selitem_adm_cancel"),
    )


@router.callback_query(AdminFlow.add_description, F.data == "selitem_adm_desc_skip")
async def cb_adm_add_desc_skip(cb: CallbackQuery, bot: Bot, state: FSMContext, config: SellableItemsConfig) -> None:
    await cb.answer()
    chat_id = _cb_chat_id(cb)
    if chat_id is None:
        return
    data = await state.get_data()
    if _flow_expired(data):
        await _clear_flow_keep_panel(state)
        await _replace_panel(bot, state, chat_id, FLOW_EXPIRED_TEXT)
        return
    if not _is_bot_admin(cb.from_user.id, config):
        # Same cleanup every other admin-flow handler in this file does on a
        # non-admin hit — clean-code review found this one branch was
        # missing it (near-zero real impact since a non-admin can't normally
        # reach this state, but inconsistent).
        await _clear_flow_keep_panel(state)
        return
    await state.update_data(new_description=None)
    await state.set_state(AdminFlow.add_price)
    await _replace_panel(bot, state, chat_id, "Введите цену в рублях (целое число):", reply_markup=kb_cancel("selitem_adm_cancel"))


@router.message(AdminFlow.add_description, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def adm_add_description(message: Message, bot: Bot, state: FSMContext, config: SellableItemsConfig) -> None:
    data = await state.get_data()
    chat_id = message.chat.id
    if _flow_expired(data):
        await _clear_flow_keep_panel(state)
        await _replace_panel(bot, state, chat_id, FLOW_EXPIRED_TEXT)
        return
    if not _is_bot_admin(message.from_user.id, config):
        await _clear_flow_keep_panel(state)
        return
    description = message.text.strip()[:DESCRIPTION_MAX_LEN]
    await state.update_data(new_description=description or None)
    await state.set_state(AdminFlow.add_price)
    await _replace_panel(bot, state, chat_id, "Введите цену в рублях (целое число):", reply_markup=kb_cancel("selitem_adm_cancel"))


@router.message(AdminFlow.add_price, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def adm_add_price(message: Message, bot: Bot, state: FSMContext, config: SellableItemsConfig) -> None:
    data = await state.get_data()
    chat_id = message.chat.id
    if _flow_expired(data):
        await _clear_flow_keep_panel(state)
        await _replace_panel(bot, state, chat_id, FLOW_EXPIRED_TEXT)
        return
    if not _is_bot_admin(message.from_user.id, config):
        await _clear_flow_keep_panel(state)
        return
    price = _parse_price(message.text)
    if price is None:
        await _replace_panel(
            bot, state, chat_id, f"Введите целое число от 1 до {PRICE_MAX}.",
            reply_markup=kb_cancel("selitem_adm_cancel"),
        )
        return
    name = data.get("new_name")
    if not name:
        # Unreachable through the normal flow order (name is always collected
        # first), but a corrupted/tampered FSM data dict must not silently
        # create a nameless item — same defensive stance as the field
        # allowlist in _update_item_field.
        await _clear_flow_keep_panel(state)
        await _replace_panel(bot, state, chat_id, FLOW_CORRUPTED_TEXT)
        return
    description = data.get("new_description")

    guard_key = _admin_busy_key(config, message.from_user.id)
    if guard_key in _busy_admin_actions:
        # Same admin's price message arrived twice in quick succession
        # (double-send / flaky client redelivery) — the first invocation is
        # already creating the item; ignore this duplicate rather than
        # create a second row for one submission.
        return
    _busy_admin_actions.add(guard_key)
    try:
        await _create_item(config.db_path, name, description, price)
    except sqlite3.Error:
        await _clear_flow_keep_panel(state)
        await _replace_panel(bot, state, chat_id, SAVE_FAILED_TEXT)
        return
    finally:
        _busy_admin_actions.discard(guard_key)

    await _clear_flow_keep_panel(state)
    items = await _all_items(config.db_path)
    await _replace_panel(
        bot, state, chat_id,
        f"✅ Позиция «{_esc(name)}» добавлена.\n\n{ADMIN_PANEL_TITLE}",
        reply_markup=kb_admin_items(items),
    )


# ── admin: edit / hide ───────────────────────────────────────────────────

@router.callback_query(F.data.startswith("selitem_adm_edit:"))
async def cb_adm_edit(cb: CallbackQuery, bot: Bot, state: FSMContext, config: SellableItemsConfig) -> None:
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    chat_id = _cb_chat_id(cb)
    if chat_id is None:
        return
    item_id = _parse_item_id(cb.data)
    if item_id is None:
        return
    item = await _item_row(config.db_path, item_id)
    await _clear_flow_keep_panel(state)
    if item is None:
        items = await _all_items(config.db_path)
        await _replace_panel(bot, state, chat_id, ITEM_NOT_FOUND_TEXT, reply_markup=kb_admin_items(items))
        return
    await _replace_panel(bot, state, chat_id, _item_detail_text(item), reply_markup=kb_item_edit_menu(item))


@router.callback_query(F.data.startswith("selitem_adm_toggle:"))
async def cb_adm_toggle(cb: CallbackQuery, bot: Bot, state: FSMContext, config: SellableItemsConfig) -> None:
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    chat_id = _cb_chat_id(cb)
    if chat_id is None:
        return
    item_id = _parse_item_id(cb.data)
    if item_id is None:
        return
    try:
        await _toggle_item_active(config.db_path, item_id)
    except sqlite3.Error:
        items = await _all_items(config.db_path)
        await _replace_panel(bot, state, chat_id, SAVE_FAILED_TEXT, reply_markup=kb_admin_items(items))
        return
    item = await _item_row(config.db_path, item_id)
    if item is None:
        items = await _all_items(config.db_path)
        await _replace_panel(bot, state, chat_id, ADMIN_PANEL_TITLE, reply_markup=kb_admin_items(items))
        return
    await _replace_panel(bot, state, chat_id, _item_detail_text(item), reply_markup=kb_item_edit_menu(item))


@router.callback_query(F.data.startswith("selitem_adm_field:"))
async def cb_adm_field(cb: CallbackQuery, bot: Bot, state: FSMContext, config: SellableItemsConfig) -> None:
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    chat_id = _cb_chat_id(cb)
    if chat_id is None:
        return
    try:
        _, item_id_str, field = cb.data.split(":", 2)
        item_id = int(item_id_str)
    except ValueError:
        return
    if field not in _EDITABLE_FIELDS:
        return

    guard_key = _admin_busy_key(config, cb.from_user.id)
    if guard_key in _busy_admin_actions:
        # A second "edit a field" tap arrived while the first is still being
        # set up — letting both proceed can race edit_item_id/edit_field in
        # FSM state, silently routing the next typed value to a DIFFERENT
        # field than the one shown on screen (async-pooling review).
        return
    _busy_admin_actions.add(guard_key)
    try:
        item = await _item_row(config.db_path, item_id)
        if item is None:
            items = await _all_items(config.db_path)
            await _replace_panel(bot, state, chat_id, ITEM_NOT_FOUND_TEXT, reply_markup=kb_admin_items(items))
            return
        prompts = {
            "name": "Введите новое название:",
            "description": "Введите новое описание:",
            "price": "Введите новую цену в рублях (целое число):",
        }
        await state.update_data(started_at=time.time(), edit_item_id=item_id, edit_field=field)
        await state.set_state(AdminFlow.edit_value)
        await _replace_panel(bot, state, chat_id, prompts[field], reply_markup=kb_cancel(f"selitem_adm_edit:{item_id}"))
    finally:
        _busy_admin_actions.discard(guard_key)


@router.message(AdminFlow.edit_value, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def adm_edit_value(message: Message, bot: Bot, state: FSMContext, config: SellableItemsConfig) -> None:
    data = await state.get_data()
    chat_id = message.chat.id
    if _flow_expired(data):
        await _clear_flow_keep_panel(state)
        await _replace_panel(bot, state, chat_id, FLOW_EXPIRED_TEXT)
        return
    if not _is_bot_admin(message.from_user.id, config):
        await _clear_flow_keep_panel(state)
        return
    item_id = data.get("edit_item_id")
    field = data.get("edit_field")
    if item_id is None or field not in _EDITABLE_FIELDS:
        await _clear_flow_keep_panel(state)
        await _replace_panel(bot, state, chat_id, FLOW_CORRUPTED_TEXT)
        return

    if field == "price":
        value = _parse_price(message.text)
        if value is None:
            await _replace_panel(
                bot, state, chat_id, f"Введите целое число от 1 до {PRICE_MAX}.",
                reply_markup=kb_cancel(f"selitem_adm_edit:{item_id}"),
            )
            return
    elif field == "name":
        value = message.text.strip()[:NAME_MAX_LEN]
        if not value:
            await _replace_panel(
                bot, state, chat_id, "Название не может быть пустым. Введите новое название:",
                reply_markup=kb_cancel(f"selitem_adm_edit:{item_id}"),
            )
            return
    else:
        value = message.text.strip()[:DESCRIPTION_MAX_LEN]

    try:
        await _update_item_field(config.db_path, item_id, field, value)
    except sqlite3.Error:
        await _clear_flow_keep_panel(state)
        await _replace_panel(bot, state, chat_id, SAVE_FAILED_TEXT)
        return

    await _clear_flow_keep_panel(state)
    item = await _item_row(config.db_path, item_id)
    if item is None:
        items = await _all_items(config.db_path)
        await _replace_panel(bot, state, chat_id, ADMIN_PANEL_TITLE, reply_markup=kb_admin_items(items))
        return
    await _replace_panel(
        bot, state, chat_id, _item_detail_text(item, prefix="✅ Обновлено.\n\n"),
        reply_markup=kb_item_edit_menu(item),
    )


@router.message(StateFilter(AdminFlow), ~F.text)
async def adm_flow_non_text_input(message: Message) -> None:
    # Monkey-testing review: a photo/sticker/voice/document sent while
    # mid-flow matched no handler at all (every step above filters on
    # F.text) — the admin got zero feedback, just an unchanged screen, until
    # the 5-minute FLOW_TIMEOUT_SECONDS self-heal. This is a low-priority
    # catch-all (registered after every specific F.text handler above, so it
    # only ever matches when none of those did).
    await message.answer("Пожалуйста, отправьте текстом.")


# ── client storefront ────────────────────────────────────────────────────

@router.callback_query(F.data == "selitem_list")
async def cb_list(cb: CallbackQuery, config: SellableItemsConfig) -> None:
    await cb.answer()
    items = await _active_items(config.db_path)
    await _edit_or_send(cb, STOREFRONT_TITLE, reply_markup=kb_storefront(items))


@router.callback_query(F.data.startswith("selitem_view:"))
async def cb_view(cb: CallbackQuery, config: SellableItemsConfig) -> None:
    item_id = _parse_item_id(cb.data)
    if item_id is None:
        await cb.answer("Некорректная команда.", show_alert=True)
        return
    item = await _item_row(config.db_path, item_id)
    if item is None or not item["active"]:
        # Single cb.answer() per callback_query_id — same reasoning as
        # shop_catalog.py's cb_product_detail: Telegram rejects a second
        # answerCallbackQuery for the same id.
        await cb.answer("Позиция больше не доступна.", show_alert=True)
        return
    await cb.answer()
    description = _esc(item["description"], DESCRIPTION_MAX_LEN) if item["description"] else ""
    text = f"🛍 <b>{_esc(item['name'])}</b>\n\n{description}\n\n💰 <b>{item['price']}{CURRENCY_SYMBOL}</b>"
    await _edit_or_send(cb, text, reply_markup=kb_item_detail(item_id))


@router.callback_query(F.data == "selitem_noop")
async def cb_noop(cb: CallbackQuery) -> None:
    await cb.answer()


@router.callback_query(F.data.startswith("selitem_buy:"))
async def cb_buy(cb: CallbackQuery, bot_id: int, config: SellableItemsConfig) -> None:
    item_id = _parse_item_id(cb.data)
    if item_id is None:
        await cb.answer("Некорректная команда.", show_alert=True)
        return
    item = await _item_row(config.db_path, item_id)
    if item is None or not item["active"]:
        await cb.answer("Позиция больше не доступна.", show_alert=True)
        return
    if "payments" not in await get_bot_features(bot_id):
        # See module docstring: without "payments" also enabled, there is no
        # pre_checkout_query handler on this bot's Dispatcher at all — an
        # invoice sent anyway would just time out unanswered on Telegram's
        # side. Caught here, before anything is sent.
        logger.warning(
            f"cb_buy: bot_id={bot_id} item_id={item_id} — 'sellable_items' is enabled "
            "but 'payments' is not, refusing to send an invoice"
        )
        await cb.answer("Оплата временно недоступна — обратитесь к администратору.", show_alert=True)
        return
    chat_id = _cb_chat_id(cb)
    if chat_id is None:
        await cb.answer("Оплата временно недоступна — обратитесь к администратору.", show_alert=True)
        return
    await cb.answer()
    payload = f"sellable_item:{item_id}:{uuid4().hex[:8]}"
    # Truncated specifically for the invoice — see INVOICE_*_MAX_LEN above.
    # UTF-16-aware (_truncate_utf16, not _short) — see that helper's own
    # docstring for why a plain codepoint-count truncation isn't safe here.
    # The DB row itself keeps the admin's full name/description (up to
    # NAME_MAX_LEN/DESCRIPTION_MAX_LEN) for the storefront/admin panel; only
    # what goes into THIS Telegram API call is shortened.
    invoice_title = _truncate_utf16(item["name"], INVOICE_TITLE_MAX_LEN)
    invoice_description = _truncate_utf16(item["description"] or item["name"], INVOICE_DESCRIPTION_MAX_LEN)
    invoice_label = _truncate_utf16(item["name"], INVOICE_LABEL_MAX_LEN)
    try:
        await create_invoice(
            bot=cb.bot, bot_id=bot_id, chat_id=chat_id,
            title=invoice_title, description=invoice_description,
            payload=payload, currency=CURRENCY,
            prices=[LabeledPrice(label=invoice_label, amount=item["price"] * 100)],
        )
    except ValueError:
        # create_invoice's own guard: bot_payment_providers has no token for
        # this bot yet, even though "payments" is enabled in bot_features.
        # Logged with the same item_id/chat_id context every other branch
        # here has — devops-logs review found this was the one exception
        # branch that logged nothing at all.
        logger.warning(
            f"cb_buy: bot_id={bot_id} item_id={item_id} chat_id={chat_id} — no payment provider configured"
        )
        await cb.bot.send_message(chat_id, "⚠️ Оплата временно недоступна — обратитесь к администратору.")
    except TelegramAPIError:
        # Covers TelegramBadRequest as well as everything else Telegram's API
        # itself can raise around send_invoice — TelegramForbiddenError (the
        # buyer blocked the bot), TelegramRetryAfter (flood control),
        # TelegramNetworkError, etc. — all subclass TelegramAPIError.
        logger.warning(f"cb_buy: bot_id={bot_id} item_id={item_id} chat_id={chat_id} send_invoice failed", exc_info=True)
        await cb.bot.send_message(chat_id, "⚠️ Не удалось выставить счёт. Попробуйте ещё раз позже.")
    except Exception:
        # Review finding: anything NOT covered above (e.g. a sqlite3.Error
        # from get_bot_payment_provider() inside create_invoice) must not
        # leave the buyer staring at a spinner with silence — cb.answer()
        # above already fired, so this is the last chance to say anything.
        logger.exception(f"cb_buy: bot_id={bot_id} item_id={item_id} chat_id={chat_id} failed unexpectedly")
        await cb.bot.send_message(chat_id, "⚠️ Не удалось выставить счёт. Попробуйте ещё раз позже.")
