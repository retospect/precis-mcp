"""ssh_node executor — claim → lease → STATUS:running → plugin dispatch.

DB-backed (the claim is real SQL). A monkeypatched job_type stands in
for precis-dft's gpaw_relax so the test exercises the executor flow
without that plugin installed.
"""

from __future__ import annotations

import json
import signal
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest
from psycopg.types.json import Jsonb

from precis.dispatch import Hub
from precis.store import Store
from precis.store.types import Tag
from precis.workers.executors import EXECUTOR_PROVIDES, ssh_node
from precis.workers.executors._common import claim_executor_jobs
from precis.workers.job_types import JobTypeSpec, get_job_type, run_slice

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


def test_parent_gate_skips_open_parent_with_any_pause_tag(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate is the whole pause vocabulary: an OPEN parent carrying
    ``waiting-for:`` or ``ask-user`` still blocks its jobs — gr575883's
    carve-out is terminal-parent-only."""
    _succeeds(ssh_node, monkeypatch)
    waiting = store.insert_ref(kind="todo", slug=None, title="waiting", meta={})
    store.add_tag(waiting.id, Tag.parse_strict("waiting-for:fleet"), set_by="agent")
    blocked_w = _mk_job(store, parent_id=waiting.id)
    asking = store.insert_ref(kind="todo", slug=None, title="asking", meta={})
    store.add_tag(asking.id, Tag.parse_strict("ask-user"), set_by="agent")
    blocked_a = _mk_job(store, parent_id=asking.id)

    ssh_node.run_ssh_node_pass(store, limit=5)
    assert _status(store, blocked_w) == "queued"
    assert _status(store, blocked_a) == "queued"


@pytest.mark.parametrize("terminal", ["done", "won't-do"])
def test_parent_gate_ignores_terminal_parent(
    store: Store, monkeypatch: pytest.MonkeyPatch, terminal: str
) -> None:
    """gr575883: a parent closed ``done``/``won't-do`` whose
    ``waiting-for:`` tag was never cleared is finished — the stale pause
    tag must not pin its queued jobs forever."""
    _succeeds(ssh_node, monkeypatch)
    done = store.insert_ref(kind="todo", slug=None, title="done", meta={})
    store.add_tag(done.id, Tag.parse_strict(f"STATUS:{terminal}"), set_by="agent")
    store.add_tag(done.id, Tag.parse_strict("waiting-for:fleet"), set_by="agent")
    rid = _mk_job(store, parent_id=done.id)

    assert ssh_node.run_ssh_node_pass(store, limit=2)["claimed"] == 1
    assert _status(store, rid) == "succeeded"


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


def test_poison_guard_release_error_does_not_undo_failure(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    released: list[int] = []

    def release(ctx: Any, handle: Any) -> None:
        released.append(ctx.ref_id)
        raise RuntimeError("release failed")

    spec = replace(_spec(), release=release)
    monkeypatch.setattr(ssh_node, "get_job_type", lambda name: spec)
    rid = _mk_running_job(store, lease_offset_s=-60, attempts=ssh_node._MAX_ATTEMPTS)

    result = ssh_node.run_ssh_node_pass(store, limit=2)

    assert result == {"claimed": 1, "ok": 0, "failed": 1}
    assert released == [rid]
    assert _status(store, rid) == "failed"
    meta = _meta(store, rid)
    assert meta["failure_class"] == "infra"
    assert "crash-loop guard" in meta["error"]


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


def test_unpinned_submit_is_polled_by_the_node_that_launched_it(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """542535/542536: an un-pinned detached job's compute lives on the
    submitting host. Submit pins it there, so the next pass on that node
    polls it to terminal and no other node can poll or reclaim it."""
    polled: list[str] = []

    def _poll(ctx: Any, handle: str) -> bool:
        polled.append(handle)
        ctx.set_status("succeeded")
        return True

    monkeypatch.setattr(
        ssh_node,
        "get_job_type",
        lambda name: _spec(submit=lambda c, s: "pid-7", poll=_poll),
    )
    monkeypatch.setenv("PRECIS_NODE", "node-a")
    rid = _mk_job(store, params={"keep": 1})

    ssh_node.run_ssh_node_pass(store, limit=1)

    params = _meta(store, rid)["params"]
    assert params == {"keep": 1, "target_node": "node-a"}
    assert polled == []  # submit pass: the poll step ran before the claim

    monkeypatch.setenv("PRECIS_NODE", "node-b")
    assert ssh_node._polling_jobs(store, "node-b") == []
    ssh_node.run_ssh_node_pass(store, limit=1)
    assert polled == []

    monkeypatch.setenv("PRECIS_NODE", "node-a")
    result = ssh_node.run_ssh_node_pass(store, limit=1)
    assert polled == ["pid-7"]
    assert result["ok"] == 1
    assert _status(store, rid) == "succeeded"


def test_submit_on_a_node_less_worker_stays_unpinned(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Single-machine install: no PRECIS_NODE, so the NULL pin stays and the
    node-less poll step selects it."""
    monkeypatch.delenv("PRECIS_NODE", raising=False)
    monkeypatch.setattr(
        ssh_node,
        "get_job_type",
        lambda name: _spec(submit=lambda c, s: "pid-8", poll=lambda c, h: False),
    )
    rid = _mk_job(store, params={"keep": 1})

    ssh_node.run_ssh_node_pass(store, limit=1)

    assert _meta(store, rid)["params"] == {"keep": 1}
    assert [row[0] for row in ssh_node._polling_jobs(store, None)] == [rid]


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


def test_poll_still_running_persists_log_freshness(
    store: Store, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """td639132: the non-terminal poll runs on the job's target host, so
    the session-log mtime it stats is real freshness — persisted to
    ``meta.log_fresh_at`` for the unpinned ``factory_rescue_tick`` to
    judge cluster-wide."""
    log_file = tmp_path / "run_slice.log"
    log_file.write_text("progress\n", encoding="utf-8")
    polled: list[Any] = []

    def _poll(ctx: Any, handle: Any) -> bool:
        polled.append(handle)
        return False

    monkeypatch.setattr(
        ssh_node,
        "get_job_type",
        lambda name: _spec(poll=_poll, submit=lambda c, s: None),
    )
    handle = {"pid": 1, "log": str(log_file)}
    rid = _mk_running_job(store, lease_offset_s=-60, compute_handle=handle)

    result = ssh_node.run_ssh_node_pass(store, limit=2)

    assert result == {"claimed": 0, "ok": 0, "failed": 0}
    assert polled == [handle]
    stamp = _meta(store, rid).get("log_fresh_at")
    assert stamp is not None
    assert datetime.fromisoformat(stamp) == datetime.fromtimestamp(
        log_file.stat().st_mtime, UTC
    )


def test_poll_still_running_without_a_log_writes_no_stamp(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A handle with no readable ``log`` stamps nothing — the key stays
    absent rather than going null (td639132)."""
    monkeypatch.setattr(
        ssh_node,
        "get_job_type",
        lambda name: _spec(poll=lambda c, h: False, submit=lambda c, s: None),
    )
    rid = _mk_running_job(store, lease_offset_s=-60, compute_handle={"pid": 1})

    result = ssh_node.run_ssh_node_pass(store, limit=2)

    assert result == {"claimed": 0, "ok": 0, "failed": 0}
    assert "log_fresh_at" not in _meta(store, rid)


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


def test_poll_past_deadline_applies_deadline_kill_meta(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr647914 / td648501: a spec's ``deadline_kill_meta`` rides the
    wall-timeout terminalize write AND switches the terminal status —
    ``land_slice`` marks the kill attempt-free and caps the re-land
    lane, so the job reads as a distinct ``STATUS:timed-out`` verdict,
    never a gate failure."""
    monkeypatch.setattr(
        ssh_node,
        "get_job_type",
        lambda name: replace(
            _spec(submit=lambda c, s: None, poll=lambda c, h: False),
            deadline_kill_meta={
                "attempt_free": "infra-land",
                "red_class": "timed-out",
                "land_again_cap": True,
            },
        ),
    )
    rid = _mk_running_job(
        store, lease_offset_s=3600, compute_handle="remote-pid-timed-out"
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
    assert _status(store, rid) == "timed-out"
    assert "swept:wall-timeout" in {str(t) for t in store.tags_for(rid)}
    meta = _meta(store, rid)
    assert meta["failure_class"] == "timeout"
    assert meta["attempt_free"] == "infra-land"
    assert meta["red_class"] == "timed-out"
    assert meta["land_again_cap"] is True


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


@pytest.mark.parametrize(
    ("tier", "wall_seconds", "elapsed_hours"),
    [("neb", 86400, 3), ("verify", 172800, 26)],
)
def test_seed_survives_previous_deadline_and_is_killed_past_its_wall(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    tier: str,
    wall_seconds: int,
    elapsed_hours: int,
) -> None:
    """gr614723: general/verify seeds outlive the previous 2.5 h/25 h
    deadlines. They remain polled within the new wall plus one-hour margin;
    a seed beyond its new deadline still fails as ``timeout``."""
    from precis.quest import compute

    monkeypatch.delenv(compute._AUTOCATPATH_WALL_SECONDS_ENV, raising=False)
    monkeypatch.delenv(compute._AUTOCATPATH_VERIFY_WALL_SECONDS_ENV, raising=False)
    params = {"resources": {"wall_seconds": compute._autocatpath_wall_seconds(tier)}}
    bound = ssh_node._wall_bound_seconds({"params": params})
    assert bound == wall_seconds + 3600

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
            description="fake autocatpath seed",
            run=lambda *a, **k: "noop",
            submit=lambda c, s: None,
            poll=_poll,
            kill=_kill,
        ),
    )
    alive = _mk_running_job(
        store, lease_offset_s=3600, compute_handle=f"seed-{elapsed_hours}h"
    )
    over = _mk_running_job(store, lease_offset_s=3600, compute_handle="seed-over")
    now = time.time()
    with store.pool.connection() as conn:
        for rid, started in (
            (alive, now - elapsed_hours * 3600),
            (over, now - bound - 5),
        ):
            conn.execute(
                "UPDATE refs SET meta = meta || jsonb_build_object("
                "'params', %s::jsonb, 'deadline', %s::float) WHERE ref_id = %s",
                (json.dumps(params), started + bound, rid),
            )
        conn.commit()

    result = ssh_node.run_ssh_node_pass(store, limit=2)

    assert result == {"claimed": 0, "ok": 1, "failed": 0}
    assert polled == [f"seed-{elapsed_hours}h"]
    assert _status(store, alive) == "running"
    assert killed == [(over, "seed-over")]
    assert _status(store, over) == "failed"
    assert _meta(store, over)["failure_class"] == "timeout"


def test_submit_deadline_reflects_an_extension_during_submit(
    store: Store, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """put(mode='extend') bumps ``params.resources.wall_seconds`` under
    the same row lock the handle-persist re-reads — an extension landing
    between the claim snapshot and the deadline stamp must be priced
    into the deadline, not silently lost to a stale-snapshot stamp
    (td627156)."""
    from precis.handlers.job import JobHandler

    jobs = JobHandler(hub=hub)

    def _submit(ctx: Any, _spec: Any) -> str:
        # The mid-submit extension: after the claim snapshot, before the
        # handle/deadline persist — the race the reread exists for.
        jobs.put(id=ctx.ref_id, mode="extend", seconds=3600, reason="mid-submit")
        return "remote-pid-extended"

    monkeypatch.setattr(
        ssh_node,
        "get_job_type",
        lambda name: _spec(submit=_submit, poll=lambda c, h: False),
    )
    rid = _mk_job(store, params={"resources": {"wall_seconds": 1800}})
    before = time.time()

    ssh_node.run_ssh_node_pass(store, limit=2)

    meta = _meta(store, rid)
    # The stale snapshot would stamp ~before + 1800 + margin; the
    # extended budget is 1800 + 3600 = 5400, plus margin.
    extended = before + 5400 + ssh_node._LEASE_MARGIN_S
    assert meta["deadline"] >= extended - 5
    assert meta["deadline"] <= extended + 30


def test_poll_rechecks_the_deadline_under_the_row_lock(
    store: Store, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The poll's deadline read comes from the pass-start ``_polling_jobs``
    snapshot — an extension committing between that snapshot and the kill
    write must win. The kill re-reads ``meta.deadline`` under the same
    row lock the extension holds, so a stale past-deadline snapshot
    cannot kill live compute (td627156)."""
    from precis.handlers.job import JobHandler

    killed: list[Any] = []
    monkeypatch.setattr(
        ssh_node,
        "get_job_type",
        lambda name: JobTypeSpec(
            name="fake_relax",
            params_schema={"type": "object"},
            compatible_executors=frozenset({"ssh_node"}),
            requires=frozenset(),
            description="fake relax for tests",
            run=lambda *a, **k: "noop",
            submit=lambda c, s: None,
            poll=lambda c, h: False,
            kill=lambda c, h: killed.append(h),
        ),
    )
    rid = _mk_running_job(store, lease_offset_s=3600, compute_handle="remote-pid-race")
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || jsonb_build_object('deadline', %s::float) "
            "WHERE ref_id = %s",
            (time.time() - 5, rid),
        )
        conn.commit()

    jobs = JobHandler(hub=hub)
    real_select = ssh_node._polling_jobs

    def _select_then_extend(store_: Store, node_: str | None) -> Any:
        rows = real_select(store_, node_)
        # The extension commits AFTER the poll snapshot, BEFORE the kill
        # decision — the stale snapshot still holds the past deadline.
        jobs.put(id=rid, mode="extend", seconds=7200, reason="more time")
        return rows

    monkeypatch.setattr(ssh_node, "_polling_jobs", _select_then_extend)

    result = ssh_node.run_ssh_node_pass(store, limit=2)

    # Still running: the re-read saw the extended (future) deadline and
    # deferred — no kill hook, no terminal write.
    assert result == {"claimed": 0, "ok": 0, "failed": 0}
    assert killed == []
    assert _status(store, rid) == "running"
    assert "swept:wall-timeout" not in {str(t) for t in store.tags_for(rid)}
    assert "failure_class" not in _meta(store, rid)
    # The extension itself did land — only the kill deferred.
    assert len(store.events_for(rid, source="job-extend")) == 1


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


def test_orphan_lookup_plan_uses_partial_index(store: Store) -> None:
    """EXPLAIN of the reaper's own statement picks ``refs_orphan_handle_idx``.

    Prod's plan (gr573350) aggregated ~164k terminal STATUS ``ref_tags``
    rows and probed ``refs_ref_id_kind_key`` once per row; the partial
    index starts from the handful of unreleased ``boot_id`` handles. On a
    tiny test table the planner could still prefer a seq scan or the
    generic ``refs_kind_idx``, so both alternatives are forbidden rather
    than weakening the assert.
    """
    _mk_terminal_handle_job(store, compute_handle=_identity_handle())
    _mk_terminal_handle_job(store, compute_handle=_identity_handle(), released=True)
    _mk_terminal_handle_job(store, compute_handle={"pid": 1})
    _mk_job(store)
    with store.pool.connection() as conn:
        conn.execute("ANALYZE refs")
        conn.execute("SET LOCAL enable_seqscan = off")
        conn.execute("SET LOCAL enable_bitmapscan = off")
        plan = "\n".join(
            str(r[0])
            for r in conn.execute(
                "EXPLAIN " + ssh_node._ORPHANED_DETACHED_SQL,
                (
                    ssh_node._EXECUTOR_NAME,
                    "spark",
                    list(ssh_node._TERMINAL),
                    ssh_node._ORPHAN_REAP_GRACE_S,
                    ssh_node._ORPHAN_REAP_LIMIT,
                ),
            ).fetchall()
        )
    assert "refs_orphan_handle_idx" in plan, plan
    assert "refs_kind_idx" not in plan, plan


def test_orphan_lookup_migration_predicate_matches(store: Store) -> None:
    """The index predicate is the statement's WHERE, kept in step.

    The planner only uses a partial index when the query's WHERE implies
    its predicate; an edit to ``_ORPHANED_DETACHED_SQL`` that breaks that
    implication turns the index dead weight, so the applied predicate is
    pinned clause-for-clause here.
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT i.indisvalid, pg_get_expr(i.indpred, i.indrelid) "
            "FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
            "WHERE c.relname = 'refs_orphan_handle_idx'"
        ).fetchone()
    assert row is not None, "refs_orphan_handle_idx missing — migration not applied"
    valid, pred = row
    assert valid
    for fragment in (
        "kind = 'job'::text",
        "retired_at IS NULL",
        "'boot_id'::text",
        "IS DISTINCT FROM 'true'::text",
        "detached_orphan_reaped",
        "detached_orphan_skipped",
    ):
        assert fragment in pred, pred


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


@pytest.mark.parametrize("handle", [None, {"pid": 4242, "pgid": 4242}])
def test_poison_guard_releases_run_slice_todo_without_process_identity(
    store: Store, monkeypatch: pytest.MonkeyPatch, handle: Any
) -> None:
    """gr546830: refusing an unverifiable kill must not strand the todo."""
    monkeypatch.setenv("PRECIS_NODE", "spark")
    store.record_heartbeat("spark")
    store.sync_host_resource_slots(
        "spark", {"repo_checkout": 1, "claude_bin": 1, "git": 1}
    )
    store.record_heartbeat(
        "spark-poison", meta={"boot_ids": {"ssh_node": "new-generation"}}
    )
    todo = store.insert_ref(kind="todo", slug=None, title="slice")
    store.add_tag(todo.id, Tag.closed("STATUS", "doing"), set_by="system")
    store.add_tag(todo.id, Tag.open("claimed-by:lane-claude-sub"), set_by="system")
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
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || %s::jsonb WHERE ref_id = %s",
            (
                Jsonb(
                    {
                        "job_type": "run_slice",
                        "lane": "claude-sub",
                        "params": {"todo_id": todo.id, "target_node": "spark"},
                    }
                ),
                rid,
            ),
        )
        conn.commit()
    releases: list[int] = []
    release_todo = run_slice.release_todo

    def release(store: Any, todo_id: int, lane: str | None) -> int:
        releases.append(todo_id)
        return release_todo(store, todo_id, lane)

    killed: list[Any] = []
    spec = get_job_type("run_slice")
    assert spec is not None
    spec = replace(
        spec,
        poll=lambda ctx, handle: False,
        kill=lambda ctx, handle: killed.append(handle),
    )
    monkeypatch.setattr(run_slice, "release_todo", release)
    monkeypatch.setattr(ssh_node, "get_job_type", lambda name: spec)
    result = ssh_node.run_ssh_node_pass(store, limit=2)

    assert result == {"claimed": 1, "ok": 0, "failed": 1}
    assert _status(store, rid) == "failed"
    assert releases == [todo.id]
    assert killed == []
    assert _status(store, todo.id) == "open"
    tags = {str(t) for t in store.tags_for(todo.id)}
    assert "claimed-by:lane-claude-sub" not in tags
    assert "attempt:1" in tags
    ssh_node.run_ssh_node_pass(store, limit=2)
    assert releases == [todo.id]


# ── gr541147: a restart re-adopting a live detached compute is not a crash ──


def _restart_reclaimed(store: Store, ref_id: int, *, deadline: float | None) -> None:
    """Stand in for one more worker restart: the row's lease identity names
    a dead generation again, as after every deploy."""
    fields: dict[str, Any] = {"lease_boot_id": "dead-generation"}
    if deadline is not None:
        fields["deadline"] = deadline
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || %s::jsonb WHERE ref_id = %s",
            (Jsonb(fields), ref_id),
        )
        conn.commit()


def _detached_restart_job(
    store: Store, *, attempts: int, deadline: float | None
) -> tuple[int, dict[str, Any]]:
    store.record_heartbeat(
        "spark-readopt", meta={"boot_ids": {"ssh_node": "new-generation"}}
    )
    handle = _identity_handle()
    rid = _mk_running_job(
        store,
        lease_offset_s=3600,
        attempts=attempts,
        lease_boot_id="dead-generation",
        lease_process="ssh_node",
        lease_host="spark-readopt",
        compute_handle=handle,
        target_node="spark",
    )
    _restart_reclaimed(store, rid, deadline=deadline)
    return rid, handle


def test_live_detached_handle_survives_repeated_restart_reclaims(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """jo543547: a 3.5 h seed re-adopted across deploys was failed on the
    third restart and its healthy child killed.  A handle proven alive
    (boot ID + start time + group) inside its deadline is re-adopted on
    every restart without spending an attempt."""
    killed: list[tuple[Any, Any]] = []
    reaped: list[tuple[Any, Any]] = []
    monkeypatch.setattr(
        ssh_node, "get_job_type", lambda name: _kill_reap_spec(killed, reaped)
    )
    _fake_detached_identity(monkeypatch)
    monkeypatch.setenv("PRECIS_NODE", "spark")
    deadline = time.time() + 7200
    rid, _ = _detached_restart_job(store, attempts=0, deadline=deadline)

    for _ in range(ssh_node._MAX_ATTEMPTS + 2):
        ssh_node.run_ssh_node_pass(store, limit=2)
        _restart_reclaimed(store, rid, deadline=deadline)

    meta = _meta(store, rid)
    assert _status(store, rid) == "running"
    assert killed == []
    assert meta["attempts"] == 0
    assert {r["why"] for r in meta["reclaims"]} == {"readopt"}


def _parked_handle() -> dict[str, Any]:
    """land_slice's deferral state: no pid/pgid/identity, just the wait."""
    return {
        "worktree": "/tmp/fake-slice-td42",
        "subject": "feat(x): y (td42)",
        "flag": "--quick",
        "main_refusals": 2,
        "not_before": "2099-01-01T00:00:00Z",
    }


def test_parked_handle_is_readoptable_on_its_deadline_alone() -> None:
    """A plugin that defers (land_slice) parks a handle with no process; the
    identity and group probes cannot apply, so only the node pin and the
    deadline gate re-adoption."""
    future = time.time() + 7200
    meta: dict[str, Any] = {
        "compute_handle": _parked_handle(),
        "params": {"target_node": "spark"},
        "deadline": future,
    }
    assert ssh_node._readoptable(meta, "spark") is True
    assert ssh_node._readoptable(meta, "other-node") is False
    assert (
        ssh_node._readoptable({**meta, "deadline": time.time() - 60}, "spark") is False
    )
    assert ssh_node._readoptable({**meta, "deadline": None}, "spark") is False
    assert (
        ssh_node._readoptable({**meta, "compute_handle_released": True}, "spark")
        is False
    )
    # without the marker the same pid-less handle is not re-adoptable
    bare = {k: v for k, v in _parked_handle().items() if k != "not_before"}
    assert ssh_node._readoptable({**meta, "compute_handle": bare}, "spark") is False


def test_parked_handle_survives_repeated_restart_reclaims(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every restart while a land is parked re-adopts it and refunds the
    attempt (before: one attempt each, the cap-th restart failed the job).
    Nothing is killed and no orphan-skip is recorded."""
    killed: list[tuple[Any, Any]] = []
    reaped: list[tuple[Any, Any]] = []
    monkeypatch.setattr(
        ssh_node, "get_job_type", lambda name: _kill_reap_spec(killed, reaped)
    )
    monkeypatch.setenv("PRECIS_NODE", "spark")
    deadline = time.time() + 7200
    store.record_heartbeat(
        "spark-readopt", meta={"boot_ids": {"ssh_node": "new-generation"}}
    )
    rid = _mk_running_job(
        store,
        lease_offset_s=3600,
        attempts=0,
        lease_boot_id="dead-generation",
        lease_process="ssh_node",
        lease_host="spark-readopt",
        compute_handle=_parked_handle(),
        target_node="spark",
    )
    _restart_reclaimed(store, rid, deadline=deadline)

    for _ in range(ssh_node._MAX_ATTEMPTS + 2):
        ssh_node.run_ssh_node_pass(store, limit=2)
        _restart_reclaimed(store, rid, deadline=deadline)

    meta = _meta(store, rid)
    assert _status(store, rid) == "running"
    assert killed == [] and reaped == []
    assert meta["attempts"] == 0
    assert {r["why"] for r in meta["reclaims"]} == {"readopt"}
    assert "detached_orphan_skipped" not in meta
    assert "detached_orphan_reaped" not in meta
    events = [
        c.text
        for c in store.chunks.list_chunks_for_ref(rid)
        if getattr(c, "chunk_kind", None) == "job_event"
    ]
    assert any("found parked detached job" in t for t in events)


def test_a_requeued_parked_handle_is_re_adopted_by_a_fresh_claim(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr651885: a frozen land gives its slot back (``waiting_time``) and
    the wake_runner re-queues it with its parked handle still on the row.
    The fresh claim re-adopts the handle — never a second ``submit``, no
    attempt bump — and the next pass polls it, which re-reads the freeze."""
    submitted: list[int] = []
    polled: list[Any] = []

    def _submit(ctx: Any, _spec: Any) -> Any:  # pragma: no cover — must not fire
        submitted.append(ctx.ref_id)
        return None

    def _poll(ctx: Any, handle: Any) -> bool:
        polled.append(handle)
        return False

    monkeypatch.setattr(
        ssh_node, "get_job_type", lambda name: _spec(submit=_submit, poll=_poll)
    )
    monkeypatch.setenv("PRECIS_NODE", "spark")
    handle = {**_parked_handle(), "not_before": "2020-01-01T00:00:00Z"}
    ref = store.insert_ref(
        kind="job",
        slug=None,
        title="frozen land, re-queued",
        meta={
            "executor": "ssh_node",
            "job_type": "fake_relax",
            "params": {"target_node": "spark"},
            "compute_handle": handle,
            "compute_handle_released": False,
            "deadline": time.time() + 7200,
        },
    )
    rid = int(ref.id)
    store.add_tag(rid, Tag.parse_strict("STATUS:queued"), set_by="agent")

    result = ssh_node.run_ssh_node_pass(store, limit=2)

    assert result["claimed"] == 1 and submitted == []
    assert _status(store, rid) == "running"
    meta = _meta(store, rid)
    assert meta["compute_handle"] == handle
    assert int(meta.get("attempts") or 0) == 0
    events = [
        c.text
        for c in store.chunks.list_chunks_for_ref(rid)
        if getattr(c, "chunk_kind", None) == "job_event"
    ]
    assert any("re-adopted" in t for t in events)
    ssh_node.run_ssh_node_pass(store, limit=2)
    assert polled == [handle] and submitted == []


def test_parked_handle_past_its_deadline_fails_without_an_orphan_skip(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A parked job whose renewed deadline lapsed is terminalized by the poll
    step like any detached job; the hook is the plugin's own (a no-op for a
    pid-less handle), and the handle is not logged as an unverifiable orphan."""
    killed: list[tuple[Any, Any]] = []
    reaped: list[tuple[Any, Any]] = []
    monkeypatch.setattr(
        ssh_node, "get_job_type", lambda name: _kill_reap_spec(killed, reaped)
    )
    monkeypatch.setenv("PRECIS_NODE", "spark")
    rid = _mk_running_job(
        store,
        lease_offset_s=3600,
        attempts=0,
        lease_boot_id="dead-generation",
        compute_handle=_parked_handle(),
        target_node="spark",
    )
    _restart_reclaimed(store, rid, deadline=time.time() - 60)

    ssh_node.run_ssh_node_pass(store, limit=2)

    meta = _meta(store, rid)
    assert _status(store, rid) == "failed"
    assert "detached_orphan_skipped" not in meta
    assert [h for _, h in killed] == [_parked_handle()]


def test_orphan_reap_leaves_a_parked_handle_alone() -> None:
    meta = {"compute_handle": _parked_handle()}
    no_conn: Any = None  # a parked handle returns before any DB use
    assert (
        ssh_node._reap_detached_job(no_conn, no_conn, 1, "t", meta, node="spark")
        is False
    )


@pytest.mark.parametrize(
    "case", ["dead-group", "start-time-mismatch", "past-deadline", "no-deadline"]
)
def test_unprovable_detached_handle_still_trips_the_guard(
    store: Store, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """Only a provably-live handle inside its deadline is refunded: a dead
    group, a recycled pid (start time differs), a blown deadline or a row
    with no deadline at all still fails at the attempt cap."""
    killed: list[tuple[Any, Any]] = []
    reaped: list[tuple[Any, Any]] = []
    monkeypatch.setattr(
        ssh_node, "get_job_type", lambda name: _kill_reap_spec(killed, reaped)
    )
    _fake_detached_identity(
        monkeypatch, start_time=999 if case == "start-time-mismatch" else 123456
    )
    if case == "dead-group":
        monkeypatch.setattr(ssh_node, "_detached_group_alive", lambda handle: False)
    monkeypatch.setenv("PRECIS_NODE", "spark")
    deadline = {
        "past-deadline": time.time() - 60,
        "no-deadline": None,
    }.get(case, time.time() + 7200)
    rid, _ = _detached_restart_job(
        store, attempts=ssh_node._MAX_ATTEMPTS, deadline=deadline
    )

    ssh_node.run_ssh_node_pass(store, limit=2)

    # past its deadline the poll step kills it before any claim; the
    # others reach the guard -- either way it ends failed, never refunded
    assert _status(store, rid) == "failed"
    assert all(r["why"] != "readopt" for r in _meta(store, rid).get("reclaims", []))


def test_readopt_entries_do_not_fill_the_reclaim_churn_backstop() -> None:
    """Ten re-adoptions are ten restarts, not legacy churn."""
    from precis.workers.executors import _common

    meta = {
        "attempts": 0,
        "reclaims": [{"at": "x", "why": "readopt"}] * _common._RECLAIMS_CAP,
    }
    assert _common.poison_guard(None, None, 1, meta) is False  # type: ignore[arg-type]


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


# ── gr567620: STATUS:cancel_requested on a running detached job ────


def _mk_cancel_job(
    store: Store,
    *,
    compute_handle: Any = None,
    submit_started_at: float | None = None,
    target_node: str | None = None,
    job_type: str = "fake_relax",
    params: dict[str, Any] | None = None,
) -> int:
    """A detached ssh_node job already in ``STATUS:cancel_requested`` —
    what an agent's ``tag(…, add=['STATUS:cancel_requested'])`` leaves
    behind on a running submit/poll job."""
    meta: dict[str, Any] = {
        "executor": "ssh_node",
        "job_type": job_type,
        "params": params
        if params is not None
        else ({"target_node": target_node} if target_node is not None else {}),
    }
    if compute_handle is not None:
        meta["compute_handle"] = compute_handle
    if submit_started_at is not None:
        meta["submit_started_at"] = submit_started_at
    ref = store.insert_ref(kind="job", slug=None, title="cancel me", meta=meta)
    store.add_tag(ref.id, Tag.parse_strict("STATUS:cancel_requested"), set_by="agent")
    return int(ref.id)


def _job_events(store: Store, ref_id: int) -> list[str]:
    return [
        c.text
        for c in store.chunks.list_chunks_for_ref(ref_id)
        if getattr(c, "chunk_kind", None) == "job_event"
    ]


def _backdate_cancel_meta(store: Store, ref_id: int, **fields: Any) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || %s::jsonb WHERE ref_id = %s",
            (json.dumps(fields), ref_id),
        )
        conn.commit()


def test_cancel_requested_terms_kills_then_cancels(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr567620 core: the cancel left the running poll set, so the pass
    must drive it — plugin kill (TERM) on first sight, SIGKILL to the
    still-verified group after the grace, then STATUS:cancelled with the
    handle released. ``poll`` never runs."""
    killed: list[tuple[Any, Any]] = []
    reaped: list[tuple[Any, Any]] = []
    polled: list[Any] = []
    signals: list[tuple[int, int]] = []

    def _poll(ctx: Any, handle: Any) -> bool:
        polled.append(handle)
        return False

    monkeypatch.setattr(
        ssh_node,
        "get_job_type",
        lambda name: replace(_kill_reap_spec(killed, reaped), poll=_poll),
    )
    _fake_detached_identity(monkeypatch)
    monkeypatch.setattr(
        ssh_node.os, "killpg", lambda pg, sig: signals.append((pg, sig))
    )
    monkeypatch.setenv("PRECIS_NODE", "spark")
    handle = _identity_handle()
    rid = _mk_cancel_job(store, compute_handle=handle, target_node="spark")

    # Tick 1: the plugin kill hook fires (its TERM), the row waits.
    result = ssh_node.run_ssh_node_pass(store, limit=2)
    assert result == {"claimed": 0, "ok": 0, "failed": 0}
    assert killed == [(rid, handle)]
    assert reaped == []
    assert _status(store, rid) == "cancel_requested"
    assert _meta(store, rid)["cancel_term_at"]
    assert polled == []

    # Tick 2 inside the grace: the group is still alive — nothing new.
    ssh_node.run_ssh_node_pass(store, limit=2)
    assert _status(store, rid) == "cancel_requested"
    assert signals == []

    # Past the grace with the group still verified alive: SIGKILL.
    _backdate_cancel_meta(
        store, rid, cancel_term_at=time.time() - ssh_node._CANCEL_KILL_GRACE_S - 1
    )
    ssh_node.run_ssh_node_pass(store, limit=2)
    assert signals == [(4242, signal.SIGKILL)]
    assert _meta(store, rid)["cancel_kill_at"]
    assert _status(store, rid) == "cancel_requested"

    # The group is gone: cancelled, handle released, poll never ran.
    monkeypatch.setattr(ssh_node, "_detached_group_alive", lambda handle: False)
    ssh_node.run_ssh_node_pass(store, limit=2)
    assert _status(store, rid) == "cancelled"
    meta = _meta(store, rid)
    assert meta["compute_handle_released"] is True
    assert polled == []
    events = _job_events(store, rid)
    assert any("cancel requested" in t for t in events)
    assert any("SIGKILL" in t for t in events)


def test_cancel_requested_without_kill_hook_terms_pgid_directly(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A submit/poll type with no ``kill`` hook still gets its verified
    process group TERMed by the executor itself."""
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        ssh_node,
        "get_job_type",
        lambda name: _spec(submit=lambda c, s: None, poll=lambda c, h: False),
    )
    _fake_detached_identity(monkeypatch)
    monkeypatch.setattr(
        ssh_node.os, "killpg", lambda pg, sig: signals.append((pg, sig))
    )
    monkeypatch.setenv("PRECIS_NODE", "spark")
    rid = _mk_cancel_job(store, compute_handle=_identity_handle(), target_node="spark")

    ssh_node.run_ssh_node_pass(store, limit=2)

    assert signals == [(4242, signal.SIGTERM)]
    assert _status(store, rid) == "cancel_requested"
    assert _meta(store, rid)["cancel_term_at"]


def test_cancel_requested_parked_handle_cancels_without_signaling(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """land_slice's deferral parks a handle with ``not_before`` and no
    process — a cancel on it is a pure status write."""
    killed: list[tuple[Any, Any]] = []
    reaped: list[tuple[Any, Any]] = []
    monkeypatch.setattr(
        ssh_node, "get_job_type", lambda name: _kill_reap_spec(killed, reaped)
    )
    monkeypatch.setenv("PRECIS_NODE", "spark")
    rid = _mk_cancel_job(store, compute_handle=_parked_handle(), target_node="spark")

    result = ssh_node.run_ssh_node_pass(store, limit=2)

    assert result == {"claimed": 0, "ok": 1, "failed": 0}
    assert killed == [] and reaped == []
    assert _status(store, rid) == "cancelled"
    assert _meta(store, rid)["compute_handle_released"] is True


def test_cancel_requested_unverifiable_handle_follows_poll(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr572654: a handle the executor cannot verify can still be observed
    by the plugin — blind-cancelling the record while the child ran on
    dropped the land's post-steps (the ship landed, land-notify never
    fired). The row holds ``cancel_requested`` and follows ``spec.poll``
    to the real terminal outcome; nothing is ever signalled."""
    polled: list[Any] = []
    terminal: dict[str, bool] = {"done": False}

    def _poll(ctx: Any, handle: Any) -> bool:
        polled.append(handle)
        if not terminal["done"]:
            return False
        ctx.set_status("succeeded")
        return True

    killed: list[tuple[Any, Any]] = []
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        ssh_node,
        "get_job_type",
        lambda name: replace(_kill_reap_spec(killed, []), poll=_poll),
    )
    monkeypatch.setattr(
        ssh_node.os, "killpg", lambda pg, sig: signals.append((pg, sig))
    )
    monkeypatch.setenv("PRECIS_NODE", "spark")
    handle = _identity_handle()
    del handle["boot_id"]  # missing-identity
    rid = _mk_cancel_job(store, compute_handle=handle, target_node="spark")

    ssh_node.run_ssh_node_pass(store, limit=2)

    assert _status(store, rid) == "cancel_requested"
    assert polled == [handle]
    assert killed == [] and signals == []
    meta = _meta(store, rid)
    assert meta["cancel_follow"] == "missing-identity"
    # The absent wall bound is minted on the first follow — a detached row
    # never gets an unbounded poll.
    assert meta["deadline"] > time.time()
    assert any(
        "unverifiable" in t and "following" in t for t in _job_events(store, rid)
    )

    # The child's real terminal outcome stands — not a fake "cancelled".
    terminal["done"] = True
    ssh_node.run_ssh_node_pass(store, limit=2)
    assert _status(store, rid) == "succeeded"
    assert not _meta(store, rid).get("detached_orphan_skipped")


def test_cancel_requested_unverifiable_past_deadline_still_follows(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr572654 review: ``meta.deadline`` bounds the compute, never the
    record. A blind cancel past it lied exactly as before — a ship that
    finished on its own then read ``cancelled`` and its land-notify never
    ran. The crossing is stamped once (``cancel_follow_overdue``) and the
    row keeps following ``spec.poll`` to the child's real terminal
    outcome."""
    polled: list[Any] = []
    terminal: dict[str, bool] = {"done": False}

    def _poll(ctx: Any, handle: Any) -> bool:
        polled.append(handle)
        if not terminal["done"]:
            return False
        ctx.set_status("succeeded")
        return True

    monkeypatch.setattr(
        ssh_node,
        "get_job_type",
        lambda name: replace(_kill_reap_spec([], []), poll=_poll),
    )
    monkeypatch.setenv("PRECIS_NODE", "spark")
    handle = _identity_handle()
    del handle["boot_id"]
    rid = _mk_cancel_job(store, compute_handle=handle, target_node="spark")
    _backdate_cancel_meta(store, rid, deadline=time.time() - 60)

    ssh_node.run_ssh_node_pass(store, limit=2)

    assert _status(store, rid) == "cancel_requested"
    assert polled == [handle]
    meta = _meta(store, rid)
    assert meta["cancel_follow"] == "missing-identity"
    assert meta["cancel_follow_overdue"]["deadline"] == meta["deadline"]
    assert not meta.get("detached_orphan_skipped")
    assert any("deadline passed" in t for t in _job_events(store, rid))

    # The crossing is stamped once — the next pass adds no second note.
    ssh_node.run_ssh_node_pass(store, limit=2)
    assert len([t for t in _job_events(store, rid) if "deadline passed" in t]) == 1

    # The child's real terminal outcome still lands — never a fake
    # "cancelled" over a finished ship.
    terminal["done"] = True
    ssh_node.run_ssh_node_pass(store, limit=2)
    assert _status(store, rid) == "succeeded"


def test_cancel_requested_unverifiable_dead_child_cancels(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``start-time-mismatch``/``boot-id-mismatch`` prove the recorded
    child is gone — its slot recycled or the host rebooted. The poll gets
    its word first (a ship that finished before dying left its outcome in
    the exit file); a non-terminal read is watching a foreign slot, so the
    row cancels rather than following a stranger forever."""
    polled: list[Any] = []
    terminal: dict[str, bool] = {"done": False}

    def _poll(ctx: Any, handle: Any) -> bool:
        polled.append(handle)
        if not terminal["done"]:
            return False
        ctx.set_status("succeeded")
        return True

    monkeypatch.setattr(
        ssh_node, "_detached_identity_reason", lambda handle: "start-time-mismatch"
    )
    monkeypatch.setattr(
        ssh_node,
        "get_job_type",
        lambda name: replace(_kill_reap_spec([], []), poll=_poll),
    )
    monkeypatch.setenv("PRECIS_NODE", "spark")
    handle = _identity_handle()
    rid = _mk_cancel_job(store, compute_handle=handle, target_node="spark")

    ssh_node.run_ssh_node_pass(store, limit=2)

    assert polled == [handle]
    assert _status(store, rid) == "cancelled"
    meta = _meta(store, rid)
    assert meta["detached_orphan_skipped"]["reason"] == "start-time-mismatch"

    # When the poll CAN settle it — the ship finished before its slot
    # recycled — the real outcome stands even for the dead-child class.
    terminal["done"] = True
    rid2 = _mk_cancel_job(store, compute_handle=_identity_handle(), target_node="spark")
    ssh_node.run_ssh_node_pass(store, limit=2)
    assert _status(store, rid2) == "succeeded"


def test_cancel_requested_unverifiable_without_poll_cancels_blind(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``poll`` hook → nothing can observe the child: the blind cancel
    is all there is (the pre-gr572654 shape)."""
    monkeypatch.setattr(
        ssh_node, "get_job_type", lambda name: _spec(dispatch=lambda c, s: None)
    )
    monkeypatch.setenv("PRECIS_NODE", "spark")
    handle = _identity_handle()
    del handle["boot_id"]
    rid = _mk_cancel_job(store, compute_handle=handle, target_node="spark")

    ssh_node.run_ssh_node_pass(store, limit=2)

    assert _status(store, rid) == "cancelled"
    assert _meta(store, rid)["detached_orphan_skipped"]["reason"] == "missing-identity"


def test_cancel_requested_dead_group_cancels_without_term(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Identity verified but the group already gone: nothing to TERM —
    cancel immediately."""
    killed: list[tuple[Any, Any]] = []
    reaped: list[tuple[Any, Any]] = []
    monkeypatch.setattr(
        ssh_node, "get_job_type", lambda name: _kill_reap_spec(killed, reaped)
    )
    _fake_detached_identity(monkeypatch)
    monkeypatch.setattr(ssh_node, "_detached_group_alive", lambda handle: False)
    monkeypatch.setenv("PRECIS_NODE", "spark")
    rid = _mk_cancel_job(store, compute_handle=_identity_handle(), target_node="spark")

    result = ssh_node.run_ssh_node_pass(store, limit=2)

    assert result == {"claimed": 0, "ok": 1, "failed": 0}
    assert killed == []
    assert _status(store, rid) == "cancelled"
    assert _meta(store, rid)["compute_handle_released"] is True


def test_cancel_requested_opaque_handle_uses_plugin_kill_then_cancels(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-dict handle is the plugin's own kill protocol: the hook
    fires once, the grace passes (no group to observe), then cancelled."""
    killed: list[tuple[Any, Any]] = []
    reaped: list[tuple[Any, Any]] = []
    monkeypatch.setattr(
        ssh_node, "get_job_type", lambda name: _kill_reap_spec(killed, reaped)
    )
    monkeypatch.setenv("PRECIS_NODE", "spark")
    rid = _mk_cancel_job(store, compute_handle="remote-pid-opaque", target_node="spark")

    ssh_node.run_ssh_node_pass(store, limit=2)
    assert killed == [(rid, "remote-pid-opaque")]
    assert _status(store, rid) == "cancel_requested"

    _backdate_cancel_meta(
        store, rid, cancel_term_at=time.time() - ssh_node._CANCEL_KILL_GRACE_S - 1
    )
    ssh_node.run_ssh_node_pass(store, limit=2)
    assert _status(store, rid) == "cancelled"
    assert _meta(store, rid)["compute_handle_released"] is True


def test_cancel_requested_submit_window_without_handle_cancels(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancel that lands between claim and the handle persist (the
    gr191673 submit window) still reaches cancelled — there is nothing
    to signal, and the event says a compute may be orphaned."""
    killed: list[tuple[Any, Any]] = []
    reaped: list[tuple[Any, Any]] = []
    monkeypatch.setattr(
        ssh_node, "get_job_type", lambda name: _kill_reap_spec(killed, reaped)
    )
    monkeypatch.setenv("PRECIS_NODE", "spark")
    rid = _mk_cancel_job(store, submit_started_at=time.time(), target_node="spark")

    result = ssh_node.run_ssh_node_pass(store, limit=2)

    assert result == {"claimed": 0, "ok": 1, "failed": 0}
    assert killed == []
    assert _status(store, rid) == "cancelled"
    assert any("submit outcome unknown" in t for t in _job_events(store, rid))


def test_cancel_requested_land_slice_keeps_worktree_for_forensics(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr567620's land_slice ask: the cancel never runs ``poll`` — its
    green path owns ``_cleanup`` (worktree remove + branch -D), so the
    branch and worktree stay."""
    from precis.workers.job_types import land_slice

    signals: list[tuple[int, int]] = []
    cleanups: list[Any] = []

    def _cleanup_spy(*a: Any) -> list[Any]:
        cleanups.append(a)
        return []

    monkeypatch.setattr(
        ssh_node.os, "killpg", lambda pg, sig: signals.append((pg, sig))
    )
    monkeypatch.setattr(land_slice, "_cleanup", _cleanup_spy)
    _fake_detached_identity(monkeypatch)
    monkeypatch.setenv("PRECIS_NODE", "spark")
    handle = {
        **_identity_handle(),
        "worktree": "/tmp/slice-td42",
        "subject": "feat(x): y (td42)",
        "flag": "--checked",
        "log": "/tmp/slice-td42/scratch/land_slice.log",
        "exit_file": "/tmp/slice-td42/scratch/land_slice.exit",
    }
    rid = _mk_cancel_job(
        store,
        compute_handle=handle,
        target_node="spark",
        job_type="land_slice",
        params={
            "todo_id": 42,
            "worktree": "/tmp/slice-td42",
            "branch": "slice/42",
            "target_node": "spark",
        },
    )
    # No get_job_type patch: the real land_slice SPEC resolves.

    ssh_node.run_ssh_node_pass(store, limit=2)
    # land_slice.kill SIGTERMs the group through run_slice._signal_group.
    assert signals == [(4242, signal.SIGTERM)]
    assert _status(store, rid) == "cancel_requested"

    _backdate_cancel_meta(
        store, rid, cancel_term_at=time.time() - ssh_node._CANCEL_KILL_GRACE_S - 1
    )
    ssh_node.run_ssh_node_pass(store, limit=2)
    assert signals[-1] == (4242, signal.SIGKILL)

    monkeypatch.setattr(ssh_node, "_detached_group_alive", lambda handle: False)
    ssh_node.run_ssh_node_pass(store, limit=2)

    assert _status(store, rid) == "cancelled"
    assert cleanups == []  # poll's green path never ran


def test_cancelling_jobs_respects_node_pin(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cancel drive is pinned like the poll set: a job targeting
    another node is not this host's to cancel."""
    monkeypatch.setenv("PRECIS_NODE", "spark")
    rid = _mk_cancel_job(
        store, compute_handle=_identity_handle(), target_node="other-node"
    )
    assert ssh_node._cancelling_jobs(store, "spark") == []
    assert [row[0] for row in ssh_node._cancelling_jobs(store, "other-node")] == [rid]


def test_cancel_requested_kill_hook_error_recorded(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr568379: a ``spec.kill`` that raises must not chunk as "nothing
    to signal" — the event carries the exception type and text."""

    def _kill(_ctx: Any, _handle: Any) -> None:
        raise RuntimeError("plugin stop exploded")

    spec = replace(_kill_reap_spec([], []), kill=_kill)
    monkeypatch.setattr(ssh_node, "get_job_type", lambda name: spec)
    _fake_detached_identity(monkeypatch)
    monkeypatch.setenv("PRECIS_NODE", "spark")
    rid = _mk_cancel_job(store, compute_handle=_identity_handle(), target_node="spark")

    ssh_node.run_ssh_node_pass(store, limit=2)

    assert _status(store, rid) == "cancel_requested"
    assert _meta(store, rid)["cancel_term_at"]
    events = _job_events(store, rid)
    assert any(
        "FAILED" in t and "RuntimeError" in t and "plugin stop exploded" in t
        for t in events
    )
    assert not any("nothing to signal" in t for t in events)


def test_cancel_requested_term_oserror_recorded(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr568379: the executor's own SIGTERM raising ``OSError`` must reach
    the event chunk — the bare ``except OSError: pass`` dropped it."""

    def _killpg(_pgid: int, _sig: int) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(
        ssh_node,
        "get_job_type",
        lambda name: _spec(submit=lambda c, s: None, poll=lambda c, h: False),
    )
    _fake_detached_identity(monkeypatch)
    monkeypatch.setattr(ssh_node.os, "killpg", _killpg)
    monkeypatch.setenv("PRECIS_NODE", "spark")
    rid = _mk_cancel_job(store, compute_handle=_identity_handle(), target_node="spark")

    ssh_node.run_ssh_node_pass(store, limit=2)

    assert _status(store, rid) == "cancel_requested"
    assert _meta(store, rid)["cancel_term_at"]
    events = _job_events(store, rid)
    assert any(
        "FAILED" in t
        and "SIGTERM" in t
        and "PermissionError" in t
        and "Operation not permitted" in t
        for t in events
    )
    assert not any("nothing to signal" in t for t in events)


def test_cancel_requested_kill_oserror_recorded(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr568379: a failed SIGKILL must not chunk as sent — the event
    records the ``OSError`` type and text next to the grace note."""

    def _killpg(_pgid: int, _sig: int) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(
        ssh_node,
        "get_job_type",
        lambda name: _spec(submit=lambda c, s: None, poll=lambda c, h: False),
    )
    _fake_detached_identity(monkeypatch)
    monkeypatch.setattr(ssh_node.os, "killpg", _killpg)
    monkeypatch.setenv("PRECIS_NODE", "spark")
    rid = _mk_cancel_job(store, compute_handle=_identity_handle(), target_node="spark")
    _backdate_cancel_meta(
        store, rid, cancel_term_at=time.time() - ssh_node._CANCEL_KILL_GRACE_S - 1
    )

    ssh_node.run_ssh_node_pass(store, limit=2)

    assert _status(store, rid) == "cancel_requested"
    assert _meta(store, rid)["cancel_kill_at"]
    events = _job_events(store, rid)
    assert any(
        "SIGKILL" in t and "FAILED" in t and "PermissionError" in t for t in events
    )


# ── gr572654: verifiable launch identity (Darwin ps + executor stamp) ──


def test_detached_identity_reason_darwin_ps_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Darwin has no ``/proc``: the identity is the launch-minted
    ``ps_identity`` (start time + user), re-read through ``ps`` — a match
    verifies; a live leader under a DIFFERENT identity means the recorded
    child is gone and the slot recycled."""
    monkeypatch.setattr(ssh_node.sys, "platform", "darwin")
    handle = {
        "pid": 4242,
        "pgid": 4242,
        "ps_identity": "Mon Nov 2 09:07:00 2026 deploy",
    }
    monkeypatch.setattr(
        ssh_node,
        "_darwin_ps_identity",
        lambda pgid: "Mon Nov 2 09:07:00 2026 deploy",
    )
    assert ssh_node._detached_identity_reason(handle) is None
    monkeypatch.setattr(ssh_node, "_darwin_ps_identity", lambda pgid: "other user")
    assert ssh_node._detached_identity_reason(handle) == "start-time-mismatch"
    del handle["ps_identity"]
    assert ssh_node._detached_identity_reason(handle) == "missing-identity"


def test_detached_identity_reason_darwin_dead_leader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ps`` answering nothing for the leader proves nothing about the
    group — descendants can outlive it. Only ESRCH on the group permits
    ``group-unavailable``; a live group stays ``identity-unreadable``
    (never a kill authorization)."""
    monkeypatch.setattr(ssh_node.sys, "platform", "darwin")
    handle = {"pid": 4242, "pgid": 4242, "ps_identity": "the-launch-identity"}
    monkeypatch.setattr(ssh_node, "_darwin_ps_identity", lambda pgid: None)

    def _esrch(pgid: int, sig: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(ssh_node.os, "killpg", _esrch)
    assert ssh_node._detached_identity_reason(handle) == "group-unavailable"
    monkeypatch.setattr(ssh_node.os, "killpg", lambda pgid, sig: None)
    assert ssh_node._detached_identity_reason(handle) == "identity-unreadable"


def test_cancel_requested_darwin_verified_terms_group(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr572654: on Darwin a launch-minted ``ps_identity`` verifies, so the
    cancel actually reaches the plugin's kill hook — the incident's
    "missing-identity; cancelled without signalling" hole."""
    killed: list[tuple[Any, Any]] = []
    monkeypatch.setattr(
        ssh_node, "get_job_type", lambda name: _kill_reap_spec(killed, [])
    )
    monkeypatch.setattr(ssh_node.sys, "platform", "darwin")
    monkeypatch.setattr(
        ssh_node, "_darwin_ps_identity", lambda pgid: "the-launch-identity"
    )
    monkeypatch.setattr(ssh_node, "_detached_group_alive", lambda handle: True)
    monkeypatch.setenv("PRECIS_NODE", "spark")
    handle = {"pid": 4242, "pgid": 4242, "ps_identity": "the-launch-identity"}
    rid = _mk_cancel_job(store, compute_handle=handle, target_node="spark")

    ssh_node.run_ssh_node_pass(store, limit=2)

    assert killed == [(rid, handle)]
    assert _status(store, rid) == "cancel_requested"
    assert _meta(store, rid)["cancel_term_at"]


def test_submit_stamps_identity_the_plugin_left_out(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr572654: the executor fills the process-identity keys a submit
    returned without — the launch record must name its process for the
    cancel/kill/reap gates to verify it."""
    monkeypatch.setattr(
        ssh_node,
        "get_job_type",
        lambda name: _spec(
            submit=lambda c, s: {"pid": 4242, "pgid": 4242},
            poll=lambda c, h: False,
        ),
    )
    _fake_detached_identity(monkeypatch)
    rid = _mk_job(store)

    ssh_node.run_ssh_node_pass(store, limit=2)

    handle = _meta(store, rid)["compute_handle"]
    assert handle["boot_id"] == "boot-for-test"
    assert handle["start_time"] == 123456


def test_submit_keeps_the_plugin_identity(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The executor's stamp never overwrites what the plugin minted at
    spawn — the plugin's read sits closer to the launch."""
    monkeypatch.setattr(
        ssh_node,
        "get_job_type",
        lambda name: _spec(
            submit=lambda c, s: _identity_handle(), poll=lambda c, h: False
        ),
    )
    _fake_detached_identity(monkeypatch, boot_id="other-boot", start_time=9)
    rid = _mk_job(store)

    ssh_node.run_ssh_node_pass(store, limit=2)

    handle = _meta(store, rid)["compute_handle"]
    assert handle["boot_id"] == "boot-for-test"
    assert handle["start_time"] == 123456
