from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.types import Message

from config import ASSEMBLYAI_API_KEY
from db.database import get_all_bots
from handlers.admin_manager import _is_owner
from handlers.feature_connect import maybe_handle_connect_request
from services.claude_service import ask_assistant
from services.voice_service import transcribe_voice

logger = logging.getLogger(__name__)

router = Router()


def _bots_summary(bots: list[dict]) -> str:
    if not bots:
        return "ботов пока нет"
    return "\n".join(
        f"- {b['name']}" + (f" ({b['display_name']})" if b.get("display_name") else "")
        for b in bots
    )


@router.message(StateFilter(None), F.voice)
async def general_voice(message: Message, bot: Bot):
    if not _is_owner(message.from_user.id):
        return
    if not ASSEMBLYAI_API_KEY:
        await message.answer("⚠️ Голосовые не настроены. Напиши текстом.")
        return

    status = await message.answer("🎤 Распознаю и думаю...")

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        f = await bot.get_file(message.voice.file_id)
        await bot.download_file(f.file_path, destination=tmp_path)
        text = await transcribe_voice(tmp_path)
    except Exception:
        try:
            await status.delete()
        except Exception:
            pass
        await message.answer("Не удалось распознать, попробуй текстом.")
        return
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    if not text.strip():
        try:
            await status.delete()
        except Exception:
            pass
        await message.answer("Не удалось разобрать голосовое, попробуй ещё раз.")
        return

    try:
        await status.delete()
    except Exception:
        pass
    await message.answer(f"🎤 <i>{text}</i>", parse_mode="HTML")

    # Same recognized-feature-connect-request check as general_text below —
    # the voice path is just transcription feeding the identical text pipeline
    # (see handlers/feature_connect.py's maybe_handle_connect_request). Wrapped
    # in the same graceful-fallback shape as ask_assistant() below — this call
    # touches the DB (get_all_bots/get_bot/discover_features/...) and can send
    # messages of its own, either of which can raise (e.g. "database is
    # locked", TelegramForbiddenError) — an unhandled exception here must not
    # leave the owner with zero response, the one thing ask_assistant's own
    # try/except already guarantees never happens.
    try:
        handled = await maybe_handle_connect_request(message, text)
    except Exception as e:
        logger.error(f"maybe_handle_connect_request failed: {e}")
        await message.answer("Что-то пошло не так, попробуй ещё раз.")
        return
    if handled:
        return

    bots = await get_all_bots()
    try:
        answer = await ask_assistant(text, _bots_summary(bots))
    except Exception:
        answer = "Что-то пошло не так, попробуй ещё раз."
    await message.answer(answer)


@router.message(StateFilter(None), F.text, ~F.text.startswith("/"))
async def general_text(message: Message):
    if not _is_owner(message.from_user.id):
        return

    # Cheap pre-filter + narrow classifier live in handlers/feature_connect.py
    # — most messages never reach the classifier at all (cost control), and
    # anything not confidently a "connect an existing feature" request falls
    # straight through to ask_assistant below, unchanged from before this hook
    # existed. Wrapped in the same graceful-fallback shape ask_assistant's own
    # try/except uses below — see the matching comment in general_voice above.
    try:
        handled = await maybe_handle_connect_request(message, message.text)
    except Exception as e:
        logger.error(f"maybe_handle_connect_request failed: {e}")
        await message.answer("Что-то пошло не так, попробуй ещё раз.")
        return
    if handled:
        return

    thinking = await message.answer("⏳")
    bots = await get_all_bots()
    try:
        answer = await ask_assistant(message.text, _bots_summary(bots))
    except Exception:
        answer = "Что-то пошло не так, попробуй ещё раз."
    try:
        await thinking.delete()
    except Exception:
        pass
    await message.answer(answer)
