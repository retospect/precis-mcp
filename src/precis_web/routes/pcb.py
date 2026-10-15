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
* ``GET  /pcb/{slug}/view/{name}`` — one read view as text (the quest
  board panel's links, gr488206): route-status, congestion, stackup and
  drc off ``get(kind='pcb', view=…)``, and ``tapeout`` — the
  ``pcb-tapeout`` checklist status for this board.
* ``POST /pcb/{slug}/op`` — the quest board panel's action buttons:
  ``route`` (``corridor=``/``escape=`` opt-ins) and ``place``
  (``groups=``) queue the job through ``put(args={'op': …})``, the same
  dispatch an agent calls; ``review`` runs a fresh ``view='check'``
  (DRC + ERC, recorded) until the ``pcb_review`` job exists. The page
  confirms before posting; the route redirects back to ``back`` (a
  ``/refs/quest/`` path only) or renders the handler's error.

Both SVG endpoints delegate to the SAME code the MCP surface serves
(``PcbHandler.get(view='svg'|'schematic')``) rather than re-assembling
renders here — one rule, one call site; the board picture a human sees is
byte-identical to the one an agent pulls by default. The opt-in shown-token
path adds identity attributes without changing fab geometry. A read-only
repeatable view binds page/manifest and SVG; a changed object token refuses.
PCB proposals extend the shared chat and require canonical explicit Apply.
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.responses import Response as RawResponse

from precis.design import history
from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers._slug_ref_shared import resolve_live_slug_ref
from precis.handlers.pcb import PcbHandler
from precis.pcb import argue
from precis_web import design_chat, design_turn, pcb_design, quest_board
from precis_web.deps import (
    await_dispatch,
    get_store,
    redirect_or_error,
    templates,
)

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
    """The numbers a reader wants above the pictures — the shared block
    (:func:`precis.pcb.argue.vitals`) the ``pcb_argue`` job quotes too."""
    return argue.vitals(store, ref_id)


@router.get("/pcb", response_class=HTMLResponse)
async def pcb_list() -> RedirectResponse:
    """Retired into the unified Drive surface — redirects to the ``kind=pcb``
    facet preset (same target as ``_drive_back.html.j2``'s back-link, so the
    detail page's "back" arrow and this retired index agree). The workbench
    (``/pcb/{slug}`` and its SVG endpoints below) is unaffected.
    """
    return RedirectResponse(url="/drive?k=pcb&folder=*&sort=recency")


@router.get("/pcb/{slug}", response_class=HTMLResponse)
async def pcb_detail(request: Request, slug: str, rev: int | None = None) -> Any:
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
    try:
        with store.pcb_view_tx() as conn:
            view = pcb_design.capture_view(store, ref, conn=conn)
            ref = view.ref
            read_only = rev is not None and rev != view.revision
            summary = None
            if read_only:
                assert rev is not None
                revision = history.revision(store, ref.id, rev, conn=conn)
                if revision is None:
                    return RawResponse(status_code=404, content="revision not recorded")
                summary = {
                    "revision": revision.rev,
                    "ops": revision.ops,
                    "turn": revision.turn,
                    "checkpoint_id": revision.checkpoint_id,
                }
            ctx = {
                "active_tab": "pcb",
                "slug": ref.slug,
                "title": ref.title or ref.slug,
                **_vitals(store, ref.id),
                **_notes_ctx(store, ref, conn),
                "pcb_context": view.context,
                "pcb_instances": view.instances,
                "read_only": read_only,
                "revision_summary": summary,
                "rev": rev or view.revision,
                "rev_current": view.revision,
                "chat": design_chat.panel_context(
                    request,
                    kind="pcb",
                    slug=slug,
                    read_only=read_only,
                    conn=conn,
                    context=view.context,
                ),
            }
    except ValueError as exc:
        return design_chat.chat_error(
            request, kind="pcb", slug=slug, detail=str(exc), status=409
        )
    return templates.TemplateResponse(request, "pcb/detail.html.j2", ctx)


def _board_objects(
    store: Store, ref: Any, conn: Any = None
) -> dict[str, dict[str, Any]]:
    """``{key: identity}`` for the via/silk/outline/fiducial objects of the
    current board — the handles :mod:`precis.pcb.argue` resolves against.
    ``{}`` when the board is not renderable (object handles then read as
    unknown on submit, dangling on read)."""

    def _catalogue(c: Any) -> list[dict[str, Any]]:
        view = pcb_design.capture_view(store, ref, conn=c)
        return pcb_design.selection_objects(store, view)

    try:
        if conn is not None:
            objs = _catalogue(conn)
        else:
            with store.pcb_view_tx() as c:
                objs = _catalogue(c)
    except ValueError:
        return {}
    return {o["key"]: o for o in objs if o.get("kind") in argue.OBJECT_PREFIXES}


def _notes_ctx(store: Store, ref: Any, conn: Any = None) -> dict[str, Any]:
    """The argument ledger as the notes partial renders it: questions in
    order, each with its answers, dangling anchors reported per note."""
    ref_id = ref.id
    notes = store.pcb_notes_list(ref_id)
    valid = store.pcb_handles(ref_id)
    if any(argue.is_object_handle(h) for n in notes for h in n["about"]):
        valid["objects"] = _board_objects(store, ref, conn)
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
        # The board catalogue is computed only when an object handle is in play.
        objects: dict[str, dict[str, Any]] | None = None
        if any(argue.is_object_handle(h) for h in clicked + argue.candidates(text)):
            objects = _board_objects(store, ref)
        resolved, unknown = argue.resolve(store, ref.id, clicked, objects)
        if unknown:
            valid = store.pcb_handles(ref.id)
            if objects is not None:
                valid["objects"] = objects
            return {
                "status": 400,
                "error": "unknown handle(s): " + ", ".join(unknown),
                "unknown": unknown,
                "valid": argue.all_handles(valid),
            }
        # The handles the sentence names: clicked first, then any typed by
        # hand that resolve — one list, the user's order, no duplicates.
        about = list(clicked)
        about += [
            h for h in argue.handles_in(text, store, ref.id, objects) if h not in about
        ]
        known = {r.handle for r in resolved}
        extra, _ = argue.resolve(
            store, ref.id, [h for h in about if h not in known], objects
        )
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
        request=request, slug=ref.slug, **_notes_ctx(store, ref)
    )
    return JSONResponse({**result, "html": html})


def _svg_response(svg: str) -> RawResponse:
    return RawResponse(
        content=svg,
        media_type="image/svg+xml",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/pcb/{slug}/board.svg")
async def pcb_board_svg(
    request: Request, slug: str, shown: str | None = None
) -> RawResponse:
    store = get_store(request)
    try:
        ref = resolve_live_slug_ref(store, kind="pcb", id=slug)
    except NotFound:
        return RawResponse(status_code=404, content="not found")
    try:
        with store.pcb_view_tx() as conn:
            view = pcb_design.capture_view(store, ref, conn=conn)
            if shown is not None and shown != view.token:
                return RawResponse(status_code=409, content="shown PCB changed; reload")
            manifest = None if shown is None else pcb_design.selection_manifest(view)
            if manifest is None:
                resp = _handler(store).get(
                    id=ref.slug, view="svg", args={"level": "fab"}
                )
            else:
                resp = _handler(store)._render_fab_svg(
                    ref.id, str(ref.slug), selection_manifest=manifest
                )
    except ValueError as exc:
        return RawResponse(status_code=409, content=str(exc))
    except Exception:
        log.debug("pcb board svg render failed for %s", slug, exc_info=True)
        return RawResponse(
            status_code=422, content="board not renderable yet (place + route it first)"
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


@router.post("/pcb/{slug}/chat")
async def pcb_chat(
    request: Request,
    slug: str,
    message: str = Form(""),
    context: str = Form(""),
    rev: int | None = None,
) -> Any:
    if rev is not None or not message.strip() or len(context) > 16384:
        return design_chat.chat_error(
            request,
            kind="pcb",
            slug=slug,
            detail="current selection and non-empty message required",
            status=400,
        )
    try:
        parsed = json.loads(context)
        result = await asyncio.to_thread(
            design_turn.run_turn,
            design_chat.hub_for(request),
            kind="pcb",
            slug=slug,
            message=message,
            context=parsed,
        )
    except (ValueError, BadInput, NotFound) as exc:
        return design_chat.chat_error(
            request,
            kind="pcb",
            slug=slug,
            detail=str(exc),
            status=409 if not isinstance(exc, NotFound) else 404,
        )
    return design_chat.redirect_after("pcb", slug, result)


@router.post("/pcb/{slug}/chat/apply")
async def pcb_chat_apply(
    request: Request,
    slug: str,
    turn: str = Form(""),
    shown: str = Form(""),
    rev: int | None = None,
) -> Any:
    form = await request.form()
    if rev is not None or "ops" in form:
        return design_chat.chat_error(
            request,
            kind="pcb",
            slug=slug,
            detail="Apply accepts the canonical stored proposal on current view",
            status=400,
        )
    result = await asyncio.to_thread(
        design_turn.apply_proposal,
        design_chat.hub_for(request),
        kind="pcb",
        slug=slug,
        turn=turn,
        shown=shown,
    )
    if not result.applied:
        return design_chat.chat_error(
            request,
            kind="pcb",
            slug=slug,
            detail=result.error or "Apply refused",
            status=409,
        )
    return design_chat.redirect_after_apply("pcb", slug, result)


def _view_call(slug: str, name: str) -> dict[str, Any]:
    """The dispatch args behind ``GET /pcb/{slug}/view/{name}``."""
    if name == "tapeout":
        return {
            "kind": "checklist",
            "id": quest_board.TAPEOUT_CHECKLIST,
            "target": f"pcb:{slug}",
        }
    return {"kind": "pcb", "id": slug, "view": name}


@router.get("/pcb/{slug}/view/{name}", response_class=HTMLResponse)
async def pcb_view(request: Request, slug: str, name: str) -> Any:
    if name not in quest_board.LINKED_VIEWS:
        return RawResponse(status_code=404, content=f"no linked view {name!r}")
    call = _view_call(slug, name)
    body, is_error = await await_dispatch(request, "get", call)
    shown = ", ".join(f"{k}={v!r}" for k, v in call.items())
    return templates.TemplateResponse(
        request,
        "pcb/view.html.j2",
        {
            "active_tab": "pcb",
            "slug": slug,
            "view": name,
            "views": quest_board.LINKED_VIEWS,
            "call": f"get({shown})",
            "body": body,
            "is_error": is_error,
        },
        status_code=400 if is_error else 200,
    )


#: The board panel's actions (gr488206).
_PANEL_OPS = ("route", "place", "review")


def _op_call(
    slug: str,
    op: str,
    *,
    corridor: str | None,
    escape: str | None,
    groups: str | None,
) -> tuple[str, dict[str, Any]]:
    """(verb, dispatch args) for one panel action. ``route``/``place`` go
    through ``put`` in the MCP's ``args=`` shape (``__extras__``, so the
    handler's ``ARGS_KEYS`` gate applies exactly as for an agent);
    ``review`` is a fresh ``view='check'``. Raises ``BadInput`` on a
    malformed corridor."""
    if op == "review":
        # TODO(pcb-datasheet-review slice 7): queue the pcb_review job
        # here once it exists, instead of the check run.
        return "get", {"kind": "pcb", "id": slug, "view": "check"}
    extras: dict[str, Any] = {"op": op}
    if op == "route":
        if corridor is not None and corridor.strip():
            try:
                extras["corridor"] = float(corridor)
            except ValueError as exc:
                raise BadInput(
                    f"corridor must be a number of mm, got {corridor!r}"
                ) from exc
        if escape:
            extras["escape"] = escape
    elif op == "place" and groups:
        extras["groups"] = True
    return "put", {"kind": "pcb", "id": slug, "__extras__": extras}


def _safe_back(back: str | None, slug: str) -> str:
    """Only a local quest page is a redirect target — never an open
    redirect to another host."""
    if back and back.startswith("/refs/quest/") and "//" not in back:
        return back
    return f"/pcb/{slug}"


@router.post("/pcb/{slug}/op")
async def pcb_op(
    request: Request,
    slug: str,
    op: str = Form(...),
    corridor: str | None = Form(None),
    escape: str | None = Form(None),
    groups: str | None = Form(None),
    back: str | None = Form(None),
) -> Any:
    try:
        if op not in _PANEL_OPS:
            raise BadInput(f"op must be one of {', '.join(_PANEL_OPS)}, got {op!r}")
        verb, args = _op_call(slug, op, corridor=corridor, escape=escape, groups=groups)
    except BadInput as exc:
        return templates.TemplateResponse(
            request,
            "error.html.j2",
            {"title": "Board action refused", "detail": str(exc), "status": 400},
            status_code=400,
        )
    return await redirect_or_error(
        request,
        verb,
        args,
        redirect=_safe_back(back, slug),
        error_title=f"{op} refused for {slug}",
    )
