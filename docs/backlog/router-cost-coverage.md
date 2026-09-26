# Router cost coverage

Grouped 2026-09-26 from 5 items that are sub-parts of one deliverable (each keeps its own section below; the originals are in the history). Split a section back out only when it becomes independently shippable.

## Everything through the router; wean bulk work off opus

_Grouped 2026-09-26; was `router-coverage-and-downshift`._

Reto: all LLM traffic through the precis router, then push "stupid work" down
to local/cheaper models (haiku, deepinfra/OpenRouter/EU; local best), keeping
claude as top-dog reviewer — coding and writing tasks included. Remaining
coverage holes: `claude_docker`/`sandbox_run`'s in-container `claude -p`
never touches dispatch (deferred — dark/unused today); the ADR-0046 "group B"
call-sites aren't backend-aware, so a cloud chain rung on those paths would
mis-route. Related asks: a cheap/local pre-filter tier for research surfaces
(asa, reviewers, `perplexity-research` ~$0.50/call) before paid escalation;
a "corpus before paid web" cost-ordering line in precis-research-help + asa's
SOUL; route mechanical passes (llm_summarize, triage children, CI-fix) to a
4B–14B model.

## Wire choose_model / select_offering into deliberative call-sites

_Grouped 2026-09-26; was `llm-catalog-wire-policy`._

`src/precis/utils/llm/requirement.py::choose_model` and
`src/precis/utils/llm/policy.py::select_offering` are shipped + green, but no
production call-site invokes them — every dispatch still resolves via the
fixed Tier table; `Selection.endpoint` (the variant-precise OpenRouter
booking) is likewise plumbed but unthreaded. Pick the first call-sites and
wire. Sonnet-shaped once the sites are chosen.

## Budget breaker: gate on resolved transport cost, not tier band

_Grouped 2026-09-26; was `breaker-gate-resolved-cost`._

`bands._TIER_BANDS[SMALL]=FREE`, so `breaker.gate_tier` never gates SMALL —
but all-remote SMALL resolves to a paid OpenRouter model at the highest
volume of any tier (~6.7k calls/24 h): a tripped cap pauses
BIG/MEDIUM/FRONTIER while SMALL keeps spending. Symmetric to the shipped
e6e02d7a fix (paid-band tier on a free-local rung exempt). Clean fix: drop
`is_paid(tier)` as the gate determinant; pass
`local=not _rung_is_cloud(rung0)` as the sole signal. Its own cycle — it
removes the is_paid assumption baked into bands.py/breaker.py; spend-check
first whether SMALL's remote $ is actually material. Owner
`src/precis/budget/bands.py`, `breaker.py` + `src/precis/utils/llm/router.py`.

## req.model pins bypass the backend-coherence check

_Grouped 2026-09-26; was `model-pin-coherence-check`._

dream's PRECIS_DREAM_AGENT_MODEL env-pin and asa's hard-pinned
`--model claude-opus-4-8` (`src/asa_bot/claude_invoke.py`) set `req.model`
directly, which dispatch honors over `resolve_model(tier, backend=)` — so the
ADR 0066 Part-3 coherence check (inside resolve_model) never runs for them,
and an OSS slug can still land on a claude transport under a half-applied
flip. Reviewer finding #2 from the Phase-1 flip-safety landing. Owner
`src/precis/utils/llm/router.py`.

## Decide the LLM: planner tag vocab's scope

_Grouped 2026-09-26; was `llm-tag-vocab`._

plan_tick always passes tools_needed=True, so `LLM:small`/`LLM:medium` todo
tags no-op through the local-fallback path instead of routing distinctively
(ADR 0066 §"Still genuinely open"). Decide what the tag vocabulary should
mean and wire it. Needs design.
