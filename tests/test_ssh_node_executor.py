"""ssh_node executor — claim → lease → STATUS:running → plugin dispatch.

DB-backed (the claim is real SQL). A monkeypatched job_type stands in
for precis-dft's gpaw_relax so the test exercises the executor flow
without that plugin installed.
"""

from __future__ import annotations

import time
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
    lease_boot_id: str | None = None,
    lease_process: str | None = None,
    lease_host: str | None = None,
    compute_handle: Any = None,
) -> int:
    """A STATUS:running job with a lease ``lease_offset_s`` from now (negative =
    expired) — stands in for a job whose worker died mid-dispatch. The
    ``lease_boot_id``/``lease_process``/``lease_host`` trio (§H,
    compute-lane-lease-epoch.md) stands in for a PRIOR claim's stamp — the
    generation that (allegedly) died. ``compute_handle`` (§H piece 4) stands
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


# ── §H boot epoch: epoch-aware reclaim (compute-lane-lease-epoch.md) ──


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


def test_epoch_reclaim_does_not_bump_attempts(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The master's "redeploy mid-run does not burn the poison guard" —
    an epoch reclaim leaves meta.attempts untouched, only the forensic
    reclaims list grows."""
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
    assert meta["attempts"] == 1  # unchanged — this was an epoch reclaim
    assert meta["reclaims"][-1]["why"] == "epoch"


def test_interleaved_epoch_reclaims_never_advance_attempts(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two SUCCESSIVE epoch reclaims (two different worker generations, each
    a redeploy — never a crash-loop) never bump meta.attempts, no matter how
    many times the row changes hands this way."""
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
        attempts=1,
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
    assert meta["attempts"] == 1  # STILL unchanged — two epoch reclaims, zero bumps
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
    events = [
        c.text
        for c in store.list_blocks_for_ref(rid)
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
    assert _status(store, rid) == "failed"


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
        for c in store.list_blocks_for_ref(rid)
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
        for c in store.list_blocks_for_ref(rid)
        if getattr(c, "chunk_kind", None) == "job_event"
    ]
    assert any("re-adopted" in t for t in events)
