---
status: ready
title: se datums + measurement-from-geometry evaluator — the attachment point for preferred-number wells
prio: high
model: opus
---

# se datums + `m(design)` evaluator

Open item 10 of `multiscale-optimisation-method.md`; the se plumbing
that lets §4's wells (`precis.structsolve.preferred`, shipped) attach to
real dimensions. Decisions closed 2026-09-16 (see log); `ready`.

## Motivation / why
`precis_se/measures.py`'s `MeasureSpec` is a declared relation with a
band and an `origin` (provenance: `user | proposed`), not a number
derived from geometry, and se has no datum concept — only the L1 pose
frame. The wells need `m(design)` and a datum the measurement is
declared against that rides with the boundary but is never a free DOF
(method doc §4, "Where the wells attach").

## Decisions (recorded — do not re-ask)
- **Persistence: a column.** `se_measures.datum TEXT NULL` via a new
  forward-only migration; `NULL` means the `frame` default. Not a key
  in the `relation` JSONB: the datum is addressable (the `datums` view,
  "which measures hang off datum A", a stale-datum DRC finding), and
  JSON keys neither index nor constrain. Round-trips through
  `persist.py::_MEASURE_COLS` and the `set_measure` op.
- **Selector grammar: predicate declares, name pins (option C).** The
  stored `datum` carries the *declared* selector — an explicit name or a
  predicate. Every evaluation records the *resolved* `instance.tag` in
  the measurement provenance. If the resolution differs from the last
  recorded one (predicate now picks another face; a named face is gone
  after a merge/split/topology move) the evaluator emits a `notes` line
  and the caller re-baselines — never integrates a gradient across the
  jump. Same posture as dangling relations: legal at write, DRC's
  finding at read.
- **v1 vocabulary — exactly this, nothing more:**
  `frame` · `port:<name>` · `face:<instance>.<tag>` · `axis:<instance>`
  · `face:largest` · `face:normal=<±x|±y|±z>` · `face:perp=assembly`.
  Tags are the cad kernel's own (`cad/primitives.py::Face`: `bottom`,
  `top`, `side<N>`, `cut`; `lateral` for curved flanks). Compound
  predicates live in the ranking function, not the grammar — ranking
  picks the default, the grammar only names an override.
- **Default when `datum` is NULL:** the block's pose frame — 3-2-1 on a
  prismatic envelope (three faces meeting at the frame origin), axis +
  base face on a rotational primitive.
- The datum is **not** an optimiser variable (the no-free-DOF guard).

## In scope
- Migration `NNNN_se_measure_datum.sql`: `ALTER TABLE se_measures ADD
  COLUMN datum TEXT NULL` (+ the usual header comment carrying the why
  above, compact).
- `MeasureSpec.datum: str | None = None`; `_MEASURE_COLS`, load/save,
  `set_measure` op accepting `datum:` (vetted through the grammar
  parser at write time — unknown selector shape is loud; a named
  instance/tag that does not exist yet is legal, per the posture).
- New `precis_se/datums.py`:
  - `parse_selector(text) -> Selector` (the vocabulary above; frozen
    dataclass with `kind` + fields).
  - `resolve(design_or_tree, block, selector) -> ResolvedDatum`
    (`frame`: origin + three axes; `face`: `instance`, `tag`, plane
    point + outward normal; `axis`: point + direction) — via the block's
    cad envelope/realised solid and `cad` faces; `port:` through the
    block's `PortSpec` pose.
  - `rank_datums(tree, block, *, assembly_dir=None) -> list[Ranked]`
    — deterministic ranking per method doc §4: port faces first (free
    datums), then area × flatness × (perpendicular to assembly
    direction) × measurable; each entry carries a one-line `reason`.
    Produces the default when `datum` is NULL.
  - `evaluate_measure(tree, spec) -> MeasureValue(value, unit,
    datum_resolved: str, source='derived', notes: tuple[str, ...])` —
    the geometric number: distance between the measured feature and the
    datum feature along the datum normal/axis (wall thickness, offset,
    width). v1 measured-feature addressing reuses the same selector
    grammar (`face:`/`axis:`) carried in `relation` under a new
    `feature` key, validated by `validate_relation`.
  - `d_measure(tree, spec, params) -> dict[param, float]` — central
    finite difference over the block's envelope params (v1); analytic
    later via the torch port.
- se `get` view `datums` reporting the ranking + reasons + which
  measures hang off each datum; `view='measures'` shows `datum` and the
  derived value beside the declared one.
- se skill: one paragraph on `datum:` and the vocabulary.
- Package docstring `precis_se/__init__.py`: the why (column vs JSON;
  predicate-declares-name-pins; no-free-DOF guard), compact.

## Explicitly NOT in scope
- The level set / `dm/dφ` path (slice 4+).
- Making the datum an optimiser variable.
- GD&T tolerance frames / MMC-LMC semantics.
- Compound predicates in the grammar; measurable-by-caliper swept-volume
  check (rank with a placeholder `accessible=True` and a TODO note).

## Acceptance criteria
1. A prismatic block with no `datum` reports `frame` as datum with the
   three 3-2-1 faces named (`instance.tag`); a cylinder block reports
   axis + `bottom`.
2. A block with a port gets that port face ranked above a larger
   non-port flat face (ports are free datums); `reason` says so.
3. `evaluate_measure` on a declared wall-thickness measure returns the
   geometric value; mismatch precedence is band-first — a declared
   `[min_value, max_value]` band flags when the *derived* value falls
   outside it, else `relation.tol` around the declared value, else
   exact equality — and a `notes` line says `mismatch` with both
   numbers.
4. Moving the block's pose (`set_pose`) leaves every datum-relative
   measurement unchanged to 1e-9 relative.
5. Central-difference `d_measure` on a box width measured against
   `face:body.side0` returns 1.0 (±1e-6) for the width param, 0 for the
   others.
6. `face:largest` on a box that is then re-parameterised so a different
   face becomes largest: the second evaluation's `datum_resolved`
   differs and `notes` carries a `datum moved: side0 → top` line.
7. `face:body.side9` on a 4-sided box: write succeeds, evaluation
   returns `value=None` with a `notes` line naming the unresolvable
   selector; `set_measure` with `datum: "faces:foo"` (bad shape) raises
   `MeasureError`.
8. Round-trip: save → load preserves `datum`; a row saved before the
   migration loads with `datum=None` and evaluates as `frame`.
9. `precis migrate --dry-run` on a throwaway DB shows only the new file
   pending; baseline unchanged by hand — verified 2026-09-16 on a
   throwaway dev DB: 0012 applies, `se_measures.datum text NULL` lands,
   second dry-run clean.

## v1 notes
- `port:<name>` resolves to port identity (block origin + direction),
  not a face tag — accepted for v1.

## Target + blast radius
`precis_se/measures.py`, `precis_se/persist.py`, `precis_se/ops.py`,
new `precis_se/datums.py`, se handler views, se skill, one migration.
`precis.cad` is read-only from here (faces/probe). Version bump.
