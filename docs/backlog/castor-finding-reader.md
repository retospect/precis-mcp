---
status: draft
title: Castor paper→findings reader — a background job that reads one full-text paper chunk by chunk and writes cited, deduped claim hubs for the quest or todo that asked
pillar: memory-graph
---

# Castor paper→findings reader

Reto 2026-10-24 (chat, relayed): "Castor paper→findings reader: should I
have it specced? << yes". qu509651 logbook 2026-10-23 23:36: speccing it
was approved "so qualitative answers (AMOC placement, fisheries, salt on
land, governance) get findings, not only survey_extract measures."
Reto rules on this spec before anyone builds it.

## Motivation / why

Castor has exactly one paper reader, `survey_extract`
(`workers/job_types/survey_extract.py`). It writes numbers: `measures`
rows anchored to chunks, under a measurand taxon. Nothing in the
background turns a paper's qualitative content into findings: siting
rules, ecosystem effects, governance, mechanisms. The climate
orchestrator's cycle-0 count (qu509651, 2026-10-23 23:27Z) was "Papers
read: 0 in every strand. Findings filed: 0", with 62 full-text papers
linked to strands. Its purpose line still reads "0/62 full-text read".

Findings exist today only when a frontier session writes them by hand.
kin-evidence read pa508953 in full and filed fi509632..fi509645: one
sentence per claim, cited to `pa508953 pc<chunk>`, `derived-from` the
paper and the chunk, `serves` qu500066, trust `generated/hunch`. That is
the target output, at local-model cost, without a session in the loop.

Every piece the reader needs already exists. It composes them rather
than forking any of them:

| need | existing seam |
|---|---|
| chunk iteration, slicing, park/resume on castor | `survey_extract.py::_survey_dispatch`, `::_chunks`, `::_skippable` |
| claim extraction from a chunk | `taproot/canon.py::extract_claim` (atoms, composite, `not_claims`, `claim_type`, `scope`) |
| hearsay / proxy gate | `canon.py::is_proxy_deferral`; `taproot/grounding.py::has_grounding_prose` |
| "same claim?" | `canon.py::block` → `dedup_judge` → `place` (`merge_confirm` below 0.85) |
| support check of claim vs chunk | `workers/_chase_llm.py::_verify_support_with_caveats` (`chase:verify`, castor-routed by Reto 2026-10-22) |
| hub write, edge stamp | `taproot/hub.py::mint_hub` / `attach_evidence` / `apply_placement`; `MintCap` |
| sampled acceptance with a Wilson bar | `extract/accept.py`, `survey_extract.py::_accept_dispatch` |
| background claim order | `executors/_common.py::_claim_order_sql` (origin rank human 0 … background 3) |
| machine-write provenance | `store/_links_ops.py::MACHINE_EDGE_META_KEYS` (`model`, `prompt_version`, `run_id`, `why`) |

Kin, kept separate: `quest/claims.py::extract_claims` (in-memory, weave
only); `workers/inbound_ground.py` (attaches a new paper to *existing*
hubs and never mints); `local-mesh-upkeep.md` §1 "finding extraction" (an
eval task set, not a writer; S1 below reuses its scorer).

## The job

`finding_read` is a coordinator-executor resumable job, the same shape as
`survey_extract`. One job reads one paper for one serving target.

```
put(kind='job', job_type='finding_read',
    params={'paper': 'pa…', 'serves': 'td…'|'qu…', 'dry_run': true})
```

Per eligible chunk, in `ord` order, sliced and parked like
`survey_extract` (`chunks_per_slice` 8):

1. **Eligible?** The chunk must pass `_skippable` (paragraph or table; no
   bibliography or admin sections; at least 60 chars), `has_grounding_prose`,
   and `not is_proxy_deferral`. Chunks tagged `ROLE3:own` go first when the
   paper has them (`quest/claims.py::own_chunks`); the rest are still read.
2. **Extract.** A new op `finding_read` reads the chunk with ±1
   neighbour for context. The prompt carries the serving question (the
   todo/quest title and the first 600 chars of its body), the paper
   identity block, and the chunk's live measures (the next section).
   Output per claim: `sentence`, `scope` (the canon keys `material` /
   `method` / `quantity` / `regime`), `claim_type` (`canon.CLAIM_TYPES`),
   `caveats[]`, a verbatim `quote` from the chunk, and `bears_on`
   (one line on how the claim answers the question). A chunk with nothing
   on the question returns NO-CLAIM. The output contract is
   `ClaimExtraction` plus `quote`/`caveats`/`bears_on`, so the parse and
   coerce rules (`_coerce_extraction`) are reused, not rewritten.
3. **Check in code**, with no model:
   - `quote` must be verbatim in the chunk, after whitespace and
     hyphenation normalisation. This is the `reground` post-validate rule.
   - Every number and unit in `sentence` must appear in the chunk or its
     ±1 neighbours.
   - Every scenario or model token in `sentence` (SSP/RCP labels, a named
     GCM/ESM, a year range) must appear in the same window.
   - A failure drops the claim with the reason `quote-miss`,
     `number-miss` or `scenario-miss`.
4. **Verify** with `_verify_support_with_caveats` (claim, chunk, neighbours,
   section path). Keep only `supports='yes'` with `same_setup`. A
   `partial` verdict narrows instead: the verifier's caveats are appended,
   the claim is re-verified once, and dropped (`partial`) if it still
   fails. `contradicts` → drop (`self-contradict`; the paper does not say
   what the model wrote).
5. **Dedupe** (below), then **write** through the hub door.

Writes go through `taproot/hub.py`, never chase-mode `put(cited_in=…)`.
A claim hub is off the chase lifecycle. kin-evidence's chase-mode
findings on paper-own chunks went `STATUS:dead_chain
(no_resolvable_cite)` (fi509632, fi509636, fi509640), because chase
looks for an upstream source a primary paper does not have.

## Dedupe

**"Same claim" is decided by `canon.place`, the rule every mint door
already uses.** `block` takes the 10 nearest hubs by ANN; `dedup_judge`
returns same / different / contradicts, biased toward different; a `same`
at confidence ≥ `MERGE_CONFIDENCE_THRESHOLD` (0.85) attaches. Below that,
`merge_confirm` decides, and an unconfirmed `same` is `needs_review`.
Over-merge is the dangerous direction; `eval_canon` gates it to zero.

| placement | reader action |
|---|---|
| `attach` | **merge as supporter**: `attach_evidence(role='corroborates', source_handle=pc…)` with the full verified stamp, so the edge is born certified. Write the `serves` link from the existing hub to the target. Nothing new is minted. |
| `new` | `mint_hub`, then the same certified edge and `serves`. |
| `new_contradicts` | mint plus a `disputes` link (existing behaviour); listed in the run summary. |
| `needs_review` | **hold, don't mint** (the automated-caller rule in the `taproot` docstring). The candidate and its hub go in the run record; at most one review todo per run lists all held pairs. |

Within one paper, claims minted earlier in the run are hubs by the time
later chunks run `block`, so a claim repeated across chunks converges.
S1 verifies that `claim_embeddings` is written in the mint transaction;
if it is not, the run keeps an in-run sentence index and checks it
before `block`. `MintCap` (3 new hubs per chunk per run) applies
unchanged.

**Against survey_extract measures.** The rule is deterministic, keyed
on the chunk. The prompt lists the chunk's live measures (`measures` rows
whose `quantifies` link has `src_chunk_id` = this chunk) as "already
recorded". A `measurement`-type candidate whose numbers all match a live
measure on the same chunk (same value after number normalisation, unit
compatible) is dropped as `dup-measure`. Other claim types (`mechanism`,
`capability`, comparative or conditional statements) are kept even when
they contain the number, because they say something the measure row
cannot. A kept claim that reuses a measure's number records the
`mx<id>` in `refs.meta.measure_ids`. No new relation is added in v1
(Q4).

## Outputs and provenance

A reader finding is a claim hub like any other: `refs.kind='finding'`,
tagged `TAPROOT:claim` + `STATUS:canonical`; the claim sentence in
`title`; claim plus setup prose in the `finding_body` chunk.

| field | value |
|---|---|
| sentence | one claim; it carries the magnitude with units when the paper gives one, and the scenario or model when the claim depends on it |
| `meta.scope` | the canon keys, from extraction; conditions live here and in the sentence |
| `meta.caveats` | the extractor's caveats plus the verifier's |
| `meta.claim_type` | from extraction |
| `meta.measure_ids` | `mx<id>`s reused (above), optional |
| writer annotation | `{model, prompt_version, run_id: <job ref_id>, why: bears_on}` |
| trust | `generated` / `hunch` (migration 0214 default). Never auto-raised; only `edit(mode='trust')` with a reason raises it. |
| evidence edge | `paper --corroborates--> hub`, `src_chunk_id` = the chunk, verified stamp (`support`, `support_reason`, `verified_by='finding_read:<model>'`, `verified_at`, `verified_claim_sha`) |
| `serves` | hub → the queuing todo/quest, written when the batch passes the gate (below) |

**Run record** (the job row, `summary_meta`):

- `model`, `prompt_version` (a `PROMPT_VERSION` constant, the
  `graph/garden/proposer.py` idiom), and `paper_text_sha`, a hash over the
  ordered body-chunk texts;
- `chunks_read` (ids), `chunks_skipped` (`{id: reason}`);
- counts per outcome: minted, attached, held, and dropped by reason;
- LLM calls and tokens, read back from `llm_call_log` by `ref_id` = the
  paper and the time window. That table has no job column.

**Human correction or retraction:**

- body: `edit(kind='finding')`, the body door from gr509620 (built in
  `g7-finding-edit`, not landed);
- sentence: `edit(title=…)`, which runs `refine_claim_sentence` and
  withholds the old verdicts on sha mismatch;
- scope: `edit(meta={'scope'})`;
- detach one supporter edge;
- retract: soft `delete`, or `STATUS:refuted`.

Each human correction to a reader finding counts as a sampled critical
defect in the gate's running tally, so a run of human corrections
tightens the gate the same way a failed spot-check does.

## Quality gate

A **batch** is the findings one reader request produced: one paper for a
single-paper request, one parent for a fan-out (S3), capped at 50 findings
(a larger parent closes a batch every 50).

**Spot-check:** a child job `finding_read_accept`, the `measure_accept`
pattern. Opus reviews a sample from the batch against the cited chunk and
its ±1 neighbours. The sample is drawn by `extract/accept.py`'s
stratified sampler, stratified by paper and `claim_type`, seeded by the
batch's job id so a re-check draws the same rows. Attaches are sampled
like mints.

| gate state | sample per batch | enter |
|---|---|---|
| TIGHT (start state, and after any fail) | min(20, batch) | first run of a new `prompt_version` or model; any failed batch |
| NORMAL | min(10, batch) | 3 consecutive passes, and a Wilson 95% upper bound on the critical rate ≤ 15% over ≥ 60 cumulative samples |
| RELAXED | 10 from every 5th batch | ≥ 200 cumulative samples, upper bound ≤ 10% |

The gate state lives per `(prompt_version, model)` in `app_settings`
`finding_read.gate.<prompt_version>.<model>`. A new prompt or model
starts in TIGHT.

**Defect classes**, each judged per sampled finding:

| class | severity |
|---|---|
| `unsupported`: the chunk does not say it | critical |
| `hearsay`: the paper's citation of another work, written as the paper's own result | critical |
| `scope-stretch`: drops a condition, generalises past the setup | critical |
| `units-scenario`: wrong unit, sign, scenario, model or region | critical |
| `vacuous`: true but says nothing a reader can use | minor |
| `missed-dupe`: an existing hub states it | minor |
| `off-question`: doesn't bear on the serving question | minor |
| `garbled-source`: the chunk is OCR or extraction noise | routed to ingest, not counted |

**Adequate:** ≤ 1 critical defect in the batch sample (≤ 5% of 20) and
≤ 15% minor defects. A pass releases the batch's `serves` links. A fail
**reverts** the batch: its new hubs are soft-deleted, its attach edges
removed, the gate goes TIGHT, and the candidates stay in the run record
for a re-run. 3 consecutive fails on one `(prompt_version, model)` pause
the reader for that pair, and only Reto un-pauses it.

**Gripes:** one gripe per defect class per batch that hit the class at
least twice, deduped against an open gripe tagged
`finding-reader:<class>` (that gripe gets a comment instead). The
`garbled-source` gripe is tagged `thread:ingest-and-fetch`.

## Priority, cost, concurrency

**Local only.** Every reader call (`finding_read`, `taproot:extract*`,
`taproot:dedup`, `taproot:merge-confirm`, `chase:verify`) runs with
`placement='local'`. When no local rung survives, the call refuses; it
never falls back to cloud (`router.py::_apply_placement`). Today the
canon calls and the verifier set no placement and route by their op
rows; `taproot:dedup` and `taproot:merge-confirm` default to cloud. S1
threads a placement override through a contextvar (canon already uses
contextvars) so the reader forces local without rerouting every other
caller of those ops.

**Before local dedup is trusted,** the live `eval_canon` fixture must
score zero over-merges with `dedup_judge` / `merge_confirm` on castor's
gpt-oss-120b. If it does not, the reader routes `needs_review` for every
`same` and mints nothing new, and that is reported, not worked around.

**Frontier spend:** only the Opus spot-check (Q1).

**Background lane:**

- Per-paper `finding_read` jobs are stamped `origin='background'` through
  `workers/_mint.py::mint_background_job`, including children of an
  agent-queued parent. This is a deliberate exception to child-inherits-
  parent; `tests/test_job_origin_contract.py` gets the case.
- Human (0) and agent (1) jobs claim first; the class-age climb never
  reaches them.
- `claim.max_running.finding_read` defaults to 2 per host.
- Each job keeps one chunk in flight, so the reader adds at most 2
  concurrent requests to castor (SGLang `--max-running-requests 32`;
  gr485635: don't raise it).

**Saturation.** A local-slot wait timeout (`PRECIS_LOCAL_SLOT_WAIT_S`)
parks the job with `wake_when`, the `survey_extract` pacing; it does not
fail and does not go to cloud. It fails with a reason after 24 parks
(the `PRECIS_SURVEY_EXTRACT_MAX_PAUSES` default) or 10 call errors. When `llm-lane-scheduler.md` builds
its lanes, the reader's class is `castor_batch`.

**Throughput.** Roughly 60 eligible chunks per paper and 4–6 calls per
chunk (extract, verify, plus dedup per candidate): about 300 local calls
per paper, about 19k for the 62 climate papers. That is overnight-scale
at 2 jobs. S1 measures it on one paper before anyone sizes a fan-out.

## Inputs and queueing

| ask | how |
|---|---|
| one paper | `put(kind='job', job_type='finding_read', params={paper, serves})` |
| a strand | `params={serves: 'td…', papers: 'serving'}`: a parent resolves the target's `serves`/`served-by` papers and mints one background child per full-text paper (S3) |
| all full-text papers serving X | the same, for `serves: 'qu…'`; the parent walks the quest's strand todos one level down |

**Full text** means it passes the floor below. Stubs are skipped and
listed in the summary.

**Idempotency.** A child's `idem_key` is
`finding-read:<paper>:<serves>:<prompt_version>:<paper_text_sha>`. Re-queueing
the same tuple returns the existing job.

**Another target.** A second target on an already-read paper re-runs
extraction, because claims are question-directed (Q2). Its attaches
converge on the hubs the first run minted.

**Paper text changes.** Chunks are append-only (DELETE + INSERT on
re-ingest), so `paper_text_sha` changes and the key changes. The re-read:

- runs normally;
- converges on the hubs it already wrote;
- finds the old evidence edges whose `src_chunk_id` no longer resolves
  and hands those hubs to `taproot.reground` to re-find a passage in the
  new text.

It never deletes a hub because its chunk vanished.

## Failure modes

| mode | guard |
|---|---|
| hallucinated citation (the chunk doesn't say it) | verbatim `quote` check in code, then `chase:verify` `supports='yes'`; both before any write. Spot-check class `unsupported`. |
| scope stretching | the verifier's `same_setup`; a `partial` narrows via its caveats or the claim drops; the prompt requires conditions in `scope`/sentence. Spot-check class `scope-stretch`. |
| unit / scenario errors | number+unit and scenario-token presence checks in code; class `units-scenario` |
| hearsay (the paper citing others) | `is_proxy_deferral`, `has_grounding_prose` (hearsay sections), the prompt rule "this paper's own result or analysis"; class `hearsay` |
| one-page or partial source | its own floor: ≥ 8 eligible body chunks and ≥ 6,000 eligible chars, or the paper is skipped `partial-source`. The ONE-PAGE SOURCE banner is ignored either way; gr510135 shows it fires on a full 141-chunk manuscript (pa509665). Abstract-only papers are always skipped. |
| OCR or extraction garbage | a chunk failing `has_grounding_prose` is skipped. More than 30% of a paper's body chunks failing → the paper is skipped `garbled`, and one gripe is filed (`thread:ingest-and-fetch`). |
| castor down or busy | park, then fail with a reason; never cloud |
| embedder down (`block` raises `Upstream`) | park; never mint without dedupe (`dedup=False` is not available to the reader) |
| over-mining a dense review chunk | `MintCap` 3 per chunk per run; `landscape`-type claims are minted only when `bears_on` is non-empty |

## Dogfood order

1. **Kinetics first, as the calibration run.** Dry-run on pa508953
   (Dumesic 1999), serving qu500066, where kin-evidence's hand-written
   fi509632..fi509645 are the reference.
   - **Success:** ≥ 10 of the 14 claims recovered, scored as placement
     `attach` onto those hubs (convergence, not duplicates);
   - 0 new hubs a reviewer calls a duplicate of one of the 14;
   - spot-check pass at TIGHT;
   - the recall cost recorded (local calls, wall time).

   This run comes first even though the brief ranks climate first: it is
   the only paper with a hand-built answer key, so it measures recall and
   dedupe before the reader writes into a quest with no key (Q6).
2. **Climate, td510498 (MCB placement to sustain AMOC).** Hirasawa 2023
   (pa510467) plus whichever of the 19 AMOC stubs (pa510511–pa510529)
   have fetched to full text.
   - **Success:** the todo's own output table ("which deployment
     regions/seasons strengthen, weaken or leave AMOC unchanged in models,
     with model names, forcing magnitude and confidence; open modelling
     gaps") can be filled from reader findings alone, each row citing a
     hub;
   - every region or season claim names its model and forcing magnitude
     where the paper gives them;
   - spot-check pass.
3. **Climate, td510496 (MCB side effects).** It has no papers linked
   today, so the orchestrator links side-effect papers first.
   - **Success:** each sub-question (fisheries and marine ecosystems, salt
     deposition on land, regional precipitation, the operational items)
     has either ≥ 1 cited finding with a magnitude or confidence, or an
     explicit "no source in the read set" line in the todo;
   - the qu509651 rubric's side-effects column is filled from those
     findings.
4. **Kinetics at scale:** the full-text papers serving qu500066 that
   kin-evidence has not read, after S3.

## Slices

**S1: one paper, end to end, gated by hand.** Size L, about 2 builds. No
dependencies; gr509620 is not needed.

Build:

- the `finding_read` job type (`workers/job_types/finding_read.py`,
  registry `ServiceSpec`, `cost_sources=("finding_read",)`);
- the `finding_read` op in `LLM_OPERATIONS` plus the prod `llm.op.finding_read`
  castor row (a runtime setting, not code), and the prompt with
  `PROMPT_VERSION`;
- steps 1–5 and the measure dedupe;
- the placement contextvar;
- the partial-source floor;
- the run record and the idempotency key;
- `dry_run` defaulting to true: a dry run writes the candidate list and
  per-candidate placement to the job summary and touches no hub.

Acceptance (fake router, `scripts/test`):

- a quote absent from the chunk is dropped `quote-miss`;
- a number absent from the window is dropped `number-miss`;
- an all-deferral chunk yields no candidate;
- a fake `same` ≥ 0.85 attaches with a certified edge and no mint;
- `needs_review` mints nothing;
- a measurement matching a live measure is dropped `dup-measure`, and a
  mechanism claim with the same number is kept;
- re-queueing the same tuple returns the existing job;
- a changed `paper_text_sha` makes a new job;
- with no local rung the job parks and makes no cloud call
  (`placement_refusal`);
- children are stamped `origin='background'`;
- the live `eval_canon` run on castor shows zero over-merges, recorded in
  the commit;
- the pa508953 dry run is scored against fi509632..fi509645 with the
  `local-mesh-upkeep` claim-match scorer.

**S2: quality gate.** Size M, 1 build. Blocked by S1.

Build:

- `finding_read_accept` (sampler, Opus review prompt, defect classes,
  Wilson tally, gate state row);
- `serves` held until pass;
- revert on fail;
- the pause after 3 fails;
- gripe filing with class dedupe;
- human corrections counted.

Acceptance:

- a seeded batch with 2 planted unsupported findings fails, reverts its
  new hubs and leaves attaches gone;
- 3 passes move TIGHT → NORMAL only when the cumulative bound holds;
- the sample is identical on re-check;
- an existing open `finding-reader:<class>` gripe gets a comment instead
  of a duplicate;
- writes go live (`dry_run=false`) only after S2.

**S3: fan-out and re-read.** Size M, 1 build. Blocked by S2.

Build:

- the strand / quest parent;
- the full-text resolution;
- per-paper background children;
- the skip-if-read-at-this-key check;
- the text-change re-read with the `reground` hand-off.

Acceptance:

- a parent over a todo with 3 full-text papers and 2 stubs mints 3
  children and lists 2 skips;
- a second parent call mints 0;
- a re-ingested paper re-reads and hands dangling-edge hubs to
  `reground`;
- climate dogfood runs 2–3 above.

## Explicitly NOT in scope

- New finding columns or a migration: no structured magnitude field
  (Q4), no measure↔finding relation.
- Numeric extraction (survey_extract owns it) and figure or image
  reading.
- Any cloud fallback for reading; abstract-only papers.
- Contradiction adjudication (stage 8 stays absent); `disputes` are filed,
  never ruled on.
- Editing or merging human-written findings; promoting trust; nanopub.
- The lane allocator (`llm-lane-scheduler.md`) and the castor agent
  harness (`castor-agent-lane.md`). The reader runs on today's claim
  order and slots.
- Reading papers for no target: every run names a `serves`.

## Target + blast radius

New:

- `workers/job_types/finding_read.py`;
- registry entries;
- an `LLM_OPERATIONS` entry;
- the prompt.

Touched:

- `taproot/canon.py` (placement contextvar honoured by `extract_claim`,
  `dedup_judge`, `merge_confirm`);
- `workers/_chase_llm.py` (the same contextvar);
- `workers/_mint.py` and `tests/test_job_origin_contract.py`
  (background children).

Prod data: new claim hubs, evidence edges and `serves` links, plus
`app_settings` rows `llm.op.finding_read` and `finding_read.gate.*`.

Post-deploy look:

- the pa508953 dry-run summary;
- `llm_call_log` rows with `source='finding_read'` all `placement_routed`
  local;
- no `taproot:dedup` cloud rows in the reader's window.

## Open questions for Reto

1. **Does the Opus spot-check count as frontier spend that needs a
   per-run human ask?**
   - **Recommendation:** a standing authorization for the gate's samples
     only, capped at $5 per UTC day across all batches. The batch waits
     (does not fail) when the cap is hit.
   - Per-run asks would stall every background batch on a human, and
     without the check nothing can serve.
   - The per-sample cost is measured in S2 before the cap is set.
2. **Question-directed or whole-paper extraction?**
   - **Recommendation:** question-directed. Only claims bearing on the
     serving target are written; a second target re-reads.
   - Whole-paper output floods dedupe and the spot-check with off-question
     hubs nobody asked for.
3. **`needs_review`: hold or mint-with-todo?**
   - **Recommendation:** hold, with one review todo per run.
   - The agent mint door mints anyway because a human wrote the claim; an
     automated writer at volume should not.
4. **Structured magnitude?**
   - **Recommendation:** not in v1. The magnitude goes in the sentence,
     with `meta.measure_ids` when survey_extract has the number.
   - Revisit if quest rubrics need to sort by magnitude across findings;
     that is a migration plus a relation.
5. **Revert or keep a failed batch?**
   - **Recommendation:** revert (soft-delete new hubs, detach edges).
   - Held-but-live hubs pollute `block` for every other mint door.
6. **Order: kinetics calibration before climate?**
   - **Recommendation:** yes, one dry run on pa508953 (a single paper,
     local only), then climate td510498.
   - The brief puts climate first. Calibrating on the one paper with a
     hand-written answer key costs part of one night of castor time and
     turns "did it work" from an opinion into a recall number.
7. **Which local model?**
   - **Recommendation:** gpt-oss-120b on castor, the survey_extract model,
     for every reader call.
   - Nemotron 3 Super is the alternative once `local-mesh-upkeep` has
     scored both on finding extraction.
