"""Slice-3 nursery tests — detectors, dedup, digest writer.

Five detector categories, plus the fingerprint-dedup path:

* orphans — open todos with no ``level:strategic`` ancestor
* stale-claim — ``claimed-by:*`` older than ``STALE_CLAIM_HOURS``
* long-wait — ``waiting-for:*`` older than ``LONG_WAIT_DAYS``
* stuck-doable — open leaf, no claim/wait/block, >24h old
* stalled-recurring — recurring whose last spawned child is stuck

Each test backdates ``ref_tags.created_at`` or ``refs.created_at``
via raw SQL — the handler doesn't take a time kwarg, and stubbing
``now()`` for one test would leak across the truncate-isolation
boundary. SQL backdate is the cheapest knob.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from precis.alerts import STATE_OPEN, STATE_RESOLVED, list_open_alerts, raise_alert
from precis.dispatch import Hub
from precis.handlers.todo import TodoHandler
from precis.store import Store
from precis.store.types import Tag
from precis.workers.nursery import (
    DEAD_WORKER_SILENCE_MIN,
    DISPATCH_STALL_MINUTES,
    LONG_WAIT_DAYS,
    PLAN_TICK_REMINT_24H,
    SPIN_LOOP_EVENTS_24H,
    STALE_CLAIM_HOURS,
    STUCK_DOABLE_HOURS,
    WORKER_RESTART_STORM_1H,
    _detect_dead_workers,
    _detect_dispatch_stalls,
    _detect_long_waits,
    _detect_orphans,
    _detect_plan_tick_spins,
    _detect_spin_loops,
    _detect_stale_claims,
    _detect_stalled_recurrings,
    _detect_stuck_doable,
    _detect_worker_restart_storms,
    _restart_storm_detail,
    run_nursery_pass,
)


@pytest.fixture
def handler(hub: Hub) -> TodoHandler:
    return TodoHandler(hub=hub)


def _id_of(body: str) -> int:
    return int(body.split("id=")[1].split()[0].rstrip(",.()"))


def _backdate_ref(store: Store, ref_id: int, hours: float) -> None:
    """Move ``refs.created_at`` backwards by ``hours``."""
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET created_at = now() - %s::interval WHERE ref_id = %s",
            (f"{hours} hours", ref_id),
        )
        conn.commit()


def _backdate_tag(store: Store, ref_id: int, tag_value: str, hours: float) -> None:
    """Move ``ref_tags.created_at`` backwards for one open tag."""
    with store.pool.connection() as conn:
        conn.execute(
            """
            UPDATE ref_tags rt
               SET created_at = now() - %s::interval
              FROM tags t
             WHERE rt.tag_id = t.tag_id
               AND rt.ref_id = %s
               AND t.namespace = 'OPEN'
               AND t.value = %s
            """,
            (f"{hours} hours", ref_id, tag_value),
        )
        conn.commit()


# ── orphans ────────────────────────────────────────────────────────


def test_orphans_detector_flags_open_todo_without_strategic_root(
    handler: TodoHandler, store: Store
) -> None:
    # Root todo without level:strategic — its descendants are orphans.
    root = handler.put(text="Bare root")
    root_id = _id_of(root.body)
    child = handler.put(text="Child", parent_id=root_id)
    child_id = _id_of(child.body)

    findings = _detect_orphans(store)
    ids = {f.ref_id for f in findings}
    # Both the root and its child are orphans (neither under a strategic).
    assert root_id in ids
    assert child_id in ids


def test_orphans_detector_excludes_strategic_subtree(
    handler: TodoHandler, store: Store
) -> None:
    root = handler.put(text="Real strategic", tags=["level:strategic"])
    root_id = _id_of(root.body)
    child = handler.put(text="Real child", parent_id=root_id)
    child_id = _id_of(child.body)

    findings = _detect_orphans(store)
    ids = {f.ref_id for f in findings}
    assert root_id not in ids
    assert child_id not in ids


def test_orphans_detector_excludes_done_leaves(
    handler: TodoHandler, store: Store
) -> None:
    root = handler.put(text="Bare root")
    root_id = _id_of(root.body)
    handler.tag(id=root_id, add=["STATUS:done"])

    findings = _detect_orphans(store)
    ids = {f.ref_id for f in findings}
    assert root_id not in ids


def test_orphans_detector_excludes_recurring_subtree(
    handler: TodoHandler, store: Store
) -> None:
    """Recurring scheduled work is exempt from the strategic invariant."""
    handler.put(
        text="Hourly watcher",
        tags=["level:recurring"],
        meta={"schedule": {"cron": "0 * * * *"}},
    )
    # The Watches umbrella + its child are both under the umbrella's
    # subtree. Neither should appear as an orphan.
    findings = _detect_orphans(store)
    titles = {f.title for f in findings}
    assert "Hourly watcher" not in titles


# ── stale claims ──────────────────────────────────────────────────


def test_stale_claim_detector_flags_old_claimed_by(
    handler: TodoHandler, store: Store
) -> None:
    r = handler.put(text="Long-claimed task")
    rid = _id_of(r.body)
    store.add_tag(rid, Tag.open("claimed-by:asa-worker"), set_by="agent")
    _backdate_tag(store, rid, "claimed-by:asa-worker", STALE_CLAIM_HOURS + 1)

    findings = _detect_stale_claims(store)
    ids = {f.ref_id for f in findings}
    assert rid in ids


def test_stale_claim_detector_ignores_fresh_claim(
    handler: TodoHandler, store: Store
) -> None:
    r = handler.put(text="Just-claimed task")
    rid = _id_of(r.body)
    store.add_tag(rid, Tag.open("claimed-by:asa-worker"), set_by="agent")
    # No backdate — claim is fresh.
    findings = _detect_stale_claims(store)
    ids = {f.ref_id for f in findings}
    assert rid not in ids


def test_stale_claim_detector_ignores_done_leaf(
    handler: TodoHandler, store: Store
) -> None:
    r = handler.put(text="Old + done")
    rid = _id_of(r.body)
    store.add_tag(rid, Tag.open("claimed-by:asa-worker"), set_by="agent")
    _backdate_tag(store, rid, "claimed-by:asa-worker", STALE_CLAIM_HOURS + 1)
    handler.tag(id=rid, add=["STATUS:done"])

    findings = _detect_stale_claims(store)
    ids = {f.ref_id for f in findings}
    assert rid not in ids


# ── long waits ────────────────────────────────────────────────────


def test_long_wait_detector_flags_old_waiting_for(
    handler: TodoHandler, store: Store
) -> None:
    r = handler.put(text="Waiting on owner")
    rid = _id_of(r.body)
    store.add_tag(rid, Tag.open("waiting-for:owner"), set_by="agent")
    _backdate_tag(store, rid, "waiting-for:owner", (LONG_WAIT_DAYS + 1) * 24)

    findings = _detect_long_waits(store)
    ids = {f.ref_id for f in findings}
    assert rid in ids


def test_long_wait_detector_ignores_fresh_wait(
    handler: TodoHandler, store: Store
) -> None:
    r = handler.put(text="Waiting fresh")
    rid = _id_of(r.body)
    store.add_tag(rid, Tag.open("waiting-for:something"), set_by="agent")
    findings = _detect_long_waits(store)
    ids = {f.ref_id for f in findings}
    assert rid not in ids


# ── stuck doable ──────────────────────────────────────────────────


def test_stuck_doable_detector_flags_old_open_leaf(
    handler: TodoHandler, store: Store
) -> None:
    r = handler.put(text="Old open leaf")
    rid = _id_of(r.body)
    _backdate_ref(store, rid, STUCK_DOABLE_HOURS + 1)

    findings = _detect_stuck_doable(store)
    ids = {f.ref_id for f in findings}
    assert rid in ids


def test_stuck_doable_detector_skips_claimed_leaf(
    handler: TodoHandler, store: Store
) -> None:
    r = handler.put(text="Old + claimed")
    rid = _id_of(r.body)
    store.add_tag(rid, Tag.open("claimed-by:asa-worker"), set_by="agent")
    _backdate_ref(store, rid, STUCK_DOABLE_HOURS + 1)

    findings = _detect_stuck_doable(store)
    ids = {f.ref_id for f in findings}
    assert rid not in ids


def test_stuck_doable_detector_skips_waiting_leaf(
    handler: TodoHandler, store: Store
) -> None:
    r = handler.put(text="Old + waiting")
    rid = _id_of(r.body)
    store.add_tag(rid, Tag.open("waiting-for:something"), set_by="agent")
    _backdate_ref(store, rid, STUCK_DOABLE_HOURS + 1)

    findings = _detect_stuck_doable(store)
    ids = {f.ref_id for f in findings}
    assert rid not in ids


def test_stuck_doable_detector_skips_recurring_umbrella(
    handler: TodoHandler, store: Store
) -> None:
    """The Watches root + recurring roots themselves aren't doable leaves."""
    handler.put(
        text="Watcher",
        tags=["level:recurring"],
        meta={"schedule": {"cron": "0 * * * *"}},
    )
    findings = _detect_stuck_doable(store)
    titles = {f.title for f in findings}
    assert "Watcher" not in titles
    assert "Watches" not in titles


# ── stalled recurrings ────────────────────────────────────────────


def test_stalled_recurring_detector_flags_old_open_child(
    handler: TodoHandler, store: Store
) -> None:
    rec = handler.put(
        text="Hourly",
        tags=["level:recurring"],
        meta={"schedule": {"cron": "0 * * * *"}},
    )
    rec_id = _id_of(rec.body)
    # Mint a child child manually carrying meta.spawned_for_tick + backdate.
    child = store.insert_ref(
        kind="todo",
        slug=None,
        title="Stuck tick child",
        meta={"spawned_for_tick": "2026-06-14T08:00"},
        parent_id=rec_id,
    )
    store.add_tag(child.id, Tag.closed("STATUS", "open"), set_by="system")
    _backdate_ref(store, child.id, 5)  # 5h old

    findings = _detect_stalled_recurrings(store)
    ids = {f.ref_id for f in findings}
    assert rec_id in ids


def test_stalled_recurring_detector_skips_done_child(
    handler: TodoHandler, store: Store
) -> None:
    rec = handler.put(
        text="Daily",
        tags=["level:recurring"],
        meta={"schedule": {"cron": "0 0 * * *"}},
    )
    rec_id = _id_of(rec.body)
    child = store.insert_ref(
        kind="todo",
        slug=None,
        title="Resolved tick",
        meta={"spawned_for_tick": "2026-06-13T00:00"},
        parent_id=rec_id,
    )
    store.add_tag(
        child.id,
        Tag.closed("STATUS", "done"),
        set_by="system",
        replace_prefix=True,
    )
    _backdate_ref(store, child.id, 5)

    findings = _detect_stalled_recurrings(store)
    ids = {f.ref_id for f in findings}
    assert rec_id not in ids


# ── spin loops ─────────────────────────────────────────────────────


def _seed_events(store: Store, ref_id: int, source: str, event: str, n: int) -> None:
    """Insert ``n`` recent ref_events for one ref/source via one INSERT."""
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO ref_events (ref_id, source, event, payload) "
            "SELECT %s, %s, %s, '{}'::jsonb FROM generate_series(1, %s)",
            (ref_id, source, event, n),
        )
        conn.commit()


def test_spin_loop_detector_flags_hammered_ref(store: Store) -> None:
    """A ref with > SPIN_LOOP_EVENTS_24H events from one source in 24h
    surfaces as a ``spin-loop`` finding naming the source + rate."""
    ref = store.insert_ref(kind="paper", slug="loopy", title="Loopy", meta={})
    _seed_events(store, ref.id, "fetcher:s2", "no_oa_version", SPIN_LOOP_EVENTS_24H + 5)

    findings = _detect_spin_loops(store)
    hits = [f for f in findings if f.ref_id == ref.id]
    assert len(hits) == 1
    assert hits[0].category == "spin-loop"
    assert "fetcher:s2" in hits[0].detail
    assert "no_oa_version" in hits[0].detail


def _mint_plan_ticks(store: Store, parent_id: int, n: int) -> None:
    for i in range(n):
        store.insert_ref(
            kind="job",
            slug=None,
            title=f"plan_tick {i}",
            meta={"job_type": "plan_tick"},
            parent_id=parent_id,
        )


def test_plan_tick_spin_detector_flags_reminting_parent(store: Store) -> None:
    """A planner parent minting > PLAN_TICK_REMINT_24H plan_tick jobs in 24h
    surfaces as a ``plan-tick-spin`` finding."""
    parent = store.insert_ref(kind="todo", slug=None, title="Spinning planner\nx")
    _mint_plan_ticks(store, parent.id, PLAN_TICK_REMINT_24H + 2)

    findings = _detect_plan_tick_spins(store)
    hits = [f for f in findings if f.ref_id == parent.id]
    assert len(hits) == 1
    assert hits[0].category == "plan-tick-spin"
    assert "plan_tick" in hits[0].detail
    assert hits[0].title == "Spinning planner"  # one line


def test_plan_tick_spin_detector_ignores_healthy_parent(store: Store) -> None:
    """A planner ticking a normal number of times is not a spin."""
    parent = store.insert_ref(kind="todo", slug=None, title="Healthy planner")
    _mint_plan_ticks(store, parent.id, 3)

    findings = _detect_plan_tick_spins(store)
    assert parent.id not in {f.ref_id for f in findings}


def test_spin_loop_detector_ignores_quiet_ref(store: Store) -> None:
    """A handful of events is normal background activity, not a loop."""
    ref = store.insert_ref(kind="paper", slug="calm", title="Calm", meta={})
    _seed_events(store, ref.id, "fetcher:s2", "no_oa_version", 5)

    findings = _detect_spin_loops(store)
    assert ref.id not in {f.ref_id for f in findings}


def test_spin_loop_detector_ignores_old_events(store: Store) -> None:
    """Events outside the 24h window don't count — yesterday's storm
    shouldn't keep flagging once the loop is fixed."""
    ref = store.insert_ref(kind="paper", slug="stale", title="Stale", meta={})
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO ref_events (ref_id, source, event, payload, ts) "
            "SELECT %s, 'fetcher:s2', 'no_oa_version', '{}'::jsonb, "
            "now() - interval '30 hours' FROM generate_series(1, %s)",
            (ref.id, SPIN_LOOP_EVENTS_24H + 5),
        )
        conn.commit()

    findings = _detect_spin_loops(store)
    assert ref.id not in {f.ref_id for f in findings}


# ── full pass → alerts ─────────────────────────────────────────────


def _open_alert_count(store: Store) -> int:
    return len(list_open_alerts(store))


def test_full_pass_raises_alerts_when_findings_appear(
    handler: TodoHandler, store: Store
) -> None:
    # Two orphans + one stale claim → 3 alerts across two sources.
    handler.put(text="Orphan A")
    handler.put(text="Orphan B")
    c = handler.put(text="Claimed")
    cid = _id_of(c.body)
    store.add_tag(cid, Tag.open("claimed-by:asa-worker"), set_by="agent")
    _backdate_tag(store, cid, "claimed-by:asa-worker", STALE_CLAIM_HOURS + 1)

    result = run_nursery_pass(store)
    assert result.claimed >= 3  # findings raised
    assert result.failed == 0

    alerts = list_open_alerts(store)
    sources = {a["source"] for a in alerts}
    assert "nursery:orphan" in sources
    assert "nursery:stale-claim" in sources
    # No memory digest is written any more.
    with store.pool.connection() as conn:
        memory_digests = conn.execute(
            "SELECT count(*) FROM refs r JOIN ref_tags rt USING(ref_id) "
            "JOIN tags t USING(tag_id) WHERE r.kind='memory' "
            "AND t.namespace='OPEN' AND t.value='tier:nursery'",
        ).fetchone()
    assert memory_digests[0] == 0


def test_full_pass_dedups_repeat_findings(handler: TodoHandler, store: Store) -> None:
    """A second pass over the same findings bumps seen_count, not a
    duplicate alert."""
    handler.put(text="Orphan O")
    run_nursery_pass(store)
    before = _open_alert_count(store)
    run_nursery_pass(store)
    after = _open_alert_count(store)
    assert after == before  # no duplicate row
    # seen_count incremented on the existing alert.
    alert = next(a for a in list_open_alerts(store) if a["source"] == "nursery:orphan")
    assert alert["seen_count"] >= 2


def test_full_pass_auto_resolves_cleared_condition(
    handler: TodoHandler, store: Store
) -> None:
    """When a finding disappears, its alert flips open → resolved on the
    next pass (the row is kept for history)."""
    r = handler.put(text="Transient orphan")
    rid = _id_of(r.body)
    run_nursery_pass(store)
    assert _open_alert_count(store) >= 1

    # Resolve the underlying orphan (mark the todo done), then re-run.
    handler.tag(id=rid, add=["STATUS:done"])
    result = run_nursery_pass(store)
    assert result.ok >= 1  # at least one alert auto-resolved
    assert _open_alert_count(store) == 0
    # The resolved alert is retained, not deleted.
    with store.pool.connection() as conn:
        resolved = conn.execute(
            "SELECT count(*) FROM refs r JOIN ref_tags rt USING(ref_id) "
            "JOIN tags t USING(tag_id) WHERE r.kind='alert' "
            "AND r.deleted_at IS NULL AND t.namespace='OPEN' AND t.value=%s",
            (STATE_RESOLVED,),
        ).fetchone()
    assert resolved[0] >= 1


def test_full_pass_reopen_is_idempotent(handler: TodoHandler, store: Store) -> None:
    """A condition that clears then recurs raises a fresh open alert
    (the prior one stays resolved) rather than stacking duplicates."""
    r = handler.put(text="Flapping orphan")
    rid = _id_of(r.body)
    run_nursery_pass(store)
    handler.tag(id=rid, add=["STATUS:done"])
    run_nursery_pass(store)
    assert _open_alert_count(store) == 0
    # Reopen the todo → orphan condition returns.
    handler.tag(id=rid, remove=["STATUS:done"])
    run_nursery_pass(store)
    open_now = [a for a in list_open_alerts(store) if a["source"] == "nursery:orphan"]
    assert len(open_now) == 1


def test_full_pass_empty_returns_clean(handler: TodoHandler, store: Store) -> None:
    # No todos, no findings, no alerts.
    result = run_nursery_pass(store)
    assert result.claimed == 0
    assert result.ok == 0
    assert _open_alert_count(store) == 0


def test_open_alerts_excludes_resolved(store: Store) -> None:
    """``list_open_alerts`` filters on the open-state tag."""
    from precis.alerts import raise_alert, resolve_stale_alerts

    raise_alert(
        store,
        source="test:probe",
        fingerprint="probe:1",
        title="probe alert",
        severity="info",
    )
    assert any(a["source"] == "test:probe" for a in list_open_alerts(store))
    # Clearing the condition resolves it → drops from the open list.
    resolve_stale_alerts(store, source="test:probe", live_fingerprints=[])
    assert not any(a["source"] == "test:probe" for a in list_open_alerts(store))
    # Sanity: it carries the resolved tag now, not the open one.
    with store.pool.connection() as conn:
        tags = conn.execute(
            "SELECT t.value FROM refs r JOIN ref_tags rt USING(ref_id) "
            "JOIN tags t USING(tag_id) WHERE r.kind='alert' "
            "AND r.meta->>'fingerprint'='probe:1' AND t.namespace='OPEN' "
            "AND t.value IN (%s, %s)",
            (STATE_OPEN, STATE_RESOLVED),
        ).fetchall()
    vals = {row[0] for row in tags}
    assert STATE_RESOLVED in vals
    assert STATE_OPEN not in vals


def test_helpers_hours_since_works() -> None:
    """``_hours_since`` handles naive timestamps + None."""
    from precis.workers.nursery import _hours_since

    now = datetime.now(UTC)
    assert _hours_since(now) < 0.01
    assert _hours_since(now - timedelta(hours=3)) > 2.9
    assert _hours_since(None) == 0.0


# ── worker-health detectors (daemon liveness) ─────────────────────


def _seed_boot_rows(
    store: Store,
    host: str,
    process: str,
    n: int,
    *,
    minutes_ago: float = 5.0,
    platform: str | None = None,
) -> None:
    """Insert ``n`` ``worker: started`` boot rows for one (host, process).

    ``platform`` stamps the boot payload so the detector can tailor its
    diagnosis (mirrors what ``_record_boot_event`` writes in prod).
    """
    payload = "NULL" if platform is None else "%(payload)s::jsonb"
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO worker_logs "
            "(ts, host, process, level, logger, message, payload) "
            "SELECT now() - (%(minutes_ago)s || ' minutes')::interval, "
            "%(host)s, %(process)s, 'INFO', 'precis.cli.worker', "
            f"'worker: started', {payload} FROM generate_series(1, %(n)s)",
            {
                "minutes_ago": minutes_ago,
                "host": host,
                "process": process,
                "n": n,
                "payload": json.dumps({"event": "boot", "platform": platform}),
            },
        )
        conn.commit()


def _seed_worker_log(
    store: Store, host: str, process: str, *, minutes_ago: float
) -> None:
    """Insert one ordinary per-pass log row (marks the daemon as having beaten)."""
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO worker_logs (ts, host, process, level, logger, message) "
            "VALUES (now() - (%s || ' minutes')::interval, %s, %s, 'INFO', "
            "'precis.workers.embed', 'worker: embed claimed=0 ok=0 failed=0')",
            (minutes_ago, host, process),
        )
        conn.commit()


def _seed_heartbeat(store: Store, host: str, *, minutes_ago: float = 0.0) -> None:
    """Mark a host alive via a fresh host_heartbeat row."""
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO host_heartbeat (host, ts) "
            "VALUES (%s, now() - (%s || ' minutes')::interval) "
            "ON CONFLICT (host) DO UPDATE SET ts = EXCLUDED.ts",
            (host, minutes_ago),
        )
        conn.commit()


def _host() -> str:
    return f"th-{uuid4().hex[:8]}"


def test_worker_restart_storm_flags_thrashing_daemon(store: Store) -> None:
    """> WORKER_RESTART_STORM_1H boot rows for a (host, process) in 1h fires."""
    host = _host()
    _seed_boot_rows(store, host, "precis-worker-agent", WORKER_RESTART_STORM_1H + 3)

    findings = _detect_worker_restart_storms(store)
    key = f"worker-restart:{host}:precis-worker-agent"
    hits = [f for f in findings if f.fingerprint_key == key]
    assert len(hits) == 1
    assert hits[0].category == "worker-restart"
    assert hits[0].ref_id is None
    assert host in hits[0].title


def test_worker_restart_storm_ignores_normal_bounce(store: Store) -> None:
    """A couple of relaunches (a deploy) is not a storm."""
    host = _host()
    _seed_boot_rows(store, host, "precis-worker", 2)

    findings = _detect_worker_restart_storms(store)
    assert not any(
        f.fingerprint_key == f"worker-restart:{host}:precis-worker" for f in findings
    )


def test_worker_restart_storm_ignores_old_boots(store: Store) -> None:
    """Boots outside the 1h window don't count."""
    host = _host()
    _seed_boot_rows(
        store, host, "precis-worker", WORKER_RESTART_STORM_1H + 3, minutes_ago=90
    )

    findings = _detect_worker_restart_storms(store)
    assert not any(
        f.fingerprint_key == f"worker-restart:{host}:precis-worker" for f in findings
    )


def test_restart_storm_message_is_hedged_not_cause_asserting() -> None:
    """The detail no longer asserts a single cause (the gr51351 bug)."""
    for plat in ("darwin", "linux", None):
        detail = _restart_storm_detail("precis-worker", "h", 9, plat)
        # Both hypotheses are named; neither is asserted as fact.
        assert "deploy bounce" in detail
        assert "crash" in detail or "OOM" in detail
        assert "not a deploy bounce" not in detail


def test_restart_storm_message_tailors_command_to_linux(store: Store) -> None:
    """A Linux boot storm gets journalctl advice, not launchctl/jetsam."""
    host = _host()
    _seed_boot_rows(
        store, host, "precis-worker", WORKER_RESTART_STORM_1H + 1, platform="linux"
    )
    finding = next(
        f
        for f in _detect_worker_restart_storms(store)
        if f.fingerprint_key == f"worker-restart:{host}:precis-worker"
    )
    assert "journalctl" in finding.detail
    assert "launchctl" not in finding.detail
    assert "jetsam" not in finding.detail


def test_restart_storm_message_tailors_command_to_macos(store: Store) -> None:
    """A macOS boot storm keeps the launchctl/jetsam diagnosis."""
    host = _host()
    _seed_boot_rows(
        store,
        host,
        "precis-worker-agent",
        WORKER_RESTART_STORM_1H + 1,
        platform="darwin",
    )
    finding = next(
        f
        for f in _detect_worker_restart_storms(store)
        if f.fingerprint_key == f"worker-restart:{host}:precis-worker-agent"
    )
    assert "launchctl" in finding.detail
    assert "jetsam" in finding.detail


def test_restart_storm_message_neutral_without_platform(store: Store) -> None:
    """Pre-fix boot rows (no platform) fall back to an OS-neutral message."""
    host = _host()
    _seed_boot_rows(store, host, "precis-worker", WORKER_RESTART_STORM_1H + 1)
    finding = next(
        f
        for f in _detect_worker_restart_storms(store)
        if f.fingerprint_key == f"worker-restart:{host}:precis-worker"
    )
    # Neutral fallback names both OSes' tools rather than guessing.
    assert "journalctl" in finding.detail and "launchctl" in finding.detail


def test_dead_worker_flags_silent_daemon_on_live_host(store: Store) -> None:
    """A continuous daemon silent > threshold while its host is alive fires."""
    host = _host()
    _seed_worker_log(
        store, host, "precis-worker-agent", minutes_ago=DEAD_WORKER_SILENCE_MIN + 5
    )
    _seed_heartbeat(store, host, minutes_ago=0)  # host is up

    findings = _detect_dead_workers(store)
    key = f"dead-worker:{host}:precis-worker-agent"
    hits = [f for f in findings if f.fingerprint_key == key]
    assert len(hits) == 1
    assert hits[0].category == "dead-worker"
    assert hits[0].ref_id is None


def test_dead_worker_ignores_live_daemon(store: Store) -> None:
    """A daemon that logged recently is not dead."""
    host = _host()
    _seed_worker_log(store, host, "precis-worker-agent", minutes_ago=1)
    _seed_heartbeat(store, host, minutes_ago=0)

    findings = _detect_dead_workers(store)
    assert not any(
        f.fingerprint_key == f"dead-worker:{host}:precis-worker-agent" for f in findings
    )


def test_dead_worker_ignores_when_whole_host_down(store: Store) -> None:
    """Silent daemon + no host liveness signal ⇒ a host/DB outage, not a
    per-daemon dead-worker (don't fan one failure into N alerts)."""
    host = _host()
    _seed_worker_log(
        store, host, "precis-worker-agent", minutes_ago=DEAD_WORKER_SILENCE_MIN + 5
    )
    # no fresh log, no heartbeat → host not "alive"

    findings = _detect_dead_workers(store)
    assert not any(
        f.fingerprint_key == f"dead-worker:{host}:precis-worker-agent" for f in findings
    )


def test_dead_worker_ignores_periodic_process(store: Store) -> None:
    """Only continuous daemons are watched — a periodic one-shot silent
    between runs must not alarm."""
    host = _host()
    _seed_worker_log(
        store, host, "precis-cron-tick", minutes_ago=DEAD_WORKER_SILENCE_MIN + 5
    )
    _seed_heartbeat(store, host, minutes_ago=0)

    findings = _detect_dead_workers(store)
    assert not any(f.category == "dead-worker" and host in f.title for f in findings)


def test_run_nursery_pass_raises_critical_for_dead_worker(store: Store) -> None:
    """End to end: a dead-worker finding becomes an open ``critical`` alert
    (exercises the fingerprint_key + is_new push-gate path; the push itself
    is a no-op with no webhook configured)."""
    host = _host()
    _seed_worker_log(
        store, host, "precis-worker-agent", minutes_ago=DEAD_WORKER_SILENCE_MIN + 5
    )
    _seed_heartbeat(store, host, minutes_ago=0)

    run_nursery_pass(store)

    alerts = list_open_alerts(store)
    mine = [
        a
        for a in alerts
        if a["source"] == "nursery:dead-worker" and host in (a["title"] or "")
    ]
    assert len(mine) == 1
    assert mine[0]["severity"] == "critical"


def test_raise_alert_reports_new_then_bumped(store: Store) -> None:
    """``raise_alert`` returns is_new=True on first sighting, False on repeat."""
    fp = f"probe-new:{uuid4().hex[:8]}"
    ref_id_1, new_1 = raise_alert(
        store, source="test:probe", fingerprint=fp, title="probe", severity="critical"
    )
    ref_id_2, new_2 = raise_alert(
        store, source="test:probe", fingerprint=fp, title="probe", severity="critical"
    )
    assert new_1 is True
    assert new_2 is False
    assert ref_id_1 == ref_id_2


def test_record_boot_event_lands_and_is_counted(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The emitter (`_record_boot_event`) writes a real `worker: started`
    row that the restart-storm detector counts — closing the loop the
    buffered log handler silently broke (boot line hit the file, not the DB)."""
    from precis.cli.worker import _record_boot_event

    host = _host()
    monkeypatch.setenv("PRECIS_HOST_NAME", host)
    monkeypatch.setenv("PRECIS_PROCESS", "precis-worker-agent")
    for _ in range(WORKER_RESTART_STORM_1H + 1):
        _record_boot_event(store, profile="agent")

    findings = _detect_worker_restart_storms(store)
    key = f"worker-restart:{host}:precis-worker-agent"
    hits = [f for f in findings if f.fingerprint_key == key]
    assert len(hits) == 1


# ── dispatch-stall (planner dark — single agent-profile executor) ──


def _mint_inproc_job(
    store: Store,
    parent_id: int,
    *,
    status: str,
    age_hours: float = 0.0,
    lease_until: datetime | None = None,
    job_type: str = "plan_tick",
) -> int:
    """Mint a ``claude_inproc`` job with a ``STATUS`` tag, optional lease, and
    a backdated ``created_at`` (jobs sit ``queued`` from mint time)."""
    meta: dict[str, object] = {"job_type": job_type, "executor": "claude_inproc"}
    if lease_until is not None:
        meta["lease_until"] = lease_until.isoformat()
    ref = store.insert_ref(
        kind="job", slug=None, title=f"{job_type} job", meta=meta, parent_id=parent_id
    )
    store.add_tag(ref.id, Tag.closed("STATUS", status), set_by="agent")
    if age_hours:
        _backdate_ref(store, ref.id, age_hours)
    return ref.id


# Comfortably past the queued-age threshold.
_STALL_H = (DISPATCH_STALL_MINUTES + 5) / 60.0


def test_dispatch_stall_flags_queued_with_nothing_running(store: Store) -> None:
    """Old queued claude_inproc jobs + nothing running = the executor stopped
    claiming (dead/culled/never-started agent worker) → one critical finding."""
    parent = store.insert_ref(kind="todo", slug=None, title="LLM planner")
    _mint_inproc_job(store, parent.id, status="queued", age_hours=_STALL_H)
    _mint_inproc_job(store, parent.id, status="queued", age_hours=_STALL_H)

    findings = _detect_dispatch_stalls(store)
    assert len(findings) == 1
    f = findings[0]
    assert f.category == "dispatch-stall"
    assert f.ref_id is None  # a cluster-wide condition, not per-todo
    assert f.fingerprint_key == "dispatch-stall"
    assert "queued" in f.detail


def test_dispatch_stall_ignores_fresh_queue(store: Store) -> None:
    """A just-minted queued job is normal — a healthy executor hasn't had a
    loop to claim it yet, so it must not alarm."""
    parent = store.insert_ref(kind="todo", slug=None, title="LLM planner")
    _mint_inproc_job(store, parent.id, status="queued", age_hours=0.0)

    assert _detect_dispatch_stalls(store) == []


def test_dispatch_stall_suppressed_when_executor_running(store: Store) -> None:
    """An old queued job behind a LIVE running job (unexpired lease) is a
    healthy backlog, not a dead executor — the 'nothing running' gate."""
    parent = store.insert_ref(kind="todo", slug=None, title="LLM planner")
    _mint_inproc_job(store, parent.id, status="queued", age_hours=_STALL_H)
    _mint_inproc_job(
        store,
        parent.id,
        status="running",
        lease_until=datetime.now(UTC) + timedelta(minutes=60),
    )

    assert _detect_dispatch_stalls(store) == []


def test_dispatch_stall_fires_when_running_lease_expired(store: Store) -> None:
    """A running job whose lease has EXPIRED is a dead claim, not a live
    executor — it must not mask a stalled queue."""
    parent = store.insert_ref(kind="todo", slug=None, title="LLM planner")
    _mint_inproc_job(store, parent.id, status="queued", age_hours=_STALL_H)
    _mint_inproc_job(
        store,
        parent.id,
        status="running",
        lease_until=datetime.now(UTC) - timedelta(minutes=5),
    )

    findings = _detect_dispatch_stalls(store)
    assert len(findings) == 1
    assert findings[0].category == "dispatch-stall"


def test_run_nursery_pass_raises_critical_for_dispatch_stall(store: Store) -> None:
    """End to end: a dispatch-stall finding becomes an open ``critical`` alert
    under the ``nursery:dispatch-stall`` source."""
    parent = store.insert_ref(kind="todo", slug=None, title="LLM planner")
    _mint_inproc_job(store, parent.id, status="queued", age_hours=_STALL_H)

    run_nursery_pass(store)

    alerts = list_open_alerts(store)
    mine = [a for a in alerts if a["source"] == "nursery:dispatch-stall"]
    assert len(mine) == 1
    assert mine[0]["severity"] == "critical"
