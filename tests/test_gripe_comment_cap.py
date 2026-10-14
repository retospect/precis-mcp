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
    # gripe's render-diet policy: the newest comment in full, then the
    # newest 8 older ones as one-line gists, then ``+N older``.
    assert default.count("## comment ") == min(count, 1)
    for i in range(1, count + 1):
        assert (f"fixture-comment-{i:03d}" in default) == (i > count - 9)
    if count > 9:
        assert f"+{count - 9} older" in default
        assert default.index("## comment") < default.index("older")
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


def test_body_head_marks_and_view_comments_carries_the_rest(hub: Hub) -> None:
    """A body over ``_BODY_HEAD_CHARS`` cuts at a line boundary in the
    bare get, with a ``+N chars`` marker naming ``view='comments'`` —
    the complete read keeps it whole. The marker is inline because a
    comment-free gripe has no comments footer to point at it."""
    from precis.handlers.gripe import _BODY_HEAD_CHARS

    handler = GripeHandler(hub=hub)
    body = "first line of a long gripe\n" + ("detail line\n" * 200)
    assert len(body) > _BODY_HEAD_CHARS
    handler.put(text=body)
    rid = next(r.id for r in handler.store.list_refs(kind="gripe"))
    default = handler.get(id=rid).body
    assert "first line of a long gripe" in default
    assert "[+" in default
    assert f"chars — get(kind='gripe', id={rid}, view='comments')]" in default
    assert 0 < default.count("detail line") < 200
    assert handler.get(id=rid, view="comments").body.count("detail line") == 200


def test_short_body_renders_uncut(hub: Hub) -> None:
    handler = GripeHandler(hub=hub)
    handler.put(text="small gripe, small body")
    rid = next(r.id for r in handler.store.list_refs(kind="gripe"))
    assert "[+" not in handler.get(id=rid).body


def test_newest_comment_head_capped_older_untouched(hub: Hub) -> None:
    """The gripe policy's per-comment budget: a newest comment past 800
    chars head-cuts with ``…``; the complete read shows it whole."""
    handler = GripeHandler(hub=hub)
    handler.put(text="gripe with a long newest comment")
    rid = next(r.id for r in handler.store.list_refs(kind="gripe"))
    handler.put(id=rid, text="short first comment")
    handler.put(id=rid, text="long comment " + "x" * 900)
    default = handler.get(id=rid).body
    assert "## comment 2" in default
    assert "x" * 900 not in default
    assert " …" in default
    # the older short comment survives as a one-liner gist
    assert "short first comment" in default
    full = handler.get(id=rid, view="comments").body
    assert "x" * 900 in full


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
