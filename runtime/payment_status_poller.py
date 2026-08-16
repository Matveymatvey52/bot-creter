from __future__ import annotations

import asyncio
import json
import logging

from db.database import (
    get_all_bot_ids_with_yookassa_credentials,
    get_bot_yookassa_credentials,
    set_bot_yookassa_status_cache,
)
from services.yookassa_api import YooKassaAuthError, fetch_shop_info

logger = logging.getLogger(__name__)

# 30 min — status changes (moderation approval) happen on ЮKassa's own timescale
# (hours/days), not seconds; no need to poll faster than the "Проверить статус"
# button already covers on-demand checks.
POLL_INTERVAL_SECONDS = 1800


async def _poll_once() -> None:
    bot_ids = await get_all_bot_ids_with_yookassa_credentials()
    for bot_id in bot_ids:
        creds = await get_bot_yookassa_credentials(bot_id)
        if not creds:
            continue
        shop_id, secret_key = creds
        try:
            info = await fetch_shop_info(shop_id, secret_key)
        except YooKassaAuthError:
            logger.warning(f"payment_status_poller: shop_id/secret_key rejected for bot_id={bot_id}")
            continue
        except Exception as e:
            logger.warning(f"payment_status_poller: GET /me failed for bot_id={bot_id}: {type(e).__name__}: {e}")
            continue
        await set_bot_yookassa_status_cache(
            bot_id, str(info.get("status", "unknown")), json.dumps(info.get("payment_methods", []))
        )


async def payment_status_poll_loop() -> None:
    """Background task, same shape as runtime/userbot_worker.py's _summarize_loop:
    periodically refreshes the cached ЮKassa status/payment_methods for every bot
    that has API credentials on file, so the owner doesn't have to press
    'Проверить статус' manually to notice a shop went from pending to enabled."""
    while True:
        try:
            await _poll_once()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"payment_status_poll_loop: sweep failed: {type(e).__name__}: {e}")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
