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
- ``simp`` — 3D density-field SIMP (the nTop leg), build order slice 4;
  not built yet.
"""

from __future__ import annotations

from precis.structsolve.formfind import FormFindError, FormFindResult, form_find

__all__ = ["FormFindError", "FormFindResult", "form_find"]
