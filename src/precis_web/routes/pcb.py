"""PCB tab — browse the ``pcb`` kind: board render + schematic.

The pcb kind is otherwise a text/MCP surface (the LLM authors a netlist
and reads graphs, never pixels). This route is the human affordance on the
same data:

* ``GET  /pcb`` — the design list.
* ``GET  /pcb/{slug}`` — one design: the fab-level board render beside the
  net-label schematic, with the netlist/route/DRC vitals above.
* ``GET  /pcb/{slug}/board.svg`` — the fab SVG (embedded via ``<object>``
  so its layer-toggle legend script keeps working).
* ``GET  /pcb/{slug}/schematic.svg`` — the net-label schematic
  (:mod:`precis.pcb.schematic` — placement-free, renders from day one).

Both SVG endpoints delegate to the SAME code the MCP surface serves
(``PcbHandler.get(view='svg'|'schematic')``) rather than re-assembling
renders here — one rule, one call site; the board picture a human sees is
byte-identical to the one an agent pulls.
"""

from __future__ import annotations

import collections
import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.responses import Response as RawResponse

from precis.dispatch import Hub
from precis.errors import NotFound
from precis.handlers._slug_ref_shared import resolve_live_slug_ref
from precis.handlers.pcb import PcbHandler
from precis_web.deps import get_store, templates

if TYPE_CHECKING:
    from precis.store.store import Store

router = APIRouter(tags=["pcb"])

log = logging.getLogger(__name__)

_LIST_LIMIT = 100


def _handler(store: Store) -> PcbHandler:
    # Hub is a light composition object; per-request construction is the
    # same pattern the render fixture uses. Handler registration is
    # self-contained — nothing global is mutated beyond this Hub instance.
    return PcbHandler(hub=Hub(store=store))


def _vitals(store: Store, ref_id: int) -> dict[str, Any]:
    """The numbers a reader wants above the pictures: part/net counts,
    route status, and the latest DRC error tally."""
    design = store.pcb_load(ref_id)
    _run, findings = store.pcb_drc_findings_latest(ref_id)
    drc = collections.Counter(
        str(f["rule"]) for f in findings if f["severity"] == "error"
    )
    return {
        "n_parts": len(design["instances"]),
        "n_nets": len(design["nets"]),
        "route_status": design.get("route_status") or {},
        "drc": dict(sorted(drc.items())),
    }


@router.get("/pcb", response_class=HTMLResponse)
async def pcb_list(request: Request) -> HTMLResponse:
    store = get_store(request)
    refs = store.list_refs(kind="pcb", limit=_LIST_LIMIT)
    rows = [
        {"slug": r.slug, "title": r.title or r.slug, "handle": f"pc{r.id}"}
        for r in refs
    ]
    return templates.TemplateResponse(
        request,
        "pcb/list.html.j2",
        {"active_tab": "pcb", "designs": rows},
    )


@router.get("/pcb/{slug}", response_class=HTMLResponse)
async def pcb_detail(request: Request, slug: str) -> HTMLResponse:
    store = get_store(request)
    try:
        ref = resolve_live_slug_ref(store, kind="pcb", id=slug)
    except NotFound:
        return templates.TemplateResponse(
            request,
            "error.html.j2",
            {
                "title": "PCB design not found",
                "detail": f"no live pcb design with slug {slug!r}",
                "status": 404,
            },
            status_code=404,
        )
    ctx = {
        "active_tab": "pcb",
        "slug": ref.slug,
        "title": ref.title or ref.slug,
        **_vitals(store, ref.id),
    }
    return templates.TemplateResponse(request, "pcb/detail.html.j2", ctx)


def _svg_response(svg: str) -> RawResponse:
    return RawResponse(
        content=svg,
        media_type="image/svg+xml",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/pcb/{slug}/board.svg")
async def pcb_board_svg(request: Request, slug: str) -> RawResponse:
    store = get_store(request)
    try:
        ref = resolve_live_slug_ref(store, kind="pcb", id=slug)
    except NotFound:
        return RawResponse(status_code=404, content="not found")
    try:
        resp = _handler(store).get(id=ref.slug, view="svg", args={"level": "fab"})
    except Exception:
        # An unplaced/unrouted design has no fab film set yet — the detail
        # page still shows the schematic; this pane says why it is empty.
        log.debug("pcb board svg render failed for %s", slug, exc_info=True)
        return RawResponse(
            status_code=422,
            content="board not renderable yet (place + route it first)",
        )
    return _svg_response(resp.body)


@router.get("/pcb/{slug}/schematic.svg")
async def pcb_schematic_svg(request: Request, slug: str) -> RawResponse:
    store = get_store(request)
    try:
        ref = resolve_live_slug_ref(store, kind="pcb", id=slug)
    except NotFound:
        return RawResponse(status_code=404, content="not found")
    resp = _handler(store).get(id=ref.slug, view="schematic")
    return _svg_response(resp.body)
