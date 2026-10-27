"""PCB tab — browse the ``pcb`` kind: board render + schematic.

The pcb kind is otherwise a text/MCP surface (the LLM authors a netlist
and reads graphs, never pixels). This route is the human affordance on the
same data:

* ``GET  /pcb`` — retired into the unified Drive surface; redirects to the
  ``kind=pcb`` facet preset.
* ``GET  /pcb/{slug}`` — one design: the fab-level board render beside the
  net-label schematic, with the netlist/route/DRC vitals above.
* ``GET  /pcb/{slug}/board.svg`` — the fab SVG (embedded via ``<object>``
  so its layer-toggle legend script keeps working). ``?flip=1`` is the
  workbench's bottom view — mirrored, the B.* layers on top.
* ``GET  /pcb/{slug}/schematic.svg`` — the net-label schematic
  (:mod:`precis.pcb.schematic` — placement-free, renders from day one).
* ``GET  /pcb/{slug}/export.epro2`` — the EasyEDA Pro file, rebuilt on
  request by the MCP view's own ``PcbHandler.epro_build`` (deterministic
  bytes, nothing stored); ``?sha=`` from the MCP response turns a stale
  link into a 409.
* ``GET  /pcb/{slug}/fab.zip`` — the ``view='fab'`` production package as
  one download (gr496699): the gerber+drill zip plus the JLC BOM and CPL,
  assembled in memory by ``PcbHandler.fab_package_zip`` off the same
  artifacts the MCP view writes to a directory.
* ``POST /pcb/create`` — an EMPTY board through the ``put(kind='pcb')``
  create path (gr496699); refuses a taken slug, redirects to the board.
* ``GET  /pcb/new`` — the board-creation page (gr496699): import an
  uploaded EasyEDA Pro ``.epro2`` through
  :func:`precis.ingest.pcb_epro.import_epro` — the same importer
  ``op='import_epro'`` runs. File bytes only; no URL fetch, no path the
  upload could reach. The import writes under the same
  ``human_write_context`` the dispatch wrapper stamps, and its report
  (counts, stackup, warnings, dropped nets / unused pins) lands on a
  result page, not the board page.
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
  ``place`` (``groups=``) queues the job through ``put(args={'op': …})``,
  the same dispatch an agent calls; ``review`` is ``put(args={'op':'review'})``
  — the ``pcb_review`` job over the not-checked/stale ``pcb-tapeout``
  items, which runs its own fresh check (DRC + ERC, recorded) first. The page
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

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.responses import Response as RawResponse

from precis.design import history
from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers._slug_ref_shared import resolve_live_slug_ref
from precis.handlers.pcb import PcbHandler
from precis.pcb import argue
from precis.utils.text import slugify
from precis_web import (
    design_chat,
    design_turn,
    pcb_design,
    pcb_hover,
    pcb_parts,
    quest_board,
)
from precis_web.deps import (
    await_dispatch,
    get_store,
    human_write_context,
    redirect_or_error,
    templates,
)

if TYPE_CHECKING:
    from precis.store.store import Store

router = APIRouter(tags=["pcb"])

log = logging.getLogger(__name__)

#: gr518348 — a fab render is model assembly + gerber export + reparse,
#: seconds on a real board, and the workbench re-requests it on every
#: flip/focus round-trip. Keyed on the shown snapshot token — a sha over
#: every board table — so a hit is provably the render of that exact
#: board state; the 4th element keeps the bare (no ``?shown=``) render
#: apart from the selection-manifest one.
_FAB_SVG_CACHE: collections.OrderedDict[tuple[int, str, bool, bool], str] = (
    collections.OrderedDict()
)
_FAB_SVG_CACHE_MAX = 8


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


#: ``POST /pcb/import``'s upload cap (gr496699): an ``.epro2`` is a zip of
#: text records; the real boards imported so far are single-digit MB.
_EPRO_UPLOAD_MAX = 32 * 1024 * 1024


def _pcb_error(request: Request, title: str, detail: str, status: int = 400) -> Any:
    return templates.TemplateResponse(
        request,
        "error.html.j2",
        {"title": title, "detail": detail, "status": status},
        status_code=status,
    )


@router.get("/pcb/new", response_class=HTMLResponse)
async def pcb_new(request: Request) -> Any:
    return templates.TemplateResponse(
        request, "pcb/new.html.j2", {"active_tab": "pcb", "title": "New board"}
    )


@router.post("/pcb/create", response_model=None)
async def pcb_create(
    request: Request,
    slug: str = Form(""),
    title: str = Form(""),
) -> Any:
    """Create an EMPTY board through the pcb ``put`` create path, then land
    on its page. Refuses a taken slug — ``put`` on an existing slug would
    edit that design, not make a new one. CSRF and auth are the app
    middleware's, as for ``/pcb/import``."""
    want = slugify(slug)
    if not want:
        return _pcb_error(
            request,
            "Create refused",
            "no slug — name the board in the form (letters, digits, dashes).",
        )
    if want == "new":
        return _pcb_error(
            request,
            "Create refused",
            "'new' is the board-creation page's own address — pick another slug.",
        )
    if get_store(request).get_ref(kind="pcb", id=want) is not None:
        return _pcb_error(
            request,
            "Create refused",
            f"a board '{want}' already exists — pick a fresh slug.",
        )
    call: dict[str, Any] = {"kind": "pcb", "id": want, "args": {}}
    if title.strip():
        call["title"] = title.strip()
    return await redirect_or_error(
        request,
        "put",
        call,
        redirect=f"/pcb/{want}",
        error_title="Create refused",
    )


@router.post("/pcb/import", response_class=HTMLResponse)
async def pcb_import(
    request: Request,
    file: UploadFile = File(...),
    slug: str = Form(""),
    title: str = Form(""),
) -> Any:
    """Import an uploaded ``.epro2`` into a NEW board and show the import
    report — the same ``import_epro`` ``op='import_epro'`` wraps."""
    from precis.ingest.pcb_epro import EproImportError, import_epro
    from precis.pcb import copper_report
    from precis.pcb.epro import EproError

    store = get_store(request)
    data = await file.read(_EPRO_UPLOAD_MAX + 1)
    if not data:
        return _pcb_error(
            request, "Import refused", "no file uploaded — choose an .epro2 file."
        )
    if len(data) > _EPRO_UPLOAD_MAX:
        return _pcb_error(
            request,
            "Import refused",
            f"the upload is larger than {_EPRO_UPLOAD_MAX // 2**20} MiB — "
            "import it with the CLI instead: "
            "precis pcb import-epro <file> --slug <slug>.",
        )
    if not data.startswith(b"PK\x03\x04"):
        return _pcb_error(
            request,
            "Import refused",
            "the upload is not an .epro2 — its first bytes are not the zip "
            "signature (PK). Export the board from EasyEDA Pro as .epro2 "
            "and upload that file.",
        )
    stem = (file.filename or "").replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0]
    want = slugify(slug) if slug.strip() else slugify(stem)
    if not want:
        return _pcb_error(
            request,
            "Import refused",
            "no slug to import under — name one in the form, or rename the "
            "file so a slug can be derived from it.",
        )
    if want == "new":
        return _pcb_error(
            request,
            "Import refused",
            "'new' is the board-creation page's own address — pick another slug.",
        )
    try:

        def _run() -> tuple[Any, int, int]:
            with human_write_context(request):
                result = import_epro(
                    store,
                    data,
                    slug=want,
                    title=title.strip() or want,
                    source_name=file.filename,
                )
                queued, facts = _handler(store)._queue_datasheets(result.ref_id)
                return result, queued, facts

        result, queued, facts = await asyncio.to_thread(_run)
    except EproError as exc:
        return _pcb_error(
            request,
            "Import refused",
            f"{exc} Export the board from EasyEDA Pro as .epro2 and upload that file.",
        )
    except (BadInput, EproImportError, ValueError) as exc:
        return _pcb_error(
            request,
            "Import refused",
            f"{exc} Pick a fresh slug or correct the named field and retry.",
        )
    return templates.TemplateResponse(
        request,
        "pcb/import_result.html.j2",
        {
            "active_tab": "pcb",
            "title": f"Imported {result.slug}",
            "slug": result.slug,
            "filename": file.filename,
            "stats": result.stats,
            "counts": result.counts,
            "stackup": [str(layer.get("name")) for layer in result.stackup],
            "planes": result.planes,
            "dropped": result.dropped_lines(),
            "warnings": result.warnings,
            "copper": (copper_report.render(result.copper) if result.copper else []),
            "alignment": result.alignment_candidates,
            "queued": queued,
            "facts": facts,
        },
    )


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
            # Sheets (workbench spec slice 2): one tab per authored
            # placement group — the same ``group`` key on instance meta
            # that ``pcb_graph`` hoists and schematic.sheet_names reads.
            # ``pcb_sheet_map`` (refdes → group) lets the cross-probe
            # switch the schematic pane to the sheet holding a clicked
            # object.
            _sheets_of = {
                r["refdes"]: str((r.get("meta") or {}).get("group"))
                for r in view.raw["tables"]["pcb_instances"]
                if not r.get("retired_at") and (r.get("meta") or {}).get("group")
            }
            # Tab tooltips (gr520805): a sheet tab says which parts its
            # group() holds; the overview tab names the ungrouped parts
            # that live only there plus the cross-sheet nets it shows.
            _members_of: dict[str, list[str]] = {}
            for _rd, _grp in _sheets_of.items():
                _members_of.setdefault(_grp, []).append(_rd)
            _ungrouped = sorted(
                r["refdes"]
                for r in view.raw["tables"]["pcb_instances"]
                if not r.get("retired_at") and r["refdes"] not in _sheets_of
            )
            _sheet_titles = {
                s: f"placement group '{s}' — {', '.join(sorted(refs))}"
                for s, refs in _members_of.items()
            }
            _sheet_titles[""] = "the whole design — " + (
                f"ungrouped parts {', '.join(_ungrouped)} plus cross-sheet nets"
                if _ungrouped
                else "every group and the nets that cross sheets"
            )
            ctx = {
                "active_tab": "pcb",
                "slug": ref.slug,
                "title": ref.title or ref.slug,
                **_vitals(store, ref.id),
                **_notes_ctx(store, ref, conn),
                "pcb_sheets": sorted(set(_sheets_of.values())),
                "pcb_sheet_map": _sheets_of,
                "pcb_sheet_titles": _sheet_titles,
                "pcb_context": view.context,
                "pcb_part_cards": pcb_parts.cards(view),
                "pcb_instances": view.instances,
                "pcb_poses": pcb_design.instance_poses(view),
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
    request: Request, slug: str, shown: str | None = None, flip: bool = False
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
            cache_key = (
                int(ref.id),
                shown if shown is not None else view.token,
                bool(flip),
                shown is not None,
            )
            cached_body = _FAB_SVG_CACHE.get(cache_key)
            if cached_body is not None:
                _FAB_SVG_CACHE.move_to_end(cache_key)
                return _svg_response(cached_body)
            manifest = None
            if shown is not None:
                manifest = {
                    **pcb_design.selection_manifest(view),
                    "hover": pcb_hover.hover_facts(store, view),
                }
            if manifest is None:
                resp = _handler(store).get(
                    id=ref.slug, view="svg", args={"level": "fab", "flip": flip}
                )
            else:
                resp = _handler(store)._render_fab_svg(
                    ref.id,
                    str(ref.slug),
                    selection_manifest=manifest,
                    flip=flip,
                )
            _FAB_SVG_CACHE[cache_key] = resp.body
            _FAB_SVG_CACHE.move_to_end(cache_key)
            while len(_FAB_SVG_CACHE) > _FAB_SVG_CACHE_MAX:
                _FAB_SVG_CACHE.popitem(last=False)
    except ValueError as exc:
        return RawResponse(status_code=409, content=str(exc))
    except Exception:
        log.debug("pcb board svg render failed for %s", slug, exc_info=True)
        return RawResponse(
            status_code=422, content="board not renderable yet (place + route it first)"
        )
    return _svg_response(resp.body)


@router.get("/pcb/{slug}/export.epro2")
async def pcb_export_epro(
    request: Request,
    slug: str,
    sha: str | None = None,
    allow_synthesized: bool = False,
) -> RawResponse:
    """The board as an EasyEDA Pro ``.epro2`` download, rebuilt on request
    by the same :meth:`PcbHandler.epro_build` ``view='epro'`` runs — no file
    is stored or read from disk (gr495289, Reto's ruling: option 1a).
    ``sha`` is the prefix the MCP response printed; a board edited since
    then rebuilds to different bytes and answers 409 rather than handing
    over a file the agent never saw."""
    store = get_store(request)
    try:
        ref = resolve_live_slug_ref(store, kind="pcb", id=slug)
    except NotFound:
        return RawResponse(
            status_code=404, content="not found", media_type="text/plain"
        )
    try:
        build = await asyncio.to_thread(
            _handler(store).epro_build, ref.id, allow_synthesized=allow_synthesized
        )
    except BadInput as exc:
        return RawResponse(status_code=422, content=str(exc), media_type="text/plain")
    if build is None:
        return RawResponse(
            status_code=422, content="no board yet", media_type="text/plain"
        )
    want = (sha or "").strip().lower()
    if want and not build.sha256.startswith(want):
        # The asked-for value is not echoed: it is caller text.
        return RawResponse(
            status_code=409,
            content=(
                "the board changed since that export; run view='epro' again "
                "for the current link"
            ),
            media_type="text/plain",
        )
    return RawResponse(
        content=build.blob,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{build.filename}"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/pcb/{slug}/fab.zip")
async def pcb_fab_zip(request: Request, slug: str) -> RawResponse:
    """The ``view='fab'`` production package as one download (gr496699):
    the gerber+drill zip plus the JLC BOM and CPL, rebuilt in memory by
    :meth:`PcbHandler.fab_package_zip` — the same three artifacts the
    MCP view writes to disk, nothing stored."""
    store = get_store(request)
    try:
        ref = resolve_live_slug_ref(store, kind="pcb", id=slug)
    except NotFound:
        return RawResponse(
            status_code=404, content="not found", media_type="text/plain"
        )
    try:
        built = await asyncio.to_thread(_handler(store).fab_package_zip, ref.id)
    except BadInput as exc:
        return RawResponse(status_code=422, content=str(exc), media_type="text/plain")
    if built is None:
        return RawResponse(
            status_code=422, content="no board yet", media_type="text/plain"
        )
    blob, filename = built
    return RawResponse(
        content=blob,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/pcb/{slug}/part/{refdes}/thumb")
async def pcb_part_thumb(request: Request, slug: str, refdes: str) -> RawResponse:
    """The part card's cached thumbnail (pcb_parts.thumbnail — safe_get,
    cached on the part ref's meta; never a hotlink)."""
    store = get_store(request)
    try:
        ref = resolve_live_slug_ref(store, kind="pcb", id=slug)
    except NotFound:
        return RawResponse(status_code=404, content="not found")
    lcsc = None
    try:
        with store.pcb_view_tx() as conn:
            view = pcb_design.capture_view(store, ref, conn=conn)
            lcsc = (pcb_parts.cards(view).get(refdes) or {}).get("lcsc")
    except ValueError:
        return RawResponse(status_code=409, content="design changed")
    if not lcsc:
        return RawResponse(status_code=404, content="no part image")
    got = await asyncio.to_thread(pcb_parts.thumbnail, store, str(lcsc))
    if got is None:
        return RawResponse(status_code=404, content="no part image")
    return RawResponse(
        content=got[0],
        media_type=got[1],
        headers={"Cache-Control": "private, max-age=86400"},
    )


@router.get("/pcb/{slug}/schematic.svg")
async def pcb_schematic_svg(
    request: Request,
    slug: str,
    focus: str | None = None,
    sheet: str | None = None,
    only: str | None = None,
) -> RawResponse:
    store = get_store(request)
    try:
        ref = resolve_live_slug_ref(store, kind="pcb", id=slug)
    except NotFound:
        return RawResponse(status_code=404, content="not found")
    # ?focus=<handle> enters focus mode: the target part/net and its one-hop
    # neighbours stay bright, the rest dims (static/pcb-focus.js reloads the
    # <object> with this query; the render stays a pure fn of the graph).
    # ?sheet=<group> renders one placement-group sheet (slice 2): absent is
    # the overview, unknown falls back to it too.
    # ?only=1 (gr520807) drops the dimmed parts and non-focus pins outright
    # — the compact relayout dimming cannot give.
    args = {
        k: v
        for k, v in (("focus", focus), ("sheet", sheet), ("only", only))
        if v and v.strip()
    } or None
    resp = _handler(store).get(id=ref.slug, view="schematic", args=args)
    return _svg_response(resp.body)


@router.post("/pcb/{slug}/pin")
async def pcb_pin(
    request: Request,
    slug: str,
    refdes: str = Form(...),
    pinned: str = Form(""),
) -> JSONResponse:
    """Toggle a part's placement lock — the focus-mode "pin" ("I want this
    part to stay here", docs/backlog/pcb-workbench-layout.md slice 1). Sets
    ``pcb_instances.fixed`` to ``'xy'`` (pin) or clears it (unpin) via the
    authorized inline edit path; no geometry moves. The pin survives a
    reload because it is a persisted column, re-read by ``pcb_graph``."""
    store = get_store(request)
    try:
        ref = resolve_live_slug_ref(store, kind="pcb", id=slug)
    except NotFound:
        return JSONResponse({"error": f"no live pcb design {slug!r}"}, status_code=404)
    rd = refdes.strip()
    if not rd:
        return JSONResponse({"error": "refdes required"}, status_code=400)
    want = pinned.strip().lower() in ("1", "true", "on", "yes")
    ok = await asyncio.to_thread(
        store.pcb_move_instance, ref.id, rd, fixed=("xy" if want else None)
    )
    if not ok:
        return JSONResponse(
            {"error": f"no instance {rd!r} on {slug!r}"}, status_code=404
        )
    return JSONResponse({"ok": True, "refdes": rd, "pinned": want})


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
_PANEL_OPS = ("place", "review")


def _op_call(
    slug: str,
    op: str,
    *,
    groups: str | None,
) -> tuple[str, dict[str, Any]]:
    """(verb, dispatch args) for one panel action. ``place`` goes through
    ``put`` in the MCP's ``args=`` shape (``__extras__``, so the
    handler's ``ARGS_KEYS`` gate applies exactly as for an agent);
    ``review`` is ``put(args={'op':'review'})`` — the ``pcb_review`` job
    (which runs its own fresh check first)."""
    extras: dict[str, Any] = {"op": op}
    if op == "place" and groups:
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
    groups: str | None = Form(None),
    back: str | None = Form(None),
) -> Any:
    try:
        if op not in _PANEL_OPS:
            raise BadInput(f"op must be one of {', '.join(_PANEL_OPS)}, got {op!r}")
        verb, args = _op_call(slug, op, groups=groups)
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
