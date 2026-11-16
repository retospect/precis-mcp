"""Real-PG tests for :mod:`precis.workers.admission` (gr647996 S1).

``admit()`` is the one admission entry point: an all-or-nothing reservation
of a :class:`Footprint` against a host's hard ``resource_slots`` rows, each
unit bracketed by a TTL ``resource_slot_holds`` lease. ``mem_mb``,
``cpu_milli`` and ``vram_mb`` are multi-unit rows: a 2048 MiB hold takes
2048 units of ``mem_mb``.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from precis.budget import meter
from precis.utils import outbound_policy, rate_limit
from precis.utils.llm import openai_tools
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


# ── spawn: the one admitted detached launcher (gr657245 S2) ────────────────


class _FakeChild:
    """Stand-in for the Popen'd child: a pid plus a ``wait`` that raises
    ``TimeoutExpired`` ``timeouts`` times (so the babysit must renew between
    passes) and then blocks on ``finish`` — the test releases the exit."""

    def __init__(self, pid: int = 4321, timeouts: int = 1) -> None:
        self.pid = pid
        self.waits: list[float] = []
        self.finish = threading.Event()
        self._timeouts = timeouts

    def wait(self, timeout: float | None = None) -> int:
        self.waits.append(float(timeout) if timeout is not None else -1.0)
        if self._timeouts > 0:
            self._timeouts -= 1
            raise subprocess.TimeoutExpired(
                cmd="fake-child", timeout=timeout if timeout is not None else -1.0
            )
        assert self.finish.wait(30), "test never released the fake child"
        return 0


def test_spawn_admits_launches_releases(
    store, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Holds exist while the child lives (renewed on schedule) and the
    exit-first babysit frees the footprint promptly at exit."""
    host = "sp1"
    store.sync_host_resource_slots(host, {"mem_mb": 8192, "cpu_milli": 4000})
    monkeypatch.setenv("PRECIS_HOST_NAME", host)
    child = _FakeChild(pid=4321, timeouts=1)
    launched: dict[str, object] = {}

    def popen(argv: list[str], **kwargs: object) -> _FakeChild:
        launched["argv"] = argv
        launched["env"] = kwargs["env"]
        return child

    renewals: list[tuple[int, ...]] = []
    released = threading.Event()
    real_renew = admission._renew
    real_release = admission._release_quietly

    def spy_renew(store_: object, adm: admission.Admission, ttl_s: float) -> None:
        renewals.append(tuple(h.hold_id for h in adm.holds))
        real_renew(store_, adm, ttl_s)

    def spy_release(store_: object, adm: admission.Admission) -> None:
        try:
            real_release(store_, adm)
        finally:
            released.set()

    monkeypatch.setattr(admission, "_renew", spy_renew)
    monkeypatch.setattr(admission, "_release_quietly", spy_release)

    pid = admission.spawn(
        store,
        host,
        Footprint(cpu_milli=1000, mem_mb=2048, wall_s=30.0),
        "t:spawn1",
        ["echo", "hi"],
        cwd=tmp_path,
        env={"PATH": "/usr/bin"},
        stdin=None,
        log_path=tmp_path / "run.log",
        exit_file=tmp_path / "exit",
        popen=popen,
    )
    assert pid == 4321
    assert launched["argv"] == [
        "/bin/sh",
        "-c",
        admission._SPAWN_SCRIPT,
        "admitted",
        "echo",
        "hi",
    ]
    env = launched["env"]
    assert isinstance(env, dict)
    assert env["RS_IN"] == os.devnull
    assert env["RS_LOG"] == str(tmp_path / "run.log")
    assert env["RS_EXIT"] == str(tmp_path / "exit")

    # Admitted: the holds exist and capacity is consumed while the child
    # lives (the admit committed before spawn returned).
    assert _holds(store, host) == [("cpu_milli", 1000), ("mem_mb", 2048)]
    assert _free(store, host) == {"cpu_milli": 3000, "mem_mb": 6144}

    # The babysit renews on schedule after the first wait times out …
    deadline = time.monotonic() + 10
    while not renewals and time.monotonic() < deadline:
        time.sleep(0.02)
    assert renewals, "babysit never renewed the holds"
    # … and the footprint is still reserved while the child runs.
    assert _holds(store, host) == [("cpu_milli", 1000), ("mem_mb", 2048)]

    child.finish.set()
    assert released.wait(10), "babysit never released at child exit"
    assert _holds(store, host) == []
    assert _free(store, host) == {"cpu_milli": 4000, "mem_mb": 8192}


def test_spawn_refused_when_no_capacity(
    store, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A host that publishes rows but cannot fit the footprint refuses —
    and nothing is launched."""
    host = "sp2"
    store.sync_host_resource_slots(host, {"mem_mb": 1024, "cpu_milli": 4000})
    monkeypatch.setenv("PRECIS_HOST_NAME", host)

    def popen(*args: object, **kwargs: object) -> object:
        raise AssertionError("popen called despite the capacity refusal")

    with pytest.raises(admission.Refused):
        admission.spawn(
            store,
            host,
            Footprint(cpu_milli=1000, mem_mb=2048),
            "t:spawn2",
            ["echo", "hi"],
            log_path=tmp_path / "run.log",
            exit_file=tmp_path / "exit",
            popen=popen,
        )
    assert _holds(store, host) == []
    assert _free(store, host) == {"cpu_milli": 4000, "mem_mb": 1024}


def test_spawn_unheld_when_host_publishes_nothing(
    store, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An unadvertised host launches without holds — admission never vetoes
    a launch it cannot measure."""
    host = "sp3"
    monkeypatch.setenv("PRECIS_HOST_NAME", host)
    child = _FakeChild(pid=4322, timeouts=0)
    pid = admission.spawn(
        store,
        host,
        Footprint(cpu_milli=1000, mem_mb=2048),
        "t:spawn3",
        ["echo", "hi"],
        log_path=tmp_path / "run.log",
        exit_file=tmp_path / "exit",
        popen=lambda *a, **k: child,
    )
    assert pid == 4322
    assert _holds(store, host) == []
    child.finish.set()


def _hold_identities(
    store, host: str
) -> list[tuple[str, str | None, str | None, str | None]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT resource, holder_host, holder_process, holder_boot_id "
            "FROM resource_slot_holds WHERE host = %s ORDER BY resource",
            (host,),
        ).fetchall()
    return [(str(r), h, p, b) for r, h, p, b in rows]


def test_spawn_holds_carry_no_worker_identity(
    store, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The detached child outlives the launching worker: its holds carry
    NO holder identity, so the reaper's epoch arm cannot refund them when
    the worker's generation is replaced while the child still runs —
    TTL-only reclaim preserves the accounting through child exit (review
    td702053)."""
    host = "sp4"
    store.sync_host_resource_slots(host, {"mem_mb": 8192, "cpu_milli": 4000})
    monkeypatch.setenv("PRECIS_HOST_NAME", host)
    child = _FakeChild(pid=4323, timeouts=0)
    pid = admission.spawn(
        store,
        host,
        Footprint(cpu_milli=1000, mem_mb=2048),
        "t:spawn4",
        ["echo", "hi"],
        log_path=tmp_path / "run.log",
        exit_file=tmp_path / "exit",
        popen=lambda *a, **k: child,
    )
    assert pid == 4323
    identities = _hold_identities(store, host)
    assert [r for r, _h, _p, _b in identities] == ["cpu_milli", "mem_mb"]
    assert all(h is None and p is None and b is None for _r, h, p, b in identities)
    child.finish.set()


def test_spawn_releases_admission_when_popen_raises(
    store, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A popen failure after a successful admit releases the just-taken
    holds before the error propagates — no footprint leaks to its TTL for
    a child that never started (review td702053)."""
    host = "sp5"
    store.sync_host_resource_slots(host, {"mem_mb": 8192, "cpu_milli": 4000})
    monkeypatch.setenv("PRECIS_HOST_NAME", host)

    def broken(*args: object, **kwargs: object) -> object:
        raise OSError("exec format error")

    with pytest.raises(OSError, match="exec format error"):
        admission.spawn(
            store,
            host,
            Footprint(cpu_milli=1000, mem_mb=2048),
            "t:spawn5",
            ["echo", "hi"],
            log_path=tmp_path / "run.log",
            exit_file=tmp_path / "exit",
            popen=broken,
        )
    assert _holds(store, host) == []
    assert _free(store, host) == {"cpu_milli": 4000, "mem_mb": 8192}


# ── remote LLM calls: one llm-api slot, counted refusals (gr657245 S2) ─────

OPENROUTER_URL = "https://openrouter.ai/api/v1"
_LLM_RESOURCE = "llm-api:openrouter"


def _seed_api_slot(store, capacity: int) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "DELETE FROM resource_slot_holds WHERE host = 'api' AND resource = %s",
            (_LLM_RESOURCE,),
        )
        conn.execute(
            "DELETE FROM resource_slots WHERE host = 'api' AND resource = %s",
            (_LLM_RESOURCE,),
        )
        conn.execute(
            "INSERT INTO resource_slots (host, resource, capacity, free, kind) "
            "VALUES ('api', %s, %s, %s, 'hard')",
            (_LLM_RESOURCE, capacity, capacity),
        )
        conn.commit()


def _api_slot_free(store) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT free FROM resource_slots WHERE host = 'api' AND resource = %s",
            (_LLM_RESOURCE,),
        ).fetchone()
    assert row is not None
    return int(row[0])


def test_remote_llm_call_takes_slot_and_respects_cost_cap(
    store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One remote call holds one ``llm-api:<provider>`` unit for the whole
    call via ``admission.admit`` and frees it after; an exhausted slot
    refuses instead of running unaccounted, and the refusal lands on the
    provider row's durable cost counter."""
    prior = meter.active_store()
    meter.bind_store(store)
    # The counted-refusal write rides rate_limit's own connection, not the
    # bound store — point it at this test clone for the refusal half.
    monkeypatch.setenv("PRECIS_DATABASE_URL", store.dsn)
    outbound_policy.reset_cache()
    limit_row = "host:openrouter.ai"
    try:
        _seed_api_slot(store, 1)
        with openai_tools._llm_api_hold(OPENROUTER_URL, 5.0):
            assert _api_slot_free(store) == 0
            # The admission ledger records the TTL hold while the call runs.
            assert _holds(store, "api") == [(_LLM_RESOURCE, 1)]
        assert _api_slot_free(store) == 1
        assert _holds(store, "api") == []

        # Exhausted: capacity 0 and a zero wait budget → a counted refusal.
        _seed_api_slot(store, 0)
        with store.pool.connection() as conn:
            conn.execute(
                "DELETE FROM external_rate_limits WHERE provider = %s", (limit_row,)
            )
            conn.execute(
                "INSERT INTO external_rate_limits "
                "(provider, capacity, refill_per_sec, tokens, daily_cap, max_wait_s) "
                "VALUES (%s, 1, 1.0, 1, NULL, 0)",
                (limit_row,),
            )
            conn.commit()
        outbound_policy.reset_cache()
        with pytest.raises(rate_limit.RateLimited):
            with openai_tools._llm_api_hold(OPENROUTER_URL, 5.0):
                pass  # never entered — the hold refuses
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT refused_today FROM external_rate_limits WHERE provider = %s",
                (limit_row,),
            ).fetchone()
        assert row is not None and int(row[0]) >= 1
    finally:
        rate_limit.close()
        with store.pool.connection() as conn:
            conn.execute(
                "DELETE FROM external_rate_limits WHERE provider = %s", (limit_row,)
            )
            conn.commit()
        meter.bind_store(prior)
        outbound_policy.reset_cache()


# ── per-site footprints: heavy launch sites admit their work class ─────────


def _spy_admit(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Footprint]]:
    """Wrap ``admission.admit`` (calling through) and record
    ``(host, footprint)`` per call."""

    seen: list[tuple[str, Footprint]] = []
    real = admission.admit

    def spy(conn: Any, host: str, footprint: Footprint, holder: str, **kw: Any):
        seen.append((host, footprint))
        return real(conn, host, footprint, holder, **kw)

    monkeypatch.setattr(admission, "admit", spy)
    return seen


def _seed_host(store, host: str, monkeypatch: pytest.MonkeyPatch) -> None:
    store.sync_host_resource_slots(host, {"mem_mb": 32768, "cpu_milli": 16000})
    monkeypatch.setenv("PRECIS_HOST_NAME", host)


@pytest.fixture
def bound(store) -> Iterator[object]:
    """Bind the test store for sites that read ``meter.active_store()``."""
    prior = meter.active_store()
    meter.bind_store(store)
    yield prior
    meter.bind_store(prior)


def test_dep_check_step_admits_its_footprint(
    store, monkeypatch: pytest.MonkeyPatch, bound
) -> None:
    from precis.workers.job_types import dep_check

    host = "dc1"
    _seed_host(store, host, monkeypatch)
    seen = _spy_admit(monkeypatch)

    res = dep_check._run(["/bin/true"], Path("/tmp"))
    assert res.returncode == 0
    ((h, fp),) = seen
    assert h == host
    assert fp.cpu_milli == 2000 and fp.mem_mb == 4096
    assert fp.wall_s == float(dep_check._STEP_TIMEOUT_S)
    assert _holds(store, host) == []  # released at block exit


def test_boundary_probe_admits_its_footprint(
    store, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from precis.workers.job_types import boundary_probe

    host = "bp1"
    _seed_host(store, host, monkeypatch)
    seen = _spy_admit(monkeypatch)
    monkeypatch.setattr(
        boundary_probe.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            a[0] if a else k.get("args"), 0, "ok", ""
        ),
    )

    rc, _out = boundary_probe._run_probe(
        {"file": "x.py"}, tmp_path, "img", name="n", tag="t", store=store
    )
    assert rc == 0
    ((h, fp),) = seen
    assert h == host
    assert fp.cpu_milli == 2000 and fp.mem_mb == 4096
    assert fp.wall_s == float(boundary_probe._PROBE_TIMEOUT_S)
    assert _holds(store, host) == []


def test_moot_probe_admits_its_footprint(
    store, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from precis.workers.job_types import run_slice

    host = "mt1"
    _seed_host(store, host, monkeypatch)
    seen = _spy_admit(monkeypatch)
    monkeypatch.setattr(
        run_slice,
        "_git",
        lambda argv, cwd: subprocess.CompletedProcess(argv, 0, "", ""),
    )
    holds_during: list[list[tuple[str, int]]] = []

    def fake_run(argv: list[str], cwd: Path, timeout: float):
        holds_during.append(_holds(store, host))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(run_slice, "_run", fake_run)

    assert run_slice._tests_green_on_main(tmp_path, ["tests/x.py"], store=store) is True
    ((h, fp),) = seen
    assert h == host
    assert fp.cpu_milli == 4000 and fp.mem_mb == 8192
    assert fp.wall_s == run_slice._MOOT_TEST_TIMEOUT_S
    assert holds_during == [[("cpu_milli", 4000), ("mem_mb", 8192)]]
    assert _holds(store, host) == []


def _exit0_stub(tmp_path: Path) -> str:
    stub = tmp_path / "exit0"
    stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    stub.chmod(0o755)
    return str(stub)


def test_codex_exec_admits_its_footprint(
    store, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bound
) -> None:
    from precis.utils import codex_exec

    host = "cx1"
    _seed_host(store, host, monkeypatch)
    seen = _spy_admit(monkeypatch)

    res = codex_exec._run_codex(
        [_exit0_stub(tmp_path)],
        binary="codex",
        timeout_s=30.0,
        env=dict(os.environ),
        cwd=str(tmp_path),
    )
    assert res.returncode == 0
    ((h, fp),) = seen
    assert h == host
    assert fp.cpu_milli == 1000 and fp.mem_mb == 2048 and fp.wall_s == 30.0
    assert _holds(store, host) == []


def test_codex_exec_refusal_maps_to_its_error_cls(
    store, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bound
) -> None:
    """A full host maps to CodexExecError — the lane's ordinary retry class —
    never an unaccounted run; the capacity refusal costs no attempt."""
    from precis.utils import codex_exec

    host = "cx2"
    store.sync_host_resource_slots(host, {"mem_mb": 1024, "cpu_milli": 16000})
    monkeypatch.setenv("PRECIS_HOST_NAME", host)

    with pytest.raises(codex_exec.CodexExecError, match="capacity"):
        codex_exec._run_codex(
            [_exit0_stub(tmp_path)],
            binary="codex",
            timeout_s=30.0,
            env=dict(os.environ),
            cwd=str(tmp_path),
        )
    assert _holds(store, host) == []


def test_claude_subprocess_admits_its_footprint(
    store, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bound
) -> None:
    from precis.utils import _claude_subprocess as cs

    host = "cl1"
    _seed_host(store, host, monkeypatch)
    seen = _spy_admit(monkeypatch)

    res = cs.run_claude(
        [_exit0_stub(tmp_path)],
        binary="claude",
        label="claude -p (test)",
        timeout_s=30.0,
        error_cls=cs.ClaudeProcessError,
        env={"PATH": os.environ.get("PATH", "")},
        bootstrap_oauth=False,
    )
    assert res.returncode == 0
    ((h, fp),) = seen
    assert h == host
    assert fp.cpu_milli == 1000 and fp.mem_mb == 2048 and fp.wall_s == 30.0
    assert _holds(store, host) == []


def test_claude_subprocess_refusal_maps_to_its_error_cls(
    store, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bound
) -> None:
    from precis.utils import _claude_subprocess as cs

    host = "cl2"
    store.sync_host_resource_slots(host, {"mem_mb": 1024, "cpu_milli": 16000})
    monkeypatch.setenv("PRECIS_HOST_NAME", host)

    with pytest.raises(cs.ClaudeProcessError, match="capacity"):
        cs.run_claude(
            [_exit0_stub(tmp_path)],
            binary="claude",
            label="claude -p (test)",
            timeout_s=30.0,
            error_cls=cs.ClaudeProcessError,
            env={"PATH": os.environ.get("PATH", "")},
            bootstrap_oauth=False,
        )
    assert _holds(store, host) == []
