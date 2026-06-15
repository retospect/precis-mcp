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


def test_record_heartbeat_upserts(store: Store) -> None:
    store.record_heartbeat("balthazar", temp_c=40.0, load1=0.1)
    first = store.recent_heartbeats()[0].ts
    store.record_heartbeat("balthazar", temp_c=88.0, load1=5.0)
    rows = store.recent_heartbeats()
    assert len(rows) == 1  # still one row for the host
    assert rows[0].temp_c == 88.0  # overwritten
    assert rows[0].load1 == 5.0
    assert rows[0].ts >= first  # ts bumped


def test_record_heartbeat_nullable_temp(store: Store) -> None:
    # macOS-without-sensor case: load reported, temp NULL.
    store.record_heartbeat("melchior", temp_c=None, load1=2.0, load5=1.5, load15=1.0)
    hb = store.recent_heartbeats()[0]
    assert hb.temp_c is None
    assert hb.load1 == 2.0


def test_recent_heartbeats_ordered_by_host(store: Store) -> None:
    store.record_heartbeat("spark", load1=0.0)
    store.record_heartbeat("caspar", load1=0.0)
    store.record_heartbeat("balthazar", load1=0.0)
    hosts = [hb.host for hb in store.recent_heartbeats()]
    assert hosts == sorted(hosts)
