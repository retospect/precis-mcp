"""Dark-factory step 1 (td540840): ``search(kind='todo', view='work')`` and
the todo tree ring on the eye (gr541348)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from precis.dispatch import Hub, boot
from precis.handlers.todo import _TREE_SEARCH_VIEWS, TodoHandler, TodoView
from precis.store import Store
from precis.todo import work_view
from tests.conftest import id_of


@pytest.fixture
def handler(hub: Hub) -> TodoHandler:
    return TodoHandler(hub=hub)


def _tree(handler: TodoHandler, store: Store) -> dict[str, Any]:
    quest = store.insert_ref(kind="quest", slug=None, title="Precis runs its own work")
    root_id = id_of(
        handler.put(text="Dark factory root.", meta={"rotation_root": True}).body
    )
    store.add_link(src_ref_id=root_id, dst_ref_id=quest.id, relation="serves")
    a = id_of(
        handler.put(
            text="Work view slice.", parent_id=root_id, meta={"llm_tier": None}
        ).body
    )
    b = id_of(
        handler.put(
            text="Gripes in the tree.", parent_id=root_id, meta={"llm_tier": None}
        ).body
    )
    handler.tag(id=a, add=["claimed-by:WORK_MANAGEMENT", "STATUS:doing"])
    lone = id_of(
        handler.put(text="Unassigned strategic.", meta={"rotation_root": True}).body
    )
    return {"quest": quest.id, "root": root_id, "a": a, "b": b, "lone": lone}


def _agent(tree: str, purpose: str, state: str = "working") -> dict[str, Any]:
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "vendor": "claude",
        "host": "melchior",
        "project": "precis-mcp",
        "tree": tree,
        "branch": tree,
        "dirty": 0,
        "ahead": 0,
        "behind": 0,
        "purpose": purpose,
        "state": state,
        "quiet_min": 1.0,
        "last_active": now,
        "ctx_pct": 20.0,
        "tmux": {"pane_id": "%1", "session": "0", "window": "1", "window_name": tree},
        "attach": "tmux attach -t 0:1",
    }


def _push_fleet(store: Store, rows: list[dict[str, Any]]) -> None:
    """Seed fleet rows the only way the kind admits them: a host report."""
    fleet_handler = boot(store=store).handlers["fleet"]
    report = {
        "host": "melchior",
        "version": 1,
        "generated": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rows": rows,
        "exceptions": [],
        "quota": {},
    }
    fleet_handler.put(mode="report", host="melchior", report=report)


def test_work_view_is_wired_in_the_vocabulary() -> None:
    assert TodoView("work") is TodoView.WORK
    assert TodoView.WORK in _TREE_SEARCH_VIEWS


def test_work_view_renders_quest_root_and_slices(
    handler: TodoHandler, hub: Hub
) -> None:
    store = hub.live_store
    t = _tree(handler, store)
    out = handler.search(view="work").body

    assert "# work — 1 quest with work in service" in out
    assert f"◆ qu{t['quest']} [active]" in out
    assert "Precis runs its own work" in out
    # root under the quest, slices under the root, indented deeper
    root_line = next(ln for ln in out.splitlines() if f"td{t['root']}" in ln)
    a_line = next(ln for ln in out.splitlines() if f"td{t['a']}" in ln)
    assert len(a_line) - len(a_line.lstrip()) > len(root_line) - len(root_line.lstrip())
    # the lease shows on the slice (open tags are stored lower-case)
    assert "◀ claimed-by:work_management" in a_line
    # a strategic that serves no quest lands in the unassigned block
    assert "unassigned roots (1)" in out
    assert f"td{t['lone']}" in out
    # no fleet report yet
    assert "none reported" in out


def test_work_view_counts_agents_by_handle_and_lease(
    handler: TodoHandler, hub: Hub
) -> None:
    store = hub.live_store
    t = _tree(handler, store)
    _push_fleet(
        store,
        [
            _agent("slice-a", f"build td{t['a']} work view"),
            _agent("WORK_MANAGEMENT", "spec dark factory"),
            _agent("dead-tree", f"td{t['b']}", state="dead"),
        ],
    )
    out = handler.search(view="work").body

    a_line = next(ln for ln in out.splitlines() if f"td{t['a']}" in ln)
    b_line = next(ln for ln in out.splitlines() if f"td{t['b']}" in ln)
    # handle match + lease-holder match, dead row ignored
    assert "agents=2" in a_line
    assert "agents=" not in b_line
    assert "2 live agents" in out
    assert "melchior reported" in out


def test_work_view_empty_store(handler: TodoHandler) -> None:
    out = handler.search(view="work").body
    assert "0 quests with work in service" in out
    assert "none reported" in out


def test_eye_1hop_on_a_child_shows_parent_and_peers(
    handler: TodoHandler, hub: Hub
) -> None:
    t = _tree(handler, hub.live_store)
    out = handler.get(id=t["a"], view="fisheye+1hop").body
    assert "— tree —" in out
    assert "part-of:" in out
    assert f"td{t['root']}" in out
    assert "peers (1):" in out
    assert f"td{t['b']}" in out
    assert "contains" not in out


def test_eye_1hop_on_the_root_shows_children(handler: TodoHandler, hub: Hub) -> None:
    t = _tree(handler, hub.live_store)
    out = handler.get(id=t["root"], view="fisheye+1hop").body
    assert "contains (2):" in out
    assert f"td{t['a']}" in out
    assert f"td{t['b']}" in out
    assert "part-of:" not in out


def test_bare_fisheye_has_no_tree_ring(handler: TodoHandler, hub: Hub) -> None:
    t = _tree(handler, hub.live_store)
    out = handler.get(id=t["a"], view="fisheye").body
    assert "— tree —" not in out


def test_tree_ring_empty_for_a_lone_root(handler: TodoHandler, hub: Hub) -> None:
    t = _tree(handler, hub.live_store)
    assert work_view.render_tree_ring(hub.live_store, t["lone"]) == ""
