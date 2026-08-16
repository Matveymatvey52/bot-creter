# Hybrid generation mode — design memo (no code, for owner review)

Worktree: `/Users/matvej/bot-creter-hybrid-mode-design` (branch `design-hybrid-generation-mode`)

## 1. Current state (as implemented in `services/claude_service.py`)

Three generation paths exist today, dispatched from `_generate_bot_code_inner` (~line 1569) based on `_select_template`'s Haiku classification:

| Mode | Function | Freedom | Review depth |
|---|---|---|---|
| Pure template | `_customize_from_template` (1364) | Only `# CUSTOMIZE ... # END CUSTOMIZE` blocks — constants, text, list literals. Handlers/FSM/schema/imports explicitly forbidden by `CUSTOMIZE_TEMPLATE_PROMPT`. | Syntax check + generic `_review_bot_code` only. **No narrow-risk review** — deliberately skipped, on the assumption that untouched code stays trustworthy. |
| Synthesis (2 templates) | `_synthesize_from_templates` (1385) | Free redesign of schema/FSM/handlers; templates are "style/pattern references," not an edit base. Explicit conflict-resolution rules (table/state/callback_data collisions, shared identity, no unrequested features). | `_review_merged_bot_code` → `_review_narrow_risk_code` (+ payment extra) → `_review_bot_code`. 3 extra LLM passes. |
| From-scratch | fallthrough in `_generate_bot_code_inner` (1589+) | Fully free, no reference code at all. | 2 self-repair round-trips + `_review_narrow_risk_code` → `_review_bot_code`. |

The key asymmetry: **freedom and review rigor scale together** in the two modes that already allow structural changes (synthesis, scratch). The one mode that's *cheap on review* (`_customize_from_template`) is cheap specifically because it's mechanically incapable of touching structure — the `# CUSTOMIZE` boundary is the safety mechanism, enforced only by prompt instruction + post-hoc `ast.parse` (no diff-based or structural verification that the boundary was respected).

This means there is currently **no mode that gives Claude structural freedom over a single, known-good template while treating that template as a real foundation** (shared identity, DB, conventions) rather than a loose style reference synthesized from two sources. That's the gap the owner wants filled.

## 2. What changes in "hybrid" mode

**Positioning**: a fourth mode, selected when exactly one template is a strong match (today's `_customize_from_template` trigger condition) *and* the request needs more than `# CUSTOMIZE`-block changes — new tables, new handlers, modified FSM flow, additional integrations beyond what the template's constants can express.

**Freedom granted**: same ceiling as `_synthesize_from_templates` already has today — free to add/modify handlers, add DB tables/columns, extend FSM states, add new callback routes — but with the single template treated as the base file to extend, not as a "pattern reference" among several. Concretely:

- Reuse `GENERATE_SYSTEM_PROMPT` (the from-scratch prompt) as the structural baseline, since that's what already teaches conventions (aiogram 3.x patterns, allowed/forbidden packages, startup/persistence pattern) — the same base synthesis mode already builds on.
- Add a new `HYBRID_CUSTOMIZE_EXTRA` prompt block (naming only — no code being written now) analogous to `MERGE_TEMPLATES_EXTRA`, instructing: "This is your starting point, not a peer reference — preserve its working conventions (table names, callback_data prefixes, existing FSM chains) unless the request requires changing them; extend rather than replace where possible; a full rewrite is allowed only when the requirements genuinely can't be expressed as an extension."
- The single template's full source is passed as-is (as `_customize_from_template` already does), but the system prompt swaps from the restrictive `CUSTOMIZE_TEMPLATE_PROMPT` to this new hybrid prompt.

**What must NOT change** (compatibility surface, per the owner's explicit ask):
- Router/config conventions that offices, miniapp config auto-gen, and feature-modules rely on: `config.db_path` injection pattern, `BotEntry.config` shape, the `# TEMPLATE:` / `# USE FOR:` header markers (needed by `discover_templates()` for future `_select_template` calls — though a hybrid-customized bot is presumably terminal, not re-templatized), the office-event hook conventions, `COMPATIBLE_WITH` markers for feature-modules/sheets/payments compatibility.
- These conventions live in structural code the template *supplies* (main(), startup pattern, DB init) — the hybrid prompt should explicitly preserve them the same way `GENERATE_SYSTEM_PROMPT` already mandates them for from-scratch, so this isn't a new constraint, just carried over from the existing from-scratch prompt's mandatory sections.

## 3. Risk balance

The core tension: wider structural freedom on top of an already-trusted template reintroduces the exact bug classes narrow-risk review exists for (FSM correctness, SQL parameterization/isolation, duplication) — but *without* the safety net synthesis mode gets from two independent template inputs cross-checking each other's conventions, and without from-scratch's "no assumptions to violate" cleanliness.

Proposal: **apply `_review_narrow_risk_code` and `_review_bot_code` to hybrid mode unconditionally** — same tier as synthesis/scratch, not the light tier `_customize_from_template` gets today. The rationale for skipping narrow-risk review in pure-customize mode (unmodified code stays trustworthy) does not hold once structural edits are permitted — hybrid mode should inherit synthesis mode's review cost, not customize mode's.

Additionally worth considering (owner decision, not committing now):
- A **template-diff-aware review pass**, unique to hybrid mode: since the review agent has both the original template and the hybrid output, it can specifically check "did existing table names/FSM states/callback_data get needlessly renamed or duplicated" — cheaper to reason about than synthesis's cross-template collision check, since there's only one prior source of truth to diff against, not two.
- Keep the `ast.parse`-fails-closed contract (revert to pre-review code) identical to all existing modes.
- No new self-repair round-trips are obviously needed (unlike scratch mode, there's always a known-good fallback: the unmodified template, same fallback `_customize_from_template` already uses on `SyntaxError`).

## 4. Mode-selection logic changes

Today `_select_template`'s Haiku prompt only decides *which* template(s) match, not *how much* freedom the request needs — that binary (customize vs. synthesis vs. scratch) is currently derived purely from template count (1 vs. 2 vs. 0), not from request complexity.

To route into hybrid mode, `_select_template` (or a sibling classifier called right after it) needs a second signal: **does this single-template match need structural changes, or only content changes?** Options, for the owner to weigh:

- **(a) Extend `_select_template`'s Haiku call** to also emit a freedom tier alongside the template name(s) — e.g. output format becomes `template_name|tier` where tier ∈ {customize, hybrid}. Cheapest (no extra LLM call), but conflates two decisions in one small-output prompt, and the existing strict output-parsing/fail-closed logic (empty on ambiguity, validate against real files) would need matching tier-fallback rules.
- **(b) Separate classifier call**, only invoked when exactly one template was selected: a small Haiku prompt asking "does satisfying these requirements against this template require new tables/handlers/FSM states, or can it be expressed entirely as constant/text substitution?" This keeps `_select_template`'s existing contract untouched (still just template names) and isolates the new decision, at the cost of one more cheap Haiku call in the single-template path only.
- **(c) Heuristic pre-check** (no LLM): if requirements mention entities/actions not present in the template's `# CUSTOMIZE` block content (e.g. keyword/entity diff between requirements text and the template's existing constants/handler names), default to hybrid; otherwise customize. Cheapest and fastest but least reliable — likely to over-trigger hybrid (and its heavier review cost) on requests that were fine as pure customize.

Recommendation to present to the owner (not deciding unilaterally): **(b)**, since it keeps `_select_template`'s existing fail-closed contract intact (a component already relied on elsewhere) and isolates the new judgment call where it's cheap to get wrong — worst case, a request that could've been pure-customize goes through hybrid's heavier review, which is strictly safer than the reverse mistake.

## 5. Open questions for the owner

1. Should hybrid mode be allowed to rename/restructure things the template's `# CUSTOMIZE` convention currently treats as fixed (table names, callback_data prefixes) when the request genuinely requires it, or should preservation be a hard constraint with from-scratch as the escape hatch instead?
2. Should a hybrid-customized bot keep the `# TEMPLATE:` / `# USE FOR:` header markers afterward (implying it could theoretically be redetected/reused later), or should hybrid output be marked as no longer template-classified, similar to how scratch-generated bots aren't?
3. Full review parity with synthesis mode (3 extra passes) is proposed above — is that acceptable added latency/cost per hybrid-mode generation, or should the new template-diff-aware pass replace one of the existing three rather than stack on top?

Stopping here per the task — no code, no template/prompt changes made. Awaiting decision on §4 routing approach and §5 before implementation.
