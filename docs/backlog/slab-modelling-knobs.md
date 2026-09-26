# Slab modelling knobs

Grouped 2026-09-26 from 3 items that are sub-parts of one deliverable (each keeps its own section below; the originals are in the history). Split a section back out only when it becomes independently shippable.

## Richer structure design ops — vacancies, hydrogen, subsurface

_Grouped 2026-09-26; was `structure-design-ops`._

Widen the proposer's design knobs beyond surface substitution: `remove_atom`
(surface vacancies/holes), add H on-surface AND subsurface/interstitial
(hydride / subsurface-H chemistry), and subsurface dopant placement (not just
adatoms). Each needs a compact op the slab-based proposal template can emit
and autocatpath can inject. Owner structure op set +
`src/precis/quest/tick.py` proposal rules.

## structure IR lacks slab/adsorbate provenance

_Grouped 2026-09-26; was `structure-slab-provenance`._

`src/precis/structure/preflight.py::_slab_adsorbate_indices` falls back to a
dominant-element heuristic when `atoms.info['n_slab']` is unset — and no
caller can set it (the Scene IR records no "these atoms came from the slab
op"). A doped slab (Cu/Ag dopant via set_element) risks the `detached` check
misreading the dopant as a floating adsorbate. Add n_slab (or richer
op-provenance) at slab-op time and thread it through preflight(). Owner
`src/precis/structure/scene.py`, `src/precis/structure/ops.py`. Polish.

## Variable-cell slab relax — container + bulk-relax follow-ups

_Grouped 2026-09-26; was `slab-relax-cell-followups`._

The relax op's `cell` param (inplane/full masked FrechetCellFilter, c-axis
pinned) landed in-repo and rides the job contract, but the precis-dft
container (gpaw-relax, external repo) doesn't honour `params.json["cell"]`
yet — its variable-cell path is unbuilt. Better for slabs: relax the bulk
once per (element, MLIP) with a full cell filter, cache the lattice constant,
and have the `slab` op cut at that MLIP-consistent constant (removes the
spurious in-plane strain at build time, amortized across candidates). Owner
`src/precis/structure/relax.py::_relax_ml` + the precis-dft container.
