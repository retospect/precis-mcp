"""Gripe get is the title card plus the body (gr499040, gr514351).

A bare ``get(kind='gripe', id=N)`` shows the title, tags, status, the
comment count and the body — head+tail-clipped at a budget with the
cut marker naming ``view='body'``, the uncapped read. The timeline
stays behind ``view='comments'``, which is selectable
(``args={'comments': K}`` newest K, ``args={'comments': 'A-B'}`` a
window, ``args={'comment': K}`` one) and pages whole comments — never
a mid-text cut (gr515093). ``view='full'`` is the bounded complete
read: a long body clips naming ``view='body'`` and elided comments
name their windowed call (gr514350).
"""

import re

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, Unsupported
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
    # the title card: title + count + newest one-liner — no body,
    # no comment blocks
    assert f"comments: {count}" in default
    assert "## comment " not in default
    if count:
        assert f"newest: comment {count} · " in default
        assert f"fixture-comment-{count:03d}"[:60] in default
    else:
        assert "newest:" not in default
    full = handler.get(id=rid, view="comments").body
    assert full.count("## comment ") == count
    for i in range(1, count + 1):
        assert f"fixture-comment-{i:03d}" in full
    # Audit log remains an independent read, never repurposed as comments.
    assert "fixture-comment-" not in handler.get(id=rid, view="log").body


def test_bare_get_is_card_plus_body(hub: Hub) -> None:
    handler = GripeHandler(hub=hub)
    handler.put(text="the whole gripe body\nsecond body line")
    rid = next(r.id for r in handler.store.list_refs(kind="gripe"))
    handler.put(id=rid, text="a comment")
    body = handler.get(id=rid).body
    assert f"# gripe {rid}" in body
    assert "STATUS:open" in body
    assert "title: the whole gripe body" in body
    assert "comments: 1" in body
    assert "newest: comment 1 · " in body and "a comment" in body
    # gr514351: the body is part of the default read — no second call
    assert "second body line" in body
    # the timeline stays behind its own read
    assert "## comment" not in body
    assert (
        "the whole gripe body\nsecond body line"
        in handler.get(id=rid, view="body").body
    )


def test_bare_get_clips_a_long_body_naming_the_escape(hub: Hub) -> None:
    handler = GripeHandler(hub=hub)
    handler.put(text="pad " * 1000 + "the tail ask")
    rid = next(r.id for r in handler.store.list_refs(kind="gripe"))
    body = handler.get(id=rid).body
    assert f"get(kind='gripe', id={rid}, view='body')" in body
    assert "the tail ask" in body  # tail survives the middle cut
    assert "chars cut" in body


def test_comments_view_window_and_single(hub: Hub) -> None:
    handler = GripeHandler(hub=hub)
    handler.put(text="window fixture")
    rid = next(r.id for r in handler.store.list_refs(kind="gripe"))
    for i in range(1, 11):
        handler.put(id=rid, text=f"comment number {i}")
    window = handler.get(id=rid, view="comments", comments=3).body
    assert "comments: newest 3 of 10" in window
    for i in (8, 9, 10):
        assert f"comment number {i}" in window
    assert "comment number 7" not in window
    # oldest-first inside the window, like the complete view
    assert window.index("number 8") < window.index("number 10")
    one = handler.get(id=rid, view="comments", comment=4).body
    assert "comment number 4" in one and "comment 4 of 10" in one
    assert "comment number 5" not in one and "## comment 5" not in one


def test_comments_view_pages_whole_comments_never_cut(hub: Hub) -> None:
    # gr515093: the page budget bounds the page, not a comment — the
    # view never cuts inside one.
    from precis.handlers._comment_timeline import COMMENT_PAGE_CHARS

    handler = GripeHandler(hub=hub)
    handler.put(text="paging fixture")
    rid = next(r.id for r in handler.store.list_refs(kind="gripe"))
    # ~640-char comments: 4 fit a 3000-char page, the 5th can't start.
    for i in range(1, 9):
        handler.put(id=rid, text="y" * 600 + f" tail-{i}")
    view = handler.get(id=rid, view="comments").body
    assert "chars cut" not in view
    assert "## comment 1" in view and "tail-4" in view
    assert "## comment 5" not in view
    # the page names exactly what it elided and the one call that reads it
    assert "comments 5-8 elided" in view
    assert "args={'comments': '5-8'}" in view
    rest = handler.get(id=rid, view="comments", comments="5-8").body
    for i in (5, 6, 7, 8):
        assert f"tail-{i}" in rest
    assert "## comment 4" not in rest
    # a lone comment bigger than the page renders whole — the view's
    # bound is the transport fold, never a mid-text cut
    handler.put(id=rid, text="z " * (COMMENT_PAGE_CHARS + 200))
    view = handler.get(id=rid, view="comments", comments="9-9").body
    assert "chars cut" not in view and "z " * 100 in view


def test_comments_range_selector_reads_a_window(hub: Hub) -> None:
    # gr515093: comments 7-9 took three per-comment fetches; the range
    # selector reads them in one.
    handler = GripeHandler(hub=hub)
    handler.put(text="range fixture")
    rid = next(r.id for r in handler.store.list_refs(kind="gripe"))
    for i in range(1, 10):
        handler.put(id=rid, text=f"comment number {i}")
    window = handler.get(id=rid, view="comments", comments="7-9").body
    for i in (7, 8, 9):
        assert f"comment number {i}" in window
    assert "## comment 6" not in window and "## comment 4" not in window
    assert "comments: 7-9 of 9" in window
    open_end = handler.get(id=rid, view="comments", comments="8-").body
    assert "comment number 8" in open_end and "comment number 9" in open_end
    assert "## comment 7" not in open_end


def test_comments_range_selector_validates(hub: Hub) -> None:
    handler = GripeHandler(hub=hub)
    handler.put(text="range validate fixture")
    rid = next(r.id for r in handler.store.list_refs(kind="gripe"))
    handler.put(id=rid, text="c1")
    handler.put(id=rid, text="c2")
    with pytest.raises(BadInput, match="1 <= A <= B"):
        handler.get(id=rid, view="comments", comments="2-1")
    with pytest.raises(BadInput, match="count or 'A-B' range"):
        handler.get(id=rid, view="comments", comments="bogus")
    with pytest.raises(BadInput, match="no comments in range"):
        handler.get(id=rid, view="comments", comments="5-9")


def test_full_view_bounds_and_names_the_reads(hub: Hub) -> None:
    # gr514350: 'full' stays inside the agent-facing fold; what it
    # elides is named with the call that reads it — no cursor needed.
    handler = GripeHandler(hub=hub)
    handler.put(text="pad " * 600 + "the tail ask")
    rid = next(r.id for r in handler.store.list_refs(kind="gripe"))
    for i in range(1, 6):
        handler.put(id=rid, text="q" * 400 + f" ctail-{i}")
    body = handler.get(id=rid, view="full").body
    # the clipped body names view='body' (the same marker the card uses)
    assert f"get(kind='gripe', id={rid}, view='body')" in body
    assert "the tail ask" in body
    # whatever comments the page couldn't hold are named as a window
    if "elided" in body:
        span = re.search(r"comments (\d+-\d+) elided", body)
        assert span is not None
        assert f"args={{'comments': '{span.group(1)}'}}" in body
        # and that window really is a one-call read
        more = handler.get(id=rid, view="comments", comments=span.group(1)).body
        assert "chars cut" not in more


def test_full_view_with_no_room_lists_the_window(hub: Hub) -> None:
    # a fat header (many tags) plus the clipped body eats the page:
    # 'full' names every comment ord rather than squeezing a fraction
    # of one onto it.
    handler = GripeHandler(hub=hub)
    handler.put(text="pad " * 900 + "the tail ask")
    rid = next(r.id for r in handler.store.list_refs(kind="gripe"))
    handler.tag(id=rid, add=[f"bulk:t{i:03d}-{'a' * 20}" for i in range(90)])
    for i in range(1, 4):
        handler.put(id=rid, text=f"small comment {i}")
    body = handler.get(id=rid, view="full").body
    assert "comments 1-3 elided" in body
    assert "args={'comments': '1-3'}" in body
    assert "## comment" not in body


def test_comments_view_single_read_still_uncapped(hub: Hub) -> None:
    handler = GripeHandler(hub=hub)
    handler.put(text="cap fixture")
    rid = next(r.id for r in handler.store.list_refs(kind="gripe"))
    long_text = "x " * 2500
    handler.put(id=rid, text=long_text)
    one = handler.get(id=rid, view="comments", comment=1).body
    assert long_text in one
    assert long_text in handler.get(id=rid, view="full").body


def test_comment_selectors_validate(hub: Hub) -> None:
    handler = GripeHandler(hub=hub)
    handler.put(text="selector fixture")
    rid = next(r.id for r in handler.store.list_refs(kind="gripe"))
    handler.put(id=rid, text="c1")
    handler.put(id=rid, text="c2")
    with pytest.raises(BadInput, match="no comment 9"):
        handler.get(id=rid, view="comments", comment=9)
    with pytest.raises(BadInput, match=">= 1"):
        handler.get(id=rid, view="comments", comments=0)
    with pytest.raises(BadInput, match="not both"):
        handler.get(id=rid, view="comments", comment=1, comments=2)
    # selectors only mean something inside view='comments'
    with pytest.raises(BadInput, match="view='comments'"):
        handler.get(id=rid, comments=2)
    # comments view on a comment-free gripe says so, not a blank page
    handler.put(text="empty gripe")
    rid2 = next(
        r.id for r in handler.store.list_refs(kind="gripe") if r.title == "empty gripe"
    )
    assert "no comments" in handler.get(id=rid2, view="comments").body


def test_legacy_body_and_view_recovery(hub: Hub) -> None:
    handler = GripeHandler(hub=hub)
    with handler.store.tx() as conn:
        ref = handler.store.insert_ref(
            kind="gripe", slug=None, title="Synthetic legacy body", conn=conn
        )
        handler.store.add_tag(ref.id, Tag.closed("STATUS", "open"), conn=conn)
    # a legacy gripe's title is its body — the title card still names it,
    # and view='body' falls back to the title like the old render did.
    assert "Synthetic legacy body" in handler.get(id=ref.id).body
    assert "Synthetic legacy body" in handler.get(id=ref.id, view="body").body
    assert "Synthetic legacy body" in handler.get(id=ref.id, view="full").body
    with pytest.raises(Unsupported) as error:
        handler.get(id=ref.id, view="bad-view")
    assert error.value.options is not None
    assert "comments" in error.value.options
    assert "body" in error.value.options
