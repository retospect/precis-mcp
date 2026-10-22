---
status: ready
pillar: 3d-design
prio: medium
model: opus
title: se flow kernel — dimension-free thin-chamber flow + particle tracing on continuous se/cad objects, with a regime card that says when the model holds
---

# se flow kernel (dimension-free Hele-Shaw + Lagrangian tracer)

Reto, 2026-10-20: "we do mm to 50 cm or so scale for now. If we can
meaningfully do this dimension-free, I love it." Source code: the local
`cfdtest/` scratch repo (`breakthrough-annealer`, gitignored, own git):
`flow.py` (Hele-Shaw finite-volume Poisson solve → depth-averaged U,
resistance), `tracker.py` (vectorised Lagrangian tracer with a stochastic
vertical dispersion active over holes, breakthrough plane), `cost.py`,
`anneal.py`, `geometry.py` (hex chamber mask, ports, hole rasterisation),
`render.py`; 1.4 k lines, numpy + scipy, tests on the invariants. Its
pyproject pins python ≥ 3.14; precis is ≥ 3.12 — the vendored code drops
the pin.

## The dimension-free formulation (worked out)

Thin chamber of lateral extent `L`, gap `h(x, y)` with reference `h₀`,
viscosity `μ`, pressure drop `Δp` between ports. Lubrication (Hele-Shaw):

```
U = −(h²/12μ) ∇p,        ∇·(h³ ∇p) = 0
```

Scale `x* = x/L`, `h* = h/h₀`, `p* = p/Δp`, `U* = U/U₀` with
`U₀ = h₀² Δp / (12 μ L)`, `t* = t/t₀` with `t₀ = L/U₀` (one transit):

```
∇*·(h*³ ∇*p*) = 0,     U* = −h*² ∇*p*,     dx*/dt* = U*
```

Nothing dimensional survives in the solve. The field, the streamlines,
the cell resistance ratio `R/R_ref`, the breakthrough fraction, its
evenness and the hole count depend only on **shape**: the mask, the
aspect ratios `h₀/L`, hole depth `/h₀`, hole diameter `/h₀`, port width
`/L`, the seed band `/L`, the tracking-plane offset `/h₀`. One solve per
shape serves a 1 mm chamber and a 50 cm chamber alike.

One modelling parameter carries physics: the vertical dispersion over a
hole, `D_v* = D_v · t₀ / h₀²` (transit time over vertical mixing time;
cfdtest's `mix_coeff` made dimensionless). It is a pure number in the
kernel, but its *value* belongs to the regime, so it is an input the
regime card sets or the user states, never a hidden default.

**Regime card — where dimensions re-enter.** From the real
`(L, h₀, μ, ρ, Δp or U₀, particle d and ρ_p, molecular D)` the kernel
computes and stores, with a verdict per line:

| Number | Formula | Model holds when | Else |
|---|---|---|---|
| thinness | `h₀/L` | ≤ 0.05 | 3-D Stokes needed (not here) |
| reduced Reynolds | `Re_h · h₀/L`, `Re_h = ρU₀h₀/μ` | ≤ 0.1 | inertia: 3-D Navier–Stokes (not here) |
| Stokes number | `St = τ_p U₀/h₀`, `τ_p = ρ_p d²/(18μ)` | ≤ 0.1 | particles are not tracers |
| gravity ratio | `G = v_s L/(U₀ h₀)`, `v_s = (ρ_p−ρ) g d²/(18μ)` | ≤ 0.1 | settling dominates; add `−v_s*` to the vertical model |
| Péclet | `Pe = U₀h₀/D` | ≥ 10 | diffusion dominates the tracer |
| Knudsen | `Kn = λ/h₀` | ≤ 0.01 | free-molecular: the nano propagator (slice 3), not this kernel |

Worked points: water, `h₀ = 1 mm`, `L = 100 mm`, `U₀ = 1 cm/s` →
`h₀/L = 0.01`, `Re_h · h₀/L = 0.1` (marginal, holds). Water, `h₀ = 5 mm`,
`L = 500 mm`, `U₀ = 5 cm/s` → `Re_h · h₀/L = 2.5`: refused, 3-D needed.
A 1 nm hexfold lumen with gas: `Kn ≫ 1`, slice 3. So "mm to 50 cm" holds
as long as the chamber is thin and slow; the card says which, per case.

**What is stored.** Results in reduced units (`x*`, `t*`, `R*`,
fractions) plus the scale triple `(L, h₀, U₀)` and the regime card, so a
stored solve re-dimensionalises for any size with the same shape and the
card tells whether that size is still inside the model.

## What a continuous se object supplies

- The fluid domain: a cad envelope (or a `cad` kind solid) → thin axis =
  the smallest bounding-box extent; mask = the projection along it;
  `h*(x*, y*)` = local extent along the thin axis over `h₀`; holes and
  pockets are floor topography (depth = max over overlaps, as cfdtest).
- Ports: cad faces selected with the existing `patch:` selector
  (`precis_se/datums.py`), tagged inlet / outlet; the seed band is the
  inlet face's footprint.
- Tracking planes: any `z*` offset, or a station z-window
  (`pocket-from-molecule` stations) — the breakthrough plane generalises
  to "fraction of particles that reach region X by `t*`".
- Outputs as **measures on regions** through the `COMPUTERS` path that
  pocket-from-molecule slice 2a wires (`breakthrough_fraction`,
  `residence_time_t0`, `resistance_ratio`, `evenness`), so a pocket spec
  or a chamber spec can *require* a flow property and DRC checks it.

## Slices

*Slice 1 (kernel package `src/precis_flow/`) landed 2026-10-20 — the
commit carrying this line; `git log` has the sha.*

2. **se adapter** *(landed 2026-10-21 — the commit carrying this
   line)*: cad envelope → `Domain` (thin-axis detection, depth map,
   `patch:` ports), `view='flow'` (field, streamlines, card), measures
   through `COMPUTERS`. Test: the cfdtest hex chamber rebuilt as a cad
   envelope reproduces cfdtest's breakthrough fraction within its own
   seed scatter. `view='flow'` also serves the player payload of slice
   2b: chamber mesh (outline + floor depth map), the recorded frames
   (`trace(record_frames=K)`, float32 `(K, n, 3)` reduced positions,
   `z*` 0 = floor, 1 = ceiling) as one binary blob, the scale triple
   and the regime card. Over ~20 MB the server subsamples particles,
   then frames, and the payload says by how much. Landed as
   `src/precis_se/flow.py`: `domain_from_solid` accepts a `Placed`
   envelope or a `(Design, Expr)` cad solid; the chamber spec is a
   pocket shaped `flow:in=<patch>;out=<patch>;dv=<d_v*>[;profile=…]
   [;zplane=…]`; the adapter's profile default is `parabolic` (Q4).

2b. **Web particle player** *(landed 2026-10-21 — the commit carrying
   this line)*: the Flow tab `/se/<slug>/flow` (`routes/se_flow.py`,
   `static/se-flow-player.js` on three-r184, the payload maths in
   `static/se-flow-frames.js`), linked from the se 3-D view when the
   design carries a `flow:` pocket. The payload gained port masks, the
   tracking plane, per-particle outcomes and `size=` re-dimensioning;
   chamber build and trace are memoised per design version. A `field:`
   envelope works as a chamber (its ports sit on other blocks).
3. **Nano propagator** *(landed 2026-10-21 — the commit carrying this
   line)*: test-particle Monte Carlo with diffuse wall reflection on a
   hexfold lumen (`generated.sites` + vdW-offset wall), same `trace`
   outputs; the card routes `Kn ≫ 1` here. Landed as
   `src/precis_flow/nano.py` (`CylinderLumen`/`AtomWall`/`propagate`),
   `RegimeCard.route`, the adapter's lumen path (`flow:lumen;alpha=`,
   `view='flow'` on structure-bound blocks), and
   `tests/test_precis_flow_nano.py` + `tests/test_se_flow_nano.py`.
   Reduced time `t* = t v̄/L` with `v̄ = sqrt(8kT/πm)`. Transmission then
   depends on shape and the accommodation coefficient `α` only: the same
   dimension-free claim the continuum kernel makes.
   Acceptance (`tests/test_precis_flow_nano.py`, kernel;
   `tests/test_se_flow_nano.py`, adapter):
   - **Clausing.** For a straight cylinder with diffuse walls (`α = 1`),
     `n = 10⁵`, transmission at `L/R = 1, 2, 10` is `0.6720, 0.5136,
     0.1910` within ±0.005 absolute (3σ ≈ 0.0045). Transmission falls
     monotonically with `L/R` and approaches `8R/3L` from below for
     `L/R ≥ 50`.
   - **Specular limit.** `α = 0` on a straight tube gives transmission
     exactly 1.
   - **Cosine law.** Re-emitted directions from a diffuse wall pass χ²
     against `cos θ` (p > 0.01, 10⁵ samples).
   - **Equilibrium.** A closed lumen with no ports, run long, gives a
     uniform number density per accessible volume bin (χ², p > 0.01),
     with no build-up at the walls.
   - **Size invariance.** The same shape at two absolute scales with the
     same seed gives identical transmission and identical `t*`
     residences.
   - **Hexfold lumen.** A tube from the hexfold generator, with the wall
     made of `generated.sites` spheres at vdW radius. Transmission lies
     within 10% of the cylinder Clausing value at `R_eff = R_axis −
     r_vdW` and the same `L/R_eff`. The measured deviation is recorded in
     the test docstring as the corrugation effect.
   - **Same outputs.** The propagator returns a `Paths`
     (`breakthrough` = transmission, `residence`, `frames`), so the four
     `FLOW_COMPUTERS` measures and the slice-2b payload work unchanged.
     Test this on a hexfold lumen block through `view='flow'`.
   - **Routing.** `RegimeCard.from_physical` with `Kn ≥ 10` names
     `regime.knudsen` and routes to the propagator. `view='flow'` runs it
     without being asked. With `0.01 < Kn < 10` (transition regime) both
     solvers refuse, and the reason names DSMC as out of scope. Slip
     (`se-multiphysics.md` slice 5) later moves the lower bound to 0.1.
   - **Determinism.** Same `seed_rng` gives bitwise-equal `Paths`.
4. **Annealer as an se op**: hole/port placement under the realize loop
   (cfdtest `anneal.py`), cost terms as measures.
   The pure annealer is vendored as `precis_flow.anneal`: Metropolis, with
   add / remove / perturb proposals scaled by `T/T0`, geometric cooling,
   and the tracer reseeded identically every evaluation (common random
   numbers). The se op is handler-level `anneal_chamber`, because it
   spends compute; above the inline budget it runs as a job. The cost is
   the weighted sum of the chamber's **soft** measures
   (`breakthrough-fraction` maximised, `resistance-ratio` and hole count
   minimised, `evenness` maximised), with weights in the measure meta.
   **Hard** measures are constraints: a proposal that violates one is
   rejected.
   Acceptance (`tests/test_precis_flow_anneal.py`,
   `tests/test_se_flow_anneal.py`):
   - **Cost parity.** On the hex chamber (`test_se_flow.py` fixture) with
     cfdtest's default weights (`w_break 10, w_holes 0.5, w_res 5,
     w_even 5`) as soft measures, `profile='plug'` and the same seed, the
     op's cost of the initial hole set equals cfdtest `cost.evaluate`'s
     `total` within 1e-9.
   - **Common random numbers.** Evaluating the same state twice gives a
     bitwise-equal cost.
   - **Reproduction.** At cfdtest's default config (iterations, cooling
     0.97, 3 iterations per temperature), the best cost lies within the
     spread of cfdtest `anneal` over 5 seeds. The best is strictly below
     the initial cost.
   - **Determinism.** The same seed gives the same best hole set.
   - **Hard constraints hold.** With a hard `resistance-ratio ≤ 1.2`, no
     accepted state violates it, and `view='drc'` on the result is clean
     for that measure.
   - **Origin.** Holes land as cad `body add` cylinders with
     `origin='proposed'`. A `user` hole is never moved or removed, and a
     test pins one through the whole run. Re-running revises only its own
     proposed holes, as one revision per call.
   - **Manufacturable.** Hole depth stays at or below the floor plate
     thickness minus the process `min_wall`. Spacing between holes stays
     at or above `min_wall`. Both are asserted on every accepted state.
   - **Round trip.** After the op, the four flow measures re-evaluated
     through `COMPUTERS` equal the op's reported best breakdown within
     1e-9.
   - **Budget.** An `iterations` value over the inline cap is refused
     with `next=` naming the job form.

## Decisions (Reto 2026-10-20: "yes")

- Q1 Vendored into `src/precis_flow/` (numpy + scipy, both already
  precis deps), not imported from `breakthrough-annealer`.
- Q2 Cad envelopes first (slice 2); hexfold lumens in slice 3 with the
  nano propagator — the continuum kernel is wrong there.
- Q3 3-D (non-thin) chambers stay out; a follow-on (external Stokes
  solver) is named when a case needs it.

Sequencing: slice 1 is self-contained and builds now. Slice 2 waits for
pocket-from-molecule slice 2 to land, because that slice fixes the
provisional `Computer` signature in `precis_se.properties.COMPUTERS`.
Slice 2b follows slice 2.

## Decided before slice 2 (Reto 2026-10-21: "Parabolic default yes")

- Q4 Vertical speed profile. cfdtest's tracker (`tracker.py::track`)
  moves every particle horizontally at the gap-averaged speed whatever
  its height; the Hele-Shaw profile is parabolic (1.5× at mid-gap, 0 at
  floor and lid), which is what spreads a tracer pulse (Taylor
  dispersion). The player makes the uniform speed visible. Using the
  profile `U(z*) = 6 z*(1 − z*) · Ū` is a few lines but changes
  cfdtest's breakthrough numbers, so slice 2's reproduction test would
  compare against the profiled tracker, not cfdtest. Decided:
  slice 2 adopts the profile behind `profile='parabolic'|'plug'`,
  default parabolic; `plug` reproduces cfdtest for the reproduction
  test.

  **Mechanism landed (the commit carrying this line).** `trace` now takes
  `profile='plug'|'parabolic'`; `'parabolic'` weights the horizontal step
  by `6 psi (1 − psi)` (vertical model untouched). The *kernel* default is
  `'plug'` so the landed slice-1 tracer and its cfdtest-calibrated tests
  are unchanged — the parabolic-as-default *policy* belongs to slice 2's
  adapter, which selects `profile='parabolic'` and runs its reproduction
  test against `'plug'`. Only that policy choice is still Reto's to confirm;
  the capability is ready.

## Explicitly NOT in scope

3-D Navier–Stokes; two-phase or free-surface flow (EWOD droplets are a
different model); heat; chemistry in the fluid; the GIF renderer (the
web player of slice 2b replaces it).

## Target + blast radius

New `src/precis_flow/`; `precis_se` gains the adapter module and the
`COMPUTERS` entries; `cad` kind untouched beyond reading envelopes;
`precis_web` gains the Flow tab and its static JS (slice 2b).
