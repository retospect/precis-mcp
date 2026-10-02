---
status: draft
pillar: 3d-design
title: T-handle bearing — the third hexfold test piece (nanotube race, rotating inner handle)
prio: high
blocked-by: hexfold-integration
---

# T-handle bearing — third hexfold test piece

Reto, product-plan review 2026-09-30: the third hexfold test piece after the
box (`src/hexfold/spec.md` §28 roadmap step 3) and the rotary ratchet valve
(`rotary-ratchet-valve.md`). A nanotube with buds on opposite sides serving
as races, with an inner T-handle that rotates.

Peer session hexa, 2026-09-30: buds-on-opposite-sides is producible today
under the existing hexfold notation; the revolute joint needed is the same
two-block joint the box and valve test pieces already specify — `spec.md`
§28 roadmap step 3 names both "axle ⇄ liner as separate blocks with a
revolute joint" (box) and "rotor ⇄ shell as separate blocks with a revolute
joint" (valve). No new joint primitive.

## Motivation / why

Two test pieces (box, valve) have proven the revolute-joint pattern once
each, but neither is designed as a *bearing* — a component meant to be
reused as a building block inside a larger assembly. The T-handle piece is
the first one built explicitly to validate the bearing pattern and to be
the first real consumer of the se joint sweep
(`precis_se.kinematics_drc.sweep_findings`: a revolute joint's declared
`params.range` checked for envelope collisions in `view='drc'`).

## In scope

- The `.hx` piece: a nanotube with buds on opposite sides as races, an
  inner T-handle block.
- The two-block revolute joint, built the same way the box/valve pieces
  already do it — no new joint kind.
- Wiring this piece up as the first consumer of the joint sweep: declare
  the handle's `range` on its revolute connect and read `view='drc'`.

## Explicitly NOT in scope

- The joint sweep itself — it lives in `precis_se.kinematics_drc`; this
  item is its first real case, not its implementation.
- Motion planning or torque budgeting for the handle — out of scope per the
  same DRC-vs-planner boundary `se-3d-viewer`'s thread draws (a check, not
  a planner).
- The incommensurate-lattice question for shaft-in-sleeve bearings
  (`diamondoid-pattern-language.md` open question 3) — that's the sp³
  diamondoid bearing variant; this is an sp² hexfold bud-race bearing, a
  different lattice regime.

## Acceptance criteria

- The `.hx` piece builds, `check`s clean under the existing hexfold
  notation, and round-trips through `generate` as bound `structure`
  designs, matching the box/valve precedent.
- The T-handle rotates within its race with the same revolute-joint
  mechanics the box/valve pieces already validate (no new joint code
  needed — if new code turns out to be needed, that is itself a finding
  worth reporting back to `hexfold-integration.md`).

## Target + blast radius

`src/hexfold/` (the `.hx` piece definition); no new `precis_se` joint
machinery expected — reuses the box/valve revolute-joint pattern.

## Open questions / decisions log

- None yet — this item is new from the 2026-09-30 review; open questions
  will surface once building starts.

Closest existing items: `rotary-ratchet-valve.md` (the same revolute-joint
pattern, prior instance), `diamondoid-pattern-language.md` (shaft-in-sleeve
bearing, sp³ variant, the incommensurate-lattice question), `hexfold-integration.md`.
Thread: `docs/backlog/threads/hexfold-toolkit.md`.
