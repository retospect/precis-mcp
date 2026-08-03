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


# ── §H boot epoch (compute-lane-lease-epoch.md) ────────────────────


def _reset_boot_id(monkeypatch) -> None:
    monkeypatch.setattr(hb, "_boot_id", None)
    monkeypatch.setattr(hb, "_boot_process", None)


def test_mint_boot_id_is_idempotent_per_process(monkeypatch) -> None:
    _reset_boot_id(monkeypatch)
    first = hb.mint_boot_id("precis-worker")
    second = hb.mint_boot_id("precis-worker")  # a second registration site
    assert first == second
    assert hb.current_boot_id() == first


def test_current_boot_id_none_before_mint(monkeypatch) -> None:
    _reset_boot_id(monkeypatch)
    assert hb.current_boot_id() is None


def test_advertise_boot_id_now_upserts_immediately_bypassing_throttle(
    store, monkeypatch
) -> None:
    """The boot-time advertise must land BEFORE the first throttled beat —
    even with a huge throttle interval already "consumed", the immediate
    call still writes."""
    _reset_boot_id(monkeypatch)
    monkeypatch.setenv("PRECIS_PROCESS", "precis-worker-agent")
    monkeypatch.setenv("PRECIS_HEARTBEAT_INTERVAL_SECONDS", "3600")

    boot_id = hb.advertise_boot_id_now(store, host="test-host-boot-1")

    rows = {row.host: row for row in store.recent_heartbeats()}
    assert rows["test-host-boot-1"].meta["boot_ids"] == {"precis-worker-agent": boot_id}

    # The throttle clock was reset by the boot advertise — an immediate
    # regular pass call is (correctly) suppressed by the throttle it just set.
    r = hb.run_heartbeat_pass(store, host="test-host-boot-1")
    assert (r.claimed, r.ok, r.failed) == (0, 0, 0)


def test_regular_pass_carries_the_minted_boot_id_every_beat(store, monkeypatch) -> None:
    _reset_boot_id(monkeypatch)
    _reset_throttle(monkeypatch)
    monkeypatch.setenv("PRECIS_PROCESS", "precis-worker")
    hb.mint_boot_id("precis-worker")

    hb.run_heartbeat_pass(store, host="test-host-boot-2")

    rows = {row.host: row for row in store.recent_heartbeats()}
    assert rows["test-host-boot-2"].meta["boot_ids"] == {
        "precis-worker": hb.current_boot_id()
    }


def test_mint_boot_id_warns_once_when_process_is_none(monkeypatch, caplog) -> None:
    """Review Finding 3: minting a boot_id without PRECIS_PROCESS produces
    a boot_id that will NEVER be advertised (see ``_own_boot_ids_meta``) —
    log a warning so this degraded-recovery-latency tradeoff is visible.
    Fires once (the mint body only ever runs once per process lifetime)."""
    _reset_boot_id(monkeypatch)
    with caplog.at_level("WARNING", logger="precis.workers.heartbeat"):
        first = hb.mint_boot_id(None)
        second = hb.mint_boot_id(None)  # idempotent — no second warning
    assert first == second
    warnings = [
        rec for rec in caplog.records if "epoch reclaim disabled" in rec.getMessage()
    ]
    assert len(warnings) == 1


def test_mint_boot_id_no_warning_with_process_set(monkeypatch, caplog) -> None:
    _reset_boot_id(monkeypatch)
    with caplog.at_level("WARNING", logger="precis.workers.heartbeat"):
        hb.mint_boot_id("precis-worker")
    warnings = [
        rec for rec in caplog.records if "epoch reclaim disabled" in rec.getMessage()
    ]
    assert warnings == []
