---
status: draft
title: "pcb: dogfood-6 routing levers — a distance-cost channel assignment before routing is worth +9 nets; capacity is not the wall, the plaza/pad-row jam is"
pillar: 3d-design
prio: high
model: opus
---

# dogfood-6 routing levers: what moves the routed count, measured

Reto, 2026-10-07 (via chat-interface): "ewod-dogfood-6 looks cool but
far from optimal. Discuss and investigate improved routing/placement/
pinswap mechanisms." Brief relayed with five steps; every number below
is measured on the replay fixture
(`tests/fixtures/pcb/ewod-dogfood-6-replay-v1.json.gz`, hydrated as
`tests/test_pcb_escape_replay._hydrate`), one hard `realize()` per arm,
default `route_passes`, no negotiation unless stated, **no prod board
touched**. Probes were untracked scratch tests; the method is in
[Probe method](#probe-method). "In2.Cu open" = the stackup/class lever
Reto accepted the same day (`In2.Cu` signal, escape class layers
`[In2.Cu, B.Cu]`); "B.Cu lock" = the stored fixture's one-layer escape.

## Results, numbered as the brief's steps

### 1. Pin swap runs, and buys 0 (B.Cu) to +3 (In2.Cu)

`pcb_route._dispatch` resolves the sink's declared swap group (55 used
HV507 channels, real pad offsets), applies
`pinswap.propose_radial_assignment` as a warm start, and the anneal's
`PIN_SWAP` move (`propose_reassignment`, crossing-only cost, gr469871)
runs on top; the settled result is written back to `pcb_pin_swaps`
(53 live rows on dogfood-6). Measured, same poses, same fixed copper:

| assignment | airwire crossings | B.Cu lock | In2.Cu open |
|---|---|---|---|
| authored (generator's channel map) | 140 | 22/55 | 39/55 |
| radial warm start | 47 | 22/55 | 42/55 |
| stored (radial + anneal PIN_SWAP) | 47 | 22/55 | 42/55 |

The stored and radial arms fail the **identical** net sets, so the
anneal's own pin-swap moves changed nothing on this board — the whole
shipped effect is the radial warm start, and it is worth 0 nets on one
layer and 3 on two. Cutting airwire crossings 140 → 47 did not move the
one-layer count at all, which is the first sign that crossings are the
wrong objective here.

### 2. Lane capacity is not the wall; the plaza-row/pad-row jam is

Geometry read off the fixture (sink `ARR1_SINK_0`, HV507 PQFP-80 at
(0, 0.75) rot 270 on the bottom; 55 plaza vias Ø0.45/drill 0.15 in nine
plazas at electrode rows/cols {1, 4, 7}; escape rules resolved by
`realize._resolve_track_rules`: track 0.15 mm, clearance 0.099 mm, so
lane pitch 0.249 mm). Everything happens INSIDE the sink's pad ring on
B.Cu: the ring interior is x ±10.5, y −6.75..8.25; the via blobs span
x and y −6.59..−4.66, 0.16..2.09, 6.91..8.10 (each plaza's 8 vias sit
0.47 mm apart, 0.02 mm barrel gap, so a plaza is a solid 1.9 mm blob).

| corridor (per signal layer) | width | lanes at 0.249 |
|---|---|---|
| x: ring-left → blob 1 | 3.92 mm | 15 |
| x: blob 1 → blob 2 | 4.83 mm | 18 |
| x: blob 2 → blob 3 | 4.83 mm | 18 |
| x: blob 3 → ring-right | 2.40 mm | 9 |
| y: ring-bottom → blob 1 | **0.16 mm** | **0** |
| y: blob 1 → blob 2 | 4.83 mm | 18 |
| y: blob 2 → blob 3 | 4.83 mm | 18 |
| y: blob 3 → ring-top | **0.15 mm** | **0** |

So one layer offers 60 vertical and 36 horizontal lanes for 55 nets,
and two layers double that: **55/55 is reachable on In2.Cu + B.Cu at the
0.15 mm track** on capacity grounds. What binds is topology, not
capacity: the ring interior is only 0.31 mm taller than the plaza field,
so the bottom and top plaza rows (P1_x, P7_x: 21 + 13 vias) are jammed
against the sink's bottom (24 channel pads) and top (15) pad rows with
no lane between. A via in those rows can drop straight onto a pad under
it (if one is under it) or must leave sideways through an x corridor;
each plaza's four edge-centre vias additionally can exit only outward
(0.09 mm between neighbouring barrels after clearance, under the 0.15
track). The HV507's channel pads are on three sides (left 16, bottom 24,
top 15; the right side carries no channels), so on B.Cu alone every net
from the middle plaza row must reach a side through corridors that the
jammed rows' sideways exits already fill — that is the measured ≈22
ceiling of the one-layer lock (Do next 4 of threads/ewod-pcb.md). With
In2.Cu open a net can climb at its plaza via, run on a layer with no pad
ring, and drop outside or between pads; 3.5 mm of board remains outside
the ring for staggered drop vias (Ø0.75 at 0.8 mm pad pitch needs two
rows).

### 3. Routing-aware channel assignment before routing: 42 → 51/55

Prototype: one Hungarian solve (`pinswap._hungarian`) over
55 escape nets × 55 channel pins with **cost = Manhattan distance from
the net's plaza via (generator ledger `pads[*].via`) to the candidate
pad** (`ir.pin_point`), applied with `PcbIR.swap_pins` (49
transpositions), then the same hard `realize()`:

| arm | airwire crossings | routed | vias |
|---|---|---|---|
| In2.Cu open, distance assignment | 53 | **51/55** | 25 |
| B.Cu lock, distance assignment | 53 | **31/55** | 0 |

+9 nets on both stackups against the shipped radial/anneal assignment,
with MORE airwire crossings (53 vs 47): the objective that matters is
how far each net has to travel through the jammed interior, not whether
its airwire crosses another. Failed with In2.Cu open: R1C5, R6C5, R7C2,
R7C6.

The brief's iteration ("reassign only the failed nets, route again")
was tried as a (net, pin) history penalty of 12 mm on each failed net's
current pin and a full re-solve: iteration 1 routed 50, iteration 2
routed 46 — the re-solve reshuffles 23 nets each time and loses more
than it frees. A local repair (swap a failed net with one neighbour pad,
keep everything else) is the right second stage; not built.

Negotiation on top of the distance assignment (In2.Cu open) adds
nothing: `negotiate=10` and `negotiate=50` both end at 51/55 with the
hard passes' result kept (`NegotiationReport`: 35 → 13 and 35 → 11
nets in conflict, plateau from iteration 12, 22–23 of 55 proposals
committed verbatim, "result not taken"). The same four nets fail. So
after a good assignment the loop has nothing left to negotiate; those
four are the jammed-row topology of step 2. On the B.Cu lock the same
assignment plus `negotiate=10` stays at 31/55 with 45–47 nets in
conflict on every iteration: the one-layer board is over capacity in
the jammed interior and no assignment or negotiation changes that.

### 4. Structured escape planning: the template already has the fact

`pcb-escape-and-driver-chain.md`'s table has the only 54/54 ever
measured on the dogfood-1 fixture, and that row ("fabric not claimed +
any signal layer") is marked NOT achievable there — it let escapes route
through foreign vias and across F.Cu. The legal best on that fixture is
50/54 with In2.Cu authored as signal. So there is no legal 54/54 to cite
as a structured-escape result; what step 2 adds is where a structured
plan would pay: the jammed rows. A per-via lane plan (BGA recipe) for
this template is small: 13 top-row and 21 bottom-row vias get "drop to
the pad row beneath where a pad is under you, else climb to In2.Cu and
exit through the nearest x corridor to a side pad"; the middle row's 21
vias get "climb, run to the left column (16 pads) or the nearest
corridor". The maze then only joins lane ends to pads. This is the
`escape.py` principle (escape is footprint-intrinsic and precomputed)
applied to the plaza fabric, which that item's "Deferred" section says
`escape.py` cannot do as-is (polygon pads, zero capacity between plaza
vias). Not built; a measurement first (step 3's assignment already
captures most of the row-distance signal, so the increment is the
corridor choice).

### 5. Placement: only the sink's offset and rotation matter, and both are jammed by design

Electrodes and plaza vias are fixed template copper; the sink is free
(`fixed=NULL`) but the anneal only translates it (`ROTATE` is
cost-neutral and `SIDE_FLIP` is wire-only — R15 C premise, gr469871
comment 1). The sink's ring interior is 21.0 × 15.0 mm against a
14.7 × 14.7 mm plaza field: 6.3 mm of slack along the sink's long axis,
0.31 mm along its short one. Any rotation jams two plaza rows against
two pad rows; the current rot 270 jams the rows whose pads carry 39 of
the 55 channels (bottom 24 + top 15), the alternative (rot 0/180) would
jam the left column's 16 and the right side's 0. **Rotating the sink by
90° is the one placement move worth measuring**: it moves the jam to
the side with the fewest channel pads. `pcb-tightest-connected-part.md`
does not apply (one net per channel, no affinity to express). Moving
the sink off the array measured worse on one layer (35 → 32, that item)
and was not re-measured here.

## Recommendation (for Reto to rule on)

1. **Ship the distance-cost assignment as the route job's warm start**
   (replace or precede `propose_radial_assignment` in
   `pcb_route._dispatch`; the anneal's `PIN_SWAP` move keeps the
   crossing cost or gains a length term per gr469871). Measured +9 on
   both stackups; the write-back path is unchanged. One bounded change,
   then one prod route of ewod-dogfood-6 to confirm 51/55.
2. **Then measure the sink rotated 90°** (step 5) with the same
   assignment, replay only.
3. **Then the template lane plan for the jammed rows** (step 4) if 1+2
   leave nets failing; its scope is the generator's fabric ledger, not
   the router.
4. Do not spend on negotiation iterations or crossing minimisation as
   levers: iterations plateau (Do next 4 of threads/ewod-pcb.md, 43 →
   45 for 90 extra iterations) and crossings do not predict the count.

## In scope (when ruled)

- Recommendation 1: `pcb_route._dispatch` warm start = Hungarian over
  Manhattan via→pad distance (far end = the fixed-copper island
  terminal, honestly resolved as the R15 A arm demanded), falling back
  to the radial order when a group has no via terminal; `last_route`
  meta records which warm start ran and its summed distance.
- A replay regression pinning 51/55 (In2.Cu open) and 31/55 (B.Cu) the
  way `test_pcb_escape_replay.py` pins today's numbers.

## Explicitly NOT in scope

- Opening unused HV507 channels to the swap: the generator's group is
  `list(channel_map)` (55 used pins); the 9 unused channels are not IR
  pins and `swap_pins` needs equal degree, so that is a generator +
  IR change, filed here as an open question, not built.
- A new router. The maze with negotiation is adequate once assignment
  stops fighting it.

## Acceptance criteria

- Route job on ewod-dogfood-6 (prod, Reto-authorized dogfood board)
  after recommendation 1: ≥ 51/55 realized at `negotiate=10`, zero
  geometric DRC errors, every escape track on In2.Cu/B.Cu.
- The replay regression above is green and the numbers are quoted in
  threads/ewod-pcb.md's Do next 4.

## Target + blast radius

`src/precis/workers/job_types/pcb_route.py` (`_dispatch` warm start,
`_resolve_pin_swap_groups`), `src/precis/pcb/pinswap.py` (a distance
assignment next to `propose_radial_assignment`), `tests/test_pcb_escape_
replay.py`. Changes the settled channel assignment of every board with
a declared swap group that has via terminals — EWOD boards only today.

## Probe method

Untracked `tests/scratch.local/*.py` (gitignored `*.local/`), each a
`store`-fixture test: hydrate like `_hydrate`, optionally skip
`apply_pin_swap_overrides` (authored arm) or apply the radial / stored
/ distance assignment with `ir.swap_pins`; In2.Cu arms set
`ir.stackup[i] = {"name": "In2.Cu", "role": "signal", "routable": True}`
and `class_rules["ewod_ARR1_escape"]["layers"] = ["In2.Cu", "B.Cu"]`;
`realize.realize(ir, config, footprints, fixed_copper)`; count
`result.unrouted`. One realize ≈ 25 s. Capacity: `realize.pads_for_ir`
for the sink's B.Cu pads, fixed-copper vias for the blobs, lanes =
floor((width − clearance) / (track + clearance)).

## Open questions / decisions log

- **Open (Reto):** which of recommendations 1–3 to build, in what order.
- **Open:** expose the HV507's 9 unused channels to assignment (needs
  netless IR pins or a generator-side channel pool).
- **Open:** the failed-net repair stage — local pairwise swap with
  re-route of the two nets only, instead of a global re-solve.
