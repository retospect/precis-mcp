"""Dark weekly harvest of trace positives into an operator review queue.

The exporter owns positive/miss semantics. This pass only selects benchmark
examples, deduplicates queries, and snapshots live repo-dev answers (the
scorer's scope). Review history, including settled batches, suppresses repeats
before a new package carries accepted pairs. The queue is deliberately parked:
only an operator in a repo checkout can accept, and acceptance never lands code.
The distinct recall-set-review tag avoids memory-review's negative attribution
in the exporter; these batches review positive examples, not disputed memories.
"""

from __future__ import annotations

import logging
import re
from contextlib import closing
from typing import TYPE_CHECKING, Any

import numpy as np
from psycopg import Connection

from precis.cli.memory import load_recall_set
from precis.cli.memory_recall_set import PROPOSALS_KEY
from precis.cli.traces import export_rows
from precis.workers.runner import BatchResult

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)
SERVICE_NAME = "memory_recall_harvest"
MAX_PROPOSALS = 50


def harvest_enabled(store: Store) -> bool:
    """Missing or unreadable service_config is OFF, like citer_watch."""
    try:
        with store.pool.connection() as conn:
            return (
                conn.execute(
                    "SELECT 1 FROM service_config "
                    "WHERE service = %s AND prio > 0 LIMIT 1",
                    (SERVICE_NAME,),
                ).fetchone()
                is not None
            )
    except Exception:
        log.warning("recall harvest: service_config lookup failed", exc_info=True)
        return False


class _Queries:
    def __init__(self, queries: list[str], embedder: Any) -> None:
        self.embedder = embedder
        self.exact = {self._key(q) for q in queries}
        self.vectors = (
            [self._unit(v) for v in embedder.embed(queries)] if queries else []
        )

    @staticmethod
    def _key(query: str) -> str:
        return " ".join(query.casefold().split())

    @staticmethod
    def _unit(vector: list[float]) -> Any:
        v = np.asarray(vector, dtype=float)
        norm = float(np.linalg.norm(v))
        if not np.isfinite(v).all() or norm == 0:
            raise ValueError("recall harvest requires finite, nonzero query embeddings")
        return v / norm

    def add(self, query: str) -> bool:
        key = self._key(query)
        if key in self.exact:
            return False
        vector = self._unit(self.embedder.embed_one(query))
        if any(1 - float(np.dot(vector, old)) < 0.1 for old in self.vectors):
            return False
        self.exact.add(key)
        self.vectors.append(vector)
        return True


def _answer(conn: Connection, handle: str) -> tuple[str, str] | None:
    if re.fullmatch(r"me[1-9][0-9]*", handle) is None:
        return None
    row = conn.execute(
        "SELECT r.title, r.meta->>'hook' FROM refs r "
        "WHERE r.ref_id = %s AND r.kind = 'memory' AND r.retired_at IS NULL "
        "AND EXISTS (SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id) "
        "WHERE rt.ref_id = r.ref_id AND t.namespace = 'SPACE' AND t.value = 'repo-dev')",
        (int(handle[2:]),),
    ).fetchone()
    return (row[0] or "", row[1] or "") if row else None


def propose_pairs(
    conn: Connection, *, queries: list[str], embedder: Any
) -> list[dict[str, str]]:
    """Consume the read-only exporter, with at most one answer per distinct query."""
    seen = _Queries(queries, embedder)
    proposals: list[dict[str, str]] = []
    with closing(export_rows(conn, since="90 days", kind="memory")) as rows:
        for row in rows:
            query = row["q"]
            if not isinstance(query, str) or not query.strip() or row["trace_miss"]:
                continue
            positives = sorted(
                set(row["positives"]) - set(row["misses"]),
                key=lambda h: (h not in row["used"], h),
            )
            for handle in positives:
                answer = _answer(conn, handle)
                if answer is None:
                    continue
                if not seen.add(query):
                    break
                evidence = [
                    name for name in ("used", "followed") if handle in row[name]
                ]
                proposals.append(
                    {
                        "query": query,
                        "want": handle,
                        "title": answer[0],
                        "hook": answer[1],
                        "source": "trace",
                        "why": f"{row['id']}: {', '.join(evidence)} {handle} "
                        f"(mode={row['mode']})",
                    }
                )
                break
            if len(proposals) == MAX_PROPOSALS:
                break
    return proposals


def run_memory_recall_harvest_pass(
    store: Store,
    *,
    enabled: bool | None = None,
    entries: list[dict[str, Any]] | None = None,
    embedder: Any = None,
) -> BatchResult:
    """No-op until explicitly lit; one parked todo per nonempty batch."""
    if not (harvest_enabled(store) if enabled is None else enabled):
        return BatchResult(handler=SERVICE_NAME, claimed=0, ok=0, failed=0)
    if embedder is None:
        from precis.config import load_config
        from precis.embedder import make_embedder_from_config

        embedder = make_embedder_from_config(load_config(), dim=store.embedding_dim())
    entries = load_recall_set() if entries is None else entries
    queries = [entry["query"] for entry in entries]
    with store.pool.connection() as conn:
        batches = conn.execute(
            "SELECT meta->%s FROM refs WHERE kind = 'todo' AND meta ? %s",
            (PROPOSALS_KEY, PROPOSALS_KEY),
        ).fetchall()
    queries.extend(pair["query"] for (batch,) in batches for pair in batch)
    with store.pool.connection() as conn:
        proposals = propose_pairs(conn, queries=queries, embedder=embedder)
    todo_id = None
    if proposals:
        from precis.dispatch import Hub

        lines = [
            "Review trace-derived recall pairs; these are proposed examples, "
            "not memory corrections.",
            "Accept all: precis memory recall-set accept <this todo id>",
            "Accept a subset: precis memory recall-set accept <this todo id> --only 1,3",
            "Run by hand in a precis Git checkout. The command prints a file diff; "
            "the operator reviews it for private data and lands it. "
            "No automatic commit, push, graph write or fixer.",
        ]
        for n, pair in enumerate(proposals, 1):
            lines.append(
                f"\n{n}. {pair['title']}\n"
                f"q: {pair['query']}\nwant: {pair['want']}\nwhy: {pair['why']}"
            )
        response = (
            Hub(store=store)
            .sibling("todo")
            .put(
                text=f"Review {len(proposals)} trace-derived memory recall pairs",
                body="\n".join(lines),
                tags=["recall-set-review", "waiting-for:reto"],
                meta={PROPOSALS_KEY: proposals, "llm_tier": None},
            )
        )
        todo_id = response.ref_id
    return BatchResult(
        handler=SERVICE_NAME,
        claimed=len(proposals),
        ok=len(proposals),
        failed=0,
        extra={"todo_id": todo_id},
    )
