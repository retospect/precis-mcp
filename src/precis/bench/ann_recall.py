"""HNSW recall benchmark for ``chunk_embeddings`` (gr690288).

The HNSW index behind semantic search was never benchmarked for recall at
the current scale. This module measures recall@k of the approximate index
against an exact scan: same sampled query vectors, same distance operator,
two planner regimes.

* exact leg — ``SET LOCAL enable_indexscan = off`` (and bitmapscan) forces
  a seqscan + bounded sort, i.e. the true top-k;
* ANN leg — index access allowed, ``SET LOCAL hnsw.ef_search`` per leg, i.e.
  what a production query gets at the pgvector default (40) or any swept
  value; plus the filtered-scan regime the product's own search sets
  (``hnsw.iterative_scan = 'strict_order'``, ``hnsw.max_scan_tuples`` —
  without them a filtered HNSW scan can return fewer than k rows).

Both legs exclude the query's own row from the candidates: the sample ranks
stored vectors as queries, and without the exclusion the distance-0 self-hit
dominates every exact top-k and reduces recall@1 to whether the HNSW walk
visits the query's node — a degenerate metric no prod query (fresh embedded
text, never an exact row) would produce.

Recall@k is ``|ann top-k ∩ exact top-k| / k`` per query, then mean and min
over the query set. The sample is deterministic per seed
(``hashtextextended`` ordering), so the exact leg and every ``ef`` leg rank
the same queries and their numbers are directly comparable.

Every leg is one transaction: ``BEGIN``, the leg's ``SET LOCAL`` GUCs, a
plan probe, then the one ranking SELECT. The probe and the payload run in
the *same* call, so the reported plan describes the execution that
produced the ranking — pre-land review td761407 caught the first version
explaining a different statement in a separate transaction, where a plan
label can drift from the numbers it certifies. The probe is ``EXPLAIN``
of the verbatim ranking statement: ``ANALYZE`` for the ANN legs (a real
execution of that very statement — ~1 s at >1M rows, cheap against the
recall it certifies), planner-only for the context and exact legs (a
second 200 s scan buys nothing; the seqscan is forced anyway). ``TIMING
OFF`` keeps per-node clock reads off the measured path.

The legs also carry a bounded server-side budget. The prod hop lands on
``agent_rw``, whose role default ``statement_timeout`` is 120 s — shorter
than the exact leg's seqscans at >1M rows — so every leg opens with
``SET LOCAL statement_timeout`` (default 20 min, ``--stmt-timeout-s``):
it overrides the role default for this transaction only, still bounds
every statement in the leg (the probe's execution included), and never
leaves a pooled backend unbounded.

Transports:

* :class:`PgUrlRunner` — a direct Postgres URL (dev/test DB, or a host that
  can reach the DB without the ssh hop).
* :class:`ProdPsqlRunner` — pipes each leg through ``scripts/prod-psql``
  (the sanctioned prod hop). The SQL is SELECT-only by construction (no
  UPDATE/DELETE, so the row-cap wrapper never engages) and never COMMITs:
  psql's exit rolls the transaction back, so no setting can persist on
  pgbouncer's pooled backends. psql echoes a command tag (``BEGIN``,
  ``SET``) to stdout for every non-row statement in a multi-statement
  ``-c``, so marker SELECTs delimit the plan and payload sections.

The exact leg seqscans ``chunk_embeddings`` once per query; at >1M rows
that is minutes of sequential read. Run it off-peak.

The leg SQL is deliberately parameter-free (every value is inlined as a
literal): psycopg auto-prepares parameterized statements server-side, and a
cached generic plan ignores the per-leg ``SET LOCAL`` planner GUCs — an
early version of this benchmark used ``%s`` parameters and the "exact" leg
silently reused the ANN leg's cached index plan after the prepare
threshold (recall read 1.0 for every query). Literal-only SQL goes through
the simple protocol, plans fresh each execution, and the legs stay honest.

CLI: ``python -m precis.bench.ann_recall`` (see ``--help``); the repo
wrapper is ``scripts/bench-ann-recall``.

Measured (2026-11-22, throwaway docker pgvector/pgvector:pg17 = pgvector
0.8.7, one host; 2.2M rows total, 1.1M per embedder, prod's index
definition, 50 queries per leg, seed 690288):

* ``rand-unit`` — i.i.d. uniform unit vectors (no neighborhood structure,
  the conservative bound): recall@20 at ef=40 is 0.002 mean, rising to 0.026
  at ef=320. Uniform-random 1024-dim data is pathological for HNSW — the
  exact top-k sit inside a near-flat distance tail shared by millions of
  rows — so treat these as worst-case, not prod-like.
* ``clustered`` — 3 000 Gaussian clusters (within-cluster cosine ~0.92,
  the realistic first-order model of embedding-manifold structure):
  recall@20 at ef=40 is 0.557 mean (min 0.0), 0.558 at ef=80, 0.606 at
  ef=160, 0.682 at ef=320; recall@1 tracks recall@20 (0.56→0.68). The walk
  finds the query's cluster essentially every time — what ef cannot fix is
  the within-cluster ordering, flat in distance to ~367 cluster-mates. ANN
  leg < 0.7 s per 50 queries at every swept ef (exact leg ~220 s).

Interpretation lives in the land commit message for td761406; the headline
for the contextual-embedding question this number gates: at prod scale the
index is fast (ANN legs < 2 s for 50 queries at any swept ef) and recall is
a property of the data's cluster structure, not of ef alone — measure on
prod data with ``scripts/bench-ann-recall --efs 40,80,160,320 --json``
before any re-embed sweep.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

SCHEMA = 1

#: Default ``ef_search`` legs: the pgvector default only. The product's own
#: search raises this with the LIMIT (``_ANN_EF_SEARCH_PER_LIMIT``); sweep
#: higher values with ``--efs 40,80,160,320`` to read the tradeoff.
DEFAULT_EF_SEARCH = (40,)
DEFAULT_KS = (1, 5, 10, 20)
DEFAULT_N_QUERIES = 50
#: Fixed default so re-runs rank the same query set; override with ``--seed``.
DEFAULT_SEED = 690288

#: pgvector's hard ceiling on ``hnsw.ef_search``.
_EF_SEARCH_MAX = 1000

#: Bounded server-side budget one leg gets by default (20 min in ms). The
#: prod ``agent_rw`` role default is 120 s — under the exact leg's seqscans
#: at >1M rows (216–219 s for 50 queries on the scale test); 20 min covers
#: a few hundred queries at that rate while still bounding every statement.
DEFAULT_STATEMENT_TIMEOUT_MS = 1_200_000

HNSW_INDEX_PREFIX = "chunk_embeddings_vec_hnsw"

#: Marker rows delimiting a leg's two output sections. psql prints a
#: command tag (``BEGIN``, ``SET``) to stdout for every non-row-returning
#: statement in a multi-statement ``-c``, so neither section can be located
#: by line position alone: plan lines are everything between the markers,
#: the payload everything after the second.
_PLAN_MARKER = "__precis_ann_recall_plan__"
_RESULT_MARKER = "__precis_ann_recall_result__"


class AnnRecallError(RuntimeError):
    """The benchmark could not run (transport failure, empty table, ...)."""


@dataclass(frozen=True)
class AnnRecallConfig:
    """One benchmark run.

    ``ann_set_local`` are extra ``SET LOCAL`` statements applied to the ANN
    legs (and their plan check) only — the exact leg must never be nudged,
    or it stops being exact. Tests force HNSW on a table too small for the
    cost model to pick it with all of ``enable_seqscan``,
    ``enable_bitmapscan`` and ``enable_sort`` off — once stats exist,
    seqscan-off alone loses to a pkey bitmap scan + sort. A host
    benchmarking a small replica may want the same trio.
    """

    n_queries: int = DEFAULT_N_QUERIES
    k: int = max(DEFAULT_KS)
    ks: tuple[int, ...] = DEFAULT_KS
    ef_search: tuple[int, ...] = DEFAULT_EF_SEARCH
    seed: int = DEFAULT_SEED
    embedder: str | None = None
    statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS
    ann_set_local: tuple[str, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        if self.n_queries < 1:
            raise ValueError(f"n_queries must be >= 1, got {self.n_queries}")
        if self.k < 1:
            raise ValueError(f"k must be >= 1, got {self.k}")
        ks = tuple(sorted({int(k) for k in self.ks}))
        if not ks or ks[0] < 1 or ks[-1] > self.k:
            raise ValueError(
                f"ks must be non-empty and within 1..{self.k}, got {self.ks}"
            )
        if self.statement_timeout_ms < 1:
            raise ValueError(
                f"statement_timeout_ms must be >= 1 (bounded, never 0), "
                f"got {self.statement_timeout_ms}"
            )
        object.__setattr__(self, "ks", ks)
        object.__setattr__(self, "ef_search", self._checked_efs())
        object.__setattr__(
            self,
            "ann_set_local",
            tuple(
                s if s.upper().startswith("SET ") else f"SET LOCAL {s}"
                for s in self.ann_set_local
            ),
        )

    def _checked_efs(self) -> tuple[int, ...]:
        efs = tuple(sorted({int(e) for e in self.ef_search}))
        if not efs or any(e < 1 or e > _EF_SEARCH_MAX for e in efs):
            raise ValueError(
                f"ef_search values must be within 1..{_EF_SEARCH_MAX}, "
                f"got {self.ef_search}"
            )
        return efs


def recall_at_k(exact_ids: Sequence[int], ann_ids: Sequence[int], k: int) -> float:
    """Share of the exact top-``k`` ids present in the ANN top-``k``.

    Both legs rank with the same ``(distance, chunk_id)`` ordering, so ties
    break identically and the sets are well-defined.
    """
    if k < 1:
        raise ValueError(f"k must be >= 1, got {k}")
    exact_top = set(exact_ids[:k])
    if not exact_top:
        return 1.0  # degenerate: fewer rows than k; nothing to miss
    return len(exact_top & set(ann_ids[:k])) / len(exact_top)


def _lit(value: str) -> str:
    """Quote ``value`` as a SQL string literal."""
    return "'" + value.replace("'", "''") + "'"


def _em_cte(cfg: AnnRecallConfig) -> str:
    where = "status = 'ok' AND vector IS NOT NULL"
    if cfg.embedder is not None:
        where += f" AND embedder = {_lit(cfg.embedder)}"
    return (
        "em AS (\n"
        "  SELECT embedder FROM chunk_embeddings\n"
        f"  WHERE {where}\n"
        "  GROUP BY embedder ORDER BY count(*) DESC LIMIT 1\n"
        ")"
    )


def _qs_cte(cfg: AnnRecallConfig) -> str:
    return (
        "qs AS (\n"
        "  SELECT chunk_id AS qid, vector\n"
        "  FROM chunk_embeddings\n"
        "  WHERE status = 'ok' AND vector IS NOT NULL\n"
        "    AND embedder = (SELECT embedder FROM em)\n"
        f"  ORDER BY hashtextextended(chunk_id::text, {cfg.seed})\n"
        f"  LIMIT {cfg.n_queries}\n"
        ")"
    )


def _ann_filter(cfg: AnnRecallConfig) -> str:
    # ce.chunk_id <> q.qid excludes the query's own row. The sample ranks
    # stored vectors as queries, and the self-hit (distance exactly 0)
    # otherwise dominates every exact top-k and makes recall@1 a lottery on
    # whether the HNSW walk happens to visit the query's node (measured on
    # the 2.2M-row scale test: self absent from the ef=40 top-20 for 31/50
    # queries). Prod queries embed fresh text and never match a row exactly,
    # so the exclusion models the prod regime; near-duplicates of the query
    # remain candidates.
    where = "ce.status = 'ok' AND ce.vector IS NOT NULL AND ce.chunk_id <> q.qid"
    if cfg.embedder is not None:
        where += f" AND ce.embedder = {_lit(cfg.embedder)}"
    else:
        where += " AND ce.embedder = (SELECT embedder FROM em)"
    return where


def _lateral(cfg: AnnRecallConfig) -> str:
    return (
        "CROSS JOIN LATERAL (\n"
        "  SELECT ce.chunk_id, ce.vector\n"
        "  FROM chunk_embeddings ce\n"
        f"  WHERE {_ann_filter(cfg)}\n"
        "  ORDER BY ce.vector <=> q.vector, ce.chunk_id\n"
        f"  LIMIT {cfg.k}\n"
        ") e"
    )


def _with_prefix(cfg: AnnRecallConfig) -> str:
    return f"WITH {_em_cte(cfg)},\n{_qs_cte(cfg)}\n"


def context_sql(cfg: AnnRecallConfig) -> str:
    """One-row context snapshot: scale, indexes, pgvector version, defaults."""
    where = "status = 'ok' AND vector IS NOT NULL"
    if cfg.embedder is not None:
        where += f" AND embedder = {_lit(cfg.embedder)}"
    return (
        f"{_with_prefix(cfg)}\n"
        "SELECT json_build_object(\n"
        "  'rows_total', (SELECT count(*) FROM chunk_embeddings),\n"
        f"  'rows_ok', (SELECT count(*) FROM chunk_embeddings WHERE {where}),\n"
        "  'embedder', (SELECT embedder FROM em),\n"
        "  'embedder_counts', COALESCE((\n"
        "    SELECT json_object_agg(embedder, n) FROM (\n"
        f"      SELECT embedder, count(*) AS n FROM chunk_embeddings\n"
        f"      WHERE {where} GROUP BY embedder) s), '{{}}'::json),\n"
        "  'hnsw_indexes', COALESCE((\n"
        "    SELECT json_agg(json_build_object(\n"
        "        'name', c.relname, 'reloptions', c.reloptions) ORDER BY c.relname)\n"
        "    FROM pg_class c\n"
        "    JOIN pg_index i ON i.indexrelid = c.oid\n"
        "    JOIN pg_am am ON am.oid = c.relam\n"
        f"    WHERE c.relname LIKE '{HNSW_INDEX_PREFIX}%idx' AND i.indisvalid),\n"
        "    '[]'::json),\n"
        "  'pgvector_version', (SELECT extversion FROM pg_extension\n"
        "    WHERE extname = 'vector'),\n"
        "  'hnsw_defaults', json_build_object(\n"
        "    'ef_search', current_setting('hnsw.ef_search', true),\n"
        "    'm', current_setting('hnsw.m', true),\n"
        "    'ef_construction', current_setting('hnsw.ef_construction', true))\n"
        ")"
    )


def ranking_query_sql(cfg: AnnRecallConfig) -> str:
    """The shared top-k ranking SELECT (both legs run exactly this text)."""
    return (
        f"{_with_prefix(cfg)},\n"
        "ranked AS (\n"
        "  SELECT q.qid, e.chunk_id,\n"
        "         row_number() OVER (\n"
        "           PARTITION BY q.qid\n"
        "           ORDER BY e.vector <=> q.vector, e.chunk_id) AS rank\n"
        f"  FROM qs q\n{_lateral(cfg)}\n"
        ")\n"
        "SELECT json_agg(json_build_object('qid', qid, 'ids', ids) ORDER BY qid)\n"
        "FROM (\n"
        "  SELECT qid, array_agg(chunk_id ORDER BY rank) AS ids\n"
        "  FROM ranked GROUP BY qid\n"
        ") s"
    )


def statement_timeout_sql(cfg: AnnRecallConfig) -> str:
    """The leg's bounded server-side budget as a ``SET LOCAL``.

    The prod hop lands on ``agent_rw``, whose role default
    ``statement_timeout`` is 120 s (``deploy/roles/postgres``) — shorter
    than the exact leg's seqscans at >1M rows. ``SET LOCAL`` overrides the
    role default for this transaction only, and still bounds every
    statement in the leg — probe included — so a worse-than-expected scan
    cannot hang a pooled backend forever. Raise it via
    ``--stmt-timeout-s`` for a bigger table or more queries; never drop it.
    """
    return f"SET LOCAL statement_timeout = {cfg.statement_timeout_ms}"


def exact_set_statements(cfg: AnnRecallConfig) -> tuple[str, ...]:
    """Planner GUCs forcing the exact leg: no index can answer the ORDER BY."""
    return (
        statement_timeout_sql(cfg),
        "SET LOCAL enable_indexscan = off",
        "SET LOCAL enable_bitmapscan = off",
    )


def ann_set_statements(cfg: AnnRecallConfig, ef: int) -> tuple[str, ...]:
    """Planner GUCs for one ANN leg: the index, at this ``ef_search``.

    Appends the filtered-scan regime the product's own search sets
    (``_chunks_ops.py``: ``strict_order`` + the 200k tuple cap) — without
    ``hnsw.iterative_scan``, a filtered HNSW scan collects its ef candidates
    *pre-filter* and can return fewer than k rows (measured: 874/1000 rows
    across 50 queries on the scale test), which reads as recall loss the
    product's search never experiences. Requires pgvector >= 0.8 (the
    version floor the product itself probes for).
    """
    return (
        statement_timeout_sql(cfg),
        f"SET LOCAL hnsw.ef_search = {ef}",
        "SET LOCAL hnsw.iterative_scan = 'strict_order'",
        "SET LOCAL hnsw.max_scan_tuples = 200000",
        *cfg.ann_set_local,
    )


def plan_probe_sql(query: str, *, analyze: bool = False) -> str:
    """``EXPLAIN`` of ``query`` verbatim — the probe one leg call runs in the
    same transaction as the payload SELECT.

    With ``analyze`` the probe is a real execution of the statement (ANN
    legs: sub-second at >1M rows), so the reported plan is the plan an
    execution of the ranking actually used — the td761407 review's "same
    execution" requirement. ``TIMING OFF`` keeps per-node clock reads off
    the measured path; ``COSTS OFF`` keeps the plan text compact for the
    report. Without ``analyze`` the probe is planner-only — for the
    context snapshot and the exact leg, whose forced seqscan is not worth
    a second minutes-long execution.
    """
    opts = "ANALYZE, TIMING OFF, COSTS OFF" if analyze else "COSTS OFF"
    return f"EXPLAIN ({opts})\n{query}"


_EXEC_MS_RE = re.compile(r"Execution Time:\s*([0-9.]+)\s*ms")


def _execution_ms(plan_text: str) -> float | None:
    """The server-side execution time an ``EXPLAIN ANALYZE`` probe reports."""
    m = _EXEC_MS_RE.search(plan_text)
    return round(float(m.group(1)), 1) if m else None


def plan_used_hnsw(plan_text: str) -> bool:
    """True when some plan line is an index scan on a chunk_embeddings HNSW."""
    return any(
        "Index Scan" in line and HNSW_INDEX_PREFIX in line
        for line in plan_text.splitlines()
    )


def leg_transaction_text(
    statements: Sequence[str], query: str, *, analyze: bool = False
) -> str:
    """The text one prod-psql call runs: ``BEGIN``, the SET LOCALs, the plan
    probe, then the SELECT — one transaction, one execution context.

    Deliberately no COMMIT: psql's exit rolls the transaction back, so the
    read can never leave state behind on a pooled backend, and the SELECT
    is the last statement so psql prints its rows. Marker rows delimit the
    output sections: psql echoes a command tag (``BEGIN``, ``SET``) to
    stdout for every non-row statement in a multi-statement ``-c``, so the
    plan lines are everything between the two markers and the SELECT's
    rows everything after the second.
    """
    parts = ["BEGIN;"]
    parts.extend(f"{s};" for s in statements)
    parts.append(f"SELECT {_lit(_PLAN_MARKER)};")
    parts.append(f"{plan_probe_sql(query, analyze=analyze)};")
    parts.append(f"SELECT {_lit(_RESULT_MARKER)};")
    parts.append(f"{query};")
    return "\n".join(parts) + "\n"


@dataclass(frozen=True)
class LegResult:
    """What one :meth:`SqlRunner.run` returns: the payload plus the plan of
    the execution that produced it (see the module docstring)."""

    plan: str
    payload: Any


class SqlRunner(Protocol):
    """Run one leg (``SET LOCAL`` statements + plan probe + result SELECT)."""

    def run(
        self, statements: Sequence[str], query: str, *, analyze: bool = False
    ) -> LegResult: ...


@dataclass
class PgUrlRunner:
    """Direct psycopg transport for a dev/test/reachable database.

    ``connect_timeout`` bounds only the connection handshake (the outbound
    wait-bound ratchet); query execution time is not part of it.
    """

    url: str
    connect_timeout: int = 15

    def run(
        self, statements: Sequence[str], query: str, *, analyze: bool = False
    ) -> LegResult:
        import psycopg

        with psycopg.connect(self.url, connect_timeout=self.connect_timeout) as conn:
            for stmt in statements:
                conn.execute(stmt)
            plan_rows = conn.execute(plan_probe_sql(query, analyze=analyze)).fetchall()
            plan = "\n".join(str(r[0]) for r in plan_rows)
            rows = conn.execute(query).fetchall()
            if not rows or (len(rows) == 1 and (rows[0][0] is None)):
                raise AnnRecallError(
                    "the ranking SELECT returned NULL — no embeddings match "
                    "the leg filter (status='ok', vector IS NOT NULL, "
                    "embedder from --embedder or the modal one)"
                )
            if len(rows) == 1:
                # The context/ranking SELECTs: one scalar (json) cell.
                # psycopg parses json/jsonb to Python objects; prod-psql
                # hands back text. run_report accepts either.
                return LegResult(plan=plan, payload=rows[0][0])
            return LegResult(plan=plan, payload="\n".join(str(r[0]) for r in rows))


@dataclass
class ProdPsqlRunner:
    """``scripts/prod-psql`` transport — the sanctioned hop to prod.

    ``timeout_s`` bounds one leg call (the outbound wait-bound ratchet). It
    must cover the exact leg's per-query seqscans at the benchmarked scale —
    raise it (or ``--timeout-s``) for a bigger table, never drop it.
    """

    script: Path = Path("scripts/prod-psql")
    timeout_s: float = 3600

    def run(
        self, statements: Sequence[str], query: str, *, analyze: bool = False
    ) -> LegResult:
        text = leg_transaction_text(statements, query, analyze=analyze)
        env = dict(os.environ)
        env["PRECIS_PROD_PSQL_OPTS"] = "-At"
        try:
            proc = subprocess.run(
                [str(self.script)],
                input=text,
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=env,
                check=False,
                timeout=self.timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            raise AnnRecallError(
                f"{self.script} produced no result within {self.timeout_s:.0f}s "
                "(timeout_s) — the leg did not finish; raise --timeout-s for a "
                "larger table"
            ) from exc
        if proc.returncode != 0:
            raise AnnRecallError(
                f"{self.script} exited {proc.returncode}:\n{proc.stderr.strip()}"
            )
        lines = proc.stdout.splitlines()
        try:
            plan_i = lines.index(_PLAN_MARKER)
            result_i = lines.index(_RESULT_MARKER)
        except ValueError:
            raise AnnRecallError(
                f"{self.script} output is missing a marker row "
                f"— unexpected psql output:\n{proc.stdout[:400]}"
            ) from None
        if result_i <= plan_i:
            raise AnnRecallError(
                f"{self.script} markers out of order "
                f"— unexpected psql output:\n{proc.stdout[:400]}"
            )
        plan = "\n".join(lines[plan_i + 1 : result_i]).strip()
        out = "\n".join(lines[result_i + 1 :]).strip()
        if not out:
            raise AnnRecallError(
                f"{self.script} printed no result — the query returned NULL "
                "(no embeddings match the filter?)"
            )
        return LegResult(plan=plan, payload=out)


def _as_json(raw: Any) -> Any:
    """psycopg parses json to Python objects; prod-psql hands back text."""
    return raw if isinstance(raw, (dict, list)) else json.loads(raw)


def _parse_ranking(raw: Any) -> dict[int, list[int]]:
    rows = _as_json(raw)
    return {int(r["qid"]): [int(i) for i in r["ids"]] for r in rows}


def run_report(cfg: AnnRecallConfig, runner: SqlRunner) -> dict[str, Any]:
    """Run every leg and assemble the report dict."""
    measured_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    context = _as_json(
        runner.run((statement_timeout_sql(cfg),), context_sql(cfg)).payload
    )

    ranking_sql = ranking_query_sql(cfg)
    t0 = time.monotonic()
    exact_res = runner.run(exact_set_statements(cfg), ranking_sql)
    exact_wall = time.monotonic() - t0
    if plan_used_hnsw(exact_res.plan):
        raise AnnRecallError(
            "the exact leg's plan used the HNSW index — the enable_indexscan/"
            "enable_bitmapscan GUCs did not take, so the leg is not exact"
        )
    exact = _parse_ranking(exact_res.payload)

    ann_legs: list[dict[str, Any]] = []
    for ef in cfg.ef_search:
        # The probe runs with ANALYZE inside the leg's own transaction under
        # its full GUC set — the plan of a real execution of the ranking
        # statement, not a sibling-transaction guess (td761407).
        stmts = ann_set_statements(cfg, ef)
        t0 = time.monotonic()
        res = runner.run(stmts, ranking_sql, analyze=True)
        wall = time.monotonic() - t0
        ann = _parse_ranking(res.payload)
        if set(ann) != set(exact):
            raise AnnRecallError(
                f"ef={ef} leg ranked a different query set than the exact leg — "
                "the database changed mid-run; re-run"
            )
        recalls: dict[str, dict[str, float]] = {}
        for k in cfg.ks:
            values = [recall_at_k(exact[q], ann[q], k) for q in sorted(exact)]
            recalls[str(k)] = {
                "mean": round(sum(values) / len(values), 4),
                "min": round(min(values), 4),
            }
        used = plan_used_hnsw(res.plan)
        ann_legs.append(
            {
                "ef_search": ef,
                "wall_s": round(wall, 3),
                "exec_ms": _execution_ms(res.plan),
                "plan_used_index": used,
                "plan": res.plan if not used else None,
                "recall": recalls,
            }
        )

    notes: list[str] = []
    if any(not leg["plan_used_index"] for leg in ann_legs):
        notes.append(
            "one or more ANN legs did not use the HNSW index (the planner "
            "preferred a seqscan — the table is likely too small for the "
            "cost model). That leg's recall is 1.0 by construction and says "
            "nothing about ANN quality; run at the real scale or add an "
            "ann_set_local planner nudge."
        )
    notes.append(
        "recall ties break on (distance, chunk_id) in both legs; duplicate "
        "vectors at the k-th boundary can move borderline ids between the "
        "exact and ANN sets."
    )

    return {
        "schema": SCHEMA,
        "gripe": "gr690288",
        "measured_at": measured_at,
        "config": {
            "n_queries": cfg.n_queries,
            "k": cfg.k,
            "ks": list(cfg.ks),
            "ef_search": list(cfg.ef_search),
            "seed": cfg.seed,
            "embedder": cfg.embedder or context.get("embedder"),
        },
        "context": context,
        "exact_leg": {
            "forced": "enable_indexscan=off, enable_bitmapscan=off",
            "wall_s": round(exact_wall, 3),
            "plan": exact_res.plan,
        },
        "ann_legs": ann_legs,
        "notes": notes,
    }


def format_summary(report: dict[str, Any]) -> str:
    """The human table: one recall row per ANN leg."""
    cfg = report["config"]
    lines = [
        "# HNSW recall — chunk_embeddings (gr690288)",
        "",
        f"measured_at: {report['measured_at']}",
        f"table: {report['context']['rows_total']} rows "
        f"({report['context']['rows_ok']} ok+embedded), "
        f"embedder {report['config']['embedder']!r}, "
        f"pgvector {report['context']['pgvector_version']}",
        f"queries: {cfg['n_queries']} (seed {cfg['seed']}), "
        f"exact leg {report['exact_leg']['wall_s']}s",
        "",
        "| ef_search | plan | exec_ms | wall_s | "
        + " | ".join(f"recall@{k} (mean/min)" for k in cfg["ks"]),
        "|---|---|---|---|" + "---|" * len(cfg["ks"]),
    ]
    for leg in report["ann_legs"]:
        plan = "hnsw" if leg["plan_used_index"] else "SEQSCAN FALLBACK"
        exec_ms = "-" if leg["exec_ms"] is None else leg["exec_ms"]
        cells = [
            f"{leg['recall'][str(k)]['mean']:.3f}/{leg['recall'][str(k)]['min']:.3f}"
            for k in cfg["ks"]
        ]
        lines.append(
            f"| {leg['ef_search']} | {plan} | {exec_ms} | {leg['wall_s']} | "
            + " | ".join(cells)
        )
    lines.append("")
    lines.extend(f"- {n}" for n in report["notes"])
    return "\n".join(lines)


def _int_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split(",") if part.strip())


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="bench-ann-recall",
        description="Benchmark HNSW recall@k of chunk_embeddings against an "
        "exact scan (gr690288). Default transport is the scripts/prod-psql "
        "prod hop; pass --url to run against a directly reachable DB.",
    )
    ap.add_argument("--url", help="database URL (default: prod via prod-psql)")
    ap.add_argument("--n", type=int, default=DEFAULT_N_QUERIES, help="queries")
    ap.add_argument("--k", type=int, default=max(DEFAULT_KS), help="rank depth")
    ap.add_argument(
        "--ks",
        type=_int_tuple,
        default=DEFAULT_KS,
        help="comma list of recall depths (<= --k)",
    )
    ap.add_argument(
        "--efs",
        type=_int_tuple,
        default=DEFAULT_EF_SEARCH,
        help="comma list of hnsw.ef_search values (default 40)",
    )
    ap.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_SEED,
        help="query-sample seed (deterministic per seed)",
    )
    ap.add_argument(
        "--embedder",
        default=None,
        help="embedder to benchmark (default: the modal one)",
    )
    ap.add_argument(
        "--out", type=Path, default=None, help="also write the JSON report here"
    )
    ap.add_argument(
        "--timeout-s",
        type=float,
        default=3600,
        help="per-leg bound on the prod-psql transport (default 3600; must "
        "cover the exact leg's seqscans at the benchmarked scale)",
    )
    ap.add_argument(
        "--stmt-timeout-s",
        type=float,
        default=DEFAULT_STATEMENT_TIMEOUT_MS / 1000,
        help="per-statement server-side bound each leg SET LOCALs "
        "(default 1200; overrides the prod role's 120s statement_timeout "
        "for the leg's transaction — the exact leg is one statement, so "
        "this must cover all --n seqscans at the benchmarked scale)",
    )
    ap.add_argument("--json", action="store_true", help="print JSON, not table")
    args = ap.parse_args(argv)

    cfg = AnnRecallConfig(
        n_queries=args.n,
        k=args.k,
        ks=tuple(args.ks),
        ef_search=tuple(args.efs),
        seed=args.seed,
        embedder=args.embedder,
        statement_timeout_ms=int(args.stmt_timeout_s * 1000),
    )
    runner: SqlRunner = (
        PgUrlRunner(args.url) if args.url else ProdPsqlRunner(timeout_s=args.timeout_s)
    )
    report = run_report(cfg, runner)
    if args.out is not None:
        args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(format_summary(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
