---
status: draft
title: se datums + measurement-from-geometry evaluator — the attachment point for preferred-number wells
prio: high
model: opus
blocked-by: preferred-number-term
---

# se datums + `m(design)` evaluator

Open item 10 of `multiscale-optimisation-method.md`; the se plumbing
that lets §4's wells attach to real dimensions. Spec only — status
`draft` until the block-feature resolution API is decided.

## Motivation / why
`precis_se/measures.py`'s `MeasureSpec` is a declared relation with a
band and an `origin` (provenance: `user | proposed`), not a number
derived from geometry, and se has no datum concept — only the L1 pose
frame. The wells need `m(design)` and a datum the measurement is
declared against that rides with the boundary but is never a free DOF
(method doc §4, "Where the wells attach").

## In scope
- `datum:` field on `MeasureSpec` — a reference to a block feature:
  `frame` (default; the pose frame = 3-2-1 for prismatic envelopes,
  axis + base face for rotational primitives), `port:<name>`, or
  `face:<selector>` / `axis:<selector>` resolved against the block's
  cad expression.
- Deterministic datum ranking (method doc §4 heuristics: largest flat
  face, assembly-normal, port faces free, process-setup faces,
  same-setup correlation, measurable) producing the default when
  `datum:` is absent; `view='datums'` reporting the ranking + reason.
- `evaluate_measure(design, spec) -> (value, provenance)` — the number
  from geometry via the cad kernel (probe/relate), stamped
  `source: derived`.
- Chain-rule hook: `d m / d params` for feature-parameterised blocks
  (finite difference acceptable in v1; analytic later via the torch
  port).

## Explicitly NOT in scope
- The level set / `dm/dφ` path (slice 4+).
- Making the datum an optimiser variable — forbidden by the guard.
- GD&T tolerance frames / MMC-LMC semantics.

## Acceptance criteria
1. A prismatic block with no `datum:` reports `frame` as datum with the
   3-2-1 faces named; a cylinder reports axis + base face.
2. A block with a port gets that port face ranked above a larger
   non-port face when both are flat (ports are free datums).
3. `evaluate_measure` on a declared wall-thickness measure returns the
   geometric value and disagrees loudly (a `notes` line) when the
   declared value differs by more than the band.
4. Moving the block's pose leaves every datum-relative measurement
   unchanged (datum rides along).
5. Finite-difference `dm/dparam` on a box width measure returns 1.0
   for the width param, 0 for the others.

## Target + blast radius
`precis_se/measures.py`, a new `precis_se/datums.py`, se `get` views,
the se skill. Possibly a plugin migration if `datum:` is persisted
(columnar) rather than carried in the measure JSON — decide at `ready`.

## Open questions / decisions log
- Persist `datum:` in the measure JSON (no migration) vs a column —
  lean JSON.
- Feature selector grammar for `face:`/`axis:` — reuse cad `probe` hit
  addressing? Needs a look at `cad/probe.py` before `ready`.
