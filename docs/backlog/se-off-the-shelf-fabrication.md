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
- **Series, not SKUs.** *(rung 2a is built; rungs 2b–5 of this item are
  not — do not delete this file.)* `component` is
  entity-per-SKU; hand-entering 400 screws is not viable. A **series**
  carries a family + a **valid-combination size table** (M6×2 does not
  exist; DN50 wall thicknesses are a fixed list), and an entity is minted
  from a size row on first use — `put(kind='component', series='iso-4762',
  size='M6x30')`, deterministic slug, idempotent re-mint, off-list length
  warns rather than refuses. It is also what makes engine 3's
  snap-to-stock possible.

  **It landed as a file, not a table** — `precis/data/component_series.json`
  + `precis/component_series.py`, the `pcb_capabilities.json` posture. The
  schema addition the design sketch expected turned out to be unnecessary:
  these are published standards dimensions, so they are curated, versioned
  and diffable, changed by a commit rather than a migration, and nothing
  agent-authored ever lands in them. The only DB change was migration
  0152, which seeds ten **universal geometric-extent specs**
  (`outer_diameter`, `inner_diameter`, `wall_thickness`, `thickness`,
  `width`, `height`, `across_flats`, `head_diameter`, `head_height`,
  `drive_size`, all mm) — 0093 seeded the specs an agent *shops* by, none
  of which says how big the thing is.

### Where the catalog data comes from (surveyed 2026-09-05)

Reto asked whether there's an EU McMaster-Carr to integrate. There isn't
— and McMaster itself has no open API (account-gated, data explicitly
not redistributable, scraping against ToS). The useful answer is that
the question splits in two, and the split is already the series-vs-SKU
line above:

- **Geometry + valid sizes = standards data, not supplier data.** DIN/ISO
  fastener tables are published and there are open, redistributable
  encodings of them: **BOLTS** (open library of technical
  specifications) and the **FreeCAD Fasteners workbench**, which between
  them cover ISO 4017/4762/4032/4033/4035, DIN 557/562/985, ISO
  7089/7090/7093/7094 and more, as parametric tables. Seed the series +
  size tables from these — offline, deterministic, no account, no
  vendor lock — and rung 2's envelope generators fall out of the same
  numbers. **Decision this implies:** the series table is standards
  data, so it belongs in core beside `component`, not plugin-local
  (resolving that open question below).
- **Price / stock / lead time = supplier data, a later enrichment layer**
  on rows that stay supplier-neutral and are keyed by standards
  designation. Candidates when we want it, all EU-reachable:
  **TraceParts** (French; a real REST developer hub — catalog list, CAD
  availability, CAD request — aggregating hundreds of supplier catalogs
  and 100M+ models; built for distributors, so keys are a commercial
  arrangement); **Würth** (the EU fastener giant, offers an API for
  availability and product data); **Misumi Europe** (configurable
  mechanical parts — the closest thing to McMaster's *mechanical*
  breadth; integration path is eProcurement punchout/EDI rather than a
  public REST API); **RS** (750k MRO products across 32 countries — the
  closest to McMaster's catalog *shape*); **Fabory** (NL, ~400k fastener
  articles). Electronics already has its path: the `part` kind's
  LCSC/JLCPCB ingest, plus Würth Elektronik's customer API.

- **The Chinese option is the strongest supplier candidate, because we
  already own the ingest pattern.** **JLCMC** (jlcmc.com) is the
  JLCPCB/LCSC group's *mechanical* store — ~1M hardware / mechanical /
  automation parts (linear motion, bearings, fasteners, extrusion), no
  MOQ, same-day shipping: the Chinese McMaster analogue, from the same
  family whose catalog machinery is already in-tree
  (`precis/pcb/jlc_api.py`, the `parts_refresh` worker, the community
  `jlcparts` dump). Access is `api.jlcpcb.com` (Parts / Components APIs
  — real-time price, stock, specs), but **gated**: applications are
  reviewed against the applicant's order history and company standing,
  so it is not a signup. For electronics there's also the community
  fallback (the `jlcparts` dump, and jlcsearch's `.json`-suffix
  endpoints); no equivalent public dump for the *mechanical* catalog was
  found. Same platform also exposes **3D-printing/CNC ordering APIs**
  (JLC3DP), which is the other half — see below.
- **Fabrication services are a second, separate integration** and land
  with rungs 4–5, not here: once we emit a DXF cut file or a STEP, an
  instant-quote endpoint turns it into a price and an order. JLC3DP
  (3D print / CNC / sheet metal) and PCBWay are the Chinese route;
  Xometry / Protolabs the EU-US one. Deliberately *not* rung 2 work —
  it consumes the outputs rung 5 produces.

Sequencing that falls out: **do not integrate a supplier for rung 2.**
Standards data alone makes "M6×30 8.8 socket cap" resolvable to geometry,
ports and a size table; a supplier only becomes necessary when someone
wants a price, and by then the `component` row it enriches already
exists.

## Engine 2 — mechanism → geometry propagation

**The highest-leverage building block, and nothing in the tree does any
of it today.** se slice 3 made a mechanism *demand a relation*; it has
never made a mechanism *change a part*. Off-the-shelf assembly is almost
entirely this:

- `screw` ⇒ a clearance hole through every intermediate member (Ø from a
  fit class — ISO 273 fine/medium/coarse for M6 = 6.4/6.6/7.0, house
  default `d + 0.2` = 6.2; see the resolved open question), a counterbore or head
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
- ~~**Component series + size table**~~ — **not a schema addition after
  all** (rung 2a): the series registry is a versioned data *file* in core
  (`precis/data/component_series.json`), and the only migration it needed
  was 0152's ten universal geometric-extent specs.

## Ship order

Rungs 1–3 are mode-independent and pay off even in an all-FDM design;
4–5 are where plexiglass and steel pipe come out the far end.

1. **`se_bom` + component binding + `purchase` mode** — bought things
   exist, with cost/mass rollup and *no geometry at all*. Closes slice
   3's deferred `bearing`-demands-a-BOM check. Cheapest useful rung; the
   only one that does not depend on slice 5.
2. **Catalog → geometry + port templates** — bought parts join
   clearance / DOF / connectivity / `envelope_fit`.
   - **2a (built)** — the series registry + mint (above): a bought part
     now has *dimensions*, in core, resolvable from a colloquial name.
   - **2b — next**, and the se half: envelope generators per category
     (spec values → a cad DSL config) and port templates per category, so
     `connect` can attach to a bought part at all. Reads
     `component_specs.canonical_unit` and converts to metres; se is
     float64 metres everywhere and the component store is mm.
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

- ~~**Series table home**~~ — **resolved 2026-09-05**: core, beside
  `component`. The catalog survey settled it: a series table is
  *standards* data (BOLTS/ISO tables), not se-specific and not
  supplier-specific, so the whole tree can use it.
- ~~**Fit classes as data or as a table?**~~ — **resolved 2026-09-05
  (Reto): data, four tiers, house default `d + 0.2`.** Splits the same
  way the series question did:
  - **The published table is standards data** — ISO 273 (= DIN EN 20273)
    gives three classes, fine / medium / coarse, keyed by thread size:
    M3 3.2/3.4/3.6 · M4 4.3/4.5/4.8 · M5 5.3/5.5/5.8 · M6 6.4/6.6/7.0 ·
    M8 8.4/9.0/10.0 · M10 10.5/11.0/12.0 · M12 13.0/13.5/14.5 ·
    M16 17.0/17.5/18.5 · M20 21.0/22.0/24.0. Lands beside
    `component_series.json` in core, same posture: a file, keyed by
    `thread_size`. (Consistency check the data already passes: the ISO
    273 *fine* column is exactly the ISO 7089 washer bore — 3.2, 4.3,
    5.3, 6.4, 8.4, 10.5, 13, 17, 21 — so the two tables agree where they
    overlap.)
  - **The class you build to is a house/process choice** — se-side
    capability data, one lookup, so no consumer hardcodes a number.
    **House default is `d + 0.2`** (M6 → 6.2), Reto's shop rule.
  - Worth recording because it bites in two places: `d + 0.2` is
    *tighter* than ISO 273 fine (6.4 for M6), so it assumes accurately
    located holes — 0.1 mm radial slack per hole means a two-hole pattern
    binds on a 0.1 mm position error, and the stamping pass should say so
    rather than silently emit a pattern that won't assemble. And on a
    laser (rung 4) the kerf widens the hole beyond nominal, so `+0.2` in
    the cut file is not `+0.2` in the part — the fit lookup and the kerf
    compensation have to compose, not both be applied.

  Unbuilt: the table lands with **rung 3**, its first and only consumer
  (clearance-hole stamping). Nothing in rungs 1–2 reads it.
