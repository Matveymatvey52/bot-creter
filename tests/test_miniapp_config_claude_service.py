"""services/claude_service.py's mini-app config generator (see
docs/MINIAPP_DESIGN.md §6) — _generate_miniapp_config/_parse_miniapp_config/
_validate_miniapp_config_against_code/_extract_create_table_names. Same
mocking pattern as tests/test_narrow_risk_review.py: patch claude_service's
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
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_name TEXT NOT NULL,
                status TEXT,
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

VALID_CONFIG_JSON = """{
    "resources": [
        {
            "name": "orders",
            "table": "orders",
            "order_by": "created_at DESC",
            "creatable": true,
            "fields": [
                {"name": "customer_name", "required": true},
                {"name": "status"},
                {"name": "created_at", "creatable": false}
            ]
        }
    ]
}"""


def _fake_response(text: str) -> SimpleNamespace:
    return SimpleNamespace(content=[SimpleNamespace(text=text)])


class ExtractCreateTableNames(unittest.TestCase):
    def test_finds_table_and_its_columns(self):
        tables = claude_service._extract_create_table_names(SAMPLE_BOT_CODE)
        self.assertIn("orders", tables)
        self.assertEqual(tables["orders"], {"id", "customer_name", "status", "created_at"})

    def test_finds_multiple_tables(self):
        tables = claude_service._extract_create_table_names(SAMPLE_BOT_CODE)
        self.assertIn("admins", tables)
        self.assertEqual(tables["admins"], {"telegram_id"})

    def test_empty_for_code_with_no_create_table(self):
        self.assertEqual(claude_service._extract_create_table_names("print('hello')"), {})


class ValidateMiniappConfigAgainstCode(unittest.TestCase):
    def test_valid_config_passes(self):
        config = {
            "resources": [
                {"name": "orders", "table": "orders", "fields": [{"name": "customer_name"}]}
            ]
        }
        self.assertTrue(claude_service._validate_miniapp_config_against_code(config, SAMPLE_BOT_CODE))

    def test_hallucinated_table_fails(self):
        config = {
            "resources": [
                {"name": "invoices", "table": "invoices", "fields": [{"name": "amount"}]}
            ]
        }
        self.assertFalse(claude_service._validate_miniapp_config_against_code(config, SAMPLE_BOT_CODE))

    def test_hallucinated_column_fails(self):
        config = {
            "resources": [
                {"name": "orders", "table": "orders", "fields": [{"name": "total_price"}]}
            ]
        }
        self.assertFalse(claude_service._validate_miniapp_config_against_code(config, SAMPLE_BOT_CODE))

    def test_one_bad_resource_invalidates_the_whole_config(self):
        config = {
            "resources": [
                {"name": "orders", "table": "orders", "fields": [{"name": "customer_name"}]},
                {"name": "ghost", "table": "ghost_table", "fields": [{"name": "x"}]},
            ]
        }
        self.assertFalse(claude_service._validate_miniapp_config_against_code(config, SAMPLE_BOT_CODE))

    def test_no_tables_in_code_fails(self):
        config = {"resources": [{"name": "orders", "table": "orders", "fields": [{"name": "x"}]}]}
        self.assertFalse(claude_service._validate_miniapp_config_against_code(config, "no tables here"))


class ParseMiniappConfig(unittest.TestCase):
    def test_valid_json_matching_code_parses(self):
        result = claude_service._parse_miniapp_config(VALID_CONFIG_JSON, SAMPLE_BOT_CODE)
        self.assertIsNotNone(result)
        self.assertEqual(result["resources"][0]["table"], "orders")

    def test_strips_markdown_fences(self):
        fenced = f"```json\n{VALID_CONFIG_JSON}\n```"
        result = claude_service._parse_miniapp_config(fenced, SAMPLE_BOT_CODE)
        self.assertIsNotNone(result)

    def test_malformed_json_returns_none(self):
        self.assertIsNone(claude_service._parse_miniapp_config("not json at all {{{", SAMPLE_BOT_CODE))

    def test_empty_resources_returns_none_not_empty_dict(self):
        # {"resources": []} is a valid "nothing to show" answer from the
        # model, but there's nothing to store — treated as no config.
        self.assertIsNone(claude_service._parse_miniapp_config('{"resources": []}', SAMPLE_BOT_CODE))

    def test_missing_resources_key_returns_none(self):
        self.assertIsNone(claude_service._parse_miniapp_config('{"foo": "bar"}', SAMPLE_BOT_CODE))

    def test_hallucinated_table_returns_none(self):
        bad = '{"resources": [{"name": "x", "table": "nonexistent", "fields": [{"name": "y"}]}]}'
        self.assertIsNone(claude_service._parse_miniapp_config(bad, SAMPLE_BOT_CODE))

    def test_resource_missing_required_keys_returns_none(self):
        bad = '{"resources": [{"name": "orders"}]}'  # no "table", no "fields"
        self.assertIsNone(claude_service._parse_miniapp_config(bad, SAMPLE_BOT_CODE))

    def test_not_a_dict_returns_none(self):
        self.assertIsNone(claude_service._parse_miniapp_config("[1, 2, 3]", SAMPLE_BOT_CODE))

    def test_display_metadata_passes_through_when_present(self):
        with_display = """{
            "resources": [
                {
                    "name": "orders", "table": "orders", "creatable": true,
                    "title": "Заказы", "titleField": "customer_name",
                    "fields": [
                        {"name": "customer_name", "required": true, "label": "Клиент", "kind": "text", "list": true, "detail": true, "create": true},
                        {"name": "status", "label": "Статус", "kind": "status", "list": true, "detail": true, "create": false}
                    ]
                }
            ]
        }"""
        result = claude_service._parse_miniapp_config(with_display, SAMPLE_BOT_CODE)
        self.assertIsNotNone(result)
        resource = result["resources"][0]
        self.assertEqual(resource["title"], "Заказы")
        self.assertEqual(resource["titleField"], "customer_name")
        self.assertEqual(resource["fields"][0]["label"], "Клиент")
        self.assertEqual(resource["fields"][0]["kind"], "text")
        self.assertTrue(resource["fields"][1]["list"])

    def test_missing_display_metadata_still_parses(self):
        # Older-shape configs (no title/label/kind/list/detail/create) must
        # keep working — display metadata is optional, not required.
        result = claude_service._parse_miniapp_config(VALID_CONFIG_JSON, SAMPLE_BOT_CODE)
        self.assertIsNotNone(result)
        self.assertNotIn("title", result["resources"][0])

    def test_title_field_referencing_unknown_field_returns_none(self):
        bad = """{
            "resources": [
                {
                    "name": "orders", "table": "orders",
                    "titleField": "nonexistent_field",
                    "fields": [{"name": "customer_name"}]
                }
            ]
        }"""
        self.assertIsNone(claude_service._parse_miniapp_config(bad, SAMPLE_BOT_CODE))

    def test_title_field_referencing_real_field_passes(self):
        ok = """{
            "resources": [
                {
                    "name": "orders", "table": "orders",
                    "titleField": "customer_name",
                    "fields": [{"name": "customer_name"}]
                }
            ]
        }"""
        result = claude_service._parse_miniapp_config(ok, SAMPLE_BOT_CODE)
        self.assertIsNotNone(result)
        self.assertEqual(result["resources"][0]["titleField"], "customer_name")


class GenerateMiniappConfig(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._patcher = patch.object(claude_service, "client")
        self.mock_client = self._patcher.start()

    async def asyncTearDown(self):
        self._patcher.stop()

    async def test_uses_haiku_and_the_miniapp_prompt(self):
        create = AsyncMock(return_value=_fake_response(VALID_CONFIG_JSON))
        self.mock_client.messages.create = create
        result = await claude_service._generate_miniapp_config(SAMPLE_BOT_CODE, "an order-taking bot")
        _, kwargs = create.call_args
        self.assertEqual(kwargs["model"], "claude-haiku-4-5-20251001")
        self.assertEqual(kwargs["system"], claude_service.MINIAPP_CONFIG_SYSTEM_PROMPT)
        self.assertIsNotNone(result)
        self.assertEqual(result["resources"][0]["table"], "orders")

    async def test_few_shot_example_is_the_real_tour_operator_config(self):
        # The prompt must actually embed the tour_operator example, not just
        # describe it — this is what makes it "few-shot".
        self.assertIn('"tours"', claude_service.MINIAPP_CONFIG_SYSTEM_PROMPT)
        self.assertIn('"guests"', claude_service.MINIAPP_CONFIG_SYSTEM_PROMPT)

    async def test_prompt_requests_display_metadata(self):
        # Phase 2: the generator must ask for label/kind/list/detail/create
        # display metadata, not just the bare resource/field data contract.
        prompt = claude_service.MINIAPP_CONFIG_SYSTEM_PROMPT
        for keyword in ("titleField", "\"label\"", "\"kind\"", "\"list\"", "\"detail\"", "\"create\""):
            self.assertIn(keyword, prompt)
        self.assertIn('"titleField": "name"', claude_service._TOUR_OPERATOR_MINIAPP_CONFIG_EXAMPLE)

    async def test_api_exception_never_raises_returns_none(self):
        self.mock_client.messages.create = AsyncMock(side_effect=RuntimeError("network broke"))
        result = await claude_service._generate_miniapp_config(SAMPLE_BOT_CODE, "whatever")
        self.assertIsNone(result)

    async def test_malformed_response_never_raises_returns_none(self):
        self.mock_client.messages.create = AsyncMock(return_value=_fake_response("garbage{{{"))
        result = await claude_service._generate_miniapp_config(SAMPLE_BOT_CODE, "whatever")
        self.assertIsNone(result)

    async def test_max_tokens_is_small_not_full_generation_size(self):
        create = AsyncMock(return_value=_fake_response(VALID_CONFIG_JSON))
        self.mock_client.messages.create = create
        await claude_service._generate_miniapp_config(SAMPLE_BOT_CODE, "whatever")
        _, kwargs = create.call_args
        self.assertLessEqual(kwargs["max_tokens"], 2000)


class GenerateBotCodeReturnsCodeAndMiniappConfig(unittest.IsolatedAsyncioTestCase):
    """generate_bot_code's public contract: always a (code, miniapp_config,
    office_hook_config, voice_cashflow_config) tuple, and any one config
    generator's failure never prevents code (or the other configs) from
    coming back."""

    async def test_wraps_inner_generation_and_appends_miniapp_config(self):
        with patch.object(
            claude_service, "_generate_bot_code_inner", AsyncMock(return_value=SAMPLE_BOT_CODE)
        ), patch.object(
            claude_service, "_generate_miniapp_config", AsyncMock(return_value={"resources": [{"name": "orders"}]})
        ), patch.object(
            claude_service, "_generate_office_hook_config", AsyncMock(return_value=None)
        ), patch.object(
            claude_service, "_generate_voice_cashflow_config", AsyncMock(return_value=None)
        ):
            code, miniapp_config, office_hook_config, voice_cashflow_config = await claude_service.generate_bot_code("an order bot")
        self.assertEqual(code, SAMPLE_BOT_CODE)
        self.assertEqual(miniapp_config, {"resources": [{"name": "orders"}]})
        self.assertIsNone(office_hook_config)
        self.assertIsNone(voice_cashflow_config)

    async def test_miniapp_config_none_still_returns_the_code(self):
        with patch.object(
            claude_service, "_generate_bot_code_inner", AsyncMock(return_value=SAMPLE_BOT_CODE)
        ), patch.object(
            claude_service, "_generate_miniapp_config", AsyncMock(return_value=None)
        ), patch.object(
            claude_service, "_generate_office_hook_config", AsyncMock(return_value=None)
        ), patch.object(
            claude_service, "_generate_voice_cashflow_config", AsyncMock(return_value=None)
        ):
            code, miniapp_config, office_hook_config, voice_cashflow_config = await claude_service.generate_bot_code("an order bot")
        self.assertEqual(code, SAMPLE_BOT_CODE)
        self.assertIsNone(miniapp_config)
        self.assertIsNone(office_hook_config)
        self.assertIsNone(voice_cashflow_config)

    async def test_office_hook_config_is_appended_when_generated(self):
        with patch.object(
            claude_service, "_generate_bot_code_inner", AsyncMock(return_value=SAMPLE_BOT_CODE)
        ), patch.object(
            claude_service, "_generate_miniapp_config", AsyncMock(return_value=None)
        ), patch.object(
            claude_service, "_generate_office_hook_config",
            AsyncMock(return_value={"table": "orders", "match_field": "user_id"}),
        ), patch.object(
            claude_service, "_generate_voice_cashflow_config", AsyncMock(return_value=None)
        ):
            code, miniapp_config, office_hook_config, voice_cashflow_config = await claude_service.generate_bot_code("an order bot")
        self.assertEqual(office_hook_config, {"table": "orders", "match_field": "user_id"})

    async def test_voice_cashflow_config_is_appended_when_generated(self):
        with patch.object(
            claude_service, "_generate_bot_code_inner", AsyncMock(return_value=SAMPLE_BOT_CODE)
        ), patch.object(
            claude_service, "_generate_miniapp_config", AsyncMock(return_value=None)
        ), patch.object(
            claude_service, "_generate_office_hook_config", AsyncMock(return_value=None)
        ), patch.object(
            claude_service, "_generate_voice_cashflow_config",
            AsyncMock(return_value={"voice_intake": None, "cashflow_ledger": True}),
        ):
            code, miniapp_config, office_hook_config, voice_cashflow_config = await claude_service.generate_bot_code("an order bot")
        self.assertEqual(voice_cashflow_config, {"voice_intake": None, "cashflow_ledger": True})


if __name__ == "__main__":
    unittest.main()
