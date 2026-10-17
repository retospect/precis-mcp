---
status: draft
title: A pour can emit an island with none of its net's copper in it, and nothing ties it to its net
prio: medium
pillar: 3d-design
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

## Ruling (Reto, 2026-10-16)

**Stitch the island to its net; do not drop it.** The copper is already
there and it shields. Drop an island only when no via site can reach a
same-net plane, and report that drop. This replaces the earlier "drop, the
tool default" lean (survey: `/tmp/gripe-fleet/w6/pcb-stackup-plan.md` Q5;
umbrella item [pcb-stackup-regions](pcb-stackup-regions.md)).

## In scope

1. **Stitch each bare fragment with vias to a same-net plane.** At least one
   via per island; a large island gets more, spaced at the stage-1
   `stitch_pitch_mm` of `_stitch_plane_fragments`, so a big plate is tied
   down along its extent, not at one corner. A via site is legal when it lies inside the island, clears
   foreign copper, and its barrel lands in the same net's pour on another
   layer. Reuse stage 2's site search; a bare fragment is a fragment with
   zero anchors, so it gets the same targeted-via treatment, not a separate
   mechanism.

2. **Do it after stitching, not during pouring.** `plane_pours` cannot know
   whether a fragment will end up with copper in it — `_stitch_plane_fragments`
   runs later and may put a jumper via inside one. The island pass belongs
   after that pass, where `bare_fragments` is already known.

3. **Drop only what cannot be reached, and say so.** An island with no
   legal via site to a same-net plane is removed from the emitted pours.
   Each drop is recorded (net, layer, area, centroid) in a `RealizeResult`
   field and the route-row note — silently deleting copper is invisible
   until someone measures the finished gerbers. Stitched islands are
   counted too (islands stitched, vias added).

## Explicitly NOT in scope

- **The `connectivity` rule.** It is right to stay quiet about this; making
  it report bare islands would give one alarm two meanings, which is what
  `UnstitchedNet.bare_fragments` was just added to stop.
- **`MIN_FRAGMENT_MM2`.** That filter drops fragments by AREA, and this
  island survived it — a big island with nothing in it is still bare. The
  two tests are independent, not a tuning of each other.
- **Same-layer jumpers for islands.** `_try_plane_jumper` joins fragments
  that already carry net copper; an island needs a via down to a plane, not
  a trace over a spare layer.

## Acceptance criteria

- The 40mm fixture's `pcb_routes` row for `GND` no longer carries a
  `floating copper —` note, and the reason is that the island is stitched
  (it now holds at least one GND via that lands in a GND pour on another
  layer), not that the report was suppressed.
- A fixture with a deliberately bare island that has a reachable plane
  asserts, on the emitted gerber model, that the island copper is still
  present and holds a same-net via; a large island in the same fixture
  gets more than one via, spaced at the stitch pitch.
- A fixture where no via site reaches a same-net plane asserts the island
  is gone from the emitted copper and the drop is reported with net, layer
  and area.
- No net loses copper that carried a via, trace or pad.

Related evidence: gr492108. Plane nets that stay split into separate
components after stitching (taz15-blank PV+ 2, GND 5 when poured; nano V5
15), measured in
[pcb-stackup-regions](pcb-stackup-regions.md) slice 1's table. These are
pieces carrying copper, not bare islands, so this item or slice 4 needs
to say which one owns them.

## Measured 2026-10-17 (pour-stitch, gr492108)

Shipped: `_rescue_plane_pins` (a plane pin the fan-out found no drop site
for gets a routed trace, net width then fab-floor width, to the nearest
same-net drop via that touches the pour; pour re-carved), the jumper's
fab-floor width retry, and a named reason per bare piece in
`UnstitchedNet.message` (area, centroid, "no via site fits" vs "no legal
via or jumper"). Harness: seed 0/1 as in the stackup-regions slice 1 table.
`net_islands` components per plane net:

| board | routed/failed before → after | plane nets before → after |
|---|---|---|
| taz15-blank as stored | 66/13 → 67/12 | PV+ 2 → **1**, 12V 1 → 1 |
| taz15-blank + GND→B, 3V3→F | 64/15 → 64/15 | PV+ 2 → 2, GND 5 → 5, 3V3 1, 12V 1 |
| nano, GND→In1 + V5→In2 | 34/30 → 34/30 (DRC errors 75 → 73) | V5 15 → **4**, GND 33 → **2** |
| nano as tested (no pours) | 35/29, silk 36, warns 9 → same | — |
| esp32c3 seed 1 | 11/0 → 11/0 | GND 1, VCC3V3 1 |

Not done, still open:
- A rescue trace is refused on any layer another net pours, so on taz15 + GND/3V3
  (F.Cu 3V3, B.Cu GND, the only routable layers) PV+ stays 2: the
  unguarded version reached PV+ 1 and 65/14 but cut the foreign planes.
- taz15-blank PV+ keeps one bare 8.19 mm² In1 piece (`pour_quality`: 2
  fragments): a via site fits, but no legal via or jumper reaches the net.
  taz15 + GND/3V3: 3V3 is 12 bare F.Cu pieces, the largest 5190 mm², with
  no spare unpoured layer for a jumper.
- taz15 + GND/3V3 GND stays 5 components (F.Cu pads at (6.1, 94.6),
  (19.0, 43.8), (16.0, 43.8), (9.7, 9.9), (54.2, 20.0)); nano V5 4 and GND 2
  components (F.Cu pads; nearest same-net via 1.7–18 mm away). Not
  diagnosed past "the fan-out reports no blocked pin and the pad is still
  its own component".
- Bare islands are reported, not dropped (the task brief: report, do not
  silently delete); the drop-on-unreachable clause above is unimplemented.

## Target + blast radius

`src/precis/pcb/realize.py` (`_realize_maze`'s pour/stitch ordering,
`_stitch_plane_fragments`, `_stitch_one_net`'s `bare_fragments`), possibly
`planes.py`. Adds vias to any board with a bare plane fragment; removes
copper only where no stitch is possible.
