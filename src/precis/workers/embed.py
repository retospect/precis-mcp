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

from typing import Any, ClassVar

from psycopg import Connection

from precis.embedder import Embedder, EmbedderUnavailable, make_embedder
from precis.workers.base import ClaimedChunk, WorkerHandler

# ---------------------------------------------------------------------------
# Fresh-claim priority tiers — draft/conv jump the ~1M-chunk paper backlog
# ---------------------------------------------------------------------------
#
# See docs/backlog/embed-priority-lane-for-interactive-writes.md (gr262963):
# ``WorkerHandler._claim_fresh``'s flat ``ORDER BY c.chunk_id`` FIFO buries a
# chunk a human just edited (draft citation backfill, a live conv turn)
# behind the entire corpus backlog — its re-derived embedding becomes
# effectively unreachable. Mirrors the precedent already shipped twice:
# ``chunk_keywords.claim_chunks_needing_keywords`` (a single ``ORDER BY
# CASE``) and ``llm_summarize._FRESH_TIERS`` (separate per-tier queries, so
# an empty priority tier costs an index probe on ``refs_kind_idx``, not a
# sort over the whole candidate set — the shape reused here). Only
# ``EmbedHandler`` overrides ``_claim_fresh``; ``RakeLemmaHandler`` (the base
# class's other subclass) keeps the inherited flat order — lifting tiering
# into the shared base would change its claim order too, out of scope here.

#: Ref kinds whose chunks jump the embed queue.
_PRIORITY_KINDS = ("draft", "conv")

#: Fresh-claim tiers, in queue order: draft > conv > rest. Each is
#: ``(kind_pred, order_by)`` spliced into ``_TIERED_FRESH_CLAIM_SQL``. The
#: priority tiers keep ``c.ref_id, c.ord`` contiguity (small populations);
#: ``rest`` keeps the base class's flat ``c.chunk_id`` FIFO so corpus-wide
#: embed throughput is unchanged when no priority chunk is pending.
_FRESH_TIERS: dict[str, tuple[str, str]] = {
    "draft": ("r.kind = 'draft'", "c.ref_id, c.ord"),
    "conv": ("r.kind = 'conv'", "c.ref_id, c.ord"),
    "rest": ("(r.kind <> ALL(ARRAY['conv', 'draft']))", "c.chunk_id"),
}

#: Same predicate as ``WorkerHandler._claim_fresh`` (no current, non-failed
#: artifact row for this model; a stale ``content_sha`` re-derives an edited
#: draft chunk) plus a ``r.kind`` tier filter — JOINed against ``refs`` so
#: the planner can walk ``refs_kind_idx`` for the (tiny) priority tiers
#: instead of scanning ``chunks`` in ``chunk_id`` order. ``{output_table}`` /
#: ``{model_column}`` are ``EmbedHandler`` ClassVars, not caller input.
_TIERED_FRESH_CLAIM_SQL = """
    WITH cand AS (
        SELECT c.chunk_id, c.text
          FROM chunks c
          JOIN refs r ON r.ref_id = c.ref_id
         WHERE NOT EXISTS (
                   SELECT 1 FROM {output_table} o
                    WHERE o.chunk_id = c.chunk_id
                      AND o.{model_column} = %(artifact)s
                      AND (o.status = 'failed'
                           OR o.content_sha IS NOT DISTINCT FROM c.content_sha)
               )
           AND NOT EXISTS (
                   SELECT 1 FROM chunk_claims cl
                    WHERE cl.chunk_id = c.chunk_id AND cl.artifact = %(artifact)s
               )
           AND (c.meta->>'no_index') IS DISTINCT FROM 'true'
           AND {kind_pred}
           {skip_clause}
         ORDER BY {order_by}
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
        explicit label was set.

        ``name`` is a **log/label** accessor and MUST NOT raise. It is
        evaluated all over the runner's error-handling paths —
        ``run_handler_once``'s ``EmbedderUnavailable`` defer branch and
        ``run_loop``'s catch-all ``log.exception(..., handler.name)``.
        Resolving :attr:`model_name` does a live ``GET /model`` round-trip
        (cached after first success); when the embedder is *down* that
        round-trip raises ``EmbedderUnavailable``. If that propagated out
        of the runner's ``except`` block it would escape ``run_loop`` and
        **crash-loop the whole worker** — exactly the incident this guard
        prevents (a down embedder must degrade the embed pass, never take
        down summarize / dispatch / nursery with it). So fall back to a
        static label until the embedder is reachable; the real
        ``embed:<model>`` label reappears once ``model_name`` resolves.
        """
        if self._name_override is not None:
            return self._name_override
        try:
            return f"embed:{self.model_name}"
        except EmbedderUnavailable:
            return "embed:<embedder-unavailable>"

    @name.setter
    def name(self, value: str) -> None:
        # Writeable to satisfy the base ``name: str`` contract; the
        # label is normally derived from model_name, not set.
        self._name_override = value

    # ------------------------------------------------------------------
    # process — pure compute (delegate to embedder)
    # ------------------------------------------------------------------

    def process(self, row: ClaimedChunk) -> list[float]:
        """Return the dense vector for ``row.text``.

        ``Embedder.embed_one`` performs L2 normalization and any
        per-model truncation guards (see ``BgeM3Embedder._BGE_M3_MAX_CHARS``).
        Empty text is *not* a special case — the embedder will produce
        a (possibly degenerate) vector and the runner records it as
        ``status='ok'``. If the caller wants to skip empty chunks, do
        it upstream in ingest, not here.
        """
        return self._embedder.embed_one(row.text)

    def process_batch(self, rows: list[ClaimedChunk]) -> list[object]:
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
        except EmbedderUnavailable:
            # The service is transiently down/busy — NOT a per-row fault.
            # Propagate so the runner defers the whole batch (rows stay
            # unclaimed, no failure markers). Falling through to the
            # per-row path here would fire one single-text request per
            # chunk against an already-overloaded embedder — amplifying
            # the very 429 storm that triggered this, and stamping every
            # chunk ``failed`` for a blip that clears on its own.
            raise
        except Exception:
            # A genuine whole-batch fault (OOM, dim mismatch, one poison
            # row). Don't lose the whole batch: the per-row path runs each
            # chunk through embed_one and routes each failure to
            # write_failed via the runner.
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

        TOCTOU guard (gr196720): a chunk can be retyped into a skip-kind
        (e.g. ``bib_retag`` promoting a paragraph to ``'references'``,
        which deletes its embedding) *between* this handler's claim and
        this write — the claim query filtered on ``chunk_kind`` at claim
        time, not now. Re-checking here, inside the same INSERT (an
        ``INSERT ... SELECT ... WHERE`` instead of ``INSERT ... VALUES``)
        rather than a separate SELECT beforehand, keeps the check and the
        write atomic: no window where a concurrent retag lands between
        "checked OK" and "wrote anyway". A skip-kind chunk's row is
        simply not selected, so the INSERT (and any ``ON CONFLICT``
        update) affects zero rows and no embedding is (re)created for it.
        """
        if not isinstance(payload, list):  # pragma: no cover — defensive
            raise TypeError(
                f"EmbedHandler.write_ok expected list[float], got {type(payload).__name__}"
            )
        conn.execute(
            """
            INSERT INTO chunk_embeddings
                (chunk_id, embedder, vector, status, content_sha)
            SELECT c.chunk_id, %(embedder)s, %(vector)s, 'ok', c.content_sha
              FROM chunks c
             WHERE c.chunk_id = %(chunk_id)s
               AND c.chunk_kind <> ALL(%(skip_kinds)s)
            ON CONFLICT (chunk_id, embedder) DO UPDATE
               SET vector = EXCLUDED.vector,
                   status = 'ok',
                   last_error = NULL,
                   content_sha = EXCLUDED.content_sha,
                   attempts = chunk_embeddings.attempts + 1
            """,
            {
                "chunk_id": chunk_id,
                "embedder": self.model_name,
                "vector": payload,
                "skip_kinds": list(self.skip_chunk_kinds),
            },
        )

    # ------------------------------------------------------------------
    # _claim_fresh — priority-tiered override of the base class query
    # ------------------------------------------------------------------

    def _claim_fresh(self, conn: Connection, *, limit: int) -> list[ClaimedChunk]:
        """Claim fresh (never-embedded or stale-``content_sha``) chunks in
        queue-priority order: draft > conv > rest (see :data:`_FRESH_TIERS`).

        Overrides :meth:`WorkerHandler._claim_fresh` — everything else
        (:meth:`WorkerHandler.claim_batch`'s reclaim top-up, ``release_claims``,
        ``status``) is inherited unchanged; only the fresh-claim ordering
        differs from the base class's flat ``ORDER BY c.chunk_id``. Each tier
        runs its own claim statement (rather than one query with an ``ORDER
        BY CASE``) so an empty priority tier costs a cheap indexed probe, not
        a sort over the whole candidate set — the tiers partition, so
        corpus-wide throughput is unchanged when no draft/conv chunk is
        pending. A chunk claimed by an earlier tier is excluded from a later
        one by the fresh ``NOT EXISTS chunk_claims`` (its claim row is
        written in the same statement).
        """
        skip_clause, skip_kinds = self._skip_clause("c")
        claimed: list[ClaimedChunk] = []
        for tier in ("draft", "conv", "rest"):
            remaining = limit - len(claimed)
            if remaining <= 0:
                break
            kind_pred, order_by = _FRESH_TIERS[tier]
            sql = _TIERED_FRESH_CLAIM_SQL.format(
                output_table=self.output_table,
                model_column=self.model_column,
                kind_pred=kind_pred,
                order_by=order_by,
                skip_clause=skip_clause,
            )
            params: dict[str, object] = {
                "artifact": self.model_name,
                "limit": remaining,
            }
            if skip_kinds:
                params["skip_kinds"] = skip_kinds
            rows = conn.execute(sql, params).fetchall()
            claimed += [ClaimedChunk(chunk_id=int(r[0]), text=str(r[1])) for r in rows]
        return claimed


def resolve_embedder(
    *,
    name: str | None = None,
    dim: int = 1024,
    url: str | None = None,
    timeout: float | None = None,
    max_retries: int | None = None,
) -> Embedder:
    """Build the configured :class:`~precis.embedder.Embedder` from
    explicit args, falling back to ``PrecisConfig`` (Tier 1 —
    ``load_config()``, not raw env: ``embedder``/``embedder_url``/
    ``embedder_timeout``/``embedder_max_retries`` are all ``PrecisConfig``
    fields). Extracted here (§F cycle a) so a caller with no
    ``argparse.Namespace`` — ``embed_batch``'s job dispatch — doesn't have
    to re-derive them; ``cli/worker.py``'s ``_resolve_embedder`` reads the
    SAME fields via its argparse ``--embedder*`` flags (env-defaulted at
    parse time in the bootstrap zone). Raises ``ValueError`` for an
    unknown embedder name or a missing ``remote`` URL (see
    :func:`~precis.embedder.make_embedder`).
    """
    from precis.config import load_config

    cfg = load_config()
    return make_embedder(
        name or cfg.embedder,
        dim=dim,
        url=url if url is not None else cfg.embedder_url,
        timeout=timeout if timeout is not None else cfg.embedder_timeout,
        max_retries=(
            max_retries if max_retries is not None else cfg.embedder_max_retries
        ),
    )


def unembedded_chunk_count(conn: Connection) -> int:
    """Chunks still needing the corpus's default embedder's vector — the
    SAME predicate :class:`EmbedHandler`'s derived-queue claim
    (``WorkerHandler._claim_fresh``) uses: no current, non-stale
    ``chunk_embeddings`` row for that model, excluding
    ``chunk_kind='references'`` and ``meta.no_index`` chunks.

    Read-only — no ``chunk_claims`` lease is taken or consulted, so this is
    a backlog *count* (including chunks a live worker already has leased),
    not "claimable right now".

    The target model is resolved via ``embedders.is_default = TRUE`` (the
    same anchor :meth:`~precis.store.Store.embedding_dim` uses) rather
    than constructing a live ``Embedder`` — resolving it that way would
    cost a network round-trip (``RemoteEmbedder.model``) and could itself
    raise when the embedder is down, which a cheap periodic backlog count
    must never depend on.

    Shared by the ``materialize`` cadence (backlog high-water threshold)
    and ``embed_batch`` (the "queue_remaining" summary figure) so the two
    can never disagree about what "backlog" means (§F cycle a).
    """
    row = conn.execute(
        "SELECT name FROM embedders WHERE is_default = TRUE ORDER BY name LIMIT 1"
    ).fetchone()
    if row is None:
        return 0
    model_name = str(row[0])
    skip_kinds = EmbedHandler.skip_chunk_kinds
    skip_clause = ""
    params: list[Any] = [model_name]
    if skip_kinds:
        skip_clause = "AND c.chunk_kind <> ALL(%s)"
        params.append(list(skip_kinds))
    count_row = conn.execute(
        f"""
        SELECT count(*)
          FROM chunks c
         WHERE NOT EXISTS (
                 SELECT 1 FROM {EmbedHandler.output_table} o
                  WHERE o.chunk_id = c.chunk_id
                    AND o.{EmbedHandler.model_column} = %s
                    AND (o.status = 'failed'
                         OR o.content_sha IS NOT DISTINCT FROM c.content_sha)
               )
           AND (c.meta->>'no_index') IS DISTINCT FROM 'true'
           {skip_clause}
        """,
        params,
    ).fetchone()
    return int(count_row[0]) if count_row else 0


__all__ = ["EmbedHandler", "resolve_embedder", "unembedded_chunk_count"]
