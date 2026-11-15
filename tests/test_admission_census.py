"""gr647996 S7b — the live admitted-vs-running check.

The heartbeat census (:func:`precis.workers.heartbeat._admission_census`)
folds ``ps``/``docker`` output into over-threshold load, matches it to
live ``resource_slot_holds`` / running-job handles / init-parented
daemons, and publishes ``meta.unadmitted`` (+``beats``), ``admitted_
mem_mb``/``used_mem_mb`` and ``meta.footprint_drift`` on
``host_heartbeat``. Nursery's ``unadmitted-load`` fires only once an
entry has persisted two beats; ``footprint-drift`` covers host drift and
the ``job_footprint_summary`` p95/p50 spread.
"""

from __future__ import annotations

import pytest

from precis.store._resource_slots_ops import insert_slot_hold
from precis.store.types import Tag
from precis.workers import capability_probe, nursery
from precis.workers import heartbeat as hb

pytestmark = pytest.mark.db


def _row(pid, pgid, ppid, *, rss_mb=0.0, cpu=0.0, age_s=120.0, comm="proc"):
    return {
        "pid": pid,
        "pgid": pgid,
        "ppid": ppid,
        "rss_mb": rss_mb,
        "cpu": cpu,
        "age_s": age_s,
        "comm": comm,
    }


def _stub_census_io(monkeypatch, *, rows=None, containers=None):
    """Fake the probe halves; DB reads stay real."""
    monkeypatch.setattr(hb, "_collect_process_rows", lambda: rows or [])
    monkeypatch.setattr(hb, "_collect_containers", lambda: containers or [])
    monkeypatch.setattr(hb, "_collect_container_pids", lambda: set())
    monkeypatch.setattr(hb, "_own_group_chain", lambda r: set())
    monkeypatch.setattr(capability_probe, "_total_mem_mb", lambda: 8192)
    monkeypatch.setattr(capability_probe, "mem_used_pct", lambda: 50.0)


def _mint_running_job(store, host, *, handle=None, container=None):
    meta = {"executor": "ssh_node", "job_type": "fake", "lease_host": host}
    if handle:
        meta["compute_handle"] = handle
    if container:
        meta["container"] = container
    ref = store.insert_ref(kind="job", slug=None, title="j", meta=meta)
    store.add_tag(ref.id, Tag.parse_strict("STATUS:running"), set_by="agent")
    return int(ref.id)


def _mint_hold(store, host, units, holder):
    store.sync_host_resource_slots(host, {"mem_mb": 65536})
    with store.pool.connection() as conn:
        with conn.transaction():
            insert_slot_hold(conn, host, "mem_mb", units, holder, 3600.0)


# ── parsers ──────────────────────────────────────────────────────────


def test_parse_etime_seconds_forms():
    assert hb._parse_etime_seconds("90") == 90.0  # GNU etimes
    assert hb._parse_etime_seconds("01:30") == 90.0  # mm:ss
    assert hb._parse_etime_seconds("01:02:03") == 3723.0  # hh:mm:ss
    assert hb._parse_etime_seconds("2-03:04:05") == 2 * 86400 + 3 * 3600 + 245
    assert hb._parse_etime_seconds("  00:00:10.5 ") == 10.5
    assert hb._parse_etime_seconds("") is None
    assert hb._parse_etime_seconds("garbage") is None
    assert hb._parse_etime_seconds("x-01:00") is None


def test_parse_container_mem_mb():
    assert hb._parse_container_mem_mb("123.4MiB / 8GiB") == 123.4
    assert hb._parse_container_mem_mb("1.5GiB / 8GiB") == 1536.0
    assert hb._parse_container_mem_mb("512KiB / 8GiB") == 0.5
    assert hb._parse_container_mem_mb("2TiB / 8GiB") == 2 * 1024 * 1024
    assert hb._parse_container_mem_mb("oops") is None


# ── process-group folding ─────────────────────────────────────────────


def test_process_groups_thresholds():
    rows = [
        # over RSS threshold → in
        _row(100, 100, 7, rss_mb=600.0),
        _row(101, 100, 100, rss_mb=50.0),
        # under everything → out
        _row(200, 200, 7, rss_mb=100.0, cpu=10.0),
        # cpu-heavy and old → in
        _row(300, 300, 7, rss_mb=10.0, cpu=60.0, age_s=120.0),
        # cpu-heavy but young → out
        _row(400, 400, 7, rss_mb=10.0, cpu=80.0, age_s=10.0),
        # leaderless group → out
        _row(501, 500, 1, rss_mb=900.0),
        # pgid 1 (kernel/launchd domain) → out
        _row(1, 1, 0, rss_mb=9000.0),
    ]
    groups = {g["pgid"]: g for g in hb._process_groups(rows)}
    assert set(groups) == {100, 300}
    assert groups[100]["rss_mb"] == 650.0
    assert groups[100]["leader_ppid"] == 7
    assert groups[300]["cpu"] == 60.0


# ── census matching ───────────────────────────────────────────────────


def test_census_flags_unmatched_group(store, monkeypatch):
    host = "census-h1"
    _stub_census_io(
        monkeypatch,
        rows=[_row(900, 900, 7, rss_mb=900.0, comm="rogue")],
    )
    meta = hb._admission_census(store, host)
    assert meta["admitted_mem_mb"] == 0
    assert meta["used_mem_mb"] == 4096
    (e,) = meta["unadmitted"]
    assert e["key"] == "pgid:900" and e["pgid"] == 900
    assert e["beats"] == 1 and e["rss_mb"] == 900.0


def test_census_skips_daemon_and_own_groups(store, monkeypatch):
    host = "census-h2"
    rows = [
        _row(910, 910, 1, rss_mb=900.0),  # init-parented daemon → skip
        _row(920, 920, 7, rss_mb=900.0),  # claimant's own group → skip
    ]
    _stub_census_io(monkeypatch, rows=rows)
    monkeypatch.setattr(hb, "_own_group_chain", lambda r: {920})
    meta = hb._admission_census(store, host)
    assert meta["unadmitted"] == []


def test_census_matches_hold_pid(store, monkeypatch):
    host = "census-h3"
    _mint_hold(store, host, 2048, f"{host}:930")
    _stub_census_io(
        monkeypatch,
        rows=[_row(930, 930, 7, rss_mb=900.0), _row(931, 930, 930)],
    )
    meta = hb._admission_census(store, host)
    assert meta["unadmitted"] == []
    assert meta["admitted_mem_mb"] == 2048


def test_census_matches_running_job_handles(store, monkeypatch):
    host = "census-h4"
    _mint_running_job(store, host, handle={"pid": 940, "pgid": 940})
    _stub_census_io(
        monkeypatch,
        rows=[_row(940, 940, 7, rss_mb=900.0), _row(941, 940, 940)],
    )
    assert hb._admission_census(store, host)["unadmitted"] == []


def test_census_matches_job_container_and_flags_strays(store, monkeypatch):
    host = "census-h5"
    _mint_running_job(store, host, container="precis-job-1")
    containers = [
        {
            "name": "precis-job-1",
            "project": "",
            "oneoff": False,
            "rss_mb": 500.0,
            "cpu": 10.0,
            "comm": "img",
        },
        {
            "name": "stack-db-1",
            "project": "stack",
            "oneoff": False,
            "rss_mb": 500.0,
            "cpu": 10.0,
            "comm": "pg",
        },  # compose service
        {
            "name": "stray",
            "project": "stack",
            "oneoff": True,
            "rss_mb": 500.0,
            "cpu": 10.0,
            "comm": "img",
        },  # compose run
        {
            "name": "oneshot",
            "project": "",
            "oneoff": False,
            "rss_mb": 500.0,
            "cpu": 10.0,
            "comm": "img",
        },  # docker run
    ]
    _stub_census_io(monkeypatch, containers=containers)
    meta = hb._admission_census(store, host)
    keys = {e["key"] for e in meta["unadmitted"]}
    assert keys == {"container:stray", "container:oneshot"}


def test_census_exempt_env(store, monkeypatch):
    host = "census-h6"
    monkeypatch.setenv("PRECIS_CENSUS_EXEMPT", "^llm-")
    monkeypatch.setattr(hb, "_census_exempt_loaded", False)
    _stub_census_io(
        monkeypatch,
        rows=[_row(950, 950, 7, rss_mb=900.0, comm="llm-server")],
    )
    meta = hb._admission_census(store, host)
    assert meta["unadmitted"] == []


def test_census_two_beat_tracking(store, monkeypatch):
    host = "census-h7"
    _stub_census_io(
        monkeypatch,
        rows=[_row(960, 960, 7, rss_mb=900.0, comm="rogue")],
    )
    first = hb._admission_census(store, host)
    assert first["unadmitted"][0]["beats"] == 1
    store.record_heartbeat(host, meta=first)
    second = hb._admission_census(store, host)
    assert second["unadmitted"][0]["beats"] == 2
    store.record_heartbeat(host, meta=second)
    third = hb._admission_census(store, host)
    assert third["unadmitted"][0]["beats"] == 3


def test_census_footprint_drift(store, monkeypatch):
    host = "census-h8"
    _mint_hold(store, host, 512, f"{host}:970")
    _stub_census_io(
        monkeypatch,
        rows=[_row(970, 970, 7, rss_mb=900.0)],  # 900 > 512 * 1.25
    )
    meta = hb._admission_census(store, host)
    (d,) = meta["footprint_drift"]
    assert d["admitted_mb"] == 512 and d["measured_mb"] == 900.0
    # within margin → no drift
    _stub_census_io(
        monkeypatch,
        rows=[_row(970, 970, 7, rss_mb=600.0)],
    )
    meta = hb._admission_census(store, host)
    assert "footprint_drift" not in meta


def test_census_ps_failure_writes_nothing(store, monkeypatch):
    monkeypatch.setattr(hb, "_collect_process_rows", lambda: None)
    assert hb._admission_census(store, "census-h9") == {}


# ── nursery: unadmitted-load ──────────────────────────────────────────


def _beat_unadmitted(store, host, entries):
    store.record_heartbeat(host, meta={"unadmitted": entries})


def test_unadmitted_load_fires_on_second_beat(store):
    one = [{"key": "pgid:1", "comm": "a", "rss_mb": 800, "cpu": 0, "beats": 1}]
    two = [{"key": "pgid:1", "comm": "a", "rss_mb": 800, "cpu": 0, "beats": 2}]
    _beat_unadmitted(store, "nur-h1", one)
    assert nursery._detect_unadmitted_load(store) == []
    _beat_unadmitted(store, "nur-h1", two)
    out = nursery._detect_unadmitted_load(store)
    (s,) = [s for s in out if s.fingerprint_key == "unadmitted-load:nur-h1"]
    assert s.category == "unadmitted-load" and "nur-h1" in s.title


def test_unadmitted_load_ignores_stale_rows(store):
    entries = [{"key": "pgid:1", "comm": "a", "rss_mb": 800, "cpu": 0, "beats": 5}]
    _beat_unadmitted(store, "nur-h2", entries)
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE host_heartbeat SET ts = now() - interval '30 minutes' "
            "WHERE host = 'nur-h2'"
        )
    out = nursery._detect_unadmitted_load(store)
    assert not [s for s in out if "nur-h2" in (s.fingerprint_key or "")]


# ── nursery: footprint-drift ──────────────────────────────────────────


def test_footprint_drift_host_arm(store, monkeypatch):
    monkeypatch.setattr(store, "job_footprint_summary", lambda days=None: [])
    store.record_heartbeat(
        "nur-h3",
        meta={
            "footprint_drift": [
                {"key": "pgid:1", "comm": "x", "admitted_mb": 100, "measured_mb": 500.0}
            ]
        },
    )
    out = nursery._detect_footprint_drift(store)
    (s,) = [s for s in out if s.fingerprint_key == "footprint-drift:host:nur-h3"]
    assert "nur-h3" in s.title


def test_footprint_drift_spread_arm(store, monkeypatch):
    summary = [
        {
            "job_type": "fat",
            "size_name": None,
            "size_value": None,
            "n": 30,
            "rss_p50_mb": 100.0,
            "rss_p95_mb": 900.0,
            "cpu_p50_s": 1.0,
            "cpu_p95_s": 2.0,
        },
        {
            "job_type": "thin",
            "size_name": None,
            "size_value": None,
            "n": 30,
            "rss_p50_mb": 100.0,
            "rss_p95_mb": 150.0,
            "cpu_p50_s": 1.0,
            "cpu_p95_s": 2.0,
        },
        {
            "job_type": "rare",
            "size_name": None,
            "size_value": None,
            "n": 5,
            "rss_p50_mb": 10.0,
            "rss_p95_mb": 900.0,
            "cpu_p50_s": 1.0,
            "cpu_p95_s": 2.0,
        },
    ]
    monkeypatch.setattr(store, "job_footprint_summary", lambda days=None: summary)
    out = nursery._detect_footprint_drift(store)
    keys = {s.fingerprint_key for s in out}
    assert "footprint-drift:spread:fat" in keys
    assert "footprint-drift:spread:thin" not in keys  # ratio 1.5
    assert "footprint-drift:spread:rare" not in keys  # n < 20
