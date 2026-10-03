---
status: draft
pillar: memory-graph
title: Graph gardener — scheduled passes that merge, split, relink and prune the knowledge graph, always as reversible proposals
prio: normal
model: sonnet
---

# Graph gardener — maintenance passes over the knowledge graph

## Motivation / why

Reto, 2026-09-28: *"gardening, do we have agents combine and split and
enhance links?"* Today: no. The graph only ever grows. Every write path
mints — nodes, edges, hubs — and nothing consolidates. Duplicates
accumulate, conflated terms stay conflated, and an edge that was wrong
when it was written stays wrong forever.

`term-taxonomy.md` makes this concrete and gives it a first customer. Its
v1 deliberately defers merge-as-redirect, alias repointing and the dedup
judge, and its reaches-≥1-start-node check *detects* orphans without
repairing them. Those deferrals are correct — they are curation, not
schema — but they have to land somewhere, and this is it.

The existing hub door already does the read half of this well (lexical →
embedding neighbour → judge). What is missing is anything that runs on a
schedule, over the whole graph, without a human asking.

## Design

**Five passes, one shape.** Each is a job type, each scans a slice, each
emits **proposals** — never a silent rewrite:

- **merge** — two nodes that are the same thing. Reuses the hub door's
  ladder with the dimension hard gate; applies as merge-as-redirect
  (loser becomes an alias, mentions repoint, reversible). Memories
  included: `MemoryHandler.supersede()` is the apply step for a memory
  merge; it never fires today (prod `superseded_by` was 0 at the
  2026-07-23 audit — re-confirm before building). Absorbed 2026-09-29
  from `graph-completeness-findings.md` (deleted).
- **split** — one node used as several things. The trigger is evidence,
  not suspicion: an open polysemy `finding` on the node, or measures
  under it with mutually incompatible dimensions.
- **relink** — a missing edge. Embedding neighbourhood plus a judge
  proposes `specialises` edges the author did not write. Also repairs
  orphans: a node reaching no start node gets a proposed parent. Also
  the isolated-memory nursery check — ~10% of memories have zero links
  either direction at the 2026-07-23 audit; propose a link rather than
  leave them unreachable. Absorbed 2026-09-29 from
  `graph-completeness-findings.md` (deleted).
- **prune** — an edge that fails a structural check (cycle, an axis that
  leaks across branches, an edge whose endpoint is retired).
- **tag-sweep** — one closed taxonomy axis per run (`topic:`, `area:`,
  `status:`…), sweeping under-tagged refs and proposing tags, same
  review-queue rule as the other four (gr51220). Absorbed 2026-09-29 from
  `corpus-auto-tagging-cadence.md` (deleted); that item's two open
  questions — mechanism, review gate — are answered by this item's
  shape: a scheduled pass, proposals as `todo` + `waiting-for:reto`,
  never blind apply.

### Vocabulary consolidation rules (from the 2026-09-29 mesh handoff)

Cluster **labels, not edges**: a merge decision over relations or axis
values is made over a few dozen usage examples per label (the taxon's
definition + sample edges), never by reading every edge.

Merges are **bulk remaps that keep the prior**: the losing relation/axis is
recorded in `links.meta.prior_relation` / `links.meta.prior_axis` on every
remapped row, so a bad merge is one query to undo. This is the same
reversibility rule as merge-as-redirect for nodes.

**Promotion thresholds are recurrence, not corpus size**: a free
`meta.axis` value (or a proposed relation) is promoted when it has ≥N uses
across ≥M distinct writing runs. Runs are `agentlog` refs; this needs
`links.meta.agentlog_id` stamped on every link written under
`PRECIS_CURRENT_AGENTLOG` — in-scope item 6: stamp `agentlog_id` into
`links.meta` at write time; the tool_calls ledger records names only and
cannot supply it. Thresholds may be relaxed while the corpus is small.
Promotion of a relation is NOT a row flip: it emits a migration-proposal
`todo` (`waiting-for:reto`) because the relation registry changes only by
migration.

**Only edges near a new boundary get the per-edge read** after a merge or
split; the rest are remapped in bulk.

**Proposals, not edits, is the load-bearing rule.** A gardener that
rewrites silently is unreviewable, and the failure mode is not a bad edit
but an undetected one. Each proposal lands as a `todo` with
`waiting-for:reto` (the same mint-is-the-spend boundary the roadmap body
uses), carrying the before/after and the evidence that triggered it.
Merges additionally record enough to reverse.

**Budgeted and quiet.** A pass that finds nothing writes nothing — no dry
run entries, no "checked 400 nodes" logs. Reuse the existing dry-rest
escalation so a gardener that proposes nothing for N runs cools itself
rather than burning a slot forever.

**Never auto-apply a judge's verdict.** Embedding neighbours are for
exploration only — that ruling already holds for taxon promotion and
applies unchanged here.

## In scope

1. Five passes behind one `graph_garden` job type with a `pass=` selector.
2. Proposal emission as `todo` + `waiting-for:reto`, with a reversal
   record on merges.
3. An apply verb for an accepted proposal, and an undo for an applied
   merge.
4. Scheduling: off by default, enabled per pass by service config.
5. Runtime docs: `precis-gardener-help`.
6. Stamp `agentlog_id` into `links.meta` at write time; the tool_calls
   ledger records names only and cannot supply it.

## Explicitly NOT in scope

- Any change to `term-taxonomy`'s schema — this consumes it.
- Numeric-conflict detection across measures (`knowledge-mesh.md`).
- Auto-apply without a human accept. Not in v1, and probably not ever
  for merge and split.
- Gardening prose chunks (drafts, paper bodies). Nodes and edges only.
- A new judge. Reuse the hub door's.

## Acceptance criteria

1. A seeded pair of duplicate taxon nodes with matching dimension yields
   exactly one merge proposal; a pair with different `si_vector` yields
   none.
2. A node with an open polysemy finding yields a split proposal naming
   the incompatible uses; the same node without the finding yields none.
3. A taxon node reaching no start node yields a relink proposal naming a
   candidate parent.
4. An accepted merge repoints mentions and leaves the loser resolvable
   as an alias; undo restores both nodes and every repointed mention.
5. A pass over a clean graph emits zero proposals and zero log entries,
   and cools after the configured number of empty runs.

## Target + blast radius

New: `workers/job_types/graph_garden.py`, `graph/garden/*.py`,
`precis-gardener-help`. Touched: the job-type registry, service config.

Read-mostly. The only writes are proposal `todo`s and, on explicit
accept, the merge/relink itself. Nothing runs unless enabled.

## Open questions / decisions log

- **[decided 2026-10-03, Reto knowledge-mesh-10]** A proposal that a
  bigger model reviews skips the per-proposal `waiting-for:reto` todo.
  It lands as a `proposed` row in `local-mesh-upkeep.md`'s `reviews`
  ledger, and Reto sees a weekly sample. The per-proposal todo stays for
  a pass with no reviewing model. Merge and split never auto-apply.

- **[open]** Whether split proposals are worth building in v1 — they
  need a polysemy finding to exist first, and nothing mints those yet.
  Splitting could ship after the annotation path is real.
- **[open]** Whether relink should propose across the whole graph or
  only within a start node's branch. Whole-graph is likely too noisy.
