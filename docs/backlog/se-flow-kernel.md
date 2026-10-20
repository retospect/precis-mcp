---
status: draft
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

1. **Kernel package** `src/precis_flow/` (vendored from cfdtest
   `flow.py` + `tracker.py` + `cost.py`, dimension-free API):
   `Domain(mask, h_star, ports)`, `solve(domain) -> Field(U_star,
   p_star, R_star)`, `trace(field, seed, n, D_v_star, seed_rng) ->
   Paths(breakthrough, residence)`, `RegimeCard.from_physical(...)`.
   Tests: cfdtest's invariants ported; a uniform rectangle reproduces
   Poiseuille `R*`; the same mask at `L = 1 mm` and `L = 500 mm` gives
   identical reduced results and different cards; the card refuses the
   `Re_h · h₀/L = 2.5` worked point by name.
2. **se adapter**: cad envelope → `Domain` (thin-axis detection, depth
   map, `patch:` ports), `view='flow'` (field, streamlines, card),
   measures through `COMPUTERS`. Test: the cfdtest hex chamber rebuilt
   as a cad envelope reproduces cfdtest's breakthrough fraction within
   its own seed scatter.
3. **Nano propagator**: test-particle Monte Carlo with diffuse wall
   reflection on a hexfold lumen (`generated.sites` + vdW-offset wall),
   same `trace` outputs; the card routes `Kn ≫ 1` here.
4. **Annealer as an se op**: hole/port placement under the realize loop
   (cfdtest `anneal.py`), cost terms as measures.

## Open questions (Reto)

- Q1 Vendor into `src/precis_flow/` (one dependency, numpy + scipy) or
  keep `breakthrough-annealer` a separate package and import it?
  Recommendation: vendor; it is 1.4 k lines and the API changes.
- Q2 Which se objects first: cad envelopes (EWOD cells, the hex chamber)
  or hexfold lumens? Recommendation: cad first (slice 2), lumens in
  slice 3 with the nano propagator — the continuum kernel is wrong there.
- Q3 3-D (non-thin) chambers: out of scope here; name a follow-on
  (external Stokes solver) when a case needs it.

## Explicitly NOT in scope

3-D Navier–Stokes; two-phase or free-surface flow (EWOD droplets are a
different model); heat; chemistry in the fluid; the GIF renderer.

## Target + blast radius

New `src/precis_flow/`; `precis_se` gains the adapter module and the
`COMPUTERS` entries; `cad` kind untouched beyond reading envelopes.
