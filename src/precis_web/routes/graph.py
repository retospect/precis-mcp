"""Graph navigation — the link neighbourhood of one ref, as JSON and HTML.

Routes:

* ``GET /graph/{kind}/{ref_id}.json`` — :meth:`Store.neighbourhood` for the
  ref (``store/_links_ops.py``), the one shape the neighbourhood panel, the
  graph focus page and agent tooling all read. Query params: ``depth``
  (``1``|``2``), ``rels`` / ``kinds`` (comma lists; ``rels`` speaks the
  presented slug, so an inbound ``cites`` is ``cited-by``), ``since`` /
  ``until`` (parsed as Drive does, ``routes/items.py::_parse_date``) and
  ``cap`` (hop-1 node cap, default :data:`DEFAULT_CAP`, at most
  :data:`MAX_CAP`).
* ``GET /graph/{kind}/{ref_id}/panel`` — the HTMX fragment behind the
  Neighbourhood panel on the shared ref detail page
  (``templates/refs/detail.html.j2``): the hop-1 ring grouped by
  ``ring_group`` heading → rel → kind, with per-group "expand"
  (``?group=<heading>`` returns that one group uncapped) and the
  "Show memories" radio (``?memories=0|1``; absent → the spec default,
  :func:`_default_show_memories`).
* ``GET /graph/{kind}/{ref_id}`` — the focus page: the depth-2
  neighbourhood as a server-rendered radial SVG
  (``precis_web.graph_svg.focus_graph_svg``), node click re-focusing on
  that node's own ``/graph/...`` page.

On top of the store shape the JSON route adds ``groups``: the hop-1
relations bucketed under the fisheye ring headings
(``utils/refeye.py::ring_group``), ``"Other"`` for a relation no ring
follows — the heading order a browser panel renders in. ``trust`` is
deliberately not exposed yet: the store's tier predicate duplicates the
finding handler's and is to be single-sourced before it reaches the
browser (backlog ``web-graph-navigation.md``).

Errors are JSON too (``{"error": ...}``) on ``.json`` — 404 for a missing
ref, 400 for a bad argument — not the HTML error page the ``PrecisError``
handler renders, since the consumer is a fetch, not a tab. The HTML routes
render the shared error template / a small fragment instead.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from precis.errors import BadInput, NotFound
from precis.utils import handle_registry
from precis.utils.refeye import RING_GROUPS, ring_group
from precis_web.deps import get_store, templates
from precis_web.graph_filters import csv_list
from precis_web.graph_svg import KIND_SHAPES, focus_graph_svg
from precis_web.ref_urls import ref_url
from precis_web.routes.items import _parse_date

router = APIRouter(prefix="/graph", tags=["graph"])

#: Hop-1 node cap when the request names none (the "60 nodes per page"
#: ruling, 2026-10-06) and the most a request may ask for.
DEFAULT_CAP = 60
MAX_CAP = 200

#: Rows rendered per ring group in the neighbourhood panel before the
#: group's "expand" affordance (the "25 per group" ruling, 2026-10-06).
PANEL_GROUP_ROWS = 25

OTHER_GROUP = "Other"


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
            rels=csv_list(rels),
            kinds=csv_list(kinds),
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


def _handle(kind: str, ref_id: int) -> str:
    """The neighbourhood's own handle rule (``_links_ops._h``)."""
    return handle_registry.try_format(kind, ref_id) or f"{kind}:{ref_id}"


def _default_show_memories(data: dict[str, Any]) -> bool:
    """The "Show memories" toggle's default (decisions log, 2026-10-06):
    ON when the focused neighbourhood is mainly memory refs — the centre
    is a memory, or memory is the strict majority kind among its direct
    links."""
    if data["focus"]["kind"] == "memory":
        return True
    total = sum(n for kinds in data["counts"].values() for n in kinds.values())
    mem = sum(kinds.get("memory", 0) for kinds in data["counts"].values())
    return mem > total - mem


def _without_memories(data: dict[str, Any]) -> dict[str, Any]:
    """Drop memory-kind nodes, their edges and their count buckets — the
    panel's "Show memories" OFF state. Counts stay consistent with the
    rendered edge set so group totals never claim hidden rows."""
    gone = {_handle("memory", n["id"]) for n in data["nodes"] if n["kind"] == "memory"}
    counts = {
        rel: {k: n for k, n in kinds.items() if k != "memory"}
        for rel, kinds in data["counts"].items()
    }
    return {
        **data,
        "nodes": [n for n in data["nodes"] if n["kind"] != "memory"],
        "edges": [
            e for e in data["edges"] if e["src"] not in gone and e["dst"] not in gone
        ],
        "counts": {rel: kinds for rel, kinds in counts.items() if kinds},
    }


def _panel_groups(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Heading → rel → kind → rows, the panel's render shape.

    Kind counts come from ``data['counts']`` (every matching edge, the
    honest totals); rows come from ``data['edges']`` (the capped window).
    ``rows_total`` counts the returned rows — when ``n`` exceeds it the
    neighbourhood was truncated and the group still expands (to its
    "beyond the cap" per-kind line). ``slug`` is a DOM-id-safe heading.
    """
    nodes = {_handle(n["kind"], n["id"]): n for n in data["nodes"]}
    rows_by_rel: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for e in data["edges"]:
        far = nodes.get(e["dst"] if e["dir"] == "out" else e["src"])
        if far is None:
            continue
        row = {
            "dir": e["dir"],
            "kind": far["kind"],
            "handle": _handle(far["kind"], far["id"]),
            "label": far["label"],
            "url": ref_url(far["kind"], far["id"]),
            "graph_url": f"/graph/{far['kind']}/{far['id']}",
            "state": far.get("state") or "live",
        }
        rows_by_rel.setdefault(e["rel"], {}).setdefault(far["kind"], []).append(row)

    groups: list[dict[str, Any]] = []
    for g in ring_groups(data["counts"]):
        rels: list[dict[str, Any]] = []
        for rel in g["rels"]:
            kind_counts = data["counts"][rel]
            rels.append(
                {
                    "rel": rel,
                    "n": sum(kind_counts.values()),
                    "kinds": [
                        {
                            "kind": k,
                            "n": n,
                            "rows": rows_by_rel.get(rel, {}).get(k, []),
                        }
                        for k, n in sorted(kind_counts.items())
                    ],
                }
            )
        groups.append(
            {
                "heading": g["heading"],
                "slug": re.sub(r"[^a-z0-9]+", "-", g["heading"].lower()).strip("-"),
                "n": g["n"],
                "rels": rels,
                "rows_total": sum(len(k["rows"]) for r in rels for k in r["kinds"]),
            }
        )
    return groups


def _cap_group_rows(g: dict[str, Any]) -> None:
    """Trim a group's rendered rows to :data:`PANEL_GROUP_ROWS` (in-place);
    ``hidden_rows`` is the unrendered remainder that "expand" reveals."""
    left = PANEL_GROUP_ROWS
    for r in g["rels"]:
        for k in r["kinds"]:
            rows = k["rows"]
            k["rows"] = rows[: max(left, 0)]
            left -= len(k["rows"])
    g["hidden_rows"] = g["rows_total"] - sum(
        len(k["rows"]) for r in g["rels"] for k in r["kinds"]
    )


@router.get("/{kind}/{ref_id}", response_class=HTMLResponse)
async def graph_focus(
    request: Request,
    kind: str,
    ref_id: int,
    cap: int = DEFAULT_CAP,
) -> HTMLResponse:
    """The focus page (slice 3): the depth-2 neighbourhood as one
    server-rendered radial SVG — focus centre, hop 1 and hop 2 rings,
    kinds as node shapes, rels as edge labels, every node a link to its
    own ``/graph/...`` page. Nodes past the cap collapse to count chips
    on the ring they would have filled. ``cap`` bounds hop-1 nodes,
    :data:`DEFAULT_CAP` default / :data:`MAX_CAP` max, as the JSON's."""
    store = get_store(request)
    try:
        if not 1 <= cap <= MAX_CAP:
            raise BadInput(f"cap must be between 1 and {MAX_CAP}, got {cap!r}")
        data = store.neighbourhood(kind, ref_id, depth=2, cap=cap)
    except NotFound as exc:
        return templates.TemplateResponse(
            request,
            "error.html.j2",
            {"title": "Not found", "status": 404, "detail": str(exc)},
            status_code=404,
        )
    except BadInput as exc:
        return templates.TemplateResponse(
            request,
            "error.html.j2",
            {"title": "Bad request", "status": 400, "detail": str(exc)},
            status_code=400,
        )
    focus_h = _handle(kind, ref_id)
    n_hop1 = sum(1 for n in data["nodes"] if not n.get("hop"))
    legend = sorted(
        {n["kind"] for n in data["nodes"]} | {kind},
        key=lambda k: (KIND_SHAPES.get(k, "circle"), k),
    )
    return templates.TemplateResponse(
        request,
        "graph/focus.html.j2",
        {
            "kind": kind,
            "ref_id": ref_id,
            "handle": focus_h,
            "focus_label": data["focus"]["label"],
            "reader_url": ref_url(kind, ref_id),
            "json_url": f"/graph/{kind}/{ref_id}.json",
            "eye_url": f"/eye/{focus_h}",
            "svg": focus_graph_svg(data),
            "n_hop1": n_hop1,
            "n_hop2": len(data["nodes"]) - n_hop1,
            "n_edges": len(data["edges"]),
            "truncated": data["truncated"],
            "cap": cap,
            "legend": legend,
        },
    )


@router.get("/{kind}/{ref_id}/panel", response_class=HTMLResponse)
async def neighbourhood_panel(
    request: Request,
    kind: str,
    ref_id: int,
    group: str = "",
    memories: int | None = None,
) -> HTMLResponse:
    """The Neighbourhood panel's HTMX fragment (slice 2).

    Default response: every ring group, rows capped at
    :data:`PANEL_GROUP_ROWS`. ``group=<heading>`` answers one group's
    expand — that group alone, every returned row. ``memories`` carries
    the toggle's explicit state (the radio posts 0|1); absent →
    :func:`_default_show_memories`. A 404 is a small fragment, not the
    error page: the swap target is a panel div.
    """
    store = get_store(request)
    try:
        data = store.neighbourhood(kind, ref_id, depth=1, cap=MAX_CAP)
    except NotFound:
        return HTMLResponse(
            '<p class="text-xs text-slate-400">ref not found.</p>',
            status_code=404,
        )
    show = _default_show_memories(data) if memories is None else bool(memories)
    if not show:
        data = _without_memories(data)
    groups = _panel_groups(data)
    group_only = group.strip()
    if group_only:
        groups = [g for g in groups if g["heading"] == group_only]
    else:
        for g in groups:
            _cap_group_rows(g)
    return templates.TemplateResponse(
        request,
        "graph/_panel.html.j2",
        {
            "kind": kind,
            "ref_id": ref_id,
            "show_memories": show,
            "groups": groups,
            "group_only": group_only,
            "truncated": data["truncated"],
        },
    )
