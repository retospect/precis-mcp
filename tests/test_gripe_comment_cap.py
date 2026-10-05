"""Bounded default gripe timeline with an explicit complete history read."""

import pytest

from precis.dispatch import Hub
from precis.handlers.gripe import GripeHandler
from precis.store.types import ChunkInsert


@pytest.mark.parametrize("count", [0, 20, 55])
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
    assert default.count("## comment ") == min(count, 20)
    for i in range(1, count + 1):
        assert (f"fixture-comment-{i:03d}" in default) == (i > count - 20)
    if count > 20:
        assert "+35 more" in default
        assert "view='comments'" in default
        assert default.index("## comment 36") < default.index("## comment 55")
    else:
        assert "+" not in default
    full = handler.get(id=rid, view="comments").body
    assert full.count("## comment ") == count
    for i in range(1, count + 1):
        assert f"fixture-comment-{i:03d}" in full
    # Audit log remains an independent read, never repurposed as comments.
    assert "fixture-comment-" not in handler.get(id=rid, view="log").body
