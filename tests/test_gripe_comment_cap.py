"""Bounded default gripe timeline with an explicit complete history read."""

import pytest

from precis.dispatch import Hub
from precis.errors import Unsupported
from precis.handlers.gripe import GripeHandler
from precis.store import Tag
from precis.store.types import ChunkInsert


@pytest.mark.parametrize("count", [0, 3, 8, 55])
def test_comment_timeline_default_and_full(hub: Hub, count: int) -> None:
    handler = GripeHandler(hub=hub)
    handler.put(text="Synthetic timeline body")
    rid = next(
        r.id
        for r in handler.store.list_refs(kind="gripe")
        if r.title == "Synthetic timeline body"
    )
    handler.store.chunks.insert_chunks(
        rid,
        [
            ChunkInsert(
                ord=i,
                text=f"fixture-comment-{i:03d}",
                meta={"chunk_kind": "gripe_comment"},
            )
            for i in range(1, count + 1)
        ],
    )
    default = handler.get(id=rid).body
    assert "Synthetic timeline body" in default
    # newest 3 in full (newest first); older ones as one-liners, capped at 20
    assert default.count("## comment ") == min(count, 3)
    for i in range(1, count + 1):
        assert (f"fixture-comment-{i:03d}" in default) == (i > count - 23)
    if count > 23:
        assert "+32 older" in default
        assert default.index("## comment 55") < default.index("## comment 53")
    else:
        assert "+" not in default
    if count:
        assert "view='comments'" in default
    full = handler.get(id=rid, view="comments").body
    assert full.count("## comment ") == count
    for i in range(1, count + 1):
        assert f"fixture-comment-{i:03d}" in full
    # Audit log remains an independent read, never repurposed as comments.
    assert "fixture-comment-" not in handler.get(id=rid, view="log").body


def test_legacy_body_and_view_recovery(hub: Hub) -> None:
    handler = GripeHandler(hub=hub)
    with handler.store.tx() as conn:
        ref = handler.store.insert_ref(
            kind="gripe", slug=None, title="Synthetic legacy body", conn=conn
        )
        handler.store.add_tag(ref.id, Tag.closed("STATUS", "open"), conn=conn)
    assert "Synthetic legacy body" in handler.get(id=ref.id).body
    assert "Synthetic legacy body" in handler.get(id=ref.id, view="comments").body
    with pytest.raises(Unsupported) as error:
        handler.get(id=ref.id, view="bad-view")
    assert error.value.options is not None
    assert "comments" in error.value.options
