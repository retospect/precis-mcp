"""Annotator — Atoms → Views.

Single entry point :func:`annotate` runs every sub-annotator and
returns the materialized view dict in one shape:

    {
      "header":        {...},      # dimensionality, composition, cell, slab info
      "sites":         [...],      # per-atom: element, layer, role, coord_num, neighbors
      "graph":         [...],      # per-atom: adjacency list + bond distances
      "special_sites": [...],      # named top/bridge/fcc/hcp + interstitials
      "symmetry":      {...},      # spacegroup, point group, equivalence classes
      "ascii_top":     "...",      # 24×12 grid
      "ascii_side":    "...",      # 30×18 grid
      "warnings":      [...],      # heuristic quality warnings
      "view_version":  int,        # bump on any annotator algorithm change
    }

The view chunks each reference one of these keys. Bumping
``VIEW_VERSION`` invalidates all caches and forces re-annotation
(the view_worker watches for staleness).
"""

from __future__ import annotations

from typing import Any

from ase import Atoms

from . import header as header_mod
from . import renderings, sites, special_sites, symmetry, warnings

#: Bump on any annotator algorithm change so caches invalidate.
VIEW_VERSION = 1


def annotate(atoms: Atoms) -> dict[str, Any]:
    """Compute every view for an ASE Atoms object."""
    hdr = header_mod.header(atoms)
    return {
        "header": hdr,
        "sites": sites.site_table(atoms),
        "graph": sites.adjacency(atoms),
        "special_sites": special_sites.find_surface_sites(atoms)
        + special_sites.find_interstitials(atoms),
        "symmetry": symmetry.analyse(atoms),
        "ascii_top": renderings.ascii_top(atoms),
        "ascii_side": renderings.ascii_side(atoms),
        "warnings": warnings.quality_warnings(atoms),
        "view_version": VIEW_VERSION,
    }


__all__ = ["VIEW_VERSION", "annotate"]
