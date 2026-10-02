# knowledge mesh — taxonomy, measures, memory and the fisheye

**Status:** ends when the graph is the memory: a richly linked mesh of
small typed nodes (findings, measures, taxa, skills, memories, plans,
arguments, logbook entries) that an LLM navigates by walk and fisheye,
with typed links whose rules are data, and drafts rendered from it
(Reto's goal, stated 2026-09-30). Today the substrate (refs, chunks,
links, tags, an 81-relation registry) already is that mesh; the taxon kind
shipped 2026-10-01 (kind, seed of 79 nodes, guarded hierarchy, facets,
dedup); the measurand-list pipeline
has its prompt, census and synonym fixes measured (stability 0.052 →
0.270 on the same 100 hubs, 0.55 of what that sample can show), per-call
metering in place, and a freeze that cannot land until the taxon kind
exists. Reviewed 2026-09-30 against the goal: the memory half (skills,
memory files, session history, context hierarchy) had no ordering owner
and is ranked here from now; the ordering rule is "unblocked and visible
first, then the measures chain, then the memory half behind the walk".
**Last reviewed:** 2026-09-30 (the pillar review the same day inserted
class-lattice-similarity-spaces-and-laws, first-party-experiment-records,
graph-health-metrics, five parked gripes, and the seam with
`graph-memory-consumers.md`)
**Worktree:** `knowledge-mesh`

## Do next

1. **backlog/taxonomy-bootstrap.md §Resume** — the gate is now cross-run
   naming agreement (Reto, knowledge-mesh-7). Bar 0.711 folded, from two
   unpacked 100-row runs (median 0.773). Packed runs read 0.68-0.71
   against unpacked, so packing likely costs agreement. The full run's
   configuration waits on knowledge-mesh-8 (unpacked ~$100 recommended).
2. **backlog/hub-duplicate-reconcile.md** — Reto ruled 2026-10-02
   (td461151, gr180306): the cheap duplicate-hub reconcile, in order:
   re-check on embed, text-version watermark, a distance cutoff
   calibrated on hand-merged twins, one backfill, a pre-publish check.
   Above fisheye because duplicate hubs split live evidence that the
   monthly papers (qu459585) and nanopub cites read now. Reuse waits on
   stranded branch `gripe_180306` (bundle on Reto's Mac) reaching origin;
   `merge_hubs` on main covers the merge if it cannot. Sibling gr462136
   (an errored dedup judgment read as "different") is owned by
   claims-and-evidence.
3. **backlog/linkable-parts.md** — Reto ruled 2026-10-02
   (knowledge-mesh-6, "this is what we do"): a catalog part becomes a
   chunkless ref on first use (`ref_identifiers('lcsc', …)`), `component
   realized-by part`, `pcb contains part` with the refdes; items inside a
   design stay addressed through it. Unblocked; one small relation-seed
   migration goes to the orchestrator's gate. Unblocks
   datasheet-facts-mesh (Horizon 16).
4. **backlog/fisheye-everywhere.md** — status ready, no blocker. Per-family
   ring groups, `fisheye+2hop` and the `+recall` suffix shipped
   2026-10-02 (in-scope 1 and 3); open are the ladder on every kind
   (in-scope 2, AC 1), the `/eye/<handle>` focus page (in-scope 4) and the
   skill's partial-rollout section. The goal's most visible surface; also
   the answer to "a viewer for the memory" once 7 lands.
5. **backlog/measures-substrate.md** — unblocked (taxon kind shipped 2026-10-01); identity =
   taxon + reference + convention, and it unblocks
   knowledge-mesh and the experiment loop. Fold-in ruled 2026-09-30
   (Reto: `component_spec_values` joins `measures` in the same
   migration).
6. **backlog/class-lattice-similarity-spaces-and-laws.md** — owned here as
   term-taxonomy's v2 (defined classes as canonical constraint sets with
   membership yes/no/unknown, per-axis similarity spaces, participant
   roles, curves/fits/laws — Reto's sourced-KG design notes, folded in at
   the pillar review). Right after 5 because it builds on the same taxon
   identity; consumed by `se-machine-design.md` (pocket specs) and
   `materials-molecular-substitution-db.md`.
7. **backlog/file-mirror.md** — status draft, no blocker. Skills and the
   Claude Code memory files as read-only `markdown` roots with links from
   `[[slug]]` and frontmatter; replaces the unfiled "memory/skills mesh
   pilot". Mirror first, ruled 2026-09-30; native authoring is judged
   after its recall AC. Readiness vet the same day: needs-work, four
   blockers folded into the item, re-vet before build. Below 6 only
   because its recall AC is cheap to run at any time.
8. **backlog/norr-her-meta.md** — the consumer of list.v1.yaml (20-paper
   round, gold set, figure). Starts on a frozen list.

## Horizon

1. **backlog/taxonomy-bootstrap.md full run** (1231 calls) — waits on the
   re-probe clearing the probe criterion and the concurrency decision;
   delivers list.v1.yaml, the first frozen measurand list.
2. **backlog/term-taxonomy.md v1.5** (axis start node, meta.axis validated)
   — waits on gardener-promoted axes; link filters by axis. This is
   the thread's answer to "hierarchies over links": axis hierarchy, not
   relation specialisation.
3. **backlog/knowledge-mesh.md** (walk, numeric conflicts, quest_mesh,
   universal row) — waits on Do-next 5; its ladder item moved to Do-next 4.
4. **backlog/experiment-loop.md** — waits on Do-next 5; hypothesis → todo
   `tests` → measure → ruling → refuted, walked end to end through the
   verbs, plus the skill that teaches it.
5. **backlog/graph-gardener.md** — waits on a populated mesh
   from Horizon 1; earned axes and merges without hand curation.
6. **backlog/session-history-into-precis.md** — the linear logbook: human
   sessions as `conv`, machine runs as `agentlog` (Reto's 2026-09-29
   split); waits on the shared redaction path, whichever item ships it
   first.
7. **backlog/curation-gate.md** — waits on eval-run-spine's verdict column
   (serving-programme thread); the guard that lets 5 run unattended.
8. **backlog/draft-linearization.md** — waits on 3; the graph as the
   truth and a draft as a rendered subgraph. Render-only v1, ruled
   2026-09-30.
9. **backlog/context-memory-hierarchy.md** — the resident/discovered split
   for the harness memory; P0 is repo-only and can go any time, P1 after
   Do-next 7 gives the topic files a recall measurement, P2 ruled yes
   2026-09-30 (one resident identity + style block).
10. **backlog/corpus-quantitative-extraction.md** — waits on Horizon 1 and
    Do-next 5; sourced numeric triples bound to list entries.
11. **backlog/norr-her-meta.md steps 3-5** (20-paper round, gold set,
    figure, draft) — waits on Horizon 1; the paper's spine.
12. **backlog/dreaming.md** — the consolidation pass over memory nodes;
    revisit once 5 and 7 exist, since both replace hand consolidation.
13. **backlog/first-party-experiment-records.md** — waits on Do-next 5;
    our own runs (a job, a quest tick, an se design) need the same measure
    identity as a paper's before they can be stored honestly. Note the
    open contradiction it records: `measures-substrate.md` §3 owns an
    `experiment` kind, `experiment-loop.md` says no new kind.
14. **backlog/graph-health-metrics.md** — waits on Horizon 3; a populated
    mesh is the precondition for measuring its own reachability, orphan
    rate and edge precision, which is what gardener (5) fixes against.
15. **backlog/capability-landscape-steals.md** — five externally sourced
    ideas (a ChemBench eval slice, categorizer rule distillation, and
    three more) from the capability-landscape comparison; sequenced behind
    the substrate (Do-next 1-6) by choice, not blocked.

16. **backlog/datasheet-facts-mesh.md** — waits on Do-next 3 and Do-next
    5; a pulled datasheet's ratings, specs, package and pin table as
    page-cited `measures` rows on the part ref (Reto, ewod-pcb-2 and
    knowledge-mesh-6); the pin table feeds gr458878's pad-map check.
17. **backlog/greenfield-schema-review.md** — file only (Reto,
    knowledge-mesh-6): what the schema would be designed today and the
    migration path there. A review, not a refactor; any time.
## Waiting on Reto

- **knowledge-mesh-8 (review queue, 2026-10-02):** the full taxonomy
  run unpacked (~$100, recommended) or packed (~$37), or a ~$2
  packed-vs-packed check first. The method is with the orchestrator in
  `reviews/knowledge-mesh.md` §3.

The six 2026-09-30 rulings (fold-in, mirror first, render-only, this
rename, memory-lint repo-local, one resident block) are recorded in their
items.

## Parked

- **gr445532** — experiment tracking needs multi-class context for
  qualitative terms ("low temperature" means opposite things in different
  communities); unparks with Do-next 6, whose per-axis similarity spaces
  are the natural home for a comparison class.
- **gr449840** — precis-finding-help's admission criteria exclude the
  definitional/methodological claim classes a taxonomy or architecture
  paper needs to cite; unparks with Do-next 6, which names a definitional
  claim class explicitly (membership yes/no/unknown).
- **gr182230** — taproot chase-trigger recall gap (60 days old, pre-enablement). Unparks
  when chase_trigger is enabled by default; inert until then.
- **gr445531** — whether spectral graph theory buys anything on precis's
  graph structures; unparks when Horizon 3 gives it a populated graph
  worth measuring the spectrum of.

## No action needed

- relation constraints — shipped 2026-10-02 (migration 0180, one validator
  `_link_tag_ops.py::check_relation_constraints` at both link doors, the
  shared `Store.ancestors` walk). Open, non-blocking: whether a
  `functional` relation should accept a repoint instead of
  remove-then-add; it refuses until an agent gripe asks. `instance-of`'s
  range row lands with term-taxonomy. Revisit trigger: when `contains`
  grows (deep se/BOM trees), measure the acyclic check's per-link
  `Store.ancestors` walk and its depth cap of 64.

- taxonomy-bootstrap blockers 2, 3a, 3b and 4 — measured fixed (over-cap
  107 → 3, facet nodes 22 → 0, stranded unit borrowed, synonym families one
  node each); nothing further. The campaign vocabularies grow from node
  notes, not from code.
- the 0.80 stability threshold — not readable at 100 hubs (unit-key ceiling
  0.49); it is a full-run criterion, not a probe failure. The probe reads
  `min_probe_ratio` (0.60 of the ceiling) instead, stated 2026-09-30.
- per-call metering + raw-reply capture — `responses.jsonl` per discovery
  call, streamed as each call lands; nothing further until a paid run
  fills it.
- memory-lint `--currency` stays a repo-local script (Reto 2026-09-30;
  the extraction stub is deleted, `docs/how-to-setup-like-this.md`
  carries the line).
- a relation hierarchy (sub-relations as rows) — rejected in
  `backlog/term-taxonomy.md`'s reconciliation table; Horizon 2 covers the
  need.
- a graph database or a closure table — rejected in knowledge-mesh and
  term-taxonomy with a stated revisit trigger.

## Seam

`graph-memory-consumers.md` (dormant) owns the agent-side affordances this
thread does not rank: draft authoring in the graph, the `focus` verb,
capability discovery, skill quality gates, source-code ingest. The memory
half (file-mirror, context-memory-hierarchy, session-history-into-precis)
and every surface item (fisheye-everywhere, draft-linearization) are
ranked HERE. Do not duplicate ranking across the two files.
