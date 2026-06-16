"""Shared dependencies: runtime / store accessors, Jinja env, dispatch.

The app holds a single :class:`precis.runtime.PrecisRuntime` on
``app.state.runtime`` (built once at startup, see ``app.py`` lifespan).
Route handlers reach it through these accessors so tests can inject a
fake runtime onto ``app.state`` without monkeypatching globals.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import jinja2
from fastapi import Request
from fastapi.templating import Jinja2Templates

from precis_web.config import WebConfig

_TEMPLATES_DIR = Path(__file__).parent / "templates"


#: Process-wide Jinja environment.
#:
#: ``ChainableUndefined`` is the defensive choice: a missing context
#: key renders as empty string and tolerates chained access
#: (``missing.foo.bar`` → empty, not 500). The trigger was the live
#: incident on melchior — a stale process omitted ``usage`` from the
#: status context and Jinja's default ``Undefined`` raised
#: ``UndefinedError`` on ``usage.get(...)``, blanking the whole page.
#: Routes still pass full context dicts; this only catches the
#: stale-deploy / context-drift case so the page degrades to empty
#: panels instead of a 500.
def _make_jinja_env() -> jinja2.Environment:
    """Compose the Jinja environment with shared filters.

    Kept as a small factory so test fixtures can mint a fresh env
    without re-registering filters by hand.
    """
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=jinja2.select_autoescape(),
        undefined=jinja2.ChainableUndefined,
    )
    # Inline ``kind:ref`` → hover-preview anchor + click-through.
    # Applied via ``{{ value | linkify_refs }}`` on prose surfaces
    # (dashboard rows, ref detail pages, asks list, console output).
    from precis_web.linkify import linkify_refs

    env.filters["linkify_refs"] = linkify_refs
    return env


templates = Jinja2Templates(env=_make_jinja_env())


def get_runtime(request: Request) -> Any:
    """Return the live ``PrecisRuntime`` from app state.

    Raises a clear RuntimeError when the app booted without a runtime
    (e.g. no ``PRECIS_DATABASE_URL``); the error surfaces as a 500 the
    error middleware renders.
    """
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:
        raise RuntimeError(
            "no runtime configured — set PRECIS_DATABASE_URL before starting precis web"
        )
    return runtime


def get_store(request: Request) -> Any:
    """Return the connected ``Store`` (or raise if stateless)."""
    store = getattr(get_runtime(request), "store", None)
    if store is None:
        raise RuntimeError("runtime has no store (no PRECIS_DATABASE_URL?)")
    return store


def get_web_config(request: Request) -> WebConfig:
    """Return the :class:`WebConfig` stored on app state."""
    cfg = getattr(request.app.state, "web_config", None)
    if cfg is None:
        cfg = WebConfig.from_env()
    return cfg


def dispatch(request: Request, verb: str, args: dict[str, Any]) -> tuple[str, bool]:
    """Run one seven-verb call through the in-process runtime (sync).

    Returns ``(rendered_body, is_error)`` — the same shape the MCP
    server uses. Web writes go through here so the handler validation,
    tree guards, and level gradient stay single-sourced (no surface
    drift between the web and MCP).

    **Use ``await_dispatch`` from FastAPI route handlers.** Calling this
    sync helper directly from an ``async def`` route blocks the entire
    uvicorn event loop for the duration of the verb — a 60s Perplexity
    call freezes every other request on the process, /healthz included.
    """
    runtime = get_runtime(request)
    return runtime.dispatch_with_status(verb, args)


async def await_dispatch(
    request: Request, verb: str, args: dict[str, Any]
) -> tuple[str, bool]:
    """Async wrapper: run :func:`dispatch` in a worker thread.

    Same return shape as the sync version. Use from every route
    handler that might dispatch a verb whose handler does a blocking
    network call (Perplexity, EPO OPS, Crossref, claude -p). The
    event loop stays responsive while one slow verb bakes; /healthz
    and concurrent tabs survive.

    The dispatch itself is single-threaded inside the runtime (the
    psycopg pool serialises DB writes), so wrapping in a worker
    thread doesn't change correctness — it just stops one slow call
    from monopolising the asyncio loop.
    """
    runtime = get_runtime(request)
    return await asyncio.to_thread(runtime.dispatch_with_status, verb, args)
