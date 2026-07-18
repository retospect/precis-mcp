# 0050 — The `fluidics` kind: a microfluidic network + constraint IR the LLM can *reason* over

- **Status**: proposed (2026-07-09) · **v1 draft / discussion — OPEN, not
  closed**. Five of six forks decided (see *Fork decisions*); only **constraints
  storage (fork 4)** remains open. (The
  microfluidics sibling of [ADR 0041](./0041-cad-kind-analytic-ir.md) /
  [ADR 0042](./0042-pcb-kind-netlist-placement-ir.md) /
  [ADR 0043](./0043-structure-kind-atomistic-ir.md); same philosophy — **own a
  legible IR, rent the heavy kernel only at export** — applied to a fluidic
  **network**: a netlist + declarative constraints the LLM reads as a graph,
  not pixels. DXF (adhesive/film cut layers) + G-code (milled plastic) are the
  headline export consumers.)
- **Deciders**: Reto + agent
- **Builds on**:
  - [ADR 0042 — The `pcb` kind](./0042-pcb-kind-netlist-placement-ir.md) — the
    **direct structural twin**: a netlist is a graph stored in **dedicated
    relational tables + one card chunk**, with a graded **measure/constraint**
    family and a `fixed` mark for what the optimizer may not move, a two-layer
    logical/physical split, and *route it, don't draw it*. A fluidic network is
    as relational as an electronic one.
  - [ADR 0041 — The `cad` kind](./0041-cad-kind-analytic-ir.md) — the
    **keystone source**: give the LLM *eyes* without pixels via a graph it
    queries with probes + persisted observers; heavy geometry only at export.
  - [ADR 0043 — The `structure` kind](./0043-structure-kind-atomistic-ir.md) —
    the storage + `derive`-lineage + web-viewer + instruction-box template.
  - [ADR 0044 — The derived-job lane](./0044-derived-job-lane.md) — routing,
    sizing, and fab-file generation are **compute-lane** derived jobs owned by
    the artifact (idempotent, content-addressed, cache-fillable), exactly as
    `cad` tessellate / `structure` relax / `pcb` route.
  - [ADR 0033 — editable chunk documents](./0033-draft-chunks-editable-document.md)
    — only the soft-delete *semantics* (a `deleted_at` column), if any tables
    carry mutable rows. The graph itself is **not** chunk-native (as 0042/0043).
  - [ADR 0036 — Universal handles](./0036-universal-handles.md) — the design
    ref's 2-char code (proposed **`fl`**; verify free in `handle_registry.py`
    at implementation).
  - [ADR 0051 — Turn-taking: persona threads + blackboard](./0051-turn-taking-persona-threads-and-blackboard-convergence.md)
    — the substrate for **fork 3** (assisted routing): fluidics placement is a
    **persona thread** that **raises constraints** as ordinary turn moves
    (binding constraints get salience eyes in the render), *not* an autorouter.
    The `fluidics` persona + its verb-surface extension ride 0051's turn loop.

## Context

A microfluidic network *is* a netlist — reservoirs / ports / junctions
(nodes), channels (edges), valves (gating elements), on one or more fabricated
layers — with **constraints** on top: volumes of individual and grouped
channels/valves fixed to values or ranges, valve shapes, tooling limits,
spacing. The workflow is: settle the graph + constraints, then **route** it
(place, size, assign layers) subject to further geometric rules, and export
**DXF** (adhesive/film cut layers) + **G-code** (milled plastic).

The hard question 0041/0042/0043 already answered for solids, boards, and
crystals: **how to give an LLM traction without pixels.** The answer here is
the same — the design is *already a graph*, so we make it queryable (netlist +
constraints the model reads and argues over) and keep the heavy machinery
(routing, sizing, fab-file synthesis) at **compute/export** as derived jobs.
The value is that the graph and its constraints are legible, searchable,
embeddable objects, so an LLM can **reason, argue, and make variations** — fork
a design, tweak a constraint, re-derive geometry.

## Decision

### 0. Framing — a thin substrate + an authored→derived cascade

Two framing decisions govern how to read everything below (they answer "what is
the substrate / how do we think about this properly / how do we avoid a rigid
tool surface").

**(a) Substrate vs skill — the tool surface stays thin.** Do **not** hardcode
fluidic application concepts. "valve / pump / through-well / tube-adapter /
peristaltic / bubble-trap / actuator-plane" are **compositions**, and
compositions belong in a **skill (+ a data template catalog)**, not in code — as
a "bolt" is not a cad primitive but a composition of frustums. The substrate
hardcodes only the *engine-facing atoms*; every fluidic noun is skill vocabulary
composed from them, so the LLM invents a new element / stackup / actuation with
**no code change**.

| substrate atom (code, generic) | skill (data + prose) |
|---|---|
| attributed graph (`node`/`edge` + free `type` + `attrs`) | **element library** (valve/pump/well/adapter…) = **clonable template footprints stored as data** — the `pcb` parts-catalog pattern (a *table*, not an enum) — + how-to-compose prose |
| plane stack (`{material, thickness, bond-type, role, export}`; count/roles = data) | **stackup recipes** (film-film … film-adhesive-moldedpart-adhesive-film) |
| footprint + facets (`{anchor, facet[]}`, place-once-stamps-all); a facet's only **code-visible roles** are **has-net→router / solid-op add\|subtract→cad / part-link→BOM** (§8d's five kinds are *skill interpretation*) | **actuation patterns** (peristaltic 6-phase, metering) over the generic state+sequence primitive |
| the **constraint** entity (fork 4): handle-targeting, graded, re-evaluated | **bubble heuristics** + **tooling/DRC rule-sets** = constraint *templates* the skill instantiates |
| probe/observer library; route + geometry/export seams; derive/variation; generic **state+sequence** | — |

So **§3–§8d read as the *content of the skill*, not the tool surface** — all
true, just on the skill side of the line. Correctness comes from the probe/lint
layer (compose freely; truism-lint + observers catch a malformed valve); reuse
comes from **cloning data templates**, not named code.

**(b) Inside the substrate — a small authored core, a big derived cascade.**
Coordinates, rules, routings, and derivative parts all sort into *authored
source-of-truth* vs *derived output*, and that sort **is** the mental model.

- **Authored** (small, legible, the only thing you fork/edit) — three orthogonal
  facets: **graph** (abstract topology + attrs + intent; coordinate-free),
  **placement** (footprint *anchors* + plane stack; coarse coordinates only),
  **rules** (constraints).
- **Derived** (regenerable, content-addressed, `derived-from`-linked, never
  hand-edited) — the cascade: **routing** = route(graph, placement, rules⊂) →
  centerlines; **geometry** = assemble(footprint bodies + routing + stack) → the
  cad solid; **derivative parts / fab** = project/CAM(geometry) → DXF, G-code,
  BOM, core, actuator-plate; **analyses** = observe(geometry + graph + rules) →
  DRC, bubble, volumes, throughput.

Governing principles: **(1) author small, derive big** — edit only
graph+anchors+rules. **(2) coordinates at two grains** — author coarse *anchors*,
never fine geometry (routed path, fillet); derivation fills the fine grain (this
is *why* the surface stays thin — you place a footprint, you don't draw a
channel). **(3) the cascade is a DAG with invalidation** — change an anchor /
rule / linked cad part (§8b) → content-address changes → everything downstream
re-runs (cad-recompute + ADR 0044). **(4) rules are connective tissue, not a
stage** — authored, but each engine consumes only the subset it understands
(router: width/clearance/layer; sizer: volume; DRC/bubble: the rest), and a rule
may target an authored *or* a derived handle. **(5) nothing fluidic is named**
in (b) — generic categories; the nouns live in (a)'s skill.

**(c) Engine vs document — who does the work, and the loop between them.**
Orthogonal to (b)'s *data* split (authored vs derived) is a *labor* split
(LLM judgment vs deterministic code):

- **Document** (LLM/human — intent, judgment, fluidic naming): the graph, the
  component placements (anchors), the rules + **global tolerance policy**, the
  stackup, text-spec instructions that parameterize derivations, and —
  critically — the **loop control** (what to change when a constraint binds; the
  trade-offs; "good enough").
- **Engine** (pure, deterministic, reproducible, content-addressed; *no
  judgment, no fluidic names*): **route** (Freerouting seam), **size** (LP
  width/serpentine to a volume within tolerance), **check** (DRC / volume /
  bubble evaluators → met/violated/underdetermined, *raising* the binding ones),
  **assemble** (cad kernel), **project/CAM/export** (DXF/G-code/BOM/actuator-
  plate), **probe/observe**.

**The place-and-route loop is the handshake** (fork 3 turn-taking): engine
evaluates → *raises* which rules bind → document (LLM) adjusts
placement/graph/rules → engine re-evaluates. Converged = every rule within its
tolerance = **"within spec."** The engine never decides *what* to change; the
document never computes geometry or tests a rule. Then the settled valve
coordinates are **frozen** [document] and the derived blocks (cad assembly,
actuator holes projected from the valve anchors, fab files) are produced **by the
engine from the document's text-spec** — never hand-edited (principle 1).

**Tolerances (decided): a global policy + per-constraint override.** "Within
spec" is undefined without a band, so the rule layer carries a **global tolerance
/ design-rule default set** (clearances, width tol, registration tol, min
feature) that any constraint may override locally — the cad/pcb design-rule
pattern. Engines consume it; the checker's met/violated verdict *is* a tolerance
test.

**A component, definitively:** an instance record `{template-ref, anchor,
overrides}` [document] pointing at a **catalog template footprint** [document/
data, §0a]; its pads + bodies + facets are **expanded from the template at the
anchor** [engine, derived]. "A component" is a *placement of a template*; the
geometry it becomes is derived, not drawn.

### 1. Three-layer IR (the key structural call)

Do **not** fuse topology, intent, and geometry. Three legible layers in the
`fluidics` kind:

1. **Netlist (topology).** Nodes (reservoir / port / junction / via), edges
   (channel), elements (valve, pump, …), layer assignment. Pure graph;
   hand- or LLM-authored; stable; cheap to reason over.
2. **Constraints (intent).** Declarative, grouped, ranged — the "argue"
   surface (§6).
3. **Placement / geometry (realization).** Coordinates, routed paths,
   cross-sections, via positions. *Derived*, re-solvable, checked against
   layers 1–2, consumed by the exporters.

Layers 1–2 are what you fork to vary a design; layer 3 is a derived-lane job.

### 2. The stack model, bond-types, and stackup taxonomy

A design targets a **layer stack**: an ordered sequence of layers, each
`{material, thickness, bond-type}`. **Adhesive is just a layer with
thickness > 0** — that is how "the adhesive has a height" enters for free.

The **fab route is a property of how layers join and how features are
formed** — carried by bond-type:

| bond-type | feature formed by | exported geometry **means** | depth model |
|---|---|---|---|
| `adhesive` (laminate) | cut-through void | **void boundary** (closed loop) | pierced-span thickness (incl. adhesive height) |
| `weld` (film–film / fused) | patterned seam | **weld-seam path** (channel = complement) | nominal film gap — **compliant**, pressure-dependent |
| `mill` (molded/milled part) | pocket | **toolpath** | continuous, tool-Ø bounded |

The exporter **switches on bond-type**: adhesive/cut → removed-region boundary
DXF; weld → deposited-seam DXF (opposite semantics — same "2D curves out,"
different meaning, different DRC: seam width & seam-to-seam gap vs cut kerf &
void geometry); mill → G-code per face.

**Depth is a layer-*span*, not a scalar** — the generalization that subsumes
adhesive-height, laminate quantization, and deep channels:

```
depth  = Σ thickness(layer) over the contiguous pierced span
volume = width × depth × length            # rectangular approximation
```

- Adhesive height is one term in the sum.
- Laminate quantization = the span is a **discrete** set of catalog sheets
  (you pick a sheet thickness, you don't dial in a number).
- A deep channel = the span is more than one sheet.
- Welded/compliant channels are the one place `w·d·L` fails: height is set by
  film compliance + pressure (§7 scope: fixed nominal for v1).

**Enumerated stackups** the kind must express:

1. **film–film** (welded) — ~0 resting volume (no gap layer) but **super
   cheap**; channels = unwelded lanes, volume from inflation/compliance.
   `weld` bond-type, compliant depth.
2. **film–adhesive–film** — the adhesive defines the channel (void cut through
   the adhesive; depth = adhesive thickness).
3. **film–adhesive–…–film (N-layer)** — channel defined by *either* the
   adhesive layer *or* a sandwiched shim, with vias on layer crossings.
4. **film–adhesive–moldedpart–adhesive–film** — a milled/molded **core** carries
   its own channels (deep, 3D, both faces), capped by films, bonded by
   adhesive. The `mill` model meets the laminate model in **one stack**: the
   core exports G-code, the caps export DXF. A single design mixes bond-types.

A "single- vs bi-layer core" milled design is a degenerate stackup: one `mill`
layer, channels on one or both faces + through-vias.

### 3. Valves are servo-actuated pinch fingers (fork 2, decided)

The valve is a **mechanical pinch**: a servo-driven rubber finger presses down
on a channel and occludes it by deforming a compliant cap over the channel.
`{shape, stroke, displacement_volume, state}` still holds — `shape` is the
finger's **contact footprint** on the channel, `stroke` is how far it presses,
and `displacement_volume ≈ footprint_area(shape) × stroke` is the fluid pushed
aside. That number is **dead volume** (a metering *error*) in a metering
context and **stroke volume** (the *feature*) in a pump context; change the
footprint and every downstream quantity re-derives.

Consequences of pinch/servo (vs a Quake pneumatic membrane):

- **The actuator is external, not a fluidic layer.** There is **no control/flow
  bi-layer as a core concept** — the "control layer" is the servo rig above the
  chip, not a second fluidic network. Bi-layer, when present, is just a
  geometry choice (channels on two faces), *not* a valve-mechanism requirement.
- **A valve needs a deformable cap.** The pinched channel must have a compliant
  membrane over it, so pinch valves favor the **film-capped stackups** (the top
  film of `film–adhesive–…` or `film–adhesive–moldedpart–adhesive–film` is the
  pinch surface). This couples valves to the compliant-channel model (§2, §7).
- **Finger footprint sets spacing.** A servo finger has real physical width, so
  valve-to-valve **min-pitch** (and the pump's **max-pitch** for 3 proximate
  fingers) is bounded by the finger/servo footprint, not a lithographic pitch
  (§6.1 DRC).

**Rotary/selector valves remain out of scope** (§8).

### 4. Logical groupings + the behavioral layer (the pump)

The pseudo-peristaltic pump forces two additions a flat netlist lacks:

- **Logical groupings with graph-level claims** (fork 5, decided — *not* a
  parameterized library, *not* an expandable parametric part). A pump is a
  **named grouping** of existing graph elements carrying **logical claims** at
  the graph level: "`{v1,v2,v3}` form a peristaltic pump; stroke =
  `displacement(v_i)`." The grouping is a label + assertions over primitives
  that already exist, queryable as a unit — nothing is *instantiated* or
  *expanded*. The **distance/proximity constraints** such a grouping implies
  (the 3 fingers must be close, §6.1) are **recorded separately as ordinary
  constraints** (§6, fork 4's mechanism), *emergent from* the grouping but not
  baked into a part definition. No shared cross-design part catalog in v1.
- **Actuation programs (a valve-state trajectory).** Three proximate valves
  actuated in sequence (classic 6-phase `001→011→010→110→100→101…`); per cycle
  the moving compartment carries **one valve-displacement-volume** across the
  pump:

  ```
  pump stroke volume / cycle  ≈  displacement_volume(member valve)
  throughput                  =  stroke volume × cycle rate
  ```

  Pure graph + arithmetic — **no CFD**.

### 5. The compartment mechanic (load-bearing)

Valves partition the network into **compartments**: delete closed-valve edges →
the connected components are the isolated compartments. A metered bolus = the
volume of the compartment trapped between two closing valves.

- **Static:** volume constraints live on **valve-bounded compartments**,
  evaluated by connected-components — **no solver**.
- **Dynamic:** walk the actuation program and the same mechanic becomes
  transport (the pump's per-cycle displacement).

One mechanic → metering verification, dead-volume detection, pump throughput,
all as legible graph queries.

### 6. Constraints — split, not monolithic

The trap: **volume is not a netlist property — it emerges from geometry**, so a
volume constraint is really a constraint on the router/sizer. Split by cost:

- **Cheap graph checks** (instant — the "argue" layer): reachability,
  compartment volumes per state, dead valves, floating segments,
  metering-boundedness, pump stroke volume.
- **Bounded sizer** (a derived job, only when solving geometry): fix depths
  (continuous for `mill`, discrete catalog for `adhesive`) → volume targets
  become a small **LP/MILP** over channel widths + serpentine lengths. Group
  constraints (sum = X, ratio A:B = k:1 for a mixer) couple channels; ranges
  give slack.
- **DRC** (post-placement, geometric): §6.1.
- **Auto-routing** (the hard piece): deferred; assisted-first.

**Storage of a constraint (fork 4 — open, working note).** For the assisted
turn-taking loop (fork 3, ADR 0051) to *raise* a constraint, a constraint must
be an **addressable object**, not a field buried in the design's JSONB. The
leaning is the `pcb` **`measure` model**: each constraint is a first-class row
(`{class, target(s), predicate, value/range, note, status}`) with a **handle**,
graded from hard (a tooling limit that must hold) to soft (a preference), and
**re-evaluated like a 0041 observer** on every geometry change. That gives, for
free: a constraint can pick up a **salience eye** when it binds/violates (so the
persona "raises" it), it is searchable ("show designs where a volume constraint
is underdetermined"), and the emergent grouping constraints (§4 fork 5) are just
more rows. The alternative — inline JSONB + a render — is simpler to slice-0 but
loses the per-constraint handle the turn loop wants. **Not a new kind either
way.** To settle next.

Taxonomy:

| class | examples | where checked |
|---|---|---|
| topological | connectivity, which valve gates which edge | graph, instant |
| volumetric | `V(channel)=x`, `V(group)∈[a,b]`, ratio A:B=k:1, compartment dose | graph (declared geom) + sizer (solved geom) |
| dimensional / tooling | min/max width, min/max depth, cross-section shape (ball → round bottom; flat → forced corner radius; blade/laser → straight walls), **laminate depth discrete** | sizer + DRC |
| spacing / DRC | valve pitch, wall thickness, via keepout, bend radius, edge margin | post-placement |
| layer / topology | single vs bi-layer, which face, via parity on crossings | placement |
| behavioral | actuation-program validity, pump proximity + ordering | graph + placement |

#### 6.1 DRC is bidirectional

- **min-pitch** (isolation): collisions, wall thickness, via keepout, edge
  margin.
- **max-pitch + ordering** (composites): a pump's valves must be **proximate**
  (bound dead volume / phase timing) *and* **sequentially ordered** along the
  channel.

Both signs from the start (mirror `pcb` truism lint and `cad` `/analysis`).

### 7. Bubbles are a *predicted* hazard, not a graph element

Bubbles are **not authored into the netlist** and **not a node/annotation you
place** — they are **emergent, and predictable**. So they live as a **derived
prediction** (a DRC-like analysis), never as IR primitives:

- **Predicted from geometry + priming.** Given the routed geometry and the fill
  direction / actuation program, predict where gas will nucleate or lodge:
  local high points in the fill direction, dead-ends, sudden expansions,
  sharp corners. Output = predicted trap locations + severity.
- **Purge-path check.** Verify a priming / purge path exists to sweep predicted
  traps; flag unvented dead-ends.
- **Metering impact.** A predicted bubble inside a metered compartment is
  flagged as a dosing error (a trapped gas pocket is compressible — it breaks
  the fixed-volume assumption, like a welded compliant channel).

The prediction re-runs whenever geometry or the priming program changes — it is
a report over layer 3 (geometry) + the behavioral layer, not an input to them.
Intentional bubbles (air-gap separators, segmented flow) are the same
prediction read constructively; deferred past v1.

### 8. Scope for v1 (exclusions / deferrals)

- **Rotary / selector valves — out.** Deflectable/pinch/membrane only.
- **Compliant (pressure-dependent) volume — deferred.** Treat welded /
  film–film height as a **fixed nominal** in v1's sizer; file "compliant
  volume model" as a known deferral (drags in a pressure model).
- **Auto-routing — deferred.** Assisted placement first (human/LLM places; we
  DRC + size + export), like early PCB before autoroute.

### 8a. Relationship to the `cad` kind — rent it at the mill/export lane, don't build on its IR

The `cad` kind ([ADR 0041](./0041-cad-kind-analytic-ir.md)) already exists, and
the honest read is: **useful as a rented backend at the geometry/export lane,
wrong as the fluidic IR.** This is the same boundary `pcb` (0042) drew — a
sibling of cad, not built on it.

**Where cad *is* useful (reuse):**

1. **The molded/milled core is a cad solid.** The `mill` bond-type layer (the
   `moldedpart` in `film–adhesive–moldedpart–adhesive–film`, and the milled-core
   case) is exactly a slab with pockets: channels are `subtract` ops on a
   frustum/box, which cad expresses **exactly** (rigid-only ⇒ no mesh error) and
   already exports to STEP/STL/3mf. So the mill lane's solid representation +
   solid export come for free; only the **G-code/CAM step is net-new**, and it
   consumes the cad solid (or the layer-3 pocket geometry) rather than starting
   from a blank kernel.
2. **The whole-stack 3D preview is a *derived* cad assembly.** Each stack layer
   → an extruded solid with its channel voids subtracted, stacked at
   `z = Σ thickness`. That assembly feeds the **existing `/cad` three.js viewer +
   client tessellation** — the `/fluidics` viewer's 3D mode is a cad derivation,
   not new render code (the 2D-per-layer SVG of §9 slice 2 is still the primary,
   cheap view).
3. **Machinery + philosophy** (already threaded elsewhere in this ADR): the
   `derive`/`_propose`-job pattern (`fluidics_propose` ≈ `cad_propose`), the
   export-worker shell, and cad's **observer/probe** model — which fork 4's
   constraints (`pcb`-measure rows re-evaluated like 0041 observers) already
   borrow.
4. **DXF is a *projection/section* of the derived solid — one geometry source of
   truth.** Once the whole-stack assembly exists (point 2), every fab file is a
   *view* of it: STEP = the solid, 3D preview = its tessellation, G-code = CAM
   over it, and **DXF = a planar section/projection** of the relevant layer's
   solid (cad's `section` probe; OpenSCAD `projection()` / OCCT sectioning yield
   the closed 2D loops a DXF wants). This beats a parallel 2D emitter in three
   cases: **(a) non-rectangular milled cross-sections** (ball-endmill round
   bottom, flat-endmill corner radius, draft, variable depth — the honest outline
   is the projection, not a rectangular "profile"); **(b) cross-layer
   registration** — define a via/port/cap-opening *once* in 3D and project its
   footprint onto each layer plane, so alignment holds **by construction** and
   independently-authored layers cannot drift; **(c) DRC for free** — cad's
   `clearance`/`interference`/`section` observers check the assembled stack in 3D
   (top-face↔bottom-face wall thickness, channel-vs-via clash) instead of a 2D
   reimplementation. **Direct-2D remains the fast path only for straight-wall,
   cut-through layers**, where the profile *is* the section and the solid
   round-trip buys nothing.

**Where cad is the *wrong* home (own it, don't force it):**

- **The netlist + routed channel graph is not a cad primitive DAG.** A
  valve-gated graph and serpentine routed 2D paths-per-layer are their own
  representation (as a netlist is for `pcb`); a routed channel is not naturally a
  frustum, and the compartment/valve/pump semantics have no cad analog. Forcing
  the network into cad's DAG would repeat the mistake 0042 avoided. (The 2D cut
  geometry is a *derivation* of the routed layout, not a cad-owned thing — but
  its **realization into fab files** is best mediated through the derived cad
  solid, per point 4.)

**Geometry source of truth + sequencing.** Layer-3 canonical stays the **routed
2D paths + per-layer cross-section spec + stack** (what the placer emits and what
is easy to edit); the **cad assembly is a pure derivation** from it, and it is
the single geometry from which STEP / preview / G-code / DXF are all projected.
Caveat: manufacturing-grade section-to-DXF (closed loops, arcs, kerf/tool
compensation) leans on cad's section/projection **export**, which cad's own v1
plan defers to phase 2 (OCCT/2D-contours). So **MVP** = direct-2D DXF for
straight-wall cut layers + cad solid for the milled core; **target** = unified
projection-from-solid as cad's sectioning export matures. The principle is
decided now; the wiring lands with cad phase 2.

**The bridge is at layer 3 → export**, the same "rent the heavy kernel only at
export" move the keystone kinds all make — except here the rented kernel is our
own `cad`: the fluidics geometry layer **derives** one cad assembly and projects
every fab file from it, while owning the netlist / constraints / routed-2D-layout
representation itself.

### 8b. Linking cad objects to the graph — two relations, cross-kind constraint targets

Yes, `fluidics` refs and `cad` refs link (the precis `link` verb, typed
relations — same machinery as `derived-from` lineage and ADR 0027/0044 links).
But "link cad to the graph" is **two distinct relations in opposite
directions**, and conflating them is a trap:

- **`derived-from` (generated, fluidics → cad).** The whole-stack assembly and
  the molded-core solid (§8a) are *outputs* — a cad ref `derived-from` the
  fluidics design, regenerable, content-addressed (0044 derived lane). **Not
  hand-edited**; edits go to the fluidics IR and re-derive.
- **`realized-by` (authored, graph element → pre-existing cad object;** inverse
  `realizes`). A node/layer *references* a cad object that already exists as its
  geometry — a cad *input*, not an output. This is the **type/instance split
  from `pcb`** (0042): the cad object is the "component-type," the graph element
  "instances" it. Cases: a **molded core authored directly in cad** (the films
  are then designed around it); a reservoir node → a cad well/cavity; a port node
  → a cad luer/barb fitting; a valve seat or a fixture (dowel/registration pin,
  clamp boss) → a cad feature the router must respect.

**The payoff — cad features become first-class constraint targets.** Because
both sides are handle-addressable IRs with datums + probes (ADR 0036 handles,
cad **datums**), a fluidics constraint (fork 4 measure) can **target a cad
feature by handle**: "this channel's port aligns to cad datum `D` on `ca123`
within 0.1 mm." And cad's `clearance`/`interference` observers can **DRC the
fluidic geometry against the linked cad part** (channel-vs-manifold-wall,
port-vs-fitting). Linking is not mere attachment — it lets constraints and DRC
**span both kinds**.

**Consistency + invalidation.** A `realized-by` cad part is an *input
dependency*: the derived stack assembly (§8a) **composes** it — projecting its
ports/features to keep the films registered *by construction* (§8a point 4). So
a cad edit cascades: if the linked part is edited or superseded, the fluidics
derived assembly goes stale → **re-derive + re-check** the registration
constraints. cad-side change → fluidics-side recompute, through the link.

### 8c. Fluidic footprints + the pcb route seam (candidate — revises the slice-4 "not Freerouting" aside)

Freerouting needs no electrical semantics — it connects pads per a netlist under
width/clearance/layer/via rules, which *is* channel path-finding. So model
fluidic elements as **footprints** (EDA sense: pads + a body shape), reusing
pcb's component-type/instance split (§8b) and renting pcb's headless route seam
(`pcb/route.py`, DSN→route→SES) — **not** the pcb kind.

- **Each channel = its own 2-pad net** — preserves per-edge identity; never
  collapse into a PCB "net" (which erases edge/volume/valve).
- **Valve footprint** = an **in-pad and out-pad coincident at the center** (on
  the *two* channel-segment nets, so the router is *forced* to bring the channel
  through the valve center, yet geometrically seamless) **+ a body circle** (the
  pinch-finger contact / servo keepout, §3). The router routes in→out; the circle
  *is* the physical valve.
- **Composite footprints encode proximity by construction.** A pump = 3
  valve-circles at a fixed pitch in one footprint, so the pump's max-pitch +
  ordering (§6.1) becomes **footprint geometry, not a separate constraint** — it
  *absorbs* that constraint. (Reservoir / port / junction / mixer are footprints
  too — the type/instance library of §8b, fork 5's groupings given a body.)

**The unifying win — a footprint bridges both engines.** Its **pads → nets → the
router** (path-finding); its **body shapes → the cad export assembly** (§8a —
the valve circle *is* the layer-3 geometry primitive that gets
extruded/subtracted). The routed centerline + width (from the SES) unioned with
the footprint bodies → the clean channel solid.

**Rounded edges** are a footprint/trace spec and a **dual requirement**: tooling
(tool/endmill radius) *and* fluidic (bend radius, no sharp corners →
anti-bubble/DRC, §7/§6.1). The router uses **arc routing** (no hard 90°); final
tool-radius filleting is realized at cad export (caveat: cad fillets are cad
phase 2).

**Still ours — the router won't do it:** **metering** (Freerouting *minimizes*
length; the sizer adds width/serpentine to hit a target volume, §6),
**depth/cross-section** (no PCB analog), and **fluidic-specific DRC**. Division
of labor: rent the route seam for topology, keep sizer + fluidic DRC adjacent —
the same "own the IR, rent the seam" move as cad-at-export. Reinforces standalone
constraints (fork 4): the **router consumes width/clearance/layer, the sizer
consumes volume, DRC the rest** — different engines, disjoint constraint subsets.

### 8d. Footprints are multi-facet, multi-plane objects (working — the actuator plane, through-wells, glued parts)

A footprint is **not** a single 2D pad pattern — it is a **type + one placement
anchor + a set of facets**, and every facet shares that one anchor, so **placing
the footprint once registers all its facets across every plane by
construction** (the master generalization of §8a.4 projection-registration and
§8b realized-by). "How many facets" is **per-type and open-ended**; the *kinds*
of facet are fixed:

| facet | lives on | consumed by |
|---|---|---|
| **port/pad** (position, shape, net role in/out) | a fluidic layer | the router (§8c) |
| **body/geometry** (2D/2.5D shape) | a fluidic layer / the core | cad export (§8a) |
| **actuator** (poky-bit hole / clearance) | the **actuator plane** | actuator-plane export + registration |
| **part** (realized-by a physical/cad part + bond) | a mount plane | BOM + export (§8b) |
| **behavior** (port roles, flow direction, orientation) | — (non-geometric) | bubble prediction (§7), metering, compartments (§5) |

A plain channel is 2 port facets. A **valve** = coincident in/out port facets
(§8c) + a body circle + **an actuator facet** (the stepper hole). A
**through-core well** = a top-port facet + a bottom-port facet on two fluidic
layers + a vertical-bore body facet + a behavior facet. A **tube adapter** = a
port facet + a **part facet** (an adhesive part glued on).

**The actuator plane is a first-class non-fluidic plane.** Valves are driven by
steppers whose poky bits pass through an actuator plate whose holes **must align
to the valve centers**. Because the hole is an *actuator facet of the same
footprint* as the valve's fluidic pads, alignment is **automatic** — one anchor,
nothing authored twice, nothing to drift (the §8a.4/§8b registration guarantee,
now spanning a mechanical plane). So the stack model (§2) gains a **plane role**
— fluidic / structural / **actuator** / part-mount — and each role exports
differently (fluidic → channel DXF/G-code; actuator → hole-pattern DXF/G-code;
part → BOM). The plate is just another cad layer (a slab with hole
subtractions). *Caveat:* the shared anchor gives the **nominal** center; the
poky-bit-vs-hole **clearance** (the bit has slop) is still a real constraint
(fork 4), not removed by registration.

**Through-wells stay direction-*predicted*, now orientation-aware.** A
through-core well (Port A below, Port B above) is *geometrically symmetric* but
its bubble outcome is not: in-top/out-bottom **traps** a bubble; in-bottom/out-top
is **bubble-free**. So "trap: yes/no" **cannot** be baked into the footprint — it
stays a §7 *prediction* over **(well geometry × flow direction × gravity
vector)**, evaluated **per operational state** (a forward-pump trap may prime
fine in reverse; the device may even be primed in a different orientation, so
gravity can itself be per-state). The footprint supplies geometry + port
topology; the operational analysis supplies flow direction + orientation. This
adds **device orientation / gravity** as an input to §7.

**Glued parts give a fluidic BOM.** A footprint whose *part facet* is a physical
adhesive adapter is the fluidic analog of a soldered PCB component — it feeds a
**BOM** (adapters, films, adhesives, core, actuator plate, steppers): a
`pcb`-style BOM view over the part facets.

**Open** (you flagged "need to think this through"): the facet *set* per
footprint type is open-ended (this is what "how many facets" resolves to —
zero-or-more of each kind); the master invariant is **one anchor, all facets
registered**. Marked working — revisit once the plane-role taxonomy (fluidic /
structural / actuator / part-mount) firms.

### 9. Slice plan (each merges dark)

0. `fluidics` kind + **netlist IR** (nodes/edges/valves/layers/stack) +
   get/put/search + embedding-friendly render + graph probes (path,
   compartment volumes) + truism lint. *Useful before any geometry.*
1. **Constraints + checker** (met / violated / **underdetermined**) + composite
   elements + actuation programs + compartment-trajectory eval (pump stroke
   volume is a slice-1 output).
2. **Placement/geometry IR + DRC** (both-signs spacing) + **bubble prediction
   pass** (needs geometry) + a `/fluidics` 2D-per-layer SVG viewer (reuse
   `cad`/`structure` scaffolding).
3. **Sizer** as a derived job (fix depths → LP for widths/serpentine).
4. **Router** as a derived job — rent pcb's Freerouting seam over fluidic
   **footprints** (§8c: channel = 2-pad net, valve = coincident-pad footprint +
   body circle), then **meter** the routed skeleton with the sizer (slice 3).
   Revises the earlier "not Freerouting."
5. **Fab exporters** (§8a): the derived whole-stack **cad** assembly is the one
   geometry source; every file is a view of it — STEP + 3D preview (the solid),
   G-code (CAM over it), DXF (section/projection of the layer solid). **MVP**:
   direct-2D DXF for straight-wall cut layers + cad solid for the milled core.
   **Target**: unified projection-from-solid as cad's sectioning export matures
   (cad phase 2). Content-addressed derived artifacts + export bundle (mirror
   `pcb` exporters and the draft `+ sources` zip). A
   film–adhesive–moldedpart–adhesive–film design emits DXF + G-code from one
   stack.

Value compounds: reasoning tool at slice 1, viewer at 2, real fab output at 5.

**Variation mechanic** throughout: a `fluidics_propose` job (peer of
`cad_propose` / `structure_propose`) — instruction in ("halve the mixer dead
volume", "add a bypass around v3", "move control channels to the back face") →
derive a new slug → re-check → re-route.

## Fork decisions (2026-07-09)

1. **Primary fab route — BOTH.** v1 carries `adhesive`/DXF *and* `mill`/G-code,
   including the mixed `film–adhesive–moldedpart–adhesive–film` case that emits
   both from one stack. So the sizer must handle **discrete** (laminate catalog
   depth) *and* **continuous** (milled depth) together. `weld` rides as the
   third bond-type; its compliant depth is a nominal (fork 6).
2. **Valve model — servo-actuated pinch fingers** (§3, rewritten). External
   mechanical actuator, so **bi-layer control/flow is NOT a core concept**; the
   top film is the pinch surface, and valves couple to the compliant-cap model.
   Finger footprint sets valve spacing. (Rotary still excluded.)
3. **Router ambition — assisted turn-taking with constraint raising**
   (ADR 0051). Placement is a `fluidics` **persona thread** on 0051's turn
   loop: the model works the design turn-by-turn and **raises constraints** —
   surfaces the binding volume/tooling/DRC/spacing constraints as salience eyes
   / turn moves (the fluidic lint/checker piped through the render), human in
   the loop. **Auto-routing stays deferred** (§8); constraint-raising is the
   collaboration surface, not a solver verdict.
4. **Constraints storage — UNDER DISCUSSION** (only open fork). Leaning: not
   opaque inline JSONB and not a whole new kind, but **first-class addressable
   rows in the `pcb` `measure` mold** — graded design intent, re-evaluated like
   0041 observers, each a **handle** so a constraint can be *raised* and argued
   in the 0051 turn loop (fork 3) and pick up a salience eye when it binds. See
   the working note in §6. To be settled next.
5. **Macros — just logical groupings + graph-level claims** (§4, rewritten).
   *Not* a shared parameterized library, *not* an expandable part: a named
   grouping over existing primitives + logical claims. Emergent distance
   constraints are recorded **separately** as ordinary constraints (fork 4).
6. **Welded compliant channels — nominal.** Fixed nominal height in v1's sizer;
   pressure-dependent (compliant) volume is the named deferral (§8).

## Consequences

- A new keystone kind consistent with 0041/0042/0043; storage follows
  `pcb`/`structure` (dedicated relational tables + one card chunk, **not**
  chunk-native), routing/sizing/export as 0044 derived jobs.
- Reasoning value lands at slice 1 with **zero geometry** (the compartment
  mechanic + constraint checker), before the expensive router/exporter work.
- Bubbles, compliant volume, auto-routing, and rotary valves are explicitly
  bounded out of v1, each with a named re-entry point.
