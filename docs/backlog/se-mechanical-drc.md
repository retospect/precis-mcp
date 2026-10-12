---
status: draft
pillar: 3d-design
---

# se: mechanical DRC — what is left after fastener_insertion_path

`fastener_insertion_path` (can each fastener REACH its seat, and its tool
and hand operate, in the fully assembled state) and its viewer render (the
3D page's "tool volumes" toggle, `/se/<slug>/tool-volumes.json`) are
built; their contract lives in `src/precis_se/toolaccess.py`'s module
docstring and `precis_web.blocktree_3d.tool_volume_group`. Three things
remain, none buildable without a decision or a prerequisite.

## Rulings that still bind the rest (Reto, 2026-09-29)

- **Final state first.** "All bolts accessible all the time" is a design
  property; a final-state failure is the design losing it, not a false
  positive. Declared install-order boundaries are phase 2, an escape hatch
  with a stated cost. Automatic assembly-sequence search is out of scope.
- **Insertion is a straight axial sweep.** Tilt-and-align or multi-step
  insertion is motion planning, out of scope.
- **Tool scope is additive.** The hand is modelled for screwdrivers;
  hex keys and the rest keep the no-hand treatment until their own item
  (`se-tool-sector-and-lkey-access.md`).

## Open: a fastener blocked only by another fastener (2026-10-03)

`fastener_insertion_path` checks the final state, so a screw whose path
crosses only another fastener reports an error even when inserting it
first would work. First case: unicycle-c1's `flange_bolt_left` is blocked
by `seatpost_clamp_bolt`; with the pinch bolt out it goes in. Needs Reto:
should a blocker that is itself a fastener become an install-order finding
(phase 2's declared boundaries) or stay an error under "all bolts
accessible all the time"?

## Phase 2: declared install-order boundaries, load-path tuning

Waits on `se-interface-reaction-forces.md`, which says which interfaces
carry load (the `se-3d-viewer` thread's ordering).

## Open design questions for the original "missing fastener" rule

The rule that flags a load-bearing interface with no fastening declared is
not built. Undecided:

(a) How does a designer declare "this interface is glued/welded/press-fit/
bonded" so the rule does not false-positive on intentional adhesive or
integral joints?

(b) Per interface (every structural connect) or per load path (only
interfaces that carry reaction forces — which also waits on
`se-interface-reaction-forces.md`)?
