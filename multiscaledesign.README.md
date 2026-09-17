# Multiscale design optimisation — intermediate state (2026-09-16)

Working note for the multiscale optimisation leg. Not a spec: the specs
are in `docs/backlog/`, the method in
`docs/backlog/multiscale-optimisation-method.md`. This file records
where the build stands so a session can resume without re-deriving it.

## Method (settled)

Discrete outer layer (annealing over topology, process, material,
joints, lattice index, symmetry group, tier snaps) around a continuous
inner layer (gradient descent / CMA-ES over geometry and sizing). Every
cost term — physics, manufacturability, dimensional aesthetics — is an
integral over the domain so all contribute to one boundary velocity.

Corrections made on intake, all in the method doc:

- Preferred-number tiers combine by **min of offset wells** (softmin),
  not max — max inverted the intent (coarse tier dragged a round 10 mm
  toward 0/100).
- **Bilateral Huber**: quadratic caps at both the well bottom and the
  sawtooth peak; the peak becomes a zero-gradient ridge, no chatter.
- Tiers derive from part size `L` (`L/10, L/50, L/100` → nearest 1-2-5);
  remaining coefficients are rules (`ε = 1e-4·p_min`, `B` offsets a fixed
  fraction of the fine peak, `A` scaled to a target pull ratio).
- Level set at multiple layers: per-block φ → multiphase seams at
  assembly level → topological derivative for nucleation. SIMP stays the
  shipped default topology route.
- Symmetry: imposed (owner `pattern-groups.md`) and soft `J_sym`
  (method doc §6a).
- Datum is a **feature** of the block, never a free DOF.
- Tooling: torch autodiff via an `optim` extra (CPU fallback is fine);
  level-set machinery written in-house; pcb becomes a second renter via
  `seed_placement`.

## Shipped

- `main 65eb5e9f` — slice 1, `precis.structsolve.preferred`
  (`tiers_for`, `well`, `penalty`, `evaluate`, `default_coefficients`,
  `scale_A`, `pull_ratio`), pure numpy, 8 tests, v8.33.1.

## In flight — branch `feat/se-datum-measure-eval` (slice 2)

Item: `docs/backlog/se-datum-measure-eval.md` (`ready`; decisions
recorded there: `se_measures.datum` **column**; selector grammar
"predicate declares, name pins"; v1 vocabulary `frame · port:<name> ·
face:<instance>.<tag> · axis:<instance> · face:largest ·
face:normal=<±x|±y|±z> · face:perp=assembly`).

Implemented: `src/precis_se/datums.py` (`parse_selector`, `resolve`,
`rank_datums`, `evaluate_measure`, `d_measure`), `MeasureSpec.datum` +
`relation.feature`, persist round-trip, `set_measure`/`add_measure`
vetting, `view='datums'`, `measures` view gains datum/derived columns,
skill + package docstring, `tests/test_se_datums.py` (c1–c8), v8.34.0.

Verified: `scripts/test tests/test_se_datums.py tests/test_se*.py` →
1232 passed; ruff check + format clean.

Review findings fixed: the `datums.py` mypy redefinition and the
`str | None in str` test both cleaned up; `evaluate_measure` mismatch
precedence is band-first (a declared `[min,max]` flags on the
*derived* value falling outside it, else `relation.tol` around the
declared value, else exact); `precis migrate --dry-run` verified on a
throwaway dev DB — plugin `0012_se_measure_datum` is the only file
pending after 0010, applies clean, `se_measures.datum text NULL`
lands, a second dry-run reports nothing to apply.

v1 note: `port:<name>` resolves to port identity (block origin +
direction), not a face tag — accepted for v1.

## Queue after slice 2

`J_sym` over a sampled φ → `precis/optim/anneal.py` protocol
(State/Move/generator/energy, staged schedule + reheat) → cad→grid
voxeliser + gradient sampler → author the struts-and-strings bracket as
a prod se design → level-set core (HJ, reinit, velocity extension) →
stress p-norm/KS with torch adjoint → pcb `seed_placement` bridge →
disclosure write-up (after it works).
