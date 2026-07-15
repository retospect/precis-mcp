"""Store write-back for route results — shared by the handler's inline
slice-0 path and the ``retrosynth`` worker dispatch.

Kept out of both ``route.py`` and ``jobs.py`` so neither imports the other:
the handler mints/awaits a job; the job runs the engine; **both** land the
resulting :class:`~precis_chem.ir.RouteGraph` on the route ref through this
one function, so the write shape (``meta.route`` + the ``card_combined``
search chunk) is defined once.
"""

from __future__ import annotations

from typing import Any

from precis_chem.ir import RouteGraph


def apply_route_result(
    store: Any,
    route_ref_id: int,
    graph: RouteGraph,
    *,
    cache_key: str,
) -> None:
    """Persist a solved/failed route onto its ref.

    Writes the normalized graph to ``meta.route``, stamps the content-address
    (``meta.cache_key``) + status, and (re-)emits the embeddable
    ``card_combined`` chunk so the route is searchable by target/precursor
    SMILES. Idempotent — a re-run with the same ``cache_key`` overwrites in
    place (the DELETE+INSERT card keeps the embedding cascade clean).
    """
    with store.tx() as conn:
        store.stamp_ref_meta(
            route_ref_id,
            {
                "route": graph.to_json(),
                "cache_key": cache_key,
                "engine": graph.engine,
                "engine_version": graph.engine_version,
                "status": "solved" if graph.solved else "unsolved",
            },
            conn=conn,
        )
        store.upsert_card_combined(route_ref_id, graph.card_text(), conn=conn)
