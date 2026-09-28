# se: mechanical DRC — a validator pass for missing fasteners and unconstrained joints

`unicycle-c1` in the `/se` 3D viewer shows a design with blocks that touch (`connects` edges between them) but no fasteners, welds, or glue declared at load-bearing interfaces. `se`'s `validate()` produces findings (envelope overlaps, no_tool_access, stability) but nothing that flags "this interface carries load and has no fastening declaration". PCB's DRC concept (clearance/short checks) has an architectural twin here: a new validator rule family that walks `connects`/joint edges and alerts when an interface with no fastening mechanism exists.

**Open design questions — do not answer, just sketch:**

(a) How does a designer declare "this interface is glued/welded/press-fit/bonded" so the rule does not false-positive on intentional adhesive/integral joints?

(b) Is the rule per-interface (check every connect) or per-load-path (only flag interfaces that carry actual reaction forces)?

(c) Does the rule need the reaction-force solver from `docs/backlog/se-interface-reaction-forces.md` to know which interfaces are load-bearing, or can it run standalone on all structural `connects` edges?

**Cross-link:** `se-interface-reaction-forces.md` is a sibling item from the same session (Reto, 2026-09-28); the reaction-force solver may be a prerequisite for load-path-aware rule tuning.

Owner `src/precis_se/validate.py`, `src/precis_se/joints.py`.
