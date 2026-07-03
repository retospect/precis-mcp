"""Geometry export — what a structure *ultimately emits* (ADR 0043 §13).

The output side of the IR. Two formats are **hand-written, pure, zero-dependency**
(they're simple and the most-used): **VASP POSCAR** (the DFT-engine lingua
franca) and **extended XYZ** (ASE-native, lossless — carries cell + pbc + our
labels + the `fixed` constraint). **CIF** (crystallographic, symmetry) is
ASE-gated behind the optional ``[dft]`` extra — a missing ASE surfaces as
``Unsupported`` at the handler, never a crash.

Bonds are dropped by all three (DFT consumes positions + cell, §8.1); the bond
graph round-trips only via formats that carry it (MOL/SDF/LAMMPS — later).
"""

from __future__ import annotations

import io

import numpy as np

from .scene import Scene


def _grouped(scene: Scene) -> tuple[list[str], dict[str, list]]:
    """Atoms grouped by element in first-seen order (POSCAR needs this)."""
    order: list[str] = []
    groups: dict[str, list] = {}
    for atom in scene.atoms.values():
        if atom.element not in groups:
            groups[atom.element] = []
            order.append(atom.element)
        groups[atom.element].append(atom)
    return order, groups


def to_poscar(scene: Scene) -> str:
    """VASP POSCAR (Direct coords). Emits *Selective dynamics* iff any atom is
    fixed — ``T`` = free along that axis, ``F`` = fixed (the `fixed` bitmask)."""
    order, groups = _grouped(scene)
    any_fixed = any(a.fixed for a in scene.atoms.values())
    lines: list[str] = []
    lines.append("".join(f"{el}{len(groups[el])}" for el in order) or "structure")
    lines.append("1.0")
    for row in scene.cell.lattice:
        lines.append(f"  {row[0]:.16f}  {row[1]:.16f}  {row[2]:.16f}")
    lines.append("  " + "  ".join(order))
    lines.append("  " + "  ".join(str(len(groups[el])) for el in order))
    if any_fixed:
        lines.append("Selective dynamics")
    lines.append("Direct")
    for el in order:
        for a in groups[el]:
            row = f"  {a.frac[0]:.16f}  {a.frac[1]:.16f}  {a.frac[2]:.16f}"
            if any_fixed:
                row += "  " + " ".join(
                    "F" if (a.fixed >> ax) & 1 else "T" for ax in range(3)
                )
            lines.append(row)
    return "\n".join(lines) + "\n"


def to_extxyz(scene: Scene) -> str:
    """Extended XYZ: Cartesian positions + a Lattice/pbc header + our `label`
    as an extra per-atom column (the lossless, precis-native round-trip form)."""
    flat = " ".join(f"{x:.8f}" for x in np.asarray(scene.cell.lattice).flatten())
    pbc = " ".join("T" if p else "F" for p in scene.cell.pbc)
    head = f'Lattice="{flat}" Properties=species:S:1:pos:R:3:label:S:1 pbc="{pbc}"'
    lines = [str(len(scene.atoms)), head]
    for label, a in scene.atoms.items():
        x, y, z = scene.cell.frac_to_cart(a.frac)
        lines.append(f"{a.element} {x:.8f} {y:.8f} {z:.8f} {label}")
    return "\n".join(lines) + "\n"


def ase_available() -> bool:
    """True if ASE is importable (the optional ``[dft]`` extra)."""
    try:
        import ase  # noqa: F401
    except ImportError:
        return False
    return True


def _to_ase(scene: Scene):  # type: ignore[no-untyped-def]
    """Scene → ASE Atoms (requires ASE)."""
    from ase import Atoms

    symbols = [a.element for a in scene.atoms.values()]
    scaled = [list(map(float, a.frac)) for a in scene.atoms.values()]
    return Atoms(
        symbols=symbols,
        scaled_positions=scaled,
        cell=np.asarray(scene.cell.lattice),
        pbc=scene.cell.pbc,
    )


def to_cif(scene: Scene) -> str:
    """CIF via ASE (caller checks :func:`ase_available` first)."""
    from ase.io import write

    buf = io.StringIO()
    write(buf, _to_ase(scene), format="cif")
    return buf.getvalue()
