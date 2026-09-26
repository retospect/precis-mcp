---
status: draft
title: PCB engine plan — one cost function, seeded simultaneous search, snapshot state
prio: high
model: opus
---

# The engine plan (2026-08-28)

Supersedes the aggregation section of `pcb-residual-defects-0828.md` §11 and
the interface priorities in `pcb-agent-interface-gaps.md`. Decisions are the
user's and are **settled** — this file sequences them, it does not relitigate.

## Settled decisions

1. **Freerouting is out.** The in-house layered rubber-banding graph engine is
   the system. `view='route'` goes away. Because `place.autoplace` is reachable
   *only* from that path, measures become 100% dead — so measures **must**
   become registered cost terms, and `place.py`'s objective is deleted.
2. **One scalarization over all terms.** Augmented weighted Tchebycheff
   `s = max_t(w_t·P_t) + ρ·Σ_t(w_t·P_t)` (Steuer & Choo 1983 — the ρ term is
   the standard cure for weakly-Pareto plateaus, not a smoothing hack).
   Criticality becomes `w_t`. **`risk_to_money` and `Family` are both
   deleted** — money is not dollars, it is just another objective, so
   area/layers/vias join the same Tchebycheff. `extended_part_fees` is
   dropped (a JLC billing quirk, not geometry).
   - **Within a term**, only for terms that emit many values (`gap_capacity`
     is per-segment): **hinged** sum, fractions above ~0.5 only. Single-value
     terms (`board_area`, `layer_count`, `via_count`) skip this entirely.
   - **No user-declared budgets.** Margin terms already carry physical
     budgets from the fab caps; goal terms self-normalize against their
     **seed** value (everything starts at 1.0, so the score measures relative
     improvement). Nothing here blocks on user input.
   - **ρ is load-bearing now, not a tie-breaker.** Without the money/risk
     split, ρ·Σ is the only thing still rewarding progress on a non-binding
     objective — under a bare `max`, area improvements stop counting the
     moment something else is worse. Use ~0.01–0.05, not 1e-3.
   - **Crossings and via count are ONE term at two fidelities**, not two
     terms. A crossing is resolved either by a layer change (→2 vias) or by
     re-routing (→0), and vias also arise with no crossing (planes,
     bottom-side parts, through-hole pins). Charging both double-counts one
     physical fact — near-exactly so on 2 layers. Crossings is the coarse
     estimator that via count replaces once layer assignment exists; this is
     what the L0–L5 "same term, different level" design is for, and it is
     newly correct now that via count is honest.
3. **Seeded, not phased.** Quadratic/spectral placement seed → spreading →
   simultaneous refinement with *all* move classes live. The seed only picks a
   starting point; nothing it decides is locked.
   - **Coordinates are `int32` nanometres, not float** (KiCad's own internal
     representation; ±2.1 m of range). Beyond exact arithmetic and
     bit-identical snapshots, this makes a whole class of cost terms
     *reachable*: the distilled rule "a term rewarding an exact coincidence
     over a continuous variable is measure-zero and never fires" means an
     alignment or round-coordinate term **cannot work in float**. On a
     nanometre grid it can. The seed solves in float and rounds to grid.
   - **Engine caches become CSR arrays, not dicts of sets** — the pattern
     `rotation_index`/`rotation_darts` already uses. Faster to iterate
     (contiguous, no pointer chasing) *and* one memcpy to clone.

3b. **Net topology is a tree, and it co-optimizes with everything else.**
   High-fanout nets decompose into degree-bounded trees with virtual inner
   nodes (Steiner points) — reparent/split/merge are **move classes in the
   same loop**, not a separate phase. Consequences:
   - **Fanout weighting is deleted, not deferred.** If the seed builds a
     trivial tree first (MST or star over the ratsnest), the 200-pin GND
     clique never exists and there is no dominance to correct for. The
     `1/(k−1)` clique normalization was only needed on the assumption of a
     clique model.
   - **The real prize is current summation.** `net_current_a` is per-*net*,
     which is physically wrong for a power net: the trunk carries the sum of
     its subtree, a leaf carries only its own draw. One number per net means
     over-widening every leaf or under-widening the trunk. In a tree each
     edge's current is its subtree sum — correct physics, and it feeds
     per-segment IPC-2221 width directly.
   - **It makes series-vs-shunt structural.** The unsafe `min(iMax)` over a
     net's pins (a 100 kΩ pull-up dragging a 3 A rail's estimate to 50 mA)
     stops being a hazard: the pull-up is a leaf on its own branch and never
     touches the trunk. No annotation to remember to write.
   - Interacts with the growable-IR question — virtual nodes change `n_pins`.
4. **Simultaneous search, worst-first proposal.** Keep the per-instance penalty
   list `aggregate_margin` currently throws away; sample targets ∝ penalty.
   Greedy acceptance at the current schedule, not Metropolis.
5. **No inverse moves.** Snapshot whole configurations; restore on worse;
   backtrack when stuck; track best-ever separately and return the incumbent.
6. **One feature vocabulary** — closed 6-type enum (`pad`/`hole`/`conductor`/
   `keepout`/`body`/`marking`) + `owner` (landpattern|instance|board) +
   `provenance` (declared|ingested|derived). A via is `hole`+net+derived; a
   courtyard is a `keepout` derived from `body`. `span` is full-stack only.
7. **`put` returns delta + bounded exceptions**, via the read renderer with a
   scope parameter. Plus a removal path (`op='disconnect'`, `op='remove'`).
8. **Producer/consumer ledger test** — every IR field, schema column and move
   kind needs a mechanically asserted producer AND consumer.

## Sequence

Engine track **S1→S7 is strictly ordered**. Surface track **S8/S9 is parallel**
and independent. S10 is last by design.

- **S1 — delete the second cost function.** `handlers/pcb.py`: drop
  `_render_route`, `_place_and_store`. `place.py`: drop `autoplace`,
  `_objective`, `W_*`; relocate `route_feasibility` (its one other consumer is
  `view='feasibility'`); delete the file. Drop the DSN exporter with it.
  Measures are dead until S4 — say so in `view='measures'` output, the way the
  `view='drc'` via caveat already does, and fix `precis-measures-help`, which
  currently documents `place.py`'s weights as *the* objective.
- **S2 — the ledger test.** Lands early so S3–S7 run under it. **Must ship with
  a checked-in known-inert ledger**, each entry naming the slice that retires
  it, or it reds on day one (`seg_side`, `inst_rot`/`ROTATE`, the L2 rotation
  system, `model_3d`, SVG drills, measures). The ledger doubles as the progress
  meter: a slice that ships without retiring its entry is a red gate.
- **S3 — aggregation.** Replace `margin_penalties`/`aggregate_margin`. Delete
  `p_norm` and `OptimizeConfig`'s guard against it — a sum is decomposable, so
  the "max isn't local" problem class disappears. Delete the test pinning
  1×99% vs 500×5% against the flat max; replace with Pareto monotonicity.
  **Decide money's place here** (recommended: Tchebycheff over margin only,
  money stays additive through `risk_to_money`) — decide it, don't discover it.
- **S4 — measures as cost terms.** Operands resolved at job hydration; read
  through a `CostConfig` side channel like `net_annotations`. S2 then demands
  their move-reachability for free.
- **S5 — snapshot/restore.** Delete `undo_move` and the inverse payloads on
  `Move`. **The real scope is the engine caches, not the IR** — see below.
- **S6 — search rework.** Sample `(term, key)` ∝ `w_t·hardened_penalty`, then a
  small per-term *levers function* (~10 lines, not a term×move matrix). Greedy
  acceptance; backtrack when stuck; schedule escalates at sweep boundaries.
  Temperature/cooling/reheat all go away. **Add a cycle guard.**
- **S7 — quadratic seed + spreading.** New `pcb/seed.py`. Laplacian from the
  `seg_*` arrays, fanout-normalized weights, Dirichlet on `inst_fixed_xy`,
  per-connected-component solve with spectral fallback where no fixed anchor
  exists. Replaces `seed_placement` outright.
- **S8 — authoring surface** (parallel). Exception collectors through the
  `_toc`-family renderer with a scope parameter. Removal ops, with the
  soft-delete filter audited across every sibling read.
- **S9 — vocabulary collapse** (parallel). New `pcb/features.py`; migration
  normalizing `pcb_features`/`part_footprints`; `easyeda.py` emits `hole` and
  **fails loudly on unparsed primitive classes**. Does *not* gate S1–S7:
  verified, `cost.py`/`optimize.py`/`ir.py` never read `pcb_features`.
- **S10 — L2 embedding consumer.** Deliberately last, with an expiry: if it
  slips twice, delete the rotation system. The ledger makes that visible
  instead of eternal.

## The findings that change the design

**The within-term sum drowns the critical constraint — hinge it.**
`hardened_penalty(f,s) = f²·(1 + 4s·f⁶)`. At f=0.05 that's 0.0025; `gap_capacity`
emits one value **per segment**, so a 2000-segment board's background sums to
**5.0** — exceeding `hardened(0.99,1) = 4.67`. A naive sum re-admits exactly the
dilution the flat max existed to prevent. Only fractions above ~0.5 contribute.
Related: `w_t·P_t` scales with per-term cardinality (2000 gap entries vs a
handful of coupling pairs), so the outer max is cardinality-biased without the
hinge or a per-term scale.

**`hardened_penalty` is affine in schedule — this is the key mechanism.**
Below budget `f² + 4s·f⁸`; above, affine in both. So `P_t` reconstructs exactly
from four schedule-independent accumulators per term (`Σf²`, `Σf⁸`, overage
count, `Σ(f−1)`). That gives the O(1) per-move delta **and** O(#terms)
re-scoring at any schedule — which is what makes stored states safe under an
escalating cost. **Never store a scalar energy; store accumulators and
scalarize on demand.** This is strictly better than freezing the schedule per
episode.

**Snapshot scope is bigger than the IR.** The ~100 KB figure is honest for the
IR arrays and wrong for the system. `OptimizeEngine._margin` (dict of
`TermValue`), `_seg_crossing_partners` (dict of sets), `_segments_by_layer`,
`_seg_via_count`, `ir._segs_of_instance` all hold segment-indexed state.
Rolling back IR arrays alone leaves every one stale — "dead rows fake live
state," the same family as the soft-delete bugs. `np.append` itself is
snapshot-safe (it returns a new array), but it changes `n_segments`, orphaning
cache keys and invalidating the fixed-size dirty masks. Convert the dict/set
caches to array form (the "arrays not objects" discipline they already violate)
or rebuild on restore **with the cost measured** — `_init_crossing_state` is
O(Σ layer_size²), ≈4M pair tests at 2000 segments. **This is the actual work of
S5** and was unbudgeted.

**Not beam search.** The engine is a mutable incremental evaluator: expanding k
children needs k engine copies or restore-per-child, so beam maximizes use of
the *weakest* operation. It also forces cross-node comparisons, making the
schedule trap load-bearing every generation — and with sampled rather than
enumerated children its selection guarantee mostly evaporates. Depth-first
walker + snapshot stack matches the machinery. If diversity is wanted later,
run **P independent walkers** with periodic culling: embarrassingly parallel,
each owns its schedule, no new engine machinery.

**Cycle defense is missing.** Greedy + worst-first + backtracking with no uphill
acceptance can two-cycle (fix A breaks B, fix B breaks A). SA's noise and
PathFinder's history are the two standard cures, and decisions 4/5 removed one
without adopting the other. A tabu list or per-instance cooldown is cheap.
**Without it the walker livelocks silently, and it looks exactly like
convergence.**

**The board outline is invisible to the engine.** Verified:
`OptimizeEngine.board_side = max(20, 6·√n)` — a synthetic square. The authored
outline feature never reaches the IR; there is no outline field. The quadratic
seed and spreading would solve inside a fiction. Add outline→IR in S7, or
document the synthetic square per run.

**The decap trap in the seed.** If plane-class nets are excluded from `w_ij`
(the convention `place.py` used), a decap — whose only nets are PWR and GND —
becomes an isolated vertex: the "decap invisible to the optimizer" hole that
was already fixed once. At seed time nothing is plane-promoted yet, so include
all electrical nets with fanout-normalized weights so the GND star doesn't
collapse the board onto its hub.

## Cut / deferred

- **Beam search** — don't build.
- **Term→move-kind registry column** — a levers function covers it; a matrix is
  schema for three facts.
- **1000-deep snapshot stack** — build the mechanism, default it shallow (~10s).
- **PathFinder history prices** — the hardening schedule already *is* present-
  cost escalation. Add history only when oscillation is observed, and keep it
  **outside** the snapshot (restoring prices means negotiation never converges).
- **Vocabulary payload fields with no producer** (`finish`, `model_ref`) — the
  enum types yes, the unproduced optional fields no. S2 enforces this for free.
- **Tchebycheff weight sweeps / Pareto tooling** — one default `w_t` first.

## Obligations to the paper (pcb-paper worktree, td266176)

The arXiv preprint's ship gate is the acceptance criterion in
`pcb-guided-place-route.md` — the synthetic ESP32-C3, 4-layer, `op:'place'`
then `op:'route'` through the tool surface, 100% routed, zero DRC errors.
That run outranks S1–S10; it is also a *measurement*, and every defect in this
build was found by measuring, never by reading. Two standing obligations:

1. **Record the detection method alongside every new defect**, not just the
   defect. The paper's §6.6 splits defects by how they were found (review →
   composition; oracles/properties → meaning; **invariant-vs-code audit →** a
   third arm added because of today's three findings). That split is post-hoc,
   so method-at-discovery is the only thing that lets a later reader check it.
   Write it down *at* discovery — it is unrecoverable afterwards.
2. ~~A change to `cost.py`'s module docstring needs its own note.~~
   **DISCHARGED 2026-08-28.** The paper now pins the quote to commit
   `4ff7add4` rather than to a line number, with an explicit sentence on why:
   a quotation used to show that prose and code *disagreed* has to be pinned
   to the state in which they did. **S1 can land freely; no flag needed.**
   The subtlety that forced this: S1 does not falsify the docstring, it makes
   it **true** — the prose was never wrong as a design statement, it asserted
   an invariant the code did not satisfy.

3. **The user's competing explanation, ON THE BOOKS and untested
   (2026-08-29).** The paper's causal story for the defect density is "a
   fast LLM-built system produces silent defects". The user's is narrower:
   *the first run against **the benchmark** — not the first run against any
   board — is what drives it.* On that reading most of these bugs are
   first-contact-with-a-real-artefact bugs (a constant that was tuned to
   nothing, a rule wired at one of two call sites) and a *second* reference
   board would surface far fewer, because the constants now derive from
   geometry and the call sites have been reconciled. **This is falsifiable
   and nobody has run it**: add a second, structurally different reference
   design and count defects on ITS first run. Until that is measured, the
   two explanations fit the same evidence and the paper should not assert
   the general one.
   My own reading, for whoever runs it: the residuals split. Wrong-constant
   defects (courtyard radius, seed pitch, claim radius, `PAD_RADIUS_MM`)
   should NOT recur — they were exposed by first contact and are now
   derived. Missing-invariant defects (nothing asks whether a net's copper
   is one connected component) and one-rule-two-components drift (outline,
   pad geometry) should recur on board two, because neither the invariant
   nor the shared function exists yet. If that split holds, both
   explanations are partly right and the interesting number is the ratio.

Why today's three findings mattered to the paper: they were found neither by
review nor by a property test, but by checking a stated invariant against the
code claiming it — which is now a third detection arm and the cheapest of the
six disciplines. The via defect is the sharpest, because via count enters as a
*penalty*: an always-zero penalty is not a dead metric, it is a **standing
instruction to prefer a via over a detour of any length**. It is also the
second instance of the always-zero-estimator class (after the crossings
estimator), which is the paper's first evidence that the class recurs rather
than being one accident.

## 2026-08-28 late: DRC to zero (UNCOMMITTED)

The acceptance criterion — 100% routed, zero DRC errors — is met on the
ESP32-C3 reference fixture. 1063 -> 612 -> 234 -> 0, in three steps.

### Step 1: pin positions were the root cause

**The IR had no per-pin geometry.** `PcbIR` carried `inst_x`/`inst_y` per
INSTANCE and nothing per pin, so `realize._instance_point` and every
geometric term in `cost.py` resolved all of a part's pins to its centroid.
A 14-pin MCU emitted 14 tracks on 14 different nets **all starting at one
coordinate** — an exact 0.000mm clearance violation before any routing ran,
which no router could fix.

One missing field explained most of the day's findings:
~600 `clearance` errors; `crossings` measured on a degenerate graph; and
three of eight move classes provably cost-neutral — `ROTATE` ("its
`total()` delta is a true, provable zero"), `SIDE_FLIP` ("structurally
blind" at centroid granularity), and `PIN_SWAP` (payoff computed by
`pinswap.py`'s own evaluator, never read by `total()`).

**Shipped in-tree (uncommitted):**
- `pcb/landpattern.py` (NEW) — synthesized pad offsets by pin count/label.
  Returns `(offsets, synthesized)`; pads clear the 0.090mm fab minimum;
  pin order counterclockwise from pin 1 (order decides adjacency, which is
  what makes a pin swap change crossings); mirror-before-rotate,
  clockwise-from-north, matching `padplace`. 19 tests.
- `ir.py` — `pin_dx`/`pin_dy`/`pin_offsets_synthesized`, assigned per
  INSTANCE so each part gets one coherent land pattern; `ir.pin_point()` is
  the single place local offsets become board coordinates.
- `realize.py` — track endpoints read pads (`_pin_point`).
- `cost.py` — the two segment-geometry sites read pads. **`courtyard_overlap`
  deliberately still reads instance centroids** — a courtyard is the part
  BODY and pads sit inside it, so measuring body overlap from pads would
  understate it. (Got this wrong once; comment in place.)

**Measured: 612 → 239 DRC errors** (`clearance` ~611→228,
`board_edge_clearance` 20→5, `courtyard_overlap` 5→6), routed still ≥9/11.

### What the residual 228 were, and how they went to zero

Characterized, not guessed: `track+track` 159, `track+via` 71, `via+via` 3.
**211 of 228 were still gap 0.000 — but a CROSSING, not a coincidence.** Two
straight tracks crossing on the same layer touch at the crossing point.

That made the remaining work separable, and all three of it landed the same
day. **Final: 0 DRC errors, 61/61 connections routed, ~0.5s to realize**
(seed 1; measured 0 errors on seeds 1-5, with the routed count varying —
which is the honest place for variance to live).

**1. Hard placement constraints replaced two graded cost terms.**
`OptimizeEngine._placement_is_legal` / `bounds_for` reject any TRANSLATE or
SWAP that overlaps two parts or hangs one off the board, and both keep-outs
come from each part's OWN land pattern (`ir.instance_pad_radius`), not a
1.0mm constant. **A single constant is not merely coarse, it is wrong**: a
14-pin dual-row pattern reaches 2.27mm and a module 8.89mm, so parts at the
nominal 2.0mm separation had physically interleaved pads. `seed_placement`
became a shelf pack by actual part size for the same reason — the old fixed
8mm grid spread 29 parts over 148mm of board for ~30mm of parts, which then
forced the routing grid 5x coarser than the pad pitch it had to resolve.
**234 errors, and courtyard_overlap + board_edge_clearance to zero.**

**2. The maze router** (`pcb/maze.py`, `RealizeConfig.router='maze'`, now the
default). Copper is claimed on a shared occupancy grid before it is drawn.
**234 → 0.** Four things had to be right, each of which was wrong first and
worth remembering:
   - *The claim is the copper plus its OWN clearance; the query dilates by
     the routing net's half-width.* Claiming `w_self/2 + clearance + w_max/2`
     up front is safe-by-construction and reserves the widest net on the
     board around every pad — 58 of 61 unrouted, **with DRC reading zero**.
   - *A via can go to ANY signal layer, not layer ±1.* This board's signal
     layers are 0 and 3 (1 and 2 are planes), so adjacency-only transitions
     made every layer change unreachable and the router silently
     single-layer: 8 nets unrouted with three empty layers under them.
   - *A via is not a track.* The search planned corridors at trace width and
     then dropped a via annulus into them: 21 clearance errors straight back.
     Layer changes now gate on their own wider mask, collapsed across layers.
   - *A via group is sized by ampacity.* Planning for one via and stitching
     four (5A rail) puts three of them in someone else's copper, so the
     search clears the whole group's extent up front.

**3. The route job never had the board outline.** `pcb_route._dispatch`
called `build_ir(graph)` with no `outline=`, while `pcb_place` passed one and
`_render_drc` checked the result against the real one. So the anneal and the
router both worked against a board they could not see, and the place job's
outline-aware placement was quietly re-annealed away. **One rule, two call
sites, drifted** — the same shape as most defects this build has produced.

### The trap this whole result sits on

**Zero DRC is trivially achievable by routing nothing**, and the maze
router's first revision did exactly that and read as a triumph. Every
measurement of DRC here is now paired with a routed count, in the acceptance
test (`tests/test_pcb_reference_end_to_end.py`), in `tests/test_pcb_maze.py`,
and in the bench. Do not un-pair them.

### Known design tension, deliberately not resolved

The maze router **ignores `ir.seg_layer`** — it picks its own layers. So the
optimizer's `LAYER_ASSIGN` move, and the `crossings` and `via_count` cost
terms derived from `seg_layer`, now model something the shipping realizer
does not do. Restricting the router to `{PAD_LAYER, seg_layer}` would make
them coherent again at a real cost in routability (it is what makes the
0-unrouted result possible on a 2-signal-layer board). Left as-is and named
here rather than silently reconciled; `tests/test_pcb_realize.py` is now
explicitly the TANGENT drawer's contract and says why.

### 2026-08-29 late: connectivity, and the six defects it found

**`precis/pcb/connectivity.py` is new and it is the check nothing else was
making.** Every prior rule asks how CLOSE copper is; none asked whether a
net's copper is one connected component. It is wired as
`drc.check_connectivity` (severity `error`), alongside `drc.check_unrouted`
— so "zero DRC errors" now subsumes both, and means something on its own.

Union-find over the SAME primitive alphabet and gap arithmetic as `drc.py`'s
O(n²) oracle (two notions of "touching" would be a defect generator of its
own); a via barrel unions its own layers by assertion, not geometry; a pour
unions by containment via `planes.point_in_pour` (ray casting, no shapely —
this check must not be built on the engine it exists to catch).

**Result on the reference board: seeds 1–5 all reach 0 DRC errors, 0
unrouted, 0 disconnected nets.** Before: 1 seed clean, and only against a
DRC that could not see either failure.

Six defects, in the order they were found — every one silent, none caught
by a type check or a crash:

1. **`_snap_to_pads` dragged an ATTACHED head onto the source pad.** The
   proximity proxy ("the head is near the pad") is true for every branch,
   because the star decomposition puts the trunk right past the hub pin.
   Fixed by having the router report `RoutePath.attached` — the caller
   cannot infer it.
2. **`OccupancyGrid._routed_cells` stored cell indices, not coordinates.**
   A cell index answers "may this net start here" (clearance); it does not
   answer "where is the copper" (connectivity). Now `net -> cell ->
   coordinate`, and `route()` emits the trunk coordinate as the first point.
3. **Nine nets promoted onto two plane layers.** `_gen_plane_promote`
   picked any plane layer with no occupancy check. A plane is a sheet of
   copper and two nets cannot both be it — a hard constraint, not a price.
   `net_plane_layer` is per-net so it can represent the contradiction, and
   every layer→net consumer silently kept the last writer.
4. **Plane-promoted nets were pure floating copper.** Stubs, no drop via,
   no pour — `planes.py` was "a later module" and the net was connected to
   nothing. Now `planes.plane_pours` + `realize._plane_fanout`, which is
   per PIN (the old per-segment version emitted the hub's stub once per
   connection and gave leaf pins nothing) and asks the grid before claiming
   (`OccupancyGrid.disk_is_free`) instead of stamping unconditionally.
5. **`pcb_route` never read `rres.unrouted`.** The router reported "I could
   not route 23 of these" and every one of those nets was still written
   `'realized'`; a plane-promoted net took an unconditional `'realized'`
   branch regardless. `route_complete` read green over a board with holes.
6. **`inst_rot` was WRITE-ONLY.** `pcb_set_pose` persisted it, `pcb_graph`
   never selected it, `ir.from_graph` hardcoded zeros. Placement's settled
   rotations never reached routing, and no rebuilt IR could reproduce the
   pin coordinates of its own copper — 10 of the reference board's nets
   read "disconnected" from this alone. **This is the single highest-impact
   fix in the batch and it is three lines.**

Also: **rip-up and retry** (`RealizeConfig.route_passes`, default 12) —
PathFinder's idea in its crudest form, history-based *ordering* rather than
history-based *cost*, re-running the whole pass on a fresh grid with the
previous failures moved to the front. Chosen because the occupancy grid has
no incremental un-claim. The loop stops at the first fully-routed pass, so
four of five seeds finish on pass 1 (~0.3s) and the fifth needs 12 (~4.8s).
Raising `max_expansions` does not substitute: 400k leaves the same
connection unrouted, because it is losing a corridor race, not running out
of search.

Pads are now in the model (`realize.pads_for_ir` — ONE definition, called
by both `to_gerber_model` and the DRC handler) carrying `synthesized: True`,
and `gerber.export_fab` **refuses** a model containing them
(`SynthesizedPadError`) because `landpattern.py` says these are bounds that
"must never be exported to fabrication".

**Still open from this pass:**
- **PIN_SWAP is not persisted.** `_apply_pin_swap` calls `ir.swap_pins`,
  which genuinely mutates pin→net, and nothing writes it back — so the
  stored netlist and the stored copper would describe different boards.
  **Measured, not assumed: it never fires on this fixture** (3000
  iterations, `_gen_pin_swap` returned `None` 91 times and produced 0
  moves — no swappable groups), so it is dormant rather than fixed, and it
  was NOT the cause of anything above. Either persist the assignment (it
  is a netlist edit, so it belongs in `pcb_connections`) or take PIN_SWAP
  out of the schedule until it can be.
- **Plane promotion is now never chosen by the annealer on this fixture.**
  Measured: 79 PLANE_PROMOTE moves proposed over 3000 iterations, all
  rejected on cost. Before the one-net-per-layer fix nine nets ended up
  promoted, which looked like the search liking planes and was mostly the
  search stacking nets onto a layer that could only hold one. Zero is a
  defensible answer here — SDA on a plane layer was always nonsense — but
  a real 4-layer board wants GND and VCC3V3 on its planes, and the cost
  model does not currently make that attractive. That is a cost-model gap,
  not a router one. Consequence for coverage: the reference board no
  longer exercises the pour path at all; `tests/workers/test_pcb_route.py`
  covers it through an AUTHORED plane assignment, which is now the only
  thing that does.
- An unpourable plane (no board outline) is reported as `unrouted` rather
  than naming its cause. Correct but not legible; "fail legibly" wants a
  dedicated finding.

### 2026-08-29 later: "we don't DRC out a board smaller than its parts"

User's question, and they were right. Shrinking the reference outline and
counting what leaves the board:

| board | parts outside | pads outside | copper outside | DRC errors (before) |
|---|---|---|---|---|
| 20mm | 24/29 | 48/81 | 70 | **10** |
| 10mm | 26/29 | 61/81 | 86 | 19 |
| 2mm | 28/29 | **81/81** | 95 | **9** |

The count went DOWN as the board got more absurd. `check_board_edge_
clearance` measures `boundary.distance(geom)` — unsigned distance to the
outline as a LINE — so it is symmetric about the edge and silent on
anything far from it, on either side. Copper 20mm off the board is not
*near* the edge. **Nothing anywhere asked whether copper was INSIDE the
board.** Not for copper, not for pads, not for parts.

Fixed by `drc.check_outline_containment` (copper + pads + courtyards,
always `error`, no two-tier margin — a fab images what is inside the
profile and copper outside it is not marginal, it does not exist). Same
sweep after: 146 / 176 / 194, rising monotonically, and boards that fit
still report 0.

Second half: **`seed_placement` shelf-packed from the origin with no idea
where the board was.** On any outline narrower than its natural row width
it packed parts straight off the edge — and once a part is outside,
`bounds_for` clamps every TRANSLATE that could rescue it while
`_placement_is_legal` rejects the crowding that bringing it back would
cause, so it stays outside for the whole anneal. Now anchored at the
outline and wrapped to the board's width. A 20mm-wide board seeds 0 parts
outside where it used to run out to 28mm. Genuinely over-full boards still
overflow downward, which is honest and now reported.

**And a third, found by LOOKING at the render:** three traces and three
vias on In1.Cu, a **plane** layer. `stamp_path` registers a via's attach
cells on every layer its barrel passes through (right — that is where the
copper is, and connectivity needs it), and `route` used those as
multi-source starts without filtering back to its `layers` argument. So a
net owning a through via could start a later connection *inside the
barrel* on an inner layer and run a trace along it. It shorts to the plane
the moment one is poured. Every existing test asked whether copper
overlapped, was reachable, or was connected; **none asked which layer it
was on**. Now
`tests/test_pcb_maze.py::test_routed_copper_only_ever_lands_on_a_signal_layer`.

Note the pattern across all three: the viewer found the last one in
minutes, and it is the one no existing check could have found, because
every check was about relationships between pieces of copper rather than
about where a piece of copper is.

**The acceptance fixture's board is 300×300mm and its parts occupy 44mm.**
Noticed by rendering it (`view='svg' args={'level':'fab'}`) — the board is
a speck in the corner of its own outline. 46× more area than the design
needs makes "0 unrouted" a weaker claim than it reads as, because
congestion is what a router is actually for.

**Measured before assuming, and it survives**: re-running seeds 1–3 with
the outline tightened to 60mm, 45mm and 35mm square gives 0 unrouted, 0
islands, 0 DRC errors at every size; only the runtime moves (0.4s–5.6s,
and not monotonically — the hard seed changes as the board shrinks). So
the oversized board is not what is producing the result. It is still worth
fixing, because a fixture that cannot get harder cannot detect a router
getting worse — but it is a *sensitivity* gap, not an inflated number.

**Correction to how that was first reported.** It was written up as "35mm
is tighter than the parts' own 44mm extent", which is wrong reasoning: the
44mm figure is the extent the parts settle to on a 300mm board, and the
annealer compacts when the board shrinks. At 35mm nothing is outside, so
the test never exercised the failure direction at all — it was a pass
generalised into a robustness claim without checking what a failure would
even look like. Asking that question is what produced the section above.

Related, and the honest place to put it: this is one axis of "is the
benchmark easy?" ruled out. §Obligations item 3 records the user's
competing explanation for the defect density and the experiment that would
settle it, which is a SECOND reference design, not a resized one.

### 2026-08-29: 32 track ends float in mid-air (FIXED — see above)

Found by measuring the rendered board, not by reading code. On the seed-1
reference board, after the pad-snap fix below, **32 track endpoints lie on
neither a pad, nor a via, nor any same-net copper.** All on GND and
VCC3V3 — the two highest-fanout nets.

**They are not dogbone stubs.** That was the first hypothesis and it is
wrong: this run has **zero** `is_dogbone` tracks (neither net was
plane-promoted). Recording the disproof because the hypothesis is
seductive and someone will have it again.

**Root cause: T-junction attach points are cell centres.** With
attach-to-own-copper, a net's 2nd..Nth connection starts from a cell in
`OccupancyGrid._routed_cells`, and that set holds *cell indices*, snapped
from the trunk's resampled centreline. The new branch therefore begins at
the cell's CENTRE — up to half a cell diagonal (~0.10mm at this pitch)
off the trunk it is supposed to join. With a 0.09-0.20mm trace, the
branch can simply **not touch the trunk**: the net is electrically
severed while DRC reports a clean board and `unrouted` is empty.

This is the same root cause as the pad-snap defect below — cell centre vs.
true geometry — and it is the more dangerous of the two, because a pad is
0.4mm wide and absorbs the error while a trunk trace is 0.1mm wide and
does not.

**Fix:** make `_routed_cells` a `dict[int, dict[int, tuple[float,
float]]]` (net -> cell -> the exact resampled centreline coordinate) and
have `route()` emit that coordinate as the path's first point instead of
the cell centre. Then extend `_snap_to_pads` (or fold both into one
"anchor the ends to real geometry" step).

**Test that would have caught it, and should exist regardless:** a
connectivity check — every net's realized copper must form ONE connected
component, unioning tracks that touch, vias, and pads. That is a
different question from clearance and nothing currently asks it. It also
subsumes the stitched-via-group defect below.

### Latent: a stitched via group is not connected to its own trace

`_realize_maze` spreads an ampacity-sized via group along x through the
transition point at `via_dia + clearance` pitch, so for `n > 1` **no via
sits at the point the trace ends on** — the trace terminates in the
`clearance/2` gap between two annuli. Every group on the reference board
is size 1, so it does not fire there, but `tests/workers/test_pcb_route.py`
's high-current wall design asserts `len(rows) > 1`, so it is live in the
shipping path. Fix by emitting a stitch bar (one segment from the first
via to the last, on both spanned layers) — it sits inside the group extent
the search already cleared.

### An unrouted connection must be a DRC finding (user, 2026-08-29)

Today an unrouted connection is reported in `RealizeResult.unrouted` and in
`pcb_routes.status='failed'`, and `view='drc'` says **zero errors**. So
"DRC clean" does not mean "board is finished" — which is the *same* trap as
"zero DRC by routing nothing", just relocated: the number a reader trusts
is silent about the thing that matters. A board with 23 unrouted nets and a
clean DRC is not a clean board.

Add an `unrouted` rule to `drc.run_geometric_drc` (severity `error`, one
finding per unrouted connection, naming both endpoints). Then the single
number is honest on its own and the paired-count discipline in the tests
becomes a belt to the DRC rule's braces rather than the only guard.

Note this changes what `BASELINE_DRC_ERRORS = 0` asserts — it would then
subsume `BASELINE_ROUTED_FANOUT2` rather than sit beside it. Keep both
anyway; they fail with different messages, and the routed count is the one
a human reads.

### RESUME POINTER (2026-08-29 LATE — supersedes the evening one below)

**Tree state: everything is COMMITTED on this branch, nothing shipped.**
`origin/main` is still `74390332`; this branch is many commits ahead of it,
last one `docs(pcb): correct padplace's false claim...`. The earlier "33
files uncommitted" hazard is resolved — the working practice now is a
**checkpoint commit at every agent boundary** (written into
`.claude/skills/flow` and `coder-chain`, and into auto-memory). Keep doing
that. **Never run `git checkout`, `git stash` or any destructive git command
in this tree** — an agent destroyed WIP that way earlier today — and put
that sentence in every agent prompt.

**Stroke font: DONE (`d388d6cb`), no agents in flight.** The hand-authored
table is gone, replaced by real Hershey Roman Simplex transcribed from
`rowmans.jhf` (licence permits conversion but REQUIRES two acknowledgements,
now in the module docstring — do not delete them). The defect it fixed:
`_RAW["S"]` authored both arcs with reversed sweeps, so every "S" this
system had ever silkscreened was MIRRORED — root-caused to the glyph table,
not the pipeline (`gerber_view` applies exactly one uniform y-flip,
correctly). The "N" was never affected; an earlier claim that it was is
wrong.

Verified by plotting the *shipped* table through the real `layout_text`
path, top-side and mirrored: S starts upper-right and ends lower-left, P's
bowl is upper-and-right of the stem, F's upper bar is longer than its
middle, R's leg runs down-right. Five handedness tests now pin those ordered
relations. **They exist because the old bbox/stroke-count assertions were
symmetry-blind by construction** — see "A test fixture must have a TRIVIAL
symmetry group". Any future glyph test must use a glyph asymmetric in BOTH
axes; never S or N alone.

**Latest rendered board: `/tmp/board-latest.svg`** (regenerate with
`PRECIS_PCB_RENDER_OUT=/app/board-latest.svg UV_WITH="--with shapely"
scripts/test tests/test_pcb_fab_render_all_layers.py -q`, then copy it out
of the worktree — `scripts/test` now forwards that env var). That test FAILS
by design on its DRC assertion; the SVG is written first. Render warnings
ride in a `<desc>` element at the end of the SVG (NOT an XML comment — the
messages contain `--`, which is illegal there and corrupted the file once).

**Known state of that board:** 3 DRC errors (`board_edge_clearance` 0.390 vs
0.400mm — 10µm short; GND in 3 pieces; VCC3V3 in 2). Title block and S/N
patch place correctly; only 2 of 3 fiducials fit and they are flooded by the
F.Cu pour anyway (needs a realize-time antipad, which the render path
structurally cannot cut). 7 courtyards for 29 parts.

**Nothing in flight.** (Earlier agents — multi-layer copper fill, via↔via
DRC + cross-part silk collision, the `fable` critique of
`pcb-drc-keepout-matrix.md` — have all reported and landed.)

**Next up, in order** (the silk queue agreed with the user; all in
`silk.py` and `drc.py`, all were blocked on the font and no longer are):
structured per-instance silk census → a `silk_missing` DRC rule → a
`silk_printability` rule (consumes the currently-dead `silk_width_mm`) →
labels placed OUTSIDE courtyards → pin-1 honesty (a marker only where a
pin is really named "1"; 7 of 29 parts on the reference board are not, and
today they get a `min(pin_id)` guess drawn with full confidence), plus a
cathode band for diodes. Then: restrict routing to orthogonal/45° — note
`_collapse_straight`'s any-angle shortcutting is what *creates* the
diagonals, so it must be constrained too, not just the search.

**Do not ship while agents are editing** — `scripts/ship` snapshots WIP.

#### The lesson that outranks every individual fix

**DRC IS PULL-BASED AND NOTHING RUNS IT FOR YOU.**
`run_geometric_drc` executes only from `get(view='drc')`. `place` and
`route` never trigger it. `Store.pcb_drc_findings_latest` reads *persisted*
rows and returns `(None, [])` when no run exists — its own docstring says
**"no run yet means 'not yet', not 'clean'"**.

`tests/test_pcb_fab_render_all_layers.py` asserted `not errors` after
reading that function *without ever calling `view='drc'`*. It passed
vacuously, and a board with **7 `via_pad_keepout` errors** was reported to
the user as verified-clean. The assertion had been added specifically to
stop a defective board being presented as a deliverable, and it caught
nothing. Fixed: the test now calls `view='drc'` and asserts `run_id is not
None` before reading findings.

**Generalise it: any caller that reads `pcb_drc_findings_latest` without
having caused a run is asserting nothing.** Audit every such caller —
including `workers/auto_check_evaluators/netlist_drc_clean.py`, whose
docstring admits it "reads what a prior call already wrote".

#### Still open, in priority order

1. **7 `via_pad_keepout` errors** on the 4-layer render config — plane
   drop vias landing on pads *despite* `_drop_via_site` already calling
   `via_clears_pads`. The guard exists and they got through it. Likely the
   reach is derived from *this* pin's pad while the via lands on a
   *neighbouring* pin's pad (the failing SCL via sits on R2 pin 1).
2. **Two vias overlapping each other**, drills included — SCL pair 0.1598mm
   apart (drills are 0.25mm), GND pair near U1 at −0.392mm. No rule exists;
   `check_clearance`'s same-net exemption is correct for copper and wrong
   for holes.
3. **Cross-part silk collision** — `build_silk` tracks `own_silk` per
   instance and resets it each iteration, so two parts' labels may overlap.
   6 refdes labels dropped on this board (C14, C3, C6, C8, R2, U3), each
   honestly reported in `SilkResult.dropped` — which no test reads.
4. **A part can keep a lone pin-1 tick** after losing both its courtyard
   and its label (C3), leaving context-less ink beside a pad.
5. **Pour inset (0.975mm) ≠ DRC board-edge minimum (0.6mm).** The pour
   borrows the router's grid-clip figure, so a part can be legally placed
   and still sit in the strip that gets no copper (C9). Two numbers, nobody
   reconciling them.
6. **Fiducials/title block are built but not wired** — `handlers/pcb.py`
   call-site diffs are in the agent report, unapplied. Also
   `soldermask_gerber` swells every pad by a fixed 0.05mm with no per-pad
   override, so a fiducial's 2mm opening comes out 1.1mm.
7. **Inner layers barely used** (In1: 3 traces, In2: 0, F.Cu: 66). The maze
   router ignores `ir.seg_layer`, so nothing can *ask* for a layer; choice
   is A* cost alone and `VIA_COST_MM = 3.0` each way never pays on a 40mm
   board. User also wants direction *restricted* (up/down + diagonal, or
   up/down + 90° arcs) rather than merely penalised.
8. **Font is a reconstruction, not real Hershey** — the agent had no
   network. Genuine digitized data can be fetched and swapped behind the
   same interface.
9. `padplace.board_pads` lacks `refdes`/`pin`, so tooltips stay anonymous
   on the cached-footprint path.
10. `gr270090` (THT pad claimed on `PAD_LAYER` only, drill never read) and
    `gr269811` (tangent router `_vias_for_track` places a via at the pad
    coordinate) — both dormant, both real.

### THE SHORT: a pour cuts no antipad around any PAD (user, 2026-08-29)

**The worst defect this build has produced, found by a human looking at a
render, on a board reporting zero DRC errors.** The user's words, which
name the mechanism exactly: *"there is a tiny round cutout for the square
pad that is much larger"* — and *"all nets are functionally connected with
the copper on F.Cu."*

**Cause 1 — the pour never sees pads.** `realize._pour_planes` calls
`plane_pours(..., copper=to_gerber_model(interim, ir, ...)["copper"])`.
`to_gerber_model` returns **`copper` and `pads` as separate keys**; only
`copper` is passed. So the fill flows around tracks, vias and other pours
— and straight over every pad on its layer. The round cutout the user saw
is a *via*'s antipad (vias are in `copper`); the square pad beside it gets
nothing. Every pad on a filled layer is electrically merged with the fill
net.

**Cause 2 — clearance DRC never sees pads either.**
`drc.clearance_pairs_indexed` iterates `model["copper"]` only. Its own
docstring says "every same-layer, different-net copper-item pair
(tracks/vias/pours)" — pads are not in that list. So **pad↔pour,
pad↔track, pad↔via and pad↔pad are all unchecked.** Clearance is the one
rule that independently verifies the occupancy grid's central guarantee,
and it has been blind to the primitive class you solder to. Check the
O(n²) reference oracle too: if it shares the blindness, then "the oracle
agrees" was load-bearing evidence over a shared blind spot.

Two independent omissions, same shape — *"pads live in a separate list and
this code forgot"* — and together they produce a shorted board with a
clean DRC. Neither is a subtle geometry error; both are a missing key.

**The lesson, stated as a rule:** the model has `copper` and `pads` as
sibling keys and **a pad is copper**. Every consumer that reasons about
copper must be audited for whether it reads both. Grep for
`model["copper"]` and `model.get("copper")` and check each one.

### Drop vias skip the pad keep-out: 55 of 57 errors (user, 2026-08-29)

User: *"on J2 I see square pads that overlap with vias" … "same in C7 and
U3."* Confirmed, root-caused, and board-wide rather than three instances.

`realize._drop_via_site` — the plane fan-out path, used for **every pin of
every plane-promoted net** — checks `grid.disk_is_free` only, which is
deliberately **same-net-blind** (that is precisely what `via_clears_pads`
exists to catch), and never calls `via_clears_pads`. `grid.route()`'s own
via search applies the guard through `_pad_keepout_mask`; this path does
not.

Measured on the user's exact board (esp32c3, 40mm, GND→In1.Cu,
VCC3V3→In2.Cu, seed 1): **57 DRC errors, 55 of them `via_pad_keepout`.**
GND has 26 pins and VCC3V3 has 23. J2 gap −0.1675mm, U3 −0.1675mm, C7
−0.100mm.

**Second fault, and the more instructive one:** `dogbone_stub_mm` is a
flat **0.5mm** — exactly the distance measured at J2 — that ignores the
pin's own pad size, which is why a 0.585mm pad and a 0.45mm pad overlap by
different amounts. **Fifth instance of "a constant tuned to nothing"**
(courtyard radius, seed pitch, claim radius, `PAD_RADIUS_MM`, this).
Derive the stub from pad extent + via radius + clearance.

Ruled out by census on the same board: every via's drill against every
foreign pad and track, on every layer it spans — **zero** overlaps. The
normal router's via placement is correct; this is confined to plane
fan-out.

**Dormant sibling, filed `gr270090`:** `pad_geometry` never reads a
footprint's `drill`, so a **through-hole** pad is claimed on `PAD_LAYER`
only while its real annular ring spans every copper layer — a foreign
trace may run under a THT pad and be drilled through. Measured at the code
level: foreign trace vs the real In1.Cu pad, gap −0.725mm; vs the drill
itself, −0.425mm. Cannot fire today because no fixture has a cached THT
footprint. `check_npth_clearance` cannot catch it either: it filters to
`not plated`, and a THT component drill is always plated.

### "Inner layers are barely used" — nothing can ASK for a layer

User confirmed after seeing In1 carry 3 traces and In2 zero, against 66 on
F.Cu, on a board where both inner layers were explicitly made routable
with opposed preferred directions.

The capability is real; the *mechanism to request it* does not exist.
**The maze router picks its own layers and ignores `ir.seg_layer`** — its
module docstring says so outright. So the optimizer's `LAYER_ASSIGN` move
and the `crossings`/`via_count` cost terms derived from `seg_layer` model
something the shipping realizer does not do (already recorded as a
deliberate known tension). Layer choice is therefore made solely by A*
cost, where a layer change costs `VIA_COST_MM = 3.0` **each way**; on a
40mm board with a mostly-empty top layer no detour is ever long enough to
pay for that.

Note the fill cannot supply the missing pressure either: a pour is
computed **after** routing by design (it is defined by what it must
avoid), so it takes leftover space rather than competing for it.

Two levers, and they are not equivalent:
- **`VIA_COST_MM`** is an explicitly documented routing *preference* dial.
  Cheap, measurable, and a global nudge — it buys layer spread with vias
  and does not give a *structured* board.
- **Honour `seg_layer`** (or bias strongly toward it) in the maze router.
  This is the real answer, it is what makes H/V preferred directions mean
  something, and it simultaneously un-deadens `LAYER_ASSIGN` and the two
  cost terms. It costs routability — routing to a fixed layer assignment
  is what makes 0-unrouted hard — which is exactly why it was deferred.

### A via may not sit on a pad — and 14 do (user, 2026-08-29)

User: *"vias should not overlap with pads (they have a courtyard too)."*
Correct, and it was unchecked everywhere.

**Why nothing caught it.** `OccupancyGrid.disk_is_free` deliberately
treats **same-net** copper as legal — that is precisely what lets a trace
end on its own pad. No via-placement site had any notion of "pad" as
distinct from "claimed copper", so a via landing on its own net's pad was
legal at every one of them. It cannot be expressed by tightening
clearance, because clearance's same-net exemption is load-bearing. It is a
different question about the same geometry, the way `check_connectivity`
is to `check_clearance` — and it is a **manufacturing** rule, not an
electrical one: a hole drilled through a land you intend to solder to
wicks solder down the barrel and starves the joint.

**All four grid-aware sites were unsafe**, each for the same reason:
- the maze router's layer-transition via — gated only against *foreign*
  copper via `via_ok`/`via_blocked`;
- the **ampacity via group's satellites** — placed by straight-line
  arithmetic and **never re-asked of the grid at all**;
- `_drop_via_site` — searches outward at `dogbone_stub_mm * step`, and at
  step 1 a typical via radius plus pad radius still overlaps;
- `_shove_vias` — the worst, because it is the only one that can take an
  **already-legal** via and move it into a violation.

**A fifth site, found unprompted and NOT fixed** (`gr269811`):
`_vias_for_track`, used by `router='tangent'` and `re_realize_segments`,
has no `OccupancyGrid` at all — pure IR arithmetic — and its single-via
case places the via at `offset=0`, which *is* the pad coordinate.
Measured: gap **−0.875mm** against a 0.090mm requirement. Needs a
different mechanism.

**The margin is not a new constant.** `capabilities.py` has no via-to-pad
field and does not need one: `trace_spacing_mm` is already this project's
"how close may two independent copper features get", and a via annulus and
a solder land are independent features by that rule's own premise,
regardless of net. `maze`'s `clearance_mm` is already that same figure.

**Reachability verified on REAL realized geometry**, not a synthetic
fixture — `check_npth_clearance` sat wired-in and permanently dead for
exactly the want of that check. Also confirmed `check_clearance` stays
silent on the identical geometry, which is the whole point.

**The finding: 14 `via_pad_keepout` errors on the ESP32-C3 reference
board** — the board that has reported 0 DRC errors all week. Third
instance this session of the same lesson, and it should now be treated as
a rule of the build: **a measured zero means the check is dormant until
proven otherwise.** (The other two: `courtyard_overlap`'s flat 1.0mm
against real keep-outs reaching 8.9mm; `npth_clearance`'s absent
producer.) Those 14 must go to zero **by prevention** — the router-side
guards — never by exempting them. If any survive, that is a router
capability gap and must be reported as one.

### The drill layer was rendered and invisible (user, 2026-08-29)

User asked "can we show the drill layer too". It was already there:
`PTH` carried all 26 holes and was painted `#101010` on the viewer's
`#12141a` background — a brightness delta of a few percent. **A layer that
exists and cannot be seen reads exactly like a layer that is missing**,
and no presence check can tell them apart, which is why the test for this
asserts a *brightness gap* between the hole fill and the document
background rather than that a `<circle>` was emitted. That test fails
against the old code by the right mechanism (delta ~5, needs >100).

Holes now render as **voids** — light fill, thin dark stroke so they read
against a light or dark background — matching `svg.py`'s existing
`_via_el`/`_drill_el` convention rather than inventing a third look for
the same physical thing. Plated vs unplated is the dash cue `svg.py`
already uses (solid = PTH, dashed = NPTH); they are drilled on separate
fab passes and must not look alike. The legend swatch had the identical
invisibility bug — it is drawn on a near-black panel — and is fixed too.
Rows are labelled `PTH (drill)` / `NPTH (drill)`, since the user's own
words were *"i know its not a gerber"* and a row reading `PTH` does not
answer that.

### Silkscreen must clear vias, not just pads (user, 2026-08-29)

Silk already refused to print over a **pad** (a fab scrapes silk off
exposed copper, so the text is silently lost). A via has the same problem
and was not in the obstacle set. Now `build_silk(..., vias=...)`, applied
to all three silk elements — courtyard outline, pin-1 tick and refdes —
not only the label.

Two details worth keeping:
- **Side-aware.** Only a via whose barrel actually reaches the silk's own
  side blocks it; a blind via to `F.Cu` must not push the bottom-side
  refdes around. Derived from the via's `span` against `ir.stackup`'s
  order, the same convention `svg._via_layer_names` and `drc.py` use. A
  malformed span conservatively blocks both sides.
- A via is reshaped into a circle-pad dict so it rides the **existing**
  SAT overlap helpers. Adding a second geometry convention for "does this
  stroke hit that thing" is how the two would later disagree.

### "Pads are to be precisely what the footprint says" (user, 2026-08-29)

**The join key was dropped in the middle, and both ends already worked.**
`realize.pad_geometry(ir, footprints=...)` uses real per-pin geometry when
given a footprints dict; `Store.pcb_footprints_for` returns exactly that,
**keyed by LCSC C-number**; `pcb_graph` selected
`refdes/x/y/layer/roles/label/height_mm/n_pins/fixed/rot` from a query that
**already joined `pcb_components`** — and not `c.part_lcsc`. So nothing
could ever pass `footprints=`, every pad on every board was a synthesized
bound, and `gerber.export_fab` therefore refused every routed board
forever, including one whose parts were all real. Identical shape to the
write-only `inst_rot` defect: persisted, round-tripped, never selected.

Closed: `pcb_graph` selects `part_lcsc`, `PcbIR.instance_part_lcsc` carries
it (sentinel `None`, deliberately not an indexable value — see the NO_NET
finding below), `session.footprints_by_refdes` does the remap, and the DRC,
gerber, fab-preview and route-job call sites all pass it.

**And a silent one found on the way, worse than the gap itself.**
`_render_gerber`'s primary pad source is `padplace.board_pads()`, which by
design "contributes nothing" for an **uncached** part; the handler's only
fallback was all-or-nothing (`if not pads: pads = self._drc_pads(...)`),
firing only when *every* part was uncached. So a **mixed** design — the
realistic case — silently **dropped the uncached part's pads from the fab
set entirely**. Not flagged, not synthesized, absent. And `export_fab`'s
refusal only inspects `pad.get("synthesized")`, so with the pads simply
gone it had nothing to object to and exported a complete-looking,
unsolderable board. A guard that checks the pads it *has* cannot see the
pads it *lost*. Fixed by merging per-pin synthesized pads for exactly the
missing refdes' pins.

**Still not "precisely what the footprint says", and this is the honest
caveat.** Pad **size** now comes from the real footprint; pad **POSITION**
does not. `from_graph` takes no `footprints` argument at all, so
`pin_dx`/`pin_dy` are always `landpattern` synthesis. The fab export is
unaffected (it sources position and size together from
`padplace.board_pads`, bypassing the IR), but **the router, the DRC and
the `level='fab'` preview all read the IR** — so they see a real pad size
at a guessed position. `PcbIR.pin_dx`'s docstring actively claimed the
opposite ("populated from real `part_footprints` pads where cached");
corrected in place, with the correction recorded rather than quietly
deleted, because that false claim is precisely what would stop the next
reader noticing. Closing it means reconciling per-pin real offsets with
netlist pin identity through the L0 pin model.

### "Is the router aware where it cannot go?" (user, 2026-08-29) — partly

Answered by reading what actually reaches `maze.OccupancyGrid`, not by
reading the prose about it. **Copper-vs-copper: yes, structurally.
Mechanical: no, and nothing anywhere says so.**

**What the grid knows.** Every other net's copper — tracks, vias, pads —
is *claimed* on the shared grid before it is drawn, which is what makes an
inter-net clearance violation impossible rather than merely unlikely. That
guarantee is real and it is the whole design.

**What it does not know, in increasing order of how much it matters:**

1. **A pad's actual shape.** Pads are claimed as **discs** — the enclosing
   circle of the rectangle (`hypot(w,h)/2`). That errs toward *over*-claiming,
   so it is safe, but it means a fine-pitch rectangular land pattern
   reserves up to 41% more area than its copper occupies, and the router
   is denied space that is genuinely free. Documented as deliberate in
   `_realize_maze`; worth revisiting only if congestion becomes the
   binding constraint (it now has, twice — see the keep-out measurement
   above).
2. **A non-rectangular board.** `realize._outline_clip` returns the
   outline's **bounding box** inset by the edge clearance, and
   `maze.grid_for` takes that as a rectangular clip. So an L-shaped board,
   a rounded corner, a notch or an internal cutout is invisible: copper
   can land inside the bbox and outside the actual profile. The docstring
   is honest that this is an over-approximation and consistent with
   `cost.outline_bbox` and the placer's bounds — but note the whole
   containment DRC rule (`check_outline_containment`) uses the true
   polygon, so **DRC can now report a violation the router had no way to
   avoid**. That asymmetry is new and is the thing to fix first.
3. **Mounting holes and every other mechanical feature.** Verified: zero
   references to `mounting_hole`, `keepout`, `npth` or `drill` anywhere in
   `maze.py` or in the grid-stamping paths of `realize.py`.
   `session.outline_from_features` reads **only** `ftype == 'outline'` and
   drops every other feature type on the floor. **So the router will route
   a trace straight through a 3.2mm mounting hole**, and — since
   `_drc_drills` now populates the DRC's `drills` input — DRC will report
   it afterwards as an `npth_clearance` error the router could not have
   prevented.

The pattern across all three is one thing: **the grid's obstacle set is
"copper other nets claimed", and the board's obstacle set is larger than
that.** Everything mechanical — the profile, holes, cutouts, keepouts,
courtyards, mask openings — reaches DRC and does not reach the grid. That
is why the DRC/router asymmetry keeps appearing: they are reading two
different models of the same board.

**Fix direction:** one obstacle-assembly step that walks `pcb_features` and
stamps *everything* non-routable into the grid before routing starts —
holes and keepouts as claimed discs/polygons on all layers, the true
outline polygon as the passable region rather than a bbox. It is the same
"one source of truth" move that fixed pad geometry and the keep-out
radius, applied to the obstacle set. Until it exists, the honest statement
is that the router guarantees clearance from copper and nothing else.

### SVG drills: closed (`pcb-residual-defects-0828.md` §3)

`svg.DEFAULT_INCLUDE` was `{outline, copper, pours, pads, vias, silk}` —
no `drills`, and `render_board` ignored the model's `drills` list
entirely. Now included by default and drawn **on top of** copper, as a
white disc with a stroked edge (dashed when unplated).

Why the omission was invisible: a *plated* hole already renders, because
`_via_el`/`_pad_el` paint an annulus and punch a white centre. Only a hole
with no copper around it — a mounting hole — had nothing to reveal it, and
it therefore rendered as **nothing at all**, or as a solid disc if
something else happened to sit there. A missing feature that looks like a
clean board is the worst way for a render to be wrong. The handler's
`level='board'` model now supplies `drills` too, reusing `_drc_drills`
rather than growing a second reader of the same features.

### Two DRC rules that could not fire — both now live (2026-08-29)

Board two's findings 2 and 3, fixed in `handlers/pcb.py::_render_drc`.

**`check_npth_clearance` had no producer.** It reads `model["drills"]`;
`_render_drc` built a model with `layers`/`copper`/`pads` and no `drills`
key. New `_drc_drills` populates it from `mounting_hole` features, unplated
(a mounting hole is mechanical, never a soldered lead — which is what makes
it NPTH-eligible under the rule's own filter). The three existing consumers
of the drill shape (`check_npth_clearance`, `gerber.excellon_files`,
`padplace.place_footprint_pads`) were checked first and **already agreed**
on `{x, y, dia_mm, plated}` — no drift, no third spelling. **Now fires:
`npth_clearance: 1` on board two, seed 4.**

Deliberately left out, and this is the right call: a through-hole
component pad's own drill. DRC's pad source is `realize.pads_for_ir`, the
IR path, which carries no through-hole concept at all. Pulling drills from
`padplace`'s cached-footprint source instead would reintroduce exactly the
two-sources-of-pad-truth drift that path was chosen to avoid. It is a real
separate gap in `realize.py` — related to the paste finding above, where a
pad also could not say it was through-hole.

**`courtyard_overlap` read a flat 1.0mm.** Now reads the same derived
per-part radius placement legality uses. `check_courtyard_overlap` itself
is unchanged — it still measures between instance **centroids**, because a
courtyard is the part BODY and pads sit inside it; only the radius source
moved. Reads 0 on both boards, which now means placement's hard legality
is being independently verified rather than rubber-stamped by a laxer
check.

**Measured and REVERTED, and the measurement is the point (2026-08-29):**
`ir.instance_pad_radius` — which the placer's hard legality uses — derives
from pin OFFSETS and ignores pad SIZE, so the placer packs as though every
pad were a point. Fixing that is unambiguously more correct and it
**regresses the acceptance board**, so it is not landed:

| bound | seeds at 0 DRC / 11-of-11 |
|---|---|
| offset only (shipped) | 5 of 5 |
| offset + pad enclosing circle | 0 of 5 (2-7 unrouted per seed) |
| exact far-corner `hypot(|dx|+w/2, |dy|+h/2)` | 3 of 5 |

Every finding at every bound was `unrouted` — **zero** clearance,
courtyard or connectivity errors throughout. So placement legality was
never the confound: the keep-out really is too loose, tightening it
correctly and tightly still spreads the board past what `pcb/maze.py` can
absorb at the current iteration/schedule budget on 2 of 5 seeds. **That
names a router capacity limit nothing else has named**, and it is worth
more on the books than a quietly-shrunk constant would have been. The
per-seed numbers live in `instance_pad_radius`'s own docstring so a
future attempt starts from them.

Related, now answered: `landpattern._TIGHT_FRACTION`/`_LONG_FRACTION`
were reduced 0.35/0.65 → 0.25/0.45 when real pad sizes first congested
the fixture, and the open question was whether honest placer legality
would make the larger values viable again. **It does not** — measured
with the fix applied, 0.35/0.65 is *further* from viable than before
(10-16 findings per seed, 3-5 of 11 routed). Same router capacity
ceiling, approached from the other side.

**Follow-up, filed:** that unification left the keep-out formula
`max(instance_pad_radius[i] + _PAD_BREATHING_MM,
COURTYARD_MIN_SEPARATION_MM/2)` written out in **three** places —
`OptimizeEngine`, `seed_placement`, and the DRC handler, the last
importing a private constant across a module boundary. Collapse to one
`ir.instance_keepout_radius_mm`.

### The annealer silently DEMOTES an authored ground plane (2026-08-29)

**Found by rendering the board and looking at it**, which is now three for
three on defects no check could have caught. Declared GND on `In1.Cu` and
VCC3V3 on `In2.Cu` through the real tool surface
(`put(args={'op':'plane_net', ...})`), placed, routed, rendered the fab
SVG — and the two plane layers carry **26 via barrels and zero poured
copper**. No ground plane. The declaration reached the database and never
reached the board.

Isolated in six lines, no DB, no router: build the IR, `promote_plane`
GND and VCC3V3, run `optimize(iters=3000, seed=1)`, read
`net_plane_layer` back.

```
BEFORE anneal: {'VCC3V3': 2, 'GND': 1}
AFTER  anneal: {}
```

**`PLANE_DEMOTE` is offered every plane-promoted net with no regard for
where the assignment came from.** It is already measured that this cost
model dislikes planes — 79 `PLANE_PROMOTE` moves proposed over 3000
iterations, all rejected on cost — so the mirror-image fact was
inevitable and nobody looked for it: every `PLANE_DEMOTE` on an authored
net is *accepted*, immediately and permanently.

`pcb_route`'s write-back is careful never to overwrite the authored row,
and its comment states the intent plainly: *"an authored row is a standing
instruction the optimizer is free to explore away from during search but
whose PERSISTED value this job must never overwrite."* That reasoning is
what makes the bug invisible. Realization runs on the **post-anneal IR**,
not on the persisted row — so "free to explore away from" is not a
temporary excursion, it is the final answer. The persisted row is
protected and inert: correct in the database, absent from the board, and
re-read on the next run only to be demoted again.

**A declaration is a constraint, not a hint.** When the LLM says "GND is
the plane on In1.Cu" it is supplying exactly the domain judgment the
search cannot derive — see §"Placement quality is a ratsnest problem".
Fix: authored plane nets are **locked** — excluded from `PLANE_DEMOTE`,
and from `PLANE_PROMOTE` onto a different layer. Optimizer-*derived*
assignments stay fully explorable; only the human's do not. Needs an
`OptimizeConfig` field carrying the locked net ids, plumbed from
`pcb_route` which already computes `authored_net_names` for the
write-back and can reuse it unchanged.

Note what this does NOT fix: the cost model still has no reason to *want*
a plane, so `PLANE_PROMOTE` will keep being rejected and a board with no
authored declaration still gets no planes. That is the cost-model gap
already recorded. Locking makes the declared case work, which is the case
that matters, because deciding which nets are power and ground is a
judgment call and not a search problem.

### Solder paste, and the pad that could not say it was through-hole (2026-08-29)

`export_gerbers` emitted copper, mask, silk and outline but **no solder
paste** — the stencil film, missing from every fab set the system has ever
produced. Added as `gerber.solderpaste_gerber` (+ `F_Paste`/`B_Paste` in
the viewer's stack, off by default so it does not read as a recolouring of
`F_Cu`).

Writing it found a real gap immediately, which is the argument for adding
the film rather than deferring it. Paste must skip through-hole pads —
paste printed over a plated hole falls through it — so the filter is
`pad.get("drill")`. **It never fired.** `padplace.place_footprint_pads`
splits a THT pad into a pad row and a `model["drills"]` row, and the pad
row keeps only `net/shape/x/y/w/h/layer`. So the pad rows encode the
*consequence* of being through-hole (they land on every copper layer) while
discarding the *cause*, and nothing downstream can ask a pad whether it is
plated-through. Fixed by carrying `drill` on the pad. The tempting
alternative — matching a pad's coordinate against `model["drills"]` — is
re-deriving a fact we chose to throw away, at rounding-tolerance risk.

Detection method: writing the first consumer that needed to ask the
question. Nothing before paste had ever needed to distinguish SMD from THT
at the pad, so nothing had noticed the pad could not answer.

Deliberately NOT built: a global paste-aperture shrink. Stencil houses
reduce fine-pitch apertures 5-15%, but the correct reduction is a function
of pitch, stencil thickness and aspect ratio, none of which this model
carries. A single constant would be a number tuned to nothing — the exact
defect class this subsystem keeps producing. 1:1 and say so.

### PIN_SWAP: persisted, and still structurally unable to fire (2026-08-29)

Closed the netlist-divergence hole. New `pcb_pin_swaps` table (migration
`0141`), deliberately a **derived-assignment table rather than a rewrite of
`pcb_netconns`**: `pcb_netconns` IS the authored fact and enforces "a
physical pin is on at most one net", so rewriting it in place would destroy
the human's wiring with no way to tell a search result from it afterwards.
Same `meta.source` authored/derived discipline `pcb_planes` established.
Both jobs re-apply on a fresh `build_ir`; only `pcb_route` writes back,
because only `pcb_route` can create a swap.

**Fixed in passing, and it is the more consequential half:** `pcb_place`
was not re-applying persisted **plane** assignments at all. An authored
`op='plane_net'` declaration was invisible to the placement anneal, which
optimized as though GND were an ordinary high-fanout signal net that wants
to be short; routing then applied the declaration to a placement chosen
under the wrong model. `inst_rot`-class, one rule two call sites.

**Still open, and it should be decided rather than left:**
`OptimizeConfig.pin_swap_groups` defaults to `()` and **nothing in `src/`
ever sets it** — `pcb_route` constructs `OptimizeConfig(iters, seed)` with
no groups. So PIN_SWAP is dead by construction while carrying a 0.2 weight
in the late-stage `DEFAULT_SCHEDULE`. Firing it for real needs a
caller-supplied pin-equivalence set per instance (datasheet domain
judgment `pinswap.py` deliberately refuses to invent) and a schema place
for it (footprint pin-function data, which does not exist). Either wire
those and add an `op='pin_swap'` authoring verb, or take PIN_SWAP out of
the schedule. The persistence path is now correct and tested either way —
it is no longer the blocker.

### BOARD TWO: the benchmark experiment has been run (2026-08-29)

§Obligations item 3 has been discharged as a *measurement*. The second
reference design exists: `tests/fixtures/pcb/motor_power_reference.json`
— a 21-part buck regulator + H-bridge motor driver, pinned structurally by
`tests/test_pcb_second_reference_design.py` and driven through the real
tool surface by `tests/test_pcb_second_reference_end_to_end.py`.

Structurally different from board one along axes board one never touched:
**3.0-3.5A nets** (board one's worst is 0.5A, near the fab floor), **eight
through-hole parts** (board one is 100% SMD), an authored **bottom-side**
part, two `mounting_hole` features, a **concentrated** net-degree profile
(GND/VIN/VM = 47% of all connections), and a **70×50mm outline** rather
than board one's flagged 300×300mm-for-44mm-of-parts.

**First-run result, seeds 1-5** (DRC errors and routed count always
together — routing nothing satisfies the first one alone):

| seed | DRC errors | breakdown | routed | disconnected | runtime |
|---|---|---|---|---|---|
| 1 | 14 | connectivity 6, unrouted 8 | 6/14 | 6 | 13.2s |
| 2 | 12 | connectivity 6, unrouted 6 | 8/14 | 6 | 8.4s |
| 3 | 11 | connectivity 6, unrouted 5 | 9/14 | 6 | 13.9s |
| 4 | 12 | connectivity 6, unrouted 6 | 8/14 | 6 | 9.8s |
| 5 | 15 | connectivity 6, unrouted 9 | 5/14 | 6 | 10.5s |

**The answer to the A-vs-B question is the ratio, and the ratio is
lopsided in a way that supports BOTH halves of the plan's own prediction.**
Classified: **0 wrong-constant, 3 missing-invariant, 2
one-rule-two-call-sites, 1 unclassified.**

- **Zero** new instances of "a magic number tuned to board one". Pin
  geometry, placement legality and the maze router's clearance guarantee
  all transferred cleanly to a structurally different board. That is the
  user's explanation B, confirmed, on exactly the class B predicted would
  vanish.
- **Full recurrence** in the classes B predicted would persist, because
  neither the missing invariant nor the shared function was ever built.
- Plus **one failure mode neither framing names**, and it is the largest
  number in the run — see below. It exists only because board two combined
  realistic current with realistic density, which no re-run of board one
  could have produced. That is evidence for A's general claim, and it cost
  a new board to find.

So: not a wash, and not a clean win for either. The honest statement for
the paper is the ratio above plus the observation that **the classes split
by whether a shared definition exists**, not by how fast the system was
built.

#### What board two found

1. **Every current-annotated net fails to route, 100% of the time, on
   every seed — with no diagnostic at all.** GND, VIN, VM, OUT_A, OUT_B
   always fail. Causally isolated, not guessed: same board, same topology,
   only the current annotation changed — **0 unrouted at 0.3A** (widths
   0.09mm), **25/25 segments unrouted at 3.0-3.5A** (widths 1.37-1.69mm).
   `RealizeResult.warnings` is **completely silent**, because the
   `gap-capacity` channel only covers "found a path but it is tight" and
   never "found no path at all". A caller sees net names with no reason.
   Two things are true and both need fixing: the width itself is wrong
   (per-net current applied uniformly to every segment of a star from one
   hub pin over-widens every leaf and stacks multiple full-width branches
   at one physical pin — **this is precisely the failure decision 3b
   predicts and 3b is unbuilt**), and total routing failure produces no
   explanation, which is the "fail legibly" gap in its sharpest form.
2. **`check_npth_clearance` cannot ever fire.** It reads
   `model.get("drills")`; `handlers/pcb.py::_render_drc` builds a model
   with `layers`/`copper`/`pads` and **no `drills` key, ever**. Board two
   authors two mounting holes and gets 0 findings — as the code predicts
   regardless of design content. Same always-zero-estimator family as
   crossings and via_count: wired into `run_geometric_drc`, structurally
   dead.
3. **`courtyard_overlap` still uses a flat `DEFAULT_COURTYARD_RADIUS_MM =
   1.0`** while placement legality uses the real per-part
   `ir.instance_pad_radius`. One rule, two call sites, drifted — the
   recurrence the plan predicted. It reads as 0 findings on both boards,
   and **that zero means the check is dormant, not that the geometry is
   right**: the flat 1.0mm is smaller than any real part's derived
   keepout, so placement always separates parts further than DRC would
   flag. A TO-220's real several-mm courtyard is checked against nothing.
4. **`pcb_instances.layer` is dropped by `ir.from_graph`** — persisted,
   round-tripped through `pcb_graph`, then never read. `PcbIR` has no side
   array. Board two authors `C3` on the bottom deliberately, so this is now
   concrete rather than theoretical: C3 is an ordinary top-side instance to
   the placer, the router and DRC. This is `pcb-residual-defects-0828.md`
   §1, confirmed against a design that actually uses it.
5. **The tool surface hard-blocks non-4-layer boards; everything under it
   supports 2-layer.** `_enqueue_op` raises `BadInput` on
   `len(stackup) != 4`, but `pcb_route._process_for_stackup` handles `n==2`
   and `pcb_capabilities.json` has a real `2layer` row. Verified by
   bypassing the gate: place + route + DRC ran to completion, 7/15 nets
   realized (worse, as expected — no plane relief, every crossing costs a
   via). The v1-scope decision is real; the *message* misleads, because a
   caller reads it as an engine limitation.
6. **Not root-caused:** in the 2-layer run, the genuinely dangling net
   `VM_TP` (1 connection, should be one pad and no copper) is reported by
   `check_connectivity` as 2 disconnected pieces ~20mm apart. `pads_for_ir`
   emits one pad per pin, so two disjoint witnesses for a single-pin net
   implies a second primitive is being tagged with this net name, or a
   net-identity mixup in the connectivity model build. Flagged rather than
   guessed at.

### Placement quality is a ratsnest problem, not a cost-weight problem (user, 2026-08-29)

Recorded as the user's direction, not yet built. Observation that prompted
it: on the rendered reference board, connected parts sit scattered and nets
run right across the outline. The reflex is to reach for cost weights. The
user's call is that this is the wrong lever:

> *Placement quality is something we can fix with the layered rubber bands.
> If the LLM tells us what power and ground nets are we can minimize the
> rest at rubber-band ratsnest time.*

Two claims, and they compose:

1. **The layered rubber-band engine is where placement quality comes from.**
   Not a wirelength weight bolted onto the annealer — the rubber bands *are*
   the objective, pulling connected parts together as a physical consequence
   of the representation. This is decision 1 ("the in-house layered
   rubber-banding graph engine is the system") reaching placement, and it
   subsumes the deleted `place.py` `W_LEN` term rather than reviving it.
2. **Power and ground must be DECLARED, not inferred, and then excluded from
   the pull.** A 200-pin GND net contributes a star that dominates every
   other force and drags the whole board onto its hub — the classic
   fanout-dominance failure. But the fix is not fanout normalization
   (decision 3b already deletes that): it is that GND and VCC *do not want
   to be short*. They want to be a plane. So the ratsnest that the rubber
   bands minimize should be **the signal nets only**, with the declared
   power/ground nets removed from it and handed to the plane/pour path
   instead.

**Who declares.** The LLM caller does — it is exactly the kind of judgment
the agent surface exists to carry, and it is not reliably inferrable from
geometry (name heuristics on `GND`/`VCC`/`3V3` are a guess, and a board with
two isolated grounds breaks them). This is the same declaration the pour
path needs to produce actual ground planes, so it pays for two things at
once and should be built once, on the net row, not twice.

**Watch the decap trap** (already recorded under §"The findings that change
the design"): excluding plane nets from `w_ij` makes a decap — whose only
nets are PWR and GND — an isolated vertex with nothing pulling it anywhere.
Exclusion from the *ratsnest* must not mean exclusion from *placement*; a
decap's placement constraint is proximity to the pin it decouples, which is
a different (and stronger) relation than the net star it currently rides on.
Do not ship the exclusion without answering this.

### Viewer + geometry-quality direction (user, 2026-08-29) — 1,2,3,5 shipped, 4 open

Shipped: layer-selector SVG rendered from the gerbers
(`pcb/gerber_view.py`, `view='svg' args={'level':'fab'}`), copper pours
(`pcb/planes.py`), per-layer preferred directions
(`maze.preferred_directions`), and a straighten pass
(`realize._straighten`). Item 4's **via shoving** is NOT done — the
straighten pass treats a via as a fixed point, because moving one moves a
hole.

**Measured on the reference board, seeds 1–5** (all with 0 DRC errors, 0
unrouted, 0 disconnected nets throughout — neither pass costs correctness):

| | segments | copper | vias |
|---|---|---|---|
| baseline | 421 | 568mm | 24.4 |
| + preferred directions | 307 (−27%) | 551mm | 29.8 (+22%) |
| + straighten | 132 (−69%) | 536mm (−6%) | 29.8 |

The via increase is the honest price of alternating H/V layers: crossing
the grain now costs a layer change, which is the trade every VLSI router
makes and the reason free space stops fragmenting into islands. If it ever
looks too expensive, `maze.OFF_AXIS_PENALTY` is the dial and
`RealizeConfig.preferred_directions=False` is the off switch.

`_straighten` can only ever REMOVE copper: it drops an interior vertex
only when the chord between its neighbours tests free on the same
occupancy grid that proved the original path clear, at the same radius the
search itself queries with (own half-width + a cell — NOT
`core_radius_mm`, which already contains the other net's clearance and
would double-count it).

The original list, for the record:

1. **An SVG with a layer selector**, doubling as the basis for a web
   viewer later.
2. **Render the SVG FROM THE GERBERS, not from the model** — "see the bugs
   that go to manufacturing". This is the right call and this session
   produced the evidence for it unprompted: `svg.render_board` applies a
   per-layer `stroke-dasharray` (`svg.py`'s `_DASH_PATTERNS`) as a
   colour-independent layer cue, so B.Cu traces render as **dashes**. Time
   was then spent proving the copper was continuous (it is) because the
   picture could not distinguish decoration from a real gap. A view that
   is stylistically different from the artefact cannot verify the
   artefact. Render the gerbers and the question never arises.
3. **Pours / "in-between-traces polygons"** — none exist. `planes.py` was
   always a later module; a plane-promoted net gets a dogbone stub and no
   copper is ever poured. (Note for whoever picks this up: on seed 1 no
   net was actually plane-promoted, so the board has no stubs either.)
4. **A straighten-and-shove pass** over the routed result, with **vias
   allowed to move slightly** so a wire can go straight. The octile grid
   leaves staircases: 482 segments over 81 tracks, median segment 0.41mm.
   Post-route straightening against the same occupancy grid is a natural
   fit — the grid already answers "may this copper be here".
5. **Per-layer preferred direction** (primarily horizontal / vertical /
   diagonal), because unstructured routing on every layer fragments the
   remaining free space into unusable islands. This is the standard VLSI
   preferred-direction discipline and it is the cheapest of the five:
   a per-layer penalty on off-axis steps inside the A* step cost. It
   should improve both routability and pourability, and it composes with
   (3) — coherent corridors leave pourable space, staircases do not.

Suggested order by value/cost: **5, then 4, then 3, then 2+1 together**
(the viewer wants something worth viewing first) — but 2 is cheap and is
the thing that makes the rest verifiable, so taking it early is defensible.

### Fab output: pads now reach the gerbers (2026-08-29 late)

The "no pads, header-only mask" defect below is closed. `realize.
pads_for_ir` is the single pad source; `_render_gerber` falls back to it
when the footprint cache is empty (it used to emit nothing and ship a
well-formed unsolderable board), and `gerber.export_fab` **refuses** a
model whose pads are synthesized (`SynthesizedPadError`) because
`landpattern.py` says those are bounds that must never be fabricated.
Round-tripped and measured on the reference board: F_Cu 99 flashes, F_Mask
81 flashes with the correct `SOLDERMASK_EXPANSION_MM` swell, PTH 18 holes.

**Both of the gaps this section used to name are now closed (2026-08-29).**

**Silkscreen** exists: `pcb/stroke_font.py` (a hand-authored single-stroke
font, no font files, no new dependency) + `pcb/silk.py` (`build_silk`),
emitting a refdes label, a body outline sized from the part's OWN land
pattern, and a pin-1 tick per instance. Silk is checked against real pad
geometry and **relocated, then dropped, rather than printed over a pad** —
a fab scrapes silk off pads, so text under one is silently lost. Drops and
relocations are returned, never swallowed. One builder, called from both
`_render_gerber` and `_render_fab_svg`, per the `pads_for_ir` precedent.

*And a defect the render caught immediately:* a part rotated 180° got
upside-down text, one at 270° got text running top-to-bottom. Silk exists
to be READ; every EDA tool folds text rotation into a readable half-turn
and this one now does too (`silk.readable_text_rotation`, KiCad's
`(-90, 90]` rule). Only the glyph orientation folds — the label's anchor
still follows the footprint.

**Real pad sizes** exist: `ir.pin_w`/`pin_h`/`pin_shape`/
`pin_pad_synthesized` from `landpattern.sizes_for`, merged by
`realize.pad_geometry` (real cached footprint if available, honestly
labelled synthesized bound otherwise). Router, DRC and the gerber preview
all read it; `maze.PAD_RADIUS_MM` is demoted to a documented last-resort
fallback with no production reader. The synthesized-pad **refusal stays**
— a bound is still not a measurement.

Two threads left dangling by that work, both recorded so they are not
lost:
- **Real footprints still do not reach the IR.** `pad_geometry` accepts a
  `footprints=` kwarg and nothing passes one, because
  `Store.pcb_footprints_for` keys by LCSC part number and `PcbIR` has no
  `part_lcsc` field to remap by. So every pad on both reference boards is
  a synthesized bound today.
- **The synthesized-bound fractions were tuned against the fixture.**
  `landpattern._TIGHT_FRACTION`/`_LONG_FRACTION` went to 0.25/0.45 after
  0.35/0.65 regressed the acceptance board to 10 DRC errors. That is a
  defensible choice among plausible bounds and it is documented with its
  numbers — but the suspected cause of the congestion is that
  `ir.instance_pad_radius` (which the placer's legality uses) still
  derives from pin OFFSETS and ignores pad SIZE, so the placer packs as
  though every pad were a point. Fix that first, then re-measure whether
  the larger fractions become viable; the answer decides whether the
  tuning was engineering or a workaround.

### Fab output: the original diagnosis (2026-08-29 morning)

`export_fab` runs on the routed board and produces a well-formed 10-file
set (4 copper + 2 mask + 2 silk + edge cuts + PTH drill, 7 KB zipped).
**It is not a board yet**, and the reason is one thing:

- **No pads.** Soldermask and silkscreen gerbers are header-only (10 lines,
  zero apertures) and no pad copper is flashed anywhere. The traces connect
  to nothing. A mask with no openings is an unsolderable board.
- **The cause is a second, disagreeing source of pad geometry.**
  `handlers/pcb.py`'s gerber path builds pads with
  `padplace.board_pads(...)` off the **cached footprint table**
  (`pcb_footprints_for`, keyed by `part_lcsc`), while the IR, the router and
  the DRC all use **`landpattern.offsets_for`'s synthesized offsets**. The
  reference design has no LCSC parts, so the footprint cache is empty, every
  placed part lands in the handler's `missing` warning, and no pads are
  emitted. Worse than the absence: if the cache *were* populated, the fab
  output would flash pads at coordinates the router never routed to. Same
  one-rule-two-components shape as the outline drift. Fix by making the
  synthesized land pattern a fallback *inside* the pad source, so there is
  exactly one answer to "where are this part's pads".
- Silkscreen is hardcoded `{"top": [], "bottom": []}` — genuinely not
  implemented (no silk table). Refdes and part outlines are missing.
- No NPTH drill file (no mounting holes in the model); PTH carries the 20
  via drills only, which is correct for an all-SMD board.
- BOM/CPL (`view='gerber'` + `fmt='bom'|'cpl'`) read the `export_model`
  shape, a **third** model dict, keyed on `instances`. Wired, untested
  against a routed board here.

### Still to do on the router

1. **Rip-up and retry / negotiated congestion** (PathFinder, Ebeling &
   McMurchie 1995). Routing is one pass, shortest-first; on some seeds that
   strands ~20 connections. This is the single biggest routability lever and
   it does not touch the clearance guarantee, which the grid enforces
   independently of search order.
2. **Segments are a STAR from `member_pins[0]`** (`ir.from_graph`), so all 26
   GND segments share one endpoint. The router's attach-to-own-copper
   sourcing mitigates this but a real spanning tree / Steiner decomposition
   (plan §3b) would cut length and vias.
3. **Plane dog-bone stubs are DRAWN, not routed.** `_realize_maze` hands
   plane-served segments to `realize_segment` (the tangent drawer) and then
   claims the result, so a stub is guaranteed to be avoided by everything
   routed after it but is not itself checked against anything already
   there — including another plane net's stub. On the reference fixture
   this shows up only as a handful of `clearance` **warnings** (the house
   tier, 0.09–0.15mm; zero errors), but the mechanism can produce an error
   on a tighter board and the warnings are the visible edge of it. Fix by
   routing the stub through the grid like any other connection, or at
   minimum by rejecting a stub whose corridor is already claimed.
4. **Pad extents are a constant** (`maze.PAD_RADIUS_MM = 0.2`). Real pad
   geometry belongs in the land pattern next to the offsets. Note that
   pads are not in the DRC model at all today (`_render_drc` builds from
   `pcb_copper`, which holds tracks/vias/pours), so pad-to-track clearance
   is currently enforced by the router and checked by nobody.
5. **The outline clip is a bounding box** — copper can land inside the bbox
   but outside a concave board. Consistent with `cost.outline_bbox` and the
   placer, so at least uniformly wrong; fix when a non-rectangular board
   first matters.

### Process lesson, worth more than the fix
Three agents were run concurrently in one worktree and clobbered each other
— one hit a `NameError` from a sibling's half-written edit and its
measurement became unattributable. Serialize agents that share files, or
give each its own worktree.

## SILKSCREEN: decisions from the 2026-08-29 render review

Found by rendering the board and looking at it — none of it by reading code
or by any test. Do these in order; the first is a prerequisite for the rest.

1. **Structured per-instance silk census, replacing free-text drops.**
   `build_silk` reports drops as prose (`"C2: courtyard outline overlaps a
   pad or via -- dropped"`) and only for SOME failure modes. Measured on the
   reference board: **7 courtyards drawn for 29 parts, with only 2 drops
   reported** — ~20 silent omissions. `SilkResult` must carry, per placed
   instance, three outcomes (courtyard / pin-1 / label), each `placed` or a
   REASON CODE. The render warnings and the DRC rule below are both
   generated FROM that structure, so there is one source and absence stops
   being inferred from silence.
2. **Two rules, not one — PRESENCE and PRINTABILITY** (user, 2026-08-29:
   "drc fail for lack of courtyard line printability and label printability").
   - **`silk_missing`** — presence. Needs item 1's census; a rule reading
     today's free-text drops would only see the ANNOUNCED subset and would
     certify a board with ~20 silent omissions as clean.
   - **`silk_printability`** — is what WAS placed manufacturable: stroke
     width below the fab minimum, silk overlapping a soldermask opening (it
     will not adhere and contaminates the pad — the user's ENIG question),
     silk crossing the board edge.
   **`silk_width_mm` and `soldermask_dam_mm` in `capabilities.py` have ZERO
   consumers** (found by the dead-export sweep the same day). The
   printability rule is precisely what consumes the first, and a mask-dam
   rule the second — the dead fab fields and the missing rules are the same
   gap from opposite ends: capability data ingested for checks nobody wrote.

   Original note on severity (user: "should DRC fail, or at least have a
   rather high cost"). One finding per instance per missing element,
   severity `error` for a dropped refdes — a board you cannot identify parts
   on is one you cannot assemble or inspect. Note `drc.py` has **zero**
   mentions of silk today (11 rules, none about silkscreen); silk avoidance
   is enforced only at CONSTRUCTION time in `build_silk` and never verified.
   Wiring: `handlers/pcb.py::_board_furniture` already builds silk once for
   both render paths — feed DRC from there rather than letting DRC rebuild
   silk and the two drift.
3. **Labels go OUTSIDE the courtyard** (user). The default candidate is
   currently centred INSIDE it, so J1's refdes will be invisible once the
   connector is soldered. This DELETES a special case rather than adding
   one: the courtyard's solid bbox is presently withheld from its own
   instance's label search and folded in only afterwards, purely to let the
   centred candidate survive. Outside-only makes it an ordinary obstacle for
   everyone. **Expect the measured placement rate to DROP** — labels under a
   part body were being counted as placed while being unreadable; the worse
   number is the honest one.
4. **Pin-1 must be able to say it does not know.** `_pin1_id` looks for a
   pin literally named `"1"` and otherwise falls back to `min(pin_id)` —
   whichever pin was ingested first — then draws the tick with full
   confidence. Measured: **22 of 29 parts have a pin named `"1"` (all
   passives, where orientation is irrelevant); the 7 that do not are U1, U2,
   U3, D1, D2, J1, J2** — exactly the parts where pin 1 matters. U1's pins
   are `3V3/GND1/EN/BOOT/...`; J1's are `P1..P6`, so one character decides
   whether the marker is a fact or a guess, and the output is identical
   either way. **A wrong pin-1 marker solders a part backwards — strictly
   worse than no marker.** Recognise real spellings (`1`, `P1`, `A1`), and
   skip the tick when unknown.
   - **A diode has no pin 1.** `D1`/`D2` are `A`/`K`: polarity needs a
     CATHODE BAND, a different silk primitive. A corner tick on a two-pin
     polarised part communicates nothing.
5. **Placement cost proxy for un-drawable silk** — the deeper fix, after the
   above. Making the placer pay stops the problem at source. **Trap:** silk
   placement is a per-part search and the annealer runs thousands of
   iterations on an incremental dirty cascade, so running real silk in the
   loop is both slow and breaks incrementality. Use free area in each part's
   label candidate ring, which depends on neighbour positions exactly as
   `gap_capacity` already does — a term that can actually fire, unlike the
   alignment rewards this engine has already shipped twice.

**Decorative silk: DEFERRED (user, 2026-08-29) — do not let it hold up items
1-5.** Board art (image underlay, logos, exposed-copper design work) is not
urgent. Recorded here only so the eventual retrofit is a known, priced cost
rather than a surprise: the `silk_missing` rule in item 2 counts PER-INSTANCE
elements and treats absence as an error, and artwork has no instance — so
whenever art does arrive it needs a silk role that is a DRC *participant*
(respects keep-outs) without being a DRC *subject* (can never be "missing"),
which means amending that rule rather than adding a struct field. Cheap
either way; just not free.

**Co-generation (user, 2026-08-29): art as a PARTICIPANT in placement, not
an overlay.** Bind refdes to named artwork anchors ("left eye") and add an
`art_anchor` cost term = Σ distance(instance, its anchor). Two properties
make this worth doing here rather than in an external tool:
- **It can actually fire.** A distance penalty is continuous with a gradient
  everywhere — the opposite of the alignment and concentric-arc rewards
  killed on this same day for being measure-zero. It is also CHEAPER than
  the existing terms: it depends only on the instance's own position, so the
  dirty cascade handles it trivially (`gap_capacity` must re-search
  neighbours).
- **It makes the art/function trade a measurable number** instead of a
  workflow: turn the weight and read back the extra vias, trace length and
  unrouted nets. An overlay pipeline cannot report that.
Routing side: the tractable form is CORRIDORS (regions where routing is
cheap, so traces flow along the image's contours) or authored traces as a
fixed feature; traces-as-art directly is not in the maze router's
vocabulary. The 2026-08-29 fillet work is the aesthetic vocabulary for both.

**CO-generation, where the art is a free variable too (user: "make this
circuit and a dragon face could co-run").** Three shapes, very different
costs:
- **Parametric art is nearly free.** Eye spacing, jaw width, horn angle
  become continuous variables in the annealer's own state vector alongside
  positions and rotations; one anneal optimises art and circuit together.
- **Generative-model art is an expensive OUTER loop** — no image model can
  live inside an annealer running thousands of iterations. It degrades to
  generate → place → measure → regenerate, minutes per round trip. Decide
  which of these two we are building BEFORE starting; they share almost no
  machinery.
- **Best of all, possibly no optimisation at all:** choose art whose
  primitives are already the board's primitives — scales that ARE the
  ground-plane hatch, whiskers that ARE traces, a pupil that IS a via
  cluster. Art that fights the medium needs heavy co-optimisation; art
  chosen to fit it needs almost none.
**Division of labour this stack is already shaped for:** "which component is
the eye, which net is a whisker" is SEMANTIC BINDING — the LLM layer's job,
proposed once, not the annealer's. The cost function then optimises geometry
given the binding. No model in the inner loop.

**Traces following a direction field (user: "follow the direction of hair in
the image") — deferred, but cheaper than it sounds.** `_layer_preferences`
already gives each layer a preferred routing direction, which IS a direction
field, just a constant one. This is the same per-step cost bias sampled per
CELL instead of per layer; the orientation field comes from the structure
tensor of the image gradient. The bounded change is the sampling, not the
mechanism.
- **Conflicts with the requested up/down/left/right + 45° restriction**, and
  the reconciliation is to QUANTISE the field to the allowed direction set —
  traces then step along the flow in 45° increments, which likely reads as
  deliberate rather than melted. Decide the two together; shipping the
  direction restriction without knowing this is coming would bake in a
  constant-direction assumption.
**Sequence it after the ratsnest/rubber-band placement work** — adding an
aesthetic objective to a placer that is not yet reliably meeting its
ELECTRICAL objectives means the art term gets blamed for pre-existing
placement quality problems.

Direction when it happens: artwork belongs in the board's existing
`features` list (`ftype: "outline"` is already there, so `ftype: "artwork"`
targeting a named layer set fits), with **gerbolyze as an OFFLINE
converter** whose output is stored as geometry — it is AGPL-3.0 and C++, and
this repo is a network-served MCP server with a container deploy that has
been bitten twice by optional/binary dependencies. Offline keeps both out of
the serving path. Build our own only for TEXT (labels, boxes, knockouts):
that geometry has to stay attributable for the census, and the primitives
(`G36`/`G37` regions, `%LPC*%` clear polarity) already exist in `gerber.py`.
Note also that ~5 tones (bare laminate, mask-over-copper, mask-over-laminate,
exposed copper, silk) are available with NO vendor feature — that is what
most PCB art actually uses. JLC's multi-colour silkscreen is unverified here
and must be sourced from their live capability data before it enters a spec.

Also observed and NOT yet filed as its own item: silk placement has no
concept of the component BODY as a readability keep-out (it avoids pads and
vias only), which is the mechanism behind item 3; and no part in
`esp32c3_reference` has a real footprint (`footprint: None` for all 29, every
pad synthesized, all 57 drills are vias and there are zero component drills)
— so courtyards, pad sizes and body outlines on that board are BOUNDS, not
data, and `export_fab` already refuses to call it manufacturable.

## The drawn courtyard is not the checked one (2026-08-29, half-closed)

Root-causing the 53 `silk_missing` findings turned up something bigger than
the bug it was chasing: this subsystem had FIVE independently-computed
notions of a part's courtyard, and the one drawn on the board was not the
one enforced.

**Closed 2026-08-30.** There is now one courtyard shape,
`ir.instance_courtyard_polygon` — the hull of the part's own pad outlines,
offset outward — and the placer reserves it, `courtyard_overlap` and
`outline_containment` check it, and `silk.py` draws it. The three differ
only in the clearance they offset by, which is a real difference (router
escape room vs. fab ink clearance) rather than drift. The circle path
(`instance_keepout_radius_mm`, `instance_pad_radius`) is deleted. What
remains is that the anneal's GRADED cost term still steers by a flat
2.0mm centre-distance; that one open question lives in
`docs/backlog/pcb-courtyard-polygon.md` — go there, not here.

Two findings from this investigation are worth keeping because they are
traps, not status:

**The drop predicate is CORRECT and must not be narrowed.** 18 of the 22
courtyard drops were a part colliding with its OWN pad. Making the check
ignore a part's own pads would have cleared 21 of 22 without moving a
single line of silk — the fab would still print ink on copper, now
invisible to DRC. The fix was to make the collision unrepresentable
(derive the shape FROM those pads), not to stop looking.

**A tighter courtyard finds MORE vias, not fewer.** The oversized square
was so large it *enclosed* a part's plane fan-out vias; an honest polygon
passes through them instead. Shipping the polygon alone therefore made
`silk_missing` worse (27 → 32) until outlines learned to break around an
obstacle rather than drop whole. Anyone tightening a keep-out here should
expect the same sign flip.

## OPEN from the 2026-08-29 fable review of the unshipped branch

A `fable`-model review of `origin/main...HEAD` (26 commits) found six
defects. Two are FIXED (the fillet budget, `8034a79b`; the stale
`_board_furniture` ordering docstring, folded into the silk-census change).
The rest are open and are recorded here because a review finding that lives
only in a transcript is gone at the next compaction.

- **Fiducials are invisible to DRC** — `_board_furniture` folds fiducial
  pads into `model["pads"]`, but only on the gerber/fab-SVG paths;
  `_render_drc` builds its model from `_drc_pads` + `pcb_copper_list` and
  never sees them. A track passing near an outline-bbox corner ships a
  gerber with a 1mm copper dot (net `""`) flashed on top of it while
  `view='drc'` reports green. **Being fixed** as part of the silk census
  (the same change adds a `_board_furniture` call to `_render_drc`). The
  second half — `build_fiducials` checks candidates against pads and the
  title/SN bboxes but never against tracks, vias or pours — is still open.
- **Three collinear fiducials are reachable.** `_FIDUCIAL_CORNER_SIGNS`'
  comment argues any 3 of a rectangle's 4 corners form a right triangle,
  which holds only when all three use the SAME inset. The
  `for m in (margin_mm, margin_mm * 2.0)` fallback moves a blocked corner
  diagonally inward. Concrete: 12x12mm board, 3mm margin — corners at
  (3,3) and (9,9), third corner's 3mm spot blocked, its 6mm fallback lands
  at (6,6). All three on the main diagonal; the >=2mm spacing check passes.
  That reinstates exactly the 180-degree rotation ambiguity `FIDUCIAL_COUNT
  = 3` exists to resolve. `test_build_fiducials_are_non_collinear` cannot
  catch it — it only runs the unblocked 60x40 fixture, so the assertion
  exists but only over the easy input. Fix: a triangle-area check inside
  the placement loop, plus a corner-blocked small-board test.
- **All net-`""` pads are mutually same-net-exempt.** `pads_for_ir`
  deliberately writes `net: ""` for every NO_NET pin;
  `clearance_pairs_indexed` skips a pair when `net_i == net_j`, so two
  genuinely unconnected pads (test points, NC pins, mounting holes on
  different parts) at zero gap are never a clearance candidate. Needs a
  "both empty -> still check" carve-out, or an explicitly stated gap.
- **`_pcb_ops.py` binds the alias `p` twice in one statement** (scalar
  subquery `pcb_pins p`, plus the `LEFT JOIN parts p` added for
  `extended_part`). Legal — inner scope shadows outer — but anyone
  extending either side silently gets the wrong table. Rename one.

Design questions the review raised, for a human not an agent:

- `test_check_via_pad_keepout_fires_on_a_real_realized_via` pins that the
  **tangent router's own output fails the new `via_pad_keepout` rule**
  (`_vias_for_track` puts the k=0 via at the pad coordinate). Documented
  and asserted, but it means every tangent-routed layer transition is a DRC
  error by construction. Decide whether the tangent drawer should offset
  its vias.
- `_op_plane_net`'s conflict guard reads `pcb_planes_list`, which includes
  DERIVED rows — a stale optimizer-derived assignment blocks a human's
  authored `plane_net` until the next route job replaces it. Whether a
  human can evict a derived row without running a route is a workflow call.
- `stroke_font.text_bbox_corners` claims to be "exactly the box the glyphs
  are drawn inside"; `(`, `$`, `#`, `,`, `Q` legitimately overhang it (the
  module's own docstring says so). Title-block pad overlap is checked
  against real strokes, but outline containment uses only the 4 advance-box
  corners, so a descender can leak past a concave outline edge. Marginal at
  today's >=2mm margins.

Reviewed and explicitly found clean: `_quantized` bool-before-float,
`quantize`/`GERBER_UNIT_MM` single-sourcing, `_corner_fillet`'s near-0/near-pi
guards, the stroke-font conversion contract and both licence attributions,
`stamp_pad`/`via_clears_pads` (the prior vacuous-check defect is genuinely
fixed and producer/checker stay independent), the `net_plane_layers` bitmask
migration, and gerber X2 attribute round-tripping.

## AGREED QUEUE (2026-08-29, with the user) — do these in this order

Triaged out of a long representation thread; most of that thread was
**rejected** and the rejections are recorded below so they are not
re-proposed.

1. **Rounded corners (fillets)** — replace each interior vertex of a routed
   polyline with a tangent arc. Cheap because arcs are already first-class
   end to end: `gerber.py::_emit_stroke` emits `G02`/`G03` with `I`/`J` (and
   `G75` is already in the header), `svg.py` renders `shape: "arc"`,
   `drc.py` flattens arcs for clearance, and `realize.py::tangent_arc_path`
   already does closed-form tangency and signed sweep. A fillet is the
   EASIER tangency problem than the one already solved (tangent to two
   lines, not from two points to a circle).
   - **NOT purely subtractive.** The arc departs from the mitered centerline
     INWARD by `r·(1 − sin(θ/2))` — `0.29·r` at a right angle — which is new
     copper on the concave side where a pad or via may sit. Either clamp `r`
     so the deviation stays under half the trace width, or end the pass with
     a real `get(view='drc')` run. Do not assume a pre-fillet clean board
     stays clean.
   - Must be ONE shared function used by both corner-making paths
     (`tangent_arc_path` and the new maze-path filleter), not a parallel
     implementation.
2. **Quantise coordinates at the model boundary** — snap every coordinate to
   the gerber unit (`1e-6` mm) on entry to the model; keep float mm as the
   compute type. This is the ~10% of the int32-nanometre idea that carries
   most of its value: `gerber._u()`'s emission `round()` becomes a no-op, so
   the artefact IS the geometry the router computed, and exact-equality
   geometry (e.g. "do these two arcs share a center") becomes decidable.
   Choke point is `realize.py::to_gerber_model`.

Then **render a board and take user feedback** before continuing. Only after
that:

3. **Typed `ctype` + one net-id space, replacing string-keyed dicts.** Kills
   the defect family that produced five bugs on 2026-08-29 (a key read but
   never written yields `None` instead of failing). **This must BE the S9
   decision-6 vocabulary migration** (`pad/hole/conductor/keepout/body/
   marking`), not a fourth taxonomy alongside it — inventing a competing
   vocabulary is precisely why the DRC keep-out matrix was killed.
4. **One affine-transform path** for footprint placement, silk and text.
   Defect prevention, not speed — a refdes is three glyphs, nobody waits on
   that arithmetic. **Evidence gathered 2026-08-29, this is not speculative:**
   - `padplace._rotate_cw` returns `x*c + y*s, -x*s + y*c`.
   - `landpattern.rotate_offset` returns `dx*cos_t + dy*sin_t, -dx*sin_t +
     dy*cos_t`. The identical `[[cos, sin], [-sin, cos]]`, written twice.
   - Distribution is inverted: `rotate_offset` lives in the FOOTPRINT module
     but its callers are `silk.py::_place` and `stroke_font.py`
     (`layout_text`, `text_bbox_corners`) — text and silk rotate through the
     footprint module while footprint pads use padplace's private copy.
   - `padplace`'s docstring CLAIMED `_rotate_cw` was "the one place that
     matrix lives" (corrected 2026-08-29). A false uniqueness claim is worse
     than silence: a reader who checks it stops looking.
   - Mirror-before-rotate is implemented twice and pinned by two separate
     test suites asserting the same fact; `export.jlc_rotation` is an
     acknowledged third re-expression.
   - The composition (scale/mirror/rotate/translate/alignment) is duplicated
     per call site, and that is where the real bugs have been: `mirror`
     negating x about the ANCHOR not the board forced a `draw_h_align`
     compensation flag in `build_title_block`, and the bottom-side S/N path
     compounds two reflections. Composed matrices delete the ordering
     question rather than documenting it.

### Rejected in the same thread — do not re-propose without new evidence

- **Full int32-nanometre coordinates.** `int32` at 1 nm is ±2.147 m (not
  10 m — 10 m needs 34 bits). Fine for any PCB, but the change needs the
  `np.nan` "unplaced" sentinel replaced by an explicit `placed` mask, and
  every distance computation cast to int64 because **numpy integer overflow
  wraps silently** (`dx²` on a 100 mm board is `10¹⁶`, past int32 at
  `2.1·10⁹`). Item 2 buys most of the benefit for a fraction of the cost.
- **Bit-packing into `uint64`.** x+y at 32 bits each already consume the
  whole word, so nothing else fits. Unpack/repack beats the copy saving, and
  it destroys readability in exactly the test-failure and debugger output
  that has caught every bug here.
- **A universal fixed-width matrix** (`x, y, type, rotation, id`). Pours are
  variable-length; tracks need two endpoints; rotation is meaningless on a
  track. A column that is always zero for a structural reason is
  **indistinguishable from one nobody populated** — the exact bug class this
  subsystem keeps shipping.
- **Matrix ops for speed.** No profile exists for the place/route/DRC split
  of the ~13 s end-to-end. Vectorising `clearance_violations_naive` is
  genuinely attractive (it would promote a fixture-only oracle to always-on)
  but must follow a measurement, not precede it.
- **A concentric-arc reward as a COST TERM.** Measure-zero over continuous
  coordinates: two independently-derived centers are never bit-equal, so the
  term scores zero forever and is indistinguishable from a bug. Viable only
  as a construction rule — a bundle shares one center and varies the radius
  by track pitch — which needs bundle detection that does not exist.

## Still open

- **Removal × measures referential integrity** (S8): `op='remove'` on a refdes a
  measure names must raise a dangling-operand exception with a defined store
  cascade, or removal creates the next stale-row bug.
- **Growable-IR protocol**: decision 5 unblocks append-geometry moves, but
  `n_segments` is baked into the dirty masks, `_seg_inst_*`, and store
  persistence. If no v1 move actually appends, say so and defer.
- **Incumbent checkpointing** for minutes-long `pcb_place` jobs; interacts with
  `gr266041` (idem-key drift on mid-flight edits).
- **Coarse-level crossings blowup**: the pre-L3 placeholder is `C(m,2)` ≈ 2·10⁶
  at 2000 segments, saturating any below-L3 scoring. Moot in-loop (the engine
  pins L4) but poisons seed-stage or tree-node scoring — clamp or exclude it.

---

# Absorbed 2026-09-26

## Intent canonical, films derived

_Grouped 2026-09-26; was `pcb-feature-model-vs-layer-films`, status draft, prio high._

**Audit 2026-09-26: same work as S9 (pcb/features.py); nothing shipped.**

The next application of this build's recurring principle (sketch canonical
→ copper derived; text canonical → strokes derived): **functional intent is
canonical, and the mask/paste/silk films are projections of it.**

### The precise claim (not "layers are just output")

**Copper layers are physically real.** A 4-layer board has four copper
planes; z-order drives coupling, return paths and impedance. A conductor
must name its layer.

**Mask, paste and silk are not real in that sense.** They exist only to
express intent *about* copper. They should never be primary storage.

So: *layers are real for conductors; films are derived from intent.*

### Why boolean decoding is lossy, not merely inelegant

The natural decode rule — copper ∧ ¬mask ∧ ¬hole ⇒ SMT pad — conflates
functionally distinct things:

| Intent | copper | mask opening | paste | hole |
|---|---|---|---|---|
| SMT pad | ✓ | ✓ | ✓ | — |
| **Thermal pad** | ✓ | ✓ (one) | **windowpaned grid** | — |
| **Test point** | ✓ | ✓ | **none** | — |
| **Tented via** | ✓ | ✗ | — | ✓ |
| Covered trace | ✓ | ✗ | — | — |
| **Via-in-pad** | ✓ | ✓ | ✓ | ✓ plugged+plated |

A thermal pad's paste windowpane **cannot be derived** from
copper-and-mask-opening; get it wrong and the part floats on molten solder.
A test point is layer-identical to an SMT pad minus one film. A tented via
decodes as a covered trace. The stack cannot represent these distinctions,
so an intent-first model is *strictly more expressive*, not just tidier.

### Sketch of the model

Features, each knowing how to project itself into films:

- `SolderablePad{shape, pos, side, paste: full|windowpane|none}`
- `Conductor{layer, geometry}` — layer is real, keep it
- `Keepout{extent, layers, excludes: conductor|component|both}`
- `Hole{dia, plated, plugged}`
- `Marking{content, side}` → silk
- `Pour{layer, net}`

Projection to the gerber model is a pure function; the existing
`pcb/gerber.py` writer stays as the projection target, unchanged.

### What it buys

**Better DRC rules, not just faster ones.** "Are these two *solderable
pads* too close" is a solder-bridging rule with a different threshold than
trace-to-trace clearance. "Does a conductor enter a keepout" becomes a
direct query rather than a layer-mask intersection. Fewer rules, each more
precise.

**Interrogable footprints.** "Does this footprint have 8 solderable pads,
and where?" becomes answerable — exactly what `view='pinout'`, connector
intake and escape routing need and cannot currently get.

**Speed, honestly:** fewer objects, and clearance queries filterable by
semantic pair type (pad↔pad, conductor↔keepout) instead of all-pairs across
every film; optimizer deltas touch features rather than re-deriving four
layers. Real, but secondary to expressiveness — do not sell it as the main
benefit.

### The honest cost

EasyEDA footprint data arrives **layer-shaped**, so ingest must *lift*
layers into features — the very inference this avoids. That is acceptable
**only** because it happens once, at the boundary, with ambiguities
recorded rather than silently resolved. Confining the inference is the
goal; eliminating it is not possible.

Watch for: a lift that guesses wrong writes a permanently wrong footprint.
Record confidence and the evidence used, and make unresolved cases loud.

### Lower late, and never lift back

Features are the **IR**; films are the **target**. Everything internal —
optimizer, DRC, rendering, escape routing — works on features. Lowering to
mask/paste/silk happens **once, at export**, as the last step.

**Lowering is one-way and terminal.** Nothing inside the system may read
films back. Code that consumes the gerber model and infers intent is a
*decompiler*, and boolean layer-decoding is exactly that: reconstructing
something you had and discarded. The classic compiler error is emitting
machine code and then trying to optimize the machine code.

**Enforceable invariant** (same shape as the dead-export test): the gerber
model has exactly **one producer** (the lowering pass) and **one consumer**
(`pcb/gerber.py`'s writer). Anything else touching it is a bug and a test
should say so.

This also explains an earlier oddity: `realize.to_gerber_model` with zero
callers was a lowering step with no terminus — correct structure, missing
end. The fix was never to give it more callers *inside* the system.

### Blast radius

Additive, not a rewrite: define features → lift at ingest → project to the
existing gerber model. Sits *above* today's representation.

### Timing

Decide before pad handling calcifies. Instance pad placement is being
written now (`pcb-fab-output-unwired.md`); if features are the target, that
transform should eventually emit features rather than layer shapes. The
transform mathematics is identical either way, so the in-flight work is not
wasted — but do not accrete more layer-shaped pad logic on top of it.

> **Consider for the paper.** "Manufacturing output formats make poor
> internal models" generalizes past PCB: the film stack is a
> photoplotting artifact from the 1970s that most tools still use as their
> data model, forcing semantics to be recovered by boolean decode. See
> `pcb-paper-benchmark-selection.md` §CONSIDER FOR THE PAPER.

## Residuals from the 2026-08-28 session

_Grouped 2026-09-26; was `pcb-residual-defects-0828`, status draft, prio high._

**Audit 2026-09-26: shipped §3 drills, §9 measures-as-cost-terms; partial §1 (ir.inst_bottom, no SIDE_FLIP), §5, §8; open §2 §4 §6 §7 §10 §11.**

Found by *walking the lifecycle* of a footprint and of a via, and by
rendering an actual SVG and looking at it — not by reading code. Each is
the same family this build keeps producing: a correct-looking component
that is unreachable, or two components implementing one rule.

Being fixed separately (agent dispatched 2026-08-28): the `via_count`
cost/realize divergence — `_via_count` reads `ir.n_vias`, `add_via` has
zero callers, `realize._vias_for_track` emits the real ones. Not repeated
here.

### 1. `SIDE_FLIP` is not board side — and the IR cannot express board side

`seg_side` is documented as *"which side of an **obstacle** a connection
takes"* — a rubber-band routing concept. It has nothing to do with
populating the bottom of the board.

Meanwhile `PcbIR` has `inst_x`, `inst_y`, `inst_rot`, `inst_fixed_*`,
`inst_extended_part` — and **no per-instance board side at all**. The DB
has it (`pcb_instances.layer`), the store returns it, `padplace` reads it
and mirrors correctly for the gerbers. The optimizer's IR drops it.

Consequences:
- A bottom-side part exports correctly and is **invisible to placement**.
- The second-side assembly step-function cost (user, 2026-08-28: *"if you
  put anything on the other side, it's the cost, even just 1, so it's not
  per part"*) has nowhere to attach.
- **The trap**: whoever implements that cost will find `SIDE_FLIP` /
  `seg_side`, assume it is the hook, and wire an assembly penalty onto a
  routing variable. It would look right and be entirely wrong.

Fix: add `inst_layer` to the IR; rename or clearly re-document `seg_side`
so the collision cannot mislead. Note the DB column is `layer`, not
`side` — match it, do not introduce a third spelling.

### 2. Crossings are answered geometrically, not topologically

`cost._crossings` uses a **geometric sweep-line** over straight-line
segments at L3 (with a coarse fallback below). But the L2 combinatorial
embedding — which `ir.py`'s docstring insists is stored **explicitly** and
*"never, ever derived from L3 coordinates"* — is the actual topological
answer to "what does this wire cross".

So we store the authoritative representation and then consult the derived
one. Two representations of one fact; the usual outcome follows.

Not necessarily a bug today (the geometric count is real), but it means
the topological invariant the IR exists to protect is not what any cost
term reads. Decide deliberately: either the crossings term reads L2, or
the docstring's claim about why L2 is stored explicitly needs weakening.

### 3. SVG: drills are invisible — **FIXED 2026-08-29**

`drills` is in `svg.DEFAULT_INCLUDE` and `render_board` draws bare holes
on top of copper; the handler's `level='board'` model supplies them via
`_drc_drills`. See `pcb-engine-plan.md` §"SVG drills". Original text kept
below for the detection-method record.


`svg.DEFAULT_INCLUDE` is `{outline, copper, pours, pads, vias, silk}` —
**no `drills`**. The model carries a `drills` list and `render_board`
ignores it. Verified by rendering: two solder-on nuts with 3.2 mm holes
render as solid discs.

On a through-hole board every hole silently vanishes from what we call a
publication-quality figure.

### 4. SVG: every figure is y-mirrored

`svg.py` documents the choice: *"Coordinate convention — deliberately NOT
flipped"*, model mm coordinates straight into SVG's y-down space, because
it keeps arc sweep-flag arithmetic and text placement trivially correct.

Verified by rendering: a part at y=5 draws at the **top** of the image.
That is a **mirror, not a rotation** — a reader cannot fix it by turning
the page, and silkscreen text will render upside-down once text lands.

The stated reason is real, but the cost is paid by every consumer of every
figure rather than once inside `_arc_flags`. Revisit before any figure
reaches the paper.

### 5. A layer change on a current-annotated net changes required width

Deliberately out of scope for the `via_count` fix; filed here so it is not
lost. Measured via `rules.ipc2221_track_width_mm`: 10 A needs 7.19 mm on
an outer layer and **18.72 mm on an inner one** (2.6×, because internal
copper is sandwiched in dielectric and cannot shed heat).

So moving a segment of a high-current net to an inner layer does not just
add a via — it triples the width that net requires, and nothing in the
cost function knows. Once `via_count` is honest, this is the next term:
the layer decision on an annotated net must be priced by the width it
implies.

### 6. `part_footprints.model_3d` has no producer and no consumer

The column exists in `0047_pcb_kind.sql` and the baseline schema, and
appears **nowhere else in the repo**. Meanwhile EasyEDA's 3D model
reference lives in the `SVGNODE` primitive, which `parse_component` skips.

Give it a producer when the `SVGNODE` lift lands (see
`pcb-component-model.md` §Features, `Body.model_ref`), or drop the column.
A schema field with neither end wired is indistinguishable from one that
works.

### 7. Ingest skips primitive classes silently

`parse_component` handles `PAD` and `TRACK`; `HOLE`, `TEXT`, `SVGNODE`,
`SOLIDREGION`, `CIRCLE`, `ARC` are skipped with a comment and no signal.

`HOLE` is NPTH — mounting holes. `TEXT` is the refdes anchor the label
constraint needs. `SVGNODE` is the 3D reference above. Dropping `HOLE`
silently is the entire reason the solder-on nut appeared to need special
modelling: its defining feature was discarded at ingest and nobody could
see it.

**Fail loudly on an unparsed primitive class** (or record it as an
explicit gap on the row). Skipping silently is how "tested but
unreachable" gets built.

### 9. TWO live cost functions — and measures are priced only by the demoted one

*Fable review, 2026-08-28. The largest defect in the build, and it is the
same generator as the other eight, at the largest possible grain.*

Verified by grep, not by reading prose:

- `measures` appears **0 times** in `cost.py` and **0 times** in
  `optimize.py`. Every hit is the English word "measured/measurement" in a
  docstring. It appears **25 times in `place.py`**.
- `place.autoplace` is the Slice-4 annealer with hardcoded
  `W_CROSS=100, W_LEN=1, W_MEASURE=10`. It **reads measures** and **has a
  wirelength term**.
- `cost.py`'s docstring says "ONE cost function" and "**No wirelength
  term** … Do not add one back."

So the two engines disagree on both the objective *and* what inputs
exist. Which runs depends on the verb: `op='place'` enqueues
`workers/job_types/pcb_place.py` → `optimize.py`+`cost.py` (**ignores
measures**); `_place_and_store` → `place.autoplace` (`handlers/pcb.py:873`,
**honours measures**) is reached only from the **Freerouting round-trip**,
the path we ourselves demoted.

Net effect: **every measure the agent states is invisible to the engine we
call the system.** `view='measures'` still evaluates and displays them, so
they look wired. `precis-measures-help` documents the `place.py` weights
as if they were the objective. This is "tested but unreachable" applied to
the entire intent-expression feature.

Fix direction: pick one engine. If `optimize.py` is the system, measures
must become registered cost terms reading `operands`, and `place.py`'s
objective is deleted or quarantined behind a loud deprecation — **not left
as a second opinion.** Do not design more cost terms before this is
settled; you would be extending the wrong one 50% of the time.

### 10. Nothing reads the L2 embedding — anywhere

Stronger than §2 above, which undersold it as "decide deliberately whether
the crossings term reads L2". Verified: `rotation_darts`, `rotation_index`
and `validate_embedding` have **zero production readers** — only `ir.py`
itself and tests. `ir.py`'s docstring calls the explicit embedding "the
invariant this module exists to protect"; the crossings answer is computed
from L3 geometry in `cost.py` and maintained geometrically in
`optimize.py`.

So it is the tenth member of the dead-plumbing family this document was
written to catalogue (`add_via`, `model_3d`, `SIDE_FLIP` at ~15% of the
move budget, drills unrendered, measures unpriced) — and the biggest one,
missed *while writing the list*.

**The one test that catches all of them.** `cost.py` already has the right
pattern: the registry-driven move-reachability property ("every registered
term must take two distinct values under random moves"). Generalize it —
every IR field, schema column and move kind must have a mechanically
asserted **producer and consumer**. That single test subsumes §§1, 3, 6, 9,
10 as a class. The docs kept proposing more model; the defect generator
was unwatched plumbing.

### 11. Margin aggregation: don't build `max + ε·mean` — it cannot be tested

This session proposed replacing the exact `max` over margin penalties with
`max + ε·mean`, plus a property test asserting it "orders like max". Fable
killed both halves; recording the kill so nobody rebuilds it.

- **The property test is unsatisfiable.** "For any two states with
  different maxima, `max + ε·mean` orders like `max`" is false for every
  ε > 0 — choose maxima differing by δ < ε·(mean difference) and the order
  flips. Only a lexicographic comparison satisfies it exactly, and SA needs
  a scalar energy for `exp(−Δ/T)`. Any honest generator finds the
  counterexample, so the test would end up quietly rigged.
- **Exact `max` already violates `optimize.py`'s own doctrine** — "every
  cost term must decompose into local contributions with an efficient
  delta". `max` does not decompose; when the argmax improves, the engine
  must rescan or heap the whole penalty list per move.
- **The standard answer is to schedule the smoothing, and the dial
  exists.** Log-sum-exp `risk = τ·ln Σ exp(pᵢ/τ)` with τ annealed down
  alongside the existing `schedule`: `max ≤ LSE ≤ max + τ·ln n`, gradient
  everywhere, exact max recovered as τ→0, O(1) delta via a running sum.
- **Stronger still — drop `max` and just sum the hardened penalties.**
  `hardened_penalty` is already superlinear with a schedule-sharpened
  barrier; summing convex per-constraint penalties under escalating
  hardness *is* the penalty method (and is what PathFinder-style negotiated
  congestion does for routing). At `schedule=1`, `hardened(0.99) ≈ 4.6` vs
  500 nets at 5% ≈ 1.25 — convexity makes the worst constraint govern at
  the hard end by itself, and early-schedule mean-sensitivity is not a bug,
  it is **the gradient the plateau lacks**. Then `Family` stops controlling
  aggregation entirely and only controls normalization (USD vs
  fraction-of-budget) — one whole mechanism deleted.

That last point follows from a principle already written down here:
*"convexity IS the schedule — one mechanism, not two."* `max` was the
second mechanism all along.

### 8. The JLC scope probe could not fail — fixed in the doc, verify the claim

The probe in `pcb-guided-place-route.md` had two bugs, both live-fixed
2026-08-28: `c.available()` on a property (raised `TypeError` before
probing anything), and — worse — `component_info` returns `None` both
when credentials are absent *and* when the part is not found, so with no
credentials it never reached the network and the probe printed
`RESULT: success`.

A diagnostic whose success path is reachable without doing the thing it
diagnoses. **Measured: credentials do not resolve on melchior** under a
bare ssh + `/opt/mcps/venv/bin/python` — but that is not proof the vault
is empty, because a bare `python -c` never calls `secrets.bind_store` and
has no DSN, so the DB vault was never consulted. A human with the web
service's `PRECIS_DATABASE_URL` must run the corrected probe.

## Round-3 review findings + feature asks (user, 2026-09-01)

_Grouped 2026-09-26; was `pcb-review-round3-0901`, status draft, prio high._

**Audit 2026-09-26: items 1-7 done, 8 subsumed, 9/10/14 shipped via round 4; open = 12 (post-route rip-up shortening) and 12b (sensitivity classes, isthmus-weighted stitch scoring; net-class half belongs to pcb-lazy-netlist-and-checks slice 9).**

From peeking at the regenerated `board_nano.svg` / `board_motor.svg`.
Diagnoses verified in-session. **Items 1-7 are DONE** (this worktree,
full pcb sweep 1524 green; deltas noted per item where the implementation
diverged from the first plan), **[filed]** items need their own cycle.

### Diagnosed defects

1. **[DONE] ~40 small vias in rows along the nano board's bottom edge**
   (0.25mm drill, marching through the S/N patch). They are GND plane
   stitching vias (`realize._stitch_plane_fragments` stage-1 sprinkle),
   but `_grid_candidates` enumerates the overlap grid bottom-row-first
   and `max_sprinkle_vias_per_overlap` (24) accepts the FIRST 24 — so
   the whole budget clusters into the lowest rows instead of spreading
   over the overlap. Fix: allocate the budget per disjoint part of the
   overlap (a small island always gets its via) and spread each part's
   share evenly across its candidates. User refinement (2026-09-01):
   ideal placement is inside small islands and ISTHMUSES (necks that
   are only thinly connected on some layer) — the per-part allocation
   covers islands now; narrowness-weighted candidate scoring is the
   follow-on (see item 13's sensitivity work, same cost-shaping pass).
2. **[DONE — inverted] Stitch vias walk under silk furniture** (the S/N
   patch is a *writing surface* — a via bump under the Sharpie area
   defeats it). Shipped the opposite direction from the first plan: the
   furniture placement (title block + S/N patch) is route-independent,
   so instead of grid claims the builders now AVOID vias (soft — scored
   fewest-vias fallback, `silk.via_obstacles`) and part courtyards
   (hard, inflated bbox obstacles in `_board_furniture`), with deeper
   ladders (edge-slide for the title, edge-centre rects for S/N, six
   fiducial rungs) so corner mounting hardware can't evict them.
3. **[DONE — via SVG mask, not ring union] White corners in the "N".**
   `gerber_view._region_els` renders [solid ring + clear rings] as ONE
   `fill-rule="evenodd"` path; where two knockout letter strokes overlap
   (the N's corner joints) the double-count flips the region back to
   filled. Fix: union overlapping clear rings before emitting the path
   (geometry fix in the viewer; real gerber clear polarity is idempotent
   so the artefact exists only in this renderer).
4. **[DONE] `npth_clearance: 4` on the nano board** — the four corner
   mounting holes are never claimed on the routing grid (same family as
   the fiducial copper-claim leak, root-caused 08-31). Fix: claim them
   (hole + annulus + clearance) before `_stamp_pads`.

### Feature asks

5. **[DONE] M4 solder nuts in each corner.** Mounting holes become
   `style: "solder_nut"`: plated hole + copper annulus both sides
   (SMTSO-M4-class: 5.6mm drill, ~8mm ring), rendered + gerber'd +
   DRC'd as plated (pad clearance, not npth), claimed per item 4.
   Corner centres move inboard so the ring clears the edge rule.
6. **[DONE] Rounded board corners.** Outline feature gains optional
   `corner_radius_mm`; the handler polygonizes the rounding at IR build
   (arc facets), so pours/DRC/fiducials/silk all inherit it through the
   one outline polygon. Fixtures set a radius.
7. **[DONE] Label side consistency.** `silk._refdes_candidates` orders
   ring directions "nearest straight up first"; user wants labels on a
   consistent side (right, else bottom) so arrays read tidy. Reorder the
   preference sweep; keep the existing global obstacle avoidance.
8. **[MOSTLY SUBSUMED] Via-out-of-silk relaxation.** A via near C5
   (motor board, also L1/U1) clipped silk ink. Refdes labels already
   avoid vias at candidate time, and the furniture now does too (item
   2), so the remaining exposure is a via clipping a COURTYARD stroke
   (which cannot relocate) — the original idea (post-silk via nudge
   reusing `_shove_vias`) stays filed for that case only. (The "push
   min-gap via pairs apart" half was WITHDRAWN by the user 2026-09-01:
   non-sensitive signals run tight — close vias are fine, see 12b.)
9. **[filed] Placement groups ("super footprint").** J1/J2 are the two
   header rows of a nano daughterboard and must hold their exact
   relative offset; optimize needs rigid group moves (members anneal as
   one body). Generalizes to any repeated-connector cluster.
10. **[filed] Repeated-pattern tiling pressure.** Q1..Q4 / J4-J7 style
    repeated subcircuits should get IDENTICAL internal layout (detect
    isomorphic groups, replicate one layout). Big; interacts with 9.
    The shipped `alignment` term (0.002 USD/pair) is the lightweight
    stand-in already pulling R's into rows.
11. **[filed] Nano netlist deep integration.** Replace the J1/J2
    header pair with the nano's own netlist integrated into the board
    (drop the daughterboard), USB-C connector with an edge-affinity
    constraint ("rubber-banded to the board edge"). Needs: netlist
    source, new fixture, edge-affinity placement term. Own spec when
    picked up.
12b. **[filed] Signal sensitivity classes + tight-bundle routing**
    (user, 2026-09-01). The LLM marks sensitive signals on the netlist
    (none on the current fixtures); sensitive nets get special rules —
    cross power at 90°, GND guard either side, spacing floors. NON-
    sensitive nets are the default and should run TIGHT: parallel
    bundles without extra spacing, vias allowed close together (this
    withdraws the via-island spreading idea). Stitch-via placement
    should eventually prefer isthmuses — thin necks in a plane
    fragment — over open field (narrowness-weighted candidate score).
    Needs: a net-class/sensitivity attribute on the IR, router cost
    shaping per class, and the guard/crossing rules. Own spec when
    picked up.

14. **[filed] Placer is blind to mounting holes.** The ROUTER now claims
    them (item 4) and pours antipad them, but `optimize.py`'s anneal has
    no hole keepout: it can legally park a part's courtyard over a
    corner solder nut, and only the route pass (no_path near the corner)
    or courtyard DRC would complain downstream. Fix: mounting holes as
    static courtyard obstacles in the anneal (same polygon machinery as
    part courtyards). Today's fixtures centre their packs away from the
    corners, so this is latent, not observed.

12. **[filed] Post-route wire shortening.** Wires longer than needed;
    `_collapse_straight` already pulls taut within the corridor, the
    residual slack is maze detours around since-vanished congestion.
    A bounded rip-up-and-reroute pass (re-route each net against the
    final grid, keep if strictly shorter) is the standard fix.

**Verify before delete-on-ship:** regenerate both boards; user re-peeks
(stitch rows spread, S/N writable + clean N, nuts + rounded corners
visible, labels consistent, C5/R1-R4 vias relaxed).

## Round-4 review findings + feature asks (user, 2026-09-01)

_Grouped 2026-09-26; was `pcb-review-round4-0901`, status draft, prio high._

**Audit 2026-09-26: items 1-9 done; open = 10 (bottom-side placer SIDE_FLIP for instances) and 11 (tiling v2 mid-anneal co-optimisation).**

From peeking at the round-3 regenerated `board_motor.svg` /
`board_nano.svg`. Diagnoses verified in-session before dispatch.

### Motor board

1. **[DONE — fixture → top; full support filed as item 10] "C3 on the
   bottom, why?"** — `motor_power_reference.json` declares C3
   `"layer": "bottom"` (added with the second reference board,
   74390332, to exercise bottom silk). Diagnosis: the component-side
   declaration is HALF-honoured — the handler's refdes→layer map feeds
   only `silk.build_model_silkscreen` (mirrored bottom silk); pads,
   mask, paste and routing all still emit on F_Cu ("F_Cu · pad · pin
   C3.2" in the very same SVG). A bottom part today is a silk-only lie
   in the gerbers. Fix now: C3 back to top. Full bottom-side support =
   item 10.
2. **[DONE — router-side ink field] Vias near U1/C5 still clip silk** (nano: D4 too). Labels
   already avoid vias; the residual clips are COURTYARD strokes, which
   cannot relocate. Root fix at the source: courtyard-stroke rings are
   placement-derived and known BEFORE routing — add a soft via-site
   penalty (not hard block) under the future courtyard ink
   (`world_courtyard_rings` + silk clearance) so the router stops
   dropping vias there. Replaces the filed post-silk nudge idea.
3. **[DONE] Fiducials span all layers.** Currently flashed on
   `layers[0]` only. Emit the copper disc on EVERY copper layer, mask
   opening on BOTH sides, pour antipads on all layers, router keepout
   claims on all layers. Silkscreen stays deliberately EMPTY at the
   fiducial (a silk-free zone IS the alignment feature; ink there
   would defeat the optical target).
4. **[DONE] Pin-1 marks only where polarity exists.** R/C/L/FB refdes
   families get NO pin-1 dot unless polarized: explicit
   `"polarized": true` on the component, or label matching
   ELEC/TANT/POL. Nano C1 (CAP-ELEC…) is the live test case. All other
   families (D, Q, U, J, LED…) keep the mark.
5. **[DONE — film render] "Solder mask layers are empty."** Two findings: (a) B_Mask
   had only nut rings because the ONLY bottom part is the C3 half-lie
   (item 1) — correct once C3 is top; (b) the real UX bug: mask
   gerbers contain openings that sit exactly on the pads, so toggling
   the layer shows nothing new. Render mask layers in the viewer as
   what they ARE physically: a translucent film over the whole board
   with the openings cut out (reuse the SVG `<mask>` machinery from
   round 3).

### Nano board

6. **[DONE] J1/J2 rigid group ("super footprint").** Components gain
   `"group": "<name>"` (+ per-member `group_offset {x, y, rot}` fixing
   internal geometry — J1/J2 at the nano's real 15.24 mm row pitch).
   The anneal moves a group as ONE rigid body: translate/rotate apply
   to all members about the group centroid, internal offsets locked,
   legality checked per member. Fits the existing multi-instance
   `Move` shape (SWAP already carries 2 instances).
7. **[DONE — v1, rigid tiles stamped from instance 0] Repeated-pattern tiling.** Components gain
   `"pattern": "<name>"` + `"pattern_instance": <n>`. All instances of
   a pattern share ONE internal layout (leader = instance 0; followers
   stamp the leader's internal member offsets at seed) and anneal as
   rigid bodies — identical tiles by construction, the alignment term
   pulls them into rows. Nano: channels {J4,Q1,R1,D1} … {J7,Q4,R4,D4}.
   V2 (filed): co-optimize the shared internal layout mid-anneal;
   automatic isomorphic-subcircuit detection.
8. **[DONE] Placer blind to mounting holes — now OBSERVED** (Q3 overlaps
   the top-left nut, R1 possibly fully under it, C1 clipping). Round-3
   item 14 promoted: mounting holes (ring dia + courtyard clearance)
   become static courtyard obstacles in the anneal
   (`_placement_is_legal` + overlap term), read off
   `ir.mounting_holes`.

### Viewer

9. **[DONE] Hierarchical mouseover.** Titles lead with the layer, then
   position, then what the element belongs to: "F_Cu · (x, y) mm ·
   pad 1 of Q4 · net OUT4"; tracks/regions carry their net; keep the
   existing escape rules. Viewer-only formatting + ownership plumb.

### Filed

10. **[filed] Full bottom-side component support.** `"layer":
    "bottom"` must flow past silk into `pads_for_ir` (mirrored B_Cu
    pads), mask/paste sides, router pad-layer starts, and a placer
    SIDE_FLIP-for-instances move. Until then the loader should warn on
    (or reject) bottom parts rather than half-honour them.
11. **[filed] Tiling v2** — mid-anneal shared-layout co-optimization;
    automatic isomorphic-group detection (round-3 item 10's full
    scope).

**Verify before delete-on-ship:** regenerate both boards; user
re-peeks (C3 top, no courtyard via clips, fiducials on every copper
layer + both masks, no pin-1 dots on R/C except C1, mask film
renders, J1/J2 locked, channels tiled identically, nothing under the
nuts, hierarchical tooltips).
