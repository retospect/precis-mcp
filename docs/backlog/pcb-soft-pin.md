---
status: draft
title: "pcb: soft pin — fixed with bounded travel"
pillar: 3d-design
---

# pcb: soft pin (fixed with `max_travel_mm`)

## Problem

`pcb_instances.fixed` takes `'xy'`, `'rot'` or `'both'`, and all three are
hard holds. `OptimizeEngine.bounds_for` is the whole board minus the
keep-out radius, with no per-part box, so there is no way to say "stay
about here".

## Trigger

taz15. The frozen source headers CN2 and CN5 overlap by 0.225 mm under the
courtyard rule (pad hull plus clearance), while their footprint boxes clear
by about 0.075 mm, so every route refuses. Reto ruled (2026-10-14) to
soften the hold slightly rather than force the route or add a waiver.

## Design

An instance meta key or a `fixed` variant carrying `max_travel_mm`;
rotation held. `OptimizeEngine.bounds_for` intersects the board box with a
per-instance box of half-width `max_travel_mm` around the stored pose.
Moves leaving the box are filtered the same way the existing `fixed`
filters work. DRC and the job summary record the travel actually used per
soft-pinned part.

## Acceptance criteria

- Parts with a soft pin never leave their box.
- Rotation stays unchanged.
- With a bound of 0, behaviour equals `fixed='both'`.
- The taz15 CN2/CN5 overlap clears in a regression fixture.

## First use

taz15 CN2/CN5 with about 0.5 mm, after the THT side fix lands (pcbfixes-c2
commit 1b987247b, the `_sides_clear` change).

## Related

[pcb-preferred-placement](pcb-preferred-placement.md) — the grid snap must
respect the soft-pin box.
