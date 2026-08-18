"""Shared pytest fixtures for channel_monitor tests.

isolated_db returns a tmp_path-backed sqlite file path for channel_monitor's
tables (userbot_sessions / monitored_channels / channel_posts) — these live in
a per-bot db_path now (docs/USERBOT_FEATURE_DESIGN.md §2 Variant A), passed
explicitly to every db/database.py channel_monitor function, so this fixture
just hands back a throwaway path instead of monkeypatching a module-level
DB_PATH the way the old central-DB design needed. See MEMORY.md "Backlog:
test DB isolation" for why isolation matters (existing suite has some tests
that hit the real DB; new tests in this module must not add to that list).
"""
from __future__ import annotations

import pytest

import db.database as db_module


@pytest.fixture
def isolated_db(tmp_path):
    return str(tmp_path / "test_channel_monitor.db")


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
