---
status: draft
title: Cloud-bypass for unstable local LLM serving, routed through OpenRouter
model: opus
---

# Cloud-bypass for unstable local LLM serving, routed through OpenRouter

## Motivation / why

Melchior's local serving stack (litellm proxy → llama-swap) is currently
flaky under memory pressure: swapping between differently-sized models
under concurrent load takes 10-15s per swap, and a config drift
(`PRECIS_SUMMARIZE_MODEL=qwen`, a litellm alias that never matches a real
`served_by` model id) let `local-small` traffic fall through to the proxy
silently instead of engaging direct local-serving — flooding
`worker_logs` with 2782+ connection-refused errors in 24h (documented in
`OPEN-ITEMS.md` §"Track 2 — litellm-retire transport-collapse", live
2026-07-23). That specific config drift is already hotfixed on the plist
(and `9ab8e7a7`, this repo, adds the templated per-host override) — but
the deeper issue is that **local hardware serving large models is
inherently a shared, memory-pressured resource**, and a today-fix for one
symptom doesn't remove tomorrow's.

Reto's ask: stop depending on local-serving stability *right now* — route
that traffic through OpenRouter instead, as a resilience/cost-bearing
substitute, while the local-cluster stability work (a parallel session is
already porting the `served_by`/litellm-alias fix into `~/work/cluster`'s
ansible) proceeds as its own, separate optimization. This proposal is
scoped to that bypass — **not** to the fleet-wide "wean off Opus" ambition
in `OPEN-ITEMS.md`'s todo list (though it reuses the same mechanism and
sets up that follow-on cleanly).

**What's already built (ADR 0046, `utils/llm/router.py`) — this proposal
adds one small, symmetric gap-fill on top, not a new subsystem:**

- A `Tier` (`local-small` / `local-big` / `cloud-small` / `cloud-mid` /
  `cloud-super`) names capability; a `Backend` (`anthropic` / `openai`,
  `PRECIS_LLM_BACKEND`, live-switchable via `/factory`'s `app_settings`
  override, `utils/llm/live_config.py`) picks the cloud vendor family.
  `cloud-*` tiers already fork cleanly onto an OpenAI-compatible hosted
  backend (`PRECIS_LLM_BASE_URL` + vaulted `PRECIS_LLM_API_KEY`) when
  `backend=openai` — this is **config-only**, no code change, and it's
  what today routes `dream`, the structural/deep reviewers,
  `cad_propose`/`structure_propose`, the web follow-up, and the
  `claude_p` judges (chase, good-search triage, figure) — unit 4b is done
  fleet-wide for the cloud tiers.
- `PRECIS_LLM_FAILOVER=1` wraps an OSS primary transport in a
  `FailoverProvider` that automatically falls back to `claude` on any
  transport error (`router._failover_ladder` — eligible whenever the
  primary transport is `OPENAI_TOOLS` or `OPENAI_COMPAT`). This is the
  resilience net that makes flipping to OpenRouter *safe* — a bad
  OpenRouter day degrades to Claude, not to a hard failure. Off by
  default; this proposal turns it on for the tiers it retargets.
  **Correction (surfaced by the parallel local-stability session,
  2026-07-23): this ladder is currently unreachable on a *paused local
  slot*.** `dispatch()` calls `local_serving.acquire()` and, when the
  slot is `paused` (all capacity busy), returns the paused
  `LlmResult` **directly** — before `provider.run()` (where the ladder
  logic lives) is ever invoked. So `FAILOVER` today only protects
  against a transport-level error from a call that *did* go out, not
  against local-serving saturation itself. Falling over to OpenRouter
  *on a busy/paused local slot* (rather than only on a hard transport
  error) is real, additional work — see item 4 in Open questions.
- The `kind='llm'` catalog already has a **fully-priced OpenRouter
  frontier ladder** seeded and reconciled (`llm_catalog.seed_frontier_cards`,
  12 cards, `lm162503`–`lm162514`), spanning `cloud-super`/`cloud-mid`/
  `cloud-small` — see the roster audit below. This is real, current
  (pricing pulled live from prod 2026-07-23) OpenRouter pricing/capability
  data, not guesses.

**The one real gap.** `Tier.LOCAL_BIG` already has a hosted-backend
fallback: with no `served_by` local slot, `OPENAI_TOOLS` falls through to
`PRECIS_LLM_BASE_URL` (dark by default) — so `local-big` can already reach
OpenRouter once base-url/model/key are configured and no local slot is
seeded for that model id. **`Tier.LOCAL_SMALL` has no such fallback** —
`select_transport` pins it unconditionally to `Transport.LITELLM`
(`router.py:279-280`), whose provider (`LitellmProvider` →
`_dispatch_local`) always targets a local `LlmConfig` base url (the
litellm proxy, or a `served_by`-resolved llama-swap endpoint) — never
`PRECIS_LLM_BASE_URL`, never a vaulted key. This is exactly the tier
carrying today's flaky traffic (`llm_summarize` / `classify` /
`paper_glossary`), and it is structurally unable to reach OpenRouter
without a code change.

## In scope

1. **A master local-serving bypass switch** — a new env flag (name TBD in
   review, suggest `PRECIS_LOCAL_SERVING_DISABLED`) that, when set, makes
   `dispatch()`/`dispatch_async()` skip `local_serving.acquire()`
   entirely for `local-small`/`local-big` calls, so neither tier ever
   reaches for local hardware regardless of `served_by`/`resource_slots`
   state. Dark by default (unset = today's behavior, byte-identical) —
   this is additive, not a rewrite of the local-serving mechanism the
   parallel session is stabilizing; it's an independent kill-switch that
   sits *above* it.
2. **Give `local-small` the same hosted-backend fallback `local-big`
   already has.** Add a `backend`-aware branch to `select_transport` for
   `Tier.LOCAL_SMALL`: when the bypass is active (or, symmetrically, when
   `backend is Backend.OPENAI`), route to `Transport.OPENAI_COMPAT`
   instead of `Transport.LITELLM`. This is the one actual code change —
   small, mirrors the existing `LOCAL_BIG`→`OPENAI_TOOLS` pattern, and
   makes `local-small` eligible for the `FailoverProvider` ladder for
   free (today it has zero fallback) — **for transport errors only**
   (see the correction above; a paused/busy slot is a separate case,
   below).
3. **Wire a per-call OpenRouter fallback on local-serving saturation** —
   the request the parallel local-stability session made explicitly:
   when `local_serving.acquire()` returns a paused (all-slots-busy)
   result — or, per the open scope question below, when the
   litellm/llama-swap endpoint is unreachable — `dispatch()` should feed
   that case into the `FailoverProvider`/`Rung` ladder (an `OPENAI_COMPAT`
   rung) instead of returning the paused error immediately. This is
   **real, non-trivial work**, not a flag flip: it means restructuring
   `dispatch()`'s paused-slot branch so it degrades into the ladder
   rather than short-circuiting before it. Distinct from — and
   complementary to — item 1's manual bypass switch: item 1 is a blunt
   "never touch local hardware" kill switch (useful for today's
   incident); this item is the standing, automatic "local is saturated
   right now, borrow OpenRouter for this one call" resilience behavior
   that would still be valuable once local serving is stable again.
   Scope questions (trigger condition, default-on vs opt-in given real
   $ spend) are open — see below; this item is not committed to build
   without that direction.
4. **A recommended OpenRouter model roster** (below) to bind
   `PRECIS_MODEL_OPUS`/`PRECIS_MODEL_SONNET`/`PRECIS_MODEL_HAIKU`/
   `PRECIS_SUMMARIZE_MODEL`/`PRECIS_LOCAL_BIG_MODEL` to, sourced from the
   catalog's already-reconciled OpenRouter frontier ladder — not new
   picks, just a decision about which of the 12 already-seeded cards
   becomes the *default* per tier (the catalog itself has no
   golden-eval-backed auto-picker yet, so this is a human decision, not
   `select_offering`'s job today).
5. **The ops recipe**: `PRECIS_LLM_BACKEND=openai` +
   `PRECIS_LLM_BASE_URL=https://openrouter.ai/api/v1` + vault
   `PRECIS_LLM_API_KEY` (already present locally at
   `~/.secrets/pw/openrouter_api_key` — needs vaulting for the cluster) +
   `PRECIS_LLM_FAILOVER=1` + the new bypass flag, applied fleet-wide via
   the (unreachable-from-here) `~/work/cluster` overlay — same ops
   pattern as the `served_by` env-var port already tracked in
   `OPEN-ITEMS.md`.

## Explicitly NOT in scope

- **Not** flipping the fleet's default cloud backend to OpenRouter for
  its own sake (the `OPEN-ITEMS.md` "wean off Opus" ambition). Cloud-tier
  work (reviewers, `fix_gripe`, `plan_tick`'s `LLM:opus` ticks, the
  generic `claude_agent` default) is not part of today's outage and stays
  on Anthropic unless a *separate*, more deliberated proposal (gated on
  the golden-eval harness — see Open questions) decides otherwise. This
  proposal's roster recommendations exist so that decision, if made
  later, doesn't start from zero — but making it is out of scope here.
- **Not** touching `local_serving.py`'s `served_by`/`resource_slots`
  seeding, `advertise_local_llm()`, or the llama-swap-direct routing
  mechanism itself. That's the parallel session's track
  (`docs/design/local-model-router-integration.md`, the ansible port in
  flight on `dapper-scribbling-bird`). This proposal adds a switch that
  sits *above* that mechanism and does not touch its internals.
- **Not** building the golden-task eval harness (`record_eval` write
  surface exists; the harness doesn't — see `llm_catalog_proposal`
  memory / `OPEN-ITEMS.md`). The roster below is
  `published-benchmark`-band evidence only (vendor claims), not measured
  on precis's own workload — flagged explicitly, not hidden.
- **Not** a per-tier backend override (e.g. "keep `deep_review` on
  Anthropic while everything else flips to OpenRouter"). `PRECIS_LLM_BACKEND`
  is fleet-wide today; scoping it finer is a real gap this proposal
  surfaces (see Open questions) but does not build.
- **Not** containerizing or otherwise changing `plan_tick`'s or
  `fix_gripe`'s own spawn seams — out of scope per `OPEN-ITEMS.md` Track 3.

## Recommended OpenRouter roster (audit of the already-seeded catalog)

All pricing/capability below is from the live `kind='llm'` catalog,
pulled 2026-07-23 (`$/M tokens`, in/out; capability axes are 1-5 ordinals,
**published-benchmark band — vendor claims, not yet measured on our own
traffic**).

| Tier | What it's for | Recommended default | Price in/out | Window | code / tool / reasoning |
|---|---|---|---|---|---|
| **cloud-super** ("opus tier") | Heavy reasoning + tools: structural/deep reviewers, `fix_gripe`, `dream`, generic `claude_agent` default, `LLM:opus` ticks | **`z-ai/glm-5.2`** — the cheap-massive model that comes closest to Opus: 744B MoE, first OSS to beat GPT-5.5 on SWE-bench Pro, 1M context | $0.97 / $3.05 | 1M | 5 / 5 / 5 |
| — cheaper alternative | Pure-reasoning judge work, less code-shaped | `deepseek/deepseek-v4-pro` | $0.44 / $0.87 | 1M | 4 / 4 / 5 |
| **cloud-mid** (workhorse) | Planner ticks, tex-fix, general agentic | **`z-ai/glm-4.7`** | $0.40 / $1.75 | 203K | 4 / 4 / 4 |
| — long-context alternative | Same rung, wider window | `qwen/qwen3.7-max` | $1.48 / $4.43 | 1M | 4 / 4 / 4 |
| **coder** (specific coding rung) | Coding-shaped tasks (a future `coder`-agent model default, or dep-bumper/mechanical-refactor work) | **`moonshotai/kimi-k2.7-code`** — already coding-specialised, cheaper than the mid workhorses for this shape | $0.75 / $3.50 | 262K | 5 / 5 / 4 |
| **cloud-small** (triage/judge) | One-shot JSON judges, classification, chase-verifier | **`deepseek/deepseek-v4-flash`** — cheapest model that still reasons | $0.098 / $0.196 | 1M | 3 / 3 / 4 |
| — floor alternative | Pure lexical classification, no reasoning need | `z-ai/glm-4.7-flash` | $0.06 / $0.40 | 203K | 3 / 3 / 3 |
| **local-small / local-big bypass target** | `llm_summarize`/`classify`/`paper_glossary` and the local-big tools rung, when the bypass is active | Same as **cloud-small** default above | — | — | — |

All 12 candidate cards (`lm162503`-`lm162514`) are visible via
`get(kind='llm', id='<slug>')`; this table is a recommendation, not an
exhaustive relisting.

## Acceptance criteria

1. With the bypass flag **unset**, every existing test + a live dispatch
   of `local-small`/`local-big` behaves byte-identically to today (no
   regression when this ships dark).
2. With the bypass flag **set** + `PRECIS_LLM_BACKEND=openai` +
   `PRECIS_LLM_BASE_URL` + a vaulted key: a `local-small` dispatch
   (`llm_summarize` or an equivalent test harness call) POSTs to
   OpenRouter, not to the litellm proxy or any `127.0.0.1:114xx` — verified
   in a worker log or a targeted probe, mirroring the verification recipe
   in `docs/design/local-model-router-integration.md` §S4.
3. Same for `local-big` (already has the fallback path — confirm the
   bypass flag reaches it too, i.e. `local_serving.acquire()` is skipped
   even when a `served_by` slot *would* otherwise match).
4. `PRECIS_LLM_FAILOVER=1` + a forced OpenRouter error (e.g. a bad model
   id or a simulated 5xx) causes the call to fall back to `claude_p` and
   return a usable, error-free `LlmResult` — a new regression test
   alongside the existing failover-ladder tests.
5. `local-small`'s new `OPENAI_COMPAT` branch is covered by
   `select_transport` unit tests (mirrors the existing `LOCAL_BIG` case)
   and the resolver totality assert still holds.
6. Ops recipe (env vars + vault key) is documented in
   `docs/reference/config-variables.md`, with an explicit note that the
   fleet-wide values are set via `deploy/inventory`'s host_vars
   (gitignored/private, not present in a fresh worktree — same place the
   `served_by`-override values from `9ab8e7a7` still need setting).
7. (If item 3 is greenlit) a forced "all slots busy" `paused` result
   degrades into the `OPENAI_COMPAT` rung and returns a usable result,
   with a log line distinguishing "fell back due to local saturation"
   from "fell back due to a transport error" — the two are different
   operationally (one is capacity tuning, the other is an outage).

## Target + blast radius

- `src/precis/utils/llm/router.py` — `select_transport` (new
  `local-small` branch), `dispatch()`/`dispatch_async()` (bypass gate
  before `local_serving.acquire()`; if item 3 is greenlit, also the
  paused-slot branch that currently returns early instead of entering
  the `FailoverProvider` ladder).
- `src/precis/utils/llm/local_serving.py` — read-only reference point for
  the bypass gate; no internal changes, unless item 3's paused-result
  shape needs a field to carry "why" (busy vs unreachable) to the ladder.
- `tests/test_llm_router.py` (or equivalent) — new cases for the
  `local-small` OpenRouter branch + bypass-flag-on/off + failover +
  (if item 3 ships) paused-slot-degrades-to-ladder.
- `docs/reference/config-variables.md`, `docs/architecture/state-map.md`
  (LLM router section) — document the new flag + roster decision in the
  same commit (per this repo's doc-freshness convention).
- No migration, no schema change, no new `kind`. All call sites
  (`llm_summarize.py`, `classify.py`, `paper_glossary.py`) are unaffected
  at the source level — they still request `Tier.LOCAL_SMALL`/
  `Tier.LOCAL_BIG`; only the transport resolution changes.
- Ops-side: `deploy/inventory` host_vars — gitignored/private, not
  present in this worktree, flagged as a follow-on ops step to apply
  wherever ansible actually runs from.

## Open questions / decisions log

- **Exact flag name** for the bypass switch
  (`PRECIS_LOCAL_SERVING_DISABLED` suggested here) — confirm against
  house naming before merge.
- **Per-tier backend override.** Today `PRECIS_LLM_BACKEND` is one
  fleet-wide switch. If a later decision wants `cloud-super` to stay on
  Anthropic while `cloud-small`/`local-*` move to OpenRouter, that needs
  a finer-grained override (`PRECIS_LLM_BACKEND_<TIER>`?) not built here —
  surfaced but deliberately deferred (this proposal's scope is the local
  tiers only, which don't yet have *any* backend switch, so the question
  doesn't block this work).
- **Roster confirmation.** The table above is a recommendation from
  published-benchmark data; Reto should confirm (or override) the picks
  before they're wired into env defaults, especially the `coder` pick
  (`kimi-k2.7-code`) since no agent def currently names a model override
  that would consume it — is this proposal expected to also wire that
  agent-def hookup, or just make the model available in the roster for a
  later, separate wiring pass?
- **Item 3's trigger + cost scope (raised verbatim by the parallel
  local-stability session, 2026-07-23) — awaiting direction before
  building:**
  - Trigger only on "busy" (`local_serving.acquire()` returns
    `paused`), or also on litellm/llama-swap being unreachable
    (a connection error, not just a capacity gate)? The two are
    different failure shapes — busy means "healthy but full,"
    unreachable means "actually down" — and may want different
    handling (busy → wait-a-beat-then-retry-local is arguably fine;
    unreachable → OpenRouter immediately is more clearly justified).
  - Default-on for `llm_summarize` (and the other `local-small`
    consumers), or opt-in? OpenRouter is real $ spend against a task
    that's normally free local backfill — worth being deliberate about
    which passes are allowed to silently start costing money the first
    time local is merely busy, versus only when it's actually down.
  - Related: does this want to be a manual, sticky operator flag (item 1
    covers that shape already, as a full stop) or a scoped, automatic
    per-call fallback that self-heals once local capacity frees up
    (item 3 as drafted) — these aren't mutually exclusive, but the
    build is different work either way.
