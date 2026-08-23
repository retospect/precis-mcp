"""Needs-you tab — the unified "waiting on you" queue.

Folds the two queues where *you* must act into one landing:

* **Asks** — open ``kind='todo'`` refs carrying an ``ask-user`` tag
  (the planner is paused on a question). Rendered fully interactive:
  answer inline to unlock the todo. The answer / dismiss forms POST to
  ``/asks/...`` — the standalone Asks page is kept for those deep links
  and for its own pager.
* **Proposed hypotheses** — ``kind='finding'`` claim hubs tagged
  ``hypothesis-proposed``: conjectures an agent prepared and gate-checked
  (``handlers/_finding_hypothesis.py``). Approve and sign are human doors
  by design, so a prepared proposal genuinely waits on a person. Each row
  links to its claim page, where the approve form arrives pre-filled.
* **Needs triage** — ``kind='paper'`` refs tagged ``needs-triage``
  (metadata automation gave up; a human must fix them). Shown as a
  compact preview linking each row to its detail page with the triage
  panel open (``?triage=1``); the full queue (pager) stays at
  ``/papers/triage``.

The chunkless paper-stub *fetch* backlog is intentionally NOT here —
the fetcher works it automatically, so it lives under Browse →
``/papers-needed``, not in the "needs you" queue.

The top-bar "Needs you" badge counts asks + triage + proposals — see
:mod:`precis_web.nav`. This route is the badge's destination; it does
not own the writes.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from precis.handlers._finding_hypothesis import PROPOSED_TAG
from precis_web.deps import get_store, templates
from precis_web.routes.asks import _load_asks
from precis_web.routes.papers import _TRIAGE_TAG

router = APIRouter(prefix="/needs-you", tags=["needs-you"])

#: Asks shown inline (every ask is actionable; this is just a sanity cap).
_ASK_CAP = 50
#: Triage rows previewed inline before the "view all" deep-link.
_TRIAGE_PREVIEW = 20
#: Proposed hypotheses previewed inline; the full lane lives on /nanopub.
_PROPOSED_PREVIEW = 20


def _load_proposed(
    store: Any, *, limit: int = _PROPOSED_PREVIEW
) -> tuple[list[dict[str, Any]], int]:
    """``(preview rows, total)`` for agent-proposed nanopub hypotheses.

    A module-level seam rather than inline, mirroring ``_load_asks`` — the
    count and the preview must read the same tag, and one function is
    easier to keep honest than two call sites.
    """
    total = store.count_refs(kind="finding", tags=[PROPOSED_TAG])
    rows = [
        {
            "id": r.id,
            "title": (getattr(r, "title", None) or "").strip() or f"#{r.id}",
        }
        for r in store.list_refs(kind="finding", tags=[PROPOSED_TAG], limit=limit)
    ]
    return rows, total


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    """Combined asks + needs-triage landing."""
    store = get_store(request)
    asks = _load_asks(store, limit=_ASK_CAP)

    triage_total = store.count_refs(kind="paper", tags=[_TRIAGE_TAG])
    triage_refs = store.list_refs(
        kind="paper", tags=[_TRIAGE_TAG], limit=_TRIAGE_PREVIEW
    )
    triage: list[dict[str, Any]] = []
    for r in triage_refs:
        title = (getattr(r, "title", None) or "").strip()
        triage.append(
            {
                "id": r.id,
                "title": title or getattr(r, "slug", None) or f"#{r.id}",
                "year": getattr(r, "year", None),
            }
        )

    proposed, proposed_total = _load_proposed(store, limit=_PROPOSED_PREVIEW)

    return templates.TemplateResponse(
        request,
        "needs_you/index.html.j2",
        {
            "active_tab": "needs-you",
            "asks": asks,
            "triage": triage,
            "triage_total": triage_total,
            "triage_more": max(0, triage_total - len(triage)),
            "proposed": proposed,
            "proposed_total": proposed_total,
            "proposed_more": max(0, proposed_total - len(proposed)),
        },
    )
