# TEMPLATE: delivery_tracker
# USE FOR: курьерская служба доставки (документы, цветы, посылки, еда без каталога товаров — просто "что и куда доставить"): приём заказа от клиента, статус-флоу с автоуведомлением клиента на КАЖДОЕ изменение статуса, ввод цены при переводе в "принят", приватные заметки курьера
# CUSTOMIZE: sections marked with # CUSTOMIZE
from __future__ import annotations

import asyncio
import html
import logging
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

import aiosqlite
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

# ── CUSTOMIZE ────────────────────────────────────────────────────────────────
BOT_DESCRIPTION = (
    "Курьерская служба доставки: клиент оставляет заказ (откуда/куда/что "
    "доставить), статус-флоу (новый → принят → забрано → в пути → доставлено), "
    "клиент автоматически уведомляется о каждом изменении статуса."
)
WELCOME_TEXT = (
    "🚚 <b>Служба доставки — трекер заказов</b>\n\n"
    "Приём заказов, статус-флоу: новый → принят → забрано → в пути → "
    "доставлено. Клиент уведомляется о каждом изменении статуса.\n\n"
    "Выберите действие:"
)
CLIENT_WELCOME_TEXT = (
    "👋 Здравствуйте! Это бот курьерской службы доставки.\n\n"
    "Оставьте заказ — и мы сообщим вам о каждом изменении его статуса."
)
# ── END CUSTOMIZE ─────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
router = Router()

# Forward-only flow, same shape as templates/repair_tracker.py's
# STATUS_TRANSITIONS: no backward moves. "cancelled" is reachable from any
# non-terminal status as a side-branch.
STATUS_TRANSITIONS = {
    "new": ["accepted", "cancelled"],
    "accepted": ["picked_up", "cancelled"],
    "picked_up": ["in_transit", "cancelled"],
    "in_transit": ["delivered", "cancelled"],
    "delivered": [],
    "cancelled": [],
}

# "Активные заказы" = everything except the two terminal statuses.
TERMINAL_STATUSES = ("delivered", "cancelled")

# Labels WITH emoji — used on buttons and in the admin/client detail cards.
STATUS_LABELS = {
    "new": "🆕 Новый",
    "accepted": "✅ Принят",
    "picked_up": "📦 Забрано",
    "in_transit": "🚚 В пути",
    "delivered": "🏁 Доставлено",
    "cancelled": "❌ Отменён",
}

# Plain labels (no emoji) — used inside the client notification's «quoted»
# phrase.
STATUS_LABEL_PLAIN = {
    "new": "Новый",
    "accepted": "Принят",
    "picked_up": "Забрано",
    "in_transit": "В пути",
    "delivered": "Доставлено",
    "cancelled": "Отменён",
}


# ── config ───────────────────────────────────────────────────────────────────
# Same pattern as every other template — see docs/STAGE2_DESIGN.md.

@dataclass
class DeliveryTrackerConfig:
    bot_name: str
    db_path: str
    admins_file: Path
    welcome_image: Path
    display_name: str | None = None
    group_chat_id: str | None = None


def _paths_for(name: str, data_dir: Path) -> DeliveryTrackerConfig:
    return DeliveryTrackerConfig(
        bot_name=name,
        db_path=str(data_dir / f"{name}_data.db"),
        admins_file=data_dir / f"admins_{name}.json",
        welcome_image=data_dir / "bot_images" / f"{name}.jpg",
    )


def config_from_env() -> DeliveryTrackerConfig:
    """Standalone/subprocess mode."""
    name = Path(__file__).stem
    data_dir = Path(os.getenv("DATA_DIR", "./data"))
    data_dir.mkdir(exist_ok=True)
    return _paths_for(name, data_dir)


def config_from_bot_row(bot_row: dict, data_dir: Path) -> DeliveryTrackerConfig:
    """Webhook runtime mode. Paths built from bot_row["bot_id"] (bots.id, the
    physically unique AUTOINCREMENT PK) — NOT bot_row["name"] — same reasoning
    as every other template's config_from_bot_row (see docs/STAGE2_DESIGN.md
    "Изоляция по bots.id"). This is the ENTIRE data-isolation guarantee: two
    bots running this same module get two distinct db_path/admins_file pairs
    derived from their own bot_id, so even the identical Telegram admin
    user_id driving both bots can never see the other bot's rows — every
    query below is scoped to config.db_path, which is per-bot."""
    bot_id = bot_row["bot_id"]
    config = DeliveryTrackerConfig(
        bot_name=bot_row["name"],
        db_path=str(data_dir / f"bot_{bot_id}_data.db"),
        admins_file=data_dir / f"admins_{bot_id}.json",
        welcome_image=data_dir / "bot_images" / f"bot_{bot_id}.jpg",
    )
    config.display_name = bot_row.get("display_name")
    config.group_chat_id = bot_row.get("group_chat_id")
    return config


class ConfigMiddleware(BaseMiddleware):
    """Injects this bot's DeliveryTrackerConfig into data["config"]."""

    def __init__(self, config: DeliveryTrackerConfig) -> None:
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

def _is_admin(user_id: int, config: DeliveryTrackerConfig) -> bool:
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


# ── phone normalization ────────────────────────────────────────────────────────
# Same RU-phone formula as templates/vehicle_service.py's _normalize_phone(),
# reused verbatim per the project convention.

def _normalize_phone(raw: str) -> str | None:
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
    if price < 0 or price > 1_000_000_000:
        return None
    return price


def _valid_admin_id(text: str) -> bool:
    if not (bool(text) and text.isascii() and text.isdigit() and len(text) <= 15):
        return False
    return int(text) > 0 and str(int(text)) == text


# ── db ────────────────────────────────────────────────────────────────────────
# DESIGN NOTE — data model: a single `deliveries` table, schema fixed by the
# design brief. client_user_id is captured DIRECTLY from the Telegram user
# who runs the "📦 Заказать доставку" flow (same simplification as
# repair_tracker.py's repair_tickets — no separate linking step needed since
# a delivery is only ever created BY the client themselves). "Мои доставки"
# and the detail view both enforce `WHERE client_user_id = <requesting
# user's own id>` on every read.
#
# DESIGN NOTE — notifications: client is notified on EVERY status change
# (best-effort, catches TelegramAPIError so a blocked bot never crashes the
# admin flow). _apply_status_change() does one compare-and-swap UPDATE
# (`WHERE id=? AND status=?`) per transition — a duplicate tap on an
# already-applied transition button hits rowcount=0 and is a no-op, so the
# client is never notified twice for one real change.
#
# DESIGN NOTE — status graph: STATUS_TRANSITIONS above is forward-only
# (new → accepted → picked_up → in_transit → delivered) with "cancelled"
# reachable as a side-branch from any non-terminal status, mirroring
# repair_tracker.py/vehicle_service.py precedent. The one stateful branch is
# new → accepted, which per the brief must collect a price BEFORE applying:
# cb_dlv_status() detects that specific (old_status, target) pair and
# diverts into StatusPriceFlow instead of calling _apply_status_change()
# immediately; every other transition applies directly.
#
# DESIGN NOTE — courier_note is admin-only, never rendered in any
# client-facing text (see _client_delivery_text() below, which deliberately
# omits it, same as repair_tracker.py's admin_note).

async def init_db(db_path: str):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS deliveries (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                client_user_id      INTEGER NOT NULL,
                client_name         TEXT,
                client_phone        TEXT,
                pickup_address      TEXT NOT NULL,
                dropoff_address     TEXT NOT NULL,
                item_description    TEXT NOT NULL,
                status              TEXT NOT NULL DEFAULT 'new'
                                    CHECK(status IN ('new','accepted','picked_up','in_transit','delivered','cancelled')),
                price               INTEGER,
                courier_note        TEXT,
                created_at          TEXT DEFAULT (datetime('now','localtime')),
                updated_at          TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_deliveries_client ON deliveries(client_user_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_deliveries_status ON deliveries(status)")
        await db.commit()


# ── mini-app config (see docs/MINIAPP_DESIGN.md, templates/car_rental.py's
# own miniapp_config for the authoring contract) ────────────────────────────
# Not creatable: every real delivery is created by a client via DeliveryFlow
# (pickup/dropoff/item/phone), never by the owner typing raw rows — same
# read-only posture as car_rental.py's bookings.status field (owner-managed
# transitions belong in Telegram's guided status/price/note flows, not a
# generic create form that would bypass StatusPriceFlow's price-collection
# step).
miniapp_config = {
    "resources": [
        {
            "name": "deliveries",
            "table": "deliveries",
            "order_by": "created_at DESC",
            "creatable": False,
            "title": "Доставки",
            "titleField": "item_description",
            "fields": [
                {"name": "client_name", "label": "Клиент", "kind": "text", "list": True, "detail": True, "create": False},
                {"name": "client_phone", "label": "Телефон", "kind": "text", "list": False, "detail": True, "create": False},
                {"name": "pickup_address", "label": "Откуда", "kind": "text", "list": True, "detail": True, "create": False},
                {"name": "dropoff_address", "label": "Куда", "kind": "text", "list": True, "detail": True, "create": False},
                {"name": "item_description", "label": "Что везём", "kind": "text", "list": False, "detail": True, "create": False},
                {"name": "status", "label": "Статус", "kind": "status", "list": True, "detail": True, "create": False},
                {"name": "price", "label": "Цена", "kind": "number", "list": True, "detail": True, "create": False},
                {"name": "courier_note", "label": "Заметка курьера", "kind": "text", "list": False, "detail": True, "create": False},
                {"name": "created_at", "label": "Создано", "kind": "date", "list": False, "detail": True, "create": False},
            ],
        },
    ],
}


# ── FSM staleness guard ─────────────────────────────────────────────────────────
FLOW_TIMEOUT_SECONDS = 300


def _flow_expired(data: dict) -> bool:
    started_at = data.get("started_at")
    return started_at is None or (time.time() - started_at) > FLOW_TIMEOUT_SECONDS


# ── FSM states ───────────────────────────────────────────────────────────────

class DeliveryFlow(StatesGroup):
    """Client-side: pickup address → dropoff address → item description → phone → create."""
    pickup = State()
    dropoff = State()
    item = State()
    phone = State()

class StatusPriceFlow(StatesGroup):
    """Admin-side: the ONE transition (new → accepted) that needs a price
    collected before it applies."""
    price = State()

class NoteFlow(StatesGroup):
    """Admin-side: free-text courier note attached to a delivery, never shown
    to the client."""
    text = State()

class AdminMgmtFlow(StatesGroup):
    add_admin = State()
    remove_admin_pick = State()


MAX_LIST_BUTTONS = 25
MAX_ADMIN_REMOVE_BUTTONS = 30
MAX_ADDRESS_LEN = 300
MAX_ITEM_LEN = 500
MAX_NOTE_LEN = 1000


# ── keyboards ─────────────────────────────────────────────────────────────────

def kb_back(callback_data: str = "main_menu") -> InlineKeyboardButton:
    return InlineKeyboardButton(text="◀️ Назад", callback_data=callback_data)

def kb_flow_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="flow_cancel")],
    ])

def kb_phone_skip() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭ Пропустить", callback_data="dlv_phone_skip")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="flow_cancel")],
    ])

# ── client menu ──
def kb_client_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Заказать доставку", callback_data="dlv_new")],
        [InlineKeyboardButton(text="📋 Мои доставки", callback_data="dlv_mine")],
    ])

# ── admin menu ──
def kb_admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚚 Активные заказы", callback_data="adlv_active")],
        [InlineKeyboardButton(text="📋 Все доставки", callback_data="adlv_all")],
        [InlineKeyboardButton(text="💬 Заметка курьера", callback_data="adlv_note")],
        [InlineKeyboardButton(text="👥 Админы", callback_data="adm_menu")],
    ])

def _menu_for(user_id: int, config: DeliveryTrackerConfig) -> tuple[str, InlineKeyboardMarkup]:
    if _is_admin(user_id, config):
        return WELCOME_TEXT, kb_admin_menu()
    return CLIENT_WELCOME_TEXT, kb_client_menu()


def kb_delivery_list(rows: list[tuple], callback_prefix: str, back_cb: str) -> InlineKeyboardMarkup:
    """Shared list-rendering for both the client's "Мои доставки" and every
    admin delivery list — rows are (id, status, item_description) tuples."""
    btns = [
        [InlineKeyboardButton(
            text=f"№{did} · {STATUS_LABELS.get(status, status)} · {_esc(item, 25)}",
            callback_data=f"{callback_prefix}:{did}",
        )]
        for did, status, item in rows[:MAX_LIST_BUTTONS]
    ]
    btns.append([kb_back(back_cb)])
    return InlineKeyboardMarkup(inline_keyboard=btns)


_STATUS_FILTERS = [(code, STATUS_LABELS[code]) for code in STATUS_LABELS] + [("all", "📋 Все")]

def kb_status_filters() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=label, callback_data=f"adlv_filter:{code}")] for code, label in _STATUS_FILTERS]
    rows.append([kb_back("main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_delivery_detail_admin(delivery_id: int, status: str) -> InlineKeyboardMarkup:
    rows = []
    for target in STATUS_TRANSITIONS.get(status, []):
        icon = "❌ Отменить" if target == "cancelled" else f"▶️ {STATUS_LABELS.get(target, target)}"
        rows.append([InlineKeyboardButton(text=icon, callback_data=f"adlv_status:{delivery_id}:{target}")])
    rows.append([InlineKeyboardButton(text="💬 Заметка", callback_data=f"note_pick:{delivery_id}")])
    rows.append([kb_back("adlv_all")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_delivery_detail_client(delivery_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[kb_back("dlv_mine")]])

def kb_admins_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить админа", callback_data="adm_add")],
        [InlineKeyboardButton(text="➖ Убрать админа", callback_data="adm_remove")],
        [kb_back()],
    ])

def kb_remove_admins(ids: list[str]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=admin_id, callback_data=f"adm_rm:{i}")] for i, admin_id in enumerate(ids)]
    rows.append([kb_back("adm_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── rendering helpers ────────────────────────────────────────────────────────

async def _admin_delivery_detail(db_path: str, delivery_id: int, extra_note: str | None = None) -> tuple[str, str] | None:
    """Returns (rendered_html, status) for the admin-facing detail card, or
    None if the delivery doesn't exist. Includes courier_note — this view is
    ADMIN-ONLY, never reused for the client-facing card below."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute("SELECT * FROM deliveries WHERE id=?", (delivery_id,))).fetchone()
    if not row:
        return None
    lines = [
        f"🚚 <b>Доставка №{row['id']}</b> · {STATUS_LABELS.get(row['status'], row['status'])}\n",
        f"👤 {_esc(row['client_name'] or 'Без имени')}"
        + (f" · {_esc(row['client_phone'])}" if row["client_phone"] else ""),
        f"📍 Откуда: {_esc(row['pickup_address'])}",
        f"🎯 Куда: {_esc(row['dropoff_address'])}",
        f"📦 Что доставить: {_esc(row['item_description'])}",
    ]
    if row["price"] is not None:
        lines.append(f"💰 Цена: {row['price']}")
    if row["courier_note"]:
        lines.append(f"📝 Заметка курьера: {_esc(row['courier_note'])}")
    lines.append(f"🕐 Создана: {row['created_at']} · Обновлена: {row['updated_at']}")
    if extra_note:
        lines.append(f"\n{extra_note}")
    return _join_bounded(lines), row["status"]


def _client_delivery_text(row) -> str:
    """Client-facing detail card — deliberately excludes courier_note (never
    shown to the client, per the brief)."""
    lines = [
        f"🚚 <b>Доставка №{row['id']}</b> · {STATUS_LABELS.get(row['status'], row['status'])}\n",
        f"📍 Откуда: {_esc(row['pickup_address'])}",
        f"🎯 Куда: {_esc(row['dropoff_address'])}",
        f"📦 Что доставить: {_esc(row['item_description'])}",
    ]
    if row["price"] is not None:
        lines.append(f"💰 Цена: {row['price']}")
    lines.append(f"🕐 Создана: {row['created_at']}")
    return _join_bounded(lines)


async def _apply_status_change(
    config: DeliveryTrackerConfig, bot: Bot, delivery_id: int, old_status: str, new_status: str,
    price: int | None = None,
) -> tuple[bool, str | None]:
    """Compare-and-swap status UPDATE (`WHERE id=? AND status=?`) + client
    notification. Returns (applied, note):
    - applied=False means rowcount was 0 — either a stale/double-tapped
      button, NOT an error; caller re-renders the delivery's actual current
      state.
    - applied=True means THIS call performed the transition — the client is
      then notified (best-effort; a TelegramAPIError, e.g. the client
      blocked the bot, does not roll back the status change)."""
    async with aiosqlite.connect(config.db_path) as db:
        if price is not None:
            cur = await db.execute(
                "UPDATE deliveries SET status=?, price=?, updated_at=datetime('now','localtime') "
                "WHERE id=? AND status=?",
                (new_status, price, delivery_id, old_status),
            )
        else:
            cur = await db.execute(
                "UPDATE deliveries SET status=?, updated_at=datetime('now','localtime') WHERE id=? AND status=?",
                (new_status, delivery_id, old_status),
            )
        if cur.rowcount == 0:
            await db.commit()
            return False, None
        row = await (await db.execute(
            "SELECT client_user_id, item_description FROM deliveries WHERE id=?", (delivery_id,)
        )).fetchone()
        await db.commit()

    note = None
    if row:
        client_user_id, item_description = row
        label = STATUS_LABEL_PLAIN.get(new_status, new_status)
        text = (
            f"🔔 Ваша доставка №{delivery_id}: статус изменён на «{label}».\n"
            f"Что доставить: {_esc(item_description)}"
        )
        try:
            await bot.send_message(client_user_id, text)
            note = "🔔 Клиент уведомлён."
        except TelegramAPIError as e:
            logger.warning(f"delivery_tracker: failed to notify client for delivery {delivery_id}: {e}")
            note = "⚠️ Не удалось уведомить клиента (возможно, заблокировал бота)."
    return True, note


# ── /start ────────────────────────────────────────────────────────────────────

@router.message(Command("start"), F.chat.type == "private")
async def cmd_start(message: Message, state: FSMContext, config: DeliveryTrackerConfig):
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


@router.callback_query(F.data == "main_menu")
async def cb_main_menu(cb: CallbackQuery, state: FSMContext, config: DeliveryTrackerConfig):
    await cb.answer()
    await state.clear()
    text, kb = _menu_for(cb.from_user.id, config)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "flow_cancel")
async def cb_flow_cancel(cb: CallbackQuery, state: FSMContext, config: DeliveryTrackerConfig):
    await cb.answer()
    await state.clear()
    _text, kb = _menu_for(cb.from_user.id, config)
    await cb.message.edit_text("Отменено.", reply_markup=kb)


# ── CLIENT: new delivery flow (pickup → dropoff → item → phone[optional] → create) ──

@router.callback_query(F.data == "dlv_new")
async def cb_dlv_new(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.clear()
    await state.set_state(DeliveryFlow.pickup)
    await state.update_data(started_at=time.time())
    await cb.message.edit_text(
        "📍 Откуда забрать? Укажите адрес:", reply_markup=kb_flow_cancel(),
    )


@router.message(DeliveryFlow.pickup, F.text, ~F.text.startswith("/"))
async def dlv_pickup(msg: Message, state: FSMContext):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_client_menu())
        return
    pickup = msg.text.strip()
    if not pickup:
        await msg.answer("Адрес не может быть пустым. Откуда забрать?", reply_markup=kb_flow_cancel())
        return
    if len(pickup) > MAX_ADDRESS_LEN:
        await msg.answer(f"⚠️ Слишком длинный адрес. Уложитесь в {MAX_ADDRESS_LEN} символов:", reply_markup=kb_flow_cancel())
        return
    await state.update_data(pickup_address=pickup)
    await state.set_state(DeliveryFlow.dropoff)
    await msg.answer("🎯 Куда доставить? Укажите адрес:", reply_markup=kb_flow_cancel())


@router.message(DeliveryFlow.dropoff, F.text, ~F.text.startswith("/"))
async def dlv_dropoff(msg: Message, state: FSMContext):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_client_menu())
        return
    dropoff = msg.text.strip()
    if not dropoff:
        await msg.answer("Адрес не может быть пустым. Куда доставить?", reply_markup=kb_flow_cancel())
        return
    if len(dropoff) > MAX_ADDRESS_LEN:
        await msg.answer(f"⚠️ Слишком длинный адрес. Уложитесь в {MAX_ADDRESS_LEN} символов:", reply_markup=kb_flow_cancel())
        return
    await state.update_data(dropoff_address=dropoff)
    await state.set_state(DeliveryFlow.item)
    await msg.answer("📦 Что нужно доставить? (например: пакет документов)", reply_markup=kb_flow_cancel())


@router.message(DeliveryFlow.item, F.text, ~F.text.startswith("/"))
async def dlv_item(msg: Message, state: FSMContext):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_client_menu())
        return
    item = msg.text.strip()
    if not item:
        await msg.answer("Описание не может быть пустым. Что нужно доставить?", reply_markup=kb_flow_cancel())
        return
    if len(item) > MAX_ITEM_LEN:
        await msg.answer(f"⚠️ Слишком длинное описание. Уложитесь в {MAX_ITEM_LEN} символов:", reply_markup=kb_flow_cancel())
        return
    await state.update_data(item_description=item)
    await state.set_state(DeliveryFlow.phone)
    await msg.answer(
        "📱 Укажите номер телефона для связи (или нажмите «Пропустить»):", reply_markup=kb_phone_skip(),
    )


async def _finalize_delivery(message_answer, state: FSMContext, config: DeliveryTrackerConfig, bot: Bot, from_user, phone: str | None) -> None:
    data = await state.get_data()
    pickup = data.get("pickup_address")
    dropoff = data.get("dropoff_address")
    item = data.get("item_description")
    if not pickup or not dropoff or not item:
        await state.clear()
        await message_answer("Сессия устарела, начните заново.", reply_markup=kb_client_menu())
        return
    await state.clear()
    name = from_user.full_name
    async with aiosqlite.connect(config.db_path) as db:
        cur = await db.execute(
            "INSERT INTO deliveries (client_user_id, client_name, client_phone, pickup_address, dropoff_address, item_description) "
            "VALUES (?,?,?,?,?,?)",
            (from_user.id, name, phone, pickup, dropoff, item),
        )
        delivery_id = cur.lastrowid
        await db.commit()

    await message_answer(
        f"✅ Заказ №{delivery_id} создан! Мы свяжемся с вами по мере обработки.", reply_markup=kb_client_menu(),
    )

    admins = _load_admins(config.admins_file)
    notify_text = (
        f"🆕 Новый заказ на доставку №{delivery_id}\n"
        f"👤 {_esc(name)}" + (f" · {_esc(phone)}" if phone else "") + "\n"
        f"📍 Откуда: {_esc(pickup)}\n🎯 Куда: {_esc(dropoff)}\n📦 {_esc(item)}"
    )
    for admin_id in admins:
        try:
            await bot.send_message(int(admin_id), notify_text)
        except (TelegramAPIError, ValueError) as e:
            # ValueError covers a corrupt/non-numeric entry in admins.json —
            # one bad admin id must not crash the order-creation flow for
            # the client, who has already gotten their own confirmation above.
            logger.warning(f"delivery_tracker: failed to notify admin {admin_id} of new delivery {delivery_id}: {e}")


@router.message(DeliveryFlow.phone, F.text, ~F.text.startswith("/"))
async def dlv_phone(msg: Message, state: FSMContext, config: DeliveryTrackerConfig, bot: Bot):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_client_menu())
        return
    phone = _normalize_phone(msg.text.strip())
    if phone is None:
        await msg.answer(
            "❌ Не удалось распознать номер. Введите номер, например <b>+7 999 123-45-67</b>, "
            "или нажмите «Пропустить»:", parse_mode="HTML", reply_markup=kb_phone_skip(),
        )
        return
    await _finalize_delivery(msg.answer, state, config, bot, msg.from_user, phone)


@router.callback_query(DeliveryFlow.phone, F.data == "dlv_phone_skip")
async def cb_dlv_phone_skip(cb: CallbackQuery, state: FSMContext, config: DeliveryTrackerConfig, bot: Bot):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_client_menu())
        return
    await _finalize_delivery(cb.message.answer, state, config, bot, cb.from_user, None)


# ── CLIENT: "Мои доставки" ────────────────────────────────────────────────────

@router.callback_query(F.data == "dlv_mine")
async def cb_dlv_mine(cb: CallbackQuery, state: FSMContext, config: DeliveryTrackerConfig):
    await cb.answer()
    await state.clear()
    async with aiosqlite.connect(config.db_path) as db:
        rows = await (await db.execute(
            "SELECT id, status, item_description FROM deliveries WHERE client_user_id=? ORDER BY id DESC LIMIT ?",
            (cb.from_user.id, MAX_LIST_BUTTONS),
        )).fetchall()
    if not rows:
        await cb.message.edit_text("У вас пока нет доставок.", reply_markup=kb_client_menu())
        return
    await cb.message.edit_text("📋 Ваши доставки:", reply_markup=kb_delivery_list(rows, "dlv_view", "main_menu"))


@router.callback_query(F.data.startswith("dlv_view:"))
async def cb_dlv_view(cb: CallbackQuery, config: DeliveryTrackerConfig):
    await cb.answer()
    try:
        delivery_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    # Ownership check: a hand-crafted callback_data guessing another
    # client's delivery id must not leak it. "not found" is deliberately
    # used for BOTH "doesn't exist" and "exists but isn't yours".
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            "SELECT * FROM deliveries WHERE id=? AND client_user_id=?", (delivery_id, cb.from_user.id)
        )).fetchone()
    if not row:
        await cb.message.edit_text("Доставка не найдена.", reply_markup=kb_client_menu())
        return
    await cb.message.edit_text(_client_delivery_text(row), parse_mode="HTML", reply_markup=kb_delivery_detail_client(delivery_id))


# ── ADMIN: delivery lists ────────────────────────────────────────────────────

@router.callback_query(F.data == "adlv_active")
async def cb_adlv_active(cb: CallbackQuery, state: FSMContext, config: DeliveryTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    placeholders = ",".join("?" * len(TERMINAL_STATUSES))
    async with aiosqlite.connect(config.db_path) as db:
        rows = await (await db.execute(
            f"SELECT id, status, item_description FROM deliveries WHERE status NOT IN ({placeholders}) "
            "ORDER BY id DESC LIMIT ?",
            (*TERMINAL_STATUSES, MAX_LIST_BUTTONS),
        )).fetchall()
    if not rows:
        await cb.message.edit_text("Активных заказов нет.", reply_markup=kb_admin_menu())
        return
    await cb.message.edit_text(f"🚚 Активные заказы ({len(rows)}):", reply_markup=kb_delivery_list(rows, "adlv_view", "main_menu"))


@router.callback_query(F.data == "adlv_all")
async def cb_adlv_all(cb: CallbackQuery, config: DeliveryTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await cb.message.edit_text("Выберите фильтр по статусу:", reply_markup=kb_status_filters())


@router.callback_query(F.data.startswith("adlv_filter:"))
async def cb_adlv_filter(cb: CallbackQuery, config: DeliveryTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    status = cb.data.split(":", 1)[1]
    async with aiosqlite.connect(config.db_path) as db:
        if status == "all":
            rows = await (await db.execute(
                "SELECT id, status, item_description FROM deliveries ORDER BY id DESC LIMIT ?", (MAX_LIST_BUTTONS,)
            )).fetchall()
        else:
            rows = await (await db.execute(
                "SELECT id, status, item_description FROM deliveries WHERE status=? ORDER BY id DESC LIMIT ?",
                (status, MAX_LIST_BUTTONS),
            )).fetchall()
    if not rows:
        await cb.message.edit_text("Доставок не найдено.", reply_markup=kb_status_filters())
        return
    await cb.message.edit_text(f"📋 Доставки (последние {len(rows)}):", reply_markup=kb_delivery_list(rows, "adlv_view", "adlv_all"))


@router.callback_query(F.data.startswith("adlv_view:"))
async def cb_adlv_view(cb: CallbackQuery, config: DeliveryTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        delivery_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    res = await _admin_delivery_detail(config.db_path, delivery_id)
    if res is None:
        await cb.message.edit_text("Доставка не найдена.", reply_markup=kb_admin_menu())
        return
    text, status = res
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_delivery_detail_admin(delivery_id, status))


# ── ADMIN: status transitions (new→accepted needs a price first) ─────────────

@router.callback_query(F.data.startswith("adlv_status:"))
async def cb_adlv_status(cb: CallbackQuery, state: FSMContext, config: DeliveryTrackerConfig, bot: Bot):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        _, delivery_id_s, new_status = cb.data.split(":", 2)
        delivery_id = int(delivery_id_s)
    except ValueError:
        return
    if new_status not in STATUS_LABELS:
        return

    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute("SELECT status FROM deliveries WHERE id=?", (delivery_id,))).fetchone()
    if not row:
        await cb.message.edit_text("Доставка не найдена.", reply_markup=kb_admin_menu())
        return
    old_status = row[0]
    if new_status not in STATUS_TRANSITIONS.get(old_status, []):
        # Stale button (already transitioned by another admin, or a
        # double-tap on an already-applied transition) — re-render instead
        # of silently no-op'ing.
        res = await _admin_delivery_detail(config.db_path, delivery_id)
        if res:
            text, status = res
            await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_delivery_detail_admin(delivery_id, status))
        return

    if old_status == "new" and new_status == "accepted":
        # The ONE transition that needs a price collected before it applies.
        await state.set_state(StatusPriceFlow.price)
        await state.update_data(started_at=time.time(), price_delivery_id=delivery_id, price_old_status=old_status)
        await cb.message.edit_text(
            "💰 Введите стоимость доставки (число, например 350):", reply_markup=kb_flow_cancel(),
        )
        return

    applied, note = await _apply_status_change(config, bot, delivery_id, old_status, new_status)
    res = await _admin_delivery_detail(config.db_path, delivery_id, extra_note=note if applied else None)
    if res is None:
        await cb.message.edit_text("Доставка не найдена.", reply_markup=kb_admin_menu())
        return
    text, status = res
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_delivery_detail_admin(delivery_id, status))


@router.message(StatusPriceFlow.price, F.text, ~F.text.startswith("/"))
async def status_price_entered(msg: Message, state: FSMContext, config: DeliveryTrackerConfig, bot: Bot):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_admin_menu())
        return
    price = _valid_price(msg.text)
    if price is None:
        await msg.answer("Введите целое число от 0 до 1000000000, например: 350", reply_markup=kb_flow_cancel())
        return
    delivery_id = data.get("price_delivery_id")
    old_status = data.get("price_old_status")
    if not delivery_id or not old_status:
        await state.clear()
        await msg.answer("Сессия устарела, начните заново.", reply_markup=kb_admin_menu())
        return
    await state.clear()

    applied, note = await _apply_status_change(config, bot, delivery_id, old_status, "accepted", price=price)
    res = await _admin_delivery_detail(
        config.db_path, delivery_id,
        extra_note=note if applied else "⚠️ Доставка уже была изменена — попробуйте снова.",
    )
    if res is None:
        await msg.answer("Доставка не найдена.", reply_markup=kb_admin_menu())
        return
    text, status = res
    await msg.answer(text, parse_mode="HTML", reply_markup=kb_delivery_detail_admin(delivery_id, status))


# ── ADMIN: courier note ───────────────────────────────────────────────────────

@router.callback_query(F.data == "adlv_note")
async def cb_adlv_note_menu(cb: CallbackQuery, config: DeliveryTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    async with aiosqlite.connect(config.db_path) as db:
        rows = await (await db.execute(
            "SELECT id, status, item_description FROM deliveries ORDER BY id DESC LIMIT ?", (MAX_LIST_BUTTONS,)
        )).fetchall()
    if not rows:
        await cb.message.edit_text("Доставок пока нет.", reply_markup=kb_admin_menu())
        return
    await cb.message.edit_text("Выберите доставку для заметки:", reply_markup=kb_delivery_list(rows, "note_pick", "main_menu"))


@router.callback_query(F.data.startswith("note_pick:"))
async def cb_note_pick(cb: CallbackQuery, state: FSMContext, config: DeliveryTrackerConfig):
    """Reached either from the "💬 Заметка курьера" list, OR as a shortcut
    button directly on a delivery's own admin detail card — same
    callback_data shape either way, so one handler covers both entry points."""
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        delivery_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute("SELECT id FROM deliveries WHERE id=?", (delivery_id,))).fetchone()
    if not row:
        await cb.message.edit_text("Доставка не найдена.", reply_markup=kb_admin_menu())
        return
    await state.set_state(NoteFlow.text)
    await state.update_data(started_at=time.time(), note_delivery_id=delivery_id)
    await cb.message.edit_text(
        "💬 Введите текст заметки (видна только администраторам, клиенту не показывается):",
        reply_markup=kb_flow_cancel(),
    )


@router.message(NoteFlow.text, F.text, ~F.text.startswith("/"))
async def note_text_entered(msg: Message, state: FSMContext, config: DeliveryTrackerConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_admin_menu())
        return
    note = msg.text.strip()
    if not note:
        await msg.answer("Заметка не может быть пустой. Введите текст заметки:", reply_markup=kb_flow_cancel())
        return
    if len(note) > MAX_NOTE_LEN:
        await msg.answer(f"⚠️ Слишком длинная заметка. Уложитесь в {MAX_NOTE_LEN} символов:", reply_markup=kb_flow_cancel())
        return
    delivery_id = data.get("note_delivery_id")
    await state.clear()
    if not delivery_id:
        await msg.answer("Сессия устарела, начните заново.", reply_markup=kb_admin_menu())
        return

    async with aiosqlite.connect(config.db_path) as db:
        cur = await db.execute(
            "UPDATE deliveries SET courier_note=?, updated_at=datetime('now','localtime') WHERE id=?",
            (note, delivery_id),
        )
        await db.commit()
    if cur.rowcount == 0:
        await msg.answer("Доставка не найдена (возможно, удалена).", reply_markup=kb_admin_menu())
        return

    res = await _admin_delivery_detail(config.db_path, delivery_id, extra_note="✅ Заметка сохранена.")
    if res is None:
        await msg.answer("Доставка не найдена.", reply_markup=kb_admin_menu())
        return
    text, status = res
    await msg.answer(text, parse_mode="HTML", reply_markup=kb_delivery_detail_admin(delivery_id, status))


# ── ADMINS menu ────────────────────────────────────────────────────────────────
# Same shape as every other template's admins.json-backed pattern.

async def _admins_list_text(config: DeliveryTrackerConfig) -> str:
    ids = sorted(_load_admins(config.admins_file))
    if not ids:
        return "👥 Пусто"
    return _join_bounded(["👥 <b>Администраторы бота:</b>\n"] + [f"• <code>{_esc(i)}</code>" for i in ids])


@router.callback_query(F.data == "adm_menu")
async def cb_adm_menu(cb: CallbackQuery, state: FSMContext, config: DeliveryTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    text = await _admins_list_text(config)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_admins_menu())


@router.callback_query(F.data == "adm_add")
async def cb_adm_add(cb: CallbackQuery, state: FSMContext, config: DeliveryTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.set_state(AdminMgmtFlow.add_admin)
    await state.update_data(started_at=time.time())
    await cb.message.edit_text("Введите Telegram ID нового администратора:", reply_markup=kb_flow_cancel())


@router.message(AdminMgmtFlow.add_admin, F.text, ~F.text.startswith("/"))
async def admin_add_id(msg: Message, state: FSMContext, config: DeliveryTrackerConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_admin_menu())
        return
    text = msg.text.strip()
    if not _valid_admin_id(text):
        await msg.answer("Некорректный ID. Введите числовой Telegram ID.", reply_markup=kb_flow_cancel())
        return
    await state.clear()
    ids = _load_admins(config.admins_file)
    ids.add(text)
    _save_admins(config.admins_file, ids)
    await msg.answer(f"✅ <code>{text}</code> добавлен.", parse_mode="HTML", reply_markup=kb_admins_menu())


@router.callback_query(F.data == "adm_remove")
async def cb_adm_remove(cb: CallbackQuery, state: FSMContext, config: DeliveryTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    ids = sorted(_load_admins(config.admins_file))
    if len(ids) <= 1:
        await cb.message.edit_text("Нельзя удалить последнего администратора.", reply_markup=kb_admins_menu())
        return
    if len(ids) > MAX_ADMIN_REMOVE_BUTTONS:
        await cb.message.edit_text(
            "Слишком много админов для списка кнопок. Обратитесь к разработчику.", reply_markup=kb_admins_menu()
        )
        return
    await state.set_state(AdminMgmtFlow.remove_admin_pick)
    await state.update_data(started_at=time.time(), remove_admin_ids=ids)
    await cb.message.edit_text("Выберите администратора для удаления:", reply_markup=kb_remove_admins(ids))


@router.callback_query(AdminMgmtFlow.remove_admin_pick, F.data.startswith("adm_rm:"))
async def cb_adm_remove_pick(cb: CallbackQuery, state: FSMContext, config: DeliveryTrackerConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_admin_menu())
        return
    try:
        idx = int(cb.data.split(":", 1)[1])
        target = data["remove_admin_ids"][idx]
    except (ValueError, IndexError, KeyError):
        await state.clear()
        await cb.message.edit_text("Некорректный выбор.", reply_markup=kb_admins_menu())
        return
    ids = _load_admins(config.admins_file)
    if len(ids) <= 1:
        await state.clear()
        await cb.message.edit_text("Нельзя удалить последнего администратора.", reply_markup=kb_admins_menu())
        return
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
