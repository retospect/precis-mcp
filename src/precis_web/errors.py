"""Exception → HTML mapper.

Keeps the four tabs from leaking a raw 500 stacktrace to the browser.
``NotFound`` renders as a 404 (a link to an absent ref is a missing
page, not a bad request — browsers, crawlers and the user's own reading
of the status all agree on that); every other ``PrecisError`` (typed
handler failures, ``BadInput`` first among them) renders as a clean
400 inline panel with its recovery hint; anything else renders a
generic 500 with the exception type only (the full traceback goes to
the server log, mirroring the runtime's F10 posture). Starlette picks
the most specific handler by MRO, so the ``NotFound`` one wins over the
``PrecisError`` one regardless of registration order; it is registered
first anyway so the file reads in precedence order.
"""

from __future__ import annotations

import logging

from fastapi import Request
from fastapi.responses import HTMLResponse

from precis.errors import NotFound, PrecisError
from precis_web.deps import templates

log = logging.getLogger(__name__)


def register_error_handlers(app) -> None:
    """Attach the NotFound, PrecisError and catch-all handlers to ``app``."""

    @app.exception_handler(NotFound)
    async def _not_found(request: Request, exc: NotFound) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "error.html.j2",
            {"title": "Not found", "detail": str(exc), "status": 404},
            status_code=404,
        )

    @app.exception_handler(PrecisError)
    async def _precis_error(request: Request, exc: PrecisError) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "error.html.j2",
            {"title": "Request error", "detail": str(exc), "status": 400},
            status_code=400,
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> HTMLResponse:
        log.exception("precis web: unhandled error on %s", request.url.path)
        return templates.TemplateResponse(
            request,
            "error.html.j2",
            {
                "title": "Internal error",
                "detail": f"{type(exc).__name__} (see server log)",
                "status": 500,
            },
            status_code=500,
        )
