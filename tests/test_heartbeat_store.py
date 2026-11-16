"""DB-backed tests for the host_heartbeat store surface.

Auto-skipped when no postgres is reachable (the ``store`` fixture
handles the skip). Migration 0017 is applied by the session-scoped
schema fixture.
"""

from __future__ import annotations

import pytest

from precis.store import Store


def test_record_and_read_heartbeat(store: Store) -> None:
    store.record_heartbeat(
        "birch",
        temp_c=61.5,
        load1=1.2,
        load5=0.9,
        load15=0.7,
        meta={"platform": "Linux"},
    )
    rows = store.recent_heartbeats()
    assert len(rows) == 1
    hb = rows[0]
    assert hb.host == "birch"
    assert hb.temp_c == 61.5
    assert hb.load1 == 1.2
    assert hb.meta == {"platform": "Linux"}
    assert hb.ts is not None


def _by_host(rows, host: str):
    """Filter recent_heartbeats() output to one host.

    The store fixture's truncate-isolation doesn't always clear
    ``host_heartbeat`` between tests (it's outside the refs/tags
    family the canonical truncate sweeps), so each per-host test
    scopes its assertions to its own host.
    """
    return [r for r in rows if r.host == host]


def test_record_heartbeat_upserts(store: Store) -> None:
    store.record_heartbeat("alder", temp_c=40.0, load1=0.1)
    first = _by_host(store.recent_heartbeats(), "alder")[0].ts
    store.record_heartbeat("alder", temp_c=88.0, load1=5.0)
    rows = _by_host(store.recent_heartbeats(), "alder")
    assert len(rows) == 1  # still one row for the host
    assert rows[0].temp_c == 88.0  # overwritten
    assert rows[0].load1 == 5.0
    assert rows[0].ts >= first  # ts bumped


def test_record_heartbeat_nullable_temp(store: Store) -> None:
    # macOS-without-sensor case: load reported, temp NULL.
    store.record_heartbeat("maple", temp_c=None, load1=2.0, load5=1.5, load15=1.0)
    hb = _by_host(store.recent_heartbeats(), "maple")[0]
    assert hb.temp_c is None
    assert hb.load1 == 2.0


def test_recent_heartbeats_ordered_by_host(store: Store) -> None:
    store.record_heartbeat("spruce", load1=0.0)
    store.record_heartbeat("birch", load1=0.0)
    store.record_heartbeat("alder", load1=0.0)
    hosts = [hb.host for hb in store.recent_heartbeats()]
    assert hosts == sorted(hosts)


# ── Worker boot epoch: meta.boot_ids nested-merge ──────────────────────────


def test_boot_ids_merge_across_two_processes_on_one_host(store: Store) -> None:
    """A host running BOTH profiles (maple runs system + agent) advertises
    two processes' boot_ids under the SAME host_heartbeat row (PK is host,
    not (host, process)) — a full-meta replace by one process's beat must
    not wipe the other's last-advertised generation."""
    store.record_heartbeat(
        "maple-boot-1",
        meta={"platform": "Darwin", "boot_ids": {"precis-worker": "sys-gen-1"}},
    )
    store.record_heartbeat(
        "maple-boot-1",
        meta={"platform": "Darwin", "boot_ids": {"precis-worker-agent": "agent-gen-1"}},
    )
    hb = _by_host(store.recent_heartbeats(), "maple-boot-1")[0]
    assert hb.meta["boot_ids"] == {
        "precis-worker": "sys-gen-1",
        "precis-worker-agent": "agent-gen-1",
    }


def test_boot_ids_same_process_overwrites_its_own_entry(store: Store) -> None:
    """A process's OWN re-beat with a new boot_id (a restart) replaces just
    its own entry — the merge is per-process-key, not append-only."""
    store.record_heartbeat(
        "maple-boot-2", meta={"boot_ids": {"precis-worker": "gen-1"}}
    )
    store.record_heartbeat(
        "maple-boot-2", meta={"boot_ids": {"precis-worker": "gen-2"}}
    )
    hb = _by_host(store.recent_heartbeats(), "maple-boot-2")[0]
    assert hb.meta["boot_ids"] == {"precis-worker": "gen-2"}


def test_boot_ids_survive_a_beat_that_omits_them(store: Store) -> None:
    """The REGULAR full-snapshot heartbeat write (platform/release/top_cpu,
    no boot_ids key at all) must not wipe a previously-advertised boot_id —
    every ``_collect_and_upsert`` call includes its OWN process's boot_ids
    entry, but a caller that never minted one (a bare ``precis heartbeat``)
    must not erase what a worker process on the same host already
    advertised."""
    store.record_heartbeat(
        "maple-boot-3", meta={"boot_ids": {"precis-worker": "gen-1"}}
    )
    store.record_heartbeat("maple-boot-3", meta={"platform": "Darwin"})
    hb = _by_host(store.recent_heartbeats(), "maple-boot-3")[0]
    assert hb.meta["boot_ids"] == {"precis-worker": "gen-1"}
    assert hb.meta["platform"] == "Darwin"


# ── Per-process boot version: meta.code_sha_by_process nested-merge ──────


def test_code_sha_by_process_merges_across_processes_on_one_host(
    store: Store,
) -> None:
    """td649547: each process's own ``{"sha","at"}`` advertisement
    survives the other process's beat — the mixed-version host the
    host-wide, last-writer ``meta.code_sha`` would hide."""
    store.record_heartbeat(
        "maple-sha-1",
        meta={
            "code_sha": "a" * 40,
            "code_sha_by_process": {
                "precis-worker": {"sha": "a" * 40, "at": "t1"},
            },
        },
    )
    store.record_heartbeat(
        "maple-sha-1",
        meta={
            "code_sha": "b" * 40,
            "code_sha_by_process": {
                "precis-worker-agent": {"sha": "b" * 40, "at": "t2"},
            },
        },
    )
    hb = _by_host(store.recent_heartbeats(), "maple-sha-1")[0]
    assert hb.meta["code_sha"] == "b" * 40  # last writer wins, host-wide
    assert hb.meta["code_sha_by_process"] == {
        "precis-worker": {"sha": "a" * 40, "at": "t1"},
        "precis-worker-agent": {"sha": "b" * 40, "at": "t2"},
    }


def test_code_sha_by_process_survives_a_beat_that_omits_it(
    store: Store,
) -> None:
    """A beat carrying no ``code_sha_by_process`` key (a bare CLI, a
    nameless process) must not erase what a named process advertised —
    same merge contract as boot_ids."""
    store.record_heartbeat(
        "maple-sha-2",
        meta={"code_sha_by_process": {"precis-worker": {"sha": "c" * 40, "at": "t1"}}},
    )
    store.record_heartbeat("maple-sha-2", meta={"platform": "Darwin"})
    hb = _by_host(store.recent_heartbeats(), "maple-sha-2")[0]
    assert hb.meta["code_sha_by_process"] == {
        "precis-worker": {"sha": "c" * 40, "at": "t1"}
    }


# ── host_heartbeat_log time-series (migration 0113) ──────────────────────


def _history_for(store: Store, host: str, hours: float = 24.0):
    return [r for r in store.heartbeat_history(hours=hours) if r["host"] == host]


def test_record_heartbeat_history_appends(store: Store) -> None:
    store.record_heartbeat_history("birch-hist", temp_c=50.0, load1=1.0)
    store.record_heartbeat_history("birch-hist", temp_c=60.0, load1=3.0)
    rows = _history_for(store, "birch-hist")
    # Both beats land in the same hour bucket, so one rollup row.
    assert len(rows) == 1
    r = rows[0]
    assert r["beats"] == 2
    assert r["load1_avg"] == 2.0
    assert r["load1_max"] == 3.0
    assert r["temp_max"] == 60.0


def test_record_heartbeat_history_prunes_old_rows(store: Store) -> None:
    # Plant a row well outside the retention window, then beat once —
    # the write's own prune must sweep it.
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO host_heartbeat_log (host, ts, load1) "
            "VALUES ('spruce-hist', now() - interval '30 days', 9.9)"
        )
        conn.commit()
    store.record_heartbeat_history("spruce-hist", load1=1.0, retention_days=14.0)
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM host_heartbeat_log WHERE host = 'spruce-hist'"
        ).fetchone()
    assert row is not None
    assert row[0] == 1  # the fresh beat; the 30-day-old row is gone


def test_record_heartbeat_history_disabled_by_nonpositive_retention(
    store: Store,
) -> None:
    store.record_heartbeat_history("alder-hist", load1=1.0, retention_days=0)
    assert _history_for(store, "alder-hist") == []


def test_heartbeat_history_nullable_sensors(store: Store) -> None:
    store.record_heartbeat_history("maple-hist", temp_c=None, load1=None)
    rows = _history_for(store, "maple-hist")
    assert len(rows) == 1
    assert rows[0]["load1_avg"] is None
    assert rows[0]["temp_max"] is None


def test_heartbeat_history_serving_columns(store: Store) -> None:
    """llm-lane-scheduler S0: the serving-pressure columns land verbatim
    (and stay NULL when not collected — a non-serving host)."""
    store.record_heartbeat_history(
        "birch-serving",
        serving_running=4.0,
        serving_queued=1.0,
        serving_kv_usage=0.75,
        serving_retractions_total=2.0,
        swap_in_pages_total=100.0,
    )
    store.record_heartbeat_history("birch-quiet", load1=0.5)
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT serving_running, serving_queued, serving_kv_usage, "
            "serving_retractions_total, swap_in_pages_total "
            "FROM host_heartbeat_log WHERE host = 'birch-serving'"
        ).fetchone()
        quiet = conn.execute(
            "SELECT serving_running, swap_in_pages_total "
            "FROM host_heartbeat_log WHERE host = 'birch-quiet'"
        ).fetchone()
    assert row == (4.0, 1.0, 0.75, 2.0, 100.0)
    assert quiet == (None, None)


def test_heartbeat_history_mem_column_and_rollup(store: Store) -> None:
    """gr592932: the memory-utilisation sample lands verbatim and rolls
    up as mem_avg / mem_max; unmeasured beats stay NULL."""
    store.record_heartbeat_history("birch-mem", mem_used_pct=40.0)
    store.record_heartbeat_history("birch-mem", mem_used_pct=80.0)
    store.record_heartbeat_history("birch-nomem", load1=0.5)
    with store.pool.connection() as conn:
        vals = conn.execute(
            "SELECT mem_used_pct FROM host_heartbeat_log "
            "WHERE host = 'birch-mem' ORDER BY mem_used_pct"
        ).fetchall()
        nomem = conn.execute(
            "SELECT mem_used_pct FROM host_heartbeat_log WHERE host = 'birch-nomem'"
        ).fetchone()
    assert [r[0] for r in vals] == [40.0, 80.0]
    assert nomem == (None,)
    rows = _history_for(store, "birch-mem")
    assert len(rows) == 1
    assert rows[0]["mem_avg"] == 60.0
    assert rows[0]["mem_max"] == 80.0


# ── gr592809: CPU/GPU utilisation columns + heartbeat_means window ────────


def test_heartbeat_history_cpu_gpu_columns(store: Store) -> None:
    """gr592809: the utilisation samples land verbatim and stay NULL when
    not collected (a beat that sampled nothing)."""
    store.record_heartbeat_history("birch-util", cpu_used_pct=12.5, gpu_util_pct=33.0)
    store.record_heartbeat_history("birch-noutil", load1=0.5)
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT cpu_used_pct, gpu_util_pct FROM host_heartbeat_log "
            "WHERE host = 'birch-util'"
        ).fetchone()
        noutil = conn.execute(
            "SELECT cpu_used_pct, gpu_util_pct FROM host_heartbeat_log "
            "WHERE host = 'birch-noutil'"
        ).fetchone()
    assert row == (12.5, 33.0)
    assert noutil == (None, None)


def test_heartbeat_means_window(store: Store) -> None:
    """The 1h/24h means read only beats inside the window — a row older
    than the window must not drag the average up."""
    store.record_heartbeat_history(
        "birch-means", cpu_used_pct=10.0, gpu_util_pct=20.0, mem_used_pct=30.0
    )
    store.record_heartbeat_history(
        "birch-means", cpu_used_pct=30.0, gpu_util_pct=60.0, mem_used_pct=90.0
    )
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO host_heartbeat_log "
            "(host, ts, cpu_used_pct, gpu_util_pct, mem_used_pct) "
            "VALUES ('birch-means', now() - interval '48 hours', "
            "90.0, 90.0, 90.0)"
        )
        conn.commit()
    means = store.heartbeat_means("birch-means", hours=24)
    assert means == {"cpu_used_pct": 20.0, "gpu_util_pct": 40.0, "mem_used_pct": 60.0}
    # A wide enough window folds the 48 h-old row back in.
    means_all = store.heartbeat_means("birch-means", hours=72)
    assert means_all["cpu_used_pct"] == pytest.approx(130.0 / 3)
    assert means_all["gpu_util_pct"] == pytest.approx(170.0 / 3)
    assert means_all["mem_used_pct"] == pytest.approx(210.0 / 3)


def test_heartbeat_means_no_samples(store: Store) -> None:
    """A host with beats but no utilisation sample reads all-NULL, never
    an error or a fabricated 0."""
    store.record_heartbeat_history("birch-empty", load1=0.5)
    assert store.heartbeat_means("birch-empty", hours=24) == {
        "cpu_used_pct": None,
        "gpu_util_pct": None,
        "mem_used_pct": None,
    }


def test_set_host_retired_stamps_and_clears(store: Store) -> None:
    """The marker that takes a decommissioned host off nursery's host-dark
    detector. An ISO timestamp, not `true`: "when was this retired" is the
    only question anyone asks of the row later."""
    store.record_heartbeat("retiree-1", load1=1.0)
    assert store.set_host_retired("retiree-1", retired=True)
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta->>'retired' FROM host_heartbeat WHERE host = 'retiree-1'"
        ).fetchone()
    assert row is not None and row[0] is not None
    assert row[0].endswith("Z") and row[0][4] == "-"

    assert store.set_host_retired("retiree-1", retired=False)
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta ? 'retired' FROM host_heartbeat WHERE host = 'retiree-1'"
        ).fetchone()
    assert row is not None and row[0] is False


def test_set_host_retired_reports_no_such_host(store: Store) -> None:
    """False, not an exception — the CLI turns it into a message naming the
    hosts that do exist."""
    assert not store.set_host_retired("never-existed-host", retired=True)


def test_a_returning_host_clears_its_own_retired_marker(store: Store) -> None:
    """Not a bug to be fixed with a merge-protected key: a host that comes
    back is not retired, and the UPSERT's wholesale meta replace gives that
    for free. The converse — retiring a host that is still beating — is
    refused by `precis heartbeat --retire` precisely because this would
    silently undo it."""
    store.record_heartbeat("returner", load1=1.0)
    store.set_host_retired("returner", retired=True)

    store.record_heartbeat("returner", load1=2.0)
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta->>'retired' IS NULL FROM host_heartbeat WHERE host = 'returner'"
        ).fetchone()
    # Asserted via `->> IS NULL` rather than `? 'retired'`, which returns NULL
    # (not false) when the whole meta column is: a beat carrying no meta at all
    # replaces it with NULL. Either way the marker is gone, and the detector's
    # own predicate is this same `->> IS NULL`.
    assert row is not None and row[0] is True
