"""Status tab — corpus / ingest / worker health.

Direct SQL summaries off the live DB: ref counts per kind, the paper
corpus (held vs stub), todo status breakdown, finding-chase status,
and the most recent ``ref_events`` (ingests, status flips, worker
activity). Each section is computed defensively so a schema surprise
in one query degrades to an empty panel instead of a 500.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from precis import health_checks
from precis.alerts import STATE_OPEN, STATE_RESOLVED
from precis.workers.executors._common import QUEUED, RUNNING, STATUS_NAMESPACE, TERMINAL
from precis.workers.registry import SERVICES, ServiceKind
from precis.workers.service_config import ALL_HOSTS, DEFAULT_PRIO
from precis_web.deps import get_store, get_web_config, templates
from precis_web.routes.alerts import _rows as _alert_rows
from precis_web.routes.factory import _ALL, _CATEGORY_ORDER
from precis_web.routes.factory import _activity as _factory_activity
from precis_web.routes.factory import _config_rows as _factory_config_rows
from precis_web.routes.factory import _errors_by_host as _factory_errors_by_host
from precis_web.routes.factory import _host_options as _factory_host_options
from precis_web.routes.factory import _hosts as _factory_hosts
from precis_web.routes.factory import _llm_models as _factory_llm_models
from precis_web.routes.factory import _next_run as _factory_next_run
from precis_web.routes.factory import _quests as _factory_quests
from precis_web.routes.factory import _reserves as _factory_reserves
from precis_web.routes.factory import _scheduler_leases as _factory_scheduler_leases
from precis_web.routes.factory import _slots_by_host as _factory_slots_by_host
from precis_web.timefmt import abs_ts as _abs_ts
from precis_web.timefmt import age_seconds as _age_seconds
from precis_web.timefmt import ago as _ago
from precis_web.timefmt import relative as _relative

if TYPE_CHECKING:
    from precis.store.store import Store

router = APIRouter(prefix="/status", tags=["status"])

#: The System-page merge folded the read-only
#: `/status`, the editable `/factory` console, and the `/budget` cap
#: editor into one "System" page under these sub-tabs. `/status` stays
#: the base URL (it's the most-deep-linked of the three); `?tab=`
#: scopes which section's queries actually run so the page stays as
#: fast as each formerly-standalone route. `/factory` and `/budget`
#: now just redirect here (their POST endpoints stay mounted at their
#: original paths — only the write path's redirect target moved).
_TABS = ("health", "services", "budget", "models", "now")

log = logging.getLogger(__name__)

#: A host that hasn't reported (heartbeat) or logged (worker_logs)
#: within this many seconds is flagged stale in the UI. Generous
#: enough to ride out a missed reporter tick on a few-minute cadence.
_STALE_AFTER_S = 600

#: One DB connection shared across every section of a single request.
#: The page runs ~15 independent SQL sections; opening a pooled
#: connection per section means ~20 serial round-trips to the DB (which
#: is a network hop from the web host), and that dominated page time far
#: more than any single query. The ``index`` route opens ONE connection,
#: parks it here, and :func:`_connect` hands it to every section instead
#: of checking out a fresh one. Unset (``None``) outside a request →
#: :func:`_connect` falls back to the pool, so each helper still works
#: standalone (tests call them directly; the backlog fragment is its own
#: request). A ContextVar keeps this per-async-task, so concurrent
#: requests never share a connection.
_request_conn: ContextVar[Any | None] = ContextVar("_status_request_conn", default=None)


@contextmanager
def _connect(store: Store) -> Iterator[Any]:
    """Yield the request-shared connection if one is parked, else a pooled one.

    When reusing the shared connection we must NOT close it (the route's
    outer ``with`` owns its lifecycle); we only close connections we open
    ourselves.
    """
    shared = _request_conn.get()
    if shared is not None:
        yield shared
    else:
        with store.pool.connection() as conn:
            yield conn


def _rollback_request_conn() -> None:
    """Reset the shared connection after a failed section.

    All sections read on one connection, so a query that errors leaves
    the transaction aborted — every following section on the same
    connection would then fail. Rolling back clears that state so the
    next section starts clean. No-op when there's no shared connection
    (each standalone helper owns a fresh connection that self-heals on
    close) or when the rollback itself fails (best-effort).
    """
    conn = _request_conn.get()
    if conn is not None:
        try:
            conn.rollback()
        except Exception:
            log.exception("status: rollback of shared request connection failed")


def _safe(fn) -> Any:
    """Run a query closure, returning its result or a sentinel on error."""
    try:
        return fn()
    except Exception:
        log.exception("precis web status: section query failed")
        _rollback_request_conn()
        return None


def _kind_counts(store: Store) -> list[dict[str, Any]]:
    with _connect(store) as conn:
        rows = conn.execute(
            "SELECT kind, count(*)::int FROM refs WHERE deleted_at IS NULL "
            "GROUP BY kind ORDER BY count(*) DESC"
        ).fetchall()
    return [{"kind": r[0], "count": int(r[1])} for r in rows]


def _paper_summary(store: Store) -> dict[str, int]:
    with _connect(store) as conn:
        row = conn.execute(
            "SELECT count(*)::int AS total, "
            "count(*) FILTER (WHERE pdf_sha256 IS NOT NULL)::int AS held "
            "FROM refs WHERE kind = 'paper' AND deleted_at IS NULL"
        ).fetchone()
    total, held = (int(row[0]), int(row[1])) if row else (0, 0)
    return {"total": total, "held": held, "stub": total - held}


def _todo_status(store: Store) -> list[dict[str, Any]]:
    with _connect(store) as conn:
        rows = conn.execute(
            """
            SELECT COALESCE(st.value, 'open') AS status, count(*)::int
              FROM refs r
              LEFT JOIN LATERAL (
                SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                 WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS' LIMIT 1
              ) st ON TRUE
             WHERE r.kind = 'todo' AND r.deleted_at IS NULL
             GROUP BY COALESCE(st.value, 'open')
             ORDER BY count(*) DESC
            """
        ).fetchall()
    return [{"status": r[0], "count": int(r[1])} for r in rows]


def _recent_dreams(store: Store, limit: int = 5) -> list[dict[str, Any]]:
    """Most-recent dream-tagged memories.

    Dream-pass writes new memory refs carrying ``tier:dream`` (see
    ``workers/dream_agent.py``). The dream prompt also promotes high-quality
    cross-kind connections to ``tier:synthetic-insight`` during the
    Step-7 self-review. Surface a flag for each so the operator's
    eye lands on the curated insights.
    """
    with _connect(store) as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id, r.title, r.updated_at,
                   EXISTS (
                     SELECT 1 FROM ref_tags rt2
                       JOIN tags t2 ON t2.tag_id = rt2.tag_id
                      WHERE rt2.ref_id = r.ref_id
                        AND t2.namespace = 'tier'
                        AND t2.value = 'synthetic-insight'
                   ) AS is_insight
              FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.kind = 'memory'
               AND r.deleted_at IS NULL
               AND t.namespace = 'tier' AND t.value = 'dream'
             ORDER BY r.updated_at DESC
             LIMIT %s
            """,
            (limit,),
        ).fetchall()
    return [
        {
            "ref_id": r[0],
            "title": (r[1] or "").split("\n", 1)[0][:80] or "—",
            "ago": _ago(r[2]),
            "is_insight": bool(r[3]),
        }
        for r in rows
    ]


def _synthetic_insights_count(store: Store) -> int:
    """How many ``tier:synthetic-insight`` memories exist total.

    Used as the badge on the "Recent dreams" panel link to the
    full insights view at /tags/refs?namespace=tier&value=synthetic-insight.
    """
    with _connect(store) as conn:
        row = conn.execute(
            """
            SELECT count(*)::int
              FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.kind = 'memory'
               AND r.deleted_at IS NULL
               AND t.namespace = 'tier'
               AND t.value = 'synthetic-insight'
            """
        ).fetchone()
    return int(row[0]) if row else 0


def _recent_todo_done(store: Store, limit: int = 5) -> list[dict[str, Any]]:
    """Most-recent todos that flipped to a terminal state.

    Reads ref_events for the ``status:done`` / ``auto-resolved`` /
    ``auto-timeout`` flips on todo refs. Done IS the "work done"
    signal; auto-* siblings keep the panel honest about how a todo
    closed (manual vs evaluator).
    """
    with _connect(store) as conn:
        rows = conn.execute(
            """
            SELECT e.ts, e.event, e.ref_id, r.title
              FROM ref_events e
              JOIN refs r ON r.ref_id = e.ref_id
             WHERE e.event IN ('status:done', 'auto-resolved', 'auto-timeout')
               AND r.kind = 'todo'
             ORDER BY e.ts DESC
             LIMIT %s
            """,
            (limit,),
        ).fetchall()
    return [
        {
            "ts": r[0].strftime("%Y-%m-%d %H:%M") if r[0] else "",
            "ago": _ago(r[0]),
            "event": r[1] or "",
            "ref_id": r[2],
            "title": (r[3] or "").split("\n", 1)[0][:80] or "—",
        }
        for r in rows
    ]


def _backlog_counts(store: Store) -> dict[str, dict[str, Any]]:
    """Per-pass backlog counts: how many *claimable* chunks are waiting?

    Thin wrapper over :func:`precis.health_checks.compute_backlog_counts`
    (the shared "one liveness truth" — §D, ``health_digest`` reads the same
    function) over the request-shared connection. See that function's
    docstring for the pending/done/failed/blocked shape.
    """
    with _connect(store) as conn:
        return health_checks.compute_backlog_counts(conn)


def _recent_agent_activity(store: Store, limit: int = 10) -> list[dict[str, Any]]:
    """Last N LLM-agent pass results — dream / reviewer / job runner.

    These passes each shell out to ``claude -p`` so they're the
    expensive, observable ones. Surface every fire (success AND
    failure) so a string of silent ``failed=1`` shows up on the
    Status panel instead of being lost under the "Recent worker
    passes" filter (which excludes idle ticks). Failed runs render
    in red so the eye lands on them first.
    """
    with _connect(store) as conn:
        rows = conn.execute(
            """
            SELECT ts, host, pass,
                   COALESCE((payload->>'claimed')::int, 0) AS claimed,
                   COALESCE((payload->>'ok')::int, 0)      AS ok,
                   COALESCE((payload->>'failed')::int, 0)  AS failed
              FROM worker_logs
             WHERE pass IN (
                       'dream_agent', 'structural', 'deep_review',
                       'job_claude_inproc', 'quota_check'
                   )
               AND payload IS NOT NULL
               AND COALESCE((payload->>'claimed')::int, 0) > 0
             ORDER BY ts DESC
             LIMIT %s
            """,
            (limit,),
        ).fetchall()
    return [
        {
            "ago": _ago(r[0]),
            "ts": r[0].strftime("%Y-%m-%d %H:%M") if r[0] else "",
            "host": r[1] or "?",
            "pass": r[2] or "?",
            "claimed": int(r[3]),
            "ok": int(r[4]),
            "failed": int(r[5]),
            "ok_flag": int(r[5]) == 0 and int(r[4]) > 0,
        }
        for r in rows
    ]


def _recent_passes(store: Store, limit: int = 5) -> list[dict[str, Any]]:
    """Most-recent chunk_keywords / summarize / embed pass batches.

    These workers DON'T write ref_events — pass summaries naturally
    aren't per-ref (a batch can touch dozens of refs) so the runner
    logs them as ``worker_logs`` rows with a ``payload`` BatchResult.
    Surface the last few productive batches so the activity panel
    doesn't look frozen just because the per-ref event stream has
    quieted down.

    ``Productive`` means ``claimed > 0`` — quiet idle ticks would
    otherwise drown out the real activity.

    NB the chunk-level handlers all log under ``pass='runner'`` (the
    runner's own logger; ``pass`` is the logger name, not the handler),
    so we constrain on ``pass='runner'`` to ride the ``(pass, ts)``
    index and recover the real pass name from ``payload->>'handler'``
    (``embed:bge-m3`` → ``embed``). Bounded to 6h so the index walk
    terminates fast.
    """
    with _connect(store) as conn:
        rows = conn.execute(
            """
            SELECT ts, host,
                   split_part(payload->>'handler', ':', 1) AS pass,
                   COALESCE((payload->>'claimed')::int, 0) AS claimed,
                   COALESCE((payload->>'ok')::int, 0)      AS ok,
                   COALESCE((payload->>'failed')::int, 0)  AS failed
              FROM worker_logs
             WHERE pass = 'runner'
               AND ts > now() - interval '6 hours'
               AND payload IS NOT NULL
               AND COALESCE((payload->>'claimed')::int, 0) > 0
               AND split_part(payload->>'handler', ':', 1)
                   IN ('chunk_keywords', 'summarize', 'embed', 'tag_embeddings')
             ORDER BY ts DESC
             LIMIT %s
            """,
            (limit,),
        ).fetchall()
    return [
        {
            "ago": _ago(r[0]),
            "host": r[1] or "?",
            "pass": r[2] or "?",
            "claimed": int(r[3]),
            "ok": int(r[4]),
            "failed": int(r[5]),
        }
        for r in rows
    ]


def _recent_events(store: Store, limit: int = 20) -> list[dict[str, Any]]:
    # NB: ``ref_events`` stamps its timestamp in column ``ts`` (see
    # 0001_initial.sql), not ``created_at`` — the earlier name made
    # this query raise and the panel silently rendered empty under
    # the ``_safe`` wrapper.
    with _connect(store) as conn:
        rows = conn.execute(
            "SELECT ts, source, event, ref_id FROM ref_events "
            "ORDER BY ts DESC LIMIT %s",
            (limit,),
        ).fetchall()
    return [
        {
            "ts": r[0].strftime("%Y-%m-%d %H:%M") if r[0] else "",
            "source": r[1] or "",
            "event": r[2] or "",
            "ref_id": r[3],
        }
        for r in rows
    ]


def _claude_usage(store: Store) -> dict[str, Any]:
    """Roll up Claude spend from ``ref_events.cost_usd``.

    Every agentic call (dream / reviewers / plan_tick via
    ``claude_agent``) logs an ``agent:done`` event carrying
    ``cost_usd`` and a payload with ``model`` / ``turns_used``. We
    sum cost + count calls over a 24h and 7d window, plus a 7d
    per-model breakdown. Rows without a cost (non-LLM events) are
    excluded by the ``cost_usd IS NOT NULL`` filter.
    """
    with _connect(store) as conn:
        totals = conn.execute(
            "SELECT "
            "count(*) FILTER (WHERE ts > now() - interval '24 hours')::int, "
            "COALESCE(sum(cost_usd) FILTER "
            "(WHERE ts > now() - interval '24 hours'), 0)::float, "
            "count(*)::int, "
            "COALESCE(sum(cost_usd), 0)::float "
            "FROM ref_events "
            "WHERE cost_usd IS NOT NULL AND ts > now() - interval '7 days'"
        ).fetchone()
        by_model = conn.execute(
            "SELECT COALESCE(payload->>'model', source) AS label, "
            "count(*)::int, COALESCE(sum(cost_usd), 0)::float "
            "FROM ref_events "
            "WHERE cost_usd IS NOT NULL AND ts > now() - interval '7 days' "
            "GROUP BY COALESCE(payload->>'model', source) "
            "ORDER BY 3 DESC LIMIT 12"
        ).fetchall()
    cd, cost_d, cw, cost_w = (
        (int(totals[0]), float(totals[1]), int(totals[2]), float(totals[3]))
        if totals
        else (0, 0.0, 0, 0.0)
    )
    return {
        "day": {"calls": cd, "cost": cost_d},
        "week": {"calls": cw, "cost": cost_w},
        "by_model": [
            {"label": r[0] or "\u2014", "calls": int(r[1]), "cost": float(r[2])}
            for r in by_model
        ],
    }


def _tote_state(spent: float, cap: float) -> str:
    """Bar colour for a spend-vs-cap window: green under, amber near, red over."""
    if cap <= 0:
        return "green"
    if spent >= cap:
        return "red"
    return "amber" if spent / cap >= 0.8 else "green"


# store stays Any: tests pass a hand-rolled fake narrower than Store
def _budget_tote(store: Any) -> dict[str, Any]:
    """Whole-runtime rolling spend vs the budget caps, with breakdowns.

    Extends the claude-only :func:`_claude_usage` rollup to the *whole* spend
    ledger the breaker meters — ``llm_call_log`` (router LLMs) **+**
    ``cache_state`` (paid fetches) — over the breaker's own hourly + 24h
    windows, shown against the live caps. Breakdowns (24h) by model and by
    source/provider make a spike diagnosable at a glance. Read-only mirror of
    :mod:`precis.budget.meter`; degrades to an empty dict if the tables or the
    budget module are unavailable.
    """
    from precis.budget import meter

    status = meter.current_status(store, use_cache=False)
    if status is None:
        return {}
    with _connect(store) as conn:
        by_model = conn.execute(
            "SELECT COALESCE(NULLIF(model, ''), '\u2014') AS label, count(*)::int, "
            "COALESCE(sum(cost_usd), 0)::float "
            "FROM llm_call_log "
            "WHERE cost_usd IS NOT NULL AND ts > now() - interval '24 hours' "
            "GROUP BY 1 ORDER BY 3 DESC LIMIT 12"
        ).fetchall()
        llm_by_source = conn.execute(
            "SELECT COALESCE(NULLIF(source, ''), '\u2014') AS label, count(*)::int, "
            "COALESCE(sum(cost_usd), 0)::float "
            "FROM llm_call_log "
            "WHERE cost_usd IS NOT NULL AND ts > now() - interval '24 hours' "
            "GROUP BY 1"
        ).fetchall()
        fetch_by_provider = conn.execute(
            "SELECT COALESCE(NULLIF(provider, ''), '\u2014') AS label, count(*)::int, "
            "COALESCE(sum(cost_usd), 0)::float "
            "FROM cache_state "
            "WHERE cost_usd IS NOT NULL AND cost_usd > 0 "
            "AND fetched_at > now() - interval '24 hours' "
            "GROUP BY 1"
        ).fetchall()
    # Merge LLM sources + paid-fetch providers into one "by source" ledger.
    merged: dict[str, dict[str, float]] = {}
    for r in list(llm_by_source) + list(fetch_by_provider):
        acc = merged.setdefault(str(r[0]), {"calls": 0.0, "cost": 0.0})
        acc["calls"] += int(r[1])
        acc["cost"] += float(r[2])
    source_items: list[tuple[str, int, float]] = [
        (k, int(v["calls"]), float(v["cost"])) for k, v in merged.items()
    ]
    source_items.sort(key=lambda t: t[2], reverse=True)
    by_source = [
        {"label": k, "calls": calls, "cost": cost}
        for k, calls, cost in source_items[:12]
    ]
    windows = [
        {
            "label": "Hourly",
            "spent": status.hourly_spent,
            "cap": status.hourly_cap,
            "pct": min(100, round(status.hourly_spent / status.hourly_cap * 100))
            if status.hourly_cap > 0
            else 0,
            "state": _tote_state(status.hourly_spent, status.hourly_cap),
        },
        {
            "label": "24h",
            "spent": status.daily_spent,
            "cap": status.daily_cap,
            "pct": min(100, round(status.daily_spent / status.daily_cap * 100))
            if status.daily_cap > 0
            else 0,
            "state": _tote_state(status.daily_spent, status.daily_cap),
        },
    ]
    return {
        "windows": windows,
        "tripped": status.tripped,
        "by_model": [
            {"label": r[0], "calls": int(r[1]), "cost": float(r[2])} for r in by_model
        ],
        "by_source": by_source,
    }


def _hosts(store: Store) -> list[dict[str, Any]]:
    """Per-host liveness from ``worker_logs``: last-seen + recent errors.

    A host that logged anything in the last 7 days appears; its
    ``last_seen`` is the newest log line and ``problems`` counts
    WARNING/ERROR rows in the last 24h. ``stale`` flags hosts quiet
    for longer than ``_STALE_AFTER_S``.
    """
    with _connect(store) as conn:
        rows = conn.execute(
            "SELECT host, max(ts) AS last_seen, "
            "count(*) FILTER (WHERE level IN ('WARNING','ERROR') "
            "AND ts > now() - interval '24 hours')::int AS problems "
            "FROM worker_logs WHERE ts > now() - interval '7 days' "
            "GROUP BY host ORDER BY max(ts) DESC"
        ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        age = _age_seconds(r[1])
        out.append(
            {
                "host": r[0],
                "ago": _ago(r[1]),
                "stale": age is None or age > _STALE_AFTER_S,
                "problems": int(r[2]),
            }
        )
    return out


def _heartbeats(store: Store) -> list[dict[str, Any]]:
    """Per-host sensor snapshot from ``host_heartbeat`` (temp + load).

    Read via raw SQL (not the ``HeartbeatMixin``) so the fake-store
    route tests need no method. ``temp`` / ``load`` are ``None`` when
    the reporting host couldn't read them; ``stale`` flags a missed
    reporter cadence.
    """
    with _connect(store) as conn:
        rows = conn.execute(
            "SELECT host, ts, temp_c, load1, load5, load15 "
            "FROM host_heartbeat ORDER BY host"
        ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        age = _age_seconds(r[1])
        out.append(
            {
                "host": r[0],
                "ago": _ago(r[1]),
                "stale": age is None or age > _STALE_AFTER_S,
                "temp_c": float(r[2]) if r[2] is not None else None,
                "load1": float(r[3]) if r[3] is not None else None,
                "load5": float(r[4]) if r[4] is not None else None,
                "load15": float(r[5]) if r[5] is not None else None,
            }
        )
    return out


#: Liveness signals — the end-to-end "is it alive" heartbeat. Each is
#: ``(label, sql returning one timestamp, staleness threshold seconds)``.
#: Only the *scheduled-cadence* signals (news / briefing) carry a
#: threshold and flag amber when overdue; the pipeline stages move only
#: when there's work to ingest, so flagging them on a quiet corpus would
#: cry wolf — they stay informational (threshold ``None``).
_LIVENESS_SIGNALS: list[tuple[str, str, int | None]] = [
    (
        "Paper ingested",
        "SELECT max(created_at) FROM refs WHERE kind = 'paper' AND deleted_at IS NULL",
        None,
    ),
    # ``max(created_at)`` seq-scans all ~1.5M chunks (no index on
    # created_at) — ~900ms. ``created_at`` is monotonic with the serial
    # ``chunk_id`` PK, so the newest row by id carries the newest
    # timestamp: an ``ORDER BY chunk_id DESC LIMIT 1`` PK-index probe
    # gives the same "last extracted" answer in ~1ms.
    (
        "Chunk extracted",
        "SELECT created_at FROM chunks ORDER BY chunk_id DESC LIMIT 1",
        None,
    ),
    ("Chunk indexed (embed)", "SELECT max(created_at) FROM chunk_embeddings", None),
    ("Chunk summarized", "SELECT max(created_at) FROM chunk_summaries", None),
    (
        "News ingested",
        "SELECT max(created_at) FROM refs WHERE kind = 'news' AND deleted_at IS NULL",
        2 * 3600,  # cron */30m — amber after ~4 missed polls
    ),
    (
        # When the dream pass last *ran* (worker_logs), not a tag on its
        # output: dream memories don't carry a stable ``tier:dream`` tag,
        # so the pass log is the reliable liveness source.
        "Dream",
        "SELECT max(ts) FROM worker_logs WHERE pass = 'dream_agent'",
        None,
    ),
    (
        "Morning briefing",
        "SELECT max(ts) FROM worker_logs WHERE pass = 'briefing'",
        26 * 3600,  # daily 07:00 — amber after a missed day
    ),
]


def _liveness(store: Store) -> list[dict[str, Any]]:
    """End-to-end freshness: last activity per pipeline stage + watch.

    Answers "is it alive?" at a glance — when did the corpus last take
    in a paper, extract / index / summarise a chunk, ingest news, dream,
    or deliver the briefing. Each signal is read independently (via the
    shared :func:`precis.health_checks.fetch_freshness_timestamps` — the
    same probe-runner ``health_digest``'s curated Layer-1 checks use) so
    one schema surprise degrades *that row* to "unknown" rather than
    dropping the whole panel (mirrors :func:`_backlog_counts`). Only the
    scheduled-cadence signals (news / briefing) flag stale; the pipeline
    stages are informational since idle is normal on a quiet corpus.
    """
    out: list[dict[str, Any]] = []
    # One connection for all seven signals (was one *per* signal — seven
    # round-trips).
    with _connect(store) as conn:
        probes = health_checks.fetch_freshness_timestamps(
            conn, [(label, sql) for label, sql, _budget in _LIVENESS_SIGNALS]
        )
    for label, _sql, stale_after_s in _LIVENESS_SIGNALS:
        probe = probes[label]
        if not probe.ok:
            out.append(
                {
                    "label": label,
                    "ago": "—",
                    "stale": False,
                    "scheduled": stale_after_s is not None,
                    "unknown": True,
                }
            )
            continue
        age = _age_seconds(probe.ts)
        stale = stale_after_s is not None and (age is None or age > stale_after_s)
        out.append(
            {
                "label": label,
                "ago": _ago(probe.ts) if probe.ts is not None else "never",
                "stale": stale,
                "scheduled": stale_after_s is not None,
                "unknown": False,
            }
        )
    return out


# ── Now sub-tab: live worker activity + job lanes + alerts ─────────
#
# Answers "what is the cluster doing THIS INSTANT" — the 2026-08-09
# fetch_oa monopolization postmortem (a long, log-silent pass that looked
# indistinguishable from a dead worker short of ``py-spy dump``) is what
# this exists to make visible without SSH. See ``precis.workers.activity``
# for the write side.

#: A queued ``kind='job'`` older than this is flagged stalled — long
#: enough to ride out a normal claim-queue depth, short enough to catch a
#: dispatch problem before an operator would otherwise notice.
_STALLED_QUEUE_AFTER_S = 900  # 15 min

#: How many recently-resolved alerts the Now tab shows, dimmed, below the
#: active ones (mirrors ``/alerts``'s own history-tail sizing intent, just
#: much shorter — this is a glance, not a browse).
_NOW_RECENT_ALERTS_LIMIT = 10

#: How many terminal (succeeded/failed/cancelled) jobs the Now tab shows.
_NOW_RECENT_TERMINAL_LIMIT = 20


def _now_hosts(store: Store) -> list[dict[str, Any]]:
    """Per-host live activity: each process's current pass (or idle),
    straight from ``host_heartbeat.meta.activity`` — the centerpiece of
    the Now tab. A host with no ``activity`` key yet (pre-first-pass, or
    a heartbeat-only reporter) still renders with an empty process list.
    """
    with _connect(store) as conn:
        rows = conn.execute(
            "SELECT host, ts, load1, meta FROM host_heartbeat ORDER BY host"
        ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        age = _age_seconds(r[1])
        meta = r[3] or {}
        activity: dict[str, Any] = meta.get("activity") or {}
        processes: list[dict[str, Any]] = []
        for process, state in sorted(activity.items()):
            state = state or {}
            if state.get("idle"):
                processes.append(
                    {
                        "process": process,
                        "idle": True,
                        "last_pass": state.get("last_pass"),
                        "finished_ago": _ago(state.get("finished")),
                    }
                )
            else:
                processes.append(
                    {
                        "process": process,
                        "idle": False,
                        "pass_name": state.get("pass"),
                        "running_ago": _ago(state.get("since")),
                        "running_minutes": (_age_seconds(state.get("since")) or 0.0)
                        / 60.0,
                        "detail": state.get("detail"),
                    }
                )
        out.append(
            {
                "host": r[0],
                "ago": _ago(r[1]),
                "stale": age is None or age > _STALE_AFTER_S,
                "load1": float(r[2]) if r[2] is not None else None,
                "processes": processes,
            }
        )
    return out


def _now_jobs(store: Store) -> dict[str, Any]:
    """Job STATUS-lane snapshot: running (with lease countdown), queued
    (flagging stalled), and the last N terminal transitions.

    ``kind='job'`` rows carry at most one ``STATUS:`` tag at a time
    (``executors._common.set_status`` replaces the prefix, never
    appends), so the join below returns each job's current status plus
    exactly when it was set (``ref_tags.created_at`` — the STATUS-tag
    timestamp).
    """
    with _connect(store) as conn:
        running_rows = conn.execute(
            """
            SELECT r.ref_id, r.title, r.meta, rt.created_at
              FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.kind = 'job' AND r.deleted_at IS NULL
               AND t.namespace = %s AND t.value = %s
             ORDER BY rt.created_at DESC
            """,
            (STATUS_NAMESPACE, RUNNING),
        ).fetchall()
        queued_rows = conn.execute(
            """
            SELECT r.ref_id, r.title, r.meta, r.created_at
              FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.kind = 'job' AND r.deleted_at IS NULL
               AND t.namespace = %s AND t.value = %s
             ORDER BY r.created_at ASC
            """,
            (STATUS_NAMESPACE, QUEUED),
        ).fetchall()
        terminal_rows = conn.execute(
            """
            SELECT r.ref_id, r.title, t.value, rt.created_at
              FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.kind = 'job' AND r.deleted_at IS NULL
               AND t.namespace = %s AND t.value = ANY(%s)
             ORDER BY rt.created_at DESC
             LIMIT %s
            """,
            (STATUS_NAMESPACE, list(TERMINAL), _NOW_RECENT_TERMINAL_LIMIT),
        ).fetchall()

    running: list[dict[str, Any]] = []
    for ref_id, title, meta, since in running_rows:
        meta = meta or {}
        lease_until = meta.get("lease_until")
        lease_age = _age_seconds(lease_until) if lease_until else None
        running.append(
            {
                "ref_id": int(ref_id),
                "title": title,
                "job_type": meta.get("job_type"),
                "lease_host": meta.get("lease_host"),
                "lease_until": lease_until,
                # Positive = time left before expiry, negative = already
                # expired that long ago; None = no lease recorded.
                "lease_remaining_s": -lease_age if lease_age is not None else None,
                "lease_relative": _relative(lease_until) if lease_until else None,
                "lease_until_title": _abs_ts(lease_until) if lease_until else None,
                "lease_expired": lease_age is not None and lease_age > 0,
                "since_ago": _ago(since),
            }
        )

    queued: list[dict[str, Any]] = []
    for ref_id, title, meta, created_at in queued_rows:
        meta = meta or {}
        age = _age_seconds(created_at)
        queued.append(
            {
                "ref_id": int(ref_id),
                "title": title,
                "job_type": meta.get("job_type"),
                "age_ago": _ago(created_at),
                "stalled": age is not None and age > _STALLED_QUEUE_AFTER_S,
            }
        )

    terminal = [
        {
            "ref_id": int(ref_id),
            "title": title,
            "status": status,
            "when_ago": _ago(when),
        }
        for ref_id, title, status, when in terminal_rows
    ]

    return {
        "running": running,
        "queued": queued,
        "terminal": terminal,
        "running_count": len(running),
        "queued_count": len(queued),
        "stalled_count": sum(1 for q in queued if q["stalled"]),
    }


def _now_alerts(store: Store) -> dict[str, Any]:
    """Active alerts first, then a short dimmed tail of recently-resolved
    ones — reuses ``/alerts``'s own row-shaping helper (:func:`_alert_rows`)
    so the two views can never drift on what "open" means."""
    active = [
        {
            "ref_id": a["ref_id"],
            "title": a["title"],
            "state": "active",
            "when": a["last_seen"],
        }
        for a in _alert_rows(store, state_tag=STATE_OPEN, limit=None)
    ]
    recent = [
        {
            "ref_id": a["ref_id"],
            "title": a["title"],
            "state": "resolved",
            "when": a["last_seen"],
        }
        for a in _alert_rows(
            store, state_tag=STATE_RESOLVED, limit=_NOW_RECENT_ALERTS_LIMIT
        )
    ]
    return {"active": active, "recent": recent}


def _now_ctx(store: Store) -> dict[str, Any]:
    """Assemble the Now tab's fragment context — each section degrades to
    empty on its own query failure (:func:`_safe`), same as every other
    sub-tab."""
    jobs = _safe(lambda: _now_jobs(store)) or {
        "running": [],
        "queued": [],
        "terminal": [],
        "running_count": 0,
        "queued_count": 0,
        "stalled_count": 0,
    }
    alerts = _safe(lambda: _now_alerts(store)) or {"active": [], "recent": []}
    return {
        "now_hosts": _safe(lambda: _now_hosts(store)) or [],
        "jobs": jobs,
        "alerts": alerts,
    }


#: A single (ref_id, source) above this many ref_events in 24h is a
#: worker spin loop. Mirrors ``precis.workers.nursery.SPIN_LOOP_EVENTS_24H``
#: — kept as a local literal so the web package doesn't import the
#: worker package just for a threshold.
_SPIN_LOOP_EVENTS_24H = 200


def _background_anomalies(store: Store) -> dict[str, list[dict[str, Any]]]:
    """Background-worker health: spin loops + failed passes (24h).

    Two cheap reads that turn the invisible failure modes of the
    derived-queue workers into something the operator can see without
    SSHing into the DB:

    * ``spin_loops`` — any ``(ref_id, source)`` emitting more than
      :data:`_SPIN_LOOP_EVENTS_24H` ``ref_events`` in 24h. A worker
      re-claiming the same ref every pass (a broken retry window, a
      no-op outcome that never clears the claim) shows up here long
      before it would surface anywhere else.
    * ``failed_passes`` — ``worker_logs`` rows with ``failed > 0`` in
      24h, grouped by ``(host, handler)`` so the operator sees *which*
      derived-queue handler is erroring rather than an opaque ``runner``
      total. The ``schedule`` handler is excluded: its ``failed`` is a
      *skipped-tick* counter, not errors (see the query comment). Distinct
      from the existing "recent agent activity" panel, which only shows
      *productive* passes; this one is specifically the failures.

    Both degrade to an empty list on any schema surprise (the outer
    ``_safe`` wrapper) so the panel can't 500 the page.
    """
    spin_loops: list[dict[str, Any]] = []
    failed_passes: list[dict[str, Any]] = []
    with _connect(store) as conn:
        spin_rows = conn.execute(
            """
            SELECT ref_id, source,
                   (array_agg(event ORDER BY ts DESC))[1] AS last_event,
                   count(*)::int AS n
              FROM ref_events
             WHERE ts > now() - interval '24 hours'
             GROUP BY ref_id, source
            HAVING count(*) > %s
             ORDER BY count(*) DESC
             LIMIT 20
            """,
            (_SPIN_LOOP_EVENTS_24H,),
        ).fetchall()
        spin_loops = [
            {
                "ref_id": r[0],
                "source": r[1] or "?",
                "last_event": r[2] or "?",
                "count": int(r[3]),
            }
            for r in spin_rows
        ]
        fail_rows = conn.execute(
            """
            SELECT host,
                   COALESCE(payload->>'handler', pass) AS handler,
                   sum(COALESCE((payload->>'failed')::int, 0))::int AS failed,
                   max(ts) AS last_ts
              FROM worker_logs
             WHERE ts > now() - interval '24 hours'
               AND COALESCE((payload->>'failed')::int, 0) > 0
               -- Two passes overload BatchResult.failed to mean a normal
               -- verdict, not an error, so they are dropped from this error
               -- panel rather than cry wolf:
               --   * schedule — 'ticks *skipped* this pass' (collision-skip
               --     when the previous spawned child is still open). A single
               --     recurring wedged behind an open child inflates this to
               --     tens of thousands/day; the real condition (a stalled
               --     recurring) surfaces as a nursery 'stalled-recurring' alert.
               --   * corpus_reconcile — 'held PDFs *recorded absent* on this
               --     host' (a normal presence-ledger verdict, not a pass
               --     error; the count is already in the pass's INFO log). One
               --     node with a partial corpus mount inflates this to tens of
               --     thousands/day.
               AND COALESCE(payload->>'handler', '') NOT IN
                   ('schedule', 'corpus_reconcile')
             GROUP BY host, COALESCE(payload->>'handler', pass)
             ORDER BY failed DESC
             LIMIT 20
            """,
        ).fetchall()
        failed_passes = [
            {
                "host": r[0] or "?",
                "handler": r[1] or "?",
                "failed": int(r[2]),
                "ago": _ago(r[3]),
            }
            for r in fail_rows
        ]
    return {"spin_loops": spin_loops, "failed_passes": failed_passes}


def _automations(store: Store, limit: int = 20) -> list[dict[str, Any]]:
    """Standing automations — recurring todos (``meta.schedule`` set)
    carrying the ``automation`` tag (folded the retired
    ``kind='cron'`` push-notification mechanism onto the recurring facet
    + ``meta.deliver``).

    The recurring *agent behaviours* (the morning/evening podcast casts, the
    news briefing) as opposed to plain doable-queue recurrings. For each,
    surface the schedule + last tick + status plus the most recent artifact
    it produced (via a ``derived-into`` link), so the operator can see what
    runs and what it made. See ``automations-index`` (git-only).

    Uses ``store.list_refs(has_schedule=True, tags=['automation'])`` (§M
    facet normalization — recurring is a ``meta.schedule`` presence filter,
    not a tag, since ``level:recurring`` was retired). Automations are
    few, so the per-ref tag/link/event reads are cheap.
    """
    refs = store.list_refs(
        kind="todo", has_schedule=True, tags=["automation"], limit=limit
    )
    out: list[dict[str, Any]] = []
    for r in refs:
        meta = r.meta or {}
        schedule = meta.get("schedule") or {}
        deliver = meta.get("deliver") or {}
        # Subtype = any open tag other than the marker.
        subtype = ", ".join(
            t.value
            for t in store.tags_for(r.id)
            if t.namespace == "open" and t.value != "automation"
        )
        status = "open"
        fire_count = 0
        last_dt = None
        with _connect(store) as conn:
            srow = conn.execute(
                "SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id "
                "WHERE rt.ref_id = %s AND t.namespace = 'STATUS' LIMIT 1",
                (r.id,),
            ).fetchone()
            if srow is not None:
                status = srow[0]
            crow = conn.execute(
                "SELECT count(*), max(ts) FROM ref_events "
                "WHERE ref_id = %s AND source = 'schedule' AND event IN ('spawn', 'deliver')",
                (r.id,),
            ).fetchone()
            if crow is not None:
                fire_count = int(crow[0] or 0)
                last_dt = crow[1]
            prow = conn.execute(
                "SELECT o.ref_id, o.kind, o.title FROM links l "
                "JOIN refs o ON o.ref_id = l.dst_ref_id "
                "WHERE l.src_ref_id = %s AND l.relation = 'derived-into' "
                "  AND o.deleted_at IS NULL "
                "ORDER BY l.created_at DESC LIMIT 1",
                (r.id,),
            ).fetchone()
        produced: dict[str, Any] | None = None
        if prow is not None:
            produced = {
                "ref_id": prow[0],
                "kind": prow[1] or "?",
                "title": (prow[2] or "").split("\n", 1)[0][:80] or "—",
            }
        recurring = schedule.get("cron") or (
            f"at:{schedule.get('at')}" if schedule.get("at") else "?"
        )
        out.append(
            {
                "ref_id": r.id,
                "label": subtype or "(untyped)",
                "payload": (r.title or "").split("\n", 1)[0][:80] or "—",
                "recurring": recurring,
                "last_fired": _ago(last_dt) if last_dt is not None else "never",
                "fire_count": fire_count,
                "status": status,
                "produced": produced,
                "target": deliver.get("target") or "",
            }
        )
    return out


def _app_version() -> str:
    """Installed ``precis-mcp`` version, for stale-server detection."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("precis-mcp")
    except PackageNotFoundError:  # pragma: no cover - editable/source runs
        return "unknown"


def _quota_view(store: Store) -> dict[str, Any]:
    """The claude-OAuth quota lane: the snapshot's windows + the live pause
    decision (if any). Degrades to an empty view when no snapshot exists.

    Moved here from ``budget.py`` (WS3) — ``budget.py``'s own ``/budget``
    GET route now just redirects to the Budget sub-tab, and this needed to
    live somewhere that ``status.py`` could reach without a circular
    import (``budget.py`` already imports :func:`_budget_tote` from here).
    """
    from precis.budget import quota as budget_quota

    try:
        row = store.read_claude_quota()
    except Exception:
        row = None
    windows: list[dict[str, Any]] = []
    ts = None
    if row is not None:
        ts = row.ts
        raw = row.data.get("windows")
        if isinstance(raw, dict):
            for name, bucket in raw.items():
                if not isinstance(bucket, dict):
                    continue
                windows.append(
                    {
                        "name": name,
                        "status": str(bucket.get("status", "") or "—"),
                        "used": bucket.get("used_percentage"),
                        "resets_at": bucket.get("resets_at"),
                    }
                )
    pause = budget_quota.evaluate(store)
    return {
        "ts": ts,
        "windows": windows,
        "paused": pause is not None,
        "pause_reason": pause.reason if pause is not None else None,
    }


def _health_ctx(store: Store, cfg: Any) -> dict[str, Any]:
    """Health sub-tab: the old Status page's telemetry + the reconciled
    host strip (WS3 folds factory's capability chips + 6h error samples
    onto the same heartbeat row instead of a second, separate strip on
    ``/factory`` — see the module docstring on the host-strip merge)."""
    heartbeats = _safe(lambda: _heartbeats(store)) or []
    host_logs = {h["host"]: h for h in (_safe(lambda: _hosts(store)) or [])}
    # factory's own `_hosts` reads `host_heartbeat` too (a second, capability-
    # oriented pass over the same table) and already parses `meta.top_cpu` —
    # gr162694 #5 just wires that already-gathered field into the strip; no
    # new collection.
    top_cpu_by_host = {h["host"]: h["top_cpu"] for h in _factory_hosts(store)}
    slots_by_host = _factory_slots_by_host(store)
    errors_by_host = _factory_errors_by_host(store)
    reserves = _factory_reserves(store)
    wildcard_reserve = reserves.get(ALL_HOSTS)
    for hb in heartbeats:
        hb["slots"] = slots_by_host.get(hb["host"], [])
        hb["errors_6h"] = errors_by_host.get(hb["host"])
        hb["reserve"] = reserves.get(hb["host"]) or wildcard_reserve
        hb["top_cpu"] = top_cpu_by_host.get(hb["host"], [])
        lg = host_logs.pop(hb["host"], None)
        hb["log_ago"] = lg["ago"] if lg else None
        hb["problems"] = lg["problems"] if lg else 0
    # Hosts that only show up via worker_logs (no heartbeat row at all)
    # still render, as a plain pill — same as the old status page.
    extra_hosts = list(host_logs.values())
    return {
        "kind_counts": _safe(lambda: _kind_counts(store)) or [],
        "papers": _safe(lambda: _paper_summary(store)) or {},
        "todo_status": _safe(lambda: _todo_status(store)) or [],
        "events": _safe(lambda: _recent_events(store)) or [],
        "recent_dreams": _safe(lambda: _recent_dreams(store)) or [],
        "insight_count": _safe(lambda: _synthetic_insights_count(store)) or 0,
        "recent_todo_done": _safe(lambda: _recent_todo_done(store)) or [],
        "recent_passes": _safe(lambda: _recent_passes(store)) or [],
        "recent_agents": _safe(lambda: _recent_agent_activity(store)) or [],
        "automations": _safe(lambda: _automations(store)) or [],
        # NB ``backlog`` is intentionally absent here — it is the
        # slowest section (full-table ``chunks`` scans) and is
        # lazy-loaded by the template via ``GET /status/backlog``
        # (the ``_backlog`` fragment below) so it never blocks the
        # initial page render.
        "liveness": _safe(lambda: _liveness(store)) or [],
        "usage": _safe(lambda: _claude_usage(store)) or {},
        "heartbeats": heartbeats,
        "extra_hosts": extra_hosts,
        "bg_health": _safe(lambda: _background_anomalies(store))
        or {"spin_loops": [], "failed_passes": []},
        "corpus_dir": "  ".join(str(p) for p in cfg.corpus_dirs),
        "app_version": _app_version(),
    }


def _services_ctx(store: Store, host: str) -> dict[str, Any]:
    """Services sub-tab: the retired ``/factory`` page's category tables +
    editable prio/model_pref + Quests, unchanged (its SQL helpers already
    degrade to empty on their own — see ``factory.py``).

    The host *strip* (load/capability chips) moved to the Health sub-tab
    (see :func:`_health_ctx`) — this only needs the host *list*, to
    build the ``?host=`` selector that scopes prio/model_pref edits.
    """
    hosts = _factory_hosts(store)
    config = _factory_config_rows(store)
    activity = _factory_activity(store)
    models = _factory_llm_models(store)
    host_options = _factory_host_options(hosts, config)
    if host not in host_options:
        host = _ALL
    leases = _factory_scheduler_leases(store)
    reserves = _factory_reserves(store)

    # Explicit rows for the selected host, and the cross-host override hints.
    exact: dict[str, tuple[int, str | None]] = {
        s: (p, m) for (s, h2, p, m) in config if h2 == host
    }
    others: dict[str, list[str]] = {}
    for s, h2, p, _m in config:
        if h2 != host:
            others.setdefault(s, []).append(f"{h2}={p}")

    by_category: dict[str, list[dict[str, Any]]] = {}
    for spec in SERVICES:
        act = activity.get(spec.log_handler, {})
        ex = exact.get(spec.name)
        row = {
            "name": spec.name,
            "label": spec.label,
            "kind": spec.kind.value,
            "one_line": spec.one_line,
            "profiles": ", ".join(sorted(spec.default_profiles)) or "—",
            "enable_env": spec.enable_env,
            "requires": sorted(spec.requires),
            "uses_model": spec.uses_model,
            "external": list(spec.uses_external),
            "has_agent": spec.introspect is not None,
            "prio": ex[0] if ex is not None else None,  # None → "default"
            "model_pref": ex[1] if ex is not None else None,
            "others": ", ".join(others.get(spec.name, [])),
            "last_ok": _ago(act["last_ok"]) if act.get("last_ok") else None,
            # gr162694 #4 (tooltip half only — the session drill-through is
            # out of scope): the absolute wall-clock the relative "Xh ago"
            # text hides, from the same row already loaded above.
            "last_ok_title": _abs_ts(act["last_ok"]) if act.get("last_ok") else None,
            "last_fail": _ago(act["last_fail"]) if act.get("last_fail") else None,
            "last_fail_title": (
                _abs_ts(act["last_fail"]) if act.get("last_fail") else None
            ),
            # gr162694 #1: cadence next-fire, "every cycle", or blank.
            "next_run": _factory_next_run(spec.name, spec.kind.value, leases),
        }
        by_category.setdefault(spec.category, []).append(row)

    ordered = [c for c in _CATEGORY_ORDER if c in by_category]
    ordered += sorted(c for c in by_category if c not in _CATEGORY_ORDER)
    categories = [{"name": c, "services": by_category[c]} for c in ordered]

    return {
        "hosts": hosts,
        "categories": categories,
        "default_prio": DEFAULT_PRIO,
        "selected_host": host,
        "host_options": host_options,
        "models": models,
        "service_kinds": [k.value for k in ServiceKind],
        "quests": _factory_quests(store),
        "llm_chains": _llm_chain_ctx(store),
        "llm_ops": _llm_ops_ctx(store),
        # §B-2 reserve mode (gr162694 §K item 4) — the selected host's own
        # row, or the wildcard's if it carries no row of its own.
        "reserve": reserves.get(host) or reserves.get(ALL_HOSTS),
    }


def _llm_chain_ctx(store: Store) -> dict[str, Any]:
    """The capability tiers + placement chains Phase B operator placement-chain editor state — one row
    per pure-capability tier (``FRONTIER``/``BIG``/``MEDIUM``/``SMALL``) plus
    the cloud-throttle dial, for the Services sub-tab's chain-editor panel.

    Reads go through an explicit ``store`` via ``budget_settings.get_setting``
    (not ``live_config``'s ambient-store cached readers — this is a page
    render, not the dispatch hot path) and degrade to empty/default on any
    surprise: a bad row or DB hiccup shows "default"/blank rather than
    500ing the page.
    """
    try:
        from precis.budget import settings as budget_settings
        from precis.utils.llm import live_config
        from precis.utils.llm.router import Tier

        tiers = []
        for t in (Tier.FRONTIER, Tier.BIG, Tier.MEDIUM, Tier.SMALL):
            raw = budget_settings.get_setting(store, live_config.chain_key(t))
            chain_json = ""
            if raw:
                try:
                    parsed = json.loads(raw)
                except (json.JSONDecodeError, TypeError, ValueError):
                    parsed = None
                if isinstance(parsed, list):
                    chain_json = json.dumps(parsed, indent=2)
            tiers.append({"tier": t.value, "chain_json": chain_json})

        cloud_raw = budget_settings.get_setting(store, live_config.CLOUD_ENABLED_KEY)
        cloud_enabled = (
            True
            if cloud_raw is None
            else cloud_raw.strip().lower() not in ("false", "0", "no", "off")
        )
        return {"tiers": tiers, "cloud_enabled": cloud_enabled}
    except Exception:
        log.warning("status: _llm_chain_ctx failed", exc_info=True)
        return {
            "tiers": [
                {"tier": t, "chain_json": ""}
                for t in ("frontier", "big", "medium", "small")
            ],
            "cloud_enabled": True,
        }


def _llm_op_stats(store: Store) -> dict[str, dict[str, Any]]:
    """One ``GROUP BY source`` rollup over ``llm_call_log`` — last-run,
    7-day call volume, and last-seen model per observed ``source``, for the
    operations panel's row set (:func:`_llm_ops_ctx`) and its activity
    columns. Migration ``0078`` dropped the ``(source, ts)`` composite index
    as unused; a live join is fine at today's table size (the proposal's
    blast-radius note). Degrades to ``{}`` on any surprise.
    """
    try:
        with _connect(store) as conn:
            cur = conn.execute(
                "SELECT source, max(ts) AS last_run, "
                "count(*) FILTER (WHERE ts > now() - interval '7 days') AS calls_7d, "
                "(array_agg(model ORDER BY ts DESC))[1] AS last_seen_model "
                "FROM llm_call_log "
                "WHERE source IS NOT NULL AND source <> '' "
                "GROUP BY source"
            )
            rows = cur.fetchall()
    except Exception:
        log.warning("status: llm_call_log source rollup failed", exc_info=True)
        return {}
    return {
        r[0]: {"last_run": r[1], "calls_7d": int(r[2] or 0), "last_seen_model": r[3]}
        for r in rows
    }


def _llm_ops_ctx(store: Store) -> dict[str, Any]:
    """Per-operation LLM routing panel state — one row per operation over
    **union(registry LLM_OPERATIONS keys, EXCLUDED_OPERATIONS keys, observed
    ``llm_call_log.source`` values)**, sorted last-run desc (never-run
    operations sort last). Excluded ops are unioned in unconditionally
    (not only when observed) so AC8's "non-steerable ops are visible but
    inert" holds even before any excluded op has actually run.

    Reads go through an explicit ``store`` via ``budget_settings.get_setting``
    (not ``live_config``'s cached readers), mirroring :func:`_llm_chain_ctx`;
    degrades to an empty row list on any surprise.
    """
    try:
        from precis.budget import settings as budget_settings
        from precis.utils.llm import live_config, operations

        stats = _llm_op_stats(store)
        sources = (
            set(operations.LLM_OPERATIONS)
            | set(operations.EXCLUDED_OPERATIONS)
            | set(stats)
        )

        rows: list[dict[str, Any]] = []
        for source in sources:
            default = operations.op_default(source)
            st = stats.get(source, {})
            steerable = operations.is_steerable(source)

            # A stale ``llm.op.<source>`` row from before an operation was
            # demoted into EXCLUDED_OPERATIONS (or never registered) must
            # NOT surface as "effective" — resolve_op() only reads the
            # override for a *registered* source, so a non-steerable row is
            # dead data the router already ignores. Only read/show it here
            # when steerable, so the display can't lie about what's live.
            override = None
            if steerable:
                raw = budget_settings.get_setting(store, live_config.op_key(source))
                if raw:
                    try:
                        parsed = json.loads(raw)
                    except (json.JSONDecodeError, TypeError, ValueError):
                        parsed = None
                    if isinstance(parsed, dict):
                        override = parsed

            if override and override.get("model"):
                effective = str(override["model"])
            elif override and override.get("tier"):
                effective = f"{override['tier']} (tier)"
            elif default is not None:
                effective = f"{default.tier.value} / {default.model or '—'}"
            else:
                effective = "—"

            last_run = st.get("last_run")
            rows.append(
                {
                    "source": source,
                    "label": default.label if default else source,
                    "description": default.description if default else "",
                    "note": default.note if default else None,
                    "default_tier": default.tier.value if default else None,
                    "default_model": default.model if default else None,
                    "steerable": steerable,
                    "excluded_reason": operations.excluded_reason(source),
                    "override": override,
                    "effective": effective,
                    "last_run": _ago(last_run) if last_run else None,
                    "last_seen_model": st.get("last_seen_model"),
                    "calls_7d": int(st.get("calls_7d") or 0),
                    "_last_run_ts": last_run,
                }
            )

        rows.sort(
            key=lambda r: (
                (0, -r["_last_run_ts"].timestamp())
                if r["_last_run_ts"] is not None
                else (1, 0.0)
            )
        )
        for r in rows:
            del r["_last_run_ts"]

        return {"rows": rows, "models": _factory_llm_models(store)}
    except Exception:
        log.warning("status: _llm_ops_ctx failed", exc_info=True)
        return {"rows": [], "models": []}


#: Representative ``tools_needed`` per capability tier for the active-routing
#: header. The agentic tiers (FRONTIER/BIG) dispatch with tools, the
#: judge/classify tiers (MEDIUM/SMALL) are one-shot JSON. This only sways the
#: *transport* shown for a tier with **no** operator chain override (the
#: resolved model is tools-independent); an operator chain pins its own
#: transport per rung, so the value is moot there.
_TIER_TOOLS: dict[str, bool] = {
    "frontier": True,
    "big": True,
    "medium": False,
    "small": False,
}


# `Any`, not `Store`: unit tests (test_status_models.py) drive this with a
# `_FakeStore` that only implements `list_refs`, narrower than the
# `Store`-typed `meter.bind_store` call this forwards into (precedent:
# `briefing_cast._lane_quest`).
def _active_routing_ctx(store: Any) -> dict[str, Any]:
    """The capability tiers + placement chains "what each capability tier routes to *right now*" header for
    the Models sub-tab. For each pure-capability tier it resolves the live
    placement chain (:func:`~precis.utils.llm.router.resolve_chain`) and the
    concrete model each rung runs, so an operator sees FRONTIER→opus,
    MEDIUM→glm-4.7, SMALL→(its chain rung-0) at a glance rather than inferring
    it from the catalog cards below.

    The chain + models come from the SAME resolvers ``dispatch`` walks, so this
    can't drift from real routing. Those resolvers read the operator
    ``app_settings`` overrides through ``live_config``'s process-bound store, so
    we ``bind_store`` first (as the budget routes do) — otherwise the reads dark
    to the compiled defaults and the header would silently lie. Everything
    degrades (per-tier to a dash, and the whole section to empty) rather than
    500ing the page.
    """
    try:
        from precis.budget import meter
        from precis.utils.llm import live_config
        from precis.utils.llm.router import (
            Backend,
            Tier,
            _rung_is_cloud,
            resolve_backend,
            resolve_chain,
            resolve_model,
        )

        meter.bind_store(store)
        try:
            backend = resolve_backend()
        except Exception:
            backend = Backend.ANTHROPIC

        rows: list[dict[str, Any]] = []
        for tier in (Tier.FRONTIER, Tier.BIG, Tier.MEDIUM, Tier.SMALL):
            try:
                chain = resolve_chain(
                    tier,
                    tools_needed=_TIER_TOOLS.get(tier.value, False),
                    backend=backend,
                )
                primary = resolve_model(tier)
                has_override = bool(live_config.chain_override(tier))
                rungs = [
                    {
                        # A rung with ``model=None`` inherits the tier's resolved
                        # primary; an operator rung pins its own slug.
                        "model": r.model or primary,
                        "transport": r.transport.value,
                        "placement": "cloud" if _rung_is_cloud(r) else "local",
                    }
                    for r in chain
                ]
            except Exception:
                log.warning(
                    "status: _active_routing_ctx tier %s failed",
                    tier.value,
                    exc_info=True,
                )
                rungs, has_override = [], False
            rows.append(
                {
                    "tier": tier.value,
                    # The rung dispatch tries first IS what the tier routes to now.
                    "active_model": rungs[0]["model"] if rungs else "—",
                    "active_placement": rungs[0]["placement"] if rungs else "—",
                    "source": "operator chain" if has_override else "default",
                    "rungs": rungs,
                }
            )
        return {"active_routing": rows, "active_backend": backend.value}
    except Exception:
        log.warning("status: _active_routing_ctx failed", exc_info=True)
        return {"active_routing": [], "active_backend": "—"}


#: Cloud tiers, strongest first — the sort order for the Models sub-tab's
#: cloud grid (local cards sort served-first, then by model_id). Capability tiers + placement chains
#: capability tiers; SMALL routes to a cloud model too (post-Phase-C), so it
#: ranks after MEDIUM rather than falling into the unranked ``.get(..., 9)``
#: bucket.
_TIER_RANK: dict[str, int] = {"frontier": 0, "big": 1, "medium": 2, "small": 3}


def _llm_card_view(ref: Any) -> dict[str, Any]:
    """Normalise one ``llm`` catalog card into the flat shape the Models
    sub-tab renders: name, tier, provider, headline cost + window, the local
    ``served_by`` hosts (where a model is *sourced* on the fleet — the fact the
    text renderer never surfaces), and the capability axes. Everything degrades
    to ``None``/``[]`` so a half-populated card never 500s the page.
    """
    from precis import llm_catalog

    meta = ref.meta or {}
    model_id = meta.get("model_id") or f"llm:{ref.id}"
    tier = meta.get("tier_floor") or ""
    offerings = meta.get("offerings") or []
    off = offerings[0] if offerings and isinstance(offerings[0], dict) else {}

    hosts = [
        {
            "host": e.get("host"),
            "endpoint": e.get("endpoint"),
            "slots": e.get("max_parallel"),
            "model": e.get("model"),
        }
        for e in (meta.get("served_by") or [])
        if isinstance(e, dict) and e.get("host")
    ]

    if "/" in model_id:
        provider = model_id.split("/", 1)[0]
    elif model_id.startswith("claude"):
        provider = "anthropic"
    else:
        prov = meta.get("provenance")
        provider = (prov.get("source") if isinstance(prov, dict) else None) or "—"

    cap = meta.get("capability") or {}
    caps: list[dict[str, Any]] = []
    for axis in llm_catalog.CAPABILITY_AXES:
        if axis in cap:
            val = cap[axis]
            score = val.get("score") if isinstance(val, dict) else val
            if isinstance(score, (int, float)):
                caps.append({"axis": axis, "score": int(score)})

    return {
        "id": ref.id,
        "model_id": model_id,
        "tier": tier,
        # tier_floor is now capability, not location — the grid
        # split is "where a model is sourced" (per _models_ctx's docstring).
        # A card with `served_by` hosts is fleet-served, so it's ALWAYS
        # local regardless of what its model_id looks like. The MODEL-id
        # sniff (`claude-*` / provider-slugged, e.g. `z-ai/glm-4.7`) is only
        # the fallback classifier for un-served cards — bare local aliases
        # (`summarizer`, `qwen-heavy`, `rake-lemma`) have neither → local.
        "is_cloud": not hosts and ("/" in model_id or model_id.startswith("claude")),
        "provider": provider,
        "price_in": off.get("price_in"),
        "price_out": off.get("price_out"),
        "transport": off.get("transport"),
        "window": off.get("max_input"),
        "quant": off.get("quant"),
        "hosts": hosts,
        "caps": caps,
        "prose": (ref.title or "").strip(),
    }


# `Any`, not `Store`: same `_FakeStore` (list_refs-only) as
# `_active_routing_ctx`, which this forwards `store` into unchanged.
def _models_ctx(store: Any) -> dict[str, Any]:
    """Models sub-tab: the ``llm`` catalog rendered as cards, split by where a
    model is *sourced* — Cloud (``tier_floor`` = ``cloud-*``: provider + list
    price + window) vs Local (fleet-served: the ``served_by`` host chips, plus
    the tier-anchor cards that resolve to one). Read-only; the catalog is minted
    by the ``llm_reconcile`` pass, not here.
    """
    try:
        cards = [_llm_card_view(r) for r in store.list_refs(kind="llm", limit=200)]
    except Exception:
        cards = []
    cloud = [c for c in cards if c["is_cloud"]]
    local = [c for c in cards if not c["is_cloud"]]
    cloud.sort(
        key=lambda c: (
            _TIER_RANK.get(c["tier"], 9),
            c["price_in"] if c["price_in"] is not None else float("inf"),
            c["model_id"],
        )
    )
    # Served (host-backed) models first, then the abstract tier anchors.
    local.sort(key=lambda c: (0 if c["hosts"] else 1, c["model_id"]))
    serving_hosts = sorted({h["host"] for c in local for h in c["hosts"]})
    ctx = {
        "cloud_cards": cloud,
        "local_cards": local,
        "serving_hosts": serving_hosts,
    }
    # The active-routing header is independent of the catalog cards (it reads
    # the live chains, not ``list_refs``), so it renders even when the catalog
    # query above degraded to empty.
    ctx.update(_active_routing_ctx(store))
    return ctx


def _budget_ctx(store: Store) -> dict[str, Any]:
    """Budget sub-tab: the retired ``/budget`` page folded in verbatim —
    the tote, the quota live-pause banner, the cap-editor state, and the
    dream pass's cadence knob (Wave-0 §G)."""
    from precis.budget import meter
    from precis.budget import settings as budget_settings
    from precis.workers import dream_throttle

    tote = _safe(lambda: _budget_tote(store)) or {}
    hourly_override = budget_settings.get_float(store, budget_settings.HOURLY_KEY)
    daily_override = budget_settings.get_float(store, budget_settings.DAILY_KEY)
    bstatus = meter.current_status(store, use_cache=False)
    dream_interval_override = budget_settings.get_float(
        store, dream_throttle.MIN_INTERVAL_KEY
    )
    return {
        "budget": tote,
        "quota": _quota_view(store),
        "hourly_cap": bstatus.hourly_cap if bstatus else None,
        "daily_cap": bstatus.daily_cap if bstatus else None,
        "hourly_custom": hourly_override is not None,
        "daily_custom": daily_override is not None,
        "resume_until": budget_settings.get_resume_until(store),
        "resume_active": budget_settings.resume_active(store),
        "dream_min_interval_minutes": dream_throttle.resolve_min_interval_minutes(
            store
        ),
        "dream_interval_custom": dream_interval_override is not None,
        "dream_last_real_run_at": dream_throttle.last_real_run_at(store),
    }


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def index(
    request: Request, tab: str = "health", host: str = _ALL
) -> HTMLResponse:
    """Render the merged System page (WS3): Health / Services / Budget.

    ``/status`` stays the base URL; ``?tab=`` scopes which sub-tab's
    queries actually run, so a page-load only pays for the section it's
    showing — same cost as when Health/Services/Budget were three
    separate routes.
    """
    if tab not in _TABS:
        tab = "health"
    store = get_store(request)
    cfg = get_web_config(request)
    ctx: dict[str, Any] = {"active_tab": "status", "tab": tab}
    if tab == "health":
        # One connection for the whole section, parked so every reader
        # reuses it (see ``_request_conn``) instead of checking out ~15
        # separate pooled connections — that per-section round-trip
        # fan-out, not any single query, was the bulk of the page's
        # latency.
        with store.pool.connection() as conn:
            token = _request_conn.set(conn)
            try:
                ctx.update(_health_ctx(store, cfg))
            finally:
                _request_conn.reset(token)
    elif tab == "services":
        ctx.update(_services_ctx(store, host))
    elif tab == "models":
        ctx.update(_models_ctx(store))
    elif tab == "now":
        # No section query on the initial page load — the template's pane
        # is a bare htmx shell (same lazy-fragment shape as ``backlog``
        # below), so this route stays cheap regardless of which sub-tab a
        # deep link lands on.
        pass
    else:
        ctx.update(_budget_ctx(store))
    return templates.TemplateResponse(request, "status.html.j2", ctx)


@router.get("/backlog", response_class=HTMLResponse)
async def backlog_fragment(request: Request) -> HTMLResponse:
    """Lazy-loaded chunk-pipeline backlog panel (htmx fragment).

    :func:`_backlog_counts` is the heaviest work on the Status page —
    three full-table aggregate scans over ``chunks`` that mirror each
    worker's claim SQL. The main ``index`` route deliberately omits it
    so the page paints immediately; the template then fetches this
    fragment via ``hx-get="/status/backlog"`` on load and swaps it in.
    """
    store = get_store(request)
    return templates.TemplateResponse(
        request,
        "_status_backlog.html.j2",
        {"backlog": _safe(lambda: _backlog_counts(store)) or {}},
    )


@router.get("/now", response_class=HTMLResponse)
async def now_fragment(request: Request) -> HTMLResponse:
    """Live worker-activity + job-lane + alert snapshot (htmx fragment).

    Polled every 10s by the Now sub-tab's pane (``hx-trigger="load, every
    10s"``) — see :func:`_now_ctx` for the assembled sections and the
    module docstring's "Now" note for why this exists.
    """
    store = get_store(request)
    with store.pool.connection() as conn:
        token = _request_conn.set(conn)
        try:
            ctx = _now_ctx(store)
        finally:
            _request_conn.reset(token)
    return templates.TemplateResponse(request, "_status_now.html.j2", ctx)
