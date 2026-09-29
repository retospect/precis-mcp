---
status: draft
title: Synthesized vias use a hardcoded PAD_LAYER for their span and are never tested against pads
prio: normal
---

# Via geometry ignores which side the pads are on, and never checks pads at all

## Motivation / why

Two defects in the same function family, read out of the code on
2026-09-29 while investigating a route of the EWOD dogfood board
`pb345846` with all four layers open (job 456172, worker code
`a7cc256f`, DRC run `9575e411`).

> **⚠ THIS ITEM HAS NO EVIDENCE FROM A REAL BOARD. Both defects below
> rest entirely on reading the code against its own docstring.**
>
> It was first written attributing the 2026-09-29 run's 14
> `via_pad_keepout` errors to router-synthesized vias. **That was
> checked and is wrong.** The decisive via — the one the DRC named at
> `(7.4495, -6.0505)` on net `ARR1_R0C6` — is `pcb_fixed_copper` row
> 125, `generator_name="ARR1"`: an AUTHORED plaza via, not router
> output. That board carries 54 authored vias against 45 realized ones,
> and the collision is with `ARR1_SINK_0`'s lands because the sink is
> placed under the electrode array. Those errors belong to
> `pcb-placement-must-be-valid-before-routing`.
>
> Priority dropped high -> normal accordingly: the two code defects may
> well be real, but nothing observed on a board demonstrates them, so
> **the first step here is a reproduction, not a fix.** Construct a board
> with a bottom-mounted part whose track needs a layer transition and
> show that the via span or count is wrong. If that cannot be
> constructed, the defects are theoretical and this item should be
> closed rather than fixed.

### A. The fourth `PAD_LAYER` hardcode

`realize.py::_vias_for_track` computes a via's layer span as

```python
layer_lo, layer_hi = min(PAD_LAYER, track.layer), max(PAD_LAYER, track.layer)
```

and `rules.py::implied_via_count` decides a track needs no via at all with

```python
if layer in (UNSET_LAYER, PAD_LAYER):
```

Both treat `PAD_LAYER` (= 0, F.Cu) as "the layer this track's pads sit on".
`_vias_for_track`'s own docstring states the intent correctly — *"this
track's layer isn't the layer its own two pads sit on"* — so this is an
intent/code mismatch, not a deliberate simplification. For a
bottom-mounted instance the pads are on B.Cu, and the consequences invert:

- a track on F.Cu serving a bottom-side pad is judged "nothing to
  transition" and emits **zero** vias, leaving the net's copper in two
  disconnected pieces;
- a track on B.Cu serving that same pad gets a via span it does not need.

`realize._side_layer(ir, inst_id, layers)` is the established answer to
"which side does this instance mount on" and is already used correctly by
the NC-pin sentinel loop and by `_unclaimed_pad_claims` a few thousand
lines away. This is the same defect shape as the three `PAD_LAYER`
hardcodes already fixed in this module (`3cb0c927`, `7b747cc0`,
`169d69ee`) — treat `PAD_LAYER` in `realize.py`/`rules.py` as guilty
until proven innocent.

### B. Vias are never tested against pads

`_vias_for_track` synthesizes via groups *after* routing, spread along a
straight line at via pitch from each track endpoint, and clamps them only
against the board edge. Its own comment says so:

```python
# These vias never touch the routing grid, so nothing else
# keeps them off the board edge — see `_clamp_via_into_board`.
```

The maze grid's protections — the per-pin NC sentinel (`net = ir.n_nets +
pid`) and `_unclaimed_pad_claims`' third synthetic band — keep **tracks**
off pads. Nothing keeps these vias off pads. So the standing rule that no
wire routes through a pad *even if it is NC* holds at track granularity
and is silently unenforced at via granularity, which is the more damaging
case: a via is a drilled hole, so landing one on a solder land destroys
the joint rather than merely crowding it.

`view='drc'` catches it after the fact as `via_pad_keepout` — *"a via
drilled into a solder land starves the joint regardless of net"* — which
is the familiar shape this campaign keeps hitting: a constraint a pass
cannot see is one it never honours, reported one stage downstream as
someone else's error.

## Evidence

Route on `pb345846` with all four layers routable (F.Cu / In1.Cu plane=GND
routable / In2.Cu / B.Cu) and the escape class widened to all four:
**61/62 nets realized, 53/54 escapes**, up from 28 failed / 34 realized on
the default stackup.

DRC on that result — the header line is authoritative, the grouping below
is a single unaudited read of the paged output, so treat the per-rule
counts as indicative and re-derive them in any test that asserts on them
(see `probe-numbers-need-a-pinned-method`):

```
# DRC — run 9575e411 — 53 error(s), 262 warn(s)
```

| rule | errors |
| --- | --- |
| clearance | 30 (all on B.Cu) |
| via_pad_keepout | 14 |
| silk_missing | 7 |
| connectivity | 1 |
| unrouted | 1 |
| synthesized_footprint | 1 (pre-existing: U_TEMP has no real footprint) |

Representative rows:

```
error via_pad_keepout via[ARR1_R0C6] @ (7.4495, -6.0505)
  <-> ARR1_SINK_0/HVOUT60 (net ) on B.Cu -0.315
  via clears ARR1_SINK_0/HVOUT60 (net ) by -0.225mm, needs 0.090mm

error clearance via[ARR1_R0C6] <-> pad[] on B.Cu -0.090
  copper clearance 0.000mm < JLC min 0.090mm (4layer)
```

`ARR1_SINK_0` mounts on the **bottom**, which is why every one of these is
on B.Cu.

**CORRECTION (2026-09-29, same day) — SETTLED: these rows are not
evidence for defect B.** `drc.py::check_via_pad_keepout` exempts an
authored via only when `item["fixed"] and via_net == pad_net`, *both
non-empty*. The pads here are `ARR1_SINK_0/HVOUT60 (net )` — empty net —
so the exemption cannot fire and the rule reports AUTHORED plaza vias
exactly as it reports router-placed ones. A prod read then settled which
kind it was: the named via at `(7.4495, -6.0505)` is `pcb_fixed_copper`
row 125 with `generator_name="ARR1"` — authored. The design carries 54
authored vias and 45 realized ones.

So Reto's reading on inspecting the board was correct: these are plaza
vias colliding with a sink placed under the array, and they belong to
`pcb-placement-must-be-valid-before-routing`. The item originally read
them as router output because nobody checked the `fixed` flag.

**What the evidence does NOT establish.** The single `connectivity` error
(net `ARR1_R4C6`, copper in two pieces, witnesses on F.Cu and B.Cu) looks
like a missing inter-layer via and would be a clean witness for defect A —
but `ARR1_R4C6` is also the one net that failed `no_path`, so its copper is
expected to be in pieces regardless. It is not usable as proof. Defect A
rests on reading the code against its own docstring, not on that row.

## In scope

1. Replace `PAD_LAYER` in `_vias_for_track`'s span and in
   `implied_via_count`'s no-transition gate with the track's own pads'
   side via `_side_layer`. Both must change together — `implied_via_count`
   was hoisted out precisely so the optimizer's `via_count` cost term and
   the realizer could not drift, so fixing one alone reintroduces the
   drift it exists to prevent.
2. Test via placement against the same pad claims the maze grid already
   builds (NC-pin sentinels + `_unclaimed_pad_claims`), so a via group
   that would land on a land is moved or the track re-routed. The claims
   already exist and are already on the correct layer; the gap is that via
   synthesis does not consult them.
3. A regression fixture asserting zero `via_pad_keepout` errors on a board
   with a bottom-mounted part.

## Explicitly NOT in scope

- Widening the dogfood fixture or changing the board's stackup to make the
  errors go away. The board's geometry is design data.
- The remaining `no_path` on `ARR1_R4C6` — that is the placement corridor
  item (`pcb-placer-starves-the-escape-corridor`).
- `silk_missing` (7) — a courtyard/silk rendering issue, unrelated.

## Target + blast radius

`src/precis/pcb/realize.py::_vias_for_track`,
`src/precis/pcb/rules.py::implied_via_count`, and
`src/precis/pcb/cost.py`'s `via_count` term which shares that predicate.
Changes via counts and via positions on **every** board, so it moves
numbers in existing tests; expect the escape yields recorded in
`pcb-escape-and-driver-chain.md` to move, and re-measure rather than
assuming the direction.
