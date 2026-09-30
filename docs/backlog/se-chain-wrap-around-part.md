---
status: draft
title: wrap_chain — derive a helix path from a carbon part's envelope instead of an arbitrary waypoint path
prio: high
blocked-by: hexfold-integration
---

# wrap_chain — a helix path derived from a part, not authored freehand

Pillar: 3d-design

Evidence: peer session DNA-SE, 2026-09-30. A helix today can be authored on
any waypoint path an agent supplies, but nothing *derives* that path from a
carbon part — wrapping a strand around a hexfold bud or a nanotube is manual
waypoint authoring. `relax_chain`'s excluded-volume term is helix-vs-helix
only (`src/precis_se/chain/relax.py`'s bodies are `layout_chain` children,
plus a narrow `walker` exception for one state-owning non-chain block — not
a general check against arbitrary non-chain geometry), so a settled scaffold
passes straight through a hexfold part with no finding. The only existing
chain-to-part tie is a strand's 5' tether to a block port
(`src/precis_se/ops.py` domain/tether machinery) — a point connection, not a
surface-following one.

## Motivation / why

Wrapping DNA/RNA around a rigid nanostructure (a nanotube, a hexfold bud) is
a real design pattern (`se-nucleic-chain` thread, the DNA-SE peer session's
working case), and today it requires hand-computing waypoints that
approximate the part's surface — no check that the resulting helix actually
clears the part it's meant to wrap, and no update if the part moves.

## In scope

- **`wrap_chain`**: an op that derives a helix's waypoint path as an offset
  curve around a block's envelope or atom hull, instead of taking waypoints
  directly.
- **`relax_chain` excluded volume widened** to check chain bodies against
  non-chain blocks the chain is meant to wrap (not just other chain bodies),
  using the same excluded-volume kernel already in place for helix-vs-helix.
- **Anchor domains on the part's surface sites** — using the region
  selectors `se-region-property-layer.md` specs (that item's slug is
  referenced here as a dependency; this item does not define region
  selectors itself).

## Explicitly NOT in scope

- Defining the region-selector grammar itself — owned by
  `se-region-property-layer.md`.
- A physics-grade "does it actually hold" check — no physics rung exists
  for chains at all yet (oxDNA is not in the image;
  `nanostructure-check-tiers.md` §"Chains: the physics tier is oxDNA" tracks
  that separately). This item's acceptance criteria are geometric
  (excluded-volume clearance, offset-curve fidelity to the part surface),
  not energetic.
- Wrapping a part whose atom hull is not se-visible — blocked entirely on
  `hexfold-integration.md` making a carbon part's atom hull an se-readable
  surface; this item cannot start before that lands.

## Acceptance criteria

- `wrap_chain` on a block with a declared envelope or atom hull produces a
  helix path that follows the part's surface within a stated tolerance.
- A wrapped helix, after `relax_chain`, reports an excluded-volume finding
  if it settles through the wrapped part — not silently, the way an
  unrelated hexfold part passes through today.
- "Holds the shape" is defined geometrically for this item: the settled
  excluded-volume clearance against the wrapped part meets the same
  `min_gap` discipline `relax_chain` already applies helix-to-helix — no
  claim about thermodynamic stability, which stays unanswerable until the
  oxDNA rung exists.

## Target + blast radius

`src/precis_se/chain/relax.py` (excluded-volume widening), a new op
(`wrap_chain`, likely `src/precis_se/chain/` alongside `layout_chain`);
depends on `hexfold-integration.md`'s atom-hull-as-surface work and
`se-region-property-layer.md`'s selector grammar.

## Open questions / decisions log

- Whether the offset curve is computed against the envelope (cheap, coarse)
  or the atom hull (expensive, exact) by default — likely envelope-first
  with an atom-hull opt-in, mirroring the layout_chain/relax_chain split
  between cheap layout and expensive settle.

Closest existing items: `se-region-property-layer.md` (the selector
grammar this item consumes), `hexfold-integration.md` (hard blocker),
`nanostructure-check-tiers.md` (owns the missing physics rung this item
explicitly does not attempt). Thread: `docs/backlog/threads/se-nucleic-chain.md`.
