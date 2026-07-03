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

#: Backlog eligibility mirrors the worker *claim* predicates so the
#: panel counts only chunks a pass will actually process. A naive
#: ``NOT EXISTS(ok embedding)`` count includes chunk kinds the worker
#: skips, ``no_index`` chunks, and terminally-``failed`` rows the
#: worker treats as done — so it can never reach 0 even when the pass
#: is fully caught up. Keep these in sync with the workers (they are
#: small + stable; a mismatch only skews the panel, never the pipeline):
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
    """Per-pass backlog counts: how many *claimable* chunks are waiting?

    Each row maps to a worker pass and reports three disjoint tallies
    over the pass's **eligible** universe (the chunks its claim query
    would actually consider — see ``_*_SKIP_KINDS`` and the ``no_index``
    / length / embedding gates above):

    * ``done`` — has a terminal successful artifact for the current
      ``content_sha`` (embeddings / summaries live in their own tables
      per ADR 0007; keywords live in-place on ``chunks``).
    * ``failed`` — has a terminal ``status='failed'`` row. The worker
      treats these as *done* (a poison chunk must not loop, ADR 0007)
      and never retries them, so they are neither progress nor
      claimable work — they get their own bar. Keywords has no failure
      state (it writes in place), so its ``failed`` is always 0.
    * ``pending`` — eligible, not yet done, not failed. **This is the
      real backlog**: ``pending = 0`` means the pass is caught up.
      (The old panel counted skipped kinds, ``no_index`` chunks, and
      terminally-failed rows as pending, so it could never reach 0.)

    ``total`` for the fraction is ``pending + done + failed`` (the
    eligible universe), which differs per pass — keywords excludes
    short + un-embedded chunks, so its universe is smaller than embed's.

    The predicates mirror the worker claim SQL; on any query error we
    surface the pass with ``pending = -1`` so the operator sees the
    panel didn't lie and can dig in.
    """
    rows: dict[str, dict[str, Any]] = {}

    def _terminal_pass(
        label: str,
        output_table: str,
        artifact_col: str,
        artifact: str,
        skip_kinds: tuple[str, ...],
    ) -> None:
        # embed / summarize: artifact lives in its own table keyed
        # (chunk_id, artifact). A chunk is *done* only with a non-failed
        # row whose content_sha matches (stale rows re-derive); a
        # ``failed`` row is terminal. Eligible = not a skipped kind and
        # not ``no_index`` — exactly the worker's claim filter.
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
            with store.pool.connection() as conn:
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
            log.exception("status: backlog query for %s failed", label)
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

    # ``chunk_keywords`` writes in place on ``chunks`` and has a much
    # stricter claim filter: skip the non-prose kinds, require
    # ``length(text) >= _MIN_CHUNK_CHARS``, skip ``no_index``, and — a
    # hard gate — require a *current* ``bge-m3`` embedding (KeyBERT
    # scores against it), so a chunk whose embed failed is not yet
    # eligible here. Pending = eligible and stale/missing keywords.
    try:
        with store.pool.connection() as conn:
            r = conn.execute(
                """
                SELECT count(*) FILTER (WHERE elig)              AS total,
                       count(*) FILTER (WHERE elig AND NOT pend) AS done
                  FROM (
                    SELECT
                        (c.chunk_kind <> ALL(%(skip)s)
                         AND length(c.text) >= %(minlen)s
                         AND (c.meta->>'no_index') IS DISTINCT FROM 'true'
                         AND EXISTS (
                               SELECT 1 FROM chunk_embeddings ce
                                WHERE ce.chunk_id = c.chunk_id
                                  AND ce.embedder = %(emb)s
                                  AND ce.status = 'ok'
                                  AND ce.content_sha
                                      IS NOT DISTINCT FROM c.content_sha)) AS elig,
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
        total, done = int(r[0]), int(r[1])
        rows["chunk_keywords"] = {
            "pending": total - done,
            "done": done,
            "failed": 0,
        }
    except Exception:
        log.exception("status: backlog query for chunk_keywords failed")
        rows["chunk_keywords"] = {"pending": -1, "done": 0, "failed": 0}

    # Stamp each pass with the timestamp of its last *productive* batch
    # (``ok > 0``) from ``worker_logs`` so the panel can show "last
    # done N ago" — useful for spotting a pass that's stalled even
    # while pending sits flat. The chunks table carries no per-row
    # "processed_at" (keywords write in place; embeddings/summaries
    # live in their own tables) so the worker-pass log is the cheap,
    # indexed source of "when did this pass last move".
    #
    # NB the chunk-level handlers (embed / summarize / chunk_keywords)
    # all log under ``pass='runner'`` — the runner's own logger
    # (``precis.workers.runner``), since ``pass`` is derived from the
    # logger name, not the handler. The real pass name lives in
    # ``payload->>'handler'`` (e.g. ``embed:bge-m3``,
    # ``summarize:rake-lemma``, ``chunk_keywords``); match on the prefix
    # before the first ``:`` so it lines up with the backlog keys. One
    # indexed ``ORDER BY ts DESC LIMIT 1`` per pass — the
    # ``(pass, ts DESC)`` index terminates early since these passes fire
    # constantly — bounded to 6h so a *stalled* pass simply shows no
    # timestamp rather than scanning all of history. Failure here is
    # non-fatal — counts already rendered; we just skip the timestamp.
    for pass_name in rows:
        try:
            with store.pool.connection() as conn:
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
            log.exception("status: backlog last-done query for %s failed", pass_name)

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

    NB the chunk-level handlers all log under ``pass='runner'`` (the
    runner's own logger; ``pass`` is the logger name, not the handler),
    so we constrain on ``pass='runner'`` to ride the ``(pass, ts)``
    index and recover the real pass name from ``payload->>'handler'``
    (``embed:bge-m3`` → ``embed``). Bounded to 6h so the index walk
    terminates fast.
    """
    with store.pool.connection() as conn:
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
    ("Chunk extracted", "SELECT max(created_at) FROM chunks", None),
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


def _liveness(store: Any) -> list[dict[str, Any]]:
    """End-to-end freshness: last activity per pipeline stage + watch.

    Answers "is it alive?" at a glance — when did the corpus last take
    in a paper, extract / index / summarise a chunk, ingest news, dream,
    or deliver the briefing. Each signal is read independently so one
    schema surprise degrades *that row* to "unknown" rather than
    dropping the whole panel (mirrors :func:`_backlog_counts`). Only the
    scheduled-cadence signals (news / briefing) flag stale; the pipeline
    stages are informational since idle is normal on a quiet corpus.
    """
    out: list[dict[str, Any]] = []
    for label, sql, stale_after_s in _LIVENESS_SIGNALS:
        try:
            with store.pool.connection() as conn:
                row = conn.execute(sql).fetchone()
            ts = row[0] if row else None
        except Exception:
            log.exception("status: liveness query for %s failed", label)
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
        age = _age_seconds(ts)
        stale = stale_after_s is not None and (age is None or age > stale_after_s)
        out.append(
            {
                "label": label,
                "ago": _ago(ts) if ts is not None else "never",
                "stale": stale,
                "scheduled": stale_after_s is not None,
                "unknown": False,
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
            SELECT host,
                   COALESCE(payload->>'handler', pass) AS handler,
                   sum(COALESCE((payload->>'failed')::int, 0))::int AS failed,
                   max(ts) AS last_ts
              FROM worker_logs
             WHERE ts > now() - interval '24 hours'
               AND COALESCE((payload->>'failed')::int, 0) > 0
               -- The schedule pass overloads BatchResult.failed to mean
               -- "ticks *skipped* this pass" (collision-skip when the
               -- previous spawned child is still open), not "errors". A
               -- single recurring wedged behind an open child inflates
               -- this to tens of thousands/day — pure noise here. The real
               -- condition (a stalled recurring) surfaces as a nursery
               -- 'stalled-recurring' alert, so drop the handler from this
               -- error panel rather than cry wolf.
               AND COALESCE(payload->>'handler', '') <> 'schedule'
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
            "liveness": _safe(lambda: _liveness(store)) or [],
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
