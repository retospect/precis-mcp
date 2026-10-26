---
status: draft
title: Local-first routing for cheap product LLM ops — castor first, paid model on escalation, shadow-measured before cutover
pillar: local-compute
prio: high
---

# Local-first routing for cheap product LLM ops

Reto, 2026-10-25: "Give castor real work." castor serves
`openai/gpt-oss-120b` on SGLang (32 running requests, 131k window, $0 per
call) and its GPU sits near 0 %. This item routes three cheap, high-volume
product LLM jobs onto it through the existing `llm.op.<source>` chains.
Paid models (Sonnet-class, haiku-class) stay as the escalation target for
the hard cases and as the fallback.

**Scope.** One shared mechanism, the *local-first op*: a local attempt on
castor, a caller-supplied acceptance check, and an escalation to the
op's paid path with a per-op daily budget. Named sets of jobs have three modes
(`off` / `shadow` / `on`), all off by default behind one master switch.
Three first consumers:

1. the gripe triage tick's noise and already-fixed screen
   (`workers/gripe_triage.py`);
2. the first pass of table numeric extraction, the `survey_extract`
   table-recipe call (gr506762, `extract/` + `workers/job_types/survey_extract.py`);
3. a new claim checker for the design-tree manifests
   (`src/precis/data/design-tree/`). Today no code verifies those claims:
   DESIGN verified each rule against its `source_url` by hand with Sonnet
   drafter agents (README there, "~87 % of claims confirmed").

Future consumer: the dream-into-gardener spec being written in parallel
(the dream agent folded into the graph gardener on castor) reuses this
mechanism for any step that wants a paid review on escalation. That spec
is not part of this one.

## Motivation / why

- Everything exists except escalation. Op chain rungs reach castor
  (`operations.py::resolve_op_chain`, `router.py::_served_local_rung`);
  `local_serving.acquire` holds slots with a bounded wait
  (`PRECIS_LOCAL_SLOT_WAIT_S`, 10 s) that returns `paused`; `router.Rung`
  carries `max_tokens`/`timeout_s`/`reasoning_effort`; `llm_call_log`
  records `placement` (ran) and `placement_routed` (routed).
- `FailoverProvider` has an `accept` seam, but `route()` never passes
  one. Also, `placement="local"` filters every cloud rung out
  (`_apply_placement`), so "castor, then Sonnet if castor's answer fails
  a check" cannot be written as a chain. Callers work around this alone:
  survey_extract falls back to its per-cell local path, chase returns `None`.
- The MEDIUM replay eval (`docs/reference/castor-medium-load-eval.md`,
  `scripts/llm_eval/medium_castor_eval.py`) found 86–100 % agreement with
  the cloud rung on verify, reground and locate, at 3–8 s versus 23–38 s
  per call. `negate` scored 12 %. So every candidate is shadow-measured
  before cutover.
- gpt-oss spends its reasoning tokens first, so a small `max_tokens` cap
  returns empty text or truncated JSON (`_empty_length_is_failure`,
  gr489474). The slice-2 branch (`worktree-tables-s2`, not landed) pins
  `max_tokens=1024` on `survey_extract:table_recipe`. A caller cap beats
  the rung's cap, so castor will truncate there. The reader call hit the
  same trap, which is why it is unpinned and its prod row carries 4096.

**Value, ranked.** (2) and (3) carry the volume: 1,406 pipe tables for
qu500066 alone, then corpus-wide runs; about 2,250 rules from 894
distinct source URLs, re-checked on every manifest change. (1) is small
(84 open gripes, intake peak ~520 a day, roughly a dollar a day of Sonnet
quota), but it goes first: it has a live paid path to shadow against.

## Decisions

**D1. One helper, call-site escalation, not a chain rung.**
`precis.utils.llm.local_first.run(req, *, op, accept, compare=None) ->
LocalFirstResult`. Under mode `on` it:
1. routes `replace(req, source=f"{op}:local", placement="local",
   max_tokens=None)`. The local arm never inherits the caller's
   `max_tokens`: the rung governs it (fixes the 1024 trap by
   construction).
2. calls `accept(result) -> None | reason`. The caller parses and
   validates there.
3. on a reason, routes the caller's original `req` (source `op`, today's
   paid routing, unchanged) with `features.local_first.escalated =
   <reason>`, provided the op's daily escalation budget has room.
An `AcceptFn` on `LlmRequest` was rejected because requests are
serialized into `llm_call_log`, and the budget and stats belong per op,
not per rung.

**D2. Source naming.** The paid arm keeps today's source (`op`), so mode
`off` is byte-identical to today. The local arm is a new registered op
`<op>:local`, whose prod `llm.op.<op>:local` row is the castor rung. With
no row, the local arm refuses up front (`placement_refusal`). The helper
counts that as `escalated: no_local_rung` and logs it once per process.

**D3. Escalation triggers** (the reason strings, stable for stats):
`parse` (accept rejected the shape), `truncated` (stop reason
length/max_tokens), `timeout`, `refusal` (reason_class `content`),
`low_confidence` (caller field below its bar), `validator` (a
deterministic check after the parse dropped the answer), `saturated`
(slot wait expired), `transport`, `no_local_rung`. Per-op policy decides
which reasons escalate and which park (D5).
`LocalFirstOp.park` contains `saturated`, `timeout`, `transport` and
`no_local_rung` for table recipe and dtree; triage's set is empty.
A parked `LocalFirstResult` has `deferred=<reason>`, `result.paused=True`,
the original attempted answer in `local` when present, and no paid dispatch
or escalation-budget read. An open local circuit follows the same policy.

**D4. Master and named job sets.** Reto's ruling: "I turn on sets of jobs."
Switches are `settings.py::REGISTRY` keys, which resolve
DB row → env → compiled default and propagates within the ~60 s TTL.
- Master key `llm.local_first.enabled` (bool, env
  `PRECIS_LOCAL_FIRST`, compiled `False`).
- Per set `llm.local_first.set.<set>.mode` (`off|shadow|on`, env
  `PRECIS_LOCAL_FIRST_SET_<SET>`, compiled `off`). `LOCAL_FIRST_SETS`
  lists each set's operations. An op runs local-first only while the master
  is true and at least one containing set is `on`; an unrelated enabled set
  has no effect. For overlapping sets, `on` wins over `shadow`, which wins
  over `off`. S1 leaves shadow on the original paid path; S2 adds pairing.
- Per op `llm.local_first.<name>.escalate_usd_day` (float).
There are no per-op enable/mode flags. Budgets and accounting remain per op.

| Setting | Members / purpose | Compiled default |
|---|---|---|
| `llm.local_first.enabled` | Master | `false` |
| `llm.local_first.set.triage.mode` | `gripe_triage:tick` | `off` |
| `llm.local_first.set.tables.mode` | `survey_extract:table_recipe` | `off` |
| `llm.local_first.set.claims.mode` | `dtree:claim_check` | `off` |
| `llm.local_first.triage.escalate_usd_day` | Triage budget | `3.0` |
| `llm.local_first.table_recipe.escalate_usd_day` | Table recipe budget | `10.0` |
| `llm.local_first.dtree.escalate_usd_day` | Claim check budget | `10.0` |

Reto flips the master and named sets. Builders and agents do not write these rows.
Flipping the master or every containing set to `off` reverts on the next
call after the settings TTL and leaves no state to unwind,
because the local arm writes only through the caller's existing write
paths.

**D5. Per-candidate routing.**

| | local arm (castor) | escalate to paid on | on saturation / castor down | paid model | escalation budget |
|---|---|---|---|---|---|
| triage screen | one call per gripe, same verdict schema as the Sonnet batch, `reasoning_effort` low, rung `max_tokens` 4096, `timeout_s` 90; local phase capped at 150 s of the tick's 240 s by the ambient deadline (`utils/deadline.py`) | any non-screen verdict; parse / truncated / timeout / refusal; `confidence` ≠ `high`; reopened or owner-filed gripe; any `fix:reto`; dispute verdicts | the gripe joins the Sonnet batch: today's path, no worse than the status quo | today's `Tier.BIG` (Sonnet-class, `PRECIS_GRIPE_TRIAGE_MODEL`) | `$3/day`, then remaining gripes stay `open` for the next day |
| table recipe | one call per parsed, non-ragged table chunk, `reasoning_effort` low, rung `max_tokens` 4096, `timeout_s` 300 | parse / truncated / refusal / `validator` (`expand_recipe` drops every output series, or more than half of them for `unknown_measurand` / `unit_dimension_mismatch`) | park on `wake_when`, the existing survey_extract pacing; never paid | `survey_extract:table_recipe`'s `Tier.MEDIUM` (haiku-class) | `$10/day`; over budget, the per-cell local reader path (the branch's fallback) |
| dtree claim check | one call per rule: claim text, value, unit, and the top-3 source windows; `reasoning_effort` low, rung `max_tokens` 3000, `timeout_s` 180 | verdict `contradicted` or `not_found` while the source was fetched; parse / truncated / refusal; `confidence` ≠ `high` | retry with backoff; never paid | `Tier.BIG` (Sonnet-class), matching the DESIGN drafters | `$10/day`; over budget, the row reports `needs_paid_check` |

**What castor alone may decide in triage.** Castor alone may write only
the two verdicts whose deterministic gates already bind the Sonnet path:
- `noise`, only where `_noise_flagged` is true;
- `fixed`, only where `_match_evidence` finds a cited commit proven
  deployed.
Everything else goes to the Sonnet batch.

`fixed` depends on gr517975: prod has no deployed-commit evidence, so no
`fixed` close fires on prod today, castor's or Sonnet's. Until gr517975
ships, the screen acts on `noise` only.

**D6. Concurrency and back-pressure.**
- Per-process in-flight caps held by the helper: triage 6, dtree 6; the
  table recipe keeps one chunk in flight per job
  (`claim.max_running.survey_extract`). Worst case 6 + 6 + 2 per host,
  beside the gardener floor (2) and summarise/classify (~8 average),
  stays under the 32 slots (gr485635: do not raise).
- When `llm-lane-scheduler.md` S1 ships, the three `:local` sources map
  to one new class `local_first` (priority 3, min 0, max 12, not a
  filler). Until then the caps above are the only guard.
- A saturated slot (`paused` after the 10 s wait) follows the D5 column.
- Five consecutive `timeout`/`transport` failures in one process make the
  helper skip the local arm for 10 minutes (logged once). The paid
  arm's costs still count against the D4 budget.
- An acceptance test proves that a saturated `placement="local"` call
  never lands on the hosted-OSS retry: `placement_routed` = `placement`
  = local, or the call is paused.

**D7. Quality gate: shadow before `on`.** Mode `shadow` runs the
paid arm as today and uses its answer. It also runs the local arm under
`<op>:local` with `features.local_first = {shadow: true, pair:
<paid request_hash>, agree, diff}`, where `compare(local, paid)` computes
`agree` and `diff`. Castor is free, so the shadow rate is 100 %.

Ops with no live paid path (table recipe as landed is local-only; dtree
has no code path at all) shadow offline. They run through
`scripts/llm_eval/local_first_shadow.py --op <name>`, which generalizes
`medium_castor_eval.py`: read-only on prod, a `--cap-usd` stop, raw output
gitignored under `gold_set/local/`.

Cutover bars (Wilson 95 % bounds, as `extract/accept.py` uses):

| op | sample | bar to flip `on` |
|---|---|---|
| triage stage 1 | ≥ 150 live-shadowed gripes, of which ≥ 40 castor screens pass the gates | screen agrees with Sonnet's verdict on ≥ 95 % of screens (upper bound on disagreement ≤ 10 %); answer rate ≥ 95 %; p95 per gripe ≤ 30 s |
| table recipe | 100 table chunks from qu500066's papers, stratified by column count; arm A castor at 4096, arm B castor at 1024 (pins the lesson), arm C cloud MEDIUM | arm A parse-and-validate rate ≥ 90 %; on ≥ 80 % of tables every output series has the same measurand, unit and orientation as arm C; an Opus blind judge on ≤ 20 disagreements finds castor wrong in ≤ half; then the first live castor batch passes `measure_accept`'s Wilson rule (5 % bar) |
| dtree | 150 rules stratified by file × `confidence`, plus 40 mutated controls (value ×2 or ÷2, or a swapped unit, seeded) | verdict agreement with the Sonnet arm ≥ 85 %; castor flags ≥ 36/40 controls `contradicted`/`not_found` and calls ≤ 2/40 `supported`; Opus judge on ≤ 20 disagreements |

Offline spend per op: under $5 for the paid arm plus the judge. The
script stops at `--cap-usd 15`. Below the bar, the op stays in `shadow`
or `off`, and the numbers go into this file's decisions log.

**D8. Observability.**
- Every helper call stamps `llm_call_log.features.local_first =
  {mode, arm, escalated, shadow, pair, agree}`.
- `get(kind='llm', id='op:<source>', view='stats')` gains a local-first
  block: local attempts, accepted, escalated, the escalation rate
  (escalated helper calls ÷ all helper calls) by reason; shadow agreement
  with a Wilson bound and the local answer rate; `spent_usd` (escalated rows' `cost_usd`);
  `saved_usd` (accepted local calls' tokens at the paid model's catalog
  price; counts reasoning tokens, so it overstates, and the view says so).
- `/placement` already shows the local share. An escalation rate above
  40 % over 24 h on an `on` op raises one precis alert per UTC day. The alert is
  the signal for Reto to flip the op back. The helper never writes a
  setting itself.

## Slices

**S1: the helper, settings keys and logging** (`utils/llm/local_first.py`,
`settings.py`, `operations.py`). `run()` per D1–D4 and D6 (in-flight cap,
10-minute skip, budget = sum of escalated `cost_usd` per op per UTC day
from `llm_call_log`); the registry keys; a `LOCAL_FIRST_OPS` table (name,
op, paid source, cap, default budget) that registers the `:local` ops.
Acceptance (stubbed router, beside `tests/test_llm_operations*.py`):
- `off` makes one route call whose serialized request equals the caller's;
- `on` plus an accepted result makes one call on `<op>:local` with
  `max_tokens=None`, even when the caller pinned 1024;
- each D3 reason not in the op's park set makes a second call on `op` with
  `features.local_first.escalated=<reason>`;
- budget exhausted means no paid call and `escalated: budget`;
- master false makes every set's `on` act as `off`; an enabled set activates
  only its member ops;
- a saturated local call never lands as `placement='cloud'`;
- five timeouts trigger the skip, and an advanced clock clears it.
- table recipe and dtree park without paid calls when their cap is saturated
  or castor is down, including while the local circuit is open.

**S2: shadow mode, the stats block and the offline harness.** The
`compare` hook and shadow stamping, the `op:<source>` stats block and
board line (D8), and `scripts/llm_eval/local_first_shadow.py` with a
per-op adapter (sample, arms, compare, judge prompt).
Acceptance: a shadow call returns the paid answer and writes two
`llm_call_log` rows linked by `pair`; the stats view on a dev DB fixture
reports rate, reasons, agreement bound and saved/spent; harness
`--dry-run` prints the sample plan and cost estimate without dispatching.

**S3: triage screen** (`workers/gripe_triage.py`; blocked by S1, S2). A
per-gripe castor pass before `_route_call` (same prompt shape, one gripe
block with its own nonce, `parse_verdicts` over a claimed set of one).
Screens that pass the D5 gates write through `_write_verdict`; the rest,
plus every escalation, form today's Sonnet batch. In shadow Sonnet
triages all, and `compare` records per-field agreement (verdict, fix
type, size, severity), so stage 2 data comes for free.
Acceptance (`tests/test_gripe_triage*.py`, stubbed router):
- `noise` on a `_noise_flagged` gripe writes `wontfix` with zero Sonnet
  calls for it; `noise` on an unflagged gripe goes to the batch;
- `fixed` without deployed evidence goes to the batch;
- a castor timeout sends the gripe to the batch, lease kept;
- the local phase stops at 150 s and the rest go to the batch;
- mode `off` is identical to today (existing tests unchanged).

**S4: table recipe local-first** (blocked by table-numeric-extraction
slice 2 landing, and by S1). Drop the branch's `max_tokens=1024` pin (a
bug fix regardless of mode; the `llm.op` row carries 4096); route the
recipe through `local_first.run` with the D5 `validator` accept
(`parse_reply` plus `expand_recipe` drop counts); keep the per-cell
fallback over budget; `summary_meta.tables` gains `escalated` and
`escalated_by_reason`.
S1 keeps the per-op lock through paid dispatch; S4 revisits reserve-then-reconcile for dtree escalation throughput.
Acceptance (`tests/test_survey_extract_job.py` additions):
- a truncated castor recipe escalates once to MEDIUM;
- a validator rejection (all series `unknown_measurand`) escalates;
- with the budget exhausted the job takes the per-cell path;
- `tables.recipe + fallback + no_results + ragged + escalated` equals
  the table chunks read;
- the router stub sees no `max_tokens` on the local arm.

**S5: dtree claim checker** (new; blocked by S1). `precis conventions
check-claims --manifest design-tree [--file F] [--limit N] --out
report.jsonl`: read each distinct `source_url` once through the web
kind's fetch path (`handlers/web.py`: `polite_safe_get`, PDF → text,
cached); pick the top-3 windows per rule by its numbers and terms; run op
`dtree:claim_check` through `local_first.run`; report one row per rule
(`supported | partial | contradicted | not_found | source_unreachable |
needs_paid_check`, quoted window). Writes nothing to manifests or refs.
Acceptance (fixture source text, stubbed router): a ×2 mutated value →
`contradicted`; an unreachable URL → `source_unreachable` with no LLM
call; a `contradicted` castor verdict escalates; every rule appears
exactly once; `--limit 5` makes ≤ 5 LLM calls.

**S6: castor class registration** (blocked by `llm-lane-scheduler.md`
S1). Add the `local_first` class row and the three `:local` sources to
the `source → class` map.
Acceptance: a 12-wide local-first burst during a gardener call keeps the
gardener at or above its floor on `/lanes`. The burst's thirteenth
request waits.

**Operations (not a builder slice), each step after its numbers are
recorded here:** write the three `llm.op.<op>:local` rows (rung
`{"placement":"local","model":"openai/gpt-oss-120b","transport":"local",
"reasoning_effort":"low","max_tokens":…,"timeout_s":…}` per D5); run the
offline shadows; Reto flips the master, then each named set `shadow` → `on`
after every member op passes its quality bar.

## In scope

S1–S6 above, the shadow runs, and their recorded numbers.

## Explicitly NOT in scope

- The castor coding-agent lane, `scripts/claude-local`, the shim and
  fleet launch ([castor-agent-lane](castor-agent-lane.md)).
  - Overlap: the same endpoint, slots, `llm_call_log`, lane classes, and
    the `reasoning_effort`/`max_tokens` lessons.
  - Difference: that lane is dev-side (scripts, builders), and its batch
    lane deliberately has no cloud rung. This item is product ops that
    already have a paid baseline, so escalation on a quality signal is
    the point. It still never escalates a batch op on saturation.
  - Its slice 3 (`scripts/castor/triage-gripes`) duplicates S3's subject
    from outside the tick; see the open questions.
- The paper→findings reader ([castor-finding-reader](castor-finding-reader.md)).
  It is local-only with an Opus spot-check gate. It may adopt the helper
  later and is not changed here.
- Host memory and OOM alerting on castor
  ([castor-resource-observability](castor-resource-observability.md)).
  This item's load is bounded by D6; that item watches the host.
- The dream-into-gardener fold (parallel spec): a future consumer only.
- Routing whole tiers (`llm.chain.<tier>`) to castor
  ([local-rungs-small-medium](local-rungs-small-medium.md)), raising
  32/0.80, a second model, or SGLang priority scheduling.
- Writing dtree verdicts back into the manifests. That is a builder PR
  from the report, gated by the human red-list review.

## Acceptance criteria

- With every key at its compiled default, all three call sites behave
  exactly as before: existing tests unchanged, and the serialized
  request is byte-equal in mode `off`.
- Each op's shadow numbers and verdict against the D7 bar are recorded
  in this file before a containing set is flipped `on`.
- After a flip, `op:<source>` stats show the escalation rate by reason,
  shadow agreement, and saved/spent USD. Escalation spend per op stays
  under its daily budget (checked in `llm_call_log`).
- `/placement` shows castor's share rising for the three sources. The
  gardener's p95 latency in `llm_call_log` stays within 2× its idle
  value during a local-first burst.
- No paid call is ever made by a `:local` source, and no batch op
  (table recipe, dtree) escalates on saturation.

## Target + blast radius

`src/precis/utils/llm/local_first.py` (new), `utils/llm/operations.py`
(three `:local` ops, `dtree:claim_check`), `settings.py` (keys),
`handlers/llm.py` (stats block), `workers/gripe_triage.py`,
`workers/job_types/survey_extract.py` and `extract/prompt.py` (after the
slice-2 merge), `cli/conventions.py` plus a new checker module beside
`design_tree.py`, `scripts/llm_eval/local_first_shadow.py`, and
`llm-lane-scheduler.md`'s class config (S6). No migration:
`features` is jsonb. No handler contract change. Prod writes are the
`app_settings` op rows and the flips (Reto).

## Open questions / decisions log

### Builder picks (S2)

- Live shadow requires `compare` and an existing paid baseline (triage).
  Without either, preserve the caller's original path; table recipe and
  dtree use the offline harness, never an implicit new paid baseline.
- Paid shadow runs first and remains the returned answer even if the local
  call, acceptance callback or comparator fails. Caps and the local circuit
  still apply; a skipped local call records a deferral, not agreement.
  No shadow call consumes the escalation budget. A full paid request blob
  supplies the pair hash; a unique invocation token isolates post-dispatch
  annotations from concurrent identical prompts, including lite local rows.
- Savings are an estimate using the latest observed paid model in the window,
  falling back to the configured cloud rung's catalog price. Missing prices
  or token telemetry are reported; reasoning-token overstatement is explicit.
- **GRIPE_FIX 2026-10-26 — D8 alert:** use the existing in-process
  `precis.alerts.raise_alert` writer, registered as
  `local-first:escalation/{op}`. `scripts/board` is absent from the production
  image and is no longer the delivery path. Claim the UTC day in an escalated
  log row under a DB lock before raising; a failed write consumes the claim
  (at most once), and missing accounting emits no alert.
- **GRIPE_FIX 2026-10-26 — escalation denominator:** count all recorded
  helper calls for the op, deduplicating both arms by invocation token.
  Circuit-skip and saturation escalations count even without a local row;
  a fully escalated window reports 100% and can alert. On-mode alerts use
  the same denominator within their trailing 24h window.
- **GRIPE_FIX 2026-10-26 — shadow quality:** keep `agree` null when either
  arm fails. Report accepted local answers divided by all shadow helper
  calls next to agreement over comparable pairs, so failures and skipped
  local arms remain visible in the answer rate.
- The offline harness defaults to planning only. Execution needs explicit
  `--run` and supplied JSONL samples with source evidence; no census file or
  source-fetch pipeline is assumed. Table execution refuses until the shared
  recipe validator from table extraction slice 2 is available. Missing or
  invalid paid cost telemetry stops further paid dispatch.

### Remaining rulings

- **[open, Reto] Flip authority and order.** Proposed: the coordinator
  writes the `:local` op rows (harmless while the master is off); Reto
  flips the master and each named set's mode.
- **[open, Reto] Agent-lane slice 3.** Retire
  `scripts/castor/triage-gripes` in favour of S3's in-tick screen, or
  retarget it to per-file review? Two triagers over the same 84 gripes
  is double work.
- **[open, Reto] Triage stage 2.** After stage 1 numbers, should castor
  write the full verdict (fix type, cluster, size, severity) on
  `confidence: high` rows, with Sonnet only for the rest? Proposed bar:
  fix-type agreement ≥ 80 %, severity within one band ≥ 85 %, and a high
  share ≥ 50 %. S3's shadow already collects these fields.
- **[open, Reto] Table-numeric slice 4 lane.** That spec recommends a
  cloud haiku-class run (~$10). With castor first and a $10/day
  escalation cap, the castor lane (option b) costs at most the same. Run
  qu500066 on castor after S4's shadow passes?
- **[open, Reto] Budgets.** `$3 / $10 / $10` a day for triage, table
  recipe and dtree. All are under the $25 threshold, and the budget
  breaker still applies on top.
- **[open, non-blocking]** dtree `reasoning_effort`: low first; a medium
  arm on the same 150 rules if low misses the controls bar.
- **[decided]** Escalation lives in the call-site helper, not on
  `LlmRequest` (D1); saturation never buys paid calls for batch ops (D5).
