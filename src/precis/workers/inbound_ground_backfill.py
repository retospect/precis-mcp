"""Claims-first grounding for the older, embedded paper corpus (gr693782).

The claim order is ascending priority of its most urgent live serving quest,
then ascending count of live evidence sources. Each visit ranks up to the
inbound top-K unchecked papers by their closest body chunk. An empty query
completes the claim. Completion is durable for this backfill campaign.

A pair's watermark commits with its evidence. A kill preserves completed
pairs and leaves an in-flight pair retryable. Already settled pairs use
the existing evidence (or rejection memo) without another model call. A
pair whose verifier keeps failing is retried on each run rather than
watermarked (a ``None`` verdict means nothing was written): the per-run
pair cap bounds that re-spend, and a transient dispatch failure is the
assumed cause.

Retraction checks ride along exactly as in the recent-paper pass: the
verdict seam queues the paper for ``taproot.hub.run_retraction_checks``
(``pending_checks``), drained after the pair's transaction commits — the
backfill reads the oldest papers, the likeliest to be retracted, so the
deferred trigger-1 check is deliberately not opted out of here. ``memory``
claims take the bare-link write path (``attach_evidence`` requires a
finding hub) and so carry no retraction trigger at all.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from psycopg import Connection

from precis.taproot.canon import (
    CLAIM_HUB_PREDICATE_PARAMS,
    NOT_HYPOTHESIS_PREDICATE_PARAMS,
    claim_sha,
)
from precis.taproot.grounding import has_grounding_prose
from precis.taproot.hub import HUB_ROLES, META_REJECTED, run_retraction_checks
from precis.taproot.reground import is_hearsay_section
from precis.workers._grounding_budget import GroundingBudget
from precis.workers.chase_trigger import (
    _refresh_claim_embeddings,
    claim_embedding_target_sql,
)
from precis.workers.inbound_ground import (
    _CHUNKS_PER_HUB,
    _NOT_HYPOTHESIS_SQL,
    _SKIP_CHUNK_KINDS,
    _WIDENABLE_SQL,
    VerifyFn,
    _env_int,
    _ground_pair,
    _HubMatch,
    _max_age_days,
    _paper_identity,
    _topk_default,
)

if TYPE_CHECKING:
    from precis.store.store import Store

_SOURCE = "inbound_ground_backfill"
_CLAIMS_SQL = claim_embedding_target_sql(include_memories=True)


def _next_claim(
    conn: Connection, *, model: str, visited: list[int]
) -> tuple[int, str, str] | None:
    row = conn.execute(
        f"""
        SELECT r.ref_id, r.title, ce.claim_sha
          FROM refs r
          JOIN claim_embeddings ce
            ON ce.hub_ref_id = r.ref_id AND ce.embedder = %(model)s
           AND ce.vector IS NOT NULL
         WHERE r.retired_at IS NULL
           AND NOT (r.ref_id = ANY(%(visited)s))
           AND {_CLAIMS_SQL}
           AND {_NOT_HYPOTHESIS_SQL}
           AND {_WIDENABLE_SQL}
           AND NOT EXISTS (
               SELECT 1 FROM inbound_ground_backfilled b WHERE b.claim_ref_id = r.ref_id
           )
           AND NOT EXISTS (
               SELECT 1 FROM links l JOIN refs a ON a.ref_id = l.src_ref_id
                WHERE l.dst_ref_id = r.ref_id AND l.relation = 'conjunct-of'
                  AND a.kind = 'finding' AND a.retired_at IS NULL
           )
         ORDER BY (
               SELECT min(COALESCE(q.prio, 5))
                 FROM refs q
                WHERE q.kind = 'quest' AND q.retired_at IS NULL
                  AND (
                      EXISTS (
                          SELECT 1 FROM links l
                           WHERE l.src_ref_id = r.ref_id AND l.dst_ref_id = q.ref_id
                             AND l.relation = 'serves'
                      )
                      OR EXISTS (
                          SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                           WHERE rt.ref_id = r.ref_id AND t.namespace = 'OPEN'
                             AND (rt.expires_at IS NULL OR rt.expires_at > now())
                             AND (
                                 t.value = 'quest:' || q.ref_id::text
                                 OR EXISTS (
                                     SELECT 1 FROM ref_identifiers ri
                                      WHERE ri.ref_id = q.ref_id
                                        AND ri.id_kind = 'cite_key'
                                        AND t.value = 'quest:' || ri.id_value
                                 )
                             )
                      )
                  )
           ) NULLS LAST, (
               SELECT count(DISTINCT l.src_ref_id)
                 FROM links l JOIN refs p ON p.ref_id = l.src_ref_id
                WHERE l.dst_ref_id = r.ref_id AND l.relation = ANY(%(roles)s)
                  AND p.retired_at IS NULL
           ), r.ref_id
         LIMIT 1
        """,
        {
            "model": model,
            "visited": visited,
            "roles": [*HUB_ROLES, "disputes"],
            **CLAIM_HUB_PREDICATE_PARAMS,
            **NOT_HYPOTHESIS_PREDICATE_PARAMS,
        },
    ).fetchone()
    if row is None:
        return None
    return int(row[0]), str(row[1]).strip(), str(row[2])


def _candidate_papers(
    conn: Connection, claim: int, *, model: str, max_age_days: int, limit: int
) -> list[int]:
    rows = conn.execute(
        """
        SELECT p.ref_id
          FROM claim_embeddings cl
          JOIN chunk_embeddings ce ON ce.embedder = cl.embedder
               AND ce.status = 'ok' AND ce.vector IS NOT NULL
          JOIN chunks c ON c.chunk_id = ce.chunk_id
          JOIN refs p ON p.ref_id = c.ref_id
         WHERE cl.hub_ref_id = %(claim)s AND cl.embedder = %(model)s
           AND p.kind = 'paper' AND p.retired_at IS NULL
           AND p.created_at < now() - make_interval(days => %(age)s)
           AND c.ord >= 0 AND c.retired_at IS NULL
           AND c.chunk_kind <> ALL(%(skip)s)
           AND (c.meta->>'no_index') IS DISTINCT FROM 'true'
           AND NOT EXISTS (
               SELECT 1 FROM inbound_grounded g
                WHERE g.claim_ref_id = cl.hub_ref_id AND g.paper_ref_id = p.ref_id
           )
         GROUP BY p.ref_id
         ORDER BY min(cl.vector <=> ce.vector), p.ref_id
         LIMIT %(limit)s
        """,
        {
            "claim": claim,
            "model": model,
            "age": max_age_days,
            "skip": list(_SKIP_CHUNK_KINDS),
            "limit": limit,
        },
    ).fetchall()
    return [int(row[0]) for row in rows]


def _best_passage(
    conn: Connection,
    claim: int,
    paper: int,
    *,
    model: str,
    title: str,
    meta: Mapping[str, Any],
) -> _HubMatch | None:
    rows = conn.execute(
        """
        SELECT c.chunk_id, c.ord, c.text,
               array_to_string(c.section_path, ' > ') AS section,
               cl.vector <=> ce.vector AS distance
          FROM chunks c JOIN chunk_embeddings ce USING (chunk_id)
          JOIN claim_embeddings cl ON cl.embedder = ce.embedder
         WHERE c.ref_id = %(paper)s AND cl.hub_ref_id = %(claim)s
           AND ce.embedder = %(model)s AND ce.status = 'ok' AND ce.vector IS NOT NULL
           AND c.ord >= 0 AND c.retired_at IS NULL
           AND c.chunk_kind <> ALL(%(skip)s)
           AND (c.meta->>'no_index') IS DISTINCT FROM 'true'
         ORDER BY distance, c.ord
         LIMIT %(limit)s
        """,
        {
            "paper": paper,
            "claim": claim,
            "model": model,
            "skip": list(_SKIP_CHUNK_KINDS),
            "limit": _CHUNKS_PER_HUB,
        },
    ).fetchall()
    for chunk, ord_, text, section, distance in rows:
        if text and has_grounding_prose(text) and not is_hearsay_section(section):
            return _HubMatch(
                hub_ref_id=claim,
                title=title,
                scope={str(k): str(v) for k, v in (meta.get("scope") or {}).items()},
                rejected={},
                chunk_id=int(chunk),
                chunk_ord=int(ord_),
                chunk_text=str(text),
                section_path=section,
                distance=float(distance),
            )
    return None


def _check_pair(
    conn: Connection,
    store: Store,
    claim: int,
    paper: int,
    *,
    model: str,
    title: str,
    verify: VerifyFn,
    pending_checks: list[int] | None = None,
) -> tuple[str, bool] | None:
    """Judge one (claim, paper) pair; return ``(verdict, newly_judged)``.

    ``newly_judged`` is ``True`` only when the verifier ran on this call —
    a pair settled from existing evidence or a rejection memo returns the
    settled verdict with ``False``, so callers can count fresh supports/
    and denies without re-attributing old ones. ``None`` leaves the pair
    retryable: the paper or claim died, the watermark already exists, or
    the verifier failed (nothing was written in any of these cases).

    ``pending_checks`` (pass a list) opts a finding-hub support into the
    deferred retraction check; the caller drains it with
    ``run_retraction_checks`` after the transaction commits.
    """
    live_paper = conn.execute(
        "SELECT 1 FROM refs WHERE ref_id = %s AND retired_at IS NULL FOR UPDATE",
        (paper,),
    ).fetchone()
    target = conn.execute(
        "SELECT kind, title, meta FROM refs "
        "WHERE ref_id = %s AND retired_at IS NULL FOR UPDATE",
        (claim,),
    ).fetchone()
    if live_paper is None or target is None or str(target[1]).strip() != title:
        return None
    if conn.execute(
        "SELECT 1 FROM inbound_grounded WHERE claim_ref_id = %s AND paper_ref_id = %s",
        (claim, paper),
    ).fetchone():
        return None

    kind, _, meta = target
    links = conn.execute(
        "SELECT relation FROM links WHERE src_ref_id = %s AND dst_ref_id = %s "
        "AND relation = ANY(%s)",
        (paper, claim, [*HUB_ROLES, "disputes"]),
    ).fetchall()
    evidence = {row[0] for row in links}
    memo = (meta.get(META_REJECTED) or {}).get(str(paper))
    if evidence:
        return ("deny" if evidence & {"disputes", "contradicts"} else "support"), False
    if memo is not None:
        return (
            "deny"
            if memo.get("verdict") == "deny" or memo.get("contradicts")
            else "no-relation"
        ), False
    match = _best_passage(conn, claim, paper, model=model, title=title, meta=meta)
    if match is None:
        return "no-relation", False
    result = _ground_pair(
        conn,
        store,
        paper,
        match,
        verify_fn=verify,
        claim_kind=kind,
        source=_SOURCE,
        identity=_paper_identity(conn, paper),
        pending_checks=pending_checks,
    )
    if result is None:
        return None
    verdict = result[0]
    return ("no-relation" if verdict == "neutral" else verdict), True


def run_inbound_ground_backfill_pass(
    store: Store,
    *,
    embedder: Any | None,
    limit: int | None = None,
    verify_fn: VerifyFn | None = None,
) -> dict[str, int]:
    """Run at most one top-K batch per claim and cap pair attempts per run."""
    result = dict(
        claims_visited=0,
        pairs_checked=0,
        supports=0,
        denies=0,
        tokens=0,
        claimed=0,
        ok=0,
        failed=0,
    )
    pair_cap = (
        limit
        if limit is not None
        else _env_int("PRECIS_INBOUND_GROUND_BACKFILL_PAIRS_PER_PASS", 20)
    )
    topk = _topk_default()
    budget = GroundingBudget(store, local=True) if verify_fn is None else None
    if (
        embedder is None
        or pair_cap <= 0
        or topk <= 0
        or (budget is not None and budget.exhausted)
    ):
        return result

    model = str(embedder.model)
    verify = budget.verify if budget is not None else verify_fn
    assert verify is not None
    max_age_days = _max_age_days()
    visited: list[int] = []
    with store.pool.connection() as conn:
        _refresh_claim_embeddings(
            conn, embedder, model, limit=64, include_memories=True
        )
        conn.commit()
        while (
            len(visited) < pair_cap
            and result["claimed"] < pair_cap
            and (budget is None or not budget.exhausted)
        ):
            claim = _next_claim(conn, model=model, visited=visited)
            if claim is None:
                break
            claim_id, title, embedded_sha = claim
            visited.append(claim_id)
            if not title or claim_sha(title) != embedded_sha:
                continue
            result["claims_visited"] += 1
            papers = _candidate_papers(
                conn,
                claim_id,
                model=model,
                max_age_days=max_age_days,
                limit=min(topk, pair_cap - result["claimed"]),
            )
            if not papers:
                conn.execute(
                    "INSERT INTO inbound_ground_backfilled (claim_ref_id) VALUES (%s) "
                    "ON CONFLICT DO NOTHING",
                    (claim_id,),
                )
                conn.commit()
                continue

            conn.commit()
            for paper in papers:
                pending_checks: list[int] = []
                with conn.transaction():
                    checked = _check_pair(
                        conn,
                        store,
                        claim_id,
                        paper,
                        model=model,
                        title=title,
                        verify=verify,
                        pending_checks=pending_checks,
                    )
                    if checked is not None:
                        verdict, new = checked
                        conn.execute(
                            "INSERT INTO inbound_grounded VALUES (%s, %s, %s)",
                            (claim_id, paper, verdict),
                        )
                # Trigger-1 retraction checks queue during the write and
                # drain only once the pair's watermark is committed —
                # never inside the held transaction.
                if pending_checks:
                    run_retraction_checks(store, pending_checks)
                result["claimed"] += 1
                if checked is None:
                    result["failed"] += 1
                    if budget is not None and budget.exhausted:
                        break
                    continue
                result["ok"] += 1
                result["pairs_checked"] += 1
                result["supports"] += int(new and verdict == "support")
                result["denies"] += int(new and verdict == "deny")
    result["tokens"] = budget.tokens if budget is not None else 0
    return result
