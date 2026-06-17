"""Papers-needed tab — chunkless paper stubs awaiting fetch.

Also surfaces the cluster's drop-zone paths so the operator knows
where to put files for manual ingest. The ``precis watch`` daemon
on melchior monitors a watch-dir; sub-directories under it select
the ingest kind (``papers/`` → paper pipeline, ``books/`` → book,
``presentations/`` → slides). We read the watch daemon's plist to
find the live path so the operator gets the actual path, not a
guess.


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

import plistlib
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from precis_web.deps import get_store, templates

router = APIRouter(prefix="/papers-needed", tags=["papers-needed"])

_WATCH_PLIST = Path("/Library/LaunchDaemons/com.precis.watch.plist")


def _watch_dir_from_plist() -> str | None:
    """Lift the watch-dir argument out of ``precis watch``'s plist.

    The plist invokes ``bash -c "exec /opt/precis/venv/bin/precis
    watch <flags> <watch_dir>"`` — the watch_dir is the last
    whitespace-separated token in the bash command. Returns ``None``
    when the plist isn't readable so the template falls back to a
    placeholder hint.
    """
    if not _WATCH_PLIST.exists():
        return None
    try:
        with _WATCH_PLIST.open("rb") as fh:
            payload = plistlib.load(fh)
    except Exception:
        return None
    args = payload.get("ProgramArguments") or []
    if not isinstance(args, list) or not args:
        return None
    # Find the bash -c command argument (longest string, contains
    # 'precis watch'). The watch_dir is the final positional in it.
    for tok in args:
        if isinstance(tok, str) and "precis watch" in tok:
            # Split into shell-style tokens; walk backwards for the
            # first absolute path that isn't preceded by a flag.
            parts = tok.split()
            for i in range(len(parts) - 1, -1, -1):
                p = parts[i]
                # Skip flag values (preceded by a ``--flag``).
                if i > 0 and parts[i - 1].startswith("--"):
                    continue
                if p.startswith("/") and "/" in p[1:]:
                    return p
    return None


#: Per-kind drop-zone routing. Mirrors ``_KIND_DIRS`` in
#: ``src/precis/cli/watch.py`` — keep these in sync when adding a
#: new kind to the watcher.
_KIND_DROPZONES: tuple[tuple[str, str, str], ...] = (
    (
        "Papers (PDFs)",
        "papers",
        "PDFs of journal articles, preprints, theses. Marker-pdf "
        "extracts text + structure, chunker splits, embedder + "
        "chunk_keywords pick up the chunks.",
    ),
    (
        "Books",
        "books",
        "Long-form PDFs (>50 pages). Chunked the same way as papers "
        "but at the book corpus.",
    ),
    (
        "Presentations (slides)",
        "presentations",
        "Slide-deck PDFs. Same pipeline as papers but tagged as "
        "presentations so the slug pattern differs.",
    ),
)


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
    watch_dir = _watch_dir_from_plist()
    dropzones: list[dict[str, str]] = []
    if watch_dir:
        for label, sub, description in _KIND_DROPZONES:
            dropzones.append(
                {
                    "label": label,
                    "path": str(Path(watch_dir) / sub),
                    "description": description,
                }
            )
    return templates.TemplateResponse(
        request,
        "papers_needed/index.html.j2",
        {
            "active_tab": "papers-needed",
            "rows": display,
            "awaiting": awaiting_flag,
            "watch_dir": watch_dir,
            "dropzones": dropzones,
        },
    )
