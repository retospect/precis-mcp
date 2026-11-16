"""Weekly factory numbers from stored records and an optional main checkout.

Activity uses UTC half-open windows. Job state is current state of jobs
created in that window; releases and land outcomes use terminal-tag time.
Git reads only the configured checkout's main ref, never its working branch.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

Rows = dict[str, list[dict[str, Any]]]


def bound(value: str | datetime) -> datetime:
    """Parse a UTC report bound, accepting ISO dates and timestamps."""
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    return (
        result.replace(tzinfo=UTC) if result.tzinfo is None else result.astimezone(UTC)
    )


def git_rows(since: datetime, until: datetime) -> list[dict[str, Any]]:
    """Count main commits by type/scope; retain distinct mentioned gripe handles."""
    checkout = os.environ.get("PRECIS_WEEKLY_REPORT_CHECKOUT")
    if not checkout:
        return [{"note": "git unavailable"}]
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                checkout,
                "log",
                "main",
                f"--since-as-filter={since.isoformat()}",
                f"--until={until.isoformat()}",
                "--format=%ct%x00%s%x00%b%x1e",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return [{"note": "git unavailable"}]
    counts: Counter[tuple[str, str]] = Counter()
    gripes: dict[tuple[str, str], set[str]] = {}
    for record in result.stdout.split("\x1e"):
        parts = record.strip().split("\x00", 2)
        if (
            len(parts) != 3
            or not since.timestamp() <= int(parts[0]) < until.timestamp()
        ):
            continue
        match = re.match(r"([a-z]+)(?:\(([^)]+)\))?!?:", parts[1])
        key = (match[1], match[2] or "(none)") if match else ("other", "(none)")
        counts[key] += 1
        gripes.setdefault(key, set()).update(
            re.findall(r"\bgr\d+\b", " ".join(parts[1:]))
        )
    return [
        {
            "type": k[0],
            "scope": k[1],
            "commits": n,
            "gripes": ", ".join(sorted(gripes[k])),
        }
        for k, n in sorted(counts.items())
    ]


def collect(store: Any, since: str | datetime, until: str | datetime) -> Rows:
    """Collect named row lists without generating prose or mutating records."""
    lo, hi = bound(since), bound(until)
    if lo >= hi:
        raise ValueError("since must be before until")
    data: Rows = {}
    # One status row per ref prevents tag joins multiplying the counts.
    common = """
        WITH status AS (
          SELECT DISTINCT ON (rt.ref_id) rt.ref_id, t.value, rt.created_at
          FROM ref_tags rt JOIN tags t USING (tag_id)
          WHERE t.namespace = 'STATUS'
            AND (rt.expires_at IS NULL OR rt.expires_at > now())
          ORDER BY rt.ref_id, rt.created_at DESC, t.value
        ), live AS (
          SELECT r.*, s.value AS state, s.created_at AS state_at
          FROM refs r LEFT JOIN status s USING (ref_id)
          WHERE r.retired_at IS NULL
        )
    """

    def query(name: str, columns: str, sql: str, params: tuple[Any, ...] = ()) -> None:
        with store.pool.connection() as conn:
            rows = conn.execute(common + sql, params).fetchall()
        data[name] = [dict(zip(columns.split(), row, strict=True)) for row in rows]

    # GPU compute and catalyst-engine jobs are simulations, including CPU
    # aggregation. Pourbaix is a deterministic chemistry job without a GPU.
    from precis.workers.registry import SERVICES, ServiceKind

    sims = [
        s.name
        for s in SERVICES
        if s.kind == ServiceKind.COMPUTE or "autocatpath" in s.placement
    ] + ["pourbaix_bulk"]
    query(
        "Jobs",
        "class job_type state count",
        """
        SELECT CASE WHEN meta->>'job_type' = ANY(%s) THEN 'simulation'
               ELSE 'infrastructure' END, meta->>'job_type', coalesce(state, 'unknown'), count(*)
        FROM live WHERE kind = 'job' AND created_at >= %s AND created_at < %s
        GROUP BY 1,2,3 ORDER BY 1,2,3
    """,
        (sims, lo, hi),
    )
    from precis.reports.activity import flow_buckets

    with store.pool.connection() as conn:
        opens, closes = flow_buckets(
            conn,
            bucket="week",
            where="r.kind = 'gripe' AND r.retired_at IS NULL",
            params=[],
            closed=["done", "wontfix"],
            opens_window=" AND r.created_at >= %s AND r.created_at < %s",
            closes_window=" AND rt.created_at >= %s AND rt.created_at < %s",
            window_params=[lo, hi],
        )
    data["Gripes filed and closed"] = [
        {"event": "filed", "count": sum(n for _, n in opens)},
        {"event": "closed", "count": sum(n for _, n in closes)},
    ]
    query(
        "Gripes by origin and goal",
        "event axis value count",
        """
        SELECT event, t.namespace, t.value, count(DISTINCT r.ref_id)
        FROM live r JOIN ref_tags rt USING (ref_id) JOIN tags t USING (tag_id)
        CROSS JOIN (VALUES ('filed'), ('closed')) AS events(event)
        WHERE r.kind = 'gripe' AND t.namespace IN ('origin-class','goal')
          AND (rt.expires_at IS NULL OR rt.expires_at > now())
          AND ((event = 'filed' AND r.created_at >= %s AND r.created_at < %s)
            OR (event = 'closed' AND state IN ('done','wontfix')
                AND state_at >= %s AND state_at < %s))
        GROUP BY 1,2,3 ORDER BY 1,2,3
    """,
        (lo, hi, lo, hi),
    )
    if not data["Gripes by origin and goal"]:
        data["Gripes by origin and goal"] = [{"note": "origin/goal not stamped yet"}]
    query(
        "Releases",
        "job event release sha date_UTC",
        """
        SELECT ref_id, meta->>'job_type',
          coalesce(meta#>>'{release_cut,rN}', meta#>>'{release_confirm,rN}'),
          coalesce(meta#>>'{release_cut,sha}', meta#>>'{release_confirm,sha}'), state_at
        FROM live WHERE kind = 'job' AND meta->>'job_type' IN ('release_cut','release_confirm')
          AND state = 'succeeded' AND state_at >= %s AND state_at < %s
        ORDER BY state_at, ref_id
    """,
        (lo, hi),
    )
    query(
        "Land slice success",
        "succeeded terminal success_percent",
        """
        SELECT count(*) FILTER (WHERE state = 'succeeded'), count(*),
          coalesce(round(100.0 * count(*) FILTER (WHERE state = 'succeeded')
            / nullif(count(*),0), 2), 0)
        FROM live WHERE kind = 'job' AND meta->>'job_type' = 'land_slice'
          AND state IN ('succeeded','failed','cancelled','timed-out')
          AND state_at >= %s AND state_at < %s
    """,
        (lo, hi),
    )
    query(
        "Active quests",
        "quest ticks simulations",
        """
        SELECT q.ref_id,
          (SELECT count(DISTINCT (j.ref_id, substring(c.text FROM '^tick #([0-9]+):')))
            FROM live j JOIN chunks c ON c.ref_id = j.ref_id
            WHERE j.kind = 'job'
            AND j.meta->>'job_type' = 'quest_tick'
            AND j.meta#>>'{params,quest_id}' = q.ref_id::text
            AND c.chunk_kind = 'job_event' AND c.retired_at IS NULL
            AND c.text ~ '^tick #[0-9]+:'
            AND c.created_at >= %s AND c.created_at < %s),
          (SELECT count(*) FROM live j WHERE j.kind = 'job'
            AND j.meta->>'job_type' = ANY(%s)
            AND j.created_at >= %s AND j.created_at < %s
            AND (j.meta#>>'{params,quest_id}' = q.ref_id::text OR EXISTS (
              SELECT 1 FROM links l WHERE l.src_ref_id = j.parent_id
                AND l.dst_ref_id = q.ref_id AND l.relation = 'serves')))
        FROM live q WHERE q.kind = 'quest' AND q.state = 'active' ORDER BY q.ref_id
    """,
        (lo, hi, sims, lo, hi),
    )
    query(
        "Open gripes",
        "gripe created_UTC age_days",
        """
        SELECT ref_id, created_at, floor(extract(epoch FROM now()-created_at)/86400)
        FROM live WHERE kind = 'gripe' AND coalesce(state,'') NOT IN ('done','wontfix')
        ORDER BY created_at, ref_id
    """,
    )
    ages = Counter({"0-1d": 0, "1-7d": 0, "7-30d": 0, "30d+": 0})
    for row in data["Open gripes"]:
        age = row["age_days"]
        ages[
            "0-1d"
            if age < 1
            else "1-7d"
            if age < 7
            else "7-30d"
            if age < 30
            else "30d+"
        ] += 1
    data["Open gripe ages (current snapshot)"] = [
        {"age": k, "count": v} for k, v in ages.items()
    ]
    data["Commits on main"] = git_rows(lo, hi)
    return data


def table_data(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Use the draft table's canonical data and shared renderer."""
    from precis.utils.table_data import normalize_table

    if not rows:
        rows = [{"count": 0}]
    columns = list(rows[0])

    def cell(value: Any) -> Any:
        if isinstance(value, datetime):
            return value.astimezone(UTC).isoformat()
        if isinstance(value, Decimal):
            return float(value)
        return value

    return normalize_table(
        {"header": columns, "rows": [[cell(row[c]) for c in columns] for row in rows]}
    )


def render_table(rows: list[dict[str, Any]]) -> str:
    """Render via the shared draft-table projection."""
    from precis.utils.table_data import table_to_markdown

    return table_to_markdown(table_data(rows))


def render_markdown(data: Rows) -> str:
    """One heading and table for every collected section."""
    return "\n\n".join(
        f"## {name}\n\n{render_table(rows)}" for name, rows in data.items()
    )


def write_tables(store: Any, draft: str, section: str, data: Rows) -> None:
    """Replace direct table children, keeping prose and nested sections intact.

    A section lock serializes retries; the outer transaction makes the draft
    store's nested writes atomic on its thread-bound connection.
    """
    ref = store.get_ref(kind="draft", id=draft)
    heading = store.drafts.get_draft_chunk(section)
    if (
        ref is None
        or heading is None
        or heading.retired
        or heading.ref_id != ref.id
        or heading.chunk_kind != "heading"
    ):
        raise ValueError("draft and section must identify a live draft heading")
    from precis.utils.table_data import table_to_markdown

    with store.atomic() as conn:
        store.drafts._lock_sections(conn, ref.id, heading.chunk_id)
        old = [
            c
            for c in store.drafts.reading_order(ref.id)
            if c.parent_chunk_id == heading.chunk_id and c.chunk_kind == "table"
        ]
        for chunk in old:
            store.drafts.retire_chunk(chunk.dc, mode="cascade")
        for name, rows in data.items():
            table = table_data(rows)
            store.drafts.add_chunks(
                ref_id=ref.id,
                chunk_kind="table",
                text=table_to_markdown(table, caption=name),
                at={"into": heading.dc},
                split=False,
                meta={"table": table, "caption": name, "source": "weekly_report"},
            )
