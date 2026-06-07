"""MemoryHandler — through both direct invocation and full dispatch."""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, Gone, NotFound
from precis.handlers.memory import MemoryHandler
from precis.response import Response
from precis.runtime import PrecisRuntime
from precis.store import Store, as_dream_actor

# ---------------------------------------------------------------------------
# Handler-level tests (skip MCP, skip dispatch)
# ---------------------------------------------------------------------------


@pytest.fixture
def handler(hub: Hub) -> MemoryHandler:
    return MemoryHandler(hub=hub)


def test_create_returns_id(handler: MemoryHandler) -> None:
    r = handler.put(text="my first memory")
    assert "created memory id=" in r.body


def test_create_requires_text(handler: MemoryHandler) -> None:
    with pytest.raises(BadInput):
        handler.put()


def test_create_then_get(handler: MemoryHandler) -> None:
    r = handler.put(text="findable memory body")
    # parse out the id
    new_id = int(r.body.rsplit("=", 1)[1])

    got = handler.get(id=new_id)
    assert "findable memory body" in got.body
    assert f"memory {new_id}" in got.body


def test_get_unknown_raises(handler: MemoryHandler) -> None:
    with pytest.raises(NotFound):
        handler.get(id=99999)


def test_get_string_id_coerced(handler: MemoryHandler) -> None:
    r = handler.put(text="numeric coercion")
    new_id = int(r.body.rsplit("=", 1)[1])
    got = handler.get(id=str(new_id))
    assert "numeric coercion" in got.body


def test_get_bad_id(handler: MemoryHandler) -> None:
    with pytest.raises(BadInput, match="integer"):
        handler.get(id="not-a-number")


def test_put_on_existing_id_rejected(handler: MemoryHandler) -> None:
    """Seven-verb cutover: put is creation-only on numeric kinds.

    Mutating an existing memory's text body is no longer exposed —
    capture the new wording as a fresh memory or use
    ``delete + put`` to replace. Tag/link mutation moves to the
    dedicated verbs.
    """
    r = handler.put(text="original")
    new_id = int(r.body.rsplit("=", 1)[1])

    with pytest.raises(BadInput, match="put on existing memory"):
        handler.put(id=new_id, text="updated")

    # The original text is untouched.
    got = handler.get(id=new_id)
    assert "original" in got.body


def test_update_tags_only(handler: MemoryHandler) -> None:
    # The closed-axis allow-list for memory is empty (per-kind axis
    # enforcement) — memory uses only open tags. Demonstrate updating
    # a memory with two open tags from the documented vocabulary.
    r = handler.put(text="memory with tags")
    new_id = int(r.body.rsplit("=", 1)[1])

    handler.tag(id=new_id, add=["kind:decision", "confidence-strong"])

    got = handler.get(id=new_id)
    assert "kind:decision" in got.body
    assert "confidence-strong" in got.body


def test_update_open_tags_accumulate(handler: MemoryHandler) -> None:
    """Open tags accumulate (no axis-replacement contract). Two
    confidence-* tags can coexist — the agent is responsible for
    untagging the old value, exactly the pattern documented in
    ``precis-memory-help`` for the open-tag confidence axis."""
    r = handler.put(text="x", tags=["confidence-tentative"])
    new_id = int(r.body.rsplit("=", 1)[1])

    handler.tag(id=new_id, add=["confidence-strong"])

    got = handler.get(id=new_id)
    # Both stick around — that's the open-tag contract. The
    # remove-on-update workflow uses ``untags=`` (see
    # test_untags_on_put.py).
    assert "confidence-tentative" in got.body
    assert "confidence-strong" in got.body


def test_status_axis_rejected_on_memory(handler: MemoryHandler) -> None:
    """Per-kind axis enforcement: STATUS: belongs on todo/gripe,
    not on memory. The MCP critic flagged ``STATUS:open`` on a memory
    as a smell — the tag is decorative because no STATUS-filtered
    query against ``kind='memory'`` can find it. Reject at the write
    boundary instead."""
    r = handler.put(text="m")
    new_id = int(r.body.rsplit("=", 1)[1])
    with pytest.raises(BadInput, match="axis not allowed on kind 'memory'"):
        handler.tag(id=new_id, add=["STATUS:open"])


def test_tag_no_op_rejected(handler: MemoryHandler) -> None:
    """``tag()`` with no add= and no remove= is a misuse — reject
    rather than silently no-op so a typo doesn't vanish."""
    r = handler.put(text="x")
    new_id = int(r.body.rsplit("=", 1)[1])
    with pytest.raises(BadInput, match="requires add= or remove="):
        handler.tag(id=new_id)


def test_delete(handler: MemoryHandler) -> None:
    r = handler.put(text="goodbye")
    new_id = int(r.body.rsplit("=", 1)[1])

    deleted = handler.delete(id=new_id)
    assert "deleted" in deleted.body

    # MCP critic MINOR-C (round 1): soft-deleted refs raise ``Gone``
    # (distinct from ``NotFound`` for never-existed ids). The row is
    # still addressable at the SQL layer by clearing ``deleted_at``.
    with pytest.raises(Gone, match="soft-deleted"):
        handler.get(id=new_id)


def test_delete_missing_raises(handler: MemoryHandler) -> None:
    with pytest.raises(NotFound):
        handler.delete(id=99999)


def test_search_finds_match(handler: MemoryHandler) -> None:
    handler.put(text="nitrate reduction on copper")
    handler.put(text="something completely different")

    r = handler.search(q="nitrate copper")
    assert "nitrate" in r.body
    assert "1 memory" in r.body


def test_search_no_match(handler: MemoryHandler) -> None:
    handler.put(text="hello world")
    r = handler.search(q="frobnicate")
    assert "no memory entries match" in r.body


def test_search_requires_q(handler: MemoryHandler) -> None:
    with pytest.raises(BadInput):
        handler.search()
    with pytest.raises(BadInput):
        handler.search(q="   ")


# ---------------------------------------------------------------------------
# Card emission (dreaming foundation): memories become embeddable
# ---------------------------------------------------------------------------


def test_create_emits_card_combined(handler: MemoryHandler) -> None:
    """A new memory emits a synthetic ``card_combined`` chunk (``ord=-1``)
    holding its text, so the embed worker can vectorize it and semantic
    search finds true neighbours (docs/design/dreaming.md)."""
    r = handler.put(text="electrochemical CO2 reduction on copper")
    new_id = int(r.body.rsplit("=", 1)[1])
    with handler.store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ord, chunk_kind, text FROM chunks WHERE ref_id = %s",
            (new_id,),
        ).fetchall()
    assert rows == [(-1, "card_combined", "electrochemical CO2 reduction on copper")]


def test_upsert_card_combined_is_idempotent(store: Store) -> None:
    """Re-emitting replaces (DELETE+INSERT), never duplicates: a memory
    keeps exactly one ``ord=-1`` card, with the latest text."""
    ref = store.insert_ref(kind="memory", slug=None, title="first", meta={})
    store.upsert_card_combined(ref.id, "first")
    store.upsert_card_combined(ref.id, "second")
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ord, text FROM chunks WHERE ref_id = %s ORDER BY ord",
            (ref.id,),
        ).fetchall()
    assert rows == [(-1, "second")]


# ---------------------------------------------------------------------------
# supersede: the one guarded destructive merge (dreaming)
# ---------------------------------------------------------------------------


def _id_of(resp: Response) -> int:
    """Parse the id off a ``put`` create-ack (``...id=N``)."""
    return int(resp.body.rsplit("=", 1)[1])


def _new_id(resp: Response) -> int:
    """Parse the survivor id off a ``supersede`` ack (``...id=N (...)``)."""
    return int(resp.body.split("id=", 1)[1].split()[0])


def test_supersede_merges_and_soft_deletes(
    handler: MemoryHandler, store: Store
) -> None:
    a = _id_of(handler.put(text="nitrate reduces on copper"))
    b = _id_of(handler.put(text="copper reduces nitrate"))
    r = handler.supersede(merge_ids=[a, b], new_text="Cu reduces nitrate")
    new_id = _new_id(r)
    # survivor is a live memory carrying its text + DREAM:consolidated
    got = handler.get(id=new_id)
    assert "Cu reduces nitrate" in got.body
    assert any(str(t) == "DREAM:consolidated" for t in store.tags_for(new_id))
    # originals are soft-deleted and point back via supersedes edges
    assert store.get_ref(kind="memory", id=a) is None
    assert store.get_ref(kind="memory", id=b) is None
    sup = store.links_for(new_id, direction="out", relation="supersedes")
    assert {link.dst_ref_id for link in sup} == {a, b}
    assert (
        store.get_ref(kind="memory", id=a, include_deleted=True).meta["superseded_by"]
        == new_id
    )


def test_supersede_migrates_links(handler: MemoryHandler, store: Store) -> None:
    keep = _id_of(handler.put(text="keep this neighbour"))
    a = _id_of(handler.put(text="dup one"))
    b = _id_of(handler.put(text="dup two"))
    store.add_link(src_ref_id=a, dst_ref_id=keep, relation="related-to")
    new_id = _new_id(handler.supersede(merge_ids=[a, b], new_text="dup"))
    # a -> keep migrated onto survivor -> keep
    out = store.links_for(new_id, direction="out", relation="related-to")
    assert any(link.dst_ref_id == keep for link in out)
    # the original edge off `a` is gone (only the supersedes edge remains)
    assert not [
        link
        for link in store.links_for(a, relation="related-to")
        if link.relation == "related-to"
    ]


def test_supersede_default_tags_union(handler: MemoryHandler, store: Store) -> None:
    a = _id_of(handler.put(text="alpha", tags=["topic:co2"]))
    b = _id_of(handler.put(text="beta", tags=["confidence-strong"]))
    new_id = _new_id(handler.supersede(merge_ids=[a, b], new_text="ab"))
    tags = {str(t) for t in store.tags_for(new_id)}
    assert {"topic:co2", "confidence-strong", "DREAM:consolidated"} <= tags


def test_supersede_requires_two_ids(handler: MemoryHandler) -> None:
    a = _id_of(handler.put(text="solo"))
    with pytest.raises(BadInput, match="2"):
        handler.supersede(merge_ids=[a], new_text="x")
    with pytest.raises(BadInput, match="2 distinct"):
        handler.supersede(merge_ids=[a, a], new_text="x")


def test_supersede_rejects_non_memory(handler: MemoryHandler, store: Store) -> None:
    a = _id_of(handler.put(text="a real memory"))
    todo = store.insert_ref(kind="todo", slug=None, title="not a memory", meta={})
    with pytest.raises(BadInput, match="not a live memory"):
        handler.supersede(merge_ids=[a, todo.id], new_text="x")


def test_supersede_is_compress_only(handler: MemoryHandler) -> None:
    a = _id_of(handler.put(text="short"))
    b = _id_of(handler.put(text="tiny"))
    with pytest.raises(BadInput, match="compress-only"):
        handler.supersede(merge_ids=[a, b], new_text="x" * 200)


def test_supersede_caps_merge_count(handler: MemoryHandler) -> None:
    ids = [_id_of(handler.put(text=f"dup {i}")) for i in range(11)]
    with pytest.raises(BadInput, match="caps at 10"):
        handler.supersede(merge_ids=ids, new_text="merged")


# ---------------------------------------------------------------------------
# Salience: memory search heats its card chunk (dreaming target signal)
# ---------------------------------------------------------------------------


def _card_last_seen(store: Store, ref_id: int) -> object:
    cids = store.card_chunk_ids([ref_id])
    assert len(cids) == 1
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT last_seen FROM chunks WHERE chunk_id = %s", (cids[0],)
        ).fetchone()
    assert row is not None
    return row[0]


def test_memory_search_bumps_card_salience(
    handler: MemoryHandler, store: Store
) -> None:
    mid = _id_of(handler.put(text="copper nitrate reduction pathway"))
    before = _card_last_seen(store, mid)
    handler.search(q="copper nitrate")
    after = _card_last_seen(store, mid)
    assert after > before


def test_memory_search_does_not_bump_for_dream_actor(
    handler: MemoryHandler, store: Store
) -> None:
    mid = _id_of(handler.put(text="palladium hydride storage capacity"))
    before = _card_last_seen(store, mid)
    with as_dream_actor():
        handler.search(q="palladium hydride")
    after = _card_last_seen(store, mid)
    assert after == before


# ---------------------------------------------------------------------------
# Through the runtime dispatcher
# ---------------------------------------------------------------------------


def test_runtime_create_memory(runtime_with_store: PrecisRuntime) -> None:
    out = runtime_with_store.dispatch("put", {"kind": "memory", "text": "via dispatch"})
    assert "created memory id=" in out


def test_runtime_create_then_get(runtime_with_store: PrecisRuntime) -> None:
    create = runtime_with_store.dispatch(
        "put", {"kind": "memory", "text": "round trip"}
    )
    new_id = int(create.rsplit("=", 1)[1].split()[0])

    got = runtime_with_store.dispatch("get", {"kind": "memory", "id": new_id})
    assert "round trip" in got


def test_runtime_search_memory(runtime_with_store: PrecisRuntime) -> None:
    runtime_with_store.dispatch(
        "put", {"kind": "memory", "text": "kwargs vs modes decision"}
    )
    out = runtime_with_store.dispatch("search", {"kind": "memory", "q": "kwargs"})
    assert "kwargs" in out


def test_runtime_unknown_memory_renders_error(
    runtime_with_store: PrecisRuntime,
) -> None:
    out = runtime_with_store.dispatch("get", {"kind": "memory", "id": 99999})
    assert "[error:NotFound]" in out
    assert "next:" in out


def test_kindspec_supports_get_search_put(runtime_with_store: PrecisRuntime) -> None:
    handler = runtime_with_store.hub.handler_for("memory")
    assert handler.spec.is_numeric is True
    assert handler.spec.supports_get is True
    assert handler.spec.supports_search is True
    assert handler.spec.supports_put is True
    assert handler.spec.supports_delete is True
    assert handler.spec.supports_tag is True
    assert handler.spec.supports_link is True
