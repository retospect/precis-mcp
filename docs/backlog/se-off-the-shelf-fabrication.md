---
status: draft
title: se off-the-shelf fabrication — bought parts, stock-constrained modes, mechanism→geometry propagation
prio: high
model: opus
---

# se: building with things you didn't print

Design session 2026-09-04 (Reto + agent), precious-juggling-map worktree,
from the question "the se kind has a 3d printer implementation — what
additional building blocks do we need for off-the-shelf parts (steel
pipes, screws, nuts, laser-cut plexiglass sheets)?". Companion to
`se-kind.md` (which owns the kind itself); this item owns the
**fabrication modes that aren't additive**, and slots into that doc's
ship order at slice 7 ("more modes"), with its first rung buildable
against slice 5.

The framing that makes the work small: FDM is the *easy* mode. It assumes
you author any solid you like, at any dimension, in one piece, and the
plan is "orient it and slice it". Every off-the-shelf process breaks one
of those three assumptions, and each broken assumption is one engine:

1. **You didn't author the geometry** — a bought part's solid comes from
   its catalog row, not from an envelope you drew.
2. **The joint is real** — a screw or a tab-and-slot *changes the parts
   it joins*; print-in-place never had to.
3. **Stock is discrete, and the output isn't a mesh** — 3 mm acrylic
   exists and 3.4 mm doesn't, and what you send out is a cut file or a
   cut list, not an STL.

Everything below is one of those three, plus the data each needs.

## What already exists (do not rebuild)

- **`component` kind** (mig 0093/0095) — the procurable-part store, with a
  category registry that *already seeds* `fastener`, `pipe`, `profile`,
  `bearing`, `seal`, `fitting`, `laminate`; per-value sourced specs in
  canonical units (`thread_size`, `thread_pitch`, `length`, `grade`,
  `drive_type`, `bore_diameter`, …); `made_of` → `material`; a `contains`
  assembly tree; and **`view='bom'` with cost/mass rollup**. The purchase
  side of a BOM is written — se has to *reach* it, not reinvent it.
- **`part` kind** — the LCSC/JLCPCB catalog, ingest-only, C-number
  addressed. The precedent for a supplier catalog with a colloquial→row
  resolver (`precis-part-select-help`), not a thing to extend here.
- **se L3** already names "a `component` binding (bought part with a
  datasheet envelope)" as a realization option, and `se_bom` is in
  se-kind.md's schema sketch — both unbuilt. Slice 3 shipped the
  `mechanism` registry (`snap|screw|press|key|magnet|bearing|bond|
  integral`) with implied demands, and explicitly deferred `bearing`'s
  BOM demand to this table (`precis_se/joints.py`, the `"deferred"` note).
- **The cad kernel covers the geometry.** Bolt = `hex:`+`cyl:`, nut =
  `hex:`−`cyl:`, pipe = `cyl:`−`cyl:`, sheet = `box:` or (slice 5's
  profile tier) an extruded arc-polyline; a tube-to-tube cope is a
  cyl−cyl cut, closed-form. **No new primitives are implied by this
  item** — a real result, and the reason it is affordable.

## Engine 1 — catalog → geometry + ports

A bought part enters a design as a **binding**, never as a block you
drew: the block's L3 realization is `component:<slug>` (or `part:<C…>`),
and its solid + attachment points are *derived from the spec row*.

- **Envelope generators, one per category**, pure functions of the
  component's spec values → a cad DSL config (+ a small node set where
  one primitive won't do). `fastener` → head (hex/cyl by `drive_type`) +
  shank at nominal Ø × `length`; `pipe`/`profile` → section swept to
  `length_overall`; `bearing` → annulus from bore/OD/width; `laminate`/
  sheet → `box:` at stock thickness. Missing specs degrade to a **bound
  volume** flagged opaque, never to a guess (suggestive-by-contract, and
  the same honesty tier as an unfilled envelope).
- **Threads are never modelled.** A screw is a nominal shank plus a
  *thread port*; whether it fits is a declared tolerance relation, not a
  geometric interference test. (se-kind.md already defers thread
  generators; this keeps that deferral honest instead of quietly
  needing them.)
- **Port templates per category** — the part people forget, and without
  it `connect` cannot attach to a bought part at all. A screw exposes
  `head` (bearing face + axis), `shank`, `thread`; a pipe exposes `end_a`
  /`end_b` + axis; a bearing exposes `bore`/`od`/`face_a`/`face_b`. Same
  `se_ports` shape as an authored block, resolved at read time from the
  binding the way an instance's ports resolve from its template
  (`ops.effective_ports` extends to a third source).
- **Colloquial resolution.** Agents say "M6×30 8.8 socket cap", "1/2 in
  EMT", "3 mm cast acrylic". A designation resolver (standards
  designation as a first-class spec — `DIN 912`, `ISO 4762`, `EN 10255`)
  maps that to a component row, the `part`/C-number precedent one level
  up. Without it the kind is unusable by the propose loop.
- **Series, not SKUs.** `component` is entity-per-SKU; hand-entering 400
  screws is not viable. A **series** row carries a family + a
  **valid-combination size table** (M6×2 does not exist; DN50 wall
  thicknesses are a fixed list), and an entity is minted from a series
  row on first use. This is the one genuine schema addition on the
  `component` side, and it is also what makes engine 3's snap-to-stock
  possible.

## Engine 2 — mechanism → geometry propagation

**The highest-leverage building block, and nothing in the tree does any
of it today.** se slice 3 made a mechanism *demand a relation*; it has
never made a mechanism *change a part*. Off-the-shelf assembly is almost
entirely this:

- `screw` ⇒ a clearance hole through every intermediate member (Ø from a
  fit class — M6 close/normal/loose = 6.4/6.6/7), a counterbore or head
  clearance at the near end, and a tapped hole, nut pocket, or captive
  T-slot at the far end.
- sheet ⇒ **finger joints / tab-and-slot** cut into both members, kerf
  compensated — the sheet analogue of hole stamping, and how laser-cut
  assemblies are actually held together.
- tube ⇒ miter angles and **cope/fishmouth** notches at the joint.
- `press`/`bearing` ⇒ the seat bore and shoulder.

One engine, four instances: a joint, given its mechanism and the posed
geometry of its two members, emits **feature ops into both members**.
Design posture, following the house pattern everywhere else (sketch
canonical / copper derived, realization-never-flattens): the joint stays
canonical, the stamped features are **derived and regenerable**, named
after the joint that made them, and a hand edit to a stamped feature is
an explicit unlink — never a silent divergence. `suggested_fix` shape
from the structure-validate DRC vocabulary; applying is an explicit L3
edit, exactly as se-kind.md decided for process skills.

Checks that come with it, all reusing shipped machinery:

- **Fastener stack-up along the joint axis** — grip = Σ member
  thicknesses on the axis; screw length ≥ grip + nut height +
  protrusion; thread engagement ≥ 1×D into a blind tapped hole. Pure
  reuse of slice 3's `measures.py` stack-up; the axis walk is the new
  part.
- **Tool access** — a socket driver needs a clear cylinder above the
  head, a spanner a swept annulus around a nut. The one genuinely new
  *geometric* check, answerable with the existing clearance/DOF probes
  against a swept solid; needs driver envelopes per `drive_type` × size
  as capability data.
- **Assembly-order existence** — each part insertable along *some* free
  direction against the partial assembly (`relate.translational_dof`
  against a growing union). An existence check, explicitly **not** path
  planning.
- **Edge distance** — a hole too near a sheet edge or a tube end; a
  material rule, not a process one (`min_edge_distance` as a function of
  thickness/Ø).

## Engine 3 — stock-constrained realizability + non-mesh outputs

FDM lets you pick any dimension. Stock does not: thickness, OD, wall and
length are **discrete design variables**, which no part of the tree has
ever modelled.

**A realizability predicate per mode** — the honest generalization of
`cad-printability-probe.md`'s orientation search. Each mode answers "can
*this solid* be made by *this process*, and if not, where":

| mode | predicate |
|------|-----------|
| `fdm/*` | any solid; scored by orientation (the shipped/planned one) |
| `laser/<material>` | **one profile extruded at a stock thickness** — every z-section identical (`probe_section_z`), thickness ∈ series |
| `stock-cut/<profile>` | a length of a stock cross-section, mitered / drilled / coped |
| `cnc-2.5ax/*` | already in se-kind.md slice 6: pockets reachable from the top |
| `purchase` | must be a `component` binding; DRC is SKU existence / stock / lead time |

The laser predicate is exactly answerable with the analytic kernel plus
slice 5's profile tier — which is why this item **sequences after slice
5** and needs nothing from it that isn't already scoped.

**Snap-to-stock**: an op that pulls an envelope dimension onto the
nearest available size from the series table, records the choice as a
`decision` note (slice 4's ledger), and leaves the tolerance relations to
absorb the delta.

**Capability rows** — same `se_capabilities.json` discipline as FDM
(versioned data, two tiers, `source`/`retrieved`/`field_confidence` per
field, `None` where unpublished, never a figure carried over from
another row):

- `laser/acrylic`, `laser/ply` — kerf width, min feature ≈ t, min hole Ø
  ≥ t, min web between features, edge taper angle, max sheet size,
  through vs engrave, and **min internal corner radius** (acrylic crazes
  and cracks at sharp internal corners under load — the real failure
  mode, and the same rule *shape* as CNC's tool radius, so one checker
  serves both).
- `stock-cut/pipe`, `stock-cut/profile` — saw kerf, miter range, min cut
  length, hole Ø vs wall thickness, min edge distance from a cut end,
  bend radius where bending is in scope.
- Anisotropy carries over from FDM's strength vector: ply has grain,
  extrusions have a rolling direction; acrylic is isotropic but
  notch-sensitive. Same field, different physics, already in the row
  shape.

**Outputs — the mesh is no longer the artifact.** L5's export list grows
from "OpenSCAD / STL-3MF / STEP" to:

- **Flat pattern → DXF/SVG**, kerf-compensated, with per-sheet
  **nesting**. Rent `pcb/gerber.py` — it is the in-tree prior art for
  flattening arcs/polygons to a 2D vendor format with quantization
  (`gerber.py::_u`), and the profile tier gives it arc-polylines
  directly.
- **Cut list** for linear stock — part, stock SKU, length, both end
  miters, hole positions along the axis — plus **offcut yield** from
  stock lengths (1-D bin packing; small, deterministic, worth doing
  properly once).
- **Purchase BOM** — `se_bom` rolled up through the array multiplicities
  into `component`'s existing `view='bom'` cost/mass rollup. Nearly free
  once `se_bom` exists.
- **Assembly instructions** fall out of the joint list + assembly order +
  the notes ledger; explicitly *not* scoped as a separate generator.

## Schema additions

- **`se_bom`** (already sketched in se-kind.md, unbuilt) — `ref_id` ·
  `block`/`connect` name-keyed · target kind (`component`|`part`) ·
  target slug · quantity · uom · reason · `retired_at`. Name-keyed like
  everything else (persist is retire-all/reinsert-all).
- **`se_blocks.bound_kind`** must admit `'component'` — 0001's CHECK is
  `('cad','nm')`. Forward-only: a new migration drops and re-adds the
  constraint (never an edit to the sealed file).
- **Stamped features** need no table: they are derived from the connect +
  its mechanism at read/realize time, cached nowhere (the
  copper-derived rule). Their *provenance* is the connect name.
- **Component series + size table** on the `component` side, in core's
  migration namespace — the one addition outside the plugin.

## Ship order

Rungs 1–3 are mode-independent and pay off even in an all-FDM design;
4–5 are where plexiglass and steel pipe come out the far end.

1. **`se_bom` + component binding + `purchase` mode** — bought things
   exist, with cost/mass rollup and *no geometry at all*. Closes slice
   3's deferred `bearing`-demands-a-BOM check. Cheapest useful rung; the
   only one that does not depend on slice 5.
2. **Catalog → geometry + port templates** — bought parts join
   clearance / DOF / connectivity / `envelope_fit`.
3. **Mechanism → geometry propagation**, `screw` first (hole stamping +
   grip stack-up + tool access). Where it stops being a diagram.
4. **`laser/*` + `stock-cut/*`** — capability rows, realizability
   predicates, snap-to-stock, series/size tables.
5. **Flat pattern DXF/SVG + nesting; cut lists + offcut yield.**

## Deferred, named so they are not re-derived

Thread geometry (a thread port + a relation, always); weld/braze as a
mechanism (a bond variant, no geometry propagation modelled); bending
and roll-forming beyond a declared bend radius; multi-axis CNC (already
deferred in se-kind.md); sheet-metal bend allowance / k-factor (a
different unfold math from laser flat pattern — do not conflate them);
2-D nesting *optimality* (first-fit decreasing is the rung-5 bar);
supplier stock/price APIs (the `part` refresh pattern applies, but no
customer demands it); path planning for assembly (existence only).

## Open questions for Reto

- **Series table home**: extend `component` in core (one more table, the
  whole tree can use it) or keep a stock series table plugin-local to
  `precis_se`? Leaning core — pipes and screws are not se-specific.
- **Fit classes as data or as a table?** Clearance-hole Ø per thread
  size is a published table (ISO 273 medium/close/coarse); seeding it as
  capability data means every mode reads it through the one resolver.
  Leaning data.
