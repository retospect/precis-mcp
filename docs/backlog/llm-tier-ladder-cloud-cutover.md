---
status: idea
title: LLM tier ladder — SMALL to cloud, MEDIUM/BIG/FRONTIER onto sonnet/opus/fable
---

# LLM tier ladder — SMALL to cloud, capability tiers onto Claude models

Reto's ask (2026-08-15), verbatim: *"move the small tier to the cloud that works
for now. And lets make the next tier up sonnet, then opus, then for frontier put
fable, with the claude -p thing. we'll need to clean this up but I want to get
going."*

**This deliberately reverses the placement half of
[[small-llm-derived-drain-band]].** That spec's Slice 4 was to *drop* the SMALL
cloud rung so SMALL ran melchior-local-only, capped 6, never cloud — also Reto's
ask, in Aug 11's words (*"only running on melchior … up to 6 at a time … They
should not go elsewhere"*). Slice 4 step 3 was never applied (gated on operator
approval; the prod `app_settings` write was blocked by the permission
classifier), so prod still carries the cloud rung. **Do not apply that cutover.**
The intent has changed: throughput now beats placement discipline. Someone
re-reading the older spec in isolation will re-apply it and undo this — hence
this file.

## Motivation: the drain cannot keep up

`derived_drain` is losing ground. Over ~23 h the summarize backlog went
1,832,919 → 1,834,662 and classify 190,351 → 193,848 while only ~5,250 chunks
drained. Twelve jobs completed in 21 h, each "drained 500 chunk(s) (~438 ok,
~62 failed/deferred)", sequential, one at a time, all on melchior.

## Finding 1 — the SMALL cloud rung is configured but structurally unreachable

Prod `app_settings`:

```json
llm.chain.small = [{"placement":"local","model":"glm-4.7-flash","transport":"local"},
                   {"placement":"cloud","model":"z-ai/glm-4.7-flash","transport":"openai_compat"}]
```

Rung 1 is never walked. `router.py:2098-2160` — after `_local.acquire(serve_model)`
returns a saturated slot, the escape to the hosted rung is gated on
`transport in (Transport.OPENAI_TOOLS, Transport.OPENAI_COMPAT)`.
`Transport.LOCAL` is excluded by design; the inline comment reasons it "has no
hosted mode at all and would just re-hit the same saturated loopback wire."
Correct for a single-rung LOCAL chain, wrong when a real `openai_compat` rung
sits at index 1: dispatch returns `paused=True` immediately and the ladder is
never consulted.

This is **already documented** in [[small-llm-derived-drain-band]] ("Present-state
facts this builds on") as the mechanism that *delivers* never-spill. It was a
feature. Under the new intent it is the blocker.

`SMALL` with no chain override resolves the same way anyway —
`select_transport` (`router.py:837-838`) maps SMALL → `Transport.LOCAL` under
the ANTHROPIC backend.

### Why summarize is stuck and classify is not

`derived_drain._make_summarize_runner` (`derived_drain.py:114-133`) and
`_make_classify_runner` (`:136-173`) build identical
`DispatchClient(tier=Tier.SMALL, …)` wiring — no placement pin, no hardcoded
locality. The difference is that classify pins `PRECIS_CLASSIFY_MODEL` and
reaches a different rung. In the 24 h sample classify, bib_parse and the axis:*
family made 15,035 cloud `z-ai/glm-4.7-flash` calls during windows where
summarize made zero. Summarize is the only SMALL consumer with no model pin, so
it is the only one that lands on rung 0 `Transport.LOCAL` and dies there.

(Recorded because an earlier read of this attributed it to a "local-only
runner" in `derived_drain.py`. There is no such hardcode.)

## Finding 2 — the ~12% deferral rate is that gate, working as specified

Every job reports ~62 failed/deferred per 500. That is `DispatchError.paused`
exhausting `EMPTY_RETRY_ATTEMPTS` in `llm_summarize.py` and recording the chunk
for retry — what [[small-llm-derived-drain-band]] calls "clean backpressure, as
designed." Not a defect against the old intent. A pure loss against the new one.

Contributing mismatch, worth fixing either way: `derived_drain._DEFAULT_CONCURRENCY`
is 6 (`derived_drain.py:100`, commented "== the router's cap-6 local SMALL slot")
and `resource_slots` advertises `llm:glm-4.7-flash` at capacity 6, but melchior's
`llama-server` actually runs `--parallel 4`. Two of every six threads are
structurally guaranteed into backoff. The old spec's open question ("confirm
melchior's slot `max_parallel` is exactly 6") is now answered: it is not — and
the model has changed from `qwen3.5-9b-q4_k_m` to `glm-4.7-flash` since that
spec was written, so the advertised capacity was never re-derived.

## Finding 3 — measured and eliminated as bottlenecks

| Candidate | Measured | Verdict |
|---|---|---|
| Chunk-claim tiers | draft 82 ms · conv 17 ms · hot 0.09 ms · rest 4 ms ≈ 105 ms/batch | not it |
| `fetch_doc_card` | single indexed lookup per ref | not it |
| melchior→caspar network | 1.5 ms RTT | not it |
| Model availability | `llama-server` GLM-4.7-Flash-Q5_K_M up 2d11h, never swapped | not it |
| Model time | 1,199 s of a 3,063 s job — ~200 s wall at concurrency 6 | not it |

All four claim tiers get index-only scans despite `chunks` at 2.78 M rows /
7.3 GB, `chunk_summaries` 2.53 M, `chunk_claims` 2.42 M. The claim path is
healthy; the queries in [[small-drain-throughput-starvation]]'s "measure
melchior's actual rotation period first" are answered on the claim side.

## Finding 4 — UNEXPLAINED: each job idles 85–93% of its wall clock

Per-minute `llm_call_log` histogram inside two consecutive drain jobs:

```
job 203729   18:05:33 → 18:56:34   (51 min)
   18:14–18:49   ── 36 minutes, zero calls ──
   18:50–18:56   436 calls  @ ~72/min

job 203730   18:56:36 → 20:38:30   (101 min)
   18:57–20:30   ── 94 minutes, zero calls ──
                    (the 16 calls at 19:21 are the standing pass, not the drain)
   20:31–20:38   435 calls  @ ~62/min
```

Systematic across both jobs: all work compressed into a final ~7-minute burst
after a 36–94 minute window with no LLM calls, no DB load, and melchior's worker
at 2:36 CPU across 4 hours. Not slow — **stalled, then fast.**

Burst throughput ≈ 4,000 chunks/h against an observed average of 285 chunks/h.
The machinery is ~14× faster than what is being realised.

**Open. Two things would close it:**

1. Rule out a logging artifact — if `llm_call_log` rows are flushed in batches
   the burst is not real. Cheap check: during 203730's dead window melchior's
   *other* passes were making cloud calls; if those rows land spread out in real
   time while summarize's clump, batching is dead as a hypothesis.
2. `sample <worker-pid> 30` on melchior during a live drain (macOS built-in,
   read-only, no install). Names the blocking frame outright.

Candidate not yet excluded: the shared client wiring built up front in
`_make_summarize_runner`, or the first `client.complete()` blocking on something
with a long timeout. Related but distinct from the rotation starvation in
[[small-drain-throughput-starvation]] — that explains the gap *between* jobs,
this is dead time *inside* one.

## Finding 5 — `classify_drain` has never run, not once

`claim_executor_jobs` orders `COALESCE(r.prio, 5) ASC, r.ref_id ASC`. All 82
queued drains are prio 8. Summarize's ref_ids (203719+) always beat classify's
(up to 205351), and summarize is continuously re-minted, so band B is formally
unreachable. That is why classify's backlog grew 190,351 → 193,848 untouched.

This is the "Queue fairness" bullet of [[small-drain-throughput-starvation]],
now confirmed in prod rather than predicted. Its suggested fix (round-robin per
`params.pass`, or per-band prio staggering at mint) stands. Its sizing question
— "watch whether the classify batch drains on its own by 2026-08-16" — is
answered: it will not.

## The change

Four `app_settings` rows. Live config has a 15 s TTL — **no deploy, no restart,
no service touch.** Editor at `/factory`; gotcha from
[[medium-chain-tool-transport]]: `llm.chain.*` is edited live, re-read the row
before writing.

```json
llm.chain.small    [{"placement":"cloud","model":"z-ai/glm-4.7-flash","transport":"openai_compat"}]
llm.chain.medium   [{"placement":"cloud","model":"claude-sonnet-5","transport":"claude_p"},
                    {"placement":"cloud","model":"claude-sonnet-5","transport":"claude_agent"}]
llm.chain.big      [{"placement":"cloud","model":"claude-opus-5","transport":"claude_p"},
                    {"placement":"cloud","model":"claude-opus-5","transport":"claude_agent"}]
llm.chain.frontier [{"placement":"cloud","model":"claude-fable-5","transport":"claude_p"},
                    {"placement":"cloud","model":"claude-fable-5","transport":"claude_agent"}]
```

SMALL drops the local rung entirely, so rung 0 is `openai_compat` — no slot
acquisition, no `paused` path, no deferrals.

Model IDs verified current: `claude-sonnet-5`, `claude-opus-5`, `claude-fable-5`
are complete as written — no date suffixes. The codebase carries no retired IDs
either (`router.py:229-231` pins `claude-opus-4-8` / `claude-sonnet-5` /
`claude-haiku-4-5-20251001`; `fixer/tick.py:53-55` and `claude_p.py:61`
likewise), so nothing needs a model migration alongside this.

Note the compiled `_MODEL_BY_TIER` defaults are already a Claude ladder —
FRONTIER `claude-opus-4-8`, BIG `claude-sonnet-5`, MEDIUM
`claude-haiku-4-5-20251001`. This proposal shifts every tier up one rung.

Sonnet 5 is $2/$10 per MTok under introductory pricing **through 2026-08-31**,
$3/$15 after. Budget MEDIUM at the later number.

### Rung order is load-bearing: tool-less first, tools second

`resolve_chain` (`router.py:1556-1650`) filters tool-less rungs out of
tool-using calls, and **if the filter empties the chain it falls back to
`_default_chain`, which carries no model pin.** Only `claude_agent` and
`openai_tools` satisfy `Transport.carries_tools` (`router.py:193`); `claude_p`,
`local` and `openai_compat` do not. A `claude_p`-only chain therefore routes
every agentic call on that tier to the *default* model, silently — which is
exactly the failure [[medium-chain-tool-transport]] documents in prod today, and
which the `resolve_chain` docstring records as having run for days on
`llm.chain.medium` with planner ticks re-minting forever.

With the tool-less rung first: a tool-less call takes rung 0 (`claude_p`, cheap,
one-shot); a tool-using call has rung 0 filtered out and lands on `claude_agent`.
Correct in both directions, one chain.

Landing MEDIUM as above also resolves [[medium-chain-tool-transport]] — that
item can be closed or retargeted once this ships.

## Open questions

- **BIG: Opus-only, or local-first with Opus underneath?** Not yet decided.
  Today rung 0 is a *local* `deepseek/deepseek-v4-flash` on the DGX pair
  (castor/pollux) with cloud `z-ai/glm-5.2` beneath. The ladder above drops the
  local rung, so the pair stops carrying BIG entirely. Alternative keeps
  `{"placement":"local","model":"deepseek/deepseek-v4-flash","transport":"openai_tools"}`
  as rung 0 with Opus below, so owned hardware still earns and Opus is the
  fallback rather than the default. **Operator call (Reto).**
- **Should FRONTIER be Fable at all, or Opus 5?** Reto asked for Fable. The
  four findings below argue for landing `claude-opus-5` at FRONTIER first and
  revisiting Fable once they're answered: half the price ($5/$25 vs $10/$50 per
  MTok), no data-retention requirement, narrower classifiers. Fable's advantage
  is the hardest long-horizon *agentic* work — which is exactly the traffic
  `claude_p`'s tool-less, effort-less, 120-second one-shot shape cannot carry
  anyway. **Operator call (Reto).**
- **Fable requires 30-day data retention.** It is not available under zero data
  retention; an org whose retention configuration is below that gets
  `400 invalid_request_error` on **every** request, valid payload or not. Check
  the org's retention config before anyone debugs a dark FRONTIER tier.
- **Fable's classifiers target research biology and most cybersecurity
  content** — it is explicitly not intended for those domains, and benign
  adjacent work false-positives into `stop_reason: "refusal"` (HTTP 200, not an
  error). FRONTIER serves the whole corpus: 1.8 M chunks of scientific papers
  with `protein`, `material`, `patent` and `datasheet` kinds. The current
  catalysis quest is chemistry, not biology, so this is a corpus-wide risk
  rather than a quest-blocker — but it is a real one.
- **`call_claude_p` has no refusal path.** It parses the last JSON block from
  `claude -p` stdout (`utils/claude_p.py:124-149`), so a refusal — which returns
  successfully with no JSON — surfaces as a parse failure, not a recognizable
  refusal. Worth handling before FRONTIER lands on any model whose classifiers
  can decline.
- **Fable entitlement is unverified.** `call_claude_p` shells out to the
  `claude` CLI on the worker host with `--model`. Whether that CLI's auth
  actually has `claude-fable-5` is untested — one command on melchior settles it.
- **Per-host env defaults will break on Fable.** `PRECIS_CLAUDE_MAX_USD`
  defaults to **$0.10/call** — roughly 10k input tokens at Fable's rates — and
  `PRECIS_CLAUDE_TIMEOUT_S` to **120 s**, while Fable turns on hard tasks
  routinely run many minutes. Both need raising before the first call. Per-host
  env, not chain config.
- **Knob loss, and it bites hardest at FRONTIER.** `claude_p`/`claude_agent`
  reject `temperature`, `thinking` and `effort` (capability matrix,
  `router.py:526-532`). Anything currently tuning `effort` on `openai_compat`
  loses it silently — but the sharper problem is Fable, where thinking is always
  on (`thinking:{disabled}` and `budget_tokens` both 400) and
  `output_config.effort` is the **only** depth control. On `claude_p` that means
  no depth control at all, at $10/$50 per MTok. Opus 5 has the same shape at
  half the price.
- **Opus 5 is a separate rate-limit bucket** from the combined Opus 4.x pool
  that `claude-opus-4-8` draws on today. Moving BIG or FRONTIER to
  `claude-opus-5` neither frees headroom on the old bucket nor inherits it —
  check the tier's Opus 5 limits before shifting volume.
- **Spend.** This moves the whole fleet from OSS-cheap to Anthropic pricing at
  once. SMALL is the highest-volume tier by a wide margin (~6.7k calls/24 h per
  [[breaker-gate-resolved-cost]]), and that item notes
  `bands._TIER_BANDS[SMALL]=FREE` means `breaker.gate_tier` **never gates SMALL**
  — so a tripped cap pauses BIG/MEDIUM/FRONTIER while an all-cloud SMALL keeps
  spending. That item becomes materially more urgent under this change; it
  should probably land first or alongside.
- **Does SMALL-to-cloud alone explain the dead window?** Unknown. If the stall
  is slot starvation, moving off the local slot removes it and Findings 4 and 5
  collapse into one fix. If the burst persists, the stall is elsewhere. Land
  SMALL alone first and watch one drain job before touching the other three
  tiers — it is reversible in one row and it is the cheapest possible
  discriminator.

## Ship order

1. `llm.chain.small` → cloud-only. Watch one full `derived_drain` job: deferral
   rate should go to ~0 and the per-minute histogram should flatten. This is
   also the experiment for the last open question above.
2. Re-derive `_DEFAULT_CONCURRENCY` from the advertised slot capacity instead of
   the hardcoded 6, and fix `resource_slots` capacity to match `--parallel`.
   Independently correct regardless of placement.
3. MEDIUM / BIG / FRONTIER, once the BIG question is answered. Land FRONTIER on
   `claude-opus-5` unless the Fable questions above (retention, classifiers,
   entitlement, `claude_p` env limits) all come back clean; raise
   `PRECIS_CLAUDE_MAX_USD` / `PRECIS_CLAUDE_TIMEOUT_S` on the worker hosts in
   the same change either way.
4. Round-robin or prio-stagger the drain mint so `classify_drain` can be reached
   (Finding 5 / [[small-drain-throughput-starvation]]).

## Blast radius

- **Ops only for steps 1 and 3** — prod `app_settings`, four rows, live TTL 15 s.
  No code, no deploy.
- **Code for step 2** — `workers/job_types/derived_drain.py`, and whatever
  advertises `resource_slots` capacity on melchior.
- **Code for step 3 (if FRONTIER lands on a classifier-carrying model)** —
  `utils/claude_p.py` needs a refusal path; `PRECIS_CLAUDE_MAX_USD` /
  `PRECIS_CLAUDE_TIMEOUT_S` are per-host env in the deploy overlay, not chain
  config.
- **Code for step 4** — `workers/materialize.py` mint, or `claim_executor_jobs`
  ordering in `workers/executors/_common.py`.
- **Supersedes:** the placement half (Slice 4) of
  [[small-llm-derived-drain-band]]. That spec's Slices 1–3 (the `derived_drain`
  job type, the minter bands, classify's fair claim anchor) all stand unchanged.
