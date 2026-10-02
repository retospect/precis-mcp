---
status: draft
title: carbon-encapsulated metal catalysts (cage, nanobud flow cell, octagon window, single-atom crater) are the surface-Pourbaix optimizer's candidate space for NO→NH₃
pillar: 3d-design
prio: normal
blocked-by: surface-pourbaix-staircase-optimizer
---

# Carbon-encapsulated metal catalysts as the surface-Pourbaix optimizer's candidate space

**File only, not built** (Reto, 2026-10-02T20:37Z: "convert to standard
and add to the appropriate place"). The source is Reto's transfer prompt,
`~/.claude/projects/-Users-reto-precis-mcp/scratch/review-msgs/encapsulated-metal-transfer-prompt.md`
(§ numbers below refer to it).

Labels:
- **DECIDED**: settled, not re-litigated here.
- **OPEN**: unresolved; listed under Open questions.
- **PROPOSED**: a mapping suggested while joining the two designs. It is
  **unreviewed and not settled**.

Sibling of `surface-pourbaix-staircase-optimizer.md`, which supplies the
machinery. This item supplies the outer-loop candidates and two additions:
a second Pourbaix layer for the carbon host and caps, and host/containment
constraints in the Shapley mask.

## Motivation / why

The optimizer's outer loop needs a candidate generator. Reto's
encapsulated-metal designs give four families with one shape (carbon host
+ metal + designed defects), each a parameterised generator (§1–2). The
cage is meant to make non-noble metals usable, and to bias selectivity, not
only to shield the metal.

**How it feeds qu164903.** The prompt's PROPOSED demo reaction is NO→NH₃
(§5), which is this thread's own north star. Its "selectivity is the real
objective" over NH₃ / N₂ / N₂O / NH₂OH is the same scorecard as
`pathway-nh3-network-completeness.md` and
`pathway-selectivity-u-ph-window.md`. So:
- **F3/F4 candidates enter qu164903** (or a sibling quest) as ordinary
  candidates with the same reaction_config, on the same corrected
  references (catalysis-selectivity-19).
- They are ranked like-with-like by the network-basis rule (R1),
  alongside the Pd slabs.
- A candidate is a cage motif, not a slab variant.

## In scope (DECIDED design content, summarised)

- **The four families (§1):**
  - **F1, cluster-in-cage.** A magic-number Pd cluster (13/55/147/309),
    with Fe doping on the hull.
    - **Cage generator, DECIDED, already exists:** a vdW-spacer-inflated
      hull → a smooth surface → a surface-following carbon tiler → a
      closure score: 12 net pentagons in the vertex caps, low facet
      strain, no compensating 5-7 pairs on flats.
    - The spacer distance is a tunable knob.
  - **F2, nanobud flow cell.** A cage budding from a tube that serves as
    the feed channel. Multi-port: a feed and an exit, with containment
    traded against throughput.
  - **F3, octagon window.** One 8-ring on a flat or saddled facet. The
    reaction happens at the least-coordinated, carbon-ligated rim atoms
    (an M-N-C-like countable site).
  - **F4, single-atom crater. Geometry DECIDED, corrected.** A graphene
    hole with a Y-junction seam (120° × 3) and a 60° leaf tilt. An sp3-H
    crease after 1–2 rows. An 8/9-ring socket that the Pd plugs with σ
    bonds.
    - 8-ring with an alternating H/N rim → square-planar Pd-C₄ / Pd-N₄.
    - The bilateral variant is a two-state site.
- **Cross-family genome (§2)**, as discrete outer-loop moves: family,
  metal/dopant, cluster rung, spacer, corner caps, facet pores, rim
  treatment, ring size, collar rows, sidedness, ports.
- **Engineered pores (§2, DECIDED concept):** extra 5-7 pairs as transport
  windows. Stability and permeability go on different defects.
- **Corner caps (§3, OPEN as a variable):** F baseline, H, in-ring N; avoid
  OH. The tiler must model the caps, not bolt them on afterwards.

## Explicitly NOT in scope

- Any build, until Reto lifts "file only".
- The optimizer machinery itself (CHE sweep, validity map, staircase,
  Shapley field, reset contour). That is
  `surface-pourbaix-staircase-optimizer.md`.
- Re-specifying the cage generator, the tiler or nanobud geometry. Those
  exist; see Cross-links. Nothing here edits hexfold-toolkit or
  nanobuds-paper items.

## Slices (the prompt's §10 build order)

1. **F4 single-atom crater first.**
   - Build the 8-ring Pd-C₄ and Pd-N₄ geometries (1 row, single-sided).
   - Relax with the MLIP.
   - Check the corner-strain prediction: base corners about −30°, top
     sp3-H corners about +31.6°.
2. A two-layer Pourbaix for F4 (metal + carbon/caps), then the NO→NH₃
   staircase with per-intermediate masks. This needs the optimizer's
   slices 1–4.
3. The host/containment constraints (§6, PROPOSED), then the Shapley field
   and IPR reduction (optimizer slice 5).
4. F3 octagon window on a 55/147-atom magic cluster.
5. F1 cage generator over the magic ladder: closure scoring and Fe-site
   enumeration. Reuses the existing generator, below.
6. F2 multi-port variants.
7. An outer-loop surrogate over all families, plus reset contours and NEB
   barriers for the containment and permeation steps.

## PROPOSED mappings (unreviewed; review before any slice relies on them)

- **§4 fidelity tiers:**
  - MLIP screen of the cage first (relax, closure strain, cap stability);
  - then DFT anchors on the active-site region: F3/F4 as a cluster or
    periodic cut-out, F1 as a facet slab plus a cage patch.
- **§4 two coupled Pourbaix layers.**
  - Layers:
    - the metal layer, shifted by confinement and rim ligation;
    - the carbon layer: oxidation/etching, cap stability, crease
      hydrogenation.
  - Honest window = metal protected ∩ carbon stable ∩ caps intact ∩ every
    intermediate viable.
  - The validity map is extended with host states: etched vs intact, caps
    stripped, socket top vs bottom, Fe site.
- **§5 NO→NH₃ as the primary demo.**
- **§6 the extended constraint set:**
  - catalytic rungs, plus N₂/N₂O suppression;
  - host/containment toggles: metal retention, carbon oxidation, caps,
    sp3 crease, permeation, egress, port vs solvated cation, socket
    switching;
  - the bottleneck → structural-move proposal table.
- **§7 encapsulation barriers:** H/H⁺ permeation per pore type, NO entry
  and NH₃ exit, metal escape, socket hopping, Fe migration.

## Acceptance criteria (for when it is unblocked; per slice)

- **Slice 1:** relaxed F4 Pd-C₄ and Pd-N₄ (8-ring, 1 row, single-sided)
  with:
  - measured base- and top-corner angular deficits against the §1
    prediction;
  - the Pd–C/N bond lengths;
  - whether the 9-ring reconstructs, as predicted.

  All recorded in this item, with the MLIP named and its transfer caveat
  to strained sp3 creases stated (§8).
- **Slice 2:** an F4 two-layer map in which each region names its trusted
  host and metal state, and the operating window shown as the
  intersection.
- **Every slice:** the three uncertainty sources reported separately (§8,
  DECIDED). References on the same corrected footing as the slab
  campaigns (catalysis-selectivity-19).

## Cross-links (reuse, do not duplicate)

- **The cage generator, §1 F1, exists:**
  - `src/precis_surface/` is the lattice-agnostic smooth-surface kernel:
    level set → periodic marching cubes → dual (see its `__init__`
    docstring and `docs/backlog/precis-surface-kernel.md`).
  - `hexfold.smooth` is the carbon binding: the surface-following hex
    tiler / discretiser.
  - `src/hexfold/` (`fullerene.py`, `defects.py`, nanobud menus in
    `spec.md`) is the defect and cap notation.
  - The smooth→hex tiler campaign (memory: remeshing made bond spread
    worse, so measure strain, do not assume) and the nanobud generator /
    viz work live in the **hexfold-toolkit** and **nanobuds-paper**
    threads. Their items are left alone.
- **Overlap to watch.**
  - F1 cages and F2 nanobuds overlap `nanobud-campaign.md`,
    `se-nanobud-graph.md` and the hexfold seam/sp3 items
    (`hexfold-sp3-seam.md`, `hexfold-sp3-isolation-band.md`; F4's sp3-H
    crease is a relative of those).
  - **Flag: the F2 nanobud flow cell touches the nanobuds paper's
    subject.** Any F2 slice must check with the nanobuds-paper owner first.
    Not messaged from here, per Reto's instruction.
- `surface-pourbaix-staircase-optimizer.md`: the machinery; this item's
  `blocked-by`.
- `pd-hydride-substrate.md`: subsurface H as discrete slabs. The same
  question arises inside a caged Pd cluster under reducing potential.
- catalysis-selectivity-19 gas and H* corrections
  (`scratch/catsel-catpath-brief-corrections.md`). The H* shift is keyed by
  host metal, so a carbon-ligated Pd rim needs its own PBE anchor before
  the shift applies.
- `neb-barriers-in-the-catpath-pipeline.md`: the §7 encapsulation barriers
  and the NEBscape audit.

## Open questions (the prompt's §9, OPEN)

1. Does the metal act *through* intact carbon (spectator vs participant)?
   To be settled by DFT and experiment.
2. How activity and selectivity fold into the window score alongside
   stability.
3. The F4 bilateral two-state site: a bug or a switchable feature? Hopping
   barrier vs potential.
4. Fe placement: one minimum or an ensemble, at each magic size (§1 F1,
   OPEN).
5. Cap choice: optimised per window, or fixed per family (§3).
6. The size of the DFT anchor model (cluster cut-out vs periodic) needed
   to stay reference-consistent with the slab campaigns.
7. Should the Wulff shape under reaction conditions loop back into the
   generator?
8. The cheapest validating experiment: one predicted carbon-oxidation or
   metal-leaching onset, checked against CV/ICP-MS.

## Target + blast radius

None until unblocked. When built: new structure-generation paths for
F3/F4 (likely `src/precis/structure/` ops or a hexfold template), quest
candidates on qu164903 or a sibling quest, and catpath runs on non-slab
geometries. The slab-centric assumptions in catpath (`n_slab`, the
fixed-layer convention) need checking for cut-out models.
