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

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar

from psycopg import Connection

from precis.embedder import Embedder, EmbedderUnavailable, make_embedder
from precis.workers._claim import (
    bulk_reserve,
    claim_frontier,
    note_claim_frontier,
)
from precis.workers._embed_jobs import targeted_embed_job_sql
from precis.workers.base import ClaimedChunk, WorkerHandler

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Fresh-claim priority tiers — memory first, then finding/draft/conv, all
# ahead of the ~1M-chunk paper backlog
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
# sort over the whole candidate set — the shape reused here). gr563702 put
# ``memory`` at the head of the lanes and capped the interactive share per
# claim (:func:`precis.workers._claim.bulk_reserve`); ``RakeLemmaHandler``
# (the base class's other subclass) rides the same policy through the base
# class's ``priority_ref_kinds`` lane.

#: Ref kinds whose chunks jump the embed queue. ``memory`` leads
#: (gr563702 — Reto's scheduling direction): a fresh or edited memory is
#: what an agent or person needs to recall next, so its body and header
#: chunks claim ahead of every other lane — and the memory write paths
#: also nudge a job (:func:`nudge_fresh_embed`) so the lane is drained
#: within seconds rather than on the materializer cadence (gr563669:
#: prod lag measured 15–32 min behind the paper backlog). ``finding``
#: follows: a hub's ``finding_body`` is what the mint-time dedup gate
#: ANN-searches, so an unembedded hub is invisible to the next mint
#: (gr489243). The interactive lanes are capped per claim
#: (:func:`precis.workers._claim.bulk_reserve`) — the bulk backlog always
#: keeps a floor of each micro-batch, so interactive work reorders the
#: queue but can never starve it.
_PRIORITY_KINDS = ("memory", "finding", "draft", "conv")

#: The corpus-wide predicate shared by the three backlog tiers: every ref
#: kind that has no priority lane. Derived from ``_PRIORITY_KINDS`` so the
#: two can never drift apart.
_CORPUS_PRED = (
    "(r.kind <> ALL(ARRAY[" + ", ".join(f"'{k}'" for k in _PRIORITY_KINDS) + "]))"
)

#: Fresh-claim tiers, in queue order: memory > finding > draft > conv,
#: then the corpus backlog in value order — paragraph >
#: table/figure/equation > everything else (gr513749; Reto 2026-10-24:
#: body prose is what semantic search returns, so it drains first). Each
#: is ``(kind_pred, order_by)`` spliced into ``_TIERED_FRESH_CLAIM_SQL``.
#: The priority tiers keep ``c.ref_id, c.ord`` contiguity (small
#: populations); each corpus tier keeps the base class's flat
#: ``c.chunk_id`` FIFO (oldest-first — chunk_id order is insertion order)
#: under its own frontier floor. Separate statements, not one ``ORDER BY
#: CASE``: a CASE rank can't carry a per-tier floor (the high-water is
#: not one ``chunk_id`` once ranks differ) and would sort the whole
#: unembedded set per 32-chunk micro-batch — the gr506753 claim-cost
#: regression this machinery exists to avoid.
_FRESH_TIERS: dict[str, tuple[str, str]] = {
    "memory": ("r.kind = 'memory'", "c.ref_id, c.ord"),
    "finding": ("r.kind = 'finding'", "c.ref_id, c.ord"),
    "draft": ("r.kind = 'draft'", "c.ref_id, c.ord"),
    "conv": ("r.kind = 'conv'", "c.ref_id, c.ord"),
    "paragraph": (
        _CORPUS_PRED + " AND c.chunk_kind = 'paragraph'",
        "c.chunk_id",
    ),
    "structured": (
        _CORPUS_PRED + " AND c.chunk_kind = ANY(ARRAY['table', 'figure', 'equation'])",
        "c.chunk_id",
    ),
    "other": (
        _CORPUS_PRED
        + " AND c.chunk_kind <> ALL(ARRAY['paragraph', 'table', 'figure', 'equation'])",
        "c.chunk_id",
    ),
}

#: Tiers carrying a frontier floor — the corpus backlog tiers, keyed by
#: their own name under ``embed:<model>`` in
#: :func:`precis.workers._claim.claim_frontier`.
_FLOORED_TIERS = frozenset({"paragraph", "structured", "other"})

#: Tiers that are NOT floored — the interactive lanes (memory/finding/
#: draft/conv). Their combined claim in one batch is capped at
#: ``limit - bulk_reserve(limit)`` (gr563702): the fairness bound that
#: keeps bulk embedding progressing while an interactive burst drains.
_INTERACTIVE_TIERS = frozenset(_FRESH_TIERS) - _FLOORED_TIERS

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
    # ``field`` — a cad sampled-SDF grid's carrier chunk
    # (``store/_cad_ops.py::put_field``): structured geometry, never
    # embedded, same treatment as ``cad_nodes``.
    # ``paper_note`` — a paper's append-only note timeline (gr521094):
    # notes are agent annotations, never body text; the ord<0 band
    # keeps them out of body predicates and this keeps them out of
    # the vector index (meta.no_index backs it on the chunk too).
    # ``scan_geometry`` — a coverage scan point's extxyz carrier chunk
    # (precis/store/scan_geometry.py, gr540872): a one-line summary
    # over a chunk_blobs payload, never embedded (meta.no_index too).
    skip_chunk_kinds: ClassVar[tuple[str, ...]] = (
        "references",
        "field",
        "paper_note",
        "scan_geometry",
    )

    def __init__(self, embedder: Embedder) -> None:
        self._embedder = embedder
        # The corpus tiers' ``ORDER BY c.chunk_id`` scans ride the shared
        # per-process frontier floor (gr573350,
        # :func:`precis.workers._claim.claim_frontier`) keyed by this
        # model — the successor of the ``_rest_after`` per-instance
        # resume cursors (gr506753: ~4M row probes, ~9s per claim on a
        # 4.5M-chunk corpus — claim SQL, not the embedder, was the drain's
        # wall-clock bound). Module state, nothing to init here.
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
        queue-priority order: memory > finding > draft > conv > paragraph >
        table/figure/equation > other (see :data:`_FRESH_TIERS`).

        Overrides :meth:`WorkerHandler._claim_fresh` — everything else
        (:meth:`WorkerHandler.claim_batch`'s reclaim top-up, ``release_claims``,
        ``status``) is inherited unchanged; only the fresh-claim ordering
        differs from the base class's flat ``ORDER BY c.chunk_id``. Each tier
        runs its own claim statement (rather than one query with an ``ORDER
        BY CASE``) so an empty tier costs a cheap indexed probe, not
        a sort over the whole candidate set — the tiers partition, so
        corpus-wide throughput is unchanged when no higher-priority chunk is
        pending. A chunk claimed by an earlier tier is excluded from a later
        one by the fresh ``NOT EXISTS chunk_claims`` (its claim row is
        written in the same statement).

        The interactive tiers (:data:`_INTERACTIVE_TIERS`) are capped
        collectively at ``limit - bulk_reserve(limit)`` (gr563702): an
        interactive burst can reorder the queue but never consume a whole
        batch — every claim leaves the bulk backlog its fairness floor.

        Each corpus tier (``_FLOORED_TIERS``) rides the shared per-process
        frontier floor (gr573350,
        :func:`precis.workers._claim.claim_frontier`) keyed by this model:
        the claim starts at the lowest chunk_id the previous non-empty
        claim returned instead of re-walking the embedded prefix. An
        empty claim raises the floor to ``max(chunk_id)`` — only the TTL
        clears it, so a chunk re-pended below the floor (an edit bumps
        ``content_sha`` in place, resurfacing at the ORIGINAL id) waits
        at most one TTL; a drained tier parks at the high-water and
        costs one full scan per TTL, not one per claim.
        """
        skip_clause, skip_kinds = self._skip_clause("c")
        pass_name = f"embed:{self.model_name}"
        claimed: list[ClaimedChunk] = []
        interactive_cap = limit - bulk_reserve(limit)
        for tier, (kind_pred, order_by) in _FRESH_TIERS.items():
            if tier in _INTERACTIVE_TIERS:
                # Interactive lanes share one cap; every claim so far is
                # interactive (they precede the floored tiers), so this is
                # also the collective ``interactive_cap - claimed`` bound.
                remaining = interactive_cap - len(claimed)
            else:
                remaining = limit - len(claimed)
            if remaining <= 0:
                continue
            frontier = None
            if tier in _FLOORED_TIERS:
                frontier = claim_frontier(pass_name, tier)
                if frontier is not None:
                    kind_pred += " AND c.chunk_id >= %(frontier)s"
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
            if frontier is not None:
                params["frontier"] = frontier
            rows = conn.execute(sql, params).fetchall()
            if tier in _FLOORED_TIERS:
                high_water = None
                if not rows:
                    row = conn.execute("SELECT max(chunk_id) FROM chunks").fetchone()
                    high_water = int(row[0]) if row and row[0] is not None else None
                note_claim_frontier(
                    pass_name,
                    tier,
                    floor=frontier,
                    min_key=min((int(r[0]) for r in rows), default=None),
                    high_water=high_water,
                )
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


def unembedded_chunk_count(conn: Connection, *, ref_id: int | None = None) -> int:
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
    can never disagree about what "backlog" means (§F cycle a). Both read
    the **unscoped** (``ref_id=None``) predicate through
    :func:`cached_unembedded_backlog`, an hourly-parked copy (gr573440) —
    that path's predicate and result are byte-identical to before
    ``ref_id=`` existed; never change it.

    ``ref_id=`` (read-for-question loop, slice 4 / docs/backlog/
    embed-status-hint-three-state.md) scopes the count to one ref's body
    chunks (``ord >= 0``) for the toc readiness line's poll. It is
    deliberately a **different predicate**, not just an added ``WHERE
    c.ref_id = %s``: the unscoped form's ``NOT EXISTS (... OR o.status =
    'failed' ...)`` treats a permanently-failed embedding as "don't
    retry" — satisfied, so it does NOT count toward the backlog (the
    correct behaviour for a high-water threshold, which must not spin
    forever on a row that will never succeed). The scoped form drops that
    escape: a chunk whose only row is ``status = 'failed'`` counts as NOT
    embedded here, because a caller polling "is my edit embedded yet"
    needs to see the failure, not read it as done. See
    :func:`failed_embedding_count` for the "how many of those are
    specifically failed" breakdown the readiness line's ``· failed N``
    suffix needs.
    """
    model_name = _default_embedder_name(conn)
    if model_name is None:
        return 0
    where, params = _unembedded_where(model_name, ref_id=ref_id)
    count_row = conn.execute(
        f"SELECT count(*) FROM chunks c WHERE {where}", params
    ).fetchone()
    return int(count_row[0]) if count_row else 0


def _default_embedder_name(conn: Connection) -> str | None:
    """The corpus's default embedder (``embedders.is_default = TRUE``) —
    the anchor every unscoped backlog probe shares, resolved without a
    live ``Embedder`` (see :func:`unembedded_chunk_count`)."""
    row = conn.execute(
        "SELECT name FROM embedders WHERE is_default = TRUE ORDER BY name LIMIT 1"
    ).fetchone()
    return str(row[0]) if row else None


def _unembedded_where(model_name: str, *, ref_id: int | None) -> tuple[str, list[Any]]:
    """The ``chunks c`` WHERE fragment + params every unembedded-backlog
    probe shares — :func:`unembedded_chunk_count` (``count(*)``) and
    :func:`oldest_unembedded_chunk_at` (``min(created_at)``) project over
    the same eligibility set, so the predicate lives here exactly once.

    Scoped (``ref_id``) vs unscoped diverge the same way
    :func:`unembedded_chunk_count`'s docstring describes: a
    permanently-``failed`` row satisfies the unscoped probe (don't retry —
    must not hold the backlog open) but NOT the scoped one (a caller
    polling "is my edit embedded yet" must see the failure).
    """
    skip_kinds = EmbedHandler.skip_chunk_kinds
    skip_clause = ""
    params: list[Any] = [model_name]
    if skip_kinds:
        skip_clause = "AND c.chunk_kind <> ALL(%s)"
        params.append(list(skip_kinds))
    if ref_id is None:
        not_exists_pred = (
            "(o.status = 'failed' OR o.content_sha IS NOT DISTINCT FROM c.content_sha)"
        )
        scope_clause = ""
    else:
        not_exists_pred = (
            "o.status = 'ok' AND o.content_sha IS NOT DISTINCT FROM c.content_sha"
        )
        scope_clause = "AND c.ref_id = %s AND c.ord >= 0"
        params.append(ref_id)
    return (
        f"""
        NOT EXISTS (
                SELECT 1 FROM {EmbedHandler.output_table} o
                 WHERE o.chunk_id = c.chunk_id
                   AND o.{EmbedHandler.model_column} = %s
                   AND {not_exists_pred}
            )
        AND (c.meta->>'no_index') IS DISTINCT FROM 'true'
        {skip_clause}
        {scope_clause}
        """,
        params,
    )


def oldest_unembedded_chunk_at(conn: Connection) -> datetime | None:
    """``min(created_at)`` over :func:`unembedded_chunk_count`'s UNSCOPED
    backlog — when the oldest eligible chunk started waiting.

    Feeds the materializer's sub-high-water age trigger
    (``materialize._materialize_age_trigger``, gr483036): the 500
    high-water alone let a trickle backlog sit ~4.5h between bulk mints —
    every gap tripped the embed watchdog's 2h budget and fresh
    memory/gripe/message chunks waited hours for a vector. ``None`` on an
    empty backlog (or no default embedder).

    Cost: no index covers the eligibility predicate, so this is one
    count-shape scan — the same plan the per-tick backlog count already
    pays — and it's only run while ``0 < backlog <= HIGH`` with nothing
    queued/running.
    """
    model_name = _default_embedder_name(conn)
    if model_name is None:
        return None
    where, params = _unembedded_where(model_name, ref_id=None)
    row = conn.execute(
        f"SELECT min(c.created_at) FROM chunks c WHERE {where}", params
    ).fetchone()
    return row[0] if row else None


@dataclass(frozen=True, slots=True)
class EmbedBacklog:
    """The unscoped embed backlog as parked by
    :func:`cached_unembedded_backlog`: :func:`unembedded_chunk_count`,
    :func:`oldest_unembedded_chunk_at` and when they were counted."""

    count: int
    oldest: datetime | None
    counted_at: datetime


def cached_unembedded_backlog(pool: Any) -> EmbedBacklog:
    """:func:`unembedded_chunk_count` and :func:`oldest_unembedded_chunk_at`
    (both unscoped) from one ``chunks`` scan, recounted at most hourly
    through :mod:`precis.backlog_cache` (gr573440) on its own connection
    from ``pool``. The materializer's mint gate and age trigger and
    ``embed_batch``'s ``queue_remaining`` read this instead of scanning on
    every tick and every job."""
    from precis import backlog_cache

    def _compute(conn: Connection) -> dict[str, Any]:
        model_name = _default_embedder_name(conn)
        if model_name is None:
            return {"count": 0, "oldest": None}
        where, params = _unembedded_where(model_name, ref_id=None)
        row = conn.execute(
            f"SELECT count(*), min(c.created_at) FROM chunks c WHERE {where}",
            params,
        ).fetchone()
        oldest = row[1] if row else None
        return {
            "count": int(row[0]) if row else 0,
            "oldest": oldest.isoformat() if oldest is not None else None,
        }

    hit = backlog_cache.cached_count(pool, "embed:unembedded", _compute)
    oldest_s = hit.value.get("oldest")
    return EmbedBacklog(
        count=int(hit.value["count"]),
        oldest=datetime.fromisoformat(oldest_s) if oldest_s else None,
        counted_at=hit.counted_at,
    )


def eligible_chunk_count(conn: Connection, *, ref_id: int) -> int:
    """Body chunks (``ord >= 0``) of ``ref_id`` eligible for embedding — the
    denominator the toc readiness line pairs with
    :func:`unembedded_chunk_count`'s scoped count (``embedded N/<eligible>``).
    Same predicate as that function's scoped ``skip_clause``: not a
    :attr:`EmbedHandler.skip_chunk_kinds` kind, not ``meta.no_index``. No
    length window — unlike the summarizer, the embedder has none, so a
    ``table``/``equation`` chunk kind is the only thing this excludes that
    :meth:`~precis.store._chunks_ops.ChunkStore.count_chunks` (the naive
    "every body chunk" total the toc readiness line used to divide by)
    would have counted as done.
    """
    skip_kinds = EmbedHandler.skip_chunk_kinds
    skip_clause = ""
    params: list[Any] = [ref_id]
    if skip_kinds:
        skip_clause = "AND c.chunk_kind <> ALL(%s)"
        params.append(list(skip_kinds))
    row = conn.execute(
        f"""
        SELECT count(*)
          FROM chunks c
         WHERE c.ref_id = %s
           AND c.ord >= 0
           AND (c.meta->>'no_index') IS DISTINCT FROM 'true'
           {skip_clause}
        """,
        params,
    ).fetchone()
    return int(row[0]) if row else 0


def failed_embedding_count(conn: Connection, *, ref_id: int) -> int:
    """Body chunks (``ord >= 0``) of ``ref_id`` whose CURRENT content's only
    default-embedder row is ``status = 'failed'`` — the readiness line's
    ``· failed N`` breakdown of :func:`unembedded_chunk_count`'s scoped
    total (every one of these is already counted there; this just names
    how many).

    ``content_sha IS NOT DISTINCT FROM c.content_sha`` matters: a chunk
    edited since its last (failed) embed attempt is "pending" again, not
    "failed" — the stale failed row belongs to content that no longer
    exists.
    """
    row = conn.execute(
        "SELECT name FROM embedders WHERE is_default = TRUE ORDER BY name LIMIT 1"
    ).fetchone()
    if row is None:
        return 0
    model_name = str(row[0])
    count_row = conn.execute(
        f"""
        SELECT count(*)
          FROM chunks c
          JOIN {EmbedHandler.output_table} o ON o.chunk_id = c.chunk_id
         WHERE o.{EmbedHandler.model_column} = %s
           AND o.status = 'failed'
           AND o.content_sha IS NOT DISTINCT FROM c.content_sha
           AND c.ref_id = %s
           AND c.ord >= 0
        """,
        (model_name, ref_id),
    ).fetchone()
    return int(count_row[0]) if count_row else 0


# ---------------------------------------------------------------------------
# Fresh-hub nudge — create embed work within seconds of a hub write
# ---------------------------------------------------------------------------
#
# gr489243 (embed-freshness B; Reto 2026-10-15: "worker picks"): the
# materializer mints ``embed_batch`` on a 300s cadence with a 30-minute age
# trigger, so a new hub's ``finding_body`` waited p50 34 min (max 2h09m, prod,
# 2026-10-08..15) for its vector, and until then the mint-time dedup gate
# cannot see it. Priority alone can't fix that (no job exists to reorder); the
# nudge creates one. job_inproc claims an ``embed_batch`` in p50 1.4 s / p90
# 4.6 s, and the ``finding`` tier in ``_FRESH_TIERS`` makes that job (or any
# already-running one, at its next micro-batch) take the hub first; memory
# writes share the mechanism through the ``memory`` tier (gr563669). The
# embed itself still runs in the worker: the write only queues a job and
# never calls the embedder.

#: ``params.limit`` of a nudge job: enough for a burst of hubs, small enough
#: to finish in seconds and free the embedder slot for the materializer.
NUDGE_LIMIT = 64

#: Job prio of a nudge (ASC, lower = more urgent): ahead of the materializer's
#: background mints (8), since a caller is about to search for this hub.
NUDGE_PRIO = 2

#: Burst-coalescing window: hub writes within one bucket share one job.
_NUDGE_BUCKET_S = 10


def nudge_fresh_embed(store: Store, *, conn: Connection, reason: str) -> int | None:
    """Queue a small high-priority ``embed_batch`` so a just-written
    interactive chunk (a hub's ``finding_body``, a memory's
    ``memory_body``/``card_combined``) is embedded within seconds. Returns
    the new job's ref id, or ``None`` when a bulk ``embed_batch`` is
    already queued (it claims the priority tiers first) or the nudge
    failed. A targeted memory job cannot claim the finding and therefore
    cannot coalesce this nudge.

    Runs on the caller's ``conn`` inside a savepoint, so the job commits with
    the hub. A failure is logged and swallowed: the materializer's age trigger
    still embeds the hub, only later. Never raises into the write.
    """
    from precis.workers._mint import mint_background_job

    idem_key = f"embed-nudge:{int(time.time()) // _NUDGE_BUCKET_S}"
    try:
        with conn.transaction():
            queued = conn.execute(
                f"""
                SELECT 1 FROM refs r
                 WHERE r.kind = 'job' AND r.retired_at IS NULL
                   AND r.meta->>'job_type' = 'embed_batch'
                   AND NOT {targeted_embed_job_sql()}
                   AND (r.meta->>'idem_key' = %s
                        OR EXISTS (
                             SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                              WHERE rt.ref_id = r.ref_id
                                AND t.namespace = 'STATUS'
                                AND t.value = 'queued'))
                 LIMIT 1
                """,
                (idem_key,),
            ).fetchone()
            if queued is not None:
                return None
            ref = mint_background_job(
                store,
                conn=conn,
                job_type="embed_batch",
                executor="job_inproc",
                title=f"embed_batch (fresh-hub nudge: {reason})",
                params={"limit": NUDGE_LIMIT},
                idem_key=idem_key,
                prio=NUDGE_PRIO,
            )
            return int(ref.id)
    except Exception:
        log.warning(
            "embed nudge failed (%s); the materializer embeds it later",
            reason,
            exc_info=True,
        )
        return None


__all__ = [
    "NUDGE_LIMIT",
    "NUDGE_PRIO",
    "EmbedBacklog",
    "EmbedHandler",
    "cached_unembedded_backlog",
    "eligible_chunk_count",
    "failed_embedding_count",
    "nudge_fresh_embed",
    "oldest_unembedded_chunk_at",
    "resolve_embedder",
    "unembedded_chunk_count",
]
