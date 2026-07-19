"""ssh_node executor — claim → lease → STATUS:running → plugin dispatch.

DB-backed (the claim is real SQL). A monkeypatched job_type stands in
for precis-dft's gpaw_relax so the test exercises the executor flow
without that plugin installed.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.store import Store
from precis.store.types import Tag
from precis.workers.executors import EXECUTOR_PROVIDES, ssh_node
from precis.workers.job_types import JobTypeSpec

pytestmark = pytest.mark.db


# ── helpers ──────────────────────────────────────────────────────


def _mk_job(
    store: Store,
    *,
    executor: str = "ssh_node",
    job_type: str = "fake_relax",
    params: dict[str, Any] | None = None,
    parent_id: int | None = None,
) -> int:
    ref = store.insert_ref(
        kind="job",
        slug=None,
        title="fake relax job",
        meta={"executor": executor, "job_type": job_type, "params": params or {}},
        parent_id=parent_id,
    )
    store.add_tag(ref.id, Tag.parse_strict("STATUS:queued"), set_by="agent")
    return int(ref.id)


def _succeeds(ssh_node_mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Wire a job_type whose dispatch just marks the job succeeded."""
    monkeypatch.setattr(
        ssh_node_mod,
        "get_job_type",
        lambda name: _spec(dispatch=lambda c, s: c.set_status("succeeded")),
    )


def _status(store: Store, ref_id: int) -> str | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT t.value FROM ref_tags rt JOIN tags t USING (tag_id) "
            "WHERE rt.ref_id = %s AND t.namespace = 'STATUS'",
            (ref_id,),
        ).fetchone()
    return row[0] if row else None


def _meta(store: Store, ref_id: int) -> dict[str, Any]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    return dict(row[0] or {})


def _spec(*, dispatch: Any, name: str = "fake_relax") -> JobTypeSpec:
    def _run(*_a: Any, **_k: Any) -> str:
        return "noop"

    return JobTypeSpec(
        name=name,
        params_schema={"type": "object"},
        compatible_executors=frozenset({"ssh_node"}),
        requires=frozenset({"has_gpaw"}),
        description="fake relax for tests",
        run=_run,
        dispatch=dispatch,
    )


# ── tests ────────────────────────────────────────────────────────


def test_provides_registered() -> None:
    assert "ssh_node" in EXECUTOR_PROVIDES
    assert "has_gpaw" in EXECUTOR_PROVIDES["ssh_node"]


def test_lease_seconds_from_wall_seconds() -> None:
    assert ssh_node._lease_seconds({}) == ssh_node._LEASE_FLOOR_S
    big = {"params": {"resources": {"wall_seconds": 100_000}}}
    assert ssh_node._lease_seconds(big) == 100_000 + ssh_node._LEASE_MARGIN_S


def test_claims_and_dispatches_success(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _dispatch(ctx: Any, _spec: Any) -> None:
        ctx.append_chunk("job_summary", "fake relax done")
        ctx.set_status("succeeded")

    monkeypatch.setattr(
        ssh_node, "get_job_type", lambda name: _spec(dispatch=_dispatch)
    )
    rid = _mk_job(store, params={"resources": {"wall_seconds": 1800}})

    result = ssh_node.run_ssh_node_pass(store, limit=2)

    assert result == {"claimed": 1, "ok": 1, "failed": 0}
    assert _status(store, rid) == "succeeded"
    assert "lease_until" in _meta(store, rid)
    blocks = store.list_blocks_for_ref(rid)
    assert any("fake relax done" in b.text for b in blocks)


def test_skips_jobs_for_other_executors(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        ssh_node, "get_job_type", lambda name: _spec(dispatch=lambda c, s: None)
    )
    rid = _mk_job(store, executor="claude_inproc")

    result = ssh_node.run_ssh_node_pass(store, limit=2)

    assert result["claimed"] == 0
    assert _status(store, rid) == "queued"  # untouched


def test_missing_dispatch_records_failure(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A job_type with no dispatch — ssh_node runs plugin dispatchers only.
    monkeypatch.setattr(ssh_node, "get_job_type", lambda name: _spec(dispatch=None))
    rid = _mk_job(store)

    result = ssh_node.run_ssh_node_pass(store, limit=2)

    assert result["claimed"] == 1
    assert _status(store, rid) == "failed"


def test_unknown_job_type_records_failure(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ssh_node, "get_job_type", lambda name: None)
    rid = _mk_job(store, job_type="nope")

    result = ssh_node.run_ssh_node_pass(store, limit=2)

    assert result["claimed"] == 1
    assert _status(store, rid) == "failed"


def test_empty_queue_is_noop(store: Store) -> None:
    assert ssh_node.run_ssh_node_pass(store, limit=2) == {
        "claimed": 0,
        "ok": 0,
        "failed": 0,
    }


# ── node gate (§23 #3) ────────────────────────────────────────────


def test_node_gate_pins_job_to_its_node(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A job pinned via params.target_node is claimed only by that node's
    worker (PRECIS_NODE) — the box that stages to NFS == the box the container
    runs on, so the bind paths line up."""
    _succeeds(ssh_node, monkeypatch)
    rid = _mk_job(store, params={"target_node": "spark"})

    # A node-less worker (PRECIS_NODE unset) must not grab a pinned job.
    monkeypatch.delenv("PRECIS_NODE", raising=False)
    assert ssh_node.run_ssh_node_pass(store, limit=2)["claimed"] == 0
    assert _status(store, rid) == "queued"

    # The wrong node skips it too.
    monkeypatch.setenv("PRECIS_NODE", "melchior")
    assert ssh_node.run_ssh_node_pass(store, limit=2)["claimed"] == 0
    assert _status(store, rid) == "queued"

    # spark's worker claims it.
    monkeypatch.setenv("PRECIS_NODE", "spark")
    assert ssh_node.run_ssh_node_pass(store, limit=2)["claimed"] == 1
    assert _status(store, rid) == "succeeded"


def test_node_gate_unpinned_job_claimed_by_any_node(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An un-pinned job (no target_node) is claimable regardless of node — the
    gate is opt-in, so existing ssh_node job_types are unaffected."""
    _succeeds(ssh_node, monkeypatch)
    rid = _mk_job(store)
    monkeypatch.delenv("PRECIS_NODE", raising=False)
    assert ssh_node.run_ssh_node_pass(store, limit=2)["claimed"] == 1
    assert _status(store, rid) == "succeeded"


# ── parent gate (§23 #3) ──────────────────────────────────────────


def test_parent_gate_skips_paused_project(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A job whose parent todo is halted / asking-user is not claimed — a paused
    project must not burn heavy compute until the owner unblocks it."""
    _succeeds(ssh_node, monkeypatch)
    paused = store.insert_ref(kind="todo", slug=None, title="paused", meta={})
    store.add_tag(paused.id, Tag.parse_strict("halt:manual"), set_by="agent")
    blocked = _mk_job(store, parent_id=paused.id)

    live = store.insert_ref(kind="todo", slug=None, title="live", meta={})
    ok = _mk_job(store, parent_id=live.id)

    ssh_node.run_ssh_node_pass(store, limit=5)
    assert _status(store, blocked) == "queued"  # parent halted → skipped
    assert _status(store, ok) == "succeeded"  # live parent → claimed


# ── crash recovery: reclaim expired-lease STATUS:running jobs ─────


def _mk_running_job(
    store: Store,
    *,
    lease_offset_s: int,
    attempts: int | None = None,
    target_node: str | None = None,
) -> int:
    """A STATUS:running job with a lease ``lease_offset_s`` from now (negative =
    expired) — stands in for a job whose worker died mid-dispatch."""
    params: dict[str, Any] = {}
    if target_node is not None:
        params["target_node"] = target_node
    meta: dict[str, Any] = {
        "executor": "ssh_node",
        "job_type": "fake_relax",
        "params": params,
    }
    if attempts is not None:
        meta["attempts"] = attempts
    ref = store.insert_ref(
        kind="job", slug=None, title="orphaned running job", meta=meta
    )
    store.add_tag(ref.id, Tag.parse_strict("STATUS:running"), set_by="agent")
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || jsonb_build_object("
            "  'lease_until', (now() + make_interval(secs => %s))::text"
            ") WHERE ref_id = %s",
            (lease_offset_s, int(ref.id)),
        )
        conn.commit()
    return int(ref.id)


def test_reclaims_expired_lease_running_job(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A STATUS:running job whose lease has expired (worker died mid-dispatch)
    is stolen, re-run, and its attempt counter bumped."""
    _succeeds(ssh_node, monkeypatch)
    rid = _mk_running_job(store, lease_offset_s=-60, attempts=1)

    result = ssh_node.run_ssh_node_pass(store, limit=2)

    assert result == {"claimed": 1, "ok": 1, "failed": 0}
    assert _status(store, rid) == "succeeded"
    assert _meta(store, rid)["attempts"] == 2  # bumped on the steal


def test_does_not_steal_live_lease_running_job(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A STATUS:running job with a still-valid lease is left alone — only a
    provably-dead (expired-lease) holder is stolen."""
    _succeeds(ssh_node, monkeypatch)
    rid = _mk_running_job(store, lease_offset_s=3600, attempts=1)

    result = ssh_node.run_ssh_node_pass(store, limit=2)

    assert result["claimed"] == 0
    assert _status(store, rid) == "running"  # untouched


def test_poison_guard_fails_past_max_attempts(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A job re-claimed past the attempt cap is failed (bubbled), not stolen
    yet again — a crash-loop can't burn the worker forever."""
    dispatched = {"n": 0}

    def _dispatch(ctx: Any, _s: Any) -> None:
        dispatched["n"] += 1
        ctx.set_status("succeeded")

    monkeypatch.setattr(
        ssh_node, "get_job_type", lambda name: _spec(dispatch=_dispatch)
    )
    rid = _mk_running_job(store, lease_offset_s=-60, attempts=ssh_node._MAX_ATTEMPTS)

    result = ssh_node.run_ssh_node_pass(store, limit=2)

    assert result == {"claimed": 1, "ok": 0, "failed": 1}
    assert _status(store, rid) == "failed"
    assert dispatched["n"] == 0  # never dispatched
