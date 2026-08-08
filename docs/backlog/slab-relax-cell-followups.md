# Variable-cell slab relax — container + bulk-relax follow-ups

The relax op's `cell` param (inplane/full masked FrechetCellFilter, c-axis
pinned) landed in-repo and rides the job contract, but the precis-dft
container (gpaw-relax, external repo) doesn't honour `params.json["cell"]`
yet — its variable-cell path is unbuilt. Better for slabs: relax the bulk
once per (element, MLIP) with a full cell filter, cache the lattice constant,
and have the `slab` op cut at that MLIP-consistent constant (removes the
spurious in-plane strain at build time, amortized across candidates). Owner
`src/precis/structure/relax.py::_relax_ml` + the precis-dft container.
