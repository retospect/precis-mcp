"""Worker handler base class and shared dataclasses.

The handler models *one* derived artifact: a ``(output_table,
model_column, model_name)`` triple plus a ``process`` callable.

Two concrete handlers ship today:

* :class:`precis.workers.embed.EmbedHandler` — writes
  ``chunk_embeddings`` rows.
* :class:`precis.workers.summarize.RakeLemmaHandler` — writes
  ``chunk_summaries`` rows.

The base class implements the SQL plumbing (claim, failure-marker,
status query) once; subclasses only override :meth:`process` and
:meth:`write_ok`. New artifact kinds are a one-file addition.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar

from psycopg import Connection


@dataclass(frozen=True)
class ChunkRow:
    """A chunk claimed for processing.

    Carries only the columns a handler needs to do its job (text)
    plus the primary key (chunk_id). Section path / metadata are
    not propagated: handlers either treat the chunk as opaque text
    (embedder, summarizer) or open a fresh query if they need more.
    """

    chunk_id: int
    text: str


@dataclass(frozen=True)
class ArtifactStatus:
    """Per-handler progress snapshot."""

    name: str
    total: int
    ok: int
    failed: int
    pending: int


# Cap how much of a Python exception string we persist into
# ``last_error``. Postgres TEXT has no length limit but a runaway
# traceback would balloon the row; 1 KiB is plenty for "what
# happened, was it transient?" without being a denial-of-service
# vector.
_LAST_ERROR_MAX_CHARS = 1000

#: A ``chunk_claims`` row older than this many minutes is treated as abandoned
#: (worker crashed/stalled mid-batch) and re-claimed. Only crash recovery
#: matters here — base-handler failures are terminal (ADR 0007), so they leave
#: no retrying claim. Must exceed the worst-case batch wall-time (the embedder
#: forward pass); generous is fine since a completed batch deletes its claims.
_CLAIM_COOLDOWN_MIN = 20


class WorkerHandler(ABC):
    """Base class for derived-artifact workers.

    Subclasses set the three ``ClassVar`` slots (``output_table``,
    ``model_column``, ``model_name``) plus a human-friendly
    :attr:`name` (e.g. ``'embed:bge-m3'``) and implement
    :meth:`process` + :meth:`write_ok`. Failure handling, claim
    queries, and status queries are inherited.
    """

    #: Output table this handler writes (e.g. ``chunk_embeddings``).
    output_table: ClassVar[str]
    #: Column in ``output_table`` that names the model
    #: (``embedder`` for embeddings; ``summarizer`` for summaries).
    model_column: ClassVar[str]

    #: Chunk kinds this handler must *not* process. Filters the claim
    #: query so references / boilerplate never enter the work queue
    #: at all (storage-v2 contract: references are excluded from
    #: default embedding to keep search clean). Empty tuple means
    #: "process every chunk regardless of kind."
    skip_chunk_kinds: ClassVar[tuple[str, ...]] = ()

    #: Per-instance — the model identifier used in the output row
    #: (e.g. ``'bge-m3'``). Set by subclass ``__init__``.
    model_name: str

    #: Human-friendly handler name for logs and ``--status`` output
    #: (e.g. ``'embed:bge-m3'``). Set by subclass ``__init__``.
    name: str

    # ------------------------------------------------------------------
    # Claim — lease via the shared ``chunk_claims`` table
    # ------------------------------------------------------------------

    def claim_batch(self, conn: Connection, *, limit: int) -> list[ChunkRow]:
        """Lease up to ``limit`` chunks needing this artifact.

        Writes a ``chunk_claims`` row (``artifact = model_name``) for each
        claimed chunk in the same statement and returns its data. The caller
        commits promptly (releasing the ``FOR UPDATE`` lock) and does the slow
        work with NO open transaction — so the batch's processing time is never
        held as an open transaction pinning the xmin horizon (the old
        lock-across-``process`` behaviour starved autovacuum).

        Two sources: FRESH chunks (no current artifact, no claim) then, if the
        batch isn't full, RECLAIM of stale claims (a worker that crashed
        mid-batch — base-handler failures are terminal, so there are no
        retrying claims). Empty list means "no work right now".
        """
        if limit <= 0:
            raise ValueError("limit must be positive")
        claimed = self._claim_fresh(conn, limit=limit)
        if len(claimed) < limit:
            claimed += self._claim_reclaim(conn, limit=limit - len(claimed))
        return claimed

    def _skip_clause(self, alias: str) -> tuple[str, list[str]]:
        """`(sql_fragment, skip_kinds_list)` for the chunk-kind filter, empty
        when the handler processes every kind. ``= ANY`` keeps the param shape
        uniform regardless of list length."""
        skip = self.skip_chunk_kinds
        if not skip:
            return "", []
        return f"AND {alias}.chunk_kind <> ALL(%(skip_kinds)s)", list(skip)

    def _claim_fresh(self, conn: Connection, *, limit: int) -> list[ChunkRow]:
        # A chunk needs work when it has no *current, non-failed* artifact row:
        # no row at all, or a row built against a stale content_sha (re-derive
        # edited `draft` chunks, ADR 0033 — papers leave content_sha NULL so
        # NULL IS DISTINCT FROM NULL never fires). `failed` rows are terminal
        # (a poison chunk must not loop) — they count as "done" until a manual
        # DELETE, per ADR 0007. The second NOT EXISTS skips chunks already
        # leased by a live claim. `meta->>'no_index'` excludes ephemeral chunks.
        skip_clause, skip_kinds = self._skip_clause("c")
        sql = f"""
            WITH cand AS (
                SELECT c.chunk_id, c.text
                  FROM chunks c
                 WHERE NOT EXISTS (
                           SELECT 1 FROM {self.output_table} o
                            WHERE o.chunk_id = c.chunk_id
                              AND o.{self.model_column} = %(artifact)s
                              AND (o.status = 'failed'
                                   OR o.content_sha IS NOT DISTINCT FROM c.content_sha)
                       )
                   AND NOT EXISTS (
                           SELECT 1 FROM chunk_claims cl
                            WHERE cl.chunk_id = c.chunk_id AND cl.artifact = %(artifact)s
                       )
                   AND (c.meta->>'no_index') IS DISTINCT FROM 'true'
                   {skip_clause}
                 ORDER BY c.chunk_id
                 LIMIT %(limit)s
                   FOR UPDATE OF c SKIP LOCKED
            ),
            claimed AS (
                INSERT INTO chunk_claims (chunk_id, artifact)
                SELECT chunk_id, %(artifact)s FROM cand
                ON CONFLICT (chunk_id, artifact) DO NOTHING
                RETURNING chunk_id
            )
            SELECT cand.chunk_id, cand.text
              FROM cand JOIN claimed USING (chunk_id)
        """
        params: dict[str, object] = {"artifact": self.model_name, "limit": limit}
        if skip_kinds:
            params["skip_kinds"] = skip_kinds
        rows = conn.execute(sql, params).fetchall()
        return [ChunkRow(chunk_id=int(r[0]), text=str(r[1])) for r in rows]

    def _claim_reclaim(self, conn: Connection, *, limit: int) -> list[ChunkRow]:
        skip_clause, skip_kinds = self._skip_clause("c")
        sql = f"""
            WITH cand AS (
                SELECT cl.chunk_id, c.text
                  FROM chunk_claims cl
                  JOIN chunks c ON c.chunk_id = cl.chunk_id
                 WHERE cl.artifact = %(artifact)s
                   AND cl.claimed_at < now() - (%(cooldown_min)s * interval '1 minute')
                   AND (c.meta->>'no_index') IS DISTINCT FROM 'true'
                   {skip_clause}
                 ORDER BY cl.claimed_at
                 LIMIT %(limit)s
                   FOR UPDATE OF cl SKIP LOCKED
            ),
            reclaimed AS (
                UPDATE chunk_claims cl SET claimed_at = now()
                  FROM cand
                 WHERE cl.chunk_id = cand.chunk_id AND cl.artifact = %(artifact)s
                RETURNING cl.chunk_id
            )
            SELECT cand.chunk_id, cand.text
              FROM cand JOIN reclaimed USING (chunk_id)
        """
        params: dict[str, object] = {
            "artifact": self.model_name,
            "cooldown_min": _CLAIM_COOLDOWN_MIN,
            "limit": limit,
        }
        if skip_kinds:
            params["skip_kinds"] = skip_kinds
        rows = conn.execute(sql, params).fetchall()
        return [ChunkRow(chunk_id=int(r[0]), text=str(r[1])) for r in rows]

    def release_claims(self, conn: Connection, chunk_ids: list[int]) -> None:
        """Drop the ``chunk_claims`` rows for ``chunk_ids`` (this artifact).

        Called by the runner once a batch is written (ok or terminal-failed —
        base-handler failures don't retry) and on an :class:`EmbedderUnavailable`
        deferral, where releasing makes the chunks immediately re-claimable
        rather than waiting out the cooldown."""
        if not chunk_ids:
            return
        conn.execute(
            "DELETE FROM chunk_claims WHERE artifact = %s AND chunk_id = ANY(%s)",
            (self.model_name, list(chunk_ids)),
        )

    # ------------------------------------------------------------------
    # Per-row computation + write — subclass surface
    # ------------------------------------------------------------------

    @abstractmethod
    def process(self, row: ChunkRow) -> object:
        """Compute the artifact for ``row``. May raise on failure.

        Must be pure: no DB access, no filesystem writes. The
        runner calls :meth:`write_ok` (success) or
        :meth:`write_failed` (exception) after this returns.
        """

    def process_batch(self, rows: list[ChunkRow]) -> list[object]:
        """Compute artifacts for a whole claimed batch.

        Returns a list parallel to ``rows`` where each element is
        either the payload (success) or the exception object
        (failure). The default implementation calls :meth:`process`
        per row so a poison-pill chunk does not break the batch;
        subclasses that benefit from bulk compute (e.g. a transformer
        forward pass with batch_size > 1) override this.

        Type-wise the result list is ``list[object | BaseException]``
        but is declared ``list[object]`` to keep the abstract surface
        consistent with :meth:`process`; the runner pattern-matches
        on ``isinstance(payload, BaseException)`` to route.

        WARNING: this default turns *every* per-row exception into a
        terminal ``failed`` marker. A handler that can raise a
        **transient, defer-worthy** error — e.g. ``EmbedderUnavailable``
        (embedder down: retry later, don't burn the chunk) — MUST
        override this method to let that error propagate out of
        ``process_batch`` so the runner's phase-2 deferral releases the
        claims instead of marking the batch failed. ``EmbedHandler`` does
        exactly this; a future embedder-backed handler must too.
        """
        out: list[object] = []
        for row in rows:
            try:
                out.append(self.process(row))
            except Exception as exc:
                out.append(exc)
        return out

    @abstractmethod
    def write_ok(self, conn: Connection, chunk_id: int, payload: object) -> None:
        """Persist a successful result.

        ``payload`` is whatever :meth:`process` returned. Subclasses
        own the INSERT shape because the per-artifact columns differ
        (vector vs. text vs. token_count).
        """

    # ------------------------------------------------------------------
    # Failure marker — see ADR 0007
    # ------------------------------------------------------------------

    def write_failed(self, conn: Connection, chunk_id: int, error: str) -> None:
        """Insert a ``status='failed'`` marker row.

        The marker doubles as the de-claim signal: the chunk now
        has a row for this model, so the next pass's
        ``LEFT JOIN ... WHERE o.chunk_id IS NULL`` skips it.
        Manual retry is a one-line ``DELETE`` (see ADR 0007).
        """
        truncated = (error or "").strip()
        if len(truncated) > _LAST_ERROR_MAX_CHARS:
            truncated = truncated[: _LAST_ERROR_MAX_CHARS - 1] + "…"
        sql = f"""
            INSERT INTO {self.output_table}
                (chunk_id, {self.model_column}, status, last_error)
            VALUES (%s, %s, 'failed', %s)
            ON CONFLICT (chunk_id, {self.model_column}) DO UPDATE
               SET status = 'failed',
                   last_error = EXCLUDED.last_error,
                   attempts = {self.output_table}.attempts + 1
        """
        conn.execute(sql, (chunk_id, self.model_name, truncated))

    # ------------------------------------------------------------------
    # Status — for `precis worker --status` and ADR-0007 observability
    # ------------------------------------------------------------------

    def status(self, conn: Connection) -> ArtifactStatus:
        """Return ``(total, ok, failed, pending)`` for this handler.

        ``total`` is the chunks count; ``ok`` / ``failed`` aggregate
        rows in the output table for this model; ``pending = total
        - ok - failed`` is what the worker still needs to claim.
        """
        sql = f"""
            SELECT
                (SELECT count(*) FROM chunks) AS total,
                count(o.chunk_id) FILTER (WHERE o.status = 'ok')     AS ok,
                count(o.chunk_id) FILTER (WHERE o.status = 'failed') AS failed
              FROM {self.output_table} o
             WHERE o.{self.model_column} = %s
        """
        row = conn.execute(sql, (self.model_name,)).fetchone()
        if row is None:  # pragma: no cover — count() always returns one row
            total = ok = failed = 0
        else:
            total = int(row[0] or 0)
            ok = int(row[1] or 0)
            failed = int(row[2] or 0)
        pending = max(0, total - ok - failed)
        return ArtifactStatus(
            name=self.name,
            total=total,
            ok=ok,
            failed=failed,
            pending=pending,
        )


__all__ = ["ArtifactStatus", "ChunkRow", "WorkerHandler"]
