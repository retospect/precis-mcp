---
status: idea
title: "pcb: land parts on round-value positions (preferred-number placement)"
pillar: 3d-design
prio: low
---

# pcb: preferred-number (round-value) placement

## Problem

The placer lands parts on arbitrary decimals (x = 37.4183 mm). SE already
pulls declared measurements toward round values
(`src/precis/structsolve/preferred.py`: tiers from part size, 1-2-5 pitches,
softmin of bilateral-Huber wells; method in
`multiscale-optimisation-method.md` §4); PCB placement does not use it.
Round poses read better in review, survive hand edits and diffs, and line
up headers on the 1.27/2.54 mm pitch a human expects. Reto 2026-10-14:
approved filing now, build later.

## Option 1 — deterministic post-anneal snap (recommended first slice)

A pass after the anneal, shaped like
`src/precis/pcb/optimize.py::OptimizeEngine.snap_hard_aligns`: propose,
gate on `_placement_is_legal`, keep or revert, report.

- xy snaps to a grid scaled to the part: 0.5 mm general; 1.27 / 2.54 mm
  (or the connector's own pitch) for header-class parts; 0.05 / 0.1 mm floor
  for tiny passives. Rotation snaps to the nearest 90 deg.
- Reject any snap the courtyard / DRC checks flag (new finding vs the
  unsnapped pose); the part keeps its unsnapped pose. Stamp meta on every
  part that did snap (grid used, delta) so the result is inspectable.
- Rigid-group members move by the same delta, as every TRANSLATE does.

## Option 2 (later) — wells as a soft cost term in the anneal

Feed `preferred.py` wells into the anneal cost so the search settles on
round values itself instead of being corrected afterwards. Needs the cost
coefficients (`default_coefficients`, `scale_A`) calibrated against the PCB
money terms; do only if Option 1's rejection rate shows the post-pass
leaves too many parts unsnapped.

## Interactions

- Frozen parts (`fixed` set) are never snapped.
- Hard aligns win over the grid: snap runs after `snap_hard_aligns` and
  never breaks a satisfied hard align (revert that snap).
- Poses carried from an import (EasyEDA) are left alone unless the part is
  free.
- Related, separate item: [pcb-soft-pin](pcb-soft-pin.md) (bounded travel
  instead of a hard hold).

## Acceptance criteria (slice 1)

- Every snapped pose lies on its part's grid; rotation is a multiple of
  90 deg.
- No new DRC findings versus the pre-snap board; a snap that would add one
  is reverted and the part keeps its pose.
- Frozen parts are bit-identical before and after.
- Hard aligns satisfied before the pass are satisfied after.
- Idempotent: running the pass twice changes nothing the second time.
- Snapped parts carry the meta stamp; unsnapped ones do not.

## Target + blast radius

`src/precis/pcb/optimize.py` (new pass beside `snap_hard_aligns`), the
caller that runs the post-anneal passes, job summary text. No schema
change expected.
