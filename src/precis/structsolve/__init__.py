"""precis.structsolve — in-tree structural solvers (docs/backlog/
structural-solution-space.md).

The *generator* side of the generator/checker split: these modules
propose geometry; :mod:`precis_se.stability` (and, later, nm's
state-dependent twin) *checks* it. Keeping the solvers here — outside
any one kind's plugin — preserves that separation while staying in-tree
(Reto's 2026-09-09 decision: numpy/scipy, the pcb-SA posture, not an
external engine repo).

House rules, same as :mod:`precis.cad`: **pure functions over passed-in
arrays, no store access** — handlers own IO and units. Every solver is
unit-agnostic (se feeds metres, nm feeds Å; the numbers never know).

Modules:

- :mod:`precis.structsolve.formfind` — force-density form-finding
  (slice 2): given topology, per-member force densities and anchored
  coordinates, one linear solve per axis returns node geometry in
  equilibrium.
- :mod:`precis.structsolve.complementarity` — active-set unilateral
  analysis (docs/backlog/complementarity-solver.md slices 1 and 3):
  given topology, per-member axial rate/free-length/sign-idiom and
  supports, finds the small-displacement equilibrium in which every
  tension-only, compression-only and must-contact member obeys its
  one-sidedness — never both a gap and a force. Slice 3's
  :func:`~precis.structsolve.complementarity.probe_bistability` takes
  two candidate free-length assignments over the same topology (e.g. a
  photoswitch's ``{trans, cis}`` states) and reports whether each is a
  stable equilibrium plus, when both are, an advisory-tier
  energy-barrier estimate between them.
- :mod:`precis.structsolve.simp` — 3D density-field SIMP (the nTop leg,
  slice 4): a voxel domain plus nodal loads and supports goes in, a
  density field with a compliance history comes out, optionally through
  an additive-manufacturing overhang filter. Same lattice also carries
  the naive gyroid fill.

Where :mod:`~precis.structsolve.formfind` returns an exact equilibrium,
``simp`` returns an **estimate**: it discretises a continuum into voxels
and reports compliance from that mesh, so its numbers are a screening
tier — good for ranking candidate layouts against each other, never good
enough to certify one, and never a hard DRC. That is why every
:class:`~precis.structsolve.simp.SimpResult` carries a ``notes`` tuple
saying what the run checked and what it did not; callers propagate the
notes rather than quoting the number alone. The voxelisation itself (cad
keep-in/keep-out sampling) and the se ops that drive it are a later
slice — nothing here touches the store.
"""

from __future__ import annotations

from precis.structsolve.complementarity import (
    IDIOMS,
    BistabilityResult,
    ComplementarityError,
    ComplementarityInputError,
    ComplementarityResult,
    EquilibriumStabilityResult,
    probe_bistability,
    solve_complementarity,
)
from precis.structsolve.formfind import FormFindError, FormFindResult, form_find
from precis.structsolve.simp import (
    LatticeResult,
    SimpResult,
    lattice_fill,
    overhang_violations,
    simp_optimize,
)

__all__ = [
    "IDIOMS",
    "BistabilityResult",
    "ComplementarityError",
    "ComplementarityInputError",
    "ComplementarityResult",
    "EquilibriumStabilityResult",
    "FormFindError",
    "FormFindResult",
    "LatticeResult",
    "SimpResult",
    "form_find",
    "lattice_fill",
    "overhang_violations",
    "probe_bistability",
    "simp_optimize",
    "solve_complementarity",
]
