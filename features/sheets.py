# FEATURE: sheets
# COMPATIBLE_WITH: accountant, booking_beauty, booking_fitness, booking_medical, booking_restaurant, campaign_tracker, event_manager, expense_tracker, inventory, loyalty_program, manager_secretary, moderator, orders_tracker, referral_program, shop_catalog, support_tickets, tourist_documents, trip_manager
"""Reusable Google Sheets feature module — see sheets-feature-inventory for
the design decisions behind this file.

Unlike features/payments.py, this module has NO Config Protocol and NO
init_db: it never writes into a host template's own db_path. All state it
needs is either (a) a shared, factory-wide Service Account credential —
config.GOOGLE_SHEETS_SA_KEY_B64 (base64 JSON, decoded in memory) if set,
else config.GOOGLE_SHEETS_SA_KEY_PATH (a key file on disk); see
_load_credentials_info() — one robot for every bot on the platform, not
per-bot — or (b) db/database.py's bot_sheets_config table (which bot is
connected to which spreadsheet_id — factory-side, same tier as
bot_payment_providers, not host-template data). Callers pass bot_id
explicitly to every function, exactly like features/payments.py's
create_invoice(bot, bot_id, ...) — see tests/fixtures/payment_fixture_template.py
for the established convention of a host template threading its own
bot_id through its Config into calls like this.

`router` is exported empty for structural 1:1 with the by-convention
interface runtime/registry.py's _load_and_include_features() expects
(getattr(module, "router", None) — a missing router only warns and skips
the include step, never breaks the bot). There is no Telegram-native event
to hook here (unlike payments' pre_checkout_query/successful_payment) —
every function below is called programmatically from a host template's own
handlers, never registered as a handler itself.

gspread is a SYNCHRONOUS library (requests under the hood, not aiohttp) —
every call into it below is wrapped in asyncio.to_thread so a slow Google
API round trip never blocks the shared event loop other bots run on (same
class of bug Phase A of the payments work fixed for pre_checkout_query).

Scope choice — confirmed by a live smoke test against a spreadsheet shared
through the normal Sheets UI (not created by this Service Account, not
opened via a Picker): the 'drive.file' scope does NOT grant Drive-API-level
access to such a file (404). This is irrelevant to write_row/read_data/
verify_access below, because gspread's open_by_key() talks to the Sheets
API directly and never touches the Drive API — confirmed working with
'spreadsheets' scope alone. 'drive.file' is exercised ONLY by create_sheet()
below, where the Service Account itself creates the file — a case Google's
docs describe drive.file as unconditionally covering, regardless of the
smoke-tested limitation above.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging

import gspread
from aiogram import Router

from config import GOOGLE_SHEETS_SA_KEY_B64, GOOGLE_SHEETS_SA_KEY_PATH
from db.database import get_bot_sheets_config

logger = logging.getLogger(__name__)

router = Router()

_SCOPES = (
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
)

_client: gspread.Client | None = None
_client_lock = asyncio.Lock()
_sa_email: str | None = None


def _load_credentials_info() -> dict | None:
    """Resolves the Service Account credential as a dict, in priority order:
    GOOGLE_SHEETS_SA_KEY_B64 first (base64 of the JSON key, decoded here in
    memory and NEVER written to disk — added because pasting the raw JSON key
    file through Railway's Console proved unreliable while Railway Variables
    did not), then the legacy GOOGLE_SHEETS_SA_KEY_PATH file, for backward
    compatibility. Returns None if neither is set, or the one that is set is
    unreadable/invalid — callers treat that the same as "not configured"."""
    if GOOGLE_SHEETS_SA_KEY_B64:
        try:
            return json.loads(base64.b64decode(GOOGLE_SHEETS_SA_KEY_B64))
        except (ValueError, json.JSONDecodeError):
            logger.error("_load_credentials_info: GOOGLE_SHEETS_SA_KEY_B64 is not valid base64/JSON", exc_info=True)
            return None
    if not GOOGLE_SHEETS_SA_KEY_PATH:
        return None
    try:
        with open(GOOGLE_SHEETS_SA_KEY_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        logger.error(f"_load_credentials_info: could not read {GOOGLE_SHEETS_SA_KEY_PATH!r}", exc_info=True)
        return None


async def get_service_account_email() -> str | None:
    """Reads client_email out of the resolved credential — the connect FSM
    (handlers/manage_bots.py) shows this so the owner knows exactly which
    account to share their spreadsheet with. Returns None if no credential
    is configured/readable (same "feature unavailable" signal _get_client()
    would raise on, but this one is read on every panel render, not just on
    first use — an unreadable credential must not crash the features panel,
    just hide the sheets connect option)."""
    global _sa_email
    if _sa_email is not None:
        return _sa_email
    info = await asyncio.to_thread(_load_credentials_info)
    if info is None:
        return None
    _sa_email = info.get("client_email")
    return _sa_email


async def _get_client() -> gspread.Client:
    """Lazy singleton — built once from the shared Service Account
    credential, then reused for every bot. asyncio.Lock guards against two
    concurrent callers both building it on first use; both reading the
    credential and gspread.service_account_from_dict() are synchronous,
    hence the to_thread wraps even for this one-time setup."""
    global _client
    if _client is not None:
        return _client
    async with _client_lock:
        if _client is None:
            info = await asyncio.to_thread(_load_credentials_info)
            if info is None:
                raise ValueError(
                    "Neither GOOGLE_SHEETS_SA_KEY_B64 nor GOOGLE_SHEETS_SA_KEY_PATH is set/readable "
                    "in .env — the sheets feature has no Service Account to act as."
                )
            _client = await asyncio.to_thread(gspread.service_account_from_dict, info, scopes=_SCOPES)
    return _client


async def create_sheet(title: str, owner_email: str) -> str:
    """Creates a new spreadsheet as the shared Service Account and shares it
    to owner_email (Drive API permissions.create, role='writer') so the real
    human sees it in their own Drive. Returns the new spreadsheet_id.

    Deliberately does NOT touch bot_sheets_config — persisting the returned
    id for a specific bot is the caller's decision, not this function's."""
    gc = await _get_client()

    def _create() -> str:
        spreadsheet = gc.create(title)
        spreadsheet.share(owner_email, perm_type="user", role="writer")
        return spreadsheet.id

    spreadsheet_id = await asyncio.to_thread(_create)
    # Owner's email is logged partially masked — Railway logs are not a place
    # to keep a plaintext email trail (review found this, low severity but
    # cheap to avoid).
    masked_email = owner_email[:2] + "***@" + owner_email.split("@")[-1] if "@" in owner_email else "***"
    logger.info(f"create_sheet: created spreadsheet_id={spreadsheet_id} title={title!r} shared_to={masked_email}")
    return spreadsheet_id


async def write_row(bot_id: int, worksheet: str, row: list) -> None:
    """Appends `row` to `worksheet` in bot_id's connected spreadsheet
    (creating the worksheet if it doesn't exist yet). Raises ValueError if
    bot_id has no connected sheet — same contract as create_invoice()
    raising when there's no configured payment provider."""
    sheet_config = await get_bot_sheets_config(bot_id)
    if sheet_config is None:
        raise ValueError(f"write_row: no Google Sheet connected for bot_id={bot_id}")
    gc = await _get_client()

    def _write() -> None:
        spreadsheet = gc.open_by_key(sheet_config["spreadsheet_id"])
        try:
            ws = spreadsheet.worksheet(worksheet)
        except gspread.exceptions.WorksheetNotFound:
            ws = spreadsheet.add_worksheet(title=worksheet, rows=100, cols=max(len(row), 1))
        ws.append_row(row)

    try:
        await asyncio.to_thread(_write)
    except Exception:
        # Not swallowed — no active caller exists yet (no template wires this
        # in), so there's no established "expected exception" contract to
        # narrow this to (unlike payments.py's IntegrityError/OperationalError
        # split, which came from a known caller). Logged so a quota/expired-
        # credential failure is distinguishable from "bad worksheet name" in
        # Railway logs once a template does start calling this.
        logger.error(f"write_row: FAILED bot_id={bot_id} worksheet={worksheet!r}", exc_info=True)
        raise
    logger.info(f"write_row: bot_id={bot_id} worksheet={worksheet!r} appended 1 row")


async def read_data(bot_id: int, worksheet: str) -> list[list[str]]:
    """Returns worksheet.get_all_values() for bot_id's connected spreadsheet.
    Raises ValueError if bot_id has no connected sheet (same contract as
    write_row) and gspread.exceptions.WorksheetNotFound if the sheet exists
    but that specific worksheet tab doesn't."""
    sheet_config = await get_bot_sheets_config(bot_id)
    if sheet_config is None:
        raise ValueError(f"read_data: no Google Sheet connected for bot_id={bot_id}")
    gc = await _get_client()

    def _read() -> list[list[str]]:
        spreadsheet = gc.open_by_key(sheet_config["spreadsheet_id"])
        return spreadsheet.worksheet(worksheet).get_all_values()

    try:
        return await asyncio.to_thread(_read)
    except Exception:
        logger.error(f"read_data: FAILED bot_id={bot_id} worksheet={worksheet!r}", exc_info=True)
        raise


async def verify_access(spreadsheet_id: str) -> str | None:
    """Attempts to open spreadsheet_id with the shared Service Account via
    the Sheets API (gc.open_by_key — never touches the Drive API, see module
    docstring). Returns the spreadsheet's title on success, None on ANY
    failure — including _get_client() itself (a missing/corrupt SA key file),
    not just gspread's own not-found/API errors. Never raises: the connect
    FSM (handlers/manage_bots.py) calls this BEFORE saving to
    bot_sheets_config and must always get a clean re-prompt back, never an
    unhandled exception that leaves the owner's message unanswered."""
    try:
        gc = await _get_client()

        def _verify() -> str | None:
            try:
                return gc.open_by_key(spreadsheet_id).title
            except (gspread.exceptions.SpreadsheetNotFound, gspread.exceptions.APIError):
                return None

        return await asyncio.to_thread(_verify)
    except Exception:
        logger.error(f"verify_access: FAILED spreadsheet_id={spreadsheet_id!r}", exc_info=True)
        return None
