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
        for col in (
            "display_name TEXT",
            "group_chat_id TEXT",
            "archived_at TIMESTAMP",
            "owner_telegram_id INTEGER",
            # creation_prompt — the original free-text request the bot was
            # created from (see handlers/create_bot.py), stored for future
            # reference/reporting stages of the multitenancy rollout. NULL
            # for bots created before this column existed, or wherever the
            # call site had no prompt text on hand.
            "creation_prompt TEXT",
        ):
            try:
                await db.execute(f"ALTER TABLE bots ADD COLUMN {col}")
            except aiosqlite.OperationalError:
                pass
        # One-time backfill: bots created before owner_telegram_id existed
        # belong to OWNER_ID (the only person who could create bots at the
        # time) — see handlers/create_bot.py's owner-vs-customer split.
        owner_id_raw = os.getenv("OWNER_ID", "")
        if owner_id_raw.isdigit():
            await db.execute(
                "UPDATE bots SET owner_telegram_id = ? WHERE owner_telegram_id IS NULL",
                (int(owner_id_raw),),
            )
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
        # bot_feature_config — Variant D's "what should this feature do"
        # free-text description, one row per (bot_id, feature_name), separate
        # from bot_features (which only records ON/OFF). description is the
        # LATEST accepted text (what the owner sees under "Изменить описание");
        # thread_json is the in-progress clarification transcript ([{role,
        # text}, ...]) for a feature still stuck in "pending" (owner wrote
        # something, Claude asked a follow-up, no accepted description yet) —
        # NULL once accepted or never started. A feature reaching "enabled"
        # always has description NOT NULL; thread_json is cleared at that
        # point (see set_bot_feature_description below) since the transcript
        # has no further use once the description it produced is accepted.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bot_feature_config (
                bot_id        INTEGER NOT NULL REFERENCES bots(id),
                feature_name  TEXT NOT NULL,
                description   TEXT,
                thread_json   TEXT,
                updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
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
        # office_digest_group — the Telegram group a factory OWNER (system
        # owner OR a customer who owns their own bots) has opted to bind as a
        # read-only showcase/mirror of office_events activity (see
        # docs/OFFICES_DESIGN.md §12 "витрина"). Was a single fixed row
        # (id=1) when the whole factory had exactly one possible "owner";
        # Stage 1 of the multitenancy rollout makes this per-owner
        # (owner_telegram_id PRIMARY KEY) so each customer can bind their own
        # showcase group without clobbering anyone else's — unlike
        # bots.group_chat_id, which is a PER-BOT fallback destination for
        # that bot's own moderator-style notifications and is semantically
        # unrelated to this. Read via get_office_digest_group(owner_telegram_id)/
        # set via set_office_digest_group(owner_telegram_id, chat_id) below —
        # the miniapp's showcase_group_status_handler
        # (runtime/factory_analytics_api.py) and the Telegram-side
        # "🔔 Использовать как витрину" button (main.py) both bind through
        # this same pair, one canonical table.
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='office_digest_group'"
        ) as cursor:
            _odg_exists = await cursor.fetchone()
        if _odg_exists:
            async with db.execute("PRAGMA table_info(office_digest_group)") as cursor:
                _odg_cols = {row[1] for row in await cursor.fetchall()}
            if "owner_telegram_id" not in _odg_cols:
                # Old single-row (id=1) schema — migrate in place: rename,
                # recreate with the new PK, backfill the old row to OWNER_ID
                # (the only person who could have bound it under the old
                # schema), drop the renamed original. SQLite can't ALTER a
                # PRIMARY KEY directly, hence the rename/recreate/copy dance
                # (same pattern already used for bots.owner_telegram_id's
                # backfill above, just at the table level instead of a
                # column).
                await db.execute("ALTER TABLE office_digest_group RENAME TO office_digest_group_old")
                await db.execute("""
                    CREATE TABLE office_digest_group (
                        owner_telegram_id INTEGER PRIMARY KEY,
                        chat_id           TEXT NOT NULL,
                        bound_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                owner_id_raw = os.getenv("OWNER_ID", "")
                if owner_id_raw.isdigit():
                    await db.execute(
                        "INSERT INTO office_digest_group (owner_telegram_id, chat_id, bound_at) "
                        "SELECT ?, chat_id, bound_at FROM office_digest_group_old LIMIT 1",
                        (int(owner_id_raw),),
                    )
                await db.execute("DROP TABLE office_digest_group_old")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS office_digest_group (
                owner_telegram_id INTEGER PRIMARY KEY,
                chat_id           TEXT NOT NULL,
                bound_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
        # group_task_config (docs/GROUP_TASK_CHANNEL_DESIGN.md §4) — one row
        # per bot, the Telegram group chat this bot is bound to for the
        # owner-only "mention/reply to give a task" channel
        # (features/group_task.py). group_chat_id is NOT entered by hand —
        # the owner doesn't know a new group's numeric id — it's captured
        # automatically from the first mention/reply update the bot receives
        # in a group with no existing row here (see features/group_task.py's
        # router). PRIMARY KEY(bot_id): one bot is bound to at most one group
        # at a time, same "no per-bot multiplicity in v1" choice as
        # bot_sheets_config/bot_office_hook_config above.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bot_group_task_config (
                bot_id        INTEGER PRIMARY KEY REFERENCES bots(id),
                group_chat_id INTEGER NOT NULL,
                enabled       INTEGER NOT NULL DEFAULT 1,
                created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # voice_cashflow_config (docs/VOICE_CASHFLOW_FROM_SCRATCH_DESIGN.md) —
        # same shape/purpose as bot_office_hook_config, one row per bot,
        # generated by the same cheap Haiku step tier as miniapp_config/
        # office_hook_config (services/claude_service.py's
        # _generate_voice_cashflow_config). Declarative
        # {"voice_intake": {...} | null, "cashflow_ledger": bool} read at
        # registry build time by runtime/registry.py's
        # _load_and_include_features() when the voice_intake/cashflow_ledger
        # features are enabled for this bot — see
        # features/voice_intake.py's register_data_driven_schema(). A
        # missing/NULL row just means "neither feature for this bot", same as
        # a bot with no miniapp_config today.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS bot_voice_cashflow_config (
                bot_id       INTEGER PRIMARY KEY REFERENCES bots(id),
                config_json  TEXT NOT NULL,
                generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
        # template_candidate_clusters (docs/TEMPLATE_CANDIDATE_CLUSTERING_DESIGN.md) —
        # domain groups an incremental LLM classification pass assigns to
        # otherwise-unclustered template_candidates rows (cluster_id column
        # added below). label/description are both LLM-generated free text,
        # not a fixed enum (unlike bot_type) — the whole point is to surface
        # domains the 5-value bot_type vocabulary is too coarse to
        # distinguish. Created BEFORE the ALTER TABLE below so the FK target
        # already exists. Feeds the /analytics "Топ незакрытых паттернов"
        # section.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS template_candidate_clusters (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                label         TEXT NOT NULL,
                description   TEXT,
                created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        for col in ("cluster_id INTEGER REFERENCES template_candidate_clusters(id)",):
            try:
                await db.execute(f"ALTER TABLE template_candidates ADD COLUMN {col}")
            except aiosqlite.OperationalError:
                pass
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
    owner_telegram_id: int | None = None,
    creation_prompt: str | None = None,
) -> int:
    """Insert the bot record and its initial admins as one atomic transaction —
    a crash between the two would otherwise leave a bot with no admin at all.
    owner_telegram_id is the customer/creator this bot belongs to for the
    factory dashboard's per-owner filtering (runtime/factory_analytics_api.py) —
    None only for legacy call sites that haven't been updated yet.
    creation_prompt is the original free-text request the bot was generated
    from (handlers/create_bot.py) — None when no such text was available at
    the call site (e.g. flows that don't collect one)."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO bots (name, username, description, token, file_path, status, owner_telegram_id, creation_prompt) "
            "VALUES (?, ?, ?, ?, ?, 'stopped', ?, ?)",
            (name, username, description, _encrypt_token(token), file_path, owner_telegram_id, creation_prompt),
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
        await db.execute("DELETE FROM bot_voice_cashflow_config WHERE bot_id = ?", (bot_id,))
        await db.execute("DELETE FROM bot_feature_config WHERE bot_id = ?", (bot_id,))
        await db.execute(
            "DELETE FROM bot_office_links WHERE source_bot_id = ? OR target_bot_id = ?", (bot_id, bot_id)
        )
        await db.execute("DELETE FROM bot_group_task_config WHERE bot_id = ?", (bot_id,))
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


async def get_bot_feature_config(bot_id: int, feature_name: str) -> dict | None:
    """Returns {"description": str|None, "thread": list[dict]} or None if no
    row exists yet (feature never entered the Variant D configure flow).
    thread is decoded from thread_json — [] if NULL/absent, never raises on
    malformed JSON (degrades to [], same defensive posture as
    get_bot_miniapp_config's json.loads sibling calls)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT description, thread_json FROM bot_feature_config WHERE bot_id = ? AND feature_name = ?",
            (bot_id, feature_name),
        ) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            thread = []
            if row[1]:
                try:
                    thread = json.loads(row[1])
                except (json.JSONDecodeError, ValueError):
                    thread = []
            return {"description": row[0], "thread": thread}


async def set_bot_feature_thread(bot_id: int, feature_name: str, thread: list[dict]) -> None:
    """Persists the in-progress clarification transcript for a feature still
    pending (owner wrote something, Claude is not yet satisfied). Does NOT
    touch description — call set_bot_feature_description separately once
    Claude accepts it."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO bot_feature_config (bot_id, feature_name, thread_json, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT (bot_id, feature_name) DO UPDATE SET
                thread_json = excluded.thread_json,
                updated_at = CURRENT_TIMESTAMP
            """,
            (bot_id, feature_name, json.dumps(thread)),
        )
        await db.commit()


async def set_bot_feature_description(bot_id: int, feature_name: str, description: str) -> None:
    """Accepts a description (Claude judged the owner's text sufficient) —
    clears thread_json since the transcript that produced this description
    has no further use once accepted (see bot_feature_config's own comment
    in init_db). Callers still need to call enable_bot_feature separately;
    this only records WHAT the feature should do, not whether it's ON."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO bot_feature_config (bot_id, feature_name, description, thread_json, updated_at)
            VALUES (?, ?, ?, NULL, CURRENT_TIMESTAMP)
            ON CONFLICT (bot_id, feature_name) DO UPDATE SET
                description = excluded.description,
                thread_json = NULL,
                updated_at = CURRENT_TIMESTAMP
            """,
            (bot_id, feature_name, description),
        )
        await db.commit()
    logger.info(f"set_bot_feature_description: feature_name={feature_name!r} bot_id={bot_id}")


async def clear_bot_feature_config(bot_id: int, feature_name: str) -> None:
    """Wipes both description and thread — called when the owner cancels the
    configure flow (tumbler reverts to off, nothing should be remembered)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM bot_feature_config WHERE bot_id = ? AND feature_name = ?",
            (bot_id, feature_name),
        )
        await db.commit()


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


async def set_bot_voice_cashflow_config(bot_id: int, config: dict) -> None:
    """Upserts this bot's voice-intake/cashflow-ledger config (docs/
    VOICE_CASHFLOW_FROM_SCRATCH_DESIGN.md) — 1:1 with bots.id, same
    ON CONFLICT pattern as set_bot_office_hook_config. config is
    {"voice_intake": {...} | None, "cashflow_ledger": bool} (see
    services/claude_service.py's _generate_voice_cashflow_config for how
    it's produced)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO bot_voice_cashflow_config (bot_id, config_json) VALUES (?, ?)
            ON CONFLICT(bot_id) DO UPDATE SET config_json = excluded.config_json,
                generated_at = CURRENT_TIMESTAMP
            """,
            (bot_id, json.dumps(config, ensure_ascii=False)),
        )
        await db.commit()
    logger.info(f"set_bot_voice_cashflow_config: bot_id={bot_id}")


async def get_bot_voice_cashflow_config(bot_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT config_json FROM bot_voice_cashflow_config WHERE bot_id = ?", (bot_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            try:
                return json.loads(row[0])
            except (json.JSONDecodeError, ValueError):
                logger.warning(f"get_bot_voice_cashflow_config: corrupt config_json for bot_id={bot_id}")
                return None


async def delete_bot_voice_cashflow_config(bot_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM bot_voice_cashflow_config WHERE bot_id = ?", (bot_id,))
        await db.commit()


async def get_bot_features(bot_id: int) -> list[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT feature_name FROM bot_features WHERE bot_id = ?", (bot_id,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [row[0] for row in rows]


async def add_office_link(source_bot_id: int, target_bot_id: int, event_type: str) -> bool:
    """Subscribes target_bot_id to event_type published by source_bot_id — see
    docs/OFFICES_DESIGN.md §3. INSERT OR IGNORE: re-subscribing to an already
    existing (source, target, event_type) triple is a no-op, not an error —
    same idempotency the PRIMARY KEY already gives bot_features.

    Stage 1 multitenancy guard: a link is only created when the two bots are
    "owner-equivalent" — same owner_telegram_id, or either one is owned by
    the system OWNER_ID (same semantics as handlers/admin_manager.py's
    _is_owner/_can_manage_bot: the system owner can always bridge bots,
    customers can only link their own bots to each other). Returns False
    (no-op, nothing written) when the two bots belong to different customers
    and neither is the system owner; True otherwise (link created or already
    existed). Callers (handlers/manage_bots.py's cb_office_do_link,
    runtime/factory_analytics_api.py's add_office_handler) must check this
    return value and report the rejection rather than silently claiming
    success."""
    owner_id_raw = os.getenv("OWNER_ID", "")
    system_owner_id = int(owner_id_raw) if owner_id_raw.isdigit() else None
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, owner_telegram_id FROM bots WHERE id IN (?, ?)",
            (source_bot_id, target_bot_id),
        ) as cursor:
            owners = {row["id"]: row["owner_telegram_id"] for row in await cursor.fetchall()}
        source_owner = owners.get(source_bot_id)
        target_owner = owners.get(target_bot_id)
        # source_owner == target_owner covers both "same customer" and the
        # legacy/test case where neither bot has owner_telegram_id set yet
        # (both None) — in production every bot gets one at creation time or
        # via init_db()'s backfill, so None-vs-None in practice only shows up
        # for bots created directly in tests without passing one.
        is_owner_equivalent = source_owner == target_owner or (
            system_owner_id is not None and system_owner_id in (source_owner, target_owner)
        )
        if not is_owner_equivalent:
            return False
        await db.execute(
            """
            INSERT OR IGNORE INTO bot_office_links (source_bot_id, target_bot_id, event_type)
            VALUES (?, ?, ?)
            """,
            (source_bot_id, target_bot_id, event_type),
        )
        await db.commit()
    return True


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


async def set_office_digest_group(owner_telegram_id: int, chat_id: str) -> None:
    """Upserts this owner's office-digest showcase group — see
    office_digest_group's CREATE TABLE comment above for why this is
    per-owner (Stage 1 multitenancy) rather than one row for the whole
    factory."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO office_digest_group (owner_telegram_id, chat_id) VALUES (?, ?)
            ON CONFLICT(owner_telegram_id) DO UPDATE SET chat_id = excluded.chat_id, bound_at = CURRENT_TIMESTAMP
            """,
            (owner_telegram_id, chat_id),
        )
        await db.commit()


async def get_office_digest_group(owner_telegram_id: int) -> str | None:
    """This owner's bound showcase group's chat_id, or None if they never
    bound one — see features/office_events.py's publish_event(), the only
    reader."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT chat_id FROM office_digest_group WHERE owner_telegram_id = ?", (owner_telegram_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None


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


async def get_group_task_config(bot_id: int) -> dict | None:
    """This bot's bound group chat (docs/GROUP_TASK_CHANNEL_DESIGN.md §4), or
    None if never connected. Returns the row as a plain dict — the only two
    callers (handlers/manage_bots.py's "💬 Рабочая группа" panel,
    features/group_task.py's router) each need different fields
    (group_chat_id for routing, enabled for the panel toggle), so a full dict
    is simpler than two narrower query functions for a one-row lookup."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT bot_id, group_chat_id, enabled FROM bot_group_task_config WHERE bot_id = ?",
            (bot_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def set_group_task_config(bot_id: int, group_chat_id: int) -> None:
    """Binds bot_id to group_chat_id — called exactly once per bot, by
    features/group_task.py's router the first time it sees a mention/reply
    update from a group with no existing row (see that module's docstring for
    why this is auto-captured rather than entered by hand). ON CONFLICT
    overwrites rather than erroring so a bot moved to a different group (old
    row deleted via delete_group_task_config first, or re-added straight
    over an existing row) always ends up bound to the group it was just
    addressed in, never stuck on a stale chat_id."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO bot_group_task_config (bot_id, group_chat_id, enabled) VALUES (?, ?, 1)
            ON CONFLICT(bot_id) DO UPDATE SET group_chat_id = excluded.group_chat_id, enabled = 1
            """,
            (bot_id, group_chat_id),
        )
        await db.commit()
    logger.info(f"set_group_task_config: bot_id={bot_id} group_chat_id={group_chat_id}")


async def delete_group_task_config(bot_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM bot_group_task_config WHERE bot_id = ?", (bot_id,))
        await db.commit()


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


async def list_unclustered_template_candidates() -> list[dict]:
    """template_candidates rows not yet assigned a cluster_id — the input to
    each incremental pass of runtime/template_candidate_clustering.py. Oldest
    first (unlike list_template_candidates' newest-first) so a pass that gets
    interrupted partway still processes candidates in a stable, resumable
    order rather than re-picking whichever happen to sort first."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, summary, bot_type FROM template_candidates "
            "WHERE cluster_id IS NULL ORDER BY id ASC"
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def list_template_candidate_cluster_labels() -> list[dict]:
    """{id, label, description} for every existing cluster — the context an
    incremental classification pass shows the model so it can route a new
    candidate to an existing domain instead of always minting a new one."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, label, description FROM template_candidate_clusters ORDER BY id ASC"
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def create_template_candidate_cluster(label: str, description: str | None) -> int:
    """Mints a new domain cluster and returns its id, for the classification
    pass to assign to candidates it decided don't match any existing label."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO template_candidate_clusters (label, description) VALUES (?, ?)",
            (label, description),
        )
        await db.commit()
        return cursor.lastrowid


async def assign_template_candidate_cluster(candidate_id: int, cluster_id: int) -> None:
    """Marks one candidate as processed by this pass — the row drops out of
    list_unclustered_template_candidates() on the next call, which is what
    keeps the pass incremental (§3 of the design doc: never re-cluster
    already-assigned rows)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE template_candidates SET cluster_id = ? WHERE id = ?",
            (cluster_id, candidate_id),
        )
        await db.commit()


async def list_template_candidate_clusters_with_stats(limit_examples: int = 3) -> list[dict]:
    """Clusters with member count + a few example summaries, largest first —
    the data behind /analytics' "Топ незакрытых паттернов" section
    (runtime/factory_analytics_api.py). Unclustered candidates (cluster_id
    IS NULL, not yet picked up by a clustering pass) are deliberately
    excluded here — list_template_candidates() already surfaces the raw feed
    for anyone who wants to see everything including the not-yet-processed
    tail."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT c.id, c.label, c.description, "
            "COUNT(t.id) AS count, MIN(t.created_at) AS first_seen, MAX(t.created_at) AS last_seen "
            "FROM template_candidate_clusters c "
            "JOIN template_candidates t ON t.cluster_id = c.id "
            "GROUP BY c.id ORDER BY count DESC, c.id ASC"
        ) as cursor:
            cluster_rows = [dict(row) for row in await cursor.fetchall()]

        for cluster in cluster_rows:
            async with db.execute(
                "SELECT summary FROM template_candidates WHERE cluster_id = ? "
                "ORDER BY id DESC LIMIT ?",
                (cluster["id"], limit_examples),
            ) as cursor:
                cluster["examples"] = [row["summary"] for row in await cursor.fetchall()]

        return cluster_rows


async def list_bots_with_stats(owner_telegram_id: int | None = None) -> list[dict]:
    """One row per bot for the factory analytics dashboard (see
    docs/FACTORY_ANALYTICS_DESIGN.md): created_at/status/archived_at straight
    off `bots`, plus aggregate counts joined in. Deliberately excludes
    `token` (encrypted at rest — see _decrypt_row/_encrypt_token — this
    dashboard has no business decrypting it) and file_path (template is
    derived from it separately via runtime.registry.infer_template_id, kept
    out of the DB layer to avoid a db/database.py -> runtime/registry.py
    import). Feature list comes back as a comma-joined string (SQLite has no
    array type); callers split on ',' and filter empties.

    owner_telegram_id, when given, restricts the result to bots owned by
    that customer (runtime/factory_analytics_api.py's non-owner path) —
    None (the owner path) returns every bot, unchanged behavior."""
    where_clause = "WHERE b.owner_telegram_id = ?" if owner_telegram_id is not None else ""
    params = (owner_telegram_id,) if owner_telegram_id is not None else ()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(f"""
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
            {where_clause}
            ORDER BY b.created_at DESC
        """, params) as cursor:
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



# ── channel_monitor feature (userbot_sessions / monitored_channels / channel_posts) ──
# See docs/USERBOT_FEATURE_DESIGN.md — moved from the central factory DB to
# each host bot's OWN per-bot db_path (same pattern as features/payments.py's
# init_payments_tables): a userbot session now belongs to ONE bot_id, not to
# an owner_user_id shared across the whole factory (§2 Variant A — chosen for
# isolation and consistency with "feature data lives in the host's db_path").
# session_string is Fernet-encrypted with USERBOT_ENCRYPTION_KEY
# (_encrypt_session/_decrypt_session above) — a SEPARATE key from
# ENCRYPTION_KEY (bot tokens).

async def init_channel_monitor_tables(db_path: str) -> None:
    """Adds channel_monitor's tables to a template's OWN per-bot db_path.
    Call this from features/channel_monitor.py's init_db(), which
    runtime/registry.py's _load_and_include_features() calls automatically
    for any bot with 'channel_monitor' enabled — this module never opens/owns
    a database file by itself (matches init_payments_tables)."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS userbot_sessions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id          INTEGER NOT NULL,
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
                bot_id            INTEGER NOT NULL,
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
        await db.commit()


def _decrypt_userbot_row(row: dict) -> dict:
    if "session_string" in row:
        row["session_string"] = _decrypt_session(row["session_string"])
    return row


async def create_userbot_session(db_path: str, bot_id: int, phone: str) -> int:
    """Inserts the 'pending' row an auth FSM attaches to as it progresses —
    one row per authorization attempt, status starts 'pending', session_string
    is NULL until sign-in succeeds (activate_userbot_session below). A bot can
    have multiple historical rows (e.g. after a revoke + re-auth); only the
    most recent 'active' one matters at runtime."""
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "INSERT INTO userbot_sessions (bot_id, phone, status) VALUES (?, ?, 'pending')",
            (bot_id, phone),
        )
        await db.commit()
        return cursor.lastrowid


async def activate_userbot_session(db_path: str, session_id: int, session_string: str) -> None:
    """Encrypts and stores the completed Telethon StringSession, marking the
    row 'active'. The plaintext session_string exists only in the caller's
    memory up to this call — never logged, never persisted unencrypted."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE userbot_sessions SET session_string = ?, status = 'active', "
            "updated_at = datetime('now','localtime') WHERE id = ?",
            (_encrypt_session(session_string), session_id),
        )
        await db.commit()
    logger.info(f"activate_userbot_session: session_id={session_id} is now active")


async def mark_userbot_session_failed(db_path: str, session_id: int) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE userbot_sessions SET status = 'auth_failed', updated_at = datetime('now','localtime') "
            "WHERE id = ?",
            (session_id,),
        )
        await db.commit()


async def set_userbot_session_active(db_path: str, session_id: int, session_string: str) -> None:
    """Alias for activate_userbot_session — matches
    features/channel_monitor.py's call-site naming for the FSM success path."""
    await activate_userbot_session(db_path, session_id, session_string)


async def set_userbot_session_status(db_path: str, session_id: int, status: str) -> None:
    """Generic status setter (status in {'pending','active','revoked','auth_failed'})
    used by features/channel_monitor.py's FSM failure paths (e.g. marking
    'auth_failed' after too many bad codes) — unlike revoke_userbot_session,
    does NOT touch session_string, since these failure paths never had one."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE userbot_sessions SET status = ?, updated_at = datetime('now','localtime') WHERE id = ?",
            (status, session_id),
        )
        await db.commit()


async def revoke_userbot_session(db_path: str, session_id: int) -> None:
    """Per design §1: the row is kept for audit ('revoked', not deleted), but
    session_string is wiped — reconnecting after revoke requires a full new
    authorization, not a resume."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE userbot_sessions SET session_string = NULL, status = 'revoked', "
            "updated_at = datetime('now','localtime') WHERE id = ?",
            (session_id,),
        )
        await db.commit()
    logger.info(f"revoke_userbot_session: session_id={session_id} revoked")


async def get_userbot_session(db_path: str, session_id: int) -> dict | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM userbot_sessions WHERE id = ?", (session_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return _decrypt_userbot_row(dict(row)) if row else None


async def get_userbot_session_by_bot(db_path: str, bot_id: int) -> dict | None:
    """A bot is expected to have at most one CURRENT userbot session — most
    recent row wins if somehow more than one exists (e.g. after a revoke +
    re-authorization)."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM userbot_sessions WHERE bot_id = ? ORDER BY id DESC LIMIT 1",
            (bot_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return _decrypt_userbot_row(dict(row)) if row else None


async def get_userbot_sessions_by_bot(db_path: str, bot_id: int) -> list[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM userbot_sessions WHERE bot_id = ? ORDER BY created_at DESC",
            (bot_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [_decrypt_userbot_row(dict(row)) for row in rows]


async def get_active_userbot_sessions(db_path: str) -> list[dict]:
    """Used by runtime/userbot_worker.py on startup to restore every active
    client for THIS bot's db_path — same role restore_bots() plays for tenant
    bots in main.py. userbot_worker.py iterates every bot with the
    channel_monitor feature enabled and calls this per db_path."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM userbot_sessions WHERE status = 'active'"
        ) as cursor:
            rows = await cursor.fetchall()
            return [_decrypt_userbot_row(dict(row)) for row in rows]


# ── monitored_channels (channel_monitor) ────────────────────────────────────

async def add_monitored_channel(
    db_path: str,
    bot_id: int,
    channel_username: str | None,
    channel_id: int | None,
    channel_title: str | None,
) -> int:
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "INSERT INTO monitored_channels (bot_id, channel_username, channel_id, channel_title) "
            "VALUES (?, ?, ?, ?)",
            (bot_id, channel_username, channel_id, channel_title),
        )
        await db.commit()
        return cursor.lastrowid


async def get_monitored_channels(db_path: str, bot_id: int, active_only: bool = False) -> list[dict]:
    query = "SELECT * FROM monitored_channels WHERE bot_id = ?"
    params: list = [bot_id]
    if active_only:
        query += " AND active = 1"
    query += " ORDER BY added_at DESC"
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def get_monitored_channels_by_bot(db_path: str, bot_id: int) -> list[dict]:
    """Alias for get_monitored_channels(db_path, bot_id) — kept as a separate
    name matching features/channel_monitor.py's call sites (mirrors
    get_userbot_sessions_by_bot's naming), same query underneath."""
    return await get_monitored_channels(db_path, bot_id)


async def get_monitored_channel(db_path: str, channel_row_id: int) -> dict | None:
    """channel_row_id is monitored_channels.id (the PK), not the Telegram
    channel id — same naming ambiguity as bot_id elsewhere in this file."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM monitored_channels WHERE id = ?", (channel_row_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def get_all_active_monitored_channels(db_path: str) -> list[dict]:
    """Used by runtime/userbot_worker.py to know which channels each restored
    client should subscribe events.NewMessage on, for THIS bot's db_path."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM monitored_channels WHERE active = 1"
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def get_active_monitored_channels(db_path: str) -> list[dict]:
    """Alias for get_all_active_monitored_channels — matches
    runtime/userbot_worker.py's call-site naming."""
    return await get_all_active_monitored_channels(db_path)


async def set_monitored_channel_active(db_path: str, channel_row_id: int, active: bool) -> None:
    """Toggle used by the "📋 Мои каналы" list — channel_row_id is monitored_channels.id."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE monitored_channels SET active = ? WHERE id = ?",
            (1 if active else 0, channel_row_id),
        )
        await db.commit()


async def set_monitored_channel_resolved(
    db_path: str, channel_row_id: int, channel_id: int, channel_title: str | None
) -> None:
    """Fills in channel_id/channel_title once client.get_entity() resolves the
    @username the client typed — see design §5 (formerly §4)."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE monitored_channels SET channel_id = ?, channel_title = ? WHERE id = ?",
            (channel_id, channel_title, channel_row_id),
        )
        await db.commit()


# ── channel_posts (channel_monitor) ─────────────────────────────────────────

async def add_channel_post(db_path: str, monitored_channel_id: int, message_id: int | None, text: str | None) -> int:
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "INSERT INTO channel_posts (monitored_channel_id, message_id, text) VALUES (?, ?, ?)",
            (monitored_channel_id, message_id, text),
        )
        await db.commit()
        return cursor.lastrowid


async def set_channel_post_summary(db_path: str, post_id: int, summary: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE channel_posts SET summary = ? WHERE id = ?", (summary, post_id)
        )
        await db.commit()


async def get_posts_missing_summary(db_path: str, limit: int = 20) -> list[dict]:
    """Used by the background Gemini-summarization loop in
    runtime/userbot_worker.py to pick up posts batched since the last run,
    for THIS bot's db_path, oldest first so a backlog drains in order."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM channel_posts WHERE summary IS NULL ORDER BY id ASC LIMIT ?",
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def get_recent_posts_for_bot(db_path: str, bot_id: int, limit: int = 10) -> list[dict]:
    """Powers the «📰 Лента» screen — most recent posts across this bot's
    ACTIVE monitored channels, newest first. Joins in channel_title/
    channel_username so the feed can label which channel each post came from."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT cp.*, mc.channel_title, mc.channel_username
            FROM channel_posts cp
            JOIN monitored_channels mc ON mc.id = cp.monitored_channel_id
            WHERE mc.bot_id = ? AND mc.active = 1
            ORDER BY cp.id DESC
            LIMIT ?
            """,
            (bot_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]
