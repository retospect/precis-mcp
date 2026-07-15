"""Quest reweighting — priority as a field flowing down the `serves` DAG.

Slice 2 of the quest layer (docs/proposals/quest-layer.md). A quest's **striving
weight** flows *down* the `serves` edges into the three places work + knowledge
are actually chosen — rotation, reading, acquisition. Aggregation on overlap is
**max** (a node serving two quests inherits the stronger pull), with light decay
per hop up the quest→quest ladder. Only **active** quests exert pull — a
dormant / abandoned striving stops steering.

The whole thing is a **no-op until quests + `serves` edges exist**: with no
active quests, :func:`active_quest_weights` returns ``{}`` and every reweighted
ordering collapses to its original form. So the callers can wire this in on the
hot path safely — it changes nothing until a project/paper/concept is actually
linked to an active quest. That is the "reweight, don't mint" contract.

Priority is read from the canonical ``refs.prio`` column (1..10, **lower =
hotter**, the same scale the todo tree rotates on), inverted + normalised into a
weight in (0, 1] by :func:`base_weight`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from precis.store import Store

#: Per-hop attenuation as priority flows *up* the quest→quest ladder: a grand
#: quest's pull reaches a sub-quest at ``DECAY`` of its own weight, and the
#: sub-quest's servers inherit that. The work→quest edge itself is *not*
#: decayed — serving a quest directly grants the pull present at that quest.
STRIVING_DECAY = 0.5

#: Matches the todo tree's ``COALESCE(prio, 5)`` — an unset priority is neutral.
_DEFAULT_PRIO = 5


def base_weight(prio: int | None) -> float:
    """Striving weight in (0, 1] from a quest's ``prio`` column.

    ``refs.prio`` is inverted (1 = most urgent … 10 = least), so we flip +
    normalise: prio 1 → 1.0, prio 5 (default) → 0.6, prio 10 → 0.1. An unset
    prio is treated as the neutral default 5.
    """
    p = _DEFAULT_PRIO if prio is None else max(1, min(10, int(prio)))
    return (11 - p) / 10.0


def active_quest_weights(store: Store) -> dict[int, float]:
    """Effective striving weight per **active** quest, folding the ladder.

    Base weight comes from each active quest's ``prio``. A quest that ``serves``
    a grander active quest inherits ``max(own, grand × DECAY)`` — priority flows
    down the ladder — via a bounded max-relaxation over the quest→quest `serves`
    edges (``DECAY < 1`` guarantees convergence even if the strivings form a
    cycle). Returns ``{}`` when nothing is active (the no-op case).
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT r.ref_id, r.prio FROM refs r "
            "JOIN ref_tags rt ON rt.ref_id = r.ref_id "
            "JOIN tags t ON t.tag_id = rt.tag_id "
            "WHERE r.kind = 'quest' AND r.deleted_at IS NULL "
            "AND t.namespace = 'STATUS' AND t.value = 'active'"
        ).fetchall()
    eff: dict[int, float] = {int(q): base_weight(p) for q, p in rows}
    if not eff:
        return {}
    active_ids = list(eff)
    # quest→quest serves edges among the active set: `sub serves grand`.
    with store.pool.connection() as conn:
        edges = conn.execute(
            "SELECT src_ref_id, dst_ref_id FROM links "
            "WHERE relation = 'serves' "
            "AND src_ref_id = ANY(%s) AND dst_ref_id = ANY(%s)",
            (active_ids, active_ids),
        ).fetchall()
    ladder: list[tuple[int, int]] = [(int(s), int(d)) for s, d in edges if s != d]
    # Bellman-Ford-style max relaxation: sub inherits grand's weight × DECAY.
    # At most len(active) passes for the longest simple path; DECAY shrinks any
    # cycle so it settles well within that bound.
    for _ in range(len(active_ids)):
        changed = False
        for sub, grand in ladder:
            cand = eff[grand] * STRIVING_DECAY
            if cand > eff[sub]:
                eff[sub] = cand
                changed = True
        if not changed:
            break
    return eff


def served_striving_weight(
    store: Store, ref_ids: list[int] | set[int]
) -> dict[int, float]:
    """For each ref, the max active-quest striving weight it directly serves.

    One ``serves`` hop from the work/knowledge node to a quest, taking that
    quest's effective (ladder-folded) weight; overlap aggregates by **max**.
    Refs serving no active quest map to ``0.0``. The inverse of
    :func:`server_weights_for_active_quests` — this is the "given these refs"
    direction (for the quest tree / ad-hoc consumers)."""
    ids = [int(r) for r in ref_ids]
    if not ids:
        return {}
    qw = active_quest_weights(store)
    out: dict[int, float] = {r: 0.0 for r in ids}
    if not qw:
        return out
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT src_ref_id, dst_ref_id FROM links "
            "WHERE relation = 'serves' "
            "AND src_ref_id = ANY(%s) AND dst_ref_id = ANY(%s)",
            (ids, list(qw)),
        ).fetchall()
    for s, d in rows:
        w = qw[int(d)]
        if w > out[int(s)]:
            out[int(s)] = w
    return out


def server_weights_for_active_quests(
    store: Store, *, server_kind: str | None = None
) -> dict[int, float]:
    """Every ref that ``serves`` an active quest → its inherited striving weight.

    The "from the quest side" direction: walk the inbound `serves` edges of the
    active quests and, for each server ref, keep the **max** effective weight of
    the quests it serves. ``server_kind`` filters the servers (e.g. ``'paper'``
    for the acquisition backlog, or leave ``None`` and let the caller join on
    the ids that matter — the rotation joins the map on strategic-root ids).
    Returns ``{}`` when nothing is active (the no-op case)."""
    qw = active_quest_weights(store)
    if not qw:
        return {}
    sql = (
        "SELECT l.src_ref_id, l.dst_ref_id FROM links l "
        "JOIN refs s ON s.ref_id = l.src_ref_id "
        "WHERE l.relation = 'serves' AND l.dst_ref_id = ANY(%s) "
        "AND s.deleted_at IS NULL"
    )
    params: list[object] = [list(qw)]
    if server_kind is not None:
        sql += " AND s.kind = %s"
        params.append(server_kind)
    with store.pool.connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    out: dict[int, float] = {}
    for s, d in rows:
        w = qw[int(d)]
        if w > out.get(int(s), 0.0):
            out[int(s)] = w
    return out


__all__ = [
    "STRIVING_DECAY",
    "active_quest_weights",
    "base_weight",
    "served_striving_weight",
    "server_weights_for_active_quests",
]
