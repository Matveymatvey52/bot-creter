# TEMPLATE: inventory
# USE FOR: складской учёт, движение товара по SKU, приход/расход, поставщики, остатки, инвентаризация
# CUSTOMIZE: sections marked with # CUSTOMIZE
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import aiosqlite
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
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
BOT_DESCRIPTION = "Складской учёт: приход/расход товара по SKU, поставщики, остатки, оповещения о низком запасе."
WELCOME_TEXT = (
    "📦 <b>Складской учёт</b>\n\n"
    "Веду движение товара по SKU — приход, расход, остатки.\n"
    "Оповещу, когда запас упадёт ниже порога.\n\n"
    "Выберите действие:"
)
IN_REASONS = [("purchase", "📦 Закупка"), ("return", "↩️ Возврат от клиента")]
OUT_REASONS = [("sale", "💵 Продажа"), ("writeoff", "🗑 Списание")]
_REASON_LABELS = dict(IN_REASONS + OUT_REASONS)
# ── END CUSTOMIZE ─────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
router = Router()

# ── mini-app config (see docs/MINIAPP_DESIGN.md, templates/tour_operator.py's
# own miniapp_config for the pilot) ─────────────────────────────────────────
# Declarative schema read by runtime/miniapp_api.py's generic CRUD handlers —
# NOT executable code (see that module's docstring). `table` and each field's
# `name` must match init_db()'s CREATE TABLE columns below exactly.
miniapp_config = {
    "resources": [
        {
            "name": "items",
            "table": "items",
            "order_by": "id DESC",
            "creatable": True,
            "title": "Товары",
            "titleField": "name",
            "fields": [
                {"name": "sku", "required": True, "label": "Артикул", "kind": "text", "list": True, "detail": True, "create": True},
                {"name": "name", "required": True, "label": "Название", "kind": "text", "list": True, "detail": True, "create": True},
                {"name": "supplier_id", "label": "Поставщик", "kind": "number", "list": False, "detail": True, "create": True, "ref": {"resource": "suppliers", "labelField": "name"}},
                {"name": "low_stock_threshold", "label": "Мин. остаток", "kind": "number", "list": False, "detail": True, "create": True},
                {"name": "active", "label": "Активен", "kind": "bool", "list": True, "detail": True, "create": True},
            ],
        },
        {
            "name": "suppliers",
            "table": "suppliers",
            "order_by": "id DESC",
            "creatable": True,
            "title": "Поставщики",
            "titleField": "name",
            "fields": [
                {"name": "name", "required": True, "label": "Название", "kind": "text", "list": True, "detail": True, "create": True},
                {"name": "contact", "label": "Контакт", "kind": "text", "list": False, "detail": True, "create": True},
            ],
        },
        {
            "name": "stock_movements",
            "table": "stock_movements",
            "order_by": "created_at DESC",
            "creatable": True,
            "title": "Движения склада",
            "titleField": "reason",
            "fields": [
                {"name": "item_id", "required": True, "label": "Позиция", "kind": "number", "list": True, "detail": True, "create": True, "ref": {"resource": "items", "labelField": "name"}},
                {"name": "change_qty", "required": True, "label": "Изменение", "kind": "number", "list": True, "detail": True, "create": True},
                {"name": "reason", "required": True, "label": "Причина", "kind": "text", "list": True, "detail": True, "create": True},
                {"name": "note", "label": "Заметка", "kind": "text", "list": False, "detail": True, "create": True},
                {"name": "created_at", "label": "Создано", "kind": "date", "list": False, "detail": True, "create": False},
            ],
        },
    ],
}


def _esc(value, max_len: int = 500) -> str:
    """HTML-escapes AND length-bounds any user-supplied text before it goes into
    a parse_mode="HTML" message — same helper/rationale as templates/
    booking_medical.py's _esc(): free-text item names/notes/supplier contacts
    could otherwise contain '<'/'&' and break message rendering, or run long
    enough to hit Telegram's message-length limit."""
    text = str(value) if value is not None else ""
    if len(text) > max_len:
        text = text[:max_len] + "…"
    return html.escape(text)


def _join_bounded(lines: list[str], limit: int = 3500) -> str:
    """Joins lines with a length budget, dropping only WHOLE trailing lines —
    review-found bug: a raw `text[:limit]` character-offset slice can land
    inside an open <b>/</b> HTML span (each line here is self-contained with
    balanced tags), producing unbalanced HTML that Telegram's parse_mode="HTML"
    rejects outright (the whole message fails to send, not just truncates)."""
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
# Same pattern as every other template — see docs/STAGE2_DESIGN.md.

@dataclass
class InventoryConfig:
    bot_name: str
    db_path: str
    admins_file: Path
    welcome_image: Path
    display_name: str | None = None
    group_chat_id: str | None = None
    bot_id: int | None = None
    owner_telegram_id: int | None = None


def _paths_for(name: str, data_dir: Path) -> InventoryConfig:
    return InventoryConfig(
        bot_name=name,
        db_path=str(data_dir / f"{name}_data.db"),
        admins_file=data_dir / f"admins_{name}.json",
        welcome_image=data_dir / "bot_images" / f"{name}.jpg",
    )


def config_from_env() -> InventoryConfig:
    """Standalone/subprocess mode."""
    name = Path(__file__).stem
    data_dir = Path(os.getenv("DATA_DIR", "./data"))
    data_dir.mkdir(exist_ok=True)
    return _paths_for(name, data_dir)


def config_from_bot_row(bot_row: dict, data_dir: Path) -> InventoryConfig:
    """Webhook runtime mode. Paths built from bot_row["bot_id"] (bots.id, the
    physically unique AUTOINCREMENT PK) — NOT bot_row["name"] — same reasoning
    as every other template's config_from_bot_row (see docs/STAGE2_DESIGN.md
    "Изоляция по bots.id")."""
    bot_id = bot_row["bot_id"]
    config = InventoryConfig(
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
    """Injects this bot's InventoryConfig into data["config"]."""

    def __init__(self, config: InventoryConfig) -> None:
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

def _is_bot_admin(user_id: int, config: InventoryConfig) -> bool:
    # The DB-known owner (bots.owner_telegram_id) is always an admin, even if
    # the local admins_file is empty/stale/hijacked — same defense-in-depth
    # rationale as templates/shop_catalog.py's _is_bot_admin.
    if config.owner_telegram_id is not None and str(user_id) == str(config.owner_telegram_id):
        return True
    return str(user_id) in _load_admins(config.admins_file)


# ── db ────────────────────────────────────────────────────────────────────────

async def init_db(db_path: str):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS suppliers (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                name    TEXT NOT NULL,
                contact TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS items (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                sku                 TEXT NOT NULL UNIQUE,
                name                TEXT NOT NULL,
                supplier_id         INTEGER REFERENCES suppliers(id),
                low_stock_threshold INTEGER DEFAULT 0,
                active              INTEGER DEFAULT 1
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS stock_movements (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id    INTEGER NOT NULL REFERENCES items(id),
                change_qty INTEGER NOT NULL,
                reason     TEXT NOT NULL,
                note       TEXT,
                user_id    INTEGER,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.commit()


async def _current_stock(db: aiosqlite.Connection, item_id: int) -> int:
    row = await (await db.execute(
        "SELECT COALESCE(SUM(change_qty),0) FROM stock_movements WHERE item_id=?", (item_id,)
    )).fetchone()
    return row[0]


async def _active_items(db_path: str) -> list[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT * FROM items WHERE active=1 ORDER BY name"
        )).fetchall()
        return [dict(r) for r in rows]


# ── keyboards ─────────────────────────────────────────────────────────────────

def kb_main() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="➕ Приход"), KeyboardButton(text="➖ Расход")],
        [KeyboardButton(text="📦 Остатки"), KeyboardButton(text="📋 История")],
    ], resize_keyboard=True)

def kb_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Отмена", callback_data="inv_cancel")
    ]])

def kb_items(items: list[dict], prefix: str) -> InlineKeyboardMarkup:
    rows = []
    for it in items:
        label = f"{it['name']} ({it['sku']})"
        short = label if len(label) <= 40 else label[:37] + "…"
        rows.append([InlineKeyboardButton(text=short, callback_data=f"{prefix}:{it['id']}")])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="inv_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_reasons(direction: str) -> InlineKeyboardMarkup:
    options = IN_REASONS if direction == "in" else OUT_REASONS
    rows = [[InlineKeyboardButton(text=label, callback_data=f"inv_reason:{code}")] for code, label in options]
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="inv_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_confirm() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подтвердить", callback_data="inv_confirm"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="inv_cancel"),
    ]])


# ── FSM ───────────────────────────────────────────────────────────────────────

class MovementFlow(StatesGroup):
    item = State(); qty = State(); reason = State(); note = State(); confirm = State()


# ── /start, /cancel ───────────────────────────────────────────────────────────

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext, config: InventoryConfig):
    # Review-found: /start must reset any dangling mid-flow FSM state (e.g. a
    # user who abandoned a "приход/расход" flow) — otherwise the user's very
    # next plain-text message gets silently captured as a note/qty for a
    # movement they believed they'd left.
    await state.clear()
    admins = _load_admins(config.admins_file)
    sender_id = message.from_user.id
    # Security fix: this used to grant admin to whoever sent /start FIRST,
    # letting any client who messages the bot before its owner does
    # permanently seize the admin commands below (/additem, /addadmin, ...).
    # When bots.owner_telegram_id is known, only that user may claim the
    # empty-admins bootstrap slot; in standalone/env mode (owner_telegram_id
    # unknown) the old first-comer behavior is kept as the only option.
    is_owner = config.owner_telegram_id is not None and sender_id == config.owner_telegram_id
    first_time_admin = not admins and (is_owner or config.owner_telegram_id is None)
    if first_time_admin:
        _save_admins(config.admins_file, {str(sender_id)})
        if config.bot_id is not None:
            try:
                await add_bot_admin(config.bot_id, str(sender_id))
            except Exception as e:
                logger.warning(f"cmd_start: add_bot_admin sync failed for bot {config.bot_id}: {e}")
    if config.welcome_image.exists():
        await message.answer_photo(FSInputFile(str(config.welcome_image)),
                                   caption=WELCOME_TEXT, parse_mode="HTML", reply_markup=kb_main())
    else:
        await message.answer(WELCOME_TEXT, parse_mode="HTML", reply_markup=kb_main())
    if first_time_admin:
        await message.answer(
            "👑 <b>Вы — администратор этого бота.</b>\n\n"
            "Управление товарами:\n"
            "<code>/additem SKU | Название | Поставщик | Порог</code>\n"
            "(последние два поля не обязательны)\n"
            "<code>/removeitem SKU</code> — деактивировать\n"
            "<code>/items</code> — список\n\n"
            "Управление поставщиками:\n"
            "<code>/addsupplier Имя | Контакт</code>\n"
            "<code>/suppliers</code> — список\n\n"
            "Управление администраторами:\n"
            "<code>/addadmin ID</code> — добавить администратора\n"
            "<code>/removeadmin ID</code> — убрать администратора\n"
            "<code>/admins</code> — список администраторов",
            parse_mode="HTML"
        )

@router.message(Command("cancel"), StateFilter("*"))
async def cmd_cancel(message: Message, state: FSMContext):
    if await state.get_state() is None:
        await message.answer("Нечего отменять.")
        return
    await state.clear()
    await message.answer("Отменено.", reply_markup=kb_main())

@router.callback_query(F.data == "inv_cancel")
async def cb_cancel(cb: CallbackQuery, state: FSMContext):
    await cb.answer(); await state.clear()
    await cb.message.edit_text("Отменено.")
    await cb.message.answer("Что дальше?", reply_markup=kb_main())


# ── MOVEMENT FLOW: direction → item → qty → reason → note → confirm ──────────

@router.message(F.text.in_({"➕ Приход", "➖ Расход"}))
async def movement_start(msg: Message, state: FSMContext, config: InventoryConfig):
    await state.clear()
    items = await _active_items(config.db_path)
    if not items:
        await msg.answer("Список товаров пуст. Добавьте товар: /additem SKU | Название"); return
    direction = "in" if "Приход" in msg.text else "out"
    label = "прихода" if direction == "in" else "расхода"
    await msg.answer(f"Выберите товар для {label}:", reply_markup=kb_items(items, "inv_item"))
    await state.update_data(direction=direction)
    await state.set_state(MovementFlow.item)

@router.callback_query(MovementFlow.item, F.data.startswith("inv_item:"))
async def cb_item(cb: CallbackQuery, state: FSMContext, config: InventoryConfig):
    await cb.answer()
    item_id = int(cb.data.split(":", 1)[1])
    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute(
            "SELECT name, sku FROM items WHERE id=? AND active=1", (item_id,)
        )).fetchone()
    if not row:
        await cb.message.edit_text("Товар не найден или деактивирован. Выберите другой.")
        return
    await cb.message.edit_text(f"📦 {_esc(row[0])} ({_esc(row[1])})\n\nВведите количество:", parse_mode="HTML")
    await state.update_data(item_id=item_id, item_name=row[0], item_sku=row[1])
    await state.set_state(MovementFlow.qty)

@router.message(MovementFlow.qty, F.text, ~F.text.startswith("/"))
async def movement_qty(msg: Message, state: FSMContext):
    try:
        qty = int(msg.text.strip())
        # Review-found: an unbounded int (e.g. 30+ digits) parses fine in
        # Python but overflows SQLite's 64-bit INTEGER column at insert time
        # with an unhandled OverflowError, leaving the user stuck. Reject a
        # clearly-unreasonable quantity here instead.
        if qty <= 0 or qty > 1_000_000:
            raise ValueError
    except ValueError:
        await msg.answer("Введите целое число от 1 до 1000000, например: 10"); return
    data = await state.get_data()
    direction = data.get("direction")
    if not direction:
        # Stale/out-of-order state (e.g. a leftover keyboard from an already
        # completed or abandoned flow) — reset instead of KeyError-ing below.
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_main())
        return
    await msg.answer("Выберите причину:", reply_markup=kb_reasons(direction))
    await state.update_data(qty=qty)
    await state.set_state(MovementFlow.reason)

@router.callback_query(MovementFlow.reason, F.data.startswith("inv_reason:"))
async def cb_reason(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    reason = cb.data.split(":", 1)[1]
    await cb.message.edit_text("📝 Комментарий (необязательно): отправьте текст или /skip")
    await state.update_data(reason=reason)
    await state.set_state(MovementFlow.note)

@router.message(MovementFlow.note, F.text, ~F.text.startswith("/"))
async def movement_note(msg: Message, state: FSMContext):
    await _show_movement_confirmation(msg, state, msg.text.strip())

@router.message(Command("skip"), MovementFlow.note)
async def movement_note_skip(msg: Message, state: FSMContext):
    await _show_movement_confirmation(msg, state, None)


async def _show_movement_confirmation(msg: Message, state: FSMContext, note: str | None) -> None:
    await state.update_data(note=note)
    data = await state.get_data()
    direction_label = "Приход" if data["direction"] == "in" else "Расход"
    sign = "+" if data["direction"] == "in" else "-"
    text = (
        f"📋 <b>Подтвердите операцию:</b>\n\n"
        f"{direction_label}: {sign}{data['qty']} шт.\n"
        f"📦 {_esc(data.get('item_name', ''))} ({_esc(data.get('item_sku', ''))})\n"
        f"🏷 {_REASON_LABELS.get(data['reason'], data['reason'])}\n"
        f"📝 {_esc(note) if note else '—'}"
    )
    try:
        await msg.answer(text, parse_mode="HTML", reply_markup=kb_confirm())
    except Exception as e:
        logger.error(f"_show_movement_confirmation failed to send: {e}")
        await state.clear()
        await msg.answer("⚠️ Не удалось показать подтверждение. Начните заново.", reply_markup=kb_main())
        return
    await state.set_state(MovementFlow.confirm)


@router.callback_query(MovementFlow.confirm, F.data == "inv_confirm")
async def cb_confirm(cb: CallbackQuery, state: FSMContext, bot: Bot, config: InventoryConfig):
    await cb.answer()
    data = await state.get_data()
    if not data.get("item_id"):
        # Review-found blocker: a double-tap (or a stale "✅ Подтвердить"
        # button re-tapped later — Telegram does NOT auto-clear inline
        # keyboards just because the message text changed) would otherwise
        # re-run this whole block with the SAME pending data, inserting a
        # duplicate stock_movements row — unlike booking_medical's
        # appointments.slot_id UNIQUE, there is no natural constraint here
        # that would catch a real duplicate (two legitimate -10 sales look
        # identical to one accidental double -10). Fix: state.clear() happens
        # immediately below, with NO real (suspending) await between reading
        # the data and clearing it — MemoryStorage's get_data()/clear() do no
        # actual I/O, so this sequence cannot be interleaved by a concurrently
        # scheduled second invocation of this same handler.
        try:
            await cb.message.edit_text("Эта операция уже обработана или недоступна.")
        except Exception:
            pass
        return
    await state.clear()
    item_id = data["item_id"]
    qty = data["qty"]
    signed_qty = qty if data["direction"] == "in" else -qty

    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute(
            "SELECT low_stock_threshold FROM items WHERE id=? AND active=1", (item_id,)
        )).fetchone()
        if not row:
            await cb.message.edit_text("❌ Товар был деактивирован. Операция отменена.")
            return
        threshold = row[0]
        stock_before = await _current_stock(db, item_id)
        stock_after = stock_before + signed_qty
        if stock_after < 0:
            # Review-found: nothing previously stopped a "расход" from
            # driving recorded stock negative — a real integrity gap for a
            # tool whose whole point is reflecting physical reality.
            await cb.message.edit_text(
                f"❌ Недостаточно товара на складе (в наличии {stock_before}, запрошено {qty})."
            )
            return

        await db.execute(
            "INSERT INTO stock_movements (item_id, change_qty, reason, note, user_id) VALUES (?,?,?,?,?)",
            (item_id, signed_qty, data["reason"], data.get("note"), cb.from_user.id)
        )
        await db.commit()

    direction_label = "Приход" if data["direction"] == "in" else "Расход"
    await cb.message.edit_text(
        f"✅ <b>{direction_label} записан</b>\n\n"
        f"📦 {_esc(data.get('item_name', ''))}\n"
        f"Остаток: <b>{stock_after}</b> шт.",
        parse_mode="HTML"
    )

    # Low-stock alert — ONLY on the crossing event (was at/above threshold,
    # now below it), not on every subsequent movement while already below —
    # otherwise every next sale of an already-low item would re-spam admins.
    if stock_after < threshold <= stock_before:
        async with aiosqlite.connect(config.db_path) as db:
            db.row_factory = aiosqlite.Row
            supplier = await (await db.execute(
                "SELECT s.name, s.contact FROM items i LEFT JOIN suppliers s ON i.supplier_id = s.id "
                "WHERE i.id=?", (item_id,)
            )).fetchone()
        supplier_line = "Поставщик не указан."
        if supplier and supplier["name"]:
            supplier_line = f"Поставщик: {_esc(supplier['name'])}" + (
                f" · {_esc(supplier['contact'])}" if supplier["contact"] else ""
            )
        for admin_id in _load_admins(config.admins_file):
            try:
                await bot.send_message(
                    int(admin_id),
                    f"⚠️ <b>Низкий остаток!</b>\n\n"
                    f"📦 {_esc(data.get('item_name', ''))} ({_esc(data.get('item_sku', ''))})\n"
                    f"Остаток: <b>{stock_after}</b> шт. (порог: {threshold})\n\n"
                    f"{supplier_line}",
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.warning(f"low-stock alert to admin {admin_id} failed: {e}")


# ── STOCK LEVELS ──────────────────────────────────────────────────────────────

@router.message(F.text == "📦 Остатки")
async def show_stock(msg: Message, config: InventoryConfig):
    items = await _active_items(config.db_path)
    if not items:
        await msg.answer("Список товаров пуст."); return
    async with aiosqlite.connect(config.db_path) as db:
        stocks = []
        for it in items:
            stock = await _current_stock(db, it["id"])
            stocks.append((it, stock))
    stocks.sort(key=lambda pair: pair[1])
    lines = ["📦 <b>Остатки:</b>\n"]
    for it, stock in stocks:
        warn = " ⚠️" if stock < it["low_stock_threshold"] else ""
        lines.append(f"• {_esc(it['name'])} ({_esc(it['sku'])}): <b>{stock}</b> шт.{warn}")
    await msg.answer(_join_bounded(lines), parse_mode="HTML")


# ── HISTORY ───────────────────────────────────────────────────────────────────

@router.message(F.text == "📋 История")
async def history_start(msg: Message, state: FSMContext, config: InventoryConfig):
    await state.clear()
    # Review-found: deliberately lists ALL items, not just active=1 — a
    # deactivated (discontinued) SKU's movement history must stay browsable
    # (that's the whole point of soft-deactivation instead of DELETE), and
    # _active_items()-only listing here would make it unreachable once the
    # menu with its (now-stale) button scrolls out of the chat.
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute("SELECT * FROM items ORDER BY name")).fetchall()
        items = [dict(r) for r in rows]
    if not items:
        await msg.answer("Список товаров пуст."); return
    rows_kb = []
    for it in items:
        label = f"{it['name']} ({it['sku']})" + ("" if it["active"] else " 🚫")
        short = label if len(label) <= 40 else label[:37] + "…"
        rows_kb.append([InlineKeyboardButton(text=short, callback_data=f"inv_hist:{it['id']}")])
    rows_kb.append([InlineKeyboardButton(text="❌ Отмена", callback_data="inv_cancel")])
    await msg.answer(
        "Выберите товар для просмотра истории (🚫 = деактивирован):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows_kb)
    )

@router.callback_query(F.data.startswith("inv_hist:"))
async def cb_history(cb: CallbackQuery, config: InventoryConfig):
    await cb.answer()
    item_id = int(cb.data.split(":", 1)[1])
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        item = await (await db.execute("SELECT name, sku FROM items WHERE id=?", (item_id,))).fetchone()
        if not item:
            await cb.message.edit_text("Товар не найден."); return
        rows = await (await db.execute(
            "SELECT change_qty, reason, note, created_at FROM stock_movements "
            "WHERE item_id=? ORDER BY id DESC LIMIT 20", (item_id,)
        )).fetchall()
    if not rows:
        await cb.message.edit_text(f"По товару «{_esc(item['name'])}» движений нет.", parse_mode="HTML")
        return
    lines = [f"📋 <b>История: {_esc(item['name'])} ({_esc(item['sku'])})</b>\n"]
    for r in rows:
        sign = "+" if r["change_qty"] > 0 else ""
        lines.append(
            f"{r['created_at']}  <b>{sign}{r['change_qty']}</b>  "
            f"{_esc(_REASON_LABELS.get(r['reason'], r['reason']))}"
            + (f" · {_esc(r['note'])}" if r["note"] else "")
        )
    await cb.message.edit_text(_join_bounded(lines), parse_mode="HTML")


# ── ADMIN: items ──────────────────────────────────────────────────────────────

@router.message(Command("additem"))
async def cmd_additem(msg: Message, config: InventoryConfig):
    if not _is_bot_admin(msg.from_user.id, config):
        await msg.answer("⛔ Нет доступа"); return
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2 or "|" not in parts[1]:
        await msg.answer("Использование: /additem SKU | Название | Поставщик | Порог\n(последние два поля не обязательны)")
        return
    fields = [p.strip() for p in parts[1].split("|")]
    sku, name = fields[0], fields[1] if len(fields) > 1 else ""
    supplier_name = fields[2] if len(fields) > 2 and fields[2] else None
    threshold_raw = fields[3] if len(fields) > 3 else ""
    if not sku or not name:
        await msg.answer("SKU и название обязательны."); return
    if len(sku) > 40 or len(name) > 60:
        await msg.answer("⚠️ SKU должен быть короче 40, название — короче 60 символов."); return
    threshold = 0
    if threshold_raw:
        try:
            threshold = int(threshold_raw)
        except ValueError:
            await msg.answer("⚠️ Порог должен быть целым числом."); return

    supplier_id = None
    supplier_note = ""
    async with aiosqlite.connect(config.db_path) as db:
        if supplier_name:
            row = await (await db.execute(
                "SELECT id FROM suppliers WHERE name=?", (supplier_name,)
            )).fetchone()
            if row:
                supplier_id = row[0]
            else:
                supplier_note = f"\n⚠️ Поставщик «{_esc(supplier_name)}» не найден — добавьте через /addsupplier и свяжите позже."
        try:
            await db.execute(
                "INSERT INTO items (sku, name, supplier_id, low_stock_threshold) VALUES (?,?,?,?)",
                (sku, name, supplier_id, threshold)
            )
            await db.commit()
        except sqlite3.IntegrityError:
            await msg.answer(f"⚠️ Товар с SKU «{_esc(sku)}» уже существует.", parse_mode="HTML")
            return
    await msg.answer(f"✅ Товар <b>{_esc(name)}</b> ({_esc(sku)}) добавлен.{supplier_note}", parse_mode="HTML")

@router.message(Command("removeitem"))
async def cmd_removeitem(msg: Message, config: InventoryConfig):
    if not _is_bot_admin(msg.from_user.id, config):
        await msg.answer("⛔ Нет доступа"); return
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await msg.answer("Использование: /removeitem SKU"); return
    sku = parts[1].strip()
    async with aiosqlite.connect(config.db_path) as db:
        cur = await db.execute("UPDATE items SET active=0 WHERE sku=?", (sku,))
        await db.commit()
    if cur.rowcount == 0:
        await msg.answer(f"Товар с SKU «{_esc(sku)}» не найден.", parse_mode="HTML"); return
    await msg.answer(f"✅ Товар «{_esc(sku)}» деактивирован (история движений сохранена).", parse_mode="HTML")

@router.message(Command("items"))
async def cmd_items(msg: Message, config: InventoryConfig):
    if not _is_bot_admin(msg.from_user.id, config):
        await msg.answer("⛔ Нет доступа"); return
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT i.*, s.name AS supplier_name FROM items i "
            "LEFT JOIN suppliers s ON i.supplier_id = s.id ORDER BY i.name"
        )).fetchall()
    if not rows:
        await msg.answer("Список товаров пуст."); return
    lines = ["📦 <b>Товары:</b>\n"]
    for r in rows:
        status = "✅" if r["active"] else "🚫"
        supplier = f" · {_esc(r['supplier_name'])}" if r["supplier_name"] else ""
        lines.append(f"{status} {_esc(r['name'])} ({_esc(r['sku'])}) · порог {r['low_stock_threshold']}{supplier}")
    await msg.answer(_join_bounded(lines), parse_mode="HTML")


# ── ADMIN: suppliers ──────────────────────────────────────────────────────────

@router.message(Command("addsupplier"))
async def cmd_addsupplier(msg: Message, config: InventoryConfig):
    if not _is_bot_admin(msg.from_user.id, config):
        await msg.answer("⛔ Нет доступа"); return
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2 or "|" not in parts[1]:
        await msg.answer("Использование: /addsupplier Имя | Контакт"); return
    name, contact = (p.strip() for p in parts[1].split("|", 1))
    if not name:
        await msg.answer("Имя поставщика обязательно."); return
    if len(name) > 60 or len(contact) > 100:
        await msg.answer("⚠️ Имя должно быть короче 60, контакт — короче 100 символов."); return
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute("INSERT INTO suppliers (name, contact) VALUES (?,?)", (name, contact))
        await db.commit()
    await msg.answer(f"✅ Поставщик <b>{_esc(name)}</b> добавлен.", parse_mode="HTML")

@router.message(Command("suppliers"))
async def cmd_suppliers(msg: Message, config: InventoryConfig):
    if not _is_bot_admin(msg.from_user.id, config):
        await msg.answer("⛔ Нет доступа"); return
    async with aiosqlite.connect(config.db_path) as db:
        rows = await (await db.execute("SELECT name, contact FROM suppliers ORDER BY name")).fetchall()
    if not rows:
        await msg.answer("Список поставщиков пуст."); return
    lines = ["🚚 <b>Поставщики:</b>\n"]
    for name, contact in rows:
        lines.append(f"• {_esc(name)}" + (f" · {_esc(contact)}" if contact else ""))
    await msg.answer("\n".join(lines), parse_mode="HTML")


# ── ADMIN COMMANDS ────────────────────────────────────────────────────────────

@router.message(Command("addadmin"))
async def cmd_addadmin(msg: Message, config: InventoryConfig):
    if not _is_bot_admin(msg.from_user.id, config): await msg.answer("⛔ Нет доступа"); return
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
async def cmd_removeadmin(msg: Message, config: InventoryConfig):
    if not _is_bot_admin(msg.from_user.id, config): await msg.answer("⛔ Нет доступа"); return
    parts = msg.text.split()
    if len(parts) < 2: await msg.answer("Использование: /removeadmin <id>"); return
    ids = _load_admins(config.admins_file); ids.discard(parts[1]); _save_admins(config.admins_file, ids)
    if config.bot_id is not None:
        try:
            await remove_bot_admin(config.bot_id, parts[1])
        except Exception as e:
            logger.warning(f"cmd_removeadmin: remove_bot_admin sync failed for bot {config.bot_id}: {e}")
    await msg.answer(f"✅ <code>{_esc(parts[1])}</code> удалён.", parse_mode="HTML")

@router.message(Command("admins"))
async def cmd_admins(msg: Message, config: InventoryConfig):
    if not _is_bot_admin(msg.from_user.id, config): await msg.answer("⛔ Нет доступа"); return
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
