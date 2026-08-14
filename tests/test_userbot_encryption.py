"""db/database.py — userbot session encryption (_encrypt_session/_decrypt_session).

Same pattern as bot-token encryption but with a SEPARATE key
(USERBOT_ENCRYPTION_KEY, not ENCRYPTION_KEY) — see
docs/USERBOT_CHANNEL_MONITOR_DESIGN.md §1.
"""
from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

import db.database as db_module
from db.database import _decrypt_session, _encrypt_session, _get_userbot_fernet


def test_encrypt_decrypt_roundtrip(userbot_key):
    plaintext = "1BVtsOKAB...fake-telethon-string-session...xyz"
    encrypted = _encrypt_session(plaintext)
    assert encrypted != plaintext
    assert _decrypt_session(encrypted) == plaintext


def test_encrypt_none_passthrough(userbot_key):
    assert _encrypt_session(None) is None
    assert _encrypt_session("") == ""


def test_decrypt_none_passthrough(userbot_key):
    assert _decrypt_session(None) is None
    assert _decrypt_session("") == ""


def test_missing_key_raises_clear_error(monkeypatch):
    monkeypatch.delenv("USERBOT_ENCRYPTION_KEY", raising=False)
    monkeypatch.setattr(db_module, "_userbot_fernet", None)
    monkeypatch.setattr(db_module, "_userbot_fernet_key_used", None)
    with pytest.raises(ValueError, match="USERBOT_ENCRYPTION_KEY is not set"):
        _get_userbot_fernet()


def test_invalid_key_raises_clear_error(monkeypatch):
    monkeypatch.setenv("USERBOT_ENCRYPTION_KEY", "not-a-valid-fernet-key")
    monkeypatch.setattr(db_module, "_userbot_fernet", None)
    monkeypatch.setattr(db_module, "_userbot_fernet_key_used", None)
    with pytest.raises(ValueError, match="USERBOT_ENCRYPTION_KEY is invalid"):
        _get_userbot_fernet()


def test_decrypt_with_rotated_key_returns_none_not_plaintext(monkeypatch):
    """A ciphertext encrypted under one key must not be decryptable (or
    silently pass through) under a different key — the rotated-key case
    returns None (unlike bot tokens, which fall back to returning the raw
    blob; userbot sessions are treated as unrecoverable rather than risking
    exposing an undecryptable blob that might look like session material)."""
    key_a = Fernet.generate_key().decode()
    monkeypatch.setenv("USERBOT_ENCRYPTION_KEY", key_a)
    monkeypatch.setattr(db_module, "_userbot_fernet", None)
    monkeypatch.setattr(db_module, "_userbot_fernet_key_used", None)
    encrypted = _encrypt_session("secret-session-string")

    key_b = Fernet.generate_key().decode()
    monkeypatch.setenv("USERBOT_ENCRYPTION_KEY", key_b)
    monkeypatch.setattr(db_module, "_userbot_fernet", None)
    monkeypatch.setattr(db_module, "_userbot_fernet_key_used", None)
    assert _decrypt_session(encrypted) is None


def test_userbot_encryption_key_independent_of_bot_encryption_key(userbot_key):
    """Encrypting the same plaintext with _encrypt_session (USERBOT_ENCRYPTION_KEY)
    vs _encrypt_token (ENCRYPTION_KEY) must not be interchangeable — decrypting
    a userbot-encrypted value with the bot-token Fernet instance must fail."""
    from db.database import _fernet as bot_fernet
    from cryptography.fernet import InvalidToken

    encrypted = _encrypt_session("secret-session-string")
    with pytest.raises(InvalidToken):
        bot_fernet.decrypt(encrypted.encode())
