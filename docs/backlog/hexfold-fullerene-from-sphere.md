---
status: draft
title: Standard fullerenes from a sphere — give a diameter, get the canonical cage
pillar: 3d-design
---

# Standard fullerenes from a sphere

Reto, 2026-10-03 (via nanobuds-paper-25): given only a sphere of the right
diameter, hexfold should produce the standard buckyball on its own. That
means C60, plus C70, C80 and so on wherever a canonical isomer exists, so
hero generation stops hand-building balls.

## Motivation / why

- hexfold has exactly one cage today: `fullerene(C60)`, in closed form
  (`hexfold.fullerene`).
- `precis_se` generators say the same: "General Goldberg construction is a
  later round" (`precis_se/atomic/generators/sp2.py`).
- `hexfold.radii` already *enumerates* the icosahedral sizes
  20(h² + hk + k²), but only as fillet radii, never as atoms.

## In scope

1. **Cage from a ring spiral.**
   - The Fowler–Manolopoulos face spiral (twelve pentagon positions) gives
     the cage graph.
   - The coordinates are seeded on the sphere (topological coordinates from
     the graph Laplacian, scaled to the radius) and relaxed with stick.
   - One generator covers every isomer.
2. **Canonical table.**
   - One isomer per size where the literature names a standard one:
     - C60-Ih;
     - C70-D5h;
     - C76-D2;
     - C78-C2v(3);
     - C80-Ih;
     - C84-D2(22) and C84-D2d(23).
   - Each row carries its spiral and a citation.
   - The spiral indices come from the Atlas of Fullerenes (Fowler &
     Manolopoulos), checked against the published symmetry and the
     isolated-pentagon rule. They are not written from memory.
   - The icosahedral Goldberg (h,k) family (60, 80, 140, 180, 240, …) is
     added as a second source.
3. **Pick by size.**
   - Syntax: `fullerene(d=7.1A)` or `fullerene(r=…)`.
   - It picks the table row whose relaxed diameter is nearest, and reports
     the choice and the residual as INFO.
   - When two rows tie within the tolerance, the smaller wins. An
     elongated cage (C70) reports both of its axes.
   - `fullerene(C70)` names a cage directly.
4. **Ports.**
   - A `- hexagon@…` hole works on every cage, as it does on C60, so buds
     and pillars can fuse to any of them.

## Explicitly NOT in scope

- Non-IPR isomers, endohedral cages, and isomer search. The table is
  curated, not enumerated.
- Onions (nested cages).

## Acceptance criteria

- Each table row:
  - builds with ring census {5:12, 6:N/2−10} and pentagons isolated;
  - has a point group matching the table (symmetry of the relaxed
    coordinates within 0.05 Å);
  - has bonds 1.38–1.47 Å after stick.
- C60 from the spiral is the same graph as the closed-form C60, and its
  atoms land within 0.02 Å after alignment.
- `fullerene(d=…)` returns C60 for 7.1 Å, and C70 or C80 at their
  diameters.
- A hero spec that writes `fullerene(d=…)` builds the same scene as one
  that writes `fullerene(C60)`.

## Target + blast radius

- `hexfold.fullerene` and `hexfold.text` (the `fullerene(...)` grammar).
- spec.md §7, the primitives table.
- `precis_se` generators can reuse the spiral builder (sp2.py's "later
  round" note).

test: tests/hexfold/test_fullerene_spiral.py (new)
