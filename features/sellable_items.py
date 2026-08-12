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
"""
from __future__ import annotations

import html
import json
import logging
import time
from pathlib import Path
from typing import Protocol
from uuid import uuid4

import aiosqlite
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
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

# Same rationale as shop_catalog.py's FLOW_TIMEOUT_SECONDS: MemoryStorage
# keeps FSM state until explicitly cleared, so an admin who opens "Добавить
# позицию" and goes quiet must not have every later message silently
# swallowed by a stale flow step forever.
FLOW_TIMEOUT_SECONDS = 300


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


def _load_admins(admins_file: Path | str) -> set[str]:
    try:
        return set(json.loads(Path(admins_file).read_text()).get("ids", []))
    except Exception:
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


# ── db ────────────────────────────────────────────────────────────────────

async def init_db(db_path: str) -> None:
    """Adds bot_sellable_items to the host template's OWN db_path — same
    convention as payments.py's init_payments_tables: this module never opens
    its own database file. No bot_id column — isolation between bots already
    comes from one db_path per bot, same as every other feature/template
    table in this project."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bot_sellable_items (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                description TEXT,
                price       INTEGER NOT NULL,
                active      INTEGER NOT NULL DEFAULT 1,
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
        cur = await db.execute(
            "INSERT INTO bot_sellable_items (name, description, price) VALUES (?,?,?)",
            (name, description, price),
        )
        await db.commit()
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
        await db.execute(f"UPDATE bot_sellable_items SET {field}=? WHERE id=?", (value, item_id))
        await db.commit()


async def _toggle_item_active(db_path: str, item_id: int) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute("UPDATE bot_sellable_items SET active = 1 - active WHERE id=?", (item_id,))
        await db.commit()


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
    await state.clear()
    if _is_bot_admin(message.from_user.id, config):
        items = await _all_items(config.db_path)
        await _replace_panel(
            bot, state, message.chat.id, "💰 <b>Управление позициями</b>", reply_markup=kb_admin_items(items),
        )
    else:
        items = await _active_items(config.db_path)
        await message.answer("🛍 <b>Доступные позиции</b>", parse_mode="HTML", reply_markup=kb_storefront(items))


# ── admin: list / add ────────────────────────────────────────────────────

@router.callback_query(F.data == "selitem_adm_list")
async def cb_adm_list(cb: CallbackQuery, bot: Bot, state: FSMContext, config: SellableItemsConfig) -> None:
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    chat_id = _cb_chat_id(cb)
    if chat_id is None:
        return
    await _clear_flow_keep_panel(state)
    items = await _all_items(config.db_path)
    await _replace_panel(bot, state, chat_id, "💰 <b>Управление позициями</b>", reply_markup=kb_admin_items(items))


@router.callback_query(F.data == "selitem_adm_cancel")
async def cb_adm_cancel(cb: CallbackQuery, bot: Bot, state: FSMContext, config: SellableItemsConfig) -> None:
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    chat_id = _cb_chat_id(cb)
    if chat_id is None:
        return
    await _clear_flow_keep_panel(state)
    items = await _all_items(config.db_path)
    await _replace_panel(bot, state, chat_id, "💰 <b>Управление позициями</b>", reply_markup=kb_admin_items(items))


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
async def adm_add_name(message: Message, state: FSMContext, config: SellableItemsConfig) -> None:
    data = await state.get_data()
    if _flow_expired(data):
        await _clear_flow_keep_panel(state)
        await message.answer("⏳ Время ожидания истекло — начните заново из /items.")
        return
    if not _is_bot_admin(message.from_user.id, config):
        await _clear_flow_keep_panel(state)
        return
    name = message.text.strip()[:NAME_MAX_LEN]
    if not name:
        await message.answer("Название не может быть пустым.")
        return
    await state.update_data(new_name=name)
    await state.set_state(AdminFlow.add_description)
    await message.answer(
        "Введите описание позиции (или нажмите «Пропустить»):",
        reply_markup=kb_skip_cancel("selitem_adm_desc_skip", "selitem_adm_cancel"),
    )


@router.callback_query(AdminFlow.add_description, F.data == "selitem_adm_desc_skip")
async def cb_adm_add_desc_skip(cb: CallbackQuery, state: FSMContext, config: SellableItemsConfig) -> None:
    await cb.answer()
    chat_id = _cb_chat_id(cb)
    if chat_id is None:
        return
    data = await state.get_data()
    if _flow_expired(data):
        await _clear_flow_keep_panel(state)
        await cb.bot.send_message(chat_id, "⏳ Время ожидания истекло — начните заново из /items.")
        return
    if not _is_bot_admin(cb.from_user.id, config):
        return
    await state.update_data(new_description=None)
    await state.set_state(AdminFlow.add_price)
    await cb.bot.send_message(chat_id, "Введите цену в рублях (целое число):", reply_markup=kb_cancel("selitem_adm_cancel"))


@router.message(AdminFlow.add_description, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def adm_add_description(message: Message, state: FSMContext, config: SellableItemsConfig) -> None:
    data = await state.get_data()
    if _flow_expired(data):
        await _clear_flow_keep_panel(state)
        await message.answer("⏳ Время ожидания истекло — начните заново из /items.")
        return
    if not _is_bot_admin(message.from_user.id, config):
        await _clear_flow_keep_panel(state)
        return
    description = message.text.strip()[:DESCRIPTION_MAX_LEN]
    await state.update_data(new_description=description or None)
    await state.set_state(AdminFlow.add_price)
    await message.answer("Введите цену в рублях (целое число):", reply_markup=kb_cancel("selitem_adm_cancel"))


@router.message(AdminFlow.add_price, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def adm_add_price(message: Message, bot: Bot, state: FSMContext, config: SellableItemsConfig) -> None:
    data = await state.get_data()
    if _flow_expired(data):
        await _clear_flow_keep_panel(state)
        await message.answer("⏳ Время ожидания истекло — начните заново из /items.")
        return
    if not _is_bot_admin(message.from_user.id, config):
        await _clear_flow_keep_panel(state)
        return
    price = _parse_price(message.text)
    if price is None:
        await message.answer(f"Введите целое число от 1 до {PRICE_MAX}.")
        return
    name = data.get("new_name")
    if not name:
        # Unreachable through the normal flow order (name is always collected
        # first), but a corrupted/tampered FSM data dict must not silently
        # create a nameless item — same defensive stance as the field
        # allowlist in _update_item_field.
        await _clear_flow_keep_panel(state)
        await message.answer("⚠️ Что-то пошло не так — начните заново из /items.")
        return
    description = data.get("new_description")
    await _create_item(config.db_path, name, description, price)
    await _clear_flow_keep_panel(state)
    items = await _all_items(config.db_path)
    await _replace_panel(
        bot, state, message.chat.id,
        f"✅ Позиция «{_esc(name)}» добавлена.\n\n💰 <b>Управление позициями</b>",
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
    item_id = int(cb.data.split(":", 1)[1])
    item = await _item_row(config.db_path, item_id)
    await _clear_flow_keep_panel(state)
    if item is None:
        items = await _all_items(config.db_path)
        await _replace_panel(
            bot, state, chat_id,
            "Позиция не найдена — возможно, уже удалена.\n\n💰 <b>Управление позициями</b>",
            reply_markup=kb_admin_items(items),
        )
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
    item_id = int(cb.data.split(":", 1)[1])
    await _toggle_item_active(config.db_path, item_id)
    item = await _item_row(config.db_path, item_id)
    if item is None:
        items = await _all_items(config.db_path)
        await _replace_panel(bot, state, chat_id, "💰 <b>Управление позициями</b>", reply_markup=kb_admin_items(items))
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
    _, item_id_str, field = cb.data.split(":", 2)
    item_id = int(item_id_str)
    if field not in _EDITABLE_FIELDS:
        return
    item = await _item_row(config.db_path, item_id)
    if item is None:
        items = await _all_items(config.db_path)
        await _replace_panel(
            bot, state, chat_id,
            "Позиция не найдена — возможно, уже удалена.\n\n💰 <b>Управление позициями</b>",
            reply_markup=kb_admin_items(items),
        )
        return
    prompts = {
        "name": "Введите новое название:",
        "description": "Введите новое описание:",
        "price": "Введите новую цену в рублях (целое число):",
    }
    await state.update_data(started_at=time.time(), edit_item_id=item_id, edit_field=field)
    await state.set_state(AdminFlow.edit_value)
    await _replace_panel(bot, state, chat_id, prompts[field], reply_markup=kb_cancel(f"selitem_adm_edit:{item_id}"))


@router.message(AdminFlow.edit_value, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def adm_edit_value(message: Message, bot: Bot, state: FSMContext, config: SellableItemsConfig) -> None:
    data = await state.get_data()
    if _flow_expired(data):
        await _clear_flow_keep_panel(state)
        await message.answer("⏳ Время ожидания истекло — начните заново из /items.")
        return
    if not _is_bot_admin(message.from_user.id, config):
        await _clear_flow_keep_panel(state)
        return
    item_id = data.get("edit_item_id")
    field = data.get("edit_field")
    if item_id is None or field not in _EDITABLE_FIELDS:
        await _clear_flow_keep_panel(state)
        await message.answer("⚠️ Что-то пошло не так — начните заново из /items.")
        return

    if field == "price":
        value = _parse_price(message.text)
        if value is None:
            await message.answer(f"Введите целое число от 1 до {PRICE_MAX}.")
            return
    elif field == "name":
        value = message.text.strip()[:NAME_MAX_LEN]
        if not value:
            await message.answer("Название не может быть пустым.")
            return
    else:
        value = message.text.strip()[:DESCRIPTION_MAX_LEN]

    await _update_item_field(config.db_path, item_id, field, value)
    await _clear_flow_keep_panel(state)
    item = await _item_row(config.db_path, item_id)
    if item is None:
        items = await _all_items(config.db_path)
        await _replace_panel(bot, state, message.chat.id, "💰 <b>Управление позициями</b>", reply_markup=kb_admin_items(items))
        return
    await _replace_panel(
        bot, state, message.chat.id, _item_detail_text(item, prefix="✅ Обновлено.\n\n"),
        reply_markup=kb_item_edit_menu(item),
    )


# ── client storefront ────────────────────────────────────────────────────

@router.callback_query(F.data == "selitem_list")
async def cb_list(cb: CallbackQuery, config: SellableItemsConfig) -> None:
    await cb.answer()
    items = await _active_items(config.db_path)
    await _edit_or_send(cb, "🛍 <b>Доступные позиции</b>", reply_markup=kb_storefront(items))


@router.callback_query(F.data.startswith("selitem_view:"))
async def cb_view(cb: CallbackQuery, config: SellableItemsConfig) -> None:
    item_id = int(cb.data.split(":", 1)[1])
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
    item_id = int(cb.data.split(":", 1)[1])
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
    try:
        await create_invoice(
            bot=cb.bot, bot_id=bot_id, chat_id=chat_id,
            title=item["name"], description=item["description"] or item["name"],
            payload=payload, currency=CURRENCY,
            prices=[LabeledPrice(label=item["name"], amount=item["price"] * 100)],
        )
    except ValueError:
        # create_invoice's own guard: bot_payment_providers has no token for
        # this bot yet, even though "payments" is enabled in bot_features.
        await cb.bot.send_message(chat_id, "⚠️ Оплата временно недоступна — обратитесь к администратору.")
    except TelegramAPIError:
        # Covers TelegramBadRequest as well as everything else Telegram's API
        # itself can raise around send_invoice — TelegramForbiddenError (the
        # buyer blocked the bot), TelegramRetryAfter (flood control),
        # TelegramNetworkError, etc. — all subclass TelegramAPIError.
        logger.warning(f"cb_buy: bot_id={bot_id} item_id={item_id} send_invoice failed", exc_info=True)
        await cb.bot.send_message(chat_id, "⚠️ Не удалось выставить счёт. Попробуйте ещё раз позже.")
    except Exception:
        # Review finding: anything NOT covered above (e.g. a sqlite3.Error
        # from get_bot_payment_provider() inside create_invoice, or a
        # TypeError if a manually-tampered row has price=NULL) must not leave
        # the buyer staring at a spinner with silence — cb.answer() above
        # already fired, so this is the last chance to say anything at all.
        logger.exception(f"cb_buy: bot_id={bot_id} item_id={item_id} failed unexpectedly")
        await cb.bot.send_message(chat_id, "⚠️ Не удалось выставить счёт. Попробуйте ещё раз позже.")
