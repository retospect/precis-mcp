# CAD examples (ADR 0041)

Hand-authored `cad` designs in the text node-list language, with every
export artifact regenerated into [`out/`](./out). Each design exports to:

| ext     | what                          | backend (optional extra)        |
| ------- | ----------------------------- | ------------------------------- |
| `.scad` | OpenSCAD source               | none — always available         |
| `.stl`  | printable mesh                | `manifold3d` (`[cad-export]`)   |
| `.3mf`  | printable mesh (slicer fmt)   | `manifold3d` (`[cad-export]`)   |
| `.step` | exact ISO-10303 B-rep         | OpenCASCADE (`[cad-step]`)      |

Regenerate them all:

```bash
uv run python examples/cad/generate.py
```

## The designs

### `flange`
Round 50 mm mounting flange, 8 mm thick, with a Ø16 hub bore and a 6-bolt
circle (Ø5 holes on a Ø36 PCD via a `polar` pattern).

### `hex_standoff`
M3 hex standoff — 8 mm across-flats body, 20 mm long, through clearance
bore (`hex` prism + a `cyl` cut). Shows a non-circular base solid.

### `l_bracket`
Right-angle bracket: a 40×30×4 base with two M4 holes plus a 26 mm
upstand with two **horizontal** bores (`cyl` rotated `rot:90,0,0`, so the
hole axis runs through the wall thickness). Two components folded into the
one part.

### `drain_box`  +  `drain_lid`  — a mating pair
A box and its lid, dimensioned to fit.

`drain_box` — 60×40×30 mm open-top box: a solid `shell` with the `cavity`
cut out (3 mm walls, 6 mm floor) and a Ø4 **drainage hole** cut through
the floor in a corner so water/debris drains out.

`drain_lid` — a 60×40×3 mm top plate with a **locating boss**: a
53×33×6 mm rim that protrudes *down* from the underside and nests inside
the box opening. The boss is 1 mm under the cavity in each axis (0.5 mm
clearance per side), so the lid drops in by hand but is **registered by
the boss and cannot slide off**. A small vent hole keeps it from sealing
airtight.

```
            lid top plate (60×40×3)
        ┌───────────────────────────┐
        │   ┌───────────────────┐   │   ← boss (53×33×6), drops into…
        └───┤                   ├───┘
            │   box opening     │       …the box cavity (54×34), 0.5mm/side gap
  ┌─────────┤   (inner rim)     ├─────────┐
  │  wall   └───────────────────┘   wall  │  60×40×30 outside, 3mm walls
  │                                       │
  │     floor (6mm)         ● drain Ø4 ───┼──→ corner drainage hole
  └───────────────────────────────────────┘
```

All five designs verified watertight (manifold) and probe-checked (the
drain corner reads empty, the boss reads solid at the expected extents)
before export.

### Verified fit (analytic, no meshing)

Probing an assembled config (lid seated, boss in the opening):

- `clearance(box, lid)` → **gap = 0.52 mm**, no interference — the boss
  slips into the rim with a hand-fit gap.
- `translational_dof(lid, box)` → **+z = ∞** (lifts straight off) but
  lateral travel is bounded by the boss hitting the rim (~0.5 mm in y) —
  so once seated the lid is registered and **cannot slide off**.

### `wheel_assembly` — a 4-part axle assembly in one file
A wheel mounted on a bracket's hub and retained by a washer and screw —
four components, four bodies:

- **`bracket`** — a mounting plate (two bolt holes) with a Ø12 **hub post**
  and a Ø5 **axial bore** down its centre for the screw.
- **`wheel`** — the 5-spoke wheel (rim + hub + spokes via a `polar`
  pattern), bored Ø12.4 to **slip onto the Ø12 hub post** with a 0.2 mm
  radial running clearance.
- **`washer`** — a Ø18 ring that **overhangs the wheel bore** to trap it.
- **`screw`** — a Ø5 shaft (head + shaft) that drops through the washer
  and down the hub's axial bore, capping the stack.

No threads, just cylinders: the **screw shaft Ø5 fills the axial bore Ø5**
(a line-to-line fastener fit, per spec), while the **wheel bore Ø12.4 rides
the Ø12 hub post with a 0.2 mm radial running clearance** so the wheel can
turn freely.

It also demonstrates **multi-solid export**:

| format  | assembly behaviour                                            |
| ------- | ------------------------------------------------------------- |
| `.step` | **four distinct named solids** (`bracket`/`wheel`/`washer`/`screw`) in one file — a true assembly (XCAF); CAD apps show all four in the tree |
| `.3mf`  | **four named `<object>`s** referenced by the build — stay separable in the slicer |
| `.stl`  | **one welded body** — STL is a triangle soup with no part identity |
| `.scad` | one `union()` of the four components (source renders to one mesh) |

#### Verified fit (analytic, no meshing)

All six part-pairs were probed: **no pair penetrates another** — every
solid-vs-solid intersection is `0 mm³`. The fits are:

- **screw shaft Ø5 in the axial bore Ø5** — line-to-line (coincident wall,
  the intended fastener fit, per spec);
- **wheel bore Ø12.4 on the Ø12 hub post** — the exact CSG signed-distance
  field reads **0.2 mm from the post wall to the bore wall**, i.e. a 0.2 mm
  radial running clearance all the way round, so the wheel turns freely.

The wheel is **axially captured**: the washer (Ø18) overhangs the wheel
bore (Ø12.4) by ~2.8 mm, and the wheel sits between the bracket plate (top
face at z=0) and the washer (z=12) — so it spins on the post but cannot
slide off.

(The 0.2 mm figure is read straight from the exact per-component
signed-distance field; the global `clearance` minimiser is seeded on a
coarse grid and isn't reliable for thin contacts inside a part as large as
the Ø80 wheel — a known limitation, not a fit problem.)

## Can an assembly be exported as one file?

Yes — **STEP** and **3MF** both carry multiple distinct bodies in a single
file, and precis keeps each `component` separate for them (STEP as named
`MANIFOLD_SOLID_BREP` solids via the XCAF document model; 3MF as named
`<object>`s). **STL** and **`.scad`** cannot represent part boundaries, so
there the components are welded into one solid. Use a separate `component`
per part; everything else (probes, clearance/DOF) already treats
components as distinct bodies.
