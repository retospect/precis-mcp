"""Tests for TodoHandler — phase 5 state kind."""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, Gone, NotFound
from precis.handlers.todo import TodoHandler


@pytest.fixture
def handler(hub: Hub) -> TodoHandler:
    return TodoHandler(hub=hub)


# ── put: create ──────────────────────────────────────────────────────


def test_create_assigns_id_and_default_status(handler: TodoHandler) -> None:
    r = handler.put(text="finish the report")
    assert "created todo td" in r.body  # universal handle (e.g. td5)
    # The create-ack carries the initial closed-prefix tag inline as
    # ``(STATUS:open)`` — matches the canonical tag form rather than
    # the prose ``status: open`` we used pre-Slice 1.
    assert "STATUS:open" in r.body


def test_create_response_carries_ref_id(handler: TodoHandler) -> None:
    """A sibling handler (structured-field callers) reads ``ref_id``
    instead of regex-parsing the ack's ``td<id>`` handle."""
    r = handler.put(text="finish the report")
    refs = handler.store.list_refs(kind="todo", limit=10)
    assert r.ref_id == refs[0].id


def test_create_records_status_open_tag(handler: TodoHandler) -> None:
    handler.put(text="task 1")
    refs = handler.store.list_refs(kind="todo", limit=10)
    assert refs
    tags = handler.store.tags_for(refs[0].id)
    assert any("STATUS:open" in str(t) for t in tags)


def test_create_with_extra_tags_keeps_default_first(handler: TodoHandler) -> None:
    handler.put(text="t", tags=["context:work"])
    refs = handler.store.list_refs(kind="todo", limit=10)
    tags = {str(t) for t in handler.store.tags_for(refs[0].id)}
    assert "STATUS:open" in tags
    assert "context:work" in tags


def test_create_requires_text(handler: TodoHandler) -> None:
    with pytest.raises(BadInput, match="creating a todo"):
        handler.put()
    with pytest.raises(BadInput):
        handler.put(text="   ")


# ── optional details body (additive, migration 0050) ─────────────────


def _latest_todo_id(handler: TodoHandler) -> int:
    return handler.store.list_refs(kind="todo", limit=1)[0].id


def test_create_without_body_writes_no_chunk(handler: TodoHandler) -> None:
    """The common case: a todo is just a title — no body chunk at all."""
    handler.put(text="finish the report")
    tid = _latest_todo_id(handler)
    assert handler.store.chunks.list_chunks_for_ref(tid) == []


def test_create_with_body_writes_todo_body_chunk(handler: TodoHandler) -> None:
    """put(body=...) attaches a todo_body chunk; the title stays in
    refs.title (a good header already), the details ride in the chunk."""
    handler.put(text="finish the report", body="cover Q3 revenue and churn")
    tid = _latest_todo_id(handler)
    with handler.store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ord, chunk_kind, text FROM chunks WHERE ref_id = %s",
            (tid,),
        ).fetchall()
    assert rows == [(0, "todo_body", "cover Q3 revenue and churn")]
    # The body renders on the single-ref read.
    detail = handler.get(id=tid).body
    assert "finish the report" in detail
    assert "cover Q3 revenue and churn" in detail


def test_edit_body_replaces_the_chunk(handler: TodoHandler) -> None:
    handler.put(text="finish the report", body="old details")
    tid = _latest_todo_id(handler)
    handler.edit(id=tid, mode="replace", body="new details")
    with handler.store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT chunk_kind, text FROM chunks WHERE ref_id = %s AND ord >= 0",
            (tid,),
        ).fetchall()
    assert rows == [("todo_body", "new details")]


def test_edit_requires_text_or_body(handler: TodoHandler) -> None:
    handler.put(text="finish the report")
    tid = _latest_todo_id(handler)
    with pytest.raises(BadInput, match="requires text= and/or body="):
        handler.edit(id=tid, mode="replace")


# ── put: status transitions ──────────────────────────────────────────


def test_status_done_replaces_open(handler: TodoHandler) -> None:
    """STATUS: is a closed prefix → setting STATUS:done must drop STATUS:open."""
    r = handler.put(text="task")
    todo_id = int(r.body.split("id=")[1].split()[0].rstrip(",.()"))
    handler.tag(id=todo_id, add=["STATUS:done"])
    tags = {str(t) for t in handler.store.tags_for(todo_id)}
    assert "STATUS:done" in tags
    assert "STATUS:open" not in tags


def test_can_transition_to_doing(handler: TodoHandler) -> None:
    r = handler.put(text="task")
    todo_id = int(r.body.split("id=")[1].split()[0].rstrip(",.()"))
    handler.tag(id=todo_id, add=["STATUS:doing"])
    tags = {str(t) for t in handler.store.tags_for(todo_id)}
    assert "STATUS:doing" in tags


# ── tag(): remove UX (gr192827 item 10) ────────────────────────────────


def test_tag_remove_accepts_displayed_open_prefix(handler: TodoHandler) -> None:
    """A ``child-failed:<job>`` bubble displays as ``OPEN:child-failed:<job>``
    everywhere (nursery digest, draft WIP hints, DB namespace column), but
    the wire form never carries the ``OPEN:`` sentinel — it used to reject
    ``remove=['OPEN:child-failed:N']`` as an unregistered closed axis.
    ``tag(remove=...)`` must accept the displayed form too."""
    r = handler.put(text="task")
    todo_id = int(r.body.split("id=")[1].split()[0].rstrip(",.()"))
    handler.tag(id=todo_id, add=["child-failed:999"])
    assert "child-failed:999" in {str(t) for t in handler.store.tags_for(todo_id)}

    handler.tag(id=todo_id, remove=["OPEN:child-failed:999"])
    assert "child-failed:999" not in {str(t) for t in handler.store.tags_for(todo_id)}


def test_tag_remove_response_says_untagged(handler: TodoHandler) -> None:
    """A pure removal reports "untagged", distinct from an add's
    "tagged" — the old response read "tagged todo id=N" for both."""
    r = handler.put(text="task", tags=["topic:x"])
    todo_id = int(r.body.split("id=")[1].split()[0].rstrip(",.()"))
    resp = handler.tag(id=todo_id, remove=["topic:x"])
    assert resp.body.startswith("untagged ")


def test_tag_add_and_remove_together_names_both_verbs(handler: TodoHandler) -> None:
    r = handler.put(text="task", tags=["topic:x"])
    todo_id = int(r.body.split("id=")[1].split()[0].rstrip(",.()"))
    resp = handler.tag(id=todo_id, add=["topic:y"], remove=["topic:x"])
    assert "tagged" in resp.body
    assert "untagged" in resp.body


def test_tag_remove_noop_reports_no_such_tag(handler: TodoHandler) -> None:
    """Removing a tag that isn't present must not report blanket success
    — a bulk cleanup driven by the response shape needs to see that
    nothing actually changed."""
    r = handler.put(text="task")
    todo_id = int(r.body.split("id=")[1].split()[0].rstrip(",.()"))
    resp = handler.tag(id=todo_id, remove=["topic:never-set"])
    assert "no such tag" in resp.body
    assert "no change" in resp.body


def test_tag_status_flip_reports_truthfully(handler: TodoHandler) -> None:
    """gr293293: add=['STATUS:done'], remove=['STATUS:open'] both apply
    (the add's replace_prefix clears STATUS:open before the remove= loop
    runs), but the old response read "tagged todo id=N (no such tag,
    unchanged: STATUS:open)" — actively lying about the remove= half of a
    call that fully succeeded. Pins the honest wording."""
    r = handler.put(text="task")  # default_tags_on_create seeds STATUS:open
    todo_id = int(r.body.split("id=")[1].split()[0].rstrip(",.()"))
    resp = handler.tag(id=todo_id, add=["STATUS:done"], remove=["STATUS:open"])
    assert resp.body == (
        f"tagged/untagged todo id={todo_id} (already cleared by the add=: STATUS:open)"
    )
    assert "no such tag" not in resp.body
    tags = {str(t) for t in handler.store.tags_for(todo_id)}
    assert "STATUS:done" in tags
    assert "STATUS:open" not in tags


def test_tag_status_flip_genuine_noop_still_reports_no_such_tag(
    handler: TodoHandler,
) -> None:
    """Same add=/remove= shape, but the remove= tag was never present
    (typo'd STATUS value the ref never carried) — must still read as a
    genuine no-op, not get swept into the subsumed bucket."""
    r = handler.put(text="task")  # seeds STATUS:open, not STATUS:blocked
    todo_id = int(r.body.split("id=")[1].split()[0].rstrip(",.()"))
    resp = handler.tag(id=todo_id, add=["STATUS:done"], remove=["STATUS:blocked"])
    assert resp.body == (
        f"tagged todo id={todo_id} (no such tag, unchanged: STATUS:blocked)"
    )
    assert "already cleared" not in resp.body


# ── get: single + list views ─────────────────────────────────────────


def test_get_single(handler: TodoHandler) -> None:
    r = handler.put(text="finish the report")
    todo_id = int(r.body.split("id=")[1].split()[0].rstrip(",.()"))
    out = handler.get(id=todo_id)
    assert "finish the report" in out.body
    assert "STATUS:open" in out.body  # tags rendered


def test_get_missing_raises_not_found(handler: TodoHandler) -> None:
    with pytest.raises(NotFound, match="todo id=99999 not found"):
        handler.get(id=99999)


def test_list_recent(handler: TodoHandler) -> None:
    handler.put(text="a")
    handler.put(text="b")
    handler.put(text="c")
    out = handler.get(id="/recent")
    assert "recent todo (3)" in out.body
    assert "a" in out.body and "b" in out.body and "c" in out.body


def test_list_open_filters_by_status(handler: TodoHandler) -> None:
    r1 = handler.put(text="open one")
    id1 = int(r1.body.split("id=")[1].split()[0].rstrip(",.()"))
    r2 = handler.put(text="finished one")
    id2 = int(r2.body.split("id=")[1].split()[0].rstrip(",.()"))
    handler.tag(id=id2, add=["STATUS:done"])

    out = handler.get(id="/open")
    assert "open one" in out.body
    assert "finished one" not in out.body
    assert str(id1) in out.body
    # The hint trailer references the operations available.
    assert "Next:" in out.body


def test_list_done_only_shows_done(handler: TodoHandler) -> None:
    r1 = handler.put(text="will-finish")
    r2 = handler.put(text="still-open")
    id1 = int(r1.body.split("id=")[1].split()[0].rstrip(",.()"))
    handler.tag(id=id1, add=["STATUS:done"])
    out = handler.get(id="/done")
    assert "will-finish" in out.body
    assert "still-open" not in out.body


def test_list_empty_status(handler: TodoHandler) -> None:
    out = handler.get(id="/done")
    assert "no todos" in out.body


def test_bare_get_lists_recent(handler: TodoHandler) -> None:
    handler.put(text="something")
    out = handler.get()
    assert "recent todo" in out.body


# ── delete ─────────────────────────────────────────────────────────


def test_delete(handler: TodoHandler) -> None:
    r = handler.put(text="ephemeral")
    todo_id = int(r.body.split("id=")[1].split()[0].rstrip(",.()"))
    handler.delete(id=todo_id)
    # MCP critic MINOR-C (round 1): soft-deleted refs raise ``Gone``
    # (distinct from ``NotFound`` for never-existed ids) so the LLM
    # can tell a tombstone from a typo.
    with pytest.raises(Gone, match="soft-deleted"):
        handler.get(id=todo_id)


def test_delete_cascades_to_descendant_todos(handler: TodoHandler) -> None:
    """Deleting a todo takes its live subtree with it — a child left
    under a deleted parent evades the nursery orphan walk (which needs
    a live parent chain) and rots forever as a stuck-doable leaf."""
    root_id = handler.put(text="root").ref_id
    child_id = handler.put(text="child").ref_id
    grandchild_id = handler.put(text="grandchild").ref_id
    bystander_id = handler.put(text="unrelated").ref_id
    assert root_id and child_id and grandchild_id and bystander_id
    handler.link(id=child_id, target=f"todo:{root_id}", rel="parent")
    handler.link(id=grandchild_id, target=f"todo:{child_id}", rel="parent")

    r = handler.delete(id=root_id)
    assert "+2 descendant todos" in r.body
    for gone_id in (root_id, child_id, grandchild_id):
        with pytest.raises(Gone, match="soft-deleted"):
            handler.get(id=gone_id)
    # An unrelated root is untouched.
    assert handler.get(id=bystander_id)


# ── search ─────────────────────────────────────────────────────────


def test_search(handler: TodoHandler) -> None:
    handler.put(text="upgrade the postgres server")
    handler.put(text="something completely different")
    r = handler.search(q="postgres")
    assert "postgres" in r.body
    assert "1 todo match" in r.body


def test_search_no_match(handler: TodoHandler) -> None:
    handler.put(text="hello")
    r = handler.search(q="frobnicate")
    assert "no todo entries match" in r.body


# ── Next: trailer hint round-trips ─────────────────────────────────────


def test_recent_list_hint_round_trips(handler: TodoHandler) -> None:
    """Base-class instance: the shared ``NumericRefHandler._list_view``'s
    ``/recent`` trailer used to advertise a bareword ``id=N`` — ships
    on EVERY numeric-ref kind's
    ``/recent`` list. It now interpolates the first listed row's real
    id (todo exercises the base-class fallback for 'recent')."""
    from tests.hintcheck import assert_hints_round_trip

    handler.put(text="first todo")
    handler.put(text="second todo")
    resp = handler.get(id="/recent")
    # ``/recent`` is most-recent-first — the top row is whatever
    # ``_list_view`` actually rendered, not necessarily the first put.
    top_row_id = handler.store.list_refs(kind="todo", limit=20)[0].id

    def dispatch(verb: str, kwargs: dict[str, Any]) -> object:
        kwargs = dict(kwargs)
        kwargs.pop("kind", None)
        return getattr(handler, verb)(**kwargs)

    hints = assert_hints_round_trip(resp.body, dispatch, whole_body=True)
    get_hints = [h for h in hints if h.startswith("get(")]
    assert get_hints, f"expected a get(...) hint: {hints!r}"
    assert f"id={top_row_id}" in get_hints[0]


def test_status_list_hints_round_trip(handler: TodoHandler) -> None:
    """``_render_status_list``'s two hints used to advertise a
    bareword ``id=N`` — a ``CommandParseError`` on copy-paste. Both
    now interpolate the first row's real id."""
    from tests.hintcheck import assert_hints_round_trip

    r = handler.put(text="an open todo")
    rid = r.ref_id
    resp = handler.get(id="/open")

    def dispatch(verb: str, kwargs: dict[str, Any]) -> object:
        kwargs = dict(kwargs)
        kwargs.pop("kind", None)
        return getattr(handler, verb)(**kwargs)

    hints = assert_hints_round_trip(resp.body, dispatch, whole_body=True)
    assert any(f"id={rid}" in h for h in hints), hints
    tag_hints = [h for h in hints if h.startswith("tag(")]
    assert tag_hints and f"id={rid}" in tag_hints[0]
