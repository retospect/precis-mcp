"""``structure_tree`` view — derivation-tree walker.

Surfaced as a view on the ``structure`` / ``structure_draft``
handlers: ``get(kind='structure', id='structure:<sha>',
view='tree', depth=3)`` returns the lineage subgraph rooted at the
given structure, joined with the latest ``dft_calculation`` per
node.

Walks ``derived_from`` links via the standard ``links_for`` helper;
joins to ``dft_calculation`` by ``link(rel='computed_on')`` on
each visited structure.
"""

from __future__ import annotations

from typing import Any


def structure_tree(
    store: Any,
    *,
    root: str,
    depth: int = 3,
    include_calculations: bool = True,
) -> dict[str, Any]:
    """Return the lineage subgraph rooted at ``root``."""
    raise NotImplementedError("structure_tree — wiring not landed")
