from __future__ import annotations

import asyncio

import assemblyai as aai

from config import ASSEMBLYAI_API_KEY

if ASSEMBLYAI_API_KEY:
    aai.settings.api_key = ASSEMBLYAI_API_KEY


def _transcribe_sync(file_path: str) -> str:
    config = aai.TranscriptionConfig(language_code="ru")
    transcriber = aai.Transcriber(config=config)
    transcript = transcriber.transcribe(file_path)
    if transcript.status == aai.TranscriptStatus.error:
        raise RuntimeError(transcript.error)
    return transcript.text


async def transcribe_voice(file_path: str) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _transcribe_sync, file_path)
