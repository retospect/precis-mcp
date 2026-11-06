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

``?done=1`` (page-wide) or ``?done=<id>,<id>`` (those quests) toggles the
completed rows — a quest's done/won't-do todos and terminal jobs of the
last seven days (td573298). The template turns the state back into
toggle links, so it round-trips through the URL.
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


def _done_param(request: Request) -> bool | frozenset[int]:
    """The ``?done=`` toggle (td573298): ``1`` is page-wide, anything
    else a comma list of quest ref ids; absent/empty is off."""
    raw = (request.query_params.get("done") or "").strip()
    if not raw:
        return False
    if raw == "1":
        return True
    return frozenset(int(t) for t in raw.split(",") if t.strip().isdigit())


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    """Render the hierarchy; the model is computed off the event loop."""
    store = get_store(request)
    done = _done_param(request)
    model = await asyncio.to_thread(work_model, store, done=done)
    can_write = current_user(request) is not None
    return templates.TemplateResponse(
        request,
        "work/index.html.j2",
        {
            "active_tab": "status",
            "model": model,
            "can_write": can_write,
            "done_all": done is True,
            "done_ids": set(done) if isinstance(done, frozenset) else set(),
        },
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
