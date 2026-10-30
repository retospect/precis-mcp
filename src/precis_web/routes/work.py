"""Work page — ``GET /work``, the quest → root → slices hierarchy.

The browser twin of ``search(kind='todo', view='work')`` (dark-factory
step 1, td540843): both read :func:`precis.todo.work_view.work_model`, so
the page and the MCP text cannot drift. Read-only — no forms, no writes.
Each row carries ``data-handle`` (``td540836``) and links to
``/eye/<handle>``, so the step-5b list / click / chat surface
(``precis_web.design_chat`` pattern) can select rows later. Columns:
status, agents on it, stage, age (``updated_at``, falling back to
``created_at``). Tabbed under ``/status`` as a sibling link.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from precis.todo.work_view import work_model
from precis_web.deps import get_store, templates

router = APIRouter(prefix="/work", tags=["work"])

log = logging.getLogger(__name__)


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    """Render the hierarchy; the model is computed off the event loop."""
    store = get_store(request)
    model = await asyncio.to_thread(work_model, store)
    return templates.TemplateResponse(
        request, "work/index.html.j2", {"active_tab": "status", "model": model}
    )
