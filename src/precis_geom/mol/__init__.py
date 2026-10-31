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
- :func:`prism` / :func:`generate` — the hex tiler's prism tube and the
  ``GENERATORS`` registry's ``hexfold_scene`` / ``cnt`` / ``fullerene`` /
  ``cyclodextrin`` as cached steps returning a Geom (slice 5).
- :func:`check` — ``precis.structure.check.check``: the chemistry check
  ``Run.finish()`` and the importer run (slice 4).

``frag`` (the embed), ``lib``, ``decorate``, ``cap``, ``relax``, ``prism``
and ``generate`` are cached steps (:mod:`precis_geom.steps`); each keeps its
own trace frame name. Every public call is traced (atoms carry ``maker``); the geometry
math is :mod:`precis_geom.assembly` (``pga.place``), the site taxonomy is
:mod:`precis.structure.sites` (what ``view='sites'`` renders) and the
validator rules are ``precis.structure.validate`` — nothing here
re-derives them.

``relax(rung='auto')`` keeps UFF for finite systems; periodic systems use
xTB when available, otherwise EMT only within its metal-containing domain.
The finite hexfold comparison (gr543629, 2026-10-31; 200-step cap,
fmax 0.1 eV/A, 2 A displacement bound) found no broken/formed bonds or lost
rings under either UFF or GFN2: the H-capped (4,0), len=2 tube (40 atoms)
settled in 120/30 steps, and the H-capped (6,0), len=5 tube fused k=0 to
C60-minus-hexagon (180 atoms) in 173/39 steps. Both stayed one fragment
with verdict ``ok``. These fixtures give no bond-graph reason to switch
the finite default to xTB; ``tests/workspaces/test_mol_relax.py`` pins the
small comparison and the system-type selection with/without tblite.
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
from precis_geom.mol.generate import generate, prism
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
    "generate",
    "lib",
    "prism",
    "relax",
    "sites",
]
