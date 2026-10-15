"""Graph navigation — the link neighbourhood of one ref, as JSON and HTML.

Routes:

* ``GET /graph/{kind}/{ref_id}.json`` — :meth:`Store.neighbourhood` for the
  ref (``store/_links_ops.py``), the one shape the neighbourhood panel, the
  graph focus page and agent tooling all read. Query params: ``depth``
  (``1``|``2``), ``rels`` / ``kinds`` (comma lists; ``rels`` speaks the
  presented slug, so an inbound ``cites`` is ``cited-by``), ``since`` /
  ``until`` (parsed as Drive does, ``routes/items.py::_parse_date``),
  ``trust`` (the finding tier vocabulary,
  :data:`precis.store.types.FINDING_TRUST_TIERS`) and ``cap`` (hop-1 node
  cap, default :data:`DEFAULT_CAP`, at most :data:`MAX_CAP`).
* ``GET /graph/{kind}/{ref_id}/panel`` — the HTMX fragment behind the
  Neighbourhood panel on the shared ref detail page
  (``templates/refs/detail.html.j2``): the hop-1 ring grouped by
  ``ring_group`` heading → rel → kind, with per-group "expand"
  (``?group=<heading>`` returns that one group uncapped) and the
  "Show memories" radio (``?memories=0|1``; absent → the spec default,
  :func:`_default_show_memories`). The panel is hop-1 by contract —
  ``depth`` is the focus page's axis and is not taken here.
* ``GET /graph/{kind}/{ref_id}`` — the focus page: the neighbourhood as a
  server-rendered radial SVG (``precis_web.graph_svg.focus_graph_svg``),
  node click re-focusing on that node's own ``/graph/...`` page; ``depth``
  defaults to 2. ``?view=outline`` (gr486410) renders the same filtered
  neighbourhood as a copyable bullet list — hop-1 rows under the panel's
  ring-group headings, hop-2 nested under its anchor, retired endpoints
  as tombstones (``include_retired``).
* ``GET /graph/path?from=&to=&depth=&rels=`` — the shortest typed-link
  path between two refs (:meth:`Store.link_path`, depth ≤ 4), as a chain
  of ref cards; the focus page carries a "path to…" form into it.

Every focus page advances the focus trail (slice 6): a cookie-held
``kind:id|…`` list (:data:`TRAIL_COOKIE`, no server storage) rendered as
crumbs plus a "retreat" link to the previous focus; revisiting a crumb
truncates the trail back to it.

Slice 4 (``docs/backlog/web-graph-navigation.md``): the filter axes —
``rels``, ``kinds``, ``since``/``until``, ``trust`` and ``depth`` — are
one parsed :class:`_Filters` applied to the same
:meth:`Store.neighbourhood` call on all three surfaces, so a filter set
narrows the JSON, the panel and the page identically. The URL is the
state; the chips rendered above the panel and the SVG reflect it (each
chip is the same URL minus one value). ``trust`` reaches the browser now
that the tier predicate is single-sourced
(``precis.nanopub.overview.passes_trust``).

On top of the store shape the JSON route adds ``groups``: the hop-1
relations bucketed under the fisheye ring headings
(``utils/refeye.py::ring_group``), ``"Other"`` for a relation no ring
follows — the heading order a browser panel renders in.

Errors are JSON too (``{"error": ...}``) on ``.json`` — 404 for a missing
ref, 400 for a bad argument — not the HTML error page the ``PrecisError``
handler renders, since the consumer is a fetch, not a tab. The HTML routes
render the shared error template / a small fragment instead.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from precis.errors import BadInput, NotFound
from precis.store._links_ops import PATH_MAX_DEPTH
from precis.store.types import FINDING_TRUST_TIERS
from precis.utils import handle_registry
from precis.utils.refeye import RING_GROUPS, ring_group
from precis_web.deps import get_store, templates
from precis_web.graph_filters import csv_list, parse_kind_filter
from precis_web.graph_svg import KIND_SHAPES, focus_graph_svg
from precis_web.ref_urls import ref_url
from precis_web.routes.favorites import faved
from precis_web.routes.items import _parse_date

if TYPE_CHECKING:
    from precis.store.store import Store

router = APIRouter(prefix="/graph", tags=["graph"])

#: Hop-1 node cap when the request names none (the "60 nodes per page"
#: ruling, 2026-10-06) and the most a request may ask for.
DEFAULT_CAP = 60
MAX_CAP = 200

#: Rows rendered per ring group in the neighbourhood panel before the
#: group's "expand" affordance (the "25 per group" ruling, 2026-10-06).
PANEL_GROUP_ROWS = 25

OTHER_GROUP = "Other"

#: The focus page's presentations (``?view=``): the radial SVG and the
#: hierarchical outline list (gr486410). Empty = radial.
_VIEWS = ("radial", "outline")


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


@dataclass(frozen=True)
class _Filters:
    """The ``/graph`` filter axes shared by the JSON, the panel and the
    focus page (slice 4): one parse and one query-string writer so the
    three surfaces cannot drift.

    ``rels``/``kinds`` are the presented comma lists; ``since``/``until``
    keep the raw box text (``_parse_date`` degrades a half-typed date to
    no-filter, as Drive); ``trust`` is a
    :data:`precis.store.types.FINDING_TRUST_TIERS` tier, validated by the
    store; ``depth``/``cap`` are the focus page's own axes (the panel is
    hop-1 by contract and the JSON defaults itself). ``view`` is the
    page's presentation axis (``outline``, gr486410): carried through
    :meth:`qs`/chips like a filter but never sent to the store.
    """

    rels: tuple[str, ...] = ()
    kinds: tuple[str, ...] = ()
    since: str = ""
    until: str = ""
    trust: str = ""
    depth: int | None = None
    cap: int | None = None
    view: str = ""

    def qs(self, *, remove: Iterable[tuple[str, str]] = ()) -> str:
        """This filter set as a query string (no leading ``?``), minus the
        ``(axis, value)`` pairs in ``remove`` — a chip's "×" is the same
        URL one value narrower."""
        gone = set(remove)
        parts: list[str] = []
        for axis, vals in (("rels", self.rels), ("kinds", self.kinds)):
            kept = [v for v in vals if (axis, v) not in gone]
            if kept:
                parts.append(f"{axis}={quote(','.join(kept), safe=',')}")
        for axis in ("since", "until", "trust", "view"):
            v: str = getattr(self, axis)
            if v and (axis, v) not in gone:
                parts.append(f"{axis}={quote(v)}")
        if self.depth is not None and ("depth", str(self.depth)) not in gone:
            parts.append(f"depth={self.depth}")
        if (
            self.cap not in (None, DEFAULT_CAP)
            and (
                "cap",
                str(self.cap),
            )
            not in gone
        ):
            parts.append(f"cap={self.cap}")
        return "&".join(parts)

    @property
    def filters_on(self) -> bool:
        """True when a narrowing axis is set — drives the filtered empty
        state ("no links match the filters", not "no typed links").
        ``view`` is presentation, not narrowing; ``depth``/``cap`` count
        only when non-default."""
        return bool(
            self.rels
            or self.kinds
            or self.since
            or self.until
            or self.trust
            or (self.depth is not None and self.depth != 2)
            or (self.cap is not None and self.cap != DEFAULT_CAP)
        )

    def _narrow_drops(self) -> list[tuple[str, str]]:
        """Every active narrowing pair — ``qs(remove=…)`` with this clears
        the filters and keeps presentation state (``view``)."""
        drops = [("rels", v) for v in self.rels] + [("kinds", v) for v in self.kinds]
        for axis in ("since", "until", "trust"):
            v: str = getattr(self, axis)
            if v:
                drops.append((axis, v))
        if self.depth is not None:
            drops.append(("depth", str(self.depth)))
        if self.cap is not None and self.cap != DEFAULT_CAP:
            drops.append(("cap", str(self.cap)))
        return drops

    def clear_href(self, base: str) -> str:
        """``base`` with every narrowing axis dropped (``view`` kept)."""
        q = self.qs(remove=self._narrow_drops())
        return f"{base}?{q}" if q else base

    def chips(
        self, base: str, *, hx_base: str = "", hx_extra: str = ""
    ) -> list[dict[str, str]]:
        """One chip per active filter value — ``href`` is ``base`` minus
        that value (the URL is the state, the chips reflect it), ``hx``
        the same narrowing against the panel fragment when the chips live
        inside it — plus ``clear`` when more than one is on."""

        def _chip(label: str, drop: list[tuple[str, str]]) -> dict[str, str]:
            q = self.qs(remove=drop)
            hx = ""
            if hx_base:
                rest = [p for p in (q, hx_extra) if p]
                hx = f"{hx_base}?{'&'.join(rest)}" if rest else hx_base
            return {
                "label": label,
                "href": f"{base}?{q}" if q else base,
                "hx": hx,
            }

        out = [_chip(f"rel:{v}", [("rels", v)]) for v in self.rels]
        out += [_chip(f"kind:{v}", [("kinds", v)]) for v in self.kinds]
        for axis, v in (
            ("since", self.since),
            ("until", self.until),
            ("trust", self.trust),
        ):
            if v:
                out.append(_chip(f"{axis}:{v}", [(axis, v)]))
        if self.depth is not None and self.depth != 2:
            out.append(_chip(f"depth:{self.depth}", [("depth", str(self.depth))]))
        if len(out) > 1:
            out.append(_chip("clear", self._narrow_drops()))
        return out


def _parse_filters(
    *,
    rels: str,
    kinds: str,
    since: str,
    until: str,
    trust: str,
    depth: int | None = None,
    cap: int | None = None,
    view: str = "",
) -> _Filters:
    """Query params → :class:`_Filters`. ``kinds`` is registry-validated
    (``/eye``'s :func:`parse_kind_filter`) so an unknown kind is a 400 on
    every surface, not a silent empty page; ``trust`` stays raw — the
    store owns tier validation; ``view`` is the focus page's presentation
    axis (``outline``, ``gr486410``)."""
    try:
        kind_set = parse_kind_filter(kinds)
    except ValueError as exc:
        raise BadInput(str(exc)) from exc
    view_v = view.strip().lower()
    if view_v and view_v not in _VIEWS:
        raise BadInput(f"unknown view={view!r}", options=list(_VIEWS))
    return _Filters(
        rels=tuple(csv_list(rels) or ()),
        kinds=tuple(sorted(kind_set)) if kind_set else (),
        since=since.strip(),
        until=until.strip(),
        trust=trust.strip().lower(),
        depth=depth,
        cap=cap,
        view="" if view_v == "radial" else view_v,
    )


def _neighbourhood(
    store: Store,
    kind: str,
    ref_id: int,
    f: _Filters,
    *,
    depth: int,
    cap: int,
    include_retired: bool = False,
) -> dict[str, Any]:
    """The one read all three surfaces share — same filters in, same
    nodes/edges out. ``include_retired`` (the outline view) keeps retired
    endpoints as tombstones."""
    return store.neighbourhood(
        kind,
        ref_id,
        depth=depth,
        rels=list(f.rels) or None,
        kinds=list(f.kinds) or None,
        since=_parse_date(f.since),
        until=_parse_date(f.until),
        trust=f.trust or None,
        cap=cap,
        include_retired=include_retired,
    )


_ENDPOINT_RE = re.compile(r"^([a-z][a-z0-9-]*)[/:](\d+)$")


def _parse_endpoint(text: str, axis: str) -> tuple[str, int]:
    """A path endpoint as ``<kind>/<id>`` (the focus page's own URL tail),
    ``<kind>:<id>`` or a handle (``me474312``) → ``(kind, ref_id)``."""
    raw = text.strip().strip("/")
    if raw.startswith("graph/"):
        raw = raw[len("graph/") :]
    m = _ENDPOINT_RE.match(raw)
    if m:
        return m.group(1), int(m.group(2))
    parsed = handle_registry.parse(raw)
    if parsed is not None and not parsed[1]:
        return parsed[0], parsed[2]
    raise BadInput(
        f"{axis}={text!r} is not a ref: give <kind>/<id> or a handle such as me474312"
    )


def _card(ref: Any) -> dict[str, Any]:
    """One ref card for the path chain and the trail."""
    return {
        "kind": ref.kind,
        "id": ref.id,
        "handle": _handle(ref.kind, ref.id),
        "label": ref.title or _handle(ref.kind, ref.id),
        "url": ref_url(ref.kind, ref.id),
        "graph_url": f"/graph/{ref.kind}/{ref.id}",
    }


#: The focus trail (slice 6): a cookie-held list of the last focus pages,
#: no server storage (Reto 2026-10-06: "cookie is adequate for now").
#: Value ``kind:id|kind:id|…``, oldest first — every character is legal in
#: a bare cookie value, so it travels unquoted.
TRAIL_COOKIE = "graph_trail"
TRAIL_MAX = 8
_TRAIL_ITEM_RE = re.compile(r"^([a-z][a-z0-9-]*):(\d+)$")


def _read_trail(request: Request) -> list[tuple[str, int]]:
    """The trail cookie as ``[(kind, id), …]``, oldest first; malformed
    items are dropped (the cookie is client-held, so it is untrusted)."""
    out: list[tuple[str, int]] = []
    for item in request.cookies.get(TRAIL_COOKIE, "").split("|"):
        m = _TRAIL_ITEM_RE.match(item)
        if m:
            out.append((m.group(1), int(m.group(2))))
    return out[-TRAIL_MAX:]


def _advance_trail(
    trail: list[tuple[str, int]], here: tuple[str, int]
) -> list[tuple[str, int]]:
    """``trail`` after visiting ``here``: a node already on it truncates
    the trail back to that node (a retreat or a crumb click), a new one is
    appended, oldest dropped past :data:`TRAIL_MAX`."""
    if here in trail:
        return trail[: trail.index(here) + 1]
    return [*trail, here][-TRAIL_MAX:]


def _trail_cards(store: Store, trail: list[tuple[str, int]]) -> list[dict[str, Any]]:
    """The trail as ref cards, one batched read; retired or vanished refs
    (and a kind that no longer matches the id) drop out."""
    refs = store.fetch_refs_by_ids([i for _, i in trail], include_deleted=False)
    return [_card(refs[i]) for k, i in trail if i in refs and refs[i].kind == k]


@router.get("/path", response_class=HTMLResponse)
async def graph_path(
    request: Request,
    from_: str = Query("", alias="from"),
    to: str = "",
    depth: int = PATH_MAX_DEPTH,
    rels: str = "",
) -> HTMLResponse:
    """Path finding (slice 5): the shortest typed-link path between two
    refs within ``depth`` links (at most :data:`PATH_MAX_DEPTH`, the
    2026-10-06 ruling), via :meth:`Store.link_path` — links walked either
    way, ``rels`` narrowing to the listed slugs (either slug of an inverse
    pair). Rendered as a chain of ref cards, the relation on each arrow and
    the arrow in the link's stored direction. "No path within depth N" is
    an answer (200), not an error; a malformed endpoint is a 400 and a
    missing or retired one a 404. With neither endpoint given the page is
    the empty form."""
    store = get_store(request)
    ctx: dict[str, Any] = {
        "from_": from_,
        "to": to,
        "depth": depth,
        "rels": rels,
        "max_depth": PATH_MAX_DEPTH,
        "asked": False,
        "trail": _trail_cards(store, _read_trail(request)),
    }
    if not from_.strip() and not to.strip():
        return templates.TemplateResponse(request, "graph/path.html.j2", ctx)
    try:
        if not 1 <= depth <= PATH_MAX_DEPTH:
            raise BadInput(
                f"depth must be between 1 and {PATH_MAX_DEPTH}, got {depth!r}"
            )
        ends = [_parse_endpoint(from_, "from"), _parse_endpoint(to, "to")]
        refs = []
        for k, i in ends:
            ref = store.get_ref(kind=k, id=i)
            if ref is None:
                raise NotFound(f"no live {k} ref with id {i}")
            refs.append(ref)
        found = store.link_path(
            refs[0].id, refs[1].id, depth=depth, rels=csv_list(rels) or None
        )
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
    steps: list[dict[str, Any]] = []
    if found["ids"] is not None:
        by_id = store.fetch_refs_by_ids(found["ids"])
        cards = [_card(by_id[i]) for i in found["ids"]]
        for i, (s, _d, rel) in enumerate(found["links"]):
            forward = s == found["ids"][i]
            steps.append(
                {
                    "card": cards[i],
                    "rel": rel,
                    "forward": forward,
                    "presented": rel
                    if forward
                    else (store.inverse_relation(rel) or rel),
                }
            )
        steps.append({"card": cards[-1]})
    ctx.update(
        asked=True,
        ends=[_card(r) for r in refs],
        steps=steps,
        hops=len(found["links"]),
        budget_hit=found["budget_hit"],
    )
    return templates.TemplateResponse(request, "graph/path.html.j2", ctx)


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
    trust: str = "",
    cap: int = DEFAULT_CAP,
) -> JSONResponse:
    """The ref's link neighbourhood, see the module docstring for the shape."""
    store = get_store(request)
    try:
        if not 1 <= cap <= MAX_CAP:
            raise BadInput(f"cap must be between 1 and {MAX_CAP}, got {cap!r}")
        f = _parse_filters(
            rels=rels, kinds=kinds, since=since, until=until, trust=trust
        )
        data = _neighbourhood(store, kind, ref_id, f, depth=depth, cap=cap)
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


def _row(n: dict[str, Any], e: dict[str, Any]) -> dict[str, Any]:
    """One renderable row for neighbourhood node ``n`` reached by edge
    ``e`` — shared by the panel and the outline (which also reads
    ``rel``/``retired``/``kids``; the panel template ignores the extras)."""
    return {
        "dir": e["dir"],
        "rel": e["rel"],
        "kind": n["kind"],
        "handle": _handle(n["kind"], n["id"]),
        "label": n["label"],
        "url": ref_url(n["kind"], n["id"]),
        "graph_url": f"/graph/{n['kind']}/{n['id']}",
        "state": n.get("state") or "live",
        "retired": bool(n.get("retired")),
        "kids": [],
    }


def _panel_groups(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Heading → rel → kind → rows, the panel's render shape.

    Kind counts come from ``data['counts']`` (every matching edge, the
    honest totals); rows come from ``data['edges']`` (the capped window,
    hop-1 only — a depth-2 neighbourhood's second-hop edges are the
    outline's ``kids``, not top-level rows). ``rows_total`` counts the
    returned rows — when ``n`` exceeds it the neighbourhood was
    truncated and the group still expands (to its "beyond the cap"
    per-kind line). ``slug`` is a DOM-id-safe heading.
    """
    nodes = {_handle(n["kind"], n["id"]): n for n in data["nodes"]}
    rows_by_rel: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for e in data["edges"]:
        if e.get("hop"):
            continue
        far = nodes.get(e["dst"] if e["dir"] == "out" else e["src"])
        if far is None:
            continue
        rows_by_rel.setdefault(e["rel"], {}).setdefault(far["kind"], []).append(
            _row(far, e)
        )

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


def _outline_groups(data: dict[str, Any]) -> list[dict[str, Any]]:
    """The outline view's rows (gr486410): the panel's heading → rel →
    kind → rows shape, each hop-1 row carrying its hop-2 children under
    ``kids`` — nested under the first anchor the edge names (the SVG's
    own rule), never back at the focus. A hop-2 node reached via several
    anchors nests once, under the first."""
    nodes = {_handle(n["kind"], n["id"]): n for n in data["nodes"]}
    kids: dict[str, list[dict[str, Any]]] = {}
    claimed: set[str] = set()
    for e in data["edges"]:
        if e.get("hop") != 2:
            continue
        anchor = e["src"] if e["dir"] == "out" else e["dst"]
        far_h = e["dst"] if e["dir"] == "out" else e["src"]
        if far_h in claimed or far_h not in nodes:
            continue
        claimed.add(far_h)
        kids.setdefault(anchor, []).append(_row(nodes[far_h], e))
    groups = _panel_groups(data)
    for g in groups:
        for r in g["rels"]:
            for k in r["kinds"]:
                for row in k["rows"]:
                    row["kids"] = kids.get(row["handle"], [])
    return groups


@router.get("/{kind}/{ref_id}", response_class=HTMLResponse)
async def graph_focus(
    request: Request,
    kind: str,
    ref_id: int,
    depth: int | None = None,
    rels: str = "",
    kinds: str = "",
    since: str = "",
    until: str = "",
    trust: str = "",
    cap: int = DEFAULT_CAP,
    view: str = "",
) -> HTMLResponse:
    """The focus page (slices 3–4, gr486410): the neighbourhood as one
    server-rendered radial SVG — focus centre, hop 1 and (``depth=2``,
    the default) hop 2 rings, kinds as node shapes, rels as edge labels,
    every node a link to its own ``/graph/...`` page — or, at
    ``?view=outline``, as a hierarchical bullet list: hop-1 rows under
    the panel's ring-group headings, hop-2 nested under its anchor,
    retired endpoints as tombstones (``include_retired``). Nodes past
    the cap collapse to count chips on the ring they would have filled.
    ``cap`` bounds hop-1 nodes, :data:`DEFAULT_CAP` default /
    :data:`MAX_CAP` max, as the JSON's. The filter form is a plain GET;
    the chips are the same URL one value narrower."""
    store = get_store(request)
    try:
        if not 1 <= cap <= MAX_CAP:
            raise BadInput(f"cap must be between 1 and {MAX_CAP}, got {cap!r}")
        f = _parse_filters(
            rels=rels,
            kinds=kinds,
            since=since,
            until=until,
            trust=trust,
            depth=depth,
            cap=cap,
            view=view,
        )
        data = _neighbourhood(
            store,
            kind,
            ref_id,
            f,
            depth=depth if depth is not None else 2,
            cap=cap,
            include_retired=f.view == "outline",
        )
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
    page = f"/graph/{kind}/{ref_id}"
    trail = _trail_cards(store, _advance_trail(_read_trail(request), (kind, ref_id)))
    qs = f.qs()
    json_qs = f.qs(remove=[("view", f.view)]) if f.view else qs
    other = "radial" if f.view == "outline" else "outline"
    q_less = f.qs(remove=[("view", f.view)] if f.view else [])
    view_href = f"{page}?{q_less + '&' if q_less else ''}view={other}"
    n_hop1 = sum(1 for n in data["nodes"] if not n.get("hop"))
    legend = sorted(
        {n["kind"] for n in data["nodes"]} | {kind},
        key=lambda k: (KIND_SHAPES.get(k, "circle"), k),
    )
    response = templates.TemplateResponse(
        request,
        "graph/focus.html.j2",
        {
            "kind": kind,
            "ref_id": ref_id,
            "handle": focus_h,
            "focus_label": data["focus"]["label"],
            "focus_state": data["focus"].get("state", "live"),
            "focus_retired": bool(data["focus"].get("retired")),
            "reader_url": ref_url(kind, ref_id),
            "json_url": f"{page}.json" + (f"?{json_qs}" if json_qs else ""),
            "eye_url": f"/eye/{focus_h}",
            "svg": "" if f.view else focus_graph_svg(data),
            "outline": _outline_groups(data) if f.view == "outline" else None,
            "view": f.view or "radial",
            "view_href": view_href,
            "view_other": other,
            "n_hop1": n_hop1,
            "n_hop2": len(data["nodes"]) - n_hop1,
            "n_edges": len(data["edges"]),
            "counts2": data.get("counts2") or {},
            "truncated": data["truncated"],
            "cap": cap,
            "legend": legend,
            "f": f,
            "qs": qs,
            "chips": f.chips(page),
            "trust_tiers": FINDING_TRUST_TIERS,
            "filters_on": f.filters_on,
            "clear_url": f.clear_href(page),
            # Heart toggle (gr486409) — None when auth is off, so the
            # template skips the button rather than rendering a dead one.
            "faved": faved(request, ref_id),
            "trail": trail,
            "retreat": trail[-2] if len(trail) > 1 else None,
        },
    )
    response.set_cookie(
        TRAIL_COOKIE,
        "|".join(f"{c['kind']}:{c['id']}" for c in trail),
        path="/graph",
        httponly=True,
        samesite="lax",
    )
    return response


@router.get("/{kind}/{ref_id}/panel", response_class=HTMLResponse)
async def neighbourhood_panel(
    request: Request,
    kind: str,
    ref_id: int,
    group: str = "",
    memories: int | None = None,
    rels: str = "",
    kinds: str = "",
    since: str = "",
    until: str = "",
    trust: str = "",
) -> HTMLResponse:
    """The Neighbourhood panel's HTMX fragment (slices 2 + 4).

    Default response: every ring group, rows capped at
    :data:`PANEL_GROUP_ROWS`. ``group=<heading>`` answers one group's
    expand — that group alone, every returned row. ``memories`` carries
    the toggle's explicit state (the radio posts 0|1); absent →
    :func:`_default_show_memories`. ``rels``/``kinds``/``since``/
    ``until``/``trust`` are the shared filter axes (the panel is hop-1 by
    contract, so there is no ``depth`` here). A 404/400 is a small
    fragment, not the error page: the swap target is a panel div.
    """
    store = get_store(request)
    try:
        f = _parse_filters(
            rels=rels, kinds=kinds, since=since, until=until, trust=trust
        )
        data = _neighbourhood(store, kind, ref_id, f, depth=1, cap=MAX_CAP)
    except NotFound:
        return HTMLResponse(
            '<p class="text-xs text-slate-400">ref not found.</p>',
            status_code=404,
        )
    except BadInput as exc:
        return HTMLResponse(
            f'<p class="text-xs text-rose-600">{html.escape(str(exc))}</p>',
            status_code=400,
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
    qs = f.qs()
    page = f"/graph/{kind}/{ref_id}"
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
            "f": f,
            "qs": qs,
            "chips": f.chips(
                page,
                hx_base=f"{page}/panel",
                hx_extra=f"memories={1 if show else 0}",
            ),
            "trust_tiers": FINDING_TRUST_TIERS,
            "filters_on": bool(qs),
        },
    )
