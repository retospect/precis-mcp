"""Read-only memory inventory over lifetime counters, without focused reads.

Aggregate bodies and active tags separately before joining: joining raw chunks
and tags would multiply counters and character counts. The one query uses the
existing chunks kind/ref indexes, live refs index and ref_reads primary key;
only one bounded page reaches Python. No renderer calls back into the store.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from precis._result_budget import result_context_cap
from precis.errors import BadInput
from precis.format import render_agent_table
from precis.response import Response
from precis.store import Store, Tag
from precis.store._tag_filter import split_tag_polarity
from precis.utils import handle_registry
from precis.utils.next_block import render_next_section

_NEXT = "search(kind='memory', view='reads', args={'sort': 'reads', 'page': 1})"
_SORTS = {
    "reads": "n_agent::numeric + n_human",
    "size": "body_chars",
    "score": "(n_agent::numeric + n_human) * body_chars",
}
_SCHEMA = [
    "handle",
    "title",
    "space",
    "n_agent",
    "n_human",
    "first_read_at",
    "last_read_at",
    "body_chars",
]

#: The shared inventory: every live memory joined to its body size, read
#: counters and active tags. The page select and the domain-footer count
#: run the same WHERE over it — the footer must count the corpus the
#: view renders, nothing broader.
_INVENTORY = """
    WITH live AS MATERIALIZED (
        SELECT ref_id, title FROM refs
        WHERE kind = 'memory' AND retired_at IS NULL
    ), bodies AS (
        SELECT c.ref_id, SUM(char_length(c.text)) AS body_chars
        FROM chunks c JOIN live USING (ref_id)
        WHERE c.chunk_kind = 'memory_body' AND c.ord >= 0
          AND c.retired_at IS NULL
        GROUP BY c.ref_id
    ), active_tags AS (
        SELECT rt.ref_id,
               array_agg(t.value ORDER BY t.value)
                   FILTER (WHERE t.namespace = 'SPACE') AS spaces,
               array_agg(CASE WHEN t.namespace IN ('OPEN', 'FLAG')
                              THEN t.value ELSE t.namespace || ':' || t.value
                         END) AS tag_values
        FROM ref_tags rt JOIN live USING (ref_id)
        JOIN tags t USING (tag_id)
        WHERE rt.expires_at IS NULL OR rt.expires_at > now()
        GROUP BY rt.ref_id
    ), inventory AS (
        SELECT live.ref_id, live.title,
               COALESCE(active_tags.spaces, ARRAY[]::text[]) AS spaces,
               COALESCE(active_tags.tag_values, ARRAY[]::text[]) AS tag_values,
               COALESCE(rr.n_agent, 0) AS n_agent,
               COALESCE(rr.n_human, 0) AS n_human,
               rr.first_read_at,
               GREATEST(rr.last_agent_at, rr.last_human_at) AS last_read_at,
               COALESCE(bodies.body_chars, 0) AS body_chars
        FROM live LEFT JOIN ref_reads rr USING (ref_id)
        LEFT JOIN bodies USING (ref_id)
        LEFT JOIN active_tags USING (ref_id)
    )
"""


def _integer(value: int, name: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise BadInput(f"{name}= must be an integer >= {minimum}", next=_NEXT)
    return value


def _stamp(value: datetime | None) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z") if value else "-"


def _reads_args(
    space: str | None, min_chars: int | None, min_reads: int | None
) -> tuple[str | None, int, int]:
    """Normalize the value predicates both selects share."""
    min_chars = _integer(0 if min_chars is None else min_chars, "min_chars", 0)
    min_reads = _integer(0 if min_reads is None else min_reads, "min_reads", 0)
    if space is not None:
        if not isinstance(space, str) or not space.strip():
            raise BadInput("space= must name a SPACE value or 'untagged'", next=_NEXT)
        space = space.strip().removeprefix("SPACE:")
        if not space:
            raise BadInput("space= must name a SPACE value or 'untagged'", next=_NEXT)
    return space, min_chars, min_reads


def _reads_where(
    *,
    space: str | None,
    min_chars: int,
    min_reads: int,
    tags: list[str] | None,
    include_ref_ids: list[int] | None,
    exclude_ref_ids: list[int] | None,
) -> tuple[str, list[Any]]:
    """The inventory WHERE clause + params shared by the page select and
    :func:`count_reads`."""
    where = ["body_chars >= %s", "n_agent::numeric + n_human >= %s"]
    params: list[Any] = [min_chars, min_reads]
    if space == "untagged":
        where.append("cardinality(spaces) = 0")
    elif space is not None:
        where.append("%s = ANY(spaces)")
        params.append(space)
    normalized = Tag.normalize_filter(tags, kind="memory")
    if normalized:
        # ``'!<tag>'`` entries (gr568354) exclude: the row must carry
        # every positive tag and none of the negated ones — same split
        # ``build_tag_filter`` applies on the SQL search paths.
        positive, negated = split_tag_polarity(normalized)
        if positive:
            where.append("tag_values @> %s::text[]")
            params.append(positive)
        for tag in negated:
            where.append("NOT (tag_values @> %s::text[])")
            params.append([tag])
    if include_ref_ids is not None:
        where.append("ref_id = ANY(%s::bigint[])")
        params.append(include_ref_ids)
    if exclude_ref_ids:
        where.append("NOT (ref_id = ANY(%s::bigint[]))")
        params.append(exclude_ref_ids)
    return " AND ".join(where), params


def count_reads(
    store: Store,
    *,
    space: str | None = None,
    min_chars: int | None = None,
    min_reads: int | None = None,
    tags: list[str] | None = None,
    include_ref_ids: list[int] | None = None,
    exclude_ref_ids: list[int] | None = None,
) -> int:
    """The size of the reads-view corpus under these predicates — the
    domain-footer's before/after counter."""
    space, min_chars, min_reads = _reads_args(space, min_chars, min_reads)
    where, params = _reads_where(
        space=space,
        min_chars=min_chars,
        min_reads=min_reads,
        tags=tags,
        include_ref_ids=include_ref_ids,
        exclude_ref_ids=exclude_ref_ids,
    )
    with store.pool.connection() as conn:
        row = conn.execute(
            f"{_INVENTORY} SELECT COUNT(*) FROM inventory WHERE {where}",
            params,
        ).fetchone()
    return int(row[0]) if row else 0


def render_reads(
    store: Store,
    *,
    sort: str | None = None,
    space: str | None = None,
    min_chars: int | None = None,
    min_reads: int | None = None,
    page: int = 1,
    page_size: int = 10,
    tags: list[str] | None = None,
    include_ref_ids: list[int] | None = None,
    exclude_ref_ids: list[int] | None = None,
) -> Response:
    """Rank and render live memory rows with one SELECT and no read bumps."""
    sort = sort.strip().lower() if isinstance(sort, str) else sort
    sort = "reads" if sort is None else sort
    if not isinstance(sort, str) or sort not in _SORTS:
        raise BadInput(
            "sort= must be reads, size or score", options=list(_SORTS), next=_NEXT
        )
    page = _integer(page, "page", 1)
    page_size = _integer(page_size, "page_size", 1)
    if page_size > 100:
        raise BadInput("page_size= must be <= 100", next=_NEXT)
    space, min_chars, min_reads = _reads_args(space, min_chars, min_reads)
    where, params = _reads_where(
        space=space,
        min_chars=min_chars,
        min_reads=min_reads,
        tags=tags,
        include_ref_ids=include_ref_ids,
        exclude_ref_ids=exclude_ref_ids,
    )
    params.extend([page_size + 1, (page - 1) * page_size])
    # SQL fragments are closed-set sort expressions and fixed predicates only.
    sql = f"""
        {_INVENTORY}
        SELECT ref_id, title, spaces, n_agent, n_human,
               first_read_at, last_read_at, body_chars
        FROM inventory WHERE {where}
        ORDER BY {_SORTS[sort]} DESC, ref_id ASC
        LIMIT %s OFFSET %s
    """
    with store.pool.connection() as conn:
        records = conn.execute(sql, params).fetchall()
    rows = [
        {
            "handle": handle_registry.format_handle("memory", ref_id),
            "title": title or "",
            "space": ", ".join(f"SPACE:{s}" for s in spaces) or "untagged",
            "n_agent": str(n_agent),
            "n_human": str(n_human),
            "first_read_at": _stamp(first_read),
            "last_read_at": _stamp(last_read),
            "body_chars": str(body_chars),
        }
        for ref_id, title, spaces, n_agent, n_human, first_read, last_read, body_chars in records[
            :page_size
        ]
    ]
    body = f"Memory reads — page {page}, sort={sort}. Counters are lifetime totals, not a window.\n"
    body += (
        render_agent_table(rows, schema=_SCHEMA)
        if rows
        else f"no memory entries on page {page}"
    )
    args: dict[str, Any] = {
        "sort": sort,
        "page": page + 1,
        "page_size": page_size,
        "min_chars": min_chars,
        "min_reads": min_reads,
    }
    if space is not None:
        args["space"] = space
    if include_ref_ids is not None:
        args["include_ref_ids"] = include_ref_ids
    if exclude_ref_ids:
        args["exclude_ref_ids"] = exclude_ref_ids
    tags_echo = f", tags={tags!r}" if tags else ""
    if len(records) > page_size:
        body += render_next_section(
            [
                (
                    f"search(kind='memory', view='reads'{tags_echo}, args={args!r})",
                    "next page",
                )
            ]
        )
    # Fixed SQL offsets must never advance past rows the runtime fitter hid.
    # Leave room for runtime hints; refuse rather than emit an incomplete page.
    if len(body.encode("utf-8")) > max(200, result_context_cap() - 400):
        if page_size == 1:
            args["page"] = page
            recovery = "raise the result byte budget, then retry: "
        else:
            args.update(page=1, page_size=max(1, page_size // 2))
            recovery = "restart with a smaller page size: "
        raise BadInput(
            "memory reads page exceeds the result byte budget; no partial page returned",
            next=recovery
            + f"search(kind='memory', view='reads'{tags_echo}, args={args!r})",
        )
    return Response(
        body=body,
        result_count=len(rows),
        result_ids=[int(r[0]) for r in records[:page_size]],
    )
