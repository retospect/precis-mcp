"""DB-backed tests for the host_heartbeat store surface.

Auto-skipped when no postgres is reachable (the ``store`` fixture
handles the skip). Migration 0017 is applied by the session-scoped
schema fixture.
"""

from __future__ import annotations

from precis.store import Store


def test_record_and_read_heartbeat(store: Store) -> None:
    store.record_heartbeat(
        "caspar",
        temp_c=61.5,
        load1=1.2,
        load5=0.9,
        load15=0.7,
        meta={"platform": "Linux"},
    )
    rows = store.recent_heartbeats()
    assert len(rows) == 1
    hb = rows[0]
    assert hb.host == "caspar"
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
    store.record_heartbeat("balthazar", temp_c=40.0, load1=0.1)
    first = _by_host(store.recent_heartbeats(), "balthazar")[0].ts
    store.record_heartbeat("balthazar", temp_c=88.0, load1=5.0)
    rows = _by_host(store.recent_heartbeats(), "balthazar")
    assert len(rows) == 1  # still one row for the host
    assert rows[0].temp_c == 88.0  # overwritten
    assert rows[0].load1 == 5.0
    assert rows[0].ts >= first  # ts bumped


def test_record_heartbeat_nullable_temp(store: Store) -> None:
    # macOS-without-sensor case: load reported, temp NULL.
    store.record_heartbeat("melchior", temp_c=None, load1=2.0, load5=1.5, load15=1.0)
    hb = _by_host(store.recent_heartbeats(), "melchior")[0]
    assert hb.temp_c is None
    assert hb.load1 == 2.0


def test_recent_heartbeats_ordered_by_host(store: Store) -> None:
    store.record_heartbeat("spark", load1=0.0)
    store.record_heartbeat("caspar", load1=0.0)
    store.record_heartbeat("balthazar", load1=0.0)
    hosts = [hb.host for hb in store.recent_heartbeats()]
    assert hosts == sorted(hosts)


# ── §H boot epoch: meta.boot_ids nested-merge (compute-lane-lease-epoch.md) ──


def test_boot_ids_merge_across_two_processes_on_one_host(store: Store) -> None:
    """A host running BOTH profiles (melchior runs system + agent) advertises
    two processes' boot_ids under the SAME host_heartbeat row (PK is host,
    not (host, process)) — a full-meta replace by one process's beat must
    not wipe the other's last-advertised generation."""
    store.record_heartbeat(
        "melchior-boot-1",
        meta={"platform": "Darwin", "boot_ids": {"precis-worker": "sys-gen-1"}},
    )
    store.record_heartbeat(
        "melchior-boot-1",
        meta={"platform": "Darwin", "boot_ids": {"precis-worker-agent": "agent-gen-1"}},
    )
    hb = _by_host(store.recent_heartbeats(), "melchior-boot-1")[0]
    assert hb.meta["boot_ids"] == {
        "precis-worker": "sys-gen-1",
        "precis-worker-agent": "agent-gen-1",
    }


def test_boot_ids_same_process_overwrites_its_own_entry(store: Store) -> None:
    """A process's OWN re-beat with a new boot_id (a restart) replaces just
    its own entry — the merge is per-process-key, not append-only."""
    store.record_heartbeat(
        "melchior-boot-2", meta={"boot_ids": {"precis-worker": "gen-1"}}
    )
    store.record_heartbeat(
        "melchior-boot-2", meta={"boot_ids": {"precis-worker": "gen-2"}}
    )
    hb = _by_host(store.recent_heartbeats(), "melchior-boot-2")[0]
    assert hb.meta["boot_ids"] == {"precis-worker": "gen-2"}


def test_boot_ids_survive_a_beat_that_omits_them(store: Store) -> None:
    """The REGULAR full-snapshot heartbeat write (platform/release/top_cpu,
    no boot_ids key at all) must not wipe a previously-advertised boot_id —
    every ``_collect_and_upsert`` call includes its OWN process's boot_ids
    entry, but a caller that never minted one (a bare ``precis heartbeat``)
    must not erase what a worker process on the same host already
    advertised."""
    store.record_heartbeat(
        "melchior-boot-3", meta={"boot_ids": {"precis-worker": "gen-1"}}
    )
    store.record_heartbeat("melchior-boot-3", meta={"platform": "Darwin"})
    hb = _by_host(store.recent_heartbeats(), "melchior-boot-3")[0]
    assert hb.meta["boot_ids"] == {"precis-worker": "gen-1"}
    assert hb.meta["platform"] == "Darwin"
