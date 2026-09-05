---
id: precis-cad-help
title: precis — the CAD kind (analytic solid design you can read)
summary: author a parametric solid as a text node-list, then probe it analytically (point/ray/arc/section/volume) and relate parts (clearance/interference/connectivity/translational DOF) — no meshing, no pixels; STL/3MF/STEP/SCAD are downstream exports
answers:
  - how do I author a parametric solid model as text?
  - how do I check clearance or interference between two parts?
  - how do I export a CAD design to STL / STEP / SCAD?
  - how do I probe a design — find a point, section, or volume?
applies-to: get/search/put/delete (kind='cad')
status: active
---

# precis-cad-help — design solids the LLM can *read*

A `cad` design is a **boolean DAG of placed analytic primitives** (ADR
0041). You author it as text, and instead of staring at a render you
**probe it analytically** — "what's along this ray?", "what's the gap
between shaft and bore?", "what's the section at z=4?". Postgres is
canonical; SCAD/STL/3MF/STEP export is a regenerable downstream view. Units
are **millimetres**, transforms are **rigid** (translate + rotate, no
scale), so every probe is **exact** (only volume/centroid are sampled).

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
- `@x,y,z` places the node (default origin); `rot:rx,ry,rz` rotates it
  (degrees). `polar:`/`linear:` replicate it into one pattern node.
- `component <name>` opens a part; nodes belong to it until the next
  `component` line. Default part name is `part`.
- `#` starts a comment.

**All angles in `cad` are degrees** — `rot:rx,ry,rz`, the `polar:` even
spacing (360°/N), and the `arc` probe's θ output. Lengths/coordinates are
millimetres. (The `calc` kind defaults to degrees too — see the tip
below.)

```python
put(
    kind="cad",
    id="flange",
    text="""
component flange
plate     add  cyl:r25h8
hub_bore  cut  cyl:r8h10    @0,0,-1
bolts     cut  cyl:r2.5h10  @18,0,-1  polar:n6r18
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
slab  add  box:w60d60h4
use standoff as sw  @-20,-20,4
use standoff as se  @20,-20,4
use standoff as nw  @-20,20,4  rot:0,0,90
""",
)
```

`use` is a **top-level directive** like `component` — it doesn't join or
close the component block above it. `@x,y,z` / `rot:` pose the whole
sub-assembly, and `polar:` / `linear:` replicate it (`use bolt as b @18,0,0
polar:n6r18` is six bolts).

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

### Assemble by interface — `port` and `mate`

Typing world coordinates for every sub-assembly is where designs (and
models) drift. Declare a **port** — a named frame on a design — and let a
**mate** compute the pose:

```python
put(kind="cad", id="nema17", text="""
component body
case   add  box:w42d42h40
port   shaft  @0,0,40          # the output face, 40 mm up its own z
""")

put(kind="cad", id="drivetrain", text="""
port deck @0,0,12                # a frame on THIS design

use gearbox as g
use nema17  as m

mate g.input to deck             # anchor = one of this design's ports
mate m.shaft to g.output flip    # anchor = another instance's port
""")
```

- `port <name> [@x,y,z] [rot:rx,ry,rz]` is a **top-level directive** like
  `component` / `use` — it names a frame, not geometry, so it never becomes
  a node, never appears in a probe, and never exports.
- `mate <instance>.<port> to <anchor> [flip] [spin:<deg>]` places
  `<instance>`. The anchor is either `<port>` (this design's own, fixed) or
  `<instance>.<port>` (another instance, posed first).
- **The default is coincidence** — the two frames land exactly on top of
  each other, same origin, same axes. That is "put your connection point
  right here". `flip` adds an explicit 180° about x (the two faces then
  oppose, which is what you want for a shaft entering a bore); `spin:<deg>`
  rotates about the port's z, for clocking a bolt pattern.
- Addressing is **one level**: `m.shaft`, not `m.inner.shaft`.
- An instance with no mate sits where you placed it (the origin by default)
  — a frame or base part needs no mate.

Refused at `put`, each naming the offender: mating an instance twice, or
mating one that also carries `@`/`rot:` (both over-constrained — a mate
already fixes all six DOF); mating a `polar:`/`linear:` instance; a mate
cycle (`a` mated to `b` mated to `a`); an unknown instance or port (the
error lists the ports the design actually declares).

Ports are also **searchable**: they go into the design's one card, so
`search(kind='cad', q='nema17 mount port')` finds designs by the interfaces
they advertise.

Ports take two optional tags: `type:<t>` (free compatibility tag — two
*typed* ports only mate when the types match, an untyped side always may)
and `of:<component>` (scopes the frame to a component — required for the
pivot of a component `joint` below).

### Articulate — `joint`, `state`, and `view='sweep'`

A **mate is a `fixed` joint**. The articulated kinds insert one degree of
freedom at the interface, about/along the **anchor frame's z axis**:

```python
put(kind="cad", id="crane", text="""
component tower
mast add box:w20d20h200

component jib
beam add box:w150d10h10 @75,0,205
port slew @0,0,205 of:jib

joint jib revolute at:slew limits:-170..170     # component form

use hook_block as h
joint h.eye to jib.tip prismatic limits:0..180  # instance form
""")

# pose it — a joint's name is its subject instance / component:
get(kind="cad", id="crane", view="point",
    args={"state": {"jib": 45, "h": 120}, "p": [0, 90, 205]})

# the payoff question — does anything hit anything, anywhere in the travel?
get(kind="cad", id="crane", view="sweep")
```

- Kinds: `revolute` (deg) · `prismatic` (mm) · `cylindrical`
  (`[deg, mm]`, two DOF) · `screw` (deg, advances `pitch:<mm>` per rev) ·
  `fixed` (= `mate`).
- **Two forms**: `joint <inst>.<port> to <anchor> <kind> [opts]` poses an
  instance (a generalised mate — `flip`/`spin:` still apply);
  `joint <component> <kind> at:<port>` articulates a whole component of
  *this* design about a port scoped `of:` that component.
- `state=` in any probe/export view's `args` poses the design. Missing
  joints default to 0 (clamped into `limits:`); an **explicit** state
  outside `limits:` is an error, never clamped. `state` addresses only the
  top design's joints — instanced sub-designs pose at their defaults.
- `gear <a> to <b> ratio:<r>` / `belt …` couple two joint states
  (`b = r × a`; the sign carries the sense, so contact gears want a
  negative ratio). A driven joint derives; setting it explicitly to a
  conflicting value is an error.
- A mate/joint anchored on a port `of:` a jointed component **follows**
  that component — a motor mated onto an articulated arm swings with it.
- `view='sweep'` sweeps each joint across its `limits:` (others held
  neutral, `args.n` samples, default 9), reporting every colliding pair
  with the state range where it interferes, plus the swept envelope per
  moving body. `args={'joint': 'jib'}` sweeps one joint only.

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
base  add  box:w40d40h5
hole  cut  cyl:r3h6  @10,10,-1
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

| shape | grammar | example |
|-------|---------|---------|
| box | `box:w<W>d<D>h<H>` | `box:w40d20h10` |
| cylinder | `cyl:r<R>h<H>` | `cyl:r3h12` |
| cone | `cone:r<R>h<H>` | `cone:r5h8` |
| truncated cone | `tcone:rb<RB>rt<RT>h<H>` | `tcone:rb6rt2h5` |
| hex prism | `hex:r<R>h<H>` | `hex:r5h10` |
| n-gon prism | `ngon:n<N>r<R>h<H>` | `ngon:n6r5h10` |
| n-gon frustum | `frustum:n<N>rb<RB>rt<RT>h<H>` | `frustum:n6rb4rt2h5` |
| pyramid | `pyramid:n<N>r<R>h<H>` | `pyramid:n4r5h8` |
| sphere | `sphere:r<R>` | `sphere:r6` |
| torus | `torus:R<major>r<minor>` | `torus:R10r2` |
| chamfer bevel tool | `chamfer:<size>x<angle°>` | `chamfer:1x45` |

All are placed base-at-`z=0`, centred on the local axis; `@x,y,z` and
`rot:` set the world pose.

`chamfer` is an unbounded half-space *tool*, not a solid: `cut` /
`intersect` only, never a component's first node. Its cutting plane sits
`size` along −normal from the node origin, tilted `angle`° from local
+z — pose it with `@`/`rot:` onto the edge to bevel (patterns apply).
Exports and the viewer substitute a finite clamped box automatically.

## Read the design — `get`

```python
get(kind="cad")  # list all designs
get(kind="cad", id="flange")  # the node tree (TOON: handle name part op config pose)
get(kind="cad", id="ca7")  # one node as JSON (handle = ca<chunk_id>)
```

A node is addressed by its **`ca<chunk_id>` handle** (shown in the tree).

## Probe it — `get(view=…, args={…})`

All probes are full-DOF (any origin / direction / orientation). Pass the
geometry in `args=`. `args.component` scopes to one part (default: the
whole design).

```python
# 0D — classify a point: containing node(s), or (if carved) the blocker + nearest
get(kind="cad", id="flange", view="point", args={"p": [0, 0, 4]})

# 1D — ray: material/void intervals, each void attributed to the node that removed it
get(kind="cad", id="flange", view="ray", args={"o": [-30, 0, 4], "d": [1, 0, 0]})

# 1D — arc: angular intervals around an axis (bolt circles, radial features)
get(
    kind="cad",
    id="flange",
    view="arc",
    args={"c": [0, 0, 4], "axis": [0, 0, 1], "r": 18},
)

# 2D — section at z=const: feature-attributed loops (outer / hole)
get(kind="cad", id="flange", view="section", args={"z": 4})

# bulk — geometric volume + centroid (SAMPLED, labelled with ±error)
get(kind="cad", id="flange", view="volume")
```

A carved region reads **empty** and **names the blocking node** ("empty;
removed by hub_bore") — subtraction is visible without ever merging the
solid.

## Relate parts — clearance / interference / DOF

Built at real dimensions and *analyzed*, not declared (there is no `fit`
object — a press fit is simply *clearance = −0.02 mm*, and whether that's
intended is your call):

```python
# signed min gap between two components: + clear, ≈0 line-to-line, − interference
get(kind="cad", id="asm", view="clearance", args={"a": "shaft", "b": "hub"})

# how far one part can translate along ±x/±y/±z before hitting another
get(kind="cad", id="asm", view="dof", args={"moving": "shaft", "fixed": "hub"})
```

Clearance is measured against the *material* — a shaft sitting in a bored
hub reads the **radial wall gap**, not a false collision against the
un-bored plate.

## Connectivity — is it one solid? what touches what?

`view='connectivity'` builds the **contact graph** over the design's
components: two parts are *connected* when their realised (post-cut)
material touches or overlaps (signed gap ≤ tol). It answers three questions:

```python
# full report: the connected bodies + every contact + the one-solid verdict
get(kind="cad", id="wheel", view="connectivity")

# what touches this part? (empty ⇒ a floating body)
get(kind="cad", id="wheel", view="connectivity", args={"of": "hub"})

# is there a contact path between two parts? (e.g. hub → rim through spokes)
get(kind="cad", id="wheel", view="connectivity", args={"a": "hub", "b": "rim"})

# loosen/tighten what counts as "touching" (mm)
get(kind="cad", id="wheel", view="connectivity", args={"tol": 0.05})
```

Because contact is tested on the **folded CSG** (cuts already applied),
the classic trap is avoided: a rim (`disc − cutout`) and a hub
(`disc − cutout`) whose *raw* discs overlapped massively before the cuts
are correctly seen as **not touching** — only their post-cut annulus/disc
material counts. So "is the hub connected to the rim?" gives the physical
answer, not the pre-cut one.

### Truisms — a real part is one connected solid

A manufacturable part is a *single connected body*: a wheel is its hub, its
spokes, **and** its rim, and they must all touch (directly or through each
other). Model each distinct body as its own **component** (`hub`, `rim`,
`spoke`) — then `connectivity` verifies the whole thing hangs together, and
`put` warns you at author time if it doesn't:

- `⚠ floating (touches nothing): rim` — a part welded to nothing.
- `⚠ 2 disconnected bodies: hub+spoke | rim` — two islands that should be one.

After any edit that moves or resizes a body, re-check connectivity: a spoke
nudged 0.1 mm too short silently disconnects the rim. Connectivity is at
the **component** level — a stray *instance* inside one component isn't
caught; keep distinct bodies as distinct components.

> **Tip — need a number, exactly?** Don't eyeball arithmetic. The
> `calc` kind is a local sympy engine: `get(kind='calc', q='2+3*4')`
> evaluates arbitrarily complex expressions *exactly* — fractions,
> roots (`sqrt(2)`, `2**10`), **trig** (`sin cos tan atan2`, `pi`), even
> calculus and linear algebra. Handy here for bolt-circle coordinates,
> slant/draft angles, and tolerance stacks before you `put` them into
> the source.
> **`calc` trig is in degrees by default** — matching cad's convention —
> so `get(kind='calc', q='sin(30)')` → `1/2` and `get(kind='calc',
> q='N(atan2(1,1))')` → `45` directly, and the result carries a
> "degrees" note. Pass `view='rad'` for radians (symbolic calculus);
> wrap in `N(...)` for a decimal instead of the exact form.

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
  works with no extra). 3MF carries units/metadata; STL is universal.
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

## Scope (v1)

Primitives: frustum family (box / cyl / cone / tcone / n-gon prism /
pyramid), sphere, torus, chamfer half-space bevel tool. Ops: merge /
subtract / intersect, place, polar / linear pattern, **instance another
design** (`use <slug> as <name>`). Probes: point /
ray / arc / section(z). Relations: clearance / interference /
translational DOF. Bulk: geometric volume (sampled). **Deferred to
phase 2**: threads / gears, rotational DOF, fillets / rounds, datums,
persisted observers, mass/density.

One limit worth knowing: a design whose node is *both* patterned and
`intersect` can't be instanced (flattening it under a pose would change the
solid) — split that node into explicit nodes and it instances fine.
