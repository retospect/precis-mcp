---
status: idea
pillar: 3d-design
title: se viewer colours atoms by tight-binding LDOS at the Fermi level and by spring-derived strain
---

# se: per-atom electronic and strain fields, colourable in the viewer

Reto, 2026-10-09 (via chat-interface): "In the se/structure viewer, can we
meaningfully show conductivity, above/below Fermi level, by colour? What
else can we meaningfully highlight, and can we do it from springs or only
from DFT-type stuff?" Today the viewer colours by element only
(`_atom_colors` in `src/precis/viz3d/stickfig.py`) and
`src/precis_se/atomic/mechanics.py` reports strain as one total
(`harmonic_strain_energy_J`), so no per-atom scalar reaches the render.

## What

**Fields.** One se op (`fields`, or a view on the block) computes named
per-atom scalars and stores them on the block beside `topology`:

- From a nearest-neighbour π tight-binding model on the bond graph (one
  orbital per sp² carbon, hopping t ≈ 2.7 eV on bonded pairs, each hopping
  scaled by the π-orbital alignment across the relaxed bond so curvature
  and seam rings open their own gap):
  - `ldos_ef`: local density of states in a window around the Fermi level.
    The "conductivity" colour: hot where states exist at E_F.
  - `ldos_signed`: occupied-window minus empty-window LDOS (donor-like
    versus acceptor-like sites), a centred diverging field.
  - `homo`, `lumo`: |ψ|² per atom of the frontier orbitals.
  - Block-level: `gap_eV`, `electronic` (metallic / semiconducting by the
    gap), so the (n,m) rule from
    [hexfold-tube-chirality-class](hexfold-tube-chirality-class.md) is
    verified on the built atoms and still works on composites with no (n,m).
- From geometry alone (the spring side; no electronic content):
  - `bond_dev_a`: signed bond-length deviation from 1.42 Å (per bond,
    averaged to atoms for colouring).
  - `angle_strain`: per vertex, the quantity the warn-tier `angle_strain`
    rule already computes but does not export.
  - `pyramidalization_deg`: from the three neighbour directions; the
    standard predictor of where addition chemistry lands on curved carbon.
  - `gauss_curvature`: discrete, from ring membership (5-rings positive,
    7-rings negative); neighbour of
    [berry-phase-and-topological-defects-in-precis-models](berry-phase-and-topological-defects-in-precis-models.md).

**Viewer.** `color_by=<field>` on the se render and the /se page: a
sequential palette for unsigned fields, a diverging palette centred on
zero for signed ones, element colouring when unset; a legend with the
field name, range and, for the LDOS fields, the Fermi window in eV.
Hydrogen caps are drawn but carry no tight-binding field (they are not
in the π system) and are excluded from the colour range.

**Cost.** Dense eigensolve of the 5,790-atom Y variant is tens of seconds
in numpy; the Fermi-window LDOS alone is a sparse shift-invert solve in
seconds. Store the fields; do not recompute per render.

## Why

The tube's two integers encode its electronic class, but a composite
(Y-junction, fin, nanobud) has no (n,m), and the physically interesting
states sit on the defect rings and zigzag edges, not on the pristine
wall. Tight-binding is the physics the (n,m) rule is derived from, costs
seconds, and needs only the bond graph the block already carries. The
spring fields are free and answer the nanoreactor question "where will
chemistry happen".

## Not in scope

Charges and quotable gaps (xTB or `precis_dft`); edge magnetism
(mean-field Hubbard, a later slice); heteroatoms beyond an on-site energy
shift for B and N; any change to the relax itself.

test: pristine (10,0) → `gap_eV` > 0.5 and `ldos_ef` ~ 0 everywhere;
(6,6) → gap ~ 0 and `ldos_ef` uniform; a 5-7 seam → `ldos_ef` maximum on
seam-ring atoms; `pyramidalization_deg` ~ 0 on a flat sheet and largest
on pentagon atoms of a cap; `color_by=ldos_ef` renders with a legend and
`color_by=unknown` is a typed refusal naming the known fields.
