"""Read-only JSONL export of search evidence and weak/explicit feedback.

Rendered-but-unfollowed is a weak negative, not proof of irrelevance.
Unrendered candidates stay unlabeled. Explicit trace misses are retained even
for empty searches; graph misses name the returned handles they concern.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Generator
from datetime import datetime
from typing import Any

import psycopg
from psycopg import Connection

from precis.cli._common import resolve_dsn
from precis.utils import handle_registry


def _since(value: str) -> str:
    match = re.fullmatch(r"([1-9][0-9]*)([mhdw])", value)
    if match is None:
        raise argparse.ArgumentTypeError("use a positive duration, e.g. 90d or 24h")
    units = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
    return f"{match[1]} {units[match[2]]}"


def add_parser(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser(
        "traces", help="Export search traces and feedback (read-only)."
    )
    verbs = parser.add_subparsers(dest="traces_cmd", required=True)
    export = verbs.add_parser("export", help="Stream search traces as JSONL.")
    export.add_argument("--jsonl", action="store_true", required=True)
    export.add_argument("--since", type=_since, default="90d")
    export.add_argument("--kind", default=None)
    export.add_argument("--database-url", default=None)


def _graph_misses(conn: Connection, metas: list[dict[str, Any]]) -> dict[str, datetime]:
    """Latest qualifying evidence per handle, compared to each trace's time."""
    handles = {
        c["handle"]
        for meta in metas
        for c in meta.get("candidates", [])
        if c.get("rendered")
    }
    refs: dict[str, int] = {}
    chunks: dict[str, int] = {}
    for handle in handles:
        parsed = handle_registry.parse(handle)
        if parsed is not None:
            _kind, chunk, ident = parsed
            (chunks if chunk else refs)[handle] = ident
    if chunks:
        owners: dict[int, int] = dict(
            conn.execute(
                "SELECT chunk_id, ref_id FROM chunks WHERE chunk_id = ANY(%s)",
                (list(chunks.values()),),
            ).fetchall()
        )
        refs.update({h: owners[c] for h, c in chunks.items() if c in owners})
    if not refs:
        return {}
    rows = conn.execute(
        "SELECT l.dst_ref_id, max(CASE "
        "WHEN l.relation = 'raises-concern-about' THEN l.created_at "
        "ELSE source.created_at END) FROM links l "
        "JOIN refs source ON source.ref_id = l.src_ref_id "
        "WHERE l.dst_ref_id = ANY(%s) AND source.retired_at IS NULL AND ("
        "l.relation = 'raises-concern-about' OR "
        "(source.kind = 'todo' AND EXISTS ("
        "SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id "
        "WHERE rt.ref_id = source.ref_id AND t.namespace = 'OPEN' "
        "AND t.value = 'memory-review'))) GROUP BY l.dst_ref_id",
        (sorted(set(refs.values())),),
    ).fetchall()
    missed_at: dict[int, datetime] = dict(rows)
    return {h: missed_at[ident] for h, ident in refs.items() if ident in missed_at}


def export_rows(
    conn: Connection, *, since: str, kind: str | None = None
) -> Generator[dict[str, Any], None, None]:
    """Stream a snapshot in bounded batches; enforce read-only at the DB."""
    with conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        with conn.cursor(name="search_trace_export") as cursor:
            cursor.execute(
                "SELECT r.ref_id, r.meta, EXISTS("
                "SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id "
                "WHERE rt.ref_id = r.ref_id AND t.namespace = 'OPEN' "
                "AND t.value = 'trace:miss'), r.created_at "
                "FROM refs r WHERE r.kind = 'agentlog' "
                "AND r.retired_at IS NULL AND r.meta->>'trace' = 'search' "
                "AND r.created_at >= now() - %s::interval "
                "AND (%s::text IS NULL OR r.meta->'kinds' ? %s) "
                "ORDER BY r.created_at, r.ref_id",
                (since, kind, kind),
            )
            while batch := cursor.fetchmany(500):
                graph_misses = _graph_misses(conn, [r[1] for r in batch])
                for ident, meta, explicit_miss, created_at in batch:
                    candidates = meta.get("candidates", [])
                    handles = {c["handle"] for c in candidates}
                    rendered = {c["handle"] for c in candidates if c.get("rendered")}
                    followed = set(meta.get("followed", [])) & handles
                    used = set(meta.get("used", [])) & handles
                    yield {
                        "id": f"ag{ident}",
                        "q": meta.get("q"),
                        "kind": meta.get("kinds", []),
                        "mode": meta.get("mode"),
                        "candidates": candidates,
                        "rendered": sorted(rendered),
                        "followed": sorted(followed),
                        "used": sorted(used),
                        "positives": sorted(used | followed),
                        "negatives": sorted(rendered - followed - used),
                        "misses": sorted(
                            {
                                h
                                for h in rendered
                                if h in graph_misses and graph_misses[h] >= created_at
                            }
                            | (rendered if explicit_miss else set())
                        ),
                        "trace_miss": explicit_miss,
                    }


def run(args: argparse.Namespace) -> None:
    with psycopg.connect(resolve_dsn(args.database_url), connect_timeout=15) as conn:
        for row in export_rows(conn, since=args.since, kind=args.kind):
            print(json.dumps(row))
