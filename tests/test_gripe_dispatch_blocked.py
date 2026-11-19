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
from tests.workers.test_gripe_dispatch import _gripe, _light, _roots_for


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
