"""Slice-1 todo-tree view tests: roots, strategic, tree, doable,
waiting, blocked, ask-user.

Each view is exercised through ``TodoHandler.search`` / ``TodoHandler.get``
so the test verifies both the renderer (``_todo_views``) and the
handler-side dispatch wiring.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.handlers.todo import TodoHandler
from precis.store import Store


@pytest.fixture
def handler(hub: Hub) -> TodoHandler:
    return TodoHandler(hub=hub)


def _id_of(body: str) -> int:
    from tests.conftest import id_of

    return id_of(body)


# ── view='roots' ──────────────────────────────────────────────────


def test_roots_empty_state(handler: TodoHandler) -> None:
    out = handler.search(view="roots")
    assert "no strategic todos yet" in out.body


def test_roots_lists_one_strategic(handler: TodoHandler) -> None:
    r = handler.put(text="Build the platform.", tags=["level:strategic"])
    rid = _id_of(r.body)
    out = handler.search(view="roots")
    assert f"td{rid}" in out.body
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
    assert f"td{root_id}" in out.body
    assert f"td{tac_id}" in out.body
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
    assert f"td{root_id}" in out.body
    assert f"td{child_id}" in out.body
    assert f"td{grand_id}" in out.body


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
    assert f"td{leaf_id}" in out.body


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


# ── view='ask-user' ───────────────────────────────────────────────


def test_ask_user_lists_open_asks(handler: TodoHandler) -> None:
    root = handler.put(text="Strategic.", tags=["level:strategic"])
    root_id = _id_of(root.body)
    handler.put(
        text="Cite Tanaka or skip?",
        parent_id=root_id,
        tags=["ask-user"],
    )
    out = handler.search(view="ask-user")
    assert "Cite Tanaka or skip?" in out.body


def test_ask_user_skips_done(handler: TodoHandler) -> None:
    root = handler.put(text="Strategic.", tags=["level:strategic"])
    root_id = _id_of(root.body)
    ask = handler.put(text="Resolved ask.", parent_id=root_id, tags=["ask-user"])
    ask_id = _id_of(ask.body)
    handler.tag(id=ask_id, add=["STATUS:done"])
    out = handler.search(view="ask-user")
    assert "Resolved ask." not in out.body


def test_asking_reto_alias_removed(handler: TodoHandler) -> None:
    """The deprecated ``view='asking-reto'`` alias was removed
    (2026-06-19) — it is now an unknown view, not a silent fall-through
    to ask-user. See docs/design/user-identity-and-ask-routing.md."""
    from precis.errors import Unsupported

    with pytest.raises(Unsupported, match="unknown view"):
        handler.search(view="asking-reto")


# ── unknown view rejection ────────────────────────────────────────


def test_unknown_view_rejected_with_options(handler: TodoHandler) -> None:
    from precis.errors import Unsupported

    with pytest.raises(Unsupported, match="unknown view"):
        handler.search(view="frobnicate")


def test_get_with_search_view_redirects_to_search(handler: TodoHandler) -> None:
    # gr48523: get(kind='todo', view='projects') is a search view on the
    # wrong verb — redirect explicitly instead of the generic "requires id=".
    from precis.errors import BadInput

    with pytest.raises(BadInput, match="search view") as ei:
        handler.get(view="projects")
    assert "search(kind='todo', view='projects')" in (ei.value.next or "")
    # doable too (another search-only view)
    with pytest.raises(BadInput, match="search view"):
        handler.get(view="doable")


# ── view='raw' (universal debug view) ─────────────────────────────


def test_raw_view_dumps_meta_and_columns(handler: TodoHandler) -> None:
    r = handler.put(
        text="Recurring watch.",
        tags=["level:recurring"],
        meta={"executor": "claude_inproc", "schedule": {"every": "1h"}},
    )
    rid = _id_of(r.body)
    out = handler.get(id=rid, view="raw")
    # Behavioural meta keys — invisible in the default render — appear here.
    assert "executor" in out.body
    assert "claude_inproc" in out.body
    # The schedule (canonicalised to cron at write time) is visible too.
    assert "schedule" in out.body
    assert "cron" in out.body
    # Scalar columns + title are present.
    assert f"id: {rid}" in out.body
    assert "kind: todo" in out.body
    assert "Recurring watch." in out.body


def test_raw_view_in_unknown_view_options(handler: TodoHandler) -> None:
    """The 'unknown view' error now advertises 'raw' (it is a base view),
    so an agent that guessed a bad view name has a working recovery path."""
    from precis.errors import Unsupported

    r = handler.put(text="A todo.")
    rid = _id_of(r.body)
    with pytest.raises(Unsupported) as exc:
        handler.get(id=rid, view="frobnicate")
    assert "raw" in (exc.value.options or [])


def test_doable_excludes_child_failed_parents(
    handler: TodoHandler, store: Store
) -> None:
    """Slice-5: a parent carrying child-failed:N shouldn't be doable.

    The parent's owner has to decide retry/switch/give-up before the
    leaf re-enters the doable rotation."""
    from precis.store.types import Tag
    from tests.conftest import id_of

    r = handler.put(text="parent of failed job")
    rid = id_of(r.body)
    # Pre-flight: the parent IS doable.
    out_before = handler.search(view="doable")
    assert f"td{rid}" in out_before.body
    # Bubble a child-failed tag on it.
    store.add_tag(rid, Tag.open("child-failed:999"), set_by="system")
    out = handler.search(view="doable")
    assert f"td{rid}" not in out.body


def test_attention_view_empty_when_nothing_pending(
    handler: TodoHandler, store: Store
) -> None:
    out = handler.search(view="attention")
    assert "no todos need attention" in out.body
    assert "view='doable'" in out.body


def test_attention_view_lists_ask_user_leaves(
    handler: TodoHandler, store: Store
) -> None:
    from precis.store.types import Tag
    from tests.conftest import id_of

    r = handler.put(text="Need the owner's call on Tanaka 2024")
    rid = id_of(r.body)
    store.add_tag(rid, Tag.open("ask-user"), set_by="agent")
    out = handler.search(view="attention")
    assert "Ask user (1)" in out.body
    assert f"td{rid}" in out.body
    assert "Need the owner" in out.body


def test_attention_view_lists_child_failed_parents(
    handler: TodoHandler, store: Store
) -> None:
    from precis.store.types import Tag
    from tests.conftest import id_of

    r = handler.put(text="Fix the rate-limit gripe")
    rid = id_of(r.body)
    store.add_tag(rid, Tag.open("child-failed:143"), set_by="system")
    # Add a job_event so the digest can quote a reason.
    from precis.store.types import BlockInsert

    job = store.insert_ref(
        kind="job", slug=None, title="fix attempt", meta={}, parent_id=rid
    )
    # Use the store's chunk-insert path so chunk_kind lands correctly.
    store.insert_blocks(
        job.id,
        [
            BlockInsert(
                pos=0, text="claude -p exited 2", meta={"chunk_kind": "job_event"}
            )
        ],
    )
    # The bubble tag references the synthetic job_id 143, not the
    # one we just inserted. Test focuses on the parent surface; the
    # reason lookup uses the tag's job_id which won't have an event
    # chunk — so the reason placeholder shows.
    _ = job
    out = handler.search(view="attention")
    assert "Child-failed parents (1)" in out.body
    assert f"td{rid}" in out.body
    assert "jo143" in out.body


def test_attention_view_unions_both_signals(handler: TodoHandler, store: Store) -> None:
    from precis.store.types import Tag
    from tests.conftest import id_of

    a = handler.put(text="Ask the owner")
    a_id = id_of(a.body)
    store.add_tag(a_id, Tag.open("ask-user"), set_by="agent")
    b = handler.put(text="Failed child")
    b_id = id_of(b.body)
    store.add_tag(b_id, Tag.open("child-failed:99"), set_by="system")

    out = handler.search(view="attention")
    assert "2 todos need attention" in out.body
    assert "Ask user (1)" in out.body
    assert "Child-failed parents (1)" in out.body


def test_doable_excludes_halted_leaves(handler: TodoHandler, store: Store) -> None:
    """Halt tag pulls a leaf out of the doable rotation.

    Owner adds ``halt`` → the worker / chatter never proposes the
    leaf again until the owner lifts the tag.
    """
    from precis.store.types import Tag
    from tests.conftest import id_of

    r = handler.put(text="paused work")
    rid = id_of(r.body)
    out_before = handler.search(view="doable")
    assert f"td{rid}" in out_before.body
    store.add_tag(rid, Tag.open("halt"), set_by="user")
    out = handler.search(view="doable")
    assert f"td{rid}" not in out.body


def test_attention_view_lists_halted_leaves(handler: TodoHandler, store: Store) -> None:
    """Halted leaves surface under the attention view.

    Since doable hides them, attention is where they have to land
    or they vanish from every chatter surface.
    """
    from precis.store.types import Tag
    from tests.conftest import id_of

    r = handler.put(text="Need a thought before resuming")
    rid = id_of(r.body)
    store.add_tag(rid, Tag.open("halt"), set_by="user")
    out = handler.search(view="attention")
    assert "Halted (1)" in out.body
    assert f"td{rid}" in out.body
    assert "Need a thought" in out.body


def test_halt_remove_is_owner_only(
    handler: TodoHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Workers can ADD halt (escalation) but only owner removes it."""
    from precis.errors import BadInput
    from precis.store.types import Tag
    from tests.conftest import id_of

    # Pre-tag from the owner side so the row already carries halt.
    r = handler.put(text="halted by owner")
    rid = id_of(r.body)
    store.add_tag(rid, Tag.open("halt"), set_by="user")

    # Worker source: removing halt is rejected.
    monkeypatch.setenv("PRECIS_SOURCE", "asa-worker")
    with pytest.raises(BadInput, match="halt"):
        handler.tag(id=rid, remove=["halt"])

    # Worker source: ADDING halt to a different ref is allowed —
    # that's the escalation path.
    r2 = handler.put(text="worker-escalated")
    r2id = id_of(r2.body)
    handler.tag(id=r2id, add=["halt"])

    # Owner can remove. Reset env to default (owner).
    monkeypatch.delenv("PRECIS_SOURCE", raising=False)
    handler.tag(id=rid, remove=["halt"])
    out = handler.search(view="doable")
    assert f"td{rid}" in out.body


def test_tree_view_includes_child_jobs(handler: TodoHandler) -> None:
    """Slice-5: kind='job' children show up under their todo parent
    in view='tree', distinguished by a gear marker."""
    from tests.conftest import id_of

    r = handler.put(text="Parent todo")
    rid = id_of(r.body)
    job = handler.store.insert_ref(
        kind="job", slug=None, title="fix_gripe attempt", meta={}, parent_id=rid
    )
    out = handler.get(id=rid, view="tree")
    assert f"jo{job.id}" in out.body
    assert "fix_gripe attempt" in out.body
    # Gear glyph distinguishes the job row from todo rows.
    assert "⚙" in out.body
