# term taxonomy and measurand list

**Status:** ends when the corpus's measurands live as taxon nodes with
identity, navigable as a mesh, gardened without hand curation, and bound to
sourced numbers (the norr-her-meta paper is the first consumer). Today the
taxon kind is ready to build and four items wait on it; the measurand-list
pipeline (census → discovery → freeze) has its prompt, census and
synonym fixes measured (stability 0.052 → 0.270 on the same 100 hubs, 0.55
of what that sample can show), no vocabulary blocker left, and a freeze that
cannot land until the taxon kind exists.
**Last reviewed:** 2026-09-30
**Worktree:** `term-taxonomy`

## Do next

1. **backlog/term-taxonomy.md** — status ready, Reto's two rulings recorded
   2026-09-30 (six seed axes, element symbols one node). Direct dependency
   of taxonomy-bootstrap's freeze, measures-substrate, graph-gardener and
   knowledge-mesh; nothing else in this thread can ship its output until
   the taxon kind exists.
2. **backlog/taxonomy-bootstrap.md §Resume (2026-09-30)** — per-call
   metering + raw-reply capture in `discover()` + the n=100 pass criterion.
   Before the 1231-call full run, or that run is an unmeasured spend;
   concurrency vs packing is decided by the metering's cache-read counts.
3. **backlog/norr-her-meta.md** — the consumer of list.v1.yaml (20-paper
   round, gold set, figure). Below 2 because it starts on a frozen list.
4. **backlog/measures-substrate.md** — blocked-by term-taxonomy; identity =
   taxon + reference + convention, so it lands right after 1 and unblocks
   knowledge-mesh.

## Horizon

1. **backlog/taxonomy-bootstrap.md full run** (1231 calls) — waits on the
   re-probe clearing the n=100 criterion and the metering answer; delivers
   list.v1.yaml, the first frozen measurand list.
2. **backlog/term-taxonomy.md v1** (taxon kind, instance-of, specialises
   with meta.axis, six seeded axes) — waits on nothing; the node type every
   later milestone writes into, the freeze target for 1.
3. **backlog/norr-her-meta.md steps 3-5** (20-paper round, gold set, figure,
   draft) — waits on 1; the paper's spine.
4. **backlog/measures-substrate.md** — waits on 2; measure identity and the
   qualifiers 3 needs to store values honestly.
5. **backlog/term-taxonomy.md v1.5** (axis start node, meta.axis validated)
   — waits on 2 and gardener-promoted axes; link filters by axis.
6. **backlog/knowledge-mesh.md** (eye ladder, hop filters, universal row) —
   waits on 4; fisheye navigation over taxons.
7. **backlog/graph-gardener.md** — waits on 2 plus a populated mesh from 1;
   earned axes and merges without hand curation.
8. **backlog/curation-gate.md** — waits on eval-run-spine's verdict column
   (serving-programme thread); the guard that lets 7 run unattended.
9. **backlog/corpus-quantitative-extraction.md** — waits on 1 and 4; sourced
   numeric triples bound to list entries.
10. **Claude Code memory/skills mesh pilot** (NOT filed; Reto's yes pending)
    — waits on 6; a recall measurement, the only AC that makes the
    migration decidable.

## Parked

- (none beyond Horizon's stated waits)

## No action needed

- taxonomy-bootstrap blockers 2, 3a, 3b and 4 — measured fixed (over-cap
  107 → 3, facet nodes 22 → 0, stranded unit borrowed, synonym families one
  node each); nothing further. The campaign vocabularies grow from node
  notes, not from code.
- the 0.80 stability threshold — not readable at 100 hubs (unit-key ceiling
  0.49); it is a full-run criterion, not a probe failure.
