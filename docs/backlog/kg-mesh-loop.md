---
status: draft
title: Knowledge-graph mesh loop — a cheap tier proposes stance candidates per quest cluster, a strong tier writes pinned claim hubs and stance edges, every write passes a before/after retrieval test, on the 2-h cadence under budget caps
pillar: memory-graph
prio: high
model: opus
---

# Knowledge-graph mesh loop

Approved by Reto 2026-10-22 (chat). Turns the hand-driven gardener pilot
into a recurring pass. Builds on [graph-gardener-value](graph-gardener-value.md)
(slice 1 machine-edge gate, slice 2 `precis mesh retrieval`, slice 4 scout,
slice 5 castor filler) and [local-mesh-upkeep](local-mesh-upkeep.md)
(reviews ledger, per-action bars).

## Motivation / why

Pilot 2026-10-22, Pd-hydride / Pd(111) HER, qu164903 (memory me504790;
hubs fi504759–fi504782): 23 writes, 19 clear wins over what a RAG call
returns (stance between papers 0.3 V apart on the same facet, a hub
re-grounded from a verifier-rejected chunk to the table that holds the
numbers, a hypothesis qualified by its own motivating paper's SPD), ~6k
tokens per useful write. The pilot ran on Fable by hand because:

- `link(meta=)` is refused on finding/paper (gr504789; fix in flight,
  tree `g5-handlers`) — the per-edge why/evidence had to go into a memory.
- Nothing records which nodes a reviewer considered, so a second pass
  re-reads everything. Review stamps (`kg-review:<model>:<date>:ok|fixed`,
  the gardener skipping considered-and-unchanged nodes) are being built
  (Q1).
- The findings review found value-over-RAG at 16% overall: harvest
  writers mint restatements, and quest→finding edges point the wrong way
  (being fixed). The loop must not mint from that pile; it writes only
  where a signal says RAG cannot recompute the fact
  (graph-gardener-value §The argument).

## Design

```text
select cluster ──▶ cheap tier: candidates ──▶ strong tier: writes ──▶ acceptance test ──▶ stamp + metrics
 (SQL)             (castor / Haiku,            (Opus / Fable,          (retrieval before/        (reviews row,
                    proposals only)             pins + meta)            after; revert on fail)    coverage)
```

**Cluster selection (SQL, no model).** A quest's hub set = findings linked
from the quest (after the direction fix: finding `serves` quest). A
cluster qualifies when it holds ≥8 hubs and carries <1 stance edge per
hub (`supports|disputes|qualifies|refines|corroborates|explains` among
the set, both directions). Order: quest `prio`, then lowest stance
density, then most recent paper. Pilot check: qu164903 (2,546 links, a
handful of stance edges) selects first.

**Cheap tier (castor gpt-oss-120b via `llm.op.kg_mesh:propose`; Haiku
fallback when castor is dark).** Writes proposals only — reviews-ledger
rows `actor='kg-mesh-propose'`, `verdict='proposed'`, payload
`{signal, hubs, chunks, claim}`. Four signals, deterministic where the
data allows, the model only for the judgement:

| signal | detector | pilot case |
|---|---|---|
| same measurand + facet + method across papers | group `quantifies` edges (179) / `measures` rows by measurand, the refs' `instance-of` facets (ontology-v2 slice 3) and `mode`/method; two papers with numbers → ask stance | fi504769 disputes fi504766 (hydride onset 0.3 V apart, same facet) |
| hub chained to a verifier-rejected chunk while the same paper holds the numbers | evidence edge `support='no'` + a numeric-atom match of the hub's numbers in another chunk of the same paper | fi498891 re-grounded from pc4491073 to Table 1 at pc4491050 |
| hypothesis hub whose motivating paper says the opposite | `motivated-by` edge → the paper's chunks nearest the hub card → ask stance | fi504782 qualifies fi503043 |
| `AUDIT:ungrounded-number` whose cited chunk contains the numbers | numeric-atom match in the cited chunk → tag-removal candidate | me272562 cleared |

**Strong tier (Opus default, `llm.op.kg_mesh:write`; Fable re-review on a
sample, as `precis garden review` does).** Reads the proposal, the hubs
and the pinned chunks; writes at most: a claim hub (`put(kind='finding',
supporters=[pins])`) and stance edges (`supports|disputes|qualifies|
refines|corroborates`, hub→hub or paper→hub) with `meta.evidence=[pc…]`
and `meta.{model,prompt_version,run_id,why}` — the slice-1 gate admits
nothing less. Never `related-to`; structural edges go to the gardener
ledger. `explains` only hub→hub through `taproot.hub.link_claims`.

**Acceptance test, per write.** The proposal carries the natural question
the write answers ("at what potential does Pd(111) form the hydride?").
`mesh_eval/retrieval.py` runs it before and after the write against the
quest's fisheye; the write stays when the gold/pinned chunk's rank
improves or tokens-to-answer drop, and neither regresses. A failed write
is retired (link removed, hub `meta.reverted`), never hard-deleted, and
the proposal row gets `verdict='rejected'` with the two scores. The kept
write stores `meta.acceptance={question, before, after}`.

**Stamp + coverage.** Every hub the strong tier considered gets the
review stamp (Q1 build) — recommended substrate: a reviews-ledger row
with `content_sha`, the `kg-review:` tag as its mirror — so the next tick
skips unchanged nodes. Metrics per quest, `precis mesh loop stats`:
hubs, stamped hubs, stance edges per hub, clusters left, writes
proposed/accepted/reverted, strong-tier tokens per accepted write,
proposer–writer agreement (the gardener's `stats` shape).

**Cadence and budget.** Own `job_type='kg_mesh_loop'` on the same
recurring-todo shape as `graph_garden` (`precis-recurring-help`; one open
child at a time, idempotent ticks), ticking on the existing 2-h recurring
seed. Per tick: one cluster, ≤10 strong-tier writes, ≤40 cheap-tier
candidates. Per day: `max_writes` (default 60) and `max_usd` (default
well under the $25 ask threshold, `docs/conventions/thresholds.md`); the
tick exits `skipped='budget'` when either is hit. `llm_usage` rows carry
`run_id` so the cost is one query.

**Measures sharpen the cheap pass.** Signal 1 is a model judgement today
because numbers sit in chunk text. Once a cluster's papers have `measures`
rows (`survey_extract` + `measure_accept`, graph-gardener-value slice 5a),
the same signal is a unit-normalised comparison per (measurand,
reference, normalization, conditions): compatible → `corroborates`,
incompatible → `disputes`, the model writes only the `why`
(knowledge-mesh §3). Slice 5 makes extraction the loop's step 0 for a
cluster without measures.

## Slices

0. **Prerequisites, verified not built:** gr504789 `link(meta=)` landed;
   review stamps (Q1) landed; quest→finding direction fixed; one
   retrieval gold question for qu164903 answers through `precis mesh
   retrieval` on prod (the harness exists; gold labels await Reto).
   Gold set built: `scripts/llm_eval/gold_set/retrieval_qu164903.jsonl`
   (11 questions over hubs fi504759–fi504782, typed lookup/cross-paper/
   stance/grounding, 5 marked `open`; `labelled_by: model:…` until Reto
   reviews; form test `tests/test_kg_mesh_gold.py`). Gap: none of the 10
   hubs links to qu164903, so the fisheye arm cannot reach them from the
   quest anchor until the quest→finding fix lands.
1. **Cluster selector + coverage metrics.** SQL only; `precis mesh loop
   clusters|stats`. Run read-only on prod; qu164903 selects first.
2. **Cheap tier.** The four detectors → ledger proposals on castor, no
   graph writes. Measure: Opus reads 50 sampled proposals; precision per
   signal is the bar for slice 3 (≥60% or the signal is switched off).
3. **Strong tier + acceptance test + revert**, manual trigger
   (`put(kind='job', job_type='kg_mesh_loop', params={'quest': …})`).
   Re-run the Pd-hydride cluster; compare with the pilot's 19/23 and the
   per-write token count.
4. **Cadence, budgets, kill switches**, `watchdog:cadence/kg_mesh_loop`.
5. **Measure-first sharpening**: step 0 extraction for clusters without
   measures; signal 1 switches to numeric when measures exist.

## Acceptance criteria

- Slice 1: `precis mesh loop clusters` lists qu164903 with hubs ≥8 and
  stance density <1; stats print for every active quest.
- Slice 2: a tick on qu164903 writes ≥10 proposals and 0 links; every
  proposal names its signal, chunk pins and question; sampled precision
  per signal is recorded in the item.
- Slice 3: every kept write carries `meta.evidence`, `meta.model`,
  `meta.prompt_version`, `meta.run_id`, `meta.why`, `meta.acceptance`;
  a write that fails the retrieval test is retired within the same job
  and visible in `get(kind='finding', view='judgments')` or the link's
  `meta.reverted`; accepted-write rate on the pilot cluster ≥ 70%
  (pilot: 19/23).
- Slice 4: two ticks never overlap; a tick over budget exits
  `skipped='budget'`; the day's spend is one `llm_usage` query by
  `run_id`; the stamp makes a second tick on the same cluster skip every
  unchanged hub.
- Slice 5: on a cluster with measures, signal-1 candidates come from the
  numeric comparison with no model call before the `why`.

## Costs

Cheap tier on castor: $0 marginal (local); Haiku fallback metered. Strong
tier: ~6k tokens per useful write (pilot), ≤10 writes per tick, so a
full day of ticks is bounded by `max_writes` (60) before `max_usd` bites.
Acceptance tests are embedding retrieval, no LLM. Slice 5 extraction
runs on castor under its own sampled-acceptance budget.

## Kill criteria (any one stops the recurring; a gripe names it)

- Accepted-write rate < 50% over three days.
- Retrieval delta ≤ 0 on two consecutive clusters.
- Opus–Fable disagreement > 30% on the sampled writes.
- Budget cap hit three days running with no coverage gain.
- Any kept edge found without pins or annotation (gate failure) — stop
  and treat as a bug, not a tuning problem.

## Explicitly NOT in scope

Minting hubs from harvest findings (restatements — the findings-review
problem); structural graph edits (gardener ledger); `related-to`
anywhere; the retrieval gold-label build (graph-gardener-value slice 2);
the review-stamp mechanism itself (Q1); extraction quality
(measures-substrate); cloud fallback for the strong tier beyond Opus.

## Target + blast radius

New `src/precis/graph/mesh_loop/` (selector, detectors, writer,
acceptance) beside `graph/garden/`; `workers/registry.py` (job_type),
`utils/llm/operations.py` (`kg_mesh:propose`, `kg_mesh:write`),
`llm_catalog.py`, `cli/mesh.py` (`loop clusters|stats`), a watchdog
cadence row, `mesh_eval/retrieval.py` (before/after entry point).
Skills: `precis-gardener-help` (sibling pass section), `precis-link-help`
(no change — the gate is the contract), a short `precis-mesh-loop-help`.
No migration unless the reviews ledger needs a `signal` column (open
question 1).

## Open questions / decisions log

**Ruled (Reto 2026-10-22): all approved as recommended; slice 0 may
start.**

1. **Stamp substrate: tag or reviews row.** Recommend the reviews row
   (`content_sha` gives "unchanged" for free; the gardener reads it
   already), with the `kg-review:` tag written as a mirror for agents.
2. **Strong tier model.** Pilot was Fable. Recommend Opus default with
   the Fable sample re-review the gardener already runs (its Q2), so
   both numbers exist before choosing.
3. **Acceptance metric.** Rank of the pinned chunk vs tokens-to-answer.
   Recommend both, keep when either improves and neither regresses;
   revisit after slice 3's first 100 writes.
4. **Revert policy.** Recommend retire with `meta.reverted` (undo-able,
   visible), never hard delete.
5. **Own job_type or a gardener `pass`.** Recommend own job_type: the
   budgets and kill switches must be independent of merge/prune ticks.
6. **Thresholds (≥8 hubs, <1 stance/hub, ≤10 writes/tick, 60/day).**
   Recommend shipping them as job params with these defaults; the first
   week's stats set them.
7. **Hub minting from signal 1 when no hub exists on either side.**
   Recommend yes, but only with both papers' chunk pins as supporters
   (a two-source hub is what the pilot's best writes were); a one-paper
   candidate becomes an `AUDIT`-free finding proposal, not a hub.
