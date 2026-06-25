"""Needs-you tab — the unified "waiting on you" queue.

Folds the two queues where *you* are the blocker into one landing:

* **Asks** — open ``kind='todo'`` refs carrying an ``ask-user`` tag
  (the planner is paused on a question). Rendered fully interactive:
  answer inline to unlock the todo. The answer / dismiss forms POST to
  ``/asks/...`` — the standalone Asks page is kept for those deep links
  and for its own pager.
* **Papers needed** — the chunkless paper-stub backlog
  (``store.stub_backlog``). Shown as a compact preview; the full
  backlog page (pager, ``?awaiting=1`` filter, drop-zone hints) stays
  at ``/papers-needed``.

The top-bar "Needs you" badge counts both — see :mod:`precis_web.nav`.
This route is the badge's destination; it does not own the writes.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from precis_web.deps import get_store, templates
from precis_web.routes.asks import _load_asks
from precis_web.routes.papers_needed import _doi_url, _title_for_ref

router = APIRouter(prefix="/needs-you", tags=["needs-you"])

#: Asks shown inline (every ask is actionable; this is just a sanity cap).
_ASK_CAP = 50
#: Stub rows previewed inline before the "view all" deep-link.
_STUB_PREVIEW = 20


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    """Combined asks + papers-needed landing."""
    store = get_store(request)
    asks = _load_asks(store, limit=_ASK_CAP)

    stub_total = store.stub_backlog_count(awaiting=False)
    stub_rows = store.stub_backlog(limit=_STUB_PREVIEW, awaiting=False)
    refs = store.fetch_refs_by_ids(
        [row["ref_id"] for row in stub_rows], include_deleted=False
    )
    stubs: list[dict[str, Any]] = []
    for row in stub_rows:
        rid = row["ref_id"]
        stubs.append(
            {
                "id": rid,
                "title": _title_for_ref(refs, rid),
                "cite_key": row["cite_key"],
                "identifier": row["identifier"],
                "identifier_url": _doi_url(row["identifier"]),
                "state": row["state"],
                "last_attempt": row["last_attempt"],
            }
        )

    return templates.TemplateResponse(
        request,
        "needs_you/index.html.j2",
        {
            "active_tab": "needs-you",
            "asks": asks,
            "stubs": stubs,
            "stub_total": stub_total,
            "stub_more": max(0, stub_total - len(stubs)),
        },
    )
