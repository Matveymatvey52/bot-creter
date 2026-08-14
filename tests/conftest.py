"""Shared pytest fixtures for channel_monitor tests.

isolated_db patches db.database.DB_PATH to a tmp_path-backed sqlite file for
the duration of one test, so tests exercise userbot_sessions /
monitored_channels / channel_posts against a throwaway database instead of
the real data/bots.db — see MEMORY.md "Backlog: test DB isolation" for why
this matters (existing suite has some tests that hit the real DB; new tests
in this module must not add to that list).
"""
from __future__ import annotations

import pytest

import db.database as db_module


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test_channel_monitor.db"
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    return db_path


@pytest.fixture
def userbot_key(monkeypatch):
    """A valid Fernet key for USERBOT_ENCRYPTION_KEY, set for the duration of
    one test, and the module's cached Fernet instance reset so a key set (or
    changed) mid-test-session is always picked up."""
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode()
    monkeypatch.setenv("USERBOT_ENCRYPTION_KEY", key)
    monkeypatch.setattr(db_module, "_userbot_fernet", None)
    monkeypatch.setattr(db_module, "_userbot_fernet_key_used", None)
    return key
