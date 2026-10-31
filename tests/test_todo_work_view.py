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


def _plan_subtree(handler: TodoHandler, store: Store, root_id: int) -> dict[str, int]:
    """A plan under ``root_id`` (td542097): an auto_close phase holding a gate
    with a stored evaluation, a loop at 1/3, a generator, and a task with two
    child jobs (older lane ``old-lane``, newest ``claude-max``) and
    ``attempt:2``."""

    def todo(title: str, parent: int, meta: dict[str, Any]) -> int:
        return store.insert_ref(
            kind="todo", slug=None, title=title, parent_id=parent, meta=meta
        ).id

    phase = todo("Ship the slice", root_id, {"auto_close": True})
    gate = todo(
        "Wait for prod",
        phase,
        {
            "gate": {"kind": "sha-in-prod", "args": {"sha": "abc1234"}},
            "gate_result": {
                "open": False,
                "why": "sha abc1234 is not an ancestor of origin/prod yet, "
                "deploy is still pending on the release train",
                "at": "2026-10-31T10:00:00Z",
            },
        },
    )
    loop = todo(
        "Review until clean",
        phase,
        {
            "loop": {
                "max": 3,
                "count": 1,
                "until": {"kind": "answer"},
                "body": "review",
            }
        },
    )
    gen = todo(
        "Slice the spec",
        phase,
        {"generate": {"role": "slicer", "from": "docs/backlog/x.md"}},
    )
    task = todo("Run the slice", phase, {"op": "run_slice"})
    for lane in ("old-lane", "claude-max"):
        store.insert_ref(
            kind="job",
            slug=None,
            title="run_slice",
            parent_id=task,
            meta={"lane": lane},
        )
    handler.tag(id=task, add=["attempt:2"])
    handler.tag(id=gate, add=["STATUS:doing"])
    return {"phase": phase, "gate": gate, "loop": loop, "gen": gen, "task": task}


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


def test_work_view_folds_child_jobs_into_a_cell(handler: TodoHandler, hub: Hub) -> None:
    from precis.store.types import Tag

    store = hub.live_store
    t = _tree(handler, store)
    jobs = [
        store.insert_ref(
            kind="job", slug=None, title=f"plan_tick {i}", parent_id=t["a"]
        )
        for i in range(3)
    ]
    store.add_tag(
        jobs[0].id, Tag.parse_strict("STATUS:running", kind="job"), set_by="system"
    )
    store.add_tag(
        jobs[1].id, Tag.parse_strict("STATUS:succeeded", kind="job"), set_by="system"
    )
    store.add_tag(
        jobs[2].id, Tag.parse_strict("STATUS:failed", kind="job"), set_by="system"
    )
    out = handler.search(view="work").body

    a_line = next(ln for ln in out.splitlines() if f"td{t['a']}" in ln)
    assert "jobs=3(1 live)" in a_line
    # no job gets a row of its own
    assert "plan_tick" not in out
    assert f"jo{jobs[0].id}" not in out
    b_line = next(ln for ln in out.splitlines() if f"td{t['b']}" in ln)
    assert "jobs=" not in b_line


def test_work_view_renders_a_nested_served_todo_once(
    handler: TodoHandler, hub: Hub
) -> None:
    store = hub.live_store
    t = _tree(handler, store)
    # the slice serves the quest directly *and* sits under the served root
    store.add_link(src_ref_id=t["a"], dst_ref_id=t["quest"], relation="serves")
    out = handler.search(view="work").body

    assert sum(1 for ln in out.splitlines() if f"td{t['a']}" in ln) == 1
    assert "unassigned roots" in out  # the lone strategic still lands there
    assert f"td{t['a']}" not in out.split("unassigned roots")[1]


def test_work_view_plain_rows_carry_no_plan_cells(
    handler: TodoHandler, hub: Hub
) -> None:
    """A tree without plan meta renders exactly the pre-td542097 row shape."""
    t = _tree(handler, hub.live_store)
    model = work_view.work_model(hub.live_store)
    rows = [r for root in model.quests[0].roots for r in root.rows]
    assert {r.ref_id for r in rows} == {t["root"], t["a"], t["b"]}
    for r in rows:
        assert (r.node, r.node_text, r.lane, r.attempt) == ("", "", "", None)
    out = handler.search(view="work").body
    for token in ("⊢", "↻", "⇶", "lane=", "attempt:"):
        assert token not in out
    handles = tuple(f"td{t[k]} " for k in ("root", "a", "b"))
    row_lines = [ln for ln in out.splitlines() if ln.lstrip().startswith(handles)]
    assert len(row_lines) == 3
    assert all(" · " not in ln for ln in row_lines)


def test_work_view_plan_nodes_in_the_model(handler: TodoHandler, hub: Hub) -> None:
    store = hub.live_store
    t = _tree(handler, store)
    p = _plan_subtree(handler, store, t["root"])
    model = work_view.work_model(store)
    rows = {r.ref_id: r for r in model.quests[0].roots[0].rows}

    assert rows[p["phase"]].node == "phase"
    assert rows[p["phase"]].node_text == "phase"
    gate = rows[p["gate"]]
    assert gate.node == "gate"
    # why is cut to 60 characters (59 + the ellipsis)
    assert gate.node_text.startswith(
        "gate sha-in-prod abc1234 — sha abc1234 is not an ancestor of origin/prod"
    )
    assert gate.node_text.endswith("…")
    assert len(gate.node_text.split(" — ", 1)[1]) == 60
    assert rows[p["loop"]].node == "loop"
    assert rows[p["loop"]].node_text == "loop 1/3 until answer"
    assert rows[p["gen"]].node == "generator"
    assert rows[p["gen"]].node_text == "generator slicer from docs/backlog/x.md"
    task = rows[p["task"]]
    assert (task.node, task.node_text) == ("task", "")
    assert task.lane == "claude-max"  # the newest child job's lane
    assert task.attempt == 2
    assert task.jobs == 2


def test_work_view_plan_nodes_in_the_text(handler: TodoHandler, hub: Hub) -> None:
    store = hub.live_store
    t = _tree(handler, store)
    p = _plan_subtree(handler, store, t["root"])
    lines = {
        i: next(
            ln
            for ln in handler.search(view="work").body.splitlines()
            if f"td{i} " in ln
        )
        for i in p.values()
    }

    # no glyph change for a phase; node_text trails the title
    assert lines[p["phase"]].lstrip().startswith(f"td{p['phase']} ○")
    assert lines[p["phase"]].endswith("— Ship the slice · phase")
    # gate: ⊢ replaces the status icon, STATUS stays visible, predicate trails
    gate = lines[p["gate"]].lstrip()
    assert gate.startswith(f"td{p['gate']} ⊢ [doing] ")
    assert "○" not in gate
    assert " · gate sha-in-prod abc1234 — sha abc1234 is not" in gate
    loop = lines[p["loop"]].lstrip()
    assert loop.startswith(f"td{p['loop']} ↻ [open] ")
    assert loop.endswith(" · loop 1/3 until answer")
    gen = lines[p["gen"]].lstrip()
    assert gen.startswith(f"td{p['gen']} ⇶ [open] ")
    assert gen.endswith(" · generator slicer from docs/backlog/x.md")
    # task: lane and attempt cells after the jobs cell, no node_text
    task = lines[p["task"]]
    assert "jobs=2 lane=claude-max attempt:2 — Run the slice" in task
    assert " · " not in task
    assert "old-lane" not in task


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
