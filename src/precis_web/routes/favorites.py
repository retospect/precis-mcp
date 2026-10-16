"""Favorites — the per-user heart toggle and the ``/favorites`` list (gr486409).

The heart is a small form on the graph focus page and the ref/paper
readers (``templates/_heart.html.j2``) posting here; the list at
``GET /favorites`` is the "come back to it" half — kind, title, last
change, links to the reader and the ``/graph`` focus view, newest
hearted first, with the same toggle for un-hearting in place.

* ``GET  /favorites``                    — the caller's list.
* ``GET  /fav``                          — the drive-search slash command:
  typing ``/fav`` (or ``/favorites``) in the Drive box resolves through
  the served-path jump (``drive._site_url_target``) to this GET, which
  redirects to the list.
* ``POST /favorites/{kind}/{ref_id}``    — the toggle. ``op=add`` /
  ``op=remove`` (the button posts the opposite of the rendered state);
  anything else flips. htmx gets the re-rendered button back (outerHTML
  swap — no reload); a no-JS submit 303s back to ``return_to``.

Everything is scoped to the signed-in ``web_users`` row
(:func:`current_user`): there is no notion of "the" favorites — only
"this login's". When auth is off there is no login to key on, so the
routes refuse with 503 rather than guess, and the readers don't render
a heart at all (they only render one when the route could compute a
state — see ``faved()`` below).

Storage is :class:`precis.store._favorites_ops.FavoritesMixin` over
``ref_favorites`` (migration 0211) — a plain table, not a verb: like
``/account``'s ``web_users`` writes, a per-user pref is a web-surface
detail, not a corpus mutation the dispatch ledger needs to narrate.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.exceptions import HTTPException
from starlette.responses import Response

from precis import profiles
from precis.store._favorites_ops import FavoriteRow
from precis.users import WebUser
from precis_web.auth import current_user
from precis_web.deps import get_store, templates
from precis_web.ref_urls import ref_url
from precis_web.routes.flags import _safe_local_redirect

router = APIRouter(tags=["favorites"])


def _require_user(request: Request) -> WebUser:
    """The signed-in user, or a 503 explaining why there isn't one.

    Same contract as ``account._require_self``: ``current_user`` is
    ``None`` only when the gate never ran (``PRECIS_WEB_AUTH`` off), and
    a favorite has no meaning without a login to key it to.
    """
    user = current_user(request)
    if user is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "HTTP auth is disabled (PRECIS_WEB_AUTH), so this server "
                "has no signed-in user to favorite for."
            ),
        )
    return user


def faved(request: Request, ref_id: int) -> bool | None:
    """The heart's render state for one ref on a reader/focus page.

    ``None`` — not ``False`` — when nobody is signed in: the templates
    only draw the heart when the route could compute a real state, so
    an auth-off deployment gets no dead button instead of one that
    always errors.
    """
    user = current_user(request)
    if user is None:
        return None
    return get_store(request).is_favorite(user.login, ref_id)


def _row(r: FavoriteRow) -> dict[str, Any]:
    """One favorites-list row. A retired ref renders as a tombstone:
    its links point at the generic ``/refs/{kind}/{id}`` page (which
    serves the tombstone view), not the native reader or ``/graph``
    (both of which would 404 on a deleted ref)."""
    retired = r.retired_at is not None
    return {
        "kind": r.kind,
        "ref_id": r.ref_id,
        # Same fallback chain as the store's: a None/empty title would
        # otherwise render the literal "None" — degrade to the handle,
        # then the ``kind:id`` address.
        "title": r.title or r.handle or f"{r.kind}:{r.ref_id}",
        "handle": r.handle or "",
        "slug": r.slug or "",
        "updated_at": r.ref_updated_at,
        "favorited_at": r.favorited_at,
        "retired": retired,
        "reader_url": (
            f"/refs/{r.kind}/{r.ref_id}"
            if retired
            else ref_url(r.kind, r.ref_id, r.slug)
        ),
        "graph_url": None if retired else f"/graph/{r.kind}/{r.ref_id}",
    }


@router.get("/fav")
async def fav_shortcut() -> RedirectResponse:
    """``/fav`` → ``/favorites`` — the drive-search slash command."""
    return RedirectResponse(url="/favorites", status_code=302)


@router.get("/favorites", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    """The caller's hearted refs, most recently hearted first."""
    user = _require_user(request)
    store = get_store(request)
    return templates.TemplateResponse(
        request,
        "favorites/index.html.j2",
        {
            "rows": [_row(r) for r in store.list_favorites(user.login)],
            "return_to": "/favorites",
        },
    )


@router.post("/favorites/{kind}/{ref_id}", response_model=None)
async def toggle(
    request: Request,
    kind: str,
    ref_id: int,
    op: str = Form("toggle"),
    return_to: str = Form("/favorites"),
) -> Response:
    """Add or remove one heart, then swap the button back (htmx) or 303.

    ``op=add``/``op=remove`` are the idempotent forms the buttons post
    (``aria-pressed`` state → opposite op); ``toggle`` flips. A ref that
    doesn't exist — or whose stored kind isn't the path's — is a 404,
    never a silent write.
    """
    user = _require_user(request)
    store = get_store(request)
    redirect = _safe_local_redirect(return_to, "/favorites")

    ref = store.fetch_refs_by_ids([ref_id], include_deleted=True).get(ref_id)
    # Another owner's profile is a missing ref (gr490266) — no write, and
    # no 404-vs-303 existence oracle.
    if ref is None or ref.kind != kind or not profiles.visible_to(ref, user.login):
        if request.headers.get("HX-Request") == "true":
            return HTMLResponse(
                '<span class="text-xs text-slate-400">ref not found</span>',
                status_code=404,
            )
        raise HTTPException(status_code=404, detail=f"{kind} id={ref_id} not found")

    if op == "add" or (op != "remove" and not store.is_favorite(user.login, ref_id)):
        store.set_favorite(user.login, ref_id)
    else:
        store.unset_favorite(user.login, ref_id)

    if request.headers.get("HX-Request") != "true":
        return RedirectResponse(url=redirect, status_code=303)
    return templates.TemplateResponse(
        request,
        "_heart.html.j2",
        {
            "fav_kind": kind,
            "fav_id": ref_id,
            "faved": store.is_favorite(user.login, ref_id),
            "return_to": return_to,
        },
    )
