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
    # Claim — derived queue: chunks LEFT JOIN <output_table>
    # ------------------------------------------------------------------

    def claim_batch(self, conn: Connection, *, limit: int) -> list[ChunkRow]:
        """Lock and return up to ``limit`` chunks missing this artifact.

        ``FOR UPDATE OF c SKIP LOCKED`` lets concurrent workers
        coexist without double-processing. The lock is released when
        the caller commits (typically right after :meth:`write_ok`
        / :meth:`write_failed` for every claimed row).

        Empty list means "no work right now"; the caller should
        sleep and re-poll.
        """
        if limit <= 0:
            raise ValueError("limit must be positive")
        skip = self.skip_chunk_kinds
        skip_clause = ""
        params: tuple[object, ...] = (self.model_name, limit)
        if skip:
            # Use ``= ANY(%s)`` rather than ``IN (%s, …)`` to keep the
            # parameter shape uniform across handlers with different
            # skip-list lengths — psycopg adapts a Python tuple/list
            # to a SQL array transparently.
            skip_clause = "AND c.chunk_kind <> ALL(%s)"
            params = (self.model_name, list(skip), limit)
        sql = f"""
            SELECT c.chunk_id, c.text
              FROM chunks c
              LEFT JOIN {self.output_table} o
                ON o.chunk_id = c.chunk_id
               AND o.{self.model_column} = %s
             WHERE o.chunk_id IS NULL
               {skip_clause}
             ORDER BY c.chunk_id
             LIMIT %s
               FOR UPDATE OF c SKIP LOCKED
        """
        rows = conn.execute(sql, params).fetchall()
        return [ChunkRow(chunk_id=int(r[0]), text=str(r[1])) for r in rows]

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
