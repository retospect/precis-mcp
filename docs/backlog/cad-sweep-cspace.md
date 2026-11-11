---
status: ready
title: cad view='sweep' reports exact collision windows (configuration space)
pillar: 3d-design
prio: normal
---

# cad view='sweep' reports exact collision windows (configuration space)

## Motivation / why

`view='sweep'` (`CadHandler._render_sweep`, src/precis/handlers/cad.py)
moves one joint at a time across its `limits:`, the others held neutral,
at `args.n` evenly spaced states, and runs the full pair clearance at
each. Three defects follow from sampling:

- It can miss a collision between samples. At the default n over a full
  turn the step is ~45°; the 6 mm post at r30 in the test rig
  `_GEARED_RIG` blocks ~11° and is caught only because one sample lands
  on 90°.
- Cost scales with n even where every pair is far apart (gr561417: 76 s
  for three boxes).
- The report is "hit at sample i", not where the collision starts and
  ends.

Configuration space fixes all three for one degree of freedom. A pair
with signed gap g at joint value q cannot touch within |Δq| ≤ g / v, where
v bounds how fast any point of the moving material travels per unit q
(revolute: max distance from the axis to the moving body's AABB corners,
times the coupling ratio for gear/belt-driven bodies; prismatic: 1 times
the ratio). Stepping by that bound (conservative advancement) never
skips a collision, takes big steps far from obstacles, and bisection at
the edges gives the interval [q_in, q_out] to a stated tolerance. A
gear/belt chain is still one degree of freedom: a curve in joint space,
so the same walk covers it.

## In scope

- New module `src/precis/cad/cspace.py`: for one swept joint (plus its
  coupled chain), per cross-group pair, return the interference
  intervals over the joint range. Inputs: a callable building the design
  at q (the handler's expand + build_design), the pair, the speed bound.
  Uses the existing signed gap (`relate._clearance`, or the mesh referee
  where `_component_gap` uses it) as g.
- Conservative advancement with a minimum step floor, and bisection at
  each sign change to a tolerance of `CONTACT_TOL_REL` of the pair's
  governing length converted to q (tol / v).
- Persistent-contact pairs: a pair touching (|g| ≤ resolution) across the
  whole range is the joint's own partners (hinge pin and its bore). It
  would stall the walk at zero step. Detect it and test it for
  *interference deeper than resolution* only, sampled at the floor step,
  and label it in the report ("in contact throughout — joint partners").
- `_render_sweep` reports each colliding pair's windows in the joint's
  own unit ("a2 × post: 78°..102°"), keeps the swept-envelope section,
  and keeps `args.n` as the cap on gap evaluations per pair (refuse with a
  next step when the cap is hit before the range is covered, never a
  silent partial answer).
- Update `precis-cad-help` (src/precis/data/skills/) for the new report
  shape, in the same commit.

## Explicitly NOT in scope

- Simultaneous sweeps of two or more independent joints (a 2-D or higher
  configuration space). Follow-up item once this ships; note it in the
  report's footer only if a cheap check (two joints whose swept-envelope
  AABBs overlap) says it matters.
- Speeding up the gap evaluation itself — that is gr561417 / td561550
  (tree g2-sweepfast). Rebase on it when it lands; do not duplicate it.
- Changing `view='clearance'`, `connectivity` or the DOF probe.

## Acceptance criteria

- Recovery: `rescue/cad-sweep-cspace` preserves the unfinished implementation
  and its tests. Do not land it as-is: a resolution-band step can miss a
  narrow penetration. Add a regression for
  `gap(q) = min(abs(q - .55) - .01, .001)`, resolution `.002`, range `[0,1]`,
  speed bound `1`, tolerance `1e-6`, evaluation cap `9`: the walk currently
  reports persistent contact with no intervals despite penetration at `.55`.
  A bounded walk must report uncertainty when it cannot exclude that event.
  Preserve main's batched sweep scoring when integrating the recovery.
- `test_sweep_sees_gear_driven_collisions` still passes, and a new test
  asserts the a2 × post window brackets 90° with edges within 2° of the
  analytic value for the rig geometry (derive it in the test from the
  box dimensions).
- New test: an obstacle narrower than the old 45° sample spacing,
  placed between two sample angles, is reported (the sampler missed it).
- New test: the hinge partner pair does not stall (bounded call count)
  and is labelled in contact throughout, not as a collision.
- The gear-rig sweep makes fewer gap evaluations than the old 9 × 3
  (assert a count through a counting wrapper), with the same verdict.
- `scripts/test tests/test_cad_handler.py` and the cspace tests green;
  `scripts/test --typecheck` green.

## Target + blast radius

`view='sweep'` on kind='cad' (agent-visible report text changes),
src/precis/handlers/cad.py `_render_sweep`, new src/precis/cad/cspace.py,
precis-cad-help skill. No schema, no migration, no worker.

## Open questions / decisions log

- Decision: speed bound from AABB corners, not exact farthest material
  point — looser but always safe, and costs nothing.
- Decision: `args.n` survives as an evaluation cap so existing callers
  keep a bounded cost.
