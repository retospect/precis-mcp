---
status: open
prio: high
---

# A routed track can sit 0.040mm from a foreign pad, which the occupancy grid says is impossible

`tests/test_pcb_reference_end_to_end.py::test_esp32c3_reference_place_and_route_never_regresses_the_baseline[4]`
produces one `clearance` error — a **GND track against an RXD pad, both on
`F.Cu`, 0.040mm where the 4-layer process needs 0.090mm**. That test holds
copper DRC at a hard zero, and its own constant says why:

> Inter-net clearance is enforced by the occupancy grid in
> `precis.pcb.maze` — copper is claimed before it is drawn — so a nonzero
> value here is not "worse placement", it is a hole in the guarantee: some
> path reached the board without claiming its corridor. **Do not raise this
> to accommodate a measurement. Find the leak.**

This item is that leak. It is filed separately from whatever provoked it
because the guarantee is the defect: under a correct router a tighter
placement can only ever yield an **unrouted** net, never a clearance
violation, so any placement change can re-trigger this.

## How it surfaced, and why that is not the cause

It reproduces with the uncommitted `routing_area` cost term applied
(worktree `ewod-pcb`), and not without it — verified both ways by
reverting only `cost.py`/`optimize.py`/`tests/test_pcb_optimize.py`
(`5 passed` / `8 passed`) and restoring them (both fail). `routing_area`
changes the placement, so it changes which corridors the router is asked
for; it does not draw copper. Treating this as "the cost term needs
recalibrating" would tune one seed around a latent router defect and leave
the guarantee broken — which is what the constant above forbids.

The grid is not merely *approximately* right here either: `_realize_maze`
builds it with `clearance = max(config.clearance_mm, max per-net
clearance)` and `RealizeConfig.clearance_mm` defaults to **0.15mm**, so the
grid enforces a clearance well ABOVE the 0.090mm the DRC asks for. The
offending copper is outside what the grid would have permitted at all,
which means it was never planned on grid cells — or the pad it violates
was not the shape the grid protected.

## Eliminated (do not re-spend time here)

By experiment, each re-running the failing test:

- **The `seed_placement` mounting-hole slide** (the other half of the
  `routing_area` WIP) — `_SEED_HOLE_SLIDE_TRIES = 0`: still fails.
- **`realize._snap_to_pads`** (pulling a path's ends onto exact pad
  centres) — early-returned unchanged: still fails.
- **The taut elbow** inserted after that snap, the one piece of geometry
  the code marks "UNCHECKED against the grid on purpose": both insertion
  sites disabled: still fails.

By reading, with the evidence that settles each:

- **Plane fan-out / dog-bone stubs.** Every GND track on this board has
  `is_dogbone: False`, so GND is not plane-promoted here. (And
  `_drop_via_site` does ask the grid, for both the via annulus and the
  stub feeding it.)
- **An unstamped pad.** `_realize_maze`'s pad loop stamps every pin with a
  position, including NC pins — which get a per-pin sentinel net precisely
  so they cannot read as `FREE`.
- **Width mismatch between planning and drawing.** `grid.route` and
  `_tracks_from_path` are both handed `rules.track_width_mm`.
- **A rotated pad under-claimed.** `_pad_shape` emits a true rotated
  polygon for polygon pads and falls back to a *conservative enclosing
  circle* for an oblique rotation, so it over-claims rather than under.
- **The DRC mis-measuring an arc.** Every track here carries fillet arcs,
  but `drc._flatten_segments` expands them through `_arc_points` rather
  than chording them, and the fillet cuts the corner INWARD (checked by
  arithmetic on the first GND track: the pre-fillet corner vertex lies
  0.1875mm from the arc centre against a 0.1732mm radius, so the arc
  passes inside the corner). The checker looks right on this one.

## Where to look next

- **`_shove_vias`** — a post-route mutation of copper positions. Does it
  re-ask the grid, or move a via (and the track meeting it) after the
  corridor was proven?
- **The second `grid.route` call site** (`realize.py` ~3558, the escape /
  fan-out path, distinct from the main `~2202` one) — does the copper it
  emits get drawn at the width and position it planned?
- Identify the pad by refdes/pin first. The DRC finding's `objects` carry
  only net/ctype/layer; adding the pad's refdes/pin and both features'
  coordinates to a `clearance` finding would have made this a ten-minute
  job instead of an afternoon, and `check_via_pad_keepout` already does
  exactly that (gr451052). Worth doing for `check_clearance` regardless of
  this item.

## Acceptance

- The esp32c3 reference test passes at all five seeds **with the
  `routing_area` term applied**, with the fix in the router rather than in
  the cost function.
- A regression test that pins the mechanism directly, not through a
  full place+route: given a pad and a foreign-net route whose only corridor
  grazes it, the router returns UNROUTED (or a legal detour) and never
  copper inside the clearance.
- `BASELINE_DRC_ERRORS` stays 0. It is not to be raised.

test: `tests/test_pcb_reference_end_to_end.py::test_esp32c3_reference_place_and_route_never_regresses_the_baseline`
