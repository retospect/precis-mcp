"""§A: the ``heartbeat`` worker pass — a per-host system-worker pass, NOT on
``scheduler_leases`` (heartbeat is the liveness signal that lease/claim
machinery is judged by, so it must never depend on it). Self-throttled via an
in-process timestamp, honoring ``PRECIS_HEARTBEAT_INTERVAL_SECONDS``.
"""

from __future__ import annotations

from precis.workers import heartbeat as hb


def _reset_throttle(monkeypatch) -> None:
    monkeypatch.setattr(hb, "_last_beat_monotonic", None)


def test_heartbeat_pass_upserts_per_host(store, monkeypatch) -> None:
    _reset_throttle(monkeypatch)
    r = hb.run_heartbeat_pass(store, host="test-host-1")
    assert (r.handler, r.claimed, r.ok, r.failed) == ("heartbeat", 1, 1, 0)

    rows = {row.host: row for row in store.recent_heartbeats()}
    assert "test-host-1" in rows


def test_heartbeat_pass_honors_throttle(store, monkeypatch) -> None:
    _reset_throttle(monkeypatch)
    monkeypatch.setenv("PRECIS_HEARTBEAT_INTERVAL_SECONDS", "3600")

    r1 = hb.run_heartbeat_pass(store, host="test-host-2")
    assert (r1.claimed, r1.ok, r1.failed) == (1, 1, 0)

    # immediately again — still well inside the 1h window, self-throttled.
    r2 = hb.run_heartbeat_pass(store, host="test-host-2")
    assert (r2.claimed, r2.ok, r2.failed) == (0, 0, 0)


def test_heartbeat_pass_touches_no_scheduler_lease_row(store, monkeypatch) -> None:
    """The pass must never read/write ``scheduler_leases`` — it is the
    liveness signal that lease/claim machinery is judged by, not a client of
    it."""
    _reset_throttle(monkeypatch)
    before = {lease.name for lease in store.scheduler_leases()}

    hb.run_heartbeat_pass(store, host="test-host-3")

    after = {lease.name for lease in store.scheduler_leases()}
    assert before == after


def test_heartbeat_registered_as_a_system_profile_pass() -> None:
    from precis.workers.registry import SERVICES_BY_NAME

    spec = SERVICES_BY_NAME["heartbeat"]
    assert "system" in spec.default_profiles
    assert spec.ref_pass is True
