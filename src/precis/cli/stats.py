"""``precis stats`` — quick observability summaries.

Summaries, picked via flags:

* ``--findings`` (default) — counts of findings per ``STATUS:``
  value. Surfaces "are my chases progressing?" at a glance:
  ``tracing`` rows are in-flight, ``established`` rows are done,
  ``multi_candidate`` / ``dead_chain`` rows want operator
  attention.
* ``--stubs`` — count of stub paper refs (``pdf_sha256 IS NULL``)
  outstanding. Complements ``precis stubs`` (which lists the
  backlog row-by-row); this command answers "how big is the
  backlog?" without dumping it.
* ``--argument`` — the argument graph argument-graph corpus report (build
  order step 5): inferences resting on a retracted/concerned source
  (``STALE:retracted-premise``), inferences carrying an inherited,
  unaddressed caveat, and open ``contradicts`` edges between
  argument-graph nodes. Exhaustive by construction (a SQL walk over
  ``links``/``ref_tags``, not an LLM scan) — see
  ``docs/decisions/0054-argument-graph-lemmas-inferences-reasoning-shadow.md``.
* ``--utilization`` — "was the cluster always hot?" over the trailing
  ``--hours`` window (default 24): per-host hourly CPU load/temp from
  ``host_heartbeat_log`` (migration 0113), per-hour LLM duty cycle from
  ``llm_call_log`` (busy % of wall-clock; >100 means concurrent
  calls), and any LLM silence longer than five minutes.

Flags can be combined to print several sections; default (no flags)
prints all of them.

Sibling commands:

* ``precis stubs`` — row-level stub listing.
* ``precis worker --only chase`` / ``--only fetch`` — drive the
  workers that empty each backlog.
* ``get(kind='memory', id=<inference>, view='argument')`` — the
  per-inference proof-tree read (this command is the corpus-wide
  complement).
"""

from __future__ import annotations

import argparse
from typing import Any

from precis.cli._common import (
    add_format_argument,
    resolve_dsn,
    resolve_format,
)
from precis.format import serialize
from precis.store import Store

# Pinned column order for each section. Adding a column lands in
# one place so TOON / JSON / table all stay in sync.
_FINDINGS_SCHEMA: list[str] = ["status", "count"]
_STUBS_SCHEMA: list[str] = ["state", "count"]
_ARGUMENT_STALE_SCHEMA: list[str] = ["id", "title"]
_ARGUMENT_CAVEATS_SCHEMA: list[str] = ["id", "title"]
_ARGUMENT_CONTRADICTIONS_SCHEMA: list[str] = ["a_id", "a_title", "b_id", "b_title"]
_CPU_UTILIZATION_SCHEMA: list[str] = [
    "hr",
    "host",
    "beats",
    "load1_avg",
    "load1_max",
    "temp_max",
]
_LLM_UTILIZATION_SCHEMA: list[str] = ["hr", "calls", "busy_pct", "cost_usd", "errors"]
_LLM_GAPS_SCHEMA: list[str] = ["gap_ended_at", "gap_minutes"]

#: An LLM silence shorter than this is normal scheduling jitter, not idleness.
_LLM_GAP_MINUTES = 5.0


def add_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "stats",
        help="Summarise finding-status counts, stub backlog, argument graph.",
        description=(
            "Surface quick observability summaries. ``--findings`` "
            "shows counts per STATUS: value (tracing / established / "
            "multi_candidate / dead_chain). ``--stubs`` shows the "
            "stub paper backlog (PDFs the chase worker wants but "
            "doesn't have yet). ``--argument`` shows the argument graph "
            "argument-graph corpus report (retracted-source ripple, "
            "unaddressed caveats, open contradictions). Default: print "
            "all sections."
        ),
    )
    p.add_argument(
        "--findings",
        action="store_true",
        help="Show STATUS-count summary for kind='finding'.",
    )
    p.add_argument(
        "--stubs",
        action="store_true",
        help="Show count of stub paper refs (pdf_sha256 IS NULL).",
    )
    p.add_argument(
        "--argument",
        action="store_true",
        help="Show the argument-graph corpus report.",
    )
    p.add_argument(
        "--utilization",
        action="store_true",
        help="Show hourly CPU (host_heartbeat_log) + LLM (llm_call_log) "
        "utilization and LLM idle gaps.",
    )
    p.add_argument(
        "--hours",
        type=float,
        default=24.0,
        help="Trailing window for --utilization (default 24).",
    )
    p.add_argument(
        "--database-url",
        default=None,
        help="Override PRECIS_DATABASE_URL.",
    )
    add_format_argument(p)


def run(args: argparse.Namespace) -> None:
    # No flags = print everything. A flag toggles inclusion of just
    # that section so the operator can pipe one summary to a
    # downstream filter without the others muddying it up.
    any_flag = args.findings or args.stubs or args.argument or args.utilization
    show_findings = args.findings or not any_flag
    show_stubs = args.stubs or not any_flag
    show_argument = args.argument or not any_flag
    show_utilization = args.utilization or not any_flag

    dsn = resolve_dsn(args.database_url)
    fmt = resolve_format(args)

    sections: list[tuple[str, list[str], list[dict[str, Any]]]] = []
    store = Store.connect(dsn)
    try:
        if show_findings:
            sections.append(("findings", _FINDINGS_SCHEMA, _query_findings(store)))
        if show_stubs:
            sections.append(("stubs", _STUBS_SCHEMA, _query_stubs(store)))
        if show_argument:
            sections.append(
                (
                    "argument-stale-premise",
                    _ARGUMENT_STALE_SCHEMA,
                    _query_argument_stale(store),
                )
            )
            sections.append(
                (
                    "argument-unaddressed-caveats",
                    _ARGUMENT_CAVEATS_SCHEMA,
                    _query_argument_caveats(store),
                )
            )
            sections.append(
                (
                    "argument-open-contradictions",
                    _ARGUMENT_CONTRADICTIONS_SCHEMA,
                    _query_argument_contradictions(store),
                )
            )
        if show_utilization:
            sections.append(
                (
                    "cpu-utilization-by-hour",
                    _CPU_UTILIZATION_SCHEMA,
                    _query_cpu_utilization(store, args.hours),
                )
            )
            sections.append(
                (
                    "llm-utilization-by-hour",
                    _LLM_UTILIZATION_SCHEMA,
                    _query_llm_utilization(store, args.hours),
                )
            )
            sections.append(
                ("llm-gaps", _LLM_GAPS_SCHEMA, _query_llm_gaps(store, args.hours))
            )
    finally:
        store.close()

    if fmt == "json":
        # Single JSON object keyed by section name so callers piping
        # to ``jq`` can pick one half: ``precis stats --format json
        # | jq .findings``.
        import json

        payload = {name: rows for name, _, rows in sections}
        print(json.dumps(payload, indent=2))
        return

    for i, (name, schema, rows) in enumerate(sections):
        if i:
            print()
        print(f"# {name}")
        if not rows:
            print("(empty)")
            continue
        print(serialize(rows, schema=schema, format=fmt))


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def _query_findings(store: Store) -> list[dict[str, Any]]:
    """STATUS-count summary for ``kind='finding'``.

    Rows whose STATUS tag is absent surface under
    ``status='(none)'`` so the count is exhaustive of the
    ``finding`` corpus (deleted refs excluded).
    """
    sql = (
        "WITH rows AS ("
        "  SELECT r.ref_id, "
        "         COALESCE(t.value, '(none)') AS status "
        "  FROM refs r "
        "  LEFT JOIN ref_tags rt ON rt.ref_id = r.ref_id "
        "  LEFT JOIN tags t ON t.tag_id = rt.tag_id "
        "     AND t.namespace = 'STATUS' "
        "  WHERE r.kind = 'finding' AND r.retired_at IS NULL"
        ") "
        "SELECT status, count(*)::int AS count "
        "FROM rows "
        "GROUP BY status "
        "ORDER BY count DESC, status ASC"
    )
    with store.pool.connection() as conn:
        cur = conn.execute(sql)
        return [{"status": r[0], "count": int(r[1])} for r in cur.fetchall()]


def _query_stubs(store: Store) -> list[dict[str, Any]]:
    """Stub backlog summary.

    Two states surface:

    * ``awaiting`` — stub created, never fetched (no
      ``ref_events.source LIKE 'fetcher:%'`` row).
    * ``retry`` — stub was attempted at least once and still has
      no PDF; ripe for the next fetch pass.

    A stub that has a PDF (``pdf_sha256 IS NOT NULL``) is no
    longer a stub and falls out of the count.
    """
    sql = (
        "SELECT CASE "
        "         WHEN last_event.source IS NULL THEN 'awaiting' "
        "         ELSE 'retry' "
        "       END AS state, "
        "       count(*)::int AS count "
        "FROM refs r "
        "LEFT JOIN LATERAL ( "
        "  SELECT source FROM ref_events "
        "  WHERE ref_id = r.ref_id AND source LIKE 'fetcher:%' "
        "  ORDER BY ts DESC LIMIT 1 "
        ") last_event ON TRUE "
        "WHERE r.kind = 'paper' "
        "  AND r.pdf_sha256 IS NULL "
        "  AND r.retired_at IS NULL "
        "GROUP BY state "
        "ORDER BY state ASC"
    )
    with store.pool.connection() as conn:
        cur = conn.execute(sql)
        return [{"state": r[0], "count": int(r[1])} for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Argument-graph corpus report (build order step 5)
# ---------------------------------------------------------------------------


#: A ref alias is a walkable argument-graph node when its kind is
#: ``finding`` (no sub-kind tag needed) or it's a ``memory`` carrying the
#: open tag ``kind:lemma`` / ``kind:inference``. Mirrors the kind-scoping
#: in ``precis.store._argument_ops`` / ``precis.handlers._argument_view``.
def _walkable_node_clause(alias: str) -> str:
    return (
        f"({alias}.kind = 'finding' OR ({alias}.kind = 'memory' AND EXISTS ("
        f"  SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id "
        f"  WHERE rt.ref_id = {alias}.ref_id AND t.namespace = 'OPEN' "
        f"    AND t.value IN ('kind:lemma', 'kind:inference'))))"
    )


def _query_argument_stale(store: Store) -> list[dict[str, Any]]:
    """Inferences currently tagged ``STALE:retracted-premise``.

    The tag is maintained live by the retraction-ripple link-write hook
    (:meth:`precis.store._argument_ops.ArgumentGraphMixin.argument_ripple_retraction`)
    on every ``retracts`` / ``raises-concern-about`` edge add or remove, so
    this is a direct read of already-current state, not a fresh walk.
    """
    sql = (
        "SELECT r.ref_id, r.title FROM refs r "
        "JOIN ref_tags rt ON rt.ref_id = r.ref_id "
        "JOIN tags t ON t.tag_id = rt.tag_id "
        "WHERE r.kind = 'memory' AND r.retired_at IS NULL "
        "  AND t.namespace = 'STALE' AND t.value = 'retracted-premise' "
        "ORDER BY r.ref_id"
    )
    with store.pool.connection() as conn:
        rows = conn.execute(sql).fetchall()
    return [{"id": int(r[0]), "title": r[1] or ""} for r in rows]


def _query_argument_caveats(store: Store) -> list[dict[str, Any]]:
    """Inferences with a premise reachable via an inbound ``qualifies``
    (caveat → premise) edge.

    Every caveat is "unaddressed" in v1 — edge-scoped discharge
    (``meta.addresses``) is phase 2, so there is no
    "addressed here" bucket to subtract yet; this list is exhaustive of
    every inference carrying an inherited caveat.
    """
    sql = (
        "SELECT DISTINCT inf.ref_id, inf.title FROM refs inf "
        "JOIN links dl ON dl.src_ref_id = inf.ref_id AND dl.relation = 'derived-from' "
        "JOIN links ql ON ql.dst_ref_id = dl.dst_ref_id AND ql.relation = 'qualifies' "
        "WHERE inf.kind = 'memory' AND inf.retired_at IS NULL "
        "  AND EXISTS (SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id "
        "              WHERE rt.ref_id = inf.ref_id AND t.namespace = 'OPEN' "
        "                AND t.value = 'kind:inference') "
        "ORDER BY inf.ref_id"
    )
    with store.pool.connection() as conn:
        rows = conn.execute(sql).fetchall()
    return [{"id": int(r[0]), "title": r[1] or ""} for r in rows]


# ---------------------------------------------------------------------------
# Utilization (``utilization-log`` (git-only))
# ---------------------------------------------------------------------------


def _query_cpu_utilization(store: Store, hours: float) -> list[dict[str, Any]]:
    """Per-host hourly load/temp rollup from ``host_heartbeat_log``.

    Empty until the fleet's heartbeat pass has run with migration 0113
    applied — the table fills at one row per host per minute from then on.
    """
    rows = store.heartbeat_history(hours=hours)
    return [
        {
            "hr": r["hr"].isoformat(sep=" ", timespec="minutes"),
            "host": r["host"],
            "beats": r["beats"],
            "load1_avg": round(r["load1_avg"], 2)
            if r["load1_avg"] is not None
            else None,
            "load1_max": round(r["load1_max"], 2)
            if r["load1_max"] is not None
            else None,
            "temp_max": round(r["temp_max"], 1) if r["temp_max"] is not None else None,
        }
        for r in rows
    ]


def _query_llm_utilization(store: Store, hours: float) -> list[dict[str, Any]]:
    """Per-hour LLM duty cycle from ``llm_call_log``.

    ``busy_pct`` is summed ``duration_ms`` as a percentage of the hour's
    wall-clock — values over 100 mean concurrent calls (the fleet runs
    several models in parallel), which is exactly the "how hot" signal.
    """
    sql = (
        "SELECT date_trunc('hour', ts) AS hr, "
        "       count(*)::int AS calls, "
        "       sum(duration_ms) / 36000.0 AS busy_pct, "
        "       sum(cost_usd) AS cost_usd, "
        "       count(*) FILTER (WHERE errored)::int AS errors "
        "FROM llm_call_log "
        "WHERE ts > now() - (%s || ' hours')::interval "
        "GROUP BY 1 ORDER BY 1"
    )
    with store.pool.connection() as conn:
        rows = conn.execute(sql, (str(hours),)).fetchall()
    return [
        {
            "hr": r[0].isoformat(sep=" ", timespec="minutes"),
            "calls": int(r[1]),
            "busy_pct": round(float(r[2]), 1) if r[2] is not None else None,
            "cost_usd": round(float(r[3]), 3) if r[3] is not None else None,
            "errors": int(r[4]),
        }
        for r in rows
    ]


def _query_llm_gaps(store: Store, hours: float) -> list[dict[str, Any]]:
    """LLM silences longer than :data:`_LLM_GAP_MINUTES` in the window —
    the direct "how was the cluster not always hot" evidence (empty means
    the LLM lane never went cold)."""
    sql = (
        "WITH gaps AS ( "
        "  SELECT ts, ts - lag(ts) OVER (ORDER BY ts) AS gap "
        "  FROM llm_call_log "
        "  WHERE ts > now() - (%s || ' hours')::interval "
        ") "
        "SELECT ts, EXTRACT(EPOCH FROM gap) / 60.0 AS gap_minutes "
        "FROM gaps "
        "WHERE gap > (%s || ' minutes')::interval "
        "ORDER BY gap DESC LIMIT 20"
    )
    with store.pool.connection() as conn:
        rows = conn.execute(sql, (str(hours), str(_LLM_GAP_MINUTES))).fetchall()
    return [
        {
            "gap_ended_at": r[0].isoformat(sep=" ", timespec="seconds"),
            "gap_minutes": round(float(r[1]), 1),
        }
        for r in rows
    ]


def _query_argument_contradictions(store: Store) -> list[dict[str, Any]]:
    """Open ``contradicts`` edges where both endpoints are argument-graph
    nodes (finding / kind:lemma / kind:inference) — the surface the
    persona-threads blackboard *pulls* (the argument graph exposes it, adds
    no hook). One row per edge; ``a`` is the ``contradicts`` source.
    """
    sql = (
        "SELECT a.ref_id, a.title, b.ref_id, b.title "
        "FROM links l "
        "JOIN refs a ON a.ref_id = l.src_ref_id AND a.retired_at IS NULL "
        "JOIN refs b ON b.ref_id = l.dst_ref_id AND b.retired_at IS NULL "
        "WHERE l.relation = 'contradicts' "
        f"  AND {_walkable_node_clause('a')} "
        f"  AND {_walkable_node_clause('b')} "
        "ORDER BY a.ref_id"
    )
    with store.pool.connection() as conn:
        rows = conn.execute(sql).fetchall()
    return [
        {
            "a_id": int(r[0]),
            "a_title": r[1] or "",
            "b_id": int(r[2]),
            "b_title": r[3] or "",
        }
        for r in rows
    ]


__all__ = ["add_parser", "run"]
