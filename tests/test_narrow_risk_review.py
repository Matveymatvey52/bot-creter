"""_review_narrow_risk_code (services/claude_service.py) — the lightweight,
risk-targeted third review pass (FSM state-transition correctness, SQL
parameterization / cross-bot data isolation, duplication/dead code) reserved
for the two riskiest generation paths: template synthesis and
custom_features. Its wiring into generate_bot_code's synthesis branch (and
proof that ordinary single-template customization never touches it) is
covered in tests/test_template_synthesis.py; this file covers the prompt
content, the function's own return-value contract, and its wiring into
generate_custom_feature — previously the only generation path in the whole
pipeline with zero LLM-based code review at all.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import services.claude_service as claude_service


def _fake_response(text: str) -> SimpleNamespace:
    return SimpleNamespace(content=[SimpleNamespace(text=text)])


class NarrowRiskReviewPromptCoversAllThreeCategories(unittest.TestCase):
    """The prompt must actually name the three risk categories from the
    design — otherwise the checklist exists only in comments, not in what
    the model is told to check."""

    def test_covers_fsm_state_transitions(self):
        self.assertIn(
            "FSM STATE-TRANSITION CORRECTNESS", claude_service.NARROW_RISK_REVIEW_SYSTEM_PROMPT
        )

    def test_covers_sql_safety_and_data_isolation(self):
        self.assertIn(
            "DATA ISOLATION / SQL SAFETY", claude_service.NARROW_RISK_REVIEW_SYSTEM_PROMPT
        )
        self.assertIn(
            "aiosqlite parameter placeholders (?)", claude_service.NARROW_RISK_REVIEW_SYSTEM_PROMPT
        )

    def test_covers_duplication_and_dead_code(self):
        self.assertIn(
            "DUPLICATION / DEAD CODE", claude_service.NARROW_RISK_REVIEW_SYSTEM_PROMPT
        )


class ReviewNarrowRiskCode(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._patcher = patch.object(claude_service, "client")
        self.mock_client = self._patcher.start()

    async def asyncTearDown(self):
        self._patcher.stop()

    async def test_uses_narrow_risk_prompt_and_returns_reviewed_code(self):
        create = AsyncMock(return_value=_fake_response("REVIEWED\nasyncio.run(main())"))
        self.mock_client.messages.create = create
        result = await claude_service._review_narrow_risk_code("SOME_CODE", "requirements text")
        _, kwargs = create.call_args
        self.assertEqual(kwargs["system"], claude_service.NARROW_RISK_REVIEW_SYSTEM_PROMPT)
        self.assertEqual(result, "REVIEWED\nasyncio.run(main())")

    async def test_syntax_error_keeps_pre_review_code(self):
        self.mock_client.messages.create = AsyncMock(return_value=_fake_response("def broken(:\n"))
        result = await claude_service._review_narrow_risk_code("ORIGINAL_CODE", "requirements text")
        self.assertEqual(result, "ORIGINAL_CODE")

    async def test_without_reference_code_uses_full_token_budget_and_no_readonly_section(self):
        create = AsyncMock(return_value=_fake_response("REVIEWED\nasyncio.run(main())"))
        self.mock_client.messages.create = create
        await claude_service._review_narrow_risk_code("SOME_CODE", "requirements text")
        _, kwargs = create.call_args
        self.assertEqual(kwargs["max_tokens"], 25000)
        self.assertNotIn("READ-ONLY", kwargs["messages"][0]["content"])

    async def test_with_reference_code_uses_small_token_budget_and_includes_it_readonly(self):
        create = AsyncMock(return_value=_fake_response("REVIEWED"))
        self.mock_client.messages.create = create
        await claude_service._review_narrow_risk_code(
            "PATCH_CODE", "owner's feature request", reference_code="MAIN_BOT_CODE"
        )
        _, kwargs = create.call_args
        self.assertEqual(kwargs["max_tokens"], 8000)
        content = kwargs["messages"][0]["content"]
        self.assertIn("READ-ONLY", content)
        self.assertIn("MAIN_BOT_CODE", content)
        self.assertIn("PATCH_CODE", content)


class GenerateCustomFeatureCallsNarrowRiskReview(unittest.IsolatedAsyncioTestCase):
    """Wiring into custom_features."""

    # _strip_code_fences() strips surrounding whitespace, including a
    # trailing newline — these are the exact strings that survive it, so
    # equality assertions against generate_custom_feature's return value
    # don't trip on a newline the pipeline itself always removes.
    _CLEAN_PATCH = "from aiogram import Router\nrouter = Router()"
    _IMPROVED_PATCH = "from aiogram import Router\nrouter = Router()\n# improved"

    async def asyncSetUp(self):
        self._patcher = patch.object(claude_service, "client")
        self.mock_client = self._patcher.start()

    async def asyncTearDown(self):
        self._patcher.stop()

    async def test_narrow_review_runs_after_clean_generation_and_its_output_wins(self):
        call_order: list[str] = []

        async def fake_create(*, model, max_tokens, system, messages):
            if system == claude_service.CUSTOM_FEATURE_SYSTEM_PROMPT:
                call_order.append("generate")
                return _fake_response(self._CLEAN_PATCH)
            if system == claude_service.NARROW_RISK_REVIEW_SYSTEM_PROMPT:
                call_order.append("narrow_review")
                return _fake_response(self._IMPROVED_PATCH)
            raise AssertionError(f"unexpected system prompt: {system[:120]!r}")

        self.mock_client.messages.create = AsyncMock(side_effect=fake_create)

        result = await claude_service.generate_custom_feature("MAIN_CODE", "add a /stats command")

        self.assertEqual(call_order, ["generate", "narrow_review"])
        self.assertEqual(result, self._IMPROVED_PATCH)

    async def test_narrow_review_receives_main_code_as_readonly_reference(self):
        async def fake_create(*, model, max_tokens, system, messages):
            if system == claude_service.CUSTOM_FEATURE_SYSTEM_PROMPT:
                return _fake_response(self._CLEAN_PATCH)
            if system == claude_service.NARROW_RISK_REVIEW_SYSTEM_PROMPT:
                self.assertIn("MAIN_CODE_MARKER", messages[0]["content"])
                return _fake_response(self._CLEAN_PATCH)
            raise AssertionError(f"unexpected system prompt: {system[:120]!r}")

        self.mock_client.messages.create = AsyncMock(side_effect=fake_create)

        await claude_service.generate_custom_feature("MAIN_CODE_MARKER", "add a /stats command")

    async def test_narrow_review_breaking_syntax_falls_back_to_pre_review_code_no_exception(self):
        async def fake_create(*, model, max_tokens, system, messages):
            if system == claude_service.CUSTOM_FEATURE_SYSTEM_PROMPT:
                return _fake_response(self._CLEAN_PATCH)
            if system == claude_service.NARROW_RISK_REVIEW_SYSTEM_PROMPT:
                return _fake_response("def broken(:\n")
            raise AssertionError(f"unexpected system prompt: {system[:120]!r}")

        self.mock_client.messages.create = AsyncMock(side_effect=fake_create)

        result = await claude_service.generate_custom_feature("MAIN_CODE", "add a /stats command")

        self.assertEqual(result, self._CLEAN_PATCH)

    async def test_narrow_review_reintroducing_forbidden_import_falls_back_to_pre_review_code(self):
        """The narrow review's own model call is itself unvalidated model
        output — if its 'fix' introduces a disallowed import, the pre-review
        code (already known clean) must win, not the tainted rewrite, and no
        exception should propagate (that would need a second retry loop this
        pass deliberately doesn't have)."""
        async def fake_create(*, model, max_tokens, system, messages):
            if system == claude_service.CUSTOM_FEATURE_SYSTEM_PROMPT:
                return _fake_response(self._CLEAN_PATCH)
            if system == claude_service.NARROW_RISK_REVIEW_SYSTEM_PROMPT:
                return _fake_response("import pandas\nfrom aiogram import Router\nrouter = Router()\n")
            raise AssertionError(f"unexpected system prompt: {system[:120]!r}")

        self.mock_client.messages.create = AsyncMock(side_effect=fake_create)

        result = await claude_service.generate_custom_feature("MAIN_CODE", "add a /stats command")

        self.assertEqual(result, self._CLEAN_PATCH)

    async def test_narrow_review_runs_only_after_a_successful_generation_retry(self):
        """generate_custom_feature's existing retry-on-violation loop must
        complete first — narrow review only ever sees code that already
        passed the hard syntax/import gate."""
        call_order: list[str] = []
        first_call = True

        async def fake_create(*, model, max_tokens, system, messages):
            nonlocal first_call
            if system == claude_service.CUSTOM_FEATURE_SYSTEM_PROMPT:
                if first_call:
                    first_call = False
                    call_order.append("generate_forbidden")
                    return _fake_response("import pandas\n" + self._CLEAN_PATCH)
                call_order.append("retry")
                return _fake_response(self._CLEAN_PATCH)
            if system == claude_service.NARROW_RISK_REVIEW_SYSTEM_PROMPT:
                call_order.append("narrow_review")
                return _fake_response(self._CLEAN_PATCH)
            raise AssertionError(f"unexpected system prompt: {system[:120]!r}")

        self.mock_client.messages.create = AsyncMock(side_effect=fake_create)

        result = await claude_service.generate_custom_feature("MAIN_CODE", "add a /stats command")

        self.assertEqual(call_order, ["generate_forbidden", "retry", "narrow_review"])
        self.assertEqual(result, self._CLEAN_PATCH)


if __name__ == "__main__":
    unittest.main()
