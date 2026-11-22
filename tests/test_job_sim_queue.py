"""The read-only quest simulation queue on the job MCP kind."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.job import JobHandler
from precis.runtime import PrecisRuntime
from precis.store import Store, Tag

pytestmark = pytest.mark.db


def test_quest_sim_queue(
    store: Store, runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_SUSPENDED_JOB_TYPES", "struct_relax")
    quest = store.insert_ref(kind="quest", slug=None, title="test quest")
    candidate = store.insert_ref(kind="structure", slug="candidate", title="candidate")
    store.add_link(src_ref_id=candidate.id, dst_ref_id=quest.id, relation="serves")
    aggregate = store.insert_ref(
        kind="todo", slug=None, title="aggregate", parent_id=candidate.id
    )
    seed = store.insert_ref(
        kind="todo", slug=None, title="seed", parent_id=aggregate.id
    )
    store.add_tag(seed.id, Tag.open("waiting-for:review"), set_by="system")

    def add_job(job_type: str, status: str, parent_id: int | None) -> int:
        job = store.insert_ref(
            kind="job",
            slug=None,
            title=job_type,
            parent_id=parent_id,
            meta={"job_type": job_type, "executor": "ssh_node"},
        )
        store.add_tag(job.id, Tag.closed("STATUS", status), set_by="system")
        return job.id

    relax = add_job("struct_relax", "queued", candidate.id)
    blocked = add_job("autocatpath_seed", "queued", seed.id)
    waiting = add_job("autocatpath_seed", "waiting_time", seed.id)
    running = add_job("autocatpath_aggregate", "running", aggregate.id)
    slow = add_job("autocatpath_seed", "yielded", aggregate.id)
    terminal = add_job("autocatpath_seed", "succeeded", seed.id)
    unrelated = add_job("autocatpath_seed", "queued", None)
    non_sim = add_job("quest_tick", "queued", quest.id)
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE ref_tags SET created_at = %s WHERE ref_id = %s",
            (datetime.now(UTC) - timedelta(hours=2, minutes=5, seconds=30), relax),
        )

    args = {"kind": "job", "under": f"qu{quest.id}", "view": "sim-queue"}
    body = runtime_with_store.dispatch("search", args)
    assert f"job:{relax} struct_relax [queued] queued 2h" in body
    assert "type suspended in server configuration" in body
    assert f"job:{blocked} autocatpath_seed [queued]" in body
    assert "parent todo is paused" in body
    assert f"job:{waiting} autocatpath_seed [waiting_time]" in body
    assert f"job:{running} autocatpath_aggregate [running]" in body
    assert f"job:{slow} autocatpath_seed [yielded]" in body
    assert "no recorded blocker" in body
    for job_id in (terminal, unrelated, non_sim):
        assert f"job:{job_id} " not in body

    first = runtime_with_store.dispatch("search", {**args, "page_size": 1})
    assert f"job:{relax} " in first
    assert f"job:{blocked} " not in first
    assert f"under='qu{quest.id}', page_size=1, args={{'page': 2}}" in first
    second = runtime_with_store.dispatch(
        "search", {**args, "page_size": 1, "__extras__": {"page": 2}}
    )
    assert f"job:{blocked} " in second


def test_empty_queue_reports_disabled_dispatch(store: Store, hub: Hub) -> None:
    quest = store.insert_ref(
        kind="quest", slug=None, title="off", meta={"compute_lane": "off"}
    )
    body = JobHandler(hub=hub).search(view="sim-queue", under=quest.id).body
    assert "Quest simulation dispatch is off" in body
    assert "No active simulation jobs" in body


def test_sim_queue_requires_a_quest(store: Store, hub: Hub) -> None:
    todo = store.insert_ref(kind="todo", slug=None, title="todo")
    jobs = JobHandler(hub=hub)
    with pytest.raises(BadInput, match="requires under"):
        jobs.search(view="sim-queue")
    with pytest.raises(BadInput, match="requires a quest"):
        jobs.search(view="sim-queue", under=todo.id)
