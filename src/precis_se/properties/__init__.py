"""se region properties — measurands from the term taxonomy, and the
registry of computers that turn a declared region measure into a realised
value (docs/backlog/se-region-property-layer.md).

**Slice A (this package today):** a measure may name a ``measurand`` — a
``taxon`` node under the ``measurand`` start node (seeded by core migration
``0182_se_measurand_seed.sql``) — instead of, or as well as, the legacy
closed ``unit`` enum. :mod:`precis_se.properties.measurand` resolves it at
write time and snapshots three facts onto the measure row
(:class:`~precis_se.measures.MeasureSpec`): the taxon ref id, the slug, and
the se unit (``MeasureSpec.unit``). Reads need no join.

**The computer registry** (:data:`COMPUTERS`) is the seam slice B fills:
measurand slug → a computer that derives the realised value for a region
(Gasteiger-class partial charges, a group-contribution contact angle, a
point-charge field sum). It is empty here, so every non-geometric measurand
is *declared but unchecked* — :func:`is_checked` says so, and
:func:`precis_se.drc.drc` emits a loud ``measurand_unchecked`` warning for
each such measure rather than staying silent.

A measurand that is one of the legacy enum's four nodes
(:data:`precis_se.measures.LEGACY_MEASURANDS` — length, count, ratio,
angle) counts as checked: it is exactly the legacy ``unit=`` form, which
was never flagged, and ``length`` is computed by the datum evaluator
(:func:`precis_se.datums.evaluate_measure`).

**Where measurands attach** (migration ``0018_se_regions.sql``; skill
``precis-se-regions-help``). The measurand is snapshotted onto
``se_measures.measurand``/``measurand_ref_id`` with the se unit in ``unit``,
resolved at write through :attr:`~precis_se.ops.SeTree.measurands`. The datum
grammar gains region selectors (``patch:``, ``ring:`` resolved on the cad
envelope; ``sites:``, ``atoms:`` parsed, resolution deferred to the bound
structure — :mod:`precis_se.datums`). :mod:`precis_se.pockets` names a set of
regions plus a shape on a block (``add_pocket``/``set_pocket``/
``remove_pocket``, table ``se_pockets``, ``view='pockets'``); a region's
measures are derived from their ``datum``. Until slice B fills
:data:`COMPUTERS`, DRC's ``measurand_unchecked`` names every measure nothing
computes. Block names may not contain ``/ @ [ ]``
(:data:`precis_se.ops.BLOCK_NAME_RESERVED`).

**The molecule painter** (:mod:`precis_se.properties.surface`,
``view='surface'``; docs/backlog/pocket-from-molecule.md slice 1) samples
a bound molecule's van der Waals surface and paints each point with a
GFN2 point-charge ESP, an H-bond class and a polarity class. It computes
on read and registers no computer yet: slice 2 turns its output into
pocket regions, and running it on a wall cluster is what fills
:data:`COMPUTERS`. It is not imported here, so this package stays free
of the cad and structure imports it needs.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from precis_se.measures import LEGACY_MEASURANDS

#: A region computer: ``(tree, measure) -> realised value or None`` —
#: ``None`` means "not computable here" and the entry carries a
#: ``reason`` attribute saying why (:mod:`precis_se.properties.computers`).
Computer = Callable[[Any, Any], float | None]


#: Lazy computer tables: a short name → (module, dict) resolved on first
#: use, so this package's import stays light (the surface painter's
#: structure/ase imports and the flow adapter's scipy solve only load
#: when their measurand is actually evaluated).
_LAZY_TABLES: dict[str, tuple[str, str]] = {
    "wall": ("precis_se.properties.computers", "WALL_COMPUTERS"),
    "flow": ("precis_se.flow", "FLOW_COMPUTERS"),
}


class _LazyComputer:
    """A :data:`COMPUTERS` entry that defers importing its table — and
    with it the surface painter's structure/ase imports or the flow
    adapter — until the measurand is actually evaluated, so this
    package's import stays light. ``reason`` forwards the real
    computer's last None reason for ``measure_uncomputable``."""

    def __init__(self, name: str, table: str = "wall"):
        self._name = name
        self._table = table
        self.reason: str | None = None

    def __call__(self, tree: Any, measure: Any) -> float | None:
        import importlib

        module, attr = _LAZY_TABLES[self._table]
        real = getattr(importlib.import_module(module), attr)[self._name]
        value = real(tree, measure)
        self.reason = getattr(real, "reason", None)
        return value


#: measurand slug → computer. The wall-side region computers
#: (:mod:`precis_se.properties.computers`, pocket-from-molecule slice 2a)
#: and the flow-chamber computers (:mod:`precis_se.flow`,
#: se-flow-kernel slice 2) — every other measurand stays
#: declared-but-unchecked.
COMPUTERS: dict[str, Computer] = {
    slug: _LazyComputer(slug)
    for slug in (
        "net-partial-charge",
        "h-bond-donor-count",
        "h-bond-acceptor-count",
        "electrostatic-potential",
    )
}
COMPUTERS.update(
    {
        slug: _LazyComputer(slug, table="flow")
        for slug in (
            "breakthrough-fraction",
            "residence-time-t0",
            "resistance-ratio",
            "evenness",
        )
    }
)


def is_checked(measurand: str | None) -> bool:
    """Does anything compute a realised value for this measurand? ``None``
    (a legacy measure with no measurand) is checked by definition — the
    legacy form never carried the advisory."""
    if measurand is None:
        return True
    return measurand in COMPUTERS or measurand in LEGACY_MEASURANDS.values()
