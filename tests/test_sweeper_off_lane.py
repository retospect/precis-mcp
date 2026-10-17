"""Off-lane pinned reap (gr493226).

A job pinned (``meta.params.target_node``) to a node that has stopped
running its executor's drain pass is unclaimable — every claim path
node-gates on the pin — yet the host still heartbeats, so the dead-node
reaps abstain. jo489305-307 sat ~28h this way after castor went
heartbeat-only. The sweeper must fail such jobs as infra with a clear
error and raise ``sweeper/off-lane-pin:<node>/<executor>``.

Hosts are uuid-tagged so shared-DB liveness rows from sibling tests can't
leak in.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from precis.dispatch import Hub
from precis.handlers.todo import TodoHandler
from precis.store import Store
from precis.store.types import Tag
from precis.workers.sweeper import run_sweeper_pass

_TAG = "reaped:off-lane-pin"


@pytest.fixture
def handler(hub: Hub) -> TodoHandler:
    return TodoHandler(hub=hub)


def _id_of(body: str) -> int:
    return int(body.split("id=")[1].split()[0].rstrip(",.()"))


def _pass_log(store: Store, host: str, handler: str, *, age_minutes: float) -> None:
    """One per-cycle ``worker_logs`` row, as ``runner.run_loop`` writes it."""
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO worker_logs (ts, host, process, level, message, payload) "
            "VALUES (now() - make_interval(secs => %s), %s, "
            "'precis-worker-compute', 'INFO', 'cycle', %s::jsonb)",
            (age_minutes * 60, host, json.dumps({"handler": handler, "claimed": 0})),
        )
        conn.commit()


def _heartbeat(store: Store, host: str) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO host_heartbeat (host, ts) VALUES (%s, now()) "
            "ON CONFLICT (host) DO UPDATE SET ts = excluded.ts",
            (host,),
        )
        conn.commit()


def _mint_job(
    store: Store,
    parent_id: int,
    *,
    status: str,
    node: str,
    executor: str = "ssh_node",
    age_seconds: float = 1200,
    meta_extra: dict | None = None,
) -> int:
    meta: dict = {
        "job_type": "autocatpath_seed",
        "executor": executor,
        "params": {"target_node": node},
        **(meta_extra or {}),
    }
    job = store.insert_ref(
        kind="job", slug=None, title="seed", meta=meta, parent_id=parent_id
    )
    store.add_tag(
        job.id, Tag.closed("STATUS", status), set_by="system", replace_prefix=True
    )
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET created_at = now() - make_interval(secs => %s) "
            "WHERE ref_id = %s",
            (age_seconds, job.id),
        )
        conn.commit()
    return int(job.id)


def _left_lane_node(store: Store) -> str:
    """castor's shape: host heartbeats, ran job_ssh_node until 6h ago."""
    node = f"offlane-{uuid4().hex[:8]}"
    _heartbeat(store, node)
    _pass_log(store, node, "job_ssh_node", age_minutes=6 * 60)
    return node


def _tags(store: Store, ref_id: int) -> set[str]:
    return {str(t) for t in store.tags_for(ref_id)}


def _meta(store: Store, ref_id: int) -> dict:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    assert row is not None
    return dict(row[0] or {})


def _alert(store: Store, fingerprint: str) -> tuple[bool, dict] | None:
    """``(is_open, meta)`` of the newest alert with ``fingerprint``."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT resolved_at IS NULL, meta FROM refs WHERE kind = 'alert' "
            "AND retired_at IS NULL AND alert_source = 'sweeper' "
            "AND fingerprint = %s ORDER BY ref_id DESC LIMIT 1",
            (fingerprint,),
        ).fetchone()
    return None if row is None else (bool(row[0]), dict(row[1] or {}))


def test_queued_job_pinned_to_node_that_left_lane_fails_loudly(
    handler: TodoHandler, store: Store
) -> None:
    node = _left_lane_node(store)
    rid = _id_of(handler.put(text="quest parent").body)
    job_id = _mint_job(store, rid, status="queued", node=node)

    run_sweeper_pass(store, limit=10)

    tags = _tags(store, job_id)
    assert "STATUS:failed" in tags
    assert _TAG in tags
    meta = _meta(store, job_id)
    assert meta["failure_class"] == "infra"
    assert node in meta["error"]
    assert "job_ssh_node" in meta["error"]
    assert any(e.event == "job-reaped" for e in store.events_for(job_id))
    # Infra-class: the parent gets the bounded retry, not an immediate latch.
    assert f"child-failed:{job_id}" not in _tags(store, rid)
    alert = _alert(store, f"off-lane-pin:{node}/ssh_node")
    assert alert is not None
    is_open, alert_meta = alert
    assert is_open
    assert alert_meta["rule_id"] == "sweeper/off-lane-pin:{host}/{executor}"
    assert str(job_id) in alert_meta["detail"]


def test_running_job_with_expired_lease_on_off_lane_node_is_reaped(
    handler: TodoHandler, store: Store
) -> None:
    """jo489305's shape: claimed, then the node's compute worker went away —
    lease expired, nothing left on that node to reclaim it."""
    node = _left_lane_node(store)
    rid = _id_of(handler.put(text="quest parent").body)
    job_id = _mint_job(
        store,
        rid,
        status="running",
        node=node,
        meta_extra={
            "lease_host": node,
            "lease_until": "2000-01-01T00:00:00+00:00",
            "compute_handle": {"pid": 27681},
        },
    )

    run_sweeper_pass(store, limit=10)

    tags = _tags(store, job_id)
    assert "STATUS:failed" in tags
    assert _TAG in tags
    assert "compute_handle" in _meta(store, job_id)["error"]


def test_node_still_in_lane_is_left_alone(handler: TodoHandler, store: Store) -> None:
    node = _left_lane_node(store)
    _pass_log(store, node, "job_ssh_node", age_minutes=1)
    rid = _id_of(handler.put(text="parent").body)
    job_id = _mint_job(store, rid, status="queued", node=node)

    run_sweeper_pass(store, limit=10)

    assert "STATUS:queued" in _tags(store, job_id)


def test_node_busy_with_a_live_lease_is_left_alone(
    handler: TodoHandler, store: Store
) -> None:
    """A legacy blocking dispatch stops the node's whole rotation, so its
    pass falls silent while a job there holds a live lease: busy, not gone."""
    node = _left_lane_node(store)
    rid = _id_of(handler.put(text="parent").body)
    _mint_job(
        store,
        rid,
        status="running",
        node=node,
        meta_extra={
            "lease_host": node,
            "lease_process": "precis-worker-compute",
            "lease_until": "2999-01-01T00:00:00+00:00",
        },
    )
    job_id = _mint_job(store, rid, status="queued", node=node)

    run_sweeper_pass(store, limit=10)

    assert "STATUS:queued" in _tags(store, job_id)


def test_node_that_never_ran_the_pass_is_left_alone(
    handler: TodoHandler, store: Store
) -> None:
    """No history means the node never proved it was in the lane; this arm
    reaps nodes that *left*, it doesn't guess about ones yet to arrive."""
    node = f"never-{uuid4().hex[:8]}"
    _heartbeat(store, node)
    rid = _id_of(handler.put(text="parent").body)
    job_id = _mint_job(store, rid, status="queued", node=node)

    run_sweeper_pass(store, limit=10)

    assert "STATUS:queued" in _tags(store, job_id)


def test_other_executors_pass_does_not_count(
    handler: TodoHandler, store: Store
) -> None:
    """castor's sandbox worker kept nothing for ssh_node: lane membership is
    per executor, so a fresh job_claude_docker row doesn't keep an ssh_node
    pin alive."""
    node = _left_lane_node(store)
    _pass_log(store, node, "job_claude_docker", age_minutes=1)
    rid = _id_of(handler.put(text="parent").body)
    job_id = _mint_job(store, rid, status="queued", node=node)

    run_sweeper_pass(store, limit=10)

    assert _TAG in _tags(store, job_id)


def test_fresh_queued_job_within_grace_is_left_alone(
    handler: TodoHandler, store: Store
) -> None:
    node = _left_lane_node(store)
    rid = _id_of(handler.put(text="parent").body)
    job_id = _mint_job(store, rid, status="queued", node=node, age_seconds=60)

    run_sweeper_pass(store, limit=10)

    assert "STATUS:queued" in _tags(store, job_id)


def test_alert_resolves_when_node_rejoins_lane(
    handler: TodoHandler, store: Store
) -> None:
    node = _left_lane_node(store)
    rid = _id_of(handler.put(text="parent").body)
    _mint_job(store, rid, status="queued", node=node)
    run_sweeper_pass(store, limit=10)
    fingerprint = f"off-lane-pin:{node}/ssh_node"
    alert = _alert(store, fingerprint)
    assert alert is not None and alert[0]

    _pass_log(store, node, "job_ssh_node", age_minutes=0)
    run_sweeper_pass(store, limit=10)

    alert = _alert(store, fingerprint)
    assert alert is not None and not alert[0]


def test_every_executor_maps_to_a_registered_drain_pass() -> None:
    from precis.workers.executors import EXECUTOR_PROVIDES
    from precis.workers.registry import SERVICES
    from precis.workers.sweeper import executor_pass_handler

    handlers = {spec.log_handler for spec in SERVICES}
    missing = {
        ex for ex in EXECUTOR_PROVIDES if executor_pass_handler(ex) not in handlers
    }
    assert not missing


def test_silence_env_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    from precis.workers.sweeper import _off_lane_silence_s

    monkeypatch.delenv("PRECIS_OFF_LANE_PIN_SILENCE_S", raising=False)
    assert _off_lane_silence_s() == 4 * 3600
    monkeypatch.setenv("PRECIS_OFF_LANE_PIN_SILENCE_S", "7200")
    assert _off_lane_silence_s() == 7200
    monkeypatch.setenv("PRECIS_OFF_LANE_PIN_SILENCE_S", "5")
    assert _off_lane_silence_s() == 600
    monkeypatch.setenv("PRECIS_OFF_LANE_PIN_SILENCE_S", "bogus")
    assert _off_lane_silence_s() == 4 * 3600
