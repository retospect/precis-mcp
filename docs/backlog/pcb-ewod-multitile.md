---
status: draft
title: EWOD multitile boards in the pcb kind — generator footprints + electrode-aware DRC/fab
prio: normal
---

# EWOD multitile boards in the pcb kind — generator footprints + electrode-aware DRC/fab

## BUILD STATUS (2026-09-14) — resume pointer

**Slices 1 + 2 built, plus the round-1 dogfood vehicle** (rounds 1–8, all
on `main`): authored local footprints with polygon/role/mask/paste pads
through store → padplace → DRC → SVG → gerber (slice 1); the
`ewod_pad_array` generator — derived sizing with validated floors,
meshing zigzag with a proven constant-width gap channel, plaza slot
rings, chamfered/tapered escapes, merged pads, reserve ledger,
`sink_grid` bottom-side driver emission with a serial chain — plus
electrode-gap net classes, `view='capability'` (SVG + ledger), pads-only
`view='drc'` and fab metadata/notes (slice 2); and `ewod-dogfood-1` as a
test fixture (`tests/test_pcb_ewod_dogfood.py`) that applies, places,
exports a loadable gerber zip, renders, and is DRC-clean array-internally.

**Both engine gaps that blocked the acceptance criteria are closed on
`main`** (both were kind-wide, not EWOD-specific): `rules.py::PAD_LAYER`
forcing every pad onto layer 0 (gr341516, closed); and the plaza-via
escape — the IR carries ONE position per PIN so the via was invisible to
the router (fixed by island terminals from fixed copper,
`pcb-pre-place-route-blocks.md`), and once visible every F.Cu cell was
still walled off by neighbours' enclosing-DISC pad claims at a pitch
narrower than the disc (gripe 346962, true-shape claims; 2026-09-18).
The dogfood fixture now routes escapes through the fabric; the
remaining escapes lose a congestion race (gripe 347037). **Criterion
1's "routes escapes on B.Cu" is met on the fixture; gripe 339236 closes
on the PROD observation after the next deploy** (cluster predates every
fix). Full mechanism: the round-8 decisions-log entry below.

Target board (Reto, 2026-09-13): top copper is a field of EWOD drive
electrode pads (e.g. **9×9**) tiled as 3×3 multitiles — 8 driven outer
pads per tile, the **middle position empty: no pad there**. Via-in-pad
costs extra at fab, so it is avoided entirely; the vacant centre spot is a
**via plaza** where the 8 neighbours' escape stubs neck in and drop to the
bottom layer (~9 via slots), optionally covered by a sealing/spacer
sticker that doubles as the top-plate standoff. The pad pattern is thus
deliberately non-continuous (DFM), but every pad can connect. Electrode
edges are **complementary zigzag, interdigitated both within a tile and
with neighbouring tiles — and on the array's external boundary too, so
generated arrays tile against each other**. Pad size is not chosen — it
is **derived**: via size + HV separation determine the minimum pitch, and
pad size falls out as pitch − gap (~2 mm pitch expected — still tiny for
the droplet scale). The whole field is **no mask, no paste** (bare
ENIG under the parylene; mask edges are exactly the topography the lit
flags for pinning). **All 3 lower copper layers** (In1/In2/B.Cu) are
available for connecting the footprint. **Bottom layer**: signal
routing, discrete I2C temperature sensors, and a **regular grid of HV
switches/connectors directly under the array** — escapes terminate
locally at the nearest sink, so fanout stays manageable. **Inner
layers**: heater traces (their 2 legs surface through plaza slots
claimed via a **suppression map**). **Peltiers mounted below.** ENIG
finish, parylene coating over the top as the dielectric.

Reto expects the authoring surface to be a few **specialized footprint
generators**: a heater chain generator, a scalable pad array (squares
*and* rectangles — 9×9, 3×8 — plus rim-only variants like "4×4 outer rim
only"), and dispenser components with some configuration — not
hand-authored per-pad geometry. Decision (Reto, 2026-09-13): the array
generator emits the **integrated unit** (pads + stubs + via plazas + slot
ledger), not bare pads plus a placement skill — a bigger unit is more
tractable; a runtime skill documents *using* it.

## Motivation / why

The pcb kind can place, route, DRC and export a conventional SMD board,
but an EWOD board is copper-as-actuator, and today the kind cannot express
its central objects at all. Survey against code (2026-09-13):

- **Electrodes don't exist.** A partless instance is legal
  (`ir.py::PcbIR.instance_part_lcsc` = None) but gets only a synthesized
  land-pattern bound, and `gerber.py::export_fab` refuses to export any
  synthesized pad (`SynthesizedPadError`). Footprints are *pulled, never
  synthesized* (`footprint.py::ensure_footprint`, keyed by LCSC C-number);
  `store.part_footprint_put` has no MCP verb. There is no authoring path
  for "this pin is a 1.5 mm square copper electrode".
- **Pad shapes are circle/rect/obround only** (`gerber.py::_aperture_for_pad`,
  `padplace.py::_SHAPE_MAP` down-approximates POLYGON → rect). No zigzag /
  crenellated electrode edges — which the literature says are *the* lever
  for droplet-transfer reliability on PCB-class devices (below).
- Via-in-pad would be an unwaivable DRC error
  (`drc.py::check_via_pad_keepout`) — moot by design: it is also
  *expensive at fab*, so the board keeps every via in the padless plaza
  spots and the check stays untouched. What the plazas do need — polygon
  pads, stub necking into the plaza, authored clearances — the surface
  can't express.
- **Abutting electrode arrays are an error field by construction**:
  `drc.py::check_courtyard_overlap` hard-errors on courtyard contact and
  `check_clearance` exempts same-net copper only, so adjacent electrodes
  (different nets at ~100 µm gaps) redden everything.
- **The stackup is a constant.** Only `pcb/__init__.py::DEFAULT_STACKUP`
  is ever written (`_pcb_ops.py::PcbMixin._pcb_ensure_board`); no update
  path exists, inner layers are plane-role, and
  `realize.py::_signal_layers` therefore never routes them → no inner-layer
  heater traces. No surface-finish / coating / copper-weight field exists
  anywhere, so "ENIG + parylene" can't even be recorded for fab output.
- **Bottom-side parts are invisible to the engine**: `rules.py::PAD_LAYER=0`
  and `ir.py::from_graph` ignores `pcb_instances.layer`, so bottom-mounted
  sensors/Peltier drivers aren't seen by placer/router/DRC (known-inert
  note in `pcb-guided-place-route.md` BUILD STATUS).
- **No resistance/length targets, no serpentine generator** — heater
  traces and copper-RTD sensors need "hit R ohms in this region";
  `objectives.py::ObjectiveVector.low_resistance` is only a width bias,
  and `cost.py` `thermal_rise` optimises *against* self-heating.
- Mask is derived wholesale ("open over every outer pad, swelled" —
  `gerber.py::soldermask_gerber`); no per-pad mask/paste intent (that
  model is draft-only in `pcb-component-model.md` §Features).
- `ftype='keepout'` stores but nothing reads it; no sticker/spacer or
  below-board mechanical annotation (`MountingHole.head_dia_mm` is the
  only above-board envelope).
- Closest-in-spirit module `tiling.py` (per-net copper-region growth) is
  dead code with zero production callers — revive-or-delete decision
  belongs to this work.

## Literature grounding (secondary — Perplexity deep-research, 2026-09-13)

Two cached reports; numbers below are survey-derived, primaries not yet
held (same caveat regime as `ewod-oil-constraint-grounding.md` — do not
mint claims from these):

- `perplexity-research:338199` — electrode edge shape. For PCB-class
  devices (~100 µm gaps, ENIG): **zigzag/crenellated edges beat straight,
  interdigitated, crescent and notched** on speed and stability (25 µL
  droplet 6.3 mm/s @150 V → ~15 mm/s @240 V, no stall/sideslip; Frontiers
  Phys. 2020). Rules of thumb: electrode pitch slightly **smaller** than
  droplet base diameter; electrode length in motion direction < droplet
  base diameter, width ≥ it; teeth extend ~50–75 µm into a 100 µm gap at
  ~200–300 µm tooth pitch so several teeth sit under each droplet edge;
  natural PCB corner rounding is beneficial (reduces pinning) — keep it.
  Fine 10–30 µm crenellation optima (Berthier–Peponnet) are cleanroom-only.
- `perplexity-research:338200` — practical PCB-DMF engineering.
  Via-in-pad addressing with **filled/planarized vias** is the standard
  way to keep the top surface pure electrode array. Parylene-C over
  as-fabricated PCB needs ≥7 µm (≈500 V drive); over planarized/thin
  metal 1–2 µm ≈ 55–70 V AC (~1 kHz preferred over DC for charge
  trapping). Spacer-height : electrode-size ratio ~1/10–1/20 for reliable
  pinch-off (e.g. 1.5 mm pads + 100 µm gap height); smaller gap height →
  better dispense reproducibility. Dispensing: square reservoirs are the
  worst geometry; **TCC reservoir (T + two C electrodes) + circular
  cutting electrode** fixes the pinch-off point — inaccuracy 2.74 %→1.33 %
  and inconsistency 1.60 %→0.53 % as cutting radius grows 0.7→2.0 mm;
  daughter-droplet volume is most uniform when reservoir fill matches the
  reservoir-electrode volume, so a **refill well / ancillary reservoir**
  belongs in the dispenser layout. Oil fill smooths motion and blocks
  evaporation but one feedback study saw 3× worse dispense σ in oil.
  Co-fabricated PCB microheater + sensor pairs with closed-loop control
  are proven (Lab Chip 2025); thermal isolation slots limit crosstalk;
  Peltier below for sub-ambient / large swings.

Generator defaults below come from these numbers; each default must be an
overridable parameter, not a constant (the recurring-defect memory:
per-part constants are wrong, and these are *survey* numbers).

## In scope

Sliced; each slice independently shippable, later slices depend on
earlier.

### Slice 1 — authored copper footprints + polygon pads + honest export

The enabling substrate for everything else.

1. An authoring path for real pad geometry without an LCSC part: a
   `footprints` block on `put(kind='pcb')` (or reuse
   `store.part_footprint_put` behind a new arg) storing named local
   footprints; instances reference them like parts. Pads carry
   `role: solderable | electrode | probe` (default solderable).
2. `shape: polygon` pads (explicit vertex ring, mm) through the whole
   chain: `ir.py` pin geometry, `padplace.py`, courtyard hull, DRC
   clearance (polygon-aware — shapely is already a test dep), SVG render,
   and `gerber.py` (region fill / AM aperture; keep circle/rect/obround
   aperture path for the simple shapes).
3. `export_fab` exports authored footprints; `SynthesizedPadError` stays
   for the *synthesized* fallback only.
4. Mask/paste intent, minimum viable slice of the
   `pcb-component-model.md` §Features draft: per-pad
   `mask: open|covered`, `paste: none|full`, **plus a region-level
   `mask_open` feature** — an electrode field is opened as *one* region
   covering pads and gaps (a 100 µm gap can't hold a mask dam, and mask
   edges add pinning topography), not per-pad swelled openings. Wire into
   `soldermask_gerber`/`solderpaste_gerber`.

### Slice 2 — the EWOD pad-field generator + electrode-aware DRC

A `generators` block on `put(kind='pcb')`: a generator call is stored
(name + params + version) and *expanded* deterministically into
components/instances/nets/features at apply time — re-running with the
same params is idempotent; changing params replaces the expansion
(delete + re-insert its instances, same discipline as card variants).

1. `ewod_pad_array` (the "scalable pad array") — an **MCP-facing
   computed-component generator**, a capability class the kind has never
   had (footprints are pulled today, never computed). The contract: "I
   want 1024 pads" → the generator lays them out programmatically,
   exposes the whole array as **one component whose pins are the
   electrode nets** (plaza vias + stubs are internal footprint copper;
   downstream drivers connect to pins like on any part), and emits a
   **capability map** — an SVG (PDF later) of the field showing usable
   vs reserved/suppressed pads, plaza slot allocation, pin naming, and
   the computed ratings — plus the same as a machine-readable ledger.
   Scales to ~1024 pads (32×32); layout is closed-form, the annealer
   never sees the interior.

   Geometry rule: **complementary zigzag interdigitation on every
   internal edge — within a tile and across tile boundaries alike — and
   on the external boundary** (a half-profile that meshes with a
   neighbouring array, so arrays tile). Both neighbours are offset
   ±gap/2 from a shared zigzag centreline, so the gap is a
   constant-width zigzag channel everywhere.

   Everything below is a generator param; unset means *derived*:
   - `grid` (**in pads**, rows×cols — squares and rectangles: 9×9, 3×8;
     or just `pads: 1024`) and `variant: full | rim` (rim = perimeter
     pads only, hollow interior — "4×4 but just the outer rim").
   - **Sizing is derived, not chosen**: `via` (dia/drill, default fab
     min), `via_spacing`, `stub_width` (F.Cu necks), `escape_trace`
     (B.Cu width/spacing) and `hv_separation` (from `drive_voltage_v`
     via IPC-2221-style coated/uncoated rows, overridable) determine the
     minimum pitch — the plaza must hold its via slots + 8 stubs at HV
     clearance — and `pad size = pitch − gap` falls out. `pitch`
     (default 2.0 mm) is validated against that floor and errors when
     under it. Second floor: **escape-channel capacity**, a BGA-style
     fanout check — but two design facts keep it manageable (Reto,
     2026-09-13): the footprint may connect through **all 3 lower copper
     layers** (In1/In2/B.Cu, sharing area with heater regions), and the
     expected topology is **local termination, not edge fanout** —
     HV switches / connectors are placed in a regular grid on the bottom
     directly under the array, so each region's nets end at the nearest
     sink instead of accumulating toward the edge. A `sink_grid` param
     (one sink per k×k tiles) makes this explicit; the channel check
     then applies per sink cell, per layer:
     ⌊(plaza spacing − cluster width) / (escape width + HV spacing)⌋ ×
     available layers. The capability map reports per-region escape
     feasibility, and an infeasible pad is marked unusable rather than
     silently unrouted. (Full 3-layer escape depends on the slice-3
     stackup work — In1/In2 are plane-role and unroutable today; slice 2
     standalone means B.Cu-only escape.)
   - **Larger pads**: a `pad_sizes` override map — a pad may span m×n
     grid cells (merged outline, zigzag preserved along its boundary,
     one net): reservoir / dispense / common electrodes as part of the
     same field. **One via suffices regardless of size — the electrode
     is a capacitor, current is negligible.** Hard rule: a merged pad
     may **never cover a plaza cell** — the plaza carries 8 different
     nets' stubs and vias, and copper over it shorts them all; the
     generator rejects such a `pad_sizes` entry rather than relocating
     the plaza. Cells a merged pad covers drop out of the escape budget.
   - `gap` (default 0.10 mm — an *electrode* gap: the parylene/oil
     dielectric owns the insulation there, so HV separation applies to
     plaza internals and B.Cu escapes, and the electrode gap gets an
     advisory, not the HV floor), `edge` (`tooth_depth` default 0.06 mm,
     `tooth_pitch` default 0.25 mm), `corner_radius`, external-edge
     profile (`mesh | straight`, default mesh).
   - **Via plazas** (no via-in-pad anywhere — it's a fab cost adder and
     the design avoids it): at every third interior grid position (auto;
     overridable position list) the pad is **omitted**. Each vacant spot
     is a plaza: the 8 surrounding pads neck a short F.Cu stub into it
     and drop one ordinary via to B.Cu — **~9 via slots per plaza**
     (8 electrode escapes + 1 spare). Tented is fine, maybe unnecessary
     (Reto) — `tenting: on|off` param, no filled/capped vias. Plaza
     vias clear all pad copper by authored clearance, so
     `check_via_pad_keepout` passes *unchanged*. For `variant: rim` the
     hollow interior is all plaza: stubs route inward, via placement
     free.
   - **Suppression map** (`reserve`): named plaza slots withheld from
     electrode escape and handed to other consumers — e.g. the heater's
     2 legs surfacing from In1 (slice 3). The generator emits a
     free/reserved **slot ledger** other generators and the router claim
     against.
   - Mask/paste: one field-wide `mask_open` region (slice 1), paste none.
   - Optional `sticker` annotation per plaza (`dia`, `thickness` —
     thickness recorded as spacer height; derived warn when
     `thickness/pitch` is outside 1/20–1/10).
   - Emits **fixed grid placement** (`fixed='both'` — the annealer never
     moves electrodes) and one net per pad (`pad_R_C`).
2. DRC profile for electrode fields:
   - Courtyards: generator-emitted array members share a pattern id;
     `check_courtyard_overlap` exempts intra-array contact.
   - Clearance: electrode-gap adjacency checked against the *authored*
     `gap` (a dedicated net-class floor), not `trace_spacing_mm`; the fab
     capability floor still binds (gap ≥ fab min spacing → else error).
   - No via-pad waiver needed (vias live in padless plazas); keep a
     regression test that a via overlapping electrode copper still
     errors.
3. Board-level fab metadata: `surface_finish` (enig), `coating`
   (parylene + thickness note), `copper_oz` on `pcb_boards`; emitted into
   a fab-notes text file in the gerber zip (no `.gbrjob` needed yet).

### Slice 3 — stackup authoring, inner-layer heaters, bottom side

1. Authorable stackup: accept a `stackup` list on `put` (validated
   against `pcb_capabilities.json`; still 4-layer only for now — lift the
   count check only when a capability row exists). Honour
   `routable: true` on inner layers end-to-end
   (`ir.py::layer_is_routable` already reads it; the gap is purely that
   nothing can author it).
2. `heater_chain` generator: `layer` (inner), `region` (rect or path),
   `target_resistance_ohm` OR explicit `width×length`, `style:
   serpentine` (meander pitch param), computes length from copper sheet
   resistance (`copper_oz` + temp coefficient noted in output), emits the
   serpentine as routed copper (pre-routed, router treats as fixed).
   Its **2 legs surface through plaza slots reserved via the pad array's
   suppression map** (slot ledger, slice 2). Heater nets stay
   `domain='electrical'` — they are; no unlock of `thermal` domain
   needed. Add `net meta target_resistance_ohm` and a DRC/report check:
   realized trace R within tolerance of target (compute from geometry —
   the "check that cannot fire must say so" rule: if geometry missing,
   report NOT-CHECKED, not clean).
3. Temperature sensing: **discrete I2C temp sensors, bottom side of the
   PCB — they are cheap** (Reto, 2026-09-13; supersedes the copper-RTD
   idea). Ordinary LCSC parts under the tile field, shared I2C bus on
   B.Cu; not integrated into the heater element by default. This is what
   makes the board-side fix load-bearing.
4. Per-instance board side: `ir.py::from_graph` reads
   `pcb_instances.layer`; pads land on the correct outer layer (replace
   `rules.py::PAD_LAYER=0`); placer/DRC/render honour it. (This fixes the
   known-inert note for *all* boards, not just EWOD — the bottom-side I2C
   sensors and drive electronics need it.)
5. Below-board mechanical envelope for the Peltiers: a `bottom_clearance`
   feature (footprint region + height) read by placement as a bottom-side
   keepout and exported in `view='mechanical'`. Make `ftype='keepout'`
   live while in there (read it in `session.py` + placer), or delete the
   dead ftype — don't leave it stored-but-unread.

### Cross-cutting: pre-place-route blocks (meta-components) — needs its own spec round

The generators above converge on one primitive worth naming: a
**pre-place-route block** — a parametric meta-component whose internal
placement *and* routing are pre-solved in block-relative coordinates, and
which is stamped onto the board as a rigid unit the annealer may
translate/rotate (or that is pinned) but never enters. The pad array, the
heater chain, and the dispenser are all instances; a **thermal-zone
block** (heater serpentine on In1 + a bottom-side I2C sensor placed under
the zone + the plaza-slot escapes, as one unit) is the case Reto flagged
as "may also be cool" — sensor/heater co-location without hand
coordination.

What exists to build on: `ir.py::_parse_instance_groups`
(`group`/`pattern` = rigid multi-*instance* clusters,
`optimize.py::_stamp_pattern_tiles` leader-stamping) — placement-only,
no routed copper; and the heater emission above already produces
router-fixed copper. The block concept unifies the two: a stored block =
instances + pads + pre-routed copper + claimed plaza slots + exported
ports (nets that cross the block boundary).

Decision (Reto, 2026-09-13): **we'll spec out the pre-place-route
blocks** — a dedicated design round before slice 3 implementation locks
the generator output format. Open there: storage shape (block table vs
generator-tagged rows), port/net-binding semantics, whether blocks nest,
and how a block's internal DRC findings attribute to the block vs the
board.

**That round is now written: `pcb-pre-place-route-blocks.md`** (opened by
Reto's 2026-09-15 ruling — the generator emits vias *and* the traces to
the pads as real fixed copper, unit cell solved once and tiled, spacing
derived from `_plaza_capacity`). It answers the storage question
(authored `pcb_fixed_copper` feeding derived `pcb_copper`, the
`pcb_planes` precedent — `GeneratorExpansion` has no copper channel
today), absorbs gr339236, and **re-sequences the engine gaps: the
`PAD_LAYER` fix (gr341516) becomes a PREREQUISITE** of emitting real
multi-layer copper, not a follow-on. 9×9 needs no new lattice rule —
`r % 3 == 1` tiles it exactly.

### Slice 4 — dispenser generator + reservoir plumbing

`dispenser` generator ("dispenser components with some configuration"):
`style: tcc|square` (default tcc), `cutting_electrode_radius` (default
1.6 mm — the ≥1.33 %/0.53 % accuracy point), `neck_electrode_count`,
`reservoir_volume_ul` (sized so reservoir-electrode area × gap height =
fill volume — the volume-matching rule), optional `refill_well`
(well dia + an ancillary reservoir electrode under it, for top-plate
gravity refill). Emits reservoir + cutting + neck + first transport pads
as polygon electrodes with nets, plus a `well` feature (top-plate drill
annotation, mechanical export only). Approximate T/C curves with polygon
arcs within fab min-width. Where a plain rectangular reservoir suffices,
the pad array's `pad_sizes` merged pads (slice 2) already cover it — this
generator adds only the shaped TCC/cutting geometry.

## Design-review additions (adopted, Reto, 2026-09-13)

1. **Top-plate terminal + AC drive.** HV507-class chips switch DC;
   the droplet gets AC (~1 kHz) via **complement switching** — flip
   every electrode state *and* the top-plate potential each half-cycle
   (OpenDrop pattern). The board therefore provides the top-plate
   contact: **multiple recessed pogo pins, through-hole, adequately
   low height** — part to be found (open sourcing item). The sink
   block drives the top-plate rail; the capability map and pin budget
   include it.
2. **Parylene process order** in fab-notes, explicit sequence:
   assemble bottom → mask bottom (fixturing/tape) → parylene deposit
   the top. Parylene is conformal over everything; wrong order coats
   the solderable pads.
3. **Capacitive droplet sensing provisioned**: DropBot-style impedance
   sensing through the drive lines — a sense path in the sink block +
   one ADC line, reserved now; usage documented in the runtime skill
   (the lit says feedback pays exactly at dispense-in-oil).
4. **Thermal path**: board `thickness` becomes a generator/fab param
   (thinner helps the Peltier path through FR-4); thermal-via
   allocation under Peltier zones goes through the suppression map and
   therefore into the escape-capacity math.
5. **Top copper weight**: 0.5 oz / thin-foil outer layer as a fab
   param next to `surface_finish` — halves the edge topography the
   parylene must bridge (1 oz = 35 µm steps; PCB topography is the
   lit's voltage-cost story).
6. **HV rail**: external boost for round 1 — a connector, a bleed
   resistor on the rail, creepage attention at the connector.
7. **Top-plate registration**: loose tolerance is fine (the top plate
   is unpatterned ITO; all patterning is bottom-side) — optional
   simple alignment features only.
8. **Fiducials must not land inside the electrode field** — likely
   already emergent from the field's copper/courtyards, but assert it
   in a test rather than assume (dormant-check rule).

## Multi-card assemblies (design assumption, not a slice)

There is **no standard for tiling EWOD cards into one droplet plane**,
and the field deliberately avoids it — scaling is done on one bigger
substrate (larger PCB; active-matrix TFT beyond that). Survey:
`perplexity-reasoning:338358` (secondary — same caveat regime as the
other reports). What is semi-standardized is everything *except* the
seam: chip-to-instrument connectors (pogo-pin arrays, fixed-pitch pad
arrays, FPC/ZIF for active-matrix). Droplet transfer between modules
exists in research but is **vertical** (orifice-through-electrode
core–shell handoff ~97 % recovery; overlapping plates; open↔closed
transitions at ~250 V with shaped geometry) — engineered 3D interfaces,
never abutted flat boards.

Why a lateral seam fails: to a droplet it must look like a normal
inter-electrode gap — lateral discontinuity well under 100 µm,
coplanarity within a few µm (bare PCBs vary >10 µm), and *continuous
dielectric + hydrophobic coating*. The last is the killer here: parylene
is deposited per card, so an abutted seam always carries a coating break
that pins the contact line, even with perfectly meshing external zigzag
edges.

Design assumptions recorded (Reto, 2026-09-13):

- **Separate fluid domains per card.** No droplet walks across a card
  seam. Inter-card liquid transfer, when needed, goes through a shared
  well/reservoir at the boundary (world-to-chip style) or a vertical
  interface — a future item, not this spec.
- **The serial chain crosses cards freely** (HV507-class DOUT→DIN
  through a connector), so a multi-card assembly runs under one
  controller; the capability map of each card stays per-card.
- The meshing external zigzag stays: it costs nothing and lets two
  cards share a frame/top plate with usable near-seam electrodes.
- The pressure to tile is low: 32×32 pads at 2 mm pitch is a 64 mm
  square — single-substrate scaling covers very large arrays before a
  seam is ever forced.

## Dogfood vehicle (round 1 — build target after slices 1–2)

Decision (Reto, 2026-09-13): build now and dogfood. Board
`ewod-dogfood-1`, needing **no slice-3 work**:

- 8×8 pad field @2 mm pitch (64 electrodes ≡ one driver's channel
  count; plazas per auto rule), one merged reservoir pad, zigzag edges
  incl. external, field-wide mask opening.
- Driver: **HV507** (64-ch push-pull, 300 V class, the OpenDrop-proven
  EWOD part, hand-solderable QFP) on the bottom, serial bus out to a
  connector. HV583 (the inkjet chip: 128-ch push-pull) is the
  higher-density candidate but is **80 V max** and 169-ball TFBGA —
  viable only with a thin high-quality dielectric and BGA escape;
  discrete FETs + pullups only as fallback. Confirm LCSC stock
  (footprint puller needs the C-number; HV583GA-G is C633832).
- One bottom-side I2C temp sensor, B.Cu-only escape, no heater, no
  Peltier. Top-plate pogo terminal (design-review item 1), HV-in
  connector + bleed resistor, plain pin header to the instrument.
  0.5 oz top copper; fab-notes carry the parylene process order.
  Gerber zip out; visual fab-SVG review; order placed manually
  (JLC ordering slice stays unbuilt).

## Explicitly NOT in scope

- **Droplet routing / actuation sequencing** (which electrode fires when)
  — that is firmware/runtime, not layout. The kind ships copper and
  metadata only.
- Electrowetting physics simulation, droplet-volume solvers, thermal FEM.
- Cleanroom-class geometry (10–30 µm crenellation optimisation) — fab
  capability floors stay authoritative.
- Panelization / step-and-repeat of whole boards (still absent
  kind-wide; the tile array is *within* one board).
- Top plate (ITO glass / grid ground) as a designed object — record it as
  a fab-note string only.
- Sticker/gasket **cut-file export** (the laminar-laser / Cricut output
  tool) — deliberate follow-on item (Reto: "we can export sticker type
  cuts later"); the sticker feature only records geometry for now.
- Filled/capped/plugged vias — plaza vias are plain tented (or bare).
- Copper-trace RTDs — superseded by discrete bottom-side I2C sensors.
- Ordering (JLC slice 9), blind/buried vias, `.gbrjob` generation.
- Minting claim hubs from the two Perplexity reports (see
  `ewod-oil-constraint-grounding.md` for why survey-derived numbers don't
  ground claims; acquiring the EWOD primaries is that item's work).

## Acceptance criteria

1. A single `put(kind='pcb')` authoring one `ewod_pad_array` (9×9 full =
   72 electrodes + 9 via plazas, heater slots reserved via the
   suppression map), one 4×4 `rim` array, one `heater_chain` (In1, 25 Ω
   target, legs up through reserved slots), bottom-side I2C sensors, and
   one `dispenser` (tcc, refill well) applies cleanly, places with
   electrodes fixed on grid, routes escapes on B.Cu, and
   `get(view='drc')` is **error-free with the checks RUN** (drc view
   executed, findings row exists — "no rows" is not clean). The array is
   addressable as one component: its electrode nets appear as pins, and
   the capability map (SVG + ledger) renders usable vs reserved pads and
   pin names. A `pads: 1024` (32×32) invocation generates in seconds —
   layout is closed-form.
2. `view='svg' level='fab'` shows the complementary zigzag edges (tile
   boundaries indistinguishable from intra-tile edges) and the
   serpentine; visual review is part of DoD (defects in this kind are
   found by looking, not by reading code). Geometric assertion: the gap
   channel measures constant width == `gap` along the zigzag, including
   across tile boundaries.
3. `view='gerber'` exports the polygon electrode pads (region fill), the
   plaza vias + stubs, **one field-wide mask opening** (pads and gaps,
   no per-pad dams), **no paste** on any `role: electrode` pad, and a
   fab-notes file naming ENIG + parylene + sticker/spacer thickness. Zip
   loads in a gerber viewer without aperture errors.
4. Plaza vias produce zero findings under the **unchanged**
   `check_via_pad_keepout`; a via moved to overlap electrode copper still
   errors (test the failure direction).
5. Electrode gap narrower than fab min spacing → DRC error naming the
   capability floor; gap ≥ floor → clean.
6. Heater realized resistance within ±10 % of `target_resistance_ohm`,
   asserted from exported geometry; when geometry can't be computed the
   check reports itself as not-run.
7. Re-applying the same generator params is a no-op (idempotent); changed
   params replace the expansion without orphaning instances/nets.
8. A bottom-side instance's pads land on B.Cu in IR, router and gerber
   (fixture with a genuinely asymmetric footprint so a side-flip error is
   visible — trivial-symmetry-group rule).
9. Existing reference fixtures (`esp32c3_reference`,
   `motor_power_reference`) stay green; if a placement-affecting change
   reddens a pinned DRC count, sweep seeds before touching constants.

## Target + blast radius

- Handlers: `handlers/pcb.py` (put schema: footprints/generators/stackup
  blocks; DRC profile plumbing).
- Store: `_pcb_ops.py` (footprint/generator/feature persistence),
  migration for `pcb_boards` fab-metadata columns + generator table.
- Engine: `ir.py`, `padplace.py`, `rules.py` (PAD_LAYER removal),
  `realize.py`, `drc.py` (courtyard exemption, gap-class clearance),
  `gerber.py` (polygon apertures, mask/paste intent, fab notes),
  `silk.py` (no refdes text on electrode arrays), `optimize.py` (fixed
  grids, pattern exemptions). `tiling.py`: revive as the electrode-region
  helper or delete in slice 1 — decide, don't strand.
- Docs/skills: `precis-pcb-help.md` + a new `precis-pcb-ewod-help` skill
  documenting the generators (runtime agents author these boards too).
- Related drafts subsumed or advanced: `pcb-component-model.md`
  (§Features mask/paste — slice 1 implements the minimum),
  `pcb-feature-model-vs-layer-films.md`, `pcb-fab-output-unwired.md`
  (stale on gaps 1–2 — fold/refresh while in there).

## Open questions / decisions log

**Rulings 2026-09-18 (Reto, walked one by one) — five open items closed;
each is now a build item, not a question:**

1. **Stub-vs-electrode clearance → (b) rule-derived corridor.** Chamfer
   only the plaza-adjacent corners back until the escape corridor is
   `trace_width + 2 × trace_spacing` from the fab capability row
   (0.27 mm at JLC 4-layer), and re-solve the zigzag wall against that
   chamfer. `gap` stays 0.10 mm everywhere else; plaza-adjacent
   electrodes lose a small corner triangle (ledger-visible). Rejected:
   raising `gap` to 0.27 (changes the droplet physics) and accepting the
   finding / tighter fab house. The earlier "widen the margin" regression
   was a constant bump without re-solving the wall — this is the re-solve.
   BUILT 2026-09-19 in tree.
2. **9×9 sink packing → balanced by chain order.** `sink_grid.per_tiles`
   (square cell blocks) is replaced by `channels_per_sink` (default = the
   part's 64); sink count = ceil(driven / channels_per_sink), electrodes
   assigned in serpentine chain order in equal shares (72 → 36 + 36), each
   sink placed under its own share. Same rule at any size. Rejected:
   greedy 64 + 8 (one chip at 12 %, all escapes converge), trimming to 64
   driven (not the 9×9). BUILT 2026-09-19 in tree.
3. **HV separation → per-copper-class IPC-2221B Table 6-1 rows, valid
   for ANY actuation pattern.** Electrode gaps (top, under parylene +
   oil) stay advisory — the dielectric stack owns them. Plaza internals
   and B.Cu escapes / sink fan (under mask, coated) take the external-
   coated row (B4); inner-layer heaters (slice 3) take the internal row
   (B1). Derived from `drive_voltage_v` + layer + coated, replacing the
   0.002 mm/V placeholder slope. The table values quoted in the session
   (B4 ≈ 0.4 mm, B1 ≈ 0.25 mm at 101–300 V) are FROM MEMORY — verify
   against the table before they land in `pcb_capabilities.json`.
   Explicitly rejected: relying on sequential-neighbour switching (a
   stuck droplet, a test pattern or a firmware bug puts any two
   electrodes at full differential; a board rule must not depend on
   software behaviour).
4. **Top-plate terminal → no pogo pin at all.** The top plate is hinged
   along one board edge with conductive copper tape (3M 1181 class): the
   tape bridges the plate's ITO to a bare, mask-open ENIG landing strip
   on the board edge AND is the hinge, so the plate folds open for
   loading/cleaning. Generator: the THT "pogo" ring becomes a
   `role: tape_land` strip (mask open, no paste, no drill) with a length
   param (tape contact resistance scales with overlap). Rejected:
   overhang + pogo, depth-milled pocket, spring clip.
5. **U_TEMP → TI TMP117** (WSON-6 2×2 mm with exposed pad, ±0.1 °C,
   4 addresses, ALERT). Board temperature under the array, not droplet
   temperature — a heater-loop sensor. Intake: `op='footprint'` on the
   chosen C-number, verify `view='footprints'` shows no synthesized pin;
   the exposed pad needs paste/stencil care on B.Cu under the array.

**Rulings 2026-09-19 (Reto, from reviewing prod `ewod-dogfood-2` in the
web view) — four more, each a build item:**

6. **Sink channel pins are freely swappable → wire pin swap through
   `op='route'`.** Any electrode may drive any HV507 channel (the
   chain order is firmware's problem), so the generator emits ONE
   :class:`precis.pcb.pinswap.PinSwapGroup`-shaped admissible set per
   sink (all `channel_pins`, no exclusions) as authored data, and the
   route job resolves it — with real per-pin offsets from the cached
   footprint (`pinswap.group_from_pads`) — into
   `OptimizeConfig.pin_swap_groups`. Today that field is never populated
   from the tool surface (`precis-pcb-route-help.md` "PIN_SWAP needs
   footprint pad-offset + admissible-pin data this tool surface does not
   supply"); this closes that documented gap. Expected effect: the
   gr347037 congestion race disappears — each plaza via takes the nearest
   free ring pad, and the swap decisions persist through the existing
   `pcb_pin_swaps_replace_derived` path (already written back per run).
   BUILT 2026-09-19 in tree — the generator emits `pin_swap_groups` on
   every sink instance (all channel pins it actually wired; an unwired
   spare pin has no IR pin id to swap and is never listed), carried on
   `pcb_components.meta` (the column already existed, unused — no
   migration needed) and accepted the same way on a hand-authored
   component. `pcb_route._resolve_pin_swap_groups` resolves it into real
   `PinSwapGroup`s each run, dropping a member whose rotation-CSR degree
   doesn't match its group (a job-summary warning, never a failure). The
   job summary now names how many swaps settled.
7. **Fabric escapes are single-layer B.Cu, no crossovers.** The escape
   is plaza via → B.Cu track → sink pad, nothing else: a net class may
   name its allowed `layers` (`["B.Cu"]` for the `{name}_*` escape nets,
   emitted by the generator next to the existing electrode-gap class),
   and `_realize_maze` passes that per-net list to `maze.route(layers=)`
   instead of the global `signal_layers`. With ruling 6 the assignment
   exists that makes the B.Cu fan planar; a net that still cannot route
   single-layer fails visibly (`no_path`) rather than sprouting a via.
   BUILT 2026-09-19 in tree — a pin whose plaza via/stub gives it real
   B.Cu copper is tagged a NEW `{class}_escape` class (same clearance
   floor as the electrode-gap class, plus `layers: ["B.Cu"]`); a pin with
   no via stays on the plain class. `_realize_maze` resolves each net's
   allowed layers once (`_net_class_layers`) and, per segment, substitutes
   a real fixed-copper island terminal on the allowed layer for an
   endpoint whose own native pad isn't on it (the plaza via's B.Cu
   landing, for the electrode side) — never a fabricated point on bare
   board; an end with no legal landing at all fails the segment. A net
   locked to a layer absent from the stackup (or with no routable
   intersection) fails whole with `UnroutedReason.kind == "layer_lock"`,
   naming the class and the layer. Single-layer confinement is strictly
   MORE restrictive than the old multi-layer routing, so the dogfood
   fixture's realized-escape floor was recalibrated down (still ~half of
   what seed=1 currently realizes, same margin convention as before).
8. **Plaza slots: maximise clearance to foreign electrode copper, not
   ring uniformity.** Round 2 chose a uniform 8-slot ring because a
   uniform 3×3 square grid was unsatisfiable at the spec's numbers
   (`_plaza_capacity` docstring). Reto's point stands for the cardinal
   slots though: for a via-via floor `d`, a ring needs radius 1.307 d
   while an axis-aligned layout puts the cardinals at `d` from centre —
   0.3 d more edge margin. Build: replace the one-radius ring with the
   two-parameter axis-aligned family (cardinals at ±a on the axes,
   diagonals at (±b, ±b)) and pick `(a, b)` by maximising the minimum
   clearance from any slot to any FOREIGN electrode's copper, subject to
   every slot pair ≥ `via_dia + hv_separation` and the same
   foreign-corner constraint round 2 already solves. Keep
   `_plaza_capacity`'s `min_half`/`min_pitch` contract (validated the
   same way); the ledger keeps the 8 slot names. The centre spare slot
   stays. Composes with ruling 1 (the corridor is the diagonal slot's
   stub path). BUILT 2026-09-19 in tree — the actual optimum the search
   finds moves the cardinals to a LARGER radius than the old ring
   (`slot_a` ≈0.738mm vs. the ring's ≈0.707mm at default via/hv numbers,
   min_pitch ≈1.377mm vs. ≈1.435mm — a net improvement, not the
   originally-expected "cardinals move in": the closed-form foreign-
   clearance curve is U-shaped in the cardinal's own distance from centre
   (minimum exactly at `half`), so the true maximiser sits on the FAR
   side of that minimum once the via-via pairwise floor is folded in —
   see `precis.pcb.generators._family_foreign_clearance`'s own docstring
   for the derivation); still strictly beats the old ring's own minimum
   foreign-copper clearance at every `half`, which is the acceptance bar
   this ruling actually set.
9. **Add a Teensy 4.0 as the controller** (`U_MCU`, top side, outside
   the array, near the serial-in end of the chain). Authored as a LOCAL
   footprint (PJRC's own drawing is the source: 2 × 14 through-hole pins
   at 2.54 mm, rows 15.24 mm apart, 17.78 × 35.56 mm body — VERIFY the
   pin count/positions against pjrc.com/teensy/card10a_rev2.pdf before
   the footprint lands; the bottom SMD pads are NOT modelled). Wiring:
   sink chain `DIN`/`CLK`/`LE`/`BL`/`POL` (whatever `sink_grid` names —
   today only `serial_in_pin`/`serial_out_pin` are parametrised; add the
   clock/latch/blank/polarity pins the HV507 actually has) from Teensy
   GPIOs; I2C `SDA`/`SCL` (pins 18/19) to U_TEMP; `GND`; Teensy `VIN`
   from the board's logic rail. **Level shifting is part of this item —
   settled from the datasheet (Microchip DS20005845A): HV507 VDD is
   4.5–5.5 V and `V_IH` = VDD − 0.9 V (≥ 4.1 V at 5 V), so the Teensy's
   3.3 V outputs cannot drive it directly.** One 74HCT245-class octal
   buffer (HCT inputs accept 3.3 V, outputs at 5 V; `U_LVL`, top side,
   between Teensy and sink chain) shifts DIN/CLK/LE/BL/POL; the HV507's
   DOUT back to the Teensy is 5 V into a 3.3 V-only pin — take it through
   the same buffer powered from 3.3 V or drop it (the chain end is only
   needed for read-back). `ewod-dogfood-2` acceptance moves to
   "fully placed + fully routed (62/62 + the Teensy nets) on prod".

**Rulings 6+7 measured on the faithful prod rebuild (2026-09-19, main
loop; harness in memory `ewod-pcb-campaign-state.md`):** three more
router defects surfaced and are fixed in the same tree — the swap
evaluator matched footprint pads by raw `number` (a real HV507 names
pins through `pin_map`, so every pin collapsed to the centroid and every
swap scored 0 → `pinswap.offsets_from_ir` reads the IR's own mirrored,
rotated pin points); every airwire's far end was its INSTANCE centroid
(the whole array is one instance → all 54 airwires met at one point →
`_instance_edges` now uses the far pin's real position) plus a closed-
form cyclic-angular warm start (`propose_radial_assignment`); fixed
plaza vias were claimed as one-shot core discs wider than the slot pitch
so the last stamped overwrote its neighbours' rims (`no_path` "walled
in" even routed alone → cores, then true discs, then centre cells); and
`PcbHandler._build_ir` never applied persisted pin swaps (DRC/gerber
disagreed with the routed copper the moment a swap actually settled).
Result: 53 swaps settled; unlocked 38/62 realized (was 32); **locked to
B.Cu 27–30/57 fabric nets**. The remainder is geometry, not the router:
`ewod-dogfood-2` declares no `drive_voltage_v`, so `hv_separation`
fell to the 0.09 mm fab floor and the plaza packs its 8 vias 0.54 mm
apart, while the maze needs ~0.43 mm between a track centreline and a
foreign via centre (0.15 clearance + 0.2 search dilation); the three
same-side vias of a plaza pinch each other on the way to the west ring
pads (watched cell by cell; capping clearance at the class's 0.099 did
not change the count).

**Two levers, Reto's call:** (1) declare the drive voltage / land
ruling 3 — IPC-2221B B4 is 0.2 mm ≤250 V (plaza min pitch 1.68 mm,
fits the 2.0 mm array) and 0.4 mm at 251–300 V (min pitch 2.23 mm, the
array pitch must grow); (2) **radial B.Cu breakout stubs as fixed
copper** (gr347037's "pre-solved breakout"): the generator emits, per
plaza via, a ~1 mm B.Cu stub outward along its slot direction so the 8
exits sit on a ~1.7 mm circle 1.3 mm apart and the router starts where
there is room — recommended regardless of (1). Also seen: the sink's
QFP pads at 0.8 mm pitch leave zero free rows between their clearance
zones on B.Cu, so a pad is enterable only from its ends — fine for a
radial fan, but the Teensy/shifter/I2C nets must not cross the ring.

Also from the same review: the web view shows F.Cu tracks ending over
bottom-side SMD pads (sink / U_TEMP) with no connection. That is the
layer-blind-router signature gr346744 already fixed on `main` — prod is
still at 8e0099fc, which predates every route fix; the render is expected
to change only after the next deploy + `op='route'`. If it persists after
that, it is a new defect.

Decided 2026-09-13 (Reto): no via-in-pad anywhere (fab cost) → padless
via plazas; plaza vias plain tented, maybe bare — no fill; sticker
cut-file export deferred to a future laminar-laser/Cricut output tool;
temperature sensing = discrete bottom-side I2C parts, not copper RTDs,
not heater-integrated by default; generator ships the integrated unit
(pads+stubs+plazas+ledger), not bare pads + a placement skill;
pre-place-route blocks get their own spec round; the array is exposed as
**one computed component with pins** (the "MCP-like component
generator" framing) and emits a capability map; sizing params (via,
hv_separation, pitch, gap, edge) are all generator params, derived when
unset; pitch default 2.0 mm; external edges zigzag so arrays tile;
escape uses all 3 lower layers with a regular bottom-side
switch/connector sink grid (local termination, not edge fanout);
bottom routing is distributed, with the **serial bus + switching on the
bottom** (serial-chained HV switch ICs — only power + the serial chain
leaves the field, which is what makes 1024 pads routable); one via per
electrode regardless of pad size (capacitive load, negligible current);
merged pads never cover a plaza (would short the 8 escape nets).

Resolved 2026-09-14 (round 2, `precis.pcb.generators`):

- **A sharp (right-angle) crenellated edge CANNOT hold a constant-width
  gap channel at all**, at any tooth depth: a 90-degree corner has zero
  radius of curvature, and offsetting the shared centreline by `gap/2`
  on the concave side of a zero-radius corner is geometrically
  undefined — shapely's `offset_curve` trims the resulting
  self-intersection regardless of join style (mitre/bevel/round all
  reproduce it), which collapses that side onto the corner and zeroes
  the gap to the convex side's own offset right there. Caught by
  `test_zigzag_gap_between_row_neighbours_is_constant` (two "adjacent"
  electrodes measuring 0.0mm apart, not the constant `gap` the
  acceptance criterion requires) — this is the single most important
  finding of this round: the spec's own "shared centreline, offset
  +-gap/2, constant-width channel everywhere" model (decided above)
  is only realisable with ROUNDED transition corners. Fixed by rounding
  the centreline's own transitions into a two-arc reverse curve
  (`_s_curve`, radius = `tooth_depth`) before offsetting (`_meshing_wall`,
  round join) — geometrically exact (not merely close) once
  `tooth_depth >= gap/2`, now a validated floor (`resolve_ewod_sizing`
  errors below it, naming the reason). `tooth_pitch > 2*tooth_depth` is
  validated too (room for a transition on top of a flat plateau).
- **Plaza slot capacity IS now computed, not assumed** — but as a
  RING, not a 3×3 sub-grid: all 8 escapes sit on one circle around the
  plaza centre at 45°-spacing (`_plaza_capacity`). A 3×3 square sub-grid
  (the originally-assumed shape) turned out unsatisfiable at this spec's
  own cited numbers (0.45mm via + a plausible HV separation) — the
  diagonal slots and the cardinal slots need genuinely different spacing
  from the plaza centre, which a single square-grid pitch cannot give
  both at once; a ring decouples them. `min_pitch` is derived from two
  closed-form constraints (adjacent-slot clearance on the ring; diagonal-
  slot-to-neighbouring-electrode-corner clearance) and `put` errors below
  it, naming the floor.
- **Diagonal escape stubs must be tapered, not uniform-width** — a
  diagonal neighbour's own corner sits only `gap*sqrt(2)` away from a
  plaza's diagonal via; a uniform-width neck clips it regardless of the
  ring fix above. `_stub_polygon` now tapers from a point at the
  electrode's own anchor to full width at the via, and the via-end width
  is additionally capped at `gap` (a wider neck still clips at default
  sizing even tapered).
- **Default `hv_separation`** (round 2 default, still not "the" answer
  to the item below): 0.3mm when a `drive_voltage_v` is declared
  (scaling 0.002mm/V above that), else the fab's own `jlc_min
  trace_spacing_mm` (~0.09mm) when no voltage is declared at all — an
  invented "HV" number with no declared voltage to derive it from was
  making the plaza infeasible at the spec's own default pitch (2.0mm).
- **Plaza auto-placement for grids not divisible into 3×3** — resolved
  as a documented mechanical default: `r % 3 == 1 and c % 3 == 1` for
  ANY grid size (not just multiples of 3); a truncated last block simply
  leaves some boundary electrodes with no plaza neighbour, marked
  `unusable` in the ledger rather than misplaced.
- **Rim-variant via placement** — resolved as a documented mechanical
  default, not the "closed-form global routing" a from-scratch design
  might give: a non-corner rim pad's via sits a short reach inward along
  its one open cardinal axis; a CORNER pad (whose only hollow neighbour
  is diagonal) reuses the SAME ring radius the plaza's own diagonal
  slots use, for the same clipping-avoidance reason.
- **Generator representation**: decided as "expansion stored as ordinary
  rows tagged with generator id+version" — `pcb_generators` (identity +
  canonical params + ledger) plus ordinary `pcb_components`/
  `pcb_instances`/`pcb_nets`/etc. rows the generator's own expansion
  feeds through `_pcb_apply`'s normal insert path (`precis.store.
  _pcb_ops.PcbMixin._pcb_apply`'s `generators` block). Lazy-expansion-at-
  IR-build was the other option on the table; not taken, since it would
  have meant every downstream reader (DRC/gerber/SVG/router) needing to
  know a component was generated, exactly the coupling the ordinary-rows
  approach avoids.

Resolved 2026-09-14 (round 3, DRC/net-class wiring):

- **The plaza via stays a drilled THT footprint pad — DECIDED, not a
  persistent `model["copper"]` via row.** Round 2's own docstring worried
  `check_via_pad_keepout` was blind to a plaza via for this reason; that
  worry had the risk backwards. That check protects PADS from a
  router-placed via, and reads `model["pads"]` generically (any
  shape/role) — a polygon electrode is protected on the exact same terms
  as any rect/obround pad, unchanged, no waiver (acceptance criterion 4,
  regression-tested in `test_pcb_ewod_generator_drc.py`). The REAL gap
  was `check_annular_ring`, which iterated `model["copper"]` vias
  exclusively — a plaza via's own drilled hole (a THT pad by design) had
  its ring computed but never validated against the fab floor. Fixed:
  `check_annular_ring` (`drc.py`) now also ring-checks every drilled
  footprint pad, deduplicated across the one-flash-per-copper-layer
  repetition `padplace.py` emits for a THT pad.
- **Fixed a real manufacturability bug this surfaced**: `resolve_ewod_
  sizing`'s `via_drill` default was a bare 0.2mm literal, decoupled from
  the jlc_min-derived `via_dia` default (0.45mm at 4-layer) — pairing the
  two gave a ring of 0.125mm, UNDER the fab's own 0.15mm floor, on every
  default-sized plaza via, on every EWOD board, silently, until a check
  existed that could see it. Fixed by deriving `via_drill` from the SAME
  `drill_mm` capability figure that produced `via_dia`, landing the ring
  exactly at the floor (still a WARN against `house_default` — a
  documented, deliberate consequence of choosing the jlc_min tier for via
  sizing at all, same reasoning already given for `via_dia`/`stub_width`).
- **Electrode-gap net-class clearance floor**: every electrode net gets
  `net_class = f"ewod_{name}"` with a dedicated `pcb_net_classes` rule
  (`clearance_mm = gap`, minus a 1um safety margin for the padplace
  pipeline's own 4-decimal-place coordinate rounding —
  `_GEOMETRY_ROUNDING_SLACK_MM`), upserted by `_pcb_apply` in the same
  transaction as the rest of the expansion. Without it, every ordinary
  electrode-to-electrode adjacency pair would carry a spurious WARN
  against the fab's flat `trace_spacing_mm` house_default tier (0.15mm at
  4-layer, above the 0.10mm default `gap`) on every board; the override
  only ever changes that WARN threshold, never the ERROR floor (`jlc_min`,
  which still binds a `gap` set below what the fab can make).
- **Handler-level `view='drc'` gate was OUT of this slice's remit —
  RESOLVED round 4, see below.** (Round 3's own note: bails "no realized
  copper yet" whenever `pcb_copper_list` is empty, true for ANY
  standalone `ewod_pad_array` board since every net is fanout-1; a
  handler-level UX contract change reaching every board in the kind, not
  an EWOD-specific fix. Considered and reverted round 3 because it broke
  the then-pinned `test_drc_view_before_any_route_run` — that turned out
  to be a false alarm, see round 4's entry.)

Resolved 2026-09-14 (round 4, diagonal-stub geometry + `view='drc'` gate):

- **Fixed the round-3 diagonal-stub clearance gap — root cause was NOT
  the stub taper.** Round 3's working theory (a foreign tooth protruding
  `tooth_depth` past the flat-corner model) was wrong: the true root
  cause is pure trigonometry, independent of stub width entirely. A
  corner electrode's diagonal escape runs exactly along the 45-degree
  line joining its own flat corner to the two FLANKING (cardinal-
  escaping) neighbours' own flat corners — and those flanking corners
  sit at perpendicular distance exactly `gap/sqrt(2)` from that line (a
  fixed function of `gap` alone, independent of pitch/via/hv_separation).
  At the spec's own default `gap` (0.10mm), `gap/sqrt(2)` (~0.0707mm) is
  BELOW the 4-layer `jlc_min trace_spacing_mm` floor (0.09mm) — a
  hypothetically ZERO-WIDTH path through that exact pinch point is
  already too close; no taper redesign can rescue it. Fixed by chamfering
  (`precis.pcb.generators._electrode_polygon`'s `plaza_corner_chamfer`,
  derived from `gap` alone in `resolve_ewod_sizing`) exactly the two
  flanking corners — both walls meeting there retreat INWARD along their
  own axis, which can only ever gain clearance, never newly violate
  anything else — back out to the array's own uniform `gap` design
  target. `test_diagonal_escape_stub_can_undercut_the_fab_clearance_floor`
  is now `test_diagonal_escape_stub_clears_the_fab_floor_against_neighbour_bodies`
  (positive assertion, parametrized across grid sizes),
  `test_pcb_ewod_generator_drc.py`.
- **A second, independent zero-margin finding surfaced once the above
  stopped dominating**: the plaza ring's adjacent-slot chord
  (`_plaza_capacity` constraint 1) sits EXACTLY at `via_dia +
  hv_separation` — zero margin — whenever `hv_separation` falls back to
  the fab's own `jlc_min` floor (no `drive_voltage_v` declared), the SAME
  floor `check_clearance`'s ERROR tier checks against. Placed-pad
  coordinate rounding (4 decimal places) can shave a few 0.00001mm off
  the exact analytic chord, enough at zero margin to flip a
  genuinely-manufacturable ring into a spurious ERROR. Fixed by padding
  the chord requirement with the same `_GEOMETRY_ROUNDING_SLACK_MM` the
  electrode-gap net class already uses.
- **Verified the round-3 "same root cause" suspicion about the rim
  variant's corner-pad via reach — real, but a DIFFERENT bug than
  suspected, and it took two fixes.** (1) `_rim_via_point`'s diagonal
  branch measured `slot_radius` from the CORNER PAD'S OWN centre rather
  than from its virtual plaza's centre (a full `pitch` away, matching how
  a real plaza's ring is measured) — `slot_radius` being comfortably
  smaller than `pitch`, this placed the via only partway to the corner,
  still WELL INSIDE the pad's own polygon: a genuine via-in-pad defect,
  silently, on every rim corner, on every board, since round 2. (2) Once
  fixed to reach the correct virtual-plaza position, a rim corner's
  diagonal via and its two flanking cardinal neighbours' own (independent,
  uncoordinated) vias turned out to sit too close to EACH OTHER — a rim's
  4 corners are a genuine 3-consumer plaza in every way that matters (the
  corner's virtual plaza and both flanking cardinal pads' virtual plazas
  are literally the SAME hollow cell), but `_rim_via_point`'s ad hoc
  per-pad reach formulas never proved mutual clearance the way a real
  plaza's single shared ring does. Fixed by routing EVERY rim pad's via
  through the identical `_plaza_slot_point` ring construction a real
  plaza's own consumers use — a corner's 3-consumer cell is just an
  under-subscribed real plaza, not a special case. New regression
  coverage: `test_rim_corner_via_lands_outside_its_own_electrode_body`,
  `test_rim_corner_via_clears_its_flanking_cardinal_neighbours_vias`,
  `test_pcb_ewod_generator_drc.py`.
- **`view='drc'` gate widened — a repo-wide contract change, not
  EWOD-specific.** New rule (`PcbHandler._render_drc`,
  `src/precis/handlers/pcb.py`): run geometric DRC whenever ANY placed
  pad is REAL (authored-local or cached, `synthesized=False`), even with
  zero realized copper — the response states the reduced scope
  explicitly (`(pads-only DRC — no routed copper yet)`) so a clean
  pads-only pass is never mistaken for a full one. The old "no realized
  copper yet" bail stays ONLY when every placed pad is a synthesized
  BOUND (or there are no pads at all) — DRC over a dimensionally-
  plausible guess is meaningless, same reasoning `gerber.export_fab`'s
  `SynthesizedPadError` already applies at export time. **Affects every
  board in the kind**: an ordinary placed-but-unrouted board with cached
  real footprints (e.g. `esp32c3_reference`, `motor_power_reference`, if
  either is ever queried for `view='drc'` before `op='route'` — neither
  currently is, so their pinned end-to-end DRC counts are unaffected)
  now gets pads-only findings instead of "not yet". Round 3's own
  concern that this would break `test_drc_view_before_any_route_run`
  was a false alarm: that fixture's parts have no `part_footprints` row
  cached in the test store, so every pad there is STILL synthesized and
  the old bail still applies unchanged — the test is renamed
  (`test_drc_view_before_any_route_run_and_with_no_cached_footprint_bails`)
  to say so, and a new sibling test
  (`test_drc_view_runs_pads_only_before_any_route_when_pads_are_real`,
  using an authored local footprint — always real, no cache needed)
  pins the NEW contract. `test_pcb_ewod_generator.py`'s own handler-path
  smoke test (`test_drc_view_runs_pads_only_on_a_standalone_generated_array`)
  confirms a standalone EWOD array now gets a real pads-only pass
  (currently non-empty: every net reads `unrouted` because `op='route'`
  was never invoked at all in that test — the router-bookkeeping default
  for "no `pcb_routes` row", not a defect this decision is scoped to
  fix; running `op='route'` first, per the normal authoring flow, is what
  marks even a fanout-1 net `'realized'`).

Resolved 2026-09-14 (round 5, capability map — `view='capability'`):

- **Built the capability map** (`precis.pcb.svg.render_capability_map` +
  `PcbHandler._render_capability`, `get(view='capability',
  args={'name'?,'format':'svg'|'ledger'})`): a schematic overview off the
  generator's OWN `pcb_generators` ledger + canonical params — one square
  per grid cell (usable=green, unusable/reserved=vermillion), one dashed
  ring per plaza with 8+1 coloured dots for slot status (used/free/
  reserved), every electrode's own escape via drawn as a dot+leader line
  back to the pad centre (the ONLY visual confirmation a `rim` board's
  escape geometry exists at all, since `rim` has no tracked plaza dict),
  pin names, and a computed-sizing text block (pitch/gap/via/
  hv_separation). Deliberately NOT the exact fab-accurate zigzag geometry
  (`_electrode_polygon` owns that; `view='svg' args={'level':'fab'}` is
  the render that must match the artefact) — a plain square per cell
  keeps the ledger's own STATUS legible instead of buried in tooth
  geometry, matching the spec's "found by looking" DoD for a different
  question ("which pads can I actually drive") than the fab-fidelity
  view answers. `format='ledger'` tabulates the SAME ledger dict
  (`render_agent_table`) rather than inventing a second on-disk shape —
  the ledger IS already the machine-readable artefact the spec asks for.
- **Real defect found stress-testing this round's own new code, not a
  pre-existing one**: the capability map's title/summary/sizing text (3
  lines) plus a 5-row colour legend routinely need MORE width than the
  pad grid itself on a small array (a 9x9 field at the spec's own default
  2mm pitch is only 18mm wide) — an early version sized the viewBox off
  the pad grid + a flat margin alone, so the summary/legend text ran past
  the viewBox's right edge and was silently CLIPPED by the root `<svg>`'s
  own default UA viewport (no parse error, no visible artefact corruption
  — the text nodes are simply invisible past that edge, exactly the kind
  of defect the spec's own "found by looking" DoD exists to catch, found
  here only because the round's own visual review step was actually run
  against a real raster, not skipped). Fixed by measuring the longest
  text line (a crude but adequate 0.55em/char sans-serif width estimate —
  no text-measurement library available) and widening the viewBox (kept
  the pad grid horizontally centred) whenever that estimate exceeds the
  grid's own content width; `test_capability_map_no_text_element_falls_
  outside_the_viewbox` (`tests/test_pcb_svg.py`) pins a geometric
  assertion (every `<text>` element's `x` inside `[vb_x, vb_x+vb_w]`),
  not just a visual spot-check, so this can't silently regress.
- **Visual-verification tooling gap, not a product bug** (filed as
  gripe 338918, not fixed here — out of this round's remit): the only
  in-sandbox SVG rasterizer (`resvg_py`, via `precis.utils.figure_source.
  _svg_to_png`) silently drops every text element using a generic
  CSS `font-family="sans-serif"` (every existing `precis.pcb.svg`
  text element, not just this round's new one) with no error — an
  explicit family name (`"DejaVu Sans"`, confirmed present in the
  container) renders fine. Also rejects `width="...mm"`-style
  dimensions (`_wrap_svg`'s own output shape) outright
  (`ValueError: SVG has an invalid size`). Neither is a real defect —
  browsers resolve both correctly — but an agent doing "render and
  look" verification in this sandbox needs to strip the `mm` suffix and
  substitute a concrete font family before rasterizing, or the check
  silently looks clean (all labels invisible, not obviously wrong) while
  proving nothing.

Resolved 2026-09-14 (round 7, `sink_grid` emission + `ewod-dogfood-1`):

- **`sink_grid` emission built, mechanically, part-agnostic** (`precis.
  pcb.generators._SinkGrid`/`_parse_sink_grid`, wired into
  `_expand_ewod_pad_array`): one bottom-side (`layer='bottom'`) component
  instance per `per_tiles` x `per_tiles` tile block, wired to that
  block's own USABLE electrode escape nets (row-major channel
  assignment against a caller-supplied `channel_pins` list — the
  generator is pure/DB-free so it cannot look up a real part's own pin
  names, per the round-6 handoff's own framing), a DIN->DOUT daisy chain
  across tiles in row-major order (the chain's own first/last nets
  exposed externally, e.g. `{name}_serial_in`/`{name}_serial_{tr}_{tc}`,
  for a board-level connection to reach), and an optional shared
  top-plate/complement-rail net fanning out to every sink. `variant=
  'rim'` + `sink_grid` together is a documented `ValueError` (round-7
  scope boundary, not attempted — rim has no tracked plaza dict a tile
  block could key off). `_pcb_generator_retire_expansion` (`_pcb_ops.py`)
  widened to retire every instance matching `refdes = %s OR refdes LIKE
  '{generator_name}\_%'`, not just the array's own exact refdes — a
  changed `sink_grid` (e.g. different `per_tiles`) would otherwise
  orphan the OLD sink instances' pins/netconns while their own escape
  nets got silently retired out from under them by the (unchanged)
  net-name LIKE query.
- **`ewod-dogfood-1` built** as a tests fixture (`tests/
  test_pcb_ewod_dogfood.py`, not the session MCP — that targets prod):
  8x8 @ 2mm pitch, one merged reservoir pad (`pad_sizes`), one HV507PG-G
  (C639448) 64-channel sink under the whole array (`per_tiles=8`), a
  bottom-side placeholder I2C temp sensor, a top-plate pogo terminal
  (plain THT pad ring — the real pogo part stays an open spec item, TODO
  in the fixture), an HV-in connector + bleed resistor + instrument pin
  header. Every cached "part" footprint is a hand-authored SYNTHETIC
  grid of pads (not the real package outline — same trimming precedent
  `tests/test_pcb_fab_export.py`'s own fixtures already use), so no
  network fetch happens. Applies, places (all parts hand-fixed — no
  `op='place'` run), exports a loadable gerber zip, and renders both
  `view='capability'` and `view='svg' level='fab'`.
- **Fixed a real, narrow bug this round's own dogfood DRC stress-test
  surfaced**: `precis.pcb.realize._real_pad_sizes`'s `raw_poly_by_name`
  construction overwrote its entry on EVERY poly-bearing raw pad sharing
  a pin (an EWOD electrode emits two: the electrode BODY and its own
  escape STUB, "nothing stops two pads sharing the same pin number" —
  this module's own docstring), so whichever iterated LAST (the tiny
  stub taper, not the crenellated body) silently won for every
  `pads_for_ir` consumer (DRC, connectivity) — inconsistent with the
  "first wins" convention the very next loop in the same function
  already uses for size/shape. Fixed: same "first wins" rule, both loops
  now agree.
- **Found, NOT fixed (architecture decision, out of remit) — filed as
  gripe 338983**: fixing the bug above did not clean up `view='drc'`,
  because it exposed a MUCH larger pre-existing defect underneath it.
  `ir.py::from_graph` always assigns every pin's `pin_dx`/`pin_dy` from
  `landpattern.offsets_for(pin_count, label=...)` — a generic,
  label-keyed SYNTHESIZED layout — regardless of whether a real
  footprint with real per-pin coordinates is attached; `pads_for_ir`
  (DRC/connectivity's pad source, and per its own docstring routing's
  too) overrides pad SIZE/SHAPE/POLY from a real footprint but never
  POSITION, which still comes from that synthesized `pin_dx`/`pin_dy`.
  Invisible for an ordinary catalog part whose LABEL names a recognized
  package family (the synthesized layout happens to approximate the
  real one); wildly wrong for `ewod_pad_array`'s own 72-pin custom grid
  (measured: `pin_dx`/`pin_dy` for one pin sat ~10mm from that same
  pin's real electrode-body position) — gerber/SVG export uses a THIRD,
  independent, position-CORRECT path (`padplace.board_pads`, real
  instance pose against real footprint x/y directly), so the defect is
  invisible there and only surfaces in `view='drc'`/routing/
  connectivity, which is presumably why rounds 2-6 never caught it.
  **This means acceptance criterion 1's "`view='drc'` is error-free"
  promise is currently unreachable for any full-size array board** —
  not a mechanical fix (`ir.py::from_graph` accepting real per-pin
  positions breaks its current "no footprint arg" layering; the
  alternative, teaching `pin_point`/routing a parallel real-position
  override the way `pad_geometry` already has for size, is the same
  class of call) — gripe 338983 has the full mechanism and measurements;
  a dedicated round should decide the shape of the fix before any
  further EWOD DRC-cleanliness work is attempted. `ewod-dogfood-1`'s own
  DRC test pins the OBSERVED (noisy, not chased-to-zero) finding count
  with this explanation rather than asserting a currently-impossible
  clean pass.

Resolved 2026-09-14 (round 8, real per-pin positions + the routed dogfood):

- **gripe 338983 FIXED — real footprint positions now reach the IR, via
  the SAME channel that already carried real pad SIZE.** Decision (not
  the layering change the gripe feared): `ir.py::from_graph` still takes
  no footprint argument, and `pin_dx`/`pin_dy` are still synthesized at
  build time; a new sanctioned mutator (`PcbIR.set_pin_offset`, dirty
  cascade identical to `move_instance`'s) lets the layer ABOVE overwrite
  them per pin, and `precis.pcb.session.apply_real_pin_offsets` — the
  position twin of `footprints_by_refdes` -> `realize.pad_geometry` —
  does exactly that, wired into `session.build_ir` itself so every
  consumer that reads a pin position (DRC, maze-router pad claims,
  ratsnest/cost, courtyard hulls, silk) inherits it from ONE call site
  rather than each caller remembering. Position is taken from the first
  raw pad of each pin (the same first-wins rule, and deliberately the
  same iteration order, `realize._real_pad_sizes` uses for size/poly —
  an electrode's body must not get its outline from one pad and its
  anchor from another). Unmatched pins keep the synthesized bound AND
  keep saying so (`pin_offsets_synthesized`). All three `build_ir`
  callers pass both caches (`handlers/pcb.py::_build_ir`, `pcb_place`,
  `pcb_route`); `pcb_route`'s own `footprints_by_refdes` call was also
  one source short — it never passed LOCAL (authored/generated)
  footprints, so every EWOD board's copper reserved landpattern BOUNDS
  on the router's occupancy grid.
- **`view='drc'` is signal, not noise, and is asserted as such**: the
  8x8 `ewod-dogfood-1` array's own pads, through the IR/DRC path, now
  produce ZERO array-internal clearance errors (`test_dogfood_drc_view_
  findings_are_all_the_documented_side_gap` runs `check_clearance` over
  the array's pads in isolation — isolating it is what makes the claim
  meaningful, since a sink's channel pins share their electrodes' nets
  and no net-name filter could separate array-internal pairs from
  array-vs-sink ones). A second test pins the underlying property
  directly: every ARR1 pad from `realize.pads_for_ir` sits where
  `padplace.board_pads` (the gerber writer's own path) puts it —
  the two pad sources describe one board again.
- **The spec's "B.Cu escape needs no new routing code" claim is FALSE as
  written — disproven by running it, and the reason is architectural.**
  `op='route'` on the dogfood board (sink under the array, every escape
  net fanout-2) routes the ordinary nets fine and leaves EVERY electrode
  escape unrouted. Mechanism: the IR carries ONE position per PIN, so an
  electrode's three authored pads (crenellated body + neck stub + plaza
  via) collapse to the body alone — **the router never sees the plaza
  via**, which is the escape the design is built around, and must invent
  its own layer change starting from the electrode body, inside a field
  where every F.Cu cell is already claimed: `realize._stamp_pads` claims
  each pad as its ENCLOSING CIRCLE (`hypot(w,h)/2`), and a 1.9mm
  electrode's enclosing circle (2.69mm) is wider than the 2.0mm pitch by
  construction, so neighbours' claims overlap and no via site exists
  inside the array. Not a tuning miss and not fixable per-generator: the
  fix is multi-pad-per-pin in the IR (both `pad_geometry` and
  `pads_for_ir` are per-PIN and index-aligned with `ir.pin_*`, and
  `handlers/pcb.py` zips one against `placed_pin_ids` with
  `strict=True`). Filed as gripe 339236; **acceptance criterion 1's
  "routes escapes on B.Cu" is therefore NOT met and needs that
  architecture round** (a natural companion to the pre-place-route block
  spec, which has the same "a block owns pre-routed copper the annealer
  never enters" shape). `ewod-dogfood-1` asserts the honest state: an
  unrouted escape stays VISIBLE (non-`realized` status + a recorded
  reason), never silently green. **Closed 2026-09-18**: island
  terminals made the via visible; the enclosing-circle claim was then
  the sole remaining wall and is gone (gripe 346962, true rect/polygon
  claims + contest + centre-cell invariant). The fixture assertion
  flipped to "escapes route; failures keep a reason"; the residue is
  gripe 347037 (congestion race).
- **What slices 1-2 delivered, end to end** (rounds 1-8): authored local
  footprints + polygon pads + role/mask/paste through store, padplace,
  DRC, SVG and gerber; the `ewod_pad_array` generator (derived sizing
  with validated floors, meshing zigzag with proven constant-gap
  channel, plaza rings, tapered/chamfered escapes, merged pads, reserve
  ledger, `sink_grid` bottom-side driver emission with a serial chain);
  electrode-gap net classes; the `view='capability'` map + ledger;
  pads-only `view='drc'`; fab-notes/gerber export; and `ewod-dogfood-1`
  as a fixture that applies, places, exports a loadable zip, renders,
  DRCs clean array-internally, and routes. **Not delivered** (each named
  above or in its own slice): B.Cu escape ROUTING (needs multi-pad pins),
  bottom-side pad layers (`rules.py::PAD_LAYER`, Slice 3), stackup
  authoring / inner-layer heaters / dispenser (Slices 3-4), and the
  pre-place-route block spec round.

Still open:

- **HV separation row, pogo pin, sink granularity/part**: RULED
  2026-09-18 — see "Rulings 2026-09-18" at the top of this log (per-
  class IPC rows / copper-tape hinge, no pogo / HV507 balanced by chain
  order). What remains of the sink item is only how array pins bind to
  switch-channel order along the chain (auto-assignment vs explicit
  map) — decide with the block spec.
- **Suppression-map schema**: how a CROSS-generator consumer (heater
  legs, spare GND) names and claims reserved slots is still part of the
  block-spec round. Round 2 shipped a slot-id scheme good enough for
  intra-generator use (`ewod_pad_array`'s own `reserve: ["P{row}_{col}:
  {N|S|E|W|NE|NW|SE|SW|C}"]`, ledger-visible free/used/reserved per
  slot) — the open part is a second generator/consumer claiming against
  the SAME ledger, which needs a shared store, not just a per-call param.
- Droplet-size ↔ geometry advisory (pitch vs droplet base diameter, the
  1/10–1/20 gap-height band): warn-only in the generator, or a
  `feasibility` view extension?
