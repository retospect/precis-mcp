"""Executor lane cap + per-class slot reservation (gr603792).

``ssh_node`` shares one per-host running pool between long science
computes (``autocatpath_seed`` et al.) and the factory ops
(``run_slice``, ``land_slice``). ``claim.executor_cap.<executor>``
bounds the pool (code default 8 for ``ssh_node``) and
``claim.reserve.<executor>`` holds named slots inside it
(``{"run_slice": 1, "land_slice": 1}``): a compute may not take the
last reserved slots, and a factory op never waits behind a deep
compute queue — while the science class keeps its (larger) band.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.job import JobHandler
from precis.store import Store
from precis.store.types import Tag
from precis.workers.executors._common import (
    _EXECUTOR_CAP_KEY_PREFIX,
    _RESERVE_KEY_PREFIX,
    claim_executor_jobs,
)


@pytest.fixture
def jobs(hub: Hub) -> JobHandler:
    return JobHandler(hub=hub)


@pytest.fixture
def _factory_advertised(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the advertised-capability map so ``run_slice``/``land_slice``'s
    placement tokens (repo_checkout/claude_bin/git) pass the check-only
    placement gate — orthogonal to the lane-cap math under test."""
    from precis.workers.executors import _common

    real = _common._advertised_by_host

    def _patched(conn: Any) -> dict[str, set[str]]:
        out = real(conn)
        out.setdefault("lane_host_a", set()).update(
            {"repo_checkout", "claude_bin", "git"}
        )
        out.setdefault("lane_host_c", set()).update(
            {"repo_checkout", "claude_bin", "git"}
        )
        out.setdefault("lane_host_d", set()).update(
            {"repo_checkout", "claude_bin", "git"}
        )
        out.setdefault("lane_host_f", set()).update(
            {"repo_checkout", "claude_bin", "git"}
        )
        return out

    monkeypatch.setattr(_common, "_advertised_by_host", _patched)


def _job(
    store: Store,
    *,
    job_type: str,
    executor: str = "ssh_node",
    status: str = "queued",
    lease_host: str | None = None,
) -> int:
    meta: dict[str, object] = {
        "job_type": job_type,
        "executor": executor,
        "params": {},
        "origin": "background",
    }
    if lease_host is not None:
        meta["lease_host"] = lease_host
    ref = store.insert_ref(kind="job", slug=None, title=job_type, meta=meta)
    store.add_tag(
        ref.id, Tag.closed("STATUS", status), set_by="agent", replace_prefix=True
    )
    return ref.id


def _claim(
    store: Store, host: str, *, executor: str = "ssh_node", limit: int = 10
) -> list[int]:
    with store.pool.connection() as conn:
        rows = claim_executor_jobs(
            conn, executor=executor, limit=limit, reserve_host_id=host
        )
        conn.commit()
    return [int(r[0]) for r in rows]


def _fill_running(store: Store, host: str, job_type: str, n: int) -> list[int]:
    """Stamp ``n`` already-``STATUS:running`` rows on ``host`` (the running
    set the cap counts — no claim pass needed)."""
    return [
        _job(store, job_type=job_type, status="running", lease_host=host)
        for _ in range(n)
    ]


# ── code defaults: cap 8, reserve {run_slice: 1, land_slice: 1} ────────


def test_full_compute_occupancy_leaves_reserved_slots_claimable(
    store: Store, _factory_advertised: None
) -> None:
    """The gripe's acceptance shape: six computes running (the shared
    band 8 - 1 - 1 = 6 full) — a queued ``run_slice`` and a queued
    ``land_slice`` still claim into their reserved slots, while another
    queued compute does not."""
    host = "lane_host_a"
    _fill_running(store, host, "autocatpath_seed", 6)
    run = _job(store, job_type="run_slice")
    land = _job(store, job_type="land_slice")
    compute = _job(store, job_type="demo_compute")
    claimed = _claim(store, host, limit=10)
    assert run in claimed
    assert land in claimed
    assert compute not in claimed  # only reserved slots remained


def test_compute_waits_when_only_reserved_slots_remain(store: Store) -> None:
    """A non-reserved type stops at ``cap - reserve_total`` = 6: the 6th
    compute claims, the 7th waits even though the executor pool has two
    rows nominally free."""
    host = "lane_host_b"
    _fill_running(store, host, "autocatpath_seed", 5)
    c6 = _job(store, job_type="demo_compute")
    c7 = _job(store, job_type="demo_compute")
    claimed = _claim(store, host, limit=10)
    assert c6 in claimed
    assert c7 not in claimed


def test_reserved_type_also_uses_shared_headroom(
    store: Store, _factory_advertised: None
) -> None:
    """A reserved type is not confined to its allotment: ``run_slice``
    claims fine while the shared band still has room (it just also owns
    the last slot)."""
    host = "lane_host_c"
    _fill_running(store, host, "autocatpath_seed", 2)
    run = _job(store, job_type="run_slice")
    claimed = _claim(store, host, limit=10)
    assert run in claimed


def test_reserved_types_do_not_steal_each_others_slots(
    store: Store, _factory_advertised: None
) -> None:
    """With 7 running (6 compute + 1 run_slice) only ``land_slice``'s own
    reserved slot remains: a second ``run_slice`` waits, a ``land_slice``
    claims."""
    host = "lane_host_d"
    _fill_running(store, host, "autocatpath_seed", 6)
    _fill_running(store, host, "run_slice", 1)
    land = _job(store, job_type="land_slice")
    run2 = _job(store, job_type="run_slice")
    claimed = _claim(store, host, limit=10)
    assert land in claimed
    assert run2 not in claimed


def test_uncapped_executor_unaffected(store: Store) -> None:
    """No code-default cap → the lane is unbounded as before (the
    reserve map is inert without a cap)."""
    host = "lane_host_e"
    _fill_running(store, host, "demo", 20)
    queued = [_job(store, job_type="demo", executor="ex_free_lane") for _ in range(3)]
    claimed = _claim(store, host, executor="ex_free_lane", limit=10)
    assert sorted(claimed) == sorted(queued)


def test_app_state_cap_and_reserve_override_code_defaults(
    store: Store, _factory_advertised: None
) -> None:
    """``claim.executor_cap``/``claim.reserve`` app_state keys replace
    the code defaults live — cap 3 / reserve {"run_slice": 1} lets two
    computes run, holds one slot for the factory."""
    host = "lane_host_f"
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO app_state (key, value) VALUES (%s, '3'), "
            "(%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (
                f"{_EXECUTOR_CAP_KEY_PREFIX}ssh_node",
                f"{_RESERVE_KEY_PREFIX}ssh_node",
                json.dumps({"run_slice": 1}),
            ),
        )
        conn.commit()
    _fill_running(store, host, "autocatpath_seed", 2)
    run = _job(store, job_type="run_slice")
    compute = _job(store, job_type="demo_compute")
    claimed = _claim(store, host, limit=10)
    assert run in claimed
    assert compute not in claimed  # 2 running = cap 3 - reserve 1 → full for computes
    # cleanup so other tests on the shared store are unaffected
    with store.pool.connection() as conn:
        conn.execute(
            "DELETE FROM app_state WHERE key = ANY(%s)",
            (
                [
                    f"{_EXECUTOR_CAP_KEY_PREFIX}ssh_node",
                    f"{_RESERVE_KEY_PREFIX}ssh_node",
                ],
            ),
        )
        conn.commit()


def test_cap_zero_suspends_executor_claims(store: Store) -> None:
    """cap 0 mirrors ``claim.max_running``'s 0 = suspend — even a
    reserved type claims nothing."""
    host = "lane_host_g"
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO app_state (key, value) VALUES (%s, '0') "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (f"{_EXECUTOR_CAP_KEY_PREFIX}ssh_node",),
        )
        conn.commit()
    run = _job(store, job_type="run_slice")
    compute = _job(store, job_type="demo_compute")
    assert _claim(store, host, limit=10) == []
    assert run and compute  # both stay queued
    with store.pool.connection() as conn:
        conn.execute(
            "DELETE FROM app_state WHERE key = %s",
            (f"{_EXECUTOR_CAP_KEY_PREFIX}ssh_node",),
        )
        conn.commit()


# ── the setting door validates the new key families ────────────────────


def test_setting_door_accepts_lane_knobs(jobs: JobHandler) -> None:
    out = jobs.put(
        mode="setting",
        key="claim.executor_cap.ssh_node",
        value=6,
        reason="test",
    )
    assert "set claim.executor_cap.ssh_node" in out.body
    out = jobs.put(
        mode="setting",
        key="claim.reserve.ssh_node",
        value={"run_slice": 2, "land_slice": 1},
        reason="test",
    )
    assert "set claim.reserve.ssh_node" in out.body
    for key in ("claim.executor_cap.ssh_node", "claim.reserve.ssh_node"):
        jobs.put(mode="setting", key=key, unset=True, reason="test")


def test_setting_door_refuses_bad_reserve(jobs: JobHandler) -> None:
    with pytest.raises(BadInput):
        jobs.put(
            mode="setting",
            key="claim.reserve.ssh_node",
            value={"no_such_type": 1},
            reason="test",
        )
    with pytest.raises(BadInput):
        jobs.put(
            mode="setting",
            key="claim.reserve.no_such_executor",
            value={"run_slice": 1},
            reason="test",
        )
    with pytest.raises(BadInput):
        jobs.put(
            mode="setting",
            key="claim.executor_cap.ssh_node",
            value="many",
            reason="test",
        )
