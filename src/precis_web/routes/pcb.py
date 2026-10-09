"""PCB tab — browse the ``pcb`` kind: board render + schematic.

The pcb kind is otherwise a text/MCP surface (the LLM authors a netlist
and reads graphs, never pixels). This route is the human affordance on the
same data:

* ``GET  /pcb`` — retired into the unified Drive surface; redirects to the
  ``kind=pcb`` facet preset.
* ``GET  /pcb/{slug}`` — one design: the fab-level board render beside the
  net-label schematic, with the netlist/route/DRC vitals above.
* ``GET  /pcb/{slug}/board.svg`` — the fab SVG (embedded via ``<object>``
  so its layer-toggle legend script keeps working).
* ``GET  /pcb/{slug}/schematic.svg`` — the net-label schematic
  (:mod:`precis.pcb.schematic` — placement-free, renders from day one).
* ``POST /pcb/{slug}/note`` — argue with the design
  (docs/backlog/pcb-argue-with-design.md). The page's one text box
  accumulates handles as the user clicks the board render (the fab SVG
  stamps ``data-handle`` per :mod:`precis.pcb.argue`'s grammar); submit
  posts ``{text, handles}``. Every clicked handle must resolve against
  the live board — a stale one is a 400 quoting the valid roster, not a
  stored note. The typed text is stored verbatim as a ``question`` note
  with the handles it names in ``about``; the model's reply (one
  :func:`precis.utils.llm.router.route` call with the resolved context)
  is stored as an ``answer`` note re the question. A model failure
  degrades to "recorded, unanswered" — the argument is never lost to an
  outage. Returns the re-rendered notes list. Mirrors ``POST
  /se/{slug}/note`` (``routes/blocktree_view.py``) in access posture:
  ambient store, no layer beyond the page's own.

Both SVG endpoints delegate to the SAME code the MCP surface serves
(``PcbHandler.get(view='svg'|'schematic')``) rather than re-assembling
renders here — one rule, one call site; the board picture a human sees is
byte-identical to the one an agent pulls.
"""

from __future__ import annotations

import asyncio
import collections
import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.responses import Response as RawResponse

from precis.dispatch import Hub
from precis.errors import NotFound
from precis.handlers._slug_ref_shared import resolve_live_slug_ref
from precis.handlers.pcb import PcbHandler
from precis.pcb import argue
from precis_web.deps import get_store, templates

if TYPE_CHECKING:
    from precis.store.store import Store

router = APIRouter(tags=["pcb"])

log = logging.getLogger(__name__)


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
async def pcb_list() -> RedirectResponse:
    """Retired into the unified Drive surface — redirects to the ``kind=pcb``
    facet preset (same target as ``_drive_back.html.j2``'s back-link, so the
    detail page's "back" arrow and this retired index agree). The workbench
    (``/pcb/{slug}`` and its SVG endpoints below) is unaffected.
    """
    return RedirectResponse(url="/drive?k=pcb&folder=*&sort=recency")


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
        **_notes_ctx(store, ref.id),
    }
    return templates.TemplateResponse(request, "pcb/detail.html.j2", ctx)


def _notes_ctx(store: Store, ref_id: int) -> dict[str, Any]:
    """The argument ledger as the notes partial renders it: questions in
    order, each with its answers, dangling anchors reported per note."""
    notes = store.pcb_notes_list(ref_id)
    valid = store.pcb_handles(ref_id)
    for n in notes:
        n["dangling"] = set(argue.dangling(n["about"], valid))
    by_re: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for n in notes:
        if n["re"]:
            by_re[n["re"]].append(n)
    threads = [
        {"note": n, "replies": by_re.get(n["name"], [])} for n in notes if not n["re"]
    ]
    return {"threads": threads, "n_notes": len(notes)}


@router.post("/pcb/{slug}/note")
async def pcb_note_save(request: Request, slug: str) -> JSONResponse:
    store = get_store(request)
    try:
        ref = resolve_live_slug_ref(store, kind="pcb", id=slug)
    except NotFound:
        return JSONResponse({"error": f"no live pcb design {slug!r}"}, status_code=404)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "body must be JSON"}, status_code=400)
    text = str(payload.get("text") or "")
    if not text.strip():
        return JSONResponse({"error": "empty argument"}, status_code=400)
    raw = payload.get("handles") or []
    if not isinstance(raw, list) or not all(isinstance(h, str) for h in raw):
        return JSONResponse(
            {"error": "handles must be a list of strings"}, status_code=400
        )
    clicked = [h.strip() for h in raw if h.strip()]

    def _save() -> dict[str, Any]:
        resolved, unknown = argue.resolve(store, ref.id, clicked)
        if unknown:
            return {
                "status": 400,
                "error": "unknown handle(s): " + ", ".join(unknown),
                "unknown": unknown,
                "valid": argue.all_handles(store.pcb_handles(ref.id)),
            }
        # The handles the sentence names: clicked first, then any typed by
        # hand that resolve — one list, the user's order, no duplicates.
        about = list(clicked)
        about += [h for h in argue.handles_in(text, store, ref.id) if h not in about]
        known = {r.handle for r in resolved}
        extra, _ = argue.resolve(store, ref.id, [h for h in about if h not in known])
        resolved.extend(extra)
        taken = {n["name"] for n in store.pcb_notes_list(ref.id)}
        q_name = argue.note_name("question", text, taken)
        store.pcb_note_insert(
            ref.id, name=q_name, kind="question", body=text, about=about
        )
        out: dict[str, Any] = {"ok": True, "name": q_name, "about": about}
        try:
            answer = argue.ask(
                title=str(ref.title or ref.slug),
                text=text,
                resolved=resolved,
                vitals=_vitals(store, ref.id),
                ref_id=ref.id,
            )
        except Exception as exc:
            # Degrade honestly: the question is on record; the answer is
            # not, and the page says so.
            log.warning("pcb argue: model call failed for %s: %s", slug, exc)
            out.update(answer=None, degraded=True, detail=str(exc))
            return out
        a_name = argue.note_name("answer", answer, taken | {q_name})
        store.pcb_note_insert(
            ref.id,
            name=a_name,
            kind="answer",
            body=answer,
            re=q_name,
            about=about,
            origin="proposed",
        )
        out.update(answer=a_name, degraded=False)
        return out

    try:
        result = await asyncio.to_thread(_save)
    except Exception as exc:
        log.exception("pcb note save failed for %s", slug)
        return JSONResponse({"error": str(exc)}, status_code=400)
    status = int(result.pop("status", 200))
    if status != 200:
        return JSONResponse(result, status_code=status)
    html = templates.get_template("pcb/_notes.html.j2").render(
        request=request, slug=ref.slug, **_notes_ctx(store, ref.id)
    )
    return JSONResponse({**result, "html": html})


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
