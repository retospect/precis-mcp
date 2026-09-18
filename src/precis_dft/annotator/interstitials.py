"""Interstitial site finder.

Identifies octahedral and tetrahedral voids in a bulk structure (or
the bulk-like portion of a slab). Used by the ``intercalate`` op so
the LLM can say "insert H in the octahedral interstitial near atom
12" rather than picking fractional coordinates.

v1 uses pymatgen's ``VoronoiInterstitialGenerator``. A faster custom
Delaunay-based finder may replace it for high-symmetry hosts if
profiling shows a bottleneck.
"""

from __future__ import annotations

from typing import Any


def find_interstitials(atoms: Any) -> list[dict[str, Any]]:
    """Return named interstitial sites.

    Each entry: ``{name, kind, position, host_atoms, coord_num}``.
    ``kind`` is ``'octahedral'`` or ``'tetrahedral'``. ``name`` is
    e.g. ``octahedral_3`` (deterministic ordering by position).
    """
    raise NotImplementedError("find_interstitials — wiring not landed")
