"""Element→chunk bindings for diagrams (ADR 0057, slice 1).

Kind-agnostic chunk-level ``depicts`` links: one link row per (source,
target, relation) edge, with the depicting element id(s) accumulated in
``links.meta.elements`` (the links UNIQUE key excludes meta, so two elements
that depict the same target share one row)."""

from __future__ import annotations

import pytest

from precis.errors import BadInput
from precis.store.store import Store


def _project(store: Store) -> int:
    return store.insert_ref(kind="todo", slug=None, title="proj").id


def _diagram_and_targets(store: Store) -> tuple[int, str, str]:
    """A source 'diagram' chunk + two target draft chunks (dc handles)."""
    dia, dtitle = store.create_draft(
        name="dia", title="Diagram", project_ref_id=_project(store)
    )
    node = dtitle.chunk_id
    src, _t = store.create_draft(
        name="src", title="Src", project_ref_id=_project(store)
    )
    store.add_chunks(
        ref_id=src.id, chunk_kind="paragraph", text="deck hook part\n\nlatch pawl"
    )
    order = store.reading_order(src.id)
    return node, order[1].dc, order[2].dc  # two paragraph handles


def _bindings(store: Store, node: int) -> set[tuple[str, str]]:
    return {(b["element"], b["handle"]) for b in store.element_bindings(node)}


def _depicts_rows(store: Store, node: int) -> int:
    with store.pool.connection() as c:
        return int(
            c.execute(
                "SELECT count(*) FROM links "
                "WHERE src_chunk_id=%s AND relation='depicts'",
                (node,),
            ).fetchone()[0]
        )


def test_bind_two_elements_same_target_share_one_row(store: Store) -> None:
    node, h1, h2 = _diagram_and_targets(store)
    store.bind_element(node_chunk_id=node, element="deckhook", target=h1)
    store.bind_element(node_chunk_id=node, element="load-arrow", target=h1)
    store.bind_element(node_chunk_id=node, element="latch", target=h2)

    assert _bindings(store, node) == {
        ("deckhook", h1),
        ("load-arrow", h1),
        ("latch", h2),
    }
    # h1 carries TWO elements on ONE row; h2 is a second row.
    assert _depicts_rows(store, node) == 2


def test_bind_is_idempotent(store: Store) -> None:
    node, h1, _h2 = _diagram_and_targets(store)
    store.bind_element(node_chunk_id=node, element="deckhook", target=h1)
    store.bind_element(node_chunk_id=node, element="deckhook", target=h1)
    assert _bindings(store, node) == {("deckhook", h1)}
    assert _depicts_rows(store, node) == 1


def test_unbind_keeps_row_until_last_element(store: Store) -> None:
    node, h1, h2 = _diagram_and_targets(store)
    store.bind_element(node_chunk_id=node, element="deckhook", target=h1)
    store.bind_element(node_chunk_id=node, element="load-arrow", target=h1)
    store.bind_element(node_chunk_id=node, element="latch", target=h2)

    # drop one of two elements on the shared-target row → row survives
    assert store.unbind_element(node_chunk_id=node, element="deckhook", target=h1) == 1
    assert _bindings(store, node) == {("load-arrow", h1), ("latch", h2)}
    assert _depicts_rows(store, node) == 2

    # drop the sole element on h2 → its row is deleted
    store.unbind_element(node_chunk_id=node, element="latch")
    assert _depicts_rows(store, node) == 1


def test_bind_ref_level_target_memory(store: Store) -> None:
    from precis.utils import handle_registry

    node, _h1, _h2 = _diagram_and_targets(store)
    mem = store.insert_ref(kind="memory", slug=None, title="load path note")
    mh = handle_registry.format_handle("memory", mem.id)  # me<id>, record-level
    store.bind_element(node_chunk_id=node, element="load-arrow", target=mh)

    got = store.element_bindings(node)
    assert got == [
        {
            "element": "load-arrow",
            "relation": "depicts",
            "kind": "memory",
            "ident": str(mem.id),
            "handle": mh,
            "chunk_id": None,  # ref-level target: dst_chunk_id NULL
            "title": "load path note",
        }
    ]


def test_set_element_bindings_reconciles(store: Store) -> None:
    node, h1, h2 = _diagram_and_targets(store)
    store.bind_element(node_chunk_id=node, element="a", target=h1)

    res = store.set_element_bindings(
        node_chunk_id=node,
        desired=[{"element": "a", "target": h1}, {"element": "b", "target": h2}],
    )
    assert res == {"added": 1, "removed": 0}
    assert _bindings(store, node) == {("a", h1), ("b", h2)}

    # empty desired clears everything
    res2 = store.set_element_bindings(node_chunk_id=node, desired=[])
    assert res2["removed"] == 2
    assert store.element_bindings(node) == []


def test_set_element_bindings_skips_unresolvable(store: Store) -> None:
    node, h1, _h2 = _diagram_and_targets(store)
    res = store.set_element_bindings(
        node_chunk_id=node,
        desired=[
            {"element": "ok", "target": h1},
            {"element": "bad", "target": "dc999999999"},  # dangling → skipped
            {"element": "", "target": h1},  # empty element → skipped
        ],
    )
    assert res == {"added": 1, "removed": 0}
    assert _bindings(store, node) == {("ok", h1)}


def test_bind_bad_input_raises(store: Store) -> None:
    node, h1, _h2 = _diagram_and_targets(store)
    with pytest.raises(BadInput):
        store.bind_element(node_chunk_id=node, element="e", target="dc999999999")
    with pytest.raises(BadInput):
        store.bind_element(node_chunk_id=node, element="  ", target=h1)
