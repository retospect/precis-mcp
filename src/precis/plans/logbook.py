"""Plan events → the quest logbook (gr548800).

The events that matter on a plan — the root opened or paused, a node
parked at its retry cap, a slice landed, an ask raised — append one-line
``quest_log`` entries on the quest above the plan root, through the same
store path ``put(kind='quest', id=N, text=…)`` uses
(:func:`precis.quest.logbook.append_entry`, ``by='system'``). "Above" is
resolved the way the asks view resolves it: the parent chain up from the
plan root, first ``serves`` link to a quest
(:func:`precis.todo.asks_view.quest_of`).

A plan with no quest above its root writes nothing (the logbook is a
quest feature). An append failure must never break the write path it
annotates — callers get ``None`` and a warning, not an exception.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from precis.quest.logbook import MEASURED_BY, append_entry

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)


def plan_root_of(store: Store, todo_id: int) -> int | None:
    """The plan root ``todo_id`` sits inside: the nearest ancestor (or the
    todo itself) carrying an open ``plan:<id>`` tag — the tag value is the
    root. ``None`` for a todo outside every plan."""
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            WITH RECURSIVE chain AS (
                SELECT ref_id, parent_id, 0 AS depth FROM refs
                 WHERE ref_id = %s AND retired_at IS NULL
                UNION ALL
                SELECT r.ref_id, r.parent_id, c.depth + 1 FROM refs r
                  JOIN chain c ON r.ref_id = c.parent_id
                 WHERE r.retired_at IS NULL AND c.depth < %s
            )
            SELECT t.value FROM chain c
              JOIN ref_tags rt ON rt.ref_id = c.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE t.namespace = 'OPEN' AND t.value LIKE 'plan:%%'
             ORDER BY c.depth
             LIMIT 1
            """,
            (todo_id, 20),
        ).fetchone()
    if row is None:
        return None
    tail = str(row[0])[5:]
    return int(tail) if tail.isdigit() else None


def log_plan_event(
    store: Store,
    node_id: int,
    event: str,
    *,
    entry_type: str = "note",
    root: int | None = None,
) -> int | None:
    """Append ``plan td<root>: <event>`` (one line) on the quest above the
    plan root ``node_id`` sits under (``root`` skips the lookup when the
    caller already knows it). Returns the 1-based entry number, or
    ``None`` when there is no plan or no quest above it, or the append
    failed."""
    try:
        if root is None:
            root = plan_root_of(store, node_id)
        if root is None:
            return None
        from precis.todo.asks_view import quest_of

        quest_id = quest_of(store, root)
        if quest_id is None:
            return None
        return append_entry(
            store,
            quest_id,
            text=f"plan td{root}: {event}",
            entry_type=entry_type,
            by=MEASURED_BY,
        )
    except Exception:  # an annotation never breaks the write it records
        log.warning("plans.logbook: entry for plan td%s failed", root, exc_info=True)
        return None


def first_line(text: Any, cap: int = 80) -> str:
    """The one-line display stub of a todo title for an entry text."""
    line = str(text or "").split("\n", 1)[0].strip()
    return line if len(line) <= cap else line[: cap - 1].rstrip() + "…"


__all__ = ["first_line", "log_plan_event", "plan_root_of"]
