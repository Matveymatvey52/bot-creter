"""services/claude_service.py's voice-intake/cashflow-ledger config generator
(docs/VOICE_CASHFLOW_FROM_SCRATCH_DESIGN.md) —
_generate_voice_cashflow_config/_parse_voice_cashflow_config/
_validate_voice_cashflow_config. Same mocking pattern as
tests/test_miniapp_config_claude_service.py: patch claude_service's
module-level `client`, never hit the real Anthropic API.

Central contract under test: this step must NEVER raise and NEVER block bot
creation — any malformed or hallucinated output degrades to None (or, for
cashflow_ledger, to False), and a hallucinated voice_intake table/column
degrades only the voice_intake portion, not the whole response.
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
            CREATE TABLE IF NOT EXISTS warehouses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                warehouse_id INTEGER,
                title TEXT,
                quantity INTEGER
            )
        """)
        await db.commit()
'''

VALID_VOICE_INTAKE_CONFIG_JSON = """{
    "voice_intake": {
        "context_table": "warehouses",
        "context_column": "id",
        "record_types": [
            {
                "key": "item", "label": "Item", "icon": "📦",
                "prompt_desc": "title, quantity",
                "table": "items",
                "context_column": "warehouse_id",
                "fields": [
                    {"name": "title", "label": "Title", "column": "title"},
                    {"name": "quantity", "label": "Quantity", "column": "quantity"}
                ]
            }
        ]
    },
    "cashflow_ledger": false
}"""

NEITHER_FEATURE_JSON = """{"voice_intake": null, "cashflow_ledger": false}"""

CASHFLOW_ONLY_JSON = """{"voice_intake": null, "cashflow_ledger": true}"""

HALLUCINATED_TABLE_JSON = """{
    "voice_intake": {
        "context_table": null, "context_column": null,
        "record_types": [
            {
                "key": "item", "label": "Item", "icon": "📦",
                "prompt_desc": "title",
                "table": "does_not_exist",
                "context_column": null,
                "fields": [{"name": "title", "label": "Title", "column": "title"}]
            }
        ]
    },
    "cashflow_ledger": true
}"""


def _fake_response(text: str) -> SimpleNamespace:
    return SimpleNamespace(content=[SimpleNamespace(text=text)])


class ValidateVoiceCashflowConfig(unittest.TestCase):
    def test_valid_config_passes_through(self):
        import json
        data = json.loads(VALID_VOICE_INTAKE_CONFIG_JSON)
        result = claude_service._validate_voice_cashflow_config(data, SAMPLE_BOT_CODE)
        self.assertIsNotNone(result)
        self.assertEqual(result["voice_intake"]["record_types"][0]["table"], "items")
        self.assertFalse(result["cashflow_ledger"])

    def test_neither_feature_returns_none(self):
        import json
        data = json.loads(NEITHER_FEATURE_JSON)
        result = claude_service._validate_voice_cashflow_config(data, SAMPLE_BOT_CODE)
        self.assertIsNone(result)

    def test_cashflow_only_is_kept_even_with_null_voice_intake(self):
        import json
        data = json.loads(CASHFLOW_ONLY_JSON)
        result = claude_service._validate_voice_cashflow_config(data, SAMPLE_BOT_CODE)
        self.assertIsNotNone(result)
        self.assertIsNone(result["voice_intake"])
        self.assertTrue(result["cashflow_ledger"])

    def test_hallucinated_table_drops_voice_intake_but_keeps_cashflow_flag(self):
        import json
        data = json.loads(HALLUCINATED_TABLE_JSON)
        result = claude_service._validate_voice_cashflow_config(data, SAMPLE_BOT_CODE)
        self.assertIsNotNone(result)
        self.assertIsNone(result["voice_intake"])
        self.assertTrue(result["cashflow_ledger"])

    def test_non_dict_input_returns_none(self):
        self.assertIsNone(claude_service._validate_voice_cashflow_config([], SAMPLE_BOT_CODE))

    def test_non_bool_cashflow_ledger_defaults_to_false(self):
        data = {"voice_intake": None, "cashflow_ledger": "yes"}
        # Neither a valid voice_intake nor a real bool cashflow_ledger -> None
        self.assertIsNone(claude_service._validate_voice_cashflow_config(data, SAMPLE_BOT_CODE))


class ParseVoiceCashflowConfig(unittest.TestCase):
    def test_malformed_json_returns_none(self):
        result = claude_service._parse_voice_cashflow_config("garbage{{{", SAMPLE_BOT_CODE)
        self.assertIsNone(result)

    def test_valid_json_parses_and_validates(self):
        result = claude_service._parse_voice_cashflow_config(VALID_VOICE_INTAKE_CONFIG_JSON, SAMPLE_BOT_CODE)
        self.assertIsNotNone(result)
        self.assertEqual(result["voice_intake"]["record_types"][0]["table"], "items")


class GenerateVoiceCashflowConfig(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._patcher = patch.object(claude_service, "client")
        self.mock_client = self._patcher.start()

    async def asyncTearDown(self):
        self._patcher.stop()

    async def test_uses_haiku_and_the_voice_cashflow_prompt(self):
        create = AsyncMock(return_value=_fake_response(VALID_VOICE_INTAKE_CONFIG_JSON))
        self.mock_client.messages.create = create
        result = await claude_service._generate_voice_cashflow_config(SAMPLE_BOT_CODE, "an inventory bot")
        _, kwargs = create.call_args
        self.assertEqual(kwargs["model"], "claude-haiku-4-5-20251001")
        self.assertEqual(kwargs["system"], claude_service.VOICE_CASHFLOW_CONFIG_SYSTEM_PROMPT)
        self.assertIsNotNone(result)
        self.assertEqual(result["voice_intake"]["record_types"][0]["table"], "items")

    async def test_few_shot_example_is_the_real_tour_operator_config(self):
        self.assertIn('"locations"', claude_service.VOICE_CASHFLOW_CONFIG_SYSTEM_PROMPT)
        self.assertIn('"user_prefs"', claude_service.VOICE_CASHFLOW_CONFIG_SYSTEM_PROMPT)

    async def test_api_exception_never_raises_returns_none(self):
        self.mock_client.messages.create = AsyncMock(side_effect=RuntimeError("network broke"))
        result = await claude_service._generate_voice_cashflow_config(SAMPLE_BOT_CODE, "whatever")
        self.assertIsNone(result)

    async def test_malformed_response_never_raises_returns_none(self):
        self.mock_client.messages.create = AsyncMock(return_value=_fake_response("garbage{{{"))
        result = await claude_service._generate_voice_cashflow_config(SAMPLE_BOT_CODE, "whatever")
        self.assertIsNone(result)

    async def test_empty_content_list_never_raises_returns_none(self):
        # A refusal or a max_tokens cutoff before any content block is
        # emitted can leave response.content == [] — accessing content[0]
        # must stay inside the try/except, not raise IndexError past it.
        self.mock_client.messages.create = AsyncMock(return_value=SimpleNamespace(content=[]))
        result = await claude_service._generate_voice_cashflow_config(SAMPLE_BOT_CODE, "whatever")
        self.assertIsNone(result)

    async def test_neither_feature_response_returns_none(self):
        self.mock_client.messages.create = AsyncMock(return_value=_fake_response(NEITHER_FEATURE_JSON))
        result = await claude_service._generate_voice_cashflow_config(SAMPLE_BOT_CODE, "whatever")
        self.assertIsNone(result)

    async def test_max_tokens_is_small_not_full_generation_size(self):
        create = AsyncMock(return_value=_fake_response(VALID_VOICE_INTAKE_CONFIG_JSON))
        self.mock_client.messages.create = create
        await claude_service._generate_voice_cashflow_config(SAMPLE_BOT_CODE, "whatever")
        _, kwargs = create.call_args
        self.assertLessEqual(kwargs["max_tokens"], 2000)


if __name__ == "__main__":
    unittest.main()
