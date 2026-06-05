"""bge-m3 embeddings of every tag in use, for semantic discovery.

The agent-facing ``kind='tag'`` handler needs to answer "find tags
related to X" without the LLM having to scroll the whole tag corpus.
This worker keeps one row per ``(namespace, value)`` in
``tag_embeddings`` with a vector embedded from the canonical slug
form (``STATUS:done``, ``topic:co2-capture``, ``pinned``).

Mirrors :mod:`precis.workers.chunk_keywords`:

1. Claim — :meth:`Store.unembedded_tags` returns tags missing from
   ``tag_embeddings`` or whose stored ``version`` is below
   :data:`TAG_EMBEDDINGS_VERSION`. ``FOR UPDATE SKIP LOCKED`` on
   ``tags`` keeps concurrent workers off the same rows.
2. Embed — one batched ``embedder.embed`` call per pass.
3. Write — :meth:`Store.write_tag_embedding` upserts each row.

Bump :data:`TAG_EMBEDDINGS_VERSION` to invalidate every existing
row's stored version; the next pass re-claims them.

Tags change at a different cadence than chunks (rare bursts: when
a kind opts in to a new closed prefix, or when the corpus grows
into a new topic). Default loop sleep is fine; idle most of the
time.
"""

from __future__ import annotations

import logging
from typing import Any

from precis.embedder import Embedder

log = logging.getLogger(__name__)

#: Bump on canonicalisation / model changes so the lazy-update
#: claim query re-picks every row.
TAG_EMBEDDINGS_VERSION = 1


def _slug_for(namespace: str, value: str) -> str:
    """Canonical agent-facing string for a ``(namespace, value)`` tag.

    Mirrors the slug grammar the handler accepts on ``id=``:

    * ``namespace='OPEN'``  → ``value`` verbatim (already carries the
      lowercase prefix if any — ``topic:co2-capture``).
    * ``namespace='FLAG'``  → ``value`` verbatim (bare flag).
    * everything else       → ``f"{namespace}:{value}"`` (closed
      axis: ``STATUS:done``, ``CACHE:fresh``, …).
    """
    if namespace in ("OPEN", "FLAG"):
        return value
    return f"{namespace}:{value}"


def run_tag_embeddings_pass(
    store: Any,
    embedder: Embedder,
    *,
    batch_size: int = 100,
) -> dict[str, int]:
    """One pass over the tag_embeddings queue.

    Returns ``{"claimed": N, "ok": K, "failed": F}``. ``claimed`` is
    the size of the claim batch; ``ok``/``failed`` sum to the same.
    Per-tag exceptions are caught and counted as failures so a
    single poison-pill row never crashes the pass — the row stays
    unembedded and gets re-claimed next pass (no failure-marker
    today; tag strings are pure ASCII at the boundary so a runtime
    embedder exception means the worker, not the tag).
    """
    claimed = ok = failed = 0
    pairs = store.unembedded_tags(limit=batch_size, version=TAG_EMBEDDINGS_VERSION)
    claimed = len(pairs)
    if not pairs:
        return {"claimed": 0, "ok": 0, "failed": 0}

    # One batched embed call per pass — bge-m3 amortises far better
    # than N round-trips.
    slugs = [_slug_for(ns, v) for (ns, v) in pairs]
    try:
        vecs = embedder.embed(slugs)
    except Exception:
        log.exception("tag_embeddings: batched embed call failed")
        return {"claimed": claimed, "ok": 0, "failed": claimed}

    for (namespace, value), vec in zip(pairs, vecs, strict=True):
        try:
            store.write_tag_embedding(
                namespace=namespace,
                value=value,
                vector=vec,
                embedder=embedder.model,
                version=TAG_EMBEDDINGS_VERSION,
            )
            ok += 1
        except Exception:
            log.exception("tag_embeddings: write failed for (%s, %s)", namespace, value)
            failed += 1
    return {"claimed": claimed, "ok": ok, "failed": failed}


__all__ = [
    "TAG_EMBEDDINGS_VERSION",
    "run_tag_embeddings_pass",
]
