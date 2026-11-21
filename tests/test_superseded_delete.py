"""Deleting a merged handle must never silently retire its survivor."""

from __future__ import annotations

from typing import Any

import pytest
from psycopg import Connection

from precis.runtime import PrecisRuntime
from precis.store import Store
from precis.store.types import ResolvedHandle


def _snapshot(store: Store, ref_ids: list[int]) -> dict[str, Any]:
    with store.pool.connection() as conn:
        links = conn.execute("SELECT * FROM links ORDER BY link_id").fetchall()
    return {
        "refs": store.fetch_refs_by_ids(ref_ids, include_deleted=True),
        "events": {ref_id: store.events_for(ref_id) for ref_id in ref_ids},
        "links": links,
    }


@pytest.mark.parametrize("kind,prefix", [("paper", "pa"), ("memory", "me")])
@pytest.mark.parametrize("address", ["bare", "explicit", "prefixed", "batch"])
def test_superseded_delete_refuses_before_rewriting_source(
    runtime_with_store: PrecisRuntime, kind: str, prefix: str, address: str
) -> None:
    store = runtime_with_store.live_store
    loser = store.insert_ref(
        kind=kind,
        slug="deletemergedloser2026" if kind == "paper" else None,
        title="Merged loser",
        meta={"keep": "loser provenance"},
    ).id
    survivor = store.insert_ref(
        kind=kind,
        slug="deletemergedsurvivor2026" if kind == "paper" else None,
        title="Live survivor",
        meta={"keep": "survivor provenance"},
    ).id
    store.add_link(src_ref_id=survivor, dst_ref_id=loser, relation="supersedes")
    store.stamp_ref_meta(loser, {"superseded_by": survivor, "dedup": "test merge"})
    store.retire_ref(loser)
    for ref_id in (loser, survivor):
        store.append_event(
            ref_id, source="test", event="duplicate_merged", payload={"loser": loser}
        )
    loser_handle = f"{prefix}{loser}"
    survivor_handle = f"{prefix}{survivor}"
    args: dict[str, Any] = {"id": loser_handle}
    if address == "explicit":
        args["kind"] = kind
    elif address == "prefixed":
        args["id"] = f"{kind}:{loser_handle}"
    elif address == "batch":
        # A live first item must not be deleted before a later merged id refuses.
        args["id"] = [survivor_handle, loser_handle]
    before = _snapshot(store, [loser, survivor])

    body, is_error = runtime_with_store.dispatch_with_status("delete", args)

    assert is_error, body
    assert "[error:BadInput]" in body
    assert f"{loser_handle} was merged into {survivor_handle}" in body
    assert f"next: delete(kind='{kind}', id='{survivor_handle}')" in body
    assert _snapshot(store, [loser, survivor]) == before


def test_live_memory_delete_still_retires_requested_ref(
    runtime_with_store: PrecisRuntime,
) -> None:
    store = runtime_with_store.live_store
    ref_id = store.insert_ref(
        kind="memory", slug=None, title="Ordinary delete target"
    ).id

    body, is_error = runtime_with_store.dispatch_with_status(
        "delete", {"id": f"me{ref_id}"}
    )

    assert not is_error, body
    assert store.fetch_refs_by_ids([ref_id], include_deleted=True)[ref_id].retired_at


@pytest.mark.parametrize("batch", [False, True])
def test_delete_refuses_merge_committed_after_preflight(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch, batch: bool
) -> None:
    store = runtime_with_store.live_store
    loser = store.insert_ref(kind="memory", slug=None, title="Concurrent loser").id
    survivor = store.insert_ref(
        kind="memory", slug=None, title="Concurrent survivor"
    ).id
    before = _snapshot(store, [survivor])
    loser_handle = f"me{loser}"
    resolve = store.resolve_handle
    merged = False

    def resolve_then_merge(
        handle: str, *, conn: Connection | None = None
    ) -> ResolvedHandle | None:
        nonlocal merged
        result = resolve(handle, conn=conn)
        if handle == loser_handle and not merged:
            merged = True
            with store.tx() as merge_conn:
                store.stamp_ref_meta(
                    loser, {"superseded_by": survivor}, conn=merge_conn
                )
                store.retire_ref(loser, conn=merge_conn)
        return result

    monkeypatch.setattr(store, "resolve_handle", resolve_then_merge)
    body, is_error = runtime_with_store.dispatch_with_status(
        "delete", {"id": [loser_handle] if batch else loser_handle}
    )

    assert merged
    assert is_error, body
    assert "[error:BadInput]" in body
    assert f"me{loser} was merged into me{survivor}" in body
    assert _snapshot(store, [survivor]) == before


def _memory_node(store: Store, title: str) -> int:
    return store.insert_ref(kind="memory", slug=None, title=title).id


def _edges(store: Store, ref_id: int) -> tuple[set[Any], set[Any]]:
    outbound = {
        (link.relation, link.dst_ref_id)
        for link in store.links_for(ref_id, direction="out")
    }
    inbound = {
        (link.relation, link.src_ref_id)
        for link in store.links_for(ref_id, direction="in")
    }
    return outbound, inbound


def test_superseded_by_delete_keeps_the_successors_one_parent(
    runtime_with_store: PrecisRuntime,
) -> None:
    """gr695729: the retired node's parent must not become a second parent."""
    store = runtime_with_store.live_store
    old_parent = _memory_node(store, "Parent of the retired node")
    own_parent = _memory_node(store, "Parent of the successor")
    old = _memory_node(store, "Retired node")
    successor = _memory_node(store, "Successor")
    child = _memory_node(store, "Child of the retired node")
    citer = _memory_node(store, "Gotcha scoped to the retired node")
    neighbour = _memory_node(store, "Neighbour of the retired node")
    store.add_link(src_ref_id=old, dst_ref_id=old_parent, relation="part-of")
    store.add_link(src_ref_id=successor, dst_ref_id=own_parent, relation="part-of")
    store.add_link(src_ref_id=child, dst_ref_id=old, relation="part-of")
    store.add_link(src_ref_id=citer, dst_ref_id=old, relation="qualifies")
    store.add_link(src_ref_id=old, dst_ref_id=neighbour, relation="related-to")

    body, is_error = runtime_with_store.dispatch_with_status(
        "delete",
        {"kind": "memory", "id": f"me{old}", "superseded_by": f"me{successor}"},
    )

    assert not is_error, body
    outbound, inbound = _edges(store, successor)
    assert {dst for rel, dst in outbound if rel == "part-of"} == {own_parent}
    assert outbound == {("part-of", own_parent), ("related-to", neighbour)}
    assert inbound == {("part-of", child), ("qualifies", citer)}
    assert f"3 link(s) migrated to memory id={successor}" in body
    assert f"1 part-of edge(s) not moved: me{successor} keeps its own parent" in body
    assert _edges(store, old_parent) == (set(), set())


def test_superseded_by_delete_gives_a_parentless_successor_the_parent(
    runtime_with_store: PrecisRuntime,
) -> None:
    """The successor takes the retired node's place when it has no parent."""
    store = runtime_with_store.live_store
    old_parent = _memory_node(store, "Parent of the retired node")
    old = _memory_node(store, "Retired node")
    successor = _memory_node(store, "Successor without a parent")
    child = _memory_node(store, "Child of the retired node")
    store.add_link(src_ref_id=old, dst_ref_id=old_parent, relation="part-of")
    store.add_link(src_ref_id=child, dst_ref_id=old, relation="part-of")

    body, is_error = runtime_with_store.dispatch_with_status(
        "delete",
        {"kind": "memory", "id": f"me{old}", "superseded_by": f"me{successor}"},
    )

    assert not is_error, body
    assert _edges(store, successor) == (
        {("part-of", old_parent)},
        {("part-of", child)},
    )
    assert f"2 link(s) migrated to memory id={successor}" in body
    assert "not moved" not in body


def test_same_row_kind_conversion_is_not_a_superseded_delete(
    runtime_with_store: PrecisRuntime,
) -> None:
    store = runtime_with_store.live_store
    ref_id = store.insert_ref(
        kind="datasheet",
        slug="deleteconverted2026",
        title="Converted paper",
        meta={"converted_from_kind": "paper"},
    ).id
    resolved = store.resolve_handle(f"pa{ref_id}")
    assert resolved is not None and resolved.ref_id == ref_id
    assert resolved.kind == "datasheet"
    before = _snapshot(store, [ref_id])

    body, is_error = runtime_with_store.dispatch_with_status(
        "delete", {"id": f"pa{ref_id}"}
    )

    # Datasheets have no delete verb; the existing capability refusal wins.
    assert is_error, body
    assert "[error:Unsupported]" in body
    assert "datasheet does not support delete" in body
    assert "was merged into" not in body
    assert _snapshot(store, [ref_id]) == before
