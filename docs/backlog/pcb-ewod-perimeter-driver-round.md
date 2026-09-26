---
status: draft
title: EWOD round 9 — drive the array from OUTSIDE it, so escape is a fan-out and not a search
prio: high
---

# EWOD round 9 — drive the array from OUTSIDE it

## Motivation / why

Round 8 shipped a board where 34 of 62 nets realize. Four measured
experiments this round (probes 1-4, `tests/test_pcb_*_probe.py` at the
time of writing, deleted on ship) say the remaining 28 are not a tuning
problem:

| lever | escapes realized (of 54) |
| --- | --- |
| baseline | 13 |
| electrode pitch 2.0 -> 2.25mm | 15 (measured in prod, round 8) |
| maze clearance 0.15 -> 0.05mm (below ANY fab minimum) | 16 |
| per-net layer lock lifted | 15 |
| **generator's own escape fabric not claimed as an obstacle** | **22** |

> **READ THIS BEFORE TRUSTING THE TABLE.** Every row except the pitch one
> was measured on `tests/test_pcb_ewod_dogfood.py`'s fixture, whose HV507
> stand-in is `_grid_footprint(cols=9)` -- a SOLID 8x9 grid, 1.0mm pitch,
> 0.5mm pads, so ~31 pads are INTERIOR. At the 0.15mm the grid dilates by,
> those interior pads are unreachable by construction. Prod's real
> C639448 is a PQFP-80: **80 pads, ZERO interior**, 17x23mm (queried
> against prod 2026-09-26). The fixture manufactures a wall prod does not
> have, which is why it sits at 13/54 while prod sits at 34/62. Re-measure
> against a ring-shaped stand-in (item 0) before designing to these
> numbers.

Three dimensional levers move the number ~5% each. Removing the fabric
from the obstacle set moves it 70%. And the verdict on the failures is
`no_path`, which `_diagnose_unrouted` only returns after a probe grid
with *every other net's copper cleared away* fails at near-zero width:
"walled in, not merely too tight". The wall is not made of gap. It is
made of the fabric's own copper.

**The fabric exists because the driver sits UNDER the array.** That one
placement decision causes all of it: electrodes must drop through vias to
reach a driver on B.Cu; the vias and their stubs are 108 fixed tracks +
54 fixed vias of B.Cu congestion; `_claim_fixed_copper` claims each row
with its own net as owner, so every escape's stub blocks the other 53;
and the sink's pads overlap the array, which is gr451052's nine
keepout errors.

Move the driver off the array and none of that is needed. On B.Cu, with
no electrodes above competing for it, 64 vias in a grid fan out radially
to a driver on the perimeter. That is a planar problem with room to
spare: escaping the innermost ring of an 8x8 needs 3 traces between
adjacent perimeter vias; at 2.25mm pitch with a 0.6mm via land that gap
is ~1.65mm, and at 0.1mm trace + 0.09mm clearance it fits ~8.

The code already says this is the intended topology. The coating check's
own polarity argument states "outline margin, connectors and the sink
grid all sit outside it" (`generators.py`, `_coating_verdict`) while
`sink_grid` places the sink underneath.

## In scope

0. **Fix the fixture first, then re-measure.** Give the HV507 stand-in a
   PERIPHERAL pad ring (80 pads, no interior), matching the real
   C639448's shape, and re-run probes 1/2/4. Everything below is designed
   against the re-measured ranking, not the table above. This is a test
   change only and gates the rest of the round.

1. **A driver placement that does not overlap the array.** The generator
   grows a keep-clear region for the electrode field and places the sink
   (or sinks) outside it. Prefer: driver on the perimeter, rotation left
   FREE so the placer can use it.
2. **Unpin what must move.** Today every instance carries `fixed='both'`,
   so `optimize`'s existing `ROTATE` move (90 degree steps) and its
   existing `crossings` cost term are both dead code on this board. The
   generator should pin only what genuinely must be pinned (the electrode
   array itself) and leave the driver's rotation, at minimum, free.
3. ~~Escape as fan-out, drop the stub fabric.~~ **DELETED after review.**
   The B.Cu breakout stub is not there to reach the sink -- it is there
   to get the escape's start terminal OUT of its own 8-via plaza ring.
   Dropping it reinstates the "walled in even routed alone" failure
   recorded at `realize.py:1813-1826`, and the stub already points
   radially outward (`_breakout_far_point`). Keep it. Note also that the
   vias are not a 64-point grid: they sit 8-per-plaza in 9 clusters, with
   ~4.4mm of open B.Cu between clusters, so the fan-out that matters is
   cluster-to-cluster.
4. **Pad set sourced from the footprint** (gr451276). A pad is physical;
   it occupies space and nothing may route through it whether or not a
   net names it. Add a footprint-sourced, pad-number-keyed pad set to the
   IR, claimed on the occupancy grid and checked by DRC, SEPARATE from
   pin identity.

   **Corrected premise** (the gripe and this item's first draft both had
   it wrong): the design declares **59** pins, not 69 -- 69 was the
   FIXTURE footprint's `pin_map`. And the missing-pad gap is **SVG-only**:
   the fab SVG renders via `_drc_pads` -> `pads_for_ir` (IR-driven), but
   the gerber zip goes through `board_pads`, which emits every footprint
   pad. So the manufactured artifact is complete and the VIEWER is not.
   The serious half is neither: the ROUTER's obstacle set is
   `pads_for_ir` too, so copper can be routed through a pad that the
   gerber does contain -- a real short, not a cosmetic gap.

   Known break sites for a separate pad set: `realize.py` router pads
   (:1287), `_stamp_pads` (:1838), `pads_for_ir` (:5600), `_pad_blockers`
   (:2315), `_fixed_copper_connectivity_model` (:4846 -- a second
   same-named pad becomes a net island and mints new connectivity errors
   unless it is net-less), and the courtyard hull built from IR pins
   (`ir.py::instance_courtyard_polygon`, feeding `courtyard_overlap` and
   `_courtyard_body_mask`).
5. **Pre-route DRC gate** (gr451274a) — refuse to route a board that is
   already unmanufacturable. Cheap once (4) lands, and meaningless
   before it.

## Explicitly NOT in scope

- Promoting footprint pads to IR **pins**. Pin ids are load-bearing for
  pin swaps, net indexing, and `pin_to_net`-by-name; adding ~20 pins per
  driver surfaces in all three. Pads get their own set.
- Touching the star decomposition or the occupancy grid's per-cell
  ownership. The router depends on the star (a true MST reddened the
  ESP32-C3 acceptance board at every seed).
- The `max()` clearance collapse. Two real defects live there — the 0.15
  literal in `RealizeConfig` (`realize.py:180`, applied at `:1337`)
  outranks every computed per-net rule, and the collapse discards the
  per-net values. Separate gripe, not this round — BUT the "worth ~3
  nets" estimate came from the broken fixture and is probably low: with a
  ring footprint, 0.15 is what closes the QFP's own ~0.8mm pin gaps.
  Re-judge after item 0.
- Placement illegality as a placer cost term (gr451274b/c). On this board
  the colliding part is placed AND pinned by the generator, so the fix is
  in the generator, not the optimizer. `optimize` already has
  `courtyard_overlap`.

## Open questions / decisions log

- **Fresh board vs. amend `ewod-dogfood-2`?** Leaning fresh: the topology
  change is large enough that a diff against the old board is not
  informative, and a new vehicle can carry the controller/USB/power the
  old one lacks. Board size is free to change.
- **Controller + USB + power on-board.** Wanted. Needs part picks.
- **Edge-insertion connectors have no representation.** A side-insertion
  connector MUST sit on the board edge with a specific outward
  orientation, and nothing in the part/footprint model records that, so
  neither placement nor DRC can honour or check it. Filed separately;
  this round should not invent it ad hoc.
- **Rotation is safe for the fabric, but NOT free elsewhere.** Every
  fixed-copper row is positioned from `layout`, never from the sink's
  pose, so an unpinned sink cannot invalidate its own fabric. The traps
  are: `PinSwapGroup.offsets` are computed once from `pin_point` BEFORE
  the anneal and read raw, so a `ROTATE` leaves swap scoring on stale
  offsets; and `pcb_route` writes the anneal's pose back via
  `pcb_set_pose`, so an unpinned sink MOVES on every `op='route'`.
  Decide whether rotation is unpinned for a one-off solve or permanently.
- **Channel assignment must face the array.** `pinswap.
  propose_radial_assignment` assumes the far ends sit "inside or around
  that ring"; with the driver outside, the far side of a QFP gets chords
  through its own package. The generator likely has to bias channel
  assignment to the array-facing sides.
- **HV-vs-control steering.** `cost.py` is already net-class aware
  (`ir.net_class[net_id]` feeds coupling/thermal) but `cost.py`'s own
  note says per-class rules are not fully wired into the IR yet. Decide
  whether rotation + `crossings` alone gets the tangle acceptable before
  adding a class-separation term.

## Acceptance criteria

- The driver's courtyard does not intersect the electrode field: zero
  `via_pad_keepout` and zero courtyard findings between the array and its
  driver (gr451052 closes).
- Every footprint pad on every placed instance appears in the router's
  obstacle set, in DRC's pad set, and in the fab render. Pinned by a test
  that gives a part more footprint pads than the netlist declares.
- Escape yield is reported against the CORRECTED pad set. Expect the
  baseline to DROP first: 18 pads were being routed through illegally,
  and a lower honest number is the right outcome.
- `tests/test_pcb_reference_end_to_end.py` and
  `tests/test_pcb_fab_render_all_layers.py` green (other shards — run by
  name).

## Probe recipe (how the table above was measured; regenerate after item 0)

The probes were throwaway test files, deleted rather than shipped -- they
always pass, they are measurements not contracts, and each costs ~3
minutes. Regeneration is mechanical. Each one imports `_seed`,
`_drain_one_job` and the `pcb` fixture from
`tests/test_pcb_ewod_dogfood.py`, drives `pcb.put(id=slug, args={"op":
"route", "seed": 1})`, drains the in-process job, then reads
`store.pcb_route_status(ref.id)` and counts nets named `ARR1_R*`. The
lever is a `monkeypatch` on `precis.pcb.realize`:

- **clearance**: wrap `_resolve_track_rules` to `dataclasses.replace(r,
  clearance_mm=min(r.clearance_mm, X))` AND wrap `RealizeConfig` with a
  factory that defaults `clearance_mm=X`. Both are needed -- the config
  default is a floor, and any net with no class override falls through to
  the fab HOUSE tier (0.15), so patching either alone changes nothing.
- **layer lock**: replace `_net_class_layers` with
  `lambda ir, n, config, signal_layers: (list(signal_layers), None)`.
- **fabric self-block**: replace `_claim_fixed_copper` with a no-op.
  Island terminals are built separately, so the escapes keep the plaza
  vias they terminate on -- the arms differ in what BLOCKS, not in what
  is reachable.

Write results to a JSONL file under the worktree root, NOT `/tmp`:
`scripts/test` runs in a container and only the worktree is mounted.

## Target + blast radius

`pcb/generators.py` (`_expand_ewod_pad_array`, `_parse_sink_grid`),
`pcb/ir.py` (pad set), `pcb/session.py` (`apply_real_pin_offsets`),
`pcb/realize.py` (`pads_for_ir`, `_claim_fixed_copper`), `pcb/drc.py`,
`handlers/pcb.py` (`view='drc'`, `board_pads`).
