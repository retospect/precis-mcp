---
status: draft
title: Placement validity must be a gate before routing starts, and it must refuse rather than warn
prio: high
---

# An invalid placement must not reach the router

## Motivation / why

Reto, 2026-09-29, after looking at a routed EWOD dogfood board
(`pb345846`, job 456172):

> There are severe clerance issues. Vias are inside the footprint of
> ARRY1_SINK_O. That should never go to routing because placement is not
> valid. So placement must be valid before routing starts (drc of "no
> vias in pad" "all components inside pcb" that class of stuff).

The board had `ARR1_SINK_0` placed directly under the `ARR1` electrode
array, so the generator's authored plaza vias land inside the sink's
footprint — on its solder lands. Routing ran anyway, produced 61/62
realized and 53 DRC errors, and two route jobs plus a DRC read were spent
interpreting numbers off a board that was never valid.

**Confirmed by a prod read, not inferred.** The via the DRC named —
`via[ARR1_R0C6] @ (7.4495, -6.0505) <-> ARR1_SINK_0/HVOUT60 (net ) on
B.Cu`, margin -0.315 — is `pcb_fixed_copper` row 125 with
`generator_name="ARR1"`: authored generator output, present before any
routing ran. The design holds 54 authored vias against 45 realized ones.
So all 14 `via_pad_keepout` errors in that run are the generator's plaza
fabric meeting a badly placed instance, and **every one of them was
knowable before the router started**. That is the whole argument for this
item in one row.

**Why this is its own item and outranks the routing-side fixes.** The
campaign already has two items about copper landing where it must not
(`pcb-via-geometry-ignores-pad-side-and-pads`) and about a corridor too
narrow to carry its nets (`pcb-placer-starves-the-escape-corridor`). Both
are reports made by a downstream pass about an upstream decision. This
item is the general form: a placement that cannot be legal should be
refused at placement time, so no routing result is ever produced from it
and no measurement taken on it means anything.

It is the same shape this campaign keeps finding — a constraint a pass
cannot see is one it never honours — but stated as a gate rather than as
another cost term.

## In scope

1. **A placement-validity check that runs before `op='route'` does any
   work.** The class of checks Reto named:
   - no via (authored fixed copper OR otherwise) inside a pad it does not
     belong to;
   - every component fully inside the board outline;
   - no footprint/courtyard overlapping another footprint.
2. **It must REFUSE, not warn.** A warning is what the current DRC
   already is, and it arrives after the expensive part. The value here is
   not detecting the problem, it is not spending a route on it.
3. **The refusal names the instances and the offending geometry**, in the
   same "fail legibly" style as the routing failures — e.g. which two
   instances overlap, which via sits in which pad.

## Explicitly NOT in scope

- Deciding where the sink *should* go. Whether `ARR1_SINK_0` belongs under
  the array is design data. Reto has said he wants it moved out; that is a
  design edit, and this item is only about refusing the invalid state.

  **Resolved 2026-09-29, and it was not a board edit.** The sink's pose was
  never authored by hand: `generators.py` emitted every sink at the centroid
  of its own electrode share *and* hardcoded `fixed='both'`, locking the
  placer out. Reto's ruling — "placement should always be done with the
  placer; sometimes we keep a thing fixed (nuts, screw holes, alignment pins,
  connectors) but most of it should move" — makes this a generator change,
  shipped as expansion version 3 with `sink_grid.fixed` as the opt-in lock.
  A board-level `op='move'` would have been the wrong instrument anyway: the
  regenerate retires and reinserts the instance, so the unpin has to come
  from the generator or it is erased. The ARRAY stays locked, because its
  authored plaza copper lives in `pcb_fixed_copper` in the same absolute
  frame and would desync if the instance moved.
- Auto-repairing a bad placement. Refusing is the deliverable.
- The via-geometry defects (`pcb-via-geometry-ignores-pad-side-and-pads`),
  which are about how vias are synthesized, not about whether placement
  was legal.

## Relationship to the existing pre-route DRC gate item

`pcb-escape-and-driver-chain.md` item 6 already carries a pre-route DRC
gate (gr451274a). This is that item's motivating case and sharpens it in
one way worth keeping: the gate's job is to make an invalid board
**unroutable**, not to annotate it.

## Acceptance criteria

- A board with an instance whose footprint overlaps another's, or with an
  authored via inside a foreign pad, is refused by `op='route'` before any
  anneal or maze work happens, naming the participants.
- The `pb345846` sink-under-array case is the regression fixture.
- Escape yield is NOT a criterion. A gate that refuses a board produces no
  yield at all, which is the point.

## Open questions / decisions log

- **Generative cause now has its own item**, filed 2026-09-29:
  `pcb-placer-obstacle-set-is-mounting-holes-only`. The placer's only
  geometric obstacle is a mounting hole — authored `fixed_copper` is
  invisible to `optimize.py`/`cost.py` entirely. Refusing an invalid
  placement (this item) and giving the placer the force that would avoid
  one (that item) are both wanted; a gate with no cost term refuses boards
  it could have placed.
- **OPEN — is "no via in a pad" checkable at placement time in general?**
  Authored fixed copper yes: its geometry exists before routing. Router
  vias do not exist yet, so for those this gate can only check that the
  placement leaves somewhere legal to put them, which is closer to the
  corridor-capacity question in
  `pcb-placer-starves-the-escape-corridor` than to a geometric test.
- **OPEN — escape hatch?** A board someone intends to finish by hand may
  legitimately want to route a placement this gate rejects. Leaning: no
  silent bypass; an explicit opt-in argument if one is ever needed.
