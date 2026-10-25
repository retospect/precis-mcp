"""``gripe_parents`` — pipeline v2 slice 2: potential-duplicate-parent pass
(``docs/backlog/gripe-pipeline-v2.md`` §Intake).

A scheduler :class:`~precis.workers.scheduler.Cadence` (300 s,
fleet-singleton via the lease claim, mirroring ``materialize``) that
fills ``refs.meta.dup_parents`` for gripes whose body embedding has
landed but which carry no ``dup_parents`` key yet. **Key presence is
the marker**: a gripe with no hits gets ``[]`` so the pass never
re-scans it. No model call, no embedder call — one indexed ANN query
per gripe, so filing stays free (Reto 2026-10-25: no model at intake).

Query shape (per spec): the ANN returns chunks, not gripes, and gripes
carry comment chunks too, so ``chunk_kinds=['gripe_body']``, oversample
``3k``, dedupe per ref keeping the best chunk, cut to ``k``. Candidates
are pre-filtered to non-terminal gripes — ``exclude_tags`` on the
terminal pair inside the ANN (a post-filter under-fills), after
:func:`~precis.store._chunks_ops._prepare_filtered_ann` inside
``search_chunks_semantic`` routes ``gripe`` to its partial index.
Exclude self and refs already ``supersedes``-linked to the filing.

Lease (the 0045 lesson, 30766194a — never hold a transaction across
slow work): the claim is one short transaction stamping
``meta.pipeline.claimed_at`` via an atomic conditional ``jsonb_set``
UPDATE (a racing claimant's WHERE re-check loses); the ANN fan-out runs
with no open transaction; a second short transaction per gripe writes
``dup_parents`` and clears ``claimed_at``. A crashed worker's lease
ages out after :data:`_CLAIM_MINUTES` and is re-claimed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from psycopg.types.json import Jsonb

from precis.utils.env import env_float, env_int

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

_K_ENV = "PRECIS_DUP_PARENTS_K"
_FLOOR_ENV = "PRECIS_DUP_PARENTS_FLOOR"

_DEFAULT_K = 8
#: Cosine *similarity* floor (spec: 0.80, expect ~0.85 after
#: calibration). ``search_chunks_semantic`` takes a distance ceiling —
#: cosine distance = 1 − similarity, so 0.80 floors at ``max_distance``
#: 0.20.
_DEFAULT_FLOOR = 0.80

#: Minutes a ``meta.pipeline.claimed_at`` lease lives before another
#: claimant may take the gripe. Generous vs the one-ANN-query work so a
#: live worker never loses a gripe mid-pass; a dead worker's lease
#: re-enters on its own.
_CLAIM_MINUTES = 15

#: Chunk kind the pass reads and queries — the gripe's body, never its
#: comment timeline (``_BODY_KIND`` in ``handlers/gripe.py``).
_BODY_KIND = "gripe_body"

#: Candidate pre-filter: every non-terminal gripe stays (``open``,
#: ``triaged``, ``ready_for_fix``, ``in_review``, ``verify`` — Reto
#: approved non-terminal 2026-10-25); the terminal pair is excluded
#: inside the query. An untagged gripe counts as ``open`` and stays.
_EXCLUDE_TAGS = ("STATUS:done", "STATUS:wontfix")

#: The flood tag (slice 8 mints it) skips the parents pass per §Flood
#: control — the tick folds flood rows into one keeper instead.
_FLOOD_TAG = "flood"


def _k() -> int:
    return env_int(_K_ENV, _DEFAULT_K, lo=1)


def _floor() -> float:
    return env_float(_FLOOR_ENV, _DEFAULT_FLOOR)


@dataclass(slots=True)
class ParentsResult:
    """What one ``gripe_parents`` fire did — logged by the scheduler
    wrapper (``materialize``/``draft_refresh_scan`` convention: the
    cadence's ``ok`` only says the callable didn't raise)."""

    claimed: int = 0
    written: int = 0
    no_hits: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


def _claim_batch(store: Store, limit: int) -> list[int]:
    """Claim up to ``limit`` eligible gripes in ONE short transaction.

    Eligible: live ``kind='gripe'`` at a non-terminal ``STATUS:``, no
    ``meta.dup_parents`` key, at least one embedded ``gripe_body``
    chunk under the default embedder, not ``flood``-tagged, and no
    live ``pipeline.claimed_at`` lease. The conditional ``jsonb_set``
    UPDATE re-checks the lease at write time, so two workers racing
    the same tick can never both win a gripe.
    """
    eligible = """
            SELECT r.ref_id FROM refs r
             WHERE r.kind = 'gripe' AND r.retired_at IS NULL
               AND NOT (r.meta ? 'dup_parents')
               AND (r.meta->'pipeline'->>'claimed_at' IS NULL
                    OR (r.meta->'pipeline'->>'claimed_at')::timestamptz
                       < now() - %s * interval '1 minute')
               AND NOT EXISTS (
                     -- Processed set is non-terminal only (ruling:
                     -- done/wontfix gripes get no parents pass, same
                     -- boundary as the candidate set).
                     SELECT 1 FROM ref_tags rt
                       JOIN tags t ON t.tag_id = rt.tag_id
                      WHERE rt.ref_id = r.ref_id
                        AND t.namespace = 'STATUS'
                        AND t.value IN ('done', 'wontfix'))
               AND (r.created_at <= now() - interval '1 hour'
                    OR NOT EXISTS (
                     SELECT 1 FROM ref_tags rt
                       JOIN tags t ON t.tag_id = rt.tag_id
                      WHERE rt.ref_id = r.ref_id
                        AND t.value = %s
                        AND t.namespace IN ('OPEN', 'FLAG')))
               AND EXISTS (
                     -- The e.is_default join keeps eligibility in
                     -- lockstep with _gripe_vector: a gripe embedded
                     -- only under a non-default embedder would claim,
                     -- fetch NULL and churn forever otherwise.
                     SELECT 1 FROM chunks ch
                       JOIN chunk_embeddings ce
                         ON ce.chunk_id = ch.chunk_id
                        AND ce.status = 'ok'
                       JOIN embedders e
                         ON e.name = ce.embedder AND e.is_default
                      WHERE ch.ref_id = r.ref_id
                        AND ch.chunk_kind = %s
                        AND ch.retired_at IS NULL)
    """
    with store.tx() as conn:
        cand = conn.execute(
            eligible + "ORDER BY r.ref_id LIMIT %s",
            (_CLAIM_MINUTES, _FLOOD_TAG, _BODY_KIND, limit),
        ).fetchall()
        if not cand:
            return []
        # Conditional claim: the WHERE re-reads the row's latest
        # committed version under READ COMMITTED, so a racing claimant
        # whose lease committed first fails the lease check and loses.
        # jsonb_set creates only the LAST key in a path, so '{pipeline}'
        # is built by hand: coalesce the existing object (a gripe that
        # already carries triaged_at keeps it), merge claimed_at, then
        # set the pipeline key itself.
        rows = conn.execute(
            """
            UPDATE refs r
               SET meta = jsonb_set(
                            r.meta, '{pipeline}',
                            coalesce(r.meta->'pipeline', '{}'::jsonb)
                            || jsonb_build_object(
                                'claimed_at',
                                to_char(now() AT TIME ZONE 'UTC',
                                        'YYYY-MM-DD"T"HH24:MI:SS"Z"')))
             WHERE r.ref_id = ANY(%s)
               AND r.retired_at IS NULL
               AND NOT (r.meta ? 'dup_parents')
               AND (r.meta->'pipeline'->>'claimed_at' IS NULL
                    OR (r.meta->'pipeline'->>'claimed_at')::timestamptz
                       < now() - %s * interval '1 minute')
            RETURNING r.ref_id
            """,
            ([int(r[0]) for r in cand], _CLAIM_MINUTES),
        ).fetchall()
    return [int(r[0]) for r in rows]


def _gripe_vector(store: Store, ref_id: int) -> Any | None:
    """The gripe's own ``gripe_body`` embedding (lowest ord chunk —
    there is exactly one per spec'd filing) under the default
    embedder, or ``None`` when the embed worker hasn't reached it."""
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT ce.vector
              FROM chunks c
              JOIN chunk_embeddings ce
                ON ce.chunk_id = c.chunk_id AND ce.status = 'ok'
              JOIN embedders e ON e.name = ce.embedder AND e.is_default
             WHERE c.ref_id = %s AND c.chunk_kind = %s
               AND c.retired_at IS NULL
             ORDER BY c.ord
             LIMIT 1
            """,
            (ref_id, _BODY_KIND),
        ).fetchone()
    return row[0] if row else None


def _linked_ids(store: Store, ref_id: int) -> list[int]:
    """ref_ids already ``supersedes``-linked to this gripe (either
    direction) — excluded from its own candidate list."""
    out: set[int] = set()
    for link in store.links_for(ref_id, relation="supersedes"):
        other = link.dst_ref_id if link.src_ref_id == ref_id else link.src_ref_id
        out.add(int(other))
    return sorted(out)


def _status_map(store: Store, ref_ids: list[int]) -> dict[int, str]:
    """``{ref_id: STATUS value}`` for the kept candidates — ONE query
    for the batch (was one ``tags_for`` pool checkout per hit)."""
    if not ref_ids:
        return {}
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT rt.ref_id, t.value
              FROM ref_tags rt
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE rt.ref_id = ANY(%s) AND t.namespace = 'STATUS'
            """,
            (list(ref_ids),),
        ).fetchall()
    return {int(rid): str(val) for rid, val in rows}


def _dup_entries(store: Store, ref_id: int, vec: Any, k: int) -> list[dict[str, Any]]:
    """One ANN query → ``[{id, sim, status}]`` for ``ref_id``.

    ``3k`` rows back (oversample for the per-ref dedupe), best chunk
    per candidate ref, cut to ``k``. Floor, non-terminal pre-filter and
    the self/supersedes exclusions all apply inside
    ``search_chunks_semantic``.
    """
    # The pgvector ``Vector`` goes straight to ``_vec_param`` inside
    # search_chunks_semantic — no 1024-float list round trip.
    hits = store.chunks.search_chunks_semantic(
        query_vec=vec,
        kind="gripe",
        chunk_kinds=[_BODY_KIND],
        tags=None,
        exclude_tags=list(_EXCLUDE_TAGS),
        exclude_ref_ids=[ref_id, *_linked_ids(store, ref_id)],
        max_distance=1.0 - _floor(),
        limit=3 * k,
    )
    seen: set[int] = set()
    kept: list[tuple[int, float]] = []
    for _chunk, ref, dist in hits:  # distance-asc: first per ref is best
        if ref.id in seen:
            continue
        seen.add(ref.id)
        kept.append((ref.id, round(1.0 - float(dist), 4)))
        if len(kept) >= k:
            break
    statuses = _status_map(store, [rid for rid, _sim in kept])
    return [
        {"id": rid, "sim": sim, "status": statuses.get(rid, "open")}
        for rid, sim in kept
    ]


def _drop_lease_sql() -> str:
    """``meta`` minus the lease — and minus ``pipeline`` itself when
    claimed_at was its only key (don't leave ``"pipeline": {}``
    littering the ref). A NULL pipeline falls through to the
    path-delete branch, which is a no-op."""
    return (
        "CASE WHEN coalesce(meta->'pipeline', '{}'::jsonb) - 'claimed_at'"
        "          = '{}'::jsonb THEN meta - 'pipeline'"
        "      ELSE meta #- '{pipeline,claimed_at}' END"
    )


def _write_back(store: Store, ref_id: int, parents: list[dict[str, Any]]) -> None:
    """Second short transaction: ``meta.dup_parents`` in (``[]`` is the
    no-hits marker — key presence, not non-emptiness, means "done") and
    the ``pipeline.claimed_at`` lease out."""
    with store.tx() as conn:
        conn.execute(
            f"""
            UPDATE refs
               SET meta = ({_drop_lease_sql()})
                          || jsonb_build_object('dup_parents', %s::jsonb)
             WHERE ref_id = %s AND retired_at IS NULL
            """,
            (Jsonb(parents), ref_id),
        )


def _release_claim(store: Store, ref_id: int) -> None:
    """Drop ``pipeline.claimed_at`` without touching ``dup_parents``."""
    with store.tx() as conn:
        conn.execute(
            f"UPDATE refs SET meta = {_drop_lease_sql()} WHERE ref_id = %s",
            (ref_id,),
        )


def run_gripe_parents_pass(store: Store, batch_size: int) -> ParentsResult:
    """One ``gripe_parents`` fire: claim a batch of un-parented gripes,
    run one ANN query each with NO open transaction, write
    ``meta.dup_parents`` back. A per-gripe fault keeps its lease (it
    re-claims after :data:`_CLAIM_MINUTES`, the backoff clock) and
    doesn't stop the batch."""
    res = ParentsResult()
    claimed = _claim_batch(store, batch_size or 50)
    res.claimed = len(claimed)
    for ref_id in claimed:
        try:
            vec = _gripe_vector(store, ref_id)
            if vec is None:
                # Embedding vanished between claim and fetch (retired
                # chunk) — release the lease only. Without a vector the
                # gripe is ineligible again anyway; if one ever lands it
                # re-claims next fire.
                _release_claim(store, ref_id)
                continue
            parents = _dup_entries(store, ref_id, vec, _k())
            _write_back(store, ref_id, parents)
            res.written += 1
            if not parents:
                res.no_hits += 1
        except Exception as exc:  # keep the lease: cooldown is the reaper
            res.failed += 1
            res.errors.append(f"gr{ref_id}: {exc}")
            log.warning("gripe_parents: gr%d failed: %s", ref_id, exc)
    return res
