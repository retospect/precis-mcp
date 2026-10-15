"""Bounded default gripe timeline with an explicit complete history read."""

import re

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


def test_body_cut_keeps_head_and_tail_and_view_comments_carries_the_rest(
    hub: Hub,
) -> None:
    """A body past ``_BODY_HEAD_CHARS`` plus slack keeps its head and its
    tail (the fix shape lives at the end) around a sized marker naming
    ``view='comments'`` — the complete read keeps it whole. The marker is
    inline because a comment-free gripe has no comments footer."""
    from precis.handlers.gripe import _BODY_HEAD_CHARS

    handler = GripeHandler(hub=hub)
    body = (
        "first line of a long gripe\n"
        + ("detail line\n" * 200)
        + "Fix shape: keep the tail"
    )
    assert len(body) > _BODY_HEAD_CHARS * 1.25
    handler.put(text=body)
    rid = next(r.id for r in handler.store.list_refs(kind="gripe"))
    default = handler.get(id=rid).body
    assert "first line of a long gripe" in default
    assert default.rstrip().endswith("Fix shape: keep the tail")
    marker = re.search(r"\[… (\d+) chars cut — (.*?) …\]", default)
    assert marker is not None
    assert marker.group(2) == f"get(kind='gripe', id={rid}, view='comments')"
    shown = default.count("detail line")
    assert 0 < shown < 200
    assert int(marker.group(1)) >= (200 - shown) * len("detail line\n") - 2
    assert handler.get(id=rid, view="comments").body.count("detail line") == 200


def test_body_barely_over_budget_renders_whole(hub: Hub) -> None:
    """gr489192: a body 20% over budget is not cut — the cut would hide
    more than the overage it saves."""
    from precis.handlers.gripe import _BODY_HEAD_CHARS

    handler = GripeHandler(hub=hub)
    body = "x " * int(_BODY_HEAD_CHARS * 0.6) + "\nLikely culprit: the tail"
    assert _BODY_HEAD_CHARS < len(body) <= _BODY_HEAD_CHARS * 1.25
    handler.put(text=body)
    rid = next(r.id for r in handler.store.list_refs(kind="gripe"))
    default = handler.get(id=rid).body
    assert "chars cut" not in default
    assert "Likely culprit: the tail" in default


def test_short_body_renders_uncut(hub: Hub) -> None:
    handler = GripeHandler(hub=hub)
    handler.put(text="small gripe, small body")
    rid = next(r.id for r in handler.store.list_refs(kind="gripe"))
    assert "chars cut" not in handler.get(id=rid).body


def test_newest_comment_cut_keeps_its_tail_ask(hub: Hub) -> None:
    """gr489190/gr489191: a newest comment past the 800-char budget plus
    slack keeps head and tail (the current-state ask sits at the end)
    around a sized marker; the complete read shows it whole."""
    handler = GripeHandler(hub=hub)
    handler.put(text="gripe with a long newest comment")
    rid = next(r.id for r in handler.store.list_refs(kind="gripe"))
    handler.put(id=rid, text="short first comment")
    handler.put(
        id=rid,
        text="long comment " + "x " * 700 + "\nNeeds slice 5b or Reto's ruling.",
    )
    default = handler.get(id=rid).body
    assert "## comment 2" in default
    assert "x " * 700 not in default
    assert "long comment" in default
    assert "Needs slice 5b or Reto's ruling." in default
    assert re.search(
        rf"\[… \d+ chars cut — get\(kind='gripe', id={rid}, view='comments'\) …\]",
        default,
    )
    # the older short comment survives as a one-liner gist
    assert "short first comment" in default
    full = handler.get(id=rid, view="comments").body
    assert "x " * 700 in full


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
