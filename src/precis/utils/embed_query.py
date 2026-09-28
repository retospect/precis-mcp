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

Gripe #450123 (option d): a busy-but-alive embedder service (bounded
admission queue timed out) is a *different* failure than a genuinely
down one — the retry is short and the corpus is not in question.
:func:`semantic_unavailable_upstream` builds that message once so
:func:`query_vec_for` here and ``_embed_query_batch`` in
``handlers/_paper_search.py`` (the batch/broad-retrieval door with the
same mode='semantic' loudness split) can't drift apart.
"""

from __future__ import annotations

import logging
from typing import Any

from precis.embedder import EmbedderUnavailable
from precis.errors import Upstream

log = logging.getLogger(__name__)


def _embedder_unavailable_cause(exc: BaseException) -> EmbedderUnavailable | None:
    """Walk ``exc.__cause__`` for an :class:`EmbedderUnavailable`, if any.

    ``RemoteEmbedder`` raises it directly, but a wrapping layer (a
    batch helper, a retry shim) may re-raise a different exception
    ``from`` it — walk the chain rather than assume it's ``exc`` itself.
    """
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        if isinstance(cur, EmbedderUnavailable):
            return cur
        seen.add(id(cur))
        cur = cur.__cause__
    return None


def semantic_unavailable_upstream(exc: Exception) -> Upstream:
    """Build the ``Upstream`` raised when an explicit ``mode='semantic'``
    leg's embed call fails — factored out so the two call sites (here,
    and ``_paper_search._embed_query_batch``) can't drift.

    When the failure chains back to an :class:`EmbedderUnavailable`
    carrying ``retry_after_s`` (the service's bounded admission queue
    timed out, gripe #450123), the message names the capacity cause and
    the concrete retry window instead of a bare "unavailable" — capacity
    is not absence, and a caller that reads a zero-hit degrade as "does
    not exist" has corrupted campaigns before (gripe #254606).
    """
    cause = _embedder_unavailable_cause(exc)
    if cause is not None and cause.retry_after_s is not None:
        n = cause.retry_after_s
        return Upstream(
            "query embedder at capacity — the explicit mode='semantic' "
            "leg cannot run (zero hits here would be a false answer); "
            f"the service asked for a retry in ~{n:g} s",
            next=(
                f"wait ~{n:g} s and retry the same call; or use "
                "mode='hybrid' to accept lexical-only degrade. Capacity, "
                "not absence: do not conclude the hub or passage does "
                "not exist"
            ),
        )
    next_text = "retry, or use mode='hybrid' to accept lexical-only degrade"
    if cause is not None:
        next_text += (
            ". Capacity, not absence: do not conclude the hub or passage does not exist"
        )
    return Upstream(
        "query embedder unavailable — the explicit mode='semantic' "
        "leg cannot run (zero hits here would be a false answer)",
        next=next_text,
    )


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
            raise semantic_unavailable_upstream(exc) from exc
    return embed_query(embedder, q)
