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
was on**. Now `tests/test_pcb_maze.py::test_routed_copper_only_ever_lands_
on_a_signal_layer`.

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

### Viewer + geometry-quality direction (user, 2026-08-29) — 1,2,3,5 DONE

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

Still genuinely missing: **silkscreen** (no table; refdes and part outlines
absent) and real pad SIZES (`maze.PAD_RADIUS_MM` is still a constant, so
every pad exports as a 0.4mm circle — which is exactly why the
synthesized-pad refusal has to stay).

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
