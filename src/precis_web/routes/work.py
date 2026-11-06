"""Work page — ``GET /work``, the quest → root → slices hierarchy.

The browser twin of ``search(kind='todo', view='work')`` (dark-factory
step 1, td540843): both read :func:`precis.todo.work_view.work_model`, so
the page and the MCP text cannot drift. Each row carries ``data-handle``
(``td540836``) and links to ``/eye/<handle>``, so the step-5b list / click
/ chat surface (``precis_web.design_chat`` pattern) can select rows later.
Columns: status, agents on it, stage, age (``updated_at``, falling back to
``created_at``), plus a Pause/Resume control for a signed-in caller
(td573297). Tabbed under ``/status`` as a sibling link.

Pause/resume dispatches through the same ``tag``/``edit`` verbs the MCP
surface uses (:mod:`precis.handlers.todo`/:mod:`precis.handlers.quest`),
so the web button and ``edit(kind='quest', mode='pause'|'resume')`` share
one guard path. ``current_user`` is a module attribute (not inlined) so a
test can monkeypatch it to simulate a signed-in/anonymous caller.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from starlette.responses import Response

from precis.todo.work_view import work_model
from precis_web.auth import current_user
from precis_web.deps import get_store, redirect_or_error, templates

router = APIRouter(prefix="/work", tags=["work"])

log = logging.getLogger(__name__)

#: ``op`` -> the STATUS tag ``POST /work/slice/{ref_id}/{op}`` adds.
_SLICE_OPS: dict[str, str] = {"pause": "STATUS:paused", "resume": "STATUS:open"}


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    """Render the hierarchy; the model is computed off the event loop."""
    store = get_store(request)
    model = await asyncio.to_thread(work_model, store)
    can_write = current_user(request) is not None
    return templates.TemplateResponse(
        request,
        "work/index.html.j2",
        {"active_tab": "status", "model": model, "can_write": can_write},
    )


@router.post("/slice/{ref_id}/{op}", response_model=None)
async def slice_pause_resume(request: Request, ref_id: int, op: str) -> Response:
    """Pause or resume one slice: a plain ``STATUS:`` tag through
    dispatch, so :meth:`precis.handlers.todo.TodoHandler.tag`'s own
    guards (claim takeover, halt, …) still run."""
    if current_user(request) is None:
        return Response("write access requires sign-in", status_code=403)
    tag_value = _SLICE_OPS.get(op)
    if tag_value is None:
        raise HTTPException(status_code=404, detail=f"unknown op {op!r}")
    return await redirect_or_error(
        request,
        "tag",
        {"kind": "todo", "id": ref_id, "add": [tag_value]},
        redirect="/work",
        error_title="Slice pause/resume error",
    )


@router.post("/quest/{quest_id}/{op}", response_model=None)
async def quest_pause_resume(request: Request, quest_id: int, op: str) -> Response:
    """Pause or resume a whole quest: dispatches
    ``edit(kind='quest', mode=op)`` — :class:`precis.handlers.quest.QuestHandler`
    walks its ``serves``-linked strategic roots and stamps/lifts
    ``STATUS:paused`` on them (td573297)."""
    if current_user(request) is None:
        return Response("write access requires sign-in", status_code=403)
    if op not in ("pause", "resume"):
        raise HTTPException(status_code=404, detail=f"unknown op {op!r}")
    return await redirect_or_error(
        request,
        "edit",
        {"kind": "quest", "id": quest_id, "mode": op},
        redirect="/work",
        error_title="Quest pause/resume error",
    )
