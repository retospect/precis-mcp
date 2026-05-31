"""Embedder worker handler.

Wraps a :class:`precis.embedder.Embedder` (``MockEmbedder`` in tests,
``BgeM3Embedder`` in production) and writes one ``chunk_embeddings``
row per processed chunk.

The vector dimension is enforced by the column type
(``vector(1024)`` for the seeded ``bge-m3`` row in ``embedders``).
Mismatched-dim embedders raise on INSERT — caller should pre-check
``embedder.dim`` before constructing the handler.
"""

from __future__ import annotations

from typing import ClassVar

from psycopg import Connection

from precis.embedder import Embedder
from precis.workers.base import ChunkRow, WorkerHandler


class EmbedHandler(WorkerHandler):
    """Compute and persist a dense vector for each chunk.

    The handler's ``model_name`` is taken from the wrapped
    ``embedder.model`` so registering a new embedder is just
    ``INSERT INTO embedders (...)`` plus instantiating ``EmbedHandler``
    with that embedder.
    """

    output_table: ClassVar[str] = "chunk_embeddings"
    model_column: ClassVar[str] = "embedder"
    # Storage-v2 contract: bibliographies don't earn their search
    # weight. We tag them ``chunk_kind='references'`` at ingest (see
    # ``precis.ingest.pipeline._retag_references``) so the worker
    # claim query can drop them before they ever reach the embedder.
    skip_chunk_kinds: ClassVar[tuple[str, ...]] = ("references",)

    def __init__(self, embedder: Embedder) -> None:
        self._embedder = embedder
        # Two distinct attrs deliberately: ``model_name`` is the FK
        # value (matches a row in the ``embedders`` table); ``name``
        # is a human-friendly handler label used in logs and status
        # output. They share a string today but diverge if we ever
        # ship two flavours of the same model.
        self.model_name = embedder.model
        self.name = f"embed:{embedder.model}"

    @property
    def embedder(self) -> Embedder:
        """The wrapped embedder (exposed for tests + observability)."""
        return self._embedder

    # ------------------------------------------------------------------
    # process — pure compute (delegate to embedder)
    # ------------------------------------------------------------------

    def process(self, row: ChunkRow) -> list[float]:
        """Return the dense vector for ``row.text``.

        ``Embedder.embed_one`` performs L2 normalization and any
        per-model truncation guards (see ``BgeM3Embedder._BGE_M3_MAX_CHARS``).
        Empty text is *not* a special case — the embedder will produce
        a (possibly degenerate) vector and the runner records it as
        ``status='ok'``. If the caller wants to skip empty chunks, do
        it upstream in ingest, not here.
        """
        return self._embedder.embed_one(row.text)

    # ------------------------------------------------------------------
    # write_ok — INSERT into chunk_embeddings
    # ------------------------------------------------------------------

    def write_ok(self, conn: Connection, chunk_id: int, payload: object) -> None:
        """Persist the success row for ``chunk_id``.

        ``payload`` is ``list[float]`` from :meth:`process`. pgvector's
        psycopg adapter (registered per-connection in
        :func:`precis.store.pool._configure_connection`) accepts
        plain Python lists so we don't import numpy here.

        On primary-key conflict (same chunk_id + embedder) we update
        in place rather than failing — this lets the operator
        re-run by ``DELETE``-ing failed rows and immediately
        re-claiming, without first scrubbing any partial inserts.
        """
        if not isinstance(payload, list):  # pragma: no cover — defensive
            raise TypeError(
                f"EmbedHandler.write_ok expected list[float], got {type(payload).__name__}"
            )
        conn.execute(
            """
            INSERT INTO chunk_embeddings
                (chunk_id, embedder, vector, status)
            VALUES (%s, %s, %s, 'ok')
            ON CONFLICT (chunk_id, embedder) DO UPDATE
               SET vector = EXCLUDED.vector,
                   status = 'ok',
                   last_error = NULL,
                   attempts = chunk_embeddings.attempts + 1
            """,
            (chunk_id, self.model_name, payload),
        )


__all__ = ["EmbedHandler"]
