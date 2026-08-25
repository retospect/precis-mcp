"""Safe query-embedding for search verbs.

Every search verb fuses a lexical leg with a semantic leg; the semantic
leg needs the query embedded into a vector. A *missing* embedder already
degrades to lexical-only by design. A *failing* embedder — a remote
embed endpoint that's down, a cold model that raises, a degenerate query
the model rejects — must degrade the **same** way rather than escape as
an internal server error.

Before this helper, handlers called ``self.embedder.embed_one(q)``
unguarded, so any embedder hiccup surfaced to the agent as a bare 500
(gripes #38684 ``search(kind='paper', q='*')`` and #38690
``search(kind='skill', …)``). Routing every search-time embed through
:func:`embed_query` makes the degrade uniform and logged.

One deliberate exception: an explicit ``mode='semantic'`` request with
a wired-but-failing embedder raises loudly instead of degrading — the
caller asked for the vector leg by name, and silently answering with
zero hits reads as "no matches" and has corrupted a campaign (gripe
#254606). :func:`query_vec_for` owns that split.
"""

from __future__ import annotations

import logging
from typing import Any

from precis.errors import Upstream

log = logging.getLogger(__name__)


def embed_query(embedder: Any | None, q: str) -> list[float] | None:
    """Embed a search query, degrading to ``None`` on any failure.

    Returns the query vector, or ``None`` to signal "run lexical-only"
    — both when no embedder is wired and when the embedder raises.
    Never propagates; a failed embed is logged at WARNING with the
    traceback so the operator can see the underlying cause.
    """
    if embedder is None:
        return None
    try:
        return embedder.embed_one(q)
    except Exception:
        log.warning(
            "embed_query: query embed failed for %r; falling back to lexical-only",
            q,
            exc_info=True,
        )
        return None


def query_vec_for(
    embedder: Any | None, q: str, mode: str | None = None
) -> list[float] | None:
    """Query vector for a search, honouring an explicit ``mode=``.

    ``mode='lexical'`` and ``mode='verbatim'`` skip the embed entirely
    (return ``None``) so the store dispatcher runs a pure keyword pass —
    the deterministic FTS / GIN-containment paths, embedder-independent.
    Every other mode (``'hybrid'`` default, ``'semantic'``) embeds via
    :func:`embed_query`.

    Failure handling splits on intent (gripe #254606): with the default
    / ``'hybrid'`` mode, or with no embedder wired at all, an embed
    failure degrades to ``None`` (the lexical leg still answers). But an
    explicit ``mode='semantic'`` is a statement that the caller needs
    the vector leg — a wired-but-raising embedder there raises
    :class:`~precis.errors.Upstream` instead of silently returning zero
    hits the caller will misread as "no matches in the corpus".
    """
    m = mode.strip().lower() if mode is not None else None
    if m in ("lexical", "verbatim"):
        return None
    if m == "semantic" and embedder is not None:
        try:
            return embedder.embed_one(q)
        except Exception as exc:
            log.warning(
                "query_vec_for: semantic-mode query embed failed for %r",
                q,
                exc_info=True,
            )
            raise Upstream(
                "query embedder unavailable — the explicit mode='semantic' "
                "leg cannot run (zero hits here would be a false answer)",
                next="retry, or use mode='hybrid' to accept lexical-only degrade",
            ) from exc
    return embed_query(embedder, q)
