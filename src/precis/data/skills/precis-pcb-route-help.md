---
id: precis-pcb-route-help
family: pcb
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

Each `put` mints a `kind='job'` under the parent — `pcb_place` for
`op='place'`, `pcb_route` for `op='route'` — drained by the `job_inproc`
executor lane (see `precis-job-help`).

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
  boundary, not just by the optimizer's own move generators. A softer
  pin exists: component field `max_travel_mm` (or
  `op='move' ... 'max_travel_mm': <mm>`, `null` clears) lets the placer
  move the part at most that far from its stored pose with rotation held;
  `0` behaves like `fixed='both'`. The job summary reports the travel each
  soft-pinned part used.
- `op='place'` never writes a placement worse than the stored one: the
  placement-only DRC (below) is measured on the stored poses before the
  anneal and on the settled poses after it; more error-severity findings
  after than before fails the job (`non-convergence`) with nothing
  written and the findings listed. `force=True` writes it anyway; a
  forced write that is worse opens its summary with
  `pre-route DRC gate OVERRIDDEN (force=True)`. On a
  board imported with its source routing as fixed copper, the anneal moves
  parts off the copper that ends on their pads — pin those parts
  (`fixed='both'`) or route the stored placement instead of re-placing.
- `op='place'` also accepts `snap` (bool, default `false`): a
  deterministic post-anneal pass that snaps each non-frozen part's centre
  onto a part-scaled grid (its own pin pitch for connectors/headers over
  1mm — 2.54/1.27; 0.5mm general; 0.1mm for <=4-pin parts) and rotation to
  the nearest 90°. A snap the legality gate rejects keeps the unsnapped
  pose; hard `align` operands are left alone (hard aligns win);
  soft-pinned parts snap only inside their leash. Snapped parts get
  `meta.preferred_snap` stamped. Idempotent.
- Both `op='place'` and `op='route'` accept `groups` (bool, default
  `false`): a POST-anneal pass that recognises support passives from
  the netlist — pull-up/pull-down banks sharing a rail, per-IC
  decoupling caps, LED+resistor and series pairs — and re-lays each
  group as an organised row: uniform pitch, member rotations putting
  the shared (rail/return/link) pads on one line so a single trace
  serves the row, beside the pins it serves. The members anneal
  independently (the pass only arranges them at the end, legal per the
  same gate a generated move takes — a row with no legal landing keeps
  the annealed poses). `fixed`/`max_travel_mm`/edge-mated/hard-aligned
  parts are never members. Detection is deterministic per netlist;
  nothing persists but poses.

## Assign pins by supply and client roles

Pin assignment is a matching, not an anneal move. A part declares
`supplies` (`{"role", "blocks": [[pin], ...], optional "voltage_v"}`) and a
terminal declares `client_of` (a role name, or `{"role", "min_voltage_v"}`);
top-level `roles` carry `voltage_v` and `fanout`. Before the optimizer or any
routing, `op='route'` solves every role as a min-cost Euclidean assignment
(cross-instance, warm-started from the current wiring) and persists changes
as derived `pcb_pin_swaps`. More clients than supply blocks fails the job
`input` with e.g. "6 pad_driver clients unsupplied" and writes nothing.
`pin_swap_groups` on a component is a deprecated alias (warned in the job
summary). `warm_start` defaults to `"distance"` (that matching);
`"radial"` keeps the old per-instance angular path.
`last_route.warm_start` lists per role `{role, method: "match", clients,
cost_mm, prior_cost_mm}`.

A net is named after its constrained end, and a supply pin is a free end
(the matcher may move it). `put` refuses a net you touch whose members are
2+ supply pins and nothing else: connect a constrained terminal (e.g. the
client electrode) or tag the clients `client_of` and let the matcher bind them.

`get(kind="pcb", id=s, view="channel-map")` lists which supply terminal each
client is wired to now: `role,client,net,supplier` rows (`REFDES/PIN`; empty
supplier = unbound), or `args={"format": "json"}`. It is read off the current
assignment, swaps included, and regenerated on every read; it stores
nothing. `view='check'` raises an `unsupplied_client` error per client that
no supplier terminal can serve (the same capacity and voltage gate the route
job applies).

## Route job options, idempotency and the autoplace alias

- `op='route'` also accepts `negotiate` (int 0–100, default 0 = off):
  experimental negotiated-congestion iterations, tried only when the
  normal rip-up passes leave nets unrouted; capped at 60 s, and the result
  is kept only if it routes more nets. Unmeasured on real boards yet.
- `op='route'` also accepts `corridor` (number ≥ 0, default 0 = off):
  the tile-plan corridor experiment (slice 1 of the global-plan
  router). When on, a strict
  flow-capacity tile plan is negotiated first, each planned segment
  searches with every off-corridor cell priced `corridor`, and the
  ordinary rip-up passes still run after — so it can cost time, never
  nets. Measured 2026-10-15 on both benchmarks: EWOD dogfood-6 replay
  51/55 (same as plain), QFP plaza warm 53/55 (same as plain) — opt-in
  only until a pinned measurement beats plain on both.
- `op='route'` also accepts `escape` (string `"plaza"`, default off):
  plaza packing for tough pad rows (slice 1b of the global-plan
  router). A closed pad row whose
  ring-crossing boundaries read zero lanes on the strict flow map gets
  an angular escape assignment — stub outward to a packed margin via
  lattice, inner-layer run to the source — handed to the maze as wedge
  corridors at the `corridor` price (default 5 when unset); the plain
  passes still run after. Measured 2026-10-15: EWOD replay 51/55,
  QFP plaza warm 53/55 — same as plain on both.
- `op='route'` also accepts `loop` (int 0–3, default 0 = off): the
  bounded **route → penalise → anneal → route** outer loop
  (`optimize.anneal_route_loop`). Each round routes the current board,
  folds its failed nets/escapes and congestion into the next anneal's
  margins, and re-places — including two *priced* expansion moves
  (grow the outline, add a symmetric layer pair) **only when the design
  authors bounds**. Set them on the design's meta at authoring time:
  `args={'meta': {'expansion_caps': {'outline_max_mm': [w, h],
  'max_copper_layers': n, 'max_fab_usd': usd}}}` — every field required;
  absent caps disable both moves (never treated as infinity). The loop
  ends in a conflict digest — binding constraints, net/pad/region
  witnesses, marginal fab-USD prices — not placement advice. The job
  commits the best round whose board shape is still the stored one: it
  never writes a grown outline or an added layer pair, so a round that
  only routed because the board grew is reported, not stored —
  `meta.last_route.loop.proposed_expansions` lists each growth with its
  round, routed count and fab-USD delta; apply it as a design edit
  (outline feature / stackup) and re-run. The job summary and
  `view='route-status'` show the one-line verdict; the full digest lives
  at `meta.last_route.loop.digest` next to per-round routed/failed/
  dangling counts, outline mm, layers, `best_round`, `committed_round`
  and any `refused_rounds` (a later round the pre-route gate refused
  counts as zero routed instead of failing the whole job).
- **Idempotent per (design, op, content-hash).** A re-submit against
  *unchanged* netlist/placement state and the same `iters`/`seed`
  collapses onto the in-flight/prior job instead of minting a duplicate —
  edit something (move a part, add a net class) and the hash changes, so a
  genuinely new submit always gets a fresh job.
- `args={'autoplace': {...}}` is a **deprecated alias** for `op='place'`
  (same job, same params) — carries a deprecation note in its response;
  will be removed in a future release. Use `op='place'` directly.

## Check a route job's result

After a route job lands:

```python
get(kind="pcb", id="sensor-node", view="route-status")  # per-net: unrouted|sketched|realized|failed
get(kind="pcb", id="sensor-node", view="congestion")     # the run's over-capacity-gap warnings
```

A net only ever reads `'realized'` when it is **actually clean** — no
residual same-layer crossing, no over-capacity gap, and its pads plus
fixed and routed copper form **one connected piece** (the same check
`view='drc'`'s `connectivity` rule runs, so the two views agree). A
`'failed'` net's `pcb_routes.fail` names the blocking participants (the
same "fail legibly" discipline as everywhere else in this kind): a
same-layer crossing names the other net it crosses; a congestion failure
names the gap size, its capacity, and how many strands wanted through it;
a `disconnected` failure gives the piece count and one witness point per
piece.

**The route job itself fails when any net is not realized.** Its copper,
per-net statuses and summary are still written first, so the partial board
stays inspectable — unless the result is worse than the board it would
replace (see "A worse result is never committed" below), in which case
nothing is written. The job's reason lists the failed nets. A failed route
job is not a crash: read `view='route-status'`, fix, and re-route.

**Fixed copper on another net's pad refuses the route.** If a fixed via or
track has a clearance error against a pad of a different net, the job
fails before routing and writes nothing; its reason lists each collision.
No route can remove such a short. Move the part off the fixed copper
(`op='move'`) or re-place, then route again. `view='drc'` shows the same
findings.

**An illegal placement refuses the route.** After the anneal and before the
router, the job runs the placement-only DRC (pad overlap, pad-to-via keepout,
courtyard overlap, outline containment) over the settled poses plus authored
fixed copper. Any error-severity finding fails the job (`input`) and writes
nothing; the reason lists each finding with its measured margin. Warnings do
not block. `courtyard_overlap` is an error only when two parts' lands
touch; overlapping courtyards with clear lands (inside the placer's
0.40 mm margin) are a warning naming the land gap. Fix the placement
(`op='move'`, unpin a part, re-place) and route again; `view='drc'` runs the full check. `force=True` routes the illegal
placement anyway; the job summary then opens with `pre-route DRC gate
OVERRIDDEN (force=True)` and the findings.

**A stale generator expansion refuses the enqueue.** If a stored generator
expansion's version is behind the code's (`view`/`place` responses flag it),
`op='route'` fails before queueing a job — the board still carries copper an
older build emitted. Re-put the same `generators` entry (the version mismatch
re-expands it), then re-route.

**A worse result is never committed.** After routing, the job compares the
attempt against the board's own measured floor (nets unrealized, and DRC
errors on what would be stored, both sides scored by the same rule sets).
More unrealized nets or more DRC errors than the board already had → nothing
is written: prior poses, copper and route statuses stay, and the job fails
(`non-convergence`) with the attempt's summary. An equal or better result
commits as usual.

`view='congestion'` reports gaps plus the tile-capacity map — the strict
flow model's per-layer boundary lanes, measured off the same static
occupancy grid a route pass starts from (interior chokes count, so the
map's zeroes are real walls). With failed nets and no gap warning
it says the failures have another cause; read `view='route-status'` for
each net's reason. Before routing, `view='feasibility'` lists pins whose
net class `"layers"` lock forbids their pad's layer. A pin with no authored
copper on an allowed layer is **stranded**. The router never places a via
at a pad, so a stranded pin's net fails as `layer_lock`.

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

## Author the stackup — which layers may carry a trace

```python
put(kind="pcb", id="s", args={"op": "stackup", "layers": [
    {"name": "F.Cu",   "role": "signal"},
    {"name": "In1.Cu", "role": "plane", "plane_net": "GND"},
    {"name": "In2.Cu", "role": "signal"},   # a SECOND routing layer
    {"name": "B.Cu",   "role": "signal"},
]})
```

Every board is born on the default stackup (`F.Cu` / `In1.Cu` plane /
`In2.Cu` plane / `B.Cu`), which has exactly **two** routing layers, and on
a board whose front is spoken for — an electrode field, a big connector
land — that leaves **one**. This is usually the real reason a dense board
will not route. Measured on the EWOD dogfood: opening `In2.Cu` as signal
took electrode escapes from **28/54 to 50/54** with zero new DRC errors,
having changed nothing else.

- `role`: `signal` (routable) or `plane` (pourable by the annealer). Add
  `"routable": true` / `"pourable": true` to grant the OTHER capability
  without losing the one `role` implies — the standard real-world outer
  layer is signal traces flowing around a copper fill, which is
  `{"role": "signal", "pourable": true}`.
- `plane_net`: pours that net on that layer, same as `op='plane_net'`.
  The net must already exist.
- `pour_nets`: the signal-layer spelling of the same fact — net(s) a
  board-covering fill already carries on a still-routable layer (imports
  set it from the source's pour zones). One named net assigns like
  `plane_net`; several are recorded only — one sheet cannot pour two.
- First and last layers must be `F.Cu` and `B.Cu` — a bottom-side part's
  pads, the Gerber file map and the escape-layer rule all resolve those
  two by name. 2 or 4 layers today (the DRC capability table has rows for
  those); `place`/`route` need 4.
- Unknown keys are **rejected**, not ignored, so a typo (`routeable`) is
  an error rather than an instruction silently dropped.
- Refused if it would drop a layer that still carries copper or a plane
  assignment — `op='rip'` first if you mean it.

Read it back with `get(kind="pcb", id="s", view="stackup")` — one row per
layer in stack order: `role` (`signal`/`plane`, or `mixed` when the layer
both routes and pours), the plane's net with its assignment `src`
(`authored`/`derived`; `stackup-declared` marks a `plane_net` key that
never reached `pcb_planes`, so the router will NOT pour it), `routable`/
`pourable` as the router's own predicates answer them, and any stored
physical keys (copper weight, dielectric). Its last line names the nets
the router skips — a pour carries them, so their pads via down instead of
routing end-to-end. `view='planes'` is the assignment table alone.

**A stackup is half the change.** Opening a layer does not make any net
use it: a net class that locks `"layers"` (and the EWOD generator's
`escape_layers`) still says where its nets may go. Widen both, or the new
layer sits empty.

## Assign a plane

```python
put(kind="pcb", id="s", args={"op": "plane_net", "layer": "In1.Cu", "net": "GND"})
get(kind="pcb", id="s", view="planes")  # see what's assigned
```

`layer` must name a layer in the board's stackup (`get(id=s)`'s TOC shows
it, e.g. `4 layers: F.Cu/In1.Cu(GND)/In2.Cu/B.Cu`); an unknown name is
rejected with the valid options. A plane-assigned net's pins **dog-bone
fan out** (a short stub off the pad, no via-in-pad) instead of routing
point-to-point — on the pin's OWN side, so a bottom-mounted part's stub is
on B.Cu and its drop via spans B.Cu→the plane. This is real and takes
effect on the next `op='route'`
run (the assignment is re-applied onto the freshly-built IR every time,
same as the pinned sketch). **Never route ground/power beyond the
dog-bone fanout** is the house policy this encodes; a routed trace on a
plane-served net is an anomaly, not a normal outcome.

## Move and lock edits

```python
put(kind="pcb", id="s", args={"op": "move", "refdes": "U1", "x": 10.0, "y": 5.0, "rot": "90deg"})
put(kind="pcb", id="s", args={"op": "move", "refdes": "J1", "fixed": "xy"})   # lock in place
put(kind="pcb", id="s", args={"op": "move", "refdes": "J1", "fixed": None})  # unlock
put(kind="pcb", id="s", args={"op": "class_rules", "name": "i2c", "rules": {"clearance_mm": 0.2}})
```

`op='move'` is the one op allowed to reposition a **locked** instance —
unlike the optimizer's own write-back (which never touches a `fixed`
instance, enforced in SQL), this IS the authorized edit path. Any of
`x`/`y`/`rot`/`fixed` may be given; omit `fixed` to leave the lock alone,
pass `fixed=None` to explicitly clear it. `rot` is degrees (alias `rot_deg`); a unit string (`"1.5 rad"`) works too.

A move is refused when the part's new pose would break a rule the placer
also enforces. The error names the rule and what it hit:
`courtyard_overlap with B`, `courtyard_hole with hole @ (30, 30)`, or
`via_pad_keepout with authored via`. Only the moved part is checked, so
existing conflicts elsewhere on the board don't block the move. A move
that only changes `fixed` is never checked.

Moving a part a **generator** emitted (an `ewod_pad_array` or one of its
sinks) moves the whole generator group as one rigid body: every instance
it emitted and all of its authored fixed copper (plaza vias, stubs) turn
and shift together, so a pad never separates from its own escape via. The
carried copper is judged against every other part's pads and fixed copper
first; any error refuses the whole move and writes nothing. Router copper
on a net touching a moved pad (or colliding with the moved group) is
ripped, not kept: the response lists `ripped N net(s): …` — re-route them
with `op='route'`.

`op='move'` also takes `moves=[{refdes, x, y, rot}, ...]`, judged together
as one resulting board, so a swap works where neither single move would (a
generator member in the list still brings its group; one part twice, or two
members of one group, is refused; nothing is written on any refusal):
`put(kind="pcb", id="s", args={"op": "move", "moves": [{"refdes": "U1", "x": 12, "y": 5}, {"refdes": "U2", "x": 4, "y": 5}]})`.

## Net-class rule edits

`op='class_rules'` upserts one net class's rules (`{name, rules}` — same
shape as `put(args={'net_classes': {...}})` at design-authoring time, just
scoped to one class). It is judged: router copper the new rules no longer admit is ripped
(re-run `op='route'`); pads never refuse it. Contract in [[precis-pcb-help]]
("The judged-mutation gate").

**Who reads a class's `clearance_mm` / `track_width_mm` / `via_dia_mm` /
`via_drill_mm`:**

- Read: the realizer (each track's width and via size come from its net's
  class; the route job passes the stored classes in); the router's maze grid
  (one board-wide clearance, the largest any net's class resolves to, not a
  per-net value); the route job's post-route DRC gate (a net-class-aware
  clearance check strips violating router nets); `view='drc'`; the judged
  mutation gate.
- Not read: the annealer/optimizer cost. Its `class_rules` input is never fed
  from the store, so placement is scored with default via counts and a generic
  gap pitch.

Class clearance is clamped up to the fab minimum, never below it.

Two more clearance keys: `working_voltage_v` derives `clearance_mm` from
IPC-2221B spacing (coated column; above 500 V it is refused as bad input,
not extrapolated), and `min_clearance_mm` is a hard floor. Either one
makes a copper gap under the floor a DRC **error** (not a warning) naming
`min_clearance_mm`; the stricter floor of the two nets in the pair wins,
and the derived value also raises `clearance_mm`.

**Net-class `layers` IS consumed (Rulings 2026-09-19 item 7, gr347037).**
A class's `rules` may name `"layers": ["B.Cu", ...]` (stackup layer names)
— every net tagged with that class routes ONLY on the intersection of
those names with the board's own routable signal layers
(`precis.pcb.realize._net_class_layers`), never silently widened back to
the full set. A net whose lock resolves to nothing routable (a name
absent from the stackup, or one with no routable member at all) fails
with `UnroutedReason.kind == "layer_lock"`, naming the class and the
offending layer(s), rather than routing anyway.

## Inert (or conditional) move classes — say so plainly

The joint optimizer's move set is documented in full in
`precis.pcb.optimize`; a few are worth calling out here because their
inertness is
easy to miss from the tool surface alone:

- **`SIDE_FLIP` has no cost effect.** The `crossings` term (the only term
  that could plausibly respond to a side choice) is a straight-line sweep
  at component-centroid granularity — it is structurally blind to which
  side of an obstacle a connection routes on, the same way it is blind to
  `inst_rot`. The move still runs (its dirty-cascade bookkeeping is
  exercised so the plumbing is ready), it just never changes `total()`.
  This is why `op='pin_side'` is a seed, not a lock (see above).
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
