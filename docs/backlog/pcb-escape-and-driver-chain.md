---
status: draft
title: Escape and driver-chain are general PCB primitives wearing EWOD names
prio: high
pillar: 3d-design
---

# Escape and driver-chain are general primitives wearing EWOD names

Supersedes `pcb-ewod-perimeter-driver-round.md` (deleted). That item was
built on "move the driver off the array", which measurement retired — see
"What the measurements actually say".

## Governing constraint: the CODE and the PCB stay separate

Reto, 2026-09-26/27. The engine in this repo is a **general capability
whose job is to let an LLM fulfil arbitrary text requests for boards**.
Any particular board — every EWOD one included — is DATA authored through
the MCP, not code. For each item below: general primitive, or this board's
parameters? "Engine, but only EWOD uses it" is a smell.

The engine currently fails that test at the top level: `_REGISTRY` holds
exactly one generator, `ewod_pad_array`, so the only thing the engine
knows how to make is an application.

## What the measurements actually say

Escape yield on `tests/test_pcb_ewod_dogfood.py`, seed 1, 54 escape nets.
**Read the caveats — two of these rounds measured artifacts.**

| arm | realized | `no_path` | `congestion` | vias |
| --- | --- | --- | --- | --- |
| old GRID-footprint fixture | 13 / 54 | 35 | 6 | 7 |
| ring fixture, lands unclaimed by the router | 35 / 54 | 13 | 6 | 10 |
| **ring fixture, as shipped (item 2)** | **28 / 54** | — | — | — |
| sink moved outside the array | 32 / 54 | 15 | 7 | 5 |
| **escape fabric not claimed as an obstacle** | **46 / 54** | 4 | 4 | 5 |
| clearance forced 0.15 → 0.093 | 35 / 54 | 10 | 9 | 10 |
| "any signal layer" escape class | 42 / 54 | 12 | 0 | 25 |
| fabric not claimed + any signal layer | 54 / 54 | 0 | 0 | 26 |
| **In2.Cu authored as a signal layer** | **50 / 54** | 2 | 2 | — |

00. **THE GAP WAS A STACKUP PROBLEM. 28 → 50 / 54, SHIPPED 2026-09-27.**
   Every arm above was measured on a board with exactly ONE routing
   layer, because `DEFAULT_STACKUP` makes In1/In2 planes and nothing
   could say otherwise. `put(args={'op':'stackup'})` now can. Declaring
   In2.Cu `signal` (plus `escape_layers: ["B.Cu","In2.Cu"]`, which is
   the other half — a layer nothing is allowed to use stays empty) takes
   escapes **28 → 50 of 54**, DRC clearance errors **0 → 0**, and nets
   whose copper is in more than one island **26 → 4**. That beats the 46
   from the illegal arm, and it IS legal: it opens an inner layer, never
   F.Cu, so the electrode field is untouched. Pinned by
   `test_dogfood_an_inner_signal_layer_nearly_closes_the_escape_gap`.
   **Read every row below as "on a one-layer board".**
0. **35 IS NOT THE BASELINE ANY MORE — 28 IS.** Every row above except
   the last was measured while the router could route through unclaimed
   footprint lands, which it did: 24 DRC clearance errors at 0.000mm.
   Item 2 closed that on 2026-09-27 and the honest number dropped to 28.
   Read every other row as "n, of which some were shorts".
1. **The FIXTURE was most of the wall.** The old stand-in was
   `_grid_footprint(cols=9)`: a solid pad grid with ~31 INTERIOR pads,
   unreachable at any clearance the maze dilates by. The real C639448 is
   a PQFP-80 — 80 pads, ZERO interior, 17x23mm (queried against prod).
   Swapping to a ring took 13 → 35, and 35/54 now sits alongside prod's
   34/62. Fixed 2026-09-27 (`_qfp_ring_footprint`).
2. **The last two rows are NOT achievable.** "Any signal layer" let
   escapes route across F.Cu, which the electrode field owns. Discard
   those two numbers; the honest ceiling measured so far is 46.
3. **Driver placement is not a routing fix.** Outside the array measured
   WORSE (35 → 32), and still worse than baseline when the fabric was also
   unclaimed (46 → 44). It survives only as gr451052's collision fix.
4. **Clearance is worth zero** (35 → 35). The `max()` collapse in
   `realize.py` is a real defect with no measurable payoff. File, forget.
5. ~~**The fabric is the one measured lever**~~ — SUPERSEDED. It measured
   35 → 46 with `no_path` 13 → 4, but that arm unclaimed real copper and
   was never shippable. The LAYER is the lever, and it is legal: see
   point 00. Fabric ownership is still the next thing to try if 50/54
   needs to become 54/54, but it is no longer the only measured one.
6. **The realized escapes are single-island copper, and the assignment
   is already ordered** — but "single island" was never "clearance-legal",
   see point 0. Audited 2026-09-27 (throwaway probe, deleted): of the
   realized escapes, zero carry an explanatory `note` (so none is a
   dangling-net freebie counted as realized) and zero have copper in more
   than one island per `connectivity.net_islands` over the exact fab
   copper. EVERY failure DOES hold copper, in ≥2 pieces — electrode stub
   and driver stub, never joined. **53 pin swaps settled and all 54
   electrode→channel airwires cross ZERO times**, unchanged whether or
   not the router claims the unclaimed lands, so the channel assignment is monotone and is NOT why they
   fail. (Straight-line proxy, not routed paths — but it is the thing
   "are they in order" asks.) Do not spend a round on channel assignment
   or crossing minimisation **on this metric** — and the parenthesis is
   load-bearing: it is AIRWIRES, so it says nothing about crossings among
   the paths the router actually draws.

   **⚠ THE ZERO DOES NOT REPRODUCE, AND LOOKS VACUOUS (2026-09-28).** An
   independent probe counting straight-line crossings on the same board
   got **158 crossings over 158 net pairs**, cross-checked with two
   methods that agree exactly (an exact `Fraction` orientation test and
   shapely's `.crosses()`). The difference is the ENDPOINT DEFINITION:
   that probe used each net's OWN sink pad, while the original zero
   appears to have measured every airwire to a single shared sink point.
   **A star from one common endpoint cannot cross — it is crossing-free
   by construction, not empirically.** If that is what happened, the zero
   measured the definition, not the board; it is the vacuous-green shape
   this repo keeps producing (cf. `pads_for_ir does not feed the router`,
   and the two pour bugs of the same date).

   This has not been confirmed against the original probe, which is
   deleted. Treat the zero as unusable pending a pinned re-measure, and
   do NOT cite it — in either direction. In particular the argument
   "straight-line metrics are blind, so a swap objective needs a bent or
   routed metric" rested on this zero and is now unsupported: a metric
   reporting 158 is not blind, it is merely different. Item 3's
   measurement discrepancy is the same family of problem.

## The general primitives hiding in `ewod_pad_array`

| EWOD name | General thing | Also serves |
| --- | --- | --- |
| `sink` / `sink_grid` / `ewod_sink` | **driver chain** — N loads, M identical K-channel drivers, contiguous shares, daisy-chained serial in/out, each placed at its share's centroid, pin-swap group so the router picks the channel assignment | LED matrix, keyboard matrix, display driver, sensor/MEMS array |
| plaza via + breakout stub fabric | **area-array escape** — fan an array out through its own shells | BGA / LGA, any area-array package |
| electrode array | **pad array** — rows/cols/pitch/merged spans | any patterned-copper region |
| array-vs-driver overlap | **polygon keep-out** — the placer today honours only mounting-hole CIRCLES (`optimize.py`, `_hole_keepout_radius_mm`) | RF zones, antenna clearance, heatsinks |
| `top_plate_pin` | a shared rail across the chain | any common-return array |

Genuinely EWOD and belonging in design data: that the loads are
electrodes, that the shared rail is a top plate, the drive voltage and the
HV clearance derived from it, parylene, plaza geometry.

**`precis.pcb.escape` already IS the general escape primitive, and nothing
imports it.** Every reference to it in `src/` is a docstring. It computes
convex-layer ("onion") shells, pad gaps, per-gap capacity, and
`required_layers()` — from the footprint alone, before placement exists.
Its own docstring states the principle Reto independently asked for ("a
smart footprint... with some code that does its placement"):

> escape routing is footprint-INTRINSIC and PRECOMPUTED

That principle is right and the module is still the general primitive.
What it is NOT is a drop-in for this generator's plaza fabric — see
"Deferred out of this item" below for why its inputs and its answer both
fail to apply here.

## In scope

Ordered. 1 has shipped (detection only). 2 is the routing half, attempted
and backed out. 3 must be MEASURED before it is built.

1. ~~**Footprint-sourced pad set** (gr451276)~~ — **SHIPPED 2026-09-27**.
   `_unclaimed_pad_rows` in `realize.py` places every footprint pad no
   netlist pin claims, once, for:
   `_unclaimed_footprint_pads`, appended by `pads_for_ir` → DRC's
   `_drc_pads`, the plane pours via `_pad_blockers`, the fixed-copper
   connectivity model, `to_gerber_model`. Label-keyed, no net, never
   promoted to pins; only where a REAL cached footprint exists; an
   instance whose pin↔pad label join failed contributes nothing, or the
   same physical pad would be emitted twice at two coordinates.

   **DETECTION ONLY. `pads_for_ir` does NOT feed the maze router** —
   `_pad_blockers` has exactly one caller, `_pour_planes`, and the router
   stamps `pad_geometry` over `ir.pin_*` directly in `_route_pass`. So a
   track drawn through an unclaimed land is reported by `view='drc'`, not
   prevented. Yield is therefore unchanged at 35/54, and that null is
   explained, not evidence. Item 2 below is the routing half.

2. ~~**Claim unclaimed pads in the ROUTER's grid**~~ — **SHIPPED
   2026-09-27**, the half that answers Reto's actual words ("no wire can
   route thru it, even if it is nc"). `_unclaimed_pad_claims` reshapes
   `_unclaimed_pad_rows`' output as `(point, owner, PadShape, layers)`
   4-tuples appended to `_realize_maze`'s `pads` list, owner =
   `ir.n_nets + ir.n_pins + _UNCLAIMED_PAD_NET_OFFSET + k`, extending the
   per-pin NC sentinel scheme that already keeps `NO_NET` distinct from
   `maze.FREE`. **The offset is load-bearing** — three claim families
   stamp one grid and all count from `n_nets + n_pins`: fiducial
   candidate sites (bare), mounting holes (+4096), unclaimed lands
   (+8192). Without it the first land and the first fiducial share an
   owner id, and `stamp_shape`'s contest test is `owner != net_id`, so
   two claimants read as one and reassign instead of going CONTESTED.
   Caught in review, pre-ship. Appended AFTER
   `maze.grid_for`, deliberately: the grid's pitch is cut from the point
   set it is handed, so feeding it lands no net routes to would re-cut
   the pitch board-wide for a purely local obstacle.

   **What it bought, measured on the dogfood at seed 1:**

   | | before | after |
   | --- | --- | --- |
   | DRC clearance ERRORS | 24 | **0** |
   | DRC clearance warns | 134 | 132 |
   | escapes realized | 35 / 54 | **28 / 54** |
   | GND | `failed`, copper in 2 islands | `realized`, **1 island** |

   Every one of the 24 was `track[ARR1_RxCy] <-> pad[]` at **0.000mm** —
   six escape nets (R1C5, R2C5, R3C5, R5C4, R6C3, R6C4) drawing B.Cu
   straight across a land the gerbers flash. **The yield drop is the
   point.** An escape that shorts an NC land was never realized; it was
   reported as realized. 28 is the first number on this fixture that
   means what it says, and it is the baseline every later arm compares to.

   **The "dangling GND tracks" that backed this out on the first attempt
   were a TEST defect, not a router one.** `_route_pass` calls
   `grid.route` with `attach` at its default `True`, so a later segment
   of a net may start anywhere on copper that net already owns — a
   T-junction into an earlier run instead of a second trip back to the
   pad. Measured: GND seg 59 started **0.000mm** from GND's own earlier
   B.Cu run, and `connectivity.net_islands` called GND one component. The
   dogfood assertion accepted an endpoint at a pad, at fixed copper, or
   at one of the net's own routed VIA centres — not on one of its own
   routed TRACKS, which is exactly what attach produces. Fixed by adding
   that third target, with the two runs' own half-widths as the tolerance
   (their copper bodies overlap) rather than an epsilon: the stored
   polyline is filleted, so an anchor placed exactly on the raw path sits
   hundredths off the rounded corner that actually ships.

   **That third target is checked TRANSITIVELY**, seeded from tracks
   ending at a real pad / fixed-copper point / own routed via and
   propagated to fixpoint. A pairwise version would let two tracks that
   touch only each other excuse one another and float free of every pad
   — gripe 338983's signature, the thing the assertion exists to catch.
   A `is_dogbone` stub is anchored by construction (it ends at its own
   drop via), which is why it is exempt from the assertion but is still
   a legal thing to attach to.

   Four hypotheses were ruled out before that, all producing
   byte-identical failing coordinates, and none was the cause: feeding
   the extra points to `maze.grid_for`; stamp ORDER vs `_stamp_pads`'
   pass-3 `claim_centre`; pad-identity vs label claiming;
   `config.straighten` (turning it off moved the coordinate and kept the
   failure). The lesson is the same one item 1 recorded: four variants
   agreeing is evidence the *hypothesis class* is wrong, not that the
   next variant will be right — dump the object instead.

   **One real bug found on the way, kept:** claiming a pin's own extra
   same-label pads (a split thermal slug, an EWOD electrode's body + stub
   taper) as "unclaimed" gives them `net=""`, which drops them from their
   net's pad set AND makes them foreign obstacles to their own net. A pad
   is unclaimed iff its label matches NO pin; `_real_pad_sizes`' "first
   wins" decides which pad's SIZE stands for a pin, not which pads belong
   to it.

   **Residual gap:** the non-first pads of a multi-pad pin stay invisible
   to the router and DRC, since both index per pin. Closing that needs
   those pads carried WITH their pin's net, not as netless obstacles.

3. **Rim exit — MEASURE BEFORE BUILDING.** Reto, 2026-09-27: "the escape
   is just the needful vias we'll need in any case (except for the most
   outer rim)" — the outer ring has open F.Cu outside the array, so those
   electrodes should not need a via at all. On the 8x8 dogfood that is
   **22 pins**, not 28: five rim cells are themselves plazas
   (`_default_plaza`, `r % 3 == 1 and c % 3 == 1` puts plazas at {1,4,7},
   so (1,7) (4,7) (7,1) (7,4) (7,7) are rim), and the RESV merge makes
   (0,0)+(0,1) one net.

   **The 35 → 46 measurement does NOT support this change.** That arm
   (`_noop_fixed_copper`) unclaimed ALL 54 nets' fabric including the
   interior, and let routes pass through foreign vias — which is why the
   doc calls it illegal. Dropping only the rim triples is a different,
   unmeasured arm. Measure it first — see "Probe recipe" below.

   **Three blockers, all verified in code, that the naive change hits:**
   - A pin whose native pad layer is outside the class lock routes ONLY
     from a fixed-copper island terminal (`realize.py::_resolve_route_end`
     — the plaza via's own B.Cu landing IS that terminal for the escape
     case). Remove a rim pin's via and the segment has no legal start; it
     fails rather than freeing up.
   - Letting rim pins fall through to the plain class instead
     (the class is keyed on `"via" in ledger_pads.get(pin, {})`,
     `generators.py`) routes them outward on **F.Cu** — which
     `test_pcb_ewod_dogfood.py` asserts never happens and this item's own
     acceptance criterion forbids. Making that legal means redefining
     "the electrode layer" as the field POLYGON rather than the layer,
     which needs a **router-side** polygon keep-out. That primitive does
     not exist; item 4's keep-out is for the placer (`optimize.py`), a
     different consumer. **Item 2 therefore depends on a new routing
     primitive** — split it out if the measurement says it is worth it.
   - HV moves from geometry into a router that does not know about it.
     The escape/electrode class clearance is `gap` (0.10); at 250 V
     `hv_separation` is 0.4 (IPC-2221B B4). The plaza fabric bakes 0.4
     into slot geometry; a router-made F.Cu exit beside a foreign
     electrode would be checked at 0.10. Rim nets need an HV clearance
     rule or this ships copper the generator itself would refuse.

   **The (neck, via, breakout) triple is ATOMIC.** "108 stub tracks" is
   not over-build: the B.Cu breakout closed gr347037, and the per-plaza
   via-claim ordering in `realize.py` is what fixed "walled in even when
   routed alone". Whole triples may be removed for a pin that gains
   another exit; a plaza's stubs may never be thinned. ("8 vias per
   plaza" is a ceiling, not a count — rim plazas have slots facing off
   the array.)

   **"Driven" and "has fabric" are the same flag today** and must be
   decoupled first: `chain_pins.append` runs only after the via is
   emitted (`generators.py`), so a via-less rim pin gets no driver
   channel at all. The net class (above) and the fabric ledger's
   `pads_usable` read the same flag.

   **REDIRECTED by Reto, 2026-09-28 — this is not a via-removal item any
   more.** "I would say this is trivially routed from the vias provided by
   the template with proper pinswapping. We just … do them in order
   without crossing or looping around, there must be a ratsnest level
   metric that swaps right." So: KEEP every template via, including the
   rim's, and win by ordering the channel assignment instead. That
   sidesteps all three blockers above — no pin loses its route start, no
   F.Cu exit is needed, no HV rule is needed — and it is not the arm the
   35→46 measurement discredited.

   **What already exists, and what it does NOT settle.** Point 6 measured
   "53 pin swaps settled and all 54 electrode→channel airwires cross ZERO
   times", and concluded "do not spend a round on crossing minimisation".
   That conclusion is narrower than it reads, because of its own
   parenthesis: **the metric is a straight-line proxy over AIRWIRES, not
   over routed paths.** Zero airwire crossings does not imply zero
   crossings among the paths the router actually draws, which have to dodge
   every plaza via and share corridors. So Reto's mechanism is not refuted
   by the existing number — it was never measured on the geometry it is
   about.

   > **⚠ THE NUMBERS BELOW ARE DISPUTED — do not build on the specific
   > counts.** A second, independent probe on the identical board and
   > seed, with `src/precis/pcb/` unchanged since, could not reproduce
   > them: it got **70 routed × routed crossings over 52 net pairs**
   > (collinear-merged) or 76/54 (raw router segments), against the
   > 51/41 recorded here. Segment granularity moves the number (raw vs
   > merged) but neither value lands on 51, so at least one of the two
   > probes is wrong and it is not yet known which. Both were throwaway,
   > single-run, and unaudited; this block was committed treating the
   > first as settled, which was a mistake.
   >
   > **What survives, and is all that survives:** realized escape paths
   > cross MANY times — every measurement agrees the count is in the
   > dozens, nowhere near zero. So the qualitative conclusion (the swap
   > lever is not spent) stands. The exact counts, the buckets, and the
   > within/between split do not.
   >
   > **First build step is therefore NOT the swap objective.** It is a
   > crossing count with a PINNED method, as a real test in the suite:
   > fix the segment-granularity convention (raw or collinear-merged),
   > fix what counts as an escape path's endpoints, and assert the
   > number. Two throwaway probes disagreeing by 37% is a measurement
   > problem, and a swap objective scored on an unpinned metric cannot be
   > evaluated. See also the straight-airwire retraction under point 6.
   >
   > **SUPERSEDED 2026-09-29 by a better answer to "pinned how?" — see
   > "Method, decided with Reto 2026-09-29" immediately below.** Pinning a
   > GEOMETRIC crossing count means choosing conventions and defending
   > them. Counting inversions of a PERMUTATION has no conventions to
   > choose. That is the metric to build; this paragraph's "fix the
   > segment-granularity convention" framing is the weaker version of the
   > same goal.

   ### Method, decided with Reto 2026-09-29

   > **⚠ REVIEWED THE SAME DAY AND LARGELY WRONG. Read the correction
   > block after piece 4 before building ANY of this.** Of the eight
   > claims behind pieces 1-3, an independent review found three false
   > (the 2n unrollings, the basin argument, the far-side-cut claim) and
   > four true only under conditions not stated. The pieces below are
   > kept as written because the corrections are easier to read against
   > them; **the corrected method is the one to build.**

   Design conversation, not yet built. Four pieces, in build order.

   1. **Crossings as permutation INVERSIONS, not segment intersections.**
      For a matching between pads in cyclic order (a ring of pins) and a
      target in linear order (an escape channel), the claim is that a
      crossing-free realization exists iff the cyclic order, cut at some
      point and possibly reflected, equals the linear order — 2n candidate
      unrollings — and that for a fixed assignment the minimum crossing
      count is the number of inversions (Kendall tau), O(n log n).
      If that holds it retires the whole disputed-counts problem: a
      geometric count has at least three free parameters (segment
      granularity, endpoint definition, which copper counts) and an
      inversion count has none, so two correct implementations must agree.
      Note this measures the TOPOLOGICAL assignment's non-planarity — the
      lower bound a single-layer realization must respect — which is a
      different quantity from what the router actually draws. It is the
      right one for scoring pin swaps because it measures exactly what a
      swap can change.

   2. **Rotation and reflection as MOVE CLASSES.** The reason the current
      annealer cannot find the right ring cut is not the objective, it is
      the move set. Each rotation of a ring assignment is its own local
      optimum, and walking from one to the next via adjacent pin swaps
      passes through strictly worse states, with the barrier growing with
      n. Adding `ROTATE_GROUP(k)` and `REFLECT_GROUP` makes states that
      were n steps apart one step apart. Both are O(1) to propose and slot
      into the existing `PinSwapGroup` machinery
      (`pcb_route._resolve_pin_swap_groups`).

   3. **Swap groups must NEST (Reto).** Today `pin_swap_groups` is a flat
      list of sets on `pcb_components.meta` — the route-help skill's own
      "no partial-exclusion syntax yet". Real swappability is a tree:
      leaves are pins, an internal node means "my children are
      interchangeable as units". That covers a differential pair (swap
      within the pair AND pairs with each other), a bus (bits within a
      lane, lanes as units), and a driver with banks. The flat case is a
      depth-one tree. Consequence: rotation/reflection apply at every
      node, and the metric in piece 1 must range over the orderings the
      tree admits rather than over a flat set. This is a PQ-tree (P-node =
      children permute freely, Q-node = children reverse only), with the
      circular PC-tree variant matching the ring — subject to the same
      review caveat. **What a given part's tree IS stays design data**, a
      datasheet fact the author or generator supplies; the code's job is
      to support nesting.

   4. **Negotiated congestion for the rip-up loop**, PathFinder-style
      (McMurchie & Ebeling 1995): every routing resource carries
      `base + history` cost, each iteration rips up ALL nets and re-routes
      every one against accumulated costs, letting them overlap illegally
      at first; history rises monotonically on contested resources until
      someone yields. Two properties earn it a place here: it makes the
      result largely ORDER-INDEPENDENT, which is the route-ordering worry
      left over from the VDD_LOGIC episode; and history lives on
      RESOURCES, not nets, so a pin swap between iterations does not
      invalidate it.

      This also answers Reto's "if the board is 80% routed and we rip up
      one wire, can we go back?" — in this scheme you never rip one wire,
      so you never need to. Separately, if the outer discrete search does
      want a best-so-far, the snapshot is cheap: sketch is canonical and
      copper is derived, so the state is O(nets + pins), not O(copper).

   Tracelength is the tiebreak among crossing-free assignments, not the
   primary objective. The claim that minimum-inversions and minimum-length
   pick the SAME cut (the far side) is part of what went out for review —
   it may hold only for uniform spacing.

   ### CORRECTION 2026-09-29 — what the review actually returned

   Model it checked against: single layer, no vias, 2-pin nets, component
   body and escape array as obstacles, so wires live in an **annulus**
   (outer boundary = escape line ∪ board edge, inner = the pad ring).
   That is the geometry we care about, and it is NOT the geometry the
   textbook result applies to.

   **The root error: a strip result applied to an annulus.** "Minimum
   crossings = inversions" is true when BOTH orders are linear and wires
   are confined to the strip between them. The ring is not that.

   1. **No reflection — n unrollings, not 2n.** Disjoint arcs across an
      annulus must respect the induced orientation, so the pad cyclic
      order is read in a geometry-fixed sense. All n rotations are
      realisable (wrapping the far side is a Dehn twist); the mirror is
      not. **A `REFLECT_GROUP` move is therefore wrong** — it jumps to a
      state with ≈ n²/4 crossings. The only way to get the mirror is
      physical: mirror the footprint, or put the part on the other side.

   2. **Inversions are an UPPER bound, not the minimum, and not a lower
      bound.** Wires may wind individually. With wire `i` running from
      sorted escape `a_i` to pad `b_i + w_i` (`w_i ∈ Z` its winding), the
      crossing count is `Σ_{i<j} |w_i − w_j + [b_i > b_j]|`. Uniform `w`
      recovers the inversion count of one cut; non-uniform `w` can beat
      every cut (reviewer's n=4 case: best cut 2, best winding 1, with
      one wire wrapping the other way to cross one wire instead of two).
      Minimising over `w` is a min-cost-tension problem — the dual of
      min-cost flow, totally unimodular, so still polynomial — but it is
      **not counting inversions**.

      Consequence for item 4 of the original piece list: a router that
      wraps one wire the other way can legitimately produce FEWER
      crossings than the inversion count. Treating that as a measurement
      bug would be exactly backwards.

   3. **"Definition-free" does not survive.** The inversion count is
      canonical once two linear orders exist; *producing* those orders is
      where the parameters live — the angular sort centre for the pad
      ring (corner pads, double-row or non-convex footprints, ties), the
      projection axis when escapes are not collinear, and which nets
      participate (multi-pin, power, unconnected pads). On top of that,
      fixed-cut vs min-over-cuts vs min-over-windings are **three
      different numbers**. So this does not retire
      `probe-numbers-need-a-pinned-method`; it reduces the parameter
      count and moves the remaining ones somewhere more inspectable.

   4. **The basin story is false for this objective.** Under a fixed cut
      the inversion landscape has *no* non-global local optimum: any
      unsorted sequence has an adjacent descent that lowers inversions by
      exactly 1 (bubble sort), so plain greedy descent reaches the
      optimum. Under min-over-cuts, all n rotations score 0 — there is no
      "right rotation" for a crossing objective to find at all. The
      inter-rotation barrier is real but small (measured 1, 2, 2 for
      n = 4, 5, 6; ≈ ⌊(n−1)/2⌋), so "not at any usable temperature" was
      unsupported. Rotations only matter once a **length** term
      distinguishes them — and then the answer is to evaluate the n
      rotations directly, not to anneal toward one.

   5. **PQ/PC-trees: right definitions, wrong fit.** They need each swap
      group's pins CONTIGUOUS in the pad order (a bank scattered around a
      package is not a PQ language); a subgroup with a rigid internal
      order (an op-amp gate's +/−/out) is neither P nor Q and must be a
      compound leaf or you over-admit reversals; and a Q-node's reversal
      is not an EDA primitive except for 2-element groups. The category
      error: for the ring the admissible set is one big P-node, while
      "cyclic order up to rotation" is the PLANAR set imposed by
      geometry — and PQ/PC nodes always identify a sequence with its
      reverse, which point 1 says is wrong here. Nested swap groups
      remain real and worth supporting (Reto's point stands); they are
      just not a PQ-tree.

   6. **Nearest relative is level planarity, and the constrained version
      is NP-complete.** Jünger–Leipert–Mutzel for level planarity,
      Bachmaier–Brandenburg–Forster (PQR) for radial/cyclic. With order
      constraints inside a level — which is what swap groups are —
      Constrained Level Planarity is NP-complete from 4 levels
      (Brückner–Rutter), polynomial for single-source and height ≤ 3. And
      the PQ family answers feasibility only; it does not minimise.

   7. **Far-side cut: false, and my reason for it was backwards.** With
      the cut at the far side, the pads *nearest* the escapes land in the
      MIDDLE of the linear order and the farthest pads sit at both ends —
      the opposite of what was argued. Inversions do not select a cut at
      all (every rotation scores 0); only length distinguishes them, and
      the length-optimal rotation moves toward the antipode of the escape
      centroid as soon as the escape column is off-centre or spacing is
      uneven. The far-side cut is the symmetric special case.

   **Scope correction that changes the build most.** For ONE ring fan
   there is nothing to anneal: the exact answer is n rotations × sort
   within each contiguous swap group × a min-cost-tension solve for the
   windings. Annealing only earns its place once several components with
   swap groups sit on BOTH ends of shared nets and couple. Say that as
   the scope before building a search.

   **Testing trap, stated so we don't walk into it.** With uniform
   spacing, winding-minimum equals rotation-minimum for n ≤ 6 — the two
   only diverge under non-uniform escape/pad spacing. A unit test built
   on evenly-spaced pads would therefore pass while testing nothing,
   which is the vacuous-green shape this campaign has already hit twice.

   **What survives unreviewed:** the negotiated-congestion rip-up loop
   (piece 4) was not among the claims sent out, and the cheap-snapshot
   answer (sketch canonical, copper derived) is a property of this
   codebase rather than a theorem.

   **Verification status.** Points 1, 4 (the bubble-sort half) and 7 were
   re-derived independently here and are certainly right. The n = 4
   winding counterexample, the crossing formula, and the measured barrier
   heights come from the reviewer's own brute force and have NOT been
   independently reproduced — reproduce them before relying on the exact
   figures, per this campaign's own standing lesson.

   **MEASURED 2026-09-28, NOT REPRODUCIBLE — see the warning above.**
   Crossings among the REALIZED escape paths on the 50/54 run
   (In2.Cu open, seed 1), counted with an exact orientation-sign proper
   intersection test over `Fraction` — no epsilon — with shared endpoints
   and T-touches excluded, then split by whether each segment is authored
   template copper or router-drawn:

   | bucket | crossings | net pairs | within plaza | between |
   | --- | --- | --- | --- | --- |
   | routed × routed (swappable) | **51** | 41 | 29 | 22 |
   | routed × fixed (half) | 27 | 20 | 24 | 3 |
   | fixed × fixed (unswappable) | **0** | 0 | 0 | 0 |
   | total | 78 | 56 | 53 | 25 |

   Same-layer crossings: **0** in every bucket, consistent with the
   board's zero clearance violations — so none of the 78 is a latent DRC
   bug, and all are layer-change crossings.

   Two things follow. **The swappable bucket is the answer**: 51
   crossings over 41 net pairs is nowhere near zero, so scoring swaps on
   routed-path crossings has something real to optimise. And **the
   authored fabric is exonerated** — fixed × fixed is exactly 0, so the
   template's own vias and stubs never cross each other; the whole
   crossing signature is routed-side. That is the strongest available
   support for "keep every template via, win by ordering".

   **Point 6's zero is not evidence against this and must not be cited as
   such.** It counted straight-line airwires. Measured on the geometry
   the claim is about, the same board has 78.

   **But do not expect ordering alone to close the 4 failures.** Each of
   them records, as its FIRST-listed problem, a board-wide capacity wall:

   ```
   gap 0.093 mm between instances (0, 1, 7) fits 0 strand(s) at
   0.300 mm pitch, but 55 want through (… 54 escapes + VDD_LOGIC …)
   — needs 16.500 mm
   ```

   Byte-identical across all 4. It is recorded only on the failures, but
   that is an artefact of `pcb_routes` storing a `problems` payload only
   for a net whose row failed — the finding's own `usage: 55` names every
   escape plus `VDD_LOGIC` as wanting that gap. So it is one physical
   gap, shared, admitting **zero** strands. Reordering can change which
   nets win the alternate corridors (2 of the 4 fail with `congestion`,
   a race outcome); it cannot widen the gap (2 fail `no_path`, walled in).

   **This makes the 0.093 mm gap a PLACEMENT finding, not a routing one** —
   instances 0, 1 and 7 are placed 16.4 mm closer than the escape fan
   needs. That is item 4's territory (polygon keep-out + the
   side-blindness fix), and it is now the first thing item 4 should be
   measured against. Filed as its own item:
   `docs/backlog/pcb-placer-starves-the-escape-corridor.md`.

   **So this item becomes** "score swaps on routed-path crossings" —
   build it. Its honest ceiling is the 51 swappable crossings and
   whichever of the 4 failures are races, not all 4.

   **The bent-airwire proxy was measured 2026-09-28 and is NOT good
   enough alone.** Hypothesis: score swaps on a two-leg polyline
   (electrode pad → its own plaza drop-via → channel land), cheap enough
   for an optimizer loop. Measured against that probe's own
   self-consistent realized ground truth (70 crossings / 52 pairs):

   | | |
   | --- | --- |
   | bent-proxy crossings | 223 (192 net pairs) |
   | net-pair RECALL vs realized | 21/52 = **40%** |
   | net-pair PRECISION | 21/192 = **11%** |
   | Spearman rho, per-plaza (n=9) | 0.608 |
   | spread over 20 random channel permutations | 426–627, **19 distinct** |

   Read: it passes the non-degeneracy gate decisively (a metric returning
   the same score under every permutation would be useless however well
   it correlated, and this one moves), but it **fails on recall** — an
   optimizer driven by it would be blind to 60% of the crossings it
   exists to remove while spending most of its effort on the 89% of
   flagged pairs that are not real. Bending at the plaza via is right in
   KIND and insufficient in DEGREE. So the options are a proxy with more
   legs (actual breakout/corridor topology, not one via) or maze-lite
   routes in the loop — a scope question, not a detail.

   *Retracted from the same measurement:* a detour-ratio distribution
   (min 0.082 / median 1.174 / max 4.453). A ratio below 1 is impossible
   for a connected path, which proves the denominator used the wrong
   endpoint pair (electrode body pad → sink land, where the router
   actually connects plaza drop-via → sink). Do not quote those numbers.
   A corrected detour metric is separate work.

4. **Polygon keep-out as a placer primitive.** `optimize.py`'s only
   non-instance obstacle is mounting-hole circles
   (`_hole_keepout_radius_mm`). Ship the primitive alone, and with it the
   side-awareness fix: the placer's courtyard term is side-BLIND (no
   bottom handling anywhere in `optimize.py`/`cost.py`) while DRC's is
   side-aware (`drc.py`, `bottom_by_refdes`). Without that fix a keep-out
   of "array extent + HV" over a bottom-side driver pushes it off the
   array — which is the arm that measured WORSE (35 → 32).

5. **Unpin the driver** (generator-side, after 4). Reto: "driver should
   be placed where it falls, there is no requirement it be directly
   below." Deletes the `fixed='both'` and the escape-locality
   justification on the sink component. This is what gr451274(b)/(c)
   becomes. Do NOT claim it closes gr451052 — that gripe's code trail is
   a `check_via_pad_keepout` NAMING fix in `drc.py` (router vias vs
   driver pads), which a placer keep-out does not touch. Re-read the
   gripe before closing it.

6. **Pre-route DRC gate** (gr451274a). Cheap once 1 lands, meaningless
   before it.

### Deferred out of this item: `escape.py`

`precis.pcb.escape` is still the only general escape primitive and still
has zero importers — but wiring it into THIS generator relocates nothing
and produces a number with no consumer:

- Its input is `number/x/y/w/h` pad dicts (`compute_shells`,
  `gap_capacity`); electrode pads are `shape: polygon` with none of those
  fields.
- Its answer for this board is 0. Electrode gaps:
  `gap_capacity(2.25, 2.15, 0.15, 0.15)` → width negative → 0. Plaza
  vias as pads: `floor((0.4 - 0.30) / 0.30)` → 0. `_layers_for_escape`
  skips zero-capacity shells, so `required_layers` = 1.
- The generator ALREADY asks the real plaza question, with a stricter
  HV-aware constraint: `_plaza_capacity` / `_pair_floor` / `_foreign_req`
  (`generators.py`). Zero capacity between plaza vias is exactly WHY
  every via gets a radial breakout. (An earlier draft of this item said
  the fabric "assumes rather than asks" — that was wrong.)
- `part_footprints.escape` (migration 0140) is an unpopulated cache:
  `part_footprint_put` has no callers.

If `escape.py` is wanted, the real item is "populate
`part_footprints.escape` on footprint pull, with a named consumer"
(`required_layers` feeding the stackup story) — a footprint-pull change,
not a fabric change, and not in the same round as a behaviour change.

### Tests item 3 would break (none of which the earlier draft listed)

Every 3x3 fixture is ALL rim, so a rim-skipping generator emits zero
fabric: `test_pcb_ewod_generator.py`, `test_pcb_ewod_fabric.py`,
`test_pcb_ewod_capability_map.py`, `test_pcb_ewod_generator_drc.py`.
`variant='rim'` would emit nothing at all. The 6x6 sink shares shift
because rim pins leave `chain_pins`. And the dogfood's `>= 6` escape
floor is far too loose to catch a 35 → 10 regression — tighten it to a
real number in item 1.

## Blocked on / needs deciding first

- ~~**`put(stackup=...)` authoring (Slice 3) does not exist.**~~ —
  **SHIPPED 2026-09-27** as `put(args={'op':'stackup','layers':[...]})`
  (`handlers/pcb.py::_op_stackup`, `ir.py::validate_stackup`,
  `store.pcb_set_stackup`). This was the whole layer story: every board
  was born on `DEFAULT_STACKUP` and the row was never writable after, so
  "which layers may carry a trace" was engine policy instead of design
  data. Nothing downstream needed changing — `layer_is_routable`,
  `layer_is_pourable` and `process_for_stackup` already read the board's
  own stackup. Escape layers were already DERIVED ("every signal layer
  the electrode field does not own"), so they widen by themselves.
  **In2 as signal, In1 kept as GND is the arrangement, and it is the
  board's call, not the engine's** — for a 250V switcher beside analog
  sensing giving up the ground plane would be a real trade. The unknown
  key check is load-bearing: misspell `routable` and
  `layer_is_routable` falls back to `role`, so the instruction is
  accepted, stored, and never honoured.

  **Authoring it exposed two pour bugs, both fixed in the same round.**
  A GND plane on In1.Cu had never been expressible, so nobody had seen
  what `plane_pours` does with one: **61 `clearance` errors**.
  - `_pour_planes` fed `plane_pours` router-drawn copper + pads +
    mounting holes, never the AUTHORED `fixed_copper` rows
    `_claim_fixed_copper` already respects on the router's grid — so the
    fill flooded over every plaza via barrel. 61 → 7.
  - `_pad_blockers` pinned each pad to one layer, but `pads_for_ir`'s
    own docstring names `pad["drill"]` as the "spans every layer" signal
    DRC reads — so an inner plane poured solid through every
    through-hole barrel. 7 → **0**.

  Both are the same shape as item 2's router bug: a fixture the pass
  cannot see is one it draws through. Pinned by
  `tests/test_pcb_pour_blockers.py`, including the complement (a plane
  must still swallow its OWN authored copper), and each verified to fail
  with the fix reverted.

  **And a third, FIXED 2026-09-28: the GND-plane arm's 3 islands were
  not a stitching gap — `_plane_fanout` dropped every pin from `PAD_LAYER`.**
  I had filed this as "`_stitch_plane_fragments` does not close it"; the
  stitcher was never the pass at fault. `_plane_fanout` spans the drop via
  `min/max([PAD_LAYER, *plane_layers])` and draws the stub on `PAD_LAYER`,
  so a BOTTOM-mounted pin got a via F.Cu→In1.Cu that never reached B.Cu
  and a stub on bare board above its own pad. Its docstring already
  claimed "from the pad's own layer", and diagnoses this exact mistake one
  paragraph earlier for `ir.seg_layer` — then repeats it. Unreachable
  before: `DEFAULT_STACKUP` declares `plane_net: GND` on In1.Cu, but that
  key was DEAD until `op='stackup'` applied it, so no board had ever
  poured a plane under a bottom-side part. Fix = `_side_layer` against the
  OUTER index pair (mount side decides a pad's layer; routability has no
  say). Pinned by `test_pcb_realize.py::test_plane_fanout_drops_a_bottom_
  mounted_pin_from_its_OWN_layer`, verified red without it.

  Measured on the dogfood at seed 1, In1.Cu poured GND:

  | | before | after |
  | --- | --- | --- |
  | GND islands | 3, reporting `realized` | **1** |
  | escapes | 50 / 54 | 50 / 54 |
  | nets not realized | 4 | **4** |

  **There was no price. The extra failure was a second bug in the same
  fix, and this paragraph used to say otherwise.** For one commit
  (`7b747cc0` alone) `VDD_LOGIC` went `realized` → `failed: congestion`
  and that was written up here as the honest cost of connecting GND
  properly — a route-ORDER problem for the negotiated-congestion question.
  It was not. `_drop_via_site` was still checking the stub corridor on
  `PAD_LAYER` while `_plane_fanout` drew the stub on the pin's own layer,
  so GND's two bottom-side stubs were placed across `VDD_LOGIC`'s B.Cu
  pads having cleared nothing — a potential short, reported one pass
  downstream as the victim's routing failure. `169d69ee` fixed the
  corridor check; measured after it: histogram `{'realized': 59,
  'failed': 4}`, `VDD_LOGIC` back to `realized`, GND still one island.
  So the board gets the GND fix for free.

  **The lesson is about the label, not the layer.** A `congestion`
  status on a net that never contended is what a clearance check asked of
  the wrong layer looks like from downstream. Treat `congestion` on a net
  adjacent to a plane fanout as a layer-mismatch suspect before accepting
  it as contention — this cost a round of reasoning about router ordering
  that had nothing to do with the defect.
- **Router annealing: not now.** `congestion` is the smaller failure
  class and goes to 0 as soon as layers open. If a global method is ever
  needed the answer is negotiated congestion (PathFinder), not annealing —
  annealing belongs to placement, where `optimize.py` already uses it.

## Explicitly NOT in scope

- Splitting `ewod_pad_array` into `pad_array` / `escape_fabric` /
  `driver_chain`. The table above is the map for when it happens; doing it
  in the same round as a behaviour change makes both unreviewable.
- The `max()` clearance collapse (worth zero, see above).
- Promoting footprint pads to IR pins.
- Touching the star decomposition or per-cell grid ownership. The router
  DEPENDS on the star; a true MST reddened the ESP32-C3 acceptance board
  at every seed.

## Acceptance criteria

- Escape yield pinned by a REAL number in the dogfood test, re-baselined
  on each behaviour change. **Two baselines now, because the board has
  two stackups**: `>= 24` on the default one-routing-layer board
  (measured 28), `>= 46` with In2.Cu authored as signal (measured 50).
  46 came from the illegal arm and was never a target; the layer arm
  beat it legally. The old `>= 6` could not tell 35 from 10, which is
  how both the fixture swap and a 7-escape drop went unnoticed.
- **Run the SLOW lane before shipping anything in this area.**
  `test_dogfood_route_op_routes_real_geometry...` is `@slow`, so
  `scripts/test -m 'not slow'` skips it and the GitHub shards do not —
  that is exactly how item 2's dangling-GND regression reached a red gate.
- Zero courtyard findings between the array and its driver after item 5.
  gr451052 is a SEPARATE observability fix already shipped — re-read it
  before closing, do not assume this round closes it.
- Every footprint pad on every placed instance appears in DRC's pad set
  and the fab render (done, item 1), AND in the router's obstacle set
  (item 2, outstanding). Pinned by a test giving a part more footprint
  pads than the netlist declares — the ring fixture already does this
  (11 unnamed `NC` pads).
- No escape ever routes on the electrode layer, whatever the stackup opens
  up. Already asserted in `test_pcb_ewod_dogfood.py`.
- `tests/test_pcb_reference_end_to_end.py` and
  `tests/test_pcb_fab_render_all_layers.py` green — other shards, run by
  name.

## Probe recipe

Write a `@pytest.mark.slow` throwaway that imports `_seed`,
`_drain_one_job` and the `pcb` fixture from
`tests/test_pcb_ewod_dogfood.py`, calls `pcb.put(op='route', seed=1)`,
and writes a JSON/JSONL row per arm. Results go to the worktree root,
never `/tmp` — `scripts/test` runs in a container and only the worktree
is mounted. Delete it on ship; the numbers belong in the table above,
the file does not.

Each arm monkeypatches `precis.pcb.realize`: `_claim_fixed_copper` →
no-op for the fabric arm; `_net_class_layers` → all signal layers for
the layer arm (**which is why that arm is invalid — it admits F.Cu**);
`_resolve_track_rules` AND `RealizeConfig` together for clearance, since
the config default is a floor and any net without a class override falls
through to the fab house tier; `_unclaimed_pad_claims` → `[]` to measure
against the pre-item-2 board. Sink placement is moved by wrapping
`generators._REGISTRY["ewod_pad_array"]`.

**Author the stackup in the arm, not just `escape_layers`.** The two are
independent and a change to one alone measures nothing: a layer nothing
is allowed to use stays empty, and a class allowed onto a layer the
stackup calls a plane fails `layer_lock`. Round 10's arm was
`put(args={'op':'stackup', ...})` with In2.Cu `signal` PLUS
`escape_layers: ["B.Cu","In2.Cu"]` threaded into the generator params.

**Measure DRC, not just yield.** Build DRC's own model
(`{"layers", "copper": pcb_copper_list(...), "pads": pcb._drc_pads(...)}`)
and run `drc.check_clearance` against
`capability_for(drc.process_for_stackup(stackup))`, plus
`connectivity.net_islands` over the same model. Escape yield alone could
not tell "routed" from "routed through a land" — that is how 35/54 stood
for a week with 24 shorts under it. To spy on the router itself,
monkeypatch `realize._tracks_from_path` (it is handed the `RoutePath`,
so `.points` and `.vias` are both there) and `realize._realize_maze`
(for the `ir`).

## Target + blast radius

`pcb/generators.py` (`_expand_ewod_pad_array`, `_parse_sink_grid`),
`pcb/escape.py` (wiring it up at all), `pcb/optimize.py` (polygon
keep-out), `pcb/ir.py` (pad set), `pcb/session.py`
(`apply_real_pin_offsets`), `pcb/realize.py` (`pads_for_ir`,
`_claim_fixed_copper`), `pcb/drc.py`, `handlers/pcb.py`.

### 2026-10-05 faithful-snapshot diagnosis and bounded grid repair

Claude/Reto reopened ewod-pcb routing; EasyEDA remains parked. Reto explicitly
permits route/place/rebuild on ewod-dogfood-N, which are public dogfood data;
non-dogfood boards remain protected. Snapshot/replay is internal, no schema/op.
Source branch work/pcb/board-snapshot-replay, snapshot checkpoint a101e5665.
Exact fixture tests/fixtures/pcb/ewod-dogfood-6-replay-v1.json.gz captures 2instances,
58nets, 55escape connections. Its pre-reroute status is22routed/33failed/3dangling;
fixed-pose realization reproduces22/33 and zero routed geometric DRC errors.

All17 historical no_path labels are congestion under the ACTUAL route request:
_seg_request's fixed-copper island terminals, permitted B.Cu layer and full width
find a path for each on the static-only grid. _diagnose_unrouted probes native pad
centres/board-wide layers instead; diagnostic parity is a separate known defect,
not evidence to remove fixed copper, widen layer locks or invent topology.

Measured candidate comparison (same stored poses, pads, fixed copper and rules):
existing four-iteration negotiation22routed; resolved maximum across58net rules
still0.15mm and22routed; half default grid-pitch cap23routed; quarter arm also23 because
grid_for's existing minimum pitch clamps it. Every arm has zero routed DRC errors.
Select the smallest measured routing change: _PITCH_PER_CLEARANCE 2/3→1/3.
Bump route CODE_VERSION2→3 so unchanged requests do not reuse old jobs.
No clearance/width/voltage/layer/copper rule changes; grid_for's floor/bounds/search
budget remain authoritative. It recovers5 of the17 (R1C3,R1C5,R1C6,R2C3,R6C1),
12previouslyfailed nets total, while11previouslyrouted nets now fail: net+1.
This is the strongest measured bounded candidate, not a global routing optimum.

Acceptance: canonical slow fixture regression forces old coefficient for baseline
22routed/33failed/3dangling, then uses new default twice and requires identical
failed-net set and complete realized copper,23routed/32failed/3dangling. Geometric
DRC must remain zero errors and all escape router tracks must remain B.Cu. Fresh
copy only; no source implementation/deploy on production before Codex review.
Adjacent realizer/fixed-copper/bottom-pad/negotiation tests, scoped container types,
Ruff/format/diff check. Root owns full gate/version/merge/deploy. Postdeploy replay
may reroute the labelled dogfood board; source-review checkpoint reports fixture
before/after honestly, not an already-deployed production improvement.


### R14 snapshot review correction — negative cache fidelity

R13 excludes optional snapshot a101e5665 after independent P2 review. For every
non-NULL part_lcsc referenced by captured components, replay must enforce
source presence/absence in BOTH parts and part_footprints, as well as existing
row equality. A target row for a source-absent key refuses the entire design
transaction with table/key guidance; never delete/overwrite target caches or
invent source placeholders. Unreferenced target cache rows are irrelevant.
Keep existing source-present cache insertion/equality, fresh slug/dev-only
guards and no-job behavior unchanged. No API/schema/routing changes.

Acceptance: actual C639448 source-absent catalog shape and source-absent
referenced footprint each refuse with all design/identifier/checkpoint counts
unchanged and target cache data identical; transaction remains usable.
Unrelated target cache keys and NULL/unbound components still replay exactly.
Focused canonical snapshot tests, scoped container types and Ruff/diff checks;
publish exact R14 candidate for review, root owns integration/release.


### R14 route-gate repair checkpoint — frozen 53e6cb255

Local/main CI/release CI all fail the ESP32 reference seed5 and fab-film
seeds2/3/4 at route-job drain after successful placement. Old job errors were
not retained. Global pitch2/3→1/3 plus route CODE_VERSION3 is a hypothesis,
not an established cause. First run ONE affected canonical case with test-local
job meta.error and persisted route-summary evidence captured before DB cleanup.
Then fix only the demonstrated cause; preserve every routing/DRC/render ratchet,
budgets, success semantics and seed set. No threshold lowering or broad skips.

Acceptance: affected reference5 and fab2/3/4 pass with retained exact failure/
after evidence; demonstrate ewod fixed-pose22→23 tradeoff under the repair.
Run focused adjacent tests/types/Ruff only. Root owns source review and renewed
full gate. New isolated work/pcb/r14-route-gate-repair at frozen53e6cb255;
preserve prior owner trees/fixtures. Snapshot dead-export correction is separate.
No production/native writes, provider/service/image/CI/release operations.


R14 controlled diagnosis: frozen fine-cap reference5 fails recorded
non-convergence, EN congestion, partial copper persisted. With ONLY cap restored
to2/3, same canonical reference5 passes (11/11 multi-pin nets plus all unchanged
DRC ratchets). Proposed minimal correction withdraws the global1/3 refinement,
retains explicit fine-cap EWOD experiment and its22→23/determinism/DRC assertions,
and advances route epoch3→4 so old fine-grid jobs cannot be reused. No new opt-in
API, fixture-specific selection, best-of retry or extra search budget is proposed.
Default EWOD count returns22: the unshipped global gain is honestly withdrawn,
not repaired through a special case. Root review must assess this tradeoff.


## R15 dogfood ABC experiment contract (2026-10-06)

Owner pcb/ewod-pcb; isolated work/pcb/r15-dogfood-abc from deployed
110f6dd4b464565a6b5503f4c48451872b5a6089 (8.35.13). Reto authorizes
only ewod-dogfood-6, pcb458868, for this slice. Other boards' defaults
remain unchanged. Baseline job469182: 22 realized / 33 failed / 3 dangling,
seed0, iters3000, epoch4/coarse default. New arms in order B,A,C,ABC,
each seed0/iters3000; do not mislabel sequential accumulated changes as
independent arms. Retain per-job raw state and source/config manifests.

B: existing native put(kind=pcb,id=ewod-dogfood-6,args={op:route,seed:0,
iters:3000,negotiate:10}); ten PathFinder iterations, existing 60s budget,
only keep a proposal if it routes more. Default negotiate=0 unchanged.
Capture baseline before B, actual terminal job metadata and after-state.

A: gr469871 records confirmed crossing-only cost defect. Opt-in Manhattan
length from the actual route terminal at the far end to each candidate
physical pad plus 2mm per crossing, same Hungarian permutation and existing
canonical swap/writeback constraints. Use Hungarian for this arm's warm
start instead of radial angular ordering. Unselected route jobs retain
existing assignment behavior until measurements/review. Fixed copper's
escape via terminal must be resolved honestly; pad-centre IR endpoints
are not silently called vias. No new schema/provider/global router default.

C premise checkpoint: generators.py version3 intentionally fixes ARR1
because authored electrode/plaza copper is its design in an absolute
frame; ARR1_SINK_0 already has fixed=NULL on the actual board. Array stays
fixed. Existing anneal translates this free sink, but ROTATE is explicitly
cost-neutral and SIDE_FLIP changes a segment's obstacle-side choice, not
the component's physical top/bottom side. A fixed=NULL edit alone does
not create the requested rotation/side search. Prove a supported pose
search or report the missing seam before claiming an unfix gain.

Metrics: terminal realized/failed/dangling; full exposed DRC error/warning
counts (separate from job's pre-route and stripping checks); routed length
and arithmetic mean per-routed-net detour (length / Manhattan distance).
Define copper inclusion and endpoints explicitly before calculating;
dangling/zero-distance nets have no ratio, never silently divide by zero.
Retain original baseline snapshot before any write. Read-only runtime
Store capture uses REPEATABLE READ READ ONLY SELECTs for fields omitted
by native views. No public snapshot/clone claim: loader is dev/test only.

Focused canonical regressions, scoped container types and Ruff; independent
source review and normal root land precede production use of new code.
Ready origin/work/pcb/r15-dogfood-abc must match exact full candidate SHA.
No fullsuite/CI/deploy/service/provider/non-dogfood changes; scientific,
Catpath0.22 and EasyEDA holds remain. No prune or shared /tmp scripts.

### B prerequisite: native refusal reproduced, gr469872

The deployed B request above returns typed BadInput before enqueue:
job_type=pcb_route unknown params [negotiate]; allowed iters/pcb_ref_id/seed.
No route job or board mutation. The focused canonical handler regression
fails identically. Minimal prerequisite adds only the existing knob to
PARAMS_SCHEMA; no epoch/version/default/algorithm change. Tests retain
0/10/100 through actual job metadata and dedup, omitted remains absent/off,
existing -1/101 refusal stays, and worker config reaches the realizer.
Normal independent review/root land/deploy is required before native B
can run. A/C/ABC have not run; do not supply invented job ids or results.

Direct job puts also become reachable through this registration. The v1
shared params validator checks type, not numeric min/max, so the worker
enforces the existing 0..100 cap before its first board read. Focused
-1/101 direct-job controls assert refusal without reaching pcb_graph.
No validator rewrite, no default or budget change.
