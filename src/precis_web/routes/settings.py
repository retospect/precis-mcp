"""``/settings`` — the DB-resident settings editor
(:mod:`precis.settings`), sibling of the ``/secrets`` vault editor and the
``/budget`` cap editor.

Unlike ``/secrets`` this page is **not** write-only: settings values aren't
secrets (the redact-rule from ``docs/backlog/db-resident-settings.md`` —
"would you redact it in a log line?" no → settings), so the current
resolved value is shown and pre-fills each row's edit field.

``set`` refuses an unregistered key and validates the submitted value
against the registered type (:func:`precis.cli.settings.coerce_for_write`
— shared with the CLI so the two surfaces enforce identically) before
writing; a rejected write re-renders the page with an error banner rather
than silently no-op'ing or storing junk.
"""

from __future__ import annotations

from urllib.parse import quote_plus

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.responses import Response

from precis import settings as psettings
from precis.cli.settings import coerce_for_write
from precis_web.deps import get_store, templates

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def index(request: Request, error: str = "") -> HTMLResponse:
    """Render the registered inventory + the per-row set/clear editor."""
    store = get_store(request)
    rows = psettings.list_settings(store=store)
    return templates.TemplateResponse(
        request,
        "settings/index.html.j2",
        {"active_tab": "settings", "rows": rows, "error": error},
    )


@router.post("/set")
async def set_setting(
    request: Request,
    key: str = Form(...),
    value: str = Form(""),
) -> Response:
    """Write a DB override. Refuses an unregistered key or a value that
    doesn't parse as the registered type — redirects back with the reason
    rather than a silent no-op."""
    key = key.strip()
    store = get_store(request)
    entry = psettings.REGISTRY.get(key)
    if entry is None:
        msg = quote_plus(f"unregistered key {key!r} — refusing to set")
        return RedirectResponse(f"/settings?error={msg}", status_code=303)
    try:
        coerced = coerce_for_write(entry, value)
    except ValueError as exc:
        return RedirectResponse(
            f"/settings?error={quote_plus(str(exc))}", status_code=303
        )
    psettings.set_setting(key, coerced, store=store)
    return RedirectResponse("/settings", status_code=303)


@router.post("/clear")
async def clear_setting(request: Request, key: str = Form(...)) -> Response:
    """Delete one DB override — reverts to the env / compiled default."""
    key = key.strip()
    if key:
        psettings.clear_setting(key, store=get_store(request))
    return RedirectResponse("/settings", status_code=303)
