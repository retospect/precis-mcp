"""Symmetry-distinct enumeration for combinatorial ops.

Implements ``substitute(... enumerate='all')`` etc. — given a host
structure and a fractional substitution request, returns the
symmetry-inequivalent ordered patterns, capped at ``max_siblings``.

Uses pymatgen's ``EnumerateStructureTransformation`` (enumlib
under the hood) with a fallback to a brute-force loop when enumlib
is unavailable.
"""

from __future__ import annotations

from typing import Any


def enumerate_substitutions(
    atoms: Any,
    *,
    equivalence_class: str,
    fraction: float,
    element: str,
    max_siblings: int = 32,
) -> list[Any]:
    """Return the symmetry-inequivalent ordered substitution patterns."""
    raise NotImplementedError("enumerate_substitutions — wiring not landed")
