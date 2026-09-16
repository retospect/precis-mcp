---
status: ready
title: preferred-number term — differentiable round-value wells (structsolve/preferred.py), slice 1 of multiscale-optimisation-method
prio: high
model: sonnet
---

# Preferred-number term — `precis/structsolve/preferred.py`

Slice 1 of `multiscale-optimisation-method.md` §4. Pure numpy,
store-free, unit-agnostic (the structsolve house rules); no dependency
on anything unshipped.

## Motivation / why
Optimisers emit 8.3586 mm; humans want 10. Rounding after the fact
discards the optimiser's knowledge of which dimensions can move. A
differentiable well term lets roundness compete with physics on the
same gradient. Design decisions (min-of-offset-wells not max; bilateral
Huber; softmin across tiers; tiers from part size) are settled in the
method doc — implement, don't re-derive.

## In scope
- `tiers_for(L: float, *, n: int = 3) -> tuple[float, ...]` — pitches
  `L/10, L/50, L/100` (first `n`), each rounded to the nearest 1-2-5
  value.
- `well(m, p, *, A, B, eps) -> (value, dvalue_dm)` — one tier:
  `d_p(m)`, bilateral Huber (quadratic within `eps` of both bottom
  `d=0` and peak `d=p/2`, linear between), value `A*h(d) − B`.
- `penalty(m, tiers, *, A, B, eps, beta) -> (value, dvalue_dm)` —
  softmin (log-sum-exp with sharpness `beta`) over tiers; vectorised
  over an array of measurements.
- `default_coefficients(tiers, *, kappa=0.5) -> Coefficients` —
  `eps = 1e-4*min(tiers)`, `beta` from blend width `= eps`, `B` offsets
  from the survival fraction `kappa` (see method doc table), `A=1`
  (unscaled).
- `scale_A(grad_penalty, grad_physics, *, rho_star=0.1) -> float` —
  the `A0` rule.
- `pull_ratio(grad_penalty, grad_physics) -> float` — the diagnostic.
- `PreferredResult` dataclass with `notes: tuple[str, ...]` in the SIMP
  posture (which tiers, coefficients used).
- Package docstring "why" paragraph: min-not-max (with the m=10
  counterexample), bilateral Huber, softmin, tiers-from-L.
- Export from `precis.structsolve.__init__`.

## Explicitly NOT in scope
- Attaching to se measurements / datums (`se-datum-measure-eval.md`).
- Any annealer move (snap-to-coarser-tier).
- torch port; level set; anything with a field solve.

## Acceptance criteria
1. Gradient check: central finite difference of `penalty` matches the
   returned derivative to 1e-6 relative on 1000 random `m` in
   `[0, 3L]`, including points within `eps` of well bottoms and peaks.
2. Continuity: `penalty` and its derivative are continuous across
   `d = eps`, `d = p/2 − eps`, and across tier tie surfaces (no jump >
   1e-9 at 1e-7 offsets).
3. Rest points: for `L = 87` (tiers 10, 2, 1) with default
   coefficients, **every** grid point of every tier in `[0, L]` is a
   strict local minimum of `penalty` (derivative changes sign across
   it). This is the survival criterion and is what `max` would fail.
4. Counterexample regression: with the tiers above, the derivative at
   `m = 10` is 0 and `penalty(10) < penalty(10 ± 0.3)`; the same test
   against a `max`-combined variant (test-local helper) must FAIL —
   i.e. the test asserts `max` pulls away from 10.
5. Coarse preference: `penalty(10) < penalty(8) < penalty(7)` — coarser
   rest points are deeper, but `8` and `7` (grid points of tiers 2 and
   1) remain local minima (criterion 3).
6. Descent demo: gradient descent on `m0 = 8.3586` with a fixed step
   under `penalty` alone converges to 8 (nearest tier-2 point), not 10
   — the term does not haul across basins by itself.
7. Rounding rule: nearest 1-2-5 value **in log space**, duplicates
   removed (a small part collapses to fewer tiers). Tests:
   `tiers_for(87.0) == (10.0, 2.0, 1.0)` and
   `tiers_for(0.0347) == (0.005, 0.0005)` (raw `L/10, L/50, L/100` =
   0.00347, 0.000694, 0.000347 → 0.005, 0.0005, 0.0005 → deduped).
8. `pull_ratio` and `scale_A`: scaling `A` by `scale_A(...)` makes
   `pull_ratio == rho_star` to 1e-12.
9. `uv run ruff check`, `ruff format --check`, `mypy src tests` clean;
   `scripts/test tests/test_structsolve_preferred.py` green; changed
   lines test-executed.

## Target + blast radius
New module + `structsolve/__init__.py` exports +
`tests/test_structsolve_preferred.py`. No handlers, verbs, workers,
schema.

## Open questions / decisions log
- 2026-09-15 (Reto): tiers scale from part size, few steps;
  coefficients become rules — decided, see method doc §4.
- 2026-09-15 (Reto): torch autodiff via an `optim` extra is approved
  for LATER slices (thresholds.md new-dependency trip pre-cleared);
  this slice stays numpy.
