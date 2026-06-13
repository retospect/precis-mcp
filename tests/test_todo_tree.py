"""Slice-1 todo-tree tests: guards, parent_id wiring, ancestry walk.

Companion to :mod:`test_todo` (the flat surface). Tests here exercise
the additions in ``docs/design/todo-tree-plan.md`` Slice 1:

* ``parent_id`` kwarg on ``put``
* cycle / depth / parent-kind guards
* level-gradient guard (``PRECIS_SOURCE`` source routing)
* walk-on-read ancestry on ``get(id=N)``
* ``status:done`` event emission on ``tag(add=['STATUS:done'])``
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.todo import TodoHandler


@pytest.fixture
def handler(hub: Hub) -> TodoHandler:
    return TodoHandler(hub=hub)


def _id_of(resp_body: str) -> int:
    """Parse ``created todo id=N ...`` out of a put-create ack."""
    return int(resp_body.split("id=")[1].split()[0].rstrip(",.()"))


# ── parent_id wiring ───────────────────────────────────────────────


def test_put_with_parent_id_links_into_tree(handler: TodoHandler) -> None:
    root = handler.put(text="Build the platform.")
    root_id = _id_of(root.body)
    child = handler.put(text="Write the first paper.", parent_id=root_id)
    child_id = _id_of(child.body)
    ref = handler.store.get_ref(kind="todo", id=child_id)
    assert ref is not None and ref.parent_id == root_id


def test_put_under_missing_parent_raises_not_found(handler: TodoHandler) -> None:
    with pytest.raises(NotFound, match="parent todo id="):
        handler.put(text="orphan", parent_id=99999)


def test_put_under_non_todo_parent_rejects(handler: TodoHandler) -> None:
    # The test fixture only has the todo kind handler; use the
    # store to mint a non-todo ref so the parent-kind guard fires.
    other = handler.store.insert_ref(
        kind="memory", slug=None, title="just a memory", meta={}
    )
    with pytest.raises(BadInput, match="not a todo"):
        handler.put(text="bad", parent_id=other.id)


def test_self_parent_is_rejected_via_cycle_guard(handler: TodoHandler) -> None:
    # Direct self-reference is the trivial cycle. We can't create
    # a ref pointing at itself in one put, but we can simulate the
    # re-parent path via the underlying guard.
    from precis.handlers import _todo_guards as g

    a = handler.put(text="A")
    a_id = _id_of(a.body)
    with pytest.raises(BadInput, match="cannot be its own parent"):
        g.check_no_cycle(handler.store, child_id=a_id, parent_id=a_id)


def test_cycle_via_ancestor_rejects(handler: TodoHandler) -> None:
    from precis.handlers import _todo_guards as g

    a = handler.put(text="A")
    a_id = _id_of(a.body)
    b = handler.put(text="B", parent_id=a_id)
    b_id = _id_of(b.body)
    # Trying to make A a child of B would create A→B→A.
    with pytest.raises(BadInput, match="cycle"):
        g.check_no_cycle(handler.store, child_id=a_id, parent_id=b_id)


# ── depth wall ─────────────────────────────────────────────────────


def test_depth_wall_rejects_eleventh_level(handler: TodoHandler) -> None:
    """Build a 10-deep chain, then try to add an 11th — must reject."""
    parent_id: int | None = None
    last_id = 0
    for i in range(10):
        resp = handler.put(text=f"level {i}", parent_id=parent_id)
        last_id = _id_of(resp.body)
        parent_id = last_id
    with pytest.raises(BadInput, match="depth limit hit"):
        handler.put(text="too deep", parent_id=last_id)


# ── level-gradient guard ──────────────────────────────────────────


def test_owner_can_create_strategic(handler: TodoHandler) -> None:
    # Default test env has no PRECIS_SOURCE → owner.
    r = handler.put(text="Strategic intent.", tags=["level:strategic"])
    rid = _id_of(r.body)
    tags = {str(t) for t in handler.store.tags_for(rid)}
    assert "level:strategic" in tags


def test_worker_cannot_create_strategic(
    handler: TodoHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_SOURCE", "asa-worker")
    with pytest.raises(BadInput, match="owner-only"):
        handler.put(text="bad strategic", tags=["level:strategic"])


def test_worker_can_propose_tactical(
    handler: TodoHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_SOURCE", "asa-dreamer")
    r = handler.put(text="proposed", tags=["level:proposed-tactical"])
    rid = _id_of(r.body)
    tags = {str(t) for t in handler.store.tags_for(rid)}
    assert "level:proposed-tactical" in tags


def test_worker_cannot_add_strategic_via_tag(
    handler: TodoHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = handler.put(text="a leaf")
    rid = _id_of(r.body)
    monkeypatch.setenv("PRECIS_SOURCE", "asa-chatter")
    with pytest.raises(BadInput, match="owner-only"):
        handler.tag(id=rid, add=["level:strategic"])


def test_worker_cannot_remove_tactical_tag(
    handler: TodoHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = handler.put(text="tactical", tags=["level:tactical"])
    rid = _id_of(r.body)
    monkeypatch.setenv("PRECIS_SOURCE", "asa-worker")
    with pytest.raises(BadInput, match="owner-only"):
        handler.tag(id=rid, remove=["level:tactical"])


def test_web_source_treated_as_owner(
    handler: TodoHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_SOURCE", "web:reto")
    r = handler.put(text="strategic from web", tags=["level:strategic"])
    rid = _id_of(r.body)
    tags = {str(t) for t in handler.store.tags_for(rid)}
    assert "level:strategic" in tags


def test_worker_cannot_delete_strategic(
    handler: TodoHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = handler.put(text="strategic root", tags=["level:strategic"])
    rid = _id_of(r.body)
    monkeypatch.setenv("PRECIS_SOURCE", "asa-worker")
    with pytest.raises(BadInput, match="owner-only"):
        handler.delete(id=rid)


def test_unknown_source_defaults_to_owner(
    handler: TodoHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typo in PRECIS_SOURCE must not silently demote to worker."""
    monkeypatch.setenv("PRECIS_SOURCE", "asasworker")  # missing dash
    r = handler.put(text="ok", tags=["level:strategic"])
    rid = _id_of(r.body)
    tags = {str(t) for t in handler.store.tags_for(rid)}
    assert "level:strategic" in tags


# ── ancestry walk-on-read ─────────────────────────────────────────


def test_get_root_has_no_ancestry_section(handler: TodoHandler) -> None:
    r = handler.put(text="root")
    rid = _id_of(r.body)
    out = handler.get(id=rid)
    assert "Ancestry:" not in out.body


def test_get_descendant_includes_ancestry(handler: TodoHandler) -> None:
    root = handler.put(text="Vision: long horizon plan.")
    root_id = _id_of(root.body)
    mid = handler.put(text="Tactical: focused workstream.", parent_id=root_id)
    mid_id = _id_of(mid.body)
    leaf = handler.put(text="Draft setup paragraph.", parent_id=mid_id)
    leaf_id = _id_of(leaf.body)
    out = handler.get(id=leaf_id)
    assert "Ancestry:" in out.body
    assert f"#{root_id}" in out.body
    assert f"#{mid_id}" in out.body
    assert f"#{leaf_id}" in out.body


# ── status:done event emission ────────────────────────────────────


def test_marking_done_writes_ref_event(handler: TodoHandler) -> None:
    r = handler.put(text="something to do")
    rid = _id_of(r.body)
    before = handler.store.events_for(rid)
    assert not [e for e in before if e.event == "status:done"]
    handler.tag(id=rid, add=["STATUS:done"])
    after = handler.store.events_for(rid)
    done_events = [e for e in after if e.event == "status:done"]
    assert len(done_events) == 1
    assert done_events[0].source in ("cli", "user")  # default owner


def test_done_event_carries_caller_source(
    handler: TodoHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_SOURCE", "asa-worker")
    r = handler.put(text="leaf")
    rid = _id_of(r.body)
    handler.tag(id=rid, add=["STATUS:done"])
    events = [e for e in handler.store.events_for(rid) if e.event == "status:done"]
    assert events and events[0].source == "asa-worker"


# ── put-create ack mentions parent ────────────────────────────────


def test_put_ack_mentions_parent(handler: TodoHandler) -> None:
    root = handler.put(text="root")
    rid = _id_of(root.body)
    child = handler.put(text="child", parent_id=rid)
    assert f"under #{rid}" in child.body


def test_put_without_parent_has_no_under_phrase(handler: TodoHandler) -> None:
    r = handler.put(text="bare")
    assert "under #" not in r.body
