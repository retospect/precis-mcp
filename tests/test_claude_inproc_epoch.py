"""claude_inproc — §H boot epoch: ``reclaim_stale_running`` (compute-lane-
lease-epoch.md).

The dispatch runs entirely in-process (a ``claude -p`` subprocess this
worker owns), so a worker restart mid-tick means the compute is genuinely
dead — the same "worker death = compute death" assumption ``ssh_node`` was
written under (see ``executors/claude_inproc.py::_claim_jobs``). Before
this, a bounced worker's plan_tick/fix_gripe job sat ``STATUS:running`` for
the full 90-min lease before anything reclaimed it.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.store import Store
from precis.store.types import Tag
from precis.workers.executors import claude_inproc
from precis.workers.job_types import JobTypeSpec

pytestmark = pytest.mark.db


def _spec(*, dispatch: Any, name: str = "fake_tick") -> JobTypeSpec:
    def _run(*_a: Any, **_k: Any) -> str:
        return "noop"

    return JobTypeSpec(
        name=name,
        params_schema={"type": "object"},
        compatible_executors=frozenset({"claude_inproc"}),
        requires=frozenset(),
        description="fake job_type for tests",
        run=_run,
        dispatch=dispatch,
    )


def _succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        claude_inproc,
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


def _mk_running_job(
    store: Store,
    *,
    lease_offset_s: int,
    attempts: int | None = None,
    lease_boot_id: str | None = None,
    lease_process: str | None = None,
    lease_host: str | None = None,
) -> int:
    meta: dict[str, Any] = {
        "executor": "claude_inproc",
        "job_type": "fake_tick",
    }
    if attempts is not None:
        meta["attempts"] = attempts
    if lease_boot_id is not None:
        meta["lease_boot_id"] = lease_boot_id
    if lease_process is not None:
        meta["lease_process"] = lease_process
    if lease_host is not None:
        meta["lease_host"] = lease_host
    ref = store.insert_ref(
        kind="job", slug=None, title="orphaned in-proc job", meta=meta
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


def _meta(store: Store, ref_id: int) -> dict[str, Any]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    assert row is not None
    return dict(row[0] or {})


def test_expired_lease_running_job_is_reclaimed(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Flipping ``reclaim_stale_running=True`` for claude_inproc means a
    bounced worker's job no longer waits out the full 90-min lease."""
    _succeeds(monkeypatch)
    rid = _mk_running_job(store, lease_offset_s=-60)

    result = claude_inproc.run_claude_inproc_pass(store, limit=2)

    assert result == {"claimed": 1, "ok": 1, "failed": 0}
    assert _status(store, rid) == "succeeded"


def test_epoch_mismatch_reclaims_before_lease_expiry(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A running job whose claiming generation is provably replaced is
    stolen on the FIRST claim pass — even while its 90-min lease is still
    hours away from expiring (the gr187627-class bounce wedge)."""
    _succeeds(monkeypatch)
    store.record_heartbeat(
        "melchior-epoch-1", meta={"boot_ids": {"precis-worker-agent": "new-gen"}}
    )
    rid = _mk_running_job(
        store,
        lease_offset_s=3600,
        lease_boot_id="dead-gen",
        lease_process="precis-worker-agent",
        lease_host="melchior-epoch-1",
    )

    result = claude_inproc.run_claude_inproc_pass(store, limit=2)

    assert result == {"claimed": 1, "ok": 1, "failed": 0}
    assert _status(store, rid) == "succeeded"


def test_live_holder_never_stolen_by_epoch_arm(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A still-live holder (boot_id matches the current advertisement) is
    never stolen while its lease is unexpired."""
    _succeeds(monkeypatch)
    store.record_heartbeat(
        "melchior-epoch-2", meta={"boot_ids": {"precis-worker-agent": "same-gen"}}
    )
    rid = _mk_running_job(
        store,
        lease_offset_s=3600,
        lease_boot_id="same-gen",
        lease_process="precis-worker-agent",
        lease_host="melchior-epoch-2",
    )

    result = claude_inproc.run_claude_inproc_pass(store, limit=2)

    assert result["claimed"] == 0
    assert _status(store, rid) == "running"


# ── §H piece 3: generalized attempt cap ────────────────────────────


def test_expiry_reclaim_bumps_attempts(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An expiry (no live-successor-proof) reclaim bumps meta.attempts —
    claude_inproc now shares the generalized cap ssh_node originated."""
    _succeeds(monkeypatch)
    rid = _mk_running_job(store, lease_offset_s=-60, attempts=1)

    result = claude_inproc.run_claude_inproc_pass(store, limit=2)

    assert result == {"claimed": 1, "ok": 1, "failed": 0}
    assert _meta(store, rid)["attempts"] == 2
    assert _meta(store, rid)["reclaims"][-1]["why"] == "expiry"


def test_epoch_reclaim_does_not_bump_attempts(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A redeploy-mid-run (epoch) reclaim must NOT burn the poison guard —
    the master's "redeploy mid-run does not burn the poison guard"."""
    _succeeds(monkeypatch)
    store.record_heartbeat(
        "melchior-epoch-3", meta={"boot_ids": {"precis-worker-agent": "new-gen"}}
    )
    rid = _mk_running_job(
        store,
        lease_offset_s=3600,
        attempts=1,
        lease_boot_id="dead-gen",
        lease_process="precis-worker-agent",
        lease_host="melchior-epoch-3",
    )

    result = claude_inproc.run_claude_inproc_pass(store, limit=2)

    assert result == {"claimed": 1, "ok": 1, "failed": 0}
    assert _meta(store, rid)["attempts"] == 1  # unchanged
    assert _meta(store, rid)["reclaims"][-1]["why"] == "epoch"


def test_poison_guard_fails_past_max_attempts(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A job re-claimed past the shared attempt cap is failed (bubbled),
    not run again — claude_inproc's own "ZERO attempt cap" gap, closed."""
    from precis.workers.executors._common import MAX_ATTEMPTS

    dispatched = {"n": 0}

    def _dispatch(ctx: Any, _s: Any) -> None:
        dispatched["n"] += 1
        ctx.set_status("succeeded")

    monkeypatch.setattr(
        claude_inproc, "get_job_type", lambda name: _spec(dispatch=_dispatch)
    )
    rid = _mk_running_job(store, lease_offset_s=-60, attempts=MAX_ATTEMPTS)

    result = claude_inproc.run_claude_inproc_pass(store, limit=2)

    assert result == {"claimed": 1, "ok": 0, "failed": 1}
    assert _status(store, rid) == "failed"
    assert dispatched["n"] == 0
    assert _meta(store, rid)["failure_class"] == "infra"


def test_reclaim_churn_cap_poisons_despite_zero_attempts(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review Finding 4 (poison-guard evasion via host-reboot-loop): a job
    whose HOST reboots every run is reclaimed via the epoch arm every
    time — epoch reclaims never bump ``meta.attempts`` by design (a
    redeploy isn't a crash-loop) — so the attempt cap alone never trips.
    Once ``meta.reclaims`` (already capped at 10, forensic-only) hits that
    cap, ``poison_guard`` trips regardless of ``attempts``."""
    from precis.workers.executors._common import _RECLAIMS_CAP, set_meta

    dispatched = {"n": 0}

    def _dispatch(ctx: Any, _s: Any) -> None:
        dispatched["n"] += 1
        ctx.set_status("succeeded")

    monkeypatch.setattr(
        claude_inproc, "get_job_type", lambda name: _spec(dispatch=_dispatch)
    )
    store.record_heartbeat(
        "melchior-epoch-churn", meta={"boot_ids": {"precis-worker-agent": "new-gen"}}
    )
    prior_reclaims = [
        {"at": "2026-01-01T00:00:00+00:00", "why": "epoch"}
        for _ in range(_RECLAIMS_CAP - 1)
    ]
    rid = _mk_running_job(
        store,
        lease_offset_s=3600,  # far future — only the epoch arm can fire
        attempts=0,
        lease_boot_id="dead-gen",
        lease_process="precis-worker-agent",
        lease_host="melchior-epoch-churn",
    )
    with store.pool.connection() as conn:
        set_meta(conn, rid, reclaims=prior_reclaims)
        conn.commit()

    result = claude_inproc.run_claude_inproc_pass(store, limit=2)

    assert result == {"claimed": 1, "ok": 0, "failed": 1}
    assert _status(store, rid) == "failed"
    assert dispatched["n"] == 0  # never dispatched — poisoned before running
    meta = _meta(store, rid)
    assert meta["attempts"] == 0  # never bumped — every reclaim was epoch
    assert len(meta["reclaims"]) == _RECLAIMS_CAP
    assert meta["failure_class"] == "infra"
