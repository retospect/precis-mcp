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

The graded-term disagreement and the single-seed fixtures closed
2026-08-31 (ledger below). What is left: the two filed-not-fixed notes,
the pin-1-dot suppression decision, and whatever residuals the first
honest multi-seed measurement surfaced (tracked in their own items).

## CLOSED (2026-08-31) — the disagreement

`cost.courtyard_overlap_pair_term` no longer grades a flat 2.0mm
centre-distance circle: it reads the SAME courtyard polygons legality and
DRC test, via `geom.convex_polygons_signed_separation` (the graded
companion of the boolean SAT — exact MTV depth when overlapping, exact
zero at contact, an axis-gap lower bound on clearance when disjoint).

**Not overlap DEPTH, deliberately** (the open decision's first option, as
literally written, was a trap): legality already forbids polygon overlap
for every generated move, so a depth-only term would be identically zero
on every reachable state — dead code, cost.py's own move-reachability
ban. The live quantity on legal states is the CLEARANCE between the
polygons, so the term grades the shortfall of signed separation below one
routing corridor (`config.default_pitch_mm`): 0.0 at a full corridor,
1.0 (at budget) exactly at contact — the legality/DRC line — and
`1 + depth/corridor` past it (seed/fixed poses only). The predicted grid
coupling was real and handled: `_courtyard_cell_mm` is now derived from
`2*max(courtyard_bound_radius_mm) + corridor`, floored at the old
constant. Bonus: the term left `_NOT_MOVE_REACHABLE` — the old circle
term was itself already dead on engine-generated states (legality's old
circle floor coincided with it), the relaxation is not.

## CLOSED (2026-08-31) — the fixtures pin one lottery draw

Both acceptance fixtures now parametrize over seeds 1-5 and hold every
baseline on EVERY seed (worst-case-over-seeds). The first multi-seed run
on the pre-change engine proved the point better than the argument did:
seed 3 of the natural-size reference board carried **1 copper clearance
error at 0.000mm plus a via_pad_keepout error** — a real hole in the
occupancy-grid guarantee that seed-1 pinning had been hiding — and seeds
3/4 of the 40mm stress board carried unstitched-plane connectivity
splits. Single-seed baselines were not conservative, they were blind.
Residuals from the multi-seed measurement are tracked below /
in their own items, not by raising baselines.

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
- **CLOSED (2026-08-31) — the graded cost term** (above: a separation-
  shortfall relaxation over the shared polygons, not a depth term, which
  would have been dead code).
- **OPEN (2026-08-30) — a dropped courtyard still suppresses the pin-1
  DOT.** `silk.build_silk`'s "a pin-1 tick never survives alone" guard was
  written when the marker was a corner TICK, which is a cut of the
  courtyard outline and so genuinely meaningless without it. The dot is
  not: it sits OUTSIDE the courtyard beside pin 1's own land, so it still
  points at real geometry when the outline is gone. The guard now
  suppresses both, which costs a legible marker every time a courtyard
  drops — 2 of the 3 remaining `silk_missing` findings on the 40mm fixture
  were of exactly this shape before the ordering fix. Not widened on a
  hunch: decide whether a lone dot reads as a pin-1 marker or as stray
  ink, ideally by rendering one, before changing the rule.
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
