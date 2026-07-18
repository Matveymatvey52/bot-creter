from __future__ import annotations

import asyncio
import logging
import os
import sys

logger = logging.getLogger(__name__)

_processes: dict[int, asyncio.subprocess.Process] = {}
_last_errors: dict[int, str] = {}
_stderr_tasks: dict[int, asyncio.Task] = {}
_locks: dict[int, asyncio.Lock] = {}


def _get_lock(bot_id: int) -> asyncio.Lock:
    return _locks.setdefault(bot_id, asyncio.Lock())


async def _collect_stderr(bot_id: int, process: asyncio.subprocess.Process) -> None:
    try:
        data = await process.stderr.read()  # read until EOF to prevent pipe buffer deadlock
        if data:
            _last_errors[bot_id] = data.decode(errors="replace").strip()
    except Exception:
        pass


async def start_bot(bot_id: int, file_path: str, token: str, extra_env: dict | None = None) -> int:
    async with _get_lock(bot_id):
        if is_running(bot_id):
            await _stop_bot_unlocked(bot_id)

        env = os.environ.copy()
        env["BOT_TOKEN"] = token
        if extra_env:
            env.update(extra_env)

        process = await asyncio.create_subprocess_exec(
            sys.executable,
            file_path,
            env=env,
            stdout=asyncio.subprocess.DEVNULL,  # never read, avoid pipe buffer deadlock
            stderr=asyncio.subprocess.PIPE,
        )
        _processes[bot_id] = process

        # Check if the process crashes within 2 seconds (no asyncio.shield — it leaks coroutines)
        try:
            await asyncio.wait_for(process.wait(), timeout=2.0)
            # Process already exited — it crashed
            stderr_data = await process.stderr.read()
            error_text = stderr_data.decode(errors="replace").strip()
            _last_errors[bot_id] = error_text
            _processes.pop(bot_id, None)
            short = error_text[-600:] if len(error_text) > 600 else error_text
            raise RuntimeError(short or "бот завершился сразу после запуска")
        except asyncio.TimeoutError:
            # Still running after 2 seconds — good; start background stderr drain
            task = asyncio.create_task(_collect_stderr(bot_id, process))
            _stderr_tasks[bot_id] = task  # keep reference to avoid GC and suppress warnings

        logger.info(f"Bot {bot_id} started with PID {process.pid}")
        return process.pid


async def _stop_bot_unlocked(bot_id: int) -> bool:
    process = _processes.get(bot_id)
    if not process:
        return False
    try:
        process.terminate()
        await asyncio.wait_for(process.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        process.kill()
    _processes.pop(bot_id, None)
    task = _stderr_tasks.pop(bot_id, None)
    if task and not task.done():
        task.cancel()
    logger.info(f"Bot {bot_id} stopped")
    return True


async def stop_bot(bot_id: int) -> bool:
    async with _get_lock(bot_id):
        return await _stop_bot_unlocked(bot_id)


def _make_extra_env(bot: dict) -> dict | None:
    extra = {}
    if bot.get("display_name"):
        extra["BOT_DISPLAY_NAME"] = bot["display_name"]
    if bot.get("group_chat_id"):
        extra["GROUP_CHAT_ID"] = bot["group_chat_id"]
    return extra or None


def is_running(bot_id: int) -> bool:
    process = _processes.get(bot_id)
    return process is not None and process.returncode is None


def get_bot_logs(bot_id: int) -> str | None:
    return _last_errors.get(bot_id)
