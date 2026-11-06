"""Queue dependencies and stored execution placement (td573296)."""

from __future__ import annotations

import re
from unittest.mock import patch

from fastapi.testclient import TestClient

from precis.handlers.todo import TodoHandler
from precis.plans.lanes import ladder
from precis.store import Store
from precis.todo import work_view
from tests.test_work_board_rows import _job, _quest, _quest_tree, client, handler

__all__ = ["client", "handler"]


def _rows(store: Store, tree: dict[str, int]) -> dict[int, work_view.WorkRow]:
    quest = _quest(work_view.work_model(store), tree["quest"])
    return {r.ref_id: r for block in quest.roots for r in block.rows}


def test_blocked_row_shows_after_marker(
    handler: TodoHandler, store: Store, client: TestClient
) -> None:
    tree = _quest_tree(handler, store, "Blocked")
    blocker = store.insert_ref(kind="todo", slug=None, title="Open blocker")
    store.add_link(
        src_ref_id=tree["leaf"], dst_ref_id=blocker.id, relation="blocked-by"
    )
    assert _rows(store, tree)[tree["leaf"]].after == (f"td{blocker.id}",)
    assert f"after td{blocker.id}" in handler.search(view="work").body
    html = client.get("/work").text
    row = re.search(rf'<li[^>]*data-handle="td{tree["leaf"]}".*?</li>', html, re.S)
    assert row is not None
    assert f'href="/eye/td{blocker.id}"' in row.group()


def test_done_blocker_gives_no_marker(handler: TodoHandler, store: Store) -> None:
    tree = _quest_tree(handler, store, "Done blocker")
    blocker = store.insert_ref(kind="todo", slug=None, title="Done blocker")
    handler.tag(id=blocker.id, add=["STATUS:done"])
    store.add_link(
        src_ref_id=tree["leaf"], dst_ref_id=blocker.id, relation="blocked-by"
    )
    assert _rows(store, tree)[tree["leaf"]].after == ()


def test_gated_row_shows_why(handler: TodoHandler, store: Store) -> None:
    tree = _quest_tree(handler, store, "Gate")
    gate = store.insert_ref(
        kind="todo",
        slug=None,
        title="Gate",
        parent_id=tree["root"],
        meta={
            "gate": {"kind": "answer"},
            "gate_result": {"open": False, "why": "Needs a decision"},
        },
    )
    row = _rows(store, tree)[gate.id]
    assert row.hold == row.node_text
    assert "Needs a decision" in row.hold
    assert row.preview == ""


def test_waiting_for_and_ask_user_hold(handler: TodoHandler, store: Store) -> None:
    tree = _quest_tree(handler, store, "Holds")
    handler.tag(id=tree["leaf"], add=["waiting-for:review", "ask-user"])
    assert _rows(store, tree)[tree["leaf"]].hold == "waiting-for:review"
    handler.tag(id=tree["leaf"], remove=["waiting-for:review"])
    assert _rows(store, tree)[tree["leaf"]].hold == "ask-user"


def test_queued_slice_preview(handler: TodoHandler, store: Store) -> None:
    tree = _quest_tree(handler, store, "Preview")
    assert _rows(store, tree)[tree["leaf"]].preview == ""
    handler.tag(id=tree["root"], add=[f"plan:{tree['root']}"])
    rung = ladder("code")[0]
    expected = f"{rung.lane} {rung.model or ''}".strip()
    assert _rows(store, tree)[tree["leaf"]].preview == expected
    assert _rows(store, tree)[tree["root"]].preview == ""
    assert f"preview: {expected}" in handler.search(view="work").body
    _job(store, tree["leaf"], "failed")
    assert _rows(store, tree)[tree["leaf"]].preview == ""


def test_dispatched_job_line_has_lane_model_host(
    handler: TodoHandler, store: Store
) -> None:
    tree = _quest_tree(handler, store, "Dispatched")
    jid = _job(store, tree["leaf"], "running")
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = %s::jsonb WHERE ref_id = %s",
            ('{"lane":"codex","model":"test-model","lease_host":"test-host"}', jid),
        )
    quest = _quest(work_view.work_model(store), tree["quest"])
    assert quest.jobs == (
        f"  jo{jid} [running] run_slice · under td{tree['leaf']} · lane=codex model=test-model host=test-host",
    )
    assert _rows(store, tree)[tree["leaf"]].lane == "codex"


def test_batched_queries(handler: TodoHandler, store: Store) -> None:
    tree = _quest_tree(handler, store, "Batch")
    count = 2  # the root and its initial leaf
    for size in (5, 50):
        for i in range(size - count):
            store.insert_ref(
                kind="todo", slug=None, title=f"Leaf {size} {i}", parent_id=tree["root"]
            )
        count = size
        with (
            patch.object(
                work_view, "_open_blockers", wraps=work_view._open_blockers
            ) as blockers,
            patch.object(
                work_view, "_job_fields", wraps=work_view._job_fields
            ) as fields,
        ):
            work_view.work_model(store)
        assert blockers.call_count == fields.call_count == 1
