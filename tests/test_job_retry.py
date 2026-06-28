"""Tests for the job retry verb (``put(kind='job', mode='retry')``).

A failed job bubbles ``child-failed:<job_id>`` onto its parent todo,
which excludes the parent from the doable rotation. Retry clears that
bubble (optionally swapping the parent's ``LLM:<model>`` tag) so the
dispatch worker re-mints a fresh attempt.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.job import JobHandler
from precis.handlers.todo import TodoHandler
from precis.store import Store
from precis.store.types import Tag
from precis.workers.dispatch import run_dispatch_pass
from tests.conftest import id_of


@pytest.fixture
def todos(hub: Hub) -> TodoHandler:
    return TodoHandler(hub=hub)


@pytest.fixture
def jobs(hub: Hub) -> JobHandler:
    return JobHandler(hub=hub)


def _child_jobs(store: Store, parent_id: int) -> list[dict]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, meta FROM refs "
            "WHERE parent_id = %s AND kind = 'job' AND deleted_at IS NULL "
            "ORDER BY ref_id",
            (parent_id,),
        ).fetchall()
    return [{"id": int(r[0]), "meta": r[1]} for r in rows]


def _parent_tags(store: Store, parent_id: int) -> set[str]:
    return {str(t) for t in store.tags_for(parent_id)}


def _fail_first_job(store: Store, jobs: JobHandler, parent_id: int) -> int:
    """Dispatch a tick under ``parent_id`` and mark it failed (bubbles)."""
    run_dispatch_pass(store)
    children = _child_jobs(store, parent_id)
    assert len(children) == 1, "expected exactly one minted tick"
    job_id = children[0]["id"]
    # Tagging STATUS:failed through the handler fires the failure-bubble.
    jobs.tag(id=job_id, add=["STATUS:failed"])
    assert f"child-failed:{job_id}" in _parent_tags(store, parent_id)
    return job_id


def test_retry_clears_bubble_and_redispatches(
    todos: TodoHandler, jobs: JobHandler, store: Store
) -> None:
    """Retry removes child-failed so dispatch mints a fresh tick."""
    rid = id_of(todos.put(text="planner brief", tags=["LLM:opus"]).body)
    job_id = _fail_first_job(store, jobs, rid)

    # While bubbled, dispatch refuses to re-mint.
    run_dispatch_pass(store)
    assert len(_child_jobs(store, rid)) == 1

    resp = jobs.put(id=job_id, mode="retry")
    assert f"child-failed:{job_id}" not in _parent_tags(store, rid)
    assert "retry queued" in resp.body
    # Failed job retained for forensics.
    assert job_id in {c["id"] for c in _child_jobs(store, rid)}

    # Now dispatch re-mints a second tick.
    run_dispatch_pass(store)
    assert len(_child_jobs(store, rid)) == 2


def test_retry_with_model_swaps_llm_tag(
    todos: TodoHandler, jobs: JobHandler, store: Store
) -> None:
    """model= rewrites the parent's LLM:<model> tag before re-dispatch."""
    rid = id_of(todos.put(text="planner brief", tags=["LLM:opus"]).body)
    job_id = _fail_first_job(store, jobs, rid)

    jobs.put(id=job_id, mode="retry", model="sonnet")
    tags = _parent_tags(store, rid)
    assert "LLM:sonnet" in tags
    assert "LLM:opus" not in tags
    assert f"child-failed:{job_id}" not in tags

    run_dispatch_pass(store)
    fresh = [c for c in _child_jobs(store, rid) if c["id"] != job_id]
    assert len(fresh) == 1
    assert fresh[0]["meta"]["params"]["model"] == "sonnet"


def test_retry_rejects_non_terminal_job(
    todos: TodoHandler, jobs: JobHandler, store: Store
) -> None:
    """A queued/running job can't be retried — it hasn't failed yet."""
    rid = id_of(todos.put(text="planner brief", tags=["LLM:opus"]).body)
    run_dispatch_pass(store)
    job_id = _child_jobs(store, rid)[0]["id"]  # STATUS:queued
    with pytest.raises(BadInput, match="only a failed"):
        jobs.put(id=job_id, mode="retry")


def test_retry_model_requires_llm_parent(
    todos: TodoHandler, jobs: JobHandler, store: Store
) -> None:
    """model= is rejected when the parent isn't an LLM-planner todo."""
    rid = id_of(todos.put(text="plain todo").body)
    job = store.insert_ref(
        kind="job", slug=None, title="manual", meta={}, parent_id=rid
    )
    jobs.tag(id=job.id, add=["STATUS:failed"])
    with pytest.raises(BadInput, match="no LLM"):
        jobs.put(id=job.id, mode="retry", model="opus")
    # Bubble still present — the rejected retry made no partial write.
    assert f"child-failed:{job.id}" in _parent_tags(store, rid)
    # Without model=, the same retry succeeds (clears the bubble).
    jobs.put(id=job.id, mode="retry")
    assert f"child-failed:{job.id}" not in _parent_tags(store, rid)


def test_retry_rejects_orphan_job(jobs: JobHandler, store: Store) -> None:
    """A parentless legacy job has nothing to re-dispatch from."""
    job = store.insert_ref(
        kind="job", slug=None, title="orphan", meta={}, parent_id=None
    )
    store.add_tag(job.id, Tag.closed("STATUS", "failed"), set_by="system")
    with pytest.raises(BadInput, match="no todo parent"):
        jobs.put(id=job.id, mode="retry")


def test_retry_rejects_bad_model(
    todos: TodoHandler, jobs: JobHandler, store: Store
) -> None:
    """An out-of-vocab model is rejected (closed-vocab LLM tag)."""
    rid = id_of(todos.put(text="planner brief", tags=["LLM:opus"]).body)
    job_id = _fail_first_job(store, jobs, rid)
    with pytest.raises(BadInput):
        jobs.put(id=job_id, mode="retry", model="opos")
    # Failed retry left the bubble + original model intact.
    tags = _parent_tags(store, rid)
    assert "LLM:opus" in tags
    assert f"child-failed:{job_id}" in tags
