"""services/claude_service.py's office-hook config generator (see
docs/OFFICES_DESIGN.md §11) — _generate_office_hook_config/
_parse_office_hook_config. Same mocking pattern as
tests/test_miniapp_config_claude_service.py: patch claude_service's
module-level `client`, never hit the real Anthropic API.

Central contract under test: this step must NEVER raise and NEVER block bot
creation — any malformed or hallucinated output degrades to None.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import services.claude_service as claude_service

SAMPLE_BOT_CODE = '''
import aiosqlite

DB_PATH = "data/bot_1.db"

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS clients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                telegram_id TEXT PRIMARY KEY
            )
        """)
        await db.commit()
'''


def _fake_response(text: str) -> SimpleNamespace:
    return SimpleNamespace(content=[SimpleNamespace(text=text)])


class ParseOfficeHookConfigTests(unittest.TestCase):
    def test_valid_config_with_match_field_parses(self):
        raw = '{"table": "clients", "match_field": "telegram_id"}'
        result = claude_service._parse_office_hook_config(raw, SAMPLE_BOT_CODE)
        self.assertEqual(result, {"table": "clients", "match_field": "telegram_id"})

    def test_valid_config_with_null_match_field_parses(self):
        raw = '{"table": "clients", "match_field": null}'
        result = claude_service._parse_office_hook_config(raw, SAMPLE_BOT_CODE)
        self.assertEqual(result, {"table": "clients", "match_field": None})

    def test_no_table_at_all_returns_none(self):
        raw = '{"table": null, "match_field": null}'
        self.assertIsNone(claude_service._parse_office_hook_config(raw, SAMPLE_BOT_CODE))

    def test_hallucinated_table_returns_none(self):
        raw = '{"table": "does_not_exist", "match_field": "telegram_id"}'
        self.assertIsNone(claude_service._parse_office_hook_config(raw, SAMPLE_BOT_CODE))

    def test_hallucinated_match_field_returns_none(self):
        raw = '{"table": "clients", "match_field": "no_such_column"}'
        self.assertIsNone(claude_service._parse_office_hook_config(raw, SAMPLE_BOT_CODE))

    def test_malformed_json_returns_none(self):
        self.assertIsNone(claude_service._parse_office_hook_config("garbage{{{", SAMPLE_BOT_CODE))

    def test_non_dict_json_returns_none(self):
        self.assertIsNone(claude_service._parse_office_hook_config("[1, 2, 3]", SAMPLE_BOT_CODE))

    def test_wrong_type_for_table_returns_none(self):
        raw = '{"table": 123, "match_field": null}'
        self.assertIsNone(claude_service._parse_office_hook_config(raw, SAMPLE_BOT_CODE))

    def test_wrong_type_for_match_field_returns_none(self):
        raw = '{"table": "clients", "match_field": 123}'
        self.assertIsNone(claude_service._parse_office_hook_config(raw, SAMPLE_BOT_CODE))

    def test_markdown_fenced_response_is_stripped(self):
        raw = '```json\n{"table": "clients", "match_field": "telegram_id"}\n```'
        result = claude_service._parse_office_hook_config(raw, SAMPLE_BOT_CODE)
        self.assertEqual(result, {"table": "clients", "match_field": "telegram_id"})


class GenerateOfficeHookConfigTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._patcher = patch.object(claude_service, "client")
        self.mock_client = self._patcher.start()

    async def asyncTearDown(self):
        self._patcher.stop()

    async def test_valid_response_returns_parsed_config(self):
        self.mock_client.messages.create = AsyncMock(
            return_value=_fake_response('{"table": "clients", "match_field": "telegram_id"}')
        )
        result = await claude_service._generate_office_hook_config(SAMPLE_BOT_CODE, "a client-tracking bot")
        self.assertEqual(result, {"table": "clients", "match_field": "telegram_id"})

    async def test_api_exception_never_raises_returns_none(self):
        self.mock_client.messages.create = AsyncMock(side_effect=RuntimeError("network broke"))
        result = await claude_service._generate_office_hook_config(SAMPLE_BOT_CODE, "whatever")
        self.assertIsNone(result)

    async def test_malformed_response_never_raises_returns_none(self):
        self.mock_client.messages.create = AsyncMock(return_value=_fake_response("garbage{{{"))
        result = await claude_service._generate_office_hook_config(SAMPLE_BOT_CODE, "whatever")
        self.assertIsNone(result)

    async def test_uses_haiku_model_and_small_max_tokens(self):
        create = AsyncMock(return_value=_fake_response('{"table": null, "match_field": null}'))
        self.mock_client.messages.create = create
        await claude_service._generate_office_hook_config(SAMPLE_BOT_CODE, "whatever")
        _, kwargs = create.call_args
        self.assertEqual(kwargs["model"], "claude-haiku-4-5-20251001")
        self.assertLessEqual(kwargs["max_tokens"], 500)


if __name__ == "__main__":
    unittest.main()
