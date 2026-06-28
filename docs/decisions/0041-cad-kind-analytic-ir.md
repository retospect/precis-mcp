# 0041 — The `cad` kind: analytic-IR solid design the LLM can *read*

- **Status**: proposed (2026-06-27) · **v2** (supersedes the v1 draft of
  this file after the design discussion converged on an analytic IR,
  rigid-only transforms, a generalized-frustum primitive set, a DAG of
  operator nodes, and persisted observers)
- **Deciders**: Reto + agent
- **Amendment 1 (2026-06-28)**: storage — design nodes move to a dedicated `cad_nodes` table; the design keeps **one** `card_combined` chunk for intent-search. Supersedes the “every node is a chunk” decision in §3/§9/§12. See **Amendment 1** at the end.
- **Amendment 2 (2026-06-28)**: export — the printable route is **in-process** (manifold3d CSG → hand-written STL/3MF), and **STEP is exact** (OpenCASCADE B-rep), both optional extras. Drops the OpenSCAD-binary STL route from §10 (the `.scad` text view stays). See **Amendment 2** at the end.
- **Builds on**:
  - [ADR 0033 — Drafts as editable chunk-native documents](./0033-draft-chunks-editable-document.md)
    (the mutable, soft-deletable chunk model reused for nodes — *not* the
    append-only body-chunk rule).
  - [ADR 0029 — Multi-root corpus for PDF serving](./0029-multi-root-corpus-pdf.md)
    (exported artifacts land on `PRECIS_CORPUS_DIR`).
  - [ADR 0026 — precis-web as a sibling package](./0026-precis-web-surface.md).
  - [ADR 0035 — Computed chunks & the recompute boundary](./0035-computed-chunks-recipes-and-the-recompute-boundary.md)
    (the evaluated geometry is a *derived* layer recomputed on change —
    note: 0035 is itself still *proposed*, a soft dependency).
  - [ADR 0036 — Universal handles](./0036-universal-handles.md) (node
    handles; the `cad` 2-char code is **`ca`**).

## Context

We want a CAD capability so an LLM can design solids. The hard question
is not "which CAD tool" — it is **how to give an LLM *eyes* into a 3D
solid without making it stare at pixels.**

A render (PNG) is the obvious answer and the wrong one: it needs a GL
context (a deploy foot-gun across the Mac nodes + the Linux spark box),
it is lossy, and an LLM reasons about "3 mm wall, 1.2 mm interference,
6-hole bolt circle" far more reliably — and far more cheaply in tokens —
from *structured geometry* than from a raster.

The precis-shaped answer is the one we use for papers: **structure the
model can query** (a TOC + targeted lookups), not an opaque blob. A CAD
model is a graph of analytic primitives with transforms; we answer every
"what's there?" question *analytically over that graph* and **never
compute the merged boolean solid, and never mesh, to inspect**. Merging
and meshing happen only at **export**.

### Why not lean on OpenSCAD's evaluated `.csg`?

The obvious shortcut — `openscad -o out.csg` and parse — flattens in the
ways that hurt and not the ways that help. It keeps the geometric layer
(a `cube` stays a `cube`) but loses the authoring layer (modules, loops,
variables, names) and, worse, hands us operations we **cannot evaluate
analytically** (`hull`, `minkowski`, the extrudes). Probing those forces
a mesh — exactly what we refuse. Owning the IR fixes all three and
unlocks semantic primitives (§4).

## Decision

### 1. Keystone — own the analytic IR; rent the kernel only at export

The IR — a curated set of **analytic primitives** + rigid transforms +
booleans — is the source of truth and the thing the LLM authors, reads,
and probes. **OpenSCAD and OpenCASCADE/OCCT are export backends**, not
the evaluator and not the store. The cost we accept: a small analytic
geometry kernel of our own (§9). The discipline that bounds it: **admit
only primitives we can probe analytically** (the contract, §4).

### 2. Invariants

- **Units: millimetres, `float64`.** float64 carries ~15 sig-figs
  *relative to magnitude*, so mm is sub-micron-exact from microns to
  kilometres — precision never drives the unit choice; convention and
  readability do, and both favour mm (the CAD/STEP/3D-print lingua
  franca; `50` reads better than `0.05 m`). STEP export **declares mm**,
  so SI-correctness is not lost.
- **Coincidence tolerance.** A global **linear epsilon ≈ 1e-6 mm** and an
  **angular epsilon** govern touch / coincidence / zero-clearance tests
  (needed by fusion-on-touch §3 and clearance≈0 §7). This is the
  load-bearing tunable, not the unit.
- **Rigid transforms only — translate + rotate, no scale, no shear.**
  This makes membership *and* distance **exact everywhere, with no
  caveats**. Cost: no squishing a sphere into an ellipsoid — an ellipsoid
  would be its own primitive; dimensions are always explicit per-node
  (change `w/h/l`, never scale), which is cleaner anyway.

### 3. The node model — a DAG stored as a flat list

A design is a **flat list of nodes** (= the ref's chunk set); the graph
structure lives in each node's **operand-id fields**, not in physical
nesting. The list *is* a DAG — better than a nested tree for editing
(soft-delete a chunk, re-point an operand, no structural surgery).

- **Node kinds**: **primitives** (placed leaves) and **operators**
  (`merge`, `subtract`, `intersect`; `move`; `pattern`; `instance`)
  referencing operands by id/name. So `c = subtract(a, b)` is a node;
  `a` may be referenced by several operators → a shared sub-DAG. **No
  `xor`** (vanishingly rare; `(a−b)∪(b−a)` if ever needed).
- **Instances & the DAG.** `move(child, T1)`, `move(child, T2)` — two
  placements of the *same* `child` — is the instance mechanism, and the
  moment a node has >1 referrer it is a shared sub-DAG. A **pattern**
  (`polar` / `linear` / `mirror`) is sugar for N such instances on a
  rule; it collapses to **one node** (one chunk, one TOC line —
  "6× bolt, polar r18"), which is how the bolt-circle stays legible.
- **Placement.** **Leaf primitives carry `location` + `angle` (placed at
  creation)** — flat, the pose is right there, best LLM context. `move`
  is needed *only* for groups and instances (you cannot place a composite
  or a reused def by editing leaves). Export **bakes the accumulated
  world transform** either way, so place-at-creation lowers to OpenSCAD's
  `multmatrix(...) cube(...)` cleanly. (One-off leaf = placed-at-creation;
  a *reused* def = at-origin + `move` — the common case stays flat, reuse
  opts into the indirection.)
- **Components** are **top-level named nodes**, each one physical part. A
  lone primitive is a one-node component; the assembly is the ref itself
  (its set of components + observers) — no separate assembly node.
- **Fusion.** Inside a component, children **fuse automatically** (a
  merge of overlapping/touching solids is one part). "Fuse" is a
  *membership* fact, not a geometry merge — the graph stays analytic; the
  merged B-rep is computed only at export. **Between components** there is
  no auto-fusion; overlap is *interference* (§7).

### 4. The primitive set and the membership contract

Every admitted primitive must provide, **in closed form under rigid
transforms**, the full card: `{membership, ray-intersection, distance,
plane-section, faces + normals}`. Anything that cannot (`hull`,
`minkowski`, arbitrary revolve) is **excluded by definition** — the
contract *is* the exclusion line.

**v1 set:**

- **Frustum** — the workhorse: two parallel cross-sections + straight
  ruled sides, parameterized by `(profile, bottom_size, top_size,
  height)`. One analytic kernel, exposed under named aliases:
  - **box** = rectangular profile, top = bottom (a *rectangular* frustum;
    rectangles need `w×d`, so "rectangular" is a distinct profile from the
    regular n-gon);
  - **cylinder** = circular, top = bottom; **cone** = circular, top = 0;
    **truncated cone** = circular, top < bottom;
  - **hex / n-gon prism** (nuts, bolt heads) = regular n-gon profile;
    **pyramid** = n-gon → point;
  - **any taper** = top ≠ bottom — and the **side-face tilt *is* the draft
    angle**, so draft analysis = read the face normal (§6), for free.
- **sphere**, **torus** (curved; separate from the ruled frustum).
- **Edge treatment: chamfer** (a planar bevel = a half-space cut) — fully
  analytic, composes with the frustum/boolean machinery, no rounded SDF.
  **Rounds/fillets are deferred**; a *general* edge-fillet (arbitrary edge
  of the merged solid) is **export-time OCCT** only (it needs the merged
  B-rep + a rolling-ball blend kernel — the classic fragile part — and the
  probe would see the un-filleted geometry, an accepted tradeoff since
  edge fillets rarely move clearance/wall answers).
- **Phase 2: `thread`, `gear`** — **precise** (full helical / involute
  geometry, exact analytic membership — *not* an envelope, so the probed
  model equals the exported model). They are the heaviest to implement
  (each needs a precise prober **and** a precise export generator that
  must agree), hence phased last; §1's "semantic primitive" payoff lands
  here.

### 5. Datums, names, handles

- **Datums** — named **axis / plane / point** reference geometry, **auto
  -exposed by primitives** (a cylinder exposes its axis; a box its faces /
  centre-planes) and creatable by hand. Observers and operators reference
  them by name — `dof(shaft, about=shaft.axis)` reads naturally and stays
  correct as `shaft` moves, so the LLM never re-derives coordinates.
- **Names** — optional, **unique per design**; surfaced in TOC / section /
  clearance reports ("min gap: shaft ↔ bore") and usable as references.
- **Handles** — every node is a chunk, so it gets a chunk-anchor; the
  design ref gets a **`ca…`** universal handle (ADR 0036). A node is
  addressable three ways: `name`, node handle (`ca3`), or chunk anchor;
  `name` is a friendly alias over the id.

### 6. The eyes — a probe ladder, one mechanism

> **A probe is a parametric path through space; membership along it is
> interval arithmetic.**

**All probes are full-DOF — nothing is axis-locked.** A ray is `origin +
direction`, a section plane is `point + normal`, an arc is `center +
axis + radius` (any orientation); every test inverse-transforms into the
probe's frame first, so an angled probe costs exactly what an
axis-aligned one does.

- **0D — point.** Containing node(s), or nearest in increasing distance.
- **1D — line / ray.** The workhorse: material-vs-void intervals along the
  ray with each void **attributed to the node that removed it**.
  Thickness, clearance, interference length all fall out. The point is
  `t=0` on a ray.
- **1D — arc / radial.** Marched in θ → angular intervals. The instrument
  for lathed / radial features — bolt circles, gear teeth, radial slots —
  that the linear ray is blind to.
- **2D — section.** Plane ∩ graph → 2D loops, exact, no merge —
  **feature-attributed** (each loop tagged with its contributing node(s);
  a *fused* loop lists its whole contributor set). A labelled floor-plan,
  not polygon soup.

**Subtraction is visible without a merge — a correctness guarantee.**
Because we fold per-primitive results through the boolean ops
(`merge=any`, `subtract=first ∧ ¬rest`, `intersect=all`), a probe in a
carved region reports *empty* and names the **blocking node** ("empty;
removed by `bolt#1`"). Testing primitives independently would wrongly
report material in the hole — walking the ops is what makes a drilled
bore read as a bore.

**Derived back-refs.** On eval each node carries `fused-with` / `cut-by`
links, so the boolean relationships are navigable from any chunk.

**Draft check** is just another probe: given a pull direction, `normal ·
pull` per face flags faces below the minimum (a vertical wall = 0° draft
= a release failure). Frustum tapers give draft directly.

**Out of scope for the eyes: pixel rendering / 2D projection.** A
faithful elevation needs the merged solid or a depth buffer (≈
rendering); that is external — **export the solid(s), raytrace or draft
elsewhere** (§10). Section *SVGs* are exact planar geometry, not a render,
and stay.

### 7. Relational analysis — clearance, interference, motion

Two components built at real dimensions are *analyzed*, not declared
(there is **no `fit` object**):

- **Clearance / interference** — the signed gap: negative = press /
  interference, ≈0 = line-to-line, positive = clearance. A "press fit" is
  simply *"clearance = −0.02 mm"*; whether that is intended is the **LLM's
  judgement**, not a stored flag. Interference is the negative-clearance
  case of the same machinery and is the eager check on `put` (§11).
- **Degrees of freedom (v1: translation; rotation phase 2)** — how far A
  can translate along x/y/z before colliding with B (analytic directional
  clearance, min over primitive pairs). Rotational freedom about an axis
  (does the bearing spin?) is analytic only for symmetric cases and
  sampled otherwise → **phase 2**.

These are **persisted `observer` nodes**, re-evaluated as the design
changes — so a clearance or DOF check stays pinned and "tries again"
after each edit. Observers thus split into *geometric* (point / ray / arc
/ plane) and *relational* (clearance / interference / dof); both are
first-class nodes alongside components.

### 8. Exact vs sampled

Under the rigid-only invariant, the whole probe ladder + clearance +
translational DOF + draft are **exact** — no asterisks. The only
**sampled** quantities are bulk integrals — **volume, centroid** (and,
with a density, *mass* — deferred to phase 2; v1 reports geometric volume
only). **Connectivity** ("did a cut split the part?") is **dropped from
v1** (not needed until cuts get complex; it would require sampling or the
merged solid). Sampled results are labelled as such.

### 9. Evaluation & storage

- **Storage = the chunk set**; the DAG lives in operand-id fields. Query =
  **ask each primitive its local result, then fold through the ops** in
  topological order. At typical part sizes (tens of primitives) this O(N)
  scan is instant; pairwise interference is O(components²) with few
  components.
- **Loop evaluator now; vectorized / SoA later, scoped to the bulk tier
  only** (voxel volume/centroid, any future ray-grid raster) — a pure
  evaluator swap behind the same node-list, so it is a free deferral; the
  "object-id column" is how attribution survives a vectorized pass.
- **No spatial index** (BVH / octree) until parts get big — an AABB
  pre-filter is the only cheap win and even that is optional. Linear scan
  is fine into the low thousands of primitives; past that, the AABB-BVH or
  the vectorized path is the lever. **`log()` any cap** so "covered the
  whole part" never silently means "scanned the first K".

### 10. Cadence & export

- **On change**: re-evaluate the graph (cheap, ours, no GL, no mesh) — the
  derived-index cascade (ADR 0035).
- **On observation**: probes computed lazily from the cached eval.
- **On export** (STL-via-OpenSCAD first to prove the loop; **STEP/OCCT
  fast-follow**): two modes —
  - **assembly** — a set of B-rep solids, one named solid per component;
    **STEP carries multiple solids natively**, so the assembly travels as
    one file un-flattened;
  - **merged solid** — one watertight B-rep (→ STEP via OCCT) for any part
    containing subtractions (an external consumer, unlike our probe, does
    not know B was removed from A unless we merge); `.scad` → STL via
    OpenSCAD is the lossy fallback. **Meshing lives only here.**
- **Drawings are downstream, by hand** — dimensioned orthographic / section
  drawings come from a CAD tool consuming the STEP (e.g. FreeCAD
  *TechDraw*); precis emits solids, not drawings or pixels.

### 11. Verb surface & TOON output

Authoring is `put`; reading is `get` with views. Homogeneous row-lists
come back as **TOON** (`{header}` + tab-separated rows); a single node is
**JSON** (per `precis-toon`). The `config` string is a compact typed
mini-DSL (`box:w40d20h10`, `cyl:r3h12`, `frustum:n6rb4rt2h5`,
`chamfer:1x45`); grammar lives in the skill.

**Insert** — eager validation in the result:

```
put(kind='cad', parent='ca1', name='bore', config='cyl:r3h12',
    location='x10y10z-1', op='cut')
→ {handle  name  role  shape  dims  pose  check}
  ca3       bore  cut   cyl    r3 h12  @10,10,−1  ok      # or "⚠ interferes …"
```

**See the whole design tree** — `get(kind='cad', id='ca1')`; DAG encoded
as the root-row expression + flat node rows; a pattern is one row:

```
# flange — 1 part · 5 nodes · bbox Ø50 × 8 mm
{handle  name      role         shape   dims          pose}
ca1      flange    result       —       merge ca2 − ca3,ca4,ca5   —
ca2      plate     add          cyl     r25 h8        @0,0,0
ca3      hub_bore  cut          cyl     r8 h10        @0,0,−1
ca4      bolts     cut·pattern  cyl     r2.5 h10 ×6   polar r18 z
ca5      cham      cut          chamfer 1×45°         hub_bore.top
```

**See the line** (ray) — unified probe columns, axis named in the header:

```
probe ray o(−30,0,4) d(+x) — crosses plate, center bore, 2 bolt holes
{x_in  x_out  len   state  feature}
−25    −20.5  4.5   solid  plate
−20.5  −15.5  5.0   void   bolt#4
−15.5  −8     7.5   solid  plate
−8     8      16.0  void   hub_bore
8      15.5   7.5   solid  plate
15.5   20.5   5.0   void   bolt#1
20.5   25     4.5   solid  plate
```

**See the round line** (arc) — same shape, θ axis:

```
probe arc c(0,0,4) axis=z r=18 — solid plate, 6 voids @ 60° (Ø5) → bolt circle
{θ_in  θ_out  span  state  feature}
−8     8      16°   void   bolt#1
52     68     16°   void   bolt#2
…
292    308    16°   void   bolt#6
```

**See the point** (degenerate ray):

```
probe point (18,0,4) — empty
{handle  name    state          measure}
ca2      plate   would-contain  —
ca4      bolt#1  removed-by     2.5 mm inside the cut
```

**See the plane** (section) — feature-attributed loops:

```
section z=4 — outer Ø50 · 7 holes · area 1593 mm²
{loop   handle  name      shape   geom}
outer   ca2     plate     circle  Ø50 @0,0
hole    ca3     hub_bore  circle  Ø16 @0,0
hole    ca4     bolt#1    circle  Ø5  @18,0
…
```

**Find a part** — by handle `get(id='ca3')`; by name (per-design unique)
`get(id='flange#hub_bore')`; by path `get(id='flange/bolts/bolt#3')`. A
single node returns JSON, including derived back-refs:

```json
{"handle":"ca3","name":"hub_bore","role":"cut","shape":"cyl",
 "dims":{"r":8,"h":10},"pose":{"at":[0,0,-1]},
 "parent":"ca1","cut_from":["ca2"]}
```

Probes are reachable as `get(... view='probe', args={point|ray|arc})`,
`get(... view='section', args={plane})`, `get(... view='clearance'|'dof',
args={a,b,…})`. Shared `state` vocabulary: `solid` / `void` for paths,
`contains` / `removed-by` / `would-contain` for points.

### 12. Mapping onto existing precis machinery

- **Kind & gating.** `kind='cad'`, `corpus_role='none'`. Gated on **binary
  presence at export** (`shutil.which('openscad')` / OCCT) — the gate
  guards *export*, not the design loop, which has **no external
  dependency**. **In-tree, not a plugin** (the entry-point surface covers
  only the handler; the export worker + web viewer want to be in-tree).
- **Chunks.** Each node is a soft-deletable chunk (`deleted_at`), the ADR
  0033 editable-document model — *not* the append-only body rule;
  re-evaluating the graph on change is the cascade.
- **Search.** The embedded `card_combined` = component names + an
  LLM-authored one-line description + overall bbox dims, so
  `search(kind='cad', q=…)` works on intent, not geometry.
- **Export worker.** A `job_type` like `workers/job_types/draft_export.py`
  — shells out (OpenSCAD/OCCT) as `export/compile.py` shells to
  `latexmk`, returns a `CompileResult`-shape with `log_tail`, records
  artifact paths in `refs.meta`; artifacts on `PRECIS_CORPUS_DIR`.
- **Web.** Section SVGs + probe results as the primary view; an optional
  three.js viewer for the human (vendored like pdf.js). The agent never
  needs it.
- **Agent loop.** `put` → re-evaluate → probe → edit on the `job` /
  `plan_tick` substrate; each design is a persisted, linked artifact
  (`derived-from` a paper, `produced-by` a todo).
- **Skill.** `precis-cad-help` (verb mechanics + the `config` DSL +
  probe/observer model); index rows in `precis-overview` / `precis-help` /
  `precis-toolpath-help`.

## Phasing

- **v1**: frustum (rectangular + n-gon/circular) + sphere + torus;
  chamfer; `merge`/`subtract`/`intersect`, `move`, `pattern`, `instance`;
  point/ray/arc/section probes; clearance + interference + **translational**
  DOF; datums; persisted observers; eager `put` validation; STL export;
  volume (geometric). mm / float64 / rigid-only.
- **Phase 2**: `thread` / `gear` (precise); **rotational** DOF; rounds /
  fillets + general edge-fillet (export-time OCCT); STEP via OCCT;
  named-definition library parts; vectorized bulk evaluator; a 2D contour
  sub-IR feeding `revolve` / `extrude`; mass (density).

## Non-goals

- Pixel rendering / 2D projection / raytracing (export, render elsewhere).
- General arbitrary-edge filleting in the design loop (export-time only).
- `hull` / `minkowski` (fail the membership contract).
- `xor`; non-uniform scale / shear; `n > 3` dimensions; a 2D sketch layer
  in v1; connectivity checks in v1; material / density / mass in v1.

## Consequences

- **Good**: the LLM gets *legible* geometry — TOC, feature-attributed
  sections, exact ray/arc measurements, clearance / interference / draft —
  at a fraction of a render's tokens; subtraction correct without a merge;
  **everything exact** (rigid-only kills the distance caveat); **zero GL**
  in the design loop; storage and evaluation are dead-simple (flat
  chunk-list, linear fold); reuses the draft / chunk / job-export
  machinery wholesale.
- **Cost / risk**: we own an analytic geometry kernel (bounded by the
  membership contract); semantic primitives (thread/gear) cost double
  (prober + export generator that must agree) and are phased last;
  export-time fillets mean probed geometry can differ slightly from the
  exported part; no-2D-sketch limits arbitrary swept profiles (build from
  solids instead) until the phase-2 contour layer.
- **Deferred**: threads/gears, rotational DOF, fillets, STEP/OCCT, library
  instancing, vectorized bulk eval, 2D contours, mass.

## Open questions

1. **TOC rendering of deep CSG** — the flat-row + root-expression shape
   works for the flange; confirm it stays legible for deeper subtract /
   merge / nested-pattern trees (may want collapse/drill like the paper
   TOC).
2. **`config` mini-DSL grammar** — finalize the per-shape letter
   conventions in `precis-cad-help`.
3. **Versioning / concurrent edits** — inherit the `draft` (0033) model;
   confirm no extra machinery is needed for two ticks editing one design.


## Amendment 1 — node storage: a dedicated table, one search card (2026-06-28)

**Supersedes**: the §3/§9/§12 decision that *each node is a chunk*.
**Status**: accepted (by decree). **Keeps everything else** in this ADR
(the IR, the probe ladder, the relations, export) unchanged.

### Why

The original plan stored every node as a `chunks` row, to reuse the
embedding / handle / soft-delete machinery. But the chunk **indexers
claim work by a derived-queue join with no kind filter**: `EmbedHandler`
(`workers/base.py:claim_batch`) embeds *any* chunk missing a vector
(skipping only `chunk_kind='references'` and `meta.no_index='true'`),
`chunk_keywords` runs KeyBERT on any chunk ≥150 chars, the `chunks.tsv`
column is `GENERATED ALWAYS`, and the dream/salience attention rotation
sweeps chunks broadly. So a `cad_node` chunk earns a **1024-dim BGE-M3
vector, KeyBERT keywords, a tsvector, and salience rotation** — none of
which anyone wants, because *nobody semantic-searches for `frustum5`*.
Keeping nodes as chunks would mean teaching every present and future
indexer to skip `cad_*` (scattered, fragile) — the same namespace-
pollution trap ADR 0029 (alert) called out.

### Decision

Split storage by *what is actually a search target*:

1. **The design is a `ref`** (`kind='cad'`, slug, links, tags, handle
   `cd<id>`). Design-level metadata (units, default tolerances,
   provenance) lives on `refs.meta`.
2. **The design keeps exactly one embeddable unit**: a single
   `card_combined` chunk (`ord<0`) per design, whose text is an
   auto-built summary — title + component names + **node names**
   (`hub_bore`, `bolts`, …, which carry the author’s intent) + distinct
   shapes + bbox. This is the *right* use of refs+chunks: **one vector
   per design**, so `search(kind='cad', q='6-bolt flange')` works on
   intent and participates in `search(kind='*')` fan-out — while the
   geometry never touches the embedding DB.
3. **Nodes move to a dedicated `cad_nodes` table** — structured
   geometry, never embedded:

   ```sql
   CREATE TABLE cad_nodes (
       node_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
       ref_id     bigint NOT NULL REFERENCES refs(ref_id) ON DELETE CASCADE,
       ord        integer NOT NULL,            -- author / eval order
       name       text NOT NULL,               -- per-design unique label
       component  text NOT NULL,               -- owning part
       op         text NOT NULL,               -- add | cut | intersect
       config     text NOT NULL,               -- the mini-DSL shape string
       loc        double precision[] NOT NULL DEFAULT '{0,0,0}',
       rot        double precision[] NOT NULL DEFAULT '{0,0,0}',
       pattern    jsonb,                        -- polar/linear sugar, or NULL
       operands   bigint[],                     -- explicit DAG (forward-compat)
       retired_at timestamptz,                  -- ADR 0033 soft-delete, kept
       created_at timestamptz NOT NULL DEFAULT now(),
       UNIQUE (ref_id, name)
   );
   ```

### Handles

Node code `ca` (ADR 0036) now resolves against `cad_nodes`, not
`chunks`: `parse('ca7')` still yields `('cad', True, 7)` for the handler,
but the cross-kind `universal_chunk` reader skips `cad` (a node is read
via `get(view=…)`, not the chunk hover — a web node view is phase-2).
The design ref handle `cd<ref_id>` is unchanged.

### Consequences

- **Zero indexer pollution**, no skip-list maintenance, no future-worker
  landmine; the schema is indexed for “nodes of design X in order.”
- `operands[]` gives the §3 shared-sub-DAG / instance model a real home
  (v1 still folds sequentially; the column is forward-compat).
- **Blast radius is the persistence layer only**: the pure kernel
  (`scene`/`primitives`/`fold`/`probe`/`relate`/`bulk`) is DB-agnostic
  and untouched. `0041`’s migration swaps the three `cad_*` chunk_kinds
  for the `cad_nodes` table; `_cad_ops` reads/writes the table + the one
  card; the handler resolves `ca` against it.


## Amendment 2 — export backends: in-process mesh + exact STEP (2026-06-28)

**Refines §10** ("Cadence & export"). Keeps the principle (meshing lives
only at export; the analytic IR + probes never mesh) and the OpenSCAD
*text* form; **changes the kernels**.

### Why

§10 sketched "STL-via-OpenSCAD first, STEP/OCCT fast-follow." Shipping it
revealed two problems with leaning on the OpenSCAD *binary*: (1) it is an
external desktop app on `PATH` — a fragile, heavy runtime dependency for
what should be a library call; and (2) OpenSCAD is a **mesh-only** kernel
(CGAL/Manifold), so it *cannot* emit STEP at all. STEP is a
boundary-representation (exact surfaces) interchange; reaching it requires
a B-rep kernel regardless. So the binary buys us a faceted STL and nothing
toward the STEP goal §10 already committed to.

### Decision

Three routes, by fidelity vs dependency weight:

1. **`.scad` text** (`get(view='scad')`) — pure, **zero-dependency**,
   always available; the human/debug form (drop into the OpenSCAD GUI).
2. **Printable mesh** (`view='stl'|'3mf'`) — **in-process**: precis
   tessellates each analytic primitive (`precis.cad.tessellate`, numpy
   only, watertight + outward-oriented via a signed-volume check), folds
   the boolean DAG with **manifold3d** (the same robust CSG kernel
   OpenSCAD now uses, as a library), then **hand-writes** binary STL and
   3MF (zip/XML) — no external app, no `trimesh`. 3MF is the modern
   slicer format; STL the universal fallback. Extra: **`[cad-export]`**.
3. **Exact STEP** (`view='step'`) — **OpenCASCADE** B-rep via the
   **raw `OCP`** binding (the bare OCCT pip wheel `cadquery-ocp-novtk` —
   no build123d, no system OpenCASCADE): each primitive maps 1:1 onto an
   OCCT make-primitive,
   our rigid `Transform` is exactly a `gp_Trsf`, the DAG folds with
   `Fuse`/`Cut`/`Common`, and `STEPControl_Writer` serialises mm B-rep
   (`MANIFOLD_SOLID_BREP`, `ADVANCED_FACE`). Extra: **`[cad-step]`** (heavy
   — OCCT is hundreds of MB, so it is **out of `[all]`**, like the torch
   paths). `[cad-export]` *is* in `[all]`.

**Drops** the OpenSCAD-binary STL route (`export_stl` + `openscad_available`).

**Assembly (multi-solid) export.** Honouring §10's "assembly travels
un-flattened": components are *not* fused across each other. STEP writes
each component as its own **named** `MANIFOLD_SOLID_BREP` via the XCAF
document model (`STEPCAFControl_Writer`); 3MF writes one named `<object>`
per component. STL/`.scad` cannot express part identity, so there the
components are welded into one solid. (Earlier drafts fused all
components into a single body on every route — corrected here.)

### Consequences

- No external-app dependency on the printable path; STEP — which §10
  already promised and the binary could never deliver — is now real and
  exact, fed by the same kernel-agnostic IR.
- Both kernels are import-gated optional extras; a missing one surfaces as
  `Unsupported` with an install hint, never a crash. The design/probe loop
  imports neither.
- The tessellator and both backends consume the **same** `SceneSpec` fold
  order as `build_design`, so every export route renders the geometry the
  agent probed. Verified: a flange (box + cyl + frustum-loft + 6-bolt
  polar pattern, with subtractions) round-trips to a watertight mesh
  (bbox-exact) and to a 1.6k-entity STEP B-rep.

