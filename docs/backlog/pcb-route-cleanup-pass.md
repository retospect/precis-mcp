---
status: ready
title: "pcb: route clean-up post-pass — move another net's blocking via so a kinked track runs straight"
prio: medium
model: opus
pillar: 3d-design
---

# pcb route clean-up post-pass

Source: gr495397 (Reto 2026-10-17 ~16:25Z, first-hand; approved 2026-10-18
"gr495397 is good to go"). On `/pcb/usbc-attiny-dogfood-b` the U1 → C2
track kinks only to avoid a via on D1's net; moving that via would let the
track run straight.

No slice of [pcb-vector-router](pcb-vector-router.md) covers this: slice 7's
SCP cleanup runs under `router='topo'` only, and `_shove_vias`
(`RealizeConfig.shove_vias`) moves a route's OWN via while it is being
straightened, never another net's. This pass is standalone and does not
depend on vector-router slices 0–2.

## Where it runs

`realize._realize_maze`, after the winning reorder/negotiation pass is
chosen and before pours, stitching and diagnosis. The route job
(`workers/job_types/pcb_route.py`) supplies the DRC check; its post-route
legality gate and never-worse ratchet run unchanged on the result.

A move needs no incremental un-claim on the occupancy grid. The winning
pass records each segment's stamped `maze.RoutePath`. A trial builds a
fresh grid, claims the static copper (`_claim_static`), stamps every kept
segment's recorded path verbatim in the original commit order, then
routes the ripped nets' segments through the ordinary search, straighten
and commit code. Kept copper is therefore identical to the original.

## Trigger

A routed segment (not a plane fan-out stub) is a **victim** when either:

- its centreline has more than **N = 2** bends (a direction change between
  two consecutive spans on one layer; a layer change is not a bend), or
- its centreline length exceeds **R = 1.15 ×** the octilinear shortest
  distance between its two pins (`max(|dx|, |dy|) + (√2 − 1)·min(|dx|, |dy|)`),
  and the excess is at least 0.1 mm.

The octilinear distance is the yardstick, not the Euclidean chord, because
a 45° router cannot do better than it.

Victims are tried worst first, by excess length.

## Legal moves

For a victim, a **blocking via** is a via of another routed net (a via
from a plane fan-out, a stitch or a fixed-copper row is never moved) whose
keep-out (via radius + board clearance + victim half-width) meets the
victim's straight chord or either of its two octilinear doglegs. At most
**3** blocking vias per victim are tried, nearest the chord first.

A move rips the victim's net and the via's net, whole (a net is the unit
that moves, as in `_negotiate`), and re-routes them: victim net first,
then the via's net. Each re-routed net must:

- route every segment it routed before (no new unrouted segment);
- keep each new via within **1.0 mm** of one of that net's old vias;
- use only layers it used before.

## Accept rule

Measured over the two re-routed nets (every other net's copper is
unchanged, so this equals the board total):

- total centreline length, bend count and via count each hold or improve,
  and at least one improves (length by at least 0.01 mm); **and**
- DRC stays clean: the routed-copper rules the job's post-route gate runs
  (`session.routed_drc_findings`: clearance, trace width, annular ring,
  NPTH, via/pad and via/via keep-out, board edge) are run on the pre-pour
  copper, and no (rule, severity) count rises, errors and warns both.

A rejected move leaves the board exactly as it was. An accepted move
becomes the new baseline and the victim list is recomputed.

## Bounds

Per board (per `realize()` call): at most **40** moves tried and **20 s**
wall clock, checked before each trial. A route with no victim, or with
no DRC check supplied, costs nothing.

## Flag

`RealizeConfig.route_cleanup` (default `True`) and the `pcb_route` param
`cleanup` (boolean, default `true`). The pass runs only when the caller
supplies the DRC check (`realize(..., cleanup_check=...)`); the route job
does. A bare `realize()` call (tests, cost probes, the anneal's inner
loop) is unchanged.

## Report

`RealizeResult.cleanup` carries the counts; the route job summary gains
one line:

`cleanup: K moves kept, L tried, length -X mm, bends -Y`

and `vias -Z` when the via count fell. A pass that tried nothing prints no
line.

## Tests

- Synthetic board: one net's via sits on another net's straight line and
  forces a kink; the pass straightens it (bends and length fall, the via
  moves, DRC unchanged).
- A board where no move improves: the result is byte-identical to
  `route_cleanup=False`.

## Gates

Before and after, posted on gr495397: nano seeds 1/3/5/6/7/8
(`tests/test_pcb_nano_usb_pd_testboard.py` floors and DRC by rule), the
EWOD replay and taz15-blank. Floors are one-way: tighten if better; any
regression stops the slice.

## Check

The U1–C2 kink on `/pcb/usbc-attiny-dogfood-b` is gone after a re-route.
