---
status: ready
title: a write-free dry-run for join, proven through the reading surfaces
pillar: 3d-design
prio: high
---

# A write-free dry-run for `join`, proven through the reading surfaces

## Motivation / why

`view='report'` exposes stored generate/join findings by direct design block,
optionally selected by label or uid with `args={'block': ...}`. It reuses the
block view's findings renderer without geometry loading or recomputation.
Missing records or malformed findings are unavailable, never a clean bill of
health. State overrides are rejected; template occurrences are not expanded.

`view='catalogue'` (SPEC §25.3) lists the catalogue edge rows a join can
see and the measured rows the gate withholds, then per join side the row
consulted, its `resolve_edge` label (`pinned z`, `exact z10`, `nearest
z12`), what governed the seam radius and leak threshold in force
(`explicit`, the label, or `table`), and which withheld row the gate kept
from that side. `compose` carries the label on
`composite.seam["catalogue"]`; the view reads it, never re-resolves.

What remains is the write path: today every probe `join` mints a
composite, which is how the dogfood permanently corrupted a design in
order to ask a question about it.

## In scope

**A dry-run for `join`** — the equivalent of `generate`'s
`fidelity="check"`: resolve, compose, report, mint nothing. The two
reading surfaces above are what proves it wrote nothing.

## Explicitly NOT in scope

- Changing what a join computes or records.
- Persisting findings in their own table. They are already in the
  structure's meta.
- A web/3D viewer surface. MCP views only.

## Acceptance criteria

- A join dry-run on a design leaves the design byte-identical — assert it.
- The dry-run's report and catalogue resolution match what `view='report'`
  and `view='catalogue'` would show for the same join once minted.
- The test helpers in `tests/test_se_join.py` that dig findings out of
  meta by hand can be deleted in favour of the view.

## Target + blast radius

`precis_se/atomic/join.py` for the dry-run path (the only write path
touched; it must be provably write-free), `precis_se/handler.py` only if
the dry-run needs a flag on the op surface.

## Open questions / decisions log

- **DECIDED 2026-09-30: three slices, in this file, shipped in order.**
  Slice 1 (`view='report'`) and slice 2 (`view='catalogue'`) have shipped;
  slice 3 (the dry-run) is the only one that touches a write path and
  goes last, now that there is a reading surface to prove it wrote
  nothing with.
- **DECIDED 2026-09-30: `view='report'` goes on `se`, addressed by
  block.** `view='catalogue'` follows the same addressing.
