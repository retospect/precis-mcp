---
status: draft
title: The anneal steers by a courtyard it no longer enforces
prio: medium
---

# The anneal steers by a courtyard it no longer enforces

**Items 1-6 of the original courtyard-polygon item all shipped
(2026-08-30).** A part's courtyard is now one shape —
`ir.instance_courtyard_polygon`, the convex hull of its own pad outlines
offset outward — and the placer reserves it, `courtyard_overlap` and
`outline_containment` check it, and `silk.py` draws it. The three differ
only in the CLEARANCE they offset by, which is a real difference (router
escape room vs. fab ink clearance), not drift. `silk_missing` went 27 → 0
on the reference board and 61 → 0 on the 40mm fixture. Surviving detail
lives in the owning docstrings: `ir.COURTYARD_CLEARANCE_MM` (the measured
sweep), `geom.convex_polygons_overlap` (the one SAT primitive),
`landpattern.place_points` (the one affine path),
`handlers/pcb.py::_drc_geometry` (why three clearances, one shape).

What is left is one real disagreement and two smaller notes.

## The disagreement

`cost.courtyard_overlap_pair_term` grades placements by a flat
`COURTYARD_MIN_SEPARATION_MM` (2.0mm) **centre-distance** between two
circles — `drc.DEFAULT_COURTYARD_RADIUS_MM * 2`, a constant that predates
every real courtyard in this subsystem. Legality is now polygon overlap.
So the annealer descends a slope defined by one courtyard while
`_placement_is_legal` enforces a different one.

This is not currently producing wrong boards: the graded term steers away
from tight packing generally, and legality catches the categorical
violation it cannot price. But the two answer the same question with
different geometry, which is the exact shape of defect the courtyard work
just spent two ships removing everywhere else.

**Open decision:** move the graded term to polygon overlap DEPTH (the
`drc._overlap_depth_mm` reading, already written and tested), or keep the
circle term and state in `cost.py` that it is a deliberately coarse
STEERING heuristic that legality overrides. Either is defensible; leaving
it undocumented is not.

Note the coupling if the term moves: `optimize._courtyard_cell` buckets
instances on a uniform grid whose cell size EQUALS
`COURTYARD_MIN_SEPARATION_MM`, and `_courtyard_candidates_near`'s claim to
be exact rather than approximate rests on that equality. A polygon-depth
term has no single interaction radius, so the grid would need its cell
size derived from `courtyard_bound_radius_mm`'s maximum instead.

## The fixtures pin one lottery draw

Both acceptance fixtures assert exact DRC counts at ONE placement seed
over a simulated anneal. That is why a correctness fix can read as a
regression, and it is the root cause of the two contradictory clearance
sweeps recorded in `ir.COURTYARD_CLEARANCE_MM`: the same four values
ranked differently before and after `_gen_rotate` gained a legality gate,
because the search itself changed.

The consequence is concrete. `COURTYARD_CLEARANCE_MM` is a point inside a
working range (~0.25-0.40) chosen because it leaves both fixtures at their
recorded baselines — a pin, not an optimum — and nothing stops the next
engine change from moving the fixtures again and inviting another round of
constant-nudging.

**The fix is to make the fixtures assert over N seeds** (median, or
worst-case, or "no seed exceeds K"), so a baseline measures the engine
rather than one draw. The sweep harness for this is trivial — monkeypatch
`_SEED` and the ratchets, loop — and both sweeps in this item's history
were produced that way. Until then, treat a single-seed fixture
regression as a prompt to measure across seeds, never as a prompt to
adjust a constant.

## Filed, not fixed: the silk obstacle list has no broad phase

`build_silk`'s per-instance loop tests every candidate against
`side_obstacles`, a plain list that grows by ~3 entries per part already
processed. That is O(parts^2) and was so before this work; the refdes ring
sweep multiplied its constant by ~6 (6 candidate spots → 37), and a
blocked pin-1 adds up to 24 more dot candidates.

No new complexity class, and unmeasurable on a 29-part board. But a
few-hundred-part board would feel it. The fix when needed is a spatial
index (grid bucket keyed on the board bbox) behind the same
`side_obstacles` interface, **not** fewer candidates: the candidate count
is what closed 24 findings and is pinned by measurement.
`silk._near_obstacles` is already the broad-phase shape this would
generalize — it exists for `_clip_polyline` only.

## Filed, not fixed: two capability figures at medium confidence

`soldermask_expansion_mm` (0.05) and `silk_to_mask_clearance_mm` (0.15
jlc_min / 0.25 house) were added to `pcb_capabilities.json` on 2026-08-30
at `medium` confidence and have not been re-verified against a live JLCPCB
page. Every other figure in that row was checked on 2026-08-27. They size
every courtyard on every board, so they deserve the same standard.

## Open questions / decisions log

- **CLOSED (2026-08-30) — does the 40mm fixture still place?** Yes, and
  its DRC went to zero errors of every rule.
- **CLOSED (2026-08-30) — keep a broad phase?** Yes, derived from the
  polygon (`ir.courtyard_bound_radius_mm`, a rotation-invariant
  circumscribed radius). The vectorized `d2 < (r_i + r_j)^2` sweep that
  used to BE the legality test is now the filter in front of the exact
  SAT, so the common "nowhere near each other" answer still costs one
  numpy comparison over the whole board. Full SAT on all 406 pairs of a
  29-part board would not have been affordable in Python.
- **CLOSED (2026-08-30) — batched numpy SAT?** Not built, deliberately.
  The original item asked for one; sharing a SINGLE overlap primitive with
  `silk.py` and `drc.py` matters more than batching, and after the broad
  phase there are only a handful of pairs left to test. Revisit only if
  profiling asks.
- **OPEN — the graded cost term** (above).
- **DECIDED (user, 2026-08-29): full arbitrary/convex shape, not a bigger
  circle.** Radii are not a source of truth.

## Explicitly NOT in scope

- **Non-convex courtyards / exact package outlines.** Every consumer's
  overlap primitive is SAT, which is exact for convex shapes and silently
  wrong for concave ones.
- **Mirroring in the placer.** `PcbIR` carries no per-instance board side,
  so `OptimizeEngine` has always been side-agnostic (a circle was
  mirror-invariant, which is why it never came up). An asymmetric
  bottom-side part reserves its unmirrored twin — same extent, reflected.
  Fixing that needs a side on the IR, not a change in the courtyard code.
- **A `soldermask_dam_mm` DRC rule.** The field is genuinely dead
  (allow-listed in `tests/test_pcb_dead_exports.py`).
