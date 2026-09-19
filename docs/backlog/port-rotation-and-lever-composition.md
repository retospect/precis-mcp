---
status: draft
title: ports carry a measured rotation; transitions derive a swing; the proposer composes levers
prio: high
model: opus
---

# Ports carry a measured rotation; transitions derive a swing; the proposer composes levers

Written 2026-09-18 after `port-pose-and-composition-search.md` shipped its
three decisions and blocktree slices 1–6 landed. That item left one line
open: "lever/hinge families once ports carry rotation (the pose slot's
`rot` is declared but no composition uses it)". This is that item. Three
slices, each shippable alone, in order.

## Motivation / why

The multiscale spec's mechanical principle (addendum A, "levers / cranks
/ scissors") is that a rotation at a hinge port times an arm length is a
displacement at the far end — se-plugin migration `0011` said so when it
added `pose_rot`. Today that slot is a declared-only field: `set_port_pose`
writes it, per-state `port_pose_overrides` compose a delta onto it, the
cad kernel swings a revolute joint about it (`tests/test_cad_examples.py`
piano hinge), but `bind_structure` never measures it ("an atom has a
position, not an orientation", `atomic/bind.py`), no read derives an angle
from a transition, and the composition proposer knows only the linear
switch + spacer chain. So a molecular rotary motor (pa345694, Feringa
3 MHz; pa345695, the Pb(111) gear train) or a printed hinge can be
declared but its rotation is never checked against its realization and
never enters a library search.

## In scope

### Slice R1 — measure the port frame from the structure

`bind_structure`'s `ports=` mapping keeps its string form (`{port:
'C7'}`, the bound atom) and gains an object form per port: `{port:
{'atom': 'C7', 'axis_atom': 'C8', 'phase_atom': 'C12'}}` — `atom`
required, the other two optional together (both or neither; one alone
is a `BadInput` at op time). With `atom` alone the bind measures `pose`
as today. With all three it also measures the port's full **`rot`**: the
frame whose z is the unit vector `atom → axis_atom` in the block frame
(an axle bond: stator atom → rotor atom) and whose x is the projection
of `atom → phase_atom` onto the plane normal to it (a rotor substituent
fixes the roll). There is NO direction-only measurement: `direction`
stays a declared field with no provenance (it never had one), and a
measured frame is compared to it instead — the frame's z against the
declared direction. Same rules as the pose half of Decision 1, restated
so they cannot drift:

- `pose_source='bound'` fills an empty slot or refreshes an earlier bound
  reading; a `declared` `rot` is never overwritten — it is the
  requirement the realization is checked against. Disagreement past a
  tolerance is reported twice: the bind echo and a standing
  `port_rot_mismatch` warn finding. Tolerance: `PORT_ROT_MISMATCH_RAD`
  = 0.175 (10°) on the axis-angle between declared and measured frames;
  the measured z against a declared `direction` uses the same constant
  on the angle between the two vectors and the same finding.
- A frame mismatch (`envelope_fit`'s `FrameMismatch`) measures nothing.
- `axis_atom == atom`, or `phase_atom` collinear with the axis
  (|sin| < 1e-3), is a `BadInput` naming the atoms — a degenerate frame
  is not a measurement. All three labels must exist in the structure
  scene already loaded for the pose measurement (`store.structure_load`
  once; no second query).
- `unbind_structure` and a re-target drop measured rots and keep
  declared ones, as they do for poses.

Storage: `se_ports` gains `axis_atom text`, `phase_atom text` and
`rot_source text` (se-plugin migration `0014` — `0011` is already
doubled by two sealed files, so check every worktree, not the sequence),
all nullable. `rot_source` (`declared` | `bound` | NULL) is rot's OWN
provenance, independent of `pose_source`: the review of the first build
found that one stamp cannot express "pose declared, rot measured", which
is exactly the `add_port(pose=…)` then `bind_structure` case, and the
measurement was being dropped silently. `add_port(rot=)`/`set_port_pose
(rot=)` write `declared`; the bind writes `bound` when rot is empty or
already bound; unbind drops only a bound rot. The `## ports` render
shows `axis_atom`/`phase_atom`/`rot_source` when set.

### Slice R2 — derive the swing of every transition

Nothing new is *declared* for an angle: two declared states already carry
the port's frame in each (`port_pose_overrides[port].rot`, composed onto
the port's own `rot`), so the rotation a transition performs on a port is
`R_to · R_from⁻¹`, read as axis-angle through a new
`precis.cad.vec.axis_angle_from_matrix` (angle < 1e-6 → no rotation,
axis undefined; angle within 1e-6 of π → axis from the dominant
eigenvector of R + I, not the antisymmetric shortcut). **Precondition:**
the port carries a pose — `_apply_port_delta` drops every override on a
pose-less port. R2 therefore adds a `port_override_unapplied` warn
finding to `view='validate'` (a state's `rot`/`pose` override names a
port with no pose: "declare `set_port_pose` first"), and the kinematics
row for such a port prints `no pose` rather than `—`, so an undeclared
pivot is never mistaken for a non-rotating port. New read
`view='kinematics'` on `get(kind='se')`: one table row per (block,
transition, port-with-a-pose) — `axis` (block frame, unit vector, 3
decimals), `angle` (degrees, signed by the right-hand rule about that
axis), `arm (envelope)` (the distance from the port origin to the block
envelope's farthest extent along the plane normal to the axis — a
geometric upper bound on the lever arm the block itself offers), and the
tip displacement `2·arm·sin(angle/2)`. Ports whose frame does not change
between the two states print `—` for angle; a block with no declared
states prints one line saying so. Where a connect carries a `revolute`
joint (`joints.py`, axis in the WORLD frame), the derived port axis is
first taken to world through the block's placement (the same
`cad_pose(node.pose, node.rot)` chain the geometry checks use, parents
included) and then compared; disagreement past `PORT_ROT_MISMATCH_RAD`
is a `revolute_axis_mismatch` warn finding in `view='drc'` (the joint
names the axis, the port carries the rotation — both must agree). Rates,
barriers and step counts stay sourced facts on what the block is
realized-by (Decision 2): proposed-tier properties `step_angle` (rad),
`rotation_rate` (Hz), `rotation_barrier` (eV) resolve through the star
schema like `delta_length` does, and the kinematics table shows the
sourced `step_angle` beside the derived angle when both exist, flagging
a disagreement past 10 %.

### Slice R3 — lever composition

`compose=` learns a second family beside the linear chain. A **rotary
unit** is a library block with a transition whose derived swing on some
port is non-zero (R2, computed from the candidate's already-cached
`SeTree` + `design_states` rows in `library._ReadCache` — no new store
read; when several ports swing, the port with the largest angle is the
rotary port and the row names it) or, failing a derived swing, a sourced
`step_angle`; an **arm** is a spacer (has `unit_length`, no delta). A
lever composition = one rotary unit + `k` arm units attached at the
rotating port; its stroke at the tip is `2·(arm₀ + k·unit_length)·
sin(angle/2)` where `arm₀` is the rotary block's own `arm (envelope)`
from R2. The box gains `swing: [lo, hi]` (degrees) as a third box key
beside `delta`/`span` (`_ALLOWED_KEYS`, `parse_requires`, and the "needs
at least one of" refusal all grow it): with `delta`, the enumerator ranks
levers by tip stroke next to linear chains (same
`_match_value_row`/`order_rows` scoring, `k ≤ m_max`); with `swing`, it
ranks rotary units and series of them (a gear train transmits, a motor
series stacks — `n ≤ n_max`, angles summed) by total angle. Every lever
row surfaces the arm length, the angle it used and where it came from
(derived vs sourced), the PSS / bistability verdicts exactly as the
linear rows do, and the `connect` ops script attaching the arms at the
rotating port. `compose='<slug>#<block>'` reads `swing` off a
transition's `requires` like `delta`.

## Explicitly NOT in scope

- Per-state `bind_structure` (one structure per state, measuring the
  step angle from realizations): Decision 1 excluded it and this keeps
  it out; the swing is derived from *declared* states, measured frames
  check the current realization only.
- Torque, stiffness or load transmission through a lever; any dynamics.
- A joining-name → RXNO map or any chemistry of the axle.
- Kinematic loops (a four-bar); R3 composes open chains only.
- The motor papers' numbers: pa345694's held chunks are the SI; the main
  text (3 MHz, step angles, barriers) needs a manual PDF drop before any
  real `step_angle` row can be sourced. The slices do not wait for it —
  the mechanics are testable with a declared two-state hinge.

## Acceptance criteria

- R1: a block bound to a structure with `bound_atom`, `axis_atom`,
  `phase_atom` renders a `bound` pose+rot whose axis matches the atoms;
  a declared rot that disagrees by > 10° yields `port_rot_mismatch` in
  drc and the bind echo; degenerate atoms are refused at op time;
  unbind drops the measured rot and keeps a declared one.
- R2: a two-state block whose port rot override differs by 90° about z
  prints `axis 0 0 1 · angle 90°` in `view='kinematics'` with the tip
  displacement from its envelope; a revolute joint declared with axis x
  on that connect yields `revolute_axis_mismatch`.
- R3: a library with one 90° hinge block and one 10 nm spacer answers
  `compose={'delta': [12, 16]}` with a lever row `hinge + 1 × spacer`
  whose tip stroke is `2·(arm₀ + 10 nm)·sin(45°)`, ranked among linear
  rows; `compose={'swing': [170, 190]}` over two 90° units returns the
  series row at 180°; a rotary unit with a sourced `step_angle` that
  disagrees with its derived angle shows both.

## Target + blast radius

`src/precis_se/atomic/bind.py` (R1 measurement), `src/precis_se/ops.py`
(`bind_structure` mapping keys), se-plugin migration (R1 columns),
`src/precis_se/handler.py` (`view='kinematics'`, drc finding wiring),
`src/precis_se/joints.py` (axis comparison), `src/precis_se/compose.py`
(R3 family), skills `precis-se-help` + `precis-se-atomic-help`. Web
viewer untouched. No worker.

## Open questions / decisions log

- Where does the *rotating* side live — port or joint? Decided: the
  joint names the axis and class (connection intent, on the connect, in
  the world frame); the port carries the frame and therefore the
  rotation (the attachment point, in the block frame); R2 transforms and
  checks they agree. Not a new slot on joints.
- 2026-09-18 readiness pass, four blockers, all folded in above: the
  `ports=` wire shape (string form kept, object form added); no
  direction-only measurement (a frame needs both atoms; `direction`
  stays declared-only and is checked against the measured z);
  joint axis is world-frame, port axis block-frame — transform first;
  `_apply_port_delta` no-ops on a pose-less port — precondition made a
  validate finding and a distinct `no pose` cell.
- 2026-09-18 R1 review: `pose_source` alone lost a measured rot on a
  declared-pose port; `rot_source` added to migration 0014 (above). Advisories folded:
  `axis_angle_from_matrix` with both degenerate cases; rotary units read
  the cached tree; `swing` joins `_ALLOWED_KEYS`; migration `0014`
  (`0011` is already doubled); the bind tests need a 3-labelled-atom
  scene, extendable from the 2-atom fixtures.
