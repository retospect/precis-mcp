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
        # ``model_name`` / ``name`` are resolved lazily (see the
        # properties below), NOT here. Both read ``embedder.model``,
        # which for the remote backend is a ``GET /model`` round-trip.
        # Doing it in ``__init__`` meant a *down* embedder at worker
        # boot raised ``RuntimeError`` out of ``_build_handlers`` and
        # crash-looped the entire worker — taking summarize / chase /
        # fetch / dispatch down with the one pass that actually needs
        # the embedder. Deferring keeps construction network-free; only
        # the embed pass bears the dependency, the runner skips just
        # that pass (and retries it next cycle) while the embedder is
        # unreachable, and it recovers with no worker restart once the
        # embedder is back. ``embedder.model`` caches after the first
        # success, so this is a one-time round-trip.
        self._model_name: str | None = None
        self._name_override: str | None = None

    @property
    def embedder(self) -> Embedder:
        """The wrapped embedder (exposed for tests + observability)."""
        return self._embedder

    @property
    def model_name(self) -> str:
        """FK value for the ``embedder`` column — the wrapped embedder's
        model id, fetched from ``/model`` (and cached) on first use."""
        if self._model_name is None:
            self._model_name = self._embedder.model
        return self._model_name

    @model_name.setter
    def model_name(self, value: str) -> None:
        # Writeable to satisfy the base ``model_name: str`` contract
        # (and let tests pin it); normally resolved from the embedder.
        self._model_name = value

    @property
    def name(self) -> str:
        """Human-friendly handler label (``embed:<model>``), derived
        from :attr:`model_name` (and thus equally lazy) unless an
        explicit label was set."""
        if self._name_override is not None:
            return self._name_override
        return f"embed:{self.model_name}"

    @name.setter
    def name(self, value: str) -> None:
        # Writeable to satisfy the base ``name: str`` contract; the
        # label is normally derived from model_name, not set.
        self._name_override = value

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

    def process_batch(self, rows: list[ChunkRow]) -> list[object]:
        """Embed the whole claimed batch in one forward pass.

        ``Embedder.embed`` accepts ``list[str]`` and returns
        ``list[list[float]]`` with the same length, so we can feed
        the entire batch to BGE-M3 once instead of paying the
        per-call overhead 32 times per pass. Empty input list short-
        circuits to ``[]``.

        Whole-batch failure (OOM, model dim mismatch) falls back to
        per-row processing so a single poison-pill chunk gets a
        failure marker rather than poisoning the rest of the batch.
        """
        if not rows:
            return []
        try:
            vectors = self._embedder.embed([row.text for row in rows])
        except Exception:
            # Don't lose the whole batch on a single bad row. Per-row
            # path runs each chunk through embed_one and routes each
            # failure to write_failed via the runner.
            return super().process_batch(rows)
        return list(vectors)

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
