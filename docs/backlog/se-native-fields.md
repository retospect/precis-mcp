---
status: draft
pillar: 3d-design
prio: normal
model: opus
title: se native fields — one 3-D conservation-law solver ∇·(K∇u)+S on a cut-cell grid from the cad solid; heat, DC current, electrostatics, species, Darcy as coefficient sets; Joule coupling; thermoelastic via scikit-fem; the `evaluate` toolkit shared with the flow kernel
---

# se native fields — solids and bulk as one field solver

Reto, 2026-10-22: "Could our se model do this all natively? It's all
fields." Yes, for one family: every physics whose steady state is a
conservation law `∇·(K∇u) + S = 0` is the same sparse operator with a
different coefficient set and boundary vocabulary. Heat conduction,
DC current, electrostatics, species diffusion and Darcy flow are that
family. Linear elasticity (with thermal expansion) is the vector cousin
and takes a finite-element path. What is *not* that family — inertia,
turbulence, free surfaces, waves — stays with the external backend
(`se-multiphysics.md` slice B) and is out of scope here.

Code-first (Reto, same day): designs are built by Python scripts in
workspaces ([structure-workspaces](structure-workspaces.md)); the server
keeps inspection. So the kernel is a library the sandbox script calls
through one toolkit function, `evaluate(shape, physics=[...], spec)`,
shared with the thin-gap flow kernel ([se-flow-kernel](se-flow-kernel.md)
slice 4). The server recomputes the same measures on import and never
trusts the script's numbers.

## Scope boundary — three layers, one vocabulary

| layer | package | carries | does not carry |
|---|---|---|---|
| thin-gap kernel (exists) | `src/precis_flow/` + `src/precis_se/flow.py` | depth-averaged 2-D fields in a thin fluid gap: pressure/flow, tracer, species, heat *in the fluid*, EO, slip, compliance (`se-multiphysics.md`) | anything across the gap, solids |
| **native fields (this spec)** | `src/precis_fields/` + `src/precis_se/fields.py` | 3-D steady diffusion-family fields in solids and bulk: heat, DC current, electrostatics, species, Darcy; Joule coupling; small-strain elasticity + thermal expansion/stress | advection (slice 3 couples the flow kernel's velocity), transients (later, named), turbulence, RF/EM waves, acoustics, optics |
| external backend B (`se-multiphysics.md`) | `precis_flow.backend` | 3-D Navier–Stokes, turbulence, two-phase with 3-D interfaces; **validation** of both native kernels | nothing a native kernel can hold inside its card |

The boundary between layer 1 and 2 is the regime card: a thin-gap case
whose wall is thick (`t_wall/L > 0.05` or wall `Bi > 0.1`, multiphysics
slice 2) hands the wall to this kernel, not to the backend. Between 2
and B it is the physics list: a request naming `navier-stokes` or
`turbulence` is refused here with `next=` naming the backend case.

## Discretization — decided: cut-cell finite volume on a Cartesian grid for the scalar family; P1/P2 tetrahedral FEM (scikit-fem) for elasticity

What the cad solid gives us decides it. A `precis.cad` design is a CSG
tree with an exact signed-distance field at any point
(`precis/cad/relate.py::component_sdf_np`, `fold.py::expr_sdf_np`),
ray material intervals (`fold.py::material_intervals`) and an AABB
(`bulk.py::expr_aabb`). It has no B-rep, no STEP unless OCP is
installed, and the marching-cubes surface (`fieldmesh.py::field_mesh`)
is derived from the SDF, not the other way round.

| | (a) cut-cell FV on a uniform Cartesian grid | (b) FEM on an unstructured tet mesh | (c) plain voxel FV (no cut cells) |
|---|---|---|---|
| grid from the solid | sample the SDF at cell corners (one vectorised `component_sdf_np` call); volume fraction and face apertures from the corner values; deterministic, no mesh generator | marching-cubes surface → gmsh 3-D (external GPL binary) → tets; mesh quality and generator version enter the result | as (a) without fractions |
| boundary accuracy on a curved solid | aperture-weighted faces + interface area from the cell's SDF crossing: the flux through a curved boundary converges at first order with the **correct area**; the Johansen–Colella embedded-boundary form (cell volume fraction, face apertures, one-sided interface gradient `(u_bc − u_c)/|φ(c)|`) is second order in `u` for Poisson problems | second order (P2); boundary conforming | staircase: surface area is wrong by a constant factor (~1.5 on a sphere) at *every* resolution — never converges in flux |
| operator | the harmonic-mean face operator `precis_flow.flow.solve` already assembles, in 3-D; a material interface is exact for series conductance | standard | same as (a) |
| vector unknowns (elasticity) | awkward: traction BCs on cut faces, locking, no standard embedded form | standard; rigid-body modes, traction and thermal strain are textbook | awkward |
| memory | 7-point stencil, ~150 B/cell with CG; no fill-in | ~15 nonzeros/row/dof; mesh plus hierarchy | as (a) |
| determinism | bitwise: fixed grid from `(solid sha, cells, pad)`, no RNG | gmsh output depends on its version and options; pin and record | bitwise |

Decision: **(a) for the scalar family** — `precis_fields.grid` builds
a cut-cell grid from an SDF callable; `precis_fields.fv` assembles the
operator; verification (§Verification) measures the boundary order on
the cylinder and sphere cases and the card refuses below the resolution
line. **(b) for elasticity** (slice 2) — scikit-fem (BSD-3) on a tet
mesh from gmsh run as a subprocess in the job container (never
imported into precis; Q2), with a hex-mesh fallback straight from the
cut-cell grid (staircase, P1, no gmsh) for verification and for cases
that need no curved boundary. (c) is rejected: a wrong area that never
converges is exactly the silent error a regime card exists to catch.

### Grid from the solid

`precis_fields.grid.Grid.from_sdf(sdf, lo, hi, *, cells, pad=0.0)`:

- `cells` is the count along the longest AABB axis (the flow kernel's
  `nx` convention); `h = max_extent / cells`; the other axes follow;
  `pad` widens the box (electrostatics needs it; solids use 0).
- corners: one `(N, 3) → (N,)` SDF evaluation; per cell the **volume
  fraction** `α ∈ [0, 1]` from trilinear interpolation of the eight
  corner values (closed form on the corner signs; a 2×2×2 subsample
  when a cell has a sign change on more than two faces), per face the
  **aperture** `a ∈ [0, 1]` from its four corners.
- cells with `α < α_min` (0.01) merge into the neighbour with the
  largest shared aperture (the small-cell fix; the merge count is a
  card line, never silent).
- **interface** per cut cell: area `A_int` from the cell's marching
  cube, centroid, outward normal from the SDF gradient; the distance
  from the cell centroid to the interface is `|φ(c)|`.
- **material id per cell**: the component whose SDF is negative at the
  centroid; two negatives (overlapping components) take the lower
  component index and emit `fields.material_overlap` naming the pair.
  Void cells carry no unknown — except in electrostatics, where the
  padded box is a dielectric (`medium=` slug, default `vacuum`, ε_r = 1).
- multi-block se designs: the adapter poses every cad-bound block into
  the design frame (the `_chamber_placed` pattern in
  `precis_se/flow.py`) and folds them into one `(Design, Expr)` union
  with a component → block map; a script passes the `Design` it built.
- `Grid.sha` = sha256 over `(sdf identity, lo, hi, cells, pad, α_min)`;
  two builds of the same design give the same sha and bitwise-equal
  fractions.

### The operator

`precis_fields.fv.assemble(grid, K, bcs, S) → (A, b)` for `ndim ∈
{2, 3}`:

- face conductance `C_f = a_f · A_f · harm(K_i, K_j) / h` (harmonic mean,
  exact for series layers); `K` is a scalar per cell or a 3×3 tensor
  per cell (anisotropic conductivity; only the diagonal couples on a
  Cartesian face, the off-diagonal needs the 9-point stencil
  multiphysics slice 1 adds to `precis_flow.fv` — the 2-D operator
  there and this one are **one module**: multiphysics's `precis_flow.fv`
  extraction becomes `precis_fields.fv` with `ndim=2`, and its fv-parity
  test (1e-12 on the hex chamber) carries over);
- BC on a cut cell's interface: Dirichlet `C_int = K · A_int / |φ(c)|`
  against `u_bc`; Neumann adds `q · A_int` to `b`; Robin (convective)
  adds `h_c A_int` to the diagonal and `h_c A_int u_∞` to `b`; a cut
  face with no BC is insulated (natural);
- a BC is attached to a cell when its interface centroid lies within
  `0.5 h` of the patch rectangle's plane and inside its bounds
  (`precis.cad.patch`, §Vocabulary);
- the matrix is symmetric positive (semi-)definite; a pure-Neumann
  problem (DC current with only `current` BCs) pins one cell to 0 and
  reports the floating potential.

`precis_fields.solve.solve(A, b, *, method)`: `direct` (SuperLU,
≤ `DIRECT_CELLS` = 5e4 cells — verification cases, bitwise
reproducible), `cg` (Jacobi, always available), `amg` (pyamg
smoothed aggregation when installed, Q6). Reported: method,
iterations, final relative residual (target 1e-10), seconds, bytes.

## The physics registry — coefficient sets, pluggable

`precis_fields.physics.Physics` is a frozen dataclass: `name`,
`unknown` (symbol, unit), `coefficient` (material property id → `K`,
optionally a function of another field), `sources`, `bc_kinds`,
`derived` (what is computed from `u` and `K`), `card` (the validity
lines, §Card). `REGISTRY: dict[str, Physics]`; a plugin adds a row.
Material property ids are the `material` kind's registry
(`src/precis/migrations/0092_material_kind.sql`); a missing property
makes the coefficient not-applicable and the computer returns a reason
naming the `put(kind='material', property=...)` call — never a default.

| physics | `u` | `K` from material | `S` | patch BC measurands | derived outputs (measurands) |
|---|---|---|---|---|---|
| `heat` | `T` (K) | `thermal_conductivity` (W/m·K) | volumetric heat (W/m³): Joule power from `dc-current`, or a `heat-source` region measure | `temperature` (Dirichlet), `heat-flux` (W/m², Neumann), `convection-coefficient` + `ambient-temperature` (Robin) | `max-temperature`, `patch-temperature` (area mean), `heat-flux-total` (W through the datum patch), `thermal-resistance` (K/W between the datum patch and the Dirichlet patch) |
| `dc-current` | `φ` (V) | `σ = 1/electrical_resistivity` (S/m); `σ(T) = σ₀ / (1 + α_ρ (T − T₀))` when `resistivity_temp_coeff` (1/K, Q4) exists | none | `voltage` (Dirichlet), `current` (A: uniform `J = I/A_patch`, Neumann; the floating potential is reported), insulated default | `electrical-resistance` (Ω = ΔV/I), `current-total` (A through the datum patch), `joule-power` (W), `max-current-density` (A/m²) |
| `electrostatics` | `φ` (V) | `ε = ε₀ · relative_permittivity`; void = `medium` (ε_r 1) | `charge-density` region measure (C/m³) | `voltage`, `surface-charge-density` (C/m², exists in the measurand seed), box faces Neumann 0 (Q5) | `capacitance` (F between the datum patch and the other Dirichlet patch), `max-field` (V/m), `field.breakdown` finding when `max-field > dielectric_strength` |
| `species` | `c` (mol/m³) | `D` from the `species-diffusivity` region measure (multiphysics slice 1 vocabulary; not a material property) | first-order sink via a patch Robin from `wall-rate-constant` (m/s) | `concentration`, `species-flux`, `wall-rate-constant` | `species-flux-total` (mol/s per patch), `min-concentration`, `damkohler` (gauge) |
| `darcy` | `p` (Pa) | `κ/μ`: `permeability` region measure (m²), `μ` from the fluid material (`fluid=` slug, multiphysics item 2) | none | `pressure`, `flow-rate` (m³/s, uniform flux), impermeable default | `flow-rate-total`, `hydraulic-resistance` (Pa·s/m³) |
| `elastic` (slice 2) | `u` (m, vector) | `youngs_modulus`, `poissons_ratio`; thermal strain `α ΔT I` from `thermal_expansion_coeff` and the `heat` field | body force `gravity` optional | `fixed` (u = 0), `displacement` (m), `traction` (Pa), `force` (N total, uniform traction) | `max-displacement`, `max-von-mises` (Pa), `reaction-force` (N per fixed patch), `safety-factor` (= `tensile_strength_yield` / max von Mises) |
| `flow` (thin-gap, exists) | `p*` | `h*³` | — | `flow:` token ports | the four `FLOW_COMPUTERS` measurands |

Measurand slugs that do not exist yet need a core taxon seed migration
(release owner, as multiphysics says).

### Coupling — Picard, reported

`precis_fields.coupled.solve(grid, physics=[...], couplings=[...])`
is fixed-point iteration over the pure per-physics solves:

- **Joule** (first; Reto): `dc-current` → `S_heat = σ |∇φ|²` → `heat` →
  `σ(T)` → repeat. Converged when the relative change of `max T` and
  of every patch current is below `tol` (1e-6) — at most `max_iter` 50;
  under-relaxation `ω = 0.7` switches on when the residual rises twice;
  a residual still rising after that is the refusal
  `coupling.diverged` carrying the last three residuals and `next=`
  naming `omega=` / a job. Without `resistivity_temp_coeff` the loop is
  two solves (one-way) and the card's `sigma_t` line says so.
- **thermoelastic** (slice 2): `heat` → `T` interpolated to mesh nodes
  → `elastic`; one-way (no thermo-mechanical feedback).
- **advection** (slice 3): the flow kernel's `U*` re-dimensionalised as
  the advection velocity in the fluid cells for `species`/`heat`.

Every `Outcome` carries `coupling: {iterations, residuals[], omega,
converged}`. Convergence is reported, never assumed.

## Vocabulary — se measures, patches, regions, materials (as se-multiphysics defines them)

Nothing new in se. The three additions multiphysics made are reused
unchanged: BCs are measures on `patch:` regions with measurand slugs
(`origin='user'`), materials are `material` rows, outputs are measures
through `COMPUTERS`.

- **Case token.** A pocket whose shape is `fields:physics=<name>[,<name>…]
  [;cells=<n>][;pad=<m>][;medium=<slug>][;fluid=<slug>]` on the block
  that is the solid; cad-bound child blocks are part of the solid with
  their own materials. `cells` defaults to `DEFAULT_CELLS` = 48; the
  server never runs a token above `INLINE_CELLS` inline (§Budgets).
- **BC measures** on patches, measurand slugs per the registry table;
  a Robin pair (`convection-coefficient` + `ambient-temperature`) is
  two measures on the same datum.
- **Materials.** A block's material is its mode key's material half
  (`precis_se/modes.py::parse_mode`) read as a `material` kind slug
  (Q3); a half that resolves to no row makes every coefficient line
  not-applicable with a reason naming the block. A sandbox script
  passes either a slug (resolved through the read MCP,
  `get(kind='material', id=…)`, staged as `/work/materials.json` at run
  start for the blocks' slugs) or a literal property dict; the card
  records `material: row | literal`, and the import re-check uses the
  row.
- **Patch resolver, DB-free.** The face resolver in
  `precis_se/datums.py` (`_face_frame` and the `patch:` grammar) moves
  to `precis.cad.patch` so a script resolves `patch:` on the `Design`
  it built; `datums.py` imports it (one implementation).
- **Outputs.** `FIELDS_COMPUTERS` in `src/precis_se/fields.py`,
  registered lazily like `FLOW_COMPUTERS`
  (`precis_se/properties/__init__.py::_LAZY_TABLES` gains `"fields"`).
  `hard` → DRC gate, `soft` → a script's objective term, `gauge` →
  reports.
- **`view='fields'`** on the design: the card per physics, per-physics
  extremes with the cell's position, per-patch integrated fluxes, the
  coupling block, the solver block; `args={'physics': …}` narrows;
  `format='json'` serves the fields as float32 blobs on the grid
  (shape, `h`, `lo`) for the web viewer (pointer only; no UI in this
  spec).

## Card — validity per physics, and refusals

`precis_fields.card.FieldsCard`: the `RegimeCard` shape
(`precis_flow/regime.py::RegimeLine`: value, threshold, verdict,
applicable), one line group per physics plus a `grid` group. A line
with missing inputs is not-applicable and never blocks `ok`.

| group | line | number | holds when | else |
|---|---|---|---|---|
| grid | `resolution` | thinnest material feature / `h` (min over components of the smallest AABB extent, and the shortest inside chord sampled on the three axes) | ≥ 4 | refuse `fields.resolution`, `next=` names `cells=` or the job |
| grid | `cut_fraction` | cut cells / solid cells | informational | — |
| grid | `merged_cells` | count merged below `α_min` | ≤ 1 % of solid cells | refuse `fields.small_cells`, `next=` names `cells=` |
| grid | `solver` | residual | ≤ 1e-8 | refuse `fields.unconverged` naming iterations |
| heat | `steady` | Fourier `Fo = α t / L²` when a `time` is given | ≥ 3 (steady reached) | refuse: transient out of scope (later slice) |
| heat | `biot` | `h_c L_c / k` | informational: ≤ 0.1 means a lumped estimate would do; the field answer stands either way | — |
| heat | `radiation` | `ε σ_SB (T_max⁴ − T∞⁴) / (h_c (T_max − T∞))` when `emissivity` given | ≤ 0.1 | refuse: radiation not modelled (later, as a linearised Robin) |
| dc-current | `sigma_t` | `α_ρ ΔT` | ≤ 0.05 → constant σ within 5 %; else coupling on | informational |
| dc-current | `contact` | — | constant line: contact resistance not modelled | — |
| electrostatics | `box_margin` | `pad / max object extent` | ≥ 3 | refuse `fields.box_margin` (Q5) |
| electrostatics | `breakdown` | `max-field / dielectric_strength` | < 1 | finding `field.breakdown` (an outcome, not a refusal) |
| species | `damkohler` | `k_s L / D` | informational | — |
| darcy | `reynolds_pore` | `ρ q √κ / μ` | ≤ 1 | refuse: Forchheimer/inertial regime |
| elastic | `small_strain` | max principal strain | ≤ 0.01 | refuse `fields.large_strain` |
| elastic | `deflection` | max displacement / thinnest feature | ≤ 0.1 | refuse: geometric nonlinearity |
| elastic | `elements_through` | elements across the thinnest feature | ≥ 2 (P2), ≥ 4 (P1) | refuse, `next=` names mesh size |
| elastic | `yield` | max von Mises / `tensile_strength_yield` | < 1 | finding `stress.yield_exceeded` |

Kernel refusals are `ValueError` naming `fields.<line>`; the adapter
and the toolkit map them to `BadInput` with `next=`.

## Budgets — memory, resolution, inline vs job

Spark is unified memory (~119 GB shared with any model it serves),
pollux similar; earlyoom (`deploy/roles/hardening`) SIGTERMs the
largest process at 10 % free, so a job near 100 GB is killed, not
swapped. The kernel estimates bytes **before** allocating and refuses
above the lane's cap.

| lane | cells (total) | memory estimate | solver | where |
|---|---|---|---|---|
| inline (MCP process; a `view='fields'` read; a script call inside its run) | ≤ `INLINE_CELLS` = 3e5 (~64³) | ≤ ~60 MB | cg / amg | the session host or the sandbox run |
| job (`fields_solve` job type on pollux/spark; a script's `budget='job'` request) | ≤ `JOB_CELLS` = 3e7 (~310³) | ≤ ~12 GB at ~400 B/cell with the AMG hierarchy | amg (cg fallback, slower) | compute lane |
| refused | above `JOB_CELLS`, or estimate above `cgroup memory.max` | — | — | `next=` names `cells=` |

Direct solves only to `DIRECT_CELLS`. A script's `evaluate` above
`INLINE_CELLS` refuses with `next=` naming `budget='job'`; that writes
a request (the pcb `requests[]` pattern, keyed on `(grid sha, physics,
spec sha, kernel version)`) the importer dispatches, and the next run
reads the cached `Outcome`. Inside a run the cgroup cap
(`/sys/fs/cgroup/memory.max`) is the hard ceiling. Starting values
above; slice 1 measures real bytes per cell on the Joule wire at 64³
and 128³ and writes them into this item.

## Determinism

No RNG anywhere in `precis_fields`. The grid is a pure function of
`(sdf, lo, hi, cells, pad)`; assembly order is the cell index order;
CG/AMG run a fixed iteration order, so the same inputs on the same
machine give bitwise-equal `Outcome`s, and across machines agree to
≤ 1e-12 relative on the verification cases (test). `Outcome.cost`
records `grid_sha`, `kernel_version`, `solver`, `iterations`,
`residual`, so two runs are comparable by their stamps. The common
random numbers rule of the flow tracer (`seed=`) is honoured by
`evaluate` and ignored by the fields kernel.

## The toolkit — `evaluate(shape, physics=[...], spec)` shared with the flow kernel

`precis_geom.physics.evaluate` lives in the `precis-geom` wheel
(structure-workspaces §Packaging); it is the one entry for a sandbox
script and for the server's computers.

```python
import precis_geom as pg
import precis_geom.physics as pgp

d = pg.Design(...)                       # the script's cad graph
spec = {
  "materials": {"body": "copper-c110", "fin": {"thermal_conductivity": 200.0}},
  "bc": {"dc-current": {"patch:a": {"voltage": 0.0}, "patch:b": {"current": 10.0}},
         "heat": {"patch:fins": {"convection-coefficient": 25.0, "ambient-temperature": 293.15}}},
  "measures": [
    {"name": "t_max", "measurand": "max-temperature", "strength": "hard", "max_value": 350.0},
    {"name": "r", "measurand": "electrical-resistance", "datum": "patch:a", "strength": "soft"},
  ],
  "cells": 64,
}
o = pgp.evaluate(d, physics=["dc-current", "heat"], spec=spec)
o.measures["r"], o.measures["t_max"], o.hard_ok, o.cards["heat"].ok, o.cost.seconds
```

`Outcome`: `measures: {name: value}` (physics outcomes only),
`cards: {physics: FieldsCard | RegimeCard}`, `hard_ok: bool`,
`failures: [(name, value, bound)]`, `coupling`, `cost: {cells, bytes,
seconds, grid_sha, kernel_version}`, `fields` (lazy array handles,
never serialised into the manifest). `physics=['flow']` routes to
`precis_flow` (`solve` + `trace`, `seed=` = common random numbers);
the scalar names route to `precis_fields`; `['elastic']` to the
scikit-fem slice; a mixed list runs the couplings the registry knows.

**Physics only.** Reto, 2026-10-22: "hole count will not be the
answer to evaluate shape. That's what the tool makes the space to
be." `Outcome.measures` contains registered measurands and nothing
else — no counts, mass, price, manufacturability. Those are the
script's own objective terms (it has the geometry). A test asserts the
key set is a subset of the registry and that `evaluate` is pure over
`(shape, spec, seed)`.

**Spec = case.** The `measures` list is the same JSON the manifest's
`measures` carry (`MeasureSpec` fields: `measurand`, `strength`,
`value`/`min_value`/`max_value`, `datum`, `origin`), so the script's
target and the stored design's target are one object. `out.physics(
block, physics=[...], spec)` writes the manifest entry the importer
turns into the `fields:` pocket, the BC measures (`origin='user'`) and
the result measures.

**Optimizer loop.** The guidance lives in the skill
`precis-workspace-physics-help` (CFD first section, then fields):
objective = Σ weight·soft measures + script terms; any proposal with
`hard_ok == False` is rejected before its cost is read; fix `seed=`
for common random numbers; budgets (`cost.seconds` per call, inline vs
`budget='job'`, checkpoint the search in `/work/out/search.json` and
resume via `params.resume`); record the search with `out.search(
iteration, cost, measures)` into `out/trace.json` so `view='trace'`
renders the path; gradients: `precis-differentiation-help` — the
scalar operator is self-adjoint, so `dJ/dK` and `dJ/d(patch shape)` are
one extra solve each (adjoint sensitivities, a later slice); the
tracer is Monte-Carlo and non-smooth, so flow cases anneal.

## Server-side independent re-check on import

The importer (`precis_se/workspace_import.py`, structure-workspaces
slice 0/1) runs `COMPUTERS` on every imported measure whose measurand
is a fields or flow slug, at the token's `cells`, on the imported
solid, with the DB material rows. The manifest's stated values are
advisory: equal `cells` and `|Δ| ≤ 1e-6` relative → stored as the
server's value with `origin='proposed'`; a larger gap → finding
`measure.script_mismatch` naming both numbers; different `cells` →
INFO `measure.resolution_differs` with both resolutions and the delta.
Hard measures go through the existing DRC hard gate (`precis_se/drc.py`)
on the server's numbers only. A script cannot pass a hard constraint
by asserting it.

## Verification

Kernel tests `tests/test_precis_fields.py` (analytic), adapter tests
`tests/test_se_fields.py`, backend cross-checks `tests/test_fields_backend.py`
(skipped without the backend lane). Tolerances are asserted, orders
are measured and written into this item.

| case | geometry | analytic | assert |
|---|---|---|---|
| slab | axis-aligned slab `L`, `k`, `T₁/T₂` | linear `T`, `q = kΔT/L` | ≤ 1e-12 (exact for the harmonic operator) |
| two-layer slab | `k₁`, `k₂` in series | `R = L₁/k₁ + L₂/k₂` | ≤ 1e-12 |
| rod (fin) | cylinder `R, L`, base `T_b`, convection `h_c` on the side, insulated tip | `θ/θ_b = cosh m(L−x)/cosh mL`, `m² = 2h_c/(kR)` | ≤ 1 % at `h = R/8`; error ratio `h → h/2` ≥ 1.8 |
| resistor | bar `ρ, L, A` | `R = ρL/A` | ≤ 1e-12 |
| L-conductor | two bars at right angles | Elmer (backend B) | ≤ 2 % on `R` at the finest grid |
| Joule wire, constant σ | wire `R`, current `I`, convection `h_c`, `k` | `T_c − T_s = q‴R²/(4k)`, `T_s − T∞ = q‴R/(2h_c)`, `q‴ = ρJ²` | ≤ 1 % at `h = R/8` |
| Joule wire, σ(T) | same with `α_ρ` | the implicit closed form solved by `brentq` | ≤ 1e-6 after Picard convergence; iterations reported; `ω` kicks in on a contrived `α_ρ` that oscillates |
| hollow cylinder | `R_i, R_o`, `T_i, T_o` | `T ∝ ln r` | ≤ 2 % at `h = R_i/8`; order ≥ 1.8 |
| concentric spheres | `R₁, R₂`, `V₁, V₂` | `C = 4πε₀ R₁R₂/(R₂−R₁)` | ≤ 2 % at `h = R₁/8` |
| species slab | one reactive wall `k_s` | linear profile, flux `= D c₀ / (L + D/k_s)` | ≤ 1e-12 |
| Darcy slab | `κ, μ, Δp` | `Q = κAΔp/(μL)` | ≤ 1e-12 |
| uniaxial bar (slice 2) | bar `L, A, E`, force `F` | `δ = FL/(EA)` | ≤ 1e-10 (P1, linear field) |
| cantilever (slice 2) | `L, t, w`, tip force | Euler–Bernoulli `δ = FL³/(3EI)` | ≤ 3 % with P2 at 4 elements through `t`; P1 at the same mesh recorded (locking) |
| free thermal bar (slice 2) | `α, ΔT` | `δ = αΔT L`, `σ = 0` | `δ` ≤ 1e-8; `σ` ≤ 1e-8 E |
| constrained bar (slice 2) | fixed both ends | `σ = −EαΔT` | ≤ 1e-8 |
| bimetal strip (slice 2) | two layers | Timoshenko curvature | ≤ 3 % |
| Elmer cross-check | Joule wire, concentric spheres, L-conductor | backend B2 (Elmer, as a job) | ≤ 2 % on integrated quantities |
| FEniCSx cross-check | bimetal strip | backend B1 | ≤ 2 % on tip deflection |
| determinism | Joule wire | — | bitwise-equal `Outcome` twice; cross-machine ≤ 1e-12 |
| size invariance | cylinder at 1 mm and 50 cm | — | identical reduced fields when `k, h_c, L` scale together |

## Slices

| # | delivers | extends | acceptance | size |
|---|---|---|---|---|
| 1 | **Scalar family + Joule.** `src/precis_fields/` (`grid.py`, `fv.py` N-d — absorbs multiphysics's `precis_flow.fv`, `physics.py` registry with heat/dc-current/electrostatics/species/darcy, `coupled.py` Picard + Joule, `card.py`, `solve.py` direct/cg/amg); `precis_geom.fields.grid_from_design` (SDF from `component_sdf_np`), `precis.cad.patch` (resolver moved out of `datums.py`), `precis_geom.physics.evaluate` gains the scalar names; `src/precis_se/fields.py` (`fields:` token, BC measures → kernel BCs, material rows → coefficients, `FIELDS_COMPUTERS`, `view='fields'`), `_LAZY_TABLES['fields']`, the `fields_solve` job type + request table reuse, measurand seed migration, import-linter contract for `precis_fields`, the fields section of `precis-workspace-physics-help`, BC lines in `precis-se-regions-help` | `precis_se/properties/__init__.py`, `precis_se/datums.py`, `precis_se/drc.py` hard gate, `workspace_import.py` re-check, `packaging/geom/` | every analytic case in §Verification up to Darcy passes at its tolerance with its order recorded; fv parity 1e-12 on the hex chamber; a hard `max-temperature ≤ 350 K` on a wire that reaches 400 K is a DRC finding; a missing `electrical_resistivity` gives a reason, never 0; `evaluate` twice is bitwise equal; a 128³ request inline refuses naming `budget='job'`; the job lane solves it and the next run reads it; bytes/cell at 64³ and 128³ written into this item; a tampered manifest value yields `measure.script_mismatch` | L, 2–3 builds; after structure-workspaces slice 2 and se-flow-kernel slice 4 (the toolkit entry and the skill exist) |
| 2 | **Thermoelastic via scikit-fem.** `precis_fields.mesh` (marching-cubes surface → gmsh subprocess → `.msh` via meshio; hex fallback from the cut-cell grid), `precis_fields.elastic` (P1/P2 vector problem, λ/μ from E/ν, thermal strain from the slice-1 `T`, BCs `fixed`/`displacement`/`traction`/`force`, rigid-body near-nullspace for AMG), the `elastic` card group, outputs `max-displacement`/`max-von-mises`/`reaction-force`/`safety-factor`; `[fields]` extra (`scikit-fem`, `meshio`, optional `pyamg`); gmsh binary in the job image | `pyproject.toml`, `deploy/` job image, `view='fields'` | the five slice-2 verification rows pass; P1-vs-P2 locking recorded on the cantilever; the FEniCSx bimetal cross-check ≤ 2 %; a yield exceedance is `stress.yield_exceeded` with the element's position; the hex fallback runs the uniaxial bar without gmsh | M–L, 2 builds; after 1 |
| 3 | **Advection coupling with the flow kernel.** The fluid cells of a thin-gap chamber carry `U` from `precis_flow` (re-dimensionalised via the scale triple), species/heat advect in the fluid and conduct in the solid (conjugate heat transfer), interface continuity through the harmonic face; `peclet` cell line on the card; TVD limiter as multiphysics slice 1 | `precis_fields.fv` advection term, `precis_se/fields.py` + `flow.py` | the multiphysics slice-1 Taylor-dispersion case run as a 3-D fluid-plus-wall grid agrees with the depth-averaged result within 5 %; a heated wall under a flowing chamber reproduces the Nu = 8.23 plate coupling within 5 % | M, 2 builds; after 1 and multiphysics slice 1 |

Later, named: transient (implicit Euler / Crank–Nicolson on the same
operator), adjoint sensitivities for `evaluate`, radiation as a
linearised Robin, anisotropic 9-point stencil in 3-D, the web fields
viewer.

## Target + blast radius

New `src/precis_fields/` (numpy + scipy only; import-linter contract
like `precis_flow`'s in `pyproject.toml`), new `src/precis_se/fields.py`,
new `precis_geom.fields` / `precis_geom.physics` / `precis.cad.patch`
(wheel modules, structure-workspaces §Packaging), `precis_se/properties/
__init__.py` (`_LAZY_TABLES`, `COMPUTERS`), `precis_se/datums.py` (import
the moved resolver), `precis_se/drc.py` (no change; the hard gate reads
computed values), `workspace_import.py` (re-check), a measurand seed
migration, `workers/job_types/fields_solve.py` + the request table,
`pyproject.toml` (`[fields]` extra), the job image (gmsh, slice 2),
skills `precis-workspace-physics-help` (fields section),
`precis-se-regions-help` (BC lines), `precis-material-help`
(`resistivity_temp_coeff`), `docs/glossary.md` (coefficient set, cut
cell). `precis_flow` loses `fv` to `precis_fields` once multiphysics
slice 1 has extracted it (or never gains it if this lands first).

## Open questions (default taken if unanswered)

- Q1 Cut-cell FV for the scalar family (default) vs immersed/conforming
  FEM. The default stands unless the cylinder and sphere cases miss
  2 % at `R/8`; then the scalar family moves onto the slice-2 mesh.
- Q2 gmsh as a subprocess binary in the job container (default; GPL
  tool never imported, the policy multiphysics slice B set) vs the
  `gmsh` PyPI wheel in-process. The hex fallback needs neither.
- Q3 Block material = the mode key's material half read as a
  `material` slug (default) vs a new `material=` block attribute. The
  default adds no op; a free-text half that resolves to no row gives
  a reason.
- Q4 `resistivity_temp_coeff` minted as a `proposed` material property
  through the existing `put(kind='material', property=..., unit='1/K')`
  path (default) vs a core migration.
- Q5 Electrostatics far field: Neumann 0 on the padded box with the
  `box_margin ≥ 3` line (default) vs a Robin/infinite-element far field
  later.
- Q6 `pyamg` as an optional extra (default; MIT; CG + Jacobi always
  works, slower) vs required.
- Q7 A script's over-budget `evaluate` refuses with `next=` naming
  `budget='job'` (default) vs silently queueing the job — the default
  keeps a run's cost visible to the script that pays it.
