---
status: draft
title: "workspace scripts: parameterize — lift a literal into a named variable from a trace frame"
pillar: 3d-design
---

# workspace scripts: parameterize

Source gripes: gr521589 (build it, Reto 2026-10-28), gr521588 (the wider
output→code ask, ruled out — decisions log).

## Motivation / why

Gonzalez UIST'24 (pa521194, pc5380407) lets a user select elements in the
view plus a geometric property, then refactors the code to introduce a
variable that keeps the relationships — found geometric properties are
mainly linear combinations of variables. Gonzalez SUI'23 (pa521209,
pc5382550/pc5382624) and Sketch-n-Sketch + Chugh (pa266428 pc3368764,
pa266331 pc3377237) go further: direct manipulation of the rendered model
updates the code coherently.

Ours: a literal in a workspace script stays a literal; nothing on the
agent surface proposes "introduce `H = 12` at the def, replace the
literal at `line_in_def`" — though the frame's `args` digest already
names the literal and `src`/`line_in_def` locate it (gr521589). Both nav
directions exist — ray hits name `frame:<key>` + `file:line`, `frame:`
selectors reach back to geometry — but edits go one way (gr521588).

## In scope

- A `parameterize` ask that, given a frame + arg name, returns the
  parameter-introducing rewrite: introduce a variable at the def and
  replace the literal at `line_in_def` (ruled by Reto 2026-10-28) — the
  smallest step toward live parametric edits.

## Explicitly NOT in scope

- Output→code edits: an affordance that takes a delta ("move instance X
  2 mm +z", "set this measure to 4 mm") and returns the minimal script
  edit (gr521588) — not approved for now, re-ask Reto before building.
- Applying the rewrite — the script stays the source of truth and the
  next run overwrites; whether `parameterize` only proposes or also
  patches is a separate decision.
- Identity-targeted edit steps on a stored design —
  [local-edits-processed-designs](local-edits-processed-designs.md) is a
  different surface.

## Acceptance criteria

- Given a frame and an arg name, `parameterize` returns a rewrite that
  introduces a named variable at the frame's def and replaces the arg's
  literal at `src`/`line_in_def`; re-running the script with the rewrite
  applied yields identical geometry.
- The rewrite names the frame it was derived from and touches only the
  one literal.
- TBD: acceptance for the measure-on-geometry input form, if it is in v1
  (open question below).

## Target + blast radius

Workspace trace surface (`frame:` selectors, the `args` digest,
`src`/`line_in_def`) and whichever kind hosts the ask — see open
questions; script-side rewrite proposals, no op on designs.

## Open questions / decisions log

- **[decided, Reto 2026-10-28 via GRIPE_FIX]** Build `parameterize`:
  given a frame + arg name, return the rewrite that introduces a
  variable at the def and replaces the literal at `line_in_def`.
- **[decided, Reto 2026-10-28]** gr521588's output→code edit path is
  not approved for now; re-ask Reto before building or closing it.
- Open: does v1 include the measure-on-geometry input form, or only
  frame + arg name? The ruling names only the latter.
- Open: where the ask surfaces — a verb/view on which kind (workspace,
  se, python frame)?
- Open: what `parameterize` returns — a diff, a rewritten def, or an
  applicable edit record.
