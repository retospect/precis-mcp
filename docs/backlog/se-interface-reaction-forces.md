---
status: draft
title: se — per-interface reaction forces from a declared load case
pillar: 3d-design
---

# se — per-interface reaction forces from a declared load case

## Motivation / why

`multiscale-design-system-spec.md` §1.2 specifies an `Interface` carrying a
`Contract` whose first field is `loads{Fx,Fy,Fz,Mx,My,Mz}` with all six
components — "absent = unknown, never zero" — and a `load_provenance` enum
covering `user_stated`, `llm_assumed`, and `derived_by_statics`. §2.1's
Phase-1 constraint table makes "No unreacted load anywhere" a HARD constraint:
"Every block's six load components resolve; ground contact is one reaction
case". **Neither the six-component contract nor the statics that propagates it
are built today.**

`unicycle-c1`, the live design in `view='stability'`, declares a complete load
case: `saddle` carries `force: [0, 0, -1800]`, `pedal_left` carries
`force: [0, 0, -900]`, and `wheel` carries `fixed: ["x","y","z"]` as the
ground support. The viewer shows "no axial members — stability analysis does
not apply" and notes that the declared loads sit outside the analysed subgraph.
Every connect in the design uses a non-axial joint class, so the only solver
that exists declines the whole design. **The declared loads go nowhere.**

Reto, reading `unicycle-c1` in the 3D viewer (2026-09-28): "I cannot see the
forces on the interfaces. I hope to click on the saddle and get something like
−500 N to +2000 N vertical, 500 N left/right and front/back, and radial
clockwise/counterclockwise." In other words: select a block, see the six-component
load (Fx/Fy/Fz/Mx/My/Mz) its interfaces carry, from a declared load case.

## In scope

Rigid-body quasi-static equilibrium solve over the whole connect graph, not
just the axial subset. Assemble 6-DOF equilibrium at each block, apply
declared external loads and `fixed` supports, constrain per joint class, solve
for reactions at each interface.

1. **DOF-reduction mapping per joint class.** `src/precis_se/joints.py` already
   names which DOF each joint kind constrains (rigid = 6, revolute = 1 free
   DOF, prismatic = 1 free DOF, etc.). Formalize this as a mapping from joint
   class to the rows/columns of the 6-DOF equilibrium matrix that remain free
   vs. constrained.

2. **Formal load-case concept.** Today loads are a flat `objectives` dict on a
   block. Allow more than one scenario to be analysed independently: a `load_case`
   entity naming the external load set, so the same design can solve under
   "saddle only" vs. "pedal + saddle" vs. "dynamic braking" without re-editing
   the design itself.

3. **Equilibrium assembly and solution.** Reuse the existing seams in
   `stability.py` (equilibrium-matrix assembly, fixed-DOF masking) to scale
   from the axial subgraph to the full connect graph. Solve for interface
   reactions Fx/Fy/Fz/Mx/My/Mz at each connect.

4. **Viewer surface.** `_se_member_facts` in `blocktree_view.py::_se_member_facts`
   is the existing seam that carries per-connect facts to the reader's topology
   panel, keyed by connect subject. Route reaction numbers there so they are
   visible on block selection in the 3D pane.

## Explicitly NOT in scope

- **FEA or stress distribution.** This is rigid-body statics only, not
  continuum mechanics.
- **Fatigue, margin analysis, or structural adequacy.** Those belong in later
  phases and are different items.
- **Resolving statically indeterminate designs.** When rigid-body equilibrium
  alone does not give a unique answer, the solver returns this fact (e.g.
  "reactions under-determined, declare additional constraints or a compliance
  model"). This item does not build the modal/FEA layer that would resolve
  indeterminacy.
- **Dynamic loads or time-domain analysis.** Quasi-static only: loads are
  steady-state.

## Acceptance criteria

- A design with declared external loads and `fixed` supports can be solved for
  interface reactions without manually computing separations.
- Selecting a block in the 3D viewer displays Fx/Fy/Fz/Mx/My/Mz at each of
  its connected interfaces, sourced from the equilibrium solve.
- `unicycle-c1` solves cleanly (17 connects, all non-axial joint classes, known
  load case) and reports reaction magnitudes within the viewer.
- A design with an over-constrained or under-constrained load case is rejected
  with a structured reason (e.g. "three `fixed` supports in a line — reactions
  under-determined").
- Reactions carry `load_provenance: 'derived_by_statics'` in the contract so
  they are visibly distinct from user-stated loads.

## Target + blast radius

`src/precis_se/stability.py` (extend the existing equilibrium assembly to full
6-DOF), `src/precis_se/joints.py` (formalize the DOF-reduction mapping),
`src/precis_se/ops.py` (add the load-case entity and its op surface),
`src/precis_se/handler.py` (route reactions into the view),
`src/precis_web/routes/blocktree_view.py::_se_member_facts` (surface the
reactions in the topology panel), and the `precis-se-help` skill's op/view
rosters.

## Open questions / decisions log

- **Statically indeterminate systems.** A frame with more constraints than DOF
  has multiple valid equilibrium solutions; rigid-body statics cannot choose
  between them. Decide on the reporting posture: (a) reject indeterminate
  designs outright; (b) report the solution space as a range per component
  (e.g. "Fx ∈ [−500, +500]"); (c) add a compliance/stiffness model at
  interfaces so the solver can pick one. Current lean: (b) — report the
  indeterminacy as a structured reason and let a higher-fidelity phase
  (FEA) decide if needed.
- **Load-case hierarchy and defaults.** Should a design carry one canonical
  load case or multiple named scenarios? If multiple, is there a "default" for
  the viewer to show, or must the user pick? Current lean: multiple named
  scenarios, viewer defaults to the most-recently-solved.
- **Reaction sign convention.** At interface block_a–block_b, should the
  reaction on block_a be "force exerted by block_b on block_a" (Newton's third
  law pair of the interface force) or "force carried by the interface as seen
  from block_a" (load inside the connection)? Block-frame or world-frame?
  Current lean: world frame, Newton's third law pair, so user sees the
  force *on* the block, not *from* it.
