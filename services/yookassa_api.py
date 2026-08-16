from __future__ import annotations

import logging

import aiohttp

logger = logging.getLogger(__name__)

_ME_URL = "https://api.yookassa.ru/v3/me"


class YooKassaAuthError(Exception):
    """shop_id/secret_key rejected by ЮKassa (401/403) — credentials are wrong, not a network blip."""


async def fetch_shop_info(shop_id: str, secret_key: str) -> dict:
    """Calls GET /me with HTTP Basic Auth (shop_id as username, secret_key as password) and
    returns the parsed JSON: account_id, status, test, payment_methods, fiscalization, itn.
    Raises YooKassaAuthError on 401/403 (bad credentials) and RuntimeError on any other
    non-2xx response or malformed body — callers decide how to present these to the owner.
    """
    async with aiohttp.ClientSession() as session:
        async with session.get(
            _ME_URL,
            auth=aiohttp.BasicAuth(shop_id, secret_key),
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status in (401, 403):
                raise YooKassaAuthError("ЮKassa отклонила shopId/secret key")
            if resp.status != 200:
                body = await resp.text()
                logger.warning(f"yookassa GET /me failed: status={resp.status} body={body[:300]}")
                raise RuntimeError(f"ЮKassa API вернул статус {resp.status}")
            data = await resp.json()

    if not isinstance(data, dict) or "account_id" not in data:
        raise RuntimeError(f"Unexpected ЮKassa /me response shape: {data}")
    return data
