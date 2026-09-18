"""Header view — a compact, coordinate-free description of the structure.

This is the LLM's first read on a structure. It says what the structure
is (slab vs. bulk vs. cluster), how big (n_atoms, cell dimensions, vacuum
when relevant), and what's in it (composition). No coordinates leave
this view.

Slab detection: the structure is treated as a slab when one cell vector
is more than 25% longer than the other two AND a contiguous vacuum gap
of at least 6 Å exists in that direction. Otherwise it's a bulk
periodic structure (3D-periodic). Clusters (0D, non-periodic) are
detected by ``atoms.pbc`` falling to all-False.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

import numpy as np
from ase import Atoms

#: Minimum vacuum gap (Å) for a structure to be classified as a slab.
SLAB_MIN_VACUUM_ANGSTROM = 6.0

#: Long-axis ratio threshold for slab detection.
SLAB_LONG_AXIS_RATIO = 1.25


def detect_dimensionality(atoms: Atoms) -> str:
    """Return ``'cluster'`` / ``'slab'`` / ``'bulk'``."""
    if not any(atoms.pbc):
        return "cluster"
    cell_lengths = atoms.cell.lengths()
    if len(cell_lengths) != 3:
        return "bulk"
    long_axis = int(np.argmax(cell_lengths))
    others = [i for i in range(3) if i != long_axis]
    if cell_lengths[long_axis] < SLAB_LONG_AXIS_RATIO * max(
        cell_lengths[others[0]], cell_lengths[others[1]]
    ):
        return "bulk"
    vacuum = _vacuum_along_axis(atoms, long_axis)
    if vacuum >= SLAB_MIN_VACUUM_ANGSTROM:
        return "slab"
    return "bulk"


def _vacuum_along_axis(atoms: Atoms, axis: int) -> float:
    """Estimate the contiguous vacuum gap along ``axis`` in Å.

    Projects all atoms onto ``axis``, finds the largest gap between
    sorted projections (wrapping around the cell). Returns that gap
    times the axis length.
    """
    positions = atoms.get_scaled_positions(wrap=True)[:, axis]
    if len(positions) == 0:
        return float(atoms.cell.lengths()[axis])
    sorted_pos = np.sort(positions)
    gaps = np.diff(sorted_pos)
    # Wrap-around gap from last to first.
    wrap_gap = 1.0 - sorted_pos[-1] + sorted_pos[0]
    largest_frac = max(float(gaps.max(initial=0.0)), float(wrap_gap))
    return largest_frac * float(atoms.cell.lengths()[axis])


def detect_slab_layers(atoms: Atoms, axis: int) -> list[list[int]]:
    """Group atoms into layers along ``axis``.

    Uses a fixed bin width of 0.6 Å in real-space along the axis;
    atoms inside the same bin are one layer. Returns a list of atom-
    index lists, layers ordered from low to high projection.
    Atoms inside a vacuum gap are kept in their own (empty) layer
    slot — we just sort by projection.
    """
    if len(atoms) == 0:
        return []
    projections = atoms.get_positions()[:, axis]
    order = np.argsort(projections)
    sorted_proj = projections[order]
    layer_idx: list[list[int]] = [[int(order[0])]]
    prev = sorted_proj[0]
    bin_width = 0.6
    for i in range(1, len(sorted_proj)):
        if sorted_proj[i] - prev <= bin_width:
            layer_idx[-1].append(int(order[i]))
        else:
            layer_idx.append([int(order[i])])
        prev = sorted_proj[i]
    return layer_idx


def header(atoms: Atoms) -> dict[str, Any]:
    """Return the header dict — the LLM's first read.

    Schema::

        {
          "n_atoms":          int,
          "composition":      {"Pt": 27, "Ni": 9},
          "formula":          "Pt27Ni9",
          "dimensionality":   "cluster" | "slab" | "bulk",
          "cell_lengths_angstrom": [a, b, c],
          "cell_angles_degrees":   [alpha, beta, gamma],
          "pbc":              [bool, bool, bool],
          "slab": {            # only when dimensionality == 'slab'
            "axis":            int (0/1/2),
            "vacuum_angstrom": float,
            "n_layers":        int,
          } | null,
        }
    """
    species = atoms.get_chemical_symbols()
    composition = dict(Counter(species))
    cell = atoms.cell
    info: dict[str, Any] = {
        "n_atoms": len(atoms),
        "composition": composition,
        "formula": atoms.get_chemical_formula(),
        "dimensionality": detect_dimensionality(atoms),
        "cell_lengths_angstrom": [float(x) for x in cell.lengths()],
        "cell_angles_degrees": [float(x) for x in cell.angles()],
        "pbc": [bool(x) for x in atoms.pbc],
        "slab": None,
    }
    if info["dimensionality"] == "slab":
        cell_lengths = cell.lengths()
        long_axis = int(np.argmax(cell_lengths))
        info["slab"] = {
            "axis": long_axis,
            "vacuum_angstrom": _vacuum_along_axis(atoms, long_axis),
            "n_layers": len(detect_slab_layers(atoms, long_axis)),
        }
    return info


__all__ = [
    "SLAB_LONG_AXIS_RATIO",
    "SLAB_MIN_VACUUM_ANGSTROM",
    "detect_dimensionality",
    "detect_slab_layers",
    "header",
]
