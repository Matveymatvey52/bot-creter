# FEATURE: payments
# COMPATIBLE_WITH: accountant, booking_beauty, booking_fitness, booking_medical, booking_restaurant, campaign_tracker, car_rental, course_tracker, coworking_space, delivery_tracker, event_manager, event_rsvp, inventory, loyalty_program, manager_secretary, moderator, orders_tracker, referral_program, repair_tracker, shop_catalog, tourist_documents, trip_manager, vehicle_service
"""Reusable Telegram Payments feature module — see docs/STAGE2_DESIGN.md "Фаза
B — services/payments.py" for the original design, and feature-modules-inventory
for how this became the library's first feature module.

Exposes a module-level `router` (pre_checkout_query + successful_payment already
registered on it) so runtime/registry.py's build_entry() can clone and attach it
to any bot that has "payments" enabled in bot_features, the same by-convention
mechanism templates/*.py already uses — no per-template wiring needed. A host
template's own ConfigMiddleware still supplies `data["config"]` (with .db_path)
to these handlers, since this router rides in the SAME Dispatcher as the host
template's own router; this module intentionally has no config_from_bot_row of
its own (features reuse the host's per-bot db_path — additional tables in the
same file, not a separate db per feature).

Refunds are NEVER sent back through the provider (there is no such API for
real money on Telegram Payments) — record_refund() only flips a status flag.
Callers must surface that as "отметить как возвращённый", not "вернуть деньги".
"""
from __future__ import annotations

import logging
import os
import sqlite3
from typing import Protocol

import aiosqlite
from aiogram import Bot, F, Router
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, Message, PreCheckoutQuery

from db.database import create_cloudpayments_invoice, get_bot_payment_provider, get_bot_provider_type
from features.office_events import OrderCreatedEvent, publish_event

logger = logging.getLogger(__name__)

router = Router()


class PaymentsConfig(Protocol):
    """Any template Config dataclass with a db_path AND bot_id works —
    duck-typed so this module doesn't need to know about any specific
    template's Config shape. bot_id is required (not just db_path) since
    on_successful_payment now publishes office_events' order.created with
    source_bot_id=config.bot_id — see docs/OFFICES_DESIGN.md §10 q4. Every
    template wired to payments already carries bot_id in webhook mode
    (config_from_bot_row sets it — same convention as templates/
    tour_operator.py's/templates/event_rsvp.py's own Config.bot_id)."""
    db_path: str
    bot_id: int | None


async def init_payments_tables(db_path: str) -> None:
    """Adds the payments table to a template's OWN per-bot db_path. Call this
    from the template's own init_db(), alongside its other CREATE TABLEs —
    this module never opens/owns a database file by itself.

    Sets journal_mode=WAL — review found the default rollback journal makes a
    concurrent write (e.g. a /refund landing mid-successful_payment insert) far
    more likely to raise sqlite3.OperationalError ("database is locked")
    instead of the sqlite3.IntegrityError on_successful_payment actually
    handles; WAL lets readers and one writer proceed without blocking on each
    other, which is what money-critical inserts need."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id                          INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_payment_charge_id  TEXT NOT NULL UNIQUE,
                provider_payment_charge_id  TEXT,
                user_id                     INTEGER NOT NULL,
                invoice_payload             TEXT NOT NULL,
                currency                    TEXT NOT NULL,
                total_amount                INTEGER NOT NULL,
                status                      TEXT NOT NULL DEFAULT 'paid' CHECK(status IN ('paid','refunded')),
                refunded_at                 TEXT,
                refunded_by                 TEXT,
                created_at                  TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.commit()


async def create_invoice(
    bot: Bot,
    bot_id: int,
    chat_id: int,
    title: str,
    description: str,
    payload: str,
    currency: str,
    prices: list[LabeledPrice],
) -> Message:
    """Sends a payment prompt via whichever provider this bot has connected
    (bot_payment_providers.provider_type — see handlers/manage_bots.py's
    PaymentConnectFlow / CloudpaymentsConnectFlow). Two entirely different
    payment mechanisms live behind this one signature so every one of the
    ~24 payments-compatible templates keeps calling create_invoice() exactly
    as before regardless of which provider the bot owner picked:

      - 'yookassa' (default): the original behavior — bot.send_invoice()
        with provider_token, a native Telegram Payments invoice message.
      - 'cloudpayments': no Telegram provider_token exists at all for this
        bot. Instead a `cloudpayments_invoices` row is created and a message
        with an inline "Оплатить" button (url=...) pointing at
        runtime/cloudpayments_api.py's own /pay/{bot_id}/{invoice_id} page is
        sent — see PAYMENT_STATUS.md / docs discussion "Вариант 1", same
        button-not-raw-link shape as ЮKassa's own send_invoice message.
        on_successful_payment's aiogram
        F.successful_payment listener never fires for this path; the
        webhook handler in cloudpayments_api.py performs the equivalent
        payments-table insert + office_events publish itself.

    Raises ValueError (caller's to catch and turn into an admin-facing
    message) if no provider is configured for this bot yet."""
    provider_type = await get_bot_provider_type(bot_id)
    if provider_type == "cloudpayments":
        return await _create_cloudpayments_invoice_message(
            bot, bot_id, chat_id, title, description, payload, currency, prices,
        )

    provider_token = await get_bot_payment_provider(bot_id)
    if not provider_token:
        logger.warning(f"create_invoice: no payment provider configured for bot_id={bot_id}")
        raise ValueError(
            f"No payment provider configured for bot_id={bot_id} — "
            "set one in bot_payment_providers before selling anything."
        )
    invoice_message = await bot.send_invoice(
        chat_id=chat_id,
        title=title,
        description=description,
        payload=payload,
        provider_token=provider_token,
        currency=currency,
        prices=prices,
    )
    logger.info(f"create_invoice: sent invoice bot_id={bot_id} chat_id={chat_id} payload={payload}")
    return invoice_message


async def _create_cloudpayments_invoice_message(
    bot: Bot,
    bot_id: int,
    chat_id: int,
    title: str,
    description: str,
    payload: str,
    currency: str,
    prices: list[LabeledPrice],
) -> Message:
    """LabeledPrice amounts are minor units summed the same way Telegram's
    own send_invoice expects (see aiogram's LabeledPrice docs) — reused
    as-is so callers don't need a different amount convention per provider."""
    total_amount = sum(p.amount for p in prices)
    invoice_id = await create_cloudpayments_invoice(
        bot_id=bot_id,
        invoice_payload=payload,
        title=title,
        description=description,
        currency=currency,
        amount=total_amount,
        chat_id=chat_id,
    )
    base_url = os.getenv("PUBLIC_BASE_URL", "").strip()
    if not base_url:
        logger.error(f"create_invoice: PUBLIC_BASE_URL not set — cannot build a Cloudpayments /pay link for bot_id={bot_id}")
        raise ValueError("PUBLIC_BASE_URL is not configured — cannot generate a Cloudpayments payment link.")
    pay_url = f"{base_url.rstrip('/')}/pay/{bot_id}/{invoice_id}"
    message = await bot.send_message(
        chat_id=chat_id,
        text=f"💳 {title}\n{description}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Оплатить", url=pay_url)],
        ]),
    )
    logger.info(f"create_invoice: sent Cloudpayments link bot_id={bot_id} chat_id={chat_id} invoice_id={invoice_id}")
    return message


async def on_pre_checkout_query(query: PreCheckoutQuery) -> None:
    """Telegram gives 10 seconds to answer this, shared across every bot on the
    same event loop (see Phase A — docs/STAGE2_DESIGN.md / payment-eventloop-fix).
    Deliberately does ZERO I/O: no DB, no HTTP, nothing awaited except the
    answer itself. Only synchronous, in-memory validation belongs here."""
    ok = bool(query.invoice_payload)
    try:
        await query.answer(ok=ok, error_message=None if ok else "Некорректный заказ, попробуйте оформить его заново.")
    except Exception:
        logger.error(f"on_pre_checkout_query: answer() failed for query_id={query.id}", exc_info=True)


async def on_successful_payment(message: Message, config: PaymentsConfig) -> None:
    """Records a completed payment. Telegram may redeliver the same
    successful_payment update — telegram_payment_charge_id is UNIQUE, so a
    re-delivery hits sqlite3.IntegrityError and is silently ignored (already
    credited), same pattern as templates/inventory.py's cmd_additem.

    Any OTHER sqlite3 error (e.g. OperationalError from a lock, less likely
    now that init_payments_tables sets WAL but still possible) is logged at
    ERROR with the charge_id before re-raising — review found this was the one
    failure mode with zero trace anywhere: money charged by the provider, no
    row, no log. Re-raising (not swallowing) keeps it visible to whatever
    error handling the runtime already has (aiogram/webhook_app's catch-all)."""
    payment = message.successful_payment
    if payment is None:
        return
    async with aiosqlite.connect(config.db_path) as db:
        await db.execute("PRAGMA busy_timeout=5000")
        try:
            cursor = await db.execute(
                """
                INSERT INTO payments
                    (telegram_payment_charge_id, provider_payment_charge_id,
                     user_id, invoice_payload, currency, total_amount)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    payment.telegram_payment_charge_id,
                    payment.provider_payment_charge_id,
                    message.from_user.id,
                    payment.invoice_payload,
                    payment.currency,
                    payment.total_amount,
                ),
            )
            await db.commit()
            payments_row_id = cursor.lastrowid
        except sqlite3.IntegrityError:
            logger.info(
                f"on_successful_payment: duplicate delivery of "
                f"telegram_payment_charge_id={payment.telegram_payment_charge_id}, ignoring"
            )
            return
        except (sqlite3.Error, ValueError):
            # ValueError covers e.g. an embedded null byte in a TEXT column
            # (not a sqlite3.Error subclass) — review found it would otherwise
            # slip past both excepts here silently: money charged, no row, no
            # log, exactly the failure mode this function's docstring warns
            # about.
            logger.error(
                f"on_successful_payment: FAILED to record payment "
                f"telegram_payment_charge_id={payment.telegram_payment_charge_id} "
                f"user_id={message.from_user.id} amount={payment.total_amount} {payment.currency} — "
                "money was charged by the provider but NOT recorded",
                exc_info=True,
            )
            raise
    logger.info(
        f"on_successful_payment: recorded telegram_payment_charge_id={payment.telegram_payment_charge_id} "
        f"user_id={message.from_user.id} amount={payment.total_amount} {payment.currency}"
    )

    # "Офисы" (docs/OFFICES_DESIGN.md §10 q4) — publish order.created from this
    # ONE central place rather than each of the 27 payments-compatible
    # templates calling publish_event() itself: order_id/amount/currency/
    # customer_chat_id are already on hand here, right after the credit is
    # durably committed. config.bot_id is None only in standalone/subprocess
    # mode (no bots-table row to source it from — see PaymentsConfig's
    # docstring); office_events has no meaning there (no live Registry either,
    # per features/office_events.py's own no-op-without-registry behavior),
    # so this is skipped rather than publishing with a fabricated source_bot_id.
    # Deliberately best-effort: publish_event() never raises for a subscriber
    # failure, but a broken office_events call here must ALSO never turn a
    # successfully credited payment into an error response to Telegram —
    # same isolation reasoning as runtime/registry.py's
    # _load_and_include_features().
    if config.bot_id is not None:
        try:
            await publish_event(
                config.bot_id,
                "order.created",
                OrderCreatedEvent(
                    order_id=payments_row_id,
                    amount=payment.total_amount,
                    currency=payment.currency,
                    customer_chat_id=message.from_user.id,
                ),
            )
        except Exception:
            logger.exception(
                "on_successful_payment: publish_event(order.created) raised — "
                f"telegram_payment_charge_id={payment.telegram_payment_charge_id} bot_id={config.bot_id}, "
                "payment credit is unaffected"
            )


async def record_refund(db_path: str, telegram_payment_charge_id: str, admin_id: str) -> bool:
    """Manual status flip ONLY — there is no API to actually return real money
    through a Telegram Payments provider. Returns True if a 'paid' row was
    flipped to 'refunded', False if no such charge exists or it was already
    refunded (caller turns that into its own user-facing text)."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout=5000")
        try:
            cursor = await db.execute(
                """
                UPDATE payments
                SET status = 'refunded', refunded_at = datetime('now','localtime'), refunded_by = ?
                WHERE telegram_payment_charge_id = ? AND status = 'paid'
                """,
                (admin_id, telegram_payment_charge_id),
            )
            await db.commit()
        except sqlite3.Error:
            # A concurrent successful_payment insert holding the writer lock
            # would otherwise surface as an unhandled "database is locked" —
            # busy_timeout above should absorb most of that, but if it still
            # happens the admin deserves a log line, not a swallowed 500.
            logger.error(
                f"record_refund: FAILED to update telegram_payment_charge_id={telegram_payment_charge_id} "
                f"admin_id={admin_id}",
                exc_info=True,
            )
            raise
        ok = cursor.rowcount > 0
    logger.info(
        f"record_refund: telegram_payment_charge_id={telegram_payment_charge_id} "
        f"admin_id={admin_id} ok={ok}"
    )
    return ok


router.pre_checkout_query.register(on_pre_checkout_query)
router.message.register(on_successful_payment, F.successful_payment)

# runtime/registry.py's build_entry() expects an `init_db(db_path)` attribute
# by convention (same as templates/*.py) — this is the same function
# init_payments_tables already was, just under the expected name too.
init_db = init_payments_tables
