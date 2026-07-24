"""Papers-needed tab — retired into the unified Drive surface (WS1b).

``/papers-needed`` (and its ``?awaiting=1`` narrowing) used to render the
chunkless paper-stub backlog directly; per
``docs/proposals/web-ui-rationalization.md`` decision D3 it now just
redirects to Drive's ``state=stub`` facet (the "papers to get" queue) —
fully folded, no retained nav badge (the fetcher works the backlog
automatically; that was the original rationale for keeping it out of
Needs-you too, see ``routes/needs_you.py``). The acquisition-provenance
flag buttons (:data:`precis_web.routes.flags.ACQUIRE_FLAG_DEFS`) ride
along on Drive's stub rows (``templates/drive/index.html.j2``).

This module still owns the watch-dir drop-zone helpers
(:func:`_watch_dir_from_plist`, :data:`_KIND_DROPZONES`) — ``routes/drive.py``
imports them for its own drop-zone panel (a WS1a graft), so they stay here
rather than duplicating.
"""

from __future__ import annotations

import plistlib
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import RedirectResponse

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


@router.get("", response_class=RedirectResponse)
@router.get("/", response_class=RedirectResponse)
async def index() -> RedirectResponse:
    """Retired into the unified Drive surface (WS1b, decision D3) —
    redirects to the ``state=stub`` facet (the old ``?awaiting=1``
    next-pass narrowing has no Drive equivalent and is dropped with the
    list; the fetcher already works the whole backlog automatically)."""
    return RedirectResponse(url="/drive?state=stub")
