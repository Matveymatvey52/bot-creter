# TEMPLATE: property_rental
# USE FOR: управляющий недвижимостью, сдающий квартиры/офисы в аренду: каталог объектов, заявки на просмотр от потенциальных арендаторов, договоры аренды, ежемесячные платежи с отслеживанием просрочек, заявки на ремонт от текущих арендаторов
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
from datetime import date, datetime, timedelta
from pathlib import Path

import aiosqlite
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

from db.database import add_bot_admin, remove_bot_admin
from features.cashflow_ledger import init_cashflow_tables, record_entry

# BASE_URL/PORT are process-wide, same treatment as templates/car_rental.py's
# own comment — every bot in the shared webhook process reads the same
# values, so these stay module-level env reads rather than PropertyRentalConfig
# fields.
PORT           = int(os.getenv("PORT", "8080"))
RAILWAY_DOMAIN = os.getenv("RAILWAY_PUBLIC_DOMAIN", "")
BASE_URL       = f"https://{RAILWAY_DOMAIN}" if RAILWAY_DOMAIN else f"http://localhost:{PORT}"

# ── CUSTOMIZE ────────────────────────────────────────────────────────────────
BOT_DESCRIPTION = (
    "Управление арендой недвижимости: каталог квартир/офисов, заявки на "
    "просмотр, договоры аренды, ежемесячные платежи с напоминаниями и учётом "
    "просрочек, заявки на ремонт от арендаторов."
)
OWNER_WELCOME_TEXT = (
    "🏢 <b>Управление недвижимостью</b>\n\n"
    "Объекты, заявки на просмотр, договоры аренды, платежи и заявки на "
    "ремонт — всё в одном месте.\n\nВыберите действие:"
)
TENANT_WELCOME_TEXT = (
    "👋 Здравствуйте!\n\n"
    "Здесь вы можете посмотреть свой договор аренды, историю платежей и "
    "оставить заявку на ремонт."
)
PUBLIC_WELCOME_TEXT = (
    "👋 Здравствуйте!\n\n"
    "Здесь можно посмотреть свободные объекты и оставить заявку на просмотр."
)

# За сколько дней до due_date напоминать арендатору об оплате (см.
# reminders_config ниже, offsets_hours = [REMINDER_DAYS_BEFORE_RENT * 24]).
REMINDER_DAYS_BEFORE_RENT = 3

# Пеня за просрочку платежа, в процентах от суммы — используется только для
# ОТОБРАЖЕНИЯ (см. _amount_with_late_fee), не меняет сохранённую сумму
# платежа: rent_payments.amount остаётся суммой по договору, пеня
# показывается отдельно в карточке просроченного платежа/аналитике.
LATE_FEE_PERCENT = 5

# Как часто фоновый цикл проверяет, не просрочен ли ожидающий платёж
# (status='pending' AND due_date < сегодня) — тот же паттерн фонового
# sweep-цикла, что и templates/team_manager.py's _overdue_sweep_loop.
OVERDUE_SWEEP_INTERVAL_SECONDS = 3600
# ── END CUSTOMIZE ─────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
router = Router()

# ── status flows ─────────────────────────────────────────────────────────────
# properties.status is NOT a forward-only flow (a unit can go back to
# "available" after "renovation", or after a lease ends) — the owner picks a
# target status directly from a small fixed set, no STATUS_TRANSITIONS graph
# needed for this table.
PROPERTY_STATUS_LABELS = {
    "available": "🟢 Свободен",
    "occupied": "🔴 Сдан",
    "renovation": "🛠 На ремонте",
}

# viewing_requests: forward-only, "closed" reachable directly from either state.
VR_STATUS_TRANSITIONS = {"new": ["contacted", "closed"], "contacted": ["closed"], "closed": []}
VR_STATUS_LABELS = {"new": "🆕 Новая", "contacted": "☎️ Связались", "closed": "✅ Закрыта"}

# leases: forward-only, both end states are terminal.
LEASE_STATUS_TRANSITIONS = {"active": ["ended", "terminated"], "ended": [], "terminated": []}
LEASE_STATUS_LABELS = {"active": "🟢 Активен", "ended": "⏹ Завершён", "terminated": "❌ Расторгнут"}

# rent_payments: pending -> paid or overdue; overdue -> paid; paid is terminal.
# "overdue" is normally set by _sweep_overdue_payments below, but the owner
# can also flip it manually (per the design brief: "mark payment ... late").
PAY_STATUS_TRANSITIONS = {"pending": ["paid", "overdue"], "overdue": ["paid"], "paid": []}
PAY_STATUS_LABELS = {"pending": "🕐 Ожидает", "paid": "✅ Оплачен", "overdue": "🔴 Просрочен"}

# maintenance_requests: forward-only, same "новая/в работе/закрыта" shape as
# templates/repair_tracker.py's repair_tickets.
MAINT_STATUS_TRANSITIONS = {"new": ["in_progress"], "in_progress": ["closed"], "closed": []}
MAINT_STATUS_LABELS = {"new": "🆕 Новая", "in_progress": "⚙️ В работе", "closed": "✅ Закрыта"}


# ── config ───────────────────────────────────────────────────────────────────
# Same pattern as every other template — see docs/STAGE2_DESIGN.md.

@dataclass
class PropertyRentalConfig:
    bot_name: str
    db_path: str
    admins_file: Path
    welcome_image: Path
    display_name: str | None = None
    group_chat_id: str | None = None
    # Only set in webhook mode (config_from_bot_row) — used by _miniapp_url()
    # to scope a magic-link token to this bot, same as templates/car_rental.py.
    bot_id: int | None = None
    owner_telegram_id: int | None = None


def _paths_for(name: str, data_dir: Path) -> PropertyRentalConfig:
    return PropertyRentalConfig(
        bot_name=name,
        db_path=str(data_dir / f"{name}_data.db"),
        admins_file=data_dir / f"admins_{name}.json",
        welcome_image=data_dir / "bot_images" / f"{name}.jpg",
    )


def config_from_env() -> PropertyRentalConfig:
    """Standalone/subprocess mode."""
    name = Path(__file__).stem
    data_dir = Path(os.getenv("DATA_DIR", "./data"))
    data_dir.mkdir(exist_ok=True)
    return _paths_for(name, data_dir)


def config_from_bot_row(bot_row: dict, data_dir: Path) -> PropertyRentalConfig:
    """Webhook runtime mode. Paths built from bot_row["bot_id"] (bots.id, the
    physically unique AUTOINCREMENT PK) — NOT bot_row["name"] — same reasoning
    as every other template's config_from_bot_row (see docs/STAGE2_DESIGN.md
    "Изоляция по bots.id")."""
    bot_id = bot_row["bot_id"]
    config = PropertyRentalConfig(
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
    """Injects this bot's PropertyRentalConfig into data["config"]."""

    def __init__(self, config: PropertyRentalConfig) -> None:
        self.config = config
        super().__init__()

    async def __call__(self, handler, event, data):
        data["config"] = self.config
        return await handler(event, data)


# ── admin helpers ─────────────────────────────────────────────────────────────
# admins.json remains the SOURCE OF TRUTH for gating owner-only bot commands
# (same convention as every other template) — property_access (see init_db
# below) is a SEPARATE table that exists only so the mini-app's role_filter
# has a single resolve table/column pair to query, per the frozen contract in
# docs/MINIAPP_ROLE_SCOPING_DESIGN.md ("SELECT {role_column} FROM
# {resolve.table} WHERE {identity_column} = :telegram_user_id" — one table,
# not a UNION view). The two are kept in sync explicitly: every admins.json
# write below (_save_admins call sites) is paired with a property_access
# 'owner' row insert; every new active lease's tenant_user_id is paired with
# a property_access 'tenant' row insert (see _grant_tenant_access).

def _load_admins(admins_file: Path) -> set:
    try:
        return set(json.loads(admins_file.read_text()).get("ids", []))
    except Exception:
        return set()

def _save_admins(admins_file: Path, ids: set) -> None:
    admins_file.write_text(json.dumps({"ids": list(ids)}, ensure_ascii=False))

def _is_admin(user_id: int, config: PropertyRentalConfig) -> bool:
    # The DB-known owner (bots.owner_telegram_id) is always an admin, even if
    # the local admins_file is empty/stale/hijacked — see cmd_start below for
    # why the file alone can't be trusted as the sole source of truth.
    if config.owner_telegram_id is not None and str(user_id) == str(config.owner_telegram_id):
        return True
    return str(user_id) in _load_admins(config.admins_file)


async def _grant_owner_access(db_path: str, user_id: int) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT OR IGNORE INTO property_access (user_id, role) VALUES (?, 'owner')", (user_id,),
        )
        await db.commit()


async def _grant_tenant_access(db_path: str, user_id: int) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT OR IGNORE INTO property_access (user_id, role) VALUES (?, 'tenant')", (user_id,),
        )
        await db.commit()


async def _active_lease_for(db_path: str, tenant_user_id: int) -> dict | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            "SELECT * FROM leases WHERE tenant_user_id=? AND status='active' ORDER BY id DESC LIMIT 1",
            (tenant_user_id,),
        )).fetchone()
        return dict(row) if row else None


def _esc(value, max_len: int = 500) -> str:
    """HTML-escapes AND length-bounds any user-supplied text before it goes into
    a parse_mode="HTML" message — same helper/rationale as every other
    template's _esc()."""
    text = str(value) if value is not None else ""
    if len(text) > max_len:
        text = text[:max_len] + "…"
    return html.escape(text)


def _join_bounded(lines: list[str], limit: int = 3500) -> str:
    """Joins lines with a length budget, dropping only WHOLE trailing lines —
    same rationale as templates/repair_tracker.py's _join_bounded()."""
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
# Same RU-phone formula as templates/repair_tracker.py's _normalize_phone().

def _normalize_phone(raw: str) -> str | None:
    digits = re.sub(r"[^\d]", "", raw)
    if len(digits) == 11 and digits[0] in ("7", "8"):
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    else:
        return None
    return f"+{digits[0]} ({digits[1:4]}) {digits[4:7]}-{digits[7:9]}-{digits[9:11]}"


# ── validation helpers ──────────────────────────────────────────────────────
MAX_BOOKING_HORIZON_DAYS = 3650  # ~10 years, same guard as car_rental.py's own

def _parse_date(text: str) -> str | None:
    text = text.strip()
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None
    today = datetime.now().date()
    if parsed > today + timedelta(days=MAX_BOOKING_HORIZON_DAYS):
        return None
    return parsed.isoformat()


_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

def _parse_period(text: str) -> str | None:
    text = text.strip()
    return text if _PERIOD_RE.match(text) else None


def _valid_amount(text: str) -> int | None:
    try:
        amount = int(text.strip())
    except ValueError:
        return None
    if amount < 0 or amount > 1_000_000_000:
        return None
    return amount


def _valid_area(text: str) -> float | None:
    try:
        area = float(text.strip().replace(",", "."))
    except ValueError:
        return None
    if area <= 0 or area > 1_000_000:
        return None
    return area


def _valid_admin_id(text: str) -> bool:
    """Same guard as templates/repair_tracker.py's _valid_admin_id()."""
    if not (bool(text) and text.isascii() and text.isdigit() and len(text) <= 15):
        return False
    return int(text) > 0 and str(int(text)) == text


def _amount_with_late_fee(amount: int) -> int:
    """Display-only: amount owed including LATE_FEE_PERCENT — never written
    back to rent_payments.amount (see CUSTOMIZE block above)."""
    return round(amount * (1 + LATE_FEE_PERCENT / 100))


# ── mini-app config (docs/MINIAPP_ROLE_SCOPING_DESIGN.md — role-resolved
# shape, same contract templates/team_manager.py pioneers) ──────────────────
# DESIGN NOTE — resolve table: the frozen contract's `resolve` step is
# exactly one `SELECT {role_column} FROM {resolve.table} WHERE
# {identity_column} = :telegram_user_id` — a single table/column pair, no
# UNION view support. property_access is that one table; it carries BOTH
# roles this bot recognizes ('owner' seeded from admins.json at bootstrap/
# promotion — see _grant_owner_access; 'tenant' seeded whenever a lease is
# created — see _grant_tenant_access). A viewer can legitimately hold both
# roles at once (an owner renting from themselves is a degenerate but
# harmless case) — resolve() returns the full set, same "membership test,
# not scalar" semantics the design doc describes for team_manager.
#
# DESIGN NOTE — why `where: None` for "owner" is safe HERE (unlike
# team_manager's original bug): this template's "owner" role administers
# THIS ENTIRE bot instance — one property_rental bot is one landlord's own
# portfolio, not a shared instance hosting multiple independent landlords'
# separate businesses the way one team_manager bot hosts multiple unrelated
# teams. There is no per-related-record scoping needed for "owner" because
# there is no second tenant-of-the-bot-itself to leak data to — this matches
# docs/MINIAPP_ROLE_SCOPING_DESIGN.md's own carve-out: "where: None should be
# reserved for roles that generically mean administers this entire bot
# instance." Every co-admin added via /addadmin-equivalent (adm_add below)
# is deliberately granted the SAME unfiltered "owner" view, same as
# car_rental/repair_tracker's admins.json already implies for their
# Telegram-side commands.
_PROPERTY_ACCESS_RESOLVE = {
    "table": "property_access",
    "identity_column": "user_id",
    "role_column": "role",
}

miniapp_config = {
    "resources": [
        {
            "name": "properties",
            "table": "properties",
            "order_by": "id DESC",
            "creatable": True,
            "title": "Объекты",
            "titleField": "address",
            "fields": [
                {"name": "address", "required": True, "label": "Адрес", "kind": "text", "list": True, "detail": True, "create": True},
                {"name": "area", "label": "Площадь, м²", "kind": "number", "list": True, "detail": True, "create": True},
                {"name": "photo", "label": "Фото", "kind": "text", "list": False, "detail": True, "create": False},
                {"name": "rent_price", "label": "Аренда/мес", "kind": "number", "list": True, "detail": True, "create": True},
                {"name": "status", "label": "Статус", "kind": "status", "list": True, "detail": True, "create": False},
                {"name": "created_at", "label": "Создан", "kind": "date", "list": False, "detail": True, "create": False},
            ],
            "role_filter": {
                "resolve": _PROPERTY_ACCESS_RESOLVE,
                "rules": [
                    {"role": "owner", "where": None},
                    {
                        "role": "tenant",
                        "where": "id IN (SELECT property_id FROM leases WHERE tenant_user_id = :telegram_user_id)",
                    },
                ],
                "default_deny": True,
            },
        },
        {
            "name": "leases",
            "table": "leases",
            "order_by": "created_at DESC",
            "creatable": False,
            "title": "Договоры аренды",
            "titleField": "tenant_name",
            "fields": [
                {"name": "property_id", "label": "Объект", "kind": "number", "list": True, "detail": True, "create": False, "ref": {"resource": "properties", "labelField": "address"}},
                {"name": "tenant_user_id", "label": "ID арендатора", "kind": "number", "list": False, "detail": True, "create": False},
                {"name": "tenant_name", "label": "Арендатор", "kind": "text", "list": True, "detail": True, "create": False},
                {"name": "tenant_phone", "label": "Телефон", "kind": "text", "list": False, "detail": True, "create": False},
                {"name": "start_date", "label": "Начало", "kind": "date", "list": True, "detail": True, "create": False},
                {"name": "end_date", "label": "Окончание", "kind": "date", "list": True, "detail": True, "create": False},
                {"name": "monthly_amount", "label": "Аренда/мес", "kind": "number", "list": True, "detail": True, "create": False},
                {"name": "deposit", "label": "Депозит", "kind": "number", "list": False, "detail": True, "create": False},
                {"name": "status", "label": "Статус", "kind": "status", "list": True, "detail": True, "create": False},
            ],
            "role_filter": {
                "resolve": _PROPERTY_ACCESS_RESOLVE,
                "rules": [
                    {"role": "owner", "where": None},
                    {"role": "tenant", "where": "tenant_user_id = :telegram_user_id"},
                ],
                "default_deny": True,
            },
        },
        {
            "name": "rent_payments",
            "table": "rent_payments",
            "order_by": "due_date DESC",
            "creatable": False,
            "title": "Платежи",
            "titleField": "period",
            "fields": [
                {"name": "lease_id", "label": "Договор", "kind": "number", "list": True, "detail": True, "create": False, "ref": {"resource": "leases", "labelField": "tenant_name"}},
                {"name": "period", "label": "Период", "kind": "text", "list": True, "detail": True, "create": False},
                {"name": "amount", "label": "Сумма", "kind": "number", "list": True, "detail": True, "create": False},
                {"name": "due_date", "label": "Срок оплаты", "kind": "date", "list": True, "detail": True, "create": False},
                {"name": "paid_at", "label": "Оплачен", "kind": "date", "list": False, "detail": True, "create": False},
                {"name": "status", "label": "Статус", "kind": "status", "list": True, "detail": True, "create": False},
            ],
            # rent_payments has no tenant_user_id column of its own — scoped
            # through the owning lease, same join-through-parent pattern as
            # templates/team_manager.py's reports/attachments resources.
            "role_filter": {
                "resolve": _PROPERTY_ACCESS_RESOLVE,
                "rules": [
                    {"role": "owner", "where": None},
                    {
                        "role": "tenant",
                        "where": "lease_id IN (SELECT id FROM leases WHERE tenant_user_id = :telegram_user_id)",
                    },
                ],
                "default_deny": True,
            },
        },
        {
            "name": "maintenance_requests",
            "table": "maintenance_requests",
            "order_by": "created_at DESC",
            "creatable": True,
            "title": "Заявки на ремонт",
            "titleField": "description",
            "fields": [
                {"name": "lease_id", "label": "Договор", "kind": "number", "list": False, "detail": True, "create": True, "ref": {"resource": "leases", "labelField": "tenant_name"}},
                {"name": "property_id", "label": "Объект", "kind": "number", "list": True, "detail": True, "create": True, "ref": {"resource": "properties", "labelField": "address"}},
                {"name": "tenant_user_id", "label": "ID арендатора", "kind": "number", "list": False, "detail": True, "create": False},
                {"name": "description", "required": True, "label": "Описание", "kind": "text", "list": True, "detail": True, "create": True},
                {"name": "photo", "label": "Фото", "kind": "text", "list": False, "detail": True, "create": False},
                {"name": "status", "label": "Статус", "kind": "status", "list": True, "detail": True, "create": False},
                {"name": "created_at", "label": "Создана", "kind": "date", "list": False, "detail": True, "create": False},
            ],
            # tenant_user_id lives DIRECTLY on this table (unlike
            # rent_payments) specifically so create_resource_handler's
            # minimal single-column-equality enforcement (docs/
            # MINIAPP_ROLE_SCOPING_DESIGN.md §create_resource_handler) can
            # force it to the authenticated viewer on tenant-submitted
            # maintenance requests from the mini-app.
            "role_filter": {
                "resolve": _PROPERTY_ACCESS_RESOLVE,
                "rules": [
                    {"role": "owner", "where": None},
                    {"role": "tenant", "where": "tenant_user_id = :telegram_user_id"},
                ],
                "default_deny": True,
            },
        },
        # viewing_requests deliberately has NO role_filter: prospective
        # tenants aren't authenticated bot users with a role yet (they may
        # never message the bot again after submitting one request), so this
        # resource stays behind the plain admin-only gate (_admin_gate_ok
        # requires admins.json membership when role_filter is absent) —
        # exactly the "prospects aren't authenticated bot users" case docs/
        # MINIAPP_ROLE_SCOPING_DESIGN.md's admin-gate section describes.
    ],
}


# ── reminders (features/reminders.py) ───────────────────────────────────────
# recipient_query, not recipient_field: rent_payments has no tenant_user_id
# column of its own (see role_filter comment above) — the recipient is found
# by joining through the owning lease, exactly the join-through-parent shape
# features/reminders.py's own docstring describes for recipient_query. The
# date-bearing row's own id (a rent_payments.id) is bound to the query's `?`.
reminders_config = {
    "rules": [
        {
            "id": "property_rental_rent_due",
            "table": "rent_payments",
            "date_field": "due_date",
            "date_format": "%Y-%m-%d",
            "recipient_query": (
                "SELECT l.tenant_user_id AS chat_id FROM leases l "
                "JOIN rent_payments rp ON rp.lease_id = l.id WHERE rp.id = ?"
            ),
            "active_field": "status = 'pending'",
            "offsets_hours": [REMINDER_DAYS_BEFORE_RENT * 24],
            "message_template": (
                "🔔 Напоминание: оплата аренды за {period} на сумму {amount} ₽ "
                "должна быть внесена до {due_date:%d.%m.%Y}."
            ),
        },
    ],
}


# ── db ────────────────────────────────────────────────────────────────────────

async def init_db(db_path: str):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS properties (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                address        TEXT NOT NULL,
                area           REAL,
                photo          TEXT,
                rent_price     INTEGER,
                status         TEXT NOT NULL DEFAULT 'available'
                               CHECK(status IN ('available','occupied','renovation')),
                created_at     TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS viewing_requests (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                property_id        INTEGER NOT NULL,
                requester_user_id  INTEGER NOT NULL,
                requester_name     TEXT,
                requester_phone    TEXT,
                status             TEXT NOT NULL DEFAULT 'new'
                                   CHECK(status IN ('new','contacted','closed')),
                created_at         TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS leases (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                property_id      INTEGER NOT NULL,
                tenant_user_id   INTEGER NOT NULL,
                tenant_name      TEXT,
                tenant_phone     TEXT,
                start_date       TEXT NOT NULL,
                end_date         TEXT NOT NULL,
                monthly_amount   INTEGER NOT NULL,
                deposit          INTEGER,
                status           TEXT NOT NULL DEFAULT 'active'
                                 CHECK(status IN ('active','ended','terminated')),
                created_at       TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS rent_payments (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                lease_id    INTEGER NOT NULL,
                period      TEXT NOT NULL,
                amount      INTEGER NOT NULL,
                due_date    TEXT NOT NULL,
                paid_at     TEXT,
                status      TEXT NOT NULL DEFAULT 'pending'
                            CHECK(status IN ('pending','paid','overdue')),
                created_at  TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS maintenance_requests (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                lease_id        INTEGER NOT NULL,
                property_id     INTEGER NOT NULL,
                tenant_user_id  INTEGER NOT NULL,
                description     TEXT NOT NULL,
                photo           TEXT,
                status          TEXT NOT NULL DEFAULT 'new'
                                CHECK(status IN ('new','in_progress','closed')),
                created_at      TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        # See the admin-helpers comment block above for why this table
        # exists and how it's kept in sync with admins.json/leases.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS property_access (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id  INTEGER NOT NULL,
                role     TEXT NOT NULL CHECK(role IN ('owner','tenant')),
                UNIQUE(user_id, role)
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_vr_property ON viewing_requests(property_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_leases_property ON leases(property_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_leases_tenant ON leases(tenant_user_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_pay_lease ON rent_payments(lease_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_pay_status ON rent_payments(status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_maint_lease ON maintenance_requests(lease_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_access_user ON property_access(user_id)")
        await db.commit()
    # features/cashflow_ledger.py's own table, in this template's db_path —
    # see the module docstring: "no separate db per feature".
    await init_cashflow_tables(db_path)


# ── overdue sweep (pending rent_payments past due_date -> overdue) ──────────
# Pure function, directly testable without the asyncio loop below — same
# split as templates/team_manager.py's _sweep_overdue()/_overdue_sweep_loop().

async def _sweep_overdue_payments(db_path: str, today: str | None = None) -> list[dict]:
    today = today or date.today().isoformat()
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT * FROM rent_payments WHERE status='pending' AND due_date < ?", (today,),
        )).fetchall()
        rows = [dict(r) for r in rows]
        if rows:
            await db.executemany(
                "UPDATE rent_payments SET status='overdue' WHERE id=? AND status='pending'",
                [(r["id"],) for r in rows],
            )
            await db.commit()
    return rows


async def _overdue_sweep_loop(bot: Bot, db_path: str) -> None:
    while True:
        try:
            overdue = await _sweep_overdue_payments(db_path)
            for payment in overdue:
                lease = await _get_lease(db_path, payment["lease_id"])
                if not lease:
                    continue
                due_amount = _amount_with_late_fee(payment["amount"])
                try:
                    await bot.send_message(
                        lease["tenant_user_id"],
                        f"🔴 Платёж за {payment['period']} просрочен. "
                        f"К оплате с учётом пени: {due_amount} ₽.",
                    )
                except TelegramAPIError as e:
                    logger.warning(f"property_rental: failed to notify tenant of overdue payment {payment['id']}: {e}")
        except Exception:
            logger.exception("property_rental: overdue sweep failed")
        await asyncio.sleep(OVERDUE_SWEEP_INTERVAL_SECONDS)


# ── db helpers ───────────────────────────────────────────────────────────────

async def _get_property(db_path: str, property_id: int) -> dict | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute("SELECT * FROM properties WHERE id=?", (property_id,))).fetchone()
        return dict(row) if row else None


async def _get_lease(db_path: str, lease_id: int) -> dict | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute("SELECT * FROM leases WHERE id=?", (lease_id,))).fetchone()
        return dict(row) if row else None


async def _get_payment(db_path: str, payment_id: int) -> dict | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute("SELECT * FROM rent_payments WHERE id=?", (payment_id,))).fetchone()
        return dict(row) if row else None


async def _get_maintenance(db_path: str, request_id: int) -> dict | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute("SELECT * FROM maintenance_requests WHERE id=?", (request_id,))).fetchone()
        return dict(row) if row else None


async def _create_lease(
    db_path: str, property_id: int, tenant_user_id: int, tenant_name: str | None, tenant_phone: str | None,
    start_date: str, end_date: str, monthly_amount: int, deposit: int | None,
) -> dict:
    """Creates the lease, flips the property to 'occupied', grants
    property_access tenant role, and auto-creates the FIRST month's
    rent_payment (period = start_date's YYYY-MM, due on start_date) —
    subsequent months are added by the owner via _create_payment below."""
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "INSERT INTO leases (property_id, tenant_user_id, tenant_name, tenant_phone, start_date, end_date, "
            "monthly_amount, deposit) VALUES (?,?,?,?,?,?,?,?)",
            (property_id, tenant_user_id, tenant_name, tenant_phone, start_date, end_date, monthly_amount, deposit),
        )
        lease_id = cur.lastrowid
        await db.execute("UPDATE properties SET status='occupied' WHERE id=?", (property_id,))
        first_period = start_date[:7]
        await db.execute(
            "INSERT INTO rent_payments (lease_id, period, amount, due_date) VALUES (?,?,?,?)",
            (lease_id, first_period, monthly_amount, start_date),
        )
        await db.commit()
    await _grant_tenant_access(db_path, tenant_user_id)
    return await _get_lease(db_path, lease_id)


async def _create_payment(db_path: str, lease_id: int, period: str, due_date: str, amount: int) -> int:
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "INSERT INTO rent_payments (lease_id, period, amount, due_date) VALUES (?,?,?,?)",
            (lease_id, period, amount, due_date),
        )
        await db.commit()
        return cur.lastrowid


async def _mark_payment_status(db_path: str, payment_id: int, old_status: str, new_status: str) -> bool:
    """Compare-and-swap, same double-tap-safety principle as every other
    template's status transitions. On 'paid', also stamps paid_at and
    records an income entry in the cashflow ledger (features/cashflow_ledger.py),
    parent_id=lease_id — a bot-wide view can later sum across every lease's
    entries the same way tour_operator sums per-tour entries."""
    async with aiosqlite.connect(db_path) as db:
        if new_status == "paid":
            cur = await db.execute(
                "UPDATE rent_payments SET status=?, paid_at=datetime('now','localtime') WHERE id=? AND status=?",
                (new_status, payment_id, old_status),
            )
        else:
            cur = await db.execute(
                "UPDATE rent_payments SET status=? WHERE id=? AND status=?",
                (new_status, payment_id, old_status),
            )
        await db.commit()
        applied = cur.rowcount > 0
    if applied and new_status == "paid":
        payment = await _get_payment(db_path, payment_id)
        lease = await _get_lease(db_path, payment["lease_id"]) if payment else None
        if payment and lease:
            await record_entry(
                db_path, parent_id=lease["id"], date=payment["paid_at"] or date.today().isoformat(),
                amount_rub=payment["amount"], description=f"Аренда за {payment['period']}",
                entity=lease.get("tenant_name"), type="in",
            )
    return applied


# ── FSM staleness guard ─────────────────────────────────────────────────────────
FLOW_TIMEOUT_SECONDS = 300

def _flow_expired(data: dict) -> bool:
    started_at = data.get("started_at")
    return started_at is None or (time.time() - started_at) > FLOW_TIMEOUT_SECONDS


# ── FSM states ───────────────────────────────────────────────────────────────

class PropertyFlow(StatesGroup):
    """Owner-side: add a new property (address -> area[skip] -> price[skip] -> photo[skip])."""
    address = State()
    area = State()
    price = State()
    photo = State()

class LeaseFlow(StatesGroup):
    """Owner-side: create a lease for a chosen available property."""
    tenant_id = State()
    tenant_name = State()
    tenant_phone = State()
    start_date = State()
    end_date = State()
    amount = State()
    deposit = State()

class PaymentFlow(StatesGroup):
    """Owner-side: manually accrue a subsequent month's rent_payment."""
    period = State()
    due_date = State()
    amount = State()

class ViewingRequestFlow(StatesGroup):
    """Public/prospect-side: request a viewing for a chosen property."""
    name = State()
    phone = State()

class MaintenanceFlow(StatesGroup):
    """Tenant-side: submit a maintenance request for their own active lease."""
    description = State()
    photo = State()

class AdminMgmtFlow(StatesGroup):
    add_admin = State()
    remove_admin_pick = State()


MAX_LIST_BUTTONS = 25
MAX_ADMIN_REMOVE_BUTTONS = 30
MAX_ADDRESS_LEN = 300
MAX_DESCRIPTION_LEN = 1000
LEASE_EXPIRING_WITHIN_DAYS = 30  # CUSTOMIZE: "аналитика" expiring-soon window


# ── keyboards ─────────────────────────────────────────────────────────────────

def kb_back(callback_data: str = "main_menu") -> InlineKeyboardButton:
    return InlineKeyboardButton(text="◀️ Назад", callback_data=callback_data)

def kb_back_markup(callback_data: str = "main_menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[kb_back(callback_data)]])

def kb_flow_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="flow_cancel")]])

def kb_skip_and_cancel(skip_cb: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭ Пропустить", callback_data=skip_cb)],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="flow_cancel")],
    ])

# ── owner main menu ──
def kb_owner_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Объекты", callback_data="prop_menu")],
        [InlineKeyboardButton(text="📩 Заявки на просмотр", callback_data="vr_menu")],
        [InlineKeyboardButton(text="📄 Договоры аренды", callback_data="lse_menu")],
        [InlineKeyboardButton(text="🔧 Заявки на ремонт", callback_data="mnt_menu")],
        [InlineKeyboardButton(text="📊 Аналитика", callback_data="an_view")],
        [InlineKeyboardButton(text="👥 Админы", callback_data="adm_menu")],
        [InlineKeyboardButton(text="🌐 Веб-приложение", callback_data="miniapp_link")],
    ])

# ── tenant menu ──
def kb_tenant_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📄 Моя аренда", callback_data="ten_lease")],
        [InlineKeyboardButton(text="🔧 Заявка на ремонт", callback_data="tmnt_new")],
        [InlineKeyboardButton(text="📋 Мои заявки на ремонт", callback_data="tmnt_mine")],
        [InlineKeyboardButton(text="🏠 Свободные объекты", callback_data="pub_browse")],
    ])

# ── public/prospect menu ──
def kb_public_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Свободные объекты", callback_data="pub_browse")],
    ])


async def _menu_for(user_id: int, config: PropertyRentalConfig) -> tuple[str, InlineKeyboardMarkup]:
    if _is_admin(user_id, config):
        return OWNER_WELCOME_TEXT, kb_owner_menu()
    if await _active_lease_for(config.db_path, user_id):
        return TENANT_WELCOME_TEXT, kb_tenant_menu()
    return PUBLIC_WELCOME_TEXT, kb_public_menu()


# ── owner: properties ──
def kb_properties_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить объект", callback_data="prop_new")],
        [InlineKeyboardButton(text="📋 Список объектов", callback_data="prop_list")],
        [kb_back()],
    ])

def kb_property_list(rows: list[tuple], callback_prefix: str = "prop_view", back_cb: str = "prop_menu") -> InlineKeyboardMarkup:
    btns = [
        [InlineKeyboardButton(
            text=f"{PROPERTY_STATUS_LABELS.get(status, status)} · {_esc(address, 30)}",
            callback_data=f"{callback_prefix}:{pid}",
        )]
        for pid, status, address in rows[:MAX_LIST_BUTTONS]
    ]
    btns.append([kb_back(back_cb)])
    return InlineKeyboardMarkup(inline_keyboard=btns)

def kb_property_detail(property_id: int, status: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"prop_status:{property_id}:{code}")]
        for code, label in PROPERTY_STATUS_LABELS.items() if code != status
    ]
    rows.append([kb_back("prop_list")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── owner: viewing requests ──
_VR_FILTERS = [(code, label) for code, label in VR_STATUS_LABELS.items()] + [("all", "📋 Все")]

def kb_vr_filters() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=label, callback_data=f"vr_filter:{code}")] for code, label in _VR_FILTERS]
    rows.append([kb_back()])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_vr_list(rows: list[tuple], back_cb: str) -> InlineKeyboardMarkup:
    btns = [
        [InlineKeyboardButton(
            text=f"№{vid} · {VR_STATUS_LABELS.get(status, status)} · {_esc(address, 20)}",
            callback_data=f"vr_view:{vid}",
        )]
        for vid, status, address in rows[:MAX_LIST_BUTTONS]
    ]
    btns.append([kb_back(back_cb)])
    return InlineKeyboardMarkup(inline_keyboard=btns)

def kb_vr_detail(vr_id: int, status: str, property_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=VR_STATUS_LABELS[target], callback_data=f"vr_status:{vr_id}:{target}")]
        for target in VR_STATUS_TRANSITIONS.get(status, [])
    ]
    rows.append([InlineKeyboardButton(text="📝 Создать договор из этой заявки", callback_data=f"lse_from_vr:{vr_id}")])
    rows.append([kb_back("vr_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── owner: leases ──
def kb_leases_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Новый договор", callback_data="lse_new")],
        [InlineKeyboardButton(text="📋 Список договоров", callback_data="lse_list")],
        [kb_back()],
    ])

def kb_lease_list(rows: list[tuple], back_cb: str = "lse_menu") -> InlineKeyboardMarkup:
    btns = [
        [InlineKeyboardButton(
            text=f"№{lid} · {LEASE_STATUS_LABELS.get(status, status)} · {_esc(name or '—', 25)}",
            callback_data=f"lse_view:{lid}",
        )]
        for lid, status, name in rows[:MAX_LIST_BUTTONS]
    ]
    btns.append([kb_back(back_cb)])
    return InlineKeyboardMarkup(inline_keyboard=btns)

def kb_lease_detail(lease_id: int, status: str) -> InlineKeyboardMarkup:
    rows = []
    for target in LEASE_STATUS_TRANSITIONS.get(status, []):
        rows.append([InlineKeyboardButton(text=LEASE_STATUS_LABELS[target], callback_data=f"lse_status:{lease_id}:{target}")])
    rows.append([InlineKeyboardButton(text="➕ Начислить платёж", callback_data=f"pay_new:{lease_id}")])
    rows.append([InlineKeyboardButton(text="💰 Платежи по договору", callback_data=f"pay_list:{lease_id}")])
    rows.append([kb_back("lse_list")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_property_pick_for_lease(rows: list[tuple]) -> InlineKeyboardMarkup:
    btns = [
        [InlineKeyboardButton(text=_esc(address, 40), callback_data=f"lse_pick_prop:{pid}")]
        for pid, address in rows[:MAX_LIST_BUTTONS]
    ]
    btns.append([InlineKeyboardButton(text="❌ Отмена", callback_data="flow_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=btns)


# ── owner: payments ──
def kb_payment_list(rows: list[tuple], lease_id: int) -> InlineKeyboardMarkup:
    btns = [
        [InlineKeyboardButton(
            text=f"{PAY_STATUS_LABELS.get(status, status)} · {period} · {amount}₽",
            callback_data=f"pay_view:{pid}",
        )]
        for pid, status, period, amount in rows[:MAX_LIST_BUTTONS]
    ]
    btns.append([kb_back(f"lse_view:{lease_id}")])
    return InlineKeyboardMarkup(inline_keyboard=btns)

def kb_payment_detail(payment_id: int, status: str, lease_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=PAY_STATUS_LABELS[target], callback_data=f"pay_status:{payment_id}:{target}")]
        for target in PAY_STATUS_TRANSITIONS.get(status, [])
    ]
    rows.append([kb_back(f"pay_list:{lease_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── owner: maintenance ──
_MNT_FILTERS = [(code, label) for code, label in MAINT_STATUS_LABELS.items()] + [("all", "📋 Все")]

def kb_mnt_filters() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=label, callback_data=f"mnt_filter:{code}")] for code, label in _MNT_FILTERS]
    rows.append([kb_back()])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_mnt_list(rows: list[tuple], back_cb: str, prefix: str = "mnt_view") -> InlineKeyboardMarkup:
    btns = [
        [InlineKeyboardButton(
            text=f"№{mid} · {MAINT_STATUS_LABELS.get(status, status)} · {_esc(desc, 25)}",
            callback_data=f"{prefix}:{mid}",
        )]
        for mid, status, desc in rows[:MAX_LIST_BUTTONS]
    ]
    btns.append([kb_back(back_cb)])
    return InlineKeyboardMarkup(inline_keyboard=btns)

def kb_mnt_detail(request_id: int, status: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=MAINT_STATUS_LABELS[target], callback_data=f"mnt_status:{request_id}:{target}")]
        for target in MAINT_STATUS_TRANSITIONS.get(status, [])
    ]
    rows.append([kb_back("mnt_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── admins menu (standard shape, every template) ──
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

def _property_text(row: dict) -> str:
    lines = [
        f"🏠 <b>{_esc(row['address'])}</b>\n",
        f"Статус: {PROPERTY_STATUS_LABELS.get(row['status'], row['status'])}",
    ]
    if row["area"] is not None:
        lines.append(f"Площадь: {row['area']} м²")
    if row["rent_price"] is not None:
        lines.append(f"Аренда: {row['rent_price']} ₽/мес")
    return _join_bounded(lines)


def _lease_text(lease: dict, property_row: dict | None) -> str:
    lines = [
        f"📄 <b>Договор №{lease['id']}</b> · {LEASE_STATUS_LABELS.get(lease['status'], lease['status'])}\n",
        f"🏠 {_esc(property_row['address']) if property_row else lease['property_id']}",
        f"👤 {_esc(lease['tenant_name'] or '—')}" + (f" · {_esc(lease['tenant_phone'])}" if lease["tenant_phone"] else ""),
        f"🗓 {lease['start_date']} — {lease['end_date']}",
        f"💰 {lease['monthly_amount']} ₽/мес",
    ]
    if lease["deposit"] is not None:
        lines.append(f"💵 Депозит: {lease['deposit']} ₽")
    return _join_bounded(lines)


def _payment_text(payment: dict) -> str:
    lines = [
        f"💰 <b>Платёж за {payment['period']}</b> · {PAY_STATUS_LABELS.get(payment['status'], payment['status'])}\n",
        f"Сумма: {payment['amount']} ₽",
        f"Срок оплаты: {payment['due_date']}",
    ]
    if payment["status"] == "overdue":
        lines.append(f"С учётом пени ({LATE_FEE_PERCENT}%): {_amount_with_late_fee(payment['amount'])} ₽")
    if payment["paid_at"]:
        lines.append(f"Оплачен: {payment['paid_at']}")
    return _join_bounded(lines)


def _maintenance_text(row: dict) -> str:
    lines = [
        f"🔧 <b>Заявка №{row['id']}</b> · {MAINT_STATUS_LABELS.get(row['status'], row['status'])}\n",
        f"{_esc(row['description'])}",
        f"🕐 Создана: {row['created_at']}",
    ]
    return _join_bounded(lines)


# ── /start ────────────────────────────────────────────────────────────────────

@router.message(Command("start"), F.chat.type == "private")
async def cmd_start(message: Message, state: FSMContext, config: PropertyRentalConfig):
    await state.clear()
    admins = _load_admins(config.admins_file)
    sender_id = message.from_user.id
    # Bug fixed here: this used to grant admin to whoever sent /start FIRST,
    # which lets any client who messages the bot before the owner does
    # permanently seize the admin panel. When bots.owner_telegram_id is known
    # (webhook/production mode), only that user may claim the empty-admins
    # bootstrap slot; a non-owner sending /start first now just gets the
    # public/tenant menu. In standalone/env mode (owner_telegram_id unknown)
    # the old first-comer behavior is kept as the only option available.
    is_owner = config.owner_telegram_id is not None and sender_id == config.owner_telegram_id
    first_time_admin = not admins and (is_owner or config.owner_telegram_id is None)
    if first_time_admin:
        _save_admins(config.admins_file, {str(sender_id)})
        admins = {str(sender_id)}
        await _grant_owner_access(config.db_path, sender_id)
        if config.bot_id is not None:
            try:
                await add_bot_admin(config.bot_id, str(sender_id))
            except Exception as e:
                logger.warning(f"cmd_start: add_bot_admin sync failed for bot {config.bot_id}: {e}")

    text, kb = await _menu_for(sender_id, config)
    if str(sender_id) in admins and config.welcome_image.exists():
        await message.answer_photo(FSInputFile(str(config.welcome_image)), caption=text, parse_mode="HTML", reply_markup=kb)
    else:
        await message.answer(text, parse_mode="HTML", reply_markup=kb)
    if first_time_admin:
        await message.answer(
            "👑 <b>Вы — владелец этого бота.</b>\n\n"
            "Управление другими администраторами — кнопка «👥 Админы» выше.",
            parse_mode="HTML",
        )


def _miniapp_url(bot_id: int, telegram_user_id: int) -> str | None:
    """Same pattern as templates/car_rental.py's own _miniapp_url()."""
    from runtime.miniapp_api import mint_magic_link_token

    try:
        token = mint_magic_link_token(bot_id, telegram_user_id)
    except RuntimeError:
        return None
    return f"{BASE_URL}/app/{bot_id}?token={token}"


@router.message(Command("app"))
async def cmd_app(m: Message, config: PropertyRentalConfig):
    url = _miniapp_url(config.bot_id, m.from_user.id) if config.bot_id is not None else None
    if not url:
        await m.answer("🌐 Веб-приложение временно недоступно. Пользуйтесь кнопками ниже.")
        return
    await m.answer(
        f"🌐 <b>Веб-приложение</b>\n\n<a href=\"{url}\">Открыть →</a>\n\n<code>{url}</code>",
        parse_mode="HTML", disable_web_page_preview=True,
    )


@router.callback_query(F.data == "miniapp_link")
async def cb_miniapp_link(cb: CallbackQuery, config: PropertyRentalConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    url = _miniapp_url(config.bot_id, cb.from_user.id) if config.bot_id is not None else None
    if not url:
        await cb.message.answer("🌐 Веб-приложение временно недоступно. Пользуйтесь кнопками ниже.")
        return
    await cb.message.answer(
        f"🌐 <b>Веб-приложение</b>\n\n<a href=\"{url}\">Открыть →</a>\n\n<code>{url}</code>",
        parse_mode="HTML", disable_web_page_preview=True,
    )


@router.callback_query(F.data == "main_menu")
async def cb_main_menu(cb: CallbackQuery, state: FSMContext, config: PropertyRentalConfig):
    await cb.answer()
    await state.clear()
    text, kb = await _menu_for(cb.from_user.id, config)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "flow_cancel")
async def cb_flow_cancel(cb: CallbackQuery, state: FSMContext, config: PropertyRentalConfig):
    await cb.answer()
    await state.clear()
    _text, kb = await _menu_for(cb.from_user.id, config)
    await cb.message.edit_text("Отменено.", reply_markup=kb)


# ── OWNER: properties CRUD ────────────────────────────────────────────────────

@router.callback_query(F.data == "prop_menu")
async def cb_prop_menu(cb: CallbackQuery, state: FSMContext, config: PropertyRentalConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    await cb.message.edit_text("🏠 <b>Объекты</b>", parse_mode="HTML", reply_markup=kb_properties_menu())


@router.callback_query(F.data == "prop_list")
async def cb_prop_list(cb: CallbackQuery, config: PropertyRentalConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    async with aiosqlite.connect(config.db_path) as db:
        rows = await (await db.execute(
            "SELECT id, status, address FROM properties ORDER BY id DESC LIMIT ?", (MAX_LIST_BUTTONS,),
        )).fetchall()
    if not rows:
        await cb.message.edit_text("Объектов пока нет.", reply_markup=kb_properties_menu())
        return
    await cb.message.edit_text("📋 Объекты:", reply_markup=kb_property_list(rows))


@router.callback_query(F.data.startswith("prop_view:"))
async def cb_prop_view(cb: CallbackQuery, config: PropertyRentalConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        property_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    row = await _get_property(config.db_path, property_id)
    if not row:
        await cb.message.edit_text("Объект не найден.", reply_markup=kb_properties_menu())
        return
    await cb.message.edit_text(_property_text(row), parse_mode="HTML", reply_markup=kb_property_detail(property_id, row["status"]))


@router.callback_query(F.data.startswith("prop_status:"))
async def cb_prop_status(cb: CallbackQuery, config: PropertyRentalConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        _, pid_s, new_status = cb.data.split(":", 2)
        property_id = int(pid_s)
    except ValueError:
        return
    if new_status not in PROPERTY_STATUS_LABELS:
        return
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute("UPDATE properties SET status=? WHERE id=?", (new_status, property_id))
        await db.commit()
    row = await _get_property(config.db_path, property_id)
    if not row:
        await cb.message.edit_text("Объект не найден.", reply_markup=kb_properties_menu())
        return
    await cb.message.edit_text(_property_text(row), parse_mode="HTML", reply_markup=kb_property_detail(property_id, row["status"]))


@router.callback_query(F.data == "prop_new")
async def cb_prop_new(cb: CallbackQuery, state: FSMContext, config: PropertyRentalConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.set_state(PropertyFlow.address)
    await state.update_data(started_at=time.time())
    await cb.message.edit_text("🏠 Введите адрес объекта:", reply_markup=kb_flow_cancel())


@router.message(PropertyFlow.address, F.text, ~F.text.startswith("/"))
async def prop_address(msg: Message, state: FSMContext):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_properties_menu())
        return
    address = msg.text.strip()
    if not address:
        await msg.answer("Адрес не может быть пустым. Введите адрес:", reply_markup=kb_flow_cancel())
        return
    if len(address) > MAX_ADDRESS_LEN:
        await msg.answer(f"⚠️ Слишком длинный адрес. Уложитесь в {MAX_ADDRESS_LEN} символов:", reply_markup=kb_flow_cancel())
        return
    await state.update_data(address=address)
    await state.set_state(PropertyFlow.area)
    await msg.answer("📐 Площадь в м² (или «Пропустить»):", reply_markup=kb_skip_and_cancel("prop_area_skip"))


async def _prop_area_next(answer, state: FSMContext, area: float | None) -> None:
    await state.update_data(area=area)
    await state.set_state(PropertyFlow.price)
    await answer("💰 Цена аренды в месяц (или «Пропустить»):", reply_markup=kb_skip_and_cancel("prop_price_skip"))


@router.message(PropertyFlow.area, F.text, ~F.text.startswith("/"))
async def prop_area(msg: Message, state: FSMContext):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_properties_menu())
        return
    area = _valid_area(msg.text)
    if area is None:
        await msg.answer("Введите число (например 45.5) или нажмите «Пропустить»:", reply_markup=kb_skip_and_cancel("prop_area_skip"))
        return
    await _prop_area_next(msg.answer, state, area)


@router.callback_query(PropertyFlow.area, F.data == "prop_area_skip")
async def cb_prop_area_skip(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_properties_menu())
        return
    await _prop_area_next(cb.message.answer, state, None)


async def _prop_price_next(answer, state: FSMContext, price: int | None) -> None:
    await state.update_data(rent_price=price)
    await state.set_state(PropertyFlow.photo)
    await answer("📷 Пришлите фото объекта (или «Пропустить»):", reply_markup=kb_skip_and_cancel("prop_photo_skip"))


@router.message(PropertyFlow.price, F.text, ~F.text.startswith("/"))
async def prop_price(msg: Message, state: FSMContext):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_properties_menu())
        return
    price = _valid_amount(msg.text)
    if price is None:
        await msg.answer("Введите целое число (например 50000) или нажмите «Пропустить»:", reply_markup=kb_skip_and_cancel("prop_price_skip"))
        return
    await _prop_price_next(msg.answer, state, price)


@router.callback_query(PropertyFlow.price, F.data == "prop_price_skip")
async def cb_prop_price_skip(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_properties_menu())
        return
    await _prop_price_next(cb.message.answer, state, None)


async def _finalize_property(answer, state: FSMContext, config: PropertyRentalConfig, photo_file_id: str | None) -> None:
    data = await state.get_data()
    address = data.get("address")
    if not address:
        await state.clear()
        await answer("Сессия устарела, начните заново.", reply_markup=kb_properties_menu())
        return
    await state.clear()
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute(
            "INSERT INTO properties (address, area, photo, rent_price) VALUES (?,?,?,?)",
            (address, data.get("area"), photo_file_id, data.get("rent_price")),
        )
        await db.commit()
    await answer(f"✅ Объект добавлен: {_esc(address)}", parse_mode="HTML", reply_markup=kb_properties_menu())


@router.message(PropertyFlow.photo, F.photo)
async def prop_photo(msg: Message, state: FSMContext, config: PropertyRentalConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_properties_menu())
        return
    await _finalize_property(msg.answer, state, config, msg.photo[-1].file_id)


@router.callback_query(PropertyFlow.photo, F.data == "prop_photo_skip")
async def cb_prop_photo_skip(cb: CallbackQuery, state: FSMContext, config: PropertyRentalConfig):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_properties_menu())
        return
    await _finalize_property(cb.message.answer, state, config, None)


# ── PUBLIC/TENANT: browse available properties, request a viewing ───────────

@router.callback_query(F.data == "pub_browse")
async def cb_pub_browse(cb: CallbackQuery, state: FSMContext, config: PropertyRentalConfig):
    await cb.answer()
    await state.clear()
    async with aiosqlite.connect(config.db_path) as db:
        rows = await (await db.execute(
            "SELECT id, address, rent_price FROM properties WHERE status='available' ORDER BY id DESC LIMIT ?",
            (MAX_LIST_BUTTONS,),
        )).fetchall()
    if not rows:
        _text, kb = await _menu_for(cb.from_user.id, config)
        await cb.message.edit_text("😔 Свободных объектов сейчас нет.", reply_markup=kb)
        return
    btns = [
        [InlineKeyboardButton(text=f"{_esc(a, 30)}" + (f" · {p}₽/мес" if p is not None else ""), callback_data=f"pub_view:{pid}")]
        for pid, a, p in rows[:MAX_LIST_BUTTONS]
    ]
    btns.append([kb_back()])
    await cb.message.edit_text("🏠 Свободные объекты:", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))


@router.callback_query(F.data.startswith("pub_view:"))
async def cb_pub_view(cb: CallbackQuery, config: PropertyRentalConfig):
    await cb.answer()
    try:
        property_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    row = await _get_property(config.db_path, property_id)
    if not row or row["status"] != "available":
        await cb.message.edit_text("Этот объект больше недоступен.", reply_markup=kb_back_markup())
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📩 Запросить просмотр", callback_data=f"pub_vr_new:{property_id}")],
        [kb_back()],
    ])
    await cb.message.edit_text(_property_text(row), parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("pub_vr_new:"))
async def cb_pub_vr_new(cb: CallbackQuery, state: FSMContext, config: PropertyRentalConfig):
    await cb.answer()
    try:
        property_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    row = await _get_property(config.db_path, property_id)
    if not row or row["status"] != "available":
        await cb.message.edit_text("Этот объект больше недоступен.", reply_markup=kb_back_markup())
        return
    await state.set_state(ViewingRequestFlow.name)
    await state.update_data(started_at=time.time(), vr_property_id=property_id)
    await cb.message.edit_text("👤 Как к вам обращаться?", reply_markup=kb_flow_cancel())


@router.message(ViewingRequestFlow.name, F.text, ~F.text.startswith("/"))
async def vr_name(msg: Message, state: FSMContext):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_back_markup())
        return
    name = msg.text.strip()
    if not name:
        await msg.answer("Имя не может быть пустым:", reply_markup=kb_flow_cancel())
        return
    await state.update_data(requester_name=name[:200])
    await state.set_state(ViewingRequestFlow.phone)
    await msg.answer("📱 Номер телефона для связи (или «Пропустить»):", reply_markup=kb_skip_and_cancel("vr_phone_skip"))


async def _finalize_viewing_request(answer, state: FSMContext, config: PropertyRentalConfig, bot: Bot, from_user, phone: str | None) -> None:
    data = await state.get_data()
    property_id = data.get("vr_property_id")
    name = data.get("requester_name")
    if not property_id or not name:
        await state.clear()
        await answer("Сессия устарела, начните заново.", reply_markup=kb_back_markup())
        return
    await state.clear()
    async with aiosqlite.connect(config.db_path) as db:
        cur = await db.execute(
            "INSERT INTO viewing_requests (property_id, requester_user_id, requester_name, requester_phone) "
            "VALUES (?,?,?,?)",
            (property_id, from_user.id, name, phone),
        )
        vr_id = cur.lastrowid
        await db.commit()
    await answer("✅ Заявка на просмотр отправлена! Мы свяжемся с вами.", reply_markup=kb_back_markup())

    row = await _get_property(config.db_path, property_id)
    admins = _load_admins(config.admins_file)
    notify_text = (
        f"📩 Новая заявка на просмотр №{vr_id}\n"
        f"🏠 {_esc(row['address']) if row else property_id}\n"
        f"👤 {_esc(name)}" + (f" · {_esc(phone)}" if phone else "")
    )
    for admin_id in admins:
        try:
            await bot.send_message(int(admin_id), notify_text)
        except (TelegramAPIError, ValueError) as e:
            logger.warning(f"property_rental: failed to notify admin {admin_id} of viewing request {vr_id}: {e}")


@router.message(ViewingRequestFlow.phone, F.text, ~F.text.startswith("/"))
async def vr_phone(msg: Message, state: FSMContext, config: PropertyRentalConfig, bot: Bot):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_back_markup())
        return
    phone = _normalize_phone(msg.text.strip())
    if phone is None:
        await msg.answer(
            "❌ Не удалось распознать номер. Введите номер, например +7 999 123-45-67, или нажмите «Пропустить»:",
            reply_markup=kb_skip_and_cancel("vr_phone_skip"),
        )
        return
    await _finalize_viewing_request(msg.answer, state, config, bot, msg.from_user, phone)


@router.callback_query(ViewingRequestFlow.phone, F.data == "vr_phone_skip")
async def cb_vr_phone_skip(cb: CallbackQuery, state: FSMContext, config: PropertyRentalConfig, bot: Bot):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_back_markup())
        return
    await _finalize_viewing_request(cb.message.answer, state, config, bot, cb.from_user, None)


# ── OWNER: viewing requests review ───────────────────────────────────────────

@router.callback_query(F.data == "vr_menu")
async def cb_vr_menu(cb: CallbackQuery, state: FSMContext, config: PropertyRentalConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    await cb.message.edit_text("Выберите фильтр по статусу:", reply_markup=kb_vr_filters())


@router.callback_query(F.data.startswith("vr_filter:"))
async def cb_vr_filter(cb: CallbackQuery, config: PropertyRentalConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    status = cb.data.split(":", 1)[1]
    base_sql = "SELECT v.id, v.status, p.address FROM viewing_requests v JOIN properties p ON p.id = v.property_id "
    async with aiosqlite.connect(config.db_path) as db:
        if status == "all":
            rows = await (await db.execute(base_sql + "ORDER BY v.id DESC LIMIT ?", (MAX_LIST_BUTTONS,))).fetchall()
        else:
            rows = await (await db.execute(base_sql + "WHERE v.status=? ORDER BY v.id DESC LIMIT ?", (status, MAX_LIST_BUTTONS))).fetchall()
    if not rows:
        await cb.message.edit_text("Заявок не найдено.", reply_markup=kb_vr_filters())
        return
    await cb.message.edit_text(f"📋 Заявки на просмотр ({len(rows)}):", reply_markup=kb_vr_list(rows, "vr_menu"))


@router.callback_query(F.data.startswith("vr_view:"))
async def cb_vr_view(cb: CallbackQuery, config: PropertyRentalConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        vr_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            "SELECT v.*, p.address FROM viewing_requests v JOIN properties p ON p.id = v.property_id WHERE v.id=?",
            (vr_id,),
        )).fetchone()
    if not row:
        await cb.message.edit_text("Заявка не найдена.", reply_markup=kb_vr_filters())
        return
    lines = [
        f"📩 <b>Заявка №{row['id']}</b> · {VR_STATUS_LABELS.get(row['status'], row['status'])}\n",
        f"🏠 {_esc(row['address'])}",
        f"👤 {_esc(row['requester_name'] or '—')}" + (f" · {_esc(row['requester_phone'])}" if row["requester_phone"] else ""),
        f"🕐 {row['created_at']}",
    ]
    await cb.message.edit_text(
        _join_bounded(lines), parse_mode="HTML", reply_markup=kb_vr_detail(vr_id, row["status"], row["property_id"]),
    )


@router.callback_query(F.data.startswith("vr_status:"))
async def cb_vr_status(cb: CallbackQuery, config: PropertyRentalConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        _, vr_id_s, new_status = cb.data.split(":", 2)
        vr_id = int(vr_id_s)
    except ValueError:
        return
    if new_status not in VR_STATUS_LABELS:
        return
    async with aiosqlite.connect(config.db_path) as db:
        row = await (await db.execute("SELECT status FROM viewing_requests WHERE id=?", (vr_id,))).fetchone()
        if not row:
            await cb.message.edit_text("Заявка не найдена.", reply_markup=kb_vr_filters())
            return
        old_status = row[0]
        if new_status not in VR_STATUS_TRANSITIONS.get(old_status, []):
            await db.commit()
        else:
            await db.execute("UPDATE viewing_requests SET status=? WHERE id=? AND status=?", (new_status, vr_id, old_status))
            await db.commit()
    cb.data = f"vr_view:{vr_id}"
    await cb_vr_view(cb, config)


# ── OWNER: create a lease (from scratch, or from a viewing request) ─────────

@router.callback_query(F.data == "lse_menu")
async def cb_lse_menu(cb: CallbackQuery, state: FSMContext, config: PropertyRentalConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    await cb.message.edit_text("📄 <b>Договоры аренды</b>", parse_mode="HTML", reply_markup=kb_leases_menu())


@router.callback_query(F.data == "lse_new")
async def cb_lse_new(cb: CallbackQuery, state: FSMContext, config: PropertyRentalConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    async with aiosqlite.connect(config.db_path) as db:
        rows = await (await db.execute(
            "SELECT id, address FROM properties WHERE status='available' ORDER BY id DESC LIMIT ?", (MAX_LIST_BUTTONS,),
        )).fetchall()
    if not rows:
        await cb.message.edit_text("Нет свободных объектов для нового договора.", reply_markup=kb_leases_menu())
        return
    await state.clear()
    await state.set_state(LeaseFlow.tenant_id)
    await state.update_data(started_at=time.time(), lease_pick_stage="property")
    await cb.message.edit_text("🏠 Выберите объект:", reply_markup=kb_property_pick_for_lease(rows))


@router.callback_query(LeaseFlow.tenant_id, F.data.startswith("lse_pick_prop:"))
async def cb_lse_pick_prop(cb: CallbackQuery, state: FSMContext, config: PropertyRentalConfig):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_leases_menu())
        return
    try:
        property_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    row = await _get_property(config.db_path, property_id)
    if not row or row["status"] != "available":
        await cb.message.edit_text("Этот объект больше недоступен.", reply_markup=kb_leases_menu())
        return
    await state.update_data(lease_property_id=property_id)
    await cb.message.edit_text(
        "👤 Введите Telegram ID арендатора (числом):", reply_markup=kb_flow_cancel(),
    )


@router.callback_query(F.data.startswith("lse_from_vr:"))
async def cb_lse_from_vr(cb: CallbackQuery, state: FSMContext, config: PropertyRentalConfig):
    """Shortcut from a viewing_requests detail card: the requester's own
    telegram id/name/phone are already on file — skip re-asking for them."""
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        vr_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        vr = await (await db.execute("SELECT * FROM viewing_requests WHERE id=?", (vr_id,))).fetchone()
    if not vr:
        await cb.message.edit_text("Заявка не найдена.", reply_markup=kb_leases_menu())
        return
    prop = await _get_property(config.db_path, vr["property_id"])
    if not prop or prop["status"] != "available":
        await cb.message.edit_text("Этот объект больше недоступен для сдачи.", reply_markup=kb_leases_menu())
        return
    await state.clear()
    await state.set_state(LeaseFlow.start_date)
    await state.update_data(
        started_at=time.time(), lease_property_id=prop["id"], lease_tenant_id=vr["requester_user_id"],
        lease_tenant_name=vr["requester_name"], lease_tenant_phone=vr["requester_phone"],
    )
    await cb.message.edit_text(
        f"✅ Арендатор: {_esc(vr['requester_name'] or vr['requester_user_id'])}\n\n"
        "🗓 Введите дату начала аренды в формате ГГГГ-ММ-ДД:",
        parse_mode="HTML", reply_markup=kb_flow_cancel(),
    )


@router.message(LeaseFlow.tenant_id, F.text, ~F.text.startswith("/"))
async def lease_tenant_id(msg: Message, state: FSMContext):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_leases_menu())
        return
    if not data.get("lease_property_id"):
        return  # object not chosen yet — ignore stray text
    text = msg.text.strip()
    if not _valid_admin_id(text):
        await msg.answer("Некорректный ID. Введите числовой Telegram ID арендатора:", reply_markup=kb_flow_cancel())
        return
    await state.update_data(lease_tenant_id=int(text))
    await state.set_state(LeaseFlow.tenant_name)
    await msg.answer("Имя арендатора:", reply_markup=kb_flow_cancel())


@router.message(LeaseFlow.tenant_name, F.text, ~F.text.startswith("/"))
async def lease_tenant_name(msg: Message, state: FSMContext):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_leases_menu())
        return
    name = msg.text.strip()
    if not name:
        await msg.answer("Имя не может быть пустым:", reply_markup=kb_flow_cancel())
        return
    await state.update_data(lease_tenant_name=name[:200])
    await state.set_state(LeaseFlow.tenant_phone)
    await msg.answer("📱 Телефон арендатора (или «Пропустить»):", reply_markup=kb_skip_and_cancel("lse_phone_skip"))


async def _lease_phone_next(answer, state: FSMContext, phone: str | None) -> None:
    await state.update_data(lease_tenant_phone=phone)
    await state.set_state(LeaseFlow.start_date)
    await answer("🗓 Дата начала аренды в формате ГГГГ-ММ-ДД:", reply_markup=kb_flow_cancel())


@router.message(LeaseFlow.tenant_phone, F.text, ~F.text.startswith("/"))
async def lease_tenant_phone(msg: Message, state: FSMContext):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_leases_menu())
        return
    phone = _normalize_phone(msg.text.strip())
    if phone is None:
        await msg.answer("❌ Не удалось распознать номер, или нажмите «Пропустить»:", reply_markup=kb_skip_and_cancel("lse_phone_skip"))
        return
    await _lease_phone_next(msg.answer, state, phone)


@router.callback_query(LeaseFlow.tenant_phone, F.data == "lse_phone_skip")
async def cb_lease_phone_skip(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_leases_menu())
        return
    await _lease_phone_next(cb.message.answer, state, None)


@router.message(LeaseFlow.start_date, F.text, ~F.text.startswith("/"))
async def lease_start_date(msg: Message, state: FSMContext):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_leases_menu())
        return
    start = _parse_date(msg.text)
    if start is None:
        await msg.answer("❌ Неверная дата. Формат ГГГГ-ММ-ДД, например 2026-09-01:", reply_markup=kb_flow_cancel())
        return
    await state.update_data(lease_start_date=start)
    await state.set_state(LeaseFlow.end_date)
    await msg.answer("🗓 Дата окончания аренды в формате ГГГГ-ММ-ДД:", reply_markup=kb_flow_cancel())


@router.message(LeaseFlow.end_date, F.text, ~F.text.startswith("/"))
async def lease_end_date(msg: Message, state: FSMContext):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_leases_menu())
        return
    end = _parse_date(msg.text)
    if end is None:
        await msg.answer("❌ Неверная дата. Формат ГГГГ-ММ-ДД, например 2027-09-01:", reply_markup=kb_flow_cancel())
        return
    start = data.get("lease_start_date")
    if not start or end < start:
        await msg.answer("❌ Дата окончания не может быть раньше даты начала. Введите заново:", reply_markup=kb_flow_cancel())
        return
    await state.update_data(lease_end_date=end)
    await state.set_state(LeaseFlow.amount)
    await msg.answer("💰 Сумма аренды в месяц:", reply_markup=kb_flow_cancel())


@router.message(LeaseFlow.amount, F.text, ~F.text.startswith("/"))
async def lease_amount(msg: Message, state: FSMContext):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_leases_menu())
        return
    amount = _valid_amount(msg.text)
    if amount is None:
        await msg.answer("Введите целое число, например 50000:", reply_markup=kb_flow_cancel())
        return
    await state.update_data(lease_amount=amount)
    await state.set_state(LeaseFlow.deposit)
    await msg.answer("💵 Депозит (или «Пропустить»):", reply_markup=kb_skip_and_cancel("lse_deposit_skip"))


async def _finalize_lease(answer, state: FSMContext, config: PropertyRentalConfig, bot: Bot, deposit: int | None) -> None:
    data = await state.get_data()
    property_id = data.get("lease_property_id")
    tenant_id = data.get("lease_tenant_id")
    tenant_name = data.get("lease_tenant_name")
    tenant_phone = data.get("lease_tenant_phone")
    start = data.get("lease_start_date")
    end = data.get("lease_end_date")
    amount = data.get("lease_amount")
    if not (property_id and tenant_id and start and end and amount):
        await state.clear()
        await answer("Сессия устарела, начните заново.", reply_markup=kb_leases_menu())
        return
    await state.clear()
    prop = await _get_property(config.db_path, property_id)
    if not prop or prop["status"] != "available":
        await answer("😔 Этот объект больше недоступен.", reply_markup=kb_leases_menu())
        return
    lease = await _create_lease(config.db_path, property_id, tenant_id, tenant_name, tenant_phone, start, end, amount, deposit)
    await answer(f"✅ Договор №{lease['id']} создан.", reply_markup=kb_leases_menu())
    try:
        await bot.send_message(
            tenant_id,
            f"📄 С вами заключён договор аренды на «{_esc(prop['address'])}» с {start} по {end}, "
            f"{amount} ₽/мес.",
            parse_mode="HTML",
        )
    except TelegramAPIError as e:
        logger.warning(f"property_rental: failed to notify tenant {tenant_id} of new lease {lease['id']}: {e}")


@router.message(LeaseFlow.deposit, F.text, ~F.text.startswith("/"))
async def lease_deposit(msg: Message, state: FSMContext, config: PropertyRentalConfig, bot: Bot):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_leases_menu())
        return
    deposit = _valid_amount(msg.text)
    if deposit is None:
        await msg.answer("Введите целое число или нажмите «Пропустить»:", reply_markup=kb_skip_and_cancel("lse_deposit_skip"))
        return
    await _finalize_lease(msg.answer, state, config, bot, deposit)


@router.callback_query(LeaseFlow.deposit, F.data == "lse_deposit_skip")
async def cb_lease_deposit_skip(cb: CallbackQuery, state: FSMContext, config: PropertyRentalConfig, bot: Bot):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_leases_menu())
        return
    await _finalize_lease(cb.message.answer, state, config, bot, None)


# ── OWNER: lease list/detail/status ──────────────────────────────────────────

@router.callback_query(F.data == "lse_list")
async def cb_lse_list(cb: CallbackQuery, config: PropertyRentalConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    async with aiosqlite.connect(config.db_path) as db:
        rows = await (await db.execute(
            "SELECT id, status, tenant_name FROM leases ORDER BY id DESC LIMIT ?", (MAX_LIST_BUTTONS,),
        )).fetchall()
    if not rows:
        await cb.message.edit_text("Договоров пока нет.", reply_markup=kb_leases_menu())
        return
    await cb.message.edit_text("📋 Договоры:", reply_markup=kb_lease_list(rows))


@router.callback_query(F.data.startswith("lse_view:"))
async def cb_lse_view(cb: CallbackQuery, config: PropertyRentalConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        lease_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    lease = await _get_lease(config.db_path, lease_id)
    if not lease:
        await cb.message.edit_text("Договор не найден.", reply_markup=kb_leases_menu())
        return
    prop = await _get_property(config.db_path, lease["property_id"])
    await cb.message.edit_text(_lease_text(lease, prop), parse_mode="HTML", reply_markup=kb_lease_detail(lease_id, lease["status"]))


@router.callback_query(F.data.startswith("lse_status:"))
async def cb_lse_status(cb: CallbackQuery, config: PropertyRentalConfig, bot: Bot):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        _, lid_s, new_status = cb.data.split(":", 2)
        lease_id = int(lid_s)
    except ValueError:
        return
    if new_status not in LEASE_STATUS_LABELS:
        return
    lease = await _get_lease(config.db_path, lease_id)
    if not lease:
        await cb.message.edit_text("Договор не найден.", reply_markup=kb_leases_menu())
        return
    old_status = lease["status"]
    if new_status in LEASE_STATUS_TRANSITIONS.get(old_status, []):
        async with aiosqlite.connect(config.db_path) as db:
            cur = await db.execute("UPDATE leases SET status=? WHERE id=? AND status=?", (new_status, lease_id, old_status))
            if cur.rowcount > 0:
                # A lease ending frees its property back up for a new tenant.
                await db.execute("UPDATE properties SET status='available' WHERE id=?", (lease["property_id"],))
            await db.commit()
    lease = await _get_lease(config.db_path, lease_id)
    prop = await _get_property(config.db_path, lease["property_id"])
    await cb.message.edit_text(_lease_text(lease, prop), parse_mode="HTML", reply_markup=kb_lease_detail(lease_id, lease["status"]))
    if new_status in ("ended", "terminated"):
        try:
            await bot.send_message(lease["tenant_user_id"], f"ℹ️ Ваш договор аренды №{lease_id} переведён в статус: {LEASE_STATUS_LABELS[new_status]}")
        except TelegramAPIError as e:
            logger.warning(f"property_rental: failed to notify tenant of lease status change {lease_id}: {e}")


# ── OWNER: rent payments (accrue / mark paid / overdue) ─────────────────────

@router.callback_query(F.data.startswith("pay_new:"))
async def cb_pay_new(cb: CallbackQuery, state: FSMContext, config: PropertyRentalConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        lease_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    lease = await _get_lease(config.db_path, lease_id)
    if not lease:
        await cb.message.edit_text("Договор не найден.", reply_markup=kb_leases_menu())
        return
    await state.set_state(PaymentFlow.period)
    await state.update_data(started_at=time.time(), pay_lease_id=lease_id, pay_default_amount=lease["monthly_amount"])
    await cb.message.edit_text("📅 Период платежа в формате ГГГГ-ММ (например 2026-09):", reply_markup=kb_flow_cancel())


@router.message(PaymentFlow.period, F.text, ~F.text.startswith("/"))
async def pay_period(msg: Message, state: FSMContext):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_leases_menu())
        return
    period = _parse_period(msg.text)
    if period is None:
        await msg.answer("❌ Формат ГГГГ-ММ, например 2026-09:", reply_markup=kb_flow_cancel())
        return
    await state.update_data(pay_period=period)
    await state.set_state(PaymentFlow.due_date)
    await msg.answer("🗓 Срок оплаты в формате ГГГГ-ММ-ДД:", reply_markup=kb_flow_cancel())


@router.message(PaymentFlow.due_date, F.text, ~F.text.startswith("/"))
async def pay_due_date(msg: Message, state: FSMContext):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_leases_menu())
        return
    due = _parse_date(msg.text)
    if due is None:
        await msg.answer("❌ Формат ГГГГ-ММ-ДД, например 2026-09-05:", reply_markup=kb_flow_cancel())
        return
    await state.update_data(pay_due_date=due)
    await state.set_state(PaymentFlow.amount)
    default_amount = data.get("pay_default_amount")
    await msg.answer(
        f"💰 Сумма платежа (по договору: {default_amount} ₽) — введите число или нажмите «Как в договоре»:",
        reply_markup=kb_skip_and_cancel("pay_amount_default"),
    )


async def _finalize_payment(answer, state: FSMContext, config: PropertyRentalConfig, amount: int) -> None:
    data = await state.get_data()
    lease_id = data.get("pay_lease_id")
    period = data.get("pay_period")
    due = data.get("pay_due_date")
    if not (lease_id and period and due):
        await state.clear()
        await answer("Сессия устарела, начните заново.", reply_markup=kb_leases_menu())
        return
    await state.clear()
    payment_id = await _create_payment(config.db_path, lease_id, period, due, amount)
    await answer(f"✅ Платёж №{payment_id} начислен ({period}, {amount} ₽, срок {due}).", reply_markup=kb_leases_menu())


@router.message(PaymentFlow.amount, F.text, ~F.text.startswith("/"))
async def pay_amount(msg: Message, state: FSMContext, config: PropertyRentalConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_leases_menu())
        return
    amount = _valid_amount(msg.text)
    if amount is None:
        await msg.answer("Введите целое число или нажмите «Как в договоре»:", reply_markup=kb_skip_and_cancel("pay_amount_default"))
        return
    await _finalize_payment(msg.answer, state, config, amount)


@router.callback_query(PaymentFlow.amount, F.data == "pay_amount_default")
async def cb_pay_amount_default(cb: CallbackQuery, state: FSMContext, config: PropertyRentalConfig):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_leases_menu())
        return
    await _finalize_payment(cb.message.answer, state, config, data.get("pay_default_amount"))


@router.callback_query(F.data.startswith("pay_list:"))
async def cb_pay_list(cb: CallbackQuery, config: PropertyRentalConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        lease_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    async with aiosqlite.connect(config.db_path) as db:
        rows = await (await db.execute(
            "SELECT id, status, period, amount FROM rent_payments WHERE lease_id=? ORDER BY due_date DESC LIMIT ?",
            (lease_id, MAX_LIST_BUTTONS),
        )).fetchall()
    if not rows:
        await cb.message.edit_text("Платежей пока нет.", reply_markup=kb_back_markup(f"lse_view:{lease_id}"))
        return
    await cb.message.edit_text("💰 Платежи:", reply_markup=kb_payment_list(rows, lease_id))


@router.callback_query(F.data.startswith("pay_view:"))
async def cb_pay_view(cb: CallbackQuery, config: PropertyRentalConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        payment_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    payment = await _get_payment(config.db_path, payment_id)
    if not payment:
        await cb.message.edit_text("Платёж не найден.", reply_markup=kb_leases_menu())
        return
    await cb.message.edit_text(
        _payment_text(payment), parse_mode="HTML",
        reply_markup=kb_payment_detail(payment_id, payment["status"], payment["lease_id"]),
    )


@router.callback_query(F.data.startswith("pay_status:"))
async def cb_pay_status(cb: CallbackQuery, config: PropertyRentalConfig, bot: Bot):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        _, pid_s, new_status = cb.data.split(":", 2)
        payment_id = int(pid_s)
    except ValueError:
        return
    if new_status not in PAY_STATUS_LABELS:
        return
    payment = await _get_payment(config.db_path, payment_id)
    if not payment:
        await cb.message.edit_text("Платёж не найден.", reply_markup=kb_leases_menu())
        return
    old_status = payment["status"]
    if new_status in PAY_STATUS_TRANSITIONS.get(old_status, []):
        await _mark_payment_status(config.db_path, payment_id, old_status, new_status)
        if new_status == "paid":
            lease = await _get_lease(config.db_path, payment["lease_id"])
            if lease:
                try:
                    await bot.send_message(lease["tenant_user_id"], f"✅ Ваш платёж за {payment['period']} отмечен как оплаченный.")
                except TelegramAPIError as e:
                    logger.warning(f"property_rental: failed to notify tenant of payment {payment_id}: {e}")
    payment = await _get_payment(config.db_path, payment_id)
    await cb.message.edit_text(
        _payment_text(payment), parse_mode="HTML",
        reply_markup=kb_payment_detail(payment_id, payment["status"], payment["lease_id"]),
    )


# ── OWNER: maintenance requests ──────────────────────────────────────────────

@router.callback_query(F.data == "mnt_menu")
async def cb_mnt_menu(cb: CallbackQuery, state: FSMContext, config: PropertyRentalConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    await cb.message.edit_text("Выберите фильтр по статусу:", reply_markup=kb_mnt_filters())


@router.callback_query(F.data.startswith("mnt_filter:"))
async def cb_mnt_filter(cb: CallbackQuery, config: PropertyRentalConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    status = cb.data.split(":", 1)[1]
    async with aiosqlite.connect(config.db_path) as db:
        if status == "all":
            rows = await (await db.execute(
                "SELECT id, status, description FROM maintenance_requests ORDER BY id DESC LIMIT ?", (MAX_LIST_BUTTONS,)
            )).fetchall()
        else:
            rows = await (await db.execute(
                "SELECT id, status, description FROM maintenance_requests WHERE status=? ORDER BY id DESC LIMIT ?",
                (status, MAX_LIST_BUTTONS),
            )).fetchall()
    if not rows:
        await cb.message.edit_text("Заявок не найдено.", reply_markup=kb_mnt_filters())
        return
    await cb.message.edit_text(f"🔧 Заявки на ремонт ({len(rows)}):", reply_markup=kb_mnt_list(rows, "mnt_menu"))


@router.callback_query(F.data.startswith("mnt_view:"))
async def cb_mnt_view(cb: CallbackQuery, config: PropertyRentalConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        request_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    row = await _get_maintenance(config.db_path, request_id)
    if not row:
        await cb.message.edit_text("Заявка не найдена.", reply_markup=kb_mnt_filters())
        return
    await cb.message.edit_text(_maintenance_text(row), parse_mode="HTML", reply_markup=kb_mnt_detail(request_id, row["status"]))


@router.callback_query(F.data.startswith("mnt_status:"))
async def cb_mnt_status(cb: CallbackQuery, config: PropertyRentalConfig, bot: Bot):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    try:
        _, rid_s, new_status = cb.data.split(":", 2)
        request_id = int(rid_s)
    except ValueError:
        return
    if new_status not in MAINT_STATUS_LABELS:
        return
    row = await _get_maintenance(config.db_path, request_id)
    if not row:
        await cb.message.edit_text("Заявка не найдена.", reply_markup=kb_mnt_filters())
        return
    old_status = row["status"]
    if new_status in MAINT_STATUS_TRANSITIONS.get(old_status, []):
        async with aiosqlite.connect(config.db_path) as db:
            cur = await db.execute(
                "UPDATE maintenance_requests SET status=? WHERE id=? AND status=?", (new_status, request_id, old_status),
            )
            await db.commit()
        if cur.rowcount > 0:
            try:
                await bot.send_message(
                    row["tenant_user_id"],
                    f"🔧 Статус вашей заявки на ремонт №{request_id} изменён: {MAINT_STATUS_LABELS[new_status]}",
                )
            except TelegramAPIError as e:
                logger.warning(f"property_rental: failed to notify tenant of maintenance status {request_id}: {e}")
    row = await _get_maintenance(config.db_path, request_id)
    await cb.message.edit_text(_maintenance_text(row), parse_mode="HTML", reply_markup=kb_mnt_detail(request_id, row["status"]))


# ── OWNER: analytics ─────────────────────────────────────────────────────────

@router.callback_query(F.data == "an_view")
async def cb_analytics(cb: CallbackQuery, config: PropertyRentalConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        total = (await (await db.execute("SELECT COUNT(*) c FROM properties")).fetchone())["c"]
        occupied = (await (await db.execute("SELECT COUNT(*) c FROM properties WHERE status='occupied'")).fetchone())["c"]
        revenue_row = await (await db.execute("SELECT COALESCE(SUM(amount),0) s FROM rent_payments WHERE status='paid'")).fetchone()
        revenue = revenue_row["s"]
        overdue_rows = await (await db.execute(
            "SELECT rp.id, rp.period, rp.amount, l.tenant_name FROM rent_payments rp "
            "JOIN leases l ON l.id = rp.lease_id WHERE rp.status='overdue' ORDER BY rp.due_date ASC LIMIT ?",
            (MAX_LIST_BUTTONS,),
        )).fetchall()
        today = date.today().isoformat()
        horizon = (date.today() + timedelta(days=LEASE_EXPIRING_WITHIN_DAYS)).isoformat()
        expiring_rows = await (await db.execute(
            "SELECT id, tenant_name, end_date FROM leases WHERE status='active' AND end_date BETWEEN ? AND ? "
            "ORDER BY end_date ASC LIMIT ?",
            (today, horizon, MAX_LIST_BUTTONS),
        )).fetchall()

    occupancy_pct = round(100 * occupied / total) if total else 0
    lines = [
        "📊 <b>Аналитика</b>\n",
        f"🏠 Объектов: {total} (занято: {occupied}, заполняемость: {occupancy_pct}%)",
        f"💰 Доход от аренды (оплачено): {revenue} ₽",
        f"🔴 Просроченных платежей: {len(overdue_rows)}",
    ]
    for r in overdue_rows[:10]:
        lines.append(
            f"  • №{r['id']} {_esc(r['tenant_name'] or '—')} · {r['period']} · "
            f"{_amount_with_late_fee(r['amount'])} ₽ с пеней"
        )
    lines.append(f"\n📅 Договоры, истекающие в течение {LEASE_EXPIRING_WITHIN_DAYS} дней: {len(expiring_rows)}")
    for r in expiring_rows[:10]:
        lines.append(f"  • №{r['id']} {_esc(r['tenant_name'] or '—')} · до {r['end_date']}")

    await cb.message.edit_text(_join_bounded(lines), parse_mode="HTML", reply_markup=kb_back_markup())


# ── TENANT: my lease, my maintenance requests ────────────────────────────────

@router.callback_query(F.data == "ten_lease")
async def cb_ten_lease(cb: CallbackQuery, config: PropertyRentalConfig):
    await cb.answer()
    lease = await _active_lease_for(config.db_path, cb.from_user.id)
    if not lease:
        await cb.message.edit_text("У вас пока нет активного договора аренды.", reply_markup=kb_back_markup())
        return
    prop = await _get_property(config.db_path, lease["property_id"])
    async with aiosqlite.connect(config.db_path) as db:
        rows = await (await db.execute(
            "SELECT status, period, amount, due_date FROM rent_payments WHERE lease_id=? ORDER BY due_date DESC LIMIT 6",
            (lease["id"],),
        )).fetchall()
    lines = [_lease_text(lease, prop), "\n💰 <b>Последние платежи:</b>"]
    if not rows:
        lines.append("— пока нет —")
    for status, period, amount, due in rows:
        lines.append(f"• {period} · {amount} ₽ · {PAY_STATUS_LABELS.get(status, status)} (срок {due})")
    await cb.message.edit_text(_join_bounded(lines), parse_mode="HTML", reply_markup=kb_back_markup())


@router.callback_query(F.data == "tmnt_new")
async def cb_tmnt_new(cb: CallbackQuery, state: FSMContext, config: PropertyRentalConfig):
    await cb.answer()
    lease = await _active_lease_for(config.db_path, cb.from_user.id)
    if not lease:
        await cb.message.edit_text("У вас нет активного договора аренды, заявку оставить нельзя.", reply_markup=kb_back_markup())
        return
    await state.clear()
    await state.set_state(MaintenanceFlow.description)
    await state.update_data(started_at=time.time(), maint_lease_id=lease["id"], maint_property_id=lease["property_id"])
    await cb.message.edit_text("🔧 Опишите проблему:", reply_markup=kb_flow_cancel())


@router.message(MaintenanceFlow.description, F.text, ~F.text.startswith("/"))
async def tmnt_description(msg: Message, state: FSMContext):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_back_markup())
        return
    desc = msg.text.strip()
    if not desc:
        await msg.answer("Описание не может быть пустым:", reply_markup=kb_flow_cancel())
        return
    if len(desc) > MAX_DESCRIPTION_LEN:
        await msg.answer(f"⚠️ Уложитесь в {MAX_DESCRIPTION_LEN} символов:", reply_markup=kb_flow_cancel())
        return
    await state.update_data(maint_description=desc)
    await state.set_state(MaintenanceFlow.photo)
    await msg.answer("📷 Пришлите фото (или «Пропустить»):", reply_markup=kb_skip_and_cancel("tmnt_photo_skip"))


async def _finalize_maintenance(answer, state: FSMContext, config: PropertyRentalConfig, bot: Bot, from_user, photo_file_id: str | None) -> None:
    data = await state.get_data()
    lease_id = data.get("maint_lease_id")
    property_id = data.get("maint_property_id")
    description = data.get("maint_description")
    if not (lease_id and property_id and description):
        await state.clear()
        await answer("Сессия устарела, начните заново.", reply_markup=kb_back_markup())
        return
    await state.clear()
    async with aiosqlite.connect(config.db_path) as db:
        cur = await db.execute(
            "INSERT INTO maintenance_requests (lease_id, property_id, tenant_user_id, description, photo) "
            "VALUES (?,?,?,?,?)",
            (lease_id, property_id, from_user.id, description, photo_file_id),
        )
        request_id = cur.lastrowid
        await db.commit()
    await answer(f"✅ Заявка №{request_id} создана.", reply_markup=kb_back_markup())

    admins = _load_admins(config.admins_file)
    for admin_id in admins:
        try:
            await bot.send_message(int(admin_id), f"🔧 Новая заявка на ремонт №{request_id}:\n{_esc(description)}")
        except (TelegramAPIError, ValueError) as e:
            logger.warning(f"property_rental: failed to notify admin {admin_id} of maintenance request {request_id}: {e}")


@router.message(MaintenanceFlow.photo, F.photo)
async def tmnt_photo(msg: Message, state: FSMContext, config: PropertyRentalConfig, bot: Bot):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_back_markup())
        return
    await _finalize_maintenance(msg.answer, state, config, bot, msg.from_user, msg.photo[-1].file_id)


@router.callback_query(MaintenanceFlow.photo, F.data == "tmnt_photo_skip")
async def cb_tmnt_photo_skip(cb: CallbackQuery, state: FSMContext, config: PropertyRentalConfig, bot: Bot):
    await cb.answer()
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_back_markup())
        return
    await _finalize_maintenance(cb.message.answer, state, config, bot, cb.from_user, None)


@router.callback_query(F.data == "tmnt_mine")
async def cb_tmnt_mine(cb: CallbackQuery, config: PropertyRentalConfig):
    await cb.answer()
    async with aiosqlite.connect(config.db_path) as db:
        rows = await (await db.execute(
            "SELECT id, status, description FROM maintenance_requests WHERE tenant_user_id=? ORDER BY id DESC LIMIT ?",
            (cb.from_user.id, MAX_LIST_BUTTONS),
        )).fetchall()
    if not rows:
        await cb.message.edit_text("У вас пока нет заявок на ремонт.", reply_markup=kb_back_markup())
        return
    await cb.message.edit_text("📋 Ваши заявки:", reply_markup=kb_mnt_list(rows, "main_menu", prefix="tmnt_view"))


@router.callback_query(F.data.startswith("tmnt_view:"))
async def cb_tmnt_view(cb: CallbackQuery, config: PropertyRentalConfig):
    await cb.answer()
    try:
        request_id = int(cb.data.split(":", 1)[1])
    except ValueError:
        return
    # Ownership check: a hand-crafted callback_data guessing another
    # tenant's request id must not leak it — same principle as
    # templates/repair_tracker.py's cb_tkt_view.
    row = await _get_maintenance(config.db_path, request_id)
    if not row or row["tenant_user_id"] != cb.from_user.id:
        await cb.message.edit_text("Заявка не найдена.", reply_markup=kb_back_markup())
        return
    await cb.message.edit_text(_maintenance_text(row), parse_mode="HTML", reply_markup=kb_back_markup())


# ── ADMINS menu (standard shape, every template) ────────────────────────────

async def _admins_list_text(config: PropertyRentalConfig) -> str:
    ids = sorted(_load_admins(config.admins_file))
    if not ids:
        return "👥 Пусто"
    return _join_bounded(["👥 <b>Администраторы бота:</b>\n"] + [f"• <code>{_esc(i)}</code>" for i in ids])


@router.callback_query(F.data == "adm_menu")
async def cb_adm_menu(cb: CallbackQuery, state: FSMContext, config: PropertyRentalConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.clear()
    text = await _admins_list_text(config)
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb_admins_menu())


@router.callback_query(F.data == "adm_add")
async def cb_adm_add(cb: CallbackQuery, state: FSMContext, config: PropertyRentalConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    await state.set_state(AdminMgmtFlow.add_admin)
    await state.update_data(started_at=time.time())
    await cb.message.edit_text("Введите Telegram ID нового администратора:", reply_markup=kb_flow_cancel())


@router.message(AdminMgmtFlow.add_admin, F.text, ~F.text.startswith("/"))
async def admin_add_id(msg: Message, state: FSMContext, config: PropertyRentalConfig):
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await msg.answer("Сессия истекла, начните заново.", reply_markup=kb_owner_menu())
        return
    text = msg.text.strip()
    if not _valid_admin_id(text):
        await msg.answer("Некорректный ID. Введите числовой Telegram ID.", reply_markup=kb_flow_cancel())
        return
    await state.clear()
    ids = _load_admins(config.admins_file)
    ids.add(text)
    _save_admins(config.admins_file, ids)
    await _grant_owner_access(config.db_path, int(text))
    if config.bot_id is not None:
        try:
            await add_bot_admin(config.bot_id, text)
        except Exception as e:
            logger.warning(f"admin_add_id: add_bot_admin sync failed for bot {config.bot_id}: {e}")
    await msg.answer(f"✅ <code>{text}</code> добавлен.", parse_mode="HTML", reply_markup=kb_admins_menu())


@router.callback_query(F.data == "adm_remove")
async def cb_adm_remove(cb: CallbackQuery, state: FSMContext, config: PropertyRentalConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    ids = sorted(_load_admins(config.admins_file))
    if len(ids) <= 1:
        await cb.message.edit_text("Нельзя удалить последнего администратора.", reply_markup=kb_admins_menu())
        return
    if len(ids) > MAX_ADMIN_REMOVE_BUTTONS:
        await cb.message.edit_text("Слишком много админов для списка кнопок. Обратитесь к разработчику.", reply_markup=kb_admins_menu())
        return
    await state.set_state(AdminMgmtFlow.remove_admin_pick)
    await state.update_data(started_at=time.time(), remove_admin_ids=ids)
    await cb.message.edit_text("Выберите администратора для удаления:", reply_markup=kb_remove_admins(ids))


@router.callback_query(AdminMgmtFlow.remove_admin_pick, F.data.startswith("adm_rm:"))
async def cb_adm_remove_pick(cb: CallbackQuery, state: FSMContext, config: PropertyRentalConfig):
    await cb.answer()
    if not _is_admin(cb.from_user.id, config):
        return
    data = await state.get_data()
    if _flow_expired(data):
        await state.clear()
        await cb.message.edit_text("Сессия истекла, начните заново.", reply_markup=kb_owner_menu())
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
    if config.bot_id is not None:
        try:
            await remove_bot_admin(config.bot_id, target)
        except Exception as e:
            logger.warning(f"cb_adm_remove_pick: remove_bot_admin sync failed for bot {config.bot_id}: {e}")
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
    sweep_task = asyncio.create_task(_overdue_sweep_loop(bot, config.db_path))
    try:
        await dp.start_polling(bot)
    finally:
        sweep_task.cancel()

if __name__ == "__main__":
    asyncio.run(main())
