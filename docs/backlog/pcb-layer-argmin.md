# pcb-layer-argmin — corridor-scored layer choice in assign_layers

Status: spec for the slice AFTER the corridor-visibility payload ships
(wires.json per-net per-layer obstruction share). Sequencing: router files
are ewod-pcb-thread-owned; `scripts/inflight` shows no live router editor,
and this slice is additive (no behavior change unless masks are passed).

## State

`realise.assign_layers` resolves crossings as a bounded local search whose
move cost is the lexicographic tuple `(live pairs, overflow, vias,
length)` (`flip_key`, realise.py). A run (the span between two cuts) may
sit on ANY layer its net's candidate set leaves open — inners included —
but the chooser sees only via count and length. Obstruction is invisible:
a net can be routed through a layer whose pad-block mask covers 40% of
its corridor while a 2%-blocked inner layer goes unused. Those nets then
die at via siting or emit ("no via site", "copper withdrawn: would
short") even though a feasible layer choice existed.

`topoplace.masks.corridor_score` now measures exactly this
(`(blocked_fraction, spine_crossings)` for a centerline thickened to a
width against one layer's mask set), and the route stage's wires.json
payload exposes per-net per-layer scores — the argmin inputs, visible
before the chooser consumes them.

## Slice

Additive, optional `masks: BoardMasks | None = None` on
`assign_layers` and `plan_route` (threaded from the caller; `None` =
today's behavior bit-for-bit).

1. **Mask cost of a runs/cuts state.** For each branch, for each run p
   on its candidate layer L(p): run polyline = the sub-span of
   `WirePath.points` covering run p's edges; width = the net's rule
   track width. `corridor_score(layers[L(p)], pts, w, rb_union=precomputed)`
   per run; the branch's mask cost is the length-weighted mean
   `blocked_fraction` plus `k *` total `spine_crossings`. Precompute the
   per-layer route_block unions once per `assign_layers` call (pose is
   fixed inside the call — the annealer moves poses between calls, so
   the mask build's seconds are paid once per call, amortized over all
   rounds).
2. **Move key.** Extend `flip_key`'s tuple with a fifth element,
   `mask_cost` of the moved branch under the proposed runs/cuts
   (0.0 when `masks is None`). Lexicographic order is unchanged in
   front, so masks only break ties the old search left equal — and
   prefer corridors the geometry actually leaves open when they do.
   `k` starts at 0.25 (one spine crossing ≈ a quarter of the corridor
   blocked; calibration measured on taz-leaf, reported below).
3. **Explicit non-goals for this slice.** No via_penalty term (lands
   with the via-bracket slice — it needs point-wise via_prevent
   containment, not the board-level free fraction). No re-thread cost
   change (re-threads regrow topology; scoring half-grown geometry
   against masks is a different slice). No scoring change when
   `masks is None` — synthetic fixtures and the ewod replay must run
   bit-identical.

## Measurement (taz-leaf local loop, next slug)

- nets realized / failed, via count, route wall time (must not regress
  beyond ~10% — the mask build is once-per-call).
- per-net: did the chooser pick the argmin layer per wires.json's own
  corridor block? Report agrees/conflicts.
- Exit: more nets realized than the pre-slice baseline at comparable
  wall time, or a measured, legible reason the mask cost misleads
  (recorded here).

## Risks

- **Own pads count as blocked** (found on local-55): corridor_score sees
  the layer's full route_block, which includes the scored net's OWN pad
  rings — a short net's corridor starts/ends inside its pads, so
  blocked_fraction saturates (S2_REF reads b1.00 on the very layer it was
  realized on). For argmin this is tolerable when comparing layers for
  the SAME net (own-pad contribution is roughly layer-local), but the
  wires.json display overstates obstruction, and cross-net comparisons
  are meaningless without the exclusion. Slice 1.5: exclude the scored
  net's own parts' rings from route_block before scoring (mask them out
  of the precomputed union).
- Mask build at pose costs seconds per `assign_layers` call; if the
  caller invokes it per anneal move (it does not today — pose is fixed
  per realise), this becomes the bottleneck and needs the cached-mask
  delta approximation (same answer as the placement-objective slice).
- `corridor_score` uses the committed pose's masks; inside the route
  shove loop the pose moves between iterations, and each iteration
  rebuilds anyway — consistent.
- k miscalibration: a crossings-heavy but unblocked layer could beat a
  blocked one. The wires.json corridor block makes this visible per net;
  tune k from data, not vibes.
