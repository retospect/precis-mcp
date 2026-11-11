---
status: in-progress
claimed-by: pcb-ws4
title: PCB workspaces — netlists and modules built by code, place/route as cached services, DRC in MCP
pillar: 3d-design
prio: high
model: fable
blocked-by: structure-workspaces
---

# PCB workspaces — the workspace model applied to boards

> 2026-10-25: `vroute` and the `pcb_route` job are deleted (tag
> `vroute-final`, gr515107). `pcb.local.route` and `op='route'` refuse
> until the topo realiser lands (pcb-topo-place slice 5); `pcb_place` is
> the only compute job left. Rows below that prescribe the route leg are
> historical.

Applies [structure-workspaces](structure-workspaces.md) (exec blocks
against head with keep/revert — §Amendment 2026-11-08 — commit boundary,
construction traces, `pga` assembly API, relax as a service) to the
`pcb` kind. Same versions and worker, same `commit` shape; the design is
`pcb:<slug>` instead of `se:<slug>`. Reto's three questions are §Q1–§Q3;
the rest is what a build needs. The design-optimisation kit
([se-flow-kernel](se-flow-kernel.md) slice 4b) is se's: pcb shares its
snapshot and ask/tell layer only — builders and evaluators differ, and
place/route/DRC stay server services like relax.

The de facto netlist format already is Python that returns a dict:
`tests/fixtures/pcb/nano_usb_pd_testboard.py::build_design()` emits the
batch `put(kind='pcb')` payload (`{components, nets, connections,
features, footprints, generators, meta, net_classes, roles, profile}`,
`handlers/pcb.py::put` ARGS; `store/_pcb_ops.py::PcbMixin.pcb_apply`
docstring has the row shapes). This spec gives that dict a typed builder,
a trace, a replace-not-extend `commit`, and makes placement and routing
callable from a block.

## Amendment 2026-11-08 — exec blocks against head

Reto, 2026-11-08: the structure-workspaces amendment
([structure-workspaces](structure-workspaces.md) §Amendment 2026-11-08,
R1–R10) applies to boards. The interactive loop for a board is exec
blocks against head; head is the source. Blocks never produce copper;
the library (`precis_geom.pcb`, `pga`, the `Board` IR) and the job lane
survive; the importer is `commit`, the only writer of the pcb tables.
The sections below are written to that.

- **Head is the board snapshot** (R1): the `to_batch()` dict (the
  `pcb_apply` payload) + constraints, measures and requests. Intent
  (generator + args, constraints), not only rows. No host git, no
  `script_sha`; blocks and parts cite (version, block index, code
  anchors) (R8).
- **Exec** (R3): `put(kind='pcb', id=<slug>, mode='exec', text='...',
  args={'keep': 'auto'|'candidate'})` runs with `b` (the `Board`) in
  scope, the design library and the `precis` proxy (R3a); the echo
  returns prints (~20 kB cap), an entity diff (parts/nets/handles
  created, moved, removed) and a structured error. Default `keep:'auto'`
  commits a clean block (validator + `Board.check` errors clean) and
  returns diff + new version; a flagged block leaves a candidate with the
  findings. `keep:'candidate'` stops at the candidate: `view='check'`/
  `drc`/`placement` take `args={'version': <candidate>}`, then
  `mode='keep'` commits or `mode='revert'` drops it. `text` may be a list
  of blocks (ordered, one echo each, stop at the first failure, per-block
  commit under auto, one stacked candidate under candidate; caps per
  block). A raising block writes nothing. Versions are recorded on keep
  (R2: `design_revisions` + `design_checkpoints`, full copy, no journal,
  block text only while a candidate); stepping back past a keep is out of
  scope for slice 1x (gr598376).
- **Single write path** (R2a): pcb head (the relational tables) is
  written only by `commit(ref, snapshot) → version`, one transaction,
  always forward. The candidate is one `design_checkpoints` row holding a
  jsonb board snapshot keyed by natural keys (refdes, net name), as
  `pcb_netlist_snapshots` already does; the worker has no DB handle.
  Derived tables (`pcb_copper`, DRC findings, netlist snapshots) are
  regenerable and not in the snapshot. Acceptance: hash every head row of
  the pcb tables before a block that adds, moves and deletes things; the
  hash is unchanged after exec (candidate mode) and after `revert`.
- **Keep path = `commit`, slice 1's replace/retire:**
  replace, retire rows absent from the snapshot (same refdes/net →
  update in place), keep poses of surviving refdes, rip copper on a net
  whose membership changed. Route/feature row ids may renumber on keep;
  agent handles are natural keys.
- **A block never produces copper.** Place/route stay requests (slice 2,
  Q2 option c); the validator refuses tracks/vias in a candidate
  ("copper enters through requests"). Long things go to the lane with the
  `hit | queued` echo.
- **Containment** (R4, R4a): the same forked warm worker, rlimits,
  scrubbed env, no network, never in-process; AST lint in front of the
  fork and the audit hook in the child; trusted-side validation (schema,
  entity-count cap, `Board.check`) before commit or candidate. Shared
  with structure slice 1.
- **The script path is not a surface** (Reto, 2026-11-08): `main.py` as
  source, host git per board, a venv per lock sha and a hand-run import
  step are gone as agent-facing modes. Survivors: the library blocks call
  (`precis_geom.pcb`), graduation (R7), the job lane (the same code under
  the same namespace, committing through the same `commit`) and the
  DB-free library run (§Q3) for laptop/CI. The importer is `commit`;
  two surfaces would be two places head can be written from (R2a).
  Memory: me598371, me598372.
- **DB-free library run** (slice 4, landed): the laptop/CI path and a
  reference for the `Board` loaders; not a design loop (§Q3).
- **Modules stay functions** (item 5): promoted from blocks via
  gr596135 (`graduate:block`), then `ws-promote`.
- **Skill** (R9, slice 5) and the metrics (blocks per kept change,
  keep/revert, median block length, wall-limit hits) as in the structure
  amendment; R10 (direct ops vs blocks) is measured per task.

## Core design

1. **A PCB workspace is a board design plus its recorded versions.**
   The design is `pcb:<slug>`; head is the board snapshot (Amendment).
   Blocks run against it through `mode='exec'`; `commit` is the only
   writer of the pcb tables. Nothing in the exec machinery is
   pcb-specific — the `Board` loaders, the validator and `commit` are.
2. **A block builds L0 + constraints, never copper.** The candidate
   snapshot is `Board.to_batch()` — byte-for-byte the `pcb_apply`
   payload — plus constraints, measures and requests, keyed by refdes and
   net name; `commit` is the replace pass over it (§Commit). Poses are
   seeds or `fixed` pins, as today's component `x/y/rot/fixed`; tracks,
   vias, pours never enter through a block (authored fixed copper only
   via a generator recipe, as today).
3. **Place and route are services** (§Q2, option c): a block records a
   request keyed by the board's content hash; `commit` dispatches the
   existing `pcb_place`/`pcb_route` job types or answers from cache; a
   later block reads the result through the read MCP. Solver output
   lands in the DB through the job path only (never-worse guard,
   judged mutation, `last_route`), never through a block or snapshot.
4. **Replace, not extend.** Today a batch put only creates or patches
   (`_pcb_apply` keys components by refdes and nets by name; no removal
   op exists — engine-plan #7 `op='remove'` was never built). `commit`
   diffs the snapshot against head: rows absent from it retire
   (`retired_at`, the `_pcb_generator_retire_expansion` precedent),
   poses of surviving refdes are kept (gr481705: changed generator
   params keep poses), router copper on a net whose membership changed is
   ripped (the judged-mutation rule), DRC runs re-key. A block that drops
   a part or net from the board is the removal path.
5. **Modules are functions.** A sub-circuit is a Python function that
   takes the board and its port nets and makes parts; refdes prefixing and
   net namespacing are the builder's, the hierarchy is the trace. This is
   [pcb-meta-blocks](pcb-meta-blocks.md) level 1 (schematic blocks) as
   code; level 2 (pre-routed bundles) is the placed-module cache (§Modules).
6. **Inspection stays in MCP.** `view='check'`/`drc`/`crossings`/
   `ratsnest`/`placement`/`route-status`/`schematic`/`svg`/`fab`, `op=
   'check'`, `op='phase'`, `op='order'`, `import_epro` — unchanged. New:
   `view='trace'` for boards and a `frame` column on findings.

## Q1 — the Python API

Package `precis_geom.pcb` inside the `precis-geom` wheel (structures
§Packaging); alias **`pcb`**. `pg` and `pga` keep their meaning; a block
that also builds mechanics uses all three. In a block `b` is the head
`Board` and the library is in scope under these aliases; the imports
are what CI and the lane write out.

```python
import precis_geom as pg
import precis_geom.assembly as pga
import precis_geom.pcb as pcb
```

| object | call | emits (batch key) | notes |
|---|---|---|---|
| board | `pcb.Board(slug, *, profile='prod'|'prototype', stackup='4l-two-sided'|'4l-top-only'|'2l'|[layers], sides='both'|'top', outline=pcb.rect(w, h, corner_mm=) | [(x,y),...])` | `profile`, `meta.stackup_preset` + `placement_sides` (`op='stackup'` semantics, `STACKUP_PRESETS`), `features[outline]` | one outline; `stackup=[...]` is the `pcb_set_stackup` list verbatim; the constructor serves the DB-free library and the lane — an exec block's `b` is the existing head board, created by `put(kind='pcb', …)` |
| plane | `b.plane('In1.Cu', net=gnd, priority=)` | `op='plane_net'` rows | regions stay derived ([pcb-stackup-regions](pcb-stackup-regions.md)) |
| net | `b.net(name, *, cls=, current=, voltage=, impedance=, edge_rate=, function=, note=)` → `Net` | `nets[]` (`est_current_a`, `working_voltage_v`, …) | `domain` electrical only (`_reject_non_electrical`) |
| net class | `b.net_class(name, **rules)` | `net_classes{name: rules}` (`op='class_rules'` shape) | pair clearances via `rules['pairs']` as today |
| role | `b.role(name, voltage=)`; `part.supplies(role, pins=[...])`; `pin.client_of(role)` | `roles`, component `supplies`, pin tags | [pcb-lazy-netlist-and-checks](pcb-lazy-netlist-and-checks.md) §1 unchanged |
| part | `b.part(refdes, *, lcsc=None, label=, footprint=None, value=, height_mm=, roles=set(), pins=[...], note=)` → `Part` | `components[]` | `lcsc` → `part_lcsc`; `footprint=` names an authored `pcb.Footprint`; neither → `landpattern` synthesized bound, flagged as today |
| pin access | `part['VBUS']`, `part[1]`, `part.pins('A1','B1')` → `Pin`s | — | pad numbers map to names at import (`mapped`) |
| pin class | `part.pin(num|any_of=[…], name=, etype=, fmax_hz=, max_current_a=, max_voltage_v=, source=, assume=)` or `pin.etype(…)` same kwargs | `pins[].etype` + `meta`, slot `pin_decls.slots[]` | the first-use class (gr520810): `power_in`/`power_out` take current+voltage, never `fmax_hz`; other etypes take `fmax_hz` (highest significant component, knee ≈ 0.35/t_rise, 0 = explicit DC); `nc` none; every class carries `source=` (chunk handle) or `assume=` (note) |
| pin facts | `pin.etype('power_in')`, `pin.current_role('shunt')`, `pin.nc()`, `pin.unused()`, `pin.must_terminate()` | `pins[].tags`/`meta` (`op='pins'` shape, `PIN_ETYPES`, `PIN_CURRENT_ROLES`) | |
| connect | `net.tie(*pins)`; `b.wire(pin_a, pin_b, name=None)` | `connections[]` | `wire` without a name mints the free-ended-net name per §1d and records it. Ties land unconditionally; the first-use class gate (gr520810) fires once at finalise — `check` reports `pin.unclassified` findings and `to_batch` refuses, **aggregating every offending net's pins into one error** (gr543813), so classify before or in the same call that connects |
| footprint | `pcb.Footprint(name, pads=[pcb.pad(pin, shape, x, y, w=, h=, poly=, drill=, role=, mask=, paste=)], pin_map=, courtyard=, centroid=)` | `footprints[]` | authored/local footprints (EWOD) |
| catalog part | `pcb.catalog(lcsc)` | — | read MCP `get(kind='part', id=, view='facts')`: pin table, package; a miss returns a stub and `commit` queues the pull (as the batch put queues datasheets) |
| mechanical | `b.hole(x, y, drill=, ring=, plated=)`, `b.fiducial(x, y)`, `b.testpoint(net, x=, y=)`, `b.corner_nuts()` | `features[]` (`mounting_hole`, `fiducial`, `testpoint`; `op='corner_nuts'` shape) | |
| keepout | `b.keepout(poly, layers=[...], reason=)` | `features[keepout]` | binds once [pcb-keepout-does-not-bind](pcb-keepout-does-not-bind.md) ships; until then recorded, not enforced, and the echo says so |
| fixed | `part.at(x, y, rot=0, side='top', fixed='both'|'xy'|'rot')`, `part.leash(max_travel_mm=)` | component `x/y/rot/layer/fixed/max_travel_mm` | |
| edge | `part.on_edge('left'|'right'|'top'|'bottom', at=None)` | instance meta `edge_mate` | |
| group | `b.group(name, parts, *, rigid=True, pattern=None)` | instance meta `group/group_offset`, `pattern/pattern_instance` | |
| measure | `b.near(a, b, within_mm=)`, `b.apart(a, b, at_least_mm=)`, `b.align(a, b, axis=)`, `b.measure(metric, operands, bound=, goal=, hard=, weight=)` | `measures[]` (`MeasureSpec` operands) | only pair measures steer placement today; the rest are recorded |
| generator | `b.generate(name, generator='ewod_pad_array', **params)` | `generators[]` recipe | expansion stays trusted-side (`generators.expand`, replay rule); `pcb.generators.expand` is importable for DB-free preview |
| module | `@pcb.module def ldo(b, *, vin, vout, gnd): ...`; `b.use(ldo, prefix='U2', vin=vbus, vout=v33, gnd=gnd)` | prefixed `components[]`, `prefix/NET` internal nets | §Modules |
| check | `b.check()` → findings | — | pure L0 checks: pin names resolve, unknown nets, free-ended naming, unresolved parts, missing footprints NOT-CHECKED |
| shape | `b.shape()` → `BoardShape` (outline, one prism per placed part with `height_mm` + `origin`, hole centres/diameters, keepouts); `shape.fits(other)` → `FitResult` | — | slice 0b: derived data + the fit-check call; nothing enters the batch; one derivation in `precis_geom.pcb_shape` shared with `export.mechanical_profile`/`precis_se.board` |
| snapshot | `b.to_batch()`; `out.board(b)` (lane and DB-free runs only) | the board snapshot's batch dict; `out/manifest.json` + `out/trace.json` | in a block the snapshot is taken after the block; `out.board` is not called |
| services | `pcb.place(b, seed=, iters=, groups=)` → `Placement`; `pcb.route(b, placement=, router='vector')` → `Routing` | `requests[]` | §Q2 |

Every builder call is a traced primitive (`@traced`, structures
§Construction traces); `b.use(...)` is the module frame, the module
function's own lines are user frames.

### Worked example — USB-C + ATtiny (exec blocks)

LCSC numbers illustrative; `pcb.catalog` resolves them at build time
through the read MCP. One intent per block (R9), sent as the list form
under the default `keep:'auto'`: `put(kind='pcb', id='usbc-attiny',
mode='exec', text=[block_1, block_2, block_3, block_4])`. Each clean
block commits its own version; the echo per block is prints + entity
diff. Blocks are shown separated by `# --- block N`. The board exists
before the first block: creating it (profile `prototype`, stackup `2l`,
a 30 x 20 mm outline with 2 mm corners) stays a `put(kind='pcb',
id='usbc-attiny', …)` as today, never a block; `b` is always bound to the
head of an existing board, and `pcb.Board(...)` is for the DB-free library
and the lane only. Locals do not cross blocks (each runs in a fresh fork
against head), so later blocks look nets up by name: `b.net(name)` is
get-or-create and idempotent, so a re-run block changes nothing (both are
the builder's names).

```python
# --- block 1: holes, nets, USB-C receptacle (pcb, pg, pga are in scope)
for x, y in ((3, 3), (27, 17)):
    b.hole(x, y, drill=2.2, ring=4.0)

gnd  = b.net("GND",  cls="ground", current=0.5, voltage=0.0)
vbus = b.net("VBUS", cls="power",  current=0.5, voltage=5.0)
v33  = b.net("3V3",  cls="power",  current=0.3, voltage=3.3)
b.net_class("usb", width_mm=0.2, clearance_mm=0.2, pairs=[["D+", "D-"]])
b.net_class("power", width_mm=0.4)

# USB-C receptacle: 5.1 k CC pull-downs make it a UFP sink.
# Every pin a net touches is classified at first use — etype plus its
# facts and a `source=` chunk handle or an `assume=` note (gr520810);
# the board's finalise gate refuses the gap, naming every offending
# net and pin in one error (gr543813).
j1 = b.part("J1", lcsc="C165948", roles={"connector"})
for p in j1.pins("A4", "A9", "B4", "B9"):
    p.etype("power_in", max_current_a=3.0, max_voltage_v=20.0,
            assume="usb-c vbus")
for p in j1.pins("A1", "A12", "B1", "B12", "SHELL"):
    p.etype("power_in", max_current_a=3.0, max_voltage_v=20.0,
            assume="usb-c gnd")
j1["A5"].etype("input", fmax_hz=0.0, assume="cc line")
j1["B5"].etype("input", fmax_hz=0.0, assume="cc line")
vbus.tie(*j1.pins("A4", "A9", "B4", "B9"))
gnd.tie(*j1.pins("A1", "A12", "B1", "B12", "SHELL"))
for i, cc in ((1, "A5"), (2, "B5")):
    r = b.part(f"R{i}", lcsc="C25905", value="5.1k")
    r[1].etype("passive", fmax_hz=0.0, assume="cc pull-down")
    r[2].etype("passive", fmax_hz=0.0, assume="cc pull-down")
    b.wire(j1[cc], r[1], name=f"CC{i}")
    gnd.tie(r[2])
j1.on_edge("left")
j1.at(2.0, 10.0, rot=90, fixed="rot")   # x/y free along the edge
print(b.check().summary())

# --- block 2: the LDO as a module, used once
gnd, vbus, v33 = b.net("GND"), b.net("VBUS"), b.net("3V3")   # get-or-create by name

@pcb.module
def ldo_3v3(b, *, vin, vout, gnd):
    """ME6211 LDO with its input/output caps; ports vin, vout, gnd."""
    u = b.part("U", lcsc="C82942")
    cin = b.part("CIN", lcsc="C15849", value="1u")
    cout = b.part("COUT", lcsc="C15849", value="1u")
    u["VIN"].etype("power_in", max_current_a=0.3, max_voltage_v=6.5,
                   assume="ldo vin")
    u["CE"].etype("input", fmax_hz=0.0, assume="chip enable")
    u["VOUT"].etype("power_out", max_current_a=0.3, max_voltage_v=3.6,
                    assume="ldo vout")
    u["GND"].etype("power_in", max_current_a=0.3, max_voltage_v=6.5,
                   assume="ldo gnd")
    for cap in (cin, cout):
        for n in (1, 2):
            cap[n].etype("passive", fmax_hz=0.0, assume="bypass cap")
    vin.tie(u["VIN"], u["CE"], cin[1])
    vout.tie(u["VOUT"], cout[1])
    gnd.tie(u["GND"], cin[2], cout[2])
    b.near(u, cin, within_mm=2.0)
    b.near(u, cout, within_mm=2.0)
    return u

u2 = b.use(ldo_3v3, prefix="U2", vin=vbus, vout=v33, gnd=gnd)   # U2.U, U2.CIN, U2.COUT
print(b.check().summary())

# --- block 3: MCU, bypass cap, UPDI header, LED
v33, gnd = b.net("3V3"), b.net("GND")       # get-or-create by name; locals do not cross blocks
u1 = b.part("U1", lcsc="C168022", roles={"mcu"})            # ATtiny1616-SNR, SOIC-20
u1["VDD"].etype("power_in", max_current_a=0.05, max_voltage_v=5.5,
                assume="attiny vdd")
u1["GND"].etype("power_in", max_current_a=0.05, max_voltage_v=5.5,
                assume="attiny gnd")
v33.tie(u1["VDD"]); gnd.tie(u1["GND"])
c3 = b.part("C3", lcsc="C1525", value="100n")
c3[1].etype("passive", fmax_hz=0.0, assume="bypass cap")
c3[2].etype("passive", fmax_hz=0.0, assume="bypass cap")
v33.tie(c3[1]); gnd.tie(c3[2])
c3["1"].current_role("shunt")
b.near(u1, c3, within_mm=1.5)

j2 = b.part("J2", lcsc="C2337", label="UPDI 1x3")            # UPDI header
j2[1].etype("bidi", fmax_hz=100e3, assume="updi line")
j2[2].etype("power_in", max_current_a=0.05, max_voltage_v=5.5, assume="vcc pin")
j2[3].etype("power_in", max_current_a=0.05, max_voltage_v=5.5, assume="gnd pin")
u1["PA0"].etype("bidi", fmax_hz=100e3, assume="updi")
b.wire(j2[1], u1["PA0"], name="UPDI"); v33.tie(j2[2]); gnd.tie(j2[3])
j2.on_edge("right")

led = b.part("D1", lcsc="C2286", value="LED green")
r3 = b.part("R3", lcsc="C25744", value="1k")
u1["PA7"].etype("output", fmax_hz=0.0, assume="led gpio")
r3[1].etype("passive", fmax_hz=0.0, assume="led resistor")
r3[2].etype("passive", fmax_hz=0.0, assume="led resistor")
led["A"].etype("passive", fmax_hz=0.0, assume="led anode")
led["K"].etype("passive", fmax_hz=0.0, assume="led cathode")
b.wire(u1["PA7"], r3[1], name="LED"); b.wire(r3[2], led["A"]); gnd.tie(led["K"])

for pin in ("PA1", "PA2", "PA3", "PB0", "PB1"):
    u1[pin].unused()

print(b.check().summary())                 # pure: pins resolve, nets named

# --- block 4: place request (a block never produces copper)
pl = pcb.place(b, seed=1)                  # request: cached by content hash
if pl.ready:                               # hit: poses + crossings from the stored run
    print(pl.summary())                    # "placed by field placer run 3: 0 overlaps, 2 crossings"
    rt = pcb.route(b, placement=pl, loop=2)
    if rt.ready:
        print(rt.summary())                # "12/12 routed, 0 DRC errors, 4 vias"
```

Refdes inside a module are `<prefix>.<local>` (`U2.U`, `U2.CIN`);
internal nets are `<prefix>/<name>`; ports are the caller's `Net`
objects, so no binding table exists — the ports *are* the arguments.
`b.use` refuses a prefix already used and a module that touches a net it
was not handed (the net-namespacing invariant, checked at build time).

## Q2 — where does place/route run

Ruled (Reto 2026-10-22, option c): place and route are **requests**, not
block work. A block never produces copper; it records a request keyed by
the board content hash + params + placer/router `CODE_VERSION`, and the
existing `pcb_place`/`pcb_route` jobs run on the worker lane. Solving
inside a block was rejected: a nano place+route is ~2.5 min per seed
(8 seeds 20:02, pcb-platform thread; a `loop=2` route is bounded at
`budget_s=600`) against a block wall of 10 s (requestable to ~60, R4);
nothing would cache across edits or agents; and copper entering through a
block would bypass the never-worse guard (gr480010), judged mutation,
`last_route`, the lock columns (`fixed_side`, `group_fixed`, `room_id`)
and recorded DRC runs, with the library's solver pinned to a wheel
version that drifts from the server's DRC. An unchanged netlist with a
changed constraint re-runs; an unchanged board after an edit elsewhere
hits. Slice 2 builds it, as relax requests work for structures (request
→ job → cached result read back by a later block).

What a block's request returns: `{op, key, status: hit|queued|running|
failed, job, run}`; on `hit` the read MCP serves `get(kind='pcb',
id=slug, view='placement')` / `view='route-status'` whose `revision`
equals the request key, so `Placement.ready` is one read. A `queued` echo
names the job and the wait (`place: queued job j…; ask again in ~3 min`);
inspection via the MCP views meanwhile. A `failed` request carries the
job's failure class (`non-convergence` from `pcb_route`) and `next=`
naming the constraint (the `loop`/`expansion_caps` knobs are deleted —
accepted-and-ignored since 2026-10-25).

## Q3 — the library without a DB

The same `precis-geom` wheel (`precis-geom[pcb-kernel]` adds the pure
half of `precis.pcb`: ir, optimize, field, cost, realize, planes, drc,
gerber, svg, export; anything importing `precis.store` is split out,
asserted by the import-graph test) runs DB-free on a laptop or in CI for
goldens (§Goldens) and as the job lane's code; it is not a design loop,
and its place/route output is a preview, never board state. With no
`part_footprints` cache, parts with `lcsc=` and no authored footprint fall
to the `landpattern` synthesized bound (flagged, as in the fixtures); a
design export embeds the cached footprint rows the board used, so an
exported board rebuilds with real pads.

## Commit (trusted side)

`commit(ref, snapshot) → version` is the importer renamed in role: the
only writer of the pcb tables (R2a), reached by `keep:'auto'` on a clean
block and by `mode='keep'` on a candidate. The bounded run intake
`src/precis/pcb/workspace.py::run_batch` (shared by `precis pcb
import-run` and sandbox harvest, landed under the script workflow) is the
lane's door onto the same validator; do not add a second envelope parser.
Remaining here: changed-board replacement/retirement, planes, requests
and W5 refusals.

`src/precis/pcb/workspace_import.py` implements `commit` for `kind:
'pcb'`:

- **Snapshot** `{schema: 1, kind: 'pcb', board: <batch dict>, requests:
  [{op: 'place'|'route', key, params}], notes?}` — a jsonb
  `design_checkpoints` row keyed by natural keys, `additionalProperties:
  false`; a bad key refuses the block with `next=`. `board` is
  validated by the same code the batch put used (`_pcb_apply` and its
  argument checks: `max_travel_mm`, `_reject_non_electrical`, feature
  shapes) — one validator, moved out of the handler into this module so
  the retired op path and `commit` refuse identically. The validator
  also refuses tracks/vias ("copper enters through requests").
- **Apply** inside one transaction: `pcb_apply` (the board ref
  already exists, created by `put(kind='pcb', …)`) → `pcb_set_board_profile`
  → `pcb_upsert_net_classes` → `pcb_upsert_roles` → stackup/preset/sides
  → planes → `_legalize_generator_seeds` → the judged-mutation gate
  (`_judged_mutation`: a new pad/authored-copper DRC error refuses the
  commit, router copper on changed nets is ripped and listed in the
  echo). On any failure the transaction rolls back and head is unchanged.
- **Replace.** Before apply, `commit` diffs the snapshot against head:
  components (by refdes), nets (by name), connections (by `(net, refdes,
  pin)`), features (by `(ftype, geom sha)`), footprints, net classes,
  roles, generators, measures. Absent → retire (`retired_at`; new store
  ops `pcb_retire_components`/`_nets`/`_netconns`/`_features`/
  `_measures`, the generator-retire shape); copper and routes of a
  retired net retire with it; a retired part's fixed copper retires.
  Present with changed pins → the component type is re-created, the
  instance keeps its pose and lock columns. There are no foreign rows
  (R2a): a block starts from head's whole IR, the snapshot is the whole
  board, and `commit` retires only what the block removed — an
  `import_epro` board's rows and `op='move'` poses survive a block that
  does not touch them. `meta.workspace` and the `import_epro` tags are
  provenance only. An empty `board` = clear, undoable through the retire
  trail.
- **Provenance** (R8). `pcb_boards.meta.generated = {generator:
  'workspace', workspace, version, run_job (lane commits only),
  requested_by}`; every made row `meta.workspace` and `meta.frame` =
  (version, block index, code anchors), the anchors resolving through the
  code-anchor mechanism (gr595549, `an<N>` handles). Connections have no
  meta column — their anchors live in the trace's `made` index keyed
  `(net, refdes, pin)`. A kept version is reproducible by its snapshot,
  not by a re-run.
- **Requests.** Each `requests[]` entry: key = `sha(canonical board dict,
  params, job CODE_VERSION)`. Table `pcb_requests(ref_id, op, key, job_id,
  status, created_at)` (migration). Hit → echo `place: hit (run 3)`;
  miss → enqueue the existing job type with `params = {pcb_ref_id, ...}`
  and the key stamped in job meta; the job's completion writes
  `status`. Stale rows (board rows changed under the key) are refused by
  the job's own placement-DRC gate, not by the table.
- **Caps**: parts per board, nets, connections, bytes (the harvest cap);
  refused before any write.

## Construction traces for boards

Remaining integration (gr521596): the recorder supplies Part/Net maker IDs
and creation counts, but these do not yet reach a stored board. This needs
the following pipeline before the views below can resolve creators:

- Persist the block's trace on the version `commit` records (R8:
  version, block index, code anchors) for the pcb kind.
  `src/precis_geom/pcb.py::write_manifest` writes the local/lane artifacts;
  the existing `src/precis_se/workspace_import.py::import_workspace_run` is
  the SE storage pattern and its `_vet_manifest` requires SE blocks.
- Export a separate entity-to-frame mapping alongside `Board.to_batch`,
  whose authored payload excludes maker stamps; persist component, instance
  and net bindings to their version (`src/precis/store/_pcb_ops.py::PcbMixin._pcb_insert_component`
  currently filters component metadata). Extend capture/bindings to
  connections, holes and the other entity types in the table below.
- Bind the board to its versions' traces and resolve it using the pattern in
  `src/precis_se/persist.py::trace_doc_resolver`; then reuse
  `src/precis_se/traceview.py::Trace`, `card` and `diff` for the four asks.
- Choose backward-compatible routing: `src/precis/handlers/pcb.py::PcbHandler._render_view`
  already uses `view='trace', args={'net': ...}` for connectivity through
  `src/precis/pcb/eyes.py::trace`. The proposed creator lookup collides with that call.
- Exercise commit → persistence → handler lookup in integration tests,
  including collapsed frames and earlier versions.

The frame object, keys, capture and storage are structures §Construction
traces, with R8's (version, block index, code anchors) in place of raw
stack frames. What differs is the entity set and the solver frames.

| entity | creator frame stored | modifier frames |
|---|---|---|
| component / instance | `pcb_components.meta.frame`, `pcb_instances.meta.frame` (the `b.part` or `b.use` frame) | `place(...)` service frames, `op` frames from `op='move'` |
| net | `pcb_nets.meta.frame` | `route(...)` service frames, `op='rip'` |
| connection | trace `made` index | — |
| feature, measure, footprint, net class, role, generator | `meta.frame` | — |
| copper row | `pcb_copper.meta.frame` = the `route` service frame | — |

**Solver frames.** A `pcb_place` job appends a `service` frame
`place(field, seed=1, iters=2000, run=N)` to the board's trace with
`touched` = instances whose pose changed, `args` = the `PolishResult`
reason and `e_before/e_after`; a `pcb_route` job appends `route(vector,
loop=k, round=j)` touching the nets it realized, `made` = copper rows;
`op='move'`/`op='rip'` append `op` frames (`tool_ledger` call id as
`line`). So "who placed U1 here" reads `v3 b3 an52 b.part(U1) ·
placed by field placer run 3 · moved by op c-1234`.

**Views** (`get(kind='pcb', id=slug, view='trace', args=...)`):

| ask | args | rendering |
|---|---|---|
| part/net → who made it | `{'refdes': 'U2.CIN'}` / `{'net': 'U2/VOUT'}` | root→leaf frame lines (`v2 b2 an41 b.use(ldo_3v3, prefix=U2) → ldo_3v3 v2 b2 an38 b.part(CIN)`; illustration, builder's format: `v<version> b<block index>` plus the code-anchor handle `an<N>` when one exists, never a line number), then modifier lines in event order |
| frame → what it made | `{'frame': key}` | the frame line, counts per entity (parts, nets, connections, measures, copper), selector `frame:<key>` |
| set → LCA | `{'refdes': [...]}` | the most specific frame covering the set, coverage, strays |
| version vs version | `{'against': version}` | frames added/removed/changed by key |

**Findings close the loop.** `DrcFinding.objects` already names refdes
and nets; `view='check'`/`view='drc'` gain a `frame` column rendering the
nearest user frame of the first object's creator —
`drc.courtyard_overlap  U2.CIN↔U2.COUT  v2 b2 an41 b.use(ldo_3v3) → v2 b2 an44 b.near` —
so the agent knows the block and line. `frame:<key>` joins the pcb selector set
(`#REFDES`, `@NET`) for `view='svg'` highlight and `view='placement'`.

**Python-kind join.** A module graduated to `pcbmods` (R7) resolves as a
python symbol, as for se (structures slice 2p); `view='placed'` on a
python id lists the boards built from it with parts/nets counts per
occurrence.

## What replaces which MCP ops (W5)

Ruled pattern (structures §MCP surface): the slice that makes `exec`
usable removes the construction ops in the same change — refusal with
`next=` naming `exec`, skill text removed; stored rows keep
working. Applied to `handlers/pcb.py` (`_OPS` and the batch keys):

| today | after slice 1 |
|---|---|
| batch put keys `components`, `nets`, `connections`, `net_classes`, `roles`, `footprints`, `features`, `measures`, `generators`, `meta`, `profile` | refused for agents (`next=` naming `exec`); `pcb_apply` stays as `commit`'s and `import_epro`'s internal path |
| `op='class_rules'`, `'stackup'`, `'plane_net'`, `'profile'`, `'pins'`, `'pin_rename'`, `'pin_remove'`, `'corner_nuts'` | one-line blocks over the `pcb` builder calls (§Q1 table) |
| never-built `op='instantiate_block'` (meta-blocks), `op='remove'`/`'disconnect'` (engine plan #7) | `b.use(module)`; a block that drops the part or net |
| `op='place'`, `op='route'` | stay as ops and become `pcb.place`/`pcb.route` requests (slice 2); same job types |
| `op='move'`, `'rip'`, `'pin_side'` | stay — solver-state edits, each writes an `op` frame; a block's `part.at(..., fixed=)` wins for the parts it pins, everything else keeps the op's pose |
| `op='footprint'`, `'datasheets'`, `'import_epro'`, `'order'`, `'check'`, `'phase'`, `'review'` | stay — intake, inspection, fab |
| all `get` views | stay; `view='trace'` added |

A board made by `import_epro` takes blocks as is: the block starts from
its whole head IR and `commit` retires only what a block removes
(§Commit).

## Modules — schematic blocks now, placed modules later

**Level 1 (code).** `@pcb.module` = [pcb-meta-blocks](pcb-meta-blocks.md)
schematic block: parts + internal nets + ports (the arguments) +
measures + provenance (the frame carries (version, block index, code
anchors) and the module's qualname). Reusable across boards once graduated (R7, `scripts/ws-promote`,
structures §Packaging): `from pcbmods.usb import usb_c_sink`. The block
content hash the meta-blocks spec keys checklist verdicts on is the
module frame's `args` digest + the promoted wheel version. Parametric
modules are free (they are functions); nesting is free (frames nest).

**Level 2 (placed-module cache, later slice).** A module placed and
routed once in some board can be stamped rigid: `commit` records,
per `(module qualname, wheel version, params sha, stackup preset,
profile)`, the relative poses of its parts and the interior copper —
the PCB analogue of structures §Relaxed units. `b.use(ldo_3v3, ...,
placed=True)` asks for it: a hit emits the parts as one rigid group
(`group` + `group_offset`, the instance-meta keys the IR already reads,
`group_fixed`) and the interior copper as fixed copper rows under the
module's nets; a miss emits the plain module and queues nothing (the
cache fills from boards that routed cleanly, `0 DRC` on the module's
nets). The rule envelope (stackup/layer count/clearance rules) is in the
key, so a mismatch is a miss, never a rescale. Not before slice 3.

**Hierarchical routing — seed, then a global pass (slice 3; Reto
2026-10-22).** A stamped module is a seed, not an answer — the PCB
analogue of structures §Relaxed units: a good starting point,
re-optimised in context.

1. **Module library.** The level-2 cache entries, promoted with their
   module (`pcbmods`, `ws-promote`): pre-placed, pre-routed chunks keyed
   on module/version/params/stackup/profile. A library hit is the
   module's seed; a miss seeds from a solo place+route of that module
   (below) and fills the library when it routes clean.
2. **Hierarchical routing as the seed.** `b.use` frames nest, so the
   board is a tree of modules. Bottom-up, per frame: a leaf module is
   placed and routed on its own parts and internal nets (library hit,
   else `pcb_place`/`pcb_route` scoped to the frame, ports as fixed
   pads at the frame's hull); the result is stamped rigid (`group`/
   `group_offset`/`group_fixed`, interior copper as fixed rows); the
   parent frame then places its modules as blocks among its own parts
   and routes the nets between them (ports and the frame's own nets)
   around the stamped copper. Same two job types, one run per frame,
   inner frames first; each run appends its `service` frame with
   `scope = frame key`.
3. **Mandatory global refinement pass, allowed to break module
   boundaries.** Hierarchy alone loses global optimisation. Concretely:
   a via inside a module is fixed copper, so it cannot move to make
   room for an inter-module net that has to thread past it; a module
   cannot shift a millimetre to shorten a bus into its neighbour; a
   port escape routed against the module's own hull ignores which side
   the port net arrives from. So after the inter-module route, one pass
   with the boundaries dissolved: `group_fixed` released to a leash
   (`max_travel_mm`, small moves of modules and of parts inside them —
   a placement polish pass with the leash as the bound), interior
   copper demoted from fixed to router copper, then rip-up and reroute
   of every net crossing a block edge plus every interior net the
   crossing nets displaced (`pcb_route` negotiation), under
   the never-worse guard: the pass replaces the seed only if the
   unrouted count and DRC errors do not rise. The pass is not a knob:
   `pcb.route(b, hierarchical=True)` is seed + pass, and the echo
   reports both stages (`seed 11/12 routed · 38 vias · global 12/12 ·
   31 vias · 2 parts moved · 4 nets rerouted across edges`). The
   per-frame stamps stay in the library untouched; the pass edits the
   board only.

## DRC and inspection stay in MCP

Nothing in `drc.py`/`check_run.py`/`eyes.py` moves. The library's
`b.check()` is the structural subset only (pure functions over the batch
dict: pin resolution, net naming, NOT-CHECKED lists). `op='check'`
records runs; `view='drc'` reads; the pre-route gate and the judged
mutation keep refusing. Two additions: the `frame` column (above) and
`view='check'` echoing the version that last changed the board
(`revision` = version), so a check after a keep says what it checked.

## Goldens in CI

A golden is a kept version: a checkpoint snapshot of a reference board,
compared by the IR diff (the `diff(head, snapshot)` contract `commit`
uses), not a text diff. `examples/boards/<name>/` holds each reference
board's block list (the landed slice 0 `main.py` examples convert in
slice 0g); `scripts/test` regenerates the snapshot by running the list
through the DB-free library, no container (the wheel is importable in the
test env), and diffs it against the stored golden:

- **Tier 1, always:** snapshot diff empty (canonical JSON sha +
  counts), `b.check()` golden, trace key set golden. For the converted
  fixtures the batch dict is asserted byte-equal to the old
  `build_design()` output, so the router regression fixture
  (`nano_usb_pd_testboard.py`, "stays untouched") is preserved as a
  golden rather than as hand-written dicts.
- **Tier 2, selected (`scripts/impacted-tests` when `src/precis/pcb/**`
  changes):** place + route through the `[pcb-kernel]` library at seed 1
  on `sanity_board` (hard: every net routed, 0 DRC errors — the ruled
  absolute target), nano and esp32c3 (measured and printed, not floored —
  the existing test policy, Reto 2026-10-19). A placer or router change
  that moves a measured number shows up in the test output naming the
  board and the frame.

CI therefore checks actual boards end to end; the fixture dicts stop
being a second netlist format.

## Migration

- **Boards made under the script workflow** (slices 0–4 landed) carry
  their rows as-is; there is no script to migrate. Head is the stored
  relational state, the first block loads it through the `Board` loaders,
  and later commits replace/retire against it. Their stored `generated`
  provenance (`script_sha`) stays as history; new versions carry R8
  provenance.
- **pcb-se binding** ([pcb-se-binding](pcb-se-binding.md)): unchanged —
  `set_binding(kind='pcb', design=<slug>)` by slug, resolved at read
  time, `mechanical_profile` the only mm→m crossing. A block builds one
  design, `pcb:<slug>` or `se:<slug>`, never both. An se block reads the
  bound board's envelope through the `precis` proxy (R3a;
  `get(kind='pcb', view='mechanical')`, `pcb` is in
  `READ_KIND_ALLOWLIST`), so a bracket block can size itself from the
  board. `pcb_height_proposed` and the hole ports are untouched.
- **Dogfood boards.** `nano-usb-pd-test` / r2 → `examples/boards/nano_usb_pd{,_r2}/`
  (fixtures become goldens); `esp32c3_reference.json`,
  `motor_power_reference.json`, `nano_oc_switch.json` → the same, built
  from the JSON by a one-off converter that emits `pcb` calls (kept as a
  script, not a runtime path); `ewod-dogfood-6` replay stays relational
  (`snapshot.py`, never re-expanded) and `ewod-dogfood-2/-4` in prod get
  one block, `b.generate('ewod_pad_array', ...)` plus the authored
  footprints — its commit must be a no-op diff against the stored
  expansion (same generator version and params);
  `usbc-attiny-dogfood-b` (prod, no fixture) is rebuilt by the §Q1
  blocks on a fork slug and compared through `view='check'` against the
  live board before cut-over.
- **Skills.** `precis-pcb-help` loses the batch-put and construction-op
  sections, gains the exec loop pointer; `precis-pcb-workspace-help`
  (new) carries the §Q1 table, the example, the request semantics and
  the trace asks; `precis-pcb-route-help` keeps `place`/`route`/`move`/
  `rip` and adds the request echo lines; `precis-workspace-help` lists
  `pcb:` as a design kind.

## Slices

| # | delivers | extends | acceptance | size |
|---|---|---|---|---|
| 0 | **Builder + goldens, no container, no DB.** ·landed 2026-10-23 (g4-pcb-ws0)· *(landed under the script workflow; provenance now per R8)* `precis_geom.pcb` (`Board`, `Net`, `Part`, `Pin`, `Footprint`, `module`/`use`, measures, features, `to_batch`, `check`, the manifest writer), `@traced` on every builder call (depends on structures slice 2 `precis_geom.trace`), `examples/boards/` for nano, nano_r2, esp32c3, sanity; tier-1 goldens | `packaging/geom/`, `tests/workspaces/` | `to_batch()` of the converted nano fixture is byte-equal to `build_design()`; `b.use` refuses a reused prefix and an un-handed net; trace keys stable across two builds; `b.check()` on the nano board lists the same NOT-CHECKED parts the fixture's `meta.synthesized_footprint_refdes` names | M, 1–2 builds |
| 0b | ·landed 2026-10-23 (pcb-ws0b)· *(landed under the script workflow; provenance now per R8)* **Board 3-D shape in the container.** The board's 3-D shape — outline, per-part heights and courtyards, mounting holes, keepouts — is exposed as data and as a callable inside the workspace container, so a script can check the board against se parts or an enclosure and argue the fit. Carries the moved pcb-se-binding "Follow-up v2" remainder (Reto 2026-10-23): per-part prisms keep the `origin: proposed` honesty flag, holes carry position + diameter (the subtraction question — derived child blocks vs a compound envelope — is carried, shared with spec `me630088`). Depends on slice 0. | `precis_geom/pcb` shape surface, `precis/pcb/export.py` `mechanical_profile` + `precis_se/board.py` reused as the one derivation | a workspace script on a fixture board reads the 3-D shape — outline polygon, one prism per part with height + origin, hole centres/diameters, keepouts — matching `mechanical_profile`, and the callable answers a fit check against an se part or enclosure | M |
| 1 | **`commit` + replace + retirements (W5).** The keep path for exec candidates (Amendment 2026-11-08); `pcb/workspace_import.py`, `kind: 'pcb'` dispatch in the `commit` path, retire ops for workspace-owned rows, `meta.workspace`/`meta.frame`, `generated` on the board, provenance on the version per R8 (version, block index, code anchors); batch construction keys and the eight construction ops refuse with `next=` naming `exec`; `precis-pcb-help` sections removed | `store/_pcb_ops.py`, `handlers/pcb.py`, `se workspace_import` dispatch, skills | A block that removes `c3`: `commit` retires `C3`, its dependent netconns and its router copper, `restore` brings them back; a block that changes nothing produces no new version and no candidate (zero rows touched, echo says so); `put(kind='pcb', args={'components': ...})` refuses naming `exec`; an `import_epro` board's rows survive a block that does not touch them, and a block that removes one retires it; `mode='keep'` on a candidate produced by a block imports through this path (`commit`) and `revert` deletes the checkpoint row and touches no other | L, 2–3 builds; after structures slice 1 |
| 0g | **Goldens as block lists.** Convert `examples/boards/<name>/main.py` to the board's block list, run through the DB-free library in CI (§Goldens) | `examples/boards/`, `tests/workspaces/` | CI regenerates each reference board's snapshot from its block list and the tier-1 goldens (§Goldens) still pass unchanged | S; after 1x |
| 1x | **Exec primitive for boards** (Amendment 2026-11-08). Shares the worker, lint/audit layers, `precis` proxy and version machinery of structure-workspaces slice 1; adds `Board` loaders from the stored snapshot (inverse of `to_batch`), `b` in scope, the board snapshot (jsonb, natural keys) as the candidate's `design_checkpoints` row, `commit` (slice 1's replace/retire), `keep:'auto'`/`'candidate'` and list batching for boards, candidate-scoped `view='check'`/`drc`/`placement`; the validator refuses copper. Stepping back past a keep is out of scope (gr598376) | structure slice 1 (worker, versions, keep/revert), `precis_geom.pcb` (`Board`, `to_batch`), `handlers/pcb.py` views, slice 1 `commit` | A block that adds one part and one net to the nano fixture commits in one call under the default `keep:'auto'` and returns the diff + new version; under `keep:'candidate'` `view='check'` on the candidate lists them and `keep` lands it; a block that writes tracks/vias is refused by the validator ("copper enters through requests"); head unchanged until commit; the head-hash test over the pcb tables (hash before a block that adds, moves and deletes, unchanged after exec in candidate mode and after `revert`); a list of three blocks where the third raises leaves two versions and no candidate; a version is recorded on every keep; a block importing `os` is refused by the lint before any fork | M, 1 build; after structure slice 1 and pcb slice 1 |
| 2 | **Services.** `requests[]`, `pcb_requests` migration, dispatch to `pcb_place`/`pcb_route`, request key with `CODE_VERSION`, `Placement`/`Routing` readback via the read MCP (`revision` on `view='placement'`/`route-status`), `service` frames from both jobs, `op` frames on `move`/`rip` | `workers/job_types/pcb_place.py`, `pcb_route.py`, `handlers/pcb.py` views | A block's first place request queues, echo names the job; after it lands the same request in a later block reports `hit` and `pl.ready` is true with no second job; changing one measure re-queues, changing a `note` does not; the board's trace shows `place(field, seed=1, run=N)` touching exactly the moved instances; `op='move'` adds an `op` frame | M, 2 builds |
| 2t | **Trace views.** `view='trace'` for pcb (refdes/net/frame/set/against), `frame` column on `view='check'`/`drc`, `frame:<key>` selector in `view='svg'`/`placement`, python-kind join for graduated pcb modules | `handlers/pcb.py`, `drc.py` render, `handlers/python.py` | A courtyard finding on `U2.CIN` names `v2 b2 an41 b.use(ldo_3v3)`; LCA of `U2.*` is the `b.use` frame at 100 %; a 10-frame trace renders ≤ 200 tokens | M, 1 build |
| 3 | **Modules promoted + placed-module cache + hierarchical routing.** `pcbmods` promotion via `ws-promote`, module content hash, `placed=True` → rigid group + interior fixed copper from a cache keyed on module/version/params/stackup/profile, filled from clean-routed boards; `pcb.route(b, hierarchical=True)` = per-frame bottom-up place+route (library hit or scoped job), block placement of modules, inter-module route, then the mandatory global pass (leashed `group_fixed`, interior copper demoted, rip-up/reroute across block edges; §Modules); `route_bench.py` gains a flat-vs-hierarchical mode | `workspace_import.py`, `pcb_requests`, `generators`-style ledger table, `workers/job_types/pcb_place.py`/`pcb_route.py` (frame scope), `tests/fixtures/pcb/route_bench.py` | A board using the promoted `ldo_3v3` twice gets two rigid groups with identical relative poses and interior copper; a 4-layer stamp into a 2-layer board is a miss, not a rescale; the stamped board routes the port escapes only. Hierarchical: the seed stage touches only frame-scoped nets per run and the trace shows one `route(vector, scope=<frame>)` per module; the global pass runs unconditionally and its echo names the rerouted edge-crossing nets and moved parts; a seed whose pass would raise unrouted or DRC errors keeps the seed and says so. Comparison on esp32c3 (`bench_esp32c3`, seeds 1–5): flat `pcb_route` vs hierarchical seed + pass, side by side per seed — unrouted count, total track length (mm), via count, wall time (s); unrouted after the pass ≤ flat on every seed (hard), the other three measured and printed (the esp32c3 policy, §Goldens); on at least one seed the pass changes something the seed could not (a via moved or a module shifted), else the pass is a no-op and the row says so | M–L, 2–3 builds |
| 4 | ·landed 2026-10-24 (pcb-ws4)· *(landed under the script workflow; provenance now per R8; survives as the DB-free library run, §Q3)* **Offline full program.** `precis-geom[pcb-kernel]` extra, `session.py` store split, `pcb.local.place/route/drc/gerbers`, `main.py --place --route`, tier-2 CI goldens on sanity (hard) + nano/esp32c3 (measured) | `packaging/geom/pyproject.toml`, `scripts/impacted-tests`, `tests/workspaces/` | `uv run main.py --place --route` on a laptop with no network writes gerbers for the §Q1 board; sanity routes 100 % with 0 DRC in CI; a `vroute` change that alters nano's routed count prints the delta naming the board | M, 2 builds |
| 5 | **Dogfood migration + skills.** A generator-recipe block on `ewod-dogfood-2/-4` (no-op first commit), `usbc-attiny-dogfood-b` rebuilt as a fork by blocks and graduated modules promoted, `precis-pcb-workspace-help`, pointer lines in the pcb skills and `precis-workspace-help`, glossary (pcb workspace, request, placed module); **Amendment 2026-11-08 (R9): `precis-pcb-workspace-help` teaches the exec loop (look → one block → check → keep or revert), op vs block, library before loops, the anti-patterns and the graduation path** | skills, the repo-dev vocabulary taxonomy (tn559460), prod boards | The EWOD boards' first block touches zero rows (empty snapshot diff); the attiny fork's `view='check'` matches the live board's before cut-over; no skill names a retired op | S–M, 1 build |

**Carried question — hole subtraction (0b).** The shape exposes mounting
holes as data (centre, diameter, `through_mm`) and the fit check warns
when a solid covers one; nothing subtracts a hole from any solid. When
holes need to subtract for real, the two candidate shapes are derived
child blocks (one per hole with its bore port — itemised, nameable, no
kernel change) vs a compound envelope (one solid with real bores —
needs the compound-envelope support se does not have yet). Shared with
spec `me630088`;
deliberately not decided here.

Later, named: layout-level block library search (`tags=['module']` for
pcb), parametric generator authoring in the wheel, `keepout` binding
(its own item), mate-aware `on_edge` with connector geometry
([pcb-pinout-view-and-connector-intake](pcb-pinout-view-and-connector-intake.md)).

## Risks

- **Retiring the batch put strands in-flight boards.** Every prod board
  authored by put keeps its rows; only *new* construction needs an exec
  block. Slice 1 ships the refusals in the same change (W5).
- **Request-key churn.** `CODE_VERSION` in the key re-queues every
  request on a placer bump — correct (no silent reuse), and the echo
  says `miss: placer version 12 → 13`.
- **Catalog access in a block.** Footprint/datasheet pulls need the
  network a block lacks; `commit` queues them, blocks see NOT-CHECKED
  until the pull lands. Same shape as a relax miss.
- **Two placers of record.** A laptop/CI place run and a cluster
  `pcb.place` request can differ by wheel version. Only the cluster
  result is board state; the DB-free result is a preview. The echo and
  the skill say so.
- **Module namespacing vs today's flat refdes.** `U2.CIN` is a new
  refdes shape; `argue.py`'s handle grammar (`U1`, `U1.3` = pin) collides
  on the dot. Resolution: module separator is `:` in handles
  (`U2:CIN.1`) and `.` in Python; the builder emits the handle form into
  `refdes`. Decided here; the grammar test pins it.

## Open questions for Reto (with recommendations)

**Ruled (Reto 2026-10-22): all six approved as recommended.**

Ruled 2026-11-08: see Amendment — exec blocks against head; head is the
source; script-as-source reversed.

1. **Retire the batch construction put for agents in slice 1, W5-style,
   or keep it until the first prod board is block-built?** Rec:
   retire in slice 1 (the ruling's reasoning holds — dev-only users, no
   wrapper phase); `import_epro` and `commit` keep the internal path.
2. **`op='move'` on block-built boards.** Rec: keep (poses are solver
   state), with an `op` frame; a block's `at(..., fixed=)` wins only for
   the parts it pins.
3. **Generator expansion: in the wheel at build time, or trusted-side at
   commit?** Rec: trusted-side (keeps the `pcb_generators` ledger and the
   replay never-re-expand rule); the wheel exposes `expand` for DB-free
   preview only.
4. **Ship the kernel in the wheel as an extra (`[pcb-kernel]`, for
   laptops and CI)?** Rec: yes, version-pinned, output is preview only;
   it is what makes CI check boards and what makes an exported board
   self-contained.
5. **Module refdes separator** (`U2:CIN` in handles). Rec: as in §Risks;
   alternative is flat prefixing (`U2_CIN`), which loses the hierarchy in
   every view.
6. **Placed-module cache (slice 3) before or after the vector router's
   remaining slices?** Rec: after — the cache fills only from boards
   that route clean, so it is worth nothing until the router hits the
   absolute target on the reference boards.
