---
status: draft
title: Intent-to-realize loop — declarative region/property statement → picked, joined, checked design
pillar: 3d-design
prio: high
blocked-by: se-region-property-layer
---

# Intent-to-realize loop — declarative region statement to realized design

Reto's stated loop (product-plan review, 2026-09-30): "here be pocket, this
side hydrophobic, then negative, then positive, 1 nm × 0.2 nm with shape X,
backbone on the back — then groups are picked and joined until things
work."

Mining pass, 2026-09-30: the pieces exist separately — top-down phases
(`cad-machine-spec.md`, its §1.6 top-down framing), `se-pick-hierarchy.md`
(pick → hierarchical reference), `pattern-groups.md` (symmetric repetition
as a tree node), `hexfold-seam-type-catalogue.md` (a closed catalogue of
joinable geometries), `complementarity-solver.md` — but no authoring
surface takes a declarative region/intent statement and drives the pick →
join → check loop to convergence.

## Motivation / why

Today's authoring path is op-by-op: an agent or Reto picks a group, places
it, checks it, and repeats by hand. Reto's stated loop describes the
inverse — state the *intent* (a region, its properties, a rough shape
envelope) and have the system search for and assemble candidates that
satisfy it, looping until checks pass. Nothing today closes that loop; each
piece is a manual step an agent performs, not a search the system runs.

## In scope

- **Intent statement** = a set of region specs, each using
  `se-region-property-layer.md`'s region selector grammar (blocked-by),
  each region carrying a defined class from
  `class-lattice-similarity-spaces-and-laws.md` (e.g. "hydrophobic",
  "negative charge" — named classes in that item's similarity-space sense,
  not free text).
- **Realization** = a membership query over candidate groups — the hexfold
  seam catalogue, substituent tiers (`materials-molecular-substitution-db.md`
  once it exists), the part library — filtered by per-axis nearest-
  neighbour match to the stated region properties.
- **Join**: the matched candidate is joined using the existing join/seam
  machinery (`hexfold-seam-type-catalogue.md`, `pattern-groups.md`).
- **Check, loop**: run the relevant checks (DRC, `se-mechanical-drc.md`
  once it exists, chain/atomic validators depending on design type); on
  failure, pick a different candidate or relax the region spec; repeat
  until checks pass or the loop reports it cannot satisfy the intent.
- **Provenance**: every pick records which candidate was chosen, from which
  library/tier, and against which region spec it was matched — so a
  realized design can answer "why is this group here" after the fact.

## Explicitly NOT in scope

- Defining the region-selector grammar (`se-region-property-layer.md`) or
  the similarity-space classes (`class-lattice-similarity-spaces-and-laws.md`)
  — both are hard blockers, owned elsewhere.
- `design-workbench-realize.md`'s scope — that item turns an *already-picked*
  candidate (from `se_propose_atomic`) into a bound structure; this item is
  the step before it, picking the candidate from a declarative intent
  rather than starting from one an agent already named.
- A full constraint solver that backtracks over the whole design — v1 is a
  greedy loop (pick, join, check, retry-on-failure), not global search.

## Acceptance criteria

- A region spec ("this face, hydrophobic, ~1nm x 0.2nm") resolves to a
  ranked list of candidate groups via per-axis nearest-neighbour match.
- The top candidate is joined and checked automatically; a failing check
  triggers a re-pick, not a silent placement.
- Every block placed by this loop carries provenance back to the region
  spec and candidate-library entry that produced it.
- The loop terminates (success or a named "cannot satisfy" report) — no
  infinite retry.

## Target + blast radius

New authoring surface in `precis_se` (op or workbench-level, TBD — depends
on how `design-workbench-realize.md` ships); consumes
`se-region-property-layer.md`, `class-lattice-similarity-spaces-and-laws.md`,
`hexfold-seam-type-catalogue.md`, `pattern-groups.md`.

## Open questions / decisions log

- Whether this is an MCP op (`intent_realize` or similar) or a workbench-
  chat-turn affordance (`design-workbench-realize.md`'s realize-in-the-loop
  slice) — undecided, depends on how much human-in-the-loop review each
  pick needs.

Closest existing items: `design-workbench-realize.md` (works from an
already-picked op, not a declarative intent — the nearest existing
surface this item extends backward from). Thread: `docs/backlog/threads/se-machine-design.md`.
