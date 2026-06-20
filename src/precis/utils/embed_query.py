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
"""

from __future__ import annotations

import logging
from typing import Any

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
