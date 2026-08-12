"""Shared health-check SQL — the ONE liveness/backlog truth.

Extracted (``docs/backlog/self-healing-spine.md`` Layer 2) out of
``precis_web.routes.status`` so the web `/status` panel and the
``health_digest`` worker pass (``workers/health_digest.py``) read the
*same* backlog/freshness/activity computation instead of two
independently-maintained copies that could drift. §F (demand-materialized
elastic work) is expected to reuse this module too — "one liveness
truth", per the design doc's warning at the top of §D.

Three things live here, each a straight extraction of pre-existing SQL
(behaviour-preserving — the web panel's numbers must not change):

* :func:`compute_backlog_counts` — per-pass pending/done/failed/blocked
  counts (embed / summarize / chunk_keywords), idle-aware: a pass with
  zero eligible backlog reads "0 pending", never "stale by omission".
  This is the piece the digest's idle-vs-stuck freshness checks need
  (an empty backlog is healthy; a non-draining one is not — the
  embedded/keyworded Layer-1 checks key off ``pending``, not a bare
  timestamp).
* :func:`fetch_freshness_timestamps` — run a batch of ``(key, sql)``
  "last activity" probes on one connection, degrading a single failing
  probe to ``None`` (+ a rollback so later probes on the same
  connection still run) instead of losing the whole batch.
* :func:`activity_by_handler` — ``worker_logs`` BatchResult rows rolled
  up to ``handler -> {last_ok, last_fail}``, the registry × worker_logs
  join precedent (``precis_web/routes/factory.py::_activity``) the
  digest's Layer-2 "intended-on but silent" coherence check reuses.

Every function takes either an open ``conn`` (for a caller already
holding one — the web request-shared connection) or, for the
convenience wrapper, a ``store`` (opens its own).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class FreshnessProbe:
    """One :func:`fetch_freshness_timestamps` result.

    ``ok=False`` means the probe's SQL raised (a schema surprise) — the
    caller should render/treat that as "unknown", distinct from ``ok=True,
    ts=None`` (the query ran fine and legitimately found no matching row —
    "never happened")."""

    ts: datetime | None
    ok: bool


#: Backlog eligibility mirrors the worker *claim* predicates so the counts
#: only cover chunks a pass will actually process. Keep these in sync with
#: the workers (they are small + stable; a mismatch only skews the panel,
#: never the pipeline):
#:   embed          — precis.workers.embed.EmbedHandler.skip_chunk_kinds
#:   summarize      — precis.workers.summarize.RakeLemmaHandler.skip_chunk_kinds
#:   chunk_keywords — precis.workers.chunk_keywords {_SKIP_KINDS,
#:                    _MIN_CHUNK_CHARS, KEYWORDS_VERSION}
_EMBED_ARTIFACT = "bge-m3"
_SUMMARIZE_ARTIFACT = "rake-lemma"
_EMBED_SKIP_KINDS = ("references",)
_SUMMARIZE_SKIP_KINDS = ("references", "table")
_KEYWORDS_SKIP_KINDS = (
    "card_authors",
    "card_combined",
    "card_title",
    "table",
    "equation",
    "figure",
    "references",
)
_KEYWORDS_MIN_CHARS = 150
_KEYWORDS_VERSION = "1.0"


def compute_backlog_counts(conn: Any) -> dict[str, dict[str, Any]]:
    """Per-pass backlog counts on an already-open ``conn``.

    Same three-way (pending/done/failed) plus ``chunk_keywords``' extra
    ``blocked`` split as the original ``/status`` panel query — see the
    module docstring. On any query error the affected pass reads
    ``pending=-1`` (so the caller sees the panel/check didn't lie) and the
    connection is rolled back so later sections on it still run.
    """
    rows: dict[str, dict[str, Any]] = {}

    def _terminal_pass(
        label: str,
        output_table: str,
        artifact_col: str,
        artifact: str,
        skip_kinds: tuple[str, ...],
    ) -> None:
        sql = f"""
            SELECT count(*) FILTER (WHERE elig)                   AS total,
                   count(*) FILTER (WHERE elig AND st = 'ok')     AS done,
                   count(*) FILTER (WHERE elig AND st = 'failed') AS failed
              FROM (
                SELECT
                    (c.chunk_kind <> ALL(%(skip)s)
                     AND (c.meta->>'no_index') IS DISTINCT FROM 'true') AS elig,
                    (SELECT o.status
                       FROM {output_table} o
                      WHERE o.chunk_id = c.chunk_id
                        AND o.{artifact_col} = %(artifact)s
                        AND (o.status = 'failed'
                             OR o.content_sha IS NOT DISTINCT FROM c.content_sha)
                      LIMIT 1) AS st
                  FROM chunks c
              ) t
        """
        try:
            r = conn.execute(
                sql, {"skip": list(skip_kinds), "artifact": artifact}
            ).fetchone()
            total, done, failed = int(r[0]), int(r[1]), int(r[2])
            rows[label] = {
                "pending": total - done - failed,
                "done": done,
                "failed": failed,
            }
        except Exception:
            log.exception("health_checks: backlog query for %s failed", label)
            try:
                conn.rollback()
            except Exception:
                log.exception("health_checks: rollback after backlog query failed")
            rows[label] = {"pending": -1, "done": 0, "failed": 0}

    _terminal_pass(
        "embed",
        "chunk_embeddings",
        "embedder",
        _EMBED_ARTIFACT,
        _EMBED_SKIP_KINDS,
    )
    _terminal_pass(
        "summarize",
        "chunk_summaries",
        "summarizer",
        _SUMMARIZE_ARTIFACT,
        _SUMMARIZE_SKIP_KINDS,
    )

    try:
        r = conn.execute(
            """
            SELECT
                count(*) FILTER (WHERE elig)                            AS total,
                count(*) FILTER (WHERE elig AND NOT pend)               AS done,
                count(*) FILTER (WHERE elig AND pend AND emb_ready)     AS pending,
                count(*) FILTER (WHERE elig AND pend AND NOT emb_ready) AS blocked
              FROM (
                SELECT
                    (c.chunk_kind <> ALL(%(skip)s)
                     AND length(c.text) >= %(minlen)s
                     AND (c.meta->>'no_index') IS DISTINCT FROM 'true') AS elig,
                    EXISTS (
                           SELECT 1 FROM chunk_embeddings ce
                            WHERE ce.chunk_id = c.chunk_id
                              AND ce.embedder = %(emb)s
                              AND ce.status = 'ok'
                              AND ce.content_sha
                                  IS NOT DISTINCT FROM c.content_sha) AS emb_ready,
                    (c.keywords IS NULL
                     OR (c.keywords_meta->>'version') IS DISTINCT FROM %(kv)s
                     OR (c.keywords_meta->>'content_sha')
                         IS DISTINCT FROM c.content_sha) AS pend
                  FROM chunks c
              ) t
            """,
            {
                "skip": list(_KEYWORDS_SKIP_KINDS),
                "minlen": _KEYWORDS_MIN_CHARS,
                "emb": _EMBED_ARTIFACT,
                "kv": _KEYWORDS_VERSION,
            },
        ).fetchone()
        done, pending, blocked = int(r[1]), int(r[2]), int(r[3])
        rows["chunk_keywords"] = {
            "pending": pending,
            "blocked": blocked,
            "done": done,
            "failed": 0,
        }
    except Exception:
        log.exception("health_checks: backlog query for chunk_keywords failed")
        try:
            conn.rollback()
        except Exception:
            log.exception("health_checks: rollback after chunk_keywords query failed")
        rows["chunk_keywords"] = {"pending": -1, "done": 0, "failed": 0}

    for pass_name in rows:
        try:
            row = conn.execute(
                """
                SELECT ts FROM worker_logs
                 WHERE pass = 'runner'
                   AND ts > now() - interval '6 hours'
                   AND COALESCE((payload->>'ok')::int, 0) > 0
                   AND split_part(payload->>'handler', ':', 1) = %s
                 ORDER BY ts DESC
                 LIMIT 1
                """,
                (pass_name,),
            ).fetchone()
            if row and row[0] is not None:
                rows[pass_name]["last_ts"] = row[0]
        except Exception:
            log.exception(
                "health_checks: backlog last-done query for %s failed", pass_name
            )
            try:
                conn.rollback()
            except Exception:
                log.exception("health_checks: rollback after last-done query failed")

    return rows


def backlog_counts(store: Any) -> dict[str, dict[str, Any]]:
    """:func:`compute_backlog_counts` over a fresh connection of its own."""
    with store.pool.connection() as conn:
        return compute_backlog_counts(conn)


def fetch_freshness_timestamps(
    conn: Any, signals: Iterable[tuple[str, str]]
) -> dict[str, FreshnessProbe]:
    """Run each ``(key, sql)`` "last activity" probe on ``conn``.

    ``sql`` must be a single-row, single-column query returning a
    (possibly ``NULL``) timestamp. A probe that raises degrades to
    :class:`FreshnessProbe`\\ ``(ts=None, ok=False)`` for that key — plus a
    rollback so a schema surprise on one probe doesn't abort the shared
    connection for the rest of the batch (the ``/status`` "Liveness" panel
    and ``health_digest``'s curated freshness checks share this: one bad
    query degrades to "unknown" for *that* row, not a dropped panel / a
    false alarm).
    """
    out: dict[str, FreshnessProbe] = {}
    for key, sql in signals:
        try:
            row = conn.execute(sql).fetchone()
            out[key] = FreshnessProbe(ts=row[0] if row else None, ok=True)
        except Exception:
            log.exception("health_checks: freshness probe %s failed", key)
            try:
                conn.rollback()
            except Exception:
                log.exception("health_checks: rollback after freshness probe failed")
            out[key] = FreshnessProbe(ts=None, ok=False)
    return out


def activity_by_handler(
    store: Any, *, window_hours: int = 24 * 7
) -> dict[str, dict[str, Any]]:
    """``handler -> {last_ok, last_fail, last_seen}`` from ``worker_logs``
    BatchResult rows.

    Keyed by the ``payload.handler`` string (a pass's ``BatchResult.handler``
    — not the logger-derived ``pass`` column, which is ``'runner'`` for
    every ref-pass/handler routed through ``workers/runner.py::run_loop``),
    so callers look it up via ``ServiceSpec.log_handler``. Extracted from
    ``precis_web/routes/factory.py::_activity`` (the ``/status`` "Services"
    tab's registry × worker_logs join) so the digest's Layer-2
    "intended-on but silent" coherence check reads the exact same
    last-activity truth the web console shows, just over a shorter window
    (24h) than the console's default 7 days. Degrades to ``{}`` on any
    surprise — never raises.

    ``last_ok``/``last_fail`` are FILTERed on ``ok > 0`` / ``failed > 0`` —
    the web panel's two colour-coded columns. ``last_seen`` is the
    **unfiltered** max ``ts`` for the handler in the window, so a pass that
    ran the whole window claiming 0 rows every cycle (healthy idle — every
    ``run_loop`` iteration logs a row regardless of ``claimed``, see
    ``workers/runner.py``) still shows *some* activity: without it, Layer-2
    read "no last_ok and no last_fail" as "never ran" and flagged an
    idle-but-alive pass as silent (violates the idle-vs-stuck gate).
    """
    try:
        with store.pool.connection() as conn:
            cur = conn.execute(
                "SELECT payload->>'handler' AS h, "
                "  MAX(ts) FILTER ("
                "    WHERE payload->>'ok' ~ '^[0-9]+$' "
                "      AND (payload->>'ok')::int > 0) AS last_ok, "
                "  MAX(ts) FILTER ("
                "    WHERE payload->>'failed' ~ '^[0-9]+$' "
                "      AND (payload->>'failed')::int > 0) AS last_fail, "
                "  MAX(ts) AS last_seen "
                "FROM worker_logs "
                "WHERE payload ? 'handler' "
                "  AND ts > now() - (%(hrs)s || ' hours')::interval "
                "GROUP BY payload->>'handler'",
                {"hrs": window_hours},
            )
            rows = cur.fetchall()
    except Exception:
        log.warning("health_checks: worker_logs activity read failed", exc_info=True)
        return {}
    return {
        h: {"last_ok": ok, "last_fail": fail, "last_seen": seen}
        for h, ok, fail, seen in rows
    }


__all__ = [
    "FreshnessProbe",
    "activity_by_handler",
    "backlog_counts",
    "compute_backlog_counts",
    "fetch_freshness_timestamps",
]
