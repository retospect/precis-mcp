"""Reading-intent flags — kind-agnostic one-click tag toggles.

The first slice of the unified-item-view proposal
(``docs/proposals/unified-item-view.md``). A *flag* is a plain
``OPEN:`` tag carrying reading intent — ``read-later`` / ``must-read``
/ ``skim`` — that a person (or the LLM) can stick on any ref with one
click. Because a paper stub and its eventually-ingested paper are the
**same** ``ref_id``, a flag set on a stub in ``/papers-needed`` rides
through fetch + ingest into the finished paper: flag now, read when it
lands.

Kind-agnostic on purpose: the same ``_flag_buttons`` partial and this
one route serve every item list the unified view will grow, not just
papers. Writes flow through the shared ``tag`` verb (via
:func:`redirect_or_error`) so vocabulary validation and tree guards
stay single-sourced — no direct ``ref_tags`` writes here.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from starlette.responses import Response

from precis_web.deps import (
    await_dispatch,
    get_store,
    redirect_or_error,
    templates,
)

router = APIRouter(prefix="/flags", tags=["flags"])

#: Namespace bare flag tags land under (the ``tag`` verb stores an
#: un-prefixed value as ``OPEN:<value>``; this is the namespace
#: :meth:`Store.ref_tag_values` / :meth:`Store.has_tag` probe with).
FLAG_NAMESPACE = "OPEN"

#: The reading-intent flag vocabulary, in display order. ``value`` is
#: the bare tag (stored ``OPEN:<value>``); ``emoji`` + ``label`` render
#: the button; ``title`` is the hover tooltip. Kept small on purpose —
#: extend here and every item list picks the new button up.
FLAG_DEFS: tuple[dict[str, str], ...] = (
    {
        "value": "read-later",
        "emoji": "📖",
        "label": "Read later",
        "title": "Flag to read when it lands (rides through ingest)",
    },
    {
        "value": "must-read",
        "emoji": "⭐",
        "label": "Must-read",
        "title": "Priority — read this first",
    },
    {
        "value": "skim",
        "emoji": "👀",
        "label": "Skim",
        "title": "Just want the gist",
    },
)

#: Acquisition-attempt provenance for paper stubs we can't auto-fetch —
#: a second flag axis surfaced on ``/papers-needed``. Same one-click
#: ``OPEN:<value>`` tag mechanism as the reading-intent flags, but a
#: distinct group so it doesn't leak onto every generic item list. The
#: point is a visible record of which manual route was already tried
#: (so you don't chase the same dead end twice) plus a "this ref is
#: junk" marker.
ACQUIRE_FLAG_DEFS: tuple[dict[str, str], ...] = (
    {
        "value": "cant-get-uol",
        "emoji": "📕",
        "label": "No UoL",
        "title": "Tried the University Library — couldn't get it",
    },
    {
        "value": "cant-get-scholar",
        "emoji": "🎓",
        "label": "No Scholar",
        "title": "Tried Google Scholar — couldn't get it",
    },
    {
        "value": "invalid-paper",
        "emoji": "🚫",
        "label": "Invalid",
        "title": "Bad reference — not a real / retrievable paper",
    },
    {
        "value": "is-book",
        "emoji": "📚",
        "label": "Book",
        "title": "This is a book, not a paper — sink it to the back",
    },
)

#: The flag groups, keyed by the ``group`` a button posts. Each group
#: is an independent toggle set rendered by the shared partial; the
#: route re-renders exactly the posting group after a toggle. Extend a
#: group here and its item lists pick the new button up.
FLAG_GROUPS: dict[str, tuple[dict[str, str], ...]] = {
    "reading": FLAG_DEFS,
    "acquire": ACQUIRE_FLAG_DEFS,
}

#: Fast membership set for request validation (union over all groups).
_FLAG_VALUES: frozenset[str] = frozenset(
    d["value"] for defs in FLAG_GROUPS.values() for d in defs
)

#: Ordered list of every flag value — the batched
#: :meth:`Store.ref_tag_values` probe argument for a list view. A list
#: only renders its own group's buttons, so probing the union is a
#: harmless superset.
FLAG_VALUE_LIST: list[str] = [d["value"] for defs in FLAG_GROUPS.values() for d in defs]


def _safe_local_redirect(return_to: str, fallback: str) -> str:
    """Constrain a ``return_to`` to a local path (no open redirect).

    Accepts only a same-origin absolute path (``/…``); a
    protocol-relative ``//host`` or an absolute URL falls back.
    """
    if return_to.startswith("/") and not return_to.startswith("//"):
        return return_to
    return fallback


@router.post("/{kind}/{ref_id}", response_model=None)
async def toggle(
    request: Request,
    kind: str,
    ref_id: int,
    flag: str = Form(...),
    op: str = Form("add"),
    group: str = Form("reading"),
    return_to: str = Form("/papers-needed"),
) -> Response:
    """Add or remove one flag on a ref, then bounce back.

    ``group`` selects the toggle set (:data:`FLAG_GROUPS` — ``reading``
    default, or ``acquire`` for the paper-stub acquisition-provenance
    buttons); ``flag`` is validated against *that* group so a value
    can't cross groups. ``op`` is ``add`` (default) or ``remove`` — the
    button sends ``remove`` when the flag is already active so a second
    click toggles it off. Dispatched through the ``tag`` verb so a
    rejected mutation renders the handler's own error instead of a
    silent redirect. Idempotent: re-adding a present tag (or removing
    an absent one) is a no-op.

    htmx requests get just the re-rendered button group back (swapped
    in place) so the list stays put — no full reload, no scroll-to-top.
    Non-htmx (no-JS fallback) still 303-redirects back to ``return_to``.
    """
    redirect = _safe_local_redirect(return_to, "/papers-needed")
    defs = FLAG_GROUPS.get(group)
    if defs is None or flag not in {d["value"] for d in defs}:
        return RedirectResponse(url=redirect, status_code=303)
    key = "remove" if op == "remove" else "add"
    args = {"kind": kind, "id": ref_id, key: [flag]}
    if request.headers.get("HX-Request") != "true":
        return await redirect_or_error(
            request,
            "tag",
            args,
            redirect=redirect,
            error_title="Flag error",
        )
    # htmx path: dispatch, then swap the fresh button group in place.
    _body, is_error = await await_dispatch(request, "tag", args)
    if is_error:
        return templates.TemplateResponse(
            request,
            "error.html.j2",
            {"title": "Flag error", "detail": _body, "status": 400},
            status_code=400,
        )
    store = get_store(request)
    active = store.ref_tag_values([ref_id], FLAG_NAMESPACE, FLAG_VALUE_LIST).get(
        ref_id, set()
    )
    return templates.TemplateResponse(
        request,
        "_flag_buttons.html.j2",
        {
            "flag_defs": defs,
            "group": group,
            "active": active,
            "ref_kind": kind,
            "ref_id": ref_id,
            "return_to": return_to,
        },
    )
