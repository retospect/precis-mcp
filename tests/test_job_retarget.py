"""``put(kind='job', id=N, mode='retarget')``: move a queued job's
``params.target_node`` pin to another node (gr496506)."""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.job import JobHandler
from precis.store import Store
from precis.store.types import Tag


@pytest.fixture
def jobs(hub: Hub) -> JobHandler:
    return JobHandler(hub=hub)


def _queued_pinned(
    store: Store,
    *,
    node: str | None = "node-a",
    status: str = "queued",
    requires: dict[str, int] | None = None,
) -> int:
    params: dict[str, Any] = {"target_node": node} if node else {}
    meta: dict[str, Any] = {
        "job_type": "fake",
        "executor": "ssh_node",
        "params": params,
    }
    if requires:
        meta["requires"] = requires
    ref = store.insert_ref(kind="job", slug=None, title="pinned", meta=meta)
    store.add_tag(ref.id, Tag.closed("STATUS", status), set_by="system")
    return ref.id


def _params(store: Store, job_id: int) -> dict[str, Any]:
    ref = store.fetch_refs_by_ids({job_id})[job_id]
    return dict((ref.meta or {}).get("params") or {})


def _retarget(jobs: JobHandler, job_id: int, node: str = "node-b", **kw: Any) -> str:
    kw.setdefault("text", "node-a is saturated; node-b is idle")
    return jobs.put(id=job_id, mode="retarget", params={"target_node": node}, **kw).body


def test_retarget_moves_the_pin_and_records_who_and_why(
    jobs: JobHandler, store: Store
) -> None:
    job_id = _queued_pinned(store)

    body = _retarget(jobs, job_id)

    assert "node-a -> node-b" in body
    assert _params(store, job_id)["target_node"] == "node-b"
    [ev] = store.events_for(job_id, source="job-retarget")
    assert ev.payload["from"] == "node-a"
    assert ev.payload["to"] == "node-b"
    assert ev.payload["reason"] == "node-a is saturated; node-b is idle"
    assert ev.payload["by"]


def test_retarget_keeps_other_params(jobs: JobHandler, store: Store) -> None:
    job_id = _queued_pinned(store)
    store.stamp_ref_meta(job_id, {"params": {"target_node": "node-a", "seed": 7}})

    _retarget(jobs, job_id)

    assert _params(store, job_id) == {"target_node": "node-b", "seed": 7}


@pytest.mark.parametrize("status", ["running", "failed", "succeeded", "cancelled"])
def test_retarget_refuses_a_job_that_is_not_queued(
    jobs: JobHandler, store: Store, status: str
) -> None:
    job_id = _queued_pinned(store, status=status)

    with pytest.raises(BadInput, match="only a queued job"):
        _retarget(jobs, job_id)

    assert _params(store, job_id)["target_node"] == "node-a"
    assert store.events_for(job_id, source="job-retarget") == []


def test_retarget_requires_a_reason(jobs: JobHandler, store: Store) -> None:
    job_id = _queued_pinned(store)

    with pytest.raises(BadInput, match="requires text="):
        jobs.put(id=job_id, mode="retarget", params={"target_node": "node-b"})

    assert _params(store, job_id)["target_node"] == "node-a"


def test_retarget_refuses_an_unpinned_job(jobs: JobHandler, store: Store) -> None:
    job_id = _queued_pinned(store, node=None)

    with pytest.raises(BadInput, match="not pinned"):
        _retarget(jobs, job_id)


def test_retarget_refuses_the_current_node(jobs: JobHandler, store: Store) -> None:
    job_id = _queued_pinned(store)

    with pytest.raises(BadInput, match="already pinned"):
        _retarget(jobs, job_id, node="node-a")


def test_retarget_refuses_unknown_params_keys(jobs: JobHandler, store: Store) -> None:
    job_id = _queued_pinned(store)

    with pytest.raises(BadInput, match="exactly params"):
        jobs.put(
            id=job_id,
            mode="retarget",
            params={"target_node": "node-b", "seed": 1},
            text="why",
        )


def test_retarget_refuses_a_node_with_a_retired_lane(
    jobs: JobHandler, store: Store
) -> None:
    job_id = _queued_pinned(store)
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO service_config (host, service, prio) "
            "VALUES ('node-b', 'job_ssh_node', 0) "
            "ON CONFLICT (host, service) DO UPDATE SET prio = 0"
        )
        conn.commit()

    with pytest.raises(BadInput, match="cannot take job"):
        _retarget(jobs, job_id)

    assert _params(store, job_id)["target_node"] == "node-a"


def test_retarget_checks_the_nodes_capability(jobs: JobHandler, store: Store) -> None:
    job_id = _queued_pinned(store, requires={"gpu": 1})
    store.sync_host_resource_slots("node-a", {"gpu": 1})

    with pytest.raises(BadInput, match=r"does not advertise \['gpu'\]"):
        _retarget(jobs, job_id)
    assert _params(store, job_id)["target_node"] == "node-a"

    store.sync_host_resource_slots("node-b", {"gpu": 1})
    assert "node-a -> node-b" in _retarget(jobs, job_id)
    assert _params(store, job_id)["target_node"] == "node-b"


def test_retarget_needs_an_id(jobs: JobHandler) -> None:
    with pytest.raises(BadInput, match="requires id="):
        jobs.put(mode="retarget", params={"target_node": "node-b"}, text="why")
