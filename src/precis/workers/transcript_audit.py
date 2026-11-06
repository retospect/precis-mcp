"""Model-free sampling of closed factory chains for Claude transcript audits.

Dark until service_config lights the host. The related-to link is the durable
idempotency marker; all mint writes share a transaction so partial audits do
not hide chains from later fires. Scheduler leases serialize normal fires.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from precis.corpus_layout import host_name
from precis.store import Store
from precis.store.types import ChunkInsert, Ref, Tag
from precis.workers.runner import BatchResult
from precis.workers.service_config import ALL_HOSTS, ServiceConfigResolver

log = logging.getLogger(__name__)
SERVICE_NAME = "transcript_audit"
_MAX_PER_TICK = 1


def transcript_audit_enabled(
    store: Store, *, conn: Any = None, host: str | None = None
) -> bool:
    """Exact-host configuration wins; absent, zero, or failed lookup is dark."""
    h = host or host_name()
    try:
        if conn is None:
            return ServiceConfigResolver(store, h).enabled(
                SERVICE_NAME, default_on=False
            )
        row = conn.execute(
            "SELECT prio FROM service_config "
            "WHERE service = %s AND host IN (%s, %s) "
            "ORDER BY (host = %s) DESC LIMIT 1",
            (SERVICE_NAME, h, ALL_HOSTS, h),
        ).fetchone()
        return bool(row) and int(row[0]) > 0
    except Exception:
        log.warning("transcript_audit: service_config lookup failed", exc_info=True)
        return False


def candidates(conn: Any, now: datetime, percent: int) -> list[int]:
    """Recently closed tagged plan roots with factory jobs, oldest first."""
    rows = conn.execute(
        """
        WITH RECURSIVE roots AS (
            SELECT DISTINCT substring(t.value from 6)::bigint AS root_id
              FROM ref_tags rt JOIN tags t USING (tag_id)
              JOIN refs r ON r.ref_id = rt.ref_id
             WHERE t.namespace = 'OPEN' AND t.value LIKE 'plan:%%'
               AND substring(t.value from 6) ~ '^[0-9]+$'
               AND r.kind = 'todo' AND r.retired_at IS NULL
        ), tree AS (
            SELECT r.root_id, r.root_id AS todo_id FROM roots r
            UNION
            SELECT t.root_id, c.ref_id FROM tree t
              JOIN refs c ON c.parent_id = t.todo_id AND c.kind = 'todo'
        )
        SELECT r.ref_id FROM roots p JOIN refs r ON r.ref_id = p.root_id
         WHERE r.kind = 'todo' AND r.retired_at IS NULL
           AND r.updated_at >= %s AND r.updated_at <= %s
           AND mod(r.ref_id, 100) < %s
           AND EXISTS (SELECT 1 FROM ref_tags rt JOIN tags s USING (tag_id)
                        WHERE rt.ref_id = r.ref_id AND s.namespace = 'STATUS'
                          AND s.value IN ('done', 'won''t-do'))
           AND EXISTS (SELECT 1 FROM tree t JOIN refs j ON j.parent_id = t.todo_id
                        WHERE t.root_id = r.ref_id AND j.kind = 'job'
                          AND j.meta->>'job_type' IN ('run_slice', 'land_slice'))
           AND NOT EXISTS (
               SELECT 1 FROM links l JOIN refs a ON a.ref_id = l.src_ref_id
               JOIN ref_tags at ON at.ref_id = a.ref_id
               JOIN tags s ON s.tag_id = at.tag_id
                WHERE l.dst_ref_id = r.ref_id AND l.relation = 'related-to'
                  AND a.kind = 'todo' AND s.namespace = 'OPEN'
                  AND s.value = 'audit:transcript')
         ORDER BY r.updated_at, r.ref_id
        """,
        (now - timedelta(days=7), now, max(0, min(100, percent))),
    ).fetchall()
    return [int(row[0]) for row in rows]


def _jobs(conn: Any, root: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        WITH RECURSIVE tree AS (
            SELECT ref_id FROM refs WHERE ref_id = %s
            UNION
            SELECT c.ref_id FROM refs c JOIN tree t ON c.parent_id = t.ref_id
             WHERE c.kind = 'todo'
        )
        SELECT j.ref_id, j.meta,
               (SELECT s.value FROM ref_tags rt JOIN tags s USING (tag_id)
                 WHERE rt.ref_id = j.ref_id AND s.namespace = 'STATUS' LIMIT 1)
          FROM refs j JOIN tree t ON j.parent_id = t.ref_id
         WHERE j.kind = 'job' AND j.meta->>'job_type' IN ('run_slice', 'land_slice')
         ORDER BY j.created_at, j.ref_id
        """,
        (root,),
    ).fetchall()
    return [dict(id=int(r[0]), meta=r[1] or {}, status=r[2] or "?") for r in rows]


def render_brief(chain_root: Ref, jobs: list[dict[str, Any]]) -> str:
    """Instructions for the audit session, including the reachability stop rule."""
    lines = ["## Brief", f"Chain td{chain_root.id}: {chain_root.title}. Jobs:"]
    for job in jobs:
        meta = job["meta"]
        lines.append(
            f"jo{job['id']} {meta.get('job_type', '?')} "
            f"lane={meta.get('lane') or '?'} model={meta.get('model') or '?'} "
            f"status={job['status']} wall={meta.get('wall_s', '?')}s"
        )
    lines.append(
        "\nHow to read: get(kind='job', id=<id>) shows the launch event and the "
        "summary; get(kind='job', id=<id>, view='events') shows the full event log "
        "where offered; get(kind='agentlog', id='/recent') lists session logs. "
        "First-run rule: if a job's transcript (tool calls and their results) is "
        'not reachable through the MCP, file ONE gripe titled "transcript audit: '
        '<lane> job transcript not reachable through the MCP (jo<id>)" with what '
        "was tried, and stop on that job; do not guess at what the agent did.\n\n"
        "What to find, blatant and skill- or MCP-rooted only: an identical MCP "
        "call retried after an error; the same skill or file fetched three or "
        "more times; grep or search for something a kind, view or skill already "
        "provides; a [callerr] refusal whose next step did not work; an anchor "
        "named in the brief that took five or more calls to locate; test reruns "
        "caused by harness or environment, not by the code. Threshold: a pattern "
        "must have cost five or more tool calls or two or more identical retries. "
        "At most three gripes per audit.\n\n"
        "Dedup: search(kind='gripe', q='<the pattern>') first; skip a pattern an "
        "open gripe already names.\n\n"
        "Gripe shape: put(kind='gripe', title='<skill or MCP affordance>: <what "
        "wasted the calls>', text=<body>), body carrying the job handle and turn "
        "range, what the agent did, what the surface should have offered, and "
        "the fix shape (file or skill name); tag audit:transcript; then "
        "link(kind='gripe', id=<gid>, target='job:<id>', rel='supported-by').\n\n"
        'Close: append a comment to this todo starting "handoff: audit read <n> '
        'jobs, filed <k> gripes: gr..." and end the session. No commits.'
    )
    return "\n".join(lines)


def mint_audit(store: Store, chain_root_id: int, jobs: list[dict[str, Any]]) -> int:
    """Create the two-node plan and its idempotency link atomically."""
    chain = store.get_ref(kind="todo", id=chain_root_id)
    assert chain is not None
    with store.tx() as conn:
        root = store.insert_ref(
            kind="todo",
            slug=None,
            title=f"Audit chain td{chain_root_id}: {len(jobs)} factory job transcripts",
            parent_id=None,
            meta={"auto_close": True},
            conn=conn,
        )
        task = store.insert_ref(
            kind="todo",
            slug=None,
            title="Read the chain's transcripts and file inefficiency gripes",
            parent_id=root.id,
            meta={"op": {"type": "run_slice", "params": {"lane": "claude-sub"}}},
            conn=conn,
        )
        for ref, tags, body in (
            (
                root,
                ["thread:factory", "audit:transcript"],
                f"Audit the factory job transcripts for chain td{chain.id}: {chain.title}.",
            ),
            (task, [], render_brief(chain, jobs)),
        ):
            # Parented todos may receive a default tier; the pinned lane owns it.
            conn.execute(
                "UPDATE refs SET meta = meta - 'llm_tier' WHERE ref_id = %s", (ref.id,)
            )
            for tag in ["STATUS:open", f"plan:{root.id}", *tags]:
                store.add_tag(
                    ref.id,
                    Tag.parse_strict(tag, kind="todo"),
                    set_by="agent",
                    replace_prefix=tag.startswith("STATUS:"),
                    conn=conn,
                )
            store.chunks.insert_chunks(
                ref.id,
                [ChunkInsert(ord=0, text=body, meta={"chunk_kind": "todo_body"})],
                conn=conn,
            )
        store.add_link(
            src_ref_id=root.id,
            dst_ref_id=chain_root_id,
            relation="related-to",
            conn=conn,
        )
        row = conn.execute(
            "SELECT coalesce(max(ord), -1) + 1 FROM chunks WHERE ref_id = %s",
            (chain_root_id,),
        ).fetchone()
        store.chunks.insert_chunks(
            chain_root_id,
            [
                ChunkInsert(
                    ord=int(row[0]) if row else 0,
                    text=f"audit: td{root.id} samples this chain's transcripts",
                    meta={"chunk_kind": "todo_comment"},
                )
            ],
            conn=conn,
        )
    return root.id


def run_transcript_audit_pass(store: Store, *, host: str | None = None) -> BatchResult:
    """Sample and mint at most one audit; never call a model."""
    if not transcript_audit_enabled(store, host=host):
        return BatchResult(handler=SERVICE_NAME, claimed=0, ok=0, failed=0)
    percent = max(
        0, min(100, int(os.environ.get("PRECIS_TRANSCRIPT_AUDIT_PERCENT", "10")))
    )
    with store.pool.connection() as conn:
        picks = candidates(conn, datetime.now(UTC), percent)[:_MAX_PER_TICK]
        jobs = {root: _jobs(conn, root) for root in picks}
    minted: list[dict[str, int]] = []
    failed = 0
    for root in picks:
        try:
            audit = mint_audit(store, root, jobs[root])
            minted.append({"chain": root, "root": audit})
        except Exception:
            failed += 1
            log.warning("transcript_audit: mint for td%d failed", root, exc_info=True)
    return BatchResult(
        handler=SERVICE_NAME,
        claimed=len(picks),
        ok=len(minted),
        failed=failed,
        extra={"minted": minted, "percent": percent},
    )
