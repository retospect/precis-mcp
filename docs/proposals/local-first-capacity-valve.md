---
status: draft
title: Local-first capacity valve — run SMALL local, spill the SAME model to cloud on demand
model: opus
---
<!-- Depends on the saturation-escape mechanism from llm-openrouter-bypass.md
     item 3, which already shipped in code (2026-07-23) — so no `blocked-by`
     branch dependency; this spec only removes the activation blockers. -->


# Local-first capacity valve — run SMALL local, spill the SAME model to cloud on demand

## Motivation / why

Reto wants the classifier/categorizer tier (`SMALL`) to run **local first and
spill to the cloud only when local is saturated** — "if model is local, run
that first, but if too much demand, spin out onto the net" — and, crucially,
the local model and the cloud spill-over to be **the same model**, so an
overflow is invisible to quality: a chunk classified locally and one classified
on the cloud get the identical model, just different hardware.

He also asked the fair question: *this seems trivial — why isn't it already the
default?* The honest answer is the crux of this proposal, so it goes up front:

**A placement chain gives failover-on-ERROR, not overflow-on-SATURATION.**
A static chain `llm.chain.small = [local → cloud]` (which we can and do set in
one SQL upsert) only advances to the cloud rung when the local rung returns an
`error` — i.e. the local transport is *down*. When the local model is merely
**busy** (all slots full), today's `litellm` loopback proxy just queues the
call; it doesn't error, so the chain never advances and the caller waits behind
the 80B. "Spill on demand" is a *different* signal from "spill on failure," and
the router already has the machinery for it — but three things keep it dark:

1. **The local model is the wrong size.** `SMALL` resolves to `summarizer` =
   **Qwen3-Next-80B via the litellm proxy @127.0.0.1:4000, real capacity ~1 per
   host** (`workers/llm_summarize.py`). Even with the valve wired, ~1 local slot
   means a 50-wide burst saturates instantly and *everything* spills — so
   "local-first" buys nothing. A genuinely small, high-parallelism model is the
   precondition for local-first to carry real load before overflowing.
2. **Name-mismatch trap → the saturation detector is dark.** `dispatch` reserves
   a slot under `resolve_model(SMALL)` = `"summarizer"` (`_local.acquire("summarizer")`
   → `resource = "llm:summarizer"`), but `resource_slots` are registered from the
   card's concrete `served_by` model_id (`llm:qwen3-next-80b-…`). `llm:summarizer`
   ∉ served set → `acquire` returns `None` (dark) and logs *"check served_by
   naming."* With no reserved slot there is never a `paused` outcome, so the
   escape below never fires — the tier silently falls through to litellm.
   (`utils/llm/local_serving.py::acquire`.)
3. **A `litellm` rung-0 cannot escape.** The saturation escape
   (`router.py::dispatch`, the `slot.paused` block, ~L1314) retries rung-0
   against `PRECIS_LLM_BASE_URL` — but *only* when rung-0's transport is
   `openai_compat`/`openai_tools` (the two that read the hosted base URL when
   `local_url` is unset). A `litellm` rung-0 has no hosted mode; retrying it just
   re-hits the same saturated loopback, so the code returns `paused` immediately.
   The local rung must be **`openai_compat` pointed at a local llama-swap
   endpoint** (via `served_by.endpoint`), so the *same transport* can be retried
   against the cloud base URL on saturation.

So the valve is not new code — the escape shipped 2026-07-23
(`llm-openrouter-bypass.md` item 3). This proposal **activates** it: pick the
model, fix the naming, seed real capacity, flip the rung transport.

## In scope

1. **Pick one small model `M` served BOTH locally and on the cloud** (the "same
   model" constraint — the headline open question). `M` must: (a) run on
   llama-swap at meaningful parallelism (cap ≫ 1) on the agent hosts; (b) be
   reachable on `PRECIS_LLM_BASE_URL` (OpenRouter) under a resolvable slug. This
   forces `M` to be an **open-weight** model (a hosted-only slug like
   `z-ai/glm-4.7-flash` can't be self-hosted, so it can't be the *same* model
   both sides). Candidate: a small Qwen (`Qwen3-4B`/`Qwen2.5-7B`) or `GLM-4-9B` —
   whichever OpenRouter carries and llama-swap can hold hot.
2. **Fix the name-mismatch (blocker 2).** Make the model `SMALL` resolves to
   equal the `served_by` resource name — either set `llm.model.small = M` so
   `resolve_model(SMALL)` → `M` and register slots under `llm:M`, or register the
   served slot under `llm:summarizer`. Result: `acquire(M)` matches the slot and
   the `paused` signal becomes live.
3. **Seed `served_by` + `resource_slots` at real capacity** on each agent host
   (`workers/llm_reconcile.py` §S seeds from `served_by.max_parallel`;
   `workers/llm_serving.py` heartbeat keeps it fresh). Set `max_parallel` to the
   host's true concurrency for `M`, and declare `served_by.endpoint` = the local
   llama-swap OpenAI URL so a reserved slot routes there directly.
4. **Flip `SMALL`'s local rung to `openai_compat` (blocker 3).**
   `llm.chain.small = [{local M openai_compat}, {cloud M openai_compat}]` — rung-0
   local (served_by endpoint), rung-1 the same `M` on the cloud base URL. Now a
   saturated local slot → escape retries rung-0 on the cloud endpoint = same `M`.
5. **Make the hosted remap same-model-safe.** `_hosted_small_remap` today rewrites
   `summarizer`/`rake-lemma` → `_hosted_small_model()` (glm-4.7-flash). When `M`
   is already a valid hosted slug, the remap must be a **no-op** (spill stays on
   `M`), not a rewrite to a different model — otherwise the "same model" contract
   breaks on the spill path. Either drop `M` from `_LOCAL_ONLY_MODEL_ALIASES`
   (it's a real slug now) or point `llm.model.local-small` at `M`.

## Explicitly NOT in scope

- **MEDIUM (and higher) tiers.** This valve is `SMALL`-only — the high-volume
  classifier tier where local-primary pays off. Whether MEDIUM's judge model
  (`z-ai/glm-4.7`) also gets a local mirror is a separate, harder question (a
  bigger model = less local capacity) — deferred, flagged below.
- **The saturation-escape mechanism itself** — already built
  (`llm-openrouter-bypass.md` item 3). This proposal only removes the three
  activation blockers; it does not touch the escape's control flow.
- **Content-sensitivity placement** (`content-sensitivity-placement.md`) — the
  "keep sensitive content off the cloud entirely" constraint is orthogonal; the
  valve spills freely by design. If both land, the sensitivity flag must veto the
  spill rung, but that composition is out of scope here.
- **Cross-host borrow** (spill to a peer host's spare slots before the cloud) —
  explicitly rejected upstream; OpenRouter is the shared-capacity answer.

## Acceptance criteria

- With `M` served locally at `max_parallel = N`, **N concurrent SMALL dispatches
  run locally** (llama-swap), and the **(N+1)th spills to the cloud running the
  same `M`** — verified by `llm_call_log`: the local N carry the llama-swap
  endpoint, the spill carries the OpenRouter endpoint, **both rows show model
  `M`**.
- The spill is triggered by **saturation, not error**: it fires while the local
  endpoint is healthy (all slots busy), and the log carries the
  `"llm-failover: local slot for M is saturated…"` line — distinct from a
  transport-error failover.
- **Cost is captured** on the spill rows (`openai_compat` → `result_from_openai`
  parses `cost_usd`), so the budget breaker sees OpenRouter spend
  (the gr171782 residual — `openai_tools` cost=null does NOT apply here).
- The **Models tab active-routing header** (shipped `dbf793a4`) shows
  `SMALL → M (local)` rung-0 with the cloud-`M` failover rung beneath, source
  "operator chain".
- Steady state with no burst: **zero cloud spend** on SMALL (everything fits
  local capacity).

## Target + blast radius

- `src/precis/utils/llm/router.py` — `_hosted_small_remap` / `_LOCAL_ONLY_MODEL_ALIASES`
  (same-model no-op), possibly `resolve_model` default for `SMALL`.
- `src/precis/utils/llm/local_serving.py` — no code change expected; the fix is
  making `resolve_model(SMALL)` and the `served_by` resource name agree.
- `src/precis/workers/llm_reconcile.py` §S + `llm_serving.py` — `served_by` /
  `resource_slots` seeding for `M` at real capacity.
- The `llm` catalog card for `M` (`served_by` with `endpoint` + `max_parallel`).
- Prod `app_settings`: `llm.chain.small`, possibly `llm.model.small` /
  `llm.model.local-small`.
- **Cluster/ops:** llama-swap must actually hold `M` hot on the agent hosts —
  the model-download + llama-swap config is an ansible/infra step, not a code
  change, and gates the whole thing.

## Open questions / decisions log

- **[BLOCKER] Which model `M`?** Must be open-weight, on OpenRouter, and holdable
  hot on llama-swap at cap ≫ 1. Candidates: `Qwen3-4B` / `Qwen2.5-7B` / `GLM-4-9B`.
  Needs: confirm the OpenRouter slug + measure llama-swap parallelism on
  melchior/balthazar. Until resolved the valve can't be built.
- **[BLOCKER] Local capacity `N` per host** — what `max_parallel` is real for `M`
  on each agent host (drives whether local-primary meaningfully absorbs a burst
  vs. spills immediately).
- Does "all the same model" (Reto) mean **within SMALL's chain** (local rung ==
  cloud rung == `M`, the transparent-overflow reading — assumed here), or **also
  across tiers** (MEDIUM should mirror the same way)? If the latter, MEDIUM needs
  its own `M_medium` served both places — a follow-on with harder capacity math.
- Keep `summarizer` (the 80B) as a *separate* explicit tier for the callers that
  genuinely want the big summarizer (`llm_summarize.py`), or fold everything onto
  `M`? The classifier burst wants `M`; some summarization may still want the 80B.
- Interaction with the cloud-throttle dial (`llm.cloud_enabled=false`): with the
  valve, a throttle should keep SMALL on local-only and simply *queue* overflow
  rather than spill — confirm `_apply_cloud_throttle` prunes the spill rung as
  intended (it should: rung-1 is `placement:"cloud"`).
