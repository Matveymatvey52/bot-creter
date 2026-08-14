"""services/gemini_service.py — summarize_post happy path and graceful
degradation. genai.Client is never actually constructed with network access —
services.gemini_service._get_client is patched to return a fake client whose
.aio.models.generate_content is an AsyncMock, per design doc §5 (only post
text ever reaches this module; no real Gemini API calls in tests).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import services.gemini_service as gemini_service

pytestmark = pytest.mark.asyncio


def _fake_client(response_text="A short summary.", raises=None):
    client = SimpleNamespace()
    client.aio = SimpleNamespace(models=SimpleNamespace())
    if raises:
        client.aio.models.generate_content = AsyncMock(side_effect=raises)
    else:
        client.aio.models.generate_content = AsyncMock(return_value=SimpleNamespace(text=response_text))
    return client


async def test_summarize_post_happy_path(monkeypatch):
    monkeypatch.setattr(gemini_service, "GEMINI_API_KEY", "fake-key")
    monkeypatch.setattr(gemini_service, "_client", None)
    monkeypatch.setattr(gemini_service, "_get_client", lambda: _fake_client("Кратко: пост про котиков."))

    result = await gemini_service.summarize_post("Длинный пост про котиков...")
    assert result == "Кратко: пост про котиков."


async def test_summarize_post_empty_text_returns_none(monkeypatch):
    monkeypatch.setattr(gemini_service, "GEMINI_API_KEY", "fake-key")
    assert await gemini_service.summarize_post("") is None
    assert await gemini_service.summarize_post("   ") is None
    assert await gemini_service.summarize_post(None) is None  # type: ignore[arg-type]


async def test_summarize_post_no_api_key_degrades_to_none(monkeypatch):
    monkeypatch.setattr(gemini_service, "GEMINI_API_KEY", None)
    result = await gemini_service.summarize_post("some post text")
    assert result is None


async def test_summarize_post_api_error_degrades_to_none(monkeypatch):
    monkeypatch.setattr(gemini_service, "GEMINI_API_KEY", "fake-key")
    monkeypatch.setattr(gemini_service, "_client", None)
    monkeypatch.setattr(
        gemini_service, "_get_client", lambda: _fake_client(raises=RuntimeError("quota exceeded"))
    )

    result = await gemini_service.summarize_post("some post text")
    assert result is None


async def test_summarize_post_timeout_degrades_to_none(monkeypatch):
    import asyncio

    async def _hang(*args, **kwargs):
        await asyncio.sleep(10)

    client = SimpleNamespace()
    client.aio = SimpleNamespace(models=SimpleNamespace(generate_content=_hang))

    monkeypatch.setattr(gemini_service, "GEMINI_API_KEY", "fake-key")
    monkeypatch.setattr(gemini_service, "_client", None)
    monkeypatch.setattr(gemini_service, "_get_client", lambda: client)
    monkeypatch.setattr(gemini_service, "_TIMEOUT_SECONDS", 0.05)

    result = await gemini_service.summarize_post("some post text")
    assert result is None


async def test_summarize_post_empty_response_degrades_to_none(monkeypatch):
    monkeypatch.setattr(gemini_service, "GEMINI_API_KEY", "fake-key")
    monkeypatch.setattr(gemini_service, "_client", None)
    monkeypatch.setattr(gemini_service, "_get_client", lambda: _fake_client(response_text=None))

    result = await gemini_service.summarize_post("some post text")
    assert result is None


async def test_summarize_post_never_receives_more_than_text(monkeypatch):
    """Structural check on the contract from design doc §5: summarize_post's
    signature accepts nothing but the post text — this test documents/locks
    that in, since it's a security-relevant boundary."""
    import inspect
    sig = inspect.signature(gemini_service.summarize_post)
    assert list(sig.parameters.keys()) == ["text"]
