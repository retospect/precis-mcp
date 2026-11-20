"""Drive ``layout=tree&rel=<rel>`` — group the filtered rows into a tree.

The flat list renders rows in the listing's own order; ``layout=tree``
instead nests each row under every parent a *named edge source*
(:data:`_TREE_EDGES`) still on the page gives it, so one mode covers the
quest ``serves`` DAG, the todo ``parent_id`` hierarchy, and any future
relation — the same data ``/design`` shows as a bespoke read-only tree
(gr664408; the ``/design`` redirect itself is superseded — Reto
2026-11-14: the quest rollup view owns that tree role and ``/design``
keeps its own).

:func:`build_tree` is the pure core: the page's row dicts plus a
``{child: [parents]}`` edge map in, nested nodes out — roots first
(rows with no in-set parent; a parent the filter cut is simply absent),
then each child under every parent it kept (a DAG child sits under two
parents twice). A per-path visited set breaks cycles so a bad edge can't
recurse forever, and a cycle with no root still renders via orphan
promotion — the same posture ``routes/refs.py::_quest_index`` takes for
the ``serves`` DAG.

Pagination is by roots, owned by the caller
(``routes/drive.py::index``): the route fetches the whole filtered set
(the browse path exactly, via its count query; the search path bounded
by all live refs of the selected kinds), builds one forest, and pages its
top level — a page is ``_PAGE_SIZE`` top-level nodes with their
filtered descendants, so a branch never splits across pages and a
child never promotes to root because its parent fell on another page.
Totals count top-level nodes, not flat rows. The doable queue already
pages its leaves, so its forest renders without a second slice and retains
the queue estimate and next-page probe.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from precis.store.store import Store


def _row_id(row: dict[str, Any]) -> int | None:
    """The ref id a row dict carries — ``item_row``'s ``id`` (``None`` on
    synthetic rows like ``k=tag``) or ``_row``'s ``ref_id``."""
    rid = row.get("id")
    if rid is None:
        rid = row.get("ref_id")
    return int(rid) if isinstance(rid, int) else None


def build_tree(
    rows: list[dict[str, Any]], edges: dict[int, list[int]]
) -> list[dict[str, Any]]:
    """Flat rows + ``{child: [parents]}`` → ``[{"row": …, "children": […]}]``.

    Roots are the rows with no parent *in the row set* — a parent the
    kind/tag/folder filters dropped doesn't count. A child renders under
    every in-set parent (the ``serves`` DAG's two-parent case), and an
    id-less row (a ``k=tag`` row) always reads as a root. The per-path
    visited set drops a child already on the current root→node path, so
    a cycle can't recurse forever; a cycle with no root at all promotes
    its first unrendered member in row order, so bad data stays visible
    instead of vanishing.
    """
    keyed = [(rid, r) for r in rows if (rid := _row_id(r)) is not None]
    ids = {rid for rid, _ in keyed}
    by_id = dict(keyed)
    pos = {rid: i for i, (rid, _r) in enumerate(keyed)}
    children_of: dict[int, list[int]] = {}
    has_parent: set[int] = set()
    for child, parents in edges.items():
        if child not in ids:
            continue
        for pid in parents:
            if pid in ids and pid != child:
                children_of.setdefault(pid, []).append(child)
                has_parent.add(child)
    for kids in children_of.values():
        kids.sort(key=pos.__getitem__)

    rendered: set[int] = set()

    def node(rid: int, path: frozenset[int]) -> dict[str, Any]:
        rendered.add(rid)
        here = path | {rid}
        return {
            "row": by_id[rid],
            "children": [
                node(k, here) for k in children_of.get(rid, []) if k not in here
            ],
        }

    out: list[dict[str, Any]] = []
    for r in rows:
        rid = _row_id(r)
        if rid is None:
            out.append({"row": r, "children": []})
        elif rid not in has_parent:
            out.append(node(rid, frozenset()))
    # A cycle whose members all carry an in-set parent yields no root —
    # promote its first unrendered member in row order rather than drop
    # the whole ring (the ``_quest_index`` fallback).
    for rid, _r in keyed:
        if rid not in rendered:
            out.append(node(rid, frozenset()))
    return out


def _serves_edges(store: Store, ref_ids: list[int]) -> dict[int, list[int]]:
    """``{child: [parents]}`` off the ``serves`` links relation — the
    serving ref (src) hangs under the ref it serves (dst), the same
    direction ``routes/refs.py::_quest_index`` renders. One
    ``links_for`` per row so the ``served-by`` inverse rewrite stays
    honoured; the tree pages this serves (quests, todo subtrees) are
    small."""
    edges: dict[int, list[int]] = {}
    for rid in ref_ids:
        for ln in store.links_for(rid, direction="in", relation="serves"):
            edges.setdefault(int(ln.src_ref_id), []).append(rid)
    return edges


def _parent_edges(store: Store, ref_ids: list[int]) -> dict[int, list[int]]:
    """``{child: [parent]}`` off ``refs.parent_id`` — the todo/job
    subtree relation. One batched read. A folder parent lands here too
    but can never join the row set (a folder never lists beside its
    contents), so a filed row simply reads as a root like any
    filtered-out parent."""
    if not ref_ids:
        return {}
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, parent_id FROM refs "
            "WHERE ref_id = ANY(%s) AND parent_id IS NOT NULL",
            (list(ref_ids),),
        ).fetchall()
    return {int(r[0]): [int(r[1])] for r in rows}


#: The named edge sources ``rel=<name>`` accepts — ``routes/drive.py``
#: 400s an unknown name and names these keys. ``serves`` covers the
#: quest ``serves`` DAG (the ``/refs/quest`` tree's data); ``parent``
#: covers the todo/jobs ``refs.parent_id`` hierarchy.
_TREE_EDGES: dict[str, Callable[[Store, list[int]], dict[int, list[int]]]] = {
    "serves": _serves_edges,
    "parent": _parent_edges,
}
