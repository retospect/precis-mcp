"""gr656970: blocks/blocked-by holds back gripe_dispatch (slice 3 of the
gripe, intake side): a gripe carrying an open ``blocked-by`` target of
any kind — a todo or another gripe — is not a dispatch candidate until
the blocker reaches a blocker-terminal STATUS (done / won't-do / wontfix
…, :data:`precis.todo.BLOCKER_TERMINAL_STATUSES`)."""

from __future__ import annotations

from typing import Any

import pytest

from precis.plans.lanes import ANY_CLASS, Lane
from precis.store import Store
from precis.store.types import Tag
from precis.workers import gripe_dispatch as gd
from tests._lane_rows import use_lanes
from tests._refs import insert_gripe
from tests.workers.test_gripe_dispatch import (
    _age_node,
    _chain_for,
    _gripe,
    _light,
    _one_lane,
    _park,
    _queue_build,
    _roots_for,
    _todo_chain_for,
    _todo_source,
)


@pytest.fixture
def two_lanes(monkeypatch: pytest.MonkeyPatch) -> tuple[Lane, Lane]:
    codex = Lane(
        "codex", "codex_exec", "codex", 1, frozenset({"code"}), vendor="openai"
    )
    sub = Lane(
        "claude-sub",
        "claude_p",
        "sub",
        2,
        frozenset({ANY_CLASS}),
        vendor="anthropic",
        models=("claude-sonnet-5-5",),
        model_prefix="claude-",
    )
    use_lanes(monkeypatch, codex, sub)
    return codex, sub


def _close(store: Store, ref_id: int, status: str) -> None:
    store.add_tag(
        ref_id, Tag.closed("STATUS", status), set_by="system", replace_prefix=True
    )


def test_gripe_blocked_by_todo_skipped_until_done(store: Store, two_lanes: Any) -> None:
    blocked = _gripe(store, "blocked by a todo")
    free = _gripe(store, "free")
    blocker = store.insert_ref(kind="todo", slug=None, title="The todo blocker")
    store.add_link(src_ref_id=blocked, dst_ref_id=blocker.id, relation="blocked-by")
    assert [c.gripe_id for c in gd.candidates(store, 10)] == [free]
    _close(store, blocker.id, "done")
    assert {c.gripe_id for c in gd.candidates(store, 10)} == {free, blocked}


def test_gripe_blocked_by_gripe_skipped_until_closed(
    store: Store, two_lanes: Any
) -> None:
    blocked = _gripe(store, "blocked by a gripe")
    free = _gripe(store, "free")
    blocker = insert_gripe(store, "The gripe blocker")
    store.add_link(src_ref_id=blocked, dst_ref_id=blocker.id, relation="blocked-by")
    assert [c.gripe_id for c in gd.candidates(store, 10)] == [free]
    _close(store, blocker.id, "wontfix")
    assert {c.gripe_id for c in gd.candidates(store, 10)} == {free, blocked}


def test_blocked_after_pick_mints_nothing_until_blocker_closes(
    store: Store, two_lanes: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr656970 review: ``candidates`` judged the gripe at pick time —
    a blocker linked between the pick and the mint still holds the
    chain; once it closes the gripe mints on the next pass."""
    _light(store)
    gid = _gripe(store, "blocked mid-pass")
    blocker = store.insert_ref(kind="todo", slug=None, title="The blocker")
    armed = {"v": True}
    real = gd._candidate_picks

    def block_after_pick(store_: Store, limit: int, **kw: Any) -> list[gd.DispatchPick]:
        picks = real(store_, limit, **kw)
        if armed["v"] and any(p.gripe_id == gid for p in picks):
            armed["v"] = False
            store_.add_link(
                src_ref_id=gid, dst_ref_id=blocker.id, relation="blocked-by"
            )
        return picks

    monkeypatch.setattr(gd, "_candidate_picks", block_after_pick)
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert (res.claimed, res.ok, res.failed) == (1, 0, 0)
    assert res.extra["held"] == [gid]
    assert _roots_for(store, gid) == []
    _close(store, blocker.id, "done")
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert res.ok == 1
    assert _roots_for(store, gid)


def _holds(store: Store, build: int, *, blockers_only: bool = False) -> str | None:
    from precis.workers.executors._common import source_todo_holds

    with store.pool.connection() as conn:
        return source_todo_holds(conn, [build], blockers_only=blockers_only).get(build)


def test_source_todo_hold_reasons(store: Store) -> None:
    """A todo chain's build is held by an open blocker, a hold tag,
    ``needs-spec``, a withdrawn ``factory:build`` tag or a terminal source;
    ``source_holds`` carries the same reasons."""
    from precis.workers.executors._common import source_holds

    source = _todo_source(store)
    _root, build = _todo_chain_for(store, source)
    assert _holds(store, build) is None

    blocker = store.insert_ref(kind="todo", slug=None, title="The blocker")
    store.add_tag(blocker.id, Tag.closed("STATUS", "open"), set_by="agent")
    store.add_link(src_ref_id=source, dst_ref_id=blocker.id, relation="blocked-by")
    reason = _holds(store, build)
    assert reason is not None and f"td{source} is blocked-by" in reason
    assert f"td{blocker.id}" in reason
    with store.pool.connection() as conn:
        assert source_holds(conn, [build])[build] == reason
    _close(store, blocker.id, "done")
    assert _holds(store, build) is None

    for tag in (*gd.HOLD_TAGS, "needs-spec"):
        store.add_tag(source, Tag.open(tag), set_by="agent")
        reason = _holds(store, build)
        assert reason is not None and f"hold tag {tag}" in reason
        store.remove_tag(source, Tag.open(tag))
        assert _holds(store, build) is None

    store.remove_tag(source, Tag.open("factory:build"))
    reason = _holds(store, build)
    assert reason is not None and "no longer carries factory:build" in reason
    store.add_tag(source, Tag.open("factory:build"), set_by="agent")

    for status in ("done", "won't-do"):
        _close(store, source, status)
        reason = _holds(store, build)
        assert reason is not None and f"td{source} is {status}" in reason
    _close(store, source, "open")
    assert _holds(store, build) is None


def test_skip_tag_holds_a_todo_chain_like_needs_spec(store: Store) -> None:
    source = _todo_source(store)
    _root, build = _todo_chain_for(store, source)
    assert _holds(store, build) is None
    store.add_tag(source, Tag.open("factory:skip"), set_by="agent")
    reason = _holds(store, build)
    assert reason is not None and "hold tag factory:skip" in reason
    # a hold tag is no blocker: the stalled-chain census does not shield it
    assert _holds(store, build, blockers_only=True) is None
    store.remove_tag(source, Tag.open("factory:skip"))
    assert _holds(store, build) is None


def test_source_todo_holds_leave_gripe_chains_alone(store: Store) -> None:
    from precis.workers.executors._common import source_holds

    g = _gripe(store, "a gripe")
    _root, build = _chain_for(store, g)
    assert _holds(store, build) is None
    blocker = store.insert_ref(kind="todo", slug=None, title="The blocker")
    store.add_link(src_ref_id=g, dst_ref_id=blocker.id, relation="blocked-by")
    with store.pool.connection() as conn:
        assert "source gripe" in source_holds(conn, [build])[build]
    assert _holds(store, build) is None


def test_stale_source_todo_meta_holds_no_build(store: Store) -> None:
    """``source_todo_holds`` validates the plan root and its related-to
    edge, like the chain inventory: a mismatched or misplaced
    ``source_todo`` meta holds no build."""
    source = _todo_source(store)
    other = _todo_source(store, "another todo")
    root, build = _todo_chain_for(store, source)
    # the key names another todo than the one the root is related to
    store.stamp_ref_meta(root, {"source_todo": other})
    store.remove_tag(other, Tag.open("factory:build"))
    assert root not in {c.root_id for c in gd.chains(store)}
    assert _holds(store, build) is None
    # the related-to edge is gone but the meta remains
    store.stamp_ref_meta(root, {"source_todo": source})
    store.remove_link(src_ref_id=root, dst_ref_id=source, relation="related-to")
    assert _holds(store, build) is None
    # the key sits on the build node, which is no plan root
    held_source = _todo_source(store, "a held todo")
    store.remove_tag(held_source, Tag.open("factory:build"))
    store.stamp_ref_meta(build, {"source_todo": held_source})
    assert _holds(store, build) is None


def _apply_hold_reason(store: Store, source: int, reason: str) -> None:
    if reason == "withdrawn":
        store.remove_tag(source, Tag.open("factory:build"))
    elif reason == "needs-spec":
        store.add_tag(source, Tag.open("needs-spec"), set_by="agent")
    elif reason == "hold-tag":
        store.add_tag(source, Tag.open(sorted(gd.HOLD_TAGS)[0]), set_by="agent")
    else:
        _close(store, source, "done")


@pytest.mark.parametrize("state", ["open", "queued"])
@pytest.mark.parametrize("reason", ["withdrawn", "needs-spec", "hold-tag", "closed"])
def test_source_hold_does_not_shield_stall(
    store: Store, monkeypatch: pytest.MonkeyPatch, state: str, reason: str
) -> None:
    """Only an open blocker reads a held todo chain as moving: with the
    source held for anything else, an open jobless or queued build is not
    moving either — the chain still reaches the stalled sweep."""
    _one_lane(monkeypatch, cap=2)
    _light(store)
    source = _todo_source(store)
    root, build = _todo_chain_for(store, source)
    if state == "queued":
        _queue_build(store, build)
    _apply_hold_reason(store, source, reason)
    assert _holds(store, build) is not None
    assert _holds(store, build, blockers_only=True) is None
    _age_node(store, build, 8)
    [chain] = [c for c in gd.chains(store) if c.root_id == root]
    assert not chain.live and not chain.plan_live
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert [z["root"] for z in res.extra["stalled"]] == [root]


@pytest.mark.parametrize("state", ["open", "queued"])
def test_blocker_shields_moving_todo_build_from_stall(
    store: Store, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    """An open blocker also shields an open jobless or queued todo build:
    the chain reads moving until the blocker closes."""
    _one_lane(monkeypatch, cap=2)
    _light(store)
    source = _todo_source(store)
    root, build = _todo_chain_for(store, source)
    if state == "queued":
        _queue_build(store, build)
    blocker = store.insert_ref(kind="todo", slug=None, title="The blocker")
    store.add_tag(blocker.id, Tag.closed("STATUS", "open"), set_by="agent")
    store.add_link(src_ref_id=source, dst_ref_id=blocker.id, relation="blocked-by")
    _age_node(store, build, 8)
    [chain] = [c for c in gd.chains(store) if c.root_id == root]
    assert not chain.live and chain.plan_live
    assert gd.run_gripe_dispatch_pass(store, host="h1").extra["stalled"] == []


def test_blocker_shields_todo_chain_from_stall(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _one_lane(monkeypatch, cap=2)
    _light(store)
    source = _todo_source(store)
    root, build = _todo_chain_for(store, source)
    _park(store, build, "paused")
    blocker = store.insert_ref(kind="todo", slug=None, title="The blocker")
    store.add_tag(blocker.id, Tag.closed("STATUS", "open"), set_by="agent")
    store.add_link(src_ref_id=source, dst_ref_id=blocker.id, relation="blocked-by")
    _age_node(store, build, 8)
    [chain] = [c for c in gd.chains(store) if c.root_id == root]
    assert not chain.live and chain.plan_live
    assert gd.run_gripe_dispatch_pass(store, host="h1").extra["stalled"] == []
    # the blocker closes: the chain, still old, is asked about
    _close(store, blocker.id, "done")
    _age_node(store, build, 8)
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert [z["root"] for z in res.extra["stalled"]] == [root]
