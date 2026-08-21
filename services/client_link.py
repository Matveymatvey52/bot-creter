"""Linking a record an admin created "for @someone" to that someone's real
Telegram id, once they actually show up.

Why this exists: templates like car_rental/event_rsvp store the customer as a
numeric `client_user_id` and send them notifications through it
(features/notifications.py's recipient_field / recipient_query, which feed
Telegram's chat_id). An admin filling in a booking from the mini-app knows the
customer's @username, never their numeric id — and Telegram's sendMessage
does NOT accept @username for private chats, so storing the username in that
column instead would leave the row looking fine while every notification to
it silently failed.

So the two live side by side: the admin types a username into a new
`client_username` column, `client_user_id` is written as the sentinel 0
("not linked yet"), and the first time that person messages the bot,
link_pending_by_username() fills in their real id. Notifications keep going
through client_user_id exactly as before; they simply don't reach a customer
who has never contacted the bot, which is the true state of affairs rather
than a bug.
"""

from __future__ import annotations

import logging
import re

import aiosqlite

logger = logging.getLogger(__name__)

# The sentinel written to the numeric id column for a record whose customer
# hasn't been matched to a Telegram account yet. 0 is safe: Telegram user ids
# are positive, so it can never collide with a real one, and the column stays
# INTEGER NOT NULL as declared.
UNLINKED_USER_ID = 0

# Telegram's own username rules: 5-32 chars, letters/digits/underscore.
# Applied on the way IN so a typo becomes a visible rejection at create time
# rather than a row that can never link to anybody.
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")


def normalize_username(raw: object) -> str | None:
    """Accepts "@ivanov", "ivanov", or " Ivanov " and returns the bare
    lowercase handle, or None when it isn't a usable Telegram username.

    Stored lowercase because Telegram usernames are case-insensitive: a row
    saved as "@Ivanov" must still match an incoming update whose from_user
    reports "ivanov".
    """
    if not isinstance(raw, str):
        return None
    handle = raw.strip().lstrip("@").strip()
    if not USERNAME_RE.match(handle):
        return None
    return handle.lower()


async def ensure_username_column(
    db: aiosqlite.Connection, table: str, username_column: str = "client_username"
) -> None:
    """Adds the username column to an existing install.

    Same try/except ALTER idiom the templates already use for their own
    backfills (see booking_fitness.py's subscriptions.autorenew): CREATE TABLE
    IF NOT EXISTS is a no-op against a table an already-running bot created
    before this shipped, so the column needs its own migration path.
    "duplicate column" on a fresh or already-migrated DB is expected.
    """
    try:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {username_column} TEXT")
    except Exception:
        pass


async def link_pending_by_username(
    db_path: str,
    table: str,
    telegram_user_id: int,
    username: str | None,
    id_column: str = "client_user_id",
    username_column: str = "client_username",
) -> int:
    """Claims every still-unlinked row addressed to this username, returning
    how many were linked.

    Only touches rows still holding the sentinel: a row already carrying a
    real id is never rewritten, so someone adopting an @handle that appears in
    an old record cannot hijack an existing customer's bookings.
    """
    handle = normalize_username(username)
    if not handle:
        return 0
    try:
        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute(
                f"UPDATE {table} SET {id_column} = ?"
                f" WHERE LOWER({username_column}) = ? AND {id_column} = ?",
                (telegram_user_id, handle, UNLINKED_USER_ID),
            )
            await db.commit()
            return cursor.rowcount or 0
    except Exception:
        # Linking is opportunistic housekeeping on someone else's message —
        # it must never break the handler that triggered it.
        logger.exception("link_pending_by_username: table=%s failed", table)
        return 0
