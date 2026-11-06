"""td573299 (spec me573285 §W5): the job → quest resolver, the /work
``unassigned jobs`` bucket and the /status "Jobs with no quest" row.

A live job resolves two ways: its ``parent_id`` chain up through live
todos to the nearest ancestor that ``serves`` a live quest, else its own
meta (``params.quest_id``, ``coordinator_state.quest_id``, a structure's
``quest:<id>`` tag). Unresolved platform-upkeep rows with no todo parent
file under qu540835; the rest are the unassigned health signal.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis.store import Store
from precis.store.types import Tag
from precis.todo import work_quest, work_view
from precis.workers.job_describe import UPKEEP_QUEST_HANDLE
from precis_web.app import create_app
from precis_web.config import WebConfig
from precis_web.routes.status import _no_quest_jobs


@pytest.fixture
def client(runtime_with_store: Any, tmp_path: Path) -> TestClient:
    app = create_app(
        runtime=runtime_with_store, web_config=WebConfig(corpus_dir=tmp_path)
    )
    return TestClient(app)


def _todo(store: Store, title: str, parent_id: int | None = None) -> int:
    return store.insert_ref(kind="todo", slug=None, title=title, parent_id=parent_id).id


def _job(
    store: Store,
    parent_id: int | None = None,
    status: str = "running",
    meta: dict[str, Any] | None = None,
) -> int:
    job = store.insert_ref(
        kind="job",
        slug=None,
        title=(meta or {}).get("job_type") or "job",
        parent_id=parent_id,
        meta=meta,
    )
    store.add_tag(
        job.id, Tag.parse_strict(f"STATUS:{status}", kind="job"), set_by="system"
    )
    return job.id


def _quest(store: Store, title: str = "Quest") -> int:
    return store.insert_ref(kind="quest", slug=None, title=title).id


# ── the resolver ──────────────────────────────────────────────────


def test_chain_resolves_a_serving_root_two_levels_up(store: Store) -> None:
    """job → mid todo → serving root → quest: the chain wins."""
    quest = _quest(store)
    root = _todo(store, "Root")
    mid = _todo(store, "Mid", parent_id=root)
    jid = _job(store, mid)
    store.add_link(src_ref_id=root, dst_ref_id=quest, relation="serves")
    assert work_quest.resolve_job_quests(store, [jid]) == {jid: quest}


def test_no_todo_parent_falls_back_to_meta_quest_id(store: Store) -> None:
    """A parentless quest_tick resolves on ``params.quest_id``."""
    quest = _quest(store)
    jid = _job(
        store,
        meta={"job_type": "quest_tick", "params": {"quest_id": quest}},
    )
    assert work_quest.resolve_job_quests(store, [jid]) == {jid: quest}


def test_struct_parent_resolves_via_the_structure_quest_tag(
    store: Store,
) -> None:
    """struct_relax parents on a structure, not a todo: the chain is
    empty and the structure's ``quest:<id>`` tag answers."""
    quest = _quest(store)
    struct = store.insert_ref(kind="structure", slug="s1", title="s1")
    store.add_tag(struct.id, Tag.open(f"quest:{quest}"), set_by="agent")
    jid = _job(
        store,
        parent_id=struct.id,
        meta={
            "job_type": "struct_relax",
            "params": {"structure_ref_id": struct.id},
        },
    )
    assert work_quest.resolve_job_quests(store, [jid]) == {jid: quest}


def test_nothing_serving_and_no_meta_resolves_none(store: Store) -> None:
    root = _todo(store, "Root")
    jid = _job(store, root, meta={"job_type": "paper_pull"})
    assert work_quest.resolve_job_quests(store, [jid]) == {jid: None}


def test_nearest_serving_ancestor_wins(store: Store) -> None:
    """A parent that serves beats a grandparent that serves."""
    outer, inner = _quest(store, "Outer"), _quest(store, "Inner")
    root = _todo(store, "Root")
    mid = _todo(store, "Mid", parent_id=root)
    jid = _job(store, mid)
    store.add_link(src_ref_id=root, dst_ref_id=outer, relation="serves")
    store.add_link(src_ref_id=mid, dst_ref_id=inner, relation="serves")
    assert work_quest.resolve_job_quests(store, [jid]) == {jid: inner}


def test_several_quests_at_one_depth_take_the_lowest(store: Store) -> None:
    low, high = _quest(store, "Low"), _quest(store, "High")
    root = _todo(store, "Root")
    jid = _job(store, root)
    store.add_link(src_ref_id=root, dst_ref_id=high, relation="serves")
    store.add_link(src_ref_id=root, dst_ref_id=low, relation="serves")
    assert low < high
    assert work_quest.resolve_job_quests(store, [jid]) == {jid: low}


def test_retired_quest_is_ignored(store: Store) -> None:
    quest = _quest(store)
    root = _todo(store, "Root")
    jid = _job(store, root)
    store.add_link(src_ref_id=root, dst_ref_id=quest, relation="serves")
    store.retire_ref(quest)
    assert work_quest.resolve_job_quests(store, [jid]) == {jid: None}


def test_retired_root_cuts_the_walk(store: Store) -> None:
    """A retired serving root is skipped AND ends the chain — the job
    falls to its meta (absent here) and resolves None."""
    quest = _quest(store)
    root = _todo(store, "Root")
    mid = _todo(store, "Mid", parent_id=root)
    jid = _job(store, mid)
    store.add_link(src_ref_id=root, dst_ref_id=quest, relation="serves")
    store.retire_ref(root)
    assert work_quest.resolve_job_quests(store, [jid]) == {jid: None}


def test_meta_quest_id_that_is_not_a_quest_counts_as_none(
    store: Store,
) -> None:
    """``params.quest_id`` pointing at a live non-quest ref resolves
    None — the id is confirmed against ``kind='quest'``."""
    not_a_quest = _todo(store, "Not a quest")
    jid = _job(
        store,
        meta={"job_type": "quest_tick", "params": {"quest_id": not_a_quest}},
    )
    assert work_quest.resolve_job_quests(store, [jid]) == {jid: None}


def test_query_count_is_constant_for_5_and_50_jobs(store: Store) -> None:
    """The resolver runs a constant number of queries: same shapes at
    5 and 50 jobs cost the same execute calls."""
    quest = _quest(store)
    struct = store.insert_ref(kind="structure", slug="s1", title="s1")
    store.add_tag(struct.id, Tag.open(f"quest:{quest}"), set_by="agent")
    root = _todo(store, "Root")
    store.add_link(src_ref_id=root, dst_ref_id=quest, relation="serves")

    class _Spy:
        def __init__(self, conn: Any) -> None:
            self._conn = conn
            self.n = 0

        def execute(self, *args: Any, **kwargs: Any) -> Any:
            self.n += 1
            return self._conn.execute(*args, **kwargs)

    counts: list[int] = []
    with store.pool.connection() as conn:
        spy = _Spy(conn)
        for n in (5, 50):
            ids = [
                _job(store, root, meta={"job_type": "sandbox_run"}) for _ in range(n)
            ]
            ids.append(
                _job(
                    store,
                    parent_id=struct.id,
                    meta={
                        "job_type": "struct_relax",
                        "params": {"structure_ref_id": struct.id},
                    },
                )
            )
            spy.n = 0
            work_quest.resolve_job_quests(store, ids, conn=spy)
            counts.append(spy.n)
    assert counts[0] == counts[1]


def test_platform_quest_id_matches_upkeep_handle() -> None:
    assert int(UPKEEP_QUEST_HANDLE[2:]) == work_quest.PLATFORM_QUEST_ID


# ── the board ─────────────────────────────────────────────────────


def test_classify_splits_unassigned_from_platform(store: Store) -> None:
    """A parentless paper_pull is unassigned; a parentless upkeep type
    is platform; a chain-resolved job is neither."""
    quest = _quest(store)
    root = _todo(store, "Root")
    store.add_link(src_ref_id=root, dst_ref_id=quest, relation="serves")
    unassigned = _job(store, meta={"job_type": "paper_pull"})
    upkeep = _job(store, meta={"job_type": "derived_drain"})
    served = _job(store, root, meta={"job_type": "run_slice"})
    # a platform type WITH a todo parent is unassigned, not platform
    parented = _todo(store, "Parent")
    upkeep_parented = _job(store, parented, meta={"job_type": "embed_batch"})
    split = work_quest.classify_live_jobs(store)
    assert {r["id"] for r in split.unassigned} == {unassigned, upkeep_parented}
    assert [r["id"] for r in split.platform] == [upkeep]
    assert [r["id"] for r in split.by_quest[quest]] == [served]


def test_work_page_lists_only_the_truly_unassigned_job(
    client: TestClient, store: Store
) -> None:
    """The /work "unassigned jobs" bucket lists only live jobs with no
    quest path; the platform line is its own section (td573299)."""
    unassigned = _job(store, None, "queued", {"job_type": "paper_pull"})
    upkeep = _job(store, None, "running", {"job_type": "derived_drain"})
    quest = _quest(store, "Meta quest")
    meta_job = _job(
        store,
        None,
        "running",
        {"job_type": "quest_tick", "params": {"quest_id": quest}},
    )
    html = client.get("/work").text
    model = work_view.work_model(store)

    assert "Unassigned jobs (1)" in html
    bucket = html.split("Unassigned jobs")[1].split("</section>")[0]
    assert f"jo{unassigned}" in bucket
    assert f"jo{upkeep}" not in bucket
    assert f"jo{meta_job}" not in bucket
    # no todo parent → no "under td" cell on the bucket line
    assert "under td" not in bucket
    assert "Platform (1)" in html and "derived_drain 1" in html
    # the meta-resolved job joins its quest's job lines, display only
    q = next(b for b in model.quests if b.ref_id == quest)
    assert any(f"jo{meta_job}" in ln for ln in q.jobs)
    assert q.running_jobs == 0 and q.queued_jobs == 0


def test_unassigned_line_names_the_todo_parent(store: Store) -> None:
    """An unassigned job under a live todo still says ``under td<id>``."""
    parent = _todo(store, "Parent")
    jid = _job(store, parent, meta={"job_type": "paper_pull"})
    model = work_view.work_model(store)
    assert len(model.unassigned_jobs) == 1
    line = model.unassigned_jobs[0]
    assert f"jo{jid} [running]" in line
    assert f"under td{parent}" in line


def test_status_no_quest_row_equals_the_bucket(store: Store) -> None:
    """The /status "Jobs with no quest" count is len(unassigned_jobs)
    — one classifier feeds both surfaces."""
    _job(store, None, "queued", {"job_type": "paper_pull"})
    _job(store, None, "running", {"job_type": "paper_pull"})
    _job(store, None, "queued", {"job_type": "sandbox_run"})
    _job(store, None, "running", {"job_type": "derived_drain"})
    ctx = _no_quest_jobs(store)
    model = work_view.work_model(store)
    assert ctx["count"] == len(model.unassigned_jobs) == 3
    assert ctx["top"] == [
        {"job_type": "paper_pull", "n": 2},
        {"job_type": "sandbox_run", "n": 1},
    ]


def test_status_health_page_renders_the_block(client: TestClient, store: Store) -> None:
    _job(store, None, "queued", {"job_type": "paper_pull"})
    html = client.get("/status?tab=health").text
    assert "Jobs with no quest" in html
    assert 'href="/work"' in html
    assert "paper_pull 1" in html
