"""Shared comment timeline (gripe + todo): append, display policy, hide/restore,
unknown-args hint, and todo details-body find-replace/insert."""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.gripe import GripeHandler
from precis.handlers.todo import TodoHandler
from precis.store.types import TODO_BODY_KIND


@pytest.fixture
def gripe(hub: Hub) -> GripeHandler:
    return GripeHandler(hub=hub)


@pytest.fixture
def todo(hub: Hub) -> TodoHandler:
    return TodoHandler(hub=hub)


def _new(handler: GripeHandler | TodoHandler, text: str = "base text") -> int:
    handler.put(text=text)
    return handler.store.list_refs(kind=handler.kind, limit=1)[0].id


def _pick(
    which: str, gripe: GripeHandler, todo: TodoHandler
) -> GripeHandler | TodoHandler:
    return gripe if which == "gripe" else todo


@pytest.mark.parametrize("which", ["gripe", "todo"])
def test_append_ack_and_display_policy(
    which: str, gripe: GripeHandler, todo: TodoHandler
) -> None:
    h = _pick(which, gripe, todo)
    rid = _new(h)
    for i in range(1, 6):
        ack = h.put(id=rid, text=f"note-{i}").body
    assert f"appended comment 5 to {which} {rid}." in ack
    assert "newest 3 in full" in ack and "args={'comment': 5}" in ack
    out = h.get(id=rid).body
    heads = [ln for ln in out.splitlines() if ln.startswith("## comment ")]
    assert [ln.split()[2] for ln in heads] == ["5", "4", "3"]
    assert "Z" in heads[0] and "·" in heads[0]
    oneliners = [ln for ln in out.splitlines() if ln.startswith("comment ")]
    assert [ln.split()[1] for ln in oneliners] == ["2", "1"]
    assert oneliners[0].endswith("· note-2")
    assert out.index("note-5") < out.index("note-4") < out.index("note-3")
    assert "comments: newest 3 in full, older as one-liners" in out
    assert f"get(kind='{which}', id={rid}, view='comments')" in out
    full = h.get(id=rid, view="comments").body
    assert full.index("note-1") < full.index("note-2") < full.index("note-5")
    assert full.count("## comment ") == 5


def test_todo_comment_ord_never_collides_with_body(todo: TodoHandler) -> None:
    rid = _new(todo)
    todo.put(id=rid, text="c1")  # no body yet
    todo.edit(id=rid, mode="replace", body="late body")
    todo.put(id=rid, text="c2")
    chunks = todo.store.chunks.list_chunks_for_ref(rid)
    ords = [c.ord for c in chunks]
    assert len(ords) == len(set(ords)) == 3
    assert {c.chunk_kind for c in chunks} == {"todo_body", "todo_comment"}


def test_todo_put_other_kwargs_still_refused(todo: TodoHandler) -> None:
    rid = _new(todo)
    for kw in ({"prio": 3}, {"tags": ["x:y"]}, {"parent_id": rid}, {"body": "b"}):
        with pytest.raises(BadInput, match="not supported"):
            todo.put(id=rid, text="hi", **kw)
    with pytest.raises(BadInput, match="not supported"):
        todo.put(id=rid)


@pytest.mark.parametrize("which", ["gripe", "todo"])
def test_hide_and_restore(which: str, gripe: GripeHandler, todo: TodoHandler) -> None:
    h = _pick(which, gripe, todo)
    rid = _new(h)
    for i in range(1, 5):
        h.put(id=rid, text=f"note-{i}")
    assert "hid comment 4" in h.delete(id=rid, args={"comment": 4}).body
    out = h.get(id=rid).body  # the ref itself is still live
    assert "note-4" not in out
    assert "1 hidden comment — view='comments' shows them" in out
    full = h.get(id=rid, view="comments").body
    assert "## comment 4" in full and "[hidden]" in full and "note-4" in full
    assert h.store.fetch_refs_by_ids({rid})[rid].meta["hidden_comments"] == [4]
    assert "already hidden" in h.delete(id=rid, args={"comment": 4}).body
    restore = {"comment": 4, "restore": True}
    assert "restored comment 4" in h.delete(id=rid, args=restore).body
    assert "note-4" in h.get(id=rid).body
    assert "was not hidden" in h.delete(id=rid, args=restore).body


@pytest.mark.parametrize("which", ["gripe", "todo"])
def test_hide_refusals(which: str, gripe: GripeHandler, todo: TodoHandler) -> None:
    h = _pick(which, gripe, todo)
    rid = _new(h)
    if which == "todo":
        todo.edit(id=rid, mode="replace", body="details")
    h.put(id=rid, text="only")
    body_ord = next(
        c.ord
        for c in h.store.chunks.list_chunks_for_ref(rid)
        if c.chunk_kind.endswith("_body")
    )
    with pytest.raises(BadInput, match="not a comment"):
        h.delete(id=rid, args={"comment": body_ord})
    with pytest.raises(BadInput, match="no comment 99"):
        h.delete(id=rid, args={"comment": 99})
    with pytest.raises(BadInput):
        h.delete(id=rid, args={"comment": "abc"})
    with pytest.raises(BadInput):
        h.delete(id=rid, args={"restore": True})
    assert "only" in h.get(id=rid).body  # no refusal retired the ref


def test_delete_without_args_still_soft_deletes(gripe: GripeHandler) -> None:
    rid = _new(gripe)
    assert "deleted" in gripe.delete(id=rid).body


def test_delete_args_keys_declared() -> None:
    assert GripeHandler.ARGS_KEYS["delete"] == frozenset({"comment", "restore"})
    assert TodoHandler.ARGS_KEYS["delete"] == frozenset({"comment", "restore"})


# ── todo details-body find-replace / insert (gr478556) ───────────────


def _todo_with_body(todo: TodoHandler, body: str = "alpha beta gamma beta") -> int:
    rid = _new(todo, "title")
    todo.edit(id=rid, mode="replace", body=body)
    return rid


def _body(todo: TodoHandler, rid: int) -> str:
    return next(
        c.text
        for c in todo.store.chunks.list_chunks_for_ref(rid)
        if c.chunk_kind == TODO_BODY_KIND
    )


def test_find_replace_edits_body_and_keeps_comments(todo: TodoHandler) -> None:
    rid = _todo_with_body(todo)
    todo.put(id=rid, text="keep me")
    r = todo.edit(id=rid, mode="find-replace", find="alpha", text="ALPHA")
    assert "edited details body" in r.body
    assert _body(todo, rid) == "ALPHA beta gamma beta"
    comments = [
        c.text
        for c in todo.store.chunks.list_chunks_for_ref(rid)
        if c.chunk_kind == "todo_comment"
    ]
    assert comments == ["keep me"]
    assert "body_replaced" in todo.get(id=rid, view="log").body


def test_find_replace_not_found_and_ambiguous(todo: TodoHandler) -> None:
    rid = _todo_with_body(todo)
    with pytest.raises(BadInput):
        todo.edit(id=rid, mode="find-replace", find="zzz", text="x")
    with pytest.raises(BadInput):
        todo.edit(id=rid, mode="find-replace", find="beta", text="x")
    todo.edit(id=rid, mode="find-replace", find="beta", text="B", match="all")
    assert _body(todo, rid) == "alpha B gamma B"


def test_insert_and_dry_run(todo: TodoHandler) -> None:
    rid = _todo_with_body(todo)
    dry = todo.edit(
        id=rid, mode="insert", find="alpha", text=" NEW", where="after", dry_run=True
    )
    assert "NEW" in dry.body
    assert _body(todo, rid) == "alpha beta gamma beta"  # nothing written
    todo.edit(id=rid, mode="insert", find="alpha", text=" NEW", where="after")
    assert _body(todo, rid) == "alpha NEW beta gamma beta"


def test_find_replace_requires_body_and_find(todo: TodoHandler) -> None:
    rid = _new(todo)
    with pytest.raises(BadInput, match="no details body"):
        todo.edit(id=rid, mode="find-replace", find="a", text="b")
    with pytest.raises(BadInput, match="requires find="):
        todo.edit(id=rid, mode="find-replace", text="b")
    rid2 = _todo_with_body(todo)
    with pytest.raises(BadInput, match="replace-only"):
        todo.edit(id=rid2, mode="find-replace", find="a", text="b", body="x")


def test_todo_spec_edit_modes() -> None:
    assert TodoHandler.spec.edit_modes == ("replace", "find-replace", "insert")
