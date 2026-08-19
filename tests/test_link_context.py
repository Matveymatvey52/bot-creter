"""services/link_context.py — Stage D URL detection during /create gathering.

Covers: URL extraction, Google Sheets CSV summary on success, "not public"
detection (both non-200 and the HTML-login-page redirect case), and plain
webpage HTML-to-text extraction. aiohttp is mocked; no real network calls.
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from services.link_context import LinkKind, extract_urls, resolve_link


def _mock_session(status: int, content_type: str, body: bytes):
    resp = MagicMock()
    resp.status = status
    resp.headers = {"Content-Type": content_type}
    resp.content.read = AsyncMock(return_value=body)

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.get = MagicMock(return_value=cm)

    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)
    return session_cm


class ExtractUrls(unittest.TestCase):
    def test_finds_multiple_urls_in_text(self):
        text = "смотри тут https://docs.google.com/spreadsheets/d/abc123/edit и вот https://example.com/page"
        urls = extract_urls(text)
        self.assertEqual(len(urls), 2)
        self.assertIn("https://docs.google.com/spreadsheets/d/abc123/edit", urls)

    def test_no_urls_returns_empty(self):
        self.assertEqual(extract_urls("просто текст без ссылок"), [])


class ResolveGoogleSheet(unittest.IsolatedAsyncioTestCase):
    async def test_public_sheet_returns_csv_summary(self):
        csv_body = b"name,price\nApple,10\nBanana,5\n"
        with patch("aiohttp.ClientSession", return_value=_mock_session(200, "text/csv", csv_body)):
            result = await resolve_link("https://docs.google.com/spreadsheets/d/abc123/edit")
        self.assertTrue(result.ok)
        self.assertEqual(result.kind, LinkKind.GOOGLE_SHEET)
        self.assertIn("name, price", result.context)
        self.assertIn("Apple | 10", result.context)

    async def test_private_sheet_redirect_to_login_html_is_not_public(self):
        with patch("aiohttp.ClientSession", return_value=_mock_session(200, "text/html", b"<html>login</html>")):
            result = await resolve_link("https://docs.google.com/spreadsheets/d/abc123/edit")
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "not_public")

    async def test_private_sheet_non_200_is_not_public(self):
        with patch("aiohttp.ClientSession", return_value=_mock_session(403, "text/html", b"")):
            result = await resolve_link("https://docs.google.com/spreadsheets/d/abc123/edit")
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "not_public")


class ResolveWebpage(unittest.IsolatedAsyncioTestCase):
    async def test_extracts_text_from_html(self):
        html = b"<html><head><style>.x{}</style></head><body><h1>Hello</h1><p>World</p></body></html>"
        with patch("aiohttp.ClientSession", return_value=_mock_session(200, "text/html", html)):
            result = await resolve_link("https://example.com/page")
        self.assertTrue(result.ok)
        self.assertEqual(result.kind, LinkKind.WEBPAGE)
        self.assertIn("Hello", result.context)
        self.assertIn("World", result.context)
        self.assertNotIn("<h1>", result.context)

    async def test_fetch_failure_is_reported(self):
        with patch("aiohttp.ClientSession", return_value=_mock_session(500, "text/html", b"")):
            result = await resolve_link("https://example.com/broken")
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "fetch_failed")


if __name__ == "__main__":
    unittest.main()
