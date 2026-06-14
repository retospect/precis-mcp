"""FastAPI application factory.

``create_app`` wires the four tab routers, the error handlers, and a
lifespan that builds the single :class:`precis.runtime.PrecisRuntime`
at startup (and closes its store at shutdown). Tests pass a pre-built
(possibly fake) runtime to skip the DB connect.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from precis_web.config import WebConfig
from precis_web.errors import register_error_handlers

log = logging.getLogger(__name__)


def create_app(
    *,
    runtime: Any | None = None,
    web_config: WebConfig | None = None,
) -> FastAPI:
    """Build the precis-web FastAPI app.

    ``runtime`` — inject a pre-built runtime (tests, or an embedding
    caller that already holds one). When ``None``, the lifespan builds
    one from the environment at startup and closes it at shutdown.
    ``web_config`` — defaults to :meth:`WebConfig.from_env`.
    """
    cfg = web_config or WebConfig.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
        owns_runtime = False
        if getattr(app.state, "runtime", None) is None:
            from precis.runtime import build_runtime

            log.info("precis web: building runtime")
            app.state.runtime = build_runtime()
            owns_runtime = True
        try:
            yield
        finally:
            if owns_runtime:
                store = getattr(app.state.runtime, "store", None)
                if store is not None:
                    store.close()

    app = FastAPI(title="precis web", lifespan=lifespan)
    app.state.web_config = cfg
    if runtime is not None:
        app.state.runtime = runtime

    register_error_handlers(app)

    # Routers — one per tab. Imported here (not at module top) so the
    # package import surface stays light and circular-import-free.
    from precis_web.routes import console, papers, status, tasks

    app.include_router(tasks.router)
    app.include_router(papers.router)
    app.include_router(console.router)
    app.include_router(status.router)

    @app.get("/", include_in_schema=False)
    async def _root() -> RedirectResponse:
        return RedirectResponse(url="/tasks")

    @app.get("/healthz", include_in_schema=False)
    async def _healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
