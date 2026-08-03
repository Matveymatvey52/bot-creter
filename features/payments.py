# FEATURE: payments
# COMPATIBLE_WITH: accountant, booking_beauty, booking_medical, campaign_tracker, inventory, manager_secretary, moderator, referral_program, tour_operator, tourist_documents, trip_manager
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
import sqlite3
from typing import Protocol

import aiosqlite
from aiogram import Bot, F, Router
from aiogram.types import LabeledPrice, Message, PreCheckoutQuery

from db.database import get_bot_payment_provider

logger = logging.getLogger(__name__)

router = Router()


class PaymentsConfig(Protocol):
    """Any template Config dataclass with a db_path works — duck-typed so this
    module doesn't need to know about any specific template's Config shape."""
    db_path: str


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
    """Thin wrapper over bot.send_invoice() — fetches this bot's provider_token
    from bot_payment_providers so callers never handle the credential directly.
    Raises ValueError (caller's to catch and turn into an admin-facing message)
    if no provider is configured for this bot yet."""
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
            await db.execute(
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
