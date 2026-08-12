"""Claim-registry reaper (self-healing-spine Layer 1 slice 1).

The epoch arm for slot holds + agentlogs: a claim whose holder's worker
generation is *provably replaced* (its ``(host, process)`` advertises a
different — or no — boot_id in ``host_heartbeat.meta.boot_ids``) is
reclaimed on the next pass; a still-advertised holder, a NULL-identity
claim, and a too-young claim are never touched. The shared predicate
itself (``precis.liveness``) is byte-compatible with the job-lease arm —
its behavior is pinned by the existing lease tests.
"""

from __future__ import annotations

import json

import pytest

from precis.liveness import worker_identity
from precis.store import Store
from precis.workers.reaper import run_claim_reaper

pytestmark = pytest.mark.db

_HOST = "reaper-test-host"
_PROC = "worker-system"
_DEAD_BOOT = "deadbeef" * 4
_LIVE_BOOT = "11ce11ce" * 4
_RES = "llm:reaper-test-model"


def _seed_heartbeat(store: Store, boot_id: str | None) -> None:
    with store.pool.connection() as conn:
        conn.execute("DELETE FROM host_heartbeat WHERE host = %s", (_HOST,))
        if boot_id is not None:
            conn.execute(
                "INSERT INTO host_heartbeat (host, meta) VALUES (%s, %s::jsonb)",
                (_HOST, json.dumps({"boot_ids": {_PROC: boot_id}})),
            )
        conn.commit()


def _seed_slot(store: Store, free: int = 0, capacity: int = 1) -> None:
    with store.pool.connection() as conn:
        conn.execute("DELETE FROM resource_slot_holds WHERE resource = %s", (_RES,))
        conn.execute(
            "DELETE FROM resource_slots WHERE host = %s AND resource = %s",
            (_HOST, _RES),
        )
        conn.execute(
            "INSERT INTO resource_slots (host, resource, capacity, free) "
            "VALUES (%s, %s, %s, %s)",
            (_HOST, _RES, capacity, free),
        )
        conn.commit()


def _insert_hold(store: Store, *, boot_id: str | None, age_s: float = 3600.0) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO resource_slot_holds "
            "(host, resource, units, holder, acquired_at, expires_at, "
            " holder_host, holder_process, holder_boot_id) "
            "VALUES (%s, %s, 1, 'test:1', now() - make_interval(secs => %s), "
            "        now() + interval '1 hour', %s, %s, %s) RETURNING id",
            (
                _HOST,
                _RES,
                age_s,
                _HOST if boot_id else None,
                _PROC if boot_id else None,
                boot_id,
            ),
        ).fetchone()
        conn.commit()
        assert row is not None
        return int(row[0])


def _hold_exists(store: Store, hold_id: int) -> bool:
    with store.pool.connection() as conn:
        return (
            conn.execute(
                "SELECT 1 FROM resource_slot_holds WHERE id = %s", (hold_id,)
            ).fetchone()
            is not None
        )


def _free(store: Store) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT free FROM resource_slots WHERE host = %s AND resource = %s",
            (_HOST, _RES),
        ).fetchone()
        assert row is not None
        return int(row[0])


# ── slot holds ───────────────────────────────────────────────────────────


def test_epoch_dead_hold_reclaimed_and_refunded(store: Store) -> None:
    """Holder's generation replaced → hold deleted + unit refunded, well
    before the TTL (expires_at is an hour out)."""
    _seed_slot(store, free=0)
    _seed_heartbeat(store, _LIVE_BOOT)  # a NEW generation advertises
    hold_id = _insert_hold(store, boot_id=_DEAD_BOOT)
    assert run_claim_reaper(store) >= 1
    assert not _hold_exists(store, hold_id)
    assert _free(store) == 1


def test_no_advertisement_at_all_is_provably_gone(store: Store) -> None:
    """host_heartbeat missing the (host, process) entirely = same signal
    as an explicit mismatch (the COALESCE-sentinel case)."""
    _seed_slot(store, free=0)
    _seed_heartbeat(store, None)
    hold_id = _insert_hold(store, boot_id=_DEAD_BOOT)
    assert run_claim_reaper(store) >= 1
    assert not _hold_exists(store, hold_id)
    assert _free(store) == 1


def test_live_holder_never_reclaimed(store: Store) -> None:
    """Same boot_id still advertised (the plain-hang case) → TTL arm's
    business, not ours."""
    _seed_slot(store, free=0)
    _seed_heartbeat(store, _DEAD_BOOT)  # still the holder's generation
    hold_id = _insert_hold(store, boot_id=_DEAD_BOOT)
    run_claim_reaper(store)
    assert _hold_exists(store, hold_id)
    assert _free(store) == 0


def test_null_identity_hold_untouched(store: Store) -> None:
    """CLI / unadvertised-worker hold (NULL identity) is invisible to the
    epoch arm — TTL-only reclaim, the pre-existing contract."""
    _seed_slot(store, free=0)
    _seed_heartbeat(store, _LIVE_BOOT)
    hold_id = _insert_hold(store, boot_id=None)
    run_claim_reaper(store)
    assert _hold_exists(store, hold_id)


def test_age_floor_shields_young_holds(store: Store) -> None:
    """A just-minted hold is never considered — protects the boot →
    first-heartbeat window."""
    _seed_slot(store, free=0)
    _seed_heartbeat(store, _LIVE_BOOT)
    hold_id = _insert_hold(store, boot_id=_DEAD_BOOT, age_s=1.0)
    run_claim_reaper(store)
    assert _hold_exists(store, hold_id)


def test_refund_capped_at_capacity(store: Store) -> None:
    """A refund can never push free past capacity (double-release /
    reseeded-slot discipline, same as the TTL sweep)."""
    _seed_slot(store, free=1, capacity=1)
    _seed_heartbeat(store, _LIVE_BOOT)
    hold_id = _insert_hold(store, boot_id=_DEAD_BOOT)
    run_claim_reaper(store)
    assert not _hold_exists(store, hold_id)
    assert _free(store) == 1  # capped, not 2


# ── agentlogs ────────────────────────────────────────────────────────────


def _open_zombie_log(store: Store, *, boot_id: str | None, age_s: float) -> int:
    meta: dict[str, object] = {"source": "test", "started_at": "x"}
    if boot_id is not None:
        meta["worker"] = {"host": _HOST, "process": _PROC, "boot_id": boot_id}
    ref = store.insert_ref(kind="agentlog", slug=None, title="zombie", meta=meta)
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET created_at = now() - make_interval(secs => %s) "
            "WHERE ref_id = %s",
            (age_s, int(ref.id)),
        )
        conn.commit()
    return int(ref.id)


def _log_meta(store: Store, ref_id: int) -> dict:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
        assert row is not None
        return dict(row[0])


def test_zombie_agentlog_finalized_aborted(store: Store) -> None:
    _seed_heartbeat(store, _LIVE_BOOT)
    rid = _open_zombie_log(store, boot_id=_DEAD_BOOT, age_s=3600.0)
    assert run_claim_reaper(store) >= 1
    meta = _log_meta(store, rid)
    assert meta["status"] == "aborted"
    assert "ended_at" in meta
    assert meta["reclaim"]["arm"] == "epoch"


def test_live_agentlog_untouched(store: Store) -> None:
    _seed_heartbeat(store, _DEAD_BOOT)  # holder generation still live
    rid = _open_zombie_log(store, boot_id=_DEAD_BOOT, age_s=3600.0)
    run_claim_reaper(store)
    meta = _log_meta(store, rid)
    assert "ended_at" not in meta


def test_identityless_agentlog_untouched(store: Store) -> None:
    """Pre-slice-1 logs (no meta.worker) are invisible to the epoch arm."""
    _seed_heartbeat(store, _LIVE_BOOT)
    rid = _open_zombie_log(store, boot_id=None, age_s=3600.0)
    run_claim_reaper(store)
    meta = _log_meta(store, rid)
    assert "ended_at" not in meta


# ── the stamp guard ──────────────────────────────────────────────────────


def test_worker_identity_requires_advertisement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No PRECIS_PROCESS → all-None triple, even with a minted boot_id
    (the unadvertised-must-not-stamp guard, shared with job leases)."""
    monkeypatch.delenv("PRECIS_PROCESS", raising=False)
    assert worker_identity() == (None, None, None)
