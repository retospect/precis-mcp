---
status: draft
title: "pcb: freezing must actually bind — mechanical features as well as parts"
prio: high
---

# pcb: a frozen hole is not frozen

## Motivation / why

Reto, 2026-09-30, asked directly for this: "All should be able to be frozen
— nuts, holes etc for sure. But also parts — a part may be tall and in the
corner because there is space there in assembly. But only actually freeze
when needed."

Half of that exists. `pcb_instances.fixed` is real: `NULL | 'xy' | 'rot' |
'both'` with a CHECK constraint, read by the placer, and the `.epro2` import
already carries a source board's own `COMPONENT.locked` into it.

The other half does not. `pcb_features.fixed` is an **unconstrained `text`
column that nothing reads** — a repo-wide grep finds no consumer, only the
column definition in `src/precis/migrations/0047_pcb_kind.sql`. So a
mounting hole, a fiducial or an outline can carry any string there and it
constrains nothing. The `.epro2` importer deliberately writes NO value
rather than a plausible one, because `fixed='true'` on a column nothing
enforces is worse than an empty column: it reads as a guarantee.

Today that gap is invisible, because nothing MOVES a feature either — no
placer, router or optimizer touches `pcb_features`. It becomes real the
moment anything does, and it is already wrong as an answer to "did my
freeze take effect?".

The second half of Reto's ruling is the part that is easy to get backwards:
**"only actually freeze when needed"**. Freezing is an explicit act, never a
default. A blanket freeze-on-import would make the board un-routable for the
wrong reason and would have to be undone before any work could start.

## In scope

- **Make `pcb_features.fixed` mean something.** Constrain it the way
  `pcb_instances.fixed` is constrained (a CHECK over a named vocabulary,
  forward-only migration — never edit the sealed 0047), and give it a
  reader: whatever can relocate a feature must refuse to move a frozen
  one, and `view='drc'` (or the always-valid-board invariant work) must
  report an attempt.
- **A surface to freeze and unfreeze**, per feature and per instance, since
  "only when needed" means the user does it deliberately after import
  rather than choosing a flag beforehand. The existing
  `pcb_move_instance(fixed=…)` sentinel (`_UNSET` vs `None` to distinguish
  "leave alone" from "clear the lock") is the precedent for the tri-state.
- **The `.epro2` import keeps carrying only the source's own lock state**
  and stops there — asserted, so a later "freeze the mechanicals"
  convenience cannot quietly become a default.
- Report what a freeze means where it is set: a frozen part communicates an
  assembly constraint (Reto's tall-part-in-the-corner), which is exactly
  the kind of fact `pcb-design-source-provenance.md` wants attributable.

## Explicitly NOT in scope

- Keepout ENFORCEMENT. `pcb-keepout-does-not-bind.md` owns that, and it is
  a different mechanism (an area that forbids other things) from a freeze
  (this object may not move). They are neighbours and will share the
  obstacle plumbing, but conflating them would make one item that ships
  neither.
- A general constraint solver, or expressing *why* a part is frozen as
  anything richer than a note.
- Freezing nets, copper or the stackup.

## Acceptance criteria

1. `pcb_features.fixed` has a CHECK constraint and at least one consumer
   that refuses a move; a test asserts the refusal, not just the column.
2. An authored freeze survives a re-`put` of the same design (the
   re-runnable contract) and an explicit unfreeze clears it.
3. An `.epro2` import of a board with no locked parts produces a design
   with **nothing** frozen; one with locked parts freezes exactly those.
   Both asserted.
4. A frozen part and a frozen hole both appear in whichever view answers
   "what is frozen on this board?" — a user cannot honour a constraint they
   cannot see.

## Target + blast radius

`src/precis/migrations/` (new, forward-only) · `src/precis/store/_pcb_ops.py`
(`pcb_features_list`, the feature write path, `pcb_move_instance`) ·
`src/precis/pcb/drc.py` or the placer's legality check (the consumer) ·
`src/precis/handlers/pcb.py` (the freeze/unfreeze surface and the view) ·
`src/precis/ingest/pcb_epro.py` (assert it stays source-only).

## Open questions / decisions log

- **Decided (Reto, 2026-09-30):** both features and parts must be
  freezable; freezing happens only when needed, never as an import
  default.
- **Open:** what the feature vocabulary should be. `pcb_instances` uses
  `xy|rot|both`, but a hole has no meaningful rotation, so a plain boolean
  may be right for features and the two columns may simply not match. Pick
  one deliberately rather than mirroring `pcb_instances` for symmetry's
  sake.
- **Open:** whether an imported outline counts as frozen by default. It is
  the one feature where "the user did not choose this, the file did" and
  changing it silently would reshape the board.
