"""The store-backed half of :mod:`precis.pcb.session` — split out for
pcb-workspace-code slice 4 so ``session.py`` itself stays wheel-clean
(``precis-geom[pcb-kernel]`` carries the pure IR glue; this module is the
MCP/DB side, excluded from the wheel by ``packaging/geom/pyproject.toml``).

:func:`load_snapshot` is the ONE read of the graph plus every hashed
surface the ``pcb_place``/``pcb_route`` jobs and the enqueue-side
idempotency digest share — see :func:`precis.pcb.session.content_hash`'s
docstring for why the read list and the hashed list must be one call.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from precis.store import Store


@dataclass(frozen=True)
class DesignSnapshot:
    """The graph plus exactly the store surfaces :func:`content_hash`
    digests (gr266041): the idem key's inputs and what the
    ``pcb_place``/``pcb_route`` workers read are ONE list, so they cannot
    drift. Raw store rows, verbatim; ``fixed_copper`` is ``[]`` for a
    board-less design. The resolved netlist snapshot is job-written and is
    deliberately not here (nor in the digest)."""

    graph: dict[str, Any]
    features: list[dict[str, Any]]
    routes: dict[str, dict[str, Any]]
    planes: list[dict[str, Any]]
    measures: list[dict[str, Any]]
    fixed_copper: list[dict[str, Any]]

    def session_state(self) -> dict[str, Any]:
        """The ``session_state`` mapping :func:`content_hash` takes."""
        return {
            "features": self.features,
            "routes": self.routes,
            "planes": self.planes,
            "measures": self.measures,
            "fixed_copper": self.fixed_copper,
        }


def load_snapshot(store: Store, ref_id: int) -> DesignSnapshot:
    """Read the graph and every hashed surface for ``ref_id`` once.
    ``fixed_copper`` is keyed by the graph's ``board_id``, not ``ref_id``."""
    graph = store.pcb_graph(ref_id)
    board_id = (graph.get("board") or {}).get("board_id")
    return DesignSnapshot(
        graph=graph,
        features=store.pcb_features_list(ref_id),
        routes=store.pcb_routes_get(ref_id),
        planes=store.pcb_planes_list(ref_id),
        measures=store.pcb_measures_list(ref_id),
        fixed_copper=(
            store.pcb_fixed_copper_list(int(board_id)) if board_id is not None else []
        ),
    )
