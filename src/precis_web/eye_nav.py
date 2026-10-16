"""The "Eye navigation" link back from a native detail page (gr492464).

``/eye/<handle>`` links out to a ref's native reader ("open in its
reader"); this is the symmetric link back — one anchor to
``/eye/<handle>``, rendered by ONE partial
(``templates/_eye_link.html.j2``) included once at the top of
``<main>`` in ``base.html.j2``, so it lands in the same spot on every
kind's page. The partial renders only when ``eye_link`` (this module's
context processor) puts ``eye_handle`` in the context — which it does
only for endpoints that serve one ref's detail page:

* the dedicated readers (``ref_urls.NATIVE_REF_URLS``' targets, plus
  the ``/pres``, ``/cfp`` and ``/gripes`` readers the registry doesn't
  list — their canonical ``ref_url`` stays the generic page),
* the generic ``/refs/<kind>/<ref_id>`` detail (every stored kind; a
  codeless kind simply yields no handle), and
* the finding claim page (``/claim/<head>`` — ``head`` is already the
  record handle).

Lists, dashboards, previews, sub-pages (``/pcb/<slug>/view/…``,
``/structure/<slug>/run/…``) and the eye/graph navigation views get
nothing — they aren't a ref's native detail page. A lookup miss (bad
slug, stale id) or a stateless app degrades to no link rather than an
error page carrying a dead one.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from fastapi import Request

from precis.utils import handle_registry

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

#: Route endpoint ``<module>.<qualname>`` → the ref kind whose detail
#: page it serves. Each endpoint takes exactly one path parameter
#: naming the ref — a slug or a numeric id; both resolve through
#: ``Store.get_ref``.
_DETAIL_ENDPOINT_KINDS: dict[str, str] = {
    "precis_web.routes.papers.detail": "paper",
    "precis_web.routes.datasheets.detail": "datasheet",
    "precis_web.routes.cfp.detail": "cfp",
    "precis_web.routes.pres.detail": "pres",
    "precis_web.routes.smartdraft.reader": "draft",
    "precis_web.routes.agentlogs.agentlog_detail": "agentlog",
    "precis_web.routes.cad.cad_detail": "cad",
    "precis_web.routes.blocktree_view.se_detail": "se",
    "precis_web.routes.blocktree_view.se_detail_2d": "se",
    "precis_web.routes.structure.structure_detail": "structure",
    "precis_web.routes.figure.figure_detail": "figure",
    "precis_web.routes.mermaid.mermaid_detail": "mermaid",
    "precis_web.routes.pcb.pcb_detail": "pcb",
    "precis_web.routes.gripes.detail": "gripe",
}

#: Endpoints whose path params carry the ref identity differently than
#: "one slug-or-id segment": the generic reader (``kind`` + ``ref_id``)
#: and the claim page (``head`` is itself the record handle).
_REFS_DETAIL = "precis_web.routes.refs.detail"
_CLAIM_VIEW = "precis_web.routes.claim.claim_view"


def _store(request: Request) -> Any:
    """The connected store, or ``None`` on a stateless app / odd fake."""
    try:
        from precis_web.deps import get_store

        return get_store(request)
    except Exception:
        return None


def _ref_handle(
    store: Store | None, kind: str, ident: Any, *, include_deleted: bool = False
) -> str | None:
    """The canonical record handle for the ref ``ident`` addresses, or
    ``None``. ``ident`` is a path parameter: digits read as a numeric
    ref id, anything else as a slug — the same coercion the routes
    apply. ``None`` when the kind has no record code (``try_format``),
    the ref doesn't resolve, or the lookup itself fails — the link is
    decoration, never a reason to fail a page."""
    if store is None:
        return None
    try:
        ref = store.get_ref(
            kind=kind,
            id=int(ident) if str(ident).isdigit() else str(ident),
            include_deleted=include_deleted,
        )
    except Exception:
        log.debug("eye nav: ref lookup failed", exc_info=True)
        return None
    if ref is None:
        return None
    return handle_registry.try_format(kind, int(ref.id))


def eye_link(request: Request) -> dict[str, Any]:
    """Context processor: inject ``eye_handle`` on native detail pages.

    Keys off the matched route's endpoint (``scope["endpoint"]`` +
    ``request.path_params`` — both set by routing, so the check is
    exact: ``/papers/triage`` is a different endpoint from
    ``/papers/{ident}`` and never matches). The store read is one
    indexed ``get_ref``, and it doubles as the liveness gate: a detail
    endpoint rendering its own not-found error page resolves no ref
    and shows no link. ``/refs/<kind>/<id>`` allows deleted rows so a
    tombstone still points at its (Gone) eye page.
    """
    endpoint = request.scope.get("endpoint")
    key = f"{endpoint.__module__}.{endpoint.__qualname__}" if endpoint else ""
    params = request.path_params

    if key == _REFS_DETAIL:
        kind = str(params.get("kind") or "")
        ref_id = params.get("ref_id")
        # profile detail refuses by design (owner-private, gr490266) —
        # its error page must not grow a link.
        if kind == "profile" or ref_id is None:
            return {"eye_handle": None}
        return {
            "eye_handle": _ref_handle(
                _store(request), kind, ref_id, include_deleted=True
            )
        }

    if key == _CLAIM_VIEW:
        parsed = handle_registry.parse(str(params.get("head") or ""))
        if parsed is None or parsed[1]:
            return {"eye_handle": None}
        kind, _is_chunk, pk = parsed
        store = _store(request)
        ref = None
        if store is not None:
            try:
                ref = store.get_ref(kind=kind, id=pk, include_deleted=True)
            except Exception:
                log.debug("eye nav: claim ref lookup failed", exc_info=True)
        return {
            "eye_handle": (
                handle_registry.try_format(kind, pk) if ref is not None else None
            )
        }

    table_kind = _DETAIL_ENDPOINT_KINDS.get(key)
    # Each table endpoint has exactly one path parameter naming the ref.
    if table_kind is None or len(params) != 1:
        return {"eye_handle": None}
    return {
        "eye_handle": _ref_handle(
            _store(request), table_kind, next(iter(params.values()))
        )
    }
