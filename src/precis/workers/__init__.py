"""Derived-artifact workers (per ADR 0007).

The worker's "queue" is the data itself: a chunk that has no row in
``chunk_embeddings`` for embedder ``bge-m3`` *needs* to be embedded.
No separate ``block_jobs`` / queue table — derived artifact tables
double as the work-tracking surface.

Each :class:`WorkerHandler` owns one ``(output_table, model)`` pair
and exposes a uniform contract:

* :meth:`claim_batch` — ``LEFT JOIN`` chunks against the output
  table; lock chunk rows ``FOR UPDATE OF c SKIP LOCKED`` so
  concurrent workers don't double-process the same chunk.
* :meth:`process` — the actual computation (embed text, summarise,
  …). Pure: must not touch the DB.
* :meth:`write_ok` / :meth:`write_failed` — INSERT a result row,
  status ``'ok'`` or ``'failed'``. Failure marker rows mean a
  poison-pill chunk is *not* re-claimed forever.
* :meth:`status` — return ``(total, ok, failed, pending)`` for
  ``precis worker --status`` / ``precis health``.

The :func:`run_handler_once` orchestrator threads chunk rows through
those four methods in a single transaction; :func:`run_loop` polls
all registered handlers in round-robin until they return zero
claimed rows, then sleeps and re-polls.
"""

from precis.workers.base import (
    ArtifactStatus,
    ChunkRow,
    WorkerHandler,
)
from precis.workers.embed import EmbedHandler
from precis.workers.runner import (
    BatchResult,
    run_handler_once,
    run_loop,
)
from precis.workers.summarize import RakeLemmaHandler

__all__ = [
    "ArtifactStatus",
    "BatchResult",
    "ChunkRow",
    "EmbedHandler",
    "RakeLemmaHandler",
    "WorkerHandler",
    "run_handler_once",
    "run_loop",
]
