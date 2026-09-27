---
status: draft
title: Escape and driver-chain are general PCB primitives wearing EWOD names
prio: high
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
| **ring fixture, as shipped** | **35 / 54** | 13 | 6 | 10 |
| sink moved outside the array | 32 / 54 | 15 | 7 | 5 |
| **escape fabric not claimed as an obstacle** | **46 / 54** | 4 | 4 | 5 |
| clearance forced 0.15 → 0.093 | 35 / 54 | 10 | 9 | 10 |
| "any signal layer" escape class | 42 / 54 | 12 | 0 | 25 |
| fabric not claimed + any signal layer | 54 / 54 | 0 | 0 | 26 |

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
5. **The fabric is the one measured lever**: 35 → 46, `no_path` 13 → 4.
6. **The realized escapes are REAL copper, and the assignment is already
   ordered.** Audited 2026-09-27
   (`tests/test_pcb_escape_audit_probe.py`, throwaway), re-run after the
   pad-set fix: of the realized escapes, zero carry an explanatory
   `note` (so none is a
   dangling-net freebie counted as realized) and zero have copper in more
   than one island per `connectivity.net_islands` over the exact fab
   copper. EVERY failure DOES hold copper, in ≥2 pieces — electrode stub
   and driver stub, never joined. **53 pin swaps settled and all 54
   electrode→channel airwires cross ZERO times**, unchanged whether or
   not the router claims the unclaimed lands, so the channel assignment is monotone and is NOT why they
   fail. (Straight-line proxy, not routed paths — but it is the thing
   "are they in order" asks.) Do not spend a round on channel assignment
   or crossing minimisation.

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

2. **Claim unclaimed pads in the ROUTER's grid** (the other half of
   gr451276, and the half that answers Reto's actual words: "no wire can
   route thru it, even if it is nc"). **Attempted 2026-09-27 and backed
   out — do not retry without reading this.**

   Mechanism that worked: a 4-tuple `(point, owner, PadShape, layers)`
   appended to `_realize_maze`'s `pads` list, owner =
   `ir.n_nets + ir.n_pins + k`, extending the per-pin NC sentinel scheme
   that already keeps `NO_NET` distinct from `maze.FREE`.

   **What broke:** `test_dogfood_route_op_routes_real_geometry...` (SLOW
   — only the unfiltered lane runs it). Two GND tracks come out dangling,
   e.g. a B.Cu track ending at `(14.514, -5.672)` where a dump shows no
   pad, no via and no other copper within 2 mm, while GND still reports
   `realized` with no note. Measured yield with it in: **35 → 28** (the
   lands cost 7 escapes), so the effect is real and the change is worth
   finishing.

   **Ruled out** (identical failing coordinates in all four variants, so
   none of these is the cause): feeding the extra points to
   `maze.grid_for` (which re-cuts the grid pitch board-wide — keep them
   out of it regardless, that reasoning stands); stamp ORDER vs
   `_stamp_pads`' pass-3 `claim_centre` (tried first and last);
   pad-identity vs label claiming.

   **One real bug found and fixed on the way, worth keeping:** claiming a
   pin's own extra same-label pads (a split thermal slug, an EWOD
   electrode's body + stub taper) as "unclaimed" gives them `net=""`,
   which drops them from their net's pad set AND makes them foreign
   obstacles to their own net. A pad is unclaimed iff its label matches NO
   pin; `_real_pad_sizes`' "first wins" decides which pad's SIZE stands
   for a pin, not which pads belong to it.

   **Next suspect, untested:** `_stamp_pads` pass 2 populates
   `grid._pads`, which `via_clears_pads`/`_pad_keepout_mask` read as the
   via keep-out, and `_straighten(shove_vias=...)` moves vias after a path
   is built. A via refused or shoved without its track following would
   produce exactly this dangling shape. Start by re-running with
   `config.straighten` off and with via shoving off, separately.

   **Residual gap either way:** the non-first pads of a multi-pad pin stay
   invisible to the router and DRC, since both index per pin. Closing that
   needs those pads carried WITH their pin's net, not as netless
   obstacles.

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
   unmeasured arm. Add it to `test_pcb_round9_probe.py` first.

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

- **`put(stackup=...)` authoring (Slice 3) does not exist.** This is why
  the layer story is stuck: `DEFAULT_STACKUP` makes In1.Cu a GND plane and
  In2.Cu a plane, so F.Cu and B.Cu are the only SIGNAL layers and F.Cu is
  the electrode field. **B.Cu is the only routing layer this board has.**
  The 2026-09-19 B.Cu lock was costing nothing; the stackup is. Shipped
  2026-09-27: escape layers are now DERIVED as "every signal layer the
  electrode field does not own", which evaluates to `["B.Cu"]` today and
  widens by itself once inner signal layers exist — no ruling to reverse.
- **Giving up In1 means giving up the ground plane.** For a 250V switcher
  beside analog sensing that is a real trade, not free layers. In2 as
  signal and In1 kept as GND gives TWO routing layers; decide before
  building.
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
  on each behaviour change. 35/54 today; item 2 takes it to 28 when it
  lands. 46 came from the illegal arm and is not a target. The floor is
  `>= 30` now — the old `>= 6` could not tell 35 from 10, which is how
  both the fixture swap and a 7-escape drop went unnoticed.
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

`tests/test_pcb_round9_probe.py` (landed; delete on ship) carries the
arms. Each monkeypatches `precis.pcb.realize`: `_claim_fixed_copper` → no-op
for the fabric arm; `_net_class_layers` → all signal layers for the layer
arm (**which is why that arm is invalid — it admits F.Cu**);
`_resolve_track_rules` AND `RealizeConfig` together for clearance, since
the config default is a floor and any net without a class override falls
through to the fab house tier. Sink placement is moved by wrapping
`generators._REGISTRY["ewod_pad_array"]`. Results go to a JSONL at the
worktree root, never `/tmp` — `scripts/test` runs in a container and only
the worktree is mounted.

## Target + blast radius

`pcb/generators.py` (`_expand_ewod_pad_array`, `_parse_sink_grid`),
`pcb/escape.py` (wiring it up at all), `pcb/optimize.py` (polygon
keep-out), `pcb/ir.py` (pad set), `pcb/session.py`
(`apply_real_pin_offsets`), `pcb/realize.py` (`pads_for_ir`,
`_claim_fixed_copper`), `pcb/drc.py`, `handlers/pcb.py`.
