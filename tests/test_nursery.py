"""Slice-3 nursery tests — detectors, dedup, digest writer.

Five detector categories, plus the fingerprint-dedup path:

* orphans — open todos with no ``meta.rotation_root`` ancestor
* stale-claim — ``claimed-by:*`` older than ``STALE_CLAIM_HOURS``
* long-wait — ``waiting-for:*`` older than ``LONG_WAIT_DAYS``
* stuck-doable — dispatch-candidate open leaf, no claim/wait/block/
  exclusion-tag, >24h old
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
    CHILD_FAILED_PARKED_HOURS,
    DEAD_WORKER_LOOKBACK_DAYS,
    DEAD_WORKER_SILENCE_MIN,
    DISPATCH_STALL_MINUTES,
    EMBED_LANE_STALL_WINDOW_MIN,
    HOST_DARK_LOOKBACK_DAYS,
    HOST_DARK_SILENCE_MIN,
    LONG_WAIT_DAYS,
    ORPHANED_COORDINATOR_STALE_HOURS,
    PLAN_TICK_REMINT_24H,
    QUEST_LOOP_FAIL_24H,
    SPIN_LOOP_EVENTS_24H,
    STALE_CLAIM_HOURS,
    STUCK_DOABLE_HOURS,
    WORKER_CONTINUOUS_PROCESSES,
    WORKER_RESTART_STORM_1H,
    _dead_worker_detail,
    _detect_child_failed_final,
    _detect_child_failed_parked,
    _detect_dead_workers,
    _detect_dispatch_stalls,
    _detect_embed_lane_stalled,
    _detect_host_dark,
    _detect_long_waits,
    _detect_nas_denied,
    _detect_orphaned_coordinator,
    _detect_orphans,
    _detect_plan_tick_spins,
    _detect_quest_loop_failures,
    _detect_spin_loops,
    _detect_stale_claims,
    _detect_stalled_recurrings,
    _detect_stuck_doable,
    _detect_worker_restart_storms,
    _restart_storm_detail,
    run_nursery_pass,
)

#: A daemon the dead-worker detector actually watches. Taken from the live
#: constant rather than spelled out: these tests previously hardcoded
#: ``precis-worker-agent``, which went silently vacuous the day that daemon was
#: retired (2026-08-04 single-worker consolidation) — the seeded process was no
#: longer in :data:`WORKER_CONTINUOUS_PROCESSES`, so the detector correctly
#: found nothing and the assertions failed. Binding to the constant keeps them
#: testing the *behaviour* across any future rename.
_CONTINUOUS_DAEMON = WORKER_CONTINUOUS_PROCESSES[0]


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


def _backdate_status_tag(store: Store, ref_id: int, hours: float) -> None:
    """Move the ``STATUS:*`` ``ref_tags.created_at`` backwards for one ref."""
    with store.pool.connection() as conn:
        conn.execute(
            """
            UPDATE ref_tags rt
               SET created_at = now() - %s::interval
              FROM tags t
             WHERE rt.tag_id = t.tag_id
               AND rt.ref_id = %s
               AND t.namespace = 'STATUS'
            """,
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
    # Root todo without meta.rotation_root — its descendants are orphans.
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
    root = handler.put(text="Real strategic", meta={"rotation_root": True})
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
        meta={"schedule": {"cron": "0 * * * *"}},
    )
    # The Watches umbrella + its child are both under the umbrella's
    # subtree. Neither should appear as an orphan.
    findings = _detect_orphans(store)
    titles = {f.title for f in findings}
    assert "Hourly watcher" not in titles


def test_orphans_detector_reports_true_total_when_capped(store: Store) -> None:
    """>50 matching orphans: only 50 ``Finding``s surface, but each one
    carries the true pre-LIMIT total, and the raised alert's detail says
    so (gr — alert-triage §A/§B)."""
    for i in range(55):
        store.insert_ref(kind="todo", slug=None, title=f"Orphan {i}")

    findings = _detect_orphans(store)
    assert len(findings) == 50
    assert all(f.total == 55 for f in findings)

    run_nursery_pass(store)
    alert = next(a for a in list_open_alerts(store) if a["source"] == "nursery:orphan")
    assert "of 55" in alert["detail"]


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


def test_stuck_doable_detector_flags_old_candidate_leaf(
    handler: TodoHandler, store: Store
) -> None:
    """A genuine dispatch candidate (``meta.llm_tier`` set) that's old,
    unclaimed, and unblocked is still flagged — the detector's core
    positive case."""
    r = handler.put(text="Old open leaf", meta={"llm_tier": "opus"})
    rid = _id_of(r.body)
    _backdate_ref(store, rid, STUCK_DOABLE_HOURS + 1)

    findings = _detect_stuck_doable(store)
    ids = {f.ref_id for f in findings}
    assert rid in ids


def test_stuck_doable_detector_skips_non_candidate_leaf(
    handler: TodoHandler, store: Store
) -> None:
    """No ``meta.executor`` / ``meta.llm_tier`` / ``OPEN:executor:*`` —
    dispatch would never touch this leaf, so it was never doable in
    the first place, not "stuck" (gripe 204308: the candidacy gate)."""
    r = handler.put(text="Old open leaf, no run signal")
    rid = _id_of(r.body)
    _backdate_ref(store, rid, STUCK_DOABLE_HOURS + 1)

    findings = _detect_stuck_doable(store)
    ids = {f.ref_id for f in findings}
    assert rid not in ids


def test_stuck_doable_detector_skips_claimed_leaf(
    handler: TodoHandler, store: Store
) -> None:
    r = handler.put(text="Old + claimed", meta={"llm_tier": "opus"})
    rid = _id_of(r.body)
    store.add_tag(rid, Tag.open("claimed-by:asa-worker"), set_by="agent")
    _backdate_ref(store, rid, STUCK_DOABLE_HOURS + 1)

    findings = _detect_stuck_doable(store)
    ids = {f.ref_id for f in findings}
    assert rid not in ids


def test_stuck_doable_detector_skips_waiting_leaf(
    handler: TodoHandler, store: Store
) -> None:
    r = handler.put(text="Old + waiting", meta={"llm_tier": "opus"})
    rid = _id_of(r.body)
    store.add_tag(rid, Tag.open("waiting-for:something"), set_by="agent")
    _backdate_ref(store, rid, STUCK_DOABLE_HOURS + 1)

    findings = _detect_stuck_doable(store)
    ids = {f.ref_id for f in findings}
    assert rid not in ids


def test_stuck_doable_detector_skips_halted_leaf(
    handler: TodoHandler, store: Store
) -> None:
    """A self-halted candidate (``OPEN:halt:tick-cap``) is working as
    designed, not stuck — the shared exclusion registry
    (``_todo_views._doable_exclusion_clause``), not a hand-copied
    tag list, is what has to catch this."""
    r = handler.put(text="Old + halted", meta={"llm_tier": "opus"})
    rid = _id_of(r.body)
    store.add_tag(rid, Tag.open("halt:tick-cap"), set_by="system")
    _backdate_ref(store, rid, STUCK_DOABLE_HOURS + 1)

    findings = _detect_stuck_doable(store)
    ids = {f.ref_id for f in findings}
    assert rid not in ids


def test_stuck_doable_detector_skips_ephemeral_compute_leaf(
    handler: TodoHandler, store: Store
) -> None:
    """Mirrors the autocatpath compute-lane shape (``quest/compute.py::
    _ensure_autocatpath_todo``): an ``OPEN:ephemeral`` leaf with no run
    signal of its own — dispatch only ever mints its child job.
    Excluded via the candidacy gate, not a special-case ``ephemeral``
    tag match (the 2026-08-12 audit's 9 false-positive autocatpath
    internals were exactly this shape)."""
    r = handler.put(text="autocatpath seed 3 model#0")
    rid = _id_of(r.body)
    store.add_tag(rid, Tag.open("ephemeral"), set_by="system")
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
        meta={"schedule": {"cron": "0 * * * *"}},
    )
    findings = _detect_stuck_doable(store)
    titles = {f.title for f in findings}
    assert "Watcher" not in titles
    assert "Watches" not in titles


def test_stuck_doable_detector_reports_true_total_when_capped(store: Store) -> None:
    """>50 matching stuck-doable leaves: only 50 ``Finding``s surface, but
    each one carries the true pre-LIMIT total, and the raised alert's
    detail says so."""
    for i in range(55):
        r = store.insert_ref(
            kind="todo", slug=None, title=f"Stuck {i}", meta={"llm_tier": "opus"}
        )
        _backdate_ref(store, r.id, STUCK_DOABLE_HOURS + 1)

    findings = _detect_stuck_doable(store)
    assert len(findings) == 50
    assert all(f.total == 55 for f in findings)

    run_nursery_pass(store)
    alert = next(
        a for a in list_open_alerts(store) if a["source"] == "nursery:stuck-doable"
    )
    assert "of 55" in alert["detail"]


# ── child-failed parked ────────────────────────────────────────────


def test_child_failed_parked_detector_flags_old_bubble(
    handler: TodoHandler, store: Store
) -> None:
    r = handler.put(text="Stuck leaf")
    rid = _id_of(r.body)
    store.add_tag(rid, Tag.open("child-failed:999"), set_by="system")
    _backdate_tag(store, rid, "child-failed:999", CHILD_FAILED_PARKED_HOURS + 1)

    findings = _detect_child_failed_parked(store)
    ids = {f.ref_id for f in findings}
    assert rid in ids


def test_child_failed_parked_detector_ignores_fresh_bubble(
    handler: TodoHandler, store: Store
) -> None:
    r = handler.put(text="Just parked")
    rid = _id_of(r.body)
    store.add_tag(rid, Tag.open("child-failed:999"), set_by="system")
    # No backdate — bubble is fresh.
    findings = _detect_child_failed_parked(store)
    ids = {f.ref_id for f in findings}
    assert rid not in ids


def test_child_failed_parked_detector_ignores_done_leaf(
    handler: TodoHandler, store: Store
) -> None:
    r = handler.put(text="Old + done despite bubble")
    rid = _id_of(r.body)
    store.add_tag(rid, Tag.open("child-failed:999"), set_by="system")
    _backdate_tag(store, rid, "child-failed:999", CHILD_FAILED_PARKED_HOURS + 1)
    handler.tag(id=rid, add=["STATUS:done"])

    findings = _detect_child_failed_parked(store)
    ids = {f.ref_id for f in findings}
    assert rid not in ids


def test_child_failed_parked_detector_dedups_multiple_bubbles(
    handler: TodoHandler, store: Store
) -> None:
    """A parent can carry more than one child-failed:<job_id> tag (each
    failed child job adds its own) — one Finding per ref_id, not per tag."""
    r = handler.put(text="Twice-bubbled leaf")
    rid = _id_of(r.body)
    store.add_tag(rid, Tag.open("child-failed:111"), set_by="system")
    _backdate_tag(store, rid, "child-failed:111", CHILD_FAILED_PARKED_HOURS + 5)
    store.add_tag(rid, Tag.open("child-failed:222"), set_by="system")
    _backdate_tag(store, rid, "child-failed:222", CHILD_FAILED_PARKED_HOURS + 1)

    findings = [f for f in _detect_child_failed_parked(store) if f.ref_id == rid]
    assert len(findings) == 1
    # Reports the oldest bubble — the parent has been parked ~5h, not ~1h.
    assert "child-failed:111" in findings[0].detail


def test_child_failed_parked_detector_excludes_child_failed_final(
    handler: TodoHandler, store: Store
) -> None:
    """Acceptance (parked-leaf-recovery, docs/backlog/parked-leaf-
    recovery.md): a leaf that hit the sweeper's unpark cap and carries
    ``child-failed-final`` is excluded from the per-leaf stream — it's
    reported instead, in aggregate, by :func:`_detect_child_failed_final`."""
    r = handler.put(text="Terminally parked")
    rid = _id_of(r.body)
    store.add_tag(rid, Tag.open("child-failed:999"), set_by="system")
    _backdate_tag(store, rid, "child-failed:999", CHILD_FAILED_PARKED_HOURS + 1)
    store.add_tag(rid, Tag.open("child-failed-final"), set_by="system")

    findings = _detect_child_failed_parked(store)
    ids = {f.ref_id for f in findings}
    assert rid not in ids


def test_child_failed_parked_detector_reports_true_total_when_capped(
    store: Store,
) -> None:
    """>50 matching parked parents: only 50 ``Finding``s surface, but each
    one carries the true pre-LIMIT (post-``DISTINCT ON``) total, and the
    raised alert's detail says so."""
    for i in range(55):
        r = store.insert_ref(kind="todo", slug=None, title=f"Parked {i}")
        store.add_tag(r.id, Tag.open(f"child-failed:{900 + i}"), set_by="system")
        _backdate_tag(
            store, r.id, f"child-failed:{900 + i}", CHILD_FAILED_PARKED_HOURS + 1
        )

    findings = _detect_child_failed_parked(store)
    assert len(findings) == 50
    assert all(f.total == 55 for f in findings)

    run_nursery_pass(store)
    alert = next(
        a
        for a in list_open_alerts(store)
        if a["source"] == "nursery:child-failed-parked"
    )
    assert "of 55" in alert["detail"]


# ── child-failed-final (aggregate) ──────────────────────────────────


def test_child_failed_final_detector_emits_one_aggregate_finding(
    handler: TodoHandler, store: Store
) -> None:
    """Acceptance: N leaves at ``child-failed-final`` produce ONE finding
    (not per-leaf) carrying the count."""
    for i in range(3):
        r = handler.put(text=f"terminally parked {i}")
        rid = _id_of(r.body)
        store.add_tag(rid, Tag.open(f"child-failed:{900 + i}"), set_by="system")
        store.add_tag(rid, Tag.open("child-failed-final"), set_by="system")

    findings = _detect_child_failed_final(store)
    assert len(findings) == 1
    assert findings[0].ref_id is None
    assert findings[0].fingerprint_key == "child-failed-final:aggregate"
    assert "3" in findings[0].title or "3" in findings[0].detail


def test_child_failed_final_detector_empty_when_none(store: Store) -> None:
    assert _detect_child_failed_final(store) == []


def test_child_failed_final_detector_ignores_done_leaf(
    handler: TodoHandler, store: Store
) -> None:
    """A leaf that reached ``child-failed-final`` but was later closed
    (STATUS:done, e.g. a human resolved it manually) drops out of the
    count."""
    r = handler.put(text="terminally parked but closed")
    rid = _id_of(r.body)
    store.add_tag(rid, Tag.open("child-failed:999"), set_by="system")
    store.add_tag(rid, Tag.open("child-failed-final"), set_by="system")
    handler.tag(id=rid, add=["STATUS:done"])

    assert _detect_child_failed_final(store) == []


# ── stalled recurrings ────────────────────────────────────────────


def test_stalled_recurring_detector_flags_old_open_child(
    handler: TodoHandler, store: Store
) -> None:
    rec = handler.put(
        text="Hourly",
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


def _mint_quest_loops(store: Store, quest_id: int, n: int, *, status: str) -> None:
    """Insert ``n`` terminal ``quest_tick`` coordinator loops for ``quest_id``,
    each carrying the given closed STATUS (fresh ``created_at`` → within 24h)."""
    for i in range(n):
        ref = store.insert_ref(
            kind="job",
            slug=None,
            title=f"quest_tick loop {i}",
            meta={
                "job_type": "quest_tick",
                "executor": "coordinator",
                "idem_key": f"quest_tick:{quest_id}",
            },
            parent_id=quest_id,
        )
        store.add_tag(ref.id, Tag.closed("STATUS", status), set_by="system")


def test_quest_loop_failing_detector_flags_repeatedly_failing_loop(
    store: Store,
) -> None:
    """A quest whose ``quest_tick`` loop rests ``STATUS:failed`` more than
    QUEST_LOOP_FAIL_24H times in 24h surfaces as a ``quest-loop-failing``
    finding (RC1)."""
    q = store.insert_ref(kind="quest", slug=None, title="Stuck quest\nmore")
    _mint_quest_loops(store, q.id, QUEST_LOOP_FAIL_24H + 2, status="failed")

    findings = _detect_quest_loop_failures(store)
    hits = [f for f in findings if f.ref_id == q.id]
    assert len(hits) == 1
    assert hits[0].category == "quest-loop-failing"
    assert "STATUS:failed" in hits[0].detail
    assert hits[0].title == "Stuck quest"  # one line


def test_quest_loop_failing_detector_ignores_occasional_failure(store: Store) -> None:
    """A couple of failures in a day is tolerable, not a standing break."""
    q = store.insert_ref(kind="quest", slug=None, title="Mostly-healthy quest")
    _mint_quest_loops(store, q.id, QUEST_LOOP_FAIL_24H, status="failed")

    findings = _detect_quest_loop_failures(store)
    assert q.id not in {f.ref_id for f in findings}


def test_quest_loop_failing_detector_ignores_succeeded_rests(store: Store) -> None:
    """A quest whose loops rest ``succeeded`` (dry / punt) is healthy — never
    flagged, however many times it re-arms."""
    q = store.insert_ref(kind="quest", slug=None, title="Busy healthy quest")
    _mint_quest_loops(store, q.id, QUEST_LOOP_FAIL_24H + 3, status="succeeded")

    findings = _detect_quest_loop_failures(store)
    assert q.id not in {f.ref_id for f in findings}


# ── orphaned coordinator (silent-outage: zero re-mint attempts) ────────


def _mk_active_quest(store: Store, title: str) -> int:
    q = store.insert_ref(kind="quest", slug=None, title=title)
    store.add_tag(q.id, Tag.closed("STATUS", "active"), set_by="system")
    return int(q.id)


def _mint_quest_loop(store: Store, quest_id: int, *, status: str) -> int:
    ref = store.insert_ref(
        kind="job",
        slug=None,
        title="quest_tick loop",
        meta={
            "job_type": "quest_tick",
            "executor": "coordinator",
            "idem_key": f"quest_tick:{quest_id}",
        },
        parent_id=quest_id,
    )
    store.add_tag(ref.id, Tag.closed("STATUS", status), set_by="system")
    return int(ref.id)


def _mk_llm_planner(store: Store, title: str) -> int:
    """A ``todo`` carrying the planner-coroutine ``meta.llm_tier``
    signature (STATUS defaults to open — nothing else needed to make it
    a candidate-eligible parent for ``_detect_orphaned_coordinator``)."""
    p = store.insert_ref(kind="todo", slug=None, title=title, meta={"llm_tier": "opus"})
    return int(p.id)


def _mint_plan_tick(store: Store, parent_id: int, *, status: str) -> int:
    ref = store.insert_ref(
        kind="job",
        slug=None,
        title="plan_tick",
        meta={"job_type": "plan_tick"},
        parent_id=parent_id,
    )
    store.add_tag(ref.id, Tag.closed("STATUS", status), set_by="system")
    return int(ref.id)


def test_orphaned_coordinator_detector_flags_dark_quest(store: Store) -> None:
    """An active quest whose newest ``quest_tick`` loop rested
    STATUS:failed past ORPHANED_COORDINATOR_STALE_HOURS, with nothing
    newer minted, is a silent outage — flagged even though it's only
    ONE failed row (below ``QUEST_LOOP_FAIL_24H``'s spin threshold)."""
    q = _mk_active_quest(store, "Dark quest\nmore detail")
    job_id = _mint_quest_loop(store, q, status="failed")
    _backdate_status_tag(store, job_id, ORPHANED_COORDINATOR_STALE_HOURS + 1)

    findings = _detect_orphaned_coordinator(store)
    hits = [f for f in findings if f.ref_id == q]
    assert len(hits) == 1
    assert hits[0].category == "orphaned-coordinator"
    assert hits[0].title == "Dark quest"
    assert f"#{job_id}" in hits[0].detail

    # Also confirmed critical severity through the real registration path.
    from precis.workers.nursery import _SEVERITY

    assert _SEVERITY["orphaned-coordinator"] == "critical"


def test_orphaned_coordinator_detector_ignores_fresh_quest_failure(
    store: Store,
) -> None:
    """A quest_tick that JUST rested failed hasn't had time for the
    reconciler's own backoff to even elapse — not yet an outage."""
    q = _mk_active_quest(store, "Recently failed quest")
    _mint_quest_loop(store, q, status="failed")

    findings = _detect_orphaned_coordinator(store)
    assert q not in {f.ref_id for f in findings}


def test_orphaned_coordinator_detector_ignores_quest_with_live_replacement(
    store: Store,
) -> None:
    """A stale failed loop with a newer non-terminal (queued/running) loop
    already minted is healthy — the reconciler IS re-arming it."""
    q = _mk_active_quest(store, "Recovering quest")
    old_job = _mint_quest_loop(store, q, status="failed")
    _backdate_status_tag(store, old_job, ORPHANED_COORDINATOR_STALE_HOURS + 1)
    _mint_quest_loop(store, q, status="running")  # newer, non-terminal

    findings = _detect_orphaned_coordinator(store)
    assert q not in {f.ref_id for f in findings}


def test_orphaned_coordinator_detector_ignores_inactive_quest(store: Store) -> None:
    """A quest that isn't ``STATUS:active`` (dormant/done) is not expected
    to be ticking at all — a stale failed loop there is not an outage."""
    q = store.insert_ref(kind="quest", slug=None, title="Dormant quest")
    job_id = _mint_quest_loop(store, int(q.id), status="failed")
    _backdate_status_tag(store, job_id, ORPHANED_COORDINATOR_STALE_HOURS + 1)

    findings = _detect_orphaned_coordinator(store)
    assert int(q.id) not in {f.ref_id for f in findings}


def test_orphaned_coordinator_detector_flags_dark_planner(store: Store) -> None:
    """An open, meta.llm_tier-set planner parent whose newest ``plan_tick``
    child rested STATUS:failed past the stale window, with no newer child
    minted and no child-failed/halt tag on it, is dark — the planner-lane
    analogue of the quest case (e.g. an infra-class retry per
    ``handlers/_job_bubble.py`` that never got a dispatch pass to act
    on it because dispatch itself is down)."""
    p = _mk_llm_planner(store, "Dark planner\nmore")
    job_id = _mint_plan_tick(store, p, status="failed")
    _backdate_status_tag(store, job_id, ORPHANED_COORDINATOR_STALE_HOURS + 1)

    findings = _detect_orphaned_coordinator(store)
    hits = [f for f in findings if f.ref_id == p]
    assert len(hits) == 1
    assert hits[0].category == "orphaned-coordinator"
    assert hits[0].title == "Dark planner"


def test_orphaned_coordinator_detector_ignores_planner_with_live_replacement(
    store: Store,
) -> None:
    """A stale failed plan_tick with a newer queued/running one is healthy."""
    p = _mk_llm_planner(store, "Recovering planner")
    old_job = _mint_plan_tick(store, p, status="failed")
    _backdate_status_tag(store, old_job, ORPHANED_COORDINATOR_STALE_HOURS + 1)
    _mint_plan_tick(store, p, status="queued")

    findings = _detect_orphaned_coordinator(store)
    assert p not in {f.ref_id for f in findings}


def test_orphaned_coordinator_detector_ignores_planner_already_flagged(
    store: Store,
) -> None:
    """A planner already carrying ``child-failed:*`` is already surfaced by
    ``child-failed-parked`` — this detector is for the case nothing else
    caught, so it stays quiet rather than double-alerting."""
    p = _mk_llm_planner(store, "Already-latched planner")
    job_id = _mint_plan_tick(store, p, status="failed")
    _backdate_status_tag(store, job_id, ORPHANED_COORDINATOR_STALE_HOURS + 1)
    store.add_tag(p, Tag.open(f"child-failed:{job_id}"), set_by="system")

    findings = _detect_orphaned_coordinator(store)
    assert p not in {f.ref_id for f in findings}


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
    assert memory_digests is not None
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
    assert resolved is not None
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


def _seed_heartbeat(
    store: Store,
    host: str,
    *,
    minutes_ago: float = 0.0,
    meta: dict | None = None,
) -> None:
    """Mark a host alive via a fresh host_heartbeat row, optionally carrying
    ``meta`` (e.g. the NAS-probe fields written by ``precis heartbeat``)."""
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO host_heartbeat (host, ts, meta) "
            "VALUES (%s, now() - (%s || ' minutes')::interval, %s::jsonb) "
            "ON CONFLICT (host) DO UPDATE SET ts = EXCLUDED.ts, meta = EXCLUDED.meta",
            (host, minutes_ago, json.dumps(meta or {})),
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
        store, host, _CONTINUOUS_DAEMON, minutes_ago=DEAD_WORKER_SILENCE_MIN + 5
    )
    _seed_heartbeat(store, host, minutes_ago=0)  # host is up

    findings = _detect_dead_workers(store)
    key = f"dead-worker:{host}:{_CONTINUOUS_DAEMON}"
    hits = [f for f in findings if f.fingerprint_key == key]
    assert len(hits) == 1
    assert hits[0].category == "dead-worker"
    assert hits[0].ref_id is None


def test_dead_worker_ignores_live_daemon(store: Store) -> None:
    """A daemon that logged recently is not dead."""
    host = _host()
    _seed_worker_log(store, host, _CONTINUOUS_DAEMON, minutes_ago=1)
    _seed_heartbeat(store, host, minutes_ago=0)

    findings = _detect_dead_workers(store)
    assert not any(
        f.fingerprint_key == f"dead-worker:{host}:{_CONTINUOUS_DAEMON}"
        for f in findings
    )


def test_dead_worker_ignores_when_whole_host_down(store: Store) -> None:
    """Silent daemon + no host liveness signal ⇒ a host/DB outage, not a
    per-daemon dead-worker (don't fan one failure into N alerts)."""
    host = _host()
    _seed_worker_log(
        store, host, _CONTINUOUS_DAEMON, minutes_ago=DEAD_WORKER_SILENCE_MIN + 5
    )
    # no fresh log, no heartbeat → host not "alive"

    findings = _detect_dead_workers(store)
    assert not any(
        f.fingerprint_key == f"dead-worker:{host}:{_CONTINUOUS_DAEMON}"
        for f in findings
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


def test_host_dark_fires_while_dead_worker_self_suppresses(store: Store) -> None:
    """gr186752: a dead single-writer host raises host-dark critical — and
    dead-worker, gated on host_alive, stays quiet for the same host (no
    N-fold noise per daemon the dead host ran)."""
    host = _host()
    _seed_worker_log(
        store, host, _CONTINUOUS_DAEMON, minutes_ago=DEAD_WORKER_SILENCE_MIN + 5
    )
    _seed_heartbeat(store, host, minutes_ago=HOST_DARK_SILENCE_MIN + 5)  # stale too

    host_dark = _detect_host_dark(store)
    key = f"host-dark:{host}"
    hits = [f for f in host_dark if f.fingerprint_key == key]
    assert len(hits) == 1
    assert hits[0].category == "host-dark"
    assert hits[0].ref_id is None
    assert host in hits[0].title

    dead_worker = _detect_dead_workers(store)
    assert not any(
        f.fingerprint_key == f"dead-worker:{host}:{_CONTINUOUS_DAEMON}"
        for f in dead_worker
    )


def test_host_dark_ignores_live_host(store: Store) -> None:
    """A fresh heartbeat means the host itself is not dark."""
    host = _host()
    _seed_worker_log(store, host, "precis-worker", minutes_ago=1)
    _seed_heartbeat(store, host, minutes_ago=0)

    findings = _detect_host_dark(store)
    assert not any(f.fingerprint_key == f"host-dark:{host}" for f in findings)


def test_host_dark_ages_out_past_lookback(store: Store) -> None:
    """A host with no worker_logs activity in HOST_DARK_LOOKBACK_DAYS is
    decommissioned, not dark — its lingering host_heartbeat row must not
    alarm forever."""
    host = _host()
    _seed_worker_log(
        store,
        host,
        "precis-worker",
        minutes_ago=(HOST_DARK_LOOKBACK_DAYS + 1) * 24 * 60,
    )
    _seed_heartbeat(store, host, minutes_ago=HOST_DARK_SILENCE_MIN + 5)

    findings = _detect_host_dark(store)
    assert not any(f.fingerprint_key == f"host-dark:{host}" for f in findings)


def test_dead_worker_message_is_hedged_across_platforms() -> None:
    """gr180078: no hardcoded launchctl-only text."""
    for plat in ("Darwin", "Linux", "darwin", "linux", None):
        detail = _dead_worker_detail("precis-worker", "h", 12.0, plat)
        assert "dead or wedged" in detail


def test_dead_worker_message_tailors_command_to_linux(store: Store) -> None:
    """A dead worker on a Linux/systemd host gets systemctl advice, not
    launchctl — host_heartbeat.meta.platform (title-cased, unlike the boot
    row's sys.platform) drives it."""
    host = _host()
    _seed_worker_log(
        store, host, _CONTINUOUS_DAEMON, minutes_ago=DEAD_WORKER_SILENCE_MIN + 5
    )
    _seed_heartbeat(store, host, minutes_ago=0, meta={"platform": "Linux"})

    finding = next(
        f
        for f in _detect_dead_workers(store)
        if f.fingerprint_key == f"dead-worker:{host}:{_CONTINUOUS_DAEMON}"
    )
    assert "systemctl" in finding.detail
    assert "launchctl" not in finding.detail


def test_dead_worker_message_tailors_command_to_macos(store: Store) -> None:
    """A dead worker on a macOS/launchd host keeps the launchctl advice."""
    host = _host()
    _seed_worker_log(
        store, host, _CONTINUOUS_DAEMON, minutes_ago=DEAD_WORKER_SILENCE_MIN + 5
    )
    _seed_heartbeat(store, host, minutes_ago=0, meta={"platform": "Darwin"})

    finding = next(
        f
        for f in _detect_dead_workers(store)
        if f.fingerprint_key == f"dead-worker:{host}:{_CONTINUOUS_DAEMON}"
    )
    assert "launchctl" in finding.detail
    assert "systemctl" not in finding.detail


def test_dead_worker_message_neutral_without_platform(store: Store) -> None:
    """No platform in the heartbeat meta ⇒ names both OSes' tools rather
    than guessing."""
    host = _host()
    _seed_worker_log(
        store, host, _CONTINUOUS_DAEMON, minutes_ago=DEAD_WORKER_SILENCE_MIN + 5
    )
    _seed_heartbeat(store, host, minutes_ago=0)  # no meta

    finding = next(
        f
        for f in _detect_dead_workers(store)
        if f.fingerprint_key == f"dead-worker:{host}:{_CONTINUOUS_DAEMON}"
    )
    assert "systemctl" in finding.detail and "launchctl" in finding.detail


def test_dead_worker_still_flags_after_multi_day_silence(store: Store) -> None:
    """A critical daemon dead for DAYS (not just minutes) must stay flagged —
    the gr176223 regression. The old 24h lookback floor aged a >24h-dead
    worker out of the candidate set, so the critical alert auto-resolved and a
    4-day agent-worker outage read as fixed after day one. With the widened
    floor a worker silent 4 days on a live host still fires."""
    host = _host()
    four_days_min = 4 * 24 * 60  # > 24h (old floor), < 30d (retention)
    _seed_worker_log(store, host, _CONTINUOUS_DAEMON, minutes_ago=four_days_min)
    _seed_heartbeat(store, host, minutes_ago=0)  # host alive (heartbeat daemon up)

    findings = _detect_dead_workers(store)
    key = f"dead-worker:{host}:{_CONTINUOUS_DAEMON}"
    hits = [f for f in findings if f.fingerprint_key == key]
    assert len(hits) == 1
    assert hits[0].category == "dead-worker"


def test_dead_worker_ignores_daemon_gone_past_retention(store: Store) -> None:
    """The floor still protects against a decommissioned daemon alarming
    forever: a (host, process) last seen beyond DEAD_WORKER_LOOKBACK_DAYS is
    treated as gone, not dead — natural log pruning, not an arbitrary 24h cap,
    is what drops it."""
    host = _host()
    past_retention_min = (DEAD_WORKER_LOOKBACK_DAYS + 1) * 24 * 60
    _seed_worker_log(store, host, _CONTINUOUS_DAEMON, minutes_ago=past_retention_min)
    _seed_heartbeat(store, host, minutes_ago=0)

    findings = _detect_dead_workers(store)
    assert not any(
        f.fingerprint_key == f"dead-worker:{host}:{_CONTINUOUS_DAEMON}"
        for f in findings
    )


def test_run_nursery_pass_raises_critical_for_dead_worker(store: Store) -> None:
    """End to end: a dead-worker finding becomes an open ``critical`` alert
    (exercises the fingerprint_key + is_new push-gate path; the push itself
    is a no-op with no webhook configured)."""
    host = _host()
    _seed_worker_log(
        store, host, _CONTINUOUS_DAEMON, minutes_ago=DEAD_WORKER_SILENCE_MIN + 5
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


# ── embed-lane-stalled (embedder-wedge-hardening.md §4) ─────────────


def _mint_embed_batch_job(
    store: Store, *, status: str, status_age_minutes: float = 0.0
) -> int:
    """Mint an ``embed_batch`` job with a ``STATUS`` tag, optionally
    backdating the TAG's ``created_at`` (not the ref's) — the detector
    reads ``STATUS:succeeded`` transition recency from
    ``ref_tags.created_at``, mirroring ``health_digest.py``'s own
    embed_batch status-window query."""
    ref = store.insert_ref(
        kind="job",
        slug=None,
        title="embed_batch job",
        meta={"job_type": "embed_batch"},
    )
    store.add_tag(ref.id, Tag.closed("STATUS", status), set_by="agent")
    if status_age_minutes:
        _backdate_status_tag(store, ref.id, status_age_minutes / 60.0)
    return ref.id


# Comfortably past the succeeded-transition window.
_EMBED_STALL_M = EMBED_LANE_STALL_WINDOW_MIN + 5


def test_embed_lane_stalled_flags_queued_with_no_recent_successes(
    store: Store,
) -> None:
    """A queued embed_batch job with zero STATUS:succeeded transitions in
    the window is the wedge pattern (embedder alive, /readyz can even
    look fine, but nothing is actually completing) → one critical
    finding."""
    _mint_embed_batch_job(store, status="queued")

    findings = _detect_embed_lane_stalled(store)
    assert len(findings) == 1
    f = findings[0]
    assert f.category == "embed-lane-stalled"
    assert f.ref_id is None  # a cluster-wide condition, not per-job
    assert f.fingerprint_key == "embed-lane-stalled"
    assert "queued" in f.detail


def test_embed_lane_stalled_ignores_when_a_recent_success_exists(
    store: Store,
) -> None:
    """A queued backlog behind a lane that's actually draining (a recent
    STATUS:succeeded transition) is healthy, not stalled."""
    _mint_embed_batch_job(store, status="queued")
    _mint_embed_batch_job(store, status="succeeded", status_age_minutes=5)

    assert _detect_embed_lane_stalled(store) == []


def test_embed_lane_stalled_ignores_stale_success_outside_window(
    store: Store,
) -> None:
    """A success from BEFORE the window doesn't count — the lane must be
    draining recently, not merely have drained once in the past."""
    _mint_embed_batch_job(store, status="queued")
    _mint_embed_batch_job(store, status="succeeded", status_age_minutes=_EMBED_STALL_M)

    findings = _detect_embed_lane_stalled(store)
    assert len(findings) == 1


def test_embed_lane_stalled_ignores_no_queued_jobs(store: Store) -> None:
    """Nothing queued means nothing is waiting on the lane — a terminal
    success/failure with no queue backlog must not alarm."""
    _mint_embed_batch_job(store, status="succeeded")
    _mint_embed_batch_job(store, status="failed")

    assert _detect_embed_lane_stalled(store) == []


def test_run_nursery_pass_raises_critical_for_embed_lane_stalled(
    store: Store,
) -> None:
    """End to end: an embed-lane-stalled finding becomes an open
    ``critical`` alert under the ``nursery:embed-lane-stalled`` source."""
    _mint_embed_batch_job(store, status="queued")

    run_nursery_pass(store)

    alerts = list_open_alerts(store)
    mine = [a for a in alerts if a["source"] == "nursery:embed-lane-stalled"]
    assert len(mine) == 1
    assert mine[0]["severity"] == "critical"


# ── nas-denied (launchd-context NAS lockout) ───────────────────────


def test_nas_denied_flags_fresh_false_heartbeat(store: Store) -> None:
    host = _host()
    _seed_heartbeat(
        store,
        host,
        minutes_ago=0,
        meta={"nas_ok": False, "nas_path": "/opt/nas/botshome", "nas_errno": 1},
    )

    findings = _detect_nas_denied(store)
    hits = [f for f in findings if f.fingerprint_key == f"nas-denied:{host}"]
    assert len(hits) == 1
    assert hits[0].category == "nas-denied"
    assert hits[0].ref_id is None
    assert host in hits[0].title


def test_nas_denied_ignores_readable_nas(store: Store) -> None:
    host = _host()
    _seed_heartbeat(store, host, minutes_ago=0, meta={"nas_ok": True})

    findings = _detect_nas_denied(store)
    assert not any(f.fingerprint_key == f"nas-denied:{host}" for f in findings)


def test_nas_denied_ignores_stale_heartbeat(store: Store) -> None:
    """A false NAS reading past the 5-min freshness gate doesn't linger — a
    stale row usually means a host/DB outage, a different failure."""
    host = _host()
    _seed_heartbeat(
        store,
        host,
        minutes_ago=10,
        meta={"nas_ok": False, "nas_path": "/opt/nas/botshome"},
    )

    findings = _detect_nas_denied(store)
    assert not any(f.fingerprint_key == f"nas-denied:{host}" for f in findings)


def test_run_nursery_pass_raises_critical_for_nas_denied_and_auto_resolves(
    store: Store,
) -> None:
    """End to end: a nas-denied finding becomes an open ``critical`` alert
    and auto-resolves once the heartbeat flips ``nas_ok`` back to true."""
    host = _host()
    _seed_heartbeat(
        store,
        host,
        minutes_ago=0,
        meta={"nas_ok": False, "nas_path": "/opt/nas/botshome"},
    )

    run_nursery_pass(store)

    alerts = list_open_alerts(store)
    mine = [
        a
        for a in alerts
        if a["source"] == "nursery:nas-denied" and host in (a["title"] or "")
    ]
    assert len(mine) == 1
    assert mine[0]["severity"] == "critical"

    # NAS access recovers — the heartbeat flips true.
    _seed_heartbeat(store, host, minutes_ago=0, meta={"nas_ok": True})
    run_nursery_pass(store)

    alerts_after = list_open_alerts(store)
    assert not any(
        a["source"] == "nursery:nas-denied" and host in (a["title"] or "")
        for a in alerts_after
    )
