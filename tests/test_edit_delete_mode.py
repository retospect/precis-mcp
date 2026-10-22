"""gr503460: ``edit(mode='delete', find='…')`` — explicit span deletion.

``mode='find-replace', text=''`` always deleted a matched span
server-side, but several MCP clients serialise the empty string badly
(the call never reaches the server). ``mode='delete'`` is the same
operation under a spelling that never needs an empty-string argument:
``find=`` only, ``text=`` refused, ``before=``/``after=``/``match=``/
``nth=``/``dry_run`` honoured, and the audit row is marked
``op='delete-span'`` so ``view='log'`` distinguishes it from a replace.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.memory import MemoryHandler
from precis.handlers.todo import TodoHandler
from precis.store import Store
from tests.conftest import id_of


@pytest.fixture
def memory(hub: Hub) -> MemoryHandler:
    return MemoryHandler(hub=hub)


@pytest.fixture
def todo(hub: Hub) -> TodoHandler:
    return TodoHandler(hub=hub)


def _body(handler: MemoryHandler | TodoHandler, ref_id: int) -> str:
    ref = handler.store.get_ref(kind=handler.kind, id=ref_id)
    assert ref is not None
    body = handler._body_text(ref)
    assert body is not None
    return body


# ── memory ────────────────────────────────────────────────────────────


def test_memory_delete_mode_removes_span(memory: MemoryHandler) -> None:
    mid = id_of(memory.put(text="line one\nline two\nline three").body)
    r = memory.edit(id=mid, mode="delete", find="line two\n")
    assert "deleted from body" in r.body
    assert _body(memory, mid) == "line one\nline three"


def test_memory_delete_mode_honours_anchors(memory: MemoryHandler) -> None:
    mid = id_of(memory.put(text="aa bb cc bb dd").body)
    memory.edit(id=mid, mode="delete", find="bb", before="cc ", after=" dd")
    assert _body(memory, mid) == "aa bb cc  dd"  # only the anchored bb went


def test_memory_delete_mode_refuses_text(memory: MemoryHandler) -> None:
    mid = id_of(memory.put(text="body").body)
    with pytest.raises(BadInput, match="takes find= only"):
        memory.edit(id=mid, mode="delete", find="body", text="x")


def test_memory_delete_mode_requires_find(memory: MemoryHandler) -> None:
    mid = id_of(memory.put(text="body").body)
    with pytest.raises(BadInput, match="requires find="):
        memory.edit(id=mid, mode="delete")


def test_memory_delete_mode_dry_run(memory: MemoryHandler) -> None:
    mid = id_of(memory.put(text="keep this\ndrop this").body)
    r = memory.edit(id=mid, mode="delete", find="drop this", dry_run=True)
    assert "drop this" in r.body
    assert _body(memory, mid) == "keep this\ndrop this"  # nothing written


def test_memory_delete_mode_logs_delete_span(
    memory: MemoryHandler, store: Store
) -> None:
    mid = id_of(memory.put(text="alpha beta gamma").body)
    memory.edit(id=mid, mode="delete", find="beta ")
    events = store.events_for(mid, limit=3)
    assert any(e.payload.get("op") == "delete-span" for e in events)


# ── todo ──────────────────────────────────────────────────────────────


def test_todo_delete_mode_removes_span(todo: TodoHandler) -> None:
    tid = id_of(todo.put(text="task", body="line one\nline two\nline three").body)
    r = todo.edit(id=tid, mode="delete", find="line two\n")
    assert "deleted from details body" in r.body
    assert _body(todo, tid) == "line one\nline three"


def test_todo_delete_mode_refuses_text(todo: TodoHandler) -> None:
    tid = id_of(todo.put(text="task", body="details").body)
    with pytest.raises(BadInput, match="takes find= only"):
        todo.edit(id=tid, mode="delete", find="details", text="x")


def test_todo_delete_mode_dry_run(todo: TodoHandler) -> None:
    tid = id_of(todo.put(text="task", body="keep\ndrop").body)
    r = todo.edit(id=tid, mode="delete", find="drop", dry_run=True)
    assert "drop" in r.body
    assert _body(todo, tid) == "keep\ndrop"


def test_todo_delete_mode_logs_delete_span(todo: TodoHandler, store: Store) -> None:
    tid = id_of(todo.put(text="task", body="alpha beta").body)
    todo.edit(id=tid, mode="delete", find="beta")
    events = store.events_for(tid, limit=3)
    assert any(e.payload.get("op") == "delete-span" for e in events)
