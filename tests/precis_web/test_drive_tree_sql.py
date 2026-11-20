"""Real-PG regression tests for ``drive_tree``'s raw SQL.

``_parent_edges`` reads ``refs.parent_id`` with an ``ANY(%s)`` array bind
and a ``parent_id IS NOT NULL`` filter; the web ``FakeStore`` does not
parse SQL, so neither is exercised by the route tests. This file uses
the live ``store`` fixture (see ``test_todo_sql.py``) to run the query
and feed its result through :func:`build_tree`.
"""

from __future__ import annotations

from typing import Any

from precis_web.drive_tree import _TREE_EDGES, _parent_edges, build_tree


def _todo(store: Any, title: str, parent_id: int | None = None) -> Any:
    return store.insert_ref(
        kind="todo", slug=None, title=title, parent_id=parent_id, meta={}
    )


def test_parent_edges_maps_each_child_to_its_parent(store: Any) -> None:
    """Children come back as ``{child: [parent]}``; a root (``parent_id``
    NULL) is filtered out by the SQL, not left as an empty entry."""
    root = _todo(store, "root")
    mid = _todo(store, "mid", root.id)
    leaf = _todo(store, "leaf", mid.id)
    sibling = _todo(store, "sibling", root.id)

    edges = _parent_edges(store, [root.id, mid.id, leaf.id, sibling.id])

    assert edges == {mid.id: [root.id], leaf.id: [mid.id], sibling.id: [root.id]}


def test_parent_edges_only_reads_the_requested_ids(store: Any) -> None:
    """``ref_id = ANY(%s)`` scopes the read: a child outside the asked set
    contributes no edge, and an empty set short-circuits to ``{}``."""
    root = _todo(store, "root")
    asked = _todo(store, "asked", root.id)
    _todo(store, "other child", root.id)

    assert _parent_edges(store, [asked.id]) == {asked.id: [root.id]}
    assert _parent_edges(store, []) == {}


def test_parent_edge_source_builds_the_nested_tree(store: Any) -> None:
    """End to end through the named ``rel=parent`` source: the real edges
    nest rows under their parent, and a parent the filter cut leaves its
    child reading as a root."""
    root = _todo(store, "root")
    kept = _todo(store, "kept", root.id)
    leaf = _todo(store, "leaf", kept.id)
    cut = _todo(store, "cut parent", root.id)
    orphan = _todo(store, "orphan", cut.id)

    ids = [root.id, kept.id, leaf.id, orphan.id]  # ``cut`` filtered out
    rows = [{"id": i} for i in ids]
    forest = build_tree(rows, _TREE_EDGES["parent"](store, ids))

    assert [n["row"]["id"] for n in forest] == [root.id, orphan.id]
    (kept_node,) = forest[0]["children"]
    assert kept_node["row"]["id"] == kept.id
    assert [c["row"]["id"] for c in kept_node["children"]] == [leaf.id]
    assert forest[1]["children"] == []
