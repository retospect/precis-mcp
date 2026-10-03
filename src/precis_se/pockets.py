"""se pockets — a named set of regions on a block, plus the shape that
bounds them (docs/backlog/se-region-property-layer.md slice A, in-scope 3).

A pocket is a **block feature**, not a new property object: it names a
set of region selectors (:mod:`precis_se.datums` grammar — ``patch:``,
``ring:``, ``sites:``, ``atoms:``, or any other datum form) and holds a
shape. The properties of a region are **ordinary measures** whose
``datum`` is that region's selector — membership is derived at read time
(:func:`region_measures`, parsed-selector equality via
:func:`precis_se.datums.same_region`), never stored, so a measure added
later with ``add_measure(datum='patch:…')`` joins the region it names,
and ``remove_pocket`` leaves the measures standing.

Ops (:mod:`precis_se.ops`): ``add_pocket`` (``block``, ``name``,
``shape``?, ``regions``? ``[{selector, measures?: [<add_measure fields>]}]``
— inline measures are written through ``add_measure`` with ``block`` and
``datum`` filled in), ``set_pocket`` (presence-based ``shape``/
``regions``), ``remove_pocket``. Stored on ``SeBlock.pockets`` and
persisted to ``se_pockets`` (migration ``0018_se_regions.sql``) in
lockstep with the owning block row, like ``se_ports``. Template-owned
like measures: an instance's pockets are its template's.

``shape`` is a cad-DSL envelope string in the block's local frame (the
``set_envelope`` grammar) or :data:`HULL` — the hull of the block's bound
atoms. ``None`` is an unshaped pocket (suggestive by contract).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: The atomic-hull shape token: the pocket is bounded by the hull of the
#: block's bound structure, computed by whatever reads it.
HULL = "hull"


@dataclass
class PocketSpec:
    """One pocket on a block. ``regions`` is the ordered list of region
    selector strings (stored as written, stripped)."""

    name: str
    shape: str | None = None
    regions: list[str] = field(default_factory=list)


def region_measures(tree: Any, block: str, selector: str) -> list[Any]:
    """The measures on ``block`` whose datum names ``selector``'s region —
    the derived membership (module docstring)."""
    from precis_se.datums import same_region

    return [
        m for m in tree.measures if m.block == block and same_region(m.datum, selector)
    ]
