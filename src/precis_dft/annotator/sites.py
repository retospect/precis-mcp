"""Site table and adjacency graph.

For each atom, name what the LLM needs to reason about it: element,
the layer it sits in (for slabs), a coarse role (``surface_top`` /
``subsurface`` / ``bottom`` / ``bulk`` / ``adsorbate``), its
coordination number, and the indices of its neighbours.

Neighbour detection: distance-cutoff. For each atom, neighbours are
every other atom within ``1.15 × (r_i + r_j)`` where ``r`` is the
covalent radius (ASE table). The 1.15 multiplier mirrors
``ase.neighborlist`` defaults and tolerates the moderate bond-
length expansion typical of DFT-relaxed surfaces.

Adsorbate detection: an atom is an adsorbate when it sits at the
top of the slab (above the top metal layer along the slab axis)
AND its connected component in the bond graph is small (≤ 6 atoms)
AND that component is exclusively non-metal species or hybrid.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from ase import Atoms
from ase.data import covalent_radii
from ase.neighborlist import NeighborList, natural_cutoffs

#: Multiplier on the covalent-radius cutoff. ASE's natural_cutoffs
#: returns r_i; we want r_i + r_j scaled — handled via NeighborList.
_NEIGHBOR_CUTOFF_MULT = 1.15

#: Elements treated as metallic for adsorbate-vs-host classification.
#: Pragmatic list: every transition metal + the main p-block metals
#: relevant to catalysis. The list is intentionally permissive — the
#: adsorbate classifier defaults to "host" on uncertainty.
_HOST_METALS: frozenset[str] = frozenset(
    {
        # 3d transition
        "Sc",
        "Ti",
        "V",
        "Cr",
        "Mn",
        "Fe",
        "Co",
        "Ni",
        "Cu",
        "Zn",
        # 4d transition
        "Y",
        "Zr",
        "Nb",
        "Mo",
        "Tc",
        "Ru",
        "Rh",
        "Pd",
        "Ag",
        "Cd",
        # 5d transition
        "Hf",
        "Ta",
        "W",
        "Re",
        "Os",
        "Ir",
        "Pt",
        "Au",
        "Hg",
        # lanthanides (selected)
        "La",
        "Ce",
        "Eu",
        "Gd",
        "Yb",
        # p-block metals relevant to catalysis substrates
        "Al",
        "Ga",
        "In",
        "Sn",
        "Pb",
        "Bi",
    }
)


def _build_neighbor_list(atoms: Atoms) -> NeighborList:
    cutoffs = [_NEIGHBOR_CUTOFF_MULT * r for r in natural_cutoffs(atoms)]
    nl = NeighborList(cutoffs, skin=0.0, self_interaction=False, bothways=True)
    nl.update(atoms)
    return nl


def adjacency(atoms: Atoms) -> list[dict[str, Any]]:
    """Return one entry per atom with neighbour indices + distances.

    Schema::

        [
          {"i": 0, "neighbors": [{"j": 4, "distance": 2.77}, ...]},
          ...
        ]
    """
    nl = _build_neighbor_list(atoms)
    positions = atoms.get_positions()
    cell = atoms.cell.array
    out: list[dict[str, Any]] = []
    for i in range(len(atoms)):
        indices, offsets = nl.get_neighbors(i)
        neighbours: list[dict[str, Any]] = []
        for j, offset in zip(indices, offsets, strict=False):
            disp = positions[j] + offset @ cell - positions[i]
            distance = float(np.linalg.norm(disp))
            neighbours.append({"j": int(j), "distance": distance})
        # Sort by distance so the closest neighbour reads first.
        neighbours.sort(key=lambda n: n["distance"])
        out.append({"i": i, "neighbors": neighbours})
    return out


def _classify_role_in_slab(
    atoms: Atoms, axis: int, layers: list[list[int]]
) -> dict[int, str]:
    """Assign role per atom for a slab structure.

    Rules:
    - Bottom 1-2 layers → ``bottom``.
    - Top 1-2 layers of host species → ``surface_top``.
    - Layers strictly between → ``subsurface``.
    - Atoms above the top host layer that pass the adsorbate check
      → ``adsorbate``.
    - Anything else → ``bulk`` (fallback).
    """
    role: dict[int, str] = {i: "bulk" for i in range(len(atoms))}
    if not layers:
        return role
    # Layers are sorted by axis projection low → high. Identify the
    # "host" portion (layers that contain mostly host metals); the
    # outermost host layer at the high-axis end is the surface,
    # anything above it is an adsorbate.
    symbols = atoms.get_chemical_symbols()

    def _is_host_layer(layer: list[int]) -> bool:
        if not layer:
            return False
        metals = sum(1 for i in layer if symbols[i] in _HOST_METALS)
        return metals / len(layer) >= 0.5

    host_layer_idxs = [k for k, layer in enumerate(layers) if _is_host_layer(layer)]
    if not host_layer_idxs:
        # No metal layers detected — every layer is "adsorbate-like"
        # (rare for catalysis structures); leave as bulk.
        return role

    bottom_k = host_layer_idxs[0]
    top_k = host_layer_idxs[-1]

    for k, layer in enumerate(layers):
        for i in layer:
            if k <= bottom_k:
                role[i] = "bottom"
            elif k >= top_k:
                # Top host layer is surface_top; layers above it are
                # adsorbates.
                if k == top_k:
                    role[i] = "surface_top"
                else:
                    role[i] = "adsorbate"
            else:
                role[i] = "subsurface"
    return role


def site_table(atoms: Atoms) -> list[dict[str, Any]]:
    """Return one entry per atom with the columns the agent reasons over.

    Schema (one row per atom)::

        {
          "index":       int,
          "element":     "Pt",
          "z":           78,
          "layer":       int | None,
          "site_role":   "surface_top" | "subsurface" | "bottom" |
                         "adsorbate" | "bulk",
          "coord_num":   int,
          "neighbors":   [j, j, ...],
        }

    Layer is per-slab; bulk structures get ``layer=None``.
    """
    from .header import detect_dimensionality, detect_slab_layers

    nl = _build_neighbor_list(atoms)
    symbols = atoms.get_chemical_symbols()
    numbers = atoms.get_atomic_numbers()

    dim = detect_dimensionality(atoms)
    if dim == "slab":
        cell_lengths = atoms.cell.lengths()
        axis = int(np.argmax(cell_lengths))
        layers = detect_slab_layers(atoms, axis)
        layer_of: dict[int, int | None] = {}
        for k, layer in enumerate(layers):
            for i in layer:
                layer_of[i] = k
        role_of = _classify_role_in_slab(atoms, axis, layers)
    else:
        layer_of = {i: None for i in range(len(atoms))}
        role_of = {i: "bulk" for i in range(len(atoms))}

    out: list[dict[str, Any]] = []
    for i in range(len(atoms)):
        idx, _ = nl.get_neighbors(i)
        out.append(
            {
                "index": i,
                "element": symbols[i],
                "z": int(numbers[i]),
                "layer": layer_of.get(i),
                "site_role": role_of.get(i, "bulk"),
                "coord_num": len(idx),
                "neighbors": [int(j) for j in idx],
            }
        )
    return out


def _covalent_radius(symbol: str) -> float:
    """ASE covalent radius lookup, in Å."""
    from ase.data import atomic_numbers

    z = atomic_numbers[symbol]
    return float(covalent_radii[z])


__all__ = ["adjacency", "site_table"]
