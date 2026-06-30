from __future__ import annotations

import asyncio
import logging
import os
import sys

logger = logging.getLogger(__name__)

_processes: dict = {}


async def start_bot(bot_id: int, file_path: str, token: str) -> int:
    env = os.environ.copy()
    env["BOT_TOKEN"] = token

    process = await asyncio.create_subprocess_exec(
        sys.executable,
        file_path,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _processes[bot_id] = process
    logger.info(f"Bot {bot_id} started with PID {process.pid}")
    return process.pid


async def stop_bot(bot_id: int) -> bool:
    process = _processes.get(bot_id)
    if not process:
        return False
    try:
        process.terminate()
        await asyncio.wait_for(process.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        process.kill()
    _processes.pop(bot_id, None)
    logger.info(f"Bot {bot_id} stopped")
    return True


def is_running(bot_id: int) -> bool:
    process = _processes.get(bot_id)
    return process is not None and process.returncode is None
