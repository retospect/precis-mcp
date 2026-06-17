"""``precis logs`` — query the centralised worker_logs table.

Operator surface for "what's the cluster doing right now?" Reads
the table populated by :class:`precis.utils.db_log_handler.
BufferedDBLogHandler` (migration 0015). Lets the operator filter by
host, process, pass, level, or free-form time window — without
ssh-ing to each box and grepping `/var/log/precis-*.log`.

Examples:

    precis logs --since 1h                       # last hour, all hosts
    precis logs --since 1d --host caspar         # one host
    precis logs --pass dispatch --level WARNING  # rejections in dispatch
    precis logs --pass structural --since 7d     # weekly review activity
    precis logs --tail                           # last 50 lines, all hosts

The file handler at ``/var/log/precis-*.log`` stays in place as
the bootstrap + fallback channel — use it for live ``tail`` when
the DB is itself the problem, or when you need every byte of a
traceback the table truncated.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any

from precis.cli._common import (
    add_format_argument,
    resolve_dsn,
    resolve_format,
)
from precis.format import render_agent_table
from precis.store import Store

# ── parser ────────────────────────────────────────────────────────


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "logs",
        help="Read the centralised worker_logs table",
        description=(
            "Query the centralised worker_logs table populated by "
            "the BufferedDBLogHandler (migration 0015). Use --since "
            "to bound the window; --host / --process / --pass / "
            "--level to narrow further; --tail to print the most "
            "recent N rows regardless of time. The text file at "
            "/var/log/precis-*.log stays as the bootstrap + fallback "
            "channel — use that when the DB is the problem."
        ),
    )
    p.add_argument(
        "--since",
        default=None,
        help=(
            "Window for the query. Accepts '1h', '15m', '7d', '1w', or "
            "an ISO timestamp ('2026-06-14T12:00:00+00:00'). Default: "
            "1 hour."
        ),
    )
    p.add_argument(
        "--host",
        default=None,
        help="Filter to a specific host (e.g. 'melchior', 'caspar').",
    )
    p.add_argument(
        "--process",
        default=None,
        help=(
            "Filter to a specific process "
            "('precis-worker', 'precis-worker-agent', etc.)."
        ),
    )
    p.add_argument(
        "--pass",
        dest="pass_name",  # 'pass' is a python keyword
        default=None,
        help=("Filter to a specific pass ('dispatch', 'structural', 'schedule', ...)."),
    )
    p.add_argument(
        "--level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default=None,
        help=(
            "Filter to rows at or above this level. WARNING + ERROR "
            "have a partial index and are the cheapest to query."
        ),
    )
    p.add_argument(
        "--limit",
        type=int,
        default=200,
        help="Cap the result set (default 200). Newest rows first.",
    )
    p.add_argument(
        "--tail",
        action="store_true",
        help=(
            "Shorthand for --since=24h --limit=50 — the most recent 50 "
            "log lines from the last day."
        ),
    )
    p.add_argument(
        "--payload",
        action="store_true",
        help=(
            "Include the JSONB payload column in the output. Off by "
            "default since the field can be large."
        ),
    )
    p.add_argument(
        "--database-url",
        default=None,
        help="Override PRECIS_DATABASE_URL.",
    )
    add_format_argument(p)
    p.set_defaults(func=run)


# ── ``--since`` parsing ──────────────────────────────────────────


_DURATION_RE = re.compile(r"^(\d+)([smhdw])$", re.IGNORECASE)
_DURATION_UNITS = {
    "s": "seconds",
    "m": "minutes",
    "h": "hours",
    "d": "days",
    "w": "weeks",
}


def _parse_since(since: str | None) -> str:
    """Translate ``--since`` into a SQL interval expression.

    Returns the right-hand side of ``ts > now() - <interval>``. We
    return the interval literal as a string and let psycopg
    parameterise it, so a malicious ``since`` value can't escape.
    """
    if not since:
        return "1 hour"
    m = _DURATION_RE.match(since.strip())
    if m:
        n, unit = m.group(1), m.group(2).lower()
        return f"{int(n)} {_DURATION_UNITS[unit]}"
    # Bare ISO timestamps fall through here — the caller will pass
    # the original string and we'll WHERE ts > %s::timestamptz
    # instead. Signalled by returning ``""`` so the SQL builder
    # picks the right branch.
    return ""


# ── runner ───────────────────────────────────────────────────────


def run(args: argparse.Namespace) -> None:
    if args.tail:
        since = "24 hours"
        limit = min(args.limit, 50)
    else:
        since = _parse_since(args.since)
        limit = args.limit

    dsn = resolve_dsn(args.database_url)
    store = Store.connect(dsn)
    try:
        rows = _query_logs(
            store,
            since_interval=since,
            since_iso=args.since if (since == "" and args.since) else None,
            host=args.host,
            process=args.process,
            pass_name=args.pass_name,
            level=args.level,
            limit=limit,
        )
        if not rows:
            print("no rows", file=sys.stderr)
            return
        _render(rows, format=resolve_format(args), with_payload=args.payload)
    finally:
        store.close()


def _query_logs(
    store: Store,
    *,
    since_interval: str,
    since_iso: str | None,
    host: str | None,
    process: str | None,
    pass_name: str | None,
    level: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Run the WHERE-chained SELECT and return one dict per row."""
    where: list[str] = []
    params: list[Any] = []
    if since_iso:
        where.append("ts > %s::timestamptz")
        params.append(since_iso)
    elif since_interval:
        where.append("ts > now() - %s::interval")
        params.append(since_interval)
    if host:
        where.append("host = %s")
        params.append(host)
    if process:
        where.append("process = %s")
        params.append(process)
    if pass_name:
        where.append("pass = %s")
        params.append(pass_name)
    if level:
        # Level filter is at-or-above by stdlib ordering, which the
        # operator's mental model expects. WARNING is above INFO,
        # ERROR is above WARNING. SQL CASE turns each level string
        # into a comparable rank.
        rank = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3}.get(level, 1)
        where.append(
            "(CASE level WHEN 'DEBUG' THEN 0 WHEN 'INFO' THEN 1 "
            "WHEN 'WARNING' THEN 2 WHEN 'ERROR' THEN 3 ELSE 0 END) "
            ">= %s"
        )
        params.append(rank)

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    sql = (
        "SELECT ts, host, process, pass, level, logger, message, payload "
        "FROM worker_logs"
        f"{where_sql} "
        "ORDER BY ts DESC LIMIT %s"
    )
    params.append(limit)
    with store.pool.connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [
        {
            "ts": r[0].isoformat(timespec="seconds"),
            "host": r[1],
            "process": r[2] or "",
            "pass": r[3] or "",
            "level": r[4],
            "logger": r[5] or "",
            "message": r[6],
            "payload": dict(r[7]) if r[7] else None,
        }
        for r in rows
    ]


def _render(rows: list[dict[str, Any]], *, format: str, with_payload: bool) -> None:
    schema = ["ts", "host", "pass", "level", "logger", "message"]
    if with_payload:
        schema.append("payload")
    if format == "json":
        # JSON output keeps payload as a dict; everything else as
        # string. Useful for jq pipelines.
        print(
            json.dumps(
                [
                    {
                        k: (v if k == "payload" else (v or ""))
                        for k, v in row.items()
                        if k in schema or k == "payload"
                    }
                    for row in rows
                ],
                indent=2,
                default=str,
            )
        )
        return
    table_rows = [
        {
            "ts": row["ts"],
            "host": row["host"],
            "pass": row["pass"],
            "level": row["level"],
            "logger": row["logger"],
            "message": row["message"][:160]
            + ("…" if len(row["message"]) > 160 else ""),
            **(
                {"payload": json.dumps(row["payload"], default=str)[:200]}
                if with_payload and row["payload"]
                else {}
            ),
        }
        for row in rows
    ]
    print(render_agent_table(table_rows, schema=schema))


__all__ = ["add_parser", "run"]
