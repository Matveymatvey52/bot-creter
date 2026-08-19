"""Stage D: detect URLs the user pastes during /create gathering and turn them
into extra context for chat_gather_requirements — either a CSV export of a
Google Sheet, or the plain-text content of an ordinary web page.

Two ways to unlock a private Google Sheet are supported:
  1. Sharing link ("Anyone with the link") — no auth, works today.
  2. Google OAuth account connection — nicer UX, not implemented yet; the
     UI always offers the choice, OAuth is marked "coming soon" until a
     later iteration wires it up (see OAUTH_ENABLED below).
"""

from __future__ import annotations

import csv
import io
import logging
import re
from enum import Enum
from typing import NamedTuple

import aiohttp

logger = logging.getLogger(__name__)

URL_RE = re.compile(r"https?://[^\s<>\"']+")

_SHEETS_RE = re.compile(
    r"https?://docs\.google\.com/spreadsheets/d/([a-zA-Z0-9_-]+)"
)

MAX_FETCH_BYTES = 200_000
MAX_CONTEXT_CHARS = 4_000
FETCH_TIMEOUT = 10.0

# Flip this on once the OAuth flow (Google account connect) actually exists.
# Until then the UI always shows the OAuth button, greyed out as "coming soon".
OAUTH_ENABLED = False


class SheetsAuthMethod(str, Enum):
    SHARE_LINK = "share_link"
    OAUTH = "oauth"


class LinkKind(str, Enum):
    GOOGLE_SHEET = "google_sheet"
    WEBPAGE = "webpage"


class LinkResult(NamedTuple):
    url: str
    kind: LinkKind
    ok: bool
    context: str | None  # text to feed into the gathering conversation
    error: str | None    # user-facing reason it failed (e.g. sheet not public)


def extract_urls(text: str) -> list[str]:
    return URL_RE.findall(text)


def _sheet_id(url: str) -> str | None:
    match = _SHEETS_RE.search(url)
    return match.group(1) if match else None


async def resolve_link(url: str) -> LinkResult:
    sheet_id = _sheet_id(url)
    if sheet_id:
        return await _resolve_google_sheet(url, sheet_id)
    return await _resolve_webpage(url)


async def _resolve_google_sheet(url: str, sheet_id: str) -> LinkResult:
    export_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                export_url, timeout=aiohttp.ClientTimeout(total=FETCH_TIMEOUT),
                allow_redirects=True,
            ) as resp:
                if resp.status != 200:
                    return LinkResult(
                        url=url, kind=LinkKind.GOOGLE_SHEET, ok=False, context=None,
                        error="not_public",
                    )
                content_type = resp.headers.get("Content-Type", "")
                body = await resp.content.read(MAX_FETCH_BYTES)
                # Google redirects to the login page (HTML) for private sheets
                # instead of returning a non-200 — detect that case too.
                if "text/csv" not in content_type and "text/html" in content_type:
                    return LinkResult(
                        url=url, kind=LinkKind.GOOGLE_SHEET, ok=False, context=None,
                        error="not_public",
                    )
    except Exception as e:
        logger.warning(f"Google Sheet export fetch failed for {url}: {e}")
        return LinkResult(
            url=url, kind=LinkKind.GOOGLE_SHEET, ok=False, context=None,
            error="fetch_failed",
        )

    text = body.decode("utf-8", errors="replace")
    context = _summarize_csv(text)
    if context is None:
        return LinkResult(
            url=url, kind=LinkKind.GOOGLE_SHEET, ok=False, context=None,
            error="empty_or_invalid",
        )
    return LinkResult(url=url, kind=LinkKind.GOOGLE_SHEET, ok=True, context=context, error=None)


def _summarize_csv(csv_text: str) -> str | None:
    reader = csv.reader(io.StringIO(csv_text))
    rows = list(reader)
    if not rows:
        return None
    header = rows[0]
    sample_rows = rows[1:6]
    lines = [
        f"Google Sheet columns: {', '.join(header)}",
        f"Total data rows: {len(rows) - 1}",
        "Sample rows:",
    ]
    for row in sample_rows:
        lines.append(" | ".join(row))
    return "\n".join(lines)[:MAX_CONTEXT_CHARS]


async def _resolve_webpage(url: str) -> LinkResult:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=FETCH_TIMEOUT),
                allow_redirects=True,
            ) as resp:
                if resp.status != 200:
                    return LinkResult(
                        url=url, kind=LinkKind.WEBPAGE, ok=False, context=None,
                        error="fetch_failed",
                    )
                raw = await resp.content.read(MAX_FETCH_BYTES)
    except Exception as e:
        logger.warning(f"Webpage fetch failed for {url}: {e}")
        return LinkResult(url=url, kind=LinkKind.WEBPAGE, ok=False, context=None, error="fetch_failed")

    html = raw.decode("utf-8", errors="replace")
    text = _strip_html(html).strip()
    if not text:
        return LinkResult(url=url, kind=LinkKind.WEBPAGE, ok=False, context=None, error="empty_or_invalid")
    return LinkResult(url=url, kind=LinkKind.WEBPAGE, ok=True, context=text[:MAX_CONTEXT_CHARS], error=None)


_TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_ANY_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"[ \t]+")
_BLANK_LINES_RE = re.compile(r"\n\s*\n+")


def _strip_html(html: str) -> str:
    no_scripts = _TAG_RE.sub(" ", html)
    text = _ANY_TAG_RE.sub(" ", no_scripts)
    text = _WHITESPACE_RE.sub(" ", text)
    text = _BLANK_LINES_RE.sub("\n", text)
    return text


SHEET_SHARE_INSTRUCTIONS = (
    "🔒 Эта Google Таблица не публична, я не смог её прочитать.\n\n"
    "Как открыть доступ по ссылке:\n"
    "1️⃣ Откройте таблицу в браузере\n"
    "2️⃣ Нажмите «Настройки доступа» (или «Share») в правом верхнем углу\n"
    "3️⃣ В разделе «Общий доступ» выберите «Все, у кого есть ссылка»\n"
    "4️⃣ Скопируйте ссылку и пришлите её мне ещё раз\n\n"
    "Так я смогу прочитать структуру таблицы (столбцы, пример данных) и учесть это при создании бота."
)
