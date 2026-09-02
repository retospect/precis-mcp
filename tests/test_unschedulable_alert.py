"""Unschedulable-job detection (slice 6d).

A queued job that requires a capability no host advertises, with no
``target_node`` pin to fall back on, can never be placed — the sweeper
raises a ``warn`` alert so the gap is visible. Pinned jobs (which still run
via the node gate) and jobs whose capability IS advertised are not flagged.
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
