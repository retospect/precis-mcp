---
status: draft
pillar: 3d-design
prio: normal
model: opus
title: se multiphysics — species, heat, electro-osmosis, Darcy, compliance, slip, power-law and two-phase on the thin-gap flow kernel, a pluggable external 3-D backend, and the se vocabulary (patch BCs, material measures, output measures) that carries them
---

# se multiphysics on the thin-gap kernel

Builds on `src/precis_flow/` (slice 1 of `se-flow-kernel.md`) and
`src/precis_se/flow.py` (slice 2, `1d0dffdac`). Slice 1 here: species
advection–diffusion with Taylor dispersion and wall reactions. Later
slices add one physics each on the same grid and operator. A pluggable
external 3-D backend (FEniCSx / Elmer / OpenFOAM) takes what the thin-gap
form cannot carry.

## Reto's question: "truly multi-physics, what can it take, will se support it"

**Answer.** Yes, within one boundary. The thin-gap form can carry a physics
when its profile across the gap has a known closed form (parabolic, plug,
linear, a thin exponential boundary layer). That profile integrates out
and leaves a 2-D field equation `∇·(T ∇φ) = S` on the existing grid,
with the same harmonic-mean face operator `flow.solve` already
assembles. All eight physics below fit that rule. The form breaks when the
across-gap structure carries its own dynamics: inertia, deep pockets with
recirculation, nanometre gaps, viscoelastic stresses, drops smaller than
the gap, 3-D buoyant convection. Those cases go to the external 3-D
backend (slice B) or to the nano propagator (`se-flow-kernel.md` slice 3).
The regime card says which, per case, with a line per physics.

se supports this without a new object. It needs three additions to
machinery that exists:

1. **Boundary conditions are measures on `patch:` regions.** An inlet
   concentration, a wall rate constant, a ζ-potential, a slip length or
   a wall temperature is a declared measure (`origin=user`) with a
   measurand slug on the patch (`se-region-property-layer.md` slice A,
   live). The `flow:` pocket token names the roles only (`in=`, `out=`,
   and the new `fluid=`).
2. **Material properties come from measures.** The fluid is a `material`
   row bound by `fluid=<slug>` on the `flow:` token. μ, ρ, D, k, c_p, σ,
   ε_r, γ, K and n are read from that row. Wall properties (E, ν,
   thickness, k_wall) are read from the block's material. A missing
   property makes its card line not-applicable, and the measure computer
   returns a reason. This is the slice-2 pattern; no number is filled in
   silently.
3. **Outputs are measures through `COMPUTERS`**, as the four flow
   measurands are today. A `hard` measure is a DRC gate, a `soft`
   measure is an annealer objective (`se-flow-kernel.md` slice 4), and a
   `gauge` measure only reports.

The backend can change (kernel, FEniCSx, OpenFOAM) without any change to
se: the `Case` (slice B) is built from the same three inputs.

### What the thin-gap form carries

| Physics | Across-gap closure | 2-D equation (transmissivity / source) | Breaks when (card line) | Slice |
|---|---|---|---|---|
| species + wall reaction | Taylor–Aris: transverse mixing faster than transit | `∇·(h𝔻∇c) − ∇·(hŪc) − Σ_w k_eff c = ∂(hc)/∂t`, `𝔻 = D I + h² ŪŪᵀ/(210 D)` | `Pe·h0/L > 0.1` (dispersion not developed): Lagrangian solver, same slice | 1 |
| heat | same as species for the fluid; walls as 2-D conducting plates coupled through `Nu = 8.23 / 5.39` (both / one wall, uniform flux) | `T_fluid` and `T_wall` fields; `μ(T)` feeds back into `h³/μ` (Picard) | thick walls with 3-D conduction (`t_wall/L > 0.05`); buoyant convection across the gap (`Ra_h > 1708`) | 2 |
| electro-osmosis | thin EDL: plug slip `u_eo = (εζ/μ)∇φ` | `∇·(hσ∇φ) = 0`, then the pressure solve with source `−∇·(h u_eo)`; ζ per patch | EDL overlap `λ_D/h0 > 0.01` (needs Poisson–Nernst–Planck: nano / backend) | 3 |
| Darcy regions | porous insert fills the gap | face transmissivity `12 κ h / h0³` inside the region (the solver already reads "Hele-Shaw / Darcy") | `κ/h² > 0.1` (Brinkman layer), or the insert covers only part of the gap height | 4 |
| wall slip | Navier slip `b₁, b₂` | flux factor `(h² + 4h(b₁+b₂) + 12 b₁b₂) / (h(h+b₁+b₂))` on `h³/12μ`; gas: `b ≈ λ` (Maxwell, full accommodation) | `Kn > 0.1` (transition regime): nano propagator; slip moves the Knudsen line from 0.01 to 0.1 | 5 |
| compliant walls | local gap `h = h_rest + w(p)` | Winkler `w = p/K_w`, then Kirchhoff plate `D∇⁴w = p`; nonlinear Reynolds equation (soft-lithography EHL), Newton | deflection `w/h0 > 1` with contact (gap closes: model as `h_min`), or `w/L > 0.05` | 6 |
| power-law fluid | `τ = K γ̇ⁿ` across the gap | `q = −(2n/(2n+1)) (h/2)^{(2n+1)/n} (|∇p|/K)^{1/n} ∇p/|∇p|`: p-Laplacian, Picard on face T | yield stress with unyielded plugs (Bingham), or Weissenberg `Wi > 0.1` (viscoelastic) | 7 |
| two-phase in the gap | pancake interface spanning the gap; Park–Homsy jump `Δp = γ(2cos θ/h + (π/4)κ_∥)` | level set on the 2-D grid, pressure jump at the interface, contact angle θ per patch (hydrophobic region measures) | drop diameter `< 2h` (3-D drop), `Ca > 0.1` (film thickness `~h Ca^{2/3}` no longer small), contact-line pinning dynamics | 8 |

Couplings carried: flow → species, flow → heat (one-way); T → μ → flow,
reaction heat → T → k_s (Picard, two-way); φ → flow (one-way); p ↔ h
(Newton, two-way). Each physics is a pure function
`(Domain, Field, params) -> field` in reduced units, with its own reduced
numbers. A coupled run is fixed-point iteration over those functions.
Convergence is reported, never assumed.

### What it cannot take (goes to the backend or the nano propagator)

- inertia: `Re_h·h0/L > 0.1` (card `reynolds`), turbulence;
- non-thin geometry: `h0/L > 0.05` (card `thinness`). This includes
  pockets with depth/diameter `> 0.5`, where the slice-1 tracer covers
  recirculation only through the `D_v*` dispersion fit;
- `Kn > 0.1` with slip, or EDL overlap: nano propagator / PNP;
- viscoelastic flow, 3-D drops, free surfaces with 3-D shape,
  fluid–structure interaction with large deflection.

## Shared machinery (slice 1 builds it)

- **`precis_flow.fv`**: the cell-centred finite-volume operator taken out
  of `flow.solve`. It assembles `div*(T* grad* φ) + advection = S` with
  harmonic-mean faces and Dirichlet / Neumann / Robin cells. Pressure
  (`T = h³`), potential (`hσ`), conduction (`hk`) and species (`h𝔻`) all
  use it. The full Taylor tensor needs a 9-point stencil for its cross
  term; `fv` provides one.
- **Regime card per physics.** `RegimeCard` gains optional line groups
  (`species`, `heat`, `electro`, …). A group's lines are not-applicable
  unless its inputs are given (the existing `_skipped` rule). Routing
  lines (`taylor`, `wall_damkohler`) carry a route verdict that names the
  solver taking over. `card.ok` stays the admission test for the solver
  that was asked for.
- **Dimension-free stays exact.** A physics adds reduced numbers to the
  shape. One solve per `(shape, numbers)` tuple serves every physical
  size with the same tuple. Results are stored in reduced units, with the
  scale triple and the card, as in slice 2.

## Slice 1 — species advection–diffusion, Taylor dispersion, wall reactions

### Formulation

Reduced numbers: `ε = h0/L`, `Pe = u0 h0/D`, wall Damköhler
`Da_w = k_s h0/D` per reactive wall, transit Damköhler
`Da = n_w Da_w/(Pe ε) = n_w k_s L/(h0 u0)`. `c* = c/c_in`.

**Eulerian, depth-averaged** (`precis_flow.species.transport`):

```
∂(h* c*)/∂t* + ∇*·(h* U* c*) = ∇*·(h* 𝔻* ∇* c*) − (L/(h0 u0)) Σ_w k_eff,w c*
𝔻* = ε [ I/Pe + (Pe/210) h*² U* U*ᵀ ]
1/k_eff,w = 1/k_s,w + 2h/(Sh_w D),   Sh = 7.54 (both walls reactive), 4.86 (one wall)
```

The equation is steady or transient. Advection is upwinded with a TVD
limiter. Inlet cells are Dirichlet `c* = 1` and outlet cells zero-gradient.
It is valid when `Pe ε ≤ 0.1`, i.e. a transverse mixing time
`h0²/D` of at most a tenth of the transit time `L/u0`.

**Lagrangian** (`trace` extended). It is valid for any `Pe ε`, and is the
reference where the depth average fails:
- `pe=` turns on molecular diffusion, horizontal
  `sqrt(2ε/Pe · dt*)·ξ` and vertical in gap-fraction units with
  `D_ψ = 1/(ε Pe h*²)`. It requires `profile='parabolic'`; plug plus
  molecular diffusion is not Taylor dispersion, so that combination is
  refused;
- `walls=` makes the floor and lid reactive. A wall hit is absorbed with
  probability `P = Da_w·sqrt(π dt*/(ε Pe))` (Erban & Chapman 2007), and
  `dt*` is capped so that `P ≤ 0.1`. `Da_w = ∞` means a perfectly
  absorbing wall, `P = 1`;
- the existing `D_v*` dispersion over pockets stays on as the modelled
  unresolved 3-D mixing. Molecular diffusion is added to it and does not
  replace it.

The two solvers cost the most in opposite regimes. Eulerian cost does not
depend on `Pe ε`. The Lagrangian vertical step needs
`~100/(Pe ε)` steps per transit. The card's `taylor` line routes each
case to the cheaper valid solver.

**Products.** Slice 1 handles independent species and one-step chains
`A → B`: B's wall source equals A's wall sink, solved after A. The
equations stay linear. Bimolecular wall kinetics (`r(c_A, c_B)`) waits on
`kinetics-engine-v2.md`.

### Rates from kinetics / catpath

The wall rate per area is `r = TOF / (A_site N_A)` in mol m⁻² s⁻¹.
`TOF` is the quest scalar `tof` (`src/precis/quest/compute.py`) of a
**trusted** solve; the trust gate is the presence of `log_tof`. `A_site`
is catpath's `kinetics.site_area_A2` (one top-layer atom = 1 site). The
first-order effective constant `k_s = r / c_wall` is linearised at the
solve's stated partial pressure (gas phase: `c = p/RT`), times a stated
roughness factor (default 1, named on the card). `log_tof_p5` / `log_tof_p95`
give a conversion band: two more solves at those values, stored with the
measure. The binding is a handler-level op `wall_rate`
(`{"op":"wall_rate","block":…,"datum":"patch:…","source":"quest:<id>","species":…}`).
It reads the store, so it cannot be pure, which is the same reason
`layout_chain` is handler-level. The op writes a `wall-rate-constant`
measure with `origin='proposed'` and records quest id, T, p, roughness
and trust in the measure meta. A rate typed in directly is the same
measure with `origin='user'`.

### Card lines (species group)

| Line | Number | Verdict |
|---|---|---|
| `peclet` (exists) | `u0 h0/D` | ≥ 10 for the particle-tracer reading; the species solvers accept any value |
| `taylor` | `Pe·ε` | ≤ 0.1 → Eulerian; else route to Lagrangian (not a failure when Lagrangian was asked) |
| `wall_damkohler` | `k_s h0/D` | ≤ 0.1 reaction-limited · 0.1–10 mixed (series `k_eff`, Lagrangian is the reference) · ≥ 10 transport-limited (Sh) |
| `damkohler` | `n_w k_s L/(h0 u0)` | informational: expected conversion `~1 − e^{−Da}` |

Worked points: water, `D = 1e-9 m²/s`, `h0 = 100 µm`, `L = 20 mm`
(`ε = 0.005`). `u0 = 0.1 mm/s` gives `Pe = 10`, `Pe ε = 0.05`: Eulerian.
`u0 = 1 mm/s` gives `Pe = 100`, `Pe ε = 0.5`: Lagrangian.

### se surface

- `flow:` token gains `fluid=<material slug>` and
  `species=<name>[,<name>…]`.
- BC measures on patches, with measurands: `inlet-concentration`
  (mol/m³, inlet patch; optional while linear), `wall-rate-constant`
  (m/s, per species, on floor/lid patches), `species-diffusivity`
  (m²/s, on the pocket region; falls back to the fluid material).
  Species scope uses the measure name `<block>.<species>.<property>`
  (Q2).
- Output computers in `FLOW_COMPUTERS`: `outlet-conversion` (ratio),
  `outlet-concentration` (`c*`), `wall-flux-fraction` (fraction of the
  inlet flux consumed on the measure's patch; per-catalyst-patch
  selectivity), `damkohler` (gauge).
- `view='flow'` with `args={'species': …}` adds the `c*` field to the
  text summary and the slice-2b payload (one float32 `(ny, nx)` blob per
  species).

### Acceptance criteria (slice 1)

Tests in `tests/test_precis_flow_species.py` (kernel) and
`tests/test_se_flow_species.py` (adapter).

1. **fv parity.** After `fv` is taken out of `flow.solve`, `solve` on the
   slice-2 hex chamber (`test_se_flow.py` fixture) returns `p*`, `U*`
   and `R*` equal to the pre-refactor values within 1e-12.
2. **Mass balance.** Steady Eulerian on any domain:
   `|inflow − outflow − wall consumption| / inflow ≤ 1e-10`. With no
   reaction on a uniform rectangle, outlet `c* = 1` within 1e-10.
3. **Taylor dispersion, Eulerian.** Uniform rectangle, `nx = 400`, pulse
   injection, `Pe ε = 0.05`, axial Péclet ≥ 100. The outlet
   residence-time variance `σ²_t*` matches `2 D_L*` with
   `D_L* = ε(1/Pe + Pe/210)` within 5%.
4. **Taylor dispersion emerges in the particle model.** Same case with the
   Lagrangian solver (`profile='parabolic'`, `pe=` set, no Taylor term
   added by hand; `n = 20 000`). It reproduces the same `σ²_t*` within
   10%.
5. **Reaction-limited.** `Da_w = 0.01`. Steady outlet `c*` matches the
   Danckwerts closed-vessel solution for axial dispersion with
   first-order sink within 1%.
6. **Transport-limited.** `Da_w = 1000` on a long channel
   (`L/(h0 Pe) ≥ 0.1`, developed). Conversion matches `k_m = Sh D/(2h)`
   with Sh = 7.54 (both walls) and 4.86 (one wall) within 5%. Lagrangian
   with `P = 1` agrees within 3σ of its Monte Carlo error.
7. **Euler ↔ Lagrange on real shape.** Hex chamber with holes,
   `Pe ε = 0.05`, `Da_w = 0.1`. Outlet conversion agrees within 3σ
   Monte Carlo + 2%.
8. **Size invariance.** The same shape and the same `(ε, Pe, Da_w)`
   built at 1 mm and at 50 cm (D, k_s scaled to match) give identical
   `c*` fields within 1e-12.
9. **Refusals.** Eulerian with `Pe ε > 0.1` raises `ValueError` naming
   `regime.taylor` and the Lagrangian alternative (the adapter maps this
   to `BadInput` with `next=`). `pe=` with `profile='plug'` is refused.
   `trace` without `pe=` is unchanged: the existing `test_precis_flow.py`
   and `test_se_flow.py` pass untouched.
10. **Card.** `from_physical(..., molecular_d=, k_s=, n_walls=)` returns
    the four species lines with the worked-point verdicts above. Without
    `k_s` the reaction lines are not-applicable and do not block `ok`.
11. **se measures.** A hard `outlet-conversion ≥ 0.9` on a chamber that
    reaches 0.6 produces a DRC finding. A missing `wall-rate-constant`
    gives a reason string, never 0. All four computers are registered in
    `precis_se.properties.COMPUTERS`.
12. **catpath binding.** Takes a quest fixture with `tof`, `log_tof*` and
    a slab site area. `wall_rate` writes `k_s` equal to
    `TOF/(A_site N_A c_wall)` within 1e-12 relative, with provenance in
    meta. An untrusted solve (no `log_tof`) is refused with a reason.
    p5/p95 produce a conversion band on the measure.

## Later slices (one physics each; acceptance written when each goes `ready`)

2. **Heat.** Fluid T plus wall-plate T, Nu coupling, `μ(T)` Picard,
   reaction enthalpy as a wall source (catpath `dH`). Card: `Pe_T`,
   `t_wall/L`, `Ra_h`. Output measures: `max-temperature`,
   `outlet-temperature`, `wall-heat-flux`.
3. **Electro-osmosis.** φ solve with `hσ`; ζ from charge measures on
   patches (region-property layer); Joule heating hands to slice 2. Card:
   `λ_D/h0`, EO/pressure velocity ratio. Outputs: `eo-flow-rate`,
   `current`.
4. **Darcy regions.** A `porous:` region on the pocket with
   `permeability` and `porosity` measures; species `D_eff = D·φ/τ`. Card:
   `κ/h²`.
5. **Slip.** `slip-length` per patch (hydrophobic regions), Maxwell slip
   for gas from `λ`. The tracer profile follows the slip-shifted parabola.
   The Knudsen line threshold becomes 0.1 when slip is on.
6. **Compliant walls.** Winkler, then plate. Wall material `E, ν, t`.
   Output `max-deflection`, `gap-closure` (valve). Card: compliance
   `Δp/(K_w h0)`.
7. **Power-law fluids.** `K, n` on the fluid material; the reduced form
   depends on `n` only (K absorbed into `u0`). Card: `Wi` when a
   relaxation time is given.
8. **Two-phase in the gap.** Level set, Park–Homsy jump, θ per patch,
   EWOD as a θ(V) patch measure. Card: `Ca`, `D_drop/h`, `Bo`. Outputs:
   `drop-arrival-time`, `pinch-off`.

B. **External 3-D backend.** It can start any time after slice 1; its
   first use is calibrating the card thresholds.
   - **Interface** (`precis_flow.backend`): a `Backend` protocol with
     `capabilities() -> set[str]` (physics names as above) and
     `prepare(case) -> JobSpec`, and `read(job) -> Results` with fields
     sampled onto named patches and regions. A `Case` holds geometry
     (STEP/STL from the cad envelope via `precis_se.ops_export`), the BC
     map by patch name (the slice-1 measures), the material table, the
     physics list and mesh hints. se builds the `Case`; nothing in se
     names a backend.
   - **Runtime.** Each backend runs as an external process in its own
     container on the compute lane, as a precis job. GPL tools (Elmer,
     OpenFOAM) are never imported into precis. Results come back as
     measure values through the same computers, tagged with the backend
     and mesh size.
   - **Order.** B1 FEniCSx: dolfinx Stokes / Navier–Stokes +
     advection–diffusion + PNP, gmsh meshing, Python-native. B2 Elmer:
     heat, electrostatics, FSI. B3 OpenFOAM: `interFoam`,
     `chtMultiRegionFoam`.
   - **Calibration test (B1).** On the slice-2 rectangle at
     `h0/L ∈ {0.01, 0.05, 0.1, 0.2}`, measure the kernel `R*` against
     3-D Stokes. The `thinness` threshold stays at 0.05 only if the error
     there is ≤ 5%; the measured curve replaces the guess.

Order: 1 → 5 (slip: small, unlocks gas Kn up to 0.1) → 2 → 3 → 4 → 6 →
7 → 8. B is unordered. Heat before electro-osmosis because Joule heating
needs it.

## Explicitly NOT in scope

Turbulence; 3-D solves inside precis (B delegates them); the nano
propagator (`se-flow-kernel.md` slice 3); bimolecular / nonlinear wall
kinetics (`kinetics-engine-v2.md`); liquid-phase catpath rates (no
solvation model; Q3); a web UI for the new fields beyond the slice-2b
payload keys; homogeneous (bulk) reactions. The last is a later
one-liner on the same `fv` source term, filed when a case needs it.

## Target + blast radius

`src/precis_flow/` gains `fv.py`, `species.py` and the species lines in
`regime.py`, and `trace` gains `pe=`/`walls=`. `src/precis_se/flow.py`
gains the token keys, the computers and the `wall_rate` handler op.
Measurand taxon nodes for the new slugs need a core seed migration, which
goes through the release owner. `precis-se-regions-help` and the flow
skill gain the BC vocabulary.

## Open questions (default taken if unanswered)

- Q1 BCs as measures on patch regions (default) vs. more keys in the
  `flow:` token. Measures reuse value/band/strength/origin and DRC; the
  token stays a role list.
- Q2 Species scope via the measure name `<block>.<species>.<property>`
  (default) vs. a new `species` column on `se_measures` (migration).
- Q3 Liquid-phase catpath rates: refuse (default) until a solvation /
  Henry model is stated.
- Q4 Backend B1 is FEniCSx (default). Run on the compute lane as a job,
  never in the MCP process.
