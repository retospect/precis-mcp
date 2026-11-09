"""Which memory layers the dev DB actually holds, and the local fill.

The ablation arms turn a layer off in the server. If the DB never had the
layer (no keywords, no summaries, no embeddings, no categorizer tags, no
edges), the arm's output is identical to all-on by construction and a null
effect would be an artefact, not a finding. :func:`coverage` measures each
layer over the benchmark's own refs so the runner can mark such an arm
``vacuous`` instead of reporting it silently (:func:`arm_vacuity`).

:func:`fill` runs the layers that have a free local path against the dev DB:
chunk embeddings (``EmbedHandler``, the embedder named by ``--embedder``) and
per-chunk keywords (``run_chunk_keywords_pass``: RAKE candidates scored by the
same embedder). It never calls an LLM, so llm summaries (``llm_summarize``),
categorizer tags (``classify`` / ``axis_pass`` / ``classify_topics``) and
machine-written edges stay empty; the coverage block says so.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

log = logging.getLogger(__name__)

#: A layer present on less than this share of its denominator is "~0": the
#: arm that removes it cannot move the numbers.
VACUOUS_BELOW = 0.01

#: Arm -> the coverage layer whose absence makes the arm a no-op, plus a short
#: reason. ``semantic-only`` has no entry: with no embeddings it returns
#: nothing (degenerate, not identical), see :func:`arm_vacuity`.
ARM_LAYER: Mapping[str, str] = {
    "no-kwd": "keywords",
    "no-summary": "summaries",
    "no-categorizer": "categorizer_tags",
    "no-fisheye": "edges",
    "no-recall": "embeddings",
    "lexical-only": "embeddings",
}

#: Layer -> the coverage key holding its share (0..1).
LAYER_FRACTION: Mapping[str, str] = {
    "keywords": "chunks_with_keywords_frac",
    "summaries": "chunks_with_llm_summary_frac",
    "embeddings": "chunks_with_embedding_frac",
    "categorizer_tags": "refs_with_categorizer_tag_frac",
    "edges": "refs_with_edge_frac",
}


def _frac(n: int, d: int) -> float:
    return round(n / d, 6) if d else 0.0


def categorizer_tag_pairs() -> tuple[list[str], str]:
    """``(closed namespaces, open-tag prefix)`` that belong to the categorizer
    layer: the axis yaml prefixes plus open ``topic:*`` tags (the same test as
    :func:`precis.categorizers.is_categorizer_tag`)."""
    from precis.categorizers import _categorizer_namespaces

    return sorted(_categorizer_namespaces()), "topic:"


def coverage(conn: Any, ref_ids: Sequence[int]) -> dict[str, Any]:
    """Per-layer coverage over the body of ``ref_ids`` (a psycopg connection).

    Chunk layers are shares of body chunks (``ord >= 0``); ref layers are
    shares of refs. ``embedder_counts`` names every embedder that stored a
    vector for these chunks, so a hash embedder cannot pass as a real one.
    """
    ids = list(ref_ids)
    out: dict[str, Any] = {"refs": len(ids)}
    if not ids:
        return out
    chunks = conn.execute(
        "SELECT count(*), "
        "count(*) FILTER (WHERE keywords IS NOT NULL AND cardinality(keywords) > 0) "
        "FROM chunks WHERE ref_id = ANY(%s) AND ord >= 0",
        (ids,),
    ).fetchone()
    n_chunks, n_kw = int(chunks[0]), int(chunks[1])
    n_sum = conn.execute(
        "SELECT count(DISTINCT s.chunk_id) FROM chunk_summaries s "
        "JOIN chunks c ON c.chunk_id = s.chunk_id "
        "WHERE c.ref_id = ANY(%s) AND c.ord >= 0 AND s.summarizer = 'llm-v1' "
        "AND s.text IS NOT NULL AND s.text <> ''",
        (ids,),
    ).fetchone()[0]
    emb_rows = conn.execute(
        "SELECT e.embedder, count(DISTINCT e.chunk_id) FROM chunk_embeddings e "
        "JOIN chunks c ON c.chunk_id = e.chunk_id "
        "WHERE c.ref_id = ANY(%s) AND c.ord >= 0 AND e.status = 'ok' "
        "GROUP BY e.embedder ORDER BY e.embedder",
        (ids,),
    ).fetchall()
    n_emb = conn.execute(
        "SELECT count(DISTINCT e.chunk_id) FROM chunk_embeddings e "
        "JOIN chunks c ON c.chunk_id = e.chunk_id "
        "WHERE c.ref_id = ANY(%s) AND c.ord >= 0 AND e.status = 'ok'",
        (ids,),
    ).fetchone()[0]
    spaces, open_prefix = categorizer_tag_pairs()
    n_cat = conn.execute(
        "SELECT count(DISTINCT rt.ref_id) FROM ref_tags rt "
        "JOIN tags t ON t.tag_id = rt.tag_id "
        "WHERE rt.ref_id = ANY(%s) AND (t.namespace = ANY(%s) "
        "OR (t.namespace = 'OPEN' AND lower(t.value) LIKE %s))",
        (ids, spaces, open_prefix + "%"),
    ).fetchone()[0]
    n_chunk_cat = conn.execute(
        "SELECT count(DISTINCT ct.chunk_id) FROM chunk_tags ct "
        "JOIN tags t ON t.tag_id = ct.tag_id "
        "JOIN chunks c ON c.chunk_id = ct.chunk_id "
        "WHERE c.ref_id = ANY(%s) AND c.ord >= 0 AND t.namespace = ANY(%s)",
        (ids, spaces),
    ).fetchone()[0]
    edges = conn.execute(
        "SELECT count(*), count(DISTINCT r) FROM ("
        "SELECT link_id, src_ref_id AS r FROM links WHERE src_ref_id = ANY(%s) "
        "UNION ALL SELECT link_id, dst_ref_id FROM links WHERE dst_ref_id = ANY(%s)"
        ") x",
        (ids, ids),
    ).fetchone()
    out.update(
        {
            "chunks": n_chunks,
            "chunks_with_keywords": n_kw,
            "chunks_with_keywords_frac": _frac(n_kw, n_chunks),
            "chunks_with_llm_summary": int(n_sum),
            "chunks_with_llm_summary_frac": _frac(int(n_sum), n_chunks),
            "chunks_with_embedding": int(n_emb),
            "chunks_with_embedding_frac": _frac(int(n_emb), n_chunks),
            "embedder_counts": {str(r[0]): int(r[1]) for r in emb_rows},
            "refs_with_categorizer_tag": int(n_cat),
            "refs_with_categorizer_tag_frac": _frac(int(n_cat), len(ids)),
            "chunks_with_categorizer_tag": int(n_chunk_cat),
            "mesh_edges": int(edges[0]),
            "refs_with_edge": int(edges[1]),
            "refs_with_edge_frac": _frac(int(edges[1]), len(ids)),
        }
    )
    return out


def hash_embedder(embedder_id: str | None) -> bool:
    """True for the deterministic hash/mock embedder (no semantics)."""
    return (embedder_id or "").lower() in ("mock", "hash", "")


def arm_vacuity(
    arm: str, cov: Mapping[str, Any], *, embedder_id: str | None
) -> dict[str, Any]:
    """``{vacuous, reason, embedder_note}`` for one arm from the coverage block.

    ``vacuous`` = the layer the arm removes is absent (share below
    :data:`VACUOUS_BELOW`), so the arm renders exactly what all-on does.
    ``semantic-only`` over a corpus with no embeddings returns nothing: it is
    flagged ``degenerate`` rather than vacuous. Every arm that ranks with the
    semantic leg also carries ``embedder: hash (not meaningful)`` when the
    stored vectors come from the hash embedder.
    """
    res: dict[str, Any] = {"vacuous": False, "reason": None}
    layer = ARM_LAYER.get(arm)
    if layer is not None:
        share = float(cov.get(LAYER_FRACTION[layer], 0.0))
        if share < VACUOUS_BELOW:
            res["vacuous"] = True
            res["reason"] = (
                f"layer '{layer}' coverage {share:.4f} < {VACUOUS_BELOW}: the DB "
                f"holds nothing of it to remove, so any {arm} delta comes from "
                "rungs/hint lines the caller still pays for, not from the layer "
                "(see refusals and identical_to_all_on_frac)"
            )
    elif arm == "semantic-only":
        share = float(cov.get(LAYER_FRACTION["embeddings"], 0.0))
        if share < VACUOUS_BELOW:
            res["degenerate"] = True
            res["reason"] = (
                f"no embeddings (coverage {share:.4f}): semantic-only has no leg to rank with"
            )
    if arm != "lexical-only" and hash_embedder(embedder_id):
        res["embedder_note"] = "embedder: hash (not meaningful)"
    return res


def fill(
    store: Any,
    embedder: Any,
    *,
    batch_size: int = 32,
    keywords: bool = True,
    max_passes: int = 100_000,
) -> dict[str, Any]:
    """Run the free local derived passes against ``store`` until drained.

    Embeddings first (``EmbedHandler``), then keywords (the keyword pass
    claims only chunks that already have an embedding). Returns
    ``{embed: {...}, keywords: {...}}`` with claimed/ok/failed totals. No LLM
    is touched: the layers that need one are reported by :func:`coverage`.
    """
    from precis.workers.embed import EmbedHandler
    from precis.workers.runner import run_handler_once

    embedder.warmup()
    handler = EmbedHandler(embedder)
    totals: dict[str, dict[str, int]] = {
        "embed": {"claimed": 0, "ok": 0, "failed": 0},
        "keywords": {"claimed": 0, "ok": 0, "failed": 0},
    }
    for _ in range(max_passes):
        res = run_handler_once(handler, store, batch_size=batch_size)
        if res.claimed == 0:
            break
        totals["embed"]["claimed"] += res.claimed
        totals["embed"]["ok"] += res.ok
        totals["embed"]["failed"] += res.failed
        if res.ok == 0:
            log.warning("bench fill: an embed batch wrote nothing; stopping")
            break
    if keywords:
        from precis.workers.chunk_keywords import run_chunk_keywords_pass

        for _ in range(max_passes):
            kres = run_chunk_keywords_pass(
                store, embedder, batch_size=500, max_seconds=900.0, single_runner=False
            )
            if kres["claimed"] == 0:
                break
            for k in ("claimed", "ok", "failed"):
                totals["keywords"][k] += kres[k]
            if kres["ok"] == 0:
                log.warning("bench fill: a keyword batch wrote nothing; stopping")
                break
    return totals


__all__ = [
    "ARM_LAYER",
    "LAYER_FRACTION",
    "VACUOUS_BELOW",
    "arm_vacuity",
    "coverage",
    "fill",
    "hash_embedder",
]
