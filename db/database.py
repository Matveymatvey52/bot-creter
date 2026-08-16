from __future__ import annotations

import json
import logging
import os
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

# Userbot (Telethon StringSession) encryption uses a SEPARATE key from
# ENCRYPTION_KEY (bot tokens / payment provider tokens) — see
# docs/USERBOT_CHANNEL_MONITOR_DESIGN.md §1: rotating one secret type must
# not force rotating access to the other. Read lazily (not at import time)
# so importing db.database doesn't hard-require USERBOT_ENCRYPTION_KEY for
# code paths that never touch userbot_sessions (existing bots-table users,
# tests unrelated to channel_monitor). The Fernet instance itself is built
# once on first use and cached, mirroring _fernet above.
_userbot_fernet: Fernet | None = None
_userbot_fernet_key_used: str | None = None


def _get_userbot_fernet() -> Fernet:
    global _userbot_fernet, _userbot_fernet_key_used
    key = os.getenv("USERBOT_ENCRYPTION_KEY")
    if not key:
        raise ValueError(
            "USERBOT_ENCRYPTION_KEY is not set in .env — required to store/read Telegram "
            "userbot sessions (channel_monitor). Generate one with: python -c "
            "\"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    # Rebuild if the env var changed since last call (e.g. tests monkeypatching
    # os.environ between cases) — cheap, and avoids a stale Fernet instance
    # silently keeping an old key alive for the rest of the process.
    if _userbot_fernet is None or _userbot_fernet_key_used != key:
        try:
            _userbot_fernet = Fernet(key.encode())
        except (ValueError, TypeError) as e:
            raise ValueError(
                "USERBOT_ENCRYPTION_KEY is invalid. Generate one with: python -c "
                "\"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
            ) from e
        _userbot_fernet_key_used = key
    return _userbot_fernet


def _encrypt_session(session_string: str | None) -> str | None:
    if not session_string:
        return session_string
    return _get_userbot_fernet().encrypt(session_string.encode()).decode()


def _decrypt_session(session_string: str | None) -> str | None:
    if not session_string:
        return session_string
    try:
        return _get_userbot_fernet().decrypt(session_string.encode()).decode()
    except (InvalidToken, AttributeError, TypeError, ValueError):
        # USERBOT_ENCRYPTION_KEY was rotated/changed since this session was
        # encrypted (or the ciphertext is corrupt) — unrecoverable. Never log
        # the undecryptable blob itself (could resemble session material);
        # return None so callers treat this exactly like "no session".
        logger.warning("Could not decrypt a userbot session — USERBOT_ENCRYPTION_KEY changed or ciphertext corrupt")
        return None


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
        for col in ("display_name TEXT", "group_chat_id TEXT", "archived_at TIMESTAMP"):
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
        # shop_id/secret_key + a cache of the last GET /me lookup (see
        # services/yookassa_api.py) — separate from provider_token (that
        # stays the Telegram-side @YooKassaBot token; these are the ЮKassa
        # merchant API credentials the owner types once so the wizard can
        # look up payment_methods/status itself). last_status/
        # last_payment_methods_json are a cache only, refreshed by the
        # "Проверить статус" button/poll — never the source of truth for
        # whether the bot can actually take payments (provider_token is).
        for col in (
            "shop_id TEXT",
            "secret_key TEXT",
            "last_status TEXT",
            "last_payment_methods_json TEXT",
            "last_checked_at TIMESTAMP",
        ):
            try:
                await db.execute(f"ALTER TABLE bot_payment_providers ADD COLUMN {col}")
            except aiosqlite.OperationalError:
                pass
        # owner_payment_credentials — the ЮKassa shop_id/secret_key entered
        # once, reused as a suggested default when connecting payments to a
        # later bot (owner_user_id-scoped, same pattern as userbot_sessions
        # below). Telegram's Bot API still requires a separate manual
        # BotFather trip per bot for provider_token — this table only
        # removes the need to re-type shop_id/secret_key for every bot.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS owner_payment_credentials (
                owner_user_id INTEGER PRIMARY KEY,
                shop_id       TEXT NOT NULL,
                secret_key    TEXT NOT NULL,
                updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bot_custom_features (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id      INTEGER NOT NULL REFERENCES bots(id),
                description TEXT NOT NULL,
                applied_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bot_feedback (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id     INTEGER NOT NULL REFERENCES bots(id),
                rating     INTEGER NOT NULL,
                comment    TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bot_sheets_config (
                bot_id         INTEGER PRIMARY KEY REFERENCES bots(id),
                spreadsheet_id TEXT NOT NULL,
                sheet_title    TEXT,
                connected_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # "Офисы" (docs/OFFICES_DESIGN.md) — explicit, factory-side subscription
        # links between two bots owned by the same factory instance. Not
        # per-bot data (like bot_sheets_config): this is metadata ABOUT the
        # relationship between two bots.id rows, so it lives here alongside
        # bot_features rather than in either bot's own per-bot db_path.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bot_office_links (
                source_bot_id INTEGER NOT NULL REFERENCES bots(id),
                target_bot_id INTEGER NOT NULL REFERENCES bots(id),
                event_type    TEXT NOT NULL,
                created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (source_bot_id, target_bot_id, event_type)
            )
        """)
        # mini-app config (see docs/MINIAPP_DESIGN.md §6) — one row per bot,
        # JSON blob (same shape as templates/tour_operator.py's miniapp_config
        # dict: {"resources": [...]}). Stored in the factory DB, not inline in
        # the bot's .py file, so it can be regenerated (custom_features edits,
        # /recreate) without touching the bot's source.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bot_miniapp_config (
                bot_id       INTEGER PRIMARY KEY REFERENCES bots(id),
                config_json  TEXT NOT NULL,
                generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # office_hook_config (docs/OFFICES_DESIGN.md §11) — same shape/purpose
        # as bot_miniapp_config, one row per bot, generated by the same cheap
        # Haiku step as miniapp_config (services/claude_service.py's
        # _generate_office_hook_config). Declarative {"table":..., "match_field":...}
        # read at office-event-delivery time by features/office_events.py's
        # generic_on_office_event() for any TEMPLATE-based bot with no
        # hand-written on_office_event of its own — see runtime/registry.py's
        # build_entry() wiring. A missing/NULL row just means "no client-match
        # field found for this bot", same as a bot with no miniapp_config today.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bot_office_hook_config (
                bot_id       INTEGER PRIMARY KEY REFERENCES bots(id),
                config_json  TEXT NOT NULL,
                generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # channel_monitor (userbot/Telethon product) — see
        # docs/USERBOT_CHANNEL_MONITOR_DESIGN.md §1, §4. Lives in the central
        # factory DB (not a per-tenant-bot DB): a userbot session belongs to
        # the factory client (owner_user_id), not to any single rented bot.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS userbot_sessions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_user_id   INTEGER NOT NULL,
                phone           TEXT NOT NULL,
                session_string  TEXT,
                status          TEXT DEFAULT 'pending',
                created_at      TEXT DEFAULT (datetime('now','localtime')),
                updated_at      TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS monitored_channels (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_user_id     INTEGER NOT NULL,
                channel_username  TEXT,
                channel_id        INTEGER,
                channel_title     TEXT,
                active            INTEGER DEFAULT 1,
                added_at          TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS channel_posts (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                monitored_channel_id INTEGER NOT NULL REFERENCES monitored_channels(id),
                message_id           INTEGER,
                text                 TEXT,
                posted_at            TEXT DEFAULT (datetime('now','localtime')),
                summary              TEXT
            )
        """)
        # template_candidates (docs/TEMPLATE_CANDIDATE_LOGGING_DESIGN.md) —
        # one row per bot whose generation fell through to from-scratch
        # (services/claude_service.py's generate_bot_code's fallback_info):
        # either no template matched at all, or a matched template's
        # customize/synthesis step produced invalid code. bot_id is nullable
        # because the row is written from handlers/create_bot.py's
        # auto_launch_managed_bot AFTER the bot record exists, but a failure
        # between generation and that point must not silently lose the
        # candidate — see add_template_candidate(). Feeds the "Кандидаты на
        # новый шаблон" section of the /analytics factory dashboard so the
        # owner can spot recurring from-scratch requests worth turning into a
        # permanent template — the decision always stays manual.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS template_candidates (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id              INTEGER REFERENCES bots(id),
                creator_user_id     INTEGER NOT NULL,
                bot_name            TEXT,
                summary             TEXT NOT NULL,
                fallback_reason     TEXT NOT NULL,
                selected_templates  TEXT,
                bot_type            TEXT,
                created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
        await db.execute("DELETE FROM bot_sheets_config WHERE bot_id = ?", (bot_id,))
        await db.execute("DELETE FROM bot_custom_features WHERE bot_id = ?", (bot_id,))
        await db.execute("DELETE FROM bot_miniapp_config WHERE bot_id = ?", (bot_id,))
        await db.execute("DELETE FROM bot_office_hook_config WHERE bot_id = ?", (bot_id,))
        await db.execute(
            "DELETE FROM bot_office_links WHERE source_bot_id = ? OR target_bot_id = ?", (bot_id, bot_id)
        )
        await db.commit()


async def update_bot_username(bot_id: int, username: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE bots SET username = ? WHERE id = ?", (username, bot_id))
        await db.commit()


async def set_bot_payment_provider(bot_id: int, provider_token: str) -> None:
    """Upserts this bot's Telegram payment provider token — 1:1 with bots.id,
    same Fernet encryption as bots.token. Set via the owner-facing payment
    onboarding wizard in handlers/manage_bots.py (PaymentConnectFlow)."""
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


async def set_bot_yookassa_credentials(bot_id: int, shop_id: str, secret_key: str) -> None:
    """Stores this bot's ЮKassa merchant API credentials (shop_id/secret_key), separate
    from provider_token — used by services/yookassa_api.py's GET /me lookups. Requires a
    pre-existing bot_payment_providers row (provider_token already set); callers connect
    the Telegram provider_token first, same order as the onboarding wizard's steps."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE bot_payment_providers SET shop_id = ?, secret_key = ? WHERE bot_id = ?",
            (shop_id, _encrypt_token(secret_key), bot_id),
        )
        await db.commit()
    logger.info(f"set_bot_yookassa_credentials: shop_id set for bot_id={bot_id}")


async def get_bot_yookassa_credentials(bot_id: int) -> tuple[str, str] | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT shop_id, secret_key FROM bot_payment_providers WHERE bot_id = ?", (bot_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if not row or not row[0] or not row[1]:
                return None
            return row[0], _decrypt_token(row[1])


async def set_bot_yookassa_status_cache(bot_id: int, status: str, payment_methods_json: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE bot_payment_providers
            SET last_status = ?, last_payment_methods_json = ?, last_checked_at = CURRENT_TIMESTAMP
            WHERE bot_id = ?
            """,
            (status, payment_methods_json, bot_id),
        )
        await db.commit()


async def get_all_bot_ids_with_yookassa_credentials() -> list[int]:
    """Bot IDs that have shop_id/secret_key on file — the periodic status-poll loop
    (runtime/payment_status_poller.py) iterates these instead of every bot, since
    most bots never opt into the (а)/(в) API layer."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT bot_id FROM bot_payment_providers WHERE shop_id IS NOT NULL AND secret_key IS NOT NULL"
        ) as cursor:
            rows = await cursor.fetchall()
            return [row[0] for row in rows]


async def get_bot_yookassa_status_cache(bot_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT last_status, last_payment_methods_json, last_checked_at "
            "FROM bot_payment_providers WHERE bot_id = ?",
            (bot_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if not row or not row["last_status"]:
                return None
            return dict(row)


async def set_owner_payment_credentials(owner_user_id: int, shop_id: str, secret_key: str) -> None:
    """Upserts the owner's ЮKassa shop_id/secret_key, reused as a suggested default the
    next time they connect payments to a different bot — see docstring on
    owner_payment_credentials in init_db()."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO owner_payment_credentials (owner_user_id, shop_id, secret_key) VALUES (?, ?, ?)
            ON CONFLICT(owner_user_id) DO UPDATE SET
                shop_id = excluded.shop_id, secret_key = excluded.secret_key, updated_at = CURRENT_TIMESTAMP
            """,
            (owner_user_id, shop_id, _encrypt_token(secret_key)),
        )
        await db.commit()


async def get_owner_payment_credentials(owner_user_id: int) -> tuple[str, str] | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT shop_id, secret_key FROM owner_payment_credentials WHERE owner_user_id = ?",
            (owner_user_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return (row[0], _decrypt_token(row[1])) if row else None


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


async def set_bot_sheets_config(bot_id: int, spreadsheet_id: str, sheet_title: str | None) -> None:
    """Upserts this bot's connected Google Sheet — 1:1 with bots.id, same
    ON CONFLICT pattern as set_bot_payment_provider. No credential here: the
    Service Account key is a shared factory-side secret (config.py's
    GOOGLE_SHEETS_SA_KEY_PATH), not a per-bot DB column."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO bot_sheets_config (bot_id, spreadsheet_id, sheet_title) VALUES (?, ?, ?)
            ON CONFLICT(bot_id) DO UPDATE SET spreadsheet_id = excluded.spreadsheet_id,
                sheet_title = excluded.sheet_title, connected_at = CURRENT_TIMESTAMP
            """,
            (bot_id, spreadsheet_id, sheet_title),
        )
        await db.commit()
    logger.info(f"set_bot_sheets_config: bot_id={bot_id} spreadsheet_id={spreadsheet_id}")


async def get_bot_sheets_config(bot_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT spreadsheet_id, sheet_title, connected_at FROM bot_sheets_config WHERE bot_id = ?",
            (bot_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def set_bot_miniapp_config(bot_id: int, config: dict) -> None:
    """Upserts this bot's mini-app schema (see docs/MINIAPP_DESIGN.md §6) —
    1:1 with bots.id, same ON CONFLICT pattern as set_bot_sheets_config.
    config is the {"resources": [...]} dict (same shape as
    templates/tour_operator.py's miniapp_config), stored as JSON since the
    resource/field list is per-bot and has no fixed column set."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO bot_miniapp_config (bot_id, config_json) VALUES (?, ?)
            ON CONFLICT(bot_id) DO UPDATE SET config_json = excluded.config_json,
                generated_at = CURRENT_TIMESTAMP
            """,
            (bot_id, json.dumps(config, ensure_ascii=False)),
        )
        await db.commit()
    logger.info(f"set_bot_miniapp_config: bot_id={bot_id}")


async def get_bot_miniapp_config(bot_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT config_json FROM bot_miniapp_config WHERE bot_id = ?", (bot_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            try:
                return json.loads(row[0])
            except (json.JSONDecodeError, ValueError):
                logger.warning(f"get_bot_miniapp_config: corrupt config_json for bot_id={bot_id}")
                return None


async def delete_bot_miniapp_config(bot_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM bot_miniapp_config WHERE bot_id = ?", (bot_id,))
        await db.commit()


async def set_bot_office_hook_config(bot_id: int, config: dict) -> None:
    """Upserts this bot's generic office-hook config (docs/OFFICES_DESIGN.md
    §11) — 1:1 with bots.id, same ON CONFLICT pattern as
    set_bot_miniapp_config. config is {"table": str, "match_field": str|None,
    "created_at_field": str|None} (see services/claude_service.py's
    _generate_office_hook_config for how it's produced; created_at_field is
    also consumed by features/sales_analytics.py for time-bucketed metrics —
    older rows written before that field existed simply have it absent, read
    back as None via dict.get, same as any other optional JSON key)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO bot_office_hook_config (bot_id, config_json) VALUES (?, ?)
            ON CONFLICT(bot_id) DO UPDATE SET config_json = excluded.config_json,
                generated_at = CURRENT_TIMESTAMP
            """,
            (bot_id, json.dumps(config, ensure_ascii=False)),
        )
        await db.commit()
    logger.info(f"set_bot_office_hook_config: bot_id={bot_id}")


async def get_bot_office_hook_config(bot_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT config_json FROM bot_office_hook_config WHERE bot_id = ?", (bot_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            try:
                return json.loads(row[0])
            except (json.JSONDecodeError, ValueError):
                logger.warning(f"get_bot_office_hook_config: corrupt config_json for bot_id={bot_id}")
                return None


async def delete_bot_office_hook_config(bot_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM bot_office_hook_config WHERE bot_id = ?", (bot_id,))
        await db.commit()


async def get_bot_features(bot_id: int) -> list[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT feature_name FROM bot_features WHERE bot_id = ?", (bot_id,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [row[0] for row in rows]


async def add_office_link(source_bot_id: int, target_bot_id: int, event_type: str) -> None:
    """Subscribes target_bot_id to event_type published by source_bot_id — see
    docs/OFFICES_DESIGN.md §3. INSERT OR IGNORE: re-subscribing to an already
    existing (source, target, event_type) triple is a no-op, not an error —
    same idempotency the PRIMARY KEY already gives bot_features."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT OR IGNORE INTO bot_office_links (source_bot_id, target_bot_id, event_type)
            VALUES (?, ?, ?)
            """,
            (source_bot_id, target_bot_id, event_type),
        )
        await db.commit()


async def remove_office_link(source_bot_id: int, target_bot_id: int, event_type: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM bot_office_links WHERE source_bot_id = ? AND target_bot_id = ? AND event_type = ?",
            (source_bot_id, target_bot_id, event_type),
        )
        await db.commit()


async def get_office_links_for_bot(bot_id: int) -> list[dict]:
    """Every bot_office_links row where bot_id is EITHER source or target —
    the shape handlers/manage_bots.py's "🏢 Офисы" panel needs to show both
    directions (bot X notifies Y / bot Z notifies X) on one screen. Unlike
    get_office_subscribers (scoped to one source+event_type pair for
    publish_event()'s single lookup), this is a UI-listing query, so it
    returns full rows rather than just target_bot_id."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT source_bot_id, target_bot_id, event_type
            FROM bot_office_links
            WHERE source_bot_id = ? OR target_bot_id = ?
            ORDER BY created_at
            """,
            (bot_id, bot_id),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def get_office_subscribers(source_bot_id: int, event_type: str) -> list[int]:
    """Every bot_id currently subscribed to event_type published by
    source_bot_id — see features/office_events.py's publish_event(), the only
    caller. Deliberately scoped to ONE source+event_type pair per call rather
    than returning the whole links table, since that's the only shape a
    publisher ever needs."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT target_bot_id FROM bot_office_links WHERE source_bot_id = ? AND event_type = ?",
            (source_bot_id, event_type),
        ) as cursor:
            rows = await cursor.fetchall()
            return [row[0] for row in rows]


async def add_custom_feature_record(bot_id: int, description: str) -> None:
    """Append-only audit log entry for one applied custom_features/bot_<id>.py
    patch — bot_id is NOT unique/PK here (unlike bot_sheets_config), so a bot
    re-patched multiple times over its lifetime accumulates one row per
    successful apply. Purely a history trail for the owner; runtime/registry.py
    never queries this table — it loads custom_features/bot_<id>.py by file
    existence alone."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO bot_custom_features (bot_id, description) VALUES (?, ?)",
            (bot_id, description),
        )
        await db.commit()
    logger.info(f"add_custom_feature_record: bot_id={bot_id} description={description!r}")


async def get_custom_feature_history(bot_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            # id DESC as the real tiebreaker — applied_at is CURRENT_TIMESTAMP
            # at second resolution, so two applies within the same second
            # would otherwise sort in an unspecified order; id is
            # AUTOINCREMENT and always reflects true insertion order.
            "SELECT id, description, applied_at FROM bot_custom_features "
            "WHERE bot_id = ? ORDER BY id DESC",
            (bot_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def add_bot_feedback(bot_id: int, rating: int, comment: str | None = None) -> None:
    """Append-only, like add_custom_feature_record — the owner can leave
    feedback more than once per bot as things change, so this is a history
    trail, not a 1:1 upsert (unlike bot_sheets_config/bot_miniapp_config)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO bot_feedback (bot_id, rating, comment) VALUES (?, ?, ?)",
            (bot_id, rating, comment),
        )
        await db.commit()
    logger.info(f"add_bot_feedback: bot_id={bot_id} rating={rating}")


async def get_bot_feedback(bot_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, rating, comment, created_at FROM bot_feedback "
            "WHERE bot_id = ? ORDER BY id DESC",
            (bot_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def add_template_candidate(
    creator_user_id: int,
    summary: str,
    fallback_reason: str,
    selected_templates: list[str],
    bot_type: str | None = None,
    bot_name: str | None = None,
    bot_id: int | None = None,
) -> None:
    """Append-only log of from-scratch fallback generations (see
    template_candidates' comment in init_db()). selected_templates is stored
    as a JSON array — even an empty list is meaningful (the classifier saw no
    match at all, vs. matched-but-customize/synthesis-failed)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO template_candidates "
            "(bot_id, creator_user_id, bot_name, summary, fallback_reason, selected_templates, bot_type) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                bot_id,
                creator_user_id,
                bot_name,
                summary,
                fallback_reason,
                json.dumps(selected_templates, ensure_ascii=False),
                bot_type,
            ),
        )
        await db.commit()
    logger.info(
        f"add_template_candidate: bot_id={bot_id} reason={fallback_reason} "
        f"selected_templates={selected_templates}"
    )


async def list_template_candidates(limit: int = 200) -> list[dict]:
    """Most recent candidates first, for the /analytics dashboard's
    "Кандидаты на новый шаблон" section. No clustering/grouping here — kept
    as raw rows (per the design doc's MVP decision) so the owner reads the
    actual requirement text rather than a heuristic summary; the frontend or
    caller may group by bot_type itself."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, bot_id, creator_user_id, bot_name, summary, fallback_reason, "
            "selected_templates, bot_type, created_at "
            "FROM template_candidates ORDER BY id DESC LIMIT ?",
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
            result = []
            for row in rows:
                item = dict(row)
                try:
                    item["selected_templates"] = json.loads(item["selected_templates"] or "[]")
                except (json.JSONDecodeError, TypeError):
                    item["selected_templates"] = []
                result.append(item)
            return result


async def list_bots_with_stats() -> list[dict]:
    """One row per bot for the factory analytics dashboard (see
    docs/FACTORY_ANALYTICS_DESIGN.md): created_at/status/archived_at straight
    off `bots`, plus aggregate counts joined in. Deliberately excludes
    `token` (encrypted at rest — see _decrypt_row/_encrypt_token — this
    dashboard has no business decrypting it) and file_path (template is
    derived from it separately via runtime.registry.infer_template_id, kept
    out of the DB layer to avoid a db/database.py -> runtime/registry.py
    import). Feature list comes back as a comma-joined string (SQLite has no
    array type); callers split on ',' and filter empties."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT
                b.id, b.name, b.username, b.display_name, b.status,
                b.created_at, b.archived_at, b.file_path,
                COALESCE(f.features, '') AS features,
                COALESCE(c.edits_count, 0) AS edits_count,
                r.avg_rating, r.feedback_count
            FROM bots b
            LEFT JOIN (
                SELECT bot_id, GROUP_CONCAT(feature_name, ',') AS features
                FROM bot_features GROUP BY bot_id
            ) f ON f.bot_id = b.id
            LEFT JOIN (
                SELECT bot_id, COUNT(*) AS edits_count
                FROM bot_custom_features GROUP BY bot_id
            ) c ON c.bot_id = b.id
            LEFT JOIN (
                SELECT bot_id, AVG(rating) AS avg_rating, COUNT(*) AS feedback_count
                FROM bot_feedback GROUP BY bot_id
            ) r ON r.bot_id = b.id
            ORDER BY b.created_at DESC
        """) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


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



# ── userbot_sessions (channel_monitor) ──────────────────────────────────────
# See docs/USERBOT_CHANNEL_MONITOR_DESIGN.md §1. session_string is Fernet-
# encrypted with USERBOT_ENCRYPTION_KEY (_encrypt_session/_decrypt_session
# above) — a SEPARATE key from ENCRYPTION_KEY (bot tokens).

def _decrypt_userbot_row(row: dict) -> dict:
    if "session_string" in row:
        row["session_string"] = _decrypt_session(row["session_string"])
    return row


async def create_userbot_session(owner_user_id: int, phone: str) -> int:
    """Inserts the 'pending' row an auth FSM attaches to as it progresses —
    same idea as create_bot_record_with_admins: one row per authorization
    attempt, status starts 'pending', session_string is NULL until sign-in
    succeeds (activate_userbot_session below)."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO userbot_sessions (owner_user_id, phone, status) VALUES (?, ?, 'pending')",
            (owner_user_id, phone),
        )
        await db.commit()
        return cursor.lastrowid


async def activate_userbot_session(session_id: int, session_string: str) -> None:
    """Encrypts and stores the completed Telethon StringSession, marking the
    row 'active'. The plaintext session_string exists only in the caller's
    memory up to this call — never logged, never persisted unencrypted."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE userbot_sessions SET session_string = ?, status = 'active', "
            "updated_at = datetime('now','localtime') WHERE id = ?",
            (_encrypt_session(session_string), session_id),
        )
        await db.commit()
    logger.info(f"activate_userbot_session: session_id={session_id} is now active")


async def mark_userbot_session_failed(session_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE userbot_sessions SET status = 'auth_failed', updated_at = datetime('now','localtime') "
            "WHERE id = ?",
            (session_id,),
        )
        await db.commit()


async def set_userbot_session_active(session_id: int, session_string: str) -> None:
    """Alias for activate_userbot_session — matches
    templates/channel_monitor.py's call-site naming for the FSM success path."""
    await activate_userbot_session(session_id, session_string)


async def set_userbot_session_status(session_id: int, status: str) -> None:
    """Generic status setter (status in {'pending','active','revoked','auth_failed'})
    used by templates/channel_monitor.py's FSM failure paths (e.g. marking
    'auth_failed' after too many bad codes) — unlike revoke_userbot_session,
    does NOT touch session_string, since these failure paths never had one."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE userbot_sessions SET status = ?, updated_at = datetime('now','localtime') WHERE id = ?",
            (status, session_id),
        )
        await db.commit()


async def revoke_userbot_session(session_id: int) -> None:
    """Per design §1: the row is kept for audit ('revoked', not deleted), but
    session_string is wiped — reconnecting after revoke requires a full new
    authorization, not a resume."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE userbot_sessions SET session_string = NULL, status = 'revoked', "
            "updated_at = datetime('now','localtime') WHERE id = ?",
            (session_id,),
        )
        await db.commit()
    logger.info(f"revoke_userbot_session: session_id={session_id} revoked")


async def get_userbot_session(session_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM userbot_sessions WHERE id = ?", (session_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return _decrypt_userbot_row(dict(row)) if row else None


async def get_userbot_session_by_owner(owner_user_id: int) -> dict | None:
    """A factory client is expected to have at most one userbot session at a
    time — most recent row wins if somehow more than one exists (e.g. after a
    revoke + re-authorization)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM userbot_sessions WHERE owner_user_id = ? ORDER BY id DESC LIMIT 1",
            (owner_user_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return _decrypt_userbot_row(dict(row)) if row else None


async def get_userbot_sessions_by_owner(owner_user_id: int) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM userbot_sessions WHERE owner_user_id = ? ORDER BY created_at DESC",
            (owner_user_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [_decrypt_userbot_row(dict(row)) for row in rows]


async def get_active_userbot_sessions() -> list[dict]:
    """Used by runtime/userbot_worker.py on startup to restore every active
    client — same role restore_bots() plays for tenant bots in main.py."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM userbot_sessions WHERE status = 'active'"
        ) as cursor:
            rows = await cursor.fetchall()
            return [_decrypt_userbot_row(dict(row)) for row in rows]


# ── monitored_channels (channel_monitor) ────────────────────────────────────

async def add_monitored_channel(
    owner_user_id: int,
    channel_username: str | None,
    channel_id: int | None,
    channel_title: str | None,
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO monitored_channels (owner_user_id, channel_username, channel_id, channel_title) "
            "VALUES (?, ?, ?, ?)",
            (owner_user_id, channel_username, channel_id, channel_title),
        )
        await db.commit()
        return cursor.lastrowid


async def get_monitored_channels(owner_user_id: int, active_only: bool = False) -> list[dict]:
    query = "SELECT * FROM monitored_channels WHERE owner_user_id = ?"
    params: list = [owner_user_id]
    if active_only:
        query += " AND active = 1"
    query += " ORDER BY added_at DESC"
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def get_monitored_channels_by_owner(owner_user_id: int) -> list[dict]:
    """Alias for get_monitored_channels(owner_user_id) — kept as a separate
    name matching templates/channel_monitor.py's call sites (mirrors
    get_userbot_sessions_by_owner's naming), same query underneath."""
    return await get_monitored_channels(owner_user_id)


async def get_monitored_channel(channel_row_id: int) -> dict | None:
    """channel_row_id is monitored_channels.id (the PK), not the Telegram
    channel id — same naming ambiguity as bot_id elsewhere in this file."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM monitored_channels WHERE id = ?", (channel_row_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def get_all_active_monitored_channels() -> list[dict]:
    """Used by runtime/userbot_worker.py to know which channels each restored
    client should subscribe events.NewMessage on."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM monitored_channels WHERE active = 1"
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def get_active_monitored_channels() -> list[dict]:
    """Alias for get_all_active_monitored_channels — matches
    runtime/userbot_worker.py's call-site naming."""
    return await get_all_active_monitored_channels()


async def set_monitored_channel_active(channel_row_id: int, active: bool) -> None:
    """Toggle used by the "📋 Мои каналы" list — channel_row_id is monitored_channels.id."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE monitored_channels SET active = ? WHERE id = ?",
            (1 if active else 0, channel_row_id),
        )
        await db.commit()


async def set_monitored_channel_resolved(channel_row_id: int, channel_id: int, channel_title: str | None) -> None:
    """Fills in channel_id/channel_title once client.get_entity() resolves the
    @username the client typed — see design §4."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE monitored_channels SET channel_id = ?, channel_title = ? WHERE id = ?",
            (channel_id, channel_title, channel_row_id),
        )
        await db.commit()


# ── channel_posts (channel_monitor) ─────────────────────────────────────────

async def add_channel_post(monitored_channel_id: int, message_id: int | None, text: str | None) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO channel_posts (monitored_channel_id, message_id, text) VALUES (?, ?, ?)",
            (monitored_channel_id, message_id, text),
        )
        await db.commit()
        return cursor.lastrowid


async def set_channel_post_summary(post_id: int, summary: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE channel_posts SET summary = ? WHERE id = ?", (summary, post_id)
        )
        await db.commit()


async def get_posts_missing_summary(limit: int = 20) -> list[dict]:
    """Used by the background Gemini-summarization loop in
    runtime/userbot_worker.py to pick up posts batched since the last run,
    oldest first so a backlog drains in order."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM channel_posts WHERE summary IS NULL ORDER BY id ASC LIMIT ?",
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def get_recent_posts_for_owner(owner_user_id: int, limit: int = 10) -> list[dict]:
    """Powers the «📰 Лента» screen — most recent posts across all of this
    owner's ACTIVE monitored channels, newest first. Joins in channel_title/
    channel_username so the feed can label which channel each post came from."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT cp.*, mc.channel_title, mc.channel_username
            FROM channel_posts cp
            JOIN monitored_channels mc ON mc.id = cp.monitored_channel_id
            WHERE mc.owner_user_id = ? AND mc.active = 1
            ORDER BY cp.id DESC
            LIMIT ?
            """,
            (owner_user_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]
