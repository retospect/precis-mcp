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
> `vroute-final`, gr515107). `pcb.local.route`, `main.py --route` and
> `op='route'` refuse until the topo realiser lands (pcb-topo-place
> slice 5); `pcb_place` is the only compute job left. Rows below that
> prescribe the route leg are historical.

Applies [structure-workspaces](structure-workspaces.md) (sandboxed Python,
import boundary, construction traces, `pga` assembly API, relax as a
service, slice 1b git objects in Postgres) to the `pcb` kind. Same
workspace kind, same `run`, same importer shape; the design link is
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
a trace, a replace-not-extend importer, and makes placement and routing
callable from the script.

## Core design

1. **A PCB workspace is a workspace whose design is a board.** `put(kind=
   'workspace', id='attiny', args={'design': 'pcb:usbc-attiny'})`; the
   folder ref is `part-of` the pcb design; `run` stages, commits
   (`script_sha`), executes on the script lane, imports. Nothing in the
   workspace machinery is pcb-specific — the importer is.
2. **The script builds L0 + constraints, never copper.** `main.py` emits
   `out/manifest.json` `{schema: 1, kind: 'pcb', board: <batch dict>,
   requests: [...]}` plus `out/trace.json`. `board` is byte-for-byte the
   `pcb_apply` payload, so the importer is `_pcb_apply` plus a replace
   pass (§Import boundary). Poses in `board` are seeds or `fixed` pins,
   as today's component `x/y/rot/fixed`; tracks, vias, pours never enter
   through the manifest (authored fixed copper only via a generator
   recipe, as today).
3. **Place and route are services** (§Q2, option c): the script writes
   a request keyed by the board's content hash; the importer dispatches
   the existing `pcb_place`/`pcb_route` job types or answers from cache;
   the next run reads the result through the read MCP. Solver output
   lands in the DB through the job path only (never-worse guard,
   judged mutation, `last_route`), never through the manifest.
4. **Replace, not extend.** Today a batch put only creates or patches
   (`_pcb_apply` keys components by refdes and nets by name; no removal
   op exists — engine-plan #7 `op='remove'` was never built). A workspace
   run owns every row it made (`meta.workspace = slug`); rows absent from
   the new manifest retire (`retired_at`, the `_pcb_generator_retire_
   expansion` precedent), poses of surviving refdes are kept (gr481705:
   changed generator params keep poses), router copper on a net whose
   membership changed is ripped (the judged-mutation rule), DRC runs
   re-key. Deleting a line is the removal path.
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
§Packaging); alias **`pcb`**. `pg` and `pga` keep their meaning; a board
script that also builds mechanics imports all three.

```python
import precis_geom as pg
import precis_geom.assembly as pga
import precis_geom.pcb as pcb
```

| object | call | emits (batch key) | notes |
|---|---|---|---|
| board | `pcb.Board(slug, *, profile='prod'|'prototype', stackup='4l-two-sided'|'4l-top-only'|'2l'|[layers], sides='both'|'top', outline=pcb.rect(w, h, corner_mm=) | [(x,y),...])` | `profile`, `meta.stackup_preset` + `placement_sides` (`op='stackup'` semantics, `STACKUP_PRESETS`), `features[outline]` | one outline; `stackup=[...]` is the `pcb_set_stackup` list verbatim |
| plane | `b.plane('In1.Cu', net=gnd, priority=)` | `op='plane_net'` rows | regions stay derived ([pcb-stackup-regions](pcb-stackup-regions.md)) |
| net | `b.net(name, *, cls=, current=, voltage=, impedance=, edge_rate=, function=, note=)` → `Net` | `nets[]` (`est_current_a`, `working_voltage_v`, …) | `domain` electrical only (`_reject_non_electrical`) |
| net class | `b.net_class(name, **rules)` | `net_classes{name: rules}` (`op='class_rules'` shape) | pair clearances via `rules['pairs']` as today |
| role | `b.role(name, voltage=)`; `part.supplies(role, pins=[...])`; `pin.client_of(role)` | `roles`, component `supplies`, pin tags | [pcb-lazy-netlist-and-checks](pcb-lazy-netlist-and-checks.md) §1 unchanged |
| part | `b.part(refdes, *, lcsc=None, label=, footprint=None, value=, height_mm=, roles=set(), pins=[...], note=)` → `Part` | `components[]` | `lcsc` → `part_lcsc`; `footprint=` names an authored `pcb.Footprint`; neither → `landpattern` synthesized bound, flagged as today |
| pin access | `part['VBUS']`, `part[1]`, `part.pins('A1','B1')` → `Pin`s | — | pad numbers map to names at import (`mapped`) |
| pin facts | `pin.etype('power_in')`, `pin.current_role('shunt')`, `pin.nc()`, `pin.unused()`, `pin.must_terminate()` | `pins[].tags`/`meta` (`op='pins'` shape, `PIN_ETYPES`, `PIN_CURRENT_ROLES`) | |
| connect | `net.tie(*pins)`; `b.wire(pin_a, pin_b, name=None)` | `connections[]` | `wire` without a name mints the free-ended-net name per §1d and records it |
| footprint | `pcb.Footprint(name, pads=[pcb.pad(pin, shape, x, y, w=, h=, poly=, drill=, role=, mask=, paste=)], pin_map=, courtyard=, centroid=)` | `footprints[]` | authored/local footprints (EWOD) |
| catalog part | `pcb.catalog(lcsc)` | — | read MCP `get(kind='part', id=, view='facts')`: pin table, package; a miss returns a stub and the importer queues the pull (as the batch put queues datasheets) |
| mechanical | `b.hole(x, y, drill=, ring=, plated=)`, `b.fiducial(x, y)`, `b.testpoint(net, x=, y=)`, `b.corner_nuts()` | `features[]` (`mounting_hole`, `fiducial`, `testpoint`; `op='corner_nuts'` shape) | |
| keepout | `b.keepout(poly, layers=[...], reason=)` | `features[keepout]` | binds once [pcb-keepout-does-not-bind](pcb-keepout-does-not-bind.md) ships; until then recorded, not enforced, and the echo says so |
| fixed | `part.at(x, y, rot=0, side='top', fixed='both'|'xy'|'rot')`, `part.leash(max_travel_mm=)` | component `x/y/rot/layer/fixed/max_travel_mm` | |
| edge | `part.on_edge('left'|'right'|'top'|'bottom', at=None)` | instance meta `edge_mate` | |
| group | `b.group(name, parts, *, rigid=True, pattern=None)` | instance meta `group/group_offset`, `pattern/pattern_instance` | |
| measure | `b.near(a, b, within_mm=)`, `b.apart(a, b, at_least_mm=)`, `b.align(a, b, axis=)`, `b.measure(metric, operands, bound=, goal=, hard=, weight=)` | `measures[]` (`MeasureSpec` operands) | only pair measures steer placement today; the rest are recorded |
| generator | `b.generate(name, generator='ewod_pad_array', **params)` | `generators[]` recipe | expansion stays trusted-side (`generators.expand`, replay rule); `pcb.generators.expand` is importable for offline preview |
| module | `@pcb.module def ldo(b, *, vin, vout, gnd): ...`; `b.use(ldo, prefix='U2', vin=vbus, vout=v33, gnd=gnd)` | prefixed `components[]`, `prefix/NET` internal nets | §Modules |
| check | `b.check()` → findings | — | pure L0 checks: pin names resolve, unknown nets, free-ended naming, unresolved parts, missing footprints NOT-CHECKED |
| shape | `b.shape()` → `BoardShape` (outline, one prism per placed part with `height_mm` + `origin`, hole centres/diameters, keepouts); `shape.fits(other)` → `FitResult` | — | slice 0b: derived data + the fit-check call; nothing enters the batch; one derivation in `precis_geom.pcb_shape` shared with `export.mechanical_profile`/`precis_se.board` |
| emit | `out.board(b)` | `out/manifest.json` + `out/trace.json` | `b.to_batch()` is the dict |
| services | `pcb.place(b, seed=, iters=, groups=)` → `Placement`; `pcb.route(b, placement=, router='vector')` → `Routing` | `requests[]` | §Q2 |

Every builder call is a traced primitive (`@traced`, structures
§Construction traces); `b.use(...)` is the module frame, the module
function's own lines are user frames.

### Worked example — USB-C + ATtiny (`main.py`)

LCSC numbers illustrative; `pcb.catalog` resolves them at build time
through the read MCP.

```python
from precis_ws import out, params
import precis_geom as pg                  # unused here; the alias set is fixed
import precis_geom.assembly as pga
import precis_geom.pcb as pcb

P = params()                              # /work/params.json, e.g. {"seed": 1}

b = pcb.Board("usbc-attiny", profile="prototype", stackup="2l",
              outline=pcb.rect(30, 20, corner_mm=2))
for x, y in ((3, 3), (27, 17)):
    b.hole(x, y, drill=2.2, ring=4.0)

gnd  = b.net("GND",  cls="ground", current=0.5, voltage=0.0)
vbus = b.net("VBUS", cls="power",  current=0.5, voltage=5.0)
v33  = b.net("3V3",  cls="power",  current=0.3, voltage=3.3)
b.net_class("usb", width_mm=0.2, clearance_mm=0.2, pairs=[["D+", "D-"]])
b.net_class("power", width_mm=0.4)

# USB-C receptacle: 5.1 k CC pull-downs make it a UFP sink.
j1 = b.part("J1", lcsc="C165948", roles={"connector"})
vbus.tie(*j1.pins("A4", "A9", "B4", "B9"))
gnd.tie(*j1.pins("A1", "A12", "B1", "B12", "SHELL"))
for i, cc in ((1, "A5"), (2, "B5")):
    r = b.part(f"R{i}", lcsc="C25905", value="5.1k")
    b.wire(j1[cc], r[1], name=f"CC{i}")
    gnd.tie(r[2])
j1.on_edge("left")
j1.at(2.0, 10.0, rot=90, fixed="rot")   # x/y free along the edge

@pcb.module
def ldo_3v3(b, *, vin, vout, gnd):
    """ME6211 LDO with its input/output caps; ports vin, vout, gnd."""
    u = b.part("U", lcsc="C82942")
    cin = b.part("CIN", lcsc="C15849", value="1u")
    cout = b.part("COUT", lcsc="C15849", value="1u")
    vin.tie(u["VIN"], u["CE"], cin[1])
    vout.tie(u["VOUT"], cout[1])
    gnd.tie(u["GND"], cin[2], cout[2])
    b.near(u, cin, within_mm=2.0)
    b.near(u, cout, within_mm=2.0)
    return u

u2 = b.use(ldo_3v3, prefix="U2", vin=vbus, vout=v33, gnd=gnd)   # U2.U, U2.CIN, U2.COUT

u1 = b.part("U1", lcsc="C168022", roles={"mcu"})            # ATtiny1616-SNR, SOIC-20
v33.tie(u1["VDD"]); gnd.tie(u1["GND"])
c3 = b.part("C3", lcsc="C1525", value="100n")
v33.tie(c3[1]); gnd.tie(c3[2])
c3["1"].current_role("shunt")
b.near(u1, c3, within_mm=1.5)

j2 = b.part("J2", lcsc="C2337", label="UPDI 1x3")            # UPDI header
b.wire(j2[1], u1["PA0"], name="UPDI"); v33.tie(j2[2]); gnd.tie(j2[3])
j2.on_edge("right")

led = b.part("D1", lcsc="C2286", value="LED green")
r3 = b.part("R3", lcsc="C25744", value="1k")
b.wire(u1["PA7"], r3[1], name="LED"); b.wire(r3[2], led["A"]); gnd.tie(led["K"])

for pin in ("PA1", "PA2", "PA3", "PB0", "PB1"):
    u1[pin].unused()

print(b.check().summary())                 # pure, offline: pins resolve, nets named
out.board(b)                               # manifest + trace

pl = pcb.place(b, seed=P.get("seed", 1))   # service: cached by content hash
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

## Q2 — where does it run

| | (a) build only; place/route stay MCP jobs | (b) one sandbox run builds, places, routes | (c) build, then place/route as services from the script |
|---|---|---|---|
| wall time per iteration | run < 10 s (structures budget: claim ≤ 2 s, warm venv, build 1–5 s, import 1–2 s); place/route enqueued separately by the agent | one run = build + anneal + realize: `optimize` ~880 moves/s, 2000 iters default, then `realize` at 60k expansions/net — nano place+route is ~2.5 min per seed (8 seeds 20:02, pcb-platform thread); a `loop=2` route is bounded at `budget_s=600` | run < 10 s; a miss queues the job and returns; the next run after the job lands reads the result |
| determinism / caching | job idem key = `session.content_hash` + params + `CODE_VERSION` (exists) | deterministic given seed, but nothing caches: every edit re-anneals; nothing is shared across agents | the request key is the board content hash + params + placer/router `CODE_VERSION`; an unchanged netlist with a changed constraint re-runs, an unchanged board after an edit elsewhere in the script hits |
| cost | zero extra | one container holds a lane slot for minutes; two sandbox hosts (W1), one live job per workspace lease | the solver runs on the worker lane as today; the sandbox slot is held for seconds |
| lane capacity | script lane: short jobs only | script lane blocked by solver time; a second workspace waits on the lease | script lane short; solver load is the existing `pcb_place`/`pcb_route` lane |
| how iteration feels to the agent | two surfaces: edit script → run → then `put(op='place')` → poll job → `view='placement'` | one surface, minutes of silence, no partial result | one surface: edit → run → the echo says `place: hit (run 3)` or `place: queued job j…; re-run in ~3 min`; inspection via MCP views meanwhile |
| fit with anneal placer + vector router | unchanged: `pcb_place` (`optimize`), `pcb_route` (`optimize` → `realize` → `vroute.route`) | the kernel ships in the wheel; results come back as authored poses and copper through the manifest — bypasses the never-worse guard (gr480010), judged mutation, `last_route`, the lock columns (`fixed_side`, `group_fixed`, `room_id`) and recorded DRC runs; the sandbox pins a wheel version that drifts from the server's DRC | unchanged job types plus a request table in front of them |
| trust boundary | the importer | copper would enter through the manifest — a second way to make copper | the importer; solver writes stay in the job path |

**Recommendation: (c), built as (a) first.** (a) is slice 1 — the
importer and the retirements — and the script can already express
everything a batch put could. (c) is slice 2: `pcb.place`/`pcb.route`
become manifest `requests` the importer resolves against a
`pcb_requests` cache or dispatches, exactly as relax requests work for
structures (request → job → cached result read back on the next run).
The reason is the row above the table: a board's placement and copper
are *solver state with a guarded write path*, not construction, and
the solver takes minutes while the loop budget is 10 s. (b) is not the
cluster loop; it is the laptop program (§Q3), and a sandbox run may
run the same local kernel for a preview inside `wall_seconds` — its
output never imports.

What a run returns for a request: `{op, key, status: hit|queued|
running|failed, job, run}`; on `hit` the read MCP serves `get(kind=
'pcb', id=slug, view='placement')` / `view='route-status'` whose
`revision` equals the request key, so `Placement.ready` is one read.
A `failed` request carries the job's failure class
(`non-convergence` from `pcb_route`) and `next=` naming the constraint
(the `loop`/`expansion_caps` knobs are deleted — accepted-and-ignored
since 2026-10-25).

## Q3 — the full program, offline

Yes, as two layers of one wheel. `precis-geom` carries the builder;
`precis-geom[pcb-kernel]` adds `precis.pcb` as it is (ir, session's pure
half, optimize, field, cost, realize, vroute, planes, drc, gerber, svg,
export — pure Python + numpy/scipy/shapely; `session.load_snapshot` and
anything importing `precis.store` split out, asserted by the import-graph
test the structures wheel already has).

`uv run main.py` on a laptop, no network, no DB:

| always (`precis-geom`) | with `[pcb-kernel]` and `--place --route` |
|---|---|
| `out/manifest.json` (the batch dict), `out/trace.json` | `out/placement.json` (poses, `PolishResult`, crossings) |
| `out/check.txt` — `b.check()`: pin resolution, unknown nets, free-ended names, parts without cached facts/footprints as NOT-CHECKED | `out/copper.json` (`RealizeResult` rows), `out/drc.txt` (`run_geometric_drc` at the profile's capability row), `out/gerbers.zip` (`gerber.py`), `out/board.svg` |
| `out/netlist.net` (`export.kicad_netlist`), `out/schematic.svg` (`schematic.py`), `out/ratsnest.svg` | `out/bom.csv`, `out/cpl.csv` |

Deterministic for a seed (`OptimizeConfig.seed` → `random.Random`;
`vroute` is work-bounded, never clock-bounded; `field` is L-BFGS-B with
no RNG). Footprints: the laptop has no `part_footprints` cache, so parts
with `lcsc=` and no authored footprint fall to the `landpattern`
synthesized bound (flagged, as in the fixtures); a design export
(`tree_to_json` `workspaces` section, structures §Code inside the design)
embeds the cached footprint rows the board used, so an exported board
rebuilds with real pads. This is what CI runs (§Goldens).

## Import boundary (trusted side)

`src/precis/pcb/workspace_import.py`, called by the workspace run's
import step when `manifest.kind == 'pcb'`:

- **Manifest** `{schema: 1, kind: 'pcb', board: <batch dict>, requests:
  [{op: 'place'|'route', key, params}], notes?}`, `additionalProperties:
  false`; a bad key is the run's failure with `next=`. `board` is
  validated by the same code the batch put used (`_pcb_apply` and its
  argument checks: `max_travel_mm`, `_reject_non_electrical`, feature
  shapes) — one validator, moved out of the handler into the importer's
  module so both the retired op path and the importer refuse identically.
- **Apply** inside one transaction: `pcb_apply` (creating the board on
  first run with `slug` from the design link) → `pcb_set_board_profile`
  → `pcb_upsert_net_classes` → `pcb_upsert_roles` → stackup/preset/sides
  → planes → `_legalize_generator_seeds` → the judged-mutation gate
  (`_judged_mutation`: a new pad/authored-copper DRC error refuses the
  run, router copper on changed nets is ripped and listed in the echo).
- **Replace.** Before apply, the importer diffs the manifest against the
  rows tagged `meta.workspace = slug`: components (by refdes), nets (by
  name), connections (by `(net, refdes, pin)`), features (by `(ftype,
  geom sha)`), footprints, net classes, roles, generators, measures.
  Absent → retire (`retired_at`; new store ops `pcb_retire_components`/
  `_nets`/`_netconns`/`_features`/`_measures`, the generator-retire
  shape); copper and routes of a retired net retire with it; a retired
  part's fixed copper retires. Present with changed pins → the component
  type is re-created, the instance keeps its pose and lock columns.
  Rows not tagged with this workspace (an `import_epro` board the
  workspace was attached to later, an `op='move'` pose) are never
  retired — the echo lists them as `foreign: 3 rows`. Empty `board` =
  clear, undoable through the retire trail.
- **Provenance.** `pcb_boards.meta.generated = {generator: 'workspace',
  workspace, run_job, script_sha, lock_sha, image_digest, params,
  requested_by}`; every made row `meta.workspace`, `meta.frame` (the
  creator frame id in the run's trace); connections have no meta column
  — their frames live in the trace's `made` index keyed `(net, refdes,
  pin)`. `regenerate` on a workspace board = re-run at `script_sha`.
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

- Add PCB manifest import and persist the run's `ws_trace`/`ws_manifest`.
  `src/precis_geom/pcb.py::write_manifest` writes local artifacts; the existing
  `src/precis_se/workspace_import.py::import_workspace_run` is the SE storage
  pattern and its `_vet_manifest` requires SE blocks.
- Export a separate entity-to-frame mapping alongside `Board.to_batch`,
  whose authored payload excludes maker stamps; persist component, instance
  and net bindings to their run (`src/precis/store/_pcb_ops.py::PcbMixin._pcb_insert_component`
  currently filters component metadata). Extend capture/bindings to
  connections, holes and the other entity types in the table below.
- Bind the board to its trace run and resolve it using the pattern in
  `src/precis_se/persist.py::trace_doc_resolver`; then reuse
  `src/precis_se/traceview.py::Trace`, `card` and `diff` for the four asks.
- Choose backward-compatible routing: `src/precis/handlers/pcb.py::PcbHandler._render_view`
  already uses `view='trace', args={'net': ...}` for connectivity through
  `src/precis/pcb/eyes.py::trace`. The proposed creator lookup collides with that call.
- Exercise import → persistence → handler lookup in integration tests,
  including collapsed frames and historical runs.

The frame object, keys, capture and storage are structures §Construction
traces, unchanged. What differs is the entity set and the solver frames.

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
`line`). So "who placed U1 here" reads `main.py:41 b.part(U1) ·
placed by field placer run 3 · moved by op c-1234`.

**Views** (`get(kind='pcb', id=slug, view='trace', args=...)`):

| ask | args | rendering |
|---|---|---|
| part/net → who made it | `{'refdes': 'U2.CIN'}` / `{'net': 'U2/VOUT'}` | root→leaf frame lines (`main.py:12 build → main.py:30 b.use(ldo_3v3, prefix=U2) → ldo_3v3 main.py:22 b.part(CIN)`), then modifier lines in event order |
| frame → what it made | `{'frame': key}` | the frame line, counts per entity (parts, nets, connections, measures, copper), selector `frame:<key>` |
| set → LCA | `{'refdes': [...]}` | the most specific frame covering the set, coverage, strays |
| run vs run | `{'against': run_job}` | frames added/removed/changed by key |

**Findings close the loop.** `DrcFinding.objects` already names refdes
and nets; `view='check'`/`view='drc'` gain a `frame` column rendering the
nearest user frame of the first object's creator —
`drc.courtyard_overlap  U2.CIN↔U2.COUT  main.py:30 b.use(ldo_3v3) → main.py:24 b.near` —
so the agent knows the line. `frame:<key>` joins the pcb selector set
(`#REFDES`, `@NET`) for `view='svg'` highlight and `view='placement'`.

**Python-kind join.** `ws-<slug>::main.ldo_3v3` resolves as for se
(structures slice 2p); `view='placed'` on a python id lists the boards
built from it with parts/nets counts per occurrence.

## What replaces which MCP ops (W5)

Ruled pattern (structures §MCP surface): the slice that makes `run`
usable removes the construction ops in the same change — refusal with
`next=` naming the workspace path, skill text removed; stored rows keep
working. Applied to `handlers/pcb.py` (`_OPS` and the batch keys):

| today | after slice 1 |
|---|---|
| batch put keys `components`, `nets`, `connections`, `net_classes`, `roles`, `footprints`, `features`, `measures`, `generators`, `meta`, `profile` | refused for agents (`next=` the workspace loop); `pcb_apply` stays as the importer's and `import_epro`'s internal path |
| `op='class_rules'`, `'stackup'`, `'plane_net'`, `'profile'`, `'pins'`, `'pin_rename'`, `'pin_remove'`, `'corner_nuts'` | `pcb` builder calls (§Q1 table) |
| never-built `op='instantiate_block'` (meta-blocks), `op='remove'`/`'disconnect'` (engine plan #7) | `b.use(module)`; delete the line |
| `op='place'`, `op='route'` | stay as ops and become `pcb.place`/`pcb.route` requests (slice 2); same job types |
| `op='move'`, `'rip'`, `'pin_side'` | stay — solver-state edits, each writes an `op` frame; a script `part.at(..., fixed=)` wins for the parts it pins, everything else keeps the op's pose |
| `op='footprint'`, `'datasheets'`, `'import_epro'`, `'order'`, `'check'`, `'phase'`, `'review'` | stay — intake, inspection, fab |
| all `get` views | stay; `view='trace'` added |

A board made by `import_epro` has no workspace; attaching one later
(`put(kind='workspace', args={'design': 'pcb:<slug>'})`) makes the next
run own only the rows it makes — foreign rows are reported, never
retired (§Import boundary).

## Modules — schematic blocks now, placed modules later

**Level 1 (code).** `@pcb.module` = [pcb-meta-blocks](pcb-meta-blocks.md)
schematic block: parts + internal nets + ports (the arguments) +
measures + provenance (the frame carries `script_sha` and the module's
qualname). Reusable across workspaces once promoted (`scripts/ws-promote`,
structures §Packaging): `from pcbmods.usb import usb_c_sink`. The block
content hash the meta-blocks spec keys checklist verdicts on is the
module frame's `args` digest + the promoted wheel version. Parametric
modules are free (they are functions); nesting is free (frames nest).

**Level 2 (placed-module cache, later slice).** A module placed and
routed once in some board can be stamped rigid: the importer records,
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

Nothing in `drc.py`/`check_run.py`/`eyes.py` moves. The script's
`b.check()` is the structural subset only (pure functions over the batch
dict: pin resolution, net naming, NOT-CHECKED lists). `op='check'`
records runs; `view='drc'` reads; the pre-route gate and the judged
mutation keep refusing. Two additions: the `frame` column (above) and
`view='check'` echoing the workspace run that last changed the board
(`revision` = run job), so a check after a run says what it checked.

## Goldens in CI

`examples/boards/<name>/main.py` per reference board, run by
`scripts/test` without a container (the wheel is importable in the test
env):

- **Tier 1, always:** `b.to_batch()` golden (canonical JSON sha +
  counts), `b.check()` golden, trace key set golden. For the converted
  fixtures the batch dict is asserted byte-equal to the old
  `build_design()` output, so the router regression fixture
  (`nano_usb_pd_testboard.py`, "stays untouched") is preserved as a
  golden rather than as hand-written dicts.
- **Tier 2, selected (`scripts/impacted-tests` when `src/precis/pcb/**`
  changes):** `--place --route` at seed 1 on `sanity_board` (hard: every
  net routed, 0 DRC errors — the ruled absolute target), nano and
  esp32c3 (measured and printed, not floored — the existing test policy,
  Reto 2026-10-19). A placer or router change that moves a measured
  number shows up in the test output naming the board and the frame.

CI therefore checks actual boards end to end; the fixture dicts stop
being a second netlist format.

## Migration

- **pcb-se binding** ([pcb-se-binding](pcb-se-binding.md)): unchanged —
  `set_binding(kind='pcb', design=<slug>)` by slug, resolved at read
  time, `mechanical_profile` the only mm→m crossing. The pcb workspace
  is `part-of` the pcb design, the se workspace `part-of` the se design;
  one workspace never builds both. An se script reads the bound board's
  envelope through the read MCP (`get(kind='pcb', view='mechanical')`,
  `pcb` is in `READ_KIND_ALLOWLIST`), so a bracket script can size
  itself from the board the pcb script built. `pcb_height_proposed` and
  the hole ports are untouched.
- **Dogfood boards.** `nano-usb-pd-test` / r2 → `examples/boards/nano_usb_pd{,_r2}/`
  (fixtures become goldens); `esp32c3_reference.json`,
  `motor_power_reference.json`, `nano_oc_switch.json` → the same, built
  from the JSON by a one-off converter that emits `pcb` calls (kept as a
  script, not a runtime path); `ewod-dogfood-6` replay stays relational
  (`snapshot.py`, never re-expanded) and `ewod-dogfood-2/-4` in prod get
  a workspace whose `main.py` is one `b.generate('ewod_pad_array', ...)`
  plus the authored footprints — the first run must be a no-op diff
  against the stored expansion (same generator version and params);
  `usbc-attiny-dogfood-b` (prod, no fixture) is rebuilt by the §Q1
  example as a fork (`target: 'fork'`) and compared through `view=
  'check'` before the workspace is attached to the live slug.
- **Skills.** `precis-pcb-help` loses the batch-put and construction-op
  sections, gains the workspace loop pointer; `precis-pcb-workspace-help`
  (new) carries the §Q1 table, the example, the request semantics and
  the trace asks; `precis-pcb-route-help` keeps `place`/`route`/`move`/
  `rip` and adds the request echo lines; `precis-workspace-help` lists
  `pcb:` as a design kind.

## Slices

| # | delivers | extends | acceptance | size |
|---|---|---|---|---|
| 0 | **Builder + goldens, no container, no DB.** ·landed 2026-10-23 (g4-pcb-ws0)· `precis_geom.pcb` (`Board`, `Net`, `Part`, `Pin`, `Footprint`, `module`/`use`, measures, features, `to_batch`, `check`, the manifest writer), `@traced` on every builder call (depends on structures slice 2 `precis_geom.trace`), `examples/boards/` for nano, nano_r2, esp32c3, sanity; tier-1 goldens | `packaging/geom/`, `tests/workspaces/` | `to_batch()` of the converted nano fixture is byte-equal to `build_design()`; `b.use` refuses a reused prefix and an un-handed net; trace keys stable across two builds; `b.check()` on the nano board lists the same NOT-CHECKED parts the fixture's `meta.synthesized_footprint_refdes` names | M, 1–2 builds |
| 0b | ·landed 2026-10-23 (pcb-ws0b)· **Board 3-D shape in the container.** The board's 3-D shape — outline, per-part heights and courtyards, mounting holes, keepouts — is exposed as data and as a callable inside the workspace container, so a script can check the board against se parts or an enclosure and argue the fit. Carries the moved pcb-se-binding "Follow-up v2" remainder (Reto 2026-10-23): per-part prisms keep the `origin: proposed` honesty flag, holes carry position + diameter (the subtraction question — derived child blocks vs a compound envelope — is carried, shared with `cross-scale-single-assembly.md`). Depends on slice 0. | `precis_geom/pcb` shape surface, `precis/pcb/export.py` `mechanical_profile` + `precis_se/board.py` reused as the one derivation | a workspace script on a fixture board reads the 3-D shape — outline polygon, one prism per part with height + origin, hole centres/diameters, keepouts — matching `mechanical_profile`, and the callable answers a fit check against an se part or enclosure | M |
| 1 | **Importer + replace + retirements (W5).** `pcb/workspace_import.py`, `kind: 'pcb'` dispatch in the workspace run import, retire ops for workspace-owned rows, `meta.workspace`/`meta.frame`, `generated` on the board, the trace stored on the run; batch construction keys and the eight construction ops refuse with `next=`; `precis-pcb-help` sections removed | `store/_pcb_ops.py`, `handlers/pcb.py`, `se workspace_import` dispatch, skills | The §Q1 example runs on the dev lane and `view='check'` on the board matches the laptop `check.txt`; deleting `c3` from the script retires `C3`, its connections and its router copper on the second run, `restore` brings them back; a re-run with no script change is a no-op (zero rows touched, echo says so); `put(kind='pcb', args={'components': ...})` refuses naming the workspace; an `import_epro` board attached later reports foreign rows and retires none | L, 2–3 builds; after structures slice 1 |
| 2 | **Services.** `requests[]`, `pcb_requests` migration, dispatch to `pcb_place`/`pcb_route`, request key with `CODE_VERSION`, `Placement`/`Routing` readback via the read MCP (`revision` on `view='placement'`/`route-status`), `service` frames from both jobs, `op` frames on `move`/`rip` | `workers/job_types/pcb_place.py`, `pcb_route.py`, `handlers/pcb.py` views | First run queues, echo names the job; after it lands the next run reports `hit` and `pl.ready` is true with no second job; changing one measure re-queues, changing a `note` does not; the board's trace shows `place(field, seed=1, run=N)` touching exactly the moved instances; `op='move'` adds an `op` frame | M, 2 builds |
| 2t | **Trace views.** `view='trace'` for pcb (refdes/net/frame/set/against), `frame` column on `view='check'`/`drc`, `frame:<key>` selector in `view='svg'`/`placement`, `ws-<slug>` python root for pcb workspaces | `handlers/pcb.py`, `drc.py` render, `handlers/python.py` | A courtyard finding on `U2.CIN` names `main.py:30 b.use(ldo_3v3)`; LCA of `U2.*` is the `b.use` frame at 100 %; a 10-frame trace renders ≤ 200 tokens | M, 1 build |
| 3 | **Modules promoted + placed-module cache + hierarchical routing.** `pcbmods` promotion via `ws-promote`, module content hash, `placed=True` → rigid group + interior fixed copper from a cache keyed on module/version/params/stackup/profile, filled from clean-routed boards; `pcb.route(b, hierarchical=True)` = per-frame bottom-up place+route (library hit or scoped job), block placement of modules, inter-module route, then the mandatory global pass (leashed `group_fixed`, interior copper demoted, rip-up/reroute across block edges; §Modules); `route_bench.py` gains a flat-vs-hierarchical mode | `workspace_import.py`, `pcb_requests`, `generators`-style ledger table, `workers/job_types/pcb_place.py`/`pcb_route.py` (frame scope), `tests/fixtures/pcb/route_bench.py` | A board using the promoted `ldo_3v3` twice gets two rigid groups with identical relative poses and interior copper; a 4-layer stamp into a 2-layer board is a miss, not a rescale; the stamped board routes the port escapes only. Hierarchical: the seed stage touches only frame-scoped nets per run and the trace shows one `route(vector, scope=<frame>)` per module; the global pass runs unconditionally and its echo names the rerouted edge-crossing nets and moved parts; a seed whose pass would raise unrouted or DRC errors keeps the seed and says so. Comparison on esp32c3 (`bench_esp32c3`, seeds 1–5): flat `pcb_route` vs hierarchical seed + pass, side by side per seed — unrouted count, total track length (mm), via count, wall time (s); unrouted after the pass ≤ flat on every seed (hard), the other three measured and printed (the esp32c3 policy, §Goldens); on at least one seed the pass changes something the seed could not (a via moved or a module shifted), else the pass is a no-op and the row says so | M–L, 2–3 builds |
| 4 | ·landed 2026-10-24 (pcb-ws4)· **Offline full program.** `precis-geom[pcb-kernel]` extra, `session.py` store split, `pcb.local.place/route/drc/gerbers`, `main.py --place --route`, tier-2 CI goldens on sanity (hard) + nano/esp32c3 (measured) | `packaging/geom/pyproject.toml`, `scripts/impacted-tests`, `tests/workspaces/` | `uv run main.py --place --route` on a laptop with no network writes gerbers for the §Q1 board; sanity routes 100 % with 0 DRC in CI; a `vroute` change that alters nano's routed count prints the delta naming the board | M, 2 builds |
| 5 | **Dogfood migration + skills.** Workspaces attached to `ewod-dogfood-2/-4` (generator recipe, no-op first run), `usbc-attiny-dogfood-b` rebuilt as a fork and promoted, `precis-pcb-workspace-help`, pointer lines in the pcb skills and `precis-workspace-help`, glossary (pcb workspace, request, placed module) | skills, `docs/glossary.md`, prod boards | The EWOD workspaces' first run touches zero rows; the attiny fork's `view='check'` matches the live board's before cut-over; no skill names a retired op | S–M, 1 build |

**Carried question — hole subtraction (0b).** The shape exposes mounting
holes as data (centre, diameter, `through_mm`) and the fit check warns
when a solid covers one; nothing subtracts a hole from any solid. When
holes need to subtract for real, the two candidate shapes are derived
child blocks (one per hole with its bore port — itemised, nameable, no
kernel change) vs a compound envelope (one solid with real bores —
needs the compound-envelope support se does not have yet). Shared with
[cross-scale-single-assembly](cross-scale-single-assembly.md);
deliberately not decided here.

Later, named: layout-level block library search (`tags=['module']` for
pcb), parametric generator authoring in the wheel, `keepout` binding
(its own item), mate-aware `on_edge` with connector geometry
([pcb-pinout-view-and-connector-intake](pcb-pinout-view-and-connector-intake.md)).

## Risks

- **Retiring the batch put strands in-flight boards.** Every prod board
  authored by put keeps its rows; only *new* construction needs a
  workspace. Slice 1 ships the attach path and the foreign-row rule
  first, the refusals in the same change (W5).
- **Request-key churn.** `CODE_VERSION` in the key re-queues every
  request on a placer bump — correct (no silent reuse), and the echo
  says `miss: placer version 12 → 13`.
- **Catalog access in the sandbox.** Footprint/datasheet pulls need the
  network the sandbox lacks; the importer queues them, the script sees
  NOT-CHECKED until the next run. Same shape as a relax miss.
- **Two placers of record.** A laptop `--place` and a cluster
  `pcb.place` can differ by wheel version. Only the cluster result is
  board state; the laptop result is a preview file. The echo and the
  skill say so.
- **Module namespacing vs today's flat refdes.** `U2.CIN` is a new
  refdes shape; `argue.py`'s handle grammar (`U1`, `U1.3` = pin) collides
  on the dot. Resolution: module separator is `:` in handles
  (`U2:CIN.1`) and `.` in Python; the builder emits the handle form into
  `refdes`. Decided here; the grammar test pins it.

## Open questions for Reto (with recommendations)

**Ruled (Reto 2026-10-22): all six approved as recommended.**

1. **Retire the batch construction put for agents in slice 1, W5-style,
   or keep it until the first prod board is workspace-built?** Rec:
   retire in slice 1 (the ruling's reasoning holds — dev-only users, no
   wrapper phase); `import_epro` and the importer keep the internal path.
2. **`op='move'` on workspace boards.** Rec: keep (poses are solver
   state), with an `op` frame; script `at(..., fixed=)` wins only for
   the parts it pins.
3. **Generator expansion: in the wheel at build time, or trusted-side at
   import?** Rec: trusted-side (keeps the `pcb_generators` ledger and the
   replay never-re-expand rule); the wheel exposes `expand` for offline
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
