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
    assert "created todo id=" in r.body
    assert "status: open" in r.body


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
