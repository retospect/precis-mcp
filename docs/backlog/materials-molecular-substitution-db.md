---
status: idea
title: Per-axis similarity + substitution-estimate database for materials and molecules
pillar: 3d-design
---

# Per-axis similarity + substitution-estimate database for materials and molecules

Reto's design notes, 2026-09-30 (transferred from another assistant; this
file is now the source of truth, not that transcript).

**What.** A query surface that asks near on one axis and far on another —
"similar size, different magnetism" — over crystals and molecules alike.

- **Per-axis similarity**: geometric, electronic, magnetic, thermal,
  mechanical, structural — separate spaces, not one blended distance.
- **Crystals** split topology from decoration: a `prototype` (structure
  topology) plus `occupancy` (site decoration), with a substitution
  distance defined per site.
- **Molecules** split scaffold from substituents: matched molecular pairs
  (MMP) mined for transform deltas — the standard cheminformatics move,
  generalized to materials via the prototype/occupancy split above.
- **Substitution estimates in tiers**, cheapest-first: bioisostere lookup
  tables → Hammett/steric parameter regression → DFT, only when the cheaper
  tiers don't resolve it.
- **Screening order**: pre-screen by fit (cheap geometric/electronic
  compatibility), then score evidence strength / predicted effect /
  synthesis risk *separately* (not blended into one scalar), weighted
  toward expected information gain, with negative controls in the training
  set so the scorer can't just learn to predict "yes."
- **Reactions**: RXNO reaction classification + SMARTS/atom-mapped rewrite
  rules — the same machinery `reaction-kind-and-synthesis-cost.md` already
  specs for the `reaction` kind, reused rather than re-invented.
- **Makeability**: retrosynthesis via ASKCOS / AiZynthFinder — both already
  shipped dark behind `PRECIS_CHEM_ENABLED` per `chem-tools-integration.md`
  — plus a check that the substituted group actually survives the proposed
  route (a route that clobbers the functional group it was meant to install
  is not a valid answer).

**Tables sketched** (names, not a committed schema): `structure`,
`prototype`, `occupancy`, `property` (same measure discipline as
`measures-substrate.md` — one row per subject/measurand/condition, not a
bespoke column per property), `transform`, `transform_delta`,
`substitution_estimate`, `reaction_rule`, `route`, `route_step`. Indexing:
`btree_gist` for range/condition queries, the RDKit Postgres cartridge for
substructure search, pgvector with one embedding row per (entity, named
similarity space) — not one shared embedding, since the whole point is
per-axis distance.

**Parked for discussion, not decided here:**
- How this joins the paper graph (is a `substitution_estimate` a `measures`
  row with an `experiment`/paper source, or its own thing with weaker
  evidence discipline — the tiered-estimate nature argues it needs its own
  `tier` semantics, not a forced fit into `measures-substrate.md`'s
  measured/computed/derived/asserted ladder).
- The per-axis query surface itself — API shape, how "near on axis A, far
  on axis B" composes as a single call.

Owner anchor: none yet — this is upstream of any code. The similarity-space
*definitions* this item needs belong to `class-lattice-similarity-spaces-and-laws.md`
(owned elsewhere in this same review round); this item is the materials/
molecular database that would consume those spaces plus the substitution-
estimate and route-making machinery on top.

test: none yet — no code exists. First testable slice would be the
`prototype`/`occupancy` split on a handful of known crystal families plus a
per-axis distance query that returns "same prototype, different magnetic
decoration" candidates correctly.

Closest existing items: `class-lattice-similarity-spaces-and-laws.md`
(owns the similarity spaces this item's per-axis queries run over),
`material-off-sample-model.md` (the trust-ordered property-read discipline
`property` above should match), `slab-modelling-knobs.md`,
`reaction-kind-and-synthesis-cost.md` (RXNO + route scoring — do not
re-derive), `chem-tools-integration.md` (ASKCOS/AiZynth already live, dark),
`structure-kind-demotion.md` (crystal/molecule storage is moving to be
se-fronted — this item's `structure`/`prototype` tables should land
compatible with that direction, not add a second import path).
