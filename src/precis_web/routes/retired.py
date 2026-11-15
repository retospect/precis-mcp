"""Retired list surfaces → Drive presets, in one redirect table (gr664412).

Every row used to be a per-module redirect-only handler
(``structure_list``, ``cad_list``, ``pcb_list``, ``figure_list``,
``mermaid_list``, the ``/drafts`` / ``/papers`` / ``/items`` index pages,
``/papers/triage``, ``/papers-needed``, and the legacy
``/items/tags/suggest`` alias): WS1b's fold-into-Drive retired each
per-kind list to a facet preset, and they are all the same shape —
GET → redirect to a ``/drive?…`` URL. One table replaces the handlers;
a retired list is a row, not a function. The workbenches and readers
under each prefix are unaffected — they live in their own route
modules.

Targets stay byte-identical to the redirects they replaced (the tests
pin the Location header):

* design kinds — ``/drive?k=<kind>&folder=*&sort=recency``, the same
  target ``_drive_back.html.j2``'s back-link uses.
* draft / paper — ``/drive?k=<kind>&submitted=1``, carrying a live
  ``?q=`` through so a bare bookmark keeps searching (the retired
  keyword/semantic toggle and ``has_pdf``/``has_chunks`` filters have no
  Drive equivalent and stay dropped).
* ``/papers-needed`` — ``/drive?state=stub`` (the ``?awaiting=1``
  next-pass narrowing has no Drive equivalent; the fetcher works the
  whole backlog automatically).
* ``/papers/triage`` — :data:`precis_web.routes.papers._TRIAGE_PRESET`,
  the ``needs-triage`` tag preset Needs-you's links land on.
* ``/items`` — the query passes through verbatim (old saved searches /
  bookmarks), as does the legacy ``/items/tags/suggest`` alias to
  ``/drive/tags/suggest``.

The retired ``/refs`` consolidated browser and the ``/refs/{kind}``
lists redirect from ``routes/refs.py`` instead — they share a router
prefix with the ``/refs/quest`` serves-DAG tree and the detail readers
they must not shadow.

``/papers/triage`` must shadow ``/papers/{ident}``: mount this router
before the route modules (see ``app.py``).
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from precis_web.routes.papers import _TRIAGE_PRESET

router = APIRouter()


def _kind_facet(kind: str) -> str:
    """The ``folder=* + recency`` preset every retired design-kind index used."""
    return f"/drive?k={kind}&folder=*&sort=recency"


def _submitted_facet(kind: str) -> Callable[[Request], str]:
    """``/drive?k=<kind>&submitted=1``, carrying a live ``?q=`` through."""

    def target(request: Request) -> str:
        params: list[tuple[str, str]] = [("k", kind), ("submitted", "1")]
        q = (request.query_params.get("q") or "").strip()
        if q:
            params.append(("q", q))
        return "/drive?" + urlencode(params)

    return target


def _passthrough(path: str) -> Callable[[Request], str]:
    """Redirect to ``path`` with the request's query string verbatim."""

    def target(request: Request) -> str:
        return f"{path}?{request.url.query}" if request.url.query else path

    return target


#: Retired path → static Drive target, or a ``request → url`` builder
#: when live query params carry through.
_TABLE: tuple[tuple[str, str | Callable[[Request], str]], ...] = (
    ("/structure", _kind_facet("structure")),
    ("/cad", _kind_facet("cad")),
    ("/pcb", _kind_facet("pcb")),
    ("/figure", _kind_facet("figure")),
    ("/mermaid", _kind_facet("mermaid")),
    ("/drafts", _submitted_facet("draft")),
    ("/drafts/", _submitted_facet("draft")),
    ("/papers", _submitted_facet("paper")),
    ("/papers/", _submitted_facet("paper")),
    ("/papers/triage", _TRIAGE_PRESET),
    ("/papers-needed", "/drive?state=stub"),
    ("/papers-needed/", "/drive?state=stub"),
    ("/items", _passthrough("/drive")),
    ("/items/", _passthrough("/drive")),
    ("/items/tags/suggest", _passthrough("/drive/tags/suggest")),
)


def _endpoint(
    target: str | Callable[[Request], str],
) -> Callable[[Request], Coroutine[Any, Any, RedirectResponse]]:
    async def endpoint(request: Request) -> RedirectResponse:
        url = target(request) if callable(target) else target
        return RedirectResponse(url=url)

    return endpoint


for _path, _target in _TABLE:
    router.add_api_route(_path, _endpoint(_target), methods=["GET"])
