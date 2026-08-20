# TEMPLATE: shop_catalog
# USE FOR: витрина товаров — каталог по категориям, корзина, оформление заявки без онлайн-оплаты (оплата уточняется вручную с оператором); универсальная база для "просто продаю товары X"
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
from pathlib import Path

import aiosqlite
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, Message, ReplyKeyboardMarkup,
)

# ── CUSTOMIZE ────────────────────────────────────────────────────────────────
# Same status as every other template's CUSTOMIZE block: per-file source-text
# customization Claude edits when generating a specific bot, not per-bot
# runtime state (that's config.db_path/admins_file below). Categories/products/
# orders themselves are runtime data, added/removed via this bot's own admin
# panel, not by editing this file.
BOT_DESCRIPTION = "Витрина товаров: каталог по категориям, корзина, оформление заявки без онлайн-оплаты."
WELCOME_TEXT = (
    "🛍 <b>Магазин</b>\n\n"
    "Смотрите каталог, добавляйте товары в корзину и оформляйте заявку — "
    "оплату и доставку уточним с вами лично.\n\n"
    "Нажмите нужную кнопку ниже:"
)
OWNER_NOTIFY_TEXT = "🔔 <b>Новая заявка!</b>\n"
CURRENCY_SYMBOL = "₽"
NAME_MAX_LEN = 100
DESCRIPTION_MAX_LEN = 1000
PRICE_MAX = 10_000_000  # цена — целое число в основных единицах валюты (без копеек/центов)
# ── END CUSTOMIZE ─────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
router = Router()

# ── mini-app config (see docs/MINIAPP_DESIGN.md, templates/tour_operator.py's
# own miniapp_config for the pilot) ─────────────────────────────────────────
# Declarative schema read by runtime/miniapp_api.py's generic CRUD handlers —
# NOT executable code (see that module's docstring). `table` and each field's
# `name` must match init_db()'s CREATE TABLE columns below exactly. cart_items
# is intentionally excluded — it has no `id` column (its PK is
# UNIQUE(user_id, product_id)), which runtime/miniapp_api.py's generic
# handlers don't support.
miniapp_config = {
    "resources": [
        {
            "name": "products",
            "table": "products",
            "order_by": "id DESC",
            "creatable": True,
            "title": "Товары",
            "titleField": "name",
            "fields": [
                {"name": "category_id", "required": True, "label": "ID категории", "kind": "number", "list": False, "detail": True, "create": True},
                {"name": "name", "required": True, "label": "Название", "kind": "text", "list": True, "detail": True, "create": True},
                {"name": "description", "label": "Описание", "kind": "text", "list": False, "detail": True, "create": True},
                {"name": "price", "required": True, "label": "Цена", "kind": "number", "list": True, "detail": True, "create": True},
                {"name": "active", "label": "Активен", "kind": "bool", "list": True, "detail": True, "create": True},
            ],
        },
        {
            "name": "orders",
            "table": "orders",
            "order_by": "created_at DESC",
            "creatable": True,
            "title": "Заказы",
            "titleField": "client_name",
            "fields": [
                {"name": "client_name", "label": "Клиент", "kind": "text", "list": True, "detail": True, "create": True},
                {"name": "client_phone", "label": "Телефон", "kind": "text", "list": False, "detail": True, "create": True},
                {"name": "total", "required": True, "label": "Сумма", "kind": "number", "list": True, "detail": True, "create": True},
                {"name": "status", "label": "Статус", "kind": "status", "list": True, "detail": True, "create": False},
                {"name": "created_at", "label": "Создан", "kind": "date", "list": False, "detail": True, "create": False},
            ],
        },
    ],
}

ORDER_STATUS_ORDER = ["new", "processing", "done", "cancelled"]
ORDER_STATUS_LABELS = {
    "new": "🆕 Новый", "processing": "🔄 В обработке",
    "done": "✅ Выполнен", "cancelled": "❌ Отменён",
}


def _esc(value, max_len: int = 500) -> str:
    """HTML-escapes AND length-bounds any user-supplied text before it goes
    into a parse_mode="HTML" message — same helper/rationale as
    booking_fitness.py's _esc()."""
    text = str(value) if value is not None else ""
    if len(text) > max_len:
        text = text[:max_len] + "…"
    return html.escape(text)


def _join_bounded(lines: list[str], limit: int = 3500) -> str:
    """Joins lines with a length budget, dropping only WHOLE trailing lines —
    same approach as booking_fitness.py's _join_bounded()."""
    out: list[str] = []
    total = 0
    for line in lines:
        if total + len(line) + 1 > limit:
            out.append("…")
            break
        out.append(line)
        total += len(line) + 1
    return "\n".join(out)


def _short(label: str, max_len: int = 40) -> str:
    return label if len(label) <= max_len else label[:max_len - 1] + "…"


def _valid_telegram_id(text: str) -> bool:
    """Same guard as moderator.py's _valid_admin_id / booking_fitness.py's
    _valid_telegram_id: bounds length before treating input as numeric,
    rejects non-ASCII look-alike digits, and rejects "0"/leading-zero/
    negative forms — no real Telegram user_id is ever 0, negative, or
    leading-zero."""
    if not (bool(text) and text.isascii() and text.isdigit() and len(text) <= 15):
        return False
    return int(text) > 0 and str(int(text)) == text


def _normalize_phone(raw: str) -> str | None:
    """Reused verbatim from booking_fitness.py/booking_beauty.py."""
    digits = re.sub(r"[^\d]", "", raw)
    if len(digits) == 11 and digits[0] in ("7", "8"):
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    else:
        return None
    return f"+{digits[0]} ({digits[1:4]}) {digits[4:7]}-{digits[7:9]}-{digits[9:11]}"


# ── config ───────────────────────────────────────────────────────────────────
# Same shape/contract as every other template's Config dataclass — see
# docs/STAGE2_DESIGN.md "Config-контракт шаблона".

@dataclass
class ShopCatalogConfig:
    bot_name: str
    db_path: str
    admins_file: Path
    welcome_image: Path
    display_name: str | None = None
    group_chat_id: str | None = None


def _paths_for(name: str, data_dir: Path) -> ShopCatalogConfig:
    return ShopCatalogConfig(
        bot_name=name,
        db_path=str(data_dir / f"{name}_data.db"),
        admins_file=data_dir / f"admins_{name}.json",
        welcome_image=data_dir / "bot_images" / f"{name}.jpg",
    )


def config_from_env() -> ShopCatalogConfig:
    """Standalone/subprocess mode."""
    name = Path(__file__).stem
    data_dir = Path(os.getenv("DATA_DIR", "./data"))
    data_dir.mkdir(exist_ok=True)
    return _paths_for(name, data_dir)


def config_from_bot_row(bot_row: dict, data_dir: Path) -> ShopCatalogConfig:
    """Webhook runtime mode. Paths keyed by bot_row["bot_id"] (bots.id, the
    physically unique AUTOINCREMENT PK) — NOT bot_row["name"] — same
    reasoning as every other template's config_from_bot_row."""
    bot_id = bot_row["bot_id"]
    config = ShopCatalogConfig(
        bot_name=bot_row["name"],
        db_path=str(data_dir / f"bot_{bot_id}_data.db"),
        admins_file=data_dir / f"admins_{bot_id}.json",
        welcome_image=data_dir / "bot_images" / f"bot_{bot_id}.jpg",
    )
    config.display_name = bot_row.get("display_name")
    config.group_chat_id = bot_row.get("group_chat_id")
    return config


class ConfigMiddleware(BaseMiddleware):
    """Injects this bot's ShopCatalogConfig into data["config"]."""

    def __init__(self, config: ShopCatalogConfig) -> None:
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

def _is_bot_admin(user_id: int, config: ShopCatalogConfig) -> bool:
    return str(user_id) in _load_admins(config.admins_file)


# ── db ────────────────────────────────────────────────────────────────────────

async def init_db(db_path: str):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS categories (
                id     INTEGER PRIMARY KEY AUTOINCREMENT,
                name   TEXT NOT NULL,
                active INTEGER DEFAULT 1
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                category_id   INTEGER NOT NULL REFERENCES categories(id),
                name          TEXT NOT NULL,
                description   TEXT,
                price         INTEGER NOT NULL,
                photo_file_id TEXT,
                active        INTEGER DEFAULT 1
            )
        """)
        # The cart is a DB table, deliberately NOT FSM state: every template's
        # main() runs Dispatcher(storage=MemoryStorage()), so FSM state does
        # not survive a process restart/redeploy. The owner's brief calls the
        # cart state that "accumulates between messages" — a bot_<id>_data.db
        # row survives restarts, MemoryStorage does not. UNIQUE(user_id,
        # product_id) makes "add to cart" an idempotent increment (ON CONFLICT
        # DO UPDATE qty=qty+1) rather than ever needing a duplicate-row check.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS cart_items (
                user_id    INTEGER NOT NULL,
                product_id INTEGER NOT NULL REFERENCES products(id),
                qty        INTEGER NOT NULL,
                UNIQUE(user_id, product_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      INTEGER NOT NULL,
                client_name  TEXT,
                client_phone TEXT,
                total        INTEGER NOT NULL,
                status       TEXT NOT NULL DEFAULT 'new',
                created_at   TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        # Snapshot table: name_snapshot/price_snapshot are copied at checkout
        # time so a LATER admin edit to a product's name/price never rewrites
        # the historical record of what was actually ordered.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS order_items (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id       INTEGER NOT NULL REFERENCES orders(id),
                product_id     INTEGER,
                name_snapshot  TEXT NOT NULL,
                price_snapshot INTEGER NOT NULL,
                qty            INTEGER NOT NULL
            )
        """)
        await db.commit()


async def _active_categories(db_path: str) -> list[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT * FROM categories WHERE active=1 ORDER BY name"
        )).fetchall()
        return [dict(r) for r in rows]

async def _category_row(db_path: str, category_id: int) -> dict | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            "SELECT * FROM categories WHERE id=?", (category_id,)
        )).fetchone()
        return dict(row) if row else None

async def _active_products_in_category(db_path: str, category_id: int) -> list[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT * FROM products WHERE category_id=? AND active=1 ORDER BY name", (category_id,)
        )).fetchall()
        return [dict(r) for r in rows]

async def _active_products_admin(db_path: str) -> list[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT p.*, c.name AS category_name FROM products p "
            "JOIN categories c ON p.category_id=c.id WHERE p.active=1 ORDER BY p.name"
        )).fetchall()
        return [dict(r) for r in rows]

async def _product_row(db_path: str, product_id: int) -> dict | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            "SELECT * FROM products WHERE id=?", (product_id,)
        )).fetchone()
        return dict(row) if row else None


# ── cart ──────────────────────────────────────────────────────────────────────

async def _cart_rows(db_path: str, user_id: int) -> list[dict]:
    """Cart lines joined with CURRENT product name/price (pre-checkout — the
    frozen snapshot only happens at order time, see _create_order).

    Review-found: p.active=1 filters out a product an admin deactivated AFTER
    it was added to someone's cart — every client-facing catalog query
    already hides inactive products (_active_products_in_category,
    cb_product_detail's own guard), so the cart must match, otherwise a
    buyer could still see and check out a product that's been pulled from
    sale. The orphaned cart_items row is simply invisible from here on
    (harmless leftover data, same as any other soft-deleted row in this
    template) rather than needing an explicit prune."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT ci.product_id, ci.qty, p.name, p.price FROM cart_items ci "
            "JOIN products p ON ci.product_id=p.id WHERE ci.user_id=? AND p.active=1 ORDER BY p.name",
            (user_id,),
        )).fetchall()
        return [dict(r) for r in rows]

def _cart_total(rows: list[dict]) -> int:
    return sum(r["price"] * r["qty"] for r in rows)

async def _add_to_cart(db_path: str, user_id: int, product_id: int) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO cart_items (user_id, product_id, qty) VALUES (?,?,1) "
            "ON CONFLICT(user_id, product_id) DO UPDATE SET qty = qty + 1",
            (user_id, product_id),
        )
        await db.commit()

async def _change_cart_qty(db_path: str, user_id: int, product_id: int, delta: int) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE cart_items SET qty = qty + ? WHERE user_id=? AND product_id=?",
            (delta, user_id, product_id),
        )
        # A decrement that reaches zero (or somehow below, e.g. a race between
        # two taps) removes the line entirely rather than leaving a qty<=0 row
        # a client could see reflected as a "0 шт." line.
        await db.execute(
            "DELETE FROM cart_items WHERE user_id=? AND product_id=? AND qty<=0",
            (user_id, product_id),
        )
        await db.commit()

async def _remove_cart_item(db_path: str, user_id: int, product_id: int) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute("DELETE FROM cart_items WHERE user_id=? AND product_id=?", (user_id, product_id))
        await db.commit()

async def _clear_cart(db_path: str, user_id: int) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute("DELETE FROM cart_items WHERE user_id=?", (user_id,))
        await db.commit()


# ── orders ────────────────────────────────────────────────────────────────────

async def _create_order(db_path: str, user_id: int, client_name: str, client_phone: str) -> dict:
    """Snapshots the user's cart into a new order + order_items, then empties
    the cart. Returns {"ok": True, "order_id": int, "total": int} or
    {"ok": False, "error": "cart_empty"}.

    BEGIN IMMEDIATE + re-reading the cart under the lock guards the same
    double-tap class of bug as booking_fitness.py's _book_slot: a double-tap
    on "✅ Подтвердить заказ" fires two callback updates. Without this, the
    second call would still see the (not-yet-cleared) cart and insert a
    SECOND order for the same items. Here the cart is deleted inside the same
    transaction as the order insert, so the second call's own re-read of
    cart_items (after the first call's commit) finds it empty and returns
    cart_empty instead of creating a duplicate order.
    """
    async with aiosqlite.connect(db_path, timeout=10) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        # p.active=1 — same reasoning as _cart_rows: a product deactivated
        # after being added to this cart must not be checked out. The final
        # DELETE FROM cart_items below still clears ALL of this user's cart
        # rows (not just the ones selected here), so an orphaned inactive-
        # product line doesn't linger past this checkout either.
        rows = await (await db.execute(
            "SELECT ci.product_id, ci.qty, p.name, p.price FROM cart_items ci "
            "JOIN products p ON ci.product_id=p.id WHERE ci.user_id=? AND p.active=1",
            (user_id,),
        )).fetchall()
        if not rows:
            await db.commit()
            return {"ok": False, "error": "cart_empty"}

        total = sum(r["price"] * r["qty"] for r in rows)
        cur = await db.execute(
            "INSERT INTO orders (user_id, client_name, client_phone, total, status) VALUES (?,?,?,?,'new')",
            (user_id, client_name, client_phone, total),
        )
        order_id = cur.lastrowid
        for r in rows:
            await db.execute(
                "INSERT INTO order_items (order_id, product_id, name_snapshot, price_snapshot, qty) "
                "VALUES (?,?,?,?,?)",
                (order_id, r["product_id"], r["name"], r["price"], r["qty"]),
            )
        await db.execute("DELETE FROM cart_items WHERE user_id=?", (user_id,))
        await db.commit()
        return {"ok": True, "order_id": order_id, "total": total}


async def _order_items(db_path: str, order_id: int) -> list[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT name_snapshot, price_snapshot, qty FROM order_items WHERE order_id=?", (order_id,)
        )).fetchall()
        return [dict(r) for r in rows]

async def _user_orders(db_path: str, user_id: int, limit: int = 20) -> list[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit)
        )).fetchall()
        return [dict(r) for r in rows]

async def _recent_orders(db_path: str, limit: int = 20) -> list[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,)
        )).fetchall()
        return [dict(r) for r in rows]

async def _order_row(db_path: str, order_id: int) -> dict | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute("SELECT * FROM orders WHERE id=?", (order_id,))).fetchone()
        return dict(row) if row else None

async def _set_order_status(db_path: str, order_id: int, status: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute("UPDATE orders SET status=? WHERE id=?", (status, order_id))
        await db.commit()

async def _stats(db_path: str) -> dict:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        total_orders = (await (await db.execute("SELECT COUNT(*) FROM orders")).fetchone())[0]
        revenue = (await (await db.execute(
            "SELECT COALESCE(SUM(total),0) FROM orders WHERE status != 'cancelled'"
        )).fetchone())[0]
        top = await (await db.execute(
            "SELECT oi.name_snapshot AS name, SUM(oi.qty) AS units FROM order_items oi "
            "JOIN orders o ON oi.order_id=o.id WHERE o.status != 'cancelled' "
            "GROUP BY oi.name_snapshot ORDER BY units DESC LIMIT 5"
        )).fetchall()
    return {"total_orders": total_orders, "revenue": revenue, "top": [dict(r) for r in top]}


# ── keyboards: client menu ─────────────────────────────────────────────────────

def kb_main(is_admin: bool) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="🛍 Каталог"), KeyboardButton(text="🛒 Корзина")],
        [KeyboardButton(text="📦 Мои заказы")],
    ]
    if is_admin:
        rows.append([KeyboardButton(text="⚙️ Панель администратора")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)

def kb_cancel(callback_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data=callback_data)]])

def kb_optional_step(skip_cb: str, cancel_cb: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏩ Пропустить", callback_data=skip_cb)],
        [InlineKeyboardButton(text="❌ Отмена", callback_data=cancel_cb)],
    ])


# ── keyboards: catalog + cart ──────────────────────────────────────────────────

def kb_categories(cats: list[dict]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"📂 {_short(c['name'])}", callback_data=f"shop_cat:{c['id']}")] for c in cats]
    if not rows:
        rows.append([InlineKeyboardButton(text="Каталог пока пуст", callback_data="shop_noop")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_products_list(products: list[dict], category_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text=_short(f"{p['name']} — {p['price']}{CURRENCY_SYMBOL}"),
            callback_data=f"shop_prod:{p['id']}",
        )] for p in products
    ]
    if not rows:
        rows.append([InlineKeyboardButton(text="В этой категории пока нет товаров", callback_data="shop_noop")])
    rows.append([InlineKeyboardButton(text="◀️ К категориям", callback_data="shop_cat_back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_product_detail(product_id: int, category_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ В корзину", callback_data=f"shop_add:{product_id}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"shop_prod_back:{category_id}")],
    ])

def kb_cart(rows: list[dict], total: int) -> InlineKeyboardMarkup:
    kb_rows = []
    for r in rows:
        pid = r["product_id"]
        kb_rows.append([
            InlineKeyboardButton(text="➖", callback_data=f"shop_cart_dec:{pid}"),
            InlineKeyboardButton(text=f"🗑 {_short(r['name'], 24)}", callback_data=f"shop_cart_rm:{pid}"),
            InlineKeyboardButton(text="➕", callback_data=f"shop_cart_inc:{pid}"),
        ])
    if rows:
        kb_rows.append([InlineKeyboardButton(text=f"✅ Оформить заказ ({total}{CURRENCY_SYMBOL})", callback_data="shop_checkout_start")])
    kb_rows.append([InlineKeyboardButton(text="🛍 В каталог", callback_data="shop_cat_list")])
    return InlineKeyboardMarkup(inline_keyboard=kb_rows)

def kb_checkout_confirm() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подтвердить заказ", callback_data="shop_checkout_confirm"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="shop_checkout_cancel"),
    ]])


# ── FSM ───────────────────────────────────────────────────────────────────────

class CheckoutFlow(StatesGroup):
    name = State(); phone = State(); confirm = State()

class AdminFlow(StatesGroup):
    cat_name = State()
    prod_category = State(); prod_name = State(); prod_desc = State(); prod_price = State(); prod_photo = State()
    edit_value = State()
    add_admin = State(); remove_admin_pick = State()

# Same rationale as booking_fitness.py's FLOW_TIMEOUT_SECONDS: MemoryStorage
# keeps a state until explicitly cleared — without an expiry, a user/admin who
# opens a checkout/add-product prompt and then goes quiet has every LATER
# plain message silently captured by that stale flow step.
FLOW_TIMEOUT_SECONDS = 300

def _flow_expired(data: dict) -> bool:
    started_at = data.get("started_at")
    return started_at is None or (time.time() - started_at) > FLOW_TIMEOUT_SECONDS


# ── admin panel navigation helpers (same pattern as moderator.py/booking_fitness.py) ─

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


def _panel_chat_id(cb: CallbackQuery) -> int | None:
    return cb.message.chat.id if cb.message else None


# ── keyboards: admin panel ─────────────────────────────────────────────────────

def kb_admin_panel_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📂 Категории", callback_data="shop_adm_categories")],
        [InlineKeyboardButton(text="📦 Товары", callback_data="shop_adm_products")],
        [InlineKeyboardButton(text="🧾 Заказы", callback_data="shop_adm_orders")],
        [InlineKeyboardButton(text="👥 Админы бота", callback_data="shop_adm_admins")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="shop_adm_stats")],
    ])

async def kb_categories_admin(db_path: str) -> InlineKeyboardMarkup:
    cats = await _active_categories(db_path)
    rows = [[InlineKeyboardButton(text=f"➖ {_short(c['name'])}", callback_data=f"shop_adm_cat_rm:{c['id']}")] for c in cats]
    rows.append([InlineKeyboardButton(text="➕ Добавить категорию", callback_data="shop_adm_cat_add")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="shop_adm_back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def kb_category_pick_for_product(db_path: str) -> InlineKeyboardMarkup:
    cats = await _active_categories(db_path)
    rows = [[InlineKeyboardButton(text=_short(c["name"]), callback_data=f"shop_adm_prod_cat:{c['id']}")] for c in cats]
    if not rows:
        rows.append([InlineKeyboardButton(text="Сначала добавьте категорию", callback_data="shop_noop")])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="shop_adm_flow_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def kb_products_admin(db_path: str) -> InlineKeyboardMarkup:
    products = await _active_products_admin(db_path)
    rows = []
    for p in products:
        label = _short(f"{p['name']} ({p['price']}{CURRENCY_SYMBOL})", 30)
        rows.append([
            InlineKeyboardButton(text=f"✏️ {label}", callback_data=f"shop_adm_prod_edit:{p['id']}"),
            InlineKeyboardButton(text="➖", callback_data=f"shop_adm_prod_rm:{p['id']}"),
        ])
    rows.append([InlineKeyboardButton(text="➕ Добавить товар", callback_data="shop_adm_prod_add")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="shop_adm_back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_product_edit_menu(product_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Название", callback_data=f"shop_adm_prod_field:{product_id}:name")],
        [InlineKeyboardButton(text="📝 Описание", callback_data=f"shop_adm_prod_field:{product_id}:description")],
        [InlineKeyboardButton(text="💰 Цена", callback_data=f"shop_adm_prod_field:{product_id}:price")],
        [InlineKeyboardButton(text="🖼 Фото", callback_data=f"shop_adm_prod_field:{product_id}:photo")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="shop_adm_products")],
    ])

async def kb_orders_admin(db_path: str) -> InlineKeyboardMarkup:
    orders = await _recent_orders(db_path)
    rows = [
        [InlineKeyboardButton(
            text=f"{ORDER_STATUS_LABELS.get(o['status'], o['status'])} · №{o['id']} · {o['total']}{CURRENCY_SYMBOL}",
            callback_data=f"shop_adm_order:{o['id']}",
        )] for o in orders
    ]
    if not rows:
        rows.append([InlineKeyboardButton(text="Заказов пока нет", callback_data="shop_noop")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="shop_adm_back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_order_detail(order_id: int, current_status: str) -> InlineKeyboardMarkup:
    rows = []
    for status in ORDER_STATUS_ORDER:
        if status == current_status:
            continue
        rows.append([InlineKeyboardButton(
            text=f"→ {ORDER_STATUS_LABELS[status]}", callback_data=f"shop_adm_order_status:{order_id}:{status}",
        )])
    rows.append([InlineKeyboardButton(text="◀️ К заказам", callback_data="shop_adm_orders")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_admins_panel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить админа", callback_data="shop_adm_addadmin")],
        [InlineKeyboardButton(text="➖ Убрать админа", callback_data="shop_adm_removeadmin")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="shop_adm_back")],
    ])

MAX_ADMIN_REMOVE_BUTTONS = 30

def kb_remove_admins(ids: list[str]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=admin_id, callback_data=f"shop_adm_rma:{i}")] for i, admin_id in enumerate(ids)]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="shop_adm_admins")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── /start ────────────────────────────────────────────────────────────────────

@router.message(Command("start"), F.chat.type == "private")
async def cmd_start(message: Message, state: FSMContext, config: ShopCatalogConfig):
    # Same reasoning as every other template's cmd_start: /start must reset
    # any dangling mid-flow FSM state.
    await state.clear()
    admins = _load_admins(config.admins_file)
    first_time_admin = not admins
    if first_time_admin:
        _save_admins(config.admins_file, {str(message.from_user.id)})
    is_admin = str(message.from_user.id) in _load_admins(config.admins_file)
    kb = kb_main(is_admin)
    if config.welcome_image.exists():
        await message.answer_photo(FSInputFile(str(config.welcome_image)),
                                   caption=WELCOME_TEXT, parse_mode="HTML", reply_markup=kb)
    else:
        await message.answer(WELCOME_TEXT, parse_mode="HTML", reply_markup=kb)
    if first_time_admin:
        await message.answer(
            "👑 <b>Вы — администратор этого бота.</b>\n\n"
            "Кнопка «⚙️ Панель администратора» открывает управление категориями, "
            "товарами и заказами.",
            parse_mode="HTML",
        )


# ── CATALOG ───────────────────────────────────────────────────────────────────

@router.message(F.text == "🛍 Каталог")
async def catalog_start(msg: Message, state: FSMContext, config: ShopCatalogConfig):
    await state.clear()
    cats = await _active_categories(config.db_path)
    await msg.answer("📂 Выберите категорию:", reply_markup=kb_categories(cats))

@router.callback_query(F.data == "shop_cat_list")
async def cb_cat_list(cb: CallbackQuery, config: ShopCatalogConfig):
    await cb.answer()
    cats = await _active_categories(config.db_path)
    await cb.message.edit_text("📂 Выберите категорию:", reply_markup=kb_categories(cats))

@router.callback_query(F.data == "shop_cat_back")
async def cb_cat_back(cb: CallbackQuery, config: ShopCatalogConfig):
    await cb.answer()
    cats = await _active_categories(config.db_path)
    await cb.message.edit_text("📂 Выберите категорию:", reply_markup=kb_categories(cats))

@router.callback_query(F.data.startswith("shop_cat:"))
async def cb_category(cb: CallbackQuery, config: ShopCatalogConfig):
    category_id = int(cb.data.split(":", 1)[1])
    cat = await _category_row(config.db_path, category_id)
    if cat is None or not cat["active"]:
        # A single cb.answer() per callback_query_id — Telegram rejects a
        # second answerCallbackQuery for the same id, so this must be the
        # ONLY answer() call on this path, not a second one after an
        # unconditional answer() above.
        await cb.answer("Категория недоступна.", show_alert=True); return
    await cb.answer()
    products = await _active_products_in_category(config.db_path, category_id)
    await cb.message.edit_text(f"📂 <b>{_esc(cat['name'])}</b>", parse_mode="HTML",
                                reply_markup=kb_products_list(products, category_id))

@router.callback_query(F.data.startswith("shop_prod_back:"))
async def cb_product_back(cb: CallbackQuery, config: ShopCatalogConfig):
    await cb.answer()
    category_id = int(cb.data.split(":", 1)[1])
    cat = await _category_row(config.db_path, category_id)
    products = await _active_products_in_category(config.db_path, category_id)
    title = f"📂 <b>{_esc(cat['name'])}</b>" if cat else "📂 <b>Категория</b>"
    kb = kb_products_list(products, category_id)
    if cb.message.photo:
        # The screen we're navigating away from may be a photo message (see
        # cb_product_detail's photo branch below) — Telegram's editMessageText
        # cannot turn a photo message into a text message, so fall back to the
        # same delete+resend cb_product_detail already uses for that case.
        try:
            await cb.message.delete()
        except Exception as e:
            logger.debug(f"cb_product_back: failed to delete old photo message: {e}")
        await cb.message.answer(title, parse_mode="HTML", reply_markup=kb)
    else:
        await cb.message.edit_text(title, parse_mode="HTML", reply_markup=kb)

@router.callback_query(F.data.startswith("shop_prod:"))
async def cb_product_detail(cb: CallbackQuery, config: ShopCatalogConfig):
    product_id = int(cb.data.split(":", 1)[1])
    product = await _product_row(config.db_path, product_id)
    if product is None or not product["active"]:
        # Single cb.answer() per callback — see cb_category's identical fix.
        await cb.answer("Товар недоступен.", show_alert=True); return
    await cb.answer()
    text = (
        f"🛍 <b>{_esc(product['name'])}</b>\n\n"
        f"{_esc(product['description'], DESCRIPTION_MAX_LEN) if product['description'] else ''}\n\n"
        f"💰 <b>{product['price']}{CURRENCY_SYMBOL}</b>"
    )
    kb = kb_product_detail(product_id, product["category_id"])
    if product["photo_file_id"]:
        try:
            await cb.message.delete()
        except Exception as e:
            logger.debug(f"cb_product_detail: failed to delete old message: {e}")
        await cb.message.answer_photo(product["photo_file_id"], caption=text, parse_mode="HTML", reply_markup=kb)
    else:
        await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)

@router.callback_query(F.data.startswith("shop_add:"))
async def cb_add_to_cart(cb: CallbackQuery, config: ShopCatalogConfig):
    product_id = int(cb.data.split(":", 1)[1])
    product = await _product_row(config.db_path, product_id)
    if product is None or not product["active"]:
        await cb.answer("Товар больше не доступен.", show_alert=True); return
    await _add_to_cart(config.db_path, cb.from_user.id, product_id)
    await cb.answer("Добавлено в корзину ✅")

@router.callback_query(F.data == "shop_noop")
async def cb_noop(cb: CallbackQuery):
    await cb.answer()


# ── CART ──────────────────────────────────────────────────────────────────────

async def _render_cart(db_path: str, user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    rows = await _cart_rows(db_path, user_id)
    if not rows:
        return "🛒 Ваша корзина пуста.", kb_cart(rows, 0)
    lines = ["🛒 <b>Ваша корзина:</b>\n"]
    for r in rows:
        subtotal = r["price"] * r["qty"]
        lines.append(f"• {_esc(r['name'])} — {r['qty']} шт. × {r['price']}{CURRENCY_SYMBOL} = {subtotal}{CURRENCY_SYMBOL}")
    total = _cart_total(rows)
    lines.append(f"\n💰 <b>Итого: {total}{CURRENCY_SYMBOL}</b>")
    return _join_bounded(lines), kb_cart(rows, total)

@router.message(F.text == "🛒 Корзина")
async def cart_view(msg: Message, state: FSMContext, config: ShopCatalogConfig):
    await state.clear()
    text, kb = await _render_cart(config.db_path, msg.from_user.id)
    await msg.answer(text, parse_mode="HTML", reply_markup=kb)

@router.callback_query(F.data.startswith("shop_cart_inc:"))
async def cb_cart_inc(cb: CallbackQuery, config: ShopCatalogConfig):
    await cb.answer()
    product_id = int(cb.data.split(":", 1)[1])
    await _change_cart_qty(config.db_path, cb.from_user.id, product_id, +1)
    text, kb = await _render_cart(config.db_path, cb.from_user.id)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)

@router.callback_query(F.data.startswith("shop_cart_dec:"))
async def cb_cart_dec(cb: CallbackQuery, config: ShopCatalogConfig):
    await cb.answer()
    product_id = int(cb.data.split(":", 1)[1])
    await _change_cart_qty(config.db_path, cb.from_user.id, product_id, -1)
    text, kb = await _render_cart(config.db_path, cb.from_user.id)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)

@router.callback_query(F.data.startswith("shop_cart_rm:"))
async def cb_cart_rm(cb: CallbackQuery, config: ShopCatalogConfig):
    await cb.answer()
    product_id = int(cb.data.split(":", 1)[1])
    await _remove_cart_item(config.db_path, cb.from_user.id, product_id)
    text, kb = await _render_cart(config.db_path, cb.from_user.id)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


# ── CHECKOUT ──────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "shop_checkout_start")
async def cb_checkout_start(cb: CallbackQuery, state: FSMContext, config: ShopCatalogConfig):
    await cb.answer()
    rows = await _cart_rows(config.db_path, cb.from_user.id)
    if not rows:
        await cb.message.edit_text("🛒 Ваша корзина пуста.", reply_markup=kb_cart(rows, 0)); return
    await cb.message.edit_text("👤 Как к вам обращаться? Введите имя:", reply_markup=kb_cancel("shop_checkout_cancel"))
    await state.update_data(started_at=time.time())
    await state.set_state(CheckoutFlow.name)

@router.message(CheckoutFlow.name, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def checkout_name(msg: Message, state: FSMContext):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("⏳ Время ожидания истекло — начните оформление заново из корзины."); return
    name = msg.text.strip()[:NAME_MAX_LEN]
    if not name:
        await msg.answer("Имя не может быть пустым."); return
    await state.update_data(client_name=name)
    await state.set_state(CheckoutFlow.phone)
    await msg.answer("📞 Введите номер телефона для связи:", reply_markup=kb_cancel("shop_checkout_cancel"))

@router.message(CheckoutFlow.phone, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def checkout_phone(msg: Message, state: FSMContext, config: ShopCatalogConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("⏳ Время ожидания истекло — начните оформление заново из корзины."); return
    phone = _normalize_phone(msg.text.strip())
    if phone is None:
        await msg.answer("Не удалось распознать номер. Пришлите в формате: +7 900 000-00-00"); return
    rows = await _cart_rows(config.db_path, msg.from_user.id)
    if not rows:
        await state.clear()
        await msg.answer("🛒 Ваша корзина опустела — начните заново."); return
    total = _cart_total(rows)
    lines = [f"📋 <b>Подтвердите заказ:</b>\n"]
    for r in rows:
        lines.append(f"• {_esc(r['name'])} — {r['qty']} шт. × {r['price']}{CURRENCY_SYMBOL}")
    lines.append(f"\n💰 <b>Итого: {total}{CURRENCY_SYMBOL}</b>")
    lines.append(f"\n👤 {_esc(data.get('client_name', ''))}")
    lines.append(f"📞 {_esc(phone)}")
    await state.update_data(client_phone=phone)
    await state.set_state(CheckoutFlow.confirm)
    try:
        await msg.answer(_join_bounded(lines), parse_mode="HTML", reply_markup=kb_checkout_confirm())
    except Exception as e:
        logger.error(f"checkout_phone failed to show confirmation: {e}")
        await state.clear()
        await msg.answer("⚠️ Не удалось показать подтверждение. Начните оформление заново.")

@router.callback_query(CheckoutFlow.confirm, F.data == "shop_checkout_confirm")
async def cb_checkout_confirm(cb: CallbackQuery, bot: Bot, state: FSMContext, config: ShopCatalogConfig):
    await cb.answer()
    data = await state.get_data()
    client_name = data.get("client_name", "")
    client_phone = data.get("client_phone", "")
    await state.clear()
    result = await _create_order(config.db_path, cb.from_user.id, client_name, client_phone)
    if not result["ok"]:
        # Double-tap on "✅ Подтвердить заказ" (Telegram does not auto-clear an
        # inline keyboard just because the message changed) — the cart is
        # already empty from the first tap, so this is a no-op, not an error.
        try:
            await cb.message.edit_text("Эта заявка уже оформлена, либо корзина пуста.")
        except Exception:
            pass
        return
    try:
        await cb.message.edit_text(
            f"✅ <b>Заявка №{result['order_id']} оформлена!</b>\n\n"
            f"💰 Сумма: {result['total']}{CURRENCY_SYMBOL}\n\n"
            f"Мы свяжемся с вами для уточнения оплаты и доставки.",
            parse_mode="HTML",
        )
    except Exception as e:
        # The order is ALREADY committed and the cart ALREADY cleared by
        # _create_order above — a failed edit_text here (message deleted,
        # chat blocked, transient network error) must not skip notifying
        # admins below, since that's the only way anyone learns this sale
        # happened.
        logger.warning(f"cb_checkout_confirm: failed to show confirmation for order {result['order_id']}: {e}")
    for admin_id in _load_admins(config.admins_file):
        try:
            await bot.send_message(
                int(admin_id),
                OWNER_NOTIFY_TEXT + f"№{result['order_id']} · {result['total']}{CURRENCY_SYMBOL}\n"
                f"👤 {_esc(client_name)} · 📞 {_esc(client_phone)}",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning(f"order notify to admin {admin_id} failed: {e}")

@router.callback_query(F.data == "shop_checkout_cancel")
async def cb_checkout_cancel(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await cb.message.edit_text("Оформление отменено.")
    await cb.message.answer("Что дальше?", reply_markup=kb_main(False))


# ── MY ORDERS ─────────────────────────────────────────────────────────────────

@router.message(F.text == "📦 Мои заказы")
async def my_orders(msg: Message, state: FSMContext, config: ShopCatalogConfig):
    await state.clear()
    orders = await _user_orders(config.db_path, msg.from_user.id)
    if not orders:
        await msg.answer("У вас пока нет заказов."); return
    lines = ["📦 <b>Ваши заказы:</b>\n"]
    for o in orders:
        items = await _order_items(config.db_path, o["id"])
        items_text = ", ".join(f"{_esc(i['name_snapshot'])} ×{i['qty']}" for i in items)
        lines.append(
            f"№{o['id']} · {ORDER_STATUS_LABELS.get(o['status'], o['status'])} · "
            f"{o['total']}{CURRENCY_SYMBOL} · {o['created_at']}\n{items_text}\n"
        )
    await msg.answer(_join_bounded(lines), parse_mode="HTML")


# ── ADMIN PANEL ──────────────────────────────────────────────────────────────

@router.message(F.text == "⚙️ Панель администратора")
async def admin_panel_entry(msg: Message, bot: Bot, state: FSMContext, config: ShopCatalogConfig):
    if not _is_bot_admin(msg.from_user.id, config):
        await msg.answer("⛔ Нет доступа"); return
    await state.clear()
    await _replace_panel(bot, state, msg.chat.id, "⚙️ <b>Панель администратора</b>", reply_markup=kb_admin_panel_main())

@router.callback_query(F.data == "shop_adm_back")
async def cb_adm_back(cb: CallbackQuery, bot: Bot, state: FSMContext, config: ShopCatalogConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    await _clear_flow_keep_panel(state)
    await _replace_panel(bot, state, chat_id, "⚙️ <b>Панель администратора</b>", reply_markup=kb_admin_panel_main())

@router.callback_query(F.data == "shop_adm_flow_cancel")
async def cb_adm_flow_cancel(cb: CallbackQuery, bot: Bot, state: FSMContext, config: ShopCatalogConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    await _clear_flow_keep_panel(state)
    await _replace_panel(bot, state, chat_id, "⚙️ <b>Панель администратора</b>", reply_markup=kb_admin_panel_main())


# ── admin: categories CRUD ────────────────────────────────────────────────────

@router.callback_query(F.data == "shop_adm_categories")
async def cb_adm_categories(cb: CallbackQuery, bot: Bot, state: FSMContext, config: ShopCatalogConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    await _replace_panel(bot, state, chat_id, "📂 <b>Категории</b>", reply_markup=await kb_categories_admin(config.db_path))

@router.callback_query(F.data.startswith("shop_adm_cat_rm:"))
async def cb_adm_cat_rm(cb: CallbackQuery, bot: Bot, state: FSMContext, config: ShopCatalogConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    category_id = int(cb.data.split(":", 1)[1])
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute("UPDATE categories SET active=0 WHERE id=?", (category_id,))
        await db.commit()
    await _replace_panel(bot, state, chat_id, "📂 <b>Категории</b>", reply_markup=await kb_categories_admin(config.db_path))

@router.callback_query(F.data == "shop_adm_cat_add")
async def cb_adm_cat_add(cb: CallbackQuery, bot: Bot, state: FSMContext, config: ShopCatalogConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    await _replace_panel(bot, state, chat_id, "Пришлите название категории:", reply_markup=kb_cancel("shop_adm_flow_cancel"))
    await state.update_data(started_at=time.time())
    await state.set_state(AdminFlow.cat_name)

@router.message(AdminFlow.cat_name, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def adm_cat_name(msg: Message, bot: Bot, state: FSMContext, config: ShopCatalogConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await _clear_flow_keep_panel(state)
        await msg.answer("⏳ Время ожидания истекло — начните заново."); return
    if not _is_bot_admin(msg.from_user.id, config):
        await _clear_flow_keep_panel(state); return
    name = msg.text.strip()[:NAME_MAX_LEN]
    if not name:
        await msg.answer("Название не может быть пустым."); return
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute("INSERT INTO categories (name) VALUES (?)", (name,))
        await db.commit()
    await _clear_flow_keep_panel(state)
    await _replace_panel(
        bot, state, msg.chat.id, f"✅ Категория добавлена.\n\n📂 <b>Категории</b>",
        reply_markup=await kb_categories_admin(config.db_path),
    )


# ── admin: products CRUD ──────────────────────────────────────────────────────

@router.callback_query(F.data == "shop_adm_products")
async def cb_adm_products(cb: CallbackQuery, bot: Bot, state: FSMContext, config: ShopCatalogConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    await _clear_flow_keep_panel(state)
    await _replace_panel(bot, state, chat_id, "📦 <b>Товары</b>", reply_markup=await kb_products_admin(config.db_path))

@router.callback_query(F.data.startswith("shop_adm_prod_rm:"))
async def cb_adm_prod_rm(cb: CallbackQuery, bot: Bot, state: FSMContext, config: ShopCatalogConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    product_id = int(cb.data.split(":", 1)[1])
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute("UPDATE products SET active=0 WHERE id=?", (product_id,))
        await db.commit()
    await _replace_panel(bot, state, chat_id, "📦 <b>Товары</b>", reply_markup=await kb_products_admin(config.db_path))

@router.callback_query(F.data == "shop_adm_prod_add")
async def cb_adm_prod_add(cb: CallbackQuery, bot: Bot, state: FSMContext, config: ShopCatalogConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    cats = await _active_categories(config.db_path)
    if not cats:
        await _replace_panel(
            bot, state, chat_id, "⚠️ Сначала добавьте хотя бы одну категорию.\n\n📦 <b>Товары</b>",
            reply_markup=await kb_products_admin(config.db_path),
        )
        return
    await state.update_data(started_at=time.time())
    await state.set_state(AdminFlow.prod_category)
    await _replace_panel(bot, state, chat_id, "Выберите категорию товара:", reply_markup=await kb_category_pick_for_product(config.db_path))

@router.callback_query(AdminFlow.prod_category, F.data.startswith("shop_adm_prod_cat:"))
async def cb_adm_prod_cat(cb: CallbackQuery, bot: Bot, state: FSMContext, config: ShopCatalogConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    data = await state.get_data()
    if _flow_expired(data):
        await _clear_flow_keep_panel(state)
        await _replace_panel(bot, state, chat_id, "⏳ Время ожидания истекло.", reply_markup=kb_admin_panel_main()); return
    category_id = int(cb.data.split(":", 1)[1])
    await state.update_data(prod_category_id=category_id)
    await state.set_state(AdminFlow.prod_name)
    await _replace_panel(bot, state, chat_id, "Пришлите название товара:", reply_markup=kb_cancel("shop_adm_flow_cancel"))

@router.message(AdminFlow.prod_name, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def adm_prod_name(msg: Message, bot: Bot, state: FSMContext, config: ShopCatalogConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await _clear_flow_keep_panel(state)
        await msg.answer("⏳ Время ожидания истекло — начните заново."); return
    if not _is_bot_admin(msg.from_user.id, config):
        await _clear_flow_keep_panel(state); return
    name = msg.text.strip()[:NAME_MAX_LEN]
    if not name:
        await msg.answer("Название не может быть пустым."); return
    await state.update_data(prod_name=name)
    await state.set_state(AdminFlow.prod_desc)
    await _replace_panel(
        bot, state, msg.chat.id, "Пришлите описание товара (или пропустите):",
        reply_markup=kb_optional_step("shop_adm_prod_desc_skip", "shop_adm_flow_cancel"),
    )

@router.message(AdminFlow.prod_desc, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def adm_prod_desc(msg: Message, bot: Bot, state: FSMContext, config: ShopCatalogConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await _clear_flow_keep_panel(state)
        await msg.answer("⏳ Время ожидания истекло — начните заново."); return
    if not _is_bot_admin(msg.from_user.id, config):
        await _clear_flow_keep_panel(state); return
    await _advance_to_prod_price(msg.bot, state, msg.chat.id, msg.text.strip()[:DESCRIPTION_MAX_LEN])

@router.callback_query(AdminFlow.prod_desc, F.data == "shop_adm_prod_desc_skip")
async def cb_adm_prod_desc_skip(cb: CallbackQuery, bot: Bot, state: FSMContext, config: ShopCatalogConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    data = await state.get_data()
    if _flow_expired(data):
        await _clear_flow_keep_panel(state)
        await _replace_panel(bot, state, chat_id, "⏳ Время ожидания истекло.", reply_markup=kb_admin_panel_main()); return
    await _advance_to_prod_price(bot, state, chat_id, None)

async def _advance_to_prod_price(bot: Bot, state: FSMContext, chat_id: int, description: str | None) -> None:
    # started_at is NOT reset here — same single-flow-wide-timer convention as
    # every other multi-step flow (e.g. booking_fitness.py's schedule/plan
    # flows): it's set once when the flow starts (cb_adm_prod_add) and never
    # refreshed per-step, so the whole category->name->description->price->
    # photo flow shares one FLOW_TIMEOUT_SECONDS deadline, not a rolling one.
    await state.update_data(prod_desc=description)
    await state.set_state(AdminFlow.prod_price)
    await _replace_panel(bot, state, chat_id, f"Пришлите цену в {CURRENCY_SYMBOL} (целое число):", reply_markup=kb_cancel("shop_adm_flow_cancel"))

@router.message(AdminFlow.prod_price, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def adm_prod_price(msg: Message, bot: Bot, state: FSMContext, config: ShopCatalogConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await _clear_flow_keep_panel(state)
        await msg.answer("⏳ Время ожидания истекло — начните заново."); return
    if not _is_bot_admin(msg.from_user.id, config):
        await _clear_flow_keep_panel(state); return
    try:
        price = int(msg.text.strip())
        if price <= 0 or price > PRICE_MAX:
            raise ValueError
    except ValueError:
        await msg.answer(f"Введите целое число от 1 до {PRICE_MAX}, например: 990"); return
    await state.update_data(prod_price=price)
    await state.set_state(AdminFlow.prod_photo)
    await _replace_panel(
        bot, state, msg.chat.id, "📷 Пришлите фото товара (или пропустите):",
        reply_markup=kb_optional_step("shop_adm_prod_photo_skip", "shop_adm_flow_cancel"),
    )

@router.message(AdminFlow.prod_photo, F.chat.type == "private", F.photo)
async def adm_prod_photo(msg: Message, state: FSMContext, config: ShopCatalogConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await _clear_flow_keep_panel(state)
        await msg.answer("⏳ Время ожидания истекло — начните заново."); return
    if not _is_bot_admin(msg.from_user.id, config):
        await _clear_flow_keep_panel(state); return
    await _finish_add_product(msg.bot, state, msg.chat.id, config, msg.photo[-1].file_id)

@router.callback_query(AdminFlow.prod_photo, F.data == "shop_adm_prod_photo_skip")
async def cb_adm_prod_photo_skip(cb: CallbackQuery, bot: Bot, state: FSMContext, config: ShopCatalogConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    data = await state.get_data()
    if _flow_expired(data):
        await _clear_flow_keep_panel(state)
        await _replace_panel(bot, state, chat_id, "⏳ Время ожидания истекло.", reply_markup=kb_admin_panel_main()); return
    await _finish_add_product(bot, state, chat_id, config, None)

async def _finish_add_product(bot: Bot, state: FSMContext, chat_id: int, config: ShopCatalogConfig, photo_file_id: str | None) -> None:
    data = await state.get_data()
    category_id = data.get("prod_category_id")
    name = data.get("prod_name", "").strip()
    if not category_id or not name:
        await _clear_flow_keep_panel(state)
        await _replace_panel(bot, state, chat_id, "📦 <b>Товары</b>", reply_markup=await kb_products_admin(config.db_path))
        return
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute(
            "INSERT INTO products (category_id, name, description, price, photo_file_id) VALUES (?,?,?,?,?)",
            (category_id, name, data.get("prod_desc"), data.get("prod_price"), photo_file_id),
        )
        await db.commit()
    await _clear_flow_keep_panel(state)
    await _replace_panel(
        bot, state, chat_id, f"✅ Товар добавлен.\n\n📦 <b>Товары</b>",
        reply_markup=await kb_products_admin(config.db_path),
    )


# ── admin: edit an existing product ───────────────────────────────────────────

@router.callback_query(F.data.startswith("shop_adm_prod_edit:"))
async def cb_adm_prod_edit(cb: CallbackQuery, bot: Bot, state: FSMContext, config: ShopCatalogConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    product_id = int(cb.data.split(":", 1)[1])
    product = await _product_row(config.db_path, product_id)
    if product is None:
        await _replace_panel(bot, state, chat_id, "📦 <b>Товары</b>", reply_markup=await kb_products_admin(config.db_path))
        return
    await _clear_flow_keep_panel(state)
    await _replace_panel(
        bot, state, chat_id, f"✏️ <b>{_esc(product['name'])}</b>\nВыберите, что изменить:",
        reply_markup=kb_product_edit_menu(product_id),
    )

@router.callback_query(F.data.startswith("shop_adm_prod_field:"))
async def cb_adm_prod_field(cb: CallbackQuery, bot: Bot, state: FSMContext, config: ShopCatalogConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    _, product_id_raw, field = cb.data.split(":", 2)
    product_id = int(product_id_raw)
    if await _product_row(config.db_path, product_id) is None:
        await _replace_panel(bot, state, chat_id, "📦 <b>Товары</b>", reply_markup=await kb_products_admin(config.db_path))
        return
    prompts = {
        "name": "Пришлите новое название:",
        "description": "Пришлите новое описание:",
        "price": f"Пришлите новую цену в {CURRENCY_SYMBOL} (целое число):",
        "photo": "Пришлите новое фото:",
    }
    if field not in prompts:
        return
    await state.update_data(edit_product_id=product_id, edit_field=field, started_at=time.time())
    await state.set_state(AdminFlow.edit_value)
    await _replace_panel(bot, state, chat_id, prompts[field], reply_markup=kb_cancel("shop_adm_flow_cancel"))

@router.message(AdminFlow.edit_value, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def adm_edit_value_text(msg: Message, bot: Bot, state: FSMContext, config: ShopCatalogConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await _clear_flow_keep_panel(state)
        await msg.answer("⏳ Время ожидания истекло — начните заново."); return
    if not _is_bot_admin(msg.from_user.id, config):
        await _clear_flow_keep_panel(state); return
    field = data.get("edit_field")
    product_id = data.get("edit_product_id")
    if field == "photo" or not product_id:
        return  # photo field is handled by the photo-message handler below
    value = msg.text.strip()
    if field == "name":
        value = value[:NAME_MAX_LEN]
        if not value:
            await msg.answer("Название не может быть пустым."); return
        column, param = "name", value
    elif field == "description":
        value = value[:DESCRIPTION_MAX_LEN]
        column, param = "description", value
    elif field == "price":
        try:
            price = int(value)
            if price <= 0 or price > PRICE_MAX:
                raise ValueError
        except ValueError:
            await msg.answer(f"Введите целое число от 1 до {PRICE_MAX}."); return
        column, param = "price", price
    else:
        return
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute(f"UPDATE products SET {column}=? WHERE id=?", (param, product_id))
        await db.commit()
    await _clear_flow_keep_panel(state)
    await _replace_panel(bot, state, msg.chat.id, "✅ Изменено.\n\n📦 <b>Товары</b>", reply_markup=await kb_products_admin(config.db_path))

@router.message(AdminFlow.edit_value, F.chat.type == "private", F.photo)
async def adm_edit_value_photo(msg: Message, bot: Bot, state: FSMContext, config: ShopCatalogConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await _clear_flow_keep_panel(state)
        await msg.answer("⏳ Время ожидания истекло — начните заново."); return
    if not _is_bot_admin(msg.from_user.id, config):
        await _clear_flow_keep_panel(state); return
    if data.get("edit_field") != "photo" or not data.get("edit_product_id"):
        return
    product_id = data["edit_product_id"]
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute("UPDATE products SET photo_file_id=? WHERE id=?", (msg.photo[-1].file_id, product_id))
        await db.commit()
    await _clear_flow_keep_panel(state)
    await _replace_panel(bot, state, msg.chat.id, "✅ Изменено.\n\n📦 <b>Товары</b>", reply_markup=await kb_products_admin(config.db_path))


# ── admin: orders ──────────────────────────────────────────────────────────────

@router.callback_query(F.data == "shop_adm_orders")
async def cb_adm_orders(cb: CallbackQuery, bot: Bot, state: FSMContext, config: ShopCatalogConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    await _clear_flow_keep_panel(state)
    await _replace_panel(bot, state, chat_id, "🧾 <b>Заказы</b>", reply_markup=await kb_orders_admin(config.db_path))

async def _render_order_detail(bot: Bot, state: FSMContext, chat_id: int, config: ShopCatalogConfig, order_id: int) -> None:
    """Shared by cb_adm_order_detail and cb_adm_order_status. Review-found
    blocker: cb_adm_order_status used to call cb_adm_order_detail(cb, ...)
    directly, reusing the SAME CallbackQuery — that handler unconditionally
    calls cb.answer() again (Telegram rejects a second answerCallbackQuery for
    the same callback_query_id) AND re-parses cb.data expecting
    "shop_adm_order:<id>" while it actually still held
    "shop_adm_order_status:<id>:<status>", so `int(cb.data.split(":",1)[1])`
    parsed "<id>:<status>" as an int and raised. Factoring the render into a
    plain helper that takes chat_id/order_id (not the CallbackQuery) removes
    both bugs at once — no reused cb.answer(), no re-parsed cb.data."""
    order = await _order_row(config.db_path, order_id)
    if order is None:
        await _replace_panel(bot, state, chat_id, "🧾 <b>Заказы</b>", reply_markup=await kb_orders_admin(config.db_path))
        return
    items = await _order_items(config.db_path, order_id)
    items_text = "\n".join(f"• {_esc(i['name_snapshot'])} — {i['qty']} шт. × {i['price_snapshot']}{CURRENCY_SYMBOL}" for i in items)
    text = (
        f"🧾 <b>Заказ №{order['id']}</b>\n"
        f"Статус: {ORDER_STATUS_LABELS.get(order['status'], order['status'])}\n"
        f"👤 {_esc(order['client_name'] or '—')} · 📞 {_esc(order['client_phone'] or '—')}\n"
        f"🗓 {order['created_at']}\n\n"
        f"{items_text}\n\n"
        f"💰 <b>Итого: {order['total']}{CURRENCY_SYMBOL}</b>"
    )
    await _replace_panel(bot, state, chat_id, text, reply_markup=kb_order_detail(order_id, order["status"]))

@router.callback_query(F.data.startswith("shop_adm_order:"))
async def cb_adm_order_detail(cb: CallbackQuery, bot: Bot, state: FSMContext, config: ShopCatalogConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    order_id = int(cb.data.split(":", 1)[1])
    await _render_order_detail(bot, state, chat_id, config, order_id)

@router.callback_query(F.data.startswith("shop_adm_order_status:"))
async def cb_adm_order_status(cb: CallbackQuery, bot: Bot, state: FSMContext, config: ShopCatalogConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    _, order_id_raw, status = cb.data.split(":", 2)
    if status not in ORDER_STATUS_LABELS:
        return
    order_id = int(order_id_raw)
    if await _order_row(config.db_path, order_id) is None:
        await _replace_panel(bot, state, chat_id, "🧾 <b>Заказы</b>", reply_markup=await kb_orders_admin(config.db_path))
        return
    await _set_order_status(config.db_path, order_id, status)
    logger.info(f"cb_adm_order_status: order_id={order_id} status={status} by admin {cb.from_user.id}")
    await _render_order_detail(bot, state, chat_id, config, order_id)


# ── admin: bot-admins panel (same pattern as booking_fitness.py/moderator.py) ─

async def _admins_list_text(config: ShopCatalogConfig) -> str:
    ids = sorted(_load_admins(config.admins_file))
    if not ids:
        return "👥 Пусто"
    return _join_bounded(["👥 <b>Администраторы бота:</b>\n"] + [f"• <code>{_esc(i)}</code>" for i in ids])

@router.callback_query(F.data == "shop_adm_admins")
async def cb_adm_admins(cb: CallbackQuery, bot: Bot, state: FSMContext, config: ShopCatalogConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    await _clear_flow_keep_panel(state)
    await _replace_panel(bot, state, chat_id, await _admins_list_text(config), reply_markup=kb_admins_panel())

@router.callback_query(F.data == "shop_adm_addadmin")
async def cb_adm_addadmin(cb: CallbackQuery, bot: Bot, state: FSMContext, config: ShopCatalogConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    await _replace_panel(
        bot, state, chat_id, "Пришлите Telegram ID пользователя, которого нужно сделать администратором:",
        reply_markup=kb_cancel("shop_adm_admins"),
    )
    await state.update_data(started_at=time.time())
    await state.set_state(AdminFlow.add_admin)

@router.message(AdminFlow.add_admin, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def adm_add_admin(msg: Message, bot: Bot, state: FSMContext, config: ShopCatalogConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await _clear_flow_keep_panel(state)
        await msg.answer("⏳ Время ожидания истекло — начните заново кнопкой «👥 Админы бота»."); return
    if not _is_bot_admin(msg.from_user.id, config):
        await _clear_flow_keep_panel(state)
        await msg.answer("⛔ Нет доступа"); return
    text = msg.text.strip()
    if not _valid_telegram_id(text):
        await msg.answer("Пришлите числовой Telegram ID."); return
    ids = _load_admins(config.admins_file); ids.add(text); _save_admins(config.admins_file, ids)
    logger.info(f"adm_add_admin: {text} added as bot admin by {msg.from_user.id}")
    await _clear_flow_keep_panel(state)
    await msg.answer(f"✅ <code>{_esc(text)}</code> добавлен.", parse_mode="HTML")
    await _replace_panel(bot, state, msg.chat.id, await _admins_list_text(config), reply_markup=kb_admins_panel())

@router.callback_query(F.data == "shop_adm_removeadmin")
async def cb_adm_removeadmin(cb: CallbackQuery, bot: Bot, state: FSMContext, config: ShopCatalogConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    ids = sorted(_load_admins(config.admins_file))
    if len(ids) <= 1:
        await _replace_panel(
            bot, state, chat_id, "⚠️ Нельзя удалить единственного администратора.\n\n" + await _admins_list_text(config),
            reply_markup=kb_admins_panel(),
        )
        return
    if len(ids) <= MAX_ADMIN_REMOVE_BUTTONS:
        await state.update_data(remove_admin_ids=ids)
        await _replace_panel(bot, state, chat_id, "Выберите администратора для удаления:", reply_markup=kb_remove_admins(ids))
    else:
        await state.update_data(started_at=time.time())
        await state.set_state(AdminFlow.remove_admin_pick)
        await _replace_panel(bot, state, chat_id, "Пришлите Telegram ID администратора для удаления:", reply_markup=kb_cancel("shop_adm_admins"))

@router.callback_query(F.data.startswith("shop_adm_rma:"))
async def cb_adm_rma(cb: CallbackQuery, bot: Bot, state: FSMContext, config: ShopCatalogConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    data = await state.get_data()
    ids = data.get("remove_admin_ids", [])
    try:
        idx = int(cb.data.split(":", 1)[1])
    except ValueError:
        idx = -1
    if idx < 0 or idx >= len(ids):
        await _replace_panel(bot, state, chat_id, await _admins_list_text(config), reply_markup=kb_admins_panel()); return
    target = ids[idx]
    current = _load_admins(config.admins_file)
    if target in current and len(current) <= 1:
        await _replace_panel(
            bot, state, chat_id, "⚠️ Нельзя удалить единственного администратора.\n\n" + await _admins_list_text(config),
            reply_markup=kb_admins_panel(),
        )
        return
    current.discard(target); _save_admins(config.admins_file, current)
    logger.info(f"cb_adm_rma: {target} removed as bot admin by {cb.from_user.id}")
    await _clear_flow_keep_panel(state)
    await _replace_panel(bot, state, chat_id, await _admins_list_text(config), reply_markup=kb_admins_panel())

@router.message(AdminFlow.remove_admin_pick, F.chat.type == "private", F.text, ~F.text.startswith("/"))
async def adm_remove_admin_text(msg: Message, bot: Bot, state: FSMContext, config: ShopCatalogConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await _clear_flow_keep_panel(state)
        await msg.answer("⏳ Время ожидания истекло — начните заново."); return
    if not _is_bot_admin(msg.from_user.id, config):
        await _clear_flow_keep_panel(state); return
    text = msg.text.strip()
    if not _valid_telegram_id(text):
        await msg.answer("Пришлите числовой Telegram ID."); return
    ids = _load_admins(config.admins_file)
    if text in ids and len(ids) <= 1:
        await msg.answer("⚠️ Нельзя удалить единственного администратора."); return
    ids.discard(text); _save_admins(config.admins_file, ids)
    logger.info(f"adm_remove_admin_text: {text} removed as bot admin by {msg.from_user.id}")
    await _clear_flow_keep_panel(state)
    await _replace_panel(bot, state, msg.chat.id, await _admins_list_text(config), reply_markup=kb_admins_panel())


# ── admin: stats ─────────────────────────────────────────────────────────────

@router.callback_query(F.data == "shop_adm_stats")
async def cb_adm_stats(cb: CallbackQuery, bot: Bot, state: FSMContext, config: ShopCatalogConfig):
    chat_id = _panel_chat_id(cb)
    if chat_id is None:
        await cb.answer(); return
    await cb.answer()
    if not _is_bot_admin(cb.from_user.id, config):
        return
    stats = await _stats(config.db_path)
    top_lines = "\n".join(f"  • {_esc(t['name'])} — {t['units']} шт." for t in stats["top"]) or "  —"
    text = (
        f"📊 <b>Статистика</b>\n\n"
        f"🧾 Всего заказов: {stats['total_orders']}\n"
        f"💰 Выручка (без отменённых): {stats['revenue']}{CURRENCY_SYMBOL}\n\n"
        f"🏆 Топ товаров:\n{top_lines}"
    )
    await _replace_panel(bot, state, chat_id, text, reply_markup=kb_admin_panel_main())


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
