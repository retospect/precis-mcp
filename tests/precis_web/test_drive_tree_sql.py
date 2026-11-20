"""Exercise the Drive parent edge query against real Postgres."""

from __future__ import annotations

from typing import TYPE_CHECKING

from precis_web.drive_tree import _parent_edges

if TYPE_CHECKING:
    from precis.store.store import Store


def test_parent_edges_select_only_requested_children(store: Store) -> None:
    root = store.insert_ref(kind="todo", slug=None, title="Root")
    child = store.insert_ref(kind="todo", slug=None, title="Child", parent_id=root.id)
    grandchild = store.insert_ref(
        kind="todo", slug=None, title="Grandchild", parent_id=child.id
    )
    store.insert_ref(kind="todo", slug=None, title="Sibling", parent_id=root.id)

    assert _parent_edges(store, [root.id, child.id, grandchild.id]) == {
        child.id: [root.id],
        grandchild.id: [child.id],
    }
