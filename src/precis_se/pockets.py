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

import json
from dataclasses import dataclass, field
from typing import Any

#: The atomic-hull shape token: the pocket is bounded by the hull of the
#: block's bound structure, computed by whatever reads it.
HULL = "hull"


@dataclass
class PocketSpec:
    """One pocket on a block. ``regions`` is the ordered list of region
    selector strings (stored as written, stripped). ``class_hash`` is the
    pocket's spec-class fingerprint (slice 2b): sha256 over the canonical
    region list — sorted canonical selector texts, each with its sorted
    measure tuples ``(measurand, target, band)`` — so two pockets
    requiring the same wall properties hash alike however their regions
    were written. Recomputed by :func:`stamp_class_hashes` on every
    ``apply_ops``; never authored."""

    name: str
    shape: str | None = None
    regions: list[str] = field(default_factory=list)
    class_hash: str | None = None


def canonical_class(tree: Any, block: str, pocket: PocketSpec) -> Any:
    """The canonical region list ``class_hash`` digests — a JSON-ready
    structure: ``{"block": block, "regions": [{"selector": <canonical
    text>, "measures": [[measurand, value, [min, max]], …]}, …]}``, each
    list sorted so equal specs serialise identically."""
    from precis_se.datums import _selector_text, parse_selector

    regions = []
    for raw in sorted(_selector_text(parse_selector(r)) for r in pocket.regions):
        tuples = sorted(
            json.dumps(
                [m.measurand, m.value, [m.min_value, m.max_value]],
                separators=(",", ":"),
            )
            for m in region_measures(tree, block, raw)
        )
        regions.append({"selector": raw, "measures": [json.loads(t) for t in tuples]})
    return {"block": block, "regions": regions}


def stamp_class_hashes(tree: Any) -> None:
    """Recompute ``class_hash`` on every pocket in the tree — called at
    the end of :func:`precis_se.ops.apply_ops` so the hash always matches
    the regions and measures as stored (a hash is derived, never
    authored)."""
    import hashlib

    for block_name, node in tree.blocks.items():
        for pocket in node.pockets.values():
            canonical = canonical_class(tree, block_name, pocket)
            pocket.class_hash = hashlib.sha256(
                json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode(
                    "utf-8"
                )
            ).hexdigest()


def region_measures(tree: Any, block: str, selector: str) -> list[Any]:
    """The measures on ``block`` whose datum names ``selector``'s region —
    the derived membership (module docstring)."""
    from precis_se.datums import same_region

    return [
        m for m in tree.measures if m.block == block and same_region(m.datum, selector)
    ]
