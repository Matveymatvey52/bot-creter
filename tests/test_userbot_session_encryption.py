"""db/database.py — userbot session encryption (_encrypt_session/_decrypt_session).

Covers the Fernet round-trip itself, and the "explicit failure" behavior when
USERBOT_ENCRYPTION_KEY is missing or gets rotated mid-flight — mirrors the
existing behavior of ENCRYPTION_KEY (_encrypt_token/_decrypt_token) but as a
SEPARATE key, per docs/USERBOT_CHANNEL_MONITOR_DESIGN.md §1.
"""
from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

import db.database as db_module


def test_encrypt_decrypt_roundtrip(userbot_key):
    encrypted = db_module._encrypt_session("a-real-looking-telethon-session-string")
    assert encrypted != "a-real-looking-telethon-session-string"
    assert db_module._decrypt_session(encrypted) == "a-real-looking-telethon-session-string"


def test_encrypt_session_none_passthrough(userbot_key):
    assert db_module._encrypt_session(None) is None
    assert db_module._encrypt_session("") == ""


def test_decrypt_session_none_passthrough(userbot_key):
    assert db_module._decrypt_session(None) is None
    assert db_module._decrypt_session("") == ""


def test_missing_key_raises_explicit_error(monkeypatch):
    monkeypatch.delenv("USERBOT_ENCRYPTION_KEY", raising=False)
    monkeypatch.setattr(db_module, "_userbot_fernet", None)
    monkeypatch.setattr(db_module, "_userbot_fernet_key_used", None)
    with pytest.raises(ValueError, match="USERBOT_ENCRYPTION_KEY is not set"):
        db_module._encrypt_session("some-session-string")


def test_invalid_key_raises_explicit_error(monkeypatch):
    monkeypatch.setenv("USERBOT_ENCRYPTION_KEY", "not-a-valid-fernet-key")
    monkeypatch.setattr(db_module, "_userbot_fernet", None)
    monkeypatch.setattr(db_module, "_userbot_fernet_key_used", None)
    with pytest.raises(ValueError, match="USERBOT_ENCRYPTION_KEY is invalid"):
        db_module._encrypt_session("some-session-string")


def test_key_rotation_makes_old_ciphertext_undecryptable(monkeypatch):
    """Same "unrecoverable, degrade to None, never crash the caller" shape as
    _decrypt_token — but for sessions, None (not the original ciphertext) is
    returned, since callers must treat an undecryptable session exactly like
    "no session" (there's no plaintext-migration case to preserve here)."""
    key1 = Fernet.generate_key().decode()
    monkeypatch.setenv("USERBOT_ENCRYPTION_KEY", key1)
    monkeypatch.setattr(db_module, "_userbot_fernet", None)
    monkeypatch.setattr(db_module, "_userbot_fernet_key_used", None)
    encrypted = db_module._encrypt_session("session-under-key-one")

    key2 = Fernet.generate_key().decode()
    monkeypatch.setenv("USERBOT_ENCRYPTION_KEY", key2)
    monkeypatch.setattr(db_module, "_userbot_fernet", None)
    monkeypatch.setattr(db_module, "_userbot_fernet_key_used", None)
    assert db_module._decrypt_session(encrypted) is None


def test_userbot_key_never_reused_for_bot_tokens(userbot_key):
    """ENCRYPTION_KEY (bots.token) and USERBOT_ENCRYPTION_KEY are separate
    Fernet instances — ciphertext produced under one must not decrypt under
    the other."""
    session_ciphertext = db_module._encrypt_session("a-session-string")
    # _fernet is the bots.token instance, built from ENCRYPTION_KEY at import
    # time — decrypting session ciphertext with it must fail, not succeed.
    from cryptography.fernet import InvalidToken
    with pytest.raises(InvalidToken):
        db_module._fernet.decrypt(session_ciphertext.encode())
