"""Slice-1 todo-tree view tests: roots, strategic, tree, doable,
waiting, blocked, asking-reto.

Each view is exercised through ``TodoHandler.search`` / ``TodoHandler.get``
so the test verifies both the renderer (``_todo_views``) and the
handler-side dispatch wiring.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.handlers.todo import TodoHandler


@pytest.fixture
def handler(hub: Hub) -> TodoHandler:
    return TodoHandler(hub=hub)


def _id_of(body: str) -> int:
    return int(body.split("id=")[1].split()[0].rstrip(",.()"))


# ── view='roots' ──────────────────────────────────────────────────


def test_roots_empty_state(handler: TodoHandler) -> None:
    out = handler.search(view="roots")
    assert "no strategic todos yet" in out.body


def test_roots_lists_one_strategic(handler: TodoHandler) -> None:
    r = handler.put(text="Build the platform.", tags=["level:strategic"])
    rid = _id_of(r.body)
    out = handler.search(view="roots")
    assert f"#{rid}" in out.body
    assert "Build the platform." in out.body


def test_roots_shows_next_pick_marker(handler: TodoHandler) -> None:
    a = handler.put(text="A strategic.", tags=["level:strategic"])
    a_id = _id_of(a.body)
    # An open leaf under A → A is "active" → marked next pick.
    handler.put(text="A leaf to do.", parent_id=a_id)
    out = handler.search(view="roots")
    assert "← next pick" in out.body


# ── view='strategic' ──────────────────────────────────────────────


def test_strategic_lists_tacticals_under_root(handler: TodoHandler) -> None:
    root = handler.put(text="Strategic.", tags=["level:strategic"])
    root_id = _id_of(root.body)
    tac = handler.put(text="Tactical work.", parent_id=root_id, tags=["level:tactical"])
    tac_id = _id_of(tac.body)
    handler.put(text="leaf 1", parent_id=tac_id)
    handler.put(text="leaf 2", parent_id=tac_id)
    out = handler.search(view="strategic")
    assert f"#{root_id}" in out.body
    assert f"#{tac_id}" in out.body
    # 2 leaves, both open → 2/2 open
    assert "[2/2 open]" in out.body


# ── view='tree' ───────────────────────────────────────────────────


def test_tree_renders_descendants(handler: TodoHandler) -> None:
    root = handler.put(text="Root.", tags=["level:strategic"])
    root_id = _id_of(root.body)
    child = handler.put(text="Child A.", parent_id=root_id)
    child_id = _id_of(child.body)
    grand = handler.put(text="Grand A.", parent_id=child_id)
    grand_id = _id_of(grand.body)
    out = handler.get(id=root_id, view="tree")
    assert "Root." in out.body
    assert "Child A." in out.body
    assert "Grand A." in out.body
    assert f"#{root_id}" in out.body
    assert f"#{child_id}" in out.body
    assert f"#{grand_id}" in out.body


def test_tree_renders_status_icons(handler: TodoHandler) -> None:
    root = handler.put(text="Root.", tags=["level:strategic"])
    root_id = _id_of(root.body)
    done = handler.put(text="Done leaf.", parent_id=root_id)
    done_id = _id_of(done.body)
    handler.tag(id=done_id, add=["STATUS:done"])
    out = handler.get(id=root_id, view="tree")
    # ``✓`` is the done icon per the plan's tree render.
    assert "✓" in out.body


# ── view='doable' ─────────────────────────────────────────────────


def test_doable_lists_open_leaf(handler: TodoHandler) -> None:
    root = handler.put(text="Strategic.", tags=["level:strategic"])
    root_id = _id_of(root.body)
    leaf = handler.put(text="A doable thing.", parent_id=root_id)
    leaf_id = _id_of(leaf.body)
    out = handler.search(view="doable")
    assert "A doable thing." in out.body
    assert f"#{leaf_id}" in out.body


def test_doable_skips_done_leaves(handler: TodoHandler) -> None:
    root = handler.put(text="Strategic.", tags=["level:strategic"])
    root_id = _id_of(root.body)
    leaf = handler.put(text="Already done.", parent_id=root_id)
    leaf_id = _id_of(leaf.body)
    handler.tag(id=leaf_id, add=["STATUS:done"])
    out = handler.search(view="doable")
    assert "Already done." not in out.body


def test_doable_skips_waiting_leaves(handler: TodoHandler) -> None:
    root = handler.put(text="Strategic.", tags=["level:strategic"])
    root_id = _id_of(root.body)
    leaf = handler.put(
        text="Waiting on reviewer.",
        parent_id=root_id,
        tags=["waiting-for:reviewer"],
    )
    out = handler.search(view="doable")
    assert "Waiting on reviewer." not in out.body
    _ = leaf  # silence unused


def test_doable_skips_paused_subtree(handler: TodoHandler) -> None:
    root = handler.put(text="Strategic.", tags=["level:strategic"])
    root_id = _id_of(root.body)
    branch = handler.put(text="A paused branch.", parent_id=root_id)
    branch_id = _id_of(branch.body)
    handler.tag(id=branch_id, add=["STATUS:paused"])
    leaf = handler.put(text="Under a pause.", parent_id=branch_id)
    out = handler.search(view="doable")
    assert "Under a pause." not in out.body
    _ = leaf


def test_doable_skips_blocked_by_open_target(handler: TodoHandler) -> None:
    root = handler.put(text="Strategic.", tags=["level:strategic"])
    root_id = _id_of(root.body)
    blocker = handler.put(text="Blocker leaf.", parent_id=root_id)
    blocker_id = _id_of(blocker.body)
    leaf = handler.put(text="Downstream leaf.", parent_id=root_id)
    leaf_id = _id_of(leaf.body)
    handler.link(id=leaf_id, target=f"todo:{blocker_id}", rel="blocked-by")
    out = handler.search(view="doable")
    assert "Downstream leaf." not in out.body
    # Marking the blocker done unblocks the downstream leaf.
    handler.tag(id=blocker_id, add=["STATUS:done"])
    out2 = handler.search(view="doable")
    assert "Downstream leaf." in out2.body


def test_doable_under_subtree_filter(handler: TodoHandler) -> None:
    root_a = handler.put(text="Strategic A.", tags=["level:strategic"])
    root_a_id = _id_of(root_a.body)
    root_b = handler.put(text="Strategic B.", tags=["level:strategic"])
    root_b_id = _id_of(root_b.body)
    leaf_a = handler.put(text="A leaf.", parent_id=root_a_id)
    leaf_b = handler.put(text="B leaf.", parent_id=root_b_id)
    _ = leaf_a, leaf_b
    out = handler.search(view="doable", args={"under": root_a_id})
    assert "A leaf." in out.body
    assert "B leaf." not in out.body


# ── view='waiting' ────────────────────────────────────────────────


def test_waiting_shows_tagged_leaves(handler: TodoHandler) -> None:
    root = handler.put(text="Strategic.", tags=["level:strategic"])
    root_id = _id_of(root.body)
    handler.put(
        text="Wait on reviewer feedback.",
        parent_id=root_id,
        tags=["waiting-for:reviewer-tanaka"],
    )
    out = handler.search(view="waiting")
    assert "Wait on reviewer feedback." in out.body
    assert "waiting-for:reviewer-tanaka" in out.body


# ── view='blocked' ────────────────────────────────────────────────


def test_blocked_lists_only_actively_blocked(handler: TodoHandler) -> None:
    root = handler.put(text="Strategic.", tags=["level:strategic"])
    root_id = _id_of(root.body)
    blocker = handler.put(text="Blocker.", parent_id=root_id)
    blocker_id = _id_of(blocker.body)
    leaf = handler.put(text="Blocked leaf.", parent_id=root_id)
    leaf_id = _id_of(leaf.body)
    handler.link(id=leaf_id, target=f"todo:{blocker_id}", rel="blocked-by")
    out = handler.search(view="blocked")
    assert "Blocked leaf." in out.body
    # Resolving the blocker should drop the leaf from the view.
    handler.tag(id=blocker_id, add=["STATUS:done"])
    out2 = handler.search(view="blocked")
    assert "Blocked leaf." not in out2.body


# ── view='asking-reto' ────────────────────────────────────────────


def test_asking_reto_lists_open_asks(handler: TodoHandler) -> None:
    root = handler.put(text="Strategic.", tags=["level:strategic"])
    root_id = _id_of(root.body)
    handler.put(
        text="Cite Tanaka or skip?",
        parent_id=root_id,
        tags=["asking-reto"],
    )
    out = handler.search(view="asking-reto")
    assert "Cite Tanaka or skip?" in out.body


def test_asking_reto_skips_done(handler: TodoHandler) -> None:
    root = handler.put(text="Strategic.", tags=["level:strategic"])
    root_id = _id_of(root.body)
    ask = handler.put(text="Resolved ask.", parent_id=root_id, tags=["asking-reto"])
    ask_id = _id_of(ask.body)
    handler.tag(id=ask_id, add=["STATUS:done"])
    out = handler.search(view="asking-reto")
    assert "Resolved ask." not in out.body


# ── unknown view rejection ────────────────────────────────────────


def test_unknown_view_rejected_with_options(handler: TodoHandler) -> None:
    from precis.errors import Unsupported

    with pytest.raises(Unsupported, match="unknown view"):
        handler.search(view="frobnicate")
