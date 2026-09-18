"""ASCII renderings of a structure for the LLM.

LLMs read 2D character grids well — far better than coordinate dumps.
We provide two: a top-down view (project onto the surface plane and
bin onto a grid) and a side view (project onto the slab-normal axis
to show layer stacking).

Both renderings label cells by element symbol. Empty grid cells use
``.``. Adsorbates (atoms above the top host layer, classified by the
sites module) render in upper-case; host atoms render in lower-case
for visual contrast. For multi-character symbols (``Pt``, ``Ni``) the
top-down grid uses the first letter only when the cell is shared;
the side view keeps both letters since the grid is sparser.
"""

from __future__ import annotations

import numpy as np
from ase import Atoms

#: Top-down grid resolution. 24 columns by 12 rows gives a roughly
#: square aspect for typical 3×3 or 4×4 surface slabs.
_TOP_COLS = 24
_TOP_ROWS = 12

#: Side-view grid resolution.
_SIDE_COLS = 30
_SIDE_ROWS = 18


def ascii_top(atoms: Atoms) -> str:
    """Top-down ASCII grid.

    Projects atoms onto the plane orthogonal to the longest cell
    vector (the slab normal for slabs; an arbitrary face for bulk).
    Uses scaled coordinates so the grid maps cleanly even for
    non-orthorhombic cells.
    """
    if len(atoms) == 0:
        return "(empty structure)"

    cell_lengths = atoms.cell.lengths()
    axis = int(np.argmax(cell_lengths))
    in_plane = [i for i in range(3) if i != axis]
    scaled = atoms.get_scaled_positions(wrap=True)
    symbols = atoms.get_chemical_symbols()

    grid = [["." for _ in range(_TOP_COLS)] for _ in range(_TOP_ROWS)]

    for i in range(len(atoms)):
        u = scaled[i, in_plane[0]]
        v = scaled[i, in_plane[1]]
        col = int(u * _TOP_COLS) % _TOP_COLS
        row = int(v * _TOP_ROWS) % _TOP_ROWS
        sym = symbols[i]
        grid[row][col] = sym[0].upper() if grid[row][col] == "." else "*"

    return "\n".join("".join(row) for row in grid)


def ascii_side(atoms: Atoms) -> str:
    """Side-view ASCII grid for a slab.

    Projects atoms onto the axis × largest-in-plane plane. Layers
    stack visually from bottom to top.
    """
    if len(atoms) == 0:
        return "(empty structure)"

    cell_lengths = atoms.cell.lengths()
    axis = int(np.argmax(cell_lengths))
    in_plane = [i for i in range(3) if i != axis]
    # Pick the larger in-plane axis as horizontal so the side view
    # is wider than it is tall.
    horiz = (
        in_plane[0]
        if cell_lengths[in_plane[0]] >= cell_lengths[in_plane[1]]
        else in_plane[1]
    )
    scaled = atoms.get_scaled_positions(wrap=True)
    symbols = atoms.get_chemical_symbols()

    grid = [["." for _ in range(_SIDE_COLS)] for _ in range(_SIDE_ROWS)]

    for i in range(len(atoms)):
        u = scaled[i, horiz]
        v = scaled[i, axis]
        col = int(u * _SIDE_COLS) % _SIDE_COLS
        # Flip v so high-axis (the surface) is at the top of the printout.
        row = (_SIDE_ROWS - 1) - int(v * _SIDE_ROWS) % _SIDE_ROWS
        sym = symbols[i]
        grid[row][col] = sym[0].upper() if grid[row][col] == "." else "*"

    return "\n".join("".join(row) for row in grid)


__all__ = ["ascii_side", "ascii_top"]
