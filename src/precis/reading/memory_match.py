"""Concept → nearest research memory edge, precomputed into ``meta.memory_match``.

``/learning`` and ``/anki`` show each concept group one link to the research
memory that explains it (gr743229). The match is computed here, at card-forge
mint/rework time and by the same sweep as a backfill, and the pages only read
``meta.memory_match`` — no search at page load.

Candidates, nearest by cosine distance between the concept's ``card_combined``
embedding and a chunk embedding: refs of kind ``memory`` tagged
``SPACE:research``; failing that, kind ``finding``. A ``SPACE:repo-dev`` memory
is never a candidate. A nearest hit farther than the bar is stored as a miss
(``{"ref_id": None}``) so the pages render nothing and the sweep does not retry.
A concept with no embedding yet is left unstamped for a later sweep.

The bar is :data:`DEFAULT_MAX_DISTANCE`, overridable with
``PRECIS_CONCEPT_MEMORY_MAX_DISTANCE``.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

META_KEY = "memory_match"

#: Cosine-distance bar (similarity = 1 - distance). Provisional: pick it by
#: measuring ~20 carded concepts (``scripts/`` sweep with the env override) —
#: not yet measured on prod data.
DEFAULT_MAX_DISTANCE = 0.45

_NEAREST_SQL = """
    SELECT r.ref_id, r.title, MIN(ce.vector <=> %(vec)s::vector) AS dist
    FROM refs r
    JOIN chunks c ON c.ref_id = r.ref_id AND c.retired_at IS NULL
    JOIN chunk_embeddings ce ON ce.chunk_id = c.chunk_id
         AND ce.embedder = %(embedder)s AND ce.status = 'ok'
         AND ce.vector IS NOT NULL
    WHERE r.kind = %(kind)s AND r.retired_at IS NULL
      {tag_clause}
    GROUP BY r.ref_id, r.title
    ORDER BY dist ASC
    LIMIT 1
"""

_RESEARCH_TAG = """AND EXISTS (
        SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
        WHERE rt.ref_id = r.ref_id AND t.namespace = 'SPACE'
          AND t.value = 'research')"""

_NOT_REPO_DEV = """AND NOT EXISTS (
        SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
        WHERE rt.ref_id = r.ref_id AND t.namespace = 'SPACE'
          AND t.value = 'repo-dev')"""


def max_distance() -> float:
    try:
        return float(
            os.environ.get("PRECIS_CONCEPT_MEMORY_MAX_DISTANCE", DEFAULT_MAX_DISTANCE)
        )
    except ValueError:
        return DEFAULT_MAX_DISTANCE


def compute_memory_match(
    store: Store, concept_id: int, *, bar: float | None = None
) -> dict[str, Any] | None:
    """The match dict for one concept, or ``None`` when it has no embedding yet.

    A hit is ``{"ref_id", "kind", "title", "distance"}``; a concept whose
    nearest candidate is past the bar (or with no candidate) is
    ``{"ref_id": None}``."""
    bar = max_distance() if bar is None else bar
    with store.pool.connection() as conn:
        embedder = store.chunks._default_embedder_name(conn)
        row = conn.execute(
            """
            SELECT ce.vector FROM chunks c
            JOIN chunk_embeddings ce ON ce.chunk_id = c.chunk_id
                 AND ce.embedder = %s AND ce.status = 'ok'
                 AND ce.vector IS NOT NULL
            WHERE c.ref_id = %s AND c.ord = -1 AND c.retired_at IS NULL
            """,
            (embedder, concept_id),
        ).fetchone()
        if row is None:
            return None
        vec = row[0]
        for kind, tag_clause in (
            ("memory", _RESEARCH_TAG + _NOT_REPO_DEV),
            ("finding", ""),
        ):
            hit = conn.execute(
                _NEAREST_SQL.format(tag_clause=tag_clause),
                {"vec": vec, "embedder": embedder, "kind": kind},
            ).fetchone()
            if hit is None:
                continue
            ref_id, title, dist = int(hit[0]), hit[1], float(hit[2])
            if dist > bar:
                # Nearest memory is too far; a finding may still be close.
                continue
            return {
                "ref_id": ref_id,
                "kind": kind,
                "title": title or "",
                "distance": round(dist, 4),
            }
    return {"ref_id": None}


def refresh_memory_match(store: Store, concept_id: int) -> bool:
    """Stamp ``meta.memory_match`` on one concept. False when not yet embedded."""
    match = compute_memory_match(store, concept_id)
    if match is None:
        return False
    store.update_ref(concept_id, meta_patch={META_KEY: match})
    return True


def backfill_memory_matches(
    store: Store, *, concept_ids: list[int] | None = None, limit: int = 200
) -> int:
    """Stamp carded concepts (or ``concept_ids``) that lack ``meta.memory_match``.

    Run after each forge pass, so it is also the backfill. Returns the number
    stamped; unembedded concepts are skipped and retried next pass."""
    if concept_ids is None:
        with store.pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT c.ref_id FROM refs c
                WHERE c.kind = 'concept' AND c.retired_at IS NULL
                  AND NOT (c.meta ? %s)
                  AND EXISTS (
                    SELECT 1 FROM links l
                    JOIN refs a ON a.kind = 'anki' AND a.retired_at IS NULL
                         AND a.ref_id = CASE WHEN l.src_ref_id = c.ref_id
                                             THEN l.dst_ref_id ELSE l.src_ref_id END
                    WHERE (l.src_ref_id = c.ref_id AND l.relation = 'represents')
                       OR (l.dst_ref_id = c.ref_id AND l.relation = 'represented-by'))
                ORDER BY c.ref_id DESC LIMIT %s
                """,
                (META_KEY, limit),
            ).fetchall()
        concept_ids = [int(r[0]) for r in rows]
    done = 0
    for cid in concept_ids:
        try:
            done += refresh_memory_match(store, cid)
        except Exception:  # best-effort: never fail the forge pass over a link
            log.warning("memory_match: concept %s failed", cid, exc_info=True)
    return done


__all__ = [
    "DEFAULT_MAX_DISTANCE",
    "META_KEY",
    "backfill_memory_matches",
    "compute_memory_match",
    "refresh_memory_match",
]
