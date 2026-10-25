---
status: draft
title: "pcb topo-place: fast placer with a pairwise part map and solver-side pin assignment, then topological route, anneal, corridor shove and its own realiser"
pillar: 3d-design
prio: high
model: opus
blocked-by: pcb-workspace-code
---

# pcb topo-place

Source: gr515107. Reto approved CHAT's brief as the basis for this spec on
2026-10-24: "Send it this is a grand start." Folds in gr514458, gr514459
and gr514460. Research: [pcb-topo-place-research](pcb-topo-place-research.md)
(rules R1–R15, the 4-plane model §B1–B5; cited below by rule number and
DOI).

**Build gate.** Building starts only after pcb-ws4 lands. Its
[pcb-workspace-code](pcb-workspace-code.md) slice 4 (the offline kernel,
`precis-geom[pcb-kernel]`, `pcb.local`) is on main; the pcb-ws4 tree still
held unmerged commits on 2026-10-24. The gate is that tree landing, not
the item's later slices (1, 2, 2t, 3, 5).

**Dogfood target (Reto 2026-10-25).** Once place, topo and the realiser
work end to end, every slice runs on the standing real board and its
numbers are posted ("yes dogfood all the time when it starts kinda
working").
No pcb exists on prod under the literal name `epro2`; the standing
.epro2-imported boards are `heater-base-test` (pb460559, from
`heaterBaseTest.epro2`) and `taz15` (pb482131 "copper stripped, routing
target", plus the refresh copies pb492863 and pb497132, from
`taz15.epro2`) — those are the dogfood set until Reto names a specific
one.

## Motivation / why

Reto: "a cheap place and topo route call. We keep the pads, and
topo-route all the wires. Once we are crossing free, we do corridor
planning on the top and bottom, and shove the placement apart." Build a
graph-optimised data structure from the ratsnest and pads so this runs
fast.

What exists, and where it falls short:

| today | gap |
|---|---|
| `place.py::autoplace` anneals on straight-airwire crossings (`W_CROSS`) + length, delta-scored per moved part | centroid pins, no rotation in the crossing metric; the crossing count is a heuristic proxy (R3, Garey–Johnson 10.1137/0604033), not a routability verdict |
| `place.py::route_feasibility` splits airwires H/V and calls residual crossings `vias_estimate` | not a rule: 2-layer routability with fixed topology is list-2-colouring of the crossing graph (R6, R15.1); the certificate is an odd cycle |
| `optimize` ~880 moves/s; nano place+route ~2.5 min per seed (pcb-workspace-code §Q2) | too slow for an LLM outer loop of 3–10 rounds |
| `matching.py::solve` assigns supply/client roles by Euclidean min-cost; `pinswap.py` warm-starts radial/distance swaps | the netlist fixes every MCU pin; no "any of these pins" declaration, no crossing-aware swap, no firmware write-back |
| `topo.py` builds a free-space CDT with portal capacities (kept for `build_region_graph`; its `vroute` consumer is deleted) | per-net centreline domains for geometry; no per-layer wire order, no crossing graph, no via sites |

The research turns the missing verdicts into exact, cheap tests: boundary
interleaving (R4, Hsu 10.1109/TCAD.1983.1270041), inversions between
facing pin rows (R14), bipartiteness with forced layers (R15.1), planar
max-cut for vias at junction degree <= 3 (R9, Hadlock 10.1137/0204019) and
the cut condition on taut paths (R11, Leiserson–Maley 10.1145/22145.22153).

Relation to other pcb items: [pcb-intent-api](pcb-intent-api.md) supplies
the objective shares and constraints this consumes;
[pcb-unified-place-route](pcb-unified-place-route.md) (`field.py`) and
[pcb-place-route-datamodel](pcb-place-route-datamodel.md) (`precis.pcb.pr`)
were the smooth-energy engines (both deleted, Reto 2026-10-25; git
history); pcb-vector-router (deleted; tag vroute-final) was the geometry
engine — `vroute.py` was deleted on 2026-10-25 (tag `vroute-final`), so
the topological router is the only router and until slice 5 lands there
is no routed-copper output. topo-place is that router's front end:
poses, pin assignments, a layer per wire piece and via sites; slice 5
turns them into copper in a topoplace realiser.

## Rulings (Reto 2026-10-24)

- **R-a. The brief is the authority** for decisions, phases 0–4, the data
  structure, the pin-assignment API, the LLM interface and the order.
- **R-b. Order.** Phase 0 + pairwise map + pin assignment first ("they pay
  off before anything else exists"), then phases 1 → 2 → 3 → 4. Exact
  pieces before heuristics.
- **R-c. gr514458.** Fix the `precis_geom.pcb.local` lazy alias so `ruff
  check` passes with no noqa; "agents should lint their code too".
- **R-d. gr514459.** "code should use index, gerber should be standard
  names." Layers are addressed by index in code (0 = top … N-1 = bottom);
  `stackup=4` is the default; export emits F.Cu, In1.Cu, In2.Cu, B.Cu …;
  no custom layer names in the authoring API.
- **R-e. gr514460.** "Not DRY … It should see prod to get stuff the normal
  way. Don't overdo isolation." No second, wheel-side normaliser; scripts
  and the wheel read parts and footprints from prod read-only by the
  normal path and use the one normalisation the server uses. The run
  manifest records the ids read plus a version/sha.
- **R-f. Pin declarations.** "the place route input netlist becomes a ...
  very optional thing!"; KISS: "lock ambiguous complex things down in the
  LLM at netlist time." Pin function tables: "When established we can
  move it into the library (and should!)".

## Decisions

From the brief unless marked *spec call*.

1. **Mesh.** A constrained triangulation (CDT) of pad centres plus
   footprint outline corners, outline edges kept as constraints, stored as
   flat numpy arrays (`verts`, `tris`, adjacency) with a pad→vertex map.
   CDT as the dynamic data structure for rubber-band routing is SURF's
   (Dai–Kong–Jue–Sato 10.1109/iccad.1990.129838; Dai–Dayan–Staepelaere
   10.1145/127601.127622).
2. **Mesh library (*spec call*).** The brief's default is `triangle` if
   the dependency is acceptable, else scipy Delaunay plus constraint
   recovery. `triangle` wraps Shewchuk's Triangle, whose licence bars
   inclusion in commercial products without a separate licence, so it is
   not acceptable by default. Use `shapely.constrained_delaunay_triangles`
   (GEOS, shapely 2.1, already a core dependency and used by
   `topo.py`), mapping output triangles back to vertex ids by exact
   coordinate lookup. scipy Delaunay plus constraint recovery is the
   fallback if shapely's CDT measures too slow for the slice 2 rate.
3. **Wires carry no coordinates.** A wire is a sequence of triangle-edge
   ids on one layer. Each edge keeps, per layer, the ordered list of wires
   crossing it. Two wires on one layer cross iff their relative order
   differs between two edges both cross (a list comparison).
4. **Vias.** A via is a vertex present on all layers. precis emits through
   vias only (`realize.py::through_span`, gr495289), so each via is also an
   antipad on every plane (§B0, §B3).
5. **Layers.** F/B first; open In1/In2 only when needed. Inner layers do
   not save vias on SMD-only nets (§B5 example A: K3 costs 4 vias on 2 or
   4 layers); 3+-layer CVM is NP-complete (Chang–Du 10.1109/12.4616), the
   2-layer relaxation is exact at junction degree <= 3 (R9).
6. **Endpoints and yielding.** Pad sides fix wire ends: front-only SMD
   nets default F, back-only default B, F↔B nets have a free via location,
   TH nets are free. At a crossing the yielder is: TH net, then F↔B net
   (move its via), then SMD-only net (2 vias) — the via-cost table R15.3.
   Odd cycles get a via-point vertex. `layer_lock.py` forces are
   respected. Certificates name the nets of each odd cycle.
7. **Placement proxy.** Straight-line airwire crossings plus length,
   recomputed incrementally for the moved part only; target thousands of
   evals/s at 100 parts.
8. **Pairwise part map.** All part pairs (~5k unordered at 100 parts).
   Connectivity strength: clique weight 1/(k-1) per shared net of k
   parts. Straightforwardness: crossings between two parts = inversions
   between their facing pin orders (R14), over 4×4 relative rotations
   (×2 mirror for back-side parts). Output per pair: best relative
   rotation; over the board: mean, p95, worst pairs — used to steer the
   placer and seed rotations and clusters.
9. **Placer memory.** A light rubber band toward good prior
   configurations that decays, plus a small diverse elite archive for
   restarts so the search does not collapse onto one optimum.
10. **Cost ladder.** Proxy every move → full topo route every N moves or
    for elites (target 10–100/s at 100 parts; measure) → phases 2–3 for
    the top few. The rate of the expensive rungs is set from the measured
    correlation between proxy crossings and real topo vias.
11. **Pin assignment.** Two forms only: `uC.pin(5)` (fixed) and
    `uC.pin(any_of=[1, 4, 9], name="BTN")` (solver picks; `name` feeds the
    firmware pin map). No peripheral DSL in v1 (I2C instances, timer
    sharing, alt-function exclusivity): the LLM resolves those by fixing
    pins; revisit if the cost report shows fixed pins are expensive.
    Solver: Hungarian (`scipy.optimize.linear_sum_assignment`) on
    pin→destination distance with infeasible pairs priced out, then an
    incremental pairwise-swap 2-opt on the full proxy crossing count. A
    min-length Euclidean matching is crossing-free among its own wires.
    Destinations along one outline: sort by position = zero inversions
    (R14). Phase 2: exact, assign in the cyclic order wires exit the
    outline (R4).
12. **Pin conflicts.** Claiming one fixed pin twice under different names
    raises `PinConflict` at once, naming both claim sites (name +
    file:line); the same name again is idempotent. Flexible slots are
    checked at solve time: fixed pins leave every `any_of` set; an empty
    set or a Hall violation raises with the exact signals and pins ("BTN,
    INT2, INT3 need [1,4]: 3 signals, 2 pins").
13. **Swap groups** (resistor packs, buffer channels): plain pin groups or
    TUPLE groups (`in_i`/`out_i` move together).
14. **Write-back is mandatory.** Assignments go to the board script and a
    firmware pin map (`pins.h` / board config, keyed by `name`) and are
    listed in the summary; a swap the firmware does not know about is a
    broken board. Form: Decision 24.
15. **Requirements carry provenance.** `net.tie(..., current=pcb.Max("20mA"),
    source="ds:…")` is checkable, not a comment. Pin choice filters on
    drive rating where the library has it; an electrical check flags, for
    example, an LED with no current limit.
16. **Pin function tables** are built per design by the LLM from the
    datasheet with source chunk ids (`uC.pin_functions({...},
    source="ds:…~chunk…")`) and promoted into the component library once
    proven. Library ids: LCSC first, plus a manufacturer/MPN/package alias
    to the same record. Promotion is not in this spec.
17. **LLM is the outer loop only** (3–10 rounds). It changes the problem
    (constraints, pin freedoms, part choice, outline, locks), never drives
    moves. Inputs: the board script (parts, footprints, outline, stackup
    by index, locked parts, fixed nets plus demands); intent
    (groups/proximity, edge-bound parts, keepouts/zones, net classes,
    preferred layers, allowed freedoms: rotate, back side, pin sets); an
    objective preset plus weights; effort and seed (deterministic).
18. **Objective = pcb-intent-api shares (*spec call*).** Presets
    (`"compact"`, `"low-via"`, `"2-layer-only"`, …) are named share
    vectors over the `pcb.objective` terms, normalised at the seed
    reference (pcb-intent-api §Weights). The phase 2 inner-layer
    unidirectionality term is soft with weight 0 unless a preset or the
    script sets it, which keeps pcb-intent-api Decision 1 (no global
    per-layer default direction).
19. **Summary, one screen.** Verdict (routable on N layers); best vs
    baseline: crossings, via bracket [lower, upper], worst corridor
    overflow, area, length; run stats: best / median / p90 over N evals,
    plateau detection ("more effort won't help"), elite agreement (stable
    parts vs undetermined parts that need hints). Then **ranked edits**,
    each computed by trying it, with a predicted delta ("rotate J2 90°: −5
    crossings", "allow BTN on any interrupt pin: −4", "unlock J1 (lock
    costs ~8)", "widen U1–C7 by 0.6 mm: clears the worst corridor", "4
    layers: 0 vias saved (SMD)"). Then diagnostics with certificates: odd
    cycles by net name, worst pin-order pairs, violated cuts, conflicting
    constraints. Fields a slice cannot compute yet print `n/a (slice N)`.
20. **Runs are stored with a manifest**; drill-down views read a stored
    run: placement SVG (crossings and heatmap), pairs, corridors, elites,
    diff runA runB.
21. **Where it runs (*spec call*).** One copy of the kernel, in
    `src/precis/pcb/topoplace/` inside the `[pcb-kernel]` wheel subset.
    Script side: `pcb.local.topo_place(b, ...)` (preview files, as
    `pcb.local.place`). Board state: the existing `pcb_place` job with
    `placer='topo'`, so solver output reaches the DB only through the job
    path (pcb-workspace-code §Q2). Opt-in until the bench shows it beats
    the default placer.
22. **Determinism (*spec call*).** `seed` → one `numpy.random.Generator`;
    effort is an eval count, never wall-clock; equal inputs give
    byte-identical run files.
23. **Lint before handback (R-c).** Every slice's builder runs `ruff
    check`, `ruff format --check` and mypy on the touched files and fixes
    them before handing back; reviewers refuse a hand-back without that
    output.
24. **Pin lock file (Q19 ruling, Reto 2026-10-24).** `pins.lock.json` is
    written at end of place-and-route alongside netlist/BOM/drill/
    gerbers/`pins.h`. It is the one output that is also an input: a later
    run loads it (e.g. `Board(..., pins_lock=path)` or a freeze flag) to
    freeze slot→pin choices once firmware has shipped or the first PCB
    is cut.
25. **Parts are code objects (Q20 ruling, Reto 2026-10-24).** Parts are
    built in the script from footprint + datasheet (pins, functions,
    meta, source chunk ids), promoted into a library module when mature.
    An offline run with script-defined parts is legitimate. The manifest
    records each part's provenance (library id+sha / script-defined with
    sources / synthesized with no source) and the run summary shows it.
    Export is NOT gated on provenance (Reto: "JLC will tell us if we use
    unobtainium").
26. **Layers by index, wheel form (*spec call*, gr514459).**
    `Board(stackup=<int n>)` emits `meta["stackup_layers"]` as the bare
    standard names; `Board()` emits NOTHING (the semantic default is 4 —
    `DEFAULT_STACKUP` — so every golden stays byte-equal). A list of
    names is refused naming `stackup=<int>`; bool and counts < 2 are
    refused. `plane()`/`keepout()` take indices (`0` = top, `-1` =
    bottom); a string raises naming its index equivalent.
    `precis.pcb.ir::layer_name` is the one index→name map; the wheel
    keeps a byte-identical mirror plus `_PRESET_LAYER_COUNTS` (authority
    `STACKUP_PRESETS`) because `precis_geom.pcb` is stdlib-only.
27. **0c row normalisation (*spec call*, gr514460).** New
    `precis/pcb/rows.py` (shipped in `[pcb-kernel]`) is the one
    normaliser: the batch-row validators moved out of `_pcb_ops.py`,
    `lcsc_id` (moved out of wheel-excluded `catalog.py`, which
    re-exports it), `pin_tags_meta` (the `_pcb_insert_pins` fold),
    `net_row`/`net_patch`, `instance_row`, `footprint_pin_labels`,
    `map_pad_numbers` (the gr495291 pad-number rewrite, pure form),
    `NET_SPEC_FIELDS`. `batch_to_graph` emits `pcb_graph` order
    (instances by refdes, nets by name, members/unconnected by
    refdes,pin) and mirrors declare-all completion plus
    connection-ghost pins, so parity is literal. **Store semantics win
    on every drift found** — the wheel now emits: `domain`
    `"electrical"`, `layer` `"top"`, `label` refdes fallback, `rot`
    parsed via `rot_deg`, `n_pins` counting live pins (declared ∪
    completed ∪ ghosts), roles `fanout` default 1 and the
    `voltage`→`voltage_v` alias, `part_lcsc` canonicalised via
    `lcsc_id`, `supplies` normalised, `client_of` normalised (no raw
    copy, no `current_role` copy), `etype` read from the row's
    top-level `etype` only — `Pin.etype` writes `extra['etype']` and
    `meta.etype` rides `meta` verbatim earning no tag (the store
    rule; 0c seam review), `polarized`/`value`/`note` dropped (never
    graph keys — the label regex is the only working polarity path).
    `get(kind='part', view='footprint')` returns the cached
    `part_footprints` row as JSON (`pads`/`pin_map`/`courtyard`/
    `centroid`/`holes` + `package`/`height_mm` off `parts` + `pn<ref>`
    + content sha); `precis_geom.pcb.catalog` resolves it through the
    staged `precis_client` under `precis_access: read`, records
    `_CATALOG_READS`, and `Board.manifest` emits `parts` +
    `precis_geom.version`. Stackup: `meta.stackup_layers`/
    `stackup_preset` ride `refs.meta` only — `pcb_apply` never reads
    them and a fresh board is born with `DEFAULT_STACKUP` verbatim
    (a `Board(stackup=2)`/`'2l'` upload silently lands 4 layers — a
    server-side gap, NOT fixed here). `_stackup` still expands them
    for the offline run (`usbc_attiny`'s 2l needs it); a bare inner
    name emits `routable: True` to match `DEFAULT_STACKUP`'s stamped
    shape, so the tested `stackup=4` parity is literal.

## Slices

| # | what | Devin alone? | Opus seam review? | migration |
|---|---|---|---|---|
| 0 | prerequisites from the folded gripes: lint-clean `pcb.local` alias (gr514458), layers by index (gr514459), one normaliser + prod read path + manifest ids (gr514460) | 0a, 0b yes; 0c no | 0c yes (store/wheel seam, read path) | none |
| 1 | **phase 0**: pairwise part map, incremental proxy, placer with rubber band + elite archive, pin assignment with write-back, summary + ranked edits, `placer='topo'` | 1a yes; 1b yes; 1c no | yes (TopoDesign input shape, ladder hooks, write-back) | none |
| 2 | **phase 1**: CDT mesh, wires as edge sequences, crossing graph, list-2-colouring, via points, certificates, via bracket; ladder rung 2 | no | yes (mesh arrays, wire order invariant) | none |
| 3 | **phase 2**: topology anneal (re-thread, move via, flip piece, move net layer, re-solve swap group); grouping, unidirectionality, corridor terms; exact cyclic pin order | yes, after 2's seam review | no | none |
| 4 | **phase 3**: corridor demand/capacity per edge per layer; rigid-body shove LP with no triangle inversion; results feed the archive | no | yes (LP formulation, topology-preserving invariant) | none |
| 5 | **phase 4**: topo's own realiser — per-net layer rules and fixed vias become copper | yes | no | none |

Sizes: 0 S–M, 1 L (three builds), 2 L (two builds), 3 M, 4 M–L, 5 S.
"Devin alone" means a fresh builder can finish it from this text; "Opus
seam review" means an Opus pass on the API before the slice lands.

Progress: slice 0 (0a/0b/0c), **1a**, **1b**, **1c**, **2a** landed;
phase-3 shove engine landed overlap-driven 2026-10-25 (pcb-loop2):
`topoplace/shove.py` runs before legalize — soft MTV-separation rows +
round-mesh triangle signed-area guard (linearised, backtrack-verified
on actual areas) + legal centre-box bounds, ≤10 rounds; tris inside a
conflict's intersection lens are exempt (transient geometry). The
corridor demand/capacity driver (`corridor.py`) waits on slice-2b
wires.
2026-10-25 — `src/precis/pcb/topoplace/` exists (design, pairmap,
proxy, archive, placer, pins, writeback, summary, runstore); 2a mesh
in flight on `worktree-topo-s2`: `edge_owner`/`edge_side` per
constrained edge (a wire may leave its own part; SMD blocks its own
face, TH/board/zones all layers, scoped keepouts their `layers`),
`conflicts` as part-index pairs with overlap area + mount-side split
(same-side = the placer violation; the proxy prices it as the
`overlap` share), `outline_conflicts`/`zone_conflicts` separated,
orphan pads refused, `TopoZone`s (keepouts/mounting holes/fixed
copper), `net_pin_force` + class-rule refusal on unresolvable layer
names. Reto ruling 2026-10-25: **vias are `(edge_id, t)` positions
inside a wire — the mesh stays frozen, edge ids never renumber**;
and `world_courtyard_polygons` mirrors bottom rings (physical
occupancy). the wheel
gained `Part.pin`/`pin_functions`/`pins_with`/`swap_group`,
`net.tie(current=, source=)`, `PinConflict`, `pcb.Max`/`pcb.Min` and
the offline `over_current`/`led_without_limit` checks; `pcb_place`
takes `placer='topo'` and the pcb kind takes `view='topo'`.

1b spec-calls made (unspec'd seams, decided during the build):
slot nets ride `decl_nets`, NOT `net_pins` — a plane net's members
would mint airwires the proxy counts, so `FlexSlot.net` is a decl_nets
index and `decl_net_route` maps back to the routed net. `TopoDesign`
gains `claims` (pin id, name) so `pins.h` can emit named fixed claims,
and `PinAssignment` gains `choice` (the chosen pin id per slot) for
unnamed-slot write-back. Wired-on-net slot candidates are immovable
seats — the wire exists regardless — so only unwired candidates and
swap-group members move; plane/empty-net slots get zero destination
cost and take any free pin. `cost_vs_fixed` is measured against the
greedy declaration-order assignment (Hungarian as fallback). Wheel:
`pin()` and `swap_group()` declare their pins so candidates reach the
IR (`unconnected` with real pad positions). Electrical check (Decision
15): `max_current_a` is read from pin meta or the `pin_functions`
entry, an LED pin is one whose function starts `LED`, and a
current-limit part is a part with exactly two wired pins, one declaring
`current_role("series")` — the reach from a requirement net crosses
non-limiting two-pin bridges and stops there.

1c spec-calls made (unspec'd seams, decided during the build):
on the `pcb_place` job `iters` IS the topo eval-count effort — its own
default 20000 (mirroring `pcb.local.topo_place`), not `PLACE_ITERS`;
the job runs `PRESETS["compact"]` always — no `preset=`/`objective=`
param is forwarded (the intent-shares plumbing is pcb-intent-api's);
a `PinConflict` out of the kernel is recorded as an unclassed failure
(the board script's error, not the job's); the winning pose folds back
into the IR so the shared gate/ripup/`pcb_set_pose` tail is unchanged —
`pcb_set_pose` writes x/y/rot only, so side flips ride
`pcb_move_instance(layer=)` in the same transaction and flipped parts
count as moved for the fixed-copper rip-up; the run files ride the job
ref's `meta.topo_run.files` (Decision 20's store — no new table);
`placer`/`run`/`part`/`against` joined the handler's ARGS spec (dispatch
refuses undeclared args keys). **Mover domain (kernel gap found while
wiring the job):** the proxy prices crossings + length only, so an
unclamped mover parked parts outside the outline and the placement-DRC
gate refused the whole run; translate moves, probes, restart jitter and
the NaN scatter are now clamped to each part's legal centre box — the
outline bbox (else the part-count square) inset by the part's pin
radius + 0.5 mm — the same translate-domain cap
`optimize._derive_placement_bounds` applies. `TopoDesign` still carries
no footprint extent, so pads/board-edge clearance beyond the pin radius
stays a later-slice concern.

2026-10-25 — `vroute` retired and deleted (tag `vroute-final`; Reto:
"This is the only router to use. Retire and delete vroute if it
confuses. Even if we have none."). `op='route'`/`local.route` refuse
with `next=` naming the topo path; `pcb_route` is gone from the job
surface and stored routes still render. Slice 5 rescoped: it is no
longer a hand-off — topo grows its own realiser (§5). Until it lands a
placed board reports every fanout `unrouted` and routed copper is not
available.

### 0. Prerequisites (folded gripes)

**0a — gr514458, lint-clean alias.**
Files: `src/precis_geom/pcb.py`.
- Replace the module `__getattr__` for `local` with a lint-clean pattern
  (a `TYPE_CHECKING` import of `precis.pcb.local as local` plus the
  existing lazy resolver, or drop `"local"` from `__all__`); `ruff check
  src/precis_geom` passes with no noqa; a comment at the site states the
  pattern for the next lazy alias.
- Audit every other `__getattr__` export under `src/precis_geom/`.
Tests: `tests/workspaces/test_geom_wheel_imports.py` gains a case:
`from precis_geom.pcb import local` works with `[pcb-kernel]` and raises
the existing install hint without it; `ruff check src/precis_geom` exit 0.

**0b — gr514459, layers by index.**
Files: `src/precis_geom/pcb.py` (`Board.__init__`, `b.plane`,
`b.keepout`, any `layers=` argument), `src/precis/pcb/ir.py`
(`validate_stackup`, new `layer_name(index, n_layers) -> str`),
`src/precis/pcb/local.py::_stackup`, `examples/boards/*/main.py`.
- `Board(stackup=4)` is the default (= `DEFAULT_STACKUP`); `stackup=2`;
  a preset name (`'4l-two-sided'`, …) stays. A list of layer names is
  refused with `next=` naming `stackup=<int>` or a preset.
- Layer arguments take ints (`0` = top, `-1` = bottom allowed); a string
  layer raises `PcbBuilderError` naming the index form.
- `layer_name` is the one index→standard-name map; the batch, gerber and
  dsn keep standard names.
Tests: `tests/workspaces/test_pcb_board.py`: `Board()` has 4 routable
layers; `Board(stackup=2)` has F.Cu/B.Cu; `b.plane(1, net=gnd)` emits
`In1.Cu`; `b.plane("In1.Cu", …)` raises naming `1`; tier-1 goldens of the
converted examples are byte-equal to before.

**0c — gr514460, one normaliser and the prod read path.**
Files: new `src/precis/pcb/rows.py` (pure: the component/pin/pad/footprint
row normalisation now inside `store/_pcb_ops.py::_pcb_apply` and
`_pcb_insert_pins`, canonical pad key `number`, `pin` accepted as an
alias); `src/precis/store/_pcb_ops.py` (calls it); `src/precis/pcb/local.py`
(calls it; delete the mirror: `_pin_facts`, `_norm_pad`, the hand-copied
hoists in `batch_to_graph`); `src/precis/pcb/sanity_board.py` (emits
`number`); `src/precis_geom/pcb.py::catalog` (resolves through the read
MCP: the sandbox's `precis_client.py` under `precis_access: read`, whose
allow-list already has `part` and `pcb`); manifest writer
(`parts: [{lcsc, part_id, footprint_sha, read_at}]`, plus the wheel
version).
Tests: `tests/test_pcb_rows.py`: one board built via the wheel and via
`put`/`pcb_apply` on the dev DB gives identical normalised rows; a pad
keyed `pin` normalises to `number`; `local.py` has no row-shaping code
left (an AST test that it imports `precis.pcb.rows` and defines none of
the deleted names); a `catalog()` call inside a `precis_access: read`
sandbox run returns pads, and the manifest lists the part id and sha.
Laptop behaviour with no read path: Decision 25.

### 1. Phase 0 — placer, pairwise part map, pin assignment

Package: `src/precis/pcb/topoplace/` (`__init__.py` docstring carries the
model; no imports from `precis.store`, asserted by the wheel import
test). Input shape, built once from the IR by `design.py`:

```python
@dataclass(frozen=True, slots=True)
class TopoDesign:
    parts: tuple[str, ...]                 # refdes, index = part id
    pin_xy: np.ndarray                     # (P, 2) pin offsets in part frame, mm
    pin_part: np.ndarray                   # (P,) int part id
    pin_side: np.ndarray                   # (P,) int8: 0 top SMD, 1 bottom SMD, 2 TH
    net_pins: tuple[np.ndarray, ...]       # per signal net: pin ids (plane nets dropped)
    flex: tuple[FlexSlot, ...]             # any_of slots (1b)
    swap_groups: tuple[SwapGroup, ...]     # plain / tuple groups (1b)
    fixed: np.ndarray                      # (N,) bool: xy locked
    freedoms: np.ndarray                   # (N,) uint8 bitset: ROTATE | BACK_SIDE
    outline: np.ndarray                    # (K, 2) board outline, mm
    pose0: np.ndarray                      # (N, 4) x, y, rot_quarter (0..3), side (0/1)

def design_from_ir(ir: PcbIR, footprints: Mapping[str, dict]) -> TopoDesign
```

**1a — pairwise map, proxy, placer.** *Landed 2026-10-25.* Files:
`topoplace/pairmap.py`, `topoplace/proxy.py`, `topoplace/placer.py`,
`topoplace/archive.py` (+ `design.py`: `TopoDesign`, `design_from_ir`,
`ObjectiveShares`, the `FlexSlot`/`SwapGroup` shells 1b fills).
Spec-calls the builder made that the seam review should confirm:
`ObjectiveShares` carries the two proxy-priced terms (`crossings`,
`ratsnest`) normalised to sum 1, applied reference-normalised at the
seed pose (pcb-intent-api §Weights); `freedoms` derives from the IR
lock bits (ROTATE unless `inst_fixed_rot`, BACK_SIDE unless
`inst_fixed_xy`); `pose0` snaps authored rotation to the nearest
quarter; `net_pins` keeps non-plane nets with ≥2 pins; a shared net's
representative pin per side is the one nearest the other part's centre
at `pose0`, fixed before the rotation grid is swept (the grid is
(4,4,2) — mirror of b); equal-projection ties order near-to-far;
crowding evicts every offender only when the candidate beats them all
(keeps the pairwise-diversity invariant); the proxy MST is pin-level
Euclidean — `MST_VIA_BIAS_MM` belongs to the layer logic of slice 2;
incumbent ranking is `(crossings, cost)`, crossings primary; a `Ladder`
rung's return keys that snapshot's archive admission; acceptance
temperature self-calibrates per restart off the median probe |Δ|.

```python
@dataclass(frozen=True, slots=True)
class PairStat:
    a: int; b: int
    strength: float            # Σ over shared nets of 1/(k-1)
    crossings: np.ndarray      # (4, 4) or (4, 4, 2) inversions per relative rotation (+mirror)
    best: tuple[int, int]      # argmin rotation pair
    best_crossings: int

def pair_map(d: TopoDesign) -> PairMap          # only pairs with strength > 0 stored
class PairMap:
    def stats(self) -> PairSummary              # mean, p95, worst k pairs (by best_crossings × strength)
    def seed_rotations(self) -> np.ndarray      # (N,) quarter turns, greedy over strongest pairs
    def clusters(self, k: int) -> list[list[int]]   # agglomerative on strength

class ProxyState:                                # incremental straight-airwire cost
    def __init__(self, d: TopoDesign, pose: np.ndarray, w: ObjectiveShares) -> None
    def try_move(self, part: int, pose_row: np.ndarray) -> float   # Δcost; rebuilds MSTs of the part's nets only
    def commit(self) -> None
    def revert(self) -> None
    @property
    def crossings(self) -> int
    @property
    def length(self) -> float

def topo_place(d: TopoDesign, *, objective: ObjectiveShares, effort: int,
               seed: int, archive_size: int = 8,
               ladder: Ladder | None = None) -> TopoPlaceResult
```

- Facing pin order for pair (a, b) at rotations (ra, rb): project each
  part's pins on nets shared with the other onto the axis perpendicular
  to the centre line, order them, map b's sequence through the shared
  nets, count inversions by merge sort (R14: inversions are the exact
  crossing count between two facing rows).
- Proxy segment tests are vectorised numpy over the moved part's airwires
  against all others, bounding boxes first.
- Moves: translate, quarter-turn rotate (if `ROTATE`), side flip (if
  `BACK_SIDE`; mirrors pin x), swap two parts of equal footprint.
- Rubber band: `E_rb = λ_t Σ_i |p_i − p_i^anchor|²` toward the elite
  chosen at restart; `λ_t = λ_0 γ^t` decays to below 1 % of λ_0 by
  mid-restart.
- Archive: admit a candidate better than the worst elite only if its mean
  part displacement from every elite is ≥ `d_min` (default 2 mm) or it
  differs in ≥ 10 % of rotations; otherwise it replaces the nearest elite
  only if better (crowding). Restarts seed from elites round-robin.
- `Ladder(rungs=[(every_n, callable)])`: slice 1 ships rung 1 (proxy) and
  the hook type; slice 2 registers the topo-route rung.

Tests (`tests/test_pcb_topoplace_pairmap.py`, `…_placer.py`):
- Two 4-pin parts facing with nets in reversed order: 6 inversions at
  (0, 0), 0 at the rotation pair that reverses one row; `best` names it.
- Clique weight: a net on 3 parts adds 0.5 to each of its 3 pairs.
- `try_move` + `commit` matches a full recompute of crossings and length
  exactly on 1000 random moves (synthetic 100-part board,
  `tests/fixtures/pcb/topo_synth.py`, seeded).
- Throughput, printed every run: proxy evals/s on the 100-part synthetic
  board; hard floor 500/s in CI, target ≥ 2000/s on the dev Mac (the
  measured-not-floored policy for the headline number).
- Placer: on the synthetic board, best proxy crossings ≤ the
  `autoplace` result at equal eval count on 5 seeds (printed per seed;
  hard on the median).
- Archive: after a run, no two elites violate the diversity rule; elite
  count ≤ `archive_size`.
- Determinism: two runs, same seed → identical `elites.json` bytes.

**1b — pin assignment and write-back.** Files: `src/precis_geom/pcb.py`
(`Part.pin`, `Part.pin_functions`, `Part.pins_with`, `Part.swap_group`,
`pcb.Max`/`pcb.Min`, `PinConflict`, `net.tie(..., current=, source=)`),
`topoplace/pins.py`, `topoplace/writeback.py`.

```python
u1.pin(5)                                     # Pin, fixed
u1.pin(any_of=[1, 4, 9], name="BTN")          # FlexPin, solver picks
u1.pin_functions({"PA1": ["GPIO", "INT1"], ...}, source="ds:<id>~<chunk>")
u1.pins_with("INT")                           # pin ids whose table lists INT
rn1.swap_group([(1, 8), (2, 7), (3, 6), (4, 5)], tuple=True)
net.tie(u1.pin(any_of=u1.pins_with("GPIO"), name="RED_LED"), d1["A"],
        current=pcb.Max("20mA"), source="ds:<id>~<chunk>")

class PinConflict(PcbBuilderError): ...       # names both claim sites
def check_slots(d: TopoDesign) -> None         # empty set / Hall → PinConflict
def assign_pins(d: TopoDesign, pose: np.ndarray, proxy: ProxyState) -> PinAssignment
@dataclass(frozen=True)
class PinAssignment:
    by_name: dict[str, tuple[str, int]]        # name → (refdes, pin)
    swaps: tuple[tuple[str, int, int], ...]    # group moves applied
    cost_vs_fixed: float                        # proxy Δ vs the declaration-order assignment
```

- Destination of a slot: the nearest other pin of its net under the
  current pose (multi-pin nets: nearest pin; plane nets excluded).
- Hall check: maximum bipartite matching
  (`scipy.sparse.csgraph.maximum_bipartite_matching`); on deficiency the
  König witness set gives the signals and pins in the message.
- Infeasible pairs cost `BIG = 1e9` after the Hall check passes.
- 2-opt: swap two slots' pins (or two tuple-group members) when the
  incremental proxy crossing count drops; stop at no improving swap.
- Collinear destinations along one outline side: sort by projected
  position, no Hungarian.
- The electrical check: a `current=Max(...)` requirement on a net whose
  chosen pin has a library drive rating below it is an error; an LED pin
  on a net with no series resistor or current-limit part is
  `led_without_limit`.
- Write-back: `writeback.py` writes the assignment to `pins.lock.json`
  (Decision 24), plus `out/pins.h` (`#define BTN_PIN 4` per name, refdes
  in a comment) and the summary table.

Tests (`tests/test_pcb_topoplace_pins.py`, `tests/workspaces/test_pcb_pins.py`):
- `u1.pin(5)` claimed as `BTN` then as `LED` raises `PinConflict` naming
  both script lines; claimed as `BTN` twice is a no-op.
- `BTN`, `INT2`, `INT3` with `any_of=[1, 4]` raise
  "BTN, INT2, INT3 need [1,4]: 3 signals, 2 pins".
- A fixed pin inside another slot's `any_of` is removed from it.
- 8 slots whose destinations sit on one connector row: 0 crossings among
  them after assignment.
- Random instances (seeded): Hungarian result has no self-crossings;
  2-opt never raises the proxy crossing count.
- Tuple group: `in_i`/`out_i` always move together.
- `pins.h` contains one define per `name`; a re-run with the same seed
  gives the same file.
- `led_without_limit` fires on a GPIO→LED→GND net and not with a series
  resistor.

**1c — summary, run storage, job path.** *Landed 2026-10-25.* Files: `topoplace/summary.py`,
`topoplace/runstore.py`, `src/precis/pcb/local.py` (`topo_place`),
`src/precis/workers/job_types/pcb_place.py` (`placer='topo'`),
`src/precis/handlers/pcb.py` (`view='topo'`).
- `pcb.local.topo_place(b, *, preset="compact", objective=None,
  effort=20000, seed=1, out="out/topo") -> TopoRun`; `TopoRun.summary()`
  renders Decision 19 with slice-2+ fields `n/a`.
- Ranked edits in slice 1: rotate/flip a part, unlock a fixed part, widen
  a part's `any_of` to its `pins_with` set; each re-runs the proxy
  placer at 10 % effort from the best elite and reports the Δ.
- Run dir: `manifest.json` (schema, wheel version, `CODE_VERSION`, seed,
  effort, objective shares, board content hash, the parts/footprints read
  from 0c), `elites.json`, `pairs.json`, `pins.json`, `summary.txt`,
  `placement.svg` (airwires, crossings marked, pair heatmap).
- `pcb_place` with `placer='topo'` runs the same kernel and stores the
  run files in job meta; `view='topo', args={'run': id, 'part':
  'summary'|'pairs'|'elites'|'svg', 'against': id2}`.
Tests: summary of the synthetic board ≤ 40 lines and names ≥ 1 ranked
edit with a negative Δ; the `against` diff lists changed poses and the
crossing Δ; `pcb_place` with `placer='topo'` on the dev DB writes poses
through the existing placement-DRC gate (refuses an illegal result like
the default placer).

### 2. Phase 1 — topological route to crossing-free

Files: `topoplace/mesh.py`, `topoplace/wires.py`, `topoplace/layers.py`,
`topoplace/certs.py`.

```python
@dataclass(frozen=True, slots=True)
class TopoMesh:
    verts: np.ndarray        # (V, 2) float64 mm; pads, outline corners, via points
    tris: np.ndarray         # (T, 3) int32, CCW
    edges: np.ndarray        # (E, 2) int32, sorted vertex pairs
    tri_edges: np.ndarray    # (T, 3) int32
    edge_tris: np.ndarray    # (E, 2) int32, -1 on the hull
    constrained: np.ndarray  # (E,) bool, footprint outline / board edge
    pad_vertex: Mapping[tuple[int, int], int]   # (part, pin) → vertex

def build_mesh(d: TopoDesign, pose: np.ndarray) -> TopoMesh
def route_topology(mesh: TopoMesh, d: TopoDesign, n_layers: int) -> TopoRoute
@dataclass
class TopoRoute:
    wires: list[Wire]                   # Wire(net, layer, edges: np.ndarray, ends)
    order: list[list[list[int]]]        # [layer][edge] → wire ids in order along the edge
    vias: list[int]                     # via-point vertex ids
    bracket: tuple[int, int]            # via [lower, upper]
    certs: list[Certificate]            # OddCycle(nets), ForcedClash(net, layers)
```

- Airwires: MST per net (`ratsnest.build_airwires` order), each pulled to
  the shortest dual path through the mesh (Dijkstra over `edge_tris`,
  weights = midpoint distances); the wire is the crossed-edge sequence.
- Crossing graph over wire pieces from the order lists (Decision 3);
  pins on one footprint boundary use cyclic interleaving directly (R4).
- Layers: list-2-colouring over {0, n−1} with forced colours from
  `pin_side` and `layer_lock.py` (R15.1), O(V+E).
- Odd cycle or forced clash: insert a via-point vertex on the yielding
  net (Decision 6 order) between the cycle's crossings (§B2 section
  midpoint), re-colour; lower bound = edge-disjoint odd cycles found,
  upper bound = 2·crossings or the greedy sweep (R15.4).
- Exact reference for small boards: the R9 max-cut as a `scipy.optimize.milp`
  ILP over sections (§B2 with L = {F, B}); a test asserts the heuristic
  via count is within the bracket and ≥ the ILP optimum.
- Inner layers open only when a TH-ended net makes them free or the
  bracket exceeds the preset's via budget (Decision 5).
- Ladder rung 2 registered; `bench_topo.py` records Spearman ρ between
  proxy crossings and rung-2 vias over 200 placements per bench board and
  prints the chosen N.

Tests (`tests/test_pcb_topoplace_route.py`):
- §B5 example A: SMD on F, 2 layers → 4 vias; all TH → 1 via; 4 layers
  SMD → 4 vias; certificate lists A, B, C.
- A bipartite synthetic board → 0 vias, empty certificates.
- On straight-line instances the order-swap crossing test agrees with
  shapely segment intersection on 10 000 random pairs.
- QFN-32 with destinations in reversed cyclic order: crossing count =
  interleaving count (R4).
- Mesh: every pad is a vertex; every constrained edge present; Euler
  check V − E + T = 1 (footprint interiors are triangulated, no holes).
- Rate, printed: full topo route evals/s at 100 parts (target 10–100/s;
  hard floor 2/s).

### 3. Phase 2 — topology anneal

Files: `topoplace/anneal.py`, `topoplace/objective.py`.
- Moves: re-thread a wire around the other side of a pad, move a via,
  flip a piece's layer, move a net's layer, re-solve a swap group.
- Objective terms (shares per board, Decision 18): via count; grouping =
  per-net layer entropy ("90 % of gnd on in2 and 100 % of v33 on in1
  beats 50/50"); inner-layer unidirectionality from piece endpoint
  vectors; peak corridor demand/capacity (slice 4's model, coarse here).
  Power nets may become plane pours (reported as a ranked edit).
- Pin assignment in cyclic exit order (Decision 11, exact).
- Budget: seconds at 100 parts, as an eval count.
Tests: best never worse than the slice 2 start; on a synthetic gnd/v33
board the entropy term moves both nets to ≥ 90 % on one layer; via count
≤ slice 2's on every bench board (printed, hard on sanity); deterministic.

### 4. Phase 3 — corridors and shove

Files: `topoplace/corridor.py`, `topoplace/shove.py`.
- Per edge per layer: demand = wires × (track + space) + via room;
  capacity = edge length − pad/keepout extents (`escape.py::_gap_between`
  rules, capability row from `capabilities.py`). This is the cut
  condition on taut representatives (R11, Leiserson–Maley
  10.1145/22145.22153; river form R8, Leiserson–Pinter 10.1137/0212029).
- Shove: footprints are rigid bodies, rotation fixed in v1. A small LP
  (`scipy.optimize.linprog`, HiGHS) finds minimal |Δx|+|Δy| satisfying
  every overflowing edge's length constraint, linearised at the current
  pose, with every triangle's signed area kept ≥ ε (no inversion, so the
  topology is preserved); iterate to a fixed point or 10 rounds.
- Shoved poses enter the phase 0 archive as elites.
Tests: a squeezed synthetic board with 3 overflowing edges ends with 0;
no triangle has signed area ≤ 0 after any round; every wire's edge
sequence is unchanged; total displacement ≤ 1.05 × the LP optimum of the
first round; outline containment holds.

### 5. Phase 4 — the topo realiser

`vroute` (`src/precis/pcb/vroute.py`) is deleted (2026-10-25, tag
`vroute-final`); topo is the only router, so this slice grows topo's own
realiser: it turns the topo route's wires (edge sequences on one layer)
and via sites into copper tracks and vias written through the normal
realise/store path. There is no routed-copper output until this lands.

- The topo route's per-wire layer assignment fixes each wire's layer (a
  surface pad's mount side stays the terminal layer); the topo route's
  via sites become fixed vias at their `(edge_id, t)` positions.
- Track width and clearance come from the net's rules (the
  `topo.build_region_graph` path already resolves per-net
  width/clearance).
- Layer names via `layer_name` (R-d).

Tests: the sanity board's topo route hands every wire a layer and every
via a site, the realiser emits copper that passes DRC 100 % with 0
errors (hard), and its routed count and via count are printed beside the
bracket.

## In scope

Slices 0–5 above; skills: `precis-pcb-workspace-help` gains the pin forms,
`pcb.local.topo_place` and the summary format in slice 1; the topo skill
(`precis-pcb-topo-help`, split out of `precis-pcb-route-help` which hit the
32KB skill cap) covers `placer='topo'` and `view='topo'`; glossary entries
(topo-place, pairwise part map, flexible pin slot, via bracket) in slice 1.

## Explicitly NOT in scope

- A peripheral DSL (I2C instances, timer sharing, alt-function
  exclusivity); the LLM fixes those pins (Decision 11).
- Promoting pin function tables into the component library (Decision 16).
- Native taut geometry — no routed-copper output exists until the
  slice-5 realiser lands (`vroute` deleted 2026-10-25, tag
  `vroute-final`).
- Rotation during shove.
- Blind and buried vias.
- Exact k-layer via minimisation beyond the 2-layer relaxation (NP-complete
  from 3 layers, R9/R10); inner layers use the heuristic of Decision 5.
- Making topo-place the default placer.
- The `pcb_requests` cache (pcb-workspace-code slice 2); `placer='topo'`
  runs through the existing `pcb_place` job until it lands.

## Acceptance criteria

1. `ruff check` and mypy are clean on every touched file in every slice;
   `from precis_geom.pcb import local` passes lint with no noqa.
2. `Board()` gives 4 routable layers; layer arguments are indices; every
   export names standard layers only.
3. One normaliser: the wheel and the store produce identical rows for the
   same board, and `local.py` contains no row-shaping code.
4. A topo run's `manifest.json` lists every part and footprint read from
   prod with its id and sha, and the wheel version.
5. Phase 0 proxy runs ≥ 500 evals/s at 100 parts in CI (printed rate,
   target ≥ 2000/s), deterministic per seed.
6. `PinConflict` and Hall violations raise with names, pins and script
   lines as in Decision 12; every chosen flexible pin appears in the
   write-back and in `pins.h`.
7. §B5 example A gives 4 / 1 / 4 vias with the K3 certificate.
8. Shove never inverts a triangle and never changes a wire's edge
   sequence.
9. Sanity routes 100 % with 0 DRC errors through the slice-5 realiser.
10. No hostnames or IPs in code, fixtures or docs.

## Questions

Only those that block slice 1.
