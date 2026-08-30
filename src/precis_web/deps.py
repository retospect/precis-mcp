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
from fastapi.responses import RedirectResponse, Response
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
        # Our templates are named ``*.html.j2`` — they end in ``.j2``,
        # which the stock ``select_autoescape()`` does NOT recognise
        # (it only enables on ``.html``/``.htm``/``.xml``). That left
        # autoescape OFF for the entire web UI: every ``{{ value }}``
        # rendered raw HTML, so a planner-prompt title containing a
        # literal ``<title or DOI>`` placeholder opened a real
        # ``<title>`` element, flipped the tokenizer to RAWTEXT, and
        # swallowed the rest of the page — silently killing the inline
        # ``<script>`` blocks (the Todo filter / collapse buttons went
        # dead with no JS error). It was also a broad stored-XSS hole.
        # Recognise ``.j2`` so escaping is ON; the only intentional HTML
        # comes from Markup-returning filters (``linkify_refs``,
        # ``tojson``), which bypass autoescape correctly.
        autoescape=jinja2.select_autoescape(
            enabled_extensions=("html", "htm", "xml", "j2"),
        ),
        undefined=jinja2.ChainableUndefined,
    )
    # Inline ``kind:ref`` → hover-preview anchor + click-through.
    # Applied via ``{{ value | linkify_refs }}`` on prose surfaces
    # (dashboard rows, ref detail pages, asks list, console output).
    from precis_web.linkify import (
        linkify_refs,
        linkify_toon,
        render_cloze,
        render_markdown,
    )
    from precis_web.timefmt import abs_ts, ago, duration, relative

    env.filters["linkify_refs"] = linkify_refs
    # Anki cloze bodies ({{c1::answer::hint}}) render as highlighted
    # deletions instead of raw markup on the ref detail page.
    env.filters["render_cloze"] = render_cloze
    # Like linkify_refs, but renders tab-separated runs (TOON ``Next:``
    # blocks, tab-delimited oracle shares) as aligned HTML tables so the
    # tabularity is visible instead of collapsing on <pre> tab-stops.
    env.filters["linkify_toon"] = linkify_toon
    # Bold/code/sub/sup markdown subset WITHOUT ref-linking — for popover
    # quotes (no nested ref anchors). Math ($…$) is left for client KaTeX.
    env.filters["render_markdown"] = render_markdown
    # Relative ('5h ago') + absolute (hover tooltip) timestamp rendering,
    # single-sourced in ``precis_web.timefmt`` so every list view formats
    # time the same way. Both tolerate a datetime *or* an ISO string.
    env.filters["ago"] = ago
    env.filters["abs_ts"] = abs_ts
    # Signed ('in 12m' / '3m ago') — for a timestamp that can sit either
    # side of now, like a job lease expiry, where ``ago``'s clamp to
    # "0s ago" would read as "expiring now" for a lease with an hour left.
    env.filters["relative"] = relative
    # Elapsed span to two units ('4m18s') — a run time compared against a
    # timeout, not a "how long ago".
    env.filters["duration"] = duration

    # ``planner_models()`` → the ordered list of picker rows
    # (``{alias, tier, model, placement, fallbacks, size, context}``) the
    # router understands for ``meta.llm_tier``. Registered as a Jinja global
    # so every model-picker dropdown (todo retry, draft change-request,
    # review, smartdraft ask) renders the SAME options — the capability tiers
    # available on this cluster (opus/sonnet/haiku + the local qwen), labelled
    # with the model each currently *routes to* (the live placement chain,
    # not a hardcoded vendor name) — from one source instead of a
    # per-template hardcoded list.
    # Short-TTL memo: the dashboard calls this inside per-row loops (retry
    # pickers on every failed job), and the underlying chain/catalog reads —
    # while themselves 15s-TTL-cached — still add up to 8 aliases × N rows of
    # resolver passes per render. 5s staleness is well under the 15s the
    # source reads already tolerate.
    _memo: dict[str, Any] = {"until": 0.0, "rows": None}

    def _planner_models() -> list[dict[str, Any]]:
        import time as _time

        from precis.utils.llm.router import planner_model_choices

        now = _time.monotonic()
        if _memo["rows"] is not None and _memo["until"] > now:
            return _memo["rows"]
        try:
            rows = planner_model_choices()
            _memo.update(until=now + 5.0, rows=rows)
            return rows
        except Exception:  # pragma: no cover - a resolver hiccup must not 500 a page
            from precis.utils.llm.router import PLANNER_MODEL_ALIASES

            return [
                {
                    "alias": a,
                    "tier": None,
                    "model": a,
                    "placement": None,
                    "fallbacks": [],
                    "size": None,
                    "context": None,
                }
                for a in PLANNER_MODEL_ALIASES
            ]

    env.globals["planner_models"] = _planner_models
    return env


#: ``nav_badges`` is a Starlette context processor: it runs on every
#: ``TemplateResponse(request, ...)`` and injects the top-bar attention
#: counts (``nav_needs_you`` / ``nav_alerts``) so the badges stay live on
#: whatever page is rendered, without each route threading them in. It is
#: fully defensive (degrades to zero on any error), so it never 500s a page.
from precis_web.nav import nav_badges

templates = Jinja2Templates(env=_make_jinja_env(), context_processors=[nav_badges])


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


async def redirect_or_error(
    request: Request,
    verb: str,
    args: dict[str, Any],
    *,
    redirect: str,
    error_title: str = "Request error",
) -> Response:
    """Dispatch one verb; redirect on success, render the error on failure.

    The canonical mutation-route wrapper. Write routes used to discard
    the handler result and redirect unconditionally, so a rejected
    mutation (an invalid tag, a guard veto, an id the handler can't
    resolve) failed silently — the operator hit submit and the page
    reloaded unchanged with no explanation. The ``/papers/{id}/untriage``
    "Clear flag" button was exactly this: it swallowed a ``NotFound`` and
    redirected, so the button looked like it worked while the tag stayed.

    Surfacing the handler's own message (its ``next=`` recovery hint
    included) makes these self-diagnosing. Every mutation route should go
    through here rather than ignoring ``await_dispatch``'s ``is_error``.

    Async because the inner dispatch may issue a blocking network call
    (Perplexity, EPO OPS, claude -p) that would otherwise freeze the
    uvicorn event loop.
    """
    body, is_error = await await_dispatch(request, verb, args)
    if is_error:
        return templates.TemplateResponse(
            request,
            "error.html.j2",
            {"title": error_title, "detail": body, "status": 400},
            status_code=400,
        )
    return RedirectResponse(url=redirect, status_code=303)
