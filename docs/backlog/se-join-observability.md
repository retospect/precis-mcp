---
status: draft
title: make a join's findings and its catalogue resolution visible without SQL
prio: high
---

# Make a join's findings and its catalogue resolution visible without SQL

## Motivation / why

A join reports. Nothing shows the report.

The findings a join produces survive only inside the minted structure
ref's `meta['generated']['report']['findings']`; the op echo carries
counts, not codes. There are seventeen `se` views and none of them shows
what an op *reported*. In tests this means hand-written helpers to dig the
codes out of meta; against prod it means SQL archaeology. During the
2026-09-30 dogfood, answering "did the catalogue get consulted, and which
row won?" took six queries and a container exec, and the question that
actually mattered — "is this composite intact?" — was not asked at all,
because asking it was expensive.

SPEC §25.3 specifies `view='catalogue'`. It does not exist. So the one
number a join looks up — the seam radius, and the leak threshold that
decides whether the seam is sound — has no provenance surface:
`resolve_edge` returns a label (`pinned z`, `exact z10`, `nearest z12`)
and `compose` discards it unless the row happens to *narrow* the radius.
The common case, where the pinned constant governs, is invisible.

This is the leverage item for the hexfold thread: it does not block any
correctness fix, but every correctness fix below it is diagnosed through
it.

## In scope

Three surfaces, probably three slices:

1. **`view='report'` on a design or block** — the findings a generate or
   join recorded, by code and severity, from the bound structure's meta.
   Read-only over stored records; no recomputation.
2. **`view='catalogue'`** (SPEC §25.3) — the rows the design's joins can
   see, and per join side, which row was consulted, its label, and
   whether it was preferred or withheld by the measured-row gate.
3. **A dry-run for `join`** — the equivalent of `generate`'s
   `fidelity="check"`: resolve, compose, report, mint nothing. Today every
   probe mints a composite, which is how the dogfood permanently
   corrupted a design in order to ask a question about it.

## Explicitly NOT in scope

- Changing what a join computes or records. This is a reading surface.
- Persisting findings in their own table. They are already in the
  structure's meta; this exposes them.
- A web/3D viewer surface. MCP views only.

## Acceptance criteria

- The codes a join emitted can be listed for a named block through one
  verb call, with no SQL and no knowledge of the meta layout.
- For a join, the seam radius and leak threshold in force are shown with
  their provenance label, including when the pinned constant governed.
- A join dry-run on a design leaves the design byte-identical — assert it.
- The test helpers in `tests/test_se_join.py` that dig findings out of
  meta by hand can be deleted in favour of the view.

## Target + blast radius

`precis_se/handler.py` view dispatch and its renderers; `hexfold.join`'s
`compose` for surfacing the resolution label it already computes;
`precis_se/atomic/join.py` for the dry-run path. Read-only except the
dry-run, which must be provably write-free.

## Open questions / decisions log

- Split into three items, or ship as one? Slice 1 is cheap and unblocks
  the most; slice 3 needs care to guarantee it writes nothing. Leaning
  three items with `blocked-by` on the shared renderer, decided when
  slice 1 is scoped.
- Does `view='report'` belong on `se` or on `structure`? The findings are
  the structure's, but the question is always asked about a block.
