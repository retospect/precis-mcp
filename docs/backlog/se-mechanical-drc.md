# se: mechanical DRC — a validator pass for missing fasteners and unconstrained joints

`unicycle-c1` in the `/se` 3D viewer shows a design with blocks that touch (`connects` edges between them) but no fasteners, welds, or glue declared at load-bearing interfaces. `se`'s `validate()` produces findings (envelope overlaps, no_tool_access, stability) but nothing that flags "this interface carries load and has no fastening declaration". PCB's DRC concept (clearance/short checks) has an architectural twin here: a new validator rule family that walks `connects`/joint edges and alerts when an interface with no fastening mechanism exists.

**Open design questions — do not answer, just sketch:**

(a) How does a designer declare "this interface is glued/welded/press-fit/bonded" so the rule does not false-positive on intentional adhesive/integral joints?

(b) Is the rule per-interface (check every connect) or per-load-path (only flag interfaces that carry actual reaction forces)?

(c) Does the rule need the reaction-force solver from `docs/backlog/se-interface-reaction-forces.md` to know which interfaces are load-bearing, or can it run standalone on all structural `connects` edges?

**Cross-link:** `se-interface-reaction-forces.md` is a sibling item from the same session (Reto, 2026-09-28); the reaction-force solver may be a prerequisite for load-path-aware rule tuning.

## Rulings (Reto, 2026-09-29)

**Ruling 1 — final-state accessibility is a design property, not an approximation.**
The first rule to build is a `fastener_insertion_path` check evaluated against the FULLY ASSEMBLED state: can each fastener reach its seat, and can its tool operate, with every other block present. This inverts the usual reading: "all bolts accessible all the time" is a desirable property of a design in its own right. A final-state failure is therefore NOT a false positive to be tuned away — it is the design reporting that it has lost that property. Only when final-state access is genuinely impossible does the design drop to declared subassembly/install-order boundaries, and that drop is a real, stated cost. Final-state access also implies subassembly installability by necessity, so it is the strictly stronger check.
Build order consequence: final-state-only FIRST. Declared install-order boundaries are phase 2, an escape hatch with an explicit cost. Automatic assembly-sequence derivation (searching for any feasible order) remains out of scope and is much larger than both.

**Ruling 2 — one combined hand+tool volume per tool class.**
Model a single swept solid per tool/tool class that INCLUDES the hand, rather than a tool envelope plus a separate hand torus. Shape is an extrusion-like body of revolution, roughly round. Scope for now is screwdrivers and electric screwdrivers only; wrenches and L-keys are not modelled yet but the same per-tool-class volume model is intended to accommodate them later.
Note the consequence explicitly: because a screwdriver is spun in place, its swept volume is already a body of revolution about the screw axis, so the "full 360° sweep versus ratchet sector arc" question does NOT arise for coaxial tools. That distinction only returns for L-keys and wrenches.

**Ruling 3 — this reverses a documented non-goal.**
`src/precis_se/toolaccess.py`'s module docstring currently states, as a deliberate exclusion: "Deliberately not modelled: the hand holding the tool; approach at an angle (every tool here is coaxial with the screw); ratchet arc, i.e. a handle that only needs a *sector* rather than a full circle." Adding a hand envelope overturns the first of those. Flag that the docstring MUST be rewritten when this is built, so the code does not quietly diverge from its own stated scope. Angle-of-approach and ratchet arc remain excluded.

**Ruling 4 — insertion motion is a straight axial sweep.**
The bolt's insertion path is swept along the screw axis only. Tilt-and-align or multi-step insertion is motion planning and is explicitly out of scope.

**Ruling 5 — findings must be visually explainable.**
Tools are not rendered in the viewer, so today a `no_tool_access` or insertion-path finding is unexplainable — the user is told "no" with no way to see what blocked it. The swept tool/hand volume for a failing finding must be renderable on demand in the 3D viewer. This is a required hook to design in from the start, not a retrofit. Cross-link `docs/backlog/se-3d-viewer-ux-batch.md`.

The motivating case: in `unicycle-c1`, blocks `flange_bolt_left` and `flange_bolt_right` sit inside the `crown` block with no physical way to install them. Today nothing catches this — `toolaccess.access()` checks only whether a driver can TURN an already-seated screw, never whether the fastener can REACH its seat. `src/precis_se/fasten.py`'s docstring already names the gap in its deferred list: "assembly-order existence — including whether a nut trap can be *reached*".

Owner `src/precis_se/validate.py`, `src/precis_se/joints.py`.
