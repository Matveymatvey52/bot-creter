"""Cloudpayments checkout — "Вариант 1" from the payments-provider-choice
design discussion (see https://claude.ai/code/artifact/ca3f6b07-28b3-4719-86e4-fc3b2d2536ce
mockup for both variants). Unlike ЮKassa (a native Telegram Payments
provider_token, handled entirely inside features/payments.py's aiogram
router), Cloudpayments has no Telegram-side integration at all — the
customer is sent a link to a standalone HTML page served here, which embeds
the official Cloudpayments JS widget and never touches the bot's own chat
until payment succeeds.

Adds routes to the SAME aiohttp Application the webhook already runs on —
same by-convention register_routes(app) shape as runtime/miniapp_api.py,
runtime/factory_analytics_api.py, runtime/owner_report_api.py (all wired
together in runtime/combined_app.py's _bootstrap_app).

Two routes:
  - GET  /pay/{bot_id}/{invoice_id}   — renders the checkout page
  - POST /webhook/cloudpayments        — Cloudpayments' own server-to-server
    "Pay"/"Fail" notification, HMAC-SHA256-signed per bot (Content-HMAC
    header, base64 of HMAC-SHA256(raw_body, api_secret)) — see
    get_bot_cloudpayments_credentials for where that per-bot api_secret
    comes from. Fail-closed: a missing/invalid signature is rejected before
    the body is ever parsed as form data, same posture as webhook_app.py's
    own WEBHOOK_SECRET check.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import logging

from aiohttp import web

from db.database import (
    get_bot_cloudpayments_credentials,
    get_cloudpayments_invoice,
    mark_cloudpayments_invoice_paid,
)
from runtime.registry_holder import RegistryHandle

logger = logging.getLogger(__name__)

# Same pattern as handlers/create_bot.py / handlers/manage_bots.py's own
# module-level RegistryHandle — filled in by combined_app.py's bootstrap
# via set_registry() below, after the live Registry is built.
_registry_handle = RegistryHandle()


def set_registry(registry) -> None:
    _registry_handle.set(registry)


async def pay_page_handler(request: web.Request) -> web.Response:
    bot_id_raw = request.match_info.get("bot_id", "")
    invoice_id_raw = request.match_info.get("invoice_id", "")
    if not bot_id_raw.isdigit() or not invoice_id_raw.isdigit():
        return web.Response(text="Bad request", status=404)
    bot_id = int(bot_id_raw)
    invoice_id = int(invoice_id_raw)

    invoice = await get_cloudpayments_invoice(invoice_id)
    if invoice is None or invoice["bot_id"] != bot_id:
        return web.Response(text="Счёт не найден", status=404)

    creds = await get_bot_cloudpayments_credentials(bot_id)
    if creds is None:
        logger.error(f"pay_page_handler: no cloudpayments credentials for bot_id={bot_id}")
        return web.Response(text="Оплата временно недоступна", status=503)
    public_id, _api_secret = creds

    if invoice["status"] == "paid":
        return web.Response(text=_render_success_page(invoice), content_type="text/html")

    return web.Response(text=_render_checkout_page(invoice, invoice_id, public_id), content_type="text/html")


def _render_checkout_page(invoice: dict, invoice_id: int, public_id: str) -> str:
    title = html.escape(invoice["title"])
    description = html.escape(invoice["description"])
    amount = invoice["amount"] / 100
    currency = html.escape(invoice["currency"])
    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Оплата — {title}</title>
<script src="https://widget.cloudpayments.ru/bundles/cloudpayments.js"></script>
<style>
  body {{ font-family: -apple-system, sans-serif; background:#0f1115; color:#eef0f4;
          display:flex; align-items:center; justify-content:center; min-height:100vh; margin:0; }}
  .card {{ background:#171a21; border:1px solid #262a34; border-radius:16px; padding:28px;
           max-width:360px; width:100%; }}
  .amount {{ font-size:34px; font-weight:700; margin:8px 0 20px; }}
  button {{ width:100%; background:#6d8dff; color:#0b0d12; border:none; border-radius:10px;
            padding:14px; font-size:15px; font-weight:600; cursor:pointer; }}
</style>
</head>
<body>
  <div class="card">
    <p>{title}</p>
    <p style="color:#8b93a3; font-size:13px;">{description}</p>
    <div class="amount">{amount:.2f} {currency}</div>
    <button id="pay-btn">Оплатить</button>
  </div>
  <script>
    document.getElementById('pay-btn').addEventListener('click', function () {{
      var widget = new cp.CloudPayments();
      widget.pay('charge', {{
        publicId: {public_id!r},
        description: {description!r},
        amount: {amount},
        currency: {currency!r},
        invoiceId: 'cp-invoice-{invoice_id}',
        skin: 'mini'
      }}, {{
        onSuccess: function () {{ window.location.reload(); }},
      }});
    }});
  </script>
</body>
</html>"""


def _render_success_page(invoice: dict) -> str:
    title = html.escape(invoice["title"])
    amount = invoice["amount"] / 100
    currency = html.escape(invoice["currency"])
    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Оплачено — {title}</title>
<style>
  body {{ font-family: -apple-system, sans-serif; background:#0f1115; color:#eef0f4;
          display:flex; align-items:center; justify-content:center; min-height:100vh; margin:0; text-align:center; }}
  .card {{ background:#171a21; border:1px solid #262a34; border-radius:16px; padding:32px;
           max-width:360px; width:100%; }}
  .amount {{ font-size:22px; font-weight:700; color:#3dd68c; margin:10px 0 4px; }}
</style>
</head>
<body>
  <div class="card">
    <p>✅ Оплата прошла успешно</p>
    <div class="amount">{amount:.2f} {currency}</div>
    <p style="color:#8b93a3; font-size:13px;">Подтверждение отправлено в чат с ботом. Можно вернуться в Telegram.</p>
  </div>
</body>
</html>"""


async def webhook_handler(request: web.Request) -> web.Response:
    """Cloudpayments POSTs application/x-www-form-urlencoded with an
    InvoiceId field — we embed it as 'cp-invoice-{our invoice_id}' in the
    widget call above precisely so this handler can recover our own
    invoice_id without a separate lookup table."""
    raw_body = await request.read()
    form = await request.post()

    invoice_field = str(form.get("InvoiceId", ""))
    if not invoice_field.startswith("cp-invoice-") or not invoice_field[len("cp-invoice-"):].isdigit():
        logger.warning(f"webhook_handler: malformed InvoiceId={invoice_field!r}")
        return web.json_response({"code": 13}, status=400)
    invoice_id = int(invoice_field[len("cp-invoice-"):])

    invoice = await get_cloudpayments_invoice(invoice_id)
    if invoice is None:
        return web.json_response({"code": 13}, status=404)

    creds = await get_bot_cloudpayments_credentials(invoice["bot_id"])
    if creds is None:
        logger.error(f"webhook_handler: no credentials for bot_id={invoice['bot_id']}")
        return web.json_response({"code": 13}, status=403)
    _public_id, api_secret = creds

    if not _verify_signature(raw_body, api_secret, request.headers.get("Content-HMAC", "")):
        logger.warning(f"webhook_handler: bad signature for invoice_id={invoice_id}")
        return web.json_response({"code": 13}, status=403)

    transaction_id = str(form.get("TransactionId", ""))
    card_last_four = str(form.get("CardLastFour", ""))
    updated = await mark_cloudpayments_invoice_paid(invoice_id, transaction_id, card_last_four)
    if updated:
        await _credit_and_notify(invoice, transaction_id)
    else:
        logger.info(f"webhook_handler: invoice_id={invoice_id} already paid or unknown, ignoring redelivery")

    return web.json_response({"code": 0})


def _verify_signature(raw_body: bytes, api_secret: str, header_value: str) -> bool:
    if not header_value:
        return False
    expected = base64.b64encode(hmac.new(api_secret.encode(), raw_body, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(expected, header_value)


async def _credit_and_notify(invoice: dict, transaction_id: str) -> None:
    """Mirrors features/payments.py's on_successful_payment: records into the
    bot's OWN per-bot `payments` table (same schema/table — so orders_tracker
    reports, refund tooling, and OFFICES_DESIGN office_events all see a
    Cloudpayments sale exactly like a ЮKassa one) and sends a Telegram
    confirmation message. Needs a live Registry entry to reach both the
    bot's db_path and its aiogram Bot instance — best-effort: if the bot
    isn't currently loaded in this process's registry, the invoice is still
    marked 'paid' in the central DB (webhook already returned code:0 to
    Cloudpayments), just without the per-bot payments-table row or chat
    notification. Logged loudly so it's not a silent gap."""
    import aiosqlite

    from features.office_events import OrderCreatedEvent, publish_event

    registry = _registry_handle.value
    if registry is None:
        logger.error(f"_credit_and_notify: no live Registry — cannot credit/notify bot_id={invoice['bot_id']}")
        return
    entry = registry.get(invoice["bot_id"])
    if entry is None:
        logger.error(f"_credit_and_notify: bot_id={invoice['bot_id']} not in registry — cannot credit/notify")
        return

    db_path = entry.config.get("db_path") if isinstance(entry.config, dict) else None
    payments_row_id = None
    if db_path:
        async with aiosqlite.connect(db_path) as db:
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
                        f"cp-{transaction_id}",
                        transaction_id,
                        invoice["chat_id"],
                        invoice["invoice_payload"],
                        invoice["currency"],
                        invoice["amount"],
                    ),
                )
                await db.commit()
                payments_row_id = cursor.lastrowid
            except aiosqlite.IntegrityError:
                logger.info(f"_credit_and_notify: duplicate transaction_id={transaction_id}, payments row already exists")

    try:
        await entry.bot.send_message(
            chat_id=invoice["chat_id"],
            text=f"✅ Оплата получена: {invoice['title']}. Спасибо!",
        )
    except Exception:
        logger.exception(f"_credit_and_notify: failed to notify chat_id={invoice['chat_id']}")

    if payments_row_id is not None:
        try:
            await publish_event(
                invoice["bot_id"],
                "order.created",
                OrderCreatedEvent(
                    order_id=payments_row_id,
                    amount=invoice["amount"],
                    currency=invoice["currency"],
                    customer_chat_id=invoice["chat_id"],
                ),
            )
        except Exception:
            logger.exception(f"_credit_and_notify: publish_event(order.created) raised for bot_id={invoice['bot_id']}")


def register_routes(app: web.Application) -> None:
    app.router.add_get("/pay/{bot_id}/{invoice_id}", pay_page_handler)
    app.router.add_post("/webhook/cloudpayments", webhook_handler)
