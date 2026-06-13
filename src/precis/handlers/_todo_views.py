"""Tree-aware view renderers for the todo handler (Slice 1).

Each public function in this module is the implementation of one
``search(kind='todo', view='<name>')`` or ``get(kind='todo', id=N,
view='tree')`` shape. Splitting them out of ``todo.py`` keeps the
handler readable; every renderer here returns a fully-rendered
:class:`Response` (including the ``Next:`` trailer).

The renderers share a few private helpers (``_status_of``,
``_level_of``, ``_ancestor_chain``) that hide the ``ref_tags`` /
``tags`` join shape and the recursive parent walks. The handler
delegates ``view='log'`` / ``view='links'`` to the base class — only
the tree-aware views live here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from precis.errors import BadInput, NotFound
from precis.response import Response
from precis.utils.next_block import render_next_section

if TYPE_CHECKING:
    from precis.store import Store


# ── shared helpers ─────────────────────────────────────────────────


def _status_of_many(store: Store, ref_ids: list[int]) -> dict[int, str]:
    """Bulk-fetch STATUS tag values for a list of refs (default 'open').

    One query plus an in-Python dict so renderers that touch a few
    dozen refs don't fire N round-trips. STATUS lives in the unified
    ``tags`` table under ``namespace='STATUS'``; the closed-prefix
    invariant means at most one row per ref.
    """
    if not ref_ids:
        return {}
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT rt.ref_id, t.value
              FROM ref_tags rt
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE rt.ref_id = ANY(%s)
               AND t.namespace = 'STATUS'
            """,
            (ref_ids,),
        ).fetchall()
    out = {int(r[0]): str(r[1]) for r in rows}
    for ref_id in ref_ids:
        out.setdefault(ref_id, "open")
    return out


def _open_tag_present(store: Store, ref_id: int, value: str) -> bool:
    """True when ``ref_id`` carries the open tag ``value`` (e.g. ``level:strategic``)."""
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM ref_tags rt
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE rt.ref_id = %s
               AND t.namespace = 'OPEN'
               AND t.value = %s
             LIMIT 1
            """,
            (ref_id, value),
        ).fetchone()
    return row is not None


def _ancestor_chain(store: Store, ref_id: int) -> list[dict[str, Any]]:
    """Return ``[root, …, ref]`` as dicts ``{id, title, level}``.

    The walk includes the ref itself so the caller can use it for
    breadcrumbs or full-chain rendering without re-fetching. Level
    is the first matching ``level:*`` open tag found on each ref, or
    ``None`` when no level tag is set.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            WITH RECURSIVE chain(ref_id, parent_id, title, depth) AS (
                SELECT ref_id, parent_id, title, 0
                  FROM refs WHERE ref_id = %s AND deleted_at IS NULL
                UNION ALL
                SELECT r.ref_id, r.parent_id, r.title, c.depth + 1
                  FROM refs r
                  JOIN chain c ON r.ref_id = c.parent_id
                 WHERE r.deleted_at IS NULL
            )
            SELECT c.ref_id, c.title, c.depth,
                   (SELECT t.value FROM ref_tags rt
                      JOIN tags t ON t.tag_id = rt.tag_id
                     WHERE rt.ref_id = c.ref_id
                       AND t.namespace = 'OPEN'
                       AND t.value LIKE 'level:%%'
                     LIMIT 1) AS level
              FROM chain c
             ORDER BY c.depth DESC
            """,
            (ref_id,),
        ).fetchall()
    return [
        {
            "id": int(r[0]),
            "title": (r[1] or "").split("\n", 1)[0],
            "level": r[3],
        }
        for r in rows
    ]


def _picks_7d_by_strategic(store: Store) -> dict[int, int]:
    """Map ``strategic_root_id`` → count of ``status:done`` events in 7d.

    Implements the rolling-window accounting from the plan
    (Accounting section, "Picks-in-window per strategic"). Skips
    paused strategics so they don't sink the rotation.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            WITH RECURSIVE
              strat AS (
                SELECT r.ref_id
                  FROM refs r
                 WHERE r.kind = 'todo'
                   AND r.deleted_at IS NULL
                   AND r.parent_id IS NULL
                   AND EXISTS (
                       SELECT 1 FROM ref_tags rt
                         JOIN tags t ON t.tag_id = rt.tag_id
                        WHERE rt.ref_id = r.ref_id
                          AND t.namespace = 'OPEN'
                          AND t.value = 'level:strategic'
                   )
                   AND NOT EXISTS (
                       SELECT 1 FROM ref_tags rt
                         JOIN tags t ON t.tag_id = rt.tag_id
                        WHERE rt.ref_id = r.ref_id
                          AND t.namespace = 'STATUS'
                          AND t.value = 'paused'
                   )
              ),
              subtree AS (
                SELECT s.ref_id AS ref_id, s.ref_id AS strategic_id FROM strat s
                UNION ALL
                SELECT r.ref_id, st.strategic_id
                  FROM refs r
                  JOIN subtree st ON r.parent_id = st.ref_id
                 WHERE r.kind = 'todo' AND r.deleted_at IS NULL
              )
            SELECT st.strategic_id, count(e.event_id) AS picks_7d
              FROM subtree st
              LEFT JOIN ref_events e ON e.ref_id = st.ref_id
                                    AND e.event = 'status:done'
                                    AND e.ts >= now() - interval '7 days'
             GROUP BY st.strategic_id
            """,
        ).fetchall()
    return {int(r[0]): int(r[1] or 0) for r in rows}


# ── view: roots ────────────────────────────────────────────────────


def render_roots(store: Store) -> Response:
    """Strategic-root dashboard: one row per top-level strategic.

    Renders the past-tense 7d accounting (picks count). The "next
    pick" indicator is the lowest-picks strategic with at least one
    open leaf — same rule the doable view uses.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id, r.title,
                   (SELECT t.value FROM ref_tags rt
                      JOIN tags t ON t.tag_id = rt.tag_id
                     WHERE rt.ref_id = r.ref_id
                       AND t.namespace = 'STATUS'
                     LIMIT 1) AS status
              FROM refs r
             WHERE r.kind = 'todo'
               AND r.deleted_at IS NULL
               AND r.parent_id IS NULL
               AND EXISTS (
                   SELECT 1 FROM ref_tags rt
                     JOIN tags t ON t.tag_id = rt.tag_id
                    WHERE rt.ref_id = r.ref_id
                      AND t.namespace = 'OPEN'
                      AND t.value = 'level:strategic'
               )
             ORDER BY r.ref_id
            """,
        ).fetchall()
    if not rows:
        body = "no strategic todos yet"
        body += render_next_section(
            [
                (
                    "put(kind='todo', text='Strategic intent', "
                    "tags=['level:strategic'])",
                    "mint a strategic root (owner-only)",
                ),
            ]
        )
        return Response(body=body)

    picks = _picks_7d_by_strategic(store)
    active_ids = _active_strategic_ids(store)
    lines = [f"# {len(rows)} strategic root{'' if len(rows) == 1 else 's'}"]
    lines.append("")
    # Pick the lowest-picks active strategic; tie broken by ref_id
    # (the plan's deterministic rule).
    next_pick: int | None = None
    if active_ids:
        ordered = sorted(active_ids, key=lambda i: (picks.get(i, 0), i))
        next_pick = ordered[0]
    for ref_id, title, status in rows:
        ref_id = int(ref_id)
        first_line = (title or "").split("\n", 1)[0]
        if len(first_line) > 60:
            first_line = first_line[:60].rstrip() + "…"
        n_picks = picks.get(ref_id, 0)
        marker = " ← next pick" if ref_id == next_pick else ""
        suffix = ""
        if status == "paused":
            suffix = " [paused]"
        lines.append(
            f"#{ref_id:<4} {first_line:<60}  7d: {n_picks:>2} picks{suffix}{marker}"
        )
    if active_ids:
        total_picks = sum(picks.get(i, 0) for i in active_ids)
        expected = total_picks / len(active_ids) if active_ids else 0
        lines.append("")
        lines.append(
            f"Active strategics: {len(active_ids)}    "
            f"Total picks (7d): {total_picks}    "
            f"Expected share: {expected:.1f} each"
        )
    body = "\n".join(lines)
    body += render_next_section(
        [
            (
                "search(kind='todo', view='strategic')",
                "drill in: strategic + tactical layer",
            ),
            (
                "search(kind='todo', view='doable')",
                "the doable leaves across all strategics",
            ),
        ]
    )
    return Response(body=body)


def _active_strategic_ids(store: Store) -> list[int]:
    """Strategic roots that have at least one open leaf (= eligible for picks)."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            WITH RECURSIVE
              strat AS (
                SELECT r.ref_id
                  FROM refs r
                 WHERE r.kind = 'todo' AND r.deleted_at IS NULL
                   AND r.parent_id IS NULL
                   AND EXISTS (
                       SELECT 1 FROM ref_tags rt
                         JOIN tags t ON t.tag_id = rt.tag_id
                        WHERE rt.ref_id = r.ref_id
                          AND t.namespace = 'OPEN'
                          AND t.value = 'level:strategic'
                   )
                   AND NOT EXISTS (
                       SELECT 1 FROM ref_tags rt
                         JOIN tags t ON t.tag_id = rt.tag_id
                        WHERE rt.ref_id = r.ref_id
                          AND t.namespace = 'STATUS'
                          AND t.value = 'paused'
                   )
              ),
              subtree AS (
                SELECT s.ref_id AS ref_id, s.ref_id AS strategic_id FROM strat s
                UNION ALL
                SELECT r.ref_id, st.strategic_id
                  FROM refs r
                  JOIN subtree st ON r.parent_id = st.ref_id
                 WHERE r.kind = 'todo' AND r.deleted_at IS NULL
              )
            SELECT DISTINCT st.strategic_id
              FROM subtree st
              JOIN refs r ON r.ref_id = st.ref_id
             WHERE NOT EXISTS (
                 SELECT 1 FROM refs c
                  WHERE c.parent_id = r.ref_id
                    AND c.deleted_at IS NULL
             )
               AND NOT EXISTS (
                 SELECT 1 FROM ref_tags rt
                   JOIN tags t ON t.tag_id = rt.tag_id
                  WHERE rt.ref_id = r.ref_id
                    AND t.namespace = 'STATUS'
                    AND t.value IN ('done', 'won''t-do')
             )
            """,
        ).fetchall()
    return [int(r[0]) for r in rows]


# ── view: strategic ────────────────────────────────────────────────


def render_strategic(store: Store) -> Response:
    """Strategic + tactical layer with leaf counts under each tactical."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            WITH leaves AS (
                SELECT r.ref_id AS leaf_id, r.parent_id AS direct_parent
                  FROM refs r
                 WHERE r.kind = 'todo' AND r.deleted_at IS NULL
                   AND NOT EXISTS (
                       SELECT 1 FROM refs c
                        WHERE c.parent_id = r.ref_id
                          AND c.deleted_at IS NULL
                   )
            ),
            leaf_status AS (
                SELECT l.leaf_id, l.direct_parent,
                       COALESCE(
                         (SELECT t.value FROM ref_tags rt
                            JOIN tags t ON t.tag_id = rt.tag_id
                           WHERE rt.ref_id = l.leaf_id
                             AND t.namespace = 'STATUS'
                           LIMIT 1),
                         'open'
                       ) AS status
                  FROM leaves l
            ),
            ancestor AS (
                -- walk each leaf up to its tactical ancestor (or strategic
                -- if a tactical isn't in the chain).
                SELECT ls.leaf_id, ls.status, r.ref_id AS anc_id, 0 AS depth
                  FROM leaf_status ls JOIN refs r ON r.ref_id = ls.leaf_id
                UNION ALL
                SELECT a.leaf_id, a.status, r.ref_id, a.depth + 1
                  FROM ancestor a
                  JOIN refs r ON r.ref_id = (
                      SELECT parent_id FROM refs WHERE ref_id = a.anc_id
                  )
                 WHERE a.depth < 10
            )
            SELECT
              s.ref_id AS strategic_id, s.title AS strategic_title,
              tac.ref_id AS tactical_id, tac.title AS tactical_title,
              COUNT(*) FILTER (WHERE a.status NOT IN ('done', 'won''t-do')) AS open_count,
              COUNT(*) AS total_count
            FROM refs s
            LEFT JOIN refs tac ON tac.parent_id = s.ref_id
                              AND tac.kind = 'todo'
                              AND tac.deleted_at IS NULL
            LEFT JOIN ancestor a ON a.anc_id = tac.ref_id
            WHERE s.kind = 'todo' AND s.deleted_at IS NULL
              AND s.parent_id IS NULL
              AND EXISTS (
                  SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                   WHERE rt.ref_id = s.ref_id AND t.namespace = 'OPEN'
                     AND t.value = 'level:strategic'
              )
            GROUP BY s.ref_id, s.title, tac.ref_id, tac.title
            ORDER BY s.ref_id, tac.ref_id NULLS FIRST
            """,
        ).fetchall()
    if not rows:
        return Response(body="no strategic todos yet")
    lines = ["# strategic + tactical layer"]
    last_strategic: int | None = None
    for (
        strategic_id,
        strat_title,
        tactical_id,
        tac_title,
        open_count,
        total_count,
    ) in rows:
        strategic_id = int(strategic_id)
        if strategic_id != last_strategic:
            lines.append("")
            first_line = (strat_title or "").split("\n", 1)[0]
            lines.append(f"#{strategic_id} {first_line}")
            last_strategic = strategic_id
        if tactical_id is None:
            continue
        tac_first = (tac_title or "").split("\n", 1)[0]
        if len(tac_first) > 60:
            tac_first = tac_first[:60].rstrip() + "…"
        lines.append(
            f"  └─ #{int(tactical_id):<4} {tac_first:<60} "
            f"[{int(open_count or 0)}/{int(total_count or 0)} open]"
        )
    body = "\n".join(lines)
    body += render_next_section(
        [
            (
                "get(kind='todo', id=N, view='tree')",
                "drill into a subtree",
            ),
            (
                "search(kind='todo', view='doable')",
                "doable leaves across all strategics",
            ),
        ]
    )
    return Response(body=body)


# ── view: tree (subtree under a given root) ────────────────────────


def render_tree(store: Store, root_id: int) -> Response:
    """Render the subtree rooted at ``root_id`` as ASCII-ish markdown.

    Walks ``root_id`` and every descendant down to ``MAX_DEPTH``,
    then renders depth-first with box-drawing prefixes. Status icons
    follow the plan's render: ◀ claimed, ⏸ waiting, ○ doable, ✓ done.
    """
    rows = _fetch_subtree(store, root_id)
    if not rows:
        raise NotFound(
            f"todo id={root_id} not found",
            next="get(kind='todo', id='/recent') to find an existing id",
        )
    # Tag bundles for each ref so we can mark claimed / waiting / level.
    ref_ids = [r["id"] for r in rows]
    statuses = _status_of_many(store, ref_ids)
    tags_by_ref = _all_open_tags(store, ref_ids)
    by_parent: dict[int | None, list[dict[str, Any]]] = {}
    for r in rows:
        by_parent.setdefault(r["parent_id"], []).append(r)
    # The root may sit anywhere — find it explicitly.
    root_row = next((r for r in rows if r["id"] == root_id), None)
    assert root_row is not None  # contract: _fetch_subtree returns the root
    lines: list[str] = []
    _render_node(
        root_row,
        depth=0,
        prefix="",
        is_last=True,
        by_parent=by_parent,
        statuses=statuses,
        tags_by_ref=tags_by_ref,
        out=lines,
    )
    body = "\n".join(lines)
    body += render_next_section(
        [
            (f"get(kind='todo', id={root_id})", "read this todo + tags + ancestry"),
            (
                f"search(kind='todo', view='doable', args={{'under': {root_id}}})",
                "doable leaves in this subtree",
            ),
        ]
    )
    return Response(body=body)


def _fetch_subtree(store: Store, root_id: int) -> list[dict[str, Any]]:
    """Return ``[{id, parent_id, title, depth}]`` for the subtree at root."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            WITH RECURSIVE walk(ref_id, parent_id, title, depth) AS (
                SELECT ref_id, parent_id, title, 0
                  FROM refs WHERE ref_id = %s
                              AND kind = 'todo'
                              AND deleted_at IS NULL
                UNION ALL
                SELECT r.ref_id, r.parent_id, r.title, w.depth + 1
                  FROM refs r
                  JOIN walk w ON r.parent_id = w.ref_id
                 WHERE r.kind = 'todo'
                   AND r.deleted_at IS NULL
                   AND w.depth < 10
            )
            SELECT ref_id, parent_id, title, depth FROM walk
             ORDER BY depth, ref_id
            """,
            (root_id,),
        ).fetchall()
    return [
        {"id": int(r[0]), "parent_id": r[1], "title": r[2], "depth": int(r[3])}
        for r in rows
    ]


def _all_open_tags(store: Store, ref_ids: list[int]) -> dict[int, list[str]]:
    """Bulk open-tag fetch for the level / claimed-by / waiting / etc. markers."""
    if not ref_ids:
        return {}
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT rt.ref_id, t.value
              FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
             WHERE rt.ref_id = ANY(%s)
               AND t.namespace = 'OPEN'
            """,
            (ref_ids,),
        ).fetchall()
    out: dict[int, list[str]] = {}
    for ref_id, value in rows:
        out.setdefault(int(ref_id), []).append(str(value))
    return out


def _render_node(
    node: dict[str, Any],
    *,
    depth: int,
    prefix: str,
    is_last: bool,
    by_parent: dict[int | None, list[dict[str, Any]]],
    statuses: dict[int, str],
    tags_by_ref: dict[int, list[str]],
    out: list[str],
) -> None:
    """Recursive tree render. Mutates ``out`` in place."""
    branch = "" if depth == 0 else ("└─ " if is_last else "├─ ")
    line = prefix + branch
    first_line = (node["title"] or "").split("\n", 1)[0]
    status = statuses.get(node["id"], "open")
    tags = tags_by_ref.get(node["id"], [])
    claimed = next((t for t in tags if t.startswith("claimed-by:")), None)
    waiting = next((t for t in tags if t.startswith("waiting-for:")), None)
    asking = any(t == "asking-reto" or t.startswith("asking-reto:") for t in tags)
    icon = _status_icon(status, claimed=claimed, waiting=waiting, asking=asking)
    line += f"#{node['id']} {icon} {first_line}"
    out.append(line)
    children = by_parent.get(node["id"], [])
    if not children:
        return
    child_prefix = prefix + ("" if depth == 0 else ("    " if is_last else "│   "))
    for i, c in enumerate(children):
        _render_node(
            c,
            depth=depth + 1,
            prefix=child_prefix,
            is_last=(i == len(children) - 1),
            by_parent=by_parent,
            statuses=statuses,
            tags_by_ref=tags_by_ref,
            out=out,
        )


def _status_icon(
    status: str,
    *,
    claimed: str | None = None,
    waiting: str | None = None,
    asking: bool = False,
) -> str:
    """Render a one-glance status marker matching the plan's tree view."""
    if status == "done":
        return "✓"
    if status == "won't-do":
        return "✗"
    if status == "paused":
        return "⏸"
    if status == "auto-timeout":
        return "!"
    if claimed:
        return f"◀ {claimed}"
    if waiting:
        return f"⏸ {waiting}"
    if asking:
        return "⏸ asking-reto"
    if status == "blocked":
        return "⏸ blocked"
    if status == "doing":
        return "▶"
    return "○"  # open / unknown — doable candidate


# ── view: doable ───────────────────────────────────────────────────


def render_doable(
    store: Store,
    *,
    under: int | None = None,
    limit: int = 20,
) -> Response:
    """Flat list of doable leaves with inline ancestry.

    "Doable" means: leaf (no live children), STATUS open / doing,
    no live blocked-by edges, no ``waiting-for:`` or ``asking-reto``
    tag, no paused ancestor. Ordering follows the plan: least-picked
    strategic first, then per-leaf priority, then sibling order.
    """
    leaves = _fetch_doable(store, under=under, limit=limit)
    if not leaves:
        body = "no doable leaves"
        body += render_next_section(
            [
                ("search(kind='todo', view='waiting')", "see what's waiting"),
                ("search(kind='todo', view='blocked')", "see what's blocked"),
                ("search(kind='todo', view='roots')", "see strategic dashboard"),
            ]
        )
        return Response(body=body)
    lines = [f"# {len(leaves)} doable leaf{'' if len(leaves) == 1 else 'ves'}"]
    for leaf in leaves:
        first_line = (leaf["title"] or "").split("\n", 1)[0]
        if len(first_line) > 76:
            first_line = first_line[:76].rstrip() + "…"
        lines.append("")
        lines.append(f"#{leaf['id']:<4} {first_line}")
        chain = leaf["ancestry"]
        if chain:
            crumb = " / ".join(a["title"][:30] for a in chain)
            lines.append(f"      ↳ {crumb}")
    totals = _doable_counters(store)
    lines.append("")
    lines.append(
        f"Waiting: {totals['waiting']} | Blocked: {totals['blocked']} | "
        f"Asking: {totals['asking']} | Open: {totals['open']}"
    )
    body = "\n".join(lines)
    body += render_next_section(
        [
            ("get(kind='todo', id=N, view='tree')", "see the subtree above a leaf"),
            (
                "tag(kind='todo', id=N, add=['claimed-by:<self>'])",
                "claim a leaf",
            ),
            (
                "tag(kind='todo', id=N, add=['STATUS:done'])",
                "mark a leaf done",
            ),
        ]
    )
    return Response(body=body)


def _fetch_doable(
    store: Store,
    *,
    under: int | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Return doable leaves (with ancestry chain) ordered per the plan."""
    under_clause = ""
    under_params: tuple[Any, ...] = ()
    if under is not None:
        under_clause = (
            "AND EXISTS ("
            "  WITH RECURSIVE up(ref_id, parent_id) AS ("
            "    SELECT ref_id, parent_id FROM refs WHERE ref_id = r.ref_id"
            "    UNION ALL"
            "    SELECT p.ref_id, p.parent_id FROM refs p"
            "      JOIN up ON p.ref_id = up.parent_id"
            "  )"
            "  SELECT 1 FROM up WHERE ref_id = %s"
            ")"
        )
        under_params = (under,)
    sql = f"""
        WITH RECURSIVE
          strat AS (
            SELECT r.ref_id
              FROM refs r
             WHERE r.kind = 'todo' AND r.deleted_at IS NULL
               AND r.parent_id IS NULL
               AND EXISTS (
                   SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                    WHERE rt.ref_id = r.ref_id
                      AND t.namespace = 'OPEN'
                      AND t.value = 'level:strategic'
               )
          ),
          subtree AS (
            SELECT s.ref_id AS ref_id, s.ref_id AS strategic_id FROM strat s
            UNION ALL
            SELECT r.ref_id, st.strategic_id
              FROM refs r JOIN subtree st ON r.parent_id = st.ref_id
             WHERE r.kind = 'todo' AND r.deleted_at IS NULL
          ),
          picks AS (
            SELECT st.strategic_id, count(e.event_id) AS picks_7d
              FROM subtree st
              LEFT JOIN ref_events e ON e.ref_id = st.ref_id
                                    AND e.event = 'status:done'
                                    AND e.ts >= now() - interval '7 days'
             GROUP BY st.strategic_id
          ),
          ancestry_paused AS (
            -- ref_ids whose ancestor chain contains a paused branch.
            SELECT DISTINCT r.ref_id
              FROM refs r
             WHERE EXISTS (
                 WITH RECURSIVE up(ref_id, parent_id) AS (
                     SELECT ref_id, parent_id FROM refs WHERE ref_id = r.ref_id
                     UNION ALL
                     SELECT p.ref_id, p.parent_id FROM refs p
                       JOIN up ON p.ref_id = up.parent_id
                 )
                 SELECT 1 FROM up u
                   JOIN ref_tags rt ON rt.ref_id = u.ref_id
                   JOIN tags t ON t.tag_id = rt.tag_id
                  WHERE t.namespace = 'STATUS' AND t.value = 'paused'
             )
          )
        SELECT
          r.ref_id, r.title, COALESCE(st.strategic_id, 0) AS strategic_id,
          COALESCE(p.picks_7d, 0) AS picks_7d,
          COALESCE(
            (SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
              WHERE rt.ref_id = r.ref_id AND t.namespace = 'PRIO' LIMIT 1),
            'zzz'
          ) AS prio
          FROM refs r
          LEFT JOIN subtree st ON st.ref_id = r.ref_id
          LEFT JOIN picks p ON p.strategic_id = st.strategic_id
         WHERE r.kind = 'todo' AND r.deleted_at IS NULL
           AND COALESCE(
                 (SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                   WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS' LIMIT 1),
                 'open'
               ) IN ('open', 'doing')
           AND NOT EXISTS (
               SELECT 1 FROM refs c WHERE c.parent_id = r.ref_id
                                      AND c.deleted_at IS NULL
           )
           AND NOT EXISTS (
               SELECT 1 FROM links l JOIN refs b ON b.ref_id = l.dst_ref_id
                WHERE l.src_ref_id = r.ref_id
                  AND l.relation = 'blocked-by'
                  AND b.deleted_at IS NULL
                  AND COALESCE(
                        (SELECT t.value FROM ref_tags rt2 JOIN tags t ON t.tag_id = rt2.tag_id
                          WHERE rt2.ref_id = b.ref_id AND t.namespace = 'STATUS' LIMIT 1),
                        'open'
                      ) NOT IN ('done', 'won''t-do')
           )
           AND NOT EXISTS (
               SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                WHERE rt.ref_id = r.ref_id
                  AND t.namespace = 'OPEN'
                  AND (t.value LIKE 'waiting-for:%%' OR t.value = 'asking-reto'
                       OR t.value LIKE 'asking-reto:%%')
           )
           AND NOT EXISTS (
               SELECT 1 FROM ancestry_paused ap WHERE ap.ref_id = r.ref_id
           )
           {under_clause}
         ORDER BY picks_7d ASC, prio ASC, r.ref_id ASC
         LIMIT %s
    """
    with store.pool.connection() as conn:
        rows = conn.execute(sql, under_params + (limit,)).fetchall()
    out: list[dict[str, Any]] = []
    for ref_id, title, _strategic_id, _picks, _prio in rows:
        chain = _ancestor_chain(store, int(ref_id))
        # Trim leaf itself from breadcrumb (rendered separately).
        crumbs = chain[:-1] if chain else []
        out.append(
            {
                "id": int(ref_id),
                "title": title,
                "ancestry": crumbs,
            }
        )
    return out


def _doable_counters(store: Store) -> dict[str, int]:
    """Quick counts for the doable view's footer line."""
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT
              count(*) FILTER (
                WHERE COALESCE(
                  (SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                    WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS' LIMIT 1),
                  'open'
                ) IN ('open', 'doing')
              ) AS open,
              count(*) FILTER (
                WHERE EXISTS (
                  SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                   WHERE rt.ref_id = r.ref_id AND t.namespace = 'OPEN'
                     AND t.value LIKE 'waiting-for:%%'
                )
              ) AS waiting,
              count(*) FILTER (
                WHERE EXISTS (
                  SELECT 1 FROM links l JOIN refs b ON b.ref_id = l.dst_ref_id
                   WHERE l.src_ref_id = r.ref_id AND l.relation = 'blocked-by'
                     AND b.deleted_at IS NULL
                )
              ) AS blocked,
              count(*) FILTER (
                WHERE EXISTS (
                  SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                   WHERE rt.ref_id = r.ref_id AND t.namespace = 'OPEN'
                     AND (t.value = 'asking-reto' OR t.value LIKE 'asking-reto:%%')
                )
              ) AS asking
              FROM refs r
             WHERE r.kind = 'todo' AND r.deleted_at IS NULL
            """,
        ).fetchone()
    if row is None:
        return {"open": 0, "waiting": 0, "blocked": 0, "asking": 0}
    return {
        "open": int(row[0] or 0),
        "waiting": int(row[1] or 0),
        "blocked": int(row[2] or 0),
        "asking": int(row[3] or 0),
    }


# ── view: waiting / blocked / asking-reto ──────────────────────────


def render_waiting(store: Store) -> Response:
    """Leaves carrying any ``waiting-for:*`` tag."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id, r.title,
                   array_agg(t.value) AS waits
              FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.kind = 'todo' AND r.deleted_at IS NULL
               AND t.namespace = 'OPEN'
               AND t.value LIKE 'waiting-for:%%'
             GROUP BY r.ref_id, r.title
             ORDER BY r.ref_id DESC
             LIMIT 50
            """,
        ).fetchall()
    if not rows:
        return Response(body="no waiting-for leaves")
    lines = [f"# {len(rows)} waiting"]
    for ref_id, title, waits in rows:
        first_line = (title or "").split("\n", 1)[0]
        wait_str = ", ".join(sorted(w for w in waits))
        lines.append(f"#{int(ref_id):<4} {first_line:<60}  {wait_str}")
    return Response(body="\n".join(lines))


def render_blocked(store: Store) -> Response:
    """Leaves with at least one non-done ``blocked-by`` link."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id, r.title,
                   array_agg(l.dst_ref_id ORDER BY l.dst_ref_id) AS blockers
              FROM refs r
              JOIN links l ON l.src_ref_id = r.ref_id AND l.relation = 'blocked-by'
              JOIN refs b ON b.ref_id = l.dst_ref_id
             WHERE r.kind = 'todo' AND r.deleted_at IS NULL
               AND b.deleted_at IS NULL
               AND COALESCE(
                     (SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                       WHERE rt.ref_id = b.ref_id AND t.namespace = 'STATUS' LIMIT 1),
                     'open'
                   ) NOT IN ('done', 'won''t-do')
             GROUP BY r.ref_id, r.title
             ORDER BY r.ref_id DESC
             LIMIT 50
            """,
        ).fetchall()
    if not rows:
        return Response(body="no blocked leaves")
    lines = [f"# {len(rows)} blocked"]
    for ref_id, title, blockers in rows:
        first_line = (title or "").split("\n", 1)[0]
        blocker_ids = ", ".join(f"#{int(b)}" for b in blockers)
        lines.append(f"#{int(ref_id):<4} {first_line:<60}  blocked-by {blocker_ids}")
    return Response(body="\n".join(lines))


def render_asking_reto(store: Store) -> Response:
    """Leaves carrying the ``asking-reto`` / ``asking-reto:*`` open tag.

    The render is intentionally compact — chatter renders this into
    the preamble as a "Pending asks" block (slice 2).
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT r.ref_id, r.title, r.created_at
              FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.kind = 'todo' AND r.deleted_at IS NULL
               AND t.namespace = 'OPEN'
               AND (t.value = 'asking-reto' OR t.value LIKE 'asking-reto:%%')
               AND COALESCE(
                     (SELECT t2.value FROM ref_tags rt2 JOIN tags t2 ON t2.tag_id = rt2.tag_id
                       WHERE rt2.ref_id = r.ref_id AND t2.namespace = 'STATUS' LIMIT 1),
                     'open'
                   ) NOT IN ('done', 'won''t-do')
             ORDER BY r.created_at DESC
             LIMIT 50
            """,
        ).fetchall()
    if not rows:
        return Response(body="no pending asks")
    lines = [f"# {len(rows)} pending ask{'' if len(rows) == 1 else 's'}"]
    lines.append("")
    for ref_id, title, _created_at in rows:
        first_line = (title or "").split("\n", 1)[0]
        if len(first_line) > 70:
            first_line = first_line[:70].rstrip() + "…"
        lines.append(f"#{int(ref_id):<4} {first_line}")
    body = "\n".join(lines)
    body += render_next_section(
        [
            ("get(kind='todo', id=N)", "read the ask + its ancestry"),
            (
                "tag(kind='todo', id=N, add=['STATUS:done'])",
                "resolve the ask",
            ),
        ]
    )
    return Response(body=body)


# ── walk-on-read ancestry (used by handler.get) ────────────────────


def render_ancestry_section(store: Store, ref_id: int) -> str:
    """Render the breadcrumb section appended to ``get(kind='todo', id=N)``.

    Returns ``""`` when the ref is a root (nothing above to show).
    Otherwise prints the title chain root → leaf with a level marker
    on each level. The renderer keeps things short — depth-10 wall
    means a sane chain is ~5-6 lines of output.
    """
    chain = _ancestor_chain(store, ref_id)
    if len(chain) <= 1:
        return ""
    lines = ["", "Ancestry:"]
    for i, node in enumerate(chain):
        depth = i
        indent = "  " * depth
        level = node["level"]
        level_tag = (
            level.removeprefix("level:") if isinstance(level, str) else "subtask"
        )
        title = node["title"] or ""
        marker = "→" if node["id"] == ref_id else "└─"
        lines.append(f"{indent}{marker} #{node['id']} [{level_tag}] {title}")
    return "\n".join(lines)


__all__ = [
    "render_ancestry_section",
    "render_asking_reto",
    "render_blocked",
    "render_doable",
    "render_roots",
    "render_strategic",
    "render_tree",
    "render_waiting",
]


# Surface BadInput so static analyzers don't complain when the
# handler only re-uses it through this module.
_ = BadInput
