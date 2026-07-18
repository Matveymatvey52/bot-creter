from __future__ import annotations

import logging

import aiohttp

logger = logging.getLogger(__name__)


async def get_managed_bot_token(manager_token: str, bot_id: int) -> str:
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"https://api.telegram.org/bot{manager_token}/getManagedBotToken",
            json={"user_id": bot_id},
        ) as resp:
            data = await resp.json()

    logger.info(f"getManagedBotToken response: ok={data.get('ok')}")

    if not data.get("ok"):
        raise RuntimeError(data.get("description", str(data)))

    result = data["result"]
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        token = result.get("token") or result.get("access_token")
        if token:
            return token
    raise RuntimeError(f"Unexpected getManagedBotToken result: {result}")
