---
status: ready
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

**Landed (slice 1), 2026-10-23 on main as `a12959f47`** — fv kernel + species
transport, tracer `pe=`/`walls=`, species card lines, the se surface
(`fluid=`/`species=`, BC patch measures, four COMPUTERS), `wall_rate`,
migration 0231 (via builder-ce), `precis-se-flow-help`, and the 12
acceptance tests.

**Landed (slice 1b), 2026-10-24** — the Lévêque entrance correction on
the Eulerian wall coefficient (`_entry_distance` + `wall_sink_star`
`entrance=`), the slice-1 `wall-flux-fraction` flag removed, and a kernel
acceptance test. See slice 1b below.

**Landed (slice 4 core), 2026-11-06** — `src/precis_flow/darcy.py`
Darcy/Brinkman transmissivity kernel plus `regime.py` hooks; the
rest of slice 4 (see its section) remains open.

**Landed (slice 4 D6 kernel), 2026-11-13** — `darcy.PorousRegion` /
`porous_dispersion` and `species.taylor_tensor`/`transport` `porous=`: the
porous tensor `ε(φ/τ)/Pe·I + (α_L/L)|U*|ûûᵀ` replaces the Taylor term in the
region, and `transport` refuses a pulse or an overlapping reactive wall (M15).
Still open for slice 4: the se surface (migration from COORDINATOR, `darcy`
registration in `PHYSICS`/`TESTED`, the D6 reason strings and D7).

**Ready.** Slice 1 on 2026-10-21; slices 2, 4 and 5 on 2026-11-04
(§Acceptance: slip, heat, Darcy), using the registrations in
`src/precis_flow/physics.py::PHYSICS`. The other slices
stay draft; each gets its acceptance when it goes ready. A physics review (Fable,
2026-10-21) found the core relations correct: the Taylor coefficient with
the full gap and 1/210, the reduced numbers, the series `k_eff`, the
Erban–Chapman wall absorption, the Da definitions and the TOF→`k_s` units.
It fixed these slice-1 items before ready:
- the catpath binding records the apparent reaction order and limits the
  first-order `k_s` to conversion ≲ 0.2 when the order is far from 1; it
  refuses TOF ≤ 0 and states the Å² and bar conversions;
- the Taylor tensor is built from the shear-carrying velocity, and
  `species.transport` takes it as an argument;
- the inlet is a Danckwerts flux condition, and advection is second order;
- tests 3, 5 and 6 are rewritten so they can fail: the old test 3 grid had
  numerical diffusion larger than the physical value, the old test 5 could
  not tell dispersion from plug flow, and the old test 6 had conversion
  `1 − e^{−75}`;
- the `wall_damkohler` thresholds are on `k_s/k_m = 2 Da_II/Sh`;
- the tracer step cap is an argument, and wall absorption uses the local
  total vertical diffusivity.
Later-slice corrections from the same review are in their slices (1b, 1c,
2, 3, 4, 5, 6, 7, 8).

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
   measurand slug on the patch (spec `me629029` slice A,
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
   measure is a workspace script's objective term (`se-flow-kernel.md`
   slice 4: the toolkit `evaluate` returns physics outcomes only), and a
   `gauge` measure only reports.

The backend can change (kernel, FEniCSx, OpenFOAM) without any change to
se: the `Case` (slice B) is built from the same three inputs.

Notation: `ε = h0/L` is the thinness throughout. Permittivity is written
`ε_r ε₀`.

### What the thin-gap form carries

| Physics | Across-gap closure | 2-D equation (transmissivity / source) | Breaks when (card line) | Slice |
|---|---|---|---|---|
| species + wall reaction | Taylor–Aris: transverse mixing faster than transit | `∇·(h𝔻∇c) − ∇·(hŪc) − Σ_w k_eff c = ∂(hc)/∂t`, `𝔻 = D I + h² Ū_s Ū_sᵀ/(210 D)` with `Ū_s` the Poiseuille (shear-carrying) part of `Ū` | `Pe·h0/L > 0.1` (dispersion not developed): Lagrangian solver, same slice | 1 |
| heat | same as species for the fluid; walls as 2-D conducting plates coupled through `Nu = 8.23 / 5.39` (both / one wall, uniform flux) | `T_fluid` and `T_wall` fields; `μ(T)` feeds back into `h³/μ` (Picard) | thick walls with 3-D conduction (`t_wall/L > 0.05`, or wall `Bi > 0.1`); buoyant convection across the gap (`Ra_h > 1708`) | 2 |
| electro-osmosis | thin EDL: plug slip `u_eo = (ε_r ε₀ ζ/μ)∇φ` | `∇·(hσ∇φ) = 0`, then `∇·(h³∇p/12μ) = ∇·(h u_eo)`; ζ per patch | EDL overlap `λ_D/h0 > 0.01` (needs Poisson–Nernst–Planck: nano / backend) | 3 |
| Darcy / Brinkman regions | porous insert fills the gap | face transmissivity `T* = 12(κh/h0³)[1 − (2√κ/h) tanh(h/(2√κ))]` inside the region (Brinkman closed form; Darcy `12κh/h0³` when `κ/h² ≤ 6e-4`, Hele-Shaw `h*³` as `κ → ∞`) | the insert covers only part of the gap height | 4 |
| wall slip | Navier slip `b₁, b₂` | flux factor `(h² + 4h(b₁+b₂) + 12 b₁b₂) / (h(h+b₁+b₂))` on `h³/12μ`; gas: `b ≈ λ` (Maxwell, full accommodation) | `Kn > 0.1` (transition regime): nano propagator; slip moves the Knudsen line from 0.01 to 0.1 | 5 |
| compliant walls | local gap `h = h_rest + w(p)` | `w = p/K_w` with `K_w` chosen by `t/W` (slice 6), or the plate `D∇⁴w = p`; nonlinear Reynolds equation (soft-lithography EHL), Newton | deflection `w/h0 > 1` with contact (gap closes: model as `h_min`), `w/t_wall > 0.5` (plate theory), or `w/L > 0.05` | 6 |
| power-law fluid | `τ = K γ̇ⁿ` across the gap | `q = −(2n/(2n+1)) (h/2)^{(2n+1)/n} (|∇p|/K)^{1/n} ∇p/|∇p|`: p-Laplacian, Picard on face T | yield stress with unyielded plugs (Bingham), or Weissenberg `Wi > 0.1` (viscoelastic) | 7 |
| two-phase in the gap | pancake interface spanning the gap; Park–Homsy jump, one wetting convention per case (slice 8) | level set on the 2-D grid, pressure jump at the interface, contact angle θ per patch (hydrophobic region measures) | drop diameter `< 2h` (3-D drop), `Ca > 0.1` (film thickness `~h Ca^{2/3}` no longer small), contact-line pinning dynamics, unstable displacement (Saffman–Taylor) | 8 |

Couplings carried: flow → species, flow → heat (one-way); T → μ → flow,
reaction heat → T → k_s (Picard, two-way; `k_s(T)` open, M6); φ → flow (one-way); p ↔ h
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

### Solids (native, not the backend)

Conduction, DC current, electrostatics and species diffusion *in
solids and bulk*, Joule heating of a conductor, and thermal expansion /
stress are [se-native-fields](se-native-fields.md): the 3-D field
kernel `precis_fields` on a cut-cell grid from the cad solid, with the
same patch-measure BCs, material rows and `COMPUTERS` outputs as here.
Slice 2's heat keeps the fluid and the wall *plates*; a thick wall
(`t_wall/L > 0.05`, wall `Bi > 0.1`) hands the wall to that kernel, not
to the backend.

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
`Da_II = k_s h0/D` per reactive wall (written `Da_w` below), transit
Damköhler `Da_I = n_w Da_w/(Pe ε) = n_w k_s L/(h0 u0)`. `c* = c/c_in`.

**Eulerian, depth-averaged** (`precis_flow.species.transport`):

```
∂(h* c*)/∂t* + ∇*·(h* U* c*) = ∇*·(h* 𝔻* ∇* c*) − (L/(h0 u0)) Σ_w k_eff,w c*
𝔻* = ε [ I/Pe + (Pe/210) h*² U_s* U_s*ᵀ ]
1/k_eff,w = 1/k_s,w + 2h/(Sh_w D),   Sh = 7.54 (both walls reactive), 4.86 (one wall)
```

- **Shear velocity.** `U_s*` is the shear-carrying (Poiseuille) part of
  the depth-averaged velocity. A uniform plug adds nothing to Taylor
  dispersion, so the tensor is not built from the total `U*`.
  `species.transport` takes `shear_velocity=` (default: the pressure-driven
  `U*` of the `Field`, which is all of `U*` in slice 1) and
  `profile='poiseuille'`. Slices 3, 5 and 7 pass their own shear part and
  profile. It refuses an unknown profile.
- **Series form always.** The solver always uses `k_eff` from the series
  form above, in every regime. The `wall_damkohler` verdict only names
  which resistance dominates.
- **Reactive walls shift Taylor dispersion.** With wall loss, the
  effective axial diffusivity and the centroid speed both change at
  O(`Da_w`) (Sankarasubramanian & Gill 1973; Lungu & Moffatt 1982 for the
  wall-conductance analogue). The Eulerian equation above does not carry
  that shift. This is why the Lagrangian is the reference in the mixed
  regime.
- **Inlet.** The inlet is the Danckwerts flux condition
  `Ū c_in = Ū c − D_L ∂c/∂n` (a Robin row in `fv`). A Dirichlet inlet
  differs from it by ~9% in outlet `c*` at axial Péclet 20. Outlet cells
  are zero-gradient.
- **Scheme.** Advection is second order (central faces). A TVD limiter is
  switched on only where the cell Péclet number `|U*|Δx*/D_L*` exceeds 2,
  and the result records the maximum cell Péclet number and whether the
  limiter was active. Transient runs use Crank–Nicolson.
- **Steady or transient.** It is valid when `Pe ε ≤ 0.1`, i.e. a
  transverse mixing time `h0²/D` of at most a tenth of the transit time
  `L/u0`.

**Lagrangian** (`trace` extended). It is valid for any `Pe ε`, and is the
reference where the depth average fails:
- `pe=` turns on molecular diffusion, horizontal
  `sqrt(2ε/Pe · dt*)·ξ` and vertical in gap-fraction units with
  `D_ψ = 1/(ε Pe h*²)`. It requires `profile='parabolic'`; plug plus
  molecular diffusion is not Taylor dispersion, so that combination is
  refused;
- `walls=` makes the floor and lid reactive. A wall hit is absorbed with
  probability `P = Da_w·sqrt(π dt*/(ε Pe · D_tot/D))` (Erban & Chapman
  2007). `D_tot` is the local total vertical diffusivity at the hit: `D`
  plus the `D_v*` pocket dispersion where the floor is deepened. `dt*` is
  capped so that `P ≤ 0.1`. `Da_w = ∞` means a perfectly absorbing wall,
  `P = 1`;
- the existing `D_v*` dispersion over pockets stays on as the modelled
  unresolved 3-D mixing. Molecular diffusion is added to it and does not
  replace it;
- `max_steps=` becomes an argument of `trace` (default the current
  `MAX_STEPS = 4000`). The vertical step needs `~100/(Pe ε)` steps per
  transit, 2000 at `Pe ε = 0.05`, so a pulse tail does not fit under the
  current cap. A run that stops on the cap says so in `Paths`.

The two solvers cost the most in opposite regimes. Eulerian cost does not
depend on `Pe ε`. The Lagrangian vertical step needs
`~100/(Pe ε)` steps per transit. The card's `taylor` line routes each
case to the cheaper valid solver.

**Products.** Slice 1 handles independent species and one-step chains
`A → B`: B's wall source equals A's wall sink, solved after A. The
equations stay linear. Concentration-dependent single-species rates
`r(c)` are slice 1c. Bimolecular wall kinetics (`r(c_A, c_B)`) waits on
`kinetics-engine-v2.md`.

### Rates from kinetics / catpath

The wall rate per area is `r = TOF / (A_site N_A)` in mol m⁻² s⁻¹, with
`A_site = site_area_A2 · 1e-20` m². `TOF` is the quest scalar `tof`
(`src/precis/quest/compute.py`) of a **trusted** solve; the trust gate is
the presence of `log_tof`. A `tof ≤ 0` (net reverse or a failed solve) is
refused with a reason. `A_site` is catpath's `kinetics.site_area_A2`
(Å², one top-layer atom = 1 site).

The first-order constant `k_s = r / c_wall` is a secant taken at the
solve's stated reactant partial pressure `p`. catpath takes pressures in
bar; the binding converts to Pa (`× 1e5`) and uses `c_wall = p/(RT)` (gas
phase). A stated roughness factor multiplies `k_s` (default 1, named on
the card).

Langmuir–Hinshelwood rates are not first order, so the binding measures
the apparent order. It runs two more catpath solves at `p(1 ± δ)`
(`δ = 0.1`) and records
`α = ln(TOF₊/TOF₋) / ln((1+δ)/(1−δ))`. The card's `apparent_order` line
holds when `|α − 1| ≤ 0.2`. Otherwise the first-order `k_s` is valid only
while the conversion stays `X ≲ 0.2`, and the line says so. Past that,
the case needs slice 1c.

`log_tof_p5` / `log_tof_p95` give a conversion band: two more solves at
those values, stored with the measure. The binding is a handler-level op
`wall_rate`
(`{"op":"wall_rate","block":…,"datum":"patch:…","source":"quest:<id>","species":…}`).
It reads the store, so it cannot be pure, which is the same reason
`layout_chain` is handler-level. The op writes a `wall-rate-constant`
measure with `origin='proposed'`. The measure meta records quest id, T,
p (Pa), roughness, trust, `α` and `δ`. A rate typed in directly is the
same measure with `origin='user'`; it has no `α`, so its
`apparent_order` line is not-applicable.

### Card lines (species group)

`k_m = Sh D/(2h)` is the developed mass-transfer coefficient, so
`k_s/k_m = 2 Da_w/Sh`.

| Line | Number | Verdict |
|---|---|---|
| `peclet` (exists) | `u0 h0/D` | ≥ 10 for the particle-tracer reading; the species solvers accept any value |
| `taylor` | `Pe·ε` | ≤ 0.1 → Eulerian; else route to Lagrangian (not a failure when Lagrangian was asked) |
| `wall_damkohler` | `k_s/k_m = 2 Da_II/Sh` | ≤ 0.1 reaction-limited (`Da_II ≤ 0.38` both walls, `≤ 0.24` one wall) · 0.1–10 mixed (Lagrangian is the reference) · ≥ 10 transport-limited (`Da_II ≥ 38` / `≥ 24`). Informational: the solver uses the series `k_eff` in every case |
| `damkohler` | `Da_I = n_w k_s L/(h0 u0)` | informational: expected conversion `~1 − e^{−Da_I}` |
| `apparent_order` | `α` from the binding | `|α − 1| ≤ 0.2`; else "first-order `k_s` valid only while X ≲ 0.2"; not-applicable without `α` |

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
  Species scope uses the measure name `<block>.<species>/<property>`
  (Q2).
- Output computers in `FLOW_COMPUTERS`: `outlet-conversion` (ratio),
  `outlet-concentration` (`c*`), `wall-flux-fraction` (fraction of the
  inlet flux consumed on the measure's patch; per-catalyst-patch
  selectivity), `damkohler` (gauge).
- `wall-flux-fraction` is entrance-corrected (slice 1b, landed): the
  Eulerian solve raises `k_m` to Lévêque's leading-edge value in the
  developing boundary layer of each reactive region, so short patches are
  no longer under-read and the slice-1 reason flag is gone.
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
3. **Taylor dispersion, Eulerian.** Uniform rectangle at the worked point
   `ε = 0.005`, `Pe = 10` (`Pe ε = 0.05`), pulse injection, no reaction.
   `D_L* = ε(1/Pe + Pe/210) = 7.4e-4` (axial Péclet ≈ 1360); the Taylor
   part is a third of it, so leaving it out fails the test. The grid has
   cell Péclet `Δx*/D_L* ≤ 1` (`nx ≥ 1400`) with the limiter off. The
   outlet residence-time variance `σ²_t*`, Richardson-extrapolated from
   `nx` and `2nx`, matches `2 D_L*` within 5%. At axial Péclet ≥ 500 the
   inlet/outlet correction to the variance is below 1% and is ignored.
4. **Taylor dispersion emerges in the particle model.** Same case with the
   Lagrangian solver (`profile='parabolic'`, `pe=` set, no Taylor term
   added by hand; `n = 20 000`, `max_steps` raised above the pulse tail).
   It reproduces the same `σ²_t*` within 10%.
5. **Reaction-limited with axial dispersion.** Uniform rectangle,
   `ε = 0.05`, `Pe = 1` (`Pe ε = 0.05`, axial Péclet ≈ 20), both walls
   reactive with `Da_w = 0.05` (`k_s/k_m = 0.013`, `Da_I ≈ 2`). Steady
   outlet `c*` matches the Danckwerts closed-vessel solution for axial
   dispersion with a first-order sink (rate from `k_eff`) within 1%. At
   this point the plug-flow `e^{−Da_I}` is 15% below the Danckwerts value
   and a Dirichlet inlet is 9% above it, so neither passes.
6. **Transport-limited.** `Da_w = 1000`, `Pe ε = 0.1`, both walls and one
   wall.
   - Eulerian: the decay slope of `ln c*` near `c* = 0.5` matches
     `−n_w Sh/(2 ε Pe)` with Sh = 7.54 (both walls) and 4.86 (one wall)
     within 5%. The `1/e` decay length spans ≥ 10 cells (`nx ≥ 1000`).
   - Lagrangian (`P = 1`): the decay slope of the surviving fraction,
     fitted downstream of the entrance length (`x* ≥ 0.2 ε Pe`) where the
     profile is developed, matches the same slope within 5% plus 3σ of its
     Monte Carlo error. Sh is not put into the particle model, so this
     test checks that it emerges.
7. **Euler ↔ Lagrange on real shape.** Hex chamber with holes,
   `Pe ε = 0.05`, `Da_w = 0.1`. Outlet conversion agrees within 3σ
   Monte Carlo + 2%.
8. **Size invariance.** The same shape and the same `(ε, Pe, Da_w)`
   built at 1 mm and at 50 cm (D, k_s scaled to match) give identical
   `c*` fields within 1e-12.
9. **Refusals.** Eulerian with `Pe ε > 0.1` raises `ValueError` naming
   `regime.taylor` and the Lagrangian alternative (the adapter maps this
   to `BadInput` with `next=`). `pe=` with `profile='plug'` is refused.
   An unknown `species.transport` profile is refused. `trace` without
   `pe=` is unchanged: the existing `test_precis_flow.py` and
   `test_se_flow.py` pass untouched.
10. **Card.** `from_physical(..., molecular_d=, k_s=, n_walls=,
    apparent_order=)` returns the five species lines with the worked-point
    verdicts above. `wall_damkohler` at `Da_II = 0.3` (both walls) is
    reaction-limited and at `Da_II = 0.5` is mixed. Without `k_s` the
    reaction lines are not-applicable and do not block `ok`; without
    `apparent_order` that line is not-applicable.
11. **se measures.** A hard `outlet-conversion ≥ 0.9` on a chamber that
    reaches 0.6 produces a DRC finding. A missing `wall-rate-constant`
    gives a reason string, never 0. All four computers are registered in
    `precis_se.properties.COMPUTERS`. A short reactive patch
    (`< 0.2 Pe h`, `k_s/k_m ≥ 0.1`) answers `wall-flux-fraction` with a
    plain value and no reason (entrance-corrected since slice 1b).
12. **catpath binding.** Takes a quest fixture with `tof`, `log_tof*` and
    a slab site area. `wall_rate` writes `k_s` equal to
    `TOF/(site_area_A2 · 1e-20 · N_A · c_wall)` with
    `c_wall = p_bar · 1e5/(RT)`, within 1e-12 relative, with provenance in
    meta. An untrusted solve (no `log_tof`) is refused with a reason, and
    so is `tof ≤ 0`. A fixture with TOF ∝ `p^0.5` records `α = 0.5`
    within 1e-6 and its `apparent_order` line fails with the X ≲ 0.2 text.
    p5/p95 produce a conversion band on the measure.

## Later slices (one physics each)

1b. **Entrance-region mass transfer — Landed.** Developed Sh ignored the
   concentration boundary layer that restarts at the leading edge of each
   reactive region. The Eulerian `wall_sink_star` now uses
   `k_m(s) = max(Lévêque(s), Sh D/(2h))` with Lévêque's
   `k_m(s) = 0.978 (Ū D²/(h s))^{1/3}` and `s` from the advected
   distance-since-region-entry scalar `precis_flow.species._entry_distance`
   (`F·∇s* = h*|U*|`, `s* = 0` where flow enters the region, a first-order
   upwind M-matrix). The ratio exceeds 1 only inside the entrance zone and
   falls to the developed value past it, so long walls and
   reaction-limited cases are unchanged (tests 2/5/6/7 untouched). The
   slice-1 `wall-flux-fraction` under-read flag is removed. `transport`
   takes `entrance=` (default on).
1c. **Nonlinear single-species wall kinetics.** The wall BC becomes
   `r(c_wall)` from a tabulated catpath p-sweep stored on the measure, and
   `fv` Picard-iterates the Robin coefficient `r(c_w)/c_w`, with
   `c_w` from `r(c_w) = k_m(c̄ − c_w)` per wall cell. This was not put
   into slice 1, although the outer loop is cheap. The rest is not: the
   binding needs an N-point sweep and an extrapolation rule outside the
   table, the per-cell local solve and convergence reporting are new, and
   the Lagrangian reference cannot carry a concentration-dependent
   absorption probability, so tests 4, 6 and 7 have no counterpart.
   Reference for its acceptance: zero-order and Michaelis–Menten plug
   flow (closed forms).
2. **Heat.** Fluid T plus wall-plate T, Nu coupling, `μ(T)` Picard,
   reaction enthalpy as a wall source (catpath `dH`; M7). Acceptance:
   §Heat.
   - Card: `Pe_T`, `t_wall/L`, wall Biot `Bi = h_c t_wall/k_wall ≤ 0.1`
     (the 2-D plate assumes no temperature gradient across the wall
     thickness), `Ra_h`.
   - Taylor dispersion of heat is smaller with conducting walls than the
     insulated-wall 1/210 value. The heat tensor uses the conjugate
     coefficient, or the card names the bound it uses.
   - Viscous (Brinkman) heating is negligible at these speeds (`Br =
     μŪ²/(kΔT) ~ 1e-9` for water at 1 mm/s and 1 K) and is left out.
   - Output measures: `max-temperature`, `outlet-temperature`,
     `wall-heat-flux`.
3. **Electro-osmosis.** φ solve with `hσ`; ζ from charge measures on
   patches (region-property layer); Joule heating hands to slice 2.
   - Pressure solve: `∇·(h³∇p/12μ) = ∇·(h u_eo)` (net flux
     `q = −(h³/12μ)∇p + h u_eo` is divergence-free).
   - Species: the EO plug carries no Taylor dispersion. In mixed flow,
     `shear_velocity` is the Poiseuille part `Ū_p` only. A floor/lid ζ
     difference adds a Couette profile with wall-velocity difference
     `Δu`, which adds `h² Δu Δuᵀ/(120 D)` to `𝔻` (no cross term with the
     Poiseuille part); or the slice requires `ζ_floor = ζ_lid` and refuses
     otherwise.
   - Card: `λ_D/h0`, EO/pressure velocity ratio, Dukhin number
     `Du = σ_s/(σ h)` (surface conduction against bulk).
   - Outputs: `eo-flow-rate`, `current`.
4. **Darcy / Brinkman regions.** A floor or lid `patch:` datum with
   `permeability` and `porosity` measures (Q1; no new selector). Acceptance:
   §Darcy / Brinkman.
   - Transmissivity is the Brinkman closed form in the table, so no
     `κ/h²` threshold is needed for the flow. The old Darcy threshold
     `κ/h² > 0.1` was wrong: at 0.1 Darcy reads 2.39× the Brinkman flux,
     and the two agree within 5% only for `κ/h² ≤ 6e-4`.
   - Species: `D_eff = D·φ/τ`. The Taylor term is off inside the region
     (the profile there is near plug). Mechanical dispersion `α_L|U|` is
     added for coarse media, with `τ` and `α_L` region measures.
   - Card: `κ/h²` (informational: which limit the closed form is near).
5. **Slip.** `slip-length` per patch (hydrophobic regions), Maxwell slip
   for gas from `λ`. Acceptance: §Slip. The tracer profile follows the
   slip-shifted parabola.
   The Knudsen line threshold becomes 0.1 when slip is on. With equal slip
   `b` on both walls, the slip part is a plug, so the Taylor coefficient
   falls by `(1 + 6b/h)⁻²` at the same mean velocity; unequal slip passes
   the asymmetric profile through `shear_velocity`/`profile`.
   **Landed (kernel core), 2026-10-26** — the Navier flux factor
   `(h² + 4h(b₁+b₂) + 12 b₁b₂)/(h(h+b₁+b₂))` on the reduced pressure
   transmissivity (`Domain.slip_floor_star`/`slip_lid_star` →
   `Domain.slip_factor`, scaling `h*³` and `U*` in `flow.solve`), and the
   slip-aware regime card (`from_physical(slip_length=)` raises the
   knudsen continuum bound 0.01→0.1 via `KN_SLIP_MAX`; `route()` reads the
   knudsen line's own threshold; an informational `slip` line reports
   `b/h0`). No-slip walls are bit-for-bit the slice-1 solve. The species-side
   profile and Taylor coefficient live in
   `src/precis_flow/slip.py::gap_profile` and `taylor_coefficient`;
   `SlipProfile` supplies the tracer hook and the Eulerian solver uses
   `shear_velocity = U* sqrt(210 kappa)`. Kernel tests cover S1–S5.
   **Still open:**
   the se surface — a `slip-length` patch measurand (needs the core seed
   migration, release owner) and the token/computer wiring in
   `precis_se/flow.py`, including the adapter acceptance cases.
6. **Compliant walls.** Wall material `E, ν, t`; the closure is named by
   `t/W` (wall thickness over channel width):
   - thick PDMS (`t ≫ W`): Winkler `K_w ≈ E/(c₁ W)`, `c₁` a stated
     geometry constant;
   - thin layer bonded to a rigid backing (`t ≪ W`):
     `K_w = E(1−ν)/((1+ν)(1−2ν) t)` (confined modulus; very stiff as
     `ν → 0.5`);
   - free plate: `D∇⁴w = p`, `D = E t³/(12(1−ν²))`.
   Card: compliance `Δp/(K_w h0)`, and a breakdown line at
   `w/t_wall > 0.5` (small-deflection plate theory). Outputs
   `max-deflection`, `gap-closure` (valve).
7. **Power-law fluids.** `K, n` on the fluid material; the reduced form
   depends on `n` only (K absorbed into `u0`).
   - The p-Laplacian face coefficient is regularised,
     `(|∇p|² + δ²)^{(1−n)/(2n)}`, with `δ` stated.
   - Reynolds line uses the Metzner–Reed Reynolds number for `n ≠ 1`.
   - Species: the gap profile is
     `u/Ū = ((2n+1)/(n+1))(1 − |1 − 2ψ|^{(n+1)/n})`, which changes the
     Taylor coefficient from 1/210. It goes through `profile=`, and the
     tracer uses the same profile.
   - Card: `Wi` when a relaxation time is given.
8. **Two-phase in the gap.** Level set, pressure jump at the interface,
   θ per patch, EWOD as a θ(V) patch measure. The Park–Homsy jump is used
   in one convention per case, never mixed:
   - pre-wetted (θ = 0, thin film):
     `Δp = (2γ/h)(1 + 3.80 Ca^{2/3}) + (π/4)γκ_∥`, valid for
     `Ca ≤ 0.01`;
   - partial wetting: `Δp = 2γ cos θ_d/h + γκ_∥`, with the dynamic angle
     `θ_d` from Cox–Voinov.
   Card: `Ca`, `D_drop/h`, `Bo`, and a Saffman–Taylor line (displacing
   fluid less viscous than the displaced one: fingering, results
   grid-dependent below the finger width). Outputs: `drop-arrival-time`,
   `pinch-off`.

B. **External 3-D backend — validation and 3-D flow.** Its remit
   shrank 2026-10-22 ([se-native-fields](se-native-fields.md)): 3-D
   Navier–Stokes, turbulence, two-phase with 3-D interfaces, and
   **cross-checking both native kernels** (the thin-gap kernel here,
   `precis_fields` there — its §Verification names the Elmer and
   FEniCSx cases). It can start any time after slice 1; its first use
   is calibrating the card thresholds.
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
     advection–diffusion + PNP, gmsh meshing, Python-native; also the
     thermoelastic cross-check. B2 Elmer: **cross-check only** (heat,
     electrostatics, elasticity against `precis_fields`; FSI stays
     named). B3 OpenFOAM: `interFoam`, `chtMultiRegionFoam`.
   - **Calibration test (B1).** On the slice-2 rectangle at
     `h0/L ∈ {0.01, 0.05, 0.1, 0.2}`, measure the kernel `R*` against
     3-D Stokes. The `thinness` threshold stays at 0.05 only if the error
     there is ≤ 5%; the measured curve replaces the guess.

Order: 1 ✓ → 1b ✓ → seam → 5, 2 and 4 in parallel, each with its own
measurand migration. Then 3 (Joule heating refused until heat lands), 7
after slip (shared gap profile), 6 and 8 later. 1c when a case has
`|α − 1| > 0.2` at conversion above 0.2. B is unordered.

## Combination rule

A lane refuses every physics combination it has not tested.

- **Combination.** The set of active physics on one chamber. A physics is
  active when any of its inputs is declared (a registered token key,
  `evaluate` spec key or input measure), whatever output was requested:
  `outlet-conversion` on a chamber with a `slip-length` measure is a slip +
  species case.
- **Tested.** The set is in `src/precis_flow/physics.py::TESTED`. An entry lands in
  the commit that adds its test. `{flow}` and `{flow, species}` seed it.
- **Refusal.** `evaluate` and `view='flow'` raise `BadInput` naming
  `physics.untested_combination`, the set, and `next=` (which input to
  drop). A computer returns `(None, reason)` with the same text.
- **No silent drop.** Ignoring a declared input is the failure this rule
  stops. A physics is never left out of a solve to make a number appear.
- **Test.** A seam test walks every subset of the registered physics and
  asserts that each one outside `TESTED` refuses, so a new lane is covered
  when it registers.

| Active set (with `flow`) | Owner | State after wave |
|---|---|---|
| none | slice 2 | tested |
| species | slice 1 | tested |
| slip | slip lane | tested by S1–S8 |
| heat | heat lane | tested by H1–H9 |
| darcy | Darcy lane | tested by D1–D5, D7 |
| slip + species | slip lane | tested by S9 |
| heat + species | heat lane | tested by H10 |
| darcy + species | Darcy lane | tested by D6 |
| slip + heat, slip + darcy, heat + darcy, or three or more | none | refused; a follow-on lane writes its acceptance |
| `flow:lumen` with any of these | none | refused |

## Acceptance: slip, heat, Darcy

Slices 5, 2 and 4 build in parallel after the seam. Tests:
`tests/test_precis_flow_<slip|heat|darcy>.py` (kernel) and
`tests/test_se_flow_<slip|heat|darcy>.py` (adapter). All three use one
face law: `U* = −(T*/h*) ∇*p*` with face transmissivity `T*`, which gives
the landed `U* = −h*²∇*p*` at `T* = h*³`.

### Common to the three

- **C1 Off is off.** With the physics inactive, `solve`, `trace`,
  `transport` and `evaluate` results are bitwise unchanged, and the
  existing flow and species test files pass untouched.
- **C2 Size invariance.** The same shape and reduced numbers at 1 mm and
  50 cm give equal reduced fields within 1e-12.
- **C3 Determinism.** `evaluate` twice gives an equal `Outcome`.
- **C4 No silent numbers.** A missing input gives a reason string, never 0
  or a default.
- **C5 Card.** The physics' card group is absent without its inputs,
  its lines are not-applicable until their own inputs are given, and
  `card_dict` serialises them.
- **C6 Combinations.** Sets the §Combination rule table does not mark
  tested refuse.
- **C7 Migration.** One measurand seed migration per lane, in the shape of
  `src/precis/migrations/0231_se_species_measurands.sql`. **The lane gets
  its number from COORDINATOR and does not pick one.** That shape skips a
  slug already present, so the later se-native-fields seed cannot clash.

  | Lane | Measurands (canonical unit) |
  |---|---|
  | slip | `Slip length` (m) |
  | heat | `Temperature` (K), `Heat flux` (W/m²), `Reaction enthalpy` (J/mol), `Max temperature` (K), `Outlet temperature` (K), `Wall heat flux` (W/m²) |
  | Darcy | `Permeability` (m²), `Porosity` (ratio), `Tortuosity` (ratio), `Longitudinal dispersivity` (m) |

- **C8 Docs.** `precis-se-flow-help` and `precis-se-regions-help` gain the
  lane's vocabulary.

### Slip (slice 5)

Local ratios `β₁ = b_floor/h`, `β₂ = b_lid/h`. `T* = h*³ F` with
`F = (1 + 4(β₁+β₂) + 12β₁β₂) / (1 + β₁ + β₂)`, the table's flux factor.

- **S1 Flux factor.** Uniform rectangle: `R* = (nx−1)/(ny F)` within 1e-10
  relative, at `(β₁, β₂) = (0.1, 0.1)` with `F = 1.6`, `(0.1, 0)` with
  `F = 14/11`, and `(0.1, 0.3)` with `F = 74/35`.
- **S2 Series.** Equal slip on the downstream half only, `nx` even:
  `R* = ((nx−1)/2)(1 + 1/F)/ny` within 1e-10 relative. The harmonic face at
  the step makes this exact.
- **S3 Gap profile.** `w(ψ) = 6(−ψ² + aψ + aβ₁)/F` with
  `a = (1+2β₂)/(1+β₁+β₂)`. `∫₀¹ w dψ = 1` within 1e-12, and `β₁ = β₂ = 0`
  gives `6ψ(1−ψ)`. `trace` uses `w`; slip with `profile='plug'` refuses.
- **S4 Taylor coefficient.** `κ = ∫₀¹ (∫₀^ψ (w−1) dψ′)² dψ` within 1e-9 of
  each reference: `1/210` at no slip, `1/(210(1+6β)²)` at equal slip,
  `210κ = 46/49` at `(0.1, 0)` and `2.68` at `(1, 0)`. As `β₁ → ∞`, `κ`
  tends to `2/105` (within 1e-7 at `β₁ = 1e6`). `transport` takes it
  through the existing `shear_velocity=` as `U*·sqrt(210κ)`, with no
  signature change.
- **S5 Taylor emerges.** Slice-1 tests 3 and 4 repeated at `ε = 0.005`,
  `Pe = 10`, mean speed held. Equal `β = 0.1`: Eulerian `σ²_t*` matches
  `2ε(1/Pe + (Pe/210)(1+6β)⁻²)` within 5%, Lagrangian within 10% with no
  Taylor term added by hand. One wall `β₁ = 1`: the Lagrangian matches
  `2ε(1/Pe + 2.68 Pe/210)` within 10%.
- **S6 Knudsen.** With slip on, the `knudsen` line threshold is 0.1 and
  `RegimeCard.route` answers `continuum` at `Kn = 0.05`; without slip the
  same card raises the transition refusal. With slip, `Kn = 0.2` raises it
  naming 0.1, and `Kn ≥ 10` still routes `knudsen`.
  `src/precis_geom/physics.py::_card` follows the same bound. Card group
  `slip`: `slip_ratio = max(b)/h0`, informational, giving `F`.
- **S7 Maxwell.** `b = λ` on both walls at full accommodation gives
  `F = 1 + 6 Kn` locally: `R*` at `Kn = 0.05` matches S1 at `F = 1.3`. A
  stated `mean_free_path` alone does not turn slip on.
- **S8 se.** `slip-length` on a floor or lid patch (`_patch_side`,
  `_patch_cells`). A lateral patch or `b < 0` gives a reason, as
  `wall-rate-constant` does; so do two overlapping `slip-length` patches on
  one wall. A hard `resistance-ratio` bound the slip chamber misses is a
  DRC finding.
- **S9 Slip + species.** Slice-1 test 7 on the hex chamber at `β = 0.1`,
  `Pe ε = 0.05`, `Da_w = 0.1`: outlet conversion agrees within 3σ Monte
  Carlo + 2%. Eulerian with a `wall_damkohler` above reaction-limited
  refuses naming the Lagrangian (M11).

### Heat (slice 2)

`α = k/(ρ c_p)` from the fluid's `thermal_conductivity`, `density` and
`specific_heat_capacity`; `Pe_T = u0 h0/α`; `h_c = Nu k/(2h)`. BC measures
reuse the se-native-fields §The physics registry slugs: `temperature`
(Dirichlet), `heat-flux` (Neumann).

- **H1 Energy balance.** Steady, any domain:
  `|inflow + wall input − outflow| / inflow ≤ 1e-10`.
- **H2 Uniform flux, mean.** Uniform rectangle, `q` on `n_w` walls:
  `dT̄/dx = n_w q/(ρ c_p Ū h)` within 1e-6 relative past the first cells.
- **H3 Uniform flux, wall offset.** `T_w − T̄ = 2h q/(Nu k)` within 1e-3
  at `Nu = 140/17` (both walls) and `140/26` (one wall), the table's 8.23
  and 5.39.
- **H4 Fixed wall temperature.** The decay slope of `ln(T_w − T̄)` matches
  `−n_w Nu_T/(2 ε Pe_T)` within 5%, with `Nu_T` per M5.
- **H5 Wall plate.** Floor plate between two `temperature` patches, uniform
  `q`, fluid temperature held by a high capacity rate:
  `T_w − T_f = (q/h_c)(1 − cosh(m(x*−½))/cosh(m/2))`,
  `m² = h_c L²/(k_w t_w)`, within 1% at `m = 8`, with `Bi` inside its line.
- **H6 `μ(T)` Picard.** Uniform rectangle, uniform flux, fixed `Δp`,
  `μ* = e^{−βθ}`. With `I(θ) = (1−e^{−βθ})/(βθ)` and `θ₁ = q₀ I(θ₁)`,
  `R*/R*₀ = I(θ₁)` within 0.5%. At `β = 1`, `q₀ = 0.5`: `θ₁ = 0.4102`,
  `I = 0.8203`; a one-way solve gives 0.7869 and fails. The law itself is M4.
- **H7 Convergence reported.** `Outcome` carries `coupling` with the
  se-native-fields §Coupling defaults (`tol` 1e-6, `max_iter` 50, `ω = 0.7`).
  A case that does not converge refuses `coupling.diverged` with its last
  three residuals; it never returns a value.
- **H8 Card.** Group `heat`:

  | Line | Number | Holds when | Else |
  |---|---|---|---|
  | `pe_t` | `Pe_T·ε` | ≤ 0.1 | refuse; heat has no Lagrangian route |
  | `wall_thickness` | `t_wall/L` | ≤ 0.05 | hand the wall to `precis_fields` |
  | `wall_biot` | `h_c t_wall/k_wall` | ≤ 0.1 | hand the wall to `precis_fields` |
  | `rayleigh` | `Ra_h` | ≤ 1708 | not-applicable until M8 gives its inputs |
  | `heat_taylor` | constant line | informational | names the insulated-wall 1/210 as the upper bound it uses |

  Worked points: water, `h0 = 100 µm`, `L = 20 mm`, `u0 = 1 mm/s` gives
  `Pe_T = 0.70`, `Pe_T·ε = 0.0035`. With `h_c = 2.47e4 W/m²K`, a 0.5 mm wall
  at `k = 150` gives `Bi = 0.082` and holds; a 1 mm wall at `k = 1` gives
  `Bi = 24.7` and fails.
- **H9 se.** `max-temperature`, `outlet-temperature` and `wall-heat-flux`
  register as computers. A hard `max-temperature` bound the chamber misses
  is a DRC finding. A missing fluid property gives a reason naming it.
- **H10 Heat + species.** `reaction-enthalpy` on a reactive patch adds the
  wall source `(−ΔH)·r`: exothermic raises `T`, and the H1 balance holds
  with the source equal to the species wall sink times `−ΔH` within 1e-10.
  `k_s` stays fixed and the card says so (M6).

### Darcy / Brinkman (slice 4)

`κ* = κ/h0²`. Inside a porous region
`T* = 12κ*h*[1 − (2√κ*/h*) tanh(h*/(2√κ*))]`, with `μ_eff = μ`. A region is
a floor or lid `patch:` datum carrying `permeability` and `porosity`
measures (Q1), and it fills the local gap.

- **D1 Closed form.** Fully porous uniform rectangle:
  `R* = (nx−1)/(ny T*)` within 1e-10 relative. `T*` over Darcy `12κ*h*` is
  0.95101, 0.80002, 0.41891 and 0.07577 at `κ/h² = 6e-4, 1e-2, 0.1, 1`,
  within 1e-5.
- **D2 Hele-Shaw limit.** At `κ* = 1e8`, `T*/h*³ − 1` equals `−1/(10κ*)`
  within 1e-12 absolute. The naive bracket gives −5e-8 there, so the
  evaluation needs a series branch at small `h/√κ`.
- **D3 Series plug.** An interior plug `n_p` columns long:
  `R* = ((nx−1−n_p) + n_p/T*)/ny` within 1e-10 relative. The solve's own
  floor is about 1e-11 at a 100:1 contrast, so 1e-12 is out of reach.
- **D4 Continuity.** Net flux through every column of the plug case is equal
  within 1e-10 relative.
- **D5 Local gap.** A region over a deepened floor uses local `h*`:
  `T*` at `h* = 2` equals the closed form within 1e-12.
- **D6 Darcy + species.** Inside the region the `taylor_tensor` result equals
  `ε(φ/τ)/Pe · I`, plus `(α_L/L)|U*|` along `U*` when `α_L` is set, within
  1e-12; the Taylor term is zero there. Steady mass balance holds within
  1e-10. Missing `porosity` or `tortuosity` gives a reason. Transient runs
  and a `wall-rate-constant` patch overlapping the region refuse (M15).
- **D7 se and card.** Group `darcy`: `kappa_ratio` `κ/h²` (informational,
  names the nearer limit); `reynolds_pore` `ρ q √κ/μ ≤ 1` (se-native-fields
  §Card); `full_height`, a constant line stating the insert fills the gap,
  since nothing can detect one that does not. `κ ≤ 0`, porosity outside
  `(0, 1]`, a lateral patch or overlapping regions give a reason. Tracer
  measurands on a porous chamber return a reason until M10 closes;
  `resistance-ratio` computes. A hard `resistance-ratio` bound the chamber
  misses is a DRC finding.

## Explicitly NOT in scope

Turbulence; 3-D *flow* solves inside precis (B delegates them; 3-D
diffusion-family solves in solids are `se-native-fields.md`); the nano
propagator (`se-flow-kernel.md` slice 3); bimolecular wall kinetics
(`kinetics-engine-v2.md`); liquid-phase catpath rates (no solvation
model; Q3); a web UI for the new fields beyond the slice-2b payload keys;
homogeneous (bulk) reactions. The last is a later one-liner on the same
`fv` source term, filed when a case needs it.

## Target + blast radius

`src/precis_flow/` gains `fv.py`, `species.py` and the species lines in
`regime.py`, and `trace` gains `pe=`/`walls=`/`max_steps=`.
`src/precis_se/flow.py` gains the token keys, the computers and the
`wall_rate` handler op. Measurand taxon nodes for the new slugs need a
core seed migration, which goes through the release owner (C7).
Slices 5, 2 and 4 add one module each under `src/precis_flow/` and
`src/precis_se/`, registering through
`src/precis_flow/physics.py::PHYSICS`,
`src/precis_flow/regime.py::REGIME_GROUPS` and
`src/precis_se/flow_registry.py::COMPUTER_GROUPS`.
Empty registrations are valid: slip and Darcy add no computers, and
none of the three adds a token key (Q1).
`precis-se-regions-help` and the flow skill gain the BC vocabulary.

## Rulings (Reto 2026-11-04, tracker gr563788)

Q1–Q4 spec defaults stand:

- Q1 BCs are measures on `patch:` regions; the `flow:` token stays a role
  list.
- Q2 Species scope is the measure name `<block>.<species>/<property>`
  (`src/precis_se/flow_species.py::_scoped_prop`), not a column on `se_measures`.
- Q3 Liquid-phase catpath rates are refused until a solvation/Henry model
  is stated (`src/precis_se/wall_rate.py::op_wall_rate`).
- Q4 Backend B1 is FEniCSx, run as a job on the compute lane, never in the
  MCP process.

Multiphysics runs in parallel through the registrations in
`src/precis_flow/physics.py::PHYSICS`, subject to §Combination rule.

## Open questions log

Statements not grounded in code or in the tracker. Each names the default
a lane takes until it closes.

- **M1 Solver hook — ruled (Reto, 2026-11-04).**
  `src/precis_flow/flow.py::solve` takes a `T*` factor field;
  `src/precis_flow/tracer.py::trace` takes a per-cell profile. Their
  defaults reproduce `h**3`, `h**2`, plug and parabolic bit-identically,
  without reordering floating-point operations.
- **M2 Physics names — ruled (Reto, 2026-11-04).** Thin-gap physics stay
  keys of the `flow` spec; `physics=['flow']` remains the thin-gap name.
  They do not route through the se-native-fields `REGISTRY`, whose
  `heat`, `species` and `darcy` names belong to `precis_fields`.
- **M3 Species on `evaluate`.** `MEASURANDS` holds the four flow measurands;
  the species computers call `_species_run`, not `evaluate`, so a script
  cannot state a species number and the import re-check cannot cover one.
  `src/precis_se/flow_species.py::_species_run` keeps that route unchanged.
  Default: left as is; S9, H10 and D6
  test through the kernel and the se computers.
- **M4 Fluid properties.** The core registry
  (`src/precis/migrations/0092_material_kind.sql`) has no viscosity or
  diffusivity, though `_species_case_for` reads `viscosity`,
  `dynamic_viscosity` and `diffusivity` from the fluid row. There is no
  source for `μ(T)` either. Default: H6 runs at kernel level on a stated
  `μ*(θ)`; se returns a reason until a property and a law are chosen.
- **M5 Fixed-wall Nusselt.** The table's 8.23 / 5.39 are uniform-flux values;
  a `temperature` patch is a fixed-temperature wall. Default: 7.54 / 4.86,
  the constants of `src/precis_flow/regime.py::_SH`.
- **M6 `k_s(T)`.** §Couplings lists reaction heat → T → `k_s`, but a
  `wall-rate-constant` measure holds one value at one `T` and no activation
  energy is stored. Default: one-way, `k_s` fixed.
- **M7 Reaction enthalpy source.** Slice 2 says catpath `dH`; quest scalars
  (`src/precis/quest/compute.py`) carry none, and the `rxn` handlers hold
  `ΔH`. Default: `reaction-enthalpy` is typed in with `origin='user'`.
- **M8 Rayleigh inputs.** Fluid volumetric expansion is not a property
  (`thermal_expansion_coeff` is a linear CTE), and the chamber's orientation
  to gravity is not modelled, though 1708 holds only heated from below.
  Default: the line stays not-applicable.
- **M9 Wall thickness.** Item 2 of Reto's question reads wall thickness from
  the block's material; no material property holds it. Default: a
  `wall-thickness` patch measure on the `length` measurand.
- **M10 Tracer in a porous region.** Particles move at the interstitial
  speed `U/φ`, not the superficial `U*`; the spec does not say which the
  tracer uses, and `residence-time-t0` depends on it. Default: tracer
  measurands refuse on a porous chamber (D7).
- **M11 Sherwood number with slip.** 7.54 / 4.86 assume a no-slip profile and
  no closed form for `Sh(β)` is stated. Default: Eulerian refuses outside
  the reaction-limited band (S9).
- **M12 Maxwell activation on se.** `mean_free_path` has no measurand, and
  the `flow:` token stays a role list (Q1). Default: Maxwell is an `evaluate`
  spec input; an se chamber declares `slip-length` with `b = λ` itself.
- **M13 `resistance-ratio` reference.** `src/precis_geom/physics.py::_flow_measures`
  solves a flat chamber with the plain gap law. Default: unchanged, so the
  ratio compares against a no-slip, non-porous chamber.
- **M14 Gas compressibility.** Slip widens the kernel to gas at `Kn` up to
  0.1, and the kernel is incompressible; no card line bounds `Δp/p`.
  Default: no line.
- **M15 Porous storage and wall reactions.** The transient term needs `φ`
  storage and the series `k_eff` assumes open-gap `Sh`. Default: both refuse
  (D6).
