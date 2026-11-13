"""Real-PG tests for :mod:`precis.workers.admission` (gr647996 S1).

``admit()`` is the one admission entry point: an all-or-nothing reservation
of a :class:`Footprint` against a host's hard ``resource_slots`` rows, each
unit bracketed by a TTL ``resource_slot_holds`` lease. ``mem_mb``,
``cpu_milli`` and ``vram_mb`` are multi-unit rows: a 2048 MiB hold takes
2048 units of ``mem_mb``.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from precis.workers import admission
from precis.workers import capability_probe as cap
from precis.workers.admission import Footprint


@pytest.fixture(autouse=True)
def _fresh_capacity_cache() -> None:
    cap.reset_capacity_cache()


def _free(store, host: str) -> dict[str, int]:
    return {s.resource: s.free for s in store.resource_slots_for_host(host)}


def _holds(store, host: str) -> list[tuple[str, int]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT resource, units FROM resource_slot_holds "
            "WHERE host = %s ORDER BY resource",
            (host,),
        ).fetchall()
    return [(str(r), int(u)) for r, u in rows]


def _admit(store, host: str, fp: Footprint, holder: str = "t:1", **kw):
    with store.pool.connection() as conn:
        with conn.transaction():
            return admission.admit(conn, host, fp, holder, **kw)


def _release(store, adm) -> None:
    with store.pool.connection() as conn:
        with conn.transaction():
            admission.release(conn, adm)


def test_footprint_requirements_skip_unset_fields() -> None:
    fp = Footprint(mem_mb=2048, llm={"llm:m": 1}, account={"account:devin": 1})
    assert fp.requirements() == {
        "mem_mb": 2048,
        "llm:m": 1,
        "account:devin": 1,
    }
    assert Footprint().requirements() == {}


def test_requirements_follow_the_shared_lock_order() -> None:
    fp = Footprint(
        vram_mb=1, mem_mb=1, cpu_milli=1, gpu=1, llm={"llm:m": 1}, account={"a": 1}
    )
    assert list(fp.requirements()) == sorted(fp.requirements())


def test_off_host_footprint_reserves_no_host_dimensions() -> None:
    fp = Footprint(
        mem_mb=512, cpu_milli=500, gpu=1, account={"account:devin": 1}, off_host=True
    )
    assert fp.requirements() == {"account:devin": 1}


def test_admit_is_all_or_nothing(store) -> None:
    """One dimension short → None, and every other dimension is untouched."""
    store.sync_host_resource_slots("ah1", {"mem_mb": 4096, "cpu_milli": 1000})
    fp = Footprint(mem_mb=2048, cpu_milli=2000)  # cpu short
    assert _admit(store, "ah1", fp) is None
    assert _free(store, "ah1") == {"cpu_milli": 1000, "mem_mb": 4096}
    assert _holds(store, "ah1") == []


def test_admit_refuses_a_dimension_the_host_does_not_offer(store) -> None:
    store.sync_host_resource_slots("ah2", {"mem_mb": 4096})
    assert _admit(store, "ah2", Footprint(mem_mb=1, vram_mb=1)) is None
    assert _free(store, "ah2") == {"mem_mb": 4096}


def test_mem_units_consumed_and_refunded_on_release(store) -> None:
    store.sync_host_resource_slots("ah3", {"mem_mb": 8192, "llm:m": 2})
    adm = _admit(store, "ah3", Footprint(mem_mb=2048, llm={"llm:m": 1}))
    assert adm is not None
    assert adm.host == "ah3"
    assert _free(store, "ah3") == {"llm:m": 1, "mem_mb": 6144}
    assert _holds(store, "ah3") == [("llm:m", 1), ("mem_mb", 2048)]

    _release(store, adm)
    assert _free(store, "ah3") == {"llm:m": 2, "mem_mb": 8192}
    assert _holds(store, "ah3") == []


def test_release_twice_never_double_refunds(store) -> None:
    store.sync_host_resource_slots("ah4", {"mem_mb": 4096})
    a = _admit(store, "ah4", Footprint(mem_mb=1024))
    b = _admit(store, "ah4", Footprint(mem_mb=1024))
    assert a is not None and b is not None
    _release(store, a)
    _release(store, a)  # hold already closed → no second refund
    assert _free(store, "ah4") == {"mem_mb": 3072}  # b still held


def test_ttl_expiry_reclaims(store) -> None:
    """A holder that never releases is refunded by the expired-hold sweep,
    and a late release afterwards does not refund again."""
    store.sync_host_resource_slots("ah5", {"mem_mb": 4096, "cpu_milli": 2000})
    adm = _admit(store, "ah5", Footprint(mem_mb=3000, cpu_milli=1500), ttl_s=-1.0)
    assert adm is not None
    assert _free(store, "ah5") == {"cpu_milli": 500, "mem_mb": 1096}

    assert store.reclaim_expired_slot_holds() == 2
    assert _free(store, "ah5") == {"cpu_milli": 2000, "mem_mb": 4096}

    # The capacity is free again for a new holder of the full amount …
    other = _admit(store, "ah5", Footprint(mem_mb=4096))
    assert other is not None
    # … and the reclaimed holder's late release leaves it alone.
    _release(store, adm)
    assert _free(store, "ah5") == {"cpu_milli": 2000, "mem_mb": 0}


def test_default_ttl_follows_wall_s(store) -> None:
    store.sync_host_resource_slots("ah6", {"mem_mb": 100})
    _admit(store, "ah6", Footprint(mem_mb=10, wall_s=30.0))
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT EXTRACT(EPOCH FROM expires_at - acquired_at) "
            "FROM resource_slot_holds WHERE host = 'ah6'"
        ).fetchone()
    assert row is not None
    assert 30.0 < float(row[0]) <= 30.0 + admission.TTL_MARGIN_S + 1.0


def test_two_holders_exceeding_capacity_second_gets_none(store) -> None:
    store.sync_host_resource_slots("ah7", {"mem_mb": 4096})
    first = _admit(store, "ah7", Footprint(mem_mb=3072), holder="a:1")
    second = _admit(store, "ah7", Footprint(mem_mb=2048), holder="b:2")
    assert first is not None
    assert second is None
    assert _free(store, "ah7") == {"mem_mb": 1024}
    assert _holds(store, "ah7") == [("mem_mb", 3072)]


def test_empty_footprint_admits_without_holds(store) -> None:
    adm = _admit(store, "ah8", Footprint())
    assert adm is not None
    assert adm.holds == ()
    _release(store, adm)


# ── capacity rows: measured totals minus reserves ─────────────────────────

_RESERVE_ENVS = (
    "PRECIS_HOST_MEM_RESERVE_MB",
    "PRECIS_HOST_CPU_RESERVE_MILLI",
    "PRECIS_HOST_VRAM_RESERVE_MB",
)


@pytest.fixture
def _no_reserve_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _RESERVE_ENVS:
        monkeypatch.delenv(name, raising=False)


def _fake_host(
    monkeypatch: pytest.MonkeyPatch,
    *,
    mem_mb: int | None,
    cpus: int | None,
    vram_mb: int | None,
) -> None:
    monkeypatch.setattr(cap, "_total_mem_mb", lambda: mem_mb)
    monkeypatch.setattr(
        cap, "_available_cpu_milli", lambda: None if cpus is None else cpus * 1000
    )
    monkeypatch.setattr(cap, "_total_vram_mb", lambda: vram_mb)
    # Homogeneous-cards stand-in: the per-card ceiling equals the patched
    # total (one card), None stays unknown.
    monkeypatch.setattr(
        cap, "_max_card_vram_mb", lambda: None if vram_mb is None else vram_mb
    )


@pytest.mark.usefixtures("_no_reserve_env")
def test_capacity_is_measured_total_minus_default_reserve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_host(monkeypatch, mem_mb=32768, cpus=8, vram_mb=0)
    assert cap.probe_host_capacity() == {
        "mem_mb": 32768 - cap.DEFAULT_MEM_RESERVE_MB,
        "cpu_milli": 8000,
        "vram_mb": 0,  # no GPU → definitively absent, row retracted
        "vram_card_mb": 0,  # no card to fit a model on, row retracted
    }


@pytest.mark.usefixtures("_no_reserve_env")
def test_capacity_reserves_come_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_host(monkeypatch, mem_mb=16384, cpus=4, vram_mb=24576)
    monkeypatch.setenv("PRECIS_HOST_MEM_RESERVE_MB", "4096")
    monkeypatch.setenv("PRECIS_HOST_CPU_RESERVE_MILLI", "1000")
    monkeypatch.setenv("PRECIS_HOST_VRAM_RESERVE_MB", "2048")
    assert cap.probe_host_capacity() == {
        "mem_mb": 12288,
        "cpu_milli": 3000,
        "vram_mb": 22528,
        # The per-card ceiling is raw: the host-wide VRAM reserve models
        # aggregate headroom, per-card fit gets its margin from the claim.
        "vram_card_mb": 24576,
    }


@pytest.mark.usefixtures("_no_reserve_env")
def test_capacity_unknown_measurement_leaves_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_host(monkeypatch, mem_mb=None, cpus=None, vram_mb=None)
    assert cap.probe_host_capacity() == {
        "mem_mb": None,
        "cpu_milli": None,
        "vram_mb": None,
        "vram_card_mb": None,
    }


@pytest.mark.usefixtures("_no_reserve_env")
def test_capacity_reserve_above_total_clamps_to_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_host(monkeypatch, mem_mb=4096, cpus=2, vram_mb=0)
    assert cap.probe_host_capacity()["mem_mb"] == 0


# ── live free probes: admittable headroom (total minus reserve) ───────────


@pytest.mark.usefixtures("_no_reserve_env")
def test_live_mem_free_nets_the_os_reserve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """td655171 review: the live headroom the claim cross-checks must not
    spend the OS reserve the static total-minus-reserve ledger protects —
    an untracked consumer eating into it vetoes, never borrows."""
    monkeypatch.setattr(cap, "_linux_mem_available_mb", lambda: 32768)
    assert cap.mem_free_mb() == 32768 - cap.DEFAULT_MEM_RESERVE_MB
    monkeypatch.setenv("PRECIS_HOST_MEM_RESERVE_MB", "4096")
    assert cap.mem_free_mb() == 32768 - 4096


@pytest.mark.usefixtures("_no_reserve_env")
def test_live_mem_free_at_or_under_reserve_reads_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Available under the reserve is a real ``0`` — vetoes memory
    admissions, not the ``None`` that would read as dark."""
    monkeypatch.setattr(cap, "_linux_mem_available_mb", lambda: 4096)
    assert cap.mem_free_mb() == 0


def test_live_mem_free_unknown_stays_dark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unmeasurable stays ``None`` — admission reads as dark, ledger only."""
    monkeypatch.setattr(cap, "_linux_mem_available_mb", lambda: None)
    monkeypatch.setattr(cap.sys, "platform", "linux")
    assert cap.mem_free_mb() is None


@pytest.mark.usefixtures("_no_reserve_env")
def test_live_vram_free_nets_the_host_reserve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The host-wide VRAM reserve comes off the best-card scalar too:
    ``0`` by default (today's reading), a configured reserve is kept."""
    monkeypatch.setattr(cap, "vram_free_per_card_mb", lambda: [1024, 81920])
    assert cap.vram_free_mb() == 81920
    monkeypatch.setenv("PRECIS_HOST_VRAM_RESERVE_MB", "2048")
    assert cap.vram_free_mb() == 81920 - 2048


def test_darwin_total_mem_from_sysctl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cap, "_linux_mem_total_mb", lambda: None)
    monkeypatch.setattr(cap.sys, "platform", "darwin")
    monkeypatch.setattr(
        cap.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="68719476736\n"),
    )
    assert cap._total_mem_mb() == 65536


def test_vram_sums_nvidia_smi_cards(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cap.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        cap.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="81920\n81920\n"),
    )
    assert cap._total_vram_mb() == 163840


@pytest.mark.usefixtures("_no_reserve_env")
def test_vram_card_ceiling_is_the_largest_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """td655172: the per-card row is the biggest single card, not the
    host-wide sum — a model must fit ONE card."""
    monkeypatch.setattr(cap.shutil, "which", lambda name: f"/usr/bin/{name}")

    def _run(*a: object, **k: object) -> SimpleNamespace:
        raw = a[0] if a else k.get("args", [])
        parts = raw if isinstance(raw, (list, tuple)) else [raw]
        argv = [str(x) for x in parts]
        field = (
            "memory.total" if any("memory.total" in x for x in argv) else "memory.free"
        )
        out = (
            "24576\n81920\n"  # two cards: 24 GiB + 80 GiB
            if field == "memory.total"
            else "1024\n81920\n"
        )
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    monkeypatch.setattr(cap.subprocess, "run", _run)
    assert cap._max_card_vram_mb() == 81920
    assert cap._total_vram_mb() == 106496
    # Largest single card, not the 82944 sum: a model must fit one card.
    assert cap.vram_free_mb() == 81920
    # …and the per-card list the claim binds a card from keeps the
    # nvidia-smi index order (td655172 card assignment).
    assert cap.vram_free_per_card_mb() == [1024, 81920]


def test_vram_free_absent_without_nvidia_smi(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cap.shutil, "which", lambda name: None)
    assert cap.vram_free_mb() is None
    assert cap.vram_free_per_card_mb() is None


def test_vram_unparseable_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unified-memory cards report ``[N/A]``: unknown, never a retract."""
    monkeypatch.setattr(cap.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        cap.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="[N/A]\n"),
    )
    assert cap._total_vram_mb() is None


def test_vram_absent_without_nvidia_smi(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cap.shutil, "which", lambda name: None)
    assert cap._total_vram_mb() == 0


@pytest.mark.usefixtures("_no_reserve_env")
def test_heartbeat_publishes_capacity_rows(store, monkeypatch) -> None:
    """The heartbeat writes the capacity as hard rows ``admit()`` reserves
    against, and a resync keeps an outstanding reservation."""
    from precis.workers import heartbeat as hb

    _fake_host(monkeypatch, mem_mb=20480, cpus=2, vram_mb=0)
    hb._report_resource_slots(store, "ah9")
    rows = {s.resource: s for s in store.resource_slots_for_host("ah9")}
    assert rows["mem_mb"].capacity == 20480 - cap.DEFAULT_MEM_RESERVE_MB
    assert rows["mem_mb"].kind == "hard"
    assert rows["cpu_milli"].capacity == 2000
    assert "vram_mb" not in rows

    adm = _admit(store, "ah9", Footprint(mem_mb=1024, cpu_milli=500))
    assert adm is not None
    hb._report_resource_slots(store, "ah9")
    free = _free(store, "ah9")
    assert free["mem_mb"] == 20480 - cap.DEFAULT_MEM_RESERVE_MB - 1024
    assert free["cpu_milli"] == 1500


@pytest.mark.usefixtures("_no_reserve_env")
def test_capacity_probe_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """Static totals: re-probed at most every ``_CAPACITY_TTL_S``."""
    calls = {"n": 0}

    def mem() -> int:
        calls["n"] += 1
        return 32768

    _fake_host(monkeypatch, mem_mb=0, cpus=4, vram_mb=0)
    monkeypatch.setattr(cap, "_total_mem_mb", mem)
    clock = {"t": 1000.0}
    monkeypatch.setattr(cap.time, "monotonic", lambda: clock["t"])

    first = cap.probe_host_capacity()
    clock["t"] += cap._CAPACITY_TTL_S - 1
    assert cap.probe_host_capacity() == first
    assert calls["n"] == 1
    clock["t"] += 2
    cap.probe_host_capacity()
    assert calls["n"] == 2


# ── available CPUs: affinity, then a cgroup v2 quota ──────────────────────


def _cgroup_tree(tmp_path: Path, rel: str, cpu_max: dict[str, str]) -> None:
    proc = tmp_path / "proc_self_cgroup"
    proc.write_text(rel, encoding="utf-8")
    root = tmp_path / "cg"
    for sub, text in cpu_max.items():
        d = root / sub
        d.mkdir(parents=True, exist_ok=True)
        (d / "cpu.max").write_text(text, encoding="utf-8")
    root.mkdir(exist_ok=True)


def _point_cgroup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cap, "_PROC_SELF_CGROUP", str(tmp_path / "proc_self_cgroup"))
    monkeypatch.setattr(cap, "_CGROUP_ROOT", str(tmp_path / "cg"))


def test_cpu_uses_affinity_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cap.os, "sched_getaffinity", lambda pid: {0, 1, 2}, raising=False
    )
    monkeypatch.setattr(cap.os, "cpu_count", lambda: 64)
    monkeypatch.setattr(cap, "_cgroup_cpu_quota_milli", lambda: None)
    assert cap._available_cpu_milli() == 3000


def test_cpu_falls_back_to_cpu_count(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(cap.os, "sched_getaffinity", raising=False)
    monkeypatch.setattr(cap.os, "cpu_count", lambda: 8)
    monkeypatch.setattr(cap, "_cgroup_cpu_quota_milli", lambda: None)
    assert cap._available_cpu_milli() == 8000


def test_cpu_capped_by_tightest_cgroup_quota(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The process's cgroup and its ancestors: the tightest quota wins,
    ``max`` (unlimited) and missing files are ignored."""
    _cgroup_tree(
        tmp_path,
        "0::/system.slice/precis.service\n",
        {
            "system.slice/precis.service": "150000 100000\n",
            "system.slice": "max 100000\n",
        },
    )
    _point_cgroup(monkeypatch, tmp_path)
    monkeypatch.setattr(
        cap.os, "sched_getaffinity", lambda pid: set(range(4)), raising=False
    )
    assert cap._cgroup_cpu_quota_milli() == 1500
    assert cap._available_cpu_milli() == 1500


def test_cpu_quota_above_affinity_keeps_affinity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _cgroup_tree(tmp_path, "0::/\n", {"": "800000 100000\n"})
    _point_cgroup(monkeypatch, tmp_path)
    monkeypatch.setattr(cap.os, "sched_getaffinity", lambda pid: {0, 1}, raising=False)
    assert cap._available_cpu_milli() == 2000


def test_cgroup_v1_has_no_quota(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _cgroup_tree(tmp_path, "12:cpu,cpuacct:/user.slice\n", {})
    _point_cgroup(monkeypatch, tmp_path)
    assert cap._cgroup_cpu_quota_milli() is None


# ── change-only capacity sync ──────────────────────────────────────────────


def _updated_at(store, host: str) -> dict[str, object]:
    return {s.resource: s.updated_at for s in store.resource_slots_for_host(host)}


def test_capacity_sync_writes_only_changed_rows(store) -> None:
    store.sync_host_capacity("cs1", {"mem_mb": 4096, "cpu_milli": 2000, "vram_mb": 0})
    before = _updated_at(store, "cs1")
    assert set(before) == {"cpu_milli", "mem_mb"}

    store.sync_host_capacity("cs1", {"mem_mb": 4096, "cpu_milli": 2000, "vram_mb": 0})
    assert _updated_at(store, "cs1") == before  # nothing rewritten

    store.sync_host_capacity(
        "cs1", {"mem_mb": 8192, "cpu_milli": 2000, "vram_mb": None}
    )
    after = _updated_at(store, "cs1")
    assert after["cpu_milli"] == before["cpu_milli"]
    assert after["mem_mb"] != before["mem_mb"]
    rows = {s.resource: s for s in store.resource_slots_for_host("cs1")}
    assert rows["mem_mb"].capacity == 8192

    store.sync_host_capacity("cs1", {"mem_mb": 0})
    assert "mem_mb" not in _free(store, "cs1")


# ── concurrency: lock order and the capacity race ──────────────────────────


def _wait_lock_blocked(store, pid_box: list[int], timeout_s: float = 10.0) -> None:
    """Block until the backend in ``pid_box`` waits on a row lock."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if pid_box:
            with store.pool.connection() as c:
                row = c.execute(
                    "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s",
                    (pid_box[0],),
                ).fetchone()
            if row is not None and row[0] == "Lock":
                return
        time.sleep(0.05)
    raise AssertionError("resync never blocked on the admitted row")


def test_admit_and_capacity_resync_never_deadlock(store) -> None:
    """A holder takes one row, a heartbeat resync then queues behind it, and
    the holder takes a second row. In one shared lock order the resync waits
    on the first row while holding nothing, so the second take proceeds.
    With the resync upserting ``mem_mb`` before ``cpu_milli`` (the pre-fix
    order) this is a lock cycle and Postgres aborts one side."""
    host = "lk1"
    store.sync_host_capacity(host, {"cpu_milli": 4000, "mem_mb": 4096, "vram_mb": 1024})
    errors: list[BaseException] = []
    pid_box: list[int] = []

    def resync() -> None:
        try:
            with store.pool.connection() as b:
                pid_box.append(b.info.backend_pid)
                with b.transaction():
                    store.sync_host_capacity(
                        host,
                        {"vram_mb": 2048, "mem_mb": 8192, "cpu_milli": 8000},
                        conn=b,
                    )
        except BaseException as exc:  # surfaced by the assert below
            errors.append(exc)

    t = threading.Thread(target=resync)
    with store.pool.connection() as a:
        with a.transaction():
            a.execute("SET LOCAL lock_timeout = '5s'")
            # cpu_milli sorts first: the resync will queue on it.
            assert admission.admit(a, host, Footprint(cpu_milli=1000), "a:1")
            t.start()
            _wait_lock_blocked(store, pid_box)
            assert admission.admit(a, host, Footprint(mem_mb=1024), "a:1")
    t.join(timeout=30)
    assert not t.is_alive()
    assert not errors, errors
    assert _free(store, host) == {"cpu_milli": 7000, "mem_mb": 7168, "vram_mb": 2048}


def test_concurrent_admits_past_capacity_exactly_one_wins(store) -> None:
    """Two connections race for a capacity that fits one holder: exactly
    one is admitted and ``free`` never goes negative."""
    host = "rc1"
    store.sync_host_resource_slots(host, {"mem_mb": 3000, "cpu_milli": 1000})
    barrier = threading.Barrier(2)
    results: list[object] = []
    errors: list[BaseException] = []

    def contender(tag: str) -> None:
        try:
            with store.pool.connection() as c:
                with c.transaction():
                    barrier.wait(timeout=10)
                    results.append(
                        admission.admit(
                            c, host, Footprint(mem_mb=2000, cpu_milli=600), tag
                        )
                    )
                    time.sleep(0.2)  # keep the tx open across the other's try
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=contender, args=(t,)) for t in ("a:1", "b:2")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, errors
    assert sum(r is not None for r in results) == 1
    assert _free(store, host) == {"cpu_milli": 400, "mem_mb": 1000}
    with store.pool.connection() as c:
        row = c.execute(
            "SELECT MIN(free) FROM resource_slots WHERE host = %s", (host,)
        ).fetchone()
    assert row is not None and row[0] >= 0
    assert len(_holds(store, host)) == 2  # the winner's two dimensions only
