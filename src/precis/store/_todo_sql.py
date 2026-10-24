"""Pure SQL-fragment helpers for the todo tree, shared by handlers and workers.

Lives in ``store`` so ``precis.workers`` can use them without importing
``precis.handlers`` (import-linter contract in ``pyproject.toml``).
"""

from __future__ import annotations

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
#:   :func:`precis.todo.guards.check_halt_remove`. The
#:   ``halt:<reason>`` prefix form lets the runtime self-halt with a
#:   reason (``halt:cost-cap``, ``halt:tick-cap``, ``halt:planner-stuck``)
#:   so the attention view can show *why* without a separate lookup.
#: * ``ask-user`` / ``ask-user:<who-or-question>`` — yield to a human.
#:   Bare = "any human will do"; ``ask-user:<handle>`` = specific person;
#:   ``ask-user:<freeform>`` = the question itself, so the attention
#:   view can render it inline. (The pre-rename ``asking-reto`` alias
#:   was removed 2026-06-19 — see
#:   ``docs/backlog/identity-and-access.md``.)
_DOABLE_EXCLUSION_TAGS: tuple[str, ...] = (
    "halt",
    "halt:",
    "waiting-for:",
    "ask-user",
    "ask-user:",
    "child-failed:",
)


def _doable_exclusion_clause(tag_alias: str = "t", reftag_alias: str = "rt") -> str:
    """Return the SQL OR clause matching every exclusion tag.

    The returned expression is parenthesised, suitable for embedding
    in a ``NOT EXISTS (... AND <clause>)`` shape. The caller is
    responsible for the surrounding ``ref_tags`` ⋈ ``tags`` join and
    the ``namespace = 'OPEN'`` filter (``reftag_alias`` must name the
    ``ref_tags`` side of that join — every current caller uses ``rt``).

    Centralising the clause means the doable view, the dispatch
    candidate query, and any future "skip robot-stay-away leaves"
    surface share the same logic — drift between them is impossible.

    ``claimed-by:`` rides along as a *lease*, not a registry tag: it
    excludes only while its ``expires_at`` is in the future (stamped
    ``now() + CLAIM_TTL_HOURS`` by ``TodoHandler._after_tag_mutation``;
    re-claiming refreshes). A legacy claim row (``expires_at IS NULL``,
    minted before the lease semantics) deliberately does NOT exclude —
    that matches its pre-lease behaviour, so old stale claims can't
    suddenly park live leaves.
    """
    parts: list[str] = []
    for t in _DOABLE_EXCLUSION_TAGS:
        if t.endswith(":"):
            parts.append(f"{tag_alias}.value LIKE '{t}%%'")
        else:
            parts.append(f"{tag_alias}.value = '{t}'")
    parts.append(
        f"({tag_alias}.value LIKE 'claimed-by:%%'"
        f" AND {reftag_alias}.expires_at IS NOT NULL"
        f" AND {reftag_alias}.expires_at > now())"
    )
    return "(" + " OR ".join(parts) + ")"


def todo_root_sql(alias: str) -> str:
    """SQL predicate: the ``alias`` row is a todo-tree *root*.

    A root's parent is not a todo: either ``parent_id IS NULL`` (the
    classic shape) or the parent is a ``kind='folder'`` container —
    placement is *where*, never part of the scheduling tree, so a
    strategic sitting in a folder stays a root for rotation / doable /
    picks / review purposes. One shared fragment so the predicate
    cannot drift across the many root-detection queries.

    ``alias`` is a trusted table alias supplied by the caller — never
    user input.
    """
    return (
        f"({alias}.parent_id IS NULL OR EXISTS ("
        f"SELECT 1 FROM refs _pf WHERE _pf.ref_id = {alias}.parent_id "
        f"AND _pf.kind = 'folder'))"
    )


def direction_todo_sql(alias: str) -> str:
    """SQL predicate: the ``alias`` row is a quest **direction** todo —
    the rotation fence (retire-dossier slice 2).

    A direction is a todo ``part-of`` a quest (either stored orientation
    of the ``part-of``/``contains`` pair) or a ``parent_id`` descendant
    of one — a strand's children carry no edge of their own. Directions
    are the quest's attempt tree, not work: they must never enter
    ``view='doable'``/``'queue'``, the nursery's todo detectors, the
    todo count surfaces, or ``gaps.quest_momentum``'s
    ``open_todo_servers``. The quest's own lifecycle is irrelevant — a
    retired quest's directions still aren't work — so no
    ``retired_at`` filter on the quest side. The recursive term uses
    ``UNION`` (dedup) as a guard against a ``parent_id`` cycle; the
    per-quest read is
    :func:`precis.quest.directions.direction_ref_ids`, kept
    semantically identical. ``alias`` is a trusted table alias —
    never user input.
    """
    return (
        "EXISTS ("
        "WITH RECURSIVE dir(ref_id) AS ("
        "  SELECT l.src_ref_id FROM links l"
        "    JOIN refs s ON s.ref_id = l.src_ref_id"
        "    JOIN refs q ON q.ref_id = l.dst_ref_id"
        "   WHERE l.relation = 'part-of' AND q.kind = 'quest'"
        "     AND s.kind = 'todo' AND s.retired_at IS NULL"
        "  UNION"
        "  SELECT l.dst_ref_id FROM links l"
        "    JOIN refs s ON s.ref_id = l.dst_ref_id"
        "    JOIN refs q ON q.ref_id = l.src_ref_id"
        "   WHERE l.relation = 'contains' AND q.kind = 'quest'"
        "     AND s.kind = 'todo' AND s.retired_at IS NULL"
        "  UNION"
        "  SELECT c.ref_id FROM refs c JOIN dir d ON c.parent_id = d.ref_id"
        "   WHERE c.kind = 'todo' AND c.retired_at IS NULL"
        ")"
        f" SELECT 1 FROM dir WHERE dir.ref_id = {alias}.ref_id)"
    )
