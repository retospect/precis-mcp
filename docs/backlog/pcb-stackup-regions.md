---
status: in-progress
title: pcb — stackup spec, derived fill regions, via-to-plane, pin affinity, rooms and side moves
prio: high
pillar: 3d-design
---

# pcb: stackup, fill regions, pin affinity and via-to-plane

Source: the 2026-10-16 survey `/tmp/gripe-fleet/w6/pcb-stackup-plan.md`
(witty-forging-bee tree at 3d6f784f8) and Reto's rulings on its twelve open
questions, the same day. The rulings are folded in below; where the survey
leaned one way and Reto ruled another, the ruling is what this item says.
Research: `perplexity-research:491417` (stackups), `491418` (plane nets in
other tools), `491443` (region/affinity placement), `491446` (switcher
layout), `491448` (routing cost rules); read with
`get(kind='perplexity-research', id='<slug>', args={'no_fetch': True})`.

## Motivation / why

The engine has the parts, each as a one-off: whole-board pours per layer
(`planes.plane_pours`), per-pin stub + drop via (`realize._plane_fanout`), a
three-stage stitcher (`realize._stitch_plane_fragments`), an independent
island checker (`connectivity.net_islands`), a `role`/`routable`/`pourable`
stackup, and H/V/D layer preferences hard-coded in the maze
(`maze.preferred_directions`, `OFF_AXIS_PENALTY`). Missing: fill
**regions** (a pour is the whole board, one net per layer), a stackup-level
**direction** and **reference**, a **pin-affinity** term or **room** in the
placer, a **side** move or side/region lock on a part, and any cost that
prefers bundled traces or penalises cutting a pour.

The trigger was a regression: 2bf9dc75e (gr489909 comment 2) made
`_plane_fanout` skip the stub for a pin whose net pours only on its own pad
layer, assuming the fill touches the pad. It does not when foreign pads'
clearance halos wall the pad in (fine-pitch IC rows), so
`test_pcb_fab_render_all_layers[3]` sees GND in pieces.

## Rulings (Reto, 2026-10-16)

- **Default stackup → the hybrid.** F.Cu signal + GND fill; In1.Cu GND
  plane, routable at a moderate cost; In2.Cu power regions; B.Cu signal +
  GND fill. "Ground pour unless we can do something more clever": any copper
  area with no assigned net region pours GND.
- **Power regions on In2.** Keep the cost of crossing a region edge small.
  `reference` is a **soft cost**, never a hard rule; pairing a wire to its
  return in the adjacent fill is mostly overkill.
- **In1 routable** at a per-layer cost of about 2–3x the base step, tuned
  from slice 1's table. "High cost does not need to be massively high."
- **Thermal reliefs:** solid by default; reliefs per class opt-in with a
  starved-thermal check. The board-wide `prototype` flag that turns
  reliefs on was a separate item, `pcb-board-profile-prototype` — shipped
  (gr495399) and deleted; `planes.ThermalSpec`/`_thermal_reliefs`/
  `drc.check_starved_thermal` are the record. Left to this item's slice
  10: the class key's schema/validation and `'relief'` as a `prod`
  opt-in.
- **Bare pour islands are stitched to their net, not dropped**; drop only
  when no via site reaches a same-net plane, and report it. Owned by
  [pcb-floating-pour-island](pcb-floating-pour-island.md).
- **Regions are derived only.** Reto draws nothing; regions come out of the
  placer (pin affinity + `derive_regions`). No authored-region op in this
  item; a later slice if one is ever needed.
- **Side flips ON by default** (two-sided placement). Presets: 4L two-sided
  (default), 4L top-only, 2L. The assembly cost is a one-time charge for the
  second side: the first bottom part is priced high, later ones low.
- **Locks are new columns**, one migration: `pcb_instances.fixed_side`,
  `max_travel_mm` (null = free), `room_id`, `group_fixed`, plus
  `pcb_planes.region` and `priority`. The side itself is already
  `pcb_instances.layer`.
- **Imported boards: unlocked means free.** Three explicit levels — locked,
  leashed to N mm, free (default). "If we want it to stay put, we say so."
  No default leash on import.
- **Reference-design blocks** (buck etc.) use the existing rigid
  group/pattern moves in `optimize.py`, plus a mirrored group side flip and
  a group bound to a room; stamping a group from a datasheet layout, with
  its hot-loop copper as fixed copper that moves with the group, is a later
  slice (11).

## Slices

Each slice lands alone. Slices 0 and 1 both edit `realize._plane_fanout`:
**slice 1 starts only after slice 0 is on main.** Slice 2 carries a
migration and lands via `/go`, never `/qland`/`/qgo`.

### Slice 0 — GND stub regression (shipped in 647be4789)

Shipped differently from the predicate first specced here. 647be4789
(router-lane-fix) does not compute `pad ∩ local pour` before the fan-out.
Instead `_plane_fanout` offers the stub and drop via to every plane pin
that has a legal site, including a pin whose net pours only on its own
pad layer, and `_prune_redundant_drop_vias` removes, after pouring, each
stub+via whose pad centre the filled pour contains. Only a pin with no
legal site trusts the pour; the post-pour connectivity check reports the
net if the fill then misses the pad. Pins whose authored fixed copper
already reaches every poured layer (`_plane_served_pins`) get no via.
fab-render seed 3 is green again; nano seed 1 stays at 35/29/36/9.

Left for slice 1: a permanent DRC rule that names a pad relying on the
pour that the pour does not touch (`pour_reaches_pad`), since the prune
asks about the pad centre and the no-site fallback asks nothing.

### Slice 1 — via-to-plane, measured across four boards

Slice 0 is on main (647be4789). Pre-claim every plane-net pin's
`_drop_via_site` candidates on the grid before signal routing, the way
`realize._claim_fiducial_keepouts` claims fiducial sites; release per pin
once its via is placed. Add `pour_reaches_pad` as a permanent DRC rule (a
pin counted as served whose pad the pour does not touch). Add a pour-quality
report per (net, layer) — fragments, largest-fragment area share, minimum
neck width — to `RealizeResult`, `pcb_routes.note` and `view='planes'`.
Expose the per-layer route cost so In1's 2–3x can be tuned from this table.

Boards (seed 1 unless noted): `tests/fixtures/pcb/esp32c3_reference.json`
(5 seeds, GND F.Cu + VCC3V3 B.Cu as the fab-render test declares);
`tests/fixtures/pcb/nano_usb_pd_testboard.py` (GND → In1.Cu, 5V/3V3 as
planes); `tests/fixtures/pcb/ewod-dogfood-6-replay-v1.json.gz` (GND on In1.Cu); the
prod export of `taz15-blank` (GND/3V3/12V via `pour_nets`).

Acceptance criteria:
- No board routes fewer nets than its pinned baseline.
- Every plane net is one component on every board (`net_islands`).
- The before/after table (routed/failed, vias, fragments per plane net,
  per board) is written into this item.
- `pour_reaches_pad` fires on a fixture with a walled-in pad and is silent
  on the five fab-render seeds.

**Measured 2026-10-16 (stackup-s1, before = 647be4789 main, after =
drop-site reservation + pour_quality + pour_reaches_pad).** Before and
after are identical on every row: the reservation changed no via and no
routed count on these boards. Fragments are pour pieces per net@layer;
islands are `net_islands` components.

| board | seed | routed/failed | vias | fragments | islands | pour_reaches_pad |
|---|---|---|---|---|---|---|
| esp32c3 | 1 | 11/0 | 29 | GND@F 3, VCC3V3@B 1 | 1, 1 | 0 |
| esp32c3 | 2 | 11/0 | 31 | 1, 1 | 1, 1 | 0 |
| esp32c3 | 3 | 11/0 | 33 | 1, 1 | 1, 1 | 0 |
| esp32c3 | 4 | 11/0 | 32 | 1, 1 | 1, 1 | 0 |
| esp32c3 | 5 | 11/0 | 32 | GND@F 3, VCC3V3@B 1 | 1, 1 | 0 |
| nano (as tested) | 1 | 35/29 (silk 36, warns 9) | 92 | no pours | — | 0 |
| nano, GND→In1 + V5→In2 declared | 1 | 34/30 | 104 | V5@In2 1, no GND pour | **V5 15** | 0 |
| ewod replay, closed | 0 | 31/24 | 55 | no pours | — | 0 |
| ewod replay, In2 open | 0 | 51/4 | 80 | no pours | — | 0 |
| taz15-blank as stored (PV+ In1, 12V_POWER In2) | 0 | 66/13 | 136 | 12V 1, PV+ 2 | 12V 1, **PV+ 2** | 0 |
| taz15-blank + GND→B, 3V3→F | 0 | 64/15 | 143 | 12V 1, PV+ 1, GND 1, 3V3@F 12 | **GND 5, PV+ 2**, 3V3 1, 12V 1 | 0 |

**Floor NOT met — known pre-existing failure.** "Every plane net is one
component" fails before and after alike, in three cases: taz15-blank as
stored, PV+ in 2 pieces; taz15-blank with GND/3V3 poured, GND in 5;
nano with GND→In1 and V5→In2 declared, V5 in 15. The drop-site
reservation does not touch it. The fix is stitching work:
[pcb-floating-pour-island](pcb-floating-pour-island.md) or slice 4
(gr492108; partly fixed 2026-10-17 by pour-stitch, remaining counts in
[pcb-floating-pour-island](pcb-floating-pour-island.md) "Measured"). Slice 1 landed on the coordinator's ruling
(2026-10-16): no row regressed.
The floor is vacuous on nano and the ewod replay as tested: they pour
nothing (no `pcb_planes` rows; ewod has no GND net). Prod taz15-blank
carries PV+/12V_POWER planes, not the GND/3V3/12V `pour_nets` this item
assumed. Slice 4 measures on boards that actually have planes. esp32c3's extra GND F.Cu pieces are floating copper, already
reported in the route note. In1 cost not tuned: no board here routes on
In1, so `layer_cost` stays opt-in.

Owning files: `src/precis/pcb/realize.py`, `src/precis/pcb/drc.py`,
`src/precis/pcb/maze.py` (grid claims), `src/precis/handlers/pcb.py`
(`view='planes'`), `tests/test_pcb_planes.py`, `tests/test_pcb_drc*.py`.

### Slice 2 — lock and region columns

Shipped 2125d4e62 (migration `0219_pcb_locks_regions.sql`: `pcb_instances`
`fixed_side`/`max_travel_mm`/`room_id`/`group_fixed`, `pcb_planes.region`/
`priority`). `git show 2125d4e62` is the record.

### Slice 3 — stackup spec keys, hybrid default, presets

**Built 2026-10-17, unlanded (g-stackup-plane, overnight commit-only),
except the default switch.** Keys `direction`/`reference`/`route_cost`
plus `fill` and `power_regions` (the hybrid needs a way to say "GND fill"
and "power regions here"); `maze`/`globalplan` read `direction`; off-axis
multiplier is the route param `off_axis`, per-layer cost the stackup's
`route_cost` (run param `layer_cost` overrides); `op='stackup', preset=`
(`ground=` renames GND) with the preset and its sides on
`pcb_boards.meta`. **`DEFAULT_STACKUP` is NOT switched**: the measurement
below loses nano nets against its one-way floor, so the hybrid is the
`4l-two-sided` preset until Reto rules. For the same reason a LEGACY
stackup's `plane_net` without a row still pours nothing (gr495287's
half); a preset's does. Measured on nano, nano-r2 and esp32c3 (seed 1,
place+route through the jobs; `A` = legacy default as stored, the floor
baseline):

| board | stackup | routed/failed | vias | DRC errors | notable warns |
|---|---|---|---|---|---|
| nano | A legacy, no plane rows | 35/29 | 92 | 78 | clearance 9 |
| nano | B legacy + In1 GND poured (gr495287 alone) | **38/26** | 130 | 74 | silk_edge_clearance **6 > floor 3** |
| nano | C In1 GND plane, routable 2.5x | 35/29 | 124 | 78 | clearance 10 > floor 9 |
| nano | D C + GND fills | 32/32 | 202 | 79 | clearance **286** |
| nano | E full hybrid (D + In2 regions) | **31/33** | 122 | 85 | clearance 9 |
| nano | F hybrid, In1 not routable | 34/30 | 238 | 74 | clearance 271 |
| nano | G fills, In1 not routable, no regions | **37/27** | 261 | 80 | clearance 287 |
| nano-r2 | A / B / C | 35/37, 35/37, 32/40 | 104/114/101 | 106/107/110 | — |
| nano-r2 | D, E, F, G | route refused: anneal parks C_AVCC 0.04 mm outside the outline | — | — | — |
| esp32c3 | A / C | 11/0, 11/0 | 21 / 37 | 0 / 0 | — |
| esp32c3 | D / E | 11/0, 11/0 | 72 / 72 | 0 / 0 | clearance 71 |

Read: In1 routable costs nano 3 nets (B 38 → C 35); fills cost 3 more and
regions 1 more. The fills' clearance warns are pours cut at the router's
clearance (0.100 mm) under the 0.150 mm house tier — fix before any
default switch. Not measured: taz15 (prod export), EWOD replay (no GND
net, so no fill or plane changes), usbc-attiny-dogfood-b (prod; needs
`op='stackup', preset='4l-two-sided'` to pour all four layers).

Add `direction` (`'h'|'v'|'d'|'any'|<angle_deg>`) and `reference` (layer
name; default the nearest layer whose pour covers most of the board) to
`ir.STACKUP_LAYER_KEYS` / `validate_stackup`. `maze.preferred_directions`
and `globalplan._off_axis` read the spec, index order as fallback; the
off-axis multiplier and a per-layer route-cost multiplier become board
parameters. `op='stackup', preset=` with `'4l-two-sided'` (the hybrid,
sides on), `'4l-top-only'`, `'2l'`. `DEFAULT_STACKUP` becomes the hybrid:
In1 GND plane `routable: true`, direction `h`, cost ~2–3x (slice 1's
number); In2 power regions, direction `v`; F/B signal with GND fill.
`reference` feeds only a soft cost (slice 8), never a refusal.

Acceptance criteria:
- `tests/test_pcb_ir.py`: the new keys validate and bad values refuse.
- A maze test: an `'h'` layer penalises vertical steps, an `'any'` layer
  does not.
- Each preset round-trips through `view='stackup'`.
- The default switch is measured on the slice-1 boards before landing; no
  board routes fewer nets, and the table is written into this item.

Owning files: `src/precis/pcb/__init__.py` (`DEFAULT_STACKUP`),
`src/precis/pcb/ir.py`, `src/precis/pcb/maze.py`,
`src/precis/pcb/globalplan.py`, `src/precis/handlers/pcb.py`
(`_op_stackup`). No per-layer preferred direction (unified
place-route Decision 2, Reto 2026-10-18).

### Slice 4 — derived pour regions

**Built 2026-10-17, unlanded (g-stackup-plane), no migration** (0219's
columns carry region/priority; `content_hash` is code). `plane_pours`
takes `PourSpec`s (region, priority; several nets per layer; a stackup
`fill` at priority -1 takes what is left); `derive_regions` +
`realize.derive_power_regions` (concave hull of the net's pins dilated by
two drop-via steps, priority = pin count, a net that does not fit routes
as traces), re-derived per route right before realize and written back as
derived rows; the fan-out searches only inside a net's region; the
`op='plane_net'` guard is one net per region; `view='planes'` lists every
effective pour (rows, `stackup`, `fill`, regions) with priority and area;
DRC `pour_fragmented` (warn). Islands: a pour piece no via, track, pad or
fixed copper reaches after the last stitch round is dropped and named in
the net's route note (pcb-floating-pour-island). Left: import does not yet
carry each fill polygon as a region row (taz15 mixed layers).

Needs slice 2's columns. `planes.plane_pours` takes regions: pour = region ∩
outline-inset − foreign copper, highest priority first, lower-priority
regions cleared around higher (KiCad/TopoR semantics). Several nets on one
layer when their regions are disjoint after clearance; `_op_plane_net`'s
one-net-per-layer guard becomes one-net-per-region. `planes.derive_regions(ir,
placement)`: for each power net with no whole-board plane, its region on its
layer is the dilated concave hull of its pins' legal drop-via sites, trimmed
to the outline and to other regions; priority by pin count. Every area no
region claims pours GND. Written through the existing `store._pcb_ops.pcb_planes_replace_derived` path (authored rows stay locked against it); no authored-region op.
**Idempotency gap to close here:** `session.content_hash` hashes planes as
`(layer, net, region_hint)`; slice 2 left it alone. Once regions change
the pour, hash `region` and `priority` instead (a job re-run with a new
region must not reuse a cached result).
Import carries each fill polygon (taz15's 3V3/GND fills round-trip).
DRC `pour_fragmented` (components per (net, layer) > 1 after stitching).

Acceptance criteria:
- Nano with 3V3 and 5V as regions on In2.Cu; esp32c3 with VCC3V3 as a
  region on B.Cu: every 3V3/5V pin inside its region is served by a via.
- Regions never overlap after clearance; the unclaimed rest is GND.
- `view='planes'` lists each region with net, layer, priority, area.
- No board routes fewer nets than slice 1's table.

Owning files: `src/precis/pcb/planes.py`, `src/precis/pcb/realize.py`
(`_pour_planes`), `src/precis/pcb/drc.py`, `src/precis/handlers/pcb.py`,
the EasyEDA import reader, `tests/test_pcb_planes.py`.

### Slice 5 — rooms, keepout binding, side moves, group side flip

`pcb_features` `ftype='room'` (polygon, `layer` top/bottom/both,
`meta.members` or `meta.selector`, `meta.mode` inside/outside,
`meta.ground` plane/local/tree) and `op='room'`; `pcb_instances.room_id`
binds a part, and a group binds through its members. Enforced hard in
`OptimizeEngine._placement_is_legal`, soft pull for members the seed left
outside; `ftype='keepout'` binds through the same path. `MoveKind`
gains an instance side flip (SMD only; pads mirrored by `padplace.place_footprint_pads` / `_effective_layer`,
courtyard re-checked, `_sides_clear` rules) and a mirrored rigid-group side
flip; both **on by default**, off for the `4l-top-only` preset and for `fixed_side`/`group_fixed` parts. Assembly cost: a one-time
second-side charge — the first bottom part pays high, later ones low.
Leash levels read slice 2's columns: locked / leashed N mm / free.

Acceptance criteria:
- No room member ever leaves its room; a keepout is never entered.
- A `fixed_side` part never flips; a flipped group stays mirrored-rigid.
- esp32c3 with a "tall parts" room in one corner on the bottom places them
  there.
- taz15 with connectors/mechanicals locked and the switcher room
  side-locked keeps locked hand poses bit-for-bit and routes at least the
  unlocked baseline.
- With one bottom part the assembly charge is the high first-part price;
  with three it is that plus two low increments.

Owning files: `src/precis/pcb/optimize.py`, `src/precis/pcb/pose.py`,
`src/precis/pcb/padplace.py`, `src/precis/pcb/cost.py`,
`src/precis/handlers/pcb.py`. Closes
[pcb-keepout-does-not-bind](pcb-keepout-does-not-bind.md).

### Slice 6 — pin affinity and the derive/anneal loop

`cost.py` term `plane_affinity` = Σ over pins of region nets of
w_class · d(pin, drop-via-legal part of region)², zero inside; a
whole-board GND contributes ~0 by construction; through-hole pins on an
inner plane get full weight. `pcb_place` alternates anneal N iterations →
`derive_regions` → continue; regions freeze when `op='route'` starts.
`OptimizeConfig.plane_affinity_w`; every new param registered in
`PARAMS_SCHEMA` (gr469872).

Acceptance criteria:
- Weight 0 is bit-for-bit today's run.
- On nano, taz15-blank and esp32c3 (ratsnest mm, crossings, routed, vias —
  the table format of [pattern-groups](pattern-groups.md)), on improves routed or
  vias on at least 2 of 3 and never loses a routed net.

Owning files: `src/precis/pcb/cost.py`, `src/precis/pcb/optimize.py`,
`src/precis/pcb/planes.py`, `src/precis/workers/` (pcb_place params).

### Slice 7 — routing cost aids, detailed

Bundling discount in the maze step loop: a step whose perpendicular
neighbour cell carries another net running the layer's direction costs
less, only when both classes bundle (`pcb_net_classes.rules.bundle`,
default true; `hv`, `drive`, `switch_node`, `sense`, `fb`, `analog`,
`diffpair` do not) and the pair is not in the class spacing matrix. Via
plazas priced as soft keep-outs (`corridor_route(soft_penalty=)`).

Acceptance criteria:
- On the four slice-1 boards pour fragments do not increase and routed
  does not decrease; total trace length reported.
- A synthetic 5-wire fixture (`qfp_fanout.py` family) routes the five
  wires adjacent on the fill layer.

Owning files: `src/precis/pcb/maze.py`, `src/precis/pcb/globalplan.py`,
`src/precis/pcb/realize.py`.

### Slice 8 — routing cost aids, global

Ground-severance edge cost and a "new corridor" fixed cost in
`globalplan.plan_routes`; pour-quality feedback into
`optimize.anneal_route_loop` (worst-severing net ripped first);
`return_path_gap` as a soft cost and a DRC warning from the `reference`
key.

Acceptance criteria:
- On the ewod replay and taz15, the plan-guided run ties or beats plain on
  routed and has fewer pour fragments.
- `return_path_gap` is a warning, never an error.

Owning files: `src/precis/pcb/globalplan.py`, `src/precis/pcb/optimize.py`,
`src/precis/pcb/drc.py`.

### Slice 9 — switcher rooms

A room with `ground='local'`: members' GND pins join a high-priority local
GND region inside the room, tied to the main plane through one reserved via
cluster at the IC-defined point; the adjacent plane stays continuous under
SW unless the room sets `plane_cut_under_sw`. `switch_node` class never
poured, never bundled; FB/COMP/sense get an automatic `separation` from SW
and the inductor; Cin↔IC and Cout↔diode `proximity` measures derived from
the `supplies`/`client_of` roles.

Acceptance criteria:
- On nano's TPS54360 bucks, each Cin–IC–diode loop area is reported in the
  job summary and is below a stated threshold; SW copper is not poured;
  the GND plane under the room is one component.

Owning files: `src/precis/pcb/planes.py`, `src/precis/pcb/realize.py`,
`src/precis/pcb/optimize.py`, `tests/fixtures/pcb/nano_usb_pd_testboard.py`.

### Slice 10 — return stitching, via fences, thermal reliefs

Class-gated post-pass: a GND return via near each signal via that changes
reference layer; fences along `ground='local'`/RF room edges at a maximum
pitch. Per-class thermal reliefs (`thermal: 'solid'|'relief'`) with a spoke
generator and a `starved_thermal` DRC; solid stays the default.

**Partially shipped (gr495399):** the spoke generator and
`starved_thermal` exist — `planes.ThermalSpec`/`_thermal_reliefs`, driven
by the board profile (`thermal_spec(profile_rules)`, prototype on, prod
off) with the read side of `thermal: 'solid'` honoured from class rules.
Left here: the class key's schema/validation (`thermal` is read but no
op validates it yet), `'relief'` as an opt-in on a `prod` board, and the
return-stitching/via-fence half.

Acceptance criteria:
- Relief off is bit-for-bit today's copper.
- A relief class gets spokes on every pad; `starved_thermal` fires on a
  pad with fewer than the minimum resolved spokes.

Owning files: `src/precis/pcb/realize.py`, `src/precis/pcb/planes.py`,
`src/precis/pcb/drc.py`.

### Slice 11 — stamp a group from a datasheet layout (later)

A reference-design block placed from a datasheet's recommended layout, its
hot-loop copper stored as fixed copper that moves (and mirrors) with the
group. Specced when slice 9 has measured the derived-measure approach.

## Explicitly NOT in scope

- Authored fill regions (Reto: derived only).
- A hard reference-plane rule (Reto: soft cost).
- A maze goal set for via-to-plane (`OccupancyGrid.route(extra_goal_terminals=)`)
  — only if slice 1's table shows walled-in pins remain.
- River routing / bus order (`pinswap._hungarian`); the global-plan
  router item owns pin assignment.

## Acceptance criteria

The item is done when slices 0–10 have each met their criteria above, and:
- every plane and region net is one component on the four slice-1 boards;
- the hybrid is the default stackup and routes no board worse than the old
  default did in slice 1's table;
- a part moves only as far as its lock level says (locked, leashed, free).

## Target + blast radius

`src/precis/pcb/` (`realize`, `planes`, `maze`, `globalplan`, `optimize`,
`cost`, `ir`, `drc`, `pose`, `padplace`), `src/precis/handlers/pcb.py`,
`src/precis/store/_pcb_ops.py`, one migration. Slices 3 (default stackup)
and 5 (side flips on by default) change every existing board's next
place/route; both are measured before landing.

## Related

- [pcb-floating-pour-island](pcb-floating-pour-island.md) — island
  stitching (Reto: stitch, not drop).
- `pcb-board-profile-prototype` — shipped and deleted; set the default
  for slice 10's thermal key (prototype on, prod off).
- [pcb-keepout-does-not-bind](pcb-keepout-does-not-bind.md) — closed by
  slice 5.
- [pcb-missing-constraint-classes](pcb-missing-constraint-classes.md) §B
  (one-sided assembly cost) and §E-1 (pairwise spacing) — slices 5 and 7.
