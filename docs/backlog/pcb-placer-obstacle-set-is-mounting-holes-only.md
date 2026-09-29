---
status: draft
title: The placer's only geometric obstacle is a mounting hole — authored copper is invisible to it (the side-blind courtyard half landed 2026-09-29)
prio: high
---

# The placer cannot see most of what it must avoid

## Motivation / why

Found 2026-09-29 while acting on Reto's ruling that placement belongs to
the placer (`pcb-placement-must-be-valid-before-routing` is the same
campaign's refuse-side of this). Two defects in one pass, read out of the
code, each independently checkable.

### A. Authored copper is invisible to the placer

`optimize.py`'s only non-instance geometric keepout is the mounting hole:
`_hole_keepout_radius_mm` / `_hole_polygon`, folded into
`courtyard_overlap` via `_refresh_courtyard`. The string `fixed_copper`
does not appear in `optimize.py` or `cost.py` at all, and the only `via`
in either is the `via_count` **money** term (`cost.py::_via_count`,
`$0.02`/hole) — a fab-cost scalar, not a position.

So a generator's authored plaza vias, which exist in `pcb_fixed_copper`
before any placement runs, exert zero force on where an instance lands.
That is the generative cause of the `pb345846` case: `ARR1_SINK_0`'s
solder lands sat on the ARR1 array's own plaza vias, 14 `via_pad_keepout`
errors, every one knowable before the router started.

**Freeing the sink does not fix this.** Generator version 3 stopped
emitting `fixed='both'` on sinks so the placer owns the position — but a
placer that cannot see a via can still put a pad on one. It will simply
do so somewhere else.

### B. `courtyard_overlap` is side-blind — **FIXED 2026-09-29, A is what remains**

`inst_bottom` — the IR's per-instance board side, `ir.py::from_graph` —
is read by `ratsnest.py`, `silk.py`, `realize.py`, `rules.py`, `ir.py`,
`generators.py` and `padplace.py`. It is **not** read by `optimize.py` or
`cost.py`. So `courtyard_overlap_pair_term` and
`_placement_is_legal` treat a bottom-side instance and a top-side
instance as bodily overlapping when their courtyards intersect in XY,
regardless of the board being between them.

Two consequences, opposite in sign:

- A legitimate bottom-side part underneath a top-side part is currently
  **unplaceable** — the normal double-sided-assembly case.
- On `pb345846` this accidentally produces the desired outcome: with the
  sink unlocked, side-blind overlap pressure pushes it off the array.
  **Do not read the resulting placement as evidence that the placer
  understands the constraint.** It is the right move for the wrong
  reason, and it will reverse the day B is fixed.

**Fix, and what it measured afterwards.** `_placement_is_legal` and
`courtyard_overlap_pair_term` now both skip a pair whose `ir.inst_bottom`
differ — the same rule `drc.check_courtyard_overlap` already applied
through its `bottom_by_refdes` map. Mounting holes are exempt from the
exemption: a hole goes through the board. Re-running the probe on the
same fixture, with no other change:

| quantity | before | after |
| --- | --- | --- |
| `ARR1_SINK_0` moved, sink-alone-unlocked arm | 0.0 mm | **10.05 mm** |
| instances that moved, that arm | 0 of 8 | 1 of 8 |
| `ARR1_SINK_0` moved, all-but-`ARR1` arm | 0.0 mm | 9.79 mm |

The trap is gone: the sink has legal positions and the annealer reaches
them. **Two things this does NOT establish.** `check_via_pad_keepout`
still returns 0 findings on this fixture (it returned 0 before too — the
fixture does not reproduce the prod symptom, see below), so defect A is
untested by it. And the mechanism probe's `born_illegal: true` is now
measuring a quantity legality no longer consults; it is kept as the
record of the pre-fix state, not as a live check.

**New, and worth a decision rather than a silent accept:** nothing in the
cost function rewards a sink for staying under the electrodes it drives.
Freed, it wandered 10 mm. That may be right (shorter nets elsewhere) or
may be a missing term; the generator's centroid is a seed, and a seed the
placer is free to abandon entirely is a different thing from one it
refines. Measure the routed result before adding a term.

The generator module docstring's claim that "a sink placed directly under
the array is correctly read as a different physical side by
courtyard-overlap/clearance checks" is true of **DRC's**
`check_courtyard_overlap`, and false of the placer's cost term of the
same name. Two passes, same name, different side-awareness. **As of
2026-09-29 the claim is true of both** — that divergence is what the fix
below closed, and it is the reason the docstring read as accurate for a
month while the placer was doing the opposite.

## Measured 2026-09-29 — the sink is STUCK, not merely unlocked

Generator v3 unlocks sinks. A throwaway probe on the `ewod-dogfood-1`
fixture (seed fresh, `op='place'` drained in-proc) measured what the
placer then does:

| arm | result |
| --- | --- |
| sink alone unlocked, everything else `fixed='both'` | `fixed_after: null`, **moved 0.0 mm**, 0 of 8 instances moved |
| everything but `ARR1` unlocked | 6 of 6 others moved (`J_SERIAL` 23.24 mm, `J_HV` 7.51, `U_TEMP` 6.38, `POGO1` 2.86, `J_INSTR` 2.38, `R_BLEED` 0.26); **`ARR1_SINK_0` moved 0.0 mm** |

The second arm is the discriminator: the placer is demonstrably working,
and the sink specifically cannot move. **So unlocking a sink is necessary
but NOT sufficient — this item blocks the EWOD placement fix rather than
following it.**

### Mechanism — measured, not inferred

`_placement_is_legal` requires `dist(i, j) >= r_i + r_j`, where `r` is
each part's circumscribed land-pattern radius (`_keepout_r`). Measured on
the fixture with the same `instance_courtyard_polygons(..., clearance_mm=
COURTYARD_CLEARANCE_MM, fallback_half_extent_mm=COURTYARD_MIN_SEPARATION_MM
/ 2)` call `optimize.py` uses for its own radii:

| quantity | mm |
| --- | --- |
| `r` ARR1 | 11.898 |
| `r` ARR1_SINK_0 | 13.650 |
| **required separation** | **25.548** |
| **actual separation** | **0.093** |
| shortfall | 25.455 |

**The sink is born 25.46 mm inside the separation legality demands.** It
starts illegal, and no generated move is accepted unless it lands ≥25.55 mm
away, so the annealer cannot walk it out — which is exactly the measured
0.0 mm against six freely-moving neighbours.

Note what this says about defect B: the legality test is not merely
side-blind, it is a CIRCUMSCRIBED-CIRCLE test. An 8x8 electrode array and
a bottom-side sink are being separated as if both were discs. That is the
same sound-vs-unsound approximation class this campaign already hit when
three checks measured pads with circles — a bounding volume licenses
conservative REJECTION only, and here the conservative rejection is
rejecting the entire legal region.

So the fix ordering in this item needs one correction: making
`courtyard_overlap` side-aware is not cosmetic here, it is what makes any
position under the array legal at all. Until then, a sink under an array
is unplaceable BY CONSTRUCTION, and generator v3's unlock cannot help.

Also measured, and worth a separate look: `check_via_pad_keepout` returned
**0 findings** on this placed fixture while the prod board's routed twin
reported 14. The probe placed but did not route, and the fixture is
`ewod-dogfood-1` against prod's `ewod-dogfood-2`, so this is not
necessarily a contradiction — but it means the fixture does not currently
reproduce the prod symptom, and
`pcb-placement-must-be-valid-before-routing`'s "the `pb345846`
sink-under-array case is the regression fixture" needs a fixture that
actually shows it.

## In scope

1. Put authored `pcb_fixed_copper` (vias especially — a drilled hole
   destroys a joint, it does not merely crowd it) into the placer's
   obstacle set, reusing the polygon machinery
   `_hole_polygon`/`_refresh_courtyard` already has rather than a second
   representation.
2. ~~Make `courtyard_overlap` side-aware by reading `ir.inst_bottom`, in
   both the graded cost term and `_placement_is_legal`, so the two agree
   with DRC's rule of the same name.~~ **DONE 2026-09-29** — three tests
   in `tests/test_pcb_optimize.py`, each with a same-side discriminator
   arm placing two parts at the identical coordinate so only
   `inst_bottom` can separate them.
3. A regression fixture for defect A: a board with an authored via where
   an instance would otherwise land. (The bottom-under-top fixture is
   the `_two_part_ir` helper added with item 2.)

## Explicitly NOT in scope

- The refuse-before-route gate
  (`pcb-placement-must-be-valid-before-routing`). That item makes an
  invalid placement unroutable; this one makes it unlikely. Both are
  wanted — a gate with no cost term refuses boards it could have placed.
- The corridor-width term (`pcb-placer-starves-the-escape-corridor`).
  Same pass, same missing-representation shape, different producer.
- Via SPAN correctness (`pcb-via-geometry-ignores-pad-side-and-pads`),
  which is about how vias are synthesized, not about what the placer sees.

## Ordering — corrected 2026-09-29 by measurement

An earlier version of this section said fixing B before A would make
`pb345846` worse, because B's side-blindness was "accidentally" pushing
the sink off the array. **That was a prediction, and the probe refuted
it.** Side-blind overlap does not push the sink anywhere; it freezes the
sink in place, 25.46 mm inside the required separation, with no legal
move available. Nothing was pushing it off.

The corrected ordering is the opposite of what that paragraph said:

1. **B first.** Until `courtyard_overlap` and `_placement_is_legal` know
   which side an instance mounts on, a bottom-side part under a top-side
   part has no legal position at all, so no amount of obstacle awareness
   can place it anywhere.
2. **Then A.** Once positions under the array are legal, the placer needs
   to see the authored plaza vias or it will pick one of them.

A alone would leave the sink frozen. B alone would free it to move
without knowing what it must avoid. Both are needed; B unblocks.

**B landed 2026-09-29 and behaved as the ordering predicted** — the sink
went from 0.0 mm to 10.05 mm of movement. A is now the whole of this
item, and it is no longer blocked by anything.

## Target + blast radius

`src/precis/pcb/optimize.py` (`_refresh_courtyard`,
`_courtyard_candidates_near`, `_placement_is_legal`, `seed_placement`),
`src/precis/pcb/cost.py` (`courtyard_overlap_pair_term`). Moves placement
on every board with authored copper or a bottom-side part, so re-measure
rather than assuming direction — and per
`pcb-fixture-footprint-manufactures-the-wall`, a yield measured on the
synthetic fixture does not transfer.
