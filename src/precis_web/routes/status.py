"""Status tab — corpus / ingest / worker health.

Direct SQL summaries off the live DB: ref counts per kind, the paper
corpus (held vs stub), todo status breakdown, finding-chase status,
and the most recent ``ref_events`` (ingests, status flips, worker
activity). Each section is computed defensively so a schema surprise
in one query degrades to an empty panel instead of a 500.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from precis_web.deps import get_store, get_web_config, templates
from precis_web.timefmt import age_seconds as _age_seconds
from precis_web.timefmt import ago as _ago

router = APIRouter(prefix="/status", tags=["status"])

log = logging.getLogger(__name__)

#: A host that hasn't reported (heartbeat) or logged (worker_logs)
#: within this many seconds is flagged stale in the UI. Generous
#: enough to ride out a missed reporter tick on a few-minute cadence.
_STALE_AFTER_S = 600


def _safe(fn) -> Any:  # type: ignore[no-untyped-def]
    """Run a query closure, returning its result or a sentinel on error."""
    try:
        return fn()
    except Exception:
        log.exception("precis web status: section query failed")
        return None


def _kind_counts(store: Any) -> list[dict[str, Any]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT kind, count(*)::int FROM refs WHERE deleted_at IS NULL "
            "GROUP BY kind ORDER BY count(*) DESC"
        ).fetchall()
    return [{"kind": r[0], "count": int(r[1])} for r in rows]


def _paper_summary(store: Any) -> dict[str, int]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*)::int AS total, "
            "count(*) FILTER (WHERE pdf_sha256 IS NOT NULL)::int AS held "
            "FROM refs WHERE kind = 'paper' AND deleted_at IS NULL"
        ).fetchone()
    total, held = (int(row[0]), int(row[1])) if row else (0, 0)
    return {"total": total, "held": held, "stub": total - held}


def _todo_status(store: Any) -> list[dict[str, Any]]:
    with store.pool.connection() as conn:
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


def _recent_dreams(store: Any, limit: int = 5) -> list[dict[str, Any]]:
    """Most-recent dream-tagged memories.

    Dream-pass writes new memory refs carrying ``tier:dream`` (see
    ``workers/dream_agent.py``). The dream prompt also promotes high-quality
    cross-kind connections to ``tier:synthetic-insight`` during the
    Step-7 self-review. Surface a flag for each so the operator's
    eye lands on the curated insights.
    """
    with store.pool.connection() as conn:
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


def _synthetic_insights_count(store: Any) -> int:
    """How many ``tier:synthetic-insight`` memories exist total.

    Used as the badge on the "Recent dreams" panel link to the
    full insights view at /tags/refs?namespace=tier&value=synthetic-insight.
    """
    with store.pool.connection() as conn:
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


def _recent_todo_done(store: Any, limit: int = 5) -> list[dict[str, Any]]:
    """Most-recent todos that flipped to a terminal state.

    Reads ref_events for the ``status:done`` / ``auto-resolved`` /
    ``auto-timeout`` flips on todo refs. Done IS the "work done"
    signal; auto-* siblings keep the panel honest about how a todo
    closed (manual vs evaluator).
    """
    with store.pool.connection() as conn:
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


def _backlog_counts(store: Any) -> dict[str, dict[str, Any]]:
    """Per-pass backlog counts: how many chunks are still waiting?

    Each row maps to a worker pass and reports:

    * ``pending`` — chunks the pass still needs to process. Keywords
      live on ``chunks`` (``keywords IS NULL``); embeddings and
      summaries live in their own tables (``chunk_embeddings`` /
      ``chunk_summaries``, ADR 0007), so their predicates are
      ``NOT EXISTS`` probes for a successful row rather than a column
      test on ``chunks``. The exact predicate per pass is fragile to
      schema drift; if any of these queries fails we surface the pass
      with ``pending = -1`` so the operator sees the panel didn't lie
      and can dig in.
    * ``done`` — chunks already done (lets the panel show progress
      as a fraction).

    Cheap reads: keywords is a single index probe; the embed/summary
    counts are correlated ``EXISTS`` probes against a chunk-id index.
    """
    rows: dict[str, dict[str, Any]] = {}

    def _two_count(label: str, pending_where: str, done_where: str) -> None:
        try:
            with store.pool.connection() as conn:
                p = conn.execute(
                    f"SELECT count(*)::int FROM chunks WHERE {pending_where}"
                ).fetchone()
                d = conn.execute(
                    f"SELECT count(*)::int FROM chunks WHERE {done_where}"
                ).fetchone()
            rows[label] = {
                "pending": int(p[0]) if p else 0,
                "done": int(d[0]) if d else 0,
            }
        except Exception:
            log.exception("status: backlog query for %s failed", label)
            rows[label] = {"pending": -1, "done": 0}

    # ``embed`` writes one row per chunk to the separate
    # ``chunk_embeddings`` table (ADR 0007) — there is no
    # ``embedding`` column on ``chunks``. A chunk counts as done once
    # it has a successful row (``status='ok'``); pending is the
    # complement.
    _two_count(
        "embed",
        pending_where=(
            "NOT EXISTS (SELECT 1 FROM chunk_embeddings e "
            "WHERE e.chunk_id = chunks.chunk_id AND e.status = 'ok')"
        ),
        done_where=(
            "EXISTS (SELECT 1 FROM chunk_embeddings e "
            "WHERE e.chunk_id = chunks.chunk_id AND e.status = 'ok')"
        ),
    )
    _two_count(
        "chunk_keywords",
        pending_where="keywords IS NULL",
        done_where="keywords IS NOT NULL",
    )
    # ``summarize`` likewise writes to a separate ``chunk_summaries``
    # table (keyed (chunk_id, summarizer)), not a ``summary`` column
    # on ``chunks``. Done = has a successful summary row.
    _two_count(
        "summarize",
        pending_where=(
            "NOT EXISTS (SELECT 1 FROM chunk_summaries s "
            "WHERE s.chunk_id = chunks.chunk_id AND s.status = 'ok')"
        ),
        done_where=(
            "EXISTS (SELECT 1 FROM chunk_summaries s "
            "WHERE s.chunk_id = chunks.chunk_id AND s.status = 'ok')"
        ),
    )

    # Stamp each pass with the timestamp of its last *productive* batch
    # (``ok > 0``) from ``worker_logs`` so the panel can show "last
    # done N ago" — useful for spotting a pass that's stalled even
    # while pending sits flat. The chunks table carries no per-row
    # "processed_at" (keywords write in place; embeddings/summaries
    # live in their own tables) so the worker-pass log is the cheap,
    # indexed source of "when did this pass last move". Failure here is
    # non-fatal — counts already rendered; we just skip the timestamp.
    try:
        with store.pool.connection() as conn:
            last = conn.execute(
                """
                SELECT pass, max(ts)
                  FROM worker_logs
                 WHERE pass = ANY(%s)
                   AND COALESCE((payload->>'ok')::int, 0) > 0
                 GROUP BY pass
                """,
                (list(rows.keys()),),
            ).fetchall()
        for pass_name, ts in last:
            if pass_name in rows:
                rows[pass_name]["last_ts"] = ts
    except Exception:
        log.exception("status: backlog last-done query failed")

    return rows


def _recent_agent_activity(store: Any, limit: int = 10) -> list[dict[str, Any]]:
    """Last N LLM-agent pass results — dream / reviewer / job runner.

    These passes each shell out to ``claude -p`` so they're the
    expensive, observable ones. Surface every fire (success AND
    failure) so a string of silent ``failed=1`` shows up on the
    Status panel instead of being lost under the "Recent worker
    passes" filter (which excludes idle ticks). Failed runs render
    in red so the eye lands on them first.
    """
    with store.pool.connection() as conn:
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


def _recent_passes(store: Any, limit: int = 5) -> list[dict[str, Any]]:
    """Most-recent chunk_keywords / summarize / embed pass batches.

    These workers DON'T write ref_events — pass summaries naturally
    aren't per-ref (a batch can touch dozens of refs) so the runner
    logs them as ``worker_logs`` rows with a ``payload`` BatchResult.
    Surface the last few productive batches so the activity panel
    doesn't look frozen just because the per-ref event stream has
    quieted down.

    ``Productive`` means ``claimed > 0`` — quiet idle ticks would
    otherwise drown out the real activity.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT ts, host, pass,
                   COALESCE((payload->>'claimed')::int, 0) AS claimed,
                   COALESCE((payload->>'ok')::int, 0)      AS ok,
                   COALESCE((payload->>'failed')::int, 0)  AS failed
              FROM worker_logs
             WHERE pass IN ('chunk_keywords', 'summarize', 'embed',
                            'tag_embeddings')
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
            "host": r[1] or "?",
            "pass": r[2] or "?",
            "claimed": int(r[3]),
            "ok": int(r[4]),
            "failed": int(r[5]),
        }
        for r in rows
    ]


def _recent_events(store: Any, limit: int = 20) -> list[dict[str, Any]]:
    # NB: ``ref_events`` stamps its timestamp in column ``ts`` (see
    # 0001_initial.sql), not ``created_at`` — the earlier name made
    # this query raise and the panel silently rendered empty under
    # the ``_safe`` wrapper.
    with store.pool.connection() as conn:
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


def _claude_usage(store: Any) -> dict[str, Any]:
    """Roll up Claude spend from ``ref_events.cost_usd``.

    Every agentic call (dream / reviewers / plan_tick via
    ``claude_agent``) logs an ``agent:done`` event carrying
    ``cost_usd`` and a payload with ``model`` / ``turns_used``. We
    sum cost + count calls over a 24h and 7d window, plus a 7d
    per-model breakdown. Rows without a cost (non-LLM events) are
    excluded by the ``cost_usd IS NOT NULL`` filter.
    """
    with store.pool.connection() as conn:
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


def _hosts(store: Any) -> list[dict[str, Any]]:
    """Per-host liveness from ``worker_logs``: last-seen + recent errors.

    A host that logged anything in the last 7 days appears; its
    ``last_seen`` is the newest log line and ``problems`` counts
    WARNING/ERROR rows in the last 24h. ``stale`` flags hosts quiet
    for longer than ``_STALE_AFTER_S``.
    """
    with store.pool.connection() as conn:
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


def _heartbeats(store: Any) -> list[dict[str, Any]]:
    """Per-host sensor snapshot from ``host_heartbeat`` (temp + load).

    Read via raw SQL (not the ``HeartbeatMixin``) so the fake-store
    route tests need no method. ``temp`` / ``load`` are ``None`` when
    the reporting host couldn't read them; ``stale`` flags a missed
    reporter cadence.
    """
    with store.pool.connection() as conn:
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


#: A single (ref_id, source) above this many ref_events in 24h is a
#: worker spin loop. Mirrors ``precis.workers.nursery.SPIN_LOOP_EVENTS_24H``
#: — kept as a local literal so the web package doesn't import the
#: worker package just for a threshold.
_SPIN_LOOP_EVENTS_24H = 200


def _background_anomalies(store: Any) -> dict[str, list[dict[str, Any]]]:
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
      24h, grouped by ``(host, pass)``. Distinct from the existing
      "recent agent activity" panel, which only shows *productive*
      passes; this one is specifically the failures.

    Both degrade to an empty list on any schema surprise (the outer
    ``_safe`` wrapper) so the panel can't 500 the page.
    """
    spin_loops: list[dict[str, Any]] = []
    failed_passes: list[dict[str, Any]] = []
    with store.pool.connection() as conn:
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
            SELECT host, pass,
                   sum(COALESCE((payload->>'failed')::int, 0))::int AS failed,
                   max(ts) AS last_ts
              FROM worker_logs
             WHERE ts > now() - interval '24 hours'
               AND COALESCE((payload->>'failed')::int, 0) > 0
             GROUP BY host, pass
             ORDER BY failed DESC
             LIMIT 20
            """,
        ).fetchall()
        failed_passes = [
            {
                "host": r[0] or "?",
                "pass": r[1] or "?",
                "failed": int(r[2]),
                "ago": _ago(r[3]),
            }
            for r in fail_rows
        ]
    return {"spin_loops": spin_loops, "failed_passes": failed_passes}


def _app_version() -> str:
    """Installed ``precis-mcp`` version, for stale-server detection."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("precis-mcp")
    except PackageNotFoundError:  # pragma: no cover - editable/source runs
        return "unknown"


_WINDOW_LABEL: dict[str, str] = {
    "five_hour": "5-hour session",
    "seven_day": "Week (all models)",
    "seven_day_sonnet": "Week (Sonnet only)",
    "seven_day_opus": "Week (Opus only)",
    "overage": "Overage / pay-per-use",
}


def _claude_quota(store: Any) -> dict[str, Any]:
    """Render the OAuth utilisation snapshot for the Status panel.

    Reads the singleton ``claude_quota_snapshot`` row written by the
    agent-worker ``quota_check`` pass. Returns ``{}`` when nothing has
    been written yet (free tier, or first run before the pass has
    fired). The template renders "snapshot unavailable" in that case.
    """
    row = store.read_claude_quota(scope="unified")
    if row is None:
        return {}
    data = row.data or {}
    windows = data.get("windows") or {}
    rendered: list[dict[str, Any]] = []
    for key, payload in windows.items():
        if not isinstance(payload, dict):
            continue
        rendered.append(
            {
                "key": key,
                "label": _WINDOW_LABEL.get(key, key),
                "used_percentage": payload.get("used_percentage"),
                "resets_at": payload.get("resets_at"),
                # Stream-json rate_limit_event carries "active" /
                # "warning" / "exceeded"; legacy --output-format json
                # has no such field and we get None.
                "status": payload.get("status"),
            }
        )
    # Show 5h first, then weeklies, then overage; alphabetical within
    # each tier matches Claude Code's own ordering in `/usage`.
    _ORDER = ("five_hour", "seven_day", "seven_day_sonnet", "seven_day_opus", "overage")
    rendered.sort(key=lambda w: _ORDER.index(w["key"]) if w["key"] in _ORDER else 99)
    return {
        "ts": row.ts.isoformat() if row.ts else None,
        "representative_claim": data.get("representative_claim"),
        "windows": rendered,
    }


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    store = get_store(request)
    cfg = get_web_config(request)
    return templates.TemplateResponse(
        request,
        "status.html.j2",
        {
            "active_tab": "status",
            "kind_counts": _safe(lambda: _kind_counts(store)) or [],
            "papers": _safe(lambda: _paper_summary(store)) or {},
            "todo_status": _safe(lambda: _todo_status(store)) or [],
            "events": _safe(lambda: _recent_events(store)) or [],
            "recent_dreams": _safe(lambda: _recent_dreams(store)) or [],
            "insight_count": _safe(lambda: _synthetic_insights_count(store)) or 0,
            "recent_todo_done": _safe(lambda: _recent_todo_done(store)) or [],
            "recent_passes": _safe(lambda: _recent_passes(store)) or [],
            "recent_agents": _safe(lambda: _recent_agent_activity(store)) or [],
            "backlog": _safe(lambda: _backlog_counts(store)) or {},
            "usage": _safe(lambda: _claude_usage(store)) or {},
            "quota": _safe(lambda: _claude_quota(store)) or {},
            "hosts": _safe(lambda: _hosts(store)) or [],
            "heartbeats": _safe(lambda: _heartbeats(store)) or [],
            "bg_health": _safe(lambda: _background_anomalies(store))
            or {"spin_loops": [], "failed_passes": []},
            "corpus_dir": "  ".join(str(p) for p in cfg.corpus_dirs),
            "app_version": _app_version(),
        },
    )
