"""Graph navigation — the link neighbourhood of one ref, as JSON.

Routes:

* ``GET /graph/{kind}/{ref_id}.json`` — :meth:`Store.neighbourhood` for the
  ref (``store/_links_ops.py``), the one shape the neighbourhood panel, the
  graph focus page and agent tooling all read. Query params: ``depth``
  (``1``|``2``), ``rels`` / ``kinds`` (comma lists; ``rels`` speaks the
  presented slug, so an inbound ``cites`` is ``cited-by``), ``since`` /
  ``until`` (parsed as Drive does, ``routes/items.py::_parse_date``) and
  ``cap`` (hop-1 node cap, default :data:`DEFAULT_CAP`, at most
  :data:`MAX_CAP`).

On top of the store shape the route adds ``groups``: the hop-1 relations
bucketed under the fisheye ring headings (``utils/refeye.py::ring_group``),
``"Other"`` for a relation no ring follows — the heading order a browser
panel renders in. ``trust`` is deliberately not exposed yet: the store's
tier predicate duplicates the finding handler's and is to be single-sourced
before it reaches the browser (backlog ``web-graph-navigation.md``).

Errors are JSON too (``{"error": ...}``): 404 for a missing ref, 400 for a
bad argument — not the HTML error page the ``PrecisError`` handler renders,
since the consumer is a fetch, not a tab.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from precis.errors import BadInput, NotFound
from precis.utils.refeye import RING_GROUPS, ring_group
from precis_web.deps import get_store
from precis_web.routes.items import _parse_date

router = APIRouter(prefix="/graph", tags=["graph"])

#: Hop-1 node cap when the request names none (the "60 nodes per page"
#: ruling, 2026-10-06) and the most a request may ask for.
DEFAULT_CAP = 60
MAX_CAP = 200

OTHER_GROUP = "Other"


def _csv(raw: str) -> list[str] | None:
    vals = [v.strip() for v in (raw or "").split(",") if v.strip()]
    return vals or None


def ring_groups(counts: dict[str, dict[str, int]]) -> list[dict[str, Any]]:
    """Bucket the ``counts`` relations under :data:`RING_GROUPS` headings.

    Returns ``[{heading, rels: [rel, ...], n}]`` in ring order, only the
    headings with at least one relation present, then ``"Other"`` for
    relations :func:`ring_group` places nowhere. ``n`` is the group's
    edge total over every kind.
    """
    buckets: dict[str, list[str]] = {}
    for rel in sorted(counts):
        buckets.setdefault(ring_group(rel) or OTHER_GROUP, []).append(rel)
    order = [*RING_GROUPS, OTHER_GROUP]
    return [
        {
            "heading": heading,
            "rels": buckets[heading],
            "n": sum(sum(counts[r].values()) for r in buckets[heading]),
        }
        for heading in order
        if heading in buckets
    ]


@router.get("/{kind}/{ref_id}.json", response_model=None)
async def neighbourhood_json(
    request: Request,
    kind: str,
    ref_id: int,
    depth: int = 1,
    rels: str = "",
    kinds: str = "",
    since: str = "",
    until: str = "",
    cap: int = DEFAULT_CAP,
) -> JSONResponse:
    """The ref's link neighbourhood, see the module docstring for the shape."""
    store = get_store(request)
    try:
        if not 1 <= cap <= MAX_CAP:
            raise BadInput(f"cap must be between 1 and {MAX_CAP}, got {cap!r}")
        data = store.neighbourhood(
            kind,
            ref_id,
            depth=depth,
            rels=_csv(rels),
            kinds=_csv(kinds),
            since=_parse_date(since),
            until=_parse_date(until),
            cap=cap,
        )
    except NotFound as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except BadInput as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    data["groups"] = ring_groups(data["counts"])
    return JSONResponse(data)
