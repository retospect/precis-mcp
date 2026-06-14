"""Exception → HTML mapper.

Keeps the four tabs from leaking a raw 500 stacktrace to the browser.
``PrecisError`` (typed handler failures) renders as a clean inline
panel with its recovery hint; anything else renders a generic 500
with the exception type only (the full traceback goes to the server
log, mirroring the runtime's F10 posture).
"""

from __future__ import annotations

import logging

from fastapi import Request
from fastapi.responses import HTMLResponse

from precis.errors import PrecisError
from precis_web.deps import templates

log = logging.getLogger(__name__)


def register_error_handlers(app) -> None:  # type: ignore[no-untyped-def]
    """Attach the PrecisError + catch-all handlers to ``app``."""

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
