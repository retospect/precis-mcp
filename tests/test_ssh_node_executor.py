"""ssh_node executor — claim → lease → STATUS:running → plugin dispatch.

DB-backed (the claim is real SQL). A monkeypatched job_type stands in
for precis-dft's gpaw_relax so the test exercises the executor flow
without that plugin installed.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest

from precis.store import Store
from precis.store.types import Tag
from precis.workers.executors import EXECUTOR_PROVIDES, ssh_node
from precis.workers.executors._common import claim_executor_jobs
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
    prio: int | None = None,
) -> int:
    ref = store.insert_ref(
        kind="job",
        slug=None,
        title="fake relax job",
        meta={"executor": executor, "job_type": job_type, "params": params or {}},
        parent_id=parent_id,
        prio=prio,
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
    assert row is not None
    return dict(row[0] or {})


def _spec(
    *,
    dispatch: Any = None,
    submit: Any = None,
    poll: Any = None,
    name: str = "fake_relax",
) -> JobTypeSpec:
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
        submit=submit,
        poll=poll,
    )


# ── tests ────────────────────────────────────────────────────────


def test_provides_registered() -> None:
    assert "ssh_node" in EXECUTOR_PROVIDES
    assert "has_gpaw" in EXECUTOR_PROVIDES["ssh_node"]


def test_lease_seconds_from_wall_seconds() -> None:
    assert ssh_node._lease_seconds({}) == ssh_node._LEASE_FLOOR_S
    big = {"params": {"resources": {"wall_seconds": 100_000}}}
    assert ssh_node._lease_seconds(big) == 100_000 + ssh_node._LEASE_MARGIN_S


def test_type_cap_bounds_dispatch_and_initial_lease() -> None:
    spec = replace(_spec(dispatch=lambda _ctx, _spec: None), wall_seconds=lambda: 14400)
    assert ssh_node._wall_bound_seconds({}, spec) == 14400 + ssh_node._LEASE_MARGIN_S
    assert ssh_node._lease_seconds({}, spec) == ssh_node._wall_bound_seconds({}, spec)
    with_resources = {"params": {"resources": {"wall_seconds": 20_000}}}
    assert ssh_node._wall_bound_seconds(with_resources, spec) == (
        20_000 + ssh_node._LEASE_MARGIN_S
    )


def test_claim_uses_type_cap_for_lease(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = replace(
        _spec(dispatch=lambda ctx, _spec: ctx.set_status("succeeded")),
        wall_seconds=lambda: 14400,
    )
    monkeypatch.setattr(ssh_node, "get_job_type", lambda _name: spec)
    rid = _mk_job(store)

    assert ssh_node.run_ssh_node_pass(store, limit=1) == {
        "claimed": 1,
        "ok": 1,
        "failed": 0,
    }
    with store.pool.connection() as conn:
        remaining = conn.execute(
            "SELECT (meta->>'lease_until')::timestamptz - now() "
            "FROM refs WHERE ref_id = %s",
            (rid,),
        ).fetchone()
    assert remaining is not None
    assert remaining[0].total_seconds() > 14400 + ssh_node._LEASE_MARGIN_S - 30


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
    blocks = store.chunks.list_chunks_for_ref(rid)
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
    lease_boot_id: str | None = None,
    lease_process: str | None = None,
    lease_host: str | None = None,
    compute_handle: Any = None,
    submit_started_at: float | None = None,
) -> int:
    """A STATUS:running job with a lease ``lease_offset_s`` from now (negative =
    expired) — stands in for a job whose worker died mid-dispatch. The
    ``lease_boot_id``/``lease_process``/``lease_host`` trio (the
    lease-identity stamp) stands in for a PRIOR claim's stamp — the
    generation that (allegedly) died. ``compute_handle`` (the detached
    submit/poll protocol) stands
    in for a prior generation's successful ``spec.submit()`` — a detached
    job's compute may still be alive."""
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
    if lease_boot_id is not None:
        meta["lease_boot_id"] = lease_boot_id
    if lease_process is not None:
        meta["lease_process"] = lease_process
    if lease_host is not None:
        meta["lease_host"] = lease_host
    if compute_handle is not None:
        meta["compute_handle"] = compute_handle
    if submit_started_at is not None:
        meta["submit_started_at"] = submit_started_at
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
    # crash-loop guard is an INFRA failure (the worker died mid-dispatch, not
    # a physical verdict) — a struct_relax-style harvest must be able to tell.
    assert _meta(store, rid)["failure_class"] == "infra"


# ── Worker boot epoch: epoch-aware reclaim (the lease-epoch reclaim arm) ──


def test_epoch_mismatch_reclaims_before_lease_expiry(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A running job whose claiming generation is provably replaced (the
    host's CURRENT advertised boot_id for that process differs) is stolen
    on the FIRST claim pass — even while its lease is still hours away
    from expiring. This is the whole point: recovery in one pass, not one
    lease-floor."""
    _succeeds(ssh_node, monkeypatch)
    store.record_heartbeat(
        "spark-epoch-1", meta={"boot_ids": {"ssh_node": "new-generation"}}
    )
    rid = _mk_running_job(
        store,
        lease_offset_s=3600,  # far future — expiry arm alone would NOT fire
        attempts=1,
        lease_boot_id="dead-generation",
        lease_process="ssh_node",
        lease_host="spark-epoch-1",
    )

    result = ssh_node.run_ssh_node_pass(store, limit=2)

    assert result == {"claimed": 1, "ok": 1, "failed": 0}
    assert _status(store, rid) == "succeeded"


def test_live_holder_unexpired_lease_never_stolen_by_epoch_arm(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A running job whose stamped lease_boot_id STILL matches the host's
    currently-advertised boot_id (a genuinely live holder) is left alone by
    the epoch arm, regardless of lease age — same guarantee as the plain
    expiry arm, now proven against the epoch mechanism too."""
    _succeeds(ssh_node, monkeypatch)
    store.record_heartbeat(
        "spark-epoch-2", meta={"boot_ids": {"ssh_node": "same-generation"}}
    )
    rid = _mk_running_job(
        store,
        lease_offset_s=3600,
        attempts=1,
        lease_boot_id="same-generation",
        lease_process="ssh_node",
        lease_host="spark-epoch-2",
    )

    result = ssh_node.run_ssh_node_pass(store, limit=2)

    assert result["claimed"] == 0
    assert _status(store, rid) == "running"  # untouched


def test_live_holder_expired_lease_still_reclaimed_by_expiry_arm(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A same-generation holder (boot_id matches) whose lease has genuinely
    expired (a hang, not a restart) is still reclaimed — the epoch arm's
    presence doesn't disable the pre-existing expiry arm."""
    _succeeds(ssh_node, monkeypatch)
    store.record_heartbeat(
        "spark-epoch-3", meta={"boot_ids": {"ssh_node": "same-generation"}}
    )
    rid = _mk_running_job(
        store,
        lease_offset_s=-60,
        attempts=1,
        lease_boot_id="same-generation",
        lease_process="ssh_node",
        lease_host="spark-epoch-3",
    )

    result = ssh_node.run_ssh_node_pass(store, limit=2)

    assert result == {"claimed": 1, "ok": 1, "failed": 0}
    assert _status(store, rid) == "succeeded"


def test_no_advertisement_at_all_reclaims_via_epoch_arm(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``host_heartbeat`` row for the stamped ``lease_host`` at all (the
    host is provably gone, or never reported one for this process) counts
    as "not currently advertised" — same as an explicit mismatch."""
    _succeeds(ssh_node, monkeypatch)
    rid = _mk_running_job(
        store,
        lease_offset_s=3600,
        attempts=1,
        lease_boot_id="dead-generation",
        lease_process="ssh_node",
        lease_host="spark-never-reported",
    )

    result = ssh_node.run_ssh_node_pass(store, limit=2)

    assert result == {"claimed": 1, "ok": 1, "failed": 0}
    assert _status(store, rid) == "succeeded"


def test_claim_stamps_this_workers_lease_identity(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every claim (fresh-queued here) stamps THIS worker's own boot_id /
    process / host onto the row — the uniform stamp every executor gets,
    win or lose the epoch arm ever firing on it."""
    from precis.workers import heartbeat as _heartbeat_mod

    _succeeds(ssh_node, monkeypatch)
    monkeypatch.setattr(_heartbeat_mod, "_boot_id", "this-workers-boot-id")
    monkeypatch.setattr(_heartbeat_mod, "_boot_process", "ssh_node")
    monkeypatch.setenv("PRECIS_PROCESS", "ssh_node")
    monkeypatch.setenv("PRECIS_HOST_NAME", "spark-claimer")
    rid = _mk_job(store, params={"resources": {"wall_seconds": 1800}})

    ssh_node.run_ssh_node_pass(store, limit=2)

    meta = _meta(store, rid)
    assert meta["lease_boot_id"] == "this-workers-boot-id"
    assert meta["lease_process"] == "ssh_node"
    assert meta["lease_host"] == "spark-claimer"
    # gr346951: every claim also records which build did the work.
    from precis.handlers.skill import code_stamp

    assert meta["lease_code"] == code_stamp()


def test_null_lease_boot_id_falls_back_to_expiry_only(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row with no stamped lease_boot_id at all (pre-epoch / a caller that
    never minted one) is unaffected by the epoch arm — expiry is still the
    only signal, exactly today's behaviour."""
    _succeeds(ssh_node, monkeypatch)
    rid = _mk_running_job(store, lease_offset_s=3600, attempts=1)  # no lease_boot_id

    result = ssh_node.run_ssh_node_pass(store, limit=2)

    assert result["claimed"] == 0
    assert _status(store, rid) == "running"


def test_unadvertised_worker_stamps_no_lease_boot_id(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review Finding 3: a worker with a MINTED boot_id but no
    PRECIS_PROCESS never ADVERTISES that boot_id (``_own_boot_ids_meta``
    requires both) — so it must never STAMP it as ``lease_boot_id`` either,
    or the epoch arm's "no live advertisement" sentinel would read the
    unadvertised-but-genuinely-live claim as provably gone and steal it on
    the very next claim pass. Confirmed via the meta this claim stamps."""
    from precis.workers import heartbeat as _heartbeat_mod

    _succeeds(ssh_node, monkeypatch)
    monkeypatch.setattr(_heartbeat_mod, "_boot_id", "unadvertised-boot-id")
    monkeypatch.setattr(_heartbeat_mod, "_boot_process", None)
    monkeypatch.delenv("PRECIS_PROCESS", raising=False)
    rid = _mk_job(store, params={"resources": {"wall_seconds": 1800}})

    ssh_node.run_ssh_node_pass(store, limit=2)

    meta = _meta(store, rid)
    assert meta["lease_boot_id"] is None
    assert meta["lease_process"] is None
    assert meta["lease_host"] is None


def test_unadvertised_worker_claim_not_epoch_stealable_while_lease_live(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§H Finding 3, end-to-end through the REAL SQL claim path: a job
    claimed by an unadvertised worker (no PRECIS_PROCESS) stamps no
    lease_boot_id (see above), so — with its lease still in the future —
    it is NOT claimable via the epoch arm, only via expiry (unchanged from
    today's null-lease_boot_id behaviour). Pre-fix, this same claim would
    have stamped the unadvertised (but real) boot_id, and the epoch arm's
    COALESCE-sentinel would have immediately treated it as provably gone
    and stolen it right here."""
    from precis.workers import heartbeat as _heartbeat_mod

    monkeypatch.setattr(_heartbeat_mod, "_boot_id", "unadvertised-boot-id")
    monkeypatch.setattr(_heartbeat_mod, "_boot_process", None)
    monkeypatch.delenv("PRECIS_PROCESS", raising=False)
    monkeypatch.setattr(
        ssh_node,
        "get_job_type",
        lambda name: _spec(dispatch=lambda c, s: None),  # leaves STATUS:running
    )
    rid = _mk_job(store, params={"resources": {"wall_seconds": 1800}})

    ssh_node.run_ssh_node_pass(store, limit=2)  # claims fresh; lease is live+future

    assert _status(store, rid) == "running"
    assert _meta(store, rid)["lease_boot_id"] is None

    with store.pool.connection() as conn:
        claimed = claim_executor_jobs(
            conn, executor="ssh_node", limit=2, reclaim_stale_running=True
        )
        conn.rollback()  # inspection only

    assert claimed == []  # not epoch-stealable — no lease_boot_id to compare
    assert _status(store, rid) == "running"  # untouched


# ── §H piece 3: generalized attempt cap ────────────────────────────


def test_epoch_reclaim_counts_attempt_and_keeps_reason(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A replaced worker may have died because of the job, not a redeploy."""
    _succeeds(ssh_node, monkeypatch)
    store.record_heartbeat(
        "spark-epoch-4", meta={"boot_ids": {"ssh_node": "new-generation"}}
    )
    rid = _mk_running_job(
        store,
        lease_offset_s=3600,
        attempts=1,
        lease_boot_id="dead-generation",
        lease_process="ssh_node",
        lease_host="spark-epoch-4",
    )

    result = ssh_node.run_ssh_node_pass(store, limit=2)

    assert result == {"claimed": 1, "ok": 1, "failed": 0}
    meta = _meta(store, rid)
    assert meta["attempts"] == 2
    assert meta["reclaims"][-1]["why"] == "epoch"


def test_expired_epoch_reclaim_counts_once(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both eligibility arms may match, but this is only one lost run."""
    _succeeds(ssh_node, monkeypatch)
    store.record_heartbeat(
        "worker-both-arms", meta={"boot_ids": {"ssh_node": "new-generation"}}
    )
    rid = _mk_running_job(
        store,
        lease_offset_s=-60,
        attempts=1,
        lease_boot_id="dead-generation",
        lease_process="ssh_node",
        lease_host="worker-both-arms",
    )

    assert ssh_node.run_ssh_node_pass(store, limit=1) == {
        "claimed": 1,
        "ok": 1,
        "failed": 0,
    }
    meta = _meta(store, rid)
    assert meta["attempts"] == 2
    assert len(meta["reclaims"]) == 1
    assert meta["reclaims"][0]["why"] == "epoch"


def test_interleaved_epoch_reclaims_advance_attempts(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two successor generations both charge the run-attempt budget."""
    from precis.workers import heartbeat as _heartbeat_mod

    def _idle_dispatch(ctx: Any, _s: Any) -> None:
        pass  # leaves STATUS:running — stands in for a still-in-flight run

    monkeypatch.setattr(
        ssh_node, "get_job_type", lambda name: _spec(dispatch=_idle_dispatch)
    )
    monkeypatch.setenv("PRECIS_PROCESS", "ssh_node")
    monkeypatch.setenv("PRECIS_HOST_NAME", "spark-interleave")
    rid = _mk_running_job(
        store,
        lease_offset_s=3600,
        attempts=0,
        lease_boot_id="gen-minus-1",
        lease_process="ssh_node",
        lease_host="spark-interleave",
    )

    # Generation 1 claims: epoch mismatch (dead "gen-minus-1" vs advertised
    # "gen-1").
    monkeypatch.setattr(_heartbeat_mod, "_boot_id", "gen-1")
    monkeypatch.setattr(_heartbeat_mod, "_boot_process", "ssh_node")
    store.record_heartbeat("spark-interleave", meta={"boot_ids": {"ssh_node": "gen-1"}})
    result = ssh_node.run_ssh_node_pass(store, limit=2)
    assert result == {"claimed": 1, "ok": 1, "failed": 0}
    assert _meta(store, rid)["attempts"] == 1
    assert _meta(store, rid)["lease_boot_id"] == "gen-1"

    # Another bounce before generation 1 finished: put the row back at
    # STATUS:running with an unexpired lease (gen-1's own claim stamp), so
    # generation 2's claim can ONLY fire via the epoch arm, not expiry.
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || jsonb_build_object("
            "  'lease_until', (now() + make_interval(secs => 3600))::text"
            ") WHERE ref_id = %s",
            (rid,),
        )
        conn.commit()
    store.add_tag(rid, Tag.parse_strict("STATUS:running"), set_by="agent")

    # Generation 2 claims: advertise a new boot_id, provably replacing gen-1.
    monkeypatch.setattr(_heartbeat_mod, "_boot_id", "gen-2")
    store.record_heartbeat("spark-interleave", meta={"boot_ids": {"ssh_node": "gen-2"}})
    result = ssh_node.run_ssh_node_pass(store, limit=2)
    assert result == {"claimed": 1, "ok": 1, "failed": 0}
    meta = _meta(store, rid)
    assert meta["attempts"] == 2
    assert [r["why"] for r in meta["reclaims"][-2:]] == ["epoch", "epoch"]


# ── Starvation-bound reclaim pre-pass (gr191124/gr192372/gr191125) ──


def test_stale_running_reclaimed_despite_higher_prio_fresh_flood(
    store: Store,
) -> None:
    """Regression for the prod incident this fix root-caused: ``plan_tick``
    job 188721 sat ``STATUS:running`` 35h before reclaim. ONE default-prio
    stale-running row with an expired lease, plus MORE fresh
    ``STATUS:queued`` rows at a higher priority (lower prio number) than
    the per-cycle claim ``limit``, used to starve the stale row out of
    EVERY claim cycle — it never ranked inside the shared prio/age
    selection window. A single ``claim_executor_jobs(...,
    reclaim_stale_running=True)`` call must reclaim the stale row
    regardless of fresh-work pressure. Pre-fix this assertion is red."""
    stale_rid = _mk_running_job(store, lease_offset_s=-60, attempts=1)
    # limit=2 below; seed MORE than 2 higher-priority (prio=2) fresh jobs
    # so the stale row (default prio 5, unset) never makes the prio/age
    # cut on its own — the exact starvation shape from the incident.
    for _ in range(4):
        _mk_job(store, params={"resources": {"wall_seconds": 60}}, prio=2)

    with store.pool.connection() as conn:
        claimed = claim_executor_jobs(
            conn, executor="ssh_node", limit=2, reclaim_stale_running=True
        )
        conn.commit()

    claimed_ids = {ref_id for ref_id, _title, _meta in claimed}
    assert stale_rid in claimed_ids


def test_fresh_work_still_respects_prio_and_limit_alongside_reclaim(
    store: Store,
) -> None:
    """Same starvation seed as above: the fresh-work half of the SAME call
    still claims exactly ``limit`` fresh rows, still prio/age-ordered —
    the reclaim pre-pass is additive to the fresh-work budget, not a
    substitute that eats into it."""
    _mk_running_job(store, lease_offset_s=-60, attempts=1)
    fresh_ids = [
        _mk_job(store, params={"resources": {"wall_seconds": 60}}, prio=2)
        for _ in range(4)
    ]

    with store.pool.connection() as conn:
        claimed = claim_executor_jobs(
            conn, executor="ssh_node", limit=2, reclaim_stale_running=True
        )
        conn.commit()

    fresh_claimed = [ref_id for ref_id, _t, _m in claimed if ref_id in fresh_ids]
    # prio-ordered, oldest-first tiebreak within the prio=2 band — the two
    # lowest ref_ids among the four fresh rows, and no more than ``limit``.
    assert fresh_claimed == sorted(fresh_ids)[:2]


def test_fresh_work_is_fifo_by_queued_since_not_ref_id(store: Store) -> None:
    """2026-09-25 starvation: within one prio band an OLDER ref_id that was
    re-queued a moment ago must NOT out-rank a YOUNGER ref_id that has been
    waiting since before it. ``queued_since`` is the current
    ``STATUS:queued`` tag's ``created_at`` (a status replace re-inserts the
    row), so the row waiting longest wins the slot."""
    older = _mk_job(store, params={"resources": {"wall_seconds": 60}}, prio=2)
    younger = _mk_job(store, params={"resources": {"wall_seconds": 60}}, prio=2)
    assert older < younger
    with store.pool.connection() as conn:
        # younger has been claimable for 10 min; older was (re-)queued just now
        conn.execute(
            "UPDATE ref_tags rt SET created_at = now() - interval '10 minutes' "
            "FROM tags t WHERE rt.tag_id = t.tag_id AND rt.ref_id = %s "
            "AND t.namespace = 'STATUS' AND t.value = 'queued'",
            (younger,),
        )
        conn.commit()

    with store.pool.connection() as conn:
        claimed = claim_executor_jobs(conn, executor="ssh_node", limit=1)
        conn.commit()

    assert [ref_id for ref_id, _t, _m in claimed] == [younger]


def test_fresh_work_prio_still_beats_queued_since(store: Store) -> None:
    """FIFO is a tiebreak INSIDE a prio band — a prio=1 row queued a second
    ago still goes ahead of a prio=5 row that has waited an hour."""
    patient = _mk_job(store, params={"resources": {"wall_seconds": 60}}, prio=5)
    urgent = _mk_job(store, params={"resources": {"wall_seconds": 60}}, prio=1)
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE ref_tags rt SET created_at = now() - interval '60 minutes' "
            "FROM tags t WHERE rt.tag_id = t.tag_id AND rt.ref_id = %s "
            "AND t.namespace = 'STATUS' AND t.value = 'queued'",
            (patient,),
        )
        conn.commit()

    with store.pool.connection() as conn:
        claimed = claim_executor_jobs(conn, executor="ssh_node", limit=1)
        conn.commit()

    assert [ref_id for ref_id, _t, _m in claimed] == [urgent]


# ── §H piece 4: detached submit/poll protocol (gr187627) ───────────


def test_submit_launches_detached_and_never_blocks(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A job_type exposing BOTH submit and poll never reaches the blocking
    dispatch path — submit returns a handle, the job stays STATUS:running
    (poll drives it terminal on a LATER pass), and the handle is persisted
    to meta.compute_handle for the next pass's poll step to find."""
    submitted: list[int] = []

    def _submit(ctx: Any, _spec: Any) -> str:
        submitted.append(ctx.ref_id)
        return "remote-pid-4242"

    def _poll(ctx: Any, handle: str) -> bool:  # pragma: no cover — not reached here
        return False

    monkeypatch.setattr(
        ssh_node, "get_job_type", lambda name: _spec(submit=_submit, poll=_poll)
    )
    rid = _mk_job(store, params={"resources": {"wall_seconds": 1800}})

    result = ssh_node.run_ssh_node_pass(store, limit=2)

    assert result == {"claimed": 1, "ok": 1, "failed": 0}
    assert submitted == [rid]
    assert _status(store, rid) == "running"  # NOT auto-finalized — poll's job
    assert _meta(store, rid)["compute_handle"] == "remote-pid-4242"
    # gr191673: the submit-intent marker is cleared once the handle landed.
    assert not _meta(store, rid).get("submit_started_at")


def test_submit_intent_marker_committed_before_launch(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr191673: the submit-intent marker must be VISIBLE IN THE DB before
    spec.submit launches the detached compute — that ordering is the whole
    fix (a crash after submit but before the handle persist is otherwise
    indistinguishable from never-submitted, and gets double-launched)."""
    seen_at_submit: list[Any] = []

    def _submit(ctx: Any, _spec: Any) -> str:
        seen_at_submit.append(_meta(store, ctx.ref_id).get("submit_started_at"))
        return "remote-pid-5555"

    monkeypatch.setattr(
        ssh_node,
        "get_job_type",
        lambda name: _spec(submit=_submit, poll=lambda c, h: False),
    )
    _mk_job(store)

    ssh_node.run_ssh_node_pass(store, limit=2)

    assert len(seen_at_submit) == 1 and seen_at_submit[0]  # committed first


def test_reclaim_mid_submit_crash_fails_without_resubmitting(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr191673 core: a reclaimed STATUS:running row carrying the
    submit-intent marker but NO compute_handle means the prior worker died
    inside the submit window — the detached compute may be alive with no
    handle to adopt. The reclaim must fail the job loudly (infra), never
    call submit() again (the double-launch + orphan leak)."""
    submitted: list[int] = []

    def _submit(ctx: Any, _spec: Any) -> str:  # pragma: no cover — must not run
        submitted.append(ctx.ref_id)
        return "second-launch"

    monkeypatch.setattr(
        ssh_node,
        "get_job_type",
        lambda name: _spec(submit=_submit, poll=lambda c, h: False),
    )
    rid = _mk_running_job(
        store, lease_offset_s=-60, attempts=1, submit_started_at=time.time()
    )

    result = ssh_node.run_ssh_node_pass(store, limit=2)

    assert result == {"claimed": 1, "ok": 0, "failed": 1}
    assert submitted == []  # never re-submitted
    assert _status(store, rid) == "failed"
    meta = _meta(store, rid)
    assert meta["failure_class"] == "infra"
    # Marker cleared so a deliberate requeue gets a fresh submit.
    assert not meta.get("submit_started_at")


def test_submit_raise_clears_intent_marker(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An in-process submit() exception routes to the normal failure path;
    the intent marker must not survive it, or a requeue of the failed job
    would be mistaken for a mid-submit crash and refused."""

    def _submit(ctx: Any, _spec: Any) -> str:
        raise RuntimeError("ssh connect timeout")

    monkeypatch.setattr(
        ssh_node,
        "get_job_type",
        lambda name: _spec(submit=_submit, poll=lambda c, h: False),
    )
    rid = _mk_job(store)

    result = ssh_node.run_ssh_node_pass(store, limit=2)

    assert result["failed"] == 1
    assert not _meta(store, rid).get("submit_started_at")


def test_poll_still_running_renews_lease_without_finishing(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A poll that returns False (still running) leaves STATUS alone but
    renews the lease — so the generic expiry-reclaim arm can't fire
    mid-run just because the original claim's lease is aging out."""
    polled: list[Any] = []

    def _poll(ctx: Any, handle: Any) -> bool:
        polled.append(handle)
        return False

    monkeypatch.setattr(
        ssh_node,
        "get_job_type",
        lambda name: _spec(poll=_poll, submit=lambda c, s: None),
    )
    rid = _mk_running_job(
        store, lease_offset_s=-60, compute_handle="remote-pid-1"
    )  # already-expired lease — must be renewed by the poll step, not reclaimed

    result = ssh_node.run_ssh_node_pass(store, limit=2)

    assert result == {"claimed": 0, "ok": 0, "failed": 0}
    assert polled == ["remote-pid-1"]
    assert _status(store, rid) == "running"
    meta = _meta(store, rid)
    assert meta["lease_until"] is not None
    # Renewed into the future, not the seeded expired value.
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT (meta->>'lease_until')::timestamptz > now() "
            "FROM refs WHERE ref_id = %s",
            (rid,),
        ).fetchone()
        assert row is not None
        (still_future,) = row
    assert still_future is True


def test_poll_terminal_drives_status_and_counts_ok(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A poll that returns True (terminal) means the plugin already called
    ctx.set_status — the executor does nothing further and counts it ok."""

    def _poll(ctx: Any, handle: Any) -> bool:
        ctx.set_status("succeeded")
        return True

    monkeypatch.setattr(
        ssh_node,
        "get_job_type",
        lambda name: _spec(poll=_poll, submit=lambda c, s: None),
    )
    rid = _mk_running_job(store, lease_offset_s=3600, compute_handle="remote-pid-2")

    result = ssh_node.run_ssh_node_pass(store, limit=2)

    assert result == {"claimed": 0, "ok": 1, "failed": 0}
    assert _status(store, rid) == "succeeded"


def test_poll_missing_resolvable_poll_fails_infra(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An in-flight row whose job_type can't be resolved (or lost its
    poll) is defensively INFRA-failed rather than polled forever."""
    monkeypatch.setattr(ssh_node, "get_job_type", lambda name: None)
    rid = _mk_running_job(store, lease_offset_s=3600, compute_handle="remote-pid-3")

    result = ssh_node.run_ssh_node_pass(store, limit=2)

    assert result == {"claimed": 0, "ok": 1, "failed": 0}
    assert _status(store, rid) == "failed"
    assert _meta(store, rid)["failure_class"] == "infra"


def test_legacy_dispatch_still_works_with_deprecation_warning(
    store: Store, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A job_type with only ``dispatch`` (no submit/poll) still runs exactly
    as before — backward compat — but ssh_node logs a deprecation warning
    naming gr187627, once per job_type per process (not once per job)."""
    ssh_node._warned_legacy_job_types.clear()
    _succeeds(ssh_node, monkeypatch)
    rid1 = _mk_job(store, params={"resources": {"wall_seconds": 1800}})
    rid2 = _mk_job(store, params={"resources": {"wall_seconds": 1800}})

    with caplog.at_level("WARNING", logger="precis.workers.executors.ssh_node"):
        r1 = ssh_node.run_ssh_node_pass(store, limit=2)

    assert r1 == {"claimed": 2, "ok": 2, "failed": 0}
    assert _status(store, rid1) == "succeeded"
    assert _status(store, rid2) == "succeeded"
    gr_warnings = [rec for rec in caplog.records if "gr187627" in rec.getMessage()]
    assert len(gr_warnings) == 1  # once per job_type, not once per job


def test_hung_legacy_dispatch_is_cut_off_at_bound(
    store: Store, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """gr336604: a legacy blocking dispatch that never returns is failed at
    the outer wall-clock bound instead of holding the worker pass forever."""
    import threading

    release = threading.Event()
    monkeypatch.setattr(
        ssh_node,
        "get_job_type",
        lambda name: _spec(dispatch=lambda c, s: release.wait()),
    )
    monkeypatch.setattr(ssh_node, "_wall_bound_seconds", lambda meta, spec=None: 1)
    rid = _mk_job(store)

    t0 = time.time()
    try:
        with caplog.at_level("ERROR", logger="precis.workers.executors.ssh_node"):
            result = ssh_node.run_ssh_node_pass(store, limit=1)
    finally:
        release.set()

    assert time.time() - t0 < 30
    assert result["failed"] == 1 and result["ok"] == 0  # timeout counts failed
    assert _status(store, rid) == "failed"
    assert _meta(store, rid)["failure_class"] == "timeout"
    assert any("gr336604" in r.getMessage() for r in caplog.records)
    blocks = store.chunks.list_chunks_for_ref(rid)
    assert any("wall-clock bound" in b.text for b in blocks)


def test_abandoned_dispatch_thread_cannot_write_job_state(
    store: Store, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """gr336604: after the bound fires, a late-returning dispatch calling
    ctx.set_status('succeeded') is a logged no-op; the job stays failed."""
    import threading

    release = threading.Event()
    done = threading.Event()
    spawned: list[Any] = []

    def _dispatch(ctx: Any, _spec: Any) -> None:
        release.wait()
        ctx.set_status("succeeded")
        ctx.append_chunk("job_summary", "late write")
        spawned.append(ctx.spawn_child("fake_relax", {}))
        done.set()

    monkeypatch.setattr(
        ssh_node, "get_job_type", lambda name: _spec(dispatch=_dispatch)
    )
    monkeypatch.setattr(ssh_node, "_wall_bound_seconds", lambda meta, spec=None: 1)
    rid = _mk_job(store)

    with caplog.at_level("WARNING", logger="precis.workers.executors.ssh_node"):
        ssh_node.run_ssh_node_pass(store, limit=1)
        assert _status(store, rid) == "failed"
        release.set()
        assert done.wait(10)

    assert _status(store, rid) == "failed"
    assert not any(
        "late write" in b.text for b in store.chunks.list_chunks_for_ref(rid)
    )
    assert any("abandoned dispatch thread" in r.getMessage() for r in caplog.records)
    # spawn_child after abandonment is a logged no-op returning None.
    assert spawned == [None]
    assert any("ctx.spawn_child" in r.getMessage() for r in caplog.records)


# ── §H piece 2: detached poll wall-clock deadline ───────────────────


def test_submit_stamps_a_wall_clock_deadline(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§H piece 2: submitting a detached job stamps meta.deadline so
    ``_poll_one`` has a termination bound — the sweeper's own wall-clock
    retirement (§H piece 6) leaves no other one for a detached job_type."""
    before = time.time()

    def _submit(ctx: Any, _spec: Any) -> str:
        return "remote-pid-deadline"

    monkeypatch.setattr(
        ssh_node,
        "get_job_type",
        lambda name: _spec(submit=_submit, poll=lambda c, h: False),
    )
    rid = _mk_job(store, params={"resources": {"wall_seconds": 1800}})

    ssh_node.run_ssh_node_pass(store, limit=2)

    meta = _meta(store, rid)
    assert meta["deadline"] >= before + 1800
    # Wall_seconds + the same margin _lease_seconds uses.
    assert meta["deadline"] <= before + 1800 + ssh_node._LEASE_MARGIN_S + 5


def test_poll_in_deadline_still_renews(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A poll before the deadline still renews the lease and calls
    poll() normally — the deadline only trips once it's actually past."""
    polled: list[Any] = []

    def _poll(ctx: Any, handle: Any) -> bool:
        polled.append(handle)
        return False

    monkeypatch.setattr(
        ssh_node,
        "get_job_type",
        lambda name: _spec(poll=_poll, submit=lambda c, s: None),
    )
    rid = _mk_running_job(
        store, lease_offset_s=3600, compute_handle="remote-pid-indeadline"
    )
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || jsonb_build_object('deadline', %s::float) "
            "WHERE ref_id = %s",
            (time.time() + 3600, rid),
        )
        conn.commit()

    result = ssh_node.run_ssh_node_pass(store, limit=2)

    assert result == {"claimed": 0, "ok": 0, "failed": 0}
    assert polled == ["remote-pid-indeadline"]
    assert _status(store, rid) == "running"


def test_poll_past_deadline_terminalizes_without_kill_hook(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A poll past its deadline stops polling and fails the job even when
    the job_type has no ``kill`` hook — a bound job_type still gets
    terminalized on schedule, it just can't proactively stop the remote
    side."""
    polled: list[Any] = []

    def _poll(ctx: Any, handle: Any) -> bool:  # pragma: no cover — must not fire
        polled.append(handle)
        return False

    monkeypatch.setattr(
        ssh_node,
        "get_job_type",
        lambda name: _spec(poll=_poll, submit=lambda c, s: None),
    )
    rid = _mk_running_job(
        store, lease_offset_s=3600, compute_handle="remote-pid-timeout"
    )
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || jsonb_build_object('deadline', %s::float) "
            "WHERE ref_id = %s",
            (time.time() - 5, rid),
        )
        conn.commit()

    result = ssh_node.run_ssh_node_pass(store, limit=2)

    assert result == {"claimed": 0, "ok": 1, "failed": 0}
    assert polled == []  # poll() never called past the deadline
    assert _status(store, rid) == "failed"
    tags = {str(t) for t in store.tags_for(rid)}
    assert "swept:wall-timeout" in tags
    meta = _meta(store, rid)
    assert meta["failure_class"] == "timeout"
    assert not meta.get("compute_handle_released", False)
    assert "killed at wall-clock deadline" in meta["error"]
    events = [
        c.text
        for c in store.chunks.list_chunks_for_ref(rid)
        if getattr(c, "chunk_kind", None) == "job_event"
    ]
    assert any("killed at wall-clock deadline" in t for t in events)


def test_poll_past_deadline_calls_kill_hook_when_present(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the job_type declares ``kill``, a past-deadline poll calls it
    (a chance to actually stop the remote compute) before terminalizing."""
    killed: list[tuple[Any, Any]] = []

    def _kill(ctx: Any, handle: Any) -> None:
        killed.append((ctx.ref_id, handle))

    monkeypatch.setattr(
        ssh_node,
        "get_job_type",
        lambda name: JobTypeSpec(
            name="fake_relax",
            params_schema={"type": "object"},
            compatible_executors=frozenset({"ssh_node"}),
            requires=frozenset({"has_gpaw"}),
            description="fake relax for tests",
            run=lambda *a, **k: "noop",
            submit=lambda c, s: None,
            poll=lambda c, h: False,
            kill=_kill,
        ),
    )
    rid = _mk_running_job(store, lease_offset_s=3600, compute_handle="remote-pid-kill")
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || jsonb_build_object('deadline', %s::float) "
            "WHERE ref_id = %s",
            (time.time() - 5, rid),
        )
        conn.commit()

    result = ssh_node.run_ssh_node_pass(store, limit=2)

    assert result == {"claimed": 0, "ok": 1, "failed": 0}
    assert killed == [(rid, "remote-pid-kill")]
    assert _meta(store, rid)["compute_handle_released"] is True
    assert _status(store, rid) == "failed"


def test_verify_seed_survives_19h_and_is_killed_past_its_wall(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr493237: verify seeds 489188/489256/489258 were killed at 19 h
    (64800 s wall + margin). At the default verify wall a seed 19 h in is
    still polled; one past wall + margin still hits the kill hook and fails
    as ``timeout``."""
    from precis.quest import compute

    monkeypatch.delenv(compute._AUTOCATPATH_WALL_SECONDS_ENV, raising=False)
    monkeypatch.delenv(compute._AUTOCATPATH_VERIFY_WALL_SECONDS_ENV, raising=False)
    params = {
        "resources": {"wall_seconds": compute._autocatpath_wall_seconds("verify")}
    }
    bound = ssh_node._wall_bound_seconds({"params": params})
    assert bound == 86_400 + ssh_node._LEASE_MARGIN_S

    polled: list[Any] = []
    killed: list[tuple[Any, Any]] = []

    def _poll(ctx: Any, handle: Any) -> bool:
        polled.append(handle)
        return False

    def _kill(ctx: Any, handle: Any) -> None:
        killed.append((ctx.ref_id, handle))

    monkeypatch.setattr(
        ssh_node,
        "get_job_type",
        lambda name: JobTypeSpec(
            name="fake_relax",
            params_schema={"type": "object"},
            compatible_executors=frozenset({"ssh_node"}),
            requires=frozenset(),
            description="fake verify seed",
            run=lambda *a, **k: "noop",
            submit=lambda c, s: None,
            poll=_poll,
            kill=_kill,
        ),
    )
    alive = _mk_running_job(store, lease_offset_s=3600, compute_handle="seed-19h")
    over = _mk_running_job(store, lease_offset_s=3600, compute_handle="seed-over")
    now = time.time()
    with store.pool.connection() as conn:
        for rid, started in ((alive, now - 19 * 3600), (over, now - bound - 5)):
            conn.execute(
                "UPDATE refs SET meta = meta || jsonb_build_object("
                "'params', %s::jsonb, 'deadline', %s::float) WHERE ref_id = %s",
                (json.dumps(params), started + bound, rid),
            )
        conn.commit()

    result = ssh_node.run_ssh_node_pass(store, limit=2)

    assert result == {"claimed": 0, "ok": 1, "failed": 0}
    assert polled == ["seed-19h"]
    assert _status(store, alive) == "running"
    assert killed == [(over, "seed-over")]
    assert _status(store, over) == "failed"
    assert _meta(store, over)["failure_class"] == "timeout"


# ── §B-2 piece 5: operator kill backstop (`precis jobs kill`) ──────


def _mk_hung_job_with_kill_request(
    store: Store,
    *,
    compute_handle: Any,
    note: str | None = None,
    parent_id: int | None = None,
    requires: dict[str, int] | None = None,
) -> int:
    """A STATUS:running detached job already carrying ``meta.kill_requested``
    — stands in for an operator having run ``precis jobs kill`` against an
    in-flight job whose plugin ``poll`` never terminates on its own (the
    injected-hang drill)."""
    request: dict[str, Any] = {"at": "2026-01-01T00:00:00+00:00", "actor": "operator"}
    if note is not None:
        request["note"] = note
    meta: dict[str, Any] = {
        "executor": "ssh_node",
        "job_type": "fake_relax",
        "params": {},
        "compute_handle": compute_handle,
        "kill_requested": request,
    }
    if requires is not None:
        meta["requires"] = requires
    ref = store.insert_ref(
        kind="job", slug=None, title="hung job", meta=meta, parent_id=parent_id
    )
    store.add_tag(ref.id, Tag.parse_strict("STATUS:running"), set_by="agent")
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || jsonb_build_object("
            "  'lease_until', (now() + make_interval(secs => 3600))::text"
            ") WHERE ref_id = %s",
            (int(ref.id),),
        )
        conn.commit()
    return int(ref.id)


def test_kill_requested_terminalizes_via_kill_hook_and_bubbles(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The injected-hang drill (§B-2 acceptance, verbatim): a running
    detached job whose ``poll`` never terminates on its own and whose
    ``kill`` records the invocation. A stamped ``meta.kill_requested``
    drives the NEXT ``_poll_one`` to call ``kill`` (never ``poll`` —
    the kill check runs first), terminal-fail with
    ``swept:killed-by-operator``, and bubble to the parent."""
    killed: list[tuple[Any, Any]] = []
    polled: list[Any] = []

    def _poll(ctx: Any, handle: Any) -> bool:
        polled.append(handle)
        return False  # would hang forever if ever reached

    def _kill(ctx: Any, handle: Any) -> None:
        killed.append((ctx.ref_id, handle))

    monkeypatch.setattr(
        ssh_node,
        "get_job_type",
        lambda name: JobTypeSpec(
            name="fake_relax",
            params_schema={"type": "object"},
            compatible_executors=frozenset({"ssh_node"}),
            requires=frozenset({"has_gpaw"}),
            description="fake relax for tests",
            run=lambda *a, **k: "noop",
            poll=_poll,
            submit=lambda c, s: None,
            kill=_kill,
        ),
    )
    parent = store.insert_ref(kind="todo", slug=None, title="owner", meta={})
    rid = _mk_hung_job_with_kill_request(
        store,
        compute_handle="remote-pid-hang",
        note="operator drill",
        parent_id=parent.id,
    )

    result = ssh_node.run_ssh_node_pass(store, limit=2)

    assert result == {"claimed": 0, "ok": 1, "failed": 0}
    assert killed == [(rid, "remote-pid-hang")]
    assert polled == []  # kill_requested short-circuits BEFORE poll() is ever called
    assert _status(store, rid) == "failed"
    tags = {str(t) for t in store.tags_for(rid)}
    assert "swept:killed-by-operator" in tags
    events = [
        c.text
        for c in store.chunks.list_chunks_for_ref(rid)
        if getattr(c, "chunk_kind", None) == "job_event"
    ]
    assert any("killed by operator" in t and "operator drill" in t for t in events)
    parent_tags = {str(t) for t in store.tags_for(parent.id)}
    assert any(t.startswith("child-failed:") for t in parent_tags)
    assert "kill_gpu_reset" not in _meta(store, rid)  # no gpu requires — untouched


def test_kill_requested_with_gpu_requires_resets_gpu(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A GPU-requiring job (``meta.requires`` explicit, or a
    ``struct_relax``/``fold`` job_type via the registry) gets a best-effort
    ``struct_relax.reset_gpu`` after the kill, recorded on
    ``meta.kill_gpu_reset``."""
    from precis.workers.job_types import struct_relax

    reset_calls: list[dict[str, Any]] = []

    def _fake_reset_gpu(*, node: str | None = None, **_kw: Any) -> bool:
        reset_calls.append({"node": node})
        return True

    monkeypatch.setattr(struct_relax, "reset_gpu", _fake_reset_gpu)
    monkeypatch.setattr(
        ssh_node,
        "get_job_type",
        lambda name: JobTypeSpec(
            name="fake_relax",
            params_schema={"type": "object"},
            compatible_executors=frozenset({"ssh_node"}),
            requires=frozenset({"has_gpaw"}),
            description="fake relax for tests",
            run=lambda *a, **k: "noop",
            poll=lambda c, h: False,
            submit=lambda c, s: None,
            kill=lambda c, h: None,
        ),
    )
    rid = _mk_hung_job_with_kill_request(
        store, compute_handle="remote-pid-gpu", requires={"gpu": 1}
    )

    result = ssh_node.run_ssh_node_pass(store, limit=2)

    assert result == {"claimed": 0, "ok": 1, "failed": 0}
    assert reset_calls == [{"node": None}]  # no target_node pinned in this test
    assert _meta(store, rid)["kill_gpu_reset"] is True
    assert _status(store, rid) == "failed"


def test_reclaimed_row_with_compute_handle_is_re_adopted_not_double_submitted(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§H piece 4, REQUIRED: an epoch-reclaimed row that already has
    meta.compute_handle (a prior generation's submit already succeeded,
    the detached compute may still be alive) must NEVER be re-submitted —
    that would double-launch the remote compute. Mirrors claude_docker's
    re-adopt branch."""
    submit_calls: list[int] = []

    def _submit(ctx: Any, _spec: Any) -> str:  # pragma: no cover — must not fire
        submit_calls.append(ctx.ref_id)
        return "should-not-happen"

    monkeypatch.setattr(
        ssh_node,
        "get_job_type",
        lambda name: _spec(submit=_submit, poll=lambda c, h: False),
    )
    store.record_heartbeat(
        "spark-epoch-readopt", meta={"boot_ids": {"ssh_node": "new-generation"}}
    )
    rid = _mk_running_job(
        store,
        lease_offset_s=3600,  # far future — only the epoch arm can fire
        attempts=1,
        lease_boot_id="dead-generation",
        lease_process="ssh_node",
        lease_host="spark-epoch-readopt",
        compute_handle="remote-pid-still-alive",
    )

    result = ssh_node.run_ssh_node_pass(store, limit=2)

    assert submit_calls == []  # never re-submitted
    assert result == {"claimed": 1, "ok": 1, "failed": 0}
    assert _status(store, rid) == "running"  # still tracked, not terminalized
    assert _meta(store, rid)["compute_handle"] == "remote-pid-still-alive"
    # The claim re-stamped THIS worker's own lease identity over the dead
    # generation's stamp (so the epoch arm doesn't misfire again next pass).
    assert _meta(store, rid)["lease_boot_id"] != "dead-generation"
    events = [
        c.text
        for c in store.chunks.list_chunks_for_ref(rid)
        if getattr(c, "chunk_kind", None) == "job_event"
    ]
    assert any("re-adopted" in t for t in events)


# ── gr520866: identity-safe detached orphan reap ──────────────────


def _identity_handle() -> dict[str, Any]:
    return {
        "pid": 4242,
        "pgid": 4242,
        "dir": "/tmp/autocatpath-seed-fake",
        "boot_id": "boot-for-test",
        "start_time": 123456,
    }


def _fake_detached_identity(
    monkeypatch: pytest.MonkeyPatch,
    *,
    boot_id: str = "boot-for-test",
    start_time: int = 123456,
    error: OSError | None = None,
) -> None:
    """All process probes are fake; comm deliberately contains spaces and ')'."""
    original = Path.read_text

    def read_text(path: Path, *args: Any, **kwargs: Any) -> str:
        if str(path) == "/proc/sys/kernel/random/boot_id":
            if error is not None:
                raise error
            return boot_id + "\n"
        if str(path) == "/proc/4242/stat":
            if error is not None:
                raise error
            fields = ["S", *(["0"] * 18), str(start_time), "0"]
            return "4242 (runner with ) spaces) " + " ".join(fields)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(ssh_node, "_detached_group_alive", lambda handle: True)


def _mk_terminal_handle_job(
    store: Store,
    *,
    compute_handle: Any,
    target_node: str | None = "spark",
    status: str = "failed",
    terminal_age_s: int = 300,
    released: bool = False,
) -> int:
    params: dict[str, Any] = {}
    if target_node is not None:
        params["target_node"] = target_node
    meta: dict[str, Any] = {
        "executor": "ssh_node",
        "job_type": "fake_relax",
        "params": params,
        "compute_handle": compute_handle,
        "compute_handle_released": released,
    }
    ref = store.insert_ref(
        kind="job", slug=None, title="orphaned terminal job", meta=meta
    )
    store.add_tag(
        ref.id, Tag.closed("STATUS", status), set_by="system", replace_prefix=True
    )
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE ref_tags rt SET created_at = now() - make_interval(secs => %s) "
            "FROM tags t WHERE rt.tag_id = t.tag_id AND rt.ref_id = %s "
            "AND t.namespace = 'STATUS'",
            (terminal_age_s, ref.id),
        )
        conn.commit()
    return int(ref.id)


def _kill_reap_spec(
    killed: list[tuple[Any, Any]], reaped: list[tuple[Any, Any]]
) -> JobTypeSpec:
    return JobTypeSpec(
        name="fake_relax",
        params_schema={"type": "object"},
        compatible_executors=frozenset({"ssh_node"}),
        requires=frozenset({"has_gpaw"}),
        description="fake relax for tests",
        run=lambda *a, **k: "noop",
        submit=lambda c, s: None,
        poll=lambda c, h: False,
        kill=lambda c, h: killed.append((c.ref_id, h)),
        reap=lambda c, h: reaped.append((c.ref_id, h)),
    )


def test_orphan_reap_kills_identity_matched_terminal_handle_once(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    killed: list[tuple[Any, Any]] = []
    reaped: list[tuple[Any, Any]] = []
    monkeypatch.setattr(
        ssh_node, "get_job_type", lambda name: _kill_reap_spec(killed, reaped)
    )
    _fake_detached_identity(monkeypatch)
    monkeypatch.setenv("PRECIS_NODE", "spark")
    handle = _identity_handle()
    rid = _mk_terminal_handle_job(store, compute_handle=handle)

    result = ssh_node.run_ssh_node_pass(store, limit=2)

    assert result == {"claimed": 0, "ok": 0, "failed": 0}
    assert killed == [(rid, handle)]
    assert reaped == [(rid, handle)]
    meta = _meta(store, rid)
    assert meta["detached_orphan_reaped"]["action"] == "killed"
    assert meta["compute_handle_released"] is True
    assert "reaped:detached-orphan" in {str(t) for t in store.tags_for(rid)}

    ssh_node.run_ssh_node_pass(store, limit=2)
    assert killed == [(rid, handle)]
    assert reaped == [(rid, handle)]


@pytest.mark.parametrize(
    ("handle", "status", "released", "terminal_age_s"),
    [
        pytest.param(_identity_handle(), "succeeded", True, 300, id="released-success"),
        pytest.param({"pid": 4242, "pgid": 4242}, "failed", False, 300, id="legacy"),
        pytest.param("remote-pid-string", "failed", False, 300, id="opaque"),
        pytest.param(_identity_handle(), "failed", False, 119, id="grace"),
        pytest.param(_identity_handle(), "failed", False, 0, id="just-terminal"),
    ],
)
def test_orphan_reap_excludes_unsafe_candidates(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    handle: Any,
    status: str,
    released: bool,
    terminal_age_s: int,
) -> None:
    killed: list[tuple[Any, Any]] = []
    reaped: list[tuple[Any, Any]] = []
    monkeypatch.setattr(
        ssh_node, "get_job_type", lambda name: _kill_reap_spec(killed, reaped)
    )
    _fake_detached_identity(monkeypatch)
    rid = _mk_terminal_handle_job(
        store,
        compute_handle=handle,
        status=status,
        released=released,
        terminal_age_s=terminal_age_s,
    )

    assert ssh_node._reap_detached_orphans(store, "spark") == 0
    assert killed == []
    assert reaped == []
    meta = _meta(store, rid)
    assert "detached_orphan_reaped" not in meta
    assert "detached_orphan_skipped" not in meta


@pytest.mark.parametrize(
    ("boot_id", "start_time", "error", "reason"),
    [
        ("boot-for-test", 999999, None, "start-time-mismatch"),
        ("new-boot", 123456, None, "boot-id-mismatch"),
        ("boot-for-test", 123456, PermissionError(), "identity-unreadable"),
        ("boot-for-test", 123456, FileNotFoundError(), "identity-unreadable"),
    ],
)
def test_orphan_reap_skips_and_stamps_unverified_identity(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    boot_id: str,
    start_time: int,
    error: OSError | None,
    reason: str,
) -> None:
    killed: list[tuple[Any, Any]] = []
    reaped: list[tuple[Any, Any]] = []
    monkeypatch.setattr(
        ssh_node, "get_job_type", lambda name: _kill_reap_spec(killed, reaped)
    )
    _fake_detached_identity(
        monkeypatch, boot_id=boot_id, start_time=start_time, error=error
    )
    rid = _mk_terminal_handle_job(store, compute_handle=_identity_handle())

    ssh_node._reap_detached_orphans(store, "spark")
    ssh_node._reap_detached_orphans(store, "spark")

    assert killed == []
    assert reaped == []  # mismatched scratch directories must survive too
    meta = _meta(store, rid)
    assert meta["detached_orphan_skipped"]["reason"] == reason
    assert "detached_orphan_reaped" not in meta


def test_orphan_reap_skips_missing_start_time(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    killed: list[tuple[Any, Any]] = []
    reaped: list[tuple[Any, Any]] = []
    monkeypatch.setattr(
        ssh_node, "get_job_type", lambda name: _kill_reap_spec(killed, reaped)
    )
    _fake_detached_identity(monkeypatch)
    handle = _identity_handle()
    del handle["start_time"]
    rid = _mk_terminal_handle_job(store, compute_handle=handle)

    ssh_node._reap_detached_orphans(store, "spark")

    assert killed == []
    assert reaped == []
    assert _meta(store, rid)["detached_orphan_skipped"]["reason"] == "missing-identity"


def test_orphan_reap_leaves_running_and_claimable_rows_alone(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    killed: list[tuple[Any, Any]] = []
    polled: list[Any] = []

    def poll(ctx: Any, handle: Any) -> bool:
        polled.append(handle)
        return False

    spec = replace(_kill_reap_spec(killed, []), poll=poll)
    monkeypatch.setattr(ssh_node, "get_job_type", lambda name: spec)
    _fake_detached_identity(monkeypatch)
    monkeypatch.setenv("PRECIS_NODE", "spark")
    handle = _identity_handle()
    running = _mk_running_job(
        store, lease_offset_s=3600, compute_handle=handle, target_node="spark"
    )
    queued = _mk_terminal_handle_job(store, compute_handle=handle, status="queued")

    result = ssh_node.run_ssh_node_pass(store, limit=2)

    assert killed == []
    assert polled == [handle]
    assert result["claimed"] == 1
    assert _status(store, running) == "running"
    assert _status(store, queued) == "running"
    assert "detached_orphan_reaped" not in _meta(store, running)
    assert "detached_orphan_reaped" not in _meta(store, queued)


@pytest.mark.parametrize("target_node", ["elsewhere", None])
def test_orphan_reap_node_gate_only_same_host(
    store: Store, monkeypatch: pytest.MonkeyPatch, target_node: str | None
) -> None:
    killed: list[tuple[Any, Any]] = []
    reaped: list[tuple[Any, Any]] = []
    monkeypatch.setattr(
        ssh_node, "get_job_type", lambda name: _kill_reap_spec(killed, reaped)
    )
    _fake_detached_identity(monkeypatch)
    rid = _mk_terminal_handle_job(
        store, compute_handle=_identity_handle(), target_node=target_node
    )

    assert ssh_node._reap_detached_orphans(store, "spark") == 0
    assert killed == []
    assert reaped == []
    assert "detached_orphan_reaped" not in _meta(store, rid)


def test_orphan_reap_marks_row_with_no_kill_hook(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        ssh_node,
        "get_job_type",
        lambda name: _spec(poll=lambda c, h: False, submit=lambda c, s: None),
    )
    _fake_detached_identity(monkeypatch)
    rid = _mk_terminal_handle_job(store, compute_handle=_identity_handle())

    ssh_node._reap_detached_orphans(store, "spark")
    ssh_node._reap_detached_orphans(store, "spark")

    assert _meta(store, rid)["detached_orphan_skipped"]["reason"] == "no-kill-hook"


def test_orphan_reap_competing_passes_only_one_claim_kills(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both passes select the same row before either performs its UPDATE claim."""
    killed: list[tuple[Any, Any]] = []
    reaped: list[tuple[Any, Any]] = []
    monkeypatch.setattr(
        ssh_node, "get_job_type", lambda name: _kill_reap_spec(killed, reaped)
    )
    _fake_detached_identity(monkeypatch)
    handle = _identity_handle()
    rid = _mk_terminal_handle_job(store, compute_handle=handle)
    select = ssh_node._orphaned_detached_jobs
    barrier = Barrier(2)

    def select_together(*args: Any, **kwargs: Any) -> Any:
        rows = select(*args, **kwargs)
        assert [row[0] for row in rows] == [rid]
        barrier.wait(timeout=10)
        return rows

    monkeypatch.setattr(ssh_node, "_orphaned_detached_jobs", select_together)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(ssh_node._reap_detached_orphans, store, "spark")
            for _ in range(2)
        ]
        counts = [future.result(timeout=20) for future in futures]

    assert sorted(counts) == [0, 1]
    assert killed == [(rid, handle)]
    assert reaped == [(rid, handle)]
    assert _meta(store, rid)["detached_orphan_reaped"]["action"] == "killed"


def test_poison_guarded_detached_job_killed_at_transition(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owning node kills immediately, without waiting for the reap grace."""
    killed: list[tuple[Any, Any]] = []
    reaped: list[tuple[Any, Any]] = []
    monkeypatch.setattr(
        ssh_node, "get_job_type", lambda name: _kill_reap_spec(killed, reaped)
    )
    _fake_detached_identity(monkeypatch)
    monkeypatch.setenv("PRECIS_NODE", "spark")
    store.record_heartbeat(
        "spark-poison", meta={"boot_ids": {"ssh_node": "new-generation"}}
    )
    handle = _identity_handle()
    rid = _mk_running_job(
        store,
        lease_offset_s=3600,
        attempts=ssh_node._MAX_ATTEMPTS,
        lease_boot_id="dead-generation",
        lease_process="ssh_node",
        lease_host="spark-poison",
        compute_handle=handle,
        target_node="spark",
    )

    first = ssh_node.run_ssh_node_pass(store, limit=2)

    assert first == {"claimed": 1, "ok": 0, "failed": 1}
    assert _status(store, rid) == "failed"
    assert killed == [(rid, handle)]
    assert reaped == [(rid, handle)]
    assert _meta(store, rid)["compute_handle_released"] is True
    assert _meta(store, rid)["detached_orphan_reaped"]["action"] == "killed"

    ssh_node.run_ssh_node_pass(store, limit=2)
    assert killed == [(rid, handle)]


def test_detached_group_permission_error_is_not_ours(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def denied(pgid: int, signal: int) -> None:
        raise PermissionError("not our process group")

    monkeypatch.setattr(ssh_node.os, "killpg", denied)
    assert ssh_node._detached_group_alive(_identity_handle()) is False


def test_orphan_reap_unavailable_group_cleans_after_terminal_stamp(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    killed: list[tuple[Any, Any]] = []
    reaped: list[tuple[Any, Any]] = []
    completion_stamps: list[bool] = []

    def reap(ctx: Any, handle: Any) -> None:
        completion_stamps.append(ctx.completion_stamped)
        reaped.append((ctx.ref_id, handle))

    spec = replace(_kill_reap_spec(killed, reaped), reap=reap)
    monkeypatch.setattr(ssh_node, "get_job_type", lambda name: spec)
    _fake_detached_identity(monkeypatch)
    monkeypatch.setattr(ssh_node, "_detached_group_alive", lambda handle: False)
    handle = _identity_handle()
    rid = _mk_terminal_handle_job(store, compute_handle=handle)

    ssh_node._reap_detached_orphans(store, "spark")
    ssh_node._reap_detached_orphans(store, "spark")

    assert killed == []
    assert reaped == [(rid, handle)]
    assert completion_stamps == [True]
    assert _meta(store, rid)["detached_orphan_skipped"]["reason"] == "group-unavailable"


@pytest.mark.parametrize("trigger", ["operator", "deadline"])
@pytest.mark.parametrize(
    "identity", ["missing-identity", "start-time-mismatch", "matched"]
)
def test_terminal_kill_requires_process_identity(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    trigger: str,
    identity: str,
) -> None:
    killed: list[tuple[Any, Any]] = []
    reaped: list[tuple[Any, Any]] = []
    monkeypatch.setattr(
        ssh_node, "get_job_type", lambda name: _kill_reap_spec(killed, reaped)
    )
    _fake_detached_identity(
        monkeypatch, start_time=654321 if identity == "start-time-mismatch" else 123456
    )
    handle = _identity_handle()
    if identity == "missing-identity":
        del handle["boot_id"]
    if trigger == "operator":
        rid = _mk_hung_job_with_kill_request(store, compute_handle=handle)
    else:
        rid = _mk_running_job(store, lease_offset_s=3600, compute_handle=handle)
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET meta = meta || jsonb_build_object('deadline', %s::float) "
                "WHERE ref_id = %s",
                (time.time() - 5, rid),
            )
            conn.commit()
    ctx = ssh_node._build_dispatch_context(
        store, rid, "identity test", _meta(store, rid)
    )

    assert ssh_node._poll_one(ctx) is True

    meta = _meta(store, rid)
    assert _status(store, rid) == "failed"
    if identity == "matched":
        assert killed == [(rid, handle)]
        assert reaped == [(rid, handle)]
        assert meta["compute_handle_released"] is True
        assert "detached_orphan_skipped" not in meta
    else:
        assert killed == []
        assert reaped == []
        assert not meta.get("compute_handle_released", False)
        assert meta["detached_orphan_skipped"]["reason"] == identity


def _legacy_deadline_context(store: Store) -> tuple[Any, dict[str, Any]]:
    handle = {"pid": 4242, "pgid": 4242, "dir": "/tmp/autocatpath-legacy-fake"}
    rid = _mk_running_job(
        store, lease_offset_s=3600, compute_handle=handle, target_node="spark"
    )
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || jsonb_build_object('deadline', %s::float) "
            "WHERE ref_id = %s",
            (time.time() - 5, rid),
        )
        conn.commit()
    return ssh_node._build_dispatch_context(
        store, rid, "legacy test", _meta(store, rid)
    ), handle


def test_legacy_own_deadline_polls_alive_then_kills(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    killed: list[tuple[Any, Any]] = []
    reaped: list[tuple[Any, Any]] = []
    base = _kill_reap_spec(killed, reaped)

    def poll(ctx: Any, handle: Any) -> bool:
        calls.append("poll")
        return False

    def kill(ctx: Any, handle: Any) -> None:
        calls.append("kill")
        killed.append((ctx.ref_id, handle))

    spec = replace(base, poll=poll, kill=kill)
    monkeypatch.setattr(ssh_node, "get_job_type", lambda name: spec)
    monkeypatch.setattr(ssh_node, "_detached_group_alive", lambda handle: True)
    monkeypatch.setenv("PRECIS_NODE", "spark")
    ctx, handle = _legacy_deadline_context(store)

    assert ssh_node._poll_one(ctx) is True

    assert calls == ["poll", "kill"]
    assert killed == [(ctx.ref_id, handle)]
    assert reaped == [(ctx.ref_id, handle)]
    assert _status(store, ctx.ref_id) == "failed"
    meta = _meta(store, ctx.ref_id)
    assert meta["compute_handle_released"] is True
    assert "detached_orphan_skipped" not in meta


@pytest.mark.parametrize(
    "veto",
    [
        "not-polled",
        "wrong-node",
        "wrong-executor",
        "terminal",
        "changed-handle",
        "operator",
    ],
)
def test_legacy_deadline_fallback_requires_current_owned_running_row(
    store: Store, monkeypatch: pytest.MonkeyPatch, veto: str
) -> None:
    killed: list[tuple[Any, Any]] = []
    reaped: list[tuple[Any, Any]] = []
    spec = _kill_reap_spec(killed, reaped)
    monkeypatch.setenv("PRECIS_NODE", "spark")
    monkeypatch.setattr(ssh_node, "_detached_group_alive", lambda handle: True)
    ctx, handle = _legacy_deadline_context(store)
    if veto == "terminal":
        store.add_tag(
            ctx.ref_id,
            Tag.closed("STATUS", "succeeded"),
            set_by="system",
            replace_prefix=True,
        )
    elif veto in {"wrong-node", "wrong-executor", "changed-handle"}:
        patch = {
            "wrong-node": {"params": {"target_node": "elsewhere"}},
            "wrong-executor": {"executor": "coordinator"},
            "changed-handle": {"compute_handle": {**handle, "pgid": 4343, "pid": 4343}},
        }[veto]
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET meta = meta || %s::jsonb WHERE ref_id = %s",
                (json.dumps(patch), ctx.ref_id),
            )
            conn.commit()

    ssh_node._kill_and_terminalize(
        ctx,
        handle,
        spec,
        swept_tag="killed-by-operator" if veto == "operator" else "wall-timeout",
        summary="legacy guard regression",
        polled_alive=veto != "not-polled",
    )

    assert killed == []
    assert reaped == []
    assert not _meta(store, ctx.ref_id).get("compute_handle_released", False)
    assert (
        _meta(store, ctx.ref_id)["detached_orphan_skipped"]["reason"]
        == "missing-identity"
    )


def test_legacy_operator_does_not_use_poll_fallback(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    killed: list[tuple[Any, Any]] = []
    reaped: list[tuple[Any, Any]] = []
    polled: list[Any] = []

    def poll(ctx: Any, handle: Any) -> bool:
        polled.append(handle)
        return False

    spec = replace(_kill_reap_spec(killed, reaped), poll=poll)
    monkeypatch.setattr(ssh_node, "get_job_type", lambda name: spec)
    monkeypatch.setattr(ssh_node, "_detached_group_alive", lambda handle: True)
    monkeypatch.setenv("PRECIS_NODE", "spark")
    ctx, _ = _legacy_deadline_context(store)
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || %s::jsonb WHERE ref_id = %s",
            (json.dumps({"kill_requested": {"actor": "operator"}}), ctx.ref_id),
        )
        conn.commit()
    ctx = ssh_node._build_dispatch_context(
        store, ctx.ref_id, "legacy operator", _meta(store, ctx.ref_id)
    )

    assert ssh_node._poll_one(ctx) is True

    assert polled == []
    assert killed == []
    assert reaped == []
    assert (
        _meta(store, ctx.ref_id)["detached_orphan_skipped"]["reason"]
        == "missing-identity"
    )


@pytest.mark.parametrize("pgid", [0, 1, None, 8888, "not-a-pgid"])
def test_invalid_group_never_probes_or_falls_back_to_pid(
    monkeypatch: pytest.MonkeyPatch, pgid: Any
) -> None:
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(ssh_node.os, "getpgrp", lambda: 8888)
    monkeypatch.setattr(
        ssh_node.os, "killpg", lambda pg, sig: signals.append((pg, sig))
    )
    handle = {**_identity_handle(), "pgid": pgid}
    if pgid is None:
        del handle["pgid"]

    assert ssh_node._detached_group_alive(handle) is False
    assert ssh_node._detached_identity_reason(handle) == "invalid-handle"
    assert signals == []


@pytest.mark.parametrize("path", ["orphan", "poison", "operator", "deadline"])
def test_zero_pgid_is_never_killed_in_any_terminal_path(
    store: Store, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    killed: list[tuple[Any, Any]] = []
    reaped: list[tuple[Any, Any]] = []
    monkeypatch.setattr(
        ssh_node, "get_job_type", lambda name: _kill_reap_spec(killed, reaped)
    )
    _fake_detached_identity(monkeypatch)
    monkeypatch.setenv("PRECIS_NODE", "spark")
    handle = {**_identity_handle(), "pgid": 0}
    if path == "orphan":
        rid = _mk_terminal_handle_job(store, compute_handle=handle)
        ssh_node._reap_detached_orphans(store, "spark")
    elif path == "poison":
        store.record_heartbeat(
            "spark-poison", meta={"boot_ids": {"ssh_node": "new-generation"}}
        )
        rid = _mk_running_job(
            store,
            lease_offset_s=3600,
            attempts=ssh_node._MAX_ATTEMPTS,
            lease_boot_id="dead-generation",
            lease_process="ssh_node",
            lease_host="spark-poison",
            compute_handle=handle,
            target_node="spark",
        )
        ssh_node.run_ssh_node_pass(store, limit=2)
    else:
        rid = _mk_running_job(
            store, lease_offset_s=3600, compute_handle=handle, target_node="spark"
        )
        patch: dict[str, Any] = {"deadline": time.time() - 5}
        if path == "operator":
            patch = {"kill_requested": {"actor": "operator"}}
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET meta = meta || %s::jsonb WHERE ref_id = %s",
                (json.dumps(patch), rid),
            )
            conn.commit()
        ctx = ssh_node._build_dispatch_context(
            store, rid, "zero pgid", _meta(store, rid)
        )
        ssh_node._poll_one(ctx)

    assert killed == []
    assert reaped == []
    meta = _meta(store, rid)
    assert meta["detached_orphan_skipped"]["reason"] == "invalid-handle"
    assert not meta.get("compute_handle_released", False)


def test_empty_dict_handle_cannot_bypass_terminal_identity_guard(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    killed: list[tuple[Any, Any]] = []
    reaped: list[tuple[Any, Any]] = []
    spec = _kill_reap_spec(killed, reaped)
    monkeypatch.setenv("PRECIS_NODE", "spark")
    ctx, _ = _legacy_deadline_context(store)

    ssh_node._kill_and_terminalize(
        ctx,
        {},
        spec,
        swept_tag="wall-timeout",
        summary="malformed handle",
        polled_alive=True,
    )

    assert killed == []
    assert reaped == []
    assert (
        _meta(store, ctx.ref_id)["detached_orphan_skipped"]["reason"]
        == "invalid-handle"
    )


@pytest.mark.parametrize("unavailable", ["group", "identity"])
def test_deadline_unavailable_group_reaps_only_after_terminal_stamp(
    store: Store, monkeypatch: pytest.MonkeyPatch, unavailable: str
) -> None:
    killed: list[tuple[Any, Any]] = []
    reaped: list[tuple[Any, Any]] = []
    completion_stamps: list[bool] = []

    def reap(ctx: Any, handle: Any) -> None:
        assert _status(store, ctx.ref_id) == "failed"
        completion_stamps.append(bool(_meta(store, ctx.ref_id).get("finished_at")))
        reaped.append((ctx.ref_id, handle))

    spec = replace(_kill_reap_spec(killed, reaped), reap=reap)
    monkeypatch.setattr(ssh_node, "get_job_type", lambda name: spec)
    _fake_detached_identity(
        monkeypatch, error=PermissionError() if unavailable == "identity" else None
    )
    monkeypatch.setattr(ssh_node, "_detached_group_alive", lambda handle: False)
    handle = _identity_handle()
    rid = _mk_running_job(store, lease_offset_s=3600, compute_handle=handle)
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || jsonb_build_object('deadline', %s::float) "
            "WHERE ref_id = %s",
            (time.time() - 5, rid),
        )
        conn.commit()
    ctx = ssh_node._build_dispatch_context(
        store, rid, "unavailable group", _meta(store, rid)
    )

    assert ssh_node._poll_one(ctx) is True

    assert killed == []
    reason = "group-unavailable" if unavailable == "group" else "identity-unreadable"
    assert _meta(store, rid)["detached_orphan_skipped"]["reason"] == reason
    if unavailable == "group":
        assert reaped == [(rid, handle)]
        assert completion_stamps == [True]
    else:
        assert reaped == []
        assert completion_stamps == []


def test_legacy_deadline_locked_row_defers_unchanged_then_retries(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NOWAIT keeps a legacy poll responsive while another transaction owns its row."""
    killed: list[tuple[Any, Any]] = []
    reaped: list[tuple[Any, Any]] = []
    polled: list[Any] = []

    def poll(ctx: Any, handle: Any) -> bool:
        polled.append(handle)
        return False

    spec = replace(_kill_reap_spec(killed, reaped), poll=poll)
    monkeypatch.setattr(ssh_node, "get_job_type", lambda name: spec)
    monkeypatch.setattr(ssh_node, "_detached_group_alive", lambda handle: True)
    monkeypatch.setenv("PRECIS_NODE", "spark")
    ctx, handle = _legacy_deadline_context(store)
    before_meta = _meta(store, ctx.ref_id)
    before_tags = sorted(str(tag) for tag in store.tags_for(ctx.ref_id))
    before_chunks = [
        chunk.text for chunk in store.chunks.list_chunks_for_ref(ctx.ref_id)
    ]
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        with store.pool.connection() as locker:
            locker.execute(
                "SELECT ref_id FROM refs WHERE ref_id = %s FOR UPDATE", (ctx.ref_id,)
            )
            future = executor.submit(ssh_node._poll_one, ctx)
            try:
                # A missing NOWAIT fails within 5s; finally unlocks BEFORE
                # executor.shutdown waits, so the regression cannot hang here.
                assert future.result(timeout=5) is False
                assert killed == []
                assert reaped == []
                assert _meta(store, ctx.ref_id) == before_meta
                assert (
                    sorted(str(tag) for tag in store.tags_for(ctx.ref_id))
                    == before_tags
                )
                assert [
                    chunk.text for chunk in store.chunks.list_chunks_for_ref(ctx.ref_id)
                ] == before_chunks
            finally:
                locker.rollback()
    finally:
        executor.shutdown(wait=True)

    assert ssh_node._poll_one(ctx) is True

    assert polled == [handle, handle]
    assert killed == [(ctx.ref_id, handle)]
    assert reaped == [(ctx.ref_id, handle)]
    assert _status(store, ctx.ref_id) == "failed"
    assert _meta(store, ctx.ref_id)["compute_handle_released"] is True


@pytest.mark.parametrize("path", ["orphan", "deadline"])
@pytest.mark.parametrize("group", ["dead", "alive", "permission-denied"])
def test_missing_leader_never_kills_and_reaps_only_proven_dead_group(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    group: str,
) -> None:
    killed: list[tuple[Any, Any]] = []
    reaped: list[tuple[Any, Any]] = []
    probes: list[tuple[int, int]] = []
    monkeypatch.setattr(
        ssh_node, "get_job_type", lambda name: _kill_reap_spec(killed, reaped)
    )
    original_read_text = Path.read_text

    def read_text(proc_path: Path, *args: Any, **kwargs: Any) -> str:
        if str(proc_path) == "/proc/sys/kernel/random/boot_id":
            return "boot-for-test\n"
        if str(proc_path) == "/proc/4242/stat":
            raise FileNotFoundError("group leader has exited")
        return original_read_text(proc_path, *args, **kwargs)

    def killpg(pgid: int, signal: int) -> None:
        probes.append((pgid, signal))
        assert signal == 0  # identity uncertainty must never send a real signal
        if group == "dead":
            raise ProcessLookupError("process group is gone")
        if group == "permission-denied":
            raise PermissionError("cannot inspect group")

    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(ssh_node.os, "getpgrp", lambda: 8888)
    monkeypatch.setattr(ssh_node.os, "killpg", killpg)
    handle = _identity_handle()
    if path == "orphan":
        rid = _mk_terminal_handle_job(store, compute_handle=handle)
        ssh_node._reap_detached_orphans(store, "spark")
    else:
        rid = _mk_running_job(store, lease_offset_s=3600, compute_handle=handle)
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET meta = meta || jsonb_build_object('deadline', %s::float) "
                "WHERE ref_id = %s",
                (time.time() - 5, rid),
            )
            conn.commit()
        ctx = ssh_node._build_dispatch_context(
            store, rid, "missing group leader", _meta(store, rid)
        )
        assert ssh_node._poll_one(ctx) is True

    assert probes
    assert all(probe == (4242, 0) for probe in probes)
    assert killed == []
    assert reaped == ([(rid, handle)] if group == "dead" else [])
    reason = "group-unavailable" if group == "dead" else "identity-unreadable"
    meta = _meta(store, rid)
    assert meta["detached_orphan_skipped"]["reason"] == reason
    assert not meta.get("compute_handle_released", False)


@pytest.mark.parametrize("path", ["orphan", "deadline"])
def test_seed_kill_refusal_does_not_release_or_reap_handle(
    store: Store, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    from precis_pathway import scratch, seed_job

    attempted: list[tuple[Any, bool]] = []
    reaped: list[tuple[Any, Any]] = []

    def refuse(handle: Any, *, keep_scratch: bool = False) -> bool:
        attempted.append((handle, keep_scratch))
        return False

    monkeypatch.setattr(scratch, "kill_seed_partial_detached", refuse)
    spec = replace(_kill_reap_spec([], reaped), kill=seed_job._kill)
    monkeypatch.setattr(ssh_node, "get_job_type", lambda name: spec)
    _fake_detached_identity(monkeypatch)
    handle = _identity_handle()
    if path == "orphan":
        rid = _mk_terminal_handle_job(store, compute_handle=handle)
        ssh_node._reap_detached_orphans(store, "spark")
        assert _meta(store, rid)["detached_orphan_skipped"]["reason"] == "kill-raised"
    else:
        rid = _mk_running_job(store, lease_offset_s=3600, compute_handle=handle)
        ctx = ssh_node._build_dispatch_context(
            store, rid, "refused kill", _meta(store, rid)
        )
        assert (
            ssh_node._kill_and_terminalize(
                ctx, handle, spec, swept_tag="wall-timeout", summary="refused kill"
            )
            is True
        )

    assert attempted == [(handle, True)]
    assert reaped == []
    assert not _meta(store, rid).get("compute_handle_released", False)
