from __future__ import annotations

import base64
import logging
import os

import aiohttp

logger = logging.getLogger(__name__)

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "")  # "username/repo-name"
_API = "https://api.github.com"


async def push_bot_to_github(bot_name: str, code: str) -> bool:
    if not GITHUB_TOKEN or not GITHUB_REPO:
        logger.info("GITHUB_TOKEN/GITHUB_REPO not set — skipping GitHub sync")
        return False

    path = f"bots/{bot_name}.py"
    url = f"{_API}/repos/{GITHUB_REPO}/contents/{path}"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    content_b64 = base64.b64encode(code.encode()).decode()

    async with aiohttp.ClientSession() as session:
        sha: str | None = None
        async with session.get(url, headers=headers) as resp:
            if resp.status == 200:
                data = await resp.json()
                sha = data.get("sha")

        body: dict = {
            "message": f"{'Update' if sha else 'Add'} bot: {bot_name}",
            "content": content_b64,
        }
        if sha:
            body["sha"] = sha

        async with session.put(url, headers=headers, json=body) as resp:
            if resp.status in (200, 201):
                logger.info(f"GitHub sync: pushed bots/{bot_name}.py to {GITHUB_REPO}")
                return True
            error = await resp.text()
            logger.error(f"GitHub sync failed ({resp.status}): {error[:300]}")
            return False
