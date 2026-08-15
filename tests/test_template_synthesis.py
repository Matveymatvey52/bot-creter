"""Two-template synthesis mode (services/claude_service.py): the tightened
_select_template threshold, _synthesize_from_templates, _review_merged_bot_code,
and their wiring into generate_bot_code.

Covers the specific risks flagged during design, not a generic smoke test:
- the prompt actually carries the naming-conflict-resolution rules (tables,
  FSM state groups, callback_data prefixes) and the shared-customer-identity
  rule, since these are the whole reason synthesis mode is safer than a
  naive file concatenation
- the tightened _select_template threshold: a single dominant template must
  resolve to ONE name, never padded to two, and the prompt text driving that
  decision states the 60%-favors-one rule explicitly
- _select_template falls back to [] (never guesses) on any garbage/invalid/
  blank model response
- generate_bot_code runs the merge-specific review BEFORE the general review
  for the 2-template branch, and an ordinary single-template request never
  touches the synthesis code path at all (the single-template branch also
  now gets the general review pass it used to skip entirely)
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import services.claude_service as claude_service


def _fake_response(text: str) -> SimpleNamespace:
    return SimpleNamespace(content=[SimpleNamespace(text=text)])


class MergeTemplatesExtraCoversConflictResolutionRules(unittest.TestCase):
    """MERGE_TEMPLATES_EXTRA is the only thing standing between the model and
    a naive two-file concatenation — each specific conflict category called
    out during design must actually be present in the prompt text."""

    def test_covers_duplicate_table_name_collisions(self):
        self.assertIn(
            "never let two unrelated tables share a name",
            claude_service.MERGE_TEMPLATES_EXTRA,
        )

    def test_covers_duplicate_state_group_merging(self):
        self.assertIn("merge into ONE state chain", claude_service.MERGE_TEMPLATES_EXTRA)

    def test_covers_callback_data_prefix_collisions(self):
        self.assertIn("prefix them by domain", claude_service.MERGE_TEMPLATES_EXTRA)

    def test_covers_shared_customer_identity(self):
        self.assertIn(
            "MUST share one identity/record", claude_service.MERGE_TEMPLATES_EXTRA
        )

    def test_forbids_forked_ab_menu(self):
        self.assertIn(
            'never a menu that forks into "template A mode" vs "template B mode"',
            claude_service.MERGE_TEMPLATES_EXTRA,
        )


class MergeReviewPromptCoversSameRiskCategories(unittest.TestCase):
    """The merge-specific review pass must check for the exact defect
    categories the synthesis prompt is trying to prevent — otherwise a model
    slip-up in the first pass has nothing catching it in the second."""

    def test_checklist_mentions_duplicate_tables(self):
        self.assertIn(
            "DUPLICATE/COLLIDING TABLE NAMES", claude_service.MERGE_REVIEW_SYSTEM_PROMPT
        )

    def test_checklist_mentions_duplicate_state_groups(self):
        self.assertIn("DUPLICATE STATE GROUPS", claude_service.MERGE_REVIEW_SYSTEM_PROMPT)

    def test_checklist_mentions_colliding_callback_data(self):
        self.assertIn("COLLIDING CALLBACK_DATA", claude_service.MERGE_REVIEW_SYSTEM_PROMPT)

    def test_checklist_mentions_forked_menu(self):
        self.assertIn("FORKED MENU", claude_service.MERGE_REVIEW_SYSTEM_PROMPT)


class SelectTemplatePromptStatesTightenedThreshold(unittest.TestCase):
    """The prompt text driving the one-vs-two decision must explicitly favor
    one template when it alone clears 60% — this is the guard against the
    false-positive risk identified during design (a dominant template plus a
    minor bolt-on feature must not get padded into a two-template merge)."""

    def test_prompt_states_one_template_wins_at_60_percent(self):
        prompt = claude_service._build_template_select_prompt()
        self.assertIn(
            "ONE template name if it alone covers 60%+ of the request's core functionality",
            prompt,
        )

    def test_prompt_prefers_one_when_in_doubt(self):
        prompt = claude_service._build_template_select_prompt()
        self.assertIn("When in doubt between one template and two, prefer ONE", prompt)


class SelectTemplateThresholdAndFallback(unittest.IsolatedAsyncioTestCase):
    """_select_template's parsing/validation logic, exercised against mocked
    Haiku responses — the real templates/ directory is used unpatched so the
    validation-against-real-files step is exercised for real."""

    async def asyncSetUp(self):
        self._patcher = patch.object(claude_service, "client")
        self.mock_client = self._patcher.start()

    async def asyncTearDown(self):
        self._patcher.stop()

    async def test_single_dominant_template_resolves_to_one_not_two(self):
        """The '60%+ one template' case from the design: even though the
        request below could plausibly brush against a second template
        (referral_program), a model response of just one name must be
        respected as ONE template, not silently expanded."""
        self.mock_client.messages.create = AsyncMock(return_value=_fake_response("shop_catalog"))
        result = await claude_service._select_template("bot that sells products online")
        self.assertEqual(result, ["shop_catalog"])

    async def test_two_genuinely_distinct_domains_returns_both(self):
        self.mock_client.messages.create = AsyncMock(
            return_value=_fake_response("shop_catalog,referral_program")
        )
        result = await claude_service._select_template("sells products and rewards referrals")
        self.assertEqual(result, ["shop_catalog", "referral_program"])

    async def test_none_response_returns_empty_list(self):
        self.mock_client.messages.create = AsyncMock(return_value=_fake_response("none"))
        self.assertEqual(await claude_service._select_template("a spaceship simulator"), [])

    async def test_blank_response_falls_back_to_empty_list_not_crash(self):
        """Pure whitespace used to hit .split()[0] on an empty list and raise
        IndexError — a garbage/empty model response must degrade to the same
        'no template' outcome as an explicit 'none', never crash."""
        self.mock_client.messages.create = AsyncMock(return_value=_fake_response("   "))
        self.assertEqual(await claude_service._select_template("anything"), [])

    async def test_unknown_single_name_falls_back_to_empty_list(self):
        self.mock_client.messages.create = AsyncMock(return_value=_fake_response("banana"))
        self.assertEqual(await claude_service._select_template("anything"), [])

    async def test_three_names_exceeds_cap_falls_back_to_empty_list(self):
        self.mock_client.messages.create = AsyncMock(
            return_value=_fake_response("shop_catalog,referral_program,accountant")
        )
        self.assertEqual(await claude_service._select_template("anything"), [])

    async def test_one_valid_one_invalid_name_falls_back_to_empty_list(self):
        """Partial garbage (one real template, one hallucinated name) must not
        be salvaged into a single-template result — it signals the model's
        response as a whole isn't trustworthy, so fall through cleanly."""
        self.mock_client.messages.create = AsyncMock(
            return_value=_fake_response("shop_catalog,not_a_real_template")
        )
        self.assertEqual(await claude_service._select_template("anything"), [])


class SynthesizeFromTemplates(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._patcher = patch.object(claude_service, "client")
        self.mock_client = self._patcher.start()

    async def asyncTearDown(self):
        self._patcher.stop()

    async def test_sends_generate_prompt_plus_merge_extra_with_both_full_template_bodies(self):
        create = AsyncMock(return_value=_fake_response("CODE\nasyncio.run(main())"))
        self.mock_client.messages.create = create

        await claude_service._synthesize_from_templates(
            ["shop_catalog", "referral_program"], "sells products and rewards points"
        )

        _, kwargs = create.call_args
        self.assertEqual(
            kwargs["system"], claude_service.GENERATE_SYSTEM_PROMPT + claude_service.MERGE_TEMPLATES_EXTRA
        )
        content = kwargs["messages"][0]["content"]
        self.assertIn("Reference template A (shop_catalog):", content)
        self.assertIn("Reference template B (referral_program):", content)
        real_shop_catalog = (claude_service._TEMPLATES_DIR / "shop_catalog.py").read_text(encoding="utf-8")
        self.assertIn(real_shop_catalog, content)

    async def test_syntax_error_falls_back_to_empty_string(self):
        """generate_bot_code checks 'asyncio.run(main())' in code to decide
        whether the synthesis path succeeded — an empty string must fail that
        check cleanly and fall through to scratch generation, never raise."""
        self.mock_client.messages.create = AsyncMock(return_value=_fake_response("def broken(:\n"))
        result = await claude_service._synthesize_from_templates(
            ["shop_catalog", "referral_program"], "anything"
        )
        self.assertEqual(result, "")


class ReviewMergedBotCode(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._patcher = patch.object(claude_service, "client")
        self.mock_client = self._patcher.start()

    async def asyncTearDown(self):
        self._patcher.stop()

    async def test_uses_merge_review_prompt(self):
        create = AsyncMock(return_value=_fake_response("REVIEWED\nasyncio.run(main())"))
        self.mock_client.messages.create = create
        result = await claude_service._review_merged_bot_code("SOME_CODE", "requirements text")
        _, kwargs = create.call_args
        self.assertEqual(kwargs["system"], claude_service.MERGE_REVIEW_SYSTEM_PROMPT)
        self.assertEqual(result, "REVIEWED\nasyncio.run(main())")

    async def test_syntax_error_keeps_pre_review_code(self):
        self.mock_client.messages.create = AsyncMock(return_value=_fake_response("def broken(:\n"))
        result = await claude_service._review_merged_bot_code("ORIGINAL_CODE", "requirements text")
        self.assertEqual(result, "ORIGINAL_CODE")


class GenerateBotCodeTwoTemplateBranch(unittest.IsolatedAsyncioTestCase):
    """The core ordering claim from the design: merge-specific review runs
    BEFORE the general review, so the general pass sees the already
    de-duplicated structure, not a pre-fix draft."""

    async def asyncSetUp(self):
        self._patcher = patch.object(claude_service, "client")
        self.mock_client = self._patcher.start()

    async def asyncTearDown(self):
        self._patcher.stop()

    async def test_merge_review_runs_before_narrow_risk_before_general_review(self):
        select_prompt = claude_service._build_template_select_prompt()
        synth_system = claude_service.GENERATE_SYSTEM_PROMPT + claude_service.MERGE_TEMPLATES_EXTRA
        call_order: list[str] = []

        async def fake_create(*, model, max_tokens, system, messages):
            if system == select_prompt:
                call_order.append("select")
                return _fake_response("shop_catalog,referral_program")
            if system == synth_system:
                call_order.append("synthesize")
                return _fake_response("SYNTH_CODE\nasyncio.run(main())")
            if system == claude_service.MERGE_REVIEW_SYSTEM_PROMPT:
                call_order.append("merge_review")
                return _fake_response("MERGE_REVIEWED\nasyncio.run(main())")
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

        with patch.object(claude_service, "_generate_miniapp_config", AsyncMock(return_value=None)):
            code, _miniapp_config = await claude_service.generate_bot_code("sells products and rewards loyalty points")

        self.assertEqual(
            call_order,
            ["select", "synthesize", "merge_review", "narrow_risk_review", "classify", "general_review"],
        )
        self.assertEqual(code, "FINAL_REVIEWED\nasyncio.run(main())")


class SynthesisModeIsolatedFromSingleTemplateRequests(unittest.IsolatedAsyncioTestCase):
    """The other half of the design's safety claim: an ordinary request that
    resolves to exactly one template must never invoke synthesis code at
    all — patch the synthesis functions directly and assert they're
    untouched, rather than trusting the branch to behave from the outside."""

    async def asyncSetUp(self):
        self._client_patcher = patch.object(claude_service, "client")
        self.mock_client = self._client_patcher.start()
        self._synth_patcher = patch.object(
            claude_service, "_synthesize_from_templates", new=AsyncMock()
        )
        self.mock_synth = self._synth_patcher.start()
        self._merge_review_patcher = patch.object(
            claude_service, "_review_merged_bot_code", new=AsyncMock()
        )
        self.mock_merge_review = self._merge_review_patcher.start()
        self._narrow_review_patcher = patch.object(
            claude_service, "_review_narrow_risk_code", new=AsyncMock()
        )
        self.mock_narrow_review = self._narrow_review_patcher.start()

    async def asyncTearDown(self):
        self._client_patcher.stop()
        self._synth_patcher.stop()
        self._merge_review_patcher.stop()
        self._narrow_review_patcher.stop()

    async def test_single_template_never_calls_synthesis_merge_or_narrow_review(self):
        select_prompt = claude_service._build_template_select_prompt()

        async def fake_create(*, model, max_tokens, system, messages):
            if system == select_prompt:
                return _fake_response("shop_catalog")
            if system == claude_service.CUSTOMIZE_TEMPLATE_PROMPT:
                return _fake_response("CUSTOMIZED\nasyncio.run(main())")
            if system == claude_service.BOT_TYPE_CLASSIFY_PROMPT:
                return _fake_response("general")
            if system == claude_service.REVIEW_SYSTEM_PROMPT:
                return _fake_response("REVIEWED\nasyncio.run(main())")
            raise AssertionError(f"unexpected system prompt: {system[:120]!r}")

        self.mock_client.messages.create = AsyncMock(side_effect=fake_create)

        with patch.object(claude_service, "_generate_miniapp_config", AsyncMock(return_value=None)):
            code, _miniapp_config = await claude_service.generate_bot_code("sells products online")

        self.mock_synth.assert_not_called()
        self.mock_merge_review.assert_not_called()
        # The narrow risk/FSM/SQL-safety/dedup review is reserved for
        # synthesis and custom_features — ordinary single-template
        # customization must never touch it.
        self.mock_narrow_review.assert_not_called()
        # Regression check for the gap found during design: the single-template
        # customize path used to return right after customization, with no
        # general review pass at all.
        self.assertEqual(code, "REVIEWED\nasyncio.run(main())")

    async def test_no_template_match_never_calls_synthesis_or_merge_review(self):
        """The from-scratch fallback still never touches synthesis-specific
        code (there's nothing to merge) — this class patches
        _review_narrow_risk_code itself out, so its real wiring/output-flow
        on this path is covered separately by GenerateBotCodeFromScratchBranch
        below, which does NOT patch it."""
        select_prompt = claude_service._build_template_select_prompt()

        async def fake_create(*, model, max_tokens, system, messages):
            if system == select_prompt:
                return _fake_response("none")
            if system == claude_service.BOT_TYPE_CLASSIFY_PROMPT:
                return _fake_response("general")
            if system == claude_service.GENERATE_SYSTEM_PROMPT:
                return _fake_response("SCRATCH_CODE\nasyncio.run(main())")
            if system == claude_service.REVIEW_SYSTEM_PROMPT:
                return _fake_response("SCRATCH_REVIEWED\nasyncio.run(main())")
            raise AssertionError(f"unexpected system prompt: {system[:120]!r}")

        self.mock_client.messages.create = AsyncMock(side_effect=fake_create)

        with patch.object(claude_service, "_generate_miniapp_config", AsyncMock(return_value=None)):
            code, _miniapp_config = await claude_service.generate_bot_code("a completely novel kind of bot")

        self.mock_synth.assert_not_called()
        self.mock_merge_review.assert_not_called()
        self.mock_narrow_review.assert_called_once()
        self.assertEqual(code, "SCRATCH_REVIEWED\nasyncio.run(main())")


class GenerateBotCodeFromScratchBranch(unittest.IsolatedAsyncioTestCase):
    """The from-scratch fallback used to be the only generation path with
    zero narrow-risk review (see NARROW_RISK_REVIEW_SYSTEM_PROMPT's own
    comment) — this covers its new wiring: narrow-risk runs after the
    generic scratch generation/fix-up steps and BEFORE the generic
    _review_bot_code pass, same ordering as the two-template branch."""

    async def asyncSetUp(self):
        self._patcher = patch.object(claude_service, "client")
        self.mock_client = self._patcher.start()

    async def asyncTearDown(self):
        self._patcher.stop()

    async def test_narrow_risk_runs_before_general_review(self):
        select_prompt = claude_service._build_template_select_prompt()
        call_order: list[str] = []

        async def fake_create(*, model, max_tokens, system, messages):
            if system == select_prompt:
                return _fake_response("none")
            if system == claude_service.BOT_TYPE_CLASSIFY_PROMPT:
                call_order.append("classify")
                return _fake_response("general")
            if system == claude_service.GENERATE_SYSTEM_PROMPT:
                call_order.append("generate")
                return _fake_response("SCRATCH_CODE\nasyncio.run(main())")
            if system == claude_service.NARROW_RISK_REVIEW_SYSTEM_PROMPT:
                call_order.append("narrow_risk_review")
                return _fake_response("NARROW_REVIEWED\nasyncio.run(main())")
            if system == claude_service.REVIEW_SYSTEM_PROMPT:
                call_order.append("general_review")
                return _fake_response("FINAL_REVIEWED\nasyncio.run(main())")
            raise AssertionError(f"unexpected system prompt: {system[:120]!r}")

        self.mock_client.messages.create = AsyncMock(side_effect=fake_create)

        with patch.object(claude_service, "_generate_miniapp_config", AsyncMock(return_value=None)):
            code, _miniapp_config = await claude_service.generate_bot_code("a completely novel kind of bot")

        self.assertEqual(
            call_order,
            ["classify", "generate", "narrow_risk_review", "general_review"],
        )
        self.assertEqual(code, "FINAL_REVIEWED\nasyncio.run(main())")


if __name__ == "__main__":
    unittest.main()
