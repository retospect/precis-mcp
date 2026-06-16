"""Papers-needed tab — chunkless paper stubs awaiting fetch.

A *stub* is a ``kind='paper'`` ref minted with a DOI / arXiv / S2
identifier but no PDF yet (``pdf_sha256 IS NULL``). The ``fetch_oa``
worker cascades Unpaywall → arXiv → Semantic Scholar trying to land
the PDF; this page surfaces the backlog so the operator can see
what's still missing and intervene (manual upload, paywall pay-out,
or mark won't-do).

Two views:

* ``/papers-needed`` — full backlog, newest stubs first
* ``/papers-needed?awaiting=1`` — only stubs the fetcher would
  actually try on its next pass (never attempted or attempted >24h
  ago and still pending)

Shares ``store.stub_backlog()`` with the ``precis stubs`` CLI, so
both views render the same data shape.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from precis_web.deps import get_store, templates

router = APIRouter(prefix="/papers-needed", tags=["papers-needed"])


def _title_for_ref(refs: dict[int, Any], ref_id: int) -> str:
    """Best-effort title for a stub. Refs landed via DOI lookup carry
    the publisher's title in ``refs.title``; refs minted from
    arXiv-only sometimes have only an identifier and an empty title.
    Fall back to the cite_key / identifier so the row is still
    distinguishable.
    """
    ref = refs.get(ref_id)
    if ref is None:
        return ""
    title = (getattr(ref, "title", None) or "").strip()
    return title


def _doi_url(identifier: str) -> str:
    """Build a clickable DOI / arXiv URL from a stub_backlog identifier.

    ``stub_backlog`` returns bare DOIs (10.…), ``arxiv:NNNN``, or
    ``s2:<hash>``. Render the publisher / arXiv URL for the first
    two so the operator can verify the cite with one click.
    """
    if not identifier:
        return ""
    if identifier.startswith("arxiv:"):
        return f"https://arxiv.org/abs/{identifier.removeprefix('arxiv:')}"
    if identifier.startswith("10."):
        return f"https://doi.org/{identifier}"
    return ""


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def index(request: Request, awaiting: int | None = None) -> HTMLResponse:
    """Backlog list. ``?awaiting=1`` narrows to fetcher's next-pass queue."""
    store = get_store(request)
    awaiting_flag = bool(awaiting)
    rows = store.stub_backlog(limit=200, awaiting=awaiting_flag)
    refs = store.fetch_refs_by_ids(
        [row["ref_id"] for row in rows], include_deleted=False
    )
    display: list[dict[str, Any]] = []
    for row in rows:
        rid = row["ref_id"]
        display.append(
            {
                "id": rid,
                "title": _title_for_ref(refs, rid),
                "cite_key": row["cite_key"],
                "identifier": row["identifier"],
                "identifier_url": _doi_url(row["identifier"]),
                "state": row["state"],
                "last_attempt": row["last_attempt"],
                "last_event": row["last_event"],
            }
        )
    return templates.TemplateResponse(
        request,
        "papers_needed/index.html.j2",
        {
            "active_tab": "papers-needed",
            "rows": display,
            "awaiting": awaiting_flag,
        },
    )
