from __future__ import annotations

import json
import logging
import re
import aiosqlite
from cryptography.fernet import Fernet, InvalidToken
from config import DATA_DIR, ENCRYPTION_KEY

_PLAINTEXT_TOKEN_RE = re.compile(r"^\d+:")

logger = logging.getLogger(__name__)

DB_PATH = DATA_DIR / "bots.db"
ADMINS_FILE = DATA_DIR / "admins.json"

if not ENCRYPTION_KEY:
    raise ValueError("ENCRYPTION_KEY is not set in .env")
try:
    _fernet = Fernet(ENCRYPTION_KEY.encode())
except (ValueError, TypeError) as e:
    raise ValueError(
        "ENCRYPTION_KEY is invalid. Generate one with: "
        "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
    ) from e


def _encrypt_token(token: str | None) -> str | None:
    if not token:
        return token
    return _fernet.encrypt(token.encode()).decode()


def _decrypt_token(token: str | None) -> str | None:
    if not token:
        return token
    try:
        return _fernet.decrypt(token.encode()).decode()
    except (InvalidToken, AttributeError, TypeError, ValueError):
        # Either not yet migrated (plaintext, first run — migrate_encrypt_tokens() will fix it)
        # or ENCRYPTION_KEY was rotated/changed since this token was encrypted — in that case
        # the original token is unrecoverable and this returns the undecryptable blob as-is.
        logger.warning("Could not decrypt a bot token — plaintext (pre-migration) or ENCRYPTION_KEY changed")
        return token


def _decrypt_row(row: dict) -> dict:
    if "token" in row:
        row["token"] = _decrypt_token(row["token"])
    return row


async def migrate_encrypt_tokens() -> None:
    """One-time migration: encrypt any plaintext tokens left over from before encryption was added.

    Plaintext is detected by Telegram bot token shape (^\\d+:...), not by "fails to decrypt" —
    a token that fails to decrypt with the current key could just as easily be ciphertext from a
    since-rotated ENCRYPTION_KEY, and re-encrypting that would destroy it irrecoverably.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT id, token FROM bots WHERE token IS NOT NULL AND token != ''") as cursor:
            rows = await cursor.fetchall()
        migrated = 0
        for row in rows:
            token = row["token"]
            if _PLAINTEXT_TOKEN_RE.match(token):
                encrypted = _fernet.encrypt(token.encode()).decode()
                await db.execute("UPDATE bots SET token = ? WHERE id = ?", (encrypted, row["id"]))
                migrated += 1
                continue
            try:
                _fernet.decrypt(token.encode())
                # already encrypted with the current key — leave as-is
            except (InvalidToken, AttributeError, TypeError, ValueError):
                logger.warning(
                    f"migrate_encrypt_tokens: bot id={row['id']} token is neither plaintext nor "
                    "decryptable with current ENCRYPTION_KEY — leaving untouched"
                )
        await db.commit()
        if migrated:
            logger.info(f"migrate_encrypt_tokens: encrypted {migrated} plaintext token(s)")


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                username TEXT,
                description TEXT,
                token TEXT,
                file_path TEXT,
                status TEXT DEFAULT 'stopped',
                pid INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bot_admins (
                bot_id INTEGER NOT NULL,
                telegram_id TEXT NOT NULL,
                PRIMARY KEY (bot_id, telegram_id)
            )
        """)
        for col in ("display_name TEXT", "group_chat_id TEXT"):
            try:
                await db.execute(f"ALTER TABLE bots ADD COLUMN {col}")
            except aiosqlite.OperationalError:
                pass
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bot_payment_providers (
                bot_id         INTEGER PRIMARY KEY REFERENCES bots(id),
                provider_token TEXT NOT NULL,
                created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bot_features (
                bot_id       INTEGER NOT NULL REFERENCES bots(id),
                feature_name TEXT NOT NULL,
                enabled_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (bot_id, feature_name)
            )
        """)
        await db.commit()
    await migrate_encrypt_tokens()


async def add_bot_admin(bot_id: int, telegram_id: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO bot_admins (bot_id, telegram_id) VALUES (?, ?)",
            (bot_id, telegram_id),
        )
        await db.commit()
    await sync_bot_admins_json(bot_id)


async def remove_bot_admin(bot_id: int, telegram_id: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM bot_admins WHERE bot_id = ? AND telegram_id = ?",
            (bot_id, telegram_id),
        )
        await db.commit()
    await sync_bot_admins_json(bot_id)


async def get_bot_admins(bot_id: int) -> list[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT telegram_id FROM bot_admins WHERE bot_id = ?", (bot_id,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [row[0] for row in rows]


async def set_bot_group(bot_id: int, group_chat_id: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE bots SET group_chat_id = ? WHERE id = ?", (group_chat_id, bot_id)
        )
        await db.commit()


async def set_bot_display_name(bot_id: int, display_name: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE bots SET display_name = ? WHERE id = ?", (display_name, bot_id)
        )
        await db.commit()


async def sync_bot_admins_json(bot_id: int) -> None:
    b = await get_bot(bot_id)
    if not b:
        return
    ids = await get_bot_admins(bot_id)
    # Keyed by bot_id, not bot name — bots.name has no UNIQUE constraint, and
    # every template's config_from_bot_row() now reads admins_<bot_id>.json
    # (Stage 2 "изоляция по bots.id"). Writing by name here would silently
    # never reach the file the running bot actually consults.
    path = DATA_DIR / f"admins_{bot_id}.json"
    path.write_text(json.dumps({"ids": ids}, ensure_ascii=False))


async def create_bot_record_with_admins(
    name: str,
    description: str,
    token: str,
    file_path: str,
    admin_ids: list[str],
    username: str | None = None,
) -> int:
    """Insert the bot record and its initial admins as one atomic transaction —
    a crash between the two would otherwise leave a bot with no admin at all."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO bots (name, username, description, token, file_path, status) VALUES (?, ?, ?, ?, ?, 'stopped')",
            (name, username, description, _encrypt_token(token), file_path),
        )
        bot_id = cursor.lastrowid
        for telegram_id in admin_ids:
            await db.execute(
                "INSERT OR IGNORE INTO bot_admins (bot_id, telegram_id) VALUES (?, ?)",
                (bot_id, telegram_id),
            )
        await db.commit()
    await sync_bot_admins_json(bot_id)
    return bot_id


async def get_all_bots() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM bots ORDER BY created_at DESC") as cursor:
            rows = await cursor.fetchall()
            return [_decrypt_row(dict(row)) for row in rows]


async def get_bot_by_name(name: str) -> dict | None:
    clean = name.lstrip("@").removesuffix("_bot")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM bots WHERE name = ? OR username = ?",
            (clean, name.lstrip("@")),
        ) as cursor:
            row = await cursor.fetchone()
            return _decrypt_row(dict(row)) if row else None


async def get_bot(bot_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM bots WHERE id = ?", (bot_id,)) as cursor:
            row = await cursor.fetchone()
            return _decrypt_row(dict(row)) if row else None


async def delete_bot(bot_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM bots WHERE id = ?", (bot_id,))
        # SQLite has foreign keys OFF by default (no PRAGMA foreign_keys=ON
        # anywhere in this module), so bot_payment_providers' REFERENCES
        # bots(id) alone would never actually cascade — clean it up explicitly
        # so a deleted bot doesn't leave its payment provider credential behind.
        await db.execute("DELETE FROM bot_payment_providers WHERE bot_id = ?", (bot_id,))
        await db.execute("DELETE FROM bot_features WHERE bot_id = ?", (bot_id,))
        await db.commit()


async def update_bot_username(bot_id: int, username: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE bots SET username = ? WHERE id = ?", (username, bot_id))
        await db.commit()


async def set_bot_payment_provider(bot_id: int, provider_token: str) -> None:
    """Upserts this bot's Telegram payment provider token — 1:1 with bots.id,
    same Fernet encryption as bots.token. No admin-facing command sets this in
    Phase B; it's written directly against the DB (factory-side), per owner."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO bot_payment_providers (bot_id, provider_token) VALUES (?, ?)
            ON CONFLICT(bot_id) DO UPDATE SET provider_token = excluded.provider_token
            """,
            (bot_id, _encrypt_token(provider_token)),
        )
        await db.commit()
    logger.info(f"set_bot_payment_provider: provider_token set for bot_id={bot_id}")


async def get_bot_payment_provider(bot_id: int) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT provider_token FROM bot_payment_providers WHERE bot_id = ?", (bot_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return _decrypt_token(row[0]) if row else None


async def enable_bot_feature(bot_id: int, feature_name: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO bot_features (bot_id, feature_name) VALUES (?, ?)",
            (bot_id, feature_name),
        )
        await db.commit()
    logger.info(f"enable_bot_feature: feature_name={feature_name!r} enabled for bot_id={bot_id}")


async def disable_bot_feature(bot_id: int, feature_name: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM bot_features WHERE bot_id = ? AND feature_name = ?",
            (bot_id, feature_name),
        )
        await db.commit()
    logger.info(f"disable_bot_feature: feature_name={feature_name!r} disabled for bot_id={bot_id}")


async def get_bot_features(bot_id: int) -> list[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT feature_name FROM bot_features WHERE bot_id = ?", (bot_id,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [row[0] for row in rows]


async def update_bot_status(bot_id: int, status: str, pid: int | None = None):
    async with aiosqlite.connect(DB_PATH) as db:
        if pid is not None:
            await db.execute(
                "UPDATE bots SET status = ?, pid = ? WHERE id = ?",
                (status, pid, bot_id),
            )
        else:
            await db.execute(
                "UPDATE bots SET status = ? WHERE id = ?",
                (status, bot_id),
            )
        await db.commit()
