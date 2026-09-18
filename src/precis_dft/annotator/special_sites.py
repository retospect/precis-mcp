"""Named special sites — top, bridge, fcc/hcp hollow, interstitials.

Surface sites use pymatgen's ``AdsorbateSiteFinder`` to find
adsorption-relevant positions on a slab; we map them to the
LLM-readable name pattern ``{kind}_{host_indices}`` (e.g.
``top_12``, ``bridge_12_13``, ``fcc_4_5_6``).

Interstitial sites use pymatgen's ``VoronoiInterstitialGenerator``
for bulk structures. They surface as ``octahedral_3`` /
``tetrahedral_7`` with deterministic numbering by position.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from ase import Atoms

from .header import detect_dimensionality


def find_surface_sites(atoms: Atoms) -> list[dict[str, Any]]:
    """Return named surface adsorption sites on a slab.

    Schema (one entry per site)::

        {
          "name":      "bridge_12_13",
          "kind":      "top" | "bridge" | "fcc" | "hcp",
          "position":  [x, y, z],
          "host_atoms":[12, 13],
        }

    Returns an empty list for non-slab structures.
    """
    if detect_dimensionality(atoms) != "slab":
        return []
    try:
        from pymatgen.analysis.adsorption import AdsorbateSiteFinder
        from pymatgen.io.ase import AseAtomsAdaptor
    except ImportError:  # pragma: no cover
        return []

    try:
        structure = AseAtomsAdaptor.get_structure(atoms)
        finder = AdsorbateSiteFinder(structure)
        site_dict = finder.find_adsorption_sites(distance=1.6, symm_reduce=0)
    except Exception:
        # AdsorbateSiteFinder can fail on highly distorted slabs or
        # weird cell shapes; we degrade gracefully to no sites
        # rather than break the whole annotation.
        return []

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for kind in ("ontop", "bridge", "hollow"):
        for pos in site_dict.get(kind, []):
            label_kind = "top" if kind == "ontop" else kind
            host_indices = _nearest_host_atoms(atoms, pos, k=_k_for(label_kind))
            name = f"{label_kind}_{'_'.join(str(i) for i in host_indices)}"
            if name in seen:
                continue
            seen.add(name)
            out.append(
                {
                    "name": name,
                    "kind": label_kind,
                    "position": [float(x) for x in pos],
                    "host_atoms": host_indices,
                }
            )
    return out


def _k_for(kind: str) -> int:
    return {"top": 1, "bridge": 2, "fcc": 3, "hcp": 3, "hollow": 3}.get(kind, 1)


def _nearest_host_atoms(atoms: Atoms, pos: list[float], *, k: int) -> list[int]:
    """Return indices of the ``k`` nearest atoms to ``pos`` (in Å)."""
    positions = atoms.get_positions()
    deltas = positions - np.asarray(pos)
    distances = np.linalg.norm(deltas, axis=1)
    order = np.argsort(distances)
    return sorted(int(i) for i in order[:k])


def find_interstitials(atoms: Atoms) -> list[dict[str, Any]]:
    """Return named interstitial sites for a bulk structure.

    Schema (one entry per site)::

        {
          "name":       "octahedral_3",
          "kind":       "octahedral" | "tetrahedral",
          "position":   [x, y, z],
          "host_atoms": [list of nearest host indices],
        }

    Returns an empty list for non-bulk structures (interstitial
    chemistry is a 3D-periodic concern).
    """
    if detect_dimensionality(atoms) != "bulk":
        return []
    try:
        from pymatgen.analysis.defects.generators import (
            VoronoiInterstitialGenerator,
        )
        from pymatgen.io.ase import AseAtomsAdaptor
    except ImportError:  # pragma: no cover
        return []

    try:
        structure = AseAtomsAdaptor.get_structure(atoms)
        gen = VoronoiInterstitialGenerator()
        candidates = list(gen.get_inserted_structures(structure, insert_species=("H",)))
    except Exception:
        return []

    out: list[dict[str, Any]] = []
    for idx, struct in enumerate(candidates):
        # The interstitial atom is the last site added (pymatgen
        # convention).
        intr_site = struct[-1]
        pos = [float(x) for x in intr_site.coords]
        host_indices = _nearest_host_atoms(atoms, pos, k=6)
        kind = _classify_void(len(host_indices))
        out.append(
            {
                "name": f"{kind}_{idx}",
                "kind": kind,
                "position": pos,
                "host_atoms": host_indices,
            }
        )
    return out


def _classify_void(n_neighbors: int) -> str:
    """Map nearest-neighbour count to a void kind label.

    Octahedral interstitials have 6 host neighbours; tetrahedral
    have 4. The Voronoi generator can produce other coordinations
    too; we label those as ``other``.
    """
    if n_neighbors <= 4:
        return "tetrahedral"
    if n_neighbors <= 6:
        return "octahedral"
    return "other"


__all__ = ["find_interstitials", "find_surface_sites"]
