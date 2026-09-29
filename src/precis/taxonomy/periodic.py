"""The periodic subject axis: an element symbol widens to its table position.

`taxonomy-bootstrap.md` §"Generated subject axes". Every proposal is a
``specialises`` edge on the ``"periodic"`` axis (`types.AxisEdge`), so a
widening query that follows only this axis never crosses into composition or
termination parents — see `types.AxisEdge`'s docstring for why the axis lives
on the edge rather than the node.
"""

from __future__ import annotations

from precis.taxonomy.elements import Element, element
from precis.taxonomy.types import AxisEdge

_AXIS = "periodic"


def periodic_edges(symbol: str) -> tuple[AxisEdge, ...]:
    """Parent proposals for one element symbol; empty for an unknown symbol.

    Base parents are group, period, block and series (`elements.Element`'s
    own columns); ``platinum-group-metal``, ``transition-metal`` and
    ``metal`` are added on top where the element qualifies, per
    `taxonomy-bootstrap.md`. A base parent and a top-up parent can coincide
    (every transition metal's ``series`` already *is*
    ``"transition-metal"``) — parents are deduplicated by key, keeping edge
    order stable.
    """
    el = element(symbol)
    if el is None:
        return ()
    child = symbol.lower()
    parents = list(_base_parents(el))
    if el.is_platinum_group:
        parents.append("platinum-group-metal")
    if el.is_transition_metal:
        parents.append("transition-metal")
    if el.is_metal:
        parents.append("metal")
    seen: dict[str, None] = {}
    for parent in parents:
        seen.setdefault(parent, None)
    return tuple(AxisEdge(child=child, parent=parent, axis=_AXIS) for parent in seen)


def _base_parents(el: Element) -> tuple[str, ...]:
    parents = []
    if el.group is not None:
        parents.append(f"group-{el.group}")
    parents.append(f"period-{el.period}")
    parents.append(f"{el.block}-block")
    parents.append(el.series)
    return tuple(parents)
