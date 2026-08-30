---
status: draft
title: A pour can emit an island with none of its net's copper in it, and nothing removes it
prio: medium
---

# A pour can emit an island with none of its net's copper in it

## Motivation / why

On the 40mm reference fixture, `GND`'s pour comes out with one fragment
that contains **no via, no trace and no pad of GND at all**. It is a plate
of copper carrying a net name and nothing else.

This is NOT the same-layer split that `_try_plane_jumper` now closes — that
one was an electrical break, part of the net unable to reach the rest, and
`connectivity.net_islands` reported it. This island is invisible to that
checker, and correctly so: a pour is a JOINER in its model, never a node,
so an island with no primitives inside it joins nothing and is not a
component. The net really does reach every pin.

It is still a defect. An unreferenced plate is an antenna — it couples
capacitively to whatever runs past it with no defined potential to hold it
— and it wastes board area that the real pour could have used. Fabs and
tools call this "island removal" and most enable it by default.

**Measured, not inferred**: `realize.py::_stitch_one_net` now computes it
(`UnstitchedNet.bare_fragments`), and the 40mm fixture reports
`2 disconnected piece(s) ... 1 of those piece(s) hold NO via or trace of
this net at all`. The stitcher tries to jumper it like any other fragment
and fails — which is the right behaviour for a piece it cannot anchor.

## In scope

1. **Decide the policy, then apply it once.** Either drop a bare fragment
   from the emitted pours, or keep it and stitch it deliberately. Dropping
   is what other tools default to; keeping it means finding an anchor,
   which is what already failed.

2. **Do it after stitching, not during pouring.** `plane_pours` cannot know
   whether a fragment will end up with copper in it — `_stitch_plane_fragments`
   runs later and may put a jumper via inside one. The prune belongs after
   that pass, where `bare_fragments` is already known.

3. **Say what was removed.** Silently deleting copper from a board is the
   kind of change that is invisible until someone measures the finished
   gerbers. A `RealizeResult` field or a route-row note, not nothing.

## Explicitly NOT in scope

- **The `connectivity` rule.** It is right to stay quiet about this; making
  it report bare islands would give one alarm two meanings, which is what
  `UnstitchedNet.bare_fragments` was just added to stop.
- **`MIN_FRAGMENT_MM2`.** That filter drops fragments by AREA, and this
  island survived it — a big island with nothing in it is still bare. The
  two tests are independent, not a tuning of each other.

## Acceptance criteria

- The 40mm fixture's `pcb_routes` row for `GND` no longer carries a
  `floating copper —` note, and the reason is that the island is gone (or
  connected), not that the report was suppressed.
- A fixture with a deliberately bare island asserts the chosen policy
  directly on the emitted gerber model, not on the realizer's own count.
- No net loses copper that carried a via, trace or pad.

## Target + blast radius

`src/precis/pcb/realize.py` (`_realize_maze`'s pour/stitch ordering,
`_stitch_one_net`'s `bare_fragments`), possibly `planes.py`. Changes the
emitted copper of any board with a fragmented plane.

## Open questions

- **OPEN — drop, or keep and warn?** Dropping is the tool default and the
  smaller board. Keeping is the conservative option for a design where the
  island was deliberate (a shield plate a human poured on purpose), which
  this engine has no way to express today.
