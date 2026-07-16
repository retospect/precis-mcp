"""Protein fold → ``structure`` convergence (ADR 0056 slice 4c / ADR 0043).

Turn an AlphaFold mmCIF into a ``structure`` :class:`Scene` so a folded protein
renders in the existing 3D structure viewer (``/structure``) and can be probed
as an atom graph. Proteins are large + **non-periodic**, so the Scene is a big
axis-aligned box with PBC off (ADR 0043 "molecule mode") — this is export/view,
not primary storage: the fold IR stays on ``meta.fold`` of the protein, and the
structure ref is a derived, on-demand projection.

**Dependency-free.** Parses the mmCIF ``_atom_site`` loop directly (no ASE,
which is ``[dft]``-gated and absent on the always-on request path), reusing the
same scan shape as :func:`precis_bio.ir.mean_plddt_from_cif`.
"""

from __future__ import annotations

import numpy as np

from precis.structure.cell import Cell
from precis.structure.scene import Atom, Scene

#: Padding (Å) around the molecule bounding box so a non-periodic cell never
#: self-images (ADR 0043 molecule mode wants ≥15 Å between periodic copies;
#: with PBC off it just keeps every atom strictly inside (0,1) fractional).
BOX_PADDING = 15.0


def parse_atom_site(cif: str) -> list[tuple[str, float, float, float]]:
    """``(element, x, y, z)`` in Å for each atom in the mmCIF ``_atom_site`` loop.

    A dependency-free whitespace scan (same shape as
    :func:`precis_bio.ir.mean_plddt_from_cif`): find the ``_atom_site`` header
    block, then read ``type_symbol`` + ``Cartn_{x,y,z}`` by column ordinal.
    Best-effort — skips malformed rows and returns ``[]`` when the loop or those
    columns aren't present.
    """
    lines = cif.splitlines()
    headers: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        if lines[i].strip() == "loop_":
            j = i + 1
            block: list[str] = []
            while j < n and lines[j].lstrip().startswith("_atom_site."):
                block.append(lines[j].strip())
                j += 1
            if block:
                headers = block
                i = j
                break
            i = j
        else:
            i += 1
    if not headers:
        return []
    try:
        sym = headers.index("_atom_site.type_symbol")
        cx = headers.index("_atom_site.Cartn_x")
        cy = headers.index("_atom_site.Cartn_y")
        cz = headers.index("_atom_site.Cartn_z")
    except ValueError:
        return []

    ncols = len(headers)
    out: list[tuple[str, float, float, float]] = []
    while i < n:
        row = lines[i].strip()
        if not row or row.startswith(("_", "#", "loop_", "data_")):
            break
        f = row.split()
        if len(f) >= ncols:
            try:
                out.append(
                    (f[sym].capitalize(), float(f[cx]), float(f[cy]), float(f[cz]))
                )
            except (ValueError, IndexError):
                pass
        i += 1
    return out


def cif_to_scene(cif: str, *, detect_bonds_max: int = 0) -> Scene:
    """Build a non-periodic ``structure`` :class:`Scene` from an mmCIF model.

    A big axis-aligned box (molecule bbox + :data:`BOX_PADDING`), PBC off; each
    atom placed in fractional coords and uniquely labelled ``<El><n>`` per
    element (so ``label_hi`` is the per-element count). Bonds are inferred only
    when the atom count ≤ ``detect_bonds_max`` — the covalent detector is O(N²)
    and a full protein is 1000s of atoms, so the default (0) skips it and the
    viewer shows an element-coloured atom cloud. Raises ``ValueError`` on a CIF
    with no readable atoms.
    """
    atoms_xyz = parse_atom_site(cif)
    if not atoms_xyz:
        raise ValueError("no _atom_site atoms found in the mmCIF")

    coords = np.array([(x, y, z) for _, x, y, z in atoms_xyz], dtype=float)
    lo = coords.min(axis=0)
    hi = coords.max(axis=0)
    box = np.maximum((hi - lo) + 2 * BOX_PADDING, 1.0)  # never a zero-length axis
    cell = Cell(lattice=np.diag(box), pbc=(False, False, False))
    scene = Scene(cell=cell)

    counts: dict[str, int] = {}
    for el, x, y, z in atoms_xyz:
        counts[el] = counts.get(el, 0) + 1
        label = f"{el}{counts[el]}"
        frac = (np.array([x, y, z]) - lo + BOX_PADDING) / box
        scene.atoms[label] = Atom(label=label, element=el, frac=frac)
    scene.label_hi = dict(counts)

    if detect_bonds_max and len(scene.atoms) <= detect_bonds_max:
        from precis.structure.probe import detect_bonds

        scene.bonds = detect_bonds(scene)
    return scene
