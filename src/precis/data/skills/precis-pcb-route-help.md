---
id: precis-pcb-route-help
title: precis — place and route a pcb design (op='place'/op='route')
summary: run the in-house topological place+route engine over an existing pcb netlist — op='place'/op='route' enqueue worker jobs (never inline), congestion/planes read views, the rip-up loop, and which move classes are still inert. Covers autoplace, autoroute, sketch, topology, layer assignment, plane/pour assignment, congestion, rip-up.
answers:
  - how do I place and route a pcb board?
  - why does put(args={'op':'route'}) return a job id instead of a result?
  - a net failed to route — how do I fix it?
  - how do I assign a net to a plane layer?
  - which pcb move classes actually do anything right now?
applies-to: put(kind='pcb', args={'op': ...}); see also kind='pcb', kind='job'
status: active
tags: [design, troubleshooting]
kinds: [pcb, job]
---

# precis-pcb-route-help — place + route as enqueued jobs

Once a `pcb` design has a netlist (see [[precis-pcb-help]]), placement and
routing run through the in-house topological engine
(`precis.pcb.optimize` + `precis.pcb.realize`) — a joint annealer over
**placement + sketch** (side choices, layer assignment, plane role), then a
deterministic realizer that turns the settled sketch into copper. **Sketch
is canonical; copper is derived** — rip a net and only its geometry
re-generates, the rest of the board is untouched.

**Every heavy op is an enqueued worker job, never inline.** The optimizer
measures ~880 moves/s on a real board — minutes, not milliseconds — so
`put(args={'op':'place'|'route'})` always returns a **job id** immediately;
poll `get(kind='job', id='<id>')` or just re-check the design's views a
little later. This is the same thread-pool-starvation lesson every other
heavy-compute kind in this server follows (structure relax, cad discuss).

## Enqueue a place or route job

```python
put(kind="pcb", id="sensor-node", args={"op": "place", "iters": 2000, "seed": 0})
put(kind="pcb", id="sensor-node", args={"op": "route", "iters": 3000, "seed": 0})
```

- `op='place'` — placement-only anneal (translate/rotate/swap). Never
  touches the sketch (side choices, layer assignment, plane role).
- `op='route'` — the JOINT engine: placement **and** sketch move together,
  followed by a realize checkpoint that writes `pcb_routes` (the sketch)
  and `pcb_copper` (derived geometry). Also re-places, so running `route`
  alone is a complete place+route pass — you don't need to run `place`
  first, though pinning placement early with `fixed=` still helps the
  anneal converge faster.
- Both accept `iters` (anneal step budget) and `seed` (RNG seed, for
  reproducibility). `fixed='xy'|'rot'|'both'` on a component (set at
  authoring time, or via `op='move'` below) is respected by both — a
  locked instance is never moved, guaranteed at the database write
  boundary, not just by the optimizer's own move generators.
- **Idempotent per (design, op, content-hash).** A re-submit against
  *unchanged* netlist/placement state and the same `iters`/`seed`
  collapses onto the in-flight/prior job instead of minting a duplicate —
  edit something (move a part, add a net class) and the hash changes, so a
  genuinely new submit always gets a fresh job.
- `args={'autoplace': {...}}` is a **deprecated alias** for `op='place'`
  (same job, same params) — carries a deprecation note in its response;
  will be removed in a future release. Use `op='place'` directly.

After a route job lands, check the result:

```python
get(kind="pcb", id="sensor-node", view="route-status")  # per-net: unrouted|sketched|realized|failed
get(kind="pcb", id="sensor-node", view="congestion")     # the run's over-capacity-gap warnings
```

A net only ever reads `'realized'` when it is **actually clean** — no
residual same-layer crossing, no over-capacity gap. A `'failed'` net's
`pcb_routes.fail` names the blocking participants (the same "fail legibly"
discipline as everywhere else in this kind): a same-layer crossing names
the other net it crosses; a congestion failure names the gap size, its
capacity, and how many strands wanted through it.

## The rip-up loop

When a net fails, the lever is **rip → steer → re-route**, not hand-editing
geometry:

```python
put(kind="pcb", id="s", args={"op": "rip", "net": "I2C_SCL"})
put(
    kind="pcb", id="s",
    args={"op": "pin_side", "net": "I2C_SCL", "a": "U1.SCL", "b": "R1.1", "side": 1},
)
put(kind="pcb", id="s", args={"op": "route"})
```

- `op='rip'` clears one net's persisted sketch (topology, layer
  assignment) and drops its realized copper — every other net's geometry
  is untouched (the derived-cache discipline). A net with nothing to rip
  (already unrouted) returns a no-op response rather than an error.
- `op='pin_side'` records which side of an obstacle one segment should
  take, keyed by its two endpoint pins (`'REFDES.PIN'`) — durable across
  the next route job's IR rebuild. **Caveat, stated plainly (see "Inert
  move classes" below): this SEEDS the side for the next anneal, it does
  not hard-lock it.** `SIDE_FLIP` is cost-neutral in the current cost
  function, so a zero-cost-delta move is always accepted — a later random
  `SIDE_FLIP` touching the same segment during that SAME route run can
  still change it. Pinning narrows the odds (the anneal starts there
  instead of an arbitrary order-of-construction default); it is not yet a
  guarantee. A real per-segment lock (mirroring `fixed=` on instances) is
  a known gap, not shipped this slice.

## Assign a plane

```python
put(kind="pcb", id="s", args={"op": "plane_net", "layer": "In1.Cu", "net": "GND"})
get(kind="pcb", id="s", view="planes")  # see what's assigned
```

`layer` must name a layer in the board's stackup (`get(id=s)`'s TOC shows
it, e.g. `4 layers: F.Cu/In1.Cu(GND)/In2.Cu/B.Cu`); an unknown name is
rejected with the valid options. A plane-assigned net's pins **dog-bone
fan out** (a short stub off the pad, no via-in-pad) instead of routing
point-to-point — this is real and takes effect on the next `op='route'`
run (the assignment is re-applied onto the freshly-built IR every time,
same as the pinned sketch). **Never route ground/power beyond the
dog-bone fanout** is the house policy this encodes; a routed trace on a
plane-served net is an anomaly, not a normal outcome.

## Move, lock, and net-class edits

```python
put(kind="pcb", id="s", args={"op": "move", "refdes": "U1", "x": 10.0, "y": 5.0, "rot": 90})
put(kind="pcb", id="s", args={"op": "move", "refdes": "J1", "fixed": "xy"})   # lock in place
put(kind="pcb", id="s", args={"op": "move", "refdes": "J1", "fixed": None})  # unlock
put(kind="pcb", id="s", args={"op": "class_rules", "name": "i2c", "rules": {"clearance_mm": 0.2}})
```

`op='move'` is the one op allowed to reposition a **locked** instance —
unlike the optimizer's own write-back (which never touches a `fixed`
instance, enforced in SQL), this IS the authorized edit path. Any of
`x`/`y`/`rot`/`fixed` may be given; omit `fixed` to leave the lock alone,
pass `fixed=None` to explicitly clear it.

`op='class_rules'` upserts one net class's rules (`{name, rules}` — same
shape as `put(args={'net_classes': {...}})` at design-authoring time, just
scoped to one class). **Honest limit:** the stored rules are not yet READ
by the realizer or the optimizer — no term consumes `clearance_mm` /
`track_width_mm` from here yet. Storing them is real (they round-trip and
appear in the design TOC); using them to size traces or set clearance is
a documented gap, not a current effect.

**Net-class `layers` IS consumed (Rulings 2026-09-19 item 7, gr347037).**
A class's `rules` may name `"layers": ["B.Cu", ...]` (stackup layer names)
— every net tagged with that class routes ONLY on the intersection of
those names with the board's own routable signal layers
(`precis.pcb.realize._net_class_layers`), never silently widened back to
the full set. A net whose lock resolves to nothing routable (a name
absent from the stackup, or one with no routable member at all) fails
with `UnroutedReason.kind == "layer_lock"`, naming the class and the
offending layer(s), rather than routing anyway. This is the one net-class
key the realizer reads today; `clearance_mm`/`track_width_mm` remain the
documented gap above.

## Inert (or conditional) move classes — say so plainly

The joint optimizer's move set is documented in full in
`precis.pcb.optimize`; a few are worth calling out here because their
inertness — or, for `PIN_SWAP`, its data-dependent conditionality — is
easy to miss from the tool surface alone:

- **`SIDE_FLIP` has no cost effect.** The `crossings` term (the only term
  that could plausibly respond to a side choice) is a straight-line sweep
  at component-centroid granularity — it is structurally blind to which
  side of an obstacle a connection routes on, the same way it is blind to
  `inst_rot`. The move still runs (its dirty-cascade bookkeeping is
  exercised so the plumbing is ready), it just never changes `total()`.
  This is why `op='pin_side'` is a seed, not a lock (see above).
- **`PIN_SWAP` now fires — declare `pin_swap_groups` on a component
  (Rulings 2026-09-19 item 6, gr347037).** A `put(kind='pcb')` component
  dict (hand-authored, or emitted by a generator — `ewod_pad_array`'s own
  sink instances do this) may carry a `"pin_swap_groups"` key: a list of
  admissible sets, each a list of that SAME component's own pin names
  that are freely interchangeable (no partial-exclusion syntax yet — a
  set names every candidate pin, nothing more). It rides `pcb_components
  .meta` and round-trips through `Store.pcb_graph`. `op='route'`'s job
  (`pcb_route._resolve_pin_swap_groups`) resolves every declared set into
  a real `precis.pcb.pinswap.PinSwapGroup` — pin ids off the instance's
  own `PcbIR.pin_label`, real per-pin offsets off the SAME cached
  footprint pads the realizer uses — and feeds it to `OptimizeConfig
  .pin_swap_groups`, so the anneal's `PIN_SWAP` move (previously always
  `None` with no groups configured) actually runs. A declared pin whose
  rotation-CSR degree doesn't match the rest of its own group (a
  datasheet mismatch, or one this design left unconnected) is dropped
  from that group alone, noted in the job summary, never failing the
  run. The settled swap persists exactly as before (`pcb_pin_swaps`,
  `source: 'derived'`) — only the FEED was missing; the persistence and
  re-application path was already real.
- **`ROTATE` is cost-neutral too**, for the same structural reason as
  `SIDE_FLIP` — no term reads `inst_rot`. `op='move'`'s `rot=` still
  writes a real rotation to the database (useful for footprint/courtyard
  correctness), it just isn't something the anneal optimizes for yet.

Everything else — `TRANSLATE`, `SWAP`, `LAYER_ASSIGN`,
`PLANE_PROMOTE`/`PLANE_DEMOTE`, and the `crossings`/`gap_capacity`/
`board_area` cost terms — is live and does what it says.

## See also

- [[precis-pcb-help]] — author the netlist this op surface acts on.
- [[precis-net-class-help]] — the `rules` shape `op='class_rules'` stores
  (not yet consumed downstream — see above).
- `get(kind='pcb', view='drc')` — geometric DRC on realized copper, a
  separate check from anything here (see that view's own help text).
