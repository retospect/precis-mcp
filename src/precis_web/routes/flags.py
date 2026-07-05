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

from precis_web.deps import redirect_or_error

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

#: Fast membership set for request validation.
_FLAG_VALUES: frozenset[str] = frozenset(d["value"] for d in FLAG_DEFS)

#: Ordered list of just the values — the batched
#: :meth:`Store.ref_tag_values` probe argument for a list view.
FLAG_VALUE_LIST: list[str] = [d["value"] for d in FLAG_DEFS]


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
    return_to: str = Form("/papers-needed"),
) -> Response:
    """Add or remove one reading-intent flag on a ref, then bounce back.

    ``flag`` is validated against :data:`FLAG_DEFS`; ``op`` is
    ``add`` (default) or ``remove`` — the button sends ``remove`` when
    the flag is already active so a second click toggles it off.
    Dispatched through the ``tag`` verb so a rejected mutation renders
    the handler's own error instead of a silent redirect. Idempotent:
    re-adding a present tag (or removing an absent one) is a no-op.
    """
    redirect = _safe_local_redirect(return_to, "/papers-needed")
    if flag not in _FLAG_VALUES:
        return RedirectResponse(url=redirect, status_code=303)
    key = "remove" if op == "remove" else "add"
    return await redirect_or_error(
        request,
        "tag",
        {"kind": kind, "id": ref_id, key: [flag]},
        redirect=redirect,
        error_title="Flag error",
    )
