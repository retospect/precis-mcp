"""Dark-factory step 1, slice 2 (td540842): open gripes hang under the todo
root carrying their ``thread:`` tag in ``search(kind='todo', view='work')``,
each with its loop stage; the rest land in one "unplaced" bucket."""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.gripe import GripeHandler
from precis.handlers.todo import TodoHandler
from precis.store import Store
from precis.todo import work_gripes
from tests._refs import insert_gripe
from tests.conftest import id_of


@pytest.fixture
def handler(hub: Hub) -> TodoHandler:
    return TodoHandler(hub=hub)


@pytest.fixture
def gripes(hub: Hub) -> GripeHandler:
    return GripeHandler(hub=hub)


def _tree(handler: TodoHandler, store: Store) -> dict[str, Any]:
    quest = store.insert_ref(kind="quest", slug=None, title="Precis runs its own work")
    root_id = id_of(
        handler.put(text="Dark factory root.", meta={"rotation_root": True}).body
    )
    store.add_link(src_ref_id=root_id, dst_ref_id=quest.id, relation="serves")
    handler.tag(id=root_id, add=["thread:factory"])
    other = id_of(handler.put(text="Other root.", meta={"rotation_root": True}).body)
    store.add_link(src_ref_id=other, dst_ref_id=quest.id, relation="serves")
    handler.tag(id=other, add=["thread:chemistry"])
    return {"quest": quest.id, "root": root_id, "other": other}


def _gripe(gripes: GripeHandler, title: str, *tags: str) -> int:
    gid = int(insert_gripe(gripes.store, title).id)
    for tag in tags:
        if tag.startswith("STATUS:"):
            # Closing needs a 'resolved:' comment first (the handler gate).
            gripes.put(id=gid, text="resolved: abc1234 (r1) — test", tags=[tag])
        else:
            gripes.tag(id=gid, add=[tag])
    return gid


def _line(out: str, ref: str) -> str:
    return next(ln for ln in out.splitlines() if ref in ln)


def test_threaded_gripe_hangs_under_its_root_with_stage(
    handler: TodoHandler, gripes: GripeHandler, hub: Hub
) -> None:
    t = _tree(handler, hub.live_store)
    g = _gripe(gripes, "Factory: the loop stalls", "thread:factory")
    out = handler.search(view="work").body

    lines = out.splitlines()
    root_i = next(i for i, ln in enumerate(lines) if f"td{t['root']}" in ln)
    other_i = next(i for i, ln in enumerate(lines) if f"td{t['other']}" in ln)
    g_i = next(i for i, ln in enumerate(lines) if f"gr{g}" in ln)
    assert root_i < g_i
    # indented under the root, before the next root begins
    row = lines[g_i]
    assert len(row) - len(row.lstrip()) > len(lines[root_i]) - len(
        lines[root_i].lstrip()
    )
    assert (g_i < other_i) or (other_i < root_i)
    # not in the loop yet -> unbriefed, no lane; row grammar
    assert row.strip() == f"gr{g} unbriefed — Factory: the loop stalls"
    assert "unplaced gripes" not in out


def test_stage_and_lane_follow_the_fix_loop(
    handler: TodoHandler, gripes: GripeHandler, hub: Hub
) -> None:
    _tree(handler, hub.live_store)
    queued = _gripe(gripes, "Factory: queued one", "thread:factory")
    gripes.edit(id=queued, mode="fix", batch="b1", brief="Task: x\nlane: devin")
    building = _gripe(gripes, "Factory: building", "thread:factory")
    gripes.edit(id=building, mode="fix", batch="b1", brief="Task: y")
    gripes.edit(id=building, mode="fix", host="mac-a", session="claude:win-1")
    out = handler.search(view="work").body

    # the lane comes from the brief when it names one
    assert _line(out, f"gr{queued}").strip() == (
        f"gr{queued} queued [devin] — Factory: queued one"
    )
    # else from the loop session's vendor
    assert _line(out, f"gr{building}").strip() == (
        f"gr{building} in-build [claude] — Factory: building"
    )


def test_terminal_gripes_are_not_listed(
    handler: TodoHandler, gripes: GripeHandler, hub: Hub
) -> None:
    _tree(handler, hub.live_store)
    done = _gripe(gripes, "Factory: finished", "thread:factory", "STATUS:done")
    wont = _gripe(gripes, "Elsewhere: dropped", "STATUS:wontfix")
    out = handler.search(view="work").body
    assert f"gr{done}" not in out
    assert f"gr{wont}" not in out
    assert "unplaced gripes" not in out


def test_unplaced_bucket_counts_open_minus_placed_and_shows_oldest_five(
    handler: TodoHandler, gripes: GripeHandler, hub: Hub
) -> None:
    _tree(handler, hub.live_store)
    placed = _gripe(gripes, "Factory: placed", "thread:factory")
    placed2 = _gripe(gripes, "Other: placed", "thread:chemistry")
    # a thread no root carries is unplaced too
    orphan = _gripe(gripes, "Ghost: nobody owns it", "thread:local-compute")
    plain = [_gripe(gripes, f"Plain {i}") for i in range(6)]
    closed = _gripe(gripes, "Plain closed", "STATUS:done")
    out = handler.search(view="work").body

    open_total = 3 + 6
    placed_n = 2
    unplaced_n = open_total - placed_n
    assert f"unplaced gripes ({unplaced_n})" in out
    bucket = out.split("unplaced gripes")[1].split("\nhosts")[0]
    shown = [ln for ln in bucket.splitlines() if ln.strip().startswith("gr")]
    assert len(shown) == work_gripes.UNPLACED_SHOWN
    # oldest first: the orphan (created before the plain ones), then plain 0..3
    assert f"gr{orphan}" in shown[0]
    for gid in plain[:4]:
        assert any(f"gr{gid}" in ln for ln in shown)
    assert f"gr{plain[4]}" not in bucket
    assert "… +2 newer" in bucket
    for gid in (placed, placed2, closed):
        assert f"gr{gid}" not in bucket


def test_gripes_by_thread_groups_open_gripes(
    handler: TodoHandler, gripes: GripeHandler, hub: Hub
) -> None:
    store = hub.live_store
    _tree(handler, store)
    a = _gripe(gripes, "A", "thread:factory")
    b = _gripe(gripes, "B", "thread:factory", "thread:chemistry")
    _gripe(gripes, "C")
    _gripe(gripes, "D", "thread:factory", "STATUS:wontfix")
    by = work_gripes.gripes_by_thread(store)
    assert sorted(by) == ["chemistry", "factory"]
    assert [r.ref_id for r in by["factory"]] == [a, b]
    assert [r.ref_id for r in by["chemistry"]] == [b]
    assert by["factory"][0].stage == "unbriefed"
    assert by["factory"][0].lane is None
