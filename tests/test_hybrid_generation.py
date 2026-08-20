"""Hybrid generation mode (services/claude_service.py): _select_freedom_tier,
_hybrid_customize_from_template, _review_hybrid_bot_code, and their wiring
into generate_bot_code's single-template branch.

Design: docs/HYBRID_GENERATION_MODE_DESIGN.md. Covers the specific
decisions made during design review, not a generic smoke test:
- _select_freedom_tier fails closed to "customize" (the narrower freedom
  level) on any response that isn't exactly "hybrid"
- HYBRID_CUSTOMIZE_EXTRA actually carries the moderate-boundary instructions
  the owner approved: preserve conventions, full structural freedom when
  needed, keep the # TEMPLATE: header and persistence conventions
- HYBRID_REVIEW_SYSTEM_PROMPT checks the template-diff-specific defect
  categories (needless renames, duplicated structure, broken compatibility
  conventions, orphaned original code, incomplete extension)
- generate_bot_code's single-template branch: "customize" tier still skips
  narrow-risk review (unchanged behavior); "hybrid" tier runs the full
  synthesis-equivalent review stack (hybrid-diff review -> narrow-risk ->
  generic), in that order
- _hybrid_customize_from_template falls back to the unmodified template on
  SyntaxError, same fail-safe as _customize_from_template
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import services.claude_service as claude_service


def _fake_response(text: str) -> SimpleNamespace:
    return SimpleNamespace(content=[SimpleNamespace(text=text)])


class HybridCustomizeExtraCoversApprovedBoundaryRules(unittest.TestCase):
    """The owner approved "moderate" boundary strictness: preserve/extend
    conventions, full structural freedom when genuinely needed, keep the
    # TEMPLATE: header and persistence conventions always. Each of these
    must actually be present in the prompt text, not just in the design doc."""

    def test_instructs_preserving_existing_conventions(self):
        self.assertIn(
            "extend them rather than replacing them",
            claude_service.HYBRID_CUSTOMIZE_EXTRA,
        )

    def test_grants_full_structural_freedom(self):
        self.assertIn(
            "You ARE allowed full structural freedom",
            claude_service.HYBRID_CUSTOMIZE_EXTRA,
        )

    def test_does_not_limit_to_customize_blocks(self):
        self.assertIn(
            "do not limit yourself to # CUSTOMIZE blocks",
            claude_service.HYBRID_CUSTOMIZE_EXTRA,
        )

    def test_requires_keeping_template_header_marker(self):
        self.assertIn(
            "Keep the template's `# TEMPLATE: <id>` header line",
            claude_service.HYBRID_CUSTOMIZE_EXTRA,
        )

    def test_requires_keeping_persistence_conventions(self):
        self.assertIn(
            "CREATE TABLE IF NOT EXISTS",
            claude_service.HYBRID_CUSTOMIZE_EXTRA,
        )


class HybridReviewPromptCoversDiffSpecificRiskCategories(unittest.TestCase):
    """The hybrid-diff review pass exists specifically to catch defects a
    template-blind reviewer (generic or narrow-risk) cannot see because it
    never gets the original template to compare against."""

    def test_checklist_mentions_needlessly_renamed_conventions(self):
        self.assertIn(
            "NEEDLESSLY RENAMED CONVENTIONS", claude_service.HYBRID_REVIEW_SYSTEM_PROMPT
        )

    def test_checklist_mentions_duplicated_structure(self):
        self.assertIn("DUPLICATED STRUCTURE", claude_service.HYBRID_REVIEW_SYSTEM_PROMPT)

    def test_checklist_mentions_broken_compatibility_conventions(self):
        self.assertIn(
            "BROKEN COMPATIBILITY CONVENTIONS", claude_service.HYBRID_REVIEW_SYSTEM_PROMPT
        )

    def test_checklist_mentions_orphaned_original_code(self):
        self.assertIn("ORPHANED ORIGINAL CODE", claude_service.HYBRID_REVIEW_SYSTEM_PROMPT)

    def test_checklist_mentions_incomplete_extension(self):
        self.assertIn("INCOMPLETE EXTENSION", claude_service.HYBRID_REVIEW_SYSTEM_PROMPT)


class SelectFreedomTierFailsClosedToCustomize(unittest.IsolatedAsyncioTestCase):
    """_select_freedom_tier must only ever return "hybrid" on an exact,
    unambiguous "hybrid" response — anything else (garbage, blank, a
    differently-worded answer) falls back to "customize", the narrower and
    safer freedom level, never guessed toward more freedom. Takes the
    template's already-read source text (not a name) — the caller reads the
    file once and threads it through, see _select_freedom_tier's docstring."""

    async def asyncSetUp(self):
        self._patcher = patch.object(claude_service, "client")
        self.mock_client = self._patcher.start()
        self.template_code = (claude_service._TEMPLATES_DIR / "shop_catalog.py").read_text(encoding="utf-8")

    async def asyncTearDown(self):
        self._patcher.stop()

    async def test_exact_hybrid_response_returns_hybrid(self):
        self.mock_client.messages.create = AsyncMock(return_value=_fake_response("hybrid"))
        tier = await claude_service._select_freedom_tier(self.template_code, "needs a loyalty table too")
        self.assertEqual(tier, "hybrid")

    async def test_customize_response_returns_customize(self):
        self.mock_client.messages.create = AsyncMock(return_value=_fake_response("customize"))
        tier = await claude_service._select_freedom_tier(self.template_code, "just rename the shop")
        self.assertEqual(tier, "customize")

    async def test_garbage_response_falls_back_to_customize(self):
        self.mock_client.messages.create = AsyncMock(return_value=_fake_response("maybe both idk"))
        tier = await claude_service._select_freedom_tier(self.template_code, "unclear request")
        self.assertEqual(tier, "customize")

    async def test_blank_response_falls_back_to_customize(self):
        self.mock_client.messages.create = AsyncMock(return_value=_fake_response(""))
        tier = await claude_service._select_freedom_tier(self.template_code, "unclear request")
        self.assertEqual(tier, "customize")

    async def test_case_and_whitespace_insensitive_hybrid_match(self):
        self.mock_client.messages.create = AsyncMock(return_value=_fake_response("  Hybrid  \n"))
        tier = await claude_service._select_freedom_tier(self.template_code, "needs a loyalty table too")
        self.assertEqual(tier, "hybrid")


class HybridCustomizeFromTemplateSyntaxFallback(unittest.IsolatedAsyncioTestCase):
    """Same fail-safe contract as _customize_from_template: a SyntaxError in
    the model's output falls back to the UNMODIFIED template, never to a
    half-broken hybrid rewrite. Takes the template's already-read source
    text (not a name), same as _select_freedom_tier."""

    async def asyncSetUp(self):
        self._patcher = patch.object(claude_service, "client")
        self.mock_client = self._patcher.start()
        self.template_code = (claude_service._TEMPLATES_DIR / "shop_catalog.py").read_text(encoding="utf-8")

    async def asyncTearDown(self):
        self._patcher.stop()

    async def test_valid_syntax_returns_model_output(self):
        self.mock_client.messages.create = AsyncMock(
            return_value=_fake_response("import os\nprint('ok')\n")
        )
        code = await claude_service._hybrid_customize_from_template(self.template_code, "add a loyalty table")
        self.assertEqual(code, "import os\nprint('ok')")

    async def test_syntax_error_falls_back_to_unmodified_template(self):
        self.mock_client.messages.create = AsyncMock(
            return_value=_fake_response("def broken(:\n    pass")
        )
        code = await claude_service._hybrid_customize_from_template(self.template_code, "add a loyalty table")
        self.assertEqual(code, self.template_code)


class GenerateBotCodeHybridBranchWiring(unittest.IsolatedAsyncioTestCase):
    """generate_bot_code's single-template dispatch: when _select_freedom_tier
    says "hybrid", the pipeline must run the hybrid-diff review, THEN
    narrow-risk review, THEN the generic review — same review depth as the
    synthesis branch, per the owner's decision (§3: full synthesis-tier
    review, not the light customize-tier review)."""

    async def asyncSetUp(self):
        self._patcher = patch.object(claude_service, "client")
        self.mock_client = self._patcher.start()

    async def asyncTearDown(self):
        self._patcher.stop()

    async def test_hybrid_tier_runs_full_review_stack_in_order(self):
        """freedom_tier and classify run concurrently (asyncio.gather, since
        neither depends on the other or on generated code) so their relative
        order isn't asserted — everything else has a real data dependency
        (each step's output/decision feeds the next) and must stay strictly
        ordered."""
        select_prompt = claude_service._build_template_select_prompt()
        call_order: list[str] = []

        async def fake_create(*, model, max_tokens, system, messages):
            if system == select_prompt:
                return _fake_response("shop_catalog")
            if system == claude_service._FREEDOM_TIER_PROMPT:
                call_order.append("freedom_tier")
                return _fake_response("hybrid")
            if system == claude_service.GENERATE_SYSTEM_PROMPT + claude_service.HYBRID_CUSTOMIZE_EXTRA:
                call_order.append("hybrid_customize")
                return _fake_response("HYBRID_CODE\nasyncio.run(main())")
            if system == claude_service.HYBRID_REVIEW_SYSTEM_PROMPT:
                call_order.append("hybrid_review")
                return _fake_response("HYBRID_REVIEWED\nasyncio.run(main())")
            if system == claude_service.NARROW_RISK_REVIEW_SYSTEM_PROMPT:
                call_order.append("narrow_risk_review")
                return _fake_response("NARROW_REVIEWED\nasyncio.run(main())")
            if system == claude_service.BOT_TYPE_CLASSIFY_PROMPT:
                call_order.append("classify")
                return _fake_response("general")
            if system == claude_service.REVIEW_SYSTEM_PROMPT:
                call_order.append("general_review")
                return _fake_response("FINAL_REVIEWED\nasyncio.run(main())")
            raise AssertionError(f"unexpected system prompt: {system[:120]!r}")

        self.mock_client.messages.create = AsyncMock(side_effect=fake_create)

        with patch.object(
            claude_service, "_generate_miniapp_config", AsyncMock(return_value=(None, None))
        ), patch.object(
            claude_service, "_generate_office_hook_config", AsyncMock(return_value=None)
        ), patch.object(claude_service, "_generate_voice_cashflow_config", AsyncMock(return_value=None)):
            code, _miniapp_config, _office_hook_config, _voice_cashflow_config, _fallback_info, _miniapp_failure_info = await claude_service.generate_bot_code(
                "sells products online, also needs a loyalty points table"
            )

        # freedom_tier and classify both happen before hybrid_customize (the
        # gather must finish before dispatch), and the remaining chain is
        # strictly ordered by data dependency.
        self.assertLess(call_order.index("freedom_tier"), call_order.index("hybrid_customize"))
        self.assertLess(call_order.index("classify"), call_order.index("hybrid_customize"))
        tail = [c for c in call_order if c not in ("freedom_tier", "classify")]
        self.assertEqual(
            tail,
            ["hybrid_customize", "hybrid_review", "narrow_risk_review", "general_review"],
        )
        self.assertEqual(code, "FINAL_REVIEWED\nasyncio.run(main())")

    async def test_customize_tier_never_touches_hybrid_or_narrow_review(self):
        """Regression guard: the "customize" tier (unchanged behavior) must
        not accidentally start running the new hybrid-only review passes."""
        select_prompt = claude_service._build_template_select_prompt()

        async def fake_create(*, model, max_tokens, system, messages):
            if system == select_prompt:
                return _fake_response("shop_catalog")
            if system == claude_service._FREEDOM_TIER_PROMPT:
                return _fake_response("customize")
            if system == claude_service.CUSTOMIZE_TEMPLATE_PROMPT:
                return _fake_response("CUSTOMIZED\nasyncio.run(main())")
            if system == claude_service.BOT_TYPE_CLASSIFY_PROMPT:
                return _fake_response("general")
            if system == claude_service.REVIEW_SYSTEM_PROMPT:
                return _fake_response("REVIEWED\nasyncio.run(main())")
            raise AssertionError(f"unexpected system prompt: {system[:120]!r}")

        self.mock_client.messages.create = AsyncMock(side_effect=fake_create)

        with patch.object(
            claude_service, "_generate_miniapp_config", AsyncMock(return_value=(None, None))
        ), patch.object(
            claude_service, "_generate_office_hook_config", AsyncMock(return_value=None)
        ), patch.object(claude_service, "_generate_voice_cashflow_config", AsyncMock(return_value=None)):
            code, _miniapp_config, _office_hook_config, _voice_cashflow_config, _fallback_info, _miniapp_failure_info = await claude_service.generate_bot_code(
                "sells products online, just rename it"
            )

        self.assertEqual(code, "REVIEWED\nasyncio.run(main())")

    async def test_hybrid_review_receives_original_template_for_diffing(self):
        """The whole point of the hybrid-diff review pass is that it sees
        the ORIGINAL template alongside the hybrid output — verify the
        actual on-disk shop_catalog.py content is what gets passed in."""
        select_prompt = claude_service._build_template_select_prompt()
        original_template = (claude_service._TEMPLATES_DIR / "shop_catalog.py").read_text(encoding="utf-8")
        captured: dict[str, str] = {}

        async def fake_create(*, model, max_tokens, system, messages):
            if system == select_prompt:
                return _fake_response("shop_catalog")
            if system == claude_service._FREEDOM_TIER_PROMPT:
                return _fake_response("hybrid")
            if system == claude_service.GENERATE_SYSTEM_PROMPT + claude_service.HYBRID_CUSTOMIZE_EXTRA:
                return _fake_response("HYBRID_CODE\nasyncio.run(main())")
            if system == claude_service.HYBRID_REVIEW_SYSTEM_PROMPT:
                captured["user_content"] = messages[0]["content"]
                return _fake_response("HYBRID_REVIEWED\nasyncio.run(main())")
            if system == claude_service.NARROW_RISK_REVIEW_SYSTEM_PROMPT:
                return _fake_response("NARROW_REVIEWED\nasyncio.run(main())")
            if system == claude_service.BOT_TYPE_CLASSIFY_PROMPT:
                return _fake_response("general")
            if system == claude_service.REVIEW_SYSTEM_PROMPT:
                return _fake_response("FINAL_REVIEWED\nasyncio.run(main())")
            raise AssertionError(f"unexpected system prompt: {system[:120]!r}")

        self.mock_client.messages.create = AsyncMock(side_effect=fake_create)

        with patch.object(
            claude_service, "_generate_miniapp_config", AsyncMock(return_value=(None, None))
        ), patch.object(
            claude_service, "_generate_office_hook_config", AsyncMock(return_value=None)
        ), patch.object(claude_service, "_generate_voice_cashflow_config", AsyncMock(return_value=None)):
            await claude_service.generate_bot_code("sells products online, also needs a loyalty points table")

        self.assertIn(original_template, captured["user_content"])
        self.assertIn("HYBRID_CODE", captured["user_content"])


class ReviewHybridBotCodeSyntaxFallback(unittest.IsolatedAsyncioTestCase):
    """Same fail-safe contract as _review_merged_bot_code/_review_narrow_risk_code:
    if the reviewed output doesn't parse, keep the pre-review code."""

    async def asyncSetUp(self):
        self._patcher = patch.object(claude_service, "client")
        self.mock_client = self._patcher.start()

    async def asyncTearDown(self):
        self._patcher.stop()

    async def test_syntax_error_keeps_pre_review_code(self):
        self.mock_client.messages.create = AsyncMock(
            return_value=_fake_response("def broken(:\n    pass")
        )
        code = await claude_service._review_hybrid_bot_code(
            "PRE_REVIEW_CODE\nasyncio.run(main())", "requirements", "original template code"
        )
        self.assertEqual(code, "PRE_REVIEW_CODE\nasyncio.run(main())")

    async def test_valid_output_returns_reviewed_code(self):
        self.mock_client.messages.create = AsyncMock(
            return_value=_fake_response("import os\nprint('reviewed')\n")
        )
        code = await claude_service._review_hybrid_bot_code(
            "PRE_REVIEW_CODE", "requirements", "original template code"
        )
        self.assertEqual(code, "import os\nprint('reviewed')")


if __name__ == "__main__":
    unittest.main()
