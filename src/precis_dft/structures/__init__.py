"""Structure serialization + canonical identity.

A frozen :class:`structure` ref carries the canonical POSCAR text on
NFS and a sha-256 content-address derived from it. This module is
the boundary between ASE ``Atoms`` objects (the in-memory shape) and
the bytes that get stored.

The canonical form is VASP-5 POSCAR with element-symbol labels on
the species line. POSCAR is portable, human-inspectable, and round-
trips cleanly through ASE's ``ase.io.vasp`` reader. Sha-256 of the
POSCAR text is stable across machines and ASE versions provided we
control the formatting; this module does the formatting.

The structure_draft shape is identical except a draft also carries
an append-only edit_log; commit promotes the draft's Atoms to a
frozen ``structure:<sha>`` ref.
"""

from __future__ import annotations

import hashlib
import io
from typing import TYPE_CHECKING

from ase import Atoms
from ase.io.vasp import read_vasp, write_vasp

if TYPE_CHECKING:
    from pathlib import Path


def canonical_poscar(atoms: Atoms) -> str:
    """Return the canonical POSCAR string for ``atoms``.

    Uses ``vasp5=True`` so the second line contains element symbols
    (round-trippable without a separate POTCAR). Direct coordinates
    so cell-vector renormalisation doesn't shift the hash on the
    same physical structure. Constraints are stored as Selective
    Dynamics so frozen layers survive the round trip.

    Empty structures (n_atoms = 0) skip write_vasp (which assumes
    at least one species) and serialize as a sentinel header.
    """
    if len(atoms) == 0:
        cell = atoms.cell.array
        return (
            "empty\n"
            "1.0\n"
            f"{cell[0, 0]:.16f} {cell[0, 1]:.16f} {cell[0, 2]:.16f}\n"
            f"{cell[1, 0]:.16f} {cell[1, 1]:.16f} {cell[1, 2]:.16f}\n"
            f"{cell[2, 0]:.16f} {cell[2, 1]:.16f} {cell[2, 2]:.16f}\n"
            "\n0\nDirect\n"
        )

    buf = io.StringIO()
    write_vasp(
        buf,
        atoms,
        vasp5=True,
        direct=True,
        sort=False,
    )
    return buf.getvalue()


def sha256_of(text: str) -> str:
    """Return hex sha-256 of UTF-8 bytes."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def structure_id_for(atoms: Atoms) -> str:
    """Return the canonical ``structure:<sha>`` id for ``atoms``."""
    return "structure:" + sha256_of(canonical_poscar(atoms))


def atoms_from_poscar(text: str) -> Atoms:
    """Round-trip parse the canonical POSCAR text back into ``Atoms``."""
    return read_vasp(io.StringIO(text))


def write_canonical_poscar(atoms: Atoms, path: Path) -> None:
    """Write the canonical POSCAR to disk."""
    path.write_text(canonical_poscar(atoms), encoding="utf-8")


__all__ = [
    "atoms_from_poscar",
    "canonical_poscar",
    "sha256_of",
    "structure_id_for",
    "write_canonical_poscar",
]
