"""services/claude_service.py's mini-app config generator (see
docs/MINIAPP_DESIGN.md §6) — _generate_miniapp_config/_parse_miniapp_config/
_validate_miniapp_config_against_code/_extract_create_table_names. Same
mocking pattern as tests/test_narrow_risk_review.py: patch claude_service's
module-level `client`, never hit the real Anthropic API.

Central contract under test: this step must NEVER raise and NEVER block bot
creation — any malformed or hallucinated output degrades to None.
"""
from __future__ import annotations

import asyncio
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

    def test_finds_all_tables_in_a_single_executescript_block(self):
        """Regression test: templates/tour_operator.py's init_db() batches
        multiple CREATE TABLEs into ONE db.executescript(\"\"\"...\"\"\")
        string, each statement terminated by ';' rather than by the string's
        own closing quotes. A prior version of this regex only matched a
        ')' immediately followed by '\"\"\"'/"'''" — true for the LAST table
        in such a block but not the earlier ones — so it silently found
        ZERO tables here, which made every miniapp_config resource for this
        template fail validation as "hallucinated" even when correct."""
        code = '''
import aiosqlite

async def init_db(db_path):
    async with aiosqlite.connect(db_path) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS tours (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS guests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tour_id INTEGER,
                name TEXT NOT NULL
            );
        """)
'''
        tables = claude_service._extract_create_table_names(code)
        self.assertEqual(tables.keys(), {"tours", "guests"})
        self.assertEqual(tables["tours"], {"id", "name"})
        self.assertEqual(tables["guests"], {"id", "tour_id", "name"})


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
    """_parse_miniapp_config now returns (config, failure_reason) — see its
    docstring. failure_reason is None on success, INCLUDING the valid
    "no mini-app needed" ({"resources": []}) answer, and otherwise
    "parse_error" (malformed shape) or "validation_failed" (well-formed JSON
    that failed table/column validation)."""

    def test_valid_json_matching_code_parses(self):
        result, reason = claude_service._parse_miniapp_config(VALID_CONFIG_JSON, SAMPLE_BOT_CODE)
        self.assertIsNotNone(result)
        self.assertIsNone(reason)
        self.assertEqual(result["resources"][0]["table"], "orders")

    def test_strips_markdown_fences(self):
        fenced = f"```json\n{VALID_CONFIG_JSON}\n```"
        result, reason = claude_service._parse_miniapp_config(fenced, SAMPLE_BOT_CODE)
        self.assertIsNotNone(result)
        self.assertIsNone(reason)

    def test_malformed_json_returns_none_with_parse_error(self):
        result, reason = claude_service._parse_miniapp_config("not json at all {{{", SAMPLE_BOT_CODE)
        self.assertIsNone(result)
        self.assertEqual(reason, "parse_error")

    def test_empty_resources_returns_none_config_and_none_reason(self):
        # {"resources": []} is a valid "nothing to show" answer from the
        # model, but there's nothing to store — treated as no config AND
        # not a failure (reason stays None, distinguishing it from a real
        # parse/validation failure).
        result, reason = claude_service._parse_miniapp_config('{"resources": []}', SAMPLE_BOT_CODE)
        self.assertIsNone(result)
        self.assertIsNone(reason)

    def test_missing_resources_key_returns_parse_error(self):
        result, reason = claude_service._parse_miniapp_config('{"foo": "bar"}', SAMPLE_BOT_CODE)
        self.assertIsNone(result)
        self.assertEqual(reason, "parse_error")

    def test_hallucinated_table_returns_validation_failed(self):
        bad = '{"resources": [{"name": "x", "table": "nonexistent", "fields": [{"name": "y"}]}]}'
        result, reason = claude_service._parse_miniapp_config(bad, SAMPLE_BOT_CODE)
        self.assertIsNone(result)
        self.assertEqual(reason, "validation_failed")

    def test_resource_missing_required_keys_returns_parse_error(self):
        bad = '{"resources": [{"name": "orders"}]}'  # no "table", no "fields"
        result, reason = claude_service._parse_miniapp_config(bad, SAMPLE_BOT_CODE)
        self.assertIsNone(result)
        self.assertEqual(reason, "parse_error")

    def test_not_a_dict_returns_parse_error(self):
        result, reason = claude_service._parse_miniapp_config("[1, 2, 3]", SAMPLE_BOT_CODE)
        self.assertIsNone(result)
        self.assertEqual(reason, "parse_error")

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
        result, reason = claude_service._parse_miniapp_config(with_display, SAMPLE_BOT_CODE)
        self.assertIsNotNone(result)
        self.assertIsNone(reason)
        resource = result["resources"][0]
        self.assertEqual(resource["title"], "Заказы")
        self.assertEqual(resource["titleField"], "customer_name")
        self.assertEqual(resource["fields"][0]["label"], "Клиент")
        self.assertEqual(resource["fields"][0]["kind"], "text")
        self.assertTrue(resource["fields"][1]["list"])

    def test_missing_display_metadata_still_parses(self):
        # Older-shape configs (no title/label/kind/list/detail/create) must
        # keep working — display metadata is optional, not required.
        result, reason = claude_service._parse_miniapp_config(VALID_CONFIG_JSON, SAMPLE_BOT_CODE)
        self.assertIsNotNone(result)
        self.assertIsNone(reason)
        self.assertNotIn("title", result["resources"][0])

    def test_title_field_referencing_unknown_field_returns_parse_error(self):
        bad = """{
            "resources": [
                {
                    "name": "orders", "table": "orders",
                    "titleField": "nonexistent_field",
                    "fields": [{"name": "customer_name"}]
                }
            ]
        }"""
        result, reason = claude_service._parse_miniapp_config(bad, SAMPLE_BOT_CODE)
        self.assertIsNone(result)
        self.assertEqual(reason, "parse_error")

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
        result, reason = claude_service._parse_miniapp_config(ok, SAMPLE_BOT_CODE)
        self.assertIsNotNone(result)
        self.assertIsNone(reason)
        self.assertEqual(result["resources"][0]["titleField"], "customer_name")


class GenerateMiniappConfig(unittest.IsolatedAsyncioTestCase):
    """_generate_miniapp_config now returns (config, failure_info) with ONE
    retry on failure before giving up — see its docstring. failure_info is
    None on success (including the valid empty-resources answer), otherwise
    {"reason": ...} once the retry has ALSO failed."""

    async def asyncSetUp(self):
        self._patcher = patch.object(claude_service, "client")
        self.mock_client = self._patcher.start()

    async def asyncTearDown(self):
        self._patcher.stop()

    async def test_uses_haiku_and_the_miniapp_prompt(self):
        create = AsyncMock(return_value=_fake_response(VALID_CONFIG_JSON))
        self.mock_client.messages.create = create
        result, failure_info = await claude_service._generate_miniapp_config(SAMPLE_BOT_CODE, "an order-taking bot")
        _, kwargs = create.call_args
        self.assertEqual(kwargs["model"], "claude-haiku-4-5-20251001")
        self.assertEqual(kwargs["system"], claude_service.MINIAPP_CONFIG_SYSTEM_PROMPT)
        self.assertIsNotNone(result)
        self.assertIsNone(failure_info)
        self.assertEqual(result["resources"][0]["table"], "orders")
        create.assert_awaited_once()  # success on the first try, no retry needed

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

    async def test_empty_resources_is_success_not_a_retriggered_retry(self):
        # The valid "no mini-app needed" answer must not be treated as a
        # failure — no retry, failure_info stays None.
        create = AsyncMock(return_value=_fake_response('{"resources": []}'))
        self.mock_client.messages.create = create
        result, failure_info = await claude_service._generate_miniapp_config(SAMPLE_BOT_CODE, "a purely conversational bot")
        self.assertIsNone(result)
        self.assertIsNone(failure_info)
        create.assert_awaited_once()

    async def test_api_exception_never_raises_retries_once_then_returns_failure_info(self):
        self.mock_client.messages.create = AsyncMock(side_effect=RuntimeError("network broke"))
        result, failure_info = await claude_service._generate_miniapp_config(SAMPLE_BOT_CODE, "whatever")
        self.assertIsNone(result)
        self.assertEqual(failure_info, {"reason": "api_error"})
        self.assertEqual(self.mock_client.messages.create.await_count, 2)  # one retry

    async def test_malformed_response_never_raises_retries_once_then_returns_failure_info(self):
        self.mock_client.messages.create = AsyncMock(return_value=_fake_response("garbage{{{"))
        result, failure_info = await claude_service._generate_miniapp_config(SAMPLE_BOT_CODE, "whatever")
        self.assertIsNone(result)
        self.assertEqual(failure_info, {"reason": "parse_error"})
        self.assertEqual(self.mock_client.messages.create.await_count, 2)

    async def test_hallucinated_response_retries_once_then_returns_validation_failed(self):
        bad = '{"resources": [{"name": "x", "table": "nonexistent", "fields": [{"name": "y"}]}]}'
        self.mock_client.messages.create = AsyncMock(return_value=_fake_response(bad))
        result, failure_info = await claude_service._generate_miniapp_config(SAMPLE_BOT_CODE, "whatever")
        self.assertIsNone(result)
        self.assertEqual(failure_info, {"reason": "validation_failed"})
        self.assertEqual(self.mock_client.messages.create.await_count, 2)

    async def test_success_on_retry_after_one_failed_attempt(self):
        # First call fails, second (retry) succeeds — the whole point of the
        # retry is to recover from exactly this.
        create = AsyncMock(side_effect=[RuntimeError("transient"), _fake_response(VALID_CONFIG_JSON)])
        self.mock_client.messages.create = create
        result, failure_info = await claude_service._generate_miniapp_config(SAMPLE_BOT_CODE, "whatever")
        self.assertIsNotNone(result)
        self.assertIsNone(failure_info)
        self.assertEqual(create.await_count, 2)

    async def test_timeout_returns_timeout_reason(self):
        self.mock_client.messages.create = AsyncMock(return_value=_fake_response(VALID_CONFIG_JSON))
        with patch.object(claude_service.asyncio, "wait_for", AsyncMock(side_effect=asyncio.TimeoutError)):
            result, failure_info = await claude_service._generate_miniapp_config(SAMPLE_BOT_CODE, "whatever")
        self.assertIsNone(result)
        self.assertEqual(failure_info, {"reason": "timeout"})

    async def test_max_tokens_is_small_not_full_generation_size(self):
        create = AsyncMock(return_value=_fake_response(VALID_CONFIG_JSON))
        self.mock_client.messages.create = create
        await claude_service._generate_miniapp_config(SAMPLE_BOT_CODE, "whatever")
        _, kwargs = create.call_args
        # Sized for a multi-domain template (tour_operator's 5 resources /
        # ~40 fields) without truncation — see the call site's own comment —
        # but still far below a full code-generation pass's budget.
        self.assertLessEqual(kwargs["max_tokens"], 8000)


class GenerateBotCodeReturnsCodeAndMiniappConfig(unittest.IsolatedAsyncioTestCase):
    """generate_bot_code's public contract: always a (code, miniapp_config,
    office_hook_config, voice_cashflow_config, fallback_info,
    miniapp_failure_info) tuple, and any one config generator's failure
    never prevents code (or the other configs) from coming back."""

    async def test_wraps_inner_generation_and_appends_miniapp_config(self):
        with patch.object(
            claude_service, "_generate_bot_code_inner", AsyncMock(return_value=(SAMPLE_BOT_CODE, None))
        ), patch.object(
            claude_service, "_generate_miniapp_config",
            AsyncMock(return_value=({"resources": [{"name": "orders"}]}, None)),
        ), patch.object(
            claude_service, "_generate_office_hook_config", AsyncMock(return_value=None)
        ), patch.object(
            claude_service, "_generate_voice_cashflow_config", AsyncMock(return_value=None)
        ):
            code, miniapp_config, office_hook_config, voice_cashflow_config, _fallback_info, miniapp_failure_info = await claude_service.generate_bot_code("an order bot")
        self.assertEqual(code, SAMPLE_BOT_CODE)
        self.assertEqual(miniapp_config, {"resources": [{"name": "orders"}]})
        self.assertIsNone(office_hook_config)
        self.assertIsNone(voice_cashflow_config)
        self.assertIsNone(miniapp_failure_info)

    async def test_miniapp_config_none_still_returns_the_code(self):
        with patch.object(
            claude_service, "_generate_bot_code_inner", AsyncMock(return_value=(SAMPLE_BOT_CODE, None))
        ), patch.object(
            claude_service, "_generate_miniapp_config", AsyncMock(return_value=(None, None))
        ), patch.object(
            claude_service, "_generate_office_hook_config", AsyncMock(return_value=None)
        ), patch.object(
            claude_service, "_generate_voice_cashflow_config", AsyncMock(return_value=None)
        ):
            code, miniapp_config, office_hook_config, voice_cashflow_config, _fallback_info, miniapp_failure_info = await claude_service.generate_bot_code("an order bot")
        self.assertEqual(code, SAMPLE_BOT_CODE)
        self.assertIsNone(miniapp_config)
        self.assertIsNone(office_hook_config)
        self.assertIsNone(voice_cashflow_config)
        self.assertIsNone(miniapp_failure_info)

    async def test_miniapp_failure_info_is_threaded_through_as_sixth_element(self):
        with patch.object(
            claude_service, "_generate_bot_code_inner", AsyncMock(return_value=(SAMPLE_BOT_CODE, None))
        ), patch.object(
            claude_service, "_generate_miniapp_config",
            AsyncMock(return_value=(None, {"reason": "validation_failed"})),
        ), patch.object(
            claude_service, "_generate_office_hook_config", AsyncMock(return_value=None)
        ), patch.object(
            claude_service, "_generate_voice_cashflow_config", AsyncMock(return_value=None)
        ):
            code, miniapp_config, _office_hook_config, _voice_cashflow_config, _fallback_info, miniapp_failure_info = await claude_service.generate_bot_code("an order bot")
        self.assertEqual(code, SAMPLE_BOT_CODE)
        self.assertIsNone(miniapp_config)
        self.assertEqual(miniapp_failure_info, {"reason": "validation_failed"})

    async def test_office_hook_config_is_appended_when_generated(self):
        with patch.object(
            claude_service, "_generate_bot_code_inner", AsyncMock(return_value=(SAMPLE_BOT_CODE, None))
        ), patch.object(
            claude_service, "_generate_miniapp_config", AsyncMock(return_value=(None, None))
        ), patch.object(
            claude_service, "_generate_office_hook_config",
            AsyncMock(return_value={"table": "orders", "match_field": "user_id"}),
        ), patch.object(
            claude_service, "_generate_voice_cashflow_config", AsyncMock(return_value=None)
        ):
            code, miniapp_config, office_hook_config, voice_cashflow_config, _fallback_info, _miniapp_failure_info = await claude_service.generate_bot_code("an order bot")
        self.assertEqual(office_hook_config, {"table": "orders", "match_field": "user_id"})

    async def test_voice_cashflow_config_is_appended_when_generated(self):
        with patch.object(
            claude_service, "_generate_bot_code_inner", AsyncMock(return_value=(SAMPLE_BOT_CODE, None))
        ), patch.object(
            claude_service, "_generate_miniapp_config", AsyncMock(return_value=(None, None))
        ), patch.object(
            claude_service, "_generate_office_hook_config", AsyncMock(return_value=None)
        ), patch.object(
            claude_service, "_generate_voice_cashflow_config",
            AsyncMock(return_value={"voice_intake": None, "cashflow_ledger": True}),
        ):
            code, miniapp_config, office_hook_config, voice_cashflow_config, _fallback_info, _miniapp_failure_info = await claude_service.generate_bot_code("an order bot")
        self.assertEqual(voice_cashflow_config, {"voice_intake": None, "cashflow_ledger": True})


if __name__ == "__main__":
    unittest.main()
