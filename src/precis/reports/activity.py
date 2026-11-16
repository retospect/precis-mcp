"""Shared opening and current-terminal-tag closure clocks for reports."""

from typing import Any


def flow_buckets(
    conn: Any,
    *,
    bucket: str,
    where: str,
    params: list[Any],
    closed: list[str],
    opens_window: str,
    closes_window: str,
    window_params: list[Any],
) -> tuple[list[Any], list[Any]]:
    """Return opening/closure buckets using the flow view's live-tag semantics.

    SQL fragments are internal callers' fixed predicates; values stay bound.
    """
    opens = conn.execute(
        "SELECT date_trunc(%s, r.created_at AT TIME ZONE 'UTC'), count(*) "
        f"FROM refs r WHERE {where}{opens_window} GROUP BY 1 ORDER BY 1",
        [bucket, *params, *window_params],
    ).fetchall()
    closes = conn.execute(
        "SELECT date_trunc(%s, rt.created_at AT TIME ZONE 'UTC'), count(DISTINCT r.ref_id) "
        "FROM refs r JOIN ref_tags rt ON rt.ref_id = r.ref_id "
        "JOIN tags t ON t.tag_id = rt.tag_id "
        "WHERE t.namespace = 'STATUS' AND t.value = ANY(%s) "
        "AND (rt.expires_at IS NULL OR rt.expires_at > now()) "
        f"AND {where}{closes_window} GROUP BY 1 ORDER BY 1",
        [bucket, closed, *params, *window_params],
    ).fetchall()
    return opens, closes
