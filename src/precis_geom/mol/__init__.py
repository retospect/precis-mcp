"""``precis_geom.mol`` (``import precis_geom.mol as mol``) — molecules as
building blocks in a structure workspace (molecular-workspace-code spec,
slice 1).

- :func:`frag` — a fragment :class:`~precis_geom.assembly.Geom` from a
  SMILES with ``[*:n]`` attachment dummies (ports ``a1..an``), a built
  ``Scene`` plus ports, or (through the read MCP) a stored structure.
- :func:`lib` — a small named set: ``OH``, ``NH2``, ``CH3``, ``COOH``,
  ``Ph``, ``PEG`` (``n=``), ``CH2n`` (``n=``), ``C2H4``, ``c60``.
- :func:`attach` — bond a fragment port to a host site (a Port, or an
  atom label with an open valence); one fused ``Geom``.
- :func:`embed_molecule` — the seeded ETKDG + best-effort MMFF embed the
  ``from_smiles`` op shares (``precis.structure.ops``).

Every public call is ``@traced`` (atoms carry ``maker``); the geometry
math is :mod:`precis_geom.assembly` (``pga.place``) and the validator
rules are ``precis.structure.validate`` — nothing here re-derives them.
Later slices add sites/decorate/cap, the collapse check and ``check``.
"""

from precis_geom.mol.attachment import attach
from precis_geom.mol.fragment import embed_molecule, frag
from precis_geom.mol.library import lib

__all__ = ["attach", "embed_molecule", "frag", "lib"]
