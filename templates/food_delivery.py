# TEMPLATE: food_delivery
# USE FOR: полноценная доставка еды из кафе/ресторана — меню по категориям со
#          стоп-листом, корзина, зона доставки и время (сейчас/к часу),
#          статус-флоу принят→готовится→у курьера→доставлено с уведомлением
#          клиента, роспись заказов курьерам, программа лояльности (баллы) и
#          промокоды, оплата онлайн или наличными курьеру. НЕ shop_catalog
#          (та витрина без ограничивающих факторов зоны/времени и без
#          курьеров/кухни) и НЕ delivery_tracker (та — курьерская логистика
#          БЕЗ каталога товаров, просто "что и куда доставить")
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
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, Message

from db.database import get_bot_features
from features.payments import create_invoice, init_payments_tables

# ── CUSTOMIZE ────────────────────────────────────────────────────────────────
# Same status as every other template's CUSTOMIZE block: per-file source-text
# customization Claude edits when generating a specific bot, not per-bot
# runtime state (that's config.db_path/admins_file below). Категории/блюда/
# курьеры/промокоды — runtime-данные, добавляются через панель самого бота.
BOT_DESCRIPTION = (
    "Доставка еды: меню по категориям, корзина, зона и время доставки, "
    "статус-флоу заказа с уведомлениями, курьеры, баллы лояльности и "
    "промокоды, оплата онлайн или наличными курьеру."
)
WELCOME_TEXT = (
    "🍔 <b>Доставка еды</b>\n\n"
    "Панель администратора: меню, заказы, кухня, курьеры, промокоды, "
    "аналитика.\n\nВыберите раздел:"
)
CLIENT_WELCOME_TEXT = (
    "👋 Добро пожаловать!\n\n"
    "Смотрите меню, собирайте заказ и оформляйте доставку — "
    "мы сообщим о каждом шаге."
)
CURRENCY_SYMBOL = "₽"
# Зоны доставки — клиент выбирает одну из них при оформлении заказа. Пустой
# адрес прямо на карте не проверяется (см. docs/TEMPLATES_domain_design_60.md
# "menu_delivery" — текстовая зона, не геолокация), только сам факт выбора
# зоны как ограничивающего фактора.
DELIVERY_ZONES = ["Центр", "Северный район", "Южный район", "Восточный район"]
MIN_ORDER_AMOUNT = 500  # минимальная сумма заказа для оформления доставки
LOYALTY_POINTS_RATE = 100  # ₽ потрачено = 1 балл, начисляется при статусе "Доставлено"
NAME_MAX_LEN = 100
DESCRIPTION_MAX_LEN = 500
ADDRESS_MAX_LEN = 300
COMMENT_MAX_LEN = 500
PRICE_MAX = 1_000_000
# ── END CUSTOMIZE ─────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
router = Router()


# ── status flow ──────────────────────────────────────────────────────────────
# Forward-only, same shape as templates/delivery_tracker.py's/orders_tracker.py's
# STATUS_TRANSITIONS: no backward moves. "cancelled" is reachable from any
# non-terminal status as a side-branch.
STATUS_TRANSITIONS = {
    "new": ["accepted", "cancelled"],
    "accepted": ["cooking", "cancelled"],
    "cooking": ["courier", "cancelled"],
    "courier": ["delivered", "cancelled"],
    "delivered": [],
    "cancelled": [],
}
TERMINAL_STATUSES = ("delivered", "cancelled")
STATUS_LABELS = {
    "new": "🆕 Новый",
    "accepted": "✅ Принят",
    "cooking": "👨‍🍳 Готовится",
    "courier": "🚴 У курьера",
    "delivered": "🏁 Доставлено",
    "cancelled": "❌ Отменён",
}
STATUS_LABEL_PLAIN = {
    "new": "Новый",
    "accepted": "Принят",
    "cooking": "Готовится",
    "courier": "У курьера",
    "delivered": "Доставлено",
    "cancelled": "Отменён",
}


# ── config ───────────────────────────────────────────────────────────────────
# Same pattern as every other template — see docs/STAGE2_DESIGN.md.

@dataclass
class FoodDeliveryConfig:
    bot_name: str
    db_path: str
    admins_file: Path
    welcome_image: Path
    display_name: str | None = None
    group_chat_id: str | None = None
    # None in standalone/subprocess mode; set from bots.id in webhook mode —
    # required by features/payments.py's create_invoice() (bot_id, not just
    # db_path), same convention as templates/orders_tracker.py's Config.bot_id.
    bot_id: int | None = None


def _paths_for(name: str, data_dir: Path) -> FoodDeliveryConfig:
    return FoodDeliveryConfig(
        bot_name=name,
        db_path=str(data_dir / f"{name}_data.db"),
        admins_file=data_dir / f"admins_{name}.json",
        welcome_image=data_dir / "bot_images" / f"{name}.jpg",
    )


def config_from_env() -> FoodDeliveryConfig:
    """Standalone/subprocess mode."""
    name = Path(__file__).stem
    data_dir = Path(os.getenv("DATA_DIR", "./data"))
    data_dir.mkdir(exist_ok=True)
    return _paths_for(name, data_dir)


def config_from_bot_row(bot_row: dict, data_dir: Path) -> FoodDeliveryConfig:
    """Webhook runtime mode. Paths built from bot_row["bot_id"] (bots.id, the
    physically unique AUTOINCREMENT PK) — NOT bot_row["name"] — same reasoning
    as every other template's config_from_bot_row (see docs/STAGE2_DESIGN.md
    "Изоляция по bots.id")."""
    bot_id = bot_row["bot_id"]
    config = FoodDeliveryConfig(
        bot_name=bot_row["name"],
        db_path=str(data_dir / f"bot_{bot_id}_data.db"),
        admins_file=data_dir / f"admins_{bot_id}.json",
        welcome_image=data_dir / "bot_images" / f"bot_{bot_id}.jpg",
    )
    config.display_name = bot_row.get("display_name")
    config.group_chat_id = bot_row.get("group_chat_id")
    config.bot_id = bot_id
    return config


class ConfigMiddleware(BaseMiddleware):
    """Injects this bot's FoodDeliveryConfig into data["config"]."""

    def __init__(self, config: FoodDeliveryConfig) -> None:
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

def _is_admin(user_id: int, config: FoodDeliveryConfig) -> bool:
    return str(user_id) in _load_admins(config.admins_file)


def _esc(value, max_len: int = 500) -> str:
    """HTML-escapes AND length-bounds any user-supplied text before it goes into
    a parse_mode="HTML" message — same helper/rationale as every other
    template's _esc()."""
    text = str(value) if value is not None else ""
    if len(text) > max_len:
        text = text[:max_len] + "…"
    return html.escape(text)


def _join_bounded(lines: list[str], limit: int = 3500) -> str:
    """Joins lines with a length budget, dropping only WHOLE trailing lines."""
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


def _normalize_phone(raw: str) -> str | None:
    """Same RU-phone formula reused verbatim across every template."""
    digits = re.sub(r"[^\d]", "", raw)
    if len(digits) == 11 and digits[0] in ("7", "8"):
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    else:
        return None
    return f"+{digits[0]} ({digits[1:4]}) {digits[4:7]}-{digits[7:9]}-{digits[9:11]}"


def _valid_price(text: str) -> int | None:
    try:
        price = int(text.strip())
    except ValueError:
        return None
    if price <= 0 or price > PRICE_MAX:
        return None
    return price


def _valid_admin_id(text: str) -> bool:
    if not (bool(text) and text.isascii() and text.isdigit() and len(text) <= 15):
        return False
    return int(text) > 0 and str(int(text)) == text


def _valid_time_text(text: str) -> str | None:
    """Loose "к какому часу" format check — HH:MM, 24h. Deliberately not a
    real time-parsing library: the brief calls this a text field ("к
    определённому часу"), not a scheduling engine."""
    m = re.match(r"^([01]?\d|2[0-3]):([0-5]\d)$", text.strip())
    if not m:
        return None
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def _valid_promo_code(text: str) -> str | None:
    code = text.strip().upper()
    if not (1 <= len(code) <= 30) or not re.match(r"^[A-Z0-9_-]+$", code):
        return None
    return code


def _valid_discount_percent(text: str) -> int | None:
    try:
        pct = int(text.strip())
    except ValueError:
        return None
    if pct <= 0 or pct > 100:
        return None
    return pct


def _valid_points(text: str) -> int | None:
    try:
        points = int(text.strip())
    except ValueError:
        return None
    if points < 0:
        return None
    return points


# ── mini-app config (see docs/MINIAPP_DESIGN.md,
# docs/MINIAPP_ROLE_SCOPING_DESIGN.md for the role_filter contract) ──────────
# `orders` has NO role_filter — per that doc, a resource with no role_filter
# requires bot-admin membership (owner sees ALL orders, unfiltered). `my_orders`
# targets the SAME table with an ownership-only role_filter so a regular
# client viewer sees only their own rows — the two resources compose to give
# exactly "owner видит все заказы... клиент только свои заказы" from the
# brief without needing a roles table (there is none here — every non-admin
# viewer just owns their own client_user_id rows).
miniapp_config = {
    "resources": [
        {
            "name": "menu_items",
            "table": "menu_items",
            "order_by": "id DESC",
            "creatable": False,
            "title": "Меню",
            "titleField": "name",
            "fields": [
                {"name": "category_id", "label": "ID категории", "kind": "number", "list": False, "detail": True, "create": False},
                {"name": "name", "label": "Название", "kind": "text", "list": True, "detail": True, "create": False},
                {"name": "description", "label": "Состав", "kind": "text", "list": False, "detail": True, "create": False},
                {"name": "price", "label": "Цена", "kind": "number", "list": True, "detail": True, "create": False},
                {"name": "available", "label": "В наличии", "kind": "bool", "list": True, "detail": True, "create": False},
                {"name": "active", "label": "Активно", "kind": "bool", "list": False, "detail": True, "create": False},
            ],
        },
        {
            "name": "orders",
            "table": "orders",
            "order_by": "created_at DESC",
            "creatable": False,
            "title": "Заказы (все)",
            "titleField": "delivery_address",
            "fields": [
                {"name": "client_name", "label": "Клиент", "kind": "text", "list": True, "detail": True, "create": False},
                {"name": "client_phone", "label": "Телефон", "kind": "text", "list": False, "detail": True, "create": False},
                {"name": "delivery_zone", "label": "Зона", "kind": "text", "list": True, "detail": True, "create": False},
                {"name": "delivery_address", "label": "Адрес", "kind": "text", "list": False, "detail": True, "create": False},
                {"name": "delivery_time_mode", "label": "Время", "kind": "text", "list": False, "detail": True, "create": False},
                {"name": "delivery_time_text", "label": "К какому часу", "kind": "text", "list": False, "detail": True, "create": False},
                {"name": "status", "label": "Статус", "kind": "status", "list": True, "detail": True, "create": False},
                {"name": "payment_method", "label": "Оплата", "kind": "text", "list": True, "detail": True, "create": False},
                {"name": "total", "label": "Сумма", "kind": "number", "list": True, "detail": True, "create": False},
                {"name": "courier_id", "label": "ID курьера", "kind": "number", "list": False, "detail": True, "create": False},
                {"name": "created_at", "label": "Создан", "kind": "date", "list": False, "detail": True, "create": False},
            ],
        },
        {
            "name": "my_orders",
            "table": "orders",
            "order_by": "created_at DESC",
            "creatable": False,
            "title": "Мои заказы",
            "titleField": "delivery_address",
            "fields": [
                {"name": "delivery_zone", "label": "Зона", "kind": "text", "list": True, "detail": True, "create": False},
                {"name": "delivery_address", "label": "Адрес", "kind": "text", "list": False, "detail": True, "create": False},
                {"name": "status", "label": "Статус", "kind": "status", "list": True, "detail": True, "create": False},
                {"name": "total", "label": "Сумма", "kind": "number", "list": True, "detail": True, "create": False},
                {"name": "created_at", "label": "Создан", "kind": "date", "list": False, "detail": True, "create": False},
            ],
            "role_filter": {"where": "client_user_id = :telegram_user_id"},
        },
        {
            "name": "couriers",
            "table": "couriers",
            "order_by": "id DESC",
            "creatable": False,
            "title": "Курьеры",
            "titleField": "name",
            "fields": [
                {"name": "name", "label": "Имя", "kind": "text", "list": True, "detail": True, "create": False},
                {"name": "phone", "label": "Телефон", "kind": "text", "list": True, "detail": True, "create": False},
                {"name": "active", "label": "Активен", "kind": "bool", "list": True, "detail": True, "create": False},
            ],
        },
    ],
}


# ── db ────────────────────────────────────────────────────────────────────────
# DESIGN NOTE — cart is a DB table, not FSM state, same rationale as
# templates/shop_catalog.py's cart_items: MemoryStorage does not survive a
# process restart, a bot_<id>_data.db row does.
#
# DESIGN NOTE — order_items snapshots name/price at checkout time, same
# rationale as shop_catalog.py's order_items: a later admin edit to a menu
# item's name/price must never rewrite the historical record of what was
# actually ordered.

async def init_db(db_path: str):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS menu_categories (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL,
                active     INTEGER DEFAULT 1
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS menu_items (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                category_id   INTEGER NOT NULL REFERENCES menu_categories(id),
                name          TEXT NOT NULL,
                description   TEXT,
                price         INTEGER NOT NULL,
                photo_file_id TEXT,
                available     INTEGER NOT NULL DEFAULT 1,
                active        INTEGER NOT NULL DEFAULT 1
            )
        """)
        # Cart, same shape/rationale as shop_catalog.py's cart_items —
        # UNIQUE(user_id, item_id) makes "add to cart" an idempotent
        # increment (ON CONFLICT DO UPDATE qty=qty+1).
        await db.execute("""
            CREATE TABLE IF NOT EXISTS cart_items (
                user_id  INTEGER NOT NULL,
                item_id  INTEGER NOT NULL REFERENCES menu_items(id),
                qty      INTEGER NOT NULL,
                UNIQUE(user_id, item_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS couriers (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                name    TEXT NOT NULL,
                phone   TEXT,
                active  INTEGER NOT NULL DEFAULT 1
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS promo_codes (
                code             TEXT PRIMARY KEY,
                discount_percent INTEGER NOT NULL,
                active           INTEGER NOT NULL DEFAULT 1,
                uses_left        INTEGER
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS loyalty_points (
                user_id  INTEGER PRIMARY KEY,
                balance  INTEGER NOT NULL DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                client_user_id      INTEGER NOT NULL,
                client_name         TEXT,
                client_phone        TEXT,
                delivery_zone       TEXT NOT NULL,
                delivery_address    TEXT NOT NULL,
                delivery_time_mode  TEXT NOT NULL CHECK(delivery_time_mode IN ('asap','scheduled')),
                delivery_time_text  TEXT,
                comment             TEXT,
                status              TEXT NOT NULL DEFAULT 'new'
                                    CHECK(status IN ('new','accepted','cooking','courier','delivered','cancelled')),
                payment_method      TEXT NOT NULL DEFAULT 'cash' CHECK(payment_method IN ('cash','online')),
                paid                INTEGER NOT NULL DEFAULT 0,
                payment_payload     TEXT,
                courier_id          INTEGER REFERENCES couriers(id),
                subtotal            INTEGER NOT NULL,
                promo_code          TEXT,
                discount_amount     INTEGER NOT NULL DEFAULT 0,
                points_used         INTEGER NOT NULL DEFAULT 0,
                points_earned       INTEGER NOT NULL DEFAULT 0,
                total               INTEGER NOT NULL,
                created_at          TEXT DEFAULT (datetime('now','localtime')),
                updated_at          TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS order_items (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id       INTEGER NOT NULL REFERENCES orders(id),
                item_id        INTEGER,
                name_snapshot  TEXT NOT NULL,
                price_snapshot INTEGER NOT NULL,
                qty            INTEGER NOT NULL
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_fd_orders_client ON orders(client_user_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_fd_orders_status ON orders(status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_fd_items_category ON menu_items(category_id)")
        await db.commit()
    # Own per-bot db_path (never a separate file) — same convention as
    # every other payments-compatible template's init_db calling this.
    await init_payments_tables(db_path)


# ── menu queries ─────────────────────────────────────────────────────────────

async def _active_categories(db_path: str) -> list[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute("SELECT * FROM menu_categories WHERE active=1 ORDER BY name")).fetchall()
        return [dict(r) for r in rows]

async def _category_row(db_path: str, category_id: int) -> dict | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute("SELECT * FROM menu_categories WHERE id=?", (category_id,))).fetchone()
        return dict(row) if row else None

async def _items_in_category(db_path: str, category_id: int, only_available: bool) -> list[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        sql = "SELECT * FROM menu_items WHERE category_id=? AND active=1"
        if only_available:
            sql += " AND available=1"
        rows = await (await db.execute(sql + " ORDER BY name", (category_id,))).fetchall()
        return [dict(r) for r in rows]

async def _all_items_admin(db_path: str) -> list[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT mi.*, mc.name AS category_name FROM menu_items mi "
            "JOIN menu_categories mc ON mi.category_id=mc.id WHERE mi.active=1 ORDER BY mc.name, mi.name"
        )).fetchall()
        return [dict(r) for r in rows]

async def _item_row(db_path: str, item_id: int) -> dict | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute("SELECT * FROM menu_items WHERE id=?", (item_id,))).fetchone()
        return dict(row) if row else None


# ── cart ──────────────────────────────────────────────────────────────────────

async def _cart_rows(db_path: str, user_id: int) -> list[dict]:
    """Cart lines joined with CURRENT item name/price/availability — same
    "hide what's no longer sellable" rule as shop_catalog.py's _cart_rows: an
    item pulled off the stop-list (available=0) or deactivated after being
    added to a cart must not still be checkable-out."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT ci.item_id, ci.qty, mi.name, mi.price FROM cart_items ci "
            "JOIN menu_items mi ON ci.item_id=mi.id "
            "WHERE ci.user_id=? AND mi.active=1 AND mi.available=1 ORDER BY mi.name",
            (user_id,),
        )).fetchall()
        return [dict(r) for r in rows]

def _cart_total(rows: list[dict]) -> int:
    return sum(r["price"] * r["qty"] for r in rows)

async def _add_to_cart(db_path: str, user_id: int, item_id: int) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO cart_items (user_id, item_id, qty) VALUES (?,?,1) "
            "ON CONFLICT(user_id, item_id) DO UPDATE SET qty = qty + 1",
            (user_id, item_id),
        )
        await db.commit()

async def _change_cart_qty(db_path: str, user_id: int, item_id: int, delta: int) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE cart_items SET qty = qty + ? WHERE user_id=? AND item_id=?", (delta, user_id, item_id),
        )
        await db.execute("DELETE FROM cart_items WHERE user_id=? AND item_id=? AND qty<=0", (user_id, item_id))
        await db.commit()

async def _remove_cart_item(db_path: str, user_id: int, item_id: int) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute("DELETE FROM cart_items WHERE user_id=? AND item_id=?", (user_id, item_id))
        await db.commit()

async def _clear_cart(db_path: str, user_id: int) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute("DELETE FROM cart_items WHERE user_id=?", (user_id,))
        await db.commit()


# ── loyalty points ───────────────────────────────────────────────────────────

async def _points_balance(db_path: str, user_id: int) -> int:
    async with aiosqlite.connect(db_path) as db:
        row = await (await db.execute("SELECT balance FROM loyalty_points WHERE user_id=?", (user_id,))).fetchone()
        return row[0] if row else 0

async def _add_points(db: aiosqlite.Connection, user_id: int, delta: int) -> None:
    """Caller-owned connection/transaction — used from _create_order (spend)
    and _apply_status_change (earn), both of which need the points change in
    the SAME transaction as the order row they're touching."""
    await db.execute(
        "INSERT INTO loyalty_points (user_id, balance) VALUES (?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET balance = balance + excluded.balance",
        (user_id, delta),
    )


# ── promo codes ───────────────────────────────────────────────────────────────

async def _promo_row(db_path: str, code: str) -> dict | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            "SELECT * FROM promo_codes WHERE code=? AND active=1", (code.strip().upper(),)
        )).fetchone()
        return dict(row) if row else None

async def _active_promos(db_path: str) -> list[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute("SELECT * FROM promo_codes WHERE active=1 ORDER BY code")).fetchall()
        return [dict(r) for r in rows]


# ── couriers ──────────────────────────────────────────────────────────────────

async def _active_couriers(db_path: str) -> list[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute("SELECT * FROM couriers WHERE active=1 ORDER BY name")).fetchall()
        return [dict(r) for r in rows]

async def _courier_row(db_path: str, courier_id: int) -> dict | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute("SELECT * FROM couriers WHERE id=?", (courier_id,))).fetchone()
        return dict(row) if row else None


# ── orders ────────────────────────────────────────────────────────────────────

async def _create_order(db_path: str, user_id: int, checkout: dict) -> dict:
    """Snapshots the user's cart into a new order + order_items, applies any
    promo/points discount, then empties the cart. Returns
    {"ok": True, "order_id": int, "total": int, "payment_method": str} or
    {"ok": False, "error": "cart_empty"|"below_minimum"|"not_enough_points"}.

    BEGIN IMMEDIATE + re-reading the cart under the lock guards the same
    double-tap class of bug as shop_catalog.py's _create_order: a double-tap
    on "✅ Подтвердить заказ" must not create two orders from the same cart.
    """
    async with aiosqlite.connect(db_path, timeout=10) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        rows = await (await db.execute(
            "SELECT ci.item_id, ci.qty, mi.name, mi.price FROM cart_items ci "
            "JOIN menu_items mi ON ci.item_id=mi.id "
            "WHERE ci.user_id=? AND mi.active=1 AND mi.available=1",
            (user_id,),
        )).fetchall()
        if not rows:
            await db.commit()
            return {"ok": False, "error": "cart_empty"}

        subtotal = sum(r["price"] * r["qty"] for r in rows)
        if subtotal < MIN_ORDER_AMOUNT:
            await db.commit()
            return {"ok": False, "error": "below_minimum"}

        discount = 0
        promo_code = checkout.get("promo_code")
        if promo_code:
            promo = await (await db.execute(
                "SELECT * FROM promo_codes WHERE code=? AND active=1", (promo_code,)
            )).fetchone()
            if promo and (promo["uses_left"] is None or promo["uses_left"] > 0):
                discount += subtotal * promo["discount_percent"] // 100
                if promo["uses_left"] is not None:
                    await db.execute(
                        "UPDATE promo_codes SET uses_left = uses_left - 1 WHERE code=?", (promo_code,)
                    )

        points_used = checkout.get("points_used") or 0
        if points_used:
            balance_row = await (await db.execute(
                "SELECT balance FROM loyalty_points WHERE user_id=?", (user_id,)
            )).fetchone()
            balance = balance_row[0] if balance_row else 0
            if points_used > balance:
                await db.commit()
                return {"ok": False, "error": "not_enough_points"}
            # 1 point = 1 currency unit off, capped so the order never goes
            # negative even if points_used alone would exceed the remaining
            # (subtotal - promo discount).
            points_used = min(points_used, max(subtotal - discount, 0))
            discount += points_used
            await _add_points(db, user_id, -points_used)

        total = max(subtotal - discount, 0)
        payment_method = checkout.get("payment_method", "cash")
        cur = await db.execute(
            "INSERT INTO orders (client_user_id, client_name, client_phone, delivery_zone, delivery_address, "
            "delivery_time_mode, delivery_time_text, comment, payment_method, subtotal, promo_code, "
            "discount_amount, points_used, total) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                user_id, checkout.get("client_name"), checkout.get("client_phone"),
                checkout["delivery_zone"], checkout["delivery_address"],
                checkout["delivery_time_mode"], checkout.get("delivery_time_text"), checkout.get("comment"),
                payment_method, subtotal, promo_code, discount, points_used, total,
            ),
        )
        order_id = cur.lastrowid
        for r in rows:
            await db.execute(
                "INSERT INTO order_items (order_id, item_id, name_snapshot, price_snapshot, qty) VALUES (?,?,?,?,?)",
                (order_id, r["item_id"], r["name"], r["price"], r["qty"]),
            )
        await db.execute("DELETE FROM cart_items WHERE user_id=?", (user_id,))
        await db.commit()
        return {"ok": True, "order_id": order_id, "total": total, "payment_method": payment_method}


async def _set_payment_payload(db_path: str, order_id: int, payload: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute("UPDATE orders SET payment_payload=? WHERE id=?", (payload, order_id))
        await db.commit()


async def _payment_status_for_order(db_path: str, order: dict) -> str | None:
    """Reads payment status straight out of features/payments.py's own
    `payments` table by the payload this order stored at invoice time —
    same "recoverable, read-only" pattern features/sellable_items.py's
    module docstring describes, chosen specifically so this template never
    has to register its own successful_payment handler (that event is
    already claimed by payments.router riding the same Dispatcher — see
    that module's docstring on why a second registration would be
    unreachable). Returns None for cash orders or orders with no invoice
    sent yet."""
    if order["payment_method"] != "online" or not order["payment_payload"]:
        return None
    async with aiosqlite.connect(db_path) as db:
        row = await (await db.execute(
            "SELECT status FROM payments WHERE invoice_payload=?", (order["payment_payload"],)
        )).fetchone()
        return row[0] if row else None


async def _order_row(db_path: str, order_id: int) -> dict | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute("SELECT * FROM orders WHERE id=?", (order_id,))).fetchone()
        return dict(row) if row else None

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
            "SELECT * FROM orders WHERE client_user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit)
        )).fetchall()
        return [dict(r) for r in rows]

async def _orders_by_status(db_path: str, status: str | None, limit: int = 25) -> list[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        if status is None:
            rows = await (await db.execute("SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,))).fetchall()
        elif status == "active":
            placeholders = ",".join("?" * len(TERMINAL_STATUSES))
            rows = await (await db.execute(
                f"SELECT * FROM orders WHERE status NOT IN ({placeholders}) ORDER BY id DESC LIMIT ?",
                (*TERMINAL_STATUSES, limit),
            )).fetchall()
        else:
            rows = await (await db.execute(
                "SELECT * FROM orders WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit)
            )).fetchall()
        return [dict(r) for r in rows]


async def _apply_status_change(
    config: FoodDeliveryConfig, bot: Bot, order_id: int, old_status: str, new_status: str,
    courier_id: int | None = None,
) -> tuple[bool, str | None]:
    """Compare-and-swap status UPDATE (`WHERE id=? AND status=?`) + client
    notification, same shape as templates/delivery_tracker.py's/
    templates/orders_tracker.py's _apply_status_change. Returns
    (applied, note). On reaching "delivered", awards loyalty points in the
    SAME transaction as the status write."""
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        if courier_id is not None:
            cur = await db.execute(
                "UPDATE orders SET status=?, courier_id=?, updated_at=datetime('now','localtime') "
                "WHERE id=? AND status=?",
                (new_status, courier_id, order_id, old_status),
            )
        else:
            cur = await db.execute(
                "UPDATE orders SET status=?, updated_at=datetime('now','localtime') WHERE id=? AND status=?",
                (new_status, order_id, old_status),
            )
        if cur.rowcount == 0:
            await db.commit()
            return False, None

        row = await (await db.execute(
            "SELECT client_user_id, total FROM orders WHERE id=?", (order_id,)
        )).fetchone()
        points_earned = 0
        if new_status == "delivered" and row is not None:
            points_earned = row["total"] // LOYALTY_POINTS_RATE
            if points_earned:
                await _add_points(db, row["client_user_id"], points_earned)
                await db.execute("UPDATE orders SET points_earned=? WHERE id=?", (points_earned, order_id))
        await db.commit()

    note = None
    if row:
        client_user_id = row["client_user_id"]
        label = STATUS_LABEL_PLAIN.get(new_status, new_status)
        text = f"🔔 Ваш заказ №{order_id}: статус изменён на «{label}»."
        if points_earned:
            text += f"\n⭐ Начислено баллов: {points_earned}."
        try:
            await bot.send_message(client_user_id, text)
            note = "🔔 Клиент уведомлён."
        except TelegramAPIError as e:
            logger.warning(f"food_delivery: failed to notify client for order {order_id}: {e}")
            note = "⚠️ Не удалось уведомить клиента (возможно, заблокировал бота)."
    return True, note


async def _stats(db_path: str) -> dict:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        total_orders = (await (await db.execute(
            "SELECT COUNT(*) FROM orders WHERE status='delivered'"
        )).fetchone())[0]
        revenue = (await (await db.execute(
            "SELECT COALESCE(SUM(total),0) FROM orders WHERE status='delivered'"
        )).fetchone())[0]
        avg_check = (revenue / total_orders) if total_orders else 0
        top = await (await db.execute(
            "SELECT oi.name_snapshot AS name, SUM(oi.qty) AS units FROM order_items oi "
            "JOIN orders o ON oi.order_id=o.id WHERE o.status='delivered' "
            "GROUP BY oi.name_snapshot ORDER BY units DESC LIMIT 5"
        )).fetchall()
        avg_minutes_row = await (await db.execute(
            "SELECT AVG((julianday(updated_at) - julianday(created_at)) * 24 * 60) FROM orders "
            "WHERE status='delivered'"
        )).fetchone()
        avg_minutes = avg_minutes_row[0] if avg_minutes_row and avg_minutes_row[0] is not None else 0
        by_day = await (await db.execute(
            "SELECT date(created_at) AS day, COUNT(*) AS cnt, COALESCE(SUM(total),0) AS revenue FROM orders "
            "WHERE status='delivered' GROUP BY date(created_at) ORDER BY day DESC LIMIT 7"
        )).fetchall()
    return {
        "total_orders": total_orders, "revenue": revenue, "avg_check": avg_check,
        "avg_minutes": avg_minutes, "top": [dict(r) for r in top], "by_day": [dict(r) for r in by_day],
    }


# ── FSM staleness guard ─────────────────────────────────────────────────────────
FLOW_TIMEOUT_SECONDS = 300

def _flow_expired(data: dict) -> bool:
    started_at = data.get("started_at")
    return started_at is None or (time.time() - started_at) > FLOW_TIMEOUT_SECONDS


# ── FSM states ───────────────────────────────────────────────────────────────

class CheckoutFlow(StatesGroup):
    """Client-side: zone(inline) -> address -> time-mode(inline) ->
    scheduled-time(conditional) -> comment(optional) -> phone ->
    promo(optional) -> points(optional) -> payment-method(inline) -> confirm."""
    address = State()
    scheduled_time = State()
    comment = State()
    phone = State()
    promo = State()
    points = State()

class AdminCategoryFlow(StatesGroup):
    name = State()

class AdminItemFlow(StatesGroup):
    name = State()
    description = State()
    price = State()
    photo = State()
    edit_value = State()

class CourierMgmtFlow(StatesGroup):
    name = State()
    phone = State()

class PromoMgmtFlow(StatesGroup):
    code = State()
    discount = State()
    uses = State()

class AdminMgmtFlow(StatesGroup):
    add_admin = State()
    remove_admin_pick = State()


MAX_LIST_BUTTONS = 25
MAX_ADMIN_REMOVE_BUTTONS = 30


# ── keyboards: shared ────────────────────────────────────────────────────────

def kb_back(callback_data: str = "fd_main") -> InlineKeyboardButton:
    return InlineKeyboardButton(text="◀️ Назад", callback_data=callback_data)

def kb_flow_cancel(callback_data: str = "fd_flow_cancel") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data=callback_data)]])

def kb_optional_step(skip_cb: str, cancel_cb: str = "fd_flow_cancel") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭ Пропустить", callback_data=skip_cb)],
        [InlineKeyboardButton(text="❌ Отмена", callback_data=cancel_cb)],
    ])


def kb_client_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🍔 Меню", callback_data="fd_menu")],
        [InlineKeyboardButton(text="🛒 Корзина", callback_data="fd_cart")],
        [InlineKeyboardButton(text="📦 Мои заказы", callback_data="fd_my_orders")],
        [InlineKeyboardButton(text="⭐ Мои баллы", callback_data="fd_points")],
    ])

def kb_admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🍔 Меню", callback_data="fd_adm_menu")],
        [InlineKeyboardButton(text="🧾 Заказы", callback_data="fd_adm_orders")],
        [InlineKeyboardButton(text="👨‍🍳 Кухня", callback_data="fd_adm_kitchen")],
        [InlineKeyboardButton(text="🚴 Курьеры", callback_data="fd_adm_couriers")],
        [InlineKeyboardButton(text="🎟 Промокоды", callback_data="fd_adm_promo")],
        [InlineKeyboardButton(text="📊 Аналитика", callback_data="fd_adm_stats")],
        [InlineKeyboardButton(text="👥 Админы", callback_data="fd_adm_admins")],
    ])

def _menu_for(user_id: int, config: FoodDeliveryConfig) -> tuple[str, InlineKeyboardMarkup]:
    if _is_admin(user_id, config):
        return WELCOME_TEXT, kb_admin_menu()
    return CLIENT_WELCOME_TEXT, kb_client_menu()


# ── keyboards: client catalog/cart ──────────────────────────────────────────

def kb_categories(cats: list[dict]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"📂 {_short(c['name'])}", callback_data=f"fd_cat:{c['id']}")] for c in cats]
    if not rows:
        rows.append([InlineKeyboardButton(text="Меню пока пусто", callback_data="fd_noop")])
    rows.append([kb_back()])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_items(items: list[dict], category_id: int) -> InlineKeyboardMarkup:
    rows = []
    for it in items:
        label = f"{it['name']} — {it['price']}{CURRENCY_SYMBOL}"
        if not it["available"]:
            label = f"🚫 {label} (нет в наличии)"
        rows.append([InlineKeyboardButton(text=_short(label, 50), callback_data=f"fd_item:{it['id']}")])
    if not rows:
        rows.append([InlineKeyboardButton(text="В этой категории пока пусто", callback_data="fd_noop")])
    rows.append([InlineKeyboardButton(text="◀️ К категориям", callback_data="fd_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_item_detail(item_id: int, category_id: int, available: bool) -> InlineKeyboardMarkup:
    rows = []
    if available:
        rows.append([InlineKeyboardButton(text="➕ В корзину", callback_data=f"fd_add:{item_id}")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"fd_cat:{category_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_cart(rows: list[dict], total: int) -> InlineKeyboardMarkup:
    kb_rows = []
    for r in rows:
        iid = r["item_id"]
        kb_rows.append([
            InlineKeyboardButton(text="➖", callback_data=f"fd_cart_dec:{iid}"),
            InlineKeyboardButton(text=f"🗑 {_short(r['name'], 24)}", callback_data=f"fd_cart_rm:{iid}"),
            InlineKeyboardButton(text="➕", callback_data=f"fd_cart_inc:{iid}"),
        ])
    if rows:
        kb_rows.append([InlineKeyboardButton(
            text=f"✅ Оформить заказ ({total}{CURRENCY_SYMBOL})", callback_data="fd_checkout_start",
        )])
    kb_rows.append([kb_back()])
    return InlineKeyboardMarkup(inline_keyboard=kb_rows)

def kb_zones() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=z, callback_data=f"fd_zone:{i}")] for i, z in enumerate(DELIVERY_ZONES)]
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="fd_checkout_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_time_mode() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚡ Сейчас", callback_data="fd_time_asap")],
        [InlineKeyboardButton(text="🕐 К определённому часу", callback_data="fd_time_scheduled")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="fd_checkout_cancel")],
    ])

def kb_payment_method(online_available: bool) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="💵 Наличными курьеру", callback_data="fd_pay_cash")]]
    if online_available:
        rows.append([InlineKeyboardButton(text="💳 Оплатить онлайн", callback_data="fd_pay_online")])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="fd_checkout_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_checkout_confirm() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подтвердить", callback_data="fd_checkout_confirm"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="fd_checkout_cancel"),
    ]])


# ── keyboards: client orders ─────────────────────────────────────────────────

def kb_order_list(rows: list[dict], back_cb: str) -> InlineKeyboardMarkup:
    btns = [
        [InlineKeyboardButton(
            text=f"№{o['id']} · {STATUS_LABELS.get(o['status'], o['status'])} · {o['total']}{CURRENCY_SYMBOL}",
            callback_data=f"fd_order_view:{o['id']}",
        )] for o in rows[:MAX_LIST_BUTTONS]
    ]
    btns.append([kb_back(back_cb)])
    return InlineKeyboardMarkup(inline_keyboard=btns)

def kb_order_detail_client(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[kb_back("fd_my_orders")]])


# ── keyboards: admin ─────────────────────────────────────────────────────────

def kb_menu_admin() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📂 Категории", callback_data="fd_adm_categories")],
        [InlineKeyboardButton(text="🍔 Блюда", callback_data="fd_adm_items")],
        [kb_back()],
    ])

async def kb_categories_admin(db_path: str) -> InlineKeyboardMarkup:
    cats = await _active_categories(db_path)
    rows = [[InlineKeyboardButton(text=f"➖ {_short(c['name'])}", callback_data=f"fd_adm_cat_rm:{c['id']}")] for c in cats]
    rows.append([InlineKeyboardButton(text="➕ Добавить категорию", callback_data="fd_adm_cat_add")])
    rows.append([kb_back("fd_adm_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def kb_category_pick_for_item(db_path: str) -> InlineKeyboardMarkup:
    cats = await _active_categories(db_path)
    rows = [[InlineKeyboardButton(text=_short(c["name"]), callback_data=f"fd_adm_item_cat:{c['id']}")] for c in cats]
    if not rows:
        rows.append([InlineKeyboardButton(text="Сначала добавьте категорию", callback_data="fd_noop")])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="fd_flow_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def kb_items_admin(db_path: str) -> InlineKeyboardMarkup:
    items = await _all_items_admin(db_path)
    rows = []
    for it in items:
        mark = "✅" if it["available"] else "🚫"
        rows.append([
            InlineKeyboardButton(text=f"✏️ {_short(it['name'], 22)}", callback_data=f"fd_adm_item_edit:{it['id']}"),
            InlineKeyboardButton(text=mark, callback_data=f"fd_adm_item_stop:{it['id']}"),
            InlineKeyboardButton(text="➖", callback_data=f"fd_adm_item_rm:{it['id']}"),
        ])
    rows.append([InlineKeyboardButton(text="➕ Добавить блюдо", callback_data="fd_adm_item_add")])
    rows.append([kb_back("fd_adm_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_item_edit_menu(item_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Название", callback_data=f"fd_adm_item_field:{item_id}:name")],
        [InlineKeyboardButton(text="📝 Состав", callback_data=f"fd_adm_item_field:{item_id}:description")],
        [InlineKeyboardButton(text="💰 Цена", callback_data=f"fd_adm_item_field:{item_id}:price")],
        [InlineKeyboardButton(text="🖼 Фото", callback_data=f"fd_adm_item_field:{item_id}:photo")],
        [kb_back("fd_adm_items")],
    ])

def kb_status_filters() -> InlineKeyboardMarkup:
    filters = [("active", "🟢 Активные")] + [(c, l) for c, l in STATUS_LABELS.items()] + [("all", "📋 Все")]
    rows = [[InlineKeyboardButton(text=label, callback_data=f"fd_adm_order_filter:{code}")] for code, label in filters]
    rows.append([kb_back("fd_adm_orders")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_order_detail_admin(order_id: int, status: str) -> InlineKeyboardMarkup:
    rows = []
    for target in STATUS_TRANSITIONS.get(status, []):
        icon = "❌ Отменить" if target == "cancelled" else f"▶️ {STATUS_LABELS.get(target, target)}"
        rows.append([InlineKeyboardButton(text=icon, callback_data=f"fd_adm_order_status:{order_id}:{target}")])
    rows.append([kb_back("fd_adm_orders")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def kb_courier_pick(db_path: str, order_id: int) -> InlineKeyboardMarkup:
    couriers = await _active_couriers(db_path)
    rows = [
        [InlineKeyboardButton(text=f"🚴 {_short(c['name'])}", callback_data=f"fd_courier_assign:{order_id}:{c['id']}")]
        for c in couriers
    ]
    if not rows:
        rows.append([InlineKeyboardButton(text="Нет активных курьеров — добавьте в разделе «Курьеры»", callback_data="fd_noop")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"fd_adm_order_view:{order_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_couriers_admin(couriers: list[dict]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text=f"{'✅' if c['active'] else '🚫'} {_short(c['name'], 30)}", callback_data=f"fd_adm_courier_toggle:{c['id']}",
        )] for c in couriers
    ]
    rows.append([InlineKeyboardButton(text="➕ Добавить курьера", callback_data="fd_adm_courier_add")])
    rows.append([kb_back()])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_promo_admin(promos: list[dict]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text=f"➖ {p['code']} (-{p['discount_percent']}%)", callback_data=f"fd_adm_promo_rm:{p['code']}",
        )] for p in promos
    ]
    rows.append([InlineKeyboardButton(text="➕ Добавить промокод", callback_data="fd_adm_promo_add")])
    rows.append([kb_back()])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_admins_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить админа", callback_data="fd_adm_add_admin")],
        [InlineKeyboardButton(text="➖ Убрать админа", callback_data="fd_adm_remove_admin")],
        [kb_back()],
    ])

def kb_remove_admins(ids: list[str]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=admin_id, callback_data=f"fd_adm_rma:{i}")] for i, admin_id in enumerate(ids)]
    rows.append([kb_back("fd_adm_admins")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── rendering helpers ────────────────────────────────────────────────────────

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
    if total < MIN_ORDER_AMOUNT:
        lines.append(f"⚠️ Минимальная сумма заказа: {MIN_ORDER_AMOUNT}{CURRENCY_SYMBOL}")
    return _join_bounded(lines), kb_cart(rows, total)


async def _order_detail_text(db_path: str, order_id: int, extra_note: str | None = None, for_admin: bool = False) -> str | None:
    order = await _order_row(db_path, order_id)
    if order is None:
        return None
    items = await _order_items(db_path, order_id)
    lines = [f"🧾 <b>Заказ №{order['id']}</b> · {STATUS_LABELS.get(order['status'], order['status'])}\n"]
    if for_admin:
        lines.append(f"👤 {_esc(order['client_name'] or 'Без имени')}" + (f" · {_esc(order['client_phone'])}" if order["client_phone"] else ""))
    lines.append(f"📍 Зона: {_esc(order['delivery_zone'])}")
    lines.append(f"🏠 Адрес: {_esc(order['delivery_address'])}")
    time_line = "⚡ Сейчас" if order["delivery_time_mode"] == "asap" else f"🕐 К {_esc(order['delivery_time_text'] or '')}"
    lines.append(time_line)
    if order["comment"]:
        lines.append(f"💬 {_esc(order['comment'], COMMENT_MAX_LEN)}")
    lines.append("\n<b>Состав заказа:</b>")
    for it in items:
        lines.append(f"• {_esc(it['name_snapshot'])} × {it['qty']} = {it['price_snapshot'] * it['qty']}{CURRENCY_SYMBOL}")
    if order["discount_amount"]:
        promo_part = f" (промокод {_esc(order['promo_code'])})" if order["promo_code"] else ""
        lines.append(f"\n🎟 Скидка: -{order['discount_amount']}{CURRENCY_SYMBOL}{promo_part}")
    pay_label = "💵 Наличными курьеру" if order["payment_method"] == "cash" else "💳 Онлайн"
    lines.append(f"\n💰 <b>Итого: {order['total']}{CURRENCY_SYMBOL}</b> · {pay_label}")
    if order["payment_method"] == "online":
        pay_status = await _payment_status_for_order(db_path, order)
        lines.append("✅ Оплачено" if pay_status == "paid" else "⏳ Ожидает оплаты")
    if order["points_earned"]:
        lines.append(f"⭐ Начислено баллов: {order['points_earned']}")
    if for_admin and order["courier_id"]:
        courier = await _courier_row(db_path, order["courier_id"])
        if courier:
            lines.append(f"🚴 Курьер: {_esc(courier['name'])}" + (f" · {_esc(courier['phone'])}" if courier["phone"] else ""))
    lines.append(f"\n🕐 Создан: {order['created_at']}")
    if extra_note:
        lines.append(f"\n{extra_note}")
    return _join_bounded(lines)


# ── /start ────────────────────────────────────────────────────────────────────

@router.message(Command("start"), F.chat.type == "private")
async def cmd_start(message: Message, state: FSMContext, config: FoodDeliveryConfig):
    # Same reasoning as every other template's cmd_start: /start must reset
    # any dangling mid-flow FSM state before showing a menu.
    await state.clear()
    admins = _load_admins(config.admins_file)
    first_time_admin = not admins
    if first_time_admin:
        _save_admins(config.admins_file, {str(message.from_user.id)})
        admins = {str(message.from_user.id)}

    text, kb = _menu_for(message.from_user.id, config)
    if str(message.from_user.id) in admins and config.welcome_image.exists():
        await message.answer_photo(FSInputFile(str(config.welcome_image)), caption=text, parse_mode="HTML", reply_markup=kb)
    else:
        await message.answer(text, parse_mode="HTML", reply_markup=kb)
    if first_time_admin:
        await message.answer(
            "👑 <b>Вы — администратор этого бота.</b>\n\n"
            "Управление другими администраторами — кнопка «👥 Админы» выше.",
            parse_mode="HTML",
        )


@router.callback_query(F.data == "fd_main")
async def cb_main(cb: CallbackQuery, state: FSMContext, config: FoodDeliveryConfig):
    await cb.answer()
    await state.clear()
    text, kb = _menu_for(cb.from_user.id, config)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "fd_flow_cancel")
async def cb_flow_cancel(cb: CallbackQuery, state: FSMContext, config: FoodDeliveryConfig):
    await cb.answer()
    await state.clear()
    text, kb = _menu_for(cb.from_user.id, config)
    await cb.message.edit_text("Отменено.\n\n" + text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "fd_noop")
async def cb_noop(cb: CallbackQuery):
    await cb.answer()


# ── CLIENT: menu browsing ─────────────────────────────────────────────────────

@router.callback_query(F.data == "fd_menu")
async def cb_menu(cb: CallbackQuery, state: FSMContext, config: FoodDeliveryConfig):
    await cb.answer()
    await state.clear()
    cats = await _active_categories(config.db_path)
    await cb.message.edit_text("📂 Выберите категорию:", reply_markup=kb_categories(cats))


@router.callback_query(F.data.startswith("fd_cat:"))
async def cb_category(cb: CallbackQuery, config: FoodDeliveryConfig):
    await cb.answer()
    try:
        category_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    cat = await _category_row(config.db_path, category_id)
    if cat is None or not cat["active"]:
        await cb.message.edit_text("Категория недоступна.", reply_markup=kb_categories(await _active_categories(config.db_path)))
        return
    items = await _items_in_category(config.db_path, category_id, only_available=False)
    await cb.message.edit_text(f"📂 <b>{_esc(cat['name'])}</b>", parse_mode="HTML", reply_markup=kb_items(items, category_id))


@router.callback_query(F.data.startswith("fd_item:"))
async def cb_item_detail(cb: CallbackQuery, config: FoodDeliveryConfig):
    try:
        item_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        await cb.answer(); return
    item = await _item_row(config.db_path, item_id)
    if item is None or not item["active"]:
        await cb.answer("Блюдо недоступно.", show_alert=True); return
    await cb.answer()
    desc = _esc(item["description"], DESCRIPTION_MAX_LEN) if item["description"] else ""
    avail_note = "" if item["available"] else "\n\n🚫 <b>Нет в наличии</b>"
    text = f"🍔 <b>{_esc(item['name'])}</b>\n\n{desc}\n\n💰 <b>{item['price']}{CURRENCY_SYMBOL}</b>{avail_note}"
    kb = kb_item_detail(item_id, item["category_id"], bool(item["available"]))
    if item["photo_file_id"]:
        try:
            await cb.message.delete()
        except Exception as e:
            logger.debug(f"cb_item_detail: failed to delete old message: {e}")
        await cb.message.answer_photo(item["photo_file_id"], caption=text, parse_mode="HTML", reply_markup=kb)
    else:
        await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("fd_add:"))
async def cb_add_to_cart(cb: CallbackQuery, config: FoodDeliveryConfig):
    try:
        item_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        await cb.answer(); return
    item = await _item_row(config.db_path, item_id)
    if item is None or not item["active"] or not item["available"]:
        await cb.answer("Блюдо больше не доступно.", show_alert=True); return
    await _add_to_cart(config.db_path, cb.from_user.id, item_id)
    await cb.answer("Добавлено в корзину ✅")


# ── CLIENT: cart ──────────────────────────────────────────────────────────────

@router.callback_query(F.data == "fd_cart")
async def cb_cart(cb: CallbackQuery, state: FSMContext, config: FoodDeliveryConfig):
    await cb.answer()
    await state.clear()
    text, kb = await _render_cart(config.db_path, cb.from_user.id)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("fd_cart_inc:"))
async def cb_cart_inc(cb: CallbackQuery, config: FoodDeliveryConfig):
    await cb.answer()
    item_id = int(cb.data.split(":", 1)[1])
    await _change_cart_qty(config.db_path, cb.from_user.id, item_id, +1)
    text, kb = await _render_cart(config.db_path, cb.from_user.id)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("fd_cart_dec:"))
async def cb_cart_dec(cb: CallbackQuery, config: FoodDeliveryConfig):
    await cb.answer()
    item_id = int(cb.data.split(":", 1)[1])
    await _change_cart_qty(config.db_path, cb.from_user.id, item_id, -1)
    text, kb = await _render_cart(config.db_path, cb.from_user.id)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("fd_cart_rm:"))
async def cb_cart_rm(cb: CallbackQuery, config: FoodDeliveryConfig):
    await cb.answer()
    item_id = int(cb.data.split(":", 1)[1])
    await _remove_cart_item(config.db_path, cb.from_user.id, item_id)
    text, kb = await _render_cart(config.db_path, cb.from_user.id)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


# ── CLIENT: checkout flow ─────────────────────────────────────────────────────
# zone(inline) -> address(text) -> time-mode(inline) -> scheduled-time(text,
# conditional) -> comment(text/skip) -> phone(text) -> promo(text/skip) ->
# points(text/skip) -> payment-method(inline) -> confirm(inline)

@router.callback_query(F.data == "fd_checkout_start")
async def cb_checkout_start(cb: CallbackQuery, state: FSMContext, config: FoodDeliveryConfig):
    await cb.answer()
    rows = await _cart_rows(config.db_path, cb.from_user.id)
    if not rows:
        await cb.message.edit_text("🛒 Ваша корзина пуста.", reply_markup=kb_cart(rows, 0)); return
    if _cart_total(rows) < MIN_ORDER_AMOUNT:
        text, kb = await _render_cart(config.db_path, cb.from_user.id)
        await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb); return
    await state.clear()
    await state.update_data(started_at=time.time())
    await cb.message.edit_text("📍 Выберите зону доставки:", reply_markup=kb_zones())


@router.callback_query(F.data.startswith("fd_zone:"))
async def cb_zone_pick(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_client_menu()); return
    try:
        idx = int(cb.data.split(":", 1)[1])
        zone = DELIVERY_ZONES[idx]
    except (ValueError, IndexError):
        return
    await state.update_data(delivery_zone=zone)
    await state.set_state(CheckoutFlow.address)
    await cb.message.edit_text("🏠 Укажите адрес доставки:", reply_markup=kb_flow_cancel("fd_checkout_cancel"))


@router.message(CheckoutFlow.address, F.text, ~F.text.startswith("/"))
async def checkout_address(msg: Message, state: FSMContext):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_client_menu()); return
    address = msg.text.strip()
    if not address:
        await msg.answer("Адрес не может быть пустым.", reply_markup=kb_flow_cancel("fd_checkout_cancel")); return
    if len(address) > ADDRESS_MAX_LEN:
        await msg.answer(f"⚠️ Слишком длинный адрес. Уложитесь в {ADDRESS_MAX_LEN} символов:", reply_markup=kb_flow_cancel("fd_checkout_cancel")); return
    await state.update_data(delivery_address=address)
    await state.set_state(None)
    await msg.answer("🕐 Когда доставить?", reply_markup=kb_time_mode())


@router.callback_query(F.data == "fd_time_asap")
async def cb_time_asap(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_client_menu()); return
    await state.update_data(delivery_time_mode="asap", delivery_time_text=None)
    await state.set_state(CheckoutFlow.comment)
    await cb.message.edit_text("💬 Комментарий к заказу (или «Пропустить»):", reply_markup=kb_optional_step("fd_comment_skip", "fd_checkout_cancel"))


@router.callback_query(F.data == "fd_time_scheduled")
async def cb_time_scheduled(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_client_menu()); return
    await state.set_state(CheckoutFlow.scheduled_time)
    await cb.message.edit_text("🕐 Введите время в формате ЧЧ:ММ (например 19:30):", reply_markup=kb_flow_cancel("fd_checkout_cancel"))


@router.message(CheckoutFlow.scheduled_time, F.text, ~F.text.startswith("/"))
async def checkout_scheduled_time(msg: Message, state: FSMContext):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_client_menu()); return
    time_text = _valid_time_text(msg.text)
    if time_text is None:
        await msg.answer("❌ Неверный формат. Введите время как ЧЧ:ММ, например 19:30:", reply_markup=kb_flow_cancel("fd_checkout_cancel")); return
    await state.update_data(delivery_time_mode="scheduled", delivery_time_text=time_text)
    await state.set_state(CheckoutFlow.comment)
    await msg.answer("💬 Комментарий к заказу (или «Пропустить»):", reply_markup=kb_optional_step("fd_comment_skip", "fd_checkout_cancel"))


@router.message(CheckoutFlow.comment, F.text, ~F.text.startswith("/"))
async def checkout_comment(msg: Message, state: FSMContext):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_client_menu()); return
    comment = msg.text.strip()[:COMMENT_MAX_LEN]
    await state.update_data(comment=comment)
    await state.set_state(CheckoutFlow.phone)
    await msg.answer("📱 Укажите номер телефона для связи:", reply_markup=kb_flow_cancel("fd_checkout_cancel"))


@router.callback_query(CheckoutFlow.comment, F.data == "fd_comment_skip")
async def cb_comment_skip(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_client_menu()); return
    await state.update_data(comment=None)
    await state.set_state(CheckoutFlow.phone)
    await cb.message.edit_text("📱 Укажите номер телефона для связи:", reply_markup=kb_flow_cancel("fd_checkout_cancel"))


@router.message(CheckoutFlow.phone, F.text, ~F.text.startswith("/"))
async def checkout_phone(msg: Message, state: FSMContext):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_client_menu()); return
    phone = _normalize_phone(msg.text.strip())
    if phone is None:
        await msg.answer(
            "❌ Не удалось распознать номер. Введите номер, например <b>+7 999 123-45-67</b>:",
            parse_mode="HTML", reply_markup=kb_flow_cancel("fd_checkout_cancel"),
        ); return
    await state.update_data(client_phone=phone, client_name=msg.from_user.full_name)
    await state.set_state(CheckoutFlow.promo)
    await msg.answer("🎟 Есть промокод? Введите его, или нажмите «Пропустить»:", reply_markup=kb_optional_step("fd_promo_skip", "fd_checkout_cancel"))


async def _proceed_to_points(message_answer, state: FSMContext, db_path: str, user_id: int) -> None:
    balance = await _points_balance(db_path, user_id)
    await state.set_state(CheckoutFlow.points)
    if balance > 0:
        await message_answer(
            f"⭐ У вас {balance} баллов. Сколько списать на скидку (1 балл = 1{CURRENCY_SYMBOL})? "
            "Введите число, или «Пропустить»:",
            reply_markup=kb_optional_step("fd_points_skip", "fd_checkout_cancel"),
        )
    else:
        await state.update_data(points_used=0)
        await _proceed_to_payment(message_answer, state, db_path, user_id)


async def _proceed_to_payment(message_answer, state: FSMContext, db_path: str, user_id: int) -> None:
    data = await state.get_data()
    bot_id = data.get("bot_id")
    online_available = False
    if bot_id is not None:
        try:
            online_available = "payments" in await get_bot_features(bot_id)
        except Exception as e:
            logger.warning(f"food_delivery: get_bot_features failed for bot_id={bot_id}: {e}")
    await message_answer("💳 Как будете оплачивать?", reply_markup=kb_payment_method(online_available))


@router.message(CheckoutFlow.promo, F.text, ~F.text.startswith("/"))
async def checkout_promo(msg: Message, state: FSMContext, config: FoodDeliveryConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_client_menu()); return
    code = _valid_promo_code(msg.text)
    if code is None:
        await msg.answer("Некорректный код. Введите промокод или нажмите «Пропустить»:", reply_markup=kb_optional_step("fd_promo_skip", "fd_checkout_cancel")); return
    promo = await _promo_row(config.db_path, code)
    if promo is None or (promo["uses_left"] is not None and promo["uses_left"] <= 0):
        await msg.answer("⚠️ Промокод не найден или больше не действует. Попробуйте другой или «Пропустить»:", reply_markup=kb_optional_step("fd_promo_skip", "fd_checkout_cancel")); return
    await state.update_data(promo_code=code, bot_id=config.bot_id)
    await msg.answer(f"✅ Промокод «{_esc(code)}» применён: -{promo['discount_percent']}%")
    await _proceed_to_points(msg.answer, state, config.db_path, msg.from_user.id)


@router.callback_query(CheckoutFlow.promo, F.data == "fd_promo_skip")
async def cb_promo_skip(cb: CallbackQuery, state: FSMContext, config: FoodDeliveryConfig):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_client_menu()); return
    await state.update_data(promo_code=None, bot_id=config.bot_id)
    await _proceed_to_points(cb.message.answer, state, config.db_path, cb.from_user.id)


@router.message(CheckoutFlow.points, F.text, ~F.text.startswith("/"))
async def checkout_points(msg: Message, state: FSMContext, config: FoodDeliveryConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_client_menu()); return
    points = _valid_points(msg.text)
    if points is None:
        await msg.answer("Введите целое неотрицательное число баллов, или «Пропустить»:", reply_markup=kb_optional_step("fd_points_skip", "fd_checkout_cancel")); return
    balance = await _points_balance(config.db_path, msg.from_user.id)
    if points > balance:
        await msg.answer(f"⚠️ У вас только {balance} баллов. Введите число не больше этого, или «Пропустить»:", reply_markup=kb_optional_step("fd_points_skip", "fd_checkout_cancel")); return
    await state.update_data(points_used=points)
    await _proceed_to_payment(msg.answer, state, config.db_path, msg.from_user.id)


@router.callback_query(CheckoutFlow.points, F.data == "fd_points_skip")
async def cb_points_skip(cb: CallbackQuery, state: FSMContext, config: FoodDeliveryConfig):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_client_menu()); return
    await state.update_data(points_used=0)
    await _proceed_to_payment(cb.message.answer, state, config.db_path, cb.from_user.id)


async def _show_checkout_confirmation(message_answer, state: FSMContext, db_path: str, user_id: int, payment_method: str) -> None:
    await state.update_data(payment_method=payment_method)
    data = await state.get_data()
    rows = await _cart_rows(db_path, user_id)
    if not rows:
        await state.clear()
        await message_answer("🛒 Ваша корзина опустела — начните заново.", reply_markup=kb_client_menu()); return
    subtotal = _cart_total(rows)
    lines = ["📋 <b>Подтвердите заказ:</b>\n"]
    for r in rows:
        lines.append(f"• {_esc(r['name'])} — {r['qty']} шт. × {r['price']}{CURRENCY_SYMBOL}")
    lines.append(f"\n📍 Зона: {_esc(data.get('delivery_zone', ''))}")
    lines.append(f"🏠 Адрес: {_esc(data.get('delivery_address', ''))}")
    time_line = "⚡ Сейчас" if data.get("delivery_time_mode") == "asap" else f"🕐 К {_esc(data.get('delivery_time_text') or '')}"
    lines.append(time_line)
    lines.append(f"💰 <b>Сумма: {subtotal}{CURRENCY_SYMBOL}</b>")
    pay_label = "💵 Наличными курьеру" if payment_method == "cash" else "💳 Онлайн"
    lines.append(f"Оплата: {pay_label}")
    await state.set_state(None)
    try:
        await message_answer(_join_bounded(lines), parse_mode="HTML", reply_markup=kb_checkout_confirm())
    except Exception as e:
        logger.error(f"food_delivery: failed to show checkout confirmation: {e}")
        await state.clear()
        await message_answer("⚠️ Не удалось показать подтверждение. Начните оформление заново.", reply_markup=kb_client_menu())


@router.callback_query(F.data == "fd_pay_cash")
async def cb_pay_cash(cb: CallbackQuery, state: FSMContext, config: FoodDeliveryConfig):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_client_menu()); return
    await _show_checkout_confirmation(cb.message.answer, state, config.db_path, cb.from_user.id, "cash")


@router.callback_query(F.data == "fd_pay_online")
async def cb_pay_online(cb: CallbackQuery, state: FSMContext, config: FoodDeliveryConfig):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_client_menu()); return
    await _show_checkout_confirmation(cb.message.answer, state, config.db_path, cb.from_user.id, "online")


@router.callback_query(F.data == "fd_checkout_confirm")
async def cb_checkout_confirm(cb: CallbackQuery, bot: Bot, state: FSMContext, config: FoodDeliveryConfig):
    await cb.answer()
    data = await state.get_data()
    await state.clear()
    result = await _create_order(config.db_path, cb.from_user.id, data)
    if not result["ok"]:
        messages = {
            "cart_empty": "Эта заявка уже оформлена, либо корзина пуста.",
            "below_minimum": f"⚠️ Сумма заказа меньше минимальной ({MIN_ORDER_AMOUNT}{CURRENCY_SYMBOL}).",
            "not_enough_points": "⚠️ Недостаточно баллов — попробуйте оформить заказ заново.",
        }
        try:
            await cb.message.edit_text(messages.get(result["error"], "Не удалось оформить заказ."), reply_markup=kb_client_menu())
        except Exception:
            pass
        return

    order_id = result["order_id"]
    if result["payment_method"] == "online" and config.bot_id is not None:
        try:
            payload = f"food_delivery_order:{order_id}:{cb.from_user.id}"
            await create_invoice(
                bot=bot, bot_id=config.bot_id, chat_id=cb.from_user.id,
                title=f"Заказ №{order_id}", description="Оплата заказа доставки еды",
                payload=payload, currency="RUB",
                prices=[LabeledPrice(label=f"Заказ №{order_id}", amount=result["total"] * 100)],
            )
            await _set_payment_payload(config.db_path, order_id, payload)
        except ValueError:
            logger.warning(f"food_delivery: order {order_id} wants online payment but no provider is configured")
            await cb.message.answer("⚠️ Оплата онлайн временно недоступна — оплатите наличными курьеру.")
        except TelegramBadRequest:
            logger.warning(f"food_delivery: send_invoice failed for order {order_id}")
            await cb.message.answer("⚠️ Не удалось выставить счёт. Оплатите наличными курьеру.")

    try:
        await cb.message.edit_text(
            f"✅ <b>Заказ №{order_id} оформлен!</b>\n\n💰 Сумма: {result['total']}{CURRENCY_SYMBOL}\n\n"
            "Мы уведомим вас о каждом изменении статуса.",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning(f"food_delivery: failed to show order-created confirmation for order {order_id}: {e}")

    for admin_id in _load_admins(config.admins_file):
        try:
            await bot.send_message(
                int(admin_id),
                f"🔔 <b>Новый заказ №{order_id}!</b>\n💰 {result['total']}{CURRENCY_SYMBOL}",
                parse_mode="HTML",
            )
        except (TelegramAPIError, ValueError) as e:
            logger.warning(f"food_delivery: failed to notify admin {admin_id} of new order {order_id}: {e}")


@router.callback_query(F.data == "fd_checkout_cancel")
async def cb_checkout_cancel(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await cb.message.edit_text("Оформление отменено.", reply_markup=kb_client_menu())


# ── CLIENT: my orders / points ────────────────────────────────────────────────

@router.callback_query(F.data == "fd_my_orders")
async def cb_my_orders(cb: CallbackQuery, state: FSMContext, config: FoodDeliveryConfig):
    await cb.answer()
    await state.clear()
    rows = await _user_orders(config.db_path, cb.from_user.id)
    if not rows:
        await cb.message.edit_text("У вас пока нет заказов.", reply_markup=kb_client_menu()); return
    await cb.message.edit_text("📦 Ваши заказы:", reply_markup=kb_order_list(rows, "fd_main"))


@router.callback_query(F.data.startswith("fd_order_view:"))
async def cb_order_view_client(cb: CallbackQuery, config: FoodDeliveryConfig):
    await cb.answer()
    try:
        order_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    # Ownership check — same principle as delivery_tracker.py's dlv_view:
    # "not found" is deliberately used for both "doesn't exist" and "exists
    # but isn't yours", so a guessed order id leaks nothing.
    order = await _order_row(config.db_path, order_id)
    if order is None or order["client_user_id"] != cb.from_user.id:
        await cb.message.edit_text("Заказ не найден.", reply_markup=kb_client_menu()); return
    text = await _order_detail_text(config.db_path, order_id, for_admin=False)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_order_detail_client(order_id))


@router.callback_query(F.data == "fd_points")
async def cb_points_view(cb: CallbackQuery, state: FSMContext, config: FoodDeliveryConfig):
    await cb.answer()
    await state.clear()
    balance = await _points_balance(config.db_path, cb.from_user.id)
    await cb.message.edit_text(
        f"⭐ <b>Ваши баллы: {balance}</b>\n\n"
        f"1 балл начисляется за каждые {LOYALTY_POINTS_RATE}{CURRENCY_SYMBOL} доставленного заказа.\n"
        f"Баллы можно списать на скидку при оформлении заказа (1 балл = 1{CURRENCY_SYMBOL}).",
        parse_mode="HTML", reply_markup=kb_client_menu(),
    )


# ── ADMIN: main menu ─────────────────────────────────────────────────────────

@router.callback_query(F.data == "fd_adm_menu")
async def cb_adm_menu_root(cb: CallbackQuery, state: FSMContext, config: FoodDeliveryConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    await cb.message.edit_text("🍔 <b>Меню</b>", parse_mode="HTML", reply_markup=kb_menu_admin())


# ── ADMIN: categories CRUD ────────────────────────────────────────────────────

@router.callback_query(F.data == "fd_adm_categories")
async def cb_adm_categories(cb: CallbackQuery, config: FoodDeliveryConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await cb.message.edit_text("📂 <b>Категории</b>", parse_mode="HTML", reply_markup=await kb_categories_admin(config.db_path))


@router.callback_query(F.data == "fd_adm_cat_add")
async def cb_adm_cat_add(cb: CallbackQuery, state: FSMContext, config: FoodDeliveryConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.set_state(AdminCategoryFlow.name)
    await state.update_data(started_at=time.time())
    await cb.message.edit_text("Введите название категории:", reply_markup=kb_flow_cancel())


@router.message(AdminCategoryFlow.name, F.text, ~F.text.startswith("/"))
async def adm_cat_name(msg: Message, state: FSMContext, config: FoodDeliveryConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_admin_menu()); return
    name = msg.text.strip()[:NAME_MAX_LEN]
    if not name:
        await msg.answer("Название не может быть пустым.", reply_markup=kb_flow_cancel()); return
    await state.clear()
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute("INSERT INTO menu_categories (name) VALUES (?)", (name,))
        await db.commit()
    await msg.answer(f"✅ Категория «{_esc(name)}» добавлена.", parse_mode="HTML", reply_markup=await kb_categories_admin(config.db_path))


@router.callback_query(F.data.startswith("fd_adm_cat_rm:"))
async def cb_adm_cat_rm(cb: CallbackQuery, config: FoodDeliveryConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    category_id = int(cb.data.split(":", 1)[1])
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute("UPDATE menu_categories SET active=0 WHERE id=?", (category_id,))
        await db.commit()
    await cb.message.edit_text("📂 <b>Категории</b>", parse_mode="HTML", reply_markup=await kb_categories_admin(config.db_path))


# ── ADMIN: menu items CRUD ────────────────────────────────────────────────────

@router.callback_query(F.data == "fd_adm_items")
async def cb_adm_items(cb: CallbackQuery, config: FoodDeliveryConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await cb.message.edit_text("🍔 <b>Блюда</b>", parse_mode="HTML", reply_markup=await kb_items_admin(config.db_path))


@router.callback_query(F.data == "fd_adm_item_add")
async def cb_adm_item_add(cb: CallbackQuery, state: FSMContext, config: FoodDeliveryConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    await state.update_data(started_at=time.time())
    await cb.message.edit_text("Выберите категорию для блюда:", reply_markup=await kb_category_pick_for_item(config.db_path))


@router.callback_query(F.data.startswith("fd_adm_item_cat:"))
async def cb_adm_item_cat(cb: CallbackQuery, state: FSMContext, config: FoodDeliveryConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_admin_menu()); return
    category_id = int(cb.data.split(":", 1)[1])
    await state.update_data(new_item_category_id=category_id)
    await state.set_state(AdminItemFlow.name)
    await cb.message.edit_text("Название блюда:", reply_markup=kb_flow_cancel())


@router.message(AdminItemFlow.name, F.text, ~F.text.startswith("/"))
async def adm_item_name(msg: Message, state: FSMContext):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_admin_menu()); return
    name = msg.text.strip()[:NAME_MAX_LEN]
    if not name:
        await msg.answer("Название не может быть пустым.", reply_markup=kb_flow_cancel()); return
    await state.update_data(new_item_name=name)
    await state.set_state(AdminItemFlow.description)
    await msg.answer("Состав/описание (или «Пропустить»):", reply_markup=kb_optional_step("fd_adm_item_desc_skip"))


@router.message(AdminItemFlow.description, F.text, ~F.text.startswith("/"))
async def adm_item_description(msg: Message, state: FSMContext):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_admin_menu()); return
    await state.update_data(new_item_description=msg.text.strip()[:DESCRIPTION_MAX_LEN])
    await state.set_state(AdminItemFlow.price)
    await msg.answer("💰 Цена:", reply_markup=kb_flow_cancel())


@router.callback_query(AdminItemFlow.description, F.data == "fd_adm_item_desc_skip")
async def cb_adm_item_desc_skip(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_admin_menu()); return
    await state.update_data(new_item_description=None)
    await state.set_state(AdminItemFlow.price)
    await cb.message.edit_text("💰 Цена:", reply_markup=kb_flow_cancel())


@router.message(AdminItemFlow.price, F.text, ~F.text.startswith("/"))
async def adm_item_price(msg: Message, state: FSMContext):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_admin_menu()); return
    price = _valid_price(msg.text)
    if price is None:
        await msg.answer(f"Введите целое число от 1 до {PRICE_MAX}, например: 350", reply_markup=kb_flow_cancel()); return
    await state.update_data(new_item_price=price)
    await state.set_state(AdminItemFlow.photo)
    await msg.answer("🖼 Пришлите фото блюда (или «Пропустить»):", reply_markup=kb_optional_step("fd_adm_item_photo_skip"))


async def _finalize_new_item(state: FSMContext, config: FoodDeliveryConfig, photo_file_id: str | None) -> str:
    data = await state.get_data()
    await state.clear()
    category_id = data.get("new_item_category_id")
    name = data.get("new_item_name")
    if not category_id or not name:
        return "Сессия устарела, начните заново."
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute(
            "INSERT INTO menu_items (category_id, name, description, price, photo_file_id) VALUES (?,?,?,?,?)",
            (category_id, name, data.get("new_item_description"), data.get("new_item_price"), photo_file_id),
        )
        await db.commit()
    return f"✅ Блюдо «{name}» добавлено."


@router.message(AdminItemFlow.photo, F.photo)
async def adm_item_photo(msg: Message, state: FSMContext, config: FoodDeliveryConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_admin_menu()); return
    photo_file_id = msg.photo[-1].file_id
    note = await _finalize_new_item(state, config, photo_file_id)
    await msg.answer(note, reply_markup=await kb_items_admin(config.db_path))


@router.callback_query(AdminItemFlow.photo, F.data == "fd_adm_item_photo_skip")
async def cb_adm_item_photo_skip(cb: CallbackQuery, state: FSMContext, config: FoodDeliveryConfig):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_admin_menu()); return
    note = await _finalize_new_item(state, config, None)
    await cb.message.edit_text(note, reply_markup=await kb_items_admin(config.db_path))


@router.callback_query(F.data.startswith("fd_adm_item_stop:"))
async def cb_adm_item_stop_toggle(cb: CallbackQuery, config: FoodDeliveryConfig):
    """Stop-list toggle — item stays in the menu (active=1) but is hidden
    from "add to cart" while available=0, satisfying brief item #1's "наличие
    (стоп-лист)" without deleting the item."""
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    item_id = int(cb.data.split(":", 1)[1])
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute("UPDATE menu_items SET available = NOT available WHERE id=?", (item_id,))
        await db.commit()
    await cb.message.edit_text("🍔 <b>Блюда</b>", parse_mode="HTML", reply_markup=await kb_items_admin(config.db_path))


@router.callback_query(F.data.startswith("fd_adm_item_rm:"))
async def cb_adm_item_rm(cb: CallbackQuery, config: FoodDeliveryConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    item_id = int(cb.data.split(":", 1)[1])
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute("UPDATE menu_items SET active=0 WHERE id=?", (item_id,))
        await db.commit()
    await cb.message.edit_text("🍔 <b>Блюда</b>", parse_mode="HTML", reply_markup=await kb_items_admin(config.db_path))


@router.callback_query(F.data.startswith("fd_adm_item_edit:"))
async def cb_adm_item_edit(cb: CallbackQuery, config: FoodDeliveryConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    item_id = int(cb.data.split(":", 1)[1])
    item = await _item_row(config.db_path, item_id)
    if item is None:
        await cb.message.edit_text("Блюдо не найдено.", reply_markup=await kb_items_admin(config.db_path)); return
    await cb.message.edit_text(f"✏️ <b>{_esc(item['name'])}</b>", parse_mode="HTML", reply_markup=kb_item_edit_menu(item_id))


@router.callback_query(F.data.startswith("fd_adm_item_field:"))
async def cb_adm_item_field(cb: CallbackQuery, state: FSMContext, config: FoodDeliveryConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        _, item_id_s, field = cb.data.split(":", 2)
        item_id = int(item_id_s)
    except ValueError:
        return
    if field not in ("name", "description", "price", "photo"):
        return
    await state.set_state(AdminItemFlow.edit_value)
    await state.update_data(started_at=time.time(), edit_item_id=item_id, edit_field=field)
    prompts = {
        "name": "Новое название:", "description": "Новое описание:",
        "price": "Новая цена:", "photo": "Пришлите новое фото:",
    }
    await cb.message.edit_text(prompts[field], reply_markup=kb_flow_cancel())


@router.message(AdminItemFlow.edit_value, F.text, ~F.text.startswith("/"))
async def adm_item_edit_value(msg: Message, state: FSMContext, config: FoodDeliveryConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_admin_menu()); return
    field = data.get("edit_field")
    item_id = data.get("edit_item_id")
    if field == "photo" or not item_id:
        await msg.answer("Пришлите фото сообщением, не текстом.", reply_markup=kb_flow_cancel()); return
    if field == "price":
        value = _valid_price(msg.text)
        if value is None:
            await msg.answer(f"Введите целое число от 1 до {PRICE_MAX}:", reply_markup=kb_flow_cancel()); return
    elif field == "name":
        value = msg.text.strip()[:NAME_MAX_LEN]
        if not value:
            await msg.answer("Название не может быть пустым.", reply_markup=kb_flow_cancel()); return
    else:
        value = msg.text.strip()[:DESCRIPTION_MAX_LEN]
    await state.clear()
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute(f"UPDATE menu_items SET {field}=? WHERE id=?", (value, item_id))
        await db.commit()
    await msg.answer("✅ Обновлено.", reply_markup=await kb_items_admin(config.db_path))


@router.message(AdminItemFlow.edit_value, F.photo)
async def adm_item_edit_photo(msg: Message, state: FSMContext, config: FoodDeliveryConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_admin_menu()); return
    if data.get("edit_field") != "photo" or not data.get("edit_item_id"):
        return
    item_id = data["edit_item_id"]
    await state.clear()
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute("UPDATE menu_items SET photo_file_id=? WHERE id=?", (msg.photo[-1].file_id, item_id))
        await db.commit()
    await msg.answer("✅ Фото обновлено.", reply_markup=await kb_items_admin(config.db_path))


# ── ADMIN: orders ─────────────────────────────────────────────────────────────

@router.callback_query(F.data == "fd_adm_orders")
async def cb_adm_orders(cb: CallbackQuery, state: FSMContext, config: FoodDeliveryConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    await cb.message.edit_text("🧾 Выберите фильтр:", reply_markup=kb_status_filters())


@router.callback_query(F.data.startswith("fd_adm_order_filter:"))
async def cb_adm_order_filter(cb: CallbackQuery, config: FoodDeliveryConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    status = cb.data.split(":", 1)[1]
    rows = await _orders_by_status(config.db_path, None if status == "all" else status)
    if not rows:
        await cb.message.edit_text("Заказов не найдено.", reply_markup=kb_status_filters()); return
    await cb.message.edit_text(f"📋 Заказы ({len(rows)}):", reply_markup=kb_order_list(rows, "fd_adm_orders"))


@router.callback_query(F.data == "fd_adm_kitchen")
async def cb_adm_kitchen(cb: CallbackQuery, config: FoodDeliveryConfig):
    """Kitchen queue — orders currently being cooked, per brief item #5."""
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    rows = await _orders_by_status(config.db_path, "cooking")
    if not rows:
        await cb.message.edit_text("👨‍🍳 На кухне сейчас пусто.", reply_markup=kb_admin_menu()); return
    await cb.message.edit_text(f"👨‍🍳 На кухне ({len(rows)}):", reply_markup=kb_order_list(rows, "fd_main"))


@router.callback_query(F.data.startswith("fd_adm_order_view:") | F.data.startswith("fd_order_view:"))
async def cb_adm_order_view(cb: CallbackQuery, config: FoodDeliveryConfig):
    if not _is_admin(cb.from_user.id, config):
        return
    await cb.answer()
    try:
        order_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    order = await _order_row(config.db_path, order_id)
    if order is None:
        await cb.message.edit_text("Заказ не найден.", reply_markup=kb_admin_menu()); return
    text = await _order_detail_text(config.db_path, order_id, for_admin=True)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_order_detail_admin(order_id, order["status"]))


@router.callback_query(F.data.startswith("fd_adm_order_status:"))
async def cb_adm_order_status(cb: CallbackQuery, config: FoodDeliveryConfig, bot: Bot):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        _, order_id_s, new_status = cb.data.split(":", 2)
        order_id = int(order_id_s)
    except ValueError:
        return
    if new_status not in STATUS_LABELS:
        return
    order = await _order_row(config.db_path, order_id)
    if order is None:
        await cb.message.edit_text("Заказ не найден.", reply_markup=kb_admin_menu()); return
    old_status = order["status"]
    if new_status not in STATUS_TRANSITIONS.get(old_status, []):
        # Stale button — same "re-render instead of no-op" principle as
        # every other template's status-transition handler.
        text = await _order_detail_text(config.db_path, order_id, for_admin=True)
        await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_order_detail_admin(order_id, old_status))
        return

    if old_status == "cooking" and new_status == "courier":
        # The one transition that needs a courier picked before it applies —
        # same shape as delivery_tracker.py's StatusPriceFlow gate.
        await cb.message.edit_text("🚴 Выберите курьера:", reply_markup=await kb_courier_pick(config.db_path, order_id))
        return

    applied, note = await _apply_status_change(config, bot, order_id, old_status, new_status)
    text = await _order_detail_text(config.db_path, order_id, extra_note=note if applied else None, for_admin=True)
    order = await _order_row(config.db_path, order_id)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_order_detail_admin(order_id, order["status"]))


@router.callback_query(F.data.startswith("fd_courier_assign:"))
async def cb_courier_assign(cb: CallbackQuery, config: FoodDeliveryConfig, bot: Bot):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        _, order_id_s, courier_id_s = cb.data.split(":", 2)
        order_id, courier_id = int(order_id_s), int(courier_id_s)
    except ValueError:
        return
    order = await _order_row(config.db_path, order_id)
    if order is None or order["status"] != "cooking":
        text = await _order_detail_text(config.db_path, order_id, for_admin=True) if order else "Заказ не найден."
        kb = kb_order_detail_admin(order_id, order["status"]) if order else kb_admin_menu()
        await cb.message.edit_text(text, parse_mode="HTML" if order else None, reply_markup=kb)
        return
    applied, note = await _apply_status_change(config, bot, order_id, "cooking", "courier", courier_id=courier_id)
    order = await _order_row(config.db_path, order_id)
    text = await _order_detail_text(config.db_path, order_id, extra_note=note if applied else None, for_admin=True)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_order_detail_admin(order_id, order["status"]))


# ── ADMIN: couriers CRUD ──────────────────────────────────────────────────────

@router.callback_query(F.data == "fd_adm_couriers")
async def cb_adm_couriers(cb: CallbackQuery, state: FSMContext, config: FoodDeliveryConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    couriers = await _active_couriers(config.db_path)
    await cb.message.edit_text("🚴 <b>Курьеры</b>\n\nНажмите на курьера, чтобы включить/выключить.", parse_mode="HTML", reply_markup=kb_couriers_admin(couriers))


@router.callback_query(F.data == "fd_adm_courier_add")
async def cb_adm_courier_add(cb: CallbackQuery, state: FSMContext, config: FoodDeliveryConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.set_state(CourierMgmtFlow.name)
    await state.update_data(started_at=time.time())
    await cb.message.edit_text("Имя курьера:", reply_markup=kb_flow_cancel())


@router.message(CourierMgmtFlow.name, F.text, ~F.text.startswith("/"))
async def courier_add_name(msg: Message, state: FSMContext):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_admin_menu()); return
    name = msg.text.strip()[:NAME_MAX_LEN]
    if not name:
        await msg.answer("Имя не может быть пустым.", reply_markup=kb_flow_cancel()); return
    await state.update_data(new_courier_name=name)
    await state.set_state(CourierMgmtFlow.phone)
    await msg.answer("📱 Телефон курьера (или «Пропустить»):", reply_markup=kb_optional_step("fd_adm_courier_phone_skip"))


async def _finalize_new_courier(state: FSMContext, config: FoodDeliveryConfig, phone: str | None) -> None:
    data = await state.get_data()
    name = data.get("new_courier_name")
    await state.clear()
    if not name:
        return
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute("INSERT INTO couriers (name, phone) VALUES (?,?)", (name, phone))
        await db.commit()


@router.message(CourierMgmtFlow.phone, F.text, ~F.text.startswith("/"))
async def courier_add_phone(msg: Message, state: FSMContext, config: FoodDeliveryConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_admin_menu()); return
    phone = _normalize_phone(msg.text.strip())
    await _finalize_new_courier(state, config, phone)
    await msg.answer("✅ Курьер добавлен.", reply_markup=kb_couriers_admin(await _active_couriers(config.db_path)))


@router.callback_query(CourierMgmtFlow.phone, F.data == "fd_adm_courier_phone_skip")
async def cb_courier_phone_skip(cb: CallbackQuery, state: FSMContext, config: FoodDeliveryConfig):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_admin_menu()); return
    await _finalize_new_courier(state, config, None)
    await cb.message.edit_text("✅ Курьер добавлен.", reply_markup=kb_couriers_admin(await _active_couriers(config.db_path)))


@router.callback_query(F.data.startswith("fd_adm_courier_toggle:"))
async def cb_courier_toggle(cb: CallbackQuery, config: FoodDeliveryConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    courier_id = int(cb.data.split(":", 1)[1])
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute("UPDATE couriers SET active = NOT active WHERE id=?", (courier_id,))
        await db.commit()
    await cb.message.edit_text("🚴 <b>Курьеры</b>", parse_mode="HTML", reply_markup=kb_couriers_admin(await _active_couriers(config.db_path)))


# ── ADMIN: promo codes CRUD ───────────────────────────────────────────────────

@router.callback_query(F.data == "fd_adm_promo")
async def cb_adm_promo(cb: CallbackQuery, state: FSMContext, config: FoodDeliveryConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    promos = await _active_promos(config.db_path)
    await cb.message.edit_text("🎟 <b>Промокоды</b>", parse_mode="HTML", reply_markup=kb_promo_admin(promos))


@router.callback_query(F.data == "fd_adm_promo_add")
async def cb_adm_promo_add(cb: CallbackQuery, state: FSMContext, config: FoodDeliveryConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.set_state(PromoMgmtFlow.code)
    await state.update_data(started_at=time.time())
    await cb.message.edit_text("Код промокода (буквы/цифры):", reply_markup=kb_flow_cancel())


@router.message(PromoMgmtFlow.code, F.text, ~F.text.startswith("/"))
async def promo_add_code(msg: Message, state: FSMContext, config: FoodDeliveryConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_admin_menu()); return
    code = _valid_promo_code(msg.text)
    if code is None:
        await msg.answer("Только буквы/цифры/дефис/подчёркивание, до 30 символов.", reply_markup=kb_flow_cancel()); return
    existing = await _promo_row(config.db_path, code)
    if existing is not None:
        await msg.answer("⚠️ Такой промокод уже существует.", reply_markup=kb_flow_cancel()); return
    await state.update_data(new_promo_code=code)
    await state.set_state(PromoMgmtFlow.discount)
    await msg.answer("Размер скидки в % (1-100):", reply_markup=kb_flow_cancel())


@router.message(PromoMgmtFlow.discount, F.text, ~F.text.startswith("/"))
async def promo_add_discount(msg: Message, state: FSMContext):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_admin_menu()); return
    pct = _valid_discount_percent(msg.text)
    if pct is None:
        await msg.answer("Введите число от 1 до 100.", reply_markup=kb_flow_cancel()); return
    await state.update_data(new_promo_discount=pct)
    await state.set_state(PromoMgmtFlow.uses)
    await msg.answer("Лимит использований (число), или «Пропустить» для безлимитного:", reply_markup=kb_optional_step("fd_adm_promo_uses_skip"))


async def _finalize_new_promo(state: FSMContext, config: FoodDeliveryConfig, uses_left: int | None) -> None:
    data = await state.get_data()
    code = data.get("new_promo_code")
    pct = data.get("new_promo_discount")
    await state.clear()
    if not code or not pct:
        return
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute(
            "INSERT INTO promo_codes (code, discount_percent, uses_left) VALUES (?,?,?)", (code, pct, uses_left),
        )
        await db.commit()


@router.message(PromoMgmtFlow.uses, F.text, ~F.text.startswith("/"))
async def promo_add_uses(msg: Message, state: FSMContext, config: FoodDeliveryConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_admin_menu()); return
    uses = _valid_points(msg.text)  # non-negative int, same shape needed here
    if uses is None:
        await msg.answer("Введите целое неотрицательное число, или «Пропустить»:", reply_markup=kb_optional_step("fd_adm_promo_uses_skip")); return
    await _finalize_new_promo(state, config, uses)
    await msg.answer("✅ Промокод создан.", reply_markup=kb_promo_admin(await _active_promos(config.db_path)))


@router.callback_query(PromoMgmtFlow.uses, F.data == "fd_adm_promo_uses_skip")
async def cb_promo_uses_skip(cb: CallbackQuery, state: FSMContext, config: FoodDeliveryConfig):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_admin_menu()); return
    await _finalize_new_promo(state, config, None)
    await cb.message.edit_text("✅ Промокод создан.", reply_markup=kb_promo_admin(await _active_promos(config.db_path)))


@router.callback_query(F.data.startswith("fd_adm_promo_rm:"))
async def cb_promo_rm(cb: CallbackQuery, config: FoodDeliveryConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    code = cb.data.split(":", 1)[1]
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute("UPDATE promo_codes SET active=0 WHERE code=?", (code,))
        await db.commit()
    await cb.message.edit_text("🎟 <b>Промокоды</b>", parse_mode="HTML", reply_markup=kb_promo_admin(await _active_promos(config.db_path)))


# ── ADMIN: analytics ──────────────────────────────────────────────────────────

@router.callback_query(F.data == "fd_adm_stats")
async def cb_adm_stats(cb: CallbackQuery, config: FoodDeliveryConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    stats = await _stats(config.db_path)
    lines = [
        "📊 <b>Аналитика</b>\n",
        f"📦 Доставленных заказов: {stats['total_orders']}",
        f"💰 Выручка: {stats['revenue']}{CURRENCY_SYMBOL}",
        f"🧾 Средний чек: {stats['avg_check']:.0f}{CURRENCY_SYMBOL}",
        f"🕐 Среднее время доставки: {stats['avg_minutes']:.0f} мин",
    ]
    if stats["top"]:
        lines.append("\n<b>Топ блюд:</b>")
        for t in stats["top"]:
            lines.append(f"• {_esc(t['name'])} — {t['units']} шт.")
    if stats["by_day"]:
        lines.append("\n<b>Выручка по дням:</b>")
        for d in stats["by_day"]:
            lines.append(f"• {d['day']}: {d['cnt']} заказ(ов), {d['revenue']}{CURRENCY_SYMBOL}")
    await cb.message.edit_text(_join_bounded(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[kb_back()]]))


# ── ADMINS menu ────────────────────────────────────────────────────────────────

async def _admins_list_text(config: FoodDeliveryConfig) -> str:
    ids = sorted(_load_admins(config.admins_file))
    if not ids:
        return "👥 Пусто"
    return _join_bounded(["👥 <b>Администраторы бота:</b>\n"] + [f"• <code>{_esc(i)}</code>" for i in ids])


@router.callback_query(F.data == "fd_adm_admins")
async def cb_adm_admins(cb: CallbackQuery, state: FSMContext, config: FoodDeliveryConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    text = await _admins_list_text(config)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_admins_menu())


@router.callback_query(F.data == "fd_adm_add_admin")
async def cb_adm_add_admin(cb: CallbackQuery, state: FSMContext, config: FoodDeliveryConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.set_state(AdminMgmtFlow.add_admin)
    await state.update_data(started_at=time.time())
    await cb.message.edit_text("Введите Telegram ID нового администратора:", reply_markup=kb_flow_cancel())


@router.message(AdminMgmtFlow.add_admin, F.text, ~F.text.startswith("/"))
async def admin_add_id(msg: Message, state: FSMContext, config: FoodDeliveryConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_admin_menu()); return
    text = msg.text.strip()
    if not _valid_admin_id(text):
        await msg.answer("Некорректный ID. Введите числовой Telegram ID.", reply_markup=kb_flow_cancel()); return
    await state.clear()
    ids = _load_admins(config.admins_file)
    ids.add(text)
    _save_admins(config.admins_file, ids)
    await msg.answer(f"✅ <code>{text}</code> добавлен.", parse_mode="HTML", reply_markup=kb_admins_menu())


@router.callback_query(F.data == "fd_adm_remove_admin")
async def cb_adm_remove_admin(cb: CallbackQuery, state: FSMContext, config: FoodDeliveryConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    ids = sorted(_load_admins(config.admins_file))
    if len(ids) <= 1:
        await cb.message.edit_text("Нельзя удалить последнего администратора.", reply_markup=kb_admins_menu()); return
    if len(ids) > MAX_ADMIN_REMOVE_BUTTONS:
        await cb.message.edit_text("Слишком много админов для списка кнопок. Обратитесь к разработчику.", reply_markup=kb_admins_menu()); return
    await state.set_state(AdminMgmtFlow.remove_admin_pick)
    await state.update_data(started_at=time.time(), remove_admin_ids=ids)
    await cb.message.edit_text("Выберите администратора для удаления:", reply_markup=kb_remove_admins(ids))


@router.callback_query(AdminMgmtFlow.remove_admin_pick, F.data.startswith("fd_adm_rma:"))
async def cb_adm_remove_pick(cb: CallbackQuery, state: FSMContext, config: FoodDeliveryConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_admin_menu()); return
    try:
        idx = int(cb.data.split(":", 1)[1])
        target = data["remove_admin_ids"][idx]
    except (ValueError, IndexError, KeyError):
        await state.clear()
        await cb.message.edit_text("Некорректный выбор.", reply_markup=kb_admins_menu()); return
    ids = _load_admins(config.admins_file)
    if len(ids) <= 1:
        await state.clear()
        await cb.message.edit_text("Нельзя удалить последнего администратора.", reply_markup=kb_admins_menu()); return
    ids.discard(target)
    _save_admins(config.admins_file, ids)
    await state.clear()
    await cb.message.edit_text(f"✅ <code>{_esc(target)}</code> удалён.", parse_mode="HTML", reply_markup=kb_admins_menu())


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
