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


# ── coordinator job-type capability (gr335087) ────────────────────────────
#
# ``quest_tick``'s ``claude_bin`` requirement is enforced at CLAIM time via
# the job_type's own declared ``JobTypeSpec.requires``
# (``workers/executors/_common.py``'s ``job_type_requires`` /
# ``_coordinator_capability_ok``), NOT ``effective_requires``'s
# ``ServiceSpec.requires``/resource_slots reservation (unsafe for a
# Yield-heavy coordinator job — see that function's docstring) — so this
# alert must fold ``job_type_requires`` in for a ``coordinator``-executed row
# too, or a fleet where literally no host ever advertises ``claude_bin``
# would starve ``quest_tick`` silently instead of paging.


def test_coordinator_job_type_requires_flagged_when_unadvertised(store: Store) -> None:
    jid = _queue(
        store,
        {"job_type": "quest_tick", "executor": "coordinator", "params": {}},
    )
    _alert_unschedulable_jobs(store)
    assert _alert_open(store, f"unschedulable:{jid}") is True


def test_coordinator_job_type_requires_not_flagged_when_advertised(
    store: Store,
) -> None:
    store.sync_host_resource_slots("melchior", {"claude_bin": 1})
    _upsert_host_heartbeat(store, "melchior", age_minutes=0.5)
    jid = _queue(
        store,
        {"job_type": "quest_tick", "executor": "coordinator", "params": {}},
    )
    _alert_unschedulable_jobs(store)
    assert _alert_open(store, f"unschedulable:{jid}") is False


def test_non_coordinator_executor_does_not_pick_up_job_type_requires(
    store: Store,
) -> None:
    """The fold-in is scoped to ``executor == 'coordinator'`` — a job_type
    that happens to declare capability tokens but runs under a DIFFERENT
    executor (already protected by its own mechanism — e.g. ``claude_inproc``
    is gated by static pass registration, not this alert) must not gain a
    NEW alert surface it never had before this change."""
    jid = _queue(
        store,
        {"job_type": "quest_tick", "executor": "claude_inproc", "params": {}},
    )
    _alert_unschedulable_jobs(store)
    assert _alert_open(store, f"unschedulable:{jid}") is False
