"""``precis_geom.mol`` (``import precis_geom.mol as mol``) — molecules as
building blocks in a structure workspace (molecular-workspace-code spec,
slices 1–2).

- :func:`frag` — a fragment :class:`~precis_geom.assembly.Geom` from a
  SMILES with ``[*:n]`` attachment dummies (ports ``a1..an``), a built
  ``Scene`` plus ports, or (through the read MCP) a stored structure.
- :func:`lib` — a small named set: ``OH``, ``NH2``, ``CH3``, ``COOH``,
  ``Ph``, ``PEG`` (``n=``), ``CH2n`` (``n=``), ``C2H4``, ``c60``.
- :func:`attach` — bond a fragment port to a host site (a Port, or an
  atom label with an open valence); one fused ``Geom``.
- :func:`sites` — the site grammar (``'open & face:out & every:3'``) over
  a Geom: host atom, free direction, face, ring class.
- :func:`decorate` — a group at every selected site, torsion scanned for
  the largest minimum non-bonded distance; :func:`cap` — H on every open
  valence (``terminate_open_edges`` on a Net-built Geom).
- :func:`embed_molecule` — the seeded ETKDG + best-effort MMFF embed the
  ``from_smiles`` op shares (``precis.structure.ops``).
- :func:`relax` — the cached collapse check: ``(Geom, CollapseCheck)``
  from a UFF/EMT/xTB/geo relax judged on the re-perceived bond graph
  (slice 3; :mod:`precis_geom.mol.collapse`).
- :func:`check` — ``precis.structure.check.check``: the chemistry check
  ``Run.finish()`` and the importer run (slice 4).

Every public call is ``@traced`` (atoms carry ``maker``); the geometry
math is :mod:`precis_geom.assembly` (``pga.place``), the site taxonomy is
:mod:`precis.structure.sites` (what ``view='sites'`` renders) and the
validator rules are ``precis.structure.validate`` — nothing here
re-derives them.
"""

from precis.structure.check import check
from precis_geom.mol.attachment import attach
from precis_geom.mol.collapse import (
    BondChange,
    CollapseCheck,
    RelaxCollapsed,
    relax,
)
from precis_geom.mol.decorator import cap, decorate
from precis_geom.mol.fragment import embed_molecule, frag
from precis_geom.mol.library import lib
from precis_geom.mol.selector import MolSite, sites

__all__ = [
    "BondChange",
    "CollapseCheck",
    "MolSite",
    "RelaxCollapsed",
    "attach",
    "cap",
    "check",
    "decorate",
    "embed_molecule",
    "frag",
    "lib",
    "relax",
    "sites",
]
