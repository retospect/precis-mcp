---
status: draft
title: A plane poured on ONE layer can fragment, and a via cannot unfragment it
prio: high
---

# A plane poured on ONE layer can fragment, and a via cannot unfragment it

## Motivation / why

`GND` on the 40mm reference fixture comes out as 3 disconnected pieces and
stays that way. This is not a tuning failure — it is unreachable by the
mechanism we have.

Measured on that board: GND is poured on **`F.Cu` only**, in 4 fragments
(hole counts 22 / 1 / 3 / 0). `_stitch_plane_fragments` bridges fragments by
placing a via, and **a via joins LAYERS, not lateral gaps on one layer**.
With a single poured sheet there is no other sheet to detour through, so no
number of vias can merge two `F.Cu` fragments. `_stitch_one_net` already
states the remedy and scopes it out: "a same-net jumper (a second via off an
extended stub, or a two-via/spare-layer trace)".

A plane in several pieces is a real manufacturing defect, not a cosmetic
one: the return-current path the plane exists to provide does not exist
between the pieces, and the checker is right to call it.

**Do not confuse this with the VCC3V3 split**, which looked identical and
was NOT this: that one was a single orphan drop via placed 10um outside the
board-edge inset, and it disappeared the moment the drop-via search got an
edge test. Same symptom, unrelated cause — which is the trap here.

## In scope

1. **Bridge two same-layer fragments with a jumper**: a via down from
   fragment A, a track on a layer with room, a via back up into fragment B.
   The router can already do each piece; what is missing is the pass that
   composes them.

2. **Choose the detour layer honestly.** The track needs clearance on
   whatever layer carries it, and that layer's own occupancy is the
   constraint. A jumper that shorts a third net is worse than the split it
   fixes.

3. **Refuse legibly when no jumper exists.** Keep the existing
   `UnstitchedNet` reporting for the case where no layer has room — this
   item must not turn an honest "could not" into a silent failure.

## Explicitly NOT in scope

- **Promoting GND to a second poured layer.** That would hide this board's
  symptom by changing the design, and leaves the engine defect in place for
  any single-plane board.
- **Re-pouring after stitching** to cut fresh antipads around the new vias
  (`_stitch_plane_fragments`'s own "known limitation" paragraph) — related
  ordering problem, separate item.
- **The `silk_missing` population** — `pcb-courtyard-polygon.md`.

## Acceptance criteria

- Two same-layer fragments of one net, with a free detour layer, come out
  as a single connected component; asserted through
  `connectivity.net_islands` (the independent checker), not through the
  stitcher's own piece count, which is the producer grading itself.
- A fixture with NO free detour layer still reports `UnstitchedNet` and
  does not place a jumper that violates clearance.
- The jumper's track and both vias appear in the emitted gerbers, not only
  in the connectivity model.
- `connectivity` on the 40mm fixture goes 1 -> 0 and the entry leaves
  `KNOWN_OPEN_DRC_ERRORS` in the same commit.

## Target + blast radius

`src/precis/pcb/realize.py` (`_stitch_one_net`, `_stitch_plane_fragments`),
possibly `maze.py` for the constrained point-to-point route.
Adds copper to any board with a fragmented single-layer plane.

## Open questions

- **OPEN — one jumper per fragment pair, or a spanning tree?** A net in 4
  pieces needs 3 jumpers; picking them by cheapest-first is a minimum
  spanning tree over fragment pairs, which is more machinery than a greedy
  pass but avoids placing jumpers that a later one makes redundant.
- **OPEN — does the detour track belong to the router or the stitcher?**
  Reusing `grid.route` keeps one routing implementation; a bespoke
  straight-line search is simpler but is a second router.
