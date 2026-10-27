"""Closing a gripe: STATUS:done/wontfix needs a newest ``resolved:`` comment."""

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.gripe import GripeHandler


def _gripe(hub: Hub) -> tuple[GripeHandler, int]:
    h = GripeHandler(hub=hub)
    h.put(text="Synthetic close-guard gripe")
    gid = next(
        r.id for r in h.store.list_refs(kind="gripe") if "close-guard" in r.title
    )
    return h, gid


def _tags(h: GripeHandler, gid: int) -> set[str]:
    return {str(t) for t in h.store.tags_for(gid)}


@pytest.mark.parametrize("status", ["STATUS:done", "STATUS:wontfix"])
def test_close_refused_without_resolved_comment(hub: Hub, status: str) -> None:
    h, gid = _gripe(hub)
    with pytest.raises(BadInput) as ei:
        h.tag(id=gid, add=[status])
    assert f"put(kind='gripe', id={gid}, text='resolved:" in str(ei.value.next)
    assert "STATUS:open" in _tags(h, gid)


def test_close_refused_when_newest_comment_is_not_resolved(hub: Hub) -> None:
    h, gid = _gripe(hub)
    h.put(id=gid, text="resolved: abc (r1) — ok")
    h.put(id=gid, text="actually still broken")
    with pytest.raises(BadInput):
        h.tag(id=gid, add=["STATUS:done"])


def test_done_succeeds_after_resolved_comment(hub: Hub) -> None:
    h, gid = _gripe(hub)
    h.put(id=gid, text="resolved: abc123 (r41) — ran the repro, passes")
    h.tag(id=gid, add=["STATUS:done"])
    assert "STATUS:done" in _tags(h, gid)


def test_wontfix_succeeds_after_resolved_comment(hub: Hub) -> None:
    h, gid = _gripe(hub)
    h.put(id=gid, text="resolved: wontfix — by design")
    h.tag(id=gid, add=["STATUS:wontfix"])
    assert "STATUS:wontfix" in _tags(h, gid)


def test_put_with_tags_closes_in_one_call(hub: Hub) -> None:
    h, gid = _gripe(hub)
    h.put(id=gid, text="resolved: abc (r1) — verified", tags=["STATUS:done"])
    assert "STATUS:done" in _tags(h, gid)


def test_other_status_changes_unguarded(hub: Hub) -> None:
    h, gid = _gripe(hub)
    h.tag(id=gid, add=["STATUS:triaged"])
    assert "STATUS:triaged" in _tags(h, gid)


def test_close_cascades_to_blocked_todo(hub: Hub) -> None:
    """gr520237: closing a gripe notifies referrers and unblocks todos
    whose last live blocker cleared — including a ``wontfix`` close,
    which the old ``('done','won''t-do')`` blocker check never saw."""
    from precis.handlers.todo import TodoHandler

    h, gid = _gripe(hub)
    todo = TodoHandler(hub=hub)
    todo.put(text="cascade target leaf")
    tid = next(r.id for r in todo.store.list_refs(kind="todo"))
    todo.store.add_link(src_ref_id=tid, dst_ref_id=gid, relation="blocked-by")
    todo.tag(id=tid, add=["STATUS:blocked"])

    h.put(id=gid, text="resolved: wontfix — by design")
    h.tag(id=gid, add=["STATUS:wontfix"])

    comments = [
        c.text
        for c in todo.store.chunks.list_chunks_for_ref(tid)
        if c.chunk_kind == "todo_comment"
    ]
    assert comments == [
        f"gr{gid} closed (resolved: wontfix — by design) — next: unblocked"
    ]
    assert "STATUS:open" in {str(t) for t in todo.store.tags_for(tid)}

    # Idempotent: a re-close (done after wontfix) does not double-post.
    h.put(id=gid, text="resolved: abc (r1) — verified")
    h.tag(id=gid, add=["STATUS:done"])
    comments = [
        c.text
        for c in todo.store.chunks.list_chunks_for_ref(tid)
        if c.chunk_kind == "todo_comment"
    ]
    assert len(comments) == 1


def test_close_keeps_live_blocker_blocking(hub: Hub) -> None:
    """A todo blocked-by the closed gripe AND another live blocker stays
    blocked — the cascade comments re-evaluate, not unblocked."""
    from precis.handlers.todo import TodoHandler

    h, gid = _gripe(hub)
    other = TodoHandler(hub=hub)
    other.put(text="second cascade leaf")
    tid = next(r.id for r in other.store.list_refs(kind="todo"))
    other.store.add_link(src_ref_id=tid, dst_ref_id=gid, relation="blocked-by")
    # Second blocker stays open, so the close clears only one edge.
    _h2, gid2 = _gripe(hub)
    other.store.add_link(src_ref_id=tid, dst_ref_id=gid2, relation="blocked-by")
    other.tag(id=tid, add=["STATUS:blocked"])

    h.put(id=gid, text="resolved: wontfix — nope")
    h.tag(id=gid, add=["STATUS:wontfix"])

    comments = [
        c.text
        for c in other.store.chunks.list_chunks_for_ref(tid)
        if c.chunk_kind == "todo_comment"
    ]
    assert comments[-1].endswith("next: re-evaluate")
    assert "STATUS:blocked" in {str(t) for t in other.store.tags_for(tid)}


def test_close_cascade_skips_terminal_referrers(hub: Hub) -> None:
    """gr520237: a referrer already at a terminal STATUS is settled —
    no re-evaluate note lands on a done todo, a closed sibling gripe,
    or a finished waiter's timeline."""
    from precis.handlers.todo import TodoHandler
    from precis.store.types import Tag

    h, gid = _gripe(hub)
    todo = TodoHandler(hub=hub)

    todo.put(text="cascade done-referrer leaf")
    tid = next(r.id for r in todo.store.list_refs(kind="todo"))
    todo.store.add_link(src_ref_id=tid, dst_ref_id=gid, relation="fixes")
    todo.tag(id=tid, add=["STATUS:done"])

    todo.put(text="cascade done-waiter leaf")
    wid = next(r.id for r in todo.store.list_refs(kind="todo") if "waiter" in r.title)
    todo.store.add_tag(wid, Tag.open(f"waiting-for:gr{gid}"), set_by="system")
    todo.tag(id=wid, add=["STATUS:done"])

    h2, gid2 = _gripe(hub)
    h2.put(id=gid2, text="resolved: abc (r1) — done first")
    h2.tag(id=gid2, add=["STATUS:done"])
    h2.store.add_link(src_ref_id=gid2, dst_ref_id=gid, relation="related-to")

    h.put(id=gid, text="resolved: def (r2) — verified")
    h.tag(id=gid, add=["STATUS:done"])

    for rid, ckind in (
        (tid, "todo_comment"),
        (wid, "todo_comment"),
        (gid2, "gripe_comment"),
    ):
        assert not [
            c
            for c in h.store.chunks.list_chunks_for_ref(rid)
            if c.chunk_kind == ckind and c.text.startswith(f"gr{gid} closed")
        ]


def test_close_cascade_dedupes_past_intervening_comment(hub: Hub) -> None:
    """gr520237: the dedupe scans ALL the referrer's comments for this
    gripe's close marker, not just the newest — an unrelated comment
    between two closes must not let the note double-post."""
    from precis.handlers.todo import TodoHandler

    h, gid = _gripe(hub)
    todo = TodoHandler(hub=hub)
    todo.put(text="cascade dedupe leaf")
    tid = next(r.id for r in todo.store.list_refs(kind="todo"))
    todo.store.add_link(src_ref_id=tid, dst_ref_id=gid, relation="related-to")

    h.put(id=gid, text="resolved: wontfix — by design")
    h.tag(id=gid, add=["STATUS:wontfix"])
    todo.put(id=tid, text="unrelated comment after the first close")
    h.put(id=gid, text="resolved: abc (r1) — verified")
    h.tag(id=gid, add=["STATUS:done"])

    notes = [
        c.text
        for c in todo.store.chunks.list_chunks_for_ref(tid)
        if c.chunk_kind == "todo_comment" and c.text.startswith(f"gr{gid} closed")
    ]
    assert len(notes) == 1


def test_close_cascade_reaches_waiting_for_tag(hub: Hub) -> None:
    """gr520237: a live ref parked on OPEN ``waiting-for:gr<N>`` — no
    link, just the tag — hears the close as a re-evaluate note; the
    park tag itself is left for the owner to clear."""
    from precis.handlers.todo import TodoHandler
    from precis.store.types import Tag

    h, gid = _gripe(hub)
    todo = TodoHandler(hub=hub)
    todo.put(text="cascade waiter leaf")
    tid = next(r.id for r in todo.store.list_refs(kind="todo"))
    todo.store.add_tag(tid, Tag.open(f"waiting-for:gr{gid}"), set_by="agent")

    h.put(id=gid, text="resolved: abc (r1) — verified")
    h.tag(id=gid, add=["STATUS:done"])

    comments = [
        c.text
        for c in todo.store.chunks.list_chunks_for_ref(tid)
        if c.chunk_kind == "todo_comment"
    ]
    assert comments == [
        f"gr{gid} closed (resolved: abc (r1) — verified) — next: re-evaluate"
    ]
    tags = {str(t) for t in todo.store.tags_for(tid)}
    assert f"waiting-for:gr{gid}" in tags
    assert "STATUS:open" in tags


def test_soft_deleted_get_shows_title_status_and_last_comment(hub: Hub) -> None:
    h, gid = _gripe(hub)
    h.put(id=gid, text="resolved: abc (r1) — verified", tags=["STATUS:done"])
    h.delete(id=gid)
    body = h.get(id=gid).body
    assert "soft-deleted" in body
    assert "Synthetic close-guard gripe" in body
    assert "STATUS:done" in body
    assert "resolved: abc (r1) — verified" in body


def test_create_already_closed_refused(hub: Hub) -> None:
    h = GripeHandler(hub=hub)
    with pytest.raises(BadInput):
        h.put(text="born closed", tags=["STATUS:done"])


def test_delete_without_id_names_the_delete_call(hub: Hub) -> None:
    """M3 burn-down (gr494831): delete without id= refuses with next=."""
    h = GripeHandler(hub=hub)
    with pytest.raises(BadInput, match="requires id=") as exc:
        h.delete()
    assert exc.value.next is not None
    assert "delete(kind='gripe', id=" in str(exc.value.next)


def test_unknown_gripe_tombstone_names_search(hub: Hub) -> None:
    """get on an id that never existed (even soft-deleted) carries next=."""
    h = GripeHandler(hub=hub)
    with pytest.raises(NotFound) as exc:
        h._render_tombstone(99999)
    assert exc.value.next is not None
    assert "search(kind='gripe'" in str(exc.value.next)


def test_hidden_resolved_comment_does_not_count(hub: Hub) -> None:
    h, gid = _gripe(hub)
    h.put(id=gid, text="resolved: abc (r1) — ok")
    h.delete(id=gid, args={"comment": 1})
    with pytest.raises(BadInput):
        h.tag(id=gid, add=["STATUS:done"])
