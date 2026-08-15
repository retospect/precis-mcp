"""§A: the ``heartbeat`` worker pass — a per-host system-worker pass, NOT on
``scheduler_leases`` (heartbeat is the liveness signal that lease/claim
machinery is judged by, so it must never depend on it). Self-throttled via an
in-process timestamp, honoring ``PRECIS_HEARTBEAT_INTERVAL_SECONDS``.
"""

from __future__ import annotations

from precis import settings as psettings
from precis.workers import activity
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


# ── crash-safe reclaim (resource_slot_holds sweep, 0118) ───────────────────


def test_report_resource_slots_sweeps_expired_holds_and_warns(store, caplog) -> None:
    """A resource NOT in the real self-probe's vocabulary (``llm:*``, mirroring
    slice-7 local serving) so the probe/sync half of the pass can't clobber the
    row this test seeds — only the reclaim sweep should touch it."""
    from precis.store._resource_slots_ops import insert_slot_hold

    store.sync_host_resource_slots("test-host-4", {"llm:heartbeat-test": 2})
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE resource_slots SET free = 1 "
            "WHERE host = 'test-host-4' AND resource = 'llm:heartbeat-test'"
        )
        conn.commit()
        with conn.transaction():
            insert_slot_hold(conn, "test-host-4", "llm:heartbeat-test", 1, "t:1", -1.0)

    with caplog.at_level("WARNING", logger="precis.workers.heartbeat"):
        hb._report_resource_slots(store, "test-host-4")

    free = {s.resource: s.free for s in store.resource_slots_for_host("test-host-4")}
    assert free["llm:heartbeat-test"] == 2  # the leaked unit was reclaimed
    msgs = [r.getMessage() for r in caplog.records]
    assert any("reclaimed 1 expired resource-slot hold" in m for m in msgs)


def test_report_resource_slots_no_warning_when_nothing_expired(store, caplog) -> None:
    store.sync_host_resource_slots("test-host-5", {"llm:heartbeat-test-2": 1})
    with caplog.at_level("WARNING", logger="precis.workers.heartbeat"):
        hb._report_resource_slots(store, "test-host-5")
    msgs = [r.getMessage() for r in caplog.records]
    assert not any("reclaimed" in m for m in msgs)


def test_heartbeat_registered_as_a_system_profile_pass() -> None:
    from precis.workers.registry import SERVICES_BY_NAME

    spec = SERVICES_BY_NAME["heartbeat"]
    assert "system" in spec.default_profiles
    assert spec.ref_pass is True


# ── Worker boot epoch (the lease-epoch reclaim mechanism) ──────────


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


# ── Live activity publishing (precis.workers.activity) ──────────────


def _reset_activity(monkeypatch) -> None:
    monkeypatch.setattr(activity, "_state", {})


def test_beat_publishes_active_pass_under_own_process(store, monkeypatch) -> None:
    """A beat fired while a pass is active stamps
    ``host_heartbeat.meta.activity[<process>]`` with that pass's snapshot."""
    _reset_throttle(monkeypatch)
    _reset_activity(monkeypatch)
    monkeypatch.setenv("PRECIS_PROCESS", "precis-worker")
    activity.set_pass("fetch_oa")
    activity.note("stub 3/10")

    hb.run_heartbeat_pass(store, host="test-host-activity-1")

    rows = {row.host: row for row in store.recent_heartbeats()}
    published = rows["test-host-activity-1"].meta["activity"]["precis-worker"]
    assert published["pass"] == "fetch_oa"
    assert published["detail"] == "stub 3/10"


def test_beat_omits_activity_key_when_snapshot_is_empty(store, monkeypatch) -> None:
    """Pre-first-pass (empty ``activity.snapshot()``), the beat must not
    write an ``activity`` key at all — avoids writing noise."""
    _reset_throttle(monkeypatch)
    _reset_activity(monkeypatch)
    monkeypatch.setenv("PRECIS_PROCESS", "precis-worker")
    assert activity.snapshot() == {}

    hb.run_heartbeat_pass(store, host="test-host-activity-2")

    rows = {row.host: row for row in store.recent_heartbeats()}
    assert "activity" not in rows["test-host-activity-2"].meta


def test_activity_merges_across_two_processes_on_one_host(store, monkeypatch) -> None:
    """Same multi-process concern as boot_ids: melchior running both
    ``system`` and ``agent`` must not have one process's beat wipe the
    other's last-published activity."""
    _reset_throttle(monkeypatch)
    _reset_activity(monkeypatch)

    monkeypatch.setenv("PRECIS_PROCESS", "precis-worker")
    activity.set_pass("chase")
    hb.run_heartbeat_pass(store, host="test-host-activity-3")

    _reset_throttle(monkeypatch)
    monkeypatch.setenv("PRECIS_PROCESS", "precis-worker-agent")
    monkeypatch.setattr(activity, "_state", {})
    activity.set_pass("job_claude_inproc")
    hb.run_heartbeat_pass(store, host="test-host-activity-3")

    rows = {row.host: row for row in store.recent_heartbeats()}
    published = rows["test-host-activity-3"].meta["activity"]
    assert published["precis-worker"]["pass"] == "chase"
    assert published["precis-worker-agent"]["pass"] == "job_claude_inproc"


def test_settings_env_present_advertised_when_set(store, monkeypatch) -> None:
    """db-resident-settings.md slice 4: a registered setting's env var set
    locally is self-reported into meta, so the condition registry can spot
    a host still carrying one after a DB row takes over."""
    _reset_throttle(monkeypatch)
    monkeypatch.setenv("PRECIS_UNPAYWALL_EMAIL", "ops@example.org")

    hb.run_heartbeat_pass(store, host="test-host-settings-1")

    rows = {row.host: row for row in store.recent_heartbeats()}
    assert (
        "contact.polite_email"
        in rows["test-host-settings-1"].meta["settings_env_present"]
    )


def test_settings_env_present_omitted_when_nothing_set(store, monkeypatch) -> None:
    """Mirrors the ``activity`` key: nothing to report → no key at all, not
    an empty list — avoids writing noise."""
    _reset_throttle(monkeypatch)
    for spec in psettings.REGISTRY.values():
        if spec.env_var:
            monkeypatch.delenv(spec.env_var, raising=False)

    hb.run_heartbeat_pass(store, host="test-host-settings-2")

    rows = {row.host: row for row in store.recent_heartbeats()}
    assert "settings_env_present" not in rows["test-host-settings-2"].meta
