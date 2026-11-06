"""td573298: the completed toggle — a quest's done / won't-do todos and
terminal jobs inside the seven-day window, page-wide (``?done=1``) or
per quest id (``?done=<id>``), on ``/work`` and in ``work_model``."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis.dispatch import Hub
from precis.handlers.todo import TodoHandler
from precis.store import Store
from precis.store.types import ChunkInsert, Tag
from precis.todo import work_view
from precis_web.app import create_app
from precis_web.config import WebConfig
from tests.conftest import id_of
from tests.test_work_board_rows import _job, _quest, _quest_tree


@pytest.fixture
def handler(hub: Hub) -> TodoHandler:
    return TodoHandler(hub=hub)


@pytest.fixture
def client(runtime_with_store: Any, tmp_path: Path) -> TestClient:
    app = create_app(
        runtime=runtime_with_store, web_config=WebConfig(corpus_dir=tmp_path)
    )
    return TestClient(app)


def _status_at(store: Store, ref_id: int, at: datetime) -> None:
    """Move the ref's STATUS tag's ``ref_tags.created_at`` — the
    completion time the seven-day window reads."""
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE ref_tags rt SET created_at = %s FROM tags t "
            "WHERE rt.tag_id = t.tag_id AND rt.ref_id = %s "
            "AND t.namespace = 'STATUS'",
            (at, ref_id),
        )


def _comment(store: Store, ref_id: int, ord_: int, text: str) -> None:
    store.chunks.insert_chunks(
        ref_id,
        [ChunkInsert(ord=ord_, text=text, meta={"chunk_kind": "todo_comment"})],
    )


def _lane_job(store: Store, parent: int, status: str, lane: str) -> int:
    job = store.insert_ref(
        kind="job", slug=None, title="run_slice", parent_id=parent, meta={"lane": lane}
    )
    store.add_tag(
        job.id, Tag.parse_strict(f"STATUS:{status}", kind="job"), set_by="system"
    )
    return job.id


def _done_handles(model: work_view.WorkModel, quest_id: int) -> set[str]:
    q = _quest(model, quest_id)
    return {c.handle for c in q.completed}


def test_done_row_leaves_the_board_by_default(
    client: TestClient, handler: TodoHandler, store: Store
) -> None:
    t = _quest_tree(handler, store, "Done row quest")
    handler.tag(id=t["leaf"], add=["STATUS:done"])
    model = work_view.work_model(store)
    shown = {r.handle for q in model.quests for root in q.roots for r in root.rows}
    assert f"td{t['leaf']}" not in shown
    assert f"td{t['root']}" in shown
    assert all(q.completed == () for q in model.quests)
    assert f"td{t['leaf']}" not in handler.search(view="work").body
    html = client.get("/work").text
    assert f"td{t['leaf']}" not in html
    assert 'data-kind="completed"' not in html


def test_done_parent_still_walks_its_children(
    handler: TodoHandler, store: Store
) -> None:
    t = _quest_tree(handler, store, "Done parent quest")
    child = id_of(
        handler.put(
            text="Open child.", parent_id=t["leaf"], meta={"llm_tier": None}
        ).body
    )
    handler.tag(id=t["leaf"], add=["STATUS:done"])
    model = work_view.work_model(store)
    rows = {r.ref_id: r for q in model.quests for root in q.roots for r in root.rows}
    assert t["leaf"] not in rows
    assert rows[child].depth == 3


def test_completed_todos_match_view_done_in_window(
    handler: TodoHandler, store: Store
) -> None:
    t = _quest_tree(handler, store, "Completed quest")
    extra = id_of(
        handler.put(
            text="Second slice.", parent_id=t["root"], meta={"llm_tier": None}
        ).body
    )
    wont = id_of(
        handler.put(
            text="Dropped slice.", parent_id=t["root"], meta={"llm_tier": None}
        ).body
    )
    handler.tag(id=t["leaf"], add=["STATUS:done"])
    handler.tag(id=extra, add=["STATUS:done"])
    handler.tag(id=wont, add=["STATUS:won't-do"])
    # a done todo in another quest's tree must not leak in
    other = _quest_tree(handler, store, "Other quest")
    handler.tag(id=other["leaf"], add=["STATUS:done"])

    q = _quest(work_view.work_model(store, done=True), t["quest"])
    completed_todos = {c.handle for c in q.completed if c.kind == "todo"}
    # view='done' lists the STATUS:done rows; the toggle adds won't-do
    done_listed = {
        int(m.group(1))
        for m in re.finditer(
            r"^\s+(\d+)\s+\[done\s*\]",
            handler.search(view="done").body,
            re.M,
        )
    }
    assert done_listed == {t["leaf"], extra, other["leaf"]}
    assert completed_todos == {
        f"td{i}" for i in (done_listed - {other["leaf"]}) | {wont}
    }
    outcomes = {c.handle: c.outcome for c in q.completed}
    assert outcomes[f"td{wont}"] == "won't-do"
    # only the toggled quest carries completed rows
    q_other = _quest(
        work_view.work_model(store, done=frozenset({t["quest"]})), other["quest"]
    )
    assert q_other.completed == ()


def test_landed_sha_is_the_newest_landed_comment(
    handler: TodoHandler, store: Store
) -> None:
    t = _quest_tree(handler, store, "Landed quest")
    handler.tag(id=t["leaf"], add=["STATUS:done"])
    _comment(store, t["leaf"], 1, "landed: aaa1111 — first landing")
    _comment(store, t["leaf"], 2, "landed: bbb2222 — relanded")
    q = _quest(work_view.work_model(store, done=True), t["quest"])
    row = next(c for c in q.completed if c.handle == f"td{t['leaf']}")
    assert row.detail == "bbb2222"
    # a ``landed: nothing`` (no-diff landing) names no sha
    _comment(store, t["leaf"], 3, "landed: nothing — already on main")
    q2 = _quest(work_view.work_model(store, done=True), t["quest"])
    row2 = next(c for c in q2.completed if c.handle == f"td{t['leaf']}")
    assert row2.detail == ""


def test_window_boundary_includes_exactly_seven_days(
    handler: TodoHandler, store: Store
) -> None:
    now = datetime(2026, 11, 6, 12, 0, 0, tzinfo=UTC)
    t = _quest_tree(handler, store, "Window quest")
    older = id_of(
        handler.put(
            text="Older slice.", parent_id=t["root"], meta={"llm_tier": None}
        ).body
    )
    handler.tag(id=t["leaf"], add=["STATUS:done"])
    handler.tag(id=older, add=["STATUS:done"])
    _status_at(store, t["leaf"], now - timedelta(days=7))
    _status_at(store, older, now - timedelta(days=7) - timedelta(seconds=1))
    model = work_view.work_model(store, now=now, done=True)
    handles = _done_handles(model, t["quest"])
    assert f"td{t['leaf']}" in handles
    assert f"td{older}" not in handles


def test_terminal_jobs_in_window_running_never_listed(
    handler: TodoHandler, store: Store
) -> None:
    now = datetime(2026, 11, 6, 12, 0, 0, tzinfo=UTC)
    t = _quest_tree(handler, store, "Job quest")
    ok = _lane_job(store, t["leaf"], "succeeded", "devin")
    bad = _job(store, t["leaf"], "failed")
    stop = _job(store, t["leaf"], "cancelled")
    old = _job(store, t["leaf"], "succeeded")
    running = _job(store, t["leaf"], "running")
    _status_at(store, ok, now - timedelta(hours=1))
    _status_at(store, bad, now - timedelta(hours=2))
    _status_at(store, stop, now - timedelta(hours=3))
    _status_at(store, old, now - timedelta(days=8))
    q = _quest(work_view.work_model(store, now=now, done=True), t["quest"])
    jobs = {c.handle: c for c in q.completed if c.kind == "job"}
    assert set(jobs) == {f"jo{ok}", f"jo{bad}", f"jo{stop}"}
    assert jobs[f"jo{ok}"].outcome == "succeeded"
    assert jobs[f"jo{ok}"].detail == "devin"
    # newest first
    assert [c.handle for c in q.completed] == [f"jo{ok}", f"jo{bad}", f"jo{stop}"]


def test_abandoned_quest_boards_only_when_toggled(
    handler: TodoHandler, store: Store
) -> None:
    t = _quest_tree(handler, store, "Abandoned quest")
    store.add_tag(t["quest"], Tag.closed("STATUS", "abandoned"), set_by="system")
    handler.tag(id=t["leaf"], add=["STATUS:done"])
    assert t["quest"] not in {q.ref_id for q in work_view.work_model(store).quests}
    untoggled = work_view.work_model(store, done=frozenset({t["quest"] + 9999}))
    assert t["quest"] not in {q.ref_id for q in untoggled.quests}
    q = _quest(work_view.work_model(store, done=frozenset({t["quest"]})), t["quest"])
    assert f"td{t['leaf']}" in {c.handle for c in q.completed}
    assert t["quest"] in {
        q.ref_id for q in work_view.work_model(store, done=True).quests
    }


def test_run_state_and_next_doable_unchanged_by_toggle(
    handler: TodoHandler, store: Store
) -> None:
    t = _quest_tree(handler, store, "Toggle quest")
    handler.tag(id=t["leaf"], add=["STATUS:done"])
    extra = id_of(
        handler.put(
            text="Open slice.", parent_id=t["root"], meta={"llm_tier": None}
        ).body
    )
    base = _quest(work_view.work_model(store), t["quest"])
    on = _quest(work_view.work_model(store, done=True), t["quest"])
    assert (base.run_state, base.next_doable) == ("queued", f"td{extra}")
    assert (on.run_state, on.next_doable) == (base.run_state, base.next_doable)


def _toggle_url(html: str, quest_id: int) -> str:
    m = re.search(r'href="([^"]+)"[^>]*data-done-toggle="' + str(quest_id) + r'"', html)
    assert m is not None, f"no completed toggle for quest {quest_id}"
    return m.group(1)


def test_page_done_param_round_trips(
    client: TestClient, handler: TodoHandler, store: Store
) -> None:
    t = _quest_tree(handler, store, "Page quest")
    other = _quest_tree(handler, store, "Other quest")
    handler.tag(id=t["leaf"], add=["STATUS:done"])
    handler.tag(id=other["leaf"], add=["STATUS:done"])
    _comment(store, t["leaf"], 1, "landed: abc1234de — feat(work): x")

    html = client.get("/work").text
    assert f"td{t['leaf']}" not in html
    assert 'data-kind="completed"' not in html
    assert "Completed (7d)" in html  # the page-wide on-link

    html_all = client.get("/work?done=1").text
    assert 'data-kind="completed"' in html_all
    assert f'href="/eye/td{t["leaf"]}"' in html_all
    assert f'href="/eye/td{other["leaf"]}"' in html_all
    assert "abc1234de" in html_all
    assert "Completed (1)" in html_all
    assert "Hide completed" in html_all  # the page-wide off-link
    assert f'href="/refs/quest/{t["quest"]}/logbook"' in html_all
    # under ?done=1 every per-quest toggle stays page-wide
    for qid in (t["quest"], other["quest"]):
        assert _toggle_url(html_all, qid) == "/work?done=1"

    # ?done=<id>: only that quest lists its completed rows (the first
    # quest's ref id is 1 — done=1 is the page-wide sentinel — so a
    # comma list exercises the single-id path here)
    html_one = client.get(f"/work?done={t['quest']},999999").text
    assert f'href="/eye/td{t["leaf"]}"' in html_one
    assert f"td{other['leaf']}" not in html_one
    # the toggled quest's link removes its own id, keeping the rest
    assert _toggle_url(html_one, t["quest"]) == "/work?done=999999"
    # the other quest's link adds its id, sorted, preserving the state
    expect = "/work?done=" + ",".join(
        str(i) for i in sorted([t["quest"], other["quest"], 999999])
    )
    assert _toggle_url(html_one, other["quest"]) == expect
    # a lone non-1 id toggles one quest and clears back to the bare page
    one_id = next(i for i in (t["quest"], other["quest"]) if i != 1)
    html_two = client.get(f"/work?done={one_id}").text
    assert 'data-kind="completed"' in html_two
    assert _toggle_url(html_two, one_id) == "/work"
