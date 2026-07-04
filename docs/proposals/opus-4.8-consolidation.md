---
status: ready
title: consolidate the cloud reasoning tier on opus-4.8 + finish the ADR-0046 router migration
---

# consolidate the cloud reasoning tier on opus-4.8 + finish the ADR-0046 router migration

## Motivation / why

"opus-4.8 throughout" is not the current state, and model selection is a
patchwork. The ADR-0046 router (`src/precis/utils/llm/router.py`,
`resolve_model(Tier)`) is only half-adopted: three sites route through it
(`plan_tick`, `fix_gripe`, `tex_llm_fix`), the rest hardcode. Today:

| Who | Model | Via |
|---|---|---|
| router `Tier.CLOUD_SUPER` | opus-4-**7** | `PRECIS_MODEL_OPUS` default |
| plan_tick / fix_gripe / tex_llm_fix | opus-4-7 | router |
| the fixer | opus-4-**8** | hardcoded default |
| structural / deep_review reviewers | opus-4-7 | hardcoded on the dataclass |
| dream_agent | **sonnet**-4-6 | hardcoded (`PRECIS_DREAM_AGENT_MODEL`) |
| claude_agent (generic default) | sonnet-4-6 | env default |

4-7 and 4-8 are the same price, so there is no cost reason to stay on 4-7, and
the reasoning/agentic work is exactly where the stronger model earns its keep.
"If it's worth thinking about, think well."

## In scope

1. **Bump the table** — `PRECIS_MODEL_OPUS` (and the `router.py` default) →
   `claude-opus-4-8`. This lifts the three router-adopted callers at once.
2. **Migrate the hardcoded stragglers onto the router** (ADR-0046 unit 4b, for
   the reasoning tier only): the two reviewers (`workers/structural.py`,
   `workers/deep_review.py`), `dream_agent`, and the generic `claude_agent`
   default **all** resolve `resolve_model(Tier.CLOUD_SUPER)` (opus-4.8),
   replacing the hardcoded `claude-opus-4-7` / `claude-sonnet-4-6` constants.
   Keep each caller's existing env override (`PRECIS_CLAUDE_AGENT_MODEL`,
   `PRECIS_DREAM_AGENT_MODEL`, `PRECIS_STRUCTURAL_MODEL`,
   `PRECIS_DEEP_REVIEW_MODEL`) so a per-pass pin still wins.
3. **Dream: model + directive.** With opus-4.8 on the dream pass, add a prompt
   directive to **pursue interesting threads that may be useful later** —
   where it sees an unexplored connection, a latent opportunity, or a question
   worth returning to, capture it as a `kind='memory'` tagged `thread:` (with
   *why it might matter later*). Constraints so it doesn't spin: reuse the
   dream's existing tier-tagged dedup, cap threads-per-pass to a handful, mark
   them explicitly speculative. Threads land as memories (surfaced by search
   when relevant), **not** as active todos — no clogging the doable rotation.

## Explicitly NOT in scope

- **The local, corpus-scale passes** — `summarize` (the `summarizer` local
  model) and `classify` tier-1 stay **local** by design (whole-corpus cost /
  throughput). opus is not an upgrade there; it's a regression. "Throughout"
  means the cloud reasoning callers, not the lexical/volume tier.
- **The cheap one-shot JSON judges** (`claude_p` — chase verifier, boolean
  verdicts, currently haiku): left cheap. opus-4.8 works but is slower/overkill
  for a `{yes/no}`. Flip them only if zero model-sprawl is wanted; default is
  leave-cheap.
- **The router's transport/dispatch half** (`dispatch(LlmRequest)`) — still
  unused; this proposal migrates *model selection*, not transport.

## Acceptance criteria

1. `resolve_model(Tier.CLOUD_SUPER)` returns `claude-opus-4-8`; the import-time
   totality assert still holds.
2. Reviewers, `dream_agent`, and the generic `claude_agent` default resolve
   opus-4.8 via the router (no residual hardcoded `4-7`/`sonnet-4-6`
   reasoning-tier constants); existing env overrides still win.
3. `summarize` / `classify` local models and the haiku JSON judges are
   **unchanged**.
4. The dream prompt carries the `thread:` directive + the anti-noise
   constraints; a test asserts the directive is present and that thread
   capture is dedup/capped.
5. `ruff` + `mypy` + `pytest` green.

## Target + blast radius

- **Edited:** `src/precis/utils/llm/router.py` (default), `workers/structural.py`,
  `workers/deep_review.py`, `workers/dream_agent.py` (model + prompt),
  `utils/claude_agent.py` (default), any prompt fixtures.
- **Runtime effect:** the reasoning/agentic passes run opus-4.8; dream begins
  emitting `thread:` memories. Watch the dream memory volume post-deploy (the
  pass has a spin-loop history — the cap + dedup are load-bearing).

## Open questions / decisions log

- Model = opus-4.8 across the cloud reasoning tier (decided 2026-07-04).
- Local passes + cheap JSON judges excluded (decided).
- Dream "pursue threads" → `thread:` memories, capped + dedup'd, non-blocking
  (decided).
