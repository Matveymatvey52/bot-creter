"""How a record identifies the customer it belongs to, and how it finds their
real Telegram id later.

The engine-wide rule (applies to every template, not just the ones wired up
today): a record about a person carries a CONTACT — free text that identifies
them however the admin actually knows them. "@ivanov", "Иван Петров",
"+7 999 123-45-67" are all valid. It is required, because a customer record
with nothing identifying the customer is useless.

Separately, templates like car_rental/event_rsvp reach that person through a
numeric `client_user_id` (features/notifications.py's recipient_field /
recipient_query feed it to Telegram as chat_id). Telegram's sendMessage does
NOT accept @username for private chats, so the contact can never simply
replace that column — the two live side by side. The numeric column is
written as the sentinel 0 ("nobody linked yet") and filled in automatically
only when the contact happens to be a Telegram username AND that exact person
messages the bot.

When the contact is a phone number or a full name there is no automatic link,
by design: nothing in an incoming update can be matched against it with any
confidence. Such a record simply stays unlinked, which is the truth about it,
rather than being guessed at.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import aiosqlite

logger = logging.getLogger(__name__)

# The sentinel written to the numeric id column for a record whose customer
# hasn't been matched to a Telegram account. 0 is safe: Telegram user ids are
# positive, so it can never collide with a real one, and the column stays
# INTEGER NOT NULL as declared.
UNLINKED_USER_ID = 0

# Telegram's own username rules: 5-32 chars, letters/digits/underscore.
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")


class ContactFormatError(ValueError):
    """Raised for a contact written as an @handle that isn't a valid Telegram
    username. Only @-prefixed input is held to that standard — anything else
    is free-form contact text and can't be "wrong"."""


@dataclass(frozen=True)
class Contact:
    """A parsed contact. `username` is set only when the text is genuinely a
    Telegram handle, which is exactly when automatic linking can work."""

    value: str
    username: str | None

    @property
    def is_username(self) -> bool:
        return self.username is not None


def parse_contact(raw: object) -> Contact | None:
    """Normalizes one contact value, or returns None when there is nothing
    usable (empty/blank/not a string).

    Strict username validation applies ONLY when the input looks like it is
    meant to be a username — either @-prefixed, or already in the exact
    Telegram handle shape. Everything else is stored verbatim (just trimmed):
    a phone number or a person's name is a perfectly good contact and must
    not be rejected for failing rules that were never meant to apply to it.

    Raises ContactFormatError for "@" + something that isn't a valid handle,
    since that input states an intent the value doesn't satisfy — silently
    keeping it as free text would produce a record that looks linkable and
    never links.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None

    if text.startswith("@"):
        handle = text[1:].strip()
        if not USERNAME_RE.match(handle):
            raise ContactFormatError(text)
        return Contact(value=handle.lower(), username=handle.lower())

    # Not @-prefixed: treat as a username only if it already has that exact
    # shape, otherwise keep it as ordinary contact text.
    if USERNAME_RE.match(text):
        return Contact(value=text.lower(), username=text.lower())
    return Contact(value=text, username=None)


def extract_username(raw: object) -> str | None:
    """The bare lowercase handle when `raw` is a Telegram username, else None.
    Never raises — used on incoming updates, where a malformed value just
    means "can't link", not "reject this message"."""
    try:
        contact = parse_contact(raw)
    except ContactFormatError:
        return None
    return contact.username if contact else None


async def ensure_contact_column(
    db: aiosqlite.Connection, table: str, contact_column: str = "client_contact"
) -> None:
    """Adds the contact column to an existing install.

    Same try/except ALTER idiom the templates already use for their own
    backfills (see booking_fitness.py's subscriptions.autorenew): CREATE TABLE
    IF NOT EXISTS is a no-op against a table an already-running bot created
    before this shipped, so the column needs its own migration path.
    "duplicate column" on a fresh or already-migrated DB is expected.
    """
    try:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {contact_column} TEXT")
    except Exception:
        pass


async def link_pending_by_username(
    db_path: str,
    table: str,
    telegram_user_id: int,
    username: str | None,
    id_column: str = "client_user_id",
    contact_column: str = "client_contact",
) -> int:
    """Claims every still-unlinked row whose contact is this person's Telegram
    username, returning how many were linked.

    Only rows still holding the sentinel are touched, so someone adopting an
    @handle that appears in an older record cannot hijack that customer's
    bookings. Rows whose contact is a phone or a name never match here — see
    the module docstring on why that is deliberate.
    """
    handle = extract_username(username)
    if not handle:
        return 0
    try:
        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute(
                f"UPDATE {table} SET {id_column} = ?"
                f" WHERE LOWER({contact_column}) = ? AND {id_column} = ?",
                (telegram_user_id, handle, UNLINKED_USER_ID),
            )
            await db.commit()
            return cursor.rowcount or 0
    except Exception:
        # Linking is opportunistic housekeeping on someone else's message —
        # it must never break the handler that triggered it.
        logger.exception("link_pending_by_username: table=%s failed", table)
        return 0
