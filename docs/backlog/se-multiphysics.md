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

**Ready (slice 1 only), 2026-10-21.** The later slices stay draft; each
gets its acceptance criteria when it goes ready. A physics review (Fable,
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
   measurand slug on the patch (`pocket-from-molecule.md` (region property layer, folded) slice A,
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
  Species scope uses the measure name `<block>.<species>.<property>`
  (Q2).
- Output computers in `FLOW_COMPUTERS`: `outlet-conversion` (ratio),
  `outlet-concentration` (`c*`), `wall-flux-fraction` (fraction of the
  inlet flux consumed on the measure's patch; per-catalyst-patch
  selectivity), `damkohler` (gauge).
- `wall-flux-fraction` uses the developed Sh, which under-reads short
  patches (slice 1b). When the patch's streamwise length is below
  `0.2 Pe h` and its `k_s/k_m ≥ 0.1`, the value carries a reason in its
  meta naming the entrance-region bias and the Lagrangian alternative.
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
    (`< 0.2 Pe h`, `k_s/k_m ≥ 0.1`) gets the entrance-region reason on
    its `wall-flux-fraction`.
12. **catpath binding.** Takes a quest fixture with `tof`, `log_tof*` and
    a slab site area. `wall_rate` writes `k_s` equal to
    `TOF/(site_area_A2 · 1e-20 · N_A · c_wall)` with
    `c_wall = p_bar · 1e5/(RT)`, within 1e-12 relative, with provenance in
    meta. An untrusted solve (no `log_tof`) is refused with a reason, and
    so is `tof ≤ 0`. A fixture with TOF ∝ `p^0.5` records `α = 0.5`
    within 1e-6 and its `apparent_order` line fails with the X ≲ 0.2 text.
    p5/p95 produce a conversion band on the measure.

## Later slices (one physics each; acceptance written when each goes `ready`)

1b. **Entrance-region mass transfer.** Developed Sh ignores the
   concentration boundary layer that restarts at the leading edge of each
   reactive patch. There the local coefficient is Lévêque's
   `k_m(s) = 0.978 (Ū D²/(h s))^{1/3}` (wall shear `6Ū/h`, distance `s`
   from the patch edge), which is larger than the developed value. Slice
   1 therefore under-reads `wall-flux-fraction` on patches shorter than
   about `0.2 Pe h` (it flags them). Either use
   `k_m(s) = max(Lévêque(s), Sh D/(2h))` with `s` from an advected
   age-since-patch-entry scalar, or route short patches with
   `k_s/k_m ≳ 0.1` to the Lagrangian. Removes the slice-1 flag.
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
   reaction enthalpy as a wall source (catpath `dH`).
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
4. **Darcy / Brinkman regions.** A `porous:` region on the pocket with
   `permeability` and `porosity` measures.
   - Transmissivity is the Brinkman closed form in the table, so no
     `κ/h²` threshold is needed for the flow. The old Darcy threshold
     `κ/h² > 0.1` was wrong: Darcy over-reads the flux by 58% at 0.1 and
     is within 5% only for `κ/h² ≤ 6e-4`.
   - Species: `D_eff = D·φ/τ`. The Taylor term is off inside the region
     (the profile there is near plug). Mechanical dispersion `α_L|U|` is
     added for coarse media, with `α_L` a region measure.
   - Card: `κ/h²` (informational: which limit the closed form is near).
5. **Slip.** `slip-length` per patch (hydrophobic regions), Maxwell slip
   for gas from `λ`. The tracer profile follows the slip-shifted parabola.
   The Knudsen line threshold becomes 0.1 when slip is on. With equal slip
   `b` on both walls, the slip part is a plug, so the Taylor coefficient
   falls by `(1 + 6b/h)⁻²` at the same mean velocity; unequal slip passes
   the asymmetric profile through `shear_velocity`/`profile`.
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

Order: 1 → 1b → 5 (slip: small, unlocks gas Kn up to 0.1) → 2 → 3 → 4 →
6 → 7 → 8. 1c when a case has `|α − 1| > 0.2` at conversion above 0.2.
B is unordered. Heat before electro-osmosis because Joule heating needs it.

## Explicitly NOT in scope

Turbulence; 3-D solves inside precis (B delegates them); the nano
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
core seed migration, which goes through the release owner.
`precis-se-regions-help` and the flow skill gain the BC vocabulary.

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
