---
id: precis-cad-help
title: precis — the CAD kind (analytic solid design you can read)
summary: author a parametric solid as a text node-list, then probe it analytically (point/ray/arc/section/volume) — no meshing, no pixels; STL/3MF/STEP/SCAD are downstream exports; assembly (ports/mates/joints) and build planning (make-tree/mass/BOM) are sibling skills
answers:
  - how do I author a parametric solid model as text?
  - how do I export a CAD design to STL / STEP / SCAD?
  - how do I probe a design — find a point, section, or volume?
applies-to: get/search/put/delete (kind='cad')
status: active
tags: [design]
kinds: [cad]
---

# precis-cad-help — design solids the LLM can *read*

A `cad` design is a **boolean DAG of placed analytic primitives** (ADR
0041). You author it as text, and instead of staring at a render you
**probe it analytically** — "what's along this ray?", "what's the gap
between shaft and bore?", "what's the section at z=4mm?". Postgres is
canonical; SCAD/STL/3MF/STEP export is a regenerable downstream view.
**Every dimensioned number in the source needs an explicit unit** — a
length (`mm`, `cm`, `in`, `Å`, pint's long tail — internally SI metres) or
an angle (`deg`/`rad` — internally SI radians); a bare number is refused
with a retry hint (the zero-counting / silent-exponent-slip guard). Reads
(probe/view output) render back through the shared neat formatter
(`2.3 mm`, `1.2 kN`) — never a bare, unit-implied number. Transforms are
**rigid** (translate + rotate, no scale), so every probe is **exact** (only
volume/centroid are sampled).

Four verbs, no new ones: `put` (create/replace a design), `get` (list /
node tree / one node / a probe), `search` (by **intent** — see below),
`delete`
(soft-retire).

## Author a design — `put(id=<slug>, text=<source>)`

The `text` is a small line language, **one node per line**:

```
<name>  <op>  <config>  [@x,y,z]  [rot:rx,ry,rz]  [polar:nNrR | linear:nNdx..dy..dz..]
```

- `<op>` is `add` (additive), `cut` (subtract), or `intersect`. The
  **first** node in a part is its base; later `add` merges, `cut`
  subtracts, `intersect` intersects.
- `<config>` is the **mini-DSL** (see below).
- `@x,y,z` places the node (default origin, one length unit per
  component — `@0mm,0mm,-1mm`); `rot:rx,ry,rz` rotates it (one angle unit
  per component — `deg` or `rad`, e.g. `rot:0deg,0deg,45deg`). `polar:`/
  `linear:` replicate it into one pattern node.
- `component <name>` opens a part; nodes belong to it until the next
  `component` line. Default part name is `part`. Node names are unique
  across the **whole design**, not per component — reusing `plate` in two
  components raises `duplicate node name`; prefix them (`lid_plate`,
  `base_plate`).
- `#` starts a comment.

**All angles in `cad` are degrees on display** — `rot:rx,ry,rz`, the
`polar:` even spacing (360°/N), and the `arc` probe's θ output all read
back in degrees; at the text boundary every angle still needs its own
explicit unit (`deg` or `rad`), stored internally as radians. Lengths need
an explicit unit too (any of `mm`/`cm`/`m`/`in`/`Å`/…, stored internally as
metres) and read back through the neat formatter (`2.3 mm`, not a bare
float). (The `calc` kind defaults to degrees too — see the tip below.)

```python
put(
    kind="cad",
    id="flange",
    text="""
component flange
plate     add  cyl:r25mmh8mm
hub_bore  cut  cyl:r8mmh10mm    @0mm,0mm,-1mm
bolts     cut  cyl:r2.5mmh10mm  @18mm,0mm,-1mm  polar:n6r18mm
""",
)
```

### Reuse a design — `use <slug> as <name>`

A design can **instance another design** as a sub-assembly, so a machine is
built from parts you already authored instead of one flat node list:

```python
put(
    kind="cad",
    id="deck",
    text="""
component base
slab  add  box:w60mmd60mmh4mm
use standoff as sw  @-20mm,-20mm,4mm
use standoff as se  @20mm,-20mm,4mm
use standoff as nw  @-20mm,20mm,4mm  rot:0deg,0deg,90deg
""",
)
```

`use` is a **top-level directive** like `component` — it doesn't join or
close the component block above it. `@x,y,z` / `rot:` pose the whole
sub-assembly, and `polar:` / `linear:` replicate it (`use bolt as b
@18mm,0mm,0mm polar:n6r18mm` is six bolts).

The sub-design's parts arrive **namespaced** under the instance name, and
that is what every probe answers in: `standoff`'s `post` component becomes
`sw.post`, its `pillar` node becomes `sw.pillar`. So `clearance`,
`connectivity` and `dof` treat an instanced part as the real separate body
it is, and the exports carry it as a named body like any other component.

- The **stored** design keeps the one compact `use:` line (that's what
  `get(id='deck')` shows); the inlining happens on the way into every
  probe/export.
- Instancing a design that doesn't exist — or was `delete`d — is a hard
  error, never a silently-missing part. Put the sub-design first.
- Cycles (`a` uses `b` uses `a`) are refused by name.
- Edit the sub-design and every assembly using it picks the change up on
  its next read; there is no stale copy to re-sync.

## Assemble parts — ports, mates, joints

See [[precis-cad-assembly-help]].

## Attach analysis results — `link` `rel='analyzed-by'`

An analysis number (FEA stress, a multiphysics result — stored as a
`finding`, later `estimate`) attaches to the design it describes:

```python
link(kind="cad", id="bracket", target="finding:189542", rel="analyzed-by")
```

The attach **pins the design version** (a content sha of the source) into
the link. If the design's geometry later changes, the analysis is stale
and the system says so loudly: `view='links'` appends
`⚠ STALE analyses (re-run or detach): …`, and the hourly `analysis-stale`
condition check files/auto-closes an alert per stale attachment. Re-run
the analysis and re-attach (same call — the pin refreshes), or
`mode='remove'` to detach. A whitespace-only re-save does not trip it —
staleness is content-driven. Put the analysis's assumptions in the finding's `scope=` dict — that is
the validity boundary a reuser checks. Recommended keys: `fidelity`
(analytic|fea|multiphysics|mlp|dft…), `engine`, `loads`/`constraints`
(named by port where possible), `temp_range`. Keep values short and
structured — prose scope values fork hubs that should converge.

File exports are version-anchored the same way: `view='stl'|'3mf'|'step'`
records the design version it wrote, so a drifted artifact is detectable
(`design version <sha> recorded` in the reply).

## Plan the build — dimensions, mass, BOM, printability

See [[precis-cad-build-help]].

## Author a design — description and the config DSL

### Describe what it's *for* — `desc:` / `use:`

Add free-text lines so the design is findable by purpose, not just by
shape (they're folded into the one search card, ADR 0041 Amendment 1):

```python
put(
    kind="cad",
    id="bracket",
    text="""
desc: L-shaped mounting bracket for a temperature sensor
use:  bolts the sensor housing to the reactor backplate
component bracket
base  add  box:w40mmd40mmh5mm
hole  cut  cyl:r3mmh6mm  @10mm,10mm,-1mm
""",
)
```

`desc:` = what it is; `use:` = what it's for. Both are optional and may
appear anywhere in the source.

`put` builds the design eagerly, so a bad shape or geometry surfaces
immediately, and the result echoes the node tree plus any
**interference** warning between parts. Re-`put`ting the same slug
**replaces** it (old nodes soft-retired, recoverable).

### The `config` mini-DSL

Every key but `n` (a dimensionless count) needs an explicit length unit;
`chamfer`'s `angle` needs an explicit angle unit (`deg`/`rad`):

| shape | grammar | example |
|-------|---------|---------|
| box | `box:w<W>d<D>h<H>` | `box:w40mmd20mmh10mm` |
| cylinder | `cyl:r<R>h<H>` | `cyl:r3mmh12mm` |
| cone | `cone:r<R>h<H>` | `cone:r5mmh8mm` |
| truncated cone | `tcone:rb<RB>rt<RT>h<H>` | `tcone:rb6mmrt2mmh5mm` |
| hex prism | `hex:r<R>h<H>` | `hex:r5mmh10mm` |
| n-gon prism | `ngon:n<N>r<R>h<H>` | `ngon:n6r5mmh10mm` |
| n-gon frustum | `frustum:n<N>rb<RB>rt<RT>h<H>` | `frustum:n6rb4mmrt2mmh5mm` |
| pyramid | `pyramid:n<N>r<R>h<H>` | `pyramid:n4r5mmh8mm` |
| sphere | `sphere:r<R>` | `sphere:r6mm` |
| torus | `torus:R<major>r<minor>` | `torus:R10mmr2mm` |
| chamfer bevel tool | `chamfer:<size><unit>x<angle><unit>` | `chamfer:1mmx45deg` |

Numbers accept scientific notation: `box:w3e-9md3e-9mh3e-10m` (nm-scale
without ten zeros — or just say `nm`/`Å` directly: `box:w3nmd3nmh1Å`).

All are placed base-at-`z=0`, centred on the local axis; `@x,y,z` and
`rot:` set the world pose. The convention is **mixed** — centred in x/y,
based in z: `box:w5mmd5mmh0.3mm @0mm,0mm,0mm` occupies x and y in
[−2.5, 2.5] mm but z in [0, 0.3] mm. To centre in z too, offset by −h/2
(`@0mm,0mm,-0.15mm`).

`chamfer` is an unbounded half-space *tool*, not a solid: `cut` /
`intersect` only, never a component's first node. Its cutting plane sits
`size` along −normal from the node origin, tilted `angle` from local
+z — pose it with `@`/`rot:` onto the edge to bevel (patterns apply).
Exports and the viewer substitute a finite clamped box automatically.

## Round it — `rd<len>`, `blend:<len>`, `field:<sha256>`

**Rounding.** Add `rd<len>` to any convex solid but the sphere/torus
(`box:w40mmd20mmh10mmrd2mm`, `cyl:r5mmh10mmrd1mm`) to round **every edge
and corner** of that node to radius `rd` — exact in the kernel (the shape
is built shrunk by `rd` and its signed distance offset back out), so the
bounding box is unchanged, the base stays at `z=0`, and probes see the
round. `rd >= ½·min dimension` is refused by name (a thin feature would
vanish — never clamped); a cone/pyramid apex becomes a sphere cap below
the sharp tip. Rounding only opens *convex* corners: where an `add` node
meets its part, the inside corner stays sharp — put `blend:<len>` on that
`add` line (`rib add box:… blend:3mm`) to fold it in with a smooth-min of
that width, a fillet-*like* seam that is **not an exact radius** (the
render says so; `cut`/`intersect` and a part's first node refuse it).
Either key switches `stl`/`3mf` export from the analytic mesh to the
**sampled-field backend** (narrow-band marching cubes over the exact
SDF at `args={'pitch': '0.2mm'}` — pass the layer height you'll print
at; default ≈ 1/256 of the design's diagonal; a pitch whose band would
exceed the sample budget is refused, never coarsened). Sharp designs
export exactly as before; `step` has no field route yet.

**Sampled field.** `field:<sha256>` is a leaf whose shape is a stored
signed-distance grid — the way an optimiser's result (SIMP density,
`realize(strategy='simp')`) or any voxel body enters a design; it has no
dims, poses like any node, and folds into `add`/`cut`/`intersect` with
analytic nodes exactly at the surface (`body add field:3f9a…` then
`bore cut cyl:…` gives a bore at the exact radius). The grid is never in
the source: a Python caller stores it (`store.put_field(ref_id, field)`
→ the sha), the DSL names it, `>= 12` hex chars resolve on `put` and
the full hash is what the design keeps. The tree row shows shape @
pitch (`~` = sign-correct, not re-distanced); the field is trusted only
inside its own box. Rounding a field is done on the grid before it is
stored — `precis.cad.fieldops`: `redistance` (exact Euclidean SDF),
`open(r)` (rounds convex edges; **reports** every strut/blob thinner
than `2r` it erased, never silently), `close(r)` (exact concave fillet,
fills necks), `from_density(rho, pitch=…, origin=…)`. `rd` on a field is
refused; export is always the field backend; `step` refuses it.

## Read the design — `get`

```python
get(kind="cad")  # list all designs
get(kind="cad", id="flange")  # the node tree (TOON: handle name part op config pose)
get(kind="cad", id="ca7")  # one node as JSON (handle = ca<chunk_id>)
```

A node is addressed by its **`ca<chunk_id>` handle** (shown in the tree).
The bare get also appends the design's **one-hop links** (assembly
`contains`, attached analyses, make-trees, `realized-by` parts — capped)
plus a `⚠ STALE analyses` warning when a pinned analysis has drifted —
`view='links'` has the uncapped detail + coverage lints.

## Probe it — `get(view=…, args={…})`

All probes are full-DOF (any origin / direction / orientation). Pass the
geometry in `args=`. `args.component` scopes to one part (default: the
whole design). **Positions carry an explicit unit per component** (`p`,
`o`, `c`, `z`, the scalar `r`); **directions don't** (`d`, `axis` — bare
numbers, no length scale of their own, normalized internally).

```python
# 0D — classify a point: containing node(s), or (if carved) the blocker + nearest
get(kind="cad", id="flange", view="point", args={"p": ["0mm", "0mm", "4mm"]})

# 1D — ray: material/void intervals, each void attributed to the node that removed it
get(kind="cad", id="flange", view="ray",
    args={"o": ["-30mm", "0mm", "4mm"], "d": [1, 0, 0]})

# 1D — arc: angular intervals around an axis (bolt circles, radial features)
get(
    kind="cad",
    id="flange",
    view="arc",
    args={"c": ["0mm", "0mm", "4mm"], "axis": [0, 0, 1], "r": "18mm"},
)

# 2D — section at z=const: feature-attributed loops (outer / hole)
get(kind="cad", id="flange", view="section", args={"z": "4mm"})

# bulk — geometric volume + centroid (SAMPLED, labelled with ±error)
get(kind="cad", id="flange", view="volume")
```

A carved region reads **empty** and **names the blocking node** ("empty;
removed by hub_bore") — subtraction is visible without ever merging the
solid.

## Find a design — `search`

```python
search(kind="cad", q="6-bolt flange")  # by intent (hybrid)
search(kind="cad", q="sensor bracket", mode="semantic")  # by meaning
search(kind="cad", q="flange", mode="lexical")  # keyword
```

Each design carries **one** embeddable summary card (title + component +
node names + your `desc:`/`use:` text + bbox), so search lands on intent,
not geometry — and `cad` joins the cross-kind fan-out `search(kind='*',
q='…')`. Hits are design-level: the handle is the design ref `cd<id>`
(open it with `get(id='<slug>')`), never a node.

## Export — `get(view='scad'|'stl'|'3mf'|'step')`

Export is the only place geometry is meshed; the design/probe loop never
is. Path defaults to a temp file named after the design — override with
`args={'path': '/abs/out.<ext>'}`.

```python
get(kind="cad", id="flange", view="scad")  # OpenSCAD source (text; always available)
get(
    kind="cad", id="flange", view="stl", args={"path": "/tmp/flange.stl"}
)  # printable mesh
get(
    kind="cad", id="flange", view="3mf", args={"path": "/tmp/flange.3mf"}
)  # printable (modern slicer fmt)
get(
    kind="cad", id="flange", view="step", args={"path": "/tmp/flange.step"}
)  # exact B-rep for CAD apps
```

- **`scad`** — pure text, zero deps; drop into the OpenSCAD GUI.
- **`stl` / `3mf`** — in-process mesh (manifold3d CSG, a core dependency —
  works with no extra). 3MF carries units/metadata; STL is universal. A
  design with `rd`/`blend:`/`field:` meshes from its signed-distance field
  instead (`args={'pitch': '0.2mm'}` sets the sample spacing — see
  *Rounding* above); the reply names which route ran.
- **`step`** — *exact* ISO-10303 B-rep via OpenCASCADE (true cylinders/
  cones, not facets) for mechanical CAD (FreeCAD / Fusion / SolidWorks).
  Needs the heavier `precis-mcp[cad-step]` extra.

A missing `[cad-step]` extra returns an Unsupported error with the install
hint, never a crash.

**Assemblies travel as one file.** Each `component` is exported as a
separate body where the format supports it — STEP as named
`MANIFOLD_SOLID_BREP` solids (XCAF), 3MF as named `<object>`s. STL and
`.scad` weld the components into one solid (no part identity in those
formats). So a wheel + bracket modelled as two components round-trips as
a real two-part assembly in a single `.step`/`.3mf`.

## Web editor (`/cad`)

A design is also a *human* affordance: `precis web` serves an interactive
viewer at `/cad/<slug>` (linked from **Drive**, which is now the default
landing page). It mirrors the DFT editor (`/structure`):

- **3D viewer** — the analytic IR is tessellated (numpy only, no heavy
  kernel) and shipped as a binary **glTF** that three.js renders *and* the
  user downloads (same bytes). Parts are coloured per component; `cut` /
  `intersect` features are translucent "tool volumes". A **Solid** toggle
  shows the true CSG-folded solid. Click a feature for its name / part /
  op / config / pose; hover the feature list or a part chip to glow it.
- **Edit by prompt** — the "Further instructions" box mints a **`cad_propose`**
  job (tool-less `claude -p`): the LLM returns a full rewritten design
  *source*, dry-run-validated (`parse_source` + `build_design`) before you
  see it. Review it, then **Apply** derives a new slug (`CadHandler.derive`,
  linked `derived-from`), optionally soft-deleting the original.
- **Downloads** — glTF + OpenSCAD + STL / 3MF always; STEP with
  `[cad-step]`.
- Create a new design straight from Drive's **+ New** dropdown.

## Retire a design

```python
delete(kind="cad", id="flange")  # soft-retire the whole design (recoverable)
```

One limit worth knowing: a design whose node is *both* patterned and
`intersect` can't be instanced (flattening it under a pose would change the
solid) — split that node into explicit nodes and it instances fine.

## See also

- [[precis-cad-assembly-help]] — ports, mates, joints, connectivity
- [[precis-cad-build-help]] — make-tree, dimensions, mass, BOM, printability
