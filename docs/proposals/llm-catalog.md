---
status: draft
title: The llm catalog — model choice as a queryable, learnable resource
---

# The `llm` catalog — model choice as a queryable, learnable resource

> **Status: model closed; not yet sliced into branches.** Captures the design
> conversation of 2026-07-16 (Reto + session). Turns *model selection* from
> a hardcoded constant (`_TIER_MODEL` + `LLM:opus|sonnet|haiku`) into a
> first-class precis kind — a **catalog** of model facts + a **ledger** of
> observations + a **policy** that picks. The planner and the coder agent both
> consult it; it gets better over time from precis's own logs; and it is the
> post-teardown home for the model knowledge that currently lives only inside
> the litellm gateway. **Every layer degrades to today's behavior when the
> catalog is empty** (`Tier` stays the floor), so it ships dark, like quest.
> Related: ADR 0046 (the router seam this plugs into), the deferred
> `turn-routing-and-context-dsl.md` (the planner delegation this makes real),
> and the litellm teardown tracked in `OPEN-ITEMS.md`.

## Motivation / why

Model choice today is a patchwork of constants:

- `utils/llm/router.py::_TIER_MODEL` — a 5-row `Tier → (env, default)` table
  (`CLOUD_SUPER → claude-opus-4-8`, … `LOCAL_SMALL → summarizer`).
- `plan_tick` picks from a closed `LLM:opus|sonnet|haiku` tag and shells
  `claude -p` directly (doesn't even go through the router).
- The cluster keeps a *second*, independent tier vocabulary (`agent_model_*`)
  and a GGUF-level `llamacpp_catalog` that already knows each local model's
  `ctx_size`.

Nothing knows a model's context window, price, or what it's good at. So we
can't:

- **Stop a doomed pairing** — "run tinymodel on this 100k assembled context"
  (2k window) silently truncates or 400s instead of refusing with the numbers.
- **Delegate** — send a cheap classify to `qwen-heavy` and keep opus for the
  hard refactor.
- **Escalate** — "that came back weak → next better model."
- **Learn** — `llm_call_log` (migration 0061) records every call's
  source/model/cost/turns/errors, but nothing rolls it into "which model is
  actually good, and cheap, at *this*."
- **Fix drift** — the router defaults opus to `claude-opus-4-8`, but the
  litellm proxy only exposes `claude-opus`/`-4-7`; any opus call *through the
  proxy* 400s, and nothing notices.

The unifying frame: **make model choice an informed, budget-aware,
window-safe decision — made only where a decision is actually being made — and
let it improve from precis's own telemetry.**

## The core model — three layers, kept separate

The failure mode of "model routers" is fusing three different things. Keep them
apart:

1. **Catalog** — *static facts*: context window, price, transport/endpoint,
   capability envelope, known issues. Authoritative, provenance-tagged,
   reconciled against reality.
2. **Ledger** — *observations*: auto-derived telemetry from `llm_call_log` +
   agent-authored reviews ("opus·medium was excellent at SQL-migration
   reasoning"). Append-only, embedded, searchable.
3. **Policy** — *the selection function*: `select_offering(requirements)` +
   `admit(tokens, offering)`. Pure, testable, cacheable.

`search(kind='llm', q='wibbling the blarg')` is layer 2 feeding a re-rank;
the headroom check is a layer-3 admission function; "next better model" is a
layer-3 ordering. The `llm` kind is layers 1+2 made first-class.

## The `llm` kind — reuse the quest/gripe shape

Structurally this is **`quest`/`gripe` again**: a card that *is a vector* +
an append-only log + a derived tote.

- **Identity = one ref per model** (`claude-opus-4-8`, `qwen-heavy`), handle
  `lm`, `emits_card` so the capability prose embeds → the card is searchable.
  `corpus_role='none'`, never exported. Migration `0071_llm_kind.sql`
  (verified free — `0070_app_settings.sql` is the latest). Handle prefix `lm`
  (verified unclaimed in `handle_registry.py`).
- **Operating points, not a row explosion.** Don't mint (model × effort ×
  window) as separate refs. The card carries an `offerings` list in `meta` —
  each `{effort, transport, endpoint, max_input, max_output, price_in,
  price_out}`. You search for a *model*; the policy returns a *(model,
  offering)*. Effort and window are axes *within* a card.
- **Body = capability prose** (embedded): "strong at multi-file refactors,
  careful SQL, weak at long-horizon planning without scaffolding." This is what
  `q='wibbling the blarg'` matches.
- **Append-only review log** (the gripe comment / quest_log WORM pattern),
  typed `published-benchmark · measured-eval · observed-telemetry ·
  agent-review`, each with `by`, date, and **provenance** — because a vendor
  MMLU number, your own `EVAL_RESULTS` accuracy, aggregated `llm_call_log`
  success rates, and "opus felt smart" are *not* the same evidence and must not
  blend. **Write surface:** the quest-logbook shape verbatim — an `edit` op on
  `kind='llm'` appending a typed, dated entry chunk (`llm_review`), never
  mutating prior entries. This is the verb the planner calls to leave "opus·
  medium was excellent at xyz"; `agentlog` attribution covers *who*, the entry's
  `type` covers *what kind of evidence*.
- **Derived tote** = a rollup query over `llm_call_log` per `(model, source)`:
  realized cost, error rate, p50 duration, turns. Same relationship the quest
  "tote" has to `quest_log`. No new store.

## Capability representation — coarse ordinal axes, not a scalar

**A single capability number is the wrong primitive** — it erases the
task-conditionality that is the entire point (if capability were one number you'd
never route *down*; you'd always pick the top affordable model). A *continuous*
number is also wrong — it implies precision the evidence can't support. The
decision is a **three-layer representation, most-specific-wins** (the same
degrade-to-floor discipline as the rest of the catalog):

1. **Ordinal tier (the floor) — already exists.** `Tier` = LOCAL_SMALL /
   CLOUD_MID / CLOUD_SUPER *is* dumb / smart / genius. The fallback when nothing
   is known about the task.
2. **A small vector of workload-relevant axes, each a coarse 1–5 ordinal** —
   axes chosen by what precis *does*, not what academia measures: `code`,
   `long-context-recall`, `tool/structured-output reliability`,
   `reasoning-convergence`, `summarize/extract`. Each score carries a
   **confidence + provenance**.
3. **Open-vocab review corpus (embedded)** for everything the fixed axes miss —
   powers `search(q=…)`.

**Most tasks route on ONE dominant axis + price** ("code, ≥strong, cheapest
such"). The full vector is for storage and search; the decision usually
collapses.

### "Reasoning" — define it operationally, don't philosophize

`reasoning-convergence` for a model = its empirical **convergence + success rate
on precis's own agentic jobs** — measurable from `llm_call_log` + the nursery's
existing **plan-tick spin** signal (a planner that "succeeds" each tick but
never converges). No abstract definition of "reasoning" is attempted or needed.

The **effort knob** (low→max) is *orthogonal* — inference-time compute on the
same weights. Represent it as a **measured modifier** on the model: `(model,
effort)` → cost multiplier + a measured quality delta on the reasoning axis
("opus@high vs @medium on the golden set: +X% success, +Y× cost"). Measured, not
reasoned about.

## Where the scores come from — your workload is the benchmark

Evidence trust ranks **observed telemetry > your own evals > public
benchmarks.** Public benches (MMLU/GPQA/HumanEval) are saturated, contaminated,
and weakly predictive of precis's tasks.

- **Primary — your replayed workload.** `scripts/classify/EVAL_RESULTS.md` is
  already per-model accuracy on *your* role axes. Generalize into a **small
  golden task set per axis** drawn from your own corpus/jobs (real `fix_gripe`
  tasks for `code`; needle-in-your-drafts for `long-context-recall`; the
  `llm_summarize` gold for `extract`). New model → run the golden set → axis
  ordinals.
- **Observed telemetry.** The `llm_call_log` rollup (the tote) — free,
  always-on, your actual workload.
- **Public benchmarks — cold-start prior only.** Low-trust, for a model not yet
  self-evaluated, overwritten the moment your own data exists. If used: SWE-bench-
  verified (code), a RULER/needle style (long-context), a tool-reliability bench
  (structured), LMArena Elo (very coarse general prior). All `published`
  provenance, weighted below measured.

## Data sources for the *facts* — harvest, don't scrape

Most static facts are self-reported; only benchmarks/issues need the outside
world.

- **OpenRouter `GET /api/v1/models`** — the richest single live feed:
  per-model `context_length`, `pricing` (prompt/completion/request/image),
  `architecture`, `top_provider` (`max_completion_tokens`), and
  **`supported_parameters`** (a structured capability envelope: `tools`,
  `reasoning`, `response_format`). A companion `…/models/:slug/endpoints`
  breaks each model down by underlying provider with per-provider price,
  context, throughput, uptime. Spans closed + open models; pricing is *live and
  authoritative* (what you'd pay through that transport). Primary reconcile feed
  for the hosted side. *(Confidence high on the shape; verify exact field names
  when wiring the reconcile — the doc-fetch failed during design.)*
- **DeepInfra** — narrower: OpenAI-compatible OSS-model serving with per-model
  price + context, thin capability metadata. Use as a cheap OSS *execution
  backend* and a price/context source for the OSS models you serve there.
- **Provider `/v1/models`** (Anthropic, OpenAI) — authoritative for *existence
  and freshness*, metadata-poor otherwise. Google Gemini `/models` is the
  exception: returns structured `inputTokenLimit`/`outputTokenLimit`.
- **litellm's bundled `model_prices_and_context_window.json`** — rich (context +
  price for ~all models) but *secondhand and lagging*. **It is a standalone file
  on GitHub — pullable via `safe_fetch` without litellm running.** So the
  teardown costs the gateway, not the data.

No single source is both authoritative and complete → **merge with provenance**;
live-vendor > secondhand-JSON, and measured > published.

## Admission vs selection — the hot-path line

Two operations, very different costs; only one is gated:

- **`admit(tokens, offering)` — pure integer arithmetic** (input token count vs
  model window + headroom). Costs nothing → **unconditional.** Every place a
  context is paired with a model, hot path included. This is the guardrail that
  refuses "100k into a 2k window" *loudly, with the numbers*, instead of silently
  truncating. Lives **inside `dispatch()`** (every routed call funnels through
  it) **plus a standalone `admit()`** the context-assembly path calls *before*
  forming the request, so it can split/trim rather than build a doomed blob.
  This argues for routing even the fixed-model mechanical passes through
  `dispatch` (tier pinned, selection skipped) so they inherit the guardrail.
- **`select_offering(requirements)` — ranking + maybe semantic search.** The
  "which model?" decision. Runs **only where `req.model`/tier isn't already
  pinned** — a decision point (a spawn/dispatch, a planner tick, an agentic
  reviewer), ~hundreds/day, where a lookup is free relative to the multi-second/
  multi-cent call it chooses for. Skipped on the hot path because the pass fixed
  the model — but the `admit` fit-check on that pinned pairing *still runs*.

On a misfit the two interlock: at a **decision point**, admission hands off to
selection ("nothing affordable fits 100k; cheapest that does is X at $Y —
proceed/split?"); at a **fixed-model point**, admission can only refuse or split
(no selection authority), but refuses loudly.

**Where the token count comes from.** Precis has no tokenizer on the hot path,
and running one per call would defeat "costs nothing." The input side of
`admit` is therefore an **estimate** (`len(chars)/4`, the standard coarse
constant), and **headroom absorbs estimator error** alongside its other job of
reserving output/thinking room. That means headroom is not a nicety — it is the
correctness margin of the whole check, which is why its default (and per-
transport variation for thinking models) is a real decision, not a config
detail. If a call site already knows a real token count (an API `usage` echo, a
prior turn), it passes that instead and the estimate is skipped.

**Refusal semantics — don't mint a new spin-loop genre.** A pinned-model worker
pass with a durably oversized input would otherwise re-claim and re-refuse
every cycle — the exact `ref_events`-flood shape the fetcher/chase backoff
fixes exist for. So a hot-path refusal is **terminal for that item (or
exponentially backed off), plus a fingerprinted `kind='alert'`** via
`raise_alert` (deduped on `(model, source)`; auto-resolves when the pairing
stops being attempted) — never a bare per-call exception that the claim loop
retries forever. The refusal message carries the numbers ("est. 100k in, window
8k, headroom 20%") so the alert is actionable without forensics.

## Who picks — split the labor

A frontier model handed the raw catalog is **good** at *task → requirement*
("hard multi-file refactor under a big context → strong tier, ≥strong-code,
≥150k window") but **bad and biased** at *requirement → model*: it's price/window-
blind (hallucinates the facts) and has a self-preference bias (the "just use
opus" reflex). So:

> The **LLM maps task → requirement vector** (judgment it's good at). The
> **deterministic `select_offering` maps requirement → model** via the catalog's
> facts (a lookup + Pareto rank the LLM is biased about). Never hand the raw
> catalog to the model to pick from directly.

The LLM stays a **judge of fit** — "here's the requirement I inferred, here's
what the policy selected and why, escalate if it's weak" — not a picker from a
40-model list it can't hold in its head. This keeps selection deterministic and
cheap even with a smart model in the loop.

### `select_offering` mechanism

1. **Hard filter** — `admit` (window + headroom), budget band (`gate_tier`),
   availability (reconcile health), required flags (`supported_parameters`:
   needs tools? structured output?).
2. **Rank** — for the requirement's dominant axis, survivors meeting the minimum
   ordinal, pick cheapest. (Semantic reviews break ties / handle the tail.)
3. **"Next better"** — the next model up the ordinal on that axis: a **Pareto
   step over (capability, cost)**, reusing the `quest/frontier.py` primitive from
   quest slice 4b. Every escalation still passes `gate_tier`, so auto-escalation
   can't blow the budget.

## Unification + litellm teardown — transport lives on the card

The two-endpoints / two-tier-maps mess resolves cleanly:

> **litellm's job is transport** (name→backend, load-balance, retry). **The
> router + catalog's job is policy** (which name to ask for). Don't duplicate the
> proxy's fallback ladder in code; don't duplicate selection in the proxy.

Today `select_transport(tier)` *derives* transport from tier. Flip it —
**transport/endpoint becomes a property of the offering in the catalog.**
`Transport.LITELLM` and `Transport.OPENAI_COMPAT` are the same protocol;
collapse to one OpenAI-compatible provider parameterized by `(base_url, key)`.
Then the loopback proxy is "one endpoint, keyless" and hosted OSS is "another,
keyed" — **two catalog entries, not two hardcoded seams** (dissolving the
`PRECIS_SUMMARIZE_LLM_URL` vs `PRECIS_LLM_BASE_URL` split).

**Capability is a property of `(model × transport)`, not the model alone.**
`claude-opus via claude -p` = MCP + thinking + caching; `claude-opus via
litellm` = OpenAI completions, no MCP, degraded thinking — same weights,
different envelope. The card records the envelope per offering, which is *why*
transport belongs on the card. (This is also why the agentic paths already
bypass litellm — the OpenAI-completions shape can't carry Anthropic's native MCP
connector, the `claude -p` harness, extended-thinking fidelity, or prompt-cache
breakpoints.)

**Teardown sequencing:** the reconcile pass harvests litellm's bundled model DB
+ `/model/info` into `llm` cards *now*, so when the proxy dies the facts already
live in precis (fetched thereafter from the standalone GitHub JSON + provider
`/v1/models`). The catalog is where model knowledge lands post-teardown.

## Slices

- **Slice 1 — read-only catalog + reconcile (no policy, no router change).**
  `kind='llm'` + migration; seed cards for the models actually run (context/price
  from OpenRouter + the pullable litellm JSON + `PRICE_TABLE`, provenance-tagged).
  **Local-model facts:** `llamacpp_catalog` is ansible-side cluster data a precis
  worker can't read — the live source is the llama.cpp server itself (`/props` /
  `/v1/models` on the loopback endpoint, which reports the *loaded* `ctx_size`),
  with the ansible catalog used once at seed time by hand. The reconcile asks the
  running server, not the config that launched it — truer anyway (a model
  launched with a smaller `-c` than the GGUF supports has the smaller window).
  A `llm_reconcile` worker pass (the
  `corpus_reconcile`/`paper_reconcile` pattern) keeps them true and flags drift
  (the opus-4-8-not-in-proxy bug) + dead endpoints. `search(kind='llm')` +
  `get(kind='llm', id=…)`. **Proves the data model, fixes drift, is step 1 of the
  litellm teardown.** No hot-path touch.
- **Slice 2 — `admit()` everywhere.** The pure fit-check in `dispatch()` + a
  standalone `admit()` for the context-assembly path. Route the fixed-model
  passes through `dispatch` so they inherit it. Refuses doomed pairings loudly.
- **Slice 3 — the telemetry tote + review log.** Roll `llm_call_log` into each
  card per `(model, source)`; the append-only review log with provenance; the
  golden-task eval harness generalized from `EVAL_RESULTS`.
- **Slice 4 — `select_offering` + Tier-as-floor.** Deterministic
  requirement→model + Pareto "next better". Wire the router's deliberative call
  sites. For `plan_tick`: an `LLM:opus|sonnet|haiku` tag **is a pin** — it keeps
  resolving directly (now *through the catalog*, so it inherits `admit` and
  drift-corrected model ids), and `select_offering` runs **only when the tag is
  absent or a requirement vector is supplied**. (The earlier phrasing "borrow
  `select_offering` instead of `resolve_model`" would have run ranking on a
  pinned call, contradicting the decision-point doctrine.)
- **Slice 5 — the agent surface.** The LLM task→requirement judge; expose the
  menu to the coder agent (Claude Code / fixer) via `search/get(kind='llm')` + a
  `precis-llm-help` skill. Optional low-frequency Perplexity/websearch enrichment
  job for the `published` band + known-issues prose.

## In scope

The `llm` kind (card + offerings + review log), the reconcile pass, `admit` +
`select_offering`, the `llm_call_log` tote, the golden-task eval harness, the
capability representation (ordinal tier floor + coarse-ordinal axes + reviews),
transport-on-the-card, and the first step of the litellm teardown.

## Explicitly NOT in scope

- **Replacing `Tier`.** Tier stays the **floor**; empty catalog ⇒ byte-identical
  to today. The catalog *enriches*, never *requires*.
- **The hot path making selection decisions.** Only `admit` runs there; ranking
  never does.
- **Completing the litellm teardown.** This proposal *harvests* litellm's facts
  and collapses the transport seam; ripping out the proxy + its ansible role is a
  follow-on.
- **A new agentic transport.** No new provider adapter beyond collapsing
  LITELLM/OPENAI_COMPAT; the `claude -p` / OpenAI-tools surfaces stay as-is.
- **Trusting public benchmarks as authority.** They are a low-trust cold-start
  prior only.

## Acceptance criteria

- `get(kind='llm', id='claude-opus-4-8')` returns a card with real context
  window, price, transport/offerings, and provenance-tagged facts; `search(kind=
  'llm', q=…)` returns models ranked by capability match.
- The reconcile pass flags at least the known `claude-opus-4-8`-not-in-proxy
  drift.
- `admit()` refuses an oversized (context, model) pairing with the numbers, on a
  fixed-model path — and a *repeated* refusal produces one deduped alert plus a
  backed-off/terminal item, not a `ref_events` flood.
- `select_offering` returns a `(model, offering)` + a "next better", filtered by
  window/budget/availability, with **empty catalog ⇒ identical to `resolve_model`
  today**.
- No hot-path per-item pass performs a ranking/semantic lookup.

## Target + blast radius

`utils/llm/router.py` (dispatch, `select_transport`, `_TIER_MODEL`), a new
`handlers/llm.py` + migration, a new `workers/llm_reconcile.py`, `route_log.py` /
`budget/*` (tote query), `workers/job_types/plan_tick.py` (selection reach),
`utils/safe_fetch.py` callers (OpenRouter / GitHub JSON), and a new
`precis-llm-help` skill. Cluster: the litellm ansible role (teardown sequencing,
later).

## Open questions / decisions log

**Decided (this conversation):**
- Card granularity = per-model; effort/window = offerings within, not new refs.
- Capability = ordinal tier floor + coarse 1–5 ordinal axes + open-vocab reviews;
  **not** a single or continuous scalar.
- "Reasoning" = operational convergence/success on precis's own jobs; effort = a
  measured modifier.
- Evidence trust: observed > measured > published; provenance on every claim.
- `admit` unconditional (incl. hot path); `select_offering` gated to decision
  points.
- Who picks: LLM does task→requirement, deterministic policy does
  requirement→model.
- Tier is the floor; catalog ships dark.
- Transport lives on the card; collapse LITELLM/OPENAI_COMPAT; this is step 1 of
  the litellm teardown.

**Decided (review pass, same day):**
- Migration `0071` + handle `lm` — both verified free.
- `admit`'s input side is a chars/4 **estimate** (real count passed through when
  a call site has one); headroom's primary job is absorbing estimator error +
  output/thinking reservation.
- Hot-path refusal = terminal-or-backoff per item + fingerprinted `kind='alert'`
  (deduped, auto-resolving), never a bare exception the claim loop retries —
  no new spin-loop genre.
- An `LLM:*` tag is a **pin**: `plan_tick` resolves it through the catalog
  (inherits `admit` + drift-corrected ids) but never triggers ranking; selection
  only where the tag is absent or a requirement is supplied.
- Local-model facts come from the running llama.cpp server (`/props` /
  `/v1/models`), not the ansible-side `llamacpp_catalog` a worker can't read
  (seed-time hand-import excepted).
- Agent reviews are written via `edit` on `kind='llm'` appending a typed, dated
  `llm_review` entry chunk — the quest-logbook WORM shape verbatim.

**Open:**
- Exact OpenRouter `/api/v1/models` field names (verify at build; design-time
  fetch failed).
- The axis set — confirm the 5 (`code`, `long-context-recall`, `tool/structured`,
  `reasoning-convergence`, `summarize/extract`) survive contact with the golden
  sets, or need a 6th (multilingual? vision?).
- Headroom **default value** (20% is the working number) and its per-transport
  variation — now load-bearing, since headroom also covers chars/4 estimator
  error. Calibrate empirically in slice 2 from `llm_call_log` `usage` echoes
  (estimated vs actual tokens on real traffic) rather than argue it a priori.
- Whether the OSS-model price should be modeled as a real per-token number or
  just the FREE band (local) / real number (hosted OSS).
- The reconcile's drift assertion (`claude-opus-4-8` absent from the proxy) is a
  prod fact from memory, not code — re-verify it still holds at build time; if
  the opus-4.8 consolidation deploy landed first, pick another seeded drift case
  for the acceptance test.
