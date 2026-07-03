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

from datetime import UTC, datetime, timedelta

import pytest

from precis.alerts import STATE_OPEN, STATE_RESOLVED, list_open_alerts
from precis.dispatch import Hub
from precis.handlers.todo import TodoHandler
from precis.store import Store
from precis.store.types import Tag
from precis.workers.nursery import (
    LONG_WAIT_DAYS,
    PLAN_TICK_REMINT_24H,
    SPIN_LOOP_EVENTS_24H,
    STALE_CLAIM_HOURS,
    STUCK_DOABLE_HOURS,
    _detect_long_waits,
    _detect_orphans,
    _detect_plan_tick_spins,
    _detect_spin_loops,
    _detect_stale_claims,
    _detect_stalled_recurrings,
    _detect_stuck_doable,
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
