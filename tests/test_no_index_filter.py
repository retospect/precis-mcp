"""``meta.no_index`` chunks are skipped by the indexer passes.

precis-dft's ``view_worker`` writes annotation chunks on every
edit of a ``structure_draft``. Those chunks churn rapidly and
hold render-state, not search-state — they shouldn't go through
the keyword extractor or the embedder. The
``chunks.meta->>'no_index' = 'true'`` opt-out keeps the indexer
workload bounded.

These tests verify the SQL predicate is in the claim queries.
End-to-end coverage (insert chunks, run the worker, assert which
ones get keywords) needs Postgres and is exercised on the
``fresh_db`` fixture.
"""

from __future__ import annotations

import inspect

from precis.workers import chunk_keywords
from precis.workers.base import WorkerHandler


def test_chunk_keywords_claim_filters_no_index() -> None:
    """``claim_chunks_without_keywords`` honours ``meta.no_index``.

    Static check that the SQL string contains the predicate. A
    full integration test runs through ``test_chunk_keywords.py``
    against ``fresh_db``.
    """
    source = inspect.getsource(chunk_keywords.claim_chunks_without_keywords)
    assert "'no_index'" in source, (
        "chunk_keywords claim SQL must filter chunks tagged "
        "meta.no_index='true' (precis-dft view chunks)"
    )
    assert "IS DISTINCT FROM" in source, (
        "use IS DISTINCT FROM 'true' for NULL-safe comparison so "
        "untagged chunks (the vast majority) still match"
    )


def test_worker_handler_claim_filters_no_index() -> None:
    """``WorkerHandler.claim_batch`` honours ``meta.no_index``.

    Shared base class for ``EmbedHandler`` and the other chunk-
    derived workers; one filter covers them all.
    """
    source = inspect.getsource(WorkerHandler.claim_batch)
    assert "'no_index'" in source
    assert "IS DISTINCT FROM" in source
