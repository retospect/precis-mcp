"""Open ``blocked-by`` targets, shared by the todo and gripe board rows.

gr656970: a ``blocked-by`` edge sequences work across kinds — a todo or
gripe carrying a target whose STATUS is outside
:data:`BLOCKER_TERMINAL_STATUSES` (the union across the todo / gripe /
quest vocabularies, gr520237) is held back from dispatch and named as
blocked on every surface. One query serves both row builders so the
work board and the blocked view cannot drift on the terminal policy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from precis.todo import blocker_terminal_sql

if TYPE_CHECKING:
    from precis.store import Store


def open_blocked_by(
    store: Store, ref_ids: list[int]
) -> dict[int, tuple[tuple[str, int], ...]]:
    """``{src_id: ((target_kind, target_id), …)}`` of live ``blocked-by``
    targets per source ref, target id order; an empty map for no ids."""
    if not ref_ids:
        return {}
    with store.pool.connection() as conn:
        rows = conn.execute(
            f"""SELECT l.src_ref_id, b.kind, b.ref_id FROM links l
                JOIN refs b ON b.ref_id = l.dst_ref_id
                WHERE l.src_ref_id = ANY(%s) AND l.relation = 'blocked-by'
                  AND b.retired_at IS NULL
                  AND NOT EXISTS (
                    SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                    WHERE rt.ref_id = b.ref_id AND t.namespace = 'STATUS'
                      AND t.value IN {blocker_terminal_sql()})
                ORDER BY l.src_ref_id, b.ref_id""",
            (list(ref_ids),),
        ).fetchall()
    grouped: dict[int, list[tuple[str, int]]] = {}
    for src, kind, dst in rows:
        grouped.setdefault(int(src), []).append((str(kind), int(dst)))
    return {src: tuple(targets) for src, targets in grouped.items()}


__all__ = ["open_blocked_by"]
