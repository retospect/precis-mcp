"""Rolling gripe progress shared by the web workbench and MCP.

This is a current-cohort snapshot, not an event history: reopening removes
a gripe from the totals. Fixed means done or awaiting verification; its clock
is the current STATUS attachment. Verified is the subset closed with a success
comment in that same transaction. Matching the closure clock prevents an old
pass from surviving a reopen/reclose. Duplicates need a gripe successor —
either a retirement or a live ``STATUS:wontfix`` close on the dup, the
latter timed by its STATUS attachment; junk and filed-in-error deletions
are not progress.
"""

from __future__ import annotations

from datetime import UTC, datetime

from precis.store import Store


def rolling_stats(store: Store, *, now: datetime | None = None) -> list[dict[str, int]]:
    """Count each gripe once per cumulative UTC window, independent of filters.

    The cohort is pre-narrowed to rows that can count in any window — a
    live STATUS attachment or a retirement inside the widest window —
    since the per-row ``verified``/``duplicate`` EXISTS probes are the
    cost (gr762554). The bound is hardcoded to the widest ``windows``
    entry below; widening that VALUES list past 24h without matching it
    here under-counts. A meta->>'superseded_by' successor names a bare
    or ``gr``-prefixed ref id; the ANY form probes
    ``refs_gripe_ref_id_text_idx`` (0361) where ``= text`` and
    ``= 'gr' || text`` could not.
    """
    stamp = now or datetime.now(UTC)
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            WITH cohort AS (
              SELECT r.ref_id, r.retired_at, t.value AS state,
                     rt.created_at AS state_at,
                     EXISTS (
                       SELECT 1 FROM chunks c
                       WHERE c.ref_id = r.ref_id AND c.chunk_kind = 'gripe_comment'
                         AND c.retired_at IS NULL AND c.created_at = rt.created_at
                         AND c.text ~ '^resolved:.*(auto-verified after deploy:|user-verified after deploy|prod check passed:)'
                     ) AS verified,
                     EXISTS (
                       SELECT 1 FROM links l JOIN refs keeper ON keeper.ref_id = l.src_ref_id
                       WHERE l.dst_ref_id = r.ref_id AND l.relation = 'supersedes'
                         AND keeper.kind = 'gripe' AND keeper.ref_id <> r.ref_id
                     ) OR EXISTS (
                       SELECT 1 FROM links l JOIN refs keeper ON keeper.ref_id = l.dst_ref_id
                       WHERE l.src_ref_id = r.ref_id AND l.relation = 'superseded-by'
                         AND keeper.kind = 'gripe' AND keeper.ref_id <> r.ref_id
                     ) OR EXISTS (
                       SELECT 1 FROM refs keeper
                       WHERE keeper.kind = 'gripe' AND keeper.ref_id <> r.ref_id
                         AND keeper.ref_id::text = ANY (ARRAY[
                               r.meta->>'superseded_by',
                               regexp_replace(r.meta->>'superseded_by', '^gr', '')])
                     ) AS duplicate
              FROM refs r
              LEFT JOIN ref_tags rt ON rt.ref_id = r.ref_id
                AND rt.tag_id IN (SELECT tag_id FROM tags WHERE namespace = 'STATUS')
                AND (rt.expires_at IS NULL OR rt.expires_at > %s)
              LEFT JOIN tags t USING (tag_id)
              WHERE r.kind = 'gripe'
                AND ((rt.created_at >= %s::timestamptz - interval '24 hours'
                      AND rt.created_at <= %s)
                     OR (r.retired_at >= %s::timestamptz - interval '24 hours'
                         AND r.retired_at <= %s))
            )
            SELECT hours,
              count(DISTINCT ref_id) FILTER (WHERE retired_at IS NULL
                AND state IN ('done', 'verify', 'auto_verify', 'user_verify')
                AND state_at >= %s::timestamptz - hours * interval '1 hour' AND state_at <= %s),
              count(DISTINCT ref_id) FILTER (WHERE retired_at IS NULL
                AND state = 'done' AND verified
                AND state_at >= %s::timestamptz - hours * interval '1 hour' AND state_at <= %s),
              count(DISTINCT ref_id) FILTER (WHERE duplicate AND (
                (retired_at IS NOT NULL
                  AND retired_at >= %s::timestamptz - hours * interval '1 hour'
                  AND retired_at <= %s)
                OR (retired_at IS NULL AND state = 'wontfix'
                  AND state_at >= %s::timestamptz - hours * interval '1 hour'
                  AND state_at <= %s)
              ))
            FROM (VALUES (1), (6), (12), (24)) AS windows(hours)
            LEFT JOIN cohort ON true GROUP BY hours ORDER BY hours
            """,
            (stamp,) * 13,
        ).fetchall()
    return [
        dict(
            zip(
                ("hours", "fixed", "verified", "duplicates"), map(int, row), strict=True
            )
        )
        for row in rows
    ]


def render_stats(
    rows: list[dict[str, int]], *, friction: dict[str, int] | None = None
) -> str:
    """Compact MCP table with the snapshot's counting semantics.

    ``friction`` appends the friction holding lane's lifetime measure
    (gr764123: filed / released / aged out — ``friction_stats`` in
    ``workers.gripe_triage``); the junk rate is aged out ÷ filed.
    """
    lines = [
        "Gripe progress · rolling UTC windows (current cohort)",
        "| Last | Fixed | Verified | Closed as dup |",
        "| --- | ---: | ---: | ---: |",
    ]
    lines.extend(
        f"| {r['hours']}h | {r['fixed']} | {r['verified']} | {r['duplicates']} |"
        for r in rows
    )
    lines.append(
        "Fixed: done or awaiting verification, timed by current status; "
        "verified: success recorded with that closure. Reopened gripes excluded. "
        "Duplicates: closed as wontfix or retired, with a gripe successor."
    )
    if friction is not None:
        filed = friction["filed"]
        junk = f"{friction['aged_out'] / filed:.0%}" if filed else "n/a — nothing filed"
        lines.append(
            f"Friction lane (gr764123): {filed} filed · "
            f"{friction['released']} released · {friction['aged_out']} aged out "
            f"· junk rate {junk} (aged out ÷ filed)"
        )
    return "\n".join(lines)
