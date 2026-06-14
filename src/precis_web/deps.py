"""Shared dependencies: runtime / store accessors, Jinja env, dispatch.

The app holds a single :class:`precis.runtime.PrecisRuntime` on
``app.state.runtime`` (built once at startup, see ``app.py`` lifespan).
Route handlers reach it through these accessors so tests can inject a
fake runtime onto ``app.state`` without monkeypatching globals.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates

from precis_web.config import WebConfig

_TEMPLATES_DIR = Path(__file__).parent / "templates"

#: Process-wide Jinja environment. Autoescape is on (FastAPI default
#: for ``.html``) so handler output rendered into pages is escaped.
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


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
    """Run one seven-verb call through the in-process runtime.

    Returns ``(rendered_body, is_error)`` — the same shape the MCP
    server uses. Web writes go through here so the handler validation,
    tree guards, and level gradient stay single-sourced (no surface
    drift between the web and MCP).
    """
    runtime = get_runtime(request)
    return runtime.dispatch_with_status(verb, args)
