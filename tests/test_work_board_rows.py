"""td573295: quest run-state (running / queued / idle) and the one-line
collapsed rows on ``search(kind='todo', view='work')`` and ``/work``."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis.dispatch import Hub
from precis.handlers.todo import TodoHandler
from precis.store import Store
from precis.store.types import Tag
from precis.todo import work_view
from precis_web.app import create_app
from precis_web.config import WebConfig
from tests.conftest import id_of
from tests.test_todo_work_view import _tree


@pytest.fixture
def handler(hub: Hub) -> TodoHandler:
    return TodoHandler(hub=hub)


@pytest.fixture
def client(runtime_with_store: Any, tmp_path: Path) -> TestClient:
    app = create_app(
        runtime=runtime_with_store, web_config=WebConfig(corpus_dir=tmp_path)
    )
    return TestClient(app)


def _quest_tree(handler: TodoHandler, store: Store, title: str) -> dict[str, int]:
    """A quest served by one rotation root holding one open leaf."""
    quest = store.insert_ref(kind="quest", slug=None, title=title)
    root = id_of(handler.put(text=f"{title} root.", meta={"rotation_root": True}).body)
    leaf = id_of(
        handler.put(text=f"{title} leaf.", parent_id=root, meta={"llm_tier": None}).body
    )
    store.add_link(src_ref_id=root, dst_ref_id=quest.id, relation="serves")
    return {"quest": quest.id, "root": root, "leaf": leaf}


def _job(store: Store, parent: int, status: str) -> int:
    job = store.insert_ref(kind="job", slug=None, title="run_slice", parent_id=parent)
    store.add_tag(
        job.id, Tag.parse_strict(f"STATUS:{status}", kind="job"), set_by="system"
    )
    return job.id


def _quest(model: work_view.WorkModel, ref_id: int) -> work_view.QuestBlock:
    return next(q for q in model.quests if q.ref_id == ref_id)


def test_run_state_pure() -> None:
    rs = work_view._run_state
    assert rs(1, 0, 0, 0) == "running"
    assert rs(0, 2, 0, 0) == "running"
    assert rs(0, 0, 1, 0) == "queued"
    assert rs(0, 0, 0, 3) == "queued"
    assert rs(0, 0, 0, 0) == "idle"
    # running wins over every queued signal
    assert rs(1, 1, 1, 1) == "running"
    assert rs(0, 1, 1, 1) == "running"


def test_quest_running_by_live_lease(handler: TodoHandler, hub: Hub) -> None:
    t = _tree(handler, hub.live_store)
    model = work_view.work_model(hub.live_store)
    q = _quest(model, t["quest"])
    assert q.run_state == "running"
    out = handler.search(view="work").body
    assert f"◆ qu{t['quest']} running [active]" in out


def test_quest_running_by_running_job(handler: TodoHandler, hub: Hub) -> None:
    store = hub.live_store
    t = _quest_tree(handler, store, "Running job quest")
    jid = _job(store, t["leaf"], "running")
    model = work_view.work_model(store)
    q = _quest(model, t["quest"])
    assert q.run_state == "running"
    assert q.running_jobs == 1
    assert q.queued_jobs == 0
    assert len(q.jobs) == 1
    assert f"jo{jid} [running]" in q.jobs[0]
    assert f"td{t['leaf']}" in q.jobs[0]


def test_expired_lease_is_not_running(handler: TodoHandler, hub: Hub) -> None:
    store = hub.live_store
    t = _quest_tree(handler, store, "Expired lease quest")
    handler.tag(id=t["leaf"], add=["claimed-by:gone-agent"])
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE ref_tags SET expires_at = now() - interval '1 minute'"
            " WHERE ref_id = %s",
            (t["leaf"],),
        )
    q = _quest(work_view.work_model(store), t["quest"])
    # the expired lease frees the leaf — it is doable again, so queued
    assert q.run_state == "queued"
    assert q.next_doable == f"td{t['leaf']}"


def test_quest_queued_when_only_doable_slices(handler: TodoHandler, hub: Hub) -> None:
    t = _quest_tree(handler, hub.live_store, "Doable quest")
    q = _quest(work_view.work_model(hub.live_store), t["quest"])
    assert q.run_state == "queued"
    assert q.next_doable == f"td{t['leaf']}"
    assert q.jobs == ()


def test_quest_queued_by_yielded_job(handler: TodoHandler, hub: Hub) -> None:
    store = hub.live_store
    t = _quest_tree(handler, store, "Yielded job quest")
    _job(store, t["leaf"], "yielded")
    q = _quest(work_view.work_model(store), t["quest"])
    assert q.run_state == "queued"
    assert q.queued_jobs == 1
    assert q.running_jobs == 0


def test_quest_idle_without_served_todos(handler: TodoHandler, hub: Hub) -> None:
    store = hub.live_store
    quest = store.insert_ref(kind="quest", slug=None, title="No servers")
    model = work_view.work_model(store)
    q = _quest(model, quest.id)
    assert q.run_state == "idle"
    assert q.roots == ()
    out = handler.search(view="work").body
    assert f"◆ qu{quest.id} idle" in out


def test_quest_idle_when_all_slices_done(handler: TodoHandler, hub: Hub) -> None:
    store = hub.live_store
    t = _quest_tree(handler, store, "Finished quest")
    handler.tag(id=t["leaf"], add=["STATUS:done"])
    q = _quest(work_view.work_model(store), t["quest"])
    assert q.run_state == "idle"


def test_dormant_quest_with_running_work_reads_running(
    handler: TodoHandler, hub: Hub
) -> None:
    store = hub.live_store
    t = _quest_tree(handler, store, "Dormant but running")
    store.add_tag(t["quest"], Tag.closed("STATUS", "dormant"), set_by="system")
    _job(store, t["leaf"], "running")
    q = _quest(work_view.work_model(store), t["quest"])
    assert q.run_state == "running"
    assert q.status == "dormant"


def test_blocks_ordered_running_queued_idle_then_prio(
    handler: TodoHandler, hub: Hub
) -> None:
    store = hub.live_store
    queued = _quest_tree(handler, store, "Queued quest")
    run_low = _quest_tree(handler, store, "Running prio 4")
    run_high = _quest_tree(handler, store, "Running prio 2")
    idle = store.insert_ref(kind="quest", slug=None, title="Idle quest")
    store.set_prio(run_high["quest"], 2)
    store.set_prio(run_low["quest"], 4)
    store.set_prio(idle.id, 1)
    _job(store, run_low["leaf"], "running")
    _job(store, run_high["leaf"], "running")
    model = work_view.work_model(store)
    assert [q.ref_id for q in model.quests] == [
        run_high["quest"],
        run_low["quest"],
        queued["quest"],
        idle.id,
    ]
    assert (model.n_running, model.n_queued, model.n_idle) == (2, 1, 1)
    # the text view groups in the same order
    out = handler.search(view="work").body
    assert out.index("running (2)") < out.index("queued (1)")
    assert out.index("queued (1)") < out.index("idle (1)")


def test_doable_view_unchanged(handler: TodoHandler, hub: Hub) -> None:
    """The ``_doable_rows`` split keeps ``view='doable'`` identical."""
    store = hub.live_store
    t = _tree(handler, store)
    out = handler.search(view="doable").body
    assert "doable" in out
    # b is the only free leaf: a carries a live lease
    assert f"td{t['b']}" in out
    assert f"td{t['a']}" not in out


# ── the /work page ────────────────────────────────────────────────


def _sections(html: str) -> dict[str, str]:
    """``data-handle`` → the quest ``<section>`` opening tag."""
    return {
        m.group(1): m.group(0)
        for m in re.finditer(
            r'<section[^>]*data-handle="([^"]+)"[^>]*data-kind="quest"[^>]*>',
            html,
        )
    }


def _run_state_counts(html: str) -> dict[str, int]:
    counts = {"running": 0, "queued": 0, "idle": 0}
    for m in re.finditer(r'data-run-state="(\w+)"', html):
        counts[m.group(1)] += 1
    return counts


def test_page_one_summary_line_per_quest(
    client: TestClient, handler: TodoHandler, store: Store
) -> None:
    t = _tree(handler, store)
    html = client.get("/work").text
    quest_tag = _sections(html)[f"qu{t['quest']}"]
    assert 'data-run-state="running"' in quest_tag
    # the collapsed line is a closed <details> with one <summary>
    assert html.count("<summary") == 1
    assert "<details" in html
    assert "open" not in html.split("<details", 1)[1].split(">", 1)[0]
    summary = html[html.index("<summary") : html.index("</summary>")]
    assert f'href="/eye/qu{t["quest"]}"' in summary
    assert "Precis runs its own work" in summary
    assert re.search(r"\d+[smhd] ago", summary)


def test_page_groups_in_order(
    client: TestClient, handler: TodoHandler, store: Store
) -> None:
    _quest_tree(handler, store, "Queued quest")
    running = _quest_tree(handler, store, "Running quest")
    _job(store, running["leaf"], "running")
    store.insert_ref(kind="quest", slug=None, title="Idle quest")
    html = client.get("/work").text
    assert html.index("Running (1)") < html.index("Queued (1)")
    assert html.index("Queued (1)") < html.index("Idle (1)")


def test_page_counts_match_text_header(
    client: TestClient, handler: TodoHandler, store: Store
) -> None:
    _tree(handler, store)
    _quest_tree(handler, store, "Queued quest")
    store.insert_ref(kind="quest", slug=None, title="Idle quest")
    html = client.get("/work").text
    assert _run_state_counts(html) == {"running": 1, "queued": 1, "idle": 1}
    assert "1 running" in html and "1 queued" in html and "1 idle" in html
    # the MCP text header reports the same counts
    text = handler.search(view="work").body
    assert "1 running · 1 queued · 1 idle" in text


def test_page_rows_still_addressable(
    client: TestClient, handler: TodoHandler, store: Store
) -> None:
    t = _tree(handler, store)
    html = client.get("/work").text
    tags = {
        m.group(1): m.group(0)
        for m in re.finditer(r'<(?:li|section)[^>]*data-handle="([^"]+)"[^>]*>', html)
    }
    for handle in (
        f"qu{t['quest']}",
        f"td{t['root']}",
        f"td{t['a']}",
        f"td{t['b']}",
    ):
        assert handle in tags, f"no data-handle row for {handle}"
        assert f'href="/eye/{handle}"' in html


def test_page_read_only_still_holds(
    client: TestClient, handler: TodoHandler, store: Store
) -> None:
    _tree(handler, store)
    html = client.get("/work").text
    content = html[html.index("<h1") :]
    assert "<form" not in content
    assert "<button" not in content
    assert "<input" not in content
    assert client.post("/work").status_code == 405


def _slice(handler: TodoHandler, root: int, title: str) -> int:
    return id_of(handler.put(text=title, parent_id=root, meta={"llm_tier": None}).body)


def test_quest_with_mixed_running_queued_blocked_slices(
    handler: TodoHandler, hub: Hub
) -> None:
    """Running wins: one leased-free running job, one doable leaf and one
    blocked leaf still read ``running``; ``next=`` names the doable one."""
    store = hub.live_store
    t = _quest_tree(handler, store, "Mixed quest")
    queued = _slice(handler, t["root"], "Queued slice.")
    blocked = _slice(handler, t["root"], "Blocked slice.")
    handler.tag(id=blocked, add=["STATUS:blocked"])
    _job(store, t["leaf"], "running")
    q = _quest(work_view.work_model(store), t["quest"])
    assert q.run_state == "running"
    assert q.next_doable == f"td{queued}"


def test_blocked_only_quest_reads_idle(handler: TodoHandler, hub: Hub) -> None:
    store = hub.live_store
    t = _quest_tree(handler, store, "Blocked quest")
    handler.tag(id=t["leaf"], add=["STATUS:blocked"])
    q = _quest(work_view.work_model(store), t["quest"])
    assert q.run_state == "idle"


def test_paused_only_quest_reads_idle(handler: TodoHandler, hub: Hub) -> None:
    store = hub.live_store
    t = _quest_tree(handler, store, "Paused quest")
    handler.tag(id=t["leaf"], add=["STATUS:paused"])
    q = _quest(work_view.work_model(store), t["quest"])
    assert q.run_state == "idle"


def test_abandoned_quest_is_left_off_the_board(handler: TodoHandler, hub: Hub) -> None:
    store = hub.live_store
    t = _quest_tree(handler, store, "Abandoned quest")
    store.add_tag(t["quest"], Tag.closed("STATUS", "abandoned"), set_by="system")
    model = work_view.work_model(store)
    assert t["quest"] not in {q.ref_id for q in model.quests}
    # its served root falls out to unassigned
    assert t["root"] in {u.ref_id for u in model.unassigned}


def test_added_queries_are_constant_for_5_and_50_quests(
    handler: TodoHandler, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The td573295 inputs run set-based: one ``_live_claims`` and one
    ``_prios_for`` per ``work_model``, whatever the quest count."""
    store = hub.live_store
    calls = {"claims": 0, "prios": 0}
    real_claims = work_view._live_claims
    real_prios = work_view._prios_for

    def spy_claims(s: Store, ids: list[int]) -> set[int]:
        calls["claims"] += 1
        return real_claims(s, ids)

    def spy_prios(s: Store, ids: list[int]) -> dict[int, int | None]:
        calls["prios"] += 1
        return real_prios(s, ids)

    monkeypatch.setattr(work_view, "_live_claims", spy_claims)
    monkeypatch.setattr(work_view, "_prios_for", spy_prios)
    for n in (5, 50):
        for i in range(n):
            _quest_tree(handler, store, f"Q{i} of {n}")
        calls["claims"] = 0
        calls["prios"] = 0
        work_view.work_model(store)
        assert (calls["claims"], calls["prios"]) == (1, 1)


def test_quest_title_with_html_is_escaped_on_page(
    client: TestClient, handler: TodoHandler, store: Store
) -> None:
    store.insert_ref(kind="quest", slug=None, title="Quest <script>alert(1)</script>")
    html = client.get("/work").text
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
