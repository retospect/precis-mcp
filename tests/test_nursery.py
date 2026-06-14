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

from precis.dispatch import Hub
from precis.handlers.todo import TodoHandler
from precis.store import Store
from precis.store.types import Tag
from precis.workers.nursery import (
    LONG_WAIT_DAYS,
    STALE_CLAIM_HOURS,
    STUCK_DOABLE_HOURS,
    Finding,
    _detect_long_waits,
    _detect_orphans,
    _detect_stale_claims,
    _detect_stalled_recurrings,
    _detect_stuck_doable,
    _fingerprint,
    _last_digest_matches,
    _render_digest_body,
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
            "UPDATE refs SET created_at = now() - %s::interval "
            "WHERE ref_id = %s",
            (f"{hours} hours", ref_id),
        )
        conn.commit()


def _backdate_tag(
    store: Store, ref_id: int, tag_value: str, hours: float
) -> None:
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


# ── fingerprint (pure) ─────────────────────────────────────────────


def test_fingerprint_is_order_independent() -> None:
    a = [
        Finding("orphan", 1, "t", "d"),
        Finding("stale-claim", 2, "t", "d"),
    ]
    b = [
        Finding("stale-claim", 2, "t", "d"),
        Finding("orphan", 1, "t", "d"),
    ]
    assert _fingerprint(a) == _fingerprint(b)


def test_fingerprint_ignores_title_and_detail() -> None:
    """Title/detail edits on the same (cat, ref_id) don't change the hash."""
    a = [Finding("orphan", 1, "old title", "old detail")]
    b = [Finding("orphan", 1, "new title", "new detail")]
    assert _fingerprint(a) == _fingerprint(b)


def test_fingerprint_changes_on_finding_set_change() -> None:
    a = [Finding("orphan", 1, "t", "d")]
    b = [Finding("orphan", 1, "t", "d"), Finding("orphan", 2, "t", "d")]
    assert _fingerprint(a) != _fingerprint(b)


def test_fingerprint_distinguishes_categories() -> None:
    a = [Finding("orphan", 1, "t", "d")]
    b = [Finding("stuck-doable", 1, "t", "d")]
    assert _fingerprint(a) != _fingerprint(b)


# ── digest body render (pure) ──────────────────────────────────────


def test_render_digest_body_groups_by_category() -> None:
    findings = [
        Finding("orphan", 11, "Orphan one", "no strategic ancestor"),
        Finding("orphan", 12, "Orphan two", "no strategic ancestor"),
        Finding("stale-claim", 21, "Stale one", "claimed 5h ago"),
    ]
    body = _render_digest_body(findings, today="2026-06-14")
    assert "Nursery digest 2026-06-14: 2 orphan, 1 stale-claim." in body
    assert "## orphan (2)" in body
    assert "## stale-claim (1)" in body
    assert "- #11 Orphan one" in body
    assert "- #21 Stale one" in body


def test_render_digest_body_skips_missing_categories() -> None:
    findings = [Finding("orphan", 1, "t", "d")]
    body = _render_digest_body(findings, today="2026-06-14")
    assert "## orphan (1)" in body
    assert "## stale-claim" not in body
    assert "## stuck-doable" not in body


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
    r = handler.put(text="Waiting on reto")
    rid = _id_of(r.body)
    store.add_tag(rid, Tag.open("waiting-for:reto"), set_by="agent")
    _backdate_tag(
        store, rid, "waiting-for:reto", (LONG_WAIT_DAYS + 1) * 24
    )

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


# ── full pass + dedup ──────────────────────────────────────────────


def test_full_pass_writes_digest_when_findings_appear(
    handler: TodoHandler, store: Store
) -> None:
    # Two orphans + one stale claim.
    a = handler.put(text="Orphan A")
    b = handler.put(text="Orphan B")
    c = handler.put(text="Claimed")
    aid = _id_of(a.body)
    bid = _id_of(b.body)
    cid = _id_of(c.body)
    store.add_tag(cid, Tag.open("claimed-by:asa-worker"), set_by="agent")
    _backdate_tag(store, cid, "claimed-by:asa-worker", STALE_CLAIM_HOURS + 1)

    result = run_nursery_pass(store)
    assert result.claimed >= 3
    assert result.ok == 1  # digest written

    # Confirm the memory landed with the right tags.
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT r.ref_id, r.meta->>'nursery_finding_count'
              FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.kind = 'memory' AND r.deleted_at IS NULL
               AND t.namespace = 'OPEN' AND t.value = 'tier:nursery'
             ORDER BY r.created_at DESC LIMIT 1
            """,
        ).fetchone()
    assert row is not None
    assert int(row[1]) >= 3
    _ = aid, bid


def test_full_pass_dedups_repeat_findings(
    handler: TodoHandler, store: Store
) -> None:
    r = handler.put(text="Orphan O")
    rid = _id_of(r.body)
    _ = rid
    run_nursery_pass(store)
    # Second pass with the same findings → no new memory.
    result2 = run_nursery_pass(store)
    assert result2.ok == 0
    assert result2.claimed >= 1


def test_full_pass_writes_again_after_findings_change(
    handler: TodoHandler, store: Store
) -> None:
    handler.put(text="First orphan")
    run_nursery_pass(store)
    # Add a new orphan; the fingerprint changes.
    handler.put(text="Second orphan")
    result2 = run_nursery_pass(store)
    assert result2.ok == 1


def test_full_pass_empty_returns_clean(
    handler: TodoHandler, store: Store
) -> None:
    # No todos, no findings.
    result = run_nursery_pass(store)
    assert result.claimed == 0
    assert result.ok == 0


def test_last_digest_matches_returns_false_when_no_digest(store: Store) -> None:
    assert _last_digest_matches(store, "abc123") is False


def test_last_digest_matches_true_after_write(
    handler: TodoHandler, store: Store
) -> None:
    handler.put(text="Orphan X")
    run_nursery_pass(store)
    # Fetch the fingerprint we just wrote.
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT r.meta->>'nursery_fingerprint'
              FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.kind = 'memory'
               AND t.namespace = 'OPEN' AND t.value = 'tier:nursery'
             ORDER BY r.created_at DESC LIMIT 1
            """,
        ).fetchone()
    assert row is not None
    fingerprint = row[0]
    assert _last_digest_matches(store, fingerprint) is True
    assert _last_digest_matches(store, "different") is False


def test_helpers_hours_since_works() -> None:
    """``_hours_since`` handles naive timestamps + None."""
    from precis.workers.nursery import _hours_since

    now = datetime.now(UTC)
    assert _hours_since(now) < 0.01
    assert _hours_since(now - timedelta(hours=3)) > 2.9
    assert _hours_since(None) == 0.0
