"""Unschedulable-job detection (slice 6d).

A queued job that requires a capability no host advertises, with no
``target_node`` pin to fall back on, can never be placed — the sweeper
raises a ``warn`` alert so the gap is visible. Pinned jobs (which still run
via the node gate) and jobs whose capability IS advertised are not flagged.

gr333205: an advertisement only counts as live where its host's
``host_heartbeat`` is fresh — a retired daemon's frozen ``resource_slots``
row (the retract-on-absent discipline only runs inside a live heartbeat
pass) must not silently suppress the alert forever.
"""

from __future__ import annotations

from precis.store import Store
from precis.store.types import Tag
from precis.workers.sweeper import _alert_unschedulable_jobs


def _queue(store: Store, meta: dict[str, object]) -> int:
    ref = store.insert_ref(kind="job", slug=None, title="j", meta=meta)
    store.add_tag(
        ref.id, Tag.closed("STATUS", "queued"), set_by="agent", replace_prefix=True
    )
    return ref.id


def _upsert_host_heartbeat(store: Store, host: str, *, age_minutes: float) -> None:
    """Seed/refresh a ``host_heartbeat`` row at a given age — the real
    heartbeat probe writes ``resource_slots`` and ``host_heartbeat``
    together each pass, so any advertisement in these tests should carry a
    matching heartbeat row (fresh, unless the test is deliberately
    simulating a fossil)."""
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO host_heartbeat (host, ts) "
            "VALUES (%s, now() - %s::interval) "
            "ON CONFLICT (host) DO UPDATE SET ts = EXCLUDED.ts",
            (host, f"{age_minutes} minutes"),
        )


def _alert_open(store: Store, fingerprint: str) -> bool:
    """True iff an unresolved alert with this fingerprint exists."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM refs WHERE kind = 'alert' AND retired_at IS NULL "
            "AND resolved_at IS NULL AND meta->>'fingerprint' = %s LIMIT 1",
            (fingerprint,),
        ).fetchone()
    return row is not None


def test_unpinned_unadvertised_job_is_flagged(store: Store) -> None:
    jid = _queue(
        store,
        {"job_type": "demo", "executor": "x", "requires": {"gpu": 1}, "params": {}},
    )
    _alert_unschedulable_jobs(store)
    assert _alert_open(store, f"unschedulable:{jid}") is True


def test_pinned_job_not_flagged(store: Store) -> None:
    """A target_node pin means the node gate still runs it — not stuck."""
    jid = _queue(
        store,
        {
            "job_type": "struct_relax",
            "executor": "x",
            "params": {"target_node": "spark"},
        },
    )
    _alert_unschedulable_jobs(store)
    assert _alert_open(store, f"unschedulable:{jid}") is False


def test_advertised_capability_not_flagged(store: Store) -> None:
    store.sync_host_resource_slots("some_host", {"gpu": 1})
    _upsert_host_heartbeat(store, "some_host", age_minutes=0.5)
    jid = _queue(
        store,
        {"job_type": "demo", "executor": "x", "requires": {"gpu": 1}, "params": {}},
    )
    _alert_unschedulable_jobs(store)
    assert _alert_open(store, f"unschedulable:{jid}") is False


def test_fossil_advertisement_does_not_suppress_alert(store: Store) -> None:
    """gr333205: a retired daemon's frozen ``resource_slots`` row (heartbeat
    weeks stale) must not read as a live advertisement — the retract-on-
    absent discipline never runs again once the daemon is gone, so without
    a staleness filter this fossil masks the capability gap forever."""
    store.sync_host_resource_slots("retired_host", {"gpu": 1})
    _upsert_host_heartbeat(store, "retired_host", age_minutes=60 * 24 * 21)  # 3 weeks
    jid = _queue(
        store,
        {"job_type": "demo", "executor": "x", "requires": {"gpu": 1}, "params": {}},
    )
    _alert_unschedulable_jobs(store)
    assert _alert_open(store, f"unschedulable:{jid}") is True


def test_fresh_heartbeat_host_still_advertises(store: Store) -> None:
    """A host with a fresh heartbeat still counts as advertising — the
    staleness filter must not false-positive a live, quiet host."""
    store.sync_host_resource_slots("live_host", {"gpu": 1})
    _upsert_host_heartbeat(store, "live_host", age_minutes=1.0)
    jid = _queue(
        store,
        {"job_type": "demo", "executor": "x", "requires": {"gpu": 1}, "params": {}},
    )
    _alert_unschedulable_jobs(store)
    assert _alert_open(store, f"unschedulable:{jid}") is False


def test_no_requires_not_flagged(store: Store) -> None:
    jid = _queue(store, {"job_type": "demo", "executor": "x", "params": {}})
    _alert_unschedulable_jobs(store)
    assert _alert_open(store, f"unschedulable:{jid}") is False


def test_alert_resolves_once_capability_is_advertised(store: Store) -> None:
    """gr254322: a later-advertised capability closes the stale alert."""
    jid = _queue(
        store,
        {"job_type": "demo", "executor": "x", "requires": {"gpu": 1}, "params": {}},
    )
    _alert_unschedulable_jobs(store)
    assert _alert_open(store, f"unschedulable:{jid}") is True

    store.sync_host_resource_slots("some_host", {"gpu": 1})
    _upsert_host_heartbeat(store, "some_host", age_minutes=0.5)
    _alert_unschedulable_jobs(store)
    assert _alert_open(store, f"unschedulable:{jid}") is False


def test_alert_resolves_once_job_leaves_queued(store: Store) -> None:
    """gr254322: the job moving out of STATUS:queued also closes the alert."""
    jid = _queue(
        store,
        {"job_type": "demo", "executor": "x", "requires": {"gpu": 1}, "params": {}},
    )
    _alert_unschedulable_jobs(store)
    assert _alert_open(store, f"unschedulable:{jid}") is True

    store.add_tag(
        jid, Tag.closed("STATUS", "cancelled"), set_by="agent", replace_prefix=True
    )
    _alert_unschedulable_jobs(store)
    assert _alert_open(store, f"unschedulable:{jid}") is False
