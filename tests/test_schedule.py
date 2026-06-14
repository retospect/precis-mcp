"""Slice-4 schedule + PRIO tests.

Five layers:

* the cron parser + ``every:`` shorthand translator;
* the Watches umbrella seed (idempotent on ``meta.builtin``);
* the per-tick spawn loop (idempotency stamp, collision-skip,
  backfill on/off);
* the PRIO column wiring (``put(prio=N)``, ``tag(prio=N)``,
  back-compat ``PRIO:*`` tag alias);
* delete-protection on builtin refs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.todo import TodoHandler
from precis.store import Store
from precis.store.types import Tag
from precis.workers.schedule import (
    WATCHES_BUILTIN,
    Schedule,
    ensure_watches_root,
    run_schedule_pass,
    ticks_since,
    validate_schedule,
)
from precis.workers.schedule.parse import every_to_cron, parse_cron


@pytest.fixture
def handler(hub: Hub) -> TodoHandler:
    return TodoHandler(hub=hub)


def _id_of(body: str) -> int:
    return int(body.split("id=")[1].split()[0].rstrip(",.()"))


# ── parser: cron ───────────────────────────────────────────────────


def test_parse_cron_star_expands_to_full_range() -> None:
    fields = parse_cron("* * * * *")
    assert fields[0] == frozenset(range(60))
    assert fields[1] == frozenset(range(24))
    assert fields[4] == frozenset(range(7))


def test_parse_cron_value_and_range_and_step() -> None:
    fields = parse_cron("0 9 * * 1")
    assert fields[0] == frozenset({0})
    assert fields[1] == frozenset({9})
    assert fields[4] == frozenset({1})

    fields = parse_cron("*/15 * * * *")
    assert fields[0] == frozenset({0, 15, 30, 45})

    fields = parse_cron("0 9-17 * * 1-5")
    assert fields[1] == frozenset(range(9, 18))
    assert fields[4] == frozenset(range(1, 6))


def test_parse_cron_rejects_bad_shape() -> None:
    with pytest.raises(BadInput, match="must have 5 fields"):
        parse_cron("0 9 * *")
    with pytest.raises(BadInput, match="out of range"):
        parse_cron("0 25 * * *")
    with pytest.raises(BadInput, match="bad value"):
        parse_cron("zz 9 * * *")


# ── parser: every shorthand ────────────────────────────────────────


def test_every_translates_minutes() -> None:
    assert every_to_cron("15m") == "*/15 * * * *"
    assert every_to_cron("1m") == "* * * * *"


def test_every_translates_hours() -> None:
    assert every_to_cron("1h") == "0 * * * *"
    assert every_to_cron("6h") == "0 */6 * * *"


def test_every_translates_1d_only() -> None:
    assert every_to_cron("1d") == "0 0 * * *"
    with pytest.raises(BadInput, match="only every:1d"):
        every_to_cron("2d")


def test_every_translates_weekly_dow_hhmm() -> None:
    assert every_to_cron("mon 09:00") == "0 9 * * 1"
    assert every_to_cron("sun 14:30") == "30 14 * * 0"


def test_every_rejects_garbage() -> None:
    with pytest.raises(BadInput, match="unrecognised"):
        every_to_cron("frobnicate")


# ── validator (handler-boundary) ───────────────────────────────────


def test_validate_schedule_accepts_canonical_cron() -> None:
    s = validate_schedule({"cron": "0 9 * * 1"})
    assert s.cron == "0 9 * * 1"
    assert s.backfill_missed is False


def test_validate_schedule_translates_every_to_cron() -> None:
    s = validate_schedule({"every": "1h"})
    assert s.cron == "0 * * * *"


def test_validate_schedule_rejects_both_cron_and_every() -> None:
    with pytest.raises(BadInput, match="both 'cron' and 'every'"):
        validate_schedule({"cron": "0 * * * *", "every": "1h"})


def test_validate_schedule_rejects_unknown_key() -> None:
    with pytest.raises(BadInput, match="unknown meta.schedule keys"):
        validate_schedule({"cron": "0 * * * *", "frob": True})


def test_validate_schedule_rejects_bad_backfill_type() -> None:
    with pytest.raises(BadInput, match="backfill_missed must be a bool"):
        validate_schedule({"cron": "0 * * * *", "backfill_missed": "yes"})


# ── ticks_since ────────────────────────────────────────────────────


def test_ticks_since_returns_just_the_latest_no_backfill() -> None:
    sched = Schedule(cron="0 * * * *", backfill_missed=False)
    now = datetime(2026, 6, 14, 12, 30, tzinfo=UTC)
    last = datetime(2026, 6, 14, 8, 0, tzinfo=UTC)
    ticks = ticks_since(last, sched, now=now)
    # Without backfill, only the 12:00 tick is returned (most recent).
    assert ticks == [datetime(2026, 6, 14, 12, 0, tzinfo=UTC)]


def test_ticks_since_returns_all_missed_with_backfill() -> None:
    sched = Schedule(cron="0 * * * *", backfill_missed=True)
    now = datetime(2026, 6, 14, 12, 30, tzinfo=UTC)
    last = datetime(2026, 6, 14, 8, 0, tzinfo=UTC)
    ticks = ticks_since(last, sched, now=now)
    assert ticks == [
        datetime(2026, 6, 14, 9, 0, tzinfo=UTC),
        datetime(2026, 6, 14, 10, 0, tzinfo=UTC),
        datetime(2026, 6, 14, 11, 0, tzinfo=UTC),
        datetime(2026, 6, 14, 12, 0, tzinfo=UTC),
    ]


def test_ticks_since_empty_when_no_match_in_window() -> None:
    sched = Schedule(cron="0 9 * * *", backfill_missed=False)
    now = datetime(2026, 6, 14, 10, 0, tzinfo=UTC)
    # Last tick was the 9:00 fire; nothing new yet.
    last = datetime(2026, 6, 14, 9, 0, tzinfo=UTC)
    assert ticks_since(last, sched, now=now) == []


# ── Watches umbrella seed (DB) ─────────────────────────────────────


def test_ensure_watches_root_is_idempotent(store: Store) -> None:
    a = ensure_watches_root(store)
    b = ensure_watches_root(store)
    assert a == b
    ref = store.get_ref(kind="todo", id=a)
    assert ref is not None
    assert ref.meta.get("builtin") == WATCHES_BUILTIN
    tags = {str(t) for t in store.tags_for(a)}
    assert "level:recurring" in tags


def test_watches_root_delete_is_refused(
    handler: TodoHandler, store: Store
) -> None:
    rid = ensure_watches_root(store)
    with pytest.raises(BadInput, match="builtin"):
        handler.delete(id=rid)


# ── put: recurring defaults under Watches ──────────────────────────


def test_put_level_recurring_defaults_parent_to_watches(
    handler: TodoHandler, store: Store
) -> None:
    resp = handler.put(
        text="Check arxiv weekly",
        tags=["level:recurring"],
        meta={"schedule": {"cron": "0 9 * * 1"}},
    )
    rid = _id_of(resp.body)
    ref = store.get_ref(kind="todo", id=rid)
    assert ref is not None
    watches_id = ensure_watches_root(store)
    assert ref.parent_id == watches_id
    # Schedule was canonicalised in place (cron survives unchanged here,
    # but the back-compat path strips the every shorthand on shorthand
    # writes — test that separately below).
    assert ref.meta["schedule"]["cron"] == "0 9 * * 1"
    assert ref.meta["schedule"]["backfill_missed"] is False


def test_put_recurring_with_every_shorthand_is_canonicalised(
    handler: TodoHandler, store: Store
) -> None:
    resp = handler.put(
        text="Hourly check",
        tags=["level:recurring"],
        meta={"schedule": {"every": "1h"}},
    )
    rid = _id_of(resp.body)
    ref = store.get_ref(kind="todo", id=rid)
    assert ref is not None
    assert ref.meta["schedule"] == {"cron": "0 * * * *", "backfill_missed": False}


def test_put_with_bad_schedule_rejected_at_write_time(
    handler: TodoHandler,
) -> None:
    with pytest.raises(BadInput, match="cron must have 5 fields"):
        handler.put(
            text="bad",
            tags=["level:recurring"],
            meta={"schedule": {"cron": "0 9 * *"}},
        )


# ── PRIO column ────────────────────────────────────────────────────


def test_put_with_prio_writes_column(
    handler: TodoHandler, store: Store
) -> None:
    resp = handler.put(text="prio task", prio=1)
    rid = _id_of(resp.body)
    ref = store.get_ref(kind="todo", id=rid)
    assert ref is not None and ref.prio == 1


def test_put_rejects_out_of_range_prio(handler: TodoHandler) -> None:
    with pytest.raises(BadInput, match="out of range"):
        handler.put(text="bad", prio=11)
    with pytest.raises(BadInput, match="out of range"):
        handler.put(text="bad", prio=0)


def test_put_with_prio_tag_back_compat(
    handler: TodoHandler, store: Store
) -> None:
    resp = handler.put(text="urgent", tags=["PRIO:urgent"])
    rid = _id_of(resp.body)
    ref = store.get_ref(kind="todo", id=rid)
    assert ref is not None and ref.prio == 1
    tags = {str(t) for t in store.tags_for(rid)}
    # Back-compat: the tag form is consumed; column carries the value.
    assert "PRIO:urgent" not in tags


def test_tag_prio_kwarg_writes_column(
    handler: TodoHandler, store: Store
) -> None:
    resp = handler.put(text="x")
    rid = _id_of(resp.body)
    handler.tag(id=rid, prio=3)
    ref = store.get_ref(kind="todo", id=rid)
    assert ref is not None and ref.prio == 3


def test_tag_clears_prio_via_remove_prio_tag(
    handler: TodoHandler, store: Store
) -> None:
    resp = handler.put(text="y", prio=8)
    rid = _id_of(resp.body)
    handler.tag(id=rid, remove=["PRIO:low"])
    ref = store.get_ref(kind="todo", id=rid)
    assert ref is not None and ref.prio is None


# ── spawn loop ─────────────────────────────────────────────────────


def _set_last_tick(store: Store, ref_id: int, when: datetime) -> None:
    """Inject a synthetic ``schedule:spawn`` event so ticks_since
    treats the recurring as having last fired at ``when``."""
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO ref_events (ref_id, source, event, ts, payload) "
            "VALUES (%s, 'schedule', 'spawn', %s, '{}'::jsonb)",
            (ref_id, when),
        )
        conn.commit()


def test_schedule_pass_spawns_one_child(
    handler: TodoHandler, store: Store
) -> None:
    # Daily at 00:00, no backfill, last tick 25h ago → mints today's.
    resp = handler.put(
        text="Daily",
        tags=["level:recurring"],
        meta={"schedule": {"cron": "0 0 * * *"}},
    )
    rid = _id_of(resp.body)
    yesterday_tick = datetime.now(UTC) - timedelta(hours=25)
    _set_last_tick(store, rid, yesterday_tick)

    result = run_schedule_pass(store, limit=50)
    assert result.claimed >= 1
    assert result.ok >= 1

    # Child is parented under the recurring and carries the tick stamp
    # + prio=2.
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, prio, meta->>'spawned_for_tick' "
            "FROM refs WHERE parent_id = %s AND deleted_at IS NULL",
            (rid,),
        ).fetchall()
    assert len(rows) == 1
    assert int(rows[0][1]) == 2
    assert rows[0][2] is not None


def test_schedule_pass_is_idempotent_same_minute(
    handler: TodoHandler, store: Store
) -> None:
    resp = handler.put(
        text="Daily",
        tags=["level:recurring"],
        meta={"schedule": {"cron": "0 0 * * *"}},
    )
    rid = _id_of(resp.body)
    yesterday_tick = datetime.now(UTC) - timedelta(hours=25)
    _set_last_tick(store, rid, yesterday_tick)
    run_schedule_pass(store, limit=50)
    run_schedule_pass(store, limit=50)
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT count(*) FROM refs WHERE parent_id = %s "
            "AND deleted_at IS NULL",
            (rid,),
        ).fetchone()
    assert rows is not None
    assert int(rows[0]) == 1


def test_schedule_pass_skips_when_previous_still_open(
    handler: TodoHandler, store: Store
) -> None:
    resp = handler.put(
        text="Hourly",
        tags=["level:recurring"],
        meta={"schedule": {"cron": "0 * * * *", "backfill_missed": True}},
    )
    rid = _id_of(resp.body)
    # Two hours back so two ticks are due if backfill kicks in. The
    # first tick mints; the second tick observes the first still open
    # and skips.
    two_hours_back = datetime.now(UTC) - timedelta(hours=2, minutes=5)
    _set_last_tick(store, rid, two_hours_back)
    result = run_schedule_pass(store, limit=50)
    assert result.ok == 1
    assert result.failed >= 1  # the second tick was skipped


def test_schedule_pass_skips_folder_umbrella(store: Store) -> None:
    # Watches root has schedule=None; sweep should walk past it.
    umbrella = ensure_watches_root(store)
    result = run_schedule_pass(store, limit=50)
    # No children minted under the umbrella.
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT count(*) FROM refs WHERE parent_id = %s",
            (umbrella,),
        ).fetchone()
    assert rows is not None
    assert int(rows[0]) == 0
    _ = result


def test_schedule_pass_row_lock_serialises_concurrent_workers(
    handler: TodoHandler, store: Store
) -> None:
    """Two simulated workers racing on the same recurring serialise.

    Holds the FOR UPDATE row lock on the recurring in tx A, then
    fires a full ``run_schedule_pass`` from tx B. The pass should
    skip the locked recurring (no children minted) and return a
    clean ``claimed=0`` for that ref. When the holder commits and
    the pass runs again, the spawn happens.
    """
    resp = handler.put(
        text="Locked-once",
        tags=["level:recurring"],
        meta={"schedule": {"cron": "0 * * * *"}},
    )
    rid = _id_of(resp.body)
    _set_last_tick(store, rid, datetime.now(UTC) - timedelta(hours=2))

    # Open a second connection from the pool, BEGIN, take the row
    # lock — same SQL the worker uses. Don't commit yet.
    holder = store.pool.getconn()
    try:
        holder.execute("BEGIN")
        row = holder.execute(
            "SELECT ref_id FROM refs WHERE ref_id = %s FOR UPDATE",
            (rid,),
        ).fetchone()
        assert row is not None

        # While the lock is held, run a full pass — it should see
        # zero claimed refs (SKIP LOCKED bypasses the held row).
        result = run_schedule_pass(store, limit=50)
        assert result.claimed == 0
        assert result.ok == 0

        # Confirm no child was minted while locked.
        with store.pool.connection() as c:
            n = c.execute(
                "SELECT count(*) FROM refs WHERE parent_id = %s "
                "AND deleted_at IS NULL",
                (rid,),
            ).fetchone()
        assert n is not None and int(n[0]) == 0

        # Release the lock.
        holder.execute("COMMIT")
    finally:
        store.pool.putconn(holder)

    # Now the next pass takes the lock and spawns.
    result2 = run_schedule_pass(store, limit=50)
    assert result2.claimed == 1
    assert result2.ok == 1


def test_schedule_pass_skips_paused_recurring(
    handler: TodoHandler, store: Store
) -> None:
    resp = handler.put(
        text="Paused",
        tags=["level:recurring"],
        meta={"schedule": {"cron": "0 * * * *"}},
    )
    rid = _id_of(resp.body)
    _set_last_tick(store, rid, datetime.now(UTC) - timedelta(hours=2))
    # Pause the recurring directly. The fixture runs as owner, so
    # ``handler.tag`` would work, but going via the store keeps the
    # test focused on the spawner's behaviour. ``replace_prefix=True``
    # mirrors how the handler writes closed-prefix tags atomically
    # (STATUS:open → STATUS:paused in one tx).
    store.add_tag(
        rid,
        Tag.closed("STATUS", "paused"),
        set_by="agent",
        replace_prefix=True,
    )
    result = run_schedule_pass(store, limit=50)
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT count(*) FROM refs WHERE parent_id = %s "
            "AND deleted_at IS NULL",
            (rid,),
        ).fetchone()
    assert rows is not None
    assert int(rows[0]) == 0
    _ = result
