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

import pytest

from precis.store import Store
from precis.store.types import Tag
from precis.workers.sweeper import _alert_unschedulable_jobs


def _queue(store: Store, meta: dict[str, object], parent_id: int | None = None) -> int:
    ref = store.insert_ref(
        kind="job", slug=None, title="j", meta=meta, parent_id=parent_id
    )
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
    """The fold-in is scoped to lanes that pass
    ``check_job_type_requires=True`` (``coordinator``, ``job_inproc``) — a
    job_type that happens to declare capability tokens but runs under a
    DIFFERENT executor (already protected by its own mechanism — e.g.
    ``claude_inproc`` is gated by static pass registration, not this
    alert) must not gain a NEW alert surface it never had before this
    change."""
    jid = _queue(
        store,
        {"job_type": "quest_tick", "executor": "claude_inproc", "params": {}},
    )
    _alert_unschedulable_jobs(store)
    assert _alert_open(store, f"unschedulable:{jid}") is False


def test_job_inproc_upload_pull_flagged_when_staging_unadvertised(
    store: Store,
) -> None:
    """gr537770: a ``paper_pull`` upload leg whose ``upload_staging``
    token no host advertises is unschedulable too — the params-
    conditional fold reaches it via ``job_type_requires``."""
    jid = _queue(
        store,
        {
            "job_type": "paper_pull",
            "executor": "job_inproc",
            "params": {"ref_id": 1, "upload_sha": "ab" * 32},
        },
    )
    _alert_unschedulable_jobs(store)
    assert _alert_open(store, f"unschedulable:{jid}") is True


def test_job_inproc_url_pull_not_flagged(store: Store) -> None:
    """``conditional_requires`` keys on ``upload_sha`` — the ``url`` leg
    declares nothing and earns no alert."""
    jid = _queue(
        store,
        {
            "job_type": "paper_pull",
            "executor": "job_inproc",
            "params": {"ref_id": 1, "url": "https://x/y.pdf"},
        },
    )
    _alert_unschedulable_jobs(store)
    assert _alert_open(store, f"unschedulable:{jid}") is False


def _queue_aged_job(
    store: Store,
    *,
    job_type: str,
    origin: str | None,
    prio: int | None,
    queued_min_ago: float,
) -> int:
    """A claimable ``job_inproc`` job whose STATUS tag stamp is aged —
    the queue clock the claim order reads (``ref_tags.created_at``)."""
    meta: dict[str, object] = {
        "job_type": job_type,
        "executor": "job_inproc",
        "params": {},
    }
    if origin is not None:
        meta["origin"] = origin
    ref = store.insert_ref(kind="job", slug=None, title="j", meta=meta, prio=prio)
    store.add_tag(
        ref.id, Tag.closed("STATUS", "queued"), set_by="agent", replace_prefix=True
    )
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE ref_tags SET created_at = now() - "
            "make_interval(mins => %s) WHERE ref_id = %s",
            (queued_min_ago, int(ref.id)),
        )
        conn.commit()
    return int(ref.id)


def test_starved_control_alert_fires_on_aged_recurring(store: Store) -> None:
    """gr592211: a recurring job_inproc job queued >15m fires the
    starvation alert; background bulk work at the same age does not."""
    from precis.workers.sweeper import _alert_starved_control_jobs

    poll = _queue_aged_job(
        store,
        job_type="release_poll",
        origin="recurring",
        prio=2,
        queued_min_ago=20,
    )
    _queue_aged_job(
        store,
        job_type="derived_drain",
        origin="background",
        prio=8,
        queued_min_ago=60,
    )
    _queue_aged_job(
        store,
        job_type="release_poll",
        origin="recurring",
        prio=2,
        queued_min_ago=2,
    )
    assert _alert_starved_control_jobs(store) == 1
    assert _alert_open(store, "control:job_inproc") is True
    with store.pool.connection() as conn:
        detail = conn.execute(
            "SELECT title FROM refs WHERE kind = 'alert' "
            "AND resolved_at IS NULL "
            "AND meta->>'fingerprint' = 'control:job_inproc'",
        ).fetchone()
    assert detail is not None and f"#{poll}" in str(detail[0])


def test_starved_control_alert_self_resolves(store: Store) -> None:
    """The alert closes itself once no control job waits past the bound
    — a drained queue leaves no open alert."""
    from precis.workers.sweeper import _alert_starved_control_jobs

    _queue_aged_job(
        store,
        job_type="release_poll",
        origin="recurring",
        prio=2,
        queued_min_ago=20,
    )
    _alert_starved_control_jobs(store)
    assert _alert_open(store, "control:job_inproc") is True
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE ref_tags SET created_at = now() "
            "WHERE ref_id IN (SELECT ref_id FROM ref_tags)"
        )
        conn.commit()
    assert _alert_starved_control_jobs(store) == 0
    assert _alert_open(store, "control:job_inproc") is False


def test_starved_control_alert_not_hidden_by_bulk_backlog(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """td593845 review: the scan cap used to bound the scan BEFORE the
    control filter — a bulk backlog deeper than the cap pushed every
    overdue control row out of the scanned window, so the alert never
    fired. The cap now bounds control-class rows only."""
    import precis.workers.sweeper as sweeper_mod
    from precis.workers.sweeper import _alert_starved_control_jobs

    monkeypatch.setattr(sweeper_mod, "_UNSCHEDULABLE_SCAN_CAP", 3)
    for _ in range(5):
        _queue_aged_job(
            store,
            job_type="derived_drain",
            origin="background",
            prio=8,
            queued_min_ago=60,
        )
    _queue_aged_job(
        store,
        job_type="release_poll",
        origin="recurring",
        prio=2,
        queued_min_ago=20,
    )
    assert _alert_starved_control_jobs(store) == 1
    assert _alert_open(store, "control:job_inproc") is True


# ── parent-pause alert (gr575883) ────────────────────────────────────────
#
# A queued job on a parent-gated executor (ssh_node / claude_docker /
# devin_api) whose parent todo carries an open pause tag can never be
# claimed — the alert names the blocking tags, the operator-visible twin
# of the claim gate's parent exclusion.


def _paused_parent(store: Store, *tags: str, status: str | None = None) -> int:
    parent = store.insert_ref(kind="todo", slug=None, title="p", meta={})
    if status is not None:
        store.add_tag(
            parent.id,
            Tag.closed("STATUS", status),
            set_by="agent",
            replace_prefix=True,
        )
    for tag in tags:
        store.add_tag(parent.id, Tag.open(tag), set_by="system")
    return parent.id


def test_parent_paused_job_is_flagged(store: Store) -> None:
    parent = _paused_parent(store, "waiting-for:fleet")
    jid = _queue(
        store, {"job_type": "demo", "executor": "ssh_node", "params": {}}, parent
    )
    assert _alert_unschedulable_jobs(store) == 1
    assert _alert_open(store, f"parent-paused:{jid}") is True


def test_terminal_parent_not_flagged(store: Store) -> None:
    """The alert reads the SAME predicate as the claim gate: a done
    parent's stale ``waiting-for:`` tag neither blocks the claim nor
    flags an alert."""
    parent = _paused_parent(store, "waiting-for:fleet", status="done")
    jid = _queue(
        store, {"job_type": "demo", "executor": "ssh_node", "params": {}}, parent
    )
    assert _alert_unschedulable_jobs(store) == 0
    assert _alert_open(store, f"parent-paused:{jid}") is False


def test_unparent_gated_executor_not_flagged(store: Store) -> None:
    """``coordinator`` never passes ``parent_not_paused`` — the claim
    gate doesn't bind its jobs, so no parent-pause alert (the unschedulable
    check below stays the only scheduler surface for that lane)."""
    parent = _paused_parent(store, "halt:manual")
    jid = _queue(
        store, {"job_type": "demo", "executor": "coordinator", "params": {}}, parent
    )
    assert _alert_unschedulable_jobs(store) == 0
    assert _alert_open(store, f"parent-paused:{jid}") is False
