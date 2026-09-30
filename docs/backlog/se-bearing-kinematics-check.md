---
status: idea
title: A check family for revolute/prismatic joints — sweep interference, friction/drag vs. torque, ratchet directionality
---

# A check family for revolute/prismatic joints

Pillar: 3d-design

What: motion checking stops at joint *enumeration* today —
`src/precis/handlers/cad.py::_joint_kind_map` names a joint's kind, and
`cad-machine-spec.md` records the joint vocabulary, but nothing sweeps a
declared rotation for interference, budgets friction/drag against
available torque, or checks ratchet directionality. The one existing
instance is a paragraph of prose in `rotary-ratchet-valve.md` (drag adds
linearly across ganged wheels, torque stays fixed — a real calculation, but
done by hand in a spec doc, not a runnable check).

Why: every bearing-shaped design (the rotary ratchet valve, the planned
`hexfold-t-handle-bearing.md`) currently ships with its kinematics argued
in prose rather than checked. A check family on revolute/prismatic joints —
sweep the declared motion range for envelope interference, compare drag
torque to available motor torque with margin, verify ratchet pawls only
admit one direction — turns that prose into a DRC-style pass/fail the way
`se-mechanical-drc.md` does for fastener access.

Note: `se-3d-viewer`'s DRC ruling 4 (`docs/backlog/threads/se-3d-viewer.md`)
scopes *motion planning* out of DRC. This item stays inside that boundary —
it is a static check against a declared motion range, not a planner that
searches for one.

Owner anchor: `src/precis_se/validate.py` / `src/precis_se/joints.py`
(same owner as `se-mechanical-drc.md`, a sibling check family);
`src/precis/handlers/cad.py::_joint_kind_map` (the joint enumeration this
extends).

test: first consumer is `hexfold-t-handle-bearing.md` — a T-handle sweep
that interferes with its race fails the check; one that clears passes.

Closest existing items: `se-mechanical-drc.md` (same owner, same DRC-pass
shape, different fault class), `cad-machine-spec.md` (the joint
vocabulary this checks against), `structural-solution-space.md` (its
"Deliberately deferred" section defers dynamics/vibration and belt/pulley
couplings — adjacent, not the same check, no "slice 5" exists in that file
as of 2026-09-30; corrected here from the brief).
