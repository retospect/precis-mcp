"""Tests for TodoHandler — phase 5 state kind."""

from __future__ import annotations

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
    assert "created todo td" in r.body  # ADR 0036 handle (e.g. td5)
    # The create-ack carries the initial closed-prefix tag inline as
    # ``(STATUS:open)`` — matches the canonical tag form rather than
    # the prose ``status: open`` we used pre-Slice 1.
    assert "STATUS:open" in r.body


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
    """The common case: a todo is just a task line — no body chunk at all."""
    handler.put(text="finish the report")
    tid = _latest_todo_id(handler)
    assert handler.store.list_blocks_for_ref(tid) == []


def test_create_with_body_writes_todo_body_chunk(handler: TodoHandler) -> None:
    """put(body=...) attaches a todo_body chunk; the task line stays in
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
