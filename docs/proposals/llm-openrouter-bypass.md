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
  **Built 2026-07-23 (item 3): the paused-slot gap is closed.** The
  correction below described a real bug (`dispatch()` returned a paused
  local-serving result *before* `provider.run()` — where the ladder lives
  — ever ran); it's now fixed: a paused slot retries the ladder's rung 0
  with no `local_url` override, landing on the hosted OSS endpoint instead
  of the busy local hardware, falling to claude only if that also errors.
  Building this also surfaced and fixed an independent latent bug: a
  `LOCAL_*` tier's claude-fallback rung previously pinned
  `_TIER_MODEL[tier][1]`, which for `LOCAL_BIG`/`LOCAL_SMALL` is an OSS
  alias (`qwen-heavy`), not a claude id — nonsense that would have been
  sent to `claude -p` as `model=`. `_LOCAL_ESCALATION_TIER` now maps
  `LOCAL_BIG`→`CLOUD_MID` (sonnet) per the roster's "medium" cascade role;
  `LOCAL_SMALL` gets no claude rung at all, per the roster's "small" row
  (skip Anthropic entirely).
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

1. **A master local-serving bypass switch** (`PRECIS_LOCAL_SERVING_DISABLED`
   or similar) — **not built.** Superseded by item 3: with the paused-slot
   ladder-fallback in place, there's no known scenario left that needs a
   separate blunt "never touch local hardware" flag — item 3's automatic
   degrade-to-OpenRouter covers the same ground without an operator having
   to remember to flip anything. Revisit only if a future incident needs a
   hard, unconditional local-hardware quarantine (e.g. a host is actively
   misbehaving, not just busy) that the ladder's busy/error-driven logic
   wouldn't catch.
2. **Give `local-small` the same hosted-backend fallback `local-big`
   already has.** **Built 2026-07-23**: `select_transport` now has a
   `backend`-aware branch for `Tier.LOCAL_SMALL` — `backend is
   Backend.OPENAI` routes it to `Transport.OPENAI_COMPAT` instead of
   `Transport.LITELLM`. Mirrors the cloud tiers' existing split (not
   `LOCAL_BIG`'s pattern, which is unconditional regardless of `backend` —
   `local-small` needed the conditional form since, unlike `local-big`, it
   has no tools-loop transport to always fall onto). Makes `local-small`
   eligible for the `FailoverProvider` ladder for free once `backend=openai`
   (today it has zero fallback under the default `backend=anthropic`).
3. **Wire a per-call OpenRouter fallback on local-serving saturation** —
   **built 2026-07-23** (fixed 2026-07-23 post-review: the first cut
   over-triggered on `isinstance(provider, FailoverProvider)` alone, which
   is true for *every* ladder shape whenever the flag is on — including
   `local-small`'s single-rung `LITELLM` ladder under the default
   `anthropic` backend, which would've just re-hit the same saturated
   loopback proxy instead of escaping. Gate is now also `transport in
   (OPENAI_TOOLS, OPENAI_COMPAT)` — the two transports that actually read
   `PRECIS_LLM_BASE_URL` when `local_url` is unset). `dispatch()`'s
   paused-slot branch (all local slots busy) now checks whether the ladder
   was built as a `FailoverProvider` with a hosted-capable rung 0 (i.e.
   `PRECIS_LLM_FAILOVER=1` and the tier's primary transport is
   OSS-eligible); if so, it retries the ladder from rung 0 with the
   *unmodified* request (no `local_url` override), which lands on the
   hosted OSS endpoint (`PRECIS_LLM_BASE_URL`) instead of the busy local
   slot — falling through to the claude rung only if that also errors.
   `local-small` under the default `anthropic` backend (transport
   `LITELLM`, no hosted mode) still backs off immediately, unchanged.
   Gated on the existing `PRECIS_LLM_FAILOVER` flag rather than a new one
   (settles the "default-on vs opt-in" open question inline — reuses the
   established off-by-default convention rather than proliferating flags).
   Trigger condition settled too: only the modeled `paused` (busy) state —
   "endpoint unreachable" isn't a distinct signal `local_serving.acquire()`
   produces; a genuinely unreachable local endpoint surfaces as a normal
   transport error *after* `provider.run()`, which the ladder already
   handled before this change. Distinct from item 1 (not built, see above).
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

## Recommended OpenRouter roster — DECIDED 2026-07-23

All pricing/capability below is from the live `kind='llm'` catalog,
pulled 2026-07-23 (`$/M tokens`, in/out; capability axes are 1-5 ordinals,
**published-benchmark band — vendor claims, not yet measured on our own
traffic** — see the golden-eval open question below).

The settled design is a **cascade per tier**: local (this host's resident
model, free) → OpenRouter open-weight (cheap, cloud) → real Anthropic
(reserved, deliberate escalation — not the default anymore). Reuses the
exact `FailoverProvider`/`Rung` ladder already built for the cloud tiers;
item 3 below extends it with a prepended local rung rather than inventing
a separate mechanism.

| Tier | What it's for | Local primary | OpenRouter rung | Anthropic escalation rung |
|---|---|---|---|---|
| **small** | dispatch/classify/triage — today's "haiku" duty | `a` = qwen3.6-27b-q8_0 (melchior, resident) | **`deepseek/deepseek-v4-flash`** — $0.098/$0.196, 1M window, 3/3/4 | **None — Claude Haiku is skipped entirely.** Low quality-risk, high volume; no reason to pay Anthropic here. |
| **medium** | planner ticks, tex-fix, general agentic — today's "sonnet" duty | `n` = qwen3-next-80b-a3b (melchior, resident) | **`z-ai/glm-4.7`** — $0.40/$1.75, 203K window, 4/4/4 (long-context alt: `qwen/qwen3.7-max`, $1.48/$4.43, 1M) | **`claude-sonnet-5`** — kept as the failover rung (pinned, ignores model overrides). Quality-sensitive (planning/decomposition) — roll out with `PRECIS_LLM_FAILOVER=1` and watch `view='tote'` before fully trusting it. |
| **big / biggest-open** | heavy reasoning approaching top-tier, coding-adjacent | — (nothing local fits without evicting `a`/`n`) | **`z-ai/glm-5.2`** — $0.97/$3.05, 1M window, 5/5/5/5, first OSS to beat GPT-5.5 on SWE-bench Pro. This is the new *default* top-of-ladder pick. | **`claude-opus-4-8`** — the failover rung, reached for only if GLM-5.2 doesn't converge. No longer the default agentic driver — deliberate top-dog escalation only. |
| **coder** (cross-cutting) | coding-shaped tasks at any tier | — | **`moonshotai/kimi-k2.7-code`** — $0.75/$3.50, 262K window, 5/5/4 | (inherits whichever tier it's dispatched under) |

Cheaper reasoning alternative (not the default): `deepseek/deepseek-v4-pro`
($0.44/$0.87, 4/4/5) — less code-shaped, fine for pure-reasoning judges.
Absolute floor for small: `z-ai/glm-4.7-flash` ($0.06/$0.40, 3/3/3) — pure
lexical classification with no reasoning need.

All 12 candidate cards (`lm162503`-`lm162514`) are visible via
`get(kind='llm', id='<slug>')`.

**Local roster, confirmed 2026-07-23:** melchior already runs the `a`
(27B)/`n` (80B) split coresident (~106GB/192GB) — no eviction between
them since the 2026-06-25 consolidation. Balthazar has its own analogous
small model (`qwen3.6-35b-a3b-ud-q3_k_m`, 3B active MoE) but currently
carries **zero** local-tier traffic (classify/summarize are melchior-only
today) — no urgent need to homogenize. Caspar (DB/infra host) and spark
(compute-only, `llamacpp_models: []`) are correctly excluded from serving
any model. **Explicitly decided against:** cross-host fallback (melchior
borrowing balthazar's spare capacity) — `served_by`/`local_serving` is
host-scoped by design; OpenRouter is the shared-capacity answer instead of
building cross-host routing.

## Acceptance criteria

1. ✅ **Built + tested.** With `PRECIS_LLM_BACKEND` unset (default
   `anthropic`) and `PRECIS_LLM_FAILOVER` unset, `local-small`/`local-big`
   behave byte-identically to before this change — `select_transport`'s new
   branch only fires under `backend is Backend.OPENAI`, and the paused-slot
   ladder-retry only fires when `isinstance(provider, FailoverProvider)`
   (i.e. failover is on). Covered by
   `test_dispatch_paused_local_slot_without_failover_still_returns_paused`.
2. Live-cluster verification (POSTs to OpenRouter, not the litellm proxy)
   is **not yet done** — needs `PRECIS_LLM_BACKEND=openai` +
   `PRECIS_LLM_BASE_URL` + the vaulted key actually applied via
   `deploy/inventory` host_vars (still private/gitignored, not present in
   this worktree). Unit-level coverage (mocked transports) is in place;
   this criterion is the remaining ops step, not a code gap.
3. Same as #2 — `local-big`'s existing fallback path is unit-tested; live
   verification is the same pending ops step.
4. ✅ **Built + tested** — `test_dispatch_failover_flag_falls_back_to_claude`
   (pre-existing, cloud tiers) plus the new
   `test_dispatch_paused_local_slot_still_falls_to_claude_if_hosted_also_fails`
   (local tier, paused + hosted-rung-also-errors → claude).
5. ✅ **Built + tested** — `test_select_transport_openai_backend` covers the
   new `LOCAL_SMALL`/`OPENAI_COMPAT` case; `test_tier_table_is_total` (the
   resolver totality assert) is unaffected and still passes.
6. ✅ **Documented** — `docs/reference/config-variables.md` §4 and
   `docs/architecture/state-map.md`'s LLM-independence section updated in
   the same commit as the code. The fleet-wide apply (host_vars) remains a
   separate ops step (see #2).
7. ✅ **Built + tested** —
   `test_dispatch_paused_local_slot_falls_back_to_hosted_rung` (paused →
   hosted rung, no `local_url`, distinct log line
   `"llm-failover: local slot for %s is saturated…"` vs. the pre-existing
   `"llm-failover: rung %d … failed"` / `"fell back to rung %d"` transport-
   error lines).

## Target + blast radius

- ✅ `src/precis/utils/llm/router.py` — `select_transport` (new
  `local-small` branch), `dispatch()`'s paused-slot branch (degrades into
  the `FailoverProvider` ladder instead of returning early),
  `_failover_ladder`/`_claude_default` (`_LOCAL_ESCALATION_TIER` fix for
  the `LOCAL_*`-tier claude-fallback model id). `dispatch_async()` needed
  no change — for every non-`CLAUDE_AGENT`+`on_event` case (all local-tier
  traffic) it already delegates straight to the now-fixed sync `dispatch()`.
- `src/precis/utils/llm/local_serving.py` — untouched, as planned (no
  internal changes; `dispatch()` just reads `slot.paused` as before).
- ✅ `tests/test_llm_router.py` — new cases: the `local-small`
  `OPENAI_COMPAT` branch, the two `_failover_ladder` local-tier cases
  (`LOCAL_BIG`→sonnet escalation, `LOCAL_SMALL`→no claude rung), and three
  `dispatch()` paused-slot cases (falls to hosted rung / falls further to
  claude / stays byte-identical with failover off).
- ✅ `docs/reference/config-variables.md` §4, `docs/architecture/state-map.md`
  (LLM-independence section) — updated in the same commit; the latter
  also corrected a stale "FailoverProvider not built" note.
- No migration, no schema change, no new `kind`. All call sites
  (`llm_summarize.py`, `classify.py`, `paper_glossary.py`) are unaffected
  at the source level — they still request `Tier.LOCAL_SMALL`/
  `Tier.LOCAL_BIG`; only the transport resolution changes.
- Ops-side (not done here): `deploy/inventory` host_vars — gitignored/
  private, not present in this worktree — is the remaining step to
  actually flip the switch fleet-wide (see Open questions).

## Open questions / decisions log

**Resolved 2026-07-23 (discussion with Reto):**
- ~~Roster confirmation~~ — decided, see the roster table above (small
  skips Anthropic; medium/big keep Sonnet/Opus as failover-only escalation
  rungs, not defaults).
- ~~Cross-host fallback (borrow balthazar's spare capacity)~~ — explicitly
  rejected; OpenRouter is the shared-capacity answer, not cross-host
  routing (which `served_by`/`local_serving`'s host-scoped design doesn't
  support today anyway).
- ~~API key vaulting~~ — already done. `PRECIS_LLM_API_KEY` is present in
  the DB-backed vault (seeded 2026-07-14, matches the OpenRouter key hint)
  — no seeding step needed before flipping the switch.

**Resolved 2026-07-23 (settled during the build, per the doc's own
"can settle inline" invitation):**
- ~~Item 1's kill-switch, still wanted?~~ — no; not built. Superseded by
  item 3 (see "In scope" above).
- ~~Item 3's trigger condition~~ — busy (`paused`) only. "Unreachable" isn't
  a distinct state `local_serving.acquire()` produces — a genuinely dead
  endpoint surfaces as an ordinary post-dispatch transport error, which the
  ladder already handled before this change.
- ~~Default-on vs. opt-in for item 3~~ — opt-in, gated on the existing
  `PRECIS_LLM_FAILOVER` flag rather than a new one (one flag, not two; off
  by default matches that flag's own established rationale).
- ~~`LOCAL_BIG`'s claude-escalation model~~ — not actually an open
  question in the doc, but a real bug the build surfaced: fixed via
  `_LOCAL_ESCALATION_TIER` (`LOCAL_BIG`→`CLOUD_MID`/sonnet;
  `LOCAL_SMALL`→ no claude rung). See Motivation + item 3 above.

**Still open — blocking further action (code is built and dark; these are
now pure ops/rollout decisions, not implementation gaps):**
- **Scope of the live switch, right now.** `PRECIS_LLM_BACKEND` is one
  fleet-wide switch — flipping it to `openai` moves all three cloud tiers
  (small/mid/big) at once, not just one. Full coherent flip (all three
  `PRECIS_MODEL_*` set to their roster picks + `PRECIS_LLM_FAILOVER=1`)
  vs. a narrower change accepting that an unset tier's model id gets sent
  to OpenRouter, fails, and falls back to Claude (harmless with FAILOVER
  on, but a wasted round-trip every call) — awaiting Reto's call.
- **Sequencing.** The cloud-tier flip (config-only, already-shipped code)
  and the local-tier fallback (item 3, now also shipped code) can both
  flip today — the remaining gate is applying `deploy/inventory` host_vars
  fleet-wide (private/gitignored, not present in this worktree) and
  running the live-cluster verification in acceptance criteria #2/#3.
  Flip cloud+local together in one rollout, or cloud first with local
  behind its own check? Reto's call.

**Smaller — still open, not blocking:**
- Wire `kimi-k2.7-code` to an actual call site (e.g. a `coder`-agent model
  override) now, or leave it catalog-available for a later pass?
- Actually retire `q5` (`qwen3.6-27b-q5_k_m` + its draft-model pairing)
  from melchior's `llamacpp_models` — discussed as redundant once the
  OpenRouter fallback covers the same "dispatcher failover" role q5 was
  for, but not yet executed (config change in `deploy/inventory`).
- Any explicit watch/revert tripwire once medium-tier moves off Sonnet
  (e.g. a nursery-style alert on plan_tick spin-rate), or informal
  `view='tote'` monitoring for now?
- Balthazar's own local-small duty (enabling `precis_worker_classify`/
  `precis_worker_summarize_llm` there) — a separate, optional follow-up,
  independent of the OpenRouter work.
- **Per-tier backend override — reframed 2026-07-23, see `OPEN-ITEMS.md`
  §"`Backend` (`PRECIS_LLM_BACKEND`) — residual smell".** Not blocking
  today's plan (all three tiers are moving together), but the fix isn't
  "add a third axis for per-tier override" — it's that `Backend` shouldn't
  exist as its own axis at all: a resolved model id already implies its
  vendor/transport, so inferring transport from the model id (rather than
  a separate fleet-wide switch an operator must keep hand-synced with
  `PRECIS_MODEL_*`) gives per-tier control for free and removes a
  correctness foot-gun (`backend=openai` + a forgotten `PRECIS_MODEL_*`
  override POSTs a claude model id to OpenRouter). Raised, not designed —
  see the OPEN-ITEMS.md entry for the full reasoning.
