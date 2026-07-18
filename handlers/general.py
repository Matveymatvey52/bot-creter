from __future__ import annotations

import tempfile
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.types import Message

from config import ASSEMBLYAI_API_KEY
from db.database import get_all_bots
from handlers.admin_manager import _is_owner
from services.claude_service import ask_assistant
from services.voice_service import transcribe_voice

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

    bots = await get_all_bots()
    try:
        answer = await ask_assistant(text, _bots_summary(bots))
    except Exception:
        answer = "Что-то пошло не так, попробуй ещё раз."

    try:
        await status.delete()
    except Exception:
        pass

    await message.answer(f"🎤 <i>{text}</i>", parse_mode="HTML")
    await message.answer(answer)


@router.message(StateFilter(None), F.text, ~F.text.startswith("/"))
async def general_text(message: Message):
    if not _is_owner(message.from_user.id):
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
