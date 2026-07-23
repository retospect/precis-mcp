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
from precis.handlers._todo_guards import todo_root_sql
from precis.response import Response
from precis.utils import handle_registry
from precis.utils.next_block import render_next_section

if TYPE_CHECKING:
    from precis.store import Store


def _h(ref_id: int | str, kind: str = "todo") -> str:
    """ADR 0036 universal handle for a todo/job row (e.g. ``td158``)."""
    return handle_registry.format_handle(kind, int(ref_id))


# ── doable / dispatch exclusion registry ──────────────────────────


#: Open-tag forms that pull a leaf out of ``view='doable'`` AND out of
#: the dispatch worker's candidate query. The shared registry keeps
#: the doable filter and the dispatch filter in lock-step — adding a
#: new "robot stay away" reason is a one-line append here, no SQL
#: edits in two places.
#:
#: Each entry is one of two shapes:
#:
#: * **bare value** (no trailing ``:``) — matched exactly against
#:   ``tags.value``. Examples: ``halt``, ``ask-user``.
#: * **prefix value** (ends in ``:``) — matched as a SQL LIKE prefix.
#:   Examples: ``waiting-for:`` covers ``waiting-for:owner``,
#:   ``waiting-for:paper:10.x/y1``, etc.
#:
#: Slice-5+:
#:
#: * ``halt`` / ``halt:<reason>`` — explicit "robot don't touch this"
#:   marker. A worker source can ADD it (escalation) but only the
#:   owner can REMOVE it — see
#:   :func:`precis.handlers._todo_guards.check_halt_remove`. The
#:   ``halt:<reason>`` prefix form lets the runtime self-halt with a
#:   reason (``halt:cost-cap``, ``halt:tick-cap``, ``halt:planner-stuck``)
#:   so the attention view can show *why* without a separate lookup.
#: * ``ask-user`` / ``ask-user:<who-or-question>`` — yield to a human.
#:   Bare = "any human will do"; ``ask-user:<handle>`` = specific person;
#:   ``ask-user:<freeform>`` = the question itself, so the attention
#:   view can render it inline. (The pre-rename ``asking-reto`` alias
#:   was removed 2026-06-19 — see
#:   ``docs/design/user-identity-and-ask-routing.md``.)
_DOABLE_EXCLUSION_TAGS: tuple[str, ...] = (
    "halt",
    "halt:",
    "waiting-for:",
    "ask-user",
    "ask-user:",
    "child-failed:",
)


def _doable_exclusion_clause(tag_alias: str = "t") -> str:
    """Return the SQL OR clause matching every exclusion tag.

    The returned expression is parenthesised, suitable for embedding
    in a ``NOT EXISTS (... AND <clause>)`` shape. The caller is
    responsible for the surrounding ``ref_tags`` ⋈ ``tags`` join and
    the ``namespace = 'OPEN'`` filter.

    Centralising the clause means the doable view, the dispatch
    candidate query, and any future "skip robot-stay-away leaves"
    surface share the same logic — drift between them is impossible.
    """
    parts: list[str] = []
    for t in _DOABLE_EXCLUSION_TAGS:
        if t.endswith(":"):
            parts.append(f"{tag_alias}.value LIKE '{t}%%'")
        else:
            parts.append(f"{tag_alias}.value = '{t}'")
    return "(" + " OR ".join(parts) + ")"


#: Narrow subset of ``_DOABLE_EXCLUSION_TAGS`` that a planner-coroutine
#: parent is allowed to *bypass* (after a cooldown) when it's the only
#: thing keeping the parent from re-becoming a dispatch candidate — see
#: ``workers/dispatch.py``'s child-liveness ``NOT EXISTS`` blocks.
#: ``ask-user`` / ``waiting-for:`` mean "parked, would like an answer",
#: not "stop everything" — the planner can still judge there's other
#: useful work to do. Deliberately excludes ``halt`` / ``halt:`` (an
#: explicit, owner-only-to-lift "robot stay away") and
#: ``child-failed:`` (needs a retry/give-up decision, not "work
#: around it") — those keep their current absolute-block behaviour.
_REPLAN_BYPASS_TAGS: tuple[str, ...] = (
    "ask-user",
    "ask-user:",
    "waiting-for:",
)


def _replan_bypass_clause(tag_alias: str = "t") -> str:
    """Return the SQL OR clause matching the cooldown-bypassable tags.

    Same shape as :func:`_doable_exclusion_clause` but scoped to
    :data:`_REPLAN_BYPASS_TAGS` only — used to detect whether a
    parked child todo is a candidate for the planner-replan cooldown
    bypass (as opposed to a hard ``halt`` / ``child-failed:`` block).

    Callers MUST also check :func:`_hard_block_clause` is absent before
    treating a child as bypassable — a child can carry both an
    ``ask-user:`` tag and a ``halt:`` tag at once (e.g. the planner
    escalates an existing parked child by adding ``halt:`` without
    first removing ``ask-user:``), and the hard block must win.
    """
    parts: list[str] = []
    for t in _REPLAN_BYPASS_TAGS:
        if t.endswith(":"):
            parts.append(f"{tag_alias}.value LIKE '{t}%%'")
        else:
            parts.append(f"{tag_alias}.value = '{t}'")
    return "(" + " OR ".join(parts) + ")"


#: The complement of ``_REPLAN_BYPASS_TAGS`` within ``_DOABLE_EXCLUSION_TAGS``
#: — tags that must keep blocking a planner's re-candidacy unconditionally,
#: cooldown or not. Checked separately (not just "absence of a bypass tag")
#: because a single child can carry BOTH a bypass tag and a hard-block tag
#: at once (see ``_replan_bypass_clause``'s docstring) — presence of either
#: of these must always win over any bypass tag on the same child.
_HARD_BLOCK_TAGS: tuple[str, ...] = (
    "halt",
    "halt:",
    "child-failed:",
)


def _hard_block_clause(tag_alias: str = "t") -> str:
    """Return the SQL OR clause matching the never-bypassable tags.

    Same shape as :func:`_doable_exclusion_clause` / :func:`_replan_bypass_clause`
    but scoped to :data:`_HARD_BLOCK_TAGS` — a child carrying one of
    these always blocks its parent's re-candidacy, regardless of any
    ``ask-user:``/``waiting-for:`` tag also present and regardless of
    the replan cooldown.
    """
    parts: list[str] = []
    for t in _HARD_BLOCK_TAGS:
        if t.endswith(":"):
            parts.append(f"{tag_alias}.value LIKE '{t}%%'")
        else:
            parts.append(f"{tag_alias}.value = '{t}'")
    return "(" + " OR ".join(parts) + ")"


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
            f"""
            WITH RECURSIVE
              strat AS (
                SELECT r.ref_id
                  FROM refs r
                 WHERE r.kind = 'todo'
                   AND r.deleted_at IS NULL
                   AND {todo_root_sql("r")}
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
            f"""
            SELECT r.ref_id, r.title,
                   (SELECT t.value FROM ref_tags rt
                      JOIN tags t ON t.tag_id = rt.tag_id
                     WHERE rt.ref_id = r.ref_id
                       AND t.namespace = 'STATUS'
                     LIMIT 1) AS status
              FROM refs r
             WHERE r.kind = 'todo'
               AND r.deleted_at IS NULL
               AND {todo_root_sql("r")}
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
    # Quest reweighting (slice 2): discount a strategic's picks by the striving
    # weight it serves, so the "next pick" tilts toward hot-quest work. No-op
    # (empty map) when nothing is active.
    from precis.quest.reweight import server_weights_for_active_quests

    strat_weights = server_weights_for_active_quests(store)
    lines = [f"# {len(rows)} strategic root{'' if len(rows) == 1 else 's'}"]
    lines.append("")
    # Pick the lowest reweighted-picks active strategic; tie broken by ref_id.
    next_pick: int | None = None
    if active_ids:
        ordered = sorted(
            active_ids,
            key=lambda i: (
                (picks.get(i, 0) + 1) / (1 + strat_weights.get(i, 0.0)),
                i,
            ),
        )
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
            f"{_h(ref_id):<6} {first_line:<60}  7d: {n_picks:>2} picks{suffix}{marker}"
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
    # Slice-4: second panel — recurring roots (Watches). Orthogonal to
    # the picks-7d rotation; recurring is the schedule tier, not the
    # 1/N share, so a noisy cron can't crowd a strategic out.
    watches = _watches_panel_rows(store)
    if watches:
        lines.append("")
        lines.append("")
        lines.append(f"## Watches ({len(watches)} recurring)")
        lines.append("")
        for w in watches:
            first_line = (w["title"] or "").split("\n", 1)[0]
            if len(first_line) > 50:
                first_line = first_line[:50].rstrip() + "…"
            cron = w["cron"] or "(folder)"
            last = w["last_tick"] or "never"
            deliver_suffix = f"  → {w['deliver']}" if w.get("deliver") else ""
            lines.append(
                f"{_h(w['id']):<6} {first_line:<50}  cron: {cron:<14}  "
                f"last: {last}{deliver_suffix}"
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


def render_projects(store: Store) -> Response:
    """Dashboard of projects — todos that own a workspace.

    A *project* is the existing ``meta.workspace`` concept surfaced as
    a first-class list. There is no ``kind='project'``: a project is a
    todo where a workspace *originates* — it carries
    ``meta.workspace.path`` and its parent does not carry the same path
    (so descendants that merely inherited the cascade aren't listed as
    separate projects). One row per project with its slug, on-disk
    path + format, the count of open todos across its subtree, the
    file count on disk, and the first line of its ``extra.brief``.
    """
    import os

    precis_root = os.environ.get("PRECIS_ROOT", "")
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id, r.title,
                   r.meta->'workspace'->>'path'    AS path,
                   r.meta->'workspace'->>'format'  AS format,
                   r.meta->'workspace'->>'brief'   AS brief
              FROM refs r
             WHERE r.kind = 'todo'
               AND r.deleted_at IS NULL
               AND r.meta->'workspace'->>'path' IS NOT NULL
               AND (
                   r.parent_id IS NULL
                   OR (SELECT p.meta->'workspace'->>'path' FROM refs p
                         WHERE p.ref_id = r.parent_id)
                      IS DISTINCT FROM r.meta->'workspace'->>'path'
               )
             ORDER BY r.ref_id
            """,
        ).fetchall()
        projects: list[dict[str, Any]] = []
        for ref_id, title, path, fmt, brief in rows:
            ref_id = int(ref_id)
            open_count = conn.execute(
                """
                WITH RECURSIVE sub(ref_id) AS (
                    SELECT %s::bigint
                    UNION ALL
                    SELECT c.ref_id FROM refs c
                      JOIN sub ON c.parent_id = sub.ref_id
                     WHERE c.deleted_at IS NULL
                )
                SELECT count(*)
                  FROM sub s
                  JOIN refs r ON r.ref_id = s.ref_id AND r.kind = 'todo'
                 WHERE NOT EXISTS (
                    SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                     WHERE rt.ref_id = s.ref_id
                       AND t.namespace = 'STATUS'
                       AND t.value IN ('done', 'won''t-do')
                 )
                """,
                (ref_id,),
            ).fetchone()[0]
            projects.append(
                {
                    "id": ref_id,
                    "title": title,
                    "path": path,
                    "format": fmt or "tex",
                    "brief": brief,
                    "open": int(open_count),
                    "files": _count_workspace_files(precis_root, path),
                }
            )

    if not projects:
        body = "no projects yet"
        body += render_next_section(
            [
                (
                    "put(kind='todo', text='Project goal', "
                    "tags=['level:strategic'], "
                    "meta={'workspace': {'path': 'projects/<slug>', "
                    "'format': 'tex', 'entrypoint': 'main.tex', "
                    "'brief': '<standing guidance>'}})",
                    "mint a project root (owner-only)",
                ),
            ]
        )
        return Response(body=body)

    lines = [f"# {len(projects)} project{'' if len(projects) == 1 else 's'}", ""]
    for p in projects:
        slug = p["path"].rstrip("/").split("/")[-1]
        first_line = (p["title"] or "").split("\n", 1)[0]
        if len(first_line) > 48:
            first_line = first_line[:48].rstrip() + "…"
        files = "?" if p["files"] is None else str(p["files"])
        lines.append(
            f"{_h(p['id']):<6} {slug:<20} {first_line:<48}  "
            f"open: {p['open']:>3}  files: {files:>3}  ({p['format']})"
        )
        lines.append(f"       {p['path']}")
        if p["brief"]:
            brief_line = p["brief"].split("\n", 1)[0]
            if len(brief_line) > 72:
                brief_line = brief_line[:72].rstrip() + "…"
            lines.append(f"       ↳ {brief_line}")
    body = "\n".join(lines)
    body += render_next_section(
        [
            (
                "get(kind='todo', id=N, view='tree')",
                "drill into a project's todo tree",
            ),
            (
                "search(tags=['project:<slug>'])",
                "the full cross-kind project surface",
            ),
        ]
    )
    return Response(body=body)


def _count_workspace_files(precis_root: str, path: str | None) -> int | None:
    """Count non-dotfiles under a workspace dir, or None if unresolvable.

    Best-effort: returns ``None`` (rendered as ``?``) when ``PRECIS_ROOT``
    is unset or the workspace dir doesn't exist on this host yet (init
    hasn't fired). Walks one level into the known layout subdirs rather
    than a full recursive walk — enough for an at-a-glance count.
    """
    if not precis_root or not path:
        return None
    if path.startswith("/") or ".." in path.split("/"):
        return None
    from pathlib import Path

    root = (Path(precis_root) / path).resolve()
    if not root.is_dir():
        return None
    count = 0
    for sub in ("", "tex", "sections", "pics", "data"):
        sub_root = root / sub if sub else root
        if not sub_root.is_dir():
            continue
        for entry in sub_root.iterdir():
            if entry.is_file() and not entry.name.startswith("."):
                count += 1
    return count


def _watches_panel_rows(store: Store) -> list[dict[str, Any]]:
    """Return one row per non-umbrella recurring with its last tick.

    The umbrella folder (``meta.builtin='watches-root'``) is excluded
    from the listing — it's the parent, not a watch. Each row carries:

    * ``id`` — the recurring's ref id
    * ``title`` — for display
    * ``cron`` — the canonical cron string, or ``at:<iso>`` for a
      one-shot (ADR 0061), or ``None`` when folder
    * ``deliver`` — the push-delivery target (``meta.deliver.target``),
      or ``None`` for a queue-mode recurring
    * ``last_tick`` — ISO timestamp of the most recent ``spawn`` OR
      ``deliver`` event on this recurring, or ``None`` when it hasn't
      fired
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id, r.title,
                   COALESCE(r.meta->'schedule'->>'cron',
                            'at:' || (r.meta->'schedule'->>'at')) AS cron,
                   r.meta->'deliver'->>'target' AS deliver,
                   (SELECT max(e.ts)::text FROM ref_events e
                     WHERE e.ref_id = r.ref_id
                       AND e.source = 'schedule'
                       AND e.event IN ('spawn', 'deliver')) AS last_tick
              FROM refs r
             WHERE r.kind = 'todo' AND r.deleted_at IS NULL
               AND EXISTS (
                   SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                    WHERE rt.ref_id = r.ref_id
                      AND t.namespace = 'OPEN'
                      AND t.value = 'level:recurring'
               )
               AND (r.meta->>'builtin') IS NULL
             ORDER BY r.ref_id
            """,
        ).fetchall()
    return [
        {
            "id": int(r[0]),
            "title": r[1],
            "cron": r[2],
            "deliver": r[3],
            "last_tick": r[4],
        }
        for r in rows
    ]


def _active_strategic_ids(store: Store) -> list[int]:
    """Strategic roots that have at least one open leaf (= eligible for picks)."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            f"""
            WITH RECURSIVE
              strat AS (
                SELECT r.ref_id
                  FROM refs r
                 WHERE r.kind = 'todo' AND r.deleted_at IS NULL
                   AND {todo_root_sql("r")}
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
    """Strategic + tactical layer with leaf counts under each tactical.

    For each strategic root, list its direct children (the tactical
    tier) along with a ``[open/total]`` count of leaves anywhere in
    that tactical's subtree. ``open`` = status not in (done, won't-do).
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            f"""
            WITH RECURSIVE
              tac AS (
                SELECT t.ref_id AS tactical_id, t.title AS tactical_title,
                       t.parent_id AS strategic_id
                  FROM refs t
                 WHERE t.kind = 'todo' AND t.deleted_at IS NULL
                   AND EXISTS (
                       SELECT 1 FROM refs s
                        WHERE s.ref_id = t.parent_id
                          AND s.kind = 'todo' AND s.deleted_at IS NULL
                          AND {todo_root_sql("s")}
                          AND EXISTS (
                              SELECT 1 FROM ref_tags rt
                                JOIN tags tg ON tg.tag_id = rt.tag_id
                               WHERE rt.ref_id = s.ref_id
                                 AND tg.namespace = 'OPEN'
                                 AND tg.value = 'level:strategic'
                          )
                   )
              ),
              tac_subtree AS (
                SELECT tac.tactical_id, tac.tactical_id AS desc_id FROM tac
                UNION ALL
                SELECT ts.tactical_id, r.ref_id
                  FROM tac_subtree ts
                  JOIN refs r ON r.parent_id = ts.desc_id
                 WHERE r.kind = 'todo' AND r.deleted_at IS NULL
              ),
              leaves AS (
                SELECT ts.tactical_id, ts.desc_id AS leaf_id,
                       COALESCE(
                         (SELECT t.value FROM ref_tags rt
                            JOIN tags t ON t.tag_id = rt.tag_id
                           WHERE rt.ref_id = ts.desc_id
                             AND t.namespace = 'STATUS'
                           LIMIT 1),
                         'open'
                       ) AS status
                  FROM tac_subtree ts
                 WHERE NOT EXISTS (
                     SELECT 1 FROM refs c
                      WHERE c.parent_id = ts.desc_id
                        AND c.kind = 'todo'
                        AND c.deleted_at IS NULL
                 )
              )
            SELECT s.ref_id AS strategic_id, s.title AS strategic_title,
                   tac.tactical_id, tac.tactical_title,
                   COUNT(l.leaf_id) FILTER (
                       WHERE l.status NOT IN ('done', 'won''t-do')
                   ) AS open_count,
                   COUNT(l.leaf_id) AS total_count
              FROM refs s
              LEFT JOIN tac ON tac.strategic_id = s.ref_id
              LEFT JOIN leaves l ON l.tactical_id = tac.tactical_id
             WHERE s.kind = 'todo' AND s.deleted_at IS NULL
               AND {todo_root_sql("s")}
               AND EXISTS (
                   SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                    WHERE rt.ref_id = s.ref_id AND t.namespace = 'OPEN'
                      AND t.value = 'level:strategic'
               )
             GROUP BY s.ref_id, s.title, tac.tactical_id, tac.tactical_title
             ORDER BY s.ref_id, tac.tactical_id NULLS FIRST
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
            lines.append(f"{_h(strategic_id)} {first_line}")
            last_strategic = strategic_id
        if tactical_id is None:
            continue
        tac_first = (tac_title or "").split("\n", 1)[0]
        if len(tac_first) > 60:
            tac_first = tac_first[:60].rstrip() + "…"
        lines.append(
            f"  └─ {_h(tactical_id):<6} {tac_first:<60} "
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
    """Return ``[{id, parent_id, title, depth, kind}]`` for the subtree at root.

    The walk widens to ``kind IN ('todo', 'job')`` so child jobs (Slice
    5) surface under their parent todo in ``view='tree'``. The root
    itself must still be a ``kind='todo'`` — a job is execution detail,
    not a tree anchor — so the seed row carries the kind filter.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            WITH RECURSIVE walk(ref_id, parent_id, title, depth, kind) AS (
                SELECT ref_id, parent_id, title, 0, kind
                  FROM refs WHERE ref_id = %s
                              AND kind = 'todo'
                              AND deleted_at IS NULL
                UNION ALL
                SELECT r.ref_id, r.parent_id, r.title, w.depth + 1, r.kind
                  FROM refs r
                  JOIN walk w ON r.parent_id = w.ref_id
                 WHERE r.kind IN ('todo', 'job')
                   AND r.deleted_at IS NULL
                   AND w.depth < 10
            )
            SELECT ref_id, parent_id, title, depth, kind FROM walk
             ORDER BY depth, ref_id
            """,
            (root_id,),
        ).fetchall()
    return [
        {
            "id": int(r[0]),
            "parent_id": r[1],
            "title": r[2],
            "depth": int(r[3]),
            "kind": r[4],
        }
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
    asking = any(t == "ask-user" or t.startswith("ask-user:") for t in tags)
    # Slice-5: distinguish kind='job' rows so the operator sees
    # execution attempts as different from intent nodes. ⚙ is the
    # gear glyph the Watches panel already uses for scheduler-y
    # things; we reuse it for jobs.
    kind_marker = "⚙ " if node.get("kind") == "job" else ""
    icon = _status_icon(status, claimed=claimed, waiting=waiting, asking=asking)
    line += (
        f"{_h(node['id'], node.get('kind') or 'todo')} {kind_marker}{icon} {first_line}"
    )
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
        return "⏸ ask-user"
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
    no live blocked-by edges, no ``waiting-for:`` or ``ask-user``
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
        lines.append(f"{_h(leaf['id']):<6} {first_line}")
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
    # Quest reweighting (slice 2): a strategic root that `serves` an active
    # quest gets its 7-day pick count discounted by the striving weight, so it
    # surfaces earlier in the least-picked-first rotation. No-op when no quest
    # is active (empty map → COALESCE(sv.w,0)=0 → identical ordering).
    from precis.quest.reweight import server_weights_for_active_quests

    strat_weights = server_weights_for_active_quests(store)
    weight_sids = list(strat_weights)
    weight_vals = [strat_weights[s] for s in weight_sids]

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
    exclusion_clause = _doable_exclusion_clause()
    sql = f"""
        WITH RECURSIVE
          strat AS (
            SELECT r.ref_id
              FROM refs r
             WHERE r.kind = 'todo' AND r.deleted_at IS NULL
               AND {todo_root_sql("r")}
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
          ),
          sv(strategic_id, w) AS (
            -- quest striving weight per strategic root (slice 2 reweighting).
            SELECT s, w FROM unnest(%s::bigint[], %s::float8[]) AS x(s, w)
          )
        SELECT
          r.ref_id, r.title, COALESCE(st.strategic_id, 0) AS strategic_id,
          COALESCE(p.picks_7d, 0) AS picks_7d,
          COALESCE(r.prio, 5) AS prio
          FROM refs r
          LEFT JOIN subtree st ON st.ref_id = r.ref_id
          LEFT JOIN picks p ON p.strategic_id = st.strategic_id
          LEFT JOIN sv ON sv.strategic_id = st.strategic_id
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
               -- ``_DOABLE_EXCLUSION_TAGS`` registry: every "robot
               -- stay away" reason in one place — waiting-for / ask-
               -- user / child-failed (Slice 5 bubble) / halt (explicit
               -- owner-applied marker). Adding a new exclusion form
               -- means appending to the registry above, no SQL edits
               -- needed here or in dispatch.
               SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                WHERE rt.ref_id = r.ref_id
                  AND t.namespace = 'OPEN'
                  AND {exclusion_clause}
           )
           AND NOT EXISTS (
               SELECT 1 FROM ancestry_paused ap WHERE ap.ref_id = r.ref_id
           )
           -- Slice 4: the recurring umbrella itself (level:recurring root)
           -- is a folder, not an action; its spawned children are normal
           -- subtasks. Skip the umbrella by tag — the spawned children
           -- pass through because they carry ``level:subtask`` (or no
           -- explicit level), not ``level:recurring``.
           AND NOT EXISTS (
               SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                WHERE rt.ref_id = r.ref_id
                  AND t.namespace = 'OPEN'
                  AND t.value = 'level:recurring'
           )
           {under_clause}
         ORDER BY prio ASC,
                  ((picks_7d + 1) / (1 + COALESCE(sv.w, 0))) ASC,
                  r.ref_id ASC
         LIMIT %s
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            sql, (weight_sids, weight_vals) + under_params + (limit,)
        ).fetchall()
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
                     AND (t.value = 'ask-user' OR t.value LIKE 'ask-user:%%')
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


# ── view: waiting / blocked / ask-user ───────────────────────────


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
        lines.append(f"{_h(ref_id):<6} {first_line:<60}  {wait_str}")
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
        blocker_ids = ", ".join(_h(b) for b in blockers)
        lines.append(f"{_h(ref_id):<6} {first_line:<60}  blocked-by {blocker_ids}")
    return Response(body="\n".join(lines))


def render_ask_user(store: Store) -> Response:
    """Leaves carrying the ``ask-user`` / ``ask-user:*`` open tag.

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
               AND (t.value = 'ask-user' OR t.value LIKE 'ask-user:%%')
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
        lines.append(f"{_h(ref_id):<6} {first_line}")
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


# ── view: attention (ask-user + child-failed parents) ──────────


def render_attention(store: Store) -> Response:
    """Union of every signal that needs the owner's attention.

    Signal sources (Slice 5+):

    * **ask-user** leaves — work parked on the owner's Discord reply
      (Slice 1; ``view='ask-user'`` covers this in isolation).
    * **child-failed parents** — todos carrying ``child-failed:<job_id>``
      open tags because a child ``kind='job'`` ref hit
      ``STATUS:failed``. The owner (asa-bot) decides next move:
      retry, switch executor, ask the user.
    * **halted leaves** — todos carrying the explicit ``halt`` open
      tag (owner-applied "robot stay away", or worker-applied
      escalation). The doable view skips them entirely so they'd
      otherwise vanish from the rotation; surfacing them here keeps
      them visible until the owner lifts the tag.
    * Future tags (``escalated:*``, ``human-review:*``) slot into the
      same renderer with one more ``LIKE`` clause.

    The chatter preamble renders this alongside the doable queue
    so the owner sees all of "what needs me" in one block. ``view='doable'``
    excludes all four signal classes via the
    ``_DOABLE_EXCLUSION_TAGS`` registry, so the two surfaces are
    disjoint by construction.
    """
    asks = _attention_ask_user(store)
    child_failed = _attention_child_failed(store)
    halted = _attention_halted(store)
    total = len(asks) + len(child_failed) + len(halted)
    if total == 0:
        body = "no todos need attention"
        body += render_next_section(
            [
                ("search(kind='todo', view='doable')", "see the doable queue"),
                ("search(kind='todo', view='roots')", "see strategic dashboard"),
            ]
        )
        return Response(body=body)
    lines: list[str] = [f"# {total} todo{'' if total == 1 else 's'} need attention"]
    if asks:
        lines.append("")
        lines.append(f"## Ask user ({len(asks)})")
        lines.append("")
        for a in asks:
            first = (a["title"] or "").split("\n", 1)[0]
            if len(first) > 76:
                first = first[:76].rstrip() + "…"
            lines.append(f"{_h(a['id']):<6} {first}")
    if child_failed:
        lines.append("")
        lines.append(f"## Child-failed parents ({len(child_failed)})")
        lines.append("")
        for f in child_failed:
            first = (f["title"] or "").split("\n", 1)[0]
            if len(first) > 70:
                first = first[:70].rstrip() + "…"
            lines.append(f"{_h(f['id']):<6} {first}")
            for tag in f["child_failed_tags"]:
                # Strip ``child-failed:`` prefix → the bare job id.
                job_id = tag.removeprefix("child-failed:")
                lines.append(
                    f"      {_h(job_id, 'job')}: {f['reasons'].get(job_id, '(no event chunk yet)')}"
                )
    if halted:
        lines.append("")
        lines.append(f"## Halted ({len(halted)})")
        lines.append("")
        for h in halted:
            first = (h["title"] or "").split("\n", 1)[0]
            if len(first) > 76:
                first = first[:76].rstrip() + "…"
            lines.append(f"{_h(h['id']):<6} {first}")
    body = "\n".join(lines)
    body += render_next_section(
        [
            (
                "get(kind='todo', id=N)",
                "read the leaf + its ancestry chain to triage",
            ),
            (
                "tag(kind='todo', id=N, remove=['child-failed:<job_id>'])",
                "after deciding to retry: clear the bubble flag",
            ),
            (
                "tag(kind='todo', id=N, remove=['halt'])",
                "lift the halt (owner-only; resumes doable / dispatch)",
            ),
        ]
    )
    return Response(body=body)


def _attention_ask_user(store: Store) -> list[dict[str, Any]]:
    """Same shape as ``render_ask_user`` data, but as dicts.

    Splitting the data side from the render side keeps
    ``render_attention`` from re-querying the same rows
    ``render_ask_user`` already knows how to fetch.
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
               AND (t.value = 'ask-user' OR t.value LIKE 'ask-user:%%')
               AND COALESCE(
                     (SELECT t2.value FROM ref_tags rt2 JOIN tags t2 ON t2.tag_id = rt2.tag_id
                       WHERE rt2.ref_id = r.ref_id AND t2.namespace = 'STATUS' LIMIT 1),
                     'open'
                   ) NOT IN ('done', 'won''t-do')
             ORDER BY r.created_at DESC
             LIMIT 50
            """,
        ).fetchall()
    return [{"id": int(r[0]), "title": r[1], "created_at": r[2]} for r in rows]


def _attention_halted(store: Store) -> list[dict[str, Any]]:
    """Open todos carrying ``halt`` or ``halt:<reason>``.

    Bare ``halt`` = owner-applied "robot stay away."
    ``halt:<reason>`` = system-applied self-halt with a reason
    (``halt:cost-cap``, ``halt:tick-cap``, ``halt:planner-stuck``).
    Both surface here; the reason (if present) is rendered alongside
    the title so the operator sees *why* without a follow-up lookup.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id, r.title, r.created_at,
                   array_agg(t.value ORDER BY t.value) AS halt_tags
              FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.kind = 'todo' AND r.deleted_at IS NULL
               AND t.namespace = 'OPEN'
               AND (t.value = 'halt' OR t.value LIKE 'halt:%%')
               AND COALESCE(
                     (SELECT t2.value FROM ref_tags rt2 JOIN tags t2 ON t2.tag_id = rt2.tag_id
                       WHERE rt2.ref_id = r.ref_id AND t2.namespace = 'STATUS' LIMIT 1),
                     'open'
                   ) NOT IN ('done', 'won''t-do')
             GROUP BY r.ref_id, r.title, r.created_at
             ORDER BY r.created_at DESC
             LIMIT 50
            """,
        ).fetchall()
    out: list[dict[str, Any]] = []
    for ref_id, title, created_at, halt_tags in rows:
        reasons: list[str] = []
        for t in halt_tags or []:
            t_str = str(t)
            if t_str.startswith("halt:"):
                reasons.append(t_str.removeprefix("halt:"))
        out.append(
            {
                "id": int(ref_id),
                "title": title,
                "created_at": created_at,
                "reasons": reasons,
            }
        )
    return out


def _attention_child_failed(store: Store) -> list[dict[str, Any]]:
    """Parents tagged ``child-failed:<job_id>``. One row per parent.

    For each parent collects every ``child-failed:<job_id>`` tag,
    plus the most recent ``job_event`` chunk text on each named
    job (truncated) so the digest shows *why* without an extra
    round-trip.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id, r.title,
                   array_agg(t.value ORDER BY t.value) AS bubble_tags
              FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.kind = 'todo' AND r.deleted_at IS NULL
               AND t.namespace = 'OPEN'
               AND t.value LIKE 'child-failed:%%'
               AND COALESCE(
                     (SELECT t2.value FROM ref_tags rt2 JOIN tags t2 ON t2.tag_id = rt2.tag_id
                       WHERE rt2.ref_id = r.ref_id AND t2.namespace = 'STATUS' LIMIT 1),
                     'open'
                   ) NOT IN ('done', 'won''t-do')
             GROUP BY r.ref_id, r.title
             ORDER BY r.ref_id DESC
             LIMIT 50
            """,
        ).fetchall()
    out: list[dict[str, Any]] = []
    for ref_id, title, bubble_tags in rows:
        tag_list = [str(t) for t in bubble_tags or []]
        job_ids: list[int] = []
        for t in tag_list:
            suffix = t.removeprefix("child-failed:")
            try:
                job_ids.append(int(suffix))
            except ValueError:
                continue
        reasons = _latest_job_event_reasons(store, job_ids) if job_ids else {}
        out.append(
            {
                "id": int(ref_id),
                "title": title,
                "child_failed_tags": tag_list,
                "reasons": reasons,
            }
        )
    return out


def _latest_job_event_reasons(store: Store, job_ids: list[int]) -> dict[str, str]:
    """Return ``{job_id_str: latest job_event text}`` for the given jobs.

    Truncated to ~120 chars per reason so the attention digest stays
    readable. Jobs without an event chunk silently drop out.
    """
    if not job_ids:
        return {}
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT ON (c.ref_id) c.ref_id, c.text
              FROM chunks c
             WHERE c.ref_id = ANY(%s)
               AND c.chunk_kind = 'job_event'
             ORDER BY c.ref_id, c.ord DESC
            """,
            (job_ids,),
        ).fetchall()
    out: dict[str, str] = {}
    for ref_id, text in rows:
        s = (text or "").split("\n", 1)[0]
        if len(s) > 120:
            s = s[:120].rstrip() + "…"
        out[str(int(ref_id))] = s
    return out


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
        lines.append(f"{indent}{marker} {_h(node['id'])} [{level_tag}] {title}")
    return "\n".join(lines)


__all__ = [
    "render_ancestry_section",
    "render_ask_user",
    "render_attention",
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
